"""Opt-in evidence calibration is independent of placement and routing."""
from copy import deepcopy
import json
from pathlib import Path
import random
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from circuit_structures import analyze_structures
from layout_evidence import build_layout_evidence, extend_branch_evidence


def mos(d, g, s, kind='NMOS'):
    return {'cls': 'MOS', 'type': kind, 'd': d, 'g': g, 's': s, 'bulk': '0'}


def bjt(c, b, e, kind='NPN'):
    return {'cls': 'BJT', 'type': kind, 'c': c, 'b': b, 'e': e}


def source(p, n='0', cls='ISRC'):
    return {'cls': cls, 'type': 'I' if cls == 'ISRC' else 'V', 'p': p, 'n': n, 'value': '1'}


def passive(a, b, kind='R'):
    return {'cls': 'PASSIVE', 'type': kind, 'a': a, 'b': b, 'value': '1'}


def bank():
    return {'a': mos('left', 'ip', 'tail'), 'b': mos('right', 'im', 'tail'),
            'ra': mos('control_left', 'left', 'tail'),
            'rb': mos('control_right', 'right', 'tail'),
            'ua': mos('out_left', 'control_left', 'left'),
            'ub': mos('out_right', 'control_right', 'right'),
            'pa': mos('control_left', 'pbias', 'vdd', 'PMOS'),
            'pb': mos('control_right', 'pbias', 'vdd', 'PMOS'),
            't': mos('tail', 'bias', '0')}


def pairs(report):
    return [r for r in report['relations'] if r['kind'] in ('differential_pair', 'bjt_differential_pair')]


def semantic(report, inverse=None):
    inverse = inverse or {}
    name = lambda n: inverse.get(n, n)
    return sorted((r['kind'], tuple(sorted(name(n) for n in r['members'])),
                   tuple(sorted(name(n) for n in r['roles'].get('exclusive_tails', ()))),
                   tuple(sorted(name(n) for n in r['roles'].get('shared_tails', ()))))
                  for r in report['relations'])


