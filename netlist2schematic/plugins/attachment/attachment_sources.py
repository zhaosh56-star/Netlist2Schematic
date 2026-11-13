"""Source guests derived from visible terminals, independent of layout modes.

The model identifies drawing relationships, not circuit function. A shared
signal junction remains one junction; it does not merge its hosts into a block.
Candidate consumers install source freedom once and perform geometry/routing
acceptance. This module never changes fixed-row policy or invokes a router.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from math import isfinite
from statistics import median
from time import perf_counter

VERSION = 'attachment-sources-v1'
SOURCE = frozenset(('current-source', 'voltage-source'))
PASSIVE = frozenset(('resistor', 'capacitor', 'inductor', 'diode'))
ACTIVE = frozenset(('nmos', 'pmos', 'npn', 'pnp', 'opamp'))
CONTROL = frozenset(('G', 'B', 'IN+', 'IN-'))
DIRECTIONS = {'north': (0, -1), 'south': (0, 1), 'west': (-1, 0), 'east': (1, 0)}
EPS = 1e-8


def _rail(net):
    from netlist2schematic.netlist.netlist_core import SRC_ALIASES, SNK_ALIASES
    return str(net).casefold() in SRC_ALIASES | SNK_ALIASES


def _validated(devices, comps, nets, index):
    from netlist2schematic.netlist.netlist_core import to_document
    if len(index) != len(comps) or any(index.get(c['id']) != i for i, c in enumerate(comps)):
        raise ValueError('Source attachment component index is inconsistent')
    expected = to_document(devices)
    projected = {c['id']: (c['symbol'], c.get('nodes')) for c in expected['components']}
    if projected != {c['id']: (c['symbol'], c.get('nodes')) for c in comps}:
        raise ValueError('Source attachments require the original visible terminals')
    incidence = {net: sorted(set(tuple(p) for p in ports)) for net, ports in nets.items() if ports}
    canonical = {net: sorted(set(tuple(p) for p in ports)) for net, ports in expected['nets'].items()}
    if incidence != canonical:
        raise ValueError('Source attachment net incidence is inconsistent')
    return {c['id']: c for c in comps}, incidence


def _control_path(source, signal, by_id, incidence):
    """Walk only untapped degree-two nodes, stopping at the first active node."""
    incoming, current = (source, next(p for p, n in by_id[source]['nodes'].items() if n == signal)), signal
    members, path, seen = [], [signal], set()
    while True:
        ports = [p for p in incidence[current] if p != incoming]
        active = [p for p in ports if by_id[p[0]]['symbol'] in ACTIVE]
        if active:
            if all(pin in CONTROL for _, pin in active):
                return members, path, active, None
            return None, None, None, 'path_reaches_noncontrol_active_junction'
        if len(ports) != 1:
            return None, None, None, 'branching_or_open_passive_path'
        name, entry = ports[0]
        comp = by_id[name]
        if comp['symbol'] not in PASSIVE:
            return None, None, None, 'path_reaches_another_source'
        if name in seen:
            return None, None, None, 'closed_passive_path'
        pins = comp.get('nodes', {})
        if len(pins) != 2 or len(set(pins.values())) != 2:
            return None, None, None, 'degenerate_passive_path'
        exit_pin = next(pin for pin in pins if pin != entry)
        current = pins[exit_pin]
        if _rail(current):
            return None, None, None, 'passive_path_reaches_rail'
        seen.add(name)
        members.append(name)
        path.append(current)
        incoming = (name, exit_pin)


def build_source_groups(devices, comps, nets, index, *, display_rows=None):
    """Return JSON-safe source groups without changing data, rows or geometry.

    Each group path starts at the source rail and ends at its actual host
    junction. Its member order is source, then the unique passive path. Floating
    sources and supply-only sources have no unambiguous local rail/signal split.
    """
    by_id, incidence = _validated(devices, comps, nets, index)
    groups, rejected, ambiguities = [], [], []
    sources = sorted(name for name, c in by_id.items() if c['symbol'] in SOURCE)
    junctions = {}
    for net, ports in sorted(incidence.items()):
        junctions[net] = dict(net=net, ports=[list(p) for p in ports], is_rail=_rail(net),
            active_ports=[list(p) for p in ports if by_id[p[0]]['symbol'] in ACTIVE],
            source_ports=[list(p) for p in ports if by_id[p[0]]['symbol'] in SOURCE])
    for name in sources:
        comp = by_id[name]
        pins = comp['nodes']
        rails = [pin for pin, net in pins.items() if _rail(net)]
        if len(rails) != 1 or set(pins) != {'+', '-'}:
            rejected.append(dict(device=name, reason='source_requires_one_verified_rail'))
            continue
        rail_pin = rails[0]
        signal_pin = '-' if rail_pin == '+' else '+'
        net, rail_net = pins[signal_pin], pins[rail_pin]
        ports = [p for p in incidence[net] if p != (name, signal_pin)]
        hosts = [p for p in ports if by_id[p[0]]['symbol'] in ACTIVE]
        kind, passive, path, role = None, [], [net], 'neutral'
        if (comp['symbol'] == 'current-source' and len(ports) == len(hosts) == 2
                and len({n for n, _ in hosts}) == 2 and all(p in ('S', 'E') for _, p in hosts)
                and len({by_id[n]['symbol'] for n, _ in hosts}) == 1):
            controls = [by_id[n]['nodes'].get('G', by_id[n]['nodes'].get('B')) for n, _ in hosts]
            outputs = [by_id[n]['nodes'].get('D', by_id[n]['nodes'].get('C')) for n, _ in hosts]
            if len(set(controls)) == len(set(outputs)) == 2:
                kind, role = 'direct_tail', 'exclusive_pair_tail'
        if kind is None and (not hosts or all(pin in CONTROL for _, pin in hosts)):
            passive, path, controls, reason = _control_path(name, net, by_id, incidence)
            if reason is None:
                kind, hosts, role = 'source_control_chain', controls, 'control_junction_attachment'
            elif not hosts:
                record = dict(device=name, net=net, reason=reason)
                rejected.append(record)
                if reason in ('branching_or_open_passive_path', 'closed_passive_path'):
                    ambiguities.append(deepcopy(record))
                continue
        if kind is None:
            if not hosts:
                rejected.append(dict(device=name, net=net, reason='no_active_junction_anchor'))
                continue
            kind, passive, path = 'neutral_junction_source', [], [net]
        host_net = path[-1]
        # Every host belongs to this one real junction, even when it has many
        # D/G terminals. Unrelated rail-connected loads never enter this group.
        groups.append(dict(id='source:'+name, kind=kind, members=[name, *passive],
            device=name, source_device=name, source_symbol=comp['symbol'],
            source_pins=deepcopy(pins), signal_pin=signal_pin, rail_pin=rail_pin,
            net=net, rail_net=rail_net, attachment_net=host_net,
            owner_junctions=[host_net], endpoint_junctions=[rail_net, host_net],
            host_ports=[list(p) for p in sorted(hosts)], host_members=sorted({n for n, _ in hosts}),
            passive_members=passive, path=[rail_net, *path], confidence='high',
            electrical_role=role, preserve_polarity=True,
            functional_role_claimed=kind == 'direct_tail'))
    # Two sources cannot independently claim the same passive path. Reject both
    # overlapping transactions rather than picking one by instance name.
    claims = {}
    for group in groups:
        for member in group['passive_members']:
            claims.setdefault(member, []).append(group['id'])
    overlaps = {member for member, owners in claims.items() if len(owners) > 1}
    if overlaps:
        kept = []
        for group in groups:
            if overlaps.intersection(group['passive_members']):
                record = dict(device=group['device'], reason='overlapping_source_passive_paths',
                              members=sorted(overlaps.intersection(group['passive_members'])))
                rejected.append(record); ambiguities.append(deepcopy(record))
            else:
                kept.append(group)
        groups = kept
    if display_rows is None:
        rows = None
    elif isinstance(display_rows, dict):
        rows = {name: int(display_rows[name]) for name in sorted(index)}
    else:
        rows = {name: int(display_rows[i]) for name, i in sorted(index.items())}
    recognized = {g['device'] for g in groups}
    model = dict(version=VERSION, supported=bool(groups), groups=groups, junctions=junctions,
        rejected=rejected, ambiguities=ambiguities, display_rows=rows,
        freedom=dict(source_devices=sorted(recognized),
                     guest_devices=sorted({n for g in groups for n in g['members']})),
        coverage=dict(source_devices=len(sources), recognized_sources=len(recognized),
            unassigned_sources=sorted(set(sources)-recognized), groups=len(groups),
            ambiguity_count=len(ambiguities)))
    identity = dict(model=model, components=sorted(comps, key=lambda c: c['id']))
    model['signature'] = sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return model


def _groups(model):
    return model.get('groups', ()) if isinstance(model, dict) else model


def source_group_anchor(pl, comps, index, group):
    """Return one actual-junction anchor, with no cross-net host union."""
    try:
        points, directions = [], []
        for name, pin in group['host_ports']:
            i = index[name]
            if comps[i]['nodes'][pin] != group['attachment_net']:
                return None
            point = pl.pin_world(i, pin)
            if not all(isfinite(v) for v in point):
                return None
            points.append(tuple(point)); directions.append(pl.pin_dir(i, pin))
        if not points:
            return None
        return dict(net=group['attachment_net'], point=[median(p[0] for p in points), median(p[1] for p in points)],
            host_points=[list(p) for p in points],
            preferred_direction=directions[0] if len(set(directions)) == 1 else None)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError):
        return None


def _pin(comp, net):
    pins = [p for p, value in comp['nodes'].items() if value == net]
    return pins[0] if len(pins) == 1 else None


def source_chain_errors(pl, comps, index, group):
    """Count a source path's bends, reversed order and real exit directions.

    This is separate from source-facing errors so a straight chain cannot bend
    merely because one previously incorrect source-facing direction improves.
    """
    if len(group['members']) < 2:
        return 0
    anchor = source_group_anchor(pl, comps, index, group)
    if anchor is None:
        raise ValueError('source chain has no actual junction anchor')
    points = [pl.pos_of(index[name]) for name in group['members']]
    if any(not all(isfinite(value) for value in point) for point in points):
        raise ValueError('source chain has nonfinite geometry')
    spans = [max(point[k] for point in points)-min(point[k] for point in points) for k in (0, 1)]
    axis = 0 if spans[0] >= spans[1] else 1
    direction = 1 if anchor['point'][axis] >= points[0][axis] else -1
    errors = int(spans[1-axis] > EPS)
    errors += sum(direction*(b[axis]-a[axis]) <= EPS for a, b in zip(points, points[1:]))
    for step, name in enumerate(group['members']):
        i = index[name]
        exit_pin = _pin(comps[i], group['path'][step+1])
        if exit_pin is None:
            raise ValueError('source chain has no unique real exit pin')
        errors += DIRECTIONS.get(pl.pin_dir(i, exit_pin), (0, 0))[axis]*direction <= 0
    return int(errors)


def measure_source_groups(pl, comps, nets, index, model):
    """Pure common-plan measurements in 100px units; missing is never zero."""
    groups = list(_groups(model))
    records, missing = [], []
    for group in groups:
        try:
            anchor = source_group_anchor(pl, comps, index, group)
            if anchor is None:
                raise ValueError('missing_junction_geometry')
            source_id = index[group['device']]
            if comps[source_id]['nodes'] != group['source_pins']:
                raise ValueError('changed_source_poles')
            members, path = group['members'], group['path']
            if len(path) != len(members)+1:
                raise ValueError('inconsistent_source_path')
            links, errors = [], 0
            for step, name in enumerate(members):
                i = index[name]
                exit_pin = _pin(comps[i], path[step+1])
                if exit_pin is None:
                    raise ValueError('missing_path_pin')
                point = pl.pin_world(i, exit_pin)
                if step+1 == len(members):
                    target = anchor['point']
                else:
                    other = index[members[step+1]]
                    entry = _pin(comps[other], path[step+1])
                    if entry is None:
                        raise ValueError('missing_path_pin')
                    target = pl.pin_world(other, entry)
                if not all(isfinite(v) for v in (*point, *target)):
                    raise ValueError('nonfinite_geometry')
                vector = DIRECTIONS.get(pl.pin_dir(i, exit_pin), (0, 0))
                errors += (target[0]-point[0])*vector[0]+(target[1]-point[1])*vector[1] < -EPS
                links.append(dict(device=name, pin=exit_pin, net=path[step+1],
                    point=list(point), target=list(target), distance=(abs(point[0]-target[0])+abs(point[1]-target[1]))/100.0))
            if group['kind'] == 'direct_tail' and anchor['preferred_direction'] in DIRECTIONS:
                vector = DIRECTIONS[anchor['preferred_direction']]
                position = pl.pos_of(source_id)
                errors += (position[0]-anchor['point'][0])*vector[0]+(position[1]-anchor['point'][1])*vector[1] < -EPS
            chain_errors = source_chain_errors(pl, comps, index, group)
            records.append(dict(id=group['id'], kind=group['kind'], source=group['device'],
                attachment_net=group['attachment_net'], raw_source_errors=int(errors),
                chain_errors=chain_errors, source_errors=int(errors)+chain_errors,
                source_distance=sum(link['distance'] for link in links), anchor=anchor, links=links))
        except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
            missing.append(dict(id=group.get('id'), reason=str(exc)))
    measurable = bool(groups) and not missing
    return dict(version=VERSION, source_count=len(groups), measured_sources=len(records),
        measurable=measurable, source_errors=sum(r['source_errors'] for r in records) if measurable else None,
        raw_source_errors=sum(r['raw_source_errors'] for r in records) if measurable else None,
        chain_errors=sum(r['chain_errors'] for r in records) if measurable else None,
        source_distance=sum(r['source_distance'] for r in records)/len(records) if measurable else None,
        records=records, missing=missing, coverage=deepcopy(model.get('coverage')) if isinstance(model, dict) else None)


def _fixed(pl, index):
    return {index.get(v, v) for v in set(getattr(pl, 'fixed', ())) | set(getattr(pl, 'fixed_cells', {}))}


def _specs(pl, comps, index, group, deadline):
    from netlist2schematic.layout.structured_placement import par
    anchor = source_group_anchor(pl, comps, index, group)
    if anchor is None:
        return
    names, path = group['members'], group['path']
    if len(path) != len(names)+1 or any(name not in index for name in names):
        return
    ids = [index[name] for name in names]
    if _fixed(pl, index).intersection(ids):
        return
    if comps[index[group['device']]]['nodes'] != group['source_pins']:
        return
    point = anchor['point']
    cells = sorted(((r, c) for r in range(len(pl.row_y)) for c in range(len(pl.column_x))),
        key=lambda cell: (abs(pl.column_x[cell[1]]-point[0])+abs(pl.row_y[cell[0]]-point[1]), cell))
    directions = sorted(DIRECTIONS, key=lambda d: (d != anchor['preferred_direction'], d))
    for row, col in cells:
        for direction in directions:
            if deadline is not None and perf_counter() >= deadline:
                return
            dx, dy = DIRECTIONS[direction]
            if group['kind'] == 'direct_tail' and anchor['preferred_direction'] in DIRECTIONS and direction != anchor['preferred_direction']:
                continue
            if (pl.column_x[col]-point[0])*dx+(pl.row_y[row]-point[1])*dy <= EPS:
                continue
            targets = [(row+dy*(len(ids)-1-step), col+dx*(len(ids)-1-step)) for step in range(len(ids))]
            if any(not 0 <= r < len(pl.row_y) or not 0 <= c < len(pl.column_x)
                   or pl.comp_at.get((r, c)) not in (None, *ids) for r, c in targets):
                continue
            orientations = []
            for step, i in enumerate(ids):
                before = _pin(comps[i], path[step])
                after = _pin(comps[i], path[step+1])
                choices = []
                for orientation in pl.allowed_orientations(i):
                    geo = par.GEO_DOC[comps[i]['symbol']]['orientations'][orientation]
                    pins = {p['name']: p for p in geo['pins']}
                    if before not in pins or after not in pins:
                        continue
                    if (DIRECTIONS.get(pins[before]['direction']) == (dx, dy)
                            and DIRECTIONS.get(pins[after]['direction']) == (-dx, -dy)):
                        choices.append(orientation)
                if not choices:
                    break
                orientations.append(min(choices, key=lambda o: (o != pl.orient[i], o)))
            if len(orientations) == len(ids):
                yield ids, targets, orientations, direction


def source_group_states(pl, comps, nets, index, model, *, deadline=None, max_candidates=128):
    """Yield cells/orientations/actions without cloning or mutating placement.

    The SA undo contract is exactly cells plus orientations. In particular,
    offsets, row policy and axes stay outside these bounded source proposals.
    """
    if type(max_candidates) is not int or not 0 <= max_candidates <= 128:
        raise ValueError('Source attachment candidates require 0..128 trials')
    groups = list(_groups(model))
    generators = [iter(_specs(pl, comps, index, group, deadline)) for group in groups]
    seen, count = set(), 0
    while generators and count < max_candidates:
        remaining = []
        for group, generator in zip(groups, generators):
            if deadline is not None and perf_counter() >= deadline:
                return
            try:
                ids, targets, orientations, direction = next(generator)
            except StopIteration:
                continue
            remaining.append((group, generator))
            key = tuple(ids), tuple(targets), tuple(orientations)
            if key in seen:
                continue
            seen.add(key)
            if all(pl.cell_of[i] == target and pl.orient[i] == orientation
                   for i, target, orientation in zip(ids, targets, orientations)):
                continue
            cells, orient = list(pl.cell_of), list(pl.orient)
            for i, target, orientation in zip(ids, targets, orientations):
                cells[i], orient[i] = target, orientation
            count += 1
            yield cells, orient, dict(kind='source_group_attachment', group=group['id'],
                source=group['device'], source_kind=group['kind'], members=list(group['members']),
                attachment_net=group['attachment_net'], direction=direction,
                preserve_polarity=True, candidate=count)
            if count >= max_candidates:
                return
        groups = [item[0] for item in remaining]
        generators = [item[1] for item in remaining]


def source_group_trials(pl, comps, nets, index, model, *, deadline=None, max_candidates=128):
    """Yield joint clones for init/refinement and their static transactions.

    Explicit fixed devices block their entire group. Axes, source poles,
    active positions, RNG and fixed_rows remain unchanged. Callers install
    source freedom once, then own budget and geometry/routing acceptance.
    """
    for cells, orient, action in source_group_states(pl, comps, nets, index, model,
            deadline=deadline, max_candidates=max_candidates):
        clone = deepcopy(pl)
        if hasattr(clone, 'offset_x'):
            clone.offset_x = list(clone.offset_x)
            for name in action['members']:
                i = index[name]
                if pl.cell_of[i] != cells[i]:
                    clone.offset_x[i] = 0.0
        clone.set_state(cells, orient)
        yield clone, action
