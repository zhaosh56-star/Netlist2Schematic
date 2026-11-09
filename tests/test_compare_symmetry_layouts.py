"""Comparison-tool provenance, replay and reference-display boundaries."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import compare_symmetry_layouts as comparison


class SymmetryComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def source(self, text):
        source = self.root / 'sample.cir'
        source.write_text(text, encoding='utf-8')
        return source

    def test_discovery_includes_every_dataset_and_seven_provenanced_examples(self):
        cases = comparison.discover_cases()
        examples = [case for case in cases if case['family'] == 'examples']
        self.assertEqual({case['case'] for case in examples}, set(map(str, range(1, 8))))
        self.assertEqual(next(case for case in examples if case['case'] == '5')['cell'], 'dut')
        for label in ('6', '7'):
            case = next(case for case in examples if case['case'] == label)
            self.assertTrue(case['provenance'])
            self.assertIn('/input/', case['netlist'])
        dataset = [case for case in cases if case['family'].startswith('test_data/')]
        actual = set((ROOT / 'test_data').glob('*/*.cir'))
        self.assertEqual({Path(case['netlist']) for case in dataset}, actual)
        self.assertEqual(len({case['case'] for case in cases}), len(cases))

    def test_single_cell_is_selected_without_rewriting_source(self):
        source = self.source('.subckt DUT a b\nR1 a b 1k\n.ends DUT\n')
        original = source.read_bytes()
        self.assertEqual(comparison._select_cell(source), 'dut')
        self.assertEqual(source.read_bytes(), original)

    def test_flat_circuit_keeps_no_cell(self):
        source = self.source('R1 a b 1k\n.end\n')
        self.assertIsNone(comparison._select_cell(source))

    def test_empty_malformed_and_ambiguous_inputs_are_not_guessed(self):
        for text, expected in (('* empty\n.end\n', '[Nn]o supported devices'),
                ('R1 a b 1k\n.ends Main\n', 'no matching'),
                ('.subckt a x y\nR1 x y 1k\n.ends a\n'
                 '.subckt b x y\nR2 x y 2k\n.ends b\n', 'Multiple subcircuits')):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, expected):
                    comparison._select_cell(self.source(text))

    def test_signature_freezes_recursive_models_reference_and_actual_parameters(self):
        source = self.source('.include "models.inc"\nR1 a b 1k\n.end\n')
        model = self.root / 'models.inc'
        model.write_text('* empty model declarations still have a hash\n', encoding='utf-8')
        truth = self.root / 'reference.svg'
        truth.write_text('<svg/>', encoding='utf-8')
        params = {**comparison.DEFAULT_PARAMETERS, 'quality_seconds': 7, 'jobs': 4}
        signature = comparison._signature({'case': 'test', 'netlist': str(source),
            'ground_truth': str(truth)}, 'auto', params, {'source.py': 'hash'})
        self.assertEqual(signature['parameters'], params)
        self.assertEqual(set(signature['source_files']), {str(source), str(model), str(truth)})
        for path, digest in signature['source_files'].items():
            self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), digest)

    def test_command_uses_symmetry_switch_and_exact_nondefault_budgets(self):
        p = {**comparison.DEFAULT_PARAMETERS, 'quality_seconds': 13,
             'rescue_seconds': 0, 'simplify_seconds': 1, 'jobs': 6}
        command = comparison._command({'parameters': p, 'policy': 'off',
            'source_netlist': 'input.cir', 'cell': 'dut'}, self.root)
        for flag, value in (('--symmetry-layout', 'off'), ('--quality-seconds', '13'),
                ('--rescue-seconds', '0'), ('--simplify-seconds', '1'), ('--cell', 'dut')):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertNotIn('--layout-symmetry', command)

    def test_input_checks_use_this_tool_snapshot_not_old_passive_tool(self):
        source = self.source('R1 a b 1k\n')
        signature = {'source_files': {str(source): comparison._sha(source)},
                     'code_files': {'tool': 'hash'}}
        with patch.object(comparison, '_snapshot', return_value={'tool': 'hash'}):
            comparison._check_inputs(signature)
        with patch.object(comparison, '_snapshot', return_value={'other': 'hash'}):
            with self.assertRaisesRegex(ValueError, 'code or symbol'):
                comparison._check_inputs(signature)
        source.write_text('R1 a b 2k\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Original input'):
            comparison._check_inputs(signature)

    def test_batch_freeze_includes_empty_invalid_sources_and_detects_queue_change(self):
        source = self.source('* empty input\n')
        truth = self.root / 'truth.svg'
        truth.write_text('<svg/>', encoding='utf-8')
        case = {'case': 'test', 'netlist': str(source), 'ground_truth': str(truth)}
        frozen = comparison._freeze_inputs([case])
        self.assertEqual(set(frozen), {str(source), str(truth)})
        source.write_text('R1 a b 1k\n.end\n', encoding='utf-8')
        self.assertEqual(comparison._input_changes(frozen), [str(source)])
        with self.assertRaisesRegex(ValueError, 'queued'):
            comparison.execute(case, 'auto', self.root, comparison.DEFAULT_PARAMETERS,
                               {}, expected_inputs=frozen)

    def test_matching_signature_reuses_only_after_independent_verification(self):
        signature = {'sentinel': 1}
        fingerprint = comparison._digest(signature)
        directory = self.root / 'runs/test/auto/old'
        directory.mkdir(parents=True)
        record = {'fingerprint': fingerprint, 'status': 'valid',
                  'verified': {'placement_json': 'saved.json'}, 'reused': False}
        comparison.write_json(directory / 'run-record.json', record)
        with patch.object(comparison, '_signature', return_value=signature), \
                patch.object(comparison, '_check_inputs') as check, \
                patch.object(comparison, '_verify', return_value={'valid': True}) as verify, \
                patch.object(comparison.subprocess, 'Popen', side_effect=AssertionError('rerun')):
            result = comparison.execute({'case': 'test'}, 'auto', self.root, {}, {})
        self.assertTrue(result['reused'])
        check.assert_called_once_with(signature)
        verify.assert_called_once_with(Path('saved.json'), signature)
        self.assertFalse(comparison.read_json(directory / 'run-record.json')['reused'])

    def test_overlay_is_separate_and_preserves_original_wire_geometry(self):
        source = self.root / 'clean.svg'
        raw = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><polyline data-net="a" points="0,0 100,0"/></svg>'
        source.write_text(raw, encoding='utf-8')
        class Placement:
            def pos_of(self, i):
                return (20 if i == 0 else 80, 50)
        output = self.root / 'annotated.svg'
        comparison._annotate(source, output, Placement(), {'a': 0, 'b': 1},
            [{'pairs': [['a', 'b']]}], {'groups': [{'axis_x': 50}]})
        self.assertEqual(source.read_text(encoding='utf-8'), raw)
        nodes = list(ET.parse(output).getroot().iter())
        self.assertEqual([node.get('points') for node in nodes if node.get('data-net')], ['0,0 100,0'])
        self.assertTrue(any(node.get('data-diagnostic') == 'symmetry-axes' for node in nodes))
        self.assertEqual(sum(node.tag.endswith('circle') for node in nodes), 2)

    def test_report_never_labels_shortened_runs_as_default_or_truth_verified(self):
        case = {'case': 'test', 'netlist': 'test.cir'}
        parameters = {**comparison.DEFAULT_PARAMETERS, 'quality_seconds': 1}
        report = comparison.publish(self.root, [case], {}, {}, parameters)
        self.assertFalse(report['uses_default_budgets'])
        self.assertFalse(report['ground_truth_electrically_verified'])
        self.assertEqual(report['parameters']['quality_seconds'], 1)
        self.assertTrue((self.root / 'index.html').is_file())

    def test_parallelism_does_not_change_budget_labels(self):
        parameters = {**comparison.DEFAULT_PARAMETERS, 'jobs': 4}
        report = comparison.publish(self.root, [], {}, {}, parameters)
        self.assertTrue(report['uses_default_budgets'])
        self.assertFalse(report['uses_default_parameters'])
        self.assertEqual(report['parameters']['jobs'], 4)


if __name__ == '__main__':
    unittest.main()
