"""Cross-passive column evidence retains controls and existing layout guards."""
from copy import deepcopy
from unittest.mock import patch
import unittest

from tests.test_passive_placement import base_placement, mos, resistor
from tests.test_series_alignment import PAR
from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.plugins.passive_skeleton import plan as modules
from netlist2schematic.plugins.passive_skeleton.placement import pack
from netlist2schematic.plugins.layout_shared.branch_levels import plan_branch_rows
from netlist2schematic.plugins.columns import column_layout as columns
from netlist2schematic.routing.routing_rules import RoutingOptions


def bias_devices(kind='MOS'):
    if kind == 'MOS':
        upper = dict(cls='MOS', type='PMOS', d='bias', g='bias', s='vdd', bulk='vdd', model='PMOS')
        lower = mos('bottom', 'bottom')
        tap = dict(upper, d='output')
    else:
        upper = dict(cls='BJT', type='PNP', c='bias', b='bias', e='vdd', model='PNP')
        lower = dict(cls='BJT', type='NPN', c='bottom', b='bottom', e='0', model='NPN')
        tap = dict(upper, c='output')
    return dict(upper=upper, lower=lower, tap=tap, R=resistor('bias','bottom'))


def model(devices):
    doc = to_document(devices)
    comps, nets = doc['components'], doc['nets']
    index = {c['id']: i for i,c in enumerate(comps)}
    plan = modules.compile_plan(PAR, devices, comps, nets, analyze_structures(devices))
    return comps, nets, index, plan


