"""Bounded, fully revalidated shortcuts for already valid orthogonal routes.

Only signal-wire geometry changes. Original pins, fixed pin corridors, devices
and power geometry remain the authority supplied by ``build_scene``.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from math import isfinite
from time import perf_counter

from orthogonal_router import RoutingFailure
from route_patterns import MAX_TRACKS_PER_AXIS, three_segment_candidates
from route_reattachment import MAX_TARGETS_PER_BRANCH, reattachment_candidates
from routing_rules import (EPS, build_scene, canonical_topology, check_deadline,
                           on_segment)
from sa_routing import RoutingValidationError, _verify

VERSION = 'orthogonal-shortcuts-v4'
MAX_ROUNDS = 4
MAX_CANDIDATES = 512
OPERATIONS = ('chain_shortcut', 'leaf_reattachment', 'three_segment_shortcut')


def _edge(a, b):
    return tuple(sorted((tuple(a), tuple(b))))


def _covered(segment, runs):
    return any(on_segment(segment[0], a, b) and on_segment(segment[1], a, b)
               for a, b in runs)


def _anchors(scene, net):
    return ({tuple(p) for p in scene.all_pins.get(net, ())}
            | {p.escape for p in scene.pins.get(net, ())}
            | {point for segment in scene.fixed_segments.get(net, ()) for point in segment})


def _covers_fixed(scene, net, segments, deadline=None):
    runs = canonical_topology(segments, deadline=deadline)['runs']
    return all(_covered(segment, runs) for segment in scene.fixed_segments.get(net, ()))


def _cycle_rank(segments, deadline=None):
    adjacency = canonical_topology(segments, deadline=deadline)['adjacency']
    pending = set(adjacency)
    components = 0
    while pending:
        check_deadline(deadline, 'simplify_cycle_check')
        components += 1
        todo = [pending.pop()]
        while todo:
            point = todo.pop()
            for neighbor in adjacency[point]:
                if neighbor in pending:
                    pending.remove(neighbor)
                    todo.append(neighbor)
    return sum(len(neighbors) for neighbors in adjacency.values()) // 2 - len(adjacency) + components


def _trim_tails(scene, net, segments, deadline=None):
    """Remove only unanchored dead ends after same-net trunk reuse/branch moves."""
    protected = _anchors(scene, net)
    topology = canonical_topology(segments, pins=protected, deadline=deadline)
    adjacency = {point: set(neighbors) for point, neighbors in topology['adjacency'].items()}
    fixed = canonical_topology(scene.fixed_segments.get(net, ()), deadline=deadline)['runs']
    leaves = sorted(point for point, neighbors in adjacency.items()
                    if len(neighbors) == 1 and point not in protected)
    while leaves:
        check_deadline(deadline, 'simplify_tail_pruning')
        point = leaves.pop()
        if point in protected or len(adjacency.get(point, ())) != 1:
            continue
        neighbor = next(iter(adjacency[point]))
        if _covered((point, neighbor), fixed):
            continue
        adjacency[point].remove(neighbor)
        adjacency[neighbor].remove(point)
        if neighbor not in protected and len(adjacency[neighbor]) == 1:
            leaves.append(neighbor)
    kept = {_edge(point, neighbor) for point, neighbors in adjacency.items() for neighbor in neighbors}
    return canonical_topology(sorted(kept), pins=protected, deadline=deadline)['segments']


def _chains(scene, routed, deadline=None):
    """Nonfixed maximal degree-two chains, with real pins and escapes as cuts."""
    result = []
    for net, segments in routed:
        check_deadline(deadline, 'simplify_chain_extraction')
        if net not in scene.pins:
            continue
        protected = _anchors(scene, net)
        topology = canonical_topology(segments, pins=protected, deadline=deadline)
        adjacency = topology['adjacency']
        fixed = canonical_topology(scene.fixed_segments.get(net, ()), deadline=deadline)['runs']
        fixed_edges = {_edge(a, b) for a, b in topology['segments'] if _covered((a, b), fixed)}
        stops = protected | {point for point, neighbors in adjacency.items() if len(neighbors) != 2}
        stops.update(point for edge in fixed_edges for point in edge)
        visited = set(fixed_edges)
        for start in sorted(stops & set(adjacency)):
            for neighbor in sorted(adjacency[start]):
                edge = _edge(start, neighbor)
                if edge in visited:
                    continue
                visited.add(edge)
                path = [start, neighbor]
                previous, current = start, neighbor
                while current not in stops:
                    check_deadline(deadline, 'simplify_chain_extraction')
                    following = next(iter(adjacency[current] - {previous}))
                    visited.add(_edge(current, following))
                    path.append(following)
                    previous, current = current, following
                if path[0] == path[-1]:
                    continue
                bends = sum((a[0] == b[0]) != (b[0] == c[0])
                            for a, b, c in zip(path, path[1:], path[2:]))
                if not bends:
                    continue
                if path[-1] < path[0]:
                    path.reverse()
                length = sum(abs(a[0]-b[0]) + abs(a[1]-b[1])
                             for a, b in zip(path, path[1:]))
                result.append({'net': net, 'path': tuple(path), 'bends': bends,
                               'length': length, 'segments': topology['segments']})
    return sorted(result, key=lambda chain: (-chain['bends'], -chain['length'],
                                              chain['path'], str(chain['net'])))


def _replacements(path):
    a, b = path[0], path[-1]
    choices = [[(a, b)]] if a[0] == b[0] or a[1] == b[1] else [
        [(a, (a[0], b[1])), ((a[0], b[1]), b)],
        [(a, (b[0], a[1])), ((b[0], a[1]), b)]]
    original = {_edge(p, q) for p, q in zip(path, path[1:])}
    return sorted({tuple(sorted(_edge(p, q) for p, q in choice if p != q))
                   for choice in choices
                   if {_edge(p, q) for p, q in choice if p != q} != original})


def _shortcut_candidates(scene, routed, deadline=None):
    for chain in _chains(scene, routed, deadline):
        check_deadline(deadline, 'simplify_shortcut_generation')
        path = chain['path']
        removed = {_edge(a, b) for a, b in zip(path, path[1:])}
        base = [segment for segment in chain['segments'] if _edge(*segment) not in removed]
        for replacement in _replacements(path):
            yield {'net': chain['net'], 'segments': base + list(replacement),
                   'shortcut_identity': (chain['net'], tuple(chain['segments']), replacement)}


def _three_segment_candidates(scene, routed, deadline=None):
    # Keep extraction lazy: old operations finish before this is first resumed.
    yield from three_segment_candidates(scene, routed, _chains(scene, routed, deadline), deadline)


def _candidate_stream(scene, routed, first_operation=0, deadline=None, *, with_patterns=False):
    """Give each operation one candidate in turn on this immutable incumbent."""
    iterators = {
        0: iter(_shortcut_candidates(scene, routed, deadline)),
        1: iter(reattachment_candidates(scene, routed, deadline)),
    }
    operation_count = 3 if with_patterns else 2
    if with_patterns:
        iterators[2] = iter(_three_segment_candidates(scene, routed, deadline))
    turn = first_operation % operation_count
    while iterators:
        check_deadline(deadline, 'simplify_candidate_generation')
        if turn in iterators:
            try:
                proposal = next(iterators[turn])
            except StopIteration:
                del iterators[turn]
            else:
                yield OPERATIONS[turn], proposal
        turn = (turn + 1) % operation_count


def _shape_improves(length, bends, old_length, old_bends):
    """Require no regression and a strict gain in length or bend count."""
    return (bends <= old_bends and length <= old_length + EPS
            and (bends < old_bends or length < old_length - EPS))


def _improves(metrics, incumbent):
    return (_shape_improves(metrics['wirelength'], metrics['bends'],
                            incumbent['wirelength'], incumbent['bends'])
            and metrics['crossings'] <= incumbent['crossings'])


def _wire_metrics(segments, deadline=None):
    topology = canonical_topology(segments, deadline=deadline)
    length = sum(abs(a[0]-b[0]) + abs(a[1]-b[1]) for a, b in topology['runs'])
    bends = 0
    for point, neighbors in topology['adjacency'].items():
        if len(neighbors) == 2:
            a, b = tuple(neighbors)
            bends += (a[0] == point[0]) != (b[0] == point[0])
    return length, bends


def simplify_routes(par, nets, placement, comp_index, routed_result, *, options=None, seconds=5):
    """Return the last completely verified shortcut result and JSON-safe report.

    ``routed_result`` must already be a fully validated three-item result. A
    defensive recheck shares the same deadline; expiry always retains that
    incumbent, never a partially checked trial. Other input errors propagate.
    """
    if type(seconds) not in (int, float) or not isfinite(seconds) or seconds < 0:
        raise ValueError('simplification seconds must be finite and non-negative')
    if not isinstance(routed_result, (tuple, list)) or len(routed_result) != 3:
        raise ValueError('simplification requires a validated route result')
    if not isinstance(routed_result[2], dict) or routed_result[2].get('valid') is not True:
        raise ValueError('simplification requires valid=True from complete validation')
    started = perf_counter()
    deadline = started + seconds
    incumbent = deepcopy(tuple(routed_result))
    report = {'version': VERSION, 'budget_seconds': seconds,
              'parameters': {'max_rounds': MAX_ROUNDS, 'max_candidates': MAX_CANDIDATES,
                             'max_targets_per_branch': MAX_TARGETS_PER_BRANCH,
                             'max_tracks_per_axis': MAX_TRACKS_PER_AXIS},
              'operation_order': list(OPERATIONS),
              'phase_order': ['chain_warmup', 'coordinated_refinement', 'three_segment_refinement'],
              'scheduling': 'preserve_v3_then_three_segment_refinement',
              'warmup_completed': False,
              'v3_completed': False,
              'before_metrics': deepcopy(incumbent[1]), 'after_metrics': deepcopy(incumbent[1]),
              'changed_nets': [], 'changed_net_count': 0, 'rounds': 0,
              'candidate_counts': {'considered': 0, 'validated': 0, 'accepted': 0,
                                   'geometry_rejected': 0, 'objective_rejected': 0,
                                   'coverage_rejected': 0, 'cycle_rejected': 0},
              'original_revalidated': False, 'converged': False,
              'stop_reason': 'disabled' if seconds == 0 else 'round_budget'}
    report['operations'] = {name: dict(report['candidate_counts']) for name in OPERATIONS}
    report['accepted_changes'] = []
    report['topology_rebuilds'] = 0
    changed = set()
    if seconds == 0:
        report['elapsed_seconds'] = round(perf_counter()-started, 6)
        return (*incumbent, report)
    try:
        scene = build_scene(par, nets, placement, comp_index, options=options, deadline=deadline)
        checked, actual, analysis = _verify(par, nets, placement, comp_index,
                                            incumbent[0], scene, deadline=deadline)
        original_by_net = dict(checked)
        if any(not _covers_fixed(scene, net, original_by_net.get(net, ()), deadline)
               for net in scene.pins):
            raise ValueError('The original route does not cover every fixed pin segment')
        incumbent = (checked, actual, incumbent[2])
        report['before_metrics'] = deepcopy(actual)
        report['original_revalidated'] = True
        first_operation = 0
        warmup = True
        patterns_enabled = False
        for round_number in range(1, MAX_ROUNDS + 1):
            report['rounds'] = round_number
            changed_this_round = False
            warmup_seen = set()
            while True:
                check_deadline(deadline, 'simplify_candidate_generation')
                accepted = False
                # Every generator below reads exactly this accepted geometry.
                # Coordinated refinement discards all rejected identities after
                # a commit. Warmup keeps the original per-round shortcut policy
                # before allowing new branch moves to change its opportunities.
                seen = warmup_seen if warmup else set()
                by_net = dict(incumbent[0])
                net_facts = {}
                proposals = ((('chain_shortcut', proposal) for proposal in
                              _shortcut_candidates(scene, incumbent[0], deadline))
                             if warmup else _candidate_stream(
                                 scene, incumbent[0], first_operation, deadline,
                                 with_patterns=patterns_enabled))
                for operation, proposal in proposals:
                    check_deadline(deadline, 'simplify_candidate')
                    net = proposal['net']
                    geometry = tuple(sorted({_edge(a, b)
                        for a, b in proposal['segments'] if tuple(a) != tuple(b)}))
                    identity = (proposal.get('shortcut_identity',
                        (net, tuple(by_net[net]), geometry)) if warmup else (net, geometry))
                    if identity in seen:
                        continue
                    seen.add(identity)
                    counts = report['candidate_counts']
                    op_counts = report['operations'][operation]
                    if counts['considered'] >= MAX_CANDIDATES:
                        report['stop_reason'] = 'candidate_budget'
                        raise _StopSimplifying

                    def count(key):
                        counts[key] += 1
                        op_counts[key] += 1

                    count('considered')
                    if net not in net_facts:
                        net_facts[net] = (_cycle_rank(by_net[net], deadline),
                                         *_wire_metrics(by_net[net], deadline))
                    old_rank, old_length, old_bends = net_facts[net]
                    candidate = _trim_tails(scene, net, proposal['segments'], deadline)
                    if not _covers_fixed(scene, net, candidate, deadline):
                        count('coverage_rejected')
                        continue
                    if _cycle_rank(candidate, deadline) > old_rank:
                        count('cycle_rejected')
                        continue
                    length, bends = _wire_metrics(candidate, deadline)
                    if not _shape_improves(length, bends, old_length, old_bends):
                        count('objective_rejected')
                        continue
                    trial = [(name, candidate if name == net else segments)
                             for name, segments in incumbent[0]]
                    try:
                        count('validated')
                        routed, metrics, analysis = _verify(
                            par, nets, placement, comp_index, trial, scene, deadline=deadline)
                    except RoutingValidationError:
                        count('geometry_rejected')
                        continue
                    if not _improves(metrics, incumbent[1]):
                        count('objective_rejected')
                        continue
                    validation = deepcopy(incumbent[2])
                    validation.update(valid=True, parameters=asdict(scene.options),
                        signal_nets=len(scene.pins), violations=[],
                        power_connections_checked=analysis['power_connections_checked'])
                    change = {'operation': operation, 'net': net,
                              'phase': ('chain_warmup' if warmup else 'three_segment_refinement'
                                        if patterns_enabled else 'coordinated_refinement'),
                              'before_metrics': deepcopy(incumbent[1]),
                              'after_metrics': deepcopy(metrics)}
                    for key in ('source', 'source_pin', 'component', 'pin',
                                'old_attachment', 'new_attachment',
                                'old_branch_length', 'new_branch_length',
                                'target', 'track_axis', 'track_coordinate'):
                        if key in proposal:
                            change[key] = deepcopy(proposal[key])
                    report['accepted_changes'].append(change)
                    incumbent = (routed, metrics, validation)
                    count('accepted')
                    report['topology_rebuilds'] += 1
                    first_operation = (0 if warmup else (OPERATIONS.index(operation) + 1) % 3
                                       if patterns_enabled else 1 - OPERATIONS.index(operation))
                    changed.add(net)
                    changed_this_round = accepted = True
                    break
                if not accepted and not warmup and not patterns_enabled:
                    # Exhausting both old generators on this latest topology
                    # establishes the v3 incumbent before new templates can
                    # change its opportunities. Continue in this round so the
                    # new phase shares, rather than resets, the same limits.
                    report['v3_completed'] = True
                    report['v3_metrics'] = deepcopy(incumbent[1])
                    patterns_enabled = True
                    first_operation = 2
                    continue
                if not accepted:
                    break
            if not changed_this_round:
                if warmup:
                    # Only switch after the original per-round search has
                    # reached a stable result. All rounds and candidates count
                    # against the same existing limits and deadline.
                    report['warmup_completed'] = True
                    report['warmup_metrics'] = deepcopy(incumbent[1])
                    warmup = False
                    first_operation = 1
                    continue
                report.update(stop_reason='no_improvement', converged=True)
                break
    except _StopSimplifying:
        pass
    except RoutingFailure as exc:
        if exc.diagnostics.get('reason') != 'quality_time_budget':
            raise
        report['stop_reason'] = 'time_budget'
    report.update(after_metrics=deepcopy(incumbent[1]), changed_nets=sorted(changed),
                  changed_net_count=len(changed), elapsed_seconds=round(perf_counter()-started, 6))
    return (*incumbent, report)


class _StopSimplifying(Exception):
    """An internal bounded-search stop, never a validation success."""
