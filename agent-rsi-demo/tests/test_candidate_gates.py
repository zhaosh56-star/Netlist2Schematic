from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from circuit_adapter import CircuitAdapter, tree_hash
from rsi_core import EvaluationUnavailable


ROOT = Path(__file__).resolve().parents[1]


class CandidateGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.adapter = CircuitAdapter(ROOT, self.root, suite=ROOT / 'benchmarks/suite.json',
                                      agent_config={}, mode='smoke')
        self.seed = self.adapter.create_seed()

    def tearDown(self):
        self.adapter.cancel_pending()
        self.temp.cleanup()

    def test_verifier_timeout_is_unmeasurable_and_not_cached_as_zero(self):
        def execute(command, **kwargs):
            if any(str(arg).endswith('candidate_entry.py') for arg in command):
                output = Path(command[command.index('--out') + 1])
                (output / 'one-placement.json').write_text('{}')
                (output / 'one-clean.svg').write_text('<svg/>')
                (output / 'one-layered.svg').write_text('<svg/>')
                return subprocess.CompletedProcess(command, 0)
            raise subprocess.TimeoutExpired(command, 75)
        with patch.object(self.adapter, '_run_process', side_effect=execute):
            with self.assertRaises(EvaluationUnavailable):
                self.adapter._attempt(self.seed, 'search_rc', 0)
        folder = self.root / 'measurements' / self.seed.content_hash[:20] / 'search_rc' / '0'
        self.assertFalse((folder / 'measurement.json').exists())
        self.assertTrue((folder / 'infrastructure-error.json').exists())

    def test_generation_traceback_is_available_in_search_trajectory(self):
        def execute(command, **kwargs):
            kwargs['stdout'].write('Traceback: BrokenBiasMap at strategy.py:9\n')
            return subprocess.CompletedProcess(command, 1)
        with patch.object(self.adapter, '_run_process', side_effect=execute):
            row = self.adapter._attempt(self.seed, 'search_rc', 0)
        with patch.object(self.adapter, 'regression_check', return_value={'passed': True}), \
                patch.object(self.adapter, '_attempt', return_value=row):
            evaluation = self.adapter.evaluate(self.seed, ['search_rc'], 1, purpose='diagnose')
        self.assertIn('BrokenBiasMap', evaluation.trajectories['search_rc'][0]['diagnostic']['generation.log'])
        self.assertEqual(evaluation.per_task_scores['search_rc'], 0)

    def test_candidate_parser_regression_is_rejected_by_frozen_tests(self):
        workspace = self.adapter.workspace(self.seed)
        engine = Path(workspace['directory']) / 'engine'
        (engine / 'netlist_core.py').write_text("raise RuntimeError('broken-parser')\n", encoding='utf-8')
        version = self.adapter._freeze(engine, self.seed.content_hash, {'kind': 'test'})
        result = self.adapter.regression_check(version)
        self.assertFalse(result['passed'])
        self.assertIn('broken-parser', result['diagnostic'])
        self.assertEqual(tree_hash(self.adapter.engine_for(self.seed)), self.seed.content_hash)

    def test_verifier_malformed_output_is_not_scored_candidate_failure(self):
        def execute(command, **kwargs):
            if any(str(arg).endswith('candidate_entry.py') for arg in command):
                output = Path(command[command.index('--out') + 1])
                (output / 'one-placement.json').write_text('{}')
                (output / 'one-clean.svg').write_text('<svg/>')
                (output / 'one-layered.svg').write_text('<svg/>')
            else:
                output = Path(command[command.index('--out') + 1])
                output.mkdir(parents=True, exist_ok=True)
                (output / 'verification.json').write_text('{invalid')
            return subprocess.CompletedProcess(command, 0)
        with patch.object(self.adapter, '_run_process', side_effect=execute), self.assertRaises(EvaluationUnavailable):
            self.adapter._attempt(self.seed, 'search_rc', 0)
        self.assertFalse(list(self.root.glob('measurements/*/*/*/measurement.json')))


if __name__ == '__main__':
    unittest.main()
