"""Safe postprocessing at the CLI, saved-route and publication boundaries."""
from __future__ import annotations

from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import run_flow
from sa_common import load_local


class SimplifyFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / 'small.cir'
        self.source.write_bytes(b'* original bytes\r\nR1 input output 1k\r\nC1 output 0 1n\r\n.end\r\n')
        self.source_bytes = self.source.read_bytes()
        self.initial = self.directory / 'initial'
        with contextlib.redirect_stdout(io.StringIO()):
            self.original = run_flow.run(self.source, iterations=0, seed=17,
                candidate_id='1', quality_seconds=10, fallback='off',
                out_dir=self.initial, simplify_routes='off')
        self.saved = self.initial / 'small-placement.json'
        self.saved_bytes = self.saved.read_bytes()
        self.destination = self.directory / 'simplified'

    def simplify(self, saved=None, **kwargs):
        kwargs.setdefault('out_dir', self.destination)
        with contextlib.redirect_stdout(io.StringIO()):
            return run_flow.simplify_placement(saved or self.saved, **kwargs)

    def variant(self, **changes):
        data = deepcopy(self.original)
        data.update(changes)
        path = self.directory / 'variant-placement.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def test_saved_simplification_never_enters_layout_astar_or_recovery(self):
        par = load_local('place_and_route', 'place-and-route.py')
        with patch.object(par, 'sa', side_effect=AssertionError('SA')), \
                patch('orthogonal_router.route_scene', side_effect=AssertionError('A*')), \
                patch('run_flow._normal_result', side_effect=AssertionError('layout')), \
                patch('run_flow._try_local_rescue', side_effect=AssertionError('rescue')), \
                patch('fallback_bus.create_fallback', side_effect=AssertionError('bus')):
            data = self.simplify()
        self.assertTrue(data['validation']['valid'])
        self.assertEqual(self.original['routing_stage'], data['routing_stage'])
        self.assertEqual(self.original['placement'], data['placement'])
        self.assertEqual(self.original['layout_grid'], data['layout_grid'])
        self.assertEqual(self.saved_bytes, self.saved.read_bytes())
        self.assertEqual(self.source_bytes, self.source.read_bytes())
        self.assertEqual(self.original['validation']['search'], data['validation']['search'])

    def test_simplification_off_and_zero_budget_keep_original_routes(self):
        for kwargs in ({'simplify_routes': 'off'}, {'simplify_seconds': 0}):
            with self.subTest(kwargs=kwargs), \
                    patch('simplify_routes.simplify_routes', side_effect=AssertionError('disabled')):
                data = self.simplify(**kwargs)
            self.assertEqual(json.loads(json.dumps(self.original['routes'])),
                             json.loads(json.dumps(data['routes'])))
            self.assertEqual(self.original['metrics'], data['metrics'])
            self.assertEqual('skipped', data['route_simplification']['status'])

    def test_false_saved_valid_flag_does_not_replace_geometric_validation(self):
        validation = deepcopy(self.original['validation'])
        validation['valid'] = False
        data = self.simplify(self.variant(validation=validation))
        self.assertTrue(data['validation']['valid'])

    def test_disconnected_routes_with_true_flag_are_rejected_and_previous_output_survives(self):
        self.simplify()
        old = {path.name: path.read_bytes() for path in self.destination.glob('*')}
        validation = deepcopy(self.original['validation'])
        validation['valid'] = True
        broken = self.variant(routes={}, validation=validation)
        with self.assertRaises(ValueError), \
                patch('simplify_routes.simplify_routes', side_effect=AssertionError('invalid input')):
            self.simplify(broken)
        for name, value in old.items():
            self.assertEqual(value, (self.destination / name).read_bytes())

    def test_saved_routes_are_required_and_malformed_json_is_rejected(self):
        absent = deepcopy(self.original)
        absent.pop('routes')
        path = self.directory / 'missing-placement.json'
        for content in (json.dumps(absent), '{broken', json.dumps({**self.original, 'routes': None}),
                        json.dumps({**self.original, 'routes': {'output': None}})):
            with self.subTest(content=content[:40]):
                path.write_text(content, encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.simplify(path)
        self.assertEqual(self.saved_bytes, self.saved.read_bytes())

    def test_source_netlist_change_is_rejected_before_simplification(self):
        self.source.write_bytes(self.source_bytes.replace(b'1k', b'2k'))
        with patch('simplify_routes.simplify_routes', side_effect=AssertionError('changed source')), \
                self.assertRaisesRegex(ValueError, 'source_sha256'):
            self.simplify()
        self.assertFalse(self.destination.exists())

    def test_input_files_changed_during_postprocessing_are_not_published(self):
        from simplify_routes import simplify_routes as actual
        for target in (self.source, self.saved):
            with self.subTest(target=target.name):
                self.source.write_bytes(self.source_bytes)
                self.saved.write_bytes(self.saved_bytes)
                self.simplify(simplify_routes='off')
                old = {path.name: path.read_bytes() for path in self.destination.glob('*')}

                def changing(*args, **kwargs):
                    result = actual(*args, **kwargs)
                    target.write_bytes(target.read_bytes() + b'\n')
                    return result

                with patch('simplify_routes.simplify_routes', side_effect=changing), \
                        self.assertRaisesRegex(ValueError, 'changed'):
                    self.simplify()
                for name, value in old.items():
                    self.assertEqual(value, (self.destination / name).read_bytes())

    def test_default_saved_result_replay_uses_verified_fingerprint(self):
        first = self.simplify()
        saved = self.destination / 'small-placement.json'
        with patch('simplify_routes.simplify_routes', side_effect=AssertionError('same verified result')):
            second = self.simplify(saved, out_dir=self.directory / 'replayed')
        self.assertTrue(second['route_simplification']['verified_saved_replay'])
        self.assertEqual(first['routes'], second['routes'])
        self.assertEqual(first['metrics'], second['metrics'])
        self.assertEqual(first['quality'], second['quality'])

    def test_changed_budget_and_rule_invalidate_replay(self):
        first = self.simplify()
        saved = self.destination / 'small-placement.json'
        from simplify_routes import simplify_routes as actual
        from routing_rules import RoutingOptions
        for kwargs in ({'simplify_seconds': 4},
                       {'routing_options': RoutingOptions(crossing_cost=151)}):
            with self.subTest(kwargs=kwargs), \
                    patch('simplify_routes.simplify_routes', wraps=actual) as simplify:
                data = self.simplify(saved, out_dir=self.directory / 'changed', **kwargs)
            self.assertEqual(1, simplify.call_count)
            self.assertNotIn('verified_saved_replay', data['route_simplification'])
        self.assertEqual(first['placement'], data['placement'])

    def test_previous_algorithm_version_does_not_skip_new_simplification(self):
        from simplify_routes import VERSION, simplify_routes as actual
        first = self.simplify()
        saved = self.directory / 'previous-placement.json'
        for previous in ('orthogonal-shortcuts-v1', 'orthogonal-shortcuts-v2', 'orthogonal-shortcuts-v3',
                         'orthogonal-shortcuts-v4'):
            with self.subTest(version=previous):
                first['route_simplification']['version'] = previous
                saved.write_text(json.dumps(first), encoding='utf-8')
                with patch('simplify_routes.simplify_routes', wraps=actual) as simplify:
                    data = self.simplify(saved, out_dir=self.directory / 'upgraded')
                self.assertEqual(1, simplify.call_count)
                self.assertEqual(VERSION, data['route_simplification']['version'])
                self.assertNotIn('verified_saved_replay', data['route_simplification'])
                self.assertTrue(data['validation']['valid'])
                self.assertEqual(first['placement'], data['placement'])

    def test_publication_preserves_operation_trace_and_its_final_incumbent(self):
        data = self.simplify()
        saved = json.loads((self.destination / 'small-placement.json').read_text(encoding='utf-8'))
        report = saved['route_simplification']
        self.assertEqual(['chain_shortcut', 'leaf_reattachment', 'three_segment_shortcut',
                          'track_compaction'],
                         report['operation_order'])
        self.assertEqual('preserve_v4_then_track_compaction_refinement', report['scheduling'])
        self.assertEqual(['chain_warmup', 'coordinated_refinement', 'three_segment_refinement',
                          'track_compaction_refinement'],
                         report['phase_order'])
        self.assertTrue(report['warmup_completed'])
        self.assertTrue(report['v3_completed'])
        self.assertTrue(report['v4_completed'])
        for key, total in report['candidate_counts'].items():
            self.assertEqual(total, sum(counts[key] for counts in report['operations'].values()))
        if report['accepted_changes']:
            self.assertEqual(saved['metrics'], report['accepted_changes'][-1]['after_metrics'])
        self.assertEqual(data['metrics'], saved['metrics'])
        self.assertEqual(saved['metrics'], report['after_metrics'])
        self.assertEqual(self.original['validation']['search'], saved['validation']['search'])

    def test_quality_is_final_and_original_candidate_records_are_retained(self):
        original_quality = deepcopy(self.original['quality'])
        original_quality['score'] = -12345
        saved = self.variant(quality=original_quality)
        data = self.simplify(saved)
        report = data['route_simplification']
        self.assertEqual(original_quality, report['quality_before'])
        self.assertEqual(original_quality['candidates'], data['quality']['candidates'])
        self.assertNotEqual(-12345, data['quality']['score'])
        self.assertEqual(data['quality']['details']['score'], data['quality']['score'])
        self.assertEqual(data['metrics']['wirelength'], data['quality']['details']['wirelength'])
        self.assertEqual('before_route_simplification', data['quality']['candidate_metrics_stage'])

    def test_normal_run_and_fixed_reroute_default_to_safe_postprocessing(self):
        from simplify_routes import simplify_routes as actual
        with patch('simplify_routes.simplify_routes', wraps=actual) as simplify, \
                contextlib.redirect_stdout(io.StringIO()):
            first = run_flow.run(self.source, iterations=0, seed=17, candidate_id='1',
                                fallback='off', out_dir=self.directory / 'safe-run')
            second = run_flow.reroute_placement(self.saved, out_dir=self.directory / 'safe-reroute')
        self.assertEqual(2, simplify.call_count)
        self.assertEqual('safe', first['route_simplification']['mode'])
        self.assertEqual('safe', second['route_simplification']['mode'])

    def test_tiny_budget_returns_validated_incumbent(self):
        data = self.simplify(simplify_seconds=1e-12)
        self.assertTrue(data['validation']['valid'])
        self.assertEqual('time_budget', data['route_simplification']['stop_reason'])
        self.assertEqual(json.loads(json.dumps(self.original['routes'])),
                         json.loads(json.dumps(data['routes'])))

    def test_bus_is_revalidated_but_not_simplified(self):
        with contextlib.redirect_stdout(io.StringIO()):
            data = run_flow.run(self.source, iterations=0, fallback='only',
                                out_dir=self.directory / 'bus', simplify_routes='off')
        saved = self.directory / 'bus' / 'small-placement.json'
        with patch('simplify_routes.simplify_routes', side_effect=AssertionError('bus skipped')):
            simplified = self.simplify(saved)
        self.assertEqual('bus_fallback', simplified['routing_stage'])
        self.assertEqual('bus_fallback', simplified['route_simplification']['stop_reason'])
        self.assertEqual(json.loads(json.dumps(data['routes'])),
                         json.loads(json.dumps(simplified['routes'])))
        self.assertEqual(data['power_connections'], simplified['power_connections'])

    def test_svg_and_independent_redraw_use_saved_final_routes(self):
        data = self.simplify()
        saved = self.destination / 'small-placement.json'
        renderer = load_local('render_clean', 'render-clean.py')
        with patch('simplify_routes.simplify_routes', side_effect=AssertionError('redraw must not simplify')), \
                contextlib.redirect_stdout(io.StringIO()):
            renderer.render(saved, self.directory / 'redraw.svg')
        self.assertEqual((self.destination / 'small-clean.svg').read_bytes(),
                         (self.directory / 'redraw.svg').read_bytes())
        namespace = {'svg': 'http://www.w3.org/2000/svg'}
        extract = lambda path: [(part.attrib['data-net'], part.attrib['points'])
            for part in ET.parse(path).getroot().findall('svg:polyline[@data-net]', namespace)]
        self.assertEqual(extract(self.destination / 'small-clean.svg'),
                         extract(self.destination / 'small-layered.svg'))
        self.assertEqual(data['metrics'], data['route_simplification']['after_metrics'])

    def test_output_cannot_overwrite_saved_input(self):
        with self.assertRaisesRegex(ValueError, 'differ'):
            self.simplify(out_dir=self.initial)
        self.assertEqual(self.saved_bytes, self.saved.read_bytes())

    def test_cli_choices_and_budget_validation(self):
        invalid = ([], [str(self.source), '--simplify-placement', str(self.saved)],
                   ['--reroute-placement', str(self.saved), '--simplify-placement', str(self.saved)],
                   ['--simplify-placement', str(self.saved), '--fallback', 'auto'],
                   ['--simplify-placement', str(self.saved), '--local-rescue', 'auto'])
        for args in invalid:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as caught:
                run_flow.main(args)
            self.assertEqual(2, caught.exception.code)
        with patch('run_flow.simplify_placement') as simplify:
            run_flow.main(['--simplify-placement', str(self.saved), '--simplify-routes', 'off',
                           '--simplify-seconds', '2'])
        self.assertEqual('off', simplify.call_args.kwargs['simplify_routes'])
        self.assertEqual(2, simplify.call_args.kwargs['simplify_seconds'])
        for kwargs in ({'simplify_routes': 'unknown'}, {'simplify_seconds': -1},
                       {'simplify_seconds': float('nan')}, {'simplify_seconds': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.simplify(**kwargs)


if __name__ == '__main__':
    unittest.main()
