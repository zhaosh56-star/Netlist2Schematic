"""Route complexity is canonical, bounded and independent of drawing names."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import simplify_budget as budget
from orthogonal_router import RoutingFailure


def route_fixture():
    routed = [('alpha', [((0, 0), (0, 40)), ((0, 40), (100, 40)), ((100, 40), (100, 0))]),
              ('beta', [((-100, 200), (100, 200)), ((0, 200), (0, 300))])]
    pins = {'alpha': [(0, 0), (100, 0)],
            'beta': [(-100, 200), (100, 200), (0, 300)]}
    return routed, pins


def modest_features():
    """A small bent route prevents the no-work special case masking scores."""
    return {'device_count': 2, 'signal_nets': 1, 'runs': 3, 'segments': 3,
            'foreign_run_pairs': 0, 'device_segment_checks': 6, 'bends': 2,
            'junctions': 0, 'multi_terminal_nets': 0, 'long_detour_chains': 0,
            'wirelength': 180, 'chain_excess': 80, 'detour_fraction': 0.4}


class CanonicalComplexityTests(unittest.TestCase):
    def test_semantic_runs_segments_junctions_and_pairs_have_distinct_counts(self):
        routed, pins = route_fixture()
        features = budget.extract_features(routed, pins, device_count=4)
        self.assertEqual(2, features['signal_nets'])
        self.assertEqual(5, features['runs'])
        self.assertEqual(6, features['segments'])
        self.assertEqual(2, features['bends'])
        self.assertEqual(1, features['junctions'])
        self.assertEqual(1, features['multi_terminal_nets'])
        self.assertEqual(6, features['foreign_run_pairs'])
        self.assertEqual(24, features['device_segment_checks'])
        self.assertEqual(480, features['wirelength'])

    def test_splits_duplicates_and_order_do_not_inflate_complexity(self):
        routed, pins = route_fixture()
        expected = budget.extract_features(routed, pins, device_count=4)
        altered = []
        for net, segments in reversed(routed):
            split = [part for a, b in segments
                     for mid in [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)]
                     for part in ((a, mid), (mid, b))]
            split.extend(segments)
            altered.append((net, [(b, a) for a, b in reversed(split)]))
        actual = budget.extract_features(altered,
                                         {net: list(reversed(points)) for net, points in pins.items()},
                                         device_count=4)
        self.assertEqual(expected, actual)

    def test_renaming_translation_and_mirroring_preserve_features(self):
        routed, pins = route_fixture()
        expected = budget.extract_features(routed, pins, device_count=4)

        def point(p):
            return 173 - p[0], p[1] - 41

        renamed = {'alpha': 'renamed_z', 'beta': 'renamed_a'}
        changed = [(renamed[net], [(point(a), point(b)) for a, b in segments])
                   for net, segments in reversed(routed)]
        changed_pins = {renamed[net]: list(map(point, points)) for net, points in pins.items()}
        self.assertEqual(expected, budget.extract_features(changed, changed_pins, device_count=4))

    def test_purely_large_straight_drawing_stays_at_the_minimum(self):
        routed = [('signal', [((0, 0), (1000000, 0))])]
        pins = {'signal': [(0, 0), (1000000, 0)]}
        features = budget.extract_features(routed, pins, device_count=100000)
        self.assertGreater(features['device_segment_checks'], 2000)
        self.assertEqual(0, features['bends'])
        self.assertEqual(5, budget.select_budget(features)['selected_seconds'])

    def test_adapter_uses_real_terminal_coordinates_without_mutating_placement(self):
        points = [(0, 0), (100, 0)]
        placement = SimpleNamespace(pin_world=lambda component, pin: points[component])
        comps = [{'id': 'left'}, {'id': 'right'}]
        nets = {'signal': [('left', 'P'), ('right', 'P')]}
        index = {'left': 0, 'right': 1}
        routed = [('signal', [((0, 0), (0, 40)), ((0, 40), (100, 40)), ((100, 40), (100, 0))])]
        report = budget.estimate_simplify_budget(placement, comps, nets, index, routed)
        expected = budget.extract_features(routed, {'signal': points}, device_count=2)
        self.assertEqual(expected, report['features'])
        self.assertEqual(budget.select_budget(expected)['selected_seconds'], report['selected_seconds'])
        self.assertGreaterEqual(report['estimation_seconds'], 0)
        self.assertEqual(points, [placement.pin_world(i, 'P') for i in (0, 1)])

    def test_feature_deadline_uses_bounded_fallback_without_inventing_features(self):
        points = [(0, 0), (100, 0)]
        placement = SimpleNamespace(pin_world=lambda component, pin: points[component])
        expired = RoutingFailure('feature extraction expired', {'reason': 'quality_time_budget'})
        with patch('simplify_budget.extract_features', side_effect=expired):
            report = budget.estimate_simplify_budget(placement, [{}, {}],
                {'signal': [('left', 'P'), ('right', 'P')]}, {'left': 0, 'right': 1},
                [('signal', [((0, 0), (100, 0))])])
        self.assertEqual(30, report['selected_seconds'])
        self.assertEqual('time_budget', report['feature_status'])
        self.assertIsNone(report['features'])
        self.assertEqual({}, report['scores'])
        self.assertEqual(['feature_extraction_budget_exhausted'], report['reasons'])
        self.assertGreaterEqual(report['estimation_seconds'], 0)

    def test_non_deadline_feature_errors_are_not_hidden_as_a_large_budget(self):
        failure = RoutingFailure('invalid geometry', {'reason': 'invalid_geometry'})
        placement = SimpleNamespace(pin_world=lambda component, pin: (0, 0))
        with patch('simplify_budget.extract_features', side_effect=failure), \
                self.assertRaises(RoutingFailure) as caught:
            budget.estimate_simplify_budget(placement, [], {}, {}, [])
        self.assertIs(failure, caught.exception)


class AutomaticTierTests(unittest.TestCase):
    def test_discrete_thresholds_round_up_and_cap_only_automatic_requests(self):
        for segments, expected in ((80, 5), (81, 10), (160, 10), (161, 15),
                                   (240, 15), (241, 20), (320, 20), (321, 30),
                                   (480, 30), (10000, 30)):
            with self.subTest(segments=segments):
                features = modest_features()
                features['segments'] = segments
                report = budget.select_budget(features)
                self.assertEqual(expected, report['selected_seconds'])
                self.assertEqual('auto', report['mode'])
                self.assertEqual(budget.ESTIMATOR_VERSION, report['estimator_version'])

    def test_each_workload_feature_can_raise_the_budget(self):
        cases = {'foreign_run_pairs': 2001, 'device_segment_checks': 2001,
                 'segments': 81, 'bends': 41, 'junctions': 16,
                 'multi_terminal_nets': 9, 'long_detour_chains': 5}
        for feature, value in cases.items():
            with self.subTest(feature=feature):
                features = modest_features()
                features[feature] = value
                self.assertEqual(10, budget.select_budget(features)['selected_seconds'])

    def test_detour_fraction_can_raise_budget_but_absolute_length_alone_cannot(self):
        features = modest_features()
        features['wirelength'] = 100000000
        features['chain_excess'] = 10000000
        self.assertEqual(5, budget.select_budget(features)['selected_seconds'])
        for fraction, expected in ((0.5, 5), (0.51, 10), (1.0, 10)):
            with self.subTest(fraction=fraction):
                features['detour_fraction'] = fraction
                self.assertEqual(expected, budget.select_budget(features)['selected_seconds'])

    def test_report_records_an_independent_configuration_snapshot(self):
        features = modest_features()
        before = deepcopy(features)
        config = deepcopy(budget.AUTO_CONFIG)
        report = budget.select_budget(features)
        self.assertEqual(before, features)
        self.assertEqual(config, report['config'])
        report['config']['levels'].append(999)
        self.assertEqual(config, budget.AUTO_CONFIG)


if __name__ == '__main__':
    unittest.main()
