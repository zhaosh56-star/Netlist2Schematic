"""Candidate-three feedback is bounded, transactional and never invokes A*."""
from copy import deepcopy
import time
import unittest
from unittest.mock import patch

from netlist2schematic.plugins.readability import readability_routing as feedback
from netlist2schematic.plugins.readability.readability_layout import measure_readability, readability_rank
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.routing_rules import RoutingOptions, build_scene
from netlist2schematic.routing.sa_routing import _verify
from netlist2schematic.layout import structured_placement as structured
from tests.test_passive_placement import base_placement, resistor, mos

PAR = structured.par


def fixture(fixed=False):
    devices = {'first': resistor('a', 'left'), 'second': resistor('a', 'right'),
               'unrelated': mos('md', 'mg', 'ms')}
    pl, comps, nets, index = base_placement(devices,
        {'first': (1, 1), 'second': (1, 5), 'unrelated': (2, 6)})
    if fixed:
        pl.fixed = {index['first'], index['second']}
        pl.fixed_cells = {i: pl.cell_of[i] for i in pl.fixed}
    scene = build_scene(PAR, nets, pl, index)
    first, second = scene.pins['a']
    top = min(first.escape[1], second.escape[1])-400
    path = [first.escape, (first.escape[0], top), (second.escape[0], top), second.escape]
    routed = [(net, list(segments)) for net, segments in scene.fixed_segments.items()]
    for net, segments in routed:
        if net == 'a':
            segments.extend(zip(path, path[1:]))
    checked, metrics, _ = _verify(PAR, nets, pl, index, routed, scene)
    plan = {'version': 'test-fixture', 'signature': 'shared-source', 'supported': True,
            'patterns': [], 'junctions': {}, 'links': [], 'series_axes': [],
            'sources': [], 'compact_groups': [], 'coverage': {}, 'ambiguities': []}
    metadata = {'candidate_id': 2, 'layout_readability': {'plan': plan, 'applied': True}}
    return pl, comps, nets, index, metadata, (checked, metrics, {'valid': True}), scene


def call(item, **kwargs):
    return feedback.feedback_readability(PAR, *item[:6], **kwargs)


def pure_mos_fixture(fixed=False):
    devices = {'first': mos('left_d', 'a', 'left_s'),
               'second': mos('right_d', 'a', 'right_s')}
    pl, comps, nets, index = base_placement(devices, {'first': (1, 1), 'second': (1, 5)})
    if fixed:
        pl.fixed = set(index.values())
        pl.fixed_cells = {i: pl.cell_of[i] for i in pl.fixed}
    scene = build_scene(PAR, nets, pl, index)
    a, b = scene.pins['a']
    top = min(a.escape[1], b.escape[1])-400
    path = [a.escape, (a.escape[0], top), (b.escape[0], top), b.escape]
    routes = [(net, list(runs)) for net, runs in scene.fixed_segments.items()]
    for net, runs in routes:
        if net == 'a':
            runs.extend(zip(path, path[1:]))
    checked, metrics, _ = _verify(PAR, nets, pl, index, routes, scene)
    plan = {'version': 'unsupported-but-complete', 'signature': 'pure-mos-source',
            'supported': False, 'patterns': [], 'coverage': {}, 'ambiguities': []}
    meta = {'candidate_id': 2, 'layout_readability': {'policy': 'auto', 'applied': False, 'plan': plan}}
    return pl, comps, nets, index, meta, (checked, metrics, {'valid': True}), scene


