"""Independent checks for explicitly routed power pins and relocated markers."""
from copy import deepcopy
import json
from time import perf_counter
import unittest
from unittest.mock import patch

from orthogonal_router import RoutingFailure
from routing_rules import build_scene
from sa_common import load_local
from sa_routing import RoutingValidationError, validate_final_routes


def circuit(count=1):
    par = load_local('place_and_route', 'place-and-route.py')
    comps = [{'id': f'V{i+1}', 'symbol': 'voltage-source', 'nodes': {'+': 'vdd', '-': '0'}}
             for i in range(count)]
    placement = par.Placement.__new__(par.Placement)
    placement.comps = comps
    placement.orient = [0] * count
    placement.cell_of = [(0, i) for i in range(count)]
    placement.comp_at = {cell: i for i, cell in enumerate(placement.cell_of)}
    placement.column_x = [300 * i for i in range(count)]
    placement.row_y = [0]
    nets = {'vdd': [(comp['id'], '+') for comp in comps],
            '0': [(comp['id'], '-') for comp in comps]}
    index = {comp['id']: i for i, comp in enumerate(comps)}
    placement.power_connections = []
    for i, comp in enumerate(comps):
        x = placement.pos_of(i)[0]
        for pin, net, symbol, y in (('+', 'vdd', 'vdd-port', -150), ('-', '0', 'ground', 150)):
            anchor = (x, y)
            placement.power_connections.append({
                'component_id': comp['id'], 'pin': pin, 'net': net, 'symbol': symbol,
                'anchor': anchor, 'segments': [(placement.pin_world(i, pin), anchor)]})
    return par, placement, comps, nets, index


