"""Deterministic final orthogonal routing on a compressed coordinate graph.

This module is deliberately separate from the inexpensive route estimator used
inside simulated annealing. Component placements and pin portals are immutable.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import heapq
from itertools import count
from math import inf, isfinite
from time import perf_counter


class RoutingFailure(ValueError):
    def __init__(self, message, diagnostics=None):
        super().__init__(message)
        self.diagnostics = dict(diagnostics or {})


def _check_deadline(deadline, **diagnostics):
    if deadline is not None and perf_counter() >= deadline:
        raise RoutingFailure('Routing stopped at the quality time budget',
                             {**diagnostics, 'reason': 'quality_time_budget'})


def _on(point, a, b):
    return ((a[0] == b[0] == point[0] and min(a[1], b[1]) <= point[1] <= max(a[1], b[1]))
            or (a[1] == b[1] == point[1] and min(a[0], b[0]) <= point[0] <= max(a[0], b[0])))


def _distance(point, segments):
    x, y = point
    return min((max(min(a[0], b[0])-x, 0, x-max(a[0], b[0]))
                + max(min(a[1], b[1])-y, 0, y-max(a[1], b[1]))
                for a, b in segments), default=0)


def _compress(path):
    result = []
    for point in path:
        if result and point == result[-1]:
            continue
        if len(result) > 1:
            a, b = result[-2:]
            if (a[0] == b[0] == point[0]) or (a[1] == b[1] == point[1]):
                result[-1] = point
                continue
        result.append(point)
    return list(zip(result, result[1:]))


def _scene_rectangles(scene):
    return [rect for _, rect in scene.body_rects] + [rect for _, rect in scene.marker_rects]


def _axes(scene, occupied, tree, margin, extra_points=()):
    """Hanan-style coordinates plus clearance tracks and outer escape channels."""
    spacing = scene.options.center_spacing
    quiet = scene.options.cross_clearance
    step = scene.options.grid_step
    xs, ys = set(), set()
    for pins in scene.pins.values():
        for pin in pins:
            for point in (pin.point, pin.escape):
                xs.update((point[0], point[0]-quiet, point[0]+quiet))
                ys.update((point[1], point[1]-quiet, point[1]+quiet))
    for x0, y0, x1, y1 in _scene_rectangles(scene):
        xs.update((x0-step, x1+step))
        ys.update((y0-step, y1+step))
    for segments in list(occupied.values()) + [tree]:
        for a, b in segments:
            for point in (a, b):
                xs.update((point[0], point[0]-spacing, point[0]+spacing,
                           point[0]-quiet, point[0]+quiet))
                ys.update((point[1], point[1]-spacing, point[1]+spacing,
                           point[1]-quiet, point[1]+quiet))
    for x, y in extra_points:
        xs.add(x)
        ys.add(y)
    x0, y0, x1, y1 = scene.bounds
    low_x, high_x, low_y, high_y = x0-margin, x1+margin, y0-margin, y1+margin
    xs.update((low_x, high_x)); ys.update((low_y, high_y))
    return sorted(x for x in xs if low_x <= x <= high_x), sorted(y for y in ys if low_y <= y <= high_y)


def _history_cost(a, b, history):
    total = 0
    for x0, y0, x1, y1 in history:
        if a[0] == b[0] and x0 <= a[0] <= x1:
            total += max(0, min(max(a[1], b[1]), y1)-max(min(a[1], b[1]), y0))
        elif a[1] == b[1] and y0 <= a[1] <= y1:
            total += max(0, min(max(a[0], b[0]), x1)-max(min(a[0], b[0]), x0))
    return 50 * total


def _find_path(scene, net, start, tree, occupied, margin, history, statistics,
               checks=None, prepared=None, deadline=None, *, extra_points=(),
               initial_direction=None):
    """A* carries heading; only actual turns and attachments need quiet zones."""
    _check_deadline(deadline, net=net, stage='branch_search')
    xs, ys = _axes(scene, occupied, tree, margin, extra_points=extra_points)
    nodes = len(xs)*len(ys)
    statistics['graph_nodes'] = max(statistics['graph_nodes'], nodes)
    statistics['attempts'] += 1
    if nodes > scene.options.max_nodes:
        raise RoutingFailure('Compressed coordinate graph exceeds the node budget',
                             {'reason': 'graph_node_budget', 'net': net, 'graph_nodes': nodes,
                              'max_nodes': scene.options.max_nodes})
    x_index, y_index = {x:i for i,x in enumerate(xs)}, {y:i for i,y in enumerate(ys)}
    if start[0] not in x_index or start[1] not in y_index:
        return None, Counter()
    obstacles = _scene_rectangles(scene)
    checks = checks if checks is not None else {'free': {}, 'edge': {}, 'turn': {}}
    point_cache, edge_cache, turn_cache = checks['free'], checks['edge'], checks['turn']
    prepared = prepared if prepared is not None else scene.prepare_occupied(occupied)

    def free(point):
        if point not in point_cache:
            point_cache[point] = not any(x0 <= point[0] <= x1 and y0 <= point[1] <= y1
                                        for x0,y0,x1,y1 in obstacles)
        return point_cache[point]

    def vertex(point):
        if point not in turn_cache:
            turn_cache[point] = scene.vertex_check(net, point, prepared)
        return turn_cache[point]

    if not free(start):
        return None, Counter()
    allowed, _, rejected_by = vertex(start)
    if not allowed:
        return None, Counter(rejected_by)
    initial_heading = -1
    if initial_direction is not None:
        if (len(initial_direction) != 2
                or any(type(value) not in (int, float) or not isfinite(value)
                       for value in initial_direction)
                or (initial_direction[0] == 0) == (initial_direction[1] == 0)):
            raise ValueError('The initial routing direction must be a non-zero axis vector')
        initial_heading = 0 if initial_direction[0] else 1
    initial = (x_index[start[0]], y_index[start[1]], initial_heading)
    distances, parents = {initial: 0.0}, {}
    serial = count()
    heap = [(_distance(start, tree), 0.0, next(serial), initial)]
    blockers = Counter()
    expanded = 0
    while heap:
        _, cost, _, state = heapq.heappop(heap)
        if cost != distances.get(state):
            continue
        ix, iy, heading = state
        point = (xs[ix], ys[iy])
        expanded += 1
        if expanded % 256 == 0:
            # Count the interrupted branch as well as completed branches.
            if deadline is not None and perf_counter() >= deadline:
                statistics['expanded_states'] += expanded
                _check_deadline(deadline, net=net, stage='branch_search',
                                blocking_nets=dict(blockers))
        if expanded > scene.options.max_expansions:
            statistics['expanded_states'] += expanded
            raise RoutingFailure('A* did not find a route within its state budget',
                                 {'reason':'state_budget', 'net':net,
                                  'expanded_states':expanded,
                                  'max_expansions':scene.options.max_expansions,
                                  'blocking_nets':dict(blockers)})
        if any(_on(point, a, b) for a,b in tree) and vertex(point)[0]:
            path = [point]
            while state in parents:
                state = parents[state]
                path.append((xs[state[0]], ys[state[1]]))
            statistics['expanded_states'] += expanded
            return _compress(list(reversed(path))), blockers
        # Positive and negative directions have the same bend heading. U-turns
        # cannot improve a positive-cost path, so heading need only store axis.
        for dx, dy, next_heading in ((-1,0,0),(1,0,0),(0,-1,1),(0,1,1)):
            jx, jy = ix+dx, iy+dy
            if not (0 <= jx < len(xs) and 0 <= jy < len(ys)):
                continue
            target = (xs[jx], ys[jy])
            if (initial_direction is not None and point == start
                    and ((target[0]-point[0])*initial_direction[0]
                         + (target[1]-point[1])*initial_direction[1]) < 0):
                continue
            if not free(target):
                continue
            turning = heading != -1 and next_heading != heading
            if turning:
                okay, _, blocked = vertex(point)
                if not okay:
                    blockers.update(blocked)
                    continue
            key = (point, target)
            if key not in edge_cache:
                edge_cache[key] = scene.edge_check(net, point, target, prepared)
            okay, crossings, blocked = edge_cache[key]
            if not okay:
                blockers.update(blocked)
                continue
            length = abs(point[0]-target[0]) + abs(point[1]-target[1])
            candidate = (cost + length + scene.options.bend_cost*turning
                         + scene.options.crossing_cost*crossings
                         + _history_cost(point,target,history))
            next_state = (jx,jy,next_heading)
            if candidate < distances.get(next_state, inf):
                distances[next_state] = candidate
                parents[next_state] = (ix,iy,heading)
                heapq.heappush(heap,(candidate+_distance(target,tree),candidate,next(serial),next_state))
    statistics['expanded_states'] += expanded
    return None, blockers


def _route_net(scene, net, occupied, margin, history, statistics, shared_free=None,
               deadline=None, *, _rescue_state=None):
    from routing_rules import canonical_topology
    _check_deadline(deadline, net=net, stage='net_start')
    pins = sorted(scene.pins[net], key=lambda pin:(pin.point,pin.component,pin.pin))
    if not pins:
        return [], Counter()
    first, remaining = pins[0], list(pins[1:])
    tree = [(first.point, first.escape)] if first.point != first.escape else []
    if not tree:
        # A degenerate root still gives a precise attachment goal.
        tree = [(first.escape,first.escape)]
    blockers = Counter()
    # Foreign occupancy is immutable while this one net grows. Reuse these
    # geometry queries across its branch searches; tree membership is separate.
    checks = {'free': shared_free if shared_free is not None else {}, 'edge': {}, 'turn': {}}
    prepared = scene.prepare_occupied(occupied)
    while remaining:
        _check_deadline(deadline, net=net, stage='branch_start')
        remaining.sort(key=lambda pin:(_distance(pin.escape,tree),pin.point,pin.component,pin.pin))
        pin = remaining.pop(0)
        if _rescue_state is not None:
            _rescue_state['branch'] = {'net': net, 'component': pin.component,
                                       'pin': pin.pin, 'point': pin.point,
                                       'escape': pin.escape}
        if any(_on(pin.point,a,b) for a,b in tree):
            continue
        paths, blocked = _find_path(scene,net,pin.escape,tree,occupied,margin,history,statistics,
                                   checks,prepared,deadline=deadline)
        blockers.update(blocked)
        if paths is None:
            statistics['failed_branch'] = {'net': net, 'component': pin.component,
                                           'pin': pin.pin, 'point': pin.point,
                                           'escape': pin.escape}
            return None, blockers
        tree.extend(paths)
        if pin.point != pin.escape:
            tree.append((pin.point,pin.escape))
        tree = canonical_topology(tree, pins=[pin.point for pin in pins])['segments']
    return canonical_topology(tree, pins=[pin.point for pin in pins])['segments'], blockers


def _priority(scene, net):
    pins=scene.pins[net]
    points=[pin.point for pin in pins]
    hpwl=(max(p[0] for p in points)-min(p[0] for p in points)
          + max(p[1] for p in points)-min(p[1] for p in points)) if points else 0
    return -len(pins),-hpwl,net


def route_scene(scene, deadline=None):
    """Return signal routes and diagnostics, or raise a bounded RoutingFailure."""
    from routing_rules import analyze_routes
    started=perf_counter()
    diagnostics={'router':'orthogonal-a-star-v1','expanded_states':0,'graph_nodes':0,
                 'reroute_rounds':0,'attempts':0,'elapsed_seconds':0.0}
    initial_order=sorted(scene.pins,key=lambda net:_priority(scene,net))
    last_failure={}
    history=[]
    order=list(initial_order)
    shared_free={}
    rescue_state=None
    try:
        _check_deadline(deadline, stage='routing_start')
        for margin in scene.options.margins:
            _check_deadline(deadline, stage='margin_start', margin=margin)
            completed={}
            pending=list(order)
            rounds=0
            while pending:
                _check_deadline(deadline, stage='net_start', margin=margin)
                net=pending.pop(0)
                occupied={name:list(segments) for name,segments in scene.fixed_segments.items()}
                for name,segments in completed.items():
                    occupied.setdefault(name,[]).extend(segments)
                # Keep references to immutable accepted routes until failure;
                # the potentially larger deep copy is never on the success path.
                rescue_state={'completed':dict(completed), 'failed_net':net,
                              'pending':list(pending), 'margin':margin, 'branch':None}
                result,blockers=_route_net(scene,net,occupied,margin,history,diagnostics,
                                           shared_free,deadline=deadline,
                                           _rescue_state=rescue_state)
                failure={'reason':'no_path','net':net,
                         'branch':diagnostics.get('failed_branch')}
                if result is not None:
                    trial={**completed,net:result}
                    # Analyze the actual connected geometry after each net;
                    # fixed foreign portals are checked by edge/vertex rules.
                    violations=analyze_routes(list(trial.items()),scene)['violations']
                    _check_deadline(deadline, net=net, stage='candidate_validation')
                    relevant=[item for item in violations if net in item.get('nets',())]
                    if relevant:
                        result=None
                        for item in relevant:
                            blockers.update(name for name in item.get('nets',()) if name!=net)
                        failure={'reason':'candidate_validation','net':net,'violations':relevant}
                if result is not None:
                    completed[net]=result
                    # A later deadline must not label this accepted net as the
                    # failure. A fresh checkpoint starts at the next real call.
                    rescue_state=None
                    continue
                movable=[name for name,_ in sorted(blockers.items(),key=lambda item:(-item[1],_priority(scene,item[0]) if item[0] in scene.pins else (0,0,item[0])))
                         if name in completed]
                last_failure={**failure,'blocking_nets':dict(blockers),'margin':margin}
                if diagnostics['reroute_rounds'] >= scene.options.max_rounds or not movable:
                    break
                removed=movable[:2]
                spacing=scene.options.center_spacing
                for name in removed:
                    for a,b in completed.pop(name):
                        history.append((min(a[0],b[0])-spacing,min(a[1],b[1])-spacing,
                                        max(a[0],b[0])+spacing,max(a[1],b[1])+spacing))
                pending=[net]+removed+[name for name in pending if name not in removed and name!=net]
                rounds+=1
                diagnostics['reroute_rounds']+=1
            else:
                _check_deadline(deadline, stage='final_validation')
                routed=[(net,completed[net]) for net in sorted(completed)]
                report=analyze_routes(routed,scene)
                _check_deadline(deadline, stage='final_validation')
                if not report['violations']:
                    diagnostics.pop('failed_branch',None)
                    diagnostics['elapsed_seconds']=round(perf_counter()-started,6)
                    diagnostics['margin']=margin
                    return routed,diagnostics
                last_failure={'reason':'final_validation','violations':report['violations'],'margin':margin}
            # Retry with a larger outer channel. A failed net gets first choice.
            failed=last_failure.get('net')
            order=([failed] if failed in scene.pins else [])+[net for net in initial_order if net!=failed]
        diagnostics.update(last_failure)
        diagnostics['elapsed_seconds']=round(perf_counter()-started,6)
        raise RoutingFailure('No route satisfying the geometric rules was found within the search budget',diagnostics)
    except RoutingFailure as exc:
        diagnostics.update(exc.diagnostics)
        diagnostics['elapsed_seconds']=round(perf_counter()-started,6)
        if rescue_state is None:
            diagnostics.setdefault('rescue_checkpoint_unavailable', 'no_active_net')
        failure=RoutingFailure(str(exc),diagnostics)
        failure._rescue_context=(None if rescue_state is None else
            {'scene':scene, **deepcopy(rescue_state), 'options':deepcopy(scene.options)})
        raise failure from exc
