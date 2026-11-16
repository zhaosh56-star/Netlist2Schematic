"""Keep proven repeated stacks on real, unique conduction lanes.

Movement bundles are not semantic owners. Supply, bias and control interfaces
never extend a lane; unverified forks and non-monotone lanes are skipped.
Verified folded branches keep side-access inputs separate, and corresponding
same-row leaves retain their original ownership during atomic lane moves.
All trials use the existing fixed axes, state guard and shared local budget.
"""
from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
from itertools import combinations
from math import isfinite
from time import perf_counter

from netlist2schematic.plugins.attachment.attachment_stacks import PINS

VERSION = 'repeated-stack-columns-v2'
EPS = 1e-8


def _folded_continuations(comps, plan):
    """Keep a verified bias/fold axis while its input remains side access.

    This is narrower than following every terminal at a fork. The calibrated
    folded contract must identify the input, fold and bias on both branches.
    Additional drain-only shunts may share the bias supply; another source,
    an unknown port or a different supply leaves the continuation ambiguous.
    """
    by_id = {c['id']: c for c in comps}
    junctions = plan['readability_plan']['junctions']
    axes, cohorts = [], []
    for group in plan.get('folded_cores', {}).get('groups', ()):
        try:
            roles = [group[k] for k in ('inputs', 'folded', 'bias')]
            if any(len(v) != 2 for v in roles) or len(set(sum(roles, []))) != 6:
                continue
            contract = group['terminal_contract']
            if any(by_id[n]['nodes'] != c for n, c in contract.items()):
                continue
            if not set(sum(roles, [])) <= set(contract):
                continue
            evidence = group['evidence']
            if evidence['kind'] not in ('validated_input_child_and_two_supply_fed_fold_junctions',
                    'validated_input_and_exclusive_RL_rail_fold_junctions'):
                continue
            branch_ports = {(p['input'], p['folded'], p['bias'], p['junction'])
                            for p in evidence['branch_ports']}
            proposed = []
            for inp, fold, bias in zip(*roles):
                ci, cf, cb = (by_id[n] for n in (inp, fold, bias))
                if ({ci['symbol'], cf['symbol']} != {'nmos', 'pmos'}
                        or cf['symbol'] != cb['symbol']):
                    break
                net = ci['nodes']['D']
                if (cf['nodes']['S'] != net or cb['nodes']['D'] != net
                        or (inp, fold, bias, net) not in branch_ports):
                    break
                junction = junctions[net]
                actual = {tuple(p) for p in junction['conduction_ports']}
                main = {(inp, 'D'), (fold, 'S'), (bias, 'D')}
                if junction['is_rail'] or not main <= actual:
                    break
                extras = actual-main
                if any(pin != 'D' or by_id[n]['symbol'] != cb['symbol']
                       or by_id[n]['nodes']['S'] != cb['nodes']['S'] for n, pin in extras):
                    break
                proposed.append(dict(a=bias, pin_a='D', b=fold, pin_b='S', net=net,
                    evidence='verified_folded_main_branch', core=group['id'],
                    side_access=[list(p) for p in sorted(actual-{(bias, 'D'), (fold, 'S')})]))
            else:
                axes.extend(proposed)
                cohorts.extend(dict(members=list(v), evidence='verified_folded_leaf', core=group['id'])
                               for v in roles)
        except (KeyError, TypeError, ValueError):
            continue
    return axes, cohorts


