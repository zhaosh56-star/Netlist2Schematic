"""A real small A* flow proves dx survives publication and saved rerouting."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from netlist2schematic.flow import run_flow
from netlist2schematic.common.sa_common import load_local
from netlist2schematic.common.sa_saved import load_placement


class BranchRoundTripTests(unittest.TestCase):
    def test_real_pins_saved_dx_redraw_and_fixed_reroute(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            source=folder/'pair.cir'
            text=('.model N nmos\nM1 oa inp tail 0 N\nM2 ob inn tail 0 N\n'
                  'M3 tail bias 0 0 N\nR1 vdd oa 1k\nR2 vdd ob 1k\n')
            source.write_text(text,encoding='utf8')
            with redirect_stdout(StringIO()):
                data=run_flow.run(source,structure_layout='auto',structure_phase=3,
                    candidate_id='2',iterations=120,quality_seconds=30,
                    out_dir=folder/'first',simplify_seconds=.05)
            self.assertTrue(data['validation']['valid'])
            self.assertTrue(any(row.get('dx',0)!=0 for row in data['placement'].values()))
            self.assertEqual(text,source.read_text(encoding='utf8'))
            saved=folder/'first/pair-placement.json'
            loaded=load_placement(saved)
            self.assertEqual([data['placement'][c['id']]['dx'] for c in loaded[4]],loaded[3].offset_x)
            renderer=load_local('branch_roundtrip_renderer','render-clean.py')
            with redirect_stdout(StringIO()):renderer.render(saved,folder/'redraw.svg')
            self.assertEqual((folder/'first/pair-clean.svg').read_bytes(),(folder/'redraw.svg').read_bytes())
            with patch('netlist2schematic.plugins.layout_shared.branch_alignment.regularize_branch_layout',side_effect=AssertionError('saved relayout')),redirect_stdout(StringIO()):
                rerouted=run_flow.reroute_placement(saved,out_dir=folder/'rerouted',simplify_routes='off')
            self.assertEqual(data['placement'],rerouted['placement'])
            self.assertTrue(rerouted['validation']['valid'])


if __name__=='__main__':unittest.main()
