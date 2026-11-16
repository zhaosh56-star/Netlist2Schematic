"""Publication styling reuses validated geometry and survives standalone redraw."""
from contextlib import redirect_stderr, redirect_stdout
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

    def test_cli_enables_port_preflight_without_changing_other_defaults(self):
        with patch('netlist2schematic.flow.run_flow.run') as execute:
            run_flow.main(['example.cir'])
        self.assertEqual('legacy',execute.call_args.kwargs['presentation'])
        self.assertEqual('repair',execute.call_args.kwargs['port_preflight'])
        self.assertEqual('sequential',execute.call_args.kwargs['candidate_budget'])
        for policy in ('attachment_layout','structure_layout','region_layout',
                       'readability_layout','symmetry_layout','net_labels'):
            self.assertEqual('off',execute.call_args.kwargs[policy])
        with patch('netlist2schematic.flow.run_flow.run') as execute:
            run_flow.main(['example.cir','--presentation','clean','--port-preflight','repair',
                           '--candidate-budget','reserved'])
        self.assertEqual('clean',execute.call_args.kwargs['presentation'])
        self.assertEqual('repair',execute.call_args.kwargs['port_preflight'])
        self.assertEqual('reserved',execute.call_args.kwargs['candidate_budget'])

    def test_cli_preflight_off_still_replays_the_original_policy(self):
        with patch('netlist2schematic.flow.run_flow.run') as execute:
            self.assertEqual(0,run_flow.main(['example.cir','--port-preflight','off']))
        self.assertEqual('off',execute.call_args.kwargs['port_preflight'])

    def test_saved_cli_modes_keep_implicit_preflight_off(self):
        for flag, operation in (('--reroute-placement','reroute_placement'),
                                ('--simplify-placement','simplify_placement')):
            for extra in ([], ['--port-preflight','off']):
                with self.subTest(flag=flag,extra=extra), \
                     patch.object(run_flow,operation) as execute:
                    self.assertEqual(0,run_flow.main([flag,'saved.json',*extra]))
                    execute.assert_called_once()
                    self.assertNotIn('port_preflight',execute.call_args.kwargs)

    def test_saved_cli_modes_still_reject_explicit_port_repair(self):
        for flag, operation in (('--reroute-placement','reroute_placement'),
                                ('--simplify-placement','simplify_placement')):
            with self.subTest(flag=flag), patch.object(run_flow,operation) as execute, \
                 redirect_stderr(StringIO()), self.assertRaises(SystemExit) as caught:
                run_flow.main([flag,'saved.json','--port-preflight','repair'])
            self.assertEqual(2,caught.exception.code)
            execute.assert_not_called()

    def test_python_flow_default_enables_repair_and_accepts_off(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);source=folder/'input.cir'
            source.write_text('R1 a 0 1k\n.end\n',encoding='utf8')
            for arguments, expected in (({},'repair'),({'port_preflight':'off'},'off')):
                with self.subTest(expected=expected), \
                     patch.object(run_flow,'_normal_result',side_effect=RuntimeError('stop before search')) as execute, \
                     self.assertRaisesRegex(RuntimeError,'stop before search'):
                    run_flow.run(source,out_dir=folder/'result',**arguments)
                self.assertEqual(expected,execute.call_args.kwargs['port_preflight'])


if __name__=='__main__':unittest.main()
