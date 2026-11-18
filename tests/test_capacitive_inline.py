"""Capacitive display continuations reuse main-column and module safeguards."""
from copy import deepcopy
from unittest.mock import patch
import unittest

from tests.test_inline_passive_branches import bias_devices,model
from tests.test_passive_placement import base_placement,mos,resistor
from tests.test_series_alignment import PAR
from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.plugins.layout_shared.branch_levels import _rl_chains,plan_branch_rows
from netlist2schematic.plugins.passive_skeleton.placement import pack
from netlist2schematic.plugins.columns.column_layout import build_column_plan,measure_columns
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.routing.sa_routing import final_route,validate_final_routes


def capacitive_devices(kind='MOS'):
    devices=bias_devices(kind);devices['R']['type']='C'
    return devices


class CapacitiveBridgeEvidenceTests(unittest.TestCase):
    def test_mos_and_bjt_capacitors_preserve_every_port_and_original_levels(self):
        for kind in ('MOS','BJT'):
            with self.subTest(kind=kind):
                devices=capacitive_devices(kind);before=deepcopy(devices);levels=compute_levels(devices)
                comps,nets,index,plan=model(devices);group=plan['groups'][0]
                self.assertEqual(group['role'],'capacitive_inline')
                bridge=group['conduction']
                self.assertTrue(bridge['layout_only']);self.assertFalse(bridge['dc_path'])
                self.assertEqual(bridge['passive_kinds'],['C'])
                self.assertEqual(len(bridge['conduction_ports']),2)
                self.assertTrue(bridge['control_taps']['bias'])
                self.assertEqual(devices,before)
                self.assertEqual(compute_levels(devices),levels)
                self.assertEqual(to_document(devices)['nets'],nets)
                self.assertEqual(plan['electrical_node_merges'],[])

    def test_capacitor_without_control_taps_still_has_two_explicit_hosts(self):
        devices=dict(upper=mos('top','control_u','a'),lower=mos('b','control_l'),
                     coupling=resistor('a','b','C'))
        group=model(devices)[3]['groups'][0]
        self.assertEqual(group['role'],'capacitive_inline')
        self.assertEqual(group['conduction']['conduction_ports'],{'a':['upper','S'],'b':['lower','D']})

    def test_mixed_rlc_chain_and_reversed_member_pins_keep_connection_order(self):
        devices=capacitive_devices()
        devices['R']=resistor('bias','inside')
        devices['C']=resistor('other','inside','C')
        devices['L']=resistor('other','bottom','L')
        comps,nets,index,plan=model(devices);group=plan['groups'][0]
        self.assertEqual(group['role'],'capacitive_inline')
        self.assertEqual(set(group['members']),{'R','C','L'})
        bridge=group['conduction']
        self.assertEqual(bridge['path'],['bias','inside','other','bottom'])
        self.assertEqual(bridge['passive_kinds'],['C','L','R'])
        rows,report=plan_branch_rows(devices,compute_levels(devices),[0]*len(comps),[],comps,index,bridges=[bridge])
        alignment=report['bridge_alignment'][0]
        self.assertEqual(alignment['member_pins']['C'],{'in':'2','out':'1'})
        self.assertTrue(all(c['kind']=='capacitive_bridge_axis' for c in alignment['connections']))
        order=['upper',*bridge['members'],'lower']
        self.assertTrue(all(rows[index[a]]<rows[index[b]] for a,b in zip(order,order[1:])))

    def test_only_opted_in_row_planning_uses_capacitive_display_evidence(self):
        devices=capacitive_devices();comps,nets,index,plan=model(devices)
        self.assertFalse(_rl_chains(devices,comps)[0])
        levels=compute_levels(devices);before=deepcopy(levels)
        rows,report=plan_branch_rows(devices,levels,[0]*len(comps),[],comps,index,
                                    bridges=[plan['groups'][0]['conduction']])
        self.assertIn(dict(upper='upper',lower='lower',reason='capacitive_layout_continuation'),report['strict_orders'])
        self.assertLess(rows[index['upper']],rows[index['R']])
        self.assertLess(rows[index['R']],rows[index['lower']])
        self.assertEqual(levels,before)

    def test_rail_gate_feedback_equal_direction_and_extra_channel_keep_accessory_roles(self):
        cases=[]
        rail=capacitive_devices();rail['R']=resistor('vdd','bottom','C');cases.append(rail)
        shunt=capacitive_devices();shunt['R']=resistor('bias','0','C');cases.append(shunt)
        gate=capacitive_devices();gate['lower']['d']='other';cases.append(gate)
        ambiguous=capacitive_devices();ambiguous['extra']=mos('bias','extra_control');cases.append(ambiguous)
        equal=dict(a=mos('x','g1'),b=mos('y','g2'),cap=resistor('x','y','C'));cases.append(equal)
        feedback=dict(a=mos('d','g','s'),cap=resistor('d','g','C'));cases.append(feedback)
        for devices in cases:
            with self.subTest(devices=devices):
                plan=model(devices)[3]
                self.assertFalse(any(g['role']=='capacitive_inline' for g in plan['groups']))

    def test_internal_control_tap_and_parallel_paths_do_not_become_a_fake_serial_bridge(self):
        internal=capacitive_devices();internal['R']=resistor('bias','middle','C')
        internal['L']=resistor('middle','bottom','L');internal['tap']['g']='middle'
        parallel=capacitive_devices();parallel['parallel']=resistor('bias','bottom')
        for devices in (internal,parallel):
            self.assertFalse(any(g['role']=='capacitive_inline' for g in model(devices)[3]['groups']))

    def test_new_capacitive_cycle_cannot_remove_an_existing_rl_continuation(self):
        devices=dict(upper=mos('a','gu','b'),lower=mos('c','gl','d'),
                     r=resistor('b','c'),cap=resistor('d','a','C'))
        plan=model(devices)[3]
        self.assertEqual(next(g for g in plan['groups'] if g['members']==['r'])['role'],'conduction_inline')
        self.assertFalse(any(g['role']=='capacitive_inline' for g in plan['groups']))
        self.assertTrue(any(r['reason']=='bridge_would_create_layout_cycle' for r in plan['rl_skipped']))

    def test_names_line_order_and_capacitor_polarity_do_not_supply_direction(self):
        devices=capacitive_devices();names={n:'device'+str(i) for i,n in enumerate(devices)}
        net=lambda n:n if n in ('vdd','0') else 'node_'+n
        mapped={names[n]:{k:net(v) if k in ('a','b','d','g','s','bulk') else v for k,v in d.items()}
                for n,d in reversed(list(devices.items()))}
        mapped[names['R']]['a'],mapped[names['R']]['b']=mapped[names['R']]['b'],mapped[names['R']]['a']
        bridge=model(mapped)[3]['groups'][0]['conduction']
        self.assertEqual(bridge['conduction_ports'],{'node_bias':[names['upper'],'D'],'node_bottom':[names['lower'],'D']})


