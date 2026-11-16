"""Boundary projections use real geometry, old quotas and independent acceptance."""
from copy import deepcopy
from math import floor
from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.test_passive_placement import convert,mos
from tests.test_attachment_tail_transistors import fixture as ordinary_fixture,finish as ordinary_finish
from netlist2schematic.plugins.attachment import attachment_tail_boundaries as bounds
from netlist2schematic.plugins.attachment import attachment_tail_transistors as tails
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl,_bounds
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.layout.structured_placement import par
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.routing.orthogonal_router import RoutingFailure


def blocked_fixture(*,bjt=False,dual=False,rename=False,mirror=False,translated=False,grid=2):
    devices=dict(a=mos('oa','ga','tailnet'),b=mos('ob','gb','tailnet'),
        tail=mos('tailnet','bias'),neighbour=mos('independent','other_control'))
    cells=dict(a=(1,3),b=(1,4),tail=(2,4),neighbour=(2,3))
    if bjt:
        devices={n:dict(cls='BJT',type='NPN',model='N',c=d['d'],b=d['g'],e=d['s']) for n,d in devices.items()}
    if dual:
        for d in devices.values():
            d['type']='PNP' if bjt else 'PMOS'
            d['e' if bjt else 's']='vdd' if d['e' if bjt else 's']=='0' else d['e' if bjt else 's']
            if not bjt:d['bulk']='vdd'
        cells={n:(3-r,c) for n,(r,c) in cells.items()}
    names={n:n for n in devices}
    if rename:
        names={n:'instance'+str(70-k) for k,n in enumerate(devices)}
        devices={names[n]:{k:'wire_'+v if k in ('d','g','s','c','b','e') and v not in ('0','vdd') else v
            for k,v in d.items()} for n,d in reversed(list(devices.items()))}
        cells={names[n]:cell for n,cell in cells.items()}
    pl,c,n,i,_=convert(devices,cells);pl.orient[i[names['neighbour']]]=1
    pl.offset_x=[0.]*len(c)
    pitch=pl.column_x[1]-pl.column_x[0]
    pl.offset_x[i[names['neighbour']]]=floor(pitch/4/grid)*grid
    if mirror:
        pl.set_state([(r,pl.cols-1-col) for r,col in pl.cell_of],[1-o for o in pl.orient])
        pl.offset_x=[-v for v in pl.offset_x]
    if translated:
        pl.column_x=[x+73 for x in pl.column_x];pl.row_y=[y-29 for y in pl.row_y]
    plan=ai.build_attachment_plan(devices,c,n,i,analyze_structures(devices),compute_levels(devices),pl.row_of,policy='all')
    return ai._install(pl,c,i,plan),c,n,i,plan,names


def diagnostic():return dict(scene_templates=0,forbidden_intervals=0,generated=0,stop_reason='not_started')


_REAL=object()

def finish(f,*,options=None,safe=_REAL,deadline=None,used=124):
    pl,c,n,i,plan=f[:5];options=options or RoutingOptions();b=ai.new_attachment_budget();b.update(candidates=used,spent_seconds=.3)
    tails.reserve_finish(plan,b,pl,i);r=dict(candidates=0,accepted=0,actions=[])
    args=(par,pl,c,n,i,plan,options,b,perf_counter()+1 if deadline is None else deadline,r,{})
    with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar forbidden')):
        if safe is _REAL:result=tails.finish_tails(*args,hpwl_limit=_hpwl(par,pl,n,i)*1.15)
        else:
            with patch.object(ai,'_checked_static',return_value=safe):
                result=tails.finish_tails(*args,hpwl_limit=_hpwl(par,pl,n,i)*1.15)
    return result,b,r


