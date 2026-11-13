"""Failure-only local repair of fixed-port geometry before expensive routing."""
from __future__ import annotations

from copy import deepcopy
from math import isfinite
from time import perf_counter

from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.routing_rules import (EPS, RoutingOptions, analyze_routes, build_scene,
                           check_deadline, segment_hits_rect)
from netlist2schematic.plugins.symmetry.symmetry_layout import _static_safe

VERSION = 'fixed-port-preflight-v2'
PASSIVES = frozenset(('resistor', 'capacitor', 'inductor', 'diode'))
NEIGHBORS = ((0, -1), (0, 1), (-1, 0), (1, 0),
             (-1, -1), (-1, 1), (1, -1), (1, 1))


def _wire_estimate(pl, nets, index, par):
    total = 0.0
    for net, members in nets.items():
        if par.power_marker_sym(net) or len(members) < 2:
            continue
        points = [pl.pin_world(index[name], pin) for name, pin in members]
        total += max(x for x, y in points)-min(x for x, y in points)
        total += max(y for x, y in points)-min(y for x, y in points)
    return total


def _fixed(pl, comps):
    values = set(getattr(pl, 'fixed', ())) | set(getattr(pl, 'fixed_cells', {}))
    return {i for i, comp in enumerate(comps) if i in values or comp['id'] in values}


def _shape(pl):
    rows = len(pl.row_y) if hasattr(pl, 'row_y') else getattr(pl, 'grid_size',
        max(row for row, col in pl.cell_of)+1)
    columns = len(pl.column_x) if hasattr(pl, 'column_x') else getattr(pl, 'cols',
        getattr(pl, 'grid_size', max(col for row, col in pl.cell_of)+1))
    return rows, columns


def _set_cell(trial, i, cell):
    cells = list(trial.cell_of)
    cells[i] = cell
    if hasattr(trial, 'set_state'):
        trial.set_state(cells, trial.orient)
    else:
        trial.cell_of = cells
        trial.comp_at = {position: j for j, position in enumerate(cells)}
    if hasattr(trial, 'row_of'):
        trial.row_of = [position[0] for position in trial.cell_of]
    # A new logical cell uses its own origin. An old column's physical tweak
    # must not carry the passive beyond the new column's local coordinate slot.
    if hasattr(trial, 'offset_x'):
        trial.offset_x[i] = 0.0


def _valid_state(trial, original, comps, fixed, movable):
    movable = {movable} if isinstance(movable, int) else set(movable)
    rows, columns = _shape(trial)
    if len(set(trial.cell_of)) != len(comps):
        return False
    if trial.comp_at != {cell: i for i, cell in enumerate(trial.cell_of)}:
        return False
    if hasattr(trial, 'row_of') and trial.row_of != [r for r, c in trial.cell_of]:
        return False
    for i, comp in enumerate(comps):
        row, column = trial.cell_of[i]
        if not (0 <= row < rows and 0 <= column < columns):
            return False
        if i not in movable or i in fixed or comp['symbol'] not in PASSIVES:
            if trial.cell_of[i] != original.cell_of[i] or trial.orient[i] != original.orient[i] or (
                    trial.pos_of(i) != original.pos_of(i)):
                return False
        if hasattr(trial, 'fixed_rows') and i in trial.fixed_rows and row != trial.fixed_rows[i]:
            return False
        if hasattr(trial, 'allowed_orientations') and trial.orient[i] not in trial.allowed_orientations(i):
            return False
    if hasattr(trial, 'valid_state') and not trial.valid_state():
        return False
    return not hasattr(trial, 'blocks_contiguous') or trial.blocks_contiguous()