class CapacitivePackingTests(unittest.TestCase):
    def fixture(self):
        devices=capacitive_devices();comps,nets,index,plan=model(devices)
        core,cc,cn,ci=base_placement({n:d for n,d in devices.items() if n!='R'},
                                   dict(upper=(0,2),lower=(2,2),tap=(0,4)))
        column=build_column_plan(PAR,core,cc,cn,ci,inline_bridges=[plan['groups'][0]['conduction']])
        core.column_plan=column
        return devices,comps,nets,index,plan,core,ci,column

    def test_column_packing_and_real_routes_use_the_same_capacitor_ports(self):
        devices,comps,nets,index,plan,core,ci,column=self.fixture();before=deepcopy(core.__dict__)
        edge=next(e for e in column['edges'] if e.get('via'))
        self.assertEqual(edge['evidence'],'unique_capacitive_layout_continuation')
        self.assertTrue(edge['layout_only']);self.assertFalse(edge['dc_path'])
        with patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('No A* in packing')):
            frame,report=pack(PAR,core,comps,nets,index,plan,RoutingOptions())
        self.assertIsNotNone(frame,report)
        self.assertTrue(report['column_contract_preserved'])
        self.assertEqual(core.cell_of,before['cell_of'])
        self.assertEqual(core.orient,before['orient'])
        self.assertEqual(measure_columns(core,ci,column)['column_breaks'],0)
        routed,metrics,validation=final_route(PAR,nets,frame,index)
        self.assertTrue(validation['valid'])
        self.assertEqual(validate_final_routes(PAR,nets,frame,index,routed)[1],metrics)

    def test_rejected_capacitor_pack_rolls_back_active_state(self):
        devices,comps,nets,index,plan,core,ci,column=self.fixture();before=deepcopy(core.__dict__)
        with patch('netlist2schematic.plugins.passive_skeleton.module_connections.check_guides',
                   return_value=[dict(kind='body',nets=['bias'])]):
            frame,report=pack(PAR,core,comps,nets,index,plan,RoutingOptions(),max_trials=1)
        self.assertIsNone(frame)
        self.assertFalse(report['applied'])
        self.assertEqual(core.cell_of,before['cell_of'])
        self.assertEqual(core.row_y,before['row_y'])
        self.assertEqual(core.column_x,before['column_x'])
        self.assertEqual(core.orient,before['orient'])


if __name__=='__main__':unittest.main()
