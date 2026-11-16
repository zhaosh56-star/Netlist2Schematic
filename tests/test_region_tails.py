"""Exclusive feeds move locally without touching their active hosts or axes."""
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import random
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest import TestCase,main
from unittest.mock import patch

from netlist2schematic.layout.structured_placement import StructuredPlacement,par
from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.plugins.region.region_tails import compile_tails,refine_region_tails
from netlist2schematic.plugins.region.region_layout import measure_regions
from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
from netlist2schematic.routing.routing_rules import build_scene,RoutingOptions
from netlist2schematic.routing.sa_routing import validate_final_routes
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from tests.test_region_placement import region_seed
from tests.test_region_layout import comparator,mos


def current_case():
    devices=dict(M1=mos('d1','g1','tail',bulk='0'),M2=mos('d2','g2','tail',bulk='0'),
                 I1=dict(cls='ISRC',type='I',p='tail',n='0',value='1m'))
    doc=to_document(devices);comps,nets=doc['components'],doc['nets']
    index={c['id']:i for i,c in enumerate(comps)}
    fixed={index['M1']:(1,0),index['M2']:(1,2)}
    pl=StructuredPlacement(comps,random.Random(17),[1,1,4],fixed_cells=fixed,cols=3)
    pl.column_x=[64,188,312];pl.row_y=[64,190,316,442,568]
    pl.blocks=[[index['M1'],index['M2']],[index['I1']]]
    pl.set_state([(1,0),(1,2),(4,0)],[0,0,0])
    # Emulate the fixed-row part of a flexible seed. Only the exclusive source
    # may receive a new final display row; active host rows remain fixed.
    pl.fixed_rows={i:r for i,(r,c) in enumerate(pl.cell_of)}
    plan=dict(supported=True,signature='exclusive-three-terminal-tail',
        power_interfaces=['0'],ownership={n:'input' for n in index},
        regions=[dict(id='input',kind='folded_input',members=list(index),children=[])],
        series_axes=[],junctions=[dict(kind='shared_tail',members=['M1','M2'],tail=['I1'],
                                      tail_pins={'I1':'+'},net='tail')])
    metadata={'layout_regions':dict(applied=True,plan=plan,initialization={'trials':0,'elapsed_seconds':0})}
    return pl,comps,nets,index,metadata


def metadata_for(pl,plan):
    return {'layout_regions':dict(applied=True,plan=plan,initialization=deepcopy(pl.region_initialization))}


def state(pl):
    return deepcopy((pl.cell_of,pl.orient,pl.row_of,pl.comp_at,pl.column_x,pl.row_y,
                     getattr(pl,'offset_x',None),getattr(pl,'fixed_rows',None),pl.rng.getstate()))


