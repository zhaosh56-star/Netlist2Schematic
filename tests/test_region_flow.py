"""Region plans are opt-in, late measured, and cannot be erased by feedback."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from netlist2schematic.flow import aesthetic_flow
from netlist2schematic.flow import run_flow
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from test_aesthetic_flow import candidate, route_result


PLAN = {'version': 'region-layout-v1', 'signature': 'same-active-rows',
        'supported': True, 'regions': [], 'coverage': {}}


def region_candidate(identifier, *, signature=None):
    item = candidate(identifier)
    if identifier != 1:
        plan = deepcopy(PLAN)
        if signature is not None:
            plan['signature'] = signature
        item[4]['layout_regions'] = {'policy': 'auto', 'applied': True, 'plan': plan}
    return item


class RegionInterfaceTests(unittest.TestCase):
    def test_off_default_and_auto_forwarded(self):
        for arguments, expected in (([], 'off'), (['--region-layout', 'auto'], 'auto')):
            with self.subTest(expected=expected), patch('netlist2schematic.flow.run_flow.run') as run:
                run_flow.main(['input.cir', *arguments])
            self.assertEqual(expected, run.call_args.kwargs['region_layout'])
            self.assertEqual('off', run.call_args.kwargs['structure_layout'])
            self.assertEqual('off', run.call_args.kwargs['symmetry_layout'])

    def test_invalid_and_incompatible_policies_fail_before_input_is_read(self):
        for kwargs in ({'region_layout': 'bad'},
                       {'region_layout': 'auto', 'structure_layout': 'auto'},
                       {'region_layout': 'auto', 'symmetry_layout': 'auto'},
                       {'region_layout': 'auto', 'mode': 'baseline'},
                       {'region_layout': 'auto', 'layout_style': 'legacy'}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, 'region_layout'):
                run_flow.run('not-present.cir', **kwargs)

    def test_saved_coordinate_modes_reject_relayout_request(self):
        for flag in ('--reroute-placement', '--simplify-placement'):
            with self.subTest(flag=flag), redirect_stdout(io.StringIO()), \
                 patch('sys.stderr', new=io.StringIO()), \
                 patch('netlist2schematic.flow.run_flow.reroute_placement') as reroute, \
                 patch('netlist2schematic.flow.run_flow.simplify_placement') as simplify:
                with self.assertRaises(SystemExit):
                    run_flow.main([flag, 'saved.json', '--region-layout', 'auto'])
                reroute.assert_not_called()
                simplify.assert_not_called()

    def test_selector_rejects_conflict_before_generation(self):
        with patch('netlist2schematic.layout.structured_placement.create_candidates') as producer:
            with self.assertRaisesRegex(ValueError, 'region_layout'):
                aesthetic_flow.select_layout('unused', region_layout='auto', structure_layout='auto')
            producer.assert_not_called()


class RegionFlowTests(unittest.TestCase):
    def select(self, items, *, routes=None, scores=None, feedback=None, **kwargs):
        self.events = []

        def measure(pl, comps, index, plan):
            self.events.append(('measure', pl.identifier, deepcopy(plan)))
            return {'measurable': True, 'plan_signature': plan['signature']}

        def route(par, nets, pl, index, **options):
            self.events.append(('route', pl.identifier))
            result = next(route_iterator)
            if isinstance(result, Exception):
                raise result
            return deepcopy(result)

        route_iterator = iter(routes or [route_result()] * len(items))
        quality_iterator = iter(scores or [10 * i for i in range(1, len(items) + 1)])

        def quality(*args):
            return {'score': next(quality_iterator), 'per_net': {
                'signal': {'detour_factor': 3, 'excess_detour': 600}}}

        def choose(results):
            return min(results, key=lambda row: row[0]), {'policy': 'region-layout-v1'}

        with ExitStack() as stack:
            stack.enter_context(patch('netlist2schematic.layout.structured_placement.create_candidates', return_value=iter(items)))
            self.feedback_mock = stack.enter_context(patch('netlist2schematic.layout.structured_placement.feedback_candidate',
                                                           side_effect=feedback))
            stack.enter_context(patch('netlist2schematic.plugins.region.region_layout.measure_regions', side_effect=measure))
            self.selection_mock = stack.enter_context(patch('netlist2schematic.plugins.region.region_layout.select_region_result', side_effect=choose))
            stack.enter_context(patch('netlist2schematic.flow.aesthetic_flow.final_route', side_effect=route))
            stack.enter_context(patch('netlist2schematic.layout.layout_quality.evaluate_quality', side_effect=quality))
            stack.enter_context(redirect_stdout(io.StringIO()))
            return aesthetic_flow.select_layout('unused.cir', region_layout='auto', iterations=0, **kwargs)

    def test_reference_is_measured_late_with_same_plan_without_moving_it(self):
        items = [region_candidate(i) for i in (1, 2)]
        before = deepcopy(items[0][0].positions)
        result = self.select(items)
        first_measure = next(i for i, event in enumerate(self.events) if event[0] == 'measure')
        self.assertLess(self.events.index(('route', 1)), first_measure)
        reports = result[4]['quality']['candidates']
        self.assertEqual(reports[0]['layout_regions']['plan'], reports[1]['layout_regions']['plan'])
        self.assertFalse(reports[0]['layout_regions']['applied'])
        self.assertEqual(before, result[0].positions)
        self.assertNotIn('layout_regions', items[0][4])

    def test_feedback_uses_compatible_region_incumbent_not_original_q_reference(self):
        def feedback(pl, comps, nets, index, metadata, quality):
            self.assertEqual(2, pl.identifier)
            return (deepcopy(pl), comps, nets, index, deepcopy(metadata))
        self.select([region_candidate(i) for i in (1, 2, 3)], scores=[1, 10, 20], feedback=feedback)
        self.assertEqual(1, self.feedback_mock.call_count)
        self.assertEqual([1, 2, 2], [event[1] for event in self.events if event[0] == 'route'])

    def test_incompatible_plan_keeps_third_regions(self):
        self.select([region_candidate(1), region_candidate(2, signature='other-rows'), region_candidate(3)])
        self.feedback_mock.assert_not_called()
        self.assertEqual([1, 2, 3], [event[1] for event in self.events if event[0] == 'route'])

    def test_unsupported_regions_keep_original_q_feedback_behavior(self):
        items = [region_candidate(i) for i in (1, 2, 3)]
        for item in items[1:]:
            report = item[4]['layout_regions']
            report['applied'] = False
            report['plan']['supported'] = False
        def feedback(pl, comps, nets, index, metadata, quality):
            self.assertEqual(1, pl.identifier)
            return deepcopy((pl, comps, nets, index, metadata))
        self.select(items, scores=[1, 10, 20], feedback=feedback)
        self.assertEqual(1, self.feedback_mock.call_count)
        self.assertEqual([1, 2, 1], [event[1] for event in self.events if event[0] == 'route'])

    def test_reference_cannot_supply_feedback_when_region_candidate_failed(self):
        self.select([region_candidate(i) for i in (1, 2, 3)],
                    routes=[route_result(), RoutingFailure('blocked', {'reason': 'blocked'}), route_result()])
        self.feedback_mock.assert_not_called()

    def test_explicit_candidate_is_not_reselected(self):
        result = self.select([region_candidate(i) for i in (1, 2)], candidate_id='2')
        self.assertEqual(2, result[0].identifier)
        self.selection_mock.assert_not_called()

    def test_no_evaluated_region_does_not_claim_coverage(self):
        result = self.select([region_candidate(1)])
        self.assertEqual([('route', 1)], self.events)
        self.assertNotIn('layout_regions', result[4])
        self.selection_mock.assert_not_called()

    def test_failure_checkpoint_contains_actual_region_layout(self):
        failure = RoutingFailure('blocked', {'reason': 'blocked'})
        with self.assertRaises(RoutingFailure) as caught:
            self.select([region_candidate(2)], routes=[failure])
        metadata = caught.exception.retry_context[4]
        self.assertTrue(metadata['layout_regions']['applied'])
        self.assertEqual(PLAN, metadata['layout_regions']['plan'])


class RegionSavedCoordinatesTests(unittest.TestCase):
    def test_saved_modes_only_remeasure_saved_plan_and_keep_positions(self):
        from netlist2schematic.netlist.netlist_core import parse_netlist
        from netlist2schematic.plugins.region.region_layout import build_region_plan
        from netlist2schematic.common.sa_common import load_local
        with tempfile.TemporaryDirectory(prefix='region-saved-') as folder:
            root = Path(folder)
            source = root/'rc.cir'
            source.write_text('R1 input output 1k\nC1 output 0 1n\n.end\n', encoding='utf8')
            with redirect_stdout(io.StringIO()):
                run_flow.run(source, iterations=30, out_dir=root/'original')
            original = next((root/'original').glob('*-placement.json'))
            data = json.loads(original.read_text('utf8'))
            data['requested_region_layout'] = 'auto'
            data['layout_regions'] = {'policy': 'auto', 'applied': False,
                'plan': build_region_plan(parse_netlist(source)),
                'metrics': {'deliberately_stale': True}}
            original.write_text(json.dumps(data), encoding='utf8')
            saved_bytes = original.read_bytes()
            with patch('netlist2schematic.plugins.region.region_layout.build_region_plan', side_effect=AssertionError('saved input replanned')), \
                 patch('netlist2schematic.layout.structured_placement.create_candidates', side_effect=AssertionError('saved input relayout')), \
                 redirect_stdout(io.StringIO()):
                run_flow.simplify_placement(original, out_dir=root/'simplified', simplify_seconds=0)
                run_flow.reroute_placement(original, out_dir=root/'rerouted', simplify_seconds=0)
                load_local('render_clean', 'render-clean.py').render(original, root/'redraw.svg')
            self.assertEqual(saved_bytes, original.read_bytes())
            for mode in ('simplified', 'rerouted'):
                new = json.loads(next((root/mode).glob('*placement.json')).read_text('utf8'))
                self.assertEqual(data['placement'], new['placement'])
                self.assertEqual(data['layout_grid'], new['layout_grid'])
                self.assertTrue(new['validation']['valid'])
                self.assertNotIn('deliberately_stale', new['layout_regions']['metrics'])
                self.assertEqual('final_output', new['layout_regions']['measurement_stage'])


if __name__ == '__main__':
    unittest.main()
