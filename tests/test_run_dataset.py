"""Batch execution uses one cumulative 120s default run and stops owned trees."""
from argparse import Namespace
from contextlib import redirect_stdout
from copy import deepcopy
from hashlib import sha256
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import run_dataset as batch


class DatasetRunnerTests(unittest.TestCase):
    def test_unique_subcircuit_is_selected_from_contents(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/'any-name.spi').write_text('.subckt unusual_cell a b\nR1 a b 1k\n.ends unusual_cell\n',encoding='utf8')
            rows=batch.inventory(root,None,1)
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['cell'],'unusual_cell')
            self.assertEqual(rows[0]['status'],'pending')

    def test_multiple_subcircuits_are_reported_without_guessing(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/'input.spi').write_text('.subckt a x y\nR1 x y 1k\n.ends a\n.subckt b x y\nR2 x y 2k\n.ends b\n',encoding='utf8')
            rows=batch.inventory(root,None,1)
            self.assertEqual(rows[0]['status'],'input_error')
            selected=batch.inventory(root,'b',1)
            self.assertEqual(selected[0]['status'],'pending')
            self.assertEqual(selected[0]['cell'],'b')

    def attempt(self,budget):
        with tempfile.TemporaryDirectory() as td:
            row=dict(tag='c01',source=str(Path(td)/'test.cir'),attempts=[])
            args=Namespace(iterations=20000,seed=123)
            with patch.object(batch,'run_bounded',return_value=1) as process:
                self.assertIsNone(batch.run_attempt(row,budget,1,Path(td),args))
            return process.call_args,row

    def test_default_has_one_120_second_wall_limit_and_no_stage_overrides(self):
        call,row=self.attempt(None)
        command=call.args[0]
        for flag in ('--quality-seconds','--rescue-seconds','--spacing-seconds',
                     '--fallback-seconds','--simplify-seconds','--candidate-id'):
            self.assertNotIn(flag,command)
        self.assertEqual(call.kwargs['seconds'],120)
        self.assertEqual(command[command.index('--time-seconds')+1],'120.0')
        self.assertEqual(row['attempts'][0]['budget']['wall'],120)
        self.assertEqual(call.kwargs['env'][batch.WORKER_ENV],'1')

    def test_quick_mode_has_explicit_wall_and_shared_stage_limits(self):
        call,row=self.attempt(batch.QUICK[0]);command=call.args[0]
        self.assertEqual(call.kwargs['seconds'],60)
        self.assertEqual(command[command.index('--quality-seconds')+1],'50')
        self.assertEqual(command[command.index('--simplify-seconds')+1],'5')
        self.assertNotIn('--candidate-id',command)
        call,row=self.attempt(batch.QUICK[1]);command=call.args[0]
        self.assertEqual(call.kwargs['seconds'],120)
        self.assertEqual(command[command.index('--quality-seconds')+1],'90')

    def run_main(self,mode,results):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'example.cir'
            source.write_text('* flat\nR1 a 0 1k\n.end\n',encoding='utf8')
            row=dict(tag='c01',source=str(source),source_sha256=sha256(source.read_bytes()).hexdigest(),
                ground_truth=None,gt_sha256=None,status='pending',attempts=[])
            command=[str(root),'--out-dir',str(root/'out')]
            if mode:command+=['--budget',mode]
            with patch.object(batch,'inventory',return_value=[deepcopy(row)]), \
                 patch.object(batch,'report'), \
                 patch.object(batch,'run_attempt',side_effect=results) as run,redirect_stdout(io.StringIO()):
                code=batch.main(command)
            manifest=json.loads((root/'out/manifest.json').read_text(encoding='utf8'))
            return code,run.call_args_list,manifest

    def test_default_has_one_call_and_no_whole_flow_retry(self):
        code,calls,manifest=self.run_main(None,[None])
        self.assertEqual(code,1)
        self.assertEqual(len(calls),1)
        self.assertIsNone(calls[0].args[1])
        self.assertEqual(manifest['cases'][0]['status'],'failed')

    def test_quick_success_stops_and_failure_retries_only_once(self):
        success=dict(routing_stage='normal',metrics=dict(wirelength=10,crossings=0,bends=0))
        code,calls,_=self.run_main('quick',[success])
        self.assertEqual(code,0);self.assertEqual(len(calls),1)
        code,calls,_=self.run_main('quick',[None,None])
        self.assertEqual(code,1);self.assertEqual(len(calls),2)
        self.assertEqual([c.args[1]['wall'] for c in calls],[60,120])

    def test_bus_retries_and_retains_a_verified_result_if_retry_fails(self):
        bus=dict(routing_stage='bus_fallback',metrics=dict(wirelength=100,crossings=4,bends=8))
        code,calls,manifest=self.run_main('quick',[bus,None])
        self.assertEqual(code,0);self.assertEqual(len(calls),2)
        self.assertEqual(manifest['cases'][0]['routing_stage'],'bus_fallback')


if __name__=='__main__':unittest.main()
