"""Source-only passive roles, shared junctions, and bounded reversible seeds."""
from copy import deepcopy
import json
from pathlib import Path
import random
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from circuit_structures import analyze_structures
from device_leveling import compute_levels
from netlist_core import to_document
from readability_layout import (build_readability_plan, junction_point,
    has_strong_passive_plan, measure_readability, prepare_readability_placement,
    readability_rank, score_readability)
from tests.test_passive_placement import convert, mos, resistor, source


def plan_for(devices, cells=None):
    document = to_document(devices)
    comps, nets = document['components'], document['nets']
    index = {row['id']: i for i, row in enumerate(comps)}
    rows = [cells[row['id']][0] if cells else 1 for row in comps]
    plan = build_readability_plan(devices, comps, nets, index,
                                  analyze_structures(devices), compute_levels(devices), rows)
    return plan, comps, nets, index


def network():
    return dict(a=mos('left', 'ga'), b=mos('right', 'gb'),
        r=resistor('left', 'middle0'), c=resistor('middle0', 'middle1', 'C'),
        l=resistor('middle1', 'right', 'L'), bypass=resistor('left', 'right', 'C'))


def bank_fixture():
    devices = dict(a=mos('signal', 'gate'), r=resistor('vdd', 'signal'),
                   c=resistor('vdd', 'signal', 'C'), l=resistor('vdd', 'signal', 'L'))
    cells = dict(a=(2, 3), r=(0, 0), c=(1, 2), l=(3, 5))
    pl, comps, nets, index, structs = convert(devices, cells)
    plan = build_readability_plan(devices, comps, nets, index, structs, compute_levels(devices),
                                  [cell[0] for cell in pl.cell_of])
    return devices, pl, comps, nets, index, plan


def semantics(plan, inverse=None):
    inverse = inverse or {}
    name = lambda value: inverse.get(value, value)
    return sorted((row['kind'], tuple(sorted(name(value) for value in row['members'])))
                  for row in plan['patterns'])


def snapshot(pl):
    result = deepcopy({key: value for key, value in vars(pl).items() if key != 'rng'})
    result['rng_state'] = pl.rng.getstate()
    return result


