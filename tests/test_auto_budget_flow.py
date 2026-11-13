"""Automatic simplification budgets at CLI and saved-output boundaries."""
from __future__ import annotations

import contextlib
from copy import deepcopy
import io
import json
import unittest
from unittest.mock import patch

from netlist2schematic.flow import run_flow
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.routing_rules import RoutingOptions
import test_simplify_flow as flow_fixtures


class AutoBudgetCliTests(unittest.TestCase):
    def test_omitted_budget_still_forwards_fixed_five_seconds(self):
        with patch('netlist2schematic.flow.run_flow.run') as run:
            run_flow.main(['examples/1.cir'])
        self.assertEqual(5, run.call_args.kwargs['simplify_seconds'])

    def test_auto_is_forwarded_for_each_input_mode(self):
        modes = ((['examples/1.cir'], 'run'),
                 (['--reroute-placement', 'saved.json'], 'reroute_placement'),
                 (['--simplify-placement', 'saved.json'], 'simplify_placement'))
        for arguments, entrypoint in modes:
            with self.subTest(entrypoint=entrypoint), patch('netlist2schematic.flow.run_flow.' + entrypoint) as run:
                run_flow.main([*arguments, '--simplify-seconds', 'auto'])
            self.assertEqual('auto', run.call_args.kwargs['simplify_seconds'])

    def test_explicit_numeric_cli_budget_is_preserved(self):
        for value in ('0', '5', '2.75', '31'):
            with self.subTest(value=value), patch('netlist2schematic.flow.run_flow.simplify_placement') as simplify:
                run_flow.main(['--simplify-placement', 'saved.json', '--simplify-seconds', value])
            self.assertEqual(float(value), simplify.call_args.kwargs['simplify_seconds'])

    def test_invalid_budget_does_not_reach_an_execution_entrypoint(self):
        for value in ('automatic', 'nan', 'inf', '-1'):
            with self.subTest(value=value), patch('netlist2schematic.flow.run_flow.run') as run, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                run_flow.main(['examples/1.cir', '--simplify-seconds', value])
            self.assertEqual(2, error.exception.code)
            run.assert_not_called()


