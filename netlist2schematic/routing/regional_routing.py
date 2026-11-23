"""Bounded branch searches for an organized frame, independent of device count.

All candidates use Scene's original geometry oracle. Windows restrict search
coordinates only; foreign obstacles and fixed ports always remain global.
"""
from collections import Counter
from time import perf_counter

VERSION='regional-routing-v3'


def priority(scene,net,base):
    owners=getattr(scene,'branch_routing',{}).get('ownership',{})
    names=getattr(scene,'component_names',{})
    regions={owners.get(names.get(p.component),('unassigned',p.component)) for p in scene.pins[net]}
    phase=(0 if len(regions)<=1 else 1 if sum(p.pin in ('D','S','C','E') for p in scene.pins[net])>=2 else 2)
    return (phase,)+base


def window_axes(xs,ys,start,tree,radius,step):
    points=[(min(max(start[0],min(a[0],b[0])),max(a[0],b[0])),
             min(max(start[1],min(a[1],b[1])),max(a[1],b[1]))) for a,b in tree]
    goal=min(points,key=lambda p:(abs(p[0]-start[0])+abs(p[1]-start[1]),p))
    lows=[max(min(axis),min(start[k],goal[k])-radius) for k,axis in enumerate((xs,ys))]
    highs=[min(max(axis),max(start[k],goal[k])+radius) for k,axis in enumerate((xs,ys))]
    return ([x for x in xs if lows[0]<=x<=highs[0]], [y for y in ys if lows[1]<=y<=highs[1]])


def bounded_history(a,b,history):
    axis=1 if a[0]==b[0] else 0;fixed=a[1-axis];lo,hi=sorted((a[axis],b[axis]));intervals=[]
    for rect in history:
        if rect[1-axis]<=fixed<=rect[3-axis]:
            left,right=max(lo,rect[axis]),min(hi,rect[axis+2])
            if left<right:intervals.append((left,right))
    total=0.;end=-float('inf')
    for a,b in sorted(intervals):total+=max(0.,b-max(a,end));end=max(end,b)
    return 2*total


def find_path(scene,net,start,tree,occupied,margin,history,statistics,checks,prepared,deadline,*,extra_points=(),initial_direction=None):
    from netlist2schematic.routing.orthogonal_router import _find_path,RoutingFailure
    report=statistics.setdefault('regional_search',dict(version=VERSION,windows=[],branch_timeouts=0,
        interface_repairs=[],bounded_history_multiplier=2,activated=False))
    if not scene.branch_routing.get('active',False):
        stop=min(deadline,perf_counter()+1.) if deadline is not None else perf_counter()+1.
        try:
            path,owners=_find_path(scene,net,start,tree,occupied,margin,history,statistics,
                checks,prepared,deadline=stop,extra_points=extra_points,initial_direction=initial_direction,
                precheck_goals=True)
            if path is not None:return path,owners
            reason=statistics.get('branch_rejection','no_path')
        except RoutingFailure as exc:
            if deadline is not None and perf_counter()>=deadline:raise
            if exc.diagnostics.get('reason') not in ('quality_time_budget','state_budget','graph_node_budget'):raise
            reason=exc.diagnostics.get('reason')
        scene.branch_routing['active']=True
        report.update(activated=True,activation_net=net,activation_reason=reason)
    else:report['activated']=True
    radius=8*max(scene.options.cross_clearance,scene.options.center_spacing,scene.options.grid_step)
    blockers=Counter()
    for window,seconds in ((radius,.45),(radius*2,.9),(None,2.)):
        stop=min(deadline,perf_counter()+seconds) if deadline is not None else perf_counter()+seconds
        try:
            path,owners=_find_path(scene,net,start,tree,occupied,margin,history,statistics,
                checks,prepared,deadline=stop,extra_points=extra_points,initial_direction=initial_direction,
                window=window,precheck_goals=True,cap_history=True)
            blockers.update(owners)
            if len(report['windows'])<80:report['windows'].append(dict(net=net,radius=window,found=path is not None))
            if path is not None:return path,blockers
            if statistics.get('branch_rejection') in ('source_interface','no_safe_goal'):break
        except RoutingFailure as exc:
            if deadline is not None and perf_counter()>=deadline:raise
            if exc.diagnostics.get('reason') not in ('quality_time_budget','state_budget','graph_node_budget'):raise
            blockers.update(exc.diagnostics.get('blocking_nets',{}));report['branch_timeouts']+=1
    return None,blockers


def repair_branch(scene,net,tree,pins,occupied,margin,statistics,deadline):
    from netlist2schematic.recovery.local_rescue import _route_local_net
    from netlist2schematic.routing.orthogonal_router import RoutingFailure
    record=dict(net=net,retained_segments=len(tree),branches=[],port_plans=0)
    stop=min(deadline,perf_counter()+3.) if deadline is not None else perf_counter()+3.
    try:
        result,owners,direct,failure=_route_local_net(scene,net,occupied,margin,statistics,stop,record,
            pins_override=pins,initial_tree=tree)
        record.update(repaired=result is not None,failure=failure)
    except RoutingFailure as exc:
        if deadline is not None and perf_counter()>=deadline:raise
        if exc.diagnostics.get('reason')!='quality_time_budget':raise
        result,owners,direct=None,Counter(exc.diagnostics.get('blocking_nets',{})),Counter()
        record.update(repaired=False,failure=dict(reason='branch_interface_time_budget'))
    statistics.setdefault('regional_search',{}).setdefault('interface_repairs',[]).append(record)
    return result,owners+direct
