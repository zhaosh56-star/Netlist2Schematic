"""Drawing relationships do not replace functional circuit recognition."""
from copy import deepcopy
import json
from pathlib import Path
import random
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from circuit_structures import analyze_structures
from layout_relations import build_layout_relations, measure_layout_relations, placement_blocks
from netlist_core import parse_netlist, to_document
from tests.test_passive_placement import base_placement, mos, source


def transistor(c, b, e, kind='NPN'):
    return {'cls': 'BJT', 'type': kind, 'c': c, 'b': b, 'e': e}


def strict_pair():
    return {'a': mos('left', 'inp', 'tail'), 'b': mos('right', 'inn', 'tail'),
            'i': source('ISRC', 'tail')}


def semantic(records, inverse=None):
    inverse = inverse or {}
    name = lambda value: inverse.get(value, value)
    return sorted((row['kind'], tuple(sorted(name(n) for n in row['members'])),
                   tuple(sorted(tuple(sorted(name(n) for n in pair))
                                for pair in row['layout'].get('pairs', ()))),
                   tuple(sorted(name(n) for n in row['owned_members']))) for row in records)


class RelationRecognitionTests(unittest.TestCase):
    def test_reuses_functional_roles_without_mutating_them(self):
        devices = strict_pair()
        structures = analyze_structures(devices)
        before = deepcopy((devices, structures))
        relations = build_layout_relations(devices, structures)
        pair = next(row for row in relations if row['kind'] == 'differential_pair')
        self.assertEqual(set(pair['members']), {'a', 'b'})
        self.assertEqual(pair['roles']['exclusive_tails'], ['i'])
        self.assertEqual(pair['evidence']['kind'], 'functional_roles')
        self.assertEqual((devices, structures), before)
        json.dumps(relations)

    def test_strict_bjt_pair_and_tail(self):
        devices = {'a': transistor('ca', 'ba', 'tail'), 'b': transistor('cb', 'bb', 'tail'),
                   'i': source('ISRC', 'tail')}
        pairs = [r for r in build_layout_relations(devices) if r['kind'] == 'bjt_differential_pair']
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]['roles']['exclusive_tails'], ['i'])
        del devices['i']
        self.assertFalse(any(r['kind'] == 'bjt_differential_pair' for r in build_layout_relations(devices)))

    def test_rail_common_emitter_and_identical_collectors_are_not_input_pairs(self):
        for common, collector in [('0', 'different'), ('tail', 'same')]:
            devices = {'a': transistor('ca', 'ba', common),
                       'b': transistor('ca' if collector == 'same' else 'cb', 'bb', common),
                       'i': source('ISRC', common)}
            self.assertFalse(any(r['kind'] == 'bjt_differential_pair' for r in build_layout_relations(devices)))

    def test_pair_load_requires_two_existing_branches(self):
        devices = strict_pair()
        devices.update({'load_a': dict(mos('left', 'bias', 'vdd'), type='PMOS'),
                        'load_b': dict(mos('right', 'bias', 'vdd'), type='PMOS')})
        relations = build_layout_relations(devices)
        loads = [r for r in relations if r['kind'] == 'paired_load']
        self.assertEqual(len(loads), 1)
        self.assertEqual(set(loads[0]['members']), {'load_a', 'load_b'})
        self.assertEqual(set(loads[0]['roles']['anchor_pair']), {'a', 'b'})
        # The functional matcher still describes only common bias here.
        original = analyze_structures(devices)
        self.assertTrue(any(r['kind'] == 'common_bias_pair' for r in original))
        self.assertFalse(any(r['kind'] == 'current_mirror' for r in original))

    def test_common_bias_alone_or_multiple_branch_loads_do_not_pair(self):
        devices = {'x': dict(mos('dx', 'bias', 'vdd'), type='PMOS'),
                   'y': dict(mos('dy', 'bias', 'vdd'), type='PMOS')}
        self.assertFalse(any(r['kind'] == 'paired_load' for r in build_layout_relations(devices)))
        devices = strict_pair()
        devices.update({'x': dict(mos('left', 'bias', 'vdd'), type='PMOS'),
                        'x2': dict(mos('left', 'bias', 'vdd'), type='PMOS'),
                        'y': dict(mos('right', 'bias', 'vdd'), type='PMOS')})
        self.assertFalse(any(r['kind'] == 'paired_load' for r in build_layout_relations(devices)))

    def test_visible_port_parallel_repeat_is_not_mislabeled_function(self):
        devices = {'a': mos('d', 'g', '0'), 'b': mos('d', 'g', '0')}
        relations = build_layout_relations(devices)
        repeat = next(r for r in relations if r['kind'] == 'parallel_replicas')
        self.assertTrue(repeat['rigid'])
        self.assertEqual(repeat['evidence']['ports'], ['d', 'g', '0'])
        devices['c'] = mos('d', 'g', '0')
        repeat = next(r for r in build_layout_relations(devices) if r['kind'] == 'parallel_replicas')
        self.assertFalse(repeat['rigid'])
        self.assertFalse(repeat['layout']['pairs'])
        self.assertFalse(repeat['owned_members'])

    def test_multioutput_current_mirror_has_no_arbitrary_rigid_pairs(self):
        devices = {'ref': mos('g', 'g', '0'), 'a': mos('a', 'g', '0'),
                   'b': mos('b', 'g', '0')}
        mirror = next(r for r in build_layout_relations(devices) if r['kind'] == 'current_mirror')
        self.assertFalse(mirror['rigid'])
        self.assertFalse(mirror['owned_members'])
        self.assertFalse(mirror['layout']['pairs'])
        self.assertEqual(len(mirror['layout']['same_row'][0]), 3)

    def test_bjt_mirror_keeps_reference_and_output_roles(self):
        devices = {'r': transistor('bias', 'bias', '0'),
                   'o': transistor('out', 'bias', '0')}
        mirror = next(r for r in build_layout_relations(devices) if r['kind'] == 'bjt_current_mirror')
        self.assertEqual(mirror['roles']['reference'], 'r')
        self.assertEqual(mirror['roles']['outputs'], ['o'])

    def test_local_owners_are_disjoint_even_inside_explicit_macro(self):
        path = ROOT / 'test_data/11-15/strongarm-comparator.cir'
        if not path.exists():
            self.skipTest('optional local dataset not present')
        devices = parse_netlist(path, cell='dut')
        relations = build_layout_relations(devices)
        owned = [name for relation in relations for name in relation['owned_members']]
        self.assertEqual(len(owned), len(set(owned)))
        macro = next(row for row in relations if row['kind'] == 'latch')
        self.assertTrue(macro['macro'])
        self.assertFalse(macro['owned_members'])
        self.assertTrue(macro['children'])
        self.assertFalse(any(set(row['members']) == set(devices) for row in relations if row['rigid']))

    def test_no_passive_rigid_parent_and_no_bias_union(self):
        devices = strict_pair()
        devices['r'] = {'cls': 'PASSIVE', 'type': 'R', 'a': 'left', 'b': 'rr', 'value': '1k'}
        devices['c'] = {'cls': 'PASSIVE', 'type': 'C', 'a': 'rr', 'b': 'right', 'value': '1p'}
        relations = build_layout_relations(devices)
        self.assertFalse(any(row['rigid'] for row in relations if 'r' in row['members'] or 'c' in row['members']))
        self.assertTrue(any(row['kind'] == 'series_chain' for row in relations))

    def test_rename_reorder_preserves_relation_members_and_owners(self):
        devices = strict_pair()
        devices.update({'p': dict(mos('left', 'bias', 'vdd'), type='PMOS'),
                        'q': dict(mos('right', 'bias', 'vdd'), type='PMOS')})
        old = semantic(build_layout_relations(devices))
        rng = random.Random(12)
        for _ in range(30):
            keys = list(devices)
            rng.shuffle(keys)
            mapping = {name: 'instance' + str(i) for i, name in enumerate(keys)}
            mutated = {mapping[name]: deepcopy(devices[name]) for name in keys}
            for dev in mutated.values():
                for field in ('d', 'g', 's', 'bulk', 'p', 'n'):
                    if field in dev and dev[field] not in ('0', 'vdd'):
                        dev[field] = 'node_' + dev[field]
            self.assertEqual(old, semantic(build_layout_relations(mutated), {v: k for k, v in mapping.items()}))


class RelationMeasurementsTests(unittest.TestCase):
    def test_interleaving_is_independent_from_old_quality(self):
        devices = strict_pair()
        devices['x'] = mos('other', 'control', '0')
        pl, comps, nets, index = base_placement(devices, {'a': (1, 1), 'b': (1, 3), 'i': (2, 1), 'x': (1, 2)})
        records = build_layout_relations(devices)
        measured = measure_layout_relations(pl, comps, index, records)
        self.assertEqual(measured['interleaving'], 1)
        self.assertIn('normalized_span', measured)
        self.assertIn('role_order', measured)
        self.assertNotIn('score', measured)
        pl.set_state([(1, 1), (1, 2), (2, 1), (1, 3)], pl.orient)
        self.assertEqual(measure_layout_relations(pl, comps, index, records)['interleaving'], 0)

    def test_empty_relations_work_with_minimal_mock(self):
        result = measure_layout_relations(object(), [], {}, [])
        self.assertEqual(result['interleaving'], 0)
        self.assertEqual(result['normalized_span'], 0)


if __name__ == '__main__':
    unittest.main()
