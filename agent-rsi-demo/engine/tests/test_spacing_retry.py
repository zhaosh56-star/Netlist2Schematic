"""Fresh routing of expanded copies and safe selector recovery contexts."""
from __future__ import annotations

import contextlib
from copy import deepcopy
from dataclasses import asdict
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import aesthetic_flow
from netlist_core import NetlistError
from orthogonal_router import RoutingFailure
from routing_rules import RoutingOptions
from sa_routing import RoutingValidationError
import spacing_retry


class Placement:
    def __init__(self, identifier=0, *, axes=True):
        self.identifier = identifier
        self.cell_of = [(0, 0), (1, 1), (1, 2)]
        self.orient = [0, 1, 0]
        self.comp_at = {cell: i for i, cell in enumerate(self.cell_of)}
        self.comps = [{'id': 'R1', 'symbol': 'resistor'},
                      {'id': 'R2', 'symbol': 'resistor'},
                      {'id': 'R3', 'symbol': 'resistor'}]
        if axes:
            self.column_x = [7, 17, 30]
            self.row_y = [-2, 7]

    def pos_of(self, index):
        row, col = self.cell_of[index]
        return (self.column_x[col] if hasattr(self, 'column_x') else 75 + 150 * col,
                self.row_y[row] if hasattr(self, 'row_y') else 75 + 150 * row)

    def pin_world(self, index, pin):
        x, y = self.pos_of(index)
        dx = -5 if pin == '1' else 5
        return x + (-dx if self.orient[index] else dx), y


def context(identifier=0, *, axes=True):
    placement = Placement(identifier, axes=axes)
    nets = {'signal': [('R1', '1'), ('R2', '1')]}
    metadata = {'candidate_id': identifier, 'candidate_seed': 100 + identifier,
                'energy': 123456, 'optimization_metrics': {'wirelength': 987654},
                'structs': [], 'annealing': {'stop_reason': 'iterations'}}
    if axes:
        metadata['layout_grid'] = {'column_x': list(placement.column_x),
                                   'row_y': list(placement.row_y)}
    return placement, placement.comps, nets, {'R1': 0, 'R2': 1, 'R3': 2}, metadata


def routing_result(par, nets, placement, index, *, options=None, deadline=None):
    a, b = (placement.pin_world(index[name], pin) for name, pin in nets['signal'])
    corner = b[0], a[1]
    segments = [(a, corner), (corner, b)]
    segments = [segment for segment in segments if segment[0] != segment[1]]
    length = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in segments)
    return [('signal', segments)], {'wirelength': length, 'crossings': 0, 'bends': 1}, {
        'valid': True, 'routing_seconds': .25, 'parameters': asdict(options or RoutingOptions())}


