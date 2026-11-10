"""Failure-only checkpoints and opt-in path-search interfaces for local rescue."""
from __future__ import annotations

from collections import Counter
import heapq
import json
from time import perf_counter
import unittest
from unittest.mock import patch

from orthogonal_router import RoutingFailure, _axes, _find_path, route_scene
from routing_rules import Pin, RoutingOptions, Scene
from sa_routing import RoutingValidationError, final_route


def simple_scene(points, **options):
    return Scene(
        options=RoutingOptions(margins=(50,), max_rounds=0, **options),
        pins={net: [Pin(net, index, 'P', point, point, (1, 0))
                    for index, point in enumerate(coords)]
              for net, coords in points.items()},
        all_pins={net: list(coords) for net, coords in points.items()},
        fixed_segments={net: [] for net in points},
        bounds=(-100, -100, 300, 300))


def statistics():
    return {'attempts': 0, 'graph_nodes': 0, 'expanded_states': 0}


class FailureCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.scene = simple_scene({
            'a': [(0, 0), (100, 0)],
            'b': [(0, 100), (100, 100)],
            'c': [(0, 200), (100, 200)]})

    def test_failure_keeps_only_accepted_nets_and_deep_copies_state(self):
        accepted = [((0, 0), (100, 0))]
        branch = {'net': 'b', 'component': 1, 'pin': 'P',
                  'point': (100, 100), 'escape': (100, 100)}

        def attempt(scene, net, *args, _rescue_state=None, **kwargs):
            if net == 'a':
                return accepted, Counter()
            _rescue_state['branch'] = branch
            return None, Counter({'a': 1})

        with patch('orthogonal_router._route_net', side_effect=attempt):
            with self.assertRaises(RoutingFailure) as caught:
                route_scene(self.scene)
        context = caught.exception._rescue_context
        self.assertIs(self.scene, context['scene'])
        self.assertEqual({'a': accepted}, context['completed'])
        self.assertEqual('b', context['failed_net'])
        self.assertEqual(['c'], context['pending'])
        self.assertEqual(50, context['margin'])
        self.assertEqual(branch, context['branch'])
        self.assertEqual(self.scene.options, context['options'])
        self.assertIsNot(self.scene.options, context['options'])
        accepted.append(((100, 0), (100, 10)))
        branch['pin'] = 'changed'
        self.assertEqual([((0, 0), (100, 0))], context['completed']['a'])
        self.assertEqual('P', context['branch']['pin'])
        json.dumps(caught.exception.diagnostics)
        self.assertNotIn('_rescue_context', caught.exception.diagnostics)

    def test_deadline_inside_net_preserves_last_real_branch_and_accepted_nets(self):
        def attempt(scene, net, *args, _rescue_state=None, **kwargs):
            if net == 'a':
                return [((0, 0), (100, 0))], Counter()
            _rescue_state['branch'] = {'net': net, 'pin': 'current'}
            raise RoutingFailure('expired', {'reason': 'quality_time_budget'})

        with patch('orthogonal_router._route_net', side_effect=attempt):
            with self.assertRaises(RoutingFailure) as caught:
                route_scene(self.scene)
        context = caught.exception._rescue_context
        self.assertEqual({'a'}, set(context['completed']))
        self.assertEqual('b', context['failed_net'])
        self.assertEqual(['c'], context['pending'])
        self.assertEqual({'net': 'b', 'pin': 'current'}, context['branch'])

    def test_candidate_rejected_by_geometry_never_enters_completed(self):
        def attempt(scene, net, *args, **kwargs):
            # b's proposed route overlaps a; it has not been accepted.
            return [((0, 0), (100, 0))], Counter()

        with patch('orthogonal_router._route_net', side_effect=attempt):
            with self.assertRaises(RoutingFailure) as caught:
                route_scene(self.scene)
        self.assertEqual('candidate_validation', caught.exception.diagnostics['reason'])
        self.assertEqual({'a'}, set(caught.exception._rescue_context['completed']))

    def test_real_search_budget_failure_records_source_branch(self):
        scene = simple_scene({'a': [(0, 0), (100, 0)]}, max_nodes=1)
        with self.assertRaises(RoutingFailure) as caught:
            route_scene(scene)
        context = caught.exception._rescue_context
        self.assertEqual({}, context['completed'])
        self.assertEqual('a', context['failed_net'])
        self.assertEqual((100, 0), context['branch']['point'])

    def test_deadline_before_any_net_has_no_checkpoint(self):
        with self.assertRaises(RoutingFailure) as caught:
            route_scene(self.scene, deadline=perf_counter() - 1)
        self.assertIsNone(caught.exception._rescue_context)
        self.assertEqual('no_active_net',
                         caught.exception.diagnostics['rescue_checkpoint_unavailable'])

    def test_deadline_between_nets_does_not_relabel_an_accepted_net(self):
        starts = 0

        def deadline(ignored, **details):
            nonlocal starts
            if details.get('stage') == 'net_start':
                starts += 1
                if starts == 2:
                    raise RoutingFailure('expired', {'reason': 'quality_time_budget'})

        with patch('orthogonal_router._check_deadline', side_effect=deadline), \
                patch('orthogonal_router._route_net',
                      return_value=([((0, 0), (100, 0))], Counter())):
            with self.assertRaises(RoutingFailure) as caught:
                route_scene(self.scene)
        self.assertIsNone(caught.exception._rescue_context)
        self.assertEqual('no_active_net',
                         caught.exception.diagnostics['rescue_checkpoint_unavailable'])


