"""Four-segment track tightening with an independent geometry oracle."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orthogonal_router import RoutingFailure
from routing_rules import Pin, RoutingOptions, Scene, canonical_topology, intersection
from sa_routing import _verify
import simplify_routes as shortcuts


def track_case(*, turns=0, mirror=False, shift=(0, 0), rename=False,
               subdivide=False, reverse=False, branch=False, foreign=False,
               locked=False):
    """Two directional exits and blockers require the existing four-leg shape.

    A first horizontal leg meets the upper guard; a final vertical leg meets
    the lower guard. Reversing the initial upward fixed escape is forbidden.
    The long right/top tracks can nevertheless move toward those guard edges.
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
        return x + shift[0], y + shift[1]

    def rectangle(box):
        corners = [transform((x, y)) for x in (box[0], box[2])
                   for y in (box[1], box[3])]
        return min(x for x, y in corners), min(y for x, y in corners), \
            max(x for x, y in corners), max(y for x, y in corners)

    net = 'bridge_signal_971' if not rename else 'changed_signal'
    names = ['port_234', 'port_581'] if not rename else ['zeta', 'alpha']
    positions = list(map(transform, ((20, 20), (100, 80))))
    pins = {net: [Pin(net, 0, 'P', positions[0], transform((20, 0)), vector((0, -1))),
                  Pin(net, 1, 'P', positions[1], transform((120, 80)), vector((1, 0)))]}
    nets = {net: [(name, 'P') for name in names]}
    index = {name: i for i, name in enumerate(names)}
    path = list(map(transform, ((20, 20), (20, 0), (20, -80), (240, -80),
                                (240, 80), (120, 80), (100, 80))))
    segments = list(zip(path, path[1:]))
    if branch:
        names.append('third_port')
        positions.append(transform((-20, -80)))
        pins[net].append(Pin(net, 2, 'P', positions[-1], transform((0, -80)), vector((1, 0))))
        nets[net].append((names[-1], 'P'))
        index[names[-1]] = 2
        segments.extend([(transform((-20, -80)), transform((0, -80))),
                         (transform((0, -80)), transform((20, -80)))])
    if subdivide:
        segments = [part for a, b in segments
                    for m in [((a[0]+b[0])/2, (a[1]+b[1])/2)]
                    for part in ((a, m), (m, b))]
    if reverse:
        segments = [(b, a) for a, b in reversed(segments)]
        nets[net].reverse()
    routes = [(net, segments)]
    if foreign:
        name = 'unrelated_wire'
        offset = len(positions)
        positions.extend(map(transform, ((160, -60), (160, 60))))
        pins[name] = [Pin(name, i, 'P', positions[i], positions[i], vector((0, 1)))
                      for i in (offset, offset+1)]
        nets[name] = [('foreign_a', 'P'), ('foreign_b', 'P')]
        index.update(foreign_a=offset, foreign_b=offset+1)
        routes.append((name, [(positions[offset], positions[offset+1])]))
    markers = [('guard_upper', rectangle((40, -10, 70, 10))),
               ('guard_lower', rectangle((90, 20, 150, 60)))]
    if locked:
        # These guards leave the original route on x=240/y=-80 legal, while
        # covering the corridor through which either track would move inward.
        markers.extend([('top_required', rectangle((32, -78, 238, -12))),
                        ('right_required', rectangle((152, -78, 238, 78)))])
    scene = Scene(options=RoutingOptions(), pins=pins,
                  all_pins={name: [pin.point for pin in members] for name, members in pins.items()},
                  fixed_segments={name: [pin.stub for pin in members if pin.point != pin.escape]
                                  for name, members in pins.items()},
                  body_rects=[(0, rectangle((8, 2, 32, 32))),
                              (1, rectangle((88, 68, 118, 92)))],
                  marker_rects=markers)
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
    with patch('simplify_routes.build_scene', return_value=case[5]), \
            patch('orthogonal_router._find_path', side_effect=AssertionError('A* in compaction')), \
            patch('orthogonal_router.route_scene', side_effect=AssertionError('global rerouting')):
        return shortcuts.simplify_routes(*case[:4], case[4], seconds=kwargs.pop('seconds', 5), **kwargs)


