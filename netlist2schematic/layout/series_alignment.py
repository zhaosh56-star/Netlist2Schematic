"""Bounded alignment of unambiguous conduction segments after annealing.

No architecture label, model/instance name, GT coordinate, SA or A* is used.
Control taps do not break a series segment; additional conductive taps do.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from math import isfinite
from time import perf_counter

VERSION = 'unique-series-alignment-v1'
CHANNELS = {'nmos': ('D', 'S'), 'pmos': ('D', 'S'),
            'npn': ('C', 'E'), 'pnp': ('C', 'E')}
CONTROLS = {'nmos': 'G', 'pmos': 'G', 'npn': 'B', 'pnp': 'B'}
EPS = 1e-8


def discover_series(par, placement, comps, nets, index):
    """Keep only same-polarity, row-consistent, unique D/S or C/E joins."""
    accepted, skipped = [], []
    for net, terminals in nets.items():
        if par.power_marker_sym(net):
            continue
        conductive, active = [], []
        for name, pin in terminals:
            i = index[name]
            symbol = comps[i]['symbol']
            if pin == CONTROLS.get(symbol):
                continue
            conductive.append((i, pin))
            if pin in CHANNELS.get(symbol, ()):
                active.append((i, pin))
        if len(active) < 2:
            continue
        reason = None
        if len(conductive) != 2 or len(active) != 2:
            reason = 'additional_conductive_tap'
        else:
            (a, pa), (b, pb) = active
            symbol = comps[a]['symbol']
            if a == b:
                reason = 'self_loop'
            elif symbol != comps[b]['symbol']:
                reason = 'different_polarity'
            elif pa == pb:
                reason = 'not_a_series_join'
            else:
                outgoing = CHANNELS[symbol][1 if symbol in ('nmos', 'npn') else 0]
                upper, lower = ((a, pa), (b, pb)) if pa == outgoing else ((b, pb), (a, pa))
                if placement.cell_of[upper[0]][0] >= placement.cell_of[lower[0]][0]:
                    reason = 'display_order_conflict'
        if reason:
            skipped.append({'net': net, 'reason': reason,
                            'terminals': [[comps[i]['id'], p] for i, p in conductive]})
        else:
            accepted.append({'net': net, 'upper': comps[upper[0]]['id'], 'upper_pin': upper[1],
                             'lower': comps[lower[0]]['id'], 'lower_pin': lower[1],
                             'evidence': 'unique_conduction_join_with_control_taps_allowed'})
    accepted.sort(key=lambda r: (placement.cell_of[index[r['upper']]],
                                 placement.cell_of[index[r['lower']]]))
    return accepted, skipped


def measure_series(placement, index, relations):
    details = []
    for link in relations:
        a = placement.pin_world(index[link['upper']], link['upper_pin'])
        b = placement.pin_world(index[link['lower']], link['lower_pin'])
        details.append({**link, 'upper_point': list(a), 'lower_point': list(b),
                        'horizontal_error': abs(a[0]-b[0])})
    return {'relation_count': len(details), 'misaligned': sum(r['horizontal_error'] > EPS for r in details),
            'horizontal_error': sum(r['horizontal_error'] for r in details), 'relations': details}


def _objective_context(metadata, index):
    alignment = [(index[a], index[b], r['weight']) for r in metadata.get('alignment_relations', ())
                 for a, b in [r['devices']] if a in index and b in index]
    differential = {frozenset((r['roles'].get('left'), r['roles'].get('right')))
                    for r in metadata.get('structs', ()) if r['kind'] == 'differential_pair'}
    pairs = [(index[a], index[b], 'differential_pair' if frozenset((a, b)) in differential else 'pair')
             for a, b in metadata.get('sym_pairs', ()) if a in index and b in index]
    return alignment, pairs


def _fixed_ids(placement, comps):
    values = set(getattr(placement, 'fixed', ())) | set(getattr(placement, 'fixed_cells', {}))
    return {i for i, c in enumerate(comps) if i in values or c['id'] in values}


def _proposals(placement, comps, index, relations, fixed):
    edges = [(index[r['upper']], index[r['lower']]) for r in relations]
    adjacency = defaultdict(set)
    for a, b in edges:
        adjacency[a].add(b)
        adjacency[b].add(a)
    errors = measure_series(placement, index, relations)['relations']
    order = sorted(range(len(relations)), key=lambda k: (-errors[k]['horizontal_error'],
        placement.cell_of[edges[k][0]], placement.cell_of[edges[k][1]]))
    seen = set()
    for k in order:
        if errors[k]['horizontal_error'] <= EPS:
            continue
        link = relations[k]
        a, b = edges[k]
        for moving, anchor, pin, anchor_pin in ((a, b, link['upper_pin'], link['lower_pin']),
                                               (b, a, link['lower_pin'], link['upper_pin'])):
            component, pending = {moving}, [moving]
            while pending:
                here = pending.pop()
                for other in adjacency[here]:
                    if {here, other} == {a, b} or other in component:
                        continue
                    component.add(other)
                    pending.append(other)
            groups = [{moving}]
            if component != groups[0]:
                groups.append(component)
            column = placement.cell_of[moving][1]
            local_x = placement.pin_world(moving, pin)[0] - placement.column_x[column]
            target = placement.pin_world(anchor, anchor_pin)[0]
            columns = sorted(range(max(0, column-2), min(placement.cols, column+3)),
                key=lambda c: (abs(placement.column_x[c]+local_x-target), abs(c-column), c))
            for group in groups:
                if group & fixed or anchor in group:
                    continue
                for col in columns[:2]:
                    if col == column:
                        continue
                    delta = col-column
                    cells = list(placement.cell_of)
                    for i in group:
                        row, old_col = cells[i]
                        cells[i] = row, old_col+delta
                    key = tuple(cells)
                    if key not in seen:
                        seen.add(key)
                        yield k, group, cells


def _valid_trial(trial, original, moved, fixed, *, check_columns=True):
    if len(trial.comp_at) != len(trial.comps):
        return False
    if trial.orient != original.orient or trial.column_x != original.column_x or trial.row_y != original.row_y:
        return False
    for i, cell in enumerate(trial.cell_of):
        if cell[0] != original.cell_of[i][0] or not 0 <= cell[1] < trial.cols:
            return False
        if (i not in moved or i in fixed) and (cell != original.cell_of[i]
                                              or trial.pos_of(i) != original.pos_of(i)):
            return False
    if hasattr(trial, 'valid_state') and not trial.valid_state():
        return False
    if not trial.blocks_contiguous():
        return False
    if check_columns and getattr(original, 'column_plan', None) is not None:
        from netlist2schematic.plugins.columns.column_layout import preserves_columns
        index = {c['id']: i for i, c in enumerate(original.comps)}
        if not preserves_columns(original, trial, index):
            return False
    return True


def refine_series_alignment(par, placement, comps, nets, index, metadata, options=None, *,
                            deadline=None, seconds=1.0, max_trials=128):
    """Transactionally improve true-pin alignment and the existing SA estimate.

    Every join's horizontal error must be nonincreasing; no working alignment
    can be sacrificed to align a neighboring segment. Geometry checks use the
    same fixed-port/body/marker oracle as preflight. Final A* remains mandatory.
    """
    if type(seconds) not in (int, float) or not isfinite(seconds) or seconds < 0:
        raise ValueError('series alignment seconds must be finite and >= 0')
    if type(max_trials) is not int or max_trials < 0:
        raise ValueError('series alignment max_trials must be an integer >= 0')
    started = perf_counter()
    stop = min(deadline, started+seconds) if deadline is not None else started+seconds
    report = {'version': VERSION, 'applied': False, 'trials': 0, 'actions': [],
              'seconds_limit': seconds, 'max_trials': max_trials,
              'phase': 'after_preflight_before_final_routing', 'rejections': {}}
    previous = metadata.get('series_alignment')
    if isinstance(previous, dict):
        report['inherited_actions'] = deepcopy(previous.get('inherited_actions', [])
                                                + previous.get('actions', []))
    current = placement

    def finish(reason):
        data = deepcopy(metadata)
        report.update(stop_reason=reason, elapsed_seconds=round(perf_counter()-started, 6))
        if 'relations' in report:
            report['current'] = measure_series(current, index, report['relations'])
        if report['applied']:
            report['source_sa_energy'] = metadata.get('energy')
            report['source_optimization_metrics'] = deepcopy(metadata.get('optimization_metrics'))
            data['energy'], data['optimization_metrics'] = _estimate(current, nets, index, alignment, pairs)
            data['optimization_metrics']['measurement_stage'] = 'after_series_alignment'
        data['series_alignment'] = deepcopy(report)
        return current, data

    if not all(hasattr(placement, k) for k in ('cell_of', 'orient', 'column_x', 'row_y',
                                               'cols', 'set_state', 'comp_at', 'blocks_contiguous')):
        return finish('unsupported_placement')
    if getattr(placement, 'power_connections', None) is not None:
        return finish('custom_power_connections')
    if seconds == 0 or perf_counter() >= stop:
        return finish('time_budget')
    relations, skipped = discover_series(par, placement, comps, nets, index)
    report.update(relations=relations, skipped=skipped, before=measure_series(placement, index, relations))
    if not relations:
        return finish('no_unambiguous_series')
    if not report['before']['misaligned']:
        return finish('already_aligned')
    if max_trials == 0:
        return finish('trial_limit')
    from netlist2schematic.layout.structured_placement import _estimate
    from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
    from netlist2schematic.routing.orthogonal_router import RoutingFailure
    alignment, pairs = _objective_context(metadata, index)
    energy, _ = _estimate(current, nets, index, alignment, pairs)
    report['estimate_before'] = energy
    fixed = _fixed_ids(placement, comps)
    reason = 'round_limit'

    def static_safe(trial):
        result = _static_faults(par, trial, nets, index, options, stop)
        return not result['count'] and not result['hard']

    def reject(kind):
        report['rejections'][kind] = report['rejections'].get(kind, 0)+1

    try:
        if not static_safe(current):
            return finish('unsafe_initial_geometry')
        for _ in range(4):
            before = measure_series(current, index, relations)
            incumbent = None
            interrupted = None
            for k, moved, cells in _proposals(current, comps, index, relations, fixed):
                if perf_counter() >= stop:
                    interrupted = 'time_budget'; break
                if report['trials'] >= max_trials:
                    interrupted = 'trial_limit'; break
                report['trials'] += 1
                if len(set(cells)) != len(cells) or any(not 0 <= c < current.cols for r, c in cells):
                    reject('occupied_or_boundary'); continue
                trial = deepcopy(current)
                trial.set_state(cells, current.orient)
                if not _valid_trial(trial, current, moved, fixed):
                    reject('state_or_structure'); continue
                after = measure_series(trial, index, relations)
                if (after['relations'][k]['horizontal_error'] >= before['relations'][k]['horizontal_error']-EPS
                        or any(b['horizontal_error'] > a['horizontal_error']+EPS
                               for a, b in zip(before['relations'], after['relations']))):
                    reject('alignment_regression'); continue
                candidate_energy, _ = _estimate(trial, nets, index, alignment, pairs)
                if candidate_energy >= energy-EPS:
                    reject('estimated_quality'); continue
                rank = (after['horizontal_error'], candidate_energy,
                        sum(abs(cells[i][1]-current.cell_of[i][1]) for i in moved), tuple(cells))
                if incumbent is not None and rank >= incumbent[0]:
                    continue
                try:
                    safe = static_safe(trial)
                except RoutingFailure as exc:
                    if exc.diagnostics.get('reason') != 'quality_time_budget':
                        raise
                    interrupted = 'time_budget'; break
                if not safe:
                    reject('static_geometry'); continue
                incumbent = rank, trial, candidate_energy, before, after
            if incumbent is not None:
                _, trial, new_energy, before, after = incumbent
                changes = [{'device': comps[i]['id'], 'before': list(current.cell_of[i]),
                            'after': list(trial.cell_of[i])} for i in range(len(comps))
                           if current.cell_of[i] != trial.cell_of[i]]
                report['actions'].append({'changes': changes,
                    'horizontal_error_before': before['horizontal_error'],
                    'horizontal_error_after': after['horizontal_error'],
                    'estimate_before': energy, 'estimate_after': new_energy})
                current, energy = trial, new_energy
                report['applied'] = True
            if interrupted:
                reason = interrupted; break
            if incumbent is None:
                reason = 'no_accepted_improvement'; break
            if not measure_series(current, index, relations)['misaligned']:
                reason = 'aligned'; break
    except RoutingFailure as exc:
        if exc.diagnostics.get('reason') != 'quality_time_budget':
            raise
        reason = 'time_budget'
    report['estimate_after'] = energy
    return finish(reason)
