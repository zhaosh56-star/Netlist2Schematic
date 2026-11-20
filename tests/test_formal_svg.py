"""The formal export changes typography, retaining verified devices and wires."""
from contextlib import ExitStack, redirect_stdout
from hashlib import sha256
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from netlist2schematic.flow.run_flow import run
from netlist2schematic.plugins.power_rails import power_rails
from netlist2schematic.rendering import formal_svg as formal


class MathLabelTests(unittest.TestCase):
    def test_instance_identity_survives_typographic_subscripts(self):
        for name in ('M01', 'MO2', 'XM14', 'RZ', 'CL', 'Q7prime', 'Vref', 'I1'):
            with self.subTest(name=name):
                self.assertEqual(name, ''.join(formal.math_runs(name, 'component')))
        self.assertEqual(('M', '01'), formal.math_runs('M01', 'component'))
        self.assertEqual(('XM', '14'), formal.math_runs('XM14', 'component'))

    def test_math_ports_and_literal_clock_names_are_distinguished(self):
        self.assertEqual(('V', 'DD'), formal.math_runs('VDD', 'supply'))
        self.assertEqual(('V', 'IN+'), formal.math_runs('VIN+', 'port'))
        self.assertEqual(('CLK', ''), formal.math_runs('CLK', 'port'))
        self.assertEqual(('OUTP', ''), formal.math_runs('OUTP', 'port'))

    def test_bounds_include_subscripts_and_italic_overhang(self):
        left, top, right, bottom = formal.text_bounds(100, 100, 'M12', 'middle', 'component')
        self.assertLess(left, 100)
        self.assertGreater(right, 100)
        self.assertLess(top, 85)
        self.assertGreater(bottom, 108)

    def test_dense_port_uses_free_diagonal_pocket_without_ignoring_wires(self):
        point = (100, 100)
        candidates = list(formal._pin_candidates(point))
        wires = []
        for x, y, align in candidates:
            a, b, c, d = formal.text_bounds(x, y, 'INP', align, 'port')
            wires.append((((a+c)/2, (b+d)/2-1), ((a+c)/2, (b+d)/2+1)))
        record = dict(text='INP', kind='port', anchor=point, candidates=candidates)
        selected = formal._choose_position(record, [], wires, [])
        self.assertEqual((0, 0, 0), selected[-3:])
        self.assertNotIn(selected[1:4], candidates)

    def test_valid_original_label_does_not_enable_extra_search(self):
        record = dict(text='CLK', kind='port', anchor=(100, 100),
                      candidates=list(formal._pin_candidates((100, 100))))
        with patch.object(formal, '_extended_candidates', side_effect=AssertionError('Keep good labels')):
            selected = formal._choose_position(record, [], [], [])
        self.assertEqual((0, 0, 0), selected[-3:])


class FormalSvgTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='formal-svg-tests-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        source = cls.root/'local.cir'
        source.write_text('.model pm PMOS\nM1 d g1 VDD VDD pm\nM2 d g2 VDD VDD pm\n.end\n', encoding='utf-8')
        with redirect_stdout(io.StringIO()):
            run(source, iterations=0, candidate_id='1', seed=17, quality_seconds=10,
                fallback='off', simplify_routes='off', out_dir=cls.root/'native')
        cls.placement = cls.root/'native/local-placement.json'
        cls.merged = power_rails.generate(cls.placement, cls.root/'power')
        cls.context = power_rails._load_context(cls.placement)
        cls.svg = Path(cls.merged['artifacts']['clean_svg'])

    def setUp(self):
        self.local = tempfile.TemporaryDirectory(prefix='one-', dir=self.root)
        self.addCleanup(self.local.cleanup)
        self.output = Path(self.local.name)/'formal.svg'

    def render(self, source=None):
        return formal.formalize_svg(source or self.svg, self.output, self.context, self.merged['view'])

    def test_shared_cap_and_all_electrical_shapes_survive_without_search(self):
        before = sha256(self.placement.read_bytes()).hexdigest()
        with ExitStack() as stack:
            for name in ('netlist2schematic.routing.orthogonal_router._find_path',
                         'netlist2schematic.routing.sa_routing.final_route',
                         'netlist2schematic.layout.structured_placement._anneal'):
                stack.enter_context(patch(name, side_effect=AssertionError('Export must not search')))
            report = self.render()
        self.assertEqual('merged', self.merged['view']['status'])
        self.assertEqual(formal.geometry_signature(ET.parse(self.svg).getroot()),
                         formal.geometry_signature(ET.parse(self.output).getroot()))
        self.assertEqual(before, sha256(self.placement.read_bytes()).hexdigest())
        self.assertTrue(report['electrical_geometry_unchanged'])
        self.assertTrue(all(n == 0 for n in report['text_validation'].values()))

    def test_gt_font_proportions_and_real_component_names(self):
        self.render()
        root = ET.parse(self.output).getroot()
        components = [e for e in root.iter() if e.get('data-presentation-kind') == 'component']
        self.assertEqual({'M1', 'M2'}, {''.join(e.itertext()) for e in components})
        for element in components:
            self.assertEqual('15.116', element.get('font-size'))
            self.assertEqual('700', element.get('font-weight'))
            self.assertEqual('italic', element.get('font-style'))
            self.assertIn('Metropolis', element.get('font-family'))
            self.assertEqual('11.48816px', list(element)[1].get('font-size'))
        supplies = [e for e in root.iter() if e.get('data-presentation-kind') == 'shared-supply']
        self.assertEqual(['VDD'], [''.join(e.itertext()) for e in supplies])

    def test_output_is_deterministic(self):
        self.render()
        expected = self.output.read_bytes()
        self.render()
        self.assertEqual(expected, self.output.read_bytes())

    def test_original_svg_cannot_be_overwritten(self):
        with self.assertRaisesRegex(ValueError, 'separate'):
            formal.formalize_svg(self.svg, self.svg, self.context, self.merged['view'])

    def test_background_boxes_and_pin_holes_must_be_removed_by_clean_renderer(self):
        for tag, attributes in (('rect', {'stroke-dasharray': '6 4'}),
                                ('circle', {'fill': '#fff', 'r': '3'})):
            with self.subTest(tag=tag):
                root = ET.parse(self.svg).getroot()
                ET.SubElement(root, '{'+formal.NS+'}'+tag, attributes)
                path = Path(self.local.name)/(tag+'.svg')
                ET.ElementTree(root).write(path)
                with self.assertRaisesRegex(ValueError, 'clean view'):
                    self.render(path)
                self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
