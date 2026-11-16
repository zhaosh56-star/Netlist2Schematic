"""Opposite-rail control shunts pack with their uniquely proved host."""
from copy import deepcopy
import json
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_layout_evidence import mos
from tests.test_passive_placement import convert,resistor,source
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment import attachment_control_shunts as shunts
from netlist2schematic.plugins.attachment.attachment_selection import measure_candidate,organization_rank,select_attachment_result
from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.routing import sa_routing
from netlist2schematic.routing.orthogonal_router import RoutingFailure


def circuit(*,dual=False,bjt=False,rename=False):
    d=dict(a=mos('out','bias','vdd','PMOS'),b=mos('out','input','0'),
        h=mos('out','control','0'),c=resistor('vdd','control','C'),r=resistor('control','0'))
    cells=dict(a=(1,1),b=(2,1),h=(2,5),c=(2,6),r=(2,7));names={n:n for n in d}
    if dual:
        for c in d.values():
            if c['cls']=='MOS':c['type']='PMOS' if c['type']=='NMOS' else 'NMOS'
            for k in ('d','g','s','bulk','a','b'):
                if c.get(k) in ('0','vdd'):c[k]={'0':'vdd','vdd':'0'}[c[k]]
        cells={n:(3-r,c) for n,(r,c) in cells.items()}
    if bjt:
        d={n:dict(cls='BJT',type='NPN' if c['type']=='NMOS' else 'PNP',c=c['d'],b=c['g'],e=c['s'])
           if c['cls']=='MOS' else c for n,c in d.items()}
    if rename:
        names={n:'device'+str(31-i) for i,n in enumerate(d)}
        d={names[n]:{k:'wire_'+v if k in ('d','g','s','c','b','e','a') and v not in ('0','vdd') else v
                     for k,v in c.items()} for n,c in reversed(list(d.items()))}
        cells={names[n]:p for n,p in cells.items()}
    return d,cells,names


def fixture(**kwargs):
    d,cells,names=circuit(**kwargs);structs=analyze_structures(d)
    pl,cs,nets,index,_=convert(d,cells,records=structs)
    plan=ai.build_attachment_plan(d,cs,nets,index,structs,compute_levels(d),pl.row_of,policy='all')
    return d,ai._install(pl,cs,index,plan),cs,nets,index,plan,names


def finish(f,*,safe=True,budget=None,until=None,cap=float('inf')):
    _,pl,cs,nets,index,plan,*_=f
    budget=ai.new_attachment_budget() if budget is None else budget
    report=dict(actions=[],candidates=0,accepted=0)
    args=(structured.par,pl,cs,nets,index,plan,RoutingOptions(),budget,
          perf_counter()+1 if until is None else until,report)
    if safe=='real':result=shunts.finish_shunts(*args,hpwl_limit=cap)
    else:
        with patch.object(ai,'_checked_static',return_value=safe):
            result=shunts.finish_shunts(*args,hpwl_limit=cap)
    return result,report,budget


