"""Final-cell alignment of exclusive pair tails within the existing ledger.

Keep the earlier source repair and grid proposals. Reserve, at most, one final
axis trial per tail from their existing opportunity; do not open a new budget.
"""
from math import isfinite
from time import perf_counter

from netlist2schematic.plugins.attachment.attachment_sources import (
    _fixed, _rail, source_axis_trials, source_group_anchor)

VERSION='tail-source-final-axis-v1'
EPS=1e-8
SOURCE_PIN={'nmos':'S','pmos':'S','npn':'E','pnp':'E'}


def tail_ports(comps,index,group):
    """Recheck the original exclusive-tail contract against current incidence."""
    try:
        name=group['device'];source=comps[index[name]]
        net=group['attachment_net'];pin=group['signal_pin']
        if (group['kind']!='direct_tail' or group['members']!=[name]
                or source['symbol']!='current-source' or source['nodes']!=group['source_pins']
                or source['nodes'].get(pin)!=net or _rail(net)
                or not _rail(source['nodes'].get(group['rail_pin']))):
            return None
        ports=[(c['id'],p) for c in comps for p,n in c['nodes'].items()
               if n==net and (c['id'],p)!=(name,pin)]
        if (len(ports)!=2 or len({n for n,p in ports})!=2
                or sorted(ports)!=sorted(map(tuple,group['host_ports']))):
            return None
        hosts=[comps[index[n]] for n,p in ports]
        if (hosts[0]['symbol']!=hosts[1]['symbol']
                or any(SOURCE_PIN.get(c['symbol'])!=p for c,(n,p) in zip(hosts,ports))):
            return None
        controls=[c['nodes'].get('G',c['nodes'].get('B')) for c in hosts]
        outputs=[c['nodes'].get('D',c['nodes'].get('C')) for c in hosts]
        if None in controls or None in outputs or len(set(controls))!=2 or len(set(outputs))!=2:
            return None
        return ports
    except (KeyError,IndexError,TypeError,ValueError):
        return None


def tail_anchor(pl,comps,index,group):
    if tail_ports(comps,index,group) is None:
        return None
    anchor=source_group_anchor(pl,comps,index,group)
    if anchor is None or anchor['preferred_direction'] not in ('north','south'):
        return None
    i=index[group['device']];point=pl.pin_world(i,group['signal_pin'])
    facing={'north':'south','south':'north'}[anchor['preferred_direction']]
    sign=1 if anchor['preferred_direction']=='south' else -1
    if (not all(isfinite(v) for v in point) or pl.pin_dir(i,group['signal_pin'])!=facing
            or any((point[1]-p[1])*sign<=EPS for p in anchor['host_points'])):
        return None
    return anchor


def reserve_finish(pl,comps,index,plan,budget,until):
    """Take a final trial only when at least two early opportunities remain."""
    from netlist2schematic.plugins.attachment import attachment_integration as ai
    if budget.get('tail_source_axis_finish_done') or budget.get('tail_source_axis_reserve'):
        return
    if not ai._has_budget(budget,until):
        return
    available=max(0,ai._candidate_limit(budget)-budget['candidates'])
    eligible=[g for g in plan.get('source_groups',())
        if tail_ports(comps,index,g) is not None and index[g['device']] not in _fixed(pl,index)]
    reserve=min(4,len(eligible),available//3)
    if reserve:
        budget['tail_source_axis_reserve']=reserve


def finish_axes(par,pl,comps,nets,index,plan,options,budget,until,report,*,hpwl_limit):
    """Run after grid/host/passive finishes, using their latest accepted state."""
    from netlist2schematic.plugins.attachment import attachment_integration as ai
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    stage=dict(version=VERSION,reserved_trials=budget.pop('tail_source_axis_reserve',0),
        trials=0,accepted=0,state_rejections=0,objective_rejections=0,
        hpwl_rejections=0,geometry_rejections=0,actions=[],skipped=[],
        stop_reason='not_applicable',shared_budget_reset=False)
    report['final_tail_axis']=stage
    budget['tail_source_axis_finish_done']=True
    groups=[g for g in plan.get('source_groups',()) if g['kind']=='direct_tail']
    if not groups:return pl
    current=pl
    if not ai._has_budget(budget,until):
        stage['stop_reason']='shared_attachment_budget';return current
    def order(g):
        a=tail_anchor(current,comps,index,g)
        error=abs(current.pin_world(index[g['device']],g['signal_pin'])[0]-a['point'][0]) if a else -1
        return (-error,tuple(a['point']) if a else (),g['id'])
    for group in sorted(groups,key=order):
        if not ai._has_budget(budget,until):
            stage['stop_reason']='shared_attachment_budget';return current
        if index[group['device']] in _fixed(current,index):
            stage['skipped'].append(dict(group=group['id'],reason='fixed_tail_source'));continue
        anchor=tail_anchor(current,comps,index,group)
        if anchor is None:
            stage['skipped'].append(dict(group=group['id'],reason='nonexclusive_or_nonfacing_tail'))
            continue
        model=dict(plan['source_model'],groups=[group])
        for trial,action in source_axis_trials(current,comps,index,model,deadline=until,
                grid_step=options.grid_step if options is not None else 2):
            if not ai._has_budget(budget,until):
                stage['stop_reason']='shared_attachment_budget';return current
            budget['candidates']+=1;report['candidates']+=1;stage['trials']+=1
            if not trial.valid_state() or not ai._fixed_world_safe(current,trial,index):
                stage['state_rejections']+=1;continue
            before=ai.measure_attachments(current,comps,index,plan)
            after=ai.measure_attachments(trial,comps,index,plan)
            if not ai._source_improves(before,after):
                stage['objective_rejections']+=1;continue
            old_wire=_hpwl(par,current,nets,index);new_wire=_hpwl(par,trial,nets,index)
            if new_wire>min(old_wire,hpwl_limit)+EPS:
                stage['hpwl_rejections']+=1;continue
            safe=ai._checked_static(par,trial,nets,index,options,until)
            if safe is None or perf_counter()>=until:
                budget['exhausted']=True;stage['stop_reason']='shared_attachment_budget';return current
            if not safe:
                stage['geometry_rejections']+=1;continue
            pin=group['signal_pin'];i=index[group['device']]
            action.update(kind='source_tail_axis_finish',version=VERSION,
                original_proposal='source_tail_axis',execution_stage='final_cell_after_hosts_and_passives',
                axis_error_before=abs(current.pin_world(i,pin)[0]-anchor['point'][0]),
                axis_error_after=abs(trial.pin_world(i,pin)[0]-anchor['point'][0]),
                source_distance_before=before['source_distance'],source_distance_after=after['source_distance'],
                estimated_wire_before=old_wire,estimated_wire_after=new_wire)
            current=trial;report['accepted']+=1;report['actions'].append(action)
            stage['accepted']+=1;stage['actions'].append(action)
    stage['stop_reason']='considered_final_tail_axes'
    return current
