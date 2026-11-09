"""Conservative display-row planning before structured SA.

This module does not change the electrical netlist or device_leveling defaults.
It adds only unambiguous R/L continuations to the conduction partial order,
accepts compatible corresponding-row hints, and places exclusive downstream
tail sources below their own input pair. No geometry, router, or RNG is used.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from heapq import heapify, heappop, heappush
from statistics import median_low

import device_leveling as dl
from netlist_core import SNK_ALIASES, SRC_ALIASES

VERSION = 'branch-display-rows-v1'
_ACTIVE = frozenset(('MOS', 'BJT'))
_ROW_TYPES = _ACTIVE | {'PASSIVE'}


class _Union:
    def __init__(self, names):
        self.parent = {name: name for name in names}

    def find(self, name):
        parent = self.parent
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def merge(self, names):
        roots = sorted({self.find(name) for name in names})
        if roots:
            for root in roots[1:]:
                self.parent[root] = roots[0]


def _rail(net):
    value = str(net).casefold()
    if value in SRC_ALIASES:
        return 'src'
    if value in SNK_ALIASES:
        return 'sink'
    return None


def _visible_incidence(comps):
    incidence = defaultdict(list)
    for comp in comps:
        for pin, net in comp.get('nodes', {}).items():
            incidence[net].append((comp['id'], pin))
    return incidence


def _endpoint(net, chain, devices, incidence):
    rail = _rail(net)
    if rail:
        return rail, None
    users = [(name, pin) for name, pin in incidence.get(net, ()) if name not in chain]
    if not users:
        return None, 'endpoint_without_conduction'
    directions = set()
    for name, pin in users:
        device = devices[name]
        pins = ('D', 'S') if device['cls'] == 'MOS' else ('C', 'E')
        if device['cls'] not in _ACTIVE or pin not in pins:
            return None, 'endpoint_has_nonconduction_terminal'
        inlet = 'S' if device['cls'] == 'MOS' and device['type'] == 'PMOS' else (
            'E' if device['cls'] == 'BJT' and device['type'] == 'PNP' else pins[0])
        directions.add('in' if pin == inlet else 'out')
    if len(directions) != 1:
        return None, 'ambiguous_endpoint_direction'
    return next(iter(directions)), None


def _rl_chains(devices, comps):
    """Split only at real terminals; a supply rail is always an endpoint."""
    rl = {name: device for name, device in devices.items()
          if device['cls'] == 'PASSIVE' and device['type'] in ('R', 'L')}
    adjacency = defaultdict(list)
    for name, device in rl.items():
        adjacency[device['a']].append((device['b'], name))
        adjacency[device['b']].append((device['a'], name))
    incidence = _visible_incidence(comps)
    internal = {net for net, edges in adjacency.items() if not _rail(net)
                and len(edges) == 2 and len(incidence[net]) == 2
                and all(name in rl for name, _ in incidence[net])}
    visited, accepted, skipped = set(), [], []
    for start in sorted(set(adjacency) - internal):
        for neighbor, first in sorted(adjacency[start]):
            if first in visited:
                continue
            members, path = [first], [start, neighbor]
            visited.add(first)
            current, previous = neighbor, first
            while current in internal:
                choices = [(other, name) for other, name in adjacency[current]
                           if name != previous]
                if len(choices) != 1 or choices[0][1] in visited:
                    break
                other, name = choices[0]
                members.append(name)
                path.append(other)
                visited.add(name)
                current, previous = other, name
            record = {'id': 'rl:' + '|'.join(sorted(members)),
                      'members': members, 'path': path}
            if path[0] == path[-1] or current in internal:
                skipped.append(dict(record, reason='rl_cycle'))
                continue
            first_kind, first_error = _endpoint(path[0], set(members), devices, incidence)
            last_kind, last_error = _endpoint(path[-1], set(members), devices, incidence)
            error = first_error or last_error
            if error:
                skipped.append(dict(record, reason=error))
                continue
            if first_kind in ('out', 'src') and last_kind in ('in', 'sink'):
                pass
            elif last_kind in ('out', 'src') and first_kind in ('in', 'sink'):
                record['members'] = list(reversed(members))
                record['path'] = list(reversed(path))
                first_kind, last_kind = last_kind, first_kind
            else:
                skipped.append(dict(record, reason='ambiguous_bridge_direction'))
                continue
            record.update(start=record['path'][0], end=record['path'][-1],
                          start_role=first_kind, end_role=last_kind)
            accepted.append(record)
    # An isolated closed R/L loop has no starting boundary.
    remaining = set(rl) - visited
    while remaining:
        pending = [min(remaining)]
        component = set()
        while pending:
            name = pending.pop()
            if name in component:
                continue
            component.add(name)
            remaining.discard(name)
            for net in (rl[name]['a'], rl[name]['b']):
                pending.extend(other for _, other in adjacency[net]
                               if other in remaining)
        skipped.append({'id': 'rl:' + '|'.join(sorted(component)),
                        'members': sorted(component), 'reason': 'rl_cycle'})
    return accepted, skipped


def _conduction_orders(devices, bridges):
    """Use original net SCCs; R/L arcs have no additional device-level step."""
    edges = dl.build_active_edges(devices)
    nodes = set(dl.active_terminal_nets(devices))
    nodes.update(net for bridge in bridges for net in (bridge['start'], bridge['end']))
    adjacency = {net: set() for net in nodes}
    for a, b, _ in edges:
        adjacency[a].add(b)
    original = dl.tarjan_scc(nodes, adjacency)
    augmented = {net: set(values) for net, values in adjacency.items()}
    for bridge in bridges:
        augmented[bridge['start']].add(bridge['end'])
    combined = dl.tarjan_scc(nodes, augmented)
    old_in_new = defaultdict(set)
    for net in nodes:
        old_in_new[combined[net]].add(original[net])
    accepted, skipped = [], []
    for bridge in bridges:
        a, b = bridge['start'], bridge['end']
        if original[a] == original[b]:
            skipped.append(dict(bridge, reason='within_original_scc'))
        elif combined[a] == combined[b] and len(old_in_new[combined[a]]) > 1:
            skipped.append(dict(bridge, reason='bridge_would_create_conduction_cycle'))
        else:
            accepted.append(bridge)
    zero = defaultdict(set)
    for bridge in accepted:
        zero[original[bridge['start']]].add(original[bridge['end']])
    active = sorted(name for name, device in devices.items() if device['cls'] in _ACTIVE)
    endpoints = {name: (original[dl.in_terminal(devices[name])],
                        original[dl.out_terminal(devices[name])]) for name in active}
    orders = []
    for upper in active:
        incoming, outgoing = endpoints[upper]
        # Intrinsic SCC edges do not establish a strict vertical step.
        if incoming == outgoing:
            continue
        reached, pending = {outgoing}, [outgoing]
        while pending:
            for child in zero[pending.pop()]:
                if child not in reached:
                    reached.add(child)
                    pending.append(child)
        for lower in active:
            if lower != upper and endpoints[lower][0] in reached:
                orders.append({'upper': upper, 'lower': lower,
                               'reason': 'conduction' if endpoints[lower][0] == outgoing
                               else 'rl_continuation'})
    sccs = []
    for component in sorted(set(original.values())):
        nets = sorted(net for net in nodes if original[net] == component)
        members = sorted(name for name, ends in endpoints.items()
                         if ends[0] == ends[1] == component)
        if members:
            sccs.append({'nets': nets, 'devices': members})
    return orders, accepted, skipped, sccs


def _cyclic_groups(names, orders, union):
    nodes = {union.find(name) for name in names}
    adjacency = {name: set() for name in nodes}
    bad = set()
    for order in orders:
        a, b = union.find(order['upper']), union.find(order['lower'])
        if a == b:
            bad.add(a)
        else:
            adjacency[a].add(b)
    components = dl.tarjan_scc(nodes, adjacency)
    size = defaultdict(int)
    for component in components.values():
        size[component] += 1
    bad.update(name for name in nodes if size[components[name]] > 1)
    return bad


def _same_rows(relations, names, devices, comps_by_id, orders):
    candidates, skipped, seen = [], [], set()
    for relation in relations:
        layout = relation.get('layout') or {}
        for field in ('pairs', 'same_row'):
            for values in layout.get(field, ()):
                members = sorted(set(values)) if isinstance(values, (list, tuple)) else []
                record = {'relation': relation.get('id', relation.get('kind', 'unknown')),
                          'members': members, 'source': 'layout.' + field}
                if len(members) < 2 or any(name not in names for name in members):
                    skipped.append(dict(record, reason='invalid_same_row_members'))
                    continue
                if (any(devices[name]['cls'] not in _ROW_TYPES for name in members)
                        or len({comps_by_id[name]['symbol'] for name in members}) != 1):
                    skipped.append(dict(record, reason='incompatible_same_row_members'))
                    continue
                key = tuple(members)
                if key not in seen:
                    candidates.append(record)
                    seen.add(key)
    # Drop all contributors to a contradictory component together. Choosing the
    # first legal merge would make results depend on IDs and relation order.
    while True:
        union = _Union(names)
        for candidate in candidates:
            union.merge(candidate['members'])
        bad = _cyclic_groups(names, orders, union)
        rejected = [candidate for candidate in candidates
                    if any(union.find(name) in bad for name in candidate['members'])]
        if not rejected:
            return union, candidates, skipped
        skipped.extend(dict(candidate, reason='same_row_conflicts_with_conduction')
                       for candidate in rejected)
        candidates = [candidate for candidate in candidates if candidate not in rejected]


def _tail_orders(relations, devices, names, union, orders, protected_sccs):
    hints, ownership = [], defaultdict(set)
    for relation in relations:
        if relation.get('kind') not in ('differential_pair', 'bjt_differential_pair'):
            continue
        roles = relation.get('roles') or {}
        pair = [roles.get('left'), roles.get('right')]
        for tail in set(roles.get('tail', ())) | set(roles.get('exclusive_tails', ())):
            ownership[tail].add(tuple(sorted(str(name) for name in pair)))
        for tail in roles.get('exclusive_tails', ()):
            record = {'relation': relation.get('id', relation.get('kind')),
                      'pair': pair, 'tail': tail}
            hints.append(record)
    proposed, skipped = [], []
    for hint in hints:
        pair, tail = hint['pair'], hint['tail']
        if (tail not in names or any(name not in names or devices[name]['cls'] not in _ACTIVE
                                    for name in pair) or len(set(pair)) != 2):
            skipped.append(dict(hint, reason='invalid_tail_members'))
            continue
        if len(ownership[tail]) != 1:
            skipped.append(dict(hint, reason='shared_tail'))
            continue
        if any(name != tail and union.find(name) == union.find(tail) for name in names):
            skipped.append(dict(hint, reason='tail_belongs_to_corresponding_group'))
            continue
        first, second = (devices[name] for name in pair)
        source_field = 's' if first['cls'] == 'MOS' else 'e'
        common = first[source_field]
        if (first['cls'] != second['cls'] or first['type'] != second['type']
                or second.get(source_field) != common
                or common != dl.out_terminal(first)
                or common != dl.out_terminal(second) or _rail(common)):
            skipped.append(dict(hint, reason='tail_is_not_downstream_of_pair'))
            continue
        source_users = [name for name, device in devices.items()
                        if device['cls'] in _ACTIVE
                        and device.get('s' if device['cls'] == 'MOS' else 'e') == common]
        if set(source_users) != set(pair):
            skipped.append(dict(hint, reason='shared_source_node'))
            continue
        if any(tail in component['devices'] and set(pair) & set(component['devices'])
               for component in protected_sccs):
            skipped.append(dict(hint, reason='tail_within_original_scc'))
            continue
        device = devices[tail]
        if device['cls'] in _ACTIVE:
            connected = dl.in_terminal(device) == common
        elif device['cls'] == 'ISRC':
            connected = common in (device['p'], device['n'])
        else:
            connected = False
        if not connected:
            skipped.append(dict(hint, reason='tail_not_connected_to_pair_output'))
            continue
        proposed.extend({'upper': name, 'lower': tail, 'reason': 'exclusive_tail',
                         'relation': hint['relation']} for name in pair)
    bad = _cyclic_groups(names, orders + proposed, union)
    accepted = []
    for order in proposed:
        if union.find(order['upper']) in bad and union.find(order['lower']) in bad:
            skipped.append({'relation': order['relation'], 'pair': [order['upper']],
                            'tail': order['lower'], 'reason': 'tail_order_conflict'})
        else:
            accepted.append(order)
    return accepted, skipped


def _assign_rows(names, union, orders, preferred):
    members = defaultdict(list)
    for name in names:
        members[union.find(name)].append(name)
    adjacency = {name: set() for name in members}
    indegree = {name: 0 for name in members}
    for order in orders:
        a, b = union.find(order['upper']), union.find(order['lower'])
        if b not in adjacency[a]:
            adjacency[a].add(b)
            indegree[b] += 1
    rows = {name: median_low(sorted(preferred[member] for member in group))
            for name, group in members.items()}
    ready = [name for name, degree in indegree.items() if degree == 0]
    heapify(ready)
    visited = 0
    while ready:
        node = heappop(ready)
        visited += 1
        for child in sorted(adjacency[node]):
            rows[child] = max(rows[child], rows[node] + 1)
            indegree[child] -= 1
            if indegree[child] == 0:
                heappush(ready, child)
    if visited != len(members):
        raise ValueError('Internal branch row constraint cycle')
    return {name: rows[union.find(name)] for name in sorted(names)}


def _shift_row(row, insertions):
    return row + sum(item['count'] for item in insertions if item['before_row'] <= row)


def _plan_attachments(devices, comps, bridges, base_rows, union):
    """Reserve display slots, not hard SA constraints, for unique R/L chains."""
    incidence = _visible_incidence(comps)
    by_id = {comp['id']: comp for comp in comps}
    last_boundary = max(base_rows.values(), default=-1) + 1
    eligible, skipped = [], []
    for bridge in bridges:
        members = bridge['members']
        record = {'id': bridge['id'], 'members': list(members),
                  'start_net': bridge['start'], 'end_net': bridge['end'],
                  'path': list(bridge['path'])}
        anchors, error = [], None
        for net, direction in ((bridge['start'], 'out'), (bridge['end'], 'in')):
            if _rail(net):
                anchors.append((None, None))
                continue
            users = [(name, pin) for name, pin in incidence[net] if name not in members]
            if len(users) != 1 or devices[users[0][0]]['cls'] not in _ACTIVE:
                error = 'ambiguous_attachment_endpoint'
                break
            name, pin = users[0]
            expected = dl.out_terminal(devices[name]) if direction == 'out' else dl.in_terminal(devices[name])
            if expected != net:
                error = 'attachment_endpoint_direction'
                break
            anchors.append((name, pin))
        if error:
            skipped.append(dict(record, reason=error))
            continue
        (upper, upper_pin), (lower, lower_pin) = anchors
        if upper is None and lower is None:
            skipped.append(dict(record, reason='no_active_attachment_endpoint'))
            continue
        upper_row = base_rows[upper] if upper is not None else -1
        lower_row = base_rows[lower] if lower is not None else last_boundary
        if upper_row >= lower_row:
            skipped.append(dict(record, reason='nonincreasing_attachment_endpoints'))
            continue
        member_pins = {}
        for number, name in enumerate(members):
            incoming = [pin for pin, net in by_id[name]['nodes'].items()
                        if net == bridge['path'][number]]
            outgoing = [pin for pin, net in by_id[name]['nodes'].items()
                        if net == bridge['path'][number + 1]]
            if len(incoming) != 1 or len(outgoing) != 1 or incoming == outgoing:
                error = 'member_pin_mapping_ambiguous'
                break
            member_pins[name] = {'in': incoming[0], 'out': outgoing[0]}
        if error:
            skipped.append(dict(record, reason=error))
            continue
        connections = []

        def connect(net, first, second):
            connections.append({'kind': 'rl_bridge_axis', 'net': net,
                                'pins': [list(first), list(second)]})

        if upper is not None:
            connect(bridge['start'], (upper, upper_pin), (members[0], member_pins[members[0]]['in']))
        for number, (first, second) in enumerate(zip(members, members[1:]), 1):
            connect(bridge['path'][number], (first, member_pins[first]['out']),
                    (second, member_pins[second]['in']))
        if lower is not None:
            connect(bridge['end'], (members[-1], member_pins[members[-1]]['out']), (lower, lower_pin))
        eligible.append(dict(record, upper=upper, lower=lower,
            upper_pin=upper_pin, lower_pin=lower_pin,
            upper_row=upper_row, lower_row=lower_row,
            member_pins=member_pins, connections=connections))

    while True:
        additions = {}

        def shifted(row):
            return row + sum(count for boundary, count in additions.items() if boundary <= row)

        # Coordinate order, rather than instance names, determines insertion.
        # Independent branches reuse the same gap instead of summing widths.
        for bridge in sorted(eligible, key=lambda x: (x['lower_row'], x['upper_row'], len(x['members']))):
            gap = shifted(bridge['lower_row']) - shifted(bridge['upper_row']) - 1
            missing = max(0, len(bridge['members']) - gap)
            if missing:
                boundary = bridge['lower_row']
                additions[boundary] = additions.get(boundary, 0) + missing
        rows = {name: shifted(row) for name, row in base_rows.items()}
        attachment_rows = {}
        for bridge in eligible:
            count = len(bridge['members'])
            if bridge['upper'] is None:
                end = rows[bridge['lower']]
                assigned = list(range(end - count, end))
            elif bridge['lower'] is None:
                start = rows[bridge['upper']]
                assigned = list(range(start + 1, start + count + 1))
            else:
                start, end = rows[bridge['upper']], rows[bridge['lower']]
                assigned = [start + (i + 1) * (end - start) // (count + 1)
                            for i in range(count)]
            attachment_rows.update(zip(bridge['members'], assigned))
        rows.update(attachment_rows)
        grouped = defaultdict(list)
        for name in rows:
            grouped[union.find(name)].append(name)
        bad = {root for root, members in grouped.items()
               if len({rows[name] for name in members}) != 1}
        rejected = [bridge for bridge in eligible
                    if any(union.find(name) in bad for name in bridge['members'])]
        if not rejected:
            break
        # Moving just one corresponding load would destroy a same-row hint.
        # Reject all conflicting chains together, then recompute the axis.
        skipped.extend(dict(bridge, reason='attachment_conflicts_with_same_row')
                       for bridge in rejected)
        eligible = [bridge for bridge in eligible if bridge not in rejected]

    insertions = [{'before_row': row, 'count': count} for row, count in sorted(additions.items())]
    alignment = []
    for bridge in eligible:
        value = {key: item for key, item in bridge.items() if key not in ('upper_row', 'lower_row')}
        value['attachment_rows'] = {name: rows[name] for name in bridge['members']}
        alignment.append(value)
    axis = [shifted(row) for row in range(last_boundary + 1)]
    return rows, attachment_rows, alignment, skipped, axis, insertions


def plan_branch_rows(devices, levels, row_of, relations, comps, index):
    """Return a new row list and JSON-safe display-order evidence.

    ``levels`` is the unchanged result of ``device_leveling.compute_levels``.
    Correspondence hints use ``layout.pairs``/``layout.same_row``; dedicated
    downstream tails use ``roles.exclusive_tails``. Rows remain preferences,
    rather than a hard total order between unrelated electrical branches.
    """
    names = set(devices)
    if (set(index) != names or len(comps) != len(names) or len(row_of) != len(comps)
            or any(index.get(comp.get('id')) != i for i, comp in enumerate(comps))):
        raise ValueError('devices, comps, index and row_of must cover the same device IDs')
    if not isinstance(levels, Mapping):
        raise ValueError('levels must be a device_leveling mapping')
    if any(type(row) is not int or row < 0 for row in row_of):
        raise ValueError('row_of must contain non-negative integer rows')
    relations = list(relations or ())
    if any(not isinstance(record, Mapping) for record in relations):
        raise ValueError('relations must contain relation objects')
    preferred = {name: row_of[index[name]] for name in sorted(names)}
    original_levels = dict(levels.get('device_levels', {}))
    report = {'version': VERSION, 'status': 'unchanged', 'rows_before': preferred,
              'rows': dict(preferred), 'display_rows': dict(preferred),
              'original_device_levels': original_levels, 'strict_orders': [],
              'same_row': [], 'tail_orders': [], 'rl_bridges': [],
              'tail_preferred_rows': {},
              'rows_before_attachments': dict(preferred),
              'solver_preferred_rows': dict(preferred),
              'attachment_rows': {}, 'bridge_alignment': [], 'skipped_attachments': [],
              'row_axis_map': list(range(max(row_of, default=-1) + 2)), 'inserted_rows': [],
              'skipped_bridges': [], 'skipped_relations': [], 'skipped_tails': [],
              'original_sccs': [], 'changes': []}
    if not any(device['cls'] in _ACTIVE for device in devices.values()):
        report['reason'] = 'no_active_devices'
        return list(row_of), report
    bridges, rejected = _rl_chains(devices, comps)
    orders, bridges, cycle_rejected, sccs = _conduction_orders(devices, bridges)
    union, same_row, skipped = _same_rows(relations, names, devices,
        {comp['id']: comp for comp in comps}, orders)
    tails, tail_skipped = _tail_orders(relations, devices, names, union, orders, sccs)
    all_orders = orders + tails
    # A legacy current-source attachment may sit below every active layer.
    # Its old row is not a lower bound for an exclusive local input tail.
    # Compute the pair's display row first, then prefer its immediate next row;
    # all real conduction constraints are still free to push the tail down.
    source_rows = _assign_rows(names, union, orders, preferred)
    solver_preferred = dict(preferred)
    tail_preferred = {}
    for order in tails:
        name = order['lower']
        tail_preferred[name] = max(tail_preferred.get(name, 0), source_rows[order['upper']] + 1)
    solver_preferred.update(tail_preferred)
    base_rows = _assign_rows(names, union, all_orders, solver_preferred)
    rows, attachment_rows, alignment, attachment_skipped, axis, insertions = _plan_attachments(
        devices, comps, bridges, base_rows, union)
    result = [rows[comp['id']] for comp in comps]
    report.update(rows=rows, display_rows=dict(rows),
                  strict_orders=all_orders, same_row=same_row, tail_orders=tails,
                  tail_preferred_rows={name: _shift_row(row, insertions)
                                       for name, row in tail_preferred.items()},
                  rows_before_attachments=base_rows,
                  solver_preferred_rows={name: _shift_row(row, insertions)
                                         for name, row in solver_preferred.items()},
                  attachment_rows=attachment_rows, bridge_alignment=alignment,
                  skipped_attachments=attachment_skipped,
                  row_axis_map=axis, inserted_rows=insertions,
                  rl_bridges=bridges, skipped_bridges=rejected + cycle_rejected,
                  skipped_relations=skipped, skipped_tails=tail_skipped,
                  original_sccs=sccs,
                  changes=[{'device': name, 'from': preferred[name], 'to': rows[name]}
                           for name in sorted(names) if rows[name] != preferred[name]])
    if result != list(row_of):
        report['status'] = 'changed'
    return result, report
