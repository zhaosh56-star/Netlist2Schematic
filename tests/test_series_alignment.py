"""Generic, transactional post-SA alignment without another routing search."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import unittest
from unittest.mock import patch

from netlist2schematic.layout import series_alignment as series
from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.routing_rules import RoutingOptions
from tests.test_passive_placement import base_placement, resistor

PAR = structured.par


def device(kind, first, control, second):
    if kind in ('nmos', 'pmos'):
        return dict(cls='MOS', type='NMOS' if kind == 'nmos' else 'PMOS',
                    d=first, g=control, s=second, bulk='0', model='device')
    return dict(cls='BJT', type='NPN' if kind == 'npn' else 'PNP',
                c=first, b=control, e=second, model='device')


def fixture(kind='nmos', *, orientations=None):
    if kind in ('nmos', 'npn'):
        devices = {'upper': device(kind, 'top', 'cu', 'join'),
                   'lower': device(kind, 'join', 'cl', '0')}
    else:
        devices = {'upper': device(kind, 'join', 'cu', 'vdd'),
                   'lower': device(kind, 'bottom', 'cl', 'join')}
    pl, comps, nets, index = base_placement(devices,
        {'upper': (1, 3), 'lower': (2, 1)}, orientations=orientations)
    metadata = prepare(pl, comps, nets, index, devices)
    return pl, comps, nets, index, metadata


def prepare(pl, comps, nets, index, devices):
    records = structured._structures(devices)
    alignment, pairs, blocks = structured._relations(devices, records, index, pl.row_of)
    pl.blocks = blocks
    pl.block_of = {i: n for n, block in enumerate(blocks) for i in block}
    energy, metrics = structured._estimate(pl, nets, index, alignment, pairs)
    return dict(layout_style='structured', candidate_id=1, candidate_seed=123,
        structs=records, energy=energy, optimization_metrics=metrics,
        sym_pairs=[(comps[a]['id'], comps[b]['id']) for a, b, _ in pairs],
        alignment_relations=[dict(devices=[comps[a]['id'], comps[b]['id']], weight=w)
                             for a, b, w in alignment],
        layout_grid=dict(column_x=list(pl.column_x), row_y=list(pl.row_y)))


def refine(case, **kwargs):
    pl, comps, nets, index, metadata = case
    return series.refine_series_alignment(PAR, pl, comps, nets, index, metadata,
                                          RoutingOptions(), **kwargs)


class SeriesEvidenceTests(unittest.TestCase):
    def test_four_polarities_accept_correct_terminal_roles(self):
        for kind in ('nmos', 'pmos', 'npn', 'pnp'):
            with self.subTest(kind=kind):
                pl, comps, nets, index, _ = fixture(kind)
                links, _ = series.discover_series(PAR, pl, comps, nets, index)
                self.assertEqual(len(links), 1)
                self.assertEqual((links[0]['upper'], links[0]['lower']), ('upper', 'lower'))
                result, data = refine(fixture(kind))
                self.assertTrue(data['series_alignment']['applied'])
                self.assertEqual(data['series_alignment']['current']['horizontal_error'], 0)

    def test_control_taps_do_not_break_a_unique_conduction_segment(self):
        devices = {'upper': device('nmos', 'top', 'cu', 'join'),
                   'lower': device('nmos', 'join', 'join', '0'),
                   'tap': device('nmos', 'other', 'join', '0')}
        pl, comps, nets, index = base_placement(devices,
            {'upper': (1, 3), 'lower': (2, 1), 'tap': (2, 5)})
        links, _ = series.discover_series(PAR, pl, comps, nets, index)
        self.assertEqual([r['net'] for r in links], ['join'])

    def test_additional_active_conduction_terminal_is_ambiguous(self):
        devices = {'upper': device('nmos', 'top', 'cu', 'join'),
                   'lower': device('nmos', 'join', 'cl', '0'),
                   'tap': device('nmos', 'other', 'ct', 'join')}
        pl, comps, nets, index = base_placement(devices,
            {'upper': (1, 3), 'lower': (2, 1), 'tap': (2, 5)})
        links, skipped = series.discover_series(PAR, pl, comps, nets, index)
        self.assertEqual(links, [])
        self.assertEqual(skipped[0]['reason'], 'additional_conductive_tap')

    def test_passive_or_source_tap_is_a_real_branch_not_a_gate_tap(self):
        for extra in (resistor('join', '0'), dict(cls='ISRC', type='I', p='join', n='0', value='1u')):
            devices = {'upper': device('nmos', 'top', 'cu', 'join'),
                       'lower': device('nmos', 'join', 'cl', '0'), 'tap': extra}
            pl, comps, nets, index = base_placement(devices,
                {'upper': (1, 3), 'lower': (2, 1), 'tap': (3, 5)})
            links, skipped = series.discover_series(PAR, pl, comps, nets, index)
            self.assertEqual(links, [])
            self.assertEqual(skipped[0]['reason'], 'additional_conductive_tap')

    def test_supply_nodes_and_drain_drain_joins_are_not_series(self):
        pl, comps, nets, index, _ = fixture()
        for name, terms in (('vdd', [('upper', 'S'), ('lower', 'D')]),
                            ('0', [('upper', 'S'), ('lower', 'D')]),
                            ('join', [('upper', 'D'), ('lower', 'D')])):
            links, _ = series.discover_series(PAR, pl, comps, {name: terms}, index)
            self.assertEqual(links, [])

    def test_wrong_display_order_and_same_row_are_skipped(self):
        for cells in ([(2, 3), (1, 1)], [(1, 3), (1, 1)]):
            pl, comps, nets, index, _ = fixture()
            pl.set_state(cells, pl.orient)
            pl.row_of = [r for r, c in cells]
            links, skipped = series.discover_series(PAR, pl, comps, nets, index)
            self.assertEqual(links, [])
            self.assertEqual(skipped[0]['reason'], 'display_order_conflict')


class SeriesRefinementTests(unittest.TestCase):
    def test_alignment_improves_existing_estimate_without_mutating_input(self):
        case = fixture()
        pl, comps, nets, index, metadata = case
        old_cells, old_orient = deepcopy(pl.cell_of), list(pl.orient)
        original_data = deepcopy(metadata)
        result, data = refine(case)
        report = data['series_alignment']
        self.assertTrue(report['applied'])
        self.assertEqual(report['current']['horizontal_error'], 0)
        self.assertLess(data['energy'], metadata['energy'])
        self.assertEqual(data['optimization_metrics']['measurement_stage'], 'after_series_alignment')
        self.assertEqual(report['source_sa_energy'], metadata['energy'])
        self.assertEqual(pl.cell_of, old_cells)
        self.assertEqual(pl.orient, old_orient)
        self.assertEqual(metadata, original_data)
        self.assertEqual(result.row_y, pl.row_y)
        self.assertEqual(result.column_x, pl.column_x)
        self.assertEqual(result.orient, pl.orient)
        self.assertEqual(result.row_of, pl.row_of)
        self.assertEqual(result.comp_at, {cell: i for i, cell in enumerate(result.cell_of)})

    def test_opposite_mirrors_use_true_pins_and_keep_orientations(self):
        case = fixture(orientations={'upper': 1, 'lower': 0})
        result, data = refine(case)
        self.assertTrue(data['series_alignment']['applied'])
        self.assertEqual(result.orient, case[0].orient)
        self.assertEqual(data['series_alignment']['current']['horizontal_error'], 20)

    def test_aligned_neighbor_is_preserved_by_moving_a_short_chain(self):
        devices = {'upper': device('nmos', 'top', 'cu', 'j1'),
                   'middle': device('nmos', 'j1', 'cm', 'j2'),
                   'lower': device('nmos', 'j2', 'cl', '0')}
        pl, comps, nets, index = base_placement(devices,
            {'upper': (1, 3), 'middle': (2, 1), 'lower': (3, 1)})
        pl.fixed = {index['upper']}
        pl.fixed_cells = {index['upper']: pl.cell_of[index['upper']]}
        data = prepare(pl, comps, nets, index, devices)
        result, output = refine((pl, comps, nets, index, data))
        self.assertEqual(output['series_alignment']['current']['horizontal_error'], 0)
        self.assertEqual(result.cell_of[index['middle']][1], 3)
        self.assertEqual(result.cell_of[index['lower']][1], 3)
        self.assertEqual(result.cell_of[index['upper']], pl.cell_of[index['upper']])

    def test_fixed_devices_are_never_moved_or_reoriented(self):
        case = fixture()
        pl = case[0]
        pl.fixed = set(range(len(pl.comps)))
        pl.fixed_cells = {i: cell for i, cell in enumerate(pl.cell_of)}
        result, data = refine(case)
        self.assertIs(result, pl)
        self.assertFalse(data['series_alignment']['applied'])

    def test_occupied_cells_and_leaf_protection_remain_hard_constraints(self):
        case = fixture()
        pl, comps, nets, index, metadata = case
        # Add a same-row child and a foreign interloper to a candidate state.
        devices = {'upper': device('nmos', 'top', 'cu', 'join'),
                   'lower': device('nmos', 'join', 'cl', '0'),
                   'sibling': device('nmos', 'd1', 'g1', '0'),
                   'foreign': device('nmos', 'd2', 'g2', '0')}
        pl, comps, nets, index = base_placement(devices,
            {'upper': (1, 4), 'lower': (2, 1), 'sibling': (2, 2), 'foreign': (2, 3)},
            blocks=[['lower', 'sibling']])
        trial = deepcopy(pl)
        cells = list(trial.cell_of)
        cells[index['lower']] = (2, 4)
        trial.set_state(cells, trial.orient)
        self.assertFalse(series._valid_trial(trial, pl, {index['lower']}, set()))
        cells[index['lower']] = (2, 3)
        trial.set_state(cells, trial.orient)
        self.assertFalse(series._valid_trial(trial, pl, {index['lower']}, set()))

    def test_static_geometry_failure_discards_the_whole_trial(self):
        case = fixture()
        safe = dict(count=0, hard=set())
        fault = dict(count=1, hard={('foreign_body',)})
        with patch('netlist2schematic.plugins.preflight.placement_preflight._static_faults',
                   side_effect=[safe]+[fault]*20):
            result, data = refine(case)
        self.assertIs(result, case[0])
        self.assertFalse(data['series_alignment']['applied'])
        self.assertGreater(data['series_alignment']['rejections']['static_geometry'], 0)

    def test_original_static_fault_is_not_hidden_by_alignment(self):
        case = fixture()
        with patch('netlist2schematic.plugins.preflight.placement_preflight._static_faults',
                   return_value=dict(count=1, hard={('marker',)})):
            result, data = refine(case)
        self.assertIs(result, case[0])
        self.assertEqual(data['series_alignment']['stop_reason'], 'unsafe_initial_geometry')
        self.assertEqual(data['series_alignment']['trials'], 0)

    def test_timeout_keeps_the_last_fully_checked_candidate(self):
        case = fixture()
        clock = [0.0]
        calls = []
        def check(*args):
            calls.append(1)
            if len(calls) == 2:
                clock[0] = 2.0
            return dict(count=0, hard=set())
        with patch.object(series, 'perf_counter', side_effect=lambda: clock[0]), \
             patch('netlist2schematic.plugins.preflight.placement_preflight._static_faults', side_effect=check):
            result, data = refine(case, seconds=1)
        self.assertTrue(data['series_alignment']['applied'])
        self.assertEqual(data['series_alignment']['stop_reason'], 'time_budget')
        self.assertEqual(data['series_alignment']['current']['horizontal_error'], 0)
        from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
        check = _static_faults(PAR, result, case[2], case[3], RoutingOptions(), None)
        self.assertFalse(check['count'] or check['hard'])

    def test_timeout_during_trial_validation_rolls_back_unfinished_trial(self):
        case = fixture()
        timeout = RoutingFailure('time', dict(reason='quality_time_budget'))
        with patch('netlist2schematic.plugins.preflight.placement_preflight._static_faults',
                   side_effect=[dict(count=0, hard=set()), timeout]):
            result, data = refine(case)
        self.assertIs(result, case[0])
        self.assertFalse(data['series_alignment']['applied'])
        self.assertEqual(data['series_alignment']['stop_reason'], 'time_budget')

    def test_zero_budget_and_trial_cap_never_reset(self):
        for kwargs in (dict(seconds=0), dict(max_trials=0)):
            case = fixture()
            result, data = refine(case, **kwargs)
            self.assertIs(result, case[0])
            self.assertEqual(data['series_alignment']['trials'], 0)
        case = fixture()
        result, data = refine(case, max_trials=1)
        self.assertLessEqual(data['series_alignment']['trials'], 1)

    def test_already_aligned_result_is_idempotent_and_saved_measurement_only(self):
        case = fixture()
        pl, output = refine(case)
        current = deepcopy(pl.cell_of)
        again, data = refine((pl, *case[1:4], output))
        self.assertIs(again, pl)
        self.assertFalse(data['series_alignment']['applied'])
        self.assertEqual(data['series_alignment']['stop_reason'], 'already_aligned')
        self.assertEqual(data['series_alignment']['inherited_actions'], output['series_alignment']['actions'])
        shifted = deepcopy(pl)
        shifted.row_y = [v+40 for v in shifted.row_y]
        measured = series.measure_series(shifted, case[3], output['series_alignment']['relations'])
        self.assertEqual(measured['horizontal_error'], 0)
        self.assertEqual(shifted.cell_of, current)

    def test_no_sa_or_astar_is_called_by_refinement(self):
        with patch.object(structured, '_anneal', side_effect=AssertionError('SA')), \
             patch('netlist2schematic.routing.orthogonal_router._find_path', side_effect=AssertionError('A*')):
            _, data = refine(fixture())
        self.assertTrue(data['series_alignment']['applied'])

    def test_translated_mirrored_renamed_reordered_cases_still_align(self):
        for mirrored in (False, True):
            case = fixture()
            pl, comps, nets, index, metadata = case
            if mirrored:
                pl.set_state([(r, pl.cols-1-c) for r, c in pl.cell_of], [1-o for o in pl.orient])
            pl.column_x = [x+274 for x in pl.column_x]
            pl.row_y = [y-36 for y in pl.row_y]
            # Rename every device and non-power node and reverse component order.
            names = {c['id']: f'instance_{60-i}' for i, c in enumerate(comps)}
            renamed = []
            for c in reversed(comps):
                c = deepcopy(c)
                c['id'] = names[c['id']]
                c['nodes'] = {p: ('n_'+n if n not in ('0', 'vdd') else n) for p, n in c['nodes'].items()}
                renamed.append(c)
            old_indices = list(reversed(range(len(comps))))
            pl.comps = renamed
            pl.set_state([pl.cell_of[i] for i in old_indices], [pl.orient[i] for i in old_indices])
            pl.row_of = [r for r, c in pl.cell_of]
            ix = {c['id']: i for i, c in enumerate(renamed)}
            ns = {}
            for c in renamed:
                for p, n in c['nodes'].items():
                    ns.setdefault(n, []).append((c['id'], p))
            # The pure geometry estimate is sufficient for this generic fixture.
            output, data = refine((pl, renamed, ns, ix, dict(layout_style='structured', structs=[])))
            self.assertTrue(data['series_alignment']['applied'])
            self.assertEqual(data['series_alignment']['current']['horizontal_error'], 0)


class SeriesFlowOrderTests(unittest.TestCase):
    def test_alignment_runs_after_preflight_before_one_final_route(self):
        from netlist2schematic.flow import aesthetic_flow
        case = fixture()
        order = []
        def preflight(par, pl, *args, **kwargs):
            order.append('preflight')
            return pl, dict(static_verification=dict(complete=True, valid=True, fault_count=0))
        def align(par, pl, cs, ns, ix, data, options=None, **kwargs):
            order.append('align')
            return pl, dict(data, series_alignment=dict(version=series.VERSION, applied=False))
        def route(*args, **kwargs):
            order.append('route')
            return [], dict(wirelength=10, crossings=0, bends=0), dict(valid=True)
        with patch.object(structured, 'create_candidates', return_value=iter([case])), \
             patch('netlist2schematic.plugins.preflight.placement_preflight.repair_static_ports', side_effect=preflight), \
             patch.object(series, 'refine_series_alignment', side_effect=align), \
             patch.object(aesthetic_flow, 'final_route', side_effect=route) as routed, \
             patch('netlist2schematic.layout.layout_quality.evaluate_quality', return_value=dict(score=1, per_net={})), \
             redirect_stdout(io.StringIO()):
            result = aesthetic_flow.select_layout('unused.cir', port_preflight='repair')
        self.assertEqual(order, ['preflight', 'align', 'route'])
        self.assertEqual(routed.call_count, 1)
        self.assertIn('series_alignment', result[4]['quality']['candidates'][0])

    def test_explicit_legacy_reference_does_not_run_the_new_refiner(self):
        from netlist2schematic.flow import aesthetic_flow
        case = fixture()
        case[4]['candidate_id'] = 0
        case[4]['layout_style'] = 'legacy'
        with patch.object(structured, 'create_candidates', return_value=iter([case])), \
             patch.object(series, 'refine_series_alignment', side_effect=AssertionError('legacy aligned')), \
             patch.object(aesthetic_flow, 'final_route', return_value=([],
                 dict(wirelength=10, crossings=0, bends=0), dict(valid=True))), \
             patch('netlist2schematic.layout.layout_quality.evaluate_quality', return_value=dict(score=1, per_net={})), \
             redirect_stdout(io.StringIO()):
            aesthetic_flow.select_layout('unused.cir', candidate_id=0)


if __name__ == '__main__':
    unittest.main()