def compile_columns(comps, plan, owners, structs):
    repeated = plan.get('repeated_stacks', {})
    result = dict(version=VERSION, banks=[], lanes=[], skipped=[],
        terminal_contract=deepcopy(repeated.get('terminal_contract', {})),
        power_nets=sorted(n for n, j in plan['readability_plan']['junctions'].items() if j['is_rail']))
    applicable = set(repeated.get('applicable_ids', ()))
    if not applicable:
        return result
    by_id = {c['id']: c for c in comps}
    junctions = plan['readability_plan']['junctions']
    rows = plan['display_rows']
    folded_axes, folded_cohorts = _folded_continuations(comps, plan)
    folded_keys = {(a['net'], frozenset(((a['a'], a['pin_a']), (a['b'], a['pin_b'])))) for a in folded_axes}
    adjacent = defaultdict(set)
    edges = []
    seen_axes = set()
    for axis in list(plan.get('host_axes', ()))+folded_axes:
        a, b = axis['a'], axis['b']
        key = frozenset(((a, axis['pin_a']), (b, axis['pin_b'])))
        if key in seen_axes:
            continue
        j = junctions.get(axis['net'], {})
        actual = {tuple(p) for p in j.get('conduction_ports', ())}
        if (j.get('is_rail', True)
                or ((axis['net'], key) not in folded_keys and (len(actual) != 2
                    or actual != {(a, axis['pin_a']), (b, axis['pin_b'])}))
                or any(by_id[n]['symbol'] not in PINS for n in (a, b))):
            continue
        seen_axes.add(key)
        adjacent[a].add(b); adjacent[b].add(a)
        edges.append(deepcopy(axis))
    lanes, membership, seen = {}, {}, set()
    for name in sorted(adjacent):
        if name in seen:
            continue
        todo, members = [name], set()
        while todo:
            n = todo.pop()
            if n in members:
                continue
            members.add(n); todo.extend(adjacent[n] - members)
        seen.update(members)
        own_edges = [a for a in edges if a['a'] in members and a['b'] in members]
        if len(own_edges) != len(members)-1 or any(len(adjacent[n]) > 2 for n in members):
            result['skipped'].append(dict(members=sorted(members), reason='non_unique_conduction_path'))
            continue
        ordered = sorted(members, key=lambda n: (rows[n], n))
        if (len({rows[n] for n in members}) != len(members)
                or any(b not in adjacent[a] for a, b in zip(ordered, ordered[1:]))):
            result['skipped'].append(dict(members=ordered, reason='non_monotone_display_path'))
            continue
        identifier = 'stack_lane:' + sha256('|'.join(sorted(members)).encode()).hexdigest()[:16]
        lane = dict(id=identifier, members=ordered, edges=own_edges,
            rows={n: rows[n] for n in ordered}, ownership_claim=False)
        lanes[identifier] = lane
        membership.update({n: identifier for n in members})
    mirrors = [set(s['devices']) for s in structs
        if s['kind'] == 'current_mirror' and s.get('confidence') == 'high']
    for bank in repeated.get('groups', ()):
        if bank['id'] not in applicable:
            continue
        legs = bank['anchor_outward_paths']
        ids = [membership.get(p[0]) for p in legs]
        reason = None
        if any(k is None for k in ids) or len(set(ids)) != len(ids):
            reason = 'missing_or_shared_conduction_lane'
        elif any(any(membership.get(n) != k for n in path) for k, path in zip(ids, legs)):
            reason = 'prefix_leaves_unique_lane'
        elif any(len({rows[n] for n in stage}) != 1 for stage in bank['same_row']):
            reason = 'display_row_cohort_not_preserved'
        if reason:
            result['skipped'].append(dict(bank=bank['id'], reason=reason)); continue
        members = {n for k in ids for n in lanes[k]['members']}
        # A same-row corresponding leaf can span lanes without being split:
        # move all lanes atomically, retaining its original row and ownership.
        # Mixed-row, partial and unrelated owners remain protected as before.
        cohorts = [dict(members=stage, evidence='confirmed_repeated_stage') for stage in bank['same_row']]
        cohorts += folded_cohorts
        cohorts += [dict(members=p['members'], evidence='verified_input_pair')
                    for p in plan.get('input_blocks', {}).get('pairs', ())]
        retained_cohorts = []
        for owner in map(set, owners):
            if not owner.intersection(members):
                continue
            if owner <= members and len({membership[n] for n in owner}) == 1:
                continue
            if any(owner <= mirror for mirror in mirrors) and len({rows[n] for n in owner}) == 1:
                continue
            proof = next((c for c in cohorts if owner == set(c['members'])), None)
            if (proof is not None and owner <= members and len({rows[n] for n in owner}) == 1
                    and len(owner) == len(ids) and {membership[n] for n in owner} == set(ids)):
                retained_cohorts.append(deepcopy(proof))
                continue
            reason = 'protected_owner_crosses_lane_boundary'; break
        if reason:
            result['skipped'].append(dict(bank=bank['id'], reason=reason)); continue
        result['banks'].append(dict(id=bank['id'], lane_ids=ids,
            roots=[p[0] for p in legs], same_row=deepcopy(bank['same_row']),
            members=sorted(members), ownership_claim=False,
            retained_row_cohorts=retained_cohorts,
            evidence='confirmed_repeated_prefix_plus_unique_conduction_port_continuations'))
    used = {k for b in result['banks'] for k in b['lane_ids']}
    result['lanes'] = [lanes[k] for k in sorted(used)]
    return result


