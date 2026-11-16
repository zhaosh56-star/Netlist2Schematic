"""Port-topology attachment packing stays bounded, reversible and optional."""
from copy import deepcopy
import random
from time import perf_counter
from unittest import TestCase
from unittest.mock import patch

from netlist2schematic.layout.structured_placement import StructuredPlacement, par
from netlist2schematic.layout.passive_placement import make_flexible
from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.plugins.region.region_controls import (
    compile_control_networks, measure_control_networks, refine_region_controls)
from netlist2schematic.plugins.symmetry.symmetry_layout import _static_safe
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.routing_rules import RoutingOptions, build_scene
from tests.test_region_layout import mos


MODULE = 'netlist2schematic.plugins.region.region_controls.'


def example():
    devices = dict(left=mos('u', 'g0', 'tail', bulk='0'),
                   right=mos('v', 'g1', 'tail', bulk='0'),
                   load0=mos('x', 'bias', 'vdd', 'PMOS', 'vdd'),
                   load1=mos('y', 'bias', 'vdd', 'PMOS', 'vdd'))
    for number in (0, 1):
        for name, kind, a, b in (('r', 'R', 'g'+str(number), 'mid'+str(number)),
                                ('c', 'C', 'mid'+str(number), 'external'+str(number)),
                                ('f', 'R', 'g'+str(number), ('x', 'y')[number])):
            devices[name+str(number)] = dict(cls='PASSIVE', type=kind, a=a, b=b, value='1')
    document = to_document(devices)
    comps, nets = document['components'], document['nets']
    index = {c['id']: i for i, c in enumerate(comps)}
    positions = dict(left=(3, 3), right=(3, 4), load0=(1, 0), load1=(1, 1),
                     r0=(2, 2), c0=(2, 1), f0=(4, 3), r1=(3, 6), c1=(3, 7), f1=(3, 2))
    cells = [positions[c['id']] for c in comps]
    pl = StructuredPlacement(comps, random.Random(17), [r for r, _ in cells], cols=8)
    pl.column_x = [64+124*c for c in range(8)]
    pl.row_y = [64+126*r for r in range(8)]
    pl.offset_x = [0.0]*len(comps)
    pl.set_state(cells, [int(c['id'] == 'left') for c in comps])
    pl.blocks = [[index['left'], index['right']]]
    pl = make_flexible(pl, devices, nets, index, [])
    plan = dict(supported=True, signature='port-networks', power_interfaces=['vdd', '0'],
                ownership={n: 'input' if n in ('left', 'right') else 'output'
                           for n in ('left', 'right', 'load0', 'load1')},
                regions=[dict(id='input', kind='folded_input', members=['left', 'right'], children=[],
                              pairs=[['left', 'right']]),
                         dict(id='output', kind='folded_output', members=['load0', 'load1'], children=[])],
                series_axes=[], junctions=[])
    metadata = {'layout_regions': dict(applied=True, plan=plan,
                                     initialization=dict(trials=0, elapsed_seconds=0))}
    return pl, pl.comps, nets, index, metadata


def state(pl):
    return deepcopy((pl.cell_of, pl.orient, pl.offset_x, pl.row_of, pl.comp_at,
                     pl.row_y, pl.column_x, pl.fixed_rows, pl.rng.getstate(),
                     getattr(pl, 'terminal_orientation_ids', None)))


