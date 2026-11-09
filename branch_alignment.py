"""Bounded physical pin alignment after logical placement, without routing.

The logical grid remains authoritative for ownership and layering.  Optional
``offset_x`` values refine individual origins inside their original column
cells; visible pins, symbols and power markers are subsequently rebuilt by the
ordinary scene builder.  No SA or A* belongs to this module.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from itertools import product
from math import ceil, floor, isfinite
from statistics import median
from time import perf_counter

from orthogonal_router import RoutingFailure
from routing_rules import RoutingOptions
from symmetry_layout import _static_safe

VERSION = 'branch-pin-alignment-v1'
HPWL_RATIO = 1.15
EPS = 1e-8
ACTIVE = frozenset(('nmos', 'pmos', 'npn', 'pnp'))
PASSIVE = frozenset(('resistor', 'capacitor', 'inductor', 'diode'))
CHANNEL = {'nmos': ('D', 'S'), 'pmos': ('D', 'S'),
           'npn': ('C', 'E'), 'pnp': ('C', 'E')}
CONTROL = {'nmos': 'G', 'pmos': 'G', 'npn': 'B', 'pnp': 'B'}


def _point(pl, index, endpoint):
    name, pin = endpoint
    return tuple(pl.pin_world(index[name], pin))


def _vertical(par, pl, index, endpoint):
    return pl.pin_dir(index[endpoint[0]], endpoint[1]) in ('north', 'south')


def _fixed(pl, index):
    return {index[value] if isinstance(value, str) and value in index else value
            for value in set(getattr(pl, 'fixed', ())) | set(getattr(pl, 'fixed_cells', {}))}


def _hpwl(par, pl, nets, index):
    total = 0.0
    for net, members in nets.items():
        if par.power_marker_sym(net) or len(members) < 2:
            continue
        points = [pl.pin_world(index[name], pin) for name, pin in members]
        total += max(x for x, y in points) - min(x for x, y in points)
        total += max(y for x, y in points) - min(y for x, y in points)
    return total


def _bounds(pl, i):
    """Bounds are relative to the unchanged logical column, not the last move."""
    col = pl.cell_of[i][1]
    axis, center = pl.column_x, pl.column_x[col]
    left_pitch = center - axis[col - 1] if col else axis[1] - center if len(axis) > 1 else 150
    right_pitch = axis[col + 1] - center if col + 1 < len(axis) else left_pitch
    return -left_pitch / 2, right_pitch / 2


def _grid_offset(pl, i, desired, step):
    lower, upper = _bounds(pl, i)
    first, last = ceil((lower - EPS) / step), floor((upper + EPS) / step)
    if first > last:
        return None
    candidates = {max(first, min(last, floor(desired / step))),
                  max(first, min(last, ceil(desired / step)))}
    values = [value * step for value in candidates]
    return min(values, key=lambda value: (abs(value - desired),
               abs(value - pl.offset_x[i]), abs(value), value))


def _shift(pl, i, difference, step):
    desired = pl.offset_x[i] + difference
    value = _grid_offset(pl, i, desired, step)
    return value if value is not None else pl.offset_x[i]


def _bridge_relations(par, pl, comps, index, metadata):
    """Validate an explicit unique R/L path against the original visible nets."""
    by_id = {row['id']: row for row in comps}
    incidence = defaultdict(list)
    for row in comps:
        for pin, net in row['nodes'].items():
            incidence[net].append((row['id'], pin))
    plan = metadata.get('branch_row_plan', {})
    supplied = list(plan.get('bridge_alignment', ())) if isinstance(plan, dict) else []
    supplied.extend(metadata.get('branch_measurement_connections', ()))
    accepted, skipped, seen = [], [], set()
    for row in supplied:
        if not isinstance(row, dict):
            skipped.append({'kind': 'rl_bridge_axis', 'reason': 'invalid_bridge_record'})
            continue
        members = row.get('members', ())
        problem = None
        if (not isinstance(members, (list, tuple)) or not members or len(set(members)) != len(members)
                or any(name not in by_id or by_id[name]['symbol'] not in ('resistor', 'inductor')
                       or len(by_id[name]['nodes']) != 2 for name in members)):
            problem = 'invalid_rl_members'
        upper, lower = row.get('upper'), row.get('lower')
        upper_pin, lower_pin = row.get('upper_pin'), row.get('lower_pin')
        if not problem and upper is None and lower is None:
            problem = 'bridge_without_active_anchor'
        for name, pin, direction in ((upper, upper_pin, 'south'), (lower, lower_pin, 'north')):
            if name is not None and (name not in by_id or by_id[name]['symbol'] not in CHANNEL
                    or pin not in CHANNEL[by_id[name]['symbol']]
                    or pl.pin_dir(index[name], pin) != direction):
                problem = 'invalid_conduction_anchor'
        path_hint = row.get('path') or ()
        start = row.get('start_net', path_hint[0] if path_hint else None)
        end = row.get('end_net', path_hint[-1] if path_hint else None)
        if not problem:
            start = start if start is not None else by_id[upper]['nodes'][upper_pin] if upper else None
            end = end if end is not None else by_id[lower]['nodes'][lower_pin] if lower else None
            if start is None or end is None or start == end:
                problem = 'invalid_bridge_endpoints'
        member_pins, path, connections = {}, [start], []
        if not problem:
            for name in members:
                nodes = by_id[name]['nodes']
                entry = [pin for pin, net in nodes.items() if net == path[-1]]
                if len(entry) != 1:
                    problem = 'rl_path_not_continuous'
                    break
                exit_pin = next(pin for pin in nodes if pin != entry[0])
                member_pins[name] = {'in': entry[0], 'out': exit_pin}
                path.append(nodes[exit_pin])
            if not problem and (path[-1] != end or len(set(path)) != len(path)
                                or (path_hint and list(path_hint) != path)):
                problem = 'rl_path_not_unique'
        if not problem:
            for endpoint, name, pin in ((start, upper, upper_pin), (end, lower, lower_pin)):
                outside = [item for item in incidence[endpoint] if item[0] not in members]
                if ((name is None and not par.power_marker_sym(endpoint))
                        or (name is not None and (outside != [(name, pin)]
                            or by_id[name]['nodes'][pin] != endpoint))):
                    problem = 'ambiguous_bridge_endpoint'
            for number in range(1, len(path)-1):
                expected = {(members[number-1], member_pins[members[number-1]]['out']),
                            (members[number], member_pins[members[number]]['in'])}
                if par.power_marker_sym(path[number]) or set(incidence[path[number]]) != expected:
                    problem = 'tapped_bridge_internal_net'
            if row.get('member_pins') is not None and row['member_pins'] != member_pins:
                problem = 'incorrect_bridge_pin_roles'
        if not problem:
            if upper is not None:
                connections.append({'kind': 'rl_bridge_axis', 'net': start,
                    'pins': [(upper, upper_pin), (members[0], member_pins[members[0]]['in'])]})
            for number in range(1, len(members)):
                previous, name = members[number-1], members[number]
                connections.append({'kind': 'rl_bridge_axis', 'net': path[number],
                    'pins': [(previous, member_pins[previous]['out']), (name, member_pins[name]['in'])]})
            if lower is not None:
                connections.append({'kind': 'rl_bridge_axis', 'net': end,
                    'pins': [(members[-1], member_pins[members[-1]]['out']), (lower, lower_pin)]})
            def signature(values):
                return sorted((item.get('net'), tuple(sorted(tuple(pin) for pin in item.get('pins', ()))))
                              for item in values)
            if row.get('connections') is not None and signature(row['connections']) != signature(connections):
                problem = 'incorrect_bridge_connections'
        if problem:
            skipped.append({'kind': 'rl_bridge_axis', 'id': row.get('id'), 'reason': problem})
            continue
        identity = tuple(members), tuple(path), upper, upper_pin, lower, lower_pin
        if identity in seen:
            continue
        seen.add(identity)
        accepted.append({'id': row.get('id', 'bridge:'+'|'.join(members)), 'members': list(members),
                         'member_pins': member_pins, 'path': path, 'connections': connections,
                         'upper': upper, 'upper_pin': upper_pin, 'lower': lower, 'lower_pin': lower_pin})
    membership = defaultdict(int)
    for row in accepted:
        for name in row['members']:
            membership[name] += 1
    ambiguous = {name for name, count in membership.items() if count > 1}
    result = []
    for row in accepted:
        if ambiguous.intersection(row['members']):
            skipped.append({'kind': 'rl_bridge_axis', 'id': row['id'], 'reason': 'ambiguous_bridge_membership'})
        else:
            result.append(row)
    return result, skipped


def _model(par, pl, comps, nets, index, metadata):
    """Read semantic roles, then independently check every electrical endpoint."""
    by_id = {row['id']: row for row in comps}
    explicit_controls = 'external_control_ports' in metadata
    values = metadata.get('external_control_ports')
    if values is not None and (not isinstance(values, (list, tuple, set, frozenset))
                               or any(not isinstance(value, str) for value in values)):
        raise ValueError('external_control_ports must contain actual control net names')
    external_controls = set(values or ())

    def formal_control(net):
        members = nets.get(net, ())
        return net in external_controls and bool(members) and all(
            name in by_id and by_id[name]['symbol'] in CONTROL
            and pin == CONTROL[by_id[name]['symbol']] for name, pin in members)
    records = metadata.get('layout_relations', metadata.get('structs', ()))
    records = records if isinstance(records, (tuple, list)) else ()
    paired_core_members = {name for record in records
                           for pair in record.get('layout', {}).get('pairs', ())
                           for name in pair}
    for record in records:
        roles = record.get('roles', {})
        if record.get('kind') == 'current_mirror':
            paired_core_members.update(roles.get('references', [roles.get('reference')]))
            paired_core_members.update(roles.get('outputs', ()))
    axes, tails, inputs, attachments, parallels, skipped = [], [], [], [], [], []
    ordinary_loads = []
    conductors = defaultdict(list)
    for comp in comps:
        for pin in CHANNEL.get(comp['symbol'], ()):
            conductors[comp['nodes'][pin]].append((comp['id'], pin))
    for net, endpoints in sorted(conductors.items()):
        if par.power_marker_sym(net) or len(endpoints) != 2 or endpoints[0][0] == endpoints[1][0]:
            continue
        first, second = endpoints
        a, b = by_id[first[0]], by_id[second[0]]
        output_join = first[1] in ('D', 'C') and second[1] in ('D', 'C') and (
            {a['symbol'], b['symbol']} in ({'nmos', 'pmos'}, {'npn', 'pnp'}))
        series_join = ((first[1] in ('D', 'C') and second[1] in ('S', 'E')) or
                       (second[1] in ('D', 'C') and first[1] in ('S', 'E')))
        if (output_join or series_join) and all(_vertical(par, pl, index, item) for item in endpoints):
            axes.append({'kind': 'conduction_axis', 'net': net, 'pins': [first, second]})
    # Only a unique rail-connected load can share its drain/collector's axis.
    # A parallel load bank instead needs several distinct positions around a
    # common node; forcing every member onto one x would reward vertical piles.
    rail_loads = defaultdict(list)
    for comp in comps:
        if comp['symbol'] not in PASSIVE | {'current-source'}:
            continue
        pins = list(comp['nodes'])
        if len(pins) != 2:
            continue
        rail_pins = [pin for pin in pins if par.power_marker_sym(comp['nodes'][pin])]
        if len(rail_pins) != 1:
            continue
        pin = next(pin for pin in pins if pin not in rail_pins)
        net = comp['nodes'][pin]
        active_ends = conductors.get(net, ())
        if len(active_ends) != 1 or active_ends[0][1] not in ('D', 'C'):
            continue
        rail_loads[net].append({'device': comp['id'], 'pin': pin,
                               'rail': comp['nodes'][rail_pins[0]], 'anchor': active_ends[0]})
    for net, loads in sorted(rail_loads.items()):
        if len(loads) != 1:
            banks = defaultdict(list)
            for load in loads:
                banks[load['rail']].append(load['device'])
                skipped.append({'kind': 'load_axis', 'device': load['device'], 'reason': 'parallel_load_bank'})
            for rail, names in banks.items():
                if len(names) > 1 and all(by_id[name]['symbol'] in PASSIVE for name in names):
                    parallels.append({'devices': sorted(names), 'nets': sorted((net, rail))})
            continue
        item = loads[0]
        load, anchor = (item['device'], item['pin']), item['anchor']
        axes.append({'kind': 'load_axis', 'net': net, 'pins': [anchor, load]})
        ordinary_loads.append({'device': item['device'], 'pins': list(by_id[item['device']]['nodes'])})

    pair_records, tail_owners = [], defaultdict(set)
    for record in records:
        if record.get('kind') not in ('differential_pair', 'bjt_differential_pair'):
            continue
        roles = record.get('roles', {})
        a, b = roles.get('left'), roles.get('right')
        if a not in by_id or b not in by_id or a == b or by_id[a]['symbol'] != by_id[b]['symbol']:
            continue
        symbol = by_id[a]['symbol']
        if symbol not in CHANNEL:
            continue
        drain, source = CHANNEL[symbol]
        common = by_id[a]['nodes'][source]
        if common != by_id[b]['nodes'][source] or par.power_marker_sym(common):
            continue
        source_members = {name for name, pin in conductors.get(common, ())
                          if pin == source and by_id[name]['symbol'] == symbol}
        if source_members != {a, b}:
            continue
        if by_id[a]['nodes'][drain] == by_id[b]['nodes'][drain] or (
                by_id[a]['nodes'][CONTROL[symbol]] == by_id[b]['nodes'][CONTROL[symbol]]):
            continue
        identity = tuple(sorted((a, b))), common
        candidates = roles.get('exclusive_tails', roles.get('tail', ()))
        for name in roles.get('tail', ()):
            tail_owners[name].add(identity)
        pair_records.append((identity, a, b, source, common, candidates))
    seen_pairs, seen_tails = set(), set()
    for identity, a, b, source, common, candidates in pair_records:
        if identity in seen_pairs:
            continue
        seen_pairs.add(identity)
        # Explicit formal ports can fan out to several G/B terminals. Without
        # formal evidence retain the historical single-visible-terminal rule.
        # A common bias or internal feedback network never becomes an input
        # merely because its name resembles a conventional input label.
        control = CONTROL[by_id[a]['symbol']]
        control_nets = [by_id[name]['nodes'][control] for name in (a, b)]
        external = (all(formal_control(net) for net in control_nets) if explicit_controls else
                    all(len(nets.get(net, ())) == 1 for net in control_nets))
        if control_nets[0] != control_nets[1] and external:
            inputs.append({'devices': [a, b], 'control': control,
                           'channel': CHANNEL[by_id[a]['symbol']][0]})
        for name in candidates:
            if name not in by_id or len(tail_owners[name]) != 1 or name in paired_core_members:
                if name in by_id:
                    skipped.append({'kind': 'tail_center', 'device': name,
                                    'reason': 'other_core_member' if name in paired_core_members else 'shared_tail'})
                continue
            comp = by_id[name]
            allowed = (CHANNEL[comp['symbol']][0],) if comp['symbol'] in CHANNEL else (
                ('+', '-') if comp['symbol'] == 'current-source' else ())
            ends = [pin for pin in allowed if comp['nodes'].get(pin) == common]
            if len(ends) != 1 or (name, identity) in seen_tails:
                continue
            seen_tails.add((name, identity))
            tails.append({'kind': 'tail_center', 'pins': [(a, source), (b, source)],
                          'tail': (name, ends[0]), 'net': common})

    def attach(a, b, weight, kind):
        if a[0] in by_id and b[0] in by_id and a[1] in by_id[a[0]]['nodes'] and (
                b[1] in by_id[b[0]]['nodes'] and by_id[a[0]]['nodes'][a[1]] == by_id[b[0]]['nodes'][b[1]]):
            attachments.append({'pins': [a, b], 'weight': weight, 'kind': kind})

    for record in records:
        roles, kind = record.get('roles', {}), record.get('kind')
        if kind == 'series_chain':
            order = roles.get('ordered', ())
            for a, b in zip(order, order[1:]):
                if a not in by_id or b not in by_id:
                    continue
                matches = [(pa, pb) for pa, na in by_id[a]['nodes'].items()
                           for pb, nb in by_id[b]['nodes'].items() if na == nb and not par.power_marker_sym(na)]
                if len(matches) == 1:
                    attach((a, matches[0][0]), (b, matches[0][1]), .5, 'series_attachment')
        elif kind == 'feedback_network':
            amplifier = roles.get('amplifier')
            chain = roles.get('series', ())
            if amplifier not in by_id or not chain:
                continue
            input_pin = 'IN+' if roles.get('input') == 'plus' else 'IN-'
            for name, target in [(chain[0], 'OUT'), (chain[-1], input_pin),
                                 *((name, input_pin) for name in roles.get('shunts', ()))]:
                if name in by_id and target in by_id[amplifier]['nodes']:
                    matches = [pin for pin, net in by_id[name]['nodes'].items()
                               if net == by_id[amplifier]['nodes'][target]]
                    if len(matches) == 1:
                        attach((name, matches[0]), (amplifier, target), .25, 'feedback_attachment')
        elif kind == 'parallel_passives':
            names = roles.get('members', record.get('members', record.get('devices', ())))
            if len(names) < 2 or any(name not in by_id or by_id[name]['symbol'] not in PASSIVE for name in names):
                continue
            common = set(by_id[names[0]]['nodes'].values())
            if len(common) == 2 and all(set(by_id[name]['nodes'].values()) == common for name in names):
                parallels.append({'devices': list(names), 'nets': sorted(common)})
    unique = {}
    for row in attachments:
        key = row['kind'], tuple(sorted(row['pins']))
        unique[key] = row
    parallel_groups = {(tuple(sorted(row['devices'])), tuple(row['nets'])): row for row in parallels}
    bridges, bridge_skipped = _bridge_relations(par, pl, comps, index, metadata)
    skipped.extend(bridge_skipped)
    bridge_members = {name for bridge in bridges for name in bridge['members']}
    axis_keys = {tuple(sorted(row['pins'])) for row in axes}
    for bridge in bridges:
        for connection in bridge['connections']:
            identity = tuple(sorted(connection['pins']))
            if identity not in axis_keys:
                axes.append(connection)
                axis_keys.add(identity)
    return {'axes': axes, 'tails': tails, 'inputs': inputs,
            'attachments': list(unique.values()), 'parallels': list(parallel_groups.values()),
            'bridges': bridges,
            'ordinary_loads': [row for row in ordinary_loads if row['device'] not in bridge_members],
            'skipped': skipped}


def _measure(pl, comps, index, model):
    by_id = {row['id']: row for row in comps}
    axis_errors = [abs(_point(pl, index, row['pins'][0])[0] - _point(pl, index, row['pins'][1])[0])
                   for row in model['axes']]
    bridge_errors = [abs(_point(pl, index, (name, roles['in']))[0]
                         - _point(pl, index, (name, roles['out']))[0])
                     for row in model['bridges'] for name, roles in row['member_pins'].items()]
    axis_errors.extend(bridge_errors)
    ordinary_errors = [abs(_point(pl, index, (row['device'], row['pins'][0]))[0]
                           - _point(pl, index, (row['device'], row['pins'][1]))[0])
                       for row in model['ordinary_loads']]
    axis_errors.extend(ordinary_errors)
    reverse_steps = []
    for row in model['bridges']:
        ordered = [name for name in (row['upper'], *row['members'], row['lower'])
                   if name is not None]
        heights = [pl.pos_of(index[name])[1] for name in ordered]
        reverse_steps.extend(max(0.0, previous-following)
                             for previous, following in zip(heights, heights[1:]))
    bridge_backtracking = sum(reverse_steps)
    tail_errors = [abs(_point(pl, index, row['tail'])[0] - sum(
        _point(pl, index, end)[0] for end in row['pins']) / 2) for row in model['tails']]
    access = 0
    for row in model['inputs']:
        ordered = sorted(row['devices'], key=lambda name: pl.pos_of(index[name])[0])
        for name, desired in zip(ordered, ('west', 'east')):
            access += pl.pin_dir(index[name], row['control']) != desired
    soft = []
    for row in model['attachments']:
        a, b = (_point(pl, index, end) for end in row['pins'])
        soft.append(row['weight'] * (abs(a[0]-b[0])+abs(a[1]-b[1])) / 100)
    for row in model['parallels']:
        points = [[_point(pl, index, (name, next(pin for pin, node in by_id[name]['nodes'].items()
                                               if node == net))) for name in row['devices']]
                  for net in row['nets']]
        alternatives = []
        for coordinate in (0, 1):
            spread = sum(max(p[coordinate] for p in ends)-min(p[coordinate] for p in ends)
                         for ends in points) / 200
            wrong_axis = sum(abs(first[1-coordinate]-second[1-coordinate]) > EPS
                             for first, second in zip(*points)) / len(row['devices'])
            alternatives.append(spread+wrong_axis)
        soft.append(min(alternatives))
    return {'axis_error': sum(axis_errors), 'tail_error': sum(tail_errors),
            'hard_error': sum(axis_errors)+sum(tail_errors)+bridge_backtracking,
            'input_access_errors': int(access),
            'attachment_score': sum(soft)/max(1, len(soft)),
            'axis_relations': len(axis_errors), 'tail_relations': len(tail_errors),
            'input_pairs': len(model['inputs']), 'attachment_relations': len(soft),
            'bridge_relations': len(model['bridges']), 'bridge_channel_error': sum(bridge_errors),
            'ordinary_load_channels': len(ordinary_errors),
            'ordinary_load_channel_error': sum(ordinary_errors),
            'bridge_backtracking': bridge_backtracking,
            'bridge_order_violations': sum(distance > EPS for distance in reverse_steps)}


def measure_branch_layout(par, pl, comps, nets, index, metadata):
    """Read actual visible-pin errors without changing the layout or its report.

    ``branch_measurement_connections`` may carry the same verified bridge hints
    for an untouched reference candidate without claiming it had a row plan.
    An old placement without ``offset_x`` is valid input.
    """
    return _measure(pl, comps, index, _model(par, pl, comps, nets, index, metadata))


def _key(metrics):
    return (round(metrics['hard_error'], 8), metrics['input_access_errors'],
            round(metrics['attachment_score'], 8))


def _proposals(par, pl, comps, index, model, protected, step):
    fixed = _fixed(pl, index)
    for bridge in model['bridges']:
        choices = []
        for name in bridge['members']:
            i, roles = index[name], bridge['member_pins'][name]
            allowed = (pl.orient[i],) if i in fixed else (
                pl.allowed_orientations(i) if hasattr(pl, 'allowed_orientations') else
                range(len(par.GEO_DOC[comps[i]['symbol']]['orientations'])))
            vertical = [value for value in allowed if all(any(
                pin['name'] == roles[role] and pin['direction'] == direction
                for pin in par.GEO_DOC[comps[i]['symbol']]['orientations'][value]['pins'])
                for role, direction in (('in', 'north'), ('out', 'south')))]
            choices.append(sorted(vertical, key=lambda value: (value != pl.orient[i], value)))
        if any(not values for values in choices):
            continue
        anchors = [(bridge[side], bridge[side+'_pin']) for side in ('upper', 'lower')
                   if bridge[side] is not None]
        values = [_point(pl, index, endpoint)[0] for endpoint in anchors]
        center = median(values)
        for orientations in product(*choices):
            for target in sorted(set(values+[center]), key=lambda value: (abs(value-center), value)):
                offsets, selected = list(pl.offset_x), list(pl.orient)
                for name, orientation in zip(bridge['members'], orientations):
                    i = index[name]
                    if i in fixed:
                        continue
                    selected[i] = orientation
                    local = next(pin['at']['x'] for pin in par.GEO_DOC[comps[i]['symbol']]['orientations'][orientation]['pins']
                                 if pin['name'] == bridge['member_pins'][name]['in'])
                    desired = target-pl.column_x[pl.cell_of[i][1]]-local
                    value = _grid_offset(pl, i, desired, step)
                    if value is not None:
                        offsets[i] = value
                yield offsets, selected, {'kind': 'rl_bridge_orientation', 'bridge': bridge['id'],
                                          'devices': bridge['members'], 'target_x': target}
    for row in model['inputs']:
        offsets, orientations = list(pl.offset_x), list(pl.orient)
        for name, direction in zip(sorted(row['devices'], key=lambda name: pl.pos_of(index[name])[0]),
                                   ('west', 'east')):
            i = index[name]
            if i in fixed or name in protected:
                continue
            allowed = pl.allowed_orientations(i) if hasattr(pl, 'allowed_orientations') else (0, 1)
            selected = [orientation for orientation in allowed if orientation in (0, 1) and any(
                pin['name'] == row['control'] and pin['direction'] == direction
                for pin in par.GEO_DOC[comps[i]['symbol']]['orientations'][orientation]['pins'])]
            if not selected:
                continue
            orientation = selected[0]
            current_x = pl.pin_world(i, row['channel'])[0]
            local = next(pin['at']['x'] for pin in par.GEO_DOC[comps[i]['symbol']]['orientations'][orientation]['pins']
                         if pin['name'] == row['channel'])
            desired = current_x - pl.column_x[pl.cell_of[i][1]] - local
            lower, upper = _bounds(pl, i)
            if not lower-EPS <= desired <= upper+EPS:
                continue  # Access alone must not displace an established channel.
            value = _grid_offset(pl, i, desired, step)
            if value is not None:
                offsets[i], orientations[i] = value, orientation
        yield offsets, orientations, {'kind': 'external_input_access', 'devices': row['devices']}

    adjacency, endpoints = defaultdict(set), {}
    for row in model['axes']:
        (a, pa), (b, pb) = row['pins']
        adjacency[a].add(b)
        adjacency[b].add(a)
        endpoints.setdefault(a, pa)
        endpoints.setdefault(b, pb)
    visited = set()
    for start in sorted(adjacency, key=lambda name: (*pl.pos_of(index[name]), name)):
        if start in visited:
            continue
        members, queue = [], [start]
        while queue:
            name = queue.pop()
            if name in visited:
                continue
            visited.add(name)
            members.append(name)
            queue.extend(sorted(adjacency[name]-visited))
        values = [_point(pl, index, (name, endpoints[name]))[0] for name in members]
        center = median(values)
        for target in sorted(set(values+[center]), key=lambda value: (abs(value-center), value)):
            offsets = list(pl.offset_x)
            for name in members:
                i = index[name]
                if i not in fixed:
                    offsets[i] = _shift(pl, i, target-_point(pl, index, (name, endpoints[name]))[0], step)
            yield offsets, list(pl.orient), {'kind': 'branch_axis', 'devices': members, 'target_x': target}
    for row in model['tails']:
        name, pin = row['tail']
        if index[name] in fixed:
            continue
        target = sum(_point(pl, index, end)[0] for end in row['pins'])/2
        offsets = list(pl.offset_x)
        offsets[index[name]] = _shift(pl, index[name], target-_point(pl, index, (name, pin))[0], step)
        yield offsets, list(pl.orient), {'kind': 'tail_center', 'device': name,
                                        'pin': pin, 'net': row['net'], 'target_x': target}
    for row in model['attachments']:
        for first, second in (row['pins'], list(reversed(row['pins']))):
            name = first[0]
            if comps[index[name]]['symbol'] not in PASSIVE or index[name] in fixed:
                continue
            offsets = list(pl.offset_x)
            offsets[index[name]] = _shift(pl, index[name],
                _point(pl, index, second)[0]-_point(pl, index, first)[0], step)
            yield offsets, list(pl.orient), {'kind': row['kind'], 'device': name, 'anchor': list(second)}
    by_id = {row['id']: row for row in comps}
    for row in model['parallels']:
        endpoints = {name: [(name, next(pin for pin, net in by_id[name]['nodes'].items()
                                        if net == target)) for target in row['nets']]
                     for name in row['devices']}
        if not all(abs(_point(pl, index, ends[0])[1]-_point(pl, index, ends[1])[1]) <= EPS
                   for ends in endpoints.values()):
            continue  # A vertical bank needs y/orientation changes, outside this pass.
        centers = [median(_point(pl, index, endpoints[name][side])[0] for name in row['devices'])
                   for side in (0, 1)]
        offsets = list(pl.offset_x)
        for name in row['devices']:
            if index[name] not in fixed:
                difference = sum(centers[side]-_point(pl, index, endpoints[name][side])[0]
                                 for side in (0, 1))/2
                offsets[index[name]] = _shift(pl, index[name], difference, step)
        yield offsets, list(pl.orient), {'kind': 'parallel_attachment', 'devices': row['devices']}


def _valid_motion(original, trial, comps, index, protected, step, bridge_members=None):
    if trial.cell_of != original.cell_of or trial.column_x != original.column_x or trial.row_y != original.row_y:
        return False
    if len(trial.offset_x) != len(comps) or any(type(v) not in (int, float) or not isfinite(v) for v in trial.offset_x):
        return False
    fixed = _fixed(original, index)
    for i, comp in enumerate(comps):
        lower, upper = _bounds(original, i)
        if not lower-EPS <= trial.offset_x[i] <= upper+EPS:
            return False
        if abs(trial.offset_x[i]/step-round(trial.offset_x[i]/step)) > EPS:
            return False
        if i in fixed and (trial.offset_x[i] != original.offset_x[i] or trial.orient[i] != original.orient[i]):
            return False
        if comp['id'] in protected and trial.orient[i] != original.orient[i]:
            return False
        if trial.orient[i] != original.orient[i]:
            bridge = (bridge_members or {}).get(comp['id'])
            legal_bridge = bridge is not None and comp['symbol'] in ('resistor', 'inductor') and (
                trial.pin_dir(i, bridge['in']) == 'north' and trial.pin_dir(i, bridge['out']) == 'south')
            if not legal_bridge and (comp['symbol'] not in ACTIVE or trial.orient[i] not in (0, 1)):
                return False
        if hasattr(trial, 'allowed_orientations') and trial.orient[i] not in trial.allowed_orientations(i):
            return False
    return True


def regularize_branch_layout(par, pl, comps, nets, index, metadata, options=None,
                             deadline=None, seconds=1, max_candidates=128):
    """Return a copied, statically verified incumbent and JSON-safe diagnostics."""
    if type(seconds) not in (int, float) or not isfinite(seconds) or seconds < 0:
        raise ValueError('branch alignment seconds must be finite and non-negative')
    if type(max_candidates) is not int or max_candidates < 0:
        raise ValueError('branch alignment max_candidates must be a non-negative integer')
    options = options or RoutingOptions()
    if not isinstance(options, RoutingOptions):
        raise ValueError('branch alignment options must be RoutingOptions')
    started = perf_counter()
    until = min(started+seconds, deadline) if deadline is not None else started+seconds
    placement, metadata = deepcopy(pl), deepcopy(metadata)
    report = {'version': VERSION, 'status': 'unchanged', 'actions': [], 'candidates': 0,
              'accepted': 0, 'static_rejections': 0, 'hpwl_rejections': 0,
              'budget_seconds': seconds, 'max_candidates': max_candidates,
              'coordinate_mode': 'horizontal_offsets_only', 'logical_cells_preserved': True,
              'physical_axes_preserved': True, 'metrics_stage': 'unrouted_estimate',
              'estimated_hpwl_ratio_limit': HPWL_RATIO}
    if not all(hasattr(placement, attr) for attr in ('column_x', 'row_y', 'cell_of', 'orient')):
        report.update(status='skipped', stop_reason='unsupported_placement')
    elif getattr(placement, 'power_connections', None) is not None:
        report.update(status='skipped', stop_reason='custom_power_connections')
    else:
        if not hasattr(placement, 'offset_x'):
            placement.offset_x = [0.0]*len(comps)
        if len(placement.offset_x) != len(comps) or any(
                type(v) not in (int, float) or not isfinite(v) for v in placement.offset_x):
            raise ValueError('branch alignment offset_x must contain one finite number per component')
        placement.offset_x = list(placement.offset_x)
        original = deepcopy(placement)
        protected = {row['device'] for row in metadata.get('role_adjustments', ())
                     if isinstance(row, dict) and row.get('device') in index}
        model = _model(par, placement, comps, nets, index, metadata)
        bridge_members = {name: roles for bridge in model['bridges']
                          for name, roles in bridge['member_pins'].items()}
        current = _measure(placement, comps, index, model)
        baseline_hpwl = _hpwl(par, placement, nets, index)
        report.update(before=deepcopy(current), after=deepcopy(current), current=deepcopy(current),
                      estimated_hpwl_before=baseline_hpwl, relations=deepcopy(model),
                      estimated_hpwl_limit=baseline_hpwl*HPWL_RATIO)
        seen = {(tuple(placement.offset_x), tuple(placement.orient))}
        stop = 'no_improvement'
        while True:
            improved = False
            for offsets, orientations, action in _proposals(par, placement, comps, index, model,
                                                            protected, options.grid_step):
                if perf_counter() >= until:
                    stop = 'time_budget'
                    break
                if report['candidates'] >= max_candidates:
                    stop = 'candidate_limit'
                    break
                identity = tuple(offsets), tuple(orientations)
                if identity in seen:
                    continue
                seen.add(identity)
                report['candidates'] += 1
                trial = deepcopy(placement)
                trial.offset_x, trial.orient = list(offsets), list(orientations)
                if not _valid_motion(original, trial, comps, index, protected, options.grid_step, bridge_members):
                    continue
                measured = _measure(trial, comps, index, model)
                if _key(measured) >= _key(current):
                    continue
                if _hpwl(par, trial, nets, index) > baseline_hpwl*HPWL_RATIO+EPS:
                    report['hpwl_rejections'] += 1
                    continue
                try:
                    safe = _static_safe(par, trial, nets, index, options, until)
                except RoutingFailure:
                    if perf_counter() < until:
                        raise
                    stop = 'time_budget'
                    break
                if not safe:
                    report['static_rejections'] += 1
                    continue
                changes = [{'device': comps[i]['id'], 'dx_before': placement.offset_x[i],
                            'dx_after': trial.offset_x[i], 'orientation_before': placement.orient[i],
                            'orientation_after': trial.orient[i]} for i in range(len(comps))
                           if trial.offset_x[i] != placement.offset_x[i] or trial.orient[i] != placement.orient[i]]
                report['actions'].append(dict(action, changes=changes, before=deepcopy(current), after=deepcopy(measured)))
                placement, current = trial, measured
                report['accepted'] += 1
                improved = True
                break  # Rebuild proposals from the accepted state, never a stale group.
            if stop in ('time_budget', 'candidate_limit') or not improved:
                break
        report.update(status='improved' if report['accepted'] else 'unchanged', stop_reason=stop,
                      after=deepcopy(current), current=deepcopy(current),
                      estimated_hpwl_after=_hpwl(par, placement, nets, index),
                      offsets={comp['id']: placement.offset_x[i] for i, comp in enumerate(comps)})
    report['elapsed_seconds'] = perf_counter()-started
    metadata['branch_alignment'] = report
    return placement, metadata