def measure_columns(pl, comps, index, model):
    banks = model.get('banks', ())
    result = dict(version=VERSION, count=len(banks), lane_count=len(model.get('lanes', ())),
        measurable=False, row_errors=None, order_errors=None, pin_axis_error=None,
        lane_spread=None, records=[], missing=[], skipped=deepcopy(model.get('skipped', ())))
    if not banks:
        return result
    try:
        contract = model['terminal_contract']
        if len(contract) != len(comps) or any(comps[index[n]]['symbol'] != c['symbol']
                or comps[index[n]]['nodes'] != c['nodes'] for n, c in contract.items()):
            raise ValueError('stack_column_terminal_contract_changed')
        axis, spread, row_errors, orders = 0., 0., 0, 0
        for lane in model['lanes']:
            xs = [pl.pos_of(index[n])[0] for n in lane['members']]
            if not all(isfinite(v) for n in lane['members'] for v in pl.pos_of(index[n])):
                raise ValueError('nonfinite_stack_position')
            row_errors += sum(pl.cell_of[index[n]][0] != r for n, r in lane['rows'].items())
            error = sum(abs(pl.pin_world(index[a['a']], a['pin_a'])[0]
                            - pl.pin_world(index[a['b']], a['pin_b'])[0]) for a in lane['edges'])
            if not isfinite(error):
                raise ValueError('nonfinite_stack_pin')
            axis += error; spread += max(xs)-min(xs)
            result['records'].append(dict(id=lane['id'], members=lane['members'],
                pin_axis_error=error, column_span=max(xs)-min(xs)))
        for bank in banks:
            root_x = [pl.pos_of(index[n])[0] for n in bank['roots']]
            if len(set(root_x)) != len(root_x):
                raise ValueError('ambiguous_stack_root_order')
            for stage in bank['same_row']:
                for i in range(len(stage)):
                    for j in range(i+1, len(stage)):
                        dx = pl.pos_of(index[stage[i]])[0]-pl.pos_of(index[stage[j]])[0]
                        orders += (root_x[i]-root_x[j])*dx <= 0
        result.update(measurable=True, row_errors=row_errors, order_errors=orders,
            pin_axis_error=axis, lane_spread=spread)
    except (KeyError, ValueError, TypeError, IndexError, AttributeError) as exc:
        result['missing'].append(dict(reason=str(exc)))
    return result


def column_rank(measured):
    if not measured.get('count'):
        return ()
    fields = ('row_errors', 'order_errors', 'pin_axis_error', 'lane_spread')
    if not measured.get('measurable') or any(type(measured.get(k)) not in (int, float)
            or not isfinite(measured[k]) or measured[k] < 0 for k in fields):
        return None
    return tuple(round(measured[k], 8) for k in fields)


