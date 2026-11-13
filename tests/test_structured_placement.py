"""Constraints and reproducibility of topology-driven placement proposals."""
import contextlib
import io
import math
from pathlib import Path
import time
import unittest
from unittest import mock

from netlist2schematic.netlist.netlist_core import parse_netlist, to_document
from netlist2schematic.layout import structured_placement as structured

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / 'tests' / 'fixtures' / 'aesthetic-baseline'


def seed_placement(number, seed=123, variant=0):
    path = FROZEN / f'{number}.cir'
    devices = parse_netlist(path)
    doc = to_document(devices)
    comps, nets = doc['components'], doc['nets']
    index = {comp['id']: i for i, comp in enumerate(comps)}
    levels = structured.dl.compute_levels(devices)
    conduction = {net for dev in devices.values() if structured._active(dev)
                  for net in (dev.get('d'), dev.get('s'), dev.get('c'), dev.get('e'))
                  if net is not None}
    att = structured.legacy.attach_info(devices, levels['net_levels'], conduction)
    records = structured._structures(devices)
    pl, alignment, pairs = structured._initial_placement(
        devices, comps, nets, index, levels, att, records, seed, variant,
        passive_layout='fixed')
    return pl, comps, nets, index, alignment, pairs, records, levels


class StructuredPlacementTests(unittest.TestCase):
    def test_block_moves_keep_rows_ownership_and_mirror_constraints(self):
        pl, comps, _, _, _, _, _, _ = seed_placement(4)
        expected_rows = list(pl.row_of)
        collective = False
        for step in range(1800):
            before = (list(pl.cell_of), list(pl.orient))
            previous = pl.propose()
            self.assertEqual([cell[0] for cell in pl.cell_of], expected_rows)
            self.assertEqual(len(set(pl.cell_of)), len(comps))
            self.assertTrue(pl.blocks_contiguous())
            self.assertEqual(pl.comp_at, {cell: i for i, cell in enumerate(pl.cell_of)})
            for i, comp in enumerate(comps):
                if comp['symbol'] in structured.legacy.MIRROR_ORIENTS:
                    self.assertIn(pl.orient[i], (0, 1))
            if previous:
                collective |= sum(a != b for a, b in zip(before[0], pl.cell_of)) > 2
                if step % 2:
                    pl.set_state(*previous)
                    self.assertEqual((pl.cell_of, pl.orient), before)
        self.assertTrue(collective, 'At least one legal move must move a whole structure')

    def test_temperature_has_requested_sampled_acceptance(self):
        deltas = [-10, 0, 1, 2, 10, 40]
        temperature = structured._calibrated_temperature(deltas)
        acceptance = sum(math.exp(-d / temperature) for d in deltas if d > 0) / 4
        self.assertAlmostEqual(acceptance, .7, places=8)

    def test_output_pair_aligns_and_active_layer_order_is_preserved(self):
        pl, comps, nets, index, alignment, pairs, records, levels = seed_placement(4)
        _, metrics, _ = structured._anneal(pl, nets, index, alignment, pairs, 6666, None)
        record = next(s for s in records if s['kind'] == 'output_pair')
        upper, lower = (index[record['roles'][key]] for key in ('upper', 'lower'))
        self.assertEqual(pl.cell_of[upper][1], pl.cell_of[lower][1])
        for a, la in levels['device_levels'].items():
            for b, lb in levels['device_levels'].items():
                if la < lb:
                    self.assertLess(pl.cell_of[index[a]][0], pl.cell_of[index[b]][0])
        self.assertEqual(metrics['role_order_inversions'], 0)

    def test_opamp_feedbacks_follow_real_input_side_and_parallel_shunts(self):
        pl, _, _, index, _, _, records, _ = seed_placement(1)
        amplifier = next(s for s in records if s['kind'] == 'opamp_core')['roles']['amplifier']
        amp_row = pl.cell_of[index[amplifier]][0]
        for feedback in (s for s in records if s['kind'] == 'feedback_network'):
            roles = feedback['roles']
            series_rows = [pl.cell_of[index[name]][0] for name in roles['series']]
            if roles['input'] == 'plus':
                self.assertTrue(all(row > amp_row for row in series_rows))
            else:
                self.assertTrue(all(row < amp_row for row in series_rows))
        parallel = next(s for s in records if s['kind'] == 'parallel_passives')
        self.assertEqual(len({pl.cell_of[index[name]][0] for name in parallel['devices']}), 1)
        self.assertLess(max(pl.row_y) - min(pl.row_y), 500)

    def test_annealing_is_reproducible_and_does_not_call_final_astar(self):
        results = []
        with mock.patch('netlist2schematic.routing.orthogonal_router.route_scene', side_effect=AssertionError('A* in SA')):
            for _ in range(2):
                pl, _, nets, index, alignment, pairs, _, _ = seed_placement(3, seed=77)
                result = structured._anneal(pl, nets, index, alignment, pairs, 800, None)
                results.append((list(pl.cell_of), list(pl.orient), result))
        self.assertEqual(results[0], results[1])

    def test_deadline_keeps_a_valid_best_state(self):
        pl, _, nets, index, alignment, pairs, _, _ = seed_placement(2)
        _, _, annealing = structured._anneal(pl, nets, index, alignment, pairs, 20000,
                                              time.perf_counter() - 1)
        self.assertEqual(annealing['stop_reason'], 'quality_budget')
        self.assertEqual(len(set(pl.cell_of)), len(pl.comps))
        self.assertTrue(pl.blocks_contiguous())

    def test_legacy_reference_deadline_stops_sa_and_keeps_best_rows(self):
        pl, _, nets, index, _, _, _, _ = seed_placement(3)
        before = list(pl.cell_of), list(pl.orient)
        structured.legacy.sa_layered(pl, nets, index, [], [], iterations=20000,
                                      deadline=time.perf_counter() - 1)
        self.assertEqual(pl.sa_stop_reason, 'quality_budget')
        self.assertEqual(pl.sa_iterations_completed, 0)
        self.assertEqual((pl.cell_of, pl.orient), before)

    def test_expired_generator_deadline_skips_legacy_initialization(self):
        with mock.patch.object(structured.legacy, 'create_placement') as create:
            with self.assertRaisesRegex(ValueError, 'budget exhausted'):
                next(structured.create_candidates(FROZEN / '2.cir',
                                                   deadline=time.perf_counter() - 1))
            create.assert_not_called()

    def test_generator_owns_candidates_and_preserves_input_bytes(self):
        path = FROZEN / '1.cir'
        original = path.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            generator = structured.create_candidates(path, iterations=30, seed=17)
            first = next(generator)
            first_snapshot = (list(first[0].cell_of), list(first[0].orient))
            rest = list(generator)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual((first[0].cell_of, first[0].orient), first_snapshot)
        self.assertEqual([first[4]['candidate_id']] + [row[4]['candidate_id'] for row in rest],
                         [1, 2, 3])
        self.assertEqual(len({id(row[0]) for row in [first, *rest]}), 3)
        for row in rest:
            pl = row[0]
            for i, (r, c) in enumerate(pl.cell_of):
                self.assertEqual(pl.pos_of(i), (pl.column_x[c], pl.row_y[r]))

    def test_measured_detour_feedback_opens_only_one_physical_row_channel(self):
        pl, comps, nets, index, _, _, records, _ = seed_placement(4)
        dp = next(row for row in records if row['kind'] == 'differential_pair')
        net = dp['nets']['source']
        original_cells = list(pl.cell_of)
        original_orientations = list(pl.orient)
        original_y, original_x = list(pl.row_y), list(pl.column_x)
        metadata = {'candidate_id': 1, 'structs': records,
                    'layout_grid': {'column_x': original_x, 'row_y': original_y}}
        quality = {'per_net': {net: {'terminals': 3, 'wirelength': 1900,
                                    'detour_factor': 4, 'excess_detour': 1400}}}
        result = structured.feedback_candidate(pl, comps, nets, index, metadata, quality)
        clone, _, _, _, new_metadata = result
        cut = new_metadata['feedback_row_cut']
        self.assertEqual(clone.cell_of, original_cells)
        self.assertEqual(clone.orient, original_orientations)
        self.assertEqual(clone.column_x, original_x)
        self.assertEqual(clone.row_y, [y + (32 if row > cut else 0)
                                      for row, y in enumerate(original_y)])
        self.assertEqual(pl.row_y, original_y)
        self.assertNotIn('feedback', metadata)
        self.assertEqual(new_metadata['layout_grid']['row_y'], clone.row_y)
        self.assertEqual(new_metadata['feedback_from_net'], net)

    def test_feedback_does_not_expand_long_straight_two_terminal_trunks(self):
        pl, comps, nets, index, _, _, _, _ = seed_placement(3)
        quality = {'per_net': {net: {'terminals': 2, 'wirelength': 4000,
                                    'detour_factor': 5, 'excess_detour': 3000}
                               for net in nets}}
        self.assertIsNone(structured.feedback_candidate(pl, comps, nets, index, {}, quality))

    def test_compact_grid_reserves_oriented_symbols_halos_and_two_tracks(self):
        import random
        comps = [{'id': name, 'symbol': 'opamp', 'nodes': {}} for name in ('E1', 'E2', 'E3')]
        pl = structured.StructuredPlacement(comps, random.Random(1), [0, 0, 1], cols=2)
        pl.set_state([(0, 0), (0, 1), (1, 0)], [0, 0, 0])
        pl.column_x, pl.row_y = [64, 164], [64, 164]
        structured._enforce_geometry_floor(pl)
        self.assertGreaterEqual(pl.column_x[1] - pl.column_x[0], 88 + 24 + 20)
        self.assertGreaterEqual(pl.row_y[1] - pl.row_y[0], 68 + 24 + 20)

    def test_geometry_floor_preserves_verified_example_pitches(self):
        for number in range(1, 5):
            pl, *_ = seed_placement(number)
            previous = list(pl.column_x), list(pl.row_y)
            structured._enforce_geometry_floor(pl)
            self.assertEqual((pl.column_x, pl.row_y), previous)

    def test_latch_tail_gate_exposes_control_toward_its_clock_bank(self):
        pl, comps, _, index, _, _, records, _ = seed_placement(2)
        devices = parse_netlist(FROZEN / '2.cir')
        cells, orientations = list(pl.cell_of), list(pl.orient)
        adjustments = structured._orient_latch_clock_access(pl, devices, records, index)
        self.assertEqual(len(adjustments), 1)
        row = adjustments[0]
        tail = index[row['device']]
        self.assertEqual(pl.cell_of, cells)
        for i, orientation in enumerate(orientations):
            if i != tail:
                self.assertEqual(pl.orient[i], orientation)
        expected = int(row['clock_bank_median_x'] > pl.pos_of(tail)[0])
        self.assertEqual(pl.orient[tail], expected)
        # A differential pair alone is not evidence of a clocked latch.
        before = list(pl.orient)
        self.assertEqual(structured._orient_latch_clock_access(
            pl, devices, [r for r in records if r['kind'] != 'latch'], index), [])
        self.assertEqual(pl.orient, before)


if __name__ == '__main__':
    unittest.main()
