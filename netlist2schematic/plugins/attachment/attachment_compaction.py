"""Reclaim a vacated attachment band after SA, without changing logical rows.

Only source-plan passive reservations between active rows qualify. Explicit
slots/channels, fixed world positions and the original static oracle remain
authoritative. This finish consumes the same ledger as attachment placement.
"""
from copy import deepcopy
from math import floor
from time import perf_counter

VERSION = 'vacated-attachment-bands-v1'
EPS = 1e-8


def potential_rows(plan):
    active = list(plan.get('active_rows', {}).values())
    if not active or plan.get('preserve_feedback_rows'):
        return []
    return sorted({r for r in plan.get('reserved_passive_rows', ())
                   if type(r) is int and min(active) < r < max(active)})


def reserve_finish(plan, budget):
    """Protect at most eight existing trials; no new seconds or candidates."""
    rows = potential_rows(plan)
    if not rows or budget.get('band_compaction_finish_done'):
        return
    reserve = min(8, 2*len(rows), budget['max_candidates']//16)
    budget['band_compaction_reserve'] = reserve
    budget['band_compaction_reserved_trials'] = reserve


def _eligible(pl, plan, metadata):
    potential = set(potential_rows(plan))
    occupied = set(pl.row_of)
    origins = getattr(pl, 'attachment_row_origin', tuple(range(len(pl.row_y))))
    active = [pl.cell_of[i][0] for i,c in enumerate(pl.comps)
              if c['symbol'] in ('npn','pnp','nmos','pmos')]
    protected = {r for rows in getattr(pl,'attachment_junction_rows',{}).values() for r in rows}
    bridge_rows = {plan['display_rows'][n] for g in plan.get('groups',())
                   if g.get('row_mode')=='conduction_bridge' for n in g['members']}
    protected.update(r for r,origin in enumerate(origins) if origin in bridge_rows)
    cuts = [metadata.get('feedback_row_cut'), (metadata.get('feedback') or {}).get('row_cut')]
    cuts = {cut for cut in cuts if type(cut) is int}
    eligible, skipped = [], []
    if not active:
        return eligible, skipped
    for row in range(min(active)+1, max(active)):
        if row in occupied or origins[row] not in potential:
            continue
        reason = ('protected_attachment_channel' if any(abs(row-r)<=1 for r in protected)
                  else 'feedback_channel' if cuts.intersection((row-1,row)) else None)
        if reason:
            skipped.append(dict(row=row, original_row=origins[row], reason=reason))
        else:
            eligible.append(row)
    eligible.sort(key=lambda r:(-(pl.row_y[r+1]-pl.row_y[r-1]),r))
    return eligible, skipped


def _trial(pl, row, delta, grid):
    trial = deepcopy(pl)
    trial.row_y = [y-delta if r>row else y for r,y in enumerate(pl.row_y)]
    origin = trial.row_y[0]
    midpoint = (trial.row_y[row-1]+trial.row_y[row+1])/2
    trial.row_y[row] = origin+round((midpoint-origin)/grid)*grid
    if not all(a+EPS<b for a,b in zip(trial.row_y,trial.row_y[1:])):
        return None
    # SA is over. Relock the new physical axes without changing logical row
    # constraints, slot identities, cells, orientations or any original plan.
    trial.attachment_axes = tuple(trial.column_x),tuple(trial.row_y)
    return trial


def finish_compaction(par, pl, comps, nets, index, plan, options, budget, until, report, metadata):
    from netlist2schematic.plugins.attachment import attachment_integration as ai
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    reserved = budget.pop('band_compaction_reserve',0)
    budget['band_compaction_finish_done'] = True
    rows, skipped = _eligible(pl,plan,metadata)
    detail = dict(version=VERSION, reserved_trials=reserved, eligible_rows=rows,
        skipped=skipped, trials=0, accepted=0, removed_height=0,
        before_axis=list(pl.row_y),after_axis=list(pl.row_y),
        logical_rows_unchanged=True, used_original_budget=True)
    report['band_compaction'] = detail
    if not rows:
        detail['stop_reason'] = 'no_eligible_vacated_band'
        return pl
    grid = options.grid_step if options is not None else 2
    current = pl
    stop = None
    for row in rows:
        if stop:
            break
        gaps = current.row_y[row]-current.row_y[row-1],current.row_y[row+1]-current.row_y[row]
        full = floor(min(min(gaps),sum(gaps)-2*grid)/grid)*grid
        deltas = sorted({d for d in (full,floor(full/2/grid)*grid) if d>=grid},reverse=True)
        baseline = ai.attachment_rank(ai.measure_attachments(current,comps,index,plan))
        hpwl = _hpwl(par,current,nets,index)
        for delta in deltas:
            if not ai._has_budget(budget,until) or detail['trials']>=8:
                stop = 'time_budget' if budget.get('exhausted') or perf_counter()>=until else 'candidate_budget'
                break
            budget['candidates'] += 1;report['candidates'] += 1;detail['trials'] += 1
            trial = _trial(current,row,delta,grid)
            reason = None
            if trial is None or not trial.valid_state() or not ai._fixed_world_safe(pl,trial,index):
                reason = 'state_or_fixed_world'
            else:
                rank = ai.attachment_rank(ai.measure_attachments(trial,comps,index,plan))
                if (baseline is None or rank is None or rank>baseline
                        or _hpwl(par,trial,nets,index)>hpwl+EPS):
                    reason = 'organization_or_span_regression'
            if reason:
                detail['skipped'].append(dict(row=row,delta=delta,reason=reason))
                continue
            safe = ai._checked_static(par,trial,nets,index,options,until)
            if safe is None or perf_counter()>=until:
                budget['exhausted'] = True;stop = 'time_budget'
                break
            if not safe:
                detail['skipped'].append(dict(row=row,delta=delta,reason='static_geometry'))
                continue
            action = dict(kind='compact_vacated_attachment_band',version=VERSION,
                row=row,original_row=current.attachment_row_origin[row],removed_height=delta,
                before_axis=list(current.row_y),after_axis=list(trial.row_y),
                hpwl_before=hpwl,hpwl_after=_hpwl(par,trial,nets,index),
                logical_rows_unchanged=True)
            current = trial;report['accepted'] += 1;report['actions'].append(action)
            detail['accepted'] += 1;detail['removed_height'] += delta
            break
    detail.update(after_axis=list(current.row_y),stop_reason=stop or 'considered_eligible_bands')
    return current