class ReadabilityFeedbackTests(unittest.TestCase):
    def test_pure_mos_long_branch_can_improve_without_inventing_organization_metrics(self):
        import json
        item = pure_mos_fixture()
        with patch('netlist2schematic.routing.orthogonal_router.route_scene', side_effect=AssertionError('A* forbidden')), \
                patch('netlist2schematic.routing.orthogonal_router._find_path', side_effect=AssertionError('A* forbidden')):
            result = call(item)
        self.assertIsNotNone(result)
        report = result[4]['layout_readability']['routing_feedback']
        self.assertTrue(report['organization_not_available'])
        self.assertEqual('unavailable_no_supported_relations', report['organization_guard'])
        self.assertFalse(report['before_organization']['measurable'])
        self.assertIsNone(report['before_organization']['violations'])
        self.assertIsNone(report['after_organization']['violations'])
        self.assertEqual(item[0].row_of, result[0].row_of)
        self.assertLess(report['after_risk']['key'], report['before_risk']['key'])
        json.dumps(report, allow_nan=False)

    def test_pure_mos_without_supported_relations_still_preserves_fixed_devices(self):
        item = pure_mos_fixture(fixed=True)
        original = deepcopy(item[0].__dict__)
        self.assertIsNone(call(item))
        self.assertEqual(original['cell_of'], item[0].cell_of)
        self.assertEqual(original['orient'], item[0].orient)

    def test_supported_but_unmeasurable_plan_does_not_bypass_organization_guard(self):
        item = pure_mos_fixture()
        item[4]['layout_readability']['plan']['supported'] = True
        with patch('netlist2schematic.plugins.readability.readability_routing.measure_readability', return_value={
                'supported': True, 'measurable': False, 'violations': None}), \
                patch('netlist2schematic.plugins.readability.readability_routing.build_scene', side_effect=AssertionError('must reject early')):
            self.assertIsNone(call(item))

    def test_actual_long_free_branch_has_two_original_pin_groups(self):
        item = fixture()
        pl, comps, nets, index, metadata, routed, scene = item
        branches = feedback.detouring_branches(scene, routed[0], 120)
        self.assertEqual(1, len(branches))
        branch = branches[0]
        self.assertEqual('a', branch['net'])
        self.assertGreaterEqual(branch['detour_factor'], 2)
        self.assertGreaterEqual(branch['length'], 360)
        self.assertGreaterEqual(branch['excess'], 240)
        self.assertEqual({index['first'], index['second']},
            {i for group in branch['port_groups'] for i, pin in group})

    def test_thresholds_exclude_short_or_nearly_direct_paths(self):
        item = fixture()
        routed, scene = item[5][0], item[6]
        self.assertEqual([], feedback.detouring_branches(scene, routed, 10000))
        first, second = scene.pins['a']
        height = min(first.escape[1], second.escape[1])-20
        routes = [(n, list(runs)) for n, runs in scene.fixed_segments.items()]
        for n, runs in routes:
            if n == 'a':
                path = [first.escape, (first.escape[0], height), (second.escape[0], height), second.escape]
                runs.extend(zip(path, path[1:]))
        self.assertEqual([], feedback.detouring_branches(scene, routes, 120))

    def test_actual_feedback_preserves_rows_fixed_rules_and_unrelated_components(self):
        item = fixture()
        pl, comps, nets, index, metadata, routed, scene = item
        original = deepcopy(pl)
        with patch('netlist2schematic.routing.orthogonal_router.route_scene', side_effect=AssertionError('A* forbidden')), \
                patch('netlist2schematic.routing.orthogonal_router._find_path', side_effect=AssertionError('A* forbidden')):
            result = call(item)
        self.assertIsNotNone(result)
        trial, new_comps, new_nets, new_index, meta = result
        report = meta['layout_readability']['routing_feedback']
        self.assertLess(tuple(report['after_risk']['key']), tuple(report['before_risk']['key']))
        self.assertLessEqual(report['estimated_hpwl_after'], report['estimated_hpwl_limit'])
        self.assertLessEqual(readability_rank(report['after_organization']), readability_rank(report['before_organization']))
        self.assertEqual(trial.row_of, original.row_of)
        self.assertEqual(trial.column_x, original.column_x)
        self.assertEqual(trial.row_y, original.row_y)
        outsider = index['unrelated']
        self.assertEqual((trial.cell_of[outsider], trial.orient[outsider]),
                         (original.cell_of[outsider], original.orient[outsider]))
        self.assertEqual(pl.cell_of, original.cell_of)
        self.assertEqual(pl.orient, original.orient)
        self.assertEqual(pl.rng.getstate(), original.rng.getstate())
        self.assertEqual(comps, new_comps)
        self.assertEqual(nets, new_nets)
        self.assertEqual(index, new_index)
        self.assertNotIn('routing_feedback', metadata['layout_readability'])
        self.assertFalse(report['sa_hot_path']['enabled'])

    def test_no_free_related_device_means_no_proposal_and_no_axis_expansion(self):
        item = fixture(fixed=True)
        axes = deepcopy((item[0].column_x, item[0].row_y))
        self.assertIsNone(call(item))
        self.assertEqual(axes, (item[0].column_x, item[0].row_y))

    def test_static_rejection_rolls_back_every_trial(self):
        item = fixture(); before = deepcopy(item[0].__dict__)
        statistics = {}
        with patch('netlist2schematic.plugins.readability.readability_routing._static_safe', return_value=False):
            self.assertIsNone(call(item, statistics=statistics))
        self.assertGreater(statistics['candidates'], 0)
        self.assertEqual(item[0].cell_of, before['cell_of'])
        self.assertEqual(item[0].orient, before['orient'])
        self.assertEqual(item[0].rng.getstate(), before['rng'].getstate())

    def test_hpwl_guard_is_measured_against_original_reference(self):
        item = fixture(); original = tuple(item[0].cell_of)
        with patch('netlist2schematic.plugins.readability.readability_routing._hpwl', side_effect=lambda p, pl, n, ix:
                100 if tuple(pl.cell_of) == original else 116):
            self.assertIsNone(call(item))

    def test_worse_organization_is_not_traded_for_better_escape_risk(self):
        item = fixture(); original = tuple(item[0].cell_of)
        real = feedback.measure_readability

        def measure(pl, comps, index, plan):
            result = real(pl, comps, index, plan)
            result['violations'] = 0 if tuple(pl.cell_of) == original else 1
            result['passive_supported'] = True
            return result

        with patch('netlist2schematic.plugins.readability.readability_routing.measure_readability', side_effect=measure):
            self.assertIsNone(call(item))

    def test_expired_deadline_does_not_start_scene_or_move_anything(self):
        item = fixture()
        statistics = {'candidates': 123}
        with patch('netlist2schematic.plugins.readability.readability_routing.build_scene', side_effect=AssertionError('expired')):
            self.assertIsNone(call(item, deadline=time.perf_counter()-1, statistics=statistics))
        self.assertEqual(0, statistics['candidates'])

    def test_candidate_limit_stops_without_spending_other_recovery_budgets(self):
        item = fixture()
        with patch('netlist2schematic.plugins.readability.readability_routing.MAX_TRIALS', 0):
            self.assertIsNone(call(item))

    def test_unverified_route_and_nonfinite_deadline_are_rejected(self):
        item = fixture()
        invalid = (*item[:5], (*item[5][:2], {'valid': False}), item[6])
        with self.assertRaisesRegex(ValueError, 'validated'):
            call(invalid)
        with self.assertRaisesRegex(ValueError, 'deadline'):
            call(item, deadline=float('inf'))

    def test_mismatched_saved_pin_coordinates_cannot_drive_feedback(self):
        item = fixture()
        item[0].column_x = [x+10 for x in item[0].column_x]
        self.assertIsNone(call(item))

    def test_leaf_move_is_collective_but_clock_orientation_remains_protected(self):
        devices = {'left': mos('a', 'ga', 'sa'), 'right': mos('b', 'gb', 'sb')}
        pl, comps, nets, index = base_placement(devices,
            {'left': (1, 2), 'right': (1, 3)}, blocks=[['left', 'right']])
        branch = {'port_groups': [[(index['left'], 'D')], [(index['right'], 'D')]]}
        meta = {'role_adjustments': [{'kind': 'latch_clock_access', 'device': 'left'}]}
        candidates = list(feedback._trials(pl, comps, index, branch, meta, time.perf_counter()+5))
        self.assertTrue(candidates)
        for trial, action in candidates:
            if action['kind'] == 'active_leaf_column':
                self.assertEqual(set(action['members']), {'left', 'right'})
                self.assertEqual(trial.cell_of[0][1]-pl.cell_of[0][1], trial.cell_of[1][1]-pl.cell_of[1][1])
            self.assertEqual(trial.orient[index['left']], pl.orient[index['left']])
            self.assertEqual(trial.row_of, pl.row_of)

    def test_risk_is_static_and_does_not_claim_existing_route_is_safe(self):
        item = fixture()
        branch = feedback.detouring_branches(item[6], item[5][0], 120)[0]
        risk = feedback.escape_risk(item[6], 'a', branch['port_groups'])
        self.assertIn('without_signal_occupancy', risk['scope'])
        self.assertTrue(risk['escape_path'])

    def test_risk_accounts_for_bodies_and_power_markers_on_the_l_path(self):
        item = fixture()
        scene = deepcopy(item[6])
        branch = feedback.detouring_branches(scene, item[5][0], 120)[0]
        baseline = feedback.escape_risk(scene, 'a', branch['port_groups'])
        x, y = baseline['escape_path'][0]
        scene.body_rects.append((999, (x+50, y-10, x+70, y+10)))
        scene.marker_rects.append(('vdd', (x+90, y-10, x+110, y+10)))
        crowded = feedback.escape_risk(scene, 'a', branch['port_groups'])
        self.assertGreater(crowded['key'][0], baseline['key'][0])
        self.assertIn('body:999', crowded['static_blockers'])
        self.assertIn('vdd', crowded['static_blockers'])

    def test_renaming_components_and_nets_does_not_select_a_special_example(self):
        item = fixture()
        original = call(item)
        pl, comps, nets, index, meta, routed, _ = deepcopy(item)
        names = {name: 'renamed_'+name for name in index}
        net_names = {name: 'renamed_'+name for name in nets}
        for comp in comps:
            comp['id'] = names[comp['id']]
            comp['nodes'] = {pin: net_names[net] for pin, net in comp['nodes'].items()}
        pl.comps = comps
        new_index = {names[name]: i for name, i in index.items()}
        new_nets = {net_names[net]: [(names[name], pin) for name, pin in pins] for net, pins in nets.items()}
        new_routes = [(net_names[net], runs) for net, runs in routed[0]]
        scene = build_scene(PAR, new_nets, pl, new_index)
        checked, metrics, _ = _verify(PAR, new_nets, pl, new_index, new_routes, scene)
        renamed = feedback.feedback_readability(PAR, pl, comps, new_nets, new_index, meta,
            (checked, metrics, {'valid': True}))
        self.assertIsNotNone(original)
        self.assertIsNotNone(renamed)
        self.assertEqual(original[0].cell_of, renamed[0].cell_of)
        self.assertEqual(original[4]['layout_readability']['routing_feedback']['before_risk']['key'],
                         renamed[4]['layout_readability']['routing_feedback']['before_risk']['key'])

    def test_translation_preserves_feedback_decision_and_risk(self):
        item = fixture()
        original = call(item)
        pl, comps, nets, index, meta, routed, _ = deepcopy(item)
        dx, dy = 1200, -740
        pl.column_x = [x+dx for x in pl.column_x]
        pl.row_y = [y+dy for y in pl.row_y]
        shifted = [(net, [((a[0]+dx, a[1]+dy), (b[0]+dx, b[1]+dy)) for a, b in runs])
                   for net, runs in routed[0]]
        scene = build_scene(PAR, nets, pl, index)
        checked, metrics, _ = _verify(PAR, nets, pl, index, shifted, scene)
        result = feedback.feedback_readability(PAR, pl, comps, nets, index, meta,
            (checked, metrics, {'valid': True}))
        self.assertIsNotNone(result)
        self.assertEqual(original[0].cell_of, result[0].cell_of)
        self.assertEqual(original[4]['layout_readability']['routing_feedback']['after_risk']['key'],
                         result[4]['layout_readability']['routing_feedback']['after_risk']['key'])

    def test_timeout_after_verified_trial_keeps_that_proposal_without_partial_commit(self):
        item = fixture()

        def once(pl, comps, index, branch, metadata, deadline):
            trial = deepcopy(pl)
            cells = list(pl.cell_of)
            i = index['first']; row, col = cells[i]
            cells[i] = row, col+1
            feedback._sync(trial, cells, pl.orient, (i,))
            yield trial, {'kind': 'passive_neighbor_column', 'members': ['first']}
            raise RoutingFailure('timeout', {'reason': 'quality_time_budget'})

        with patch('netlist2schematic.plugins.readability.readability_routing._trials', side_effect=once):
            result = call(item)
        self.assertIsNotNone(result)
        report = result[4]['layout_readability']['routing_feedback']
        self.assertEqual('time_budget', report['stop_reason'])
        self.assertLess(report['after_risk']['key'], report['before_risk']['key'])


