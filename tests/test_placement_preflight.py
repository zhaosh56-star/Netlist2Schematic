"""A failed fixed-port scene gets bounded, reversible passive-only repair."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from netlist_core import to_document
from orthogonal_router import RoutingFailure
import placement_preflight as preflight
from routing_rules import RoutingOptions, build_scene
from sa_common import load_local

PAR = load_local('preflight_test_geometry', 'place-and-route.py')


class GridPlacement(PAR.Placement):
    def allowed_orientations(self, i):
        return self.allowed.get(i, (0, 1) if self.comps[i]['symbol'] in ('nmos', 'pmos', 'npn', 'pnp')
                                else range(len(PAR.GEO_DOC[self.comps[i]['symbol']]['orientations'])))

    def blocks_contiguous(self):
        for group in self.blocks:
            for row in {self.cell_of[i][0] for i in group}:
                columns = [self.cell_of[i][1] for i in group if self.cell_of[i][0] == row]
                if any(self.comp_at.get((row, col)) not in (None, *group)
                       for col in range(min(columns), max(columns)+1)):
                    return False
        return True


def fixture(*, safe=False, connected=False):
    devices = {'active': dict(cls='MOS', type='NMOS', d='drain', g='input', s='0', bulk='0'),
               'passive': dict(cls='PASSIVE', type='R', a='drain' if connected else 'a', b='b')}
    document = to_document(devices)
    comps, nets = document['components'], document['nets']
    index = {row['id']: i for i, row in enumerate(comps)}
    pl = GridPlacement.__new__(GridPlacement)
    pl.comps = comps
    pl.cell_of = [(0, 1), (2 if safe else 1, 1)]
    pl.orient = [0, 0]
    pl.comp_at = {cell: i for i, cell in enumerate(pl.cell_of)}
    pl.row_of = [row for row, col in pl.cell_of]
    pl.column_x = [100, 200, 300, 400]
    pl.row_y = [200, 300, 400, 500]
    pl.offset_x = [0.0, 0.0]
    pl.fixed, pl.fixed_cells, pl.allowed, pl.blocks = set(), {}, {}, []
    return pl, comps, nets, index


def two_fault_fixture():
    devices = {}
    for suffix in ('a', 'b'):
        devices['active_' + suffix] = dict(cls='MOS', type='NMOS', d='d_' + suffix,
            g='g_' + suffix, s='0', bulk='0')
        devices['passive_' + suffix] = dict(cls='PASSIVE', type='R',
            a='pa_' + suffix, b='pb_' + suffix)
    doc = to_document(devices)
    comps, nets = doc['components'], doc['nets']
    index = {row['id']: i for i, row in enumerate(comps)}
    pl = GridPlacement.__new__(GridPlacement)
    pl.comps = comps
    cells = {'active_a': (0, 1), 'active_b': (0, 4),
             'passive_a': (1, 1), 'passive_b': (1, 4)}
    pl.cell_of = [cells[row['id']] for row in comps]
    pl.orient = [0] * len(comps)
    pl.comp_at = {cell: i for i, cell in enumerate(pl.cell_of)}
    pl.row_of = [row for row, col in pl.cell_of]
    pl.column_x = [100, 200, 300, 400, 500, 600]
    pl.row_y = [200, 300, 400, 500]
    pl.offset_x = [0.0] * len(comps)
    pl.fixed, pl.fixed_cells, pl.allowed, pl.blocks = set(), {}, {}, []
    return pl, comps, nets, index


class PlacementPreflightTests(unittest.TestCase):
    def repair(self, case, **kwargs):
        return preflight.repair_static_ports(PAR, *case, RoutingOptions(), **kwargs)

    def test_marker_collision_is_repaired_by_legal_passive_orientation(self):
        case = fixture()
        pl, comps, nets, index = case
        original = deepcopy(pl.__dict__)
        scene = build_scene(PAR, nets, pl, index, RoutingOptions())
        body = dict(scene.body_rects)[index['passive']]
        self.assertTrue(any(max(body[0], rect[0]) < min(body[2], rect[2]) and
                            max(body[1], rect[1]) < min(body[3], rect[3])
                            for net, rect in scene.marker_rects))
        self.assertFalse(preflight._static_safe(PAR, pl, nets, index, RoutingOptions(), None))
        changed, report = self.repair(case)
        self.assertTrue(report['applied'])
        self.assertEqual('orientation', report['actions'][0]['kind'])
        self.assertEqual(pl.cell_of, changed.cell_of)
        self.assertEqual(pl.pos_of(index['active']), changed.pos_of(index['active']))
        self.assertEqual(pl.orient[index['active']], changed.orient[index['active']])
        self.assertTrue(preflight._static_safe(PAR, changed, nets, index, RoutingOptions(), None))
        self.assertEqual(original, pl.__dict__)
        json.dumps(report)

    def test_unsafe_scene_can_move_only_to_a_free_neighbor_when_rotation_is_forbidden(self):
        case = fixture()
        case[0].allowed[case[3]['passive']] = [0]
        changed, report = self.repair(case)
        self.assertTrue(report['applied'])
        self.assertEqual('cell', report['actions'][0]['kind'])
        before, after = case[0].cell_of[1], changed.cell_of[1]
        self.assertLessEqual(abs(before[0]-after[0]), 1)
        self.assertLessEqual(abs(before[1]-after[1]), 1)
        self.assertNotEqual(before, after)
        self.assertEqual(changed.row_of, [row for row, col in changed.cell_of])
        self.assertEqual(changed.comp_at, {cell: i for i, cell in enumerate(changed.cell_of)})
        self.assertEqual(case[0].cell_of[0], changed.cell_of[0])
        self.assertTrue(preflight._static_safe(PAR, changed, case[2], case[3], RoutingOptions(), None))

    def test_unsafe_fixed_passive_is_not_moved_or_rotated(self):
        for fixed_value in (1, 'passive'):
            with self.subTest(fixed_value=fixed_value):
                case = fixture()
                case[0].fixed.add(fixed_value)
                changed, report = self.repair(case)
                self.assertIs(changed, case[0])
                self.assertFalse(report['applied'])
                self.assertEqual('no_safe_local_repair', report['stop_reason'])
                self.assertEqual(0, report['trials'])

    def test_a_static_safe_input_is_identical_and_never_copied_for_optimization(self):
        case = fixture(safe=True)
        original = deepcopy(case[0].__dict__)
        with patch('placement_preflight.deepcopy', side_effect=AssertionError('safe input optimized')):
            changed, report = self.repair(case)
        self.assertIs(changed, case[0])
        self.assertEqual(original, changed.__dict__)
        self.assertFalse(report['applied'])
        self.assertEqual('already_safe', report['stop_reason'])
        self.assertEqual(0, report['trials'])
        self.assertEqual(0, report['proposals'])

    def test_an_active_only_collision_is_not_repaired_by_moving_the_active(self):
        case = fixture()
        # Removing the passive leaves an active-only input; the mocked oracle
        # describes an unrepaired active/static obstruction, not a new device.
        pl, comps, nets, index = case
        pl.comps = comps[:1]
        pl.cell_of, pl.orient, pl.row_of, pl.offset_x = [(0, 1)], [0], [0], [0.0]
        pl.comp_at = {(0, 1): 0}
        nets = {n: [(name, pin) for name, pin in members if name == 'active']
                for n, members in nets.items()}
        nets = {n: members for n, members in nets.items() if members}
        with patch('placement_preflight._static_safe', return_value=False):
            changed, report = self.repair((pl, pl.comps, nets, {'active': 0}))
        self.assertIs(pl, changed)
        self.assertFalse(report['applied'])
        self.assertEqual(0, report['proposals'])

    def test_candidate_cannot_enter_the_interior_of_an_active_group(self):
        case = fixture()
        pl, comps, nets, index = case
        other = {'id': 'other_active', 'symbol': 'nmos', 'nodes': {'D': 'do', 'G': 'go', 'S': '0'}}
        comps.append(other)
        pl.cell_of.append((0, 3))
        pl.orient.append(0)
        pl.row_of.append(0)
        pl.offset_x.append(0)
        pl.comp_at[(0, 3)] = 2
        index['other_active'] = 2
        for pin, net in other['nodes'].items():
            nets.setdefault(net, []).append(('other_active', pin))
        pl.blocks = [[0, 2]]
        pl.allowed[1] = [0]
        forbidden = (0, 2)
        seen = []
        def oracle(par, trial, *args):
            seen.append(trial.cell_of[1])
            return trial.cell_of[1] == forbidden
        # Keep this synthetic oracle's opaque failure count constant: this test
        # checks forbidden cells, rather than a geometry-improving partial move.
        with patch('placement_preflight._static_safe', side_effect=oracle), \
                patch('placement_preflight._static_faults', return_value={
                    'count': 1, 'hard': set(), 'violations': []}):
            changed, report = self.repair(case)
        self.assertIs(changed, pl)
        self.assertNotIn(forbidden, seen)
        self.assertGreater(report['state_rejections'], 0)

    def test_hpwl_estimate_is_capped_at_115_percent_of_original(self):
        for estimate, expected in ((115.0, True), (115.01, False)):
            with self.subTest(estimate=estimate):
                case = fixture()
                def wire(trial, *args):
                    return 100.0 if trial is case[0] else estimate
                def oracle(par, trial, *args):
                    return trial is not case[0]
                with patch('placement_preflight._wire_estimate', side_effect=wire), \
                        patch('placement_preflight._static_safe', side_effect=oracle):
                    changed, report = self.repair(case)
                self.assertEqual(expected, report['applied'])
                self.assertAlmostEqual(115, report['estimated_hpwl_limit'])
                if expected:
                    self.assertIsNot(changed, case[0])
                else:
                    self.assertIs(changed, case[0])
                    self.assertGreater(report['hpwl_rejections'], 0)

    def test_original_estimate_is_really_recorded_for_a_connected_circuit(self):
        case = fixture(connected=True)
        original_estimate = preflight._wire_estimate(case[0], case[2], case[3], PAR)
        changed, report = self.repair(case)
        self.assertEqual(original_estimate, report['estimated_hpwl_before'])
        if report['applied']:
            self.assertLessEqual(preflight._wire_estimate(changed, case[2], case[3], PAR),
                                 1.15*original_estimate+1e-9)

    def test_all_rejected_trials_return_the_original_without_partial_changes(self):
        case = fixture()
        before = deepcopy(case[0].__dict__)
        with patch('placement_preflight._static_safe', return_value=False):
            changed, report = self.repair(case)
        self.assertIs(changed, case[0])
        self.assertEqual(before, changed.__dict__)
        self.assertFalse(report['applied'])
        self.assertEqual([], report['actions'])
        self.assertGreater(report['trials'], 0)

    def test_proposal_limit_is_finite_even_for_rejected_states(self):
        case = fixture()
        with patch('placement_preflight._static_safe', return_value=False), \
                patch('placement_preflight._static_faults', return_value={
                    'count': 1, 'hard': set(), 'violations': []}):
            changed, report = self.repair(case, max_trials=2)
        self.assertIs(changed, case[0])
        self.assertEqual(2, report['proposals'])
        self.assertLessEqual(report['trials'], 2)
        self.assertEqual('trial_limit', report['stop_reason'])

    def test_two_independent_real_marker_collisions_are_repaired_atomically(self):
        case = two_fault_fixture()
        original = deepcopy(case[0].__dict__)
        self.assertFalse(preflight._static_safe(PAR, case[0], case[2], case[3], RoutingOptions(), None))
        changed, report = self.repair(case)
        self.assertTrue(report['applied'])
        self.assertEqual(report['partial_improvements'], 1)
        self.assertEqual(len(report['actions']), 2)
        self.assertEqual({r['device'] for r in report['actions']}, {'passive_a', 'passive_b'})
        self.assertLess(report['best_partial_fault_count'], report['original_static_fault_count'])
        self.assertTrue(preflight._static_safe(PAR, changed, case[2], case[3], RoutingOptions(), None))
        self.assertLessEqual(report['proposals'], 64)
        self.assertEqual(original, case[0].__dict__)
        for name in ('active_a', 'active_b'):
            i = case[3][name]
            self.assertEqual(changed.pos_of(i), case[0].pos_of(i))
            self.assertEqual(changed.orient[i], case[0].orient[i])
        json.dumps(report)

    def test_unrepairable_second_marker_rolls_back_first_private_improvement(self):
        case = two_fault_fixture()
        case[0].fixed.add(case[3]['passive_b'])
        original = deepcopy(case[0].__dict__)
        changed, report = self.repair(case)
        self.assertIs(changed, case[0])
        self.assertEqual(original, changed.__dict__)
        self.assertFalse(report['applied'])
        self.assertEqual(report['partial_improvements'], 1)
        self.assertEqual(report['actions'], [])
        self.assertEqual(report['stop_reason'], 'no_safe_local_repair')

    def test_one_remaining_fault_still_finishes_with_one_action(self):
        case = two_fault_fixture()
        preflight._set_cell(case[0], case[3]['passive_b'], (2, 4))
        changed, report = self.repair(case)
        self.assertTrue(report['applied'])
        self.assertEqual(len(report['actions']), 1)
        self.assertEqual(report['partial_improvements'], 0)
        self.assertTrue(preflight._static_safe(PAR, changed, case[2], case[3], RoutingOptions(), None))

    def test_both_actions_share_original_115_percent_estimate_limit(self):
        case = two_fault_fixture()
        before = deepcopy(case[0].__dict__)
        def estimate(trial, *args):
            changed = sum(trial.orient[i] != case[0].orient[i] or trial.cell_of[i] != case[0].cell_of[i]
                          for i in range(len(case[1])))
            return 100. if not changed else 110. if changed == 1 else 120.
        with patch('placement_preflight._wire_estimate', side_effect=estimate):
            changed, report = self.repair(case)
        self.assertIs(changed, case[0])
        self.assertEqual(changed.__dict__, before)
        self.assertEqual(report['partial_improvements'], 1)
        self.assertGreater(report['hpwl_rejections'], 0)
        self.assertAlmostEqual(report['estimated_hpwl_limit'], 115.)
        self.assertEqual(report['actions'], [])

    def test_partial_progress_never_resets_the_64_proposal_budget(self):
        case = two_fault_fixture()
        pl, comps, nets, index = case
        pl.column_x = [100 + 100 * i for i in range(15)]
        for number in range(10):
            name = 'extra' + str(number)
            index[name] = len(comps)
            comp = {'id': name, 'symbol': 'resistor', 'nodes': {'1': name + 'a', '2': name + 'b'}}
            comps.append(comp)
            pl.cell_of.append((3, number))
            pl.orient.append(0)
            pl.row_of.append(3)
            pl.offset_x.append(0)
            for pin, net in comp['nodes'].items():
                nets[net] = [(name, pin)]
        pl.comp_at = {cell: i for i, cell in enumerate(pl.cell_of)}
        def score(par, trial, *args):
            return {'count': 4 if trial is pl else 3, 'hard': set(), 'violations': []}
        with patch('placement_preflight._static_safe', return_value=False), \
                patch('placement_preflight._static_faults', side_effect=score):
            changed, report = self.repair(case, max_trials=64)
        self.assertIs(changed, pl)
        self.assertEqual(report['partial_improvements'], 1)
        self.assertEqual(report['proposals'], 64)
        self.assertLessEqual(report['trials'], 64)
        self.assertEqual(report['stop_reason'], 'trial_limit')
        self.assertEqual(report['actions'], [])

    def test_full_fault_count_includes_body_and_marker_halo_overlap(self):
        case = fixture()
        score = preflight._static_faults(PAR, case[0], case[2], case[3], RoutingOptions(), None)
        self.assertIn('marker_body_halo', {v['kind'] for v in score['violations']})
        self.assertGreater(score['count'], 1)
        case[0].row_y[1] = 220
        score = preflight._static_faults(PAR, case[0], case[2], case[3], RoutingOptions(), None)
        self.assertIn('body_overlap', {v['kind'] for v in score['violations']})
        self.assertTrue(any(v[0] == 'body_overlap' for v in score['hard']))

    def test_fewer_faults_cannot_introduce_a_new_shape_obstruction(self):
        case = fixture()
        def score(par, trial, *args):
            return {'count': 4, 'hard': {('old_marker', 1)}, 'violations': []} if trial is case[0] else {
                'count': 1, 'hard': {('new_body_overlap', 0, 1)}, 'violations': []}
        with patch('placement_preflight._static_safe', return_value=False), \
                patch('placement_preflight._static_faults', side_effect=score):
            changed, report = self.repair(case)
        self.assertIs(changed, case[0])
        self.assertFalse(report['applied'])
        self.assertEqual(report['partial_improvements'], 0)
        self.assertGreater(report['partial_shape_rejections'], 0)

    def test_expiry_during_second_pass_rolls_back_both_private_states(self):
        case = two_fault_fixture()
        before = deepcopy(case[0].__dict__)
        clock = SimpleNamespace(now=perf_counter())
        oracle = preflight._static_safe
        def timed(par, trial, *args):
            touched = sum(trial.orient[i] != case[0].orient[i] or trial.cell_of[i] != case[0].cell_of[i]
                          for i in range(len(case[1])))
            if touched == 2:
                clock.now += 2
                raise RoutingFailure('spent', {'reason': 'quality_time_budget'})
            return oracle(par, trial, *args)
        with patch('placement_preflight.perf_counter', side_effect=lambda: clock.now), \
                patch('placement_preflight._static_safe', side_effect=timed):
            changed, report = self.repair(case)
        self.assertIs(changed, case[0])
        self.assertEqual(before, changed.__dict__)
        self.assertEqual(report['partial_improvements'], 1)
        self.assertEqual(report['stop_reason'], 'time_budget')
        self.assertFalse(report['applied'])
        self.assertEqual(report['actions'], [])

    def test_oracle_returning_safe_after_deadline_does_not_commit(self):
        case = fixture()
        clock = SimpleNamespace(now=perf_counter())
        def oracle(par, trial, *args):
            if trial is case[0]:
                return False
            clock.now += 2
            return True
        with patch('placement_preflight.perf_counter', side_effect=lambda: clock.now), \
                patch('placement_preflight._static_safe', side_effect=oracle):
            changed, report = self.repair(case)
        self.assertIs(changed, case[0])
        self.assertEqual(report['stop_reason'], 'time_budget')
        self.assertFalse(report['applied'])
        self.assertEqual(report['actions'], [])

    def test_zero_budget_and_expired_deadline_never_start_geometry_work(self):
        for options, reason in (({'seconds': 0}, 'zero_budget'), ({'deadline': perf_counter()-1}, 'time_budget')):
            with self.subTest(options=options), \
                    patch('placement_preflight._static_safe', side_effect=AssertionError('budget spent')):
                case = fixture()
                changed, report = self.repair(case, **options)
            self.assertIs(changed, case[0])
            self.assertEqual(reason, report['stop_reason'])
            self.assertFalse(report['applied'])

    def test_timeout_during_candidate_validation_keeps_attempt_diagnostics(self):
        case = fixture()
        clock = SimpleNamespace(now=perf_counter())
        def oracle(par, trial, *args):
            if trial is case[0]:
                return False
            clock.now += 2
            raise RoutingFailure('time exhausted', {'reason': 'quality_time_budget'})
        with patch('placement_preflight.perf_counter', side_effect=lambda: clock.now), \
                patch('placement_preflight._static_safe', side_effect=oracle):
            changed, report = self.repair(case)
        self.assertIs(changed, case[0])
        self.assertEqual('time_budget', report['stop_reason'])
        self.assertEqual(1, report['trials'])
        self.assertEqual([], report['actions'])

    def test_non_budget_routing_error_is_not_silently_labeled_timeout(self):
        with patch('placement_preflight._static_safe', side_effect=RoutingFailure('other failure', {'reason': 'no_path'})):
            with self.assertRaisesRegex(RoutingFailure, 'other failure'):
                self.repair(fixture())

    def test_repair_never_calls_sa_or_astar(self):
        with patch('sa_routing.final_route', side_effect=AssertionError('unexpected routing')), \
                patch('orthogonal_router._find_path', side_effect=AssertionError('unexpected A*')), \
                patch('structured_placement._anneal', side_effect=AssertionError('unexpected SA')):
            changed, report = self.repair(fixture())
        self.assertTrue(report['applied'])

    def test_invalid_parameters_are_rejected_before_geometry(self):
        for options in ({'seconds': -1}, {'seconds': float('nan')}, {'seconds': True},
                        {'max_trials': -1}, {'max_trials': True}, {'deadline': float('inf')}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.repair(fixture(), **options)


if __name__ == '__main__':
    unittest.main()
