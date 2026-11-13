"""Structured-only defaults, stable identities and preserved recovery seams."""
from __future__ import annotations

import contextlib
from copy import deepcopy
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from netlist2schematic.flow import aesthetic_flow
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.flow import run_flow
from netlist2schematic.layout import structured_placement as structured


ROOT = Path(__file__).resolve().parents[1]


def candidate(identifier):
    placement = SimpleNamespace(identifier=identifier, cell_of=[(0, 0), (0, 1)],
                                orient=[0, 0])
    comps = [{'id': 'R1', 'symbol': 'resistor'}, {'id': 'R2', 'symbol': 'resistor'}]
    return placement, comps, {'signal': [('R1', '1'), ('R2', '1')]}, {'R1': 0, 'R2': 1}, {
        'candidate_id': identifier, 'candidate_seed': 100 + identifier, 'structs': []}


def route_result():
    return ([('signal', [((0, 0), (100, 0))])],
            {'wirelength': 100, 'crossings': 0, 'bends': 0},
            {'valid': True, 'routing_seconds': .001})


def quality(*args):
    return {'score': float(args[1].identifier), 'per_net': {}}


class SkipLegacyGeneratorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'passive.cir'
        self.source.write_bytes(b'R1 input output 1k\r\nR2 output 0 2k\r\n.end\r\n')

    def test_default_generator_never_initializes_the_legacy_reference(self):
        with patch.object(structured.legacy, 'create_placement',
                          side_effect=AssertionError('default ran legacy placement/SA')):
            items = list(structured.create_candidates(self.source, iterations=30, seed=29))
        self.assertEqual([1, 2, 3], [item[4]['candidate_id'] for item in items])
        self.assertEqual([29 + i * 104729 for i in (1, 2, 3)],
                         [item[4]['candidate_seed'] for item in items])

    def test_structured_seeds_and_random_states_match_optional_legacy_sequence(self):
        fixtures = (self.source, ROOT / 'tests/fixtures/aesthetic-baseline/3.cir')
        for source in fixtures:
            for policy in ('fixed', 'flexible'):
                with self.subTest(source=source.name, policy=policy):
                    direct = list(structured.create_candidates(source, iterations=60, seed=37,
                                  passive_layout=policy))
                    historical = list(structured.create_candidates(source, iterations=60, seed=37,
                                      passive_layout=policy, include_legacy=True))
                    self.assertEqual([0, 1, 2, 3], [item[4]['candidate_id'] for item in historical])
                    for actual, reference in zip(direct, historical[1:]):
                        pl, comps, nets, index, metadata = actual
                        old_pl, old_comps, old_nets, old_index, old_metadata = reference
                        self.assertEqual((comps, nets, index), (old_comps, old_nets, old_index))
                        self.assertEqual((pl.cell_of, pl.orient, pl.rng.getstate()),
                                         (old_pl.cell_of, old_pl.orient, old_pl.rng.getstate()))
                        for key in ('candidate_id', 'candidate_seed', 'layout_grid', 'energy',
                                    'optimization_metrics', 'annealing', 'alignment_relations'):
                            self.assertEqual(metadata[key], old_metadata[key], key)

    def test_default_and_explicit_structured_replay_do_not_run_legacy_sa(self):
        for requested, expected_routes in ((None, 3), ('1', 1), ('2', 1), ('3', 3)):
            with self.subTest(candidate_id=requested), \
                    patch.object(structured.legacy, 'create_placement',
                                 side_effect=AssertionError('structured replay ran legacy SA')), \
                    patch('netlist2schematic.flow.aesthetic_flow.final_route', return_value=route_result()) as routed, \
                    patch('netlist2schematic.layout.layout_quality.evaluate_quality', return_value={'score': 1., 'per_net': {}}), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = aesthetic_flow.select_layout(self.source, iterations=3, seed=43,
                                                      candidate_id=requested)
            self.assertEqual(expected_routes, routed.call_count)
            metadata = result[4]
            self.assertFalse(metadata['quality']['legacy_reference_enabled'])
            self.assertNotIn('0', [row['candidate_id'] for row in metadata['quality']['candidates']])
            if requested is not None:
                self.assertEqual(requested, metadata['quality']['selected_candidate'])

    def test_explicit_zero_replay_runs_only_legacy_sa_and_one_final_route(self):
        original = structured.legacy.create_placement
        with patch.object(structured.legacy, 'create_placement', wraps=original) as legacy, \
                patch('netlist2schematic.layout.structured_placement._anneal', side_effect=AssertionError('zero ran new SA')), \
                patch('netlist2schematic.flow.aesthetic_flow.final_route', return_value=route_result()) as routed, \
                patch('netlist2schematic.layout.layout_quality.evaluate_quality', return_value={'score': 1., 'per_net': {}}), \
                contextlib.redirect_stdout(io.StringIO()):
            result = aesthetic_flow.select_layout(self.source, candidate_id='0', iterations=7, seed=43)
        self.assertEqual(1, legacy.call_count)
        self.assertEqual(7, legacy.call_args.kwargs['iterations'])
        self.assertEqual(1, routed.call_count)
        self.assertEqual(0, result[4]['candidate_id'])
        self.assertEqual('0', result[4]['quality']['selected_candidate'])
        self.assertTrue(result[4]['quality']['legacy_reference_enabled'])


