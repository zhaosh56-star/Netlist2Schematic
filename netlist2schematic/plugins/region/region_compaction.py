"""Safely close excess space, retaining independent regions' track capacity.

A monotone x mapping translates symbols and their fixed exits without scaling
them. Existing orthogonal wires use the same mapping; no search is performed.
Only complete independent validation can accept a proposed compression.
"""
from __future__ import annotations

from copy import deepcopy
from math import ceil, floor, isfinite
from statistics import median
from time import perf_counter

from netlist2schematic.routing.routing_rules import EPS, RoutingOptions, build_scene
from netlist2schematic.routing.sa_routing import validate_final_routes, RoutingValidationError
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.plugins.region.region_placement import _leaves, _region_gap
from netlist2schematic.plugins.region.region_layout import measure_regions

VERSION='region-route-compaction-v2'


def _free_strips(scene,pl,comps,nets,index,plan,*,channel_mode='attachments'):
    body={i:(rect[0],rect[2]) for i,rect in scene.body_rects}
    protected=list(body.values())+[(rect[0],rect[2]) for _,rect in scene.marker_rects]
    for i in body:
        axis=pl.column_x[pl.cell_of[i][1]]
        body[i]=min(body[i][0],axis),max(body[i][1],axis)
        protected.append(body[i])
    for pins in scene.pins.values():
        for pin in pins:
            interval=sorted((pin.point[0],pin.escape[0]))
            lo,hi=body[pin.component]
            body[pin.component]=min(lo,*interval),max(hi,*interval)
            protected.append(tuple(interval))
    protected.extend((min(p[0],q[0]),max(p[0],q[0]))
                     for segments in scene.fixed_segments.values() for p,q in segments)
    leaves=_leaves(plan)
    envelopes={r['id']:(min(body[index[n]][0] for n in r['members']),
                            max(body[index[n]][1] for n in r['members'])) for r in leaves}
    leaves=sorted(leaves,key=lambda r:envelopes[r['id']])
    protected=sorted(protected)
    for left,right in zip(leaves,leaves[1:]):
        independent=bool(_region_gap(left,right,comps,index,nets,plan.get('power_interfaces',())))
        if independent != (channel_mode == 'independent'):
            continue
        a,b=envelopes[left['id']][1],envelopes[right['id']][0]
        if b<=a+EPS:
            continue
        cursor=a
        for lo,hi in protected:
            if hi<=cursor or lo>=b:
                continue
            if lo>cursor:
                yield cursor,min(lo,b),left['id'],right['id']
            cursor=max(cursor,hi)
            if cursor>=b:
                break
        if cursor<b:
            yield cursor,b,left['id'],right['id']


def _channel_reservation(scene,routed,a,b,reference_pitch):
    """Reserve a fixed nominal half-pitch plus all existing vertical lanes.

    The nominal pitch is frozen for the whole pass. Repeated accepted trials
    cannot shrink the reservation by recomputing it from compressed axes.
    Counting unique x coordinates ignores arbitrary collinear wire cuts.
    """
    tracks={p[0] for _,segments in routed for p,q in segments
            if abs(p[0]-q[0])<=EPS and abs(p[1]-q[1])>EPS and a<p[0]<b}
    options=scene.options
    width=ceil(max(reference_pitch/2,2*options.cross_clearance+
        max(0,len(tracks)-1)*options.center_spacing)/options.grid_step)*options.grid_step
    return width,len(tracks)


def _mapping(a,b,delta,step):
    def xmap(x):
        if x<=a:return x
        if x>=b:return x-delta
        return round((a+(x-a)*(1-delta/(b-a)))/step)*step
    return xmap


