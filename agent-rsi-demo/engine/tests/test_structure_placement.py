"""Stage-one grouping changes structured seeds 2/3, preserving row semantics."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from layout_relations import build_layout_relations, prepare_structure_placement
import structured_placement as structured
from tests.test_layout_relations import strict_pair, transistor
from tests.test_passive_placement import base_placement, mos, source


class StructurePlacementTests(unittest.TestCase):
    def test_repacking_removes_unrelated_interleaver_without_changing_rows_axes_orients(self):
        devices = strict_pair()
        devices['other'] = mos('od', 'og', '0')
        pl, comps, nets, index = base_placement(devices, {'a': (1, 1), 'b': (1, 4),
                                            'i': (2, 1), 'other': (1, 2)})
        original = deepcopy((pl.row_of, pl.orient, pl.column_x, pl.row_y))
        relations = build_layout_relations(devices)
        prepare_structure_placement(pl, comps, index, relations)
        self.assertTrue(pl.blocks_contiguous())
        cols = sorted(pl.cell_of[index[name]][1] for name in ('a', 'b'))
        self.assertFalse(cols[0] < pl.cell_of[index['other']][1] < cols[1])
        self.assertEqual((pl.row_of, pl.orient, pl.column_x, pl.row_y), original)
        self.assertEqual(sorted(i for group in pl.blocks for i in group), list(range(len(comps))))

    def test_bjt_pair_moves_stay_local_and_rows_mirrors_remain_legal(self):
        devices = {'a': transistor('ca', 'ba', 'tail'), 'b': transistor('cb', 'bb', 'tail'),
                   'i': source('ISRC', 'tail'), 'x': mos('dx', 'gx', '0')}
        pl, comps, nets, index = base_placement(devices, {'a': (1, 1), 'b': (1, 4), 'i': (2, 1), 'x': (1, 2)})
        prepare_structure_placement(pl, comps, index, build_layout_relations(devices))
        rows = list(pl.row_of)
        for step in range(600):
            move = pl.propose()
            self.assertTrue(pl.blocks_contiguous())
            self.assertEqual([r for r, _ in pl.cell_of], rows)
            self.assertEqual(len(set(pl.cell_of)), len(comps))
            for i, comp in enumerate(comps):
                if comp['symbol'] in ('nmos', 'pmos', 'npn', 'pnp'):
                    self.assertIn(pl.orient[i], (0, 1))
            if move and step % 2:
                pl.set_state(*move)

    def test_fixed_devices_never_move_in_new_actions(self):
        devices = strict_pair()
        pl, comps, nets, index = base_placement(devices, {'a': (1, 1), 'b': (1, 3), 'i': (2, 1)})
        pl.fixed = {index['a']}
        pl.fixed_cells = {index['a']: pl.cell_of[index['a']]}
        prepare_structure_placement(pl, comps, index, build_layout_relations(devices))
        position, orientation = pl.cell_of[index['a']], pl.orient[index['a']]
        for _ in range(200):
            pl.propose()
            self.assertEqual(pl.cell_of[index['a']], position)
            self.assertEqual(pl.orient[index['a']], orientation)

    def test_off_never_calls_new_relation_builder(self):
        path = ROOT / 'tests/fixtures/aesthetic-baseline/3.cir'
        with mock.patch('layout_relations.build_layout_relations', side_effect=AssertionError('off invoked builder')):
            list(structured.create_candidates(path, iterations=30, structure_layout='off'))

    def test_reference_one_matches_off_and_only_two_three_enable_stage_one(self):
        path = ROOT / 'tests/fixtures/aesthetic-baseline/4.cir'
        old = list(structured.create_candidates(path, iterations=120, seed=98, structure_layout='off'))
        new = list(structured.create_candidates(path, iterations=120, seed=98, structure_layout='auto', structure_phase=1))
        self.assertEqual((old[0][0].cell_of, old[0][0].orient, old[0][4]),
                         (new[0][0].cell_of, new[0][0].orient, new[0][4]))
        for before, after in zip(old[1:], new[1:]):
            self.assertEqual([r for r, _ in before[0].cell_of], [r for r, _ in after[0].cell_of])
            self.assertEqual(after[4]['structure_layout']['phase'], 1)
            self.assertEqual(after[4]['structure_layout']['metrics']['interleaving'], 0)
            self.assertTrue(after[0].blocks_contiguous())
            self.assertIn('structure_regularization', after[4]['optimization_metrics'])

    def test_stage_one_has_no_astar_in_annealing(self):
        path = ROOT / 'tests/fixtures/aesthetic-baseline/3.cir'
        with mock.patch('orthogonal_router.route_scene', side_effect=AssertionError('A* in SA')):
            list(structured.create_candidates(path, iterations=150, structure_layout='auto'))

    def test_invalid_policy_is_rejected(self):
        path = ROOT / 'tests/fixtures/aesthetic-baseline/3.cir'
        with self.assertRaises(ValueError):
            list(structured.create_candidates(path, structure_layout='unknown'))


if __name__ == '__main__':
    unittest.main()
