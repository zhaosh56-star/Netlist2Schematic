"""Bounded repair of artificial routing ports, with immutable original geometry.

This stage is deliberately separate from the ordinary router.  An extended port
is ordinary wire, never an enlarged pin corridor or a changed validation rule.
Every transaction starts from the accepted checkpoint, and only a fully checked
whole circuit may leave this module.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from itertools import product
from math import isfinite
from time import perf_counter

from netlist2schematic.layout.layout_quality import evaluate_quality
from netlist2schematic.routing.orthogonal_router import RoutingFailure, _axes, _distance, _find_path, _priority
from netlist2schematic.routing.routing_rules import (EPS, RoutingOptions, Scene, analyze_routes, build_scene,
                           canonical_topology, check_deadline, on_segment,
                           point_in_rect)
from netlist2schematic.common.sa_common import load_local
from netlist2schematic.routing.sa_routing import RoutingValidationError, _verify


def _free(scene, point):
    return not any(point_in_rect(point, rect) for _, rect in
                   scene.body_rects + scene.marker_rects)


def _occupied(scene, completed):
    occupied = {name: list(segments) for name, segments in scene.fixed_segments.items()}
    for name, segments in completed.items():
        occupied.setdefault(name, []).extend(segments)
    return occupied


def _anchors(scene, net, pin, occupied, deadline):
    """Nearest four quiet points along a pin's true outward direction.

    The declared pin and its fixed escape do not move.  An extension is tested
    as one ordinary segment against the *original* scene, including all foreign
    pins, fixed stubs, branches, bodies and markers.
    """
    prepared = scene.prepare_occupied(occupied)
    blocked = Counter()
    result = []
    if pin.point == pin.escape:
        return result, blocked
    dx, dy = pin.direction
    if (dx == 0) == (dy == 0):
        return result, blocked
    dx, dy = (0 if dx == 0 else (1 if dx > 0 else -1),
              0 if dy == 0 else (1 if dy > 0 else -1))
    step = scene.options.grid_step
    radius = 8 * max(scene.options.cross_clearance, scene.options.center_spacing, step)
    # Keep exact geometry events as well as the grid tracks. A custom quiet
    # distance need not be a multiple of grid_step; its boundary is legal and
    # must not disappear through rounding (for example step=3, quiet=13).
    axis = 0 if dx else 1
    direction = dx if dx else dy
    distances = {number*step for number in range(1, int(radius // step)+1)}
    coordinates = set()
    for owner, topology in prepared.topologies.items():
        if owner == net:
            continue
        for segment in topology['runs']:
            for point in segment:
                for offset in (0, -scene.options.cross_clearance, scene.options.cross_clearance,
                               -scene.options.center_spacing, scene.options.center_spacing):
                    coordinates.add(point[axis]+offset)
    for owner, points in scene.all_pins.items():
        if owner != net:
            for point in points:
                coordinates.update((point[axis]-scene.options.cross_clearance,
                                    point[axis]+scene.options.cross_clearance))
    for _, rect in scene.body_rects + scene.marker_rects:
        coordinates.update((rect[axis]-step, rect[axis+2]+step))
    distances.update((coordinate-pin.escape[axis])*direction for coordinate in coordinates
                     if EPS < (coordinate-pin.escape[axis])*direction <= radius+EPS)
    for distance in sorted(distances):
        check_deadline(deadline, 'local_rescue_anchors')
        point = pin.escape[0] + dx * distance, pin.escape[1] + dy * distance
        okay, _, owners = scene.edge_check(net, pin.escape, point, prepared)
        blocked.update(owners)
        if not okay:
            # Every longer collinear prefix contains this forbidden interval.
            break
        okay, _, owners = scene.vertex_check(net, point, prepared)
        blocked.update(owners)
        if _free(scene, point) and okay:
            result.append(point)
            if len(result) == 4:
                break
    return result, blocked


def _safe_goals(scene, net, tree, occupied, margin, deadline, extra_points=()):
    """Check graph attachments before asking A* to explore an empty goal set."""
    xs, ys = _axes(scene, occupied, tree, margin, extra_points=extra_points)
    prepared = scene.prepare_occupied(occupied)
    blocked = Counter()
    for a, b in tree:
        if (a[0] == b[0] and a[0] not in xs) or (a[1] == b[1] and a[1] not in ys):
            continue
        coordinates = ((a[0], y) for y in ys if min(a[1], b[1]) <= y <= max(a[1], b[1])) \
            if a[0] == b[0] else \
            ((x, a[1]) for x in xs if min(a[0], b[0]) <= x <= max(a[0], b[0]))
        for point in coordinates:
            check_deadline(deadline, 'local_rescue_goal_check')
            if not _free(scene, point):
                continue
            okay, _, owners = scene.vertex_check(net, point, prepared)
            blocked.update(owners)
            if okay:
                return True, blocked
    return False, blocked


def _goal_choices(scene, net, tree, occupied, margin, deadline):
    choices, blocked = [], Counter()
    if _safe_goals(scene, net, tree, occupied, margin, deadline)[0]:
        choices.append(None)
    topology = canonical_topology(tree, pins=scene.all_pins.get(net, ()), deadline=deadline)
    true_pins = {point for points in scene.all_pins.values() for point in points}
    for pin in sorted(scene.pins[net], key=lambda p: (p.escape, p.component, p.pin)):
        leaf = pin.escape
        if (leaf in true_pins or pin.point == leaf
                or len(topology['adjacency'].get(leaf, ())) != 1):
            continue
        # Only the original artificial leaf is eligible, never a route bend,
        # real pin or branch that happens to lie near a blocked attachment.
        anchors, owners = _anchors(scene, net, pin, occupied, deadline)
        blocked.update(owners)
        for anchor in anchors:
            choices.append((leaf, anchor))
    if not choices:
        _, owners = _safe_goals(scene, net, tree, occupied, margin, deadline)
        blocked.update(owners)
    return choices, blocked


def _tree_score(scene, net, tree, occupied, deadline):
    """Compare actual assembled wire, including both port-extension prefixes."""
    topology = canonical_topology(tree, pins=scene.all_pins.get(net, ()), deadline=deadline)
    length = sum(abs(a[0]-b[0]) + abs(a[1]-b[1]) for a, b in topology['runs'])
    bends = 0
    for point, neighbors in topology['adjacency'].items():
        if len(neighbors) == 2:
            a, b = tuple(neighbors)
            bends += (a[0] == point[0]) != (b[0] == point[0])
    # A not-yet-routed foreign net contributes immutable stubs to the collision
    # model, but those artificial escape endpoints are not completed open ends.
    # Treating them as routes here would invent an extra 12-unit final keepout.
    active = {}
    for name, segments in occupied.items():
        if name == net:
            continue
        fixed = canonical_topology(scene.fixed_segments.get(name, ()), deadline=deadline)['runs']
        if canonical_topology(segments, deadline=deadline)['runs'] != fixed:
            active[name] = segments
    analysis = analyze_routes(list({**active, net: topology['segments']}.items()),
                              scene, deadline=deadline)
    relevant = [item for item in analysis['violations'] if net in item.get('nets', ())]
    crossings = analysis.get('crossings', 0)
    return (length + scene.options.bend_cost*bends + scene.options.crossing_cost*crossings,
            topology['segments'], relevant)


def _route_local_net(scene, net, occupied, margin, statistics, deadline, report):
    """Connect one full net; retain at most eight source/target plans per branch."""
    pins = sorted(scene.pins[net], key=lambda pin: (pin.point, pin.component, pin.pin))
    if not pins:
        return [], Counter(), Counter(), None
    root, remaining = pins[0], list(pins[1:])
    tree = [root.stub] if root.point != root.escape else [(root.point, root.point)]
    prepared = scene.prepare_occupied(occupied)
    blockers, direct = Counter(), Counter()
    checks = {'free': {}, 'edge': {}, 'turn': {}}
    while remaining:
        check_deadline(deadline, 'local_rescue_branch')
        remaining.sort(key=lambda pin: (_distance(pin.escape, tree), pin.point,
                                        pin.component, pin.pin))
        pin = remaining.pop(0)
        if any(on_segment(pin.point, a, b) for a, b in tree):
            continue
        branch = {'net': net, 'component': pin.component, 'pin': pin.pin,
                  'point': pin.point, 'escape': pin.escape}
        goals, goal_blocked = _goal_choices(scene, net, tree, occupied, margin, deadline)
        direct.update(goal_blocked)
        blockers.update(goal_blocked)
        if not goals:
            return None, blockers, direct, {**branch, 'reason': 'no_safe_goal'}
        sources = []
        okay, _, owners = scene.vertex_check(net, pin.escape, prepared)
        direct.update(owners)
        blockers.update(owners)
        if _free(scene, pin.escape) and okay:
            sources.append(pin.escape)
        anchors, owners = _anchors(scene, net, pin, occupied, deadline)
        direct.update(owners)
        blockers.update(owners)
        sources.extend(anchors[:max(0, 4-len(sources))])
        if not sources:
            return None, blockers, direct, {**branch, 'reason': 'no_safe_source'}
        plans = sorted(product(sources, goals), key=lambda pair: (
            abs(pair[0][0]-pin.escape[0]) + abs(pair[0][1]-pin.escape[1])
            + (0 if pair[1] is None else abs(pair[1][0][0]-pair[1][1][0])
               + abs(pair[1][0][1]-pair[1][1][1])), pair[0], str(pair[1])))[:8]
        best = None
        for source, extension in plans:
            check_deadline(deadline, 'local_rescue_plan')
            target = tree + ([] if extension is None else [extension])
            extra = [source] + ([] if extension is None else list(extension))
            if not _safe_goals(scene, net, target, occupied, margin, deadline, extra)[0]:
                continue
            paths, owners = _find_path(scene, net, source, target, occupied, margin, [],
                statistics, checks, prepared, deadline=deadline, extra_points=extra,
                initial_direction=pin.direction if pin.point != pin.escape else None)
            blockers.update(owners)
            report['port_plans'] += 1
            if paths is None:
                continue
            assembled = list(tree) + list(paths)
            if pin.point != pin.escape:
                assembled.append(pin.stub)
            if source != pin.escape:
                assembled.append((pin.escape, source))
            # The search may meet the original tree or an interior point of the
            # extension. Never retain an unused tail as a spurious open branch.
            attachment = paths[-1][1] if paths else source
            if extension is not None and on_segment(attachment, *extension):
                if attachment != extension[0]:
                    assembled.append((extension[0], attachment))
            score, assembled, violations = _tree_score(scene, net, assembled, occupied, deadline)
            if violations:
                for item in violations:
                    blockers.update(owner for owner in item.get('nets', ()) if owner != net)
                continue
            choice = (score, tuple(assembled))
            if best is None or choice < best[0]:
                best = (choice, assembled, {'source': source, 'target_extension': extension,
                                           'attachment': attachment, 'score': score})
            # A clean ordinary interface keeps exactly the old shortest-search
            # choice. Extra searches are useful only where a port was repaired.
            if source == pin.escape and extension is None:
                break
        if best is None:
            return None, blockers, direct, {**branch, 'reason': 'no_path'}
        tree = best[1]
        report['branches'].append({**branch, **best[2]})
    score, tree, violations = _tree_score(scene, net, tree, occupied, deadline)
    if violations:
        for item in violations:
            blockers.update(owner for owner in item.get('nets', ()) if owner != net)
        return None, blockers, direct, {'net': net, 'reason': 'candidate_validation',
                                       'violations': violations}
    return tree, blockers, direct, None


def _interface_count(scene, net, occupied, deadline):
    prepared = scene.prepare_occupied(occupied)
    total = 0
    for pin in scene.pins[net]:
        check_deadline(deadline, 'local_rescue_order')
        total += bool(_free(scene, pin.escape)
                      and scene.vertex_check(net, pin.escape, prepared)[0])
        total += len(_anchors(scene, net, pin, occupied, deadline)[0])
    return total


def _transaction(scene, frozen, order, margin, statistics, deadline, report):
    # Nothing in this dictionary, the occupancy or the query caches survives a
    # failed transaction. In particular, the normal router's history is absent.
    completed = deepcopy(frozen)
    for net in order:
        occupied = _occupied(scene, completed)
        result, blockers, direct, failure = _route_local_net(
            scene, net, occupied, margin, statistics, deadline, report)
        if result is None:
            return None, {'failed_net': net, 'branch': failure,
                          'blocking_nets': dict(blockers), 'direct_blockers': dict(direct)}
        completed[net] = result
    return completed, None


def rescue_routing(context, checkpoint, *, routing_options=None, seconds=60):
    """Resume a failed checkpoint without moving any device or weakening rules.

    Returns the copied five-item placement context plus ``(routes, metrics,
    validation)``. Checkpoint objects stay private; diagnostics contain only
    JSON-safe scalar, list and dictionary values.
    """
    if type(seconds) not in (int, float) or not isfinite(seconds) or seconds <= 0:
        raise ValueError('local rescue seconds must be positive and finite')
    if not isinstance(context, (list, tuple)) or len(context) != 5:
        raise ValueError('local rescue requires a five-item placement context')
    if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get('scene'), Scene):
        raise ValueError('local rescue requires a private original-scene checkpoint')
    started = perf_counter()
    deadline = started + seconds
    placement, comps, nets, index, metadata = deepcopy(context)
    if not isinstance(metadata, dict):
        raise ValueError('placement metadata must be an object')
    options = deepcopy(routing_options if routing_options is not None else checkpoint['options'])
    if not isinstance(options, RoutingOptions):
        raise ValueError('routing_options must be RoutingOptions')
    par = load_local('place_and_route', 'place-and-route.py')
    try:
        scene = build_scene(par, nets, placement, index, options=options, deadline=deadline)
    except (RoutingFailure, RoutingValidationError) as exc:
        diagnostics = deepcopy(getattr(exc, 'diagnostics', {}))
        diagnostics.update(routing_stage='local_rescue', parameters=asdict(options),
            local_rescue={'version': 'local-ports-v1', 'status': 'failed',
                          'budget_seconds': seconds,
                          'elapsed_seconds': round(perf_counter()-started, 6),
                          'source_candidate': metadata.get('candidate_id'),
                          'transactions': []})
        raise RoutingFailure('Local rescue scene preparation failed: ' + str(exc),
                             diagnostics) from exc
    if scene != checkpoint['scene']:
        raise ValueError('local rescue checkpoint does not match the unchanged placement scene')
    names = set(scene.pins)
    frozen = deepcopy(checkpoint.get('completed', {}))
    failed = checkpoint.get('failed_net')
    pending = list(checkpoint.get('pending', ()))
    if (failed not in names or not set(frozen) <= names or failed in frozen
            or len(pending) != len(set(pending)) or set(pending) & (set(frozen) | {failed})
            or set(frozen) | {failed} | set(pending) != names):
        raise ValueError('local rescue checkpoint has inconsistent net membership')
    margin = checkpoint.get('margin')
    if type(margin) not in (int, float) or not isfinite(margin) or margin <= 0:
        raise ValueError('local rescue checkpoint has an invalid fixed search margin')
    statistics = {'graph_nodes': 0, 'attempts': 0, 'expanded_states': 0}
    report = {'version': 'local-ports-v1', 'budget_seconds': seconds,
              'source_candidate': metadata.get('candidate_id'), 'margin': margin,
              'layout_preserved': True, 'fixed_port_geometry_preserved': True,
              'port_radius': 8*max(options.cross_clearance, options.center_spacing, options.grid_step),
              'port_plans': 0, 'branches': [], 'transactions': []}
    validation = None
    try:
        check_deadline(deadline, 'local_rescue_start')
        # Existing accepted nets are immutable in the first continuation.
        order = [failed] + pending
        initial = {'number': 0, 'removed': [], 'order': order, 'status': 'running'}
        report['transactions'].append(initial)
        completed, failure = _transaction(scene, frozen, order, margin, statistics,
                                           deadline, report)
        initial['status'] = 'valid' if completed is not None else 'failed'
        if failure:
            initial['failure'] = deepcopy(failure)
        seen = {((), tuple(order))}
        # Up to max_rounds independent rip-up transactions. Both deterministic
        # orders are tried; each starts from the same accepted checkpoint.
        for number in range(1, options.max_rounds + 1):
            if completed is not None:
                break
            check_deadline(deadline, 'local_rescue_transaction')
            direct = failure['direct_blockers']
            counts = failure['blocking_nets']
            ranked = sorted((name for name in frozen if name in counts or name in direct),
                key=lambda name: (name not in direct, -direct.get(name, 0),
                                  -counts.get(name, 0), _priority(scene, name)))
            possible = []
            removal_sets = [ranked[:2], ranked[:1], ranked[1:3], []]
            for removed in removal_sets:
                first = failure['failed_net']
                # A previously accepted net may itself fail while being rebuilt
                # in a prior transaction. Its old route must not survive as a
                # frozen obstacle during either order of the next attempt.
                excluded = set(removed) | {first}
                reduced = {name: routes for name, routes in frozen.items() if name not in excluded}
                todo = [name for name in names if name not in reduced]
                failed_first = [first] + sorted((name for name in todo if name != first),
                                               key=lambda name: _priority(scene, name))
                occupancy = _occupied(scene, reduced)
                few_ports = sorted(todo, key=lambda name: (
                    _interface_count(scene, name, occupancy, deadline), _priority(scene, name)))
                for chosen in (failed_first, few_ports):
                    identity = tuple(sorted(set(frozen) & excluded)), tuple(chosen)
                    if identity not in seen:
                        possible.append((identity, reduced, chosen))
                if possible:
                    break
            if not possible:
                break
            identity, reduced, chosen = possible[0]
            seen.add(identity)
            item = {'number': number, 'removed': list(identity[0]), 'order': chosen,
                    'failed_net': failure['failed_net'],
                    'status': 'running'}
            report['transactions'].append(item)
            completed, failure = _transaction(scene, reduced, chosen, margin, statistics,
                                               deadline, report)
            item['status'] = 'valid' if completed is not None else 'failed'
            if failure:
                item['failure'] = deepcopy(failure)
        if completed is None:
            raise RoutingFailure('Local rescue did not find a fully valid route',
                                 {'reason': 'local_rescue_exhausted', 'last_failure': failure})
        routed = [(net, completed[net]) for net in sorted(completed)]
        # The original scene is the sole authority. No temporary escape, halo,
        # protection zone or validation exemption was changed during search.
        routed, metrics, analysis = _verify(par, nets, placement, index, routed, scene,
                                            deadline=deadline)
        check_deadline(deadline, 'local_rescue_quality')
        quality = evaluate_quality(par, placement, comps, nets, index, routed, metrics,
                                   metadata.get('structs', []))
        check_deadline(deadline, 'local_rescue_quality')
        elapsed = round(perf_counter()-started, 6)
        report.update(elapsed_seconds=elapsed, search=deepcopy(statistics), status='valid')
        validation = {'valid': True, 'router': 'orthogonal-a-star-v1',
                      'routing_stage': 'local_rescue', 'parameters': asdict(options),
                      'signal_nets': len(scene.pins), 'routing_seconds': elapsed,
                      'power_connections_checked': analysis['power_connections_checked'],
                      'search': deepcopy(statistics), 'violations': [],
                      'pushed_tracks': 0, 'postprocess_rejections': []}
    except (RoutingFailure, RoutingValidationError) as exc:
        report.update(elapsed_seconds=round(perf_counter()-started, 6),
                      search=deepcopy(statistics), status='failed')
        diagnostics = deepcopy(getattr(exc, 'diagnostics', {}))
        diagnostics.update(routing_stage='local_rescue', local_rescue=report,
                           parameters=asdict(options))
        raise RoutingFailure('Local rescue failed: ' + str(exc), diagnostics) from exc
    for key in ('routes', 'metrics', 'validation'):
        metadata.pop(key, None)
    if 'quality' in metadata:
        report['source_quality'] = metadata.pop('quality')
    metadata['routing_stage'] = 'local_rescue'
    metadata['local_rescue'] = report
    metadata['quality'] = {
        'policy': 'balanced-v1', 'budget_seconds': seconds,
        'elapsed_seconds': report['elapsed_seconds'], 'stop_reason': 'local_rescue_complete',
        'selected_candidate': str(metadata.get('candidate_id', 'unknown')),
        'score': quality['score'], 'details': quality,
        'candidates': [{'candidate_id': str(metadata.get('candidate_id', 'unknown')),
                       'seed': metadata.get('candidate_seed'), 'routing_stage': 'local_rescue',
                       'status': 'valid', 'metrics': deepcopy(metrics), 'score': quality['score'],
                       'routing_seconds': validation['routing_seconds']}],
    }
    return placement, comps, nets, index, metadata, (routed, metrics, validation)
