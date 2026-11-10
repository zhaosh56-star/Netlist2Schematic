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
from route_tracks import MAX_TRACK_CANDIDATES_PER_CHAIN, track_compaction_candidates
from routing_rules import (EPS, PreparedOccupancy, build_scene, canonical_topology,
                           check_deadline, intersection, on_segment)
from sa_routing import RoutingValidationError, _verify

VERSION = 'orthogonal-shortcuts-v8'
MAX_ROUNDS = 4
MAX_CANDIDATES = 512
OPERATIONS = ('chain_shortcut', 'leaf_reattachment', 'three_segment_shortcut',
              'track_compaction')
PHASES = ('chain_warmup', 'coordinated_refinement', 'three_segment_refinement',
          'track_compaction_refinement')


def _chain_priority(chain):
    source, target = chain['path'][0], chain['path'][-1]
    span = abs(source[0]-target[0]) + abs(source[1]-target[1])
    return (-max(0, chain['length']-span), -chain['length'], -chain['bends'],
            chain['path'], str(chain['net']))


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
    return sorted(result, key=_chain_priority)


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
    # Extraction starts only when this operation receives its reserved turn.
    yield from three_segment_candidates(scene, routed, _chains(scene, routed, deadline), deadline)


def _track_compaction_candidates(scene, routed, deadline=None):
    yield from track_compaction_candidates(scene, routed, _chains(scene, routed, deadline), deadline)


def _candidate_stream(scene, routed, first_operation=0, deadline=None, *, with_patterns=False,
                      with_tracks=False, active_count=None, stage_report=None):
    """Give each operation one candidate in turn on this immutable incumbent."""
    factories = (_shortcut_candidates, reattachment_candidates,
                 _three_segment_candidates, _track_compaction_candidates)
    operation_count = (active_count if active_count is not None else
                       4 if with_tracks else 3 if with_patterns else 2)
    iterators = {index: None for index in range(operation_count)}
    turn = first_operation % operation_count
    while iterators:
        check_deadline(deadline, 'simplify_candidate_generation')
        if turn in iterators:
            if stage_report is not None:
                stage_report['generation_calls'] += 1
                stage_report['generation_by_operation'][OPERATIONS[turn]] += 1
                stage_report['last_operation'] = OPERATIONS[turn]
            try:
                if iterators[turn] is None:
                    iterators[turn] = iter(factories[turn](scene, routed, deadline))
                proposal = next(iterators[turn])
            except StopIteration:
                del iterators[turn]
                if stage_report is not None:
                    stage_report['exhausted_operations'].append(OPERATIONS[turn])
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


