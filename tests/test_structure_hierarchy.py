"""Composite movement must not erase confirmed row-local child constraints."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from netlist2schematic.layout.structure_hierarchy import (
    compile_hierarchy, hierarchy_report, order_children)
from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.netlist.netlist_core import to_document
from tests.test_circuit_structures import mos, analyze_structures
from tests.test_structured_placement import seed_placement


def folded_devices():
    # Tail -> input pair -> two folded nodes -> common-gate output branches.
    return {'A': mos('x', 'ip', 'tail'), 'B': mos('y', 'im', 'tail'),
            'T': mos('tail', 'bias', '0'),
            'P': mos('x', 'pb', 'vdd', 'PMOS'), 'Q': mos('y', 'pb', 'vdd', 'PMOS'),
            'F': mos('out1', 'pc', 'x', 'PMOS'),
            'H': mos('out2', 'pc', 'y', 'PMOS')}


def placement(devices=None, variant=0):
    devices = devices if devices is not None else folded_devices()
    doc = to_document(devices)
    comps, nets = doc['components'], doc['nets']
    index = {c['id']: i for i, c in enumerate(comps)}
    levels = structured.dl.compute_levels(devices)
    conduction = {d[k] for d in devices.values() for k in ('d', 's')}
    att = structured.legacy.attach_info(devices, levels['net_levels'], conduction)
    records = analyze_structures(devices)
    pl, alignment, pairs = structured._initial_placement(
        devices, comps, nets, index, levels, att, records, 83, variant,
        passive_layout='flexible')
    return pl, nets, index, alignment, pairs, records


class StructureHierarchyTests(unittest.TestCase):
    def test_recognition_retains_child_and_its_ownership(self):
        pl, _, index, _, _, records = placement()
        core = next(r for r in records if r['kind'] == 'folded_core')
        pair = next(r for r in records if r['kind'] == 'differential_pair')
        self.assertEqual(pair['parent'], core['id'])
        self.assertTrue(all(pair['owner'][n] == pair['id'] for n in ('A', 'B')))
        self.assertIn(set((index['A'], index['B'])), [set(g) for g in pl.hierarchy_guards])

    def test_all_initial_variants_keep_children_contiguous(self):
        for variant in range(3):
            pl, _, index, _, _, _ = placement(variant=variant)
            self.assertTrue(pl.blocks_contiguous())
            self.assertEqual(hierarchy_report(pl)['metrics']['interleaving'], 0)
            self.assertEqual(abs(pl.cell_of[index['A']][1] - pl.cell_of[index['B']][1]), 1)

    def test_parent_members_cannot_be_inserted_into_input_child(self):
        pl, _, index, _, _, _ = placement()
        a, b, f = (index[n] for n in ('A', 'B', 'F'))
        row = pl.cell_of[a][0]
        self.assertEqual(row, pl.cell_of[f][0])
        cells = list(pl.cell_of)
        original_columns = sorted(c for r, c in cells if r == row)
        members = sorted((i for i, (r, _) in enumerate(cells) if r == row),
                         key=lambda i: (i not in (a, b, f), {a: 0, f: 1, b: 2}.get(i, i)))
        for col, i in zip(original_columns, members):
            cells[i] = row, col
        pl.set_state(cells, pl.orient)
        # Old parent-only protection still accepts this exact state.
        guards = pl.hierarchy_guards
        pl.hierarchy_guards = []
        self.assertTrue(pl.blocks_contiguous())
        pl.hierarchy_guards = guards
        self.assertFalse(pl.blocks_contiguous())
        self.assertGreater(hierarchy_report(pl)['metrics']['interleaving'], 0)

    def test_annealing_proposals_rollback_and_best_restore_keep_children(self):
        pl, nets, index, alignment, pairs, _ = placement()
        rows = list(pl.row_of)
        for step in range(1200):
            before = list(pl.cell_of), list(pl.orient)
            old = pl.propose()
            self.assertTrue(pl.blocks_contiguous())
            self.assertEqual(len(pl.comp_at), len(pl.comps))
            self.assertEqual(pl.row_of, rows)
            self.assertEqual(hierarchy_report(pl)['metrics']['interleaving'], 0)
            if old is not None and step % 2:
                pl.set_state(*old)
                self.assertEqual((pl.cell_of, pl.orient), before)
        structured._anneal(pl, nets, index, alignment, pairs, 800, None)
        self.assertTrue(pl.blocks_contiguous())
        self.assertEqual(hierarchy_report(pl)['metrics']['interleaving'], 0)

    def test_free_space_between_child_members_is_legal_not_a_rigid_template(self):
        pl, _, index, _, _, _ = placement()
        # A synthetic row isolates the child from any parent boundary.
        a, b = index['A'], index['B']
        cells = [(n + 1, 0) for n in range(len(pl.comps))]
        cells[a], cells[b] = (0, 0), (0, 2)
        pl.set_state(cells, pl.orient)
        self.assertTrue(pl.blocks_contiguous())

    def test_fixed_child_does_not_move_or_turn_in_collective_moves(self):
        pl, _, index, _, _, _ = placement()
        i = index['A']
        pl.fixed = {i}
        pl.fixed_cells = {i: pl.cell_of[i]}
        fixed = pl.cell_of[i], pl.orient[i]
        for _ in range(800):
            pl.propose()
            self.assertEqual((pl.cell_of[i], pl.orient[i]), fixed)

    def test_partial_overlap_is_reported_not_transitively_made_rigid(self):
        comps = [{'id': n} for n in ('a', 'b', 'c', 'd')]
        index = {c['id']: i for i, c in enumerate(comps)}
        records = [dict(id=str(k), kind='differential_pair', confidence='high', devices=members)
                   for k, members in enumerate((['a', 'b'], ['b', 'c']))]
        plan, guards = compile_hierarchy(records, comps, index, [0] * 4, [list(range(4))])
        self.assertEqual(guards, [])
        self.assertTrue(all(g['protection'] == 'ambiguous_overlap' for g in plan['groups']))

    def test_row_mismatch_is_reported_not_silently_claimed_fixed(self):
        comps = [{'id': n} for n in ('a', 'b')]
        plan, guards = compile_hierarchy([
            dict(id='pair', kind='differential_pair', devices=['a', 'b'])],
            comps, {'a': 0, 'b': 1}, [0, 1], [[0, 1]])
        self.assertEqual(guards, [])
        self.assertEqual(plan['skipped'][0]['reason'], 'different_fixed_rows')

    def test_relation_only_common_supply_bias_does_not_create_guards(self):
        records = [dict(id='common', kind='common_bias_pair', confidence='relation', devices=['a', 'b'])]
        plan, guards = compile_hierarchy(records, [{'id': 'a'}, {'id': 'b'}, {'id': 'c'}],
                                        {'a': 0, 'b': 1, 'c': 2}, [0] * 3, [[0, 1, 2]])
        self.assertEqual(plan['groups'], [])
        self.assertEqual(guards, [])

    def test_vertical_complementary_branch_is_not_a_broken_same_row_pair(self):
        plan, _ = compile_hierarchy([
            dict(id='branch', kind='complementary_branch', devices=['a', 'b'])],
            [{'id': 'a'}, {'id': 'b'}], {'a': 0, 'b': 1}, [0, 1], [[0, 1]])
        self.assertEqual(plan['skipped'], [])

    def test_hierarchical_ordering_handles_nested_and_disjoint_children(self):
        result = order_children([0, 2, 4, 1, 3, 5], [[0, 1], [2, 3], [0, 1, 2, 3]])
        for group in ([0, 1], [2, 3], [0, 1, 2, 3]):
            positions = [result.index(i) for i in group]
            self.assertEqual(max(positions) - min(positions) + 1, len(group))
        self.assertEqual(sorted(result), list(range(6)))

    def test_rename_reorder_polarity_duality_preserves_guard_members(self):
        devices = folded_devices()
        pl, _, index, _, _, _ = placement(devices)
        expected = {frozenset(pl.comps[i]['id'] for i in g) for g in pl.hierarchy_guards}
        names = {n: f'z{100-i}' for i, n in enumerate(devices)}
        reverse = {v: k for k, v in names.items()}
        for dual in (False, True):
            transformed = {}
            for n, d in reversed(list(devices.items())):
                d = deepcopy(d)
                for pin in ('d', 'g', 's', 'bulk'):
                    net = d[pin]
                    d[pin] = ({'0': 'vdd', 'vdd': '0'}.get(net, net) if dual else net)
                    if d[pin] not in ('vdd', '0'):
                        d[pin] = 'n_' + d[pin]
                if dual:
                    d['type'] = {'NMOS': 'PMOS', 'PMOS': 'NMOS'}[d['type']]
                transformed[names[n]] = d
            # Compare at equivalent display rows: the separate legacy leveler
            # can choose different rows under polarity duality. This guard must
            # neither repair nor silently override that independent row plan.
            doc = to_document(transformed)
            comps = doc['components']
            ix = {c['id']: i for i, c in enumerate(comps)}
            rows = [pl.row_of[index[reverse[c['id']]]] for c in comps]
            records = analyze_structures(transformed)
            _, _, blocks = structured._relations(transformed, records, ix, rows)
            _, guards = compile_hierarchy(records, comps, ix, rows, blocks)
            actual = {frozenset(reverse[comps[i]['id']] for i in g) for g in guards}
            self.assertEqual(expected, actual)

    def test_report_remeasures_saved_or_mirrored_coordinates_without_moving(self):
        pl, _, _, _, _, _ = placement()
        before = list(pl.cell_of), list(pl.orient)
        saved = deepcopy(pl.structure_hierarchy)
        first = hierarchy_report(pl, saved)
        pl.set_state([(r, pl.cols - 1 - c) for r, c in pl.cell_of], pl.orient)
        mirrored = list(pl.cell_of), list(pl.orient)
        second = hierarchy_report(pl, saved)
        self.assertEqual(first['metrics'], second['metrics'])
        self.assertEqual((pl.cell_of, pl.orient), mirrored)
        self.assertEqual(saved, pl.structure_hierarchy)
        self.assertNotEqual(before, mirrored)

    def test_no_astar_is_called_during_initialization_or_annealing(self):
        from netlist2schematic.routing import orthogonal_router
        with patch.object(orthogonal_router, '_find_path', side_effect=AssertionError('A* in SA')):
            pl, nets, index, alignment, pairs, _ = placement()
            structured._anneal(pl, nets, index, alignment, pairs, 100, None)

    def test_latch_child_cross_pair_and_complementary_branches_remain_supported(self):
        pl, _, _, _, _, _, records, _ = seed_placement(2)
        self.assertTrue(any(r['kind'] == 'latch' for r in records))
        self.assertTrue(pl.blocks_contiguous())
        self.assertEqual(hierarchy_report(pl)['metrics']['interleaving'], 0)


if __name__ == '__main__':
    unittest.main()
