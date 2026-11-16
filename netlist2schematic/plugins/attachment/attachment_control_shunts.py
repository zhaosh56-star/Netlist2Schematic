"""Pack two proven opposite-rail shunts beside their unique control port.

No functional label is inferred. The host retains its display row and old
ownership, and guests use the existing saved passive-coordinate mechanism.
"""
from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
from math import ceil,isfinite
from time import perf_counter
from netlist2schematic.netlist.netlist_core import SRC_ALIASES,SNK_ALIASES

VERSION='control-rail-shunts-v1'
ACTIVE={'nmos':('D','G','S'),'pmos':('D','G','S'),'npn':('C','B','E'),'pnp':('C','B','E')}
PASSIVE=frozenset(('resistor','capacitor','inductor'))
EPS=1e-8


def compile_shunts(comps,plan,owners):
    result=dict(version=VERSION,groups=[],skipped=[],terminal_contract={c['id']:
        dict(symbol=c['symbol'],nodes=deepcopy(c['nodes'])) for c in comps})
    if plan.get('policy')!='all':return result
    junctions=plan['readability_plan']['junctions'];parts={c['id']:c for c in comps}
    visible=defaultdict(list)
    for c in comps:
        for pin,net in c['nodes'].items():visible[net].append((c['id'],pin))
    grouped={n for owner in owners if len(owner)>1 for n in owner}
    axes={n for axis in plan['host_axes'] for n in (axis['a'],axis['b'])}
    managed=set(plan.get('source_managed_members',()))
    for node,j in junctions.items():
        controls=j.get('control_ports',())
        if j['is_rail'] or len(controls)!=1 or j.get('conduction_ports') or j.get('source_ports'):continue
        host,pin=controls[0]
        if parts[host]['symbol'] not in ACTIVE:continue
        others=[(n,p) for n,p in visible[node] if (n,p)!=(host,pin)]
        if len(others)!=2 or len({n for n,p in others})!=2:continue
        if any(parts[n]['symbol'] not in PASSIVE or len(parts[n]['nodes'])!=2 for n,p in others):continue
        members=[n for n,p in others];reason=None;rails={}
        for n,p in others:
            nets=set(parts[n]['nodes'].values())-{node}
            if len(nets)!=1:reason='non_rail_or_shortened_member';break
            rail=next(iter(nets));role=('north' if str(rail).casefold() in SRC_ALIASES else
                                     'south' if str(rail).casefold() in SNK_ALIASES else None)
            if not junctions.get(rail,{}).get('is_rail') or role is None:
                reason='unknown_rail_role';break
            if role in rails:reason='same_rail_side';break
            rails[role]=dict(device=n,signal_pin=p,rail_net=rail,
                rail_pin=next(k for k,v in parts[n]['nodes'].items() if v==rail))
        if reason:
            result['skipped'].append(dict(node=node,reason=reason));continue
        if host in grouped or host in axes or grouped.intersection(members) or managed.intersection(members):
            result['skipped'].append(dict(node=node,reason='existing_owner_or_conduction_axis'));continue
        d,_,s=ACTIVE[parts[host]['symbol']]
        host_rails=[p for p in (d,s) if junctions[parts[host]['nodes'][p]]['is_rail']]
        connections=[p for p in (d,s) if p not in host_rails and any(n!=host for n,_ in
            junctions[parts[host]['nodes'][p]].get('conduction_ports',()))]
        if len(host_rails)!=1 or len(connections)!=1:
            result['skipped'].append(dict(node=node,reason='ambiguous_host_conduction_anchor'));continue
        link=parts[host]['nodes'][connections[0]]
        result['groups'].append(dict(id='control_shunt:'+sha256(node.encode()).hexdigest()[:16],
            node=node,host=[host,pin],members=sorted(members),rail_sides=rails,
            host_connection=[host,connections[0]],connection_net=link,
            connection_ports=[list(p) for p in junctions[link]['conduction_ports'] if p[0]!=host],
            evidence='one_actual_control_port_two_opposite_verified_rail_shunts_and_one_host_conduction_connection',
            ownership_claim=False,function_claim=None))
    return result


