"""Ordinary, independently checked wires inside an inserted passive module.

These are never fixed-pin exemptions. Only complete, connected real nets may
seed final routing; partial external-net guides remain placement diagnostics.
"""
from collections import defaultdict
from statistics import median

from netlist2schematic.routing.routing_rules import analyze_routes, build_scene
from netlist2schematic.routing.sa_routing import normalize_routes, routing_errors


def guide_routes(placement, scene, *, internal_only=False):
    routes=defaultdict(list)
    for guide in getattr(placement,'passive_module_guides',{}).values():
        for net,segments in (guide.get('internal_routes',guide['routes']) if internal_only else guide['routes']).items():
            if net not in scene.power_nets:
                routes[net].extend((tuple(a),tuple(b)) for a,b in segments if a!=b)
    return dict(routes)


def check_guides(par, placement, nets, index, options, deadline=None):
    scene=build_scene(par,nets,placement,index,options=options,deadline=deadline)
    routes={net:list(segments) for net,segments in scene.fixed_segments.items() if net not in scene.power_nets}
    for net,segments in guide_routes(placement,scene,internal_only=True).items():
        routes.setdefault(net,[]).extend(segments)
    return analyze_routes(list(routes.items()),scene,deadline=deadline)['violations']


def complete_routes(par, placement, nets, index, scene, deadline=None):
    proposed=guide_routes(placement,scene)
    internal=guide_routes(placement,scene,internal_only=True)
    from netlist2schematic.routing.sa_routing import RoutingValidationError
    hard={net:list(s) for net,s in scene.fixed_segments.items()}
    for net,segments in internal.items():
        if net not in scene.pins:raise ValueError('Module internal connection names an unknown signal network')
        hard.setdefault(net,[]).extend(segments)
    faults=analyze_routes(list(hard.items()),scene,deadline=deadline)['violations']
    if faults:
        raise RoutingValidationError('Passive module internal connections failed geometric validation',
                                     dict(reason='module_internal_conflict',violations=faults))
    result={}
    for net,segments in proposed.items():
        if net not in scene.pins:
            raise ValueError('Passive module guide names a network absent from the original circuit')
        for candidate in (segments,internal.get(net,[])):
            combined=normalize_routes([(net,candidate+scene.fixed_segments[net])],deadline=deadline)
            if routing_errors(combined,{net:nets[net]},placement,index,deadline=deadline):continue
            occupancy={name:list(s) for name,s in scene.fixed_segments.items()}
            for name,s in internal.items():occupancy.setdefault(name,[]).extend(s)
            occupancy.update(result);occupancy[net]=combined[0][1]
            faults=analyze_routes(list(occupancy.items()),scene,deadline=deadline)['violations']
            if not any(net in f.get('nets',()) for f in faults):
                result[net]=combined[0][1];break
    # Seeding cannot hide a clash with unfinished nets' immutable exits.
    occupancy={net:list(segments) for net,segments in scene.fixed_segments.items()}
    occupancy.update(result)
    violations=analyze_routes(list(occupancy.items()),scene,deadline=deadline)['violations']
    if violations:
        raise RoutingValidationError('Passive module connections failed geometric validation',
                                     dict(reason='module_connection_conflict',violations=violations))
    return result


def rebase_after_axis_expansion(before, after, index, plan, *, grid_step=2):
    """Keep each module rigid while the existing spacing stage expands axes.

    Logical guest cells are bookkeeping, so scaling their individual anchors
    would tear a module apart. Translate it by its real hosts' median motion;
    final routing starts fresh and independently checks the new coordinates.
    """
    records=[]
    for group in plan['groups']:
        terminals=[t for ts in group['anchors'].values() for t in ts]
        if group.get('owner_port'):
            terminals=[group['owner_port']['terminal']]
        deltas=[tuple(after.pin_world(index[n],p)[k]-before.pin_world(index[n],p)[k]
                      for k in (0,1)) for n,p in terminals]
        shift=tuple(median(d[k] for d in deltas) if deltas else 0. for k in (0,1))
        shift=tuple(round(v/grid_step)*grid_step for v in shift)
        for name in group['members']:
            i=index[name];old=before.pos_of(i);current=after.pos_of(i)
            for k in (0,1):
                after.passive_local_offsets[i][k]+=old[k]+shift[k]-current[k]
        records.append(dict(group=group['id'],members=list(group['members']),translation=list(shift)))
    if hasattr(after,'passive_module_guides'):
        del after.passive_module_guides
    return records
