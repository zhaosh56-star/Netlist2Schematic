"""Topology-driven, physical passive groups after the normal active finalizers.

No GT, circuit names, SA or routing searches are used. Logical cells remain
anchors; actual symbol geometry, fixed exits and rail markers decide legality.
"""
from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
from math import ceil, isfinite
from statistics import median
from time import perf_counter

from netlist2schematic.common.passive_coordinates import PASSIVE, validate_passive_offsets

VERSION = 'passive-embedding-v1'
ACTIVE = frozenset(('nmos', 'pmos', 'npn', 'pnp'))
VECTORS = {'north': (0,-1), 'south': (0,1), 'east': (1,0), 'west': (-1,0)}
EPS = 1e-8


def build_plan(par, comps, nets):
    parts = {c['id']: c for c in comps}
    passive = {n for n,c in parts.items() if c['symbol'] in PASSIVE
               and len(c['nodes']) == len(set(c['nodes'].values())) == 2}
    endpoint = {n: tuple(parts[n]['nodes'].values()) for n in passive}
    def internal(net):
        return not par.power_marker_sym(net) and len(nets[net]) == 2 and all(n in passive for n,p in nets[net])
    unseen = set(passive); paths=[]; skipped=[]
    while unseen:
        seed = min(unseen); connected={seed}; todo=[seed]
        while todo:
            here=todo.pop()
            for net in endpoint[here]:
                if internal(net):
                    for n,p in nets[net]:
                        if n not in connected:connected.add(n);todo.append(n)
        unseen -= connected
        ends=[(n,net) for n in connected for net in endpoint[n] if not internal(net)]
        if len(ends)!=2:
            skipped.append(dict(members=sorted(connected),reason='closed_or_ambiguous_passive_path'));continue
        # Direction is an interchangeable path representation, never evidence
        # about amplification. Both physical directions are later considered.
        first,net=min(ends); ordered=[]; nodes=[net]; here=first
        while True:
            ordered.append(here)
            net=next(v for v in endpoint[here] if v!=net);nodes.append(net)
            if not internal(net):break
            here=next(n for n,p in nets[net] if n!=here)
        if len(ordered)!=len(connected):
            skipped.append(dict(members=sorted(connected),reason='incomplete_path'));continue
        paths.append(dict(members=ordered,nodes=nodes))
    banks=defaultdict(list)
    for path in paths:
        banks[tuple(sorted((path['nodes'][0],path['nodes'][-1])))].append(path)
    groups=[]
    for ends,bank in sorted(banks.items()):
        normalized=[]
        for path in bank:
            if path['nodes'][0]!=ends[0]:
                path=dict(members=list(reversed(path['members'])),nodes=list(reversed(path['nodes'])))
            normalized.append(path)
        members=sorted(n for p in normalized for n in p['members'])
        anchors={net:[list(t) for t in nets[net] if t[0] not in members]
                 if not par.power_marker_sym(net) else [] for net in ends}
        # Prefer visible nonpassive ports of the real node to a movable peer.
        for net in ends:
            fixed=[t for t in anchors[net] if t[0] not in passive]
            if fixed:anchors[net]=fixed
        kind=('parallel' if len(bank)>1 and all(len(p['members'])==1 for p in bank)
              else 'parallel_paths' if len(bank)>1 else 'series' if len(members)>1 else 'single')
        groups.append(dict(id=sha256(repr((ends,members)).encode()).hexdigest()[:16],kind=kind,
            members=members,paths=normalized,ends=list(ends),anchors=anchors,
            evidence='exact_endpoint_paths_with_unbranched_interiors'))
    return dict(version=VERSION,groups=groups,skipped=skipped,
        passive_count=len(passive),covered_count=sum(len(g['members']) for g in groups),
        terminals={n:dict(symbol=c['symbol'],nodes=deepcopy(c['nodes'])) for n,c in parts.items()})


def pitch(pl):
    gaps=[b-a for axis in (getattr(pl,'column_x',()),getattr(pl,'row_y',()))
          for a,b in zip(axis,axis[1:]) if b>a]
    return median(gaps) if gaps else 150.


def _pin(comps,index,name,net):
    return next(p for p,n in comps[index[name]]['nodes'].items() if n==net)


