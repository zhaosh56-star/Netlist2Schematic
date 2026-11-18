"""Reserve real symbol space on an active grid, then place off-grid guests.

No A* or annealing runs here. Every completed batch is checked against the
original fixed exits, bodies and rail markers; an incomplete packing is not
returned as a usable layout.
"""
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict
from math import ceil, isfinite
from statistics import median
from time import perf_counter

from netlist2schematic.plugins.passive_embedding.passive_embedding import VECTORS
from netlist2schematic.plugins.passive_skeleton.module_geometry import template
from netlist2schematic.plugins.passive_skeleton.plan import ACTIVE, PASSIVE, host_pairs, measure
from netlist2schematic.plugins.attachment.attachment_physical import body_rect
from netlist2schematic.routing.routing_rules import _escape, _inflate

EPS = 1e-8


def snap(value, step):
    return round(value/step)*step


def _point_exit(par, pl, index, terminal, options):
    name, pin = terminal; i = index[name]
    point = pl.pin_world(i, pin)
    direction = VECTORS[pl.pin_dir(i, pin)]
    return _escape(point, direction, _inflate(body_rect(par, pl, i), options.halo), options.grid_step)


def expand_axis(pl, axis, boundary, amount, fixed=()):
    """Shift one axis suffix; no individual active row/column can be scrambled."""
    if amount <= EPS:
        return True
    values = pl.column_x if axis == 0 else pl.row_y
    dimension = 1-axis
    if boundary <= 0 or boundary >= len(values):
        return False
    if any(pl.cell_of[i][dimension] >= boundary for i in fixed):
        return False
    for k in range(boundary, len(values)):
        values[k] += amount
    return True


def _expand_between(pl, index, a, b, axis, minimum, step, fixed, actions, reason):
    ia, ib = index[a[0]], index[b[0]]
    pa, pb = pl.pin_world(ia, a[1]), pl.pin_world(ib, b[1])
    gap = abs(pa[axis]-pb[axis]); needed = max(0., ceil((minimum-gap)/step)*step)
    if needed <= EPS:
        return True
    coordinate = 1-axis
    ca, cb = pl.cell_of[ia][coordinate], pl.cell_of[ib][coordinate]
    if ca == cb:
        return False
    boundary = max(ca, cb)
    if not expand_axis(pl, axis, boundary, needed, fixed):
        return False
    actions.append(dict(axis='x' if axis == 0 else 'y', boundary=boundary,
        added=needed, minimum_span=minimum, reason=reason))
    return True


