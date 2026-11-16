"""Neutral conduction cells and actual gate-access ordering, opt-in only.

No logic/timing function or missing connection is inferred. Shared supply and
control are interfaces, never transitive ownership edges.
"""
from collections import defaultdict
from copy import deepcopy
from itertools import combinations
from math import isfinite
from time import perf_counter
from netlist2schematic.netlist.netlist_core import SRC_ALIASES,SNK_ALIASES

VERSION='neutral-conduction-cells-v1'
EPS=1e-8

PINS={'nmos':('D','G','S'),'pmos':('D','G','S'),'npn':('C','B','E'),'pnp':('C','B','E')}
OPPOSITE={'nmos':'pmos','pmos':'nmos','npn':'pnp','pnp':'npn'}


def detect(comps,power_nets,formal_nets=()):
    parts={c['id']:c for c in comps};active={n:c for n,c in parts.items() if c['symbol'] in PINS}
    incidence=defaultdict(list);channel=defaultdict(list);adj={n:set() for n in active}
    for c in comps:
        for pin,net in c['nodes'].items():
            incidence[net].append((c['id'],pin))
            if c['id'] in active and pin in (PINS[c['symbol']][0],PINS[c['symbol']][2]):
                channel[net].append((c['id'],pin))
    edges=[];forks=set()
    for net,users in channel.items():
        if net in power_nets:continue
        if len(users)!=2 or users[0][0]==users[1][0]:
            if len(users)>2:forks.add(net)
            continue
        (a,pa),(b,pb)=users;ca,cb=active[a],active[b]
        da,ga,sa=PINS[ca['symbol']];db,gb,sb=PINS[cb['symbol']]
        same=ca['symbol']==cb['symbol']
        direct=same and ((pa==da and pb==sb) or (pa==sa and pb==db))
        join=ca['symbol']==OPPOSITE[cb['symbol']] and pa==da and pb==db
        if not direct and not join:continue
        # A capacitor attached at a drain junction is an explicit load. A
        # tapped internal series segment has no unique cell-spine evidence.
        if direct and any(n not in active for n,_ in incidence[net]):continue
        adj[a].add(b);adj[b].add(a)
        edges.append(dict(a=a,pin_a=pa,b=b,pin_b=pb,net=net,kind='complementary_drain_join' if join else 'series'))
    pending=set(active);stages=[];skipped=[]
    while pending:
        todo=[min(pending)];members=set()
        while todo:
            n=todo.pop()
            if n in members:continue
            members.add(n);todo.extend(adj[n]-members)
        pending-=members
        relevant=[e for e in edges if e['a'] in members and e['b'] in members]
        joins=[e for e in relevant if e['kind']=='complementary_drain_join']
        rails=[]
        for n in members:
            d,g,s=PINS[active[n]['symbol']]
            for pin in (d,s):
                if active[n]['nodes'][pin] in power_nets:rails.append((n,pin,active[n]['nodes'][pin]))
        reason=None
        if len(joins)!=1:reason='no_unique_complementary_drain_junction'
        elif len(relevant)!=len(members)-1 or any(len(adj[n])>2 for n in members):reason='not_a_simple_conduction_path'
        elif len(rails)!=2 or rails[0][2]==rails[1][2]:reason='not_two_distinct_supply_boundaries'
        elif any(active[n]['nodes'][pin] in forks for n in members for pin in (PINS[active[n]['symbol']][0],PINS[active[n]['symbol']][2])):
            reason='conduction_fork_inside_candidate'
        else:
            upper=[n for n,pin,rail in rails if active[n]['symbol'] in ('pmos','pnp')
                and pin==PINS[active[n]['symbol']][2] and str(rail).casefold() in SRC_ALIASES]
            lower=[n for n,pin,rail in rails if active[n]['symbol'] in ('nmos','npn')
                and pin==PINS[active[n]['symbol']][2] and str(rail).casefold() in SNK_ALIASES]
            if len(upper)!=1 or len(lower)!=1:reason='supply_polarity_not_proved'
        if reason:
            skipped.append(dict(members=sorted(members),reason=reason));continue
        spine=[upper[0]];previous=None
        while spine[-1]!=lower[0]:
            options=adj[spine[-1]]-({previous} if previous else set())
            if len(options)!=1:reason='nonunique_spine';break
            following=next(iter(options));previous=spine[-1]
            if following in spine:reason='cyclic_spine';break
            spine.append(following)
        if reason:
            skipped.append(dict(members=sorted(members),reason=reason));continue
        join=joins[0]
        stages.append(dict(id='neutral_cell:'+'|'.join(sorted(members)),members=sorted(members),
            conduction_spine=spine,source_boundaries=[list(r) for r in rails],output_net=join['net'],
            axes=relevant,evidence='unique_simple_supply_to_supply_path_with_one_complementary_drain_junction',
            semantic_function=None))
    stages.sort(key=lambda c:c['id']);owner={n:c['id'] for c in stages for n in c['members']}
    directed=[]
    for cell in stages:
        net=cell['output_net']
        targets=sorted({owner[n] for n,pin in incidence[net] if n in owner
                        and pin==PINS[active[n]['symbol']][1] and owner[n]!=cell['id']})
        directed.extend(dict(source=cell['id'],target=target,net=net,kind='drive_to_control') for target in targets)
    next_cells={c['id']:set() for c in stages};in_degree={n:0 for n in next_cells}
    for e in directed:
        if e['target'] not in next_cells[e['source']]:
            next_cells[e['source']].add(e['target']);in_degree[e['target']]+=1
    ready=sorted(n for n,d in in_degree.items() if d==0);order=[]
    while ready:
        n=ready.pop(0);order.append(n)
        for other in sorted(next_cells[n]):
            in_degree[other]-=1
            if in_degree[other]==0:ready.append(other);ready.sort()
    cyclic=sorted(set(next_cells)-set(order))
    unknown_controls=[];interfaces=[]
    outputs={c['output_net'] for c in stages}
    for net,users in sorted(incidence.items()):
        controls=[(n,p) for n,p in users if n in owner and p==PINS[active[n]['symbol']][1]]
        if not controls or net in power_nets:continue
        consumers=sorted({owner[n] for n,p in controls})
        interfaces.append(dict(net=net,consumers=consumers,terminals=[list(p) for p in controls],
            formal=net in formal_nets,has_cell_output_driver=net in outputs))
        if net not in outputs and net not in formal_nets:
            unknown_controls.append(dict(net=net,terminals=[list(p) for p in controls],
                reason='no_visible_drive_source_in_selected_netlist',not_a_parse_failure=True))
    covered=set(owner)
    return dict(version=VERSION,stages=stages,stage_edges=directed,
        ownership=owner,interfaces=interfaces,unknown_control_interfaces=unknown_controls,
        topological_order=order,cycle_members=cyclic,skipped=skipped,
        coverage=dict(active=len(active),covered=len(covered),unassigned=sorted(set(active)-covered)),
        global_layout_eligible=bool(stages and directed and not cyclic and covered==set(active)),
        inference_scope='neutral topology only; no timing/logic/feedback-function claim',
        recognition_only=True)


