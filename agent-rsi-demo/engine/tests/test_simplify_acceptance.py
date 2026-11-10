"""Self-contained acceptance cases for safe final-route simplification.

The symbols and placement below are intentionally tiny and independent of any
saved output. All cases use the production scene builder and final validator.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from routing_rules import RoutingOptions, build_scene, canonical_topology, on_segment
from sa_common import load_local
from sa_routing import RoutingValidationError, validate_final_routes


def schematic_par():
    # Borrow stable primitive measurements, while defining all fixture symbol
    # geometry here. No Analog Canvas example or generated SVG is a fixture.
    real = load_local('place_and_route', 'place-and-route.py')
    orientations = []
    for x, y, direction in ((10, 0, 'east'), (-10, 0, 'west'), (0, -10, 'north'),
                             (0, 10, 'south')):
        orientations.append({'viewBoxAABB': {'x': -10, 'y': -10, 'width': 20, 'height': 20},
                             'pins': [{'name': 'P', 'at': {'x': x, 'y': y},
                                       'direction': direction}]})
    return SimpleNamespace(GEO_DOC={
        'terminal': {'orientations': orientations},
        'block': {'orientations': [{'viewBoxAABB': {
            'x': -5, 'y': -5, 'width': 10, 'height': 10}, 'pins': []}]}},
        DIR_VEC=real.DIR_VEC, MARGIN=12, power_marker_sym=lambda net: None,
        seg_cross=real.seg_cross, seg_overlap_len=real.seg_overlap_len,
        seg_parallel_prox=real.seg_parallel_prox, seg_hits_rect=real.seg_hits_rect)


class FixturePlacement:
    def __init__(self, comps, positions, orientations):
        self.comps = comps
        self.positions = positions
        self.orient = orientations
        self.column_x = sorted({x for x, _ in positions})
        self.row_y = sorted({y for _, y in positions})
        self.cell_of = [(self.row_y.index(y), self.column_x.index(x)) for x, y in positions]
        self.comp_at = {cell: number for number, cell in enumerate(self.cell_of)}

    def pos_of(self, index):
        return self.positions[index]

    def pin_world(self, index, pin):
        x, y = self.pos_of(index)
        dx, dy = ((10, 0), (-10, 0), (0, -10), (0, 10))[self.orient[index]]
        return x+dx, y+dy

    def pin_dir(self, index, pin):
        return ('east', 'west', 'north', 'south')[self.orient[index]]


def fixture(*, feedback=True, obstacle=False, rename=False, reverse=False,
            shift=(0, 0), mirror=False, cut=False, guarded=False):
    """Three-port shared tree, or a two-port arch over a real body obstacle."""
    names = ['left', 'right'] + (['feedback'] if feedback else [])
    positions = [(0, 0), (100, 0)] + ([(0, 100)] if feedback else [])
    orientations = [0, 1] + ([0] if feedback else [])
    comps = [{'id': name, 'symbol': 'terminal'} for name in names]
    if obstacle:
        positions.append((50, 0))
        orientations.append(0)
        comps.append({'id': 'unconnected-obstacle', 'symbol': 'block'})
    index = {comp['id']: number for number, comp in enumerate(comps)}
    net = 'shared' if not rename else 'arbitrary_node_712'
    nets = {net: [(name, 'P') for name in names]}
    segments = [((10, 0), (24, 0)), ((24, 0), (24, -30)),
                ((24, -30), (76, -30)), ((76, -30), (76, 0)),
                ((76, 0), (90, 0))]
    if feedback:
        segments += [((50, -30), (50, 100)), ((10, 100), (50, 100))]
    if guarded:
        if feedback or rename or reverse or mirror or cut or shift != (0, 0):
            raise ValueError('guarded fixture is a separate untransformed two-port scene')
        comps.append({'id': 'protected-port', 'symbol': 'terminal'})
        positions.append((50, 30))
        orientations.append(2)
        index['protected-port'] = len(comps)-1
        nets['foreign'] = [('protected-port', 'P')]
    if rename:
        replacements = {name: 'device_'+str(91+number*7) for number, name in enumerate(index)}
        for comp in comps:
            comp['id'] = replacements[comp['id']]
        nets = {net: [(replacements[name], pin) for name, pin in nets[net]]}
        index = {comp['id']: number for number, comp in enumerate(comps)}
    if reverse:
        nets[net].reverse()
        segments.reverse()
        segments = [(b, a) for a, b in segments]
    if cut:
        subdivisions = []
        for a, b in segments:
            middle = ((a[0]+b[0])/2, (a[1]+b[1])/2)
            subdivisions.extend(((a, middle), (middle, b)))
        segments = subdivisions
    dx, dy = shift
    transform = lambda point: ((-point[0] if mirror else point[0])+dx, point[1]+dy)
    positions = list(map(transform, positions))
    segments = [(transform(a), transform(b)) for a, b in segments]
    if mirror:
        orientations = [1-value if comp['symbol'] == 'terminal' else value
                        for comp, value in zip(comps, orientations)]
    placement = FixturePlacement(comps, positions, orientations)
    par = schematic_par()
    options = RoutingOptions()
    routed = [(net, segments)]
    if guarded:
        routed.append(('foreign', [((50, 6), (50, 20))]))
    result = validate_final_routes(par, nets, placement, index, routed, options=options)
    return par, nets, placement, index, result, options


def run_simplifier(case, **kwargs):
    from simplify_routes import simplify_routes
    par, nets, placement, index, result, options = case
    return simplify_routes(par, nets, placement, index, result,
                            options=options, seconds=kwargs.pop('seconds', 5), **kwargs)


def same_direction_bridge(*, turns=0, mirror=False, shift=(0, 0), obstacle=False):
    """A tall two-bend bridge between two parallel outward pin corridors."""
    def transform(point):
        x, y = point
        if mirror:
            x = -x
        for _ in range(turns):
            x, y = -y, x
        return x+shift[0], y+shift[1]

    names = ['arbitrary_device_83', 'arbitrary_device_19']
    comps = [{'id': name, 'symbol': 'terminal'} for name in names]
    positions = list(map(transform, ((0, 0), (100, 0))))
    orientations = [[2, 0, 3, 1][turns]] * 2
    if obstacle:
        comps.append({'id': 'physical_obstacle', 'symbol': 'block'})
        positions.append(transform((50, -24)))
        orientations.append(0)
    placement = FixturePlacement(comps, positions, orientations)
    index = {comp['id']: number for number, comp in enumerate(comps)}
    nets = {'arbitrary_wire_512': [(names[1], 'P'), (names[0], 'P')]}
    path = list(map(transform, ((0, -10), (0, -80), (100, -80), (100, -10))))
    routed = [('arbitrary_wire_512', list(zip(path, path[1:])))]
    par, options = schematic_par(), RoutingOptions()
    result = validate_final_routes(par, nets, placement, index, routed, options=options)
    return par, nets, placement, index, result, options


def cycle_rank(segments):
    topology = canonical_topology(segments)
    adjacency = topology['adjacency']
    if not adjacency:
        return 0
    reached = set()
    components = 0
    for point in adjacency:
        if point in reached:
            continue
        components += 1
        pending = [point]
        while pending:
            item = pending.pop()
            if item in reached:
                continue
            reached.add(item)
            pending.extend(adjacency[item]-reached)
    return len(topology['segments'])-len(adjacency)+components


class SimplificationAcceptanceTests(unittest.TestCase):
    def assert_checked(self, case, simplified):
        par, nets, placement, index, _, options = case
        routes, metrics, validation, report = simplified
        checked, measured, final = validate_final_routes(par, nets, placement, index,
                                                        routes, options=options)
        self.assertTrue(validation['valid'])
        self.assertTrue(final['valid'])
        self.assertEqual(measured, metrics)
        json.dumps(report, allow_nan=False)
        for net, segments in routes:
            self.assertEqual(0, cycle_rank(segments), (net, segments))
        return routes, metrics, report

    def test_equal_bend_bridge_compacts_in_all_four_directions_and_keeps_stubs(self):
        for turns in range(4):
            for mirrored in (False, True):
                with self.subTest(turns=turns, mirror=mirrored):
                    case = same_direction_bridge(turns=turns, mirror=mirrored,
                                                 shift=(137, -53))
                    routes, metrics, report = self.assert_checked(case, run_simplifier(case))
                    self.assertEqual(2, case[4][1]['bends'])
                    self.assertEqual(case[4][1]['bends'], metrics['bends'])
                    self.assertEqual(128, metrics['wirelength'])
                    self.assertLess(metrics['wirelength'], case[4][1]['wirelength'])
                    self.assertEqual(0, metrics['crossings'])
                    self.assertGreater(report['candidate_counts']['accepted'], 0)
                    scene = build_scene(*case[:4], options=case[5])
                    runs = canonical_topology(routes[0][1])['runs']
                    for pin in scene.pins['arbitrary_wire_512']:
                        self.assertTrue(any(on_segment(pin.point, a, b)
                                            and on_segment(pin.escape, a, b) for a, b in runs))
                    repeated = run_simplifier((*case[:4], (routes, metrics, {'valid': True}), case[5]))
                    self.assert_checked(case, repeated)
                    self.assertEqual(metrics, repeated[1])
                    self.assertEqual(0, repeated[3]['candidate_counts']['accepted'])

    def test_equal_bend_bridge_keeps_required_body_clearance_when_compacted(self):
        case = same_direction_bridge(obstacle=True)
        routes, metrics, report = self.assert_checked(case, run_simplifier(case))
        par, nets, placement, index, _, options = case
        illegal = [(next(iter(nets)), [((0, -10), (0, -24)), ((0, -24), (100, -24)),
                                      ((100, -24), (100, -10))])]
        with self.assertRaises(RoutingValidationError):
            validate_final_routes(par, nets, placement, index, illegal, options=options)
        self.assertEqual(case[4][1], report['v3_metrics'])
        self.assertLess(metrics['wirelength'], case[4][1]['wirelength'])
        self.assertEqual(case[4][1]['bends'], metrics['bends'])
        self.assertGreater(report['candidate_counts']['geometry_rejected'], 0)

    def test_shared_three_terminal_tree_straightens_without_leaving_old_t_cycle(self):
        case = fixture()
        routes, metrics, report = self.assert_checked(case, run_simplifier(case))
        self.assertLess(metrics['bends'], case[4][1]['bends'])
        self.assertLess(metrics['wirelength'], case[4][1]['wirelength'])
        self.assertEqual(0, metrics['crossings'])
        net, segments = routes[0]
        for point in ((10, 0), (90, 0), (10, 100)):
            self.assertTrue(any(on_segment(point, a, b) for a, b in segments))
        # A new direct G-to-G chord plus the whole old arch would be a loop.
        old = case[4][0][0][1]
        self.assertGreater(cycle_rank(old+[((24, 0), (76, 0))]), 0)

    def test_real_body_obstacle_preserves_a_required_arch(self):
        case = fixture(feedback=False, obstacle=True)
        routes, metrics, _ = self.assert_checked(case, run_simplifier(case))
        self.assertGreaterEqual(metrics['bends'], 4)
        self.assertTrue(any(abs(point[1]) > 17 for _, segments in routes
                            for segment in segments for point in segment))

    def test_pin_stubs_and_real_anchors_survive_simplification(self):
        case = fixture()
        par, nets, placement, index, _, options = case
        scene = build_scene(par, nets, placement, index, options=options)
        routes, _, _ = self.assert_checked(case, run_simplifier(case))
        by_net = dict(routes)
        for net, pins in scene.pins.items():
            for pin in pins:
                for point in (pin.point, pin.escape):
                    self.assertTrue(any(on_segment(point, a, b) for a, b in by_net[net]),
                                    (pin, by_net[net]))

    def test_foreign_fixed_stub_blocks_a_shortcut_even_outside_body_halo(self):
        case = fixture(feedback=False, guarded=True)
        par, nets, placement, index, source, options = case
        straight = [('shared', [((10, 0), (90, 0))]), source[0][1]]
        with self.assertRaises(RoutingValidationError) as caught:
            validate_final_routes(par, nets, placement, index, straight, options=options)
        self.assertTrue(any(item['kind'] == 'fixed_stub_clearance'
                            for item in caught.exception.diagnostics['violations']))
        self.assertFalse(any(item['kind'] == 'body'
                             for item in caught.exception.diagnostics['violations']))
        simplified = run_simplifier(case)
        routes, metrics, _ = self.assert_checked(case, simplified)
        self.assertGreaterEqual(metrics['bends'], 4)
        foreign = dict(routes)['foreign']
        self.assertTrue(any(on_segment((50, 6), a, b) for a, b in foreign))
        self.assertTrue(any(on_segment((50, 20), a, b) for a, b in foreign))

    def test_subdivision_and_segment_direction_do_not_change_geometry(self):
        plain = fixture()
        cut = fixture(cut=True, reverse=True)
        first = run_simplifier(plain)
        second = run_simplifier(cut)
        self.assert_checked(plain, first)
        self.assert_checked(cut, second)
        self.assertEqual(first[1], second[1])
        first_runs = {name: canonical_topology(segments)['runs'] for name, segments in first[0]}
        second_runs = {name: canonical_topology(segments)['runs'] for name, segments in second[0]}
        self.assertEqual(first_runs, second_runs)

    def test_arbitrary_names_and_input_pin_order_preserve_quality(self):
        plain, renamed = fixture(), fixture(rename=True, reverse=True)
        first, second = run_simplifier(plain), run_simplifier(renamed)
        self.assert_checked(plain, first)
        self.assert_checked(renamed, second)
        self.assertEqual(first[1], second[1])

    def test_translation_and_mirroring_preserve_legality_and_improvement(self):
        baseline = run_simplifier(fixture())
        for mirrored in (False, True):
            with self.subTest(mirror=mirrored):
                case = fixture(shift=(137, -53), mirror=mirrored)
                simplified = run_simplifier(case)
                self.assert_checked(case, simplified)
                self.assertLess(simplified[1]['bends'], case[4][1]['bends'])
                self.assertLess(simplified[1]['wirelength'], case[4][1]['wirelength'])
                self.assertEqual(baseline[1], simplified[1])

    def test_original_routes_validation_and_placement_are_not_mutated(self):
        case = fixture()
        before_result = deepcopy(case[4])
        before_placement = deepcopy(case[2].__dict__)
        before_nets = deepcopy(case[1])
        simplified = run_simplifier(case)
        self.assert_checked(case, simplified)
        self.assertEqual(before_result, case[4])
        self.assertEqual(before_placement, case[2].__dict__)
        self.assertEqual(before_nets, case[1])

    def test_a_saved_valid_flag_cannot_hide_a_disconnected_real_pin(self):
        case = list(fixture())
        routes, metrics, validation = deepcopy(case[4])
        routes[0] = (routes[0][0], [segment for segment in routes[0][1]
                                  if not any(point == (10, 0) for point in segment)])
        case[4] = routes, metrics, validation
        self.assertTrue(validation['valid'])
        with self.assertRaises(RoutingValidationError):
            run_simplifier(case)

    def test_zero_budget_is_a_verified_geometry_preserving_noop(self):
        case = fixture()
        result = run_simplifier(case, seconds=0)
        self.assert_checked(case, result)
        self.assertEqual(case[4][0], result[0])
        self.assertEqual(case[4][1], result[1])


if __name__ == '__main__':
    unittest.main()
