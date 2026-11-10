"""Flexible passive proposals preserve active constraints and snapshot truth."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import random
import sys
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from circuit_structures import analyze_structures
from netlist_core import parse_netlist, to_document
import structured_placement as structured
from passive_placement import (FlexiblePassivePlacement, make_flexible,
                               passive_diagnostics, score_passives)


def resistor(a, b, kind='R'):
    return {'cls': 'PASSIVE', 'type': kind, 'a': a, 'b': b, 'value': '1'}


def mos(drain, gate, source='0'):
    return {'cls': 'MOS', 'type': 'NMOS', 'd': drain, 'g': gate, 's': source,
            'bulk': '0', 'model': 'NMOS'}


def source(cls, positive, negative='0'):
    return {'cls': cls, 'type': 'V' if cls == 'VSRC' else 'I',
            'p': positive, 'n': negative, 'value': '1'}


def amplifier(plus='fp', minus='fm'):
    return {'cls': 'VCVS', 'type': 'E', 'op': 'out', 'on': '0',
            'ip': plus, 'in': minus, 'value': '100k'}


def base_placement(devices, cells, orientations=None, blocks=(), seed=123):
    """Use real symbol geometry without spending random initial-placement draws."""
    doc = to_document(devices)
    comps, nets = doc['components'], doc['nets']
    index = {comp['id']: i for i, comp in enumerate(comps)}
    pl = structured.StructuredPlacement.__new__(structured.StructuredPlacement)
    pl.comps = comps
    pl.rng = random.Random(seed)
    pl.cell_of = [tuple(cells[name]) for name in devices]
    pl.orient = [0 if orientations is None else orientations.get(name, 0)
                 for name in devices]
    pl.row_of = [row for row, _ in pl.cell_of]
    pl.cols = max(8, max(col for _, col in pl.cell_of) + 1)
    pl.comp_at = {cell: i for i, cell in enumerate(pl.cell_of)}
    pl.blocks = [[index[name] for name in block] for block in blocks]
    grouped = {i for block in pl.blocks for i in block}
    pl.blocks.extend([i] for i in range(len(comps)) if i not in grouped)
    pl.block_of = {i: number for number, block in enumerate(pl.blocks) for i in block}
    pl.role_orders = []
    pl.fixed_cells, pl.fixed = {}, set()
    pl.column_x = [80 + 120 * col for col in range(pl.cols)]
    pl.row_y = [60 + 140 * row for row in range(max(3, max(pl.row_of) + 1))]
    return pl, comps, nets, index


def convert(devices, cells, orientations=None, blocks=(), records=None, seed=123):
    pl, comps, nets, index = base_placement(devices, cells, orientations, blocks, seed)
    records = analyze_structures(devices) if records is None else records
    flexible = make_flexible(pl, devices, nets, index, records,
                             column_pitch=120, row_pitch=140)
    return flexible, comps, nets, index, records


def mixed_fixture(seed=123):
    devices = {
        'M1': mos('d1', 'g1'), 'M2': mos('d2', 'g2'),
        'Q1': {'cls': 'BJT', 'type': 'NPN', 'c': 'qc', 'b': 'qb', 'e': '0',
               'model': 'NPN'},
        'E1': amplifier(), 'V1': source('VSRC', 'vin'), 'I1': source('ISRC', 'ib'),
        'R1': resistor('d1', '0'), 'C1': resistor('d2', '0', 'C'),
        'L1': resistor('out', 'ln', 'L'), 'D1': resistor('diode', '0', 'D'),
    }
    cells = {'M1': (1, 1), 'M2': (1, 3), 'Q1': (2, 1), 'E1': (0, 4),
             'V1': (0, 0), 'I1': (2, 4), 'R1': (0, 2), 'C1': (2, 2),
             'L1': (0, 6), 'D1': (2, 6)}
    return devices, cells, convert(devices, cells, blocks=[['M1', 'M2']], seed=seed)


def real_opamp(seed=77):
    devices = parse_netlist(ROOT / 'tests/fixtures/aesthetic-baseline/1.cir')
    doc = to_document(devices)
    comps, nets = doc['components'], doc['nets']
    index = {comp['id']: i for i, comp in enumerate(comps)}
    levels = structured.dl.compute_levels(devices)
    att = structured.legacy.attach_info(devices, levels['net_levels'], set())
    records = analyze_structures(devices)
    # This deliberately restores the original fixed-row initial seed, then
    # exercises the new class separately from its public-flow dispatch.
    pl, alignment, pairs = structured._initial_placement(
        devices, comps, nets, index, levels, att, records, seed, 0)
    pl = make_flexible(pl, devices, nets, index, records)
    return pl, comps, nets, index, alignment, pairs


class FlexiblePassiveMovementTests(unittest.TestCase):
    def assert_consistent(self, pl):
        self.assertEqual(pl.row_of, [row for row, _ in pl.cell_of])
        self.assertEqual(pl.comp_at, {cell: i for i, cell in enumerate(pl.cell_of)})
        self.assertEqual(len(set(pl.cell_of)), len(pl.comps))
        self.assertTrue(pl.blocks_contiguous())
        for i, row in pl.fixed_rows.items():
            self.assertEqual(pl.cell_of[i][0], row)
        for i, comp in enumerate(pl.comps):
            self.assertIn(pl.orient[i], pl.allowed_orientations(i))
            if comp['symbol'] in ('nmos', 'pmos', 'npn', 'pnp'):
                self.assertIn(pl.orient[i], (0, 1))

    def test_factory_preserves_seed_state_and_random_stream_but_adds_room(self):
        devices, cells, _ = mixed_fixture()
        original, comps, nets, index = base_placement(devices, cells, blocks=[['M1', 'M2']])
        initial = deepcopy(original.__dict__)
        rng_state = original.rng.getstate()
        original_devices = deepcopy(devices)
        records = analyze_structures(devices)
        record_snapshot = deepcopy(records)
        result = make_flexible(original, devices, nets, index, records,
                               column_pitch=120, row_pitch=140)
        self.assertIsInstance(result, FlexiblePassivePlacement)
        self.assertIsNot(result, original)
        self.assertEqual(result.cell_of, initial['cell_of'])
        self.assertEqual(result.orient, initial['orient'])
        self.assertEqual(original.rng.getstate(), rng_state)
        self.assertEqual(result.rng.getstate(), rng_state)
        self.assertIsNot(result.rng, original.rng)
        self.assertEqual(original.cell_of, initial['cell_of'])
        self.assertEqual(original.column_x, initial['column_x'])
        self.assertEqual(original.row_y, initial['row_y'])
        self.assertEqual(result.column_x, initial['column_x'] +
                         [initial['column_x'][-1] + 120, initial['column_x'][-1] + 240])
        self.assertEqual(result.row_y, initial['row_y'] + [initial['row_y'][-1] + 140])
        self.assertEqual(result.cols, original.cols + 2)
        self.assertEqual(result.fixed_rows,
                         {index[name]: cells[name][0] for name, d in devices.items()
                          if d['cls'] != 'PASSIVE'})
        self.assertEqual(set(result.passive_indices),
                         {index[name] for name, d in devices.items() if d['cls'] == 'PASSIVE'})
        self.assertEqual(devices, original_devices)
        self.assertEqual(records, record_snapshot)
        self.assert_consistent(result)

    def test_no_passive_returns_the_original_class_without_random_draws(self):
        devices = {'M1': mos('d', 'g'), 'E1': amplifier()}
        pl, _, nets, index = base_placement(devices, {'M1': (1, 1), 'E1': (0, 4)})
        before = pl.rng.getstate(), list(pl.cell_of), list(pl.column_x), list(pl.row_y)
        result = make_flexible(pl, devices, nets, index, [])
        self.assertIs(result, pl)
        self.assertIs(type(result), structured.StructuredPlacement)
        self.assertEqual((pl.rng.getstate(), pl.cell_of, pl.column_x, pl.row_y), before)

    def test_each_passive_family_can_enter_an_active_row_without_moving_active(self):
        _, _, (pl, _, _, index, _) = mixed_fixture()
        axes = deepcopy((pl.column_x, pl.row_y))
        for name in ('R1', 'C1', 'L1', 'D1'):
            with self.subTest(name=name):
                before = list(pl.cell_of), list(pl.orient)
                previous = pl._passive_move(index[name], (1, 0))
                self.assertIsNotNone(previous)
                self.assertEqual(pl.cell_of[index[name]], (1, 0))
                for i in pl.fixed_rows:
                    self.assertEqual(pl.cell_of[i], before[0][i])
                    self.assertEqual(pl.orient[i], before[1][i])
                self.assertEqual((pl.column_x, pl.row_y), axes)
                self.assert_consistent(pl)
                pl.set_state(*previous)
                self.assertEqual((pl.cell_of, pl.orient), before)
                self.assert_consistent(pl)

    def test_new_move_rejects_every_nonpassive_occupant_and_source(self):
        _, _, (pl, _, _, index, _) = mixed_fixture()
        before = list(pl.cell_of), list(pl.orient)
        for name in ('M1', 'M2', 'Q1', 'E1', 'V1', 'I1'):
            with self.subTest(name=name):
                self.assertIsNone(pl._passive_move(index['R1'], pl.cell_of[index[name]]))
                self.assertIsNone(pl._passive_move(index[name], (3, 0)))
                self.assertEqual((pl.cell_of, pl.orient), before)
                self.assert_consistent(pl)

    def test_empty_gap_inside_active_group_is_protected(self):
        _, _, (pl, _, _, index, _) = mixed_fixture()
        self.assertNotIn((1, 2), pl.comp_at)
        before = list(pl.cell_of), list(pl.orient)
        self.assertIsNone(pl._passive_move(index['R1'], (1, 2)))
        self.assertEqual((pl.cell_of, pl.orient), before)
        self.assert_consistent(pl)

    def test_passives_can_swap_across_rows_and_fully_undo(self):
        _, _, (pl, _, _, index, _) = mixed_fixture()
        before = list(pl.cell_of), list(pl.orient)
        r, c = index['R1'], index['C1']
        previous = pl._passive_move(r, pl.cell_of[c])
        self.assertIsNotNone(previous)
        self.assertEqual(pl.cell_of[r], before[0][c])
        self.assertEqual(pl.cell_of[c], before[0][r])
        self.assertEqual(pl.orient, before[1])
        self.assert_consistent(pl)
        pl.set_state(*previous)
        self.assertEqual((pl.cell_of, pl.orient), before)
        self.assert_consistent(pl)

    def test_bounds_and_same_cell_fail_without_changing_geometry(self):
        _, _, (pl, _, _, index, _) = mixed_fixture()
        before = deepcopy((pl.cell_of, pl.orient, pl.column_x, pl.row_y))
        for target in ((-1, 0), (0, -1), (len(pl.row_y), 0), (0, pl.cols),
                       pl.cell_of[index['R1']]):
            with self.subTest(target=target):
                self.assertIsNone(pl._passive_move(index['R1'], target))
                self.assertEqual((pl.cell_of, pl.orient, pl.column_x, pl.row_y), before)
                self.assert_consistent(pl)
        previous = pl._passive_move(index['R1'], (len(pl.row_y)-1, pl.cols-1))
        self.assertIsNotNone(previous)
        self.assert_consistent(pl)

    def test_random_accept_reject_sequences_keep_dynamic_rows_and_axes(self):
        _, _, (pl, _, _, _, _) = mixed_fixture(seed=33)
        axes = deepcopy((pl.column_x, pl.row_y))
        fixed = deepcopy(pl.fixed_rows)
        initial = list(pl.cell_of), list(pl.orient)
        crossed = active_changed = False
        for step in range(1800):
            before = list(pl.cell_of), list(pl.orient)
            previous = pl.propose()
            crossed |= any(before[0][i][0] != pl.cell_of[i][0] for i in pl.passive_indices)
            active_changed |= any((before[0][i], before[1][i]) !=
                                  (pl.cell_of[i], pl.orient[i]) for i in fixed)
            self.assert_consistent(pl)
            self.assertEqual(pl.fixed_rows, fixed)
            self.assertEqual((pl.column_x, pl.row_y), axes)
            if previous and step % 2:
                pl.set_state(*previous)
                self.assertEqual((pl.cell_of, pl.orient), before)
                self.assert_consistent(pl)
        self.assertTrue(crossed)
        self.assertTrue(active_changed, 'Original horizontal/orientation proposals must remain available')
        pl.set_state(*initial)
        self.assert_consistent(pl)
        json.loads(json.dumps(passive_diagnostics(pl)))

    def test_new_proposal_branch_alone_cannot_touch_nonpassive_state(self):
        _, _, (pl, _, _, _, _) = mixed_fixture(seed=8)
        initial = {i: (pl.cell_of[i], pl.orient[i]) for i in pl.fixed_rows}
        accepted = 0
        with mock.patch.object(structured.StructuredPlacement, 'propose', return_value=None) as old:
            for _ in range(600):
                previous = pl.propose()
                accepted += previous is not None
                self.assertEqual({i: (pl.cell_of[i], pl.orient[i]) for i in pl.fixed_rows}, initial)
                self.assert_consistent(pl)
        self.assertGreater(accepted, 0)
        self.assertGreater(old.call_count, 0)

    def test_temperature_sampling_restores_the_initial_state(self):
        pl, _, nets, index, alignment, pairs = real_opamp()
        before = deepcopy((pl.cell_of, pl.orient, pl.column_x, pl.row_y))
        structured._anneal(pl, nets, index, alignment, pairs, 0, None)
        self.assertEqual((pl.cell_of, pl.orient, pl.column_x, pl.row_y), before)
        self.assert_consistent(pl)

    def test_reheat_and_best_restore_do_not_leave_stale_passive_rows(self):
        pl, _, nets, index, alignment, pairs = real_opamp()
        before = deepcopy((pl.cell_of, pl.orient, pl.column_x, pl.row_y))
        with mock.patch.object(structured, '_estimate', return_value=(0.0, {})):
            _, _, annealing = structured._anneal(pl, nets, index, alignment, pairs, 800, None)
        self.assertGreaterEqual(annealing['reheats'], 1)
        self.assertEqual((pl.cell_of, pl.orient, pl.column_x, pl.row_y), before)
        self.assert_consistent(pl)

    def test_expired_annealing_retains_a_crossed_row_state(self):
        pl, _, nets, index, alignment, pairs = real_opamp()
        r4 = index['R4']
        target = next((r, c) for r in range(len(pl.row_y)) for c in range(pl.cols)
                      if r != pl.cell_of[r4][0] and (r, c) not in pl.comp_at)
        self.assertIsNotNone(pl._passive_move(r4, target))
        before = list(pl.cell_of), list(pl.orient)
        _, _, annealing = structured._anneal(pl, nets, index, alignment, pairs,
                                              20000, time.perf_counter()-1)
        self.assertEqual(annealing['stop_reason'], 'quality_budget')
        self.assertEqual((pl.cell_of, pl.orient), before)
        self.assert_consistent(pl)

    def test_flexible_annealing_is_repeatable_without_final_astar(self):
        results = []
        with mock.patch('orthogonal_router.route_scene', side_effect=AssertionError('A* in SA')), \
                mock.patch('orthogonal_router._find_path', side_effect=AssertionError('A* in SA')):
            for _ in range(2):
                pl, _, nets, index, alignment, pairs = real_opamp(seed=91)
                result = structured._anneal(pl, nets, index, alignment, pairs, 600, None)
                self.assert_consistent(pl)
                results.append((list(pl.cell_of), list(pl.orient), result,
                                passive_diagnostics(pl)))
        self.assertEqual(results[0], results[1])


class PassiveRegularityTests(unittest.TestCase):
    def test_physical_translation_preserves_every_regularity_term(self):
        devices = {'E1': amplifier(), 'RP': resistor('out', 'fp'),
                   'RN': resistor('out', 'fm'), 'RB': resistor('fp', 'fm'),
                   'RG': resistor('fm', '0'), 'CG': resistor('fm', '0', 'C')}
        pl, *_ = convert(devices, {'E1': (1, 5), 'RP': (0, 3), 'RN': (2, 3),
                                   'RB': (1, 1), 'RG': (2, 0), 'CG': (0, 0)})
        expected = score_passives(pl)
        for field in ('series_adjacency', 'parallel_alignment', 'feedback_locality'):
            self.assertGreater(expected[field], 0, 'Exercise each real geometric relation')
        before = deepcopy((pl.cell_of, pl.orient))
        pins = {(i, pin): pl.pin_world(i, pin)
                for i, comp in enumerate(pl.comps) for pin in comp['nodes']}
        dx, dy = -137.25, 83.5
        pl.column_x = [x + dx for x in pl.column_x]
        pl.row_y = [y + dy for y in pl.row_y]
        for (i, pin), (x, y) in pins.items():
            self.assertEqual(pl.pin_world(i, pin), (x + dx, y + dy))
        self.assertEqual((pl.cell_of, pl.orient), before)
        actual = score_passives(pl)
        self.assertEqual(actual['relation_counts'], expected['relation_counts'])
        for field in ('series_adjacency', 'parallel_alignment', 'feedback_locality', 'weighted_total'):
            self.assertAlmostEqual(actual[field], expected[field], places=12)

    def test_legal_horizontal_mirror_preserves_terminal_regularity(self):
        # Vertical passives remain legal under reflection; MOS uses its allowed
        # mirror. Horizontal passives and opamps have narrower orientation sets.
        devices = {'M1': mos('reference', 'reference'), 'M2': mos('out', 'reference'),
                   'R1': resistor('out', 'middle'), 'L1': resistor('middle', 'load', 'L'),
                   'R2': resistor('load', '0'), 'C2': resistor('load', '0', 'C')}
        pl, *_ = convert(devices, {'M1': (1, 1), 'M2': (2, 4), 'R1': (0, 2),
                                   'L1': (2, 3), 'R2': (1, 5), 'C2': (3, 6)})
        expected = score_passives(pl)
        self.assertGreater(expected['series_adjacency'], 0)
        self.assertGreater(expected['parallel_alignment'], 0)
        pins = {(i, pin): pl.pin_world(i, pin)
                for i, comp in enumerate(pl.comps) for pin in comp['nodes']}
        reflected = [(row, pl.cols - 1 - col) for row, col in pl.cell_of]
        orientations = [1 - pl.orient[i] if comp['symbol'] == 'nmos' else pl.orient[i]
                        for i, comp in enumerate(pl.comps)]
        pl.set_state(reflected, orientations)
        self.assertTrue(pl.valid_state())
        reflection_sum = pl.column_x[0] + pl.column_x[-1]
        for (i, pin), (x, y) in pins.items():
            self.assertEqual(pl.pin_world(i, pin), (reflection_sum - x, y))
        actual = score_passives(pl)
        self.assertEqual(actual['relation_counts'], expected['relation_counts'])
        for field in ('series_adjacency', 'parallel_alignment', 'feedback_locality', 'weighted_total'):
            self.assertAlmostEqual(actual[field], expected[field], places=12)

    def test_series_adjacency_grows_with_shared_pin_separation(self):
        devices = {'R1': resistor('input', 'middle'), 'R2': resistor('middle', 'output')}
        pl, _, _, index, _ = convert(devices, {'R1': (1, 1), 'R2': (2, 1)})
        near = score_passives(pl)
        self.assertIsNotNone(pl._passive_move(index['R2'], (3, 1)))
        far = score_passives(pl)
        self.assertGreater(far['series_adjacency'], near['series_adjacency'])
        self.assertGreater(far['weighted_total'], near['weighted_total'])

    def test_parallel_vertical_and_horizontal_packages_both_align(self):
        devices = {'R1': resistor('node', '0'), 'C1': resistor('node', '0', 'C')}
        for cells, orientations in (({'R1': (1, 1), 'C1': (1, 3)}, None),
                                    ({'R1': (0, 1), 'C1': (2, 1)}, {'R1': 1, 'C1': 1})):
            with self.subTest(orientations=orientations):
                pl, *_ = convert(devices, cells, orientations)
                self.assertAlmostEqual(score_passives(pl)['parallel_alignment'], 0.0)
        pl, _, _, index, _ = convert(devices, {'R1': (1, 1), 'C1': (1, 3)})
        self.assertIsNotNone(pl._passive_move(index['C1'], (2, 3)))
        self.assertGreater(score_passives(pl)['parallel_alignment'], 0)
        pl.set_state([(1, 1), (1, 3)], [0, 1])
        self.assertGreater(score_passives(pl)['parallel_alignment'], 0)

    def feedback_fixture(self, positive=False):
        devices = {'E1': amplifier('fb' if positive else 'vin', 'vin' if positive else 'fb'),
                   'RF': resistor('out', 'fb'), 'RG': resistor('fb', '0')}
        return devices, convert(devices, {'E1': (1, 4), 'RF': (0, 2), 'RG': (0, 0)},
                                {'RF': 1})

    def test_feedback_uses_the_real_plus_or_minus_pin(self):
        _, plus = self.feedback_fixture(positive=True)
        _, minus = self.feedback_fixture(positive=False)
        self.assertLess(score_passives(minus[0])['feedback_locality'],
                        score_passives(plus[0])['feedback_locality'])

    def test_feedback_ground_branch_is_attracted_to_its_input(self):
        _, (pl, _, _, index, _) = self.feedback_fixture()
        far = score_passives(pl)['feedback_locality']
        self.assertIsNotNone(pl._passive_move(index['RG'], (1, 3)))
        self.assertLess(score_passives(pl)['feedback_locality'], far)

    def test_duplicate_reports_do_not_multiply_regularization(self):
        devices = {'E1': amplifier(), 'RS': resistor('out', 'shared'),
                   'RP': resistor('shared', 'fp'), 'RN': resistor('shared', 'fm'),
                   'GP': resistor('fp', '0'), 'GN': resistor('fm', '0')}
        cells = {'E1': (1, 4), 'RS': (0, 4), 'RP': (0, 2), 'RN': (2, 2),
                 'GP': (0, 0), 'GN': (2, 0)}
        records = analyze_structures(devices)
        feedbacks = [r for r in records if r['kind'] == 'feedback_network']
        self.assertEqual(len(feedbacks), 2)
        self.assertTrue(all('RS' in r['devices'] for r in feedbacks))
        first, *_ = convert(devices, cells, records=records)
        duplicate, *_ = convert(devices, cells, records=records + deepcopy(records))
        self.assertEqual(score_passives(first), score_passives(duplicate))
        self.assertEqual(len(first.comps), len(devices))
        self.assertEqual(len(set(first.cell_of)), len(devices))

    def test_shared_feedback_bridge_keeps_both_real_input_ports(self):
        # The bridge participates in the alternate path to each amplifier
        # input. Both of its terminal-to-input distances must contribute.
        devices = {'E1': amplifier(), 'RP': resistor('out', 'fp'),
                   'RN': resistor('out', 'fm'), 'RB': resistor('fp', 'fm')}
        pl, _, _, index, _ = convert(
            devices, {'E1': (1, 5), 'RP': (0, 3), 'RN': (2, 3), 'RB': (1, 1)})
        links = [('RP', '1', 'OUT'), ('RP', '2', 'IN+'),
                 ('RN', '1', 'OUT'), ('RN', '2', 'IN-'),
                 ('RB', '1', 'IN+'), ('RB', '2', 'IN-')]
        total_distance = 0
        for component, terminal, input_port in links:
            pin = pl.pin_world(index[component], terminal)
            port = pl.pin_world(index['E1'], input_port)
            total_distance += abs(pin[0] - port[0]) + abs(pin[1] - port[1])
        actual = score_passives(pl)
        self.assertEqual(actual['relation_counts']['feedback'], len(links))
        self.assertAlmostEqual(actual['feedback_locality'], total_distance / (len(links) * 100))

    def test_regularization_is_name_and_statement_order_independent(self):
        devices, (original, _, _, _, _) = self.feedback_fixture(positive=True)
        expected = score_passives(original)
        cells = {comp['id']: original.cell_of[i] for i, comp in enumerate(original.comps)}
        orientations = {comp['id']: original.orient[i] for i, comp in enumerate(original.comps)}
        names = {'E1': 'E77', 'RF': 'R82', 'RG': 'R73'}
        net_names = {'out': 'node_c', 'fb': 'node_a', 'vin': 'node_b'}
        changed = {}
        for name in reversed(list(devices)):
            changed[names[name]] = {key: net_names.get(value, value)
                                    if key in ('a', 'b', 'op', 'on', 'ip', 'in') else value
                                    for key, value in devices[name].items()}
        renamed, *_ = convert(changed, {names[name]: cell for name, cell in cells.items()},
                               {names[name]: value for name, value in orientations.items()})
        actual = score_passives(renamed)
        for field in ('series_adjacency', 'parallel_alignment', 'feedback_locality', 'weighted_total'):
            self.assertAlmostEqual(actual[field], expected[field], places=12)
        self.assertEqual(actual['relation_counts'], expected['relation_counts'])


if __name__ == '__main__':
    unittest.main()
