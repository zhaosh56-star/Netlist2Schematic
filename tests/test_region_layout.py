"""Terminal-role regions, conservative ambiguity and fair result selection."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from netlist2schematic.plugins.region.region_layout import (build_region_plan, measure_regions, region_rank,
                           select_region_result)


def mos(d, g, s, polarity='NMOS', bulk=None):
    return dict(cls='MOS', type=polarity, model=polarity, d=d, g=g, s=s,
                bulk=('lo' if polarity == 'NMOS' else 'hi') if bulk is None else bulk)


def comparator():
    return dict(ina=mos('x0', 'input0', 'tail'), inb=mos('x1', 'input1', 'tail'),
        tail=mos('tail', 'clock', 'lo'),
        la=mos('y0', 'y1', 'x0'), lb=mos('y1', 'y0', 'x1'),
        pa=mos('y0', 'y1', 'hi', 'PMOS'), pb=mos('y1', 'y0', 'hi', 'PMOS'),
        rxa=mos('x0', 'clock', 'hi', 'PMOS'), rxb=mos('x1', 'clock', 'hi', 'PMOS'),
        rya=mos('y0', 'clock', 'hi', 'PMOS'), ryb=mos('y1', 'clock', 'hi', 'PMOS'))


def ota(feedback=True):
    result = dict(feed=mos('source', 'bias', 'hi', 'PMOS'),
        ia=mos('x0', 'input0', 'source', 'PMOS', 'source'),
        ib=mos('x1', 'input1', 'source', 'PMOS', 'source'),
        ta=mos('u0', 'bias', 'hi', 'PMOS'), tb=mos('u1', 'bias', 'hi', 'PMOS'),
        ua=mos('y0', 'upperbias', 'u0', 'PMOS'), ub=mos('y1', 'upperbias', 'u1', 'PMOS'),
        qa=mos('y0', 'lowerbias', 'x0'), qb=mos('y1', 'lowerbias', 'x1'),
        sa=mos('x0', 'sinkbias', 'lo'), sb=mos('x1', 'sinkbias', 'lo'))
    if feedback:
        result.update(aa=mos('x0', 'return', 'lo'), ab=mos('x1', 'return', 'lo'),
            fa=mos('csource0', 'bias', 'hi', 'PMOS'), fb=mos('csource1', 'bias', 'hi', 'PMOS'),
            ca=mos('sense', 'y0', 'csource0', 'PMOS'),
            ra=mos('return', 'reference', 'csource0', 'PMOS'),
            cb=mos('sense', 'y1', 'csource1', 'PMOS'),
            rb=mos('return', 'reference', 'csource1', 'PMOS'),
            ds=mos('sense', 'sense', 'lo'), dr=mos('return', 'return', 'lo'))
    return result


def renamed(devices, dual=False):
    names = {name: 'device_' + str(100 - number) for number, name in enumerate(devices)}
    nets = sorted({dev[key] for dev in devices.values() for key in ('d', 'g', 's', 'bulk')})
    net_map = {name: 'wire_' + str(200 - number) for number, name in enumerate(nets)}
    result = {}
    for name in reversed(list(devices)):
        row = deepcopy(devices[name])
        for key in ('d', 'g', 's', 'bulk'):
            row[key] = net_map[row[key]]
        if dual:
            row['type'] = 'PMOS' if row['type'] == 'NMOS' else 'NMOS'
            row['model'] = row['type']
        result[names[name]] = row
    return result, {new: old for old, new in names.items()}


def semantic(plan, inverse=None):
    inverse = inverse or {}
    name = lambda value: inverse.get(value, value)
    leaves = sorted((row['kind'], tuple(sorted(name(n) for n in row['members'])))
                    for row in plan['regions'] if not row['children'])
    axes = sorted(tuple(sorted(((name(row['a']), row['pin_a']),
                                (name(row['b']), row['pin_b'])))) for row in plan['series_axes'])
    return leaves, axes


class Position:
    def __init__(self, names, points, pin_offsets=None, mirrored=False):
        self.names, self.points = list(names), deepcopy(points)
        self.pin_offsets = pin_offsets or {}
        self.sign = -1 if mirrored else 1
        self.cell_of = [(int(points[name][1] / 100), int(points[name][0] / 100)) for name in names]
        self.orient = [int(mirrored)] * len(names)

    def pos_of(self, index):
        x, y = self.points[self.names[index]]
        return self.sign * x, y

    def pin_world(self, index, pin):
        name = self.names[index]
        x, y = self.points[name]
        dx, dy = self.pin_offsets.get((name, pin), (0, 0))
        return self.sign * (x + dx), y + dy


def positions_for(devices):
    points = {name: (100 * number, 0) for number, name in enumerate(devices)}
    return points


def measured(metrics=None, *, interleaving=2, order=1, pin=3, dispersion=4,
             signature='same-source', supported=True):
    row = dict(supported=supported, measurable=supported, plan_signature=signature,
               interleaving=interleaving, order_errors=order, pin_axis_error=pin,
               axis_drift=0, dispersion=dispersion, junction_error=0,
               coverage=dict(recognized_devices=11, measured_axes=4))
    row.update(metrics or {})
    return row


def result(identifier, score, metrics, length=100, crossings=0):
    return ((score, crossings, length), None, None, None, None,
            {'layout_regions': {'metrics': metrics}},
            ([], {'wirelength': length, 'crossings': crossings}, {'valid': True}),
            {'score': score}, str(identifier))


class RegionRecognitionTests(unittest.TestCase):
    def test_comparator_has_disjoint_leaves_and_real_fork_axes(self):
        devices = comparator()
        frozen = deepcopy(devices)
        plan = build_region_plan(devices)
        self.assertTrue(plan['supported'])
        self.assertEqual(plan['coverage']['recognized_devices'], 11)
        leaves = [row for row in plan['regions'] if not row['children']]
        self.assertEqual(sorted(len(row['members']) for row in leaves), [2, 2, 7])
        self.assertEqual(len(plan['ownership']), sum(len(row['members']) for row in leaves))
        edges = {frozenset((row['a'], row['b'])) for row in plan['series_axes']}
        self.assertIn(frozenset(('ina', 'la')), edges)
        self.assertNotIn(frozenset(('ina', 'rxa')), edges)
        self.assertEqual(devices, frozen)
        json.dumps(plan, allow_nan=False)

    def test_ota_has_separate_fold_axes_detector_and_shared_source_junctions(self):
        devices = ota()
        plan = build_region_plan(devices)
        self.assertTrue(plan['supported'])
        self.assertEqual(plan['coverage']['recognized_devices'], 21)
        leaves = {row['kind']: set(row['members']) for row in plan['regions'] if not row['children']}
        self.assertEqual(leaves['folded_input'], {'feed', 'ia', 'ib', 'aa', 'ab'})
        self.assertEqual(leaves['feedback_detector'], {'fa', 'fb', 'ca', 'cb', 'ra', 'rb', 'ds', 'dr'})
        for axis in plan['series_axes']:
            self.assertEqual(devices[axis['a']][axis['pin_a'].lower()], axis['net'])
            self.assertEqual(devices[axis['b']][axis['pin_b'].lower()], axis['net'])
            self.assertNotIn(frozenset((axis['a'], axis['b'])),
                             (frozenset(('ia', 'qa')), frozenset(('ib', 'qb')),
                              frozenset(('fa', 'ca')), frozenset(('fa', 'ra'))))
        self.assertEqual(sum(row['kind'] == 'shared_feed' for row in plan['junctions']), 3)
        self.assertTrue(any(row['axis_mode'] == 'interface' for row in plan['correspondences']))

    def test_folded_core_without_detector_remains_supported(self):
        plan = build_region_plan(ota(False))
        self.assertTrue(plan['supported'])
        self.assertEqual(plan['coverage']['recognized_devices'], 11)
        self.assertFalse(any(row['kind'] == 'feedback_detector' for row in plan['regions']))

    def test_all_names_nets_order_and_polarity_are_irrelevant(self):
        for factory in (comparator, ota):
            original = factory()
            expected = semantic(build_region_plan(original))
            for dual in (False, True):
                with self.subTest(factory=factory.__name__, dual=dual):
                    devices, inverse = renamed(original, dual)
                    self.assertEqual(expected, semantic(build_region_plan(devices), inverse))

    def test_common_clock_and_bias_do_not_union_external_devices(self):
        for factory, control in ((comparator, 'clock'), (ota, 'bias')):
            devices = factory()
            devices['other'] = mos('outside', control, 'hi', 'PMOS')
            plan = build_region_plan(devices)
            self.assertTrue(plan['supported'])
            self.assertNotIn('other', plan['ownership'])
            self.assertEqual(plan['coverage']['unassigned_active'], ['other'])

    def test_duplicate_role_matching_is_ambiguous_and_not_selected_by_name(self):
        devices = comparator()
        devices['la_duplicate'] = deepcopy(devices['la'])
        devices['lb_duplicate'] = deepcopy(devices['lb'])
        plan = build_region_plan(devices)
        self.assertFalse(plan['supported'])
        self.assertTrue(any(row['reason'] == 'nonunique_role_matching' for row in plan['ambiguities']))
        self.assertEqual(plan['coverage']['recognized_devices'], 0)

    def test_duplicate_clock_tap_is_not_hidden_as_complete_coverage(self):
        devices = comparator()
        devices['extra'] = deepcopy(devices['rxa'])
        plan = build_region_plan(devices)
        self.assertFalse(plan['supported'])
        self.assertTrue(plan['ambiguities'])

    def test_conflicting_regions_both_rejected_without_first_owner(self):
        devices = comparator()
        second, inverse = renamed(comparator())
        mapping = {old: new for new, old in inverse.items()}
        # Feed two otherwise complete comparison cores from one shared tail.
        shared_source = devices['ina']['s']
        second_source = second[mapping['ina']]['s']
        second_tail = mapping['tail']
        for row in second.values():
            for field in ('d', 's'):
                if row[field] == second_source:
                    row[field] = shared_source
        second_clock = second[second_tail]['g']
        for row in second.values():
            if row['g'] == second_clock:
                row['g'] = devices['tail']['g']
        del second[second_tail]
        devices.update(second)
        # Explicit calibrated seeds exercise owner conflict independently of
        # the calibrator's own conservative four-input source-bank rejection.
        evidence = {'relations': [dict(kind='differential_pair', members=['ina', 'inb']),
            dict(kind='differential_pair', members=[mapping['ina'], mapping['inb']])]}
        plan = build_region_plan(devices, evidence=evidence)
        self.assertFalse(plan['supported'])
        self.assertTrue(any(row['reason'] == 'overlapping_region_candidates' for row in plan['ambiguities']))

    def test_rejected_evidence_cannot_be_reintroduced_by_old_relations(self):
        plan = build_region_plan(comparator(), evidence={'relations': []},
                                 relations=[dict(kind='differential_pair', members=['ina', 'inb'])])
        self.assertFalse(plan['supported'])

    def test_differential_load_or_unfed_clock_bank_is_not_a_region(self):
        simple = dict(a=mos('x', 'p', 's'), b=mos('y', 'm', 's'),
                      tail=mos('s', 'bias', 'lo'),
                      pa=mos('x', 'load', 'hi', 'PMOS'), pb=mos('y', 'load', 'hi', 'PMOS'))
        clock_bank = dict(a=mos('x', 'clock', 'hi', 'PMOS'),
                          b=mos('y', 'clock', 'hi', 'PMOS'), tail=mos('z', 'clock', 'lo'))
        for devices in (simple, clock_bank):
            self.assertFalse(build_region_plan(devices)['supported'])

    def test_broken_detector_reference_or_return_does_not_form_detector(self):
        for name, field, value in (('rb', 'g', 'other_reference'), ('ab', 'g', 'other_return')):
            devices = ota()
            devices[name][field] = value
            plan = build_region_plan(devices)
            self.assertFalse(any(row['kind'] == 'feedback_detector' for row in plan['regions']))

    def test_signature_keeps_active_rows_ignores_passive_rows_and_input_order(self):
        devices = comparator()
        devices['r'] = dict(cls='PASSIVE', type='R', a='x0', b='lo', value='1k')
        first = build_region_plan(devices, display_rows={'ina': 2, 'r': 1})
        reordered = build_region_plan(dict(reversed(list(devices.items()))), display_rows={'r': 8, 'ina': 2})
        changed = build_region_plan(devices, display_rows={'ina': 3, 'r': 1})
        self.assertEqual(first['signature'], reordered['signature'])
        self.assertNotEqual(first['signature'], changed['signature'])

    def test_unsupported_bjt_is_counted_as_uncovered_active_and_in_row_signature(self):
        devices = {'q': dict(cls='BJT', type='NPN', model='NPN', c='out', b='input', e='0')}
        first = build_region_plan(devices, display_rows={'q': 1})
        changed = build_region_plan(devices, display_rows={'q': 2})
        self.assertFalse(first['supported'])
        self.assertEqual(first['coverage']['active_devices'], 1)
        self.assertEqual(first['coverage']['recognized_devices'], 0)
        self.assertEqual(first['coverage']['unassigned_active'], ['q'])
        self.assertNotEqual(first['signature'], changed['signature'])


class RegionMeasurementTests(unittest.TestCase):
    def fixture(self):
        devices = comparator()
        plan = build_region_plan(devices)
        points = dict(ina=(300, 300), inb=(500, 300), la=(300, 200), lb=(500, 200),
                      pa=(300, 100), pb=(500, 100), tail=(400, 400),
                      rxa=(100, 100), rya=(200, 100), rxb=(700, 100), ryb=(600, 100))
        index = {name: number for number, name in enumerate(devices)}
        return devices, plan, points, index

    def test_measurement_uses_true_pins_and_leaves_placement_unchanged(self):
        devices, plan, points, index = self.fixture()
        pl = Position(devices, points, {('la', 'S'): (25, 0)})
        before = deepcopy(vars(pl))
        metrics = measure_regions(pl, [], index, plan)
        self.assertTrue(metrics['measurable'])
        weight = next(axis['weight'] for axis in plan['series_axes']
                      if axis['a'] == 'ina' and axis['b'] == 'la')
        self.assertEqual(metrics['pin_axis_error'], .25 * weight)
        self.assertEqual(metrics['interleaving'], 0)
        self.assertEqual(metrics['order_errors'], 0)
        self.assertEqual(vars(pl), before)
        json.dumps(metrics, allow_nan=False)

    def test_global_mirror_invariance_and_one_lane_swap_detection(self):
        devices, plan, points, index = self.fixture()
        ordinary = measure_regions(Position(devices, points), [], index, plan)
        mirrored = measure_regions(Position(devices, points, mirrored=True), [], index, plan)
        self.assertEqual(region_rank(ordinary), region_rank(mirrored))
        points['la'], points['lb'] = points['lb'], points['la']
        swapped = measure_regions(Position(devices, points), [], index, plan)
        self.assertGreater(swapped['order_errors'], ordinary['order_errors'])
        self.assertGreater(swapped['pin_axis_error'], ordinary['pin_axis_error'])

    def test_missing_and_unrecognized_are_unknown_not_zero(self):
        devices, plan, points, index = self.fixture()
        del index['la']
        metrics = measure_regions(Position(devices, points), [], index, plan)
        self.assertFalse(metrics['measurable'])
        self.assertIsNone(metrics['pin_axis_error'])
        self.assertEqual(metrics['coverage']['missing_devices'], ['la'])
        unknown = measure_regions(Position(devices, points), [], index, build_region_plan({}))
        self.assertIsNone(unknown['interleaving'])
        self.assertEqual(region_rank(unknown)[0], float('inf'))

    def test_parent_order_recurses_and_cross_row_reversal_is_counted(self):
        devices = ota()
        plan = build_region_plan(devices)
        regions = {row['kind']: row for row in plan['regions']}
        plan['edges'] = [dict(kind='region_order', regions=[regions['folded_main']['id'],
                                                          regions['feedback_detector']['id']])]
        points = {}
        for region in plan['regions']:
            if not region['children']:
                offset = 1000 if region['kind'] == 'feedback_detector' else 0
                for number, name in enumerate(region['members']):
                    points[name] = (offset + 10 * number, 100 * number)
        index = {name: number for number, name in enumerate(devices)}
        original = measure_regions(Position(devices, points), [], index, plan)
        self.assertTrue(original['measurable'])
        # A detector member jumps to the other side in a distinct row; the
        # global direction cannot be chosen independently for that row.
        points['ca'] = (-1000, 9000)
        changed = measure_regions(Position(devices, points), [], index, plan)
        self.assertGreater(changed['order_errors'], original['order_errors'])
        plan['edges'][0]['regions'].append('absent')
        unknown = measure_regions(Position(devices, points), [], index, plan)
        self.assertFalse(unknown['measurable'])
        self.assertTrue(unknown['coverage']['missing_order_regions'])


class RegionSelectionTests(unittest.TestCase):
    def test_region_gain_within_guard_beats_old_q_but_tie_keeps_old_q(self):
        original = result(1, 1, measured())
        improved = result(2, 3, measured(interleaving=0, order=0), length=115)
        winner, report = select_region_result([original, improved])
        self.assertIs(winner, improved)
        self.assertAlmostEqual(report['wirelength_limit'], 115)
        tied = result(3, 4, measured())
        self.assertIs(select_region_result([original, tied])[0], original)

    def test_wire_and_zero_crossing_guards_are_hard(self):
        original = result(1, 1, measured())
        for length, crossings in ((115.01, 0), (110, 1)):
            candidate = result(2, 2, measured(interleaving=0, order=0), length=length, crossings=crossings)
            self.assertIs(select_region_result([original, candidate])[0], original)
        nonzero = result(1, 1, measured(), crossings=2)
        plus_one = result(2, 2, measured(interleaving=0, order=0), crossings=3)
        plus_two = result(3, 3, measured(interleaving=0, order=0, pin=0), crossings=4)
        self.assertIs(select_region_result([nonzero, plus_one, plus_two])[0], plus_one)

    def test_no_gain_missing_or_different_plan_cannot_replace_old_q(self):
        original = result(1, 1, measured())
        candidates = [result(2, 2, measured(interleaving=0, order=0, signature='other')),
                      result(3, 3, measured(interleaving=0, order=0, supported=False)),
                      result(4, 4, measured(interleaving=0, order=2))]
        self.assertIs(select_region_result([original, *candidates])[0], original)
        unknown = result(1, 1, {})
        self.assertIs(select_region_result([unknown, candidates[0]])[0], unknown)

    def test_empty_result_has_explicit_error(self):
        with self.assertRaises(ValueError):
            select_region_result([])


if __name__ == '__main__':
    unittest.main()
