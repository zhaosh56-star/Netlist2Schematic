"""Finish a proven two-arm junction inside its real facing-port band.

Existing tap recognition is reused.  Only saved passive local offsets change;
public axes, cells, directions, hosts, SA proposals and routing stay unchanged.
"""
from copy import deepcopy
from math import ceil, floor, isfinite
from time import perf_counter

from netlist2schematic.common.passive_coordinates import PASSIVE
from netlist2schematic.plugins.attachment.attachment_taps import _bands, measure_taps

VERSION = 'two-arm-tap-offsets-v1'
EPS = 1e-8
CONDUCTION = {'nmos': {'D', 'S'}, 'pmos': {'D', 'S'},
              'npn': {'C', 'E'}, 'pnp': {'C', 'E'}}
HORIZONTAL = {'east': 1, 'west': -1}


def band_evidence(pl, comps, index, plan, relation):
    """Recheck unique live terminal roles; do not infer a functional label."""
    group = next(g for g in plan['groups'] if g['id'] == relation['group'])
    members = group['members']
    if (group['kind'] != 'two_arm_join' or len(members) != 2
            or set(members) != set(relation['members'])
            or len(relation['ends']) != 2):
        raise ValueError('not_two_arm_tap')
    common = group.get('center_junction')
    junctions = plan['readability_plan']['junctions']
    if not common or junctions.get(common, {}).get('is_rail', True):
        raise ValueError('unverified_common_signal_junction')
    parts = {c['id']: c for c in comps}
    if set(members).intersection(plan.get('source_managed_members', ())):
        raise ValueError('source_managed_arm')
    ends = {e['guest'][0]: e for e in relation['ends']}
    if set(ends) != set(members):
        raise ValueError('ambiguous_arm_endpoint')
    lo, hi, access = _bands(pl, comps, index, relation)
    arms = []
    for name in members:
        c, end = parts[name], ends[name]
        net, pin = end['net'], end['guest'][1]
        if (c['symbol'] not in PASSIVE or len(c['nodes']) != 2
                or set(c['nodes'].values()) != {common, net}
                or common == net or c['nodes'].get(pin) != net):
            raise ValueError('arm_terminal_contract_changed')
        if junctions.get(net, {}).get('is_rail', True):
            raise ValueError('rail_is_not_branch_access')
        live_hosts = {(other['id'], p) for other in comps
            for p, n in other['nodes'].items()
            if n == net and p in CONDUCTION.get(other['symbol'], ())}
        if live_hosts != set(map(tuple, end['hosts'])) or len(live_hosts) != 2:
            raise ValueError('ambiguous_or_stale_conduction_hosts')
        xs = [pl.pin_world(index[n], p)[0] for n, p in end['hosts']]
        if abs(xs[0]-xs[1]) > EPS:
            raise ValueError('host_ports_not_on_one_axis')
        common_pin = next(p for p, n in c['nodes'].items() if n == common)
        i = index[name]
        point, outer = pl.pin_world(i, common_pin), pl.pin_world(i, pin)
        direction = pl.pin_dir(i, common_pin)
        if (direction not in HORIZONTAL or abs(point[1]-outer[1]) > EPS
                or pl.pin_dir(i, pin) != {'east': 'west', 'west': 'east'}[direction]):
            raise ValueError('arm_not_horizontal')
        if (xs[0]-outer[0]) * -HORIZONTAL[direction] < -EPS:
            raise ValueError('arm_faces_away_from_its_branch')
        arms.append(dict(device=name,common_pin=common_pin,outer_pin=pin,
            point=list(point),outer_point=list(outer),direction=direction,
            net=net,host_x=xs[0],hosts=end['hosts']))
    x = sum(a['point'][0] for a in arms)/2
    if (abs(arms[0]['host_x']-arms[1]['host_x']) < EPS
            or any((x-a['point'][0])*HORIZONTAL[a['direction']] < -EPS for a in arms)):
        raise ValueError('arms_do_not_face_a_common_interior')
    if not all(isfinite(v) for v in (lo, hi, x, *(v for a in arms for v in a['point']))):
        raise ValueError('nonfinite_band_coordinate')
    return dict(group=group['id'],members=list(members),common_junction=common,
        band=[lo,hi],arms=arms,common_x=x,
        outside_errors=sum(r['outside'] > EPS for r in access),
        outside_distance=sum(r['outside'] for r in access)/100)


