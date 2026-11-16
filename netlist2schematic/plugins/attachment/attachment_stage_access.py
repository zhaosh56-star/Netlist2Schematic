"""Choose whole-cell control access with the existing static L-escape estimate.

Only an already proved neutral cell may be mirrored. Conduction organization,
logical cells, physical axes, fixed members and original route rules remain
authoritative. This pass is outside SA and never performs route search.
"""
from copy import deepcopy
from time import perf_counter

from netlist2schematic.plugins.attachment.attachment_stages import measure_stages,stage_rank,stage_owners
from netlist2schematic.plugins.readability.readability_routing import make_initializer_cost
from netlist2schematic.routing.orthogonal_router import RoutingFailure

VERSION='neutral-cell-control-access-v1'
EPS=1e-8


def mirror_trials(current,comps,index,model):
    """Use geometric order; model/instance spelling does not select a cell."""
    from netlist2schematic.plugins.attachment.attachment_integration import _fixed_ids
    fixed=_fixed_ids(current,index)
    def geometry(cell):
        return tuple(sorted((tuple(current.pos_of(index[n])),comps[index[n]]['symbol'])
                            for n in cell['members']))
    for cell in sorted(model.get('stages',()),key=geometry):
        ids=[index[n] for n in cell['members']]
        if fixed.intersection(ids):continue
        orientations=list(current.orient)
        for i in ids:
            orientations[i]=1-current.orient[i]
            if orientations[i] not in current.allowed_orientations(i):break
        else:
            trial=deepcopy(current)
            if not stage_owners(trial,comps,index,model):continue
            trial.set_state(list(current.cell_of),orientations)
            yield trial,dict(kind='neutral_stage_layout',operation='control_access_mirror',
                members=list(cell['members']),new_connections=False,
                cells_unchanged=True,axes_unchanged=True,rows_unchanged=True)


def improve_access(par,current,comps,nets,index,plan,options,budget,until,report,*,
                   initialization,hpwl_limit,max_trials=8):
    """Consume the same ledger and preserve each fully checked incumbent."""
    from netlist2schematic.plugins.attachment.attachment_integration import (
        _checked_static,_fixed_world_safe,_has_budget)
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    model=plan.get('conduction_stages',{})
    detail=dict(version=VERSION,applied=False,trials=0,accepted=0,actions=[],
        max_trials=max_trials,static_rejections=0,state_rejections=0,
        hpwl_rejections=0,stop_reason='not_applicable',
        scope='existing static control escape estimate; not a routability proof')
    if not model.get('global_layout_eligible'):return current,detail
    if max_trials<=0 or not _has_budget(budget,until):
        detail['stop_reason']='shared_budget';return current,detail
    invariant=stage_rank(measure_stages(current,comps,index,model))
    if invariant is None:
        detail['stop_reason']='unmeasurable_cells';return current,detail
    cost,audit=make_initializer_cost(par,current,comps,nets,index,plan['readability_plan'],
        options=options,deadline=until)
    detail['estimator']=audit
    if cost is None:
        detail['stop_reason']=audit.get('reason','unavailable_estimate');return current,detail
    before=cost.peek(current);value=before['cost']
    detail.update(before=before,after=before,stop_reason='no_improvement')
    seen={tuple(current.orient)}
    while detail['trials']<max_trials and _has_budget(budget,until):
        best=None;stopped=False;generated=False
        for trial,action in mirror_trials(current,comps,index,model):
            if detail['trials']>=max_trials or not _has_budget(budget,until):
                detail['stop_reason']='shared_budget' if not _has_budget(budget,until) else 'trial_limit'
                stopped=True;break
            key=tuple(trial.orient)
            if key in seen:continue
            seen.add(key);generated=True
            detail['trials']+=1;budget['candidates']+=1
            report['candidate_trials' if initialization else 'candidates']+=1
            rank=stage_rank(measure_stages(trial,comps,index,model))
            if (not trial.valid_state() or not _fixed_world_safe(current,trial,index)
                    or rank is None or rank>invariant):
                detail['state_rejections']+=1;continue
            hpwl=_hpwl(par,trial,nets,index)
            if hpwl>hpwl_limit+EPS:detail['hpwl_rejections']+=1;continue
            try:
                estimate=cost(trial)
            except RoutingFailure as exc:
                detail['stop_reason']=exc.diagnostics.get('reason','estimate_stopped')
                stopped=True;break
            if estimate>=value-EPS:continue
            safe=_checked_static(par,trial,nets,index,options,until)
            if safe is None or perf_counter()>=until:
                detail['stop_reason']='shared_budget';stopped=True;break
            if not safe:detail['static_rejections']+=1;continue
            # The estimate covers every frozen control relation, including
            # nets crossing cell boundaries. Keep coordinates as the tie-break.
            order=(estimate,rank,tuple(sorted((tuple(trial.pos_of(i)),trial.orient[i]) for i in range(len(comps)))))
            if best is None or order<best[0]:
                best=(order,trial,action,cost.peek(trial))
        if best is not None:
            _,current,action,measured=best;value=measured['cost']
            detail.update(applied=True,after=measured)
            detail['accepted']+=1;detail['actions'].append(action);report['actions'].append(action)
            if not initialization:report['accepted']+=1
        if stopped:break
        if not generated or best is None:
            detail['stop_reason']='no_improvement';break
    if detail['trials']>=max_trials:detail['stop_reason']='trial_limit'
    elif not _has_budget(budget,until):detail['stop_reason']='shared_budget'
    return current,detail
