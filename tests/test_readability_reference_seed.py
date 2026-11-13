"""Verified cores can seed local repairs without erasing stronger layout intent."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from netlist2schematic.plugins.readability.readability_selection import reference_seed, measure_candidate
from netlist2schematic.routing.sa_routing import final_route
from tests.test_readability_refinement import tail_fixture, PAR


class ReferenceSeedTests(unittest.TestCase):
    def fixture(self):
        pl, comps, nets, index = tail_fixture()
        plan = {'version': 'test', 'signature': 'same', 'supported': False,
                'patterns': [], 'junctions': {}, 'series_axes': [], 'sources': [],
                'display_rows': {c['id']: pl.cell_of[i][0] for i, c in enumerate(comps)}}
        old = {'candidate_id': 1, 'candidate_seed': 12, 'energy': 1}
        routed = final_route(PAR, nets, pl, index)
        prior = ((1, 0, 100, '1'), pl, comps, nets, index, old, routed, {'score': 1}, '1')
        trial = deepcopy(pl)
        trial.cell_of[index['bias']] = (4, 6)
        trial.set_state(trial.cell_of, trial.orient)
        metadata = {'candidate_id': 2, 'candidate_seed': 99,
                    'layout_readability': {'policy': 'auto', 'plan': plan, 'initialization': {}}}
        return trial, comps, nets, index, metadata, prior, plan

    def test_reuses_legal_core_in_same_slot_without_routing_or_parent_mutation(self):
        pl, comps, nets, index, metadata, prior, plan = self.fixture()
        before = deepcopy(prior[1].__dict__)
        with patch('netlist2schematic.routing.orthogonal_router.route_scene', side_effect=AssertionError('extra A*')):
            result = reference_seed(PAR, pl, comps, nets, index, metadata, [prior], plan)
        self.assertIsNotNone(result)
        self.assertEqual(result[0].cell_of, prior[1].cell_of)
        self.assertEqual(result[4]['candidate_id'], 2)
        self.assertEqual(result[4]['candidate_seed'], 12)
        self.assertEqual(result[4]['layout_readability']['reference_seed']['generated_candidate_seed'], 99)
        self.assertEqual(prior[1].cell_of, before['cell_of'])
        self.assertNotIn('layout_readability', prior[5])
        report = measure_candidate(PAR, result[0], comps, nets, index, result[4], plan, phase=3)
        self.assertTrue(report['layout_readability']['reference_seed']['duplicate_reference_geometry'])
        self.assertFalse(report['layout_readability']['applied'])

    def test_different_active_display_rows_reject_reference(self):
        pl, comps, nets, index, metadata, prior, plan = self.fixture()
        plan['display_rows']['a'] += 1
        self.assertIsNone(reference_seed(PAR, pl, comps, nets, index, metadata, [prior], plan))

    def test_third_candidate_keeps_independent_seed(self):
        pl, comps, nets, index, metadata, prior, plan = self.fixture()
        metadata['candidate_id'] = 3
        self.assertIsNone(reference_seed(PAR, pl, comps, nets, index, metadata, [prior], plan))

    def test_fixed_world_coordinates_are_preserved(self):
        pl, comps, nets, index, metadata, prior, plan = self.fixture()
        pl.fixed = {index['bias']}; pl.fixed_cells = {index['bias']: pl.cell_of[index['bias']]}
        self.assertIsNone(reference_seed(PAR, pl, comps, nets, index, metadata, [prior], plan))

    def test_bridge_members_keep_their_planned_display_rows(self):
        pl, comps, nets, index, metadata, prior, plan = self.fixture()
        plan['patterns'] = [{'kind': 'series_chain', 'id': 'bridge', 'members': ['bias'],
                             'row_mode': 'conduction_bridge'}]
        plan['display_rows']['bias'] = 3
        self.assertIsNone(reference_seed(PAR, pl, comps, nets, index, metadata, [prior], plan))

    def test_fewer_violations_cannot_hide_a_worse_passive_shape(self):
        pl, comps, nets, index, metadata, prior, plan = self.fixture()
        target = {'passive_supported': True, 'measurable': True, 'violations': 1,
                  'shape_error': 0, 'pin_axis_error': 0}
        source = dict(target, violations=0, shape_error=10)
        with patch('netlist2schematic.plugins.readability.readability_layout.measure_readability', side_effect=[target, source]):
            self.assertIsNone(reference_seed(PAR, pl, comps, nets, index, metadata, [prior], plan))

    def test_existing_bank_outlet_gain_cannot_be_overwritten(self):
        pl, comps, nets, index, metadata, prior, plan = self.fixture()
        a = {'bank_outlet_planned': 1, 'bank_outlet_error': 0, 'source_count': 1}
        b = dict(a, bank_outlet_error=3)
        with patch('netlist2schematic.plugins.readability.readability_refinement.measure_refinements', side_effect=[a, a, b]):
            self.assertIsNone(reference_seed(PAR, pl, comps, nets, index, metadata, [prior], plan, phase=4))


if __name__ == '__main__':
    unittest.main()
