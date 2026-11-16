"""Only proven, unrepaired static faults may skip an expensive route attempt."""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import io
import json
from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from netlist2schematic.flow import aesthetic_flow
from netlist2schematic.plugins.preflight import placement_preflight as preflight
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.routing_rules import RoutingOptions
from test_aesthetic_flow import candidate, route_result
from test_placement_preflight import fixture, two_fault_fixture, PAR


def proof(valid, *, count=0, complete=True):
    return dict(static_verification=dict(complete=complete, valid=valid,
        fault_count=count, violations=[dict(kind='marker', nets=['signal', '0'])]
        if count else []))


@contextmanager
def selection(items, reports, routes, *, repair_effect=None):
    def repair(par, pl, *args, **kwargs):
        return pl, deepcopy(reports[pl.identifier])
    with patch('netlist2schematic.layout.structured_placement.create_candidates',
               return_value=iter(items)), \
         patch('netlist2schematic.plugins.preflight.placement_preflight.repair_static_ports',
               side_effect=repair_effect or repair) as repaired, \
         patch('netlist2schematic.flow.aesthetic_flow.final_route',
               side_effect=routes) as routed, redirect_stdout(io.StringIO()):
        yield SimpleNamespace(repaired=repaired, routed=routed,
            select=lambda **kwargs: aesthetic_flow.select_layout(
                'unused.cir', iterations=0, **kwargs))


class StaticVerificationTests(unittest.TestCase):
    def repair(self, case, **kwargs):
        return preflight.repair_static_ports(PAR, *case, RoutingOptions(), **kwargs)

    def test_safe_identity_has_complete_proof_without_copy_or_trial(self):
        case = fixture(safe=True)
        with patch.object(preflight, 'deepcopy', side_effect=AssertionError('copied')):
            pl, report = self.repair(case)
        self.assertIs(pl, case[0])
        self.assertEqual(report['trials'], 0)
        self.assertEqual(report['static_verification'], dict(complete=True,
            valid=True, fault_count=0, scope='fixed_port_body_and_marker_geometry',
            basis='already_safe'))

    def test_real_repair_proves_final_scene_not_original_faults(self):
        case = fixture()
        pl, report = self.repair(case)
        self.assertIsNot(pl, case[0])
        self.assertGreater(report['original_static_fault_count'], 0)
        self.assertEqual(report['static_verification']['fault_count'], 0)
        self.assertIs(report['static_verification']['valid'], True)
        self.assertTrue(preflight._static_safe(PAR, pl, case[2], case[3],
                                              RoutingOptions(), None))

    def test_unrepairable_fixed_passive_proves_returned_original_faults(self):
        case = fixture()
        case[0].fixed.add('passive')
        before = deepcopy(case[0].__dict__)
        pl, report = self.repair(case)
        self.assertIs(pl, case[0])
        self.assertEqual(before, pl.__dict__)
        check = report['static_verification']
        self.assertIs(check['valid'], False)
        self.assertTrue(check['complete'])
        self.assertEqual(check['basis'], 'original_restored')
        self.assertEqual(check['fault_count'], report['original_static_fault_count'])
        self.assertIn('marker_body_halo', {v['kind'] for v in check['violations']})
        json.dumps(report)

    def test_private_partial_repair_is_not_reported_as_returned_geometry(self):
        case = two_fault_fixture()
        case[0].fixed.add(case[3]['passive_b'])
        pl, report = self.repair(case)
        self.assertIs(pl, case[0])
        self.assertEqual(report['partial_improvements'], 1)
        self.assertLess(report['best_partial_fault_count'],
                        report['static_verification']['fault_count'])
        self.assertEqual(report['actions'], [])
        self.assertEqual(report['static_verification']['fault_count'],
                         preflight._static_faults(PAR, pl, case[2], case[3],
                                                  RoutingOptions(), None)['count'])

    def test_budget_exits_before_full_count_remain_unknown(self):
        cases = [dict(seconds=0), dict(deadline=perf_counter()-1),
                 dict(max_trials=0)]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                case = fixture()
                pl, report = self.repair(case, **kwargs)
                self.assertIs(pl, case[0])
                self.assertFalse(report['static_verification']['complete'])
                self.assertIsNone(report['static_verification']['valid'])

    def test_custom_power_connections_without_complete_count_remain_unknown(self):
        case = fixture()
        case[0].power_connections = []
        # Isolate the early custom-geometry branch; malformed records are
        # independently required to propagate instead of becoming a veto.
        with patch.object(preflight, '_static_safe', return_value=False):
            pl, report = self.repair(case)
        self.assertIs(pl, case[0])
        self.assertEqual(report['stop_reason'], 'custom_power_connections')
        self.assertFalse(report['static_verification']['complete'])

    def test_timeout_before_count_is_unknown_after_count_restores_negative_proof(self):
        case = fixture()
        timeout = RoutingFailure('deadline', {'reason': 'quality_time_budget'})
        with patch.object(preflight, '_static_safe', side_effect=timeout):
            pl, report = self.repair(case)
        self.assertFalse(report['static_verification']['complete'])
        with patch.object(preflight, '_static_safe', side_effect=[False, timeout]):
            pl, report = self.repair(case)
        self.assertIs(pl, case[0])
        self.assertEqual(report['stop_reason'], 'time_budget')
        self.assertTrue(report['static_verification']['complete'])
        self.assertIs(report['static_verification']['valid'], False)
        self.assertGreater(report['static_verification']['fault_count'], 0)

    def test_proposal_limit_after_count_preserves_negative_proof(self):
        case = fixture()
        faults = preflight._static_faults(PAR, case[0], case[2], case[3],
                                          RoutingOptions(), None)
        with patch.object(preflight, '_static_safe', return_value=False), \
             patch.object(preflight, '_static_faults', return_value=faults):
            pl, report = self.repair(case, max_trials=1)
        self.assertIs(pl, case[0])
        self.assertEqual(report['stop_reason'], 'trial_limit')
        self.assertEqual(report['proposals'], 1)
        self.assertIs(report['static_verification']['valid'], False)

    def test_bool_only_oracle_failure_with_zero_full_faults_is_unknown(self):
        case = fixture(safe=True)
        case[0].fixed.add('passive')
        with patch.object(preflight, '_static_safe', return_value=False):
            pl, report = self.repair(case)
        self.assertEqual(report['original_static_fault_count'], 0)
        self.assertFalse(report['static_verification']['complete'])
        self.assertIsNone(report['static_verification']['valid'])