def _static_faults(par, pl, nets, index, options, deadline):
    """Count all checks in the static oracle, including inflated shape overlap.

    Shape-hit identities are private and position independent. A partial repair
    must not trade existing faults for a new body or marker obstruction.
    """
    scene = build_scene(par, nets, pl, index, options=options, deadline=deadline)
    faults = list(analyze_routes(list(scene.fixed_segments.items()), scene,
                                deadline=deadline)['violations'])
    hard, added = set(), set()

    def overlaps(a, b):
        return (max(a[0], b[0]) < min(a[2], b[2]) - EPS
                and max(a[1], b[1]) < min(a[3], b[3]) - EPS)

    def extra(identity, kind, **detail):
        hard.add(identity)
        if identity not in added:
            added.add(identity)
            faults.append({'kind': kind, 'point': None, **detail})

    for number, (component, body) in enumerate(scene.body_rects):
        check_deadline(deadline, 'preflight_shapes')
        for other, second in scene.body_rects[number + 1:]:
            if overlaps(body, second):
                extra(('body_overlap', min(component, other), max(component, other)),
                      'body_overlap', components=[component, other])
    marker_components = [index[name] for net, members in nets.items()
                         if par.power_marker_sym(net) for name, pin in members]
    for number, (net, marker) in enumerate(scene.marker_rects):
        check_deadline(deadline, 'preflight_markers')
        owner = marker_components[number] if number < len(marker_components) else None
        for component, body in scene.body_rects:
            if component != owner and overlaps(marker, body):
                extra(('marker_body_halo', number, component), 'marker_body_halo',
                      nets=[net], marker=number, component=component)
    for net, segments in scene.fixed_segments.items():
        check_deadline(deadline, 'preflight_fixed_ports')
        for a, b in segments:
            if net not in scene.power_nets:
                for component, body in scene.body_rects:
                    if not scene.body_allowed(net, a, b, component, body):
                        hard.add(('signal_body', net, component))
                for number, (_, marker) in enumerate(scene.marker_rects):
                    if segment_hits_rect(a, b, marker):
                        hard.add(('signal_marker', net, number))
                continue
            owners = {index[name] for name, pin in nets[net]
                      if tuple(pl.pin_world(index[name], pin)) in (a, b)}
            for component, body in scene.body_rects:
                if component not in owners and segment_hits_rect(a, b, body):
                    extra(('power_body', net, tuple(sorted(owners)), component),
                          'power_body', nets=[net], component=component, segment=(a, b))
            for number, (owner, marker) in enumerate(scene.marker_rects):
                own_entry = (owner == net and number < len(marker_components)
                             and marker_components[number] in owners)
                if not own_entry and segment_hits_rect(a, b, marker):
                    extra(('power_marker', net, tuple(sorted(owners)), number),
                          'power_marker', nets=[net, owner], marker=number, segment=(a, b))
    return {'count': len(faults), 'violations': faults, 'hard': hard}