def make_frame(par, core, comps, index, options):
    """Lift active indices once. Logical guest cells are bookkeeping anchors."""
    from netlist2schematic.layout.structured_placement import StructuredPlacement
    pl = StructuredPlacement.__new__(StructuredPlacement)
    pl.comps = comps; pl.rng = deepcopy(core.rng); pl.cols = core.cols
    pl.column_x = list(core.column_x); pl.row_y = list(core.row_y)
    pl.cell_of = [None]*len(comps); pl.orient = [0]*len(comps)
    mapping = {i:index[c['id']] for i,c in enumerate(core.comps)}
    if hasattr(core,'offset_x'):
        pl.offset_x=[0.]*len(comps)
        for i,j in mapping.items():pl.offset_x[j]=core.offset_x[i]
    for i,j in mapping.items():
        pl.cell_of[j] = core.cell_of[i]; pl.orient[j] = core.orient[i]
    passive = [i for i,c in enumerate(comps) if c['symbol'] in PASSIVE]
    start = len(pl.row_y)
    pitch = median([b-a for a,b in zip(pl.row_y,pl.row_y[1:])]) if len(pl.row_y)>1 else 120.
    for n,i in enumerate(passive):
        row = start+n//pl.cols; col = n%pl.cols
        while len(pl.row_y)<=row:
            pl.row_y.append(pl.row_y[-1]+pitch)
        pl.cell_of[i] = row,col
        # Parking must itself be geometrically legal, including rail stubs.
        best=[]
        for orientation in range(pl.n_orients(i)):
            pl.orient[i]=orientation
            score=0
            for pin,net in comps[i]['nodes'].items():
                marker=par.power_marker_sym(net)
                if marker:
                    score+=pl.pin_dir(i,pin)!=('south' if marker=='ground' else 'north')
            best.append((score,orientation))
        pl.orient[i]=min(best)[1]
    pl.row_of=[r for r,c in pl.cell_of];pl.comp_at={cell:i for i,cell in enumerate(pl.cell_of)}
    pl.blocks=[[mapping[i] for i in block] for block in core.blocks]+[[i] for i in passive]
    pl.block_of={i:k for k,block in enumerate(pl.blocks) for i in block}
    pl.hierarchy_guards=[[mapping[i] for i in block] for block in getattr(core,'hierarchy_guards',())]
    pl.role_orders=[[[mapping[i] for i in row] for row in group] for group in core.role_orders]
    pl.fixed={mapping[i] if isinstance(i,int) else i for i in getattr(core,'fixed',())}
    pl.fixed_cells={mapping[i]:cell for i,cell in getattr(core,'fixed_cells',{}).items()}
    pl.source_orientation_ids={mapping[i] for i in getattr(core,'source_orientation_ids',())}
    pl.terminal_orientation_ids=set(passive)
    if getattr(core, 'column_plan', {}).get('protected_plan') is not None:
        pl.column_plan = deepcopy(core.column_plan)
    pl.passive_local_offsets=[[0.,0.] for c in comps]
    # Unplaced guests never occupy an active slot or constrain core packing.
    far_x=max(core.column_x)+300; far_y=max(core.row_y)+300
    for n,i in enumerate(passive):
        x,y=pl.pos_of(i)
        pl.passive_local_offsets[i]=[far_x+220*n-x,far_y-y]
    return pl


def _geometry(par, pl, comps, index, group, vector, options):
    tree=deepcopy(group['module'])
    if group.get('mirror_bank') and tree['kind']=='parallel':
        tree['children'].reverse()
    geo=template(par,pl,comps,index,tree,vector,options)
    if geo is None:
        raise ValueError('A passive module has no compatible legal orientation')
    space=2*options.halo+max(options.center_spacing,options.cross_clearance)
    return (geo['positions'],geo['orientations'],geo['axis'],geo['extent'],space,
            geo['envelope'],dict(routes=geo['wires'],ports=geo['ports'],group=group['id']))


def _translated(pl, geometry, target, step):
    trial=deepcopy(pl);positions,orientations,*_=geometry
    for i,(x,y) in positions.items():
        ox,oy=pl.pos_of(i);base=pl.passive_local_offsets[i]
        trial.passive_local_offsets[i]=[snap(x+target[0],step)-(ox-base[0]),
                                        snap(y+target[1],step)-(oy-base[1])]
        trial.orient[i]=orientations[i]
    connection=deepcopy(geometry[6])
    def moved(point):
        return tuple(snap(point[k]+target[k],step) for k in (0,1))
    connection['routes']={net:[(moved(a),moved(b)) for a,b in segments]
                          for net,segments in connection['routes'].items()}
    connection['internal_routes']=deepcopy(connection['routes'])
    connection['ports']=[moved(p) for p in connection['ports']]
    trial.passive_module_guides=deepcopy(getattr(pl,'passive_module_guides',{}))
    trial.passive_module_guides[connection['group']]=connection
    return trial