def compile_stages(comps,junctions,rows,owners,*,formal_ports=()):
    power={n for n,j in junctions.items() if j['is_rail']}
    formal={p if isinstance(p,str) else p['net'] for p in formal_ports}
    model=detect(comps,power,formal)
    model.update(power_nets=sorted(power),formal_nets=sorted(formal),
        terminal_contract={c['id']:dict(symbol=c['symbol'],nodes=deepcopy(c['nodes'])) for c in comps},
        row_contract={c['id']:rows[c['id']] for c in comps},layout_skip_reasons=[])
    owner=model['ownership'];by_id={c['id']:c for c in comps}
    for cell in model['stages']:
        if len({rows[n] for n in cell['members']})!=len(cell['members']):
            model['layout_skip_reasons'].append(dict(stage=cell['id'],reason='same_row_spine_members'))
        output=cell['output_net']
        if any(c['symbol'] in ('current-source','voltage-source') and output in c['nodes'].values() for c in comps):
            model['layout_skip_reasons'].append(dict(stage=cell['id'],reason='external_source_at_drain_junction'))
    if any(c['symbol'] not in set(PINS)|{'resistor','capacitor','inductor','diode','current-source','voltage-source'} for c in comps):
        model['layout_skip_reasons'].append(dict(reason='other_active_core_outside_cell_graph'))
    for block in owners:
        claimed={owner[n] for n in block if n in owner}
        if claimed and (len(claimed)>1 or any(n not in owner for n in block)):
            model['layout_skip_reasons'].append(dict(members=list(block),reason='preexisting_owner_spans_cells'))
    model['global_layout_eligible'] &= not model['layout_skip_reasons']
    model['recognition_only']=not model['global_layout_eligible']
    return model


