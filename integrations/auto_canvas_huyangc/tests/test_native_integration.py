"""Optional real native contracts. Set AUTO_CANVAS_PROJECT_ROOT and runtime."""
from __future__ import annotations

from argparse import Namespace
from contextlib import redirect_stdout
from copy import deepcopy
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from integrations.auto_canvas_huyangc.cli import ADAPTER_ROOT, REPOSITORY_ROOT, find_node, run

TARGET = os.environ.get('AUTO_CANVAS_PROJECT_ROOT')
RUNTIME = Path(os.environ.get('AUTO_CANVAS_RUNTIME', REPOSITORY_ROOT/'.cache/auto-canvas-runtime'))


@unittest.skipUnless(TARGET and (RUNTIME/'runtime-manifest.json').is_file(),
                     'Native project checkout and pinned runtime are opt-in')
class NativeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory()
        cls.root=Path(cls.temporary.name)
        cls.target=Path(TARGET).resolve()
        cls.node=find_node(cls.target,None)
        cls.engine=Path(os.environ.get('NETLIST2SCHEMATIC_ENGINE_ROOT', REPOSITORY_ROOT))
        cls.results={}
        sources={
            'rc':cls.target/'netlists/native-rc-filters/circuit.spi',
            'cs':cls.target/'netlists/native-common-source/circuit.spi',
            'bjt':ADAPTER_ROOT/'tests/fixtures/bjt-dual-negative.spi',
            'ports':ADAPTER_ROOT/'tests/fixtures/multi-cell.spi',
            'ordered':ADAPTER_ROOT/'tests/fixtures/ordered-supply-ports.spi',
        }
        for name,source in sources.items():
            project=cls.root/(name+'.icproj.json')
            cls.native('import-spice',source,project)
            before=sha256(project.read_bytes()).hexdigest()
            args=Namespace(project_root=cls.target,engine_root=cls.engine,input=project,
                out_dir=cls.root/(name+'-result'),runtime=RUNTIME,node=Path(cls.node),
                document_id=None,engine_seconds=60,iterations=0,seed=17)
            args.cell='amplifier' if name=='ports' else None
            with redirect_stdout(StringIO()):
                report=run(args)
            if report['status']!='valid' or sha256(project.read_bytes()).hexdigest()!=before:
                raise AssertionError('Native integration did not preserve its input')
            cls.results[name]=(project,args,report)

    @classmethod
    def tearDownClass(cls):
        # The directory was created by this test and contains only its own artifacts.
        cls.temporary.cleanup()

    @classmethod
    def native(cls,operation,source,output,extra=(),expected=0):
        command=[cls.node,'--experimental-transform-types',str(ADAPTER_ROOT/'native.mjs'),operation,
            '--project-root',str(cls.target),'--runtime',str(RUNTIME),
            '--input',str(source),'--output',str(output),*map(str,extra)]
        result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',timeout=60)
        if result.returncode!=expected:
            raise AssertionError(result.stdout+'\n'+result.stderr)
        return result

    def test_rc_mos_and_bjt_roundtrip_are_native_editable_projects(self):
        for name,(_,args,report) in self.results.items():
            with self.subTest(name=name):
                data=json.loads(Path(report['native_project']).read_text(encoding='utf-8'))
                self.assertIn('componentDefinitions',data)
                self.assertIn('connections',data['documents'][0])
                self.assertTrue(report['validation']['electricalEquivalent'])
                self.assertTrue(report['validation']['roundtripVerified'])
                self.assertEqual(report['validation']['nativeAudit']['unsafeProblems'],[])
                visible=report['validation']['visibleSignals']
                self.assertTrue(visible['checked'])
                self.assertTrue(all(n['connected'] and n['coreCovered'] and
                                    n['cycles']<=n['originalCycles'] for n in visible['nets']))
                self.assertTrue(Path(report['native_svg']).is_file())
                manifest=json.loads((args.out_dir/'private/engine/engine-snapshot.json').read_text(encoding='utf-8'))
                self.assertFalse(manifest['source_code_modified'])

    def test_mos_external_binding_and_original_parameters_survive(self):
        _,args,report=self.results['cs']
        snap=json.loads((args.out_dir/'private/native-snapshot.json').read_text(encoding='utf-8'))
        original=next(d for d in snap['project']['documents'] if d['id']==snap['documentId'])
        self.assertTrue(any(i.get('netlist',{}).get('binding',{}).get('kind')=='external-subcircuit'
                            for i in original['instances']))
        self.assertTrue(report['validation']['modelParametersPreserved'])

    def test_vss_vee_and_zero_remain_distinct(self):
        _,args,report=self.results['bjt']
        profile=json.loads((args.out_dir/'private/adapter-profile.json').read_text(encoding='utf-8'))
        self.assertTrue({'0','vee','vss'} <= set(profile['net_names'].values()))
        plan=json.loads((args.out_dir/'native-plan.json').read_text(encoding='utf-8'))
        self.assertEqual(sum(m['style']=='negative-cap' for m in plan['powerMarkers']),2)
        self.assertTrue(report['validation']['electricalEquivalent'])

    def test_supply_glyphs_do_not_promote_or_reorder_formal_ports(self):
        _,args,report=self.results['ordered']
        snapshot=json.loads((args.out_dir/'private/native-snapshot.json').read_text(encoding='utf8'))
        before=next(d for d in snapshot['project']['documents'] if d['id']==snapshot['documentId'])
        after_file=json.loads(Path(report['native_project']).read_text(encoding='utf8'))
        after=next(d for d in after_file['documents'] if d['id']==before['id'])
        self.assertEqual(before['netlist']['terminals'],after['netlist']['terminals'])
        self.assertEqual(report['validation']['powerInterfacePresentationChanges'],[])
        plan=json.loads((args.out_dir/'native-plan.json').read_text(encoding='utf8'))
        protected={t['netId'] for t in before['netlist']['terminals']}
        self.assertFalse(any(e['kind']=='upsert_connectivity_evidence' and
            e['evidence'].get('netId') in protected for e in plan['edits']))
        self.assertTrue(report['validation']['electricalEquivalent'])

    def test_source_revision_and_model_version_tampering_are_refused(self):
        project,args,_=self.results['rc']
        original=json.loads((args.out_dir/'native-plan.json').read_text(encoding='utf-8'))
        for key in ('expectedRevision','nativeModelVersion'):
            with self.subTest(key=key):
                plan=deepcopy(original);plan[key]+=1
                path=self.root/('bad-'+key+'.json');path.write_text(json.dumps(plan),encoding='utf-8')
                output=self.root/('bad-'+key+'.icproj.json')
                result=self.native('apply',project,output,['--plan',path],expected=1)
                self.assertIn('STALE_INPUT',result.stderr)
                self.assertFalse(output.exists())

    def test_wrong_pin_geometry_is_rejected_before_success_output(self):
        project,args,_=self.results['rc']
        plan=json.loads((args.out_dir/'native-plan.json').read_text(encoding='utf-8'))
        plan['expectedPins'][0]['position']['x']+=2
        path=self.root/'bad-pins.json';path.write_text(json.dumps(plan),encoding='utf-8')
        output=self.root/'bad-pins.icproj.json'
        result=self.native('apply',project,output,['--plan',path],expected=1)
        self.assertIn('PIN_GEOMETRY_MISMATCH',result.stderr)
        self.assertFalse(output.exists())

    def test_existing_output_directory_is_not_overwritten(self):
        _,args,report=self.results['rc']
        original=Path(report['native_project']).read_bytes()
        with self.assertRaisesRegex(ValueError,'empty output'):
            run(args)
        self.assertEqual(original,Path(report['native_project']).read_bytes())

    def test_missing_signal_route_is_not_hidden_by_logical_net_membership(self):
        project,args,_=self.results['rc']
        plan=json.loads((args.out_dir/'native-plan.json').read_text(encoding='utf8'))
        removed=next(e for e in plan['edits'] if e['kind']=='set_route_path' and
                     e['route']['netId'] in plan['signalGeometry'])
        plan['edits'].remove(removed)
        path=self.root/'missing-visible-route.json'
        path.write_text(json.dumps(plan),encoding='utf8')
        output=self.root/'missing-visible-route.icproj.json'
        result=self.native('apply',project,output,['--plan',path],expected=1)
        self.assertRegex(result.stderr,'NATIVE_(VISIBLE_SIGNAL_DISCONNECTED|CORE_SIGNAL_REMOVED|ROUTE_AUDIT_FAILED)')
        self.assertFalse(output.exists())

    def test_formal_interface_and_unselected_cells_are_preserved(self):
        project,args,report=self.results['ports']
        self.assertEqual(report['validation']['powerInterfacePresentationChanges'],[])
        snap=json.loads((args.out_dir/'private/native-snapshot.json').read_text(encoding='utf-8'))
        selected=next(d for d in snap['project']['documents'] if d['id']==snap['documentId'])
        self.assertEqual({p['name'] for p in selected['netlist']['terminals']},{'inp','out','vdd','vss'})
        roundtrip=self.root/'ports-after-native-snapshot.json'
        self.native('snapshot',Path(report['native_project']),roundtrip,['--document-id',selected['id']])
        result=json.loads(roundtrip.read_text(encoding='utf8'))
        after_selected=next(d for d in result['project']['documents'] if d['id']==selected['id'])
        for interface in selected['netlist']['terminals']:
            for ident in interface['interfaceInstanceIds']:
                before=next(i for i in selected['instances'] if i['id']==ident)
                after=next(i for i in after_selected['instances'] if i['id']==ident)
                self.assertEqual(before['symbolId'],after['symbolId'])
        original=json.loads(project.read_text(encoding='utf-8'))
        after=json.loads(Path(report['native_project']).read_text(encoding='utf-8'))
        self.assertGreater(len(original['documents']),1)
        for before in original['documents']:
            if before['id']==snap['documentId']:continue
            copy=next(d for d in after['documents'] if d['id']==before['id'])
            self.assertEqual(before,copy)

    def test_browser_model_and_final_edits_use_one_verified_native_transaction(self):
        for name,(project,args,report) in self.results.items():
            with self.subTest(name=name):
                snapshot=json.loads((args.out_dir/'private/native-snapshot.json').read_text(encoding='utf8'))
                request=dict(type='plan',runId=4,project=snapshot['project'],documentIds=[snapshot['documentId']])
                payload=self.root/(name+'-browser-request.json')
                payload.write_bytes(json.dumps(request).encode('utf8'))
                converted=self.root/(name+'-browser-source.icproj.json')
                prefix=[self.node,'--experimental-transform-types',str(ADAPTER_ROOT/'web-native.mjs')]
                shared=['--project-root',str(self.target),'--runtime',str(RUNTIME)]
                first=subprocess.run(prefix+['input',*shared,'--input',str(payload),'--output',str(converted)],
                    capture_output=True,text=True,encoding='utf8',timeout=60)
                self.assertEqual(first.returncode,0,first.stderr)
                self.assertEqual(converted.read_bytes(),project.read_bytes())
                result=self.root/(name+'-browser-edits.json')
                command=prefix+['result',*shared,'--input',str(converted),'--output',str(result),
                    '--plan',str(args.out_dir/'native-plan.json'),'--validation',str(args.out_dir/'native-validation.json'),
                    '--expected',report['native_project']]
                checked=subprocess.run(command,capture_output=True,text=True,encoding='utf8',timeout=60)
                self.assertEqual(checked.returncode,0,checked.stderr)
                data=json.loads(result.read_text(encoding='utf8'))
                self.assertTrue(data['validation']['atomicTransaction'])
                self.assertTrue(data['validation']['electricalEquivalent'])
                self.assertTrue(data['validation']['visibleSignals']['checked'])
                self.assertEqual(data['documentId'],snapshot['documentId'])


if __name__=='__main__':unittest.main()