def _trial(pl,comps,index,routed,scene,a,b,delta,step):
    xmap=_mapping(a,b,delta,step)
    xs=set(pl.column_x)
    xs.update(p[0] for _,segments in routed for segment in segments for p in segment)
    xs.update(p[0] for points in scene.all_pins.values() for p in points)
    ordered=sorted(xs)
    mapped=[xmap(x) for x in ordered]
    if any(y<=x+EPS for x,y in zip(mapped,mapped[1:])):
        return None,'collapsed_coordinate_order'
    trial=deepcopy(pl)
    trial.column_x=[xmap(x) for x in pl.column_x]
    fixed={index.get(i,i) for i in set(getattr(pl,'fixed',())) | set(getattr(pl,'fixed_cells',{}))}
    for i in range(len(comps)):
        if i in fixed and trial.pos_of(i)!=pl.pos_of(i):
            return None,'fixed_position'
        offset=getattr(trial,'offset_x',[0.0]*len(comps))[i]
        col=trial.cell_of[i][1]
        axis=trial.column_x
        before=(axis[col]-axis[col-1])/2 if col else ((axis[1]-axis[0])/2 if len(axis)>1 else 75)
        after=(axis[col+1]-axis[col])/2 if col+1<len(axis) else before
        if not -before-EPS<=offset<=after+EPS:
            return None,'offset_midpoint_limit'
        for pin in comps[i]['nodes']:
            old,new=pl.pin_world(i,pin),trial.pin_world(i,pin)
            if abs(new[0]-xmap(old[0]))>EPS or abs(new[1]-old[1])>EPS:
                return None,'pin_geometry_not_translation'
    transformed=[(net,[((xmap(p[0]),p[1]),(xmap(q[0]),q[1])) for p,q in segments]) for net,segments in routed]
    return (trial,transformed),None


