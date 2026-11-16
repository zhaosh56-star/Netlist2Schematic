"""Late current-source alignment to a unique, facing real D/C access.

The original junction-distance score and SA source proposals stay unchanged.
This final pass uses only leftover attachment budget, after hosts settle.
"""
from __future__ import annotations

from copy import deepcopy
from math import isfinite
from time import perf_counter

from netlist2schematic.plugins.attachment.attachment_sources import ACTIVE, CONTROL, _fixed
from netlist2schematic.plugins.layout_shared.branch_alignment import _grid_offset, _hpwl

VERSION = 'source-conduction-axis-v1'
EPS = 1e-8
CONDUCTION = {'nmos': 'D', 'pmos': 'D', 'npn': 'C', 'pnp': 'C'}


def conduction_anchor(pl, comps, index, group):
    """One direct conduction host, with only active control fanout elsewhere.

    This selects a drawing access, not an amplifier/output functional label.
    Other sources, passive taps, or multiple conduction ports are ambiguous.
    Recheck current incidence instead of trusting a stale model after edits.
    """
    try:
        name = group['device']
        i = index[name]
        if (group['kind'] != 'neutral_junction_source' or group['members'] != [name]
                or comps[i]['symbol'] != 'current-source'
                or comps[i]['nodes'] != group['source_pins']):
            return None
        net, pin = group['attachment_net'], group['signal_pin']
        if comps[i]['nodes'].get(pin) != net:
            return None
        by_id = {c['id']: c for c in comps}
        ports = [(c['id'], p) for c in comps for p, n in c['nodes'].items()
                 if n == net and (c['id'], p) != (name, pin)]
        hosts = [(n, p) for n, p in ports if by_id[n]['symbol'] in ACTIVE]
        if sorted(hosts) != sorted(tuple(p) for p in group['host_ports']):
            return None
        conducting = [(n, p) for n, p in ports
                      if CONDUCTION.get(by_id[n]['symbol']) == p]
        if len(conducting) != 1:
            return None
        host, host_pin = conducting[0]
        if any((n, p) != (host, host_pin)
               and (by_id[n]['symbol'] not in ACTIVE or p not in CONTROL)
               for n, p in ports):
            return None
        point, target = pl.pin_world(i, pin), pl.pin_world(index[host], host_pin)
        if not all(isfinite(v) for v in (*point, *target)):
            return None
        direction, host_direction = pl.pin_dir(i, pin), pl.pin_dir(index[host], host_pin)
        if (direction, host_direction) not in (('south', 'north'), ('north', 'south')):
            return None
        if (target[1]-point[1]) * (1 if direction == 'south' else -1) <= EPS:
            return None
        return dict(net=net, point=list(target), source=name, source_pin=pin,
                    host=host, host_pin=host_pin,
                    evidence='unique_facing_conduction_host_with_only_control_fanout')
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return None


def axis_trials(pl, comps, index, group, *, deadline=None, grid_step=2):
    """Offer one in-cell horizontal shift; never move a host or a fixed source."""
    if deadline is not None and perf_counter() >= deadline:
        return
    anchor = conduction_anchor(pl, comps, index, group)
    if anchor is None:
        return
    i = index[group['device']]
    if i in _fixed(pl, index):
        return
    point = pl.pin_world(i, group['signal_pin'])
    offsets = list(getattr(pl, 'offset_x', [0.0]*len(comps)))
    before = offsets[i]
    trial = deepcopy(pl)
    trial.offset_x = offsets
    after = _grid_offset(trial, i, before+anchor['point'][0]-point[0], grid_step)
    if after is None or abs(after-before) < EPS:
        return
    trial.offset_x[i] = after
    old_error = abs(point[0]-anchor['point'][0])
    new_error = abs(trial.pin_world(i, group['signal_pin'])[0]-anchor['point'][0])
    if new_error >= old_error-EPS:
        return
    yield trial, dict(kind='source_conduction_axis', version=VERSION,
        source=group['device'], source_kind=group['kind'], members=list(group['members']),
        pin=group['signal_pin'], attachment_net=group['attachment_net'],
        anchor=anchor, dx_before=before, dx_after=after,
        axis_error_before=old_error, axis_error_after=new_error,
        cells_orientations_y_and_axes_preserved=True)


def finish_source_axes(par, pl, comps, nets, index, plan, options, budget, until, report,
                       *, hpwl_limit):
    """Finish after all host/passive moves, within the original 1s/128 ledger."""
    from netlist2schematic.plugins.attachment import attachment_integration as ai

    stage = dict(version=VERSION, trials=0, accepted=0, state_rejections=0,
        geometry_rejections=0, hpwl_rejections=0, actions=[], stop_reason='not_applicable',
        existing_source_distance_definition_unchanged=True,
        budget='remaining_shared_attachment_ledger')
    report['source_conduction_axis'] = stage
    if not plan.get('source_groups'):
        return pl
    current = pl
    if not ai._has_budget(budget, until):
        stage['stop_reason'] = 'shared_budget'
        return current
    # Sort by real geometry. Names only break otherwise identical ties.
    def order(group):
        anchor = conduction_anchor(current, comps, index, group)
        return (anchor is None, tuple(anchor['point']) if anchor else (), group['id'])

    for group in sorted(plan['source_groups'], key=order):
        if not ai._has_budget(budget, until):
            stage['stop_reason'] = 'shared_budget'
            return current
        for trial, action in axis_trials(current, comps, index, group, deadline=until,
                grid_step=options.grid_step if options is not None else 2):
            if not ai._has_budget(budget, until):
                stage['stop_reason'] = 'shared_budget'
                return current
            budget['candidates'] += 1
            report['candidates'] += 1
            stage['trials'] += 1
            if not trial.valid_state():
                stage['state_rejections'] += 1
                continue
            before = ai.measure_attachments(current, comps, index, plan)
            after = ai.measure_attachments(trial, comps, index, plan)
            if (not ai._source_guards_preserved(before, after)
                    or after['source_errors'] > before['source_errors']):
                continue
            old_hpwl = _hpwl(par, current, nets, index)
            new_hpwl = _hpwl(par, trial, nets, index)
            if new_hpwl > min(old_hpwl, hpwl_limit)+EPS:
                stage['hpwl_rejections'] += 1
                continue
            safe = ai._checked_static(par, trial, nets, index, options, until)
            if safe is None or perf_counter() >= until:
                budget['exhausted'] = True
                stage['stop_reason'] = 'shared_budget'
                return current
            if not safe:
                stage['geometry_rejections'] += 1
                continue
            action.update(estimated_hpwl_before=old_hpwl, estimated_hpwl_after=new_hpwl)
            current = trial
            report['accepted'] += 1
            report['actions'].append(action)
            stage['accepted'] += 1
            stage['actions'].append(action)
    stage['stop_reason'] = 'finished' if stage['trials'] else 'no_applicable_shift'
    return current
