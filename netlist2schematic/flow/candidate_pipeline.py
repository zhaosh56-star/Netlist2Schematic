"""Independent candidate budgets; recover each pose before comparing drawings."""
from __future__ import annotations

from copy import deepcopy
from time import perf_counter

from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.sa_routing import RoutingValidationError

POLICY = 'independent-candidates-2-3-then-1-v1'
NORMAL_SECONDS = 120.0
REPAIR_SECONDS = 30.0
RESCUE_SECONDS = 30.0
SPACING_SECONDS = 30.0
FAILURES = (RoutingFailure, RoutingValidationError)


def _accept(result, identifier):
    if result is None:
        return None
    if result[5][2].get('valid') is not True:
        raise RoutingValidationError('Candidate recovery returned unverified routes',
                                     {'reason': 'candidate_pipeline_validation'})
    actual = result[4].get('candidate_id', result[4].get('quality', {}).get('selected_candidate'))
    if str(actual) != identifier:
        raise ValueError('Candidate recovery returned a different candidate layout')
    return result


def _failed(exc):
    if exc.diagnostics.get('reason') == 'power_connections':
        raise exc
    return {'status': 'failed', 'message': str(exc),
            'diagnostics': deepcopy(exc.diagnostics)}


def _run_one(identifier, normal, *, budget, stages, check_source, normal_seconds,
             repair_seconds, rescue_seconds, spacing_seconds, spacing_scale,
             rescue_policy, fallback_policy, finishing_reserve):
    from netlist2schematic.flow import run_flow
    from netlist2schematic.recovery import primary_recovery
    from netlist2schematic.recovery.spacing_retry import retry_spacing

    started = perf_counter()
    record = dict(candidate_id=identifier, status='failed', stages=[])
    rows = []
    result = None

    def allocate(stage, requested):
        return budget.allocate(f'candidate_{identifier}.{stage}', requested,
                               reserve=finishing_reserve)

    def append(stage, requested, allocated, began, details):
        item = dict(stage=stage, candidate_id=identifier, requested_seconds=requested,
                    allocated_seconds=allocated, elapsed_seconds=round(perf_counter()-began, 6),
                    **details)
        record['stages'].append(deepcopy(item))
        stages.append(item)

    check_source()
    seconds = allocate('normal', normal_seconds)
    began = perf_counter()
    if seconds > 0:
        print(f'Candidate {identifier}: normal budget={seconds:g}s', flush=True)
        try:
            result = _accept(normal(identifier, seconds), identifier)
        except FAILURES as exc:
            result = None
            details = _failed(exc)
            rows = deepcopy(getattr(exc, '_primary_placements', None) or [])
            if not rows and getattr(exc, 'retry_context', None) is not None:
                rows = [dict(candidate_id=identifier, context=deepcopy(exc.retry_context),
                             checkpoint=deepcopy(getattr(exc, '_rescue_context', None)),
                             options=deepcopy(getattr(exc, 'retry_options', None)), attempt_index=0)]
            rows = [row for row in rows if str(row['candidate_id']) == identifier]
        else:
            details = dict(status='valid', metrics=deepcopy(result[5][1]))
    else:
        details = dict(status='skipped', reason='time_budget')
    append('normal', normal_seconds, seconds, began, details)
    check_source()

    if result is None:
        began = perf_counter()
        seconds = allocate('primary_repair', repair_seconds) if rows and repair_seconds > 0 else 0.
        if seconds > 0:
            print(f'Candidate {identifier}: primary repair budget={seconds:g}s', flush=True)
            try:
                result, report, rows = primary_recovery.repair(
                    rows, deadline=perf_counter()+seconds, candidate_ids=(identifier,),
                    budget_source='independent_candidate_stage')
                result = _accept(result, identifier)
            except FAILURES as exc:
                result = None
                details = _failed(exc)
            else:
                details = dict(status='valid' if result is not None else 'failed', primary_repair=report)
        else:
            details = dict(status='skipped', reason=('no_failure_context' if not rows else
                                                    'disabled' if repair_seconds == 0 else 'time_budget'))
        append('primary_repair', repair_seconds, seconds, began, details)
        check_source()

    if result is None:
        began = perf_counter()
        checkpoints = [row for row in rows if row.get('context') is not None
                       and row.get('checkpoint') is not None]
        seconds = (allocate('local_rescue', rescue_seconds)
                   if checkpoints and rescue_policy == 'auto' and rescue_seconds > 0 else 0.)
        if seconds > 0:
            local_stages = []
            try:
                result = run_flow._try_candidate_rescues(checkpoints, 'auto', seconds, local_stages)
                result = _accept(result, identifier)
            except FAILURES as exc:
                result = None
                details = dict(_failed(exc), attempts=local_stages)
            else:
                details = dict(status='valid' if result is not None else 'failed', attempts=local_stages)
        else:
            details = dict(status='skipped', reason=(
                'disabled' if rescue_policy == 'off' or rescue_seconds == 0 else
                'no_failure_checkpoint' if not checkpoints else 'time_budget'))
        append('local_rescue', rescue_seconds, seconds, began, details)
        check_source()

    if result is None:
        began = perf_counter()
        chosen = primary_recovery.context_for_recovery(rows, candidate_ids=(identifier,))
        seconds = (allocate('spacing_retry', spacing_seconds)
                   if chosen is not None and fallback_policy == 'auto' and spacing_seconds > 0 else 0.)
        if seconds > 0 and not primary_recovery._spacing_allowed(chosen['context'], chosen['options'], spacing_scale):
            details = dict(status='skipped', reason='fixed_device_spacing')
        elif seconds > 0:
            print(f'Candidate {identifier}: spacing retry x{spacing_scale:g}, budget={seconds:g}s', flush=True)
            try:
                result = _accept(retry_spacing(deepcopy(chosen['context']),
                    routing_options=chosen['options'], scale=spacing_scale, seconds=seconds,
                    deadline=perf_counter()+seconds), identifier)
            except FAILURES as exc:
                details = _failed(exc)
            else:
                details = dict(status='valid', metrics=deepcopy(result[5][1]))
        else:
            details = dict(status='skipped', reason=(
                'disabled' if fallback_policy != 'auto' or spacing_seconds == 0 else
                'no_attempted_layout' if chosen is None else 'time_budget'))
        append('spacing_retry', spacing_seconds, seconds, began, details)
        check_source()

    record['elapsed_seconds'] = round(perf_counter()-started, 6)
    if result is not None:
        record.update(status='valid', routing_stage=result[4].get('routing_stage', 'normal'),
                      metrics=deepcopy(result[5][1]), score=result[4].get('quality', {}).get('score'),
                      seed=result[4].get('candidate_seed'), annealing=deepcopy(result[4].get('annealing', {})))
    return result, record


