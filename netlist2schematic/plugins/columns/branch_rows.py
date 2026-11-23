"""Align corresponding *display* tiers before initializing structured SA.

Only complete, disjoint conduction columns may translate. A shorter local
root can follow a stronger column through a confirmed pair or repeated port
roles; a common gate alone is insufficient. No electrical edge, component,
orientation, physical offset, route, score or random stream is changed here.
"""
from collections import defaultdict
from copy import deepcopy
from itertools import combinations
from math import isfinite
from time import perf_counter
from types import SimpleNamespace

from netlist2schematic.plugins.columns.column_layout import build_column_plan, _ports
from netlist2schematic.layout.series_alignment import CHANNELS, CONTROLS
from netlist2schematic.plugins.layout_shared.branch_levels import _conduction_orders

VERSION = 'corresponding-branch-rows-v3'


def compatible_rows(report, placement, index):
    """Feedback/recovery may keep a plan only on its saved active rows."""
    return all(n in index and placement.cell_of[index[n]][0] == row
               for n, row in report.get('active_rows_after', {}).items())


def _relations(comps, structures, columns):
    """Typed mirror evidence and at least two distinct repeated controls."""
    active = {c['id']: c for c in comps if c['symbol'] in CHANNELS}
    result, skipped, seen = [], [], {}

    def add(a, b, kind, evidence):
        if a not in active or b not in active or a == b:
            return
        if active[a]['symbol'] != active[b]['symbol']:
            return
        key = frozenset((a, b))
        if key not in seen:
            seen[key] = dict(a=a, b=b, kind=kind, kinds=[kind], evidence=[evidence])
            result.append(seen[key])
        else:
            if kind not in seen[key]['kinds']:
                seen[key]['kinds'].append(kind)
            if evidence not in seen[key]['evidence']:
                seen[key]['evidence'].append(evidence)

    bias = defaultdict(list)
    for name, comp in active.items():
        _, source = CHANNELS[comp['symbol']]
        bias[comp['symbol'], comp['nodes'][source], comp['nodes'][CONTROLS[comp['symbol']]]].append(name)
    for (_, source, control), names in bias.items():
        refs = [n for n in names if active[n]['nodes'][CHANNELS[active[n]['symbol']][0]] == control]
        outputs = [n for n in names if n not in refs]
        if len(refs) == 1:
            for out in outputs:
                add(refs[0], out, 'current_mirror', 'unique_diode_reference_common_source_and_control')
        elif len(refs) > 1 and outputs:
            skipped.append(dict(members=sorted(names), reason='ambiguous_mirror_reference'))
    for record in structures:
        if record.get('confidence', 'high') != 'high':
            continue
        roles = record.get('roles', {})
        if record['kind'] in ('differential_pair', 'bjt_differential_pair', 'cross_coupled'):
            a, b = roles.get('left'), roles.get('right')
            if a in active and b in active:
                _, source = CHANNELS[active[a]['symbol']]
                if active[a]['nodes'][source] == active[b]['nodes'].get(source):
                    add(a, b, record['kind'], 'confirmed_same_polarity_source_roles')
    for cohort in columns['corresponding_paths']:
        for first, second in combinations(cohort, 2):
            if set(first) & set(second):
                continue
            for a, b in zip(first, second):
                add(a, b, 'corresponding_stack', 'disjoint_stack_with_multiple_distinct_control_roles')
    return result, skipped


