"""Clear supply exits and managed-source marker collisions before structure seeds.

This is a bounded orientation transaction, not another placement or routing
pass. It only consumes the caller's existing proposal/deadline ledger. Safe
seeds retain their state and random stream without full static validation.
"""
from __future__ import annotations

from copy import deepcopy
from heapq import heappop, heappush
from time import perf_counter

from netlist2schematic.routing.routing_rules import EPS, build_scene, segment_hits_rect

VERSION = 'initial-rail-clearance-v2'
ROTATABLE = frozenset(('resistor', 'capacitor', 'inductor', 'diode',
                      'voltage-source', 'current-source'))
MAX_TRIALS = 8


def inward_pins(par, pl, comps):
    """Use the same real pin direction and default stub as build_scene()."""
    bad = []
    for i, comp in enumerate(comps):
        for pin, net in comp['nodes'].items():
            symbol = par.power_marker_sym(net)
            if not symbol:
                continue
            dy = -par.STUB_LEN if symbol == 'vdd-port' else par.STUB_LEN
            direction = par.DIR_VEC[pl.pin_dir(i, pin)]
            if dy * direction[1] < -EPS:
                bad.append(dict(component=i, device=comp['id'], pin=pin, net=net,
                                direction=list(direction)))
    return bad


def _orientation_order(par, comp, orientation):
    pins = par.GEO_DOC[comp['symbol']]['orientations'][orientation]['pins']
    directions = {p['name']: par.DIR_VEC[p['direction']] for p in pins}
    powered = [(pin, net) for pin, net in comp['nodes'].items() if par.power_marker_sym(net)]
    # Prefer a genuinely outward exit over a perpendicular one, then the
    # existing geometry-table order. No functional identity or name is used.
    return (sum(directions[pin] != (0, -1 if par.power_marker_sym(net) == 'vdd-port' else 1)
                for pin, net in powered), orientation)


def source_marker_conflicts(par, pl, comps, nets, index, options, until):
    """Query actual foreign marker envelopes, even on the same supply net.

    Only already-managed V/I may obtain an orientation trial.  Own-marker
    access remains the original supply contract; neither that contract nor
    other devices' markers are moved here.
    """
    managed = {i for i in getattr(pl, 'attachment_source_ids', ())
               if comps[i]['symbol'] in ('voltage-source', 'current-source')}
    if not managed:
        return []
    scene = build_scene(par, nets, pl, index, options=options, deadline=until)
    bodies = dict(scene.body_rects)
    owners = [index[name] for net, members in nets.items() if par.power_marker_sym(net)
              for name, _ in members]
    faults = []
    for i in sorted(managed, key=lambda j: (*pl.pos_of(j), comps[j]['symbol'])):
        source_pins = [pin for pins in scene.pins.values() for pin in pins if pin.component == i]
        for number, ((net, marker), owner) in enumerate(zip(scene.marker_rects, owners)):
            if perf_counter() >= until:
                from netlist2schematic.routing.routing_rules import check_deadline
                check_deadline(until, 'initial_source_marker_query')
            if owner == i:
                continue
            body = bodies[i]
            overlap = (max(body[0], marker[0]) < min(body[2], marker[2])-EPS
                       and max(body[1], marker[1]) < min(body[3], marker[3])-EPS)
            exits = [pin.pin for pin in source_pins if segment_hits_rect(*pin.stub, marker)]
            if overlap or exits:
                faults.append(dict(component=i, device=comps[i]['id'], marker=number,
                    marker_component=owner, marker_device=comps[owner]['id'], net=net,
                    body_overlap=overlap, signal_exit_pins=exits))
    return faults


def _combinations(choices, until):
    """Yield nearest orientation combinations without materializing a product."""
    start = (0,) * len(choices)
    heap, seen = [(0, start)], {start}
    while heap and perf_counter() < until:
        _, ranks = heappop(heap)
        yield tuple(choices[i][k] for i, k in enumerate(ranks))
        for i, k in enumerate(ranks):
            if perf_counter() >= until:
                return
            if k + 1 >= len(choices[i]):
                continue
            changed = ranks[:i] + (k + 1,) + ranks[i + 1:]
            if changed not in seen:
                seen.add(changed)
                heappush(heap, (sum(changed), changed))


