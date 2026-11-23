"""Publication and native-flow compatibility for the optional shared VDD view."""
import builtins
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from netlist2schematic.common.sa_common import read_json, write_json
from netlist2schematic.flow import run_flow
from netlist2schematic.plugins.power_rails import power_rails as plugin


class PowerRailsFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='power-rail-flow-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root=Path(cls.temp.name)
        cls.include=cls.root/'empty.inc';cls.include.write_bytes(b'')
        cls.source=cls.root/'local.cir'
        cls.source.write_text('* local loads\n.include "empty.inc"\n.model pm PMOS\n'
            'M1 drain g1 VDD VDD pm\nM2 drain g2 VDD VDD pm\n.end\n',encoding='utf-8')
        original_import=builtins.__import__
        def guard(name,*args,**kwargs):
            if name.startswith('netlist2schematic.plugins.power_rails'):
                raise AssertionError('Explicit native-only flow must not import shared VDD plugin')
            return original_import(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=guard),redirect_stdout(io.StringIO()):
            run_flow.run(cls.source,iterations=0,candidate_id='1',seed=17,
                quality_seconds=10,fallback='off',simplify_routes='off',formal_output='off',out_dir=cls.root/'native')
        cls.saved=cls.root/'native/local-placement.json'
        cls.saved_bytes=cls.saved.read_bytes()

    def setUp(self):
        self.tempdir=tempfile.TemporaryDirectory(prefix='case-',dir=self.root)
        self.addCleanup(self.tempdir.cleanup)
        self.directory=Path(self.tempdir.name)

    def generate(self,**kwargs):
        return plugin.generate(self.saved,self.directory/'view',**kwargs)

    def test_original_is_verified_and_unchanged_no_search_runs(self):
        with ExitStack() as stack:
            for name in ('netlist2schematic.routing.orthogonal_router._find_path',
                         'netlist2schematic.routing.sa_routing.final_route',
                         'netlist2schematic.layout.structured_placement._anneal'):
                stack.enter_context(patch(name,side_effect=AssertionError('No new search')))
            result=self.generate()
        self.assertEqual('merged',result['view']['status'])
        self.assertEqual(1,result['view']['validation']['markers_removed'])
        self.assertEqual(self.saved_bytes,self.saved.read_bytes())
        self.assertEqual(json.loads(self.saved_bytes),result['original'])

    def test_saved_redraw_has_identical_geometry_and_text(self):
        result=self.generate()
        replay=plugin.redraw(result['artifacts']['json'],self.directory/'redrawn')
        self.assertEqual(result['view'],replay['view'])
        for key in ('clean_svg','color_svg'):
            self.assertEqual(Path(result['artifacts'][key]).read_bytes(),
                             Path(replay['artifacts'][key]).read_bytes())

    def test_old_inferred_netlist_path_survives_snapshot_redraw(self):
        data=json.loads(self.saved_bytes);data.pop('source_netlist')
        source=self.directory/'old.cir';source.write_bytes(self.source.read_bytes())
        (self.directory/'empty.inc').write_bytes(b'')
        placement=self.directory/'old-placement.json';write_json(placement,data)
        result=plugin.generate(placement,self.directory/'view')
        placement.unlink()
        replay=plugin.redraw(result['artifacts']['json'],self.directory/'redrawn')
        self.assertEqual(result['view'],replay['view'])

    def test_changed_symbol_fingerprint_is_refused(self):
        result=self.generate();changed=deepcopy(result)
        resources=changed['dependencies']['resource_files']
        resources[next(iter(resources))]='0'*64
        path=self.directory/'changed-resource.json';write_json(path,changed)
        with self.assertRaisesRegex(ValueError,'dependency changed'):
            plugin.redraw(path,self.directory/'rejected')

    def test_native_signal_polylines_and_component_transforms_are_preserved(self):
        result=self.generate()
        for key in ('clean_svg','color_svg'):
            root=ET.parse(result['artifacts'][key]).getroot()
            groups=[node for node in root.iter() if 'data-power-rail-net' in node.attrib]
            self.assertEqual(1,len(groups))
            self.assertEqual('vdd',groups[0].get('data-power-rail-net'))
            labels=[node.text for node in root.iter() if node.get('data-presentation-kind')=='shared-supply']
            self.assertEqual(['vdd'],labels)
            comps={node.get('data-component'):node.get('transform') for node in root.iter()
                   if node.get('data-component')}
            native=ET.parse(self.root/'native/local-clean.svg').getroot()
            self.assertEqual({node.get('data-component'):node.get('transform') for node in native.iter()
                              if node.get('data-component')},comps)
            paths=lambda doc:sorted((n.get('data-net'),n.get('points')) for n in doc.iter()
                                    if n.get('data-net') is not None and n.tag.endswith('polyline'))
            self.assertEqual(paths(native),paths(root))

    def test_rehashed_bad_supply_lines_and_wrong_label_are_refused(self):
        result=self.generate()
        for mutate in (lambda g:g['segments'].pop(),lambda g:g['label'].update(text='wrong')):
            changed=deepcopy(result);mutate(changed['view']['groups'][0])
            changed['view_sha256']=plugin._digest(changed['view'])
            path=self.directory/'tampered.json';write_json(path,changed)
            with self.assertRaises(ValueError):plugin.redraw(path,self.directory/'rejected')
            self.assertFalse((self.directory/'rejected').exists())

    def test_include_change_is_refused_on_redraw(self):
        result=self.generate()
        self.include.write_bytes(b'* changed\n')
        try:
            with self.assertRaisesRegex(ValueError,'dependency changed'):
                plugin.redraw(result['artifacts']['json'],self.directory/'rejected')
        finally:self.include.write_bytes(b'')

    def test_invalid_saved_routes_are_not_rescued_by_plugin(self):
        damaged=json.loads(self.saved_bytes);damaged['routes']={}
        path=self.directory/'invalid.json';write_json(path,damaged)
        with self.assertRaises(ValueError):plugin.generate(path,self.directory/'rejected')
        self.assertFalse((self.directory/'rejected').exists())

    def test_no_eligible_group_still_saves_verified_unchanged_view(self):
        result=self.generate(seconds=0)
        self.assertEqual('unchanged',result['view']['status'])
        self.assertTrue(result['view']['validation']['valid'])
        replay=plugin.redraw(result['artifacts']['json'],self.directory/'redrawn')
        self.assertEqual([],replay['view']['groups'])

    def test_plugin_failure_does_not_modify_native_finish_result(self):
        context=plugin._load_context(self.saved)
        with patch.object(plugin,'generate',side_effect=ValueError('blocked plugin')),redirect_stdout(io.StringIO()):
            result=run_flow._finish(deepcopy(context['data']),context['par'],context['placement'],
                context['comps'],context['nets'],context['index'],'local',self.directory/'finish',
                routed_result=(context['routed'],context['metrics'],context['validation']),
                simplify_routes='off',power_rails='auto')
        self.assertTrue(result['validation']['valid'])
        self.assertEqual(context['data']['routes'],json.loads(json.dumps(result['routes'])))
        diagnostic=read_json(self.directory/'finish/power-rails/plugin-diagnostics.json')
        self.assertTrue(diagnostic['native_result_preserved'])
        self.assertTrue((self.directory/'finish/local-clean.svg').is_file())

    def test_successful_hook_runs_after_saving_native_result(self):
        context=plugin._load_context(self.saved)
        generated=[];real_generate=plugin.generate
        def hook(path,**kwargs):
            self.assertTrue(Path(path).is_file())
            self.assertTrue(Path(path).with_name('local-clean.svg').is_file())
            generated.append(real_generate(path,**kwargs))
            return generated[-1]
        with patch.object(plugin,'generate',side_effect=hook),redirect_stdout(io.StringIO()):
            result=run_flow._finish(deepcopy(context['data']),context['par'],context['placement'],
                context['comps'],context['nets'],context['index'],'local',self.directory/'finish',
                routed_result=(context['routed'],context['metrics'],context['validation']),
                simplify_routes='off',power_rails='auto')
        self.assertEqual(1,len(generated))
        self.assertEqual('merged',generated[0]['view']['status'])
        self.assertEqual(read_json(self.directory/'finish/local-placement.json'),generated[0]['original'])
        self.assertEqual(context['data']['routes'],json.loads(json.dumps(result['routes'])))

    def test_cli_defaults_off_and_forwards_only_explicit_auto(self):
        for supplied in ([],['--power-rails','auto']):
            with patch.object(run_flow,'run',return_value={}) as call:
                self.assertEqual(0,run_flow.main(['input.cir',*supplied]))
            self.assertEqual('auto' if supplied else 'off',call.call_args.kwargs.get('power_rails','off'))


if __name__=='__main__':unittest.main()
