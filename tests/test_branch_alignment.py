"""Physical branch refinement preserves logical state and the geometry oracle."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import branch_alignment as align
from netlist_core import to_document
from orthogonal_router import RoutingFailure
from routing_rules import RoutingOptions
from sa_common import load_local

PAR = load_local('branch_alignment_test_symbols', 'place-and-route.py')


class OffsetPlacement(PAR.Placement):
    """Use the agreed offset contract independently of integration timing."""

    def pos_of(self, i):
        row, col = self.cell_of[i]
        return self.column_x[col] + getattr(self, 'offset_x', [0]*len(self.comps))[i], self.row_y[row]

    def allowed_orientations(self, i):
        return (0, 1) if self.comps[i]['symbol'] in align.ACTIVE else tuple(
            range(len(PAR.GEO_DOC[self.comps[i]['symbol']]['orientations'])))


def mos(d, g, s, kind='NMOS'):
    return dict(cls='MOS', type=kind, d=d, g=g, s=s, bulk=s)


def fixture(devices=None, cells=None, orientations=None, records=None):
    devices = devices or {
        'input_a': mos('oa', 'ina', 'tail'), 'input_b': mos('ob', 'inb', 'tail'),
        'tail_device': mos('tail', 'bias', '0'),
        'load_a': dict(cls='PASSIVE', type='R', a='vdd', b='oa'),
        'load_b': dict(cls='PASSIVE', type='R', a='vdd', b='ob'),
    }
    cells = cells or {'input_a': (1, 2), 'input_b': (1, 3), 'tail_device': (2, 2),
                      'load_a': (0, 2), 'load_b': (0, 3)}
    orientations = orientations or {'input_b': 1}
    document = to_document(devices)
    comps, nets = document['components'], document['nets']
    index = {row['id']: i for i, row in enumerate(comps)}
    pl = OffsetPlacement.__new__(OffsetPlacement)
    pl.comps = comps
    pl.cell_of = [cells[name] for name in devices]
    pl.row_of = [row for row, col in pl.cell_of]
    pl.orient = [orientations.get(name, 0) for name in devices]
    pl.comp_at = {cell: i for i, cell in enumerate(pl.cell_of)}
    pl.column_x = [100*i for i in range(8)]
    pl.row_y = [100+140*i for i in range(5)]
    pl.fixed, pl.fixed_cells = set(), {}
    pl.offset_x = [0.0]*len(comps)
    if records is None:
        records = [{'id': 'input', 'kind': 'differential_pair', 'members': ['input_a', 'input_b'],
                    'roles': {'left': 'input_a', 'right': 'input_b',
                              'tail': ['tail_device'], 'exclusive_tails': ['tail_device']}}]
    return pl, comps, nets, index, {'layout_relations': records}


def bridge_fixture(*, reversed_pins=False, horizontal=False, multiple=False):
    devices = {'upper': mos('upper_out', 'upper_gate', 'entry'),
               'lower': mos('exit', 'lower_gate', '0')}
    members = ['coil']
    if multiple:
        devices['resistor'] = dict(cls='PASSIVE', type='R', a='entry', b='middle')
        devices['coil'] = dict(cls='PASSIVE', type='L', a='middle', b='exit')
        members = ['resistor', 'coil']
        path = ['entry', 'middle', 'exit']
    else:
        devices['coil'] = dict(cls='PASSIVE', type='L',
                               a='exit' if reversed_pins else 'entry',
                               b='entry' if reversed_pins else 'exit')
        path = ['entry', 'exit']
    cells = {'upper': (0, 2), 'lower': (len(members)+1, 2)}
    cells.update({name: (number+1, 2) for number, name in enumerate(members)})
    orientations = {name: 1 if horizontal else 0 for name in members}
    case = fixture(devices, cells, orientations, records=[])
    pins = {name: {'in': '2' if reversed_pins and name == 'coil' and not multiple else '1',
                   'out': '1' if reversed_pins and name == 'coil' and not multiple else '2'}
            for name in members}
    connections = [{'kind': 'rl_bridge_axis', 'net': path[0],
                    'pins': [['upper', 'S'], [members[0], pins[members[0]]['in']]]}]
    for number in range(1, len(members)):
        previous, current = members[number-1], members[number]
        connections.append({'kind': 'rl_bridge_axis', 'net': path[number],
                            'pins': [[previous, pins[previous]['out']], [current, pins[current]['in']]]})
    connections.append({'kind': 'rl_bridge_axis', 'net': path[-1],
                        'pins': [[members[-1], pins[members[-1]]['out']], ['lower', 'D']]})
    case[4]['branch_row_plan'] = {'bridge_alignment': [{
        'id': 'bridge', 'upper': 'upper', 'upper_pin': 'S', 'lower': 'lower', 'lower_pin': 'D',
        'members': members, 'member_pins': pins, 'path': path,
        'start_net': path[0], 'end_net': path[-1], 'connections': connections}]}
    return case


class BranchAlignmentTests(unittest.TestCase):
    def regularize(self, case, **kwargs):
        return align.regularize_branch_layout(PAR, *case, **kwargs)

    def assert_logical_unchanged(self, before, after):
        for key in ('cell_of', 'row_of', 'comp_at', 'column_x', 'row_y'):
            self.assertEqual(getattr(before, key), getattr(after, key), key)

    def test_public_measurement_is_read_only_and_accepts_missing_offsets(self):
        case = fixture()
        del case[0].offset_x
        before, metadata = deepcopy(case[0].__dict__), deepcopy(case[4])
        measured = align.measure_branch_layout(PAR, *case)
        changed, report = self.regularize(case)
        self.assertEqual(report['branch_alignment']['before'], measured)
        self.assertEqual(before, case[0].__dict__)
        self.assertEqual(metadata, case[4])
        self.assertFalse(hasattr(case[0], 'offset_x'))

    def test_unique_vertical_rl_bridge_aligns_all_actual_conduction_pins(self):
        case = bridge_fixture()
        changed, data = self.regularize(case)
        metrics = align.measure_branch_layout(PAR, changed, *case[1:4], data)
        self.assertEqual(1, metrics['bridge_relations'])
        self.assertLessEqual(metrics['axis_error'], 2)
        self.assertEqual(0, metrics['bridge_channel_error'])
        xs = [changed.pin_world(case[3][name], pin)[0]
              for name, pin in (('upper', 'S'), ('coil', '1'), ('coil', '2'), ('lower', 'D'))]
        self.assertLessEqual(max(xs)-min(xs), 2)
        self.assert_logical_unchanged(case[0], changed)
        self.assertTrue(align._static_safe(PAR, changed, case[2], case[3], RoutingOptions(), None))

    def test_only_a_verified_bridge_can_rotate_a_horizontal_inductor_vertical(self):
        case = bridge_fixture(horizontal=True)
        before = align.measure_branch_layout(PAR, *case)
        self.assertGreater(before['bridge_channel_error'], 0)
        changed, data = self.regularize(case)
        self.assertEqual('north', changed.pin_dir(case[3]['coil'], '1'))
        self.assertEqual('south', changed.pin_dir(case[3]['coil'], '2'))
        self.assertLessEqual(data['branch_alignment']['after']['axis_error'], 2)
        bare = deepcopy(case)
        bare[4].pop('branch_row_plan')
        untouched, _ = self.regularize(bare)
        self.assertEqual(bare[0].orient[bare[3]['coil']], untouched.orient[bare[3]['coil']])

    def test_reversed_rl_pin_names_follow_actual_net_entry_and_exit(self):
        case = bridge_fixture(reversed_pins=True, horizontal=True)
        changed, data = self.regularize(case)
        self.assertEqual('north', changed.pin_dir(case[3]['coil'], '2'))
        self.assertEqual('south', changed.pin_dir(case[3]['coil'], '1'))
        self.assertLessEqual(data['branch_alignment']['after']['axis_error'], 2)

    def test_multimember_rl_chain_is_one_axis_without_copying_any_device(self):
        case = bridge_fixture(multiple=True, horizontal=True)
        changed, data = self.regularize(case)
        self.assertEqual(4, len(changed.comps))
        self.assertEqual(1, data['branch_alignment']['after']['bridge_relations'])
        self.assertLessEqual(data['branch_alignment']['after']['axis_error'], 2)
        self.assertEqual(0, data['branch_alignment']['after']['bridge_channel_error'])

    def test_reference_measurement_connections_do_not_invent_a_row_plan(self):
        case = bridge_fixture(horizontal=True)
        expected = align.measure_branch_layout(PAR, *case)
        data = deepcopy(case[4])
        data['branch_measurement_connections'] = data.pop('branch_row_plan')['bridge_alignment']
        original = deepcopy(data)
        actual = align.measure_branch_layout(PAR, *case[:4], data)
        self.assertEqual(expected, actual)
        self.assertEqual(original, data)
        self.assertNotIn('branch_row_plan', data)

    def test_duplicate_bridge_hints_do_not_double_count_errors(self):
        case = bridge_fixture(horizontal=True)
        expected = align.measure_branch_layout(PAR, *case)
        case[4]['branch_measurement_connections'] = deepcopy(case[4]['branch_row_plan']['bridge_alignment'])
        self.assertEqual(expected, align.measure_branch_layout(PAR, *case))

    def test_incorrect_bridge_connection_and_nonconduction_anchor_are_rejected(self):
        for corruption in ('net', 'anchor'):
            with self.subTest(corruption=corruption):
                case = bridge_fixture(horizontal=True)
                bridge = case[4]['branch_row_plan']['bridge_alignment'][0]
                if corruption == 'net':
                    bridge['connections'][0]['net'] = 'wrong_net'
                else:
                    bridge['upper_pin'] = 'G'
                measured = align.measure_branch_layout(PAR, *case)
                self.assertEqual(0, measured['bridge_relations'])
                changed, data = self.regularize(case)
                self.assertEqual(case[0].orient[case[3]['coil']], changed.orient[case[3]['coil']])
                self.assertTrue(data['branch_alignment']['relations']['skipped'])

    def test_fixed_horizontal_bridge_member_is_not_rotated_or_displaced(self):
        case = bridge_fixture(horizontal=True)
        i = case[3]['coil']
        case[0].fixed.add(i)
        changed, data = self.regularize(case)
        self.assertEqual(case[0].orient[i], changed.orient[i])
        self.assertEqual(case[0].pos_of(i), changed.pos_of(i))
        self.assertGreater(data['branch_alignment']['after']['bridge_channel_error'], 0)

    def test_real_pin_load_axis_and_exclusive_mos_tail_are_repaired(self):
        case = fixture()
        pl, comps, nets, index, metadata = case
        original = deepcopy(pl.__dict__)
        changed, data = self.regularize(case)
        report = data['branch_alignment']
        self.assertEqual('improved', report['status'])
        self.assertGreater(report['before']['axis_error'], report['after']['axis_error'])
        self.assertLessEqual(report['after']['axis_error'], 2)
        self.assertLessEqual(report['after']['tail_error'], 2)
        self.assertEqual(0, report['after']['input_access_errors'])
        self.assertEqual(original, pl.__dict__)
        self.assertNotIn('branch_alignment', metadata)
        self.assert_logical_unchanged(pl, changed)
        self.assertTrue(align._static_safe(PAR, changed, nets, index, RoutingOptions(), None))
        self.assertLessEqual(report['estimated_hpwl_after'], 1.15*report['estimated_hpwl_before']+1e-8)
        json.dumps(report)

    def test_external_input_mirror_retains_each_conduction_axis(self):
        devices = {'a': mos('oa', 'ina', 'tail'), 'b': mos('ob', 'inb', 'tail'),
                   'source': dict(cls='ISRC', type='I', p='tail', n='0')}
        records = [{'kind': 'differential_pair', 'roles': {'left': 'a', 'right': 'b',
                    'tail': ['source'], 'exclusive_tails': ['source']}}]
        case = fixture(devices, {'a': (1, 2), 'b': (1, 3), 'source': (2, 3)},
                       {'a': 1, 'b': 0}, records)
        pl, comps, nets, index, _ = case
        old_axes = {name: pl.pin_world(index[name], 'D')[0] for name in ('a', 'b')}
        changed, report = self.regularize(case)
        self.assertEqual('west', changed.pin_dir(index['a'], 'G'))
        self.assertEqual('east', changed.pin_dir(index['b'], 'G'))
        for name in ('a', 'b'):
            self.assertEqual(old_axes[name], changed.pin_world(index[name], 'D')[0])
        self.assertEqual(0, report['branch_alignment']['after']['input_access_errors'])

    def test_internal_gate_network_does_not_force_outward_access(self):
        devices = {'a': mos('oa', 'ina', 'tail'), 'b': mos('ob', 'inb', 'tail'),
                   'source': dict(cls='ISRC', type='I', p='tail', n='0'),
                   'feedback': dict(cls='PASSIVE', type='R', a='ina', b='ob')}
        records = [{'kind': 'differential_pair', 'roles': {'left': 'a', 'right': 'b',
                    'tail': ['source'], 'exclusive_tails': ['source']}}]
        case = fixture(devices, {'a': (1, 2), 'b': (1, 3), 'source': (2, 3), 'feedback': (0, 4)},
                       {'a': 1, 'b': 0}, records)
        changed, data = self.regularize(case)
        self.assertEqual(case[0].orient, changed.orient)
        self.assertEqual(0, data['branch_alignment']['after']['input_pairs'])

    def test_shared_formal_controls_support_four_bases_per_external_net(self):
        devices, cells, orientations, records = {}, {}, {}, []
        for number in range(4):
            left, right, supply = 'left_'+str(number), 'right_'+str(number), 'tail_'+str(number)
            common = 'emitter_'+str(number)
            devices[left] = dict(cls='BJT', type='NPN', c='oa_'+str(number), b='alpha', e=common)
            devices[right] = dict(cls='BJT', type='NPN', c='ob_'+str(number), b='beta', e=common)
            devices[supply] = dict(cls='ISRC', type='I', p=common, n='0')
            cells[left], cells[right], cells[supply] = (1, 2*number), (1, 2*number+1), (2, 2*number)
            orientations[left], orientations[right] = 1, 0
            records.append({'kind': 'bjt_differential_pair', 'roles': {
                'left': left, 'right': right, 'tail': [supply], 'exclusive_tails': [supply]}})
        case = fixture(devices, cells, orientations, records)
        case[0].column_x = [160*i for i in range(8)]
        without_formals = align.measure_branch_layout(PAR, *case)
        self.assertEqual(0, without_formals['input_pairs'])
        case[4]['external_control_ports'] = ['alpha', 'beta']
        before = align.measure_branch_layout(PAR, *case)
        self.assertEqual(4, before['input_pairs'])
        self.assertEqual(8, before['input_access_errors'])
        changed, data = self.regularize(case)
        self.assertEqual(0, data['branch_alignment']['after']['input_access_errors'])
        self.assert_logical_unchanged(case[0], changed)
        for number in range(4):
            self.assertEqual('west', changed.pin_dir(case[3]['left_'+str(number)], 'B'))
            self.assertEqual('east', changed.pin_dir(case[3]['right_'+str(number)], 'B'))

    def test_explicit_formal_table_does_not_promote_nonformal_internal_controls(self):
        case = fixture(orientations={'input_a': 1, 'input_b': 0})
        case[4]['external_control_ports'] = ['unrelated_control']
        before = align.measure_branch_layout(PAR, *case)
        changed, data = self.regularize(case)
        self.assertEqual(0, before['input_pairs'])
        self.assertEqual(case[0].orient, changed.orient)
        self.assertEqual(0, data['branch_alignment']['after']['input_access_errors'])

    def test_shared_bias_control_is_not_a_differential_input_even_if_formal(self):
        devices = {'a': mos('oa', 'common_control', 'tail'), 'b': mos('ob', 'common_control', 'tail'),
                   'supply': dict(cls='ISRC', type='I', p='tail', n='0')}
        records = [{'kind': 'differential_pair', 'roles': {'left': 'a', 'right': 'b',
                    'tail': ['supply'], 'exclusive_tails': ['supply']}}]
        case = fixture(devices, {'a': (1, 2), 'b': (1, 3), 'supply': (2, 3)},
                       {'a': 1}, records)
        case[4]['external_control_ports'] = ['common_control']
        changed, data = self.regularize(case)
        self.assertEqual(case[0].orient, changed.orient)
        self.assertEqual(0, data['branch_alignment']['after']['input_pairs'])

    def test_declared_control_with_feedback_noncontrol_terminal_is_not_reoriented(self):
        devices = {'a': mos('oa', 'ina', 'tail'), 'b': mos('ob', 'inb', 'tail'),
                   'supply': dict(cls='ISRC', type='I', p='tail', n='0'),
                   'feedback': dict(cls='PASSIVE', type='R', a='ina', b='ob')}
        records = [{'kind': 'differential_pair', 'roles': {'left': 'a', 'right': 'b',
                    'tail': ['supply'], 'exclusive_tails': ['supply']}}]
        case = fixture(devices, {'a': (1, 2), 'b': (1, 3), 'supply': (2, 3), 'feedback': (0, 4)},
                       {'a': 1, 'b': 0}, records)
        case[4]['external_control_ports'] = ['ina', 'inb']
        changed, data = self.regularize(case)
        self.assertEqual(case[0].orient, changed.orient)
        self.assertEqual(0, data['branch_alignment']['after']['input_pairs'])

    def test_current_tail_centers_the_connected_negative_pin_not_positive(self):
        devices = {'a': mos('oa', 'ina', 'tail', 'PMOS'), 'b': mos('ob', 'inb', 'tail', 'PMOS'),
                   'supply': dict(cls='ISRC', type='I', p='vdd', n='tail')}
        records = [{'kind': 'differential_pair', 'roles': {'left': 'a', 'right': 'b',
                    'tail': ['supply'], 'exclusive_tails': ['supply']}}]
        case = fixture(devices, {'a': (1, 2), 'b': (1, 3), 'supply': (0, 3)},
                       {'b': 1, 'supply': 1}, records)
        changed, data = self.regularize(case)
        index = case[3]
        center = sum(changed.pin_world(index[n], 'S')[0] for n in ('a', 'b'))/2
        self.assertLessEqual(abs(changed.pin_world(index['supply'], '-')[0]-center), 2)
        self.assertGreater(abs(changed.pin_world(index['supply'], '+')[0]-center), 20)
        self.assertTrue(any(row.get('pin') == '-' for row in data['branch_alignment']['actions']))

    def test_bjt_channels_use_geometry_not_the_mos_ten_pixel_offset(self):
        devices = {'a': dict(cls='BJT', type='NPN', c='oa', b='ina', e='tail'),
                   'b': dict(cls='BJT', type='NPN', c='ob', b='inb', e='tail'),
                   'supply': dict(cls='ISRC', type='I', p='tail', n='0')}
        records = [{'kind': 'bjt_differential_pair', 'roles': {'left': 'a', 'right': 'b',
                    'tail': ['supply'], 'exclusive_tails': ['supply']}}]
        case = fixture(devices, {'a': (1, 2), 'b': (1, 3), 'supply': (2, 2)}, {'b': 1}, records)
        changed, data = self.regularize(case)
        self.assertEqual(50, changed.offset_x[case[3]['supply']])
        self.assertEqual(0, data['branch_alignment']['after']['tail_error'])

    def test_shared_supply_source_is_not_promoted_to_a_differential_pair(self):
        devices = {'a': mos('oa', 'ina', '0'), 'b': mos('ob', 'inb', '0'),
                   'supply': dict(cls='ISRC', type='I', p='0', n='vdd')}
        records = [{'kind': 'differential_pair', 'roles': {'left': 'a', 'right': 'b', 'tail': ['supply']}}]
        case = fixture(devices, {'a': (1, 2), 'b': (1, 3), 'supply': (2, 2)}, {'a': 1}, records)
        changed, data = self.regularize(case)
        self.assertEqual(case[0].orient, changed.orient)
        self.assertEqual(0, data['branch_alignment']['after']['tail_relations'])
        self.assertEqual(0, data['branch_alignment']['after']['input_pairs'])

    def test_shared_tail_is_not_dragged_to_one_of_two_local_axes(self):
        devices = {'a': mos('oa', 'ia', 't1'), 'b': mos('ob', 'ib', 't1'),
                   'c': mos('oc', 'ic', 't2'), 'd': mos('od', 'id', 't2'),
                   'shared': dict(cls='ISRC', type='I', p='t1', n='t2')}
        records = [{'kind': 'differential_pair', 'roles': {'left': a, 'right': b,
                    'tail': ['shared'], 'exclusive_tails': ['shared']}}
                   for a, b in (('a', 'b'), ('c', 'd'))]
        case = fixture(devices, {'a': (1, 1), 'b': (1, 2), 'c': (1, 4), 'd': (1, 5), 'shared': (2, 3)},
                       {'b': 1, 'd': 1}, records)
        changed, data = self.regularize(case)
        self.assertEqual(0, changed.offset_x[case[3]['shared']])
        self.assertEqual(0, data['branch_alignment']['after']['tail_relations'])
        self.assertTrue(any(row['reason'] == 'shared_tail' for row in data['branch_alignment']['relations']['skipped']))

    def test_tail_in_another_core_pair_keeps_its_existing_position(self):
        case = fixture()
        case[4]['layout_relations'].append({'kind': 'current_mirror', 'roles': {
            'reference': 'tail_device', 'references': ['tail_device'], 'outputs': ['input_a']}})
        changed, data = self.regularize(case)
        self.assertEqual(0, changed.offset_x[case[3]['tail_device']])
        self.assertEqual(0, data['branch_alignment']['after']['tail_relations'])
        self.assertTrue(any(row['reason'] == 'other_core_member'
                            for row in data['branch_alignment']['relations']['skipped']))

    def test_fixed_device_and_clock_access_orientation_are_preserved(self):
        case = fixture(orientations={'input_b': 0})
        pl, comps, nets, index, metadata = case
        pl.fixed = {index['tail_device']}
        pl.fixed_cells = {index['tail_device']: pl.cell_of[index['tail_device']]}
        metadata['role_adjustments'] = [{'device': 'input_b', 'kind': 'latch_clock_access'}]
        changed, _ = self.regularize(case)
        self.assertEqual(pl.orient[index['input_b']], changed.orient[index['input_b']])
        self.assertEqual(pl.pos_of(index['tail_device']), changed.pos_of(index['tail_device']))
        self.assert_logical_unchanged(pl, changed)

    def test_foreign_body_blocks_tail_transaction_and_preserves_original_state(self):
        devices = {'a': mos('oa', 'ina', 'tail'), 'b': mos('ob', 'inb', 'tail'),
                   'supply': dict(cls='ISRC', type='I', p='tail', n='0'),
                   'guard': mos('guard_out', 'guard_in', '0')}
        records = [{'kind': 'differential_pair', 'roles': {'left': 'a', 'right': 'b',
                    'tail': ['supply'], 'exclusive_tails': ['supply']}}]
        case = fixture(devices, {'a': (1, 2), 'b': (1, 3), 'supply': (2, 2), 'guard': (2, 3)},
                       {'b': 1}, records)
        changed, data = self.regularize(case)
        self.assertEqual(case[0].offset_x, changed.offset_x)
        self.assertGreater(data['branch_alignment']['static_rejections'], 0)
        self.assert_logical_unchanged(case[0], changed)

    def test_offsets_stay_within_original_neighbor_midlines_and_custom_grid(self):
        devices = {'a': mos('oa', 'ina', 'tail'), 'b': mos('ob', 'inb', 'tail'),
                   'supply': dict(cls='ISRC', type='I', p='tail', n='0')}
        records = [{'kind': 'differential_pair', 'roles': {'left': 'a', 'right': 'b',
                    'tail': ['supply'], 'exclusive_tails': ['supply']}}]
        case = fixture(devices, {'a': (1, 0), 'b': (1, 4), 'supply': (2, 0)}, {'b': 1}, records)
        changed, data = self.regularize(case, options=RoutingOptions(grid_step=4))
        for offset in changed.offset_x:
            self.assertLessEqual(abs(offset), 50)
            self.assertAlmostEqual(offset/4, round(offset/4))
        self.assertGreater(data['branch_alignment']['after']['tail_error'], 4)

    def test_foreign_power_marker_blocks_a_move_without_foreign_body_overlap(self):
        from routing_rules import build_scene

        devices = {'a': mos('oa', 'ina', 'tail'), 'b': mos('ob', 'inb', 'tail'),
                   'supply': dict(cls='ISRC', type='I', p='tail', n='0'),
                   'guard': dict(cls='VSRC', type='V', p='vdd', n='0')}
        records = [{'kind': 'differential_pair', 'roles': {'left': 'a', 'right': 'b',
                    'tail': ['supply'], 'exclusive_tails': ['supply']}}]
        case = fixture(devices, {'a': (1, 2), 'b': (1, 3), 'supply': (2, 2), 'guard': (3, 3)},
                       {'b': 1}, records)
        case[0].row_y[3] = case[0].row_y[2]+80
        trial = deepcopy(case[0])
        trial.offset_x[case[3]['supply']] = 50
        scene = build_scene(PAR, case[2], trial, case[3], RoutingOptions())
        bodies = dict(scene.body_rects)
        a, b = bodies[case[3]['supply']], bodies[case[3]['guard']]
        self.assertGreater(b[1], a[3])  # Device bodies do not explain rejection.
        changed, data = self.regularize(case)
        self.assertEqual(0, changed.offset_x[case[3]['supply']])
        self.assertGreater(data['branch_alignment']['static_rejections'], 0)

    def test_passive_attachment_moves_locally_without_forming_a_rigid_block(self):
        devices = {'one': dict(cls='PASSIVE', type='R', a='start', b='join'),
                   'two': dict(cls='PASSIVE', type='R', a='join', b='end')}
        records = [{'kind': 'series_chain', 'roles': {'ordered': ['one', 'two']}}]
        case = fixture(devices, {'one': (1, 2), 'two': (1, 3)}, {}, records)
        changed, data = self.regularize(case)
        report = data['branch_alignment']
        self.assertLess(report['after']['attachment_score'], report['before']['attachment_score'])
        self.assertGreater(sum(value != 0 for value in changed.offset_x), 0)
        self.assert_logical_unchanged(case[0], changed)

    def test_feedback_members_keep_multiple_soft_relations_without_copying_devices(self):
        devices = {'amp': dict(cls='VCVS', type='E', op='out', on='0', ip='plus', **{'in': 'minus'}),
                   'feedback': dict(cls='PASSIVE', type='R', a='out', b='minus')}
        records = [{'kind': 'feedback_network', 'roles': {'amplifier': 'amp',
                    'series': ['feedback'], 'shunts': [], 'input': 'minus'}}]
        case = fixture(devices, {'amp': (1, 4), 'feedback': (0, 5)}, {}, records)
        changed, data = self.regularize(case)
        self.assertEqual(2, len(changed.comps))
        self.assertEqual(2, data['branch_alignment']['before']['attachment_relations'])
        self.assertLess(data['branch_alignment']['after']['attachment_score'],
                        data['branch_alignment']['before']['attachment_score'])

    def test_parallel_horizontal_attachments_align_without_changing_rows(self):
        devices = {name: dict(cls='PASSIVE', type='R', a='left', b='right') for name in ('a', 'b')}
        records = [{'kind': 'parallel_passives', 'roles': {'members': ['a', 'b']}}]
        case = fixture(devices, {'a': (1, 2), 'b': (2, 3)}, {'a': 1, 'b': 1}, records)
        changed, data = self.regularize(case)
        self.assertEqual(changed.pos_of(0)[0], changed.pos_of(1)[0])
        self.assertEqual(0, data['branch_alignment']['after']['attachment_score'])
        self.assert_logical_unchanged(case[0], changed)

    def test_vertical_parallel_stack_is_not_falsely_scored_as_perfect(self):
        devices = {name: dict(cls='PASSIVE', type='R', a='left', b='right') for name in ('a', 'b')}
        records = [{'kind': 'parallel_passives', 'roles': {'members': ['a', 'b']}}]
        case = fixture(devices, {'a': (1, 2), 'b': (2, 2)}, {}, records)
        changed, data = self.regularize(case)
        self.assertGreater(data['branch_alignment']['after']['attachment_score'], 0)
        self.assertEqual(case[0].offset_x, changed.offset_x)

    def test_translation_and_instance_renaming_preserve_actual_alignment(self):
        original = fixture()
        first, first_data = self.regularize(original)
        pl, comps, nets, index, metadata = deepcopy(original)
        names = {name: 'changed_' + str(len(index)-i) for name, i in index.items()}
        inverse = {new: old for old, new in names.items()}
        for comp in comps:
            comp['id'] = names[comp['id']]
            comp['nodes'] = {pin: ('signal_'+net if net not in ('0', 'vdd') else net)
                             for pin, net in comp['nodes'].items()}
        pl.comps = comps
        pl.column_x = [x+317 for x in pl.column_x]
        pl.row_y = [y-81 for y in pl.row_y]
        renamed_nets = {('signal_'+net if net not in ('0', 'vdd') else net):
                        [(names[name], pin) for name, pin in members] for net, members in nets.items()}
        renamed_index = {names[name]: i for name, i in index.items()}
        for record in metadata['layout_relations']:
            record['members'] = [names[name] for name in record.get('members', ())]
            roles = record['roles']
            for key in ('left', 'right'):
                roles[key] = names[roles[key]]
            for key in ('tail', 'exclusive_tails'):
                roles[key] = [names[name] for name in roles[key]]
        second, data = align.regularize_branch_layout(PAR, pl, comps, renamed_nets, renamed_index, metadata)
        self.assertEqual(first.offset_x, second.offset_x)
        self.assertEqual(first.orient, second.orient)
        self.assertEqual(first_data['branch_alignment']['after'], data['branch_alignment']['after'])
        for i in range(len(comps)):
            self.assertEqual((first.pos_of(i)[0]+317, first.pos_of(i)[1]-81), second.pos_of(i))

    def test_second_pass_is_stable_and_does_not_reuse_stale_macro_actions(self):
        case = fixture()
        changed, metadata = self.regularize(case)
        second, report = align.regularize_branch_layout(PAR, changed, *case[1:4], metadata)
        self.assertEqual(changed.offset_x, second.offset_x)
        self.assertEqual(changed.orient, second.orient)
        self.assertEqual(0, report['branch_alignment']['accepted'])

    def test_no_sa_or_astar_runs_during_alignment(self):
        with patch('sa_routing.final_route', side_effect=AssertionError('unexpected routing')), \
                patch('orthogonal_router._find_path', side_effect=AssertionError('unexpected A*')), \
                patch('structured_placement._anneal', side_effect=AssertionError('unexpected SA')):
            changed, data = self.regularize(fixture())
        self.assertGreater(data['branch_alignment']['accepted'], 0)

    def test_budget_and_timeout_keep_last_verified_incumbent(self):
        for options, expected in (({'seconds': 0}, 'time_budget'),
                                  ({'max_candidates': 0}, 'candidate_limit')):
            with self.subTest(options=options):
                case = fixture()
                changed, data = self.regularize(case, **options)
                self.assertEqual(case[0].offset_x, changed.offset_x)
                self.assertEqual(expected, data['branch_alignment']['stop_reason'])
        case = fixture()
        clock = SimpleNamespace(now=100.)
        def expire(*args):
            clock.now = 102.
            raise RoutingFailure('deadline reached', {'reason': 'quality_time_budget'})
        with patch('branch_alignment.perf_counter', side_effect=lambda: clock.now), \
                patch('branch_alignment._static_safe', side_effect=expire):
            changed, data = self.regularize(case)
        self.assertEqual(case[0].offset_x, changed.offset_x)
        self.assertEqual('time_budget', data['branch_alignment']['stop_reason'])

    def test_estimate_guard_uses_start_snapshot_not_compounded_tolerance(self):
        case = fixture()
        def proposals(par, pl, comps, index, model, protected, step):
            offsets = list(pl.offset_x)
            offsets[0] += 2
            yield offsets, list(pl.orient), {'kind': 'probe'}
        def score(pl, comps, index, model):
            return {'hard_error': 10-pl.offset_x[0], 'input_access_errors': 0, 'attachment_score': 0}
        def estimate(par, pl, nets, index):
            return 100+pl.offset_x[0]*5
        with patch('branch_alignment._proposals', side_effect=proposals), \
                patch('branch_alignment._measure', side_effect=score), \
                patch('branch_alignment._hpwl', side_effect=estimate), \
                patch('branch_alignment._static_safe', return_value=True):
            changed, data = self.regularize(case)
        self.assertEqual(2, changed.offset_x[0])
        self.assertEqual(100, data['branch_alignment']['estimated_hpwl_before'])
        self.assertAlmostEqual(115, data['branch_alignment']['estimated_hpwl_limit'])
        self.assertGreater(data['branch_alignment']['hpwl_rejections'], 0)

    def test_invalid_parameters_and_offsets_fail_without_mutation(self):
        for kwargs in ({'seconds': -1}, {'seconds': float('inf')}, {'max_candidates': -1},
                       {'max_candidates': True}, {'options': object()}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.regularize(fixture(), **kwargs)
        case = fixture()
        case[0].offset_x = [float('nan')]*len(case[1])
        with self.assertRaisesRegex(ValueError, 'offset_x'):
            self.regularize(case)


if __name__ == '__main__':
    unittest.main()