class RescueSearchInterfaceTests(unittest.TestCase):
    def test_default_and_explicit_empty_options_keep_the_same_route_and_work(self):
        scene = simple_scene({'a': [(0, 0), (100, 0)]})
        first_stats, second_stats = statistics(), statistics()
        args = (scene, 'a', (0, 0), [((100, 0), (100, 0))], {}, 50, [])
        first, first_blockers = _find_path(*args, first_stats)
        second, second_blockers = _find_path(
            *args, second_stats, extra_points=(), initial_direction=None)
        self.assertEqual([((0, 0), (100, 0))], first)
        self.assertEqual(first, second)
        self.assertEqual(first_blockers, second_blockers)
        self.assertEqual(first_stats, second_stats)

    def test_extra_points_add_both_coordinates_for_a_safe_interface(self):
        scene = simple_scene({'a': [(0, 0), (100, 0)]})
        start, tree = (7, 9), [((100, 0), (100, 0))]
        ordinary, _ = _find_path(scene, 'a', start, tree, {}, 50, [], statistics())
        self.assertIsNone(ordinary)
        xs, ys = _axes(scene, {}, tree, 50, extra_points=(start,))
        self.assertIn(7, xs)
        self.assertIn(9, ys)
        routed, _ = _find_path(scene, 'a', start, tree, {}, 50, [], statistics(),
                               extra_points=(start,), initial_direction=(1, 0))
        self.assertIsNotNone(routed)
        self.assertEqual(start, routed[0][0])
        self.assertEqual((100, 0), routed[-1][1])

    def test_initial_direction_charges_the_first_bend_in_all_four_directions(self):
        push = heapq.heappush
        for direction in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            target = (0, 20) if direction[0] else (20, 0)
            scene = simple_scene({'a': [(0, 0), target]})
            costs = []
            for heading in (None, direction):
                with patch('orthogonal_router.heapq.heappush', wraps=push) as pushed:
                    routed, _ = _find_path(
                        scene, 'a', (0, 0), [(target, target)], {}, 50, [], statistics(),
                        initial_direction=heading)
                self.assertIsNotNone(routed)
                # The heuristic is zero only at the precise point goal.
                costs.append(min(call.args[1][1] for call in pushed.call_args_list
                                 if call.args[1][0] == call.args[1][1]))
            with self.subTest(direction=direction):
                self.assertEqual(20, costs[0])
                self.assertEqual(20 + scene.options.bend_cost, costs[1])

    def test_initial_reverse_is_forbidden_in_all_four_directions(self):
        for direction in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            target = (-20 * direction[0], -20 * direction[1])
            scene = simple_scene({'a': [(0, 0), target]})
            routed, _ = _find_path(
                scene, 'a', (0, 0), [(target, target)], {}, 50, [], statistics(),
                initial_direction=direction)
            with self.subTest(direction=direction):
                self.assertIsNotNone(routed)
                a, b = routed[0]
                self.assertGreaterEqual((b[0]-a[0])*direction[0]
                                        + (b[1]-a[1])*direction[1], 0)
                self.assertGreater(sum(abs(b[0]-a[0]) + abs(b[1]-a[1])
                                       for a, b in routed), 20)

    def test_interface_direction_keeps_start_vertex_and_body_protection(self):
        scene = simple_scene({'a': [(10, 0), (100, 0)]})
        occupied = {'foreign': [((0, -100), (0, 100))]}
        stats = statistics()
        routed, blockers = _find_path(
            scene, 'a', (10, 0), [((100, 0), (100, 0))], occupied, 50, [], stats,
            initial_direction=(1, 0))
        self.assertIsNone(routed)
        self.assertIn('foreign', blockers)
        self.assertEqual(0, stats['expanded_states'])
        scene.body_rects = [(0, (5, -5, 15, 5))]
        routed, _ = _find_path(
            scene, 'a', (10, 0), [((100, 0), (100, 0))], {}, 50, [], statistics(),
            initial_direction=(1, 0))
        self.assertIsNone(routed)

    def test_non_axis_and_zero_initial_directions_are_rejected(self):
        scene = simple_scene({'a': [(0, 0), (100, 0)]})
        for direction in ((0, 0), (1, 1), (float('nan'), 0)):
            with self.subTest(direction=direction), self.assertRaises(ValueError):
                _find_path(scene, 'a', (0, 0), [((100, 0), (100, 0))], {}, 50, [],
                           statistics(), initial_direction=direction)