class SpacingRetryTests(unittest.TestCase):
    def retry(self, source=None, **kwargs):
        with patch('spacing_retry.load_local', return_value=SimpleNamespace()), \
                patch('spacing_retry.final_route', side_effect=routing_result) as routed:
            result = spacing_retry.retry_spacing(source or context(), **kwargs)
        return result, routed

    def test_deep_copy_keeps_logical_cells_orientations_and_local_pin_offsets(self):
        source = context()
        snapshot = deepcopy(source)
        result, routed = self.retry(source)
        placement, comps, nets, index, metadata, _ = result
        for returned, original in zip(result[:5], source):
            self.assertIsNot(returned, original)
        self.assertEqual(snapshot[0].__dict__, source[0].__dict__)
        self.assertEqual(snapshot[1:], source[1:])
        self.assertEqual(source[0].cell_of, placement.cell_of)
        self.assertEqual(source[0].orient, placement.orient)
        self.assertEqual(source[0].comp_at, placement.comp_at)
        for i in range(3):
            for pin in ('1', '2'):
                before = source[0].pin_world(i, pin)
                after = placement.pin_world(i, pin)
                center0, center1 = source[0].pos_of(i), placement.pos_of(i)
                self.assertEqual(tuple(a - b for a, b in zip(before, center0)),
                                 tuple(a - b for a, b in zip(after, center1)))
        self.assertIs(routed.call_args.args[2], placement)
        self.assertIs(placement.comps, comps)

    def test_axes_keep_first_coordinate_and_each_gap_rounds_up_to_grid(self):
        options = RoutingOptions(grid_step=4)
        result, routed = self.retry(routing_options=options)
        placement, _, _, _, metadata, _ = result
        self.assertEqual([7, 23, 43], placement.column_x)
        self.assertEqual([-2, 14], placement.row_y)
        self.assertEqual({'column_x': [7, 17, 30], 'row_y': [-2, 7]},
                         metadata['spacing_retry']['original_layout_grid'])
        self.assertEqual({'column_x': [7, 23, 43], 'row_y': [-2, 14]}, metadata['layout_grid'])
        self.assertEqual(options, routed.call_args.kwargs['options'])
        self.assertIsNot(options, routed.call_args.kwargs['options'])

    def test_missing_axes_restore_legacy_150_grid_before_expansion(self):
        result, _ = self.retry(context(axes=False))
        self.assertEqual([75.0, 301.0, 527.0], result[0].column_x)
        self.assertEqual([75.0, 301.0], result[0].row_y)
        self.assertEqual([75.0, 225.0, 375.0],
                         result[4]['spacing_retry']['original_layout_grid']['column_x'])

    def test_axes_can_be_restored_from_metadata_without_placement_attributes(self):
        source = context(axes=False)
        source[4]['layout_grid'] = {'column_x': [10, 100, 200], 'row_y': [20, 120]}
        result, _ = self.retry(source)
        self.assertEqual([10, 146, 296], result[0].column_x)
        self.assertEqual([20, 170], result[0].row_y)

    def test_actual_axes_override_stale_metadata_axes(self):
        source = context()
        source[4]['layout_grid']['column_x'] = [100, 200, 300]
        result, _ = self.retry(source)
        self.assertEqual(7, result[0].column_x[0])

    def test_only_one_fresh_route_with_new_deadline_and_no_sa(self):
        with patch('structured_placement.create_candidates', side_effect=AssertionError('SA called')), \
                patch('structured_placement._anneal', side_effect=AssertionError('SA called')), \
                patch('spacing_retry.perf_counter', side_effect=[100., 110., 112.]):
            result, routed = self.retry(seconds=60)
        self.assertEqual(1, routed.call_count)
        self.assertEqual({'options', 'deadline'}, set(routed.call_args.kwargs))
        self.assertEqual(170., routed.call_args.kwargs['deadline'])
        self.assertEqual(12., result[4]['spacing_retry']['elapsed_seconds'])

    def test_current_quality_uses_new_routes_and_old_energy_becomes_source_history(self):
        source = context()
        source[4].update(quality={'score': -100000}, routes={'stale': []},
                         metrics={'wirelength': -1}, validation={'valid': True})
        result, _ = self.retry(source)
        metadata = result[4]
        self.assertEqual('spacing_retry', metadata['routing_stage'])
        self.assertNotIn('energy', metadata)
        self.assertNotIn('optimization_metrics', metadata)
        self.assertNotIn('routes', metadata)
        self.assertNotIn('metrics', metadata)
        self.assertNotIn('validation', metadata)
        self.assertEqual(123456, metadata['spacing_retry']['source_optimization']['energy'])
        self.assertEqual(987654, metadata['spacing_retry']['source_optimization']['optimization_metrics']['wirelength'])
        self.assertEqual(-100000, metadata['spacing_retry']['source_quality']['score'])
        self.assertEqual(result[5][1]['wirelength'], metadata['quality']['details']['wirelength'])
        self.assertGreater(metadata['quality']['score'], 0)
        json.dumps(metadata, allow_nan=False)

    def test_router_failure_has_json_safe_stage_diagnostics_and_preserves_source(self):
        source = context()
        original = deepcopy(source)
        failure = RoutingFailure('blocked', {'reason': 'search_budget', 'net': 'signal'})
        with patch('spacing_retry.load_local', return_value=SimpleNamespace()), \
                patch('spacing_retry.final_route', side_effect=failure) as routed, \
                self.assertRaises(RoutingFailure) as caught:
            spacing_retry.retry_spacing(source)
        self.assertEqual(1, routed.call_count)
        self.assertEqual('search_budget', caught.exception.diagnostics['reason'])
        self.assertEqual('spacing_retry', caught.exception.diagnostics['routing_stage'])
        json.dumps(caught.exception.diagnostics, allow_nan=False)
        self.assertEqual(original[0].__dict__, source[0].__dict__)
        self.assertEqual(original[1:], source[1:])

    def test_unvalidated_router_result_is_never_accepted(self):
        with patch('spacing_retry.load_local', return_value=SimpleNamespace()), \
                patch('spacing_retry.final_route', return_value=([], {}, {'valid': False})), \
                self.assertRaises(RoutingFailure) as caught:
            spacing_retry.retry_spacing(context())
        self.assertEqual('spacing_retry_validation', caught.exception.diagnostics['reason'])

    def test_malformed_axes_and_cells_fail_before_routing(self):
        invalid_axes = ([], [7, 7, 30], [7, 6, 30], [7, float('nan'), 30],
                        [7, float('inf'), 30], [True, 17, 30], [7, 17])
        for values in invalid_axes:
            with self.subTest(axis=values):
                source = context()
                source[0].column_x = values
                with patch('spacing_retry.final_route') as routed, self.assertRaises(ValueError):
                    spacing_retry.retry_spacing(source)
                routed.assert_not_called()
        source = context()
        source[0].cell_of[1] = (0, 0)
        with self.assertRaises(ValueError):
            spacing_retry.retry_spacing(source)

    def test_invalid_parameters_are_not_reclassified_as_routing_failure(self):
        for kwargs in ({'scale': 1}, {'scale': 0}, {'scale': float('nan')},
                       {'scale': True}, {'seconds': 0}, {'seconds': float('inf')},
                       {'routing_options': {}}):
            with self.subTest(parameters=kwargs), self.assertRaises(ValueError) as caught:
                spacing_retry.retry_spacing(context(), **kwargs)
            self.assertNotIsInstance(caught.exception, RoutingFailure)