def _crossing_clearance_conflict(scene, net, candidate, occupied, deadline=None):
    """Find a definite quiet-zone violation at a real crossing only.

    Use the complete post-pruning net: a raw connector can have corners or
    branches that disappear on same-net reuse or tail removal. Real source
    pins are semantic anchors; escapes and artificial cuts are not. This is
    the same proper-crossing/Chebyshev predicate as analyze_routes, without
    giving ordinary parallel corners the stronger A* vertex keepout.
    """
    own = canonical_topology(candidate, pins=scene.all_pins.get(net, ()), deadline=deadline)
    prepared = scene.prepare_occupied(occupied)
    operations = 0
    for owner in sorted(prepared.topologies):
        check_deadline(deadline, 'simplify_crossing_precheck')
        if owner == net:
            continue
        foreign = prepared.topologies[owner]
        structures = own['structure_points'] | foreign['structure_points']
        for a, b in own['runs']:
            for c, d in foreign['runs']:
                operations += 1
                if operations % 64 == 0:
                    check_deadline(deadline, 'simplify_crossing_precheck')
                if (a[0] == b[0]) == (c[0] == d[0]):
                    continue
                point = intersection(a, b, c, d)
                if point is None or point in (a, b, c, d):
                    continue
                # Endpoint contacts, fixed crossings and other geometry stay
                # with the original full validator. Only reject the exact
                # crossing_clearance subset whose evidence is established here.
                for structure in sorted(structures):
                    distance = max(abs(point[0]-structure[0]), abs(point[1]-structure[1]))
                    if distance < scene.options.cross_clearance-EPS:
                        return {'kind': 'crossing_clearance', 'nets': sorted((net, owner)),
                                'point': point, 'structure_point': structure,
                                'distance': distance,
                                'required_distance': scene.options.cross_clearance}
    check_deadline(deadline, 'simplify_crossing_precheck')
    return None


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
                             'max_tracks_per_axis': MAX_TRACKS_PER_AXIS,
                             'max_track_candidates_per_chain': MAX_TRACK_CANDIDATES_PER_CHAIN},
              'operation_order': list(OPERATIONS),
              'phase_order': list(PHASES),
              'scheduling': 'reserved_stages_v6',
              'track_generation': {'version': 'chain_round_robin_v1',
                                   'prefilter': 'scene.edge_check',
                                   'max_candidates_per_chain': MAX_TRACK_CANDIDATES_PER_CHAIN,
                                   'occupancy': 'immutable_shared_prepared'},
              'warmup_completed': False,
              'v3_completed': False,
              'v4_completed': False,
              'before_metrics': deepcopy(incumbent[1]), 'after_metrics': deepcopy(incumbent[1]),
              'changed_nets': [], 'changed_net_count': 0, 'rounds': 0,
              'candidate_counts': {'considered': 0, 'validated': 0, 'accepted': 0,
                                   'geometry_rejected': 0, 'objective_rejected': 0,
                                   'coverage_rejected': 0, 'cycle_rejected': 0,
                                   'crossing_rejected': 0},
              'candidate_precheck': {'version': 'canonical_crossing_clearance_v1',
                                     'scope': 'all_operations',
                                     'position': 'post_trim_before_full_validation',
                                     'attempted': 0, 'passed': 0, 'rejected': 0,
                                     'examples': []},
              'original_revalidated': False, 'converged': False,
              'stop_reason': 'disabled' if seconds == 0 else 'round_budget'}
    report['operations'] = {name: dict(report['candidate_counts']) for name in OPERATIONS}
    report['stages'] = [
        {'phase': phase, 'activated_operation': OPERATIONS[index],
         'time_fraction': (index+1)/len(OPERATIONS),
         'candidate_limit': (MAX_CANDIDATES*(index+1)+len(OPERATIONS)-1)//len(OPERATIONS),
         'deadline_seconds': None, 'status': 'not_started', 'elapsed_seconds': 0.0,
         'candidate_counts': dict(report['candidate_counts']),
         'generation_calls': 0, 'generation_timeout': 0, 'validation_timeout': 0,
         'screening_timeout': 0,
         'generation_by_operation': {name: 0 for name in OPERATIONS[:index+1]},
         'exhausted_operations': [], 'duplicate_candidates': 0}
        for index, phase in enumerate(PHASES)]
    report['duplicate_candidates'] = 0
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
        # Only fully accepted geometry supplies foreign occupancy. A trial
        # that passes validation but fails the objective must not replace it.
        precheck_occupancy = PreparedOccupancy(analysis['topologies'])
        report['before_metrics'] = deepcopy(actual)
        report['original_revalidated'] = True
        work_started = perf_counter()
        remaining = max(0.0, deadline-work_started)
        for stage in report['stages']:
            stage['deadline_seconds'] = work_started-started + remaining*stage['time_fraction']
        # Rejections are reusable across operations and stages only while the
        # entire accepted scene is unchanged. A foreign-net edit can turn an
        # earlier rejected shortcut into a legal one, so every commit clears it.
        seen = set()
        net_facts = {}
        for phase_index, stage in enumerate(report['stages'][:MAX_ROUNDS]):
            check_deadline(deadline, 'simplify_stage_start')
            if report['candidate_counts']['considered'] >= MAX_CANDIDATES:
                report['stop_reason'] = 'candidate_budget'
                break
            report['rounds'] = phase_index+1
            stage_started = perf_counter()
            stage_deadline = min(deadline, started+stage['deadline_seconds'])
            before_counts = dict(report['candidate_counts'])
            stage['before_metrics'] = deepcopy(incumbent[1])
            stage['status'] = 'running'
            first_operation = phase_index
            work = 'generation'
            pending_identity = None
            try:
                while True:
                    by_net = dict(incumbent[0])
                    proposals = iter(_candidate_stream(scene, incumbent[0], first_operation,
                        stage_deadline, active_count=phase_index+1, stage_report=stage))
                    accepted = False
                    while True:
                        work = 'generation'
                        pending_identity = None
                        check_deadline(stage_deadline, 'simplify_candidate_generation')
                        if report['candidate_counts']['considered'] >= stage['candidate_limit']:
                            raise _StageQuota
                        try:
                            operation, proposal = next(proposals)
                        except StopIteration:
                            break
                        check_deadline(stage_deadline, 'simplify_candidate')
                        work = 'screening'
                        net = proposal['net']
                        candidate = _trim_tails(scene, net, proposal['segments'], stage_deadline)
                        # Merged runs make the identity independent of artificial
                        # cuts, reversed segment endpoints and proposal type.
                        identity = (net, tuple(canonical_topology(
                            candidate, deadline=stage_deadline)['runs']))
                        if identity in seen:
                            stage['duplicate_candidates'] += 1
                            report['duplicate_candidates'] += 1
                            continue
                        seen.add(identity)
                        pending_identity = identity
                        counts = report['candidate_counts']
                        op_counts = report['operations'][operation]

                        def count(key):
                            counts[key] += 1
                            op_counts[key] += 1

                        count('considered')
                        if net not in net_facts:
                            net_facts[net] = (_cycle_rank(by_net[net], stage_deadline),
                                             *_wire_metrics(by_net[net], stage_deadline))
                        old_rank, old_length, old_bends = net_facts[net]
                        if not _covers_fixed(scene, net, candidate, stage_deadline):
                            count('coverage_rejected')
                            continue
                        if _cycle_rank(candidate, stage_deadline) > old_rank:
                            count('cycle_rejected')
                            continue
                        length, bends = _wire_metrics(candidate, stage_deadline)
                        if not _shape_improves(length, bends, old_length, old_bends):
                            count('objective_rejected')
                            continue
                        precheck = report['candidate_precheck']
                        precheck['attempted'] += 1
                        conflict = _crossing_clearance_conflict(
                            scene, net, candidate, precheck_occupancy, stage_deadline)
                        if conflict is not None:
                            count('crossing_rejected')
                            precheck['rejected'] += 1
                            if len(precheck['examples']) < 16:
                                precheck['examples'].append({**conflict, 'net': net,
                                    'operation': operation, 'phase': stage['phase']})
                            continue
                        precheck['passed'] += 1
                        trial = [(name, candidate if name == net else segments)
                                 for name, segments in incumbent[0]]
                        try:
                            work = 'validation'
                            count('validated')
                            routed, metrics, analysis = _verify(par, nets, placement,
                                comp_index, trial, scene, deadline=stage_deadline)
                        except RoutingValidationError:
                            count('geometry_rejected')
                            continue
                        # Even a substituted validator may return after its
                        # deadline. Never commit a trial across that boundary.
                        check_deadline(stage_deadline, 'simplify_commit')
                        if not _improves(metrics, incumbent[1]):
                            count('objective_rejected')
                            continue
                        validation = deepcopy(incumbent[2])
                        validation.update(valid=True, parameters=asdict(scene.options),
                            signal_nets=len(scene.pins), violations=[],
                            power_connections_checked=analysis['power_connections_checked'])
                        change = {'operation': operation, 'net': net, 'phase': stage['phase'],
                                  'before_metrics': deepcopy(incumbent[1]),
                                  'after_metrics': deepcopy(metrics)}
                        for key in ('source', 'source_pin', 'component', 'pin',
                                    'old_attachment', 'new_attachment',
                                    'old_branch_length', 'new_branch_length',
                                    'target', 'track_axis', 'track_coordinate',
                                    'old_track_coordinate', 'moved_segment_index',
                                    'old_chain_length', 'new_chain_length'):
                            if key in proposal:
                                change[key] = deepcopy(proposal[key])
                        report['accepted_changes'].append(change)
                        incumbent = (routed, metrics, validation)
                        precheck_occupancy = PreparedOccupancy(analysis['topologies'])
                        count('accepted')
                        report['topology_rebuilds'] += 1
                        first_operation = (OPERATIONS.index(operation)+1) % (phase_index+1)
                        changed.add(net)
                        seen.clear()
                        net_facts.clear()
                        pending_identity = None
                        accepted = True
                        break
                    if not accepted:
                        stage['status'] = 'converged'
                        if phase_index < 3:
                            prefix = ('warmup', 'v3', 'v4')[phase_index]
                            report[prefix+'_completed'] = True
                            report[prefix+'_metrics'] = deepcopy(incumbent[1])
                        else:
                            report.update(stop_reason='no_improvement', converged=True)
                        break
            except _StageQuota:
                stage['status'] = ('candidate_budget' if
                    report['candidate_counts']['considered'] >= MAX_CANDIDATES else 'quota_yield')
                if stage['status'] == 'candidate_budget':
                    report['stop_reason'] = 'candidate_budget'
            except RoutingFailure as exc:
                if exc.diagnostics.get('reason') != 'quality_time_budget':
                    raise
                # An interrupted check establishes no reusable rejection. A
                # later stage with more time may safely retry this proposal.
                if pending_identity is not None:
                    seen.discard(pending_identity)
                stage[work+'_timeout'] += 1
                stage['timeout_stage'] = exc.diagnostics.get('stage')
                stage['status'] = ('global_timeout' if phase_index == len(OPERATIONS)-1
                                   or perf_counter() >= deadline else 'quota_yield')
                if stage['status'] == 'global_timeout':
                    report['stop_reason'] = 'time_budget'
            finally:
                stage['elapsed_seconds'] = round(perf_counter()-stage_started, 6)
                stage['candidate_counts'] = {key: value-before_counts[key]
                    for key, value in report['candidate_counts'].items()}
                stage['after_metrics'] = deepcopy(incumbent[1])
            if stage['status'] in ('global_timeout', 'candidate_budget'):
                break
    except RoutingFailure as exc:
        if exc.diagnostics.get('reason') != 'quality_time_budget':
            raise
        report['stop_reason'] = 'time_budget'
    for stage in report['stages']:
        if stage['status'] == 'not_started':
            stage['skip_reason'] = report['stop_reason']
    report.update(after_metrics=deepcopy(incumbent[1]), changed_nets=sorted(changed),
                  changed_net_count=len(changed), elapsed_seconds=round(perf_counter()-started, 6))
    return (*incumbent, report)


class _StageQuota(Exception):
    """Yield to the next operation without extending the common work budget."""
