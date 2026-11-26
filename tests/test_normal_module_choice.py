"""Normal-stage nearby choices preserve a complete, routable active frame."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_terminal_slots import fixture, incumbent, OPTIONS
from tests.test_passive_skeleton import banks
from tests.test_passive_placement import mos, resistor
from tests.test_series_alignment import PAR
from netlist2schematic.plugins.passive_skeleton import normal_choice as choice
from netlist2schematic.plugins.passive_skeleton.placement import pack
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.sa_routing import final_route


def run(args, **kw):
    frame, comps, nets, index, plan = args
    return choice.refine_modules(PAR, frame, comps, nets, index, plan, OPTIONS,
                                 seconds=kw.pop('seconds', 2), **kw)


def terminal(topology='single', kind='V'):
    devices, cells, _ = fixture(kind=kind, topology=topology)
    return incumbent(devices, cells)


class NormalModuleChoiceTests(unittest.TestCase):
    def assert_frame(self, args, after):
        before, comps, nets, index, plan = args
        self.assertEqual(before.cell_of, after.cell_of)
        self.assertEqual(before.orient, after.orient)
        self.assertEqual(before.column_x, after.column_x)
        self.assertEqual(before.row_y, after.row_y)
        for i in range(len(comps)):
            if i not in before.layout_guest_ids:
                self.assertEqual(before.pos_of(i), after.pos_of(i))
        for group in plan['groups']:
            shifts = {tuple(after.pos_of(index[n])[k]-before.pos_of(index[n])[k] for k in (0, 1))
                      for n in group['members']}
            self.assertEqual(len(shifts), 1)

    def test_normal_success_can_improve_before_any_routing_failure(self):
        args = terminal()
        with patch('netlist2schematic.routing.orthogonal_router._find_path',
                   side_effect=AssertionError('No A* in normal choices')):
            after, report = run(args)
        self.assertTrue(report['applied'], report)
        self.assertLess(report['modules'][0]['after']['estimated_cost'],
                        report['modules'][0]['before']['estimated_cost'])
        self.assert_frame(args, after)
        self.assertTrue(final_route(PAR, args[2], after, args[3])[2]['valid'])

    def test_whole_series_parallel_and_two_terminal_families_remain_routable(self):
        fixtures = [terminal(kind=k) for k in ('R', 'C', 'L', 'D', 'V', 'I')]
        fixtures += [terminal(topology=t) for t in ('series', 'parallel')]
        for args in fixtures:
            with self.subTest(members=args[4]['groups'][0]['members']):
                before = deepcopy(args[0].passive_local_offsets)
                after, report = run(args)
                self.assertEqual(args[0].passive_local_offsets, before)
                self.assert_frame(args, after)
                self.assertLessEqual(report['trials'], 64)
                self.assertTrue(final_route(PAR, args[2], after, args[3])[2]['valid'])

    def test_bridge_stays_between_its_hosts_and_preserves_the_active_axes(self):
        devices = dict(MUP=mos('a', 'ga'), MDOWN=mos('b', 'gb'),
                       R1=resistor('a', 'join'), C1=resistor('join', 'b', 'C'))
        args = incumbent(devices, dict(MUP=(1, 1), MDOWN=(1, 4)))
        after, report = run(args)
        self.assertEqual(report['modules'][0]['mode'], 'bridge_corridor')
        self.assert_frame(args, after)
        self.assertTrue(final_route(PAR, args[2], after, args[3])[2]['valid'])

    def test_inline_column_paired_banks_and_fixed_guests_keep_their_roles(self):
        inline = dict(MUP=mos('top', 'ga', 'mid'), MDOWN=mos('bottom', 'gb'),
                      R1=resistor('mid', 'bottom'))
        args = incumbent(inline, dict(MUP=(0, 2), MDOWN=(2, 2)))
        after, report = run(args)
        self.assertIs(after, args[0])
        self.assertEqual(report['skipped'][0]['reason'], 'preserve_typed_branch')
        _, core, comps, nets, index, plan = banks()
        frame, _ = pack(PAR, core, comps, nets, index, plan, OPTIONS)
        after, report = run((frame, comps, nets, index, plan))
        self.assertIs(after, frame)
        self.assertTrue(all(r['reason']=='coupled_family' for r in report['skipped']))
        args = terminal()
        args[0].fixed = {args[3]['VREF']}
        after, report = run(args)
        self.assertIs(after, args[0])
        self.assertEqual(report['skipped'][0]['reason'], 'fixed_guest')

    def test_legal_position_quota_and_total_trials_are_not_reset_per_path(self):
        args = terminal()
        _, report = run(args)
        self.assertEqual(report['modules'][0]['legal_alternatives'], 4)
        self.assertEqual(report['modules'][0]['stop_reason'], 'legal_alternative_limit')
        for quota in (0, 1, 3):
            after, report = run(args, max_trials=quota)
            self.assertLessEqual(report['trials'], quota)
            self.assert_frame(args, after)

    def test_budget_exhaustion_and_invalid_proposals_retain_the_safe_incumbent(self):
        args = terminal()
        for kw in (dict(seconds=0), dict(deadline=perf_counter()-1), dict(max_trials=0)):
            after, report = run(args, **kw)
            self.assertIs(after, args[0])
            self.assertFalse(report['applied'])
        faults = dict(count=1, hard={'body_overlap'}, violations=[])
        with patch('netlist2schematic.plugins.preflight.placement_preflight._static_faults', return_value=faults):
            after, report = run(args)
        self.assertIs(after, args[0])
        self.assertFalse(report['applied'])

    def test_timeout_discards_unfinished_score_but_keeps_completed_best_choice(self):
        args = terminal()
        failure = RoutingFailure('test timeout', dict(reason='quality_time_budget'))
        actual = choice._score
        calls = 0
        def score(*a, **kw):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise failure
            return actual(*a, **kw)
        with patch.object(choice, '_score', side_effect=score):
            after, report = run(args)
        self.assertIs(after, args[0])
        self.assertEqual(report['stop_reason'], 'time_budget')
        best, _ = run(args)
        def proposals(*a, **kw):
            yield best
            raise failure
        with patch.object(choice, '_proposals', side_effect=proposals):
            after, report = run(args)
        self.assertIs(after, best)
        self.assertTrue(report['applied'])
        self.assertEqual(report['stop_reason'], 'time_budget')

    def test_lower_obstruction_score_cannot_buy_a_longer_estimated_connection(self):
        args = terminal()
        before_key, before = choice._score(PAR, *args[:1], *args[1:4], args[4]['groups'][0],
            [], OPTIONS, perf_counter()+1)
        worse = dict(before, estimated_cost=before['estimated_cost']+1)
        self.assertFalse(choice._improves((1,)+before_key[1:], before, (0,)+before_key[1:], worse))

    def test_names_order_and_translation_do_not_supply_layout_rules(self):
        devices, cells, _ = fixture()
        args = incumbent(devices, cells)
        first, _ = run(args)
        names = {n:n+'changed' for n in devices}
        fields = {'d', 'g', 's', 'bulk', 'p', 'n', 'a', 'b'}
        net = lambda n:n if n in ('0', 'vdd') else 'changed_'+n
        changed = {names[n]:{k:net(v) if k in fields else v for k,v in d.items()}
                   for n,d in reversed(list(devices.items()))}
        other = incumbent(changed, {names[n]:c for n,c in cells.items()})
        second, _ = run(other)
        for n in devices:
            self.assertEqual(first.pos_of(args[3][n]), second.pos_of(other[3][names[n]]))
        shifted = deepcopy(args[0])
        shifted.column_x = [x+200 for x in shifted.column_x]
        shifted.row_y = [y+400 for y in shifted.row_y]
        for guide in shifted.passive_module_guides.values():
            for field in ('routes', 'internal_routes'):
                guide[field] = {net:[((a[0]+200,a[1]+400),(b[0]+200,b[1]+400)) for a,b in ss]
                                for net,ss in guide[field].items()}
            guide['ports'] = [(p[0]+200,p[1]+400) for p in guide['ports']]
        third, _ = run((shifted, *args[1:]))
        for i in range(len(args[1])):
            self.assertEqual(third.pos_of(i), tuple(first.pos_of(i)[k]+(200,400)[k] for k in (0,1)))

    def test_invalid_budget_is_explicit(self):
        for kw in (dict(seconds=-1), dict(seconds=float('nan')), dict(max_trials=1.5)):
            with self.assertRaises(ValueError):
                run(terminal(), **kw)

    def test_both_default_skeleton_candidates_apply_choices_and_off_path_skips_them(self):
        from netlist2schematic.layout.structured_placement import create_candidates
        from tests.test_corresponding_branch_rows import CorrespondingBranchIntegrationTests
        with TemporaryDirectory() as folder:
            path = CorrespondingBranchIntegrationTests().source(folder)
            results = list(create_candidates(path, iterations=0, passive_skeleton='auto', candidate_ids=(2,3)))
            self.assertEqual({r[4]['candidate_id'] for r in results}, {2,3})
            for pl, cs, ns, ix, data in results:
                self.assertEqual(data['passive_skeleton']['normal_choice']['version'], choice.VERSION)
                saved = data['passive_skeleton']['module_connections']
                self.assertEqual(saved, pl.passive_module_guides)
                for action in data['passive_skeleton']['actions']:
                    for name, entry in action['positions'].items():
                        self.assertEqual(entry['point'], list(pl.pos_of(ix[name])))
            with patch.object(choice, 'refine_modules', side_effect=AssertionError('off must skip normal choices')):
                list(create_candidates(path, iterations=0, passive_skeleton='off', candidate_ids=(2,3)))

    def test_saved_loading_preserves_coordinates_without_normal_replacement(self):
        from hashlib import sha256
        import json
        from netlist2schematic.common.sa_saved import load_placement
        from netlist2schematic.flow.run_flow import _saved_positions
        args = terminal()
        pl, _ = run(args)
        with TemporaryDirectory() as folder:
            source = Path(folder)/'case.cir'
            source.write_text('.model NM NMOS\nMHOST drain input tail 0 NM\n'
                'MLEFT left other tail 0 NM\nMTAIL tail bias 0 0 NM\nVREF input 0 DC 1\n', encoding='utf8')
            path = Path(folder)/'case-placement.json'
            path.write_text(json.dumps(dict(source_netlist=str(source),
                source_sha256=sha256(source.read_bytes()).hexdigest(),
                placement=_saved_positions(pl,args[1]),
                layout_grid=dict(column_x=pl.column_x,row_y=pl.row_y))),encoding='utf8')
            with patch.object(choice, 'refine_modules', side_effect=AssertionError('No placement search on restore')):
                data, _, _, restored, cs, ns, ix = load_placement(path)
            for name, i in args[3].items():
                self.assertEqual(pl.pos_of(i),restored.pos_of(ix[name]))


if __name__ == '__main__':
    unittest.main()
