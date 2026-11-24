"""Cumulative diagram limits and an immediately usable default formal drawing."""
from contextlib import redirect_stdout
from hashlib import sha256
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from netlist2schematic.flow.diagram_budget import DiagramBudget
from netlist2schematic.flow.process_budget import run_bounded
from netlist2schematic.flow import run_flow
from netlist2schematic.plugins.formal_output import formal_output as formal


class DiagramBudgetTests(unittest.TestCase):
    def test_later_stages_share_the_original_deadline(self):
        now = [10.]
        budget = DiagramBudget(120, clock=lambda: now[0])
        self.assertEqual(95., budget.allocate('normal', 95., reserve=10))
        now[0] += 90
        self.assertEqual(15., budget.allocate('rescue', 15., reserve=10))
        now[0] += 15
        self.assertEqual(5., budget.allocate('spacing', 60., reserve=10))
        now[0] += 5
        self.assertEqual(0., budget.allocate('bus', 60., reserve=10))
        self.assertEqual(130., budget.deadline)
        self.assertEqual(120., budget.snapshot()['limit_seconds'])

    def test_fast_success_does_not_wait_for_the_limit(self):
        now = [0.]
        budget = DiagramBudget(clock=lambda: now[0])
        now[0] = 3.
        self.assertEqual(3., budget.snapshot()['elapsed_seconds'])
        self.assertEqual([], budget.snapshot()['stages'])
        self.assertEqual(717., budget.remaining())

    def test_zero_explicitly_disables_the_total_cap_and_invalid_values_fail(self):
        budget = DiagramBudget(0)
        self.assertEqual(300., budget.allocate('normal', 300))
        for value in (-1, float('nan'), float('inf'), '120', True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                DiagramBudget(value)

    def test_cli_passes_single_limit_and_formal_output_by_default(self):
        with patch.object(run_flow, 'run', return_value={}) as execute:
            self.assertEqual(0, run_flow.main(['input.cir']))
        values = execute.call_args.kwargs
        self.assertEqual(720., values['time_seconds'])
        self.assertEqual(30., values['repair_seconds'])
        self.assertEqual(30., values['spacing_seconds'])
        self.assertIsNone(values['quality_seconds'])
        self.assertIsNone(values['rescue_seconds'])
        self.assertEqual('auto', values['formal_output'])
        with patch.object(run_flow, 'run', return_value={}) as execute:
            run_flow.main(['input.cir', '--time-seconds', '240', '--formal-output', 'off'])
        self.assertEqual(240., execute.call_args.kwargs['time_seconds'])
        self.assertEqual('off', execute.call_args.kwargs['formal_output'])


class OwnedProcessTests(unittest.TestCase):
    def test_completed_command_returns_immediately(self):
        started = time.perf_counter()
        code = run_bounded([sys.executable, '-c', 'pass'], seconds=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(0, code)
        self.assertLess(time.perf_counter()-started, 4)

    def test_timeout_reaps_the_owned_descendant_interpreter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root/'child.py'
            heartbeat = root/'heartbeat'
            child.write_text('import time\nfrom pathlib import Path\np=Path(__file__).with_name("heartbeat")\n'
                             'while True:\n p.write_text(str(time.time()))\n time.sleep(.02)\n', encoding='utf8')
            parent = root/'parent.py'
            parent.write_text('import subprocess,sys,time\nfrom pathlib import Path\n'
                              'subprocess.Popen([sys.executable,str(Path(__file__).with_name("child.py"))])\n'
                              'time.sleep(30)\n', encoding='utf8')
            code = run_bounded([sys.executable, str(parent)], seconds=1.5,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.assertEqual(124, code)
            self.assertTrue(heartbeat.is_file(), 'The descendant must actually have run')
            before = heartbeat.read_bytes()
            time.sleep(.15)
            self.assertEqual(before, heartbeat.read_bytes(), 'No child may keep running after timeout')


class DefaultFormalDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='default-delivery-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'local.cir'
        self.source.write_text('.model pm PMOS\nM1 d g1 VDD VDD pm\n'
                               'M2 d g2 VDD VDD pm\n.end\n', encoding='utf8')

    def generate(self, **kwargs):
        with redirect_stdout(io.StringIO()):
            return run_flow.run(self.source, iterations=0, seed=17, candidate_id='2',
                                simplify_routes='off', out_dir=self.root/'out', **kwargs)

    def test_default_saves_verified_formal_svg_at_the_top_level(self):
        original = self.source.read_bytes()
        data = self.generate()
        output = self.root/'out'
        info = json.loads((output/'flow-result.json').read_text(encoding='utf8'))
        self.assertEqual('valid', info['display']['status'])
        primary = output/'local-formal.svg'
        self.assertEqual(str(primary), info['display']['primary_svg'])
        self.assertEqual(sha256(primary.read_bytes()).hexdigest(), info['display']['svg_sha256'])
        saved = output/'local-placement.json'
        self.assertEqual(sha256(saved.read_bytes()).hexdigest(), info['native_sha256'])
        self.assertEqual(720., data['time_budget']['limit_seconds'])
        self.assertEqual(120., data['time_budget']['stages'][0]['allocated_seconds'])
        self.assertEqual(original, self.source.read_bytes())
        manifest = json.loads(Path(info['display']['record']).read_text(encoding='utf8'))
        self.assertTrue(manifest['validation']['valid'])
        self.assertEqual(json.loads(saved.read_text(encoding='utf8')), manifest['power_snapshot']['original'])
        self.assertEqual(0, manifest['style']['label_assignment']['ownership_violations'])

    def test_explicit_off_bypasses_the_display_plugin(self):
        with patch.object(formal, 'generate', side_effect=AssertionError('Display is off')):
            self.generate(formal_output='off')
        self.assertFalse((self.root/'out/local-formal.svg').exists())
        info = json.loads((self.root/'out/flow-result.json').read_text(encoding='utf8'))
        self.assertEqual('off', info['display']['status'])

    def test_v1_formal_records_still_redraw_byte_for_byte(self):
        self.generate(formal_output='off')
        with patch.object(formal, 'STYLE_VERSION', 'formal-schematic-v1'):
            before = formal.generate(self.root/'out/local-placement.json', self.root/'legacy')
        after = formal.redraw(before['artifacts']['json'], self.root/'redraw')
        self.assertEqual('formal-schematic-v1', after['style_version'])
        self.assertEqual(Path(before['artifacts']['svg']).read_bytes(), Path(after['artifacts']['svg']).read_bytes())


if __name__ == '__main__':
    unittest.main()
