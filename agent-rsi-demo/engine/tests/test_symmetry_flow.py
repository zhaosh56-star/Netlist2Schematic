"""Symmetry policy dispatch and candidate safety at the public flow seam."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import random
import re
import tempfile
import unittest
from unittest.mock import patch

import aesthetic_flow
from orthogonal_router import RoutingFailure
import run_flow
from sa_common import load_local
from sa_saved import load_placement


class SmallPlacement:
    """Only the state used by the candidate-selection seam, without geometry."""

    def __init__(self, identifier):
        self.identifier = identifier
        self.cell_of = [(0, 0), (0, 1)]
        self.orient = [0, 0]
        self.positions = [(0, 0), (100, 0)]

    def pos_of(self, index):
        return self.positions[index]

    def pin_world(self, index, pin):
        return self.positions[index]


def item(identifier):
    comps = [{'id': 'R1', 'symbol': 'resistor'}, {'id': 'R2', 'symbol': 'resistor'}]
    nets = {'signal': [('R1', '1'), ('R2', '1')]}
    metadata = {'candidate_id': identifier, 'candidate_seed': 100 + identifier,
                'structs': [], 'energy': 1000 - identifier}
    return SmallPlacement(identifier), comps, nets, {'R1': 0, 'R2': 1}, metadata


def routed(length=100, crossings=0, bends=0):
    return ([('signal', [((0, 0), (length, 0))])],
            {'wirelength': length, 'crossings': crossings, 'bends': bends},
            {'valid': True, 'routing_seconds': .001})


def symmetry(failed=0, bucket=0, residual=0, count=1):
    return {'version': 'test-symmetry', 'policy': 'auto',
            'relations': [{'kind': 'test_pair'}] if count else [],
            'current': {'failed_relations': failed, 'residual_bucket': bucket,
                        'residual': residual, 'relation_count': count, 'groups': []}}


class SymmetryCandidateFlowTests(unittest.TestCase):
    def choose(self, items, *, scores=None, reports=None, routes=None,
               regularizer=None, feedback=None, **kwargs):
        # These tests exercise explicit opt-in behavior, independently of the
        # product default which bypasses symmetry regularization.
        kwargs.setdefault('symmetry_layout', 'auto')
        scores = scores or {1: 10, 2: 20, 3: 30}
        reports = reports or {i: symmetry() for i in scores}
        routes = routes or {i: routed() for i in scores}
        events = []

        def measure(placement, comps, index, metadata, **options):
            identifier = int(metadata['candidate_id'])
            events.append(('measure', identifier))
            return deepcopy(reports[identifier])

        def regularize(par, placement, comps, nets, index, metadata, **options):
            identifier = int(metadata['candidate_id'])
            events.append(('regularize', identifier))
            self.assertIn('deadline', options)
            self.assertIn('options', options)
            if regularizer:
                return regularizer(par, placement, comps, nets, index, metadata, **options)
            metadata = deepcopy(metadata)
            metadata['layout_symmetry'] = deepcopy(reports[identifier])
            return placement, metadata

        def route(*args, **options):
            placement = args[2]
            events.append(('route', placement.identifier))
            result = routes[placement.identifier]
            if isinstance(result, Exception):
                raise result
            return result

        def quality(par, placement, comps, nets, index, wires, metrics, structs):
            result = scores[placement.identifier]
            return deepcopy(result if isinstance(result, dict) else {'score': result, 'per_net': {}})

        with ExitStack() as stack:
            stack.enter_context(patch('structured_placement.create_candidates', return_value=iter(items)))
            stack.enter_context(patch('aesthetic_flow.final_route', side_effect=route))
            stack.enter_context(patch('layout_quality.evaluate_quality', side_effect=quality))
            regularized = stack.enter_context(patch('symmetry_layout.regularize_symmetry', side_effect=regularize))
            measured = stack.enter_context(patch('symmetry_layout.report_symmetry', side_effect=measure))
            feedback_mock = stack.enter_context(patch('structured_placement.feedback_candidate', side_effect=feedback))
            stack.enter_context(redirect_stdout(io.StringIO()))
            result = aesthetic_flow.select_layout('unused.cir', iterations=0, **kwargs)
        return result, events, regularized, measured, feedback_mock

    def test_candidate_one_is_measured_but_keeps_original_placement(self):
        first = item(1)
        original = deepcopy(first[0].__dict__)
        result, events, regularized, measured, _ = self.choose([first])
        self.assertEqual(original, first[0].__dict__)
        self.assertEqual(original, result[0].__dict__)
        regularized.assert_not_called()
        self.assertTrue(measured.called)
        self.assertLess(events.index(('measure', 1)), events.index(('route', 1)))
        self.assertIn('layout_symmetry', result[4])

    def test_only_candidates_two_and_three_are_regularized_before_routing(self):
        result, events, regularized, _, _ = self.choose([item(i) for i in (1, 2, 3)])
        self.assertEqual(2, regularized.call_count)
        self.assertEqual([2, 3], [call.args[5]['candidate_id'] for call in regularized.call_args_list])
        for identifier in (2, 3):
            self.assertLess(events.index(('regularize', identifier)), events.index(('route', identifier)))
        self.assertEqual('device-symmetry-v1', result[4]['quality']['selection_policy'])
        self.assertTrue(all('layout_symmetry' in row for row in result[4]['quality']['candidates']))

    def test_off_does_not_even_measure_and_keeps_original_quality_selection(self):
        results = []
        for _ in range(2):
            result, events, regularized, measured, _ = self.choose(
                [item(1), item(2)], scores={1: 20, 2: 10}, symmetry_layout='off')
            self.assertFalse(any(event[0] in ('regularize', 'measure') for event in events))
            regularized.assert_not_called()
            measured.assert_not_called()
            self.assertEqual(2, result[0].identifier)
            self.assertEqual('balanced-v1', result[4]['quality']['selection_policy'])
            results.append((result[0].__dict__, result[5]))
        self.assertEqual(*results)

    def test_default_selector_bypasses_symmetry_for_all_three_candidates(self):
        candidates = [item(identifier) for identifier in (1, 2, 3)]
        qualities = [{'score': score, 'per_net': {}} for score in (30, 10, 20)]
        with patch('structured_placement.create_candidates', return_value=iter(candidates)), \
                patch('aesthetic_flow.final_route', return_value=routed()) as router, \
                patch('layout_quality.evaluate_quality', side_effect=qualities), \
                patch('symmetry_layout.regularize_symmetry', side_effect=AssertionError('default relayout')), \
                patch('symmetry_layout.report_symmetry', side_effect=AssertionError('default measure')), \
                redirect_stdout(io.StringIO()):
            # Deliberately call the production function directly without a
            # policy argument; choose() above intentionally opts into auto.
            chosen = aesthetic_flow.select_layout('unused.cir', iterations=0)
        self.assertEqual([1, 2, 3], [call.args[2].identifier for call in router.call_args_list])
        self.assertEqual(2, chosen[0].identifier)
        self.assertEqual(10, chosen[4]['quality']['score'])
        self.assertEqual('balanced-v1', chosen[4]['quality']['selection_policy'])
        self.assertNotIn('symmetry_selection', chosen[4]['quality'])
        self.assertNotIn('layout_symmetry', chosen[4])
        self.assertTrue(all('layout_symmetry' not in row for row in chosen[4]['quality']['candidates']))

    def test_better_symmetry_can_win_despite_worse_original_quality(self):
        result, *_ = self.choose([item(1), item(2)], scores={1: 10, 2: 30},
            reports={1: symmetry(failed=1, bucket=4, residual=4), 2: symmetry()},
            routes={1: routed(100), 2: routed(120)})
        self.assertEqual(2, result[0].identifier)
        self.assertEqual(30, result[4]['quality']['score'])
        self.assertEqual(120, result[5][1]['wirelength'])
        selection = result[4]['quality']['symmetry_selection']
        self.assertEqual('1', selection['reference_candidate'])
        self.assertEqual(100, selection['reference_wirelength'])
        self.assertEqual(10, selection['reference_score'])
        self.assertEqual(1.25, selection['length_ratio'])
        self.assertEqual(125, selection['wirelength_limit'])
        self.assertEqual('before_route_simplification', selection['reference_stage'])
        self.assertEqual('2', selection['selected_candidate'])

    def test_wirelength_guard_includes_exact_125_percent_boundary(self):
        result, *_ = self.choose([item(1), item(2)], scores={1: 10, 2: 30},
            reports={1: symmetry(failed=1), 2: symmetry()},
            routes={1: routed(100), 2: routed(125)})
        self.assertEqual(2, result[0].identifier)
        self.assertEqual(['1', '2'], result[4]['quality']['symmetry_selection']['eligible_candidates'])

    def test_wirelength_guard_excludes_better_symmetry_beyond_boundary(self):
        result, *_ = self.choose([item(1), item(2)], scores={1: 10, 2: 30},
            reports={1: symmetry(failed=1), 2: symmetry()},
            routes={1: routed(100), 2: routed(125.01)})
        self.assertEqual(1, result[0].identifier)
        self.assertEqual(['2'], result[4]['quality']['symmetry_selection']['excluded_candidates'])

    def test_failed_relation_count_precedes_residual_bucket(self):
        result, *_ = self.choose([item(1), item(2)], scores={1: 10, 2: 20},
            reports={1: symmetry(failed=1, bucket=0), 2: symmetry(failed=0, bucket=9)})
        self.assertEqual(2, result[0].identifier)

    def test_lower_residual_bucket_precedes_original_quality(self):
        result, *_ = self.choose([item(1), item(2)], scores={1: 10, 2: 20},
            reports={1: symmetry(bucket=2), 2: symmetry(bucket=1)})
        self.assertEqual(2, result[0].identifier)

    def test_same_bucket_uses_original_quality_instead_of_raw_residual(self):
        result, *_ = self.choose([item(1), item(2)], scores={1: 10, 2: 20},
            reports={1: symmetry(bucket=1, residual=1.9), 2: symmetry(bucket=1, residual=1.1)})
        self.assertEqual(1, result[0].identifier)

    def test_equal_symmetry_and_quality_keep_crossing_then_length_tiebreakers(self):
        result, *_ = self.choose([item(i) for i in (1, 2, 3)],
            scores={1: 10, 2: 10, 3: 10},
            routes={1: routed(100, crossings=1), 2: routed(120), 3: routed(110)})
        self.assertEqual(3, result[0].identifier)

    def test_no_relations_keeps_original_quality_selection(self):
        result, *_ = self.choose([item(1), item(2)], scores={1: 20, 2: 10},
            reports={1: symmetry(count=0), 2: symmetry(count=0)})
        self.assertEqual(2, result[0].identifier)

    def test_explicit_candidate_replay_is_not_overridden_by_symmetry_selection(self):
        result, events, *_ = self.choose([item(i) for i in (1, 2, 3)],
            scores={1: 10, 2: 20, 3: 30},
            reports={1: symmetry(), 2: symmetry(), 3: symmetry(failed=5)},
            candidate_id='3')
        self.assertEqual(3, result[0].identifier)
        self.assertEqual('3', result[4]['quality']['selected_candidate'])
        self.assertEqual('replay_complete', result[4]['quality']['stop_reason'])
        self.assertEqual([1, 2, 3], [value for action, value in events if action == 'route'])

    def test_explicit_reference_zero_is_neither_measured_nor_regularized(self):
        result, events, regularized, measured, _ = self.choose(
            [item(0)], scores={0: 10}, candidate_id='0')
        self.assertEqual(0, result[0].identifier)
        self.assertEqual([('route', 0)], events)
        regularized.assert_not_called()
        measured.assert_not_called()

    def test_feedback_source_uses_original_quality_and_regularizes_replacement(self):
        events = []
        proposal = item(99)
        proposal[4]['feedback'] = {'source_candidate': '1', 'net': 'signal'}

        def feedback(placement, *args):
            events.append(('feedback', placement.identifier))
            return proposal

        def regularizer(par, placement, comps, nets, index, metadata, **options):
            events.append(('regularize', placement.identifier))
            metadata = deepcopy(metadata)
            metadata['layout_symmetry'] = symmetry(failed=0 if placement.identifier == 2 else 4)
            return placement, metadata

        severe = {'score': 10, 'per_net': {'signal': {'detour_factor': 3, 'excess_detour': 600}}}
        result, _, _, _, feedback_mock = self.choose([item(i) for i in (1, 2, 3)],
            scores={1: severe, 2: 20, 99: 40},
            reports={1: symmetry(failed=1), 2: symmetry(), 3: symmetry(failed=4)},
            routes={1: routed(), 2: routed(), 99: routed()},
            regularizer=regularizer, feedback=feedback)
        self.assertEqual(1, feedback_mock.call_args.args[0].identifier)
        self.assertLess(events.index(('feedback', 1)), events.index(('regularize', 99)))
        self.assertNotIn(('regularize', 3), events)
        self.assertEqual(2, result[0].identifier)

    def test_regularized_failure_checkpoint_belongs_to_last_attempt_and_is_private(self):
        first = RoutingFailure('first blocked', {'reason': 'no_path'})
        first._rescue_context = {'failed_net': 'first', 'mutable': [1]}
        second = RoutingFailure('second blocked', {'reason': 'no_path'})
        second._rescue_context = {'failed_net': 'second', 'mutable': [2]}
        items = [item(1), item(2)]

        def regularizer(par, placement, comps, nets, index, metadata, **options):
            placement.positions[0] = (300, 0)
            placement.cell_of[0] = (0, 3)
            metadata = deepcopy(metadata)
            metadata['layout_symmetry'] = symmetry()
            return placement, metadata

        with self.assertRaises(RoutingFailure) as caught:
            self.choose(items, routes={1: first, 2: second}, regularizer=regularizer)
        failure = caught.exception
        self.assertEqual(2, failure.retry_context[0].identifier)
        self.assertEqual((300, 0), failure.retry_context[0].positions[0])
        self.assertEqual((0, 3), failure.retry_context[0].cell_of[0])
        self.assertIn('layout_symmetry', failure.retry_context[4])
        self.assertEqual('second', failure._rescue_context['failed_net'])
        items[1][0].positions[0] = (999, 0)
        second._rescue_context['mutable'].append(3)
        self.assertEqual((300, 0), failure.retry_context[0].positions[0])
        self.assertEqual([2], failure._rescue_context['mutable'])
        self.assertNotIn('_rescue_context', json.dumps(failure.diagnostics))

    def test_failed_later_attempt_without_checkpoint_does_not_reuse_earlier_checkpoint(self):
        first = RoutingFailure('first blocked', {'reason': 'no_path'})
        first._rescue_context = {'failed_net': 'stale'}
        with self.assertRaises(RoutingFailure) as caught:
            self.choose([item(1), item(2)],
                        routes={1: first, 2: RoutingFailure('second blocked', {'reason': 'no_path'})})
        self.assertEqual(2, caught.exception.retry_context[0].identifier)
        self.assertFalse(hasattr(caught.exception, '_rescue_context'))

    def test_all_valid_candidates_are_complete_snapshots_not_just_original_q_best(self):
        items = [item(i) for i in (1, 2, 3)]
        shared_routes, shared_metrics, shared_validation = routed()
        regularized_metadata = {}

        def regularizer(par, placement, comps, nets, index, metadata, **options):
            identifier = metadata['candidate_id']
            metadata['layout_symmetry'] = symmetry(failed=0 if identifier == 2 else 2)
            regularized_metadata[identifier] = metadata
            return placement, metadata

        def generate():
            yield items[0]
            yield items[1]
            # Candidate 2 is worse by Q but will win symmetry selection. It
            # must remain frozen even while the producer/third router runs.
            items[1][0].positions[0] = (999, 999)
            items[1][1][0]['id'] = 'changed_by_producer'
            regularized_metadata[2]['layout_symmetry']['current']['failed_relations'] = 999
            shared_routes[:] = [('other', [((1, 1), (2, 1))])]
            shared_metrics['wirelength'] = 999
            shared_validation['valid'] = False
            yield items[2]

        result, *_ = self.choose(generate(), scores={1: 10, 2: 20, 3: 30},
            reports={1: symmetry(failed=1), 2: symmetry(), 3: symmetry(failed=2)},
            routes={1: routed(), 2: (shared_routes, shared_metrics, shared_validation), 3: routed()},
            regularizer=regularizer)
        self.assertEqual(2, result[0].identifier)
        self.assertEqual((0, 0), result[0].positions[0])
        self.assertEqual('R1', result[1][0]['id'])
        self.assertEqual(0, result[4]['layout_symmetry']['current']['failed_relations'])
        self.assertEqual(routed(), result[5])

    def test_new_regularization_transaction_does_not_reuse_source_before_measurement(self):
        from symmetry_layout import regularize_symmetry
        from types import SimpleNamespace

        placement, comps, nets, index, metadata = item(3)
        placement.column_x = [0, 100]
        placement.row_y = [0]
        group = {'id': 'inherited:pair', 'kind': 'test_pair', 'pairs': [['R1', 'R2']],
                 'members': ['R1', 'R2'], 'tails': [],
                 'pin_pairs': {'R1|R2': [['1', '1']]}}
        metadata['layout_symmetry'] = {
            'relations': [group], 'before': {'residual': 999},
            'after': {'residual': 888}, 'actions': [{'kind': 'source_candidate_action'}]}
        par = SimpleNamespace(power_marker_sym=lambda net: None)
        _, updated = regularize_symmetry(par, placement, comps, nets, index, metadata, seconds=0)
        report = updated['layout_symmetry']
        self.assertEqual(report['current'], report['before'])
        self.assertEqual([], report['actions'])
        self.assertEqual({'residual': 999}, metadata['layout_symmetry']['before'])


class SymmetryPublicFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'small.cir'
        self.raw = b'* unchanged input\r\nR1 input output 1k\r\nC1 output 0 1n\r\n.end\r\n'
        self.source.write_bytes(self.raw)

    def test_cli_default_and_explicit_policy_are_forwarded(self):
        for policy, expected in ((None, 'off'), ('auto', 'auto'), ('off', 'off')):
            with self.subTest(policy=policy):
                args = [str(self.source)] + ([] if policy is None else ['--symmetry-layout', policy])
                with patch('run_flow.run') as run:
                    self.assertEqual(0, run_flow.main(args))
                self.assertEqual(expected, run.call_args.kwargs['symmetry_layout'])

    def test_invalid_policy_rejected_before_candidate_generation(self):
        with patch('structured_placement.create_candidates', side_effect=AssertionError('invalid reached generator')):
            with self.assertRaisesRegex(ValueError, 'symmetry_layout'):
                aesthetic_flow.select_layout('unused.cir', symmetry_layout='invalid')
        with patch('run_flow._normal_result', side_effect=AssertionError('invalid reached layout')):
            with self.assertRaisesRegex(ValueError, 'symmetry_layout'):
                run_flow.run(self.source, symmetry_layout='invalid')
        with patch('run_flow.run', side_effect=AssertionError('invalid reached run')), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                run_flow.main([str(self.source), '--symmetry-layout', 'invalid'])
        self.assertEqual(2, caught.exception.code)

    def test_normal_dispatch_forwards_policy(self):
        for policy, expected in ((None, 'off'), ('auto', 'auto'), ('off', 'off')):
            with self.subTest(policy=policy):
                options = {} if policy is None else {'symmetry_layout': policy}
                with patch('aesthetic_flow.select_layout', return_value='sentinel') as select:
                    result = run_flow._normal_result(self.source, None, 'layered', 'structured',
                        None, 0, 17, 10, None, None, **options)
                self.assertEqual('sentinel', result)
                self.assertEqual(expected, select.call_args.kwargs['symmetry_layout'])

    def test_public_run_default_forwards_off_and_records_requested_policy(self):
        selected = (*item(1), routed())
        with patch('run_flow._normal_result', return_value=selected) as normal, \
                patch('run_flow._finish', return_value='sentinel') as finish, \
                redirect_stdout(io.StringIO()):
            result = run_flow.run(self.source, iterations=0, fallback='off',
                                  out_dir=self.root / 'default')
        self.assertEqual('sentinel', result)
        self.assertEqual('off', normal.call_args.kwargs['symmetry_layout'])
        self.assertEqual('off', finish.call_args.args[0]['requested_symmetry_layout'])

    def generate(self, policy='auto', folder='initial'):
        with redirect_stdout(io.StringIO()):
            return run_flow.run(self.source, iterations=0, seed=17, candidate_id='1',
                quality_seconds=10, out_dir=self.root / folder, symmetry_layout=policy,
                simplify_routes='off', fallback='off')

    def test_off_matches_protected_candidate_one_routes_without_deadline_truncation(self):
        auto = self.generate('auto', 'auto')
        with patch('symmetry_layout.regularize_symmetry', side_effect=AssertionError('off relayout')), \
                patch('symmetry_layout.report_symmetry', side_effect=AssertionError('off measure')):
            off = self.generate('off', 'off')
        for key in ('placement', 'layout_grid', 'routes', 'metrics'):
            self.assertEqual(auto[key], off[key], key)
        self.assertTrue(auto['validation']['valid'])
        self.assertTrue(off['validation']['valid'])
        self.assertEqual(self.raw, self.source.read_bytes())

    def test_saved_reroute_simplify_and_redraw_never_regularize_coordinates(self):
        data = self.generate()
        saved = self.root / 'initial/small-placement.json'
        original_saved = saved.read_bytes()
        renderer = load_local('symmetry_flow_renderer', 'render-clean.py')
        with patch('symmetry_layout.regularize_symmetry', side_effect=AssertionError('saved input relayout')), \
                patch('structured_placement.create_candidates', side_effect=AssertionError('saved input generation')), \
                redirect_stdout(io.StringIO()):
            rerouted = run_flow.reroute_placement(saved, out_dir=self.root / 'rerouted',
                                                simplify_routes='off')
            simplified = run_flow.simplify_placement(saved, out_dir=self.root / 'simplified',
                                                    simplify_routes='off')
            renderer.render(saved, self.root / 'redraw.svg')
        for result in (rerouted, simplified):
            self.assertEqual(data['placement'], result['placement'])
            self.assertEqual(data['layout_grid'], result['layout_grid'])
            self.assertTrue(result['validation']['valid'])
        self.assertEqual(original_saved, saved.read_bytes())
        self.assertEqual((self.root / 'initial/small-clean.svg').read_bytes(),
                         (self.root / 'redraw.svg').read_bytes())
        self.assertEqual(self.raw, self.source.read_bytes())

    def test_final_measurement_preserves_history_but_refreshes_exported_positions(self):
        from symmetry_layout import measure_symmetry

        self.generate()
        saved = self.root / 'initial/small-placement.json'
        data = json.loads(saved.read_text(encoding='utf-8'))
        # This is a saved-layout measurement, not a fresh regularization
        # transaction. Its original before/after/actions remain provenance.
        history = {'before': {'residual': 999}, 'after': {'residual': 888},
                   'actions': [{'kind': 'original_action'}]}
        data['layout_symmetry'].update(deepcopy(history))
        data['layout_symmetry']['current'] = {'residual': 777}
        saved.write_text(json.dumps(data), encoding='utf-8')
        _, _, _, placement, comps, _, index = load_placement(saved)
        expected = measure_symmetry(placement, comps, index,
                                    data['layout_symmetry']['relations'])
        with redirect_stdout(io.StringIO()), \
                patch('symmetry_layout.regularize_symmetry', side_effect=AssertionError('saved input relayout')):
            refreshed = run_flow.simplify_placement(saved, out_dir=self.root / 'refreshed',
                                                   simplify_routes='off')
        report = refreshed['layout_symmetry']
        for key, value in history.items():
            self.assertEqual(value, report[key], key)
        self.assertEqual(expected, report['current'])
        self.assertEqual('final_output', report['measurement_stage'])


class SymmetryTopologyInvarianceTests(unittest.TestCase):
    """Compare complete relationships after names/order change, never counts."""

    _fields = {'MOS': ('d', 'g', 's', 'bulk'), 'BJT': ('c', 'b', 'e'),
               'PASSIVE': ('a', 'b'), 'VSRC': ('p', 'n'), 'ISRC': ('p', 'n'),
               'VCVS': ('op', 'on', 'ip', 'in')}

    @staticmethod
    def relationships(groups, inverse):
        result = []
        for group in groups:
            pairs = []
            for left, right in group['pairs']:
                pins = group.get('pin_pairs', {}).get(left + '|' + right, [])
                forward = (inverse[left], inverse[right], tuple(sorted(tuple(p) for p in pins)))
                reverse = (inverse[right], inverse[left], tuple(sorted((b, a) for a, b in pins)))
                pairs.append(min(forward, reverse))
            result.append((group['kind'], group.get('axis_family'), group.get('scope'),
                           tuple(sorted(pairs)), tuple(sorted(inverse[n] for n in group['tails']))))
        return sorted(result, key=repr)

    def check_transform(self, devices, names=None, nets=None, seed=17):
        from circuit_structures import analyze_structures
        from netlist_core import to_document
        from symmetry_layout import build_symmetry_groups

        names, nets = names or {n: n for n in devices}, nets or {}
        original_groups = build_symmetry_groups(to_document(devices)['components'],
                                                analyze_structures(devices))
        self.assertTrue(original_groups, 'This regression must exercise a recognized symmetry group')
        rng = random.Random(seed)
        order = list(devices)
        rng.shuffle(order)
        transformed = {}
        for name in order:
            device = deepcopy(devices[name])
            for field in self._fields[device['cls']]:
                device[field] = nets.get(device[field], device[field])
            transformed[names[name]] = device
        components = to_document(transformed)['components']
        rng.shuffle(components)
        actual_groups = build_symmetry_groups(components, analyze_structures(transformed))
        self.assertEqual(self.relationships(original_groups, {n: n for n in devices}),
                         self.relationships(actual_groups, {new: old for old, new in names.items()}))

    def dataset(self, stem):
        from netlist_core import parse_netlist

        path = Path(__file__).resolve().parents[1] / 'test_data' / '16+' / (stem + '.cir')
        if not path.is_file():
            self.skipTest('Optional local ground-truth netlist is not present')
        contents = path.read_text(encoding='utf-8-sig')
        match = re.search(r'^\s*\.subckt\s+(\S+)', contents, re.I | re.M)
        return parse_netlist(path, cell=match.group(1) if match else None)

    def test_real_expanded_input_branch_keeps_all_partners_after_instance_rename(self):
        devices = self.dataset('circuit-implementation-of-the-amplifier-and')
        names = {name: name for name in devices}
        names['M1'], names['M3'] = 'M3', 'M1'
        self.check_transform(devices, names=names)

    def test_real_folded_output_group_keeps_partners_after_signal_rename(self):
        devices = self.dataset('schematic-of-the-folded-cascode-amplifier-ota1-with-its-cmfb-circuit')
        self.check_transform(devices, nets={'net0': 'net6', 'net6': 'net0'})

    def test_synthetic_multi_depth_load_tree_is_name_and_order_independent(self):
        devices = {
            'input_left': dict(cls='MOS', type='NMOS', d='out_left', g='input_a', s='tail', bulk='0'),
            'input_right': dict(cls='MOS', type='NMOS', d='out_right', g='input_b', s='tail', bulk='0'),
            'tail_supply': dict(cls='ISRC', type='I', p='tail', n='0'),
        }
        for side in ('left', 'right'):
            previous = 'out_' + side
            for depth in range(5):
                next_net = side + '_stage_' + str(depth)
                devices[f'{side}_load_{depth}'] = dict(cls='PASSIVE', type='R',
                    a=previous, b=next_net)
                previous = next_net
        all_nets = {device[field] for device in devices.values()
                    for field in self._fields[device['cls']]}
        for seed in range(10):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                names = {name: f'device_{rng.getrandbits(64):016x}' for name in devices}
                nets = {net: f'signal_{rng.getrandbits(64):016x}'
                        for net in sorted(all_nets) if net != '0'}
                self.check_transform(devices, names=names, nets=nets, seed=seed)


if __name__ == '__main__':
    unittest.main()
