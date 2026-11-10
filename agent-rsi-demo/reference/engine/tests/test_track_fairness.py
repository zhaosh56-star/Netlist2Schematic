"""Fair track proposals with actual geometry and independent final checks."""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import route_tracks
import simplify_routes as shortcuts
from routing_rules import Pin, Scene, analyze_routes, canonical_topology
from sa_routing import _verify
from test_route_track_compaction import track_case


def _edge(a, b):
    return tuple(sorted((tuple(a), tuple(b))))


def paired_cases(*, foreign_first=False):
    """Two independent protected chains share one authoritative scene."""
    first = track_case(foreign=foreign_first)
    second = track_case(shift=(600, 0), rename=True)
    positions = []
    scene = Scene(options=first[5].options)
    nets, index, routed = {}, {}, []
    for fixture in (first, second):
        offset = len(positions)
        positions.extend(fixture[2].pin_world(component, 'P')
                         for component in range(len(fixture[3])))
        source_scene = fixture[5]
        for net, pins in source_scene.pins.items():
            scene.pins[net] = [Pin(pin.net, pin.component + offset, pin.pin,
                                   pin.point, pin.escape, pin.direction) for pin in pins]
        scene.all_pins.update(deepcopy(source_scene.all_pins))
        scene.fixed_segments.update(deepcopy(source_scene.fixed_segments))
        scene.body_rects.extend((component + offset, rect)
                                for component, rect in source_scene.body_rects)
        scene.marker_rects.extend(source_scene.marker_rects)
        nets.update(deepcopy(fixture[1]))
        index.update({name: component + offset for name, component in fixture[3].items()})
        routed.extend(deepcopy(fixture[4][0]))
    placement = SimpleNamespace(comps=[], pin_world=lambda component, pin: positions[component])
    checked, metrics, _ = _verify(first[0], nets, placement, index, routed, scene)
    return first[0], nets, placement, index, (checked, metrics, first[4][2]), scene


def proposals(fixture):
    return route_tracks.track_compaction_candidates(
        fixture[5], fixture[4][0], shortcuts._chains(fixture[5], fixture[4][0]))


def validate_proposal(fixture, proposal):
    trial = [(net, proposal['segments'] if net == proposal['net'] else segments)
             for net, segments in fixture[4][0]]
    return _verify(*fixture[:4], trial, fixture[5])


class TrackChainFairnessTests(unittest.TestCase):
    def test_two_chains_each_get_an_early_proposal_before_one_is_exhausted(self):
        fixture = paired_cases()
        original = deepcopy(fixture[4])
        generated = iter(proposals(fixture))
        first, second = next(generated), next(generated)
        self.assertNotEqual(first['net'], second['net'])
        self.assertEqual(set(fixture[1]), {first['net'], second['net']})
        for proposal in (first, second):
            _, metrics, _ = validate_proposal(fixture, proposal)
            self.assertLess(metrics['wirelength'], fixture[4][1]['wirelength'])
            self.assertLessEqual(metrics['bends'], fixture[4][1]['bends'])
            self.assertTrue(shortcuts._covers_fixed(
                fixture[5], proposal['net'], proposal['segments']))
        self.assertEqual(original, fixture[4])

    def test_a_rejected_raw_slot_yields_to_another_chains_valid_move(self):
        fixture = paired_cases(foreign_first=True)
        # The first chain's shortest raw track violates foreign-net spacing.
        # Rejecting it must consume that chain's turn rather than searching
        # through every other candidate before the independent second chain.
        proposal = next(proposals(fixture))
        self.assertEqual(list(fixture[1])[-1], proposal['net'])
        _, metrics, _ = validate_proposal(fixture, proposal)
        self.assertLess(metrics['wirelength'], fixture[4][1]['wirelength'])

    def test_coordinate_transform_and_renaming_do_not_prevent_safe_candidates(self):
        for turns, mirrored in ((0, False), (1, True), (2, False), (3, True)):
            with self.subTest(turns=turns, mirrored=mirrored):
                fixture = track_case(turns=turns, mirror=mirrored, shift=(137, -53),
                                     rename=True, subdivide=True, reverse=True)
                generated = list(proposals(fixture))
                self.assertTrue(generated)
                for proposal in generated:
                    _, metrics, _ = validate_proposal(fixture, proposal)
                    self.assertLess(metrics['wirelength'], fixture[4][1]['wirelength'])
                    self.assertLessEqual(metrics['bends'], fixture[4][1]['bends'])
                    self.assertTrue(shortcuts._covers_fixed(
                        fixture[5], proposal['net'], proposal['segments']))

    def test_foreign_parallel_clearance_is_screened_but_legal_crossings_survive(self):
        fixture = track_case(foreign=True)
        generated = list(proposals(fixture))
        self.assertTrue(generated)
        crossings = []
        for proposal in generated:
            # This scene has shorter tracks only 8--10 units from foreign
            # endpoints. They must be removed before full-net proposals are
            # yielded, while a straight crossing in a quiet zone is allowed.
            _, metrics, _ = validate_proposal(fixture, proposal)
            crossings.append(metrics['crossings'])
        self.assertIn(0, crossings)
        self.assertIn(1, crossings)

    def test_crossing_a_foreign_fixed_escape_is_screened_before_yield(self):
        fixture = track_case(foreign=True)
        scene = fixture[5]
        name = 'unrelated_wire'
        original = scene.pins[name][0]
        reserved = Pin(name, original.component, original.pin,
                       original.point, (160, -40), (0, 1))
        scene.pins[name][0] = reserved
        scene.fixed_segments[name] = [reserved.stub]
        _verify(*fixture[:4], fixture[4][0], scene)
        generated = list(proposals(fixture))
        self.assertTrue(generated)
        for proposal in generated:
            # Full validation checks both ordinary foreign wires and the more
            # restrictive reserved corridor belonging to the original pin.
            validate_proposal(fixture, proposal)