def measure_shunts(pl,comps,index,model):
    result=dict(version=VERSION,count=len(model.get('groups',())),measurable=False,
        direction_errors=None,alignment_error=None,owner_distance=None,records=[],missing=[],
        skipped=deepcopy(model.get('skipped',())))
    if not result['count']:return result
    try:
        if len(comps)!=len(model['terminal_contract']) or any(comps[index[n]]['symbol']!=c['symbol']
                or comps[index[n]]['nodes']!=c['nodes'] for n,c in model['terminal_contract'].items()):
            raise ValueError('control_shunt_terminal_contract_changed')
        for g in model['groups']:
            host,pin=g['host'];i=index[host];at=pl.pin_world(i,pin);direction=pl.pin_dir(i,pin)
            points=[];errors=direction not in ('east','west');distance=0.;values=[]
            for side,r in sorted(g['rail_sides'].items()):
                j=index[r['device']];p=pl.pin_world(j,r['signal_pin']);points.append(p)
                errors+=pl.pin_dir(j,r['rail_pin'])!=side
                errors+=pl.pin_dir(j,r['signal_pin'])!={'north':'south','south':'north'}[side]
                errors+=(p[0]-at[0])*(1 if direction=='east' else -1)<=EPS
                distance+=abs(p[0]-at[0])+abs(p[1]-at[1])
                values.append(dict(device=r['device'],signal_point=list(p),position=list(pl.pos_of(j))))
            if not all(isfinite(v) for p in [at,*points] for v in p):raise ValueError('nonfinite_shunt_coordinate')
            result['records'].append(dict(id=g['id'],members=g['members'],host=g['host'],
                direction_errors=int(errors),alignment_error=abs(points[0][0]-points[1][0])/100,
                owner_distance=distance/200,positions=values))
        result.update(measurable=True,**{k:sum(r[k] for r in result['records'])
            for k in ('direction_errors','alignment_error','owner_distance')})
    except (KeyError,IndexError,TypeError,ValueError,AttributeError) as exc:
        result['missing'].append(dict(reason=str(exc)))
    return result


def shunt_rank(measured):
    if not measured.get('count'):return ()
    fields=('direction_errors','alignment_error','owner_distance')
    if not measured.get('measurable') or any(type(measured.get(k)) not in (int,float)
            or not isfinite(measured[k]) or measured[k]<0 for k in fields):return None
    return tuple(round(measured[k],8) for k in fields)