class FinalVerificationCheckpointTests(unittest.TestCase):
    def test_unproven_final_networks_are_not_frozen_for_rescue(self):
        scene = simple_scene({'a': [(0, 0), (100, 0)]})
        error = RoutingValidationError('failed validation', {'structure_errors': ['a: missing pin']})
        with patch('routing_rules.build_scene', return_value=scene), \
                patch('orthogonal_router.route_scene', return_value=([('a', [])], {})), \
                patch('sa_routing._verify', side_effect=error):
            with self.assertRaises(RoutingValidationError) as caught:
                final_route(None, {}, None, {})
        self.assertIsNone(caught.exception._rescue_context)
        self.assertEqual('final_verification_failed',
                         caught.exception.diagnostics['rescue_checkpoint_unavailable'])

    def test_resource_and_malformed_route_errors_do_not_gain_rescue_context(self):
        scene = simple_scene({'a': [(0, 0), (100, 0)]})
        with patch('routing_rules.build_scene', side_effect=FileNotFoundError('symbol')):
            with self.assertRaises(FileNotFoundError) as caught:
                final_route(None, {}, None, {})
        self.assertFalse(hasattr(caught.exception, '_rescue_context'))
        with patch('routing_rules.build_scene', return_value=scene), \
                patch('orthogonal_router.route_scene', return_value=([('a', [])], {})), \
                patch('sa_routing._verify', side_effect=ValueError('malformed coordinates')):
            with self.assertRaises(ValueError) as caught:
                final_route(None, {}, None, {})
        self.assertFalse(hasattr(caught.exception, '_rescue_context'))


if __name__ == '__main__':
    unittest.main()