class RegionTailTests(TestCase):
    def test_current_source_moves_below_pair_and_centers_actual_pole(self):
        pl,c,n,i,m=current_case();before=state(pl)
        with patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('A*')):
            trial,out=refine_region_tails(par,pl,c,n,i,m)
        self.assertTrue(out['layout_regions']['tail_refinement']['applied'])
        self.assertEqual(trial.cell_of[i['I1']][0],2)
        self.assertEqual(trial.fixed_rows[i['I1']],2)
        self.assertEqual(trial.row_of,[r for r,_ in trial.cell_of])
        center=sum(trial.pin_world(i[name],'S')[0] for name in ('M1','M2'))/2
        self.assertEqual(trial.pin_world(i['I1'],'+')[0],center)
        self.assertEqual(trial.pin_dir(i['I1'],'+'),'north')
        for name in ('M1','M2'):
            self.assertEqual(trial.pos_of(i[name]),pl.pos_of(i[name]))
            self.assertEqual(trial.orient[i[name]],pl.orient[i[name]])
        self.assertEqual((trial.column_x,trial.row_y),(pl.column_x,pl.row_y))
        self.assertEqual(state(pl),before)
        self.assertEqual(_static_faults(par,trial,n,i,RoutingOptions(),None)['count'],0)

    def test_mos_tail_keeps_row_orientation_and_centers_drain_without_moving_core(self):
        pl,c,n,i,plan,_,_,_,_=region_seed(comparator)
        before=state(pl)
        trial,m=refine_region_tails(par,pl,c,n,i,metadata_for(pl,plan))
        self.assertTrue(m['layout_regions']['tail_refinement']['applied'])
        tail=i['tail']
        self.assertEqual(trial.cell_of[tail][0],pl.cell_of[tail][0])
        self.assertEqual(trial.orient[tail],pl.orient[tail])
        self.assertEqual(m['layout_regions']['metrics']['junction_error'],0)
        for j in range(len(c)):
            if j!=tail:self.assertEqual((trial.pos_of(j),trial.orient[j]),(pl.pos_of(j),pl.orient[j]))
        self.assertEqual(state(pl),before)

    def test_fixed_source_remains_fixed_and_is_recorded(self):
        pl,c,n,i,m=current_case();pl.fixed.add(i['I1'])
        trial,out=refine_region_tails(par,pl,c,n,i,m)
        self.assertIs(trial,pl)
        report=out['layout_regions']['tail_refinement']
        self.assertFalse(report['applied'])
        self.assertIn(dict(source='I1',reason='explicitly_fixed'),report['skipped'])

    def test_shared_or_voltage_source_does_not_gain_tail_freedom(self):
        for mode in ('tap','voltage','unknown_pin','duplicate_claim'):
            pl,c,n,i,m=current_case()
            if mode=='tap':n['tail'].append(('M2','G'))
            elif mode=='voltage':c[i['I1']]['symbol']='voltage-source'
            elif mode=='unknown_pin':m['layout_regions']['plan']['junctions'][0]['tail_pins']['I1']='wrong'
            else:
                a=deepcopy(m['layout_regions']['plan']['junctions'][0]);a['members']=['M1','M1']
                m['layout_regions']['plan']['junctions'].append(a)
            groups,_=compile_tails(c,n,i,m['layout_regions']['plan'])
            if mode=='duplicate_claim':
                # The invalid second claim cannot override the port-proven one.
                self.assertEqual(len(groups),1)
            else:
                self.assertEqual(groups,[])
                trial,out=refine_region_tails(par,pl,c,n,i,m)
                self.assertIs(trial,pl)
                report=out['layout_regions']['tail_refinement']
                self.assertEqual(report['applicability'],'not_applicable')
                self.assertTrue(report['skipped'])

    def test_exhausted_shared_time_or_trial_quota_cannot_restart_a_budget(self):
        for used in ({'elapsed_seconds':1,'trials':0},{'elapsed_seconds':0,'trials':128}):
            pl,c,n,i,m=current_case();m['layout_regions']['initialization'].update(used)
            trial,out=refine_region_tails(par,pl,c,n,i,m)
            self.assertIs(trial,pl)
            report=out['layout_regions']['tail_refinement']
            self.assertEqual(report['candidates'],0)
            self.assertEqual(report['stop_reason'],'local_budget')
            self.assertFalse(report['applied'])

    def test_static_rejection_and_timeout_roll_back_whole_proposal(self):
        for effect in (False,RoutingFailure('expired',{'reason':'quality_time_budget'})):
            pl,c,n,i,m=current_case();before=state(pl)
            with patch('netlist2schematic.plugins.region.region_tails._static_safe',
                       **({'side_effect':effect} if isinstance(effect,Exception) else {'return_value':effect})):
                trial,out=refine_region_tails(par,pl,c,n,i,m)
            self.assertIs(trial,pl)
            self.assertFalse(out['layout_regions']['tail_refinement']['applied'])
            self.assertEqual(state(pl),before)

    def test_reapplication_is_stable_and_budget_is_accumulated(self):
        pl,c,n,i,m=current_case()
        first,meta=refine_region_tails(par,pl,c,n,i,m)
        budget=deepcopy(meta['layout_regions']['local_budget'])
        second,out=refine_region_tails(par,first,c,n,i,meta)
        self.assertEqual(state(first),state(second))
        self.assertFalse(out['layout_regions']['tail_refinement']['applied'])
        self.assertGreaterEqual(out['layout_regions']['local_budget']['elapsed_seconds'],budget['elapsed_seconds'])
        self.assertGreaterEqual(out['layout_regions']['local_budget']['candidates'],budget['candidates'])

    def test_mirrored_real_geometry_has_mirrored_center_and_same_source_distance(self):
        outcomes=[]
        for mirrored in (False,True):
            pl,c,n,i,m=current_case()
            if mirrored:
                cells=[(r,2-col) for r,col in pl.cell_of]
                pl.set_state(cells,[1,1,1])
                pl.fixed_cells={j:cells[j] for j in pl.fixed_cells}
            trial,out=refine_region_tails(par,pl,c,n,i,m)
            report=out['layout_regions']['tail_refinement']
            self.assertTrue(report['applied'])
            self.assertEqual(out['layout_regions']['metrics']['junction_error'],0)
            outcomes.append((trial.pos_of(i['I1']),report['actions'][0]['after']['source_distance']))
        self.assertEqual(outcomes[0][0][0]+outcomes[1][0][0],2*188)
        self.assertEqual(outcomes[0][0][1],outcomes[1][0][1])
        self.assertEqual(outcomes[0][1],outcomes[1][1])

    def test_instance_net_rename_and_component_reordering_preserve_port_based_motion(self):
        pl,c,n,index,m=current_case()
        names={name:'unit'+str(99-j) for name,j in index.items()}
        wires={net:'wire'+str(j) for j,net in enumerate(sorted(n)) if net!='0'}
        old_cells=dict(zip((x['id'] for x in c),pl.cell_of))
        old_orient=dict(zip((x['id'] for x in c),pl.orient))
        c=[dict(comp,id=names[comp['id']],nodes={p:wires.get(net,net) for p,net in comp['nodes'].items()}) for comp in reversed(c)]
        n={wires.get(net,net):[(names[name],pin) for name,pin in reversed(ports)] for net,ports in n.items()}
        idx={comp['id']:j for j,comp in enumerate(c)}
        pl.comps=c
        inverse={new:old for old,new in names.items()}
        pl.set_state([old_cells[inverse[comp['id']]] for comp in c],[old_orient[inverse[comp['id']]] for comp in c])
        pl.row_of=[r for r,_ in pl.cell_of]
        pl.fixed={idx[names['M1']],idx[names['M2']]}
        pl.fixed_cells={j:pl.cell_of[j] for j in pl.fixed}
        pl.fixed_rows={j:r for j,(r,col) in enumerate(pl.cell_of)}
        pl.blocks=[[idx[names['M1']],idx[names['M2']]],[idx[names['I1']]]]
        plan=m['layout_regions']['plan']
        plan['ownership']={names[name]:owner for name,owner in plan['ownership'].items()}
        plan['regions'][0]['members']=list(idx)
        junction=plan['junctions'][0]
        junction.update(members=[names['M1'],names['M2']],tail=[names['I1']],
                        tail_pins={names['I1']:'+'},net=wires['tail'])
        trial,out=refine_region_tails(par,pl,c,n,idx,m)
        self.assertTrue(out['layout_regions']['tail_refinement']['applied'])
        self.assertEqual(out['layout_regions']['metrics']['junction_error'],0)
        self.assertEqual(trial.cell_of[idx[names['I1']]][0],2)

    def test_pm_pair_feed_moves_to_north_with_real_negative_pole_facing_hosts(self):
        from tests.test_region_current_feed import current_fed
        pl,c,n,i,plan,_,_,_,_=region_seed(current_fed)
        trial,out=refine_region_tails(par,pl,c,n,i,metadata_for(pl,plan))
        self.assertTrue(out['layout_regions']['tail_refinement']['applied'])
        source=i['supply']
        hosts=[trial.pin_world(i[name],'S') for name in ('ia','ib')]
        self.assertLess(trial.pos_of(source)[1],sum(p[1] for p in hosts)/2)
        self.assertEqual(trial.pin_dir(source,'-'),'south')
        self.assertEqual(c[source]['nodes'],pl.comps[source]['nodes'])

    def test_refined_source_full_connectivity_and_saved_redraw_with_real_geometry(self):
        from netlist2schematic.flow.run_flow import _finish,_saved_positions
        from netlist2schematic.common.sa_saved import load_placement
        from netlist2schematic.rendering.render_clean import render
        pl,c,n,i,m=current_case()
        trial,metadata=refine_region_tails(par,pl,c,n,i,m)
        scene=build_scene(par,n,trial,i)
        routed={net:list(segments) for net,segments in scene.fixed_segments.items() if net not in scene.power_nets}
        pins=scene.pins['tail']
        hosts=[p for p in pins if p.component!=i['I1']]
        source=next(p for p in pins if p.component==i['I1'])
        y=round((max(p.escape[1] for p in hosts)+source.escape[1])/4)*2
        for pin in pins:routed['tail'].append((pin.escape,(pin.escape[0],y)))
        routed['tail'].append(((min(p.escape[0] for p in pins),y),(max(p.escape[0] for p in pins),y)))
        checked=validate_final_routes(par,n,trial,i,list(routed.items()))
        self.assertTrue(checked[2]['valid'])
        with TemporaryDirectory(prefix='region-tail-save-') as folder:
            root=Path(folder);source_path=root/'source.cir'
            source_path.write_text('.model NMOS nmos\nM1 d1 g1 tail 0 NMOS\nM2 d2 g2 tail 0 NMOS\nI1 tail 0 1m\n.end\n',encoding='utf8')
            data=dict(metadata,source_netlist=str(source_path),source_cell=None,mode='layered',structs=[],
                placement=_saved_positions(trial,c),layout_grid=dict(column_x=trial.column_x,row_y=trial.row_y))
            with (patch('netlist2schematic.flow.run_flow.final_route',side_effect=AssertionError('A*')),
                  redirect_stdout(io.StringIO())):
                _finish(data,par,trial,c,n,i,'source',root/'out',routed_result=checked,simplify_routes='off')
                saved=root/'out/source-placement.json'
                restored=load_placement(saved)
                render(saved,root/'redraw.svg')
            self.assertEqual(restored[0]['placement'],data['placement'])
            self.assertEqual(restored[3].pin_world(restored[6]['I1'],'+'),trial.pin_world(i['I1'],'+'))
            self.assertEqual(json.loads(saved.read_text('utf8'))['metrics'],checked[1])


if __name__=='__main__':
    main()