class ExplicitPowerConnectionTests(unittest.TestCase):
    def validate(self, fixture, **kwargs):
        par, placement, _, nets, index = fixture
        return validate_final_routes(par, nets, placement, index, [], **kwargs)

    def test_valid_relocated_markers_use_real_symbol_pins_without_astar(self):
        fixture = circuit()
        par, placement, _, nets, index = fixture
        before = deepcopy(placement.power_connections)
        with patch('orthogonal_router.route_scene', side_effect=AssertionError('validation must not route')):
            routed, metrics, validation = self.validate(fixture)
        self.assertEqual([], routed)
        self.assertTrue(validation['valid'])
        self.assertEqual(2, validation['power_connections_checked'])
        self.assertEqual(260, metrics['power_wirelength'])
        scene = build_scene(par, nets, placement, index)
        self.assertEqual({}, scene.pins)
        self.assertEqual(2, len(scene.power_corridors))
        self.assertEqual((0, -170), scene.power_markers[0]['origin'])
        self.assertEqual((0, 160), scene.power_markers[1]['origin'])
        self.assertEqual(before, placement.power_connections)
        json.dumps(scene.power_connections, allow_nan=False)

    def test_no_attribute_or_none_preserves_legacy_power_stubs(self):
        par, placement, _, nets, index = circuit()
        del placement.power_connections
        first = build_scene(par, nets, placement, index)
        placement.power_connections = None
        second = build_scene(par, nets, placement, index)
        self.assertIsNone(first.power_connections)
        self.assertEqual(first.fixed_segments, second.fixed_segments)
        self.assertEqual([((0, -20), (0, -44))], first.fixed_segments['vdd'])

    def test_explicit_empty_list_does_not_silently_restore_old_stubs(self):
        fixture = circuit()
        fixture[1].power_connections = []
        with self.assertRaises(RoutingValidationError) as caught:
            self.validate(fixture)
        self.assertEqual('power_connections', caught.exception.diagnostics['reason'])

    def test_missing_duplicate_unknown_and_wrong_network_records_are_rejected(self):
        for mutation in ('missing', 'duplicate', 'unknown', 'wrong_net', 'wrong_symbol', 'nonlist'):
            with self.subTest(mutation=mutation):
                fixture = circuit()
                records = fixture[1].power_connections
                if mutation == 'missing': records.pop()
                elif mutation == 'duplicate': records.append(deepcopy(records[0]))
                elif mutation == 'unknown': records[0]['component_id'] = 'not-a-source-device'
                elif mutation == 'wrong_net': records[0]['net'] = '0'
                elif mutation == 'wrong_symbol': records[0]['symbol'] = 'ground'
                else: fixture[1].power_connections = {}
                with self.assertRaises(RoutingValidationError) as caught:
                    self.validate(fixture)
                json.dumps(caught.exception.diagnostics, allow_nan=False)

    def test_nonfinite_fake_anchor_disconnection_diagonal_and_extra_branch_are_rejected(self):
        for mutation in ('nonfinite', 'fake_anchor', 'disconnected', 'diagonal', 'extra_branch', 'loop'):
            with self.subTest(mutation=mutation):
                fixture = circuit()
                record = fixture[1].power_connections[0]
                if mutation == 'nonfinite': record['anchor'] = [float('nan'), -150]
                elif mutation == 'fake_anchor': record['anchor'] = [99, -150]
                elif mutation == 'disconnected': record['segments'] = [((0,-20),(0,-80)), ((5,-80),(5,-150))]
                elif mutation == 'diagonal': record['segments'] = [((0,-20),(1,-150))]
                elif mutation == 'extra_branch': record['segments'].append(((0,-80),(20,-80)))
                else:
                    record['segments'] = [((0,-20),(0,-80)), ((0,-80),(40,-80)),
                                          ((40,-80),(40,-40)), ((40,-40),(0,-40)),
                                          ((0,-40),(0,-150))]
                with self.assertRaises(RoutingValidationError):
                    self.validate(fixture)

    def test_original_pin_exit_and_marker_entry_directions_are_required(self):
        fixture = circuit()
        record = fixture[1].power_connections[0]
        record['anchor'] = (80,-150)
        record['segments'] = [((0,-20),(80,-20)), ((80,-20),(80,-150))]
        with self.assertRaisesRegex(RoutingValidationError, 'declared direction'):
            self.validate(fixture)
        fixture = circuit()
        record = fixture[1].power_connections[0]
        record['anchor'] = (40,-150)
        record['segments'] = [((0,-20),(0,-220)), ((0,-220),(80,-220)),
                              ((80,-220),(80,-150)), ((80,-150),(40,-150))]
        with self.assertRaisesRegex(RoutingValidationError, 'marker pin'):
            self.validate(fixture)

    def test_correct_first_exit_does_not_exempt_later_own_body_crossing(self):
        fixture = circuit()
        record = fixture[1].power_connections[0]
        record['anchor'] = (-160,-150)
        vertices = [(0,-20),(0,-80),(80,-80),(80,0),(-160,0),(-160,-150)]
        record['segments'] = list(zip(vertices, vertices[1:]))
        with self.assertRaises(RoutingValidationError) as caught:
            self.validate(fixture)
        self.assertIn('power_body', {v['kind'] for v in caught.exception.diagnostics['violations']})

    def test_power_lead_cannot_cross_another_device(self):
        # Audit the first positive lead against an independently occupied body.
        fixture = circuit()
        par, placement, comps, nets, index = fixture
        comps.append({'id': 'R1', 'symbol': 'resistor', 'nodes': {'1': 'a', '2': 'b'}})
        placement.orient.append(0)
        placement.cell_of.append((1,0))
        placement.row_y.append(-80)
        index['R1'] = 1
        nets.update(a=[('R1','1')], b=[('R1','2')])
        with self.assertRaises(RoutingValidationError) as caught:
            self.validate(fixture)
        self.assertTrue(any(v['kind'] == 'power_body' and v['component'] == 1
                            for v in caught.exception.diagnostics['violations']))

    def test_power_lead_cannot_enter_a_different_marker_even_on_the_same_net(self):
        fixture = circuit(2)
        records = fixture[1].power_connections
        records[0].update(anchor=(600,-150), segments=[((0,-20),(0,-100)),
                          ((0,-100),(600,-100)), ((600,-100),(600,-150))])
        records[2].update(anchor=(300,-100), segments=[((300,-20),(300,-100))])
        with self.assertRaises(RoutingValidationError) as caught:
            self.validate(fixture)
        self.assertTrue(any(v['kind'] == 'power_marker' and v['connection'] == ['V1','+']
                            and v['marker'] == ['V2','+'] for v in caught.exception.diagnostics['violations']))

    def test_only_the_final_port_approach_can_enter_its_own_marker_halo(self):
        fixture = circuit()
        record = fixture[1].power_connections[0]
        record['anchor'] = (200,-150)
        vertices = [(0,-20),(0,-160),(250,-160),(250,-100),(200,-100),(200,-150)]
        record['segments'] = list(zip(vertices, vertices[1:]))
        with self.assertRaises(RoutingValidationError) as caught:
            self.validate(fixture)
        self.assertIn('power_marker', {v['kind'] for v in caught.exception.diagnostics['violations']})

    def test_marker_halo_cannot_overlap_a_device(self):
        fixture = circuit()
        fixture[1].power_connections[0].update(anchor=(0,-40), segments=[((0,-20),(0,-40))])
        with self.assertRaises(RoutingValidationError) as caught:
            self.validate(fixture)
        self.assertIn('power_marker_body', {v['kind'] for v in caught.exception.diagnostics['violations']})

    def test_signal_cannot_cross_an_explicit_power_lead(self):
        par, placement, _, nets, index = circuit()
        scene = build_scene(par, nets, placement, index)
        from routing_rules import analyze_routes
        report = analyze_routes([('signal',[((-100,-80),(100,-80))])], scene)
        self.assertTrue(any(v['kind'] in ('crossing_clearance', 'fixed_stub_clearance')
                            for v in report['violations']))

    def test_different_power_nets_do_not_bypass_foreign_wire_checks(self):
        fixture = circuit()
        ground = fixture[1].power_connections[1]
        ground['anchor'] = (-100,150)
        vertices = [(0,20),(0,80),(100,80),(100,-100),(-100,-100),(-100,150)]
        ground['segments'] = list(zip(vertices, vertices[1:]))
        with self.assertRaises(RoutingValidationError) as caught:
            self.validate(fixture)
        self.assertTrue(any(v['kind'] in ('crossing_clearance', 'fixed_stub_clearance')
                            and set(v['nets']) == {'vdd','0'}
                            for v in caught.exception.diagnostics['violations']))

    def test_validation_deadline_is_enforced_without_running_astar(self):
        with self.assertRaises(RoutingFailure) as caught:
            self.validate(circuit(), deadline=perf_counter()-1)
        self.assertEqual('quality_time_budget', caught.exception.diagnostics['reason'])

    def test_invalid_absolute_deadlines_are_rejected(self):
        for deadline in (float('nan'), float('inf'), float('-inf'), True, False, 'later'):
            with self.subTest(deadline=deadline), self.assertRaisesRegex(ValueError, 'finite absolute time'):
                self.validate(circuit(), deadline=deadline)
        _, _, validation = self.validate(circuit(), deadline=perf_counter()+10)
        self.assertTrue(validation['valid'])


if __name__ == '__main__':
    unittest.main()
