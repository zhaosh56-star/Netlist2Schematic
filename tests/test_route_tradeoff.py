"""Optional sparse crossings never weaken connectivity or geometric checks."""
import random
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import route_tradeoff as policy
import simplify_routes as shortcuts
from orthogonal_router import RoutingFailure
from routing_rules import Pin, RoutingOptions, Scene, canonical_topology, intersection
from sa_routing import _verify
from tests.test_simplify_routes import case, run_case


def metrics(length=1000, crossings=0, bends=4):
    return {'wirelength': length, 'crossings': crossings, 'bends': bends}


def evaluate(before=None, after=None, **kwargs):
    values = dict(initial_crossings=0,
                  proposal={'old_chain_length': 1000, 'new_chain_length': 600},
                  pitch=100, before_crossings=frozenset(), after_crossings=frozenset(),
                  cross_clearance=12)
    values.update(kwargs)
    return policy.evaluate(before or metrics(), after or metrics(600, 1), **values)


def crossing_case():
    """A 900-unit arch can become a 500-unit straight wire crossing one net."""
    positions = [(0, 0), (500, 0), (250, -300), (250, 100)]
    pins = {'a': [Pin('a', 0, 'P', positions[0], (20, 0), (1, 0)),
                  Pin('a', 1, 'P', positions[1], (480, 0), (-1, 0))],
            'b': [Pin('b', i, 'P', positions[i], positions[i], (0, 1)) for i in (2, 3)]}
    routes = [('a', [((0, 0), (20, 0)), ((20, 0), (20, 200)),
                     ((20, 200), (480, 200)), ((480, 200), (480, 0)),
                     ((480, 0), (500, 0))]), ('b', [(positions[2], positions[3])])]
    scene = Scene(options=RoutingOptions(), pins=pins,
                  all_pins={net: [p.point for p in ps] for net, ps in pins.items()},
                  fixed_segments={net: [p.stub for p in ps if p.point != p.escape]
                                  for net, ps in pins.items()})
    placement = SimpleNamespace(comps=[], column_x=[0, 100], row_y=[0, 100],
                                pin_world=lambda i, pin: positions[i])
    nets, index = {'a': [('A', 'P'), ('B', 'P')], 'b': [('C', 'P'), ('D', 'P')]}, dict(A=0, B=1, C=2, D=3)

    def crosses(a, b, c, d):
        point = intersection(a, b, c, d)
        return point is not None and point not in (a, b, c, d)

    par = SimpleNamespace(CELL=150, power_marker_sym=lambda _: None, GEO_DOC={}, MARGIN=12,
                          seg_cross=crosses, seg_overlap_len=lambda *a: 0,
                          seg_parallel_prox=lambda *a, **k: 0, seg_hits_rect=lambda *a: False)
    checked, measured, _ = _verify(par, nets, placement, index, routes, scene)
    return par, nets, placement, index, (checked, measured, {'valid': True}), scene


def straight_candidate(scene, routed, deadline):
    yield {'net': 'a', 'segments': [((0, 0), (500, 0))],
           'old_chain_length': 860, 'new_chain_length': 460}


