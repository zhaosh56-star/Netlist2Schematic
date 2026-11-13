"""Candidate-side crossing keepouts use the final normalized topology."""
from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from netlist2schematic.postprocess import simplify_routes as shortcuts
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.routing_rules import EPS, Pin, RoutingOptions, Scene, analyze_routes, canonical_topology
from netlist2schematic.routing.sa_routing import _verify
from test_simplify_routes import case


GENERATORS = ('_shortcut_candidates', 'reattachment_candidates',
              '_three_segment_candidates', '_track_compaction_candidates')


def path_segments(points):
    return list(zip(points, points[1:]))


def crossing_case(y, *, clearance=12, foreign_y=14, signal='signal', owner='foreign'):
    candidate = path_segments([(0, 100), (0, y), (100, y), (100, 100)])
    occupied = {owner: [((50, foreign_y), (150, foreign_y))]}
    scene = Scene(options=RoutingOptions(cross_clearance=clearance),
                  all_pins={signal: [(0, 100), (100, 100)],
                            owner: [(50, foreign_y), (150, foreign_y)]})
    return scene, signal, candidate, occupied


def screen(scene, net, candidate, occupied):
    return shortcuts._crossing_clearance_conflict(scene, net, candidate, occupied) is None


def empty(*args):
    yield from ()


class CandidateCrossClearanceTests(unittest.TestCase):
    def assert_matches_geometry(self, scene, net, candidate, occupied, expected):
        accepted = screen(scene, net, candidate, occupied)
        self.assertEqual(expected, accepted)
        analysis = analyze_routes([(net, candidate), *occupied.items()], scene)
        crossing_errors = [item for item in analysis['violations']
                           if item['kind'] == 'crossing_clearance']
        if accepted:
            self.assertEqual([], crossing_errors)
        else:
            self.assertTrue(crossing_errors,
                            'the screen must have a matching final crossing violation')
        return analysis

    def test_candidate_corner_ten_or_eleven_units_from_crossing_is_rejected(self):
        for y in (4, 3):
            with self.subTest(y=y):
                scene, net, candidate, occupied = crossing_case(y)
                analysis = self.assert_matches_geometry(scene, net, candidate, occupied, False)
                self.assertEqual(1, analysis['crossings'])

    def test_rejection_reports_the_actual_intersection_corner_and_required_distance(self):
        scene, net, candidate, occupied = crossing_case(4)
        conflict = shortcuts._crossing_clearance_conflict(scene, net, candidate, occupied)
        self.assertEqual('crossing_clearance', conflict['kind'])
        self.assertEqual(['foreign', 'signal'], conflict['nets'])
        self.assertEqual((100, 14), conflict['point'])
        self.assertEqual((100, 4), conflict['structure_point'])
        self.assertEqual(10, conflict['distance'])
        self.assertEqual(12, conflict['required_distance'])

    def test_expired_precheck_raises_instead_of_claiming_clearance(self):
        scene, net, candidate, occupied = crossing_case(2)
        with self.assertRaises(RoutingFailure) as error:
            shortcuts._crossing_clearance_conflict(scene, net, candidate, occupied, deadline=0)
        self.assertEqual('quality_time_budget', error.exception.diagnostics['reason'])

    def test_exact_twelve_unit_crossing_clearance_and_eps_tolerance_are_allowed(self):
        for y in (2, 2 + EPS / 2):
            with self.subTest(y=y):
                scene, net, candidate, occupied = crossing_case(y)
                analysis = self.assert_matches_geometry(scene, net, candidate, occupied, True)
                self.assertEqual([], analysis['violations'])
                self.assertEqual(1, analysis['crossings'])

    def test_custom_crossing_clearance_is_used(self):
        for distance, expected in ((16, False), (17, False), (18, True)):
            with self.subTest(distance=distance):
                scene, net, candidate, occupied = crossing_case(
                    24 - distance, clearance=18, foreign_y=24)
                self.assert_matches_geometry(scene, net, candidate, occupied, expected)

    def test_plain_parallel_corner_at_ten_units_has_no_crossing_keepout(self):
        scene = Scene()
        candidate = [((-20, 10), (0, 10)), ((0, 10), (0, 30))]
        occupied = {'foreign': [((-100, 0), (100, 0))]}
        analysis = self.assert_matches_geometry(scene, 'signal', candidate, occupied, True)
        self.assertEqual([], analysis['violations'])
        self.assertEqual(0, analysis['crossings'])

    def test_artificial_segment_cuts_and_duplicate_same_net_runs_do_not_create_corners(self):
        scene, net, candidate, occupied = crossing_case(2)
        split = [part for a, b in candidate
                 for mid in [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)]
                 for part in ((a, mid), (mid, b))]
        split.extend([((100, 2), (100, 40)), ((100, 14), (100, 2))])
        split = [(b, a) for a, b in reversed(split)]
        occupied['foreign'] = [((50, 14), (100, 14)), ((100, 14), (150, 14))]
        analysis = self.assert_matches_geometry(scene, net, split, occupied, True)
        self.assertEqual([], analysis['violations'])
        self.assertEqual(1, analysis['crossings'])

    def test_same_net_old_geometry_is_not_a_foreign_obstacle(self):
        scene, net, candidate, occupied = crossing_case(2)
        # The previous accepted topology can have another corner near the
        # candidate's crossing. Only the new own topology has authority.
        occupied[net] = path_segments([(0, 100), (0, 4), (100, 4), (100, 100)])
        self.assertTrue(screen(scene, net, candidate, occupied))

    def test_escape_is_not_a_true_corner_or_pin(self):
        net = 'signal'
        points = [(0, -50), (100, 100), (100, -20)]
        inline = Pin(net, 2, 'P', points[2], (100, 4), (0, 1))
        scene = Scene(pins={net: [Pin(net, i, 'P', point, point, (1, 0))
                                  for i, point in enumerate(points[:2])] + [inline]},
                      all_pins={net: points, 'foreign': [(50, 14), (150, 14)]},
                      fixed_segments={net: [inline.stub]})
        candidate = [((0, -50), (100, -50)), ((100, -50), (100, -20)),
                     ((100, -20), (100, 4)), ((100, 4), (100, 100))]
        occupied = {'foreign': [((50, 14), (150, 14))]}
        self.assertNotIn(inline.escape,
                         canonical_topology(candidate, pins=scene.all_pins[net])['structure_points'])
        analysis = self.assert_matches_geometry(scene, net, candidate, occupied, True)
        self.assertEqual([], analysis['violations'])

    def test_dead_branch_is_pruned_before_the_candidate_crossing_check(self):
        net = 'signal'
        points = [(100, -50), (100, 100)]
        scene = Scene(pins={net: [Pin(net, i, 'P', point, point, (0, 1))
                                  for i, point in enumerate(points)]},
                      all_pins={net: points, 'foreign': [(50, 14), (150, 14)]},
                      fixed_segments={net: []})
        occupied = {'foreign': [((50, 14), (150, 14))]}
        candidate = [(points[0], points[1]), ((100, 4), (120, 4))]
        self.assert_matches_geometry(scene, net, candidate, occupied, False)
        trimmed = shortcuts._trim_tails(scene, net, candidate)
        analysis = self.assert_matches_geometry(scene, net, trimmed, occupied, True)
        self.assertEqual([], analysis['violations'])

    def test_real_branch_inside_crossing_keepout_is_still_rejected(self):
        scene, net, candidate, occupied = crossing_case(2)
        branch = (120, 4)
        scene.all_pins[net].append(branch)
        candidate.append(((100, 4), branch))
        self.assert_matches_geometry(scene, net, candidate, occupied, False)

    def test_translation_mirroring_and_renaming_preserve_boundary_decisions(self):
        for y, expected in ((4, False), (2, True)):
            for mirror in (False, True):
                with self.subTest(y=y, mirror=mirror):
                    scene, net, candidate, occupied = crossing_case(
                        y, signal='renamed_signal', owner='renamed_other')

                    def point(p):
                        return (-p[0] if mirror else p[0]) + 137, p[1] - 53

                    candidate = [(point(a), point(b)) for a, b in candidate]
                    occupied = {owner: [(point(a), point(b)) for a, b in segments]
                                for owner, segments in occupied.items()}
                    scene.all_pins = {owner: list(map(point, points))
                                      for owner, points in scene.all_pins.items()}
                    self.assert_matches_geometry(scene, net, candidate, occupied, expected)


