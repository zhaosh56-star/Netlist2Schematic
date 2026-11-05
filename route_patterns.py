"""Bounded three-segment candidates for already validated free wire chains.

Only geometry is proposed here.  The caller retains authority over fixed pin
corridors, original-terminal connectivity, cycles, crossings and final metrics.
No placement, global routing or A* search is performed.
"""
from __future__ import annotations

from routing_rules import (EPS, canonical_topology, check_deadline,
                           segment_hits_rect)

MAX_TRACKS_PER_AXIS = 12


def _edge(a, b):
    return tuple(sorted((tuple(a), tuple(b))))


def _length(segments):
    return sum(abs(a[0]-b[0]) + abs(a[1]-b[1]) for a, b in segments)


def _outside_body_axes(scene, deadline):
    axes = (set(), set())
    step = scene.options.grid_step
    for _, rect in (*scene.body_rects, *scene.marker_rects):
        check_deadline(deadline, 'pattern_obstacle_tracks')
        axes[0].update((rect[0]-step, rect[2]+step))
        axes[1].update((rect[1]-step, rect[3]+step))
    return axes


def _foreign_axes(scene, by_net, net, deadline):
    """Track and quiet-zone boundaries, without a Cartesian routing graph."""
    axes = (set(), set())
    spacing, quiet = scene.options.center_spacing, scene.options.cross_clearance
    owners = set(by_net) | set(scene.fixed_segments) | set(scene.all_pins)
    for owner in sorted(owners):
        check_deadline(deadline, 'pattern_foreign_tracks')
        if owner == net:
            continue
        fixed = scene.fixed_segments.get(owner, ())
        geometry = list(by_net.get(owner, ())) + list(fixed)
        topology = canonical_topology(geometry, scene.all_pins.get(owner, ()),
                                      deadline=deadline)
        # Fixed escape endpoints are meaningful for their reserved corridors,
        # even when they merge into a longer collinear final signal run.
        for a, b in list(topology['runs']) + list(fixed):
            check_deadline(deadline, 'pattern_track_clearance')
            for point in (a, b):
                for axis in (0, 1):
                    axes[axis].update((point[axis]-spacing, point[axis]+spacing))
        for point in topology['structure_points']:
            check_deadline(deadline, 'pattern_quiet_tracks')
            for axis in (0, 1):
                axes[axis].update((point[axis]-quiet, point[axis]+quiet))
    return axes


def _connector(source, target, axis, track):
    if axis == 0:
        points = (source, (track, source[1]), (track, target[1]), target)
    else:
        points = (source, (source[0], track), (target[0], track), target)
    segments = list(zip(points, points[1:]))
    # Straight/L degeneracies have already been tried by the old simplifier.
    # All remaining segments alternate axes, hence no further compression is
    # needed and neither a zero-length edge nor collinear backtracking remains.
    if any(a == b for a, b in segments):
        return None
    return segments


def _body_clear(scene, net, segments, deadline):
    """Discard obvious body/marker hits before spending a track's work slot."""
    for a, b in segments:
        for component, rect in scene.body_rects:
            check_deadline(deadline, 'pattern_body_screen')
            if not scene.body_allowed(net, a, b, component, rect):
                return False
        for _, rect in scene.marker_rects:
            check_deadline(deadline, 'pattern_marker_screen')
            if segment_hits_rect(a, b, rect):
                return False
    return True


def _follows_fixed_exits(scene, net, segments, deadline):
    """A free connector cannot start by retracing a protected pin corridor."""
    endpoints = ((segments[0][0], segments[0][1]),
                 (segments[-1][1], segments[-1][0]))
    for pin in scene.pins.get(net, ()):
        check_deadline(deadline, 'pattern_fixed_direction')
        if pin.point == pin.escape:
            continue
        for endpoint, neighbor in endpoints:
            if endpoint != pin.escape:
                continue
            delta = neighbor[0]-endpoint[0], neighbor[1]-endpoint[1]
            if delta[0]*pin.direction[0] + delta[1]*pin.direction[1] < -EPS:
                return False
    return True


def _tracks(scene, net, path, axis, boundary_axes, limit, deadline):
    source, target = path[0], path[-1]
    # HVH uses vertical tracks (axis=0); VHV uses horizontal tracks (axis=1).
    old_tracks = {a[axis] for a, b in zip(path, path[1:]) if a[axis] == b[axis]}
    candidates = set(boundary_axes[axis]) | old_tracks
    ranked = []
    for track in candidates:
        check_deadline(deadline, 'pattern_track_ranking')
        connector = _connector(source, target, axis, track)
        if connector is None or not _follows_fixed_exits(scene, net, connector, deadline):
            continue
        length = _length(connector)
        if length > limit + EPS:
            continue
        ranked.append((length, 0 if track in old_tracks else 1,
                       abs(track-(source[axis]+target[axis])/2), track, connector))
    kept = []
    for length, origin, displacement, track, connector in sorted(ranked):
        check_deadline(deadline, 'pattern_track_selection')
        if not _body_clear(scene, net, connector, deadline):
            continue
        kept.append((length, origin, displacement, axis, track, connector))
        if len(kept) >= MAX_TRACKS_PER_AXIS:
            break
    return kept


def three_segment_candidates(scene, routed, chains, deadline=None):
    """Yield full-net trial wires using bounded HVH/VHV connectors.

    ``chains`` comes from the caller's protected-anchor chain extraction. Its
    free paths contain no internal pin, branch, escape or fixed-corridor edge.
    Chains with fewer than two internal turns are outside this operation.
    A candidate keeps both ends and all other net segments.  It still needs
    complete validation and the caller's non-regression checks before commit.
    """
    check_deadline(deadline, 'pattern_generation')
    by_net = dict(routed)
    static_axes = None
    foreign_cache = {}
    for chain in chains:
        check_deadline(deadline, 'pattern_chain')
        net = chain['net']
        if chain['bends'] < 2 or net not in scene.pins or net in scene.power_nets:
            continue
        path = tuple(tuple(point) for point in chain['path'])
        if path[0] == path[-1]:
            continue
        if static_axes is None:
            static_axes = _outside_body_axes(scene, deadline)
        if net not in foreign_cache:
            foreign_cache[net] = _foreign_axes(scene, by_net, net, deadline)
        boundaries = tuple(static_axes[axis] | foreign_cache[net][axis] for axis in (0, 1))
        removed = {_edge(a, b) for a, b in zip(path, path[1:])}
        base = [segment for segment in chain['segments'] if _edge(*segment) not in removed]
        choices = []
        for axis in (0, 1):
            choices.extend(_tracks(scene, net, path, axis, boundaries,
                                   chain['length'], deadline))
        seen = set()
        for length, origin, _, axis, track, connector in sorted(choices):
            check_deadline(deadline, 'pattern_candidate')
            identity = tuple(sorted(_edge(a, b) for a, b in connector))
            if identity in seen or set(identity) == removed:
                continue
            seen.add(identity)
            yield {'net': net, 'segments': base + connector,
                   'source': path[0], 'target': path[-1],
                   'pattern': 'horizontal_vertical_horizontal' if axis == 0 else
                              'vertical_horizontal_vertical',
                   'track_axis': 'x' if axis == 0 else 'y', 'track_coordinate': track,
                   'track_origin': 'existing_track' if origin == 0 else 'geometry_boundary',
                   'old_chain_length': chain['length'], 'new_chain_length': length}
