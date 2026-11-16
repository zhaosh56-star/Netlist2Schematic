"""Place an exclusive rail-connected tail by its own pair's real S/E axis.

No global symmetry axis, new rows, SA actions or routing search is introduced.
Common bias alone is not ownership. Clock/latch and foreign functional owners
are deliberately excluded; ordinary source/marker geometry stays authoritative.
"""
from collections import Counter,defaultdict
from copy import deepcopy
from math import isfinite
from time import perf_counter

VERSION='exclusive-tail-axis-v2'
ACTIVE=frozenset(('nmos','pmos','npn','pnp'))
VECTORS={'north':(0,-1),'south':(0,1),'west':(-1,0),'east':(1,0)}
EPS=1e-8


def compile_tails(devices,comps,structs,rows,*,policy='all'):
    result=dict(version=VERSION,groups=[],skipped=[])
    if policy!='all' or sum(c['symbol'] in ACTIVE for c in comps)<3:return result
    from netlist2schematic.plugins.layout_shared.layout_evidence import build_layout_evidence
    evidence=build_layout_evidence(devices,structs);by_id={c['id']:c for c in comps}
    roles=evidence['net_roles'];incidence=defaultdict(set)
    for c in comps:
        for p,n in c['nodes'].items():incidence[n].add((c['id'],p))
    contexts=[]
    for s in structs:
        if s['kind'] in ('latch','cross_coupled'):
            names=s.get('devices',s.get('members',()))
            contexts.append({n for name in names if name in by_id for p,n in by_id[name]['nodes'].items()
                if p in ('D','S','C','E') and roles.get(n,{}).get('role')!='power'})
    for relation in evidence['relations']:
        if relation['kind'] not in ('differential_pair','bjt_differential_pair'):continue
        pair=relation['members'];r=relation['roles'];tails=r.get('exclusive_tails',())
        if len(pair)!=2:continue
        if len(tails)!=1:
            if r.get('tail'):
                result['skipped'].append(dict(pair=relation['id'],tails=list(r['tail']),
                    reason='nonexclusive_or_reserved_tail'))
            continue
        tail=tails[0]
        if any(n not in by_id for n in pair+[tail]):continue
        symbol=by_id[tail]['symbol']
        if symbol not in ACTIVE or any(by_id[n]['symbol']!=symbol for n in pair):continue
        collector,control,emitter=('D','G','S') if symbol in ('nmos','pmos') else ('C','B','E')
        net=r['source_net'];nodes=by_id[tail]['nodes'];rail=nodes[emitter];reason=None
        foreign=[s for s in structs if s.get('confidence')=='high'
            and s['kind']!='differential_pair' and tail in s.get('devices',s.get('members',()))]
        peers={by_id[name]['nodes'][collector] for name in pair}
        if roles.get(net,{}).get('role')=='power' or roles.get(rail,{}).get('role')!='power':
            reason='unverified_tail_supply'
        elif (nodes[collector]!=net or nodes[control] in (net,rail)
              or incidence[net]!={(n,emitter) for n in pair}|{(tail,collector)}):
            reason='nonexclusive_or_diode_tail'
        elif any(peers.intersection(context) for context in contexts):reason='latch_or_clock_context'
        elif foreign:reason='tail_has_other_functional_owner'
        elif len({rows[n] for n in pair})!=1:reason='input_pair_rows_incompatible'
        elif ((rows[tail]-rows[pair[0]])*(1 if symbol in ('nmos','npn') else -1)<=0):
            reason='tail_row_order'
        elif any(by_id[n]['symbol'] in ACTIVE and by_id[n]['symbol']!=symbol
                 for n,p in incidence[nodes[control]] if p in ('G','B')):
            reason='mixed_polarity_control_bank'
        if reason:
            result['skipped'].append(dict(pair=relation['id'],tail=tail,reason=reason));continue
        result['groups'].append(dict(id='tail_axis:'+tail+':'+relation['id'],pair=relation['id'],
            members=list(pair),tail=tail,symbol=symbol,pin=collector,source_pin=emitter,
            control_pin=control,net=net,rail=rail,control_net=nodes[control],
            terminal_contract={n:deepcopy(by_id[n]['nodes']) for n in pair+[tail]},
            evidence=deepcopy(relation['evidence']),roles=deepcopy(r)))
    claims=Counter(g['tail'] for g in result['groups'])
    duplicates=[g for g in result['groups'] if claims[g['tail']]!=1]
    result['skipped'].extend(dict(tail=g['tail'],reason='ambiguous_pair_tail_assignment') for g in duplicates)
    result['groups']=[g for g in result['groups'] if g not in duplicates]
    return result


