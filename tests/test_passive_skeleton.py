"""Typed skeleton edges, coherent axis dilation and atomic passive packing."""
from copy import deepcopy
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_passive_placement import base_placement,mos,resistor,source
from tests.test_series_alignment import PAR
from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.plugins.passive_skeleton.plan import compile_plan,measure,organization_key
from netlist2schematic.plugins.passive_skeleton.placement import pack,expand_axis


def banks():
    devices={'a':mos('x','y'),'b':mos('y','x')}
    for side,node in (('l','x'),('r','y')):
        for suffix,kind in (('r','R'),('c','C'),('l','L')):
            devices[side+suffix]=resistor('vdd',node,kind)
    doc=to_document(devices);cs,ns=doc['components'],doc['nets'];ix={c['id']:i for i,c in enumerate(cs)}
    core,_,_,_=base_placement({n:d for n,d in devices.items() if n in ('a','b')},dict(a=(0,1),b=(0,2)))
    plan=compile_plan(PAR,devices,cs,ns,analyze_structures(devices))
    return devices,core,cs,ns,ix,plan


def model(devices,records=(),**options):
    doc=to_document(devices);cs,ns=doc['components'],doc['nets']
    return compile_plan(PAR,devices,cs,ns,records,**options)


class PassiveSkeletonEvidenceTests(unittest.TestCase):
    def test_cross_coupled_control_taps_do_not_steal_the_drain_bank(self):
        _,_,cs,ns,ix,p=banks()
        self.assertEqual(len(p['families']),1)
        self.assertEqual({tuple(g['owner_port']['terminal']) for g in p['groups']},{('a','D'),('b','D')})
        self.assertTrue(all(len(next(ts for ts in g['anchors'].values() if ts))==2 for g in p['groups']))
        self.assertEqual(p['electrical_node_merges'],[])

    def test_common_rail_or_model_alone_does_not_pair_banks(self):
        devices,_,cs,ns,_,_=banks()
        result=compile_plan(PAR,devices,cs,ns,[])
        self.assertEqual(result['families'],[])

    def test_duplicate_pair_evidence_is_one_physical_family(self):
        devices,_,cs,ns,_,_=banks()
        # Two conflicting role assertions must not consume the same bank twice.
        records=[dict(kind=k,devices=['a','b'],roles=dict(left='a',right='b')) for k in ('differential_pair','cross_coupled')]
        # Duplicate identical physical evidence is one relation, not ambiguity.
        p=compile_plan(PAR,devices,cs,ns,records)
        self.assertEqual(len(p['families']),1)

    def test_ambiguous_multiple_families_are_not_arbitrarily_selected(self):
        dev,_,_,_,_,_=banks();dev['c']=mos('z','gc')
        for suffix,kind in (('r','R'),('c','C'),('l','L')):dev['third'+suffix]=resistor('vdd','z',kind)
        records=[dict(kind='differential_pair',devices=pair,roles=dict(left=pair[0],right=pair[1])) for pair in (['a','b'],['a','c'])]
        p=model(dev,records)
        self.assertEqual(p['families'],[]);self.assertEqual(len(p['ambiguous_families']),2)

    def test_capacitor_feedback_and_sense_resistors_never_merge_outputs(self):
        devices={'a':mos('x','g1'),'b':mos('y','g2'),
            'sense1':resistor('x','sense'),'sense2':resistor('y','sense'),
            'cap':resistor('x','y','C')}
        before=deepcopy(devices);p=model(devices)
        self.assertEqual(p['electrical_node_merges'],[]);self.assertEqual(devices,before)
        self.assertFalse(any(g['role']=='conduction_inline' for g in p['groups']))
        self.assertEqual({v for c in p['terminals'].values() for v in c['nodes'].values()},
                         {v for c in to_document(devices)['components'] for v in c['nodes'].values()})

    def test_inline_roles_distinguish_rl_capacitive_layout_and_diodes(self):
        for kind,role in (('L','conduction_inline'),('R','conduction_inline'),
                          ('C','capacitive_inline'),('D','bridge')):
            devices={'a':mos('upper','ga','mid1'),'b':mos('mid2','gb'), 'p':resistor('mid1','mid2',kind)}
            self.assertEqual(model(devices)['groups'][0]['role'],role)
        devices['p']=resistor('mid1','mid2','L');devices['tap']=mos('unrelated','mid1')
        group=model(devices)['groups'][0]
        self.assertEqual(group['role'],'conduction_inline')
        self.assertIn(['tap','G'],group['conduction']['control_taps']['mid1'])
        devices['tap']=mos('mid1','unrelated')
        self.assertNotEqual(model(devices)['groups'][0]['role'],'conduction_inline')

    def test_unique_source_control_path_and_shared_node_are_distinct(self):
        dev={'a':mos('d','g'),'v':source('VSRC','bias'),'r':resistor('bias','g')}
        # Original core-source helper remains reproducible; the new default
        # puts the voltage source and resistor into one real two-port module.
        self.assertEqual(model(dev,include_sources=False)['groups'][0]['role'],'source_control_chain')
        self.assertEqual(set(model(dev)['groups'][0]['members']),{'r','v'})
        dev['b']=mos('e','g')
        self.assertNotEqual(model(dev,include_sources=False)['groups'][0]['role'],'source_control_chain')

    def test_names_line_order_and_signal_net_names_do_not_supply_evidence(self):
        dev,_,_,_,_,p=banks();names={n:'part'+str(k) for k,n in enumerate(dev)}
        def net(n):return n if n in ('0','vdd') else 'signal_'+n
        renamed={names[n]:{k:net(v) if k in ('a','b','d','g','s','bulk') else v for k,v in d.items()} for n,d in reversed(list(dev.items()))}
        doc=to_document(renamed)
        q=compile_plan(PAR,renamed,doc['components'],doc['nets'],analyze_structures(renamed))
        self.assertEqual(sorted((g['role'],g['signature']) for g in p['groups']),sorted((g['role'],g['signature']) for g in q['groups']))
        self.assertEqual(len(p['families']),len(q['families']))

    def test_closed_passive_loops_are_skipped_with_original_topology_intact(self):
        dev={'m':mos('d','g'),'a':resistor('x','y'),'b':resistor('y','z'),'c':resistor('z','x')}
        p=model(dev);self.assertFalse(p['applicable']);self.assertEqual(len(p['skipped']),1)

    def test_pure_passives_and_black_box_opamp_keep_the_original_layout_path(self):
        from tests.test_passive_placement import amplifier
        for dev in ({'r':resistor('vdd','0')},{'e':amplifier(),'r':resistor('out','fp')}):
            self.assertFalse(model(dev)['applicable'])


