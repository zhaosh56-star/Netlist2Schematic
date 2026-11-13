"""Clean presentation edits text/decoration only, preserving every wire."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from netlist2schematic.recovery.fallback_bus import create_fallback
from netlist2schematic.rendering.sa_render import render_svg
from netlist2schematic.routing.sa_routing import validate_final_routes
from netlist2schematic.layout import structured_placement as structured
from tests.test_passive_placement import base_placement, resistor

NS = '{http://www.w3.org/2000/svg}'


def fixture():
    devices = {'R1': resistor('input', 'output'), 'C1': resistor('output', '0', 'C')}
    pl, comps, nets, index = base_placement(devices, {'R1': (1, 1), 'C1': (1, 3)}, {'R1': 1})
    routes = [('input', [((220, 200), (240, 200))]),
              ('output', [((180, 200), (160, 200)), ((160, 200), (160, 160)),
                          ((160, 160), (440, 160)), ((440, 160), (440, 180))])]
    metrics = {'wirelength': 380, 'crossings': 0, 'bends': 3,
               'body_pen': 0, 'overlap': 0, 'proximity': 0}
    return pl, comps, nets, index, routes, metrics


def wire_geometry(root):
    result = []
    for node in root.iter():
        if node.tag == NS+'polyline' and any(key in node.attrib for key in ('data-net', 'data-power-net')):
            result.append((node.tag, node.get('data-net'), node.get('data-power-net'), node.get('points')))
        elif node.tag == NS+'line':
            result.append((node.tag, *(node.get(key) for key in ('x1', 'y1', 'x2', 'y2'))))
    return result


class CleanPresentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.state = fixture()
        self.structures = [{'kind': 'test structure', 'devices': ['R1', 'C1']}]

    def render(self, *, clean=True, name='image.svg', metrics=None):
        pl, comps, nets, index, routes, original_metrics = self.state
        destination = self.directory/name
        render_svg(structured.par, pl, comps, nets, index, routes, metrics or original_metrics,
                   destination, clean=clean, structs=self.structures)
        return destination, ET.parse(destination).getroot()

    def enable(self):
        self.state[0].presentation_style = 'clean'

    def test_legacy_bytes_remain_exact_for_both_styles(self):
        expected = {False: '6eb2e8b0cb5f229f582c8320c17d2366bb60f437090e5115375c99490f38d7c8',
                    True: '0f99037f865f062a872aea37614cef40a974c9b38a1613456ea4bb7fcc855ebb'}
        for clean in (False, True):
            path, _ = self.render(clean=clean)
            self.assertEqual(sha256(path.read_bytes()).hexdigest(), expected[clean])
            self.state[0].presentation_style = 'legacy'
            explicit, _ = self.render(clean=clean, name='explicit.svg')
            self.assertEqual(path.read_bytes(), explicit.read_bytes())

    def test_color_is_identical_debug_even_when_clean_style_is_enabled(self):
        old, _ = self.render(clean=False, name='old.svg')
        self.enable()
        current, _ = self.render(clean=False, name='new.svg')
        self.assertEqual(old.read_bytes(), current.read_bytes())

    def test_clean_hides_boxes_metrics_and_pin_circles(self):
        _, old = self.render()
        self.enable()
        _, root = self.render(name='new.svg')
        texts = [''.join(node.itertext()) for node in root.iter(NS+'text')]
        self.assertFalse(any('WL=' in value or 'cross=' in value or 'test structure' in value for value in texts))
        self.assertFalse(any(node.get('stroke-dasharray') for node in root.iter(NS+'rect')))
        self.assertFalse(any(node.get('fill') == '#fff' and node.get('r') == '3'
                             for node in root.iter(NS+'circle')))
        self.assertTrue(any(node.get('r') == '3' for node in old.iter(NS+'circle')))
        self.assertTrue(all(node.get('font-size') == '14' for node in root.iter(NS+'text')))

    def test_routes_pins_and_placement_are_unchanged(self):
        pl, comps, nets, index, routes, metrics = self.state
        saved = deepcopy((pl.cell_of, pl.orient, comps, nets, routes, metrics))
        _, old = self.render(name='old.svg')
        self.enable()
        _, clean = self.render(name='clean.svg')
        _, color = self.render(clean=False, name='color.svg')
        self.assertEqual(wire_geometry(old), wire_geometry(clean))
        self.assertEqual(wire_geometry(color), wire_geometry(clean))
        self.assertEqual((pl.cell_of, pl.orient, comps, nets, routes, metrics), saved)
        validated, measured, checked = validate_final_routes(structured.par, nets, pl, index, routes)
        self.assertTrue(checked['valid'])
        self.assertEqual(measured, metrics)

    def test_only_explicit_named_ports_are_drawn_at_real_pins(self):
        self.enable()
        pl, comps, nets, index, routes, metrics = self.state
        pl.presentation_ports = {
            'input': {'name': 'VIN', 'source': 'formal', 'direction': 'generic'},
            'not_a_net': {'name': 'FAKE', 'source': 'formal'},
            'output': {'name': 'INFERRED', 'source': 'guessed'}}
        _, root = self.render()
        labels = [node for node in root.iter(NS+'text') if node.get('data-presentation-kind') == 'port']
        self.assertEqual(len(labels), 1)
        label = labels[0]
        self.assertEqual(label.text, 'VIN')
        name, pin = label.get('data-anchor-component'), label.get('data-anchor-pin')
        actual = pl.pin_world(index[name], pin)
        self.assertEqual(tuple(float(label.get('data-anchor-'+axis)) for axis in ('x', 'y')), actual)
        self.assertIn((name, pin), nets['input'])
        self.assertFalse(any(node.tag == NS+'marker' for node in root.iter()))

    def test_all_new_labels_fit_canvas_including_long_formal(self):
        self.enable()
        self.state[0].presentation_ports = {'input': {
            'name': 'VIN_'+'LONG_FORMAL_'*6, 'source': 'formal', 'direction': 'generic'}}
        _, root = self.render()
        x, y, width, height = map(float, root.get('viewBox').split())
        count = 0
        for node in root.iter(NS+'text'):
            if node.get('data-label-box'):
                left, top, right, bottom = map(float, node.get('data-label-box').split())
                self.assertGreaterEqual(left, x)
                self.assertGreaterEqual(top, y)
                self.assertLessEqual(right, x+width)
                self.assertLessEqual(bottom, y+height)
                count += 1
        self.assertGreater(count, 2)

    def test_metrics_cannot_force_clean_width(self):
        self.enable()
        first, root = self.render(name='small.svg')
        metrics = dict(self.state[-1], wirelength=10**100, crossings=10**70, bends=10**60)
        second, other = self.render(name='huge.svg', metrics=metrics)
        self.assertEqual(root.get('viewBox'), other.get('viewBox'))
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_supply_uses_existing_markers_and_actual_name(self):
        _, before = self.render(name='before.svg')
        self.enable()
        self.state[0].presentation_supply_names = {'0': 'VSS'}
        _, after = self.render(name='after.svg')
        labels = [node for node in after.iter(NS+'text') if node.get('data-presentation-kind') == 'supply']
        self.assertEqual([node.text for node in labels], ['VSS'])
        self.assertEqual(wire_geometry(before), wire_geometry(after))

    def test_true_branch_dot_remains(self):
        pl, comps, nets, index, routes, metrics = self.state
        routes[1][1][:] = [((180, 200), (160, 200)), ((160, 200), (160, 160)),
                          ((160, 160), (300, 160)), ((300, 160), (440, 160)),
                          ((440, 160), (440, 180)), ((300, 160), (300, 140))]
        self.enable()
        _, root = self.render()
        dots = [node for node in root.iter(NS+'circle') if node.get('r') == '4']
        self.assertTrue(any(node.get('cx') == '300' and node.get('cy') == '160' for node in dots))

    def test_bus_fallback_keeps_stage_and_custom_power_geometry(self):
        source = self.directory/'bus.cir'
        source.write_text('R1 a b 1k\nC1 b 0 1p\n.end\n', encoding='utf-8')
        pl, comps, nets, index, metadata, (routes, metrics, validation) = create_fallback(source)
        pl.routing_stage = 'bus_fallback'
        path = self.directory/'legacy-bus.svg'
        render_svg(structured.par, pl, comps, nets, index, routes, metrics, path,
                   clean=True, validation=validation)
        before = ET.parse(path).getroot()
        pl.presentation_style = 'clean'
        path = self.directory/'clean-bus.svg'
        render_svg(structured.par, pl, comps, nets, index, routes, metrics, path,
                   clean=True, validation=validation)
        after = ET.parse(path).getroot()
        self.assertEqual(wire_geometry(before), wire_geometry(after))
        stage = [node for node in after.iter(NS+'text') if node.get('data-presentation-stage')]
        self.assertEqual(stage[0].text, 'Bus fallback')
        self.assertFalse(any('WL=' in ''.join(node.itertext()) for node in after.iter(NS+'text')))
        self.assertEqual(len([node for node in before.iter(NS+'g') if node.get('data-power-component')]),
                         len([node for node in after.iter(NS+'g') if node.get('data-power-component')]))


if __name__ == '__main__':
    unittest.main()
