"""Real-terminal source guests, conservative paths and joint proposals."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from netlist2schematic.plugins.attachment.attachment_sources import (build_source_groups, measure_source_groups,
                                source_chain_errors, source_group_anchor, source_group_states,
                                source_group_trials)
from netlist2schematic.netlist.netlist_core import parse_netlist, to_document
from tests.test_passive_placement import convert, mos, resistor, source


def build(devices, comps=None):
    doc = to_document(devices)
    comps = comps or doc['components']
    nets = doc['nets']
    index = {c['id']: i for i, c in enumerate(comps)}
    return build_source_groups(devices, comps, nets, index), comps, nets, index


def tail_devices(reverse=False, pmos=False):
    devices = dict(a=mos('oa', 'ga', 'tail'), b=mos('ob', 'gb', 'tail'),
        bias=source('ISRC', '0' if reverse else 'tail', 'tail' if reverse else '0'),
        load=resistor('oa', '0', 'C'))
    if pmos:
        for name in ('a', 'b'):
            devices[name].update(type='PMOS', bulk='vdd', model='PMOS')
    return devices


def fixture(devices, cells):
    pl, comps, nets, index, _ = convert(devices, cells)
    model = build_source_groups(devices, comps, nets, index)
    # The integration owner installs freedom once, before bounded proposals.
    pl.source_orientation_ids = {index[name] for name in model['freedom']['source_devices']}
    if hasattr(pl, 'fixed_rows'):
        for i in pl.source_orientation_ids:
            pl.fixed_rows.pop(i, None)
    return pl, comps, nets, index, model


def freeze(pl):
    result = deepcopy({k: v for k, v in vars(pl).items() if k != 'rng'})
    result['rng_state'] = pl.rng.getstate()
    return result


class SourceGroupModelTests(unittest.TestCase):
    def test_exclusive_tail_and_real_source_poles(self):
        for reverse in (False, True):
            model, _, _, _ = build(tail_devices(reverse))
            group, = model['groups']
            self.assertEqual(group['kind'], 'direct_tail')
            self.assertEqual(group['host_ports'], [['a', 'S'], ['b', 'S']])
            self.assertEqual(group['signal_pin'], '-' if reverse else '+')
            self.assertEqual(group['rail_pin'], '+' if reverse else '-')
            self.assertEqual(group['members'], ['bias'])
            self.assertEqual(group['path'], ['0', 'tail'])
            self.assertTrue(group['preserve_polarity'])
            json.dumps(model, allow_nan=False)

    def test_bipolar_and_polarity_dual_tail_use_same_evidence(self):
        model, _, _, _ = build(tail_devices(pmos=True))
        self.assertEqual(model['groups'][0]['kind'], 'direct_tail')
        devices = {name: dict(cls='BJT', type='NPN', c='c'+name, b='b'+name,
                             e='tail', model='NPN') for name in ('a', 'b')}
        devices['i'] = source('ISRC', 'tail')
        model, _, _, _ = build(devices)
        self.assertEqual(model['groups'][0]['kind'], 'direct_tail')
        self.assertEqual(model['groups'][0]['host_ports'], [['a', 'E'], ['b', 'E']])

    def test_nonexclusive_or_voltage_driven_pair_is_neutral_not_a_tail(self):
        for modification in ('third', 'tap', 'same_control', 'voltage'):
            devices = tail_devices()
            if modification == 'third': devices['third'] = mos('oc', 'gc', 'tail')
            elif modification == 'tap': devices['tap'] = resistor('tail', '0')
            elif modification == 'same_control': devices['b']['g'] = 'ga'
            else: devices['bias'] = source('VSRC', 'tail')
            model, _, _, _ = build(devices)
            group, = model['groups']
            self.assertEqual(group['kind'], 'neutral_junction_source')
            self.assertFalse(group['functional_role_claimed'])

    def test_unique_path_to_multi_host_control_junction_is_ordered(self):
        devices = dict(v=source('VSRC', 'drive'), r=resistor('drive', 'middle'),
            c=resistor('middle', 'control', 'C'), a=mos('oa', 'control'), b=mos('ob', 'control'))
        model, _, _, _ = build(devices)
        group, = model['groups']
        self.assertEqual(group['kind'], 'source_control_chain')
        self.assertEqual(group['members'], ['v', 'r', 'c'])
        self.assertEqual(group['path'], ['0', 'drive', 'middle', 'control'])
        self.assertEqual(group['host_ports'], [['a', 'G'], ['b', 'G']])
        self.assertEqual(group['owner_junctions'], ['control'])

    def test_direct_control_and_diode_reference_do_not_require_passive_patterns(self):
        devices = dict(a=mos('out', 'control'), v=source('VSRC', 'control'),
                       reference=mos('bias', 'bias'), i=source('ISRC', 'vdd', 'bias'))
        model, _, _, _ = build(devices)
        groups = {g['device']: g for g in model['groups']}
        self.assertEqual(groups['v']['kind'], 'source_control_chain')
        self.assertEqual(groups['v']['passive_members'], [])
        self.assertEqual(groups['i']['kind'], 'neutral_junction_source')
        self.assertEqual(groups['i']['host_ports'], [['reference', 'D'], ['reference', 'G']])

    def test_branching_paths_and_internal_source_taps_are_ambiguous(self):
        for variant in ('fork', 'source_tap', 'cycle'):
            devices = dict(v=source('VSRC', 'start'), r=resistor('start', 'middle'),
                           next=resistor('middle', 'control'), host=mos('out', 'control'))
            if variant == 'fork': devices['other'] = resistor('start', 'control')
            elif variant == 'source_tap': devices['other'] = source('ISRC', 'middle')
            else: devices['next']['b'] = 'start'
            model, _, _, _ = build(devices)
            self.assertNotIn('v', {g['device'] for g in model['groups']})
            self.assertTrue(any(row['device'] == 'v' for row in model['ambiguities']))

    def test_path_to_noncontrol_or_power_node_does_not_invent_a_host(self):
        for endpoint in ('out', 'vdd'):
            devices = dict(v=source('VSRC', 'start'), r=resistor('start', endpoint), host=mos('out', 'gate'))
            model, _, _, _ = build(devices)
            self.assertFalse(model['groups'])
            self.assertEqual(model['coverage']['unassigned_sources'], ['v'])

    def test_source_only_floating_supply_and_unanchored_are_unknown(self):
        devices = dict(floating=source('VSRC', 'x', 'y'), supply=source('VSRC', 'vdd'),
                       isolated=source('ISRC', 'z'))
        model, comps, nets, index = build(devices)
        self.assertFalse(model['supported'])
        self.assertEqual(model['coverage']['recognized_sources'], 0)
        self.assertEqual(len(model['rejected']), 3)
        self.assertIsNone(measure_source_groups(None, comps, nets, index, model)['source_distance'])

    def test_shared_junction_never_merges_other_nodes_or_rail_loads(self):
        devices = dict(a=mos('joint', 'ga'), b=mos('joint', 'gb'),
            i=source('ISRC', 'joint'), other=mos('other', 'gc'), load=resistor('other', '0'))
        model, _, _, _ = build(devices)
        group, = model['groups']
        self.assertEqual(group['kind'], 'neutral_junction_source')
        self.assertEqual(group['owner_junctions'], ['joint'])
        self.assertEqual(group['members'], ['i'])
        self.assertEqual(group['host_members'], ['a', 'b'])
        self.assertNotIn('other', group['host_members'])
        self.assertNotIn('load', group['members'])

    def test_actual_data14_chain_and_data15_shared_source(self):
        for filename, cell, source_name, kind, members in (
            ('lna-with-degeneration.cir', 'main', 'V1', 'source_control_chain', ['V1', 'R2']),
            ('self-adaptive-current-mirror-buffer-fvf-ldo.cir',
             'self_adaptive_current_mirror_buffer_fvf_ldo', 'I1', 'neutral_junction_source', ['I1'])):
            devices = parse_netlist(ROOT/'test_data/11-15'/filename, cell=cell)
            original = deepcopy(devices)
            model, _, _, _ = build(devices)
            group, = model['groups']
            self.assertEqual((group['device'], group['kind'], group['members']), (source_name, kind, members))
            if source_name == 'V1':
                self.assertEqual(group['host_ports'], [['M1', 'G']])
                self.assertEqual(group['path'], ['vss', 'net0', 'net3'])
            else:
                self.assertEqual(group['attachment_net'], 'vg')
                self.assertEqual(group['host_ports'], [['M1', 'D'], ['M2', 'D'], ['M5', 'D'], ['MP', 'G']])
                self.assertNotIn('CL', group['members'])
                self.assertNotIn('RL', group['members'])
                self.assertNotIn('vout', group['owner_junctions'])
            self.assertEqual(devices, original)

    def test_rename_reorder_and_passive_endpoint_reversal_keep_same_topology(self):
        devices = dict(v=source('VSRC', 'drive'), r=resistor('drive', 'gate'), a=mos('out', 'gate'))
        model, comps, nets, index = build(devices)
        old = deepcopy((devices, comps, nets, index))
        mapping = {'v': 'z99', 'r': 'a77', 'a': 'm66', 'drive': 'n101', 'gate': 'n100', 'out': 'n102'}
        changed = {mapping[name]: {k: mapping.get(value, value) if isinstance(value, str) else value
                    for k, value in dev.items()} for name, dev in reversed(list(devices.items()))}
        changed['a77']['a'], changed['a77']['b'] = changed['a77']['b'], changed['a77']['a']
        renamed, _, _, _ = build(changed)
        a, b = model['groups'][0], renamed['groups'][0]
        self.assertEqual(b['kind'], a['kind'])
        self.assertEqual(b['members'], [mapping[n] for n in a['members']])
        self.assertEqual(b['path'], [mapping.get(n, n) for n in a['path']])
        self.assertEqual(b['host_ports'], [[mapping[n], p] for n, p in a['host_ports']])
        reordered, _, _, _ = build(dict(reversed(list(devices.items()))), list(reversed(comps)))
        self.assertEqual(reordered['signature'], model['signature'])
        self.assertEqual((devices, comps, nets, index), old)

    def test_stale_index_or_changed_incidence_is_rejected(self):
        devices = tail_devices()
        _, comps, nets, index = build(devices)
        changed = deepcopy(nets)
        changed['tail'].pop()
        with self.assertRaises(ValueError): build_source_groups(devices, comps, changed, index)
        with self.assertRaises(ValueError): build_source_groups(devices, list(reversed(comps)), nets, index)


class SourceGroupCandidateTests(unittest.TestCase):
    def tail(self, reverse=False, pmos=False):
        return fixture(tail_devices(reverse, pmos), dict(a=(1, 2), b=(1, 4), bias=(4, 0), load=(0, 0)))

    def test_tail_proposals_use_true_pins_without_mutating_axes_policy_or_rng(self):
        pl, comps, nets, index, model = self.tail()
        original = freeze(pl)
        before = measure_source_groups(pl, comps, nets, index, model)
        with patch('netlist2schematic.routing.orthogonal_router.route_scene', side_effect=AssertionError('source helper called A*')):
            trials = list(source_group_trials(pl, comps, nets, index, model, max_candidates=24))
        self.assertTrue(trials)
        self.assertLessEqual(len(trials), 24)
        self.assertEqual(freeze(pl), original)
        self.assertTrue(any(measure_source_groups(t, comps, nets, index, model)['source_distance'] < before['source_distance'] for t, _ in trials))
        for trial, action in trials:
            self.assertEqual((trial.column_x, trial.row_y, trial.fixed_rows, trial.blocks),
                             (pl.column_x, pl.row_y, pl.fixed_rows, pl.blocks))
            self.assertEqual(trial.rng.getstate(), pl.rng.getstate())
            self.assertTrue(trial.valid_state())
            self.assertEqual(action['members'], ['bias'])
            self.assertTrue(action['preserve_polarity'])
            for name in ('a', 'b', 'load'):
                self.assertEqual((trial.cell_of[index[name]], trial.orient[index[name]]),
                                 (pl.cell_of[index[name]], pl.orient[index[name]]))

    def test_reversing_poles_changes_orientation_not_electrical_connections(self):
        for reverse in (False, True):
            pl, comps, nets, index, model = self.tail(reverse)
            frozen_nodes = deepcopy(comps[index['bias']]['nodes'])
            trial, _ = next(source_group_trials(pl, comps, nets, index, model))
            group = model['groups'][0]
            self.assertEqual(trial.pin_dir(index['bias'], group['signal_pin']), 'north')
            self.assertEqual(trial.orient[index['bias']], 2 if reverse else 0)
            self.assertEqual(comps[index['bias']]['nodes'], frozen_nodes)

    def test_pmos_tail_follows_actual_north_source_outlet(self):
        pl, comps, nets, index, model = self.tail(pmos=True)
        anchor = source_group_anchor(pl, comps, index, model['groups'][0])
        self.assertEqual(anchor['preferred_direction'], 'north')
        trials = list(source_group_trials(pl, comps, nets, index, model, max_candidates=8))
        self.assertTrue(trials)
        self.assertTrue(all(action['direction'] == 'north' for _, action in trials))

    def test_source_and_chain_are_jointly_ordered_with_real_endpoints(self):
        devices = dict(v=source('VSRC', 'drive'), r=resistor('drive', 'gate'), a=mos('out', 'gate'))
        pl, comps, nets, index, model = fixture(devices, dict(v=(4, 0), r=(3, 6), a=(1, 4)))
        original = freeze(pl)
        trials = list(source_group_trials(pl, comps, nets, index, model, max_candidates=24))
        self.assertTrue(trials)
        self.assertEqual(freeze(pl), original)
        for trial, action in trials:
            self.assertEqual(action['members'], ['v', 'r'])
            self.assertTrue(trial.valid_state())
            self.assertEqual(trial.cell_of[index['a']], pl.cell_of[index['a']])
            source_cell, resistor_cell = trial.cell_of[index['v']], trial.cell_of[index['r']]
            self.assertEqual(abs(source_cell[0]-resistor_cell[0])+abs(source_cell[1]-resistor_cell[1]), 1)
            source_pin = model['groups'][0]['signal_pin']
            self.assertNotEqual(trial.pin_dir(index['v'], source_pin), trial.pin_dir(index['r'], '1'))

    def test_explicit_fixed_source_or_path_member_prevents_transaction(self):
        for fixed_name in ('v', 'r'):
            devices = dict(v=source('VSRC', 'drive'), r=resistor('drive', 'gate'), a=mos('out', 'gate'))
            pl, comps, nets, index, model = fixture(devices, dict(v=(4, 0), r=(3, 6), a=(1, 4)))
            pl.fixed.add(index[fixed_name]); pl.fixed_cells[index[fixed_name]] = pl.cell_of[index[fixed_name]]
            original = freeze(pl)
            self.assertEqual(list(source_group_trials(pl, comps, nets, index, model)), [])
            self.assertEqual(freeze(pl), original)

    def test_missing_geometry_or_stale_host_is_unknown_not_zero(self):
        pl, comps, nets, index, model = self.tail()
        original = freeze(pl)
        missing = dict(index); del missing['a']
        measured = measure_source_groups(pl, comps, nets, missing, model)
        self.assertFalse(measured['measurable'])
        self.assertIsNone(measured['source_errors'])
        self.assertIsNone(measured['source_distance'])
        changed = deepcopy(comps); changed[index['a']]['nodes']['S'] = 'other'
        self.assertIsNone(source_group_anchor(pl, changed, index, model['groups'][0]))
        self.assertEqual(list(source_group_trials(pl, changed, nets, index, model)), [])
        self.assertEqual(freeze(pl), original)

    def test_zero_budget_expired_deadline_and_source_only_do_not_propose(self):
        pl, comps, nets, index, model = self.tail()
        original = freeze(pl)
        self.assertEqual(list(source_group_trials(pl, comps, nets, index, model, max_candidates=0)), [])
        self.assertEqual(list(source_group_trials(pl, comps, nets, index, model, deadline=time.perf_counter()-1)), [])
        empty, _, _, _ = build(dict(v=source('VSRC', 'vdd')))
        self.assertEqual(list(source_group_trials(pl, comps, nets, index, empty)), [])
        self.assertEqual(freeze(pl), original)
        for budget in (-1, 129, 1.0, True):
            with self.assertRaises(ValueError):
                list(source_group_trials(pl, comps, nets, index, model, max_candidates=budget))

    def test_chain_errors_are_explicit_even_when_source_facing_errors_are_zero(self):
        devices = dict(v=source('VSRC', 'drive'), r=resistor('drive', 'gate'), a=mos('out', 'gate'))
        pl, comps, nets, index, model = fixture(devices, dict(v=(2, 1), r=(2, 2), a=(2, 4)))
        orientations = list(pl.orient)
        orientations[index['v']], orientations[index['r']] = 1, 3
        pl.set_state(pl.cell_of, orientations)
        original = freeze(pl)
        measured = measure_source_groups(pl, comps, nets, index, model)
        self.assertEqual(source_chain_errors(pl, comps, index, model['groups'][0]), 0)
        self.assertEqual(measured['source_errors'], 0)
        self.assertEqual(freeze(pl), original)
        bent = deepcopy(pl)
        cells = list(bent.cell_of); cells[index['r']] = (3, cells[index['r']][1])
        bent.set_state(cells, bent.orient)
        broken = measure_source_groups(bent, comps, nets, index, model)
        record, = broken['records']
        self.assertEqual(record['raw_source_errors'], 0)
        self.assertGreater(record['chain_errors'], 0)
        self.assertEqual(record['source_errors'], record['raw_source_errors']+record['chain_errors'])
        self.assertEqual(broken['source_errors'], sum(r['source_errors'] for r in broken['records']))

    def test_chain_error_components_are_invariant_under_world_reflection(self):
        devices = dict(v=source('VSRC', 'drive'), r=resistor('drive', 'gate'), a=mos('out', 'gate'))
        pl, comps, nets, index, model = fixture(devices, dict(v=(2, 1), r=(3, 2), a=(2, 4)))
        orientations = list(pl.orient)
        orientations[index['v']], orientations[index['r']] = 1, 3
        pl.set_state(pl.cell_of, orientations)
        class Reflected:
            def pos_of(self, i):
                x, y = pl.pos_of(i)
                return -x, y
            def pin_world(self, i, pin):
                x, y = pl.pin_world(i, pin)
                return -x, y
            def pin_dir(self, i, pin):
                return {'east': 'west', 'west': 'east'}.get(pl.pin_dir(i, pin), pl.pin_dir(i, pin))
        a = measure_source_groups(pl, comps, nets, index, model)
        b = measure_source_groups(Reflected(), comps, nets, index, model)
        self.assertEqual([a[k] for k in ('source_errors', 'raw_source_errors', 'chain_errors', 'source_distance')],
                         [b[k] for k in ('source_errors', 'raw_source_errors', 'chain_errors', 'source_distance')])

    def test_state_and_clone_candidate_sequences_are_identical_without_state_cloning(self):
        devices = dict(v=source('VSRC', 'drive'), r=resistor('drive', 'gate'),
                       a=mos('out', 'gate'), i=source('ISRC', 'out'))
        pl, comps, nets, index, model = fixture(devices,
            dict(v=(4, 0), r=(1, 4), a=(2, 4), i=(4, 6)))
        pl.offset_x = [0.0]*len(comps)
        pl.offset_x[index['i']] = 8.0
        original = freeze(pl)
        for budget in (0, 1, 8, 17):
            with patch('netlist2schematic.plugins.attachment.attachment_sources.deepcopy', side_effect=AssertionError('SA state helper cloned')):
                states = list(source_group_states(pl, comps, nets, index, model, max_candidates=budget))
            clones = [(trial.cell_of, trial.orient, action) for trial, action
                      in source_group_trials(pl, comps, nets, index, model, max_candidates=budget)]
            self.assertEqual(states, clones)
            self.assertLessEqual(len(states), budget)
        self.assertEqual(freeze(pl), original)
        self.assertEqual(pl.offset_x[index['i']], 8.0)


if __name__ == '__main__':
    unittest.main()
