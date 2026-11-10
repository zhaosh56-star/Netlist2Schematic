"""Choose one bounded simplification budget from already validated routes.

This is a deterministic allocation heuristic, not a prediction of elapsed CPU
time. It never runs placement or routing and never changes candidate limits.
"""
from __future__ import annotations

from copy import deepcopy
from math import fsum, isfinite
from time import perf_counter

from orthogonal_router import RoutingFailure
from routing_rules import EPS, canonical_topology, check_deadline

ESTIMATOR_VERSION = 'route-complexity-v1'
AUTO_CONFIG = {
    'min_seconds': 5, 'max_seconds': 30, 'levels': [5, 10, 15, 20, 30],
    'estimation_limit_seconds': 0.5,
    'normalizers': {'foreign_run_pairs': 2000, 'device_segment_checks': 2000,
                    'segments': 80, 'bends': 40, 'junctions': 15,
                    'multi_terminal_nets': 8, 'long_detour_chains': 4},
    'detour_fraction_weight': 2.0, 'long_detour_fraction': 0.25,
}


def validate_simplify_seconds(value):
    if type(value) is str and value == 'auto':
        return
    if type(value) not in (int, float) or not isfinite(value) or value < 0:
        raise ValueError('simplify_seconds must be auto or a finite number >= 0')


def parse_simplify_seconds(value):
    if value == 'auto':
        return value
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        raise ValueError('expected auto or a finite number >= 0') from None
    validate_simplify_seconds(numeric)
    return numeric


def _length(a, b):
    return abs(a[0]-b[0]) + abs(a[1]-b[1])


def _chain_opportunities(topology, deadline):
    adjacency = topology['adjacency']
    stops = topology['pin_points'] | {point for point, neighbors in adjacency.items()
                                      if len(neighbors) != 2}
    if not stops and adjacency:
        stops = {min(adjacency)}
    visited, excesses, long_count = set(), [], 0
    for start in sorted(stops & set(adjacency)):
        check_deadline(deadline, 'budget_chain_features')
        for neighbor in sorted(adjacency[start]):
            edge = tuple(sorted((start, neighbor)))
            if edge in visited:
                continue
            visited.add(edge)
            lengths = [_length(start, neighbor)]
            previous, current = start, neighbor
            while current not in stops:
                check_deadline(deadline, 'budget_chain_features')
                following = next(iter(adjacency[current]-{previous}))
                edge = tuple(sorted((current, following)))
                if edge in visited:
                    break
                visited.add(edge)
                lengths.append(_length(current, following))
                previous, current = current, following
            length = fsum(lengths)
            span = _length(start, current)
            excess = max(0.0, length-span)
            excesses.append(excess)
            if excess > EPS and excess >= AUTO_CONFIG['long_detour_fraction']*max(span, EPS):
                long_count += 1
    return fsum(excesses), long_count


def extract_features(routed, pin_points, device_count, *, deadline=None):
    """Ignore segment cuts, orientation, names and absolute canvas extent."""
    if type(device_count) is not int or device_count < 0:
        raise ValueError('device_count must be a non-negative integer')
    topologies = {net: canonical_topology(segments, pin_points.get(net, ()), deadline=deadline)
                  for net, segments in routed}
    runs = [len(topology['runs']) for topology in topologies.values()]
    segments = sum(len(topology['segments']) for topology in topologies.values())
    wirelengths, excesses = [], []
    bends = junctions = multi_terminal = long_chains = 0
    for topology in topologies.values():
        check_deadline(deadline, 'budget_features')
        wirelengths.extend(_length(a, b) for a, b in topology['runs'])
        multi_terminal += len(topology['pin_points']) >= 3
        for point, neighbors in topology['adjacency'].items():
            if len(neighbors) > 2:
                junctions += 1
            elif len(neighbors) == 2:
                a, b = tuple(neighbors)
                bends += (a[0] == point[0]) != (b[0] == point[0])
        excess, count = _chain_opportunities(topology, deadline)
        excesses.append(excess)
        long_chains += count
    wirelength, excess = fsum(wirelengths), fsum(excesses)
    return {'device_count': device_count, 'signal_nets': len(topologies),
            'runs': sum(runs), 'segments': segments,
            'foreign_run_pairs': (sum(runs)**2-sum(number**2 for number in runs))//2,
            'device_segment_checks': device_count*segments,
            'bends': bends, 'junctions': junctions,
            'multi_terminal_nets': multi_terminal, 'long_detour_chains': long_chains,
            'wirelength': wirelength, 'chain_excess': excess,
            'detour_fraction': excess/wirelength if wirelength > EPS else 0.0}


def select_budget(features):
    scores = {name: features.get(name, 0)/scale
              for name, scale in AUTO_CONFIG['normalizers'].items()}
    scores['detour_fraction'] = (features.get('detour_fraction', 0)
                                  * AUTO_CONFIG['detour_fraction_weight'])
    opportunity = any(features.get(name, 0) for name in
                      ('bends', 'junctions', 'multi_terminal_nets'))
    work_score = max(1.0, *scores.values()) if opportunity else 1.0
    hint = AUTO_CONFIG['min_seconds']*work_score
    selected = next((level for level in AUTO_CONFIG['levels'] if hint <= level+EPS),
                    AUTO_CONFIG['max_seconds'])
    return {'mode': 'auto', 'estimator_version': ESTIMATOR_VERSION,
            'config': deepcopy(AUTO_CONFIG), 'selected_seconds': selected,
            'features': deepcopy(features), 'scores': scores,
            'budget_hint_seconds': hint, 'feature_status': 'complete',
            'reasons': ([name for name, score in scores.items() if score > 1]
                        if opportunity else ['no_bends_or_branch_opportunities'])}


def estimate_simplify_budget(placement, comps, nets, comp_index, routed):
    started = perf_counter()
    deadline = started+AUTO_CONFIG['estimation_limit_seconds']
    try:
        points = {}
        for net, _ in routed:
            check_deadline(deadline, 'budget_pin_features')
            points[net] = [tuple(placement.pin_world(comp_index[cid], pin))
                           for cid, pin in nets.get(net, ())]
        features = extract_features(routed, points, len(comps), deadline=deadline)
        result = select_budget(features)
    except RoutingFailure as exc:
        if exc.diagnostics.get('reason') != 'quality_time_budget':
            raise
        # The input has already passed full validation. Expensive feature
        # extraction gets a bounded conservative allocation, never a fake score.
        result = {'mode': 'auto', 'estimator_version': ESTIMATOR_VERSION,
                  'config': deepcopy(AUTO_CONFIG),
                  'selected_seconds': AUTO_CONFIG['max_seconds'],
                  'features': None, 'scores': {}, 'feature_status': 'time_budget',
                  'reasons': ['feature_extraction_budget_exhausted']}
    result['estimation_seconds'] = round(perf_counter()-started, 6)
    return result
