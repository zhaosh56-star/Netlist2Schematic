"""Refine one-ended RLC/V/I modules around real ports in two dimensions.

This runs only after ordinary packing has produced a complete static-legal
incumbent. Templates remain rigid, active cells/axes stay fixed, and every
accepted move passes the original static and internal-wire checks. Straight
core exit channels are a soft estimate, never new routing keepouts.
"""
from collections import defaultdict
from copy import deepcopy
from heapq import heappop, heappush
from statistics import median
from time import perf_counter

from netlist2schematic.routing.routing_rules import (
    EPS, _escape, _inflate, analyze_routes, build_scene, canonical_topology,
    check_deadline, segment_distance, segment_hits_rect)

VERSION = 'terminal-module-2d-v1'
ROLES = frozenset(('rail_attachment', 'port_extension', 'terminal_bank'))


def is_terminal(group):
    return (group.get('role') in ROLES
            and sum(bool(group['anchors'][n]) for n in group['ends']) == 1)


def _channels(scene, guests):
    """Possible short core buses, using real exits and clear core bodies."""
    channels = set()
    for net, pins in scene.pins.items():
        pins = [p for p in pins if p.component not in guests]
        for axis in (0, 1):
            tracks = defaultdict(set)
            for pin in pins:
                tracks[pin.escape[axis]].add(pin.escape)
            for points in tracks.values():
                ordered = sorted(points)
                for a, b in zip(ordered, ordered[1:]):
                    if any(not scene.body_allowed(net, a, b, i, rect)
                           for i, rect in scene.body_rects if i not in guests):
                        continue
                    channels.add((net, a, b))
    return sorted(channels)


def _local_shapes(par, comps, geometry, options):
    bodies, markers, stubs = [], [], []
    for i, point in geometry[0].items():
        orient = geometry[1][i]
        definition = par.GEO_DOC[comps[i]['symbol']]['orientations'][orient]
        box = definition['viewBoxAABB']
        body = _inflate((point[0]+box['x'], point[1]+box['y'],
                         point[0]+box['x']+box['width'], point[1]+box['y']+box['height']), options.halo)
        bodies.append(body)
        for pin, net in comps[i]['nodes'].items():
            p = par.pin_pos(comps[i]['symbol'], orient, pin)
            p = tuple(point[k]+p[k] for k in (0, 1))
            item = next(p for p in definition['pins'] if p['name'] == pin)
            direction = par.DIR_VEC[item['direction']]
            symbol = par.power_marker_sym(net)
            if symbol:
                tip = p[0], p[1]+(-par.STUB_LEN if symbol == 'vdd-port' else par.STUB_LEN)
                box = par.GEO_DOC[symbol]['orientations'][0]['viewBoxAABB']
                markers.append(_inflate((tip[0]+box['x'], tip[1]+box['y'],
                    tip[0]+box['x']+box['width'], tip[1]+box['y']+box['height']), options.halo))
            else:
                stubs.append((net, p, _escape(p, direction, body, options.grid_step)))
    return bodies, markers, stubs


def _shift_rect(rect, target):
    return tuple(rect[k]+target[k % 2] for k in range(4))


def _overlap(a, b):
    return all(max(a[k], b[k]) < min(a[k+2], b[k+2])-EPS for k in (0, 1))


def _channel_risk(channels, bodies, stubs, clearance):
    hits = set()
    for number, (net, a, b) in enumerate(channels):
        if any(segment_hits_rect(a, b, rect) for rect in bodies):
            hits.add(number)
        if any(other != net and segment_distance(a, b, c, d) < clearance-EPS
               for other, c, d in stubs):
            hits.add(number)
    return len(hits)


def _interface_risk(scene, own, stubs, options):
    """Keep branching access between fixed exits on an unfinished live net.

    A two-pin local guide can seed its complete net. A multi-pin net still
    needs room to join the remaining tree; compacting two facing exits into
    a tiny gap can be static-legal but leave no safe graph attachment point.
    """
    risks = set()
    for net,_,escape in stubs:
        pins = scene.pins.get(net, ())
        if len(pins) <= 2:
            continue
        for pin in pins:
            if pin.component not in own and max(abs(escape[k]-pin.escape[k]) for k in (0,1)) < 2*options.cross_clearance-EPS:
                risks.add((net,pin.component,pin.pin))
    return len(risks)


