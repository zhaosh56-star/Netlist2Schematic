"""Reuse simple, safe module access paths only after a branch search fails.

No positions, escapes or keepouts change. A small set of straight/L candidates
connects real exits to the existing same-net tree. This is deliberately not a
second graph search, and it shares the caller's recovery deadline.
"""
from netlist2schematic.routing.routing_rules import (
    analyze_routes, canonical_topology, check_deadline, on_segment)

VERSION = 'module-access-patterns-v1'


def _paths(scene, net, pin, tree, prepared, deadline):
    start=pin.escape
    def free(point):
        return not any(r[0]<=point[0]<=r[2] and r[1]<=point[1]<=r[3]
                       for _,r in scene.body_rects)
    if not free(start) or not scene.vertex_check(net,start,prepared)[0]:
        return
    if any(on_segment(start,*segment) for segment in tree):
        yield 0.,()
        return
    # A pin without a fixed escape is represented by a zero-length root.
    # Canonical runs omit it, but it is still a real attachment goal.
    goals={a for a,b in tree if a==b}
    for a,b in canonical_topology(tree,deadline=deadline)['runs']:
        goals.update((a,b))
        goals.add(tuple(min(max(start[k],min(a[k],b[k])),max(a[k],b[k])) for k in (0,1)))
    ordered=sorted(goals,key=lambda p:(sum(abs(p[k]-start[k]) for k in (0,1)),p))
    seen=set()
    for goal in ordered[:8]:
        check_deadline(deadline,'module_access_candidates')
        if not free(goal) or not scene.vertex_check(net,goal,prepared)[0]:
            continue
        for middle in ((goal[0],start[1]),(start[0],goal[1])):
            points=[start]
            for point in (middle,goal):
                if point!=points[-1]:points.append(point)
            path=tuple(zip(points,points[1:]))
            if not path or path in seen:
                continue
            seen.add(path)
            if pin.point!=pin.escape and sum((points[1][k]-start[k])*pin.direction[k] for k in (0,1))<0:
                continue
            if any(not free(p) or not scene.vertex_check(net,p,prepared)[0] for p in points[1:-1]):
                continue
            crossings=0
            for a,b in path:
                okay,count,_=scene.edge_check(net,a,b,prepared)
                if not okay:break
                crossings+=count
            else:
                length=sum(abs(a[0]-b[0])+abs(a[1]-b[1]) for a,b in path)
                cost=length+scene.options.bend_cost*(len(path)-1)+scene.options.crossing_cost*crossings
                yield (cost,path)


def connect_tree(scene, net, tree, pins, occupied, *, deadline=None):
    """Return a complete net or None; partial attempts never escape this call."""
    report=dict(version=VERSION,net=net,attempted=False,accepted=False,candidates=0,
                connected_branches=0,stop_reason='not_module_boundary')
    if net not in getattr(scene,'module_port_nets',()):
        return None,report
    report.update(attempted=True,stop_reason='no_safe_pattern')
    current=list(tree)
    prepared=scene.prepare_occupied(occupied)
    remaining=list(pins)
    while remaining:
        check_deadline(deadline,'module_access')
        def distance(pin):
            return min(sum(max(min(a[k],b[k])-pin.escape[k],0,pin.escape[k]-max(a[k],b[k]))
                           for k in (0,1)) for a,b in current)
        remaining.sort(key=lambda pin:(distance(pin),pin.point,pin.pin))
        pin=remaining.pop(0)
        if any(on_segment(pin.point,*segment) for segment in current):
            continue
        accepted=None
        for _,path in sorted(_paths(scene,net,pin,current,prepared,deadline)):
            check_deadline(deadline,'module_access_validation')
            report['candidates']+=1
            trial=current+list(path)+([pin.stub] if pin.point!=pin.escape else [])
            routes={name:list(lines) for name,lines in occupied.items()}
            routes[net]=trial
            if any(net in fault.get('nets',()) for fault in
                   analyze_routes(list(routes.items()),scene,deadline=deadline)['violations']):
                continue
            accepted=canonical_topology(trial,pins=scene.all_pins.get(net,()),deadline=deadline)['segments']
            break
        if accepted is None:
            return None,report
        current=accepted
        report['connected_branches']+=1
    # The returned net must include every original terminal, not just the
    # requested suffix of the branch queue.
    topology=canonical_topology(current,pins=scene.all_pins.get(net,()),deadline=deadline)
    connected=set();pending=[scene.pins[net][0].point]
    while pending:
        point=pending.pop()
        if point in connected:continue
        connected.add(point);pending.extend(topology['adjacency'].get(point,()))
    if not all(pin.point in connected for pin in scene.pins[net]):
        report['stop_reason']='incomplete_net'
        return None,report
    report.update(accepted=True,stop_reason='complete_checked_net')
    return current,report
