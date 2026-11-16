"""Physical signal chains retain topology, windows and original geometry gates."""
from copy import deepcopy
from math import inf
from pathlib import Path
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_attachment_ports import installed
from tests.test_passive_placement import mos,resistor
from tests.test_attachment_flow import candidate
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment.attachment_series_paths import (
    compile_paths,measure_paths,path_rank,path_trials,finish_paths,reserve_finish)
from netlist2schematic.plugins.attachment.attachment_selection import organization_rank,select_attachment_result
from netlist2schematic.layout.structured_placement import par
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.routing.orthogonal_router import RoutingFailure


def device(kind,drain,gate,source):
    if kind in ('nmos','pmos'):
        d=mos(drain,gate,source);d.update(type=kind.upper(),bulk=source);return d
    return dict(cls='BJT',type=kind.upper(),c=drain,b=gate,e=source,model=kind.upper())


def fixture(kind='nmos',renamed=False,reverse=False,long_chain=False,inside=False):
    mapping={n:n for n in ('a','b','u','v','r','c','l')}
    if renamed:mapping=dict(a='X1',b='X2',u='P1',v='P2',r='Y',c='Z',l='W')
    left,right=('left','right') if not renamed else ('z_node','a_node')
    positive=kind in ('nmos','npn')
    if positive:
        ds=dict(a=device(kind,left,'ga','0'),b=device(kind,right,'gb','0'),
            u=device(kind,'outa','bu',left),v=device(kind,'outb','bv',right))
    else:
        ds=dict(u=device(kind,left,'gu','VDD'),v=device(kind,right,'gv','VDD'),
            a=device(kind,'outa','ba',left),b=device(kind,'outb','bb',right))
    if long_chain:
        ds.update(r=resistor(left,'middle0'),c=resistor('middle0','middle1','C'),l=resistor('middle1',right,'L'))
    else:ds.update(r=resistor(left,'middle'),c=resistor('middle',right,'C'))
    cells=dict(a=(2,1),u=(1,1),b=(2,5),v=(1,5),r=(1,2),c=(1,3 if inside else 4),l=(1,3))
    ds={mapping[n]:d for n,d in ds.items()}
    if reverse:ds=dict(reversed(list(ds.items())))
    case=installed(ds,{mapping[n]:c for n,c in cells.items() if mapping[n] in ds})
    pl,_,_,index,_=case
    path=case[4]['physical_series_paths']['paths'][0]
    for k,n in enumerate(path['members']):
        # Existing attachment policy permits all four passive orientations.
        i=index[n]
        before=next(p for p,net in pl.comps[i]['nodes'].items() if net==path['nodes'][k])
        pl.orient[i]=next(o for o in pl.allowed_orientations(i)
            if next(p for p in par.GEO_DOC[pl.comps[i]['symbol']]['orientations'][o]['pins']
                if p['name']==before)['direction']==('east' if renamed else 'west'))
    if inside:
        pl.passive_local_offsets=[(0,70) if c['id'] in path['members'] else (0,0) for c in pl.comps]
    return (*case,mapping)


def state(pl):
    return deepcopy({k:v for k,v in vars(pl).items() if k!='rng'}),pl.rng.getstate()


def finish(case,answer='real',count=0,limit=128):
    pl,cs,nets,index,plan,_=case
    budget=ai.new_attachment_budget(seconds=1,max_candidates=limit)
    reserve_finish(plan,budget);budget['candidates']=count
    report=dict(candidates=0,accepted=0,actions=[])
    if answer=='real':
        out=finish_paths(par,pl,cs,nets,index,plan,RoutingOptions(),budget,perf_counter()+1,report,hpwl_limit=inf)
    else:
        with patch.object(ai,'_checked_static',return_value=answer):
            out=finish_paths(par,pl,cs,nets,index,plan,RoutingOptions(),budget,perf_counter()+1,report,hpwl_limit=inf)
    return out,budget,report


