"""Incumbent safety, objective bounds and finite-work shortcut regressions."""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace
from time import perf_counter
import unittest
from unittest.mock import patch

import simplify_routes as shortcuts
from orthogonal_router import RoutingFailure
from routing_rules import Pin, RoutingOptions, Scene, canonical_topology, intersection
from sa_routing import _verify


def case(*, foreign=False, cycle_trap=False, parallel_escapes=False):
    """Independent pin oracle: actual validation, no bodies or resource files."""
    positions = [(0, 0), (100, 0)]
    pins = {'a': [Pin('a', 0, 'P', positions[0], (10, 0), (1, 0)),
                  Pin('a', 1, 'P', positions[1], (90, 0), (-1, 0))]}
    height = 40
    routes = [('a', [((0, 0), (10, 0)), ((10, 0), (10, height)),
                     ((10, height), (90, height)), ((90, height), (90, 0)),
                     ((90, 0), (100, 0))])]
    if parallel_escapes:
        pins['a'] = [Pin('a', i, 'P', positions[i], (positions[i][0], -20), (0, -1))
                     for i in (0, 1)]
        routes = [('a', [((0, 0), (0, -100)), ((0, -100), (100, -100)),
                         ((100, -100), (100, 0))])]
    nets = {'a': [('A', 'P'), ('B', 'P')]}
    index = {'A': 0, 'B': 1}
    if foreign:
        positions.extend(((50, -60), (50, 20)))
        pins['foreign'] = [Pin('foreign', i, 'P', positions[i], positions[i], (0, 1))
                           for i in (2, 3)]
        nets['foreign'] = [('C', 'P'), ('D', 'P')]
        index.update(C=2, D=3)
        routes.append(('foreign', [((50, -60), (50, 20))]))
    if cycle_trap:
        positions.append((50, 20))
        pins['a'].append(Pin('a', 2, 'P', positions[2], positions[2], (0, 1)))
        nets['a'].append(('C', 'P'))
        index['C'] = 2
        routes[0][1].extend([((10, 0), (10, -40)), ((10, -40), (50, -40)),
                              ((50, -40), (50, 20))])
    scene = Scene(options=RoutingOptions(), pins=pins,
                  all_pins={net: [pin.point for pin in members] for net, members in pins.items()},
                  fixed_segments={net: [pin.stub for pin in members if pin.point != pin.escape]
                                  for net, members in pins.items()})
    placement = SimpleNamespace(comps=[], pin_world=lambda i, pin: positions[i])

    def crossing(a, b, c, d):
        point = intersection(a, b, c, d)
        return point is not None and point not in (a, b, c, d)

    par = SimpleNamespace(power_marker_sym=lambda net: None, GEO_DOC={}, MARGIN=12,
                          seg_cross=crossing, seg_overlap_len=lambda *args: 0,
                          seg_parallel_prox=lambda *args, **kwargs: 0,
                          seg_hits_rect=lambda *args: False)
    checked, metrics, _ = _verify(par, nets, placement, index, routes, scene)
    validation = {'valid': True, 'router': 'original-router', 'routing_stage': 'normal',
                  'search': {'marker': 'preserve'}, 'parameters': {}}
    return par, nets, placement, index, (checked, metrics, validation), scene


def run_case(fixture, **kwargs):
    par, nets, placement, index, result, scene = fixture
    with patch('simplify_routes.build_scene', return_value=scene):
        return shortcuts.simplify_routes(par, nets, placement, index, result, **kwargs)


