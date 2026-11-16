"""Unique port paths may move together without rewriting functional owners."""
from copy import deepcopy
import json
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_layout_evidence import mos
from tests.test_passive_placement import convert
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment import attachment_stack_columns as columns
from netlist2schematic.plugins.attachment.attachment_selection import measure_candidate, organization_rank, select_attachment_result
from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.routing import sa_routing


def fixture(*, dual=False, bjt=False, rename=False, aligned=False, three=False):
    d = {}; cells = {}; count = 3 if three else 2
    for side in range(count):
        for stage, (drain, gate, source, kind) in enumerate((
                ('a'+str(side), 'pbias', 'vdd', 'PMOS'),
                ('out'+str(side), 'pcontrol', 'a'+str(side), 'PMOS'),
                ('out'+str(side), 'ncontrol', 'b'+str(side), 'NMOS'),
                ('b'+str(side), 'nbias', '0', 'NMOS'))):
            name = 'part'+str(side)+str(stage)
            d[name] = mos(drain, gate, source, kind)
            cells[name] = (stage+1, side+1 if aligned else (side+stage)%count+1)
    # A proven reference retains its mirror role when its output lanes move.
    d['part00']['d'] = 'pbias'; d['part01']['s'] = 'pbias'
    names = {n: n for n in d}
    if dual:
        for c in d.values():
            c['type'] = 'PMOS' if c['type'] == 'NMOS' else 'NMOS'
            for k in ('d', 'g', 's', 'bulk'):
                if c[k] in ('0', 'vdd'): c[k] = {'0': 'vdd', 'vdd': '0'}[c[k]]
        cells = {n: (5-r, c) for n, (r, c) in cells.items()}
    if bjt:
        d = {n: dict(cls='BJT', type='NPN' if c['type'] == 'NMOS' else 'PNP',
            c=c['d'], b=c['g'], e=c['s']) for n, c in d.items()}
    if rename:
        names = {n: 'component'+str(93-i) for i, n in enumerate(d)}
        d = {names[n]: {k: 'wire_'+v if k in ('d', 'g', 's', 'c', 'b', 'e')
            and v not in ('0', 'vdd') else v for k, v in c.items()} for n, c in reversed(list(d.items()))}
        cells = {names[n]: p for n, p in cells.items()}
    structs = analyze_structures(d)
    pl, cs, nets, index, _ = convert(d, cells, records=structs)
    plan = ai.build_attachment_plan(d, cs, nets, index, structs, compute_levels(d), pl.row_of, policy='all')
    # Test display-coherent fixtures; the existing planner is independently
    # exercised in test_attachment_stacks, not bypassed in the real flow.
    pl.set_state([(plan['display_rows'][c['id']], pl.cell_of[i][1]) for i, c in enumerate(cs)], pl.orient)
    return d, ai._install(pl, cs, index, plan), cs, nets, index, plan, structs, names


def arrange(f, *, safe=True, initial=True, budget=None, until=None, cap=float('inf')):
    _, pl, cs, nets, index, plan, *_ = f
    report = dict(actions=[], candidate_trials=0, candidates=0, accepted=0)
    budget = ai.new_attachment_budget() if budget is None else budget
    args = (structured.par, pl, cs, nets, index, plan, RoutingOptions(), budget,
            perf_counter()+1 if until is None else until, report)
    if safe == 'real':
        result = columns.arrange_columns(*args, initialization=initial, hpwl_limit=cap)
    else:
        with patch.object(ai, '_checked_static', return_value=safe):
            result = columns.arrange_columns(*args, initialization=initial, hpwl_limit=cap)
    return result, report, budget