class FinalAuthorityTests(unittest.TestCase):
    def run_bad_candidate(self, fixture, candidate):
        with ExitStack() as stack:
            stack.enter_context(patch('simplify_routes.build_scene', return_value=fixture[5]))
            for name in ('_shortcut_candidates', 'reattachment_candidates',
                         '_three_segment_candidates'):
                stack.enter_context(patch('simplify_routes.' + name, return_value=iter(())))
            stack.enter_context(patch('simplify_routes._track_compaction_candidates',
                side_effect=lambda *args: iter([{'net': fixture[6], 'segments': candidate}])))
            stack.enter_context(patch('orthogonal_router._find_path',
                                     side_effect=AssertionError('simplification called A*')))
            result = shortcuts.simplify_routes(*fixture[:4], fixture[4])
        _, metrics, _ = _verify(*fixture[:4], result[0], fixture[5])
        self.assertEqual(metrics, result[1])
        self.assertEqual(fixture[4][0], result[0])
        self.assertEqual(0, result[3]['candidate_counts']['accepted'])
        return result

    def test_fast_screen_does_not_replace_terminal_connectivity_validation(self):
        fixture = list(track_case())
        net = fixture[6]
        scene = fixture[5]
        positions = [pin.point for pin in scene.pins[net]] + [(100, -120)]
        pin = Pin(net, 2, 'P', positions[2], (100, -100), (0, 1))
        scene.pins[net].append(pin)
        scene.all_pins[net].append(pin.point)
        scene.fixed_segments[net].append(pin.stub)
        fixture[1][net].append(('third_pin', 'P'))
        fixture[3]['third_pin'] = 2
        fixture[2].pin_world = lambda component, port: positions[component]
        wire = dict(fixture[4][0])[net] + [pin.stub, (pin.escape, (100, -80))]
        checked, metrics, _ = _verify(*fixture[:4], [(net, wire)], scene)
        fixture[4] = (checked, metrics, fixture[4][2])
        fixture = tuple(fixture)
        anchors = shortcuts._anchors(fixture[5], net)
        topology = canonical_topology(dict(fixture[4][0])[net], pins=anchors)
        connector = _edge((100, -100), (100, -80))
        self.assertIn(connector, {_edge(a, b) for a, b in topology['segments']})
        disconnected = [segment for segment in topology['segments'] if _edge(*segment) != connector]
        self.assertTrue(shortcuts._covers_fixed(fixture[5], net, disconnected))
        result = self.run_bad_candidate(fixture, disconnected)
        self.assertGreater(result[3]['operations']['track_compaction']['geometry_rejected'], 0)

    def test_fast_screen_does_not_replace_the_no_new_cycle_guard(self):
        fixture = track_case()
        proposal = next(proposals(fixture))
        square = [(1000, 1000), (1002, 1000), (1002, 1002), (1000, 1002), (1000, 1000)]
        cycle = proposal['segments'] + list(zip(square, square[1:]))
        self.assertLess(shortcuts._wire_metrics(cycle)[0], fixture[4][1]['wirelength'])
        result = self.run_bad_candidate(fixture, cycle)
        self.assertGreater(result[3]['operations']['track_compaction']['cycle_rejected'], 0)


