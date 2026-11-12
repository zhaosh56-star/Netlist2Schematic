"""Readability is opt-in, uses shared evidence and preserves saved coordinates."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run_flow
from readability_selection import feedback_compatible, organization_rank, select_readable_result


def result(identifier, score, violations, length=1000, crossings=0, signature='same'):
    metrics = {'wirelength': length, 'crossings': crossings, 'bends': 5}
    report = {'applied': identifier != '1', 'phase': 2, 'plan_signature': signature,
        'plan': {'signature': signature},
        'metrics': {'passive_supported': True, 'violations': violations,
                    'shape_error': 0, 'pin_axis_error': 0, 'owner_locality': 1}}
    return ((score, crossings, length, identifier), None, [], {}, {},
            {'layout_readability': report}, ([], metrics, {'valid': True}), {'score': score}, identifier)


class ReadabilityFlowTests(unittest.TestCase):
    def test_feature_switch_conflicts_are_early(self):
        for option in ('region_layout', 'structure_layout', 'symmetry_layout'):
            with self.subTest(option=option), self.assertRaisesRegex(ValueError, 'cannot be combined'):
                run_flow.run('missing.cir', readability_layout='auto', **{option: 'auto'})
        for kwargs in ({'mode': 'baseline'}, {'layout_style': 'legacy'}, {'passive_layout': 'fixed'}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, 'requires'):
                run_flow.run('missing.cir', readability_layout='auto', **kwargs)

    def test_saved_input_cannot_request_relayout(self):
        for flag in ('--reroute-placement', '--simplify-placement'):
            with patch('sys.stderr'), self.assertRaises(SystemExit) as error:
                run_flow.main([flag, 'saved.json', '--readability-layout', 'auto'])
            self.assertEqual(error.exception.code, 2)

    def test_organized_candidate_can_win_without_rewriting_quality(self):
        old, new = result('1', 10, 4), result('2', 12, 0, length=1100)
        chosen, audit = select_readable_result([old, new])
        self.assertIs(chosen, new)
        self.assertEqual(chosen[7]['score'], 12)
        self.assertEqual(audit['reference_candidate'], '1')

    def test_routing_limits_and_evidence_mismatch_reject(self):
        old = result('1', 10, 4)
        for new in (result('2', 12, 0, length=1151), result('2', 12, 0, crossings=1),
                    result('2', 12, 0, signature='other')):
            self.assertIs(select_readable_result([old, new])[0], old)
        new = result('2', 12, 0, crossings=2)
        self.assertIs(select_readable_result([old, new], tradeoff='balanced')[0], new)
        new = result('2', 12, 0, crossings=3)
        self.assertIs(select_readable_result([old, new], tradeoff='balanced')[0], old)

    def test_no_organizational_gain_retains_original_winner(self):
        old, new = result('1', 10, 0), result('2', 12, 0)
        self.assertIs(select_readable_result([old, new])[0], old)

    def test_feedback_requires_both_applied_and_same_contract(self):
        old, a, b = result('1', 10, 4), result('2', 12, 0), result('3', 13, 0)
        self.assertFalse(feedback_compatible(old[5], b[5]))
        self.assertTrue(feedback_compatible(a[5], b[5]))
        b[5]['layout_readability']['plan']['signature'] = 'other'
        self.assertFalse(feedback_compatible(a[5], b[5]))

    def test_uncovered_relationships_remain_unknown(self):
        self.assertIsNone(organization_rank({'phase': 2, 'metrics': {
            'passive_supported': False, 'violations': None, 'shape_error': None}}))
        self.assertIsNone(organization_rank({'phase': 2, 'metrics': {
            'passive_supported': True, 'measurable': False,
            'violations': None, 'shape_error': None, 'pin_axis_error': None}}))

    def test_reference_candidate_random_stream_is_unchanged(self):
        import structured_placement as s
        source = Path(__file__).resolve().parents[1]/'examples/1.cir'
        old = list(s.create_candidates(source, iterations=30, seed=7))
        with patch('orthogonal_router.route_scene', side_effect=AssertionError('SA called A*')):
            new = list(s.create_candidates(source, iterations=30, seed=7,
                                           readability_layout='auto', readability_phase=2))
        a, b = old[0], new[0]
        self.assertEqual(a[0].cell_of, b[0].cell_of)
        self.assertEqual(a[0].orient, b[0].orient)
        self.assertEqual(a[0].rng.getstate(), b[0].rng.getstate())
        self.assertEqual(a[4]['optimization_metrics'], b[4]['optimization_metrics'])


if __name__ == '__main__':
    unittest.main()
