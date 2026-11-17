"""Physical passive groups preserve the existing active skeleton and rules."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
import io
from tests.test_passive_placement import base_placement,resistor,mos
from tests.test_series_alignment import PAR
from netlist2schematic.plugins.passive_embedding import passive_embedding as embedding
from netlist2schematic.routing.routing_rules import RoutingOptions,build_scene


def circuit():
    devices={'upper':mos('a','gu','s1'),'lower':mos('b','gl','s2'),
        'res':resistor('a','mid'),'cap':resistor('mid','b','C')}
    pl,comps,nets,index=base_placement(devices,dict(upper=(1,1),lower=(1,5),res=(3,1),cap=(4,3)))
    return pl,comps,nets,index,dict(layout_style='structured',structs=[])


def run(case,**kwargs):
    pl,cs,ns,ix,data=case
    model=embedding.build_plan(PAR,cs,ns)
    return embedding.refine(PAR,pl,cs,ns,ix,data,model,**kwargs)


class PassiveGroupTests(unittest.TestCase):
    def test_chain_preserves_order_and_actual_endpoints(self):
        pl,cs,ns,ix,data=circuit();p=embedding.build_plan(PAR,cs,ns)
        self.assertEqual(len(p['groups']),1)
        g=p['groups'][0];self.assertEqual(g['kind'],'series')
        self.assertEqual(g['ends'],['a','b'])
        self.assertEqual(g['paths'][0]['nodes'],['a','mid','b'])
        self.assertEqual(g['anchors'],{'a':[['upper','D']],'b':[['lower','D']]})

    def test_parallel_rlc_and_different_length_paths_share_real_ends(self):
        for chain in (False,True):
            dev={'a':resistor('x','y'),'b':resistor('x','y','C'),'c':resistor('x','y','L')}
            if chain:dev['b']=resistor('x','z','C');dev['c']=resistor('z','y','L')
            dev.update(host1=mos('x','g1'),host2=mos('y','g2'))
            pl,cs,ns,ix=base_placement(dev,dict(a=(1,1),b=(1,3),c=(2,3),host1=(0,0),host2=(0,4)))
            p=embedding.build_plan(PAR,cs,ns)
            self.assertEqual(len(p['groups']),1)
            self.assertEqual(p['groups'][0]['kind'],'parallel_paths' if chain else 'parallel')
            self.assertEqual(p['covered_count'],3)

    def test_control_tap_breaks_unbranched_chain(self):
        pl,cs,ns,ix,data=circuit()
        cs[0]['nodes']['G']='mid';ns['mid'].append(('upper','G'))
        p=embedding.build_plan(PAR,cs,ns)
        self.assertEqual([g['kind'] for g in p['groups']],['single','single'])

    def test_rail_does_not_glue_independent_attachments(self):
        dev={'r':resistor('x','vdd'),'c':resistor('y','vdd','C')}
        pl,cs,ns,ix=base_placement(dev,dict(r=(1,1),c=(1,3)))
        self.assertEqual(len(embedding.build_plan(PAR,cs,ns)['groups']),2)

    def test_cycle_is_reported_not_arbitrarily_ordered(self):
        dev={'a':resistor('x','y'),'b':resistor('y','z'),'c':resistor('z','x')}
        pl,cs,ns,ix=base_placement(dev,dict(a=(1,1),b=(1,3),c=(2,3)))
        p=embedding.build_plan(PAR,cs,ns)
        self.assertEqual(p['groups'],[]);self.assertEqual(len(p['skipped']),1)

    def test_reordering_and_renaming_preserve_path_topology(self):
        pl,cs,ns,ix,data=circuit();p=embedding.build_plan(PAR,cs,ns)
        names={c['id']:'id_'+str(i) for i,c in enumerate(cs)}
        newcs=[dict(c,id=names[c['id']],nodes={k:'net_'+v for k,v in c['nodes'].items()}) for c in reversed(cs)]
        newns={'net_'+n:[(names[a],b) for a,b in reversed(ts)] for n,ts in reversed(list(ns.items()))}
        q=embedding.build_plan(PAR,newcs,newns)
        self.assertEqual(q['groups'][0]['kind'],p['groups'][0]['kind'])
        self.assertEqual(set(q['groups'][0]['members']),{names[n] for n in p['groups'][0]['members']})


class PassiveGeometryTests(unittest.TestCase):
    def test_template_fixed_escapes_never_pass_each_other_in_four_directions(self):
        pl,cs,ns,ix,data=circuit();plan=embedding.build_plan(PAR,cs,ns);g=plan['groups'][0]
        for vector in ((1,0),(-1,0),(0,1),(0,-1)):
            options=RoutingOptions(halo=18,grid_step=3)
            positions,orient,axis,extent,space=embedding._template(PAR,pl,cs,ix,g,vector,options)
            trial=deepcopy(pl)
            offsets=[[0.,0.] for c in cs]
            for i,p in positions.items():
                old=trial.pos_of(i);offsets[i]=[p[k]+2000-old[k] for k in (0,1)];trial.orient[i]=orient[i]
            trial.passive_local_offsets=offsets
            scene=build_scene(PAR,ns,trial,ix,options=options)
            pins=scene.pins['mid'];sign=vector[axis]
            by_component={p.component:p for p in pins}
            ordered=g['paths'][0]['members'];a,b=(by_component[ix[n]] for n in ordered)
            self.assertGreater(sign*(b.escape[axis]-a.escape[axis]),0)

    def test_one_sided_parallel_bank_has_port_facing_trials_outside_host(self):
        dev={'host':mos('x','g'),'a':resistor('vdd','x'),'b':resistor('vdd','x','C'),'c':resistor('vdd','x','L')}
        pl,cs,ns,ix=base_placement(dev,dict(host=(3,3),a=(0,0),b=(0,2),c=(1,6)))
        model=embedding.build_plan(PAR,cs,ns);group=model['groups'][0]
        candidates=list(embedding.proposals(PAR,pl,cs,ix,group,RoutingOptions()))
        self.assertTrue(any(all(t.pos_of(ix[n])[1]<pl.pin_world(ix['host'],'D')[1] for n in group['members']) for t in candidates))

    def test_translation_and_mirror_keep_refinement_useful_and_hosts_fixed(self):
        for reflected in (False,True):
            case=circuit();pl,cs,ns,ix,data=case
            pl.column_x=[x+200 for x in pl.column_x];pl.row_y=[y+400 for y in pl.row_y]
            if reflected:
                pl.set_state([(r,pl.cols-1-c) for r,c in pl.cell_of],
                    [o^1 if cs[i]['symbol']=='nmos' else o for i,o in enumerate(pl.orient)])
            new,meta=run(case)
            self.assertTrue(meta['passive_embedding']['applied'])
            self.assertLess(embedding.rank(meta['passive_embedding']['current']),embedding.rank(meta['passive_embedding']['before']))
            for name in ('upper','lower'):
                i=ix[name];self.assertEqual((pl.pos_of(i),pl.orient[i]),(new.pos_of(i),new.orient[i]))

    def test_refinement_preserves_active_world_positions_axes_cells_and_input(self):
        case=circuit();pl,cs,ns,ix,data=case;old=deepcopy(pl.cell_of);original=deepcopy(data)
        with patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('no A*')):
            new,meta=run(case)
        self.assertTrue(meta['passive_embedding']['applied'])
        self.assertEqual(pl.cell_of,old);self.assertEqual(data,original)
        self.assertEqual(new.cell_of,pl.cell_of);self.assertEqual(new.row_y,pl.row_y);self.assertEqual(new.column_x,pl.column_x)
        for name in ('upper','lower'):
            i=ix[name];self.assertEqual((new.pos_of(i),new.orient[i]),(pl.pos_of(i),pl.orient[i]))
        self.assertLess(embedding.rank(meta['passive_embedding']['current']),embedding.rank(meta['passive_embedding']['before']))

    def test_fixed_passive_group_is_not_partially_moved(self):
        case=circuit();pl,cs,ns,ix,data=case;pl.fixed={'res'}
        new,meta=run(case)
        self.assertFalse(meta['passive_embedding']['applied'])
        self.assertEqual(new.orient,pl.orient)
        self.assertEqual([new.pos_of(i) for i in range(len(cs))],[pl.pos_of(i) for i in range(len(cs))])

    def test_hard_static_faults_are_not_treated_as_safe(self):
        case=circuit()
        with patch('netlist2schematic.plugins.preflight.placement_preflight._static_faults',return_value=dict(count=0,hard={'blocked'})):
            new,meta=run(case)
        self.assertFalse(meta['passive_embedding']['applied'])

    def test_zero_budget_and_limit_preserve_best_legal_state(self):
        for kw in (dict(seconds=0),dict(max_trials=0),dict(max_trials=1)):
            case=circuit();new,meta=run(case,**kw)
            self.assertLessEqual(meta['passive_embedding']['trials'],kw.get('max_trials',128))
            self.assertEqual(new.cell_of,case[0].cell_of)

    def test_terminal_contract_mutation_is_rejected(self):
        pl,cs,ns,ix,data=circuit();plan=embedding.build_plan(PAR,cs,ns)
        cs[0]['nodes']['D']='other'
        with self.assertRaisesRegex(ValueError,'terminal contract'):
            embedding.refine(PAR,pl,cs,ns,ix,data,plan)

    def test_unrelated_guest_cannot_use_a_child_pair_interior(self):
        case=circuit();pl,cs,ns,ix,data=case;model=embedding.build_plan(PAR,cs,ns)
        data['layout_hierarchy']=dict(groups=[dict(members=['upper','lower'])])
        # Use an unrelated pair as the protected leaf; the RC hosts are no
        # longer on either boundary node and cannot obtain an insertion permit.
        cs[ix['upper']]['nodes']['D']='other1';cs[ix['lower']]['nodes']['D']='other2'
        ns['a'].remove(('upper','D'));ns['b'].remove(('lower','D'))
        ns['other1']=[('upper','D')];ns['other2']=[('lower','D')]
        model=embedding.build_plan(PAR,cs,ns)
        pl.set_state([(1,1),(1,3),(3,1),(3,3)],pl.orient)
        trial=deepcopy(pl);trial.passive_local_offsets=[[0.,0.] for c in cs]
        i=ix['res'] if 'res' in ix else ix['r'] if 'r' in ix else next(i for i,c in enumerate(cs) if c['symbol']=='resistor')
        left,right=pl.pos_of(ix['upper']),pl.pos_of(ix['lower'])
        trial.passive_local_offsets[i]=[(left[0]+right[0])/2-pl.pos_of(i)[0],left[1]-pl.pos_of(i)[1]]
        self.assertFalse(embedding._state_safe(PAR,trial,pl,cs,ix,model,model['groups'][0],data))


class PassivePolicyTests(unittest.TestCase):
    def test_policy_preserves_default_columns_and_rejects_saved_layout_mutation(self):
        from netlist2schematic.flow import run_flow
        for flag in ('--simplify-placement','--reroute-placement'):
            with patch('sys.stderr',new=io.StringIO()),self.assertRaises(SystemExit):
                run_flow.main([flag,'saved.json','--passive-embedding','auto'])
        with patch.object(run_flow,'run') as run:
            self.assertEqual(run_flow.main(['unused.cir','--passive-embedding','auto']),0)
        self.assertEqual(run.call_args.kwargs['column_layout'],'auto')


class PassiveFlowOrderTests(unittest.TestCase):
    def test_original_candidates_and_active_finalizers_never_run_embedding(self):
        from netlist2schematic.flow import aesthetic_flow
        from netlist2schematic.layout import structured_placement,series_alignment
        from netlist2schematic.plugins.columns import column_layout
        pl,cs,ns,ix,data=circuit();data.update(layout_style='structured',candidate_id=2)
        events=[];budgets={}
        def columns(par,pl,cs,ns,ix,data,model,*args,**kw):
            events.append('columns');budgets['columns']=kw
            return pl,dict(data,layout_columns=dict(plan=model,trials=7))
        def series(par,pl,cs,ns,ix,data,*args,**kw):
            events.append('series');budgets['series']=kw
            return pl,dict(data,series_alignment=dict(trials=5))
        def route(*args,**kw):
            events.append('route');return [],dict(wirelength=1000,crossings=0,bends=8),dict(valid=True)
        with patch.object(structured_placement,'create_candidates',return_value=iter([(pl,cs,ns,ix,data)])), \
             patch.object(column_layout,'refine_columns',side_effect=columns), \
             patch.object(series_alignment,'refine_series_alignment',side_effect=series), \
             patch.object(embedding,'refine',side_effect=AssertionError('Only after the original winner')), \
             patch.object(aesthetic_flow,'final_route',side_effect=route) as routed, \
             patch('netlist2schematic.layout.layout_quality.evaluate_quality',return_value=dict(score=1,per_net={})), \
             redirect_stdout(io.StringIO()):
            aesthetic_flow.select_layout('unused',column_layout='auto')
        self.assertEqual(events,['columns','series','route']);self.assertEqual(routed.call_count,1)
        self.assertEqual(budgets['columns']['max_trials'],128)
        self.assertEqual(budgets['series']['max_trials'],121)

    def test_absent_passives_do_not_reduce_active_refinement_allowance(self):
        from netlist2schematic.flow import aesthetic_flow
        from netlist2schematic.layout import structured_placement,series_alignment
        from netlist2schematic.plugins.columns import column_layout
        dev={'a':mos('x','g1'),'b':mos('y','g2')}
        pl,cs,ns,ix=base_placement(dev,dict(a=(1,1),b=(1,3)))
        data=dict(layout_style='structured',candidate_id=2);limits=[]
        def columns(par,pl,cs,ns,ix,data,model,*a,**kw):
            limits.append(kw['max_trials']);return pl,dict(data,layout_columns=dict(plan=model,trials=7))
        with patch.object(structured_placement,'create_candidates',return_value=iter([(pl,cs,ns,ix,data)])), \
             patch.object(column_layout,'refine_columns',side_effect=columns), \
             patch.object(embedding,'refine',side_effect=AssertionError('No passive proposals without passives')), \
             patch.object(aesthetic_flow,'final_route',return_value=([],dict(wirelength=1000,crossings=0,bends=0),dict(valid=True))), \
             patch('netlist2schematic.layout.layout_quality.evaluate_quality',return_value=dict(score=1,per_net={})), \
             redirect_stdout(io.StringIO()):
            aesthetic_flow.select_layout('unused',column_layout='auto')
        self.assertEqual(limits,[128])

    def test_no_passives_are_explicitly_not_applicable(self):
        dev={'a':mos('x','g')};pl,cs,ns,ix=base_placement(dev,dict(a=(1,1)))
        new,meta=run((pl,cs,ns,ix,{}))
        self.assertIs(new,pl);self.assertEqual(meta['passive_embedding']['stop_reason'],'no_passive_groups')


class SafePostrouteTests(unittest.TestCase):
    def setUp(self):
        from netlist2schematic.plugins.passive_embedding import safe_postroute
        self.safe=safe_postroute
        self.case=circuit()
        self.trial,self.proposal=run(self.case)
        self.assertTrue(self.proposal['passive_embedding']['applied'])
        self.original=([('a',[((0,0),(100,0))])],dict(wirelength=1000,crossings=0,bends=8),dict(valid=True))

    def optimize(self, metrics=None, *, route_error=None, **kwargs):
        pl,cs,ns,ix,data=self.case
        candidate=([('a',[((0,0),(90,0))])],metrics or dict(wirelength=950,crossings=0,bends=7),dict(valid=True))
        with patch.object(self.safe,'validate_final_routes') as validated, \
             patch.object(self.safe,'refine',return_value=(self.trial,deepcopy(self.proposal))) as refined, \
             patch.object(self.safe,'final_route',side_effect=route_error,return_value=candidate) as routed:
            result=self.safe.optimize(PAR,pl,cs,ns,ix,data,self.original,seconds=5.,**kwargs)
        return result,(validated,refined,routed)

    def test_actual_final_route_regression_restores_exact_incumbent(self):
        for wire,cross,bends in ((1100,0,7),(900,1,7),(900,0,9)):
            (pl,data,result),mocks=self.optimize(dict(wirelength=wire,crossings=cross,bends=bends))
            self.assertIs(pl,self.case[0]);self.assertIs(result,self.original)
            self.assertFalse(data['passive_embedding']['applied'])
            self.assertEqual(data['passive_embedding']['stop_reason'],'actual_route_regression')
            self.assertEqual(mocks[2].call_count,1)

    def test_improved_real_wires_and_organization_commit_without_touching_input(self):
        old=deepcopy(self.case[-1]);cells=list(self.case[0].cell_of)
        (pl,data,result),mocks=self.optimize()
        self.assertIs(pl,self.trial);self.assertTrue(data['passive_embedding']['applied'])
        self.assertEqual(result[1]['wirelength'],950)
        self.assertEqual(self.case[-1],old);self.assertEqual(self.case[0].cell_of,cells)
        for name in ('upper','lower'):
            i=self.case[3][name]
            self.assertEqual((pl.pos_of(i),pl.orient[i]),(self.case[0].pos_of(i),self.case[0].orient[i]))
        deadline=mocks[0].call_args.kwargs['deadline']
        self.assertEqual(mocks[1].call_args.kwargs['deadline'],deadline)
        self.assertEqual(mocks[2].call_args.kwargs['deadline'],deadline)
        self.assertEqual(mocks[1].call_args.kwargs['max_trials'],128)

    def test_route_failure_and_timeout_roll_back_without_another_attempt(self):
        from netlist2schematic.routing.orthogonal_router import RoutingFailure
        for reason in ('no_path','quality_time_budget'):
            (pl,data,result),mocks=self.optimize(route_error=RoutingFailure('fail',dict(reason=reason)))
            self.assertIs(pl,self.case[0]);self.assertIs(result,self.original)
            self.assertEqual(mocks[2].call_count,1)
            self.assertEqual(data['passive_embedding']['diagnostics']['reason'],reason)

    def test_initial_invalid_routes_are_not_hidden_by_the_optional_plugin(self):
        from netlist2schematic.routing.sa_routing import RoutingValidationError
        pl,cs,ns,ix,data=self.case
        with patch.object(self.safe,'validate_final_routes',side_effect=RoutingValidationError('bad',{})), \
             patch.object(self.safe,'final_route') as route, self.assertRaises(RoutingValidationError):
            self.safe.optimize(PAR,pl,cs,ns,ix,data,self.original)
        route.assert_not_called()

    def test_cleanup_runs_before_final_acceptance_and_cannot_mask_regression(self):
        calls=[]
        def cleanup(data,pl,routed,remaining):
            calls.append(remaining)
            return data,(routed[0],dict(wirelength=900,crossings=0,bends=9),routed[2])
        (pl,data,result),_=self.optimize(postprocess=cleanup)
        self.assertIs(result,self.original)
        self.assertFalse(data['passive_embedding']['applied']);self.assertEqual(len(calls),1)
        self.assertGreater(calls[0],0);self.assertLessEqual(calls[0],5.)

    def test_deadline_after_cleanup_keeps_the_complete_original(self):
        def cleanup(data,pl,routed,remaining):
            # Advance only this module's clock; never sleep in a unit test.
            self.clock.return_value=20.
            return data,routed
        with patch.object(self.safe,'perf_counter',return_value=10.) as self.clock:
            (pl,data,result),_=self.optimize(postprocess=cleanup)
        self.assertIs(result,self.original)
        self.assertEqual(data['passive_embedding']['stop_reason'],'budget_after_proposed_route')

    def test_no_proposal_or_zero_budget_does_not_call_astar(self):
        pl,cs,ns,ix,data=self.case
        for zero in (True,False):
            with patch.object(self.safe,'validate_final_routes'), \
                 patch.object(self.safe,'refine',return_value=(pl,dict(data,passive_embedding=dict(trials=0,applied=False)))), \
                 patch.object(self.safe,'final_route') as route:
                new,meta,result=self.safe.optimize(PAR,pl,cs,ns,ix,data,self.original,seconds=0 if zero else 5)
            self.assertIs(result,self.original);self.assertIs(new,pl);route.assert_not_called()


if __name__=='__main__':unittest.main()