class PassiveSkeletonPackingTests(unittest.TestCase):
    def test_paired_banks_are_atomic_mirrored_and_above_their_real_drain(self):
        dev,core,cs,ns,ix,p=banks();old=deepcopy(core.cell_of);axes=deepcopy(core.column_x)
        with patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('no A* in packing')):
            result,report=pack(PAR,core,cs,ns,ix,p,RoutingOptions())
        self.assertTrue(report['applied'],report)
        self.assertEqual(core.cell_of,old);self.assertEqual(core.column_x,axes)
        for name in ('a','b'):
            self.assertEqual(result.cell_of[ix[name]],core.cell_of[ix[name]])
            self.assertEqual(result.orient[ix[name]],core.orient[ix[name]])
        self.assertEqual(measure(result,cs,ix,p)['family_error'],0)
        for g in p['groups']:
            name,pin=g['owner_port']['terminal'];hy=result.pin_world(ix[name],pin)[1]
            self.assertTrue(all(result.pos_of(ix[n])[1]<hy for n in g['members']))
        self.assertTrue(any(a['reason']=='paired_attachment_width' for a in report['expansions']))
        self.assertEqual(result.column_x[0],core.column_x[0])

    def test_fixed_member_prevents_axis_suffix_shift_without_mutating_input(self):
        _,core,cs,ns,ix,p=banks();core.fixed={1};axes=list(core.column_x)
        result,report=pack(PAR,core,cs,ns,ix,p,RoutingOptions())
        self.assertIsNone(result);self.assertFalse(report['applied']);self.assertEqual(core.column_x,axes)

    def test_failed_family_rolls_back_every_member(self):
        _,core,cs,ns,ix,p=banks();before=deepcopy(core.__dict__)
        with patch('netlist2schematic.plugins.preflight.placement_preflight._static_faults',return_value=dict(count=1,hard={'collision'},violations=[])):
            result,report=pack(PAR,core,cs,ns,ix,p,RoutingOptions())
        self.assertIsNone(result);self.assertEqual(report['actions'],[])
        self.assertEqual(core.cell_of,before['cell_of']);self.assertEqual(core.row_y,before['row_y'])

    def test_custom_geometry_rules_and_translation_preserve_family(self):
        _,core,cs,ns,ix,p=banks();core.column_x=[x+123 for x in core.column_x];core.row_y=[y-350 for y in core.row_y]
        result,report=pack(PAR,core,cs,ns,ix,p,RoutingOptions(halo=16,grid_step=2,edge_clearance=10))
        self.assertTrue(report['applied'],report);self.assertEqual(measure(result,cs,ix,p)['family_error'],0)

    def test_whole_layout_mirror_keeps_bank_correspondence(self):
        _,core,cs,ns,ix,p=banks()
        core.set_state([(r,core.cols-1-c) for r,c in core.cell_of],[o^1 for o in core.orient])
        result,report=pack(PAR,core,cs,ns,ix,p,RoutingOptions())
        self.assertTrue(report['applied'],report)
        self.assertEqual(measure(result,cs,ix,p)['family_error'],0)

    def test_polarity_dual_loads_extend_below_pmos_without_changing_nets(self):
        dev,_,_,_,_,_=banks()
        for d in dev.values():
            if d['cls']=='MOS':d.update(type='PMOS',s='vdd',bulk='vdd')
            elif d.get('a')=='vdd':d['a']='0'
        doc=to_document(dev);cs,ns=doc['components'],doc['nets'];ix={c['id']:i for i,c in enumerate(cs)}
        core,_,_,_=base_placement({n:d for n,d in dev.items() if n in ('a','b')},dict(a=(0,1),b=(0,2)))
        p=model(dev,analyze_structures(dev));before=deepcopy(ns)
        result,report=pack(PAR,core,cs,ns,ix,p,RoutingOptions())
        self.assertTrue(report['applied'],report);self.assertEqual(ns,before)
        self.assertEqual(measure(result,cs,ix,p)['family_error'],0)
        for g in p['groups']:
            host,pin=g['owner_port']['terminal']
            self.assertTrue(all(result.pos_of(ix[n])[1]>result.pin_world(ix[host],pin)[1] for n in g['members']))

    def test_source_polarity_fixed_position_and_floating_ambiguity(self):
        from netlist2schematic.plugins.passive_skeleton.candidates import _control_sources
        for reverse in (False,True):
            dev={'a':mos('d','g'),'v':source('VSRC','0','bias') if reverse else source('VSRC','bias'),
                'r':resistor('bias','g')}
            doc=to_document(dev);cs=doc['components'];ix={c['id']:i for i,c in enumerate(cs)};p=model(dev,include_sources=False)
            core,cc,ns,ci=base_placement({n:d for n,d in dev.items() if n!='r'},dict(a=(1,2),v=(1,1)))
            result,actions=_control_sources(PAR,core,cc,ci,p)
            self.assertEqual(len(actions),1);self.assertEqual(result.cell_of[ci['a']],core.cell_of[ci['a']])
            power=next(pin for pin,n in cc[ci['v']]['nodes'].items() if n=='0')
            signal=next(pin for pin,n in cc[ci['v']]['nodes'].items() if n=='bias')
            self.assertGreater(result.pin_world(ci['v'],power)[1],result.pin_world(ci['v'],signal)[1])
            core.fixed={ci['v']};fixed,actions=_control_sources(PAR,core,cc,ci,p)
            self.assertEqual(actions,[]);self.assertEqual(fixed.cell_of,core.cell_of)

    def test_axis_expansion_preserves_other_intervals_and_rows(self):
        _,core,*_=banks();before=list(core.column_x);rows=list(core.row_y)
        self.assertTrue(expand_axis(core,0,3,22))
        self.assertEqual(core.row_y,rows)
        self.assertEqual([b-a for a,b in zip(core.column_x,core.column_x[1:])],
            [b-a+(22 if i==2 else 0) for i,(a,b) in enumerate(zip(before,before[1:]))])

    def test_frame_preserves_existing_true_pin_horizontal_offsets(self):
        from netlist2schematic.plugins.passive_skeleton.placement import make_frame
        _,core,cs,ns,ix,p=banks();core.offset_x=[10.,-10.]
        frame=make_frame(PAR,core,cs,ix,RoutingOptions())
        for name in ('a','b'):
            self.assertEqual(frame.pin_world(ix[name],'D'),core.pin_world(ix[name],'D'))

    def test_physical_guest_cannot_bypass_an_unrelated_child_leaf(self):
        from netlist2schematic.plugins.passive_skeleton.placement import make_frame,_guest_guards
        devices={'a':mos('x','ga'),'b':mos('y','gb'),'c':mos('z','gc'),'r':resistor('z','vdd')}
        core,_,_,_=base_placement({n:d for n,d in devices.items() if n!='r'},
            dict(a=(1,1),b=(1,3),c=(3,4)),blocks=[['a','b']])
        doc=to_document(devices);cs,ns=doc['components'],doc['nets'];ix={c['id']:i for i,c in enumerate(cs)}
        p=model(devices);frame=make_frame(PAR,core,cs,ix,RoutingOptions());i=ix['r']
        target=((core.pos_of(0)[0]+core.pos_of(1)[0])/2,core.pos_of(0)[1]);x,y=frame.pos_of(i)
        dx,dy=frame.passive_local_offsets[i];frame.passive_local_offsets[i]=[dx+target[0]-x,dy+target[1]-y]
        self.assertFalse(_guest_guards(PAR,frame,cs,ix,p['groups']))

    def test_zero_expired_invalid_and_candidate_budgets_never_commit_partial_layout(self):
        for kw in (dict(seconds=0),dict(max_trials=0),dict(deadline=0)):
            _,core,cs,ns,ix,p=banks();old=list(core.cell_of)
            result,report=pack(PAR,core,cs,ns,ix,p,RoutingOptions(),**kw)
            self.assertIsNone(result);self.assertFalse(report['applied']);self.assertEqual(core.cell_of,old)
        _,core,cs,ns,ix,p=banks()
        result,report=pack(PAR,core,cs,ns,ix,p,RoutingOptions(),max_trials=1)
        self.assertLessEqual(report['trials'],1)
        if result is not None:self.assertEqual(len(report['actions']),len(p['groups']))
        for kw in (dict(seconds=-1),dict(seconds=float('nan')),dict(max_trials=True),dict(deadline=float('inf'))):
            _,core,cs,ns,ix,p=banks()
            with self.assertRaises(ValueError):pack(PAR,core,cs,ns,ix,p,RoutingOptions(),**kw)


