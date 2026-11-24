"""Common MOS/BJT motifs must retain real pins, family and layout protection."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.recognition.transistor_topology import channel_nets, channel_pins, common_kind
from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.layout import structured_placement as sp
from netlist2schematic.layout.structure_hierarchy import hierarchy_report, compile_hierarchy
from netlist2schematic.plugins.layout_shared.layout_relations import build_layout_relations
from netlist2schematic.plugins.layout_shared.layout_evidence import build_layout_evidence
from netlist2schematic.plugins.columns.column_layout import build_column_plan
from netlist2schematic.plugins.symmetry.symmetry_layout import build_symmetry_groups
from netlist2schematic.flow.run_flow import run
from netlist2schematic.common.sa_common import read_json, write_json
from netlist2schematic.common.sa_saved import load_placement, saved_routing_options
from netlist2schematic.routing.sa_routing import validate_final_routes
from netlist2schematic.rendering.render_clean import render


def transistor(drain, control, source, kind):
    if kind in ('NMOS', 'PMOS'):
        return dict(cls='MOS', type=kind, d=drain, g=control, s=source, bulk=source, model=kind)
    return dict(cls='BJT', type=kind, c=drain, b=control, e=source, model=kind)


def pair(kind):
    return dict(a=transistor('out_a', 'in_a', 'tail', kind),
                b=transistor('out_b', 'in_b', 'tail', kind),
                current=dict(cls='ISRC', type='I', p='tail', n='0', value='1m'))


def motifs(devices):
    return [r for r in analyze_structures(devices) if r['kind'] in ('differential_pair', 'current_mirror')]


class SharedMotifTests(unittest.TestCase):
    def test_all_four_polarities_use_the_same_pair_kind_and_tail_roles(self):
        for kind in ('NMOS', 'PMOS', 'NPN', 'PNP'):
            with self.subTest(kind=kind):
                devices = pair(kind)
                original = deepcopy(devices)
                record, = motifs(devices)
                self.assertEqual('differential_pair', record['kind'])
                self.assertEqual({'a', 'b'}, set(record['devices']))
                self.assertEqual(['current'], record['roles']['tail'])
                self.assertEqual('tail', record['nets']['source'])
                self.assertEqual(devices, original)
                self.assertEqual('BJT' if kind in ('NPN','PNP') else 'MOS', record.get('family','MOS'))
                relation, = [r for r in build_layout_relations(devices) if r['kind']=='differential_pair']
                self.assertTrue(relation['rigid'])
                self.assertEqual(['current'], relation['roles']['exclusive_tails'])

    def test_real_ports_are_never_renamed_in_the_devices(self):
        bjt = transistor('output', 'input', 'tail', 'PNP')
        self.assertEqual(('C','B','E'), channel_pins(bjt))
        self.assertEqual(('output','input','tail'), channel_nets(bjt))
        self.assertEqual({'c','b','e'}, {f for f in bjt if len(f)==1})

    def test_mirrors_keep_reference_and_all_outputs_for_both_families(self):
        for kind in ('NMOS','PMOS','NPN','PNP'):
            devices = dict(ref=transistor('bias','bias','0',kind),
                           out1=transistor('one','bias','0',kind),
                           out2=transistor('two','bias','0',kind))
            mirror, = [r for r in motifs(devices) if r['kind']=='current_mirror']
            self.assertEqual('ref', mirror['roles']['reference'])
            self.assertEqual({'out1','out2'}, set(mirror['roles']['outputs']))
            relation, = [r for r in build_layout_relations(devices) if r['kind']=='current_mirror']
            self.assertFalse(relation['rigid'])
            self.assertEqual([], relation['layout']['pairs'])

    def test_shared_source_bias_or_supply_alone_does_not_establish_a_function(self):
        for kind in ('NMOS','NPN'):
            devices = pair(kind)
            del devices['current']
            self.assertFalse(motifs(devices))
            for source in ('0','vdd'):
                devices = dict(a=transistor('one','in1',source,kind),b=transistor('two','in2',source,kind))
                self.assertFalse(motifs(devices))
            devices = dict(a=transistor('one','bias','0',kind),b=transistor('two','bias','0',kind))
            self.assertFalse(motifs(devices))

    def test_mixed_family_and_mixed_polarity_are_never_paired(self):
        for a,b in (('NMOS','NPN'),('PMOS','PNP'),('NPN','PNP'),('NMOS','PMOS')):
            devices = pair(a)
            devices['b'] = transistor('out_b','in_b','tail',b)
            self.assertFalse(motifs(devices))
            devices = dict(ref=transistor('bias','bias','0',a),out=transistor('output','bias','0',b))
            self.assertFalse(any(r['kind']=='current_mirror' for r in motifs(devices)))

    def test_cross_coupling_and_series_roles_apply_to_bipolar_devices(self):
        for kind in ('NPN','PNP'):
            devices = dict(a=transistor('one','two','0',kind), b=transistor('two','one','0',kind))
            cross, = [r for r in analyze_structures(devices) if r['kind']=='cross_coupled']
            self.assertEqual({'a','b'},set(cross['devices']))
            self.assertEqual('BJT',cross['family'])
            devices = dict(a=transistor('top','ctrl1','mid',kind),b=transistor('mid','ctrl2','bottom',kind))
            branch, = [r for r in analyze_structures(devices) if r['kind']=='series_branch']
            self.assertEqual(('a','b') if kind=='NPN' else ('b','a'),
                             (branch['roles']['upper'],branch['roles']['lower']))
            self.assertEqual('relation',branch['confidence'])

    def test_renaming_order_and_polarity_dual_preserve_membership(self):
        for kind in ('NPN','PNP'):
            devices=pair(kind);expected={frozenset(r['devices']) for r in motifs(devices)}
            rename={'a':'random_17','b':'element_402','current':'tail_source'}
            inverse={v:k for k,v in rename.items()}
            for seed in range(8):
                order=list(devices);random.Random(seed).shuffle(order)
                changed={rename[n]:deepcopy(devices[n]) for n in order}
                for d in changed.values():
                    for field in ('c','b','e','p','n'):
                        if field in d and d[field] not in ('0','vdd'):d[field]='renamed_'+d[field]
                actual={frozenset(inverse[n] for n in r['devices']) for r in motifs(changed)}
                self.assertEqual(expected,actual)

    def test_calibrated_evidence_uses_one_common_leaf_instead_of_a_duplicate_bjt_leaf(self):
        for kind in ('NPN','PNP'):
            devices=pair(kind);report=build_layout_evidence(devices)
            leaves=[r for r in report['relations'] if common_kind(r['kind'])=='differential_pair']
            self.assertEqual(1,len(leaves))
            self.assertEqual('differential_pair',leaves[0]['kind'])
            self.assertEqual('BJT',leaves[0]['family'])
            groups=build_symmetry_groups(to_document(devices)['components'])
            self.assertEqual(1,len(groups))
            self.assertEqual('differential_pair',groups[0]['kind'])


class SharedPlacementTests(unittest.TestCase):
    def test_attachment_reuses_an_identical_leaf_but_rejects_partial_overlap(self):
        from netlist2schematic.plugins.attachment.attachment_input_blocks import owner_blocks
        comps=[{'id':n} for n in ('a','b','c','d')]
        blocks=[[0,1],[2],[3]]
        model={'pairs':[{'members':['a','b'],'kind':'differential_pair','family':'BJT'}]}
        before=deepcopy((blocks,model))
        result=owner_blocks(blocks,comps,model)
        self.assertEqual({frozenset(('a','b')),frozenset(('c',)),frozenset(('d',))},
                         {frozenset(g) for g in result})
        self.assertEqual(before,(blocks,model))
        conflicting={'pairs':[{'members':['b','c'],'kind':'differential_pair','family':'BJT'}]}
        self.assertIsNone(owner_blocks(blocks,comps,conflicting))

    def seed(self, kind='NPN', legacy_tag=False):
        devices=pair(kind);devices['other']=transistor('extra','other_input','0',kind)
        doc=to_document(devices);comps,nets=doc['components'],doc['nets']
        index={c['id']:i for i,c in enumerate(comps)};records=analyze_structures(devices)
        if legacy_tag:
            for record in records:
                if record['kind']=='differential_pair':record['kind']='bjt_differential_pair'
        levels=sp.dl.compute_levels(devices)
        pl,align,pairs=sp._initial_placement(devices,comps,nets,index,levels,{},records,71,1,
            row_override=[2 if c['id']=='current' else 1 for c in comps],passive_layout='fixed')
        return pl,comps,nets,index,records,align,pairs

    def test_bipolar_pair_is_a_protected_sa_block_in_both_old_and_new_schema(self):
        for legacy in (False,True):
            pl,comps,nets,index,records,alignment,pairs=self.seed(legacy_tag=legacy)
            expected={index['a'],index['b']}
            self.assertIn(expected,[set(b) for b in pl.blocks])
            self.assertEqual(1,len(pairs))
            self.assertTrue(any(set(g['members'])=={'a','b'} for g in pl.structure_hierarchy['groups']))
            original_rows=list(pl.row_of)
            for step in range(300):
                before=list(pl.cell_of),list(pl.orient);old=pl.propose()
                self.assertTrue(pl.blocks_contiguous())
                self.assertEqual(0,hierarchy_report(pl)['metrics']['interleaving'])
                self.assertEqual(original_rows,pl.row_of)
                self.assertTrue(all(pl.orient[i] in (0,1) for n,i in index.items() if n!='current'))
                if old is not None and step%2:pl.set_state(*before)
            with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('No A* in SA')):
                sp._anneal(pl,nets,index,alignment,pairs,100,None)
            self.assertTrue(pl.blocks_contiguous())

    def test_input_emitter_is_a_column_boundary_using_actual_bjt_ports(self):
        pl,comps,nets,index,records,_,_=self.seed()
        plan=build_column_plan(sp.par,pl,comps,nets,index,records)
        self.assertFalse(any({'a','b'} <= set(c['members']) for c in plan['columns']))
        relation=deepcopy(next(r for r in records if r['kind']=='differential_pair'))
        relation['kind']='bjt_differential_pair'
        self.assertEqual(plan,build_column_plan(sp.par,pl,comps,nets,index,[relation]))


class BipolarFlowTests(unittest.TestCase):
    def test_default_candidates_save_real_bjt_pins_and_legacy_json_redraw_keeps_geometry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'input.cir'
            source.write_text('.model nq NPN\nQ1 oa ip tail nq\nQ2 ob im tail nq\n'
                'I1 tail 0 1m\nR1 vdd oa 1k\nR2 vdd ob 1k\n.end\n',encoding='utf8')
            original=source.read_bytes()
            with redirect_stdout(io.StringIO()):
                data=run(source,iterations=0,out_dir=root/'new',quality_seconds=8,fallback='off',simplify_routes='off')
            self.assertTrue(data['validation']['valid'])
            self.assertTrue((root/'new/input-formal.svg').is_file())
            self.assertEqual({'2','3'},{str(c['candidate_id']) for c in data['quality']['candidates']})
            record,=[r for r in data['structs'] if r['kind']=='differential_pair']
            self.assertEqual({'Q1','Q2'},set(record['devices']))
            saved=root/'new/input-placement.json'
            before=read_json(saved);legacy=deepcopy(before)
            for row in legacy['structs']:
                if row['kind']=='differential_pair':row['kind']='bjt_differential_pair'
            old=root/'legacy.json';write_json(old,legacy)
            loaded,_,par,pl,comps,nets,index=load_placement(old)
            for c in comps:
                if c['id'] in ('Q1','Q2'):self.assertEqual({'C','B','E'},set(c['nodes']))
            _,metrics,validation=validate_final_routes(par,nets,pl,index,list(loaded['routes'].items()),
                options=saved_routing_options(loaded))
            self.assertEqual(before['placement'],loaded['placement'])
            self.assertEqual(before['metrics'],metrics);self.assertTrue(validation['valid'])
            with redirect_stdout(io.StringIO()):render(old,root/'legacy.svg')
            self.assertEqual((root/'new/input-clean.svg').read_bytes(),(root/'legacy.svg').read_bytes())
            self.assertEqual(original,source.read_bytes())


if __name__=='__main__':unittest.main()
