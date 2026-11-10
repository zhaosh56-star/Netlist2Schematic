"""Verify the shipped coordinator really runs calibration and adoption."""
from pathlib import Path
import tempfile
import unittest

from rsi_core import Card, Diff, EvalResult, SystemAdapter, Version, run_core


class SyntheticAdapter(SystemAdapter):
    def materialize(self, diff, parent):
        return Version(parent.content_hash + '-candidate')

    def workspace(self, version, card=None):
        return version

    def analyse(self, search_set_trajectories, n_cards, **kwargs):
        return [Card('synthetic failure', 'synthetic source improvement')]

    def implement(self, card, workspace):
        return Diff('--- a/flow.py\n+++ b/flow.py\n-old\n+improved\n')

    def features(self, trajectories, predicate):
        return {}

    def evaluate(self, version, task_ids, k, *, purpose='screen'):
        score = 0.2 if version.content_hash == 'seed' else 0.8
        return EvalResult(
            per_task_scores={t: score for t in task_ids},
            trajectories={t: [{'synthetic': True} for _ in range(k)] for t in task_ids},
            costs={t: 0.0 for t in task_ids}, error_classes={t: None for t in task_ids},
            attempt_scores=[{t: score for t in task_ids} for _ in range(k)],
        )

    def evaluate_subset(self, version, reference, subset, task_ids, k):
        return (self.evaluate(version, task_ids, k),
                self.evaluate(reference, task_ids, k) if reference else None)

    def observe_safeguards(self, candidate, reference):
        return {k: {'kind': 'ratio', 'direction': 'lower_is_better', 'subject': 1.0, 'reference': 1.0}
                for k in ('hard_errors', 'crossings_ratio', 'wirelength_ratio')}


class MethodCoreTests(unittest.TestCase):
    def test_actual_v4_calibration_remeasurement_adoption_and_round_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            result = run_core(SyntheticAdapter(), Version('seed'), search=['search-1'],
                              validation=['validation-1', 'validation-2'],
                              state_dir=temp, max_rounds=2)
            self.assertEqual(result['status'], 'demo_round_limit')
            self.assertEqual(result['final_version'], 'seed-candidate')
            self.assertEqual(len(result['lineage']), 2)
            self.assertTrue((Path(temp) / 'state.json').is_file())
            self.assertTrue((Path(temp) / 'ledger.json').is_file())
            self.assertEqual(result['ledger']['circuit_demo']['max_rounds'], 2)
            self.assertTrue(result['lineage'][1]['remeasured'])


if __name__ == '__main__':
    unittest.main()