class InitialEscapeCostTests(unittest.TestCase):
    @staticmethod
    def item():
        devices = {'gate': mos('drain', 'signal', 'source'),
                   'load': resistor('signal', 'other'),
                   'barrier': mos('bd', 'bg', 'bs')}
        pl, comps, nets, index = base_placement(devices,
            {'gate': (1, 1), 'load': (1, 5), 'barrier': (2, 3)})
        plan = {'signature': 'unsupported-pure-topology', 'supported': False,
                'patterns': [], 'coverage': {}, 'ambiguities': []}
        return pl, comps, nets, index, plan

    def test_real_geometry_cost_is_diagnostic_when_organization_is_unknown(self):
        pl, comps, nets, index, plan = self.item()
        before = (deepcopy(pl.cell_of), deepcopy(pl.orient), pl.rng.getstate())
        with patch('netlist2schematic.routing.orthogonal_router._find_path', side_effect=AssertionError('A* forbidden')), \
                patch('netlist2schematic.layout.structured_placement._anneal', side_effect=AssertionError('SA forbidden')):
            estimate, audit = feedback.make_initializer_cost(PAR, pl, comps, nets, index, plan)
            self.assertIsNotNone(estimate)
            self.assertEqual(audit['before']['cost'], estimate(pl))
        self.assertEqual(before, (pl.cell_of, pl.orient, pl.rng.getstate()))
        self.assertTrue(audit['organization_not_available'])
        self.assertFalse(audit['sa_hot_path']['enabled'])
        self.assertEqual(1, audit['evaluations'])
        self.assertEqual(0, audit['trial_evaluations'])
        import json
        json.dumps(audit, allow_nan=False)

    def test_score_uses_actual_escape_geometry_and_declared_weights(self):
        pl, comps, nets, index, plan = self.item()
        estimate, audit = feedback.make_initializer_cost(PAR, pl, comps, nets, index, plan)
        measured = audit['before']
        scene = build_scene(PAR, nets, pl, index)
        pair = [(index[name], pin) for name, pin in measured['nets'][0]['pairs'][0]['ports']]
        risk = feedback.escape_risk(scene, 'signal', [[pair[0]], [pair[1]]])
        expected = 3*len(risk['static_blockers'])+risk['heading_reversals']+.25*risk['escape_distance']/100
        self.assertAlmostEqual(expected, measured['escape_risk'])
        self.assertAlmostEqual(measured['estimated_hpwl']/100+measured['readability_cost']+expected, estimate(pl))
        self.assertNotEqual(abs(pl.pos_of(index['gate'])[0]-pl.pos_of(index['load'])[0]), risk['escape_distance'])

    def test_exact_state_cache_does_not_charge_baseline_or_repeated_trials(self):
        pl, comps, nets, index, plan = self.item()
        estimate, audit = feedback.make_initializer_cost(PAR, pl, comps, nets, index, plan)
        with patch('netlist2schematic.plugins.readability.readability_routing.build_scene', side_effect=AssertionError('cached scene must not rebuild')):
            estimate(pl)
            estimate(deepcopy(pl))
        self.assertEqual(0, audit['trial_evaluations'])
        trial = deepcopy(pl)
        trial.column_x = [x+37 for x in trial.column_x]
        trial.row_y = [y-89 for y in trial.row_y]
        self.assertAlmostEqual(estimate(pl), estimate(trial))
        self.assertEqual(1, audit['trial_evaluations'])
        self.assertEqual(2, audit['evaluations'])
        self.assertIsNotNone(estimate.peek(trial))
        trial.orient[index['gate']] = 1
        self.assertIsNone(estimate.peek(trial))

    def test_pairs_are_frozen_after_device_motion_and_not_reselected_to_hide_risk(self):
        devices = {f'gate{i}': mos(f'd{i}', 'signal', f's{i}') for i in range(6)}
        cells = {name: (1, i) for i, name in enumerate(devices)}
        pl, comps, nets, index = base_placement(devices, cells)
        plan = self.item()[-1]
        estimate, audit = feedback.make_initializer_cost(PAR, pl, comps, nets, index, plan)
        catalogue = deepcopy(audit['catalogue'])
        self.assertEqual(8, audit['pair_count'])
        self.assertEqual(15, audit['possible_pair_count'])
        self.assertEqual(7, audit['uncovered_pair_count'])
        trial = deepcopy(pl)
        cells = list(trial.cell_of)
        cells[0], cells[5] = cells[5], cells[0]
        feedback._sync(trial, cells, trial.orient, ())
        estimate(trial)
        self.assertEqual(catalogue, audit['catalogue'])
        before_ports = [row['ports'] for row in audit['before']['nets'][0]['pairs']]
        self.assertEqual(before_ports, [row['ports'] for row in estimate.peek(trial)['nets'][0]['pairs']])

    def test_no_control_ports_preserves_plain_passive_initializer(self):
        pl, comps, nets, index = base_placement({'left': resistor('x', 'y'), 'right': resistor('x', 'z')},
                                               {'left': (1, 1), 'right': (1, 3)})
        estimate, audit = feedback.make_initializer_cost(PAR, pl, comps, nets, index, self.item()[-1])
        self.assertIsNone(estimate)
        self.assertEqual('no_control_port_pairs', audit['reason'])
        self.assertEqual(0, audit['pair_count'])
        self.assertEqual(0, audit['evaluations'])

    def test_power_control_connections_are_not_candidates(self):
        pl, comps, nets, index = base_placement({'left': mos('a', 'vdd'), 'right': mos('b', 'vdd')},
                                               {'left': (1, 1), 'right': (1, 3)})
        estimate, audit = feedback.make_initializer_cost(PAR, pl, comps, nets, index, self.item()[-1])
        self.assertIsNone(estimate)
        self.assertEqual('no_control_port_pairs', audit['reason'])

    def test_broken_supported_organization_cannot_use_a_zero_penalty(self):
        pl, comps, nets, index, plan = self.item()
        plan['supported'] = True
        with patch('netlist2schematic.plugins.readability.readability_routing.measure_readability', return_value={'measurable': False}), \
                patch('netlist2schematic.plugins.readability.readability_routing.build_scene', side_effect=AssertionError('reject before scene')):
            estimate, audit = feedback.make_initializer_cost(PAR, pl, comps, nets, index, plan)
        self.assertIsNone(estimate)
        self.assertEqual('unknown_organization_measurement', audit['reason'])

    def test_expired_factory_skips_and_trial_timeout_never_caches_partial_score(self):
        pl, comps, nets, index, plan = self.item()
        with patch('netlist2schematic.plugins.readability.readability_routing.build_scene', side_effect=AssertionError('expired')):
            estimate, audit = feedback.make_initializer_cost(PAR, pl, comps, nets, index, plan,
                                                              deadline=time.perf_counter()-1)
        self.assertIsNone(estimate)
        self.assertEqual('quality_time_budget', audit['reason'])
        estimate, audit = feedback.make_initializer_cost(PAR, pl, comps, nets, index, plan)
        before = deepcopy(audit['before'])
        trial = deepcopy(pl)
        trial.column_x = [x+20 for x in trial.column_x]
        with patch('netlist2schematic.plugins.readability.readability_routing.escape_risk', side_effect=RoutingFailure('timeout', {'reason': 'quality_time_budget'})):
            with self.assertRaises(RoutingFailure):
                estimate(trial)
        self.assertEqual(before, estimate.peek(pl))
        self.assertIsNone(estimate.peek(trial))
        self.assertEqual(1, audit['evaluations'])
        self.assertEqual(0, audit['trial_evaluations'])
        self.assertEqual('time_budget', audit['status'])

    def test_budget_counts_unique_completed_trials_separately_from_baseline(self):
        pl, comps, nets, index, plan = self.item()
        with patch('netlist2schematic.plugins.readability.readability_routing.INITIALIZER_TRIALS', 1):
            estimate, audit = feedback.make_initializer_cost(PAR, pl, comps, nets, index, plan)
        self.assertEqual(1, audit['max_trials'])
        trial = deepcopy(pl)
        trial.column_x = [x+20 for x in trial.column_x]
        estimate(trial)
        estimate(pl)
        estimate(trial)
        trial.column_x = [x+20 for x in trial.column_x]
        with self.assertRaises(RoutingFailure) as caught:
            estimate(trial)
        self.assertEqual('initializer_cost_trial_limit', caught.exception.diagnostics['reason'])
        self.assertEqual(1, audit['trial_evaluations'])
        self.assertEqual(2, audit['evaluations'])

    def test_many_control_nets_reduce_the_trial_quota_without_hiding_pair_coverage(self):
        devices = {f'g{n}_{i}': mos(f'd{n}_{i}', f'signal{n}', f's{n}_{i}')
                   for n in range(4) for i in range(6)}
        pl, comps, nets, index = base_placement(devices, {name: (i//6, i%6) for i, name in enumerate(devices)})
        estimate, audit = feedback.make_initializer_cost(PAR, pl, comps, nets, index, self.item()[-1])
        self.assertIsNotNone(estimate)
        self.assertEqual(32, audit['pair_count'])
        self.assertEqual(16, audit['max_trials'])
        self.assertEqual(60, audit['possible_pair_count'])
        self.assertTrue(all(len(pairs) <= 8 for pairs in audit['catalogue'].values()))

    def test_renaming_preserves_topological_cost_and_unknown_coverage(self):
        pl, comps, nets, index, plan = self.item()
        _, before = feedback.make_initializer_cost(PAR, pl, comps, nets, index, plan)
        names = {name: f'new_{len(name)}_{number}' for number, name in enumerate(index)}
        netnames = {net: f'wire{number}' for number, net in enumerate(nets)}
        for comp in comps:
            comp['id'] = names[comp['id']]
            comp['nodes'] = {pin: netnames[net] for pin, net in comp['nodes'].items()}
        renamed_nets = {netnames[net]: [(names[name], pin) for name, pin in ports] for net, ports in nets.items()}
        renamed_index = {names[name]: i for name, i in index.items()}
        _, after = feedback.make_initializer_cost(PAR, pl, comps, renamed_nets, renamed_index, plan)
        self.assertAlmostEqual(before['before']['cost'], after['before']['cost'])
        self.assertEqual(before['pair_count'], after['pair_count'])
        self.assertTrue(after['organization_not_available'])

    def test_invalid_deadline_and_options_are_explicit_errors(self):
        args = (PAR, *self.item())
        with self.assertRaisesRegex(ValueError, 'deadline'):
            feedback.make_initializer_cost(*args, deadline=float('nan'))
        with self.assertRaisesRegex(ValueError, 'options'):
            feedback.make_initializer_cost(*args, options={})


if __name__ == '__main__':
    unittest.main()