def _anchor_geometry(par, pl, comps, index, group, options):
    """A typed direction and real port pair, not a generic net-box centroid."""
    if group.get('slot_ports') is not None:
        return group['slot_ports']
    a,b=group['ends']
    live=[n for n in (a,b) if group['anchors'][n]]
    if len(live)==2:
        if group['role'] in ('conduction_inline','source_control_chain'):
            aa,bb=host_pairs(pl,comps,index,group)[0]
            # Ordering follows real heights, so PMOS duals need no special case.
            pa,pb=(pl.pin_world(index[n],p) for n,p in (aa,bb))
            vector=(0,1 if pb[1]>=pa[1] else -1)
        else:
            aa,bb=host_pairs(pl,comps,index,group)[0]
            pa,pb=(pl.pin_world(index[n],p) for n,p in (aa,bb))
            vector=(1 if pb[0]>=pa[0] else -1,0)
        return vector,aa,bb,None
    if not live:
        return None
    net=live[0]
    rail=next((n for n in (a,b) if par.power_marker_sym(n)),None)
    if group.get('owner_port'):
        terminal=group['owner_port']['terminal']
    else:
        terminals=group['anchors'][net]
        conductive=[t for t in terminals if t[1] in ('D','S','C','E') and comps[index[t[0]]]['symbol'] in ACTIVE]
        if conductive:terminals=conductive
        terminal=min(terminals,key=lambda t:(-pl.pin_world(index[t[0]],t[1])[1] if rail and par.power_marker_sym(rail)=='ground'
                                              else pl.pin_world(index[t[0]],t[1])[1],pl.pin_world(index[t[0]],t[1])[0]))
    if rail is not None:
        outward=(0,1 if par.power_marker_sym(rail)=='ground' else -1)
    else:
        outward=VECTORS[pl.pin_dir(index[terminal[0]],terminal[1])]
        if terminal[1] in ('D','C') and comps[index[terminal[0]]]['symbol'] in ACTIVE:
            control='G' if terminal[1]=='D' else 'B'
            gate=VECTORS[pl.pin_dir(index[terminal[0]],control)]
            outward=(-gate[0],-gate[1])
    vector=outward if net==a else tuple(-v for v in outward)
    return vector,terminal,None,net


def reserve(par, pl, comps, index, plan, options, fixed):
    actions=[];groups=deepcopy(plan['groups']);by_id={g['id']:g for g in groups}
    for g in groups:
        g['slot_ports']=_anchor_geometry(par,pl,comps,index,g,options)
    # Paired banks must both fit before either bank is placed.
    for family in plan['families']:
        ordered=sorted(family['groups'],key=lambda gid:pl.pos_of(index[by_id[gid]['owner_port']['terminal'][0]])[0])
        by_id[ordered[1]]['mirror_bank']=True
        terminals=[];widths=[]
        for gid in ordered:
            g=by_id[gid];vector,a,b,net=_anchor_geometry(par,pl,comps,index,g,options)
            geo=_geometry(par,pl,comps,index,g,vector,options)
            widths.append(geo[5][2]-geo[5][0]);terminals.append(a)
        minimum=sum(widths)/2+2*options.halo+2*options.center_spacing
        if not _expand_between(pl,index,*terminals,0,minimum,options.grid_step,fixed,actions,'paired_attachment_width'):
            return None,actions
    for g in groups:
        anchors=_anchor_geometry(par,pl,comps,index,g,options)
        if anchors is None:
            return None,actions
        vector,a,b,net=anchors
        if b is None:
            continue
        geo=_geometry(par,pl,comps,index,g,vector,options);axis=geo[2]
        pa,pb=(pl.pin_world(index[n],p) for n,p in (a,b))
        ea,eb=(_point_exit(par,pl,index,t,options) for t in (a,b))
        # Both the host and guest have immutable exits. Reserve their opposing
        # approach segments as well as the visible symbol, not just a body gap.
        minimum=geo[3]+sum(abs(e[0]-p[0])+abs(e[1]-p[1]) for e,p in ((ea,pa),(eb,pb)))+2*(options.halo+options.grid_step)+2*options.center_spacing
        if pl.cell_of[index[a[0]]][1-axis]!=pl.cell_of[index[b[0]]][1-axis]:
            if not _expand_between(pl,index,a,b,axis,minimum,options.grid_step,fixed,actions,'inline_length' if axis else 'bridge_length'):
                return None,actions
        # A horizontal bridge sits above the corresponding drain row. Preserve
        # that lane instead of sending its parts under the entire active core.
        if axis==0:
            row=min(pl.cell_of[index[t[0]]][0] for t in (a,b))
            if row>0:
                previous=[i for i,c in enumerate(comps) if c['symbol'] not in PASSIVE and pl.cell_of[i][0]<row]
                if previous:
                    top=max(body_rect(par,pl,i)[3] for i in previous)
                    clearance=geo[5][3]-geo[5][1]+2*options.halo+options.center_spacing
                    lower=min(_point_exit(par,pl,index,t,options)[1] for t in (a,b))
                    missing=max(0.,ceil((top+clearance-lower)/options.grid_step)*options.grid_step)
                    if missing:
                        if not expand_axis(pl,1,row,missing,fixed):return None,actions
                        actions.append(dict(axis='y',boundary=row,added=missing,reason='bridge_lane_height'))
    if not _reserve_control_access(par,pl,comps,index,groups,options,fixed,actions):
        return None,actions
    # Axis suffixes also move parked bookkeeping cells. Restore their absolute
    # separation so unplaced guests cannot create spurious static collisions.
    far_x=max(pl.column_x)+300;far_y=max(pl.row_y)+300
    for number,i in enumerate(j for j,c in enumerate(comps) if c['symbol'] in PASSIVE):
        x,y=pl.pos_of(i);dx,dy=pl.passive_local_offsets[i]
        pl.passive_local_offsets[i]=[dx+far_x+220*number-x,dy+far_y-y]
    return groups,actions


