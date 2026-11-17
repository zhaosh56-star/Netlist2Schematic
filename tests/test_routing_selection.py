"""Final candidates follow strict routing priorities, after safety validation."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from itertools import permutations
import unittest
from unittest.mock import patch

from netlist2schematic.flow import aesthetic_flow as flow
from netlist2schematic.flow.candidate_selection import POLICY, select_result
from netlist2schematic.plugins.passive_skeleton import selection as legacy_selection
from tests.test_reserved_flow import candidate, valid


def result(ident, bends, crossings, length, *, score=0, verified=True):
    metrics = dict(bends=bends, crossings=crossings, wirelength=length)
    return ((score, crossings, length, str(ident)), None, [], {}, {}, {},
            ([], metrics, {'valid': verified}), {'score': score}, str(ident))


class RoutingPriorityTests(unittest.TestCase):
    def test_fewer_bends_wins_even_with_more_crossings_and_much_longer_wires(self):
        straight = result(2, 1, 20, 10000, score=999)
        short = result(3, 2, 0, 100, score=-999)
        selected, report = select_result([short, straight])
        self.assertIs(selected, straight)
        self.assertFalse(report['relative_wirelength_or_crossing_caps_applied'])
        self.assertFalse(report['quality_score_used_for_selection'])

    def test_equal_bends_uses_crossings_before_length(self):
        fewer_crossings = result(2, 4, 0, 5000)
        shorter = result(3, 4, 1, 100)
        self.assertIs(select_result([shorter, fewer_crossings])[0], fewer_crossings)

    def test_equal_bends_and_crossings_uses_length_before_old_quality(self):
        shorter = result(3, 4, 2, 900, score=999)
        old_favorite = result(2, 4, 2, 1000, score=0)
        self.assertIs(select_result([old_favorite, shorter])[0], shorter)

    def test_exact_metric_ties_use_stable_id_and_ignore_old_score(self):
        rows = [result(3, 2, 1, 1000, score=-100), result(2, 2, 1, 1000, score=100)]
        for order in permutations(rows):
            self.assertEqual('2', select_result(order)[0][8])

    def test_unverified_drawing_cannot_win_with_ideal_metrics(self):
        unsafe = result(2, 0, 0, 0, verified=False)
        safe = result(3, 10, 10, 10000)
        selected, report = select_result([unsafe, safe])
        self.assertIs(selected, safe)
        self.assertEqual(['2'], report['excluded_candidates'])

    def test_requires_a_verified_result(self):
        for rows in ([], [result(2, 0, 0, 0, verified=False)]):
            with self.assertRaises(ValueError):select_result(rows)

    def test_report_contains_actual_metrics_and_is_json_serializable(self):
        rows = [result(2, 3, 0, 100), result(3, 2, 1, 200)]
        before = deepcopy(rows)
        selected, report = select_result(rows, stage='after_shared_route_simplification')
        self.assertEqual(POLICY, report['policy'])
        self.assertEqual(['bends', 'crossings', 'wirelength'], report['ranking'])
        self.assertEqual('3', report['selected_candidate'])
        self.assertEqual(dict(bends=2, crossings=1, wirelength=200), report['selected_metrics'])
        self.assertEqual('after_shared_route_simplification', report['comparison_stage'])
        self.assertEqual(before, rows)
        json.dumps(report)


class FinalSelectionFlowTests(unittest.TestCase):
    def test_layout_preference_and_old_score_cannot_override_final_routing_order(self):
        items = [candidate(i) for i in (2, 3, 1)]
        plan = dict(version='fixture', applicable=True)
        for item in items:
            item[4]['passive_skeleton'] = dict(applied=True, plan=plan)
        first, second = valid(), valid()
        first[1].update(bends=3, crossings=0, wirelength=100)
        second[1].update(bends=2, crossings=20, wirelength=10000)
        def old_preference(rows, *args, **kwargs):
            return rows[0], dict(policy='old-structure-priority', selected_candidate='2')
        with patch('netlist2schematic.layout.structured_placement.create_candidates', return_value=iter(items)), \
             patch.object(flow, 'final_route', side_effect=[first, second]) as router, \
             patch('netlist2schematic.layout.layout_quality.evaluate_quality', side_effect=[{'score':0}, {'score':999}]), \
             patch('netlist2schematic.plugins.passive_skeleton.plan.measure', return_value={}), \
             patch.object(legacy_selection, 'select_result', side_effect=old_preference), \
             redirect_stdout(io.StringIO()):
            output = flow.select_layout('unused.cir', passive_skeleton='auto')
        report = output[4]['quality']
        self.assertEqual('3', report['selected_candidate'])
        self.assertEqual(POLICY, report['selection_policy'])
        self.assertEqual(999, report['score'], 'The original score remains a diagnostic')
        self.assertEqual(2, router.call_count, 'The reference stays failure-only')
        old = report['layout_preference_diagnostics']['passive_skeleton']
        self.assertEqual('2', old['proposed_candidate'])
        self.assertFalse(old['used_for_selection'])
        self.assertNotIn('passive_skeleton_selection', report)

    def test_default_selection_uses_metrics_after_shared_simplification(self):
        items = [candidate(i) for i in (2, 3, 1)]
        first, second = valid(), valid()
        first[1].update(bends=8, crossings=0, wirelength=100)
        second[1].update(bends=2, crossings=0, wirelength=100)
        def cleanup(path, cell, par, rows, *args):
            cleaned = deepcopy(rows)
            cleaned[0][6][1]['bends'] = 1
            return cleaned, {'policy':'fixture'}
        with patch('netlist2schematic.layout.structured_placement.create_candidates', return_value=iter(items)), \
             patch.object(flow, 'final_route', side_effect=[first, second]), \
             patch('netlist2schematic.layout.layout_quality.evaluate_quality', side_effect=[{'score':99}, {'score':0}]), \
             patch.object(flow, '_clean_primary_results', side_effect=cleanup), \
             redirect_stdout(io.StringIO()):
            output = flow.select_layout('unused.cir', passive_skeleton='auto')
        report = output[4]['quality']
        self.assertEqual('2', report['selected_candidate'])
        self.assertEqual(1, report['routing_selection']['selected_metrics']['bends'])
        self.assertEqual('after_shared_route_simplification', report['routing_selection']['comparison_stage'])

    def test_explicit_replay_keeps_the_requested_id(self):
        with patch('netlist2schematic.layout.structured_placement.create_candidates', return_value=iter([candidate(3)])), \
             patch.object(flow, 'final_route', return_value=valid()), \
             redirect_stdout(io.StringIO()):
            output = flow.select_layout('unused.cir', passive_skeleton='auto', candidate_id='3')
        self.assertEqual('3', output[4]['quality']['selected_candidate'])
        self.assertEqual('explicit_candidate_replay', output[4]['quality']['selection_policy'])
        self.assertNotIn('routing_selection', output[4]['quality'])


if __name__ == '__main__':unittest.main()