class AutoBudgetFlowTests(unittest.TestCase):
    # Reuse just the small RC fixture, rather than inheriting/rerunning the
    # unrelated existing flow suite. It runs zero annealing proposals.
    setUp = flow_fixtures.SimplifyFlowTests.setUp
    simplify = flow_fixtures.SimplifyFlowTests.simplify
    variant = flow_fixtures.SimplifyFlowTests.variant

    def test_auto_resolves_once_then_runs_one_simplification_without_relayout(self):
        from netlist2schematic.postprocess.simplify_budget import estimate_simplify_budget as estimate
        from netlist2schematic.postprocess.simplify_routes import simplify_routes as actual
        with patch('netlist2schematic.postprocess.simplify_routes.simplify_routes', wraps=actual) as simplify, \
                patch('netlist2schematic.postprocess.simplify_budget.estimate_simplify_budget', wraps=estimate) as estimator, \
                patch('netlist2schematic.routing.orthogonal_router.route_scene', side_effect=AssertionError('A* during saved simplify')), \
                patch('netlist2schematic.flow.run_flow._normal_result', side_effect=AssertionError('layout during saved simplify')):
            data = self.simplify(simplify_seconds='auto')
        self.assertEqual(1, simplify.call_count)
        self.assertEqual(1, estimator.call_count)
        effective = simplify.call_args.kwargs['seconds']
        self.assertGreaterEqual(effective, 5)
        self.assertLessEqual(effective, 30)
        self.assertEqual(effective, data['route_simplification']['budget_seconds'])
        selection = data['route_simplification']['budget_selection']
        self.assertEqual('auto', selection['mode'])
        self.assertEqual(effective, selection['selected_seconds'])
        self.assertIsInstance(selection['features'], dict)
        self.assertIn('input_routes_sha256', selection)
        self.assertEqual(512, data['route_simplification']['parameters']['max_candidates'])
        self.assertEqual(self.original['placement'], data['placement'])
        self.assertEqual(self.original['layout_grid'], data['layout_grid'])
        self.assertEqual(self.source_bytes, self.source.read_bytes())
        self.assertEqual(self.saved_bytes, self.saved.read_bytes())
        self.assertTrue(data['validation']['valid'])

    def test_feature_extraction_timeout_still_runs_only_one_bounded_simplification(self):
        from netlist2schematic.postprocess.simplify_routes import simplify_routes as actual
        expired = RoutingFailure('feature deadline', {'reason': 'quality_time_budget'})
        with patch('netlist2schematic.postprocess.simplify_budget.extract_features', side_effect=expired), \
                patch('netlist2schematic.postprocess.simplify_routes.simplify_routes', wraps=actual) as simplify:
            data = self.simplify(simplify_seconds='auto')
        self.assertEqual(1, simplify.call_count)
        self.assertEqual(30, simplify.call_args.kwargs['seconds'])
        selection = data['route_simplification']['budget_selection']
        self.assertEqual('time_budget', selection['feature_status'])
        self.assertIsNone(selection['features'])
        self.assertEqual(512, data['route_simplification']['parameters']['max_candidates'])
        self.assertTrue(data['validation']['valid'])

    def test_same_auto_result_replays_without_another_simplification(self):
        first = self.simplify(simplify_seconds='auto')
        saved = self.destination / 'small-placement.json'
        with patch('netlist2schematic.postprocess.simplify_routes.simplify_routes',
                   side_effect=AssertionError('same auto result ran simplification again')), \
                patch('netlist2schematic.postprocess.simplify_budget.estimate_simplify_budget',
                      side_effect=AssertionError('same auto result reran its estimator')):
            second = self.simplify(saved, simplify_seconds='auto',
                                   out_dir=self.directory / 'auto-replay')
        self.assertTrue(second['route_simplification']['verified_saved_replay'])
        self.assertEqual(first['routes'], second['routes'])
        self.assertEqual(first['metrics'], second['metrics'])
        self.assertEqual(first['quality'], second['quality'])

    def test_fixed_budget_is_exact_even_above_the_auto_cap(self):
        from netlist2schematic.postprocess.simplify_routes import simplify_routes as actual
        with patch('netlist2schematic.postprocess.simplify_routes.simplify_routes', wraps=actual) as simplify, \
                patch('netlist2schematic.postprocess.simplify_budget.estimate_simplify_budget',
                      side_effect=AssertionError('fixed budgets must not estimate')):
            data = self.simplify(simplify_seconds=31.25)
        self.assertEqual(1, simplify.call_count)
        self.assertEqual(31.25, simplify.call_args.kwargs['seconds'])
        self.assertEqual(31.25, data['route_simplification']['budget_seconds'])
        self.assertEqual('fixed', data['route_simplification']['budget_selection']['mode'])

    def test_fixed_result_does_not_replay_as_auto_with_the_same_effective_budget(self):
        from netlist2schematic.postprocess.simplify_routes import simplify_routes as actual
        first = self.simplify(simplify_seconds=5)
        saved = self.destination / 'small-placement.json'
        self.assertEqual(5, first['route_simplification']['budget_seconds'])
        with patch('netlist2schematic.postprocess.simplify_routes.simplify_routes', wraps=actual) as simplify:
            second = self.simplify(saved, simplify_seconds='auto',
                                   out_dir=self.directory / 'fixed-to-auto')
        self.assertEqual(1, simplify.call_count)
        self.assertNotIn('verified_saved_replay', second['route_simplification'])
        self.assertEqual(first['placement'], second['placement'])

    def test_rule_change_invalidates_saved_auto_replay(self):
        from netlist2schematic.postprocess.simplify_budget import estimate_simplify_budget as estimate
        from netlist2schematic.postprocess.simplify_routes import simplify_routes as actual
        first = self.simplify(simplify_seconds='auto')
        saved = self.destination / 'small-placement.json'
        with patch('netlist2schematic.postprocess.simplify_routes.simplify_routes', wraps=actual) as simplify, \
                patch('netlist2schematic.postprocess.simplify_budget.estimate_simplify_budget', wraps=estimate) as estimator:
            second = self.simplify(saved, simplify_seconds='auto',
                routing_options=RoutingOptions(crossing_cost=151),
                out_dir=self.directory / 'auto-new-rules')
        self.assertEqual(1, simplify.call_count)
        self.assertEqual(1, estimator.call_count)
        self.assertNotIn('verified_saved_replay', second['route_simplification'])
        self.assertEqual(first['placement'], second['placement'])

    def test_zero_and_off_skip_simplification(self):
        for kwargs in ({'simplify_seconds': 0},
                       {'simplify_seconds': 'auto', 'simplify_routes': 'off'}):
            with self.subTest(kwargs=kwargs), patch('netlist2schematic.postprocess.simplify_routes.simplify_routes',
                    side_effect=AssertionError('disabled simplification ran')), \
                    patch('netlist2schematic.postprocess.simplify_budget.estimate_simplify_budget',
                          side_effect=AssertionError('disabled simplification estimated work')):
                data = self.simplify(**kwargs)
            self.assertEqual(self.original['metrics'], data['metrics'])
            self.assertEqual('skipped', data['route_simplification']['status'])

    def test_auto_does_not_simplify_constructive_bus_routes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            original = run_flow.run(self.source, iterations=0, fallback='only',
                                    out_dir=self.directory / 'bus', simplify_routes='off')
        with patch('netlist2schematic.postprocess.simplify_routes.simplify_routes', side_effect=AssertionError('bus simplified')), \
                patch('netlist2schematic.postprocess.simplify_budget.estimate_simplify_budget',
                      side_effect=AssertionError('bus routes estimated simplification work')):
            data = self.simplify(self.directory / 'bus' / 'small-placement.json',
                                 simplify_seconds='auto')
        self.assertEqual('bus_fallback', data['routing_stage'])
        self.assertEqual('bus_fallback', data['route_simplification']['stop_reason'])
        self.assertEqual(original['routes'], data['routes'])
        self.assertEqual(original['power_connections'], data['power_connections'])

    def test_estimator_configuration_or_version_change_invalidates_auto_replay(self):
        from netlist2schematic.postprocess.simplify_budget import estimate_simplify_budget as estimate
        from netlist2schematic.postprocess.simplify_routes import simplify_routes as actual
        first = self.simplify(simplify_seconds='auto')
        for change in ('config', 'version'):
            with self.subTest(change=change):
                modified = deepcopy(first)
                selection = modified['route_simplification']['budget_selection']
                if change == 'config':
                    selection['config']['normalizers']['segments'] += 1
                else:
                    selection['estimator_version'] = 'route-complexity-prior'
                saved = self.directory / (change + '-placement.json')
                saved.write_text(json.dumps(modified), encoding='utf-8')
                with patch('netlist2schematic.postprocess.simplify_routes.simplify_routes', wraps=actual) as simplify, \
                        patch('netlist2schematic.postprocess.simplify_budget.estimate_simplify_budget', wraps=estimate) as estimator:
                    second = self.simplify(saved, simplify_seconds='auto',
                                           out_dir=self.directory / ('changed-' + change))
                self.assertEqual(1, simplify.call_count)
                self.assertEqual(1, estimator.call_count)
                self.assertNotIn('verified_saved_replay', second['route_simplification'])
                self.assertEqual(first['placement'], second['placement'])

    def test_legacy_fixed_report_without_selection_cannot_hit_auto_replay(self):
        from netlist2schematic.postprocess.simplify_budget import estimate_simplify_budget as estimate
        from netlist2schematic.postprocess.simplify_routes import simplify_routes as actual
        first = self.simplify(simplify_seconds=5)
        first['route_simplification'].pop('budget_selection', None)
        saved = self.directory / 'legacy-fixed-placement.json'
        saved.write_text(json.dumps(first), encoding='utf-8')
        with patch('netlist2schematic.postprocess.simplify_routes.simplify_routes', wraps=actual) as simplify, \
                patch('netlist2schematic.postprocess.simplify_budget.estimate_simplify_budget', wraps=estimate) as estimator:
            second = self.simplify(saved, simplify_seconds='auto',
                                   out_dir=self.directory / 'legacy-to-auto')
        self.assertEqual(1, simplify.call_count)
        self.assertEqual(1, estimator.call_count)
        self.assertNotIn('verified_saved_replay', second['route_simplification'])

    def test_legacy_fixed_report_without_selection_still_replays_as_same_fixed_budget(self):
        first = self.simplify(simplify_seconds=5)
        first['route_simplification'].pop('budget_selection', None)
        saved = self.directory / 'legacy-fixed-replay-placement.json'
        saved.write_text(json.dumps(first), encoding='utf-8')
        with patch('netlist2schematic.postprocess.simplify_routes.simplify_routes',
                   side_effect=AssertionError('unchanged legacy fixed result was simplified again')), \
                patch('netlist2schematic.postprocess.simplify_budget.estimate_simplify_budget',
                      side_effect=AssertionError('legacy fixed replay estimated an automatic budget')):
            second = self.simplify(saved, simplify_seconds=5,
                                   out_dir=self.directory / 'legacy-fixed-replayed')
        self.assertTrue(second['route_simplification']['verified_saved_replay'])
        self.assertEqual(first['routes'], second['routes'])
        self.assertEqual(first['metrics'], second['metrics'])
        self.assertEqual(first['quality'], second['quality'])

    def test_auto_report_cannot_hit_fixed_replay_even_when_both_budgets_are_five(self):
        from netlist2schematic.postprocess.simplify_routes import simplify_routes as actual
        first = self.simplify(simplify_seconds='auto')
        saved = self.destination / 'small-placement.json'
        self.assertEqual(5, first['route_simplification']['budget_seconds'])
        with patch('netlist2schematic.postprocess.simplify_routes.simplify_routes', wraps=actual) as simplify, \
                patch('netlist2schematic.postprocess.simplify_budget.estimate_simplify_budget',
                      side_effect=AssertionError('fixed replay request estimated a budget')):
            second = self.simplify(saved, simplify_seconds=5,
                                   out_dir=self.directory / 'auto-to-fixed')
        self.assertEqual(1, simplify.call_count)
        self.assertNotIn('verified_saved_replay', second['route_simplification'])


if __name__ == '__main__':
    unittest.main()
