"""Failure-only routing forests and same-net labels, preserving the layout.

No historical placements, SA, global bus lanes or device reordering. Difficult
nets may yield to the rest of the current queue. Last-resort labels cover every
remaining physical group, including networks with more than four groups.
"""
from collections import Counter
from copy import deepcopy
from math import ceil, isfinite
from statistics import median
from time import perf_counter

from netlist2schematic.common.sa_common import load_local
from netlist2schematic.plugins.net_labels.net_label_core import _anchor_choices
from netlist2schematic.plugins.net_labels.net_label_validation import (
    canonical_routes, display_routes, label_position, lead_error, local_groups, text_error)
from netlist2schematic.rendering.sa_render import _text_box
from netlist2schematic.routing.orthogonal_router import RoutingFailure, _priority, _route_net
from netlist2schematic.routing.routing_rules import (RoutingOptions, analyze_routes,
    build_scene, canonical_topology, check_deadline, static_shape_conflicts)
from netlist2schematic.recovery.labeled_geometry import (
    REPRESENTATION, VERSION, drawing_hints, group_records, terminals, validate_view)
from netlist2schematic.recovery.spacing_retry import expanded_context


def _pose(context):
    placement, comps, _, _, _ = context
    return tuple((comp['id'], tuple(placement.cell_of[i]), placement.orient[i], tuple(placement.pos_of(i)))
                 for i, comp in enumerate(comps))


def _safe(scene, routes, deadline):
    return not analyze_routes(list(routes.items()), scene, deadline=deadline)['violations']


def _seed(scene, placement, checkpoint, deadline, *, completed=True):
    routes = {net: list(scene.fixed_segments.get(net, ())) for net in scene.pins}
    from netlist2schematic.plugins.passive_skeleton.module_connections import guide_routes
    seeds = guide_routes(placement, scene, internal_only=True) if getattr(placement, 'passive_module_guides', None) else {}
    native = checkpoint.get('completed', {}) if checkpoint and completed else {}
    if checkpoint and completed and checkpoint.get('scene') != scene:
        raise ValueError('Label recovery checkpoint does not match its own current layout')
    for net, lines in sorted({**seeds, **native}.items()):
        if net not in routes:
            continue
        trial = {**routes, net: canonical_topology(routes[net]+list(lines))['segments']}
        if _safe(scene, trial, deadline):
            routes = trial
    branch = checkpoint.get('branch') or {} if checkpoint and completed else {}
    net = checkpoint.get('failed_net') if checkpoint else None
    if net in routes and branch.get('tree'):
        trial = {**routes, net: canonical_topology(routes[net]+branch['tree'])['segments']}
        if _safe(scene, trial, deadline):
            routes = trial
    return routes


def _continue(scene, routes, checkpoint, comps, deadline, report):
    identities = terminals(scene, comps)
    order = sorted(scene.pins, key=lambda name: _priority(scene, name))
    remaining = []
    for net in order:
        _, groups = local_groups(scene, net, routes[net], identities, deadline)
        if len(groups) > 1:
            remaining.append(net)
    free = {}
    for number, net in enumerate(remaining):
        check_deadline(deadline, 'label_recovery_queue')
        slot = min(1.0, (deadline-perf_counter()) / max(1, len(remaining)-number))
        if slot <= 0:
            break
        started = perf_counter()
        statistics = dict(graph_nodes=0, attempts=0, expanded_states=0)
        partial = {}
        occupied = {name: list(lines) for name, lines in scene.fixed_segments.items()}
        for name, lines in routes.items():
            occupied.setdefault(name, []).extend(lines)
        try:
            lines, blockers = _route_net(scene, net, occupied,
                checkpoint.get('margin', scene.options.margins[0]) if checkpoint else scene.options.margins[0],
                [], statistics, free, deadline=min(deadline, started+slot), _rescue_state=partial)
            reason = None if lines is not None else 'no_path'
        except RoutingFailure as exc:
            reason = exc.diagnostics.get('reason', 'search_failed')
            lines = None
        if lines is None:
            lines = (partial.get('branch') or {}).get('tree')
        status = 'deferred'
        if lines:
            trial = {**routes, net: canonical_topology(list(scene.fixed_segments.get(net, ()))+list(lines))['segments']}
            if _safe(scene, trial, deadline):
                routes[net] = trial[net]
                _, groups = local_groups(scene, net, routes[net], identities, deadline)
                status = 'connected' if len(groups) == 1 else 'partial'
        report['net_attempts'].append(dict(net=net, status=status, reason=reason,
                                         seconds=round(perf_counter()-started, 6)))
    return routes


