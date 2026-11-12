"""Local sources and compacting drawings obey the real geometry boundary."""
from copy import deepcopy
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import readability_refinement as refine
from routing_rules import RoutingOptions
import structured_placement as structured
from tests.test_passive_placement import base_placement, make_flexible, mos, source, resistor
from readability_banks import measure_bank_outlet
from readability_layout import build_readability_plan

PAR = structured.par


def tail_fixture(reverse=False, flexible=False):
    devices = dict(a=mos('oa', 'ga', 'tail'), b=mos('ob', 'gb', 'tail'),
                   bias=source('ISRC', '0' if reverse else 'tail', 'tail' if reverse else '0'))
    pl, comps, nets, index = base_placement(devices,
        dict(a=(1, 2), b=(1, 4), bias=(4, 0)), {'b': 1})
    if flexible:
        devices['load'] = resistor('oa', '0')
        pl, comps, nets, index = base_placement(devices,
            dict(a=(1, 2), b=(1, 4), bias=(4, 0), load=(2, 0)), {'b': 1})
        pl = make_flexible(pl, devices, nets, index, [])
    return pl, comps, nets, index


def metadata(patterns=()):
    return {'layout_readability': {'plan': {'version': 'fixture', 'signature': 'same-source',
                                           'supported': False, 'patterns': list(patterns)}}}


