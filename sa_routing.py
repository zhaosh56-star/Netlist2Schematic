"""Final-route topology validation, conservative cleanup, and measurements."""
from __future__ import annotations

from collections import defaultdict
from math import isfinite
from dataclasses import asdict
from time import perf_counter


def on_segment(point, a, b):
    return ((a[0] == b[0] == point[0] and min(a[1], b[1]) <= point[1] <= max(a[1], b[1]))
            or (a[1] == b[1] == point[1] and min(a[0], b[0]) <= point[0] <= max(a[0], b[0])))


def normalize_segments(segments, deadline=None):
    """Split same-net crossings/T joins and remove duplicate drawn intervals."""
    segments = [(tuple(a), tuple(b)) for a, b in segments if tuple(a) != tuple(b)]
    for a, b in segments:
        if any(len(point) != 2 or any(type(value) not in (int, float) or not isfinite(value)
                                     for value in point) for point in (a, b)):
            raise ValueError(f'Invalid route coordinates: {a} -> {b}')
        if a[0] != b[0] and a[1] != b[1]:
            raise ValueError(f"Non-orthogonal route segment: {a} -> {b}")
    from routing_rules import check_deadline
    check_deadline(deadline, 'normalize_routes')
    points = {p for segment in segments for p in segment}
    operations = 0
    for i, (a, b) in enumerate(segments):
        for c, d in segments[i + 1:]:
            operations += 1
            if operations % 256 == 0:
                check_deadline(deadline, 'normalize_routes')
            if (a[0] == b[0]) == (c[0] == d[0]):
                continue
            vertical, horizontal = ((a, b), (c, d)) if a[0] == b[0] else ((c, d), (a, b))
            point = (vertical[0][0], horizontal[0][1])
            if on_segment(point, *vertical) and on_segment(point, *horizontal):
                points.add(point)
    result = set()
    for a, b in segments:
        check_deadline(deadline, 'normalize_routes')
        cuts = sorted(p for p in points if on_segment(p, a, b))
        result.update((p, q) for p, q in zip(cuts, cuts[1:]) if p != q)
    return sorted(result)


def normalize_routes(routed, deadline=None):
    return [(net, normalize_segments(segments, deadline=deadline)) for net, segments in routed]


def routing_errors(routed, nets, placement, comp_index, deadline=None):
    """Verify every signal net against its original pin anchors, not only wires."""
    from routing_rules import check_deadline
    errors = []
    by_net = dict(routed)
    for net, members in nets.items():
        check_deadline(deadline, 'connectivity_validation')
        segments = by_net.get(net, [])
        anchors = {placement.pin_world(comp_index[cid], pin) for cid, pin in members}
        if not segments:
            if len(anchors) > 1:
                errors.append(f"{net}: missing wire between {len(anchors)} pins")
            continue
        adjacency = defaultdict(set)
        for a, b in segments:
            if a[0] != b[0] and a[1] != b[1]:
                errors.append(f"{net}: non-orthogonal segment {a} -> {b}")
            adjacency[a].add(b)
            adjacency[b].add(a)
        for point in anchors:
            if not any(on_segment(point, a, b) for a, b in segments):
                errors.append(f"{net}: disconnected original pin at {point}")
        pending = [next(iter(adjacency))]
        visited = set(pending)
        while pending:
            for point in adjacency[pending.pop()]:
                if point not in visited:
                    visited.add(point)
                    pending.append(point)
        if len(visited) != len(adjacency):
            errors.append(f"{net}: wire graph has disconnected components")
    extra = set(by_net) - set(nets)
    if extra:
        errors.append(f"Unexpected routed nets: {sorted(extra)}")
    return errors


def measure_routes(par, routed, placement, scene=None, deadline=None):
    """Measure actual unique drawn segments after all accepted cleanup stages."""
    from routing_rules import canonical_topology, check_deadline
    runs = [(net, canonical_topology(segments, deadline=deadline)['runs']) for net, segments in routed]
    flat = [(net, a, b) for net, segments in runs for a, b in segments]
    result = dict(wirelength=0, crossings=0, bends=0, body_pen=0, overlap=0, proximity=0)
    for _, a, b in flat:
        result['wirelength'] += abs(a[0] - b[0]) + abs(a[1] - b[1])
    operations = 0
    for i, (net, a, b) in enumerate(flat):
        for other, c, d in flat[i + 1:]:
            operations += 1
            if operations % 256 == 0:
                check_deadline(deadline, 'route_metrics')
            if net != other:
                result['crossings'] += int(par.seg_cross(a, b, c, d))
                result['overlap'] += par.seg_overlap_len(a, b, c, d)
                result['proximity'] += par.seg_parallel_prox(a, b, c, d,
                    gap=scene.options.center_spacing if scene is not None else 10)
    for _, segments in routed:
        check_deadline(deadline, 'route_metrics')
        adjacency = defaultdict(set)
        for a, b in segments:
            adjacency[a].add(b)
            adjacency[b].add(a)
        for point, neighbors in adjacency.items():
            if len(neighbors) == 2:
                a, b = sorted(neighbors)
                result['bends'] += int((a[0] == point[0]) != (b[0] == point[0]))
    rectangles = []
    for i, comp in enumerate(placement.comps):
        cx, cy = placement.pos_of(i)
        box = par.GEO_DOC[comp['symbol']]['orientations'][placement.orient[i]]['viewBoxAABB']
        rectangles.append((cx + box['x'], cy + box['y'],
                           cx + box['x'] + box['width'], cy + box['y'] + box['height']))
    for _, a, b in flat:
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        for x0, y0, x1, y1 in rectangles:
            operations += 1
            if operations % 256 == 0:
                check_deadline(deadline, 'route_metrics')
            halo = (x0 - par.MARGIN, y0 - par.MARGIN, x1 + par.MARGIN, y1 + par.MARGIN)
            hit = par.seg_hits_rect(a, b, halo)
            through = (x0 < mid[0] < x1 and y0 < mid[1] < y1)
            own_endpoint = any(x0 < p[0] < x1 and y0 < p[1] < y1 for p in (a, b))
            result['body_pen'] += int(through or (hit and not own_endpoint))
    return result


