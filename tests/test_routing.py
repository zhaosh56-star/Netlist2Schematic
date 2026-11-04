"""Behavioral regressions for final-route geometry and source pin preservation."""
from __future__ import annotations

import math
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sa_common import load_local
from sa_routing import (final_route, measure_routes, normalize_routes,
                        normalize_segments, routing_errors)

par = load_local('place_and_route', 'place-and-route.py')


class PinPlacement:
    """Small independent pin oracle; no component bodies affect measurements."""
    comps = []

    def __init__(self, positions):
        self.positions = positions

    def pin_world(self, index, pin):
        return self.positions[index, pin]


def transform_svg_point(point, transform):
    """Evaluate the generated SVG's scale/rotation in SVG matrix order."""
    operations = re.findall(r'(scale|rotate)\(([^)]*)\)', transform)
    x, y = point
    for operation, arguments in reversed(operations):
        values = [float(value) for value in re.split(r'[ ,]+', arguments.strip())]
        if operation == 'scale':
            x *= values[0]
            y *= values[1] if len(values) > 1 else values[0]
        else:
            angle = math.radians(values[0])
            x, y = x * math.cos(angle) - y * math.sin(angle), x * math.sin(angle) + y * math.cos(angle)
    return round(x, 8), round(y, 8)


class SymbolOrientationTests(unittest.TestCase):
    def test_every_rendered_symbol_orientation_agrees_with_cached_pins(self):
        for symbol_id, geometry in par.GEO_DOC.items():
            symbol = par.effective_symbol(par.load_def(symbol_id))
            raw_pins = {pin['name']: (pin['at']['x'], pin['at']['y'])
                        for pin in symbol['pins']}
            for index, orientation in enumerate(geometry['orientations']):
                fragment = par.svg_symbol_parts(symbol, orientation['rotation'], orientation['mirror'])
                group = ET.fromstring(fragment)
                for pin in orientation['pins']:
                    with self.subTest(symbol=symbol_id, orientation=index, pin=pin['name']):
                        expected = (pin['at']['x'], pin['at']['y'])
                        actual = transform_svg_point(raw_pins[pin['name']], group.attrib['transform'])
                        self.assertEqual(expected, actual)
                        self.assertEqual(expected, par.pin_pos(symbol_id, index, pin['name']))


class RouteNormalizationTests(unittest.TestCase):
    def test_reversed_and_partially_overlapping_wires_become_unique_intervals(self):
        actual = normalize_segments([((0, 0), (10, 0)), ((8, 0), (2, 0)),
                                     ((10, 0), (0, 0)), ((4, 0), (4, 0))])
        self.assertEqual([((0, 0), (2, 0)), ((2, 0), (8, 0)), ((8, 0), (10, 0))], actual)
        self.assertEqual(actual, normalize_segments(actual))

    def test_same_net_crossing_and_t_join_are_connected(self):
        wires = normalize_routes([('n', [((0, 0), (10, 0)),
                                         ((5, -5), (5, 5)), ((8, 0), (8, 3))])])
        placement = PinPlacement({(0, 'P'): (0, 0), (1, 'P'): (5, 5), (2, 'P'): (8, 3)})
        nets = {'n': [('A', 'P'), ('B', 'P'), ('C', 'P')]}
        self.assertEqual([], routing_errors(wires, nets, placement, {'A': 0, 'B': 1, 'C': 2}))
        self.assertIn(((0, 0), (5, 0)), wires[0][1])
        self.assertIn(((5, 0), (8, 0)), wires[0][1])

    def test_diagonal_wires_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Non-orthogonal'):
            normalize_segments([((0, 0), (2, 3))])

    def test_wire_graph_connectivity_alone_does_not_prove_pin_connectivity(self):
        placement = PinPlacement({(0, 'P'): (0, 0), (1, 'P'): (10, 0)})
        errors = routing_errors([('n', [((0, 1), (10, 1))])],
                                {'n': [('A', 'P'), ('B', 'P')]}, placement, {'A': 0, 'B': 1})
        self.assertTrue(any('original pin' in error for error in errors))

    def test_singleton_pin_needs_no_artificial_wire(self):
        placement = PinPlacement({(0, 'P'): (0, 0)})
        self.assertEqual([], routing_errors([('n', [])], {'n': [('A', 'P')]}, placement, {'A': 0}))


class FinalRouteTests(unittest.TestCase):
    def setUp(self):
        self.placement = par.Placement.__new__(par.Placement)
        self.placement.comps = [
            {'id': 'A', 'symbol': 'resistor', 'nodes': {'1': 'n', '2': '0'}},
            {'id': 'B', 'symbol': 'resistor', 'nodes': {'1': 'n', '2': '0'}}]
        self.placement.cell_of = [(1, 0), (1, 2)]
        self.placement.orient = [0, 0]
        self.placement.comp_at = {(1, 0): 0, (1, 2): 1}
        self.nets = {'n': [('A', '1'), ('B', '1')], '0': [('A', '2'), ('B', '2')]}
        self.indices = {'A': 0, 'B': 1}
        self.route = [('n', [((75, 205), (75, 187)), ((75, 187), (375, 187)),
                            ((375, 187), (375, 205))])]

    def test_final_route_never_invokes_legacy_router_or_cleanup(self):
        with patch.object(par, 'route', side_effect=AssertionError('legacy route used')), \
                patch.object(par, 'spread_overlaps', side_effect=AssertionError('legacy cleanup used')), \
                patch.object(par, 'push_apart', side_effect=AssertionError('legacy cleanup used')):
            routed, metrics, validation = final_route(par, self.nets, self.placement, self.indices)
        self.assertTrue(validation['valid'])
        self.assertEqual([], routing_errors(normalize_routes(routed), {'n': self.nets['n']},
                                            self.placement, self.indices))
        self.assertEqual(0, metrics['overlap'])

    def test_metrics_describe_accepted_final_geometry(self):
        with patch('orthogonal_router.route_scene', return_value=(self.route, {})):
            routed, metrics, validation = final_route(par, self.nets, self.placement, self.indices)
        self.assertEqual(336, metrics['wirelength'])
        self.assertEqual(2, metrics['bends'])
        self.assertEqual([], validation['postprocess_rejections'])
        self.assertTrue(validation['valid'])

    def test_invalid_initial_route_is_not_reported_as_success(self):
        with patch('orthogonal_router.route_scene', return_value=([('n', [])], {})):
            with self.assertRaisesRegex(ValueError, 'failed validation'):
                final_route(par, self.nets, self.placement, self.indices)

    def test_power_nets_are_not_passed_to_router(self):
        with patch('orthogonal_router.route_scene', return_value=(self.route, {})) as route:
            final_route(par, self.nets, self.placement, self.indices)
        self.assertEqual({'n'}, set(route.call_args.args[0].pins))


if __name__ == '__main__':
    unittest.main()
