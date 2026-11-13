"""Bounded, failure-safe net-label presentation of fully validated routes.

This is a drawing postprocessor.  It does not move devices, run a router, or
weaken the original physical routing contract.  Every committed view is checked
again by the independent net-label validator.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from math import ceil, isfinite
from time import perf_counter

from netlist2schematic.routing.routing_rules import EPS, canonical_topology, check_deadline, intersection
from netlist2schematic.rendering.sa_render import _text_box
from netlist2schematic.plugins.net_labels.net_label_validation import (
    VERSION, FONT_SIZE, LEAD_LENGTHS, MAX_GROUPS, anchors, canonical_routes,
    covered, display_routes, edge, label_position, lead_error, local_groups,
    protected_pairs, remove_segments, text_error, validate_view)

MAX_TRIALS = 64
MAX_ANCHORS = 8


def _chains(scene, routes, scale, deadline):
    result = []
    for net, lines in routes.items():
        if net in scene.power_nets or net not in scene.pins:
            continue
        topology = canonical_topology(lines,pins=anchors(scene,net),deadline=deadline)
        adjacency = topology['adjacency']
        fixed = canonical_topology(scene.fixed_segments.get(net,()),deadline=deadline)['runs']
        visited = {edge(a,b) for a,b in topology['segments'] if covered((a,b),fixed)}
        stops = anchors(scene,net) | {p for p,ns in adjacency.items() if len(ns)!=2}
        stops.update(p for e in visited for p in e)
        for start in sorted(stops & set(adjacency)):
            for following in sorted(adjacency[start]):
                e = edge(start,following)
                if e in visited:
                    continue
                visited.add(e)
                path = [start,following]
                previous,current = start,following
                while current not in stops:
                    check_deadline(deadline,'net_label_chain_extraction')
                    nxt = next(iter(adjacency[current]-{previous}))
                    visited.add(edge(current,nxt))
                    path.append(nxt)
                    previous,current = current,nxt
                if path[0]==path[-1]:
                    continue
                if path[-1] < path[0]:
                    path.reverse()
                length = sum(abs(a[0]-b[0])+abs(a[1]-b[1]) for a,b in zip(path,path[1:]))
                span = abs(path[0][0]-path[-1][0])+abs(path[0][1]-path[-1][1])
                excess = length-span
                # Long straight wires are deliberately eligible as well.
                crossings = set()
                for other,foreign in routes.items():
                    if other==net:
                        continue
                    for a,b in zip(path,path[1:]):
                        for c,d in foreign:
                            hit = intersection(a,b,c,d)
                            if hit is not None and hit not in (a,b,c,d):
                                crossings.add((other,hit))
                count = len(crossings)
                reasons = []
                if length >= 6*scale-EPS:
                    reasons.append('very_long')
                if length >= 4*scale-EPS and excess >= 1.5*scale-EPS:
                    reasons.append('long_detour')
                if length >= 3*scale-EPS and count >= 3:
                    reasons.append('many_crossings')
                if reasons:
                    severity = max(length/(6*scale),excess/(1.5*scale),count/3)
                    result.append(dict(net=net,path=path,length=length,excess=excess,
                                       crossings=count,reason=reasons,severity=severity))
    return sorted(result,key=lambda row:(-row['severity'],-row['excess'],-row['length'],
                                         tuple(row['path']),row['net']))


def _group_records(scene,routes,terminal_ids,deadline):
    records,groups_by_net = [],{}
    for net,lines in routes.items():
        if net in scene.power_nets:
            continue
        _,groups = local_groups(scene,net,lines,terminal_ids,deadline)
        if not groups or any(not g['terminals'] for g in groups):
            raise ValueError('terminal_free_island')
        if len(groups)>MAX_GROUPS:
            raise ValueError('group_limit')
        groups_by_net[net] = groups
        if len(groups)>1:
            records.extend(dict(net=net,id=g['id'],terminals=[list(v) for v in g['terminals']])
                           for g in groups)
    return records,groups_by_net


def _anchor_choices(group,preferred,scene,net):
    choices = set(group['points'])
    step = scene.options.grid_step
    for a,b in group['segments']:
        midpoint = ((a[0]+b[0])/2,(a[1]+b[1])/2)
        if a[0]==b[0]:
            midpoint = (a[0],round(midpoint[1]/step)*step)
        else:
            midpoint = (round(midpoint[0]/step)*step,a[1])
        if (min(a[0],b[0])<=midpoint[0]<=max(a[0],b[0]) and
                min(a[1],b[1])<=midpoint[1]<=max(a[1],b[1])):
            choices.add(midpoint)
    # Original endpoints of the removed bridge are the most direct labels.
    return sorted(choices,key=lambda p:(0 if p in preferred else 1,
                  min((abs(p[0]-q[0])+abs(p[1]-q[1]) for q in preferred),default=0),p))[:MAX_ANCHORS]


def _place_labels(scene,net,groups,routes,previous,text_obstacles,preferred,deadline,rejections):
    result = []
    distances = sorted({ceil(v/scene.options.grid_step)*scene.options.grid_step for v in LEAD_LENGTHS})
    for group in groups:
        candidates = []
        for anchor in _anchor_choices(group,preferred,scene,net):
            for length in distances:
                for direction in ((-1,0),(1,0),(0,-1),(0,1)):
                    check_deadline(deadline,'net_label_placement')
                    tip = (anchor[0]+direction[0]*length,anchor[1]+direction[1]*length)
                    error = lead_error(scene,net,anchor,tip,routes,previous+result)
                    if error:
                        rejections[error] += 1
                        continue
                    x,y,align = label_position(tip,direction)
                    label = dict(net=net,group_id=group['id'],anchor=list(anchor),tip=list(tip),
                                 text=net,x=x,y=y,anchor_style=align,
                                 bounds=list(_text_box(x,y,net,align,size=FONT_SIZE)))
                    error = text_error(scene,label['bounds'],routes,previous+result,text_obstacles,own_label=label)
                    if error:
                        rejections[error] += 1
                        continue
                    candidates.append((length,anchor,tip,label))
        if not candidates:
            raise ValueError('no_safe_label_position')
        result.append(min(candidates,key=lambda row:row[:3])[3])
    return result


def _time_budget(error):
    return getattr(error,'diagnostics',{}).get('reason')=='quality_time_budget'


def build_view(scene,routed,*,scale,terminal_ids,text_obstacles=(),seconds=5):
    if type(scale) not in (int,float) or not isfinite(scale) or scale<=0:
        raise ValueError('scale must be positive and finite')
    if type(seconds) not in (int,float) or not isfinite(seconds) or seconds<0:
        raise ValueError('seconds must be finite and non-negative')
    started = perf_counter()
    deadline = started+seconds
    original = canonical_routes(routed)
    view = dict(version=VERSION,policy='auto',scale=scale,
        parameters=dict(max_groups_per_net=MAX_GROUPS,max_trials=MAX_TRIALS,
                        max_anchors_per_group=MAX_ANCHORS,seconds=seconds,
                        long_pitches=6,detour_min_pitches=4,detour_excess_pitches=1.5,
                        crossing_min_pitches=3,crossings=3,local_pitches=2,
                        lead_lengths=list(LEAD_LENGTHS),font_size=FONT_SIZE),
        base_routes=deepcopy(original),labels=[],groups=[],omitted_paths=[],status='not_needed')
    # Preserve a known-good result even when the search receives zero budget.
    verified = validate_view(scene,original,view,terminal_ids,text_obstacles=text_obstacles)
    view.update(display_metrics=verified['metrics'],validation=verified)
    attempts=accepted=0
    rejections=Counter()
    rejected_geometry=set()
    examples=[]
    status='not_needed'
    try:
        pairs = protected_pairs(scene,original,terminal_ids,scale,deadline)
        while attempts<MAX_TRIALS:
            check_deadline(deadline,'net_label_search')
            routes=canonical_routes(view['base_routes'],deadline)
            candidates=_chains(scene,routes,scale,deadline)
            if not candidates:
                status='applied' if accepted else 'not_needed'
                break
            committed=False
            available=False
            for candidate in candidates:
                check_deadline(deadline,'net_label_search')
                net,path = candidate['net'],candidate['path']
                identity=(net,tuple(path))
                if identity in rejected_geometry:
                    continue
                available=True
                if attempts>=MAX_TRIALS:
                    break
                attempts+=1
                trial=deepcopy(view)
                removed=list(zip(path,path[1:]))
                trial['base_routes'][net]=remove_segments(scene,net,routes[net],removed,deadline)
                try:
                    records,groups_by_net=_group_records(scene,trial['base_routes'],terminal_ids,deadline)
                    _,old_groups=local_groups(scene,net,routes[net],terminal_ids,deadline)
                    if len(groups_by_net[net])!=len(old_groups)+1:
                        raise ValueError('not_a_bridge')
                    ownership={identity:g['id'] for g in groups_by_net[net] for identity in g['terminals']}
                    if any(ownership[a]!=ownership[b] for a,b in pairs[net]):
                        raise ValueError('nearby_terminals')
                    previous=[label for label in view['labels'] if label['net']!=net]
                    labels=_place_labels(scene,net,groups_by_net[net],trial['base_routes'],previous,
                                         text_obstacles,{tuple(path[0]),tuple(path[-1])},deadline,rejections)
                    trial['labels']=previous+labels
                    trial['groups']=records
                    trial['omitted_paths'].append(dict(net=net,path=[list(p) for p in path],
                        reason=candidate['reason'],length=candidate['length'],
                        excess=candidate['excess'],crossings=candidate['crossings']))
                    checked=validate_view(scene,original,trial,terminal_ids,
                                           text_obstacles=text_obstacles,deadline=deadline)
                    if (checked['metrics']['wirelength']>=view['display_metrics']['wirelength']-EPS or
                            checked['metrics']['crossings']>view['display_metrics']['crossings']):
                        raise ValueError('no_incremental_benefit')
                    check_deadline(deadline,'net_label_commit')
                except ValueError as exc:
                    if _time_budget(exc):
                        raise
                    reason=str(exc)
                    rejections[reason]+=1
                    if len(examples)<12:
                        examples.append(dict(net=net,path=[list(p) for p in path],reason=reason))
                    rejected_geometry.add(identity)
                    continue
                trial.update(display_metrics=checked['metrics'],validation=checked)
                view=trial
                accepted+=1
                rejected_geometry.clear()
                committed=True
                status='applied'
                break
            if not committed:
                status='applied' if accepted else 'no_safe_split'
                break
        else:
            status='trial_budget'
    except ValueError as exc:
        if not _time_budget(exc):
            raise
        status='time_budget'
    view['status']=status
    view['diagnostics']=dict(trials=attempts,accepted=accepted,rejections=dict(rejections),
        examples=examples,elapsed_seconds=round(perf_counter()-started,6),
        stop_reason=status,partial=bool(accepted and status in ('time_budget','trial_budget')))
    view['display_metrics'].update(label_count=len(view['labels']),
        omitted_wirelength=verified['metrics']['wirelength']-view['display_metrics']['wirelength'])
    return view
