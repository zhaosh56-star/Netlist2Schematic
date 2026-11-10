from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from circuit_adapter import CircuitAdapter
from rsi_core import EvalResult, Version, EvaluationUnavailable
from rag_eval.optimizer.v4.decide import safeguard_verdict

ROOT = Path(__file__).resolve().parents[1]


class CircuitAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.adapter = CircuitAdapter(ROOT, Path(cls.temp.name), suite=ROOT / 'benchmarks/suite.json',
                                     agent_config={}, mode='smoke', iterations=200, case_timeout=120)
        cls.seed = cls.adapter.create_seed()
        cls.measured = cls.adapter.evaluate(cls.seed, ['search_rc'], 1, purpose='diagnose')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_real_symbols_routes_and_independent_verification(self):
        row = self.measured.context['measurements']['search_rc'][0]
        self.assertTrue(row['valid'], row.get('error'))
        self.assertGreater(row['quality']['wirelength'], 0)
        self.assertGreater(row['devices'], 1)
        folder = self.adapter.output / row['artifacts'] / 'verified'
        svg = (folder / 'clean.svg').read_text(encoding='utf-8')
        self.assertIn('data-net=', svg)
        self.assertIn('<path', svg)
        self.assertTrue((folder / 'layered.svg').is_file())

    def test_tampered_routes_are_rejected_by_frozen_verifier(self):
        row = self.measured.context['measurements']['search_rc'][0]
        folder = self.adapter.output / row['artifacts'] / 'verified'
        data = json.loads((folder / 'verified-placement.json').read_text(encoding='utf-8'))
        net = next(name for name, segments in data['routes'].items() if segments)
        data['routes'][net] = [[[0, 0], [10, 10]]]
        # A forged candidate flag and zero clearance do not bypass fixed rules.
        data['validation'] = {'valid': True, 'parameters': {'halo': 0, 'edge_clearance': 0}}
        tampered = self.adapter.output / 'tampered.json'
        tampered.write_text(json.dumps(data), encoding='utf-8')
        destination = self.adapter.output / 'tampered-verification'
        process = subprocess.run([sys.executable, '-B', str(ROOT / 'verify_layout.py'),
                                  '--engine', str(ROOT / 'reference/engine'), '--layout', str(tampered),
                                  '--input', self.adapter.cases['search_rc']['source'], '--out', str(destination)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertNotEqual(process.returncode, 0)
        self.assertFalse(json.loads((destination / 'verification.json').read_text(encoding='utf-8-sig'))['valid'])

    def test_device_body_overlap_is_rejected_even_before_route_checks(self):
        row = self.measured.context['measurements']['search_rc'][0]
        folder = self.adapter.output / row['artifacts'] / 'verified'
        data = json.loads((folder / 'verified-placement.json').read_text(encoding='utf-8'))
        placed = list(data['placement'].values())
        data['layout_grid'] = {'column_x': [float(i) for i in range(1 + max(d['col'] for d in placed))],
                               'row_y': [float(i) for i in range(1 + max(d['row'] for d in placed))]}
        tampered = self.adapter.output / 'overlap.json'
        tampered.write_text(json.dumps(data), encoding='utf-8')
        destination = self.adapter.output / 'overlap-verification'
        process = subprocess.run([sys.executable, '-B', str(ROOT / 'verify_layout.py'),
                                  '--engine', str(ROOT / 'reference/engine'), '--layout', str(tampered),
                                  '--input', self.adapter.cases['search_rc']['source'], '--out', str(destination)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertNotEqual(process.returncode, 0)
        result = json.loads((destination / 'verification.json').read_text(encoding='utf-8'))
        self.assertIn('overlapping device bodies', result['error'])

    def test_any_single_invalid_image_is_a_strict_v4_safeguard(self):
        parent = self.measured
        candidate = deepcopy(parent)
        candidate.context['measurements']['search_rc'][0]['valid'] = False
        observed = self.adapter.observe_safeguards(candidate, parent)
        verdict = safeguard_verdict(observed, {'hard_errors': 0})
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['breached'], ['hard_errors'])

    def test_sealed_tasks_not_available_to_optimizer(self):
        with self.assertRaises(EvaluationUnavailable):
            self.adapter.evaluate(self.seed, ['test_ota'], 1, purpose='diagnose')

    def test_source_change_is_content_addressed_and_frozen(self):
        workspace = self.adapter.workspace(self.seed)
        from coding_agent import apply_edits
        entry = Path(workspace['directory']) / 'engine/candidate_entry.py'
        original = entry.read_text(encoding='utf-8-sig')
        report = apply_edits(workspace['directory'], [{'path': 'engine/candidate_entry.py', 'content': original + '\n# A new candidate source version\n'}])
        from rsi_core import Diff
        candidate = self.adapter.materialize(Diff(report['diff'], metadata={**workspace, 'workspace': workspace['directory'], 'report': report}), self.seed)
        self.assertNotEqual(candidate.content_hash, self.seed.content_hash)
        frozen = self.adapter.engine_for(candidate)
        self.assertIn('new candidate', (frozen / 'candidate_entry.py').read_text(encoding='utf-8-sig'))
        self.assertNotIn('new candidate', (self.adapter.engine_for(self.seed) / 'candidate_entry.py').read_text(encoding='utf-8-sig'))


if __name__ == '__main__':
    unittest.main()
