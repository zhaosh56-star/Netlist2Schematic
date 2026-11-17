"""Signal wires may enter supply-marker bounds; electrical guards remain."""
from copy import deepcopy
from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from netlist2schematic.plugins.net_labels.net_label_validation import lead_error, text_error
from netlist2schematic.plugins.preflight import placement_preflight
from netlist2schematic.plugins.symmetry.symmetry_layout import _static_safe
from netlist2schematic.postprocess.route_patterns import _body_clear
from netlist2schematic.recovery import local_rescue
from netlist2schematic.routing.orthogonal_router import route_scene
from netlist2schematic.routing.routing_rules import (Pin, Scene, analyze_routes,
    canonical_topology, static_shape_conflicts)


def fixture(direction=(1, 0), shift=(0, 0), net='signal', supply='vdd'):
    def transform(point):
        x, y = point
        dx, dy = direction
        return x*dx-y*dy+shift[0], x*dy+y*dx+shift[1]
    pins = [Pin(net, 0, 'P', transform((0, 0)), transform((20, 0)), direction),
            Pin(net, 1, 'P', transform((100, 0)), transform((80, 0)),
                (-direction[0], -direction[1]))]
    corners = [transform((x, y)) for x in (10, 90) for y in (-10, 10)]
    marker = (min(p[0] for p in corners), min(p[1] for p in corners),
              max(p[0] for p in corners), max(p[1] for p in corners))
    scene = Scene(pins={net: pins}, all_pins={net: [p.point for p in pins]},
        fixed_segments={net: [p.stub for p in pins]},
        marker_rects=[(supply, marker)], power_nets={supply}, bounds=(-250, -250, 250, 250))
    return scene, net, transform


class SignalMarkerPolicyTests(unittest.TestCase):
    def test_edge_and_final_geometry_allow_signal_through_supply_marker(self):
        scene, net, transform = fixture()
        a, b = transform((0, 0)), transform((100, 0))
        self.assertEqual((True, 0, set()), scene.edge_check(net, a, b, {}))
        self.assertEqual([], analyze_routes([(net, [(a, b)])], scene)['violations'])

    def test_astar_routes_through_marker_including_both_escape_nodes(self):
        scene, net, transform = fixture()
        before = deepcopy(scene)
        routed, _ = route_scene(scene)
        self.assertEqual([((0, 0), (100, 0))], canonical_topology(dict(routed)[net])['runs'])
        self.assertEqual([], analyze_routes(routed, scene)['violations'])
        self.assertEqual(before, scene)

    def test_rotation_translation_and_renaming_keep_acceptance(self):
        for direction in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            with self.subTest(direction=direction):
                scene, net, transform = fixture(direction, (41, -23), 'renamed_signal', 'vss')
                routed, _ = route_scene(scene)
                runs = canonical_topology(dict(routed)[net])['runs']
                self.assertEqual(1, len(runs))
                self.assertEqual({transform((0, 0)), transform((100, 0))}, set(runs[0]))
                self.assertEqual([], analyze_routes(routed, scene)['violations'])

    def test_rescue_can_extend_and_attach_inside_marker_bounds(self):
        scene, net, transform = fixture()
        occupied = {name: list(lines) for name, lines in scene.fixed_segments.items()}
        deadline = perf_counter()+5
        anchors, blockers = local_rescue._anchors(scene, net, scene.pins[net][0], occupied, deadline)
        self.assertEqual([(22, 0), (24, 0), (26, 0), (28, 0)], anchors)
        self.assertFalse(blockers)
        okay, blockers = local_rescue._safe_goals(scene, net, [((40, 0), (60, 0))],
                                                occupied, 150, deadline)
        self.assertTrue(okay)
        self.assertFalse(blockers)

    def test_static_preflight_does_not_count_signal_marker_faults(self):
        scene, net, _ = fixture()
        scene.power_nets.clear()
        placement = SimpleNamespace()
        with patch.object(placement_preflight, 'build_scene', return_value=scene), \
                patch('netlist2schematic.routing.routing_rules.build_scene', return_value=scene):
            faults = placement_preflight._static_faults(None, placement, {}, {}, scene.options, None)
            self.assertEqual(0, faults['count'])
            self.assertEqual(set(), faults['hard'])
            self.assertTrue(_static_safe(None, placement, {}, {}, scene.options, None))

    def test_three_segment_screen_allows_marker_but_still_rejects_body(self):
        scene, net, _ = fixture()
        lines = [((0, 0), (100, 0))]
        self.assertTrue(_body_clear(scene, net, lines, None))
        scene.body_rects.append((99, (40, -10, 60, 10)))
        self.assertFalse(_body_clear(scene, net, lines, None))

    def test_label_leader_uses_same_wire_rule_but_text_still_avoids_marker(self):
        scene = Scene(marker_rects=[('vdd', (16, 8, 24, 16))])
        routes = {'signal': [((0, 0), (100, 0))]}
        self.assertIsNone(lead_error(scene, 'signal', (20, 0), (20, 24), routes))
        self.assertEqual('label_body', text_error(scene, (16, 8, 24, 16), routes))

    def test_real_power_wire_remains_protected_inside_marker_bounds(self):
        scene, net, _ = fixture()
        scene.fixed_segments['vdd'] = [((50, -40), (50, 40))]
        okay, _, blockers = scene.edge_check(net, (0, 0), (100, 0), {})
        self.assertFalse(okay)
        self.assertIn('vdd', blockers)
        violations = analyze_routes([(net, [((0, 0), (100, 0))])], scene)['violations']
        self.assertTrue(any(item['kind'] == 'fixed_stub_clearance' for item in violations))

    def test_foreign_terminal_and_component_body_remain_protected(self):
        for kind in ('pin', 'body'):
            with self.subTest(kind=kind):
                scene, net, _ = fixture()
                if kind == 'pin':
                    scene.all_pins['foreign'] = [(50, 0)]
                    occupied = {'foreign': [((50, 0), (50, 40))]}
                else:
                    scene.body_rects.append((99, (40, -10, 60, 10)))
                    occupied = {}
                self.assertFalse(scene.edge_check(net, (0, 0), (100, 0), occupied)[0])
                routed = [(net, [((0, 0), (100, 0))]), *occupied.items()]
                self.assertTrue(analyze_routes(routed, scene)['violations'])

    def test_power_to_marker_and_marker_to_device_checks_remain(self):
        scene, net, _ = fixture()
        self.assertFalse(scene.edge_check('vdd', (40, 0), (60, 0), {})[0])
        scene.body_rects = [(99, (40, -5, 60, 5))]
        self.assertIn('marker_body_halo', {fault['kind'] for _, fault in static_shape_conflicts(scene)})


if __name__ == '__main__':
    unittest.main()
