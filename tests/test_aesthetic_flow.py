"""Candidate selection and saved physical coordinates at the public flow seam."""
from __future__ import annotations

import contextlib
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from netlist2schematic.flow import aesthetic_flow
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.flow import run_flow
from netlist2schematic.common.sa_common import load_local
from netlist2schematic.common.sa_saved import load_placement


class CandidatePlacement:
    def __init__(self, identifier):
        self.identifier = identifier
        self.positions = [(0, 0), (100, 0)]

    def pos_of(self, index):
        return self.positions[index]

    def pin_world(self, index, pin):
        return self.positions[index]


def candidate(identifier, energy=0):
    comps = [{'id': 'R1', 'symbol': 'resistor'}, {'id': 'R2', 'symbol': 'resistor'}]
    nets = {'signal': [('R1', '1'), ('R2', '1')]}
    return (CandidatePlacement(identifier), comps, nets, {'R1': 0, 'R2': 1},
            {'candidate_id': identifier, 'candidate_seed': 10 + identifier,
             'energy': energy, 'structs': []})


def route_result(long=False):
    segments = ([((0, 0), (0, 50)), ((0, 50), (100, 50)), ((100, 50), (100, 0))]
                if long else [((0, 0), (100, 0))])
    return ([('signal', segments)],
            {'wirelength': 200 if long else 100, 'crossings': 0, 'bends': 2 if long else 0},
            {'valid': True, 'routing_seconds': .001})


