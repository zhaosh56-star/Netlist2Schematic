"""Role-aware structures must depend on topology, not schematic labels."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import random
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from circuit_structures import analyze_structures, structure_owners
from netlist_core import SNK_ALIASES, SRC_ALIASES, parse_netlist

FIXTURES = ROOT / 'tests' / 'fixtures' / 'aesthetic-baseline'


def mos(drain, gate, source, polarity='NMOS'):
    return {'cls': 'MOS', 'type': polarity, 'd': drain, 'g': gate,
            's': source, 'bulk': source, 'model': polarity}


def by_kind(records, kind):
    return [record for record in records if record['kind'] == kind]


def members(records):
    return {frozenset(record['devices']) for record in records}


def semantic_signature(records, old_names=None):
    """Ignore arbitrary symmetric left/right labelling and record IDs."""
    old_names = old_names or {}
    name = lambda item: old_names.get(item, item)
    names = lambda items: tuple(sorted(name(item) for item in items))
    signature = []
    for record in records:
        roles, kind = record['roles'], record['kind']
        if kind == 'current_mirror':
            role = (names(roles['references']), names(roles['outputs']))
        elif kind in ('complementary_branch', 'output_pair', 'series_branch'):
            role = (name(roles['upper']), name(roles['lower']))
        elif kind == 'differential_pair':
            role = names(roles['tail'])
        elif kind == 'latch':
            role = tuple(sorted((name(roles[side + '_upper']),
                                 name(roles[side + '_lower']))
                                for side in ('left', 'right')))
        elif kind == 'folded_core':
            role = tuple(sorted((name(roles['input_' + side]),
                                 name(roles['folded_' + side]),
                                 name(roles['bias_' + side]))
                                for side in ('left', 'right')))
        elif kind == 'feedback_network':
            role = (name(roles['amplifier']), roles['input'],
                    tuple(name(item) for item in roles['series']), names(roles['shunts']))
        elif kind == 'series_chain':
            chain = tuple(name(item) for item in roles['ordered'])
            role = min(chain, tuple(reversed(chain)))
        elif kind == 'opamp_core':
            role = (name(roles['amplifier']), names(roles['positive_feedback']),
                    names(roles['negative_feedback']))
        else:
            role = names(record['devices'])
        signature.append((kind, record['confidence'], names(record['devices']), role))
    return sorted(signature)


class CircuitStructureTests(unittest.TestCase):
    def fixture(self, number):
        return parse_netlist(FIXTURES / f'{number}.cir')

    def test_actual_differential_pairs_only(self):
        expected = {2: {'M9', 'M10'}, 3: {'M5', 'M6'}, 4: {'M0', 'M1'}}
        for number, pair in expected.items():
            with self.subTest(circuit=number):
                found = by_kind(analyze_structures(self.fixture(number)), 'differential_pair')
                self.assertEqual({frozenset(pair)}, members(found))
                self.assertTrue(found[0]['roles']['tail'])

    def test_ground_and_supply_pairs_and_source_bank_are_not_differential(self):
        for source, count in (('0', 2), ('vdd', 2), ('unnamed_rail', 3)):
            devices = {f'T{i}': mos(f'd{i}', f'g{i}', source) for i in range(count)}
            devices['tail'] = mos(source, 'bias', '0')
            self.assertFalse(by_kind(analyze_structures(devices), 'differential_pair'))

    def test_two_unfed_sources_are_only_a_connection(self):
        devices = {'a': mos('out_a', 'in_a', 'shared'),
                   'b': mos('out_b', 'in_b', 'shared')}
        self.assertFalse(by_kind(analyze_structures(devices), 'differential_pair'))

    def test_voltage_rail_cannot_supply_a_false_pair(self):
        devices = {'a': mos('out_a', 'in_a', 'power'),
                   'b': mos('out_b', 'in_b', 'power'),
                   'tail': mos('power', 'bias', '0'),
                   'supply': {'cls': 'VSRC', 'type': 'V', 'p': 'power', 'n': '0'}}
        self.assertFalse(by_kind(analyze_structures(devices), 'differential_pair'))

    def test_mirrors_preserve_diode_reference_and_outputs(self):
        records = by_kind(analyze_structures(self.fixture(3)), 'current_mirror')
        pairs = {(r['roles']['reference'], tuple(r['roles']['outputs'])) for r in records}
        self.assertEqual({('M7', ('M3',)), ('M8', ('M11',))}, pairs)

    def test_common_bias_is_not_promoted_to_a_mirror(self):
        records = analyze_structures(self.fixture(4))
        self.assertIn(frozenset({'M2', 'M3'}), members(by_kind(records, 'common_bias_pair')))
        self.assertFalse(by_kind(records, 'current_mirror'))
        self.assertTrue(all(r['confidence'] == 'relation'
                            for r in by_kind(records, 'common_bias_pair')))

    def test_latch_keeps_complete_overlapping_children(self):
        records = analyze_structures(self.fixture(2))
        latch, = by_kind(records, 'latch')
        self.assertEqual({'M1', 'M2', 'M7', 'M8'}, set(latch['devices']))
        self.assertEqual({frozenset({'M1', 'M7'}), frozenset({'M2', 'M8'})},
                         members(by_kind(records, 'complementary_branch')))
        cross, = by_kind(records, 'cross_coupled')
        self.assertEqual({'M1', 'M2'}, set(cross['devices']))
        self.assertEqual(latch['id'], cross['parent'])
        self.assertEqual(3, len(latch['children']))
        self.assertEqual({'M1', 'M2', 'M3', 'M4', 'M6', 'M12'},
                         set(by_kind(records, 'common_source_bank')[0]['devices']))

    def test_folded_roles_and_output_stage_use_connectivity(self):
        records = analyze_structures(self.fixture(4))
        core, = by_kind(records, 'folded_core')
        self.assertEqual({'M0', 'M1', 'M2', 'M3', 'M4', 'M5'}, set(core['devices']))
        role_branches = {(core['roles']['input_' + side], core['roles']['folded_' + side],
                          core['roles']['bias_' + side]) for side in ('left', 'right')}
        self.assertEqual({('M0', 'M4', 'M2'), ('M1', 'M5', 'M3')}, role_branches)
        output, = by_kind(records, 'output_pair')
        self.assertEqual({'upper': 'MP', 'lower': 'MN'}, output['roles'])
        self.assertEqual([['MP', 'MN']], output['layout']['aligned'])
        self.assertEqual('folded_core', core['kind'])

    def test_feedback_keeps_real_opamp_port_and_order(self):
        records = analyze_structures(self.fixture(1))
        feedback = {r['roles']['input']: r for r in by_kind(records, 'feedback_network')}
        self.assertEqual(['C1', 'R1'], feedback['plus']['roles']['series'])
        self.assertEqual({'R2', 'C2'}, set(feedback['plus']['roles']['shunts']))
        self.assertEqual(['R3'], feedback['minus']['roles']['series'])
        self.assertEqual(['R4'], feedback['minus']['roles']['shunts'])
        self.assertEqual('f2', feedback['plus']['nets']['input'])
        self.assertEqual('f3', feedback['minus']['nets']['input'])
        chain, = by_kind(records, 'series_chain')
        self.assertEqual(['C1', 'R1'], chain['roles']['ordered'])
        parallel, = by_kind(records, 'parallel_passives')
        self.assertEqual({'R2', 'C2'}, set(parallel['devices']))

    def test_passive_chain_exists_without_an_amplifier(self):
        devices = {
            'x': {'cls': 'PASSIVE', 'type': 'R', 'a': 'input', 'b': 'middle'},
            'y': {'cls': 'PASSIVE', 'type': 'L', 'a': 'middle', 'b': 'output'},
            'z': {'cls': 'PASSIVE', 'type': 'C', 'a': 'output', 'b': '0'},
        }
        chain, = by_kind(analyze_structures(devices), 'series_chain')
        self.assertEqual({'x', 'y', 'z'}, set(chain['devices']))

    def test_records_are_json_serializable_and_input_is_unchanged(self):
        for number in range(1, 5):
            devices = self.fixture(number)
            original = deepcopy(devices)
            records = analyze_structures(devices)
            json.loads(json.dumps(records, ensure_ascii=False))
            self.assertEqual(original, devices)
            self.assertTrue(all(record['display_name'] for record in records))

    def test_parent_containment_and_unique_lowest_owner(self):
        for number in range(1, 5):
            records = analyze_structures(self.fixture(number))
            lookup = {record['id']: record for record in records}
            owners = structure_owners(records)
            for record in records:
                if record['parent']:
                    parent = lookup[record['parent']]
                    self.assertIn(record['id'], parent['children'])
                    self.assertTrue(set(record['devices']).issubset(parent['devices']))
                for device, owner in record['owner'].items():
                    self.assertEqual(owners[device], owner)
                    if owner:
                        self.assertEqual('high', lookup[owner]['confidence'])
                        self.assertIn(device, lookup[owner]['devices'])
            for record in records:
                if record['kind'] == 'differential_pair':
                    self.assertTrue(all(owners[device] == record['id']
                                        for device in record['devices']))

    def test_renamed_instances_nets_and_shuffled_statements_are_equivalent(self):
        terminal_keys = {'d', 'g', 's', 'bulk', 'c', 'b', 'e', 'substrate',
                         'a', 'p', 'n', 'op', 'on', 'ip', 'in'}
        for number in range(1, 5):
            devices = self.fixture(number)
            expected = semantic_signature(analyze_structures(devices))
            for seed in range(6):
                rng = random.Random(seed)
                original_names = list(devices)
                shuffled_names = [f'instance_{i + 71}' for i in range(len(devices))]
                rng.shuffle(shuffled_names)
                rename = dict(zip(original_names, shuffled_names))
                nets = {value for dev in devices.values() for key, value in dev.items()
                        if key in terminal_keys and value not in SRC_ALIASES | SNK_ALIASES}
                net_names = list(nets)
                rng.shuffle(net_names)
                net_rename = {net: f'node_{i + 29}' for i, net in enumerate(net_names)}
                order = list(devices)
                rng.shuffle(order)
                changed = {}
                for name in order:
                    changed[rename[name]] = {
                        key: net_rename.get(value, value) if key in terminal_keys else value
                        for key, value in devices[name].items()}
                actual = semantic_signature(analyze_structures(changed),
                                            {new: old for old, new in rename.items()})
                self.assertEqual(expected, actual, (number, seed))

    def test_input_insertion_order_does_not_change_records(self):
        for number in range(1, 5):
            devices = self.fixture(number)
            expected = analyze_structures(devices)
            self.assertEqual(expected, analyze_structures(dict(reversed(list(devices.items())))))


if __name__ == '__main__':
    unittest.main()