def measure_stages(pl,comps,index,model):
    count=len(model.get('stages',())) if model.get('global_layout_eligible') else 0
    result=dict(version=VERSION,count=count,recognized_count=len(model.get('stages',())),
        applicable=bool(count),measurable=False,records=[],missing=[],
        skip_reasons=deepcopy(model.get('layout_skip_reasons',())),
        unknown_control_interfaces=deepcopy(model.get('unknown_control_interfaces',())),
        row_errors=None,flow_order_errors=None,cross_row_order_errors=None,
        pin_axis_error=None,cell_span=None,live_protected_cells=None)
    if not count:return result
    try:
        contract=model['terminal_contract']
        if len(contract)!=len(comps) or any(comps[index[n]]['symbol']!=c['symbol']
                or comps[index[n]]['nodes']!=c['nodes'] for n,c in contract.items()):
            raise ValueError('neutral_cell_terminal_contract_changed')
        centers={};by_row={};row_errors=0;axis=0.;span=0.
        blocks=[set(b) for b in pl.blocks] if hasattr(pl,'blocks') else None
        protected=0
        for cell in model['stages']:
            ids=[index[n] for n in cell['members']]
            points=[pl.pos_of(i) for i in ids]
            if not all(isfinite(v) for p in points for v in p):raise ValueError('nonfinite_cell')
            xs=[p[0] for p in points];width=max(xs)-min(xs)
            centers[cell['id']]=sum(xs)/len(xs)
            rows=defaultdict(list)
            for n,i,p in zip(cell['members'],ids,points):
                rows[pl.cell_of[i][0]].append(p[0])
                row_errors+=pl.cell_of[i][0]!=model['row_contract'][n]
            by_row[cell['id']]={r:sum(v)/len(v) for r,v in rows.items()}
            error=sum(abs(pl.pin_world(index[e['a']],e['pin_a'])[0]
                -pl.pin_world(index[e['b']],e['pin_b'])[0]) for e in cell['axes'])
            if blocks is not None:protected+=set(ids) in blocks
            axis+=error;span+=width
            result['records'].append(dict(id=cell['id'],members=cell['members'],
                spine=cell['conduction_spine'],pin_axis_error=error,span=width,
                protected_leaf=set(ids) in blocks if blocks is not None else None))
        forward=sum(centers[e['source']]>=centers[e['target']]-EPS for e in model['stage_edges'])
        backward=sum(centers[e['source']]<=centers[e['target']]+EPS for e in model['stage_edges'])
        row_conflicts=0
        names=sorted(by_row)
        for k,a in enumerate(names):
            for b in names[k+1:]:
                common=sorted(set(by_row[a])&set(by_row[b]))
                if len(common)>1:
                    sign=by_row[a][common[0]]-by_row[b][common[0]]
                    row_conflicts+=sum(sign*(by_row[a][r]-by_row[b][r])<-EPS for r in common[1:])
        result.update(measurable=True,row_errors=row_errors,flow_order_errors=min(forward,backward),
            cross_row_order_errors=row_conflicts,pin_axis_error=axis,cell_span=span,
            live_protected_cells=protected if blocks is not None else None)
    except (KeyError,ValueError,TypeError,IndexError,AttributeError) as exc:
        result['missing'].append(dict(reason=str(exc)))
    return result


def stage_rank(measured):
    if not measured.get('count'):return ()
    keys=('row_errors','flow_order_errors','cross_row_order_errors','pin_axis_error','cell_span')
    if not measured.get('measurable') or any(type(measured.get(k)) not in (int,float)
            or not isfinite(measured[k]) or measured[k]<0 for k in keys):return None
    return tuple(round(measured[k],8) for k in keys)


def stage_owners(pl,comps,index,model):
    """Replace subset owners, never join cells via their control interfaces."""
    cells=[set(index[n] for n in c['members']) for c in model['stages']]
    taken=set().union(*cells)
    for block in pl.blocks+getattr(pl,'layout_macro_blocks',[]):
        if set(block)&taken and not any(set(block)<=cell for cell in cells):return False
    previous=list(getattr(pl,'motion_blocks',()))
    pl.blocks=[b for b in pl.blocks if not set(b)&taken]+[sorted(c) for c in cells]
    pl.block_of={i:k for k,b in enumerate(pl.blocks) for i in b}
    pl.motion_blocks=pl.blocks+[b for b in previous if not set(b)&taken and b not in pl.blocks]
    return True