class InlinePassiveEvidenceTests(unittest.TestCase):
    def test_diode_connected_bias_chain_keeps_both_hosts_and_every_control(self):
        for kind,channel,control in (('MOS','D','G'),('BJT','C','B')):
            with self.subTest(kind=kind):
                devices=bias_devices(kind);original=deepcopy(devices)
                comps,nets,index,plan=model(devices);group=plan['groups'][0]
                self.assertEqual('conduction_inline',group['role'])
                bridge=group['conduction']
                self.assertEqual({'bias':['upper',channel],'bottom':['lower',channel]},bridge['conduction_ports'])
                self.assertEqual({('upper',control),('tap',control)},set(map(tuple,bridge['control_taps']['bias'])))
                self.assertEqual([['lower',control]],bridge['control_taps']['bottom'])
                self.assertEqual(devices,original)
                self.assertEqual(to_document(devices)['nets'],nets)
                self.assertEqual([],plan['electrical_node_merges'])

    def test_row_plan_and_module_host_choice_use_the_same_resolved_ports(self):
        dev=bias_devices();comps,nets,index,plan=model(dev)
        bridge=plan['groups'][0]['conduction'];levels=compute_levels(dev)
        before=deepcopy(levels)
        rows,record=plan_branch_rows(dev,levels,[0]*len(comps),[],comps,index,bridges=[bridge])
        self.assertLess(rows[index['upper']],rows[index['R']])
        self.assertLess(rows[index['R']],rows[index['lower']])
        self.assertEqual('upper',record['bridge_alignment'][0]['upper'])
        self.assertEqual('lower',record['bridge_alignment'][0]['lower'])
        self.assertEqual(levels,before)
        core,cs,ns,ix=base_placement({n:d for n,d in dev.items() if n!='R'},
                                   dict(upper=(0,2),lower=(2,3),tap=(0,5)))
        self.assertEqual([(('upper','D'),('lower','D'))],modules.host_pairs(core,cs,ix,plan['groups'][0]))

    def test_parallel_paths_internal_taps_extra_conductive_taps_and_cycles_skip(self):
        examples=[]
        parallel=bias_devices();parallel['L']=resistor('bias','bottom','L');examples.append(parallel)
        conductive=bias_devices();conductive['extra']=mos('bias','input');examples.append(conductive)
        control_only=bias_devices();control_only['lower']=mos('elsewhere','bottom');examples.append(control_only)
        internal=bias_devices();internal['R']=resistor('bias','middle');internal['L']=resistor('middle','bottom','L')
        internal['tap']['g']='middle';examples.append(internal)
        cyclic=dict(upper=mos('top','tap','start'), lower=mos('end','end','top'),
                    tap=mos('other','start'), R=resistor('start','end'));examples.append(cyclic)
        for devices in examples:
            with self.subTest(devices=devices):
                plan=model(devices)[3]
                self.assertFalse(any(g.get('conduction',{}).get('conduction_ports') for g in plan['groups']))

    def test_renaming_reordering_and_passive_endpoint_reversal_preserve_hosts(self):
        old=bias_devices();rename={name:'part'+str(k) for k,name in enumerate(reversed(old))}
        net=lambda value:value if value in ('vdd','0') else 'node_'+value
        mapped={rename[name]:{k:net(v) if k in ('a','b','c','d','e','g','s','bulk') else v
                             for k,v in device.items()} for name,device in reversed(list(old.items()))}
        mapped[rename['R']]['a'],mapped[rename['R']]['b']=mapped[rename['R']]['b'],mapped[rename['R']]['a']
        group=model(mapped)[3]['groups'][0]
        ports=group['conduction']['conduction_ports']
        self.assertEqual(ports['node_bias'],[rename['upper'],'D'])
        self.assertEqual(ports['node_bottom'],[rename['lower'],'D'])

    def test_capacitor_and_diode_stay_two_ended_accessories(self):
        for kind in ('C','D'):
            devices=bias_devices();devices['R']['type']=kind
            plan=model(devices)[3]
            self.assertFalse(any(g.get('conduction') for g in plan['groups']))

    def test_control_tapped_supply_load_keeps_original_attachment_policy(self):
        devices=bias_devices()
        devices['R']=resistor('vdd','bottom')
        group=model(devices)[3]['groups'][0]
        self.assertEqual('rail_attachment',group['role'])
        self.assertNotIn('conduction',group)
        self.assertEqual(dict(net='bottom',terminal=['lower','D']),group['owner_port'])

    def test_shared_sink_branch_reuses_active_tiers_instead_of_passive_gaps(self):
        from netlist2schematic.layout import structured_placement as structured
        from netlist2schematic.plugins.passive_skeleton.candidates import create_candidate
        devices=bias_devices()
        devices['main_top']=dict(devices['upper'],d='main_join',g='main_input')
        devices['main_low']=mos('main_join','main_bias')
        comps,nets,index,plan=model(devices)
        levels=compute_levels(devices);before=deepcopy(levels)
        conduction={net for d in devices.values() if d['cls']=='MOS' for net in (d['d'],d['s'])}
        attachments=structured.legacy.attach_info(devices,levels['net_levels'],conduction)
        old_rows,_=structured.legacy._allocate_rows(devices,comps,index,
            levels['net_levels'],levels['device_levels'],attachments)
        core_ids=[n for n,d in devices.items() if d['cls']!='PASSIVE']
        class StopBeforeAnnealing(Exception):pass
        with patch.object(structured,'_initial_placement',side_effect=StopBeforeAnnealing) as initial:
            with self.assertRaises(StopBeforeAnnealing):
                create_candidate(devices,comps,nets,index,levels,attachments,analyze_structures(devices),
                    123,1,plan,RoutingOptions(),iterations=0)
        rows=dict(zip(core_ids,initial.call_args.kwargs['row_override']))
        self.assertEqual(rows['lower'],rows['main_low'])
        self.assertLess(rows['upper'],rows['lower'])
        self.assertLessEqual(len(set(rows.values())),len({old_rows[index[n]] for n in core_ids}))
        self.assertEqual(levels,before)