class TradeoffPolicyTests(unittest.TestCase):
    def test_pitch_uses_all_positive_physical_gaps_and_legacy_grid(self):
        par = SimpleNamespace(CELL=150)
        self.assertEqual(110, policy.physical_pitch(par,
            SimpleNamespace(column_x=[64, 174, 284], row_y=[64, 176])))
        self.assertEqual(150, policy.physical_pitch(par, SimpleNamespace()))

    def test_weighted_cost_matches_existing_route_cost_units(self):
        self.assertEqual(1375, policy.cost(metrics(1000, 2, 5)))

    def test_two_extra_crossings_are_relative_to_immutable_baseline(self):
        first = evaluate(metrics(3000, 2), metrics(2400, 4), initial_crossings=2)
        second = evaluate(metrics(2400, 4), metrics(1800, 5), initial_crossings=2)
        self.assertTrue(first['accepted'])
        self.assertEqual('crossing_allowance', second['reason'])
        self.assertEqual(3, second['cumulative_crossing_increase'])

    def test_crossing_removal_does_not_reset_reference(self):
        result = evaluate(metrics(3000, 1), metrics(2300, 4), initial_crossings=2)
        self.assertTrue(result['accepted'])
        self.assertEqual(2, result['cumulative_crossing_increase'])
        self.assertEqual(450, result['minimum_saved_length'])

    def test_twenty_percent_reduction_boundary_and_branch_evidence(self):
        for new, accepted in ((800, True), (801, False)):
            with self.subTest(new=new):
                value = evaluate(metrics(1000), metrics(800, 1),
                    proposal={'old_branch_length': 1000, 'new_branch_length': new})
                self.assertEqual(accepted, value['accepted'])
                if not accepted:
                    self.assertEqual('path_reduction', value['reason'])

    def test_actual_saving_must_reach_two_pitches_and_per_crossing_cost(self):
        self.assertEqual('minimum_length_saving', evaluate(
            metrics(1000), metrics(801, 1))['reason'])
        self.assertTrue(evaluate(metrics(1000), metrics(800, 1))['accepted'])
        self.assertEqual('minimum_length_saving', evaluate(
            metrics(1000), metrics(701, 2))['reason'])

    def test_weighted_cost_must_strictly_drop_at_equal_saving(self):
        self.assertEqual('weighted_cost', evaluate(metrics(1000), metrics(850, 1), pitch=50)['reason'])
        self.assertTrue(evaluate(metrics(1000, 0, 4), metrics(850, 1, 3), pitch=50)['accepted'])

    def test_no_extra_bends_or_length_and_no_noop(self):
        for after in (metrics(600, 1, 5), metrics(1001, 0, 3), metrics()):
            with self.subTest(after=after):
                self.assertEqual('shape_non_improvement', evaluate(after=after)['reason'])

    def test_no_crossing_increase_keeps_small_existing_gains(self):
        result = evaluate(after=metrics(999), proposal={})
        self.assertTrue(result['accepted'])
        self.assertNotIn('minimum_saved_length', result)

    def test_crossing_gain_without_free_path_evidence_is_not_promoted(self):
        for proposal in ({}, {'old_chain_length': 0, 'new_chain_length': 0},
                         {'old_chain_length': float('nan'), 'new_chain_length': 3}):
            self.assertEqual('missing_path_evidence', evaluate(proposal=proposal)['reason'])

    def test_density_uses_chebyshev_distance_and_includes_existing_crossings(self):
        origin = ('a', 'b', (0, 0))
        for location, accepted in (((23, 23), False), ((23, 24), True), ((0, 24), True)):
            with self.subTest(location=location):
                other = ('c', 'd', location)
                result = evaluate(metrics(1000, 1), metrics(600, 2), initial_crossings=1,
                                  before_crossings=frozenset({other}),
                                  after_crossings=frozenset({origin, other}))
                self.assertEqual(accepted, result['accepted'])

    def test_moved_crossing_is_checked_even_without_a_count_increase(self):
        first, other = ('a', 'b', (0, 100)), ('c', 'd', (0, 20))
        shifted = ('a', 'b', (0, 0))
        result = evaluate(metrics(1000, 2), metrics(999, 2), initial_crossings=2,
                          before_crossings=frozenset({first, other}),
                          after_crossings=frozenset({shifted, other}))
        self.assertEqual('crossing_density', result['reason'])
        self.assertEqual(1, len(result['density']['new_or_moved_crossings']))

    def test_existing_crowded_crossings_are_not_retroactively_invalidated(self):
        existing = frozenset({('a', 'b', (0, 0)), ('c', 'd', (0, 20))})
        self.assertTrue(evaluate(metrics(1000, 2), metrics(999, 2), initial_crossings=2,
            before_crossings=existing, after_crossings=existing)['accepted'])

    def test_custom_clearance_controls_density(self):
        result = evaluate(before_crossings=frozenset(),
            after_crossings=frozenset({('a', 'b', (0, 0)), ('a', 'c', (30, 0))}),
            cross_clearance=20)
        self.assertEqual('crossing_density', result['reason'])
        self.assertEqual(40, result['density']['required_distance'])

    def test_crossing_identity_ignores_splits_and_reversed_segments(self):
        unsplit = {'a': [((0, 0), (100, 0))], 'b': [((50, -50), (50, 50))]}
        split = {'a': [((50, 0), (0, 0)), ((100, 0), (50, 0))], 'b': unsplit['b']}
        inventories = [policy.crossing_identities({n: canonical_topology(s) for n, s in r.items()})
                       for r in (unsplit, split)]
        self.assertEqual(inventories[0], inventories[1])
        self.assertEqual(frozenset({('a', 'b', (50, 0))}), inventories[0])