class RoutingValidationError(ValueError):
    def __init__(self, message, diagnostics):
        super().__init__(message)
        self.diagnostics = diagnostics


def _verify(par, nets, placement, comp_index, routed, scene, deadline=None):
    from routing_rules import analyze_routes, canonical_topology, check_deadline
    check_deadline(deadline, 'final_validation')
    routed = [(net, canonical_topology(segments,
                pins=[pin.point for pin in scene.pins.get(net, [])], deadline=deadline)['segments'])
              for net, segments in routed]
    routed = normalize_routes(routed, deadline=deadline)
    # Collapse the artificial cuts introduced by normalization once more.
    routed = [(net, canonical_topology(segments,
                pins=[pin.point for pin in scene.pins.get(net, [])], deadline=deadline)['segments'])
              for net, segments in routed]
    signal_nets = {net: members for net, members in nets.items()
                   if not par.power_marker_sym(net)}
    structure_errors = routing_errors(normalize_routes(routed, deadline=deadline), signal_nets,
                                     placement, comp_index, deadline=deadline)
    analysis = analyze_routes(routed, scene, deadline=deadline)
    if structure_errors or analysis['violations']:
        diagnostics = {'structure_errors': structure_errors,
                       **{key: value for key, value in analysis.items() if key != 'topologies'}}
        reasons = structure_errors + [str(item) for item in analysis['violations'][:5]]
        raise RoutingValidationError('Final route failed validation: ' + '; '.join(reasons), diagnostics)
    metrics = measure_routes(par, routed, placement, scene=scene, deadline=deadline)
    metrics['body_pen'] = analysis.get('body_violations', 0)
    if scene.power_connections is not None:
        from routing_rules import merge_runs
        metrics['power_wirelength'] = sum(abs(a[0]-b[0])+abs(a[1]-b[1])
            for net in scene.power_nets for a, b in merge_runs(scene.fixed_segments[net], deadline=deadline))
    check_deadline(deadline, 'final_validation')
    return routed, metrics, analysis


def validate_final_routes(par, nets, placement, comp_index, routed, options=None, deadline=None):
    """Recheck saved geometry; an old valid flag is never trusted."""
    from routing_rules import build_scene
    scene = build_scene(par, nets, placement, comp_index, options=options, deadline=deadline)
    routed, metrics, analysis = _verify(par, nets, placement, comp_index, routed, scene, deadline=deadline)
    return routed, metrics, {
        'valid': True, 'router': 'orthogonal-a-star-v1',
        'parameters': asdict(scene.options), 'signal_nets': len(scene.pins),
        'power_connections_checked': analysis['power_connections_checked'],
        'violations': [], 'pushed_tracks': 0, 'postprocess_rejections': []}


def final_route(par, nets, placement, comp_index, options=None, deadline=None):
    """Route frozen placement through one shared rule set; no legacy cleanup."""
    from routing_rules import build_scene
    from orthogonal_router import route_scene
    started = perf_counter()
    scene = build_scene(par, nets, placement, comp_index, options=options, deadline=deadline)
    routed, diagnostics = (route_scene(scene) if deadline is None else
                           route_scene(scene, deadline=deadline))
    routed, metrics, analysis = _verify(par, nets, placement, comp_index, routed, scene, deadline=deadline)
    return routed, metrics, {
        'valid': True, 'router': 'orthogonal-a-star-v1',
        'parameters': asdict(scene.options), 'signal_nets': len(scene.pins),
        'routing_seconds': round(perf_counter() - started, 6),
        'power_connections_checked': analysis['power_connections_checked'],
        'search': diagnostics, 'violations': [],
        'pushed_tracks': 0, 'postprocess_rejections': []}