def compact_region_routes(par,pl,comps,nets,index,routed_result,plan,*,options=None,
                          deadline=None,seconds=1.0,max_candidates=12,prevalidated=False,
                          channel_mode='attachments',reference_pitch=None):
    """Keep the latest verified incumbent on rejection, quota or timeout.

    ``prevalidated`` is only for the flow's immediate final_route() return;
    saved JSON callers must leave it false and revalidate the original wires.
    """
    if type(seconds) not in (int,float) or not isfinite(seconds) or seconds<0:
        raise ValueError('region compaction seconds must be finite and nonnegative')
    if type(max_candidates) is not int or max_candidates<0:
        raise ValueError('region compaction candidates must be nonnegative integers')
    if channel_mode not in ('attachments','independent'):
        raise ValueError('region compaction channel_mode must be attachments or independent')
    if reference_pitch is not None and (type(reference_pitch) not in (int,float)
            or not isfinite(reference_pitch) or reference_pitch<=0):
        raise ValueError('region compaction reference_pitch must be finite and positive')
    started=perf_counter()
    until=min(started+seconds,deadline) if deadline is not None else started+seconds
    options=options or RoutingOptions()
    routed,metrics,validation=routed_result
    search_validation=deepcopy(validation)
    report=dict(version=VERSION,applied=False,candidates=0,accepted=0,actions=[],rejections=[],
        before=deepcopy(metrics),after=deepcopy(metrics),stop_reason='no_supported_gap',
        coordinate_mode='shared_x_axes_and_wires',channel_mode=channel_mode,
        sa_called=False,astar_called=False)
    if not prevalidated:
        routed,metrics,validation=validate_final_routes(par,nets,pl,index,routed,options=options,deadline=deadline)
        report['before']=deepcopy(metrics)
    elif not validation.get('valid'):
        raise ValueError('region compaction requires validated original routes')
    current=pl
    if not plan or not plan.get('supported') or not hasattr(pl,'column_x'):
        report['stop_reason']='no_supported_regions'
    elif getattr(pl,'power_connections',None) is not None:
        report['stop_reason']='custom_power_geometry'
    else:
        original_org=measure_regions(pl,comps,index,plan)
        if not original_org.get('measurable'):
            report['stop_reason']='unmeasurable_region_evidence'
        else:
            nominal_pitch=reference_pitch
            if nominal_pitch is None:
                nominal_pitch=(median(b-a for a,b in zip(pl.column_x,pl.column_x[1:]))
                               if len(pl.column_x)>1 else 150)
            if channel_mode=='independent':
                report['reference_pitch']=nominal_pitch
            stop=False
            for _ in range(4):
                if perf_counter()>=until or report['candidates']>=max_candidates:
                    report['stop_reason']='time_budget' if perf_counter()>=until else 'candidate_limit'
                    break
                try:
                    scene=build_scene(par,nets,current,index,options=options,deadline=until)
                except RoutingFailure:
                    report['stop_reason']='time_budget';break
                pitch=median(b-a for a,b in zip(current.column_x,current.column_x[1:])) if len(current.column_x)>1 else 150
                improved=False
                for lo,hi,left,right in _free_strips(scene,current,comps,nets,index,plan,
                                                    channel_mode=channel_mode):
                    a=ceil((lo+EPS)/options.grid_step)*options.grid_step
                    b=floor((hi-EPS)/options.grid_step)*options.grid_step
                    reserve,tracks=(_channel_reservation(scene,routed,a,b,nominal_pitch)
                        if channel_mode=='independent' else (2*options.center_spacing,0))
                    maximum=floor(min(pitch,max(0,b-a-reserve))/options.grid_step)*options.grid_step
                    amounts=sorted({floor(maximum*f/options.grid_step)*options.grid_step for f in (1,.5,.25)},reverse=True)
                    for delta in amounts:
                        if delta<=0:continue
                        if perf_counter()>=until or report['candidates']>=max_candidates:
                            report['stop_reason']='time_budget' if perf_counter()>=until else 'candidate_limit'
                            stop=True;break
                        report['candidates']+=1
                        result,reason=_trial(current,comps,index,routed,scene,a,b,delta,options.grid_step)
                        if reason:
                            report['rejections'].append(reason);continue
                        trial,mapped=result
                        try:
                            new_routes,new_metrics,new_validation=validate_final_routes(
                                par,nets,trial,index,mapped,options=options,deadline=until)
                        except RoutingValidationError:
                            report['rejections'].append('complete_geometry_rejected');continue
                        except RoutingFailure:
                            report['stop_reason']='time_budget';stop=True;break
                        after_org=measure_regions(trial,comps,index,plan)
                        fields=('interleaving','order_errors','pin_axis_error','axis_drift','junction_error','dispersion')
                        if (not after_org.get('measurable') or any(after_org[k]>original_org[k]+EPS for k in fields)):
                            report['rejections'].append('region_organization_regression');continue
                        if (new_metrics['wirelength']>=metrics['wirelength']-EPS
                                or new_metrics['crossings']>metrics['crossings'] or new_metrics['bends']>metrics['bends']):
                            report['rejections'].append('no_safe_cost_improvement');continue
                        action=dict(left=left,right=right,strip=[a,b],removed_width=delta,
                            metrics_before=deepcopy(metrics),metrics_after=deepcopy(new_metrics))
                        if channel_mode=='independent':
                            action.update(reserved_width=reserve,actual_vertical_tracks=tracks,
                                          remaining_strip=b-a-delta)
                        report['actions'].append(action)
                        current,routed,metrics,validation=trial,new_routes,new_metrics,new_validation
                        # Original search diagnostics remain historical evidence;
                        # geometry and validity always come from the new check.
                        validation={**search_validation,**validation}
                        if 'search' in search_validation:
                            validation['search_geometry_stage']='before_region_compaction'
                        original_org=after_org
                        report['accepted']+=1;improved=True
                        break
                    if improved or stop:break
                if stop:break
                if not improved:
                    report['stop_reason']='converged';break
            else:
                report['stop_reason']='round_limit'
    report.update(applied=bool(report['accepted']),after=deepcopy(metrics),elapsed_seconds=perf_counter()-started)
    return current,(routed,metrics,validation),report