class TailBoundaryTests(unittest.TestCase):
    def test_free_integer_bands_merge_constraints_without_scanning_positions(self):
        self.assertEqual(bounds._free_bands(-4,4,[(-3,-2),(-2,0),(2,5)]),[(-4,-4),(1,1)])
        self.assertEqual(bounds._free_bands(-2,2,[(-10,10)]),[])
        self.assertEqual(bounds._free_bands(-2,2,[]),[(-2,2)])

    def test_foreign_marker_on_same_power_net_is_protected_but_own_marker_moves_with_tail(self):
        pl=SimpleNamespace(cell_of=[(0,0),(0,1)],column_x=[0,110],offset_x=[0.,0.],
            pin_world=lambda j,p:(70,-30) if j==0 else (110,10))
        def scene(foreign):return SimpleNamespace(power_connections=None,body_rects=[(1,(100,-10,120,10))],
            marker_rects=[('0',foreign),('0',(100,30,120,40))],power_nets={'0'},pins={},
            fixed_segments={'0':[((70,-30),(70,-20)),((110,10),(110,30))]})
        params=SimpleNamespace(power_marker_sym=lambda n:'gnd' if n=='0' else None)
        nets={'0':[('other','E'),('tail','S')]};index={'other':0,'tail':1}
        with patch.object(bounds,'build_scene',return_value=scene((60,-10,80,10))):
            near=bounds._geometry_bands(params,pl,nets,index,1,RoutingOptions(),perf_counter()+1,diagnostic())
        with patch.object(bounds,'build_scene',return_value=scene((-100,-10,-80,10))):
            far=bounds._geometry_bands(params,pl,nets,index,1,RoutingOptions(),perf_counter()+1,diagnostic())
        self.assertGreater(near[0][0],far[0][0]);self.assertEqual(near[0][0],-10)

    def test_moving_power_marker_cannot_enter_a_foreign_body(self):
        pl=SimpleNamespace(cell_of=[(0,0),(0,1)],column_x=[0,110],offset_x=[0.,0.],
            pin_world=lambda j,p:(70,-30) if j==0 else (110,10))
        scene=SimpleNamespace(power_connections=None,body_rects=[(1,(100,-10,120,10)),(0,(60,35,80,45))],
            marker_rects=[('0',(-100,-10,-80,10)),('0',(100,30,120,40))],power_nets={'0'},pins={},
            fixed_segments={'0':[((70,-30),(70,-20)),((110,10),(110,30))]})
        params=SimpleNamespace(power_marker_sym=lambda n:'gnd' if n=='0' else None)
        with patch.object(bounds,'build_scene',return_value=scene):
            free=bounds._geometry_bands(params,pl,{'0':[('other','E'),('tail','S')]},
                {'other':0,'tail':1},1,RoutingOptions(),perf_counter()+1,diagnostic())
        self.assertEqual(free[0][0],-10)

    def test_fixture_has_safe_incumbent_but_original_improving_targets_hit_geometry(self):
        f=blocked_fixture();pl,c,n,i,plan=f[:5];g=plan['tail_transistors']['groups'][0]
        self.assertTrue(ai._static(par,pl,n,i,RoutingOptions(),perf_counter()+1))
        d=dict(normal_generated=0,normal_dominated=0)
        proposals=list(tails.tail_trials(pl,c,i,g,[g],prune_dominated=True,diagnostic=d))
        self.assertTrue(proposals);self.assertGreater(d['normal_dominated'],0)
        self.assertTrue(all(not ai._static(par,p,n,i,RoutingOptions(),perf_counter()+1) for p,a in proposals))

    def test_partial_projection_reduces_error_and_passes_actual_static_geometry(self):
        f=blocked_fixture();pl,c,n,i,plan=f[:5];model=plan['tail_transistors']
        before=tails.measure_tails(pl,c,i,model);result,b,r=finish(f)
        after=tails.measure_tails(result,c,i,model)
        self.assertTrue(result.valid_state());self.assertTrue(ai._static(par,result,n,i,RoutingOptions(),perf_counter()+1))
        self.assertGreater(after['axis_error'],0);self.assertLess(after['axis_error'],before['axis_error'])
        self.assertEqual(r['tail_transistor_finish']['actions'][0]['kind'],'exclusive_tail_partial_axis')
        self.assertLessEqual(r['candidates'],4);self.assertEqual(b['candidates'],124+r['candidates'])

    def test_only_tail_offset_or_mirror_changes_and_every_other_state_is_preserved(self):
        f=blocked_fixture();pl,c,n,i,plan,names=f;before=deepcopy((pl.cell_of,pl.orient,pl.offset_x,pl.column_x,pl.row_y,pl.rng.getstate()))
        result,b,r=finish(f);j=i[names['tail']]
        self.assertEqual(result.cell_of,pl.cell_of);self.assertEqual(result.column_x,pl.column_x);self.assertEqual(result.row_y,pl.row_y)
        for k in set(range(len(c)))-{j}:
            self.assertEqual((result.orient[k],result.pos_of(k)),(pl.orient[k],pl.pos_of(k)))
        self.assertEqual((pl.cell_of,pl.orient,pl.offset_x,pl.column_x,pl.row_y,pl.rng.getstate()),before)
        lo,hi=_bounds(result,j);self.assertLessEqual(lo,result.offset_x[j]);self.assertLessEqual(result.offset_x[j],hi)

    def test_MOS_BJT_and_polarity_dual_use_same_real_geometry_rule(self):
        for bjt in (False,True):
            for dual in (False,True):
                f=blocked_fixture(bjt=bjt,dual=dual);result,b,r=finish(f)
                with self.subTest(bjt=bjt,dual=dual):
                    self.assertGreater(r['accepted'],0)
                    self.assertTrue(ai._static(par,result,f[2],f[3],RoutingOptions(),perf_counter()+1))

    def test_renaming_order_translation_and_horizontal_mirror_preserve_improvement(self):
        errors=[]
        for rename,mirror,translated in ((False,False,False),(True,False,True),(True,True,True)):
            f=blocked_fixture(rename=rename,mirror=mirror,translated=translated);result,b,r=finish(f)
            self.assertGreater(r['accepted'],0);self.assertTrue(result.valid_state())
            errors.append(tails.measure_tails(result,f[1],f[3],f[4]['tail_transistors'])['axis_error'])
        self.assertAlmostEqual(max(errors),min(errors))

    def test_custom_grid_and_clearance_are_used_without_new_magic_offset(self):
        f=blocked_fixture(grid=8);opts=RoutingOptions(grid_step=8,cross_clearance=16)
        result,b,r=finish(f,options=opts)
        self.assertGreater(r['accepted'],0)
        self.assertEqual(result.offset_x[f[3][f[5]['tail']]]%8,0)
        self.assertTrue(ai._static(par,result,f[2],f[3],opts,perf_counter()+1))

    def test_old_success_never_calls_partial_generator(self):
        f=ordinary_fixture()
        with patch.object(bounds,'partial_tail_trials',side_effect=AssertionError('Old success must win')):
            result,b,r=ordinary_finish(f)
        self.assertEqual(tails.measure_tails(result,f[1],f[3],f[4]['tail_transistors'])['axis_error'],0)
        self.assertEqual(r['tail_transistor_finish']['generation']['partial_groups'],[])

    def test_dominance_pruning_keeps_every_old_potential_gain_including_fixed_peers(self):
        f=ordinary_fixture();pl,c,n,i,plan=f;pl.fixed.add(i['v'])
        groups,_=tails.eligible_groups(pl,i,plan['tail_transistors']);g=groups[0]
        old=list(tails.tail_trials(pl,c,i,g,groups));before=tails.measure_tails(pl,c,i,plan['tail_transistors'])
        diag=dict(normal_generated=0,normal_dominated=0)
        new=list(tails.tail_trials(pl,c,i,g,groups,prune_dominated=True,diagnostic=diag,measurement_model=plan['tail_transistors']))
        identities={tuple(p.cell_of)+tuple(p.orient)+tuple(p.offset_x) for p,a in new}
        previous={r['tail']:r['axis_error'] for r in before['records']}
        for trial,action in old:
            m=tails.measure_tails(trial,c,i,plan['tail_transistors'])
            if tails.tail_rank(m)<tails.tail_rank(before) and all(r['axis_error']<=previous[r['tail']]+tails.EPS for r in m['records'] if r['tail'] in action['tails']):
                self.assertIn(tuple(trial.cell_of)+tuple(trial.orient)+tuple(trial.offset_x),identities)

    def test_generation_builds_two_templates_and_yields_at_most_two_unvalidated_trials(self):
        f=blocked_fixture();pl,c,n,i,plan=f[:5];g=plan['tail_transistors']['groups'][0];d=diagnostic()
        with patch.object(bounds,'build_scene',wraps=bounds.build_scene) as scene, \
             patch.object(ai,'_checked_static',side_effect=AssertionError('No hidden candidate validation')):
            p=list(bounds.partial_tail_trials(par,pl,c,n,i,g,options=RoutingOptions(),deadline=perf_counter()+1,diagnostic=d))
        self.assertGreater(len(p),0);self.assertLessEqual(len(p),2);self.assertEqual(scene.call_count,2)
        self.assertEqual(d['generated'],len(p));self.assertGreater(d['forbidden_intervals'],0)

    def test_old_rejected_positions_are_not_generated_again_in_same_scene(self):
        f=blocked_fixture();pl,c,n,i,plan=f[:5];g=plan['tail_transistors']['groups'][0]
        first=list(bounds.partial_tail_trials(par,pl,c,n,i,g,options=RoutingOptions(),deadline=perf_counter()+1,diagnostic=diagnostic()))
        excluded={(p.orient[i[g['tail']]],p.offset_x[i[g['tail']]]) for p,a in first}
        second=list(bounds.partial_tail_trials(par,pl,c,n,i,g,options=RoutingOptions(),deadline=perf_counter()+1,diagnostic=diagnostic(),excluded=excluded))
        self.assertFalse(excluded&{(p.orient[i[g['tail']]],p.offset_x[i[g['tail']]]) for p,a in second})

    def test_zero_limit_expired_deadline_and_fixed_tail_do_not_build_scene(self):
        for mode in ('limit','deadline','fixed'):
            f=blocked_fixture();pl,c,n,i,plan=f[:5];g=plan['tail_transistors']['groups'][0];d=diagnostic()
            if mode=='fixed':pl.fixed.add(i[g['tail']])
            with patch.object(bounds,'build_scene',side_effect=AssertionError('No generation allowance')):
                if mode=='deadline':
                    with self.assertRaises(RoutingFailure):list(bounds.partial_tail_trials(par,pl,c,n,i,g,options=RoutingOptions(),deadline=0,diagnostic=d))
                else:self.assertEqual(list(bounds.partial_tail_trials(par,pl,c,n,i,g,options=RoutingOptions(),deadline=perf_counter()+1,diagnostic=d,limit=0 if mode=='limit' else 2)),[])

    def test_generation_timeout_retains_incumbent_without_spending_fresh_quota(self):
        f=blocked_fixture();old=deepcopy((f[0].cell_of,f[0].orient,f[0].offset_x))
        error=RoutingFailure('Stopped',dict(reason='quality_time_budget'))
        with patch.object(bounds,'build_scene',side_effect=error):result,b,r=finish(f,safe=False)
        self.assertIs(result,f[0]);self.assertTrue(b['exhausted']);self.assertLessEqual(r['candidates'],4)
        self.assertEqual((result.cell_of,result.orient,result.offset_x),old)

    def test_static_rejection_or_late_validation_cannot_move_a_tail(self):
        for verdict in (False,None):
            f=blocked_fixture();old=deepcopy((f[0].cell_of,f[0].orient,f[0].offset_x))
            result,b,r=finish(f,safe=verdict)
            self.assertIs(result,f[0]);self.assertEqual(r['accepted'],0)
            self.assertEqual((result.cell_of,result.orient,result.offset_x),old)

    def test_non_budget_resource_error_is_not_silently_converted_to_no_improvement(self):
        f=blocked_fixture()
        with patch.object(bounds,'build_scene',side_effect=KeyError('broken symbol')):
            with self.assertRaises(KeyError):finish(f,safe=False)

    def test_custom_saved_power_geometry_is_skipped_not_reinterpreted(self):
        f=blocked_fixture();pl,c,n,i,plan=f[:5];g=plan['tail_transistors']['groups'][0];d=diagnostic()
        with patch.object(bounds,'build_scene',return_value=SimpleNamespace(power_connections=[])):
            self.assertEqual(list(bounds.partial_tail_trials(par,pl,c,n,i,g,options=RoutingOptions(),deadline=perf_counter()+1,diagnostic=d)),[])
        self.assertEqual(d['scene_templates'],2)

    def test_fixed_or_runtime_owned_tail_is_not_rescued(self):
        for mode in ('fixed','owner'):
            f=blocked_fixture();pl,c,n,i,plan,names=f;j=i[names['tail']]
            if mode=='fixed':pl.fixed.add(j)
            else:pl.layout_macro_blocks=[[j,i[names['neighbour']]]]
            with patch.object(bounds,'partial_tail_trials',side_effect=AssertionError('Protected tail')):
                result,b,r=finish(f)
            self.assertIs(result,pl);self.assertEqual(r['candidates'],0)

    def test_whole_candidate_budget_and_zero_time_never_restart(self):
        for used,deadline in ((128,None),(124,0)):
            f=blocked_fixture()
            with patch.object(bounds,'partial_tail_trials',side_effect=AssertionError('No budget')):
                result,b,r=finish(f,used=used,deadline=deadline)
            self.assertIs(result,f[0]);self.assertEqual(r['candidates'],0)

    def test_partial_result_is_stable_under_repeated_finish(self):
        f=blocked_fixture();result,b,r=finish(f)
        second=(result,*f[1:]);again,bb,rr=finish(second)
        self.assertEqual((result.cell_of,result.orient,result.offset_x),(again.cell_of,again.orient,again.offset_x))
        self.assertEqual(rr['accepted'],0)


if __name__=='__main__':unittest.main()