def normalize_initial_rails(par, current, comps, nets, index, budget, until, *, options=None):
    """Return a fully static-safe orientation repair or the original seed.

    Joint trials can repair several reversed exits or source/marker conflicts.
    Fixed devices,
    custom supply connections, active-device orientations and all other
    placement coordinates are preserved. Partial repairs never escape.
    """
    from netlist2schematic.plugins.attachment import attachment_integration as ai
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl

    detail = dict(version=VERSION, applied=False, trials=0, actions=[],
                  state_rejections=0, hpwl_rejections=0, geometry_rejections=0,
                  max_trials=MAX_TRIALS, shared_budget_reset=False,
                  execution_stage='before_input_folded_and_conduction_initialization')
    if not ai._has_budget(budget, until):
        detail['stop_reason'] = 'shared_budget'
        return current, detail
    if getattr(current, 'power_connections', None) is not None:
        detail['stop_reason'] = 'custom_power_connections'
        return current, detail
    faults = inward_pins(par, current, comps)
    detail['inward_pins_before'] = faults
    from netlist2schematic.routing.orthogonal_router import RoutingFailure
    try:
        marker_faults = source_marker_conflicts(par, current, comps, nets, index, options, until)
    except RoutingFailure:
        detail['stop_reason'] = 'time_budget'
        return current, detail
    detail['source_marker_conflicts_before'] = marker_faults
    if not faults and not marker_faults:
        detail['stop_reason'] = 'already_outward'
        return current, detail
    fixed = ai._fixed_ids(current, index)
    inward_ids = {f['component'] for f in faults}
    ids = sorted(inward_ids | {f['component'] for f in marker_faults},
                 key=lambda i: (*current.pos_of(i), comps[i]['symbol']))
    if any(i in fixed or comps[i]['symbol'] not in ROTATABLE for i in ids):
        detail['stop_reason'] = 'fixed_or_active_rail_conflict'
        return current, detail
    if any(comps[i]['symbol'] in ('voltage-source', 'current-source')
           and i not in getattr(current, 'attachment_source_ids', ()) for i in ids):
        detail['stop_reason'] = 'unmanaged_source_rail_conflict'
        return current, detail
    choices = []
    for i in ids:
        allowed = sorted({o for o in current.allowed_orientations(i)
                          if type(o) is int and 0 <= o < current.n_orients(i)
                          and (i not in inward_ids or o != current.orient[i])})
        valid = []
        for orientation in allowed:
            pins = par.GEO_DOC[comps[i]['symbol']]['orientations'][orientation]['pins']
            directions = {p['name']: par.DIR_VEC[p['direction']] for p in pins}
            if all((-par.STUB_LEN if par.power_marker_sym(net) == 'vdd-port' else par.STUB_LEN)
                   * directions[pin][1] >= -EPS
                   for pin, net in comps[i]['nodes'].items() if par.power_marker_sym(net)):
                valid.append(orientation)
        valid.sort(key=lambda o: ((o != current.orient[i]) if i not in inward_ids else False,
                                  _orientation_order(par, comps[i], o)))
        if not valid:
            detail['stop_reason'] = 'no_allowed_outward_orientation'
            return current, detail
        choices.append(valid)
    baseline = _hpwl(par, current, nets, index)
    limit = baseline * 1.15
    detail.update(estimated_hpwl_before=baseline, estimated_hpwl_limit=limit)
    for orientations in _combinations(choices, until):
        if all(orientation == current.orient[i] for i, orientation in zip(ids, orientations)):
            continue
        if detail['trials'] >= MAX_TRIALS or not ai._has_budget(budget, until):
            break
        budget['candidates'] += 1
        detail['trials'] += 1
        trial = deepcopy(current)
        os = list(trial.orient)
        for i, orientation in zip(ids, orientations):
            os[i] = orientation
        trial.set_state(trial.cell_of, os)
        if not trial.valid_state() or not ai._fixed_world_safe(current, trial, index):
            detail['state_rejections'] += 1
            continue
        estimate = _hpwl(par, trial, nets, index)
        if estimate > limit + EPS:
            detail['hpwl_rejections'] += 1
            continue
        safe = ai._checked_static(par, trial, nets, index, options, until)
        if safe is None or perf_counter() >= until:
            detail['stop_reason'] = 'time_budget'
            return current, detail
        if not safe:
            detail['geometry_rejections'] += 1
            continue
        detail.update(applied=True, stop_reason='static_safe', estimated_hpwl_after=estimate,
            static_verification=dict(valid=True, complete=True, scope='fixed_port_body_and_marker_geometry'),
            actions=[dict(kind='initial_outward_rail_orientation' if i in inward_ids else 'initial_source_marker_orientation', device=comps[i]['id'],
                before=current.orient[i], after=trial.orient[i], cells_preserved=True,
                axes_preserved=True, offsets_preserved=True, terminal_connections_preserved=True)
                for i in ids if current.orient[i] != trial.orient[i]])
        return trial, detail
    detail['stop_reason'] = 'time_budget' if perf_counter() >= until else (
        'trial_limit' if detail['trials'] >= MAX_TRIALS else
        'shared_budget' if not ai._has_budget(budget, until) else 'no_static_safe_orientation')
    return current, detail