class CalibratedEvidenceTests(unittest.TestCase):
    def test_baseline_inputs_are_untouched_and_json_is_serializable(self):
        devices = bank()
        baseline = analyze_structures(devices)
        ports = [{'net': 'ip', 'source': 'formal'}, {'net': 'im', 'source': 'formal'}]
        declared = {'tail': {'role': 'unknown', 'evidence': ['caller']}}
        before = deepcopy((devices, baseline, ports, declared))
        report = build_layout_evidence(devices, baseline, formal_ports=ports, net_roles=declared)
        self.assertEqual((devices, baseline, ports, declared), before)
        json.dumps(report, allow_nan=False)

    def test_internal_two_user_pair_is_preserved_without_formal_ports(self):
        devices = {'a': mos('x', 'g1', 't'), 'b': mos('y', 'g2', 't'), 'i': source('t')}
        old = analyze_structures(devices)
        report = build_layout_evidence(devices, old)
        pair = pairs(report)[0]
        self.assertEqual(pair['roles']['exclusive_tails'], ['i'])
        self.assertEqual(pair['evidence']['validation']['kind'], 'existing_two_user_pair')

    def test_common_source_is_not_power_and_unique_formal_pair_is_recovered(self):
        report = build_layout_evidence(bank(), formal_ports=('ip', 'im'))
        self.assertEqual(report['net_roles']['tail']['role'], 'unknown')
        self.assertEqual(len(pairs(report)), 1)
        pair = pairs(report)[0]
        self.assertEqual(set(pair['members']), {'a', 'b'})
        self.assertEqual(pair['roles']['shared_tails'], ['t'])
        self.assertEqual(pair['roles']['exclusive_tails'], [])
        self.assertTrue(pair['roles']['validated_shared_source'])

    def test_ambiguous_external_bank_never_enumerates_all_pairs(self):
        report = build_layout_evidence(bank(), formal_ports=('ip', 'im', 'left', 'right'))
        # The internal controls are not control-only formal ports, so two input
        # controls remain unique. Make a third real external control instead.
        devices = bank()
        devices['ra']['g'] = 'extra'
        report = build_layout_evidence(devices, formal_ports=('ip', 'im', 'extra'))
        self.assertFalse(pairs(report))
        self.assertTrue(any(x['reason'] == 'ambiguous_formal_controls'
                            for x in report['diagnostics']['shared_source_banks']))

    def test_common_bias_or_no_tail_is_insufficient(self):
        devices = bank()
        devices['b']['g'] = 'ip'
        self.assertFalse(pairs(build_layout_evidence(devices, formal_ports=('ip',))))
        devices = bank()
        del devices['t']
        self.assertFalse(pairs(build_layout_evidence(devices, formal_ports=('ip', 'im'))))

    def test_external_pair_requires_comparable_two_branches(self):
        devices = bank()
        devices['extra'] = mos('left', 'other', '0')
        report = build_layout_evidence(devices, formal_ports=('ip', 'im'))
        self.assertFalse(pairs(report))
        self.assertEqual(report['diagnostics']['shared_source_banks'][0]['reason'],
                         'incomparable_output_branches')

    def test_two_formals_plus_third_voltage_driven_member_is_ambiguous(self):
        devices = bank()
        devices['ra']['g'] = 'extra'
        devices['probe'] = source('extra', cls='VSRC')
        report = build_layout_evidence(devices, formal_ports=('ip', 'im'))
        self.assertFalse(pairs(report))
        self.assertEqual(report['diagnostics']['shared_source_banks'][0]['reason'],
                         'ambiguous_external_controls')

    def test_rejected_input_pair_cannot_return_through_folded_parent(self):
        devices = {'a': mos('left', 'p', 'tail'), 'b': mos('right', 'm', 'tail'),
                   'i': source('tail'), 'pa': mos('left', 'bias', 'vdd', 'PMOS'),
                   'pb': mos('right', 'bias', 'vdd', 'PMOS'),
                   'fa': mos('x', 'cascode', 'left', 'PMOS'),
                   'fb': mos('y', 'cascode', 'right', 'PMOS')}
        baseline = analyze_structures(devices)
        self.assertTrue(any(r['kind'] == 'folded_core' for r in baseline))
        report = build_layout_evidence(devices, baseline, net_roles={'tail': 'power'})
        self.assertTrue(report['diagnostics']['invalid_composites'])
        self.assertFalse(any(set(pair) == {'a', 'b'} for r in report['relations']
                             for pair in r.get('layout', {}).get('pairs', ())))
        self.assertFalse(any(r['kind'] == 'folded_core' and r['confidence'] == 'high'
                             for r in report['structures']))

    def test_voltage_source_control_is_not_promoted_to_power(self):
        devices = bank()
        devices.update(vp=source('ip', cls='VSRC'), vn=source('im', cls='VSRC'))
        report = build_layout_evidence(devices)
        self.assertNotEqual(report['net_roles']['ip']['role'], 'power')
        self.assertEqual(set(pairs(report)[0]['members']), {'a', 'b'})

    def test_internal_floating_gate_drain_offsets_do_not_prove_external_inputs(self):
        devices = bank()
        devices.update(offset_a=source('ip', 'left', cls='VSRC'),
                       offset_b=source('im', 'right', cls='VSRC'))
        report = build_layout_evidence(devices)
        self.assertFalse(pairs(report))
        self.assertFalse(report['external_controls']['voltage_source'])
        explicit = build_layout_evidence(devices, net_roles={'ip': 'signal', 'im': 'signal'})
        self.assertEqual(set(pairs(explicit)[0]['members']), {'a', 'b'})

    def test_arbitrary_grounded_source_at_internal_tail_is_not_a_supply(self):
        devices = {'a': mos('x', 'g1', 'tail'), 'b': mos('y', 'g2', 'tail'),
                   'i': source('tail'), 'probe': source('tail', cls='VSRC')}
        report = build_layout_evidence(devices)
        self.assertEqual(report['net_roles']['tail']['role'], 'unknown')
        self.assertEqual(set(pairs(report)[0]['members']), {'a', 'b'})

    def test_explicit_power_blocks_input_pair_but_signal_does_not(self):
        devices = bank()
        self.assertFalse(pairs(build_layout_evidence(devices, formal_ports=('ip', 'im'),
                                                   net_roles={'tail': 'power'})))
        self.assertEqual(len(pairs(build_layout_evidence(devices, formal_ports=('ip', 'im'),
                                                        net_roles={'tail': 'signal'}))), 1)

    def test_conflicting_supply_evidence_is_reported_conservatively(self):
        report = build_layout_evidence({'a': mos('x', 'g1', 'vdd'),
                                        'b': mos('y', 'g2', 'vdd'), 'i': source('vdd')},
                                       net_roles={'vdd': 'signal'})
        self.assertFalse(pairs(report))
        self.assertEqual(report['net_roles']['vdd']['role'], 'unknown')
        self.assertEqual(report['diagnostics']['supply_conflicts'], ['vdd'])

    def test_five_terminal_opamp_supplies_are_explicit_power(self):
        devices = {'u': {'cls': 'OPAMP', 'type': 'OPAMP', 'op': 'z', 'ip': 'p',
                         'in': 'm', 'vdd': 'positive', 'vss': 'negative'}}
        report = build_layout_evidence(devices, formal_ports=('p', 'm', 'z'))
        self.assertEqual(report['net_roles']['positive']['role'], 'power')
        self.assertEqual(report['net_roles']['negative']['role'], 'power')
        self.assertEqual(report['external_controls']['formal'], ['m', 'p'])

    def test_internal_common_drain_is_neutral_not_a_functional_output(self):
        report = build_layout_evidence(bank(), formal_ports=('ip', 'im'))
        neutral = [r for r in report['relations'] if r['kind'] == 'complementary_common_drain']
        self.assertEqual(len(neutral), 2)
        self.assertTrue(all(not r['rigid'] and r['layout']['branches'] for r in neutral))
        self.assertFalse(any(r['kind'] == 'output_pair' for r in report['structures']))

    def test_true_supplies_and_explicit_output_preserve_output_pair(self):
        devices = {'p': mos('o', 'gp', 'vdd', 'PMOS'), 'n': mos('o', 'gn', '0')}
        report = build_layout_evidence(devices, formal_ports=('o',))
        self.assertTrue(any(r['kind'] == 'output_pair' for r in report['structures']))
        report = build_layout_evidence(devices)
        self.assertTrue(any(r['kind'] == 'complementary_common_drain' for r in report['structures']))

    def test_bjt_pair_is_calibrated_and_identical_collectors_are_not_dp(self):
        devices = {'a': bjt('x', 'p', 't'), 'b': bjt('y', 'm', 't'), 'i': source('t')}
        self.assertEqual(len(pairs(build_layout_evidence(devices))), 1)
        devices['b']['c'] = 'x'
        self.assertFalse(pairs(build_layout_evidence(devices)))

    def test_mirror_member_is_not_upgraded_to_freely_movable_tail(self):
        devices = {'a': mos('x', 'p', 'tail_node'), 'b': mos('y', 'm', 'tail_node'),
                   'tail': mos('tail_node', 'bias', '0'),
                   'ref': mos('bias', 'bias', '0'), 'iref': source('vdd', 'bias')}
        report = build_layout_evidence(devices)
        pair = pairs(report)[0]
        self.assertEqual(pair['roles']['tail'], ['tail'])
        self.assertEqual(pair['roles']['exclusive_tails'], [])
        self.assertEqual(pair['roles']['reserved_tails'], ['tail'])

    def test_existing_explicit_nonexclusive_tail_is_not_upgraded(self):
        devices = {'a': mos('x', 'p', 't'), 'b': mos('y', 'm', 't'), 'i': source('t')}
        baseline = analyze_structures(devices)
        next(r for r in baseline if r['kind'] == 'differential_pair')['roles']['exclusive_tails'] = []
        self.assertEqual(pairs(build_layout_evidence(devices, baseline))[0]['roles']['exclusive_tails'], [])

    def test_current_source_shared_by_two_pairs_is_not_exclusive_to_either(self):
        devices = {'a': mos('x', 'p', 't1'), 'b': mos('y', 'm', 't1'),
                   'c': mos('xx', 'pp', 't2'), 'd': mos('yy', 'mm', 't2'),
                   'i': source('t1', 't2')}
        report = build_layout_evidence(devices)
        self.assertEqual(len(pairs(report)), 2)
        self.assertTrue(all(not r['roles']['exclusive_tails'] for r in pairs(report)))

    def test_same_net_power_profile_does_not_get_overruled_by_bjt_autodetection(self):
        devices = {'a': bjt('x', 'p', 't'), 'b': bjt('y', 'm', 't'), 'i': source('t')}
        devices.update(pa=bjt('x', 'bias', 'vdd', 'PNP'), pb=bjt('y', 'bias', 'vdd', 'PNP'))
        report = build_layout_evidence(devices, net_roles={'t': 'power'})
        self.assertFalse(pairs(report))
        self.assertFalse(any(r['kind'] == 'paired_load' for r in report['relations']))

    def test_unknown_role_is_rejected_instead_of_guessed(self):
        with self.assertRaisesRegex(ValueError, 'Unknown net role'):
            build_layout_evidence(bank(), net_roles={'tail': 'supplyish'})

    def test_rename_and_line_reordering_preserve_semantic_evidence(self):
        original = bank()
        expected = semantic(build_layout_evidence(original, formal_ports=('ip', 'im')))
        rng = random.Random(77)
        for _ in range(12):
            names = list(original)
            rng.shuffle(names)
            mapping = {name: 'part'+str(i) for i, name in enumerate(names)}
            mutated = {mapping[name]: deepcopy(original[name]) for name in names}
            nets = {}
            for dev in mutated.values():
                for field in ('d', 'g', 's', 'bulk', 'p', 'n'):
                    if field in dev and dev[field] not in ('0', 'vdd'):
                        nets.setdefault(dev[field], 'node'+str(len(nets)))
                        dev[field] = nets[dev[field]]
            report = build_layout_evidence(mutated, formal_ports=(nets['ip'], nets['im']))
            self.assertEqual(expected, semantic(report, {v: k for k, v in mapping.items()}))


