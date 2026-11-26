"""Shared-node module access uses real ports, not instance-name heuristics."""
from copy import deepcopy
from itertools import islice
from time import perf_counter
import unittest

from tests.test_passive_placement import base_placement, mos, resistor
from tests.test_series_alignment import PAR
from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.plugins.passive_skeleton.plan import compile_plan
from netlist2schematic.plugins.passive_skeleton.placement import make_frame, _anchor_geometry
from netlist2schematic.plugins.passive_skeleton.packing_search import anchor_profiles, placement_trials
from netlist2schematic.routing.routing_rules import RoutingOptions


def fixture(count=2, names=None):
    names = names or ['host'+str(k) for k in range(count)]
    devices = {n: mos('out'+str(k), 'input', '0') for k, n in enumerate(names)}
    cells = {n: (k, k+1) for k, n in enumerate(names)}
    core, _, _, _ = base_placement(devices, cells)
    devices['load'] = resistor('input', '0')
    doc = to_document(devices); comps, nets = doc['components'], doc['nets']
    index = {c['id']: i for i, c in enumerate(comps)}
    plan = compile_plan(PAR, devices, comps, nets, analyze_structures(devices))
    options = RoutingOptions()
    frame = make_frame(PAR, core, comps, index, options)
    return frame, comps, nets, index, plan['groups'][0], options


class SharedAnchorTests(unittest.TestCase):
    def test_shared_node_keeps_all_real_hosts_but_explores_both_accesses(self):
        frame, comps, nets, index, group, options = fixture()
        old = deepcopy(group)
        profiles = list(anchor_profiles(PAR, frame, comps, index, group, options))
        ports = [_anchor_geometry(PAR, frame, comps, index, p, options)[1] for p in profiles]
        self.assertEqual({tuple(p) for p in ports}, {('host0','G'), ('host1','G')})
        self.assertEqual(group, old)
        self.assertTrue(all(p['anchors']==old['anchors'] for p in profiles))

    def test_explicit_owner_and_tail_center_are_not_reassigned(self):
        frame, comps, nets, index, group, options = fixture()
        for field, value in [('owner_port', {'terminal': ['host0','G']}),
                             ('source_node_anchor', {'net': 'input', 'terminals': group['anchors']['input']})]:
            with self.subTest(field=field):
                protected = dict(group, **{field: value})
                self.assertEqual(list(anchor_profiles(PAR,frame,comps,index,protected,options)), [protected])

    def test_unique_conduction_preference_does_not_hide_shared_control_host(self):
        devices=dict(up=mos('out','control','input'),down=mos('other','input'),
                     load=resistor('input','0'))
        core,_,_,_=base_placement({n:d for n,d in devices.items() if n!='load'},
                                  dict(up=(0,2),down=(2,1)))
        doc=to_document(devices);comps,nets=doc['components'],doc['nets']
        index={c['id']:i for i,c in enumerate(comps)};options=RoutingOptions()
        frame=make_frame(PAR,core,comps,index,options)
        preferred=compile_plan(PAR,devices,comps,nets,[])['groups'][0]
        self.assertEqual(preferred['owner_port']['terminal'],['up','S'])
        profiles = list(anchor_profiles(PAR,frame,comps,index,preferred,options))
        self.assertEqual(len(profiles),2)
        self.assertEqual(profiles[0]['owner_port'],preferred['owner_port'])
        self.assertEqual({tuple(_anchor_geometry(PAR,frame,comps,index,p,options)[1]) for p in profiles},
                         {('up','S'),('down','G')})
        locked = dict(preferred, lock_access_anchor=True)
        self.assertEqual(list(anchor_profiles(PAR,frame,comps,index,locked,options)),[locked])

    def test_wider_module_is_measured_against_its_actual_spacing(self):
        from tests.test_passive_modules import fixture as nested
        from netlist2schematic.plugins.passive_skeleton.plan import measure
        from netlist2schematic.plugins.passive_skeleton.placement import pack
        _,core,comps,nets,index,plan=nested()
        plan['spacing_policy']='standard'
        for group in plan['groups']:group['extra_keepout']=10
        result,report=pack(PAR,core,comps,nets,index,plan,RoutingOptions(),seconds=2)
        self.assertIsNotNone(result,report)
        metrics=measure(result,comps,index,report['plan'])
        self.assertEqual(metrics['regularity']['shape_error'],0)

    def test_fanout_is_bounded_and_geometry_equivalent_after_rename_translation(self):
        first = fixture(6)
        second = fixture(6, ['z','a','w','b','x','c'])
        second[0].column_x=[v+200 for v in second[0].column_x]
        second[0].row_y=[v+300 for v in second[0].row_y]
        def selected(args):
            frame, comps, _, index, group, options = args
            profiles=list(anchor_profiles(PAR,frame,comps,index,group,options))
            return [frame.pin_world(index[t[0]],t[1]) for p in profiles
                    for t in [_anchor_geometry(PAR,frame,comps,index,p,options)[1]]]
        a,b=selected(first),selected(second)
        self.assertEqual(len(a),4)
        self.assertEqual(b,[(x+200,y+300) for x,y in a])

    def test_2d_candidates_visit_both_hosts_without_moving_active_devices(self):
        frame, comps, nets, index, group, options = fixture()
        proposals=list(islice(placement_trials(PAR,frame,comps,nets,index,group,options,
            deadline=perf_counter()+3),24))
        anchors={tuple(p.passive_module_guides[group['id']]['access_anchor']) for p in proposals}
        self.assertEqual(anchors,{('host0','G'),('host1','G')})
        for proposal in proposals:
            self.assertEqual(frame.cell_of,proposal.cell_of)
            self.assertEqual(frame.row_y,proposal.row_y)
            self.assertEqual(frame.column_x,proposal.column_x)
            for name in ('host0','host1'):
                self.assertEqual(frame.pos_of(index[name]),proposal.pos_of(index[name]))
                self.assertEqual(frame.orient[index[name]],proposal.orient[index[name]])


if __name__ == '__main__':
    unittest.main()