class ReadabilityPlanTests(unittest.TestCase):
    def test_strong_plan_requires_multiple_related_passives_or_a_series_axis(self):
        bank = bank_fixture()[-1]
        bridge, _, _, _ = plan_for(network())
        chain_devices = network()
        del chain_devices['bypass']
        chain, _, _, _ = plan_for(chain_devices)
        arms, _, _, _ = plan_for(dict(a=mos('left', 'ga'), b=mos('right', 'gb'),
            host=mos('out', 'sense'), r=resistor('left', 'sense'), s=resistor('right', 'sense')))
        self.assertTrue(any(row['kind'] == 'two_arm_join' for row in arms['patterns']))
        for plan in (bank, bridge, chain, arms):
            self.assertTrue(has_strong_passive_plan(plan))
        single, _, _, _ = plan_for(dict(a=mos('out', 'in'), c=resistor('out', '0', 'C')))
        self.assertFalse(has_strong_passive_plan(single))
        only_source, _, _, _ = plan_for(dict(a=mos('out', 'in'), v=source('VSRC', 'in')))
        self.assertFalse(has_strong_passive_plan(only_source))
        self.assertFalse(has_strong_passive_plan({'patterns': [
            {'kind': 'parallel_bank', 'members': ['c']},
            {'kind': 'series_chain', 'members': ['c', 'c']}]}))
        self.assertTrue(has_strong_passive_plan({'series_axes': [{'a': 'r', 'b': 'l'}]}))

    def test_parallel_bank_keeps_multiple_real_junction_anchors(self):
        devices = dict(a=mos('signal', 'ga'), b=mos('signal', 'gb'),
            r=resistor('vdd', 'signal'), c=resistor('vdd', 'signal', 'C'), l=resistor('vdd', 'signal', 'L'))
        frozen = deepcopy(devices)
        plan, _, _, _ = plan_for(devices)
        banks = [row for row in plan['patterns'] if row['kind'] == 'parallel_bank']
        self.assertEqual(len(banks), 1)
        self.assertEqual(set(banks[0]['members']), {'r', 'c', 'l'})
        self.assertEqual(plan['junctions']['signal']['conduction_ports'], [['a', 'D'], ['b', 'D']])
        self.assertEqual(banks[0]['owner_junctions'], ['signal'])
        self.assertEqual(devices, frozen)
        json.dumps(plan, allow_nan=False)

    def test_series_path_and_parallel_alternative_are_one_nested_bridge(self):
        plan, _, _, _ = plan_for(network())
        chains = [row for row in plan['patterns'] if row['kind'] == 'series_chain']
        bridges = [row for row in plan['patterns'] if row['kind'] == 'multi_path_bridge']
        self.assertEqual(len(chains), 1)
        self.assertEqual(len(bridges), 1)
        self.assertEqual(set(chains[0]['members']), {'r', 'c', 'l'})
        self.assertEqual(set(bridges[0]['members']), {'r', 'c', 'l', 'bypass'})
        self.assertEqual(chains[0]['parent'], bridges[0]['id'])
        self.assertEqual(set(bridges[0]['endpoint_junctions']), {'left', 'right'})
        keys = [(row['member'], row['pin'], row['net']) for row in plan['links']]
        self.assertEqual(len(keys), len(set(keys)))

    def test_control_tap_breaks_degree_two_chain(self):
        devices = network()
        del devices['bypass']
        devices['tap'] = mos('unused', 'middle0')
        plan, _, _, _ = plan_for(devices)
        self.assertFalse(any(set(row['members']) == {'r', 'c', 'l'} for row in plan['patterns']))
        self.assertTrue(any(row['kind'] == 'series_chain' and set(row['members']) == {'c', 'l'}
                            for row in plan['patterns']))

    def test_two_arm_join_requires_equal_resistors_and_control_only_center(self):
        devices = dict(a=mos('left', 'sense'), b=mos('right', 'sense'),
            r1=resistor('left', 'sense'), r2=resistor('right', 'sense'), cap=resistor('vdd', 'sense', 'C'))
        plan, _, _, _ = plan_for(devices)
        join = next(row for row in plan['patterns'] if row['kind'] == 'two_arm_join')
        self.assertEqual(join['center_junction'], 'sense')
        self.assertEqual(set(join['members']), {'r1', 'r2'})
        for change in ('different_value', 'third_arm', 'conductive_center'):
            changed = deepcopy(devices)
            if change == 'different_value':
                changed['r2']['value'] = '2'
            elif change == 'third_arm':
                changed['r3'] = resistor('elsewhere', 'sense')
            else:
                changed['extra'] = mos('sense', 'other')
            other, _, _, _ = plan_for(changed)
            self.assertFalse(any(row['kind'] == 'two_arm_join' for row in other['patterns']))

    def test_single_end_attachment_does_not_merge_same_rail_devices(self):
        devices = dict(a=mos('left', 'ga'), b=mos('right', 'gb'),
                       ca=resistor('left', '0', 'C'), cb=resistor('right', '0', 'C'))
        plan, _, _, _ = plan_for(devices)
        self.assertEqual(sum(row['kind'] == 'single_attachment' for row in plan['patterns']), 2)
        self.assertEqual(plan['coverage']['recognized_passives'], 2)
        self.assertFalse(any(set(row['members']) == {'ca', 'cb'} for row in plan['patterns']))

    def test_names_and_netlist_order_do_not_define_passive_roles(self):
        original = network()
        plan, _, _, _ = plan_for(original)
        names = {name: 'part' + str(100-i) for i, name in enumerate(original)}
        net_map = {net: 'node' + str(100-i) for i, net in enumerate(sorted({value
            for dev in original.values() for key, value in dev.items() if key in ('d', 'g', 's', 'a', 'b', 'bulk') and value != '0'}))}
        renamed = {}
        for name, dev in reversed(list(original.items())):
            row = deepcopy(dev)
            for key in ('d', 'g', 's', 'a', 'b', 'bulk'):
                if key in row:
                    row[key] = net_map.get(row[key], row[key])
            renamed[names[name]] = row
        changed, _, _, _ = plan_for(renamed)
        self.assertEqual(semantics(plan), semantics(changed, {new: old for old, new in names.items()}))

    def test_unique_rl_bridge_uses_existing_planner_without_other_row_hints(self):
        devices = dict(upper=mos('out', 'gu', 'top'), lower=mos('bottom', 'gl'),
                       bridge=resistor('top', 'bottom', 'L'))
        original = dict(upper=(1, 1), lower=(1, 4), bridge=(0, 2))
        levels = compute_levels(devices)
        frozen = deepcopy(levels)
        document = to_document(devices)
        comps, nets = document['components'], document['nets']
        index = {row['id']: i for i, row in enumerate(comps)}
        plan = build_readability_plan(devices, comps, nets, index, analyze_structures(devices),
                                      levels, [original[row['id']][0] for row in comps])
        self.assertLess(plan['display_rows']['upper'], plan['display_rows']['bridge'])
        self.assertLess(plan['display_rows']['bridge'], plan['display_rows']['lower'])
        self.assertTrue(plan['series_axes'])
        self.assertEqual(plan['branch_row_plan']['same_row'], [])
        self.assertEqual(plan['branch_row_plan']['tail_orders'], [])
        self.assertEqual(levels, frozen)

    def test_capacitor_or_fork_is_not_a_fake_dc_row_bridge(self):
        for kind in ('C', 'L'):
            devices = dict(upper=mos('out', 'gu', 'top'), lower=mos('bottom', 'gl'),
                           bridge=resistor('top', 'bottom', kind))
            if kind == 'L':
                devices['fork'] = mos('third', 'gthird', 'top')
            plan, _, _, _ = plan_for(devices)
            self.assertFalse(plan['series_axes'])

    def test_closed_passive_loop_reports_ambiguity_instead_of_inventing_order(self):
        devices = dict(r=resistor('x', 'y'), c=resistor('y', 'z', 'C'), l=resistor('z', 'x', 'L'))
        plan, _, _, _ = plan_for(devices)
        self.assertFalse(plan['supported'])
        self.assertTrue(plan['ambiguities'])
        self.assertEqual(plan['coverage']['recognized_passives'], 0)

    def test_source_only_coverage_and_three_conservative_source_roles(self):
        fixtures = [dict(a=mos('left', 'ip', 'tail'), b=mos('right', 'im', 'tail'),
                         bias=source('ISRC', 'tail')),
                    dict(q=mos('bias', 'bias'), bias=source('ISRC', 'vdd', 'bias')),
                    dict(q=mos('out', 'control'), bias=source('VSRC', 'control'))]
        for devices, kind in zip(fixtures, ('tail_source', 'diode_bias_source', 'gate_voltage_source')):
            plan, _, _, _ = plan_for(devices)
            self.assertEqual([row['kind'] for row in plan['sources']], [kind])
            self.assertFalse(plan['patterns'])
            self.assertEqual(plan['coverage']['source_devices'], 1)
            self.assertEqual(plan['sources'][0]['confidence'], 'high')
        devices = fixtures[0]
        devices['voltage_clamp'] = source('VSRC', 'tail')
        plan, _, _, _ = plan_for(devices)
        self.assertFalse(plan['sources'])
        self.assertEqual(plan['coverage']['unassigned_sources'], ['bias', 'voltage_clamp'])


