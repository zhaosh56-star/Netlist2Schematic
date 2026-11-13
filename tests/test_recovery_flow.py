"""Public recovery stages, output protection and saved bus geometry."""
from __future__ import annotations

from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from netlist2schematic.flow import run_flow
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.common.sa_common import load_local
from netlist2schematic.common.sa_saved import load_placement


class RecoveryFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / 'small.cir'
        self.original = b'R1 input output 1k\r\nC1 output 0 1n\r\n.end\r\n'
        self.source.write_bytes(self.original)
        self.destination = self.directory / 'result'
        with contextlib.redirect_stdout(io.StringIO()):
            self.normal = run_flow._normal_result(self.source,
                load_local('place_and_route', 'place-and-route.py'), 'layered', 'structured',
                None, 0, 17, 10, '1', None)

    def generate(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return run_flow.run(self.source, iterations=0, seed=17,
                                out_dir=self.destination, **kwargs)

    def failure(self, *, context=True, checkpoint=False):
        failure = RoutingFailure('normal search exhausted', {'reason': 'time_budget'})
        if context:
            failure.retry_context = deepcopy(self.normal[:5])
            failure.retry_options = RoutingOptions()
        if checkpoint:
            # A private execution checkpoint must never reach output JSON.
            failure._rescue_context = {'marker': object()}
        return failure

    def artifacts(self):
        return {name: (self.destination / name).read_bytes() for name in
                ('small-layered.svg', 'small-clean.svg', 'small-placement.json')}

    def test_normal_success_never_attempts_either_recovery(self):
        with patch('netlist2schematic.flow.run_flow._normal_result', return_value=deepcopy(self.normal)), \
                patch('netlist2schematic.flow.run_flow._try_local_rescue', side_effect=AssertionError('extra rescue')), \
                patch('netlist2schematic.recovery.spacing_retry.retry_spacing', side_effect=AssertionError('extra spacing')), \
                patch('netlist2schematic.recovery.fallback_bus.create_fallback', side_effect=AssertionError('extra bus')):
            data = self.generate()
        self.assertEqual('normal', data['routing_stage'])
        self.assertEqual(['normal'], [s['stage'] for s in data['recovery']['stages']])
        self.assertEqual(self.original, self.source.read_bytes())

    def test_spacing_success_is_used_without_running_bus(self):
        result = deepcopy(self.normal)
        result[4]['routing_stage'] = 'spacing_retry'
        with patch('netlist2schematic.flow.run_flow._normal_result', side_effect=self.failure()), \
                patch('netlist2schematic.recovery.spacing_retry.retry_spacing', return_value=result) as retry, \
                patch('netlist2schematic.recovery.fallback_bus.create_fallback', side_effect=AssertionError('extra bus')):
            data = self.generate(spacing_scale=1.75, spacing_seconds=7)
        self.assertEqual('spacing_retry', data['routing_stage'])
        self.assertEqual(1, retry.call_count)
        self.assertEqual(1.75, retry.call_args.kwargs['scale'])
        self.assertEqual(7, retry.call_args.kwargs['seconds'])
        self.assertEqual(['failed', 'skipped', 'valid'], [s['status'] for s in data['recovery']['stages']])
        self.assertEqual('no_failure_checkpoint', data['recovery']['stages'][1]['reason'])

    def test_local_rescue_success_preserves_layout_and_skips_later_stages(self):
        result = deepcopy(self.normal)
        with patch('netlist2schematic.flow.run_flow._normal_result', side_effect=self.failure(checkpoint=True)), \
                patch('netlist2schematic.recovery.local_rescue.rescue_routing', return_value=result) as rescue, \
                patch('netlist2schematic.recovery.spacing_retry.retry_spacing', side_effect=AssertionError('extra spacing')), \
                patch('netlist2schematic.recovery.fallback_bus.create_fallback', side_effect=AssertionError('extra bus')):
            data = self.generate(rescue_seconds=7)
        self.assertEqual(1, rescue.call_count)
        self.assertEqual(7, rescue.call_args.kwargs['seconds'])
        self.assertEqual('local_rescue', data['routing_stage'])
        self.assertEqual(['normal', 'local_rescue'],
                         [s['stage'] for s in data['recovery']['stages']])
        self.assertEqual(['failed', 'valid'], [s['status'] for s in data['recovery']['stages']])
        self.assertNotIn('_rescue_context', json.dumps(data))
        for comp in self.normal[1]:
            i = self.normal[3][comp['id']]
            self.assertEqual(tuple(data['placement'][comp['id']][key] for key in ('row', 'col')),
                             tuple(self.normal[0].cell_of[i]))
        self.assertEqual(self.original, self.source.read_bytes())

    def test_explicit_local_rescue_works_with_fallback_off(self):
        with patch('netlist2schematic.flow.run_flow._normal_result', side_effect=self.failure(checkpoint=True)), \
                patch('netlist2schematic.recovery.local_rescue.rescue_routing', return_value=deepcopy(self.normal)) as rescue, \
                patch('netlist2schematic.recovery.spacing_retry.retry_spacing', side_effect=AssertionError('disabled')), \
                patch('netlist2schematic.recovery.fallback_bus.create_fallback', side_effect=AssertionError('disabled')):
            data = self.generate(fallback='off', local_rescue='auto')
        self.assertEqual(1, rescue.call_count)
        self.assertEqual('local_rescue', data['routing_stage'])
        self.assertEqual('off', data['recovery']['policy'])

    def test_failed_local_rescue_is_transactional_before_spacing(self):
        failure = self.failure(checkpoint=True)
        original_cells = deepcopy(failure.retry_context[0].cell_of)

        def failed_rescue(context, checkpoint, **kwargs):
            context[0].cell_of[0] = (99, 99)
            context[4]['uncommitted'] = True
            checkpoint.clear()
            raise RoutingFailure('still blocked', {'reason': 'no_safe_goal'})

        with patch('netlist2schematic.flow.run_flow._normal_result', side_effect=failure), \
                patch('netlist2schematic.recovery.local_rescue.rescue_routing', side_effect=failed_rescue), \
                patch('netlist2schematic.recovery.spacing_retry.retry_spacing', return_value=deepcopy(self.normal)) as spacing:
            data = self.generate(rescue_seconds=4, spacing_seconds=9)
        self.assertEqual(original_cells, spacing.call_args.args[0][0].cell_of)
        self.assertNotIn('uncommitted', spacing.call_args.args[0][4])
        self.assertEqual(original_cells, failure.retry_context[0].cell_of)
        self.assertEqual(9, spacing.call_args.kwargs['seconds'])
        self.assertEqual(['normal', 'local_rescue', 'spacing_retry'],
                         [s['stage'] for s in data['recovery']['stages']])
        self.assertEqual('no_safe_goal', data['recovery']['stages'][1]['diagnostics']['reason'])

    def test_zero_or_disabled_local_rescue_never_calls_it(self):
        for kwargs in ({'rescue_seconds': 0}, {'local_rescue': 'off'}):
            with self.subTest(kwargs=kwargs), \
                    patch('netlist2schematic.flow.run_flow._normal_result', side_effect=self.failure(checkpoint=True)), \
                    patch('netlist2schematic.recovery.local_rescue.rescue_routing', side_effect=AssertionError('disabled')), \
                    patch('netlist2schematic.recovery.spacing_retry.retry_spacing', return_value=deepcopy(self.normal)):
                data = self.generate(**kwargs)
            self.assertEqual('disabled', data['recovery']['stages'][1]['reason'])

    def test_source_edit_during_local_rescue_stops_before_spacing(self):
        def edit(*args, **kwargs):
            self.source.write_bytes(self.original + b'* changed during rescue\n')
            raise RoutingFailure('still blocked', {'reason': 'no_path'})

        with patch('netlist2schematic.flow.run_flow._normal_result', side_effect=self.failure(checkpoint=True)), \
                patch('netlist2schematic.recovery.local_rescue.rescue_routing', side_effect=edit), \
                patch('netlist2schematic.recovery.spacing_retry.retry_spacing', side_effect=AssertionError('changed source')):
            with self.assertRaisesRegex(ValueError, 'Source netlist changed'):
                self.generate()

    def test_invalid_power_schema_does_not_enter_recovery(self):
        failure = run_flow.RoutingValidationError('invalid saved power', {'reason': 'power_connections'})
        with patch('netlist2schematic.flow.run_flow._normal_result', side_effect=failure), \
                patch('netlist2schematic.flow.run_flow._try_local_rescue', side_effect=AssertionError('invalid input')), \
                patch('netlist2schematic.recovery.fallback_bus.create_fallback', side_effect=AssertionError('invalid input')):
            with self.assertRaises(run_flow.RoutingValidationError):
                self.generate()

    def test_invalid_rescue_arguments_are_rejected_before_routing(self):
        for kwargs in ({'local_rescue': 'unknown'}, {'rescue_seconds': -1},
                       {'rescue_seconds': float('nan')}, {'rescue_seconds': True}):
            with self.subTest(kwargs=kwargs), \
                    patch('netlist2schematic.flow.run_flow._normal_result', side_effect=AssertionError('invalid options')):
                with self.assertRaises(ValueError):
                    self.generate(**kwargs)

    def test_local_only_failure_preserves_success_artifacts_and_both_diagnostics(self):
        self.generate(fallback='only')
        previous = self.artifacts()
        with patch('netlist2schematic.flow.run_flow._normal_result', side_effect=self.failure(checkpoint=True)), \
                patch('netlist2schematic.recovery.local_rescue.rescue_routing', side_effect=RoutingFailure(
                    'no safe port', {'reason': 'no_safe_goal'})):
            with self.assertRaises(RoutingFailure):
                self.generate(fallback='off', local_rescue='auto')
        self.assertEqual(previous, self.artifacts())
        diagnostic = json.loads((self.destination / 'routing-diagnostics.json').read_text(encoding='utf-8'))
        self.assertEqual(['normal', 'local_rescue'],
                         [s['stage'] for s in diagnostic['diagnostics']['stages']])

    def test_failed_normal_and_spacing_use_verified_bus_and_keep_diagnostics(self):
        with patch('netlist2schematic.flow.run_flow._normal_result', side_effect=self.failure()), \
                patch('netlist2schematic.recovery.spacing_retry.retry_spacing', side_effect=RoutingFailure(
                    'spacing blocked', {'reason': 'no_path', 'net': 'output'})) as retry:
            data = self.generate()
        self.assertEqual(1, retry.call_count)
        self.assertEqual('bus_fallback', data['routing_stage'])
        self.assertEqual('constructive-bus-v1', data['validation']['router'])
        self.assertTrue(data['validation']['valid'])
        self.assertEqual(['normal', 'local_rescue', 'spacing_retry', 'bus_fallback'],
                         [s['stage'] for s in data['recovery']['stages']])
        self.assertEqual('no_path', data['recovery']['stages'][2]['diagnostics']['reason'])
        self.assertEqual(self.original, self.source.read_bytes())

    def test_no_attempted_layout_skips_spacing(self):
        with patch('netlist2schematic.flow.run_flow._normal_result', side_effect=self.failure(context=False)), \
                patch('netlist2schematic.recovery.spacing_retry.retry_spacing', side_effect=AssertionError('no layout')):
            data = self.generate()
        self.assertEqual('no_attempted_layout', data['recovery']['stages'][1]['reason'])
        self.assertEqual('no_attempted_layout', data['recovery']['stages'][2]['reason'])
        self.assertEqual('bus_fallback', data['routing_stage'])

    def test_zero_spacing_budget_skips_retry(self):
        with patch('netlist2schematic.flow.run_flow._normal_result', side_effect=self.failure()), \
                patch('netlist2schematic.recovery.spacing_retry.retry_spacing', side_effect=AssertionError('disabled')):
            data = self.generate(spacing_seconds=0)
        self.assertEqual('disabled', data['recovery']['stages'][2]['reason'])

    def test_only_skips_normal_and_spacing_and_uses_separate_bus_deadline(self):
        from netlist2schematic.recovery import fallback_bus
        actual = fallback_bus.create_fallback
        with patch('netlist2schematic.flow.run_flow._normal_result', side_effect=AssertionError('normal called')), \
                patch('netlist2schematic.flow.run_flow._try_local_rescue', side_effect=AssertionError('rescue called')), \
                patch('netlist2schematic.recovery.spacing_retry.retry_spacing', side_effect=AssertionError('spacing called')), \
                patch('netlist2schematic.recovery.fallback_bus.create_fallback', wraps=actual) as bus:
            data = self.generate(fallback='only', quality_seconds=.001, fallback_seconds=10)
        self.assertEqual(1, bus.call_count)
        self.assertGreater(bus.call_args.kwargs['deadline'], 0)
        self.assertEqual('forced', data['fallback']['trigger'])
        self.assertEqual(10, data['fallback']['budget_seconds'])
        self.assertEqual(1, len(data['recovery']['stages']))

    def test_off_preserves_failure_and_previous_success_files(self):
        self.generate(fallback='only')
        originals = self.artifacts()
        with patch('netlist2schematic.flow.run_flow._normal_result', side_effect=self.failure()), \
                patch('netlist2schematic.recovery.fallback_bus.create_fallback', side_effect=AssertionError('disabled')):
            with self.assertRaises(RoutingFailure):
                self.generate(fallback='off')
        self.assertEqual(originals, self.artifacts())
        diagnostic = json.loads((self.destination / 'routing-diagnostics.json').read_text(encoding='utf-8'))
        self.assertEqual('time_budget', diagnostic['diagnostics']['reason'])

    def test_candidate_replay_defaults_to_off_but_explicit_auto_recovers(self):
        with patch('netlist2schematic.flow.run_flow._normal_result', side_effect=self.failure(context=False)):
            with self.assertRaises(RoutingFailure):
                self.generate(candidate_id='1')
            data = self.generate(candidate_id='1', fallback='auto')
        self.assertEqual('bus_fallback', data['routing_stage'])
        self.assertFalse((self.destination / 'routing-diagnostics.json').exists())

    def test_nonrouting_error_does_not_start_recovery(self):
        with patch('netlist2schematic.flow.run_flow._normal_result', side_effect=ValueError('invalid symbol resources')), \
                patch('netlist2schematic.recovery.fallback_bus.create_fallback', side_effect=AssertionError('masked error')):
            with self.assertRaisesRegex(ValueError, 'invalid symbol'):
                self.generate()
        self.assertFalse(self.destination.exists())

    def test_source_change_during_normal_failure_stops_recovery(self):
        def change_source(*args, **kwargs):
            self.source.write_bytes(self.original + b'* edited while running\r\n')
            raise self.failure()
        with patch('netlist2schematic.flow.run_flow._normal_result', side_effect=change_source), \
                patch('netlist2schematic.recovery.fallback_bus.create_fallback', side_effect=AssertionError('changed source')):
            with self.assertRaisesRegex(ValueError, 'Source netlist changed'):
                self.generate()
        self.assertFalse(self.destination.exists())

    def test_all_stages_failed_preserve_previous_success_and_record_all_reasons(self):
        self.generate(fallback='only')
        originals = self.artifacts()
        with patch('netlist2schematic.flow.run_flow._normal_result', side_effect=self.failure()), \
                patch('netlist2schematic.recovery.spacing_retry.retry_spacing', side_effect=RoutingFailure('spacing', {'reason': 'no_path'})), \
                patch('netlist2schematic.recovery.fallback_bus.create_fallback', side_effect=RoutingFailure('bus timeout', {'reason': 'quality_time_budget'})):
            with self.assertRaises(RoutingFailure):
                self.generate()
        self.assertEqual(originals, self.artifacts())
        diagnostic = json.loads((self.destination / 'routing-diagnostics.json').read_text(encoding='utf-8'))
        self.assertEqual('recovery_failed', diagnostic['diagnostics']['reason'])
        self.assertEqual(['normal', 'local_rescue', 'spacing_retry', 'bus_fallback'],
                         [s['stage'] for s in diagnostic['diagnostics']['stages']])
        self.assertEqual(self.original, self.source.read_bytes())

    def test_saved_bus_redraw_and_fixed_reroute_preserve_all_geometry(self):
        data = self.generate(fallback='only')
        saved = self.destination / 'small-placement.json'
        renderer = load_local('render_clean', 'render-clean.py')
        with patch('netlist2schematic.routing.orthogonal_router.route_scene', side_effect=AssertionError('A* in saved bus')), \
                contextlib.redirect_stdout(io.StringIO()):
            renderer.render(saved, self.directory / 'independent.svg')
            repeated = run_flow.reroute_placement(saved, out_dir=self.directory / 'rerouted')
        self.assertEqual((self.destination / 'small-clean.svg').read_bytes(),
                         (self.directory / 'independent.svg').read_bytes())
        for field in ('placement', 'layout_grid', 'power_connections', 'routes', 'metrics'):
            self.assertEqual(data[field], repeated[field], field)
        self.assertEqual('constructive-bus-v1', repeated['validation']['router'])
        self.assertIn(b'bus fallback', (self.destination / 'small-clean.svg').read_bytes())

    def test_saved_bus_cannot_silently_fall_back_to_legacy_power_stubs(self):
        data = self.generate(fallback='only')
        for invalid in ('missing', None, 'not a list'):
            with self.subTest(invalid=invalid):
                altered = deepcopy(data)
                if invalid == 'missing':
                    altered.pop('power_connections')
                else:
                    altered['power_connections'] = invalid
                path = self.directory / 'invalid.json'
                path.write_text(json.dumps(altered), encoding='utf-8')
                with self.assertRaises(ValueError):
                    load_placement(path)

    def test_changed_source_values_are_rejected_before_independent_redraw(self):
        self.generate(fallback='only')
        saved = self.destination / 'small-placement.json'
        renderer = load_local('render_clean', 'render-clean.py')
        destination = self.directory / 'protected.svg'
        destination.write_bytes(b'previous drawing')
        self.source.write_bytes(self.original.replace(b'1k', b'2k'))
        with self.assertRaisesRegex(ValueError, 'source_sha256'):
            load_placement(saved)
        with self.assertRaisesRegex(ValueError, 'source_sha256'):
            renderer.render(saved, destination)
        self.assertEqual(b'previous drawing', destination.read_bytes())

    def test_router_only_bus_marker_still_uses_strict_saved_power_contract(self):
        data = self.generate(fallback='only')
        data.pop('layout_style')
        data.pop('routing_stage')
        saved = self.directory / 'router-only.json'
        saved.write_text(json.dumps(data), encoding='utf-8')
        self.assertTrue(run_flow.is_bus_layout(data))
        renderer = load_local('render_clean', 'render-clean.py')
        with contextlib.redirect_stdout(io.StringIO()):
            renderer.render(saved, self.directory / 'router-only.svg')
        self.assertIn(b'bus fallback', (self.directory / 'router-only.svg').read_bytes())
        data.pop('power_connections')
        saved.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'power_connections'):
            load_placement(saved)

    def test_saved_bus_requires_physical_grid_even_without_power_connections(self):
        self.source.write_bytes(b'R1 a b 1k\nR2 b c 2k\n')
        data = self.generate(fallback='only')
        self.assertEqual([], data['power_connections'])
        data.pop('layout_grid')
        saved = self.directory / 'missing-grid.json'
        saved.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'layout_grid'):
            load_placement(saved)

    def test_cli_rejects_automatic_relayout_for_fixed_saved_input(self):
        with self.assertRaises(SystemExit) as error, contextlib.redirect_stderr(io.StringIO()):
            run_flow.main(['--reroute-placement', 'saved.json', '--fallback', 'auto'])
        self.assertEqual(2, error.exception.code)

    def test_fixed_reroute_can_explicitly_rescue_without_relayout(self):
        with patch('netlist2schematic.flow.run_flow._normal_result', return_value=deepcopy(self.normal)):
            saved_data = self.generate(fallback='off')
        saved = self.destination / 'small-placement.json'
        failure = self.failure(checkpoint=True)

        def rescued(context, checkpoint, **kwargs):
            return (*deepcopy(context), deepcopy(self.normal[5]))

        with patch('netlist2schematic.flow.run_flow.final_route', side_effect=failure), \
                patch('netlist2schematic.recovery.local_rescue.rescue_routing', side_effect=rescued) as rescue, \
                patch('netlist2schematic.recovery.spacing_retry.retry_spacing', side_effect=AssertionError('fixed coordinates')), \
                patch('netlist2schematic.recovery.fallback_bus.create_fallback', side_effect=AssertionError('fixed coordinates')), \
                contextlib.redirect_stdout(io.StringIO()):
            data = run_flow.reroute_placement(saved, out_dir=self.directory / 'rerouted',
                                               local_rescue='auto', rescue_seconds=8)
        self.assertEqual(8, rescue.call_args.kwargs['seconds'])
        self.assertEqual('local_rescue', data['routing_stage'])
        self.assertEqual(saved_data['placement'], data['placement'])
        self.assertEqual(saved_data['layout_grid'], data['layout_grid'])
        self.assertEqual(self.original, self.source.read_bytes())

    def test_saved_reroute_defaults_to_no_recovery(self):
        with patch('netlist2schematic.flow.run_flow._normal_result', return_value=deepcopy(self.normal)):
            self.generate(fallback='off')
        saved = self.destination / 'small-placement.json'
        with patch('netlist2schematic.flow.run_flow.final_route', side_effect=self.failure(checkpoint=True)), \
                patch('netlist2schematic.flow.run_flow._try_local_rescue', side_effect=AssertionError('default disabled')), \
                self.assertRaises(RoutingFailure):
            run_flow.reroute_placement(saved, out_dir=self.directory / 'rerouted')

    def test_cli_routes_rescue_options_for_saved_and_new_input(self):
        with patch('netlist2schematic.flow.run_flow.run') as run:
            run_flow.main([str(self.source), '--local-rescue', 'auto', '--rescue-seconds', '5',
                           '--fallback', 'off'])
        self.assertEqual('auto', run.call_args.kwargs['local_rescue'])
        self.assertEqual(5, run.call_args.kwargs['rescue_seconds'])
        with patch('netlist2schematic.flow.run_flow.reroute_placement') as reroute:
            run_flow.main(['--reroute-placement', 'saved.json', '--local-rescue', 'auto',
                           '--rescue-seconds', '9'])
        self.assertEqual('auto', reroute.call_args.kwargs['local_rescue'])
        self.assertEqual(9, reroute.call_args.kwargs['rescue_seconds'])


if __name__ == '__main__':
    unittest.main()