def run_candidates(source, par, normal, *, budget, stages, check_source, cell, seed, options,
                   normal_seconds=NORMAL_SECONDS, repair_seconds=REPAIR_SECONDS,
                   rescue_seconds=RESCUE_SECONDS, spacing_seconds=SPACING_SECONDS,
                   spacing_scale=1.5, fallback_seconds=60, fallback_policy='auto',
                   rescue_policy='auto', finishing_reserve=10, simplify_policy='safe',
                   simplify_seconds=5, route_tradeoff='strict', branch_layout='off'):
    """Always finish both primary pipelines; candidate 1 and bus are failure-only."""
    from netlist2schematic.flow import run_flow
    started = perf_counter()
    records, results = [], []
    report = dict(version=POLICY, primary_candidates=['2', '3'],
                  candidate_order=[], reference_fallback_only=True,
                  stage_caps=dict(normal=normal_seconds, primary_repair=repair_seconds,
                                  local_rescue=rescue_seconds, spacing_retry=spacing_seconds),
                  candidates=records)
    for identifier in ('2', '3', '1'):
        if identifier == '1' and (results or fallback_policy != 'auto'):
            break
        report['candidate_order'].append(identifier)
        result, record = _run_one(identifier, normal, budget=budget, stages=stages,
            check_source=check_source, normal_seconds=normal_seconds, repair_seconds=repair_seconds,
            rescue_seconds=rescue_seconds, spacing_seconds=spacing_seconds, spacing_scale=spacing_scale,
            rescue_policy=rescue_policy, fallback_policy=fallback_policy,
            finishing_reserve=finishing_reserve)
        records.append(record)
        if result is not None:
            metadata = result[4]
            quality = deepcopy(metadata['quality']['details'])
            key = (quality['score'], result[5][1]['crossings'], result[5][1]['wirelength'], identifier)
            results.append(deepcopy((key, *result[:5], result[5], quality, identifier)))

    if not results:
        if fallback_policy != 'auto':
            raise RoutingFailure('No candidate pipeline produced a verified drawing',
                                 dict(reason='candidate_pipelines_failed', candidate_pipeline=deepcopy(report)))
        check_source()
        seconds = budget.allocate('bus_fallback', fallback_seconds, reserve=finishing_reserve)
        if seconds <= 0:
            raise RoutingFailure('No time remains for the bus fallback',
                dict(reason='time_budget', candidate_pipeline=deepcopy(report), stages=deepcopy(stages)))
        try:
            result = run_flow._bus_result(source, cell, seed, options, seconds, stages,
                                          trigger='all_candidate_pipelines_failed')
        except FAILURES as exc:
            exc.diagnostics['candidate_pipeline'] = deepcopy(report)
            exc.diagnostics['stages'] = deepcopy(stages)
            raise
        check_source()
        report.update(selected_candidate=None, selected_stage='bus_fallback',
                      stop_reason='all_candidates_failed_bus_verified',
                      elapsed_seconds=round(perf_counter()-started, 6))
        result[4]['candidate_pipeline'] = deepcopy(report)
        return result

    # Cleanup is shared and performed before selection, so the winner is based
    # on the same final wires that will be saved, rather than stale SA scores.
    from netlist2schematic.flow.aesthetic_flow import _clean_primary_results
    from netlist2schematic.flow.candidate_selection import select_result
    from netlist2schematic.routing.routing_rules import RoutingOptions
    results, cleanup = _clean_primary_results(source, cell, par, results, records,
        options or RoutingOptions(), simplify_policy, simplify_seconds, route_tradeoff,
        budget_limit=budget.remaining(reserve=5.))
    if branch_layout == 'auto':
        from netlist2schematic.plugins.organization.branch_layout import select_results
        selected, selection = select_results(results)
    else:
        selected, selection = select_result(results, stage='after_shared_route_simplification')
    _, placement, comps, nets, index, metadata, routed, quality, identifier = selected
    report.update(selected_candidate=identifier, selected_stage=metadata.get('routing_stage', 'normal'),
                  reference_fallback_used=identifier == '1', selection=deepcopy(selection),
                  stop_reason='verified_candidates_compared',
                  elapsed_seconds=round(perf_counter()-started, 6))
    metadata['candidate_pipeline'] = deepcopy(report)
    metadata.setdefault('quality', {}).update(
        candidate_policy=POLICY, candidate_order=report['candidate_order'],
        reference_fallback_only=True, reference_fallback_used=identifier == '1',
        selected_candidate=identifier, score=quality['score'], details=deepcopy(quality),
        candidates=deepcopy(records), selection_policy=selection['policy'],
        routing_selection=deepcopy(selection), comparison_stage='after_shared_route_simplification',
        candidate_cleanup=deepcopy(cleanup), score_usage='diagnostics_and_layout_feedback_only')
    check_source()
    return placement, comps, nets, index, metadata, routed
