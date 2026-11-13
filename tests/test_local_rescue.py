"""Rescue-only port continuation and transaction safety regressions."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from netlist2schematic.recovery import local_rescue
from netlist2schematic.routing.orthogonal_router import RoutingFailure, _route_net
from netlist2schematic.routing.routing_rules import Pin, RoutingOptions, Scene, analyze_routes, on_segment


def statistics():
    return {'graph_nodes': 0, 'attempts': 0, 'expanded_states': 0}


def scene_for(direction=(1, 0), *, quiet=12):
    """A root and source whose artificial ports both face a foreign straight."""
    def turn(point):
        x, y = point
        dx, dy = direction
        return x*dx-y*dy, x*dy+y*dx

    pins = [Pin('a', 0, 'P', turn((0, 0)), turn((18, 0)), direction),
            Pin('a', 1, 'P', turn((0, 100)), turn((18, 100)), direction)]
    # Infinite-enough straight crossing; endpoints stay far from either port.
    foreign = [(turn((28, -70)), turn((28, 170)))]
    scene = Scene(options=RoutingOptions(cross_clearance=quiet, margins=(80,),
                                        max_expansions=20000),
                  pins={'a': pins, 'b': []},
                  all_pins={'a': [pin.point for pin in pins], 'b': []},
                  fixed_segments={'a': [pin.stub for pin in pins], 'b': []},
                  bounds=(-200, -200, 200, 200))
    return scene, {'a': list(scene.fixed_segments['a']), 'b': foreign}


def report():
    return {'port_plans': 0, 'branches': []}


class PortContinuationTests(unittest.TestCase):
    def test_both_artificial_ends_are_repaired_in_all_four_directions(self):
        for direction in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            with self.subTest(direction=direction):
                scene, occupied = scene_for(direction)
                before = deepcopy(scene)
                normal, _ = _route_net(scene, 'a', occupied, 80, [], statistics())
                self.assertIsNone(normal)
                result, _, _, failure = local_rescue._route_local_net(
                    scene, 'a', occupied, 80, statistics(), perf_counter()+10, report())
                self.assertIsNone(failure)
                self.assertIsNotNone(result)
                self.assertEqual(scene, before)
                self.assertEqual([], analyze_routes([('a', result), ('b', occupied['b'])],
                                                     scene)['violations'])
                for pin in scene.pins['a']:
                    self.assertTrue(any(on_segment(pin.point, a, b) for a, b in result))

    def test_custom_quiet_zone_sets_anchor_distance_and_bounded_radius(self):
        scene, occupied = scene_for(quiet=20)
        anchors, _ = local_rescue._anchors(scene, 'a', scene.pins['a'][0],
                                           occupied, perf_counter()+10)
        self.assertEqual([(48, 0), (50, 0), (52, 0), (54, 0)], anchors)
        result, _, _, failure = local_rescue._route_local_net(
            scene, 'a', occupied, 80, statistics(), perf_counter()+10, report())
        self.assertIsNone(failure)
        self.assertEqual([], analyze_routes([('a', result), ('b', occupied['b'])],
                                             scene)['violations'])

    def test_odd_grid_keeps_exact_custom_clearance_boundary(self):
        scene, occupied = scene_for(quiet=13)
        scene.options = replace(scene.options, grid_step=3)
        anchors, _ = local_rescue._anchors(scene, 'a', scene.pins['a'][0],
                                           occupied, perf_counter()+10)
        self.assertEqual((41, 0), anchors[0])
        self.assertEqual(4, len(anchors))

    def test_goal_preflight_does_not_count_points_outside_graph_axes(self):
        scene, occupied = scene_for()
        with patch('netlist2schematic.recovery.local_rescue._axes', return_value=([0, 10], [0, 20])):
            safe, _ = local_rescue._safe_goals(scene, 'a', [((8, 0), (8, 20))],
                                               occupied, 80, perf_counter()+10)
        self.assertFalse(safe)

    def test_unrouted_fixed_stubs_are_not_completed_foreign_routes(self):
        scene, occupied = scene_for()
        scene.fixed_segments['b'] = occupied['b']
        with patch('netlist2schematic.recovery.local_rescue.analyze_routes', wraps=analyze_routes) as analyze:
            local_rescue._tree_score(scene, 'a', [((0, 0), (18, 0))], occupied,
                                      perf_counter()+10)
        self.assertNotIn('b', dict(analyze.call_args.args[0]))

    def test_real_pin_without_stub_is_never_extended(self):
        scene, occupied = scene_for()
        scene.pins['a'] = [replace(pin, point=pin.escape) for pin in scene.pins['a']]
        scene.all_pins['a'] = [pin.point for pin in scene.pins['a']]
        scene.fixed_segments['a'] = []
        occupied['a'] = []
        result, _, _, failure = local_rescue._route_local_net(
            scene, 'a', occupied, 80, statistics(), perf_counter()+10, report())
        self.assertIsNone(result)
        self.assertEqual('no_safe_goal', failure['reason'])

    def test_goal_must_be_an_original_degree_one_artificial_leaf(self):
        scene, occupied = scene_for()
        pin = scene.pins['a'][0]
        branch = [pin.stub, (pin.escape, (18, -30)), (pin.escape, (18, 30))]
        choices, _ = local_rescue._goal_choices(scene, 'a', branch, occupied, 80,
                                                perf_counter()+10)
        self.assertFalse(any(choice is not None and choice[0] == pin.escape
                             for choice in choices))
        bent = [pin.stub, (pin.escape, (18, -30))]
        choices, _ = local_rescue._goal_choices(scene, 'a', bent, occupied, 80,
                                                perf_counter()+10)
        self.assertFalse(any(choice is not None and choice[0] == pin.escape
                             for choice in choices))

    def test_extension_still_respects_fixed_foreign_stub_and_body(self):
        for kind in ('fixed', 'body'):
            with self.subTest(kind=kind):
                scene, occupied = scene_for()
                if kind == 'fixed':
                    scene.fixed_segments['b'] = occupied['b']
                else:
                    scene.body_rects = [(7, (22, -30, 30, 30))]
                anchors, blockers = local_rescue._anchors(scene, 'a', scene.pins['a'][0],
                                                          occupied, perf_counter()+10)
                self.assertEqual([], anchors)
                self.assertTrue(blockers)

    def test_no_safe_goal_does_not_launch_a_star(self):
        scene, occupied = scene_for()
        scene.body_rects = [(0, (-10, -10, 16, 10)), (7, (19, -1, 150, 1))]
        with patch('netlist2schematic.recovery.local_rescue._find_path') as search:
            result, _, _, failure = local_rescue._route_local_net(
                scene, 'a', occupied, 80, statistics(), perf_counter()+10, report())
        self.assertIsNone(result)
        self.assertEqual('no_safe_goal', failure['reason'])
        search.assert_not_called()

    def test_actual_whole_tree_score_counts_added_prefixes(self):
        scene, occupied = scene_for()
        tree = [((0, 0), (40, 0)), ((40, 0), (40, 100)), ((0, 100), (40, 100))]
        score, _, violations = local_rescue._tree_score(scene, 'a', tree, occupied,
                                                        perf_counter()+10)
        self.assertEqual([], violations)
        self.assertEqual(180 + 2*15 + 2*150, score)

    def test_unused_goal_extension_tail_is_trimmed(self):
        scene, occupied = scene_for()
        result, _, _, failure = local_rescue._route_local_net(
            scene, 'a', occupied, 80, statistics(), perf_counter()+10, report())
        self.assertIsNone(failure)
        topology = local_rescue.canonical_topology(result,
            pins=scene.all_pins['a'])
        leaves = {point for point, neighbors in topology['adjacency'].items()
                  if len(neighbors) == 1}
        self.assertEqual(set(scene.all_pins['a']), leaves)

    def test_pin_order_and_renamed_nets_keep_equivalent_geometry(self):
        scene, occupied = scene_for()
        first, _, _, failure = local_rescue._route_local_net(
            scene, 'a', occupied, 80, statistics(), perf_counter()+10, report())
        self.assertIsNone(failure)
        names = {'a': 'arbitrary-signal', 'b': 'unrelated-wire'}
        renamed = deepcopy(scene)
        renamed.pins = {names[net]: [replace(pin, net=names[net], pin='terminal')
                                   for pin in reversed(pins)] for net, pins in scene.pins.items()}
        renamed.all_pins = {names[net]: list(reversed(points))
                            for net, points in scene.all_pins.items()}
        renamed.fixed_segments = {names[net]: list(reversed(segments))
                                  for net, segments in scene.fixed_segments.items()}
        other = {names[net]: list(reversed(segments)) for net, segments in occupied.items()}
        second, _, _, failure = local_rescue._route_local_net(
            renamed, names['a'], other, 80, statistics(), perf_counter()+10, report())
        self.assertIsNone(failure)
        self.assertEqual(first, second)


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        pins = {'a': [Pin('a', 0, 'P', (0, 0), (0, 0), (1, 0))],
                'b': [Pin('b', 1, 'P', (100, 0), (100, 0), (1, 0))],
                'c': [Pin('c', 2, 'P', (200, 0), (200, 0), (1, 0))]}
        self.scene = Scene(pins=pins, all_pins={name: [p.point for p in ps]
                                              for name, ps in pins.items()},
                           fixed_segments={name: [] for name in pins})
        self.context = (SimpleNamespace(cell_of=[(0, 0), (0, 1), (0, 2)],
                                        orient=[0, 1, 0], row_y=[4], column_x=[4, 104, 204]),
                        [], {}, {}, {'candidate_id': 2, 'candidate_seed': 7})
        self.checkpoint = {'scene': self.scene, 'completed': {'b': [((100, 0), (100, 10))]},
                           'failed_net': 'a', 'pending': ['c'], 'margin': 150,
                           'branch': None, 'options': self.scene.options}

    def patches(self):
        return (patch('netlist2schematic.recovery.local_rescue.load_local', return_value=SimpleNamespace()),
                patch('netlist2schematic.recovery.local_rescue.build_scene', return_value=deepcopy(self.scene)),
                patch('netlist2schematic.recovery.local_rescue._verify', side_effect=lambda *args, **kw:
                      (args[4], {'wirelength': 1}, {'power_connections_checked': 0})),
                patch('netlist2schematic.recovery.local_rescue.evaluate_quality', return_value={'score': 1}))

    def test_transaction_rolls_back_newly_completed_net_and_uses_new_queries(self):
        seen = []
        attempts = Counter()

        def route(scene, net, occupied, *args):
            seen.append((net, deepcopy(occupied)))
            attempts[net] += 1
            if net == 'c' and attempts[net] == 1:
                return None, Counter({'b': 3}), Counter({'b': 1}), {'reason': 'no_path'}
            return [((0, 100*len(seen)), (20, 100*len(seen)))], Counter(), Counter(), None

        original = deepcopy(self.context)
        checkpoint = deepcopy(self.checkpoint)
        p1, p2, p3, p4 = self.patches()
        with p1, p2, p3, p4, patch('netlist2schematic.recovery.local_rescue._route_local_net', side_effect=route):
            result = local_rescue.rescue_routing(self.context, self.checkpoint)
        self.assertEqual(original, self.context)
        self.assertEqual(checkpoint, self.checkpoint)
        self.assertEqual('local_rescue', result[4]['routing_stage'])
        transactions = result[4]['local_rescue']['transactions']
        self.assertEqual(['b'], transactions[1]['removed'])
        # The second transaction begins with c: neither the old b wire nor a's
        # previously staged wire leaks into its brand-new occupancy.
        self.assertEqual('c', seen[2][0])
        self.assertEqual([], seen[2][1]['b'])
        self.assertEqual([], seen[2][1]['a'])
        json.dumps(result[4], allow_nan=False)

    def test_frozen_scene_mismatch_is_input_error_not_recovery_success(self):
        mismatched = deepcopy(self.scene)
        mismatched.bounds = (-1, -1, 101, 101)
        with patch('netlist2schematic.recovery.local_rescue.load_local'), \
                patch('netlist2schematic.recovery.local_rescue.build_scene', return_value=mismatched):
            with self.assertRaisesRegex(ValueError, 'does not match'):
                local_rescue.rescue_routing(self.context, self.checkpoint)

    def test_failed_original_net_is_not_retained_as_an_obstacle_in_next_transaction(self):
        calls = []

        def transaction(scene, frozen, order, *args):
            calls.append((deepcopy(frozen), list(order)))
            if len(calls) == 1:
                return None, {'failed_net': 'a', 'branch': None,
                              'blocking_nets': {'b': 1}, 'direct_blockers': {'b': 1}}
            if len(calls) == 2:
                return None, {'failed_net': 'b', 'branch': None,
                              'blocking_nets': {}, 'direct_blockers': {}}
            self.assertNotIn('b', frozen)
            self.assertIn('b', order)
            return {'a': [], 'b': [], 'c': []}, None

        p1, p2, p3, p4 = self.patches()
        with p1, p2, p3, p4, \
                patch('netlist2schematic.recovery.local_rescue._transaction', side_effect=transaction), \
                patch('netlist2schematic.recovery.local_rescue._interface_count', return_value=0):
            result = local_rescue.rescue_routing(self.context, self.checkpoint)
        self.assertEqual(3, len(calls))
        self.assertEqual('b', result[4]['local_rescue']['transactions'][2]['failed_net'])
        self.assertEqual(['b'], result[4]['local_rescue']['transactions'][2]['removed'])

    def test_failed_final_validation_never_returns_partial_success(self):
        p1, p2, p3, p4 = self.patches()
        with p1, p2, p3, p4, \
                patch('netlist2schematic.recovery.local_rescue._transaction', return_value=({'a': [], 'b': [], 'c': []}, None)), \
                patch('netlist2schematic.recovery.local_rescue._verify', side_effect=RoutingFailure('bad', {'reason': 'bad'})):
            with self.assertRaises(RoutingFailure) as caught:
                local_rescue.rescue_routing(self.context, self.checkpoint)
        self.assertEqual('failed', caught.exception.diagnostics['local_rescue']['status'])
        json.dumps(caught.exception.diagnostics, allow_nan=False)

    def test_custom_options_cannot_replace_checkpoint_geometry_contract(self):
        with patch('netlist2schematic.recovery.local_rescue.load_local'), \
                patch('netlist2schematic.recovery.local_rescue.build_scene', return_value=replace(self.scene,
                      options=RoutingOptions(cross_clearance=24))):
            with self.assertRaisesRegex(ValueError, 'does not match'):
                local_rescue.rescue_routing(self.context, self.checkpoint,
                                             routing_options=RoutingOptions(cross_clearance=24))

    def test_quality_and_verification_receive_one_shared_absolute_deadline(self):
        p1, p2, p3, p4 = self.patches()
        deadlines = []

        def transaction(*args):
            deadlines.append(args[5])
            return {'a': [], 'b': [], 'c': []}, None

        with p1, p2, p3 as verify, p4, \
                patch('netlist2schematic.recovery.local_rescue._transaction', side_effect=transaction):
            local_rescue.rescue_routing(self.context, self.checkpoint, seconds=3)
        self.assertEqual(deadlines[0], verify.call_args.kwargs['deadline'])

    def test_scene_construction_budget_failure_is_reported_as_local_rescue(self):
        with patch('netlist2schematic.recovery.local_rescue.load_local'), \
                patch('netlist2schematic.recovery.local_rescue.build_scene', side_effect=RoutingFailure(
                      'scene timeout', {'reason': 'quality_time_budget', 'stage': 'scene'})):
            with self.assertRaises(RoutingFailure) as caught:
                local_rescue.rescue_routing(self.context, self.checkpoint)
        self.assertEqual('local_rescue', caught.exception.diagnostics['routing_stage'])
        self.assertEqual('quality_time_budget', caught.exception.diagnostics['reason'])
        json.dumps(caught.exception.diagnostics, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
