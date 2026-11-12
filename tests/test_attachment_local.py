"""One-ended passive guests use real access pins and existing vacancies."""
from copy import deepcopy
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from attachment_local import enum_local_attachment_trials
from readability_layout import measure_readability
from tests.test_attachment_ports import installed, pin_for
from tests.test_passive_placement import mos, resistor, source


def snapshot(pl):
    result = deepcopy({k: v for k, v in vars(pl).items() if k != 'rng'})
    result['rng_state'] = pl.rng.getstate()
    return result


def chain_fixture(mirror=False):
    devices = dict(q=mos('out', 'gate'), c=resistor('vin', 'middle', 'C'),
                   l=resistor('middle', 'gate', 'L'))
    pl, comps, nets, index, plan = installed(devices,
        dict(q=(2, 4), c=(0, 0), l=(1, 0)), dict(q=int(mirror)))
    group = next(g for g in plan['groups'] if g['kind'] == 'series_chain')
    return pl, comps, nets, index, plan, group


class LocalAttachmentTests(unittest.TestCase):
    def test_control_chain_has_joint_straight_trial_on_actual_gate_side(self):
        for mirror in (False, True):
            pl, comps, nets, index, plan, group = chain_fixture(mirror)
            original = snapshot(pl)
            with patch('orthogonal_router.route_scene', side_effect=AssertionError('local helper called A*')):
                trials = list(enum_local_attachment_trials(pl, comps, index, plan, group))
            self.assertTrue(trials)
            self.assertLessEqual(len(trials), 40)
            self.assertEqual(snapshot(pl), original)
            direction = 'east' if mirror else 'west'
            preferred = [(p, a) for p, a in trials if a['direction'] == direction and a['lateral'] == 0]
            self.assertTrue(preferred)
            for trial, action in preferred:
                self.assertTrue(trial.valid_state())
                q = index['q']
                self.assertEqual(trial.pos_of(q), pl.pos_of(q))
                self.assertEqual(trial.orient[q], pl.orient[q])
                self.assertEqual(trial.cell_of[index['c']][0], trial.cell_of[index['l']][0])
                self.assertEqual(trial.cell_of[index['l']][0], pl.cell_of[q][0])
                self.assertEqual(trial.pin_dir(index['l'], pin_for(comps[index['l']], 'gate')),
                                 'west' if mirror else 'east')
                self.assertEqual(action['members'], group['members'])
                self.assertFalse(action['axes_changed'])
            anchor = pl.pin_world(index['q'], 'G')
            for trial, _ in trials:
                for name in group['members']:
                    delta = trial.pos_of(index[name])[0]-anchor[0]
                    self.assertGreater(delta if mirror else -delta, 0)
                self.assertEqual((trial.column_x, trial.row_y, trial.fixed_rows, trial.blocks, trial.rng.getstate()),
                                 (pl.column_x, pl.row_y, pl.fixed_rows, pl.blocks, pl.rng.getstate()))
            before = measure_readability(pl, comps, index, plan['readability_plan'])
            self.assertTrue(any(measure_readability(t, comps, index, plan['readability_plan'])['owner_locality']
                                < before['owner_locality'] for t, _ in trials))

    def test_parallel_bank_stays_joint_and_true_terminal_direction_is_common(self):
        devices = dict(q=mos('out', 'gate'), r=resistor('out', '0'), c=resistor('0', 'out', 'C'))
        pl, comps, _, index, plan = installed(devices, dict(q=(2, 4), r=(0, 0), c=(0, 1)))
        group = next(g for g in plan['groups'] if g['kind'] == 'parallel_bank')
        old_nodes = deepcopy([c['nodes'] for c in comps])
        trials = list(enum_local_attachment_trials(pl, comps, index, plan, group))
        self.assertTrue(trials)
        for trial, action in trials:
            self.assertTrue(trial.valid_state())
            direction = action['direction']
            target = {'north': 'south', 'south': 'north', 'east': 'west', 'west': 'east'}[direction]
            for name in group['members']:
                self.assertEqual(trial.pin_dir(index[name], pin_for(comps[index[name]], 'out')), target)
            a, b = (trial.cell_of[index[name]] for name in group['members'])
            self.assertEqual(a[0] == b[0], direction in ('north', 'south'))
        self.assertEqual([c['nodes'] for c in comps], old_nodes)

    def test_one_rail_attachment_uses_real_owner_pin_not_dictionary_order(self):
        devices = dict(q=mos('out', 'gate'), c=resistor('0', 'gate', 'C'))
        pl, comps, _, index, plan = installed(devices, dict(q=(2, 4), c=(0, 0)))
        group = next(g for g in plan['groups'] if g['members'] == ['c'])
        comps[index['c']]['nodes'] = dict(reversed(list(comps[index['c']]['nodes'].items())))
        trials = list(enum_local_attachment_trials(pl, comps, index, plan, group))
        self.assertTrue(trials)
        for trial, action in trials:
            facing = {'north': 'south', 'south': 'north', 'east': 'west', 'west': 'east'}[action['direction']]
            self.assertEqual(trial.pin_dir(index['c'], '2'), facing)

    def test_fixed_guest_blocks_transaction_and_fixed_host_remains_unchanged(self):
        pl, comps, _, index, plan, group = chain_fixture()
        pl.fixed.add(index['l']); pl.fixed_cells[index['l']] = pl.cell_of[index['l']]
        original = snapshot(pl)
        self.assertEqual(list(enum_local_attachment_trials(pl, comps, index, plan, group)), [])
        self.assertEqual(snapshot(pl), original)
        pl, comps, _, index, plan, group = chain_fixture()
        q = index['q']; pl.fixed.add(q); pl.fixed_cells[q] = pl.cell_of[q]
        trials = list(enum_local_attachment_trials(pl, comps, index, plan, group))
        self.assertTrue(trials)
        for trial, _ in trials:
            self.assertEqual((trial.cell_of[q], trial.pos_of(q), trial.orient[q]),
                             (pl.cell_of[q], pl.pos_of(q), pl.orient[q]))

    def test_expired_deadline_missing_anchor_and_source_groups_do_not_trigger(self):
        pl, comps, _, index, plan, group = chain_fixture()
        original = snapshot(pl)
        self.assertEqual(list(enum_local_attachment_trials(pl, comps, index, plan, group, time.perf_counter()-1)), [])
        stale = deepcopy(comps); stale[index['q']]['nodes']['G'] = 'unknown'
        self.assertEqual(list(enum_local_attachment_trials(pl, stale, index, plan, group)), [])
        wrong = deepcopy(group); wrong['kind'] = 'neutral_junction_source'
        self.assertEqual(list(enum_local_attachment_trials(pl, comps, index, plan, wrong)), [])
        self.assertEqual(snapshot(pl), original)

    def test_only_guest_cells_change_and_occupied_hosts_are_not_evicted(self):
        pl, comps, _, index, plan, group = chain_fixture()
        other_cells = {i: cell for i, cell in enumerate(pl.cell_of) if comps[i]['id'] not in group['members']}
        trials = list(enum_local_attachment_trials(pl, comps, index, plan, group))
        for trial, _ in trials:
            self.assertEqual(len(set(trial.cell_of)), len(trial.cell_of))
            self.assertEqual({i: trial.cell_of[i] for i in other_cells}, other_cells)

    def test_two_owner_interior_bridge_is_not_reinterpreted_as_local_attachment(self):
        devices = dict(a=mos('left', 'ga'), b=mos('right', 'gb'), r=resistor('left', 'right'))
        pl, comps, _, index, plan = installed(devices, dict(a=(1, 1), b=(1, 6), r=(0, 0)))
        group, = plan['groups']
        self.assertEqual(list(enum_local_attachment_trials(pl, comps, index, plan, group)), [])

    def test_ground_shunt_prefers_outside_its_proven_bridge_and_real_marker_is_safe(self):
        from routing_rules import RoutingOptions
        from structured_placement import par
        from symmetry_layout import _static_safe
        devices = dict(a=mos('left', 'ga'), b=mos('right', 'gb'),
            r=resistor('left', 'mid0'), c=resistor('mid0', 'mid1', 'C'),
            l=resistor('mid1', 'right', 'L'), shunt=resistor('left', '0', 'C'))
        pl, comps, nets, index, plan = installed(devices,
            dict(a=(2, 1), b=(2, 6), r=(0, 1), c=(0, 2), l=(0, 3), shunt=(0, 0)))
        group = next(g for g in plan['groups'] if g['members'] == ['shunt'])
        trial, action = next(enum_local_attachment_trials(pl, comps, index, plan, group))
        self.assertEqual(action['direction'], 'south')
        self.assertLess(trial.pos_of(index['shunt'])[0], pl.pos_of(index['a'])[0])
        self.assertEqual(trial.pin_dir(index['shunt'], pin_for(comps[index['shunt']], '0')), 'south')
        self.assertTrue(_static_safe(par, trial, nets, index, RoutingOptions(), time.perf_counter()+1))

    @unittest.skipUnless((ROOT/'test_data/11-15/cross-coupled.cir').is_file(),
                         'optional data12 source fixture is unavailable')
    def test_production_data12_has_local_shunt_trial_with_safe_real_power_marker(self):
        import attachment_integration as integration
        import structured_placement as structured
        from netlist_core import parse_netlist, to_document
        from routing_rules import RoutingOptions
        from symmetry_layout import _static_safe
        devices = parse_netlist(ROOT/'test_data/11-15/cross-coupled.cir', cell='dut')
        structs = structured._structures(devices)
        document = to_document(devices)
        comps, nets = document['components'], document['nets']
        index = {comp['id']: i for i, comp in enumerate(comps)}
        levels = structured.dl.compute_levels(devices)
        conduction = {net for dev in devices.values() if structured._active(dev)
                      for net in (dev.get('d'), dev.get('s'), dev.get('c'), dev.get('e'))
                      if net is not None}
        attached = structured.legacy.attach_info(devices, levels['net_levels'], conduction)
        rows, _ = structured.legacy._allocate_rows(devices, comps, index,
            levels['net_levels'], levels['device_levels'], attached)
        plan = integration.build_attachment_plan(devices, comps, nets, index, structs, levels, rows)
        row_plan = plan.get('branch_row_plan')
        bridge = next(group for group in plan['groups'] if group['kind'] == 'multi_path_bridge')
        nodes = deepcopy([comp['nodes'] for comp in comps])
        for variant in (1, 2):
            with self.subTest(candidate=variant+1):
                seed = 20261029+(variant+1)*104729
                initial, _, _ = structured._initial_placement(devices, comps, nets, index,
                    levels, attached, structs, seed, variant, passive_layout='flexible',
                    row_override=[row_plan['rows'][comp['id']] for comp in comps] if row_plan else None,
                    bridge_alignment=row_plan.get('bridge_alignment', ()) if row_plan else ())
                if row_plan:
                    initial.branch_connections = deepcopy(row_plan.get('bridge_alignment', ()))
                current = integration._install(initial, comps, index, plan)
                slot = None
                for spec in integration._slot_specs(current, bridge, index):
                    trial = integration._joint_slot(current, comps, index, plan, bridge, spec)
                    if trial.valid_state():
                        slot = trial
                        break
                self.assertIsNotNone(slot)
                related = [group for group in plan['groups']
                           if group['kind'] == 'single_attachment'
                           and len(group['owner_junctions']) == 1
                           and set(group['owner_junctions']).intersection(bridge['owner_junctions'])]
                found = False
                with patch('orthogonal_router.route_scene', side_effect=AssertionError('static test called A*')):
                    for shunt in related:
                        for trial, _ in enum_local_attachment_trials(slot, comps, index, plan, shunt):
                            if trial.valid_state() and _static_safe(structured.par, trial, nets, index,
                                                                   RoutingOptions(), time.perf_counter()+1):
                                found = True
                                break
                        if found:
                            break
                self.assertTrue(found, 'a related shunt must have an existing-cell static-safe alternative')
                self.assertEqual([comp['nodes'] for comp in comps], nodes)


if __name__ == '__main__':
    unittest.main()
