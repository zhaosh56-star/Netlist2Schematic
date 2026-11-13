"""Saved routing policy and output-name boundaries at the public flow seam."""
from pathlib import Path
import contextlib
import io
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from netlist2schematic.flow import run_flow
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.common.sa_common import load_local
from netlist2schematic.common.sa_saved import saved_routing_options


class SavedPolicyTests(unittest.TestCase):
    def test_nondefault_wire_width_roundtrips_through_independent_redraw(self):
        source = ROOT / 'analog-canvas-main/netlists/native-rc-filters/circuit.spi'
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            directory = Path(directory)
            data = run_flow.run(source, iterations=0, out_dir=directory,
                                routing_options=RoutingOptions(wire_width=4), layout_style='legacy')
            self.assertEqual(4, data['validation']['parameters']['wire_width'])
            renderer = load_local('render_clean', 'render-clean.py')
            renderer.render(directory / 'circuit-placement.json', directory / 'redraw.svg')
            self.assertEqual((directory / 'circuit-clean.svg').read_bytes(),
                             (directory / 'redraw.svg').read_bytes())
            self.assertIn('stroke-width="4"', (directory / 'redraw.svg').read_text(encoding='utf-8'))

    def test_saved_mode_cannot_escape_the_output_directory(self):
        source = ROOT / 'analog-canvas-main/netlists/native-rc-filters/circuit.spi'
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            directory = Path(directory)
            data = run_flow.run(source, iterations=0, out_dir=directory / 'initial', layout_style='legacy')
            data['mode'] = '../../../../escaped'
            saved = directory / 'bad-placement.json'
            saved.write_text(json.dumps(data), encoding='utf-8')
            before = saved.read_bytes()
            with self.assertRaisesRegex(ValueError, 'layout mode'):
                run_flow.reroute_placement(saved, out_dir=directory / 'new-output')
            self.assertEqual(before, saved.read_bytes())
            self.assertFalse((directory / 'new-output').exists())

    def test_saved_search_parameters_must_be_finite_and_bounded_types(self):
        for parameters in ({'max_nodes': float('inf')}, {'max_expansions': 1.5},
                           {'margins': None}, {'edge_clearance': float('inf')},
                           {'wire_width': -1}, {'max_rounds': True}):
            with self.subTest(parameters=parameters):
                with self.assertRaises(ValueError):
                    saved_routing_options({'validation': {'parameters': parameters}})


if __name__ == '__main__':
    unittest.main()
