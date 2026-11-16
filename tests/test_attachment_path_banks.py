"""Topology-derived physical path banks, without active-axis or route changes."""
from copy import deepcopy
from math import inf
from pathlib import Path
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_attachment_channels import fixture as channel_fixture
from tests.test_attachment_flow import candidate
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment.attachment_channels import channel_trials
from netlist2schematic.plugins.attachment.attachment_path_banks import (
    compile_banks, measure_banks, bank_rank, bank_trials, finish_banks, reserve_finish)
from netlist2schematic.plugins.attachment.attachment_selection import organization_rank, select_attachment_result
from netlist2schematic.layout.structured_placement import par
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.routing.orthogonal_router import RoutingFailure


def fixture(*, options=None, **kwargs):
    _, (pl, cs, nets, index, plan) = channel_fixture(**kwargs)
    bank, = plan['parallel_channels']['banks']
    # Existing initial channel planner allocates space. New finish never grows it.
    pl, _ = next(channel_trials(par, pl, cs, index, plan, bank,
        options=options or RoutingOptions(), initialization=True))
    return pl, cs, nets, index, plan


def state(pl):
    return deepcopy({k: v for k, v in vars(pl).items() if k != 'rng'}), pl.rng.getstate()


def finish(case, safe='real', count=0, deadline=None):
    pl, cs, nets, index, plan = case
    budget = ai.new_attachment_budget(); reserve_finish(plan, budget); budget['candidates'] = count
    report = dict(candidates=0, accepted=0, actions=[])
    until = perf_counter()+1 if deadline is None else deadline
    if safe == 'real':
        out = finish_banks(par, pl, cs, nets, index, plan, RoutingOptions(), budget, until, report, hpwl_limit=inf)
    else:
        with patch.object(ai, '_checked_static', return_value=safe):
            out = finish_banks(par, pl, cs, nets, index, plan, RoutingOptions(), budget, until, report, hpwl_limit=inf)
    return out, budget, report


