"""Topology-owned, footprint-packed passive guests without a second grid."""
from copy import deepcopy
from math import inf
from pathlib import Path
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_attachment_ports import installed
from tests.test_passive_placement import amplifier, resistor, mos
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment.attachment_port_banks import (
    bank_trials, compile_banks, finish_banks, measure_banks)
from netlist2schematic.layout.structured_placement import par
from netlist2schematic.routing.routing_rules import RoutingOptions


def fixture(orientation=0, names=('amp', 'res', 'cap'), reverse=False):
    h, r, c = names
    dev = {h: amplifier('reference', 'sense'),
           r: resistor('sense', 'out'), c: resistor('out', 'sense', 'C')}
    if reverse:
        dev = dict(reversed(list(dev.items())))
    cells = {h: (2, 2), r: (2, 3), c: (2, 4)}
    case = installed(dev, cells, {h: 0, r: 1, c: 0})
    # The library has four geometric views; the original runtime policy allows
    # only its existing subset. Geometry probes do not widen that policy.
    case[0].orient[case[3][h]] = orientation
    return case


def state(pl):
    return deepcopy({k: v for k, v in vars(pl).items() if k != 'rng'}), pl.rng.getstate()


class PortBankTests(unittest.TestCase):
    def test_same_host_bank_is_not_a_two_host_interior_bridge(self):
        pl, comps, nets, index, plan = fixture()
        self.assertEqual(1, len(plan['port_banks']['banks']))
        group = next(g for g in plan['groups'] if len(g['members']) == 2)
        self.assertFalse(group['requires_interior'])
        self.assertEqual('same_host_port_bank', group['row_mode'])
        self.assertEqual(['amp'], group['host_members'])

    def test_renaming_and_netlist_reordering_preserves_role_evidence(self):
        for names, reverse in ((('amp', 'res', 'cap'), False), (('z', 'x', 'y'), True)):
            _, _, _, _, plan = fixture(names=names, reverse=reverse)
            bank = plan['port_banks']['banks'][0]
            self.assertEqual(names[0], bank['host'])
            self.assertEqual(set(names[1:]), set(bank['members']))
            self.assertEqual('IN-', bank['input_pin'])

    def test_signal_net_renaming_preserves_actual_proposals(self):
        original = fixture()
        dev = dict(amplifier=amplifier('new_ref', 'new_sense'),
            resistor=resistor('new_sense', 'new_out'), capacitor=resistor('new_out', 'new_sense', 'C'))
        dev['amplifier']['op'] = 'new_out'
        changed = installed(dev, dict(amplifier=(2,2), resistor=(2,3), capacitor=(2,4)),
            dict(amplifier=0, resistor=1, capacitor=0))
        outputs = []
        for pl, comps, _, index, plan in (original, changed):
            outputs.append([sorted(t.passive_local_offsets) for t, _ in bank_trials(
                par, pl, comps, index, plan['port_banks']['banks'][0], RoutingOptions())])
        self.assertEqual(*outputs)

    def test_trial_generation_reads_no_files_or_GT(self):
        pl, comps, _, index, plan = fixture()
        with patch.object(Path, 'read_text', side_effect=AssertionError('no file reading')):
            self.assertTrue(list(bank_trials(par, pl, comps, index,
                plan['port_banks']['banks'][0], RoutingOptions())))

    def test_four_rotations_have_safe_true_terminal_parallel_trials(self):
        for orientation in range(4):
            pl, comps, nets, index, plan = fixture(orientation)
            before = state(pl)
            bank = plan['port_banks']['banks'][0]
            with patch('netlist2schematic.routing.orthogonal_router.route_scene',
                       side_effect=AssertionError('Astar is forbidden in trial generation')):
                trials = list(bank_trials(par, pl, comps, index, bank, RoutingOptions()))
            self.assertEqual(12, len(trials))
            self.assertEqual(before, state(pl))
            self.assertTrue(any(ai._checked_static(par, t, nets, index, RoutingOptions(), None)
                                for t, _ in trials))
            for t, _ in trials:
                self.assertEqual(orientation in pl.allowed_orientations(index['amp']), t.valid_state())
                self.assertEqual(pl.cell_of, t.cell_of)
                self.assertEqual(pl.column_x, t.column_x)
                self.assertEqual(pl.row_y, t.row_y)
                self.assertEqual(pl.pos_of(index['amp']), t.pos_of(index['amp']))
                measured = measure_banks(t, comps, index, plan['port_banks'])
                self.assertEqual(0, measured['parallel_pin_error'])
                self.assertEqual(0, measured['direction_errors'])

    def test_coordinates_pack_at_actual_sizes_not_global_row_pitch(self):
        pl, comps, _, index, plan = fixture()
        t, _ = next(bank_trials(par, pl, comps, index, plan['port_banks']['banks'][0], RoutingOptions()))
        y = [t.pos_of(index[n])[1] for n in ('res', 'cap')]
        self.assertLess(abs(y[0]-y[1]), min(b-a for a, b in zip(pl.row_y, pl.row_y[1:])))
        self.assertTrue(any(abs(v) > 70 for n in ('res', 'cap')
                            for v in t.passive_local_offsets[index[n]]))
        self.assertEqual(pl.comp_at, t.comp_at)

    def test_translation_keeps_offsets_and_relative_placement(self):
        pl, comps, _, index, plan = fixture()
        one = list(bank_trials(par, pl, comps, index, plan['port_banks']['banks'][0], RoutingOptions()))
        shifted = deepcopy(pl)
        shifted.column_x = [v+37 for v in pl.column_x]
        shifted.row_y = [v-53 for v in pl.row_y]
        two = list(bank_trials(par, shifted, comps, index, plan['port_banks']['banks'][0], RoutingOptions()))
        self.assertEqual(len(one), len(two))
        for (a, _), (b, _) in zip(one, two):
            self.assertEqual(a.passive_local_offsets, b.passive_local_offsets)

    def test_rotated_host_keeps_geometrically_corresponding_bank(self):
        pl, comps, _, index, plan = fixture(0)
        mirror, mc, _, mi, mp = fixture(2)
        # Rotation180 also flips the input offset, so the preferred bank side flips.
        one = next(bank_trials(par, pl, comps, index, plan['port_banks']['banks'][0], RoutingOptions()))[0]
        two = next(bank_trials(par, mirror, mc, mi, mp['port_banks']['banks'][0], RoutingOptions()))[0]
        for n in ('res', 'cap'):
            for pin in comps[index[n]]['nodes']:
                a = one.pin_world(index[n], pin)
                b = two.pin_world(mi[n], pin)
                center = pl.pos_of(index['amp'])
                self.assertEqual((2*center[0], 2*center[1]), (a[0]+b[0], a[1]+b[1]))

    def test_custom_spacing_and_grid_are_used_without_clearance_waivers(self):
        pl, comps, nets, index, plan = fixture()
        opts = RoutingOptions(grid_step=8, halo=18, cross_clearance=16)
        ts = list(bank_trials(par, pl, comps, index, plan['port_banks']['banks'][0], opts))
        self.assertTrue(any(ai._checked_static(par, t, nets, index, opts, None) for t, _ in ts))
        for t, _ in ts:
            for n in ('res', 'cap'):
                self.assertTrue(all(abs(x/8-round(x/8)) < 1e-8
                    for x in t.passive_local_offsets[index[n]]))

    def test_fixed_member_skips_but_fixed_host_is_preserved(self):
        pl, comps, _, index, plan = fixture()
        pl.fixed.add(index['res'])
        bank = plan['port_banks']['banks'][0]
        self.assertEqual([], list(bank_trials(par, pl, comps, index, bank, RoutingOptions())))
        pl.fixed.clear(); pl.fixed.add(index['amp'])
        original = pl.pos_of(index['amp']), pl.orient[index['amp']]
        self.assertTrue(all((t.pos_of(index['amp']), t.orient[index['amp']]) == original
            for t, _ in bank_trials(par, pl, comps, index, bank, RoutingOptions())))

    def test_expired_generation_does_no_copying(self):
        pl, comps, _, index, plan = fixture()
        with patch('netlist2schematic.plugins.attachment.attachment_port_banks.deepcopy',
                   side_effect=AssertionError('no expired copying')):
            self.assertEqual([], list(bank_trials(par, pl, comps, index,
                plan['port_banks']['banks'][0], RoutingOptions(), perf_counter()-1)))

    def test_changed_connection_is_unmeasurable_and_generates_nothing(self):
        pl, comps, _, index, plan = fixture()
        bad = deepcopy(comps)
        bad[index['cap']]['nodes']['1'] = 'different'
        model = plan['port_banks']
        self.assertFalse(measure_banks(pl, bad, index, model)['measurable'])
        self.assertEqual([], list(bank_trials(par, pl, bad, index, model['banks'][0], RoutingOptions())))

    def test_ambiguous_three_host_ports_not_claimed(self):
        pl, comps, _, index, plan = fixture()
        groups = deepcopy(plan['groups'])
        groups[0]['host_ports'].append(['amp', 'IN+'])
        model = compile_banks(comps, groups)
        self.assertEqual([], model['banks'])
        self.assertTrue(model['skipped'])

    def test_mos_shared_rail_does_not_create_an_opamp_bank(self):
        pl, comps, nets, index, plan = installed(dict(q=mos('out', 'g'),
            r=resistor('out', 'g'), c=resistor('out', 'g', 'C')),
            dict(q=(2,2), r=(2,3), c=(1,3)))
        self.assertEqual([], plan['port_banks']['banks'])

    def test_finish_uses_common_counter_accepts_only_full_static_result(self):
        pl, comps, nets, index, plan = fixture()
        old = state(pl)
        budget = ai.new_attachment_budget(seconds=1, max_candidates=128)
        report = dict(candidates=0, accepted=0, actions=[])
        current = finish_banks(par, pl, comps, nets, index, plan, RoutingOptions(),
            budget, perf_counter()+1, report, hpwl_limit=inf)
        self.assertEqual(old, state(pl))
        self.assertEqual(1, report['port_bank_finish']['accepted'])
        self.assertEqual(budget['candidates'], report['candidates'])
        self.assertLessEqual(budget['candidates'], 12)
        self.assertTrue(ai._checked_static(par, current, nets, index, RoutingOptions(), None))
        self.assertLess(ai.attachment_rank(ai.measure_attachments(current, comps, index, plan)),
                        ai.attachment_rank(ai.measure_attachments(pl, comps, index, plan)))

    def test_failed_or_late_validation_rolls_back_without_touching_incumbent(self):
        for answer in (False, None):
            pl, comps, nets, index, plan = fixture()
            old = state(pl)
            budget = ai.new_attachment_budget(seconds=1, max_candidates=128)
            report = dict(candidates=0, accepted=0, actions=[])
            with patch.object(ai, '_checked_static', return_value=answer):
                current = finish_banks(par, pl, comps, nets, index, plan, RoutingOptions(),
                    budget, perf_counter()+1, report, hpwl_limit=inf)
            self.assertIs(current, pl)
            self.assertEqual(old, state(pl))
            self.assertFalse(report['accepted'])
            self.assertEqual(answer is None, bool(budget.get('exhausted')))

    def test_exhausted_shared_budget_is_not_reopened(self):
        pl, comps, nets, index, plan = fixture()
        budget = ai.new_attachment_budget(seconds=1, max_candidates=128)
        budget['candidates'] = 128
        report = dict(candidates=0, accepted=0, actions=[])
        with patch('netlist2schematic.plugins.attachment.attachment_port_banks.bank_trials',
                   side_effect=AssertionError('no new budget')):
            current = finish_banks(par, pl, comps, nets, index, plan, RoutingOptions(),
                budget, perf_counter()+1, report, hpwl_limit=inf)
        self.assertIs(pl, current)
        self.assertEqual(128, budget['candidates'])

    def test_source_managed_guests_do_not_get_second_ownership(self):
        pl, comps, nets, index, plan = fixture()
        plan['source_managed_members'] = ['res']
        budget = ai.new_attachment_budget()
        report = dict(candidates=0, accepted=0, actions=[])
        current = finish_banks(par, pl, comps, nets, index, plan, RoutingOptions(),
            budget, perf_counter()+1, report, hpwl_limit=inf)
        self.assertIs(pl, current)
        self.assertEqual(0, budget['candidates'])
        self.assertEqual('source_managed_guests', report['port_bank_finish']['skipped'][0]['reason'])


if __name__ == '__main__':
    unittest.main()