def _protect(pl, index, model, bank):
    lanes = {g['id']: g for g in model['lanes']}
    limits = {tuple(sorted(((a['a'], a['pin_a']), (a['b'], a['pin_b'])))): (a, limit)
              for a, limit in pl.attachment_axis_limits}
    bundles = {g['id']: g for g in getattr(pl, 'attachment_stack_bundles', ())}
    for k in bank['lane_ids']:
        lane = lanes[k]
        bundles[k] = dict(id=k, members=deepcopy(lane['members']), host_blocks=[], host_lanes=[])
        for a in lane['edges']:
            key = tuple(sorted(((a['a'], a['pin_a']), (a['b'], a['pin_b']))))
            error = abs(pl.pin_world(index[a['a']], a['pin_a'])[0]
                        - pl.pin_world(index[a['b']], a['pin_b'])[0])
            limits[key] = (deepcopy(a), min(error, limits.get(key, (None, error))[1]))
    bundles[bank['id']] = dict(id=bank['id'], members=deepcopy(bank['members']), host_blocks=[], host_lanes=[])
    pl.attachment_stack_bundles = tuple(bundles[k] for k in sorted(bundles))
    pl.attachment_axis_limits = tuple(limits[k] for k in sorted(limits))


def column_trials(pl, comps, index, model, bank, *, deadline, max_trials):
    from netlist2schematic.plugins.attachment.attachment_integration import _fixed_ids
    from netlist2schematic.plugins.attachment.attachment_regulation import _wire_forecast
    if max_trials <= 0 or perf_counter() >= deadline:
        return
    ids = {index[n] for n in bank['members']}
    if ids & _fixed_ids(pl, index):
        return
    lanes = {g['id']: g for g in model['lanes']}
    order = sorted(zip(bank['roots'], bank['lane_ids']), key=lambda v: pl.pos_of(index[v[0]])[0])
    if len({pl.pos_of(index[n])[0] for n, _ in order}) != len(order):
        return
    center = sum(pl.pos_of(index[n])[0] for n, _ in order)/len(order)
    near = sorted(range(len(pl.column_x)), key=lambda c: (abs(pl.column_x[c]-center), c))
    cols = sorted(near[:min(len(near), len(order)+2)])
    forecast = _wire_forecast(pl, comps, set(model['power_nets']))
    options = []
    for columns in combinations(cols, len(order)):
        if perf_counter() >= deadline:
            return
        target = {index[n]: c for (_, k), c in zip(order, columns) for n in lanes[k]['members']}
        occupied = {(pl.cell_of[i][0], c) for i, c in target.items()}
        if len(occupied) != len(ids) or any(pl.comp_at.get(p) not in ids | {None} for p in occupied):
            continue
        delta = sum(abs(pl.column_x[c]-pl.pos_of(i)[0]) for i, c in target.items())
        options.append(((forecast(target), columns[-1]-columns[0], delta, columns), target))
    emitted, seen = 0, set()
    # Include the current pose if it is already straight, to install its guard
    # without consuming a random draw or changing semantic ownership.
    coherent = all(max(pl.pos_of(index[n])[0] for n in lanes[k]['members'])
        - min(pl.pos_of(index[n])[0] for n in lanes[k]['members']) <= EPS
        and all(abs(pl.pin_world(index[a['a']], a['pin_a'])[0]
                    - pl.pin_world(index[a['b']], a['pin_b'])[0]) <= EPS
                for a in lanes[k]['edges']) for k in bank['lane_ids'])
    targets = ([None] if coherent else []) + [target for _, target in sorted(options, key=lambda v: v[0])]
    for target in targets:
        for side in ((None,) if target is None else (None, 0, 1)):
            if emitted >= max_trials or perf_counter() >= deadline:
                return
            trial = deepcopy(pl)
            positions = list(trial.cell_of)
            for i, c in (target or {}).items():
                positions[i] = positions[i][0], c
            trial.set_state(positions, trial.orient)
            if target is not None:
                for root, k in order:
                    i = index[root]; pin = PINS[comps[i]['symbol']][0]
                    old = trial.orient[i]; choices = []
                    for o in trial.allowed_orientations(i):
                        trial.orient[i] = o; choices.append(trial.pin_world(i, pin)[0])
                    trial.orient[i] = old
                    x = trial.pin_world(i, pin)[0] if side is None else (min if side == 0 else max)(choices)
                    for n in lanes[k]['members']:
                        j = index[n]; old = trial.orient[j]; d, _, s = PINS[comps[j]['symbol']]; options_o = []
                        for o in trial.allowed_orientations(j):
                            trial.orient[j] = o
                            error = abs(trial.pin_world(j, d)[0]-x)+abs(trial.pin_world(j, s)[0]-x)
                            options_o.append((error, o != old, o))
                        trial.orient[j] = min(options_o)[-1]
            key = tuple(trial.cell_of), tuple(trial.orient)
            if key in seen:
                continue
            seen.add(key)
            _protect(trial, index, model, bank)
            emitted += 1
            yield trial, dict(kind='repeated_stack_columns', bank=bank['id'],
                lanes=[lanes[k]['members'] for k in bank['lane_ids']],
                columns={n: trial.cell_of[index[n]][1] for n in bank['members']},
                rows_unchanged=True, axes_unchanged=True, ownership_unchanged=True,
                existing_motion_probability_unchanged=True)