class PassiveSkeletonFlowTests(unittest.TestCase):
    def netlist(self,directory):
        p=Path(directory)/'generic.cir'
        p.write_text('''* synthetic paired attachment banks
.model N nmos
M1 x y 0 0 N
M2 y x 0 0 N
R1 vdd x 1k
C1 vdd x 1p
L1 vdd x 1n
R2 vdd y 1k
C2 vdd y 1p
L2 vdd y 1n
.end
''',encoding='utf-8')
        return p

    def test_reference_random_stream_is_unchanged_and_new_sa_has_no_passives_or_astar(self):
        from netlist2schematic.layout import structured_placement as sp
        with tempfile.TemporaryDirectory() as d:
            path=self.netlist(d)
            old=next(sp.create_candidates(path,iterations=0))
            new=next(sp.create_candidates(path,iterations=0,passive_skeleton='auto'))
            for attr in ('cell_of','orient','column_x','row_y'):self.assertEqual(getattr(old[0],attr),getattr(new[0],attr))
            self.assertEqual(old[0].rng.getstate(),new[0].rng.getstate())
            observed=[];actual=sp._anneal
            def anneal(pl,*args,**kwargs):
                observed.append([c['symbol'] for c in pl.comps]);return actual(pl,*args,**kwargs)
            with patch.object(sp,'_anneal',side_effect=anneal),patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('no A*')):
                candidates=list(sp.create_candidates(path,iterations=0,passive_skeleton='auto'))
            self.assertTrue(any(s in ('resistor','capacitor','inductor') for s in observed[0]))
            self.assertTrue(all(s in ('nmos','pmos') for group in observed[1:] for s in group))
            self.assertTrue(candidates[1][4]['passive_skeleton']['applied'])
            self.assertEqual(candidates[1][2],old[2])
            self.assertEqual(candidates[1][4]['levels'],old[4]['levels'])
            self.assertLessEqual(candidates[1][4]['passive_skeleton']['shared_local_budget']['completed_trials'],128)

    def test_zero_pack_budget_keeps_reference_and_never_restarts_mixed_sa(self):
        from netlist2schematic.layout import structured_placement as sp
        with tempfile.TemporaryDirectory() as d:
            path=self.netlist(d);old=list(sp.create_candidates(path,iterations=0))
            actual=sp._anneal;observed=[]
            def anneal(pl,*args,**kw):
                observed.append([c['symbol'] for c in pl.comps]);return actual(pl,*args,**kw)
            with patch('netlist2schematic.plugins.passive_skeleton.candidates.pack',return_value=(None,dict(applied=False,trials=0,stop_reason='local_budget'))), \
                 patch('netlist2schematic.plugins.passive_skeleton.packing_search.resume_failed',
                       side_effect=lambda *args,**kwargs:(None,args[7])), \
                 patch.object(sp,'_anneal',side_effect=anneal):
                new=list(sp.create_candidates(path,iterations=0,passive_skeleton='auto'))
            self.assertEqual(old[0][0].cell_of,new[0][0].cell_of)
            self.assertEqual(old[0][0].orient,new[0][0].orient)
            self.assertTrue(all(item[0] is None and item[4]['generation_skipped'] for item in new[1:]))
            self.assertEqual(len(observed),3)
            self.assertTrue(all(s in ('nmos','pmos') for group in observed[1:] for s in group))

    def test_cli_policy_and_saved_coordinates_cannot_enable_rearrangement(self):
        from netlist2schematic.flow import run_flow
        for mode,style,mobility,others in (('baseline','structured','flexible',()),('layered','legacy','flexible',()),
                ('layered','structured','fixed',()),('layered','structured','flexible',('auto',))):
            with self.assertRaises(ValueError):run_flow._validate_passive_skeleton('auto',mode,style,mobility,*others)
        for flag in ('--simplify-placement','--reroute-placement'):
            with patch('sys.stderr',new=io.StringIO()),self.assertRaises(SystemExit):
                run_flow.main([flag,'not-read.json','--passive-skeleton','auto'])
        with patch.object(run_flow,'run') as run:
            self.assertEqual(run_flow.main(['not-read.cir']),0)
            self.assertEqual(run.call_args.kwargs['passive_skeleton'],'auto')
            self.assertEqual(run_flow.main(['not-read.cir','--passive-skeleton','auto']),0)
            self.assertEqual(run.call_args.kwargs['passive_skeleton'],'auto')
            self.assertEqual(run_flow.main(['not-read.cir','--passive-skeleton','off']),0)
            self.assertEqual(run.call_args.kwargs['passive_skeleton'],'off')

    def test_feedback_and_late_active_finalizers_cannot_overwrite_reserved_slots(self):
        from netlist2schematic.flow import aesthetic_flow as flow
        from netlist2schematic.layout import structured_placement as sp,series_alignment
        from netlist2schematic.plugins.columns import column_layout
        with tempfile.TemporaryDirectory() as d:
            path=self.netlist(d);items=list(sp.create_candidates(path,iterations=0,passive_skeleton='auto'))
        original,modified=items[0],items[1]
        modified[4]['candidate_id']=3
        old_positions=[modified[0].pos_of(i) for i in range(len(modified[1]))]
        qualities=[dict(score=1.,per_net=dict(n=dict(detour_factor=3.,excess_detour=700))),dict(score=1.1,per_net={})]
        metrics=dict(wirelength=1000,crossings=0,bends=8,body_pen=0,overlap=0,proximity=0)
        def series(par,pl,cs,ns,ix,metadata,*args,**kwargs):
            self.assertEqual(metadata['candidate_id'],1,'The skeleton has already finalized its core')
            return pl,metadata
        with patch.object(sp,'create_candidates',return_value=iter([original,modified])), \
             patch.object(sp,'feedback_candidate',side_effect=AssertionError('Cannot replace reserved slots')), \
             patch.object(column_layout,'refine_columns',side_effect=AssertionError('Core columns were finalized before packing')), \
             patch.object(series_alignment,'refine_series_alignment',side_effect=series), \
             patch.object(flow,'final_route',return_value=([],metrics,dict(valid=True))) as route, \
             patch('netlist2schematic.layout.layout_quality.evaluate_quality',side_effect=qualities),redirect_stdout(io.StringIO()):
            pl,cs,ns,ix,data,routed=flow.select_layout('unused',passive_skeleton='auto',column_layout='auto')
        self.assertEqual(route.call_count,2)
        self.assertEqual(data['quality']['selected_candidate'],'3')
        self.assertEqual([pl.pos_of(i) for i in range(len(cs))],old_positions)
        self.assertTrue(data['passive_skeleton']['applied'])


