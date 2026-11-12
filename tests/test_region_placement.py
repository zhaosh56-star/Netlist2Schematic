"""Horizontal region rollout preserves the source rows and old off behavior."""
from copy import deepcopy
from itertools import islice
from pathlib import Path
import random
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from layout_evidence import build_layout_evidence
from layout_relations import build_layout_relations
from netlist_core import to_document
from region_layout import build_region_plan, measure_regions
from region_placement import (_physical_axes, _region_choices, _lanes, initialize_regions, propose_region_move,
                              score_region_placement)
import structured_placement as structured
from tests.test_region_layout import comparator, ota, mos
from tests.test_passive_placement import resistor


def region_seed(factory=comparator, *, flexible=False, seed=71, variant=1):
    devices = factory()
    if flexible:
        devices['load'] = resistor(next(iter(devices.values()))['d'], 'lo')
    doc = to_document(devices)
    comps, nets = doc['components'], doc['nets']
    index = {comp['id']: i for i, comp in enumerate(comps)}
    levels = structured.dl.compute_levels(devices)
    structs = structured._structures(devices)
    evidence = build_layout_evidence(devices, structs)
    relations = build_layout_relations(devices, validated_evidence=evidence)
    att = structured.legacy.attach_info(devices, levels['net_levels'], set())
    rows, _ = structured.legacy._allocate_rows(devices, comps, index,
        levels['net_levels'], levels['device_levels'], att)
    plan = build_region_plan(devices, evidence, relations,
        {comp['id']: rows[i] for i, comp in enumerate(comps)}, comps, index)
    pl, alignment, pairs = structured._initial_placement(
        devices, comps, nets, index, levels, att, structs, seed, variant,
        passive_layout='flexible' if flexible else 'fixed',
        region_plan=plan, region_relations=relations)
    return pl, comps, nets, index, plan, alignment, pairs, devices, rows


