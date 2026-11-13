"""Bounded layout feedback from a previously verified long wire branch.

This module proposes a new layout for the existing candidate-three router slot.
It never routes a net, changes a geometric rule or inserts work into SA.
"""
from __future__ import annotations

from copy import deepcopy
from math import isfinite
from time import perf_counter

from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.plugins.readability.readability_layout import measure_readability, readability_rank
from netlist2schematic.plugins.readability.readability_refinement import ACTIVE, PASSIVE, _fixed, _sync, _valid
from netlist2schematic.postprocess.route_tradeoff import physical_pitch
from netlist2schematic.routing.routing_rules import (EPS, RoutingOptions, build_scene, check_deadline,
                           canonical_topology)
from netlist2schematic.postprocess.simplify_routes import _chains
from netlist2schematic.plugins.symmetry.symmetry_layout import _static_safe

VERSION = 'readability-route-feedback-v1'
MAX_TRIALS = 128
MAX_PORT_PAIRS = 8
SECONDS_LIMIT = 1.0
INITIALIZER_VERSION = 'readability-initial-escape-v1'
INITIALIZER_TRIALS = 32
INITIALIZER_PAIR_BUDGET = 512
CONTROL_PINS = frozenset(('G', 'B', 'IN+', 'IN-', 'OUT'))


def _terminal_groups(scene, chain, deadline=None):
    """Map a free chain's two local trees to true pin identities, not XY guesses."""
    net, path = chain['net'], chain['path']
    topology = canonical_topology(chain['segments'],
        pins=set(scene.all_pins.get(net, ())) | set(path), deadline=deadline)
    adjacency = {p: set(q) for p, q in topology['adjacency'].items()}
    for a, b in zip(path, path[1:]):
        adjacency[a].discard(b)
        adjacency[b].discard(a)
    groups = []
    visited_groups = []
    for start in (path[0], path[-1]):
        pending, seen = [start], set()
        while pending:
            check_deadline(deadline, 'readability_branch_groups')
            point = pending.pop()
            if point not in seen:
                seen.add(point)
                pending.extend(adjacency.get(point, ())-seen)
        visited_groups.append(seen)
        groups.append(sorted({(pin.component, pin.pin) for pin in scene.pins.get(net, ())
                              if pin.point in seen or pin.escape in seen}))
    if not all(groups) or visited_groups[0].intersection(visited_groups[1]):
        return None
    return groups


def detouring_branches(scene, routed, pitch, deadline=None):
    """Find actual free paths, not high net HPWL ratios that may reflect a tree."""
    result = []
    for chain in _chains(scene, routed, deadline):
        a, b = chain['path'][0], chain['path'][-1]
        span = abs(a[0]-b[0])+abs(a[1]-b[1])
        length = chain['length']
        if span <= EPS or length < 2*span-EPS or length < 3*pitch-EPS or length-span < 2*pitch-EPS:
            continue
        groups = _terminal_groups(scene, chain, deadline)
        if groups is None:
            continue
        result.append({'net': chain['net'], 'path': list(chain['path']),
            'length': length, 'manhattan': span, 'detour_factor': length/span,
            'excess': length-span, 'port_groups': groups})
    return sorted(result, key=lambda row: (-row['excess'], -row['length'], row['path'], str(row['net'])))


def _l_paths(a, b):
    result = set()
    for corner in ((a[0], b[1]), (b[0], a[1])):
        points = [a]
        for p in (corner, b):
            if p != points[-1]:
                points.append(p)
        if len(points) > 1:
            result.add(tuple(points))
    return sorted(result)