def _reserve_control_access(par,pl,comps,index,groups,options,fixed,actions):
    """Make room for inward-facing input chains without changing the core.

    A one-ended chain needs a corridor too. Two chains facing into the same
    gap reserve that gap together; common supply nodes alone do not do so.
    """
    def footprint(g):
        vector,a,b,net=_anchor_geometry(par,pl,comps,index,g,options)
        outward=vector if net==g['ends'][0] else tuple(-v for v in vector)
        if b is not None or a[1] not in ('G','B') or g['role']!='port_extension':return None
        if outward!=VECTORS[pl.pin_dir(index[a[0]],a[1])]:return None
        trial=next(slot_trials(par,pl,comps,index,g,options),None)
        if trial is None:return None
        boxes=[body_rect(par,trial,index[n]) for n in g['members']]
        rect=(min(r[0] for r in boxes),min(r[1] for r in boxes),max(r[2] for r in boxes),max(r[3] for r in boxes))
        return index[a[0]],outward,rect
    def overlaps(a,b):
        gap=2*options.halo+options.center_spacing
        return all(min(a[k+2],b[k+2])+gap>max(a[k],b[k])+EPS for k in (0,1))
    for _ in range(2):
        changed=False
        for g in groups:
            result=footprint(g)
            if result is None:continue
            host,direction,rect=result;axis=0 if direction[0] else 1;dimension=1-axis
            for j,c in enumerate(comps):
                if j==host or c['symbol'] in PASSIVE:continue
                other=body_rect(par,pl,j)
                gap_cells=pl.cell_of[j][dimension]-pl.cell_of[host][dimension]
                if gap_cells*direction[axis]<=0 or not overlaps(rect,other):continue
                clearance=2*options.halo+options.center_spacing
                missing=(rect[axis+2]+clearance-other[axis] if direction[axis]>0
                         else other[axis+2]+clearance-rect[axis])
                amount=max(0,ceil(missing/options.grid_step)*options.grid_step)
                boundary=max(pl.cell_of[host][dimension],pl.cell_of[j][dimension])
                if amount and not expand_axis(pl,axis,boundary,amount,fixed):return False
                if amount:
                    actions.append(dict(axis='x' if axis==0 else 'y',boundary=boundary,added=amount,
                        group=g['id'],reason='control_attachment_corridor'))
                    changed=True;break
        profiles=[(g,footprint(g)) for g in groups]
        for k,(g,a) in enumerate(profiles):
            if a is None:continue
            for other,b in profiles[k+1:]:
                if b is None or a[1]!=tuple(-v for v in b[1]) or not overlaps(a[2],b[2]):continue
                axis=0 if a[1][0] else 1;dimension=1-axis
                if pl.cell_of[a[0]][dimension]==pl.cell_of[b[0]][dimension]:continue
                left,right=(a,b) if pl.cell_of[a[0]][dimension]<pl.cell_of[b[0]][dimension] else (b,a)
                amount=max(0,ceil((left[2][axis+2]+2*options.halo+options.center_spacing-right[2][axis])/options.grid_step)*options.grid_step)
                boundary=pl.cell_of[right[0]][dimension]
                if amount and not expand_axis(pl,axis,boundary,amount,fixed):return False
                if amount:
                    actions.append(dict(axis='x' if axis==0 else 'y',boundary=boundary,added=amount,
                        groups=[g['id'],other['id']],reason='paired_control_attachment_corridor'))
                    changed=True
        if not changed:break
    return True


