"""Geometric leaf-branch reattachment candidates for validated signal trees.

This module constructs bounded straight/L candidates only.  It neither routes
other nets nor decides whether a trial is electrically or geometrically safe;
the final simplifier applies its unchanged complete validation before commit.
"""
from __future__ import annotations

from netlist2schematic.routing.routing_rules import (EPS, canonical_topology, check_deadline, intersection,
                           on_segment)

MAX_TARGETS_PER_BRANCH = 12


def _edge(a, b):
    return tuple(sorted((tuple(a), tuple(b))))


def _length(segments):
    return sum(abs(a[0]-b[0]) + abs(a[1]-b[1]) for a, b in segments)


def _covered(segment, runs):
    return any(on_segment(segment[0], a, b) and on_segment(segment[1], a, b)
               for a, b in runs)


def _component(adjacency, start, deadline):
    if start not in adjacency:
        return set()
    found, pending = {start}, [start]
    while pending:
        check_deadline(deadline, 'reattach_component')
        for neighbor in adjacency[pending.pop()]:
            if neighbor not in found:
                found.add(neighbor)
                pending.append(neighbor)
    return found


def _adjacency(segments):
    result = {}
    for a, b in segments:
        result.setdefault(a, set()).add(b)
        result.setdefault(b, set()).add(a)
    return result


def _branch_records(scene, routed, deadline):
    result = []
    for net, segments in routed:
        check_deadline(deadline, 'reattach_branch_extraction')
        pins = scene.pins.get(net, ())
        if len(pins) < 3 or net in scene.power_nets:
            continue
        real = {tuple(point) for point in scene.all_pins.get(net, ())}
        real.update(pin.point for pin in pins)
        fixed_segments = scene.fixed_segments.get(net, ())
        anchors = real | {pin.escape for pin in pins}
        anchors.update(point for segment in fixed_segments for point in segment)
        topology = canonical_topology(segments, pins=anchors, deadline=deadline)
        adjacency = topology['adjacency']
        if not adjacency:
            continue
        # A tree makes the isolated-terminal ownership unambiguous.  Preserve
        # more complicated existing networks rather than guessing at a cut.
        if (len(topology['segments']) != len(adjacency)-1
                or len(_component(adjacency, min(adjacency), deadline)) != len(adjacency)):
            continue
        fixed = canonical_topology(fixed_segments, deadline=deadline)['runs']
        if any(not _covered(segment, topology['runs']) for segment in fixed_segments):
            continue
        for pin in sorted(pins, key=lambda p: (p.escape, p.point, p.component, p.pin)):
            check_deadline(deadline, 'reattach_branch_extraction')
            source = pin.escape
            if pin.point == source or len(adjacency.get(pin.point, ())) != 1:
                continue
            if any(other is not pin and other.point == pin.point for other in pins):
                continue
            outward = sorted(neighbor for neighbor in adjacency.get(source, ())
                             if not _covered((source, neighbor), fixed))
            if len(outward) != 1:
                continue
            path = [source, outward[0]]
            visited = {source}
            previous, current = path
            while current not in anchors and len(adjacency[current]) == 2:
                check_deadline(deadline, 'reattach_leaf_walk')
                if current in visited:
                    break
                visited.add(current)
                following = next(iter(adjacency[current] - {previous}))
                if _covered((current, following), fixed):
                    break
                path.append(following)
                previous, current = current, following
            if (current in visited or current == source
                    or not (current in anchors or len(adjacency[current]) > 2)):
                continue
            removed = {_edge(a, b) for a, b in zip(path, path[1:])}
            if any(_covered(segment, fixed) for segment in removed):
                continue
            base = [segment for segment in topology['segments'] if segment not in removed]
            remaining = _adjacency(base)
            source_side = _component(remaining, pin.point, deadline)
            if (source not in source_side
                    or any(not on_segment(point, *pin.stub) for point in source_side)
                    or (anchors - {pin.point, source}) & source_side):
                continue
            target_side = _component(remaining, current, deadline)
            if (not target_side or source_side & target_side
                    or source_side | target_side != set(remaining)
                    or not (anchors - {pin.point, source}) <= target_side
                    or any(other.point not in target_side for other in pins if other is not pin)):
                continue
            retained = [segment for segment in base
                        if segment[0] in target_side and segment[1] in target_side]
            if not retained:
                continue
            length = _length(zip(path, path[1:]))
            distance = min(abs(source[0]-min(max(source[0], min(a[0], b[0])), max(a[0], b[0])))
                           + abs(source[1]-min(max(source[1], min(a[1], b[1])), max(a[1], b[1])))
                           for a, b in retained)
            result.append({'net': net, 'pin': pin, 'path': tuple(path), 'base': base,
                           'retained': retained, 'anchors': anchors,
                           'length': length, 'excess': length-distance})
    return sorted(result, key=lambda branch: (-branch['excess'], -branch['length'],
                                              branch['path'][0], branch['path'][-1],
                                              str(branch['net'])))