class ShortcutSafetyTests(unittest.TestCase):
    def test_three_segment_stage_preserves_both_old_operations_and_rebuilds_after_acceptance(self):
        fixture = case()
        observed = []

        def arch(height):
            return [((0, 0), (10, 0)), ((10, 0), (10, height)),
                    ((10, height), (90, height)), ((90, height), (90, 0)), ((90, 0), (100, 0))]

        def old_shortcuts(scene, routed, deadline):
            length = shortcuts._wire_metrics(dict(routed)['a'])[0]
            observed.append(('chain_shortcut', length))
            if length > 140:
                yield {'net': 'a', 'segments': arch(20)}

        def old_reattachment(scene, routed, deadline):
            length = shortcuts._wire_metrics(dict(routed)['a'])[0]
            observed.append(('leaf_reattachment', length))
            if length > 120:
                self.assertEqual(140, length)
                yield {'net': 'a', 'segments': arch(10)}

        def patterns(scene, routed, deadline):
            length = shortcuts._wire_metrics(dict(routed)['a'])[0]
            observed.append(('three_segment_shortcut', length))
            if length > 100:
                self.assertEqual(120, length, 'the complete v3 result must be retained first')
                yield {'net': 'a', 'segments': [((0, 0), (100, 0))]}

        with patch('simplify_routes._shortcut_candidates', side_effect=old_shortcuts), \
                patch('simplify_routes.reattachment_candidates', side_effect=old_reattachment), \
                patch('simplify_routes._three_segment_candidates', side_effect=patterns):
            routes, metrics, _, report = run_case(fixture)
        self.assertEqual(100, metrics['wirelength'])
        self.assertEqual(120, report['v3_metrics']['wirelength'])
        self.assertEqual(['chain_shortcut', 'leaf_reattachment', 'three_segment_shortcut'],
                         [step['operation'] for step in report['accepted_changes']])
        self.assertEqual(['chain_warmup', 'coordinated_refinement', 'three_segment_refinement'],
                         [step['phase'] for step in report['accepted_changes']])
        self.assertIn(('chain_shortcut', 100), observed)
        self.assertIn(('leaf_reattachment', 100), observed)
        self.assertEqual(metrics, report['accepted_changes'][-1]['after_metrics'])
        _verify(*fixture[:4], routes, fixture[5])

    def test_exhausted_old_stage_budget_is_not_reset_for_new_patterns(self):
        fixture = case()

        def old_shortcuts(scene, routed, deadline):
            for height in (20, 10):
                yield {'net': 'a', 'segments': [((0, 0), (10, 0)), ((10, 0), (10, height)),
                    ((10, height), (90, height)), ((90, height), (90, 0)), ((90, 0), (100, 0))]}

        with patch('simplify_routes._shortcut_candidates', side_effect=old_shortcuts), \
                patch('simplify_routes._three_segment_candidates', side_effect=AssertionError('new phase')), \
                patch('simplify_routes.MAX_CANDIDATES', 1):
            routes, metrics, _, report = run_case(fixture)
        self.assertEqual(140, metrics['wirelength'])
        self.assertEqual('candidate_budget', report['stop_reason'])
        self.assertFalse(report['v3_completed'])
        self.assertEqual(0, report['operations']['three_segment_shortcut']['considered'])
        _verify(*fixture[:4], routes, fixture[5])

    def test_operations_rebuild_from_latest_incumbent_without_restoring_old_geometry(self):
        fixture = case()
        observed = []

        def old_shortcuts(scene, routed, deadline):
            observed.append(('chain_shortcut', shortcuts._wire_metrics(dict(routed)['a'])[0]))
            if shortcuts._wire_metrics(dict(routed)['a'])[0] > 140:
                yield {'net': 'a', 'segments': [((0, 0), (10, 0)), ((10, 0), (10, 20)),
                    ((10, 20), (90, 20)), ((90, 20), (90, 0)), ((90, 0), (100, 0))]}

        def new_attachments(scene, routed, deadline):
            length = shortcuts._wire_metrics(dict(routed)['a'])[0]
            observed.append(('leaf_reattachment', length))
            if length > 100:
                self.assertEqual(140, length, 'new operation must see the accepted shortcut')
                yield {'net': 'a', 'segments': [((0, 0), (100, 0))]}

        with patch('simplify_routes._shortcut_candidates', side_effect=old_shortcuts), \
                patch('simplify_routes.reattachment_candidates', side_effect=new_attachments):
            routes, metrics, validation, report = run_case(fixture)
        self.assertEqual(100, metrics['wirelength'])
        self.assertEqual(0, metrics['bends'])
        self.assertTrue(validation['valid'])
        self.assertEqual(['chain_shortcut', 'leaf_reattachment'],
                         [change['operation'] for change in report['accepted_changes']])
        self.assertIn(('leaf_reattachment', 140), observed)
        self.assertIn(('chain_shortcut', 100), observed)
        self.assertEqual(report['accepted_changes'][0]['after_metrics'],
                         report['accepted_changes'][1]['before_metrics'])
        self.assertEqual(metrics, report['accepted_changes'][-1]['after_metrics'])
        _verify(*fixture[:4], routes, fixture[5])

    def test_rejected_duplicate_shortcuts_leave_budget_for_branch_reattachment(self):
        fixture = case()

        def many_rejections(scene, routed, deadline):
            if shortcuts._wire_metrics(dict(routed)['a'])[0] > 100:
                for _ in range(20):
                    yield {'net': 'a', 'segments': fixture[4][0][0][1]}

        def improvement(scene, routed, deadline):
            if shortcuts._wire_metrics(dict(routed)['a'])[0] > 100:
                yield {'net': 'a', 'segments': [((0, 0), (100, 0))]}

        with patch('simplify_routes._shortcut_candidates', side_effect=many_rejections), \
                patch('simplify_routes.reattachment_candidates', side_effect=improvement), \
                patch('simplify_routes.MAX_CANDIDATES', 4):
            routes, metrics, _, report = run_case(fixture)
        self.assertEqual(100, metrics['wirelength'])
        self.assertEqual(2, report['candidate_counts']['considered'])
        self.assertEqual(1, report['operations']['chain_shortcut']['considered'])
        self.assertEqual(1, report['operations']['leaf_reattachment']['considered'])
        self.assertEqual('leaf_reattachment', report['accepted_changes'][0]['operation'])
        _verify(*fixture[:4], routes, fixture[5])

    def test_existing_shortcuts_finish_before_branch_moves_can_change_the_tree(self):
        fixture = case()

        def warmup(scene, routed, deadline):
            length = shortcuts._wire_metrics(dict(routed)['a'])[0]
            if length > 120:
                height = 20 if length > 140 else 10
                yield {'net': 'a', 'segments': [((0, 0), (10, 0)), ((10, 0), (10, height)),
                    ((10, height), (90, height)), ((90, height), (90, 0)), ((90, 0), (100, 0))]}

        def reattach(scene, routed, deadline):
            length = shortcuts._wire_metrics(dict(routed)['a'])[0]
            if length > 100:
                self.assertEqual(120, length, 'all existing shortcuts must run before reattachment')
                yield {'net': 'a', 'segments': [((0, 0), (100, 0))]}

        with patch('simplify_routes._shortcut_candidates', side_effect=warmup), \
                patch('simplify_routes.reattachment_candidates', side_effect=reattach):
            routes, metrics, _, report = run_case(fixture)
        self.assertEqual(100, metrics['wirelength'])
        self.assertEqual(120, report['warmup_metrics']['wirelength'])
        self.assertEqual(['chain_shortcut', 'chain_shortcut', 'leaf_reattachment'],
                         [change['operation'] for change in report['accepted_changes']])
        self.assertEqual(['chain_warmup', 'chain_warmup', 'coordinated_refinement'],
                         [change['phase'] for change in report['accepted_changes']])
        _verify(*fixture[:4], routes, fixture[5])

    def test_a_straight_shortcut_is_fully_verified_and_preserves_search_metadata(self):
        fixture = case()
        original = deepcopy(fixture[4])
        with patch('simplify_routes._verify', wraps=_verify) as verify:
            routed, metrics, validation, report = run_case(fixture)
        self.assertGreaterEqual(verify.call_count, 2)
        self.assertEqual(0, metrics['bends'])
        self.assertEqual(100, metrics['wirelength'])
        self.assertEqual('original-router', validation['router'])
        self.assertEqual('normal', validation['routing_stage'])
        self.assertEqual({'marker': 'preserve'}, validation['search'])
        self.assertEqual(original, fixture[4])
        self.assertTrue(report['converged'])
        self.assertEqual(['a'], report['changed_nets'])
        json.dumps(report, allow_nan=False)

    def test_extra_legal_crossing_is_rejected_even_with_shorter_straighter_wire(self):
        fixture = case(foreign=True)
        bad = [('a', [((0, 0), (100, 0))]), fixture[4][0][1]]
        self.assertEqual(1, _verify(*fixture[:4], bad, fixture[5])[1]['crossings'])
        routed, metrics, _, report = run_case(fixture)
        self.assertEqual(fixture[4][1], report['v3_metrics'])
        self.assertLessEqual(metrics['wirelength'], fixture[4][1]['wirelength'])
        self.assertLessEqual(metrics['bends'], fixture[4][1]['bends'])
        self.assertEqual(0, metrics['crossings'])
        self.assertGreater(report['candidate_counts']['objective_rejected'], 0)
        self.assertEqual(dict(fixture[4][0])['foreign'], dict(routed)['foreign'])
        _verify(*fixture[:4], routed, fixture[5])

    def test_equal_bend_shortening_still_rejects_an_extra_legal_crossing(self):
        fixture = case(foreign=True, parallel_escapes=True)
        bad = [('a', [((0, 0), (0, -20)), ((0, -20), (100, -20)),
                       ((100, -20), (100, 0))]), fixture[4][0][1]]
        self.assertEqual(1, _verify(*fixture[:4], bad, fixture[5])[1]['crossings'])
        routed, metrics, _, report = run_case(fixture)
        self.assertEqual(fixture[4][1], report['v3_metrics'])
        self.assertLessEqual(metrics['wirelength'], fixture[4][1]['wirelength'])
        self.assertLessEqual(metrics['bends'], fixture[4][1]['bends'])
        self.assertEqual(0, metrics['crossings'])
        self.assertGreater(report['candidate_counts']['validated'], 0)
        self.assertGreater(report['candidate_counts']['objective_rejected'], 0)
        self.assertEqual(dict(fixture[4][0])['foreign'], dict(routed)['foreign'])
        self.assertTrue(shortcuts._covers_fixed(fixture[5], 'a', dict(routed)['a']))
        _verify(*fixture[:4], routed, fixture[5])

    def test_objective_requires_a_strict_gain_without_new_bends_or_length(self):
        incumbent = {'wirelength': 300.0, 'bends': 2, 'crossings': 0}
        candidates = (
            ({'wirelength': 140.0, 'bends': 2, 'crossings': 0}, True),
            ({'wirelength': 300.0, 'bends': 1, 'crossings': 0}, True),
            ({'wirelength': 300.0, 'bends': 2, 'crossings': 0}, False),
            ({'wirelength': 140.0, 'bends': 3, 'crossings': 0}, False),
            ({'wirelength': 301.0, 'bends': 1, 'crossings': 0}, False),
            ({'wirelength': 140.0, 'bends': 2, 'crossings': 1}, False),
            ({'wirelength': 300.0-shortcuts.EPS/2, 'bends': 2, 'crossings': 0}, False),
        )
        for candidate, expected in candidates:
            with self.subTest(candidate=candidate):
                self.assertEqual(expected, shortcuts._improves(candidate, incumbent))

    def test_reusing_own_tree_cannot_introduce_a_cycle(self):
        fixture = case(cycle_trap=True)
        routed, metrics, _, report = run_case(fixture)
        self.assertGreater(report['candidate_counts']['cycle_rejected'], 0)
        self.assertEqual(0, shortcuts._cycle_rank(dict(routed)['a']))
        self.assertLess(metrics['bends'], fixture[4][1]['bends'])

    def test_timeout_during_trial_validation_returns_the_prior_verified_incumbent(self):
        fixture = case()
        calls = 0
        clock = {'now': perf_counter()}

        def verify(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                clock['now'] += 6
                raise RoutingFailure('expired', {'reason': 'quality_time_budget'})
            return _verify(*args, **kwargs)

        with patch('simplify_routes._verify', side_effect=verify), \
                patch('simplify_routes.perf_counter', side_effect=lambda: clock['now']):
            routed, metrics, validation, report = run_case(fixture)
        self.assertEqual(fixture[4][0], routed)
        self.assertEqual(fixture[4][1], metrics)
        self.assertTrue(validation['valid'])
        self.assertEqual('time_budget', report['stop_reason'])
        self.assertEqual(0, report['candidate_counts']['accepted'])

    def test_timeout_after_acceptance_keeps_the_last_complete_improvement(self):
        fixture = case()
        extract = shortcuts._chains
        calls = 0
        clock = {'now': perf_counter()}

        def chains(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                clock['now'] += 6
                raise RoutingFailure('expired', {'reason': 'quality_time_budget'})
            return extract(*args, **kwargs)

        with patch('simplify_routes._chains', side_effect=chains), \
                patch('simplify_routes.perf_counter', side_effect=lambda: clock['now']):
            routed, metrics, validation, report = run_case(fixture)
        self.assertEqual(100, metrics['wirelength'])
        self.assertEqual(0, metrics['bends'])
        self.assertTrue(validation['valid'])
        self.assertEqual('time_budget', report['stop_reason'])
        self.assertFalse(report['converged'])
        self.assertEqual(1, report['candidate_counts']['accepted'])
        _verify(*fixture[:4], routed, fixture[5])

    def test_candidate_budget_has_a_safe_explicit_stop(self):
        fixture = case()
        with patch('simplify_routes.MAX_CANDIDATES', 0):
            routed, metrics, validation, report = run_case(fixture)
        self.assertEqual(fixture[4][0], routed)
        self.assertEqual(fixture[4][1], metrics)
        self.assertTrue(validation['valid'])
        self.assertEqual('candidate_budget', report['stop_reason'])
        self.assertFalse(report['converged'])
        self.assertEqual(0, report['candidate_counts']['considered'])

    def test_simple_completed_result_is_naturally_idempotent(self):
        fixture = case(cycle_trap=True)
        first = run_case(fixture)
        again = (*fixture[:4], first[:3], fixture[5])
        second = run_case(again)
        self.assertEqual(first[:3], second[:3])
        self.assertEqual(0, second[3]['candidate_counts']['accepted'])
        self.assertTrue(second[3]['converged'])

    def test_scene_construction_expiry_does_not_lose_valid_input(self):
        fixture = case()
        error = RoutingFailure('expired', {'reason': 'quality_time_budget'})
        with patch('simplify_routes.build_scene', side_effect=error):
            result = shortcuts.simplify_routes(*fixture[:4], fixture[4])
        self.assertEqual(fixture[4], result[:3])
        self.assertEqual('time_budget', result[3]['stop_reason'])
        self.assertFalse(result[3]['original_revalidated'])

    def test_unvalidated_input_and_bad_budgets_are_not_promoted(self):
        fixture = case()
        for seconds in (-1, float('inf'), True):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                run_case(fixture, seconds=seconds)
        invalid = (*fixture[:4], (fixture[4][0], fixture[4][1], {'valid': False}), fixture[5])
        with self.assertRaises(ValueError):
            run_case(invalid)


if __name__ == '__main__':
    unittest.main()