def slot_trials(par, pl, comps, index, group, options):
    anchored=_anchor_geometry(par,pl,comps,index,group,options)
    if anchored is None:
        return
    vector,a,b,net=anchored
    geometry=_geometry(par,pl,comps,index,group,vector,options)
    positions,orientations,axis,extent,space,envelope,connection=geometry
    transverse=1-axis;step=options.grid_step
    pa=pl.pin_world(index[a[0]],a[1]);ea=_point_exit(par,pl,index,a,options)
    targets=[]
    if b is not None:
        pb=pl.pin_world(index[b[0]],b[1]);eb=_point_exit(par,pl,index,b,options)
        target=[(ea[0]+eb[0])/2,(ea[1]+eb[1])/2]
        target[axis]-=vector[axis]*extent/2
        if axis==1:
            # An R/L inserted in a conduction edge stays on that branch axis.
            target[0]=(pa[0]+pb[0])/2
            if group['role']=='source_control_chain':
                control=next((t for ts in group['anchors'].values() for t in ts if t[1] in ('G','B') and comps[index[t[0]]]['symbol'] in ACTIVE),None)
                if control:
                    exit_point=_point_exit(par,pl,index,control,options)
                    target[0]=exit_point[0]+VECTORS[pl.pin_dir(index[control[0]],control[1])][0]*(options.halo+options.center_spacing)
                    source=next(t for ts in group['anchors'].values() for t in ts if comps[index[t[0]]]['symbol'] in ('voltage-source','current-source'))
                    sign=1 if pl.pin_world(index[source[0]],source[1])[1]>exit_point[1] else -1
                    shifted=list(target);shifted[1]+=sign*(options.halo+2*step)
                    targets=[shifted,target]
                else:targets=[target]
            else:targets=[target]
        else:
            target[1]=min(ea[1],eb[1])-2*step
            lane=2*options.halo+options.center_spacing+(envelope[3]-envelope[1])/2
            below=max(ea[1],eb[1])+options.halo+2*step+(envelope[3]-envelope[1])/2
            targets=[target,[target[0],below],[target[0],target[1]-lane],[target[0],target[1]+lane]]
            # Supply markers and other access stubs can shadow a port. Slide
            # the complete chain inside this same inter-branch corridor.
            shift=options.halo+options.center_spacing
            targets=[t for original in targets for t in (original,[original[0]-shift,original[1]],[original[0]+shift,original[1]])]
    else:
        end=0 if net==group['ends'][0] else 1
        outer=vector if end==0 else tuple(-v for v in vector)
        direction=VECTORS[pl.pin_dir(index[a[0]],a[1])]
        pad=options.halo+options.center_spacing+2*step
        contact=list(ea)
        ground_shunt=(any(par.power_marker_sym(n)=='ground' for n in group['ends'])
                      and a[1] in ('D','C') and group['role'] not in ('conduction_inline','terminal_bank'))
        if direction==outer and not ground_shunt:
            contact=[ea[k]+outer[k]*pad for k in range(2)]
        elif axis==0:
            distance=2*options.halo+2*options.center_spacing+(envelope[3]-envelope[1])/2
            contact=[pa[0]+outer[0]*distance,pa[1]]
        elif axis==1:
            # A gate RC forms a T at its actual control node. A drain-to-ground
            # shunt hangs beside the active stack instead of covering it.
            host_x=pl.pos_of(index[a[0]])[0]
            all_x=[pl.pos_of(i)[0] for i,c in enumerate(comps) if c['symbol'] in ACTIVE]
            side=-1 if host_x<=median(all_x) else 1
            box=body_rect(par,pl,index[a[0]])
            edge=box[0] if side<0 else box[2]
            clearance=(envelope[2]-envelope[0])/2+2*options.halo+options.center_spacing
            contact=[edge+side*clearance,pa[1]+outer[1]*pad]
            if direction[0]:contact[0]=ea[0]+direction[0]*pad
        target=[contact[k]-end*vector[k]*extent for k in range(2)]
        targets=[target]
        if group['role'] not in ('terminal_bank','conduction_inline'):
            for delta in (-options.center_spacing*2,options.center_spacing*2):
                shifted=list(target);shifted[transverse]+=delta;targets.append(shifted)
    # Derive additional channels from real obstacle edges, instead of trying
    # only +/-20 around a blocked tail or bridge. Keep the nearest few stable.
    from netlist2schematic.routing.routing_rules import build_scene
    scene=build_scene(par,comps_to_nets(comps),pl,index,options=options)
    extra=[];relevant=[]
    for target in targets[:2]:
        longitudinal=(envelope[axis]+target[axis],envelope[axis+2]+target[axis])
        for owner,rect in scene.body_rects+scene.marker_rects:
            if min(longitudinal[1],rect[axis+2])<max(longitudinal[0],rect[axis])-options.halo:continue
            relevant.append(rect)
            for coordinate in (rect[transverse]-options.center_spacing-envelope[transverse+2],
                               rect[transverse+2]+options.center_spacing-envelope[transverse]):
                shifted=list(target);shifted[transverse]=snap(coordinate,step);extra.append(shifted)
    extra.sort(key=lambda t:(abs(t[0]-targets[0][0])+abs(t[1]-targets[0][1]),tuple(t)))
    seen=set();ordered=[]
    for target in targets:
        key=tuple(snap(v,step) for v in target)
        if key not in seen:seen.add(key);ordered.append(key)
    added=0;channels={t[transverse] for t in ordered}
    for target in extra:
        key=tuple(snap(v,step) for v in target)
        if key in seen or key[transverse] in channels:continue
        seen.add(key);channels.add(key[transverse]);ordered.append(key);added+=1
        if added>=6:break
    # Two deterministic outer channels are a final option, after nearby gaps.
    # Repeated obstacles on the same rows cannot consume these two chances.
    if relevant:
        for coordinate in (min(r[transverse] for r in relevant)-options.center_spacing-envelope[transverse+2]-step,
                           max(r[transverse+2] for r in relevant)+options.center_spacing-envelope[transverse]+step):
            target=list(targets[0]);target[transverse]=coordinate
            key=tuple(snap(v,step) for v in target)
            if key not in seen:seen.add(key);ordered.append(key)
    for target in ordered:
        trial=_translated(pl,geometry,target,step)
        # The selected edge is a concrete real-port channel, not a net-box
        # centroid. Keep both ordinary L options; the common rule set decides.
        def approaches(terminal,port):
            point=trial.pin_world(index[terminal[0]],terminal[1])
            exit_point=_point_exit(par,trial,index,terminal,options)
            choices=[[(point,exit_point)]]
            if exit_point[0]==port[0] or exit_point[1]==port[1]:
                return [choices[0]+[(exit_point,port)]]
            return [choices[0]+[(exit_point,corner),(corner,port)]
                    for corner in ((port[0],exit_point[1]),(exit_point[0],port[1]))]
        import itertools
        ends=group['ends'];ports=trial.passive_module_guides[group['id']]['ports']
        first=0 if b is not None or net==ends[0] else 1
        choices=[[(ends[first],s)] for s in approaches(a,ports[first])]
        if b is not None:
            choices=[old+[(ends[1],s)] for old,s in itertools.product(choices,approaches(b,ports[1]))]
        for extra in choices:
            candidate=deepcopy(trial)
            guide=candidate.passive_module_guides[group['id']]
            for name,segments in extra:
                guide['routes'].setdefault(name,[]).extend((u,v) for u,v in segments if u!=v)
            yield candidate