def _hosts(pl,index,group,net):
    return [pl.pin_world(index[n],p) for n,p in group['anchors'][net]]


def _box_distance(point, points):
    x,y=point
    return (max(min(p[0] for p in points)-x,0,x-max(p[0] for p in points))+
            max(min(p[1] for p in points)-y,0,y-max(p[1] for p in points)))


def measure(pl, comps, index, plan):
    scale=pitch(pl);records=[]
    for group in plan['groups']:
        distances=backwards=0.; possibilities=[]
        for net in group['ends']:
            hosts=_hosts(pl,index,group,net)
            if not hosts:continue
            for path in group['paths']:
                name=path['members'][0 if net==path['nodes'][0] else -1]
                pin=_pin(comps,index,name,net);i=index[name];point=pl.pin_world(i,pin)
                distances+=_box_distance(point,hosts)
                dx,dy=VECTORS[pl.pin_dir(i,pin)]
                backwards+=all((h[0]-point[0])*dx+(h[1]-point[1])*dy < -EPS for h in hosts)
        for axis in (0,1):
            transverse=1-axis;shape=wrong=0.
            for path in group['paths']:
                pairs=[]
                for k,name in enumerate(path['members']):
                    a,b=(_pin(comps,index,name,n) for n in path['nodes'][k:k+2])
                    i=index[name];p,q=pl.pin_world(i,a),pl.pin_world(i,b)
                    pairs.append((p,q))
                    wrong+=abs(p[axis]-q[axis])<EPS
                ys=[p[transverse] for pair in pairs for p in pair]
                shape+=(max(ys)-min(ys))/scale
                signs=[q[axis]-p[axis] for p,q in pairs]
                wrong+=min(sum(v>EPS for v in signs),sum(v<-EPS for v in signs))
                for (a,b),(c,d) in zip(pairs,pairs[1:]):
                    if (b[axis]-a[axis])*(d[axis]-c[axis])>0 and (c[axis]-b[axis])*(b[axis]-a[axis])<0:
                        wrong+=1
            if group['kind']=='parallel':
                for net in group['ends']:
                    ps=[pl.pin_world(index[p['members'][0]],_pin(comps,index,p['members'][0],net)) for p in group['paths']]
                    shape+=(max(p[axis] for p in ps)-min(p[axis] for p in ps))/scale
            possibilities.append((wrong,shape))
        wrong,shape=min(possibilities)
        points=[pl.pos_of(index[n]) for n in group['members']]
        span=(max(p[0] for p in points)-min(p[0] for p in points)+
              max(p[1] for p in points)-min(p[1] for p in points))/scale
        records.append(dict(id=group['id'],kind=group['kind'],members=group['members'],
            direction_errors=int(wrong+backwards),shape_error=shape,access_distance=distances/scale,
            span=span,anchored=any(group['anchors'].values())))
    return dict(group_count=len(records),covered_count=plan['covered_count'],passive_count=plan['passive_count'],
        direction_errors=sum(r['direction_errors'] for r in records),
        shape_error=sum(r['shape_error'] for r in records),access_distance=sum(r['access_distance'] for r in records),
        span=sum(r['span'] for r in records),groups=records)


def rank(metrics):
    return (metrics['direction_errors'],round(metrics['shape_error'],6),
            round(metrics['access_distance'],6),round(metrics['span'],6))


