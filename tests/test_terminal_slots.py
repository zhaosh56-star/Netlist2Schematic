"""Generic endpoint search preserves the active frame and complete modules."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import json
import random
import tempfile
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_passive_placement import base_placement, mos, resistor, source
from tests.test_passive_skeleton import banks
from tests.test_series_alignment import PAR
from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.plugins.passive_skeleton.plan import compile_plan
from netlist2schematic.plugins.passive_skeleton.placement import pack, _anchor_geometry, _geometry
from netlist2schematic.plugins.passive_skeleton.terminal_slots import refine_terminals, is_terminal
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.routing.sa_routing import final_route, validate_final_routes
from netlist2schematic.routing.orthogonal_router import RoutingFailure

OPTIONS=RoutingOptions()


def fixture(kind='V', topology='single'):
    devices=dict(MHOST=mos('drain','input','tail'), MLEFT=mos('left','other','tail'),
                 MTAIL=mos('tail','bias'))
    if topology=='series':
        guests=dict(VREF=source('VSRC','feed'),RFEED=resistor('feed','middle'),
                    CFEED=resistor('middle','last','C'),LFEED=resistor('last','input','L'))
    elif topology=='parallel':
        guests={k+'LOAD':resistor('input','0',k) for k in ('R','C','L')}
    else:
        guests={kind+'REF':source('VSRC' if kind=='V' else 'ISRC','input')
                if kind in ('V','I') else resistor('input','0',kind)}
    devices.update(guests)
    return devices,dict(MHOST=(1,4),MLEFT=(1,3),MTAIL=(2,3)),set(guests)


def incumbent(devices,cells,orientations=None):
    core_devices={n:d for n,d in devices.items() if n in cells}
    core,_,_,_=base_placement(core_devices,cells,orientations=orientations,blocks=[list(cells)[:2]])
    core.column_x=[64+124*k for k in range(core.cols)]
    core.row_y=[64+126*k for k in range(len(core.row_y))]
    doc=to_document(devices);comps,nets=doc['components'],doc['nets']
    index={c['id']:i for i,c in enumerate(comps)}
    plan=compile_plan(PAR,devices,comps,nets,analyze_structures(devices))
    with patch('netlist2schematic.plugins.passive_skeleton.terminal_slots.refine_terminals',
               side_effect=lambda par,pl,*a,**kw:(pl,dict(trials=0))):
        frame,report=pack(PAR,core,comps,nets,index,plan,OPTIONS)
    assert frame is not None,report
    return frame,comps,nets,index,plan


def refine(args,**kwargs):
    frame,comps,nets,index,plan=args
    return refine_terminals(PAR,frame,comps,nets,index,plan,OPTIONS,
        deadline=kwargs.pop('deadline',perf_counter()+1),max_trials=kwargs.pop('max_trials',64),**kwargs)


class TerminalSlotTests(unittest.TestCase):
    def assert_frame_preserved(self,before,after,own):
        self.assertEqual(before.cell_of,after.cell_of)
        self.assertEqual(before.orient,after.orient)
        self.assertEqual(before.row_y,after.row_y)
        self.assertEqual(before.column_x,after.column_x)
        deltas={tuple(after.pos_of(i)[k]-before.pos_of(i)[k] for k in (0,1)) for i in own}
        self.assertEqual(len(deltas),1)
        for i in range(len(before.comps)):
            if i not in own:self.assertEqual(before.pos_of(i),after.pos_of(i))

    def test_two_dimensional_search_finds_a_nearby_slot_below_blocked_height(self):
        devices,cells,guests=fixture();args=incumbent(devices,cells)
        frame,comps,nets,index,plan=args;before=deepcopy(frame.__dict__)
        rng=random.getstate()
        with patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('No A* in search')):
            after,report=refine(args)
        self.assertTrue(report['applied'],report)
        self.assertEqual(random.getstate(),rng)
        self.assertEqual(frame.cell_of,before['cell_of']);self.assertEqual(frame.passive_local_offsets,before['passive_local_offsets'])
        i=index['VREF'];a=frame.pin_world(index['MHOST'],'G')
        distance=lambda p:sum(abs(p[k]-a[k]) for k in (0,1))
        self.assertLess(distance(after.pos_of(i)),distance(frame.pos_of(i)))
        self.assertNotEqual(frame.pos_of(i)[1],after.pos_of(i)[1])
        self.assert_frame_preserved(frame,after,{i})
        self.assertTrue(final_route(PAR,nets,after,index)[2]['valid'])

    def test_all_two_terminal_device_families_keep_their_original_nodes(self):
        for kind in ('R','C','L','D','V','I'):
            with self.subTest(kind=kind):
                devices,cells,guests=fixture(kind);original=deepcopy(devices)
                args=incumbent(devices,cells);after,report=refine(args)
                frame,comps,nets,index,plan=args
                self.assertEqual(report['eligible_count'],1)
                self.assertLessEqual(report['trials'],64)
                self.assertEqual(devices,original)
                self.assert_frame_preserved(frame,after,{index[n] for n in guests})
                self.assertTrue(final_route(PAR,nets,after,index)[2]['valid'])

    def test_series_and_parallel_modules_move_rigidly(self):
        for topology in ('series','parallel'):
            with self.subTest(topology=topology):
                devices,cells,guests=fixture(topology=topology)
                args=incumbent(devices,cells);frame,comps,nets,index,plan=args
                before=deepcopy(plan)
                after,report=refine(args)
                self.assertEqual(report['eligible_count'],1)
                self.assertEqual(plan,before)
                self.assert_frame_preserved(frame,after,{index[n] for n in guests})
                self.assertTrue(final_route(PAR,nets,after,index)[2]['valid'])

    def test_two_ended_inline_modules_are_not_reorganized(self):
        devices=dict(MUP=mos('top','ga','mid'),MDOWN=mos('bottom','gb'),R1=resistor('mid','bottom'))
        args=incumbent(devices,dict(MUP=(0,2),MDOWN=(2,2)))
        frame,comps,nets,index,plan=args
        self.assertFalse(is_terminal(plan['groups'][0]))
        after,report=refine(args)
        self.assertIs(after,frame);self.assertEqual(report['eligible_count'],0)

    def test_confirmed_paired_banks_keep_their_existing_joint_plan(self):
        _,core,comps,nets,index,plan=banks()
        frame,packing=pack(PAR,core,comps,nets,index,plan,OPTIONS)
        self.assertIsNotNone(frame,packing)
        self.assertEqual(packing['terminal_search']['eligible_count'],0)
        self.assertTrue(packing['terminal_search']['coupled_families_preserved'])

    def test_fixed_guest_is_not_moved(self):
        devices,cells,guests=fixture();args=incumbent(devices,cells)
        args[0].fixed={args[3]['VREF']}
        after,report=refine(args)
        self.assertIs(after,args[0]);self.assertEqual(report['eligible_count'],0)

    def test_zero_budget_and_expired_deadline_keep_validated_incumbent(self):
        devices,cells,_=fixture();args=incumbent(devices,cells)
        for kw in (dict(max_trials=0),dict(deadline=0)):
            with self.subTest(kw=kw):
                after,report=refine(args,**kw)
                self.assertIs(after,args[0]);self.assertFalse(report['applied']);self.assertEqual(report['trials'],0)

    def test_timeout_during_trial_discards_only_the_unfinished_proposal(self):
        devices,cells,_=fixture();args=incumbent(devices,cells)
        failure=RoutingFailure('test timeout',dict(reason='quality_time_budget'))
        with patch('netlist2schematic.plugins.preflight.placement_preflight._static_faults',side_effect=failure):
            after,report=refine(args)
        self.assertIs(after,args[0]);self.assertFalse(report['applied'])
        self.assertEqual(report['stop_reason'],'time_budget')
        self.assertEqual(report['trials'],1)

    def test_candidate_limit_is_global_and_does_not_partially_move_a_module(self):
        devices,cells,guests=fixture(topology='series');args=incumbent(devices,cells)
        after,report=refine(args,max_trials=1)
        self.assertLessEqual(report['trials'],1)
        self.assert_frame_preserved(args[0],after,{args[3][n] for n in guests})

    def test_subnet_names_and_input_order_do_not_change_search_results(self):
        devices,cells,_=fixture();args=incumbent(devices,cells);after,_=refine(args)
        names={n:n[0]+str(k)+'changed' for k,n in enumerate(reversed(devices))}
        fields={'d','g','s','bulk','p','n','a','b'}
        net=lambda n:n if n in ('0','vdd') else 'renamed_'+n
        changed={names[n]:{k:net(v) if k in fields else v for k,v in d.items()}
                 for n,d in reversed(list(devices.items()))}
        other_args=incumbent(changed,{names[n]:c for n,c in cells.items()})
        other,_=refine(other_args)
        for n in devices:
            self.assertEqual(after.pos_of(args[3][n]),other.pos_of(other_args[3][names[n]]))

    def test_translating_frame_translates_the_result(self):
        devices,cells,_=fixture();args=incumbent(devices,cells);after,_=refine(args)
        shifted=deepcopy(args[0]);shifted.column_x=[x+200 for x in shifted.column_x];shifted.row_y=[y+400 for y in shifted.row_y]
        shifted.passive_module_guides=deepcopy(args[0].passive_module_guides)
        for guide in shifted.passive_module_guides.values():
            for field in ('routes','internal_routes'):
                guide[field]={net:[((a[0]+200,a[1]+400),(b[0]+200,b[1]+400)) for a,b in ss]
                              for net,ss in guide[field].items()}
            guide['ports']=[(p[0]+200,p[1]+400) for p in guide['ports']]
        other,_=refine((shifted,*args[1:]))
        for i in range(len(args[1])):
            self.assertEqual(tuple(after.pos_of(i)[k]+(200,400)[k] for k in (0,1)),other.pos_of(i))

    def test_multiterminal_reference_keeps_branch_access_after_compaction(self):
        from tests.test_corresponding_branch_rows import CorrespondingBranchIntegrationTests
        from netlist2schematic.layout.structured_placement import create_candidates
        with tempfile.TemporaryDirectory() as directory:
            path=CorrespondingBranchIntegrationTests().source(directory)
            pl,comps,nets,index,data=next(create_candidates(path,iterations=0,passive_skeleton='auto',candidate_ids=(2,)))
            report=data['passive_skeleton']['terminal_search']
            self.assertTrue(report['incumbent_validated'])
            for action in report['actions']:
                self.assertLessEqual(action['after_metrics']['interface_access_risks'],action['before_metrics']['interface_access_risks'])
            self.assertTrue(final_route(PAR,nets,pl,index)[2]['valid'])

    def test_saved_result_restores_coordinates_without_running_search(self):
        from netlist2schematic.common.sa_saved import load_placement
        from netlist2schematic.flow.run_flow import _saved_positions
        from netlist2schematic.rendering.render_clean import render
        devices,cells,_=fixture();args=incumbent(devices,cells);pl,report=refine(args)
        _,comps,nets,index,_=args
        routes,metrics,validation=final_route(PAR,nets,pl,index)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'case.cir'
            path.write_text('.model NM NMOS\nMHOST drain input tail 0 NM\nMLEFT left other tail 0 NM\nMTAIL tail bias 0 0 NM\nVREF input 0 DC 1\n',encoding='utf8')
            saved=dict(source_netlist=str(path),source_sha256=sha256(path.read_bytes()).hexdigest(),
                placement=_saved_positions(pl,comps),layout_grid=dict(column_x=pl.column_x,row_y=pl.row_y),
                routes=dict(routes),metrics=metrics,validation=validation)
            file=Path(directory)/'case-placement.json';file.write_text(json.dumps(saved),encoding='utf8')
            data,source,par,loaded,cc,nn,ix=load_placement(file)
            for n in devices:self.assertEqual(loaded.pos_of(ix[n]),pl.pos_of(index[n]))
            with patch('netlist2schematic.plugins.passive_skeleton.terminal_slots.refine_terminals',side_effect=AssertionError('No re-placement on redraw')):
                self.assertEqual(render(file,Path(directory)/'redraw.svg'),metrics)


if __name__=='__main__':unittest.main()