class InlineColumnCompatibilityTests(unittest.TestCase):
    def fixture(self):
        devices=bias_devices()
        devices['main_upper']=mos('supply_in','main_gate','join')
        devices['main_lower']=mos('join','main_bias')
        comps,nets,index,plan=model(devices)
        active={n:d for n,d in devices.items() if n!='R'}
        cells=dict(upper=(0,3),lower=(2,6),tap=(0,7),main_upper=(1,1),main_lower=(2,1))
        core,cc,cn,ci=base_placement(active,cells)
        bridge=plan['groups'][0]['conduction']
        result=columns.build_column_plan(PAR,core,cc,cn,ci,inline_bridges=[bridge])
        return core,cc,cn,ci,result,comps,nets,index,plan

    def test_new_column_extends_the_plan_and_preserves_original_paths(self):
        core,cc,cn,ci,result,*_=self.fixture()
        self.assertIn(['upper','lower'],[c['members'] for c in result['columns']])
        protected=result['protected_plan']
        self.assertIn(['main_upper','main_lower'],[c['members'] for c in protected['columns']])
        edge=next(e for e in result['edges'] if e.get('via'))
        self.assertEqual(['R'],edge['via'])
        self.assertEqual(['bias','bottom'],edge['endpoint_nets'])
        self.assertEqual((edge['pin_a'],edge['pin_b']),('D','D'))

    def test_fixing_new_column_cannot_buy_a_broken_existing_column(self):
        core,cc,cn,ci,result,*_=self.fixture();core.column_plan=result
        trial=deepcopy(core);cells=list(trial.cell_of)
        cells[ci['lower']]=(2,3)
        cells[ci['main_lower']]=(2,2)
        trial.set_state(cells,trial.orient)
        self.assertLess(columns.measure_columns(trial,ci,result)['pin_error'],
                        columns.measure_columns(core,ci,result)['pin_error'])
        self.assertFalse(columns.preserves_columns(core,trial,ci))

    def test_fixed_hosts_and_transaction_rejection_keep_input_state(self):
        core,cc,cn,ci,result,*_=self.fixture();core.fixed={ci['upper'],ci['lower']}
        before=deepcopy(core.__dict__)
        with patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('No A* in columns')):
            refined,report=columns.refine_columns(PAR,core,cc,cn,ci,{},result,RoutingOptions())
        for i in core.fixed:self.assertEqual(refined.cell_of[i],core.cell_of[i])
        self.assertEqual(core.cell_of,before['cell_of'])
        self.assertEqual(core.orient,before['orient'])
        self.assertEqual(core.rng.getstate(),before['rng'].getstate())

    def test_packing_preserves_the_resolved_column_and_module_order(self):
        devices=bias_devices();comps,nets,index,plan=model(devices)
        core,cc,cn,ci=base_placement({n:d for n,d in devices.items() if n!='R'},
                                   dict(upper=(0,2),lower=(2,2),tap=(0,4)))
        col=columns.build_column_plan(PAR,core,cc,cn,ci,inline_bridges=[plan['groups'][0]['conduction']])
        core.column_plan=col;before=deepcopy(core.__dict__)
        with patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('No A* in packing')):
            frame,report=pack(PAR,core,comps,nets,index,plan,RoutingOptions())
        self.assertIsNotNone(frame,report)
        self.assertTrue(report['column_contract_preserved'])
        self.assertEqual(core.cell_of,before['cell_of'])
        self.assertEqual(core.column_x,before['column_x'])
        self.assertEqual(core.row_y,before['row_y'])
        self.assertLess(frame.pos_of(index['upper'])[1],frame.pos_of(index['R'])[1])
        self.assertLess(frame.pos_of(index['R'])[1],frame.pos_of(index['lower'])[1])
        self.assertEqual(modules.measure(frame,comps,index,plan)['inline_error'],0)

    def test_spacing_uses_conduction_hosts_instead_of_control_fanout(self):
        from statistics import median
        from netlist2schematic.plugins.passive_skeleton.module_connections import rebase_after_axis_expansion
        devices=bias_devices()
        for k in range(5):devices['fanout'+str(k)]=dict(devices['tap'],d='out'+str(k))
        comps,nets,index,plan=model(devices)
        active={n:d for n,d in devices.items() if n!='R'}
        cells=dict(upper=(0,2),lower=(2,2),tap=(0,5))
        cells.update({'fanout'+str(k):(k%3,6+k) for k in range(5)})
        core,cc,cn,ci=base_placement(active,cells)
        frame,report=pack(PAR,core,comps,nets,index,plan,RoutingOptions())
        self.assertIsNotNone(frame,report)
        after=deepcopy(frame)
        after.column_x=[frame.column_x[0]+1.5*(x-frame.column_x[0]) for x in frame.column_x]
        after.row_y=[frame.row_y[0]+1.5*(y-frame.row_y[0]) for y in frame.row_y]
        hosts=plan['groups'][0]['conduction']['conduction_ports'].values()
        expected=[round(median(after.pin_world(index[n],p)[k]-frame.pin_world(index[n],p)[k]
                               for n,p in hosts)/2)*2 for k in (0,1)]
        records=rebase_after_axis_expansion(frame,after,index,plan)
        self.assertEqual(records[0]['translation'],expected)
        before=frame.pos_of(index['R']);current=after.pos_of(index['R'])
        self.assertEqual(tuple(current[k]-before[k] for k in (0,1)),tuple(expected))
        self.assertEqual(frame.cell_of,after.cell_of)
        self.assertEqual(frame.orient,after.orient)
        self.assertFalse(hasattr(after,'passive_module_guides'))


if __name__=='__main__':unittest.main()