class ReadabilityMeasurementTests(unittest.TestCase):
    def test_real_junction_pins_are_used_and_missing_is_unknown(self):
        devices, pl, comps, nets, index, plan = bank_fixture()
        before = snapshot(pl)
        actual = junction_point(pl, index, plan, 'signal')
        self.assertEqual(actual, pl.pin_world(index['a'], 'D'))
        measured = measure_readability(pl, comps, index, plan)
        self.assertTrue(measured['measurable'])
        self.assertGreater(measured['owner_locality'], 0)
        self.assertEqual(snapshot(pl), before)
        json.dumps(measured, allow_nan=False)
        missing = dict(index)
        del missing['c']
        unknown = measure_readability(pl, comps, missing, plan)
        self.assertFalse(unknown['measurable'])
        self.assertEqual(readability_rank(unknown)[0], float('inf'))

    def test_scattered_bank_is_worse_than_coherent_bank(self):
        _, pl, comps, _, index, plan = bank_fixture()
        before = measure_readability(pl, comps, index, plan)
        coherent = deepcopy(pl)
        cells = list(coherent.cell_of)
        for name, col in zip(('r', 'c', 'l'), (2, 3, 4)):
            cells[index[name]] = (1, col)
        coherent.set_state(cells, [0]*len(comps))
        after = measure_readability(coherent, comps, index, plan)
        self.assertLess(readability_rank(after), readability_rank(before))

    def test_whole_chain_order_detects_middle_element_exchange(self):
        devices = network()
        del devices['bypass']
        cells = dict(a=(1, 0), b=(1, 6), r=(1, 2), c=(1, 3), l=(1, 4))
        pl, comps, _, index, structs = convert(devices, cells, orientations={name: 1 for name in ('r', 'c', 'l')})
        plan = build_readability_plan(devices, comps, {}, index, structs, compute_levels(devices), pl.row_of)
        before = measure_readability(pl, comps, index, plan)
        changed = list(pl.cell_of)
        changed[index['c']], changed[index['r']] = changed[index['r']], changed[index['c']]
        pl.set_state(changed, pl.orient)
        after = measure_readability(pl, comps, index, plan)
        self.assertGreater(after['order_errors'], before['order_errors'])

    def test_source_only_has_unknown_passive_metrics_and_explicit_source_distance(self):
        devices = dict(a=mos('left', 'ip', 'tail'), b=mos('right', 'im', 'tail'), i=source('ISRC', 'tail'))
        cells = dict(a=(1, 1), b=(1, 3), i=(3, 6))
        pl, comps, _, index, structs = convert(devices, cells)
        plan = build_readability_plan(devices, comps, {}, index, structs, compute_levels(devices), pl.row_of)
        metrics = measure_readability(pl, comps, index, plan)
        self.assertTrue(metrics['measurable'])
        self.assertFalse(metrics['passive_supported'])
        self.assertTrue(metrics['source_supported'])
        self.assertIsNone(metrics['violations'])
        self.assertGreater(metrics['source_locality'], 0)
        self.assertEqual(score_readability(pl, comps, index, plan)['weighted_total'], 0)
        self.assertGreater(score_readability(pl, comps, index, plan, phase=3)['weighted_total'], 0)

    def test_global_reflection_preserves_network_measurements(self):
        _, pl, comps, _, index, plan = bank_fixture()
        class Reflected:
            def __init__(self, original):
                self.original = original
                self.comps = original.comps
                # Axes supply pitch magnitudes, not absolute device origins.
                self.column_x, self.row_y = original.column_x, original.row_y
            def pos_of(self, i):
                x, y = self.original.pos_of(i)
                return -x, y
            def pin_world(self, i, pin):
                x, y = self.original.pin_world(i, pin)
                return -x, y
        ordinary = measure_readability(pl, comps, index, plan)
        reflected = measure_readability(Reflected(pl), comps, index, plan)
        self.assertEqual(readability_rank(ordinary), readability_rank(reflected))
        self.assertEqual(ordinary['orientation_errors'], reflected['orientation_errors'])
        self.assertEqual(ordinary['owner_locality'], reflected['owner_locality'])

    def test_compactness_is_not_a_rigid_union_between_separate_rail_only_circuits(self):
        devices = dict(a=mos('aout', 'ain'), b=mos('bout', 'bin'),
                       ca=resistor('aout', '0', 'C'), cb=resistor('bout', '0', 'C'))
        plan, _, _, _ = plan_for(devices)
        contexts = [set(row['members']) for row in plan['compact_groups']]
        self.assertIn({'a', 'ca'}, contexts)
        self.assertIn({'b', 'cb'}, contexts)
        self.assertFalse(any({'a', 'b'} <= members for members in contexts))


