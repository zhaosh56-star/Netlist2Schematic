"""Pure display-row planning: strict R/L continuations and safe correspondence."""
from __future__ import annotations

from copy import deepcopy
import json
import os
import random
import subprocess
import sys
import unittest
from unittest.mock import patch

from branch_levels import plan_branch_rows
from device_leveling import compute_levels
from netlist_core import to_document


def mos(d, s, g='gate', kind='NMOS'):
    return {'cls': 'MOS', 'type': kind, 'd': d, 'g': g, 's': s,
            'bulk': 'vdd' if kind == 'PMOS' else '0'}


def bjt(c, e, b='input', kind='NPN'):
    return {'cls': 'BJT', 'type': kind, 'c': c, 'b': b, 'e': e}


def passive(a, b, kind='R'):
    return {'cls': 'PASSIVE', 'type': kind, 'a': a, 'b': b, 'value': '1k'}


def current(p, n):
    return {'cls': 'ISRC', 'type': 'I', 'p': p, 'n': n, 'value': 'DC 1m'}


def same_row(*names, identifier='corresponding'):
    return {'id': identifier, 'kind': 'current_mirror', 'members': list(names),
            'roles': {}, 'layout': {'same_row': [list(names)]}}


def pair_tail(left, right, tail, identifier='input'):
    return {'id': identifier, 'kind': 'differential_pair',
            'members': [left, right], 'roles': {'left': left, 'right': right,
                'tail': [tail], 'exclusive_tails': [tail]},
            'layout': {'pairs': [[left, right]]}}


def plan(devices, rows, relations=()):
    document = to_document(devices)
    comps = document['components']
    index = {comp['id']: i for i, comp in enumerate(comps)}
    original = [rows[comp['id']] for comp in comps]
    result, report = plan_branch_rows(devices, compute_levels(devices), original,
                                     relations, comps, index)
    return {name: result[i] for name, i in index.items()}, report