def _rigid_move(before, after, own):
    if (before.cell_of != after.cell_of or before.orient != after.orient
            or before.column_x != after.column_x or before.row_y != after.row_y):
        return False
    shifts = {tuple(after.pos_of(i)[k]-before.pos_of(i)[k] for k in (0,1)) for i in own}
    return (len(shifts) == 1 and all(before.pos_of(i) == after.pos_of(i)
                                   for i in range(len(before.comps)) if i not in own))


def _targets(par, frame, comps, index, group, geometry, anchor, live, scene, channels, options, deadline):
    from netlist2schematic.plugins.passive_skeleton.placement import slot_trials, _point_exit, snap
    check_deadline(deadline, 'terminal_targets')
    first = next(slot_trials(par, frame, comps, index, group, options), None)
    if first is None:
        return []
    i = next(iter(geometry[0]))
    base = tuple(first.pos_of(i)[k]-geometry[0][i][k] for k in (0, 1))
    end = group['ends'].index(live)
    port = geometry[6]['ports'][end]
    exit_point = _point_exit(par, frame, index, anchor, options)
    own = {index[n] for n in group['members']}
    bodies, markers, stubs = _local_shapes(par, comps, geometry, options)
    obstacles = [r for i,r in scene.body_rects if i not in own]
    obstacles += [r for k,(_,r) in enumerate(scene.marker_rects)
                  if scene.marker_components[k] not in own]
    pitch = median([b-a for values in (frame.column_x, frame.row_y)
                    for a,b in zip(values, values[1:])]) if len(frame.column_x)+len(frame.row_y)>2 else 120.
    radius = 2*max(pitch, geometry[3]+2*options.halo)
    step = options.grid_step
    # Explore around the obstacles actually hit by a trial. Independently
    # clipping each coordinate list to the nearest values can throw away
    # every usable two-dimensional combination in a crowded local pocket.
    queue=[];seen=set();candidates=[]
    def add(raw):
        raw=tuple(snap(v,step) for v in raw)
        if raw in seen or any(abs(raw[k]-base[k]) > radius+EPS for k in (0,1)):
            return
        seen.add(raw)
        distance=sum(abs(raw[k]+port[k]-exit_point[k]) for k in (0,1))
        heappush(queue,(distance,raw))
    aligned=tuple(exit_point[k]-port[k] for k in (0,1))
    add(base);add(aligned);add((aligned[0],base[1]));add((base[0],aligned[1]))
    visited=0
    while queue and visited < 128:
        check_deadline(deadline, 'terminal_targets')
        distance,raw=heappop(queue);visited+=1
        shifted = [_shift_rect(r,raw) for r in bodies]
        shifted_markers = [_shift_rect(r,raw) for r in markers]
        blocked=[(shape,obstacle) for shape,moved in zip(bodies+markers,shifted+shifted_markers)
                 for obstacle in obstacles if _overlap(moved,obstacle)]
        if blocked:
            for shape,obstacle in blocked[:2]:
                for axis in (0,1):
                    for value in (obstacle[axis]-shape[axis+2]-step,
                                  obstacle[axis+2]-shape[axis]+step):
                        trial=list(raw);trial[axis]=value;add(trial)
            continue
        moved_stubs = [(net,tuple(c[k]+raw[k] for k in (0,1)),tuple(d[k]+raw[k] for k in (0,1)))
                       for net,c,d in stubs]
        risk = _channel_risk(channels,shifted,moved_stubs,options.cross_clearance)
        interface = _interface_risk(scene,own,moved_stubs,options)
        candidates.append(((interface,risk,distance,tuple(raw)),tuple(raw)))
        if len(candidates) >= 40:
            break
        for axis in (0,1):
            for sign in (-1,1):
                trial=list(raw);trial[axis]+=sign*options.cross_clearance;add(trial)
        if risk:
            for net,a,b in channels:
                for (other,c,d),(_,mc,md) in zip(stubs,moved_stubs):
                    if other==net or segment_distance(a,b,mc,md) >= options.cross_clearance-EPS:
                        continue
                    for axis in (0,1):
                        for value in (min(a[axis],b[axis])-options.cross_clearance-max(c[axis],d[axis])-step,
                                      max(a[axis],b[axis])+options.cross_clearance-min(c[axis],d[axis])+step):
                            trial=list(raw);trial[axis]=value;add(trial)
        if interface:
            for net,_,escape in stubs:
                if len(scene.pins.get(net,())) <= 2:continue
                for pin in scene.pins[net]:
                    if pin.component in own:continue
                    for axis in (0,1):
                        for sign in (-1,1):
                            trial=list(raw)
                            trial[axis]=pin.escape[axis]+sign*(2*options.cross_clearance+step)-escape[axis]
                            add(trial)
    return [target for _,target in sorted(candidates)[:32]]


