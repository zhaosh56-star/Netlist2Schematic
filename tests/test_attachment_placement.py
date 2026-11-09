"""Passive attachment seeding is transactional and leaves SA freedom intact."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from attachment_placement import (prepare_attachments, score_attachments,
                                  measure_parallel_coherence, refine_parallel_attachments)
from layout_relations import build_layout_relations, prepare_structure_placement
from netlist_core import parse_netlist, to_document
import structured_placement as structured
from tests.test_passive_placement import base_placement, convert, mos, source, resistor, amplifier


def tanks(extra=None):
    devices = {'left': mos('la', 'lb', '0'), 'right': mos('lb', 'la', '0')}
    cells = {'left': (2, 2), 'right': (2, 6)}
    for suffix, net, col in [('l', 'la', 0), ('r', 'lb', 8)]:
        for kind, row in [('R', 0), ('C', 1), ('L', 3)]:
            devices[kind+suffix] = resistor('vdd', net, kind)
            cells[kind+suffix] = (row, col)
    if extra:
        devices.update(extra)
        for number, name in enumerate(extra):
            cells[name] = (0, 3+number)
    pl, comps, nets, index, _ = convert(devices, cells, blocks=[['left', 'right']])
    return devices, pl, comps, nets, index, build_layout_relations(devices)


class AttachmentPreparationTests(unittest.TestCase):
    def test_cross_coupled_tanks_use_unique_drain_despite_extra_gate(self):
        devices, pl, comps, nets, index, relations = tanks()
        frozen = deepcopy((pl.cell_of, pl.orient, pl.column_x, pl.row_y, pl.blocks))
        with mock.patch('orthogonal_router.route_scene', side_effect=AssertionError('A* called')):
            result, report = prepare_attachments(pl, devices, comps, nets, index, relations)
        loads = [row for row in report['groups'] if row['kind'] == 'parallel_load']
        self.assertEqual(len(loads), 2)
        for group in loads:
            actual = [result.cell_of[index[name]] for name in group['members']]
            self.assertEqual(len({row for row, col in actual}), 1)
            self.assertEqual(sorted(col for row, col in actual), list(range(min(c for r, c in actual), max(c for r, c in actual)+1)))
        self.assertEqual((pl.cell_of, pl.orient, pl.column_x, pl.row_y, pl.blocks), frozen)
        self.assertEqual(result.blocks, pl.blocks)
        self.assertTrue(result.valid_state())
        self.assertTrue(result.blocks_contiguous())
        self.assertLess(report['after']['weighted_total'], report['before']['weighted_total'])
        json.dumps(report)

    def test_additional_source_terminal_makes_drain_anchor_ambiguous(self):
        for extra in ({'other': mos('dx', 'gx', 'la')}, {'src': source('ISRC', 'la')}):
            devices, pl, comps, nets, index, relations = tanks(extra)
            _, report = prepare_attachments(pl, devices, comps, nets, index, relations)
            load_nodes = {row['node'] for row in report['groups'] if row['kind'] == 'parallel_load'}
            self.assertNotIn('la', load_nodes)
            self.assertIn('lb', load_nodes)

    def test_different_endpoints_never_become_a_parallel_load_group(self):
        devices, pl, comps, nets, index, relations = tanks()
        devices['Cl']['b'] = 'unrelated'
        # Rebuild the canonical graph, matching source changes before preparing.
        doc = to_document(devices)
        comps, nets = doc['components'], doc['nets']
        pl.comps = comps
        _, report = prepare_attachments(pl, devices, comps, nets, index, build_layout_relations(devices))
        group = next(row for row in report['groups'] if row['kind'] == 'parallel_load' and row['node'] == 'la')
        self.assertEqual(set(group['members']), {'Rl', 'Ll'})
        self.assertNotIn('Cl', group['members'])

    def test_nonpassive_devices_and_fixed_group_members_never_move(self):
        devices, pl, comps, nets, index, relations = tanks({'bias': source('VSRC', 'v'), 'amp': amplifier()})
        pl.fixed.add(index['Rl'])
        pl.fixed_cells[index['Rl']] = pl.cell_of[index['Rl']]
        result, report = prepare_attachments(pl, devices, comps, nets, index, relations)
        protected = [i for i, comp in enumerate(comps) if comp['symbol'] not in ('resistor', 'capacitor', 'inductor', 'diode')]
        protected += [index[name] for name in ('Rl', 'Cl', 'Ll')]
        for i in protected:
            self.assertEqual(result.cell_of[i], pl.cell_of[i])
            self.assertEqual(result.orient[i], pl.orient[i])
        self.assertTrue(any(row['reason'] == 'fixed_member' for row in report['skipped']))

    def test_fixed_policy_skips_cross_row_seeding(self):
        devices, pl, comps, nets, index, relations = tanks()
        pl.passive_policy = 'fixed'
        result, report = prepare_attachments(pl, devices, comps, nets, index, relations)
        self.assertEqual(report['reason'], 'fixed_passive_rows')
        self.assertEqual((result.cell_of, result.orient), (pl.cell_of, pl.orient))

    def test_no_legal_group_transaction_rolls_back_every_member(self):
        devices, pl, comps, nets, index, relations = tanks()
        with mock.patch('attachment_placement._legal', return_value=False):
            result, report = prepare_attachments(pl, devices, comps, nets, index, relations)
        self.assertEqual((result.cell_of, result.orient), (pl.cell_of, pl.orient))
        self.assertFalse(report['actions'])
        self.assertTrue(result.valid_state())

    def test_new_cohorts_are_not_added_to_rigid_blocks_and_can_cross_rows_later(self):
        devices, pl, comps, nets, index, relations = tanks()
        result, report = prepare_attachments(pl, devices, comps, nets, index, relations)
        initial_blocks = deepcopy(result.blocks)
        crossed = False
        for _ in range(500):
            before = list(result.cell_of)
            move = result.propose()
            crossed |= any(before[i][0] != result.cell_of[i][0] for i in result.passive_indices)
            self.assertTrue(result.valid_state())
            if move:
                result.set_state(*move)
        self.assertTrue(crossed)
        self.assertEqual(result.blocks, initial_blocks)

    def test_bank_placement_respects_active_block_interior(self):
        devices, pl, comps, nets, index, relations = tanks()
        prepare_structure_placement(pl, comps, index, relations)
        result, report = prepare_attachments(pl, devices, comps, nets, index, relations)
        self.assertTrue(result.blocks_contiguous())
        self.assertEqual(result.layout_macro_blocks, pl.layout_macro_blocks)


class AttachmentAssociationTests(unittest.TestCase):
    def test_series_chain_reuses_order_and_external_endpoint_anchors(self):
        devices = {'a': mos('left', 'ga'), 'b': mos('right', 'gb'),
                   'r': resistor('left', 'middle'), 'c': resistor('middle', 'right', 'C')}
        pl, comps, nets, index, _ = convert(devices, {'a': (1, 1), 'b': (1, 7), 'r': (3, 0), 'c': (0, 6)})
        result, report = prepare_attachments(pl, devices, comps, nets, index, build_layout_relations(devices))
        self.assertTrue(any(row['kind'] == 'series' for row in report['groups']))
        self.assertEqual(report['after']['link_count'], 2)
        self.assertTrue(result.valid_state())

    def test_explicit_feedback_ports_keep_shared_device_associations(self):
        devices = {'e': amplifier('input', 'input'), 'r': resistor('out', 'input')}
        pl, comps, nets, index, _ = convert(devices, {'e': (1, 3), 'r': (0, 0)})
        relations = [{'kind': 'feedback_network', 'members': ['r'], 'roles': {
            'amplifier': 'e', 'series': ['r'], 'input': port, 'shunts': []},
            'evidence': {'nets': {'path': ['out', 'input']}}} for port in ('plus', 'minus')]
        result, report = prepare_attachments(pl, devices, comps, nets, index, relations)
        # Shared OUT link deduplicates, while the two real input pins survive.
        self.assertEqual(report['after']['link_count'], 3)
        target_pins = {tuple(row['endpoints'][1]) for row in report['links']}
        self.assertIn(('e', 'IN+'), target_pins)
        self.assertIn(('e', 'IN-'), target_pins)
        self.assertTrue(any(row['reason'] == 'shared_members_already_placed' for row in report['skipped']))

    def test_scoring_uses_actual_original_pins_and_does_not_change_existing_weights(self):
        from passive_placement import WEIGHTS
        frozen = deepcopy(WEIGHTS)
        devices, pl, comps, nets, index, relations = tanks()
        result, report = prepare_attachments(pl, devices, comps, nets, index, relations)
        distances = []
        for row in report['links']:
            a, b = row['endpoints']
            p, q = result.pin_world(index[a[0]], a[1]), result.pin_world(index[b[0]], b[1])
            distances.append((abs(p[0]-q[0])+abs(p[1]-q[1]))/100)
        score = score_attachments(result)
        self.assertAlmostEqual(score['attachment_distance'], sum(distances)/len(distances))
        self.assertAlmostEqual(score['weighted_total'], score['attachment_distance']+score['parallel_coherence'])
        self.assertEqual(WEIGHTS, frozen)
        self.assertEqual(score_attachments(object())['weighted_total'], 0)

    def test_result_and_report_are_deterministic(self):
        devices, pl, comps, nets, index, relations = tanks()
        a, am = prepare_attachments(pl, devices, comps, nets, index, relations)
        b, bm = prepare_attachments(pl, devices, comps, nets, index, relations)
        self.assertEqual((a.cell_of, a.orient, am), (b.cell_of, b.orient, bm))


class ParallelCoherenceTests(unittest.TestCase):
    def test_horizontal_and_vertical_cohorts_are_both_supported(self):
        devices, pl, comps, nets, index, relations = tanks()
        prepared, report = prepare_attachments(pl, devices, comps, nets, index, relations)
        group = next(g for g in report['groups'] if g['kind'] == 'parallel_load')
        first = measure_parallel_coherence(prepared, index, [group])
        self.assertLess(first['coherence'], .1)
        cells, orient = list(prepared.cell_of), list(prepared.orient)
        for row, name in enumerate(group['members']):
            cells[index[name]] = (row, 0)
            orient[index[name]] = 1
        prepared.set_state(cells, orient)
        vertical = measure_parallel_coherence(prepared, index, [group])
        self.assertEqual(vertical['groups'][0]['shape'], 'vertical_bank')
        self.assertLess(vertical['coherence'], .1)

    def test_scattering_and_wrong_orientation_increase_coherence_cost(self):
        devices, pl, comps, nets, index, relations = tanks()
        prepared, report = prepare_attachments(pl, devices, comps, nets, index, relations)
        groups = [g for g in report['groups'] if g['kind'] == 'parallel_load']
        before = measure_parallel_coherence(prepared, index, groups)['coherence']
        cells, orient = list(prepared.cell_of), list(prepared.orient)
        i = index[groups[0]['members'][0]]
        cells[i], orient[i] = (4, 9), 1
        prepared.set_state(cells, orient)
        self.assertGreater(measure_parallel_coherence(prepared, index, groups)['coherence'], before)

    def test_no_parallel_bank_returns_the_exact_original_objects(self):
        from types import SimpleNamespace
        pl, metadata = SimpleNamespace(), {'attachment_measurement_groups': []}
        result, output = refine_parallel_attachments(None, pl, [], {}, {}, metadata)
        self.assertIs(result, pl)
        self.assertIs(output, metadata)

    def test_post_transaction_only_commits_statically_safe_improvements(self):
        from symmetry_layout import _static_safe
        from routing_rules import RoutingOptions
        devices, pl, comps, nets, index, relations = tanks()
        seeded, initial = prepare_attachments(pl, devices, comps, nets, index, relations)
        # Start from the original dispersed state, retaining verified ownership.
        seeded.set_state(pl.cell_of, pl.orient)
        metadata = {'attachment_measurement_groups': initial['groups']}
        original = deepcopy((seeded.cell_of, seeded.orient, metadata))
        with mock.patch('orthogonal_router.route_scene', side_effect=AssertionError('A* called')):
            result, output = refine_parallel_attachments(structured.par, seeded, comps, nets, index, metadata)
        report = output['parallel_attachment_refinement']
        self.assertGreater(report['accepted'], 0)
        self.assertLess(report['after']['coherence'], report['before']['coherence'])
        self.assertTrue(_static_safe(structured.par, result, nets, index, RoutingOptions(), None))
        self.assertLessEqual(report['estimated_hpwl_after'], report['estimated_hpwl_before']*1.15+1e-8)
        self.assertEqual((seeded.cell_of, seeded.orient, metadata), original)
        self.assertEqual(result.column_x, seeded.column_x)
        self.assertEqual(result.row_y, seeded.row_y)

    def test_failed_static_transactions_restore_original(self):
        devices, pl, comps, nets, index, relations = tanks()
        seeded, initial = prepare_attachments(pl, devices, comps, nets, index, relations)
        seeded.set_state(pl.cell_of, pl.orient)
        metadata = {'attachment_measurement_groups': initial['groups']}
        with mock.patch('symmetry_layout._static_safe', return_value=False):
            result, output = refine_parallel_attachments(structured.par, seeded, comps, nets, index, metadata)
        self.assertEqual((result.cell_of, result.orient), (seeded.cell_of, seeded.orient))
        self.assertEqual(output['parallel_attachment_refinement']['accepted'], 0)

    def test_zero_and_invalid_budgets_do_not_enter_static_checks(self):
        devices, pl, comps, nets, index, relations = tanks()
        seeded, initial = prepare_attachments(pl, devices, comps, nets, index, relations)
        metadata = {'attachment_measurement_groups': initial['groups']}
        for kwargs in ({'seconds': 0}, {'max_candidates': 0}):
            with mock.patch('symmetry_layout._static_safe', side_effect=AssertionError('no budget')):
                result, output = refine_parallel_attachments(structured.par, seeded, comps, nets, index, metadata, **kwargs)
            self.assertEqual((result.cell_of, result.orient), (seeded.cell_of, seeded.orient))
        with self.assertRaises(ValueError):
            refine_parallel_attachments(structured.par, seeded, comps, nets, index, metadata, seconds=-1)

    def test_fixed_members_and_active_devices_stay_put(self):
        devices, pl, comps, nets, index, relations = tanks()
        seeded, initial = prepare_attachments(pl, devices, comps, nets, index, relations)
        seeded.fixed.add(index['C1'] if 'C1' in index else index['Cl'])
        result, output = refine_parallel_attachments(structured.par, seeded, comps, nets, index,
            {'attachment_measurement_groups': initial['groups']})
        for i, comp in enumerate(comps):
            if comp['symbol'] in ('nmos', 'pmos') or i in seeded.fixed:
                self.assertEqual((result.cell_of[i], result.orient[i]), (seeded.cell_of[i], seeded.orient[i]))


if __name__ == '__main__':
    unittest.main()
