"""Two-terminal source layout preserves original ports and active contracts."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from tests.test_passive_placement import base_placement, mos, resistor, source
from tests.test_inline_passive_branches import bias_devices, model
from tests.test_series_alignment import PAR
from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.plugins.passive_skeleton.placement import pack
from netlist2schematic.plugins.columns.column_layout import build_column_plan, measure_columns
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.plugins.layout_shared.branch_levels import plan_branch_rows
from netlist2schematic.common.passive_coordinates import validate_passive_offsets
from netlist2schematic.flow.run_flow import _saved_positions
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.routing.sa_routing import final_route, validate_final_routes


def bias_source(kind='ISRC', reverse=False, family='MOS'):
    devices=bias_devices(family)
    devices['R']=source(kind,'bottom' if reverse else 'bias','bias' if reverse else 'bottom')
    devices['R']['value']='SIN(0 1 1k)' if kind=='VSRC' else 'DC -20u'
    return devices


class SourceEvidenceTests(unittest.TestCase):
    def test_inline_sources_keep_polarity_value_ports_and_electrical_levels(self):
        for kind in ('ISRC','VSRC'):
            for reverse in (False,True):
                for family in ('MOS','BJT'):
                    with self.subTest(kind=kind,reverse=reverse,family=family):
                        devices=bias_source(kind,reverse,family);before=deepcopy(devices)
                        levels=compute_levels(devices)
                        comps,nets,index,plan=model(devices);group=plan['groups'][0]
                        self.assertTrue(plan['applicable'])
                        self.assertEqual(group['role'],'source_inline')
                        bridge=group['conduction']
                        self.assertEqual((bridge['start'],bridge['end']),('bias','bottom'))
                        self.assertTrue(bridge['source_layout']);self.assertTrue(bridge['layout_only'])
                        self.assertFalse(bridge['dc_path'])
                        self.assertTrue(bridge['control_taps']['bias'])
                        self.assertEqual(plan['source_layout']['guests'],['R'])
                        self.assertEqual(plan['passive_count'],0);self.assertEqual(plan['source_count'],1)
                        self.assertEqual(devices,before);self.assertEqual(compute_levels(devices),levels)
                        self.assertEqual(to_document(devices)['nets'],nets)
                        self.assertEqual(plan['electrical_node_merges'],[])
                        rows,report=plan_branch_rows(devices,levels,[0]*len(comps),[],comps,index,bridges=[bridge])
                        self.assertLess(rows[index['upper']],rows[index['R']])
                        self.assertLess(rows[index['R']],rows[index['lower']])
                        self.assertIn(dict(upper='upper',lower='lower',reason='source_layout_continuation'),report['strict_orders'])
                        self.assertTrue(all(c['kind']=='source_bridge_axis' for c in report['bridge_alignment'][0]['connections']))

    def test_source_and_unbranched_rlc_chain_form_one_module(self):
        devices=bias_source('VSRC')
        devices['R']['n']='inner';devices['c']=resistor('inner','next','C')
        devices['r']=resistor('next','last');devices['l']=resistor('last','bottom','L')
        group=model(devices)[3]['groups'][0]
        self.assertEqual(set(group['members']),{'R','r','l','c'})
        self.assertEqual(group['role'],'source_inline')
        self.assertEqual(group['conduction']['path'],['bias','inner','next','last','bottom'])

    def test_control_source_and_tail_node_are_attachments_not_conduction_pairs(self):
        for shared in (False,True):
            devices=dict(a=mos('d','g','tail'),vin=source('VSRC','input'),
                         r=resistor('input','g'),tail=source('ISRC','tail'))
            if shared:devices['b']=mos('other','other_g','tail')
            plan=model(devices)[3]
            self.assertEqual(set(plan['source_layout']['guests']),{'vin','tail'})
            self.assertFalse(any(g['role']=='source_inline' for g in plan['groups']))
            self.assertEqual({frozenset(g['members']) for g in plan['groups']},
                             {frozenset(('vin','r')),frozenset(('tail',))})
            self.assertEqual(plan['families'],[])

    def test_rail_only_unknown_closed_and_shorted_sources_remain_in_core(self):
        devices=dict(a=mos('d','g'),supply=source('VSRC','vdd'),
                     unknown=source('ISRC','floating','elsewhere'),short=source('VSRC','g','g'))
        plan=model(devices)[3]
        self.assertEqual(plan['source_layout']['guests'],[])
        self.assertEqual(set(plan['source_layout']['retained_in_core']),{'supply','unknown','short'})
        self.assertFalse(plan['applicable'])
        self.assertEqual(set(plan['core_ids']),set(devices))

    def test_shared_channel_end_and_feedback_do_not_invent_inline_order(self):
        devices=bias_source();devices['extra']=mos('bias','other')
        self.assertFalse(any(g['role']=='source_inline' for g in model(devices)[3]['groups']))
        devices=dict(a=mos('d','g','s'),feedback=source('VSRC','s','g'))
        self.assertEqual(model(devices)[3]['groups'][0]['role'],'bridge')

    def test_new_source_cycle_does_not_remove_existing_rl_bridge(self):
        devices=dict(a=mos('top','ga','b'),b=mos('c','gb','d'),
                     r=resistor('b','c'),i=source('ISRC','d','top'))
        plan=model(devices)[3]
        self.assertEqual(next(g for g in plan['groups'] if g['members']==['r'])['role'],'conduction_inline')
        self.assertFalse(any(g['role']=='source_inline' for g in plan['groups']))
        self.assertTrue(any(r['reason']=='bridge_would_create_layout_cycle' for r in plan['source_skipped']))

    def test_rename_reorder_and_polarity_do_not_determine_hosts(self):
        devices=bias_source();names={n:'part'+str(k) for k,n in enumerate(devices)}
        net=lambda n:n if n in ('vdd','0') else 'renamed_'+n
        renamed={names[n]:{k:net(v) if k in ('d','g','s','bulk','p','n') else v for k,v in d.items()}
                 for n,d in reversed(list(devices.items()))}
        source_device=renamed[names['R']]
        source_device['p'],source_device['n']=source_device['n'],source_device['p']
        bridge=model(renamed)[3]['groups'][0]['conduction']
        self.assertEqual(bridge['conduction_ports'],{'renamed_bias':[names['upper'],'D'],
                                                   'renamed_bottom':[names['lower'],'D']})

    def test_parallel_source_and_passive_are_not_silently_collapsed(self):
        devices=bias_source();devices['bleed']=resistor('bias','bottom')
        comps,nets,index,plan=model(devices);group=plan['groups'][0]
        self.assertEqual(group['module']['kind'],'parallel')
        self.assertEqual(set(group['members']),{'R','bleed'})
        self.assertEqual(len(group['electrical_edges']),2)
        self.assertNotEqual(group['role'],'source_inline')
        self.assertEqual(to_document(devices)['nets'],nets)


class SourcePackingTests(unittest.TestCase):
    def fixture(self,kind='ISRC',reverse=False):
        devices=bias_source(kind,reverse);comps,nets,index,plan=model(devices)
        core,cc,cn,ci=base_placement({n:d for n,d in devices.items() if n!='R'},
                                   dict(upper=(0,2),lower=(2,2),tap=(0,4)))
        column=build_column_plan(PAR,core,cc,cn,ci,inline_bridges=[plan['groups'][0]['conduction']])
        core.column_plan=column
        return devices,comps,nets,index,plan,core,ci,column

    def test_pack_route_and_saved_coordinates_preserve_actual_source_terminals(self):
        for kind in ('VSRC','ISRC'):
            for reverse in (False,True):
                with self.subTest(kind=kind,reverse=reverse):
                    devices,comps,nets,index,plan,core,ci,column=self.fixture(kind,reverse)
                    before=deepcopy(core.__dict__)
                    self.assertTrue(any(e['evidence']=='unique_source_layout_continuation' for e in column['edges']))
                    with patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('A* in packing')):
                        frame,report=pack(PAR,core,comps,nets,index,plan,RoutingOptions())
                    self.assertIsNotNone(frame,report)
                    i=index['R'];upper=frame.pin_world(index['upper'],'D');lower=frame.pin_world(index['lower'],'D')
                    self.assertLess(upper[1],frame.pos_of(i)[1]);self.assertLess(frame.pos_of(i)[1],lower[1])
                    self.assertEqual(frame.pin_world(i,'+')[1]<frame.pin_world(i,'-')[1],not reverse)
                    self.assertEqual(core.cell_of,before['cell_of']);self.assertEqual(core.orient,before['orient'])
                    self.assertEqual(measure_columns(frame,index,column)['column_breaks'],0)
                    validate_passive_offsets(frame,comps)
                    self.assertIn('local_offset',_saved_positions(frame,comps)['R'])
                    routes,metrics,validation=final_route(PAR,nets,frame,index)
                    self.assertTrue(validation['valid'])
                    self.assertEqual(validate_final_routes(PAR,nets,frame,index,routes)[1],metrics)

    def test_no_slot_and_fixed_core_source_leave_entire_state_unchanged(self):
        devices,comps,nets,index,plan,core,ci,column=self.fixture();before=deepcopy(core.__dict__)
        with patch('netlist2schematic.plugins.passive_skeleton.placement.slot_trials',return_value=iter(())):
            result,report=pack(PAR,core,comps,nets,index,plan,RoutingOptions())
        self.assertIsNone(result);self.assertEqual(core.cell_of,before['cell_of'])
        core,cc,cn,ci=base_placement(devices,dict(upper=(0,2),lower=(2,2),tap=(0,4),R=(3,2)))
        core.fixed={ci['R']};before=deepcopy(core.__dict__)
        result,report=pack(PAR,core,comps,nets,index,plan,RoutingOptions())
        self.assertIsNone(result);self.assertEqual(report['stop_reason'],'guest_is_fixed_core_member')
        self.assertEqual(core.cell_of,before['cell_of']);self.assertEqual(core.orient,before['orient'])

    def test_local_offsets_still_cannot_move_mos_or_bjt_off_grid(self):
        devices,comps,nets,index,plan,core,ci,column=self.fixture()
        core.passive_local_offsets=[[0,0] for _ in core.comps]
        core.passive_local_offsets[ci['upper']]=[2,0]
        with self.assertRaises(ValueError):validate_passive_offsets(core,core.comps)

    def test_shared_tail_centers_on_actual_node_without_moving_pair(self):
        devices=dict(a=mos('d','ga','tail'),b=mos('e','gb','tail'),i=source('ISRC','tail'))
        comps,nets,index,plan=model(devices)
        core,cc,cn,ci=base_placement({n:d for n,d in devices.items() if n!='i'},dict(a=(0,1),b=(0,3)))
        frame,report=pack(PAR,core,comps,nets,index,plan,RoutingOptions())
        self.assertIsNotNone(frame,report)
        middle=sum(frame.pin_world(index[n],'S')[0] for n in ('a','b'))/2
        self.assertEqual(frame.pos_of(index['i'])[0],middle)
        self.assertEqual(frame.cell_of[index['a']],core.cell_of[ci['a']])
        self.assertEqual(frame.cell_of[index['b']],core.cell_of[ci['b']])
        self.assertTrue(final_route(PAR,nets,frame,index)[2]['valid'])

    def test_control_source_rlc_module_has_real_internal_connections(self):
        devices=dict(a=mos('d','g'),v=source('VSRC','bias'),r=resistor('bias','mid'),
                     c=resistor('mid','next','C'),l=resistor('next','g','L'))
        comps,nets,index,plan=model(devices)
        core,*_=base_placement({'a':devices['a']},dict(a=(0,2)))
        frame,report=pack(PAR,core,comps,nets,index,plan,RoutingOptions())
        self.assertIsNotNone(frame,report)
        self.assertEqual(set(plan['groups'][0]['members']),{'v','r','c','l'})
        self.assertTrue(final_route(PAR,nets,frame,index)[2]['valid'])

    def test_save_reload_and_spacing_restore_source_symbol_not_just_its_cell(self):
        from netlist2schematic.common.sa_saved import load_placement
        from netlist2schematic.plugins.passive_skeleton.module_connections import rebase_after_axis_expansion
        devices,comps,nets,index,plan,core,ci,column=self.fixture('VSRC',True)
        frame,report=pack(PAR,core,comps,nets,index,plan,RoutingOptions())
        routes,metrics,validation=final_route(PAR,nets,frame,index)
        with tempfile.TemporaryDirectory() as tmp:
            source_path=Path(tmp)/'test.cir'
            # SPICE instance family prefixes must agree with each original ID.
            rename={n:('V' if d['cls']=='VSRC' else 'M')+n for n,d in devices.items()}
            lines=['.model NMOS NMOS','.model PMOS PMOS']+[
                rename[n]+' '+(' '.join((d['p'],d['n'],'DC -0.1')) if d['cls']=='VSRC' else
                               ' '.join((d['d'],d['g'],d['s'],d['bulk'],d['type']))) for n,d in devices.items()]
            source_path.write_text('\n'.join(lines)+'\n',encoding='utf8')
            saved={rename[n]:v for n,v in _saved_positions(frame,comps).items()}
            record=dict(source_netlist=str(source_path),source_sha256=sha256(source_path.read_bytes()).hexdigest(),
                placement=saved,layout_grid=dict(column_x=frame.column_x,row_y=frame.row_y))
            path=Path(tmp)/'placement.json';path.write_text(json.dumps(record),encoding='utf8')
            data,src,par,loaded,cc,nn,ix=load_placement(path)
            for n in devices:
                self.assertEqual(loaded.pos_of(ix[rename[n]]),frame.pos_of(index[n]))
                self.assertEqual(loaded.orient[ix[rename[n]]],frame.orient[index[n]])
            self.assertTrue(validate_final_routes(par,nn,loaded,ix,routes)[2]['valid'])
        expanded=deepcopy(frame)
        expanded.column_x=[frame.column_x[0]+1.5*(x-frame.column_x[0]) for x in frame.column_x]
        expanded.row_y=[frame.row_y[0]+1.5*(y-frame.row_y[0]) for y in frame.row_y]
        rebase_after_axis_expansion(frame,expanded,index,plan)
        self.assertEqual(expanded.cell_of,frame.cell_of);self.assertEqual(expanded.orient,frame.orient)
        self.assertFalse(hasattr(expanded,'passive_module_guides'))


if __name__=='__main__':unittest.main()
