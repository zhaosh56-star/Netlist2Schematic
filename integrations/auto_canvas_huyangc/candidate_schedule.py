"""Web-only ordering: try both normal candidates before sharing recovery time.

The engine's placement, routing, recovery and validation functions are called
unchanged. Runtime contexts stay private; only scalar reports leave this module.
"""
from copy import deepcopy
from time import perf_counter


POLICY = 'normal-2-3-then-shared-recovery-v1'
PRIMARY = ('2', '3')


def failure_rows(exc, identifier):
    rows = deepcopy(getattr(exc, '_primary_placements', None) or [])
    if not rows and getattr(exc, 'retry_context', None) is not None:
        rows = [dict(context=deepcopy(exc.retry_context),
                     checkpoint=deepcopy(getattr(exc, '_rescue_context', None)),
                     options=deepcopy(getattr(exc, 'retry_options', None)),
                     candidate_id=identifier, attempt_index=0)]
    rows = [row for row in rows if str(row.get('candidate_id')) == identifier]
    for row in rows:
        row['candidate_id'] = identifier
        row.setdefault('attempt_index', 0)
    return rows


def run_candidates(normal, *, seconds, check_source, on_failure, on_result,
                   clock=perf_counter, layout_budget=None):
    from netlist2schematic.flow import run_flow
    from netlist2schematic.flow.candidate_pipeline import FAILURES, _accept, _failed
    from netlist2schematic.recovery import primary_recovery
    from netlist2schematic.recovery.spacing_retry import retry_spacing

    started = clock()
    deadline = started + max(0., seconds)
    finishing_reserve = 6.
    usable = max(0., seconds - finishing_reserve)
    # Preserve the previous normal-stage fraction/cap, but reserve both normal
    # opportunities up front. Fast candidates leave their unused time behind.
    normal_seconds = min(120., usable / len(PRIMARY)) * .72
    records = [dict(candidate_id=identifier, status='failed', stages=[])
               for identifier in PRIMARY]
    by_id = {record['candidate_id']: record for record in records}
    report = dict(version=POLICY, normal_order=list(PRIMARY),
                  normal_seconds_per_candidate=normal_seconds,
                  finishing_reserve_seconds=finishing_reserve,
                  recovery_shared=True, recovery_attempted=False,
                  layout_budget=getattr(layout_budget,'mode',layout_budget or 'legacy'),
                  candidates=records, stages=[])
    runtime = []
    results = []

    def remaining(reserve=0.):
        return max(0., deadline - clock() - reserve)

    def append(stage, requested, allocated, began, details, identifier=None):
        item = dict(stage=stage, requested_seconds=requested,
                    allocated_seconds=allocated,
                    elapsed_seconds=round(clock()-began, 6), **details)
        if identifier is None:
            item.update(shared=True, candidate_ids=list(PRIMARY))
        else:
            item['candidate_id'] = identifier
            by_id[identifier]['stages'].append(deepcopy(item))
        report['stages'].append(item)

    def accept(result, stage):
        if result is None:
            return None
        metadata = result[4]
        identifier = str(metadata.get('candidate_id',
                         metadata.get('quality', {}).get('selected_candidate')))
        if identifier not in PRIMARY:
            raise ValueError('Web recovery returned a foreign candidate')
        result = _accept(result, identifier)
        record = by_id[identifier]
        record.update(status='valid', routing_stage=metadata.get('routing_stage', stage),
                      metrics=deepcopy(result[5][1]),
                      score=metadata.get('quality', {}).get('score'),
                      seed=metadata.get('candidate_seed'),
                      annealing=deepcopy(metadata.get('annealing', {})))
        if stage != 'normal':
            record.update(recovered_in_shared_stage=stage)
        on_result(result, identifier)
        results.append(result)
        return result

    def finish(reason):
        report.update(stop_reason=reason,
                      elapsed_seconds=round(clock()-started, 6))
        return results, report

    for offset, identifier in enumerate(PRIMARY):
        check_source()
        reserve = finishing_reserve + normal_seconds * (len(PRIMARY)-offset-1)
        allocated = min(normal_seconds, remaining(reserve))
        began = clock()
        if allocated <= 0:
            details = dict(status='skipped', reason='time_budget')
        else:
            print(f'Candidate {identifier}: normal budget={allocated:g}s', flush=True)
            try:
                result = _accept(normal(identifier, allocated), identifier)
            except FAILURES as exc:
                details = _failed(exc)
                rows = failure_rows(exc, identifier)
                runtime.extend(rows)
                # A saved safe draft must not consume the next normal slot.
                on_failure(rows, identifier, deadline=deadline-reserve)
            else:
                accept(result, 'normal')
                details = dict(status='valid', metrics=deepcopy(result[5][1]))
        append('normal', normal_seconds, allocated, began, details, identifier)
        by_id[identifier].update(normal_status=details['status'],
                                elapsed_seconds=round(clock()-began, 6))
        check_source()

    if results:
        # Even a failed candidate with a promising checkpoint cannot delay an
        # existing complete result with another recovery attempt.
        return finish('normal_result_available_recovery_skipped')
    if not runtime:
        return finish('no_failure_context')

    report['recovery_attempted'] = True
    report['recovery_budget_seconds'] = remaining(finishing_reserve)
    for position, stage in enumerate(('primary_repair', 'local_rescue', 'spacing_retry')):
        check_source()
        began = clock()
        # Unused time carries to subsequent recovery stages. These are shares
        # of one remaining deadline, not fresh per-candidate budgets.
        allocated = remaining(finishing_reserve) / (3-position)
        stage_deadline = min(deadline-finishing_reserve, clock()+allocated)
        result = None
        eligible = runtime
        if stage == 'local_rescue':
            eligible = [row for row in runtime if row.get('context') is not None
                        and row.get('checkpoint') is not None]
        if allocated <= 0 or not eligible:
            details = dict(status='skipped', reason=(
                'time_budget' if allocated <= 0 else 'no_failure_checkpoint'))
            append(stage, allocated, 0., began, details)
            continue
        print(f'Shared {stage}: budget={allocated:g}s', flush=True)
        try:
            if stage == 'primary_repair':
                adaptive=report['layout_budget']=='adaptive'
                before_count=len(runtime)
                capture_reserve=min(2., allocated*.1) if adaptive else 0.
                result, recovery, runtime = primary_recovery.repair(
                    deepcopy(runtime), deadline=stage_deadline-capture_reserve, candidate_ids=PRIMARY,
                    budget_source='web_shared_after_both_normal_failures',
                    **({'layout_budget':layout_budget} if layout_budget is not None else {}))
                if adaptive and result is None:
                    new_rows=[row for row in runtime[before_count:] if row.get('context') is not None]
                    order=[]
                    for row in primary_recovery.rank(new_rows,candidate_ids=PRIMARY):
                        if row['candidate_id'] not in order:order.append(row['candidate_id'])
                    for identifier in order:
                        on_failure([row for row in new_rows if row['candidate_id']==identifier],
                                   identifier,deadline=stage_deadline)
                details = dict(status='valid' if result is not None else 'failed',
                               primary_repair=deepcopy(recovery))
            elif stage == 'local_rescue':
                attempts = []
                result = run_flow._try_candidate_rescues(
                    deepcopy(eligible), 'auto', max(0., stage_deadline-clock()), attempts)
                details = dict(status='valid' if result is not None else 'failed',
                               attempts=deepcopy(attempts))
            else:
                attempts = []
                contexts = [primary_recovery.context_for_recovery(runtime, candidate_ids=(identifier,))
                            for identifier in PRIMARY]
                ranked = primary_recovery.rank([row for row in contexts if row is not None],
                                               candidate_ids=PRIMARY)
                for offset, row in enumerate(ranked):
                    check_source()
                    allowance = max(0., stage_deadline-clock()) / (len(ranked)-offset)
                    item = dict(candidate_id=row['candidate_id'], allocated_seconds=allowance)
                    attempts.append(item)
                    if allowance <= 0:
                        item.update(status='skipped', reason='time_budget')
                        continue
                    if not primary_recovery._spacing_allowed(row['context'], row['options'], 1.5):
                        item.update(status='skipped', reason='fixed_device_spacing')
                        continue
                    try:
                        result = _accept(retry_spacing(deepcopy(row['context']),
                            routing_options=row['options'], scale=1.5, seconds=allowance,
                            deadline=min(stage_deadline, clock()+allowance)), row['candidate_id'])
                    except FAILURES as exc:
                        item.update(_failed(exc))
                    else:
                        item.update(status='valid')
                        break
                details = dict(status='valid' if result is not None else 'failed', attempts=attempts)
        except FAILURES as exc:
            details = _failed(exc)
        check_source()
        if result is not None:
            accept(result, stage)
        append(stage, allocated, allocated, began, details)
        if result is not None:
            return finish('first_verified_shared_recovery')

    return finish('shared_recovery_exhausted')