def plan_branch_correspondence(par, devices, comps, nets, index, row_of, structures=(), *,
                               bridges=(), fixed=(), deadline=None, estimate_rows=None,
                               max_estimates=32, estimate_seconds=.5):
    """Return new row preferences and auditable transactions; inputs stay intact.

    Preserve the longer column, supply-connected heads and fixed members.
    Same-rank transactions are checked together, rejecting all contributors
    to a conflict instead of selecting one by instance name or input order.
    """
    started = perf_counter()
    if (type(max_estimates) is not int or max_estimates < 0
            or type(estimate_seconds) not in (int, float)
            or not isfinite(estimate_seconds) or estimate_seconds < 0):
        raise ValueError('Row estimate budget must be finite and non-negative')
    if estimate_rows is not None and not callable(estimate_rows):
        raise ValueError('Row estimator must be callable')
    if deadline is not None and (type(deadline) not in (int, float) or not isfinite(deadline)):
        raise ValueError('Branch row deadline must be finite')
    if (set(devices) != set(index) or len(row_of) != len(comps)
            or any(index.get(c['id']) != i for i, c in enumerate(comps))
            or any(type(r) is not int or r < 0 for r in row_of)):
        raise ValueError('Branch rows must cover the original indexed devices with non-negative integer rows')
    rows = {c['id']: row_of[i] for i, c in enumerate(comps)}
    current = dict(rows)
    report = dict(version=VERSION, policy='auto', phase='before_sa', applied=False,
        rows_before=dict(rows), rows_after=dict(rows), actions=[], skipped=[], relations=[],
        electrical_edges_added=[], columns=[], strict_orders=[], preserved_same_rows=[])
    search = dict(policy='minimum_existing_SA_estimate', enabled=estimate_rows is not None,
        activated=False, seconds_limit=estimate_seconds, max_estimates=max_estimates,
        evaluated_states=0, elapsed_seconds=0., evaluations=[], trials=[], rounds=0)
    report['target_search'] = search
    cost_cache = {}
    cost_started = cost_deadline = None

    def finish(reason):
        report.update(stop_reason=reason, rows_after=dict(current),
                      active_rows_after={n: current[n] for n in active},
                      elapsed_seconds=round(perf_counter()-started, 6))
        report['applied'] = current != rows
        report['before_error'] = sum(abs(rows[r['a']]-rows[r['b']]) for r in report['relations'])
        report['after_error'] = sum(abs(current[r['a']]-current[r['b']]) for r in report['relations'])
        return [current[c['id']] for c in comps], report

    active = {c['id']: c for c in comps if c['symbol'] in CHANNELS}
    if len(active) < 2:
        return finish('no_active_correspondence')
    if deadline is not None and perf_counter() >= deadline:
        return finish('deadline')
    bridges = tuple(bridges)
    placement = SimpleNamespace(cell_of=[(r, 0) for r in row_of])
    plan = build_column_plan(par, placement, comps, nets, index, structures, inline_bridges=bridges)
    columns = plan['columns']
    owner = {n: k for k, column in enumerate(columns) for n in column['members']}
    orders, accepted, rejected, sccs = _conduction_orders(devices, bridges)
    report['strict_orders'] = deepcopy(orders)
    report['skipped_bridges'] = deepcopy(rejected)
    relations, skipped = _relations(comps, structures, plan)
    report['relations'], report['skipped'] = relations, skipped
    mirror_outputs = defaultdict(set)
    for relation in relations:
        if relation['kinds'] == ['current_mirror']:
            mirror_outputs[relation['a']].add(relation['b'])
    if any(rows[o['upper']] >= rows[o['lower']] for o in orders):
        return finish('original_order_inconsistent')
    fixed_ids = {comps[n]['id'] if isinstance(n, int) else n for n in fixed}
    cyclic = {n for scc in sccs for n in scc['devices']}
    powered = {net for c in comps for net in c['nodes'].values() if par.power_marker_sym(net)}
    # Existing, accepted R/L supply continuations remain supply anchors.
    while True:
        extended = powered | {b['end'] for b in accepted if not b.get('layout_only') and b['start'] in powered}
        if extended == powered:
            break
        powered = extended
    ranks, locked = {}, {}
    for k, column in enumerate(columns):
        members = column['members']; root = members[0]
        inlet, _ = _ports(active[root])
        supplied = active[root]['nodes'][inlet] in powered
        ranks[k] = (len(members), int(supplied))
        reason = ('fixed_member' if set(members) & fixed_ids else
                  'supply_connected_column' if supplied else
                  'original_conduction_cycle' if set(members) & cyclic else
                  'not_a_local_branch_root' if any(o['lower'] == root and o['upper'] not in members for o in orders)
                  else None)
        if reason:
            locked[k] = reason
        report['columns'].append(dict(members=list(members), rank=list(ranks[k]), locked_reason=reason))

    # Converging/forked conduction ports intentionally end a display column.
    # A retained output can therefore be a singleton although its strict
    # upstream orders place it in an established branch. That context anchors
    # a unique diode reference without merging or moving the output columns.
    junction_outputs = {name: {n for n in outputs
        if locked.get(owner[n]) == 'not_a_local_branch_root'}
        for name, outputs in mirror_outputs.items()}
    report['junction_mirror_anchors'] = [dict(reference=name, output=n,
        upstream=sorted(o['upper'] for o in orders if o['lower'] == n),
        evidence='unique_mirror_reference_with_retained_conduction_context')
        for name in sorted(junction_outputs) for n in sorted(junction_outputs[name])]
    soft_references = set()
    if estimate_rows is not None:
        for name, outputs in mirror_outputs.items():
            k = owner[name]
            anchors = [n for n in outputs if ranks[owner[n]] > ranks[k] or owner[n] in locked]
            if (k not in locked and len({rows[n] for n in anchors}) > 1
                    and (any(ranks[owner[n]] > ranks[k] for n in anchors)
                         or bool(junction_outputs[name]))):
                soft_references.add(name)
    report['soft_mirror_rows'] = [dict(reference=n, outputs=sorted(mirror_outputs[n]))
                                  for n in sorted(soft_references)]
    preserved = set()
    for record in structures:
        if record.get('confidence', 'high') == 'high':
            # Only the conflicting multi-output mirror preference is soft.
            # Differential/latch/stack constraints involving the same members
            # through other records keep their existing hard protection.
            if (record['kind'] == 'current_mirror'
                    and record.get('roles', {}).get('reference') in soft_references):
                continue
            for members in record.get('layout', {}).get('align_rows', ()):
                for a, b in combinations(members, 2):
                    if a in active and b in active and rows[a] == rows[b]:
                        preserved.add(tuple(sorted((a, b))))
    preserved.update(tuple(sorted((r['a'], r['b']))) for r in relations
        if rows[r['a']] == rows[r['b']]
        and not (r['a'] in soft_references and r['kinds'] == ['current_mirror']))
    report['preserved_same_rows'] = [list(p) for p in sorted(preserved)]

    def hard_conflict(trial):
        if any(value < 0 for value in trial.values()):
            return 'negative_display_row'
        if any(trial[o['upper']] >= trial[o['lower']] for o in orders):
            return 'conduction_order_conflict'
        if any(trial[a] != trial[b] for a, b in preserved):
            return 'existing_same_row_conflict'
        return None

    def evaluate(trial):
        from netlist2schematic.plugins.columns.row_estimate import RowEstimateUnavailable
        key = tuple(trial[c['id']] for c in comps)
        if key in cost_cache:
            return cost_cache[key]
        if perf_counter() >= cost_deadline or search['evaluated_states'] >= max_estimates:
            raise RowEstimateUnavailable('local_budget')
        entry = dict(id=search['evaluated_states'], rows=dict(trial))
        search['evaluations'].append(entry)
        search['evaluated_states'] += 1
        before = perf_counter()
        try:
            value, details = estimate_rows(dict(trial), deadline=cost_deadline)
            if type(value) not in (int, float) or not isfinite(value) or not isinstance(details, dict):
                raise ValueError('Row estimator must return a finite cost and metric object')
            if perf_counter() >= cost_deadline:
                raise RowEstimateUnavailable('local_time_budget')
            entry.update(status='estimated', cost=value, metrics=details)
        except RowEstimateUnavailable as error:
            entry.update(status='unavailable', reason=str(error))
        entry['elapsed_seconds'] = round(perf_counter()-before, 6)
        cost_cache[key] = entry
        return entry

    def resolve_targets(choices):
        """Choose complete, improving row states; never commit half a trial."""
        nonlocal current, cost_started, cost_deadline
        from netlist2schematic.plugins.columns.row_estimate import RowEstimateUnavailable
        if cost_started is None:
            cost_started = perf_counter()
            cost_deadline = cost_started + estimate_seconds
            if deadline is not None:
                cost_deadline = min(deadline, cost_deadline)
            search['activated'] = True
        pending = list(choices)
        try:
            while pending:
                incumbent = evaluate(current)
                if incumbent['status'] != 'estimated':
                    search['stop_reason'] = 'incumbent_estimate_unavailable'
                    break
                improving = []
                for choice in pending:
                    names = columns[choice['column']]['members']
                    for delta in sorted(choice['deltas'], key=lambda d:(abs(d), d)):
                        if not delta:
                            continue
                        trial = dict(current)
                        trial.update({n:choice['base'][n]+delta for n in names})
                        attempt = dict(members=list(names), delta=delta, accepted=False)
                        search['trials'].append(attempt)
                        reason = hard_conflict(trial)
                        if reason:
                            attempt.update(status='hard_constraint_rejected', reason=reason)
                            continue
                        estimate = evaluate(trial)
                        attempt.update(status=estimate['status'], estimate_id=estimate['id'])
                        if estimate['status'] != 'estimated':
                            continue
                        attempt['cost'] = estimate['cost']
                        if estimate['cost'] < incumbent['cost']-1e-9:
                            movement = sum(abs(trial[n]-rows[n]) for n in trial)
                            improving.append((estimate['cost'], movement, choice, trial, attempt))
                # A deadline during generation/evaluation discards this whole
                # incomplete selection round, keeping earlier accepted states.
                if perf_counter() >= cost_deadline:
                    raise RowEstimateUnavailable('local_time_budget')
                search['rounds'] += 1
                if not improving:
                    search['stop_reason'] = 'current_rows_have_lowest_estimated_cost'
                    break
                best_cost = min(item[0] for item in improving)
                best = [item for item in improving if abs(item[0]-best_cost) <= 1e-9]
                movement = min(item[1] for item in best)
                best = [item for item in best if item[1] == movement]
                if len(best) != 1:
                    search['stop_reason'] = 'equal_cost_target_ambiguity'
                    break
                cost, _, choice, trial, attempt = best[0]
                names = columns[choice['column']]['members']
                report['actions'].append(dict(members=list(names), delta=attempt['delta'],
                    before={n:current[n] for n in names}, after={n:trial[n] for n in names},
                    hints=choice['hints'], selection='minimum_estimated_cost',
                    reference_cost=incumbent['cost'], selected_cost=cost,
                    selected_estimate=attempt['estimate_id']))
                current = trial
                attempt['accepted'] = True
                pending.remove(choice)
                search['stop_reason'] = 'minimum_estimated_cost_selected'
        except RowEstimateUnavailable as error:
            search['stop_reason'] = str(error)
        search['elapsed_seconds'] = round(perf_counter()-cost_started, 6)
        for choice in pending:
            report['skipped'].append(dict(members=columns[choice['column']]['members'],
                reason=search['stop_reason'], hints=choice['hints']))

    for rank in sorted(set(ranks.values()), reverse=True):
        if deadline is not None and perf_counter() >= deadline:
            return finish('deadline')
        proposals = {}
        choices = []
        for k, column in enumerate(columns):
            if ranks[k] != rank or k in locked:
                continue
            members = set(column['members']); hints = []; stronger_anchor = False
            junction_anchor = False
            for relation in relations:
                a, b = relation['a'], relation['b']
                if (a in members) == (b in members):
                    continue
                moving, anchor = (a, b) if a in members else (b, a)
                stronger = ranks[owner[anchor]] > rank
                # A locked tail/control branch contributes a target even
                # when its local column is short. Conflicting mirror targets
                # are estimated below; neither role silently disappears.
                if stronger or owner[anchor] in locked:
                    hints.append(dict(member=moving, anchor=anchor, kind=relation['kind'],
                                      delta=current[anchor]-current[moving],
                                      reference_member=(relation['kinds'] == ['current_mirror']
                                                        and relation['a'] == moving),
                                      anchor_basis='stronger_column' if stronger else locked[owner[anchor]]))
                    stronger_anchor |= stronger
                    junction_anchor |= (relation['kinds'] == ['current_mirror']
                        and relation['a'] == moving
                        and anchor in junction_outputs.get(moving, ()))
            if not (stronger_anchor or junction_anchor):
                continue
            deltas = {h['delta'] for h in hints}
            if len(deltas) != 1 or (junction_anchor and not stronger_anchor):
                if estimate_rows is not None and all(h['reference_member'] for h in hints):
                    choices.append(dict(column=k, base={n:current[n] for n in members},
                                        deltas=deltas | {0}, hints=hints))
                    continue
                report['skipped'].append(dict(members=sorted(members),
                    reason='ambiguous_target_rows' if len(deltas) != 1 else 'junction_target_requires_estimate',
                    hints=hints))
                continue
            delta = next(iter(deltas))
            if not delta:
                continue
            if min(current[n]+delta for n in members) < 0:
                report['skipped'].append(dict(members=sorted(members), reason='negative_display_row', hints=hints))
                continue
            proposals[k] = dict(delta=delta, hints=hints)
        # Commit a rank together. Removing a rejected transaction can expose a
        # second conflict; recompute until every remaining transaction is safe.
        while proposals:
            trial = dict(current)
            for k, proposal in proposals.items():
                for n in columns[k]['members']:
                    trial[n] += proposal['delta']
            bad = {}
            for order in orders:
                a, b = order['upper'], order['lower']
                if trial[a] >= trial[b]:
                    for k in {owner[a], owner[b]} & proposals.keys():
                        bad[k] = 'conduction_order_conflict'
            for a, b in sorted(preserved):
                if trial[a] != trial[b]:
                    for k in {owner[a], owner[b]} & proposals.keys():
                        bad.setdefault(k, 'existing_same_row_conflict')
            if not bad:
                for k, proposal in proposals.items():
                    names = columns[k]['members']
                    report['actions'].append(dict(members=list(names), delta=proposal['delta'],
                        before={n: current[n] for n in names}, after={n: trial[n] for n in names},
                        hints=proposal['hints']))
                current = trial
                break
            for k, reason in bad.items():
                report['skipped'].append(dict(members=columns[k]['members'], reason=reason,
                                              hints=proposals[k]['hints']))
                del proposals[k]
        if choices:
            resolve_targets(choices)
    return finish('aligned' if current != rows else 'no_safe_change')