def _targets(branch, deadline):
    source = branch['path'][0]
    points = {point for segment in branch['retained'] for point in segment}
    for a, b in branch['retained']:
        check_deadline(deadline, 'reattach_targets')
        points.add((min(max(source[0], min(a[0], b[0])), max(a[0], b[0])),
                    min(max(source[1], min(a[1], b[1])), max(a[1], b[1]))))
    points.difference_update((source, branch['path'][-1]))
    ordered = sorted(points, key=lambda point: (
        abs(point[0]-source[0]) + abs(point[1]-source[1]), point))
    return [point for point in ordered
            if abs(point[0]-source[0]) + abs(point[1]-source[1]) <= branch['length'] + EPS
            ][:MAX_TARGETS_PER_BRANCH]


def _paths(source, target):
    if source[0] == target[0] or source[1] == target[1]:
        return [(source, target)]
    return sorted(((source, (source[0], target[1]), target),
                   (source, (target[0], source[1]), target)))


def _contact(a, b, runs, deadline):
    """Find the first intersection, including the entry to a collinear run."""
    contacts = set()
    for c, d in runs:
        check_deadline(deadline, 'reattach_first_contact')
        point = intersection(a, b, c, d)
        if point is not None:
            contacts.add(point)
        elif a[0] == b[0] == c[0] == d[0]:
            low, high = max(min(a[1], b[1]), min(c[1], d[1])), min(max(a[1], b[1]), max(c[1], d[1]))
            if low <= high:
                contacts.add((a[0], low if b[1] > a[1] else high))
        elif a[1] == b[1] == c[1] == d[1]:
            low, high = max(min(a[0], b[0]), min(c[0], d[0])), min(max(a[0], b[0]), max(c[0], d[0]))
            if low <= high:
                contacts.add((low if b[0] > a[0] else high, a[1]))
    return min(contacts, key=lambda point: (abs(point[0]-a[0])+abs(point[1]-a[1]), point),
               default=None)


def _first_connection(path, retained, deadline):
    prefix = []
    for a, b in zip(path, path[1:]):
        if a == b:
            continue
        point = _contact(a, b, retained, deadline)
        if point is not None:
            if point != a:
                prefix.append((a, point))
            return prefix, point
        prefix.append((a, b))
    return None, None


def reattachment_candidates(scene, routed, deadline=None):
    """Yield complete same-net trial geometries without mutating accepted wires.

    Only a uniquely owned terminal leaf is detached, and it keeps its original
    fixed stub.  A connector ends at its first contact with the retained tree,
    so a goal farther along that tree cannot add a redundant chord or cycle.
    Every yielded candidate still requires the caller's full validation.
    """
    for branch in _branch_records(scene, routed, deadline):
        seen = set()
        source = branch['path'][0]
        pin = branch['pin']
        for goal in _targets(branch, deadline):
            for path in _paths(source, goal):
                check_deadline(deadline, 'reattach_candidate')
                initial = path[1][0]-source[0], path[1][1]-source[1]
                if initial[0]*pin.direction[0] + initial[1]*pin.direction[1] < -EPS:
                    continue
                connector, attachment = _first_connection(path, branch['retained'], deadline)
                if (not connector or attachment == branch['path'][-1]
                        or _length(connector) > branch['length'] + EPS):
                    continue
                candidate = canonical_topology(branch['base'] + connector,
                    pins=branch['anchors'], deadline=deadline)['segments']
                identity = tuple(candidate)
                if identity in seen:
                    continue
                seen.add(identity)
                yield {'net': branch['net'], 'segments': candidate,
                       'source': source, 'source_pin': pin.point, 'pin': pin.pin,
                       'component': pin.component,
                       'old_attachment': branch['path'][-1], 'new_attachment': attachment,
                       'old_branch_length': branch['length'],
                       'new_branch_length': _length(connector)}
