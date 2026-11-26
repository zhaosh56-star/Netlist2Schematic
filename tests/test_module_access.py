"""Failed attachment access may use a straight/L, with unchanged hard rules."""
from copy import deepcopy
from collections import Counter
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_orthogonal_router import simple_scene
from netlist2schematic.routing.routing_rules import analyze_routes, Pin
from netlist2schematic.plugins.passive_skeleton.module_access import connect_tree
from netlist2schematic.routing.orthogonal_router import RoutingFailure


class ModuleAccessTests(unittest.TestCase):
    def setup_scene(self, points=None):
        scene=simple_scene(points or {'input':[(0,100),(20,100),(100,0),(100,-30)]})
        scene.module_port_nets={'input'}
        return scene

    def test_connects_all_branches_with_straight_l_and_legal_crossing(self):
        scene=self.setup_scene({'input':[(0,100),(20,100),(100,0),(100,-30)],
                                'other':[(-60,50),(60,50)]})
        tree=[((0,100),(20,100))];occupied={'other':[((-60,50),(60,50))]}
        original=deepcopy((tree,occupied))
        with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('A* called')):
            route,report=connect_tree(scene,'input',tree,scene.pins['input'][2:],occupied)
        self.assertIsNotNone(route,report)
        self.assertTrue(report['accepted'])
        self.assertEqual([],analyze_routes([('input',route),*occupied.items()],scene)['violations'])
        self.assertEqual((tree,occupied),original)

    def test_disabled_for_unrelated_networks(self):
        scene=self.setup_scene();scene.module_port_nets=set()
        route,report=connect_tree(scene,'input',[((0,100),(20,100))],scene.pins['input'][2:],{})
        self.assertIsNone(route);self.assertFalse(report['attempted'])

    def test_ordinary_success_never_calls_patterns(self):
        from netlist2schematic.routing.orthogonal_router import route_scene
        scene=self.setup_scene({'input':[(0,0),(100,0)]})
        with patch('netlist2schematic.plugins.passive_skeleton.module_access.connect_tree',
                   side_effect=AssertionError('normal success entered recovery')):
            routed,report=route_scene(scene)
        self.assertEqual([],analyze_routes(routed,scene)['violations'])

    def test_failed_net_uses_patterns_without_any_region_recognition(self):
        from netlist2schematic.routing.orthogonal_router import route_scene
        scene=self.setup_scene({'input':[(0,0),(100,0)]})
        with patch('netlist2schematic.routing.orthogonal_router._find_path',return_value=(None,Counter())), \
             patch('netlist2schematic.recovery.local_rescue._find_path',side_effect=AssertionError('unneeded A*')):
            routed,report=route_scene(scene)
        repair=report['module_interface_repairs'][0]
        self.assertTrue(repair['repaired'])
        self.assertTrue(repair['module_access_patterns'][0]['accepted'])
        self.assertEqual([],analyze_routes(routed,scene)['violations'])

    def test_body_and_foreign_pin_contact_are_rejected(self):
        for blocker in ('body','pin'):
            with self.subTest(blocker=blocker):
                scene=self.setup_scene({'input':[(0,0),(100,0)]})
                if blocker=='body':scene.body_rects=[(99,(40,-20,60,20))]
                else:scene.all_pins['other']=[(50,0)]
                route,report=connect_tree(scene,'input',[((0,0),(0,0))],scene.pins['input'][1:],{})
                self.assertIsNone(route);self.assertFalse(report['accepted'])

    def test_illegal_t_junction_cannot_replace_a_wire(self):
        scene=self.setup_scene({'input':[(0,0),(100,0)],'other':[(50,-100),(50,0)]})
        occupied={'other':[((50,-100),(50,0))]}
        route,report=connect_tree(scene,'input',[((0,0),(0,0))],scene.pins['input'][1:],occupied)
        self.assertIsNone(route)

    def test_disconnected_tree_cannot_be_reported_as_complete(self):
        scene=self.setup_scene({'input':[(0,0),(100,0)]})
        route,report=connect_tree(scene,'input',[((0,0),(0,0)),((100,0),(100,0))],[],{})
        self.assertIsNone(route);self.assertEqual(report['stop_reason'],'incomplete_net')

    def test_original_exit_is_attached_when_it_already_lies_on_tree(self):
        scene=self.setup_scene({'input':[(0,0),(100,0)]})
        scene.pins['input'][1]=Pin('input',1,'P',(100,0),(80,0),(-1,0))
        scene.fixed_segments['input']=[((100,0),(80,0))]
        route,report=connect_tree(scene,'input',[((0,0),(80,0))],scene.pins['input'][1:],{})
        self.assertIsNotNone(route,report)
        self.assertEqual([],analyze_routes([('input',route)],scene)['violations'])

    def test_timeout_never_mutates_tree_or_occupancy(self):
        scene=self.setup_scene();tree=[((0,100),(20,100))];occupied={}
        before=deepcopy((tree,occupied))
        with self.assertRaises(RoutingFailure):
            connect_tree(scene,'input',tree,scene.pins['input'][2:],occupied,deadline=perf_counter()-1)
        self.assertEqual((tree,occupied),before)

    def test_renamed_mirrored_translated_topology_keeps_ability_to_connect(self):
        original=[(0,100),(20,100),(100,0),(100,-30)]
        for mirror in (1,-1):
            pts=[(mirror*x+300,y+500) for x,y in original]
            scene=simple_scene({'renamed':pts});scene.module_port_nets={'renamed'}
            route,report=connect_tree(scene,'renamed',[(pts[0],pts[1])],scene.pins['renamed'][2:],{})
            self.assertIsNotNone(route,report)
            self.assertEqual([],analyze_routes([('renamed',route)],scene)['violations'])


if __name__=='__main__':unittest.main()