def integration_case(*, movable_foreign=False):
    """A known-valid starting U, ordinary pins and an independent validator."""
    par = case()[0]
    positions = [(0, 100), (100, 100)]
    foreign_points = [(50, -80), (150, -80)] if movable_foreign else [(50, 14), (150, 14)]
    positions.extend(foreign_points)
    nets = {'signal': [('A', 'P'), ('B', 'P')], 'foreign': [('C', 'P'), ('D', 'P')]}
    index = dict(A=0, B=1, C=2, D=3)
    pins = {net: [Pin(net, i, 'P', positions[i], positions[i], (1, 0))
                  for i in components] for net, components in (('signal', (0, 1)), ('foreign', (2, 3)))}
    scene = Scene(pins=pins, all_pins={net: [pin.point for pin in group]
                                      for net, group in pins.items()},
                  fixed_segments={net: [] for net in nets})
    placement = SimpleNamespace(comps=[], pin_world=lambda component, pin: positions[component])
    signal = path_segments([(0, 100), (0, -50), (100, -50), (100, 100)])
    foreign = (path_segments([(50, -80), (50, 14), (150, 14), (150, -80)])
               if movable_foreign else [(foreign_points[0], foreign_points[1])])
    checked, metrics, _ = _verify(par, nets, placement, index,
                                  [('signal', signal), ('foreign', foreign)], scene)
    return par, nets, placement, index, (checked, metrics, {'valid': True}), scene


