from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from integrations.auto_canvas_huyangc.bootstrap_runtime import lock_entry
from integrations.auto_canvas_huyangc.native_plan import native_routes, build_plan, attach_to_last_contact, continuation_exit
from integrations.auto_canvas_huyangc.profile import compile_snapshot, stage_engine, canonical_geometry
from integrations.auto_canvas_huyangc.runtime import run_bounded

ROOT = Path(__file__).resolve().parents[3]


def terminal(ident, name='1'):
    return dict(kind='terminal', instanceId=ident, pinName=name)


def snapshot():
    def symbol(ident, pins):
        return dict(id=ident, name=ident, viewBox=dict(x=-12,y=-24,width=24,height=48),
                    pins=[dict(name=p,at=dict(x=0,y=y),direction=d,
                               presentation=dict(visibility='visible')) for p,y,d in pins], primitives=[])
    resistor = symbol('resistor', [('1',-20,'north'),('2',20,'south')])
    ground = symbol('ground', [('0',-10,'north')])
    vdd = symbol('vdd-port', [('P',20,'south')])
    def resolved(definition):
        return dict(definition=definition,variant=None,pins=[dict(p,landing=deepcopy(p['at']))
                    for p in definition['pins']])
    doc = dict(id='document',revision=7,presentation=dict(grid=10),constraints=[],
        instances=[dict(id='R-original',symbolId='resistor',reference='R1',
            placement=dict(position=dict(x=0,y=0),rotation=0,mirror='none'),
            netlist=dict(binding=dict(kind='primitive',deviceClass='resistor'),parameters=dict(value='12k')))],
        nets=[dict(id='a',terminals=[dict(instanceId='R-original',pinName='1')]),
              dict(id='b',terminals=[dict(instanceId='R-original',pinName='2')])],
        annotations=[],routes=[],junctions=[],connectivityEvidence=[])
    return dict(protocolVersion=1,documentId='document',source=dict(path='source.json',sha256='before'),
        project=dict(id='project',documents=[doc],componentDefinitions=[]),
        symbols={'R-original':resolved(resistor)}, markerSymbols={
            'ground':dict(definition=ground,variant=None),'vdd-port':dict(definition=vdd,variant=None)},
        logicalNets=[dict(id='a',baseNetIds=['a'],name='Input',conflicts=[]),
                     dict(id='b',baseNetIds=['b'],name='Output',conflicts=[])])