def comps_to_nets(comps):
    nets=defaultdict(list)
    for c in comps:
        for pin,net in c['nodes'].items():nets[net].append((c['id'],pin))
    return dict(nets)


def _guest_guards(par, trial, comps, index, groups):
    """Only an attachment of that leaf may enter its same-row interior."""
    for block in trial.blocks+getattr(trial,'hierarchy_guards',[]):
        members=set(block);rows=defaultdict(list)
        for i in block:
            if comps[i]['symbol'] in ACTIVE:rows[trial.cell_of[i][0]].append(i)
        for g in groups:
            guests={index[n] for n in g['members']}
            related=any(ts and {index[n] for n,p in ts}<=members for ts in g['anchors'].values())
            if related:continue
            for line in rows.values():
                ordered=sorted(line,key=lambda i:trial.pos_of(i)[0])
                for a,b in zip(ordered,ordered[1:]):
                    aa,bb=body_rect(par,trial,a),body_rect(par,trial,b)
                    strip=aa[2],max(aa[1],bb[1]),bb[0],min(aa[3],bb[3])
                    for i in guests:
                        box=body_rect(par,trial,i)
                        if max(box[0],strip[0])<min(box[2],strip[2])-EPS and max(box[1],strip[1])<min(box[3],strip[3])-EPS:
                            return False
    return True


