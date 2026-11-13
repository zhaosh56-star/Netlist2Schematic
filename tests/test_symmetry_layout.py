"""Device symmetry is topology-driven, bounded, and independent of routing."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
from time import perf_counter
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.netlist.netlist_core import parse_netlist, to_document
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.plugins.symmetry import symmetry_layout as symmetry


def mos(d, g, s, kind='NMOS'):
    return {'cls': 'MOS', 'type': kind, 'd': d, 'g': g, 's': s, 'bulk': s}


def bjt(c, b, e):
    return {'cls': 'BJT', 'type': 'NPN', 'c': c, 'b': b, 'e': e}


def current(p, n='0'):
    return {'cls': 'ISRC', 'type': 'I', 'p': p, 'n': n}


def resistor(a, b, kind='R'):
    return {'cls': 'PASSIVE', 'type': kind, 'a': a, 'b': b}


def fixture(devices=None, cells=None, orientations=None):
    devices = devices or {'left': mos('dl', 'il', 'tail'),
                          'right': mos('dr', 'ir', 'tail'), 'supply': current('tail')}
    document = to_document(devices)
    comps, nets = document['components'], document['nets']
    index = {comp['id']: i for i, comp in enumerate(comps)}
    cells = cells or {'left': (0, 1), 'right': (0, 5), 'supply': (1, 1)}
    pl = structured.StructuredPlacement.__new__(structured.StructuredPlacement)
    pl.comps, pl.rng = comps, random.Random(11)
    pl.cell_of = [tuple(cells[name]) for name in devices]
    pl.orient = [(orientations or {}).get(name, 0) for name in devices]
    pl.row_of = [r for r, _ in pl.cell_of]
    pl.cols = 9
    pl.comp_at = {cell: i for i, cell in enumerate(pl.cell_of)}
    pl.blocks = [[i] for i in range(len(comps))]
    pl.block_of = {i: i for i in range(len(comps))}
    pl.role_orders = []
    pl.fixed_cells, pl.fixed = {}, set()
    pl.column_x = [80 + c * 120 for c in range(pl.cols)]
    pl.row_y = [60 + r * 140 for r in range(max(pl.row_of) + 2)]
    return pl, comps, nets, index, {'structs': analyze_structures(devices)}


class SymmetryTopologyTests(unittest.TestCase):
    def test_missing_visible_ports_is_safe_empty_mock(self):
        self.assertEqual(symmetry.build_symmetry_groups([{'id': 'm', 'symbol': 'nmos'}]), [])
        report = symmetry.report_symmetry(object(), [{'id': 'm', 'symbol': 'nmos'}], {'m': 0}, {})
        self.assertEqual(report['status'], 'skipped')
        self.assertEqual(report['current']['relation_count'], 0)

    def test_mos_pair_requires_tail_and_never_uses_names(self):
        _, comps, _, _, metadata = fixture()
        groups = symmetry.build_symmetry_groups(comps, metadata['structs'])
        self.assertEqual(groups[0]['pairs'], [['left', 'right']])
        self.assertEqual(groups[0]['tails'], ['supply'])
        no_tail = [row for row in comps if row['id'] != 'supply']
        self.assertEqual(symmetry.build_symmetry_groups(no_tail), [])

    def test_bjt_pair_has_strict_tail_evidence(self):
        devices = {'a': bjt('da', 'ia', 'tail'), 'b': bjt('db', 'ib', 'tail'),
                   'i': current('tail')}
        comps = to_document(devices)['components']
        self.assertEqual(symmetry.build_symmetry_groups(comps)[0]['kind'], 'bjt_differential_pair')
        self.assertEqual(symmetry.build_symmetry_groups(comps[:2]), [])
        devices['a']['e'] = devices['b']['e'] = '0'
        self.assertEqual(symmetry.build_symmetry_groups(to_document(devices)['components']), [])

    def test_multioutput_mirror_is_not_arbitrarily_paired(self):
        devices = {'ref': mos('bias', 'bias', '0'), 'a': mos('a', 'bias', '0'),
                   'b': mos('b', 'bias', '0')}
        self.assertEqual(symmetry.build_symmetry_groups(to_document(devices)['components']), [])

    def test_paired_passive_loads_extend_topology(self):
        devices = {'a': mos('la', 'ga', 'tail'), 'b': mos('lb', 'gb', 'tail'),
                   'i': current('tail'), 'r': resistor('vdd', 'la'),
                   's': resistor('lb', 'vdd')}
        groups = symmetry.build_symmetry_groups(to_document(devices)['components'])
        pair_sets = {frozenset(pair) for group in groups for pair in group['pairs']}
        self.assertIn(frozenset(('r', 's')), pair_sets)
        self.assertLessEqual(len(groups[0]['members']), 16)

    def test_ambiguous_parallel_loads_do_not_pair(self):
        devices = {'a': mos('la', 'ga', 'tail'), 'b': mos('lb', 'gb', 'tail'),
                   'i': current('tail'), 'r': resistor('vdd', 'la'),
                   'r2': resistor('vdd', 'la'), 's': resistor('vdd', 'lb')}
        groups = symmetry.build_symmetry_groups(to_document(devices)['components'])
        self.assertEqual(len(groups[0]['pairs']), 1)

    def test_folded_axes_remain_separate(self):
        devices = {name: mos(name+'d', name+'g', '0') for name in 'abcdef'}
        comps = to_document(devices)['components']
        records = [{'kind': 'folded_core', 'confidence': 'high', 'roles': {
            'input_left': 'a', 'input_right': 'b', 'folded_left': 'c',
            'folded_right': 'd', 'bias_left': 'e', 'bias_right': 'f'}}]
        groups = symmetry.build_symmetry_groups(comps, records)
        self.assertEqual({group['axis_family'] for group in groups}, {'folded_input', 'folded_output'})
        self.assertEqual(sorted(len(group['pairs']) for group in groups), [1, 2])

    def test_rename_and_reorder_preserve_pairing(self):
        _, comps, _, _, _ = fixture()
        renamed = deepcopy(comps)
        mapping = {row['id']: 'x'+str(20-i) for i, row in enumerate(renamed)}
        inverse = {value: key for key, value in mapping.items()}
        for row in renamed:
            row['id'] = mapping[row['id']]
            row['nodes'] = {pin: ('z'+net if net != '0' else net) for pin, net in row['nodes'].items()}
        first = symmetry.build_symmetry_groups(comps)
        second = symmetry.build_symmetry_groups(list(reversed(renamed)))
        self.assertEqual({frozenset(pair) for g in first for pair in g['pairs']}, {
            frozenset(inverse[name] for name in pair) for g in second for pair in g['pairs']})

    def test_real_ground_truth_redrawing_strongarm_and_folded(self):
        cases = [('1-10/redrawing.cir', 'main'),
                 ('11-15/strongarm-comparator.cir', 'dut'),
                 ('16+/analoggenie-295-folded-cascode-amplifier-with-nmos-input-pair-and-rc-input-feedback-networks.cir', 'dut')]
        outputs = []
        for relative, cell in cases:
            path = ROOT / 'test_data' / relative
            if not path.is_file():
                self.skipTest('optional local ground-truth dataset not present')
            original = path.read_bytes()
            devices = parse_netlist(path, cell=cell)
            groups = symmetry.build_symmetry_groups(to_document(devices)['components'], analyze_structures(devices))
            self.assertEqual(path.read_bytes(), original)
            for group in groups:
                flat = [name for pair in group['pairs'] for name in pair]
                self.assertEqual(len(flat), len(set(flat)))
                self.assertLessEqual(len(group['members']), 16)
            outputs.append(groups)
        self.assertEqual(len(outputs[0][0]['pairs']), 4)
        # The latch and independent input pair remain separate local axes.
        self.assertGreaterEqual(sum(len(group['pairs']) for group in outputs[1]), 4)
        self.assertTrue(any(group['kind'] == 'latch' for group in outputs[1]))
        self.assertEqual({group['axis_family'] for group in outputs[2]}, {'folded_input', 'folded_output'})
        self.assertTrue(any({'R1', 'R2'} <= set(group['members']) for group in outputs[2]))

    def test_expansion_capacity_discards_whole_depth(self):
        devices = {'a': mos('la', 'ga', 'tail'), 'b': mos('lb', 'gb', 'tail'),
                   'i': current('tail')}
        for number, kind in enumerate(('R', 'C', 'L', 'D')):
            for side, origin in (('l', 'la'), ('r', 'lb')):
                middle = side + str(number)
                devices[kind + side] = resistor(origin, middle, kind)
                devices[kind + side + '_next'] = resistor(middle, side + '_end' + str(number), kind)
        groups = symmetry.build_symmetry_groups(to_document(devices)['components'])
        self.assertEqual(len(groups), 1)
        # Initial pair plus all four depth-1 pairs; all four depth-2 pairs are
        # discarded together instead of retaining named members up to the cap.
        self.assertEqual(len(groups[0]['pairs']), 5)
        self.assertFalse(any('_next' in name for name in groups[0]['members']))

    def test_two_differential_stages_do_not_share_axis_via_control_wires(self):
        devices = {'a': mos('la', 'ia', 'tail1'), 'b': mos('lb', 'ib', 'tail1'),
                   'i': current('tail1'), 'c': mos('lc', 'la', 'tail2'),
                   'd': mos('ld', 'lb', 'tail2'), 'j': current('tail2')}
        groups = symmetry.build_symmetry_groups(to_document(devices)['components'])
        self.assertEqual(len(groups), 2)
        self.assertEqual(sorted(len(group['pairs']) for group in groups), [1, 1])


class SymmetryMeasurementsTests(unittest.TestCase):
    def test_matching_origins_are_not_enough_for_mos_mirror(self):
        pl, comps, _, index, metadata = fixture(cells={
            'left': (0, 1), 'right': (0, 5), 'supply': (1, 3)})
        groups = symmetry.build_symmetry_groups(comps, metadata['structs'])
        first = symmetry.measure_symmetry(pl, comps, index, groups)
        pl.orient[index['right']] = 1
        second = symmetry.measure_symmetry(pl, comps, index, groups)
        self.assertGreater(first['residual'], 0)
        self.assertEqual(second['residual'], 0)
        self.assertEqual(second['failed_relations'], 0)
        json.dumps(second)

    def test_translation_preserves_measurement(self):
        pl, comps, _, index, metadata = fixture()
        groups = symmetry.build_symmetry_groups(comps, metadata['structs'])
        first = symmetry.measure_symmetry(pl, comps, index, groups)
        pl.column_x = [x + 401 for x in pl.column_x]
        pl.row_y = [y - 65 for y in pl.row_y]
        second = symmetry.measure_symmetry(pl, comps, index, groups)
        self.assertEqual(first['residual'], second['residual'])
        self.assertEqual(second['groups'][0]['axis_x'], first['groups'][0]['axis_x'] + 401)

    def test_report_refresh_keeps_history_without_mutation(self):
        pl, comps, _, index, metadata = fixture()
        report = symmetry.report_symmetry(pl, comps, index, metadata)
        report.update(status='improved', actions=[{'kind': 'historic'}], after={'retained': True})
        metadata['layout_symmetry'] = report
        original = deepcopy(metadata)
        pl.orient[index['right']] = 1
        refreshed = symmetry.report_symmetry(pl, comps, index, metadata)
        self.assertEqual(refreshed['after'], {'retained': True})
        self.assertEqual(refreshed['actions'], [{'kind': 'historic'}])
        self.assertEqual(refreshed['status'], 'improved')
        self.assertEqual(metadata, original)

    def test_fixed_class_current_rows_rebuilt_on_passive_change(self):
        pl, _, _, _, _ = fixture()
        cells = list(pl.cell_of)
        cells[0] = (1, cells[0][1])
        symmetry._set_state(pl, cells, pl.orient)
        self.assertEqual(pl.row_of, [row for row, _ in cells])

    def test_custom_grid_controls_failure_threshold(self):
        pl, comps, _, index, metadata = fixture(cells={
            'left': (0, 1), 'right': (0, 5), 'supply': (1, 3)})
        groups = symmetry.build_symmetry_groups(comps, metadata['structs'])
        self.assertGreater(symmetry.measure_symmetry(pl, comps, index, groups, 1)['failed_relations'], 0)
        self.assertEqual(symmetry.measure_symmetry(pl, comps, index, groups, 100)['failed_relations'], 0)


class SymmetryRegularizerTests(unittest.TestCase):
    def test_hashseed_does_not_change_bounded_regularization(self):
        # Multiple passive kinds on a crossed pair exercise same-depth set
        # traversal and the finite candidate order, without any A* calls.
        content = '''.model NM NMOS
M1 a b 0 0 NM
M2 b a 0 0 NM
C1 vdd b 1p
CP vdd a 1p
L1 vdd a 1n
LP vdd b 1n
R2 vdd b 1k
RP vdd a 1k
.end
'''
        script = '''import contextlib,io,json,sys
from netlist2schematic.layout import structured_placement as st
from netlist2schematic.plugins.symmetry.symmetry_layout import regularize_symmetry
with contextlib.redirect_stdout(io.StringIO()):
 for p,c,n,x,m in st.create_candidates(sys.argv[1],iterations=300,seed=20261029,deadline=None):
  if m['candidate_id']==2:break
 p,m=regularize_symmetry(st.par,p,c,n,x,m,seconds=1,max_candidates=128)
print(json.dumps({'cells':p.cell_of,'orient':p.orient,'after':m['layout_symmetry']['after']},sort_keys=True))
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pair.cir'
            path.write_text(content, encoding='utf-8')
            results = []
            for seed in ('0', '1'):
                environment = dict(os.environ, PYTHONHASHSEED=seed)
                result = subprocess.run([sys.executable, '-c', script, str(path)],
                                        cwd=ROOT, env=environment, capture_output=True,
                                        text=True, timeout=20, check=True)
                results.append(json.loads(result.stdout))
        self.assertEqual(results[0], results[1])

    def test_regularization_improves_without_sa_astar_or_input_mutation(self):
        pl, comps, nets, index, metadata = fixture()
        frozen = deepcopy((pl.cell_of, pl.orient, pl.column_x, pl.row_y, metadata))
        with mock.patch('netlist2schematic.routing.orthogonal_router.route_scene', side_effect=AssertionError('A* called')):
            with mock.patch.object(structured, '_anneal', side_effect=AssertionError('SA called')):
                result, updated = symmetry.regularize_symmetry(
                    structured.par, pl, comps, nets, index, metadata)
        report = updated['layout_symmetry']
        self.assertEqual(report['status'], 'improved')
        self.assertLess(report['after']['residual'], report['before']['residual'])
        self.assertEqual(result.column_x, pl.column_x)
        self.assertEqual(result.row_y, pl.row_y)
        self.assertEqual([r for r, _ in result.cell_of], [r for r, _ in pl.cell_of])
        self.assertEqual((pl.cell_of, pl.orient, pl.column_x, pl.row_y, metadata), frozen)
        self.assertLessEqual(report['estimated_hpwl_after'], report['estimated_hpwl_before'] * 1.25)
        self.assertLessEqual(report['candidates'], 128)
        json.dumps(report)

    def test_role_adjustment_orientation_is_protected(self):
        pl, comps, nets, index, metadata = fixture()
        metadata['role_adjustments'] = [{'device': 'right', 'kind': 'latch_clock_access'}]
        result, _ = symmetry.regularize_symmetry(structured.par, pl, comps, nets, index, metadata)
        self.assertEqual(result.orient[index['right']], pl.orient[index['right']])

    def test_fixed_cells_and_orientation_are_protected(self):
        pl, comps, nets, index, metadata = fixture()
        pl.fixed = {index['left'], index['supply']}
        pl.fixed_cells = {i: pl.cell_of[i] for i in pl.fixed}
        result, _ = symmetry.regularize_symmetry(structured.par, pl, comps, nets, index, metadata)
        for i in pl.fixed:
            self.assertEqual(result.cell_of[i], pl.cell_of[i])
            self.assertEqual(result.orient[i], pl.orient[i])

    def test_exhausted_budgets_preserve_original(self):
        for kwargs in ({'seconds': 0}, {'max_candidates': 0}, {'max_rounds': 0},
                       {'deadline': perf_counter()-1}):
            with self.subTest(kwargs=kwargs):
                pl, comps, nets, index, metadata = fixture()
                with mock.patch.object(symmetry, '_proposals', side_effect=AssertionError('empty budget expanded proposals')):
                    result, updated = symmetry.regularize_symmetry(
                        structured.par, pl, comps, nets, index, metadata, **kwargs)
                self.assertEqual((result.cell_of, result.orient), (pl.cell_of, pl.orient))
                self.assertEqual(updated['layout_symmetry']['accepted'], 0)

    def test_static_conflict_rolls_back_candidate(self):
        pl, comps, nets, index, metadata = fixture()
        with mock.patch.object(symmetry, '_static_safe', return_value=False):
            result, updated = symmetry.regularize_symmetry(
                structured.par, pl, comps, nets, index, metadata)
        self.assertEqual((result.cell_of, result.orient), (pl.cell_of, pl.orient))
        self.assertEqual(updated['layout_symmetry']['accepted'], 0)

    def test_hpwl_excess_does_not_commit(self):
        pl, comps, nets, index, metadata = fixture()
        def hpwl(par, candidate, nets, index):
            return 10.0 if (candidate.cell_of, candidate.orient) == (pl.cell_of, pl.orient) else 100.0
        with mock.patch.object(symmetry, '_hpwl', side_effect=hpwl):
            result, updated = symmetry.regularize_symmetry(
                structured.par, pl, comps, nets, index, metadata)
        self.assertEqual((result.cell_of, result.orient), (pl.cell_of, pl.orient))
        self.assertEqual(updated['layout_symmetry']['accepted'], 0)

    def test_fixed_input_is_deterministic_and_second_run_stable(self):
        pl, comps, nets, index, metadata = fixture()
        a, am = symmetry.regularize_symmetry(structured.par, pl, comps, nets, index, metadata)
        b, bm = symmetry.regularize_symmetry(structured.par, pl, comps, nets, index, metadata)
        self.assertEqual((a.cell_of, a.orient), (b.cell_of, b.orient))
        self.assertEqual(am['layout_symmetry']['after'], bm['layout_symmetry']['after'])
        c, cm = symmetry.regularize_symmetry(structured.par, a, comps, nets, index, am)
        self.assertEqual((c.cell_of, c.orient), (a.cell_of, a.orient))

    def test_new_feedback_transaction_resets_before_to_current_geometry(self):
        pl, comps, nets, index, metadata = fixture()
        first, previous = symmetry.regularize_symmetry(structured.par, pl, comps, nets, index, metadata)
        first.row_y = [y + (32 if r else 0) for r, y in enumerate(first.row_y)]
        first.orient[index['right']] = 0
        expected = symmetry.measure_symmetry(first, comps, index,
                                            previous['layout_symmetry']['relations'])
        _, updated = symmetry.regularize_symmetry(structured.par, first, comps, nets, index,
                                                   previous, seconds=0)
        self.assertEqual(updated['layout_symmetry']['before'], expected)
        self.assertEqual(updated['layout_symmetry']['actions'], [])

    def test_invalid_budgets_are_rejected(self):
        pl, comps, nets, index, metadata = fixture()
        for kwargs in ({'seconds': -1}, {'seconds': float('nan')},
                       {'max_candidates': 1.5}, {'max_rounds': -1}):
            with self.assertRaises(ValueError):
                symmetry.regularize_symmetry(structured.par, pl, comps, nets, index, metadata, **kwargs)


if __name__ == '__main__':
    unittest.main()
