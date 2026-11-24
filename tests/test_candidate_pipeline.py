"""Each candidate owns 120/30/30/30 seconds before cross-candidate selection."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from netlist2schematic.flow import candidate_pipeline as pipeline, run_flow, aesthetic_flow
from netlist2schematic.flow.diagram_budget import DiagramBudget
from netlist2schematic.recovery import primary_recovery
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from tests.test_candidate_rescue import snapshot
from tests.test_reserved_flow import candidate, valid


def result(ident, *, bends=0, crossings=0, wirelength=100):
    context = candidate(int(ident))
    context[4]['quality'] = dict(score=1, details=dict(score=1))
    routes, metrics, checked = valid()
    metrics.update(bends=bends, crossings=crossings, wirelength=wirelength)
    return *context, (routes, metrics, checked)


class IndependentCandidateTests(unittest.TestCase):
    def setUp(self):
        self.clock = SimpleNamespace(now=0.)
        self.events = []
        self.stages = []
        self.originals = []
        self.controls = {}

    def failed(self, ident):
        row = snapshot(int(ident))
        self.originals.append(deepcopy(row['context'][4]))
        failure = RoutingFailure('controlled branch failure', {'reason': 'no_path'})
        failure._primary_placements = [row]
        return failure

    def normal(self, ident, seconds):
        self.events.append((ident, 'normal', seconds, self.clock.now))
        self.clock.now += seconds
        outcome = self.controls.get((ident, 'normal'))
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is not None:
            return deepcopy(outcome)
        raise self.failed(ident)

    def repair(self, rows, *, deadline, candidate_ids, budget_source):
        ident = candidate_ids[0]
        self.events.append((ident, 'primary_repair', deadline-self.clock.now, self.clock.now))
        self.clock.now = deadline
        self.assertEqual({row['candidate_id'] for row in rows}, {ident})
        self.assertEqual(budget_source, 'independent_candidate_stage')
        outcome = self.controls.get((ident, 'primary_repair'))
        if isinstance(outcome, BaseException):
            raise outcome
        return deepcopy(outcome), dict(status='valid' if outcome else 'failed'), rows

    def rescue(self, rows, policy, seconds, stages):
        ids = {row['candidate_id'] for row in rows}
        self.assertEqual(len(ids), 1)
        ident = next(iter(ids))
        self.events.append((ident, 'local_rescue', seconds, self.clock.now))
        self.clock.now += seconds
        outcome = self.controls.get((ident, 'local_rescue'))
        if isinstance(outcome, BaseException):
            raise outcome
        return deepcopy(outcome)

    def spacing(self, context, *, seconds, deadline, **kwargs):
        ident = str(context[0].identifier)
        self.events.append((ident, 'spacing_retry', seconds, self.clock.now))
        self.assertEqual(kwargs['scale'], 1.5)
        self.assertEqual(deadline, self.clock.now+seconds)
        self.clock.now += seconds
        outcome = self.controls.get((ident, 'spacing_retry'))
        if outcome is not None:
            return deepcopy(outcome)
        raise RoutingFailure('expanded route still blocked', {'reason': 'no_path'})

    def bus(self, source, cell, seed, options, seconds, stages, **kwargs):
        self.events.append(('bus', 'bus_fallback', seconds, self.clock.now))
        self.clock.now += seconds
        outcome = result(1)
        outcome[4]['routing_stage'] = 'bus_fallback'
        return outcome

    def run_schedule(self, **kwargs):
        budget = DiagramBudget(kwargs.pop('wall', 720), clock=lambda: self.clock.now)
        with ExitStack() as stack:
            stack.enter_context(patch.object(pipeline, 'perf_counter', side_effect=lambda: self.clock.now))
            stack.enter_context(patch.object(primary_recovery, 'repair', side_effect=self.repair))
            stack.enter_context(patch.object(primary_recovery, '_spacing_allowed', return_value=True))
            stack.enter_context(patch.object(run_flow, '_try_candidate_rescues', side_effect=self.rescue))
            stack.enter_context(patch('netlist2schematic.recovery.spacing_retry.retry_spacing', side_effect=self.spacing))
            stack.enter_context(patch.object(run_flow, '_bus_result', side_effect=self.bus))
            stack.enter_context(redirect_stdout(StringIO()))
            checked = kwargs.pop('check_source', lambda: None)
            outcome = pipeline.run_candidates('unused.cir', None, self.normal, budget=budget,
                stages=self.stages, check_source=checked, cell=None, seed=17,
                options=None, simplify_policy='off', **kwargs)
        json.dumps(outcome[4])
        return outcome, budget

    def test_full_independent_caps_and_reference_only_after_both_finished(self):
        self.controls['1', 'normal'] = result(1)
        outcome, budget = self.run_schedule()
        self.assertEqual([(i, stage) for i, stage, _, _ in self.events], [
            ('2', 'normal'), ('2', 'primary_repair'), ('2', 'local_rescue'), ('2', 'spacing_retry'),
            ('3', 'normal'), ('3', 'primary_repair'), ('3', 'local_rescue'), ('3', 'spacing_retry'),
            ('1', 'normal')])
        self.assertEqual([e[2] for e in self.events], [120, 30, 30, 30, 120, 30, 30, 30, 120])
        self.assertEqual([e[3] for e in self.events if e[1] == 'normal'], [0, 210, 420])
        self.assertEqual(outcome[4]['quality']['selected_candidate'], '1')
        self.assertTrue(outcome[4]['quality']['reference_fallback_used'])
        self.assertEqual(budget.snapshot()['elapsed_seconds'], 540)

    def test_bus_only_after_reference_own_recovery_and_independent_60_seconds(self):
        outcome, _ = self.run_schedule()
        self.assertEqual(self.events[-1], ('bus', 'bus_fallback', 60, 630))
        self.assertEqual(self.clock.now, 690)
        self.assertEqual(outcome[4]['candidate_pipeline']['candidate_order'], ['2', '3', '1'])
        self.assertIsNone(outcome[4]['candidate_pipeline']['selected_candidate'])

    def test_successful_first_candidate_does_not_skip_candidate_three(self):
        self.controls['2', 'normal'] = result(2, bends=3, crossings=0, wirelength=100)
        self.controls['3', 'normal'] = result(3, bends=2, crossings=10, wirelength=300)
        outcome, _ = self.run_schedule()
        self.assertEqual([(i, s) for i, s, _, _ in self.events], [('2', 'normal'), ('3', 'normal')])
        self.assertEqual(outcome[4]['quality']['selected_candidate'], '3')
        self.assertFalse(outcome[4]['quality']['reference_fallback_used'])

    def test_early_completion_does_not_change_the_next_candidates_120_second_cap(self):
        def quick(ident, seconds):
            self.events.append((ident, 'normal', seconds, self.clock.now))
            self.clock.now += 2
            return result(ident)
        self.normal = quick
        outcome, _ = self.run_schedule()
        self.assertEqual(self.events, [('2', 'normal', 120, 0), ('3', 'normal', 120, 2)])
        self.assertEqual(self.clock.now, 4)
        self.assertEqual(outcome[4]['quality']['selected_candidate'], '2')

    def test_repaired_first_candidate_kept_while_other_is_fully_attempted(self):
        self.controls['2', 'primary_repair'] = result(2, bends=1)
        outcome, _ = self.run_schedule()
        self.assertEqual(outcome[4]['quality']['selected_candidate'], '2')
        self.assertEqual([(i, s) for i, s, _, _ in self.events], [
            ('2', 'normal'), ('2', 'primary_repair'), ('3', 'normal'),
            ('3', 'primary_repair'), ('3', 'local_rescue'), ('3', 'spacing_retry')])

    def test_local_or_spacing_success_skips_remaining_stages_of_that_candidate(self):
        for stage in ('local_rescue', 'spacing_retry'):
            with self.subTest(stage=stage):
                self.setUp()
                self.controls['2', stage] = result(2)
                self.controls['3', 'normal'] = result(3, bends=1)
                outcome, _ = self.run_schedule()
                self.assertEqual(outcome[4]['quality']['selected_candidate'], '2')
                attempted = [s for i, s, _, _ in self.events if i == '2']
                self.assertEqual(attempted[-1], stage)
                self.assertNotIn('1', outcome[4]['candidate_pipeline']['candidate_order'])

    def test_stage_routing_exception_still_allows_later_recovery(self):
        for stage in ('primary_repair', 'local_rescue'):
            with self.subTest(stage=stage):
                self.setUp()
                self.controls['2', stage] = RoutingFailure('timed out', {'reason': 'quality_time_budget'})
                self.controls['2', 'spacing_retry'] = result(2)
                self.controls['3', 'normal'] = result(3, bends=2)
                outcome, _ = self.run_schedule()
                self.assertEqual(outcome[4]['quality']['selected_candidate'], '2')

    def test_unverified_recovery_is_rejected_and_later_recovery_runs(self):
        bad = result(2)
        bad[5][2]['valid'] = False
        self.controls['2', 'primary_repair'] = bad
        self.controls['2', 'local_rescue'] = result(2)
        self.controls['3', 'normal'] = result(3, bends=3)
        outcome, _ = self.run_schedule()
        self.assertEqual(outcome[4]['quality']['selected_candidate'], '2')
        self.assertIn(('2', 'local_rescue'), [(i, s) for i, s, _, _ in self.events])

    def test_wrong_candidate_or_input_error_is_not_a_recovery_failure(self):
        self.controls['2', 'normal'] = result(3)
        with self.assertRaisesRegex(ValueError, 'different candidate'):
            self.run_schedule()
        self.assertEqual(len(self.events), 1)
        self.setUp()
        self.controls['2', 'normal'] = ValueError('invalid netlist')
        with self.assertRaisesRegex(ValueError, 'invalid netlist'):
            self.run_schedule()
        self.assertEqual(len(self.events), 1)

    def test_source_change_after_normal_prevents_all_later_work(self):
        calls = []
        def changed():
            calls.append(1)
            if len(calls) > 1:
                raise ValueError('source changed')
        with self.assertRaisesRegex(ValueError, 'source changed'):
            self.run_schedule(check_source=changed)
        self.assertEqual(len(self.events), 1)

    def test_outer_limit_is_only_an_explicit_clamp_and_does_not_reset(self):
        with self.assertRaises(RoutingFailure):
            self.run_schedule(wall=150)
        self.assertEqual(self.events[:2], [('2', 'normal', 120, 0), ('2', 'primary_repair', 20, 120)])
        self.assertEqual(self.clock.now, 140)
        self.assertFalse(any(i == 'bus' for i, _, _, _ in self.events))

    def test_disabled_fallback_never_runs_reference_or_bus(self):
        with self.assertRaises(RoutingFailure):
            self.run_schedule(fallback_policy='off', rescue_policy='off')
        self.assertEqual([(i, s) for i, s, _, _ in self.events], [
            ('2', 'normal'), ('2', 'primary_repair'), ('3', 'normal'), ('3', 'primary_repair')])


class CaptureSingleCandidateTests(unittest.TestCase):
    def test_independent_replay_has_full_deadline_and_matching_failure_snapshot(self):
        from netlist2schematic.layout import structured_placement
        for ident in (1, 2, 3):
            with self.subTest(candidate=ident):
                row = snapshot(ident)
                failure = RoutingFailure('controlled route failure', {'reason': 'no_path'})
                failure._rescue_context = row['checkpoint']
                with patch.object(structured_placement, 'create_candidates', return_value=iter([candidate(ident)])) as generate, \
                     patch.object(aesthetic_flow, 'final_route', side_effect=failure) as route, \
                     patch.object(aesthetic_flow, 'perf_counter', return_value=100.), redirect_stdout(StringIO()):
                    with self.assertRaises(RoutingFailure) as caught:
                        aesthetic_flow.select_layout('unused.cir', candidate_id=str(ident),
                            passive_skeleton='auto', quality_seconds=120, capture_failed_candidate=True)
                self.assertEqual(generate.call_args.kwargs['candidate_ids'], (ident,))
                self.assertIn('packing_failures', generate.call_args.kwargs)
                self.assertEqual(route.call_args.kwargs['deadline'], 220.)
                self.assertEqual([r['candidate_id'] for r in caught.exception._primary_placements], [str(ident)])

    def test_packing_resume_is_captured_for_an_independent_candidate(self):
        from netlist2schematic.layout import structured_placement
        frame = candidate(2)
        skipped = (None, *frame[1:4], dict(candidate_id=2, generation_skipped=True,
                    passive_skeleton=dict(applied=False, stop_reason='no_slot')))
        def generate(*args, **kwargs):
            kwargs['packing_failures'].append(dict(candidate_id='2', kind='packing',
                context=None, checkpoint=None, resume=lambda until: frame,
                options=None, attempt_index=0))
            yield skipped
        with patch.object(structured_placement, 'create_candidates', side_effect=generate), \
             patch.object(aesthetic_flow, 'final_route', side_effect=AssertionError('partial layout routed')), \
             redirect_stdout(StringIO()):
            with self.assertRaises(RoutingFailure) as caught:
                aesthetic_flow.select_layout('unused.cir', candidate_id='2',
                    passive_skeleton='auto', capture_failed_candidate=True)
        rows = caught.exception._primary_placements
        self.assertEqual(len(rows), 1)
        self.assertTrue(callable(rows[0]['resume']))
        json.dumps(caught.exception.diagnostics)

    def test_candidate_one_can_use_real_primary_repair_without_restarting_sa(self):
        from time import perf_counter
        row = dict(candidate_id='1', context=candidate(1), checkpoint=None,
                   options=None, attempt_index=0)
        with patch.object(primary_recovery, 'final_route', return_value=valid()), \
             patch('netlist2schematic.layout.layout_quality.evaluate_quality', return_value=dict(score=1)), \
             patch('netlist2schematic.layout.structured_placement._anneal', side_effect=AssertionError('SA restarted')):
            repaired, report, _ = primary_recovery.repair([row], deadline=perf_counter()+2,
                                                         candidate_ids=('1',))
        self.assertEqual(repaired[4]['quality']['selected_candidate'], '1')
        self.assertEqual(report['candidate_order'], ['1'])


if __name__ == '__main__':
    unittest.main()