def _template(par, pl, comps, index, group, direction, options):
    from netlist2schematic.routing.routing_rules import _escape, _inflate
    axis=0 if direction[0] else 1;cross=1-axis;sign=direction[axis]
    gap=ceil((2*options.halo+options.center_spacing)/options.grid_step)*options.grid_step
    widths=[];orients={};locals={};exits={};path_gaps=[];height=0.
    for path in group['paths']:
        length=0.
        for k,name in enumerate(path['members']):
            i=index[name];a,b=(_pin(comps,index,name,n) for n in path['nodes'][k:k+2])
            choices=[]
            for o in range(pl.n_orients(i)):
                geo=par.GEO_DOC[comps[i]['symbol']]['orientations'][o]
                pa,pb=par.pin_pos(comps[i]['symbol'],o,a),par.pin_pos(comps[i]['symbol'],o,b)
                directions={pin['name']:pin['direction'] for pin in geo['pins']}
                va,vb=directions[a],directions[b]
                if VECTORS[va]==(-direction[0],-direction[1]) and VECTORS[vb]==direction:
                    choices.append((o!=pl.orient[i],o,pa,pb,geo['viewBoxAABB']))
            if not choices:return None
            _,o,pa,pb,box=min(choices)
            orients[i]=o;locals[name]=(pa,pb)
            rect=_inflate((box['x'],box['y'],box['x']+box['width'],box['y']+box['height']),options.halo)
            incoming=_escape(pa,(-direction[0],-direction[1]),rect,options.grid_step)
            outgoing=_escape(pb,direction,rect,options.grid_step)
            exits[name]=(abs(incoming[axis]-pa[axis]),abs(outgoing[axis]-pb[axis]))
            length+=abs(pb[axis]-pa[axis]);height=max(height,box['height' if axis==0 else 'width'])
        gaps=[max(gap,exits[a][1]+exits[b][0]+2*options.grid_step)
              for a,b in zip(path['members'],path['members'][1:])]
        path_gaps.append(gaps);widths.append(length+sum(gaps))
    space=ceil((height+2*options.halo+options.center_spacing)/options.grid_step)*options.grid_step
    extent=max(widths); positions={}
    for j,(path,width,gaps) in enumerate(zip(group['paths'],widths,path_gaps)):
        cursor=(extent-width)/2;lane=(j-(len(widths)-1)/2)*space
        for k,name in enumerate(path['members']):
            pa,pb=locals[name];target=[0.,0.];target[axis]=sign*cursor;target[cross]=lane
            positions[index[name]]=(target[0]-pa[0],target[1]-pa[1])
            cursor+=abs(pb[axis]-pa[axis])+(gaps[k] if k<len(gaps) else 0)
    return positions,orients,axis,extent,space


def proposals(par, pl, comps, index, group, options):
    from netlist2schematic.routing.routing_rules import _escape,_inflate
    centers=[]
    for net in group['ends']:
        ps=_hosts(pl,index,group,net)
        if ps:centers.append((median(p[0] for p in ps),median(p[1] for p in ps)))
    current=[pl.pos_of(index[n]) for n in group['members']]
    targets=[];seen=set()
    for direction in ((1,0),(-1,0),(0,1),(0,-1)):
        packed=_template(par,pl,comps,index,group,direction,options)
        if packed is None:continue
        positions,orientations,axis,extent,space=packed;cross=1-axis
        connected=[n for n in group['ends'] if group['anchors'][n]]
        if len(connected)==1:
            net=connected[0];boundary=0 if net==group['ends'][0] else 1
            guests=[]
            for path in group['paths']:
                name=path['members'][0 if boundary==0 else -1]
                i=index[name];pin=_pin(comps,index,name,net);o=orientations[i]
                box=par.GEO_DOC[comps[i]['symbol']]['orientations'][o]['viewBoxAABB']
                local=par.pin_pos(comps[i]['symbol'],o,pin)
                rect=_inflate((box['x'],box['y'],box['x']+box['width'],box['y']+box['height']),options.halo)
                normal=tuple(-v for v in direction) if boundary==0 else direction
                exit=_escape(local,normal,rect,options.grid_step)
                guests.append(sum(abs(a-b) for a,b in zip(local,exit)))
            # A one-sided load belongs outside its actual port, rather than
            # being centered on and obstructing that same port. This also
            # handles banks containing several parallel R/L/C members.
            for name,pin in sorted(group['anchors'][net],key=lambda t:pl.pin_world(index[t[0]],t[1]))[:3]:
                i=index[name];point=pl.pin_world(i,pin);normal=VECTORS[pl.pin_dir(i,pin)]
                box=par.GEO_DOC[comps[i]['symbol']]['orientations'][pl.orient[i]]['viewBoxAABB']
                x,y=pl.pos_of(i);rect=_inflate((x+box['x'],y+box['y'],x+box['x']+box['width'],y+box['y']+box['height']),options.halo)
                exit=_escape(point,normal,rect,options.grid_step)
                clearance=max(guests)+2*options.grid_step
                target=[exit[k]+normal[k]*clearance-direction[k]*extent*boundary for k in (0,1)]
                targets.append((-1.,positions,orientations,target))
        center=[sum(p[k] for p in centers)/len(centers) if centers else median(p[k] for p in current) for k in (0,1)]
        # Inter-port band first; each endpoint's level is also a meaningful
        # channel. No coordinates are taken from a GT or instance identifier.
        lanes=[center[cross],*[p[cross] for p in centers]]
        for lane in list(lanes):lanes.extend((lane-space,lane+space))
        for lane in lanes:
            target=list(center);target[cross]=lane;target[axis]-=direction[axis]*extent/2
            targets.append((sum(abs(target[k]+direction[k]*extent/2-center[k]) for k in (0,1)),
                            positions,orientations,target))
    for _,positions,orientations,target in sorted(targets,key=lambda x:x[0]):
        values=[list(v) for v in getattr(pl,'passive_local_offsets',[(0.,0.)]*len(comps))]
        trial=deepcopy(pl)
        for i,point in positions.items():
            wanted=[round((point[k]+target[k])/options.grid_step)*options.grid_step for k in (0,1)]
            old=pl.pos_of(i)
            values[i]=[values[i][k]+wanted[k]-old[k] for k in (0,1)]
            trial.orient[i]=orientations[i]
        key=(tuple(sorted(orientations.items())),tuple(tuple(v) for v in values))
        if key in seen:continue
        seen.add(key);trial.passive_local_offsets=values
        trial.terminal_orientation_ids=set(getattr(trial,'terminal_orientation_ids',()))|set(positions)
        yield trial