def _target(pl,index,group):
    points=[pl.pin_world(index[n],group['source_pin']) for n in group['members']]
    return tuple(sum(p[k] for p in points)/2 for k in (0,1))


def _control_backtrack(pl,comps,index,group):
    i=index[group['tail']];point=pl.pin_world(i,group['control_pin'])
    others=[pl.pin_world(j,p) for j,c in enumerate(comps) for p,n in c['nodes'].items()
            if n==group['control_net'] and (j,p)!=(i,group['control_pin'])]
    if not all(isfinite(x) for p in [point]+others for x in p):
        raise ValueError('invalid_tail_control_geometry')
    if not others:return 0.0
    target=tuple(sum(p[k] for p in others)/len(others) for k in (0,1))
    v=VECTORS[pl.pin_dir(i,group['control_pin'])]
    return max(0.0,-sum((a-b)*d for a,b,d in zip(target,point,v)))/100


def measure_tails(pl,comps,index,model):
    records,missing=[],[]
    for group in model.get('groups',()):
        try:
            for name,contract in group['terminal_contract'].items():
                if comps[index[name]]['nodes']!=contract or comps[index[name]]['symbol']!=group['symbol']:
                    raise ValueError('tail_or_pair_contract_changed')
            target=_target(pl,index,group);i=index[group['tail']];point=pl.pin_world(i,group['pin'])
            if not all(isfinite(x) for x in target+tuple(point)):raise ValueError('invalid_tail_geometry')
            pairrows=[pl.cell_of[index[n]][0] for n in group['members']];tailrow=pl.cell_of[i][0]
            rowerror=len(set(pairrows))!=1 or (tailrow-pairrows[0])*(1 if group['symbol'] in ('nmos','npn') else -1)<=0
            vector=VECTORS[pl.pin_dir(i,group['pin'])]
            wrong=vector[0]!=0 or (target[1]-point[1])*vector[1]<=EPS
            error=abs(target[0]-point[0])
            records.append(dict(id=group['id'],tail=group['tail'],members=group['members'],
                target=list(target),point=list(point),row_errors=int(rowerror),port_direction_errors=int(wrong),
                axis_error=error/100,axis_error_pixels=error,
                control_backtracking=_control_backtrack(pl,comps,index,group)))
        except (KeyError,IndexError,TypeError,ValueError,AttributeError) as exc:
            missing.append(dict(id=group.get('id'),reason=str(exc)))
    measurable=bool(model.get('groups')) and not missing
    return dict(version=VERSION,count=len(model.get('groups',())),measurable=measurable,
        records=records,missing=missing,skipped=model.get('skipped',[]),
        **{k:sum(r[k] for r in records) if measurable else None
           for k in ('row_errors','port_direction_errors','axis_error','control_backtracking')})


def tail_rank(measured):
    if not measured.get('count'):return (0,0,0,0)
    keys=('row_errors','port_direction_errors','axis_error','control_backtracking')
    if not measured.get('measurable') or any(type(measured.get(k)) not in (int,float)
            or not isfinite(measured[k]) or measured[k]<0 for k in keys):return None
    return tuple(round(measured[k],8) for k in keys)


def eligible_groups(pl,index,model,metadata=None):
    from netlist2schematic.plugins.attachment.attachment_integration import _fixed_ids
    fixed=_fixed_ids(pl,index);groups=[];skipped=[]
    clocks={a.get('device') for a in (metadata or {}).get('role_adjustments',()) if a.get('kind')=='latch_clock_access'}
    blocks=list(getattr(pl,'blocks',()))+list(getattr(pl,'layout_macro_blocks',()))
    for g in model.get('groups',()):
        i=index.get(g['tail']);reason=None
        if i is None:reason='missing_tail'
        elif i in fixed:reason='fixed_tail'
        elif g['tail'] in clocks:reason='clock_access_orientation'
        elif any(i in b and len(b)>1 for b in blocks):reason='tail_has_runtime_owner'
        if reason:skipped.append(dict(id=g['id'],reason=reason))
        else:groups.append(g)
    return groups,skipped


