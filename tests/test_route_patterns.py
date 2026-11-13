"""Geometry-backed three-segment compaction without netlist-specific fixtures."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.routing_rules import Pin, RoutingOptions, Scene, canonical_topology, intersection
from netlist2schematic.routing.sa_routing import RoutingValidationError, _verify
from netlist2schematic.postprocess import simplify_routes as shortcuts


def pattern_case(*, turns=0, mirror=False, shift=(0, 0), rename=False,
                 subdivide=False, reverse=False, blocked=False, extra_crossing=False):
    """Parallel exits with a forbidden direct bridge and optional blocked U tracks."""
    def vector(point):
        x, y = point
        if mirror:
            x = -x
        for _ in range(turns):
            x, y = -y, x
        return x, y

    def transform(point):
        x, y = vector(point)
        return x + shift[0], y + shift[1]

    def rectangle(box):
        corners = [transform((x, y)) for x in (box[0], box[2])
                   for y in (box[1], box[3])]
        return (min(p[0] for p in corners), min(p[1] for p in corners),
                max(p[0] for p in corners), max(p[1] for p in corners))

    net = 'arbitrary_input_319' if not rename else 'renamed_node'
    foreign = 'unrelated_port_627' if not rename else 'different_node'
    names = ['endpoint_82', 'endpoint_171', 'guard_904'] if not rename else ['zeta', 'alpha', 'mu']
    positions = list(map(transform, ((0, 0), (0, 180), (0, 90))))
    middle_escape = 40 if extra_crossing else 20
    pins = {
        net: [Pin(net, 0, 'P', positions[0], transform((20, 0)), vector((1, 0))),
              Pin(net, 1, 'P', positions[1], transform((20, 180)), vector((1, 0)))],
        foreign: [Pin(foreign, 2, 'P', positions[2], transform((middle_escape, 90)),
                      vector((1, 0)))],
    }
    nets = {net: [(names[0], 'P'), (names[1], 'P')], foreign: [(names[2], 'P')]}
    index = {name: i for i, name in enumerate(names)}
    height = 40 if extra_crossing else 20
    path = list(map(transform, ((0, 0), (20, 0), (20, -height), (70, -height),
                                (70, 180+height), (20, 180+height), (20, 180), (0, 180))))
    main_segments = list(zip(path, path[1:]))
    if subdivide:
        main_segments = [segment for a, b in main_segments
                         for middle in [((a[0]+b[0])/2, (a[1]+b[1])/2)]
                         for segment in ((a, middle), (middle, b))]
    if reverse:
        main_segments = [(b, a) for a, b in reversed(main_segments)]
        nets[net].reverse()
    routes = [(net, main_segments), (foreign, [pins[foreign][0].stub])]
    bodies = [(i, rectangle((-12, y-12, 18, y+12)))
              for i, y in enumerate((0, 180, 90))]
    markers = [('reserved-shape', rectangle((25, -5, 65, 5)))] if blocked else []
    if extra_crossing:
        guard = 'other_signal_407'
        offset = len(positions)
        positions.extend(map(transform, ((35, -20), (35, 20))))
        pins[guard] = [Pin(guard, i, 'P', positions[i], positions[i], vector((0, 1)))
                       for i in (offset, offset+1)]
        nets[guard] = [('cross_138', 'P'), ('cross_773', 'P')]
        index.update(cross_138=offset, cross_773=offset+1)
        routes.append((guard, [(positions[offset], positions[offset+1])]))
    scene = Scene(options=RoutingOptions(), pins=pins,
                  all_pins={name: [pin.point for pin in members] for name, members in pins.items()},
                  fixed_segments={name: [pin.stub for pin in members if pin.point != pin.escape]
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
    validation = {'valid': True, 'router': 'original-router',
                  'search': {'original_search_is_preserved': True}}
    return par, nets, placement, index, (checked, metrics, validation), scene, net


def simplify(case, **kwargs):
    with patch('netlist2schematic.postprocess.simplify_routes.build_scene', return_value=case[5]), \
            patch('netlist2schematic.routing.orthogonal_router._find_path', side_effect=AssertionError('A*')), \
            patch('netlist2schematic.routing.orthogonal_router.route_scene', side_effect=AssertionError('global router')):
        return shortcuts.simplify_routes(*case[:4], case[4], seconds=kwargs.pop('seconds', 5), **kwargs)


class ThreeSegmentPatternTests(unittest.TestCase):
    def test_source_and_target_exits_cannot_reverse_along_their_fixed_stubs(self):
        from netlist2schematic.postprocess.route_patterns import three_segment_candidates
        # This deliberately has no symbol bodies: full geometry validation can
        # otherwise accept a path which retraces a fixed stub and leaves its
        # artificial escape as a protected, unused tail.
        unrotated = pattern_case()
        unrotated[5].body_rects = []
        backwards = [((0, 0), (20, 0)), ((0, 180), (20, 180)),
                     ((20, 0), (-12, 0)), ((-12, 0), (-12, 180)),
                     ((-12, 180), (20, 180))]
        old_trial = [(name, backwards if name == unrotated[6] else segments)
                     for name, segments in unrotated[4][0]]
        _verify(*unrotated[:4], old_trial, unrotated[5])
        self.assertTrue(shortcuts._covers_fixed(unrotated[5], unrotated[6], backwards))

        for turns in range(4):
            with self.subTest(turns=turns):
                case = pattern_case(turns=turns, shift=(137, -53))
                case[5].body_rects = []
                proposals = list(three_segment_candidates(
                    case[5], case[4][0], shortcuts._chains(case[5], case[4][0])))
                self.assertTrue(proposals)
                for proposal in proposals:
                    source, target = proposal['source'], proposal['target']
                    track = proposal['track_coordinate']
                    if proposal['track_axis'] == 'x':
                        neighbors = ((track, source[1]), (track, target[1]))
                    else:
                        neighbors = ((source[0], track), (target[0], track))
                    for endpoint, neighbor in zip((source, target), neighbors):
                        pin = next(pin for pin in case[5].pins[proposal['net']]
                                   if pin.escape == endpoint)
                        delta = neighbor[0]-endpoint[0], neighbor[1]-endpoint[1]
                        self.assertGreaterEqual(delta[0]*pin.direction[0]
                                                + delta[1]*pin.direction[1], 0)
                self.assert_safe(case, simplify(case))

    def test_a_single_bend_free_chain_does_not_trigger_three_segment_candidates(self):
        from netlist2schematic.postprocess.route_patterns import three_segment_candidates
        par = pattern_case()[0]
        points = [(0, 0), (100, 180)]
        pins = [Pin('signal', i, 'P', points[i], escape, (1, 0))
                for i, escape in enumerate(((20, 0), (120, 180)))]
        scene = Scene(options=RoutingOptions(), pins={'signal': pins},
                      all_pins={'signal': points}, fixed_segments={'signal': [pin.stub for pin in pins]})
        placement = SimpleNamespace(comps=[], pin_world=lambda i, pin: points[i])
        nets = {'signal': [('A', 'P'), ('B', 'P')]}
        index = {'A': 0, 'B': 1}
        checked, _, _ = _verify(par, nets, placement, index,
            [('signal', [((0, 0), (120, 0)), ((120, 0), (120, 180)),
                         ((100, 180), (120, 180))])], scene)
        chains = shortcuts._chains(scene, checked)
        self.assertEqual([1], [chain['bends'] for chain in chains])
        self.assertEqual([], list(three_segment_candidates(scene, checked, chains)))

    def assert_safe(self, case, result):
        routes, metrics, valid, report = result
        _, actual, _ = _verify(*case[:4], routes, case[5])
        self.assertTrue(valid['valid'])
        self.assertEqual(metrics, actual)
        self.assertTrue(all(metrics[key] <= case[4][1][key]
                            for key in ('wirelength', 'bends', 'crossings')))
        for net, segments in routes:
            self.assertTrue(shortcuts._covers_fixed(case[5], net, segments))
            self.assertLessEqual(shortcuts._cycle_rank(segments),
                                 shortcuts._cycle_rank(dict(case[4][0])[net]))
            if net != case[6]:
                self.assertEqual(canonical_topology(dict(case[4][0])[net])['runs'],
                                 canonical_topology(segments)['runs'])
        self.assertEqual(case[4][2]['search'], valid['search'])
        return routes, metrics, report

    def test_three_segment_u_succeeds_when_the_aligned_direct_bridge_is_forbidden(self):
        case = pattern_case()
        original = deepcopy(case[4])
        chains = shortcuts._chains(case[5], [(case[6], dict(case[4][0])[case[6]])])
        self.assertEqual(1, len(chains))
        self.assertEqual(4, chains[0]['bends'])
        direct = [(name, [case[5].pins[name][0].stub, case[5].pins[name][1].stub,
                          (case[5].pins[name][0].escape, case[5].pins[name][1].escape)]
                   if name == case[6] else segments) for name, segments in case[4][0]]
        with self.assertRaises(RoutingValidationError):
            _verify(*case[:4], direct, case[5])
        _, metrics, report = self.assert_safe(case, simplify(case))
        self.assertLess(metrics['wirelength'], original[1]['wirelength'])
        self.assertEqual(2, metrics['bends'])
        self.assertEqual(0, metrics['crossings'])
        self.assertGreater(report['operations']['three_segment_shortcut']['accepted'], 0)
        self.assertEqual(original, case[4])

    def test_rotations_mirrors_translation_names_and_subdivision_remain_general(self):
        baseline = simplify(pattern_case())
        for turns in range(4):
            for mirrored in (False, True):
                with self.subTest(turns=turns, mirror=mirrored):
                    case = pattern_case(turns=turns, mirror=mirrored, shift=(137, -53),
                                        rename=True, subdivide=True, reverse=True)
                    first = simplify(case)
                    routes, metrics, _ = self.assert_safe(case, first)
                    self.assertEqual(baseline[1], metrics)
                    again = (*case[:4], first[:3], *case[5:])
                    second = simplify(again)
                    self.assert_safe(again, second)
                    self.assertEqual(routes, second[0])
                    self.assertEqual(metrics, second[1])
                    self.assertEqual(0, second[3]['candidate_counts']['accepted'])

    def test_required_doglegs_around_a_marker_are_not_flattened_through_it(self):
        case = pattern_case(blocked=True)
        routes, metrics, report = self.assert_safe(case, simplify(case))
        # The three old operations cannot replace these doglegs. Track shifts
        # may tighten them while the independent oracle still protects markers.
        self.assertEqual(case[4][1], report['v4_metrics'])
        self.assertGreater(metrics['bends'], 0)
        self.assertGreater(report['candidate_counts']['geometry_rejected'], 0)

    def test_shorter_three_segment_wire_cannot_add_an_otherwise_legal_crossing(self):
        case = pattern_case(extra_crossing=True)
        net = case[6]
        trial = [(name, [((0, 0), (70, 0)), ((70, 0), (70, 180)), ((70, 180), (0, 180))]
                  if name == net else segments) for name, segments in case[4][0]]
        _, trial_metrics, _ = _verify(*case[:4], trial, case[5])
        self.assertLess(trial_metrics['wirelength'], case[4][1]['wirelength'])
        self.assertGreater(trial_metrics['crossings'], case[4][1]['crossings'])
        routes, metrics, report = self.assert_safe(case, simplify(case))
        self.assertEqual(case[4][1], report['v4_metrics'])
        self.assertEqual(case[4][1]['crossings'], metrics['crossings'])
        self.assertGreater(report['candidate_counts']['objective_rejected'], 0)

    def test_pattern_generation_expiry_retains_the_last_complete_incumbent(self):
        case = pattern_case()
        expired = RoutingFailure('expired pattern budget', {'reason': 'quality_time_budget'})
        with patch('netlist2schematic.postprocess.route_patterns.check_deadline', side_effect=expired):
            result = simplify(case)
        self.assert_safe(case, result)
        self.assertEqual(case[4][0], result[0])
        self.assertEqual(case[4][1], result[1])
        self.assertEqual('time_budget', result[3]['stop_reason'])

    def test_expiry_after_pattern_acceptance_keeps_the_improvement(self):
        case = pattern_case()
        from netlist2schematic.postprocess.route_patterns import three_segment_candidates as actual
        calls = 0

        def patterns(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RoutingFailure('expired after accepted pattern', {'reason': 'quality_time_budget'})
            yield from actual(*args, **kwargs)

        with patch('netlist2schematic.postprocess.simplify_routes.three_segment_candidates', side_effect=patterns):
            result = simplify(case)
        self.assert_safe(case, result)
        self.assertLess(result[1]['wirelength'], case[4][1]['wirelength'])
        self.assertGreater(result[3]['operations']['three_segment_shortcut']['accepted'], 0)
        self.assertEqual('time_budget', result[3]['stop_reason'])


if __name__ == '__main__':
    unittest.main()
