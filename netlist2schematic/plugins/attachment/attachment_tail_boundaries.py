"""Project a local tail offset onto geometry-derived free grid intervals.

Only envelopes of bodies, fixed exits and real power markers are used here.
This is candidate construction, not the static or final acceptance oracle.
It solves one-dimensional intervals; it does not scan poses, search routes,
move other devices or open another time/count budget.
"""
from copy import deepcopy
from math import ceil,floor

from netlist2schematic.routing.routing_rules import EPS,build_scene,check_deadline
from netlist2schematic.plugins.layout_shared.branch_alignment import _bounds

VERSION='tail-offset-intervals-v1'
MAX_PARTIAL_CANDIDATES=2


def _gap(a0,a1,b0,b1):return max(min(a0,a1)-max(b0,b1),min(b0,b1)-max(a0,a1),0.)


def _free_bands(first,last,forbidden):
    """Complement merged closed integer intervals, no point-by-point probing."""
    merged=[]
    for lo,hi in sorted((max(first,a),min(last,b)) for a,b in forbidden if b>=first and a<=last):
        if lo>hi:continue
        if merged and lo<=merged[-1][1]+1:merged[-1]=(merged[-1][0],max(hi,merged[-1][1]))
        else:merged.append((lo,hi))
    bands=[];cursor=first
    for lo,hi in merged:
        if cursor<lo:bands.append((cursor,lo-1))
        cursor=max(cursor,hi+1)
    if cursor<=last:bands.append((cursor,last))
    return bands


def _geometry_bands(par,pl,nets,index,i,options,deadline,diagnostic,excluded=()):
    scene=build_scene(par,nets,pl,index,options=options,deadline=deadline)
    diagnostic['scene_templates']+=1
    if scene.power_connections is not None:
        diagnostic['stop_reason']='custom_power_geometry';return []
    moving_rects=[rect for owner,rect in scene.body_rects if owner==i]
    foreign_rects=[rect for owner,rect in scene.body_rects if owner!=i]
    owners=[index[name] for net,members in nets.items() if par.power_marker_sym(net) for name,pin in members]
    for (net,rect),owner in zip(scene.marker_rects,owners):
        (moving_rects if owner==i else foreign_rects).append(rect)
    moving_segments=[];foreign_segments=[];moving_points=[];foreign_points=[]
    for net,members in nets.items():
        check_deadline(deadline,'tail_offset_intervals')
        for name,pin in members:
            owner=index[name];point=tuple(pl.pin_world(owner,pin))
            (moving_points if owner==i else foreign_points).append((net,point))
        if net in scene.power_nets:
            for (name,pin),segment in zip(members,scene.fixed_segments[net]):
                (moving_segments if index[name]==i else foreign_segments).append((net,*segment))
        else:
            for pin in scene.pins.get(net,()):
                (moving_segments if pin.component==i else foreign_segments).append((net,*pin.stub))
    step=options.grid_step;base=pl.offset_x[i];lower,upper=_bounds(pl,i)
    first,last=ceil((lower-EPS)/step),floor((upper+EPS)/step);forbidden=[]

    def interval(lo,hi,*,strict=False):
        lo+=base;hi+=base
        a,b=(floor(lo/step)+1,ceil(hi/step)-1) if strict else (ceil(lo/step),floor(hi/step))
        if a<=b:forbidden.append((a,b))

    for rect in moving_rects:
        check_deadline(deadline,'tail_body_intervals')
        for other in foreign_rects:
            if max(rect[1],other[1])<min(rect[3],other[3])-EPS:
                interval(other[0]-rect[2]+EPS,other[2]-rect[0]-EPS,strict=True)
        for net,a,b in foreign_segments:
            if _gap(rect[1],rect[3],a[1],b[1])<=EPS:
                interval(min(a[0],b[0])-rect[2]-EPS,max(a[0],b[0])-rect[0]+EPS)
    for net,a,b in moving_segments:
        check_deadline(deadline,'tail_corridor_intervals')
        for rect in foreign_rects:
            if _gap(a[1],b[1],rect[1],rect[3])<=EPS:
                interval(rect[0]-max(a[0],b[0])-EPS,rect[2]-min(a[0],b[0])+EPS)
        for other,c,d in foreign_segments:
            clearance=options.center_spacing-EPS
            if other!=net and _gap(a[1],b[1],c[1],d[1])<clearance:
                interval(min(c[0],d[0])-max(a[0],b[0])-clearance,
                         max(c[0],d[0])-min(a[0],b[0])+clearance,strict=True)
        for other,p in foreign_points:
            clearance=options.cross_clearance-EPS
            if other!=net and _gap(a[1],b[1],p[1],p[1])<clearance:
                interval(p[0]-max(a[0],b[0])-clearance,p[0]-min(a[0],b[0])+clearance,strict=True)
    for net,p in moving_points:
        check_deadline(deadline,'tail_pin_intervals')
        for other,a,b in foreign_segments:
            clearance=options.cross_clearance-EPS
            if other!=net and _gap(p[1],p[1],a[1],b[1])<clearance:
                interval(min(a[0],b[0])-p[0]-clearance,max(a[0],b[0])-p[0]+clearance,strict=True)
    forbidden.extend((int(round(value/step)),int(round(value/step))) for value in excluded)
    diagnostic['forbidden_intervals']+=len(forbidden)
    return _free_bands(first,last,forbidden)