def shunt_trials(par,pl,comps,nets,index,model,group,options,until,*,max_trials=12,diagnostic=None):
    from netlist2schematic.plugins.attachment.attachment_integration import _fixed_ids
    from netlist2schematic.routing.routing_rules import build_scene
    from netlist2schematic.plugins.attachment.attachment_physical import owned_footprints
    from netlist2schematic.plugins.attachment.attachment_regulation import _wire_forecast
    diagnostic={} if diagnostic is None else diagnostic
    diagnostic.update(scene_templates=0,generated=0,stop_reason='no_feasible_packing')
    if shunt_rank(measure_shunts(pl,comps,index,model)) is None:return
    host,pin=group['host'];h=index[host];ids={index[n] for n in group['members']}|{h}
    if ids&_fixed_ids(pl,index):diagnostic['stop_reason']='fixed_group_member';return
    if getattr(pl,'power_connections',None) is not None:
        diagnostic['stop_reason']='custom_power_geometry';return
    row=pl.cell_of[h][0];cols=[c for c in range(len(pl.column_x)) if pl.comp_at.get((row,c)) in (None,h)]
    if not cols:diagnostic['stop_reason']='no_host_slot';return
    forecast=_wire_forecast(pl,comps,{n for n,j in pl.attachment_plan['readability_plan']['junctions'].items() if j['is_rail']})
    cols=sorted(cols,key=lambda c:(forecast({h:c}),abs(c-pl.cell_of[h][1]),c))[:6]
    options_by_key=[];step=options.grid_step;gap=max(options.center_spacing,step)
    for orientation in pl.allowed_orientations(h):
        if perf_counter()>=until:diagnostic['stop_reason']='deadline';return
        template=deepcopy(pl);template.orient[h]=orientation
        direction=template.pin_dir(h,pin)
        if direction not in ('east','west'):continue
        for side,r in group['rail_sides'].items():
            i=index[r['device']];old=template.orient[i];choices=[]
            for o in template.allowed_orientations(i):
                template.orient[i]=o
                if template.pin_dir(i,r['rail_pin'])==side:choices.append(o)
            if not choices:diagnostic['stop_reason']='no_legal_guest_orientation';return
            template.orient[i]=min(choices,key=lambda o:(o!=old,o))
        scene=build_scene(par,nets,template,index,options=options,deadline=until)
        env=owned_footprints(par,scene,nets,index);diagnostic['scene_templates']+=1
        anchor=template.pin_world(h,pin);origin=template.pos_of(h);relative={};offset={}
        for r in group['rail_sides'].values():
            i=index[r['device']];p=template.pin_world(i,r['signal_pin']);base=template.pos_of(i)
            relative[i]=env[i][0]-p[0],env[i][1]-p[1],env[i][2]-p[0],env[i][3]-p[1]
            offset[i]=p[0]-base[0],p[1]-base[1]
        for col in cols:
            shift=pl.column_x[col]-origin[0]
            for extra in (gap,2*gap):
                if perf_counter()>=until:diagnostic['stop_reason']='deadline';return
                sign=1 if direction=='east' else -1
                front=env[h][2]+shift if sign>0 else env[h][0]+shift
                raw=front+extra-min(r[0] for r in relative.values()) if sign>0 else front-extra-max(r[2] for r in relative.values())
                x=anchor[0]+shift+sign*ceil(sign*(raw-anchor[0]-shift)/step)*step
                targets={}
                for side,r in group['rail_sides'].items():
                    i=index[r['device']];dy=(max(0,relative[i][3]) if side=='north' else max(0,-relative[i][1]))+gap
                    y=anchor[1]+(-1 if side=='north' else 1)*ceil(dy/step)*step
                    targets[i]=(x-offset[i][0],y-offset[i][1])
                projected={i:pl.cell_of[i][1] for i in ids};projected[h]=col
                options_by_key.append(((forecast(projected),abs(col-pl.cell_of[h][1]),extra,
                    orientation!=pl.orient[h],col,orientation),template,col,targets))
    seen=set()
    for key,template,col,targets in sorted(options_by_key,key=lambda v:v[0]):
        if diagnostic['generated']>=max_trials or perf_counter()>=until:return
        trial=deepcopy(template);cells=list(trial.cell_of);cells[h]=(row,col)
        trial.set_state(cells,trial.orient)
        values=list(getattr(trial,'passive_local_offsets',[(0,0)]*len(comps)))
        for i,(x,y) in targets.items():
            old=trial.pos_of(i);ox,oy=values[i];values[i]=(x-old[0]+ox,y-old[1]+oy)
        trial.passive_local_offsets=values
        identifier=(tuple(trial.cell_of),tuple(trial.orient),tuple(values))
        if identifier in seen:continue
        seen.add(identifier);diagnostic['generated']+=1
        yield trial,dict(kind='control_rail_shunt_packing',version=VERSION,group=group['id'],
            host=group['host'],host_column=col,members=group['members'],
            positions={comps[i]['id']:list(p) for i,p in targets.items()},
            logical_guest_cells_axes_and_active_rows_preserved=True,ownership_unchanged=True,
            source='actual_control_pin_body_fixed_exit_and_rail_marker_envelopes')
    diagnostic['stop_reason']='considered_finite_templates'


