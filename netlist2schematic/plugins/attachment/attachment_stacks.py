"""Repeated conduction-stack display rows, without functional or owner claims.

Two matching non-supply controls along unique same-polarity paths are required.
The existing partial-order row solver arbitrates hints against real conduction,
protected row cohorts and already accepted R/L bridges before SA starts.
"""
from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
import json
from math import isfinite

from netlist2schematic.netlist.netlist_core import SRC_ALIASES,SNK_ALIASES

VERSION='repeated-conduction-stack-rows-v1'
PINS={'nmos':('D','G','S'),'pmos':('D','G','S'),'npn':('C','B','E'),'pnp':('C','B','E')}


def detect_stacks(comps,power_nets):
    by_id={c['id']:c for c in comps};active={n:c for n,c in by_id.items() if c['symbol'] in PINS}
    channel=defaultdict(list);visible=defaultdict(list)
    for c in comps:
        for pin,net in c['nodes'].items():
            visible[net].append((c['id'],pin))
            if c['id'] in active and pin in (PINS[c['symbol']][0],PINS[c['symbol']][2]):
                channel[net].append((c['id'],pin))
    paths=[]
    for name,c in active.items():
        drain,control,anchor=PINS[c['symbol']];rail=c['nodes'][anchor]
        aliases=SNK_ALIASES if c['symbol'] in ('nmos','npn') else SRC_ALIASES
        if rail not in power_nets or str(rail).casefold() not in aliases:continue
        path=[name];controls=[c['nodes'][control]]
        while True:
            node=active[path[-1]]['nodes'][drain]
            if node in power_nets or len(channel[node])!=2:break
            others=[p for p in channel[node] if p!=(path[-1],drain)]
            if len(others)!=1:break
            other,pin=others[0]
            if other in path or pin!=anchor or active[other]['symbol']!=c['symbol']:break
            if any(n not in active for n,_ in visible[node]):break
            path.append(other);controls.append(active[other]['nodes'][control])
        paths.append(dict(id='supply_path:'+'|'.join(path),members=path,
            controls=controls,anchor_net=rail,symbol=c['symbol']))
    prefixes=defaultdict(list)
    for path in paths:
        for length in range(2,len(path['members'])+1):
            controls=tuple(path['controls'][:length])
            if any(net in power_nets for net in controls):break
            prefixes[(path['symbol'],path['anchor_net'],controls)].append(path)
    cohorts={}
    for (symbol,rail,controls),legs in prefixes.items():
        if len(legs)<2:continue
        identity=tuple(sorted(leg['id'] for leg in legs))
        if identity in cohorts and len(controls)<=len(cohorts[identity]['controls']):continue
        legs=sorted(legs,key=lambda p:p['id']);length=len(controls)
        cohorts[identity]=dict(id='repeated_stack:'+sha256(json.dumps(identity).encode()).hexdigest()[:16],
            symbol=symbol,anchor_net=rail,controls=list(controls),
            anchor_outward_paths=[leg['members'][:length] for leg in legs],
            same_row=[[leg['members'][i] for leg in legs] for i in range(length)],
            pair_claim=len(legs)==2,ownership_claim=False,function_claim=None,
            evidence='unique_same_polarity_paths_with_at_least_two_matching_nonpower_controls_and_same_supply_boundary')
    return dict(version=VERSION,groups=sorted(cohorts.values(),key=lambda g:g['id']),
        supply_paths=paths,applicable_ids=[],rows_applied=False,skip_reasons=[],
        terminal_contract={c['id']:dict(symbol=c['symbol'],nodes=deepcopy(c['nodes'])) for c in comps})


