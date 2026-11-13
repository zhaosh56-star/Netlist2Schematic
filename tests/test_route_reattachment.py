"""Geometry-backed regressions for relocating a terminal branch on its own tree."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from netlist2schematic.routing.routing_rules import (Pin, RoutingOptions, Scene, canonical_topology,
                           intersection, on_segment)
from netlist2schematic.routing.sa_routing import RoutingValidationError, _verify
from netlist2schematic.postprocess import simplify_routes as shortcuts


def tree_case(*, turns=0, mirror=False, shift=(0, 0), cut=False, reverse=False,
              marker=False, foreign=False, shared_terminal=False, loop=False, rename=False):
    """Three ports, two blocked old-endpoint Ls, and a safe nearer tree point.

    Every obstacle and original terminal is explicit. No saved output, netlist
    instance name or schematic resource is used as an expected-route oracle.
    """
    def vector(point):
        x, y = point
        if mirror:
            x = -x
        for _ in range(turns):
            x, y = -y, x
        return x, y

    def transform(point):
        x, y = vector(point)
        return x+shift[0], y+shift[1]

    names = ['device_713', 'device_29', 'device_461'] if not rename else ['alpha', 'gamma', 'beta']
    points = [(0, 0), (0, 120), (200, 120)]
    escapes = [(0, 20), (0, 100), (180, 120)]
    directions = [(0, 1), (0, -1), (-1, 0)]
    net = 'unrelated_signal_508' if not rename else 'renamed_signal'
    segments = [((0, 0), (0, 120)), ((0, 40), (-40, 40)),
                ((-40, 40), (-40, 260)), ((-40, 260), (180, 260)),
                ((180, 260), (180, 120)), ((180, 120), (200, 120))]
    if shared_terminal:
        names.append('device_808')
        points.append((180, 180))
        escapes.append((180, 160))
        directions.append((0, -1))
    if loop:
        segments.extend([((0, 40), (0, 200)), ((0, 200), (180, 200))])
    positions = list(map(transform, points))
    pins = {net: [Pin(net, i, 'P', positions[i], transform(escapes[i]),
                      vector(directions[i])) for i in range(len(points))]}
    nets = {net: [(name, 'P') for name in names]}
    index = {name: i for i, name in enumerate(names)}
    transformed = [(transform(a), transform(b)) for a, b in segments]
    if cut:
        transformed = [part for a, b in transformed
                       for middle in [((a[0]+b[0])/2, (a[1]+b[1])/2)]
                       for part in ((a, middle), (middle, b))]
    if reverse:
        transformed = [(b, a) for a, b in reversed(transformed)]
        nets[net].reverse()
    routes = [(net, transformed)]
    if foreign:
        guard = 'independent_signal_937'
        offset = len(positions)
        positions.extend(map(transform, ((90, 60), (90, 140))))
        pins[guard] = [Pin(guard, i, 'P', positions[i], positions[i], vector((0, 1)))
                       for i in (offset, offset+1)]
        nets[guard] = [('guard_17', 'P'), ('guard_86', 'P')]
        index.update(guard_17=offset, guard_86=offset+1)
        routes.append((guard, [(positions[offset], positions[offset+1])]))

    def rectangle(box):
        corners = [transform((x, y)) for x in (box[0], box[2])
                   for y in (box[1], box[3])]
        return (min(p[0] for p in corners), min(p[1] for p in corners),
                max(p[0] for p in corners), max(p[1] for p in corners))

    # One obstruction blocks each old-endpoint L. They do not occupy the
    # new horizontal approach at y=100 or any original route interval.
    bodies = [(1001, rectangle((40, 110, 60, 130)))]
    top = rectangle((60, 30, 100, 50))
    markers = [('power-marker', top)] if marker else []
    if not marker:
        bodies.append((1002, top))
    scene = Scene(options=RoutingOptions(), pins=pins,
                  all_pins={name: [pin.point for pin in members]
                            for name, members in pins.items()},
                  fixed_segments={name: [pin.stub for pin in members
                                         if pin.point != pin.escape]
                                  for name, members in pins.items()},
                  body_rects=bodies, marker_rects=markers)
    placement = SimpleNamespace(comps=[], pin_world=lambda i, pin: positions[i])

    def crossing(a, b, c, d):
        point = intersection(a, b, c, d)
        return point is not None and point not in (a, b, c, d)

    par = SimpleNamespace(power_marker_sym=lambda name: None, GEO_DOC={}, MARGIN=12,
                          seg_cross=crossing, seg_overlap_len=lambda *args: 0,
                          seg_parallel_prox=lambda *args, **kwargs: 0,
                          seg_hits_rect=lambda *args: False)
    checked, metrics, _ = _verify(par, nets, placement, index, routes, scene)
    result = checked, metrics, {'valid': True, 'router': 'original-router',
                                'search': {'accepted_before_cleanup': True}}
    return par, nets, placement, index, result, scene, net


def simplify(case):
    with patch('netlist2schematic.postprocess.simplify_routes.build_scene', return_value=case[5]), \
            patch('netlist2schematic.routing.orthogonal_router._find_path', side_effect=AssertionError('A*')):
        return shortcuts.simplify_routes(*case[:4], case[4], seconds=5)


class BranchReattachmentTests(unittest.TestCase):
    def test_actual_device_and_net_renaming_does_not_change_reconnection(self):
        plain = tree_case()
        renamed = tree_case(rename=True, reverse=True)
        first, second = simplify(plain), simplify(renamed)
        self.assert_safe(plain, first)
        self.assert_safe(renamed, second)
        self.assertEqual(first[1], second[1])
        self.assertEqual(canonical_topology(first[0][0][1])['runs'],
                         canonical_topology(second[0][0][1])['runs'])

    def test_connector_stops_at_first_tree_contact_instead_of_closing_a_loop(self):
        from netlist2schematic.postprocess.route_reattachment import _first_connection
        retained = [((40, -20), (40, 100)), ((0, 100), (40, 100))]
        connector, point = _first_connection(((100, 0), (0, 0), (0, 100)), retained, None)
        self.assertEqual((40, 0), point)
        self.assertEqual([((100, 0), (40, 0))], connector)
        self.assertEqual(0, shortcuts._cycle_rank(retained + connector))
        # Collinear contact also ends at the first edge entry in either direction.
        for path, expected in ((((100, 0), (0, 0)), (60, 0)),
                               (((0, 0), (100, 0)), (40, 0))):
            connector, point = _first_connection(path, [((40, 0), (60, 0))], None)
            self.assertEqual(expected, point)
            self.assertEqual([(path[0], expected)], connector)

    def assert_safe(self, case, result):
        routes, metrics, validation, _ = result
        _, actual, _ = _verify(*case[:4], routes, case[5])
        self.assertTrue(validation['valid'])
        self.assertEqual(actual, metrics)
        self.assertLessEqual(metrics['wirelength'], case[4][1]['wirelength'])
        self.assertLessEqual(metrics['bends'], case[4][1]['bends'])
        self.assertLessEqual(metrics['crossings'], case[4][1]['crossings'])
        by_net = dict(routes)
        for net in case[5].pins:
            self.assertTrue(shortcuts._covers_fixed(case[5], net, by_net[net]))
            self.assertLessEqual(shortcuts._cycle_rank(by_net[net]),
                                 shortcuts._cycle_rank(dict(case[4][0])[net]))
        return routes, metrics

    def test_new_tree_attachment_succeeds_when_both_old_endpoint_ls_are_blocked(self):
        case = tree_case()
        old = deepcopy(case[4])
        changed = dict(old[0])
        paths = shortcuts._chains(case[5], [(case[6], changed[case[6]])])
        longest = max(paths, key=lambda item: item['length'])
        removed = {shortcuts._edge(a, b)
                   for a, b in zip(longest['path'], longest['path'][1:])}
        base = [segment for segment in longest['segments']
                if shortcuts._edge(*segment) not in removed]
        for candidate in shortcuts._replacements(longest['path']):
            trial = [(case[6], shortcuts._trim_tails(case[5], case[6], base+list(candidate)))]
            with self.assertRaises(RoutingValidationError):
                _verify(*case[:4], trial, case[5])
        routes, metrics = self.assert_safe(case, simplify(case))
        self.assertEqual(340, metrics['wirelength'])
        self.assertEqual(2, metrics['bends'])
        self.assertEqual({'accepted_before_cleanup': True}, simplify(case)[2]['search'])
        self.assertEqual(old, case[4])

    def test_rotations_mirrors_translations_and_subdivision_keep_reconnection_general(self):
        for turns in range(4):
            for mirror in (False, True):
                with self.subTest(turns=turns, mirror=mirror):
                    case = tree_case(turns=turns, mirror=mirror, shift=(137, -53),
                                     cut=True, reverse=True)
                    routes, metrics = self.assert_safe(case, simplify(case))
                    self.assertEqual(340, metrics['wirelength'])
                    again = (*case[:4], (routes, metrics, {'valid': True}), *case[5:])
                    repeated = simplify(again)
                    self.assert_safe(again, repeated)
                    self.assertEqual(metrics, repeated[1])
                    self.assertEqual(0, repeated[3]['candidate_counts']['accepted'])

    def test_power_marker_and_bodies_are_still_checked(self):
        case = tree_case(marker=True)
        _, metrics = self.assert_safe(case, simplify(case))
        self.assertEqual(340, metrics['wirelength'])

    def test_foreign_wire_and_pin_clearance_are_preserved(self):
        case = tree_case(foreign=True)
        foreign = deepcopy(dict(case[4][0])['independent_signal_937'])
        routes, metrics = self.assert_safe(case, simplify(case))
        self.assertEqual(0, metrics['crossings'])
        self.assertEqual(foreign, dict(routes)['independent_signal_937'])

    def test_another_terminal_on_a_branch_keeps_its_pin_and_fixed_corridor(self):
        case = tree_case(shared_terminal=True)
        self.assert_safe(case, simplify(case))

    def test_original_cycle_does_not_allow_new_cycles_or_lost_terminals(self):
        case = tree_case(loop=True)
        self.assertEqual(1, shortcuts._cycle_rank(dict(case[4][0])[case[6]]))
        self.assert_safe(case, simplify(case))

    def test_disconnected_input_cannot_be_promoted_by_branch_repair(self):
        case = list(tree_case())
        routes, metrics, validation = deepcopy(case[4])
        # Remove the top real terminal's only wire while retaining the old flag.
        routes[0] = (routes[0][0], [segment for segment in routes[0][1]
                                   if not any(point == (0, 0) for point in segment)])
        case[4] = routes, metrics, validation
        with self.assertRaises(RoutingValidationError):
            simplify(case)


if __name__ == '__main__':
    unittest.main()
