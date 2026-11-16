"""Face port-proven local control banks before whole-net routing.

Extra terminals on the control net are deliberately retained.  This pass moves
only G/B access points, freezes conduction ports, and never constructs routes.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from time import perf_counter

from netlist2schematic.plugins.preflight.placement_preflight import _wire_estimate
from netlist2schematic.plugins.region.region_controls import _face_control
from netlist2schematic.plugins.region.region_layout import measure_regions
from netlist2schematic.plugins.symmetry.symmetry_layout import _static_safe
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.routing_rules import RoutingOptions

VERSION = 'region-local-control-banks-v1'
EPS = 1e-8
ACTIVE = frozenset(('nmos', 'pmos', 'npn', 'pnp'))


def compile_control_banks(par, comps, nets, index, plan):
    """Recheck source, target-branch and terminal evidence, not shared CLK alone."""
    regions = {r['id']: r for r in plan.get('regions', ())}
    rails = set(plan.get('power_interfaces', ()))
    ownership = plan.get('ownership', {})
    groups, skipped, seen = [], [], set()
    for bank in plan.get('port_banks', ()):
        source = regions.get(bank.get('source_region'), {})
        target = regions.get(bank.get('target_region'), {})
        parent = regions.get(source.get('parent'), {})
        names = source.get('members', ())
        branch = bank.get('branch')
        reason = 'unproven_bank_or_branch'
        valid = (bank.get('kind') == 'nested_conduction_fanout'
                 and source.get('kind') == 'clocked_rail_attachments'
                 and len(names) == len(set(names)) == 2
                 and not source.get('children') and not target.get('children')
                 and parent and source.get('parent') == target.get('parent')
                 and {source.get('id'), target.get('id')} <= set(parent.get('children', ()))
                 and set(names) <= set(parent.get('members', ()))
                 and all(n in index and ownership.get(n) == source.get('id') for n in names))
        source_branches = [b for b in source.get('branches', ()) if b.get('id') == branch]
        target_branches = [b for b in target.get('branches', ()) if b.get('id') == branch]
        valid = (valid and len(source_branches) == len(target_branches) == 1
                 and set(source_branches[0].get('members', ())) == set(names))
        ports = bank.get('ports', ())
        if valid:
            valid = len(ports) == 2 and {p.get('device') for p in ports} == set(names)
        if valid:
            devices = [comps[index[n]] for n in names]
            valid = devices[0]['symbol'] in ACTIVE and devices[0]['symbol'] == devices[1]['symbol']
        if valid:
            control, drain, supply = (('G', 'D', 'S') if devices[0]['symbol'] in ('nmos', 'pmos')
                                      else ('B', 'C', 'E'))
            control_net = devices[0]['nodes'].get(control)
            rail = devices[0]['nodes'].get(supply)
            valid = (control_net is not None and rail is not None
                     and control_net not in rails and not par.power_marker_sym(control_net)
                     and (rail in rails or par.power_marker_sym(rail))
                     and all(d['nodes'].get(control) == control_net and d['nodes'].get(supply) == rail
                             for d in devices)
                     and {(n, control) for n in names} <= set(map(tuple, nets.get(control_net, ())))
                     and len({d['nodes'].get(drain) for d in devices}) == 2)
            reason = 'unproven_supply_or_shared_control'
        if valid:
            target_members = set(target_branches[0].get('members', ()))
            for port in ports:
                name, net = port['device'], port.get('net')
                attachments = [a for a in source.get('attachments', ())
                               if (a.get('device'), a.get('pin'), a.get('net'), a.get('branch'),
                                   a.get('target_region'), a.get('role')) ==
                               (name, drain, net, branch, target['id'], 'clocked_rail')]
                targets = port.get('targets', ())
                if (port.get('pin') != drain or comps[index[name]]['nodes'].get(drain) != net
                        or (name, drain) not in set(map(tuple, nets.get(net, ())))
                        or len(attachments) != 1 or not targets):
                    valid = False; break
                for endpoint in targets:
                    host, pin = endpoint.get('device'), endpoint.get('pin')
                    if (host not in target_members or host not in index or pin not in ('D', 'S', 'C', 'E')
                            or ownership.get(host) != target['id']
                            or comps[index[host]]['nodes'].get(pin) != net
                            or (host, pin) not in set(map(tuple, nets.get(net, ())))):
                        valid = False; break
                if not valid: break
            reason = 'stale_or_unproven_conduction_ports'
        if not valid:
            skipped.append(dict(source_region=bank.get('source_region'), reason=reason))
            continue
        key = source['id'], target['id'], branch, tuple(sorted(names))
        if key in seen:
            continue
        seen.add(key)
        groups.append(dict(id=source['id'], source_region=source['id'], target_region=target['id'],
                           branch=branch, members=sorted(names), control_pin=control,
                           conduction_pins=[drain, supply], net=control_net, rail=rail,
                           ports=deepcopy(ports)))
    claims = defaultdict(list)
    for group in groups:
        for name in group['members']:
            claims[name].append(group['id'])
    ambiguous = {n for n, owners in claims.items() if len(owners) != 1}
    skipped.extend(dict(device=n, reason='ambiguous_bank_ownership') for n in sorted(ambiguous))
    return dict(version=VERSION, groups=[g for g in groups if not set(g['members']) & ambiguous], skipped=skipped)


def _access(pl, index, group):
    cp = group['control_pin']
    ordered = sorted((index[n] for n in group['members']),
                     key=lambda i: (pl.pin_world(i, group['conduction_pins'][1]), pl.pos_of(i)))
    a, b = (tuple(pl.pin_world(i, cp)) for i in ordered)
    return dict(horizontal=abs(a[1]-b[1]) <= EPS and
                pl.cell_of[ordered[0]][0] == pl.cell_of[ordered[1]][0] and
                pl.pin_world(ordered[0], group['conduction_pins'][1])[0] <
                pl.pin_world(ordered[1], group['conduction_pins'][1])[0]-EPS,
                order=ordered, span=abs(a[0]-b[0]),
                facing_errors=sum(pl.pin_dir(i, cp) != direction
                                  for i, direction in zip(ordered, ('east', 'west'))),
                points=[list(a), list(b)], ordered=a[0] < b[0]-EPS)


def refine_control_banks(par, pl, comps, nets, index, metadata, *, options=None, deadline=None, budget=None):
    """Atomic bank mirrors share the existing region 1s/128 proposal ledger."""
    started = perf_counter()
    result = deepcopy(metadata)
    saved = result.get('layout_regions', {})
    if not saved.get('applied'):
        return pl, result
    initial = saved.get('initialization', {})
    quota = deepcopy(budget if budget is not None else saved.get('local_budget', dict(
        seconds=1.0, max_candidates=128, elapsed_seconds=initial.get('elapsed_seconds', 0),
        candidates=initial.get('trials', 0))))
    until = started+max(0, quota['seconds']-quota['elapsed_seconds'])
    if deadline is not None:
        until = min(until, deadline)
    report = dict(version=VERSION, applied=False, candidates=0, actions=[], skipped=[],
                  budget_before=deepcopy(quota), stage='after_control_attachments_before_tail_and_astar',
                  stop_reason='no_legal_improvement', static_rejections=0, estimate_rejections=0)
    current = pl
    plan = saved.get('plan', {})
    options = options or RoutingOptions()
    protected = {index.get(i, i) for i in set(getattr(pl, 'fixed', ())) |
                 set(getattr(pl, 'fixed_cells', {})) |
                 {a['device'] for a in metadata.get('role_adjustments', ()) if 'device' in a}}
    if perf_counter() >= until or quota['candidates'] >= quota['max_candidates']:
        report['stop_reason'] = 'local_budget'
        model = dict(version=VERSION, groups=[], skipped=[])
    else:
        model = compile_control_banks(par, comps, nets, index, plan)
    report['model'] = model
    report['skipped'].extend(model['skipped'])
    if not model['groups'] and report['stop_reason'] != 'local_budget':
        report['stop_reason'] = 'no_supported_control_bank'
    groups = sorted(model['groups'], key=lambda g: tuple(sorted(pl.pos_of(index[n]) for n in g['members'])))
    for group in groups:
        if perf_counter() >= until or quota['candidates'] >= quota['max_candidates']:
            report['stop_reason'] = 'local_budget'; break
        moving = {index[n] for n in group['members']}
        if moving & protected:
            report['skipped'].append(dict(bank=group['id'], reason='fixed_or_role_protected')); continue
        before = _access(current, index, group)
        if not before['horizontal']:
            report['skipped'].append(dict(bank=group['id'], reason='not_a_horizontal_local_pair')); continue
        if before['facing_errors'] == 0 and before['ordered']:
            continue
        report['candidates'] += 1; quota['candidates'] += 1
        trial = deepcopy(current)
        trial.offset_x = list(getattr(current, 'offset_x', [0.0]*len(comps)))
        if not all(_face_control(trial, i, direction, options.grid_step)
                   for i, direction in zip(before['order'], ('east', 'west'))):
            report['skipped'].append(dict(bank=group['id'], reason='no_allowed_conduction_preserving_mirror')); continue
        if (any(trial.orient[i] not in (0, 1) for i in moving)
                or hasattr(trial, 'valid_state') and not trial.valid_state()
                or hasattr(trial, 'blocks_contiguous') and not trial.blocks_contiguous()):
            continue
        if any((trial.pos_of(i), trial.orient[i]) != (current.pos_of(i), current.orient[i])
               for i in range(len(comps)) if i not in moving):
            continue
        if any(trial.pin_world(i, pin) != current.pin_world(i, pin)
               for i in moving for pin in group['conduction_pins']):
            continue
        after = _access(trial, index, group)
        if (not after['horizontal'] or not after['ordered'] or after['facing_errors']
                or after['span'] > before['span']+EPS
                or not (after['span'] < before['span']-EPS or after['facing_errors'] < before['facing_errors'])):
            continue
        organization = measure_regions(current, comps, index, plan)
        measured = measure_regions(trial, comps, index, plan)
        if (not organization.get('measurable') or not measured.get('measurable')
                or any(measured[k] > organization[k]+EPS
                       for k in ('interleaving', 'order_errors', 'pin_axis_error', 'junction_error'))):
            continue
        estimate = _wire_estimate(current, nets, index, par)
        cost = _wire_estimate(trial, nets, index, par)
        if cost > estimate*1.15+EPS:
            report['estimate_rejections'] += 1; continue
        try:
            safe = _static_safe(par, trial, nets, index, options, until)
        except RoutingFailure:
            report['stop_reason'] = 'local_budget'; break
        if perf_counter() >= until:
            report['stop_reason'] = 'local_budget'; break
        if not safe:
            report['static_rejections'] += 1; continue
        changes = [dict(device=comps[i]['id'], orient_before=current.orient[i], orient_after=trial.orient[i],
                        dx_before=getattr(current, 'offset_x', [0.0]*len(comps))[i], dx_after=trial.offset_x[i])
                   for i in sorted(moving) if (current.pos_of(i), current.orient[i]) != (trial.pos_of(i), trial.orient[i])]
        report['actions'].append(dict(bank=group['id'], net=group['net'], changes=changes,
                                      before=before, after=after, hpwl_before=estimate, hpwl_after=cost))
        current = trial
    report.update(applied=bool(report['actions']), elapsed_seconds=perf_counter()-started)
    if report['applied'] and report['stop_reason'] == 'no_legal_improvement':
        report['stop_reason'] = 'improved'
    quota['elapsed_seconds'] += report['elapsed_seconds']
    saved.update(local_budget=quota, bank_control_refinement=report)
    if report['applied']:
        saved['metrics'] = measure_regions(current, comps, index, plan)
    return current, result