class RegionRolloutTests(unittest.TestCase):
    def test_off_and_reference_do_not_invoke_the_new_planner(self):
        path = ROOT / 'examples/2.cir'
        before = next(structured.create_candidates(path, seed=19, iterations=30))
        with mock.patch('region_layout.build_region_plan', side_effect=AssertionError('early planner')):
            off = list(structured.create_candidates(path, seed=19, iterations=30, region_layout='off'))
            reference = next(structured.create_candidates(path, seed=19, iterations=30, region_layout='auto'))
        self.assertEqual(before[0].cell_of, reference[0].cell_of)
        self.assertEqual(before[0].orient, reference[0].orient)
        self.assertEqual(before[0].rng.getstate(), reference[0].rng.getstate())
        self.assertEqual(before[4], reference[4])
        self.assertTrue(all('layout_regions' not in row[4] for row in off))

    def test_unsupported_plan_preserves_all_old_fields_and_random_state(self):
        path = ROOT / 'examples/1.cir'
        old = list(structured.create_candidates(path, seed=83, iterations=90))
        new = list(structured.create_candidates(path, seed=83, iterations=90, region_layout='auto'))
        for i, (before, after) in enumerate(zip(old, new)):
            self.assertEqual(before[0].cell_of, after[0].cell_of)
            self.assertEqual(before[0].orient, after[0].orient)
            self.assertEqual(before[0].rng.getstate(), after[0].rng.getstate())
            metadata = deepcopy(after[4])
            if i:
                regions = metadata.pop('layout_regions')
                self.assertFalse(regions['applied'])
                self.assertEqual(regions['reason'], 'no_supported_regions')
            self.assertEqual(before[4], metadata)

    def test_plan_is_built_once_for_two_three_and_source_rows_remain_unchanged(self):
        path = ROOT / 'examples/2.cir'
        old = list(structured.create_candidates(path, seed=5, iterations=0))
        with mock.patch('region_layout.build_region_plan', wraps=build_region_plan) as builder:
            new = list(structured.create_candidates(path, seed=5, iterations=0, region_layout='auto'))
        self.assertEqual(builder.call_count, 1)
        for before, after in zip(old, new):
            self.assertEqual(before[0].row_of, after[0].row_of)
        self.assertEqual(new[1][4]['layout_regions']['plan'], new[2][4]['layout_regions']['plan'])
        self.assertIsNot(new[1][4]['layout_regions']['plan'], new[2][4]['layout_regions']['plan'])
        self.assertTrue(all(row[4]['layout_regions']['applied'] for row in new[1:]))

    def test_supported_seed_timeout_falls_back_transactionally(self):
        path = ROOT / 'examples/2.cir'
        before = list(structured.create_candidates(path, seed=32, iterations=30))
        with mock.patch('region_placement.INITIAL_SECONDS', 0):
            after = list(structured.create_candidates(path, seed=32, iterations=30, region_layout='auto'))
        for i, (old, new) in enumerate(zip(before, after)):
            self.assertEqual(old[0].cell_of, new[0].cell_of)
            self.assertEqual(old[0].orient, new[0].orient)
            self.assertEqual(old[0].rng.getstate(), new[0].rng.getstate())
            metadata = deepcopy(new[4])
            if i:
                report = metadata.pop('layout_regions')
                self.assertFalse(report['applied'])
                self.assertEqual(report['initialization']['reason'], 'initialization_budget')
            self.assertEqual(old[4], metadata)

    def test_new_policy_validation_and_incompatible_structure_path(self):
        path = ROOT / 'examples/2.cir'
        for options in ({'region_layout': 'bad'},
                        {'region_layout': 'auto', 'structure_layout': 'auto'}):
            with self.assertRaises(ValueError):
                list(structured.create_candidates(path, iterations=0, **options))

    def test_no_organization_gain_keeps_the_old_seed_and_rng_even_when_trials_are_legal(self):
        path = ROOT / 'examples/2.cir'
        before = list(structured.create_candidates(path, seed=32, iterations=0))
        # Equal measured organization is insufficient, regardless of a trial's
        # cheap wire estimate. The original instance's RNG must stay untouched.
        with mock.patch('region_layout.region_rank', return_value=(0, 0, 0, 0)):
            after = list(structured.create_candidates(path, seed=32, iterations=0, region_layout='auto'))
        for old, new in zip(before[1:], after[1:]):
            self.assertEqual(old[0].cell_of, new[0].cell_of)
            self.assertEqual(old[0].orient, new[0].orient)
            self.assertEqual(old[0].rng.getstate(), new[0].rng.getstate())
            report = new[4]['layout_regions']
            self.assertFalse(report['applied'])
            self.assertEqual(report['reason'], 'no_organization_improvement')
            self.assertTrue(report['initialization']['candidates'])
            self.assertTrue(all(not trial['organization_improved']
                            for trial in report['initialization']['candidates']))

    def test_actual_ota_with_cmfb_has_legal_zero_step_trials_without_rebuilding_source_rows(self):
        path = ROOT / 'test_data/16+/schematic-of-the-folded-cascode-amplifier-ota1-with-its-cmfb-circuit.cir'
        if not path.is_file():
            self.skipTest('optional original source corpus is unavailable')
        with mock.patch('orthogonal_router.route_scene', side_effect=AssertionError('A* in initializer')):
            candidates = list(structured.create_candidates(path, cell='dut', iterations=0,
                                                          region_layout='auto'))
        rows = candidates[0][0].row_of
        for pl, _, _, _, metadata in candidates[1:]:
            report = metadata['layout_regions']
            initialization = report['initialization']
            self.assertTrue(report['applied'], report['reason'])
            self.assertGreater(len(initialization['candidates']), 0)
            self.assertEqual(initialization['packing_rejections'], 0)
            self.assertLess(tuple(initialization['selected_rank']), tuple(initialization['baseline_rank']))
            self.assertEqual(pl.row_of, rows)
            self.assertTrue(pl.blocks_contiguous())