class SourceModelTests(unittest.TestCase):
    def test_three_high_confidence_roles_use_real_original_pins(self):
        pl, comps, nets, index = tail_fixture()
        row, = refine.build_refinement_model(PAR, comps, nets, index)['sources']
        self.assertEqual(row['kind'], 'tail_source')
        self.assertEqual((row['signal_pin'], row['rail_pin']), ('+', '-'))
        devices = dict(reference=mos('bias', 'bias'), output=mos('out', 'bias'),
                       current=source('ISRC', 'vdd', 'bias'),
                       driver=mos('gain', 'vin'), voltage=source('VSRC', 'vin'))
        pl, comps, nets, index = base_placement(devices,
            dict(reference=(2, 1), output=(2, 3), current=(0, 1), driver=(1, 5), voltage=(4, 6)))
        rows = {row['device']: row for row in refine.build_refinement_model(PAR, comps, nets, index)['sources']}
        self.assertEqual(rows['current']['kind'], 'diode_bias_source')
        self.assertEqual(rows['current']['host_ports'], [['reference', 'D']])
        self.assertEqual(rows['voltage']['kind'], 'gate_voltage_source')
        self.assertEqual(rows['voltage']['host_ports'], [['driver', 'G']])

    def test_shared_tail_extra_current_path_floating_and_supply_only_sources_are_not_local(self):
        devices = dict(a=mos('oa', 'ga', 'tail'), b=mos('ob', 'gb', 'tail'),
                       third=mos('oc', 'gc', 'tail'), tail=source('ISRC', 'tail'),
                       floating=source('VSRC', 'left', 'right'),
                       supply=source('VSRC', 'vdd', '0'))
        cells = {name: (i//4, i%4) for i, name in enumerate(devices)}
        pl, comps, nets, index = base_placement(devices, cells)
        model = refine.build_refinement_model(PAR, comps, nets, index)
        self.assertEqual(model['sources'], [])
        self.assertEqual(len(model['skipped_sources']), 3)

    def test_measurement_is_pure_and_independent_of_region_supported(self):
        pl, comps, nets, index = tail_fixture()
        before = deepcopy(pl.__dict__)
        unsupported = refine.measure_refinements(PAR, pl, comps, nets, index, metadata())
        supported = refine.measure_refinements(PAR, pl, comps, nets, index,
            {'version': 'fixture', 'signature': 'same-source', 'supported': True, 'patterns': []})
        self.assertEqual(unsupported, supported)
        self.assertEqual(unsupported['source_count'], 1)
        self.assertGreater(unsupported['source_distance'], 0)
        self.assertEqual(pl.cell_of, before['cell_of'])
        self.assertEqual(pl.rng.getstate(), before['rng'].getstate())


class SourceRefinementTests(unittest.TestCase):
    def test_tail_moves_near_actual_pair_with_real_static_check_and_no_astar(self):
        pl, comps, nets, index = tail_fixture()
        old = deepcopy(pl)
        with patch('orthogonal_router.route_scene', side_effect=AssertionError('A* in source refinement')):
            result, meta = refine.refine_readability(PAR, pl, comps, nets, index, metadata(), phase=3)
        report = meta['layout_readability']['refinement']
        self.assertGreater(report['accepted'], 0)
        self.assertLess(report['after']['source_distance'], report['before']['source_distance'])
        for name in ('a', 'b'):
            i = index[name]
            self.assertEqual(result.cell_of[i], old.cell_of[i])
            self.assertEqual(result.orient[i], old.orient[i])
            self.assertEqual(result.pos_of(i), old.pos_of(i))
        self.assertLess(result.cell_of[index['bias']][0], old.cell_of[index['bias']][0])
        self.assertEqual(pl.cell_of, old.cell_of)
        self.assertEqual(pl.orient, old.orient)
        self.assertEqual(pl.rng.getstate(), old.rng.getstate())
        self.assertEqual(result.rng.getstate(), old.rng.getstate())

    def test_reversed_source_can_use_existing_180_degree_variant_without_terminal_swap(self):
        pl, comps, nets, index = tail_fixture(reverse=True)
        original_nodes = deepcopy(comps[index['bias']]['nodes'])
        result, meta = refine.refine_readability(PAR, pl, comps, nets, index, metadata())
        self.assertGreater(meta['layout_readability']['refinement']['accepted'], 0)
        self.assertEqual(result.orient[index['bias']], 2)
        self.assertEqual(comps[index['bias']]['nodes'], original_nodes)
        self.assertIn(2, result.allowed_orientations(index['bias']))

    def test_flexible_fixed_rows_are_rebound_only_for_the_eligible_source(self):
        pl, comps, nets, index = tail_fixture(flexible=True)
        source_index = index['bias']
        result, meta = refine.refine_readability(PAR, pl, comps, nets, index, metadata())
        self.assertTrue(result.valid_state())
        self.assertNotEqual(result.fixed_rows[source_index], pl.fixed_rows[source_index])
        for i, row in pl.fixed_rows.items():
            if i != source_index:
                self.assertEqual(result.fixed_rows[i], row)
        self.assertEqual(result.cell_of[index['load']], pl.cell_of[index['load']])

    def test_explicit_fixed_source_is_unchanged_including_world_position_and_orientation(self):
        pl, comps, nets, index = tail_fixture()
        i = index['bias']
        pl.fixed = {i}; pl.fixed_cells = {i: pl.cell_of[i]}
        original = pl.cell_of[i], pl.orient[i], pl.pos_of(i)
        result, meta = refine.refine_readability(PAR, pl, comps, nets, index, metadata())
        self.assertEqual((result.cell_of[i], result.orient[i], result.pos_of(i)), original)
        self.assertEqual(meta['layout_readability']['refinement']['accepted'], 0)

    def test_rejected_static_or_hpwl_trial_never_mutates_incumbent(self):
        for guard in ('static', 'hpwl'):
            pl, comps, nets, index = tail_fixture()
            original = list(pl.cell_of), list(pl.orient), list(pl.row_y), list(pl.column_x)
            if guard == 'static':
                context = patch('readability_refinement._static_safe', return_value=False)
            else:
                context = patch('readability_refinement._hpwl', side_effect=lambda p, t, n, ix:
                    100 if t.cell_of == original[0] and t.orient == original[1] else 200)
            with context:
                result, meta = refine.refine_readability(PAR, pl, comps, nets, index, metadata())
            report = meta['layout_readability']['refinement']
            self.assertEqual(report['accepted'], 0)
            self.assertGreater(report['static_rejections' if guard == 'static' else 'hpwl_rejections'], 0)
            self.assertEqual((result.cell_of, result.orient, result.row_y, result.column_x), original)

    def test_offset_is_reset_only_when_its_source_changes_cell(self):
        pl, comps, nets, index = tail_fixture()
        pl.offset_x = [6.0, -6.0, 10.0]
        result, meta = refine.refine_readability(PAR, pl, comps, nets, index, metadata())
        self.assertGreater(meta['layout_readability']['refinement']['accepted'], 0)
        self.assertEqual(result.offset_x[index['bias']], 0)
        self.assertEqual(result.offset_x[:2], pl.offset_x[:2])

    def test_two_sources_are_refined_from_the_latest_accepted_transaction(self):
        devices = dict(a=mos('oa', 'ga', 'tail'), b=mos('ob', 'gb', 'tail'),
                       bias=source('ISRC', 'tail'), driver=mos('out', 'reference'),
                       reference=source('VSRC', 'reference'))
        pl, comps, nets, index = base_placement(devices,
            dict(a=(1, 2), b=(1, 4), bias=(4, 0), driver=(1, 6), reference=(4, 7)), {'b': 1})
        result, meta = refine.refine_readability(PAR, pl, comps, nets, index, metadata())
        report = meta['layout_readability']['refinement']
        before = {row['device']: row['distance'] for row in report['before']['sources']}
        after = {row['device']: row['distance'] for row in report['after']['sources']}
        self.assertTrue(all(after[name] < distance for name, distance in before.items()))
        for action in report['actions']:
            self.assertEqual({change['device'] for change in action['changes']}, {action['device']})

    def test_passive_relationship_regression_cannot_be_offset_by_source_improvement(self):
        pl, comps, nets, index = tail_fixture()
        initial = list(pl.cell_of)
        def passive(current, *args):
            return dict(violations=0 if current.cell_of == initial else 1,
                        shape_error=0., pin_axis_error=0., owner_locality=0.)
        with patch('readability_refinement._passive_measurement', side_effect=passive):
            result, meta = refine.refine_readability(PAR, pl, comps, nets, index, metadata())
        report = meta['layout_readability']['refinement']
        self.assertGreater(report['passive_rejections'], 0)
        self.assertEqual(report['accepted'], 0)
        self.assertEqual(result.cell_of, initial)

    def test_voltage_source_moves_toward_its_gate_without_changing_driver_geometry(self):
        devices = dict(driver=mos('out', 'reference'), voltage=source('VSRC', 'reference'))
        pl, comps, nets, index = base_placement(devices, dict(driver=(1, 4), voltage=(4, 0)))
        original = pl.cell_of[0], pl.orient[0], pl.pos_of(0)
        result, meta = refine.refine_readability(PAR, pl, comps, nets, index, metadata())
        report = meta['layout_readability']['refinement']
        self.assertGreater(report['accepted'], 0)
        self.assertLess(report['after']['source_distance'], report['before']['source_distance'])
        self.assertEqual((result.cell_of[0], result.orient[0], result.pos_of(0)), original)


class CompactionTests(unittest.TestCase):
    def fixture(self):
        devices = dict(a=mos('oa', 'ga'), b=mos('ob', 'gb'))
        return base_placement(devices, dict(a=(1, 1), b=(4, 4)))

    def test_phase_four_compacts_spare_channels_and_preserves_active_rows_and_fixed_world(self):
        pl, comps, nets, index = self.fixture()
        fixed = index['a']; pl.fixed = {fixed}; pl.fixed_cells = {fixed: pl.cell_of[fixed]}
        before_rows = list(pl.row_of)
        position = pl.pos_of(fixed)
        result, meta = refine.refine_readability(PAR, pl, comps, nets, index, metadata(), phase=4)
        report = meta['layout_readability']['refinement']
        self.assertGreater(report['accepted'], 0)
        self.assertLess(report['after']['void'], report['before']['void'])
        self.assertEqual(result.row_of, before_rows)
        self.assertEqual(result.pos_of(fixed), position)
        self.assertTrue(any(action['kind'] == 'axis_compaction' for action in report['actions']))

    def test_feedback_cut_keeps_its_original_physical_channel(self):
        pl, comps, nets, index = self.fixture()
        data = metadata(); data['feedback'] = {'row_cut': 2, 'added_channel_height': 32}
        before = pl.row_y[3]-pl.row_y[2]
        result, meta = refine.refine_readability(PAR, pl, comps, nets, index, data, phase=4)
        self.assertEqual(result.row_y[3]-result.row_y[2], before)

    def test_phase_three_never_compacts_axes_or_moves_passives(self):
        pl, comps, nets, index = self.fixture()
        original = deepcopy(pl.__dict__)
        result, meta = refine.refine_readability(PAR, pl, comps, nets, index, metadata(), phase=3)
        self.assertEqual(result.row_y, original['row_y'])
        self.assertEqual(result.column_x, original['column_x'])
        self.assertEqual(result.cell_of, original['cell_of'])
        self.assertEqual(meta['layout_readability']['refinement']['accepted'], 0)

    def test_one_shared_budget_and_expired_deadline_are_reported(self):
        pl, comps, nets, index = self.fixture()
        result, meta = refine.refine_readability(PAR, pl, comps, nets, index, metadata(),
                                               phase=4, max_candidates=3)
        report = meta['layout_readability']['refinement']
        self.assertLessEqual(report['candidates'], 3)
        self.assertEqual(sum(report['stage_counts'].values()), report['candidates'])
        result, meta = refine.refine_readability(PAR, pl, comps, nets, index, metadata(),
                                               phase=4, deadline=time.perf_counter()-1)
        self.assertEqual(meta['layout_readability']['refinement']['candidates'], 0)
        self.assertEqual(result.cell_of, pl.cell_of)
        self.assertEqual(result.row_y, pl.row_y)

    def test_invalid_settings_are_rejected(self):
        pl, comps, nets, index = self.fixture()
        for settings in ({'phase': 2}, {'seconds': -1}, {'max_candidates': -1}, {'options': 'wrong'}):
            with self.assertRaises(ValueError):
                refine.refine_readability(PAR, pl, comps, nets, index, metadata(), **settings)

    def test_unknown_source_metrics_are_none_but_void_remains_measurable(self):
        pl, comps, nets, index = self.fixture()
        measured = refine.measure_refinements(PAR, pl, comps, nets, index, metadata())
        self.assertEqual(measured['source_count'], 0)
        self.assertIsNone(measured['source_errors'])
        self.assertIsNone(measured['source_distance'])
        self.assertTrue(measured['measurable'])
        self.assertGreater(measured['void'], 0)

    def test_custom_power_geometry_is_not_relocated_or_compressed(self):
        pl, comps, nets, index = self.fixture()
        pl.power_connections = []
        original = list(pl.cell_of), list(pl.row_y), list(pl.column_x)
        result, meta = refine.refine_readability(PAR, pl, comps, nets, index, metadata(), phase=4)
        self.assertEqual((result.cell_of, result.row_y, result.column_x), original)
        self.assertEqual(meta['layout_readability']['refinement']['status'], 'skipped')

    def test_offset_contract_is_rechecked_against_the_compressed_physical_column(self):
        pl, comps, nets, index = self.fixture()
        pl.offset_x = [0., 58.]
        trial = deepcopy(pl)
        trial.column_x[5] = trial.column_x[4]+80
        self.assertFalse(refine._valid(pl, trial, comps, index, set(), RoutingOptions()))
        for value in (float('nan'), 3.):
            trial = deepcopy(pl)
            trial.offset_x[1] = value
            self.assertFalse(refine._valid(pl, trial, comps, index, set(), RoutingOptions()))

    def test_bridge_only_plan_keeps_true_pin_axis_error_guarded(self):
        pl, comps, nets, index = self.fixture()
        plan = {'supported': True, 'series_axes': [{'pins': [['a', 'D'], ['b', 'S']]}], 'patterns': []}
        measured = dict(passive_supported=True, measurable=True, violations=0,
                        shape_error=0., pin_axis_error=2., owner_locality=None)
        with patch('readability_layout.measure_readability', return_value=measured) as callback:
            result = refine._passive_measurement(pl, comps, index, plan)
        callback.assert_called_once()
        self.assertEqual(result['pin_axis_error'], 2.)
        safe, _ = refine._passive_guard(result, dict(result, pin_axis_error=3.))
        self.assertFalse(safe)

    def test_unmeasurable_passive_geometry_never_becomes_a_zero_cost_guard(self):
        unknown = dict(measurable=False, violations=None, shape_error=None,
                       pin_axis_error=None, owner_locality=None)
        self.assertEqual(refine._passive_guard(unknown, unknown), (False, False))


class CompactionApplicabilityTests(unittest.TestCase):
    def fixture(self, rows=(1, 2), passive=False):
        devices = dict(a=mos('oa', 'ga'), b=mos('ob', 'gb'))
        cells = dict(a=(rows[0], 1), b=(rows[1], 6))
        if passive:
            devices['cap'] = resistor('oa', '0', 'C')
            cells['cap'] = (max(rows)+1, 0)
        return base_placement(devices, cells)

    def test_pure_mos_contiguous_active_rows_do_not_use_canvas_void_as_evidence(self):
        pl, comps, nets, index = self.fixture()
        original = deepcopy(pl)
        model = refine.build_refinement_model(PAR, comps, nets, index)
        self.assertEqual(refine.compaction_applicability(pl, comps, {}, model),
            (False, 'no_local_attachment_or_interior_row_band'))
        result, meta = refine.refine_readability(PAR, pl, comps, nets, index, metadata(), phase=4)
        report = meta['layout_readability']['refinement']
        self.assertGreater(report['before']['void'], 0)
        self.assertFalse(report['before']['compaction_applicable'])
        self.assertEqual(report['stage_counts']['compact'], 0)
        self.assertEqual(result.cell_of, original.cell_of)
        self.assertEqual(result.row_y, original.row_y)
        self.assertEqual(result.column_x, original.column_x)
        self.assertEqual(pl.rng.getstate(), original.rng.getstate())

    def test_a_single_capacitor_does_not_enable_global_compaction(self):
        pl, comps, nets, index = self.fixture(passive=True)
        plan = {'patterns': [dict(id='single', kind='single_attachment', members=['cap'])]}
        model = refine.build_refinement_model(PAR, comps, nets, index, plan)
        applicable, _ = refine.compaction_applicability(pl, comps, plan, model)
        self.assertFalse(applicable)

    def test_interior_reserved_row_can_be_empty_or_hold_an_edge_attachment(self):
        for passive in (False, True):
            pl, comps, nets, index = self.fixture(rows=(1, 3), passive=passive)
            if passive:
                cells = list(pl.cell_of); cells[index['cap']] = (2, 0)
                refine._sync(pl, cells, pl.orient)
            model = refine.build_refinement_model(PAR, comps, nets, index)
            self.assertEqual(refine.compaction_applicability(pl, comps, {}, model),
                (True, 'interior_display_row_band'))

    def test_outer_unused_rows_are_not_an_interior_reserved_band(self):
        pl, comps, nets, index = self.fixture()
        pl.row_y.extend([pl.row_y[-1]+140, pl.row_y[-1]+280])
        self.assertFalse(refine.compaction_applicability(pl, comps, {}, {'sources': []})[0])

    def test_source_alone_does_not_enable_compaction_but_real_relationships_still_do(self):
        pl, comps, nets, index = tail_fixture()
        model = refine.build_refinement_model(PAR, comps, nets, index)
        self.assertEqual(refine.compaction_applicability(pl, comps, {'supported': False}, model),
            (False, 'local_source_only_no_core_compaction'))
        pl, comps, nets, index = self.fixture()
        self.assertEqual(refine.compaction_applicability(pl, comps, {'series_axes': [{'pins': []}]}, {}),
            (True, 'verified_series_axes'))
        devices = dict(a=mos('oa', 'ga'), b=mos('ob', 'gb'),
                       r=resistor('oa', 'mid'), c=resistor('mid', '0', 'C'))
        pl, comps, nets, index = base_placement(devices,
            dict(a=(1, 1), b=(2, 6), r=(1, 3), c=(2, 3)))
        plan = {'patterns': [dict(id='chain', kind='series_chain', members=['r', 'c'])]}
        self.assertEqual(refine.compaction_applicability(pl, comps, plan, {}),
            (True, 'nontrivial_passive_relationship'))

    def test_local_current_voltage_and_multiple_sources_do_not_authorize_core_compaction(self):
        variants = [dict(a=mos('oa', 'ga', 'tail'), b=mos('ob', 'gb', 'tail'),
                         bias=source('ISRC', 'tail')),
                    dict(driver=mos('out', 'reference'), voltage=source('VSRC', 'reference')),
                    dict(a=mos('oa', 'ga', 'tail'), b=mos('ob', 'gb', 'tail'),
                         bias=source('ISRC', 'tail'), driver=mos('out', 'reference'),
                         voltage=source('VSRC', 'reference'))]
        for devices in variants:
            cells = {name: (1 if dev['cls'] == 'MOS' else 4, i)
                     for i, (name, dev) in enumerate(devices.items())}
            pl, comps, nets, index = base_placement(devices, cells)
            model = refine.build_refinement_model(PAR, comps, nets, index)
            self.assertTrue(model['sources'])
            self.assertEqual(refine.compaction_applicability(pl, comps, {}, model),
                (False, 'local_source_only_no_core_compaction'))

    def test_single_cap_plus_local_source_is_still_not_a_compaction_trigger(self):
        devices = dict(a=mos('oa', 'ga', 'tail'), b=mos('ob', 'gb', 'tail'),
                       bias=source('ISRC', 'tail'), cap=resistor('oa', '0', 'C'))
        pl, comps, nets, index = base_placement(devices,
            dict(a=(1, 2), b=(1, 4), bias=(4, 0), cap=(2, 6)))
        model = refine.build_refinement_model(PAR, comps, nets, index)
        plan = {'patterns': [dict(id='single', kind='single_attachment', members=['cap'])]}
        self.assertFalse(refine.compaction_applicability(pl, comps, plan, model)[0])

    def test_local_source_with_a_real_inner_band_or_rc_chain_still_compacts(self):
        pl, comps, nets, index = tail_fixture()
        cells = list(pl.cell_of); cells[index['b']] = (3, 4)
        refine._sync(pl, cells, pl.orient)
        model = refine.build_refinement_model(PAR, comps, nets, index)
        self.assertEqual(refine.compaction_applicability(pl, comps, {}, model),
            (True, 'interior_display_row_band'))
        devices = dict(a=mos('oa', 'ga', 'tail'), b=mos('ob', 'gb', 'tail'),
                       bias=source('ISRC', 'tail'), r=resistor('oa', 'mid'), c=resistor('mid', '0', 'C'))
        pl, comps, nets, index = base_placement(devices,
            dict(a=(1, 2), b=(1, 4), bias=(4, 0), r=(2, 6), c=(3, 6)))
        model = refine.build_refinement_model(PAR, comps, nets, index)
        plan = {'patterns': [dict(id='chain', kind='series_chain', members=['r', 'c'])]}
        self.assertEqual(refine.compaction_applicability(pl, comps, plan, model),
            (True, 'nontrivial_passive_relationship'))

    def test_phase_four_keeps_source_locality_improvement_without_moving_active_cells_or_axes(self):
        pl, comps, nets, index = tail_fixture()
        original = deepcopy(pl)
        result, meta = refine.refine_readability(PAR, pl, comps, nets, index, metadata(), phase=4)
        report = meta['layout_readability']['refinement']
        self.assertFalse(report['compaction_applicable'])
        self.assertEqual(report['compaction_reason'], 'local_source_only_no_core_compaction')
        self.assertGreater(report['accepted'], 0)
        self.assertLess(report['after']['source_distance'], report['before']['source_distance'])
        self.assertIsInstance(report['after']['void'], (int, float))
        self.assertEqual(report['stage_counts']['compact'], 0)
        self.assertEqual(result.row_y, original.row_y)
        self.assertEqual(result.column_x, original.column_x)
        for name in ('a', 'b'):
            i = index[name]
            self.assertEqual(result.cell_of[i], original.cell_of[i])
            self.assertEqual(result.orient[i], original.orient[i])
        # Same-plan selection may reward the source improvement, but changing
        # only this drawing's empty area must not change its final rank.
        from readability_selection import organization_rank
        selected = dict(phase=4, plan={}, metrics={}, refinement_metrics=deepcopy(report['after']))
        rank = organization_rank(selected)
        selected['refinement_metrics']['void'] += 100
        selected['refinement_metrics']['compactness'] += 100
        self.assertEqual(organization_rank(selected), rank)

    def test_actual_fvf_inner_band_and_lna_rl_still_qualify(self):
        from netlist_core import parse_netlist, to_document
        paths = [ROOT/'test_data/11-15/self-adaptive-current-mirror-buffer-fvf-ldo.cir',
                 ROOT/'test_data/11-15/lna-with-degeneration.cir']
        for path in paths:
            if not path.is_file():
                self.skipTest('optional original source corpus is unavailable')
            cell = ('self_adaptive_current_mirror_buffer_fvf_ldo' if 'self-adaptive' in path.name else 'main')
            devices = parse_netlist(path, cell=cell)
            doc = to_document(devices); comps, nets = doc['components'], doc['nets']
            index = {row['id']: i for i, row in enumerate(comps)}
            levels = structured.dl.compute_levels(devices)
            conduction = {net for dev in devices.values() if dev['cls'] in ('MOS', 'BJT')
                          for net in (dev.get('d'), dev.get('s'), dev.get('c'), dev.get('e')) if net is not None}
            att = structured.legacy.attach_info(devices, levels['net_levels'], conduction)
            rows, _ = structured.legacy._allocate_rows(devices, comps, index,
                levels['net_levels'], levels['device_levels'], att)
            cells = {name: (rows[i], i) for name, i in index.items()}
            pl, comps, nets, index = base_placement(devices, cells)
            plan = build_readability_plan(devices, comps, nets, index, structured._structures(devices), levels, rows)
            model = refine.build_refinement_model(PAR, comps, nets, index, plan)
            self.assertTrue(refine.compaction_applicability(pl, comps, plan, model)[0], path.name)


class BankOutletRefinementTests(unittest.TestCase):
    def fixture(self):
        devices = dict(a=mos('left', 'right'), b=mos('right', 'left'))
        cells = dict(a=(2, 2), b=(2, 6))
        for suffix, node, start in (('a', 'left', 1), ('b', 'right', 5)):
            for number, kind in enumerate(('R', 'C', 'L')):
                name = kind+suffix
                devices[name] = resistor('vdd', node, kind)
                cells[name] = (3, start+number)
        pl, comps, nets, index = base_placement(devices, cells)
        plan = build_readability_plan(devices, comps, nets, index,
            structured._structures(devices), structured.dl.compute_levels(devices), pl.row_of)
        return pl, comps, nets, index, {'layout_readability': {'plan': plan}}

    def test_both_load_banks_return_to_their_real_drain_outlet_with_shared_budget(self):
        pl, comps, nets, index, data = self.fixture()
        plan = data['layout_readability']['plan']
        old = deepcopy(pl)
        before = measure_bank_outlet(pl, comps, index, plan)
        self.assertEqual(before['count'], 2)
        self.assertGreater(before['error'], 0)
        with patch('orthogonal_router.route_scene', side_effect=AssertionError('A* in bank refinement')):
            result, meta = refine.refine_readability(PAR, pl, comps, nets, index, data, phase=4)
        report = meta['layout_readability']['refinement']
        after = measure_bank_outlet(result, comps, index, plan)
        self.assertEqual(after['error'], 0)
        self.assertTrue(all(record['error'] == 0 for record in after['records']))
        self.assertLessEqual(report['candidates'], 128)
        self.assertTrue(any(action['kind'] == 'bank_outlet_template' for action in report['actions']))
        for name in ('a', 'b'):
            self.assertEqual(result.cell_of[index[name]], old.cell_of[index[name]])
            self.assertEqual(result.orient[index[name]], old.orient[index[name]])
        self.assertEqual(pl.cell_of, old.cell_of)

    def test_bank_templates_are_round_robin_and_keep_a_common_orientation(self):
        pl, comps, nets, index, data = self.fixture()
        plan = data['layout_readability']['plan']
        specs = list(refine._bank_trial_specs(pl, comps, index, plan))
        self.assertGreater(len(specs), 2)
        self.assertNotEqual(specs[0][3]['pattern'], specs[1][3]['pattern'])
        for trial, action in list(refine._passive_trials(pl, comps, index, plan))[:4]:
            ids = [index[name] for name in action['members']]
            self.assertEqual(len({trial.orient[i] for i in ids}), 1)

    def test_phase_three_does_not_measure_or_reposition_load_outlets(self):
        pl, comps, nets, index, data = self.fixture()
        with patch('readability_banks.measure_bank_outlet', side_effect=AssertionError('bank feature in phase3')):
            result, meta = refine.refine_readability(PAR, pl, comps, nets, index, data, phase=3)
            measured = refine.measure_refinements(PAR, pl, comps, nets, index, data, phase=3)
        self.assertIsNone(measured['bank_outlet_error'])
        self.assertEqual(result.cell_of, pl.cell_of)
        self.assertEqual(result.orient, pl.orient)
        self.assertEqual(result.row_y, pl.row_y)

    def test_explicit_fixed_bank_members_keep_their_world_positions(self):
        pl, comps, nets, index, data = self.fixture()
        fixed = {index['Ra'], index['Ca'], index['La']}
        pl.fixed = fixed; pl.fixed_cells = {i: pl.cell_of[i] for i in fixed}
        original = {i: (pl.cell_of[i], pl.orient[i], pl.pos_of(i)) for i in fixed}
        result, meta = refine.refine_readability(PAR, pl, comps, nets, index, data, phase=4)
        self.assertEqual({i: (result.cell_of[i], result.orient[i], result.pos_of(i)) for i in fixed}, original)

    def test_unknown_bank_geometry_is_not_a_zero_outlet_penalty(self):
        pl, comps, nets, index, data = self.fixture()
        missing = dict(count=1, planned_count=2, measurable=False, error=None, records=[], skipped=[])
        with patch('readability_banks.measure_bank_outlet', return_value=missing):
            measured = refine.measure_refinements(PAR, pl, comps, nets, index, data, phase=4)
        self.assertIsNone(measured['bank_outlet_error'])
        self.assertFalse(measured['bank_outlet_measurable'])
        self.assertEqual(refine._compact_key(measured)[0], float('inf'))
        self.assertFalse(refine._bank_guard(measured, measured))


if __name__ == '__main__':
    unittest.main()
