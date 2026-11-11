"""Net-label selection preserves local wiring without entering the router."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from routing_rules import Pin, Scene, canonical_topology
from net_label_core import build_view
from net_label_validation import display_routes, validate_view


def fixture(routes, points, *, escapes=None):
    """Build explicit real pins and fixed exits, independently of generators."""
    scene = Scene()
    terminal_ids = {}
    component = 0
    for net, values in points.items():
        scene.all_pins[net] = list(values)
        scene.pins[net] = []
        scene.fixed_segments[net] = []
        terminal_ids[net] = []
        for number, raw in enumerate(values):
            point = tuple(raw)
            target = tuple(escapes[net][number]) if escapes else point
            dx, dy = target[0]-point[0], target[1]-point[1]
            direction = ((1 if dx > 0 else -1), 0) if dx else (0, 1 if dy > 0 else -1)
            pin = Pin(net, component, 'P', point, target, direction)
            scene.pins[net].append(pin)
            if point != target:
                scene.fixed_segments[net].append(pin.stub)
            terminal_ids[net].append({'component': f'U{component}', 'pin': 'P', 'point': point})
            component += 1
    return scene, deepcopy(routes), terminal_ids


def straight(length=1000, net='a'):
    return fixture([(net, [((0, 0), (length, 0))])],
                   {net: [(0, 0), (length, 0)]},
                   escapes={net: [(20, 0), (length-20, 0)]})


def local_islands():
    segments, points, exits = [], [], []
    for x in (0, 1000):
        for y in (0, 80, 160):
            points.append((x-40, y)); exits.append((x-20, y))
            segments.append(((x-40, y), (x, y)))
        segments += [((x, 0), (x, 160)), ((x, 160), (x, 400))]
    segments += [((0, 400), (2000, 400)), ((2000, 80), (2000, 400)),
                 ((1960, 80), (2000, 80))]
    points.append((1960, 80)); exits.append((1980, 80))
    return fixture([('shared', segments)], {'shared': points}, escapes={'shared': exits})


def geometric_routes(routed):
    return {net: canonical_topology(segments)['runs'] for net, segments in routed}


class NetLabelCoreTests(unittest.TestCase):
    def build(self, values=None, **kwargs):
        self.scene, self.original, self.terminals = values or straight()
        kwargs.setdefault('seconds', 5)
        self.view = build_view(self.scene, self.original, scale=100,
                               terminal_ids=self.terminals, **kwargs)
        return self.view

    def verify(self):
        report = validate_view(self.scene, self.original, self.view, self.terminals)
        self.assertTrue(report['valid'])
        return report

    def test_long_straight_path_is_eligible_without_any_bend(self):
        view = self.build()
        self.assertEqual(2, len(view['labels']))
        self.assertEqual(2, len(view['groups']))
        self.assertTrue(view['omitted_paths'])
        checked = self.verify()
        self.assertLess(checked['metrics']['wirelength'], 1000)

    def test_short_direct_connection_stays_physical(self):
        view = self.build(straight(180))
        self.assertEqual([], view['labels'])
        self.assertEqual(geometric_routes(self.original), geometric_routes(display_routes(view)))
        self.verify()

    def test_long_detour_can_trigger_below_six_pitches(self):
        routes = [('a', [((0,0),(100,0)), ((100,0),(100,120)),
                         ((100,120),(200,120)), ((200,120),(200,0)),
                         ((200,0),(300,0))])]
        values = fixture(routes, {'a': [(0,0),(300,0)]},
                         escapes={'a': [(20,0),(280,0)]})
        self.assertEqual(2, len(self.build(values)['labels']))
        self.verify()

    def test_three_crossings_trigger_below_four_pitches(self):
        routes = [('a', [((0,0),(390,0))])]
        points, exits = {'a': [(0,0),(390,0)]}, {'a': [(20,0),(370,0)]}
        for name, x in [('b',100),('c',190),('d',280)]:
            routes.append((name, [((x,-100),(x,100))]))
            points[name] = [(x,-100),(x,100)]
            exits[name] = [(x,-80),(x,80)]
        view = self.build(fixture(routes, points, escapes=exits))
        self.assertEqual({'a'}, {label['net'] for label in view['labels']})
        self.assertEqual(0, self.verify()['metrics']['crossings'])

    def test_multi_terminal_net_keeps_three_plus_three_plus_one_local_groups(self):
        view = self.build(local_islands())
        self.assertEqual([1,3,3], sorted(len(group['terminals']) for group in view['groups']))
        self.assertEqual(3, len(view['labels']))
        groups = [set(map(tuple, group['terminals'])) for group in view['groups']]
        for local in ({('U0','P'),('U1','P'),('U2','P')},
                      {('U3','P'),('U4','P'),('U5','P')}):
            self.assertTrue(any(local <= group for group in groups))
        self.verify()

    def test_six_remote_terminals_never_produce_more_than_four_groups(self):
        points = [(x,-100) for x in range(0,6000,1000)]
        segments = [((0,0),(5000,0)), *[((x,-100),(x,0)) for x in range(0,6000,1000)]]
        exits = [(x,-80) for x in range(0,6000,1000)]
        view = self.build(fixture([('a',segments)], {'a': points}, escapes={'a':exits}))
        self.assertGreaterEqual(len(view['groups']), 2)
        self.assertLessEqual(len(view['groups']), 4)
        self.assertEqual(6, sum(len(group['terminals']) for group in view['groups']))
        self.verify()

    def test_fixed_original_exits_remain_fully_covered(self):
        self.build()
        runs = geometric_routes(display_routes(self.view))['a']
        from routing_rules import on_segment
        for a, b in self.scene.fixed_segments['a']:
            self.assertTrue(any(on_segment(a,c,d) and on_segment(b,c,d) for c,d in runs))
        self.verify()

    def test_all_fixed_long_wire_cannot_be_removed(self):
        scene, routed, terminals = straight()
        scene.fixed_segments['a'] = deepcopy(routed[0][1])
        self.assertEqual([], self.build((scene,routed,terminals))['labels'])
        self.verify()

    def test_cycle_is_not_removed_as_a_label_shortcut(self):
        scene, routed, terminals = straight()
        routed[0][1].extend([((0,0),(0,300)), ((0,300),(1000,300)), ((1000,300),(1000,0))])
        view = self.build((scene,routed,terminals))
        self.assertEqual([], view['labels'])
        self.assertEqual(geometric_routes(routed), geometric_routes(display_routes(view)))
        self.verify()

    def test_obstructed_text_returns_original_routes_without_unsafe_labels(self):
        view = self.build(text_obstacles=[(-5000,-5000,5000,5000)])
        self.assertEqual([], view['labels'])
        self.assertEqual(geometric_routes(self.original), geometric_routes(display_routes(view)))

    def test_core_never_calls_sa_or_astar(self):
        with patch('orthogonal_router.route_scene', side_effect=AssertionError('A*')):
            self.assertTrue(self.build()['labels'])
        self.verify()

    def test_power_connections_are_not_rewritten(self):
        scene,routed,terminals = straight()
        power = [((0,200),(1000,200))]
        scene.power_nets.add('vdd')
        scene.fixed_segments['vdd'] = deepcopy(power)
        scene.all_pins['vdd'] = [(0,200),(1000,200)]
        view = self.build((scene,routed,terminals))
        self.assertEqual(power,scene.fixed_segments['vdd'])
        self.assertNotIn('vdd',{label['net'] for label in view['labels']})
        self.verify()

    def test_inputs_are_unchanged(self):
        values = straight()
        before = deepcopy(values)
        self.build(values)
        self.assertEqual(before, values)

    def test_subdivision_and_input_order_do_not_change_display_geometry(self):
        first = self.build()
        scene, routed, terminals = straight()
        routed[0] = ('a', [((x,0),(x+20,0)) for x in reversed(range(0,1000,20))])
        second = self.build((scene,routed,terminals))
        self.assertEqual(geometric_routes(display_routes(first)), geometric_routes(display_routes(second)))

    def test_renamed_net_uses_its_original_name_without_losing_capability(self):
        view = self.build(straight(net='different_name'))
        self.assertEqual(2, len(view['labels']))
        self.assertEqual({'different_name'}, {label['text'] for label in view['labels']})
        self.verify()

    def test_translated_and_mirrored_inputs_still_split_and_validate(self):
        for sx, shift in ((1,(300,500)),(-1,(1200,-300))):
            with self.subTest(sx=sx, shift=shift):
                transform = lambda p: (sx*p[0]+shift[0],p[1]+shift[1])
                points = [transform(p) for p in [(0,0),(1000,0)]]
                exits = [transform(p) for p in [(20,0),(980,0)]]
                values = fixture([('a', [(points[0],points[1])])], {'a':points}, escapes={'a':exits})
                self.assertEqual(2, len(self.build(values)['labels']))
                self.verify()

    def test_zero_budget_keeps_a_legal_unmodified_result(self):
        view = self.build(seconds=0)
        self.assertEqual([], view['labels'])
        self.assertEqual(geometric_routes(self.original), geometric_routes(display_routes(view)))
        self.verify()

    def test_timeout_during_trial_validation_rolls_back_the_whole_split(self):
        from orthogonal_router import RoutingFailure
        import net_label_core
        actual = validate_view

        def interrupt(scene, original, view, terminal_ids, **kwargs):
            if view['labels']:
                raise RoutingFailure('test deadline', {'reason':'quality_time_budget'})
            return actual(scene,original,view,terminal_ids,**kwargs)

        with patch.object(net_label_core,'validate_view',side_effect=interrupt):
            view = self.build()
        self.assertEqual([],view['labels'])
        self.assertEqual(geometric_routes(self.original),geometric_routes(display_routes(view)))
        self.verify()

    def test_invalid_budget_and_scale_are_rejected(self):
        scene, routed, terminals = straight()
        for scale, seconds in ((0,5),(-1,5),(100,-1),(float('nan'),5),(100,float('inf'))):
            with self.subTest(scale=scale,seconds=seconds), self.assertRaises(ValueError):
                build_view(scene,routed,scale=scale,terminal_ids=terminals,seconds=seconds)


if __name__ == '__main__':
    unittest.main()
