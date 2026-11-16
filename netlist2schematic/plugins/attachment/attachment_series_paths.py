"""Finite physical packing of unbranched signal paths between proven windows.

The existing conduction-tap evidence supplies both real interfaces. The path
uses local coordinates after SA; host devices, axes and fixed exits retain
their original definitions. This module neither searches routes nor reads GT.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from itertools import islice
from math import ceil
from statistics import median
from time import perf_counter

VERSION = 'physical-series-paths-v1'
PASSIVE = frozenset(('resistor','capacitor','inductor'))
EPS = 1e-8


def compile_paths(comps, plan):
    by_id = {c['id']: c for c in comps}
    incidence = defaultdict(list)
    for c in comps:
        for pin, net in c['nodes'].items():
            incidence[net].append((c['id'],pin))
    groups = {g['id']:g for g in plan.get('groups',()) if g.get('kind')=='series_chain'}
    membership = Counter(n for g in groups.values() for n in g['members'])
    managed = set(plan.get('source_managed_members',()))
    junctions = plan['readability_plan']['junctions']
    paths, skipped = [], []
    for relation in plan.get('conduction_taps',{}).get('groups',()):
        group = groups.get(relation['group'])
        if group is None or group.get('row_mode') in ('conduction_bridge','control_connection'):
            continue
        names, nodes = list(group['members']), list(group.get('path',()))
        reason = None
        if len(names)<2 or len(nodes)!=len(names)+1 or len(set(names))!=len(names):
            reason = 'not_a_unique_series_path'
        elif managed.intersection(names) or any(membership[n]>1 for n in names):
            reason = 'shared_or_source_managed_members'
        elif any(junctions.get(net,{}).get('is_rail') for net in nodes):
            reason = 'supply_node_inside_signal_path'
        elif any(by_id[n]['symbol'] not in PASSIVE or len(by_id[n]['nodes'])!=2
                or set(by_id[n]['nodes'].values())!=set(nodes[k:k+2]) for k,n in enumerate(names)):
            reason = 'member_terminal_contract'
        elif any(len(incidence[net])!=2 or {n for n,p in incidence[net]}!=set(names[k-1:k+1])
                for k,net in enumerate(nodes[1:-1],1)):
            reason = 'branched_internal_node'
        elif {nodes[0],nodes[-1]}!={end['net'] for end in relation['ends']}:
            reason = 'different_interface_evidence'
        if reason:
            skipped.append(dict(group=group['id'],reason=reason))
            continue
        paths.append(dict(group=group['id'],members=names,nodes=nodes,relation=deepcopy(relation),
            terminals={n:dict(by_id[n]['nodes']) for n in names},
            symbols={n:by_id[n]['symbol'] for n in names},
            host_symbols={n:by_id[n]['symbol'] for end in relation['ends'] for n,p in end['hosts']},
            evidence='unbranched_RCL_path_between_two_existing_unique_conduction_windows'))
    return dict(version=VERSION,paths=paths,skipped=skipped)


def _contract(comps,index,path):
    try:
        return (all(comps[index[n]]['symbol']==path['symbols'][n]
                    and comps[index[n]]['nodes']==pins for n,pins in path['terminals'].items())
            and all(comps[index[n]]['symbol']==symbol for n,symbol in path['host_symbols'].items())
            and all(comps[index[n]]['nodes'][pin]==end['net']
                for end in path['relation']['ends'] for n,pin in end['hosts']))
    except (KeyError,TypeError,IndexError):
        return False


def _window(pl,comps,index,path):
    from netlist2schematic.plugins.attachment.attachment_taps import _bands
    lo,hi,_ = _bands(pl,comps,index,path['relation'])
    ends = {end['net']:end for end in path['relation']['ends']}
    xs = [median(pl.pin_world(index[n],pin)[0] for n,pin in ends[net]['hosts'])
          for net in (path['nodes'][0],path['nodes'][-1])]
    if abs(xs[1]-xs[0])<=EPS:
        raise ValueError('no_unique_horizontal_interface_span')
    return lo,hi,xs


def measure_paths(pl,comps,index,model):
    records,missing = [],[]
    for path in model.get('paths',()):
        if not _contract(comps,index,path):
            missing.append(dict(group=path['group'],reason='terminal_or_symbol_contract_changed'))
            continue
        try:
            lo,hi,xs = _window(pl,comps,index,path)
        except ValueError as exc:
            missing.append(dict(group=path['group'],reason=str(exc)))
            continue
        sign = 1 if xs[1]>xs[0] else -1
        centers = [pl.pos_of(index[n]) for n in path['members']]
        pin_pairs=[];directions=0
        for k,n in enumerate(path['members']):
            i=index[n]
            names=[next(p for p,net in comps[i]['nodes'].items() if net==value) for value in path['nodes'][k:k+2]]
            pair=tuple(pl.pin_world(i,p) for p in names)
            pin_pairs.append(pair)
            directions += pl.pin_dir(i,names[0])!=('west' if sign>0 else 'east')
            directions += pl.pin_dir(i,names[1])!=('east' if sign>0 else 'west')
        alignment=sum(abs(a[1]-b[1]) for a,b in pin_pairs)
        alignment+=sum(abs(a[1][1]-b[0][1]) for a,b in zip(pin_pairs,pin_pairs[1:]))
        internal=sum(abs(a[1][0]-b[0][0])+abs(a[1][1]-b[0][1]) for a,b in zip(pin_pairs,pin_pairs[1:]))
        outside=sum(y<lo-EPS or y>hi+EPS or x<=min(xs)+EPS or x>=max(xs)-EPS for x,y in centers)
        order=sum(sign*(b[0]-a[0])<=EPS for a,b in zip(centers,centers[1:]))
        records.append(dict(group=path['group'],members=path['members'],access_band=[lo,hi],
            anchor_x=xs,outside_errors=outside,direction_errors=directions,order_errors=order,
            pin_alignment_error=alignment,internal_wirelength=internal,
            actual_positions={n:list(pl.pos_of(index[n])) for n in path['members']}))
    valid=bool(records) and not missing
    return dict(version=VERSION,count=len(model.get('paths',())),measured=len(records),
        measurable=valid,records=records,missing=missing,skipped=deepcopy(model.get('skipped',())),
        **{k:sum(r[k] for r in records) if valid else None for k in (
            'outside_errors','direction_errors','order_errors','pin_alignment_error','internal_wirelength')})


def path_rank(measurement):
    if not measurement.get('count'):
        return (0,0,0)
    if not measurement.get('measurable'):
        return None
    return (measurement['outside_errors']+measurement['direction_errors']+measurement['order_errors'],
            round(measurement['pin_alignment_error']/100,8),round(measurement['internal_wirelength']/100,8))


def reserve_finish(plan,budget):
    count=len(plan.get('physical_series_paths',{}).get('paths',()))
    if count and not budget.get('series_path_finish_done'):
        budget['series_path_reserve']=min(12,4*count,budget['max_candidates']//8)


def path_trials(par,pl,comps,nets,index,path,options,deadline=None,diagnostic=None):
    diagnostic={} if diagnostic is None else diagnostic
    if deadline is not None and perf_counter()>=deadline:
        diagnostic['stop_reason']='deadline';return
    if not _contract(comps,index,path):
        diagnostic['stop_reason']='changed_contract';return
    from netlist2schematic.plugins.attachment.attachment_integration import _fixed_ids
    ids=[index[n] for n in path['members']]
    if _fixed_ids(pl,index).intersection(ids):
        diagnostic['stop_reason']='fixed_guest';return
    if getattr(pl,'power_connections',None) is not None or any(par.power_marker_sym(n) for n in path['nodes']):
        diagnostic['stop_reason']='unsupported_power_geometry';return
    try:
        lo,hi,xs=_window(pl,comps,index,path)
    except ValueError as exc:
        diagnostic['stop_reason']=str(exc);return
    step=options.grid_step;sign=1 if xs[1]>xs[0] else -1
    template=deepcopy(pl);pin_offsets={}
    for k,i in enumerate(ids):
        pins=[next(p for p,net in comps[i]['nodes'].items() if net==n) for n in path['nodes'][k:k+2]]
        choices=[]
        for ori in pl.allowed_orientations(i):
            row=par.GEO_DOC[comps[i]['symbol']]['orientations'][ori]
            g={p['name']:p for p in row['pins']}
            if (g[pins[0]]['direction']==('west' if sign>0 else 'east')
                    and g[pins[1]]['direction']==('east' if sign>0 else 'west')
                    and abs(g[pins[0]]['at']['y']-g[pins[1]]['at']['y'])<=EPS):
                choices.append((ori,g[pins[0]]['at']['y']))
        if not choices:
            diagnostic['stop_reason']='no_legal_straight_orientation';return
        ori,offset=min(choices,key=lambda r:(r[0]!=pl.orient[i],r[0]))
        template.orient[i]=ori;pin_offsets[i]=offset
    from netlist2schematic.routing.routing_rules import build_scene
    from netlist2schematic.plugins.attachment.attachment_physical import owned_footprints
    scene=build_scene(par,nets,template,index,options=options,deadline=deadline)
    diagnostic['scene_templates']=diagnostic.get('scene_templates',0)+1
    boxes=owned_footprints(par,scene,nets,index)
    relative={}
    for i in ids:
        x,y=template.pos_of(i);rect=boxes[i]
        relative[i]=(rect[0]-x,rect[1]-y-pin_offsets[i],rect[2]-x,rect[3]-y-pin_offsets[i])
    mid=(lo+hi)/2;seen=set()
    # Geometric left-to-right packing is independent of the lexical order of
    # endpoint net names (which may reverse the topological path record).
    ordered=ids if sign>0 else list(reversed(ids))
    xmin,xmax=sorted(xs)
    for gap in sorted({0.,step,options.center_spacing}):
        projected=[]
        for number,i in enumerate(ordered):
            if not number:
                projected.append(0.);continue
            j=ordered[number-1]
            prev_hi=relative[j][2]
            next_lo=relative[i][0]
            projected.append(projected[-1]+prev_hi-next_lo+gap)
        if projected[-1]>abs(xs[1]-xs[0])-2*step+EPS:
            continue
        base=(xmin+xmax-projected[-1])/2
        positions=[]
        for number,i in enumerate(ordered):
            wanted=base if not number else positions[-1]+projected[number]-projected[number-1]
            positions.append(xmin+ceil((wanted-xmin)/step)*step)
        if not all(xmin+EPS<v<xmax-EPS for v in positions):
            continue
        by_id=dict(zip(ordered,positions))
        x_values=[by_id[i] for i in ids]
        xlo=min(x+relative[i][0] for i,x in zip(ids,x_values))
        xhi=max(x+relative[i][2] for i,x in zip(ids,x_values))
        lower=min(relative[i][1] for i in ids);upper=max(relative[i][3] for i in ids)
        values=[mid,*pl.row_y,median(pl.pin_world(i,next(p for p,n in comps[i]['nodes'].items()
            if n==path['nodes'][k]))[1] for k,i in enumerate(ids))]
        for owner,rect in scene.body_rects:
            if owner not in ids and max(xlo,rect[0])<min(xhi,rect[2])-EPS:
                values.extend((rect[1]-upper-step,rect[3]-lower+step))
        for _,rect in scene.marker_rects:
            if max(xlo,rect[0])<min(xhi,rect[2])-EPS:
                values.extend((rect[1]-upper-step,rect[3]-lower+step))
        ys={lo+round((v-lo)/step)*step for v in values if lo+EPS<v<hi-EPS}
        ys=[v for v in ys if lo+EPS<v<hi-EPS]
        for line_y in sorted(ys,key=lambda v:(abs(v-mid),v))[:4]:
            if deadline is not None and perf_counter()>=deadline:
                diagnostic['stop_reason']='deadline';return
            targets=[(x,line_y-pin_offsets[i]) for i,x in zip(ids,x_values)]
            key=tuple(targets),tuple(template.orient[i] for i in ids)
            if key in seen or all(abs(pl.pos_of(i)[0]-p[0])<=EPS and abs(pl.pos_of(i)[1]-p[1])<=EPS
                    and pl.orient[i]==template.orient[i] for i,p in zip(ids,targets)):
                continue
            seen.add(key);trial=deepcopy(template)
            offsets=list(getattr(pl,'passive_local_offsets',[(0,0)]*len(comps)))
            for i,(x,target_y) in zip(ids,targets):
                ox,oy=offsets[i];old=pl.pos_of(i)
                offsets[i]=x-old[0]+ox,target_y-old[1]+oy
            trial.passive_local_offsets=offsets
            diagnostic['generated']=diagnostic.get('generated',0)+1
            yield trial,dict(kind='physical_series_path',version=VERSION,group=path['group'],
                members=path['members'],access_band=[lo,hi],line_y=line_y,
                fixed_escape_gap=gap,positions={comps[i]['id']:list(p) for i,p in zip(ids,targets)},
                logical_cells_and_axes_preserved=True)
    diagnostic.setdefault('stop_reason','enumerated_or_no_axial_room')


def finish_paths(par,current,comps,nets,index,plan,options,budget,until,report,*,hpwl_limit):
    from netlist2schematic.plugins.attachment import attachment_integration as engine
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    from netlist2schematic.routing.routing_rules import RoutingOptions
    from netlist2schematic.routing.orthogonal_router import RoutingFailure
    model=plan.get('physical_series_paths',{})
    reserve=budget.pop('series_path_reserve',0)
    if not model.get('paths'):
        return current
    budget['series_path_finish_done']=True;options=options or RoutingOptions()
    detail=dict(version=VERSION,reserved_trials=reserve,trials=0,accepted=0,actions=[],generation=[],
        before=measure_paths(current,comps,index,model),shared_budget_reset=False)
    report['physical_series_finish']=detail
    for number,path in enumerate(model['paths']):
        allowance=min(12,max(0,budget['max_candidates']-budget['candidates'])//max(1,len(model['paths'])-number))
        diagnostic=dict(group=path['group'],allowance=allowance,trials=0,generated=0,scene_templates=0,
            state_rejected=0,old_organization_rejected=0,path_objective_rejected=0,hpwl_rejected=0,static_rejected=0)
        detail['generation'].append(diagnostic)
        if not allowance or not engine._has_budget(budget,until):
            detail['stop_reason']='shared_attachment_budget';break
        old=engine.attachment_rank(engine.measure_attachments(current,comps,index,plan))
        before=path_rank(measure_paths(current,comps,index,dict(model,paths=[path])))
        before_hpwl=_hpwl(par,current,nets,index)
        try:
            for trial,action in islice(path_trials(par,current,comps,nets,index,path,options,until,diagnostic),allowance):
                if not engine._has_budget(budget,until):
                    detail['stop_reason']='shared_attachment_budget';break
                budget['candidates']+=1;report['candidates']+=1;detail['trials']+=1;diagnostic['trials']+=1
                if not trial.valid_state() or not engine._fixed_world_safe(current,trial,index):
                    diagnostic['state_rejected']+=1;continue
                rank=engine.attachment_rank(engine.measure_attachments(trial,comps,index,plan))
                # Compact a two-ended path without worsening existing hard/
                # shape/source terms. Nearest-host distance alone can rise as
                # the chain becomes centered; keep total estimated wire flat.
                if old is None or rank is None or any(n>o+EPS
                        for n,o in zip(rank[:4]+rank[5:],old[:4]+old[5:])):
                    diagnostic['old_organization_rejected']+=1;continue
                after=path_rank(measure_paths(trial,comps,index,dict(model,paths=[path])))
                if before is None or after is None or after>=before:
                    diagnostic['path_objective_rejected']+=1;continue
                after_hpwl=_hpwl(par,trial,nets,index)
                if after_hpwl>min(hpwl_limit,before_hpwl)+EPS:
                    diagnostic['hpwl_rejected']+=1;continue
                safe=engine._checked_static(par,trial,nets,index,options,until)
                if safe is None:
                    budget['exhausted']=True;detail['stop_reason']='shared_attachment_budget';break
                if not safe:
                    diagnostic['static_rejected']+=1;continue
                action.update(owner_distance_before=old[4],owner_distance_after=rank[4],
                    estimated_wire_before=before_hpwl,estimated_wire_after=after_hpwl,
                    existing_non_distance_terms_preserved=True)
                current=trial;detail['accepted']+=1;report['accepted']+=1
                detail['actions'].append(action);report['actions'].append(action);break
        except RoutingFailure as exc:
            if exc.diagnostics.get('reason')!='quality_time_budget':raise
            budget['exhausted']=True;detail['stop_reason']='shared_attachment_budget';break
    detail['after']=measure_paths(current,comps,index,model)
    detail.setdefault('stop_reason','considered_paths')
    return current
