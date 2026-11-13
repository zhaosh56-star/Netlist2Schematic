"""Bounded inward shifts of tracks in already validated free wire chains.

The path keeps its endpoints and segment order. Only an interior segment's
track and its two adjacent corners move; no graph search or placement belongs
here. The caller must independently validate the resulting complete net.
"""
from __future__ import annotations

from collections import deque

from netlist2schematic.postprocess.route_patterns import (_body_clear, _follows_fixed_exits, _foreign_axes,
                            _outside_body_axes)
from netlist2schematic.routing.routing_rules import (EPS, PreparedOccupancy, canonical_topology, check_deadline,
                           intersection)

MAX_TRACK_CANDIDATES_PER_CHAIN = 24


def _edge(a, b):
    return tuple(sorted((tuple(a), tuple(b))))


def _length(path):
    return sum(abs(a[0]-b[0]) + abs(a[1]-b[1]) for a, b in zip(path, path[1:]))


def _bends(path):
    return sum((a[0] == b[0]) != (b[0] == c[0])
               for a, b, c in zip(path, path[1:], path[2:]))


def _clean_path(points):
    """Collapse degenerate straight pieces, refusing collinear backtracking."""
    path = []
    for point in points:
        if path and point == path[-1]:
            continue
        path.append(point)
        while len(path) >= 3:
            a, b, c = path[-3:]
            if not (a[0] == b[0] == c[0] or a[1] == b[1] == c[1]):
                break
            if (b[0]-a[0])*(c[0]-b[0]) + (b[1]-a[1])*(c[1]-b[1]) < -EPS:
                return None
            path.pop(-2)
    return tuple(path)


def _shift(path, segment, axis, track):
    points = list(path)
    for index in (segment, segment+1):
        point = list(points[index])
        point[axis] = track
        points[index] = tuple(point)
    # Tightening may collapse a neighboring segment, but never reverse its
    # direction and send the path back along a protected exit or old turn.
    for index in (segment-1, segment+1):
        a, b = path[index:index+2]
        c, d = points[index:index+2]
        if (b[0]-a[0])*(d[0]-c[0]) + (b[1]-a[1])*(d[1]-c[1]) < -EPS:
            return None
    return _clean_path(points)


def _crossings(path, foreign, deadline):
    """A ranking estimate only; final normalized whole-net metrics decide."""
    found = set()
    checked = 0
    for a, b in zip(path, path[1:]):
        check_deadline(deadline, 'track_compaction_crossing_rank')
        for owner, segments in foreign:
            for c, d in segments:
                checked += 1
                if checked % 64 == 0:
                    check_deadline(deadline, 'track_compaction_crossing_rank')
                point = intersection(a, b, c, d)
                if point is not None and point not in (a, b, c, d):
                    found.add((owner, point))
    return len(found)


def _priority(chain):
    source, target = chain['path'][0], chain['path'][-1]
    span = abs(source[0]-target[0]) + abs(source[1]-target[1])
    return (-(chain['length']-span), -chain['length'], -chain['bends'],
            chain['path'], str(chain['net']))


def _foreign_clear(scene, net, connector, occupied, deadline=None, cache=None):
    """Reject definite edge conflicts; a pass never replaces full validation.

    Ordinary corners and artificial escape/cut points must not receive the
    stronger A* vertex keepout. In particular a legal parallel corner can be
    ten units from another net even when cross_clearance is twelve units.
    Cache entries belong only to this immutable occupancy and scene.
    """
    cache = cache if cache is not None else {}
    prepared = scene.prepare_occupied(occupied)
    for a, b in connector:
        check_deadline(deadline, 'track_compaction_foreign_screen')
        key = net, _edge(a, b)
        if key not in cache:
            cache[key] = scene.edge_check(net, a, b, prepared)[0]
        if not cache[key]:
            return False
    return True


