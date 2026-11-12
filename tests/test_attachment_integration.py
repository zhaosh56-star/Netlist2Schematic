"""Actual junction slots, reversible active/guest moves and shared limits."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import attachment_integration as integration
import structured_placement as structured
from circuit_structures import analyze_structures
from device_leveling import compute_levels
from netlist_core import parse_netlist, to_document
from tests.test_passive_placement import convert, mos, resistor


def bridge_fixture(*, fixed=False, adjacent=False):
    devices = dict(a=mos('left', 'ga'), b=mos('right', 'gb'),
        r=resistor('left', 'middle'), c=resistor('middle', 'right', 'C'),
        outsider=resistor('unrelated', '0'))
    cells = dict(a=(1, 1), b=(1, 2 if adjacent else 6),
                 r=(0, 0), c=(0, 7), outsider=(2, 8))
    pl, comps, nets, index, structs = convert(devices, cells, blocks=[['a', 'b']])
    if fixed:
        pl.fixed = {index['a'], index['b']}
        pl.fixed_cells = {i: pl.cell_of[i] for i in pl.fixed}
    plan = integration.build_attachment_plan(devices, comps, nets, index, structs,
                                             compute_levels(devices), pl.row_of)
    return devices, pl, comps, nets, index, plan


def real_seed(filename, cell='dut', variant=1):
    path = ROOT/'test_data'/'11-15'/filename
    if not path.is_file():
        raise unittest.SkipTest('local real-netlist fixture is unavailable: '+str(path))
    devices = parse_netlist(path, cell=cell)
    document = to_document(devices)
    comps, nets = document['components'], document['nets']
    index = {c['id']: i for i, c in enumerate(comps)}
    levels, structs = compute_levels(devices), analyze_structures(devices)
    conduction = {net for device in devices.values() if structured._active(device)
                  for net in (device.get('d'), device.get('s'), device.get('c'), device.get('e'))
                  if net is not None}
    att = structured.legacy.attach_info(devices, levels['net_levels'], conduction)
    rows, _ = structured.legacy._allocate_rows(devices, comps, index,
        levels['net_levels'], levels['device_levels'], att)
    plan = integration.build_attachment_plan(devices, comps, nets, index, structs, levels, rows)
    row_plan = plan.get('branch_row_plan')
    pl, _, _ = structured._initial_placement(devices, comps, nets, index, levels,
        att, structs, 20261029+(variant+1)*104729, variant, passive_layout='flexible',
        row_override=[row_plan['rows'][c['id']] for c in comps] if row_plan else None,
        bridge_alignment=row_plan.get('bridge_alignment', ()) if row_plan else ())
    return devices, pl, comps, nets, index, plan


class AttachmentIntegrationTests(unittest.TestCase):
    def test_existing_hole_accepts_only_the_proven_bridge_guest(self):
        d, original, comps, nets, index, plan = bridge_fixture()
        frozen_plan = json.dumps(plan, sort_keys=True)
        rng = original.rng.getstate()
        result, report = integration.initialize_attachments(original, d, comps, nets, index, plan)
        self.assertTrue(report['applied'])
        self.assertTrue(result.valid_state())
        self.assertEqual(result.column_x, original.column_x)
        self.assertEqual(result.row_y, original.row_y)
        group = next(g for g in plan['groups'] if g['requires_interior'])
        self.assertEqual(set(integration._embedded(result, group, index)), {'r', 'c'})
        self.assertEqual(json.dumps(plan, sort_keys=True), frozen_plan)
        self.assertEqual(original.rng.getstate(), rng)
        cells = list(result.cell_of)
        lo = min(cells[index[n]][1] for n in ('a', 'b'))
        hi = max(cells[index[n]][1] for n in ('a', 'b'))
        vacant = next(col for col in range(lo+1, hi) if (1, col) not in result.comp_at)
        cells[index['outsider']] = (1, vacant)
        result.set_state(cells, result.orient)
        self.assertFalse(result.blocks_contiguous())

    def test_guest_qualification_does_not_extend_to_another_row_of_a_parent(self):
        d, original, comps, nets, index, plan = bridge_fixture()
        result, report = integration.initialize_attachments(original, d, comps, nets, index, plan)
        self.assertTrue(report['applied'])
        # A parent containing the real host does not grant its full rectangle.
        result.layout_macro_blocks = [[index['a'], index['b'], index['outsider']]]
        result = integration._install(result, comps, index, plan,
                                      required=result.attachment_required_embeddings)
        cells = list(result.cell_of)
        cells[index['r']] = (2, cells[index['a']][1]+1)
        result.set_state(cells, result.orient)
        self.assertFalse(result.valid_state())

    def test_fixed_world_positions_are_preserved_and_a_blocked_opening_rolls_back(self):
        for adjacent in (False, True):
            d, original, comps, nets, index, plan = bridge_fixture(fixed=True, adjacent=adjacent)
            frozen = deepcopy((original.cell_of, original.orient, original.column_x,
                               original.row_y, original.rng.getstate()))
            points = {i: original.pos_of(i) for i in original.fixed}
            result, report = integration.initialize_attachments(original, d, comps, nets, index, plan)
            self.assertEqual({i: result.pos_of(i) for i in original.fixed}, points)
            if adjacent:
                self.assertIs(result, original)
                self.assertFalse(report['applied'])
            else:
                self.assertTrue(report['applied'])
            self.assertEqual((original.cell_of, original.orient, original.column_x,
                              original.row_y, original.rng.getstate()), frozen)

    def test_sa_undo_restores_current_rows_and_does_not_mutate_slot_policy_or_axes(self):
        d, original, comps, nets, index, plan = bridge_fixture()
        result, report = integration.initialize_attachments(original, d, comps, nets, index, plan)
        self.assertTrue(report['applied'])
        policy = deepcopy((result.attachment_axes, result.fixed_rows,
            result.attachment_required_embeddings, result.attachment_shape_limits,
            result.attachment_axis_limits, result.attachment_plan))
        legal = 0
        for _ in range(100):
            saved = list(result.cell_of), list(result.orient)
            undo = result.propose()
            if undo is not None:
                legal += 1
                self.assertTrue(result.valid_state())
                result.set_state(*undo)
            self.assertEqual((result.cell_of, result.orient), saved)
            self.assertEqual(result.row_of, [r for r, _ in result.cell_of])
            self.assertEqual(result.comp_at, {c: i for i, c in enumerate(result.cell_of)})
        self.assertGreater(legal, 0)
        self.assertEqual((result.attachment_axes, result.fixed_rows,
            result.attachment_required_embeddings, result.attachment_shape_limits,
            result.attachment_axis_limits, result.attachment_plan), policy)

    def test_initialization_and_finish_share_one_candidate_allowance(self):
        d, original, comps, nets, index, plan = bridge_fixture()
        budget = integration.new_attachment_budget(max_candidates=1)
        result, report = integration.initialize_attachments(original, d, comps, nets, index, plan,
                                                           budget=budget)
        before = deepcopy(budget)
        metadata = dict(layout_attachments=dict(plan=plan, budget=budget))
        _, metadata = integration.refine_attachments(structured.par, result, comps, nets,
            index, metadata, budget=budget)
        final = metadata['layout_attachments']['refinement']
        self.assertEqual(final['candidates'], 0)
        self.assertEqual(budget['candidates'], before['candidates'])
        self.assertLessEqual(budget['candidates'], 1)
        self.assertGreaterEqual(budget['spent_seconds'], before['spent_seconds'])

    def test_single_rail_shunt_never_opens_columns_or_wins_embedding_count(self):
        d = dict(a=mos('out', 'in'), c=resistor('out', '0', 'C'))
        original, comps, nets, index, st = convert(d, dict(a=(1, 3), c=(0, 0)))
        plan = integration.build_attachment_plan(d, comps, nets, index, st, compute_levels(d), original.row_of)
        result, report = integration.initialize_attachments(original, d, comps, nets, index, plan)
        self.assertEqual(result.column_x, original.column_x)
        self.assertEqual(result.row_y, original.row_y)
        self.assertFalse(any(a['kind'] == 'joint_junction_slot' for a in report['actions']))
        self.assertEqual(integration.measure_attachments(result, comps, index, plan)['embedded_members'], 0)
        self.assertTrue(plan['supported'])

    def test_no_attachment_evidence_keeps_the_original_object_and_random_state(self):
        d = dict(a=mos('out', 'in'))
        original, comps, nets, index, st = convert(d, dict(a=(1, 3)))
        plan = integration.build_attachment_plan(d, comps, nets, index, st, compute_levels(d), original.row_of)
        rng = original.rng.getstate()
        result, report = integration.initialize_attachments(original, d, comps, nets, index, plan)
        self.assertIs(result, original)
        self.assertEqual(result.rng.getstate(), rng)
        self.assertEqual(report['candidate_trials'], 0)

    def test_post_sa_floor_or_direction_violation_restores_the_whole_checkpoint(self):
        d, original, comps, nets, index, plan = bridge_fixture()
        checkpoint, report = integration.initialize_attachments(original, d, comps, nets, index, plan)
        self.assertTrue(report['applied'])
        changed = deepcopy(checkpoint)
        changed.row_y[-1] += 10
        result, report = integration.finalize_attachment_state(changed, checkpoint)
        self.assertTrue(report['reverted'])
        self.assertEqual(result.cell_of, checkpoint.cell_of)
        self.assertEqual(result.row_y, checkpoint.row_y)
        self.assertTrue(result.valid_state())

    def test_off_and_first_reference_do_not_call_the_new_plan(self):
        path = ROOT/'examples'/'1.cir'
        original = next(structured.create_candidates(path, seed=59, iterations=0))
        with patch.object(integration, 'build_attachment_plan', side_effect=AssertionError('unexpected plan')):
            explicit_off = next(structured.create_candidates(path, seed=59, iterations=0, attachment_layout='off'))
            reference = next(structured.create_candidates(path, seed=59, iterations=0, attachment_layout='passive'))
        for candidate in (explicit_off, reference):
            self.assertEqual(candidate[0].cell_of, original[0].cell_of)
            self.assertEqual(candidate[0].orient, original[0].orient)
            self.assertEqual(candidate[0].rng.getstate(), original[0].rng.getstate())
            self.assertEqual(candidate[4], original[4])

    def test_real_data10_and_12_have_statically_legal_actual_interior_paths_without_astar(self):
        cases = [
            ('analoggenie-182-telescopic-differential-stage-with-resistive-cm-sensing-and-rc-source-degeneration.cir', {'C1','R1'}),
            ('cross-coupled.cir', {'C0','Rm','Cm','Lm'}),
        ]
        for filename, expected in cases:
            with self.subTest(filename=filename):
                d, original, comps, nets, index, plan = real_seed(filename)
                with patch('orthogonal_router._find_path', side_effect=AssertionError('A* in initializer')):
                    result, report = integration.initialize_attachments(original, d, comps, nets, index, plan)
                self.assertTrue(report['applied'])
                self.assertGreater(report['candidate_trials'], 0)
                self.assertGreater(report['legal_trials'], 0)
                self.assertTrue(result.valid_state())
                measured = integration.measure_attachments(result, comps, index, plan)
                actual = set().union(*(set(g['embedded_members']) for g in measured['groups']))
                self.assertTrue(expected <= actual)
                self.assertEqual(measured['active_order_errors'], 0)
                limits = result.attachment_shape_limits
                self.assertTrue(all(v['orientation_errors'] == 0 and v['order_errors'] == 0 for v in limits.values()))

    def test_real_input_chain_and_output_bank_receive_fixed_axis_local_trials(self):
        cases = [
            ('lna-with-degeneration.cir', 'Main', 'readability:series_chain:C2|L4'),
            ('self-adaptive-current-mirror-buffer-fvf-ldo.cir',
             'self_adaptive_current_mirror_buffer_fvf_ldo', 'readability:parallel_bank:CL|RL'),
        ]
        for filename, cell, group in cases:
            with self.subTest(filename=filename):
                d, original, comps, nets, index, plan = real_seed(filename, cell)
                result, report = integration.initialize_attachments(original, d, comps, nets, index, plan)
                self.assertGreater(report['group_trials'].get(group, 0), 0)
                self.assertGreater(report['local_candidate_trials'], 0)
                self.assertLessEqual(report['budget']['candidates'], 128)
                if report['applied']:
                    self.assertTrue(result.valid_state())

    def test_production_second_and_third_seed_both_embed_the_complete_rlc_bridge(self):
        for variant in (1, 2):
            with self.subTest(candidate=variant+1):
                d, original, comps, nets, index, plan = real_seed('cross-coupled.cir', variant=variant)
                result, report = integration.initialize_attachments(original, d, comps, nets, index, plan)
                primary = next(g for g in plan['groups'] if g['kind'] == 'multi_path_bridge')
                actions = [a for a in report['actions'] if a.get('group') == primary['id']]
                self.assertTrue(actions, 'the real candidate must apply its complete RLC bridge')
                self.assertEqual(set(integration._embedded(result, primary, index)), {'C0','Rm','Cm','Lm'})
                self.assertGreater(report['rail_direction_rejections'], 0)
                self.assertTrue(actions[0]['slot_diagnostics']['examples'])
                for name in ('C1', 'C2'):
                    self.assertEqual(integration._rail_marker_direction_conflicts(result, index[name]), [])
                self.assertTrue(result.valid_state())


if __name__ == '__main__':
    unittest.main()
