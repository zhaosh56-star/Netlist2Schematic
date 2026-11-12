"""Conservative load-bank outlet evidence and actual physical pin direction."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from readability_banks import measure_bank_outlet


class Pins:
    def __init__(self, comps, points, directions):
        self.names = [row['id'] for row in comps]
        self.points = deepcopy(points)
        self.directions = dict(directions)
        self.cell_of = [(1, i) for i in range(len(comps))]
        self.orient = [0]*len(comps)

    def pin_world(self, i, pin):
        return self.points[self.names[i]][pin]

    def pin_dir(self, i, pin):
        return self.directions[(self.names[i], pin)]

    def pos_of(self, i):
        raise AssertionError('outlet measurement must use actual pins, not origins')


def fixture(*, symbol='nmos', rail='vdd', y=220, direction='north', members=3):
    comps = [dict(id='host', symbol=symbol, nodes={'D': 'signal', 'G': 'input', 'S': '0' if symbol == 'nmos' else 'vdd'})]
    points = {'host': {'D': (100, 100), 'G': (60, 140), 'S': (100, 180)}}
    names = []
    for number, kind in enumerate(('resistor', 'capacitor', 'inductor')[:members]):
        name = 'element' + str(number)
        names.append(name)
        # Mix endpoint order to ensure the signal terminal is found by net.
        nodes = {'1': rail, '2': 'signal'} if number % 2 == 0 else {'1': 'signal', '2': rail}
        comps.append(dict(id=name, symbol=kind, nodes=nodes))
        points[name] = {pin: (50+50*number, y if net == 'signal' else y-50) for pin, net in nodes.items()}
    index = {row['id']: i for i, row in enumerate(comps)}
    plan = {'patterns': [dict(id='bank', kind='parallel_bank', members=names, nets=[rail, 'signal'])]}
    return Pins(comps, points, {('host', 'D'): direction}), comps, index, plan


class BankOutletTests(unittest.TestCase):
    def test_real_symbol_geometry_and_source_plan_are_supported(self):
        from tests.test_readability_layout import bank_fixture
        _, pl, comps, _, index, plan = bank_fixture()
        measured = measure_bank_outlet(pl, comps, index, plan)
        self.assertEqual(measured['count'], 1)
        self.assertTrue(measured['measurable'])
        row = measured['records'][0]
        self.assertEqual(row['host_endpoint'], ['a', 'D'])
        self.assertEqual(row['host_point'], list(pl.pin_world(index['a'], 'D')))
        self.assertEqual(row['direction'], pl.pin_dir(index['a'], 'D'))
        self.assertGreater(measured['error'], 0)

    def test_nmos_load_above_its_real_north_drain_is_preferred(self):
        pl, comps, index, plan = fixture(y=220)
        before = measure_bank_outlet(pl, comps, index, plan)
        self.assertEqual(before['count'], 1)
        self.assertEqual(before['planned_count'], 1)
        self.assertTrue(before['measurable'])
        self.assertAlmostEqual(before['error'], 1.2)
        self.assertEqual(before['records'][0]['host_endpoint'], ['host', 'D'])
        self.assertEqual(before['records'][0]['direction_vector'], [0, -1])
        for name in plan['patterns'][0]['members']:
            pin = next(pin for pin, net in comps[index[name]]['nodes'].items() if net == 'signal')
            pl.points[name][pin] = (pl.points[name][pin][0], -20)
        after = measure_bank_outlet(pl, comps, index, plan)
        self.assertEqual(after['error'], 0)
        self.assertEqual(after['records'][0]['violations'], 0)

    def test_pmos_return_load_prefers_the_true_south_outlet(self):
        pl, comps, index, plan = fixture(symbol='pmos', rail='vss', y=-20, direction='south')
        backward = measure_bank_outlet(pl, comps, index, plan)
        self.assertAlmostEqual(backward['error'], 1.2)
        for name in plan['patterns'][0]['members']:
            pin = next(pin for pin, net in comps[index[name]]['nodes'].items() if net == 'signal')
            pl.points[name][pin] = (pl.points[name][pin][0], 220)
        self.assertEqual(measure_bank_outlet(pl, comps, index, plan)['error'], 0)

    def test_extra_cross_coupled_gate_is_not_another_conduction_anchor(self):
        pl, comps, index, plan = fixture()
        comps.append(dict(id='other', symbol='nmos', nodes={'D': 'elsewhere', 'G': 'signal', 'S': '0'}))
        # No geometry is needed for an unrelated gate at the junction.
        result = measure_bank_outlet(pl, comps, index, plan)
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['error'], 1.2)

    def test_common_source_or_second_drain_invalidates_unique_anchor(self):
        for pin in ('S', 'D'):
            pl, comps, index, plan = fixture()
            nodes = {'D': 'other', 'G': 'bias', 'S': '0'}
            nodes[pin] = 'signal'
            comps.append(dict(id='other', symbol='nmos', nodes=nodes))
            result = measure_bank_outlet(pl, comps, index, plan)
            self.assertEqual(result['count'], 0)
            self.assertIsNone(result['error'])
            self.assertEqual(result['skipped'][0]['reason'], 'nonunique_mos_drain_anchor')

    def test_independent_source_or_amplifier_output_blocks_outlet_claim(self):
        for symbol, pin in (('voltage-source', '+'), ('current-source', '+'), ('opamp', 'OUT'), ('vcvs', 'OUT')):
            pl, comps, index, plan = fixture()
            comps.append(dict(id='driver', symbol=symbol, nodes={pin: 'signal'}))
            result = measure_bank_outlet(pl, comps, index, plan)
            self.assertEqual(result['count'], 0)
            self.assertIsNone(result['error'])
            self.assertEqual(result['skipped'][0]['reason'], 'externally_driven_junction')

    def test_pure_capacitive_shunts_and_same_return_side_are_not_loads(self):
        pl, comps, index, plan = fixture(members=2)
        for row in comps[1:]:
            row['symbol'] = 'capacitor'
        result = measure_bank_outlet(pl, comps, index, plan)
        self.assertEqual(result['skipped'][0]['reason'], 'capacitive_shunt_bank')
        self.assertIsNone(result['error'])
        pl, comps, index, plan = fixture(rail='0')
        result = measure_bank_outlet(pl, comps, index, plan)
        self.assertEqual(result['skipped'][0]['reason'], 'not_opposite_rail_load')
        self.assertIsNone(result['error'])

    def test_endpoint_mismatch_and_unverified_rail_are_not_accepted(self):
        pl, comps, index, plan = fixture()
        comps[1]['nodes']['1'] = 'different'
        result = measure_bank_outlet(pl, comps, index, plan)
        self.assertEqual(result['skipped'][0]['reason'], 'bank_endpoint_mismatch')
        pl, comps, index, plan = fixture(rail='unproven_bias')
        result = measure_bank_outlet(pl, comps, index, plan)
        self.assertEqual(result['skipped'][0]['reason'], 'bank_requires_one_verified_rail')

    def test_pin_direction_is_read_from_geometry_instead_of_mos_type(self):
        pl, comps, index, plan = fixture(direction='west')
        for name in plan['patterns'][0]['members']:
            pin = next(pin for pin, net in comps[index[name]]['nodes'].items() if net == 'signal')
            pl.points[name][pin] = (-20, 500)
        result = measure_bank_outlet(pl, comps, index, plan)
        self.assertEqual(result['error'], 0)
        self.assertEqual(result['records'][0]['direction_vector'], [-1, 0])

    def test_names_and_endpoint_order_do_not_change_measurement(self):
        pl, comps, index, plan = fixture()
        expected = measure_bank_outlet(pl, comps, index, plan)['error']
        names = {row['id']: 'part' + str(100-i) for i, row in enumerate(comps)}
        points = {}
        changed = []
        for row in reversed(comps):
            clone = deepcopy(row)
            clone['id'] = names[row['id']]
            old_points = pl.points[row['id']]
            if row['symbol'] in ('resistor', 'capacitor', 'inductor'):
                clone['nodes'] = {'1': row['nodes']['2'], '2': row['nodes']['1']}
                points[clone['id']] = {'1': old_points['2'], '2': old_points['1']}
            else:
                points[clone['id']] = deepcopy(old_points)
            changed.append(clone)
        new_index = {row['id']: i for i, row in enumerate(changed)}
        new_plan = deepcopy(plan)
        new_plan['patterns'][0]['members'] = [names[name] for name in reversed(plan['patterns'][0]['members'])]
        new_plan['patterns'][0]['nets'].reverse()
        placement = Pins(changed, points, {(names['host'], 'D'): 'north'})
        self.assertEqual(measure_bank_outlet(placement, changed, new_index, new_plan)['error'], expected)

    def test_duplicate_pattern_does_not_double_count_and_measurement_is_read_only(self):
        pl, comps, index, plan = fixture()
        plan['patterns'].append(deepcopy(plan['patterns'][0]))
        frozen = deepcopy((vars(pl), comps, index, plan))
        result = measure_bank_outlet(pl, comps, index, plan)
        self.assertEqual(result['count'], 1)
        self.assertEqual((vars(pl), comps, index, plan), frozen)

    def test_partial_geometry_never_looks_like_complete_zero_error(self):
        pl, comps, index, plan = fixture()
        del pl.points['element0']['2']
        result = measure_bank_outlet(pl, comps, index, plan)
        self.assertEqual(result['planned_count'], 1)
        self.assertEqual(result['count'], 0)
        self.assertFalse(result['measurable'])
        self.assertIsNone(result['error'])
        self.assertEqual(result['skipped'][0]['reason'], 'missing_physical_geometry')
        unknown = measure_bank_outlet(pl, comps, index, {'patterns': []})
        self.assertEqual(unknown['planned_count'], 0)
        self.assertIsNone(unknown['error'])


if __name__ == '__main__':
    unittest.main()
