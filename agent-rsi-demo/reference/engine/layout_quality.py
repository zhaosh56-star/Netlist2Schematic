"""Comparable, normalized quality measurements on the final drawn wiring.

HPWL is a conservative geometric reference for each connected net, not a claim
that the extra length can all be removed: obstacles, portals and multi-terminal
trees can require more.  Neither a necessary straight trunk nor an isolated pin
receives an extra detour penalty merely for being long.
"""
from __future__ import annotations

from itertools import combinations

from routing_rules import canonical_topology, intersection


def _length(segment):
    a, b = segment
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _outside_length(runs, bounds):
    if bounds is None:
        return 0.0
    x0, y0, x1, y1 = bounds
    total = 0.0
    for a, b in runs:
        if a[0] == b[0]:
            inside = (max(0, min(max(a[1], b[1]), y1) - max(min(a[1], b[1]), y0))
                      if x0 <= a[0] <= x1 else 0)
        else:
            inside = (max(0, min(max(a[0], b[0]), x1) - max(min(a[0], b[0]), x0))
                      if y0 <= a[1] <= y1 else 0)
        total += _length((a, b)) - inside
    return total


def _proper_cross(a, b, c, d):
    point = intersection(a, b, c, d)
    return point if point is not None and point not in (a, b, c, d) else None


def _role_layout(record):
    """Support both explicit layout hints and role-only saved records."""
    if record.get('layout'):
        return record['layout']
    roles = record.get('roles') or {}
    kind = record.get('kind')
    if kind in ('complementary_branch', 'output_pair'):
        return {'align_columns': [[roles.get('upper'), roles.get('lower')]]}
    if kind == 'latch':
        return {'align_columns': [[roles.get('left_upper'), roles.get('left_lower')],
                                  [roles.get('right_upper'), roles.get('right_lower')]],
                'align_rows': [[roles.get('left_upper'), roles.get('right_upper')],
                               [roles.get('left_lower'), roles.get('right_lower')]]}
    if kind == 'differential_pair':
        return {'align_rows': [[roles.get('left'), roles.get('right')]]}
    return {}


def _structure_quality(pl, comps, index, structs):
    positions = {name: pl.pos_of(i) for name, i in index.items()}
    alignment = dispersion = 0.0
    records = []
    by_id = {record.get('id'): record for record in structs}
    seen_alignments = set()
    for record in structs:
        if record.get('confidence') == 'relation':
            continue
        members = [name for name in record.get('devices', ()) if name in positions]
        if len(members) < 2:
            continue
        hints = _role_layout(record)
        local_alignment = 0.0
        for key, axis in (('align_columns', 0), ('align_rows', 1)):
            for group in hints.get(key, ()):
                names = tuple(sorted(name for name in group if name in positions))
                identity = (axis, names)
                if identity in seen_alignments:
                    continue
                seen_alignments.add(identity)
                values = [positions[name][axis] for name in names]
                if len(values) > 1:
                    local_alignment += (max(values) - min(values)) / 100.0
        # A related block may legitimately contain other child structures. Only
        # unrelated interlopers count as separation, not those owned by a parent.
        allowed = set(members)
        parent = by_id.get(record.get('parent'))
        visited_parents = set()
        while parent is not None and parent.get('id') not in visited_parents:
            visited_parents.add(parent.get('id'))
            allowed.update(parent.get('devices', ()))
            parent = by_id.get(parent.get('parent'))
        xs, ys = zip(*(positions[name] for name in members))
        bounds = (min(xs), min(ys), max(xs), max(ys))
        interlopers = sorted(name for name, (x, y) in positions.items() if name not in allowed
                             and bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3])
        span = bounds[2] - bounds[0] + bounds[3] - bounds[1]
        # A small independent soft term tolerates internal branch space. It
        # cannot magnify every unrelated wire in the drawing as the old energy did.
        local_dispersion = len(interlopers) + .1 * max(0, span / 100.0 - len(members) + 1)
        alignment += local_alignment
        dispersion += local_dispersion
        records.append({'id': record.get('id'), 'kind': record.get('kind'),
                        'alignment': local_alignment, 'dispersion': local_dispersion,
                        'span': span, 'interlopers': interlopers})
    return alignment, dispersion, records


def evaluate_quality(par, pl, comps, nets, index, routed, metrics, structs=()):
    """Measure one validated candidate; return only JSON-compatible values.

    ``metrics`` is accepted alongside final-route results for a stable caller
    interface. Drawing-dependent values are recomputed after canonical merging,
    so segment subdivision and duplicated same-net intervals cannot change scores.
    """
    topologies = {}
    per_net = {}
    for net, segments in routed:
        anchors = {pl.pin_world(index[name], pin) for name, pin in nets.get(net, ())}
        topology = canonical_topology(segments, anchors)
        topologies[net] = topology
        wirelength = sum(map(_length, topology['runs']))
        bounds = None
        if anchors:
            xs, ys = zip(*anchors)
            bounds = min(xs), min(ys), max(xs), max(ys)
        hpwl = bounds[2] - bounds[0] + bounds[3] - bounds[1] if bounds else 0.0
        multiple = len(anchors) > 1 and hpwl > 0
        excess = max(0.0, wirelength - hpwl) if multiple else 0.0
        bends = 0
        for point, neighbors in topology['adjacency'].items():
            if len(neighbors) == 2:
                a, b = tuple(neighbors)
                bends += int((a[0] == point[0]) != (b[0] == point[0]))
        per_net[net] = {'terminals': len(anchors), 'wirelength': wirelength,
                        'hpwl': hpwl, 'detour_factor': wirelength / hpwl if multiple else None,
                        'excess_detour': excess,
                        'outside_length': _outside_length(topology['runs'], bounds) if multiple else 0.0,
                        'crossings': 0, 'bends': bends}
    crossings = 0
    for first, second in combinations(sorted(topologies), 2):
        points = {_proper_cross(a, b, c, d)
                  for a, b in topologies[first]['runs'] for c, d in topologies[second]['runs']}
        count = len(points - {None})
        crossings += count
        per_net[first]['crossings'] += count
        per_net[second]['crossings'] += count
    wirelength = sum(row['wirelength'] for row in per_net.values())
    bends = sum(row['bends'] for row in per_net.values())
    excess = sum(row['excess_detour'] for row in per_net.values())
    outside = sum(row['outside_length'] for row in per_net.values())
    alignment, dispersion, structure_details = _structure_quality(pl, comps, index, structs)
    terms = {'wirelength': wirelength / 100.0, 'crossings': 1.5 * crossings,
             'bends': .15 * bends, 'detour': .5 * excess / 100.0,
             'alignment': alignment, 'dispersion': dispersion}
    return {'score': sum(terms.values()), 'wirelength': wirelength, 'crossings': crossings,
            'bends': bends, 'alignment': alignment, 'dispersion': dispersion,
            'detour': excess, 'outside_length': outside, 'per_net': per_net,
            'score_terms': terms, 'structures': structure_details,
            'detour_reference': 'terminal_half_perimeter_bounding_box'}
