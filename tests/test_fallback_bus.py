"""Constructive recovery must be valid without annealing or A* searches."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from time import perf_counter
import unittest
from unittest import mock

from netlist2schematic.recovery.fallback_bus import VERSION, create_fallback, reroute_fallback
from netlist2schematic.routing.routing_rules import RoutingOptions, build_scene
from netlist2schematic.common.sa_common import load_local
from netlist2schematic.routing.sa_routing import validate_final_routes
from netlist2schematic.common.sa_saved import load_placement

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / 'tests' / 'fixtures' / 'aesthetic-baseline'
FIVE = ROOT / 'tests' / 'fixtures' / 'junction-clearance' / '5.cir'
par = load_local('place_and_route', 'place-and-route.py')
legacy = load_local('place_layered', 'place-layered.py')
MIXED = '''.model QN NPN (bf=100)
.model QP PNP (bf=80)
.model DM D (is=1e-15)
VDD vdd 0 5
VIN in 0 0.7
R1 vdd c1 10k
Q1 c1 in 0 QN
Q2 c2 in vdd QP
R2 c2 0 10k
D1 c1 clamp DM
R3 clamp 0 100k
E1 out 0 c1 c2 100
R4 out 0 1k
.end
'''


class FallbackBusTests(unittest.TestCase):
    def create_text(self, text, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.cir'
            path.write_text(text, encoding='utf-8')
            before = path.read_bytes()
            result = create_fallback(path, **kwargs)
            self.assertEqual(path.read_bytes(), before)
            return result

    def assert_valid_result(self, result):
        pl, comps, nets, index, metadata, (routes, metrics, validation) = result
        self.assertTrue(validation['valid'])
        self.assertEqual(validation['router'], VERSION)
        self.assertEqual(validation['violations'], [])
        self.assertEqual(metadata['layout_style'], 'bus_fallback')
        self.assertEqual(metadata['routing_stage'], 'bus_fallback')
        self.assertEqual(metadata['fallback']['version'], VERSION)
        self.assertEqual(pl.power_connections, metadata['power_connections'])
        self.assertEqual(len(set(pl.cell_of)), len(comps))
        self.assertEqual(len({cell[1] for cell in pl.cell_of}), len(comps))
        self.assertTrue(all(o == 0 for o in pl.orient))
        self.assertEqual(metrics['body_pen'], 0)
        self.assertEqual(metrics['overlap'], 0)
        self.assertEqual(metrics['proximity'], 0)
        _, measured, check = validate_final_routes(
            par, nets, pl, index, routes, deadline=perf_counter() + 60)
        self.assertTrue(check['valid'])
        self.assertEqual(measured, metrics)
        # Current conduction levels are immutable even though x is repacked.
        levels = metadata['levels']
        for a, la in levels.items():
            for b, lb in levels.items():
                if la < lb:
                    self.assertLess(pl.pos_of(index[a])[1], pl.pos_of(index[b])[1])

    def test_six_circuits_validate_without_sa_or_global_astar(self):
        fixtures = [(BASELINE / f'{n}.cir', None) for n in range(1, 5)]
        fixtures += [(FIVE, 'dut')]
        with mock.patch('netlist2schematic.routing.orthogonal_router.route_scene', side_effect=AssertionError('A* forbidden')), \
                mock.patch.object(legacy, 'create_placement', side_effect=AssertionError('SA forbidden')), \
                mock.patch.object(legacy, 'sa_layered', side_effect=AssertionError('SA forbidden')), \
                mock.patch.object(par, 'sa', side_effect=AssertionError('SA forbidden')):
            for path, cell in fixtures:
                with self.subTest(path=path):
                    before = path.read_bytes()
                    result = create_fallback(path, cell=cell, deadline=perf_counter() + 60)
                    self.assert_valid_result(result)
                    self.assertEqual(path.read_bytes(), before)
            with self.subTest(path='self-contained mixed devices'):
                self.assert_valid_result(self.create_text(MIXED, deadline=perf_counter() + 60))

    def test_grounded_nmos_drain_and_powered_opamp_inputs_are_repaired(self):
        cases = [
            'M1 0 gate source 0 NMOS\nRG gate 0 1k\nRS source 0 1k\n',
            'E1 out 0 input 0 100\nRO out 0 1k\nRI input 0 1k\n',
            'E1 out 0 vdd input 100\nRO out 0 1k\nRI input 0 1k\n',
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assert_valid_result(self.create_text(text, deadline=perf_counter() + 60))

    def test_marker_anchor_is_its_actual_symbol_pin_and_path_is_ordered(self):
        result = self.create_text('M1 out in 0 0 NMOS\nR1 vdd out 1k\n')
        pl, comps, nets, index, metadata, _ = result
        scene = build_scene(par, nets, pl, index)
        self.assertTrue(scene.power_connections)
        for connection in metadata['power_connections']:
            segments = connection['segments']
            self.assertEqual(segments[0][0], list(pl.pin_world(index[connection['component_id']], connection['pin'])))
            self.assertEqual(segments[-1][1], connection['anchor'])
            self.assertTrue(all(a[1] == b[0] for a, b in zip(segments, segments[1:])))
            local = par.GEO_DOC[connection['symbol']]['orientations'][0]['pins'][0]['at']
            origin = [connection['anchor'][0] - local['x'], connection['anchor'][1] - local['y']]
            self.assertEqual([origin[0] + local['x'], origin[1] + local['y']], connection['anchor'])
        self.assertFalse(scene.power_nets & set(scene.pins), 'Power terminals cannot become A* signal targets')

    def test_deterministic_create_and_saved_reroute_preserve_every_saved_value(self):
        first = create_fallback(FIVE, cell='dut', seed=17, deadline=perf_counter() + 60)
        second = create_fallback(FIVE, cell='dut', seed=17, deadline=perf_counter() + 60)
        self.assertEqual(first[0].cell_of, second[0].cell_of)
        self.assertEqual(first[4], second[4])
        self.assertEqual(first[-1][:2], second[-1][:2])
        pl, comps, nets, index, metadata, original = first
        saved = deepcopy((pl.cell_of, pl.orient, pl.column_x, pl.row_y, pl.power_connections, metadata))
        with mock.patch('netlist2schematic.routing.orthogonal_router.route_scene', side_effect=AssertionError('A* forbidden')):
            again = reroute_fallback(par, pl, comps, nets, index, metadata,
                                     deadline=perf_counter() + 60)
        self.assertEqual(again[:2], original[:2])
        self.assertEqual((pl.cell_of, pl.orient, pl.column_x, pl.row_y, pl.power_connections, metadata), saved)

    def test_larger_halo_expands_pitch_instead_of_scaling_symbols(self):
        result = self.create_text('M1 out gate 0 0 NMOS\nR1 vdd out 1k\n',
                                  routing_options=RoutingOptions(halo=40),
                                  deadline=perf_counter() + 60)
        self.assertTrue(result[-1][2]['valid'])
        self.assertGreater(result[4]['fallback']['pitch'], 32)
        self.assertEqual(result[0].orient, [0] * len(result[1]))

    def test_json_reload_rebuilds_identical_routes_without_moving_power(self):
        pl, comps, nets, index, metadata, original = create_fallback(FIVE, cell='dut')
        data = dict(metadata, format_version=1, source_netlist=str(FIVE), source_cell='dut',
                    seed=20261029, mode='layered',
                    placement={c['id']: {'row': pl.cell_of[i][0], 'col': pl.cell_of[i][1],
                                          'orient': pl.orient[i]} for i, c in enumerate(comps)},
                    routes=dict(original[0]), metrics=original[1], validation=original[2])
        with tempfile.TemporaryDirectory() as directory:
            saved = Path(directory) / '5-placement.json'
            saved.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            restored, _, restored_par, restored_pl, cc, nn, ii = load_placement(saved)
            before = deepcopy((restored_pl.cell_of, restored_pl.orient,
                               restored_pl.column_x, restored_pl.row_y,
                               restored_pl.power_connections))
            again = reroute_fallback(restored_par, restored_pl, cc, nn, ii, restored)
            self.assertEqual(again[:2], original[:2])
            self.assertEqual((restored_pl.cell_of, restored_pl.orient,
                              restored_pl.column_x, restored_pl.row_y,
                              restored_pl.power_connections), before)

    def test_passive_rlc_and_no_power_net_are_supported(self):
        for text in ('V1 in 0 1\nR1 in mid 1k\nL1 mid out 1m\nC1 out 0 1n\n',
                     'R1 left right 1k\n'):
            with self.subTest(text=text):
                result = self.create_text(text)
                self.assert_valid_result(result)
                if '0' not in text:
                    self.assertEqual(result[4]['power_connections'], [])

    def test_deadline_is_checked_before_parsing(self):
        with mock.patch('netlist2schematic.recovery.fallback_bus.parse_netlist', side_effect=AssertionError('No parse after deadline')):
            with self.assertRaisesRegex(ValueError, 'time budget'):
                create_fallback(FIVE, deadline=perf_counter() - 1)

    def test_bad_saved_power_or_version_never_falls_back_to_legacy_stubs(self):
        result = self.create_text('M1 out gate 0 0 NMOS\nR1 vdd out 1k\n')
        pl, comps, nets, index, metadata, _ = result
        for replacement in (None, [], [{'net': '0'}]):
            changed = deepcopy(metadata)
            changed['power_connections'] = replacement
            copied = deepcopy(pl)
            copied.power_connections = replacement
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                reroute_fallback(par, copied, comps, nets, index, changed)
        changed = deepcopy(metadata)
        changed['fallback']['version'] = 'unknown'
        with self.assertRaisesRegex(ValueError, 'version'):
            reroute_fallback(par, pl, comps, nets, index, changed)


if __name__ == '__main__':
    unittest.main()