class PhysicalSeriesTests(unittest.TestCase):
    def test_MOS_BJT_and_polarity_duals_have_actual_safe_in_band_trials(self):
        for kind in ('nmos','pmos','npn','pnp'):
            pl,cs,nets,index,plan,_=fixture(kind)
            before=state(pl);diagnostic={}
            trials=list(path_trials(par,pl,cs,nets,index,plan['physical_series_paths']['paths'][0],
                RoutingOptions(),diagnostic=diagnostic))
            self.assertTrue(trials,kind);self.assertLessEqual(len(trials),12)
            self.assertEqual(1,diagnostic['scene_templates'])
            self.assertEqual(before,state(pl))
            safe=[]
            for t,a in trials:
                self.assertTrue(t.valid_state(),kind)
                self.assertEqual(pl.cell_of,t.cell_of)
                self.assertEqual((pl.column_x,pl.row_y),(t.column_x,t.row_y))
                self.assertNotIn(a['line_y'],pl.row_y)
                measurement=measure_paths(t,cs,index,plan['physical_series_paths'])
                self.assertEqual(0,measurement['outside_errors'])
                self.assertEqual(0,measurement['direction_errors'])
                self.assertEqual(0,measurement['order_errors'])
                self.assertEqual(0,measurement['pin_alignment_error'])
                safe.append(ai._checked_static(par,t,nets,index,RoutingOptions(),None))
            self.assertTrue(any(safe),kind)

    def test_three_member_RLC_keeps_original_topological_order(self):
        pl,cs,nets,index,plan,_=fixture(long_chain=True)
        trials=list(path_trials(par,pl,cs,nets,index,plan['physical_series_paths']['paths'][0],RoutingOptions()))
        self.assertTrue(trials)
        self.assertTrue(any(ai._checked_static(par,t,nets,index,RoutingOptions(),None) for t,a in trials))
        for t,a in trials:
            self.assertEqual(0,measure_paths(t,cs,index,plan['physical_series_paths'])['order_errors'])

    def test_instance_net_renaming_and_input_order_preserve_geometric_proposals(self):
        rows=[]
        for rename,reverse in ((False,False),(True,True)):
            pl,cs,nets,index,plan,names=fixture(renamed=rename,reverse=reverse)
            paths=list(path_trials(par,pl,cs,nets,index,plan['physical_series_paths']['paths'][0],RoutingOptions()))
            rows.append([{n:tuple(t.pos_of(index[names[n]])) for n in ('r','c')} for t,a in paths])
        self.assertEqual(*rows)

    def test_translation_preserves_local_offsets(self):
        pl,cs,nets,index,plan,_=fixture()
        path=plan['physical_series_paths']['paths'][0]
        one=list(path_trials(par,pl,cs,nets,index,path,RoutingOptions()))
        other=deepcopy(pl);other.column_x=[x+37 for x in pl.column_x];other.row_y=[y-53 for y in pl.row_y]
        two=list(path_trials(par,other,cs,nets,index,path,RoutingOptions()))
        self.assertEqual([t.passive_local_offsets for t,a in one],[t.passive_local_offsets for t,a in two])

    def test_horizontal_mirror_respects_real_ports_within_grid_quantization(self):
        pl,cs,nets,index,plan,_=fixture()
        path=plan['physical_series_paths']['paths'][0]
        one=next(path_trials(par,pl,cs,nets,index,path,RoutingOptions()))[0]
        other=deepcopy(pl)
        other.set_state([(r,len(pl.column_x)-1-c) for r,c in pl.cell_of],pl.orient)
        for n in ('a','b','u','v'):other.orient[index[n]]=1
        two=next(path_trials(par,other,cs,nets,index,path,RoutingOptions()))[0]
        axis=pl.column_x[0]+pl.column_x[-1]
        for n in path['members']:
            x,y=one.pos_of(index[n]);xx,yy=two.pos_of(index[n])
            self.assertLessEqual(abs(x+xx-axis),2)
            self.assertEqual(y,yy)

    def test_custom_grid_and_clearances_are_not_lowered(self):
        pl,cs,nets,index,plan,_=fixture()
        options=RoutingOptions(grid_step=8,halo=18,cross_clearance=16)
        trials=list(path_trials(par,pl,cs,nets,index,plan['physical_series_paths']['paths'][0],options))
        self.assertTrue(any(ai._checked_static(par,t,nets,index,options,None) for t,a in trials))

    def test_internal_branch_is_not_silently_reclassified_as_a_series_path(self):
        pl,cs,_,index,plan,_=fixture()
        cs=deepcopy(cs);cs[index['a']]['nodes']['G']='middle'
        model=compile_paths(cs,plan)
        self.assertEqual([],model['paths'])
        self.assertEqual('branched_internal_node',model['skipped'][0]['reason'])

    def test_source_managed_members_are_not_given_a_second_motion_group(self):
        pl,cs,_,index,plan,_=fixture()
        plan=deepcopy(plan);plan['source_managed_members']=['r']
        self.assertEqual([],compile_paths(cs,plan)['paths'])

    def test_two_arm_join_and_supply_chains_are_not_unbranched_signal_paths(self):
        pl,cs,_,index,plan,_=fixture()
        changed=deepcopy(plan);changed['groups'][0]['kind']='two_arm_join'
        self.assertEqual([],compile_paths(cs,changed)['paths'])
        changed=deepcopy(plan);changed['readability_plan']['junctions']['middle']['is_rail']=True
        self.assertEqual([],compile_paths(cs,changed)['paths'])

    def test_changed_symbol_or_terminal_contract_is_unknown_not_zero(self):
        pl,cs,_,index,plan,_=fixture()
        for key,value in (('symbol','inductor'),('node','wrong')):
            other=deepcopy(cs)
            if key=='node':other[index['r']]['nodes']['1']=value
            else:other[index['r']]['symbol']=value
            measured=measure_paths(pl,other,index,plan['physical_series_paths'])
            self.assertFalse(measured['measurable'])
            self.assertIsNone(path_rank(measured))

    def test_fixed_member_is_skipped_but_fixed_host_is_unchanged(self):
        pl,cs,nets,index,plan,_=fixture()
        path=plan['physical_series_paths']['paths'][0]
        pl.fixed.add(index['r'])
        self.assertEqual([],list(path_trials(par,pl,cs,nets,index,path,RoutingOptions())))
        pl.fixed.clear();pl.fixed.add(index['a'])
        self.assertTrue(all(t.pos_of(index['a'])==pl.pos_of(index['a'])
            for t,a in path_trials(par,pl,cs,nets,index,path,RoutingOptions())))

    def test_acceptance_can_compact_a_chain_without_increasing_estimated_wire(self):
        case=fixture(inside=True);pl,cs,nets,index,plan,_=case
        out,budget,report=finish(case)
        self.assertEqual(1,report['accepted'])
        action=report['actions'][0]
        self.assertGreater(action['owner_distance_after'],action['owner_distance_before'])
        self.assertLessEqual(action['estimated_wire_after'],action['estimated_wire_before'])
        self.assertLess(report['physical_series_finish']['after']['internal_wirelength'],
            report['physical_series_finish']['before']['internal_wirelength'])

    def test_whole_estimated_wire_increase_still_rejects_a_shorter_internal_link(self):
        case=fixture(inside=True);pl=case[0]
        with patch('netlist2schematic.plugins.layout_shared.branch_alignment._hpwl',
                   side_effect=lambda module,p,*args:100 if p is pl else 101):
            out,budget,report=finish(case)
        self.assertIs(pl,out);self.assertEqual(0,report['accepted'])
        self.assertGreater(report['physical_series_finish']['generation'][0]['hpwl_rejected'],0)

    def test_failed_or_late_static_validation_retains_the_old_state(self):
        for answer in (False,None):
            case=fixture();before=state(case[0]);out,budget,report=finish(case,answer)
            self.assertIs(case[0],out);self.assertEqual(before,state(case[0]))
            self.assertFalse(report['accepted'])
            self.assertEqual(answer is None,bool(budget.get('exhausted')))

    def test_expired_generator_does_not_build_geometry(self):
        pl,cs,nets,index,plan,_=fixture()
        with patch('netlist2schematic.routing.routing_rules.build_scene',side_effect=AssertionError('expired')):
            self.assertEqual([],list(path_trials(par,pl,cs,nets,index,plan['physical_series_paths']['paths'][0],
                RoutingOptions(),deadline=perf_counter()-1)))

    def test_constructor_timeout_and_resource_error_have_distinct_results(self):
        case=fixture();pl,cs,nets,index,plan,_=case
        with patch('netlist2schematic.routing.routing_rules.build_scene',side_effect=RoutingFailure(
                'expired',{'reason':'quality_time_budget'})):
            out,budget,report=finish(case)
        self.assertIs(pl,out);self.assertTrue(budget['exhausted'])
        with patch('netlist2schematic.routing.routing_rules.build_scene',side_effect=ValueError('resource failure')):
            with self.assertRaisesRegex(ValueError,'resource failure'):finish(case)

    def test_reserved_trials_come_from_the_original_counter(self):
        case=fixture();pl=case[0]
        out,budget,report=finish(case,False,count=124)
        self.assertIs(pl,out);self.assertLessEqual(budget['candidates'],128)
        self.assertEqual(124+report['candidates'],budget['candidates'])
        self.assertEqual(4,report['physical_series_finish']['reserved_trials'])
        self.assertGreater(report['physical_series_finish']['trials'],0)
        self.assertLessEqual(report['physical_series_finish']['trials'],4)

    def test_generator_does_not_read_files_or_call_Astar(self):
        pl,cs,nets,index,plan,_=fixture()
        with patch.object(Path,'read_text',side_effect=AssertionError('no source/GT')),patch(
                'netlist2schematic.routing.orthogonal_router.route_scene',side_effect=AssertionError('no Astar')):
            self.assertTrue(list(path_trials(par,pl,cs,nets,index,
                plan['physical_series_paths']['paths'][0],RoutingOptions())))

    def test_a_second_finish_is_stable(self):
        case=fixture();out,budget,report=finish(case)
        repeated=(out,*case[1:])
        again,_,second=finish(repeated)
        self.assertEqual(out.cell_of,again.cell_of)
        self.assertEqual(out.orient,again.orient)
        self.assertEqual(out.passive_local_offsets,again.passive_local_offsets)
        self.assertEqual(0,second['accepted'])

    def test_touching_same_net_fixed_escapes_pass_independent_complete_validation(self):
        from netlist2schematic.routing.routing_rules import build_scene
        from netlist2schematic.routing.sa_routing import validate_final_routes
        case=fixture();pl,cs,nets,index,plan,_=case
        current,_,report=finish(case)
        self.assertEqual(1,report['accepted'])
        scene=build_scene(par,nets,current,index)
        model=plan['physical_series_paths']['paths'][0]
        middle=model['nodes'][1]
        self.assertEqual(scene.pins[middle][0].escape,scene.pins[middle][1].escape)
        routes={net:list(scene.fixed_segments[net]) for net in scene.pins}
        y=report['actions'][0]['line_y']
        for end in model['relation']['ends']:
            hosts={index[n] for n,p in end['hosts']}
            pins=[pin for pin in scene.pins[end['net']] if pin.component in hosts]
            first,last=sorted((p.escape for p in pins),key=lambda p:p[1])
            routes[end['net']].append((first,last))
            guest=next(p for p in scene.pins[end['net']] if p.component==index[end['guest'][0]])
            routes[end['net']].append(((first[0],y),guest.escape))
        _,metrics,validation=validate_final_routes(par,nets,current,index,list(routes.items()))
        self.assertTrue(validation['valid'])
        self.assertEqual(0,metrics['body_pen'])

    def test_reversed_host_order_is_unmeasurable_without_launching_search(self):
        pl,cs,nets,index,plan,_=fixture()
        cells=list(pl.cell_of);cells[index['u']]=(3,1)
        pl.set_state(cells,pl.orient)
        with patch('netlist2schematic.routing.routing_rules.build_scene',side_effect=AssertionError('no window')):
            self.assertEqual([],list(path_trials(par,pl,cs,nets,index,
                plan['physical_series_paths']['paths'][0],RoutingOptions())))
        self.assertFalse(measure_paths(pl,cs,index,plan['physical_series_paths'])['measurable'])