def _orders(pl,index,model):
    cells={c['id']:c for c in model['stages']};adj={n:set() for n in cells};out={n:set() for n in cells}
    for e in model['stage_edges']:
        adj[e['source']].add(e['target']);adj[e['target']].add(e['source']);out[e['source']].add(e['target'])
    exclusive={r['consumers'][0] for r in model['interfaces']
               if r['formal'] and not r['has_cell_output_driver'] and len(r['consumers'])==1}
    remaining=set(cells);components=[]
    center=lambda n:sum(pl.pos_of(index[x])[0] for x in cells[n]['members'])/len(cells[n]['members'])
    while remaining:
        pending=[min(remaining)];part=set()
        while pending:
            n=pending.pop()
            if n in part:continue
            part.add(n);pending.extend(adj[n]-part)
        remaining-=part
        order=[n for n in model['topological_order'] if n in part]
        components.append((0 if part&exclusive else 1,sum(center(n) for n in part)/len(part),tuple(order)))
    components.sort()
    orders=[tuple(n for _,_,c in components for n in c)]
    for i in range(len(components)-1):
        if components[i][0]!=components[i+1][0]:continue
        other=list(components);other[i],other[i+1]=other[i+1],other[i]
        orders.append(tuple(n for _,_,c in other for n in c))
    # A globally mirrored chain is equally organized. Keep its existing pose
    # eligible for leaf protection instead of forcing a left-to-right move.
    return list(dict.fromkeys(v for order in orders for v in (order,order[::-1])))[:8]


def stage_trials(pl,comps,index,model,*,deadline=None,max_trials=16):
    if not model.get('global_layout_eligible'):return
    if stage_rank(measure_stages(pl,comps,index,model)) is None:return
    cells={c['id']:c for c in model['stages']};count=len(cells)
    if count>len(pl.column_x):return
    if max_trials<=0 or (deadline is not None and perf_counter()>=deadline):return
    # Give an already coherent pose a chance to gain real leaf ownership before
    # the bounded placement search. A forecast biased toward one reading
    # direction must not crowd this no-motion option out of the trial quota.
    protected=deepcopy(pl)
    if not stage_owners(protected,comps,index,model):return
    emitted=1
    yield protected,dict(kind='neutral_stage_layout',ownership_only=True,
        columns={n:pl.cell_of[index[n]][1] for c in model['stages'] for n in c['members']},
        cells=[c['members'] for c in model['stages']],new_connections=False,
        rows_unchanged=True,axes_unchanged=True)
    from netlist2schematic.plugins.attachment.attachment_regulation import _wire_forecast
    forecast=_wire_forecast(pl,comps,set(model['power_nets']))
    ids={index[n] for c in model['stages'] for n in c['members']}
    fixed=set(getattr(pl,'fixed',()))|set(getattr(pl,'fixed_cells',{}))
    fixed={index[x] if isinstance(x,str) and x in index else x for x in fixed}
    required={pl.cell_of[i][1] for i in ids&fixed}
    center=sum(pl.pos_of(i)[0] for i in ids)/len(ids)
    near=sorted(range(len(pl.column_x)),key=lambda c:(c not in required,abs(pl.column_x[c]-center),c))
    columns=sorted(near[:min(len(near),max(len(required),count+2))])
    options=[]
    for order in _orders(pl,index,model):
        for cols in combinations(columns,count):
            if deadline is not None and perf_counter()>=deadline:return
            target={index[n]:col for cell,col in zip(order,cols) for n in cells[cell]['members']}
            occupied={(pl.cell_of[i][0],col) for i,col in target.items()}
            if len(occupied)!=len(ids) or any(pl.comp_at.get(c) not in (None,*ids) for c in occupied):continue
            if any(col!=pl.cell_of[i][1] for i,col in target.items() if i in fixed):continue
            delta=sum(abs(pl.column_x[col]-pl.column_x[pl.cell_of[i][1]]) for i,col in target.items())
            options.append(((forecast(target),cols[-1]-cols[0],delta,tuple(cols),order),target,order))
    for key,target,order in sorted(options,key=lambda r:r[0]):
        for mirror in (0,1):
            if emitted>=max_trials or (deadline is not None and perf_counter()>=deadline):return
            trial=deepcopy(pl)
            if not stage_owners(trial,comps,index,model):continue
            positions=list(trial.cell_of)
            for i,c in target.items():positions[i]=(positions[i][0],c)
            trial.set_state(positions,trial.orient)
            for cell in model['stages']:
                head=index[cell['conduction_spine'][0]];pin=PINS[comps[head]['symbol']][0]
                old=trial.orient[head];axis=[]
                for v in trial.allowed_orientations(head):
                    if head in fixed and v!=old:continue
                    trial.orient[head]=v;axis.append(trial.pin_world(head,pin)[0])
                trial.orient[head]=old
                if not axis:continue
                target_x=(min if mirror==0 else max)(axis)
                for n in cell['members']:
                    i=index[n];d,g,s=PINS[comps[i]['symbol']];old=trial.orient[i];choices=[]
                    for v in trial.allowed_orientations(i):
                        if i in fixed and v!=old:continue
                        trial.orient[i]=v
                        error=abs(trial.pin_world(i,d)[0]-target_x)+abs(trial.pin_world(i,s)[0]-target_x)
                        choices.append((error,v!=old,v))
                    trial.orient[i]=min(choices)[-1] if choices else old
            emitted+=1
            yield trial,dict(kind='neutral_stage_layout',stage_order=list(order),
                columns={comps[i]['id']:c for i,c in target.items()},aligned_side=mirror,
                cells=[c['members'] for c in model['stages']],new_connections=False,
                rows_unchanged=True,axes_unchanged=True)


