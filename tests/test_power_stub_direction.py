"""Default supply stubs must not fold backwards through their own device."""
from copy import deepcopy
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_placement_preflight import GridPlacement, PAR
from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.plugins.preflight import placement_preflight as preflight
from netlist2schematic.routing.routing_rules import RoutingOptions, analyze_routes, build_scene


def source_case(family='ISRC', rail='vdd', pin='+', orient=2):
    nodes={'+':'signal','-':'signal'};nodes[pin]=rail
    doc=to_document({'source':dict(cls=family,type='I' if family=='ISRC' else 'V',
        p=nodes['+'],n=nodes['-'],value='DC 1')})
    comps,nets=doc['components'],doc['nets'];index={c['id']:i for i,c in enumerate(comps)}
    pl=GridPlacement.__new__(GridPlacement)
    pl.comps=comps;pl.cell_of=[(1,1)];pl.comp_at={(1,1):0};pl.orient=[orient];pl.row_of=[1]
    pl.column_x=[100,200,300];pl.row_y=[100,200,300];pl.offset_x=[0.]
    pl.fixed=set();pl.fixed_cells={};pl.allowed={};pl.blocks=[]
    return pl,comps,nets,index


def violations(case):
    pl,_,nets,index=case;scene=build_scene(PAR,nets,pl,index)
    return analyze_routes(list(scene.fixed_segments.items()),scene)['violations']


class PowerStubDirectionTests(unittest.TestCase):
    def test_both_source_types_and_both_pin_roles_reject_inward_vdd_or_ground(self):
        for family in ('ISRC','VSRC'):
            for rail,pin,orient in (('vdd','+',2),('vdd','-',0),('0','+',0),('0','-',2)):
                with self.subTest(family=family,rail=rail,pin=pin):
                    case=source_case(family,rail,pin,orient)
                    faults=violations(case)
                    fault=next(f for f in faults if f['kind']=='power_stub_body')
                    self.assertEqual(fault['component'],0);self.assertEqual(fault['pin'],pin)
                    self.assertEqual(fault['nets'],[rail])
                    self.assertFalse(preflight._static_safe(PAR,case[0],case[2],case[3],RoutingOptions(),None))

    def test_outward_or_side_pin_directions_keep_the_original_geometry_legal(self):
        for family in ('ISRC','VSRC'):
            for rail,pin,good in (('vdd','+',0),('vdd','-',2),('0','+',2),('0','-',0)):
                for orient in (good,1,3):
                    with self.subTest(family=family,rail=rail,pin=pin,orient=orient):
                        self.assertEqual(violations(source_case(family,rail,pin,orient)),[])

    def test_repair_rotates_only_the_affected_source_and_preserves_position_and_nodes(self):
        for family in ('ISRC','VSRC'):
            for rail,pin,orient in (('vdd','+',2),('vdd','-',0),('0','+',0),('0','-',2)):
                with self.subTest(family=family,rail=rail,pin=pin):
                    case=source_case(family,rail,pin,orient);pl,comps,nets,index=case
                    old=deepcopy(pl.__dict__);netcopy=deepcopy(nets)
                    with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar')):
                        repaired,report=preflight.repair_static_ports(PAR,*case,RoutingOptions())
                    self.assertEqual(report['stop_reason'],'repaired')
                    self.assertEqual(repaired.cell_of,pl.cell_of);self.assertEqual(repaired.pos_of(0),pl.pos_of(0))
                    self.assertEqual(repaired.comps,comps);self.assertEqual(nets,netcopy)
                    self.assertNotEqual(repaired.orient,pl.orient)
                    self.assertTrue(all(a['kind']=='orientation' for a in report['actions']))
                    self.assertTrue(report['static_verification']['valid'])
                    self.assertEqual(violations((repaired,comps,nets,index)),[])
                    self.assertEqual(pl.__dict__,old)

    def test_fixed_or_orientation_locked_source_stays_invalid_without_a_partial_commit(self):
        for lock in ('fixed','fixed_cells','allowed'):
            with self.subTest(lock=lock):
                case=source_case();pl=case[0]
                if lock=='fixed':pl.fixed={0}
                elif lock=='fixed_cells':pl.fixed_cells={0:(1,1)}
                else:pl.allowed={0:[2]}
                before=deepcopy(pl.__dict__)
                repaired,report=preflight.repair_static_ports(PAR,*case,RoutingOptions())
                self.assertEqual(pl.__dict__,before);self.assertIs(repaired,pl)
                self.assertFalse(report['static_verification']['valid'])
                self.assertTrue(violations(case))

    def test_zero_budget_and_expired_deadline_cannot_mutate_the_source(self):
        for kwargs in ({'seconds':0},{'max_trials':0},{'deadline':perf_counter()-1}):
            with self.subTest(kwargs=kwargs):
                case=source_case();before=deepcopy(case[0].__dict__)
                repaired,report=preflight.repair_static_ports(PAR,*case,RoutingOptions(),**kwargs)
                self.assertIs(repaired,case[0]);self.assertEqual(repaired.__dict__,before)
                self.assertFalse(report['applied']);self.assertFalse(report['actions'])

    def test_source_repair_does_not_allow_cell_movement(self):
        case=source_case();pl,comps,_,_=case;trial=deepcopy(pl)
        trial.cell_of=[(0,1)];trial.comp_at={(0,1):0};trial.row_of=[0]
        self.assertFalse(preflight._valid_state(trial,pl,comps,set(),{0},source_orientations={0}))

    def test_safe_source_is_untouched_and_requires_no_repair_trials(self):
        case=source_case(orient=0);before=deepcopy(case[0].__dict__)
        result,report=preflight.repair_static_ports(PAR,*case,RoutingOptions())
        self.assertIs(result,case[0]);self.assertEqual(result.__dict__,before)
        self.assertEqual(report['stop_reason'],'already_safe');self.assertEqual(report['trials'],0)


if __name__=='__main__':unittest.main()