class PassiveSkeletonSelectionTests(unittest.TestCase):
    def candidates(self,new_metrics):
        from netlist2schematic.plugins.passive_skeleton.selection import select_result
        dev,core,cs,ns,ix,p=banks()
        pl,report=pack(PAR,core,cs,ns,ix,p,RoutingOptions());self.assertTrue(report['applied'])
        cells={c['id']:(0,k) if c['id'] in ('a','b') else (2,k) for k,c in enumerate(cs)}
        old,_,_,_=base_placement(dev,cells)
        def result(pl,ident,score,metrics):
            return ((score,metrics['crossings'],metrics['wirelength'],ident),pl,cs,ns,ix,{},([],metrics,dict(valid=True)),dict(score=score),ident)
        reference=result(old,'1',1.,dict(wirelength=1000,crossings=0,bends=8))
        candidate=result(pl,'2',1.1,new_metrics)
        return select_result([reference,candidate],p),reference,candidate

    def test_real_bends_crossings_or_excess_length_still_reject_a_better_pattern(self):
        for wire,cross,bends in ((950,0,9),(950,1,6),(1151,0,6)):
            (selected,_),old,new=self.candidates(dict(wirelength=wire,crossings=cross,bends=bends))
            self.assertIs(selected,old)

    def test_real_legal_guardrails_allow_organization_to_choose_the_pattern(self):
        (selected,report),old,new=self.candidates(dict(wirelength=1050,crossings=0,bends=6))
        self.assertIs(selected,new)
        self.assertEqual(report['reason'],'typed_attachment_organization')


if __name__=='__main__':unittest.main()
