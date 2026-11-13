"""Publication styling reuses validated geometry and survives standalone redraw."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from netlist2schematic.flow import run_flow
from netlist2schematic.common.sa_common import load_local


def signal_paths(path):
    root=ET.parse(path).getroot()
    return sorted((item.get('data-net'),item.get('points')) for item in root.iter()
                  if item.tag.endswith('polyline') and item.get('data-net'))


class PresentationFlowTests(unittest.TestCase):
    def test_saved_styling_never_relayouts_or_reroutes_and_redraws_identically(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);source=folder/'pair.cir'
            source.write_text('.subckt dut VinP VinN VDD Bias OutP OutN\n.model N nmos\n'
                'M1 outp vinp tail 0 N\nM2 outn vinn tail 0 N\n'
                'M3 tail bias 0 0 N\nR1 vdd outp 1k\nR2 vdd outn 1k\n.ends dut\n',encoding='utf8')
            with redirect_stdout(StringIO()):
                old=run_flow.run(source,cell='dut',structure_layout='auto',candidate_id='2',
                    iterations=120,quality_seconds=30,out_dir=folder/'old',simplify_seconds=.05)
            saved=folder/'old/pair-dut-placement.json'; original=saved.read_bytes()
            with patch('netlist2schematic.routing.orthogonal_router.route_scene',side_effect=AssertionError('styling called A*')), \
                 patch('netlist2schematic.layout.structured_placement.create_candidates',side_effect=AssertionError('styling relayout')),redirect_stdout(StringIO()):
                new=run_flow.simplify_placement(saved,out_dir=folder/'new',simplify_routes='off',presentation='clean')
            self.assertEqual(original,saved.read_bytes())
            self.assertEqual(old['placement'],new['placement'])
            self.assertEqual(old['routes'],new['routes'])
            self.assertEqual(old['metrics'],new['metrics'])
            clean=folder/'new/pair-dut-clean.svg'; color=folder/'new/pair-dut-layered.svg'
            self.assertEqual(signal_paths(clean),signal_paths(color))
            text=clean.read_text(encoding='utf8')
            self.assertIn('VinP',text)
            self.assertIn('OutP',text)
            self.assertNotIn('WL=',text)
            self.assertNotIn('stroke-dasharray="6 4"',text)
            renderer=load_local('publication_flow_renderer','render-clean.py')
            with redirect_stdout(StringIO()):
                renderer.render(folder/'new/pair-dut-placement.json',folder/'redraw.svg')
            self.assertEqual(clean.read_bytes(),(folder/'redraw.svg').read_bytes())

    def test_cli_default_remains_original_and_explicit_new_options_are_forwarded(self):
        with patch('netlist2schematic.flow.run_flow.run') as execute:
            run_flow.main(['example.cir'])
        self.assertEqual('legacy',execute.call_args.kwargs['presentation'])
        self.assertEqual('off',execute.call_args.kwargs['port_preflight'])
        self.assertEqual('sequential',execute.call_args.kwargs['candidate_budget'])
        with patch('netlist2schematic.flow.run_flow.run') as execute:
            run_flow.main(['example.cir','--presentation','clean','--port-preflight','repair',
                           '--candidate-budget','reserved'])
        self.assertEqual('clean',execute.call_args.kwargs['presentation'])
        self.assertEqual('repair',execute.call_args.kwargs['port_preflight'])
        self.assertEqual('reserved',execute.call_args.kwargs['candidate_budget'])


if __name__=='__main__':unittest.main()
