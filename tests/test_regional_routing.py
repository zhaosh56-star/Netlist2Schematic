"""Geometry-preserving routing on organized frames, with no size threshold."""
from collections import Counter
from copy import deepcopy
from time import perf_counter
import unittest
from unittest.mock import patch

from netlist2schematic.routing import orthogonal_router as router
from netlist2schematic.routing import regional_routing as regional
from netlist2schematic.routing.routing_rules import RoutingOptions,analyze_routes,build_scene
from netlist2schematic.plugins.organization.route_preparation import prepare
from netlist2schematic.plugins.organization import branch_layout
from netlist2schematic.layout import structured_placement as sp
from test_orthogonal_router import simple_scene
from test_local_rescue import scene_for,statistics
from test_branch_organization import frame,mos


def enable(scene,active=True):
    scene.branch_routing=dict(version=regional.VERSION,active=active,ownership={})
    scene.component_names={p.component:str(p.component) for pins in scene.pins.values() for p in pins}
    return scene


class RegionalSearchTests(unittest.TestCase):
    def test_policy_switch_preserves_local_seed_and_requeues_global_wires(self):
        from netlist2schematic.routing.routing_rules import Pin
        scene=enable(simple_scene({'g':[(0,0),(50,0),(100,0)],'trigger':[(0,60),(100,60)],
                                  'local':[(0,200),(100,200)]}),active=False)
        scene.component_names={0:'a',1:'b',2:'c'}
        scene.branch_routing['ownership']={'a':'left','b':'right','c':'left'}
        scene.pins['trigger']=[Pin('trigger',0,'D',(0,60),(0,60),(1,0)),Pin('trigger',1,'S',(100,60),(100,60),(1,0))]
        scene.pins['local']=[Pin('local',0,'G',(0,200),(0,200),(1,0)),Pin('local',0,'D',(100,200),(100,200),(1,0))]
        calls=[]
        def net_route(sc,net,*args,**kwargs):
            calls.append(net)
            if net=='trigger' and not sc.branch_routing['active']:
                sc.branch_routing['active']=True
                return None,Counter({'g':1})
            points=[p.point for p in sc.pins[net]]
            return [(min(points),max(points))],Counter()
        initial={'local':[((0,200),(100,200))]}
        with patch.object(router,'_route_net',side_effect=net_route):result,report=router.route_scene(scene,initial_routes=initial)
        self.assertEqual(calls,['g','trigger','trigger','g'])
        self.assertEqual(report['regional_transitions'][0]['preserved'],['local'])
        self.assertEqual(report['regional_transitions'][0]['rerouted'],['g'])
        self.assertEqual(dict(result)['local'],initial['local'])
        self.assertFalse(analyze_routes(result,scene)['violations'])

    def test_successful_normal_route_keeps_the_original_path_and_order(self):
        plain=simple_scene({'a':[(0,0),(100,0)],'b':[(0,40),(100,40)]})
        old,_=router.route_scene(plain)
        guarded=enable(deepcopy(plain),active=False)
        new,report=router.route_scene(guarded,deadline=perf_counter()+3)
        self.assertEqual(old,new)
        self.assertFalse(report['regional_search']['activated'])
    def test_small_scene_uses_same_policy_and_preserves_original_pins(self):
        scene=enable(simple_scene({'a':[(0,0),(100,0),(100,100)]},[(20,-10,40,30)]))
        first,report=router.route_scene(scene,deadline=perf_counter()+5)
        self.assertFalse(analyze_routes(first,scene)['violations'])
        self.assertIn('regional_search',report)
        second,_=router.route_scene(scene,deadline=perf_counter()+5)
        self.assertEqual(first,second)

    def test_local_windows_keep_global_obstacles_and_expand_if_needed(self):
        scene=enable(simple_scene({'a':[(0,0),(100,0)]},[(40,-210,60,210)],options=RoutingOptions(margins=(300,))))
        scene.bounds=(-20,-250,120,250)
        result,report=router.route_scene(scene,deadline=perf_counter()+5)
        self.assertFalse(analyze_routes(result,scene)['violations'])
        self.assertTrue(any(r['radius'] is None and r['found'] for r in report['regional_search']['windows']))
        self.assertTrue(any(abs(p[1])>210 for _,runs in result for segment in runs for p in segment))

    def test_no_safe_goal_does_not_expand_astar(self):
        scene=simple_scene({'a':[(0,0),(0,100)]})
        def vertex(net,point,prepared):return (point[1]!=0,0,{'foreign'} if point[1]==0 else set())
        counts=statistics()
        with patch.object(scene,'vertex_check',side_effect=vertex),patch.object(scene,'edge_check',side_effect=AssertionError('empty-goal search')):
            path,blocked=router._find_path(scene,'a',(0,100),[((0,0),(20,0))],{},100,[],counts,precheck_goals=True)
        self.assertIsNone(path);self.assertEqual(counts['expanded_states'],0)
        self.assertIn('foreign',blocked);self.assertEqual(counts['branch_rejection'],'no_safe_goal')

    def test_failed_branch_repairs_interfaces_without_discarding_connected_tree(self):
        scene,occupied=scene_for();enable(scene);before=deepcopy(scene.fixed_segments)
        result,blocked=router._route_net(scene,'a',occupied,80,[],counts:=statistics(),deadline=perf_counter()+5)
        self.assertIsNotNone(result)
        self.assertFalse(analyze_routes([('a',result),('b',occupied['b'])],scene)['violations'])
        self.assertEqual(scene.fixed_segments,before)
        repairs=counts['regional_search']['interface_repairs']
        self.assertTrue(repairs[0]['repaired']);self.assertGreater(repairs[0]['retained_segments'],0)

    def test_history_duplicates_cannot_accumulate_unbounded_penalty(self):
        boxes=[(-10,-10,60,10),(20,-10,90,10)]
        self.assertEqual(regional.bounded_history((0,0),(100,0),boxes),180)
        self.assertEqual(regional.bounded_history((0,0),(100,0),boxes*8),180)

    def test_original_scene_does_not_call_regional_search(self):
        scene=simple_scene({'a':[(0,0),(100,0)]})
        with patch.object(regional,'find_path',side_effect=AssertionError('off changed')):
            result,report=router.route_scene(scene)
        self.assertFalse(analyze_routes(result,scene)['violations'])
        self.assertNotIn('regional_search',report)

    def test_actual_channel_interfaces_rank_after_local_nets_before_shared_controls(self):
        from netlist2schematic.routing.routing_rules import Pin
        s=enable(simple_scene({'local':[(0,0),(20,0)],'channel':[(0,40),(20,40)],'control':[(0,80),(20,80)]}))
        s.component_names={0:'a',1:'b'};s.branch_routing['ownership']={'a':'left','b':'right'}
        s.pins['channel']=[Pin('channel',0,'D',(0,40),(0,40),(1,0)),Pin('channel',1,'S',(20,40),(20,40),(1,0))]
        s.pins['local']=[Pin('local',0,'G',(0,0),(0,0),(1,0)),Pin('local',0,'D',(20,0),(20,0),(1,0))]
        self.assertEqual(router._priority(s,'local')[0],0)
        self.assertEqual(router._priority(s,'channel')[0],1)
        self.assertEqual(router._priority(s,'control')[0],2)

    def test_elapsed_global_deadline_is_not_replaced_by_local_budget(self):
        s=enable(simple_scene({'a':[(0,0),(100,0)]}))
        with self.assertRaises(router.RoutingFailure):router.route_scene(s,deadline=perf_counter()-1)


