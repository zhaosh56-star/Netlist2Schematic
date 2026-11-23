"""Readability constraints relate names to real owners instead of free canvas."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from netlist2schematic.rendering import formal_svg as f
from netlist2schematic.rendering import nearby_labels as nearby
from netlist2schematic.rendering.sa_render import _overlap, _wire_in_box


class NearbyLabelTests(unittest.TestCase):
    def assign(self, records, owners, boxes, wires=()):
        return nearby.assign(records, owners, boxes, wires, f.text_bounds,
                             f._extended_candidates, font_size=f.FONT_SIZE)

    def test_component_names_stay_close_to_their_own_symbol(self):
        owners = {'M1': (0, 0, 30, 50), 'M2': (85, 0, 115, 50)}
        records = [dict(kind='component', text=name, component=name, owner_box=box,
                        anchor=((box[0]+box[2])/2, 25), candidates=[(250, 200, 'start')])
                   for name, box in owners.items()]
        wires = [((50, -80), (50, 120))]
        with patch.object(f, '_extended_candidates', side_effect=AssertionError('No global component search')):
            choices, report = self.assign(records, owners, list(owners.values()), wires)
        self.assertEqual(0, report['ownership_violations'])
        for record, choice in zip(records, choices):
            bounds = choice[4]
            gap = nearby.box_distance(bounds, record['owner_box'])
            self.assertLessEqual(gap, 2.5*f.FONT_SIZE)
            for name, box in owners.items():
                if name != record['component']:
                    self.assertGreaterEqual(nearby.box_distance(bounds, box)+1e-8, gap)
            self.assertFalse(any(_overlap(bounds, box, 2) for box in owners.values()))
            self.assertFalse(any(_wire_in_box(a, b, bounds, margin=2) for a, b in wires))
        self.assertFalse(_overlap(choices[0][4], choices[1][4], 2))
        self.assertEqual(choices, self.assign(records, owners, list(owners.values()), wires)[0])

    def test_no_nearby_space_fails_instead_of_detaching_the_name(self):
        record = dict(kind='component', text='Q1', component='Q1', owner_box=(0, 0, 30, 40),
                      anchor=(15, 20), candidates=[(500, 500, 'start')])
        with self.assertRaisesRegex(ValueError, 'nearby readable'):
            self.assign([record], {'Q1': record['owner_box']}, [(-200, -200, 200, 200)])

    def test_joint_assignment_preserves_a_constrained_neighbors_only_slot(self):
        records = [dict(kind='component', text='M1', anchor=(0, 0), owner_box=(-10, -10, 40, 10),
                        candidates=[(0, 0, 'start'), (30, 0, 'start')]),
                   dict(kind='component', text='M2', anchor=(0, 0), owner_box=(-10, -10, 40, 10),
                        candidates=[(0, 0, 'start')])]
        bounds = lambda x, y, *_: (x, y, x+10, y+10)
        with patch.object(nearby, 'local_candidates', return_value=[]):
            choices, _ = nearby.assign(records, {}, [], [], bounds, lambda _: (), font_size=15)
        self.assertEqual([30, 0], [c[1] for c in choices])

    def test_supply_text_is_bound_to_the_marker_and_not_its_host_body(self):
        box = (-12, -60, 12, -34)
        root = ET.Element('{'+f.NS+'}svg')
        group = ET.SubElement(root, '{'+f.NS+'}g', {'data-component': 'M1'})
        ET.SubElement(group, '{'+f.NS+'}text', {'data-presentation-kind':'supply',
            'data-power-net-label':'VDD', 'x':'0', 'y':'-75'}).text = 'VDD'
        context = dict(index={'M1':0}, placement=SimpleNamespace(pos_of=lambda _: (0, 0)),
            par=SimpleNamespace(GEO_DOC={'vdd':{'orientations':[{'viewBoxAABB':
                dict(x=-12,y=-20,width=24,height=26)}]}}),
            hints={'supply_text':[dict(net='VDD',x=0,y=-75,identity=('M1','S'))]},
            terminals=[dict(component_id='M1',pin='S',net='VDD',stub=((0,-10),(0,-40)),symbol='vdd')])
        records = f._label_records(root, context, {'M1':(-20,0,20,50)}, [('VDD',box)])
        self.assertEqual(box, records[0]['owner_box'])
        self.assertNotEqual((-20,0,20,50), records[0]['owner_box'])


if __name__ == '__main__':
    unittest.main()