def escape_risk(scene, net, port_groups, deadline=None):
    """Inspect two L shapes from real escapes against static bodies and ports.

    Old signal lines are deliberately absent: they no longer describe moved
    devices and will be rebuilt by the original router. Legal signal crossings
    are not counted as static obstructions. This is an estimate, not a proof
    that the future complete tree is routable or that its wirelength falls.
    """
    pins = {(pin.component, pin.pin): pin for pin in scene.pins.get(net, ())}
    pairs = []
    for first in port_groups[0]:
        for second in port_groups[1]:
            a, b = pins.get(tuple(first)), pins.get(tuple(second))
            if a is None or b is None:
                continue
            span = abs(a.escape[0]-b.escape[0])+abs(a.escape[1]-b.escape[1])
            pairs.append((span, tuple(first), tuple(second), a, b))
    records = []
    empty = scene.prepare_occupied({})
    for span, first, second, source, target in sorted(pairs, key=lambda r: r[:3])[:MAX_PORT_PAIRS]:
        for path in _l_paths(source.escape, target.escape):
            check_deadline(deadline, 'readability_escape_risk')
            blockers = set()
            for a, b in zip(path, path[1:]):
                _, _, blocked = scene.edge_check(net, a, b, empty)
                blockers.update(blocked)
            for point in path[1:-1]:
                _, _, blocked = scene.vertex_check(net, point, empty)
                blockers.update(blocked)
            # A fixed exit may be continued or turned; it must not be retraced.
            start = path[1][0]-path[0][0], path[1][1]-path[0][1]
            end = path[-2][0]-path[-1][0], path[-2][1]-path[-1][1]
            reverse = int(start[0]*source.direction[0]+start[1]*source.direction[1] < -EPS)
            reverse += int(end[0]*target.direction[0]+end[1]*target.direction[1] < -EPS)
            key = (len(blockers), reverse, span)
            records.append({'key': list(key), 'static_blockers': sorted(blockers),
                'heading_reversals': reverse, 'escape_distance': span,
                'ports': [list(first), list(second)], 'escape_path': [list(p) for p in path]})
    check_deadline(deadline, 'readability_escape_risk')
    if not records:
        return None
    best = min(records, key=lambda row: (row['key'], row['escape_path'], row['ports']))
    return {**best, 'inspected_paths': len(records), 'max_port_pairs': MAX_PORT_PAIRS,
            'scope': 'static_port_escape_estimate_without_signal_occupancy'}


def _initializer_pairs(scene, comps, deadline):
    """Freeze bounded port pairs, round-robin over controls, before any trials."""
    catalogue, possible, controls_total = {}, 0, 0
    for net, pins in sorted(scene.pins.items()):
        check_deadline(deadline, 'readability_initializer_pairs')
        if net in scene.power_nets or len(pins) < 2:
            continue
        def pin_key(pin):
            return pin.escape, pin.direction, pin.pin, comps[pin.component]['id']
        controls = sorted((pin for pin in pins if pin.pin.upper() in CONTROL_PINS), key=pin_key)
        if not controls:
            continue
        controls_total += len(controls)
        possible += len(controls)*(len(pins)-len(controls))+len(controls)*(len(controls)-1)//2
        queues = []
        for control in controls[:MAX_PORT_PAIRS]:
            check_deadline(deadline, 'readability_initializer_pairs')
            peers = sorted((pin for pin in pins if pin is not control), key=lambda pin: (
                -(abs(pin.escape[0]-control.escape[0])+abs(pin.escape[1]-control.escape[1])), pin_key(pin)))
            queues.append((control, iter(peers)))
        chosen, seen = [], set()
        while queues and len(chosen) < MAX_PORT_PAIRS:
            pending = []
            for control, peers in queues:
                check_deadline(deadline, 'readability_initializer_pairs')
                for peer in peers:
                    pair = tuple(sorted(((control.component, control.pin), (peer.component, peer.pin))))
                    if pair in seen:
                        continue
                    chosen.append(pair)
                    seen.add(pair)
                    pending.append((control, peers))
                    break
                if len(chosen) >= MAX_PORT_PAIRS:
                    break
            queues = pending
        if chosen:
            catalogue[net] = tuple(chosen)
    return catalogue, possible, controls_total