def repair_static_ports(par, pl, comps, nets, index, options=None, *, deadline=None,
                        seconds=1.0, max_trials=64):
    """Keep safe inputs untouched; otherwise try at most two passive actions.

    Every successful result passes the same complete static oracle as the
    initial scene. One strictly improving partial state may be kept privately
    while fixing a different passive; only a fully safe result is committed.
    Both actions share one deadline/proposal budget and the original HPWL cap.
    """
    if type(seconds) not in (int, float) or not isfinite(seconds) or seconds < 0:
        raise ValueError('preflight seconds must be finite and non-negative')
    if type(max_trials) is not int or max_trials < 0:
        raise ValueError('preflight max_trials must be a non-negative integer')
    if deadline is not None and (type(deadline) not in (int, float) or not isfinite(deadline)):
        raise ValueError('preflight deadline must be finite')
    options = options if options is not None else RoutingOptions()
    if not isinstance(options, RoutingOptions):
        raise ValueError('preflight options must be RoutingOptions')
    started = perf_counter()
    stop = min(deadline, started+seconds) if deadline is not None else started+seconds
    report = {'version': VERSION, 'applied': False, 'trials': 0, 'proposals': 0,
              'actions': [], 'budget_seconds': seconds, 'max_trials': max_trials,
              'hpwl_ratio_limit': 1.15, 'state_rejections': 0, 'hpwl_rejections': 0,
              'partial_improvements': 0, 'partial_shape_rejections': 0,
              'stop_reason': 'already_safe'}

    def finish(result, reason):
        report.update(stop_reason=reason, elapsed_seconds=perf_counter()-started)
        return result, report

    try:
        if seconds == 0:
            return finish(pl, 'zero_budget')
        if perf_counter() >= stop:
            return finish(pl, 'time_budget')
        if _static_safe(par, pl, nets, index, options, stop):
            return finish(pl, 'already_safe')
        report['original_static_invalid'] = True
        if getattr(pl, 'power_connections', None) is not None:
            return finish(pl, 'custom_power_connections')
        if max_trials == 0:
            return finish(pl, 'trial_limit')
        original_faults = _static_faults(par, pl, nets, index, options, stop)
        report['original_violations'] = original_faults['violations'][:16]
        report['original_static_fault_count'] = original_faults['count']
        baseline = _wire_estimate(pl, nets, index, par)
        report.update(estimated_hpwl_before=baseline, estimated_hpwl_limit=baseline*1.15)
        fixed = _fixed(pl, comps)
        rows, columns = _shape(pl)
        work, faults, actions, touched = pl, original_faults, [], set()
        for step in range(2):
            partial = False
            candidates = [i for i, comp in enumerate(comps)
                          if comp['symbol'] in PASSIVES and i not in fixed | touched]
            candidates.sort(key=lambda i: (*work.pos_of(i), comps[i]['symbol'], comps[i]['id']))
            for i in candidates:
                count = len(par.GEO_DOC[comps[i]['symbol']]['orientations'])
                allowed = work.allowed_orientations(i) if hasattr(work, 'allowed_orientations') else range(count)
                alternatives = [('orientation', value) for value in sorted(set(allowed))
                                if type(value) is int and 0 <= value < count and value != work.orient[i]]
                row, column = work.cell_of[i]
                alternatives.extend(('cell', (row+dr, column+dc)) for dr, dc in NEIGHBORS)
                for kind, value in alternatives:
                    if perf_counter() >= stop:
                        return finish(pl, 'time_budget')
                    if report['proposals'] >= max_trials:
                        return finish(pl, 'trial_limit')
                    report['proposals'] += 1
                    if kind == 'cell' and (not 0 <= value[0] < rows or not 0 <= value[1] < columns
                                           or value in work.comp_at):
                        continue
                    trial = deepcopy(work)
                    if kind == 'orientation':
                        trial.orient[i] = value
                    else:
                        _set_cell(trial, i, value)
                    if not _valid_state(trial, pl, comps, fixed, touched | {i}):
                        report['state_rejections'] += 1
                        continue
                    estimate = _wire_estimate(trial, nets, index, par)
                    if estimate > baseline*1.15+1e-9:
                        report['hpwl_rejections'] += 1
                        continue
                    report['trials'] += 1
                    safe = _static_safe(par, trial, nets, index, options, stop)
                    if perf_counter() >= stop:
                        return finish(pl, 'time_budget')
                    action = {'device': comps[i]['id'], 'kind': kind,
                        'before': work.orient[i] if kind == 'orientation' else list(work.cell_of[i]),
                        'after': value if kind == 'orientation' else list(value),
                        'position_before': list(work.pos_of(i)), 'position_after': list(trial.pos_of(i))}
                    if safe:
                        report.update(applied=True, estimated_hpwl_after=estimate,
                                      final_static_fault_count=0, actions=actions + [action])
                        return finish(trial, 'repaired')
                    if step == 0:
                        candidate_faults = _static_faults(par, trial, nets, index, options, stop)
                        if not candidate_faults['hard'] <= faults['hard']:
                            report['partial_shape_rejections'] += 1
                            continue
                        if candidate_faults['count'] < faults['count']:
                            work, faults = trial, candidate_faults
                            actions.append(action)
                            touched.add(i)
                            report.update(partial_improvements=1,
                                          best_partial_fault_count=faults['count'])
                            partial = True
                            break
                if partial:
                    break
            if not partial:
                break
        return finish(pl, 'no_safe_local_repair')
    except RoutingFailure as exc:
        if exc.diagnostics.get('reason') != 'quality_time_budget':
            raise
        return finish(pl, 'time_budget')
    except TimeoutError:
        return finish(pl, 'time_budget')


def _repair_static_ports(*args, **kwargs):
    """Compatibility alias for the original private entry point."""
    return repair_static_ports(*args, **kwargs)
