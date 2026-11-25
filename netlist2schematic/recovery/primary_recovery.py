"""Recover saved candidate poses without restarting SA; callers own the budget."""
from copy import deepcopy
from time import perf_counter

from netlist2schematic.common.sa_common import load_local
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.sa_routing import RoutingValidationError, final_route
from netlist2schematic.recovery.candidate_rescue import progress

VERSION='primary-failure-recovery-v1'


def rank(candidates, *, candidate_ids=('2', '3')):
    def key(row):
        checkpoint=row.get('checkpoint')
        done=progress(row)['terminal_fraction'] if checkpoint is not None else 0.
        return (-done,row.get('kind')=='packing',row['candidate_id'], -row.get('attempt_index',0))
    return sorted((row for row in candidates if str(row['candidate_id']) in candidate_ids),key=key)


def context_for_recovery(candidates, *, candidate_ids=('2', '3')):
    """A reference pose cannot override the primary poses used for recovery."""
    return next((row for row in rank(candidates, candidate_ids=candidate_ids)
                 if row.get('context') is not None),None)


def _spacing_allowed(context, options, scale):
    from netlist2schematic.layout.series_alignment import _fixed_ids
    placement,comps,_,_,metadata=context
    fixed=_fixed_ids(placement,comps)
    if not fixed:return True
    from netlist2schematic.recovery.spacing_retry import _axis,_expanded_axis
    trial=deepcopy(placement)
    for attr,dimension in (('column_x',1),('row_y',0)):
        values=_axis(placement,metadata,attr,placement.cell_of,dimension)
        setattr(trial,attr,_expanded_axis(values,scale,options.grid_step))
    return all(trial.pos_of(i)==placement.pos_of(i) for i in fixed)


def repair(candidates, *, deadline, candidate_ids=('2', '3'), budget_source='unused_normal_budget', layout_budget=None):
    """Resume real runtime checkpoints; never read a historical result or rerun SA."""
    from netlist2schematic.flow.run_flow import _try_local_rescue
    from netlist2schematic.recovery.spacing_retry import retry_spacing
    from netlist2schematic.common.layout_budget import layout_budget as resolve_budget
    adaptive=resolve_budget(layout_budget).adaptive
    started=perf_counter()
    report=dict(version=VERSION,status='failed',budget_seconds=max(0.,deadline-started),
        candidate_order=[],attempts=[],source=budget_source,reference_enabled=False)
    runtime=deepcopy(candidates)
    queue=rank(runtime, candidate_ids=candidate_ids)
    # There is one recovery opportunity per primary candidate, not per copy
    # of its checkpoint. Later independent rescue can use any new safe scene.
    unique=[];seen=set()
    for row in queue:
        if row['candidate_id'] not in seen:
            unique.append(row);seen.add(row['candidate_id'])
    par=load_local('place_and_route','place-and-route.py')
    for position,row in enumerate(unique):
        remaining=deadline-perf_counter()
        if remaining<=0:break
        stop=min(deadline,perf_counter()+remaining/(len(unique)-position))
        ident=row['candidate_id'];report['candidate_order'].append(ident)
        context=deepcopy(row.get('context'));options=deepcopy(row.get('options'))
        local_stages=[]
        result=None
        if context is None and row.get('resume') is not None:
            item=dict(candidate_id=ident,action='resume_module_insertion',status='failed')
            report['attempts'].append(item)
            try:
                if adaptive and row.get('packing_budget'):
                    allowance=min(row['packing_budget']['recovery_seconds'],max(0.,stop-perf_counter())*.6)
                    item['packing_allocated_seconds']=allowance
                    context=row['resume'](min(stop,perf_counter()+allowance))
                else:
                    context=row['resume'](min(stop,perf_counter()+min(6.,max(0.,stop-perf_counter())*.35)))
                options=deepcopy(row['options'])
                item['status']='prepared'
                runtime.append(dict(candidate_id=ident,context=deepcopy(context),checkpoint=None,
                                    options=options,attempt_index=len(runtime)))
            except (RoutingFailure,RoutingValidationError) as exc:
                if exc.diagnostics.get('reason')=='power_connections':raise
                item.update(reason=exc.diagnostics.get('reason'),message=str(exc))
                if exc.diagnostics.get('layout_budget') is not None:
                    item['layout_budget']=deepcopy(exc.diagnostics['layout_budget'])
                continue
        if context is None:continue
        if row.get('checkpoint') is not None:
            first_end=min(stop,perf_counter()+max(0.,stop-perf_counter())*.5)
            result=_try_local_rescue(context,row['checkpoint'],options,'auto',
                max(0.,first_end-perf_counter()),local_stages,deadline=first_end,
                allow_module_channels=True)
            report['attempts'].append(dict(candidate_id=ident,action='local_repair',
                status='valid' if result is not None else 'failed',stages=local_stages))
        else:
            first_end=min(stop,perf_counter()+max(0.,stop-perf_counter())*.65)
            item=dict(candidate_id=ident,action='route_saved_primary_pose',status='failed')
            report['attempts'].append(item)
            try:
                placement,comps,nets,index,metadata=deepcopy(context)
                routed=final_route(par,nets,placement,index,options=options,deadline=first_end)
                from netlist2schematic.layout.layout_quality import evaluate_quality
                quality=evaluate_quality(par,placement,comps,nets,index,*routed[:2],metadata.get('structs',()))
                metadata['quality']=dict(score=quality['score'],details=quality,selected_candidate=ident)
                result=placement,comps,nets,index,metadata,routed
                item['status']='valid'
            except (RoutingFailure,RoutingValidationError) as exc:
                if exc.diagnostics.get('reason')=='power_connections':raise
                item.update(reason=exc.diagnostics.get('reason'),message=str(exc))
                runtime.append(dict(candidate_id=ident,context=deepcopy(context),
                    checkpoint=deepcopy(getattr(exc,'_rescue_context',None)),options=options,
                    attempt_index=len(runtime)))
        if result is None and perf_counter()<stop:
            item=dict(candidate_id=ident,action='spacing_primary_pose',scale=1.25,status='failed')
            report['attempts'].append(item)
            if not _spacing_allowed(context,options,1.25):
                item.update(status='skipped',reason='fixed_device_spacing')
                continue
            try:
                result=retry_spacing(context,routing_options=options,scale=1.25,
                    seconds=max(0.,stop-perf_counter()),deadline=stop)
                item['status']='valid'
            except (RoutingFailure,RoutingValidationError) as exc:
                if exc.diagnostics.get('reason')=='power_connections':raise
                item.update(reason=exc.diagnostics.get('reason'),message=str(exc))
        if result is not None and perf_counter()<=deadline:
            if not result[5][2].get('valid'):
                raise RoutingValidationError('Primary repair returned an unverified result',
                                             dict(reason='primary_repair_validation'))
            report.update(status='valid',selected_candidate=ident,stop_reason='first_verified_result',
                          elapsed_seconds=round(perf_counter()-started,6))
            result[4]['primary_repair']=deepcopy(report)
            result[4]['routing_stage']='primary_repair'
            result[5][2]['routing_stage']='primary_repair'
            result[4].setdefault('quality',{}).update(selected_candidate=ident,
                candidate_order=list(candidate_ids),candidate_policy='primary_2_3_then_repair',
                reference_fallback_only=False,reference_fallback_used=False,
                selection_policy='first_verified_primary_repair')
            return result,report,runtime
    report.update(stop_reason='time_budget' if perf_counter()>=deadline else 'primary_scenes_exhausted',
                  elapsed_seconds=round(perf_counter()-started,6))
    return None,report,runtime