class ReadabilityPreparationTests(unittest.TestCase):
    def test_seed_is_bounded_improves_common_estimate_and_keeps_flexible_motion(self):
        devices, pl, comps, nets, index, plan = bank_fixture()
        frozen = deepcopy((pl.cell_of, pl.orient, pl.row_y, pl.column_x, pl.blocks, pl.rng.getstate()))
        with mock.patch('orthogonal_router.route_scene', side_effect=AssertionError('A* in initializer')):
            result, report = prepare_readability_placement(pl, devices, comps, nets, index, plan)
        self.assertTrue(report['applied'])
        self.assertLessEqual(report['trials'], 128)
        self.assertLessEqual(report['common_estimate_after'], report['common_estimate_before'])
        self.assertLessEqual(report['estimated_hpwl_after'], report['estimated_hpwl_before']*1.15+1e-8)
        self.assertEqual((pl.cell_of, pl.orient, pl.row_y, pl.column_x, pl.blocks, pl.rng.getstate()), frozen)
        self.assertEqual(result.blocks, pl.blocks)
        self.assertEqual(result.rng.getstate(), pl.rng.getstate())
        self.assertTrue(result.valid_state())
        self.assertEqual(result.cell_of[index['a']], pl.cell_of[index['a']])
        self.assertEqual(result.readability_plan, plan)
        self.assertTrue(result.readability_sa_enabled)
        self.assertFalse(hasattr(result, 'attachment_model'))
        # No new rigid block prevents a passive from changing rows later.
        passive = index['r']
        target = next((row, col) for row in range(len(result.row_y)) for col in range(result.cols)
                      if row != result.cell_of[passive][0] and (row, col) not in result.comp_at)
        self.assertIsNotNone(result._passive_move(passive, target))

    def test_fixed_members_and_non_improving_common_estimate_keep_old_state(self):
        for reason in ('fixed', 'estimate'):
            devices, pl, comps, nets, index, plan = bank_fixture()
            original = (list(pl.cell_of), list(pl.orient))
            options = {}
            if reason == 'fixed':
                pl.fixed.add(index['r'])
                pl.fixed_cells[index['r']] = pl.cell_of[index['r']]
            else:
                options['estimate'] = lambda candidate: 0 if candidate.cell_of == original[0] else 100000
            result, report = prepare_readability_placement(pl, devices, comps, nets, index, plan, **options)
            self.assertEqual((result.cell_of, result.orient), original)
            self.assertFalse(report['applied'])

    def test_weak_source_plan_is_measured_without_trials_even_with_no_initialization_budget(self):
        devices = dict(a=mos('left', 'ip', 'tail'), b=mos('right', 'im', 'tail'),
                       i=source('ISRC', 'tail'))
        pl, comps, nets, index, structs = convert(devices, dict(a=(1, 1), b=(1, 3), i=(3, 6)))
        plan = build_readability_plan(devices, comps, nets, index, structs, compute_levels(devices), pl.row_of)
        original = snapshot(pl)
        with mock.patch('readability_layout._targets', side_effect=AssertionError('weak initializer moved')):
            result, report = prepare_readability_placement(pl, devices, comps, nets, index, plan,
                seconds=0, max_trials=0, estimate=lambda _: self.fail('weak initializer estimated'))
        self.assertEqual(snapshot(pl), original)
        self.assertEqual((result.cell_of, result.orient, result.rng.getstate()),
                         (pl.cell_of, pl.orient, pl.rng.getstate()))
        self.assertFalse(result.readability_sa_enabled)
        self.assertEqual(result.readability_plan, plan)
        self.assertEqual(report['status'], 'report_only')
        self.assertEqual(report['reason'], 'weak_relations_report_only')
        self.assertEqual(report['trials'], 0)
        self.assertFalse(report['applied'])
        self.assertGreater(report['after']['source_locality'], 0)
        self.assertIsNone(report['after']['violations'])

    def test_timeout_and_unsupported_are_exact_old_object_and_rng(self):
        devices, pl, comps, nets, index, plan = bank_fixture()
        original = snapshot(pl)
        for given, options in ((plan, {'seconds': 0}), ({'supported': False}, {})):
            result, report = prepare_readability_placement(pl, devices, comps, nets, index, given, **options)
            self.assertIs(result, pl)
            self.assertEqual(snapshot(pl), original)
            self.assertFalse(report['applied'])

    def test_fixed_passive_policy_does_not_repack_members(self):
        devices, pl, comps, nets, index, plan = bank_fixture()
        pl.passive_policy = 'fixed'
        result, report = prepare_readability_placement(pl, devices, comps, nets, index, plan)
        self.assertFalse(report['applied'])
        self.assertEqual((result.cell_of, result.orient), (pl.cell_of, pl.orient))

    def test_initializer_budget_validation(self):
        devices, pl, comps, nets, index, plan = bank_fixture()
        for options in ({'max_trials': 129}, {'max_trials': -1}, {'seconds': 1.1}, {'seconds': -1}):
            with self.assertRaises(ValueError):
                prepare_readability_placement(pl, devices, comps, nets, index, plan, **options)

    def test_expiration_after_a_trial_rolls_back_every_state_and_rng(self):
        devices, pl, comps, nets, index, plan = bank_fixture()
        original = snapshot(pl)
        # Timer checks cover both preprocessing and trial transactions. No
        # wall-clock delay or route search is needed to test deadline expiry.
        class Timer:
            ticks = 0
            def __call__(self):
                self.ticks += 1
                return 0.0 if self.ticks < 40 else 2.0
        with mock.patch('readability_layout.perf_counter', side_effect=Timer()):
            result, report = prepare_readability_placement(pl, devices, comps, nets, index, plan)
        self.assertIs(result, pl)
        self.assertEqual(snapshot(pl), original)
        self.assertFalse(report['applied'])
        self.assertEqual(report['reason'], 'initialization_budget')


