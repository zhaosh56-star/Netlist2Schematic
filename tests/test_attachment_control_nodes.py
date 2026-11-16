"""Shared G/B hubs and neutral source guests, never GT-specific layouts."""
from copy import deepcopy
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_attachment_ports import installed
from tests.test_passive_placement import mos,resistor,source
from netlist2schematic.plugins.attachment import attachment_control_nodes as nodes
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment.attachment_selection import organization_rank
from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.recognition.device_leveling import compute_levels


def fixture(*,unsafe=True,bjt=False,rename=False,rail='vdd',returned=True,polarity=False):
    p=lambda d:dict(cls='MOS',type='PMOS',model='PMOS',d=d,g='bias',s='vdd',bulk='vdd')
    devices=dict(pa=p('bus'),pb=p('out'),lower=mos('bus','cn'),
        c=resistor(rail,'bias','C'),i=source('ISRC','0' if polarity else 'bus','bus' if polarity else '0'),
        outside=resistor('other','0'))
    cells=dict(pa=(1,2),pb=(1,5),lower=(4,4),c=(2,6),i=(2,1),outside=(5,7))
    if returned:devices['r']=resistor('bus','bias');cells['r']=(2,3)
    if bjt:
        for name in ('pa','pb','lower'):
            d=devices[name];devices[name]=dict(cls='BJT',type='PNP' if name!='lower' else 'NPN',
                model='Q',c=d['d'],b=d['g'],e=d['s'])
    names={n:n for n in devices}
    if rename:
        names={n:'unit'+str(60-k) for k,n in enumerate(devices)}
        devices={names[n]:{k:('signal_'+v if k in ('a','b','c','d','e','g','s','p','n')
            and isinstance(v,str) and v not in ('0','vdd') else v) for k,v in d.items()}
            for n,d in reversed(list(devices.items()))}
        cells={names[n]:v for n,v in cells.items()}
    pl,comps,nets,index,plan=installed(devices,cells)
    plan=ai.build_attachment_plan(devices,comps,nets,index,analyze_structures(devices),
                                 compute_levels(devices),pl.row_of,policy='all')
    pl=ai._install(pl,comps,index,plan)
    orientations=list(pl.orient);orientations[index[names['c']]]=1
    if returned:orientations[index[names['r']]]=2
    orientations[index[names['i']]]=(0 if polarity else 2) if unsafe else (2 if polarity else 0)
    pl.set_state(pl.cell_of,orientations)
    return devices,(pl,comps,nets,index,plan),names


def state(pl):return deepcopy((pl.cell_of,pl.orient,pl.row_of,pl.comp_at,pl.column_x,pl.row_y,pl.rng.getstate()))


_UNSET=object()


def finish(f,*,static=_UNSET,max_candidates=24):
    pl,comps,nets,index,plan=f;budget=ai.new_attachment_budget(max_candidates=max_candidates)
    report=dict(actions=[],accepted=0,candidates=0)
    if static is _UNSET:
        result=ai._shared_control_finish(structured.par,pl,pl,comps,nets,index,plan,RoutingOptions(),
            budget,perf_counter()+1,report,hpwl_limit=_hpwl(structured.par,pl,nets,index)*1.15)
    else:
        with patch.object(ai,'_checked_static',return_value=static):
            result=ai._shared_control_finish(structured.par,pl,pl,comps,nets,index,plan,RoutingOptions(),
                budget,perf_counter()+1,report,hpwl_limit=_hpwl(structured.par,pl,nets,index)*1.15)
    return result,report,budget