def _state_safe(par, trial, original, comps, index, plan, group, metadata):
    from netlist2schematic.plugins.preflight.placement_preflight import _fixed
    from netlist2schematic.plugins.attachment.attachment_physical import body_rect
    moving={index[n] for n in group['members']};fixed=_fixed(original,comps)
    if moving&fixed:return False
    if trial.cell_of!=original.cell_of or trial.row_y!=original.row_y or trial.column_x!=original.column_x:return False
    if any((trial.pos_of(i),trial.orient[i])!=(original.pos_of(i),original.orient[i]) for i in range(len(comps)) if i not in moving):return False
    validate_passive_offsets(trial,comps)
    if hasattr(trial,'valid_state') and not trial.valid_state():return False
    if hasattr(trial,'blocks_contiguous') and not trial.blocks_contiguous():return False
    # Physical guests cannot bypass the old row-index child protection.
    leaves=[set(b) for b in getattr(original,'blocks',())]+[set(b) for b in getattr(original,'hierarchy_guards',())]
    leaves.extend({index[n] for n in g['members']} for g in metadata.get('layout_hierarchy',{}).get('groups',()))
    for leaf in leaves:
        rows=defaultdict(list)
        for i in leaf:
            if comps[i]['symbol'] in ACTIVE:rows[original.cell_of[i][0]].append(i)
        related=any(set(n for n,p in ts) and set(index[n] for n,p in ts)<=leaf for ts in group['anchors'].values())
        if related:continue
        for members in rows.values():
            ordered=sorted(members,key=lambda i:trial.pos_of(i)[0])
            for a,b in zip(ordered,ordered[1:]):
                first,second=body_rect(par,trial,a),body_rect(par,trial,b)
                strip=(first[2],max(first[1],second[1]),second[0],min(first[3],second[3]))
                for i in moving:
                    box=body_rect(par,trial,i)
                    if max(box[0],strip[0])<min(box[2],strip[2])-EPS and max(box[1],strip[1])<min(box[3],strip[3])-EPS:return False
    return True


