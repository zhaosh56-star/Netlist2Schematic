"""The attachment experiment keeps policy, budget and evidence boundaries."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from random import Random
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('compare_attachment_integration_tests',
    Path(__file__).resolve().parents[1] / 'tools/compare_attachment_integration.py')
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


def parameters():
    return dict(comparison.common.DEFAULT_PARAMETERS, candidate_id=None,
        candidate_budget='sequential', presentation='clean', policies=list(comparison.POLICIES),
        measurement_policy='all')


def verified(length=100, crossings=0, stage='normal'):
    return {'metrics': {'wirelength': length, 'crossings': crossings, 'bends': 4},
        'routing_stage': stage, 'strict_simplification_audit': {'valid': True},
        'attachment_plan_signature': 'shared-plan', 'connectivity_digest': 'original-ports',
        'attachment_measurement': {'supported': True, 'measurable': True,
            'violations': 0, 'active_order_errors': 0, 'source_count': 0},
        'clean_svg': str(comparison.ROOT / 'output/example-clean.svg'),
        'width': 200, 'height': 300}


def record(value):
    return {'status': 'valid', 'signature': {'source_files': {}}, 'verified': value}


class AttachmentComparisonTests(unittest.TestCase):
    def test_all_three_policies_use_identical_strict_budgets(self):
        p = parameters(); p['candidate_id'] = '2'
        for policy in comparison.POLICIES:
            command = comparison.command_for({'parameters': p, 'policy': policy,
                'source_netlist': 'source.cir', 'cell': 'dut'}, Path('out'))
            expected = {'--attachment-layout': policy, '--route-tradeoff': 'strict',
                '--readability-layout': 'off', '--region-layout': 'off', '--structure-layout': 'off',
                '--symmetry-layout': 'off', '--passive-layout': 'flexible', '--cell': 'dut',
                '--iterations': '20000', '--quality-seconds': '300', '--rescue-seconds': '60',
                '--spacing-seconds': '60', '--fallback-seconds': '60', '--simplify-seconds': '5',
                '--candidate-id': '2'}
            for flag, value in expected.items():
                self.assertEqual(value, command[command.index(flag) + 1])

    def test_only_four_attachment_targets_receive_extra_seeds(self):
        cases = [{'case': name} for name in ('1', 'data10', 'data12', 'data14', 'data15', 'data16', 'data24')]
        jobs = comparison.schedule(cases, [1], [2, 3])
        self.assertEqual(15, len(jobs))
        self.assertEqual([1], [seed for case, seed in jobs if case['case'] == 'data16'])

    def test_zero_crossings_and_single_crossing_allowance(self):
        self.assertFalse(comparison.gates(verified(), verified(crossings=1))['crossings_limit'])
        self.assertTrue(comparison.gates(verified(crossings=2), verified(115, 3))['crossings_limit'])
        self.assertFalse(comparison.gates(verified(crossings=2), verified(crossings=4))['crossings_limit'])
        self.assertFalse(comparison.gates(verified(), verified(115.01))['wirelength_115_percent'])

    def test_new_bus_and_original_port_changes_are_rejected(self):
        self.assertFalse(comparison.gates(verified(), verified(stage='bus_fallback'))['no_new_bus_fallback'])
        new = verified(); new['connectivity_digest'] = 'changed'
        self.assertFalse(comparison.gates(verified(), new)['same_original_connectivity'])

    def test_unmeasurable_recognized_relations_are_not_zero_violations(self):
        new = verified(); new['attachment_measurement']['measurable'] = False
        self.assertFalse(comparison.gates(verified(), new)['no_new_structure_violations'])
        old = verified(); old['attachment_measurement']['supported'] = False
        new['attachment_measurement']['supported'] = False
        self.assertTrue(comparison.gates(old, new)['no_new_structure_violations'])

    def test_all_policy_rejects_missing_or_unmeasurable_source_evidence(self):
        old = verified(); new = verified()
        for item in (old, new):
            item['attachment_measurement'].update(source_count=1, source_measurable=True,
                source_errors=0, source_distance=1.2)
        self.assertTrue(all(comparison.gates(old, new, policy='all').values()))
        for key in ('source_measurable', 'source_errors', 'source_distance', 'source_count'):
            invalid = deepcopy(new); invalid['attachment_measurement'].pop(key)
            self.assertFalse(comparison.gates(old, invalid, policy='all')['source_measurement_available'])
        invalid = deepcopy(new); invalid['attachment_measurement']['source_distance'] = float('inf')
        self.assertFalse(comparison.gates(old, invalid, policy='all')['source_measurement_available'])

    def test_source_error_growth_does_not_hide_behind_improved_passives(self):
        old = verified(); new = verified()
        old['attachment_measurement'].update(source_count=1, source_measurable=True,
            source_errors=0, source_distance=2)
        new['attachment_measurement'].update(source_count=1, source_measurable=True,
            source_errors=1, source_distance=1)
        self.assertFalse(comparison.gates(old, new, policy='all')['source_errors_nonincreasing'])
        self.assertNotIn('source_errors_nonincreasing', comparison.gates(old, new, policy='passive'))
        self.assertTrue(all(comparison.source_gates({'source_count': 0}, {'source_count': 0}).values()))
        self.assertFalse(all(comparison.source_gates({}, {}).values()))

    def test_simplifier_audit_disallows_balanced_even_when_final_crossings_drop(self):
        data = {'metrics': {'wirelength': 90, 'crossings': 0, 'bends': 3},
            'route_simplification': {'before_metrics': {'wirelength': 100, 'crossings': 1, 'bends': 4},
                                    'tradeoff': {'policy': 'balanced'}}}
        self.assertFalse(comparison.strict_simplification_audit(data)['valid'])
        data['route_simplification']['tradeoff']['policy'] = 'strict'
        self.assertTrue(comparison.strict_simplification_audit(data)['valid'])

    def test_simplifier_audit_rejects_worse_bends_or_incomplete_report(self):
        data = {'metrics': {'wirelength': 90, 'crossings': 0, 'bends': 5},
            'route_simplification': {'before_metrics': {'wirelength': 100, 'crossings': 1, 'bends': 4}}}
        self.assertFalse(comparison.strict_simplification_audit(data)['valid'])
        del data['route_simplification']['before_metrics']['bends']
        self.assertFalse(comparison.strict_simplification_audit(data)['valid'])

    def test_initialization_trials_do_not_count_as_final_route_candidates(self):
        data = {'candidate_selection': {'candidates': [
            {'candidate_id': 2, 'status': 'valid', 'score': 1}, {'candidate_id': 2, 'status': 'valid'},
            {'candidate_id': 3, 'status': 'failed'}, {'candidate_id': None, 'status': 'valid'}]}}
        rows = comparison.actual_candidates(data)
        self.assertEqual({2, 3}, {row['candidate_id'] for row in rows})
        self.assertEqual(2, len(rows))

    def test_runtime_rng_or_nonfinite_measurement_cannot_enter_success_report(self):
        for invalid in ({'budget': {'rng': Random(1)}}, {'distance': float('inf')}):
            with self.assertRaises((TypeError, ValueError)):
                comparison.checked_payload(invalid)
        self.assertEqual({'spent': 12, 'distance': None},
            comparison.checked_payload({'spent': 12, 'distance': None}))

    def test_independent_verification_restores_equivalent_saved_display_slots(self):
        from attachment_selection import measure_candidate
        from tests.test_attachment_integration_review import inserted_row_fixture
        trial, comps, nets, index, plan, bridge = inserted_row_fixture()
        saved = measure_candidate(trial, comps, index, {'candidate_id': 2}, plan)
        restored = deepcopy(trial)
        del restored.attachment_junction_rows
        common_plan = deepcopy(plan)
        common_plan.update(policy='all', signature='independent-all-plan')
        saved.update(placement={}, routes={}, metrics={'wirelength': 10, 'crossings': 0, 'bends': 0},
            route_simplification={'before_metrics': {'wirelength': 10, 'crossings': 0, 'bends': 0}})
        signature = {'cell': None, 'parameters': {'measurement_policy': 'all'}}
        with patch.object(comparison.region, 'verify', return_value={'valid': True}), \
                patch.object(comparison, 'read_json', return_value=saved), \
                patch.object(comparison, 'build_measurement_plan', return_value=common_plan), \
                patch('sa_saved.load_placement', return_value=(saved, Path('source.cir'), None, restored, comps, nets, index)):
            result = comparison.verify(Path('saved.json'), signature)
        self.assertEqual(len(bridge['members']), result['attachment_measurement']['embedded_members'])
        self.assertEqual('independent-all-plan', result['attachment_measurement_context']['placement_slot_contract']['plan_signature'])
        self.assertFalse(hasattr(restored, 'attachment_junction_rows'))

    def test_policy_rollouts_have_independent_release_gates(self):
        records = {('x', 1, policy): record(verified()) for policy in comparison.POLICIES}
        records[('x', 1, 'all')] = record(verified(crossings=1))
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(comparison.region, 'runtime_snapshot', return_value={}):
            report = comparison.publish(Path(folder), [({'case': 'x'}, 1)], records, parameters(), {}, {})
        self.assertTrue(all(report['policy_release_gates']['passive'].values()))
        self.assertFalse(report['policy_release_gates']['all']['per_case_gates_passed'])
        self.assertFalse(report['default_promotion_approved'])

    def test_aggregate_crossing_gate_catches_individually_allowed_growth(self):
        records = {('x', 1, policy): record(verified(crossings=2 if policy == 'off' else 3))
                   for policy in comparison.POLICIES}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(comparison.region, 'runtime_snapshot', return_value={}):
            report = comparison.publish(Path(folder), [({'case': 'x'}, 1)], records, parameters(), {}, {})
        self.assertTrue(report['policy_release_gates']['passive']['per_case_gates_passed'])
        self.assertFalse(report['policy_release_gates']['passive']['total_crossings_nonincreasing'])

    def test_skip_only_partial_or_wrong_input_cannot_pass(self):
        for mode in ('skip', 'partial', 'wrong_input'):
            records = {('x', 1, policy): record(verified()) for policy in comparison.POLICIES}
            if mode == 'skip':
                records = {key: {'status': 'skipped', 'reason': 'input_validation_error'} for key in records}
            elif mode == 'partial':
                del records[('x', 1, 'all')]
            else:
                records[('x', 1, 'all')]['signature']['source_files']['different'] = 'hash'
            with tempfile.TemporaryDirectory() as folder, \
                    patch.object(comparison.region, 'runtime_snapshot', return_value={}):
                report = comparison.publish(Path(folder), [({'case': 'x'}, 1)], records, parameters(), {}, {})
            self.assertFalse(report['quantitative_gates_passed'], mode)

    def test_reuse_only_never_launches_a_missing_case(self):
        signature = {'source_files': {}, 'code_files': {}, 'parameters': parameters()}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(comparison, 'input_signature', return_value=signature), \
                patch.object(comparison.region, 'check_signature'), \
                patch.object(comparison.subprocess, 'Popen', side_effect=AssertionError('must not run')):
            result = comparison.execute({'case': 'x'}, 1, 'off', Path(folder), parameters(), {}, {}, reuse_only=True)
        self.assertEqual('not_run', result['status'])

    def test_historical_import_keeps_old_execution_and_requires_revalidation(self):
        p = parameters()
        signature = {'source_netlist': 'source', 'source_files': {}, 'cell': None,
            'parameters': p, 'policy': 'off', 'engine_flags': {'route_tradeoff': 'strict'}, 'code_files': {}}
        old_signature = deepcopy(signature)
        old_signature['code_files'] = {'old.py': 'old-hash'}
        old = {'case': 'x', 'seed': p['seed'], 'status': 'valid', 'signature': old_signature,
               'fingerprint': 'original', 'elapsed_seconds': 12.5, 'verified': {'placement_json': 'old.json'}}
        audit = {'cases': [{'case': 'x', 'status': 'valid', 'checks': {'candidate_1': True}}],
                 '_path': 'audit.json', '_sha256': 'audit-hash'}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(comparison, 'verify', return_value=verified()) as check, \
                patch.object(comparison, 'copy_saved_output', return_value=Path('copied.json')) as copy, \
                patch.object(comparison.common, '_sha', return_value='saved-hash'), \
                patch.object(comparison.region, 'check_signature'):
            result = comparison.import_compatible_off(old, audit, signature, Path(folder))
        self.assertEqual(1, check.call_count)
        self.assertEqual(1, copy.call_count)
        self.assertEqual(Path('copied.json'), check.call_args.args[0])
        self.assertTrue(result['historical_baseline'])
        self.assertFalse(result['fresh_flow_executed'])
        self.assertEqual(12.5, result['elapsed_seconds'])
        self.assertEqual(old_signature, result['signature'])
        self.assertEqual(signature, result['validation_signature'])

    def test_historical_import_rejects_budget_resource_and_input_differences(self):
        signature = {'source_netlist': 'source', 'source_files': {}, 'cell': None,
            'parameters': parameters(), 'policy': 'off', 'engine_flags': {},
            'code_files': {'symbol-geometry.json': 'same'}}
        audit = {'cases': [{'case': 'x', 'status': 'valid', 'checks': {'same': True}}]}
        for field in ('budget', 'resource', 'input', 'policy'):
            old = {'case': 'x', 'status': 'valid', 'signature': deepcopy(signature)}
            if field == 'budget':
                old['signature']['parameters']['quality_seconds'] = 299
            elif field == 'resource':
                old['signature']['code_files']['symbol-geometry.json'] = 'different'
            elif field == 'input':
                old['signature']['source_files']['changed'] = 'new'
            else:
                old['signature']['engine_flags']['route_tradeoff'] = 'balanced'
            with self.assertRaises(ValueError), patch.object(comparison, 'verify', side_effect=AssertionError('no validation of mismatched input')):
                comparison.import_compatible_off(old, audit, signature, Path('unused'))

    def test_historical_audit_must_match_current_root_code(self):
        code = {str(comparison.ROOT / 'run_flow.py'): 'current'}
        audit = {'passed': True, 'cases': [{'case': 'x'}], 'current_files': {'wrong': 'hash'}}
        with patch.object(comparison, 'read_json', return_value=audit):
            with self.assertRaisesRegex(ValueError, 'current frozen root engine'):
                comparison.load_compatible_off(Path('old-report.json'), Path('audit.json'), code)

    def test_saved_output_copy_preserves_original_files(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            for suffix in ('-placement.json', '-clean.svg', '-layered.svg'):
                (directory / ('input' + suffix)).write_text(suffix, encoding='utf8')
            copied = comparison.copy_saved_output(directory / 'input-placement.json', directory / 'new')
            self.assertEqual('-placement.json', copied.read_text('utf8'))
            (directory / 'new/input-clean.svg').write_text('new sidecar context', encoding='utf8')
            self.assertEqual('-clean.svg', (directory / 'input-clean.svg').read_text('utf8'))

    def test_cross_drive_links_have_file_uri_fallback(self):
        with patch.object(comparison.region.common, '_relative', side_effect=ValueError('different drives')):
            self.assertTrue(comparison.region.relative_link(Path('C:/output'), comparison.ROOT / 'example.svg').startswith('file:'))

    def test_plan_only_has_no_flow_or_regression_execution(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(comparison.common, 'discover_cases', return_value=[{'case': 'data10', 'netlist': 'none'}]), \
                patch.object(comparison.common, '_freeze_inputs', return_value={}), \
                patch.object(comparison.region, 'runtime_snapshot', return_value={}), \
                patch.object(comparison.subprocess, 'Popen', side_effect=AssertionError('must not run')):
            self.assertEqual(0, comparison.main(['--plan-only', '--cases', 'data10', '--core-seeds',
                                               '--out-dir', folder]))
            plan = comparison.read_json(Path(folder) / 'batch-plan.json')
            self.assertEqual(3, plan['expected_runs'])
            self.assertEqual(0, comparison.main(['--status-only', '--out-dir', folder]))


if __name__ == '__main__':
    unittest.main()
