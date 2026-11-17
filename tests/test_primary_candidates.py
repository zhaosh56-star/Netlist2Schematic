"""Preferred layouts are compared as finished drawings; reference is failure-only."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from netlist2schematic.flow import aesthetic_flow as flow, run_flow
from netlist2schematic.flow.candidate_budget import primary_deadline
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from tests.test_reserved_flow import candidate, valid


class PrimaryScheduleTests(unittest.TestCase):
    def invoke(self, outcomes, *, ids=(2, 3, 1), clock=None, **kwargs):
        generated = []
        def generate():
            for ident in ids:
                generated.append(ident)
                yield candidate(ident)
        clock = clock or SimpleNamespace(now=100.)
        with patch('netlist2schematic.layout.structured_placement.create_candidates', return_value=generate()) as producer, \
             patch.object(flow, 'final_route', side_effect=outcomes) as router, \
             patch.object(flow, 'perf_counter', side_effect=lambda: clock.now), redirect_stdout(io.StringIO()):
            output = flow.select_layout('unused.cir', passive_skeleton='auto', **kwargs)
        return output, generated, router, producer

    def test_both_successful_primary_candidates_never_initialize_the_reference(self):
        output, generated, router, producer = self.invoke([valid(True), valid()])
        self.assertEqual(generated, [2, 3])
        self.assertEqual([c.args[2].identifier for c in router.call_args_list], [2, 3])
        self.assertEqual(producer.call_args.kwargs['candidate_ids'], (2, 3, 1))
        self.assertEqual(output[4]['quality']['selected_candidate'], '3')
        self.assertFalse(output[4]['quality']['reference_fallback_used'])
        self.assertEqual(output[4]['quality']['candidate_policy'], 'primary_2_3_then_failure_reference_1')

    def test_one_success_is_enough_to_prevent_reference_initialization(self):
        failure = RoutingFailure('branch blocked', {'reason': 'no_route'})
        for outcomes, winner in (([valid(), failure], '2'), ([failure, valid()], '3')):
            result, generated, router, _ = self.invoke(outcomes)
            self.assertEqual(generated, [2, 3])
            self.assertEqual(result[4]['quality']['selected_candidate'], winner)
            self.assertEqual(router.call_count, 2)

    def test_only_two_failed_primary_routes_can_start_reference(self):
        failure = RoutingFailure('blocked', {'reason': 'no_route'})
        result, generated, router, _ = self.invoke([failure, failure, valid()])
        self.assertEqual(generated, [2, 3, 1])
        self.assertEqual([c.args[2].identifier for c in router.call_args_list], [2, 3, 1])
        self.assertEqual(result[4]['quality']['selected_candidate'], '1')
        self.assertTrue(result[4]['quality']['reference_fallback_used'])
        self.assertEqual([r['status'] for r in result[4]['quality']['candidates']], ['failed', 'failed', 'valid'])

    def test_insertion_failure_counts_as_failure_without_routing_partial_frame(self):
        skipped = candidate(2)
        skipped = (None, *skipped[1:4], dict(candidate_id=2, generation_skipped=True,
            passive_skeleton=dict(applied=False, stop_reason='no_legal_slot')))
        failure = RoutingFailure('blocked', {'reason': 'no_route'})
        with patch('netlist2schematic.layout.structured_placement.create_candidates',
                   return_value=iter([skipped, candidate(3), candidate(1)])), \
             patch.object(flow, 'final_route', side_effect=[failure, valid()]) as router, redirect_stdout(io.StringIO()):
            output = flow.select_layout('unused.cir', passive_skeleton='auto')
        self.assertEqual([c.args[2].identifier for c in router.call_args_list], [3, 1])
        self.assertEqual(output[4]['quality']['selected_candidate'], '1')

    def test_cumulative_slots_leave_fallback_time_without_resetting_total(self):
        clock = SimpleNamespace(now=100.)
        available = []
        def route(par, nets, pl, index, *, deadline, **kwargs):
            available.append(deadline-clock.now)
            if pl.identifier in (2, 3):
                clock.now = deadline
                raise RoutingFailure('slot exhausted', {'reason': 'quality_time_budget'})
            return valid()
        result, _, router, _ = self.invoke(route, clock=clock, quality_seconds=100)
        self.assertEqual([c.kwargs['deadline'] for c in router.call_args_list], [140., 180., 200.])
        self.assertEqual(available, [40., 40., 20.])
        self.assertEqual(result[4]['quality']['candidate_budget_policy'], 'primary_reserved')
        self.assertEqual([r['diagnostics']['reason'] for r in result[4]['quality']['candidates'][:2]],
                         ['candidate_time_budget', 'candidate_time_budget'])

    def test_all_failures_keep_the_last_reference_checkpoint_for_original_recovery(self):
        failures = [RoutingFailure(str(i), {'reason': 'no_route'}) for i in (2, 3, 1)]
        failures[-1]._rescue_context = {'failed_net': 'reference', 'done': ['safe']}
        with patch('netlist2schematic.layout.structured_placement.create_candidates',
                   return_value=iter([candidate(i) for i in (2, 3, 1)])), \
             patch.object(flow, 'final_route', side_effect=failures), redirect_stdout(io.StringIO()):
            with self.assertRaises(RoutingFailure) as caught:
                flow.select_layout('unused.cir', passive_skeleton='auto')
        self.assertEqual(caught.exception.retry_context[0].identifier, 1)
        self.assertEqual(caught.exception._rescue_context['failed_net'], 'reference')

    def test_explicit_new_candidate_three_needs_no_reference_or_feedback_prerequisites(self):
        output, _, router, producer = self.invoke([valid()], ids=(3,), candidate_id='3')
        self.assertEqual(router.call_count, 1)
        self.assertEqual(producer.call_args.kwargs['candidate_ids'], (3,))
        self.assertEqual(output[4]['quality']['selected_candidate'], '3')
        self.assertEqual(output[4]['quality']['candidate_budget_policy'], 'sequential')

    def test_deadline_helpers_do_not_change_old_reserved_schedule(self):
        from netlist2schematic.flow.candidate_budget import routing_deadline
        self.assertEqual([primary_deadline(10, 100, i) for i in (2, 3, 1)], [50, 90, 110])
        self.assertEqual([routing_deadline(10, 100, i, policy='reserved') for i in (1, 2, 3)], [80, 95, 110])


class DefaultPolicyTests(unittest.TestCase):
    def test_new_structured_layout_defaults_to_modules_and_saved_or_other_modes_do_not(self):
        resolve = run_flow._resolve_skeleton_policy
        self.assertEqual(resolve(None), 'auto')
        self.assertEqual(resolve(None, saved=True), 'off')
        self.assertEqual(resolve(None, 'baseline'), 'off')
        self.assertEqual(resolve(None, 'layered', 'legacy'), 'off')
        self.assertEqual(resolve(None, 'layered', 'structured', 'fixed'), 'off')
        self.assertEqual(resolve(None, 'layered', 'structured', 'flexible', 'auto'), 'off')
        self.assertEqual(resolve('off'), 'off')

    def test_cli_explicit_off_is_forwarded_instead_of_reenabling_the_default(self):
        with patch.object(run_flow, 'run') as execute:
            run_flow.main(['x.cir'])
            self.assertEqual(execute.call_args.kwargs['passive_skeleton'], 'auto')
            run_flow.main(['x.cir', '--passive-skeleton', 'off'])
            self.assertEqual(execute.call_args.kwargs['passive_skeleton'], 'off')

    def test_reordered_generator_preserves_candidate_seeds_and_poses(self):
        from netlist2schematic.layout import structured_placement as sp
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'test.cir'
            path.write_text('.model N NMOS\nM1 out inp 0 0 N\nR1 vdd out 1k\n.end\n', encoding='utf-8')
            old = {row[4]['candidate_id']: row for row in sp.create_candidates(path, iterations=0)}
            new = list(sp.create_candidates(path, iterations=0, candidate_ids=(2, 3, 1)))
        self.assertEqual([r[4]['candidate_id'] for r in new], [2, 3, 1])
        for row in new:
            before = old[row[4]['candidate_id']]
            for attr in ('cell_of', 'orient', 'row_of', 'column_x', 'row_y'):
                self.assertEqual(getattr(before[0], attr), getattr(row[0], attr))
            self.assertEqual(before[0].rng.getstate(), row[0].rng.getstate())

    def test_module_generation_timeout_does_not_close_the_next_candidate(self):
        from time import perf_counter
        from netlist2schematic.layout import structured_placement as sp
        from netlist2schematic.plugins.passive_skeleton import candidates
        actual = candidates.create_candidate
        seen = []
        def generate(*args, **kwargs):
            seen.append(args[9])
            if len(seen) == 1:
                raise RoutingFailure('generation slot exhausted', {'reason':'quality_time_budget'})
            return actual(*args, **kwargs)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'test.cir'
            path.write_text('.model N NMOS\nM1 out inp 0 0 N\nR1 vdd out 1k\n.end\n', encoding='utf-8')
            with patch.object(candidates,'create_candidate',side_effect=generate):
                rows = list(sp.create_candidates(path,iterations=0,passive_skeleton='auto',
                    candidate_ids=(2,3),candidate_deadlines={'2':perf_counter()+10,'3':perf_counter()+20}))
        self.assertIsNone(rows[0][0])
        self.assertEqual(rows[0][4]['passive_skeleton']['stop_reason'],'candidate_time_budget')
        self.assertIsNotNone(rows[1][0])
        self.assertEqual([r[4]['candidate_id'] for r in rows],[2,3])

    def test_real_default_flow_uses_two_modules_and_does_not_simplify_winner_twice(self):
        from netlist2schematic.postprocess.simplify_routes import simplify_routes
        from netlist2schematic.common.sa_saved import load_placement
        from netlist2schematic.routing.sa_routing import validate_final_routes
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'small.cir'
            source.write_bytes(b'.model N NMOS\r\nM1 out inp 0 0 N\r\nR1 vdd out 1k\r\n.end\r\n')
            original = source.read_bytes()
            with patch('netlist2schematic.postprocess.simplify_routes.simplify_routes', wraps=simplify_routes) as cleanup, \
                 redirect_stdout(io.StringIO()):
                data = run_flow.run(source, iterations=0, quality_seconds=10, fallback='off',
                    out_dir=Path(directory)/'result', simplify_seconds=.3)
            self.assertTrue(data['validation']['valid'])
            self.assertEqual(data['requested_passive_skeleton'], 'auto')
            self.assertEqual([r['candidate_id'] for r in data['quality']['candidates']], ['2', '3'])
            self.assertEqual(cleanup.call_count, 2)
            self.assertTrue(data['route_simplification']['verified_saved_replay'])
            self.assertNotIn('_candidate_cleanup', data)
            self.assertEqual(data['quality']['comparison_stage'], 'after_shared_route_simplification')
            self.assertFalse(data['quality']['reference_fallback_used'])
            self.assertLessEqual(data['quality']['candidate_cleanup']['considered'],512)
            self.assertEqual(source.read_bytes(), original)
            path = Path(directory)/'result/small-placement.json'
            saved, _, par, pl, comps, nets, index = load_placement(path)
            _, metrics, validation = validate_final_routes(par,nets,pl,index,list(saved['routes'].items()))
            self.assertTrue(validation['valid'])
            self.assertEqual(metrics, saved['metrics'])


class FinishedDrawingSelectionTests(unittest.TestCase):
    def test_primary_structure_gain_is_not_rejected_only_for_two_more_bends(self):
        from netlist2schematic.plugins.passive_skeleton.selection import select_result
        class Pose: pass
        old, new = Pose(), Pose()
        def result(pose, ident, score, bends):
            metrics = dict(wirelength=1000, crossings=0, bends=bends)
            return ((score, 0, 1000, ident), pose, [], {}, {}, {}, ([], metrics, {'valid':True}), {'score':score}, ident)
        rows = [result(old,'2',1,8), result(new,'3',1.1,10)]
        def organization(pl,*args):
            return dict(inline_error=0, family_error=0, regularity=dict(direction_errors=0,
                shape_error=10 if pl is old else 0, access_distance=0))
        with patch('netlist2schematic.plugins.passive_skeleton.selection.measure', side_effect=organization):
            winner, report = select_result(rows, {'version':'fixture'}, primary=True)
            self.assertIs(winner, rows[1])
            self.assertEqual(report['bends_policy'], 'soft_quality_cost')
            legacy, _ = select_result(rows, {'version':'fixture'})
            self.assertIs(legacy, rows[0])

    def test_selection_cleanup_shares_budget_and_does_not_invoke_layout_or_router(self):
        clock = SimpleNamespace(now=10.)
        rows = []
        for ident in ('2','3'):
            pl, comps, nets, index, metadata = candidate(int(ident))
            quality = dict(score=4, per_net={})
            rows.append(((4,0,200,ident),pl,comps,nets,index,metadata,valid(True),quality,ident))
        seen = []
        def clean(data,par,pl,comps,nets,index,routed,options,policy,seconds,**kwargs):
            seen.append(seconds)
            clock.now += seconds
            data['quality'] = {'details':dict(score=3,per_net={})}
            data['route_simplification'] = dict(elapsed_seconds=seconds)
            return data,valid()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'source.cir';source.write_text('* fixture\n',encoding='utf-8')
            with patch.object(flow,'perf_counter',side_effect=lambda:clock.now), \
                 patch.object(run_flow,'_saved_positions',return_value={}), \
                 patch.object(run_flow,'_postprocess_routes',side_effect=clean), \
                 patch.object(flow,'final_route',side_effect=AssertionError('cleanup must not search')):
                cleaned, report = flow._clean_primary_results(source,None,None,rows,[],None,'safe',5,'strict')
        self.assertEqual(seen,[2.5,2.5])
        self.assertEqual(report['elapsed_seconds'],5)
        self.assertTrue(all(r[6][1]['wirelength']==100 for r in cleaned))
        self.assertEqual([r[8] for r in cleaned],['2','3'])
        self.assertEqual(rows[0][6][1]['wirelength'],200,'original snapshots are untouched')


if __name__ == '__main__':
    unittest.main()