def refine(par, pl, comps, nets, index, metadata, plan, options=None, *, seconds=1., deadline=None, max_trials=128):
    from netlist2schematic.routing.routing_rules import RoutingOptions
    from netlist2schematic.routing.orthogonal_router import RoutingFailure
    from netlist2schematic.plugins.preflight.placement_preflight import _static_faults,_wire_estimate
    if type(seconds) not in (int,float) or not isfinite(seconds) or seconds<0:raise ValueError('invalid passive embedding seconds')
    if type(max_trials) is not int or max_trials<0:raise ValueError('invalid passive embedding trials')
    started=perf_counter();stop=min(deadline,started+seconds) if deadline is not None else started+seconds
    options=options or RoutingOptions();current=pl
    report=dict(version=VERSION,plan=deepcopy(plan),before=measure(pl,comps,index,plan),trials=0,actions=[],applied=False,
                max_trials=max_trials,seconds_limit=seconds,rejections=defaultdict(int))
    reason='round_limit';base_wire=_wire_estimate(pl,nets,index,par)
    def finish(why):
        result=deepcopy(metadata)
        report.update(current=measure(current,comps,index,plan),stop_reason=why,elapsed_seconds=round(perf_counter()-started,6),
                      estimated_wire_before=base_wire,estimated_wire_after=_wire_estimate(current,nets,index,par))
        if report['applied']:
            report['source_energy']=metadata.get('energy')
            report['source_optimization_metrics']=deepcopy(metadata.get('optimization_metrics'))
            if all(hasattr(current,k) for k in ('row_of','blocks','role_orders')):
                from netlist2schematic.layout.structured_placement import _estimate
                from netlist2schematic.layout.series_alignment import _objective_context
                alignment,pairs=_objective_context(metadata,index)
                result['energy'],result['optimization_metrics']=_estimate(current,nets,index,alignment,pairs)
                result['optimization_metrics']['measurement_stage']='after_passive_embedding'
        report['rejections']=dict(report['rejections']);result['passive_embedding']=report
        return current,result
    contract={c['id']:dict(symbol=c['symbol'],nodes=c['nodes']) for c in comps}
    if plan['terminals']!=contract:raise ValueError('Passive embedding terminal contract changed')
    if not plan['groups']:return finish('no_passive_groups')
    if seconds==0 or max_trials==0 or perf_counter()>=stop:return finish('budget_exhausted')
    def safe(candidate):
        f=_static_faults(par,candidate,nets,index,options,stop)
        return not f['count'] and not f['hard']
    try:
        if not safe(pl):return finish('unsafe_initial_geometry')
        for _ in range(2):
            accepted=False
            measurement=measure(current,comps,index,plan);details={r['id']:r for r in measurement['groups']}
            groups=sorted(plan['groups'],key=lambda g:(g['kind']=='single',-len(g['members']),
                -details[g['id']]['direction_errors'],-details[g['id']]['access_distance'],
                tuple(sorted(current.pos_of(index[n]) for n in g['members']))))
            for group in groups:
                old=measure(current,comps,index,plan);incumbent=None
                for local_number,trial in enumerate(proposals(par,current,comps,index,group,options)):
                    if local_number>=24:break
                    if perf_counter()>=stop or report['trials']>=max_trials:
                        reason='time_budget' if perf_counter()>=stop else 'trial_limit';break
                    report['trials']+=1
                    if not _state_safe(par,trial,current,comps,index,plan,group,metadata):
                        report['rejections']['state_or_child_protection']+=1;continue
                    metrics=measure(trial,comps,index,plan)
                    if rank(metrics)>=rank(old) or metrics['direction_errors']>old['direction_errors'] or metrics['shape_error']>old['shape_error']+EPS:
                        report['rejections']['organization']+=1;continue
                    wire=_wire_estimate(trial,nets,index,par)
                    if wire>base_wire*1.10+EPS:
                        report['rejections']['estimated_wire']+=1;continue
                    key=rank(metrics)+(wire,)
                    if incumbent is not None and key>=incumbent[0]:continue
                    try:legal=safe(trial)
                    except RoutingFailure as exc:
                        if exc.diagnostics.get('reason')!='quality_time_budget':raise
                        reason='time_budget';break
                    if not legal:report['rejections']['static_geometry']+=1;continue
                    incumbent=key,trial,metrics
                if incumbent:
                    _,trial,metrics=incumbent
                    report['actions'].append(dict(group=group['id'],kind=group['kind'],members=group['members'],
                        before=old,after=metrics,positions={n:dict(before=list(current.pos_of(index[n])),
                           after=list(trial.pos_of(index[n])),orient_before=current.orient[index[n]],
                           orient_after=trial.orient[index[n]]) for n in group['members']}))
                    current=trial;accepted=True;report['applied']=True
                if reason in ('time_budget','trial_limit'):break
            if reason in ('time_budget','trial_limit'):break
            if not accepted:reason='no_legal_improvement';break
    except RoutingFailure as exc:
        if exc.diagnostics.get('reason')!='quality_time_budget':raise
        reason='time_budget'
    return finish(reason)
