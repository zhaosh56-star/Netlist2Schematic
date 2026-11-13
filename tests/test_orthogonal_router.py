"""Behavioral checks for fixed-placement orthogonal final routing."""
from __future__ import annotations

from pathlib import Path
import unittest

from netlist2schematic.routing.orthogonal_router import RoutingFailure, route_scene, _route_net
from netlist2schematic.routing.routing_rules import Pin, RoutingOptions, Scene, analyze_routes

ROOT = Path(__file__).resolve().parents[1]


def simple_scene(points, rectangles=(), options=None):
    pins = {net:[Pin(net,index,'P',point,point,(1,0)) for index,point in enumerate(coords)]
            for net,coords in points.items()}
    return Scene(options=options or RoutingOptions(), body_rects=list(enumerate(rectangles)),
                 pins=pins, all_pins={net:list(coords) for net,coords in points.items()},
                 fixed_segments={net:[] for net in points}, bounds=(-20,-20,120,120))


class OrthogonalSearchTests(unittest.TestCase):
    def test_route_avoids_existing_foreign_junction_before_final_validation(self):
        existing = [((0,-100),(0,100)), ((-100,-100),(0,-100)),
                    ((-100,0),(0,0)), ((-100,100),(0,100))]
        points = {'existing': [(-100,-100),(-100,0),(-100,100)],
                  'new': [(10,-150),(10,150)]}
        scene = simple_scene(points, options=RoutingOptions(margins=(150,)))
        scene.bounds = (-120,-170,40,170)
        direct = analyze_routes([('existing',existing), ('new',[((10,-150),(10,150))])], scene)
        self.assertTrue(any(item['kind'] == 'structure_clearance' and item.get('owner') == 'existing'
                            for item in direct['violations']))
        statistics = {'graph_nodes': 0, 'attempts': 0, 'expanded_states': 0}
        routed, _ = _route_net(scene, 'new', {'existing': existing}, 150, [], statistics)
        self.assertIsNotNone(routed)
        self.assertTrue(any(a[0] == b[0] == 12 and min(a[1], b[1]) <= 0 <= max(a[1], b[1])
                            for a, b in routed), routed)
        self.assertEqual([], analyze_routes([('existing', existing), ('new', routed)], scene)['violations'])
        for point in points['new']:
            self.assertTrue(any(point in segment for segment in routed))
        self.assertEqual(1, statistics['attempts'])

    def test_routes_around_multiple_obstacles_and_preserves_terminals(self):
        scene=simple_scene({'a':[(0,0),(100,0),(100,100)]},
                           [(20,-10,40,30),(60,-30,80,10)])
        routed,diagnostics=route_scene(scene)
        self.assertEqual([],analyze_routes(routed,scene)['violations'])
        self.assertTrue(diagnostics['expanded_states'])
        segments=dict(routed)['a']
        for point in ((0,0),(100,0),(100,100)):
            self.assertTrue(any(point in segment for segment in segments))

    def test_fixed_input_is_deterministic(self):
        scene=simple_scene({'a':[(0,0),(100,0)],'b':[(0,40),(100,40)]})
        first,_=route_scene(scene)
        second,_=route_scene(scene)
        self.assertEqual(first,second)

    def test_node_budget_is_a_diagnostic_failure(self):
        scene=simple_scene({'a':[(0,0),(100,0)]},options=RoutingOptions(max_nodes=1))
        with self.assertRaises(RoutingFailure) as caught:
            route_scene(scene)
        self.assertEqual('graph_node_budget',caught.exception.diagnostics['reason'])

    def test_expansion_budget_is_a_diagnostic_failure(self):
        scene=simple_scene({'a':[(0,0),(100,0)]},options=RoutingOptions(max_expansions=1))
        with self.assertRaises(RoutingFailure) as caught:
            route_scene(scene)
        self.assertEqual('state_budget',caught.exception.diagnostics['reason'])

    def test_congested_scene_requires_and_repeats_the_same_ripup(self):
        # The first net can initially pass 10 px beyond b's fixed portal ends.
        # That leaves too little quiet space for b's branch search to start, so
        # a must be ripped up and routed after b. The former random obstacle
        # fixture routes directly once junction clearance is enforced early.
        points={'a':[(-100,10),(100,10)], 'b':[(-50,-40),(50,-40)]}
        scene=simple_scene(points, options=RoutingOptions(
            margins=(50,), max_expansions=10000, crossing_cost=500, bend_cost=30))
        scene.pins['b']=[Pin('b',i,'P',point,(point[0],0),(0,1))
                         for i,point in enumerate(points['b'])]
        scene.fixed_segments['b']=[pin.stub for pin in scene.pins['b']]
        scene.bounds=(-120,-60,120,40)
        first,diagnostics=route_scene(scene)
        second,repeated=route_scene(scene)
        self.assertGreater(diagnostics['reroute_rounds'],0)
        self.assertLessEqual(diagnostics['reroute_rounds'],scene.options.max_rounds)
        self.assertEqual(diagnostics['reroute_rounds'],repeated['reroute_rounds'])
        self.assertEqual(first,second)
        self.assertEqual([],analyze_routes(first,scene)['violations'])

    def test_conflicting_fixed_portals_fail_with_network_and_position(self):
        points={'a':[(0,0)],'b':[(0,5)]}
        scene=simple_scene(points,options=RoutingOptions(max_rounds=0,margins=(50,)))
        scene.pins={net:[Pin(net,i,'P',coords[0],(20,coords[0][1]),(1,0))]
                    for i,(net,coords) in enumerate(points.items())}
        scene.fixed_segments={net:[pin.stub for pin in pins] for net,pins in scene.pins.items()}
        with self.assertRaises(RoutingFailure) as caught:
            route_scene(scene)
        self.assertEqual('candidate_validation',caught.exception.diagnostics['reason'])
        violations=caught.exception.diagnostics['violations']
        self.assertTrue(any(set(item['nets']) == {'a','b'} and item['point'] is not None
                            for item in violations))

    def test_power_marker_is_an_immutable_obstacle(self):
        scene=simple_scene({'a':[(0,0),(100,0)]})
        scene.marker_rects=[('vdd',(40,-10,60,10))]
        scene.power_nets={'vdd'}
        scene.fixed_segments['vdd']=[((50,-5),(50,5))]
        routed,_=route_scene(scene)
        self.assertEqual([],analyze_routes(routed,scene)['violations'])
        self.assertTrue(any(abs(point[1])>10 for segment in dict(routed)['a'] for point in segment))


if __name__ == '__main__':
    unittest.main()