class BranchLevelTests(unittest.TestCase):
    def assert_orders(self, report):
        rows = report['rows']
        for order in report['strict_orders']:
            self.assertLess(rows[order['upper']], rows[order['lower']])
        for relation in report['same_row']:
            self.assertEqual(len({rows[name] for name in relation['members']}), 1)
        for bridge in report['bridge_alignment']:
            order = ([bridge['upper']] if bridge['upper'] is not None else [])
            order += bridge['members']
            order += [bridge['lower']] if bridge['lower'] is not None else []
            self.assertTrue(all(rows[a] < rows[b] for a, b in zip(order, order[1:])))
            self.assertEqual({name: rows[name] for name in bridge['members']},
                             bridge['attachment_rows'])
        self.assertEqual(rows, report['display_rows'])
        json.dumps(report, allow_nan=False)

    def test_noop_keeps_existing_direct_conduction_rows(self):
        devices = {'upper': mos('input', 'join'), 'lower': mos('join', '0')}
        before = {'upper': 1, 'lower': 3}
        rows, report = plan(devices, before)
        self.assertEqual(rows, before)
        self.assertEqual(report['status'], 'unchanged')
        self.assertIn({'upper': 'upper', 'lower': 'lower', 'reason': 'conduction'},
                      report['strict_orders'])
        self.assert_orders(report)

    def test_corresponding_unrelated_branches_may_share_a_display_row(self):
        devices = {'reference': mos('bias', '0', 'bias'), 'output': mos('out', '0', 'bias')}
        rows, report = plan(devices, {'reference': 2, 'output': 1},
                            [same_row('reference', 'output')])
        self.assertEqual(rows, {'reference': 1, 'output': 1})
        self.assertEqual(report['status'], 'changed')
        self.assert_orders(report)

    def test_correspondence_cannot_collapse_a_real_series_step(self):
        devices = {'upper': mos('input', 'join'), 'lower': mos('join', '0')}
        before = {'upper': 1, 'lower': 2}
        rows, report = plan(devices, before, [same_row('upper', 'lower')])
        self.assertEqual(rows, before)
        self.assertEqual(report['same_row'], [])
        self.assertEqual(report['skipped_relations'][0]['reason'],
                         'same_row_conflicts_with_conduction')
        self.assert_orders(report)

    def test_collectively_cyclic_equalities_are_all_rejected(self):
        devices = {'A': mos('a', 'ab'), 'B': mos('ab', '0'),
                   'C': mos('c', 'cd'), 'D': mos('cd', '0')}
        before = {'A': 1, 'B': 2, 'C': 1, 'D': 2}
        relations = [same_row('A', 'D', identifier='one'),
                     same_row('C', 'B', identifier='two')]
        for order in (relations, relations[::-1]):
            rows, report = plan(devices, before, order)
            self.assertEqual(rows, before)
            self.assertEqual(len(report['skipped_relations']), 2)
            self.assert_orders(report)

    def test_single_rl_bridge_splits_an_old_shared_active_row(self):
        for kind in ('R', 'L'):
            for reverse in (False, True):
                with self.subTest(kind=kind, reverse=reverse):
                    terminals = ('bottom', 'top') if reverse else ('top', 'bottom')
                    devices = {'upper': mos('out', 'top'), 'lower': mos('bottom', '0'),
                               'bridge': passive(*terminals, kind)}
                    rows, report = plan(devices, dict(upper=1, lower=1, bridge=2))
                    self.assertLess(rows['upper'], rows['lower'])
                    self.assertEqual(len(report['rl_bridges']), 1)
                    self.assertIn({'upper': 'upper', 'lower': 'lower', 'reason': 'rl_continuation'},
                                  report['strict_orders'])
                    self.assert_orders(report)

    def test_rl_chain_internal_node_has_only_its_two_rl_terminals(self):
        devices = {'upper': mos('out', 'a'), 'lower': mos('b', '0'),
                   'R': passive('a', 'internal'), 'L': passive('internal', 'b', 'L')}
        rows, report = plan(devices, dict(upper=1, lower=1, R=2, L=2))
        self.assertLess(rows['upper'], rows['lower'])
        self.assertEqual(report['rl_bridges'][0]['path'], ['a', 'internal', 'b'])
        self.assert_orders(report)

    def test_rl_members_are_really_inserted_between_endpoints_and_other_rows_shift(self):
        devices = {'upper': mos('out', 'a'), 'lower': mos('b', '0'),
                   'R': passive('a', 'internal'), 'L': passive('internal', 'b', 'L'),
                   'other': current('bias', '0'), 'C': passive('elsewhere', '0', 'C')}
        rows, report = plan(devices, dict(upper=1, lower=2, R=0, L=0, other=2, C=3))
        self.assertEqual((rows['upper'], rows['R'], rows['L'], rows['lower']), (1, 2, 3, 4))
        self.assertEqual(report['inserted_rows'], [{'before_row': 2, 'count': 2}])
        self.assertEqual(rows['other'], 4)
        self.assertEqual(rows['C'], 5)
        for name in ('upper', 'lower', 'other', 'C'):
            self.assertEqual(rows[name], report['row_axis_map'][report['rows_before_attachments'][name]])
        self.assertEqual(report['attachment_rows'], {'R': 2, 'L': 3})
        self.assertTrue(all(order['upper'] not in ('R', 'L') and order['lower'] not in ('R', 'L')
                            for order in report['strict_orders']))
        self.assert_orders(report)

    def test_existing_gap_is_reused_without_expanding_other_rows(self):
        devices = {'upper': mos('out', 'a'), 'lower': mos('b', '0'),
                   'R': passive('a', 'internal'), 'L': passive('internal', 'b', 'L'),
                   'other': current('bias', '0')}
        rows, report = plan(devices, dict(upper=1, lower=6, R=0, L=0, other=3))
        self.assertEqual(report['inserted_rows'], [])
        self.assertEqual(rows['other'], 3)
        self.assertLess(rows['upper'], rows['R'])
        self.assertLess(rows['R'], rows['L'])
        self.assertLess(rows['L'], rows['lower'])
        self.assert_orders(report)

    def test_two_serial_bridges_share_one_consistent_expanded_row_axis(self):
        devices = {'upper': mos('out', 'a'), 'middle': mos('b', 'c'),
                   'lower': mos('d', '0'), 'R1': passive('a', 'b'),
                   'R2': passive('c', 'internal'), 'L': passive('internal', 'd', 'L')}
        rows, report = plan(devices, dict(upper=1, middle=2, lower=3, R1=0, R2=0, L=0))
        self.assertEqual([rows[n] for n in ('upper', 'R1', 'middle', 'R2', 'L', 'lower')],
                         [1, 2, 3, 4, 5, 6])
        self.assertEqual(report['inserted_rows'], [{'before_row': 2, 'count': 1},
                                                  {'before_row': 3, 'count': 2}])
        self.assert_orders(report)

    def test_supply_and_sink_bridges_use_neighboring_legal_rows(self):
        devices = {'M': mos('d', '0'), 'R': passive('vdd', 'internal'),
                   'L': passive('internal', 'd', 'L'), 'other': current('bias', '0')}
        rows, report = plan(devices, dict(M=0, R=0, L=0, other=1))
        self.assertEqual((rows['R'], rows['L'], rows['M']), (0, 1, 2))
        self.assertEqual(rows['other'], 3)
        self.assertIsNone(report['bridge_alignment'][0]['upper'])
        self.assert_orders(report)
        devices = {'M': mos('d', 'a'), 'R': passive('a', 'internal'),
                   'L': passive('internal', '0', 'L')}
        rows, report = plan(devices, dict(M=1, R=0, L=0))
        self.assertEqual((rows['M'], rows['R'], rows['L']), (1, 2, 3))
        self.assertIsNone(report['bridge_alignment'][0]['lower'])
        self.assert_orders(report)

    def test_bridge_connections_keep_actual_reversed_member_pin_names(self):
        devices = {'upper': mos('out', 'a'), 'lower': mos('b', '0'),
                   'R': passive('b', 'a')}
        _, report = plan(devices, dict(upper=1, lower=2, R=0))
        bridge = report['bridge_alignment'][0]
        self.assertEqual(bridge['member_pins'], {'R': {'in': '2', 'out': '1'}})
        self.assertEqual(bridge['upper_pin'], 'S')
        self.assertEqual(bridge['lower_pin'], 'D')
        self.assertEqual(bridge['connections'], [
            {'kind': 'rl_bridge_axis', 'net': 'a', 'pins': [['upper', 'S'], ['R', '2']]},
            {'kind': 'rl_bridge_axis', 'net': 'b', 'pins': [['R', '1'], ['lower', 'D']]}])
        doc = {co['id']: co for co in to_document(devices)['components']}
        for connection in bridge['connections']:
            self.assertTrue(all(doc[name]['nodes'][pin] == connection['net']
                                for name, pin in connection['pins']))

    def test_shared_active_endpoint_has_no_unique_passive_attachment(self):
        devices = {'a': mos('da', 'common'), 'b': mos('db', 'common'),
                   'tail': mos('end', '0'), 'R': passive('common', 'end')}
        rows, report = plan(devices, dict(a=1, b=1, tail=2, R=0))
        self.assertTrue(report['rl_bridges'])
        self.assertFalse(report['attachment_rows'])
        self.assertEqual(report['skipped_attachments'][0]['reason'], 'ambiguous_attachment_endpoint')
        self.assertEqual(rows['R'], 0)
        self.assert_orders(report)

    def test_conflicting_corresponding_load_attachments_are_both_skipped(self):
        devices = {'a': mos('da', '0'), 'b': mos('db', '0'),
                   'R1': passive('vdd', 'internal'), 'L': passive('internal', 'da', 'L'),
                   'R2': passive('vdd', 'db')}
        rows, report = plan(devices, dict(a=1, b=1, R1=0, L=0, R2=0),
                            [same_row('R1', 'R2')])
        self.assertEqual(rows['R1'], rows['R2'])
        self.assertFalse(report['attachment_rows'])
        self.assertFalse(report['inserted_rows'])
        self.assertEqual(len(report['skipped_attachments']), 2)
        self.assertEqual({r['reason'] for r in report['skipped_attachments']},
                         {'attachment_conflicts_with_same_row'})
        self.assert_orders(report)

    def test_tail_preference_and_every_other_row_use_the_same_attachment_axis(self):
        devices = {'upper': mos('out', 'a'), 'lower': mos('b', '0'), 'R': passive('a', 'b'),
                   'left': mos('da', 'common', 'ip'), 'right': mos('db', 'common', 'im'),
                   'I': current('common', '0')}
        rows, report = plan(devices, dict(upper=1, lower=2, R=0, left=3, right=3, I=9),
                            [pair_tail('left', 'right', 'I')])
        self.assertEqual(rows['left'], 4)
        self.assertEqual(rows['I'], 5)
        self.assertEqual(report['rows_before']['I'], 9)
        self.assertEqual(report['tail_preferred_rows'], {'I': 5})
        self.assertEqual(report['solver_preferred_rows']['I'], 5)
        self.assert_orders(report)

    def test_c_and_d_are_never_conduction_bridges(self):
        for kind in ('C', 'D'):
            with self.subTest(kind=kind):
                devices = {'upper': mos('out', 'a'), 'lower': mos('b', '0'),
                           'bridge': passive('a', 'b', kind)}
                before = dict(upper=1, lower=1, bridge=2)
                rows, report = plan(devices, before)
                self.assertEqual(rows, before)
                self.assertFalse(report['rl_bridges'])

    def test_control_terminal_on_rl_internal_node_breaks_the_bridge(self):
        devices = {'upper': mos('out', 'a'), 'lower': mos('b', '0'),
                   'tap': mos('extra', '0', 'internal'),
                   'R': passive('a', 'internal'), 'L': passive('internal', 'b', 'L')}
        before = dict(upper=1, lower=1, tap=1, R=2, L=2)
        rows, report = plan(devices, before)
        self.assertEqual(rows, before)
        self.assertFalse(report['rl_bridges'])
        self.assertTrue(any(x['reason'] == 'endpoint_has_nonconduction_terminal'
                            for x in report['skipped_bridges']))

    def test_extra_visible_capacitor_at_endpoint_rejects_bridge(self):
        devices = {'upper': mos('out', 'a'), 'lower': mos('b', '0'),
                   'R': passive('a', 'b'), 'C': passive('b', '0', 'C')}
        rows, report = plan(devices, dict(upper=1, lower=1, R=2, C=2))
        self.assertEqual(rows['upper'], rows['lower'])
        self.assertFalse(report['rl_bridges'])

    def test_nonrail_source_terminal_rejects_bridge(self):
        for source in ('ISRC', 'VSRC'):
            with self.subTest(source=source):
                dev = current('b', '0')
                dev['cls'] = source
                devices = {'upper': mos('out', 'a'), 'lower': mos('b', '0'),
                           'R': passive('a', 'b'), 'source': dev}
                _, report = plan(devices, dict(upper=1, lower=1, R=2, source=2))
                self.assertFalse(report['rl_bridges'])

    def test_shared_rail_is_an_endpoint_for_each_independent_load(self):
        devices = {'a': mos('da', '0'), 'b': mos('db', '0'), 'c': mos('dc', '0'),
                   'Ra': passive('vdd', 'da'), 'Rb': passive('vdd', 'db'),
                   'Rc': passive('vdd', 'dc')}
        rows, report = plan(devices, dict(a=1, b=1, c=1, Ra=0, Rb=0, Rc=0))
        self.assertEqual(len(report['rl_bridges']), 3)
        self.assertEqual(rows['a'], rows['b'])
        self.assert_orders(report)

    def test_parallel_rl_and_nonrail_forks_are_not_strict_bridges(self):
        devices = {'upper': mos('out', 'a'), 'lower': mos('b', '0'),
                   'R': passive('a', 'b'), 'L': passive('a', 'b', 'L')}
        _, report = plan(devices, dict(upper=1, lower=1, R=2, L=2))
        self.assertFalse(report['rl_bridges'])
        devices['third'] = mos('c', '0')
        devices['R2'] = passive('a', 'c')
        _, report = plan(devices, dict(upper=1, lower=1, third=1, R=2, L=2, R2=2))
        self.assertFalse(report['rl_bridges'])

    def test_output_to_output_and_control_endpoints_are_ambiguous(self):
        devices = {'a': mos('da', 'a'), 'b': mos('db', 'b'), 'R': passive('a', 'b')}
        _, report = plan(devices, dict(a=1, b=1, R=2))
        self.assertFalse(report['rl_bridges'])
        self.assertEqual(report['skipped_bridges'][0]['reason'], 'ambiguous_bridge_direction')
        devices['b'] = mos('db', '0', 'b')
        _, report = plan(devices, dict(a=1, b=1, R=2))
        self.assertFalse(report['rl_bridges'])

    def test_rl_cycles_are_reported_without_inventing_a_direction(self):
        devices = {'M': mos('out', '0'), 'R': passive('x', 'y'), 'L': passive('y', 'x', 'L')}
        before = dict(M=1, R=0, L=0)
        rows, report = plan(devices, before)
        self.assertEqual(rows, before)
        self.assertEqual(report['skipped_bridges'][0]['reason'], 'rl_cycle')

    def test_two_rl_bridges_that_close_an_active_cycle_are_both_rejected(self):
        devices = {'a': mos('ia', 'oa'), 'b': mos('ib', 'ob'),
                   'R1': passive('oa', 'ib'), 'R2': passive('ob', 'ia')}
        rows, report = plan(devices, dict(a=1, b=1, R1=2, R2=2))
        self.assertEqual(rows['a'], rows['b'])
        self.assertFalse(report['rl_bridges'])
        self.assertEqual({x['reason'] for x in report['skipped_bridges']},
                         {'bridge_would_create_conduction_cycle'})

    def test_original_scc_devices_never_receive_strict_internal_order(self):
        devices = {'enter': mos('vdd', 'a'), 'one': mos('a', 'b'),
                   'two': mos('b', 'a'), 'leave': mos('b', '0')}
        before = dict(enter=1, one=2, two=2, leave=2)
        rows, report = plan(devices, before, [same_row('one', 'two')])
        self.assertEqual(rows, before)
        self.assertTrue(any(set(s['devices']) == {'one', 'two'} for s in report['original_sccs']))
        self.assertFalse(any({o['upper'], o['lower']} == {'one', 'two'}
                             for o in report['strict_orders']))
        self.assert_orders(report)

    def test_bridge_within_an_original_scc_does_not_add_strict_order(self):
        devices = {'one': mos('a', 'b'), 'two': mos('b', 'a'), 'R': passive('a', 'b')}
        rows, report = plan(devices, dict(one=1, two=1, R=2))
        self.assertEqual(rows['one'], rows['two'])
        self.assertFalse(report['rl_bridges'])
        # Mixed incoming/outgoing endpoint roles may reject this even before SCC handling.
        self.assertTrue(report['skipped_bridges'])

    def test_mos_polarity_and_bjt_pin_roles_preserve_downstream_order(self):
        for kind in ('NMOS', 'PMOS', 'NPN', 'PNP'):
            with self.subTest(kind=kind):
                if kind == 'NMOS':
                    upper, lower = mos('in', 'a'), mos('b', '0')
                elif kind == 'PMOS':
                    upper, lower = mos('a', 'vdd', kind=kind), mos('out', 'b', kind=kind)
                elif kind == 'NPN':
                    upper, lower = bjt('in', 'a'), bjt('b', '0')
                else:
                    upper, lower = bjt('a', 'vdd', kind=kind), bjt('out', 'b', kind=kind)
                devices = {'upper': upper, 'lower': lower, 'R': passive('b', 'a')}
                rows, report = plan(devices, dict(upper=1, lower=1, R=0))
                self.assertLess(rows['upper'], rows['lower'])
                self.assert_orders(report)

    def test_exclusive_current_tail_is_below_its_local_input_pair(self):
        devices = {'left': mos('a', 'common', 'ip'), 'right': mos('b', 'common', 'im'),
                   'tail': current('common', '0'), 'unrelated': current('other', '0')}
        rows, report = plan(devices, dict(left=1, right=1, tail=0, unrelated=0),
                            [pair_tail('left', 'right', 'tail')])
        self.assertEqual(rows['left'], rows['right'])
        self.assertGreater(rows['tail'], rows['left'])
        self.assertEqual(rows['unrelated'], 0)
        self.assertEqual(len(report['tail_orders']), 2)
        self.assert_orders(report)

    def test_bjt_exclusive_tail_uses_emitter_and_not_base(self):
        devices = {'left': bjt('a', 'common', 'ip'), 'right': bjt('b', 'common', 'im'),
                   'tail': current('common', '0')}
        relation = pair_tail('left', 'right', 'tail')
        relation['kind'] = 'bjt_differential_pair'
        rows, report = plan(devices, dict(left=1, right=1, tail=0), [relation])
        self.assertEqual(rows['tail'], 2)
        self.assert_orders(report)

    def test_far_below_tail_moves_up_to_its_own_input_pair(self):
        devices = {'left': mos('a', 'common', 'ip'), 'right': mos('b', 'common', 'im'),
                   'tail': current('common', '0'), 'unrelated': current('other', '0')}
        before = dict(left=3, right=3, tail=7, unrelated=7)
        rows, report = plan(devices, before, [pair_tail('left', 'right', 'tail')])
        self.assertEqual(rows['tail'], 4)
        self.assertEqual(rows['unrelated'], 7)
        self.assertEqual(report['rows_before'], before)
        self.assertEqual(report['tail_preferred_rows'], {'tail': 4})
        self.assert_orders(report)

    def test_active_tail_in_another_corresponding_bank_is_not_locally_pulled(self):
        devices = {'left': mos('a', 'common', 'ip'), 'right': mos('b', 'common', 'im'),
                   'tail': mos('common', '0', 'bias'), 'reference': mos('bias', '0', 'bias')}
        before = dict(left=1, right=1, tail=7, reference=7)
        rows, report = plan(devices, before,
            [pair_tail('left', 'right', 'tail'), same_row('reference', 'tail')])
        self.assertEqual(rows['tail'], 7)
        self.assertEqual(rows['reference'], 7)
        self.assertFalse(report['tail_preferred_rows'])
        self.assertEqual(report['skipped_tails'][0]['reason'], 'tail_belongs_to_corresponding_group')
        self.assert_orders(report)

    def test_real_conduction_predecessor_can_push_tail_below_its_local_preference(self):
        devices = {'left': mos('a', 'common', 'ip'), 'right': mos('b', 'common', 'im'),
                   'tail': mos('common', '0', 'bias'),
                   'supply': mos('common', 'vdd', 'supply_bias', 'PMOS')}
        rows, report = plan(devices, dict(left=3, right=3, tail=9, supply=7),
                            [pair_tail('left', 'right', 'tail')])
        self.assertEqual(report['tail_preferred_rows'], {'tail': 4})
        self.assertEqual(rows['tail'], 8)
        self.assertEqual(rows['supply'], 7)
        self.assert_orders(report)

    def test_shared_tail_is_not_claimed_by_either_local_pair(self):
        devices = {name: mos('d' + name, 'common', 'g' + name) for name in 'abcd'}
        devices['tail'] = current('common', '0')
        relations = [pair_tail('a', 'b', 'tail', 'one'), pair_tail('c', 'd', 'tail', 'two')]
        rows, report = plan(devices, dict(a=1, b=1, c=1, d=1, tail=0), relations)
        self.assertEqual(rows['tail'], 0)
        self.assertFalse(report['tail_orders'])
        self.assertEqual({r['reason'] for r in report['skipped_tails']}, {'shared_tail'})

    def test_all_tail_mentions_reserve_ownership_even_without_exclusive_flag(self):
        devices = {'a': mos('da', 'common_a'), 'b': mos('db', 'common_a'),
                   'c': mos('dc', 'common_b'), 'd': mos('dd', 'common_b'),
                   'tail': current('common_a', 'common_b')}
        other = pair_tail('c', 'd', 'tail', 'two')
        other['roles']['exclusive_tails'] = []
        rows, report = plan(devices, dict(a=1, b=1, c=1, d=1, tail=0),
                            [pair_tail('a', 'b', 'tail', 'one'), other])
        self.assertEqual(rows['tail'], 0)
        self.assertEqual(report['skipped_tails'][0]['reason'], 'shared_tail')

    def test_raw_shared_source_node_cannot_be_claimed_by_a_partial_pair(self):
        devices = {name: mos('d' + name, 'common', 'g' + name) for name in 'abcd'}
        devices['tail'] = current('common', '0')
        rows, report = plan(devices, dict(a=1, b=1, c=1, d=1, tail=0),
                            [pair_tail('a', 'b', 'tail')])
        self.assertEqual(rows['tail'], 0)
        self.assertEqual(report['skipped_tails'][0]['reason'], 'shared_source_node')

    def test_tail_hint_never_orders_two_original_scc_members(self):
        devices = {'left': mos('a', 'common', 'ip'), 'right': mos('b', 'common', 'im'),
                   'tail': mos('common', 'a', 'bias')}
        _, report = plan(devices, dict(left=1, right=1, tail=1),
                         [pair_tail('left', 'right', 'tail')])
        self.assertFalse(report['tail_orders'])
        self.assertTrue(any(r['reason'] == 'tail_within_original_scc'
                            for r in report['skipped_tails']))
        self.assertFalse(any(o['upper'] == 'left' and o['lower'] == 'tail'
                             for o in report['strict_orders']))

    def test_upstream_pmos_supply_is_not_forced_under_the_pair(self):
        devices = {'left': mos('a', 'common', 'ip', 'PMOS'),
                   'right': mos('b', 'common', 'im', 'PMOS'),
                   'tail': current('vdd', 'common')}
        rows, report = plan(devices, dict(left=1, right=1, tail=0),
                            [pair_tail('left', 'right', 'tail')])
        self.assertEqual(rows['tail'], 0)
        self.assertFalse(report['tail_orders'])
        self.assertEqual(report['skipped_tails'][0]['reason'], 'tail_is_not_downstream_of_pair')

    def test_wrong_tail_or_same_row_source_pair_is_skipped(self):
        devices = {'left': mos('a', 'common'), 'right': mos('b', 'common'),
                   'tail': current('unconnected', '0')}
        rows, report = plan(devices, dict(left=1, right=1, tail=0),
            [pair_tail('left', 'right', 'tail'), same_row('left', 'tail')])
        self.assertEqual(rows['tail'], 0)
        self.assertTrue(report['skipped_tails'])
        self.assertTrue(report['skipped_relations'])

    def test_paired_loads_can_share_row_without_moving_other_sources(self):
        devices = {'M': mos('out', '0'), 'R1': passive('vdd', 'out'),
                   'R2': passive('vdd', 'other'), 'I': current('bias', '0')}
        rows, report = plan(devices, dict(M=2, R1=0, R2=1, I=3), [same_row('R1', 'R2')])
        self.assertEqual(rows['R1'], rows['R2'])
        self.assertEqual(rows['I'], 3)
        self.assert_orders(report)

    def test_pure_passives_keep_original_rows(self):
        devices = {'R': passive('a', 'b'), 'L': passive('b', '0', 'L')}
        before = dict(R=1, L=5)
        rows, report = plan(devices, before, [same_row('R', 'L')])
        self.assertEqual(rows, before)
        self.assertEqual(report['reason'], 'no_active_devices')

    def test_inputs_are_immutable_and_no_router_is_called(self):
        devices = {'upper': mos('out', 'a'), 'lower': mos('b', '0'), 'R': passive('a', 'b')}
        document = to_document(devices)
        comps = document['components']
        index = {c['id']: i for i, c in enumerate(comps)}
        levels = compute_levels(devices)
        rows = [1, 1, 1]
        relations = [same_row('upper', 'lower')]
        original = deepcopy((devices, levels, rows, relations, comps, index))
        with patch('sa_routing.final_route', side_effect=AssertionError('unexpected router')):
            _, report = plan_branch_rows(devices, levels, rows, relations, comps, index)
        self.assertEqual((devices, levels, rows, relations, comps, index), original)
        self.assertEqual(report['original_device_levels'], levels['device_levels'])

    def test_invalid_positions_and_mismatched_component_indices_fail_explicitly(self):
        devices = {'M': mos('out', '0')}
        comps = to_document(devices)['components']
        levels = compute_levels(devices)
        for rows, index in (([-1], {'M': 0}), ([True], {'M': 0}), ([1], {'M': 1})):
            with self.subTest(rows=rows, index=index):
                with self.assertRaises(ValueError):
                    plan_branch_rows(devices, levels, rows, [], comps, index)

    def test_renaming_net_names_and_source_order_do_not_change_row_plan(self):
        devices = {'upper': mos('out', 'a'), 'lower': mos('b', 'tail', 'input'),
                   'other': mos('other_out', 'tail', 'input2'),
                   'R': passive('a', 'internal'), 'L': passive('internal', 'b', 'L'),
                   'I': current('tail', '0')}
        before = dict(upper=1, lower=1, other=2, R=0, L=0, I=0)
        relations = [pair_tail('lower', 'other', 'I')]
        expected, report = plan(devices, before, relations)
        self.assert_orders(report)
        rng = random.Random(20261109)
        nets = sorted({n for device in devices.values() for key, n in device.items()
                       if key in ('d', 'g', 's', 'c', 'b', 'e', 'a', 'p', 'n')
                       and n not in ('0', 'vdd')})
        for iteration in range(80):
            names = list(devices)
            shuffled = names[:]
            rng.shuffle(shuffled)
            mapping = dict(zip(names, ['renamed_' + str(i) for i in range(len(names))]))
            net_map = {net: 'signal_' + str(i) for i, net in enumerate(rng.sample(nets, len(nets)))}
            changed = {}
            for name in shuffled:
                changed[mapping[name]] = {key: net_map.get(value, value)
                    if key in ('d', 'g', 's', 'c', 'b', 'e', 'a', 'p', 'n') else value
                    for key, value in devices[name].items()}
            rows, result = plan(changed, {mapping[n]: r for n, r in before.items()},
                               [pair_tail(mapping['lower'], mapping['other'], mapping['I'])])
            self.assertEqual({name: rows[mapping[name]] for name in names}, expected, iteration)
            self.assert_orders(result)

    def test_report_is_reproducible_across_hash_seeds(self):
        program = '''import sys,json
sys.path.insert(0,'tests')
from test_branch_levels import plan,mos,passive,current,pair_tail
devices={'upper':mos('out','a'),'lower':mos('b','tail','i1'),
 'other':mos('other_out','tail','i2'),'R':passive('a','mid'),
 'L':passive('mid','b','L'),'I':current('tail','0')}
rows,report=plan(devices,dict(upper=1,lower=1,other=2,R=0,L=0,I=0),
 [pair_tail('lower','other','I')])
print(json.dumps([rows,report],sort_keys=True))'''
        outputs = []
        for seed in ('1', '2', '19', 'random'):
            result = subprocess.run([sys.executable, '-B', '-c', program],
                env={**os.environ, 'PYTHONHASHSEED': seed}, capture_output=True,
                text=True, check=True)
            outputs.append(result.stdout)
        self.assertTrue(all(output == outputs[0] for output in outputs))


if __name__ == '__main__':
    unittest.main()
