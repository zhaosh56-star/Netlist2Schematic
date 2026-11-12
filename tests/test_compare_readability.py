"""Stage comparisons preserve input identity and the new crossing policy."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('compare_readability_tests',
    Path(__file__).resolve().parents[1]/'tools/compare_readability.py')
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


def parameters(stage=1):
    return dict(comparison.common.DEFAULT_PARAMETERS, stage=stage,
                candidate_budget='sequential', presentation='clean', candidate_id=None)


def verified(length=100, crossings=0, routing_stage='normal'):
    return {'metrics': {'wirelength': length, 'crossings': crossings, 'bends': 4},
        'routing_stage': routing_stage, 'tradeoff_audit': {'valid': True},
        'placement': {'device': {'row': 1, 'col': 2, 'orient': 0}},
        'measurement_plan_signature': 'source-derived-plan',
        'readability_plan_signature': 'source-derived-plan',
        'clean_svg': str(comparison.ROOT/'output/example-clean.svg'),
        'width': 200, 'height': 300}


class ReadabilityComparisonTests(unittest.TestCase):
    def test_stage_one_command_only_simplifies_saved_geometry(self):
        command = comparison.command_for({'policy': 'auto', 'parameters': parameters(),
            'placement_snapshot': 'saved.json', 'source_netlist': 'source.cir', 'cell': 'dut'}, Path('out'))
        self.assertEqual('saved.json', command[command.index('--simplify-placement')+1])
        self.assertEqual('balanced', command[command.index('--route-tradeoff')+1])
        for prohibited in ('--iterations', '--fallback', '--readability-layout', '--candidate-id', 'source.cir'):
            self.assertNotIn(prohibited, command)

    def test_later_stage_commands_have_matching_budgets_and_isolated_layout_flags(self):
        p = parameters(4); p['candidate_id'] = 2
        for policy in ('off', 'auto'):
            command = comparison.command_for({'policy': policy, 'parameters': p,
                'source_netlist': 'source.cir', 'cell': 'dut'}, Path('out'))
            expected = {'--readability-layout': policy, '--readability-phase': '4',
                        '--route-tradeoff': 'strict' if policy == 'off' else 'balanced',
                        '--region-layout': 'off', '--structure-layout': 'off',
                        '--symmetry-layout': 'off', '--iterations': '20000',
                        '--quality-seconds': '300', '--candidate-id': '2',
                        '--passive-layout': 'flexible', '--cell': 'dut'}
            for flag, value in expected.items():
                self.assertEqual(value, command[command.index(flag)+1])

    def test_new_crossing_gate_allows_two_even_for_zero_crossing_reference(self):
        self.assertTrue(all(comparison.gates(verified(), verified(115, 2), 1).values()))
        self.assertFalse(comparison.gates(verified(), verified(115, 3), 1)['crossings_within_off_plus_two'])
        self.assertFalse(comparison.gates(verified(), verified(115.01), 1)['wirelength_115_percent'])

    def test_each_result_must_pass_its_own_immutable_credit_audit(self):
        new = verified(); new['tradeoff_audit']['valid'] = False
        self.assertFalse(comparison.gates(verified(), new, 2)['tradeoff_audits_passed'])

    def test_stage_one_checks_position_and_real_source_plan(self):
        new = verified(); new['placement']['device']['col'] = 3
        self.assertFalse(comparison.gates(verified(), new, 1)['saved_placement_unchanged'])
        new.pop('measurement_plan_signature')
        self.assertFalse(comparison.gates(verified(), new, 1)['same_source_plan'])
        self.assertNotIn('saved_placement_unchanged', comparison.gates(verified(), new, 2))

    def test_no_additional_bus_fallback(self):
        self.assertFalse(comparison.gates(verified(), verified(routing_stage='bus_fallback'), 3)['no_new_bus_fallback'])

    def test_stage_one_missing_snapshot_is_not_substituted_by_a_new_flow(self):
        signature = {'parameters': parameters(), 'source_files': {}}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(comparison.common, '_signature', return_value=signature), \
                patch.object(comparison.subprocess, 'Popen', side_effect=AssertionError('no fallback run')):
            row = comparison.execute({'case': '6'}, 1, 'auto', Path(folder), parameters(), {}, {}, {})
        self.assertEqual('not_run', row['status'])
        self.assertEqual('saved_off_snapshot_unavailable', row['reason'])

    def test_snapshot_lookup_requires_matching_seed_and_valid_off(self):
        records = {'cases': [
            {'case': '6', 'seed': 1, 'results': {'off': {'status': 'valid', 'verified': {'placement_json': 'six.json'}}}},
            {'case': '6', 'seed': 2, 'results': {'off': {'status': 'failed'}, 'auto': {'status': 'valid', 'verified': {'placement_json': 'wrong.json'}}}}]}
        with patch.object(comparison, 'read_json', return_value=records):
            result = comparison.saved_baselines(Path('comparison.json'))
        self.assertIn(('6', 1), result)
        self.assertNotIn(('6', 2), result)

    def test_only_real_candidate_ids_are_retained_and_duplicates_merged(self):
        data = {'candidate_selection': {'candidates': [
            {'candidate_id': 2, 'status': 'valid', 'metric': 1},
            {'candidate_id': 2, 'status': 'valid'},
            {'candidate_id': 3, 'status': 'failed'},
            {'candidate_id': None, 'status': 'valid', 'energy': 2},
            {'status': 'valid', 'energy': 1}]}}
        result = comparison.actual_candidates(data)
        self.assertEqual(2, len(result))
        self.assertEqual({2, 3}, {row['candidate_id'] for row in result})
        self.assertEqual(1, next(row['metric'] for row in result if row['candidate_id'] == 2))

    def test_tradeoff_audit_catches_wrong_baseline_and_cumulative_overdraft(self):
        data = {'metrics': {'crossings': 4}, 'route_simplification': {
            'before_metrics': {'crossings': 2}, 'tradeoff': {'policy': 'balanced', 'baseline_crossings': 2}}}
        self.assertTrue(comparison.tradeoff_audit(data)['valid'])
        data['metrics']['crossings'] = 5
        self.assertFalse(comparison.tradeoff_audit(data)['valid'])
        data['metrics']['crossings'] = 4
        data['route_simplification']['tradeoff']['baseline_crossings'] = 3
        self.assertFalse(comparison.tradeoff_audit(data)['valid'])

    def test_tradeoff_audit_rejects_reported_crowded_new_intersection(self):
        data = {'metrics': {'crossings': 1}, 'route_simplification': {
            'before_metrics': {'crossings': 0}, 'tradeoff': {'policy': 'balanced', 'baseline_crossings': 0},
            'accepted_changes': [{'tradeoff': {'accepted': True, 'cumulative_crossing_increase': 1,
                'density': {'conflict': None, 'required_distance': 24, 'nearest_distance': 23}}}]}}
        self.assertFalse(comparison.tradeoff_audit(data)['valid'])

    def test_totals_can_increase_without_using_the_old_aggregate_gate(self):
        records = {('x', 1, p): {'status': 'valid', 'signature': {'source_files': {}},
                               'verified': verified(crossings=0 if p == 'off' else 1)}
                   for p in comparison.POLICIES}
        with tempfile.TemporaryDirectory() as folder:
            report = comparison.publish(Path(folder), [({'case': 'x'}, 1)], records, parameters(), {}, {})
        self.assertTrue(report['quantitative_gates_passed'])
        self.assertNotIn('total_crossings_nonincreasing', report['release_gates'])
        self.assertEqual(1, report['totals']['auto']['crossings'])
        self.assertFalse(report['default_promotion_approved'])

    def test_all_skipped_and_missing_runs_are_not_release_success(self):
        for status in ('skipped', 'not_run'):
            records = {('x', 1, p): {'status': status, 'reason': 'input_validation_error'}
                       for p in comparison.POLICIES}
            with tempfile.TemporaryDirectory() as folder:
                report = comparison.publish(Path(folder), [({'case': 'x'}, 1)], records, parameters(), {}, {})
            self.assertFalse(report['quantitative_gates_passed'])

    def test_different_input_snapshots_are_not_a_comparison(self):
        records = {('x', 1, p): {'status': 'valid', 'signature': {'source_files': {'snapshot': p}},
                               'verified': verified()} for p in comparison.POLICIES}
        with tempfile.TemporaryDirectory() as folder:
            report = comparison.publish(Path(folder), [({'case': 'x'}, 1)], records, parameters(), {}, {})
        self.assertFalse(report['release_gates']['paired_sources_identical'])


if __name__ == '__main__':
    unittest.main()