class ControlShuntTests(unittest.TestCase):
    def test_exact_node_and_port_evidence_preserves_electrical_data_and_owners(self):
        f=fixture();before=deepcopy((f[0],f[2],f[3],f[1].blocks))
        model=f[5]['control_rail_shunts'];self.assertEqual(len(model['groups']),1)
        g=model['groups'][0];self.assertEqual(g['host'],['h','G']);self.assertEqual(g['members'],['c','r'])
        self.assertFalse(g['ownership_claim']);self.assertIsNone(g['function_claim'])
        result,report,budget=finish(f)
        self.assertTrue(report['control_rail_shunt_finish']['applied']);self.assertTrue(result.valid_state())
        self.assertEqual((f[0],f[2],f[3],result.blocks),before)
        self.assertEqual([r for r,c in result.cell_of],[r for r,c in f[1].cell_of])
        self.assertEqual(result.attachment_axes,f[1].attachment_axes)
        for n in ('a','b'):
            i=f[4][n];self.assertEqual((result.cell_of[i],result.orient[i]),(f[1].cell_of[i],f[1].orient[i]))
        for n in ('c','r'):self.assertEqual(result.cell_of[f[4][n]],f[1].cell_of[f[4][n]])
        self.assertLessEqual(budget['candidates'],12)

    def test_N_P_BJT_and_polarity_duals_are_applicable(self):
        for dual,bjt in ((True,False),(False,True),(True,True)):
            with self.subTest(dual=dual,bjt=bjt):
                f=fixture(dual=dual,bjt=bjt);self.assertEqual(len(f[5]['control_rail_shunts']['groups']),1)
                result,report,_=finish(f,safe='real');self.assertTrue(report['control_rail_shunt_finish']['applied'])
                self.assertEqual(shunts.shunt_rank(shunts.measure_shunts(result,f[2],f[4],f[5]['control_rail_shunts']))[:2],(0,0))

    def test_rename_and_netlist_reorder_retain_same_port_relation(self):
        f=fixture(rename=True);g=f[5]['control_rail_shunts']['groups'][0]
        self.assertEqual(g['host'][0],f[6]['h']);self.assertEqual(set(g['members']),{f[6]['c'],f[6]['r']})
        result,report,_=finish(f,safe='real');self.assertTrue(report['control_rail_shunt_finish']['applied'])

    def test_same_rail_and_unknown_rail_do_not_form_opposite_rail_pair(self):
        for other in ('vdd','unknown'):
            f=fixture();cs=deepcopy(f[2]);cs[f[4]['r']]['nodes']['2']=other
            model=shunts.compile_shunts(cs,f[5],[])
            self.assertEqual(model['groups'],[])
            self.assertIn('same_rail_side' if other=='vdd' else 'unknown_rail_role',{r['reason'] for r in model['skipped']})

    def test_multiple_hosts_sources_or_extra_visible_tap_are_not_merged(self):
        for extra in ('host','voltage','passive'):
            d,cells,_=circuit()
            d['extra']=mos('aux','control','0') if extra=='host' else source('VSRC','control') if extra=='voltage' else resistor('control','0')
            cells['extra']=(3,3);structs=analyze_structures(d)
            pl,cs,nets,index,_=convert(d,cells,records=structs)
            plan=ai.build_attachment_plan(d,cs,nets,index,structs,compute_levels(d),pl.row_of,policy='all')
            self.assertEqual(plan['control_rail_shunts']['groups'],[])

    def test_existing_owner_axis_and_source_managed_members_remain_protected(self):
        f=fixture()
        for owners,key in (([['h','b']],None),([], 'axis'),([], 'managed')):
            p=deepcopy(f[5])
            if key=='axis':p['host_axes'].append(dict(a='h',b='b'))
            if key=='managed':p['source_managed_members']=['c']
            self.assertEqual(shunts.compile_shunts(f[2],p,owners)['groups'],[])

    def test_unique_host_without_main_conduction_target_is_skipped(self):
        f=fixture();p=deepcopy(f[5]);p['readability_plan']['junctions']['out']['conduction_ports']=[['h','D']]
        self.assertEqual(shunts.compile_shunts(f[2],p,[])['groups'],[])

    def test_static_real_ports_power_markers_and_forbid_Astar(self):
        f=fixture();options=RoutingOptions(cross_clearance=18,halo=16)
        with patch.object(sa_routing,'final_route',side_effect=AssertionError('no search during packing')):
            result,report,_=finish(f,safe='real')
        self.assertTrue(report['control_rail_shunt_finish']['applied'])
        # A different clearance is used independently, never substituted for
        # the production verifier. Enumerated candidates adapt to it too.
        g=f[5]['control_rail_shunts']['groups'][0];accepted=[]
        for trial,_ in shunts.shunt_trials(structured.par,f[1],f[2],f[3],f[4],f[5]['control_rail_shunts'],g,options,perf_counter()+1):
            if trial.valid_state() and ai._checked_static(structured.par,trial,f[3],f[4],options,perf_counter()+1):accepted.append(trial)
        self.assertTrue(accepted)

    def test_symbol_mirror_and_translation_do_not_depend_on_coordinates(self):
        f=fixture();pl=f[1]
        pl.column_x=[x+80 for x in pl.column_x];pl.row_y=[y+40 for y in pl.row_y]
        pl.attachment_axes=tuple(pl.column_x),tuple(pl.row_y)
        pl.set_state([(r,pl.cols-1-c) for r,c in pl.cell_of],[1-o if f[2][i]['symbol'] in shunts.ACTIVE else o for i,o in enumerate(pl.orient)])
        result,report,_=finish(f,safe='real');self.assertTrue(report['control_rail_shunt_finish']['applied'])
        self.assertEqual(shunts.measure_shunts(result,f[2],f[4],f[5]['control_rail_shunts'])['direction_errors'],0)

    def test_fixed_host_or_either_guest_skips_without_moving_other_members(self):
        for name in ('h','c','r'):
            f=fixture();f[1].fixed={f[4][name]};result,report,budget=finish(f)
            self.assertIs(result,f[1]);self.assertEqual(budget['candidates'],0)
            self.assertEqual(report['control_rail_shunt_finish']['generation'][0]['stop_reason'],'fixed_group_member')

    def test_custom_power_geometry_is_not_approximated_with_default_markers(self):
        f=fixture();f[1].power_connections=[];result,report,_=finish(f)
        self.assertIs(result,f[1]);self.assertEqual(report['control_rail_shunt_finish']['generation'][0]['stop_reason'],'custom_power_geometry')

    def test_rejected_trial_is_atomic_and_no_unrelated_axes_or_offsets_change(self):
        f=fixture();snapshot=deepcopy(f[1].__dict__);result,report,_=finish(f,safe=False)
        self.assertIs(result,f[1]);self.assertEqual(result.cell_of,snapshot['cell_of']);self.assertEqual(result.orient,snapshot['orient'])
        self.assertEqual(getattr(result,'passive_local_offsets',None),snapshot.get('passive_local_offsets'))
        self.assertGreater(report['control_rail_shunt_finish']['static_rejections'],0)

    def test_budget_reservation_and_one_remaining_trial_do_not_reset(self):
        f=fixture();budget=ai.new_attachment_budget(max_candidates=24)
        shunts.reserve_finish(f[5],budget);self.assertEqual(budget['control_shunt_reserve'],3)
        budget['candidates']=23
        result,report,_=finish(f,budget=budget)
        self.assertEqual(budget['candidates'],24);self.assertLessEqual(report['control_rail_shunt_finish']['trials'],1)
        shunts.reserve_finish(f[5],budget);self.assertNotIn('control_shunt_reserve',budget)
        result,report,_=finish((*f[:1],result,*f[2:]),budget=budget)
        self.assertEqual(budget['candidates'],24)

    def test_expired_deadline_performs_no_scene_or_trial(self):
        f=fixture()
        with patch('netlist2schematic.routing.routing_rules.build_scene',side_effect=AssertionError('expired')):
            result,report,budget=finish(f,until=perf_counter()-1)
        self.assertIs(result,f[1]);self.assertEqual(budget['candidates'],0)

    def test_late_success_cannot_commit_and_earlier_verified_best_survives(self):
        f=fixture();clock=[0.];calls=[0]
        trials=list(shunts.shunt_trials(structured.par,f[1],f[2],f[3],f[4],f[5]['control_rail_shunts'],
            f[5]['control_rail_shunts']['groups'][0],RoutingOptions(),perf_counter()+1))
        def late(*args):calls[0]+=1;clock[0]=5.;return True
        with patch.object(shunts,'shunt_trials',return_value=iter(trials)),patch.object(shunts,'perf_counter',side_effect=lambda:clock[0]),patch.object(ai,'perf_counter',side_effect=lambda:clock[0]),patch.object(ai,'_checked_static',side_effect=late):
            result,report,_=finish(f,safe='real',until=4.)
        self.assertEqual(calls[0],1);self.assertIs(result,f[1])
        with patch.object(ai,'_checked_static',side_effect=[True,None]):
            result,report,budget=finish(f,safe='real')
        self.assertTrue(result.valid_state());self.assertTrue(report['control_rail_shunt_finish']['applied']);self.assertTrue(budget['exhausted'])

    def test_wire_forecast_guard_is_still_enforced(self):
        f=fixture();result,report,_=finish(f,cap=0)
        self.assertIs(result,f[1]);self.assertGreater(report['control_rail_shunt_finish']['hpwl_rejections'],0)

    def test_candidate_generation_never_overwrites_an_occupied_host_cell(self):
        f=fixture();pl=f[1]
        host=f[4]['h'];occupied={p for p,i in pl.comp_at.items() if i!=host}
        count=0
        for result,action in shunts.shunt_trials(structured.par,pl,f[2],f[3],f[4],f[5]['control_rail_shunts'],
                f[5]['control_rail_shunts']['groups'][0],RoutingOptions(),perf_counter()+1):
            count+=1;self.assertNotIn(result.cell_of[host],occupied)
            for n in ('a','b'):
                i=f[4][n];self.assertEqual(result.cell_of[i],pl.cell_of[i]);self.assertEqual(result.orient[i],pl.orient[i])
        self.assertGreater(count,0)

    def test_saved_json_measurement_offsets_and_contract_round_trip(self):
        f=fixture();result,_,_=finish(f,safe='real');model=json.loads(json.dumps(f[5]['control_rail_shunts']))
        measured=shunts.measure_shunts(result,f[2],f[4],model)
        self.assertTrue(measured['measurable']);self.assertEqual(shunts.shunt_rank(measured)[:2],(0,0))
        result.__class__=structured.StructuredPlacement
        self.assertEqual(shunts.shunt_rank(shunts.measure_shunts(result,f[2],f[4],model)),shunts.shunt_rank(measured))
        model['terminal_contract']['h']['nodes']['G']='other'
        self.assertFalse(shunts.measure_shunts(result,f[2],f[4],model)['measurable'])

    def test_measurability_and_nonfinite_data_are_not_zero_success(self):
        f=fixture();m=shunts.measure_shunts(f[1],f[2],f[4],{})
        self.assertIsNone(m['owner_distance']);self.assertFalse(m['measurable'])
        m=shunts.measure_shunts(f[1],f[2],f[4],f[5]['control_rail_shunts']);m['owner_distance']=float('nan')
        self.assertIsNone(shunts.shunt_rank(m))

    def test_common_manifest_selection_and_missing_evidence_cannot_win(self):
        f=fixture();result,_,_=finish(f)
        old=measure_candidate(f[1],f[2],f[4],dict(candidate_id=1),f[5],policy='all')
        new=measure_candidate(result,f[2],f[4],dict(candidate_id=2),f[5],policy='all')
        self.assertLess(organization_rank(new['layout_attachments']),organization_rank(old['layout_attachments']))
        new['layout_attachments'].pop('control_rail_shunt_organization')
        self.assertIsNone(organization_rank(new['layout_attachments']))

    def test_real_wire_and_zero_crossing_caps_preserve_old_quality(self):
        f=fixture();result,_,_=finish(f)
        def row(pl,candidate,score,wire,cross):
            md=measure_candidate(pl,f[2],f[4],dict(candidate_id=candidate),f[5],policy='all')
            return (score,pl,None,None,None,md,(None,dict(wirelength=wire,crossings=cross)),None,candidate)
        old=row(f[1],1,10,100,0)
        for bad in (row(result,2,11,116,0),row(result,2,11,110,1)):
            selected,_=select_attachment_result([old,bad]);self.assertIs(selected,old)
        selected,_=select_attachment_result([old,row(result,2,11,110,0)])
        self.assertEqual(selected[5]['candidate_id'],2);self.assertEqual(selected[0],11)

    def test_passive_policy_and_reference_random_stream_remain_unmodified(self):
        f=fixture();rng=f[1].rng.getstate()
        plan=ai.build_attachment_plan(f[0],f[2],f[3],f[4],analyze_structures(f[0]),compute_levels(f[0]),f[1].row_of,policy='passive')
        self.assertEqual(plan['control_rail_shunts']['groups'],[])
        result,_,_=finish(f)
        self.assertEqual(f[1].rng.getstate(),rng);self.assertEqual(result.rng.getstate(),rng)

    def test_repeated_finish_is_stable_and_deterministic(self):
        f=fixture();result,report,_=finish(f,safe='real')
        repeat,_,_=finish((*f[:1],result,*f[2:]),safe='real')
        other,_,_=finish(f,safe='real')
        for k in ('cell_of','orient','passive_local_offsets'):
            self.assertEqual(getattr(repeat,k),getattr(result,k));self.assertEqual(getattr(other,k),getattr(result,k))

    def test_scene_timeout_is_atomic_and_does_not_extend_budget(self):
        f=fixture()
        with patch('netlist2schematic.routing.routing_rules.build_scene',side_effect=RoutingFailure('stopped',dict(reason='quality_time_budget'))):
            result,report,budget=finish(f,safe='real')
        self.assertIs(result,f[1]);self.assertTrue(budget['exhausted']);self.assertEqual(budget['candidates'],0)
        self.assertEqual(report['control_rail_shunt_finish']['stop_reason'],'shared_attachment_budget')

    def test_missing_resource_is_not_treated_as_routing_timeout_or_success(self):
        f=fixture()
        with patch('netlist2schematic.routing.routing_rules.build_scene',side_effect=ValueError('missing resource')):
            with self.assertRaisesRegex(ValueError,'missing resource'):finish(f,safe='real')


if __name__=='__main__':unittest.main()