def _nearby(scene, routes, comps, scale, deadline, report):
    """Cheap straight/L joins; an obstructed local branch never stops the queue."""
    identities = terminals(scene, comps)
    trials = 0
    for net in sorted(routes):
        for _ in range(len(scene.pins[net])):
            check_deadline(deadline, 'label_recovery_local_links')
            topology, groups = local_groups(scene, net, routes[net], identities, deadline)
            old_cycles=len(topology['segments'])-sum(len(g['points']) for g in groups)+len(groups)
            if len(groups) < 2 or trials >= 256:
                break
            choices = []
            for number, first in enumerate(groups):
                for second in groups[number+1:]:
                    aa = _anchor_choices(first, set(), scene, net)
                    bb = _anchor_choices(second, set(), scene, net)
                    choices.extend((abs(a[0]-b[0])+abs(a[1]-b[1]),a,b) for a in aa for b in bb
                                   if abs(a[0]-b[0])+abs(a[1]-b[1]) <= 2*scale)
            improved = False
            for _, a, b in sorted(choices)[:12]:
                points = ([a,b],) if a[0] == b[0] or a[1] == b[1] else (
                    [a,(a[0],b[1]),b], [a,(b[0],a[1]),b])
                for chain in points:
                    check_deadline(deadline, 'label_recovery_local_links')
                    trials += 1
                    lines = list(zip(chain,chain[1:]))
                    if not all(scene.edge_check(net,c,d,routes)[0] for c,d in lines):
                        continue
                    trial = {**routes, net: canonical_topology(routes[net]+lines)['segments']}
                    if _safe(scene,trial,deadline):
                        checked, after = local_groups(scene,net,trial[net],identities,deadline)
                        cycles=len(checked['segments'])-sum(len(g['points']) for g in after)+len(after)
                        if len(after) < len(groups) and cycles <= old_cycles:
                            routes[net]=trial[net];improved=True;break
                if improved:
                    break
            if not improved:
                break
    report['local_join_trials'] = trials
    return routes


def _labels(par, placement, comps, nets, index, scene, routes, deadline, report):
    by_net, groups = group_records(scene,routes,comps,deadline)
    hints = drawing_hints(par,placement,comps,nets,index,routes,scene,by_net)
    view = dict(version=VERSION, base_routes=routes, groups=groups, labels=[], render_hints=hints)
    todo = [(net,group) for net,items in by_net.items() if len(items)>1 for group in items]
    # More constrained single-terminal groups get their label opportunity first.
    todo.sort(key=lambda item:(len(item[1]['terminals']), min(item[1]['points']),item[0]))
    rejects = Counter()
    lengths = sorted({ceil(length/scene.options.grid_step)*scene.options.grid_step
                      for length in (24,36,48,64,96,128)})
    for net, group in todo:
        preferred = {pin.escape for pin in scene.pins[net] if pin.escape in group['points']}
        anchors = _anchor_choices(group,preferred,scene,net)
        accepted = False
        for length in lengths:
            for anchor in anchors:
                for direction in ((-1,0),(1,0),(0,-1),(0,1)):
                    check_deadline(deadline,'label_recovery_positions')
                    tip = anchor[0]+direction[0]*length, anchor[1]+direction[1]*length
                    error = lead_error(scene,net,anchor,tip,routes,view['labels'])
                    if error:
                        rejects[error]+=1;continue
                    x,y,style=label_position(tip,direction)
                    label=dict(net=net,group_id=group['id'],anchor=list(anchor),tip=list(tip),
                        text=net,x=x,y=y,anchor_style=style,
                        bounds=list(_text_box(x,y,net,style,size=12)))
                    error=text_error(scene,label['bounds'],routes,view['labels'],hints['text_obstacles'],own_label=label)
                    if error:
                        rejects[error]+=1;continue
                    trial={**view,'labels':view['labels']+[label]}
                    if analyze_routes(display_routes(trial),scene,deadline=deadline)['violations']:
                        rejects['lead_geometry']+=1;continue
                    view=trial;accepted=True;break
                if accepted:break
            if accepted:break
        if not accepted:
            raise RoutingFailure('No safe recovery label position was found',dict(
                reason='no_safe_label',net=net,group=group['id'],rejections=dict(rejects)))
    report['label_rejections']=dict(rejects)
    return view