class WireConversionTests(unittest.TestCase):
    def routes(self, lines, contacts):
        return native_routes('net',lines,contacts)

    def test_straight_has_terminal_endpoints_without_extra_junctions(self):
        edits=self.routes([((0,0),(100,0))],{(0,0):[terminal('a')],(100,0):[terminal('b')]})
        self.assertEqual([e['kind'] for e in edits],['set_route_path'])
        route=edits[0]['route']
        self.assertEqual(route['start'],terminal('a'))
        self.assertEqual(route['legs'][-1]['to']['endpoint'],terminal('b'))

    def test_l_shape_is_one_native_route_with_a_bend(self):
        edits=self.routes([((0,0),(100,0)),((100,0),(100,50))],
                          {(0,0):[terminal('a')],(100,50):[terminal('b')]})
        self.assertEqual(len(edits),1)
        self.assertEqual(edits[0]['route']['legs'][0]['to']['position'],dict(x=100,y=0))

    def test_true_branch_has_one_junction_and_three_paths(self):
        edits=self.routes([((0,0),(100,0)),((50,0),(50,50))],
                          {(0,0):[terminal('a')],(100,0):[terminal('b')],(50,50):[terminal('c')]})
        self.assertEqual(sum(e['kind']=='add_junction' for e in edits),1)
        self.assertEqual(sum(e['kind']=='set_route_path' for e in edits),3)

    def test_collinear_slicing_and_duplicate_intervals_do_not_change_routes(self):
        contacts={(0,0):[terminal('a')],(100,0):[terminal('b')]}
        direct=self.routes([((0,0),(100,0))],contacts)
        split=self.routes([((0,0),(30,0)),((30,0),(100,0)),((0,0),(70,0))],contacts)
        self.assertEqual(direct,split)

    def test_foreign_net_crossing_is_not_a_native_junction(self):
        a=self.routes([((0,0),(100,0))],{(0,0):[terminal('a')],(100,0):[terminal('b')]})
        b=native_routes('other',[((50,-50),(50,50))],{(50,-50):[terminal('c')],(50,50):[terminal('d')]})
        self.assertTrue(all(e['kind']=='set_route_path' for e in a+b))

    def test_shared_terminal_contact_uses_electrical_fact_not_zero_length_route(self):
        edits=self.routes([((0,0),(100,0))],{(0,0):[terminal('a'),terminal('b')],(100,0):[terminal('c')]})
        self.assertEqual(sum(e['kind']=='connect_endpoints' for e in edits),1)
        self.assertEqual(sum(e['kind']=='set_route_path' for e in edits),1)

    def test_missing_contact_diagonal_and_fractional_coordinates_are_rejected(self):
        for lines,contacts in [([((0,0),(100,0))],{(50,10):[terminal('a')]}),
            ([((0,0),(10,10))],{}),([((0,0),(10.5,0))],{})]:
            with self.subTest(lines=lines),self.assertRaises(ValueError):
                self.routes(lines,contacts)

    def test_empty_terminal_free_loop_is_rejected(self):
        with self.assertRaises(ValueError):
            self.routes([((0,0),(10,0)),((10,0),(10,10)),((10,10),(0,10)),((0,10),(0,0))],{})

    def test_port_branch_reuses_last_same_net_contact_in_four_directions(self):
        tree=[((0,0),(0,30)),((0,20),(50,20))]
        path=[(0,30),(0,24),(24,24),(24,-10)]
        for rotation in (0,90,180,270):
            def pose(p):
                x,y=p
                for _ in range(rotation//90):x,y=-y,x
                return (x+100,y-200)
            with self.subTest(rotation=rotation):
                result=attach_to_last_contact([pose(p) for p in path],
                    [[pose(a),pose(b)] for a,b in tree])
                self.assertEqual(result,[pose((24,20)),pose((24,-10))])

    def test_compatibility_escape_preserves_existing_terminal_stub(self):
        for direction in ((1,0),(-1,0),(0,1),(0,-1)):
            end=(direction[0]*18,direction[1]*18)
            self.assertEqual(continuation_exit((0,0),direction,[[(0,0),end]]),end)

    def test_collinear_tree_reuse_does_not_depend_on_slicing(self):
        path=[(0,0),(40,0),(40,30)]
        self.assertEqual(attach_to_last_contact(path,[[(0,0),(60,0)]]),
                         attach_to_last_contact(path,[[(0,0),(20,0)],[(20,0),(60,0)]]))


class NativeProfileTests(unittest.TestCase):
    def test_id_mapping_parameters_and_original_snapshot_are_preserved(self):
        source=snapshot();before=deepcopy(source)
        profile=compile_snapshot(source)
        self.assertEqual(source,before)
        self.assertEqual(profile['net_names'],{'a':'input','b':'output'})
        self.assertEqual(profile['components']['R1']['instance_id'],'R-original')
        self.assertIn('12k',profile['netlist'])

    def test_unclassified_dc_driver_is_not_renamed_to_a_supply(self):
        source=snapshot()
        source['logicalNets'][0]['name']=None
        source['project']['documents'][0]['connectivityEvidence']=[
            dict(kind='net-name-hint',netId='a',sourceName='net12')]
        source['netRoles']={'a':'supply'}
        profile=compile_snapshot(source)
        self.assertEqual(profile['net_names']['a'],'net12')
        source['logicalNets'][0]['powerDomain']='vdd'
        self.assertEqual(compile_snapshot(source)['net_names']['a'],'vdd')

    def test_ground_formal_keeps_its_interface_symbol_and_domain(self):
        source=snapshot();doc=source['project']['documents'][0]
        source['logicalNets'][0].update(name='0',powerDomain='ground')
        pin=dict(name='P',at=dict(x=0,y=0),landing=dict(x=0,y=0),direction='east')
        shape=dict(id='port',viewBox=dict(x=-14,y=-7,width=18,height=14),
                   pins=[{k:v for k,v in pin.items() if k!='landing'}],primitives=[])
        source['symbols']['formal']=dict(definition=shape,pins=[pin],variant=None)
        doc['instances'].append(dict(id='formal',symbolId='port',placement=None))
        doc['nets'][0]['terminals'].append(dict(instanceId='formal',pinName='P'))
        doc['netlist']=dict(name='cell',formalParameters=[],terminals=[dict(
            id='formal-decl',name='gnda',direction='passive',netId='a',interfaceInstanceIds=['formal'])])
        profile=compile_snapshot(source)
        wire=dict(validation=dict(valid=True),metrics={},components=[dict(instance_id='R-original',
            position=dict(x=100,y=100),rotation=0,mirror='none',
            pins=[dict(name='1',net='0',at=[100,80]),dict(name='2',net='output',at=[100,120])])],
            power=[dict(component_id='R1',pin='1',net='0',symbol='ground',anchor=[100,160],
                        origin=[100,170],segments=[[[100,80],[100,160]]])],routes={})
        plan=build_plan(source,profile,wire)
        self.assertEqual(plan['symbolOverrides'],[])
        self.assertTrue(any(e['kind']=='place_instance' and e['instanceId']=='formal' for e in plan['edits']))
        self.assertFalse(any(e['kind']=='upsert_connectivity_evidence' for e in plan['edits']))

    def test_ground_and_negative_supply_are_not_merged(self):
        source=snapshot();source['logicalNets'][0]['name']='0';source['logicalNets'][1]['name']='VEE'
        p=compile_snapshot(source)
        self.assertEqual(p['net_names'],{'a':'0','b':'vee'})

    def test_authored_coordinates_and_netlist_order_are_not_layout_input(self):
        source=snapshot();before=compile_snapshot(source)
        source['project']['documents'][0]['instances'][0]['placement']['position']=dict(x=10000,y=-12000)
        source['project']['documents'][0]['nets'].reverse()
        after=compile_snapshot(source)
        self.assertEqual(before['netlist'],after['netlist'])
        self.assertEqual(before['definitions'],after['definitions'])

    def test_repeated_non_electrical_hints_do_not_merge_native_nets(self):
        source=snapshot()
        for group in source['logicalNets']:group.pop('name')
        source['project']['documents'][0]['connectivityEvidence']=[
            dict(kind='net-name-hint',netId='a',sourceName='same'),
            dict(kind='net-name-hint',netId='b',sourceName='SAME')]
        p=compile_snapshot(source)
        self.assertEqual(len(set(p['net_names'].values())),2)

    def test_negative_cap_uses_native_rotated_pin_offset(self):
        source=snapshot();source['logicalNets'][0]['name']='VEE'
        profile=compile_snapshot(source)
        wire=dict(validation=dict(valid=True),metrics={},components=[dict(instance_id='R-original',
            position=dict(x=100,y=100),rotation=0,mirror='none',
            pins=[dict(name='1',net='vee',at=[100,80]),dict(name='2',net='output',at=[100,120])])],
            power=[dict(component_id='R1',pin='1',net='vee',symbol='vee-port',anchor=[100,50],
                        origin=[100,50],segments=[[[100,80],[100,50]]])],routes={})
        plan=build_plan(source,profile,wire)
        marker=next(e['instance'] for e in plan['edits'] if e['kind']=='add_instance')
        self.assertEqual(marker['placement']['rotation'],180)
        self.assertEqual(marker['placement']['position'],dict(x=100,y=70))
        self.assertEqual(plan['powerMarkers'][0]['style'],'negative-cap')

    def test_duplicate_physical_membership_is_rejected(self):
        source=snapshot()
        source['project']['documents'][0]['nets'][1]['terminals'].append(dict(instanceId='R-original',pinName='1'))
        with self.assertRaisesRegex(ValueError,'more than one'):
            compile_snapshot(source)

    def test_no_invented_layout_for_constraints_unknown_devices_or_bus_members(self):
        for change in ('fixed','unknown','bus'):
            source=snapshot();doc=source['project']['documents'][0]
            if change=='fixed':doc['constraints']=[dict(kind='fixed')]
            if change=='unknown':doc['instances'][0]['netlist']['binding']['deviceClass']='switch'
            if change=='bus':doc['nets'][0]['terminals'][0]['memberId']='member'
            with self.subTest(change=change),self.assertRaises(ValueError):
                compile_snapshot(source)

    def test_split_logical_net_and_conflicting_evidence_are_explicitly_rejected(self):
        for change in ('split','conflict'):
            source=snapshot()
            if change=='split':source['logicalNets'][0]['baseNetIds'].append('b')
            else:source['logicalNets'][0]['conflicts']=['name-conflict']
            with self.subTest(change=change),self.assertRaises(ValueError):
                compile_snapshot(source)

    def test_off_grid_contact_is_not_silently_rounded(self):
        source=snapshot();source['symbols']['R-original']['pins'][0]['landing']['x']=2
        with self.assertRaisesRegex(ValueError,'landing'):
            compile_snapshot(source)

    def test_same_type_different_footprints_are_not_silently_merged(self):
        source=snapshot();doc=source['project']['documents'][0]
        doc['instances'].append(dict(deepcopy(doc['instances'][0]),id='R-other',reference='R2'))
        source['symbols']['R-other']=deepcopy(source['symbols']['R-original'])
        source['symbols']['R-other']['definition']['viewBox']['width']=80
        with self.assertRaisesRegex(ValueError,'Heterogeneous'):
            compile_snapshot(source)

    def test_unknown_polarity_is_not_guessed_from_instance_name(self):
        source=snapshot();i=source['project']['documents'][0]['instances'][0]
        i['netlist']['binding']['deviceClass']='mos'
        with self.assertRaisesRegex(ValueError,'polarity'):
            compile_snapshot(source)

    def test_native_grid_reduces_only_as_needed_without_scaling_coordinates(self):
        source=snapshot();p=compile_snapshot(source)
        wire=dict(validation=dict(valid=True),metrics={},components=[dict(instance_id='R-original',
            position=dict(x=75,y=100),rotation=0,mirror='none',pins=[])],power=[],routes={})
        plan=build_plan(source,p,wire)
        self.assertEqual(plan['nativeGrid'],5)
        move=next(e for e in plan['edits'] if e['kind']=='move_instance')
        self.assertEqual(move['position'],dict(x=75,y=100))
        wire['components'][0]['position']['x']=80
        self.assertEqual(build_plan(source,p,wire)['nativeGrid'],10)

    def test_unverified_wire_plan_and_fractional_pose_never_export(self):
        source=snapshot();p=compile_snapshot(source)
        with self.assertRaisesRegex(ValueError,'unverified'):
            build_plan(source,p,dict(validation=dict(valid=False)))

    def test_different_native_models_keep_different_layout_model_aliases(self):
        source=snapshot();doc=source['project']['documents'][0]
        original=doc['instances'][0]
        for ident,model in [('Q-a','first'),('Q-b','second')]:
            symbol=deepcopy(source['symbols']['R-original'])
            symbol['definition']['id']='npn'
            symbol['pins']=[dict(name=p,at=dict(x=0,y=y),landing=dict(x=0,y=y),direction=d)
                for p,y,d in [('C',-20,'north'),('B',0,'west'),('E',20,'south')]]
            symbol['definition']['pins']=[{k:v for k,v in p.items() if k!='landing'} for p in symbol['pins']]
            source['symbols'][ident]=symbol
            doc['instances'].append(dict(id=ident,symbolId='npn',placement=original['placement'],
                netlist=dict(binding=dict(kind='model',deviceClass='bjt',modelName=model),parameters={})))
        profile=compile_snapshot(source)
        cards=[line for line in profile['netlist'].splitlines() if line.startswith('.model')]
        self.assertEqual(len(cards),2)
        self.assertNotEqual(cards[0].split()[1],cards[1].split()[1])


class IsolationTests(unittest.TestCase):
    def test_stage_copies_source_bytes_and_changes_only_private_resource_data(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'core';folder=Path(td)/'staged'
            (root/'netlist2schematic').mkdir(parents=True)
            code=b'# independent engine\r\nVALUE = 1\r\n'
            (root/'netlist2schematic/engine.py').write_bytes(code)
            (root/'run_flow.py').write_bytes(b'print(1)\n')
            (root/'netlist2schematic/source_map.json').write_text('{}',encoding='utf-8')
            stock=json.loads((ROOT/'symbol-geometry.json').read_text(encoding='utf-8'))
            stock['symbols']={k:v for k,v in stock['symbols'].items() if k=='resistor'}
            (root/'symbol-geometry.json').write_text(json.dumps(stock),encoding='utf-8')
            p=compile_snapshot(snapshot())
            stage_engine(root,folder,p)
            self.assertEqual((folder/'netlist2schematic/engine.py').read_bytes(),code)
            self.assertEqual((root/'netlist2schematic/engine.py').read_bytes(),code)
            geometry=json.loads((folder/'symbol-geometry.json').read_text(encoding='utf-8'))
            self.assertEqual(geometry['symbols']['resistor']['viewBox']['width'],24)
            self.assertEqual(stock['symbols']['resistor']['viewBox']['width'],20)
            with self.assertRaises(ValueError):
                stage_engine(root,folder,p)

    def test_owned_child_timeout_is_bounded(self):
        self.assertEqual(run_bounded([sys.executable,'-c','import time;time.sleep(2)'],seconds=.05,
                                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL),124)

    def test_lock_entry_uses_exact_pinned_package_and_snapshot(self):
        lock='packages:\n  "@scope/name@1.2.3":\n    resolution: {integrity: sha512-test}\n  other@1:\n\nsnapshots:\n  "@scope/name@1.2.3":\n    dependencies:\n      child: 2.0.0\n'
        self.assertIn('sha512-test',lock_entry(lock,'@scope/name@1.2.3','packages'))
        self.assertIn('child: 2.0.0',lock_entry(lock,'@scope/name@1.2.3','snapshots'))
        with self.assertRaises(ValueError):lock_entry(lock,'@scope/name@9.0.0','packages')


if __name__=='__main__':unittest.main()