class StackColumnTests(unittest.TestCase):
    def test_repeated_prefixes_share_real_lanes_once_and_preserve_roles(self):
        f = fixture(); model = f[5]['stack_columns']
        self.assertEqual(len(model['banks']), 2); self.assertEqual(len(model['lanes']), 2)
        self.assertEqual(sorted(map(lambda g: len(g['members']), model['lanes'])), [4, 4])
        self.assertTrue(all(not b['ownership_claim'] for b in model['banks']))
        owners, roles = deepcopy(f[1].blocks), deepcopy(f[6])
        result, report, budget = arrange(f)
        self.assertTrue(report['stack_column_arrangement']['applied'])
        self.assertEqual(columns.measure_columns(result, f[2], f[4], model)['pin_axis_error'], 0)
        self.assertEqual(result.blocks, owners); self.assertEqual(f[6], roles)
        self.assertEqual(result.cell_of, [(f[1].cell_of[i][0], c) for i, (_, c) in enumerate(result.cell_of)])
        self.assertEqual(result.attachment_axes, f[1].attachment_axes)
        self.assertLessEqual(budget['candidates'], 16)

    def test_N_P_BJT_duals_keep_same_two_banks(self):
        for dual, bjt in ((True, False), (False, True), (True, True)):
            with self.subTest(dual=dual, bjt=bjt):
                f = fixture(dual=dual, bjt=bjt)
                self.assertEqual(len(f[5]['stack_columns']['banks']), 2)
                result, report, _ = arrange(f)
                self.assertEqual(columns.column_rank(columns.measure_columns(result, f[2], f[4], f[5]['stack_columns'])), (0, 0, 0., 0.))

    def test_rename_and_source_reorder_keep_topological_lanes(self):
        a, b = fixture(), fixture(rename=True)
        remap = b[7]
        self.assertEqual({frozenset(remap[n] for n in g['members']) for g in a[5]['stack_columns']['lanes']},
                         {frozenset(g['members']) for g in b[5]['stack_columns']['lanes']})
        result, _, _ = arrange(b)
        self.assertEqual(columns.measure_columns(result, b[2], b[4], b[5]['stack_columns'])['pin_axis_error'], 0)

    def test_three_branches_are_not_arbitrary_pairs(self):
        f = fixture(three=True)
        self.assertEqual(len(f[5]['stack_columns']['lanes']), 3)
        self.assertEqual([len(b['lane_ids']) for b in f[5]['stack_columns']['banks']], [3, 3])

    def test_fork_and_wrong_terminal_evidence_do_not_extend_lane(self):
        f = fixture(); p = deepcopy(f[5])
        a = p['host_axes'][0]; p['readability_plan']['junctions'][a['net']]['conduction_ports'].append(['unknown', 'D'])
        model = columns.compile_columns(f[2], p, [], f[6])
        self.assertFalse(any(a in g['edges'] for g in model['lanes']))
        p = deepcopy(f[5]); p['host_axes'][0]['pin_a'] = 'G'
        model = columns.compile_columns(f[2], p, [], f[6])
        self.assertFalse(any(e['pin_a'] == 'G' for g in model['lanes'] for e in g['edges']))

    def test_mixed_protected_owner_skips_instead_of_splitting(self):
        f = fixture(); model = columns.compile_columns(f[2], f[5], [['part00', 'part12']], [])
        self.assertEqual(model['banks'], [])
        self.assertIn('protected_owner_crosses_lane_boundary', {r['reason'] for r in model['skipped']})

    def test_cycles_and_non_monotone_rows_are_not_forced_into_columns(self):
        f = fixture(); p = deepcopy(f[5]); p['display_rows']['part02'] = p['display_rows']['part00']
        model = columns.compile_columns(f[2], p, [], [])
        self.assertTrue(model['skipped']); self.assertEqual(model['banks'], [])

    def test_supply_or_one_control_only_cannot_activate(self):
        f = fixture(); p = deepcopy(f[5]); p['repeated_stacks']['applicable_ids'] = []
        self.assertEqual(columns.compile_columns(f[2], p, [], f[6])['banks'], [])

    def test_fixed_lane_member_skips_whole_bank(self):
        f = fixture(); f[1].fixed = {f[4]['part00'], f[4]['part13']}
        result, report, budget = arrange(f)
        self.assertIs(result, f[1]); self.assertEqual(budget['candidates'], 0)
        self.assertTrue(report['stack_column_arrangement']['skipped'])

    def test_static_rejection_rolls_back_everything(self):
        f = fixture(); state = deepcopy(f[1].__dict__)
        result, report, _ = arrange(f, safe=False)
        self.assertIs(result, f[1]); self.assertEqual(result.cell_of, state['cell_of'])
        self.assertEqual(result.blocks, state['blocks']); self.assertEqual(result.attachment_stack_bundles, ())
        self.assertGreater(report['stack_column_arrangement']['static_rejections'], 0)

    def test_real_static_validation_and_no_Astar(self):
        f = fixture()
        with patch.object(sa_routing, 'final_route', side_effect=AssertionError('no routing in local trial')):
            result, report, _ = arrange(f, safe='real')
        self.assertTrue(report['stack_column_arrangement']['applied'])
        self.assertTrue(ai._checked_static(structured.par, result, f[3], f[4], RoutingOptions(), perf_counter()+1))

    def test_guarded_lane_actions_and_undo_preserve_state_and_owners(self):
        f = fixture(); result, _, _ = arrange(f)
        blocks = deepcopy(result.blocks); rows = [r for r, _ in result.cell_of]
        for k in range(200):
            old = result.propose()
            if old is not None and k % 3: result.set_state(*old)
            self.assertTrue(result.valid_state()); self.assertEqual(result.blocks, blocks)
            self.assertEqual(result.row_of, rows)
            self.assertEqual(result.comp_at, {p: i for i, p in enumerate(result.cell_of)})
            self.assertEqual(columns.measure_columns(result, f[2], f[4], f[5]['stack_columns'])['pin_axis_error'], 0)

    def test_current_coherent_pose_can_adopt_policy_without_moves(self):
        f = fixture(aligned=True); result, report, _ = arrange(f)
        self.assertEqual(result.cell_of, f[1].cell_of); self.assertEqual(result.orient, f[1].orient)
        self.assertTrue(any(a['policy_only_adoption'] for a in report['actions']))
        self.assertTrue(result.attachment_stack_bundles)

    def test_bundle_tampering_is_restored_by_existing_checkpoint(self):
        f = fixture(); good, _, _ = arrange(f); damaged = deepcopy(good)
        damaged.attachment_stack_bundles = ()
        restored, report = ai.finalize_attachment_state(damaged, good)
        self.assertTrue(report['reverted']); self.assertEqual(restored.attachment_stack_bundles, good.attachment_stack_bundles)

    def test_deadline_zero_trials_and_one_remaining_trial_never_reset(self):
        f = fixture(); budget = ai.new_attachment_budget(max_candidates=1)
        result, report, _ = arrange(f, budget=budget, until=perf_counter()-1)
        self.assertIs(result, f[1]); self.assertEqual(budget['candidates'], 0)
        result, report, _ = arrange(f, budget=budget)
        self.assertEqual(budget['candidates'], 1)
        again, _, _ = arrange((*f[:1], result, *f[2:]), budget=budget, initial=False)
        self.assertEqual(budget['candidates'], 1)

    def test_finish_reserves_original_trials_and_releases_once(self):
        f = fixture(); budget = ai.new_attachment_budget(max_candidates=20)
        columns.reserve_finish(f[5], budget)
        self.assertEqual(budget['stack_column_reserve'], 16)
        budget['candidates'] = 4; self.assertFalse(ai._has_budget(budget, perf_counter()+1))
        _, report, _ = arrange(f, initial=False, budget=budget)
        self.assertNotIn('stack_column_reserve', budget); self.assertLessEqual(budget['candidates'], 20)
        self.assertLessEqual(report['stack_column_arrangement']['trials'], 16)

    def test_late_static_true_cannot_commit(self):
        f = fixture(); clock = [0.]; calls = [0]
        def late(*args): calls[0] += 1; clock[0] = 5.; return True
        with patch.object(columns, 'perf_counter', side_effect=lambda: clock[0]), patch.object(ai, 'perf_counter', side_effect=lambda: clock[0]), patch.object(ai, '_checked_static', side_effect=late):
            result, report, _ = arrange(f, until=4., safe='real')
        self.assertEqual(calls[0], 1); self.assertIs(result, f[1])

    def test_timeout_discards_partial_trial_but_retains_earlier_verified_best(self):
        f = fixture()
        with patch.object(ai, '_checked_static', side_effect=[True, None]):
            result, report, budget = arrange(f, safe='real')
        self.assertIsNot(result, f[1]); self.assertTrue(result.valid_state())
        self.assertTrue(budget['exhausted']); self.assertTrue(report['stack_column_arrangement']['applied'])
        self.assertEqual(columns.measure_columns(result, f[2], f[4], f[5]['stack_columns'])['pin_axis_error'], 0)

    def test_estimated_wire_guard_still_applies(self):
        f = fixture(); result, report, _ = arrange(f, cap=0)
        self.assertIs(result, f[1]); self.assertGreater(report['stack_column_arrangement']['hpwl_rejections'], 0)

    def test_mirror_translation_and_saved_only_measurement(self):
        f = fixture(); result, _, _ = arrange(f)
        before = columns.measure_columns(result, f[2], f[4], f[5]['stack_columns'])
        mirror = deepcopy(result); mirror.column_x = [x+92 for x in mirror.column_x]
        mirror.row_y = [y+64 for y in mirror.row_y]
        mirror.set_state([(r, mirror.cols-1-c) for r, c in mirror.cell_of], [1-o for o in mirror.orient])
        after = columns.measure_columns(mirror, f[2], f[4], json.loads(json.dumps(f[5]['stack_columns'])))
        self.assertEqual(columns.column_rank(before), columns.column_rank(after))
        mirror.__class__ = structured.StructuredPlacement
        self.assertTrue(columns.measure_columns(mirror, f[2], f[4], f[5]['stack_columns'])['measurable'])

    def test_contract_missing_data_NA_and_invalid_measurement_are_not_zero_gain(self):
        f = fixture(); self.assertIsNone(columns.measure_columns(f[1], f[2], f[4], {})['pin_axis_error'])
        p = deepcopy(f[5]['stack_columns']); p['terminal_contract'].pop(next(iter(p['terminal_contract'])))
        m = columns.measure_columns(f[1], f[2], f[4], p)
        self.assertFalse(m['measurable']); self.assertIsNone(columns.column_rank(m))
        m = columns.measure_columns(f[1], f[2], f[4], f[5]['stack_columns']); m['pin_axis_error'] = float('nan')
        self.assertIsNone(columns.column_rank(m))

    def test_common_candidate_measurement_and_missing_evidence_cannot_win(self):
        f = fixture(); new, _, _ = arrange(f)
        old = measure_candidate(f[1], f[2], f[4], dict(candidate_id=1), f[5], policy='all')
        better = measure_candidate(new, f[2], f[4], dict(candidate_id=2), f[5], policy='all')
        self.assertLess(organization_rank(better['layout_attachments']), organization_rank(old['layout_attachments']))
        better['layout_attachments'].pop('stack_column_organization')
        self.assertIsNone(organization_rank(better['layout_attachments']))

    def test_real_route_limits_and_old_quality_meaning_unchanged(self):
        f = fixture(); new, _, _ = arrange(f)
        def row(pl, candidate, score, wire, cross):
            md = measure_candidate(pl, f[2], f[4], dict(candidate_id=candidate), f[5], policy='all')
            return (score, pl, None, None, None, md, (None, dict(wirelength=wire, crossings=cross)), None, candidate)
        ref = row(f[1], 1, 10, 100, 0)
        for bad in (row(new, 2, 11, 116, 0), row(new, 2, 11, 110, 1)):
            selected, _ = select_attachment_result([ref, bad]); self.assertIs(selected, ref)
        selected, _ = select_attachment_result([ref, row(new, 2, 11, 110, 0)])
        self.assertEqual(selected[5]['candidate_id'], 2); self.assertEqual(selected[0], 11)

    def test_passive_policy_does_not_enable_stack_columns_or_draw_random(self):
        f = fixture(); state = f[1].rng.getstate()
        p = ai.build_attachment_plan(f[0], f[2], f[3], f[4], f[6], compute_levels(f[0]), f[1].row_of, policy='passive')
        self.assertEqual(p['stack_columns']['banks'], []); self.assertEqual(f[1].rng.getstate(), state)


if __name__ == '__main__': unittest.main()
