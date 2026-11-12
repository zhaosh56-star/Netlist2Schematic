"""Attachment directions follow real nets, placement and mirror geometry."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import attachment_integration as attachment
from circuit_structures import analyze_structures
from device_leveling import compute_levels
from tests.test_passive_placement import convert, mos, resistor, source


def installed(devices, cells, orientations=None):
    pl, comps, nets, index, _ = convert(devices, cells, orientations)
    plan = attachment.build_attachment_plan(devices, comps, nets, index,
        analyze_structures(devices), compute_levels(devices), pl.row_of)
    return attachment._install(pl, comps, index, plan), comps, nets, index, plan


def chain_devices():
    return dict(a=mos('left', 'ga'), b=mos('right', 'gb'),
        r=resistor('left', 'mid0'), c=resistor('mid1', 'mid0', 'C'),
        l=resistor('mid1', 'right', 'L'))


def pin_for(comp, net):
    pins = [pin for pin, value in comp['nodes'].items() if value == net]
    if len(pins) != 1:
        raise AssertionError('The test path must use one real visible terminal')
    return pins[0]


class AttachmentPortTests(unittest.TestCase):
    def assert_drawn_path_direction(self, pl, comps, index, path, axis=0):
        members, nodes = path['members'], path['path']
        if len(members) > 1:
            delta = pl.pos_of(index[members[-1]])[axis]-pl.pos_of(index[members[0]])[axis]
            direction = 1 if delta > 0 else -1
        else:
            direction = 1
        for step, name in enumerate(members):
            i = index[name]
            before, after = pin_for(comps[i], nodes[step]), pin_for(comps[i], nodes[step+1])
            a, b = pl.pin_world(i, before), pl.pin_world(i, after)
            self.assertGreater(direction*(b[axis]-a[axis]), 0, name)
            self.assertAlmostEqual(a[1-axis], b[1-axis])
            if step+1 < len(members):
                other = index[members[step+1]]
                shared = pin_for(comps[other], nodes[step+1])
                self.assertNotEqual(pl.pin_dir(i, after), pl.pin_dir(other, shared))

    def test_series_reversed_terminal_definitions_face_same_net_inside(self):
        devices = chain_devices()
        pl, comps, nets, index, plan = installed(devices,
            dict(a=(1, 0), b=(1, 6), r=(1, 2), c=(1, 3), l=(1, 4)))
        group = next(g for g in plan['groups'] if g['kind'] == 'series_chain')
        path, = attachment._group_paths(group)
        frozen = deepcopy((comps, nets, pl.cell_of, pl.column_x, pl.row_y, pl.rng.getstate()))
        attachment._orient_path(pl, comps, index, plan, path)
        self.assert_drawn_path_direction(pl, comps, index, path)
        self.assertEqual(attachment._port_errors(pl, comps, index, plan, group), 0)
        self.assertEqual((comps, nets, pl.cell_of, pl.column_x, pl.row_y, pl.rng.getstate()), frozen)
        self.assertNotEqual(pl.orient[index['r']], pl.orient[index['c']])

    def test_component_pin_dictionary_order_and_path_reversal_are_invariant(self):
        devices = chain_devices()
        pl, comps, _, index, plan = installed(devices,
            dict(a=(1, 0), b=(1, 6), r=(1, 2), c=(1, 3), l=(1, 4)))
        group = next(g for g in plan['groups'] if g['kind'] == 'series_chain')
        path, = attachment._group_paths(group)
        attachment._orient_path(pl, comps, index, plan, path)
        expected = list(pl.orient)
        changed = deepcopy(pl)
        for comp in changed.comps:
            comp['nodes'] = dict(reversed(list(comp['nodes'].items())))
        reversed_path = dict(members=list(reversed(path['members'])), path=list(reversed(path['path'])))
        attachment._orient_path(changed, changed.comps, index, plan, reversed_path)
        self.assertEqual(changed.orient, expected)
        self.assertEqual(attachment._port_errors(changed, changed.comps, index, plan, group), 0)

    def test_two_arm_common_node_faces_inward_with_opposite_pin_definitions(self):
        devices = dict(a=mos('left', 'ga'), b=mos('right', 'gb'), control=mos('out', 'sense'),
                       r=resistor('left', 'sense'), s=resistor('right', 'sense'))
        pl, comps, _, index, plan = installed(devices,
            dict(a=(1, 0), b=(1, 7), control=(0, 3), r=(1, 2), s=(1, 5)))
        group = next(g for g in plan['groups'] if g['kind'] == 'two_arm_join')
        for path in attachment._group_paths(group):
            attachment._orient_path(pl, comps, index, plan, path)
            self.assert_drawn_path_direction(pl, comps, index, path)
        self.assertEqual(pl.pin_dir(index['r'], pin_for(comps[index['r']], 'sense')), 'east')
        self.assertEqual(pl.pin_dir(index['s'], pin_for(comps[index['s']], 'sense')), 'west')
        self.assertEqual(attachment._port_errors(pl, comps, index, plan, group), 0)

    def test_multi_path_bridge_orients_each_path_from_its_own_true_pins(self):
        devices = chain_devices(); devices['bypass'] = resistor('right', 'left', 'C')
        pl, comps, _, index, plan = installed(devices,
            dict(a=(2, 0), b=(2, 6), r=(2, 2), c=(2, 3), l=(2, 4), bypass=(1, 3)))
        group = next(g for g in plan['groups'] if g['kind'] == 'multi_path_bridge')
        original_nodes = deepcopy([comp['nodes'] for comp in comps])
        self.assertEqual(len(attachment._group_paths(group)), 2)
        for path in attachment._group_paths(group):
            attachment._orient_path(pl, comps, index, plan, path)
            self.assert_drawn_path_direction(pl, comps, index, path)
        self.assertEqual(attachment._port_errors(pl, comps, index, plan, group), 0)
        self.assertEqual([comp['nodes'] for comp in comps], original_nodes)

    def test_drawn_horizontal_chain_does_not_rotate_vertical_for_offset_host_nodes(self):
        devices = chain_devices()
        pl, comps, _, index, plan = installed(devices,
            dict(a=(0, 1), b=(7, 5), r=(3, 2), c=(3, 3), l=(3, 4)))
        group = next(g for g in plan['groups'] if g['kind'] == 'series_chain')
        path, = attachment._group_paths(group)
        attachment._orient_path(pl, comps, index, plan, path)
        self.assert_drawn_path_direction(pl, comps, index, path)

    def test_vertical_chain_has_real_internal_pins_facing_each_other(self):
        devices = chain_devices()
        pl, comps, _, index, plan = installed(devices,
            dict(a=(0, 2), b=(6, 2), r=(2, 2), c=(3, 2), l=(4, 2)))
        group = next(g for g in plan['groups'] if g['kind'] == 'series_chain')
        path, = attachment._group_paths(group)
        attachment._orient_path(pl, comps, index, plan, path)
        self.assert_drawn_path_direction(pl, comps, index, path, axis=1)
        self.assertEqual(attachment._port_errors(pl, comps, index, plan, group), 0)

    def assert_joint_mirror(self, pl, comps, index, plan, group):
        before = deepcopy((pl.cell_of, pl.orient, [c['nodes'] for c in comps]))
        ids = {index[n] for n in group['members']}
        for block in group['host_blocks']:
            ids.update(index[n] for n in block)
        for lane in group.get('host_lanes', ()):
            ids.update(index[n] for n in lane)
        lo, hi = min(pl.cell_of[i][1] for i in ids), max(pl.cell_of[i][1] for i in ids)
        mirror_sum = pl.column_x[lo]+pl.column_x[hi]
        points = {(i, pin): pl.pin_world(i, pin) for i in ids if i in pl.attachment_orientation_ids
                  for pin in comps[i]['nodes']}
        rng = Mock(); rng.random.side_effect = [0.0, 0.9]; rng.choice.return_value = group
        pl.rng = rng
        undo = pl.propose()
        self.assertIsNotNone(undo)
        self.assertEqual(undo, (before[0], before[1]))
        self.assertTrue(pl.valid_state())
        self.assertEqual([c['nodes'] for c in comps], before[2])
        for (i, pin), (x, y) in points.items():
            after = pl.pin_world(i, pin)
            self.assertAlmostEqual(after[0], mirror_sum-x, msg=(comps[i]['id'], pin))
            self.assertAlmostEqual(after[1], y, msg=(comps[i]['id'], pin))

    def test_diode_horizontal_mirror_preserves_actual_a_k_endpoints(self):
        devices = dict(a=mos('left', 'ga'), b=mos('right', 'gb'), d=resistor('left', 'right', 'D'))
        pl, comps, _, index, plan = installed(devices, dict(a=(1, 1), b=(1, 6), d=(1, 3)))
        group = next(g for g in plan['groups'] if g['members'] == ['d'])
        pl.attachment_required_embeddings = {group['id']: ('d',)}
        pl.attachment_port_limits = {group['id']: 0}
        self.assertEqual(attachment._port_errors(pl, comps, index, plan, group), 0)
        self.assertTrue(pl.valid_state())
        self.assert_joint_mirror(pl, comps, index, plan, group)
        self.assertEqual(pl.orient[index['d']], 2)

    def test_source_mirror_preserves_each_real_pole_for_both_source_polarities(self):
        for reverse in (False, True):
            for orientation in range(4):
                devices = dict(a=mos('oa', 'control'), b=mos('ob', 'control'),
                    v=source('VSRC', '0' if reverse else 'control', 'control' if reverse else '0'))
                pl, comps, _, index, plan = installed(devices,
                    dict(a=(1, 1), b=(1, 6), v=(2, 2)), dict(v=orientation))
                group = dict(id='source:v', kind='source_control_chain', members=['v'],
                    owner_junctions=['control'], host_ports=[['a', 'G'], ['b', 'G']],
                    host_members=['a', 'b'], host_blocks=[['a'], ['b']], host_lanes=[],
                    path=['0', 'control'], requires_interior=False)
                plan = deepcopy(plan); plan['groups'] = [group]; plan['supported'] = True
                pl = attachment._install(pl, comps, index, plan)
                self.assert_joint_mirror(pl, comps, index, plan, group)


if __name__ == '__main__':
    unittest.main()
