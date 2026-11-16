"""Bounded, post-SA placement of exclusive tail/feed devices.

MOS/BJT retain their rows and orientations. An explicit current source may
use a nearer existing row on its host's real S/E side. No axes are inserted.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from time import perf_counter

from netlist2schematic.plugins.attachment.attachment_sources import (
    measure_source_groups,source_group_anchor,source_group_trials,source_axis_trials)
from netlist2schematic.plugins.layout_shared.branch_alignment import _grid_offset
from netlist2schematic.plugins.preflight.placement_preflight import _wire_estimate
from netlist2schematic.plugins.symmetry.symmetry_layout import _static_safe
from netlist2schematic.plugins.region.region_layout import measure_regions
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.routing_rules import RoutingOptions

VERSION='exclusive-region-tails-v1'
ACTIVE=frozenset(('nmos','pmos','npn','pnp'))


def compile_tails(comps,nets,index,plan):
    groups,skipped,seen=[],[],set()
    rails=set(plan.get('power_interfaces',()))
    for junction in plan.get('junctions',()):
        if junction.get('kind') not in ('shared_tail','shared_feed'):
            continue
        members,tails=junction.get('members',()),junction.get('tail',())
        if (len(members)!=2 or len(set(members))!=2 or len(tails)!=1 or tails[0] in members
                or any(n not in index for n in [*members,*tails])):
            continue
        name=tails[0]
        comp=comps[index[name]]
        signal_pin=junction.get('tail_pins',{}).get(name,'D' if comp['symbol'] in ('nmos','pmos') else 'C')
        net=junction.get('net')
        hosts=[]
        for host in members:
            node=comps[index[host]]['nodes']
            pin='S' if 'S' in node else 'E'
            if node.get(pin)!=net:break
            hosts.append([host,pin])
        else:
            if (len(hosts)==2 and comp['nodes'].get(signal_pin)==net
                    and comp['symbol'] in ACTIVE|{'current-source'}
                    and set(map(tuple,nets.get(net,())))=={*map(tuple,hosts),(name,signal_pin)}):
                if comp['symbol']=='current-source':
                    rail_pins=[p for p,n in comp['nodes'].items() if n in rails and p!=signal_pin]
                else:
                    rail_pins=[p for p in ('S','E') if comp['nodes'].get(p) in rails]
                if len(rail_pins)==1:
                    rail_pin=rail_pins[0]
                    key=name,signal_pin,tuple(sorted(map(tuple,hosts))),net
                    if key in seen:continue
                    seen.add(key)
                    rail=comp['nodes'][rail_pin]
                    groups.append(dict(id='region-tail:'+name,kind='direct_tail',members=[name],
                        device=name,source_symbol=comp['symbol'],source_pins=deepcopy(comp['nodes']),
                        signal_pin=signal_pin,rail_pin=rail_pin,net=net,rail_net=rail,
                        attachment_net=net,host_ports=hosts,host_members=list(members),
                        path=[rail,net],passive_members=[]))
                    continue
        skipped.append(dict(source=name,reason='tail_is_not_exclusive_or_port_proven'))
    claims=defaultdict(list)
    for group in groups:claims[group['device']].append(group)
    ambiguous={name for name,rows in claims.items() if len(rows)>1}
    skipped.extend(dict(source=name,reason='ambiguous_tail_ownership') for name in sorted(ambiguous))
    return [g for g in groups if g['device'] not in ambiguous],skipped


def _fixed(pl,index):
    return {index.get(i,i) for i in set(getattr(pl,'fixed',()))|set(getattr(pl,'fixed_cells',{}))}


def _transistor_trials(pl,comps,index,group,step,deadline):
    i=index[group['device']]
    anchor=source_group_anchor(pl,comps,index,group)
    if not anchor:return
    row=pl.cell_of[i][0]
    pin=group['signal_pin']
    columns=sorted(range(len(pl.column_x)),key=lambda c:(abs(pl.column_x[c]-anchor['point'][0]),c))
    for col in columns:
        if perf_counter()>=deadline:return
        if pl.comp_at.get((row,col)) not in (None,i):continue
        trial=deepcopy(pl)
        cells=list(pl.cell_of);cells[i]=(row,col)
        trial.set_state(cells,trial.orient)
        trial.row_of=[r for r,_ in cells]
        trial.offset_x=list(getattr(pl,'offset_x',[0.0]*len(comps)))
        if cells[i]!=pl.cell_of[i]:trial.offset_x[i]=0
        desired=trial.offset_x[i]+anchor['point'][0]-trial.pin_world(i,pin)[0]
        dx=_grid_offset(trial,i,desired,step)
        if dx is None:continue
        trial.offset_x[i]=dx
        if trial.pos_of(i)==pl.pos_of(i):continue
        yield trial,dict(kind='transistor_tail_axis',source=group['device'],pin=pin)


def refine_region_tails(par,pl,comps,nets,index,metadata,*,options=None,deadline=None,budget=None):
    """Share the initializer's one-second / 128-trial allowance, keep backups."""
    started=perf_counter()
    result=deepcopy(metadata)
    saved=result.get('layout_regions',{})
    plan=saved.get('plan',{})
    groups,skipped=compile_tails(comps,nets,index,plan)
    if not groups or not saved.get('applied'):
        if saved.get('applied'):
            saved['tail_refinement']=dict(version=VERSION,applied=False,candidates=0,actions=[],
                skipped=skipped,applicability='not_applicable',stop_reason='no_exclusive_tail',
                elapsed_seconds=perf_counter()-started)
        return pl,result
    initial=saved.get('initialization',{})
    budget=deepcopy(budget if budget is not None else saved.get('local_budget',dict(
        seconds=initial.get('seconds_limit',1.0),max_candidates=initial.get('max_trials',128),
        elapsed_seconds=initial.get('elapsed_seconds',0),candidates=initial.get('trials',0))))
    remaining=max(0,budget['seconds']-budget['elapsed_seconds'])
    until=min(started+remaining,deadline) if deadline is not None else started+remaining
    report=dict(version=VERSION,applied=False,actions=[],candidates=0,skipped=skipped,
                static_rejections=0,estimate_rejections=0,
                budget_before=deepcopy(budget),
                stage='after_sa_and_feedback_before_astar',stop_reason='no_legal_improvement')
    current=pl
    options=options or RoutingOptions()
    fixed=_fixed(pl,index)
    for group in groups:
        i=index[group['device']]
        if i in fixed:
            report['skipped'].append(dict(source=group['device'],reason='explicitly_fixed'));continue
        if perf_counter()>=until or budget['candidates']>=budget['max_candidates']:
            report['stop_reason']='local_budget';break
        model={'groups':[group]}
        before=measure_source_groups(current,comps,nets,index,model)
        if not before['measurable']:continue
        organization=measure_regions(current,comps,index,plan)
        estimate=_wire_estimate(current,nets,index,par)
        if not organization.get('measurable'):continue
        if group['source_symbol']=='current-source':
            work=deepcopy(current)
            work.source_orientation_ids=set(getattr(work,'source_orientation_ids',()))|{i}
            if hasattr(work,'fixed_rows'):work.fixed_rows.pop(i,None)
            def proposals():
                yield from source_axis_trials(work,comps,index,model,deadline=until,grid_step=options.grid_step)
                for trial,action in source_group_trials(work,comps,nets,index,model,deadline=until,
                        max_candidates=min(128,max(0,budget['max_candidates']-budget['candidates']))):
                    yield trial,action
                    for centered,axis in source_axis_trials(trial,comps,index,model,deadline=until,
                                                           grid_step=options.grid_step):
                        yield centered,dict(action,axis_adjustment=axis)
        else:
            def proposals():
                yield from _transistor_trials(current,comps,index,group,options.grid_step,until)
        best=None
        for trial,action in proposals():
            if perf_counter()>=until or budget['candidates']>=budget['max_candidates']:
                report['stop_reason']='local_budget';break
            report['candidates']+=1;budget['candidates']+=1
            trial.row_of=[r for r,_ in trial.cell_of]
            if hasattr(trial,'fixed_rows'):trial.fixed_rows[i]=trial.cell_of[i][0]
            if (len(set(trial.cell_of))!=len(comps) or not trial.blocks_contiguous()
                    or hasattr(trial,'valid_state') and not trial.valid_state()):continue
            if any((trial.pos_of(j),trial.orient[j])!=(current.pos_of(j),current.orient[j])
                   for j in range(len(comps)) if j!=i):continue
            after=measure_source_groups(trial,comps,nets,index,model)
            if (not after['measurable'] or after['source_errors']>before['source_errors']
                    or after['source_distance']>=before['source_distance']-1e-8):continue
            measured=measure_regions(trial,comps,index,plan)
            fields=('interleaving','order_errors','pin_axis_error','axis_drift','dispersion')
            if (not measured.get('measurable') or any(measured[k]>organization[k]+1e-8 for k in fields)):continue
            trial_estimate=_wire_estimate(trial,nets,index,par)
            if trial_estimate>estimate*1.15+1e-8:
                report['estimate_rejections']+=1;continue
            try:
                if not _static_safe(par,trial,nets,index,options,until):
                    report['static_rejections']+=1;continue
            except RoutingFailure:
                report['stop_reason']='local_budget';break
            key=after['source_errors'],after['source_distance'],measured['junction_error'],trial_estimate,trial.pos_of(i)
            if best is None or key<best[0]:
                best=(key,trial,dict(action,before=before,after=after,
                    cell_before=list(current.cell_of[i]),cell_after=list(trial.cell_of[i]),
                    pin=group['signal_pin'],net=group['attachment_net']))
        if best:
            _,current,action=best
            report['actions'].append(action)
    report.update(applied=bool(report['actions']),elapsed_seconds=perf_counter()-started)
    if report['applied'] and report['stop_reason']=='no_legal_improvement':
        report['stop_reason']='improved'
    budget['elapsed_seconds']+=report['elapsed_seconds']
    saved.update(local_budget=budget,tail_refinement=report)
    if report['applied']:
        saved['metrics']=measure_regions(current,comps,index,plan)
        if isinstance(result.get('passive_layout'),dict) and hasattr(current,'fixed_rows'):
            result['passive_layout']['fixed_rows']={comps[i]['id']:row for i,row in current.fixed_rows.items()}
    return current,result
