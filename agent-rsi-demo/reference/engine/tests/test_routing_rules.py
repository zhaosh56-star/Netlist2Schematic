"""Geometry contracts independent of routing order or arbitrary segmentation."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from routing_rules import (RoutingOptions, Scene, Pin, analyze_routes,
                           canonical_topology, build_scene)
from sa_common import load_local


class TopologyTests(unittest.TestCase):
    def test_collinear_grid_nodes_disappear_but_pin_and_junction_remain(self):
        segments = [((x, 0), (x+2, 0)) for x in range(0, 20, 2)]
        segments += [((10, 0), (10, 20))]
        topology = canonical_topology(segments, pins=[(4, 0)])
        self.assertEqual([((0,0),(20,0)), ((10,0),(10,20))], topology['runs'])
        self.assertEqual({(0,0),(4,0),(10,0),(20,0),(10,20)}, topology['structure_points'])
        self.assertEqual(4, len(topology['segments']))

    def test_split_x_is_the_same_geometry_as_unsplit_x(self):
        first = [('a', [((-40,0),(40,0))]), ('b', [((0,-40),(0,40))])]
        second = [('a', [((-40,0),(0,0)),((0,0),(40,0))]),
                  ('b', [((0,-40),(0,0)),((0,0),(0,40))])]
        a, b = analyze_routes(first, Scene()), analyze_routes(second, Scene())
        self.assertEqual(1, a['crossings'])
        self.assertEqual(a['crossings'], b['crossings'])
        self.assertEqual([], b['violations'])


class ClearanceTests(unittest.TestCase):
    def test_parallel_spacing_2_4_8_fail_and_10_passes(self):
        for gap in (2,4,8,10):
            with self.subTest(gap=gap):
                analysis=analyze_routes([('a',[((0,0),(40,0))]),
                                         ('b',[((0,gap),(40,gap))])], Scene())
                self.assertEqual(gap < 10, bool(analysis['violations']))

    def test_collinear_overlap_is_hard_error(self):
        analysis=analyze_routes([('a',[((0,0),(40,0))]),
                                 ('b',[((20,0),(60,0))])], Scene())
        self.assertEqual(20, analysis['overlap'])
        self.assertIn('overlap',{v['kind'] for v in analysis['violations']})

    def test_foreign_t_and_shared_endpoint_are_rejected(self):
        for segment in (((0,0),(0,40)), ((40,0),(40,40))):
            with self.subTest(segment=segment):
                analysis=analyze_routes([('a',[((-40,0),(40,0))]), ('b',[segment])],Scene())
                self.assertIn('foreign_contact',{v['kind'] for v in analysis['violations']})

    def test_x_near_pin_bend_or_junction_is_rejected(self):
        cases=[(Scene(all_pins={'a':[(0,0)]}), [((-40,0),(40,0))]),
               (Scene(), [((0,0),(40,0)),((0,0),(0,40))]),
               (Scene(), [((-40,0),(40,0)),((0,0),(0,40))])]
        for scene,segments in cases:
            with self.subTest(segments=segments):
                analysis=analyze_routes([('a',segments),('b',[((8,-40),(8,40))])],scene)
                self.assertIn('crossing_clearance',{v['kind'] for v in analysis['violations']})

    def test_x_exactly_12_from_structure_is_allowed(self):
        analysis=analyze_routes([('a',[((0,0),(60,0))]),
                                 ('b',[((12,-40),(12,40))])],Scene())
        self.assertEqual([],analysis['violations'])

    def test_a_star_subdivision_may_continue_straight_through_x(self):
        scene=Scene()
        occupied=scene.prepare_occupied({'b':[((0,-40),(0,40))]})
        self.assertTrue(scene.edge_check('a',(-20,0),(-2,0),occupied)[0])
        self.assertTrue(scene.edge_check('a',(-2,0),(2,0),occupied)[0])
        self.assertFalse(scene.vertex_check('a',(0,0),occupied)[0])
        self.assertTrue(scene.vertex_check('a',(-12,0),occupied)[0])

    def test_fixed_foreign_pin_stub_cannot_be_crossed(self):
        scene=Scene(fixed_segments={'b':[((0,-40),(0,40))]})
        self.assertFalse(scene.edge_check('a',(-40,0),(40,0),{})[0])
        analysis=analyze_routes([('a',[((-40,0),(40,0))]),
                                 ('b',[((0,-40),(0,40))])],scene)
        self.assertIn('fixed_stub_clearance',{v['kind'] for v in analysis['violations']})

    def test_only_designated_pin_corridor_is_exempt_from_own_halo(self):
        pin=Pin('a',0,'P',(0,0),(22,0),(1,0))
        scene=Scene(body_rects=[(0,(-20,-20,20,20))],pins={'a':[pin]},
                    fixed_segments={'a':[pin.stub]},all_pins={'a':[pin.point]})
        self.assertTrue(scene.edge_check('a',(0,0),(40,0),{})[0])
        self.assertFalse(scene.edge_check('a',(0,0),(0,40),{})[0])
        self.assertFalse(scene.edge_check('b',(0,0),(40,0),{})[0])


class ExistingJunctionClearanceTests(unittest.TestCase):
    """Existing foreign branches obey the same quiet zone in search and validation."""

    @staticmethod
    def branch():
        return [((-40,0),(40,0)), ((0,-40),(0,0))]

    def test_foreign_junction_rejects_10_and_11_but_allows_12(self):
        scene = Scene()
        occupied = scene.prepare_occupied({'existing': self.branch()})
        for gap in (10, 11, 12):
            with self.subTest(gap=gap):
                edge = ((-100,gap), (100,gap))
                allowed, crossings, blockers = scene.edge_check('new', *edge, occupied)
                self.assertEqual(gap == 12, allowed)
                self.assertEqual(0, crossings)
                self.assertEqual(set() if gap == 12 else {'existing'}, blockers)
                report = analyze_routes([('existing', self.branch()), ('new', [edge])], scene)
                self.assertEqual(gap < 12, bool(report['violations']))

    def test_junction_quiet_zone_uses_custom_cross_clearance(self):
        scene = Scene(options=RoutingOptions(cross_clearance=18))
        occupied = scene.prepare_occupied({'existing': self.branch()})
        for gap in (12, 17, 18):
            with self.subTest(gap=gap):
                allowed, _, blockers = scene.edge_check('new', (-100,gap), (100,gap), occupied)
                self.assertEqual(gap == 18, allowed)
                self.assertEqual(set() if gap == 18 else {'existing'}, blockers)

    def test_straight_foreign_run_still_allows_parallel_10_spacing(self):
        scene = Scene()
        occupied = scene.prepare_occupied({'existing': [((-40,0),(40,0))]})
        self.assertEqual((True, 0, set()),
                         scene.edge_check('new', (-100,10), (100,10), occupied))

    def test_same_net_can_extend_and_join_its_own_existing_branch(self):
        scene = Scene()
        occupied = scene.prepare_occupied({'same': self.branch()})
        self.assertEqual((True, 0, set()), scene.edge_check('same', (0,0), (0,40), occupied))
        self.assertEqual([], analyze_routes([('same', [*self.branch(), ((0,0),(0,40))])], scene)['violations'])

    def test_legal_crossing_at_junction_quiet_boundary_still_passes(self):
        scene = Scene()
        occupied = scene.prepare_occupied({'existing': self.branch()})
        edge = ((12,-40), (12,40))
        self.assertEqual((True, 1, set()), scene.edge_check('new', *edge, occupied))
        report = analyze_routes([('existing', self.branch()), ('new', [edge])], scene)
        self.assertEqual([], report['violations'])
        self.assertEqual(1, report['crossings'])

    def test_collinear_subdivision_does_not_change_junction_guard(self):
        unsplit = self.branch()
        split = [((-40,0),(-10,0)), ((-10,0),(0,0)), ((0,0),(10,0)), ((10,0),(40,0)),
                 ((0,-40),(0,-10)), ((0,-10),(0,0))]
        scene = Scene()
        for gap in (10, 11, 12):
            results = [scene.edge_check('new', (-100,gap), (100,gap),
                                       scene.prepare_occupied({'existing': runs}))
                       for runs in (unsplit, split)]
            self.assertEqual(results[0], results[1])
            self.assertEqual(gap == 12, results[0][0])


class SceneTests(unittest.TestCase):
    def test_default_spacing_and_rendered_power_geometry(self):
        self.assertEqual(10,RoutingOptions().center_spacing)
        par=load_local('place_and_route','place-and-route.py')
        doc={'id':'V1','symbol':'voltage-source','nodes':{'+':'vdd','-':'0'}}
        pl=par.Placement.__new__(par.Placement)
        pl.comps=[doc];pl.cell_of=[(0,0)];pl.orient=[1]
        nets={'vdd':[('V1','+')],'0':[('V1','-')]}
        scene=build_scene(par,nets,pl,{'V1':0})
        self.assertEqual({'vdd','0'},scene.power_nets)
        self.assertEqual({},scene.pins)
        for net, members in nets.items():
            point=pl.pin_world(0,members[0][1])
            delta=-24 if net=='vdd' else 24
            self.assertEqual([(point,(point[0],point[1]+delta))],scene.fixed_segments[net])
        self.assertEqual(2,len(scene.marker_rects))


if __name__=='__main__':
    unittest.main()