class WeakReadabilityFlowTests(unittest.TestCase):
    def test_real_single_capacitor_preserves_initialization_sa_random_stream_and_freedom(self):
        import structured_placement as structured
        source_path = ROOT / 'examples/3.cir'  # Eleven MOS devices and one output load capacitor.
        real_anneal = structured._anneal

        def run(mode, iterations):
            before_sa = []
            def tracked_anneal(pl, *args):
                before_sa.append(deepcopy((pl.cell_of, pl.orient, pl.row_of,
                    pl.column_x, pl.row_y, pl.blocks, pl.fixed_rows,
                    pl.passive_relations, pl.rng.getstate())))
                return real_anneal(pl, *args)
            with mock.patch.object(structured, '_anneal', side_effect=tracked_anneal):
                candidates = list(structured.create_candidates(source_path, iterations=iterations,
                    seed=731, readability_layout=mode, readability_phase=5))
            return candidates, before_sa

        for iterations in (0, 600):
            with self.subTest(iterations=iterations):
                old, old_seeds = run('off', iterations)
                with mock.patch('readability_routing.make_initializer_cost', side_effect=AssertionError('weak routing estimator')), \
                     mock.patch('readability_layout._targets', side_effect=AssertionError('weak targets')), \
                     mock.patch('readability_layout.score_readability', side_effect=AssertionError('weak SA score')), \
                     mock.patch('orthogonal_router.route_scene', side_effect=AssertionError('unexpected A*')):
                    new, new_seeds = run('auto', iterations)
                self.assertEqual(old_seeds, new_seeds)
                for reference, measured in zip(old, new):
                    a, b = reference[0], measured[0]
                    self.assertEqual((a.cell_of, a.orient, a.row_of, a.blocks, a.move_counts, a.rng.getstate()),
                                     (b.cell_of, b.orient, b.row_of, b.blocks, b.move_counts, b.rng.getstate()))
                    self.assertEqual(reference[4]['optimization_metrics'], measured[4]['optimization_metrics'])
                    self.assertEqual(reference[4]['annealing'], measured[4]['annealing'])
                for candidate in new[1:]:
                    pl, comps, _, index, metadata = candidate
                    plan = metadata['layout_readability']['plan']
                    self.assertEqual(semantics(plan), [('single_attachment', ('C1',))])
                    self.assertFalse(has_strong_passive_plan(plan))
                    self.assertFalse(pl.readability_sa_enabled)
                    report = metadata['layout_readability']['initialization']
                    self.assertEqual(report['reason'], 'weak_relations_report_only')
                    self.assertEqual(report['trials'], 0)
                    self.assertEqual(report['before'], report['after'])
                    self.assertGreater(report['after']['owner_locality'], 0)
                    self.assertTrue(metadata['layout_readability']['metrics']['measurable'])
                    # The measured load remains free to move across display rows.
                    capacitor = index['C1']
                    active_cells = [cell for i, cell in enumerate(pl.cell_of) if i != capacitor]
                    target = next((row, col) for row in range(len(pl.row_y)) for col in range(pl.cols)
                                  if row != pl.cell_of[capacitor][0] and (row, col) not in pl.comp_at)
                    undo = pl._passive_move(capacitor, target)
                    self.assertIsNotNone(undo)
                    self.assertEqual(pl.cell_of[capacitor], target)
                    self.assertEqual([cell for i, cell in enumerate(pl.cell_of) if i != capacitor], active_cells)
                    self.assertTrue(pl.valid_state())
                    pl.set_state(*undo)


if __name__ == '__main__':
    unittest.main()
