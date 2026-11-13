"""Public passive policy dispatch, compatibility and saved-position boundaries."""
from __future__ import annotations

import contextlib
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from netlist2schematic.flow import aesthetic_flow
from netlist2schematic.flow import run_flow
from netlist2schematic.common.sa_common import load_local
from netlist2schematic.layout import structured_placement as structured


class PassiveFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'passive.cir'
        self.raw = b'* keep raw input\r\nR1 input output 1k\r\nC1 output 0 1n\r\n.end\r\n'
        self.source.write_bytes(self.raw)

    def generate(self, policy='flexible', **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return run_flow.run(self.source, iterations=0, seed=17,
                passive_layout=policy, candidate_id='1', quality_seconds=10,
                out_dir=self.root / policy, **kwargs)

    def test_cli_defaults_and_fixed_policy_are_forwarded(self):
        for value in ('flexible', 'fixed'):
            argv = [str(self.source)] + ([] if value == 'flexible' else ['--passive-layout', value])
            with patch('netlist2schematic.flow.run_flow.run') as run:
                self.assertEqual(run_flow.main(argv), 0)
            self.assertEqual(run.call_args.kwargs['passive_layout'], value)

    def test_policy_validation_happens_before_layout(self):
        with patch('netlist2schematic.flow.run_flow._normal_result', side_effect=AssertionError('invalid policy reached layout')):
            with self.assertRaisesRegex(ValueError, 'passive_layout'):
                run_flow.run(self.source, passive_layout='invalid')
        with self.assertRaisesRegex(ValueError, 'passive_layout'):
            aesthetic_flow.select_layout('unused.cir', passive_layout='invalid')
        with self.assertRaisesRegex(ValueError, 'passive_layout'):
            next(structured.create_candidates('unused.cir', passive_layout='invalid'))

    def test_normal_dispatch_forwards_policy(self):
        with patch('netlist2schematic.flow.aesthetic_flow.select_layout', return_value='sentinel') as select:
            result = run_flow._normal_result(self.source, None, 'layered', 'structured',
                None, 0, 17, 10, None, None, passive_layout='fixed')
        self.assertEqual(result, 'sentinel')
        self.assertEqual(select.call_args.kwargs['passive_layout'], 'fixed')

    def test_public_flow_records_actual_and_requested_policy(self):
        for policy in ('fixed', 'flexible'):
            data = self.generate(policy)
            self.assertEqual(data['requested_passive_layout'], policy)
            self.assertEqual(data['passive_layout_policy'], policy)
            self.assertEqual(data['passive_layout']['policy'], policy)
            self.assertTrue(data['validation']['valid'])
            self.assertEqual(hashlib.sha256(self.raw).hexdigest(), data['source_sha256'])
            self.assertEqual(self.raw, self.source.read_bytes())
            if policy == 'flexible':
                self.assertIn('passive_regularization', data['optimization_metrics'])
                self.assertEqual(data['passive_layout']['metrics_stage'], 'after_sa_and_geometry_floor')

    def test_saved_reroute_simplify_and_redraw_do_not_relayout(self):
        data = self.generate()
        saved = self.root / 'flexible/passive-placement.json'
        renderer = load_local('passive_flow_renderer', 'render-clean.py')
        with patch('netlist2schematic.layout.structured_placement.create_candidates', side_effect=AssertionError('saved input relayout')), \
                contextlib.redirect_stdout(io.StringIO()):
            rerouted = run_flow.reroute_placement(saved, out_dir=self.root / 'rerouted')
            simplified = run_flow.simplify_placement(saved, out_dir=self.root / 'simplified')
            renderer.render(saved, self.root / 'redraw.svg')
        for result in (rerouted, simplified):
            self.assertEqual(result['placement'], data['placement'])
            self.assertEqual(result['layout_grid'], data['layout_grid'])
            self.assertTrue(result['validation']['valid'])
        self.assertEqual((self.root / 'redraw.svg').read_bytes(),
                         (self.root / 'flexible/passive-clean.svg').read_bytes())

    def test_reference_candidate_is_identical_in_both_policies(self):
        states = []
        for policy in ('fixed', 'flexible'):
            item = next(structured.create_candidates(self.source, iterations=40, seed=27,
                                                     passive_layout=policy, include_legacy=True))
            pl, _, _, _, metadata = item
            self.assertEqual(metadata['candidate_id'], 0)
            self.assertEqual(metadata['passive_layout_policy'], 'fixed')
            states.append((pl.cell_of, pl.orient, pl.rng.getstate(), metadata['energy'],
                           metadata['optimization_metrics']))
        self.assertEqual(states[0], states[1])

    def test_no_passives_keep_old_class_state_and_rng(self):
        self.source.write_text('M1 out input 0 0 NMOS\n.end\n', encoding='utf-8')
        outputs = []
        for policy in ('fixed', 'flexible'):
            items = list(structured.create_candidates(self.source, iterations=30, seed=71,
                                                      passive_layout=policy, include_legacy=True))
            self.assertEqual(len(items), 4)
            states = []
            for pl, _, _, _, metadata in items:
                self.assertEqual(metadata['passive_layout_policy'], 'fixed')
                self.assertNotEqual(type(pl).__name__, 'FlexiblePassivePlacement')
                states.append((pl.cell_of, pl.orient, pl.rng.getstate(),
                               metadata['layout_grid'], metadata['energy'],
                               metadata['optimization_metrics']))
            outputs.append(states)
        self.assertEqual(outputs[0], outputs[1])

    def test_legacy_and_baseline_keep_fixed_behavior(self):
        self.source.write_text('R1 input output 1k\n.end\n', encoding='utf-8')
        for mode, style in (('layered', 'legacy'), ('baseline', 'structured')):
            results = []
            for policy in ('fixed', 'flexible'):
                with contextlib.redirect_stdout(io.StringIO()):
                    data = run_flow.run(self.source, mode=mode, layout_style=style,
                        iterations=0, seed=17, passive_layout=policy,
                        out_dir=self.root / f'{mode}-{policy}', fallback='off')
                self.assertEqual(data['passive_layout_policy'], 'fixed')
                results.append((data['placement'], data['routes'], data['metrics']))
            self.assertEqual(results[0], results[1])


if __name__ == '__main__':
    unittest.main()