def reserve_finish(plan,budget):
    if plan.get('control_rail_shunts',{}).get('groups') and not budget.get('control_shunt_finish_done'):
        budget['control_shunt_reserve']=min(12,budget['max_candidates']//8)


def finish_shunts(par,current,comps,nets,index,plan,options,budget,until,report,*,hpwl_limit):
    from netlist2schematic.plugins.attachment import attachment_integration as engine
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    from netlist2schematic.routing.routing_rules import RoutingOptions
    from netlist2schematic.routing.orthogonal_router import RoutingFailure
    model=plan.get('control_rail_shunts',{});reserve=budget.pop('control_shunt_reserve',0)
    if not model.get('groups'):return current
    budget['control_shunt_finish_done']=True;options=options or RoutingOptions()
    before=measure_shunts(current,comps,index,model)
    detail=dict(version=VERSION,before=before,applied=False,trials=0,accepted=0,
        state_rejections=0,static_rejections=0,hpwl_rejections=0,objective_rejections=0,
        reserved_trials=reserve,generation=[],stop_reason='no_improvement',shared_budget_reset=False)
    report['control_rail_shunt_finish']=detail
    if shunt_rank(before) is None:detail['stop_reason']='unmeasurable';return current
    for number,group in enumerate(model['groups']):
        allowance=max(0,(12-detail['trials'])//(len(model['groups'])-number))
        diagnostic=dict(group=group['id']);detail['generation'].append(diagnostic)
        old_rank=shunt_rank(measure_shunts(current,comps,index,model))
        old_attachments=engine.measure_attachments(current,comps,index,plan)
        best=None
        if not allowance or not engine._has_budget(budget,until):
            detail['stop_reason']='shared_attachment_budget';break
        try:
            for trial,action in shunt_trials(par,current,comps,nets,index,model,group,options,until,
                                              max_trials=allowance,diagnostic=diagnostic):
                if not engine._has_budget(budget,until):detail['stop_reason']='shared_attachment_budget';break
                budget['candidates']+=1;report['candidates']+=1;detail['trials']+=1
                if not trial.valid_state() or not engine._fixed_world_safe(current,trial,index):
                    detail['state_rejections']+=1;continue
                measured=measure_shunts(trial,comps,index,model);rank=shunt_rank(measured)
                attachments=engine.measure_attachments(trial,comps,index,plan)
                if (rank is None or rank>=old_rank or not attachments.get('measurable')
                        or any(attachments[k]>old_attachments[k]+EPS
                               for k in ('active_order_errors','violations','pin_axis_error'))):
                    detail['objective_rejections']+=1;continue
                hpwl=_hpwl(par,trial,nets,index)
                if hpwl>hpwl_limit+EPS:detail['hpwl_rejections']+=1;continue
                safe=engine._checked_static(par,trial,nets,index,options,until)
                if safe is None or perf_counter()>=until:
                    if safe is None:budget['exhausted']=True
                    detail['stop_reason']='shared_attachment_budget';break
                if not safe:detail['static_rejections']+=1;continue
                key=rank,hpwl
                if best is None or key<best[0]:best=key,trial,action
        except RoutingFailure as exc:
            if exc.diagnostics.get('reason')!='quality_time_budget':raise
            budget['exhausted']=True;detail['stop_reason']='shared_attachment_budget'
        if best:
            _,current,action=best
            report['actions'].append(action);report['accepted']+=1
            detail['applied']=True;detail['accepted']+=1
        if not engine._has_budget(budget,until):detail['stop_reason']='shared_attachment_budget';break
    if detail['applied'] and detail['stop_reason']!='shared_attachment_budget':
        detail['stop_reason']='best_verified_trial'
    detail['after']=measure_shunts(current,comps,index,model)
    return current
