"""Additional necessary packing only; use existing slots and geometry rules."""
from copy import deepcopy

from netlist2schematic.common.layout_budget import PackingBudget


EXHAUSTED = frozenset(('budget_exhausted', 'local_budget', 'candidate_time_budget'))


def supplement(par, core, comps, nets, index, plan, options, previous, limits, *, deadline=None):
    from .placement import pack
    report = deepcopy(previous)
    info = dict(limits, stage='normal_packing_supplement', attempted=False)
    if previous['stop_reason'] not in EXHAUSTED:
        info['stop_reason'] = 'search_exhausted'
        report['normal_packing_budget'] = info
        return None, report
    ledger = PackingBudget(limits['normal_seconds'], limits['normal_trials'], deadline)
    seconds, trials = ledger.allocate()
    if seconds <= 0 or trials <= 0:
        info.update(ledger.snapshot(ledger.stop_reason()))
        report['normal_packing_budget'] = info
        return None, report
    packed, attempt = pack(par, core, comps, nets, index, plan, options,
        deadline=ledger.deadline, seconds=seconds, max_trials=trials,
        skip_rejected_geometry=True, refine_terminal_placements=False)
    ledger.charge(attempt)
    ledger.attempts.append(dict(scale=1., allocated_seconds=seconds, allocated_trials=trials,
                               trials=attempt['trials'], stop_reason=attempt['stop_reason']))
    info.update(ledger.snapshot(ledger.stop_reason(attempt)), attempted=True)
    report = {**report, **attempt, 'normal_packing_budget': info,
              'legacy_packing_attempt': previous}
    return packed, report


def resume(par, core, comps, nets, index, plan, options, previous, limits, *, deadline):
    from .placement import pack
    from netlist2schematic.recovery.spacing_retry import _expanded_axis
    from netlist2schematic.layout.series_alignment import _fixed_ids
    from netlist2schematic.routing.orthogonal_router import RoutingFailure
    ledger = PackingBudget(limits['recovery_seconds'], limits['recovery_trials'], deadline)
    report = deepcopy(previous)
    for number, scale in enumerate((1., 1.25)):
        seconds, trials = ledger.allocate(2-number)
        if seconds <= 0 or trials <= 0:
            break
        trial = deepcopy(core)
        if scale != 1.:
            trial.column_x = _expanded_axis(core.column_x, scale, options.grid_step)
            trial.row_y = _expanded_axis(core.row_y, scale, options.grid_step)
            if any(trial.pos_of(i) != core.pos_of(i) for i in _fixed_ids(core, core.comps)):
                ledger.attempts.append(dict(scale=scale, status='skipped', reason='fixed_device_spacing'))
                continue
        attempt_deadline = min(ledger.deadline, ledger.clock()+seconds)
        packed, attempt = pack(par, trial, comps, nets, index, plan, options,
            deadline=attempt_deadline, seconds=seconds, max_trials=trials,
            skip_rejected_geometry=True, refine_terminal_placements=False)
        ledger.charge(attempt)
        ledger.attempts.append(dict(scale=scale, allocated_seconds=seconds,
            allocated_trials=trials, trials=attempt['trials'], stop_reason=attempt['stop_reason']))
        report = {**previous, **attempt}
        if packed is not None:
            report['recovery_packing_budget'] = dict(limits, **ledger.snapshot('completed'))
            report['primary_packing_resume'] = dict(scale=scale, no_sa_restart=True)
            return trial, packed, report
    info = dict(limits, **ledger.snapshot(ledger.stop_reason(report)))
    raise RoutingFailure('Attachment insertion recovery did not complete: '+info['stop_reason'],
        dict(reason='primary_packing_incomplete', layout_budget=info))