def _score(par, frame, comps, nets, index, group, channels, options, deadline):
    from netlist2schematic.plugins.passive_skeleton.module_connections import guide_routes
    from netlist2schematic.plugins.passive_skeleton.placement import _point_exit
    scene = build_scene(par,nets,frame,index,options=options,deadline=deadline)
    guide = frame.passive_module_guides[group['id']]
    routes = {net:list(segments) for net,segments in scene.fixed_segments.items() if net not in scene.power_nets}
    for net,segments in guide_routes(frame,scene,internal_only=True).items():
        routes.setdefault(net,[]).extend(segments)
    length = bends = 0
    for net,segments in guide['routes'].items():
        if net in scene.power_nets:
            continue
        routes.setdefault(net,[]).extend(segments)
        topology = canonical_topology(segments,deadline=deadline)
        length += sum(abs(a[0]-b[0])+abs(a[1]-b[1]) for a,b in topology['runs'])
        for point,neighbors in topology['adjacency'].items():
            if len(neighbors) == 2:
                a,b = tuple(neighbors)
                bends += (a[0] == point[0]) != (b[0] == point[0])
    # All real hosts on a shared signal node participate, including its
    # multiple gates; a convenient single host cannot hide remote terminals.
    extension = 0.
    own = {index[n] for n in group['members']}
    for net in group['ends']:
        hosts = [frame.pin_world(index[n],p) for n,p in group['anchors'][net]]
        if not hosts or net in scene.power_nets:
            continue
        points = hosts+[frame.pin_world(index[n],p) for n,p in group['boundary_ports'][net]]
        hpwl = lambda pts:sum(max(p[k] for p in pts)-min(p[k] for p in pts) for k in (0,1))
        extension += hpwl(points)-hpwl(hosts)
    audit = analyze_routes(list(routes.items()),scene,deadline=deadline)
    bodies = [rect for i,rect in scene.body_rects if i in own]
    stubs = [(net,p.point,p.escape) for net,pins in scene.pins.items() for p in pins if p.component in own]
    risk = _channel_risk(channels,bodies,stubs,options.cross_clearance)
    interface = _interface_risk(scene,own,stubs,options)
    centered = 0.
    if group.get('source_node_anchor'):
        pins = [_point_exit(par,frame,index,t,options) for t in group['source_node_anchor']['terminals']]
        ports = guide['ports'];live=group['source_node_anchor']['net']
        centered = abs(ports[group['ends'].index(live)][0]-(min(p[0] for p in pins)+max(p[0] for p in pins))/2)
    energy = length+extension+options.bend_cost*bends+options.crossing_cost*audit['crossings']
    details = dict(access_conflicts=len(audit['violations']), interface_access_risks=interface,
        estimated_channel_obstructions=risk,
        centering_error=centered, guide_wirelength=length, host_span_extension=extension,
        bends=bends,crossings=audit['crossings'],estimated_cost=energy)
    return (details['access_conflicts'],interface,risk,centered,energy),details