def pack(par, core, comps, nets, index, plan, options, *, deadline=None, seconds=1., max_trials=128):
    """Atomic family packing with a single finite local budget."""
    from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
    from netlist2schematic.routing.orthogonal_router import RoutingFailure
    if type(seconds) not in (int,float) or not isfinite(seconds) or seconds<0:
        raise ValueError('passive packing seconds must be finite and non-negative')
    if type(max_trials) is not int or max_trials<0:
        raise ValueError('passive packing trial limit must be a non-negative integer')
    if deadline is not None and (type(deadline) not in (int,float) or not isfinite(deadline)):
        raise ValueError('passive packing deadline must be finite')
    plan=deepcopy(plan)
    plan['geometry_parameters']=asdict(options)
    started=perf_counter();stop=min(deadline,started+seconds) if deadline is not None else started+seconds
    report=dict(version=plan['version'],applied=False,trials=0,actions=[],plan=deepcopy(plan),
        seconds_limit=seconds,max_trials=max_trials,active_grid_before=dict(column_x=list(core.column_x),row_y=list(core.row_y)))
    report['core_checkpoint']={c['id']:dict(cell=list(core.cell_of[i]),orient=core.orient[i],
        point=list(core.pos_of(i))) for i,c in enumerate(core.comps)}
    report['fallback_policy']='retain_verified_incumbent_no_mixed_restart'
    frame=make_frame(par,core,comps,index,options)
    fixed={i for i,c in enumerate(comps) if i in frame.fixed or c['id'] in frame.fixed or i in frame.fixed_cells}
    def finish(reason,pl=None):
        report.update(stop_reason=reason,elapsed_seconds=round(perf_counter()-started,6))
        if pl is not None:
            report.update(applied=True,current=measure(pl,comps,index,plan),
                active_grid_after=dict(column_x=list(pl.column_x),row_y=list(pl.row_y)))
        return pl,report
    if seconds<=0 or max_trials<=0 or perf_counter()>=stop:return finish('budget_exhausted')
    groups,expansions=reserve(par,frame,comps,index,plan,options,fixed)
    report['expansions']=expansions
    report['core_positions']={c['id']:dict(point=list(frame.pos_of(i)),cell=list(frame.cell_of[i]),orient=frame.orient[i])
                              for i,c in enumerate(comps) if c['symbol'] not in PASSIVE}
    if groups is None:return finish('no_safe_reservation')
    by_id={g['id']:g for g in groups};batches=[];used=set()
    for family in plan['families']:
        batches.append([by_id[gid] for gid in family['groups']]);used.update(family['groups'])
    singles=sorted((g for g in groups if g['id'] not in used),key=lambda g:(g['role']!='conduction_inline',g['role']=='rail_attachment',-len(g['members'])))
    batches.extend([g] for g in singles)
    placed=[]
    def batch_trials(state,batch,position=0):
        if perf_counter()>=stop:
            return
        if position==len(batch):
            yield state
            return
        for trial in slot_trials(par,state,comps,index,batch[position],options):
            if perf_counter()>=stop:
                return
            yield from batch_trials(trial,batch,position+1)
    try:
        for batch in batches:
            chosen=None
            for candidate in batch_trials(frame,batch):
                if perf_counter()>=stop or report['trials']>=max_trials:return finish('local_budget')
                report['trials']+=1
                if not _guest_guards(par,candidate,comps,index,placed+batch):continue
                faults=_static_faults(par,candidate,nets,index,options,stop)
                if not faults['count'] and not faults['hard']:
                    from netlist2schematic.plugins.passive_skeleton.module_connections import check_guides
                    violations=check_guides(par,candidate,nets,index,options,stop)
                    if not violations:
                        chosen=candidate;break
                    faults=dict(count=len(violations),violations=violations,hard=set())
                report['last_rejection']=dict(count=faults['count'],violations=faults['violations'][:5],
                    groups=[g['id'] for g in batch],proposed_positions={n:dict(point=list(candidate.pos_of(index[n])),orient=candidate.orient[index[n]]) for g in batch for n in g['members']})
                report.setdefault('trial_rejections',[])
                if len(report['trial_rejections'])<8:report['trial_rejections'].append(deepcopy(report['last_rejection']))
            if chosen is None:
                return finish('local_budget' if perf_counter()>=stop else 'no_legal_slot')
            for g in batch:
                report['actions'].append(dict(group=g['id'],role=g['role'],members=g['members'],
                    positions={n:dict(point=list(chosen.pos_of(index[n])),orientation=chosen.orient[index[n]]) for n in g['members']}))
            frame=chosen;placed.extend(batch)
        final=_static_faults(par,frame,nets,index,options,stop)
        if final['count'] or final['hard']:return finish('final_static_conflict')
    except RoutingFailure as exc:
        if exc.diagnostics.get('reason')!='quality_time_budget':raise
        return finish('local_budget')
    for i,c in enumerate(core.comps):
        j=index[c['id']]
        if frame.cell_of[j]!=core.cell_of[i] or frame.orient[j]!=core.orient[i]:
            raise AssertionError('Passive insertion changed the active skeleton identity or orientation')
    if getattr(core, 'column_plan', {}).get('protected_plan') is not None:
        from netlist2schematic.plugins.columns.column_layout import measure_columns
        for item in (core.column_plan, core.column_plan['protected_plan']):
            before = measure_columns(core, {c['id']:i for i,c in enumerate(core.comps)}, item)
            after = measure_columns(frame, index, item)
            if any(after[key] > value+EPS for key,value in before.items()):
                return finish('column_contract_conflict')
        report['column_contract_preserved'] = True
    report['active_core_preserved']=True
    report['module_connections']=deepcopy(frame.passive_module_guides)
    return finish('packed',frame)