def plan_stack_rows(devices,comps,index,levels,readable,core_rows,blocks,*,fixed_rows=None):
    """Return a separate readable plan; failed hints never mutate the old one."""
    from netlist2schematic.plugins.layout_shared.branch_levels import plan_branch_rows
    power={n for n,j in readable['junctions'].items() if j['is_rail']}
    model=detect_stacks(comps,power)
    model['rows_before']=deepcopy(readable['display_rows'])
    if not model['groups']:
        model['status']='not_applicable';return readable,model
    # Recompute from the original row input, so accepted R/L slot insertion
    # happens once. Restore already accepted core and leaf same-row evidence.
    protected=[]
    for block in blocks:
        by_row=defaultdict(list)
        for n in block:by_row[readable['display_rows'][n]].append(n)
        protected.extend(sorted(v) for v in by_row.values() if len(v)>1)
    accepted_cores=set(core_rows.get('applied_relations',()))
    protected.extend(c['members'] for c in core_rows.get('cores',()) if c['id'] in accepted_cores)
    base=[dict(id='preserved_row:'+str(i),layout=dict(same_row=[v])) for i,v in enumerate(protected)]
    hints=[dict(id=g['id'],layout=dict(same_row=g['same_row'])) for g in model['groups']]
    original=[readable['original_rows'][c['id']] for c in comps]
    _,planned=plan_branch_rows(devices,levels,original,base+hints,comps,index)
    proposed=planned['display_rows']
    reasons=[]
    if any(len({proposed[n] for n in names})!=1 for names in protected):
        reasons.append('would_break_existing_row_cohort')
    old_bridges={b['id'] for b in (readable.get('branch_row_plan') or {}).get('bridge_alignment',())}
    if not old_bridges<={b['id'] for b in planned['bridge_alignment']}:
        reasons.append('would_displace_confirmed_rl_attachment')
    if any(proposed[n]!=row for n,row in (fixed_rows or {}).items()):
        reasons.append('fixed_display_row_conflict')
    model['row_plan']=deepcopy(planned)
    model['protected_cohorts']=deepcopy(protected)
    if reasons:
        model.update(status='skipped',skip_reasons=[dict(reason=r) for r in reasons])
        return readable,model
    accepted={frozenset(r['members']) for r in planned['same_row']}
    for g in model['groups']:
        if all(frozenset(names) in accepted for names in g['same_row']):
            model['applicable_ids'].append(g['id'])
        else:model['skip_reasons'].append(dict(group=g['id'],reason='same_row_conflicts_with_conduction'))
    if not model['applicable_ids']:
        model['status']='skipped';return readable,model
    # Dropped groups must not influence the committed plan through partial
    # hints. Re-solve with only complete, accepted groups and the old cohorts.
    hints=[r for r in hints if r['id'] in model['applicable_ids']]
    _,planned=plan_branch_rows(devices,levels,original,base+hints,comps,index)
    proposed=planned['display_rows']
    if (any(len({proposed[n] for n in names})!=1 for names in protected)
            or any(proposed[n]!=row for n,row in (fixed_rows or {}).items())
            or not old_bridges<={b['id'] for b in planned['bridge_alignment']}):
        model.update(status='skipped',applicable_ids=[],skip_reasons=model['skip_reasons']+
            [dict(reason='complete_group_resolve_broke_preserved_row_contract')])
        return readable,model
    model.update(row_plan=deepcopy(planned),target_rows=deepcopy(proposed),status='planned',
        rows_applied=proposed!=readable['display_rows'],
        changes=[dict(device=n,old=readable['display_rows'][n],new=proposed[n])
                 for n in sorted(proposed) if proposed[n]!=readable['display_rows'][n]])
    if not model['rows_applied']:return readable,model
    result=deepcopy(readable)
    result.update(branch_row_plan=deepcopy(planned),display_rows=deepcopy(proposed))
    bridges={frozenset(b['members']):b for b in planned['bridge_alignment']}
    for pattern in result['patterns']:
        if 'bridge_alignment' in pattern:
            pattern['bridge_alignment']=deepcopy(bridges[frozenset(pattern['members'])])
    result.pop('signature',None)
    result['signature']=sha256(json.dumps(dict(plan=result,devices=devices),sort_keys=True,
        separators=(',',':'),default=str).encode()).hexdigest()
    return result,model


def measure_stacks(pl,comps,index,model):
    applicable=set(model.get('applicable_ids',()))
    groups=[g for g in model.get('groups',()) if g['id'] in applicable]
    result=dict(version=VERSION,count=len(groups),recognized_count=len(model.get('groups',())),
        measurable=False,row_errors=None,row_distance=None,order_errors=None,
        pin_axis_error=None,records=[],missing=[],skip_reasons=deepcopy(model.get('skip_reasons',())))
    if not groups:return result
    try:
        contract=model['terminal_contract']
        if len(contract)!=len(comps) or any(comps[index[n]]['symbol']!=v['symbol']
                or comps[index[n]]['nodes']!=v['nodes'] for n,v in contract.items()):
            raise ValueError('repeated_stack_terminal_contract_changed')
        errors=0;distance=0.;orders=0;axis=0.
        for g in groups:
            stages=[]
            for names in g['same_row']:
                points=[pl.pos_of(index[n]) for n in names]
                if not all(isfinite(v) for point in points for v in point):raise ValueError('nonfinite_stack_position')
                ys=[p[1] for p in points]
                rows=[pl.cell_of[index[n]][0] for n in names]
                errors+=len(set(rows))>1;distance+=(max(ys)-min(ys))/100
                stages.append(dict(members=names,rows=rows,height_span=max(ys)-min(ys)))
            for path in g['anchor_outward_paths']:
                sign=-1 if g['symbol'] in ('nmos','npn') else 1
                for a,b in zip(path,path[1:]):
                    ia,ib=index[a],index[b];d,_,s=PINS[g['symbol']]
                    orders+=(pl.cell_of[ib][0]-pl.cell_of[ia][0])*sign<=0
                    axis+=abs(pl.pin_world(ia,d)[0]-pl.pin_world(ib,s)[0])
            result['records'].append(dict(id=g['id'],same_row=stages,
                branch_count=len(g['anchor_outward_paths']),ownership_claim=False))
        result.update(measurable=True,row_errors=errors,row_distance=distance,
            order_errors=orders,pin_axis_error=axis)
    except (KeyError,ValueError,TypeError,IndexError,AttributeError) as exc:
        result['missing'].append(dict(reason=str(exc)))
    return result


def stack_rank(measured):
    if not measured.get('count'):return ()
    fields=('order_errors','row_errors','row_distance')
    if not measured.get('measurable') or any(type(measured.get(k)) not in (int,float)
            or not isfinite(measured[k]) or measured[k]<0 for k in fields):return None
    return tuple(round(measured[k],8) for k in fields)
