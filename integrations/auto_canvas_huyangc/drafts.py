"""Safe, opt-in editable drawings, using the engine's existing failure objects."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path


def write_atomic(path, value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')
    temporary.replace(path)


def selection_key(payload):
    """A complete drawing always wins; incomplete drawings favor useful joins."""
    candidate=str(payload['selected_candidate'])
    if payload.get('result_kind','complete')=='complete':
        metrics=payload['metrics']
        return (0,metrics['bends'],metrics['crossings'],metrics['wirelength'],candidate)
    draft=payload['draft']
    return (1,*draft['structure_rank'],-draft['connected_terminal_fraction'],
            -draft['completed_nets'],draft['remaining_connections'],
            payload['metrics']['bends'],payload['metrics']['wirelength'],candidate)


def choose_payloads(folder, source_hash):
    candidates=[]
    for path in sorted(Path(folder).glob('candidate-*-wire-plan.json')):
        value=json.loads(path.read_text(encoding='utf8'))
        if value.get('source_hash')!=source_hash or str(value.get('selected_candidate')) not in ('2','3'):
            raise ValueError('Stale or foreign editable candidate artifact')
        if not value.get('validation',{}).get('geometry_valid'):
            raise ValueError('Unverified editable candidate artifact')
        candidates.append((value,path))
    return min(candidates,key=lambda item:selection_key(item[0])) if candidates else None


def connection_progress(scene, routed, *, deadline=None):
    from netlist2schematic.routing.routing_rules import canonical_topology
    routes=dict(routed)
    completed=connected=total=remaining=0
    missing=[]
    for net,pins in sorted(scene.pins.items()):
        topology=canonical_topology(routes.get(net,[]),[p.point for p in pins],deadline=deadline)
        adjacency=topology['adjacency']
        components={}
        for pin in pins:
            if pin.point in components:continue
            todo=[pin.point];component=len(set(components.values()))
            while todo:
                p=todo.pop()
                if p in components:continue
                components[p]=component;todo.extend(adjacency.get(p,()))
        groups={}
        for pin in pins:groups.setdefault(components[pin.point],[]).append(pin)
        total+=len(pins)
        if len(groups)<=1:completed+=1
        else:
            remaining+=len(groups)-1
            missing.append(dict(net=net,components=len(groups),terminals=len(pins)))
        connected+=sum(len(group) for group in groups.values() if len(group)>1 or len(pins)==1)
    return dict(completed_nets=completed,total_nets=len(scene.pins),connected_terminals=connected,
                total_terminals=total,connected_terminal_fraction=connected/max(1,total),
                remaining_connections=remaining,missing_nets=missing)


def make_draft(par, context, checkpoint, profile, options, identifier, *, deadline=None):
    """Only full, statically safe poses and independently checked wires survive.

    Unfinished per-net A* branches are deliberately excluded: the engine's
    checkpoint only promises completed nets. Real fixed pin stubs are retained
    for the user to wire from. No former drawing is used as a routing seed.
    """
    from netlist2schematic.routing.routing_rules import (build_scene,analyze_routes,
        static_shape_conflicts,canonical_topology,check_deadline)
    from netlist2schematic.routing.sa_routing import measure_routes
    from netlist2schematic.plugins.organization.branch_layout import valid,measure,rank
    from .engine_export import export_context
    placement,comps,nets,index,metadata=context
    if {c['id'] for c in comps}!=set(profile['components']):
        raise ValueError('Draft requires every electrical device in one complete pose')
    if not valid(placement):raise ValueError('Draft would break its active branch framework')
    check_deadline(deadline,'draft_scene')
    scene=build_scene(par,nets,placement,index,options=options)
    if static_shape_conflicts(scene,deadline):raise ValueError('Draft layout has static shape conflicts')
    fixed={net:list(scene.fixed_segments.get(net,[])) for net in scene.pins}
    if analyze_routes(list(fixed.items()),scene,deadline)['violations']:
        raise ValueError('Draft fixed pin exits are not geometrically safe')
    # A checkpoint must belong to this exact pose/options. Refuse cross-pose
    # occupancy; retaining unrelated routes is worse than leaving a flightline.
    if checkpoint and checkpoint.get('scene')!=scene:
        original=checkpoint['scene']
        if (original.pins!=scene.pins or original.body_rects!=scene.body_rects or
                original.fixed_segments!=scene.fixed_segments or original.options!=scene.options):
            raise ValueError('Draft checkpoint and pose do not match')
    completed=deepcopy(checkpoint.get('completed',{})) if checkpoint else {}
    routes=dict(fixed);discarded=[]
    for net,segments in sorted(completed.items()):
        if net not in scene.pins:raise ValueError('Draft checkpoint contains an unknown signal net')
        candidate={**routes,net:[*fixed[net],*segments]}
        if analyze_routes(list(candidate.items()),scene,deadline)['violations']:
            discarded.append(net)
        else:routes=candidate
    routed=[(net,canonical_topology(segments,[p.point for p in scene.pins[net]],deadline)['segments'])
            for net,segments in sorted(routes.items())]
    if analyze_routes(routed,scene,deadline)['violations']:
        raise ValueError('Draft routes failed independent geometry validation')
    metrics=measure_routes(par,routed,placement,scene=scene,deadline=deadline)
    checked=dict(valid=False,complete=False,geometry_valid=True,status='editable_draft',
                 violations=[],router='existing-engine-accepted-checkpoint')
    result=export_context(par,placement,comps,nets,index,routed,metrics,checked,profile,options,metadata,scene=scene)
    plan=getattr(placement,'branch_plan',None)
    structure_rank=list(rank(measure(placement,index,plan,pins=False))) if plan else [0,0,0,0]
    result.update(result_kind='editable_draft',selected_candidate=str(identifier),routing_stage='editable_draft',
        draft=dict(version='editable-routing-draft-v1',**connection_progress(scene,routed,deadline=deadline),
                   structure_rank=structure_rank,discarded_nets=discarded,
                   kept='completed_networks_and_original_fixed_pin_stubs',
                   complete=False))
    return result