def path_candidate(identifier,internal,owner=2,length=1000,crossings=0,score=10):
    row=candidate(identifier,length=length,crossings=crossings,score=score)
    report=row[5]['layout_attachments']
    report['plan']={'physical_series_paths':{'paths':[{'group':'one'}]}}
    report['metrics']['owner_distance']=owner
    report['physical_path_organization']=dict(count=1,measurable=True,outside_errors=0,
        direction_errors=0,order_errors=0,pin_alignment_error=0,internal_wirelength=internal)
    return row


class PathSelectionTests(unittest.TestCase):
    def test_proven_path_regularization_can_win_over_nearest_host_distance(self):
        old=path_candidate('1',100,owner=1)
        new=path_candidate('2',40,owner=2,length=1100,score=11)
        chosen,selection=select_attachment_result([old,new])
        self.assertIs(new,chosen)
        self.assertEqual(11,chosen[7]['score'])
        self.assertEqual('verified_organization_gain',selection['reason'])

    def test_new_path_terms_do_not_bypass_real_wire_and_crossing_limits(self):
        old=path_candidate('1',100)
        for new in (path_candidate('2',40,length=1151,score=11),
                    path_candidate('2',40,crossings=1,score=11)):
            self.assertIs(old,select_attachment_result([old,new])[0])

    def test_missing_or_unmeasurable_evidence_is_not_a_zero_error_gain(self):
        row=path_candidate('1',100)
        self.assertIsNotNone(organization_rank(row[5]['layout_attachments']))
        row[5]['layout_attachments']['physical_path_organization']['measurable']=False
        self.assertIsNone(organization_rank(row[5]['layout_attachments']))
        row[5]['layout_attachments'].pop('physical_path_organization')
        self.assertIsNone(organization_rank(row[5]['layout_attachments']))


if __name__=='__main__':unittest.main()
