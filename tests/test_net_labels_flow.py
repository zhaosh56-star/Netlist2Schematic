"""Saved-route, optional-flow and immutable publication boundaries for net labels."""
from __future__ import annotations

import builtins
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import net_labels
import run_flow
from sa_common import ROOT, load_local


class NetLabelsFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory(prefix='net-label-flow-')
        cls.addClassCleanup(cls.fixture.cleanup)
        cls.root = Path(cls.fixture.name)
        cls.source = cls.root / 'small.cir'
        cls.include = cls.root / 'empty.inc'
        cls.include.write_bytes(b'')
        cls.source.write_bytes(b'* immutable input\r\n.include "empty.inc"\r\n'
                               b'R1 input output 1k\r\nC1 output 0 1n\r\n.end\r\n')
        cls.source_bytes = cls.source.read_bytes()
        cls.resources = cls.root / 'resources'
        shutil.copytree(ROOT / 'analog-canvas-main/packages/components/definitions',
                        cls.resources / 'packages/components/definitions')
        cls.initial = cls.root / 'original'
        cls.default_plugin_imports = []
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name in ('net_labels', 'net_label_core'):
                cls.default_plugin_imports.append(name)
                raise AssertionError('Default flow must bypass the net-label plugin')
            return original_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=guarded_import), redirect_stdout(io.StringIO()):
            cls.original = run_flow.run(cls.source, iterations=0, seed=17,
                candidate_id='1', quality_seconds=10, fallback='off',
                analog_canvas_root=cls.resources, out_dir=cls.initial,
                simplify_routes='off')
        cls.saved = cls.initial / 'small-placement.json'
        cls.saved_bytes = cls.saved.read_bytes()
        cls.original = json.loads(cls.saved_bytes)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=self.root, prefix='case-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.destination = self.directory / 'views'

    def _generate(self, path=None, **kwargs):
        kwargs.setdefault('out_dir', self.destination)
        with redirect_stdout(io.StringIO()):
            return net_labels.generate(path or self.saved, **kwargs)

    def _redraw(self, path, **kwargs):
        kwargs.setdefault('out_dir', self.directory / 'redrawn')
        with redirect_stdout(io.StringIO()):
            return net_labels.redraw(path, **kwargs)

    def _variant(self, **changes):
        data = deepcopy(self.original)
        data.update(changes)
        path = self.directory / 'variant-placement.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def _tamper(self, manifest, update, *, rehash=True):
        value = deepcopy(manifest)
        update(value)
        if rehash:
            value['view_sha256'] = net_labels._digest(value['view'])
        path = self.directory / 'tampered-netlabels.json'
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def _mutate_file(self, path, contents):
        path = Path(path)
        before = path.read_bytes()
        self.addCleanup(path.write_bytes, before)
        path.write_bytes(contents)

    def _no_search(self, stack):
        par = load_local('place_and_route', 'place-and-route.py')
        for name in ('orthogonal_router.route_scene', 'orthogonal_router._find_path',
                     'sa_routing.final_route', 'run_flow.final_route',
                     'run_flow._normal_result', 'run_flow._try_local_rescue',
                     'structured_placement._anneal', 'fallback_bus.create_fallback',
                     'simplify_routes.simplify_routes'):
            stack.enter_context(patch(name, side_effect=AssertionError(name)))
        stack.enter_context(patch.object(par, 'sa', side_effect=AssertionError('baseline SA')))

    def _finish(self, *, policy='off'):
        context = net_labels._load_context(self.saved)
        data, _, par, placement, comps, nets, index, routed, metrics, validation, *_ = context
        placement.presentation_style = data.get('presentation', {}).get('style', 'legacy')
        with redirect_stdout(io.StringIO()):
            return run_flow._finish(deepcopy(data), par, placement, comps, nets, index,
                'small', self.directory / 'flow', routed_result=(routed, metrics, validation),
                simplify_routes='off', net_labels=policy)

    def test_default_full_flow_does_not_import_the_plugin(self):
        self.assertEqual([], self.default_plugin_imports)
        self.assertTrue(self.original['validation']['valid'])
        self.assertFalse((self.initial / 'net-labels').exists())
        self.assertEqual(self.saved_bytes, self.saved.read_bytes())

    def test_explicit_off_does_not_build_any_display_view(self):
        with patch('net_labels.generate', side_effect=AssertionError('disabled plugin')), \
                patch('net_label_core.build_view', side_effect=AssertionError('disabled build')):
            result = self._finish(policy='off')
        self.assertTrue(result['validation']['valid'])
        self.assertFalse((self.directory / 'flow/net-labels').exists())

    def test_auto_hook_saves_full_routes_before_publishing_separate_view(self):
        with ExitStack() as stack:
            self._no_search(stack)
            result = self._finish(policy='auto')
        full = self.directory / 'flow/small-placement.json'
        manifests = list((self.directory / 'flow/net-labels').glob('*/*-netlabels.json'))
        self.assertEqual(1, len(manifests))
        manifest = json.loads(manifests[0].read_text(encoding='utf-8'))
        self.assertEqual(json.loads(full.read_text(encoding='utf-8')), manifest['original'])
        self.assertEqual(json.loads(json.dumps(result['routes'])), manifest['original']['routes'])
        self.assertEqual(result['metrics'], manifest['original']['metrics'])
        self.assertEqual(manifest['fingerprint'][:16], manifests[0].parent.name)
        self.assertTrue((self.directory / 'flow/small-clean.svg').is_file())
        self.assertTrue((self.directory / 'flow/small-layered.svg').is_file())

    def test_plugin_failure_does_not_replace_complete_flow_success(self):
        with patch('net_labels.generate', side_effect=ValueError('no safe label position')):
            result = self._finish(policy='auto')
        self.assertTrue(result['validation']['valid'])
        self.assertTrue((self.directory / 'flow/small-placement.json').is_file())
        self.assertTrue((self.directory / 'flow/small-clean.svg').is_file())
        report = json.loads((self.directory / 'flow/net-labels/plugin-diagnostics.json').read_text(encoding='utf-8'))
        self.assertEqual('failed', report['status'])
        self.assertTrue(report['full_route_result_preserved'])

    def test_saved_generation_never_calls_sa_astar_recovery_or_simplification(self):
        with ExitStack() as stack:
            self._no_search(stack)
            manifest = self._generate()
        self.assertTrue(manifest['view']['validation']['valid'])
        self.assertEqual(self.original, manifest['original'])
        self.assertEqual(self.saved_bytes, self.saved.read_bytes())
        self.assertEqual(self.source_bytes, self.source.read_bytes())
        self.assertEqual(b'', self.include.read_bytes())

    def test_no_candidate_still_produces_a_valid_full_wire_view(self):
        manifest = self._generate()
        self.assertEqual([], manifest['view']['labels'])
        self.assertIn(manifest['view']['status'], ('not_needed', 'no_safe_split', 'time_budget'))
        self.assertEqual(manifest['verified_full_metrics']['wirelength'],
                         manifest['view']['display_metrics']['wirelength'])
        for path in manifest['artifacts'].values():
            self.assertTrue(Path(path).is_file())

    def test_redraw_revalidates_saved_display_without_replanning(self):
        manifest = self._generate()
        with ExitStack() as stack:
            self._no_search(stack)
            stack.enter_context(patch('net_label_core.build_view', side_effect=AssertionError('replan')))
            replay = self._redraw(manifest['artifacts']['json'])
        for key in ('clean_svg', 'color_svg'):
            self.assertEqual(Path(manifest['artifacts'][key]).read_bytes(),
                             Path(replay['artifacts'][key]).read_bytes())
        self.assertEqual(manifest['view'], replay['view'])

    def test_two_svg_styles_share_signal_geometry_and_expression_type(self):
        manifest = self._generate()
        roots = [ET.parse(manifest['artifacts'][key]).getroot()
                 for key in ('clean_svg', 'color_svg')]
        self.assertEqual('net-labels', roots[0].get('data-wire-expression'))
        self.assertEqual('net-labels', roots[1].get('data-wire-expression'))
        wires = [[(e.get('data-net'), e.get('points')) for e in root.iter()
                  if e.get('data-net') is not None] for root in roots]
        self.assertEqual(wires[0], wires[1])

    def test_net_label_renderer_marks_ordinary_and_leader_t_junctions_in_both_styles(self):
        from net_label_validation import label_position
        from sa_render import _text_box, render_svg
        context = net_labels._load_context(self.saved)
        (_, _, par, placement, comps, nets, index, routed, metrics, validation,
         _, hints, _) = context
        anchor, tip = (1100, 1000), (1100, 1048)
        x, y, align = label_position(tip, (0, 1))
        label = {'net': 'output', 'group_id': 'output:g0', 'anchor': anchor,
                 'tip': tip, 'text': 'output', 'x': x, 'y': y, 'anchor_style': align,
                 'bounds': _text_box(x, y, 'output', align, size=14)}
        # A long merged trunk has one ordinary T branch and another supplied
        # by the label leader. Neither junction is a trunk endpoint before
        # canonical splitting; counting endpoints alone would lose both dots.
        merged = [('output', [((900, 1000), (1200, 1000)),
                              ((1000, 960), (1000, 1000)), (anchor, tip)])]
        view = {'labels': [label], 'render_hints': hints,
                'display_metrics': dict(metrics, wirelength=388, crossings=0, bends=0)}
        roots = []
        with patch('net_label_validation.display_routes', return_value=merged):
            for clean in (True, False):
                output = self.directory / ('clean.svg' if clean else 'color.svg')
                render_svg(par, placement, comps, nets, index, routed, metrics, output,
                           clean=clean, validation=validation, net_label_view=view)
                roots.append(ET.parse(output).getroot())
        dots = [{(float(e.get('cx')), float(e.get('cy'))) for e in root.iter()
                 if e.tag.rsplit('}', 1)[-1] == 'circle' and e.get('r') == '4'}
                for root in roots]
        self.assertTrue({(1000., 1000.), (1100., 1000.)} <= dots[0])
        self.assertEqual(dots[0], dots[1])
        wires = [[(e.get('data-net'), e.get('points')) for e in root.iter()
                  if e.get('data-net') is not None] for root in roots]
        self.assertEqual(wires[0], wires[1])

    def test_snapshot_redraw_does_not_require_original_placement_file(self):
        copied = self.directory / 'detached-placement.json'
        copied.write_bytes(self.saved_bytes)
        manifest = self._generate(copied)
        copied.unlink()
        replay = self._redraw(manifest['artifacts']['json'])
        self.assertTrue(replay['view']['validation']['valid'])

    def test_empty_include_is_fingerprinted_and_changes_are_rejected(self):
        manifest = self._generate()
        self.assertIn(str(self.include.resolve()), manifest['dependencies']['source_files'])
        self._mutate_file(self.include, b'* a change to a formerly empty include\n')
        with self.assertRaisesRegex(ValueError, 'Netlist/include changed'):
            self._redraw(manifest['artifacts']['json'])

    def test_root_source_change_is_rejected_without_modifying_original_output(self):
        manifest = self._generate()
        before = {key: Path(path).read_bytes() for key, path in manifest['artifacts'].items()}
        self._mutate_file(self.source, self.source_bytes + b'* changed\n')
        with self.assertRaises(ValueError):
            self._redraw(manifest['artifacts']['json'], out_dir=self.destination)
        self.assertEqual(before, {key: Path(path).read_bytes() for key, path in manifest['artifacts'].items()})

    def test_symbol_changes_are_checked_using_copied_resources(self):
        manifest = self._generate()
        symbol = next(Path(path) for path in manifest['dependencies']['resource_files']
                      if Path(path).is_relative_to(self.resources))
        self._mutate_file(symbol, symbol.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'Symbol resource changed'):
            self._redraw(manifest['artifacts']['json'])

    def test_missing_or_broken_routes_are_rejected_despite_saved_valid_flag(self):
        for routes in (None, {}):
            with self.subTest(routes=routes), self.assertRaises((ValueError, OSError)):
                self._generate(self._variant(routes=routes))

    def test_false_saved_valid_flag_does_not_replace_complete_validation(self):
        validation = deepcopy(self.original['validation'])
        validation['valid'] = False
        manifest = self._generate(self._variant(validation=validation))
        self.assertFalse(manifest['original']['validation']['valid'])
        self.assertTrue(manifest['view']['validation']['valid'])

    def test_tampered_display_fields_are_rejected_even_after_rehash(self):
        manifest = self._generate()
        before = {key: Path(path).read_bytes() for key, path in manifest['artifacts'].items()}
        net = next(iter(manifest['view']['base_routes']))
        changes = {
            'routes': lambda m: m['view']['base_routes'].__setitem__(net, []),
            'groups': lambda m: m['view']['groups'].append({'net': net, 'id': 'fake', 'terminals': [['R1', '1']]}),
            'labels': lambda m: m['view']['labels'].append({'net': net, 'group_id': 'fake', 'text': 'wrong'}),
            'metrics': lambda m: m['view']['display_metrics'].__setitem__('wirelength', -1),
            'scale': lambda m: m['view'].__setitem__('scale', m['view']['scale'] * 2),
            'render_hints': lambda m: m['view'].__setitem__('render_hints', {}),
        }
        for name, update in changes.items():
            with self.subTest(field=name), self.assertRaises(ValueError):
                self._redraw(self._tamper(manifest, update), out_dir=self.destination)
            self.assertEqual(before, {key: Path(path).read_bytes() for key, path in manifest['artifacts'].items()})

    def test_full_metrics_are_independently_recomputed(self):
        manifest = self._generate()
        path = self._tamper(manifest, lambda m: m['verified_full_metrics'].__setitem__('wirelength', -1))
        with self.assertRaises(ValueError):
            self._redraw(path)

    def test_modified_view_without_hash_update_is_rejected(self):
        manifest = self._generate()
        path = self._tamper(manifest, lambda m: m['view'].__setitem__('status', 'forged'), rehash=False)
        with self.assertRaisesRegex(ValueError, 'fingerprints'):
            self._redraw(path)

    def test_cached_generation_reuses_only_after_full_and_display_revalidation(self):
        from net_label_validation import validate_view
        manifest = self._generate()
        with patch('net_label_core.build_view', side_effect=AssertionError('cached replan')), \
                patch('net_label_validation.validate_view', wraps=validate_view) as validation:
            cached = self._generate()
        self.assertTrue(cached['reused'])
        self.assertTrue(validation.called)
        self.assertEqual(manifest['fingerprint'], cached['fingerprint'])

    def test_corrupted_cached_display_is_not_trusted_or_overwritten(self):
        manifest = self._generate()
        path = Path(manifest['artifacts']['json'])
        altered = deepcopy(manifest)
        altered['view']['groups'].append({'net': next(iter(altered['view']['base_routes'])),
                                         'id': 'fake', 'terminals': []})
        altered['view_sha256'] = net_labels._digest(altered['view'])
        path.write_text(json.dumps(altered), encoding='utf-8')
        before = path.read_bytes()
        with patch('net_label_core.build_view', side_effect=AssertionError('cached replan')), \
                self.assertRaises(ValueError):
            self._generate()
        self.assertEqual(before, path.read_bytes())

    def test_generate_checks_scale_against_real_physical_axes(self):
        manifest = self._generate()
        view = deepcopy(manifest['view'])
        view['scale'] *= 2
        with patch('net_label_core.build_view', return_value=view), self.assertRaisesRegex(ValueError, 'scale'):
            self._generate(out_dir=self.directory / 'wrong-scale')

    def test_policy_and_budget_select_separate_fingerprints(self):
        ordinary = self._generate()
        zero = self._generate(seconds=0)
        self.assertNotEqual(ordinary['fingerprint'], zero['fingerprint'])
        self.assertTrue(zero['view']['validation']['valid'])
        self.assertEqual(self.saved_bytes, self.saved.read_bytes())

    def test_long_source_titles_and_output_paths_keep_unique_short_artifacts(self):
        # Both source names share their first forty characters. Their original
        # snapshots are identical, so they also share the strategy fingerprint;
        # truncating without a title digest would overwrite the other result.
        stem = 'long-circuit-title-' + 'shared-branch-' * 8
        paths = [self.directory / (stem + suffix + '-placement.json')
                 for suffix in ('left', 'right')]
        for path in paths:
            path.write_bytes(self.saved_bytes)
        parent = self.directory / 'dataset-comparison-results'
        destination = parent / ('x' * max(1, 120 - len(str(parent)) - 1))
        self.assertGreaterEqual(len(str(destination)), 110)
        manifests = [self._generate(path, out_dir=destination) for path in paths]
        self.assertEqual(manifests[0]['fingerprint'], manifests[1]['fingerprint'])
        self.assertNotEqual(manifests[0]['base'], manifests[1]['base'])
        self.assertNotEqual(manifests[0]['artifacts']['json'], manifests[1]['artifacts']['json'])
        for path, manifest in zip(paths, manifests):
            with self.subTest(source=path.name):
                self.assertLessEqual(len(manifest['base']), 56)
                self.assertEqual(self.original, manifest['original'])
                self.assertEqual(str(path.resolve()), manifest['original_placement'])
                self.assertEqual(self.saved_bytes, path.read_bytes())
                replay = self._redraw(manifest['artifacts']['json'], out_dir=destination / 'redraw')
                for key in ('clean_svg', 'color_svg'):
                    self.assertEqual(Path(manifest['artifacts'][key]).read_bytes(),
                                     Path(replay['artifacts'][key]).read_bytes())
                self.assertEqual(manifest['original'], replay['original'])
        self.assertEqual(self.saved_bytes, self.saved.read_bytes())
        self.assertEqual(self.source_bytes, self.source.read_bytes())

    def test_invalid_budget_and_invalid_input_do_not_create_success_artifacts(self):
        for seconds in (-1, float('nan'), float('inf'), True, '5'):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                self._generate(seconds=seconds)
        broken = self.directory / 'bad.json'
        broken.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            self._generate(broken)
        self.assertFalse(self.destination.exists())

    def test_cli_forwards_explicit_auto_and_preserves_default_off(self):
        with patch('run_flow.run') as run:
            self.assertEqual(0, run_flow.main([str(self.source)]))
            self.assertEqual('off', run.call_args.kwargs['net_labels'])
            self.assertEqual(0, run_flow.main([str(self.source), '--net-labels', 'auto']))
            self.assertEqual('auto', run.call_args.kwargs['net_labels'])


if __name__ == '__main__':
    unittest.main()