def refine_terminals(par, frame, comps, nets, index, plan, options, *, deadline, max_trials):
    from netlist2schematic.plugins.passive_skeleton.placement import (
        _anchor_geometry,_geometry,_translated,_approach_paths,_guest_guards)
    from netlist2schematic.plugins.passive_skeleton.module_connections import check_guides
    from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
    from netlist2schematic.routing.orthogonal_router import RoutingFailure
    started = perf_counter()
    report = dict(version=VERSION,applied=False,trials=0,actions=[],modules=[],
        max_targets_per_module=32,max_trials_per_module=12,incumbent_validated=True,
        budget='remaining_original_packing_budget',stop_reason='no_improvement')
    current = frame
    coupled = {gid for f in plan['families'] for gid in f['groups']}
    groups = [g for g in plan['groups'] if is_terminal(g) and g['id'] not in coupled]
    fixed = set(getattr(frame,'fixed',())) | set(getattr(frame,'fixed_cells',{}))
    groups = [g for g in groups if not any(n in fixed or index[n] in fixed for n in g['members'])]
    report['coupled_families_preserved'] = sorted(coupled)
    report['eligible_count'] = len(groups)
    # Long existing approaches get first use of the remaining budget.
    def priority(g):
        segments = current.passive_module_guides[g['id']]['routes']
        distance = sum(abs(a[0]-b[0])+abs(a[1]-b[1]) for net,ss in segments.items()
                       if not par.power_marker_sym(net) for a,b in ss)
        return -distance,tuple(current.pin_world(index[n],p) for ts in g['anchors'].values() for n,p in ts)
    try:
        for group in sorted(groups,key=priority):
            check_deadline(deadline,'terminal_refinement')
            if report['trials'] >= max_trials:
                report['stop_reason']='candidate_budget';break
            vector,anchor,_,live = _anchor_geometry(par,current,comps,index,group,options)
            geometry = _geometry(par,current,comps,index,group,vector,options)
            scene = build_scene(par,nets,current,index,options=options,deadline=deadline)
            channels = _channels(scene,current.layout_guest_ids)
            before_key,before_metrics = _score(par,current,comps,nets,index,group,channels,options,deadline)
            module = dict(group=group['id'],members=list(group['members']),before=before_metrics,
                          trials=0,accepted=False)
            report['modules'].append(module)
            best,best_key,best_metrics = current,before_key,before_metrics
            try:
                for target in _targets(par,current,comps,index,group,geometry,anchor,live,scene,channels,options,deadline):
                    if module['trials'] >= 12 or report['trials'] >= max_trials:
                        break
                    trial = _translated(current,geometry,target,options.grid_step)
                    guide = trial.passive_module_guides[group['id']]
                    end = group['ends'].index(live)
                    for path in _approach_paths(par,trial,index,anchor,guide['ports'][end],options):
                        check_deadline(deadline,'terminal_refinement')
                        if module['trials'] >= 12 or report['trials'] >= max_trials:
                            break
                        candidate = deepcopy(trial)
                        candidate.passive_module_guides[group['id']]['routes'].setdefault(live,[]).extend(
                            (a,b) for a,b in path if a != b)
                        module['trials']+=1;report['trials']+=1
                        if not _rigid_move(current,candidate,{index[n] for n in group['members']}):
                            continue
                        if not _guest_guards(par,candidate,comps,index,plan['groups']):
                            continue
                        faults = _static_faults(par,candidate,nets,index,options,deadline)
                        if faults['count'] or faults['hard'] or check_guides(par,candidate,nets,index,options,deadline):
                            continue
                        key,metrics = _score(par,candidate,comps,nets,index,group,channels,options,deadline)
                        if key < best_key:
                            best,best_key,best_metrics = candidate,key,metrics
            except RoutingFailure as error:
                if error.diagnostics.get('reason') != 'quality_time_budget':raise
                report['stop_reason']='time_budget'
            if best is not current:
                action = dict(group=group['id'],members=list(group['members']),
                    before={n:list(current.pos_of(index[n])) for n in group['members']},
                    after={n:list(best.pos_of(index[n])) for n in group['members']},
                    before_metrics=before_metrics,after_metrics=best_metrics)
                current=best;report['actions'].append(action);report['applied']=True
                module['accepted']=True
            module['after']=best_metrics
            if report['stop_reason']=='time_budget':break
        else:
            report['stop_reason']='improved' if report['applied'] else 'no_improvement'
    except RoutingFailure as error:
        if error.diagnostics.get('reason') != 'quality_time_budget':raise
        report['stop_reason']='time_budget'
    report['elapsed_seconds']=round(perf_counter()-started,6)
    return current,report