class CandidateSelectionTests(unittest.TestCase):
    def select(self, items, routing, **kwargs):
        with patch('netlist2schematic.layout.structured_placement.create_candidates', return_value=iter(items)), \
                patch('netlist2schematic.flow.aesthetic_flow.final_route', side_effect=routing) as routed, \
                contextlib.redirect_stdout(io.StringIO()):
            result = aesthetic_flow.select_layout('unused.cir', iterations=0, **kwargs)
        return result, routed

    def test_final_wire_quality_selects_candidate_despite_worse_sa_energy(self):
        first, second = candidate(0, energy=-10000), candidate(1, energy=10000)
        chosen, routed = self.select([first, second], [route_result(True), route_result()])
        self.assertEqual(1, chosen[0].identifier)
        self.assertEqual(10000, chosen[4]['energy'])
        self.assertEqual('1', chosen[4]['quality']['selected_candidate'])
        self.assertEqual(['valid', 'valid'], [r['status'] for r in chosen[4]['quality']['candidates']])
        self.assertEqual(100, chosen[5][1]['wirelength'])
        self.assertEqual(2, routed.call_count)
        self.assertTrue(all(call.kwargs['deadline'] > 0 for call in routed.call_args_list))

    def test_timeout_in_later_router_preserves_already_validated_best(self):
        timeout = RoutingFailure('deadline reached', {'reason': 'quality_time_budget'})
        chosen, _ = self.select([candidate(0), candidate(1)], [route_result(), timeout])
        self.assertEqual(0, chosen[0].identifier)
        self.assertTrue(chosen[5][2]['valid'])
        report = chosen[4]['quality']
        self.assertEqual('time_budget', report['stop_reason'])
        self.assertEqual(['valid', 'failed'], [row['status'] for row in report['candidates']])
        self.assertEqual('quality_time_budget', report['candidates'][1]['diagnostics']['reason'])

    def test_budget_expiry_between_candidates_keeps_verified_best(self):
        clock = SimpleNamespace(now=100.)

        def generate():
            yield candidate(0)
            clock.now = 111.
            yield candidate(1)

        with patch('netlist2schematic.flow.aesthetic_flow.perf_counter', side_effect=lambda: clock.now):
            chosen, routed = self.select(generate(), [route_result()], quality_seconds=10)
        self.assertEqual(1, routed.call_count)
        self.assertEqual('time_budget', chosen[4]['quality']['stop_reason'])
        self.assertEqual('0', chosen[4]['quality']['selected_candidate'])

    def test_generation_failure_retains_successful_incumbent(self):
        def generate():
            yield candidate(0)
            raise ValueError('cannot generate another legal placement')

        chosen, routed = self.select(generate(), [route_result()])
        self.assertEqual(1, routed.call_count)
        self.assertEqual('generation_stopped', chosen[4]['quality']['stop_reason'])
        self.assertEqual('generation_stopped', chosen[4]['quality']['candidates'][-1]['status'])

    def test_budget_stop_inside_candidate_generator_is_reported_as_time_budget(self):
        clock = SimpleNamespace(now=100.)

        def generate():
            yield candidate(0)
            clock.now = 111.
            # A deadline-aware generator can stop without yielding another item.
            return

        with patch('netlist2schematic.flow.aesthetic_flow.perf_counter', side_effect=lambda: clock.now):
            chosen, routed = self.select(generate(), [route_result()], quality_seconds=10)
        self.assertEqual(1, routed.call_count)
        self.assertEqual('time_budget', chosen[4]['quality']['stop_reason'])

    def test_candidate_id_replay_only_routes_selected_identifier(self):
        for identifier in (0, 1, 2):
            with self.subTest(identifier=identifier):
                chosen, routed = self.select([candidate(i) for i in range(4)], [route_result()],
                                              candidate_id=str(identifier))
                self.assertEqual(identifier, chosen[0].identifier)
                self.assertEqual(1, routed.call_count)
                self.assertEqual(str(identifier), chosen[4]['quality']['selected_candidate'])
                self.assertEqual('replay_complete', chosen[4]['quality']['stop_reason'])

    def feedback_selection(self, feedback_score, *, candidate_id=None, severe=True):
        proposal = list(candidate(99))
        proposal[4]['feedback'] = {
            'source_candidate': '2', 'net': 'signal', 'row_cut': 0,
            'added_channel_height': 32, 'action': 'expand_local_row_channel'}
        # The threshold is inclusive. Source and final scores intentionally do
        # not follow SA energy or these small mocked routing lengths.
        def quality(score):
            return {'score': score, 'per_net': {'signal': {
                'terminals': 3, 'detour_factor': 2.5 if severe else 2.49,
                'excess_detour': 500}}}

        scores = [quality(score) for score in (30, 10, feedback_score)]
        with patch('netlist2schematic.layout.structured_placement.feedback_candidate', return_value=tuple(proposal)) as feedback, \
                patch('netlist2schematic.layout.layout_quality.evaluate_quality', side_effect=scores):
            chosen, routed = self.select([candidate(i, energy=-i) for i in (1, 2, 3)],
                                          [route_result()] * 3, candidate_id=candidate_id)
        return chosen, routed, feedback

    def test_measured_severe_detour_uses_last_slot_for_feedback_without_losing_incumbent(self):
        chosen, routed, feedback = self.feedback_selection(feedback_score=40)
        self.assertEqual(3, routed.call_count)
        self.assertEqual([1, 2, 99], [call.args[2].identifier for call in routed.call_args_list])
        self.assertEqual(1, feedback.call_count)
        self.assertEqual(2, feedback.call_args.args[0].identifier)
        self.assertEqual(2, chosen[0].identifier)
        self.assertEqual('2', chosen[4]['quality']['selected_candidate'])
        records = chosen[4]['quality']['candidates']
        self.assertEqual(['1', '2', '3'], [row['candidate_id'] for row in records])
        self.assertEqual('signal', records[2]['feedback']['net'])
        self.assertEqual('2', records[2]['feedback']['source_candidate'])

    def test_better_feedback_can_win_the_last_routing_slot(self):
        chosen, routed, feedback = self.feedback_selection(feedback_score=5)
        self.assertEqual(3, routed.call_count)
        self.assertEqual(1, feedback.call_count)
        self.assertEqual(99, chosen[0].identifier)
        self.assertEqual(3, chosen[4]['candidate_id'])
        self.assertEqual('3', chosen[4]['quality']['selected_candidate'])
        self.assertEqual(5, chosen[4]['quality']['score'])
        self.assertEqual('signal', chosen[4]['feedback']['net'])

    def test_moderate_detour_keeps_normal_third_seed(self):
        chosen, routed, feedback = self.feedback_selection(feedback_score=5, severe=False)
        self.assertEqual(3, routed.call_count)
        self.assertEqual([1, 2, 3], [call.args[2].identifier for call in routed.call_args_list])
        feedback.assert_not_called()
        self.assertEqual(3, chosen[0].identifier)

    def test_replaying_feedback_candidate_evaluates_its_sources_and_selects_requested_result(self):
        chosen, routed, feedback = self.feedback_selection(feedback_score=40, candidate_id='3')
        self.assertEqual([1, 2, 99], [call.args[2].identifier for call in routed.call_args_list])
        self.assertEqual(3, routed.call_count)
        self.assertEqual(1, feedback.call_count)
        self.assertEqual(99, chosen[0].identifier)
        self.assertEqual('3', chosen[4]['quality']['selected_candidate'])
        self.assertEqual('replay_complete', chosen[4]['quality']['stop_reason'])
        self.assertEqual('2', chosen[4]['feedback']['source_candidate'])

    def test_only_three_candidates_are_routed(self):
        chosen, routed = self.select([candidate(i) for i in range(1, 7)], [route_result()] * 3)
        self.assertEqual(3, routed.call_count)
        self.assertEqual('candidate_limit', chosen[4]['quality']['stop_reason'])
        self.assertEqual(3, len(chosen[4]['quality']['candidates']))

    def test_no_successful_candidate_never_claims_a_valid_best(self):
        with self.assertRaises(RoutingFailure) as caught:
            self.select([candidate(0)], [RoutingFailure('timeout', {'reason': 'quality_time_budget'})])
        self.assertEqual('time_budget', caught.exception.diagnostics['reason'])
        self.assertEqual('failed', caught.exception.diagnostics['candidates'][0]['status'])

    def test_rescue_checkpoint_matches_last_failed_candidate_and_is_private(self):
        first = RoutingFailure('first blocked', {'reason': 'no_path'})
        first._rescue_context = {'failed_net': 'first', 'mutable': [1]}
        second = RoutingFailure('second blocked', {'reason': 'no_path'})
        second._rescue_context = {'failed_net': 'second', 'mutable': [2]}
        with self.assertRaises(RoutingFailure) as caught:
            self.select([candidate(0), candidate(1)], [first, second])
        failure = caught.exception
        self.assertEqual(1, failure.retry_context[0].identifier)
        self.assertEqual('second', failure._rescue_context['failed_net'])
        second._rescue_context['mutable'].append(3)
        self.assertEqual([2], failure._rescue_context['mutable'])
        self.assertNotIn('_rescue_context', json.dumps(failure.diagnostics))

    def test_later_failure_without_checkpoint_cannot_reuse_earlier_wires(self):
        first = RoutingFailure('first blocked', {'reason': 'no_path'})
        first._rescue_context = {'failed_net': 'stale'}
        with self.assertRaises(RoutingFailure) as caught:
            self.select([candidate(0), candidate(1)],
                        [first, RoutingFailure('second blocked', {'reason': 'no_path'})])
        self.assertEqual(1, caught.exception.retry_context[0].identifier)
        self.assertFalse(hasattr(caught.exception, '_rescue_context'))

    def test_failed_first_candidate_does_not_rescue_before_later_success(self):
        failed = RoutingFailure('blocked', {'reason': 'no_path'})
        failed._rescue_context = {'failed_net': 'first'}
        with patch('netlist2schematic.flow.run_flow._try_local_rescue', side_effect=AssertionError('early rescue')):
            chosen, _ = self.select([candidate(0), candidate(1)], [failed, route_result()])
        self.assertEqual(1, chosen[0].identifier)

    def test_invalid_power_connections_are_not_search_failure_candidates(self):
        failure = run_flow.RoutingValidationError('invalid power', {'reason': 'power_connections'})
        with self.assertRaises(run_flow.RoutingValidationError):
            self.select([candidate(0)], [failure])

    def test_best_result_is_a_snapshot_when_router_reuses_mutable_buffers(self):
        shared_routes, shared_metrics, shared_validation = route_result()

        def routing(*args, **kwargs):
            identifier = args[2].identifier
            routes, metrics, validation = route_result(long=identifier == 1)
            shared_routes[:] = routes
            shared_metrics.clear()
            shared_metrics.update(metrics)
            shared_validation.clear()
            shared_validation.update(validation)
            return shared_routes, shared_metrics, shared_validation

        chosen, _ = self.select([candidate(0), candidate(1)], routing)
        self.assertEqual(0, chosen[0].identifier)
        self.assertEqual(100, chosen[5][1]['wirelength'])
        self.assertEqual([('signal', [((0, 0), (100, 0))])], chosen[5][0])
        self.assertEqual(100, chosen[4]['quality']['candidates'][0]['metrics']['wirelength'])


class AestheticSavedFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / 'small.cir'
        self.source.write_bytes(b'* preserve this exact RC input\r\nR1 input output 1k\r\n'
                                b'C1 output 0 1n\r\n.end\r\n')
        self.original = self.source.read_bytes()

    def generate(self):
        destination = self.directory / 'initial'
        with contextlib.redirect_stdout(io.StringIO()):
            data = run_flow.run(self.source, iterations=0, seed=17, candidate_id='1',
                                quality_seconds=10, out_dir=destination)
        return data, destination / 'small-placement.json'

    def test_structured_default_records_replay_coordinates_and_preserves_source(self):
        data, _ = self.generate()
        self.assertEqual('structured', data['layout_style'])
        self.assertEqual('1', data['quality']['selected_candidate'])
        self.assertEqual(1, data['candidate_id'])
        self.assertEqual(10, data['quality']['budget_seconds'])
        self.assertTrue(data['validation']['valid'])
        self.assertEqual({'column_x', 'row_y'}, set(data['layout_grid']))
        self.assertEqual(hashlib.sha256(self.original).hexdigest(), data['source_sha256'])
        self.assertEqual(self.original, self.source.read_bytes())

    def test_initial_candidate_budget_failure_records_diagnostics_and_preserves_previous_success(self):
        _, saved = self.generate()
        destination = saved.parent
        artifacts = [destination / name for name in
                     ('small-layered.svg', 'small-clean.svg', 'small-placement.json')]
        original_artifacts = {path: path.read_bytes() for path in artifacts}
        failure = RoutingFailure('No verified candidate completed in time', {
            'reason': 'time_budget', 'quality_seconds': .01, 'candidates': []})
        with patch('netlist2schematic.flow.aesthetic_flow.select_layout', side_effect=failure), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RoutingFailure):
            run_flow.run(self.source, iterations=0, quality_seconds=.01, out_dir=destination,
                         fallback='off')
        for path, original in original_artifacts.items():
            self.assertEqual(original, path.read_bytes(), path.name)
        diagnostic = json.loads((destination / 'routing-diagnostics.json').read_text(encoding='utf-8'))
        self.assertEqual('sa-routing-v1', diagnostic['diagnostic_format'])
        self.assertFalse(diagnostic['valid'])
        self.assertEqual('time_budget', diagnostic['diagnostics']['reason'])
        self.assertEqual([], diagnostic['diagnostics']['candidates'])
        self.assertEqual(str(self.source.resolve()), diagnostic['source_netlist'])
        self.assertEqual(self.original, self.source.read_bytes())

    def test_source_hash_in_saved_json_uses_original_bytes_including_line_endings(self):
        data, saved = self.generate()
        stored = json.loads(saved.read_text(encoding='utf-8'))
        expected = hashlib.sha256(self.original).hexdigest()
        self.assertEqual(expected, data['source_sha256'])
        self.assertEqual(expected, stored['source_sha256'])
        self.assertNotEqual(hashlib.sha256(self.original.replace(b'\r\n', b'\n')).hexdigest(), expected)

    def test_physical_maps_restore_and_are_used_by_reroute_and_independent_render(self):
        data, saved = self.generate()
        _, _, par, placement, comps, _, _ = load_placement(saved)
        for i, comp in enumerate(comps):
            record = data['placement'][comp['id']]
            expected = data['layout_grid']['column_x'][record['col']], data['layout_grid']['row_y'][record['row']]
            self.assertEqual(expected, placement.pos_of(i))
        renderer = load_local('render_clean', 'render-clean.py')
        with contextlib.redirect_stdout(io.StringIO()):
            renderer.render(saved, self.directory / 'independent.svg')
            rerouted = run_flow.reroute_placement(saved, out_dir=self.directory / 'rerouted')
        self.assertEqual((saved.parent / 'small-clean.svg').read_bytes(),
                         (self.directory / 'independent.svg').read_bytes())
        for key in ('placement', 'layout_grid', 'routes', 'metrics'):
            self.assertEqual(data[key], rerouted[key], key)
        self.assertEqual(self.original, self.source.read_bytes())

    def test_old_json_without_maps_recovers_original_regular_grid(self):
        data, _ = self.generate()
        data.pop('layout_grid')
        old = self.directory / 'old-placement.json'
        old.write_text(json.dumps(data), encoding='utf-8')
        _, _, par, placement, comps, _, _ = load_placement(old)
        self.assertFalse(hasattr(placement, 'column_x'))
        for i, comp in enumerate(comps):
            cell = data['placement'][comp['id']]
            self.assertEqual((cell['col'] * par.CELL + par.CELL / 2,
                              cell['row'] * par.CELL + par.CELL / 2), placement.pos_of(i))
        self.assertEqual(self.original, self.source.read_bytes())

    def test_saved_physical_coordinate_schema_rejects_nonfinite_or_unsorted_axes(self):
        data, _ = self.generate()
        for axis in ('column_x', 'row_y'):
            for invalid in ([float('nan')], [10, 10], [20, 10], []):
                with self.subTest(axis=axis, values=invalid):
                    broken = deepcopy(data)
                    broken['layout_grid'][axis] = invalid
                    saved = self.directory / 'invalid-placement.json'
                    saved.write_text(json.dumps(broken), encoding='utf-8')
                    with self.assertRaises(ValueError):
                        load_placement(saved)

    def test_cli_exposes_default_budget_and_candidate_replay(self):
        with patch('netlist2schematic.flow.run_flow.run') as run:
            run_flow.main([str(self.source), '--candidate-id', '2', '--iterations', '0'])
        self.assertEqual('structured', run.call_args.kwargs['layout_style'])
        self.assertEqual(300, run.call_args.kwargs['quality_seconds'])
        self.assertEqual('2', run.call_args.kwargs['candidate_id'])


if __name__ == '__main__':
    unittest.main()
