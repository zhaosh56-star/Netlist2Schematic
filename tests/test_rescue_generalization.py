"""Geometry-based rescue must survive renaming and coordinate transformations."""
from time import perf_counter
import unittest

from local_rescue import _route_local_net
from routing_rules import (Pin, RoutingOptions, Scene, analyze_routes,
                           canonical_topology, on_segment)


class RescueGeneralizationTests(unittest.TestCase):
    def test_renaming_order_translation_and_reflection_preserve_feasibility(self):
        for reflected in (False, True):
            for offset in ((0, 0), (137, -59), (-241, 81)):
                for reverse in (False, True):
                    with self.subTest(reflected=reflected, offset=offset, reverse=reverse):
                        sign = -1 if reflected else 1

                        def transform(p):
                            return sign*p[0] + offset[0], p[1] + offset[1]

                        signal, guard = ('renamed_feedback', 'renamed_guard') if reverse else ('u', 'v')
                        pins = [Pin(signal, component, terminal, transform(p), transform(e), (sign, 0))
                                for component, terminal, p, e in
                                ((59, 'left', (0, 0), (18, 0)),
                                 (7, 'right', (0, 100), (18, 100)),
                                 (13, 'third', (0, 200), (18, 200)))]
                        if reverse:
                            pins.reverse()
                        foreign = [(transform((28, -70)), transform((28, 270)))]
                        x0, y0 = transform((-200, -200))
                        x1, y1 = transform((200, 400))
                        scene = Scene(options=RoutingOptions(margins=(80,), max_expansions=20000),
                                      pins={signal: pins, guard: []},
                                      all_pins={signal: [p.point for p in pins], guard: []},
                                      fixed_segments={signal: [p.stub for p in pins], guard: []},
                                      bounds=(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
                        occupied = {signal: list(scene.fixed_segments[signal]), guard: foreign}
                        statistics = {'graph_nodes': 0, 'attempts': 0, 'expanded_states': 0}
                        result, _, _, failure = _route_local_net(
                            scene, signal, occupied, 80, statistics, perf_counter()+10,
                            {'port_plans': 0, 'branches': []})
                        self.assertIsNone(failure)
                        self.assertIsNotNone(result)
                        self.assertEqual([], analyze_routes([(guard, foreign), (signal, result)],
                                                             scene)['violations'])
                        for pin in pins:
                            self.assertTrue(any(on_segment(pin.point, *segment) for segment in result))
                        topology = canonical_topology(result, pins=scene.all_pins[signal])
                        start = pins[0].point
                        seen, todo = set(), [start]
                        while todo:
                            point = todo.pop()
                            if point in seen:
                                continue
                            seen.add(point)
                            todo.extend(topology['adjacency'].get(point, ()))
                        self.assertTrue(set(scene.all_pins[signal]) <= seen)
                        self.assertEqual(set(scene.all_pins[signal]),
                                         {p for p, neighbors in topology['adjacency'].items()
                                          if len(neighbors) == 1})


if __name__ == '__main__':
    unittest.main()
