"""Paired, port-proved regulation regions; parent references never merge leaves.

Main paths follow unique conduction edges. Folded forks, clocks, supplies and
shared bias alone cannot extend them. Placement uses existing columns/rows.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from itertools import combinations
from math import isfinite
from time import perf_counter

VERSION = 'paired-regulation-regions-v2'
PINS = {'nmos': ('D','G','S'), 'pmos': ('D','G','S'),
        'npn': ('C','B','E'), 'pnp': ('C','B','E')}
OPPOSITE = {'nmos':'pmos', 'pmos':'nmos', 'npn':'pnp', 'pnp':'npn'}
EPS = 1e-8


def compile_regions(devices, comps, structs, rows, *, formal_ports=()):
    result = dict(version=VERSION, groups=[], skipped=[])
    if len([c for c in comps if c['symbol'] in PINS]) < 8:
        return result
    from netlist2schematic.plugins.layout_shared.layout_evidence import build_layout_evidence
    evidence = build_layout_evidence(devices, structs, formal_ports=formal_ports)
    result['power_nets'] = sorted(n for n,row in evidence['net_roles'].items()
                                 if row.get('role')=='power')
    parts = {c['id']:c for c in comps}
    active = {n:c for n,c in parts.items() if c['symbol'] in PINS}
    incidence = defaultdict(list)
    for c in comps:
        for pin, net in c['nodes'].items(): incidence[net].append((c['id'],pin))
    power = lambda n: evidence['net_roles'].get(n,{}).get('role')=='power'
    conduction = {net:[(n,p) for n,p in users if n in active
        and p in (PINS[active[n]['symbol']][0],PINS[active[n]['symbol']][2])]
        for net,users in incidence.items()}

    def main_path(seed):
        path,edges,seen = [seed],[],set()
        current,exit_pin = seed,PINS[active[seed]['symbol']][0]
        while True:
            net=active[current]['nodes'][exit_pin]
            if power(net):return path,edges,None
            if net in seen:return path,edges,'cyclic_main_path'
            seen.add(net)
            ports=conduction.get(net,[])
            other=[(n,p) for n,p in ports if n!=current]
            if len(ports)!=2 or len(other)!=1:
                return path,edges,'main_path_conduction_fork_or_end'
            n,pin=other[0];d,g,s=PINS[active[n]['symbol']]
            # Non-active taps are real forks, not an exclusive series edge.
            if any(nm not in active for nm,_ in incidence[net]):
                return path,edges,'main_path_has_nonactive_tap'
            cd,cg,cs=PINS[active[current]['symbol']]
            same=active[n]['symbol']==active[current]['symbol']
            direct=(exit_pin==cd and pin==s) or (exit_pin==cs and pin==d)
            complementary=(exit_pin==cd and pin==d
                           and active[n]['symbol']==OPPOSITE[active[current]['symbol']])
            if not ((same and direct) or complementary) or n in path:
                return path,edges,'main_path_direction_ambiguous'
            edges.append(dict(a=current,pin_a=exit_pin,b=n,pin_b=pin,net=net))
            path.append(n);current=n;exit_pin=s if pin==d else d

    for relation in evidence['relations']:
        if relation['kind'] not in ('differential_pair','bjt_differential_pair'):
            continue
        names=relation['members']
        if len(names)!=2 or any(n not in active for n in names):continue
        paths=[];edges=[];regulators=[];loads=[];reason=None
        if active[names[0]]['symbol']!=active[names[1]]['symbol']:continue
        for seed in names:
            path,axes,error=main_path(seed)
            if error or len(path)<2:
                reason=error or 'no_main_extension';break
            follower=path[1]
            d,g,s=PINS[active[seed]['symbol']]
            if active[follower]['symbol']!=active[seed]['symbol']:
                reason='first_main_extension_is_not_same_polarity';break
            candidates=[n for n,c in active.items() if n not in path
                and c['symbol']==active[seed]['symbol']
                and c['nodes'][s]==active[seed]['nodes'][s]
                and c['nodes'][g]==active[seed]['nodes'][d]
                and c['nodes'][d]==active[follower]['nodes'][g]]
            if len(candidates)!=1:
                reason='regulating_return_not_unique';break
            reg=candidates[0];node=active[reg]['nodes'][d]
            possible=[]
            for n,pin in conduction.get(node,[]):
                c=active[n]
                if c['symbol']!=OPPOSITE[active[seed]['symbol']]:continue
                ld,lg,ls=PINS[c['symbol']]
                if pin==ld and power(c['nodes'][ls]):possible.append(n)
            if len(possible)!=1 or set(conduction.get(node,()))!={(reg,d),(possible[0],PINS[active[possible[0]]['symbol']][0])}:
                reason='regulator_load_or_return_fork';break
            if any(n not in active for n,_ in incidence[node]):
                reason='regulator_return_has_nonactive_tap';break
            paths.append(path);edges.extend(axes);regulators.append(reg);loads.append(possible[0])
        key='paired_regulation:'+'|'.join(sorted(names))
        if reason:
            result['skipped'].append(dict(id=key,reason=reason));continue
        if ([active[n]['symbol'] for n in paths[0]]!=[active[n]['symbol'] for n in paths[1]]
                or len(set(n for path in paths for n in path))!=sum(map(len,paths))):
            result['skipped'].append(dict(id=key,reason='main_paths_not_corresponding'));continue
        ld,lg,ls=PINS[active[loads[0]]['symbol']]
        if (loads[0]==loads[1] or active[loads[0]]['nodes'][lg]!=active[loads[1]]['nodes'][lg]
                or active[loads[0]]['nodes'][ls]!=active[loads[1]]['nodes'][ls]
                or power(active[loads[0]]['nodes'][lg])):
            result['skipped'].append(dict(id=key,reason='unmatched_load_controls_or_rails'));continue
        members=[n for p in paths for n in p]+regulators+loads
        if len(set(members))!=len(members):
            result['skipped'].append(dict(id=key,reason='overlapping_regulator_roles'));continue
        side_pairs=list(map(list,zip(*paths)))+[regulators,loads]
        row_errors=[p for p in side_pairs if rows[p[0]]!=rows[p[1]]]
        direction=-1 if active[names[0]]['symbol'] in ('nmos','npn') else 1
        for path in paths:
            if any((rows[b]-rows[a])*direction<=0 for a,b in zip(path,path[1:])):
                row_errors.append(path)
        feedback=[]
        for inp,path,reg,load in zip(names,paths,regulators,loads):
            d,g,s=PINS[active[inp]['symbol']];ld,lg,ls=PINS[active[load]['symbol']]
            feedback.extend([dict(a=inp,pin_a=d,b=reg,pin_b=g),
                dict(a=reg,pin_a=d,b=path[1],pin_b=g),dict(a=load,pin_a=ld,b=reg,pin_b=d)])
        result['groups'].append(dict(id=key,inputs=list(names),main_paths=paths,
            regulators=regulators,loads=loads,members=sorted(members),main_axes=edges,
            feedback_ports=feedback,corresponding_pairs=side_pairs,
            row_compatible=not row_errors,incompatible_row_relations=row_errors,
            main_row_direction=direction,
            leaves=[list(names),regulators,loads],parent_only=True,
            source_pair=relation['id'],shared_tail_members=deepcopy(relation['roles'].get('shared_tails',())),
            evidence='verified_input_pair_unique_conduction_stacks_and_two_unique_control_return_loops',
            terminal_contract={c['id']:dict(symbol=c['symbol'],nodes=deepcopy(c['nodes'])) for c in comps}))
    claims=Counter(n for g in result['groups'] for n in g['members'])
    overlaps=[g for g in result['groups'] if any(claims[n]>1 for n in g['members'])]
    result['skipped'].extend(dict(id=g['id'],reason='ambiguous_parent_membership') for g in overlaps)
    result['groups']=[g for g in result['groups'] if g not in overlaps]
    return result


def measure_regions(pl,comps,index,model):
    records=[];missing=[]
    for g in model.get('groups',()):
        try:
            contract=g['terminal_contract']
            if len(contract)!=len(comps) or any(comps[index[n]]['symbol']!=c['symbol']
                    or comps[index[n]]['nodes']!=c['nodes'] for n,c in contract.items()):
                raise ValueError('regulation_terminal_contract_changed')
            points={n:pl.pos_of(index[n]) for n in g['members']}
            if not all(isfinite(v) for p in points.values() for v in p):
                raise ValueError('nonfinite_region_position')
            pairs=g['corresponding_pairs']
            signs=[points[a][0]-points[b][0] for a,b in pairs]
            order=sum(s*signs[0]<-EPS for s in signs[1:])
            axis=sum(abs(pl.pin_world(index[e['a']],e['pin_a'])[0]
                         -pl.pin_world(index[e['b']],e['pin_b'])[0]) for e in g['main_axes'])
            feedback=sum(sum(abs(a-b) for a,b in zip(pl.pin_world(index[e['a']],e['pin_a']),
                pl.pin_world(index[e['b']],e['pin_b']))) for e in g['feedback_ports'])
            row_errors=sum(pl.cell_of[index[a]][0]!=pl.cell_of[index[b]][0] for a,b in pairs)
            row_errors+=sum((pl.cell_of[index[b]][0]-pl.cell_of[index[a]][0])
                *g['main_row_direction']<=0 for path in g['main_paths'] for a,b in zip(path,path[1:]))
            records.append(dict(id=g['id'],members=g['members'],order_errors=order,
                main_pin_error=axis,feedback_distance=feedback,
                region_width=max(p[0] for p in points.values())-min(p[0] for p in points.values()),
                row_errors=row_errors))
        except (KeyError,ValueError,TypeError,IndexError,AttributeError) as exc:
            missing.append(dict(id=g['id'],reason=str(exc)))
    usable=bool(records) and not missing
    fields=('order_errors','main_pin_error','feedback_distance','region_width','row_errors')
    return dict(version=VERSION,count=len(model.get('groups',())),measurable=usable,
        records=records,missing=missing,skipped=deepcopy(model.get('skipped',())),
        **{k:sum(r[k] for r in records) if usable else None for k in fields})


def region_rank(m):
    if not m.get('count'):return (0,)*4
    keys=('row_errors','order_errors','main_pin_error','feedback_distance')
    if not m.get('measurable') or any(type(m.get(k)) not in (int,float)
            or not isfinite(m[k]) or m[k]<0 for k in keys):return None
    return tuple(round(m[k],8) for k in keys)


def _wire_forecast(pl,comps,power_nets):
    """Project unchanged real pins to proposed columns; no scene or route call."""
    by_net=defaultdict(list)
    for i,c in enumerate(comps):
        for pin,net in c['nodes'].items():
            if net not in power_nets:
                by_net[net].append((i,tuple(pl.pin_world(i,pin))))
    columns=[cell[1] for cell in pl.cell_of]

    def estimate(target):
        shifts={i:pl.column_x[col]-pl.column_x[columns[i]] for i,col in target.items()}
        total=0.
        for values in by_net.values():
            if len(values)<2:continue
            xs=[p[0]+shifts.get(i,0.) for i,p in values];ys=[p[1] for _,p in values]
            total+=max(xs)-min(xs)+max(ys)-min(ys)
        return total
    return estimate


def region_trials(pl,comps,index,model,*,deadline=None,max_trials=16):
    """Rank a finite set of parent-column arrangements, never move other devices."""
    generated=0
    groups=model.get('groups',())
    forecast=_wire_forecast(pl,comps,set(model.get('power_nets',())))
    for group_number,g in enumerate(groups):
        group_limit=generated+max(0,(max_trials-generated)//(len(groups)-group_number))
        if group_limit<=generated:continue
        if not g['row_compatible']:continue
        if region_rank(measure_regions(pl,comps,index,dict(model,groups=[g]))) is None:continue
        ids={index[n] for n in g['members']}
        fixed=set(getattr(pl,'fixed',()))|set(getattr(pl,'fixed_cells',{}))
        if ids.intersection(fixed) or set(g['members']).intersection(fixed):continue
        if any(set(b)&ids and not set(b)<=ids for b in getattr(pl,'blocks',())):continue
        columns=range(len(pl.column_x))
        center=sum(pl.pos_of(i)[0] for i in ids)/len(ids)
        columns=sorted(sorted(columns,key=lambda c:(abs(pl.column_x[c]-center),c))[:10])
        candidates=[]
        # r/main/main/r, main/r/r/main and their local order alternatives.
        patterns=((1,2,0,3),(0,3,1,2),(1,3,0,2),(0,2,1,3))
        for cols in combinations(columns,4):
            if deadline is not None and perf_counter()>=deadline:return
            for pattern in patterns:
                for reverse in (False,True):
                    chosen=[cols[k] for k in pattern]
                    if reverse:chosen=[chosen[1],chosen[0],chosen[3],chosen[2]]
                    target={}
                    for side,path in enumerate(g['main_paths']):
                        target.update({index[n]:chosen[side] for n in path})
                        target[index[g['regulators'][side]]]=chosen[side+2]
                        target[index[g['loads'][side]]]=chosen[side+2]
                    occupied={(pl.cell_of[i][0],c) for i,c in target.items()}
                    if len(occupied)!=len(ids) or any(pl.comp_at.get(cell) not in (None,*ids) for cell in occupied):
                        continue
                    delta=sum(abs(pl.column_x[c]-pl.pos_of(i)[0]) for i,c in target.items())
                    proximity=sum(abs(pl.column_x[chosen[s]]-pl.column_x[chosen[s+2]]) for s in (0,1))
                    wire=forecast(target)
                    if isfinite(wire):
                        candidates.append(((wire,proximity,delta,tuple(chosen)),target))
        seen=set()
        for forecast_key,target in sorted(candidates,key=lambda v:v[0]):
            if generated>=group_limit:break
            key=tuple(sorted(target.items()))
            if key in seen:continue
            seen.add(key)
            for orient in (False,True):
                if generated>=group_limit:break
                if deadline is not None and perf_counter()>=deadline:return
                trial=deepcopy(pl);cells=list(trial.cell_of)
                for i,c in target.items():cells[i]=(cells[i][0],c)
                trial.set_state(cells,trial.orient)
                if orient:
                    targets={}
                    for side,(inp,path,r,l) in enumerate(zip(g['inputs'],g['main_paths'],g['regulators'],g['loads'])):
                        d,gate,s=PINS[comps[index[inp]]['symbol']]
                        targets[r]=(gate,trial.pin_world(index[inp],d)[0])
                        targets[path[1]]=(gate,trial.pin_world(index[r],d)[0])
                        ld,lg,ls=PINS[comps[index[l]]['symbol']]
                        targets[l]=(lg,trial.pos_of(index[g['loads'][1-side]])[0])
                    for name,(pin,x) in targets.items():
                        i=index[name];old=trial.orient[i];values=[]
                        for orientation in trial.allowed_orientations(i):
                            trial.orient[i]=orientation;p=trial.pin_world(i,pin)
                            direction={'west':-1,'east':1}.get(trial.pin_dir(i,pin),0)
                            values.append(((x-p[0])*direction<-EPS,abs(x-p[0]),orientation!=old,orientation))
                        trial.orient[i]=min(values)[-1]
                generated+=1
                yield trial,dict(kind='paired_regulation_columns',region=g['id'],
                    columns={comps[i]['id']:c for i,c in target.items()},control_facing=orient,
                    forecast_hpwl=forecast_key[0],forecast_stage='before_optional_control_facing',
                    forecast_scope='all_nonpower_visible_terminal_nets',
                    rows_unchanged=True,axes_unchanged=True)


def reserve_finish(plan,budget):
    if plan.get('regulation_regions',{}).get('groups'):
        budget['regulation_reserve']=min(16,max(0,budget['max_candidates']-budget['candidates']))


def finish_regions(par,current,comps,nets,index,plan,options,budget,until,report,*,hpwl_limit):
    model=plan.get('regulation_regions',{})
    if not model.get('groups'):return current
    from netlist2schematic.plugins.attachment.attachment_integration import (
        _checked_static,_fixed_world_safe,_order_errors,_has_budget,measure_attachments)
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    from netlist2schematic.plugins.attachment.attachment_budget import StageQuota
    before=measure_regions(current,comps,index,model)
    detail=dict(version=VERSION,before=before,applied=False,trials=0,
        state_rejections=0,hpwl_rejections=0,static_rejections=0,stop_reason='no_improvement',
        groups=[],actions=[],selection_order=['row_errors','order_errors','main_pin_error',
                                            'whole_estimated_wire','feedback_distance'])
    report['regulation_finish']=detail
    budget.pop('regulation_reserve',None)
    if not _has_budget(budget,until):
        detail.update(stop_reason='shared_budget',after=before)
        return current
    after=before
    groups=model['groups']
    for group_number,group in enumerate(groups):
        if not _has_budget(budget,until) or detail['trials']>=16:
            detail['stop_reason']='shared_budget';break
        remaining_groups=len(groups)-group_number
        allowance=max(0,(16-detail['trials'])//remaining_groups)
        local_model=dict(model,groups=[group])
        local_before=measure_regions(current,comps,index,local_model)
        baseline_rank=region_rank(local_before)
        old_attachments=measure_attachments(current,comps,index,plan)
        best=None
        local=dict(region=group['id'],before=local_before,trials=0,applied=False)
        detail['groups'].append(local)
        quota=StageQuota(budget,until,clock=perf_counter,name='paired_regulation_parent',
                         time_fraction=1/remaining_groups,max_candidates=allowance)
        with quota:
            for trial,action in region_trials(current,comps,index,local_model,
                    deadline=quota.deadline,max_trials=allowance):
                if not _has_budget(budget,quota.deadline):break
                budget['candidates']+=1;report['candidates']+=1;detail['trials']+=1;local['trials']+=1
                if not trial.valid_state() or not _fixed_world_safe(current,trial,index) or _order_errors(trial,comps,index,plan):
                    detail['state_rejections']+=1;continue
                measured=measure_regions(trial,comps,index,local_model);rank=region_rank(measured)
                if rank is None or baseline_rank is None or rank>=baseline_rank:continue
                attachments=measure_attachments(trial,comps,index,plan)
                if (not old_attachments.get('measurable') or not attachments.get('measurable')
                        or any(attachments[k]>old_attachments[k]+EPS for k in
                            ('violations','active_order_errors','shape_error','owner_distance'))
                        or attachments['embedded_members']<old_attachments['embedded_members']
                        or (old_attachments['source_count'] and any(attachments[k]>old_attachments[k]+EPS
                            for k in ('source_errors','source_distance')))):
                    detail['state_rejections']+=1;continue
                hpwl=_hpwl(par,trial,nets,index)
                if hpwl>hpwl_limit+EPS:
                    detail['hpwl_rejections']+=1;continue
                safe=_checked_static(par,trial,nets,index,options,quota.deadline)
                if safe is None:
                    budget['exhausted']=True;break
                if perf_counter()>=quota.deadline:break
                if not safe:detail['static_rejections']+=1;continue
                key=(rank[:3],hpwl,rank[3])
                if best is None or key<best[0]:
                    best=(key,trial,action,measured,measure_regions(trial,comps,index,model))
        local['budget']=quota.record
        if best is not None:
            _,current,action,local_after,after=best
            local.update(applied=True,action=action,after=local_after)
            detail['actions'].append(action);detail['action']=action
            detail.update(applied=True,stop_reason='best_valid_trial')
            report['accepted']+=1;report['actions'].append(action)
        else:local['after']=local_before
        if quota.record['status']=='shared_budget':
            detail['stop_reason']='shared_budget';break
    detail['after']=after
    return current