class RegionPlacementTests(unittest.TestCase):
    def assert_valid(self, pl, rows):
        self.assertEqual(pl.row_of, [row for row, _ in pl.cell_of])
        self.assertEqual(pl.comp_at, {cell: i for i, cell in enumerate(pl.cell_of)})
        self.assertEqual(len(set(pl.cell_of)), len(pl.comps))
        self.assertTrue(pl.blocks_contiguous())
        for i, comp in enumerate(pl.comps):
            if comp['symbol'] in ('nmos', 'pmos', 'npn', 'pnp'):
                self.assertEqual(pl.cell_of[i][0], rows[i])
            self.assertIn(pl.orient[i], pl.allowed_orientations(i))
        if hasattr(pl, 'valid_state'):
            self.assertTrue(pl.valid_state())

    def test_folded_input_and_output_are_separate_regions_not_an_outer_pair(self):
        pl, comps, _, index, plan, alignment, _, _, rows = region_seed(ota)
        leaves = {row['kind']: row for row in plan['regions'] if not row['children']}
        inputs = [pl.cell_of[index[name]][1] for name in leaves['folded_input']['members']]
        outputs = [pl.cell_of[index[name]][1] for name in leaves['folded_output']['members']]
        self.assertTrue(max(inputs)+1 < min(outputs) or max(outputs)+1 < min(inputs))
        self.assertEqual(pl.role_orders, [])
        self.assertEqual(alignment, [])
        axes = {frozenset((row['a'], row['b'])) for row in pl.region_series_axes}
        self.assertNotIn(frozenset(('ia', 'qa')), axes)
        self.assertNotIn(frozenset(('ib', 'qb')), axes)
        metrics = measure_regions(pl, comps, index, plan)
        self.assertEqual((metrics['interleaving'], metrics['order_errors']), (0, 0))
        self.assert_valid(pl, rows)
        self.assertLessEqual(pl.region_initialization['trials'], 128)

    def test_real_pin_axes_are_deduplicated_and_nonunique_generic_nodes_are_not_axes(self):
        pl, _, _, _, plan, _, _, devices, _ = region_seed(ota)
        axes = _physical_axes(devices, plan)
        keys = [tuple(sorted(((row['a'], row['pin_a']), (row['b'], row['pin_b'])))) for row in axes]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(all(frozenset((row['a'], row['b'])) not in
                        {frozenset(('ia', 'qa')), frozenset(('ib', 'qb'))} for row in axes))
        self.assertTrue(any(row['a'] == 'ta' and row['b'] == 'ua' for row in axes))
        self.assertEqual(pl.region_series_axes, axes)

    def test_outside_region_true_axes_keep_the_original_functional_weight(self):
        devices = comparator()
        plan = build_region_plan(devices)
        devices.update(invp=mos('otherout', 'othergate', 'hi', 'PMOS'),
                       invn=mos('otherout', 'othergate', 'lo'))
        axis = next(row for row in _physical_axes(devices, plan)
                    if {row['a'], row['b']} == {'invp', 'invn'})
        self.assertEqual(axis['weight'], 3.5)
        devices.update(lp=mos('lout0', 'lout1', 'hi', 'PMOS'),
                       ln=mos('lout0', 'lout1', 'lo'),
                       rp=mos('lout1', 'lout0', 'hi', 'PMOS'),
                       rn=mos('lout1', 'lout0', 'lo'))
        axis = next(row for row in _physical_axes(devices, plan)
                    if {row['a'], row['b']} == {'lp', 'ln'})
        self.assertEqual(axis['weight'], 4.5)

    def test_initial_trials_rotate_orders_and_independently_mirror_subregions_and_attachments(self):
        pl, _, _, index, plan, _, _, _, rows = region_seed(ota)
        choices = list(islice(_region_choices(plan, 1), 128))
        self.assertLessEqual(len(choices), 128)
        self.assertGreater(len({tuple(order) for order, _ in choices[:6]}), 1)
        self.assertTrue(any(len(set(mirror.values())) > 1 for _, mirror in choices))
        report = pl.region_initialization
        self.assertLess(tuple(report['selected_rank']), tuple(report['baseline_rank']))
        self.assertGreater(report['baseline_estimated_wirelength'], 0)
        self.assertGreater(report['selected_estimated_wirelength'], 0)
        self.assertTrue(all('energy' in trial and 'estimated_wirelength' in trial
                            and 'rank' in trial for trial in report['candidates']))
        comp, _, _, ix, cplan, _, _, _, crows = region_seed()
        attachment = next(row for row in cplan['regions'] if row['kind'] == 'clocked_rail_attachments')
        normal, width = _lanes(attachment, crows, ix, cplan['series_axes'], 1)
        reflected, _ = _lanes(attachment, crows, ix, cplan['series_axes'], -1)
        self.assertTrue(any(normal[i] != reflected[i] for i in normal))
        self.assertEqual(reflected, {i: width-1-col for i, col in normal.items()})

    def test_local_pair_owners_stay_contiguous_under_instance_renaming_and_source_reordering(self):
        for seed in range(4):
            devices = ota()
            items = list(devices.items())
            random.Random(seed).shuffle(items)
            # Instance names and insertion order can change role tie ordering;
            # neither may let two disjoint local pairs interleave in one row.
            renamed = {f'unit{n:02}': device for n, (_, device) in enumerate(items)}
            pl, _, _, _, _, _, _, _, rows = region_seed(lambda: renamed, seed=seed)
            report = pl.region_initialization
            self.assertEqual(report['packing_rejections'], 0)
            self.assertTrue(report['candidates'])
            self.assert_valid(pl, rows)

    def test_complete_detector_mirror_is_one_reversible_move_with_other_regions_unchanged(self):
        pl, _, _, index, plan, _, _, _, rows = region_seed(ota)
        region = next(row for row in plan['regions'] if row['kind'] == 'feedback_detector')
        members = {index[name] for name in region['members']}
        original = list(pl.cell_of), list(pl.orient)
        rng = mock.Mock()
        rng.randrange.return_value = 3
        rng.choice.side_effect = lambda candidates: next(group for group in candidates if set(group) == members)
        pl.rng = rng
        undo = propose_region_move(pl)
        self.assertIsNotNone(undo)
        lo, hi = min(original[0][i][1] for i in members), max(original[0][i][1] for i in members)
        for i in range(len(pl.comps)):
            self.assertEqual(pl.cell_of[i], (original[0][i][0], lo+hi-original[0][i][1])
                             if i in members else original[0][i])
        self.assert_valid(pl, rows)
        pl.set_state(*undo)
        self.assertEqual((pl.cell_of, pl.orient), original)

    def test_score_reads_real_pins_after_orientation_and_does_not_duplicate_axes(self):
        pl, _, _, index, plan, _, _, _, _ = region_seed()
        value, report = score_region_placement(pl, index, plan)
        pl.region_series_axes += deepcopy(pl.region_series_axes)
        self.assertEqual(score_region_placement(pl, index, plan)[0], value)
        axis = pl.region_series_axes[0]
        pl.offset_x = [0.0]*len(pl.comps)
        pl.offset_x[index[axis['a']]] = 20
        _, updated = score_region_placement(pl, index, plan)
        self.assertNotEqual(report['weighted_series_pin_error'], updated['weighted_series_pin_error'])

    def test_independent_subregion_can_move_while_parent_siblings_stay_put(self):
        pl, _, _, index, plan, _, _, _, rows = region_seed()
        core = next(row for row in plan['regions'] if row['kind'] == 'comparator_core')
        original = list(pl.cell_of)
        rng = mock.Mock()
        rng.randrange.return_value = 0
        rng.choice.side_effect = lambda groups: groups[0]
        rng.randint.return_value = 1
        pl.rng = rng
        undo = propose_region_move(pl)
        self.assertIsNotNone(undo)
        for name, i in index.items():
            self.assertEqual(pl.cell_of[i], (original[i][0], original[i][1]+(name in core['members'])))
        self.assert_valid(pl, rows)
        pl.set_state(*undo)
        self.assertEqual(pl.cell_of, original)

    def test_fixed_devices_and_undo_remain_exact_through_region_and_leaf_actions(self):
        pl, _, _, index, plan, _, _, _, rows = region_seed()
        fixed = index['tail']
        pl.fixed = {fixed}
        pl.fixed_cells = {fixed: pl.cell_of[fixed]}
        fixed_state = pl.cell_of[fixed], pl.orient[fixed]
        snapshot = deepcopy(plan)
        accepted = 0
        for _ in range(300):
            before = list(pl.cell_of), list(pl.orient)
            undo = pl.propose()
            self.assertEqual((pl.cell_of[fixed], pl.orient[fixed]), fixed_state)
            self.assert_valid(pl, rows)
            if undo is not None:
                accepted += 1
                pl.set_state(*undo)
                self.assertEqual((pl.cell_of, pl.orient), before)
        self.assertGreater(accepted, 0)
        self.assertEqual(pl.region_plan, snapshot)

    def test_flexible_passive_still_crosses_rows_and_region_actions_preserve_fixed_active_rows(self):
        pl, _, _, index, _, _, _, _, rows = region_seed(flexible=True)
        passive = index['load']
        old_row = pl.cell_of[passive][0]
        target = next((row, col) for row in range(len(pl.row_y)) for col in range(pl.cols)
                      if row != old_row and (row, col) not in pl.comp_at)
        undo = pl._passive_move(passive, target)
        self.assertIsNotNone(undo)
        self.assertNotEqual(pl.cell_of[passive][0], old_row)
        self.assert_valid(pl, rows)
        pl.set_state(*undo)
        for _ in range(400):
            undo = pl.propose()
            self.assert_valid(pl, rows)
            if undo is not None:
                pl.set_state(*undo)
        self.assertGreater(pl.move_counts['passive_attempts'], 0)
        self.assertGreater(pl.move_counts['legacy_attempts'], 0)

    def test_region_generation_and_annealing_never_route_or_change_source(self):
        path = ROOT / 'examples/2.cir'
        original = path.read_bytes()
        with mock.patch('orthogonal_router.route_scene', side_effect=AssertionError('A* in regions')):
            candidates = list(structured.create_candidates(path, iterations=180, seed=25,
                                                          region_layout='auto'))
        self.assertEqual(path.read_bytes(), original)
        for pl, _, _, _, metadata in candidates[1:]:
            self.assertTrue(pl.blocks_contiguous())
            self.assertTrue(metadata['layout_regions']['applied'])
            self.assertIn('region_regularization', metadata['optimization_metrics'])
            diagnostics = metadata['layout_regions']
            counts = diagnostics['sa_proposals']['counts']
            self.assertGreater(sum(counts['attempts'].values()), 0)
            self.assertTrue(all(counts['legal'][kind] <= attempts
                                for kind, attempts in counts['attempts'].items()))
            self.assertEqual(diagnostics['changes_stage'], 'seed_to_after_sa_and_geometry_floor')
            self.assertTrue(all('position' in change['chosen'] and 'cell' in change['seed']
                                for change in diagnostics['chosen_changes']))


if __name__ == '__main__':
    unittest.main()
