"""Absolute candidate slots, incumbent protection and private rescue snapshots."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import aesthetic_flow
from orthogonal_router import RoutingFailure


class Placement:
    def __init__(self, identifier):
        self.identifier = identifier

    def pos_of(self, number):
        return (number * 100, 0)

    def pin_world(self, number, pin):
        return self.pos_of(number)


def candidate(identifier):
    comps = [{'id': 'R1', 'symbol': 'resistor'}, {'id': 'R2', 'symbol': 'resistor'}]
    return (Placement(identifier), comps, {'signal': [('R1', '1'), ('R2', '1')]},
            {'R1': 0, 'R2': 1}, {'candidate_id': identifier,
                'candidate_seed': 100 + identifier, 'structs': []})


def valid(long=False):
    runs = ([((0, 0), (0, 50)), ((0, 50), (100, 50)), ((100, 50), (100, 0))]
            if long else [((0, 0), (100, 0))])
    return [('signal', runs)], {'wirelength': 200 if long else 100,
        'crossings': 0, 'bends': 2 if long else 0}, {'valid': True, 'routing_seconds': 1.0}


class ReservedFlowTests(unittest.TestCase):
    def setUp(self):
        self.clock = SimpleNamespace(now=100.0)

    def invoke(self, items, routing, **kwargs):
        with patch('structured_placement.create_candidates', return_value=iter(items)) as producer, \
                patch('aesthetic_flow.final_route', side_effect=routing) as router, \
                patch('aesthetic_flow.perf_counter', side_effect=lambda: self.clock.now), \
                redirect_stdout(io.StringIO()):
            output = aesthetic_flow.select_layout('unused.cir', iterations=0, **kwargs)
        return output, router, producer

    def test_default_and_explicit_sequential_use_the_original_global_deadline(self):
        outputs = []
        for kwargs in ({}, {'candidate_budget': 'sequential'}):
            result, router, producer = self.invoke([candidate(i) for i in (1, 2, 3)],
                                                  [valid()] * 3, **kwargs)
            self.assertEqual([c.kwargs['deadline'] for c in router.call_args_list], [400.] * 3)
            self.assertEqual(producer.call_args.kwargs['deadline'], 400.)
            self.assertEqual(result[4]['quality']['candidate_budget_policy'], 'sequential')
            outputs.append((result[4], result[5]))
        self.assertEqual(outputs[0], outputs[1])

    def test_reserved_uses_absolute_70_85_100_percent_cutoffs(self):
        result, router, _ = self.invoke([candidate(i) for i in (1, 2, 3)],
                                       [valid()] * 3, candidate_budget='reserved')
        self.assertEqual([c.kwargs['deadline'] for c in router.call_args_list], [310., 355., 400.])
        records = result[4]['quality']['candidates']
        self.assertEqual([r['candidate_budget']['cutoff_seconds'] for r in records], [210., 255., 300.])
        self.assertEqual(result[4]['quality']['candidate_budget_policy'], 'reserved')

    def test_generation_and_sa_time_are_already_inside_absolute_slots(self):
        def generate():
            self.clock.now = 112.
            yield candidate(1)
            self.clock.now = 130.
            yield candidate(2)
            self.clock.now = 145.
            yield candidate(3)
        result, router, producer = self.invoke(generate(), [valid()] * 3,
                                                candidate_budget='reserved')
        self.assertEqual(producer.call_args.kwargs['deadline'], 400.)
        self.assertEqual([c.kwargs['deadline'] for c in router.call_args_list], [310., 355., 400.])
        self.assertEqual([r['candidate_budget']['remaining_seconds_at_start']
                          for r in result[4]['quality']['candidates']], [198., 225., 255.])

    def test_unused_time_rolls_forward_without_extra_routing_attempts(self):
        available = []
        def route(*args, deadline, **kwargs):
            available.append(deadline - self.clock.now)
            self.clock.now += 20
            return valid()
        _, router, _ = self.invoke([candidate(i) for i in (1, 2, 3)], route,
                                   candidate_budget='reserved')
        self.assertEqual(available, [210., 235., 260.])
        self.assertEqual(router.call_count, 3)

    def test_first_slot_timeout_continues_and_later_success_wins(self):
        failure = RoutingFailure('slot exhausted', {'reason': 'quality_time_budget',
                                                   'blocking_nets': {'blocked': 4}})
        def route(*args, deadline, **kwargs):
            identifier = args[2].identifier
            if identifier == 1:
                self.clock.now = deadline
                raise failure
            self.clock.now += 5
            return valid(long=identifier == 3)
        result, router, _ = self.invoke([candidate(i) for i in (1, 2, 3)], route,
                                        candidate_budget='reserved')
        self.assertEqual(router.call_count, 3)
        self.assertEqual(result[0].identifier, 2)
        first = result[4]['quality']['candidates'][0]
        self.assertEqual(first['diagnostics']['reason'], 'candidate_time_budget')
        self.assertEqual(first['diagnostics']['original_reason'], 'quality_time_budget')
        self.assertEqual(first['diagnostics']['blocking_nets'], {'blocked': 4})
        self.assertEqual(first['candidate_budget']['stop_reason'], 'candidate_time_budget')
        self.assertEqual(failure.diagnostics['reason'], 'quality_time_budget')

    def test_second_slot_timeout_keeps_verified_first_and_still_tries_third(self):
        def route(*args, deadline, **kwargs):
            identifier = args[2].identifier
            if identifier == 2:
                self.clock.now = deadline
                raise RoutingFailure('second slot exhausted', {'reason': 'quality_time_budget'})
            return valid(long=identifier == 3)
        result, router, _ = self.invoke([candidate(i) for i in (1, 2, 3)], route,
                                        candidate_budget='reserved')
        self.assertEqual(router.call_count, 3)
        self.assertEqual(result[0].identifier, 1)
        self.assertEqual([r['status'] for r in result[4]['quality']['candidates']],
                         ['valid', 'failed', 'valid'])

    def test_third_timeout_is_global_and_preserves_verified_best(self):
        def route(*args, deadline, **kwargs):
            if args[2].identifier == 3:
                self.clock.now = deadline
                raise RoutingFailure('global exhausted', {'reason': 'quality_time_budget'})
            return valid(long=args[2].identifier == 2)
        result, router, _ = self.invoke([candidate(i) for i in (1, 2, 3)], route,
                                        candidate_budget='reserved')
        self.assertEqual(router.call_count, 3)
        self.assertEqual(result[0].identifier, 1)
        self.assertEqual(result[4]['quality']['stop_reason'], 'time_budget')
        self.assertEqual(result[4]['quality']['candidates'][-1]['diagnostics']['reason'],
                         'quality_time_budget')

    def test_global_expiry_during_first_call_is_never_mislabeled_as_local(self):
        def route(*args, **kwargs):
            self.clock.now = 401.
            raise RoutingFailure('global exhausted', {'reason': 'quality_time_budget'})
        with self.assertRaises(RoutingFailure) as caught:
            self.invoke([candidate(i) for i in (1, 2, 3)], route, candidate_budget='reserved')
        report = caught.exception.diagnostics
        self.assertEqual(len(report['candidates']), 1)
        self.assertEqual(report['reason'], 'time_budget')
        self.assertEqual(report['candidates'][0]['diagnostics']['reason'], 'quality_time_budget')

    def test_explicit_ids_including_three_prerequisites_keep_full_global_deadline(self):
        for identifier in ('0', '1', '2', '3'):
            with self.subTest(identifier=identifier):
                identifiers = (1, 2, 3) if identifier == '3' else range(4)
                result, router, _ = self.invoke([candidate(i) for i in identifiers], [valid()] * 3,
                    candidate_id=identifier, candidate_budget='reserved')
                self.assertEqual(result[0].identifier, int(identifier))
                self.assertTrue(all(c.kwargs['deadline'] == 400. for c in router.call_args_list))
                self.assertEqual(router.call_count, 3 if identifier == '3' else 1)
                self.assertEqual(result[4]['quality']['candidate_budget_policy'], 'sequential')
                self.assertEqual(result[4]['quality']['requested_candidate_budget'], 'reserved')

    def test_replay_timeout_keeps_original_break_behavior(self):
        failed = RoutingFailure('old global stop', {'reason': 'quality_time_budget'})
        with self.assertRaises(RoutingFailure) as caught:
            self.invoke([candidate(i) for i in (1, 2, 3)], [failed],
                        candidate_id='3', candidate_budget='reserved')
        self.assertEqual(len(caught.exception.diagnostics['candidates']), 1)
        self.assertEqual(caught.exception.diagnostics['candidate_budget_policy'], 'sequential')

    def test_expired_generated_slot_is_skipped_without_becoming_retry_context(self):
        first = RoutingFailure('first local failure', {'reason': 'quality_time_budget'})
        first._rescue_context = {'failed_net': 'original', 'mutable': [1]}
        def generate():
            yield candidate(1)
            self.clock.now = 360.
            yield candidate(2)
        def route(*args, deadline, **kwargs):
            self.clock.now = deadline
            raise first
        with self.assertRaises(RoutingFailure) as caught:
            self.invoke(generate(), route, candidate_budget='reserved')
        failure = caught.exception
        self.assertEqual(failure.retry_context[0].identifier, 1)
        self.assertEqual(failure._rescue_context['failed_net'], 'original')
        skipped = failure.diagnostics['candidates'][1]
        self.assertEqual(skipped['status'], 'skipped')
        self.assertEqual(skipped['candidate_budget']['phase'], 'after_generation')
        first._rescue_context['mutable'].append(2)
        self.assertEqual(failure._rescue_context['mutable'], [1])
        self.assertNotIn('_rescue_context', json.dumps(failure.diagnostics))

    def test_skipped_second_slot_does_not_prevent_third_routing(self):
        def generate():
            yield candidate(1)
            self.clock.now = 360.
            yield candidate(2)
            yield candidate(3)
        result, router, _ = self.invoke(generate(), [valid(), valid(True)],
                                        candidate_budget='reserved')
        self.assertEqual([c.args[2].identifier for c in router.call_args_list], [1, 3])
        self.assertEqual(result[4]['quality']['candidates'][1]['reason'], 'candidate_time_budget')

    def test_global_expiry_between_candidates_keeps_previous_verified_result(self):
        def generate():
            yield candidate(1)
            self.clock.now = 401.
            yield candidate(2)
        result, router, _ = self.invoke(generate(), [valid()], candidate_budget='reserved')
        self.assertEqual(router.call_count, 1)
        self.assertEqual(result[0].identifier, 1)
        self.assertEqual(result[4]['quality']['stop_reason'], 'time_budget')

    def test_later_failure_without_checkpoint_clears_old_private_snapshot(self):
        first = RoutingFailure('first', {'reason': 'quality_time_budget'})
        first._rescue_context = {'failed_net': 'stale'}
        second = RoutingFailure('second', {'reason': 'quality_time_budget'})
        def route(*args, deadline, **kwargs):
            self.clock.now = deadline
            raise first if args[2].identifier == 1 else second
        with self.assertRaises(RoutingFailure) as caught:
            self.invoke([candidate(1), candidate(2)], route, candidate_budget='reserved')
        self.assertEqual(caught.exception.retry_context[0].identifier, 2)
        self.assertFalse(hasattr(caught.exception, '_rescue_context'))

    def test_early_no_path_is_preserved_as_no_path_not_slot_timeout(self):
        first = RoutingFailure('no path', {'reason': 'no_path'})
        result, router, _ = self.invoke([candidate(1), candidate(2)], [first, valid()],
                                        candidate_budget='reserved')
        self.assertEqual(router.call_count, 2)
        self.assertEqual(result[4]['quality']['candidates'][0]['diagnostics']['reason'], 'no_path')

    def test_regularization_expiring_slot_is_skipped_before_retry_capture(self):
        def regularize(par, placement, comps, nets, index, metadata, *, deadline, **kwargs):
            if placement.identifier == 2:
                self.clock.now = 360.
            return placement, metadata
        with patch('symmetry_layout.regularize_symmetry', side_effect=regularize) as adjust, \
                patch('symmetry_layout.report_symmetry', return_value={'current': {'relation_count': 0}}):
            result, router, _ = self.invoke([candidate(i) for i in (1, 2, 3)], [valid()] * 2,
                candidate_budget='reserved', symmetry_layout='auto')
        self.assertEqual([c.args[2].identifier for c in router.call_args_list], [1, 3])
        self.assertEqual([c.kwargs['deadline'] for c in adjust.call_args_list], [355., 400.])
        self.assertEqual(result[4]['quality']['candidates'][1]['candidate_budget']['phase'],
                         'before_routing')

    def test_invalid_policy_fails_before_any_generation_or_routing(self):
        for value in ('off', 'fair', '', None, 1):
            with self.subTest(value=value), patch('structured_placement.create_candidates') as producer:
                with self.assertRaisesRegex(ValueError, 'candidate_budget'):
                    aesthetic_flow.select_layout('unused.cir', candidate_budget=value)
                producer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
