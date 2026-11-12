"""Later layout patches may not silently erase accepted escape-risk gains."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from time import perf_counter
import readability_routing  # Load its geometry aliases before per-test patches.

from aesthetic_flow import _preserve_readability_feedback


class ReadabilityRoutingOrderTests(unittest.TestCase):
    def saved(self):
        return object(), {'layout_readability': {'routing_feedback': {
            'net': 'signal', 'branch': {'port_groups': [[(0, 'G')], [(1, 'D')]]},
            'after_risk': {'key': [0, 0, 100]}}}}

    def test_later_layout_with_worse_escape_is_rolled_back(self):
        saved = self.saved()
        with patch('aesthetic_flow.perf_counter', return_value=1), \
                patch('routing_rules.build_scene', return_value=object()), \
                patch('readability_routing.escape_risk', return_value={'key': [1, 0, 80]}):
            pl, meta = _preserve_readability_feedback(None, object(), [], {}, {},
                {'layout_readability': {}}, saved, None, 2)
        self.assertIs(pl, saved[0])
        self.assertEqual(meta['layout_readability']['feedback_order_check']['action'],
                         'rolled_back_later_refinement')

    def test_improved_escape_after_refinement_is_retained_and_measured(self):
        saved = self.saved(); pl = object(); meta = {'layout_readability': {}}
        with patch('aesthetic_flow.perf_counter', return_value=1), \
                patch('routing_rules.build_scene', return_value=object()), \
                patch('readability_routing.escape_risk', return_value={'key': [0, 0, 80]}):
            result, data = _preserve_readability_feedback(None, pl, [], {}, {}, meta, saved, None, 2)
        self.assertIs(result, pl)
        self.assertEqual(data['layout_readability']['feedback_order_check']['actual_risk']['key'],
                         [0, 0, 80])

    def test_expired_shared_budget_keeps_verified_feedback(self):
        saved = self.saved()
        with patch('aesthetic_flow.perf_counter', return_value=2), \
                patch('routing_rules.build_scene', side_effect=AssertionError('budget reset')):
            result, data = _preserve_readability_feedback(None, object(), [], {}, {},
                {'layout_readability': {}}, saved, None, 1)
        self.assertIs(result, saved[0])
        self.assertTrue(data['layout_readability']['feedback_order_check']['feedback_risk_preserved'])

    def test_real_saved_id_groups_remain_measurable_after_unchanged_refinement(self):
        from tests.test_readability_routing import fixture, call, PAR
        item = fixture()
        proposal = call(item)
        self.assertIsNotNone(proposal)
        pl, comps, nets, index, metadata = proposal
        saved = deepcopy((pl, metadata))
        current, result = _preserve_readability_feedback(PAR, pl, comps, nets, index,
            metadata, saved, None, perf_counter()+1)
        self.assertIs(current, pl)
        self.assertEqual(result['layout_readability']['feedback_order_check']['action'],
                         'kept_later_refinement')


if __name__ == '__main__':
    unittest.main()
