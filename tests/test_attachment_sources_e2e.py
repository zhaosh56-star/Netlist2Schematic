"""Source/all integration keeps physical state and motion ownership coherent."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import attachment_integration as integration
from attachment_sources import measure_source_groups, source_group_trials
from circuit_structures import analyze_structures
from device_leveling import compute_levels
from netlist_core import parse_netlist, to_document
import structured_placement as structured
from tests.test_passive_placement import convert, mos, resistor, source


def plan_for(devices, pl, comps, nets, index, policy='all'):
    return integration.build_attachment_plan(devices, comps, nets, index,
        analyze_structures(devices), compute_levels(devices), pl.row_of, policy=policy)


def state(pl):
    return deepcopy((pl.cell_of, pl.orient, pl.row_of, pl.column_x, pl.row_y,
                     pl.comp_at, pl.blocks, pl.fixed_cells, pl.fixed, pl.rng.getstate()))


def tail_fixture(reverse=False):
    devices = dict(a=mos('oa', 'ga', 'tail'), b=mos('ob', 'gb', 'tail'),
        i=source('ISRC', '0' if reverse else 'tail', 'tail' if reverse else '0'))
    pl, comps, nets, index, _ = convert(devices, dict(a=(1, 2), b=(1, 4), i=(4, 0)))
    return devices, pl, comps, nets, index


def production_seed(path, cell):
    devices = parse_netlist(path, cell=cell)
    structs = structured._structures(devices)
    document = to_document(devices)
    comps, nets = document['components'], document['nets']
    index = {comp['id']: i for i, comp in enumerate(comps)}
    levels = structured.dl.compute_levels(devices)
    conduction = {net for dev in devices.values() if structured._active(dev)
                  for net in (dev.get('d'), dev.get('s'), dev.get('c'), dev.get('e')) if net is not None}
    attached = structured.legacy.attach_info(devices, levels['net_levels'], conduction)
    rows, _ = structured.legacy._allocate_rows(devices, comps, index,
        levels['net_levels'], levels['device_levels'], attached)
    plan = integration.build_attachment_plan(devices, comps, nets, index, structs, levels, rows, policy='all')
    row_plan = plan.get('branch_row_plan')
    pl, _, _ = structured._initial_placement(devices, comps, nets, index,
        levels, attached, structs, 20261029+2*104729, 1, passive_layout='flexible',
        row_override=[row_plan['rows'][comp['id']] for comp in comps] if row_plan else None,
        bridge_alignment=row_plan.get('bridge_alignment', ()) if row_plan else ())
    if row_plan:
        pl.branch_connections = deepcopy(row_plan.get('bridge_alignment', ()))
    return devices, pl, comps, nets, index, plan


class AttachmentSourceIntegrationTests(unittest.TestCase):
    def test_helper_core_source_error_records_are_identical_and_not_double_counted(self):
        devices = dict(v=source('VSRC', 'drive'), r=resistor('drive', 'gate'), a=mos('out', 'gate'))
        original, comps, nets, index, _ = convert(devices, dict(v=(2, 1), r=(2, 2), a=(2, 4)))
        plan = plan_for(devices, original, comps, nets, index)
        pl = integration._install(original, comps, index, plan)
        orientations = list(pl.orient)
        orientations[index['v']], orientations[index['r']] = 1, 3
        pl.set_state(pl.cell_of, orientations)
        for bent in (False, True):
            cells = list(pl.cell_of)
            cells[index['r']] = (3 if bent else 2, cells[index['r']][1])
            pl.set_state(cells, pl.orient)
            helper = measure_source_groups(pl, comps, nets, index, plan['source_model'])
            core = integration.measure_attachments(pl, comps, index, plan)
            self.assertTrue(core['measurable'])
            self.assertEqual(core['source_errors'], helper['source_errors'])
            self.assertEqual(core['sources'], helper['records'])
            self.assertEqual(core['source_errors'], sum(r['raw_source_errors']+r['chain_errors']
                                                       for r in core['sources']))

    def test_no_sources_all_and_passive_keep_exact_geometry_and_sa_random_stream(self):
        devices = dict(a=mos('left', 'ga'), b=mos('right', 'gb'),
            r=resistor('left', 'middle'), c=resistor('middle', 'right', 'C'))
        pl, comps, nets, index, _ = convert(devices,
            dict(a=(1, 1), b=(1, 6), r=(0, 0), c=(0, 7)), blocks=[['a', 'b']])
        outputs = []
        with patch('orthogonal_router.route_scene', side_effect=AssertionError('initializer/SA called A*')):
            for policy in ('passive', 'all'):
                plan = plan_for(devices, pl, comps, nets, index, policy)
                self.assertEqual(len(plan.get('source_groups', ())), 0)
                result, report = integration.initialize_attachments(pl, devices, comps, nets, index, plan)
                energy, _, annealing = structured._anneal(result, nets, index, [], [], 120, None)
                outputs.append((state(result), energy, annealing, report['candidate_trials']))
        self.assertEqual(outputs[0], outputs[1])

    def test_source_only_tail_improves_with_true_poles_and_shared_refinement_ledger(self):
        for reverse in (False, True):
            devices, original, comps, nets, index = tail_fixture(reverse)
            plan = plan_for(devices, original, comps, nets, index)
            self.assertFalse(plan['groups'])
            self.assertEqual(plan['source_groups'][0]['kind'], 'direct_tail')
            frozen, nodes = state(original), deepcopy([c['nodes'] for c in comps])
            before = integration.measure_attachments(original, comps, index, plan)
            budget = integration.new_attachment_budget()
            with patch('orthogonal_router.route_scene', side_effect=AssertionError('source initializer called A*')):
                result, report = integration.initialize_attachments(original, devices, comps, nets,
                    index, plan, budget=budget)
            self.assertTrue(report['source_applied'])
            self.assertTrue(result.valid_state())
            measured = integration.measure_attachments(result, comps, index, plan)
            self.assertEqual(measured['source_errors'], 0)
            self.assertLess(measured['source_distance'], before['source_distance'])
            group = plan['source_groups'][0]
            self.assertEqual(result.pin_dir(index['i'], group['signal_pin']), 'north')
            self.assertEqual(result.pin_dir(index['i'], group['rail_pin']), 'south')
            self.assertEqual([c['nodes'] for c in comps], nodes)
            self.assertEqual(state(original), frozen)
            for name in ('a', 'b'):
                i = index[name]
                self.assertEqual((result.cell_of[i], result.orient[i], result.pos_of(i)),
                                 (original.cell_of[i], original.orient[i], original.pos_of(i)))
            self.assertLessEqual(budget['candidates'], 128)
            # A consumed initializer ledger cannot open a new refinement budget.
            budget['candidates'] = budget['max_candidates']
            metadata = {'layout_attachments': {'plan': plan, 'budget': budget}}
            refined, saved = integration.refine_attachments(structured.par, result, comps, nets,
                index, metadata, budget=budget)
            self.assertEqual(state(refined), state(result))
            self.assertEqual(saved['layout_attachments']['refinement']['candidates'], 0)

    def test_source_only_explicit_fixed_world_and_orientation_do_not_move(self):
        devices, original, comps, nets, index = tail_fixture(True)
        i = index['i']; original.fixed.add(i); original.fixed_cells[i] = original.cell_of[i]
        plan = plan_for(devices, original, comps, nets, index)
        frozen = state(original)
        result, report = integration.initialize_attachments(original, devices, comps, nets, index, plan)
        self.assertFalse(report['source_applied'])
        self.assertEqual(state(result), frozen)
        self.assertEqual((result.pos_of(i), result.orient[i]), (original.pos_of(i), original.orient[i]))

    def test_source_proposal_and_undo_never_rebind_freedom_or_axes(self):
        devices, original, comps, nets, index = tail_fixture()
        plan = plan_for(devices, original, comps, nets, index)
        pl = integration._install(original, comps, index, plan)
        frozen = state(pl)
        freedom = deepcopy((pl.fixed_rows, pl.attachment_source_rows, pl.attachment_source_ids,
                            pl.attachment_axes, pl.source_orientation_ids, pl.attachment_orientation_ids))
        moved = False
        for _ in range(160):
            before = (list(pl.cell_of), list(pl.orient))
            old = pl.propose()
            self.assertEqual((pl.fixed_rows, pl.attachment_source_rows, pl.attachment_source_ids,
                              pl.attachment_axes, pl.source_orientation_ids, pl.attachment_orientation_ids), freedom)
            if old is not None:
                self.assertTrue(pl.valid_state())
                moved |= pl.cell_of[index['i']] != before[0][index['i']]
                pl.set_state(*old)
                self.assertEqual((pl.cell_of, pl.orient), before)
        self.assertTrue(moved)
        # RNG and proposal counters advance; geometry and the fixed world do not.
        self.assertEqual(state(pl)[:-1], frozen[:-1])

    @unittest.skipUnless((ROOT/'test_data/11-15/lna-with-degeneration.cir').is_file(),
                         'optional data14 source fixture is unavailable')
    def test_data14_source_chain_shadows_passive_motion_without_ownership_union(self):
        devices, original, comps, nets, index, plan = production_seed(
            ROOT/'test_data/11-15/lna-with-degeneration.cir', 'main')
        source_group, = plan['source_groups']
        self.assertEqual(source_group['kind'], 'source_control_chain')
        self.assertEqual(source_group['members'], ['V1', 'R2'])
        self.assertTrue(any('R2' in group['members'] for group in plan['groups']))
        motion = integration._motion_groups(plan)
        self.assertEqual(sum('R2' in group['members'] for group in motion), 1)
        self.assertFalse(any('R2' in group['members'] for group in integration._passive_motion_groups(plan)))
        pl = integration._install(original, comps, index, plan)
        self.assertEqual(pl.blocks, original.blocks)
        self.assertNotIn(index['R2'], pl._passive_set)
        self.assertNotIn(index['V1'], pl.fixed_rows)
        self.assertNotIn(index['R2'], pl.fixed_rows)
        one = dict(plan['source_model'], groups=[source_group])
        frozen = state(pl)
        trials = list(source_group_trials(pl, comps, nets, index, one, max_candidates=24))
        self.assertTrue(trials)
        source_members = set(source_group['members'])
        for trial, _ in trials:
            for i, comp in enumerate(comps):
                if comp['id'] not in source_members:
                    self.assertEqual((trial.cell_of[i], trial.orient[i], trial.pos_of(i)),
                                     (pl.cell_of[i], pl.orient[i], pl.pos_of(i)))
        self.assertEqual(state(pl), frozen)
        result, report = integration.initialize_attachments(original, devices, comps, nets, index, plan)
        if report['applied']:
            self.assertTrue(result.valid_state())
            self.assertFalse(any('R2' in group['members'] and group['id'] in result.attachment_required_embeddings
                                 for group in plan['groups']))

    @unittest.skipUnless((ROOT/'test_data/11-15/self-adaptive-current-mirror-buffer-fvf-ldo.cir').is_file(),
                         'optional data15 source fixture is unavailable')
    def test_data15_shared_vg_source_guests_never_pull_vout_loads(self):
        _, original, comps, nets, index, plan = production_seed(
            ROOT/'test_data/11-15/self-adaptive-current-mirror-buffer-fvf-ldo.cir',
            'self_adaptive_current_mirror_buffer_fvf_ldo')
        group, = plan['source_groups']
        self.assertEqual((group['device'], group['kind'], group['attachment_net']),
                         ('I1', 'neutral_junction_source', 'vg'))
        self.assertEqual(group['members'], ['I1'])
        self.assertFalse({'CL', 'RL'}.intersection(group['host_members']))
        pl = integration._install(original, comps, index, plan)
        self.assertEqual(pl.blocks, original.blocks)
        model = dict(plan['source_model'], groups=[group])
        trials = list(source_group_trials(pl, comps, nets, index, model, max_candidates=24))
        self.assertTrue(trials)
        for trial, _ in trials:
            for i, comp in enumerate(comps):
                if comp['id'] != 'I1':
                    self.assertEqual((trial.cell_of[i], trial.orient[i], trial.pos_of(i)),
                                     (pl.cell_of[i], pl.orient[i], pl.pos_of(i)))


if __name__ == '__main__':
    unittest.main()