class SelectorRetryContextTests(unittest.TestCase):
    def run_select(self, candidates, router, **kwargs):
        with patch('structured_placement.create_candidates', return_value=iter(candidates)), \
                patch('aesthetic_flow.load_local', return_value=SimpleNamespace()), \
                patch('aesthetic_flow.final_route', side_effect=router) as routed, \
                contextlib.redirect_stdout(io.StringIO()):
            result = aesthetic_flow.select_layout('unused.cir', iterations=0, **kwargs)
        return result, routed

    def test_all_failures_keep_last_attempted_deep_copy_not_last_generated(self):
        clock = SimpleNamespace(now=100.)
        attempted = [context(0), context(1)]
        never_routed = context(2)

        def candidates():
            yield from attempted
            clock.now = 111.
            yield never_routed

        def router(par, nets, placement, index, **kwargs):
            # A failed router is allowed to reuse/mutate its private buffers.
            placement.column_x[0] = 9999
            raise RoutingFailure('blocked', {'reason': 'search_budget'})

        options = RoutingOptions(grid_step=4, crossing_cost=73)
        with patch('aesthetic_flow.perf_counter', side_effect=lambda: clock.now), \
                self.assertRaises(RoutingFailure) as caught:
            self.run_select(candidates(), router, quality_seconds=10, routing_options=options)
        failure = caught.exception
        self.assertEqual(1, failure.retry_context[0].identifier)
        self.assertEqual(7, failure.retry_context[0].column_x[0])
        self.assertIsNot(attempted[1][0], failure.retry_context[0])
        self.assertEqual(options, failure.retry_options)
        self.assertIsNot(options, failure.retry_options)
        self.assertEqual('time_budget', failure.diagnostics['reason'])
        self.assertEqual(2, len(failure.diagnostics['candidates']))
        self.assertNotIn('retry_context', failure.diagnostics)
        self.assertNotIn('retry_options', failure.diagnostics)
        json.dumps(failure.diagnostics, allow_nan=False)

    def test_route_validation_failure_is_recoverable(self):
        with self.assertRaises(RoutingFailure) as caught:
            self.run_select([context(0)], [RoutingValidationError('invalid', {'net': 'signal'})])
        self.assertEqual(0, caught.exception.retry_context[0].identifier)
        self.assertEqual('failed', caught.exception.diagnostics['candidates'][0]['status'])

    def test_controlled_placement_budget_error_is_recoverable_without_fake_context(self):
        for message in aesthetic_flow._PLACEMENT_BUDGET_MESSAGES:
            def generate():
                raise ValueError(message)
                yield  # This is a generator, as in production.

            with self.subTest(message=message), self.assertRaises(RoutingFailure) as caught:
                self.run_select(generate(), [])
            self.assertEqual('time_budget', caught.exception.diagnostics['reason'])
            self.assertIsNone(caught.exception.retry_context)
            self.assertIsInstance(caught.exception.retry_options, RoutingOptions)
            json.dumps(caught.exception.diagnostics, allow_nan=False)

    def test_parse_and_plain_router_data_errors_are_not_recovery_failures(self):
        for original in (NetlistError('malformed model'), ValueError('broken coordinates')):
            with self.subTest(error=original), self.assertRaises(type(original)) as caught:
                if isinstance(original, NetlistError):
                    def generate():
                        raise original
                        yield
                    self.run_select(generate(), [])
                else:
                    self.run_select([context(0)], [original])
            self.assertIs(original, caught.exception)
            self.assertNotIsInstance(caught.exception, RoutingFailure)

    def test_verified_incumbent_is_kept_without_spacing_recovery(self):
        failure = RoutingFailure('late timeout', {'reason': 'quality_time_budget'})
        with patch('spacing_retry.retry_spacing') as retry:
            selected, routed = self.run_select([context(0), context(1)],
                                               self._valid_then_failure(failure))
        self.assertEqual(0, selected[0].identifier)
        self.assertTrue(selected[5][2]['valid'])
        self.assertEqual(2, routed.call_count)
        self.assertNotIn('retry_context', selected[4])
        retry.assert_not_called()

    def test_bad_router_data_after_valid_incumbent_still_propagates(self):
        original = ValueError('corrupt geometry')
        with self.assertRaises(ValueError) as caught:
            self.run_select([context(0), context(1)], self._valid_then_failure(original))
        self.assertIs(original, caught.exception)
        self.assertNotIsInstance(caught.exception, RoutingFailure)

    @staticmethod
    def _valid_then_failure(failure):
        count = 0

        def route(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise failure
            return routing_result(*args, **kwargs)
        return route


if __name__ == '__main__':
    unittest.main()
