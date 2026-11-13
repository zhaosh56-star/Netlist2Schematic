"""Finite, fixed-axis proposals near one real attachment junction.

No layout mode is enabled here. The owning engine provides the common budget,
acceptance score and static validation; these candidates only move their guest
members into existing vacant cells and orient the original visible terminals.
"""
from __future__ import annotations

from copy import deepcopy
from math import isfinite
from statistics import median
from time import perf_counter

PASSIVE = frozenset(('resistor', 'capacitor', 'inductor', 'diode'))
DIRECTIONS = {'north': (0, -1), 'south': (0, 1), 'west': (-1, 0), 'east': (1, 0)}
EPS = 1e-8


def _fixed(pl, index):
    return {index.get(name, name) for name in set(getattr(pl, 'fixed', ()))
            | set(getattr(pl, 'fixed_cells', {}))}


def _junction(pl, comps, index, plan, group, net):
    readable = plan.get('readability_plan', plan.get('inner_readability_plan', {}))
    junction = readable.get('junctions', {}).get(net, {})
    ports = junction.get('conduction_ports') or junction.get('control_ports')
    if not ports:
        ports = [port for port in group.get('host_ports', ())
                 if port[0] in index and comps[index[port[0]]].get('nodes', {}).get(port[1]) == net]
    points, facing = [], []
    try:
        for name, pin in ports:
            i = index[name]
            if comps[i]['nodes'][pin] != net:
                return None
            point = pl.pin_world(i, pin)
            if not all(isfinite(v) for v in point):
                return None
            points.append(point); facing.append(pl.pin_dir(i, pin))
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    if not points:
        return None
    return ((median(p[0] for p in points), median(p[1] for p in points)),
            facing[0] if len(set(facing)) == 1 else None,
            all(pin in ('G', 'B', 'IN+', 'IN-') for _, pin in ports))


def _directions(comps, ids, owner, preferred):
    from netlist2schematic.netlist.netlist_core import SRC_ALIASES, SNK_ALIASES
    other = {net for i in ids for net in comps[i]['nodes'].values() if net != owner}
    rail = None
    if len(other) == 1:
        name = str(next(iter(other))).casefold()
        rail = 'north' if name in SRC_ALIASES else 'south' if name in SNK_ALIASES else None
    first = rail or preferred
    return sorted(DIRECTIONS, key=lambda d: (d != first, d != preferred, d))


def _outside_hint(pl, comps, index, plan, group, owner, point):
    """A proven bridge supplies the inside direction; its parent box does not."""
    readable = plan.get('readability_plan', plan.get('inner_readability_plan', {}))
    peers = []
    for pattern in readable.get('patterns', ()):
        ends = pattern.get('owner_junctions', ())
        if len(ends) != 2 or owner not in ends:
            continue
        other = next(net for net in ends if net != owner)
        anchor = _junction(pl, comps, index, plan, pattern, other)
        if anchor is not None:
            peers.append(anchor[0])
    return ((point[0]-median(p[0] for p in peers), point[1]-median(p[1] for p in peers))
            if peers else (0.0, 0.0))


def _orient_local(trial, comps, index, plan, group, owner, point, direction, deadline):
    from netlist2schematic.plugins.attachment.attachment_integration import _group_paths, _orient_path
    dx, dy = DIRECTIONS[direction]
    ids = {index[name] for name in group['members']}
    by_net = {}
    for i in ids:
        for net in comps[i]['nodes'].values():
            by_net.setdefault(net, []).append(i)
    # A two-sided known path may use the engine's shared direction oracle.
    # One-ended paths need an explicit exterior target because their other
    # endpoint is a rail marker or a boundary, with no active junction point.
    for path in _group_paths(group):
        _orient_path(trial, comps, index, plan, path,
                     axis=0 if dx else 1)
    for name in group['members']:
        if deadline is not None and perf_counter() >= deadline:
            return False
        i = index[name]
        center = trial.pos_of(i)
        targets = {}
        for pin, net in comps[i]['nodes'].items():
            if net == owner:
                targets[pin] = point, (-dx, -dy)
            elif group['kind'] == 'parallel_bank':
                targets[pin] = (center[0]+100*dx, center[1]+100*dy), (dx, dy)
            else:
                neighbours = [j for j in by_net[net] if j != i]
                if len(neighbours) == 1:
                    target = trial.pos_of(neighbours[0])
                    sign = 1 if (target[0]-center[0])*dx+(target[1]-center[1])*dy > 0 else -1
                    targets[pin] = target, (sign*dx, sign*dy)
                elif not neighbours:
                    targets[pin] = (center[0]+100*dx, center[1]+100*dy), (dx, dy)
                else:
                    return False
        if len(targets) != 2:
            return False
        choices = []
        old = trial.orient[i]
        for orientation in trial.allowed_orientations(i):
            trial.orient[i] = orientation
            bad, distance = 0, 0.0
            for pin, (target, vector) in targets.items():
                bad += DIRECTIONS.get(trial.pin_dir(i, pin)) != vector
                position = trial.pin_world(i, pin)
                distance += abs(position[0]-target[0])+abs(position[1]-target[1])
            choices.append(((bad, distance, orientation != old, orientation), orientation))
        if not choices:
            return False
        best, orientation = min(choices)
        if best[0]:
            return False
        trial.orient[i] = orientation
    return True


