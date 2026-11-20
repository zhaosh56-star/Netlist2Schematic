"""Formal output is an independent, replayable last-stage display plugin."""
import builtins
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from hashlib import sha256
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from netlist2schematic.common.sa_common import ROOT, read_json, write_json
from netlist2schematic.flow import run_flow
from netlist2schematic.plugins.formal_output import formal_output as plugin
from netlist2schematic.plugins.power_rails import power_rails as supply


class FormalOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='formal-output-tests-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root=Path(cls.temp.name)
        cls.include=cls.root/'empty.inc';cls.include.write_bytes(b'')
        cls.source=cls.root/'local.cir'
        cls.source.write_text('.include "empty.inc"\n.model pm PMOS\n'
            'M1 drain g1 VDD VDD pm\nM2 drain g2 VDD VDD pm\n.end\n',encoding='utf8')
        original_import=builtins.__import__
        def guard(name,*args,**kwargs):
            if name.startswith(('netlist2schematic.plugins.formal_output','netlist2schematic.plugins.power_rails')):
                raise AssertionError('Default flow must not import display plugins')
            return original_import(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=guard),redirect_stdout(io.StringIO()):
            run_flow.run(cls.source,iterations=0,candidate_id='1',seed=17,
                quality_seconds=10,fallback='off',simplify_routes='off',out_dir=cls.root/'native')
        cls.saved=cls.root/'native/local-placement.json'
        cls.original=cls.saved.read_bytes()

    def setUp(self):
        self.case=tempfile.TemporaryDirectory(prefix='case-',dir=self.root)
        self.addCleanup(self.case.cleanup)
        self.folder=Path(self.case.name)

    def generate(self,**kwargs):
        return plugin.generate(self.saved,self.folder/'formal',**kwargs)

    def test_generate_never_calls_search_or_changes_native(self):
        with ExitStack() as stack:
            for name in ('netlist2schematic.routing.orthogonal_router._find_path',
                         'netlist2schematic.routing.sa_routing.final_route',
                         'netlist2schematic.layout.structured_placement._anneal'):
                stack.enter_context(patch(name,side_effect=AssertionError('No new search')))
            result=self.generate()
        self.assertTrue(result['validation']['valid'])
        self.assertEqual('merged',result['power_snapshot']['view']['status'])
        self.assertEqual(1,result['validation']['power']['markers_removed'])
        self.assertEqual(self.original,self.saved.read_bytes())
        self.assertEqual(json.loads(self.original),result['power_snapshot']['original'])
        document=ET.parse(result['artifacts']['svg']).getroot()
        self.assertEqual('formal-schematic-v1',document.get('data-style-profile'))
        self.assertTrue(all(v==0 for v in result['style']['text_validation'].values()))

    def test_off_keeps_all_power_markers_but_applies_typography(self):
        result=self.generate(power_rails='off')
        self.assertEqual([],result['power_snapshot']['view']['groups'])
        self.assertEqual(0,result['validation']['power']['markers_removed'])
        self.assertEqual(15.116,result['style']['font_size'])

    def test_no_temporary_artifact_paths_are_saved(self):
        result=self.generate()
        for path in result['artifacts'].values():self.assertTrue(Path(path).is_file())
        power=read_json(result['artifacts']['power_json'])
        for path in power['artifacts'].values():self.assertTrue(Path(path).is_file())
        self.assertNotIn('.formal-stage-',Path(result['artifacts']['json']).read_text(encoding='utf8'))

    def test_redraw_preserves_exact_svg_and_never_replans_supplies(self):
        result=self.generate()
        before=Path(result['artifacts']['svg']).read_bytes()
        with patch.object(supply,'generate',side_effect=AssertionError('Do not reselect VDD')):
            replay=plugin.redraw(result['artifacts']['json'],self.folder/'replay')
        self.assertEqual(before,Path(replay['artifacts']['svg']).read_bytes())
        self.assertEqual(result['native_metrics'],replay['native_metrics'])
        self.assertEqual(result['power_snapshot']['view'],replay['power_snapshot']['view'])

    def test_redraw_does_not_require_historical_native_json(self):
        copy=self.folder/'copy-placement.json';copy.write_bytes(self.original)
        result=plugin.generate(copy,self.folder/'formal')
        copy.unlink()
        replay=plugin.redraw(result['artifacts']['json'],self.folder/'replay')
        self.assertEqual(result['svg_sha256'],replay['svg_sha256'])

    def test_include_change_is_rejected(self):
        result=self.generate()
        self.include.write_bytes(b'* changed\n')
        try:
            with self.assertRaisesRegex(ValueError,'dependency changed'):
                plugin.redraw(result['artifacts']['json'],self.folder/'rejected')
        finally:self.include.write_bytes(b'')

    def test_bad_native_connectivity_is_not_rescued(self):
        data=json.loads(self.original);data['routes']={}
        path=self.folder/'broken.json';write_json(path,data)
        with self.assertRaises(ValueError):plugin.generate(path,self.folder/'rejected')
        self.assertFalse((self.folder/'rejected').exists())

    def test_rehashed_wrong_supply_net_still_fails_independent_geometry(self):
        result=self.generate();data=deepcopy(result);power=data['power_snapshot']
        power['view']['groups'][0]['net']='wrong'
        power['view_sha256']=supply._digest(power['view'])
        data['fingerprint']=plugin._digest(plugin._identity(power,data['policy'],data['title']))
        data['snapshot_sha256']=plugin._digest(plugin._snapshot(data))
        changed=self.folder/'changed-formal.json';write_json(changed,data)
        with self.assertRaises(ValueError):plugin.redraw(changed,self.folder/'rejected')
        self.assertFalse(list((self.folder/'rejected').rglob('*-formal.svg')))

    def test_failed_text_export_preserves_previously_successful_artifacts(self):
        result=self.generate()
        before={p:sha256(Path(p).read_bytes()).hexdigest() for p in result['artifacts'].values()}
        with patch.object(plugin,'formalize_svg',side_effect=ValueError('no safe text spot')):
            with self.assertRaisesRegex(ValueError,'safe text'):self.generate()
        self.assertTrue(all(sha256(Path(p).read_bytes()).hexdigest()==h for p,h in before.items()))
        self.assertEqual(self.original,self.saved.read_bytes())

    def test_hook_failure_keeps_native_success(self):
        ctx=supply._load_context(self.saved)
        with patch.object(plugin,'generate',side_effect=ValueError('formal failure')),redirect_stdout(io.StringIO()):
            result=run_flow._finish(deepcopy(ctx['data']),ctx['par'],ctx['placement'],ctx['comps'],
                ctx['nets'],ctx['index'],'local',self.folder/'flow',
                routed_result=(ctx['routed'],ctx['metrics'],ctx['validation']),
                simplify_routes='off',formal_output='auto')
        self.assertTrue(result['validation']['valid'])
        self.assertEqual(read_json(self.saved)['routes'],json.loads(json.dumps(result['routes'])))
        self.assertTrue(read_json(self.folder/'flow/formal/plugin-diagnostics.json')['native_result_preserved'])

    def test_hook_runs_after_native_success_is_saved(self):
        ctx=supply._load_context(self.saved);calls=[];generate=plugin.generate
        def hook(path,**kwargs):
            self.assertTrue(Path(path).is_file())
            self.assertTrue(Path(path).with_name('local-clean.svg').is_file())
            calls.append(generate(path,**kwargs));return calls[-1]
        with patch.object(plugin,'generate',side_effect=hook),redirect_stdout(io.StringIO()):
            result=run_flow._finish(deepcopy(ctx['data']),ctx['par'],ctx['placement'],ctx['comps'],
                ctx['nets'],ctx['index'],'local',self.folder/'flow',
                routed_result=(ctx['routed'],ctx['metrics'],ctx['validation']),
                simplify_routes='off',formal_output='auto')
        self.assertEqual(1,len(calls));self.assertTrue(calls[0]['validation']['valid'])
        self.assertEqual(read_json(self.folder/'flow/local-placement.json'),calls[0]['power_snapshot']['original'])

    def test_cli_default_off_and_explicit_policies_for_all_input_modes(self):
        modes=[(['input.cir'],'run'),(['--reroute-placement','saved.json'],'reroute_placement'),
               (['--simplify-placement','saved.json'],'simplify_placement')]
        for arguments,method in modes:
            for options in ([],['--formal-output','auto'],['--formal-output','auto','--formal-power-rails','off']):
                with self.subTest(method=method,options=options),patch.object(run_flow,method,return_value={}) as call:
                    self.assertEqual(0,run_flow.main(arguments+options))
                    self.assertEqual('auto' if options else 'off',call.call_args.kwargs.get('formal_output','off'))
                    if options:self.assertEqual('off' if options[-1]=='off' else 'auto',call.call_args.kwargs['formal_power_rails'])

    def test_standalone_cli_works_from_another_directory(self):
        command=[sys.executable,'-X','utf8','-B',str(ROOT/'netlist2schematic/plugins/formal_output/formal_output.py'),
            '--placement',str(self.saved),'--out-dir',str(self.folder/'external'),'--power-rails','off']
        result=subprocess.run(command,cwd=self.folder,capture_output=True,text=True,encoding='utf8',timeout=15,
                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual(1,len(list((self.folder/'external').rglob('*-formal.svg'))))


if __name__=='__main__':unittest.main()