class UnifiedCrossScreenIntegrationTests(unittest.TestCase):
    def run_generators(self, fixture, generators, verify=None):
        with ExitStack() as stack:
            stack.enter_context(patch('netlist2schematic.postprocess.simplify_routes.build_scene', return_value=fixture[5]))
            for name, generator in zip(GENERATORS, generators):
                stack.enter_context(patch('netlist2schematic.postprocess.simplify_routes.' + name, side_effect=generator))
            verifier = stack.enter_context(patch('netlist2schematic.postprocess.simplify_routes._verify',
                                                 wraps=_verify if verify is None else verify))
            result = shortcuts.simplify_routes(*fixture[:4], fixture[4])
        return result, verifier.call_count

    def test_all_four_operations_filter_bad_own_corner_before_full_validation(self):
        candidate = path_segments([(0, 100), (0, 4), (100, 4), (100, 100)])
        for operation in range(4):
            with self.subTest(operation=shortcuts.OPERATIONS[operation]):
                fixture = integration_case()

                def proposal(*args):
                    yield {'net': 'signal', 'segments': candidate}

                generators = [empty] * 4
                generators[operation] = proposal
                result, calls = self.run_generators(fixture, generators)
                self.assertEqual(fixture[4][:2], result[:2])
                self.assertEqual(1, calls, 'only the original route should reach _verify')
                self.assertEqual(0, result[3]['candidate_counts']['validated'])
                self.assertEqual(0, result[3]['candidate_counts']['accepted'])

    def test_legal_candidate_still_reaches_the_independent_final_validator(self):
        fixture = integration_case()
        candidate = path_segments([(0, 100), (0, 2), (100, 2), (100, 100)])

        def proposal(*args):
            yield {'net': 'signal', 'segments': candidate}

        result, calls = self.run_generators(fixture, [proposal, empty, empty, empty])
        self.assertEqual(2, calls)
        self.assertEqual(1, result[3]['candidate_counts']['accepted'])
        _, metrics, _ = _verify(*fixture[:4], result[0], fixture[5])
        self.assertEqual(metrics, result[1])
        self.assertLess(result[1]['wirelength'], fixture[4][1]['wirelength'])

    def test_foreign_topology_cache_is_invalidated_after_a_commit(self):
        fixture = integration_case(movable_foreign=True)
        candidate = path_segments([(0, 100), (0, 4), (100, 4), (100, 100)])

        def signal(*args):
            yield {'net': 'signal', 'segments': candidate}

        def foreign(*args):
            yield {'net': 'foreign', 'segments': [((50, -80), (150, -80))]}

        result, calls = self.run_generators(fixture, [signal, foreign, empty, empty])
        self.assertEqual(['foreign', 'signal'],
                         [change['net'] for change in result[3]['accepted_changes']])
        self.assertEqual(3, calls)
        _, metrics, _ = _verify(*fixture[:4], result[0], fixture[5])
        self.assertEqual(metrics, result[1])
        self.assertEqual(0, result[1]['crossings'])

    def test_a_rejected_trial_cannot_replace_the_accepted_foreign_topology(self):
        fixture = list(integration_case(movable_foreign=True))
        scene = fixture[5]
        positions = [fixture[2].pin_world(i, 'P') for i in range(4)]
        routed = list(fixture[4][0])
        # Three guards cross the proposed straight foreign wire. Its two old
        # crossings disappear, but three new legal ones make the trial fail
        # the non-increasing-crossing objective after complete validation.
        for number, x in enumerate((70, 90, 110)):
            net = 'guard_' + str(number)
            component = len(positions)
            points = [(x, -120), (x, -64)]
            positions.extend(points)
            members = [('guard_pin_' + str(component + j), 'P') for j in (0, 1)]
            fixture[1][net] = members
            fixture[3].update({name: component + j for j, (name, _) in enumerate(members)})
            scene.pins[net] = [Pin(net, component + j, 'P', point, point, (0, 1))
                               for j, point in enumerate(points)]
            scene.all_pins[net] = points
            scene.fixed_segments[net] = []
            routed.append((net, [(points[0], points[1])]))
        fixture[2].pin_world = lambda component, port: positions[component]
        checked, metrics, _ = _verify(*fixture[:4], routed, scene)
        fixture[4] = (checked, metrics, fixture[4][2])
        fixture = tuple(fixture)
        self.assertEqual(2, metrics['crossings'])

        def foreign(*args):
            yield {'net': 'foreign', 'segments': [((50, -80), (150, -80))]}

        def signal(*args):
            yield {'net': 'signal', 'segments': path_segments(
                [(0, 100), (0, 4), (100, 4), (100, 100)])}

        result, calls = self.run_generators(fixture, [foreign, signal, empty, empty])
        self.assertEqual(fixture[4][:2], result[:2])
        self.assertEqual([], result[3]['accepted_changes'])
        # Only baseline plus the rejected foreign trial reach _verify. The
        # signal proposal still sees the original foreign U and is prefiltered.
        self.assertEqual(2, calls)
        self.assertGreater(result[3]['candidate_counts']['crossing_rejected'], 0)


if __name__ == '__main__':
    unittest.main()