def enum_local_attachment_trials(pl, comps, index, plan, group, deadline=None):
    """Yield ``(clone, action)``; never modify axes, fixed policy, hosts or RNG.

    Only one-owner series chains, exact parallel banks and single attachments
    qualify. Existing interior contracts remain the engine's responsibility.
    At most forty ray/lateral templates are visited, with no independent budget.
    """
    from netlist2schematic.plugins.attachment.attachment_integration import _group_paths
    if group.get('kind') not in ('series_chain', 'parallel_bank', 'single_attachment'):
        return
    if not pl.row_y or not pl.column_x:
        return
    owners = group.get('owner_junctions', ())
    names = group.get('members', ())
    if len(owners) != 1 or not names or any(name not in index for name in names):
        return
    ids = [index[name] for name in names]
    if (len(set(ids)) != len(ids) or _fixed(pl, index).intersection(ids)
            or any(comps[i]['symbol'] not in PASSIVE for i in ids)):
        return
    owner = owners[0]
    anchor = _junction(pl, comps, index, plan, group, owner)
    if anchor is None:
        return
    point, preferred, control_only = anchor
    parallel = group['kind'] == 'parallel_bank'
    if parallel:
        endpoints = [set(comps[i]['nodes'].values()) for i in ids]
        if any(len(ends) != 2 or owner not in ends or ends != endpoints[0] for ends in endpoints):
            return
        ordered = list(names)
    else:
        paths = _group_paths(group)
        if len(paths) != 1:
            return
        path = paths[0]
        if len(path.get('path', ())) != len(names)+1 or set(path['members']) != set(names):
            return
        if path['path'][0] == owner:
            ordered = list(path['members'])
        elif path['path'][-1] == owner:
            ordered = list(reversed(path['members']))
        else:
            return
        for step, name in enumerate(path['members']):
            if set(comps[index[name]]['nodes'].values()) != set(path['path'][step:step+2]):
                return
    origin_r = min(range(len(pl.row_y)), key=lambda r: abs(pl.row_y[r]-point[1]))
    origin_c = min(range(len(pl.column_x)), key=lambda c: abs(pl.column_x[c]-point[0]))
    outside = _outside_hint(pl, comps, index, plan, group, owner, point)
    seen = set()
    for direction in _directions(comps, ids, owner, preferred):
        dx, dy = DIRECTIONS[direction]
        side = -dy*outside[0]+dx*outside[1]
        sign = 1 if side > EPS else -1 if side < -EPS else 0
        laterals = (sign, 2*sign, 0, -sign, -2*sign) if sign else (0, -1, 1, -2, 2)
        for gap in (1, 2):
            for lateral in laterals:
                if deadline is not None and perf_counter() >= deadline:
                    return
                row, col = origin_r+dy*gap+dx*lateral, origin_c+dx*gap-dy*lateral
                if parallel:
                    start = -(len(ordered)//2)
                    targets = [(row+dx*(start+step), col-dy*(start+step)) for step in range(len(ordered))]
                else:
                    targets = [(row+dy*step, col+dx*step) for step in range(len(ordered))]
                if any(not 0 <= r < len(pl.row_y) or not 0 <= c < len(pl.column_x)
                       or pl.comp_at.get((r, c)) not in (None, *ids) for r, c in targets):
                    continue
                if any((pl.column_x[c]-point[0])*dx+(pl.row_y[r]-point[1])*dy <= EPS for r, c in targets):
                    continue
                if control_only and preferred in DIRECTIONS:
                    gx, gy = DIRECTIONS[preferred]
                    if any((pl.column_x[c]-point[0])*gx+(pl.row_y[r]-point[1])*gy <= EPS for r, c in targets):
                        continue
                trial = deepcopy(pl)
                cells = list(pl.cell_of)
                for name, target in zip(ordered, targets):
                    cells[index[name]] = target
                trial.set_state(cells, trial.orient)
                if hasattr(trial, 'offset_x'):
                    trial.offset_x = list(trial.offset_x)
                    for name, target in zip(ordered, targets):
                        if pl.cell_of[index[name]] != target:
                            trial.offset_x[index[name]] = 0.0
                if not _orient_local(trial, comps, index, plan, group, owner, point, direction, deadline):
                    continue
                key = tuple(trial.cell_of[i] for i in ids), tuple(trial.orient[i] for i in ids)
                if key in seen or all(trial.cell_of[i] == pl.cell_of[i] and trial.orient[i] == pl.orient[i] for i in ids):
                    continue
                seen.add(key)
                yield trial, dict(kind='local_junction_attachment', group=group['id'],
                    members=list(names), owner_junction=owner, direction=direction,
                    gap=gap, lateral=lateral, axes_changed=False)
