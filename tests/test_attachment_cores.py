"""Core rows are derived before allocation, independent of circuit names."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from netlist2schematic.plugins.attachment import attachment_cores as cores
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment.attachment_selection import measure_candidate, select_attachment_result
from netlist2schematic.plugins.readability.readability_layout import build_readability_plan
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.layout import structured_placement as structured
from tests.test_attachment_flow import candidate
from tests.test_passive_placement import convert, mos, resistor


def fixture(*, bjt=False, dual=False, rename=False):
    p = lambda d: dict(cls='MOS', type='PMOS', model='P', d=d, g='bias', s='vdd', bulk='vdd')
    devices = dict(head_a=p('left'), head_b=p('right'), reference=mos('left', 'left'),
                   output=mos('right', 'left'), load=resistor('right', '0'))
    cells = dict(head_a=(1, 2), head_b=(1, 5), reference=(3, 2), output=(4, 5), load=(2, 7))
    if dual:
        devices = {n: {k: ('PMOS' if v == 'NMOS' else 'NMOS' if v == 'PMOS'
            else '0' if v == 'vdd' else 'vdd' if v == '0' else v) for k, v in d.items()}
            for n, d in devices.items()}
    if bjt:
        for n, d in list(devices.items()):
            if d['cls'] == 'MOS':
                devices[n] = dict(cls='BJT', type='PNP' if d['type'] == 'PMOS' else 'NPN',
                                  model='Q', c=d['d'], b=d['g'], e=d['s'])
    names = {n: n for n in devices}
    if rename:
        names = {n: 'unit' + str(70 - i) for i, n in enumerate(devices)}
        devices = {names[n]: {k: 'signal_' + v if k in ('a', 'b', 'c', 'd', 'e', 'g', 's')
            and v not in ('0', 'vdd') else v for k, v in d.items()}
            for n, d in reversed(list(devices.items()))}
        cells = {names[n]: cell for n, cell in cells.items()}
    pl, comps, nets, index, _ = convert(devices, cells)
    if dual:
        # The load's formerly grounded second terminal is now VDD; reflect
        # its symbol too so the supply stub does not fold through its body.
        pl.orient[index[names['load']]] = 2
    structs, levels = analyze_structures(devices), compute_levels(devices)
    readable = build_readability_plan(devices, comps, nets, index, structs, levels, pl.row_of)
    model = cores.compile_cores(comps, structs, readable['junctions'])
    return devices, pl, comps, nets, index, structs, levels, readable, model, names


class LocalMirrorRowTests(unittest.TestCase):
    def test_true_mirror_requires_terminal_proof_and_unique_local_heads(self):
        d, pl, c, n, i, s, lv, readable, model, names = fixture()
        core, = model['cores']
        self.assertEqual(core['roles'], dict(reference='reference', output='output',
            reference_head='head_a', output_head='head_b'))
        self.assertEqual(core['members'], ['output', 'reference'])
        self.assertEqual(core['branches'], [['head_a', 'reference'], ['head_b', 'output']])
        self.assertTrue(core['original_structures'])
        self.assertEqual(cores.compile_cores(list(reversed(c)), s, readable['junctions']), model)

    def test_common_supply_or_bias_without_diode_is_not_a_core(self):
        d, pl, c, n, i, s, lv, readable, model, names = fixture()
        changed = deepcopy(c)
        changed[i['reference']]['nodes']['D'] = 'different'
        self.assertFalse(cores.compile_cores(changed, s, readable['junctions'])['cores'])

    def test_remote_mirror_and_ambiguous_heads_are_explicitly_not_applicable(self):
        d, pl, c, n, i, s, lv, readable, model, names = fixture()
        for mode in ('remote', 'different_bias', 'two_heads'):
            cs, js = deepcopy(c), deepcopy(readable['junctions'])
            if mode == 'remote':
                js['right']['conduction_ports'] = [['output', 'D']]
            elif mode == 'different_bias':
                cs[i['head_b']]['nodes']['G'] = 'other_bias'
            else:
                js['right']['conduction_ports'].append(['head_a', 'D'])
            result = cores.compile_cores(cs, s, js)
            self.assertEqual(result['cores'], [])
            self.assertTrue(result['skipped'])

    def test_more_outputs_or_other_control_users_do_not_choose_an_arbitrary_pair(self):
        d, pl, c, n, i, s, lv, readable, model, names = fixture()
        third = deepcopy(c[i['output']]); third['id'] = 'third'; third['nodes']['D'] = 'third_out'
        result = cores.compile_cores(c + [third], s, readable['junctions'])
        self.assertEqual(result['cores'], [])
        changed = deepcopy(readable['junctions'])
        changed['left']['control_ports'].append(['head_b', 'G'])
        self.assertEqual(cores.compile_cores(c, s, changed)['cores'], [])

    def test_bjt_polarity_dual_rename_and_reorder_preserve_roles(self):
        for kwargs in ({'bjt': True}, {'dual': True}, {'bjt': True, 'dual': True}, {'rename': True}):
            with self.subTest(kwargs=kwargs):
                d, pl, c, n, i, s, lv, readable, model, names = fixture(**kwargs)
                core, = model['cores']
                self.assertEqual(core['roles']['reference'], names['reference'])
                self.assertEqual(core['roles']['output'], names['output'])
                planned, _ = cores.plan_core_rows(d, c, i, lv, readable, model)
                rows = planned['display_rows']
                self.assertEqual(rows[names['reference']], rows[names['output']])
                for order in planned['branch_row_plan']['strict_orders']:
                    self.assertLess(rows[order['upper']], rows[order['lower']])

    def test_row_plan_precedes_allocation_and_preserves_levels_inputs_and_rng(self):
        d, pl, c, n, i, s, lv, readable, model, names = fixture()
        before = deepcopy((d, c, lv, readable, model, pl.cell_of, pl.orient,
                           pl.row_of, pl.comp_at, pl.column_x, pl.row_y, pl.rng.getstate()))
        planned, report = cores.plan_core_rows(d, c, i, lv, readable, model)
        self.assertEqual(planned['display_rows']['reference'], planned['display_rows']['output'])
        att = structured.legacy.attach_info(d, lv['net_levels'], {'left', 'right', 'vdd', '0'})
        with patch('netlist2schematic.routing.orthogonal_router._find_path', side_effect=AssertionError('Astar')):
            seed, _, _ = structured._initial_placement(d, c, n, i, lv, att, s, 37, 1,
                passive_layout='flexible', row_override=[planned['display_rows'][x['id']] for x in c])
        self.assertEqual(seed.cell_of[i['reference']][0], seed.cell_of[i['output']][0])
        self.assertNotEqual(seed.cell_of[i['reference']], seed.cell_of[i['output']])
        self.assertEqual(len(seed.comp_at), len(c))
        self.assertEqual((d, c, lv, readable, model, pl.cell_of, pl.orient,
                          pl.row_of, pl.comp_at, pl.column_x, pl.row_y, pl.rng.getstate()), before)
        self.assertEqual(report['applied_relations'], [model['cores'][0]['id']])

    def test_joint_plan_keeps_confirmed_rl_and_rejects_incompatible_cohort(self):
        d, pl, c, n, i, s, lv, readable, model, names = fixture()
        old = deepcopy(readable)
        old['branch_row_plan'] = dict(bridge_alignment=[dict(id='already_proved_rl')])
        result, report = cores.plan_core_rows(d, c, i, lv, old, model)
        self.assertEqual(result, old)
        self.assertEqual(report['unapplied_relations'][0]['reason'], 'would_displace_confirmed_rl_attachment')
        changed = deepcopy(readable)
        with patch('netlist2schematic.plugins.layout_shared.branch_levels.plan_branch_rows',
            return_value=([], dict(bridge_alignment=[], same_row=[], skipped_relations=[
                dict(relation=model['cores'][0]['id'], reason='same_row_conflicts_with_conduction')]))):
            result, report = cores.plan_core_rows(d, c, i, lv, changed, model)
        self.assertEqual(result, changed)
        self.assertFalse(report['applied_relations'])
        self.assertTrue(report['unapplied_relations'])

    def test_measurement_uses_actual_coordinates_and_detects_branch_reversal(self):
        d, pl, c, n, i, s, lv, readable, model, names = fixture()
        a = cores.measure_cores(pl, c, i, model)
        self.assertEqual(a['row_errors'], 1)
        cells = list(pl.cell_of); cells[i['reference']] = 4, 2
        pl.set_state(cells, pl.orient)
        b = cores.measure_cores(pl, c, i, model)
        self.assertEqual(b['row_errors'], 0)
        self.assertLess(cores.core_rank(b), cores.core_rank(a))
        cells[i['reference']], cells[i['output']] = cells[i['output']], cells[i['reference']]
        pl.set_state(cells, pl.orient)
        self.assertEqual(cores.measure_cores(pl, c, i, model)['order_errors'], 1)

    def test_translation_mirror_and_polarity_dual_keep_measurement_equivalent(self):
        d, pl, c, n, i, s, lv, readable, model, names = fixture()
        class View:
            def pos_of(self, j):
                x, y = pl.pos_of(j); return 3000-x, y+91
            def pin_world(self, j, pin):
                x, y = pl.pin_world(j, pin); return 3000-x, y+91
            def pin_dir(self, j, pin):
                d = pl.pin_dir(j, pin); return {'east': 'west', 'west': 'east'}.get(d, d)
        self.assertEqual(cores.core_rank(cores.measure_cores(pl, c, i, model)),
                         cores.core_rank(cores.measure_cores(View(), c, i, model)))

    def test_missing_changed_or_nonfinite_evidence_cannot_appear_perfect(self):
        d, pl, c, n, i, s, lv, readable, model, names = fixture()
        cs = deepcopy(c); cs[i['reference']]['nodes']['G'] = 'changed'
        bad = cores.measure_cores(pl, cs, i, model)
        self.assertIsNone(bad['row_errors']); self.assertIsNone(cores.core_rank(bad))
        self.assertFalse(cores.measure_cores(pl, c, i, {})['measurable'])
        good = cores.measure_cores(pl, c, i, model)
        good['support_pin_error'] = float('nan')
        self.assertIsNone(cores.core_rank(good))

    def test_core_gain_participates_in_selection_under_original_route_caps(self):
        old, new = candidate('1'), candidate('2', score=12, length=1100)
        metric = dict(count=1, applicable=1, measurable=True, row_errors=1, order_errors=0,
                      interleaving=0, row_distance=1, support_pin_error=0, control_facing_errors=0)
        old[5]['layout_attachments']['active_core_organization'] = deepcopy(metric)
        metric.update(row_errors=0, row_distance=0)
        new[5]['layout_attachments']['active_core_organization'] = deepcopy(metric)
        chosen, report = select_attachment_result([old, new])
        self.assertIs(chosen, new); self.assertEqual(chosen[7]['score'], 12)
        del new[5]['layout_attachments']['active_core_organization']
        self.assertIs(select_attachment_result([old, new])[0], old)
        new[5]['layout_attachments']['active_core_organization'] = metric
        new[6][1]['crossings'] = 1
        self.assertIs(select_attachment_result([old, new])[0], old)

    def test_off_and_reference_candidate_keep_old_algorithm_and_random_stream(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'renamed.cir'
            path.write_text('.model n nmos\n.model p pmos\n'
                'Mha a bias vdd vdd p\nMhb b bias vdd vdd p\n'
                'Mref a a 0 0 n\nMout b a 0 0 n\nRload b 0 1k\n', encoding='utf8')
            off = next(structured.create_candidates(path, iterations=0, attachment_layout='off'))
            with patch.object(ai, 'build_attachment_plan', side_effect=AssertionError('reference must precede new plan')):
                on = next(structured.create_candidates(path, iterations=0, attachment_layout='all'))
            self.assertEqual(off[0].cell_of, on[0].cell_of)
            self.assertEqual(off[0].orient, on[0].orient)
            self.assertEqual(off[0].rng.getstate(), on[0].rng.getstate())
            self.assertEqual(off[4]['energy'], on[4]['energy'])

    def test_active_rows_and_occupancy_remain_consistent_during_moves_and_undo(self):
        d, pl, c, n, i, s, lv, readable, model, names = fixture()
        plan = ai.build_attachment_plan(d, c, n, i, s, lv, pl.row_of, policy='all')
        att = structured.legacy.attach_info(d, lv['net_levels'], {'left', 'right', 'vdd', '0'})
        seed, _, _ = structured._initial_placement(d, c, n, i, lv, att, s, 37, 1,
            passive_layout='flexible', row_override=[plan['display_rows'][x['id']] for x in c])
        installed = ai._install(seed, c, i, plan)
        self.assertTrue(installed.valid_state())
        fixed_rows = dict(installed.fixed_rows)
        with patch('netlist2schematic.routing.orthogonal_router._find_path', side_effect=AssertionError('Astar')):
            for k in range(150):
                before = deepcopy((installed.cell_of, installed.orient, installed.row_of, installed.comp_at))
                old = installed.propose()
                if old is not None:
                    self.assertTrue(installed.valid_state())
                    if k % 2:
                        installed.set_state(*old)
                        self.assertEqual((installed.cell_of, installed.orient, installed.row_of, installed.comp_at), before)
                self.assertEqual({j: installed.cell_of[j][0] for j in fixed_rows}, fixed_rows)
                self.assertEqual(installed.row_of, [r for r, _ in installed.cell_of])


if __name__ == '__main__':
    unittest.main()