def track_compaction_candidates(scene, routed, chains, deadline=None):
    """Yield full-net proposals while retaining an existing multi-turn shape.

    ``chains`` must come from the caller's protected-anchor extraction: no
    interior point may be a real pin, escape, fixed segment or branch. First
    and last segments cannot shift because their chain endpoints stay fixed.
    Each chain receives one bounded candidate slot per turn. Definite foreign
    edge conflicts are filtered before yielding; all surviving full-net trials
    still need the caller's independent connectivity, cycle and metric checks.
    """
    check_deadline(deadline, 'track_compaction_generation')
    by_net = dict(routed)
    static_axes = None
    net_cache = {}
    prepared = None
    edge_cache = {}

    def candidates(chain):
        nonlocal static_axes, prepared
        check_deadline(deadline, 'track_compaction_chain')
        net = chain['net']
        if net not in scene.pins or net in scene.power_nets:
            return
        path = tuple(tuple(point) for point in chain['path'])
        if len(path) < 4 or path[0] == path[-1]:
            return
        if static_axes is None:
            static_axes = _outside_body_axes(scene, deadline)
        if prepared is None:
            prepared = PreparedOccupancy({owner: canonical_topology(
                segments, scene.all_pins.get(owner, ()), deadline=deadline)
                for owner, segments in by_net.items()})
        if net not in net_cache:
            foreign_axes = _foreign_axes(scene, by_net, net, deadline)
            own_points = {point for segment in by_net.get(net, ()) for point in segment}
            boundaries = tuple(static_axes[axis] | foreign_axes[axis]
                               | {point[axis] for point in own_points}
                               for axis in (0, 1))
            foreign = []
            for owner in sorted(prepared.topologies):
                check_deadline(deadline, 'track_compaction_foreign_geometry')
                if owner != net:
                    runs = prepared.topologies[owner]['runs']
                    foreign.append((owner, runs))
            net_cache[net] = boundaries, foreign
        boundaries, foreign = net_cache[net]
        old_crossings = _crossings(path, foreign, deadline)
        old_length, old_bends = _length(path), _bends(path)
        removed = {_edge(a, b) for a, b in zip(path, path[1:])}
        base = [segment for segment in chain['segments'] if _edge(*segment) not in removed]
        ranked, seen = [], set()
        for segment in range(1, len(path)-2):
            check_deadline(deadline, 'track_compaction_segment')
            a, b = path[segment:segment+2]
            axis = 0 if a[0] == b[0] else 1
            old_track = a[axis]
            tracks = boundaries[axis] | {point[axis] for point in path}
            for track in sorted(tracks):
                check_deadline(deadline, 'track_compaction_track')
                if abs(track-old_track) <= EPS:
                    continue
                replacement = _shift(path, segment, axis, track)
                if replacement is None or len(replacement) < 2:
                    continue
                length = _length(replacement)
                if length >= old_length-EPS or _bends(replacement) > old_bends:
                    continue
                connector = list(zip(replacement, replacement[1:]))
                identity = tuple(sorted(_edge(a, b) for a, b in connector))
                if identity in seen:
                    continue
                seen.add(identity)
                if (not _follows_fixed_exits(scene, net, connector, deadline)
                        or not _body_clear(scene, net, connector, deadline)):
                    continue
                crosses = _crossings(replacement, foreign, deadline)
                # Do not let many shorter but crossing-heavy tracks consume
                # all bounded slots before a modest safe inward shift. This
                # estimate orders proposals; it is never an acceptance rule.
                ranked.append((max(0, crosses-old_crossings), length,
                               abs(track-old_track), axis, segment, track,
                               old_track, connector))
        for _, length, _, axis, segment, track, old_track, connector in sorted(ranked)[
                :MAX_TRACK_CANDIDATES_PER_CHAIN]:
            check_deadline(deadline, 'track_compaction_candidate')
            yield {'net': net, 'segments': base + connector,
                   'source': path[0], 'target': path[-1],
                   'track_axis': 'x' if axis == 0 else 'y',
                   'old_track_coordinate': old_track, 'track_coordinate': track,
                   'moved_segment_index': segment,
                   'old_chain_length': old_length, 'new_chain_length': length,
                   '_connector': connector}

    # Prepare chains lazily: ranking every chain up front can spend the whole
    # final-stage budget before even one candidate reaches complete validation.
    pending = deque(iter(candidates(chain)) for chain in sorted(chains, key=_priority))
    while pending:
        check_deadline(deadline, 'track_compaction_chain_turn')
        iterator = pending.popleft()
        try:
            proposal = next(iterator)
        except StopIteration:
            continue
        pending.append(iterator)
        connector = proposal.pop('_connector')
        # One raw slot per chain, including rejected ones, prevents a long
        # list of near-miss tracks from withholding another chain's safe move.
        if _foreign_clear(scene, proposal['net'], connector, prepared, deadline, edge_cache):
            yield proposal