class PhysicalBankTests(unittest.TestCase):
    def test_exact_paths_and_control_taps_compile_without_merging_hosts(self):
        pl, cs, nets, index, plan = fixture()
        bank, = plan['physical_path_banks']['banks']
        self.assertEqual([1, 2, 3], sorted(len(p['members']) for p in bank['paths']))
        self.assertEqual(6, len(bank['members']))
        self.assertFalse(set(bank['members']) & {'upper_a', 'lower_a', 'outsider'})
        self.assertEqual(plan['physical_path_banks'], compile_banks(list(reversed(cs)), plan))

    def test_real_geometry_proposals_are_joint_finite_and_safe_for_MOS_and_BJT(self):
        for kind in ({}, {'bjt': True}):
            pl, cs, nets, index, plan = fixture(**kind); before = state(pl); diagnostic = {}
            bank, = plan['physical_path_banks']['banks']
            proposals = list(bank_trials(par, pl, cs, nets, index, bank, RoutingOptions(), diagnostic=diagnostic))
            self.assertTrue(proposals); self.assertLessEqual(len(proposals), 12)
            self.assertEqual(1, diagnostic['scene_templates']); self.assertEqual(before, state(pl))
            safe = []
            for trial, action in proposals:
                self.assertEqual(pl.cell_of, trial.cell_of)
                self.assertEqual((pl.column_x, pl.row_y), (trial.column_x, trial.row_y))
                self.assertTrue(trial.valid_state())
                measured = measure_banks(trial, cs, index, plan['physical_path_banks'])
                self.assertEqual((0, 0, 0), (measured['outside_errors'], measured['direction_errors'], measured['order_errors']))
                self.assertEqual(0, measured['pin_alignment_error'])
                for i, comp in enumerate(cs):
                    if comp['id'] not in bank['members']:
                        self.assertEqual((pl.pos_of(i), pl.orient[i]), (trial.pos_of(i), trial.orient[i]))
                safe.append(ai._checked_static(par, trial, nets, index, RoutingOptions(), None))
            self.assertTrue(any(safe), kind)

    def test_real_finish_compacts_internal_links_and_reports_actual_channel_span(self):
        case = fixture(); before = state(case[0]); old = measure_banks(case[0], case[1], case[3], case[4]['physical_path_banks'])
        out, budget, report = finish(case)
        self.assertEqual(1, report['accepted']); self.assertEqual(before, state(case[0]))
        new = measure_banks(out, case[1], case[3], case[4]['physical_path_banks'])
        self.assertLess(bank_rank(new), bank_rank(old))
        self.assertLess(new['internal_wirelength'], old['internal_wirelength'])
        band = new['records'][0]['access_band']
        self.assertLessEqual(new['channel_span'], band[1]-band[0])
        self.assertLessEqual(report['actions'][0]['estimated_wire_after'], report['actions'][0]['estimated_wire_before'])
        self.assertTrue(ai._checked_static(par, out, case[2], case[3], RoutingOptions(), None))

    def test_changed_terminal_or_symbol_is_unmeasurable_not_zero(self):
        pl, cs, nets, index, plan = fixture()
        for mode in ('terminal', 'symbol'):
            changed = deepcopy(cs)
            if mode == 'terminal': changed[index['a']]['nodes']['1'] = 'unexpected'
            else: changed[index['a']]['symbol'] = 'diode'
            measured = measure_banks(pl, changed, index, plan['physical_path_banks'])
            self.assertFalse(measured['measurable']); self.assertIsNone(bank_rank(measured))
            self.assertIsNone(measured['internal_wirelength'])

    def test_unrelated_passive_tap_shared_members_and_source_ownership_rejected(self):
        pl, cs, nets, index, plan = fixture()
        changed = deepcopy(cs); changed.append(dict(id='branch', symbol='capacitor', nodes={'1': 'mid0', '2': 'external'}))
        self.assertFalse(compile_banks(changed, plan)['banks'])
        shared = deepcopy(plan); shared['parallel_channels']['banks'] *= 2
        self.assertFalse(compile_banks(cs, shared)['banks'])
        managed = deepcopy(plan); managed['source_managed_members'] = ['a']
        self.assertFalse(compile_banks(cs, managed)['banks'])

    def test_fixed_guest_or_custom_power_does_not_move_the_bank(self):
        for mode in ('fixed', 'power'):
            pl, cs, nets, index, plan = fixture(); bank = plan['physical_path_banks']['banks'][0]
            if mode == 'fixed': pl.fixed_cells = {index['a']: pl.cell_of[index['a']]}
            else: pl.power_connections = []
            with patch('netlist2schematic.routing.routing_rules.build_scene', side_effect=AssertionError('skip before template')):
                self.assertEqual([], list(bank_trials(par, pl, cs, nets, index, bank, RoutingOptions())))

    def test_reversed_facing_and_insufficient_band_leave_state_unchanged(self):
        pl, cs, nets, index, plan = fixture(); bank = plan['physical_path_banks']['banks'][0]
        cells = list(pl.cell_of); cells[index['upper_a']], cells[index['lower_a']] = cells[index['lower_a']], cells[index['upper_a']]
        pl.set_state(cells, pl.orient); before = state(pl)
        self.assertEqual([], list(bank_trials(par, pl, cs, nets, index, bank, RoutingOptions())))
        self.assertEqual(before, state(pl))
        pl, cs, nets, index, plan = fixture(); bank = plan['physical_path_banks']['banks'][0]
        diagnostic = {}
        with patch('netlist2schematic.plugins.attachment.attachment_path_banks._window', return_value=(200, 210, {'left': 214, 'right': 934},
                [dict(y=205, control_y=None) for p in bank['paths']])):
            self.assertEqual([], list(bank_trials(par, pl, cs, nets, index, bank, RoutingOptions(), diagnostic=diagnostic)))
        self.assertTrue(diagnostic['insufficient_vertical_room'])

    def test_renaming_and_input_reordering_preserve_measurement_and_positions(self):
        signatures = []
        for rename in (False, True):
            pl, cs, nets, index, plan = fixture(rename=rename); bank = plan['physical_path_banks']['banks'][0]
            proposals = list(bank_trials(par, pl, cs, nets, index, bank, RoutingOptions()))
            # Anonymous symbol/position signatures also cover net-order reversal.
            signatures.append([sorted((cs[i]['symbol'], trial.pos_of(i)) for i in (index[n] for n in bank['members']))
                               for trial, action in proposals])
        self.assertEqual(*signatures)

    def test_translation_and_horizontal_mirror_keep_rank(self):
        case = fixture(); out, _, report = finish(case); cs, index, model = case[1], case[3], case[4]['physical_path_banks']
        original = bank_rank(measure_banks(out, cs, index, model))
        for mirror in (False, True):
            class View:
                def pos_of(self, i):
                    x, y = out.pos_of(i); return (-x if mirror else x)+37, y-53
                def pin_world(self, i, pin):
                    x, y = out.pin_world(i, pin); return (-x if mirror else x)+37, y-53
                def pin_dir(self, i, pin):
                    d = out.pin_dir(i, pin); return {'east': 'west', 'west': 'east'}.get(d, d) if mirror else d
            self.assertEqual(original, bank_rank(measure_banks(View(), cs, index, model)))

    def test_custom_spacing_proposals_use_original_scene_rules(self):
        options = RoutingOptions(grid_step=4, cross_clearance=16, halo=14)
        pl, cs, nets, index, plan = fixture(options=options); bank = plan['physical_path_banks']['banks'][0]
        proposals = list(bank_trials(par, pl, cs, nets, index, bank, options))
        self.assertTrue(proposals)
        self.assertTrue(any(ai._checked_static(par, t, nets, index, options, None) for t, a in proposals))

    def test_failed_or_timed_out_full_checks_roll_back_every_member(self):
        for safe in (False, None):
            case = fixture(); before = state(case[0]); out, budget, report = finish(case, safe)
            self.assertIs(out, case[0]); self.assertEqual(before, state(case[0])); self.assertEqual(0, report['accepted'])
            self.assertEqual(safe is None, bool(budget.get('exhausted')))

    def test_expired_deadline_and_original_total_counter_are_respected(self):
        case = fixture()
        with patch('netlist2schematic.routing.routing_rules.build_scene', side_effect=AssertionError('expired')):
            out, budget, report = finish(case, deadline=perf_counter()-1)
        self.assertIs(out, case[0]); self.assertEqual(0, report['candidates'])
        out, budget, report = finish(case, False, count=124)
        self.assertLessEqual(budget['candidates'], 128)
        self.assertEqual(124+report['candidates'], budget['candidates'])
        self.assertEqual(4, report['physical_path_bank_finish']['reserved_trials'])

    def test_static_timeout_and_resource_error_remain_distinct(self):
        case = fixture()
        with patch('netlist2schematic.routing.routing_rules.build_scene', side_effect=RoutingFailure('expired', {'reason': 'quality_time_budget'})):
            out, budget, report = finish(case)
        self.assertIs(out, case[0]); self.assertTrue(budget['exhausted'])
        with patch('netlist2schematic.routing.routing_rules.build_scene', side_effect=ValueError('resource error')):
            with self.assertRaisesRegex(ValueError, 'resource error'): finish(case)

    def test_no_source_or_GT_reads_or_search_and_repeat_is_stable(self):
        case = fixture()
        with patch.object(Path, 'read_text', side_effect=AssertionError('no files')), patch(
                'netlist2schematic.routing.orthogonal_router.route_scene', side_effect=AssertionError('no Astar')):
            out, budget, report = finish(case)
        again, _, repeated = finish((out, *case[1:]))
        self.assertEqual(out.passive_local_offsets, again.passive_local_offsets)
        self.assertEqual(0, repeated['accepted'])

    def test_reserved_finish_does_not_grant_earlier_stages_extra_trials(self):
        pl, cs, nets, index, plan = fixture()
        budget = ai.new_attachment_budget(); reserve_finish(plan, budget)
        budget['candidates'] = 124
        self.assertEqual(4, budget['path_bank_reserve'])
        self.assertFalse(ai._has_budget(budget, perf_counter()+1))
        budget['candidates'] = 123
        self.assertTrue(ai._has_budget(budget, perf_counter()+1))

    def test_estimated_wire_increase_never_reaches_static_acceptance(self):
        case = fixture(); baseline = case[0]
        with patch('netlist2schematic.plugins.layout_shared.branch_alignment._hpwl',
                side_effect=lambda par, pl, ns, ix: 100 if pl is baseline else 101), patch.object(
                ai, '_checked_static', side_effect=AssertionError('wire gate first')):
            out, budget, report = finish(case)
        self.assertIs(out, baseline); self.assertEqual(0, report['accepted'])
        self.assertGreater(report['physical_path_bank_finish']['generation'][0]['hpwl_rejected'], 0)

    def test_two_paths_with_fixed_escape_contacts_pass_independent_full_validation(self):
        from tests.test_attachment_ports import installed
        from netlist2schematic.routing.routing_rules import build_scene
        from netlist2schematic.routing.sa_routing import validate_final_routes
        ds, original = channel_fixture()
        ds.pop('arm_a'); ds.pop('arm_b')
        ds['upper_a']['g'] = 'separate_control_a'
        ds['upper_b']['g'] = 'separate_control_b'
        cells = {c['id']: original[0].cell_of[i] for i, c in enumerate(original[1]) if c['id'] in ds}
        pl, cs, nets, index, plan = installed(ds, cells)
        bank, = plan['parallel_channels']['banks']
        pl, _ = next(channel_trials(par, pl, cs, index, plan, bank, options=RoutingOptions(), initialization=True))
        out, _, report = finish((pl, cs, nets, index, plan))
        self.assertEqual(1, report['accepted'])
        scene = build_scene(par, nets, out, index)
        routes = {n: list(scene.fixed_segments[n]) for n in scene.pins}
        model = plan['physical_path_banks']['banks'][0]
        for end in model['ends']:
            net = end['net']; hosts = {index[n] for n, p in end['hosts']}
            pins = [p.escape for p in scene.pins[net] if p.component in hosts]
            top, bottom = sorted(pins, key=lambda p: p[1])
            self.assertEqual(top[0], bottom[0])
            routes[net].append((top, bottom))
            for pin in scene.pins[net]:
                if pin.component not in hosts:
                    routes[net].append(((top[0], pin.escape[1]), pin.escape))
        ends = {e['net'] for e in model['ends']}
        for path in model['paths']:
            for net in path['path'][1:-1]:
                points = [p.escape for p in scene.pins[net]]
                self.assertEqual(2, len(points))
                routes[net].append(tuple(points))
        _, metrics, validation = validate_final_routes(par, nets, out, index, list(routes.items()))
        self.assertTrue(validation['valid']); self.assertEqual(0, metrics['body_pen'])