class StructuredOnlySelectionTests(unittest.TestCase):
    def select(self, items, routing, **kwargs):
        with patch('netlist2schematic.layout.structured_placement.create_candidates', return_value=iter(items)) as generated, \
                patch('netlist2schematic.flow.aesthetic_flow.final_route', side_effect=routing) as routed, \
                patch('netlist2schematic.layout.layout_quality.evaluate_quality', side_effect=quality), \
                contextlib.redirect_stdout(io.StringIO()):
            result = aesthetic_flow.select_layout('unused.cir', **kwargs)
        return result, routed, generated

    def test_third_identifier_still_gets_feedback_in_the_third_default_slot(self):
        proposal = list(candidate(99))
        proposal[4]['feedback'] = {'source_candidate': 2, 'net': 'signal'}
        observed = []

        def scored(*args):
            identifier = args[1].identifier
            return {'score': {1: 10., 2: 5., 99: 1.}[identifier],
                    'per_net': {'signal': {'terminals': 3, 'detour_factor': 2.5,
                                           'excess_detour': 500}}}

        def routing(*args, **kwargs):
            observed.append(args[2].identifier)
            return route_result()

        with patch('netlist2schematic.layout.structured_placement.create_candidates', return_value=iter([candidate(i) for i in (1, 2, 3)])), \
                patch('netlist2schematic.layout.structured_placement.feedback_candidate', return_value=tuple(proposal)) as feedback, \
                patch('netlist2schematic.flow.aesthetic_flow.final_route', side_effect=routing), \
                patch('netlist2schematic.layout.layout_quality.evaluate_quality', side_effect=scored), \
                contextlib.redirect_stdout(io.StringIO()):
            result = aesthetic_flow.select_layout('unused.cir')
        self.assertEqual([1, 2, 99], observed)
        self.assertEqual(1, feedback.call_count)
        self.assertEqual(2, feedback.call_args.args[0].identifier)
        self.assertEqual(3, result[4]['candidate_id'])
        self.assertEqual('3', result[4]['quality']['selected_candidate'])
        self.assertEqual(['1', '2', '3'], [row['candidate_id'] for row in result[4]['quality']['candidates']])

    def test_three_candidates_share_one_300_second_deadline(self):
        clock = SimpleNamespace(now=100.)

        def routing(*args, **kwargs):
            identifier = args[2].identifier
            clock.now = {1: 150., 2: 200., 3: 400.}[identifier]
            if identifier == 1:
                raise RoutingFailure('first blocked', {'reason': 'no_path'})
            if identifier == 3:
                raise RoutingFailure('budget reached', {'reason': 'quality_time_budget'})
            return route_result()

        with patch('netlist2schematic.flow.aesthetic_flow.perf_counter', side_effect=lambda: clock.now):
            result, routed, generated = self.select([candidate(i) for i in (1, 2, 3)], routing)
        self.assertEqual(400., generated.call_args.kwargs['deadline'])
        self.assertFalse(generated.call_args.kwargs['include_legacy'])
        self.assertEqual([400.] * 3, [call.kwargs['deadline'] for call in routed.call_args_list])
        self.assertEqual(300, result[4]['quality']['budget_seconds'])
        self.assertEqual('2', result[4]['quality']['selected_candidate'])
        self.assertEqual('time_budget', result[4]['quality']['stop_reason'])
        self.assertTrue(result[5][2]['valid'])

    def test_all_failures_preserve_the_last_attempted_candidate_three_checkpoint(self):
        failures = []
        for identifier in (1, 2, 3):
            error = RoutingFailure(f'blocked {identifier}', {'reason': 'no_path'})
            error._rescue_context = {'candidate': identifier, 'mutable': [identifier]}
            failures.append(error)
        with self.assertRaises(RoutingFailure) as caught:
            self.select([candidate(i) for i in (1, 2, 3)], failures)
        error = caught.exception
        self.assertEqual(3, error.retry_context[0].identifier)
        self.assertEqual(3, error.retry_context[4]['candidate_id'])
        self.assertEqual(3, error._rescue_context['candidate'])
        failures[-1]._rescue_context['mutable'].append(99)
        self.assertEqual([3], error._rescue_context['mutable'])
        self.assertEqual(['1', '2', '3'], [row['candidate_id'] for row in error.diagnostics['candidates']])
        self.assertFalse(error.diagnostics['legacy_reference_enabled'])

    def test_public_flow_waits_for_later_success_before_considering_recovery(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / 'circuit.cir'
        original = b'R1 input output 1k\r\nR2 output 0 2k\r\n.end\r\n'
        source.write_bytes(original)
        blocked = RoutingFailure('first blocked', {'reason': 'no_path'})
        blocked._rescue_context = {'candidate': 1}

        def finish(data, *args, **kwargs):
            return deepcopy(data)

        with patch('netlist2schematic.layout.structured_placement.create_candidates', return_value=iter([candidate(i) for i in (1, 2, 3)])), \
                patch('netlist2schematic.flow.aesthetic_flow.final_route', side_effect=[blocked, route_result(),
                    RoutingFailure('last blocked', {'reason': 'no_path'})]), \
                patch('netlist2schematic.layout.layout_quality.evaluate_quality', side_effect=quality), \
                patch('netlist2schematic.flow.run_flow._try_local_rescue', side_effect=AssertionError('early recovery')) as rescued, \
                patch('netlist2schematic.flow.run_flow._bus_result', side_effect=AssertionError('unnecessary fallback')), \
                patch('netlist2schematic.flow.run_flow._finish', side_effect=finish), \
                contextlib.redirect_stdout(io.StringIO()):
            data = run_flow.run(source, iterations=0, out_dir=root / 'result')
        rescued.assert_not_called()
        self.assertEqual('2', data['quality']['selected_candidate'])
        self.assertEqual('normal', data['routing_stage'])
        self.assertEqual(['normal'], [row['stage'] for row in data['recovery']['stages']])
        self.assertEqual(original, source.read_bytes())


if __name__ == '__main__':
    unittest.main()