class ForeignScreenTests(unittest.TestCase):
    def test_overlap_and_insufficient_parallel_clearance_are_filtered(self):
        scene = Scene()
        occupied = {'foreign': [((-100, 0), (100, 0))]}
        for gap in (0, 2, 9):
            with self.subTest(gap=gap):
                self.assertFalse(route_tracks._foreign_clear(
                    scene, 'signal', [((-20, gap), (20, gap))], occupied))
        self.assertTrue(route_tracks._foreign_clear(
            scene, 'signal', [((-20, 10), (20, 10))], occupied))

    def test_normal_corner_ten_units_away_is_not_treated_as_an_astar_turn(self):
        scene = Scene()
        occupied = {'foreign': [((-100, 0), (100, 0))]}
        connector = [((-20, 10), (0, 10)), ((0, 10), (0, 30))]
        # A*'s conservative vertex rule is intentionally stronger than the
        # final parallel-clearance rule. It must not be applied blindly here.
        self.assertFalse(scene.vertex_check('signal', (0, 10), occupied)[0])
        self.assertTrue(route_tracks._foreign_clear(scene, 'signal', connector, occupied))
        self.assertEqual([], analyze_routes([('signal', connector), *occupied.items()], scene)['violations'])

    def test_junction_quiet_zone_and_original_pin_contacts_still_filter(self):
        scene = Scene()
        occupied = {'foreign': [((-100, 0), (100, 0)), ((0, 0), (0, -80))]}
        self.assertFalse(route_tracks._foreign_clear(
            scene, 'signal', [((-30, 10), (30, 10))], occupied))
        pin_scene = Scene(all_pins={'foreign': [(0, 0)]})
        self.assertFalse(route_tracks._foreign_clear(
            pin_scene, 'signal', [((0, -50), (0, 50))], occupied))

    def test_crossing_a_reserved_foreign_fixed_segment_is_filtered(self):
        scene = Scene(fixed_segments={'foreign': [((-20, 0), (20, 0))]})
        occupied = {'foreign': [((-100, 0), (100, 0))]}
        self.assertFalse(route_tracks._foreign_clear(
            scene, 'signal', [((0, -50), (0, 50))], occupied))

    def test_legal_crossing_and_artificial_subdivisions_are_not_filtered(self):
        scene = Scene()
        for foreign in ([((-100, 0), (100, 0))],
                        [((-100, 0), (0, 0)), ((0, 0), (100, 0))]):
            for connector in ([((0, -50), (0, 50))],
                              [((0, -50), (0, 0)), ((0, 0), (0, 50))]):
                with self.subTest(foreign=foreign, connector=connector):
                    occupied = {'foreign': foreign}
                    self.assertTrue(route_tracks._foreign_clear(
                        scene, 'signal', connector, scene.prepare_occupied(occupied)))
                    analysis = analyze_routes([('signal', connector), *occupied.items()], scene)
                    self.assertEqual([], analysis['violations'])
                    self.assertEqual(1, analysis['crossings'])

    def test_same_net_trunk_and_junction_reuse_is_not_filtered(self):
        scene = Scene(all_pins={'signal': [(0, 0)]},
                      fixed_segments={'signal': [((-10, 0), (0, 0))]})
        occupied = {'signal': [((-100, 0), (100, 0)), ((0, -100), (0, 0))],
                    'foreign': [((200, -100), (200, 100))]}
        for connector in ([((-20, 0), (20, 0))],
                          [((0, -50), (0, 0)), ((0, 0), (50, 0))]):
            with self.subTest(connector=connector):
                self.assertTrue(route_tracks._foreign_clear(
                    scene, 'signal', connector, occupied))


if __name__ == '__main__':
    unittest.main()