def make_initializer_cost(par, baseline, comps, nets, index, plan, *, options=None, deadline=None):
    """Build a bounded initial-layout estimator; never attach it to SA.

    The returned callable uses a frozen port-pair catalogue and the original
    HPWL/readability cost plus a static escape estimate. Its ``peek`` attribute
    reads an already completed measurement without work, even after timeout.
    ``None`` means no usable estimator; the accompanying audit explains why.
    A timeout during a later trial raises RoutingFailure so the caller can
    restore its initializer input instead of accepting a partial low score.
    """
    from netlist2schematic.plugins.readability.readability_layout import _hpwl as estimate_hpwl, score_readability

    options = RoutingOptions() if options is None else options
    if not isinstance(options, RoutingOptions):
        raise ValueError('Readability initializer options must be RoutingOptions')
    if deadline is not None and (type(deadline) not in (int, float) or not isfinite(deadline)):
        raise ValueError('Readability initializer deadline must be finite')
    started = perf_counter()
    until = min(started+SECONDS_LIMIT, deadline) if deadline is not None else started+SECONDS_LIMIT
    audit = dict(version=INITIALIZER_VERSION, applied=False, status='skipped', reason=None,
        max_trials=0, max_port_pairs_per_net=MAX_PORT_PAIRS, pair_evaluation_budget=INITIALIZER_PAIR_BUDGET,
        evaluations=0, trial_evaluations=0, cache_hits=0, seconds_limit=SECONDS_LIMIT,
        weights=dict(static_blockers=3.0, heading_reversals=1.0, escape_distance_per_100=.25),
        aggregation='mean_per_net_of_mean_per_pair', records=[],
        sa_hot_path=dict(enabled=False, policy='bounded_initialization_and_feedback_only'),
        scope='static_port_escape_estimate_without_signal_occupancy')

    def stopped(reason):
        audit.update(reason=reason, elapsed_seconds=perf_counter()-started)
        return None, audit

    if not isinstance(plan, dict):
        return stopped('no_readability_plan')
    if (set(index.values()) != set(range(len(comps)))
            or any(index.get(comp['id']) != i for i, comp in enumerate(comps))):
        return stopped('incomplete_component_index')
    if getattr(baseline, 'power_connections', None) is not None:
        return stopped('custom_power_connections')
    if not all(hasattr(baseline, name) for name in ('column_x', 'row_y', 'cell_of', 'orient')):
        return stopped('unsupported_placement')
    measured = measure_readability(baseline, comps, index, plan)
    audit['organization_not_available'] = not bool(plan.get('supported'))
    if plan.get('supported') and not measured.get('measurable'):
        return stopped('unknown_organization_measurement')

    def state_key(pl):
        # Logical cells also matter to the organization score; world positions
        # include optional local offsets and all geometry used by the Scene.
        return (tuple(pl.column_x), tuple(pl.row_y), tuple(pl.cell_of), tuple(pl.orient),
                tuple(tuple(pl.pos_of(i)) for i in range(len(comps))))

    cache = {}
    try:
        check_deadline(until, 'readability_initializer_start')
        scene = build_scene(par, nets, baseline, index, options=options, deadline=until)
        catalogue, possible, controls = _initializer_pairs(scene, comps, until)
        pair_count = sum(map(len, catalogue.values()))
        audit.update(pair_count=pair_count, possible_pair_count=possible, control_port_count=controls,
            uncovered_pair_count=possible-pair_count, net_count=len(catalogue),
            catalogue={net: [[[comps[i]['id'], pin] for i, pin in pair] for pair in pairs]
                       for net, pairs in catalogue.items()})
        if not pair_count:
            return stopped('no_control_port_pairs')
        audit['max_trials'] = min(INITIALIZER_TRIALS, max(1, INITIALIZER_PAIR_BUDGET//pair_count))

        def evaluate(pl, initial_scene=None):
            try:
                check_deadline(until, 'readability_initializer_cost')
                key = state_key(pl)
                if key in cache:
                    audit['cache_hits'] += 1
                    audit['elapsed_seconds'] = perf_counter()-started
                    return cache[key]['cost']
                if initial_scene is None and audit['trial_evaluations'] >= audit['max_trials']:
                    audit.update(status='trial_limit', reason='initializer_cost_trial_limit')
                    raise RoutingFailure('Readability initializer exhausted its trial quota',
                        {'reason': 'initializer_cost_trial_limit', 'stage': 'readability_initializer_cost'})
                current_scene = initial_scene or build_scene(par, nets, pl, index, options=options, deadline=until)
                net_records = []
                for net, pairs in catalogue.items():
                    pair_records = []
                    for a, b in pairs:
                        check_deadline(until, 'readability_initializer_cost')
                        risk = escape_risk(current_scene, net, [[a], [b]], until)
                        if risk is None:
                            ports = {(pin.component, pin.pin): pin for pin in current_scene.pins.get(net, ())}
                            if a in ports and b in ports and ports[a].escape == ports[b].escape:
                                _, _, blockers = current_scene.vertex_check(net, ports[a].escape,
                                                                           current_scene.prepare_occupied({}))
                                risk = dict(static_blockers=sorted(blockers), heading_reversals=0, escape_distance=0)
                            else:
                                audit.update(status='rejected', reason='missing_port_pair_measurement')
                                raise RoutingFailure('Readability initializer could not measure a frozen port pair',
                                    {'reason': 'missing_port_pair_measurement', 'stage': 'readability_initializer_cost'})
                        value = 3*len(risk['static_blockers'])+risk['heading_reversals']+.25*risk['escape_distance']/100
                        pair_records.append(dict(ports=[[comps[i]['id'], pin] for i, pin in (a, b)],
                            cost=value, static_blockers=risk['static_blockers'],
                            heading_reversals=risk['heading_reversals'], escape_distance=risk['escape_distance']))
                    net_records.append(dict(net=net, cost=sum(r['cost'] for r in pair_records)/len(pair_records),
                                            pairs=pair_records))
                risk_cost = sum(row['cost'] for row in net_records)/len(net_records)
                organization = score_readability(pl, comps, index, plan)
                if plan.get('supported') and not organization['metrics'].get('measurable'):
                    audit.update(status='rejected', reason='unknown_organization_measurement')
                    raise RoutingFailure('Readability initializer lost organization coverage',
                        {'reason': 'unknown_organization_measurement', 'stage': 'readability_initializer_cost'})
                hpwl = estimate_hpwl(pl, nets, index)
                cost = hpwl/100+organization['weighted_total']+risk_cost
                check_deadline(until, 'readability_initializer_commit')
                record = dict(cost=cost, estimated_hpwl=hpwl, readability_cost=organization['weighted_total'],
                              escape_risk=risk_cost, nets=net_records)
                cache[key] = record
                audit['evaluations'] += 1
                if initial_scene is None:
                    audit['trial_evaluations'] += 1
                audit['records'].append(deepcopy(record))
                audit.update(status='ready', reason='complete_measurement', elapsed_seconds=perf_counter()-started)
                return cost
            except RoutingFailure as exc:
                if exc.diagnostics.get('reason') == 'quality_time_budget':
                    audit.update(status='time_budget', reason='quality_time_budget')
                audit['elapsed_seconds'] = perf_counter()-started
                raise

        evaluate(baseline, scene)
        audit['before'] = deepcopy(cache[state_key(baseline)])
        audit['applied'] = True
        def estimate(pl):
            return evaluate(pl)
        estimate.peek = lambda pl: deepcopy(cache.get(state_key(pl)))
        return estimate, audit
    except RoutingFailure as exc:
        audit.update(status='time_budget' if exc.diagnostics.get('reason') == 'quality_time_budget' else 'rejected',
                     reason=exc.diagnostics.get('reason'), elapsed_seconds=perf_counter()-started)
        return None, audit


def _trials(pl, comps, index, branch, metadata, deadline):
    related = sorted({i for group in branch['port_groups'] for i, _ in group})
    fixed = _fixed(pl, index)
    clock = {index[row['device']] for row in metadata.get('role_adjustments', ())
             if row.get('kind') == 'latch_clock_access' and row.get('device') in index}
    groups = set()
    for i in related:
        check_deadline(deadline, 'readability_feedback_proposals')
        if i in fixed:
            continue
        if comps[i]['symbol'] in PASSIVE:
            group, kind = (i,), 'passive_neighbor_column'
        elif comps[i]['symbol'] in ACTIVE:
            containing = [tuple(sorted(block)) for block in getattr(pl, 'blocks', ()) if i in block]
            group = min(containing, key=lambda block: (len(block), block), default=(i,))
            if len(group) > 4 or any(comps[j]['symbol'] not in ACTIVE for j in group):
                continue
            kind = 'active_leaf_column'
        else:
            continue
        if group not in groups and not fixed.intersection(group):
            groups.add(group)
            for delta in (-1, 1, -2, 2):
                cells = list(pl.cell_of)
                for j in group:
                    row, col = cells[j]
                    cells[j] = (row, col+delta)
                if any(not 0 <= cells[j][1] < len(pl.column_x) or
                       pl.comp_at.get(cells[j]) not in (None, *group) for j in group):
                    continue
                trial = deepcopy(pl)
                _sync(trial, cells, pl.orient, group)
                yield trial, {'kind': kind, 'members': [comps[j]['id'] for j in group], 'columns': delta}
        if comps[i]['symbol'] in ('nmos', 'pmos') and i not in clock:
            other = 1-pl.orient[i]
            if other in pl.allowed_orientations(i):
                trial = deepcopy(pl)
                orient = list(pl.orient)
                orient[i] = other
                _sync(trial, pl.cell_of, orient, (i,))
                yield trial, {'kind': 'mos_horizontal_mirror', 'members': [comps[i]['id']]}


def feedback_readability(par, pl, comps, nets, index, metadata, routed_result, *,
                         options=None, deadline=None, statistics=None):
    """Return a separate candidate-three proposal, or None without any mutation."""
    if (not isinstance(routed_result, (list, tuple)) or len(routed_result) != 3
            or not isinstance(routed_result[2], dict) or not routed_result[2].get('valid')):
        raise ValueError('Readability feedback requires a fully validated routed result')
    if statistics is not None:
        if not isinstance(statistics, dict):
            raise ValueError('Readability feedback statistics must be a dict')
        statistics['candidates'] = 0
    options = options or RoutingOptions()
    if not isinstance(options, RoutingOptions):
        raise ValueError('Readability feedback options must be RoutingOptions')
    if deadline is not None and (type(deadline) not in (float, int) or not isfinite(deadline)):
        raise ValueError('Readability feedback deadline must be finite')
    started = perf_counter()
    until = min(started+SECONDS_LIMIT, deadline) if deadline is not None else started+SECONDS_LIMIT
    if perf_counter() >= until or getattr(pl, 'power_connections', None) is not None:
        return None
    if not all(hasattr(pl, name) for name in ('column_x', 'row_y', 'cell_of', 'orient')):
        return None
    plan = metadata.get('layout_readability', {}).get('plan')
    if not plan:
        return None
    if (set(index.values()) != set(range(len(comps)))
            or any(index.get(comp['id']) != i for i, comp in enumerate(comps))):
        return None
    measured = measure_readability(pl, comps, index, plan)
    organization = None
    if plan.get('supported'):
        organization = readability_rank(measured)
        if not measured.get('measurable') or not all(isfinite(value) for value in organization):
            return None
    best, best_risk, report = None, None, None
    try:
        scene = build_scene(par, nets, pl, index, options=options, deadline=until)
        routed = [(net, [(tuple(a), tuple(b)) for a, b in segments]) for net, segments in routed_result[0]]
        # Verify the snapshot belongs to this exact placement. No complete
        # routing is repeated, and no coordinates are borrowed from another seed.
        for net, pins in scene.pins.items():
            points = canonical_topology(dict(routed).get(net, ()),
                pins=scene.all_pins.get(net, ()), deadline=until)['adjacency']
            if any(pin.point not in points for pin in pins):
                return None
        branches = detouring_branches(scene, routed, physical_pitch(par, pl), until)
        if not branches:
            return None
        branch = branches[0]
        before_risk = escape_risk(scene, branch['net'], branch['port_groups'], until)
        if before_risk is None:
            return None
        baseline_hpwl = _hpwl(par, pl, nets, index)
        best_risk = before_risk
        report = {'version': VERSION, 'source_candidate': metadata.get('candidate_id'),
            'net': branch['net'], 'branch': deepcopy(branch), 'before_risk': before_risk,
            'before_organization': measured, 'candidates': 0, 'legal_rejections': 0,
            'organization_not_available': organization is None,
            'organization_guard': ('measured_non_regression' if organization is not None else
                                   'unavailable_no_supported_relations'),
            'organization_rejections': 0, 'hpwl_rejections': 0, 'risk_rejections': 0,
            'static_rejections': 0, 'max_candidates': MAX_TRIALS, 'seconds_limit': SECONDS_LIMIT,
            'estimated_hpwl_before': baseline_hpwl, 'estimated_hpwl_limit': baseline_hpwl*1.15,
            'sa_hot_path': {'enabled': False, 'policy': 'bounded_feedback_only',
                'maximum_profile_overhead_ratio': .20, 'profile': '20k-estimator-comparison'},
            'stop_reason': 'no_improvement'}
        for trial, action in _trials(pl, comps, index, branch, metadata, until):
            check_deadline(until, 'readability_feedback_trial')
            if report['candidates'] >= MAX_TRIALS:
                report['stop_reason'] = 'candidate_limit'
                break
            report['candidates'] += 1
            if statistics is not None:
                statistics['candidates'] = report['candidates']
            if trial.row_of != pl.row_of or not _valid(pl, trial, comps, index, set(), options):
                report['legal_rejections'] += 1
                continue
            candidate_measurement = measure_readability(trial, comps, index, plan)
            if organization is not None and readability_rank(candidate_measurement) > organization:
                report['organization_rejections'] += 1
                continue
            hpwl = _hpwl(par, trial, nets, index)
            if hpwl > baseline_hpwl*1.15+EPS:
                report['hpwl_rejections'] += 1
                continue
            trial_scene = build_scene(par, nets, trial, index, options=options, deadline=until)
            risk = escape_risk(trial_scene, branch['net'], branch['port_groups'], until)
            if risk is None or tuple(risk['key']) >= tuple(best_risk['key']):
                report['risk_rejections'] += 1
                continue
            if not _static_safe(par, trial, nets, index, options, until):
                report['static_rejections'] += 1
                continue
            check_deadline(until, 'readability_feedback_commit')
            best, best_risk = trial, risk
            report.update(action=deepcopy(action), after_risk=deepcopy(risk),
                after_organization=candidate_measurement, estimated_hpwl_after=hpwl,
                stop_reason='improved')
    except RoutingFailure as exc:
        if exc.diagnostics.get('reason') != 'quality_time_budget':
            raise
        if report is not None:
            report['stop_reason'] = 'time_budget'
    if best is None:
        return None
    result = deepcopy(metadata)
    report['elapsed_seconds'] = perf_counter()-started
    report['branch']['port_groups'] = [[[comps[i]['id'], pin] for i, pin in group]
                                       for group in report['branch']['port_groups']]
    result.setdefault('layout_readability', {})['routing_feedback'] = report
    result['layout_readability']['metrics'] = report['after_organization']
    result['layout_readability']['measurement_stage'] = 'after_route_feedback_before_rerouting'
    result['layout_grid'] = {'column_x': list(best.column_x), 'row_y': list(best.row_y)}
    result['feedback'] = {'action': 'readability_local_escape_risk',
                          'source_candidate': metadata.get('candidate_id'), 'net': report['net']}
    result['candidate_id'] = 'feedback'
    return best, deepcopy(comps), deepcopy(nets), deepcopy(index), result


def benchmark_escape_risk(par, pl, comps, nets, index, branch, *, iterations=20000):
    """Explicit profiling helper, never called by flow or ordinary tests."""
    from netlist2schematic.layout.structured_placement import _estimate
    started = perf_counter()
    for _ in range(iterations):
        _estimate(pl, nets, index, [], [])
    base = perf_counter()-started
    started = perf_counter()
    for _ in range(iterations):
        _estimate(pl, nets, index, [], [])
        scene = build_scene(par, nets, pl, index)
        escape_risk(scene, branch['net'], branch['port_groups'])
    combined = perf_counter()-started
    ratio = (combined-base)/max(base, EPS)
    return {'version': VERSION, 'iterations': iterations, 'baseline_seconds': base,
            'estimate_plus_risk_seconds': combined, 'overhead_ratio': ratio,
            'sa_hot_path_enabled': False, 'over_20_percent': ratio > .20,
            'decision': 'bounded_feedback_only', 'includes_scene_rebuild': True}
