"""Independent candidate checkpoints and one bounded failure-rescue stage."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from netlist2schematic.flow import aesthetic_flow, run_flow
from netlist2schematic.recovery.candidate_rescue import (
    capture_candidate, progress, rank_candidates)
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.routing_rules import RoutingOptions, Scene
from tests.test_reserved_flow import candidate, valid


def snapshot(ident, *, completed=('a',), attempt=0, options=None):
    options = options or RoutingOptions()
    scene = Scene(options=options, pins={'a': [1, 2, 3], 'b': [4, 5], 'c': [6]})
    remaining = [name for name in scene.pins if name not in completed]
    checkpoint = dict(scene=scene, completed={name: [((0, 0), (10, 0))]
                                             for name in completed},
                      failed_net=remaining[0], pending=remaining[1:], margin=150,
                      options=options, branch={'partial_tree': object()})
    return capture_candidate(candidate(ident), checkpoint, options, ident, attempt)


class CheckpointCaptureTests(unittest.TestCase):
    def fail_candidates(self, rows, checkpoints, **kwargs):
        errors = []
        def route(par, nets, placement, index, **unused):
            ident = placement.identifier
            error = RoutingFailure('blocked', {'reason': 'no_path'})
            if ident in checkpoints:
                error._rescue_context = checkpoints[ident]
            errors.append(error)
            raise error
        with patch('netlist2schematic.layout.structured_placement.create_candidates',
                   return_value=iter(rows)), patch.object(aesthetic_flow, 'final_route', side_effect=route), \
                patch('netlist2schematic.plugins.preflight.placement_preflight.repair_static_ports',
                      side_effect=lambda par, pl, *args, **kw: (pl, deepcopy(next(
                          row[4].get('port_preflight', {}) for row in rows if row[0].identifier == pl.identifier)))), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(RoutingFailure) as caught:
                aesthetic_flow.select_layout('unused.cir', passive_skeleton='auto', **kwargs)
        return caught.exception, errors

    def test_both_primary_scenes_survive_without_reference_context(self):
        checkpoints = {i: snapshot(i)['checkpoint'] for i in (2, 3, 1)}
        error, originals = self.fail_candidates([candidate(i) for i in (2, 3, 1)], checkpoints)
        pool = error._recovery_candidates
        self.assertEqual(['2', '3'], [row['candidate_id'] for row in pool])
        self.assertEqual(3, error.retry_context[0].identifier)
        for row in pool:
            self.assertEqual(int(row['candidate_id']), row['context'][0].identifier)
            self.assertIsNot(row['checkpoint']['scene'], checkpoints[int(row['candidate_id'])]['scene'])
        pool[-1]['context'][0].identifier = 99
        pool[-1]['checkpoint']['completed'].clear()
        self.assertEqual(3, error.retry_context[0].identifier)
        self.assertTrue(error._rescue_context['completed'])
        self.assertTrue(originals[-1]._rescue_context['completed'])
        json.dumps(error.diagnostics)
        self.assertNotIn('partial_tree', json.dumps(error.diagnostics))

    def test_static_rejection_and_no_checkpoint_cannot_replace_primary_scene(self):
        rejected = list(candidate(3))
        rejected[4]['port_preflight'] = {'static_verification': {
            'complete': True, 'valid': False, 'fault_count': 1}}
        error, calls = self.fail_candidates([candidate(2), tuple(rejected), candidate(1)],
                                            {2: snapshot(2)['checkpoint']}, port_preflight='repair')
        self.assertEqual(1, len(calls))
        self.assertEqual(['2'], [row['candidate_id'] for row in error._recovery_candidates])
        self.assertEqual(2, error.retry_context[0].identifier)
        self.assertEqual(error._rescue_context['failed_net'],snapshot(2)['checkpoint']['failed_net'])

    def test_explicit_replay_keeps_single_scene_recovery(self):
        error, _ = self.fail_candidates([candidate(3)], {3: snapshot(3)['checkpoint']}, candidate_id='3')
        self.assertFalse(hasattr(error, '_recovery_candidates'))
        self.assertEqual(3, error.retry_context[0].identifier)

    def test_missing_context_or_checkpoint_is_not_a_scene(self):
        self.assertIsNone(capture_candidate(None, {}, None, 2, 0))
        self.assertIsNone(capture_candidate(candidate(2), None, None, 2, 0))


class RescueRankingTests(unittest.TestCase):
    def test_primary_progress_then_stable_recent_tie_before_reference(self):
        rows = [snapshot(2, completed=('b',), attempt=0),
                snapshot(3, completed=('a',), attempt=1),
                snapshot(1, completed=('a', 'b'), attempt=2)]
        self.assertEqual(['3', '2', '1'], [row['candidate_id'] for row, _ in rank_candidates(rows)])
        rows[0] = snapshot(2, completed=('a',), attempt=0)
        self.assertEqual(['3', '2', '1'], [row['candidate_id'] for row, _ in rank_candidates(rows)])
        self.assertEqual(['3', '2', '1'], [row['candidate_id'] for row, _ in rank_candidates(list(reversed(rows)))])

    def test_partial_failed_net_never_receives_completed_terminal_credit(self):
        row = snapshot(2, completed=('b',))
        before = progress(row)
        row['checkpoint']['branch'] = dict(terminals_connected=10000, segments=[1]*1000)
        self.assertEqual(before, progress(row))
        self.assertEqual((1, 2, 6), (before['completed_nets'], before['completed_terminals'], before['total_terminals']))


class SharedRescueBudgetTests(unittest.TestCase):
    def run_rescue(self, rows, behavior, seconds=12, policy='auto'):
        self.clock = SimpleNamespace(now=100.)
        stages = []
        def invoke(context, checkpoint, **kwargs):
            return behavior(context, checkpoint, kwargs)
        with patch.object(run_flow, 'perf_counter', side_effect=lambda: self.clock.now), \
                patch('netlist2schematic.recovery.local_rescue.rescue_routing', side_effect=invoke) as router, \
                redirect_stdout(io.StringIO()):
            result = run_flow._try_candidate_rescues(rows, policy, seconds, stages)
        return result, stages, router

    def test_all_attempts_share_budget_and_stop_on_first_valid_result(self):
        rows = [snapshot(2, attempt=0), snapshot(3, attempt=1), snapshot(1, attempt=2)]
        seen = []
        def rescue(context, checkpoint, kwargs):
            ident = context[0].identifier
            seen.append((ident, kwargs['seconds']))
            self.clock.now += kwargs['seconds']
            if ident == 3:
                raise RoutingFailure('no path', {'reason': 'no_safe_goal'})
            return (*context, valid())
        result, stages, router = self.run_rescue(rows, rescue)
        self.assertEqual([(3, 4.), (2, 4.)], seen)
        self.assertEqual(2, router.call_count)
        self.assertEqual(1, len(stages))
        self.assertEqual('valid', stages[0]['status'])
        report = result[4]['local_rescue']['candidate_recovery']
        self.assertEqual(('2', 12, 8.), (report['selected_candidate'], report['budget_seconds'], report['elapsed_seconds']))
        json.dumps(report)
        self.assertNotIn('partial_tree', json.dumps(report))

    def test_unused_time_rolls_forward_without_resetting_full_budget(self):
        budgets = []
        def rescue(context, checkpoint, kwargs):
            budgets.append(kwargs['seconds'])
            self.clock.now += 1 if len(budgets) == 1 else kwargs['seconds']
            raise RoutingFailure('timeout', {'reason': 'quality_time_budget'})
        result, stages, _ = self.run_rescue([snapshot(i, attempt=n) for n, i in enumerate((2, 3, 1))], rescue)
        self.assertIsNone(result)
        self.assertEqual([4., 5.5, 5.5], budgets)
        self.assertEqual(112., self.clock.now)
        self.assertEqual('time_budget', stages[0]['diagnostics']['candidate_recovery']['stop_reason'])

    def test_ranking_time_is_inside_shared_deadline(self):
        from netlist2schematic.recovery import candidate_rescue
        actual = candidate_rescue.rank_candidates
        def slow_rank(rows):
            self.clock.now += 3
            return actual(rows)
        seen = []
        def rescue(context, checkpoint, kwargs):
            seen.append(kwargs['seconds'])
            self.clock.now += kwargs['seconds']
            raise RoutingFailure('timeout', {'reason': 'quality_time_budget'})
        with patch.object(candidate_rescue, 'rank_candidates', side_effect=slow_rank):
            _, _, _ = self.run_rescue([snapshot(2), snapshot(1)], rescue, seconds=10)
        self.assertEqual([3.5, 3.5], seen)
        self.assertEqual(110., self.clock.now)

    def test_single_reference_scene_receives_whole_stage_budget(self):
        seen = []
        def rescue(context, checkpoint, kwargs):
            seen.append(kwargs['seconds'])
            return (*context, valid())
        self.run_rescue([snapshot(1)], rescue, seconds=10)
        self.assertEqual([10.], seen)

    def test_zero_or_disabled_does_not_rank_or_search(self):
        for kwargs in (dict(seconds=0), dict(policy='off')):
            with self.subTest(kwargs=kwargs):
                _, stages, router = self.run_rescue([{'invalid': object()}], None, **kwargs)
                router.assert_not_called()
                self.assertEqual('disabled', stages[0]['reason'])

    def test_failed_mutation_cannot_leak_to_another_scene_or_original(self):
        rows = [snapshot(2, options=RoutingOptions(bend_cost=7)),
                snapshot(1, options=RoutingOptions(bend_cost=19))]
        seen = []
        def rescue(context, checkpoint, kwargs):
            seen.append((context[0].identifier, kwargs['routing_options'].bend_cost,
                         checkpoint['scene'].options.bend_cost))
            context[0].identifier = 99
            checkpoint['scene'].pins.clear()
            raise RoutingFailure('blocked', {'reason': 'no_path'})
        self.run_rescue(rows, rescue)
        self.assertEqual([(2, 7, 7), (1, 19, 19)], seen)
        self.assertEqual([2, 1], [row['context'][0].identifier for row in rows])
        self.assertEqual([7, 19], [row['options'].bend_cost for row in rows])
        self.assertTrue(all(row['checkpoint']['scene'].pins for row in rows))

    def test_invalid_result_does_not_stop_other_scenes(self):
        def rescue(context, checkpoint, kwargs):
            routes, metrics, checked = valid()
            checked['valid'] = context[0].identifier == 1
            return (*context, (routes, metrics, checked))
        result, stages, _ = self.run_rescue([snapshot(2), snapshot(1)], rescue)
        self.assertEqual(1, result[0].identifier)
        self.assertEqual('local_rescue_validation', stages[0]['candidate_recovery']['attempts'][0]['diagnostics']['reason'])

    def test_corrupt_geometry_and_power_errors_are_not_recovery_failures(self):
        for error in (ValueError('wrong scene'), RoutingFailure('bad power', {'reason': 'power_connections'})):
            def rescue(*unused):
                raise error
            with self.subTest(error=str(error)), self.assertRaises(type(error)):
                self.run_rescue([snapshot(2), snapshot(1)], rescue)

    def test_copy_preparation_cannot_reset_attempt_budget(self):
        class SlowPlacement:
            def __deepcopy__(placement, memo):
                self.clock.now += 3
                return placement
        self.clock = SimpleNamespace(now=100.)
        context = list(candidate(2))
        context[0] = SlowPlacement()
        stages = []
        with patch.object(run_flow, 'perf_counter', side_effect=lambda: self.clock.now), \
                patch('netlist2schematic.recovery.local_rescue.rescue_routing') as router, redirect_stdout(io.StringIO()):
            result = run_flow._try_local_rescue(context, {}, None, 'auto', 2, stages, deadline=102.)
        self.assertIsNone(result)
        router.assert_not_called()
        self.assertEqual('rescue_preparation', stages[0]['diagnostics']['stage'])


class PublicRecoveryIntegrationTests(unittest.TestCase):
    def test_all_pool_failures_preserve_original_spacing_context_and_safe_output(self):
        from netlist2schematic.common.sa_common import load_local
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'small.cir'
            source.write_bytes(b'R1 input output 1k\r\nC1 output 0 1n\r\n.end\r\n')
            with redirect_stdout(io.StringIO()):
                normal = run_flow._normal_result(source, load_local('place_and_route', 'place-and-route.py'),
                    'layered', 'structured', None, 0, 17, 10, '1', None)
            failure = RoutingFailure('normal failed', {'reason': 'no_path'})
            failure.retry_context = deepcopy(normal[:5])
            failure.retry_options = RoutingOptions()
            failure._recovery_candidates = []
            for n, i in enumerate((2, 3, 1)):
                row = snapshot(i, attempt=n)
                row['context'] = deepcopy(normal[:5])
                row['context'][4]['candidate_id'] = str(i)
                failure._recovery_candidates.append(row)
            original_cells = deepcopy(normal[0].cell_of)
            def rescue(context, checkpoint, **kwargs):
                context[0].cell_of[0] = (99, 99)
                context[4]['uncommitted'] = True
                checkpoint.clear()
                raise RoutingFailure('blocked', {'reason': 'no_safe_goal'})
            with patch.object(run_flow, '_normal_result', side_effect=failure), \
                    patch('netlist2schematic.recovery.local_rescue.rescue_routing', side_effect=rescue), \
                    patch('netlist2schematic.recovery.spacing_retry.retry_spacing', return_value=deepcopy(normal)) as spacing, \
                    redirect_stdout(io.StringIO()):
                data = run_flow.run(source, iterations=0, out_dir=Path(directory)/'output',
                                    rescue_seconds=3, spacing_seconds=2, simplify_routes='off',
                                    passive_skeleton='off')
            self.assertEqual(original_cells, spacing.call_args.args[0][0].cell_of)
            self.assertNotIn('uncommitted', spacing.call_args.args[0][4])
            self.assertEqual(['normal', 'local_rescue', 'spacing_retry'],
                             [s['stage'] for s in data['recovery']['stages']])
            attempts = data['recovery']['stages'][1]['diagnostics']['candidate_recovery']['attempts']
            self.assertEqual(['3', '2', '1'], [a['candidate_id'] for a in attempts])
            self.assertTrue(data['validation']['valid'])
            self.assertNotIn('_recovery_candidates', json.dumps(data))
            self.assertEqual(original_cells, failure.retry_context[0].cell_of)


if __name__ == '__main__':
    unittest.main()