class SharedControlNodeTests(unittest.TestCase):
    def test_compiler_records_real_shared_controls_not_a_functional_pair(self):
        _,f,names=fixture();pl,comps,nets,index,plan=f
        node,=plan['control_nodes']['nodes']
        self.assertEqual(node['net'],'bias');self.assertEqual(len(node['hosts']),2)
        self.assertEqual(set(node['members']),{'c','r'})
        r=next(g for g in plan['groups'] if 'r' in g['members'])
        self.assertEqual(r['row_mode'],'control_connection');self.assertFalse(r['requires_interior'])
        self.assertEqual(plan['source_groups'][0]['kind'],'neutral_junction_source')
        self.assertFalse(plan['source_groups'][0]['functional_role_claimed'])
        self.assertEqual(nodes.compile_nodes(list(reversed(comps)),plan),plan['control_nodes'])

    def test_common_power_bias_or_different_source_roles_do_not_invent_a_control_bank(self):
        _,f,_=fixture();_,comps,_,index,plan=f
        for mode in ('conduction','source','no_shunt'):
            altered=deepcopy(plan)
            if mode=='conduction':altered['readability_plan']['junctions']['bias']['conduction_ports']=[['pa','D']]
            if mode=='source':altered['readability_plan']['junctions']['bias']['source_ports']=[['drive','+']]
            if mode=='no_shunt':altered['groups']=[g for g in altered['groups'] if 'c' not in g['members']]
            self.assertEqual(nodes.compile_nodes(comps,altered)['nodes'],[])
        changed=deepcopy(comps);changed[index['pb']]['nodes']['S']='other_supply'
        self.assertEqual(nodes.compile_nodes(changed,plan)['nodes'],[])

    def test_passive_trials_keep_all_hosts_axes_fixed_and_original_state_intact(self):
        _,f,_=fixture(unsafe=False);pl,comps,nets,index,plan=f;before=state(pl)
        node,=plan['control_nodes']['nodes']
        trials=list(nodes.node_trials(pl,comps,index,plan,node))
        self.assertTrue(trials)
        for trial,action in trials:
            self.assertTrue(trial.valid_state());self.assertEqual(trial.column_x,pl.column_x);self.assertEqual(trial.row_y,pl.row_y)
            for name,i in index.items():
                if name not in node['members']:
                    self.assertEqual(trial.pos_of(i),pl.pos_of(i));self.assertEqual(trial.orient[i],pl.orient[i])
            self.assertTrue(action['active_world_unchanged'])
        self.assertEqual(state(pl),before)

    def test_full_static_finish_repairs_source_then_places_rail_cap_without_astar(self):
        _,f,_=fixture();pl,comps,nets,index,plan=f
        self.assertGreater(_static_faults(structured.par,pl,nets,index,RoutingOptions(),None)['count'],0)
        with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar')):
            result,report,budget=finish(f)
        self.assertTrue(result.valid_state());self.assertGreater(report['accepted'],0)
        self.assertEqual(_static_faults(structured.par,result,nets,index,RoutingOptions(),None)['count'],0)
        measured=nodes.measure_nodes(result,comps,index,plan['control_nodes'],plan['readability_plan']['junctions'])
        self.assertEqual(measured['shunt_direction_errors'],0)
        self.assertEqual(result.pin_dir(index['i'],'-'),'south')
        self.assertEqual(comps[index['i']]['nodes'],{'+':'bus','-':'0'})
        self.assertLessEqual(budget['candidates'],24)
        self.assertEqual(report['actions'][0]['kind'],'control_connection_source_repair')

    def test_bjt_and_renamed_reordered_inputs_keep_equivalent_access(self):
        for options in ({'bjt':True},{'rename':True}):
            _,f,names=fixture(**options)
            result,report,_=finish(f)
            self.assertTrue(report['accepted'])
            metric=nodes.measure_nodes(result,f[1],f[3],f[4]['control_nodes'],f[4]['readability_plan']['junctions'])
            self.assertTrue(metric['measurable']);self.assertEqual(metric['shunt_direction_errors'],0)
            self.assertEqual(_static_faults(structured.par,result,f[2],f[3],RoutingOptions(),None)['count'],0)

    def test_grounded_positive_source_pole_is_not_swapped_to_make_arrow_pretty(self):
        _,f,_=fixture(polarity=True);pl,comps,nets,index,plan=f
        result,report,_=finish(f)
        self.assertTrue(report['accepted']);self.assertEqual(comps[index['i']]['nodes'],{'+':'0','-':'bus'})
        self.assertEqual(result.pin_dir(index['i'],'+'),'south')

    def test_ground_shunt_points_down_without_force_placing_return_in_missing_space(self):
        _,f,_=fixture(unsafe=False,rail='0',returned=False);pl,comps,nets,index,plan=f
        result,report,_=finish(f)
        self.assertTrue(report['accepted'])
        rail=nodes._pin(comps[index['c']],'0')
        self.assertEqual(result.pin_dir(index['c'],rail),'south')

    def test_missing_or_changed_terminals_are_unknown_not_perfect(self):
        _,f,_=fixture();pl,comps,nets,index,plan=f
        changed=deepcopy(comps);changed[index['pa']]['nodes']['G']='changed'
        metric=nodes.measure_nodes(pl,changed,index,plan['control_nodes'],plan['readability_plan']['junctions'])
        self.assertFalse(metric['measurable']);self.assertIsNone(metric['shunt_direction_errors']);self.assertTrue(metric['missing'])
        bad=dict(metrics=ai.measure_attachments(pl,comps,index,plan),shared_control_access=metric)
        self.assertIsNone(organization_rank(bad))

    def test_mirror_and_translation_keep_same_relationship_metrics(self):
        _,f,_=fixture(unsafe=False);pl,comps,nets,index,plan=f
        class View:
            def pin_world(self,i,pin):
                x,y=pl.pin_world(i,pin);return 2000-x,y+67
            def pin_dir(self,i,pin):
                direction=pl.pin_dir(i,pin);return {'east':'west','west':'east'}.get(direction,direction)
        a=nodes.measure_nodes(pl,comps,index,plan['control_nodes'],plan['readability_plan']['junctions'])
        b=nodes.measure_nodes(View(),comps,index,plan['control_nodes'],plan['readability_plan']['junctions'])
        self.assertEqual(nodes.node_rank(a),nodes.node_rank(b))

    def test_fixed_passive_and_source_remain_fixed(self):
        _,f,_=fixture();pl,comps,nets,index,plan=f
        pl.fixed={index['c'],index['i']};pl.fixed_cells={i:pl.cell_of[i] for i in pl.fixed}
        pl=ai._install(pl,comps,index,plan);node,=plan['control_nodes']['nodes']
        self.assertEqual(list(nodes.node_trials(pl,comps,index,plan,node)),[])
        self.assertEqual(list(nodes.related_source_trials(pl,comps,index,plan,node)),[])

    def test_static_rejection_budget_and_deadline_leave_every_original_field(self):
        _,f,_=fixture();before=state(f[0]);node,=f[4]['control_nodes']['nodes']
        self.assertEqual(list(nodes.node_trials(f[0],f[1],f[3],f[4],node,perf_counter()-1)),[])
        for response in (False,None):
            result,report,budget=finish(f,static=response,max_candidates=1)
            self.assertIs(result,f[0]);self.assertEqual(report['actions'],[]);self.assertEqual(state(f[0]),before)
            self.assertLessEqual(budget['candidates'],1)

    def test_shared_ledger_reserves_candidates_without_resetting_spent_time(self):
        _,f,_=fixture();budget=ai.new_attachment_budget();budget.update(candidates=110,spent_seconds=.4)
        ai._reserve_control_finish(f[4],budget)
        self.assertEqual(budget['candidate_ceiling'],112)
        self.assertEqual(budget['max_candidates'],128);self.assertEqual(budget['spent_seconds'],.4)
        budget['candidates']=112
        self.assertFalse(ai._has_budget(budget,perf_counter()+1))
        budget.pop('candidate_ceiling')
        self.assertTrue(ai._has_budget(budget,perf_counter()+1))
        ai._end_budget(budget,perf_counter())
        self.assertGreaterEqual(budget['spent_seconds'],.4)

    def test_passive_policy_does_not_enable_source_freedom(self):
        devices,f,_=fixture(unsafe=False);pl,comps,nets,index,_=f
        plan=ai.build_attachment_plan(devices,comps,nets,index,analyze_structures(devices),
            compute_levels(devices),pl.row_of,policy='passive')
        passive=ai._install(pl,comps,index,plan);node,=plan['control_nodes']['nodes']
        self.assertEqual(plan['source_groups'],[])
        self.assertEqual(list(nodes.related_source_trials(passive,comps,index,plan,node)),[])
        self.assertNotIn(index['i'],passive.attachment_source_ids)

    def test_control_finish_runs_after_legacy_local_and_source_steps(self):
        _,f,_=fixture();pl,comps,nets,index,plan=f
        metadata=dict(layout_attachments=dict(plan=plan,policy='all',budget=ai.new_attachment_budget(max_candidates=32)))
        calls=[]
        def sentinel(par,current,*args,**kwargs):
            report=args[-1]
            calls.append(len(report['actions']))
            report['actions'].append(dict(kind='finish_order_sentinel'))
            report['accepted']+=1
            return current
        with patch.object(ai,'_shared_control_finish',side_effect=sentinel):
            _,record=ai.refine_attachments(structured.par,pl,comps,nets,index,metadata,options=RoutingOptions())
        self.assertEqual(len(calls),1)
        self.assertEqual(record['layout_attachments']['refinement']['actions'][-1]['kind'],'finish_order_sentinel')

    def test_top_edge_without_a_supply_slot_skips_instead_of_expanding_axes(self):
        _,f,_=fixture(unsafe=False);pl,comps,nets,index,plan=f
        cells=list(pl.cell_of)
        for name in ('pa','pb'):cells[index[name]]=0,cells[index[name]][1]
        pl.set_state(cells,pl.orient);pl=ai._install(pl,comps,index,plan)
        before=state(pl);node,=plan['control_nodes']['nodes']
        self.assertEqual(list(nodes.node_trials(pl,comps,index,plan,node)),[])
        self.assertEqual(state(pl),before)


if __name__=='__main__':unittest.main()