class TradeoffIntegrationTests(unittest.TestCase):
    def test_strict_is_default_and_preserves_rng_without_new_crossing_scan(self):
        fixture = case()
        state = random.getstate()
        with patch('simplify_routes.crossing_identities', side_effect=AssertionError('strict scan')):
            default = run_case(fixture)
            explicit = run_case(fixture, tradeoff='strict')
        self.assertEqual(default[:3], explicit[:3])
        self.assertEqual(default[3]['accepted_changes'], explicit[3]['accepted_changes'])
        self.assertEqual(state, random.getstate())

    def test_real_geometry_accepts_large_sparse_crossing_gain_only_when_opted_in(self):
        fixture = crossing_case()
        with patch('simplify_routes._shortcut_candidates', side_effect=straight_candidate), \
                patch('simplify_routes.reattachment_candidates', return_value=iter(())), \
                patch('simplify_routes._three_segment_candidates', return_value=iter(())), \
                patch('simplify_routes._track_compaction_candidates', return_value=iter(())), \
                patch('orthogonal_router._find_path', side_effect=AssertionError('A* forbidden')):
            strict = run_case(fixture, tradeoff='strict')
            balanced = run_case(fixture, tradeoff='balanced')
        self.assertEqual(fixture[4][1], strict[1])
        self.assertEqual(1, balanced[1]['crossings'])
        self.assertEqual(400, strict[1]['wirelength']-balanced[1]['wirelength'])
        self.assertTrue(balanced[2]['valid'])
        _verify(*fixture[:4], balanced[0], fixture[5])
        report = balanced[3]['tradeoff']
        self.assertEqual('balanced', report['policy'])
        self.assertEqual(1, report['net_crossing_increase'])
        self.assertEqual(1, report['remaining_crossings'])
        self.assertEqual(1, report['accepted_with_extra_crossings'])
        self.assertEqual(1, len(report['candidates'][0]['density']['new_or_moved_crossings']))

    def test_balanced_does_not_accept_geometry_rejected_contact(self):
        fixture = crossing_case()
        # Cross exactly at the lower foreign endpoint, an illegal T contact.
        bad = {'net': 'a', 'segments': [((0, 0), (20, 0)), ((20, 0), (20, 100)),
            ((20, 100), (480, 100)), ((480, 100), (480, 0)), ((480, 0), (500, 0))],
            'old_chain_length': 860, 'new_chain_length': 660}
        with patch('simplify_routes._shortcut_candidates', return_value=iter([bad])), \
                patch('simplify_routes.reattachment_candidates', return_value=iter(())), \
                patch('simplify_routes._three_segment_candidates', return_value=iter(())), \
                patch('simplify_routes._track_compaction_candidates', return_value=iter(())):
            result = run_case(fixture, tradeoff='balanced')
        self.assertEqual(fixture[4], result[:3])
        self.assertGreater(result[3]['candidate_counts']['geometry_rejected'], 0)
        self.assertEqual(0, result[3]['tradeoff']['assessed'])

    def test_tradeoff_timeout_discards_fully_routed_but_unfinished_trial(self):
        fixture = crossing_case()
        with patch('simplify_routes._shortcut_candidates', side_effect=straight_candidate), \
                patch('simplify_routes.evaluate_tradeoff', side_effect=RoutingFailure(
                    'expired', {'reason': 'quality_time_budget', 'stage': 'tradeoff'})):
            result = run_case(fixture, tradeoff='balanced')
        self.assertEqual(fixture[4], result[:3])
        self.assertEqual(0, result[3]['tradeoff']['accepted'])
        self.assertEqual(0, result[3]['candidate_counts']['accepted'])
        self.assertTrue(result[2]['valid'])

    def test_zero_budget_and_invalid_policy(self):
        fixture = case()
        with patch('simplify_routes.build_scene', side_effect=AssertionError('disabled')):
            result = run_case(fixture, tradeoff='balanced', seconds=0)
        self.assertEqual(fixture[4], result[:3])
        self.assertEqual(2, result[3]['tradeoff']['remaining_crossings'])
        self.assertFalse(result[3]['tradeoff']['baseline_revalidated'])
        with self.assertRaisesRegex(ValueError, 'tradeoff'):
            run_case(fixture, tradeoff='loose')


if __name__ == '__main__':
    unittest.main()
