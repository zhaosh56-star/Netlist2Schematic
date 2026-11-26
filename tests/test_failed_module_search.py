"""Failure placement uses real geometry, bounded transactions and saved guides."""
from copy import deepcopy
import json
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_terminal_slots import fixture
from tests.test_passive_placement import base_placement, resistor
from tests.test_series_alignment import PAR
from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.plugins.passive_skeleton.plan import compile_plan
from netlist2schematic.plugins.passive_skeleton import placement, packing_search
from netlist2schematic.plugins.passive_skeleton.module_connections import check_guides
from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.sa_routing import final_route


def case(topology='single', rename=False):
    devices, cells, _ = fixture(topology=topology)
    if rename:
        name = lambda n:'renamed_'+n[::-1]
        net = lambda n:n if n in ('0','vdd','vss','vee') else 'net_'+n[::-1]
        devices = {name(n):{k:net(v) if k in ('d','g','s','bulk','a','b','p','n') else v
                            for k,v in d.items()} for n,d in reversed(list(devices.items()))}
        cells = {name(n):cell for n,cell in cells.items()}
    core, _, _, _ = base_placement({n:d for n,d in devices.items() if n in cells},
                                   cells, blocks=[list(cells)[:2]])
    core.column_x = [64+124*k for k in range(core.cols)]
    core.row_y = [64+126*k for k in range(len(core.row_y))]
    document = to_document(devices)
    comps, nets = document['components'], document['nets']
    index = {c['id']:i for i,c in enumerate(comps)}
    plan = compile_plan(PAR, devices, comps, nets, analyze_structures(devices))
    return core, comps, nets, index, plan, RoutingOptions()


def rejected_case(args):
    """A legacy slot exhausts at an occupied height, with a real checkpoint."""
    original = placement.slot_trials
    def blocked(par, frame, comps, index, group, options):
        trial = next(original(par, frame, comps, index, group, options))
        host = next(i for i in range(len(comps)) if i not in frame.layout_guest_ids)
        guest = index[group['members'][0]]
        delta = [frame.pos_of(host)[k]-trial.pos_of(guest)[k] for k in (0,1)]
        for name in group['members']:
            i = index[name]
            for k in (0,1):
                trial.passive_local_offsets[i][k] += delta[k]
        yield trial
    with patch.object(placement, 'slot_trials', side_effect=blocked):
        result, report = placement.pack(PAR, *args, seconds=2, refine_terminal_placements=False)
    assert result is None
    assert report['stop_reason'] == 'no_legal_slot', report
    return report