class ShapePreparationTests(unittest.TestCase):
    def scene(self):
        dev=dict(upper=mos('a','ga','0'),lower=mos('b','gb','0'))
        pl,cc,nn,ii,ss=frame(dev,dict(upper=(0,1),lower=(1,1)))
        pl.row_y=[64,170]
        plan=branch_layout.build_plan(sp.par,pl,cc,nn,ii,dev,ss)
        return pl,cc,nn,ii,dict(layout_branches=dict(applied=True,plan=plan))

    def test_marker_body_collision_gets_only_minimal_gap_and_all_static_checks(self):
        from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
        pl,cc,nn,ii,meta=self.scene();old=deepcopy(pl.row_y)
        before=_static_faults(sp.par,pl,nn,ii,RoutingOptions(),perf_counter()+2)
        self.assertTrue(before['count'])
        result,data=prepare(sp.par,pl,cc,nn,ii,meta,RoutingOptions())
        self.assertTrue(data['layout_branches']['route_preparation']['applied'])
        self.assertFalse(_static_faults(sp.par,result,nn,ii,RoutingOptions(),perf_counter()+2)['count'])
        self.assertEqual(pl.row_y,old);self.assertEqual(result.cell_of,pl.cell_of);self.assertEqual(result.orient,pl.orient)
        self.assertLessEqual(result.row_y[-1]-old[-1],6)

    def test_fixed_lower_component_prevents_axis_motion(self):
        pl,cc,nn,ii,meta=self.scene();pl.fixed={ii['lower']}
        result,data=prepare(sp.par,pl,cc,nn,ii,meta,RoutingOptions())
        self.assertEqual(result.row_y,pl.row_y)
        self.assertFalse(data['layout_branches']['route_preparation']['applied'])

    def test_inserted_module_is_not_stretched(self):
        pl,cc,nn,ii,meta=self.scene();pl.passive_module_guides={'module':{'internal_routes':{'a':[((0,0),(20,0))]}}}
        result,data=prepare(sp.par,pl,cc,nn,ii,meta,RoutingOptions())
        self.assertEqual(result.row_y,pl.row_y);self.assertEqual(result.passive_module_guides,pl.passive_module_guides)
        self.assertEqual(data['layout_branches']['route_preparation']['stop_reason'],'inserted_module_geometry_locked')

    def test_zero_budget_preserves_frame(self):
        pl,cc,nn,ii,meta=self.scene()
        result,data=prepare(sp.par,pl,cc,nn,ii,meta,RoutingOptions(),seconds=0)
        self.assertEqual(result.row_y,pl.row_y);self.assertFalse(data['layout_branches']['route_preparation']['applied'])

    def test_partial_repair_is_rolled_back_on_local_timeout(self):
        pl,cc,nn,ii,meta=self.scene()
        with patch('netlist2schematic.plugins.organization.route_preparation._static_faults',
                   side_effect=router.RoutingFailure('timeout',dict(reason='quality_time_budget'))):
            result,data=prepare(sp.par,pl,cc,nn,ii,meta,RoutingOptions())
        self.assertEqual(result.row_y,pl.row_y)
        self.assertFalse(data['layout_branches']['route_preparation']['applied'])


if __name__=='__main__':unittest.main()