def _common_shunt(pl, comps, index, plan, evidence, y):
    """An optional unique singleton rail shunt follows its actual common node."""
    from netlist2schematic.plugins.attachment.attachment_integration import _fixed_ids
    common = evidence['common_junction']
    others = [c for c in comps if c['id'] not in evidence['members']
        and c['symbol'] in PASSIVE and common in c['nodes'].values()]
    if len(others) != 1:
        return None, 'no_unique_common_shunt'
    c = others[0]
    if len(c['nodes']) != 2 or len(set(c['nodes'].values())) != 2:
        return None, 'invalid_shunt_terminals'
    rail = next(n for n in c['nodes'].values() if n != common)
    if not plan['readability_plan']['junctions'].get(rail, {}).get('is_rail'):
        return None, 'common_attachment_is_not_rail_shunt'
    owners = [g for g in plan['groups'] if c['id'] in g['members']]
    if (len(owners) != 1 or owners[0]['kind'] != 'single_attachment'
            or owners[0]['members'] != [c['id']]
            or c['id'] in plan.get('source_managed_members', ())):
        return None, 'shunt_has_other_owner'
    i = index[c['id']]
    if i in _fixed_ids(pl, index):
        return None, 'fixed_common_shunt'
    pin = next(p for p, n in c['nodes'].items() if n == common)
    point, direction = pl.pin_world(i, pin), pl.pin_dir(i, pin)
    if (direction not in ('north', 'south')
            or (y-point[1])*(1 if direction == 'south' else -1) <= EPS):
        return None, 'shunt_does_not_face_common_band'
    return dict(device=c['id'],pin=pin,point=list(point)), None


def band_trials(pl, comps, index, plan, relation, *, grid_step=2, halo=12, deadline=None):
    """At most four atomic offset-only trials; no dynamic row insertion."""
    from netlist2schematic.plugins.attachment.attachment_integration import _fixed_ids
    if deadline is not None and perf_counter() >= deadline:
        return
    try:
        evidence = band_evidence(pl, comps, index, plan, relation)
    except (KeyError, ValueError, TypeError, IndexError, AttributeError):
        return
    if {index[n] for n in evidence['members']} & _fixed_ids(pl, index):
        return
    lo, hi = evidence['band']
    if (not isfinite(grid_step) or grid_step <= 0 or hi-lo < 2*halo+2*grid_step
            or not evidence['outside_errors']):
        return
    mid = (lo+hi)/2
    ratio = (mid-lo)/grid_step
    choices = {lo+floor(ratio)*grid_step, lo+ceil(ratio)*grid_step}
    choices = sorted(choices,key=lambda y:(abs(y-mid),
        sum(abs(y-a['point'][1]) for a in evidence['arms']),y-lo))
    for y in choices:
        if deadline is not None and perf_counter() >= deadline:
            return
        shunt, reason = _common_shunt(pl, comps, index, plan, evidence, y)
        modes = (True, False) if shunt is not None else (False,)
        for include_shunt in modes:
            if deadline is not None and perf_counter() >= deadline:
                return
            trial = deepcopy(pl)
            values = [list(p) for p in getattr(pl,'passive_local_offsets',[(0.,0.)]*len(comps))]
            changes = []
            for arm in evidence['arms']:
                i = index[arm['device']]
                dy = y-arm['point'][1]
                values[i][1] += dy
                changes.append(dict(device=arm['device'],delta=[0.,dy],
                    role='two_arm_conduction_band'))
            if include_shunt:
                i = index[shunt['device']]
                dx = round((evidence['common_x']-shunt['point'][0])/grid_step)*grid_step
                if abs(dx) > EPS:
                    values[i][0] += dx
                    changes.append(dict(device=shunt['device'],delta=[dx,0.],
                        role='unique_common_junction_rail_shunt'))
            trial.passive_local_offsets = values
            yield trial, dict(kind='two_arm_tap_offsets',version=VERSION,
                group=evidence['group'],members=evidence['members'],band=evidence['band'],
                target_y=y,common_x=evidence['common_x'],host_evidence=evidence['arms'],
                offsets_changed=changes,optional_shunt_skipped=(None if include_shunt else reason or 'arms_only_variant'),
                cells_orientations_axes_hosts_and_sources_preserved=True)