def bank_candidate(identifier, span, owner=2, length=1000, crossings=0, score=10):
    row = candidate(identifier, length=length, crossings=crossings, score=score)
    report = row[5]['layout_attachments']
    report['plan'] = {'physical_path_banks': {'banks': [{'id': 'common-evidence'}]}}
    report['metrics']['owner_distance'] = owner
    report['physical_path_bank_organization'] = dict(count=1, measurable=True,
        outside_errors=0, direction_errors=0, order_errors=0, pin_alignment_error=0,
        internal_wirelength=span, channel_span=span)
    return row


class PhysicalBankSelectionTests(unittest.TestCase):
    def test_bank_organization_can_win_but_never_bypass_real_route_limits(self):
        old = bank_candidate('1', 200, owner=1)
        new = bank_candidate('2', 50, owner=2, length=1100, score=11)
        self.assertIs(new, select_attachment_result([old, new])[0])
        for bad in (bank_candidate('2', 40, length=1151, score=11), bank_candidate('2', 40, crossings=1, score=11)):
            self.assertIs(old, select_attachment_result([old, bad])[0])

    def test_missing_bank_evidence_cannot_win_by_omission(self):
        row = bank_candidate('1', 200); report = row[5]['layout_attachments']
        self.assertIsNotNone(organization_rank(report))
        report['physical_path_bank_organization']['measurable'] = False
        self.assertIsNone(organization_rank(report))
        report.pop('physical_path_bank_organization')
        self.assertIsNone(organization_rank(report))

    def test_unrelated_empty_model_has_exact_previous_rank(self):
        row = candidate('1'); report = row[5]['layout_attachments']
        original = organization_rank(report)
        report['plan'] = {'physical_path_banks': {'banks': []}}
        self.assertEqual(original, organization_rank(report))


if __name__ == '__main__': unittest.main()
