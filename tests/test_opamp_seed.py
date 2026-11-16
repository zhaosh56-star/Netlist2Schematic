"""Short initialization checks for three-signal-pin amplifier seeds."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.netlist.netlist_core import parse_netlist, to_document


ROOT = Path(__file__).resolve().parents[1]


def initialize(devices, policy='flexible'):
    doc = to_document(devices)
    comps, nets = doc['components'], doc['nets']
    index = {component['id']: i for i, component in enumerate(comps)}
    levels = structured.dl.compute_levels(devices)
    conduction = {net for device in devices.values() if structured._active(device)
                  for net in (device.get('d'), device.get('s'),
                              device.get('c'), device.get('e')) if net is not None}
    attachments = structured.legacy.attach_info(devices, levels['net_levels'], conduction)
    placement, _, _ = structured._initial_placement(
        devices, comps, nets, index, levels, attachments,
        structured._structures(devices), 7, 0, passive_layout=policy)
    return placement, index


class OpampSeedTests(unittest.TestCase):
    def test_parallel_feedback_paths_get_distinct_cells_on_input_side(self):
        devices = {
            'amp': {'cls': 'OPAMP', 'type': 'E', 'op': 'o', 'ip': 'p',
                    'in': 'n', 'vdd': 'vdd', 'vss': '0', 'model': 'opamp'},
            'r': {'cls': 'PASSIVE', 'type': 'R', 'a': 'o', 'b': 'n', 'value': '1k'},
            'c': {'cls': 'PASSIVE', 'type': 'C', 'a': 'o', 'b': 'n', 'value': '1p'},
        }
        placement, index = initialize(devices)
        self.assertTrue(placement.valid_state())
        self.assertEqual(len(set(placement.cell_of)), len(devices))
        self.assertEqual(placement.cell_of[index['r']][0],
                         placement.cell_of[index['c']][0])
        self.assertLess(placement.cell_of[index['r']][0],
                        placement.cell_of[index['amp']][0])
        self.assertEqual(placement.orient[index['r']], 1)
        self.assertEqual(placement.orient[index['c']], 1)

    def test_existing_noncolliding_feedback_template_is_unchanged(self):
        devices = parse_netlist(ROOT / 'tests' / 'fixtures' / 'aesthetic-baseline' / '1.cir')
        index = {name: i for i, name in enumerate(devices)}
        rows = [0] * len(devices)
        cells, orientations, width = structured._opamp_seed(
            devices, structured._structures(devices), index, rows, 0)
        expected = {'E1': (2, 1), 'R1': (3, 1), 'C1': (3, 2),
                    'R2': (4, 0), 'C2': (4, 1), 'R3': (1, 2), 'R4': (0, 1)}
        self.assertEqual({name: cells[i] for name, i in index.items()}, expected)
        self.assertEqual({name: orientations.get(i, 0) for name, i in index.items()},
                         {'E1': 0, 'R1': 1, 'C1': 1, 'R2': 0,
                          'C2': 0, 'R3': 1, 'R4': 0})
        self.assertEqual(width, 4)

    def test_collision_repair_does_not_take_another_requested_cell(self):
        devices = {
            'amp': {'cls': 'OPAMP', 'type': 'E', 'op': 'o', 'ip': 'p', 'in': 'n'},
            'a': {'cls': 'PASSIVE', 'type': 'R', 'a': 'o', 'b': 'n', 'value': '1k'},
            'b': {'cls': 'PASSIVE', 'type': 'C', 'a': 'o', 'b': 'n', 'value': '1p'},
            'c': {'cls': 'PASSIVE', 'type': 'L', 'a': 'o', 'b': 'x', 'value': '1u'},
            'd': {'cls': 'PASSIVE', 'type': 'R', 'a': 'x', 'b': 'n', 'value': '2k'},
        }
        index = {name: i for i, name in enumerate(devices)}
        rows = [0] * len(devices)
        cells, _, _ = structured._opamp_seed(
            devices, structured._structures(devices), index, rows, 0)
        # The two-member path's second component owns this requested position.
        self.assertEqual(cells[index['d']], (1, 1))
        self.assertEqual(cells[index['amp']], (2, 1))
        self.assertEqual(len(set(cells.values())), len(devices))

    def test_tia_parallel_feedback_initializes_without_astar(self):
        source = ('\n'.join(('.subckt Main ref out input',
                  'C3 input out 1p', 'R4 input out 1k', 'R6 aux ref 1k',
                  'E1 0 0 aux input out opamp', '.ends Main')) + '\n')
        with TemporaryDirectory() as directory, mock.patch(
                'netlist2schematic.routing.orthogonal_router.route_scene',
                side_effect=AssertionError('A* must not run in initialization')):
            path = Path(directory) / 'feedback.cir'
            path.write_text(source, encoding='utf-8')
            result = next(structured.create_candidates(
                path, cell='Main', iterations=0, seed=7))
            placement, components = result[:2]
            self.assertEqual(len(set(placement.cell_of)), len(components))
            self.assertTrue(placement.blocks_contiguous())
            self.assertTrue(placement.valid_state())


if __name__ == '__main__':
    unittest.main()
