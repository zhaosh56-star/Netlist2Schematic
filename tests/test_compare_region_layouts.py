"""Independent region comparison cannot mistake partial coverage for release."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

spec = importlib.util.spec_from_file_location('region_compare',
    Path(__file__).resolve().parents[1]/'tools/compare_region_layouts.py')
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


def verified(length=100, crossings=0, violations=0, stage='normal'):
    return {'metrics': {'wirelength': length, 'crossings': crossings, 'bends': 2},
        'measurement': {'supported': True, 'measurable': True, 'interleaving': violations,
                        'order_errors': 0, 'pin_axis_error': 0, 'dispersion': 0},
        'measurement_plan_signature': 'same-plan', 'routing_stage': stage,
        'active_rows': {'a': 1, 'b': 2}, 'width': 200, 'height': 300,
        'clean_svg': 'clean.svg', 'placement_json': 'placement.json',
        'route_detours': {'maximum_factor': 2, 'per_net': {'n': {'detour_factor': 2}}}}


class ComparisonGateTests(unittest.TestCase):
    def test_final_length_and_crossings_guard(self):
        self.assertTrue(all(comparison.gates(verified(crossings=1), verified(115, 2)).values()))
        self.assertFalse(comparison.gates(verified(), verified(115.01))['wirelength_115_percent'])
        self.assertFalse(comparison.gates(verified(), verified(crossings=1))['crossings_limit'])

    def test_measurement_catalogue_must_be_identical(self):
        new = verified(); new['measurement_plan_signature'] = 'other'
        self.assertFalse(comparison.gates(verified(), new)['same_measurement_plan'])
        old = verified(); old.pop('measurement_plan_signature')
        new.pop('measurement_plan_signature')
        self.assertFalse(comparison.gates(old, new)['same_measurement_plan'])

    def test_missing_measurement_is_not_zero_violation(self):
        new = verified(); new['measurement']['measurable'] = False
        new['measurement']['interleaving'] = None
        self.assertFalse(comparison.gates(verified(), new)['no_new_region_violations'])

    def test_unsupported_plan_is_not_claimed_as_measurement(self):
        old, new = verified(), verified()
        for value in (old, new):
            value['measurement'].update(supported=False, measurable=False,
                interleaving=None, order_errors=None)
        self.assertTrue(comparison.gates(old, new)['no_new_region_violations'])
        self.assertIsNone(new['measurement']['interleaving'])

    def test_no_new_bus_active_row_or_structural_regression(self):
        self.assertFalse(comparison.gates(verified(), verified(stage='bus_fallback'))['no_new_bus_fallback'])
        new = verified(); new['active_rows']['a'] = 9
        self.assertFalse(comparison.gates(verified(), new)['active_rows_unchanged'])
        self.assertFalse(comparison.gates(verified(), verified(violations=1))['no_new_region_violations'])

    def test_incomplete_and_asymmetric_skips_are_not_success(self):
        jobs = [({'case': 'x'}, 1)]
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            report = comparison.publish(output, jobs, {}, {}, {}, {})
            self.assertFalse(report['quantitative_gates_passed'])
            records = {('x', 1, 'off'): {'status': 'skipped', 'reason': 'input_validation_error'},
                       ('x', 1, 'auto'): {'status': 'failed', 'error': 'oops'}}
            report = comparison.publish(output, jobs, records, {}, {}, {})
        self.assertFalse(report['release_gates']['paired_status_consistent'])
        self.assertFalse(report['release_gates']['no_routing_failures'])

    def test_all_skipped_batch_is_not_a_quantitative_success(self):
        records = {('x', 1, p): {'status': 'skipped', 'reason': 'input_validation_error'}
                   for p in comparison.POLICIES}
        with tempfile.TemporaryDirectory() as folder:
            report = comparison.publish(Path(folder), [({'case': 'x'}, 1)], records, {}, {}, {})
        self.assertTrue(report['release_gates']['complete'])
        self.assertFalse(report['quantitative_gates_passed'])

    def test_different_sources_are_never_a_passing_comparison(self):
        records = {('x', 1, p): {'status': 'valid', 'signature': {'source_files': {'file': p}},
                                'verified': verified()} for p in comparison.POLICIES}
        with tempfile.TemporaryDirectory() as folder:
            report = comparison.publish(Path(folder), [({'case': 'x'}, 1)], records, {}, {}, {})
        self.assertFalse(report['release_gates']['paired_sources_identical'])

    def test_numeric_success_still_requires_visual_and_full_suite(self):
        records = {('x', 1, p): {'status': 'valid', 'signature': {'source_files': {}},
                                'verified': verified()} for p in comparison.POLICIES}
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            report = comparison.publish(output, [({'case': 'x'}, 1)], records, {}, {}, {})
            saved = json.loads((output/'comparison.json').read_text(encoding='utf8'))
        self.assertTrue(report['quantitative_gates_passed'])
        self.assertFalse(report['default_promotion_approved'])
        self.assertFalse(report['full_suite'])
        self.assertEqual('pending', report['visual_review'])
        self.assertEqual({'n': {'detour_factor': 2}}, saved['cases'][0]['results']['auto']['verified']['route_detours']['per_net'])

    def test_aggregate_crossings_must_not_increase_even_if_each_case_passes(self):
        records = {('x', 1, 'off'): {'status': 'valid', 'signature': {'source_files': {}},
                                    'verified': verified(crossings=1)},
                   ('x', 1, 'auto'): {'status': 'valid', 'signature': {'source_files': {}},
                                     'verified': verified(crossings=2)}}
        with tempfile.TemporaryDirectory() as folder:
            report = comparison.publish(Path(folder), [({'case': 'x'}, 1)], records, {}, {}, {})
        self.assertTrue(report['release_gates']['paired_gates'])
        self.assertFalse(report['release_gates']['total_crossings_nonincreasing'])


class ComparisonExecutionTests(unittest.TestCase):
    def test_cross_drive_links_use_file_uri_for_gt_and_all_saved_outputs(self):
        root = Path(__file__).resolve().parents[1]
        image = root/'output/region-layout-comparison/a b&c.svg'
        with patch.object(comparison.common, '_relative', side_effect=ValueError('different drives')):
            href = comparison.relative_link(Path('C:/temporary-report'), image)
            html = comparison.figure(Path('C:/temporary-report'), 'GT <reference>', image)
        self.assertEqual(image.as_uri(), href)
        self.assertIn('a%20b%26c.svg', href)
        self.assertIn('GT &lt;reference&gt;', html)
        self.assertIn(href, html)

    def test_reference_only_run_is_not_misreported_as_region_evaluation(self):
        data = {'layout_regions': {'applied': False}, 'candidate_records': [
            {'status': 'valid', 'candidate_id': 1, 'layout_regions': {'applied': False}},
            {'status': 'skipped', 'candidate_id': 2, 'layout_regions': {'applied': True}}]}
        self.assertFalse(comparison.region_was_evaluated(data))
        data['candidate_records'][1]['status'] = 'failed'
        self.assertTrue(comparison.region_was_evaluated(data))

    def test_reuse_only_never_starts_missing_run(self):
        signature = {'source_files': {'input.cir': 'hash'}, 'parameters': {}, 'policy': 'off'}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(comparison.common, '_signature', return_value=signature), \
                patch.object(comparison.subprocess, 'Popen', side_effect=AssertionError('no new run')):
            row = comparison.execute({'case': 'x'}, 1, 'off', Path(folder), {}, {},
                                     {'input.cir': 'hash'}, reuse_only=True)
        self.assertIsNone(row)

    def test_command_is_region_only_and_does_not_enable_old_experiments(self):
        parameters = dict(comparison.common.DEFAULT_PARAMETERS,
                          candidate_budget='sequential', presentation='clean')
        cmd = comparison.command_for({'parameters': parameters, 'source_netlist': 'input.cir',
            'cell': 'dut', 'policy': 'auto'}, Path('out'))
        for key, value in (('--region-layout', 'auto'), ('--structure-layout', 'off'),
                           ('--symmetry-layout', 'off'), ('--net-labels', 'off'),
                           ('--passive-layout', 'flexible'), ('--cell', 'dut')):
            self.assertEqual(value, cmd[cmd.index(key)+1])

    def test_core_extra_seeds_are_unique_and_only_applied_to_core_validation_cases(self):
        cases = [{'case': '1'}, {'case': 'data16'}, {'case': 'data24'}]
        jobs = comparison.schedule(cases, [1, 1], [1, 2, 3])
        self.assertEqual(7, len(jobs))
        self.assertEqual([1], [seed for case, seed in jobs if case['case'] == '1'])

    def test_unsupported_inputs_do_not_start_flow(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)/'unknown.cir'; p.write_text('X1 a b c d mystery\n', encoding='utf8')
            with patch.object(comparison.subprocess, 'Popen', side_effect=AssertionError('no flow')):
                row = comparison.execute({'case': 'bad', 'netlist': str(p)}, 1, 'auto',
                                         Path(folder), {}, {}, {})
        self.assertEqual('skipped', row['status'])
        self.assertEqual('input_validation_error', row['reason'])

    def test_overlay_never_changes_original_svg_or_placement(self):
        pl = SimpleNamespace(pos_of=lambda i: (100+i*100, 100),
                             pin_world=lambda i, pin: (100+i*100, 100))
        plan = {'regions': [{'id': 'region<&', 'members': ['a', 'b'], 'children': [],
                            'branches': [{'id': 'A', 'members': ['a']}, {'id': 'B', 'members': ['b']}]}],
                'series_axes': [{'a': 'a', 'b': 'b', 'pin_a': 'D', 'pin_b': 'S'}]}
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)/'original.svg'; target = Path(folder)/'overlay.svg'
            original = b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200"><path d="M 0 0 L 1 1"/></svg>'
            p.write_bytes(original)
            comparison.annotated_svg(p, target, pl, [], {'a': 0, 'b': 1}, plan)
            self.assertEqual(original, p.read_bytes())
            root = ET.parse(target).getroot()
            self.assertEqual('region-layout', root.find(comparison.SVG+'g').get('data-diagnostic'))
            self.assertEqual(1, len(root.findall('.//'+comparison.SVG+'line')))


if __name__ == '__main__':
    unittest.main()
