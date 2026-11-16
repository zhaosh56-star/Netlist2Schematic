"""Host-side preferences complement guest pin direction, without hard bans."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from netlist2schematic.plugins.attachment import attachment_integration as integration
from netlist2schematic.plugins.attachment.attachment_access import (
    build_access_links, measure_access, access_terms)
from netlist2schematic.plugins.attachment.attachment_selection import organization_rank
from tests.test_attachment_ports import installed
from tests.test_passive_placement import mos, resistor


def fixture():
    devices = dict(q=mos('out', 'gate'), r=resistor('out', 'vdd'),
                   c=resistor('vdd', 'out', 'C'), l=resistor('out', 'vdd', 'L'))
    cells = dict(q=(2, 4), r=(4, 3), c=(4, 4), l=(4, 5))
    pl, comps, nets, index, plan = installed(devices, cells)
    pl.orient[index['c']] = 2
    return devices, pl, comps, nets, index, plan


class HostAccessTests(unittest.TestCase):
    def test_wrong_side_is_measured_even_when_guest_pins_face_host(self):
        _, pl, comps, _, index, plan = fixture()
        group, = plan['groups']
        self.assertEqual(integration._port_errors(pl, comps, index, plan, group), 0)
        metric = measure_access(pl, comps, index, plan['host_access'])
        self.assertTrue(metric['measurable'])
        self.assertEqual((metric['count'], metric['errors']), (3, 3))
        self.assertGreater(metric['backward_distance'], 0)
        self.assertTrue(pl.valid_state())  # Side preference is not a geometric ban.

    def test_real_terminal_and_mirror_are_translation_invariant(self):
        _, pl, comps, _, index, plan = fixture()
        expected = measure_access(pl, comps, index, plan['host_access'])
        for mirror in (False, True):
            class View:
                def pin_world(self, i, pin):
                    x, y = pl.pin_world(i, pin)
                    return (37-x if mirror else x+37), y-51
                def pin_dir(self, i, pin):
                    direction = pl.pin_dir(i, pin)
                    return {'west':'east', 'east':'west'}.get(direction, direction) if mirror else direction
            measured = measure_access(View(), comps, index, plan['host_access'])
            self.assertEqual(measured, expected)

    def test_only_chain_endpoint_is_linked_not_all_chain_members(self):
        devices = dict(q=mos('out', 'gate'), c=resistor('input', 'mid', 'C'),
                       r=resistor('mid', 'gate'))
        pl, comps, _, index, plan = installed(devices, dict(q=(2, 4), c=(0, 0), r=(0, 1)))
        links = plan['host_access']['links']
        self.assertEqual(len(links), 1)
        self.assertEqual((links[0]['member'], links[0]['net'], links[0]['hosts']),
                         ('r', 'gate', [['q', 'G']]))

    def test_equivalent_shared_source_is_supported_but_mixed_roles_are_skipped(self):
        devices = dict(a=mos('outa', 'ga', 'tail'), b=mos('outb', 'gb', 'tail'),
                       r=resistor('tail', '0'))
        _, _, _, _, plan = installed(devices, dict(a=(1, 1), b=(1, 5), r=(2, 3)))
        self.assertEqual(plan['host_access']['links'][0]['hosts'], [['a', 'S'], ['b', 'S']])
        devices['b'] = mos('tail', 'gb')
        _, _, _, _, plan = installed(devices, dict(a=(1, 1), b=(2, 5), r=(2, 3)))
        self.assertEqual(plan['host_access']['links'], [])
        self.assertEqual(plan['host_access']['skipped'][0]['reason'], 'ambiguous_host_access_roles')

    def test_shared_controls_are_not_arbitrarily_assigned_one_access_side(self):
        devices = dict(a=mos('outa', 'bias'), b=mos('outb', 'bias'), r=resistor('bias', '0'))
        _, _, _, _, plan = installed(devices, dict(a=(1, 1), b=(1, 5), r=(0, 3)))
        self.assertEqual(plan['host_access']['links'], [])
        self.assertTrue(plan['host_access']['skipped'])

    def test_drain_anchor_takes_precedence_over_cross_coupled_control(self):
        devices = dict(a=mos('left', 'right'), b=mos('right', 'left'), r=resistor('left', 'vdd'))
        _, _, _, _, plan = installed(devices, dict(a=(2, 1), b=(2, 5), r=(1, 1)))
        self.assertEqual(plan['host_access']['links'][0]['hosts'], [['a', 'D']])

    def test_changed_connections_are_unmeasurable_not_zero_error(self):
        _, pl, comps, _, index, plan = fixture()
        stale = deepcopy(comps)
        stale[index['q']]['nodes']['D'] = 'different'
        metric = measure_access(pl, stale, index, plan['host_access'])
        self.assertFalse(metric['measurable'])
        self.assertIsNone(metric['errors'])
        self.assertIsNone(access_terms({'host_access': metric}))
        self.assertTrue(metric['missing'])

    def test_duplicate_relations_and_input_order_do_not_inflate_score(self):
        _, pl, comps, _, index, plan = fixture()
        duplicate = deepcopy(plan)
        duplicate['groups'].append(deepcopy(plan['groups'][0]))
        evidence = build_access_links(list(reversed(comps)), duplicate)
        self.assertEqual(evidence, plan['host_access'])
        self.assertEqual(measure_access(pl, comps, index, evidence)['count'], 3)

    def test_local_initializer_puts_load_on_real_drain_side_without_astar(self):
        devices, pl, comps, nets, index, plan = fixture()
        old = deepcopy((pl.cell_of, pl.orient, pl.column_x, pl.row_y, pl.rng.getstate()))
        before = integration.measure_attachments(pl, comps, index, plan)
        with patch('netlist2schematic.routing.orthogonal_router.route_scene',
                   side_effect=AssertionError('attachment trials must not use A*')):
            result, report = integration.initialize_attachments(pl, devices, comps, nets, index, plan)
        self.assertTrue(report['applied'])
        after = integration.measure_attachments(result, comps, index, plan)
        self.assertEqual(after['host_access']['errors'], 0)
        self.assertLess(integration.attachment_rank(after), integration.attachment_rank(before))
        self.assertLess(organization_rank({'metrics':after}), organization_rank({'metrics':before}))
        self.assertTrue(result.valid_state())
        self.assertEqual((pl.cell_of, pl.orient, pl.column_x, pl.row_y, pl.rng.getstate()), old)


if __name__ == '__main__':
    unittest.main()