class TrackCompactionTests(unittest.TestCase):
    def assert_safe(self, case, result):
        routed, metrics, validation, report = result
        _, measured, _ = _verify(*case[:4], routed, case[5])
        self.assertEqual(measured, metrics)
        self.assertTrue(validation['valid'])
        self.assertEqual(case[4][2]['search'], validation['search'])
        for key in ('wirelength', 'bends', 'crossings'):
            self.assertLessEqual(metrics[key], case[4][1][key])
        for net, segments in routed:
            self.assertTrue(shortcuts._covers_fixed(case[5], net, segments))
            self.assertLessEqual(shortcuts._cycle_rank(segments),
                                 shortcuts._cycle_rank(dict(case[4][0])[net]))
            if net != case[6]:
                self.assertEqual(canonical_topology(dict(case[4][0])[net])['runs'],
                                 canonical_topology(segments)['runs'])
        return routed, metrics, report

    def test_four_leg_chain_tightens_without_removing_required_bends(self):
        fixture = track_case()
        original = deepcopy(fixture[4])
        with patch('simplify_routes._track_compaction_candidates', return_value=iter(())):
            previous = simplify(fixture)
        self.assertEqual(original[0], previous[0], 'the existing v4 templates cannot tighten this shape')
        routed, metrics, report = self.assert_safe(fixture, simplify(fixture))
        self.assertLess(metrics['wirelength'], original[1]['wirelength'])
        self.assertEqual(3, metrics['bends'])
        self.assertEqual(0, metrics['crossings'])
        self.assertGreater(report['operations']['track_compaction']['accepted'], 0)
        self.assertTrue(report['v4_completed'])
        self.assertEqual(original[1], report['v4_metrics'])
        self.assertEqual(original, fixture[4])

    def test_rotated_mirrored_translated_renamed_and_split_routes_remain_safe(self):
        baseline = simplify(track_case())
        for turns in range(4):
            for mirrored in (False, True):
                with self.subTest(turns=turns, mirror=mirrored):
                    fixture = track_case(turns=turns, mirror=mirrored, shift=(137, -53),
                                         rename=True, subdivide=True, reverse=True)
                    result = simplify(fixture)
                    _, metrics, _ = self.assert_safe(fixture, result)
                    self.assertEqual(baseline[1], metrics)

    def test_a_branch_and_its_original_pin_remain_connected(self):
        fixture = track_case(branch=True)
        original_branch = fixture[5].pins[fixture[6]][2]
        result = simplify(fixture)
        routed, metrics, report = self.assert_safe(fixture, result)
        self.assertLess(metrics['wirelength'], fixture[4][1]['wirelength'])
        self.assertIn(original_branch.point,
                      canonical_topology(dict(routed)[fixture[6]],
                                         pins=[original_branch.point])['adjacency'])

    def test_a_required_outer_track_is_not_pushed_through_blockers(self):
        fixture = track_case(locked=True)
        result = simplify(fixture)
        routed, metrics, report = self.assert_safe(fixture, result)
        self.assertEqual(fixture[4][0], routed)
        self.assertEqual(fixture[4][1], metrics)
        self.assertEqual(0, report['operations']['track_compaction']['accepted'])

    def test_generated_moves_preserve_fixed_exits_and_have_bounded_work(self):
        from route_tracks import MAX_TRACK_CANDIDATES_PER_CHAIN, track_compaction_candidates
        fixture = track_case()
        fixture[5].body_rects = []
        chains = shortcuts._chains(fixture[5], fixture[4][0])
        proposals = list(track_compaction_candidates(fixture[5], fixture[4][0], chains))
        self.assertTrue(proposals)
        self.assertLessEqual(len(proposals), len(chains) * MAX_TRACK_CANDIDATES_PER_CHAIN)
        for proposal in proposals:
            self.assertEqual(fixture[6], proposal['net'])
            self.assertTrue(shortcuts._covers_fixed(fixture[5], proposal['net'], proposal['segments']))
            anchors = {p for pin in fixture[5].pins[proposal['net']] for p in (pin.point, pin.escape)}
            adjacency = canonical_topology(proposal['segments'], pins=anchors)['adjacency']
            for pin in fixture[5].pins[proposal['net']]:
                for neighbor in adjacency[pin.escape] - {pin.point}:
                    delta = neighbor[0]-pin.escape[0], neighbor[1]-pin.escape[1]
                    self.assertGreaterEqual(sum(a*b for a,b in zip(delta, pin.direction)), 0)

    def test_legal_crossings_cannot_increase_during_tightening(self):
        fixture = track_case(foreign=True)
        result = simplify(fixture)
        self.assert_safe(fixture, result)
        self.assertEqual(0, result[1]['crossings'])

    def test_an_individually_valid_track_with_an_extra_crossing_is_rejected(self):
        fixture = track_case(foreign=True)
        points = [(20, 20), (20, 0), (20, -12), (240, -12),
                  (240, 80), (120, 80), (100, 80)]
        candidate = list(zip(points, points[1:]))
        trial = [(name, candidate if name == fixture[6] else segments)
                 for name, segments in fixture[4][0]]
        _, metrics, _ = _verify(*fixture[:4], trial, fixture[5])
        self.assertLess(metrics['wirelength'], fixture[4][1]['wirelength'])
        self.assertEqual(fixture[4][1]['bends'], metrics['bends'])
        self.assertEqual(1, metrics['crossings'])
        with patch('simplify_routes._track_compaction_candidates',
                   return_value=iter([{'net': fixture[6], 'segments': candidate}])):
            result = simplify(fixture)
        self.assert_safe(fixture, result)
        self.assertEqual(fixture[4][0], result[0])
        self.assertEqual(fixture[4][1], result[1])
        self.assertEqual(1, result[3]['operations']['track_compaction']['objective_rejected'])

    def test_repeated_run_is_deterministic_and_converged_result_is_stable(self):
        fixture = track_case()
        first, second = simplify(fixture), simplify(fixture)
        self.assertEqual(first[:3], second[:3])
        self.assertEqual(first[3]['accepted_changes'], second[3]['accepted_changes'])
        again = (*fixture[:4], first[:3], *fixture[5:])
        repeated = simplify(again)
        self.assert_safe(again, repeated)
        self.assertEqual(first[0], repeated[0])
        self.assertEqual(first[1], repeated[1])
        self.assertEqual(0, repeated[3]['candidate_counts']['accepted'])

    def test_generation_deadline_retains_the_last_verified_incumbent(self):
        fixture = track_case()
        expired = RoutingFailure('track deadline', {'reason': 'quality_time_budget'})
        with patch('route_tracks.check_deadline', side_effect=expired):
            result = simplify(fixture)
        self.assert_safe(fixture, result)
        self.assertEqual(fixture[4][0], result[0])
        self.assertEqual('time_budget', result[3]['stop_reason'])