class RegionControlTests(TestCase):
    def test_compile_all_paths_without_treating_return_as_amplifier_direction(self):
        pl, c, n, i, m = example()
        model = compile_control_networks(c, n, i, m['layout_regions']['plan'])
        self.assertEqual(len(model['groups']), 2)
        for g in model['groups']:
            self.assertEqual(len(g['external']['members']), 2)
            self.assertEqual(len(g['returned']['members']), 1)
            self.assertEqual(g['external']['path'][0], g['net'])
            self.assertEqual(g['returned']['path'][0], g['net'])
        self.assertTrue(measure_control_networks(pl, c, i, model)['measurable'])

    def test_rehomes_complete_chains_faces_pair_outward_and_preserves_conduction_points(self):
        pl, c, n, i, m = example()
        before = state(pl)
        with patch('netlist2schematic.routing.sa_routing.final_route', side_effect=AssertionError('A*')):
            trial, out = refine_region_controls(par, pl, c, n, i, m)
        report = out['layout_regions']['control_refinement']
        self.assertTrue(report['applied'])
        self.assertLess(report['after']['endpoint_distance'], report['before']['endpoint_distance'])
        self.assertEqual(report['after']['backward_distance'], 0)
        self.assertTrue(trial.valid_state())
        self.assertTrue(_static_safe(par, trial, n, i, RoutingOptions(), None))
        for name, direction in (('left', 'west'), ('right', 'east')):
            self.assertEqual(trial.pin_dir(i[name], 'G'), direction)
            for pin in ('D', 'S'):
                self.assertEqual(trial.pin_world(i[name], pin), pl.pin_world(i[name], pin))
        for name in ('load0', 'load1'):
            self.assertEqual((trial.pos_of(i[name]), trial.orient[i[name]]),
                             (pl.pos_of(i[name]), pl.orient[i[name]]))
        self.assertEqual((trial.column_x, trial.row_y), (pl.column_x, pl.row_y))
        self.assertEqual(state(pl), before)

    def test_taps_shared_controls_rails_and_cyclic_paths_do_not_gain_guessed_ownership(self):
        for kind in ('tap', 'other_control', 'rail', 'loop'):
            pl, c, n, i, m = example()
            if kind == 'tap':
                n['mid0'].append(('right', 'G'))
            elif kind == 'other_control':
                n['g0'].append(('right', 'G'))
            elif kind == 'rail':
                m['layout_regions']['plan']['power_interfaces'].append('external0')
            else:
                pin = next(p for p, net in c[i['c0']]['nodes'].items() if net == 'external0')
                c[i['c0']]['nodes'][pin] = 'g0'
                n['g0'].append(('c0', pin))
                n.pop('external0')
            model = compile_control_networks(c, n, i, m['layout_regions']['plan'])
            self.assertNotIn('left', [g['host'] for g in model['groups']], kind)

    def test_fixed_members_and_fixed_passive_policy_are_not_moved(self):
        for kind in ('host', 'passive', 'policy', 'off'):
            pl, c, n, i, m = example()
            if kind == 'host': pl.fixed.add(i['left'])
            elif kind == 'passive': pl.fixed.add(i['r0'])
            elif kind == 'policy': pl.passive_policy = 'fixed'
            else: m['layout_regions']['applied'] = False
            before = state(pl)
            trial, out = refine_region_controls(par, pl, c, n, i, m)
            self.assertEqual(state(trial), before)
            self.assertEqual(state(pl), before)

    def test_static_rejection_and_timeout_restore_entire_transaction(self):
        for effect in (False, RoutingFailure('deadline', {'reason': 'quality_time_budget'})):
            pl, c, n, i, m = example();before = state(pl)
            kw = {'side_effect': effect} if isinstance(effect, Exception) else {'return_value': effect}
            with patch(MODULE+'_static_safe', **kw):
                trial, out = refine_region_controls(par, pl, c, n, i, m)
            self.assertEqual(state(trial), before)
            self.assertFalse(out['layout_regions']['control_refinement']['applied'])

    def test_exhausted_shared_quota_or_deadline_does_not_start_new_budget(self):
        for budget, deadline in ((dict(seconds=1, max_candidates=128, elapsed_seconds=1, candidates=0), None),
                                 (dict(seconds=1, max_candidates=128, elapsed_seconds=0, candidates=128), None),
                                 (dict(seconds=1, max_candidates=128, elapsed_seconds=0, candidates=0), perf_counter()-1)):
            pl, c, n, i, m = example()
            with patch(MODULE+'_static_safe') as check:
                trial, out = refine_region_controls(par, pl, c, n, i, m, budget=budget, deadline=deadline)
            check.assert_not_called()
            self.assertIs(trial, pl)
            self.assertEqual(out['layout_regions']['control_refinement']['candidates'], 0)

    def test_translation_mirror_and_renaming_preserve_evidence_and_improvement(self):
        results = []
        for transform in ('none', 'translate', 'mirror', 'rename'):
            pl, c, n, i, m = example()
            if transform == 'translate':
                pl.column_x = [x+200 for x in pl.column_x]
                pl.row_y = [y-100 for y in pl.row_y]
            elif transform == 'mirror':
                pl.set_state([(r, len(pl.column_x)-1-col) for r, col in pl.cell_of], pl.orient)
                for j in range(len(c)):
                    pl.orient[j] = 1-pl.orient[j] if c[j]['symbol'] in ('nmos', 'pmos') else pl.orient[j]
            elif transform == 'rename':
                names = {name: 'component_'+str(90-k) for k, name in enumerate(i)}
                wires = {net: 'wire_'+str(90-k) for k, net in enumerate(n)}
                for comp in c:
                    comp['id'] = names[comp['id']]
                    comp['nodes'] = {p: wires[net] for p, net in comp['nodes'].items()}
                n = {wires[net]: [(names[name], pin) for name, pin in reversed(ports)] for net, ports in reversed(list(n.items()))}
                i = {comp['id']: j for j, comp in enumerate(c)}
                plan = m['layout_regions']['plan']
                plan['ownership'] = {names[name]: owner for name, owner in plan['ownership'].items()}
                plan['power_interfaces'] = [wires.get(net, net) for net in plan['power_interfaces']]
                for region in plan['regions']:
                    region['members'] = [names[name] for name in region['members']]
                    region['pairs'] = [[names[name] for name in pair] for pair in region.get('pairs', ())]
            trial, out = refine_region_controls(par, pl, c, n, i, m)
            report = out['layout_regions']['control_refinement']
            self.assertTrue(report['applied'], transform)
            results.append(report['after']['endpoint_distance'])
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[3])

    def test_second_pass_is_stable_and_reuses_consumed_quota(self):
        pl, c, n, i, m = example()
        first, meta = refine_region_controls(par, pl, c, n, i, m)
        second, out = refine_region_controls(par, first, c, n, i, meta)
        self.assertEqual(state(first), state(second))
        self.assertFalse(out['layout_regions']['control_refinement']['applied'])
        self.assertGreaterEqual(out['layout_regions']['local_budget']['candidates'],
                                meta['layout_regions']['local_budget']['candidates'])

    def test_duplicate_pair_evidence_does_not_repeat_a_transaction(self):
        pl, c, n, i, m = example()
        first, original = refine_region_controls(par, pl, c, n, i, m)
        region = m['layout_regions']['plan']['regions'][0]
        region['pairs'] += [['right', 'left'], ['left', 'right']]
        second, duplicate = refine_region_controls(par, pl, c, n, i, m)
        self.assertEqual(state(first), state(second))
        for field in ('candidates', 'actions'):
            self.assertEqual(original['layout_regions']['control_refinement'][field],
                             duplicate['layout_regions']['control_refinement'][field])

    def test_bjt_and_polarity_dual_keep_real_conduction_terminals(self):
        for symbol in ('npn', 'pmos'):
            pl, c, n, i, m = example()
            for name in ('left', 'right'):
                comp = c[i[name]]
                comp['symbol'] = symbol
                if symbol == 'npn':
                    mapping = {'D': 'C', 'G': 'B', 'S': 'E'}
                    comp['nodes'] = {mapping.get(p, p): net for p, net in comp['nodes'].items()}
                    n = {net: [(host, mapping.get(pin, pin) if host == name else pin)
                               for host, pin in ports] for net, ports in n.items()}
            before = {name: [pl.pin_world(i[name], p) for p in (('C', 'E') if symbol == 'npn' else ('D', 'S'))]
                      for name in ('left', 'right')}
            trial, out = refine_region_controls(par, pl, c, n, i, m)
            self.assertTrue(out['layout_regions']['control_refinement']['applied'], symbol)
            for name, points in before.items():
                for pin, point in zip(('C', 'E') if symbol == 'npn' else ('D', 'S'), points):
                    self.assertEqual(trial.pin_world(i[name], pin), point)
            self.assertTrue(_static_safe(par, trial, n, i, RoutingOptions(), None))

    def test_reversed_passive_pin_order_uses_geometry_rotation_and_direct_host_access(self):
        for reverse in (False, True):
            pl, c, n, i, m = example()
            if reverse:
                for comp in c:
                    if comp['symbol'] in ('resistor', 'capacitor', 'inductor'):
                        comp['nodes'] = {'1': comp['nodes']['2'], '2': comp['nodes']['1']}
                n = {}
                for comp in c:
                    for pin, net in comp['nodes'].items():
                        n.setdefault(net, []).append((comp['id'], pin))
            original_nodes = deepcopy([comp['nodes'] for comp in c])
            trial, out = refine_region_controls(par, pl, c, n, i, m)
            report = out['layout_regions']['control_refinement']
            self.assertTrue(report['applied'])
            self.assertEqual(report['after']['host_entry_reversals'], 0)
            self.assertEqual([comp['nodes'] for comp in c], original_nodes)
            self.assertTrue(trial.valid_state())
            if reverse:
                self.assertTrue(any(trial.orient[i[name]] in (2, 3) for name in ('r0', 'c0', 'f0')))
            scene = build_scene(par, n, trial, i, RoutingOptions())
            for group in report['model']['groups']:
                member = group['returned']['members'][0]
                pins = scene.pins[group['net']]
                host = next(p for p in pins if p.component == i[group['host']])
                resistor = next(p for p in pins if p.component == i[member])
                self.assertEqual(host.escape[1], resistor.escape[1])
                self.assertTrue(scene.edge_check(group['net'], host.escape, resistor.escape, {})[0])

    def test_extra_rotations_are_opt_in_and_cannot_rotate_active_or_source_devices(self):
        pl, c, n, i, m = example()
        self.assertEqual(pl.allowed_orientations(i['r0']), [0, 1])
        pl.terminal_orientation_ids = {i['r0'], i['left']}
        self.assertEqual(pl.allowed_orientations(i['r0']), [0, 1, 2, 3])
        self.assertEqual(pl.allowed_orientations(i['left']), [0, 1])