def finish_bands(par, pl, comps, nets, index, plan, options, budget, until, report, *, hpwl_limit):
    """Use only leftover original allowance, after existing hosts have settled."""
    from netlist2schematic.plugins.attachment import attachment_integration as ai
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    from netlist2schematic.routing.routing_rules import RoutingOptions
    options = options or RoutingOptions()
    stage = dict(version=VERSION,trials=0,accepted=0,state_rejections=0,
        objective_rejections=0,hpwl_rejections=0,geometry_rejections=0,
        actions=[],skipped=[],stop_reason='not_applicable',shared_budget_reset=False)
    report['two_arm_band_finish'] = stage
    groups = {g['id']:g for g in plan.get('groups',())}
    relations = [r for r in plan.get('conduction_taps',{}).get('groups',())
                 if groups.get(r['group'],{}).get('kind') == 'two_arm_join']
    current = pl
    ordered = []
    for relation in relations:
        if not ai._has_budget(budget,until):
            stage['stop_reason']='shared_attachment_budget'
            return current
        try:
            e = band_evidence(current,comps,index,plan,relation)
            ordered.append(((-e['outside_errors'],-e['outside_distance'],e['band'][0],e['common_x'],e['group']),relation))
        except (KeyError,ValueError,TypeError,IndexError,AttributeError) as exc:
            stage['skipped'].append(dict(group=relation['group'],reason=str(exc)))
    for _, relation in sorted(ordered,key=lambda item:item[0]):
        if not ai._has_budget(budget,until):
            stage['stop_reason']='shared_attachment_budget'
            return current
        local = dict(groups=[relation])
        access = measure_taps(current,comps,index,local)
        if not access['measurable'] or not access['errors']:
            stage['skipped'].append(dict(group=relation['group'],reason='already_in_band_or_unmeasurable'))
            continue
        before = ai.attachment_rank(ai.measure_attachments(current,comps,index,plan))
        old_wire = _hpwl(par,current,nets,index)
        for trial, action in band_trials(current,comps,index,plan,relation,
                grid_step=options.grid_step,halo=options.halo,deadline=until):
            if not ai._has_budget(budget,until):
                stage['stop_reason']='shared_attachment_budget'
                return current
            budget['candidates']+=1;report['candidates']+=1;stage['trials']+=1
            if not trial.valid_state() or not ai._fixed_world_safe(current,trial,index):
                stage['state_rejections']+=1
                continue
            after = ai.attachment_rank(ai.measure_attachments(trial,comps,index,plan))
            improved = measure_taps(trial,comps,index,local)
            # Match the existing physical-path guard: preserve shape, axis,
            # inclusion and source terms. Nearest-owner distance is soft.
            if (before is None or after is None or any(n>o+EPS for n,o in
                    zip(after[:4]+after[5:],before[:4]+before[5:]))
                    or not improved['measurable']
                    or (improved['errors'],improved['outside_distance']) >=
                       (access['errors'],access['outside_distance'])):
                stage['objective_rejections']+=1
                continue
            new_wire = _hpwl(par,trial,nets,index)
            # Reuse the existing tap-stage 115% estimate guard. HPWL may rise
            # slightly when a common node leaves the gate row even though the
            # actual final routing becomes shorter and avoids crossings.
            if new_wire > hpwl_limit+EPS:
                stage['hpwl_rejections']+=1
                continue
            safe = ai._checked_static(par,trial,nets,index,options,until)
            if safe is None or perf_counter() >= until:
                budget['exhausted']=True;stage['stop_reason']='shared_attachment_budget'
                return current
            if not safe:
                stage['geometry_rejections']+=1
                continue
            action.update(before=access,after=improved,
                estimated_wire_before=old_wire,estimated_wire_after=new_wire,
                owner_distance_before=before[4],owner_distance_after=after[4])
            current = trial
            report['accepted']+=1;report['actions'].append(action)
            stage['accepted']+=1;stage['actions'].append(action)
            break
    stage['stop_reason']='considered_bands' if relations else 'not_applicable'
    return current
