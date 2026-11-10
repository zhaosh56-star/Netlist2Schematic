"""Reserved simplification budgets and state-sensitive candidate safety."""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from math import ceil
import unittest
from unittest.mock import patch

import simplify_routes as shortcuts
from routing_rules import Pin, Scene, check_deadline
from sa_routing import _verify
from test_simplify_routes import case, run_case


GENERATORS = ('_shortcut_candidates', 'reattachment_candidates',
              '_three_segment_candidates', '_track_compaction_candidates')


class Clock:
    """Only explicit simulated work advances this monotonic clock."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def arch(height):
    return [((0, 0), (10, 0)), ((10, 0), (10, height)),
            ((10, height), (90, height)), ((90, height), (90, 0)),
            ((90, 0), (100, 0))]


def empty(*args):
    yield from ()


def run_with(fixture, generators, *, clock=None, verify=None, seconds=5):
    with ExitStack() as stack:
        for name, generator in zip(GENERATORS, generators):
            stack.enter_context(patch('simplify_routes.' + name, side_effect=generator))
        if clock is not None:
            stack.enter_context(patch('simplify_routes.perf_counter', new=clock))
            stack.enter_context(patch('routing_rules.perf_counter', new=clock))
        if verify is not None:
            stack.enter_context(patch('simplify_routes._verify', side_effect=verify))
        return run_case(fixture, seconds=seconds)


class ReservedSchedulingTests(unittest.TestCase):
    def assert_safe(self, fixture, result):
        _, measured, _ = _verify(*fixture[:4], result[0], fixture[5])
        self.assertEqual(measured, result[1])
        self.assertTrue(result[2]['valid'])
        for key in ('wirelength', 'crossings', 'bends'):
            self.assertLessEqual(result[1][key], fixture[4][1][key])

    def test_rejections_cannot_spend_later_operations_reserved_candidate_slots(self):
        fixture = case()
        clock = Clock()

        def proposals(number):
            def generate(scene, routed, deadline):
                for count in range(1000):
                    # Longer routes are independently rejected by the existing
                    # objective; distinct geometry prevents dedup hiding work.
                    yield {'net': 'a', 'segments': arch(50 + 1000 * number + count)}
            return generate

        result = run_with(fixture, [proposals(i) for i in range(4)], clock=clock)
        report = result[3]
        self.assert_safe(fixture, result)
        self.assertEqual(fixture[4][:2], result[:2])
        self.assertEqual(512, report['candidate_counts']['considered'])
        self.assertEqual('candidate_budget', report['stop_reason'])
        self.assertEqual('reserved_stages_v6', report['scheduling'])
        self.assertEqual(4, len(report['stages']))
        for i, stage in enumerate(report['stages']):
            self.assertEqual(shortcuts.OPERATIONS[i], stage['activated_operation'])
            self.assertEqual(ceil(512 * (i + 1) / 4), stage['candidate_limit'])
            self.assertEqual((i + 1) / 4, stage['time_fraction'])
            self.assertEqual(128, stage['candidate_counts']['considered'])
            self.assertGreater(report['operations'][shortcuts.OPERATIONS[i]]['considered'], 0)
        self.assertEqual(['quota_yield'] * 3 + ['candidate_budget'],
                         [stage['status'] for stage in report['stages']])
        self.assertFalse(report['warmup_completed'])
        self.assertFalse(report['v3_completed'])
        self.assertFalse(report['v4_completed'])
        self.assertFalse(report['converged'])

    def test_time_reservations_advance_after_local_generation_expiry(self):
        fixture = case()
        clock = Clock()
        observed = []

        def expires(number):
            def generate(scene, routed, deadline):
                observed.append(number)
                clock.now = deadline
                check_deadline(deadline, 'test_generator')
                yield  # pragma: no cover - check_deadline must terminate it
            return generate

        result = run_with(fixture, [expires(i) for i in range(4)], clock=clock)
        report = result[3]
        self.assert_safe(fixture, result)
        self.assertEqual([0, 1, 2, 3], observed)
        self.assertEqual([1.25, 2.5, 3.75, 5.0],
                         [stage['deadline_seconds'] for stage in report['stages']])
        self.assertEqual(['quota_yield'] * 3 + ['global_timeout'],
                         [stage['status'] for stage in report['stages']])
        self.assertEqual([1, 1, 1, 1],
                         [stage['generation_timeout'] for stage in report['stages']])
        self.assertEqual('time_budget', report['stop_reason'])
        self.assertEqual(5.0, report['elapsed_seconds'])
        self.assertEqual(0, report['candidate_counts']['considered'])

    def test_original_revalidation_cost_is_deducted_from_the_one_global_deadline(self):
        fixture = case()
        clock = Clock()

        def baseline(*args, **kwargs):
            clock.now += 1.0
            return _verify(*args, **kwargs)

        result = run_with(fixture, [empty] * 4, clock=clock, verify=baseline)
        report = result[3]
        self.assert_safe(fixture, result)
        # One second of preparation leaves four seconds of optimization. The
        # global deadline remains five seconds after entry, not six seconds.
        self.assertEqual([2.0, 3.0, 4.0, 5.0],
                         [stage['deadline_seconds'] for stage in report['stages']])
        self.assertEqual(['converged'] * 4,
                         [stage['status'] for stage in report['stages']])
        self.assertTrue(report['converged'])
        self.assertTrue(report['warmup_completed'])
        self.assertTrue(report['v3_completed'])
        self.assertTrue(report['v4_completed'])

    def test_unfinished_local_validation_is_discarded_before_later_stage_succeeds(self):
        fixture = case()
        clock = Clock()
        calls = 0

        def verify(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                clock.now = kwargs['deadline']
            return _verify(*args, **kwargs)

        def chain(scene, routed, deadline):
            yield {'net': 'a', 'segments': arch(20)}

        def leaf(scene, routed, deadline):
            yield {'net': 'a', 'segments': [((0, 0), (100, 0))]}

        result = run_with(fixture, [chain, leaf, empty, empty], clock=clock, verify=verify)
        self.assert_safe(fixture, result)
        self.assertEqual(100, result[1]['wirelength'])
        self.assertEqual('quota_yield', result[3]['stages'][0]['status'])
        self.assertEqual(1, result[3]['stages'][0]['validation_timeout'])
        self.assertEqual(['leaf_reattachment'],
                         [step['operation'] for step in result[3]['accepted_changes']])
        self.assertEqual(fixture[4][1], result[3]['accepted_changes'][0]['before_metrics'])

    def test_global_validation_expiry_does_not_start_later_operations(self):
        fixture = case()
        clock = Clock()
        calls = 0
        later_calls = []

        def verify(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                clock.now = 1005.0
            return _verify(*args, **kwargs)

        def chain(scene, routed, deadline):
            yield {'net': 'a', 'segments': arch(20)}

        def later(scene, routed, deadline):
            later_calls.append(True)
            yield from ()

        result = run_with(fixture, [chain, later, later, later], clock=clock, verify=verify)
        self.assert_safe(fixture, result)
        self.assertEqual(fixture[4], result[:3])
        self.assertEqual([], later_calls)
        self.assertEqual(['global_timeout'] + ['not_started'] * 3,
                         [stage['status'] for stage in result[3]['stages']])
        self.assertEqual('time_budget', result[3]['stop_reason'])
        self.assertEqual(0, result[3]['candidate_counts']['accepted'])

    def test_interrupted_candidate_can_be_retried_in_a_later_stage(self):
        fixture = case()
        clock = Clock()
        calls = 0

        def verify(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                clock.now = kwargs['deadline']
            return _verify(*args, **kwargs)

        def same(scene, routed, deadline):
            yield {'net': 'a', 'segments': arch(20)}

        result = run_with(fixture, [same, same, empty, empty], clock=clock, verify=verify)
        self.assert_safe(fixture, result)
        self.assertEqual(140, result[1]['wirelength'])
        self.assertEqual(1, result[3]['stages'][0]['validation_timeout'])
        self.assertEqual(2, result[3]['candidate_counts']['validated'])
        self.assertEqual(['leaf_reattachment'],
                         [step['operation'] for step in result[3]['accepted_changes']])

    def test_validation_returning_after_its_deadline_cannot_commit(self):
        fixture = case()
        clock = Clock()
        verification_calls = 0
        emitted = False

        def verify(*args, **kwargs):
            nonlocal verification_calls
            verification_calls += 1
            result = _verify(*args, **kwargs)
            if verification_calls == 2:
                # A delayed validator can return normally. The caller must
                # still check its phase boundary before committing the route.
                clock.now = kwargs['deadline']
            return result

        def once(scene, routed, deadline):
            nonlocal emitted
            if not emitted:
                emitted = True
                yield {'net': 'a', 'segments': arch(20)}

        result = run_with(fixture, [once, empty, empty, empty], clock=clock, verify=verify)
        self.assert_safe(fixture, result)
        self.assertEqual(fixture[4], result[:3])
        self.assertEqual(1, result[3]['stages'][0]['validation_timeout'])
        self.assertEqual([], result[3]['accepted_changes'])

    def test_zero_seconds_skips_scene_building_and_all_operations(self):
        fixture = case()
        with patch('simplify_routes.build_scene',
                   side_effect=AssertionError('disabled simplification must not build a scene')):
            result = shortcuts.simplify_routes(*fixture[:4], fixture[4], seconds=0)
        self.assertEqual(fixture[4], result[:3])
        self.assertEqual('disabled', result[3]['stop_reason'])
        self.assertFalse(result[3]['original_revalidated'])
        self.assertEqual(['not_started'] * 4,
                         [stage['status'] for stage in result[3]['stages']])
        self.assertEqual(0, result[3]['candidate_counts']['considered'])

    def test_original_revalidation_expiry_leaves_every_stage_not_started(self):
        fixture = case()
        clock = Clock()

        def verify(*args, **kwargs):
            clock.now = 1005.0
            return _verify(*args, **kwargs)

        def forbidden(*args):
            raise AssertionError('a stage started after original revalidation timed out')

        result = run_with(fixture, [forbidden] * 4, clock=clock, verify=verify)
        self.assert_safe(fixture, result)
        self.assertEqual(fixture[4], result[:3])
        self.assertEqual('time_budget', result[3]['stop_reason'])
        self.assertFalse(result[3]['original_revalidated'])
        self.assertEqual(['not_started'] * 4,
                         [stage['status'] for stage in result[3]['stages']])
        self.assertEqual(['time_budget'] * 4,
                         [stage['skip_reason'] for stage in result[3]['stages']])
        self.assertEqual(0, result[3]['candidate_counts']['considered'])

    def test_equal_trial_geometry_is_not_revalidated_by_each_operation(self):
        fixture = case(foreign=True)

        def same(number):
            def generate(scene, routed, deadline):
                # Valid electrically, but adds an unwanted legal crossing.
                # Artificial cuts and endpoint/segment order must not turn
                # the same rejected geometry into four independent trials.
                segments = ([((0, 0), (100, 0))] if number == 0 else
                            [((0, 0), (40, 0)), ((40, 0), (100, 0))])
                if number % 2:
                    segments = [(b, a) for a, b in reversed(segments)]
                yield {'net': 'a', 'segments': segments}
            return generate

        result = run_with(fixture, [same(i) for i in range(4)], clock=Clock())
        self.assert_safe(fixture, result)
        self.assertEqual(fixture[4][:2], result[:2])
        self.assertEqual(1, result[3]['candidate_counts']['validated'])
        self.assertEqual(1, result[3]['candidate_counts']['considered'])
        self.assertEqual(0, result[3]['candidate_counts']['accepted'])
        self.assertGreater(result[3]['duplicate_candidates'], 0)

    def test_rejected_trial_is_rechecked_after_a_foreign_net_changes(self):
        fixture = list(case())
        positions = [(0, 0), (100, 0), (30, -60), (70, -60)]
        fixture[1]['b'] = [('C', 'P'), ('D', 'P')]
        fixture[3].update(C=2, D=3)
        fixture[2].pin_world = lambda i, pin: positions[i]
        scene = fixture[5]
        scene.pins['b'] = [Pin('b', i, 'P', positions[i], positions[i], (0, -1))
                           for i in (2, 3)]
        scene.all_pins['b'] = positions[2:]
        scene.fixed_segments['b'] = []
        b_path = [(30, -60), (30, 20), (70, 20), (70, -60)]
        initial = deepcopy(fixture[4][0]) + [('b', list(zip(b_path, b_path[1:])))]
        checked, metrics, _ = _verify(*fixture[:4], initial, scene)
        fixture[4] = (checked, metrics, fixture[4][2])
        fixture = tuple(fixture)
        a_trials = []

        def verify(*args, **kwargs):
            result = _verify(*args, **kwargs)
            if shortcuts._wire_metrics(dict(result[0])['a'])[0] == 100:
                a_trials.append(result[1]['crossings'])
            return result

        def chain(scene, routed, deadline):
            yield {'net': 'a', 'segments': [((0, 0), (100, 0))]}

        def leaf(scene, routed, deadline):
            yield {'net': 'b', 'segments': [((30, -60), (70, -60))]}

        result = run_with(fixture, [chain, leaf, empty, empty], clock=Clock(), verify=verify)
        self.assert_safe(fixture, result)
        self.assertEqual([2, 0], a_trials)
        self.assertEqual(['b', 'a'],
                         [step['net'] for step in result[3]['accepted_changes']])
        self.assertEqual(140, result[1]['wirelength'])
        self.assertEqual(0, result[1]['crossings'])

    def test_small_candidate_limit_is_shared_and_uses_rounded_cumulative_reservations(self):
        fixture = case()

        def proposal(number):
            def generate(scene, routed, deadline):
                for count in range(20):
                    yield {'net': 'a', 'segments': arch(50 + number * 100 + count)}
            return generate

        with patch('simplify_routes.MAX_CANDIDATES', 5):
            result = run_with(fixture, [proposal(i) for i in range(4)], clock=Clock())
        self.assert_safe(fixture, result)
        self.assertEqual([2, 3, 4, 5],
                         [stage['candidate_limit'] for stage in result[3]['stages']])
        self.assertEqual(5, result[3]['candidate_counts']['considered'])
        self.assertEqual('candidate_budget', result[3]['stop_reason'])
        self.assertGreater(result[3]['operations']['track_compaction']['considered'], 0)


class DetourOrderingTests(unittest.TestCase):
    def test_a_large_few_bend_detour_precedes_a_small_many_bend_zigzag(self):
        paths = {
            'long': [(0, 0), (0, 1000), (100, 1000), (100, 0)],
            'small': [(200, 0), (210, 0), (210, 10), (220, 10), (220, 0), (230, 0)]}
        pins = {net: [Pin(net, i, 'P', point, point, (1, 0))
                      for i, point in enumerate((path[0], path[-1]))]
                for net, path in paths.items()}
        scene = Scene(pins=pins,
                      all_pins={net: [pin.point for pin in pair] for net, pair in pins.items()},
                      fixed_segments={net: [] for net in paths})
        routed = [(net, list(zip(path, path[1:]))) for net, path in paths.items()]
        chains = shortcuts._chains(scene, routed)
        self.assertEqual(['long', 'small'], [chain['net'] for chain in chains])
        self.assertLess(chains[0]['bends'], chains[1]['bends'])


if __name__ == '__main__':
    unittest.main()
