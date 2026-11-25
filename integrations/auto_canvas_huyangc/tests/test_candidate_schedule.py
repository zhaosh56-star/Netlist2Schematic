from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from integrations.auto_canvas_huyangc.candidate_schedule import run_candidates
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.sa_routing import RoutingValidationError


class Clock:
    def __init__(self):
        self.now = 100.

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def valid_result(identifier, stage='normal', valid=True):
    metrics = dict(bends=3, crossings=1, wirelength=100)
    metadata = dict(candidate_id=identifier, routing_stage=stage,
                    quality=dict(score=7, selected_candidate=identifier, details=dict(score=7)))
    return None, [], {}, {}, metadata, ([], metrics, dict(valid=valid))


def failed(identifier, context=True):
    error = RoutingFailure('Controlled routing failure', dict(reason='no_path'))
    if context:
        scene = SimpleNamespace(pins={'small': [1, 2], 'large': [3, 4, 5, 6]})
        error.retry_context = dict(candidate_id=identifier)
        error.retry_options = SimpleNamespace(grid_step=2)
        error._rescue_context = dict(scene=scene,
            completed={'small' if identifier == '2' else 'large': []},
            failed_net='pending', pending=[], margin=150)
    return error


class CandidateScheduleTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.events = []
        self.check_source = Mock()
        self.saved = []
        self.finished = []
        self.repair = self.enter('netlist2schematic.recovery.primary_recovery.repair')
        self.rescue = self.enter('netlist2schematic.flow.run_flow._try_candidate_rescues')
        self.spacing = self.enter('netlist2schematic.recovery.spacing_retry.retry_spacing')
        self.allowed = self.enter('netlist2schematic.recovery.primary_recovery._spacing_allowed')
        self.repair.side_effect = lambda rows, **kw: (None, dict(status='failed'), rows)
        self.rescue.return_value = None
        self.spacing.side_effect = RoutingFailure('No path', dict(reason='no_path'))
        self.allowed.return_value = True

    def enter(self, target):
        patcher = patch(target)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def capture(self, rows, identifier, *, deadline):
        self.events.append('capture'+identifier)
        self.saved.extend(deepcopy(rows))
        self.assertLessEqual(self.clock(), deadline)

    def complete(self, result, identifier):
        self.events.append('complete'+identifier)
        self.finished.append(identifier)

    def run_schedule(self, normal, seconds=240, capture=None):
        return run_candidates(normal, seconds=seconds, clock=self.clock,
            check_source=self.check_source, on_failure=capture or self.capture,
            on_result=self.complete)

    def both_fail(self, identifier, seconds):
        self.events.append('normal'+identifier)
        raise failed(identifier)

    def assert_no_recovery(self):
        self.repair.assert_not_called()
        self.rescue.assert_not_called()
        self.spacing.assert_not_called()

    def test_failed_two_then_valid_three_skips_every_recovery(self):
        def normal(identifier, seconds):
            self.events.append('normal'+identifier)
            self.clock.advance(2)
            if identifier == '2':
                raise failed(identifier)
            return valid_result(identifier)
        results, report = self.run_schedule(normal)
        self.assertEqual(self.events, ['normal2', 'capture2', 'normal3', 'complete3'])
        self.assertEqual(len(results), 1)
        self.assertEqual(self.finished, ['3'])
        self.assertFalse(report['recovery_attempted'])
        self.assertEqual(report['stop_reason'], 'normal_result_available_recovery_skipped')
        self.assert_no_recovery()

    def test_valid_two_still_gives_three_its_normal_opportunity(self):
        def normal(identifier, seconds):
            self.events.append('normal'+identifier)
            if identifier == '3':
                raise failed(identifier)
            return valid_result(identifier)
        _, report = self.run_schedule(normal)
        self.assertEqual(self.events, ['normal2', 'complete2', 'normal3', 'capture3'])
        self.assertFalse(report['recovery_attempted'])
        self.assert_no_recovery()

    def test_two_valid_normal_results_remain_available_to_original_selection(self):
        results, report = self.run_schedule(lambda identifier, seconds: valid_result(identifier))
        self.assertEqual(self.finished, ['2', '3'])
        self.assertEqual(len(results), 2)
        self.assertEqual([row['status'] for row in report['candidates']], ['valid', 'valid'])
        self.assert_no_recovery()

    def test_both_normals_finish_before_progress_ranked_shared_repair(self):
        from netlist2schematic.recovery.primary_recovery import rank
        def repair(rows, **kwargs):
            self.assertEqual(self.events, ['normal2', 'capture2', 'normal3', 'capture3'])
            self.assertEqual(kwargs['candidate_ids'], ('2', '3'))
            best = rank(rows)[0]['candidate_id']
            self.assertEqual(best, '3')
            return valid_result(best, 'primary_repair'), dict(status='valid'), rows
        self.repair.side_effect = repair
        _, report = self.run_schedule(self.both_fail)
        self.assertEqual(self.finished, ['3'])
        self.assertEqual(report['stop_reason'], 'first_verified_shared_recovery')
        self.assertEqual(report['candidates'][1]['recovered_in_shared_stage'], 'primary_repair')
        self.rescue.assert_not_called()
        self.spacing.assert_not_called()

    def test_shared_local_rescue_success_stops_before_spacing(self):
        self.rescue.return_value = valid_result('3', 'local_rescue')
        _, report = self.run_schedule(self.both_fail)
        self.assertEqual(self.finished, ['3'])
        self.assertEqual([s['stage'] for s in report['stages']],
                         ['normal', 'normal', 'primary_repair', 'local_rescue'])
        self.spacing.assert_not_called()

    def test_spacing_tries_progress_ranked_candidates_once_and_stops(self):
        attempted = []
        def spacing(context, **kwargs):
            identifier = context['candidate_id']
            attempted.append(identifier)
            self.clock.advance(1)
            if identifier == '3':
                raise failed(identifier)
            return valid_result(identifier, 'spacing_retry')
        self.spacing.side_effect = spacing
        _, report = self.run_schedule(self.both_fail)
        self.assertEqual(attempted, ['3', '2'])
        self.assertEqual(self.finished, ['2'])
        self.assertTrue(report['stages'][-1]['shared'])

    def test_normal_and_checkpoint_work_cannot_consume_the_second_normal_slot(self):
        allowances = []
        def normal(identifier, seconds):
            allowances.append(seconds)
            self.clock.advance(seconds)
            raise failed(identifier)
        def capture(rows, identifier, *, deadline):
            self.clock.advance(max(0., deadline-self.clock()))
        _, report = self.run_schedule(normal, capture=capture)
        self.assertEqual(len(allowances), 2)
        self.assertAlmostEqual(allowances[0], allowances[1])
        self.assertGreater(allowances[1], 80)
        self.assertLessEqual(self.clock(), 340)
        self.repair.assert_not_called()
        self.rescue.assert_not_called()
        self.spacing.assert_not_called()

    def test_shared_stage_deadlines_use_remaining_time_without_reset(self):
        deadlines = []
        def repair(rows, **kwargs):
            deadlines.append(kwargs['deadline'])
            self.clock.now = kwargs['deadline']
            return None, dict(status='failed'), rows
        def rescue(rows, policy, seconds, stages):
            deadlines.append(self.clock()+seconds)
            self.clock.advance(seconds)
            return None
        def spacing(context, **kwargs):
            deadlines.append(kwargs['deadline'])
            self.clock.now = kwargs['deadline']
            raise failed(context['candidate_id'])
        self.repair.side_effect = repair
        self.rescue.side_effect = rescue
        self.spacing.side_effect = spacing
        _, report = self.run_schedule(self.both_fail)
        self.assertEqual(deadlines, sorted(deadlines))
        self.assertLessEqual(max(deadlines), 334)
        self.assertAlmostEqual(self.clock(), 334)
        self.assertEqual(report['stop_reason'], 'shared_recovery_exhausted')

    def test_unused_repair_time_carries_forward_to_later_stages(self):
        _, report = self.run_schedule(self.both_fail)
        stages = [row for row in report['stages'] if row.get('shared')]
        self.assertLess(stages[0]['allocated_seconds'], stages[1]['allocated_seconds'])
        self.assertLess(stages[1]['allocated_seconds'], stages[2]['allocated_seconds'])
        self.assertEqual(self.spacing.call_count, 2)
        json.dumps(report)
        self.assertNotIn('checkpoint', json.dumps(report))

    def test_fixed_devices_can_disable_spacing_without_moving_them(self):
        self.allowed.return_value = False
        _, report = self.run_schedule(self.both_fail)
        self.spacing.assert_not_called()
        self.assertTrue(all(row['reason'] == 'fixed_device_spacing'
                            for row in report['stages'][-1]['attempts']))

    def test_no_failure_context_safely_skips_recovery(self):
        def normal(identifier, seconds):
            raise failed(identifier, context=False)
        results, report = self.run_schedule(normal)
        self.assertEqual(results, [])
        self.assertEqual(report['stop_reason'], 'no_failure_context')
        self.assert_no_recovery()

    def test_zero_budget_does_not_start_any_candidate(self):
        normal = Mock()
        results, report = self.run_schedule(normal, seconds=0)
        normal.assert_not_called()
        self.assertEqual(results, [])
        self.assertTrue(all(r['normal_status'] == 'skipped' for r in report['candidates']))
        self.assert_no_recovery()

    def test_adaptive_recovery_retains_a_new_complete_pose_for_manual_wiring(self):
        def repair(rows,**kwargs):
            self.assertEqual(kwargs['layout_budget'],'adaptive')
            rows.append(dict(candidate_id='3',context=dict(new_complete_pose=True),
                             checkpoint=None,options=None,attempt_index=3))
            return None,dict(status='failed'),rows
        self.repair.side_effect=repair
        _,report=run_candidates(self.both_fail,seconds=240,clock=self.clock,
            check_source=self.check_source,on_failure=self.capture,on_result=self.complete,
            layout_budget='adaptive')
        self.assertTrue(any(row.get('context',{}).get('new_complete_pose') for row in self.saved))
        self.assertEqual(report['layout_budget'],'adaptive')

    def test_source_change_is_fatal_and_does_not_enter_recovery(self):
        self.check_source.side_effect = [None, ValueError('Source changed')]
        with self.assertRaisesRegex(ValueError, 'Source changed'):
            self.run_schedule(self.both_fail)
        self.assertEqual(self.events, ['normal2', 'capture2'])
        self.assert_no_recovery()

    def test_power_connection_error_is_not_recovered_as_a_signal_failure(self):
        def normal(identifier, seconds):
            raise RoutingFailure('Invalid power connection', dict(reason='power_connections'))
        with self.assertRaisesRegex(RoutingFailure, 'Invalid power connection'):
            self.run_schedule(normal)
        self.assertEqual(self.saved, [])
        self.assert_no_recovery()

    def test_foreign_or_unverified_recovery_cannot_be_published(self):
        for identifier, valid, error in [('1', True, ValueError), ('3', False, RoutingValidationError)]:
            with self.subTest(identifier=identifier, valid=valid):
                self.repair.side_effect = lambda rows, **kw: (
                    valid_result(identifier, 'primary_repair', valid), dict(status='valid'), rows)
                with self.assertRaises(error):
                    self.run_schedule(self.both_fail)
        self.assertEqual(self.finished, [])


if __name__ == '__main__':
    unittest.main()
