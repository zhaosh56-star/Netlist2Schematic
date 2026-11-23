"""Repair an organized core before insertion and retry only exhausted packing.

The first packing attempt retains its existing order and local quota. A failed
attempt may reuse the same core once; no mixed SA, private state reconstruction,
or historical placement is involved. Both attempts obey the candidate deadline.
"""
from copy import deepcopy
from time import perf_counter

VERSION = 'module-packing-recovery-v1'


def prepare_and_pack(par, core, comps, nets, index, plan, options, branch_report, *,
                     local_deadline, candidate_deadline=None, max_trials=128):
    from netlist2schematic.plugins.organization.route_preparation import prepare
    from netlist2schematic.plugins.passive_skeleton.placement import pack

    names = {comp['id']: i for i, comp in enumerate(core.comps)}
    core_nets = {}
    for comp in core.comps:
        for pin, net in comp['nodes'].items():
            core_nets.setdefault(net, []).append((comp['id'], pin))
    core, prepared = prepare(par, core, core.comps, core_nets, names,
        {'layout_branches': deepcopy(branch_report)}, options,
        deadline=candidate_deadline, seconds=max(0., min(.25, local_deadline-perf_counter())))
    branch_report = deepcopy(branch_report)
    branch_report['core_preparation'] = deepcopy(prepared['layout_branches']['route_preparation'])
    first, report = pack(par, core, comps, nets, index, plan, options,
        deadline=local_deadline, seconds=max(0., local_deadline-perf_counter()),
        max_trials=max_trials)
    recovery = dict(version=VERSION, retried=False, core_repaired=branch_report['core_preparation']['applied'],
        initial_stop_reason=report['stop_reason'], initial_trials=report['trials'],
        initial_seconds=report['elapsed_seconds'], initial_actions=deepcopy(report['actions']),
        extra_seconds_limit=3., extra_trial_limit=512)
    report['packing_recovery'] = recovery
    if first is not None or report['stop_reason'] not in ('local_budget', 'budget_exhausted'):
        return core, first, report, branch_report
    now = perf_counter()
    stop = min(candidate_deadline, now+3.) if candidate_deadline is not None else now+3.
    if stop <= now:
        recovery['stop_reason'] = 'candidate_deadline'
        return core, None, report, branch_report
    # Retry from the actual runtime core, retaining all hierarchy/fixed guards.
    # A rejected static geometry need not be rechecked for every L-shaped guide.
    packed, retried = pack(par, core, comps, nets, index, plan, options,
        deadline=stop, seconds=stop-now, max_trials=512, skip_rejected_geometry=True)
    recovery.update(retried=True, retry_trials=retried['trials'],
        retry_seconds=retried['elapsed_seconds'], stop_reason=retried['stop_reason'],
        total_trials=report['trials']+retried['trials'],
        total_seconds=report['elapsed_seconds']+retried['elapsed_seconds'])
    retried['packing_recovery'] = recovery
    return core, packed, retried, branch_report
