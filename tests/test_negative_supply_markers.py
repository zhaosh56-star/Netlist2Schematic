"""Negative supply stays electrically distinct, including saved-layout replay."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from tests.test_power_stub_direction import source_case, PAR
from netlist2schematic.common.supply_markers import (
    CURRENT_POLICY, LEGACY_POLICY, SYMBOL, negative_connection, saved_module,
    symbol_definition, resource_files)
from netlist2schematic.common.sa_geometry import validate_geometry
from netlist2schematic.common.sa_saved import load_placement
from netlist2schematic.netlist.netlist_core import NetlistError, parse_netlist
from netlist2schematic.routing.routing_rules import RoutingOptions, analyze_routes, build_scene


class NegativeSupplyMarkerTests(unittest.TestCase):
    def test_supply_roles_do_not_merge_negative_ground_or_positive_nets(self):
        for net in ('VEE', 'vee', 'Vee!', 'VEE!'):
            self.assertEqual(PAR.power_marker_sym(net), SYMBOL)
            self.assertIsNone(PAR.legacy_power_marker_sym(net))
        self.assertEqual(PAR.power_marker_sym('VDD'), 'vdd-port')
        for net in ('0', 'GND', 'VSS'):
            self.assertEqual(PAR.power_marker_sym(net), 'ground')
        self.assertIsNone(PAR.power_marker_sym('veebias'))

    def test_one_bar_cap_has_real_top_entry_and_validated_runtime_geometry(self):
        validate_geometry(PAR.GEO_DOC)
        symbol = symbol_definition()
        self.assertEqual(symbol['pins'][0]['direction'], 'north')
        self.assertEqual(symbol['pins'][0]['at'], {'x': 0, 'y': 0})
        self.assertEqual([p['kind'] for p in symbol['primitives']], ['line', 'polygon'])
        self.assertEqual(len(symbol['primitives'][1]['points']), 4)
        self.assertGreater(min(p['y'] for p in symbol['primitives'][1]['points']), 0)
        self.assertIn('<polygon', PAR.svg_symbol_parts(symbol, 0, 'none'))

    def test_all_real_source_pin_directions_exit_owner_and_enter_cap_from_above(self):
        for family in ('ISRC', 'VSRC'):
            for pin in ('+', '-'):
                for orient in range(4):
                    with self.subTest(family=family, pin=pin, orient=orient):
                        pl, comps, nets, index = source_case(family, 'vee', pin, orient)
                        scene = build_scene(PAR, nets, pl, index)
                        self.assertIn('vee', scene.power_nets)
                        record = scene.negative_connections[0]
                        (a, b), (c, d) = record['segments'][0], record['segments'][-1]
                        direction = PAR.DIR_VEC[pl.pin_dir(0, pin)]
                        self.assertGreater(sum((b[k]-a[k])*direction[k] for k in (0, 1)), 0)
                        self.assertEqual(c[0], d[0])
                        self.assertLess(c[1], d[1])
                        self.assertEqual(d, record['anchor'])
                        self.assertEqual(analyze_routes(list(scene.fixed_segments.items()), scene)['violations'], [])
                        self.assertEqual(pl.orient, [orient])

    def test_custom_halo_and_grid_share_the_same_physical_lead(self):
        pl, _, nets, index = source_case('VSRC', 'vee', '+', 0)
        options = RoutingOptions(halo=20, grid_step=4)
        scene = build_scene(PAR, nets, pl, index, options)
        record = negative_connection(PAR, pl, 0, '+', 'vee', halo=20, grid_step=4)
        self.assertEqual(record['segments'], scene.fixed_segments['vee'])
        self.assertEqual(analyze_routes(list(scene.fixed_segments.items()), scene)['violations'], [])

    def test_negative_supply_does_not_relax_e_proxy_ground_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'boundary.cir'
            source.write_text('E1 out VEE in 0 100\n.end\n', encoding='utf8')
            with self.assertRaisesRegex(NetlistError, 'grounded out-'):
                parse_netlist(source)

    def test_saved_policy_is_local_and_unknown_versions_are_rejected(self):
        legacy, policy = saved_module(PAR, {})
        self.assertEqual(policy, LEGACY_POLICY)
        self.assertIsNone(legacy.power_marker_sym('vee'))
        self.assertEqual(PAR.power_marker_sym('vee'), SYMBOL)
        current, policy = saved_module(PAR, {'supply_marker_policy': CURRENT_POLICY})
        self.assertIs(current, PAR)
        with self.assertRaisesRegex(ValueError, 'supply_marker_policy'):
            saved_module(PAR, {'supply_marker_policy': 'unknown'})

    def test_old_saved_placement_preserves_signal_vee_and_positions(self):
        from netlist2schematic.common.sa_common import load_local
        pn = load_local('vee_test_parser', 'parse-netlist.py')
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'legacy.cir'
            source.write_text('R1 a VEE 1k\n.end\n', encoding='utf8')
            doc = pn.parse(source)
            positions = {'R1': {'row': 2, 'col': 3, 'orient': 0}}
            saved = Path(tmp)/'legacy-placement.json'
            data = {'source_netlist': str(source), 'placement': positions}
            saved.write_text(json.dumps(data), encoding='utf8')
            restored, _, par, pl, comps, nets, index = load_placement(saved)
            self.assertIsNone(par.power_marker_sym('vee'))
            self.assertEqual(pl.supply_marker_policy, LEGACY_POLICY)
            self.assertEqual(pl.cell_of, [(2, 3)])
            self.assertEqual(pl.orient, [0])
            self.assertIn('vee', build_scene(par, nets, pl, index).pins)
            data['supply_marker_policy'] = CURRENT_POLICY
            saved.write_text(json.dumps(data), encoding='utf8')
            _, _, par, pl, comps, nets, index = load_placement(saved)
            self.assertEqual(par.power_marker_sym('vee'), SYMBOL)
            self.assertIn('vee', build_scene(par, nets, pl, index).power_nets)

    def test_symbol_dependency_added_only_for_new_negative_supply_interpretation(self):
        pl, comps, nets, index = source_case('ISRC', 'vee', '-', 0)
        legacy, _ = saved_module(PAR, {})
        self.assertEqual(resource_files(legacy, comps), [])
        self.assertEqual(len(resource_files(PAR, comps)), 1)
        comps = deepcopy(comps)
        comps[0]['nodes']['-'] = 'vss'
        self.assertEqual(resource_files(PAR, comps), [])

    def test_body_intrusion_after_exit_is_rejected_without_moving_any_device(self):
        pl, _, nets, index = source_case('ISRC', 'vee', '+', 0)
        scene = build_scene(PAR, nets, pl, index)
        line = scene.negative_connections[0]['segments'][-1]
        x, y = line[1]
        scene.body_rects.append((42, (x-4, y-12, x+4, y+12)))
        faults = analyze_routes(list(scene.fixed_segments.items()), scene)['violations']
        self.assertTrue(any(f['kind'] in ('power_body', 'power_marker_body') and f['component'] == 42
                            for f in faults))

    def test_positive_rail_merge_can_reorder_markers_without_changing_negative_identity(self):
        pl, _, nets, index = source_case('ISRC', 'vee', '-', 0)
        scene = build_scene(PAR, nets, pl, index)
        scene.marker_rects.insert(0, ('vdd', (0, 0, 20, 20)))
        scene.marker_components.insert(0, 44)
        faults = analyze_routes(list(scene.fixed_segments.items()), scene)['violations']
        self.assertEqual(faults, [])
        scene.marker_rects.reverse()
        scene.marker_components.reverse()
        self.assertEqual(analyze_routes(list(scene.fixed_segments.items()), scene)['violations'], [])

    def test_missing_or_wrong_net_negative_cap_is_rejected(self):
        for replacement in (None, 'gnd'):
            pl, _, nets, index = source_case('ISRC', 'vee', '-', 0)
            scene = build_scene(PAR, nets, pl, index)
            if replacement is None:
                scene.marker_rects.clear()
                scene.marker_components.clear()
            else:
                scene.marker_rects[0] = replacement, scene.marker_rects[0][1]
            faults = analyze_routes(list(scene.fixed_segments.items()), scene)['violations']
            self.assertTrue(any(f.get('reason') == 'negative_cap_missing_or_ambiguous' for f in faults))


if __name__ == '__main__':
    unittest.main()