class TrackPhaseOrderTests(unittest.TestCase):
    def test_v4_finishes_before_new_tracks_then_all_actions_use_latest_state(self):
        from test_simplify_routes import case
        fixture = case()
        observed = []

        def arch(height):
            return [((0, 0), (10, 0)), ((10, 0), (10, height)),
                    ((10, height), (90, height)), ((90, height), (90, 0)), ((90, 0), (100, 0))]

        def chain(scene, routed, deadline):
            observed.append(('chain', shortcuts._wire_metrics(dict(routed)['a'])[0]))
            yield from ()

        def leaves(scene, routed, deadline):
            observed.append(('leaf', shortcuts._wire_metrics(dict(routed)['a'])[0]))
            yield from ()

        def patterns(scene, routed, deadline):
            length = shortcuts._wire_metrics(dict(routed)['a'])[0]
            observed.append(('patterns', length))
            if length > 140:
                yield {'net': 'a', 'segments': arch(20)}

        def tracks(scene, routed, deadline):
            length = shortcuts._wire_metrics(dict(routed)['a'])[0]
            observed.append(('tracks', length))
            if length > 120:
                self.assertEqual(140, length, 'new stage must see the complete v4 result')
                yield {'net': 'a', 'segments': arch(10)}

        with patch('simplify_routes.build_scene', return_value=fixture[5]), \
                patch('simplify_routes._shortcut_candidates', side_effect=chain), \
                patch('simplify_routes.reattachment_candidates', side_effect=leaves), \
                patch('simplify_routes._three_segment_candidates', side_effect=patterns), \
                patch('simplify_routes._track_compaction_candidates', side_effect=tracks):
            result = shortcuts.simplify_routes(*fixture[:4], fixture[4])
        _verify(*fixture[:4], result[0], fixture[5])
        self.assertEqual(140, result[3]['v4_metrics']['wirelength'])
        self.assertEqual(120, result[1]['wirelength'])
        self.assertEqual(['three_segment_shortcut', 'track_compaction'],
                         [change['operation'] for change in result[3]['accepted_changes']])
        self.assertEqual(['three_segment_refinement', 'track_compaction_refinement'],
                         [change['phase'] for change in result[3]['accepted_changes']])
        self.assertIn(('chain', 120), observed)
        self.assertIn(('leaf', 120), observed)
        self.assertIn(('patterns', 120), observed)

    def test_existing_budget_cannot_be_reset_to_force_new_tracks(self):
        fixture = track_case()
        with patch('simplify_routes.MAX_CANDIDATES', 0), \
                patch('simplify_routes._track_compaction_candidates',
                      side_effect=AssertionError('new stage must share the exhausted budget')):
            # A candidate in a preceding phase exhausts the common budget.
            proposal = {'net': fixture[6], 'segments': dict(fixture[4][0])[fixture[6]]}
            with patch('simplify_routes._shortcut_candidates', return_value=iter([proposal])):
                result = simplify(fixture)
        self.assert_safe_for_budget(fixture, result)
        self.assertEqual('candidate_budget', result[3]['stop_reason'])
        self.assertFalse(result[3]['v4_completed'])
        self.assertEqual(0, result[3]['operations']['track_compaction']['considered'])

    def assert_safe_for_budget(self, fixture, result):
        _verify(*fixture[:4], result[0], fixture[5])
        self.assertEqual(fixture[4][0], result[0])
        self.assertEqual(fixture[4][1], result[1])


if __name__ == '__main__':
    unittest.main()