def _candidate_rows(candidates, context, options):
    rows=[];seen=set()
    for item in candidates or ():
        if item.get('context') is None:continue
        pose=_pose(item['context'])
        if pose in seen:continue
        seen.add(pose);rows.append(item)
    if context is not None and _pose(context) not in seen:
        rows.append(dict(context=context,checkpoint=None,options=options,
                         candidate_id=str(context[4].get('candidate_id','unknown')),attempt_index=-1))
    def priority(row):
        checkpoint=row.get('checkpoint') or {}
        scene=checkpoint.get('scene')
        done=sum(len(scene.pins.get(net,())) for net in checkpoint.get('completed',())) if scene else 0
        total=sum(map(len,scene.pins.values())) if scene else 1
        return (str(row.get('candidate_id')) not in ('2','3'),
                -int(row['context'][4].get('passive_skeleton',{}).get('applied',False)),
                -done/max(1,total),-row.get('attempt_index',0))
    return sorted(rows,key=priority)


def recover(context, *, candidates=(), routing_options=None, seconds=60, source=None, cell=None):
    """Deliver the first verified current-layout forest, never a bus redraw."""
    if type(seconds) not in (int,float) or not isfinite(seconds) or seconds<=0:
        raise ValueError('label recovery seconds must be finite and positive')
    started=perf_counter();deadline=started+seconds
    rows=_candidate_rows(candidates,context,routing_options)
    if not rows:
        raise RoutingFailure('No current-run layout is available for labels',{'reason':'no_attempted_layout'})
    par=load_local('place_and_route','place-and-route.py')
    report=dict(version=VERSION,budget_seconds=seconds,history_used=False,bus_used=False,attempts=[])
    variants=[(row,1.) for row in rows]
    # A larger grid retains the exact logical frame. Every multiplier starts
    # from the original current pose, not a cumulatively distorted predecessor.
    variants.extend((rows[0],scale) for scale in (2.,4.,8.))
    last_failure=None
    for number,(row,scale) in enumerate(variants):
        if perf_counter() >= deadline:
            break
        slot=(deadline-perf_counter())/max(1,len(variants)-number)
        slot_end=min(deadline,perf_counter()+slot)
        options=row.get('options') or routing_options or RoutingOptions()
        original=row['context']
        chosen=(deepcopy(original) if scale==1 else expanded_context(original,
            routing_options=options,scale=scale,seconds=max(slot,.001)))
        placement,comps,nets,index,metadata=chosen
        if source is not None:
            from netlist2schematic.rendering.presentation import configure_presentation
            metadata['presentation']=configure_presentation(
                placement,source,cell,comps,nets,'clean')
        attempt=dict(candidate_id=str(metadata.get('candidate_id','unknown')),scale=scale,
                     net_attempts=[],status='running')
        report['attempts'].append(attempt)
        try:
            scene=build_scene(par,nets,placement,index,options=options,deadline=slot_end)
            fixed={net:list(scene.fixed_segments.get(net,())) for net in scene.pins}
            faults=analyze_routes(list(fixed.items()),scene,deadline=slot_end)['violations']
            faults += [fault for _,fault in static_shape_conflicts(scene,slot_end)]
            if faults:
                attempt.update(status='invalid_fixed_geometry',violations=faults[:12]);continue
            checkpoint=row.get('checkpoint') if scale==1 else None
            routes=_seed(scene,placement,checkpoint,slot_end)
            search_end=min(slot_end,perf_counter()+slot*.35)
            gaps=[b-a for axis in ('column_x','row_y') for a,b in
                  zip(getattr(placement,axis,()),getattr(placement,axis,())[1:])]
            pitch=median(gaps) if gaps else par.CELL
            try:
                routes=_continue(scene,routes,checkpoint,comps,search_end,attempt)
                routes=_nearby(scene,routes,comps,pitch,search_end,attempt)
            except RoutingFailure:
                # The last committed forest remains geometry checked. Search
                # time does not consume the label-placement reserve.
                attempt['queue_stop']='search_budget'
            label_start=perf_counter()
            # If completed wires leave no label access, withdraw those wires
            # transactionally. Module-local links and fixed exits stay intact.
            modes=('continued_forest','release_failed_net','local_links','local_exits')
            failed_net=None
            for mode_number,mode in enumerate(modes):
                if mode=='release_failed_net':
                    if failed_net not in routes:continue
                    clean=_seed(scene,placement,None,slot_end,completed=False)
                    routes={**routes,failed_net:clean[failed_net]}
                elif mode in ('local_links','local_exits'):
                    routes=_seed(scene,placement,None,slot_end,completed=False)
                mode_end=min(slot_end,perf_counter()+(slot_end-perf_counter())/(len(modes)-mode_number))
                if mode=='local_links':
                    try:
                        routes=_nearby(scene,routes,comps,pitch,
                                       min(mode_end,perf_counter()+(mode_end-perf_counter())*.3),attempt)
                    except RoutingFailure:
                        pass
                try:
                    view=_labels(par,placement,comps,nets,index,scene,routes,mode_end,attempt)
                    routed,metrics,validation=validate_view(par,nets,placement,index,view,
                                                            options=options,deadline=mode_end)
                    break
                except RoutingFailure as exc:
                    last_failure=exc.diagnostics
                    failed_net=exc.diagnostics.get('net')
                    if perf_counter()>=slot_end:raise
            else:
                raise RoutingFailure('No safe labeled forest was found',last_failure)
            attempt.update(status='valid',mode=mode,label_count=len(view['labels']),
                           label_seconds=round(perf_counter()-label_start,6))
            from netlist2schematic.plugins.net_labels.net_labels import _sources,_resource_hashes
            source=source or metadata.get('source_netlist')
            if source is None:raise ValueError('Labeled recovery requires its current source netlist')
            view['display_metrics']=deepcopy(metrics)
            report.update(status='valid',source_candidate=str(metadata.get('candidate_id','unknown')),
                elapsed_seconds=round(perf_counter()-started,6),scale=scale,
                logical_layout_preserved=True,orientations_preserved=True,
                physical_layout_preserved=scale==1,view=view,
                source_placement={comp['id']:dict(row=original[0].cell_of[i][0],
                    col=original[0].cell_of[i][1],orient=original[0].orient[i],
                    xy=list(original[0].pos_of(i))) for i,comp in enumerate(comps)},
                final_placement={comp['id']:dict(row=placement.cell_of[i][0],col=placement.cell_of[i][1],
                    orient=placement.orient[i],xy=list(placement.pos_of(i))) for i,comp in enumerate(comps)},
                dependencies=dict(sources=_sources(source),resources=_resource_hashes(par,comps)))
            for field in ('routes','metrics','validation','quality','_candidate_cleanup'):
                metadata.pop(field,None)
            metadata.update(routing_stage='labeled_recovery',route_representation=REPRESENTATION,
                labeled_recovery=report,quality=dict(policy=VERSION,score=None,
                    selected_candidate=str(metadata.get('candidate_id','unknown')),
                    physical_metrics=deepcopy(metrics),connectivity_mode=REPRESENTATION))
            validation.update(routing_seconds=report['elapsed_seconds'])
            placement.routing_stage='labeled_recovery'
            return placement,comps,nets,index,metadata,(routed,metrics,validation)
        except RoutingFailure as exc:
            attempt.update(status='failed',diagnostics=deepcopy(exc.diagnostics))
            last_failure=exc.diagnostics
    report.update(status='failed',elapsed_seconds=round(perf_counter()-started,6))
    raise RoutingFailure('No geometry-safe current-layout label plan completed',
        dict(reason='labeled_recovery_failed',labeled_recovery=report,last_failure=last_failure))
