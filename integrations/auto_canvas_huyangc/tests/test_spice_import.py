"""Import real native projects; inspect terminal identity, not just success flags."""
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
from integrations.auto_canvas_huyangc.profile import compile_snapshot

TARGET = os.environ.get('AUTO_CANVAS_PROJECT_ROOT')
RUNTIME = Path(os.environ.get('AUTO_CANVAS_RUNTIME', REPOSITORY_ROOT/'.cache/auto-canvas-runtime'))


@unittest.skipUnless(TARGET and (RUNTIME/'runtime-manifest.json').is_file(), 'Native runtime is opt-in')
class SpiceImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.target = Path(TARGET).resolve()
        cls.node = find_node(cls.target, None)
        cls.input = ADAPTER_ROOT/'tests/fixtures/named-opamps.spi'
        cls.project = cls.root/'named.icproj.json'
        cls.report = json.loads(cls.native('import-spice', cls.input, cls.project).stdout)
        cls.snapshot_file = cls.root/'snapshot.json'
        cls.native('snapshot', cls.project, cls.snapshot_file)
        cls.snapshot = json.loads(cls.snapshot_file.read_text(encoding='utf8'))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @classmethod
    def native(cls, operation, source, output, expected=0):
        result = subprocess.run([cls.node, '--experimental-transform-types', str(ADAPTER_ROOT/'native.mjs'),
            operation, '--project-root', str(cls.target), '--runtime', str(RUNTIME),
            '--input', str(source), '--output', str(output)],
            capture_output=True, text=True, encoding='utf8', timeout=60)
        if result.returncode != expected:
            raise AssertionError(result.stdout+'\n'+result.stderr)
        return result

    def test_all_original_devices_and_all_five_opamp_terminals_are_present(self):
        s=self.snapshot
        doc=next(d for d in s['project']['documents'] if d['id']==s['documentId'])
        self.assertEqual({i['id'] for i in doc['instances']},
                         {'I1','M1','M2','R1','R2','XEfull','XEsmall'})
        names=compile_snapshot(s)['net_names']
        pins={t['pinName']:names[n['id']] for n in doc['nets'] for t in n['terminals']
              if t['instanceId']=='XEfull'}
        self.assertEqual(pins, {'IN+':'left','IN-':'right','OUT':'feedback','VDD':'vdd','VSS':'vss'})
        small={t['pinName'] for n in doc['nets'] for t in n['terminals'] if t['instanceId']=='XEsmall'}
        self.assertEqual(small, {'IN+','IN-','OUT'})
        self.assertEqual({p['name'] for p in s['symbols']['XEfull']['pins']}, {'IN+','IN-','OUT'})

    def test_original_source_and_explicit_parameters_survive_native_serialization(self):
        project=json.loads(self.project.read_text(encoding='utf8'))
        self.assertEqual(project['source']['files'][0]['originalContent']['text'],
                         self.input.read_text(encoding='utf8'))
        doc=self.snapshot['project']['documents'][0]
        full=next(i for i in doc['instances'] if i['id']=='XEfull')
        self.assertEqual(full['netlist']['parameters']['gain'],'1e5')
        self.assertEqual(len(self.report['compatibility']['changes']),5)

    def test_adapter_uses_named_opamp_port_order_without_synthetic_ground(self):
        before=deepcopy(self.snapshot)
        p=compile_snapshot(self.snapshot)
        self.assertEqual(self.snapshot,before)
        rows=[line.split() for line in p['netlist'].splitlines() if line.startswith('E')]
        self.assertEqual([row[1:] for row in rows], [
            ['vdd','vss','left','right','feedback','opamp'],
            ['feedback','reference','output','opamp']])

    def test_unverified_or_supply_visible_black_boxes_are_rejected(self):
        for case in ('target','descriptor','visible'):
            source=deepcopy(self.snapshot)
            doc=source['project']['documents'][0]
            full=next(i for i in doc['instances'] if i['id']=='XEfull')
            if case=='target':full['netlist']['binding']['name']='unrelated'
            elif case=='descriptor':source['symbols']['XEfull'].pop('builtInSubcircuit')
            else:source['symbols']['XEfull']['pins'].append(dict(name='VDD',at=dict(x=0,y=-30),
                landing=dict(x=0,y=-30),direction='north'))
            with self.subTest(case=case),self.assertRaisesRegex(ValueError,'Unsupported|Drawn native pins'):
                compile_snapshot(source)

    def test_native_hierarchy_errors_are_not_hidden_by_compatibility(self):
        for index,text in enumerate(['* bad\nR1 a b 1k\n.ends dut',
                '* bad\n.ends\nR1 a b 1k', '.subckt dut a b\nR1 a b 1k\n.ends wrong']):
            path=self.root/f'bad-{index}.cir';path.write_text(text,encoding='utf8')
            out=self.root/f'bad-{index}.json'
            result=self.native('import-spice',path,out,expected=1)
            self.assertIn('NATIVE_SPICE_IMPORT_FAILED',result.stderr)
            self.assertFalse(out.exists())

    def test_existing_model_polarity_overrides_its_spelling(self):
        path=self.root/'declared.cir'
        path.write_text('* x\n.model NMOS pmos\nM1 d g s b NMOS\n.end',encoding='utf8')
        out=self.root/'declared.json'
        report=json.loads(self.native('import-spice',path,out).stdout)
        snap=self.root/'declared-snapshot.json'
        self.native('snapshot',out,snap)
        project=json.loads(snap.read_text(encoding='utf8'))['project']
        self.assertEqual(project['documents'][0]['instances'][0]['symbolId'],'pmos')
        self.assertEqual(report['compatibility']['changes'],[])

    def test_named_opamps_roundtrip_through_real_layout_preserves_hidden_supplies(self):
        before=sha256(self.project.read_bytes()).hexdigest()
        args=Namespace(project_root=self.target,
            engine_root=Path(os.environ.get('NETLIST2SCHEMATIC_ENGINE_ROOT',REPOSITORY_ROOT)),
            input=self.project,out_dir=self.root/'layout',runtime=RUNTIME,node=Path(self.node),
            document_id=None,cell=None,engine_seconds=60,iterations=0,seed=17)
        with redirect_stdout(StringIO()):
            report=run(args)
        self.assertEqual(report['status'],'valid',report)
        self.assertEqual(sha256(self.project.read_bytes()).hexdigest(),before)
        self.assertTrue(report['validation']['electricalEquivalent'])
        self.assertTrue(report['validation']['roundtripVerified'])
        self.assertTrue(report['validation']['modelParametersPreserved'])
        self.assertEqual(report['validation']['visibleSupplies']['remainingSupplyGuides'],0)


if __name__=='__main__':unittest.main()