class AnchoredBranchEvidenceTests(unittest.TestCase):
    def cascode(self, family='MOS'):
        make = mos if family == 'MOS' else bjt
        return {'a': make('x', 'bias', '0'), 'b': make('y', 'bias', '0'),
                'u': make('hi', 'cb', 'x'), 'v': make('ho', 'cb', 'y')}

    def calibrated_cascode(self, family='MOS'):
        devices = self.cascode(family)
        # A diode-connected reference establishes the base mirror pair.
        devices['a']['d' if family == 'MOS' else 'c'] = 'bias'
        devices['u']['s' if family == 'MOS' else 'e'] = 'bias'
        return devices, build_layout_evidence(devices)

    def test_unique_cascode_tiers_form_local_pair_and_explicit_parent(self):
        devices, base = self.calibrated_cascode()
        before = deepcopy((devices, base))
        report = extend_branch_evidence(devices, base)
        self.assertEqual((devices, base), before)
        upper = next(r for r in report['relations'] if r['kind'] == 'corresponding_branch_pair')
        self.assertEqual(set(upper['members']), {'u', 'v'})
        self.assertEqual(upper['roles']['branches'], [['u', 'a'], ['v', 'b']])
        self.assertIsNone(upper['evidence']['functional_claim'])
        parent = next(r for r in report['relations'] if r['kind'] == 'paired_branches')
        self.assertTrue(parent['macro'])
        self.assertEqual(set(parent['members']), {'a', 'b', 'u', 'v'})
        self.assertEqual(upper['parent'], parent['id'])
        self.assertFalse(parent['owned_members'])
        json.dumps(report, allow_nan=False)

    def test_bjt_collector_emitter_correspondence_uses_actual_pins(self):
        devices, base = self.calibrated_cascode('BJT')
        report = extend_branch_evidence(devices, base)
        upper = next(r for r in report['relations'] if r['kind'] == 'corresponding_branch_pair')
        self.assertEqual(upper['roles']['anchor_pins'], ['C', 'C'])
        self.assertEqual(upper['roles']['member_pins'], ['E', 'E'])

    def test_shared_gate_alone_does_not_create_correspondence(self):
        devices = {'u': mos('x', 'b', '0'), 'v': mos('y', 'b', '0')}
        report = extend_branch_evidence(devices, build_layout_evidence(devices))
        self.assertFalse(any(r['kind'] == 'corresponding_branch_pair' for r in report['relations']))

    def test_multiple_continuations_do_not_get_arbitrarily_paired(self):
        devices, _ = self.calibrated_cascode()
        devices['extra'] = mos('extra_out', 'cb', 'y')
        report = extend_branch_evidence(devices, build_layout_evidence(devices))
        self.assertFalse(any(r['kind'] == 'corresponding_branch_pair' for r in report['relations']))
        self.assertTrue(any(r['reason'] == 'nonunique_conduction_continuation'
                            for r in report['branch_evidence']['skipped_branches']))

    def test_branch_family_mismatch_and_extra_source_block_extension(self):
        for mutation in ('polarity', 'source'):
            devices, _ = self.calibrated_cascode()
            if mutation == 'polarity':
                devices['v']['type'] = 'PMOS'
            else:
                devices['injection'] = source('y')
            report = extend_branch_evidence(devices, build_layout_evidence(devices))
            self.assertFalse(any(r['kind'] == 'corresponding_branch_pair' for r in report['relations']))

    def test_soft_passive_anchors_keep_real_control_and_output_pins(self):
        devices = {'a': mos('out', 'g', '0'), 'r': passive('out', 'vdd'),
                   'c': passive('g', '0', 'C')}
        report = extend_branch_evidence(devices, build_layout_evidence(devices))
        attachments = [r for r in report['relations'] if r['kind'] == 'port_attachment']
        self.assertEqual(len(attachments), 2)
        self.assertEqual({tuple(r['roles']['anchor']) for r in attachments}, {('a', 'D'), ('a', 'G')})
        self.assertTrue(all(not r['rigid'] and not r['owned_members'] for r in attachments))

    def test_ambiguous_active_anchor_is_not_guessed(self):
        devices = {'a': mos('out', 'g', '0'), 'b': mos('out', 'gg', '0'),
                   'r': passive('out', 'vdd')}
        report = extend_branch_evidence(devices, build_layout_evidence(devices))
        self.assertFalse(any(r['kind'] == 'port_attachment' for r in report['relations']))

    def bridge(self):
        return {'id': 'bridge', 'upper': 'u', 'lower': 'a', 'members': ['l'],
                'path': ['high', 'low'], 'upper_pin': 'S', 'lower_pin': 'D',
                'member_pins': {'l': {'in': '1', 'out': '2'}},
                'connections': [{'net': 'high', 'pins': [['u', 'S'], ['l', '1']]},
                                {'net': 'low', 'pins': [['l', '2'], ['a', 'D']]}]}

    def test_strict_rl_bridge_is_rechecked_and_remains_soft(self):
        devices = {'u': mos('top', 'b', 'high'), 'a': mos('low', 'input', '0'),
                   'l': passive('high', 'low', 'L')}
        report = extend_branch_evidence(devices, build_layout_evidence(devices),
                                        bridge_alignment=[self.bridge()])
        bridge = next(r for r in report['relations'] if r['kind'] == 'rl_branch_bridge')
        self.assertEqual(bridge['layout']['branches'], [['u', 'l', 'a']])
        self.assertFalse(bridge['rigid'])
        self.assertFalse(bridge['layout'].get('same_row'))

    def test_rl_controls_capacitors_wrong_pins_and_extra_branches_are_rejected(self):
        for mutation in ('C', 'control', 'wrong_pin', 'extra', 'missing_pin_map'):
            devices = {'u': mos('top', 'b', 'high'), 'a': mos('low', 'input', '0'),
                       'l': passive('high', 'low', 'L')}
            bridge = self.bridge()
            if mutation == 'C':devices['l']['type'] = 'C'
            elif mutation == 'control':devices['a']['g'] = 'low'
            elif mutation == 'wrong_pin':bridge['lower_pin'] = 'G'
            elif mutation == 'missing_pin_map':bridge.pop('member_pins')
            else:devices['r'] = passive('low', '0')
            report = extend_branch_evidence(devices, build_layout_evidence(devices), bridge_alignment=[bridge])
            self.assertFalse(any(r['kind'] == 'rl_branch_bridge' for r in report['relations']), mutation)
            self.assertEqual(len(report['branch_evidence']['rejected_bridges']), 1)

    def test_bjt_emitter_merge_is_not_a_differential_pair_or_buffer_claim(self):
        devices = {'a': bjt('vdd', 'ga', 'o'), 'b': bjt('vdd', 'gb', 'o')}
        report = extend_branch_evidence(devices, build_layout_evidence(devices))
        merge = next(r for r in report['relations'] if r['kind'] == 'common_emitter_merge')
        self.assertFalse(merge['rigid'])
        self.assertIsNone(merge['evidence']['functional_claim'])
        self.assertFalse(pairs(report))

    def test_equal_resistor_sensing_is_neutral_and_requires_corresponding_outputs(self):
        devices, _ = self.calibrated_cascode()
        devices.update(r1=passive('hi', 'sense'), r2=passive('ho', 'sense'),
                       control=mos('z', 'sense', '0'))
        report = extend_branch_evidence(devices, build_layout_evidence(devices))
        sense = next(r for r in report['relations'] if r['kind'] == 'paired_sense_paths')
        self.assertEqual(set(sense['roles']['sense_nets']), {'hi', 'ho'})
        self.assertIsNone(sense['evidence']['functional_claim'])
        devices['r2']['value'] = '2'
        report = extend_branch_evidence(devices, build_layout_evidence(devices))
        self.assertFalse(any(r['kind'] == 'paired_sense_paths' for r in report['relations']))

    def test_common_bias_fanout_does_not_become_clock_access(self):
        devices, _ = self.calibrated_cascode()
        report = extend_branch_evidence(devices, build_layout_evidence(devices, formal_ports=('cb',)))
        self.assertFalse(any(r['kind'] == 'clock_access' for r in report['relations']))

    def test_repeated_extension_does_not_duplicate_relations(self):
        devices, base = self.calibrated_cascode()
        once = extend_branch_evidence(devices, base)
        twice = extend_branch_evidence(devices, once)
        self.assertEqual(once['relations'], twice['relations'])

    def test_branch_roles_survive_renaming_and_source_reordering(self):
        devices, base = self.calibrated_cascode()
        expected = semantic(extend_branch_evidence(devices, base))
        rng = random.Random(919)
        for _ in range(10):
            names = list(devices)
            rng.shuffle(names)
            mapping = {n: 'device'+str(i) for i, n in enumerate(names)}
            changed = {mapping[n]: deepcopy(devices[n]) for n in names}
            nets = {}
            for device in changed.values():
                for field in ('d', 'g', 's', 'bulk'):
                    if device.get(field) not in (None, '0', 'vdd'):
                        value = device[field]
                        nets.setdefault(value, 'net'+str(len(nets)))
                        device[field] = nets[value]
            actual = extend_branch_evidence(changed, build_layout_evidence(changed))
            self.assertEqual(expected, semantic(actual, {v: k for k, v in mapping.items()}))


if __name__ == '__main__':
    unittest.main()
