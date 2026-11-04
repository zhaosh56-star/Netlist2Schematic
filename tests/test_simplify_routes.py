"""Incumbent safety, objective bounds and finite-work shortcut regressions."""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import simplify_routes as shortcuts
from orthogonal_router import RoutingFailure
from routing_rules import Pin, RoutingOptions, Scene, canonical_topology, intersection
from sa_routing import _verify


def case(*, foreign=False, cycle_trap=False):
    """Independent pin oracle: actual validation, no bodies or resource files."""
    positions = [(0, 0), (100, 0)]
    pins = {'a': [Pin('a', 0, 'P', positions[0], (10, 0), (1, 0)),
                  Pin('a', 1, 'P', positions[1], (90, 0), (-1, 0))]}
    height = 40
    routes = [('a', [((0, 0), (10, 0)), ((10, 0), (10, height)),
                     ((10, height), (90, height)), ((90, height), (90, 0)),
                     ((90, 0), (100, 0))])]
    nets = {'a': [('A', 'P'), ('B', 'P')]}
    index = {'A': 0, 'B': 1}
    if foreign:
        positions.extend(((50, -20), (50, 20)))
        pins['foreign'] = [Pin('foreign', i, 'P', positions[i], positions[i], (0, 1))
                           for i in (2, 3)]
        nets['foreign'] = [('C', 'P'), ('D', 'P')]
        index.update(C=2, D=3)
        routes.append(('foreign', [((50, -20), (50, 20))]))
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
        routed, metrics, _, report = run_case(fixture)
        self.assertEqual(fixture[4][0], routed)
        self.assertEqual(fixture[4][1], metrics)
        self.assertEqual(0, metrics['crossings'])
        self.assertGreater(report['candidate_counts']['objective_rejected'], 0)
        self.assertEqual(0, report['candidate_counts']['geometry_rejected'])

    def test_reusing_own_tree_cannot_introduce_a_cycle(self):
        fixture = case(cycle_trap=True)
        routed, metrics, _, report = run_case(fixture)
        self.assertGreater(report['candidate_counts']['cycle_rejected'], 0)
        self.assertEqual(0, shortcuts._cycle_rank(dict(routed)['a']))
        self.assertLess(metrics['bends'], fixture[4][1]['bends'])

    def test_timeout_during_trial_validation_returns_the_prior_verified_incumbent(self):
        fixture = case()
        calls = 0

        def verify(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RoutingFailure('expired', {'reason': 'quality_time_budget'})
            return _verify(*args, **kwargs)

        with patch('simplify_routes._verify', side_effect=verify):
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

        def chains(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RoutingFailure('expired', {'reason': 'quality_time_budget'})
            return extract(*args, **kwargs)

        with patch('simplify_routes._chains', side_effect=chains):
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
