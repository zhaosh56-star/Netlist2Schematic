"""A short mirror reference can follow outputs retained at branch junctions."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from netlist2schematic.recognition.device_leveling import compute_levels
from tests.test_branch_levels import bjt, mos
from tests.test_branch_row_cost import family, estimate_for
from tests.test_corresponding_branch_rows import run_plan


def junction_family(make=mos):
    # Equal branches at join intentionally prevent choosing either output as
    # the continuation of upper. The tail is likewise retained at a merge.
    devices = dict(reference=make('bias', '0', 'bias'),
        output_a=make('join', '0', 'bias'), output_b=make('join', '0', 'bias'),
        upper=make('port', 'join', 'upper_control'),
        tail=make('tail_node', '0', 'bias'),
        input_a=make('da', 'tail_node', 'ga'), input_b=make('db', 'tail_node', 'gb'))
    rows = dict(reference=0, output_a=1, output_b=1, upper=0,
                tail=2, input_a=1, input_b=1)
    return devices, rows


class JunctionMirrorRowsTests(unittest.TestCase):
    def assert_safe(self, before, after, report):
        for order in report['strict_orders']:
            self.assertLess(after[order['upper']], after[order['lower']])
        for a, b in report['preserved_same_rows']:
            self.assertEqual(after[a], after[b])
        for column in report['columns']:
            deltas = {after[n] - before[n] for n in column['members']}
            self.assertEqual(len(deltas), 1)
            if column['locked_reason']:
                self.assertEqual(deltas, {0})
        self.assertEqual(report['electrical_edges_added'], [])
        json.dumps(report, allow_nan=False)

    def test_mos_and_bjt_use_retained_upstream_evidence_then_estimate_target(self):
        for make in (mos, bjt):
            with self.subTest(kind=make.__name__):
                devices, rows = junction_family(make)
                original = deepcopy(devices)
                levels = compute_levels(devices)
                seen = []

                def cost(state, **kwargs):
                    seen.append(state['reference'])
                    return abs(state['reference'] - 2), {}

                result, report = run_plan(devices, rows, estimate_rows=cost)
                self.assertEqual(result, {**rows, 'reference': 2})
                self.assertEqual(set(seen), {0, 1, 2})
                self.assertTrue(all(len(c['members']) == 1 for c in report['columns']))
                evidence = report['junction_mirror_anchors']
                self.assertEqual({r['output'] for r in evidence}, {'output_a', 'output_b', 'tail'})
                self.assertTrue(all(r['upstream'] for r in evidence))
                self.assertEqual(report['actions'][0]['selection'], 'minimum_estimated_cost')
                self.assertEqual(devices, original)
                self.assertEqual(compute_levels(devices), levels)
                self.assert_safe(rows, result, report)

    def test_original_row_and_equal_cost_keep_original_position(self):
        devices, rows = junction_family()
        for cost in (lambda s, **kw: (s['reference'], {}), lambda s, **kw: (1., {})):
            with self.subTest(cost=cost):
                result, report = run_plan(devices, rows, estimate_rows=cost)
                self.assertEqual(result, rows)
                self.assertFalse(report['applied'])
                self.assertTrue(report['target_search']['activated'])

    def test_unique_junction_target_also_requires_an_improving_estimate(self):
        devices, rows = junction_family()
        for name in ('tail', 'input_a', 'input_b'):
            devices.pop(name)
            rows.pop(name)
        result, report = run_plan(devices, rows,
            estimate_rows=lambda s, **kw: (1. - s['reference'], {}))
        self.assertEqual(result, {**rows, 'reference': 1})
        self.assertEqual(report['target_search']['evaluated_states'], 2)
        result, report = run_plan(devices, rows)
        self.assertEqual(result, rows)
        self.assertTrue(any(s['reason'] == 'junction_target_requires_estimate' for s in report['skipped']))

    def test_fixed_reference_is_never_moved_or_estimated(self):
        devices, rows = junction_family()
        with patch('netlist2schematic.plugins.columns.branch_rows.perf_counter', return_value=1.):
            result, report = run_plan(devices, rows, fixed=['reference'],
                estimate_rows=lambda *a, **kw: (_ for _ in ()).throw(AssertionError('fixed')))
        self.assertEqual(result, rows)
        self.assertFalse(report['target_search']['activated'])

    def test_common_control_without_a_unique_diode_reference_is_not_evidence(self):
        for ambiguous in (False, True):
            devices, rows = junction_family()
            if ambiguous:
                devices['second_reference'] = deepcopy(devices['reference'])
                rows['second_reference'] = 0
            else:
                devices['reference']['d'] = 'independent_drain'
            result, report = run_plan(devices, rows,
                estimate_rows=lambda *a, **kw: (_ for _ in ()).throw(AssertionError('no unique mirror')))
            self.assertEqual(result, rows)
            self.assertEqual(report.get('junction_mirror_anchors'), [])

    def test_unrooted_equal_columns_do_not_anchor_each_other(self):
        devices = dict(reference=mos('bias', '0', 'bias'), output=mos('free', '0', 'bias'))
        rows = dict(reference=0, output=1)
        result, report = run_plan(devices, rows,
            estimate_rows=lambda *a, **kw: (_ for _ in ()).throw(AssertionError('no upstream')))
        self.assertEqual(result, rows)
        self.assertEqual(report['junction_mirror_anchors'], [])

    def test_other_same_row_protection_remains_hard(self):
        devices, rows = junction_family()
        from netlist2schematic.layout.structured_placement import _structures
        records = _structures(devices) + [dict(kind='differential_pair', confidence='high',
            roles={}, layout=dict(align_rows=[['reference', 'upper']]))]
        result, report = run_plan(devices, rows, records=records,
            estimate_rows=lambda s, **kw: (-s['reference'], {}))
        self.assertEqual(result, rows)
        self.assertTrue(all(t['status'] == 'hard_constraint_rejected'
                            for t in report['target_search']['trials']))
        self.assert_safe(rows, result, report)

    def test_interrupted_selection_rolls_back_partial_improvement(self):
        devices, rows = junction_family()
        result, report = run_plan(devices, rows, max_estimates=2,
            estimate_rows=lambda s, **kw: (-s['reference'], {}))
        self.assertEqual(result, rows)
        self.assertEqual(report['target_search']['stop_reason'], 'local_budget')
        self.assertFalse(any(t['accepted'] for t in report['target_search']['trials']))

    def test_renaming_reorder_and_translation_preserve_topological_result(self):
        devices, rows = junction_family()
        original, _ = run_plan(devices, rows,
            estimate_rows=lambda s, **kw: (abs(s['reference'] - s['tail']), {}))
        names = {n: 'unit' + str(i) for i, n in enumerate(reversed(devices))}
        def node(n):
            return n if n in ('0', 'vdd') else 'other_' + n
        changed = {names[n]: {k: node(v) if k in ('d', 's', 'g', 'bulk') else v
                              for k, v in dev.items()} for n, dev in reversed(list(devices.items()))}
        shifted = {names[n]: r + 3 for n, r in rows.items()}
        result, report = run_plan(changed, shifted,
            estimate_rows=lambda s, **kw: (abs(s[names['reference']] - s[names['tail']]), {}))
        self.assertEqual({n: result[names[n]] - 3 for n in rows}, original)
        self.assert_safe(shifted, result, report)

    def test_real_initializer_estimates_junction_targets_without_sa_or_astar(self):
        devices, rows = junction_family()
        estimator = estimate_for(devices)
        with patch('netlist2schematic.layout.structured_placement._anneal',
                   side_effect=AssertionError('No SA in row estimates')), \
                patch('netlist2schematic.routing.sa_routing.final_route',
                      side_effect=AssertionError('No A* in row estimates')):
            result, report = run_plan(devices, rows, estimate_rows=estimator)
        self.assertTrue(report['target_search']['activated'])
        self.assertEqual(report['target_search']['evaluated_states'], 3)
        self.assertTrue(all(e['metrics']['astar_calls'] == 0
                            for e in report['target_search']['evaluations']))
        self.assert_safe(rows, result, report)

    def test_existing_longer_column_target_rule_stays_the_same(self):
        devices, rows = family()
        result, report = run_plan(devices, rows,
            estimate_rows=lambda s, **kw: (abs(s['reference'] - 2), {}))
        self.assertEqual(result, {**rows, 'reference': 2})
        self.assertEqual(report['target_search']['evaluated_states'], 3)


if __name__ == '__main__':
    unittest.main()
