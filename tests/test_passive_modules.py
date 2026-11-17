"""Generic two-port composition, real-node retention and skeleton rollback."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from collections import Counter

from tests.test_passive_placement import base_placement, mos, resistor
from tests.test_series_alignment import PAR
from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.plugins.passive_skeleton.modules import signature, reverse, members
from netlist2schematic.plugins.passive_skeleton.plan import compile_plan, measure
from netlist2schematic.plugins.passive_skeleton.placement import pack, make_frame, _geometry, _translated, reserve, slot_trials
from netlist2schematic.plugins.passive_skeleton.module_connections import complete_routes, check_guides
from netlist2schematic.routing.routing_rules import RoutingOptions, build_scene
from netlist2schematic.routing.sa_routing import final_route, validate_final_routes, RoutingValidationError


def nested():
    devices=dict(a=mos('left','ga'),b=mos('right','gb'),
                 r=resistor('left','join'),c=resistor('join','right','C'),
                 r2=resistor('join','middle'),l=resistor('middle','right','L'))
    return devices


def fixture(devices=None):
    devices=nested() if devices is None else devices
    document=to_document(devices);cs,ns=document['components'],document['nets']
    ix={c['id']:i for i,c in enumerate(cs)}
    active={n:d for n,d in devices.items() if d['cls']!='PASSIVE'}
    cells={name:(1,1+3*k) for k,name in enumerate(active)}
    core,*_=base_placement(active,cells)
    plan=compile_plan(PAR,devices,cs,ns,[])
    return devices,core,cs,ns,ix,plan


class ModuleEvidenceTests(unittest.TestCase):
    def test_nested_series_parallel_is_one_module_with_each_real_part_once(self):
        dev,core,cs,ns,ix,p=fixture();g=p['groups'][0]
        self.assertEqual(p['covered_count'],4);self.assertEqual(len(p['groups']),1)
        self.assertEqual(g['kind'],'series_parallel')
        self.assertEqual(g['module']['kind'],'series')
        self.assertEqual(sorted(members(g['module'])),['c','l','r','r2'])
        self.assertEqual(len(members(g['module'])),len(set(members(g['module']))))
        self.assertEqual(g['internal_nets'],['join','middle'])
        self.assertEqual(g['electrical_edges'][0]['pins'],cs[ix[g['electrical_edges'][0]['device']]]['nodes'])
        self.assertEqual(p['electrical_node_merges'],[])

    def test_same_rail_does_not_join_independent_modules(self):
        dev=dict(a=mos('x','ga'),b=mos('y','gb'),r=resistor('vdd','x'),c=resistor('vdd','y','C'))
        *_,p=fixture(dev)
        self.assertEqual(len(p['groups']),2)
        self.assertEqual({frozenset(g['members']) for g in p['groups']},{frozenset(['r']),frozenset(['c'])})

    def test_external_control_tap_is_preserved_as_a_boundary(self):
        dev=nested();dev['tap']=mos('other','join')
        *_,p=fixture(dev)
        self.assertEqual(p['covered_count'],4)
        self.assertEqual(len(p['groups']),2)
        self.assertTrue(all('join' not in g['internal_nets'] for g in p['groups']))
        self.assertTrue(all('join' in g['ends'] for g in p['groups']))

    def test_non_series_parallel_bridge_is_not_arbitrarily_shortened(self):
        dev=dict(a=mos('left','ga'),b=mos('right','gb'))
        for k,(a,b) in enumerate((('left','x'),('x','right'),('left','y'),('y','right'),('x','y'))):
            dev['p'+str(k)]=resistor(a,b)
        *_,p=fixture(dev)
        self.assertFalse(p['applicable']);self.assertEqual(p['groups'],[])
        self.assertEqual(p['skipped'][0]['reason'],'not_series_parallel')

    def test_short_circuited_passive_is_not_dropped_from_coverage(self):
        dev=nested();dev['short']=resistor('left','left')
        *_,p=fixture(dev)
        self.assertFalse(p['applicable']);self.assertEqual(p['passive_count'],5)
        self.assertTrue(any(s['reason']=='not_a_distinct_two_terminal_part' for s in p['skipped']))

    def test_instance_net_renaming_and_line_reordering_preserve_module_topology(self):
        dev,_,_,_,_,p=fixture();names={n:'part'+str(i) for i,n in enumerate(dev)}
        rename=lambda n:n if n=='0' else 'n_'+n[::-1]
        renamed={names[n]:{k:rename(v) if k in ('a','b','d','g','s','bulk') else v for k,v in d.items()}
                 for n,d in reversed(list(dev.items()))}
        *_,q=fixture(renamed)
        key=lambda g:min(signature(g['module']),signature(reverse(g['module'])))
        self.assertEqual(sorted(map(key,p['groups'])),sorted(map(key,q['groups'])))
        self.assertEqual(p['covered_count'],q['covered_count'])


class ModuleInsertionTests(unittest.TestCase):
    def packed(self,options=None):
        dev,core,cs,ns,ix,p=fixture()
        pl,report=pack(PAR,core,cs,ns,ix,p,options or RoutingOptions())
        self.assertTrue(report['applied'],report)
        return dev,core,cs,ns,ix,p,pl,report

    def test_nested_module_inserts_with_ordinary_local_wires_and_unchanged_active_cells(self):
        dev,core,cs,ns,ix,p,pl,report=self.packed()
        self.assertTrue(report['active_core_preserved'])
        for i,c in enumerate(core.comps):
            self.assertEqual(pl.cell_of[ix[c['id']]],core.cell_of[i])
            self.assertEqual(pl.orient[ix[c['id']]],core.orient[i])
        self.assertEqual(check_guides(PAR,pl,ns,ix,RoutingOptions()),[])
        self.assertEqual({net for g in report['module_connections'].values() for net in g['routes']},
                         {'left','right','join','middle'})
        self.assertEqual(ns,to_document(dev)['nets'])

    def test_complete_local_nets_are_reused_without_an_astar_search(self):
        dev,core,cs,ns,ix,p,pl,report=self.packed()
        with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('local wire already complete')):
            routed,metrics,valid=final_route(PAR,ns,pl,ix)
        self.assertTrue(valid['valid'])
        self.assertEqual(valid['search']['passive_module_seed_nets'],4)
        self.assertTrue(validate_final_routes(PAR,ns,pl,ix,routed)[2]['valid'])

    def test_partial_multiterminal_external_net_is_not_misreported_complete(self):
        dev=nested();dev['tap']=mos('left','other')
        dev,core,cs,ns,ix,p=fixture(dev)
        pl,report=pack(PAR,core,cs,ns,ix,p,RoutingOptions())
        self.assertTrue(report['applied'],report)
        scene=build_scene(PAR,ns,pl,ix)
        seeds=complete_routes(PAR,pl,ns,ix,scene)
        self.assertNotIn('left',seeds)
        self.assertIn('join',seeds)

    def test_custom_rules_and_translated_inputs_still_have_legal_local_connections(self):
        dev,core,cs,ns,ix,p=fixture()
        core.column_x=[x+102 for x in core.column_x];core.row_y=[y-240 for y in core.row_y]
        options=RoutingOptions(halo=16,cross_clearance=16,edge_clearance=10)
        pl,report=pack(PAR,core,cs,ns,ix,p,options)
        self.assertTrue(report['applied'],report)
        self.assertEqual(check_guides(PAR,pl,ns,ix,options),[])

    def test_geometry_composition_supports_all_four_directions(self):
        dev,core,cs,ns,ix,p=fixture();frame=make_frame(PAR,core,cs,ix,RoutingOptions())
        for direction in ((1,0),(-1,0),(0,1),(0,-1)):
            geo=_geometry(PAR,frame,cs,ix,p['groups'][0],direction,RoutingOptions())
            trial=_translated(frame,geo,(2000.,2000.),2)
            self.assertEqual(check_guides(PAR,trial,ns,ix,RoutingOptions()),[])
            self.assertEqual(set(geo[0]),{ix[n] for n in ('r','c','r2','l')})

    def test_rejected_insertion_does_not_mutate_core_or_emit_partial_layout(self):
        dev,core,cs,ns,ix,p=fixture();before=deepcopy(core.__dict__)
        with patch('netlist2schematic.plugins.passive_skeleton.module_connections.check_guides',return_value=[dict(kind='body',nets=['join'])]):
            pl,report=pack(PAR,core,cs,ns,ix,p,RoutingOptions(),max_trials=3)
        self.assertIsNone(pl);self.assertFalse(report['applied']);self.assertEqual(report['actions'],[])
        for key in ('cell_of','orient','row_of','row_y','column_x','comp_at'):
            self.assertEqual(getattr(core,key),before[key])
        self.assertEqual(report['fallback_policy'],'retain_verified_incumbent_no_mixed_restart')

    def test_tampered_module_line_cannot_gain_a_body_exemption(self):
        dev,core,cs,ns,ix,p,pl,report=self.packed()
        g=next(iter(pl.passive_module_guides.values()))
        # Keep original connectivity but add an ordinary wire through a body.
        x,y=pl.pos_of(ix['r'])
        g['routes']['join'].append(((x-100,y),(x+100,y)))
        g['internal_routes']['join'].append(((x-100,y),(x+100,y)))
        with self.assertRaises(RoutingValidationError):
            final_route(PAR,ns,pl,ix)

    def test_nested_metrics_do_not_enumerate_or_duplicate_shared_series_parts(self):
        dev,core,cs,ns,ix,p,pl,report=self.packed()
        metrics=measure(pl,cs,ix,p)
        self.assertEqual(metrics['covered_count'],4)
        self.assertEqual(len(metrics['regularity']['groups'][0]['members']),4)
        self.assertEqual(metrics['regularity']['direction_errors'],0)

    def test_spacing_recovery_keeps_modules_rigid_and_clears_old_wires(self):
        from netlist2schematic.plugins.passive_skeleton.module_connections import rebase_after_axis_expansion
        dev,core,cs,ns,ix,p,pl,report=self.packed();trial=deepcopy(pl)
        trial.column_x=[pl.column_x[0]+1.5*(x-pl.column_x[0]) for x in pl.column_x]
        trial.row_y=[pl.row_y[0]+1.5*(y-pl.row_y[0]) for y in pl.row_y]
        shifts=rebase_after_axis_expansion(pl,trial,ix,p)
        reference=ix['r'];old=pl.pos_of(reference);new=trial.pos_of(reference)
        for name in p['groups'][0]['members']:
            self.assertEqual(tuple(trial.pos_of(ix[name])[k]-new[k] for k in (0,1)),
                             tuple(pl.pos_of(ix[name])[k]-old[k] for k in (0,1)))
        self.assertFalse(hasattr(trial,'passive_module_guides'))
        self.assertEqual(trial.cell_of,pl.cell_of);self.assertEqual(trial.orient,pl.orient)
        self.assertEqual(len(shifts),1)

    def test_passive_improvement_cannot_override_main_column_integrity(self):
        from netlist2schematic.plugins.columns.column_layout import build_column_plan
        from netlist2schematic.plugins.passive_skeleton.selection import select_result
        dev=dict(a=mos('top','ga','join'),b=mos('join','gb'),r=resistor('join','outer'))
        old,cs,ns,ix=base_placement(dev,dict(a=(1,2),b=(2,2),r=(1,0)))
        new=deepcopy(old);new.set_state([(1,2),(2,3),(1,1)],new.orient)
        p=compile_plan(PAR,dev,cs,ns,[]);cp=build_column_plan(PAR,old,cs,ns,ix,[])
        self.assertTrue(cp['supported'])
        metrics=dict(wirelength=1000,crossings=0,bends=8)
        def item(pl,ident,score):
            return ((score,0,1000,ident),pl,cs,ns,ix,{},([],metrics,dict(valid=True)),dict(score=score),ident)
        reference=item(old,'1',1.);candidate=item(new,'2',1.1)
        def regularity(pl,*args):
            return dict(inline_error=0,family_error=0,regularity=dict(direction_errors=0,
                        shape_error=10 if pl is old else 0,access_distance=0))
        with patch('netlist2schematic.plugins.passive_skeleton.selection.measure',side_effect=regularity):
            winner,report=select_result([reference,candidate],p,column_plan=cp)
        self.assertIs(winner,reference)
        self.assertTrue(report['active_structure_priority'])

    def test_failed_proposal_retains_verified_reference_without_routing_a_partial_frame(self):
        from netlist2schematic.flow import aesthetic_flow as flow
        from netlist2schematic.layout import structured_placement as sp
        dev,core,cs,ns,ix,p,pl,report=self.packed()
        original=deepcopy(pl)
        metadata=dict(candidate_id=1,layout_style='structured')
        skipped=dict(candidate_id=2,generation_skipped=True,passive_skeleton=dict(
            applied=False,plan=p,stop_reason='no_legal_slot',core_checkpoint=report['core_checkpoint']))
        records=[(pl,cs,ns,ix,metadata),(None,cs,ns,ix,skipped)]
        routed=final_route(PAR,ns,pl,ix)
        with patch.object(sp,'create_candidates',return_value=iter(records)), \
             patch.object(flow,'final_route',return_value=routed) as router,redirect_stdout(io.StringIO()):
            result=flow.select_layout('unused',passive_skeleton='auto')
        self.assertEqual(router.call_count,1)
        self.assertEqual(result[4]['quality']['selected_candidate'],'1')
        self.assertEqual(result[0].cell_of,original.cell_of)
        self.assertEqual(result[0].orient,original.orient)
        self.assertEqual(result[0].column_x,original.column_x)
        self.assertEqual(result[5],routed)
        self.assertEqual(result[4]['quality']['candidates'][1]['status'],'skipped')

    def test_saved_base_placement_can_measure_modules_without_rearranging_them(self):
        dev,core,cs,ns,ix,p,pl,report=self.packed()
        restored=PAR.Placement.__new__(PAR.Placement)
        restored.__dict__=deepcopy(pl.__dict__)
        before=[restored.pos_of(i) for i in range(len(cs))]
        self.assertEqual(measure(restored,cs,ix,p),measure(pl,cs,ix,p))
        self.assertEqual(before,[restored.pos_of(i) for i in range(len(cs))])

    def test_inward_control_chains_reserve_a_common_gap_without_moving_core_cells(self):
        dev=dict(a=mos('x','ga'),b=mos('y','gb'),r=resistor('ga','in_a'),s=resistor('gb','in_b'))
        doc=to_document(dev);cs,ns=doc['components'],doc['nets'];ix={c['id']:i for i,c in enumerate(cs)}
        core,*_=base_placement({n:d for n,d in dev.items() if n in ('a','b')},dict(a=(1,2),b=(1,3)),dict(a=1,b=0))
        p=compile_plan(PAR,dev,cs,ns,[]);before=deepcopy(core.__dict__)
        pl,report=pack(PAR,core,cs,ns,ix,p,RoutingOptions())
        self.assertTrue(report['applied'],report)
        self.assertTrue(any('control_attachment_corridor' in a['reason'] for a in report['expansions']))
        self.assertGreater(pl.column_x[3]-pl.column_x[2],core.column_x[3]-core.column_x[2])
        for n in ('a','b'):
            i=ix[n];j=next(k for k,c in enumerate(core.comps) if c['id']==n)
            self.assertEqual(pl.cell_of[i],before['cell_of'][j]);self.assertEqual(pl.orient[i],before['orient'][j])
        self.assertTrue(final_route(PAR,ns,pl,ix)[2]['valid'])

    def test_fixed_core_blocks_control_corridor_expansion_atomically(self):
        dev=dict(a=mos('x','ga'),b=mos('y','gb'),r=resistor('ga','input'))
        doc=to_document(dev);cs,ns=doc['components'],doc['nets'];ix={c['id']:i for i,c in enumerate(cs)}
        core,*_=base_placement({n:d for n,d in dev.items() if n in ('a','b')},dict(a=(1,2),b=(1,3)),dict(a=1,b=0))
        core.fixed={1};before=list(core.column_x);p=compile_plan(PAR,dev,cs,ns,[])
        pl,report=pack(PAR,core,cs,ns,ix,p,RoutingOptions())
        self.assertIsNone(pl);self.assertEqual(core.column_x,before)
        self.assertEqual(report['stop_reason'],'no_safe_reservation')

    def test_duplicate_obstacle_rows_do_not_starve_distinct_or_outer_channels(self):
        dev=dict(a=mos('x','ga'),r=resistor('ga','input'))
        doc=to_document(dev);cs,ns=doc['components'],doc['nets'];ix={c['id']:i for i,c in enumerate(cs)}
        core,*_=base_placement({'a':dev['a']},dict(a=(1,3)))
        p=compile_plan(PAR,dev,cs,ns,[]);pl=make_frame(PAR,core,cs,ix,RoutingOptions())
        obstacles=[(i,(-10000.,100.,10000.,150.)) for i in range(20)]+[(21,(-10000.,300.,10000.,350.))]
        with patch('netlist2schematic.routing.routing_rules.build_scene',return_value=SimpleNamespace(body_rects=obstacles,marker_rects=[])):
            choices=list(slot_trials(PAR,pl,cs,ix,p['groups'][0],RoutingOptions()))
        channels={c.passive_module_guides[p['groups'][0]['id']]['ports'][0][1] for c in choices}
        self.assertGreaterEqual(len(channels),7)
        self.assertTrue(any(y<100 for y in channels));self.assertTrue(any(y>350 for y in channels))

    def test_invalid_optional_host_l_hint_is_discarded_and_independently_routed(self):
        dev,core,cs,ns,ix,p,pl,report=self.packed()
        guide=next(iter(pl.passive_module_guides.values()))
        x,y=pl.pos_of(ix['l'])
        guide['routes']['left'].append(((x-100,y),(x+100,y)))
        routes,metrics,valid=final_route(PAR,ns,pl,ix)
        self.assertTrue(valid['valid']);self.assertTrue(validate_final_routes(PAR,ns,pl,ix,routes)[2]['valid'])

    def test_axis_reservation_reparks_unplaced_modules_in_absolute_coordinates(self):
        dev=dict(a=mos('left','ga'),b=mos('right','gb'))
        nodes=['left']+['internal'+str(k) for k in range(15)]+['right']
        for k,(a,b) in enumerate(zip(nodes,nodes[1:])):dev['p'+str(k)]=resistor(a,b)
        dev,core,cs,ns,ix,p=fixture(dev);pl=make_frame(PAR,core,cs,ix,RoutingOptions())
        groups,actions=reserve(PAR,pl,cs,ix,p,RoutingOptions(),())
        self.assertIsNotNone(groups)
        parked=[pl.pos_of(ix[n])[0] for n in dev if dev[n]['cls']=='PASSIVE']
        self.assertTrue(all(b-a>=219.99 for a,b in zip(parked,parked[1:])))
        self.assertTrue(actions)


class ModuleInterfaceTests(unittest.TestCase):
    def scene(self,enabled=True):
        from netlist2schematic.routing.routing_rules import Scene,Pin
        scene=Scene(options=RoutingOptions(),pins={'n':[
            Pin('n',0,'a',(0,0),(0,0),(1,0)),Pin('n',1,'b',(100,0),(100,0),(-1,0))]},
            all_pins={'n':[(0,0),(100,0)]},fixed_segments={'n':[]})
        if enabled:scene.module_port_nets={'n'}
        return scene

    def test_only_failed_module_net_uses_existing_safe_interfaces(self):
        from netlist2schematic.routing.orthogonal_router import route_scene
        route=[((0,0),(100,0))]
        with patch('netlist2schematic.routing.orthogonal_router._route_net',return_value=(None,Counter())), \
             patch('netlist2schematic.recovery.local_rescue._route_local_net',return_value=(route,Counter(),Counter(),None)) as repaired:
            result,report=route_scene(self.scene())
        self.assertEqual(result,[('n',route)]);self.assertEqual(repaired.call_count,1)
        self.assertTrue(report['module_interface_repairs'][0]['repaired'])

    def test_original_success_does_not_call_module_interface_repair(self):
        from netlist2schematic.routing.orthogonal_router import route_scene
        with patch('netlist2schematic.routing.orthogonal_router._route_net',return_value=([((0,0),(100,0))],Counter())), \
             patch('netlist2schematic.recovery.local_rescue._route_local_net',side_effect=AssertionError('success stays unchanged')):
            result,report=route_scene(self.scene())
        self.assertNotIn('module_interface_repairs',report)

    def test_off_or_unrelated_net_never_calls_new_interface_hook(self):
        from netlist2schematic.routing.orthogonal_router import route_scene,RoutingFailure
        for scene in (self.scene(False),self.scene()):
            scene.module_port_nets={'unrelated'}
            with patch('netlist2schematic.routing.orthogonal_router._route_net',return_value=(None,Counter())), \
                 patch('netlist2schematic.recovery.local_rescue._route_local_net',side_effect=AssertionError('not a module net')):
                with self.assertRaises(RoutingFailure):route_scene(scene)

    def test_failed_interface_is_bounded_across_margins_and_shares_deadline(self):
        from netlist2schematic.routing.orthogonal_router import route_scene,RoutingFailure
        from time import perf_counter
        deadline=perf_counter()+10
        with patch('netlist2schematic.routing.orthogonal_router._route_net',return_value=(None,Counter())), \
             patch('netlist2schematic.recovery.local_rescue._route_local_net',return_value=(None,Counter(),Counter(),dict(reason='no_safe_goal'))) as repaired:
            with self.assertRaises(RoutingFailure):route_scene(self.scene(),deadline=deadline)
        self.assertEqual(repaired.call_count,1)
        self.assertEqual(repaired.call_args.args[5],deadline)


if __name__=='__main__':unittest.main()