class PreflightRoutingGateTests(unittest.TestCase):
    def test_proven_fault_skips_router_and_has_no_fabricated_checkpoint(self):
        with selection([candidate(2)], {2: proof(False, count=2)}, []) as s:
            with self.assertRaises(RoutingFailure) as caught:
                s.select(port_preflight='repair')
        s.routed.assert_not_called()
        exc = caught.exception
        record = exc.diagnostics['candidates'][0]
        self.assertEqual(record['diagnostics']['reason'], 'static_port_conflict')
        self.assertTrue(record['diagnostics']['routing_skipped'])
        self.assertNotIn('metrics', record)
        self.assertIsNone(exc.retry_context)
        self.assertFalse(hasattr(exc, '_rescue_context'))

    def test_unknown_or_zero_fault_proofs_do_not_veto_search(self):
        reports = [proof(None, complete=False), proof(False, count=0),
                   proof(None, count=2), dict(original_static_invalid=True),
                   proof(False, count=2, complete=False)]
        for report in reports:
            with self.subTest(report=report):
                with selection([candidate(2)], {2: report}, [route_result()]) as s:
                    chosen = s.select(port_preflight='repair')
                self.assertEqual(chosen[0].identifier, 2)
                self.assertEqual(s.routed.call_count, 1)

    def test_fixed_original_repaired_safe_still_routes_normally(self):
        report = dict(proof(True), original_static_fault_count=3, applied=True)
        with selection([candidate(2)], {2: report}, [route_result()]) as s:
            chosen = s.select(port_preflight='repair')
        self.assertEqual(s.routed.call_count, 1)
        self.assertTrue(chosen[5][2]['valid'])

    def test_off_ignores_saved_negative_proof_and_never_runs_preflight(self):
        item = list(candidate(1))
        item[4]['port_preflight'] = proof(False, count=2)
        with selection([tuple(item)], {}, [route_result()],
                repair_effect=AssertionError('disabled preflight called')) as s:
            chosen = s.select(port_preflight='off')
        s.repaired.assert_not_called()
        self.assertEqual(s.routed.call_count, 1)
        self.assertEqual(chosen[5], route_result())

    def test_vetoed_candidates_do_not_discard_valid_incumbent(self):
        reports = {1: proof(False, count=1), 2: proof(True), 3: proof(False, count=2)}
        with selection([candidate(i) for i in (1, 2, 3)], reports,
                       [route_result()]) as s:
            chosen = s.select(port_preflight='repair')
        self.assertEqual(chosen[0].identifier, 2)
        self.assertEqual([c.args[2].identifier for c in s.routed.call_args_list], [2])
        self.assertEqual([r['status'] for r in chosen[4]['quality']['candidates']],
                         ['failed', 'valid', 'failed'])

    def test_last_actual_failed_route_keeps_its_rescue_and_spacing_context(self):
        first = RoutingFailure('first route failed', {'reason': 'no_path'})
        second = RoutingFailure('second route failed', {'reason': 'no_safe_goal'})
        first._rescue_context = dict(candidate=1, completed=['old'])
        second._rescue_context = dict(candidate=2, completed=['retained'])
        reports = {1: proof(True), 2: proof(True), 3: proof(False, count=2)}
        with selection([candidate(i) for i in (1, 2, 3)], reports,
                       [first, second]) as s:
            with self.assertRaises(RoutingFailure) as caught:
                s.select(port_preflight='repair')
        exc = caught.exception
        self.assertEqual([c.args[2].identifier for c in s.routed.call_args_list], [1, 2])
        self.assertEqual(exc.retry_context[0].identifier, 2)
        self.assertEqual(exc._rescue_context, second._rescue_context)
        self.assertIsNot(exc._rescue_context, second._rescue_context)
        self.assertIsInstance(exc.retry_options, RoutingOptions)

    def test_reserved_cutoffs_remain_absolute_and_skipping_does_not_reset_budget(self):
        clock = SimpleNamespace(now=100.)
        def repair(par, pl, *args, **kwargs):
            clock.now += .1
            return pl, proof(False, count=1) if pl.identifier == 1 else proof(True)
        reports = {1: proof(False, count=1), 2: proof(True), 3: proof(True)}
        with patch.object(aesthetic_flow, 'perf_counter', side_effect=lambda: clock.now), \
             selection([candidate(i) for i in (1, 2, 3)], reports,
                       [route_result(), route_result()], repair_effect=repair) as s:
            chosen = s.select(port_preflight='repair', candidate_budget='reserved',
                              quality_seconds=10)
        self.assertEqual([c.kwargs['deadline'] for c in s.routed.call_args_list],
                         [108.5, 110.])
        self.assertEqual([r['candidate_budget']['cutoff_seconds']
                          for r in chosen[4]['quality']['candidates']], [7., 8.5, 10.])

    def test_explicit_vetoed_replay_has_no_other_candidate_recovery(self):
        with selection([candidate(1), candidate(2)], {2: proof(False, count=2)}, []) as s:
            with self.assertRaises(RoutingFailure) as caught:
                s.select(port_preflight='repair', candidate_id='2')
        self.assertEqual(caught.exception.diagnostics['reason'], 'replay_complete')
        self.assertEqual(caught.exception.diagnostics['requested_candidate'], '2')
        self.assertIsNone(caught.exception.retry_context)
        s.routed.assert_not_called()

    def test_resource_error_during_preflight_is_not_hidden_by_a_valid_incumbent(self):
        def repair(par, pl, *args, **kwargs):
            if pl.identifier == 2:
                raise ValueError('malformed symbol geometry')
            return pl, proof(True)
        with selection([candidate(1), candidate(2)], {}, [route_result()],
                       repair_effect=repair) as s:
            with self.assertRaisesRegex(ValueError, 'malformed symbol geometry'):
                s.select(port_preflight='repair')
        self.assertEqual(s.routed.call_count, 1)


if __name__ == '__main__':
    unittest.main()