def reserve_finish(plan,budget,pl,index):
    if budget.get('tail_transistor_finish_done'):return
    groups,_=eligible_groups(pl,index,plan.get('tail_transistors',{}))
    if not groups:return
    reserve=min(8,4*len(groups),budget['max_candidates']//16)
    budget['tail_transistor_reserve']=reserve
    budget['tail_transistor_reserved_trials']=reserve


def _snap(pl,index,group,grid):
    from netlist2schematic.plugins.layout_shared.branch_alignment import _grid_offset
    i=index[group['tail']];desired=pl.offset_x[i]+_target(pl,index,group)[0]-pl.pin_world(i,group['pin'])[0]
    value=_grid_offset(pl,i,desired,grid)
    if value is None:return False
    pl.offset_x[i]=value
    return True


def tail_trials(pl,comps,index,group,eligible,*,deadline=None,grid=2,prune_dominated=False,diagnostic=None,
                measurement_model=None):
    i=index[group['tail']];row=pl.cell_of[i][0];target=_target(pl,index,group)[0]
    by_index={index[g['tail']]:g for g in eligible};permitted=set(by_index)
    columns=[col for col in range(pl.cols) if pl.comp_at.get((row,col)) in (None,*permitted)]
    columns.sort(key=lambda col:(abs(pl.column_x[col]-target),col!=pl.cell_of[i][1],pl.column_x[col]))
    measurement_model=measurement_model or dict(groups=eligible)
    before=measure_tails(pl,comps,index,measurement_model) if prune_dominated else None
    old_errors={r['tail']:r['axis_error'] for r in before['records']} if before is not None else {}
    seen=set()
    for col in columns[:2]:
        for orientation in pl.allowed_orientations(i):
            if orientation not in (0,1):continue
            if deadline is not None and perf_counter()>=deadline:return
            trial=deepcopy(pl);trial.offset_x=list(getattr(pl,'offset_x',[0.0]*len(comps)))
            cells=list(trial.cell_of);orient=list(trial.orient);other=pl.comp_at.get((row,col))
            affected=[group];cells[i]=(row,col);orient[i]=orientation
            if other is not None and other!=i:
                cells[other]=pl.cell_of[i];affected.append(by_index[other])
            trial.set_state(cells,orient)
            if not _snap(trial,index,group,grid):continue
            if other is not None and other!=i:
                peer=by_index[other];choices=[]
                for o in trial.allowed_orientations(other):
                    if o not in (0,1):continue
                    trial.orient[other]=o
                    if _snap(trial,index,peer,grid):
                        choices.append(((abs(trial.pin_world(other,peer['pin'])[0]-_target(trial,index,peer)[0]),
                            _control_backtrack(trial,comps,index,peer),o),o,trial.offset_x[other]))
                if not choices:continue
                _,trial.orient[other],trial.offset_x[other]=min(choices)
            key=tuple(trial.cell_of),tuple(trial.orient),tuple(trial.offset_x)
            old=tuple(pl.cell_of),tuple(pl.orient),tuple(getattr(pl,'offset_x',[0.0]*len(comps)))
            if key in seen or key==old:continue
            seen.add(key)
            if diagnostic is not None:diagnostic['normal_generated']+=1
            if prune_dominated:
                measured=measure_tails(trial,comps,index,measurement_model)
                rank=tail_rank(measured);previous=tail_rank(before)
                if (rank is None or previous is None or rank>=previous or any(
                        r['axis_error']>old_errors[r['tail']]+EPS for r in measured['records']
                        if r['tail'] in {g['tail'] for g in affected})):
                    if diagnostic is not None:diagnostic['normal_dominated']+=1
                    continue
            yield trial,dict(kind='exclusive_tail_axis',version=VERSION,group=group['id'],
                tails=[g['tail'] for g in affected],members=[g['tail'] for g in affected],
                atomic_swap=len(affected)>1,rows_unchanged=True,axes_unchanged=True,actual_pins=True)


def finish_tails(par,pl,comps,nets,index,plan,options,budget,until,report,metadata,*,hpwl_limit):
    from netlist2schematic.plugins.attachment import attachment_integration as ai
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    from netlist2schematic.routing.routing_rules import RoutingOptions
    reserved=budget.pop('tail_transistor_reserve',0);budget['tail_transistor_finish_done']=True
    model=plan.get('tail_transistors',{});groups,skipped=eligible_groups(pl,index,model,metadata)
    detail=dict(version=VERSION,count=len(model.get('groups',())),reserved_trials=reserved,
        trials=0,accepted=0,skipped=skipped,reason='not_applicable',actions=[])
    detail['generation']=dict(normal_generated=0,normal_dominated=0,partial_groups=[],
        policy='old_improving_candidates_first;partial_only_after_no_accepted_old_candidate')
    report['tail_transistor_finish']=detail
    before=measure_tails(pl,comps,index,model);detail['before']=before
    if not groups or not reserved:
        if groups:detail['reason']='no_reserved_trials'
        return pl
    if tail_rank(before) is None:detail['reason']='unknown_tail_geometry';return pl
    detail.update(reason='no_legal_improvement',stop_reason='candidate_set_exhausted')
    errors={r['tail']:r['axis_error'] for r in before['records']}
    groups.sort(key=lambda g:(-errors[g['tail']],_target(pl,index,g)[0],g['id']))
    for group in groups:
        if detail['trials']>=reserved or not ai._has_budget(budget,until):
            detail['reason']='trial_or_shared_budget';detail['stop_reason']='trial_or_shared_budget';break
        measured=measure_tails(pl,comps,index,model);old_rank=tail_rank(measured)
        transaction_before=deepcopy(measured)
        old_errors={r['tail']:r['axis_error'] for r in measured['records']}
        old=ai.measure_attachments(pl,comps,index,plan);legacy=ai.attachment_rank(old);best=None
        def proposals():
            previous=set();tail_index=index[group['tail']]
            for trial,action in tail_trials(pl,comps,index,group,groups,deadline=until,
                grid=(options or RoutingOptions()).grid_step,prune_dominated=True,
                diagnostic=detail['generation'],measurement_model=model):
                if trial.cell_of[tail_index]==pl.cell_of[tail_index] and len(action['tails'])==1:
                    previous.add((trial.orient[tail_index],trial.offset_x[tail_index]))
                yield trial,action
            if best is not None or detail['trials']>=reserved or not ai._has_budget(budget,until):return
            from netlist2schematic.plugins.attachment.attachment_tail_boundaries import partial_tail_trials
            from netlist2schematic.routing.orthogonal_router import RoutingFailure
            partial=dict(group=group['id'],scene_templates=0,forbidden_intervals=0,generated=0,
                excluded_old_points=len(previous),stop_reason='not_started')
            detail['generation']['partial_groups'].append(partial)
            try:
                yield from partial_tail_trials(par,pl,comps,nets,index,group,options=options or RoutingOptions(),
                    deadline=until,diagnostic=partial,limit=reserved-detail['trials'],excluded=previous)
            except RoutingFailure as exc:
                if exc.diagnostics.get('reason')!='quality_time_budget':raise
                budget['exhausted']=True;partial['stop_reason']='shared_time_budget'
                detail['reason']='shared_attachment_budget';detail['stop_reason']='shared_attachment_budget'
        for trial,action in proposals():
            if detail['trials']>=reserved or not ai._has_budget(budget,until):
                detail['stop_reason']='trial_or_shared_budget';break
            budget['candidates']+=1;report['candidates']+=1;detail['trials']+=1
            if not trial.valid_state() or not ai._fixed_world_safe(pl,trial,index):continue
            measured=measure_tails(trial,comps,index,model);key=tail_rank(measured)
            if key is None or key>=old_rank:continue
            if any(r['axis_error']>old_errors[r['tail']]+EPS for r in measured['records'] if r['tail'] in action['tails']):continue
            after=ai.measure_attachments(trial,comps,index,plan);rank=ai.attachment_rank(after)
            if rank is None or legacy is None or rank[:4]>legacy[:4]:continue
            if (old.get('source_count') and after['source_errors']>old['source_errors']):continue
            if _hpwl(par,trial,nets,index)>hpwl_limit+EPS:continue
            safe=ai._checked_static(par,trial,nets,index,options,until)
            if safe is None:
                budget['exhausted']=True;detail['reason']='shared_attachment_budget'
                detail['stop_reason']='shared_attachment_budget';break
            if not safe:continue
            if best is None or key<best[0]:best=(key,trial,dict(action,before=transaction_before,after=measured))
        if best:
            _,pl,action=best;detail['actions'].append(action);detail['accepted']+=1
            report['actions'].append(action);report['accepted']+=1;detail['reason']='applied'
    detail['after']=measure_tails(pl,comps,index,model)
    return pl