def partial_tail_trials(par,pl,comps,nets,index,group,*,options,deadline,diagnostic,limit=2,excluded=()):
    from netlist2schematic.plugins.attachment.attachment_tail_transistors import _target
    from netlist2schematic.plugins.attachment.attachment_integration import _fixed_ids
    i=index[group['tail']]
    if limit<=0:diagnostic['stop_reason']='no_proposal_allowance';return
    if i in _fixed_ids(pl,index):diagnostic['stop_reason']='fixed_tail';return
    target=_target(pl,index,group)[0];old_error=abs(pl.pin_world(i,group['pin'])[0]-target)
    if old_error<=EPS:diagnostic['stop_reason']='already_aligned';return
    choices=[];step=options.grid_step
    original_offset=getattr(pl,'offset_x',[0.0]*len(comps))[i]
    for orientation in pl.allowed_orientations(i):
        if orientation not in (0,1):continue
        check_deadline(deadline,'tail_partial_projection')
        template=deepcopy(pl);template.orient[i]=orientation
        template.offset_x=list(getattr(pl,'offset_x',[0.0]*len(comps)))
        bands=_geometry_bands(par,template,nets,index,i,options,deadline,diagnostic,
            excluded=[value for orient,value in excluded if orient==orientation])
        wanted=template.offset_x[i]+target-template.pin_world(i,group['pin'])[0]
        for lo,hi in bands:
            for k in {max(lo,min(hi,floor(wanted/step))),max(lo,min(hi,ceil(wanted/step)))}:
                value=k*step;point=template.pin_world(i,group['pin'])[0]+value-template.offset_x[i]
                error=abs(point-target)
                if error>=old_error-EPS:continue
                key=(error,abs(value-original_offset),orientation!=pl.orient[i],orientation,value)
                choices.append((key,template,value))
    seen=set()
    for key,template,value in sorted(choices,key=lambda row:row[0]):
        check_deadline(deadline,'tail_partial_candidate')
        identity=(template.orient[i],value)
        if identity in seen:continue
        seen.add(identity)
        if len(seen)>min(limit,MAX_PARTIAL_CANDIDATES):break
        trial=deepcopy(template);trial.offset_x[i]=value
        diagnostic['generated']+=1;diagnostic['stop_reason']='generated'
        yield trial,dict(kind='exclusive_tail_partial_axis',version=VERSION,group=group['id'],
            tails=[group['tail']],members=[group['tail']],atomic_swap=False,
            rows_unchanged=True,axes_unchanged=True,actual_pins=True,
            projected_dx=value,predicted_residual=key[0],projection='geometry_free_grid_interval')
    if not seen:diagnostic['stop_reason']='no_improving_free_interval'
