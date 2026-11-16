"""Plan monotone conduction columns; keep short side accesses on nearby lanes.

GT and instance/net names never choose a layout. Unique continuations are safe
seeds. A fork needs explicit composite port roles or repeated multi-level
controls on disjoint paths; longest-path preference alone cannot resolve it.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
from math import isfinite
from statistics import median
from time import perf_counter

from netlist2schematic.layout.series_alignment import (
    CHANNELS, CONTROLS, EPS, _fixed_ids, _objective_context, _valid_trial,
    discover_series, measure_series)

VERSION = 'conduction-column-plan-v1'


def _ports(comp):
    first, second = CHANNELS[comp['symbol']]
    return (first, second) if comp['symbol'] in ('nmos', 'npn') else (second, first)


def _identity(prefix, names):
    return prefix + ':' + sha256(repr(tuple(sorted(names))).encode()).hexdigest()[:16]


def build_column_plan(par, pl, comps, nets, index, structures=()):
    active = {c['id']: c for c in comps if c['symbol'] in CHANNELS}
    rows = {n: pl.cell_of[index[n]][0] for n in active}
    order = lambda n: (rows[n], pl.cell_of[index[n]][1])
    incoming, outgoing = defaultdict(list), defaultdict(list)
    for name, comp in active.items():
        inlet, outlet = _ports(comp)
        incoming[comp['nodes'][inlet]].append((name, inlet))
        outgoing[comp['nodes'][outlet]].append((name, outlet))
    anchors, preferred = set(), set()
    for r in structures:
        if r.get('confidence', 'high') != 'high':
            continue
        roles = r.get('roles', {})
        if r['kind'] == 'differential_pair':
            pair = [roles.get('left'), roles.get('right')]
            if all(n in active for n in pair):
                terminal = CHANNELS[active[pair[0]]['symbol']][1]
                values = {active[n]['nodes'][terminal] for n in pair}
                if len(values) == 1:
                    anchors.update(values)
        elif r['kind'] == 'folded_core':
            for side in ('left', 'right'):
                a, b, tap = (roles.get(k+'_'+side) for k in ('bias', 'folded', 'input'))
                if all(n in active for n in (a, b, tap)):
                    for first, second in ((a, b), (b, a)):
                        _, pa = _ports(active[first]); pb, _ = _ports(active[second])
                        net = active[first]['nodes'][pa]
                        if (net == active[second]['nodes'][pb] == active[tap]['nodes'].get('D')
                                and active[a]['symbol'] == active[b]['symbol'] != active[tap]['symbol']):
                            preferred.add((first, second, net))
        elif r['kind'] == 'latch':
            for side in ('left', 'right'):
                a, b = roles.get(side+'_upper'), roles.get(side+'_lower')
                if a in active and b in active:
                    _, pa = _ports(active[a]); pb, _ = _ports(active[b])
                    if active[a]['nodes'][pa] == active[b]['nodes'][pb]:
                        preferred.add((a, b, active[a]['nodes'][pa]))

    raw, skipped = [], []
    for net in nets:
        if par.power_marker_sym(net) or net in anchors:
            continue
        for a, pa in outgoing[net]:
            for b, pb in incoming[net]:
                if a == b or rows[a] >= rows[b]:
                    continue
                raw.append(dict(a=a, pin_a=pa, b=b, pin_b=pb, net=net,
                    unique=len(outgoing[net]) == len(incoming[net]) == 1,
                    explicit=(a, b, net) in preferred))
    raw.sort(key=lambda e: (order(e['a']), order(e['b'])))
    adjacent = defaultdict(list)
    for edge in raw:
        adjacent[edge['a']].append(edge['b'])
    # Strict display-row increase makes this a DAG, even if electrical feedback
    # exists. One longest candidate from each start avoids exponential paths.
    suffix = {}
    for n in sorted(active, key=order, reverse=True):
        options = [(n,)+suffix[b] for b in adjacent[n]]
        best = max((len(p) for p in options), default=0)
        longest = [p for p in options if len(p) == best]
        # An equally long fork has no distinguished backbone. Never choose it
        # by current left/right coordinates, name, or input order.
        suffix[n] = longest[0] if len(longest) == 1 else (n,)
    candidates = list(dict.fromkeys(suffix.values()))
    signature = lambda path: tuple((active[n]['symbol'], active[n]['nodes'][CONTROLS[active[n]['symbol']]]) for n in path)
    repeated = defaultdict(list)
    for path in candidates:
        sig = signature(path)
        controls = {net for _, net in sig if not par.power_marker_sym(net)}
        if len(path) >= 2 and len(controls) >= 2:
            repeated[sig].append(path)
    supported, cohorts = defaultdict(int), []
    for paths in repeated.values():
        disjoint = [p for p in paths if any(not set(p) & set(q) for q in paths if q != p)]
        if len(disjoint) < 2:
            continue
        cohorts.append([list(p) for p in disjoint])
        for path in disjoint:
            for a, b in zip(path, path[1:]):
                supported[a, b] = max(supported[a, b], len(path))

    # Resolve competing continuation ports explicitly. Ties remain interfaces.
    scored = []
    for e in raw:
        score = (int(e['explicit']), supported[e['a'], e['b']])
        if e['unique'] or score > (0, 0):
            scored.append((score, e))
        else:
            skipped.append(dict(e, reason='unconfirmed_fork'))
    by_out, by_in = defaultdict(list), defaultdict(list)
    for score, e in scored:
        by_out[e['a']].append((score, e)); by_in[e['b']].append((score, e))
    edges = []
    for score, e in scored:
        choices = by_out[e['a']]
        entries = by_in[e['b']]
        best_out = max(s for s, _ in choices); best_in = max(s for s, _ in entries)
        if (score != best_out or score != best_in
                or sum(s == best_out for s, _ in choices) != 1
                or sum(s == best_in for s, _ in entries) != 1):
            skipped.append(dict(e, reason='ambiguous_or_lower_supported_continuation'))
            continue
        edges.append(dict(e, evidence='explicit_composite_ports' if e['explicit'] else
                          'repeated_multilevel_controls' if score[1] else 'unique_active_continuation'))
    after = {e['a']: e['b'] for e in edges}
    predecessors = {e['b'] for e in edges}
    paths = []
    for root in sorted(set(active)-predecessors, key=order):
        path = [root]
        while path[-1] in after:
            path.append(after[path[-1]])
        paths.append(path)
    paths.sort(key=lambda p: (-len(p), tuple(order(n) for n in p)))
    columns = [dict(id=_identity('column', p), members=p) for p in paths]
    owner = {n: c['id'] for c in columns for n in c['members']}
    by_id = {c['id']: c for c in columns}
    side, seen = [], set()
    for e in edges:
        host = owner[e['a']]
        for tap, pin in outgoing[e['net']]+incoming[e['net']]:
            child = owner[tap]
            if child == host or len(by_id[child]['members']) >= len(by_id[host]['members']):
                continue
            key = child, host, e['net']
            if key not in seen:
                seen.add(key)
                side.append(dict(side=child, host=host, net=e['net'], tap=tap, pin=pin,
                                 anchor_devices=[e['a'], e['b']]))
    covered = sum(len(p) for p in paths if len(p) > 1)
    return dict(version=VERSION, columns=columns, edges=edges, side_accesses=side,
        corresponding_paths=cohorts, skipped=skipped, shared_junction_anchors=sorted(anchors),
        active_count=len(active), covered_active_count=covered,
        supported=bool(edges),
        terminal_contract={c['id']: dict(symbol=c['symbol'], nodes=deepcopy(c['nodes'])) for c in comps},
        active_rows=rows)


def compatible_plan(plan, pl, comps, index):
    return (plan['terminal_contract'] == {c['id']: dict(symbol=c['symbol'], nodes=c['nodes']) for c in comps}
            and all(pl.cell_of[index[n]][0] == row for n, row in plan['active_rows'].items()))


def measure_columns(pl, index, plan):
    breaks = pin_error = spread = 0
    for e in plan['edges']:
        a, b = index[e['a']], index[e['b']]
        breaks += pl.cell_of[a][1] != pl.cell_of[b][1]
        pin_error += abs(pl.pin_world(a, e['pin_a'])[0]-pl.pin_world(b, e['pin_b'])[0])
    by_id = {c['id']: c for c in plan['columns']}
    for c in plan['columns']:
        cols = [pl.cell_of[index[n]][1] for n in c['members']]
        spread += max(cols)-min(cols)
    intrusions = distance = 0
    for r in plan['side_accesses']:
        host_cols = [pl.cell_of[index[n]][1] for n in by_id[r['host']]['members']]
        side_cols = [pl.cell_of[index[n]][1] for n in by_id[r['side']]['members']]
        gap = abs(median(host_cols)-median(side_cols))
        intrusions += gap < 1
        distance += max(0, gap-1)
    reversals = 0
    for cohort in plan['corresponding_paths']:
        for k, a in enumerate(cohort):
            for b in cohort[k+1:]:
                if set(a) & set(b):
                    continue
                signs = [pl.pos_of(index[x])[0]-pl.pos_of(index[y])[0] for x, y in zip(a, b)]
                reversals += min(sum(v < 0 for v in signs), sum(v > 0 for v in signs))
    return dict(column_breaks=breaks, order_reversals=reversals, side_intrusions=intrusions,
        pin_error=pin_error, column_spread=spread, side_distance=distance,
        covered_active_count=plan['covered_active_count'], active_count=plan['active_count'])


def column_key(metrics):
    return (metrics['column_breaks'], metrics['order_reversals'], metrics['side_intrusions'],
            round(metrics['pin_error'], 6), metrics['column_spread'], metrics['side_distance'])


def preserves_columns(before, trial, index):
    plan = getattr(before, 'column_plan', None)
    if plan is None:
        return True
    a, b = measure_columns(before, index, plan), measure_columns(trial, index, plan)
    return all(b[k] <= a[k]+EPS for k in ('column_breaks', 'order_reversals', 'side_intrusions',
                                         'pin_error', 'column_spread', 'side_distance'))


def _proposals(pl, index, plan):
    paths = plan['columns']; by_id = {c['id']: c for c in paths}
    seen = set()
    for path in paths:
        if len(path['members']) < 2:
            continue
        ids = {index[n] for n in path['members']}
        columns = sorted(range(pl.cols), key=lambda c: (
            sum(abs(pl.cell_of[i][1]-c) for i in ids), c))
        for col in columns:
            cells = list(pl.cell_of)
            for i in ids:
                cells[i] = (cells[i][0], col)
            if cells == pl.cell_of:
                continue
            yield_states = [cells]
            # Only a confirmed side access may yield space; unrelated devices
            # and peer long columns are never moved to make a trial fit.
            occupied = {i for i, cell in enumerate(pl.cell_of) if i not in ids and
                        cell in {cells[j] for j in ids}}
            for r in plan['side_accesses']:
                child = {index[n] for n in by_id[r['side']]['members']}
                if r['host'] != path['id'] or not occupied or not occupied <= child:
                    continue
                for side_col in (col-1, col+1):
                    if not 0 <= side_col < pl.cols:
                        continue
                    adjusted = list(cells)
                    for i in child:
                        adjusted[i] = (adjusted[i][0], side_col)
                    yield_states.append(adjusted)
            for state in yield_states:
                key = tuple(state)
                if key not in seen:
                    seen.add(key)
                    moved = {i for i, (a, b) in enumerate(zip(pl.cell_of, state)) if a != b}
                    yield moved, state
    for r in plan['side_accesses']:
        child = {index[n] for n in by_id[r['side']]['members']}
        host_col = round(median(pl.cell_of[index[n]][1] for n in by_id[r['host']]['members']))
        for col in (host_col-1, host_col+1):
            if not 0 <= col < pl.cols:
                continue
            cells = list(pl.cell_of)
            for i in child:
                cells[i] = (cells[i][0], col)
            key = tuple(cells)
            if key not in seen and cells != pl.cell_of:
                seen.add(key)
                yield {i for i, (a, b) in enumerate(zip(pl.cell_of, cells)) if a != b}, cells


def refine_columns(par, pl, comps, nets, index, metadata, plan, options=None, *,
                   deadline=None, seconds=1.0, max_trials=128):
    if type(seconds) not in (int, float) or not isfinite(seconds) or seconds < 0:
        raise ValueError('column seconds must be finite and >= 0')
    if type(max_trials) is not int or max_trials < 0:
        raise ValueError('column max_trials must be an integer >= 0')
    started = perf_counter(); stop = min(deadline, started+seconds) if deadline is not None else started+seconds
    report = dict(version=VERSION, policy='auto', reference_candidate=False, plan=deepcopy(plan),
        applied=False, trials=0, actions=[], seconds_limit=seconds, max_trials=max_trials)
    current = pl
    def finish(reason):
        data = deepcopy(metadata)
        if report['applied']:
            current.column_plan = deepcopy(plan)
            report['source_energy'] = metadata.get('energy')
            data['energy'], data['optimization_metrics'] = _estimate(current, nets, index, alignment, pairs)
            data['optimization_metrics']['measurement_stage'] = 'after_column_layout'
        report.update(stop_reason=reason, elapsed_seconds=round(perf_counter()-started, 6),
                      current=measure_columns(current, index, plan))
        data['layout_columns'] = deepcopy(report)
        return current, data
    if not compatible_plan(plan, pl, comps, index):
        return finish('incompatible_plan')
    report['before'] = measure_columns(pl, index, plan)
    if not plan['supported']:
        return finish('no_supported_columns')
    if seconds == 0 or perf_counter() >= stop or max_trials == 0:
        return finish('budget_exhausted')
    from netlist2schematic.layout.structured_placement import _estimate
    from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
    from netlist2schematic.routing.orthogonal_router import RoutingFailure
    from netlist2schematic.plugins.preflight.placement_preflight import _wire_estimate
    alignment, pairs = _objective_context(metadata, index)
    fixed = _fixed_ids(pl, comps)
    unique, _ = discover_series(par, pl, comps, nets, index)
    wire = _wire_estimate(pl, nets, index, par)
    report['estimated_wire_limit'] = wire*1.15
    reason = 'round_limit'
    def safe(candidate):
        faults = _static_faults(par, candidate, nets, index, options, stop)
        return not faults['count'] and not faults['hard']
    try:
        if not safe(pl):
            return finish('unsafe_initial_geometry')
        current = deepcopy(pl)
        current.column_plan = deepcopy(plan)
        for _ in range(4):
            incumbent = None
            old_metrics = measure_columns(current, index, plan)
            old_unique = measure_series(current, index, unique)['relations']
            for moved, cells in _proposals(current, index, plan):
                if perf_counter() >= stop or report['trials'] >= max_trials:
                    reason = 'time_budget' if perf_counter() >= stop else 'trial_limit'; break
                report['trials'] += 1
                if len(set(cells)) != len(cells):
                    continue
                trial = deepcopy(current); trial.set_state(cells, trial.orient)
                if not _valid_trial(trial, current, moved, fixed, check_columns=False):
                    continue
                metrics = measure_columns(trial, index, plan)
                if column_key(metrics) >= column_key(old_metrics):
                    continue
                new_unique = measure_series(trial, index, unique)['relations']
                if any(b['horizontal_error'] > a['horizontal_error']+EPS for a, b in zip(old_unique, new_unique)):
                    continue
                if _wire_estimate(trial, nets, index, par) > wire*1.15+EPS:
                    continue
                energy, _ = _estimate(trial, nets, index, alignment, pairs)
                rank = column_key(metrics)+(energy, tuple(cells))
                if incumbent is not None and rank >= incumbent[0]:
                    continue
                try:
                    valid = safe(trial)
                except RoutingFailure as exc:
                    if exc.diagnostics.get('reason') != 'quality_time_budget':raise
                    reason = 'time_budget'; break
                if valid:incumbent = rank, trial, metrics
            if incumbent is None:
                if reason == 'round_limit':reason = 'no_legal_improvement'
                break
            _, trial, metrics = incumbent
            report['actions'].append(dict(before=old_metrics, after=metrics,
                changes=[dict(device=comps[i]['id'], before=list(current.cell_of[i]), after=list(cell))
                    for i, cell in enumerate(trial.cell_of) if cell != current.cell_of[i]]))
            current = trial; report['applied'] = True
            if reason in ('time_budget','trial_limit'):break
    except RoutingFailure as exc:
        if exc.diagnostics.get('reason') != 'quality_time_budget':raise
        reason = 'time_budget'
    return finish(reason)


def select_column_result(results, plan):
    reference = min(results, key=lambda r:r[0])
    metrics = reference[6][1]
    cap = metrics['crossings']+(1 if metrics['crossings'] else 0)
    eligible = [r for r in results if r[6][1]['wirelength'] <= metrics['wirelength']*1.15+EPS
                and r[6][1]['crossings'] <= cap]
    def measurement(r):return measure_columns(r[1], r[4], plan)
    winner = min(eligible, key=lambda r:column_key(measurement(r))+r[0])
    improved = column_key(measurement(winner)) < column_key(measurement(reference))
    chosen = winner if improved else reference
    return chosen, dict(policy='column-organization-v1', reference_candidate=reference[8],
        selected_candidate=chosen[8], wirelength_limit=metrics['wirelength']*1.15, crossings_limit=cap,
        reason='column_organization_gain' if improved else 'no_organization_gain',
        reference_metrics=measurement(reference), selected_metrics=measurement(chosen))
