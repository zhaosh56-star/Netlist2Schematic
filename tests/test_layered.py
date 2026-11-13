"""Behavioral regression tests for constrained placement and attachment rows."""
import contextlib
import io
from pathlib import Path
import random
import tempfile
import unittest

from netlist2schematic.common.sa_common import load_local

layered = load_local('place_layered', 'place-layered.py')


class LayeredPlacementTests(unittest.TestCase):
    def make_placement(self):
        comps = [{'id': 'M1', 'symbol': 'nmos', 'nodes': {}},
                 {'id': 'M2', 'symbol': 'pmos', 'nodes': {}},
                 {'id': 'Q1', 'symbol': 'npn', 'nodes': {}},
                 {'id': 'R1', 'symbol': 'resistor', 'nodes': {}}]
        return layered.LayeredPlacement(comps, random.Random(42), [1, 1, 2, 1],
                                        fixed_cells={0: (1, 1)}, cols=4)

    def test_fixed_identity_rows_and_mirror_survive_moves(self):
        placement = self.make_placement()
        self.assertEqual(placement.cell_of[0], (1, 1))
        for iteration in range(1200):
            original = (list(placement.cell_of), list(placement.orient))
            move = placement.try_move()
            self.assertEqual(placement.cell_of[0], (1, 1))
            self.assertEqual([cell[0] for cell in placement.cell_of], placement.row_of)
            self.assertEqual(len(set(placement.cell_of)), len(placement.comps))
            for index in range(3):
                self.assertIn(placement.orient[index], (0, 1))
            if move is not None and iteration % 2:
                placement.undo(move)
                self.assertEqual((placement.cell_of, placement.orient), original)
            self.assertEqual(placement.comp_at,
                             {cell: index for index, cell in enumerate(placement.cell_of)})

    def test_swap_cannot_displace_fixed_destination(self):
        comps = [{'id': 'M1', 'symbol': 'nmos', 'nodes': {}},
                 {'id': 'M2', 'symbol': 'nmos', 'nodes': {}}]
        placement = layered.LayeredPlacement(comps, random.Random(1), [1, 1],
                                            fixed_cells={0: (1, 1)}, cols=2)

        class FixedDestination:
            def randrange(self, limit):
                return 1

            def random(self):
                return 0.1

        placement.rng = FixedDestination()
        before = list(placement.cell_of)
        self.assertIsNone(placement.try_move())
        self.assertEqual(placement.cell_of, before)

    def test_conflicting_fixed_positions_fail_clearly(self):
        comps = [{'id': 'R1', 'symbol': 'resistor'}, {'id': 'R2', 'symbol': 'resistor'}]
        with self.assertRaisesRegex(ValueError, 'Two fixed'):
            layered.LayeredPlacement(comps, random.Random(1), [1, 1],
                                     fixed_cells={0: (1, 1), 1: (1, 1)}, cols=3)


class CircuitPlacementTests(unittest.TestCase):
    def place_text(self, text, iterations=0, cell=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'circuit.spi'
            path.write_text(text, encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                return layered.create_placement(path, cell=cell, iterations=iterations, seed=42)

    def test_pure_passive_and_diode_have_complete_unique_positions(self):
        placement, comps, _, _, metadata = self.place_text(
            '* Pure passive path\nV1 in 0 1\nR1 in mid 1k\nL1 mid out 1m\n'
            'C1 out 0 1n\nD1 out in DMOD\n.model DMOD D\n.end\n')
        self.assertEqual(len(comps), 5)
        self.assertEqual(len(set(placement.cell_of)), 5)
        self.assertTrue(all(isinstance(row, int) for row, _ in placement.cell_of))
        self.assertEqual(metadata['levels'], {})

    def test_resistor_loaded_common_source_has_reachable_layer(self):
        placement, _, _, index, metadata = self.place_text(
            'VDD vdd 0 1.8\nRD vdd out 10k\n'
            'XM1 out in 0 0 sky130_fd_pr__nfet_01v8 W=10 L=1\nCL out 0 1p\n')
        self.assertGreaterEqual(metadata['levels']['XM1'], 1)
        self.assertIsNotNone(placement.cell_of[index['RD']])
        self.assertEqual(placement.cell_of[index['VDD']][0], 0)

    def test_multiple_cross_layer_passives_use_distinct_fixed_gap_cells(self):
        placement, _, _, index, metadata = self.place_text(
            'VDD vdd 0 1.8\nM1 n1 g1 vdd vdd PMOS\nM2 n1 g2 n2 0 NMOS\n'
            'M3 n2 g3 n3 0 NMOS\nM4 n3 g4 0 0 NMOS\n'
            'CSPAN n1 n3 1p\nRSPAN n3 n1 1k\n', iterations=40)
        for name in ('CSPAN', 'RSPAN'):
            self.assertEqual(metadata['attach'][name]['mode'], 'mid')
            row = placement.cell_of[index[name]][0]
            self.assertLess(placement.cell_of[index['M1']][0], row)
            self.assertLess(row, placement.cell_of[index['M3']][0])
            expected = metadata['fixed_positions'][name]
            self.assertEqual(placement.cell_of[index[name]], (expected['row'], expected['col']))
        self.assertNotEqual(placement.cell_of[index['CSPAN']], placement.cell_of[index['RSPAN']])

    def test_mixed_vcvs_bjt_and_diode_have_rows(self):
        placement, comps, _, _, metadata = self.place_text(
            'VDD vdd 0 5\nQ1 out in 0 QN\n.model QN NPN\n'
            'R1 vdd out 1k\nD1 out 0 DD\n.model DD D\nE1 amp 0 in out 100\n')
        self.assertEqual(len(comps), 5)
        self.assertEqual(len(set(placement.cell_of)), 5)
        self.assertEqual(metadata['attach']['E1']['mode'], 'vcvs')

    def test_seed_reproduces_placement_and_energy(self):
        text = ('VDD vdd 0 1.8\nM1 out in 0 0 NMOS\nR1 vdd out 10k\n'
                'C1 out 0 1p\n')
        first = self.place_text(text, iterations=60)
        second = self.place_text(text, iterations=60)
        self.assertEqual(first[0].cell_of, second[0].cell_of)
        self.assertEqual(first[0].orient, second[0].orient)
        self.assertEqual(first[4]['energy'], second[4]['energy'])


if __name__ == '__main__':
    unittest.main()