def reserve_finish(plan, budget):
    if plan.get('stack_columns', {}).get('banks'):
        budget['stack_column_reserve'] = min(16, max(0, budget['max_candidates']-budget['candidates']))


def arrange_columns(par, current, comps, nets, index, plan, options, budget, until, report, *,
                    initialization, hpwl_limit):
    model = plan.get('stack_columns', {})
    if not model.get('banks'):
        return current
    from netlist2schematic.plugins.attachment.attachment_integration import _checked_static, _fixed_world_safe, _has_budget, _fixed_ids
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    if not initialization:
        budget.pop('stack_column_reserve', None)
    before = measure_columns(current, comps, index, model)
    detail = dict(version=VERSION, before=before, applied=False, trials=0, accepted=0,
        state_rejections=0, hpwl_rejections=0, static_rejections=0, skipped=[],
        initialization=initialization, stop_reason='no_improvement')
    report['stack_column_arrangement'] = detail
    if column_rank(before) is None:
        detail['stop_reason'] = 'unmeasurable'; return current
    for number, bank in enumerate(model['banks']):
        if {index[n] for n in bank['members']} & _fixed_ids(current, index):
            detail['skipped'].append(dict(bank=bank['id'], reason='fixed_lane_member')); continue
        remaining = (16-detail['trials'])//(len(model['banks'])-number)
        best = None; old_rank = column_rank(measure_columns(current, comps, index, model))
        for trial, action in column_trials(current, comps, index, model, bank,
                                           deadline=until, max_trials=remaining):
            if not _has_budget(budget, until):
                detail['stop_reason'] = 'shared_budget'; break
            budget['candidates'] += 1; detail['trials'] += 1
            report['candidate_trials' if initialization else 'candidates'] += 1
            if not trial.valid_state() or not _fixed_world_safe(current, trial, index):
                detail['state_rejections'] += 1; continue
            measured = measure_columns(trial, comps, index, model); rank = column_rank(measured)
            same = trial.cell_of == current.cell_of and trial.orient == current.orient
            policy_only = same and len(getattr(trial, 'attachment_stack_bundles', ())) > len(getattr(current, 'attachment_stack_bundles', ()))
            if rank is None or (rank >= old_rank and not policy_only):
                continue
            hpwl = _hpwl(par, trial, nets, index)
            if hpwl > hpwl_limit+EPS:
                detail['hpwl_rejections'] += 1; continue
            safe = _checked_static(par, trial, nets, index, options, until)
            if safe is None or perf_counter() >= until:
                if safe is None: budget['exhausted'] = True
                detail['stop_reason'] = 'shared_budget'; break
            if not safe:
                detail['static_rejections'] += 1; continue
            key = rank, hpwl
            if best is None or key < best[0]:
                best = key, trial, dict(action, policy_only_adoption=policy_only)
        if best:
            _, current, action = best; report['actions'].append(action)
            detail['applied'] = True; detail['accepted'] += 1
            if not initialization: report['accepted'] += 1
        if not _has_budget(budget, until):
            detail['stop_reason'] = 'shared_budget'; break
    if detail['applied'] and detail['stop_reason'] != 'shared_budget':
        detail['stop_reason'] = 'best_verified_trial'
    detail['after'] = measure_columns(current, comps, index, model)
    return current