class FailedModuleSearchTests(unittest.TestCase):
    def transaction(self, allow_relocation):
        devices, cells, _ = fixture()
        devices.pop('VREF')
        devices['RA'] = resistor('input', '0')
        devices['RB'] = resistor('other', '0')
        core, _, _, _ = base_placement({n:d for n,d in devices.items() if n in cells}, cells)
        document = to_document(devices)
        comps, nets = document['components'], document['nets']
        index = {c['id']:i for i,c in enumerate(comps)}
        plan = compile_plan(PAR, devices, comps, nets, analyze_structures(devices))
        options = RoutingOptions()
        frame = placement.make_frame(PAR, core, comps, index, options)
        old, new = sorted(plan['groups'], key=lambda g:g['members'])
        def position(state, group, target):
            vector, *_ = placement._anchor_geometry(PAR, state, comps, index, group, options)
            geometry = placement._geometry(PAR, state, comps, index, group, vector, options)
            return placement._translated(state, geometry, target, options.grid_step)
        frame = position(frame, old, (50, 600))
        original = deepcopy(frame.__dict__)
        def trials(par, state, cs, ns, ix, group, op, **kwargs):
            target = (250, 600) if group['id'] == old['id'] and allow_relocation else (50, 600)
            yield position(state, group, target)
        report = dict(trials=0)
        search = packing_search.LocalPackingSearch(PAR, comps, nets, index,
            plan, options, perf_counter()+3, 128, report)
        with patch.object(packing_search, 'placement_trials', side_effect=trials):
            result = search.find(frame, [new], [old])
        self.assertEqual(frame.passive_local_offsets, original['passive_local_offsets'])
        self.assertEqual(frame.passive_module_guides, original['passive_module_guides'])
        self.assertLessEqual(len(report['insertion_search']['transactions']), 4)
        return frame, result, report, comps, nets, index, options

    def test_only_direct_blocker_is_reinserted_as_an_atomic_transaction(self):
        before, after, report, comps, nets, index, options = self.transaction(True)
        self.assertIsNotNone(after, report)
        self.assertTrue(any(t['accepted'] for t in report['insertion_search']['transactions']))
        self.assertEqual(after.cell_of, before.cell_of)
        for i in range(len(comps)):
            if i not in before.layout_guest_ids:
                self.assertEqual(after.pos_of(i), before.pos_of(i))
                self.assertEqual(after.orient[i], before.orient[i])
        self.assertEqual(_static_faults(PAR, after, nets, index, options, None)['count'], 0)
        self.assertEqual(check_guides(PAR, after, nets, index, options), [])

    def test_failed_local_transaction_restores_positions_and_guides(self):
        _, result, report, *_ = self.transaction(False)
        self.assertIsNone(result)
        self.assertTrue(report['insertion_search']['transactions'])
        self.assertFalse(any(t['accepted'] for t in report['insertion_search']['transactions']))

    def test_exhausted_slot_resumes_in_two_dimensions_and_routes(self):
        args = case()
        core, comps, nets, index, plan, options = args
        previous = rejected_case(args)
        checkpoint = deepcopy(previous.checkpoint)
        with patch('netlist2schematic.routing.orthogonal_router._find_path',
                   side_effect=AssertionError('A* called during insertion')):
            placed, report = packing_search.resume_failed(PAR, *args, previous, seconds=3)
        self.assertIsNotNone(placed, report)
        self.assertTrue(report['insertion_search']['checkpoint_resumed'])
        self.assertFalse(_static_faults(PAR, placed, nets, index, options, None)['count'])
        self.assertEqual(check_guides(PAR, placed, nets, index, options), [])
        self.assertEqual(previous.checkpoint['frame'].passive_local_offsets,
                         checkpoint['frame'].passive_local_offsets)
        for i, comp in enumerate(core.comps):
            j = index[comp['id']]
            self.assertEqual(placed.pos_of(j), checkpoint['frame'].pos_of(j))
            self.assertEqual(placed.cell_of[j], core.cell_of[i])
            self.assertEqual(placed.orient[j], core.orient[i])
        self.assertTrue(final_route(PAR, nets, placed, index, deadline=perf_counter()+5)[2]['valid'])

    def test_runtime_checkpoint_is_private_and_deepcopyable(self):
        report = rejected_case(case())
        self.assertIsNotNone(report.checkpoint)
        text = json.dumps(report)
        self.assertNotIn('checkpoint', json.loads(text))
        other = deepcopy(report)
        self.assertIsNot(other.checkpoint['frame'], report.checkpoint['frame'])

    def test_translation_changes_only_the_absolute_output_coordinates(self):
        args = case()
        first, _ = packing_search.resume_failed(PAR, *args, rejected_case(args), seconds=3)
        shifted = case()
        shifted[0].column_x = [x+200 for x in shifted[0].column_x]
        shifted[0].row_y = [y+400 for y in shifted[0].row_y]
        second, _ = packing_search.resume_failed(PAR, *shifted, rejected_case(shifted), seconds=3)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        for i in range(len(args[1])):
            self.assertEqual(second.pos_of(i), tuple(first.pos_of(i)[k]+(200,400)[k] for k in (0,1)))

    def test_renaming_and_record_order_preserve_the_generic_result(self):
        args = case()
        first, _ = packing_search.resume_failed(PAR, *args, rejected_case(args), seconds=3)
        changed = case(rename=True)
        second, _ = packing_search.resume_failed(PAR, *changed, rejected_case(changed), seconds=3)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        for name,i in args[3].items():
            j=changed[3]['renamed_'+name[::-1]]
            self.assertEqual(first.pos_of(i),second.pos_of(j))
            self.assertEqual(first.orient[i],second.orient[j])

    def test_expired_or_zero_budget_has_no_trial_and_keeps_input(self):
        args = case()
        previous = rejected_case(args)
        for limits in (dict(deadline=0), dict(seconds=0), dict(max_trials=0)):
            with self.subTest(limits=limits):
                result, report = packing_search.resume_failed(PAR, *args, previous, **limits)
                self.assertIsNone(result)
                self.assertIs(report, previous)

    def test_incomplete_trial_times_out_and_restores_checkpoint(self):
        args = case()
        previous = rejected_case(args)
        before = deepcopy(previous.checkpoint['frame'].__dict__)
        timeout = RoutingFailure('interrupted check', dict(reason='quality_time_budget'))
        with patch('netlist2schematic.plugins.preflight.placement_preflight._static_faults', side_effect=timeout):
            result, report = packing_search.resume_failed(PAR, *args, previous)
        self.assertIsNone(result)
        self.assertFalse(report['insertion_search']['applied'])
        self.assertEqual(report['stop_reason'], 'local_budget')
        self.assertEqual(previous.checkpoint['frame'].passive_local_offsets, before['passive_local_offsets'])

    def test_new_spacing_profiles_get_trials_under_a_small_shared_limit(self):
        args = case('parallel')
        core, comps, nets, index, plan, options = args
        frame, _ = placement.pack(PAR, *args, seconds=2)
        group = plan['groups'][0]
        policies = set()
        for trial in packing_search.placement_trials(PAR, frame, comps, nets, index,
                group, options, deadline=perf_counter()+3):
            spacing = trial.passive_module_guides[group['id']]['spacing']
            policies.add((spacing['policy'], spacing.get('extra_keepout', 0)))
            if len(policies) == 3:
                break
        self.assertEqual(policies, {('compact', 0), ('standard', 0), ('standard', options.center_spacing)})
        self.assertEqual(frame.passive_module_guides[group['id']]['spacing']['policy'], 'compact')

    def test_fixed_guest_is_excluded_from_failure_search(self):
        args = case()
        previous = rejected_case(args)
        frame = previous.checkpoint['frame']
        frame.fixed.add(args[3][args[4]['groups'][0]['members'][0]])
        with patch.object(packing_search, 'placement_trials', side_effect=AssertionError('fixed moved')):
            result, report = packing_search.resume_failed(PAR, *args, previous)
        self.assertIsNone(result)

    def test_all_rejected_geometry_obeys_global_count_and_transaction_limits(self):
        args = case()
        previous = rejected_case(args)
        with patch('netlist2schematic.plugins.preflight.placement_preflight._static_faults',
                   return_value=dict(count=1,hard=set(),violations=[dict(kind='body_overlap',components=[0,1])])):
            result, report = packing_search.resume_failed(PAR, *args, previous, max_trials=5)
        self.assertIsNone(result)
        self.assertLessEqual(report['trials'], 5)
        self.assertEqual(report['stop_reason'], 'local_budget')
        self.assertLessEqual(len(report['insertion_search']['transactions']), 4)

    def test_valid_fast_path_does_not_use_failure_search(self):
        from netlist2schematic.layout.structured_placement import create_candidates
        from tests.test_passive_skeleton import PassiveSkeletonFlowTests
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            source = PassiveSkeletonFlowTests().netlist(directory)
            with patch.object(packing_search, 'resume_failed', side_effect=AssertionError('fast path searched')):
                result = next(create_candidates(source, iterations=0, passive_skeleton='auto', candidate_ids=(2,)))
        self.assertIsNotNone(result[0])


if __name__ == '__main__':
    unittest.main()
