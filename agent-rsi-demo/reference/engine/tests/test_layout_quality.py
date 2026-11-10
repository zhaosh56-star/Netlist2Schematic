"""Final drawing metrics and bounded routing, independent of SA estimates."""
from __future__ import annotations

import json
from time import perf_counter
import unittest

from layout_quality import evaluate_quality
from orthogonal_router import RoutingFailure, route_scene
from routing_rules import Pin, RoutingOptions, Scene, analyze_routes


class FixturePlacement:
    def __init__(self, positions, pins):
        self.positions, self.pins = positions, pins

    def pos_of(self, index):
        return self.positions[index]

    def pin_world(self, index, pin):
        return self.pins[index, pin]


def quality(points, routes, positions=None, structs=()):
    names = sorted({name for members in points.values() for name, _, _ in members})
    index = {name: i for i, name in enumerate(names)}
    anchors = {(index[name], pin): point for members in points.values() for name, pin, point in members}
    pl = FixturePlacement(positions or [(0, 0)] * len(names), anchors)
    nets = {net: [(name, pin) for name, pin, _ in members] for net, members in points.items()}
    return evaluate_quality(None, pl, [], nets, index, routes, {}, structs)


class FinalQualityTests(unittest.TestCase):
    def test_score_is_normalized_and_necessary_long_trunks_have_no_detour(self):
        points = {'a': [('a', 'P', (0, 0)), ('b', 'P', (1000, 0))]}
        result = quality(points, [('a', [((0, 0), (1000, 0))])])
        self.assertEqual(10, result['score'])
        self.assertEqual(0, result['detour'])
        self.assertEqual(1, result['per_net']['a']['detour_factor'])
        self.assertEqual(0, result['outside_length'])
        json.dumps(result, allow_nan=False)

    def test_crossings_are_counted_after_merging_arbitrary_subdivisions(self):
        points = {'a': [('a', 'P', (-50, 0)), ('b', 'P', (50, 0))],
                  'b': [('c', 'P', (0, -50)), ('d', 'P', (0, 50))]}
        routes = [('a', [((-50, 0), (0, 0)), ((0, 0), (50, 0)), ((-50, 0), (20, 0))]),
                  ('b', [((0, -50), (0, 0)), ((0, 0), (0, 50))])]
        result = quality(points, routes)
        self.assertEqual(1, result['crossings'])
        self.assertEqual(0, result['bends'])
        self.assertEqual(200, result['wirelength'])
        self.assertEqual(3.5, result['score'])

    def test_detour_uses_terminal_hpwl_and_reports_outside_excursion(self):
        points = {'a': [('a', 'P', (0, 0)), ('b', 'P', (100, 0))]}
        result = quality(points, [('a', [((0, 0), (0, 50)), ((0, 50), (100, 50)),
                                         ((100, 50), (100, 0))])])
        self.assertEqual(200, result['wirelength'])
        self.assertEqual(100, result['detour'])
        self.assertEqual(2, result['per_net']['a']['detour_factor'])
        self.assertEqual(200, result['outside_length'])
        self.assertAlmostEqual(2 + .3 + .5, result['score'])

    def test_singleton_exit_does_not_receive_detour_penalty(self):
        result = quality({'a': [('a', 'P', (0, 0))]}, [('a', [((0, 0), (0, 20))])])
        self.assertEqual(0, result['detour'])
        self.assertIsNone(result['per_net']['a']['detour_factor'])

    def test_steiner_tree_is_compared_with_hpwl_not_terminal_mst(self):
        points = {'a': [('a', 'P', (0, 0)), ('b', 'P', (100, 0)), ('c', 'P', (50, 100))]}
        result = quality(points, [('a', [((0, 0), (100, 0)), ((50, 0), (50, 100))])])
        self.assertEqual(200, result['per_net']['a']['hpwl'])
        self.assertEqual(200, result['wirelength'])
        self.assertEqual(0, result['detour'])

    def test_role_alignment_is_independent_of_global_wirelength(self):
        points = {'a': [('a', 'P', (0, 0)), ('b', 'P', (100, 0))]}
        record = {'id': 'branch', 'kind': 'output_pair', 'confidence': 'high',
                  'devices': ['a', 'b'], 'roles': {'upper': 'a', 'lower': 'b'}}
        first = quality(points, [('a', [((0, 0), (100, 0))])],
                        positions=[(0, 0), (50, 100)], structs=[record])
        second = quality(points, [('a', [((0, 0), (0, 50)), ((0, 50), (100, 50)),
                                          ((100, 50), (100, 0))])],
                         positions=[(0, 0), (50, 100)], structs=[record])
        self.assertEqual(.5, first['alignment'])
        self.assertEqual(first['alignment'], second['alignment'])
        self.assertEqual(first['dispersion'], second['dispersion'])
        record['confidence'] = 'relation'
        related = quality(points, [('a', [((0, 0), (100, 0))])],
                          positions=[(0, 0), (50, 100)], structs=[record])
        self.assertEqual(0, related['alignment'])
        self.assertEqual(0, related['dispersion'])


class RoutingCostAndDeadlineTests(unittest.TestCase):
    def test_cost_defaults_and_explicit_legacy_replay(self):
        self.assertEqual((150, 15), (RoutingOptions().crossing_cost, RoutingOptions().bend_cost))
        legacy = RoutingOptions(crossing_cost=500, bend_cost=30)
        self.assertEqual((500, 30), (legacy.crossing_cost, legacy.bend_cost))
        for name in ('crossing_cost', 'bend_cost'):
            for value in (float('inf'), float('nan'), -1, True):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    RoutingOptions(**{name: value})

    def test_expired_deadline_reports_failure_instead_of_routes(self):
        scene = Scene()
        with self.assertRaises(RoutingFailure) as caught:
            route_scene(scene, deadline=perf_counter() - 1)
        self.assertEqual('quality_time_budget', caught.exception.diagnostics['reason'])
        self.assertIn('elapsed_seconds', caught.exception.diagnostics)

    def test_active_search_obeys_a_short_real_deadline(self):
        pins = [Pin('a', i, 'P', point, point, (1, 0))
                for i, point in enumerate(((0, 0), (1200, 0)))]
        barriers = [(i, (20 * i + 10, -30 if i % 2 else -500,
                         20 * i + 16, 500 if i % 2 else 30)) for i in range(50)]
        scene = Scene(pins={'a': pins}, all_pins={'a': [pin.point for pin in pins]},
                      body_rects=barriers, fixed_segments={'a': []}, bounds=(-10, -510, 1210, 510))
        started = perf_counter()
        with self.assertRaises(RoutingFailure) as caught:
            route_scene(scene, deadline=started + .002)
        self.assertEqual('quality_time_budget', caught.exception.diagnostics['reason'])
        self.assertGreater(caught.exception.diagnostics['attempts'], 0)
        self.assertLess(perf_counter() - started, 1)

    def test_future_deadline_preserves_valid_result(self):
        pins = [Pin('a', i, 'P', point, point, (1, 0))
                for i, point in enumerate(((0, 0), (100, 0)))]
        scene = Scene(pins={'a': pins}, all_pins={'a': [pin.point for pin in pins]},
                      fixed_segments={'a': []}, bounds=(-10, -10, 110, 10))
        routes, _ = route_scene(scene, deadline=perf_counter() + 10)
        self.assertEqual([], analyze_routes(routes, scene)['violations'])


if __name__ == '__main__':
    unittest.main()