def reserve_finish(plan,budget):
    if plan.get('conduction_stages',{}).get('global_layout_eligible'):
        budget['stage_finish_reserve']=min(16,max(0,budget['max_candidates']-budget['candidates']))


def arrange_stages(par,current,comps,nets,index,plan,options,budget,until,report,*,initialization,hpwl_limit):
    model=plan.get('conduction_stages',{})
    if not model.get('global_layout_eligible'):return current
    from netlist2schematic.plugins.attachment.attachment_integration import _checked_static,_fixed_world_safe,_has_budget
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    before=measure_stages(current,comps,index,model)
    detail=dict(version=VERSION,before=before,applied=False,trials=0,static_rejections=0,
        state_rejections=0,hpwl_rejections=0,stop_reason='no_improvement',initialization=initialization)
    report['conduction_stage_arrangement']=detail
    if not initialization:budget.pop('stage_finish_reserve',None)
    if not _has_budget(budget,until):
        detail.update(stop_reason='shared_budget',after=before)
        return current
    best=None
    # Share the original sixteen trials: eight structural opportunities and
    # eight control-access mirrors. Neither operation adds a routing attempt.
    for trial,action in stage_trials(current,comps,index,model,deadline=until,max_trials=8):
        if not _has_budget(budget,until):detail['stop_reason']='shared_budget';break
        budget['candidates']+=1;detail['trials']+=1
        report['candidate_trials' if initialization else 'candidates']+=1
        if not trial.valid_state() or not _fixed_world_safe(current,trial,index):
            detail['state_rejections']+=1;continue
        measured=measure_stages(trial,comps,index,model);rank=stage_rank(measured)
        same=(trial.cell_of==current.cell_of and trial.orient==current.orient)
        policy_only=same and measured['live_protected_cells']>before['live_protected_cells']
        if rank is None or stage_rank(before) is None or (rank>=stage_rank(before) and not policy_only):continue
        hpwl=_hpwl(par,trial,nets,index)
        if hpwl>hpwl_limit+EPS:detail['hpwl_rejections']+=1;continue
        safe=_checked_static(par,trial,nets,index,options,until)
        if safe is None:budget['exhausted']=True;detail['stop_reason']='shared_budget';break
        if perf_counter()>=until:break
        if not safe:detail['static_rejections']+=1;continue
        key=(rank,hpwl)
        if best is None or key<best[0]:best=(key,trial,dict(action,policy_only_adoption=policy_only),measured)
    if best:
        _,current,action,after=best
        detail.update(applied=True,action=action,after=after,stop_reason='best_verified_trial')
        report['actions'].append(action)
        if not initialization:report['accepted']+=1
    else:detail['after']=before
    detail['structural_trials']=detail['trials']
    from netlist2schematic.plugins.attachment.attachment_stage_access import improve_access
    current,access=improve_access(par,current,comps,nets,index,plan,options,budget,until,report,
        initialization=initialization,hpwl_limit=hpwl_limit,max_trials=min(8,16-detail['trials']))
    detail['control_access']=access;detail['trials']+=access['trials']
    if access['applied']:
        detail.update(applied=True,after=measure_stages(current,comps,index,model),
                      stop_reason='best_verified_trial')
    return current
