"""Local current-mirror row cohorts proved by terminals and nearby branches.

This is an initial row plan, never an in-place edit of a saved placement.
Supply/bias sharing alone does not create a core or merge its ownership.
"""
from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
import json
from math import isfinite

VERSION = 'local-mirror-rows-v2'
_PINS = {'nmos': ('D', 'G', 'S'), 'pmos': ('D', 'G', 'S'),
         'npn': ('C', 'B', 'E'), 'pnp': ('C', 'B', 'E')}
_OPPOSITE = {'nmos': 'pmos', 'pmos': 'nmos', 'npn': 'pnp', 'pnp': 'npn'}
EPS = 1e-8


def compile_cores(comps, structures, junctions):
    """Require a two-device mirror and unique common-bias opposite heads.

    The heads support local correspondence. They are references, not additional
    members of a transitive rigid block. Other terminals on their drain nodes
    remain explicit forks and receive no new same-column constraint.
    """
    by_id = {c['id']: c for c in comps}
    groups = defaultdict(list)
    for c in comps:
        if c['symbol'] not in _PINS:
            continue
        drain, control, source = _PINS[c['symbol']]
        groups[c['symbol'], c['nodes'][control], c['nodes'][source]].append(c['id'])
    cores, skipped = [], []
    for (symbol, control_net, rail), members in sorted(groups.items()):
        if len(members) < 2:
            continue
        drain, control, source = _PINS[symbol]
        diode = [n for n in members if by_id[n]['nodes'][drain] == control_net]
        if not diode:
            continue
        record = dict(id='local_mirror:' + '|'.join(sorted(members)), members=sorted(members))
        if (len(members) != 2 or len(diode) != 1
                or junctions.get(control_net, {}).get('is_rail')
                or not junctions.get(rail, {}).get('is_rail')):
            skipped.append(dict(record, reason='mirror_not_a_local_two_device_rail_pair'))
            continue
        ports = {tuple(p) for p in junctions.get(control_net, {}).get('control_ports', ())}
        if ports != {(n, control) for n in members}:
            skipped.append(dict(record, reason='shared_control_has_other_active_consumers'))
            continue
        reference = diode[0]
        output = next(n for n in members if n != reference)
        heads, error = [], None
        for name in (reference, output):
            net = by_id[name]['nodes'][drain]
            possible = []
            for other, pin in junctions.get(net, {}).get('conduction_ports', ()):
                comp = by_id[other]
                if comp['symbol'] != _OPPOSITE[symbol]:
                    continue
                d, g, s = _PINS[comp['symbol']]
                if pin == d and junctions.get(comp['nodes'][s], {}).get('is_rail'):
                    possible.append(other)
            if len(possible) != 1:
                error = 'opposite_branch_head_not_unique'
                break
            heads.append(possible[0])
        if error is None:
            first, second = (by_id[n] for n in heads)
            _, gate, origin = _PINS[first['symbol']]
            if (heads[0] == heads[1] or first['nodes'][gate] != second['nodes'][gate]
                    or first['nodes'][origin] != second['nodes'][origin]
                    or junctions.get(first['nodes'][gate], {}).get('is_rail')):
                error = 'branch_heads_lack_corresponding_terminal_roles'
        if error:
            skipped.append(dict(record, reason=error))
            continue
        roles = dict(reference=reference, output=output,
                     reference_head=heads[0], output_head=heads[1])
        support = [s['id'] for s in structures if s['kind'] == 'current_mirror'
                   and set(s.get('devices', ())) == set(members)]
        terminals = {n: deepcopy(by_id[n]['nodes']) for n in members + heads}
        cores.append(dict(record, roles=roles, terminal_contract=terminals,
            branches=[[heads[0], reference], [heads[1], output]],
            control_net=control_net, source_rail=rail, original_structures=support,
            evidence='one_diode_reference_two_control_users_and_unique_common_bias_opposite_heads'))
    return dict(version=VERSION, cores=cores, skipped=skipped)


def plan_core_rows(devices, comps, index, levels, readable, model):
    """Consume the original row solver before any columns/positions exist."""
    result = deepcopy(readable)
    report = deepcopy(model)
    report.update(applied_relations=[], unapplied_relations=[])
    if not model['cores']:
        return result, report
    from netlist2schematic.plugins.layout_shared.branch_levels import plan_branch_rows
    relations = [dict(id=c['id'], kind='local_mirror_cohort',
                      layout=dict(same_row=[c['members']])) for c in model['cores']]
    rows = [readable['original_rows'][c['id']] for c in comps]
    _, planned = plan_branch_rows(devices, levels, rows, relations, comps, index)
    original_bridges = {b['id'] for b in (readable.get('branch_row_plan') or {}).get('bridge_alignment', ())}
    new_bridges = {b['id'] for b in planned['bridge_alignment']}
    if not original_bridges <= new_bridges:
        report['unapplied_relations'] = [dict(id=c['id'],
            reason='would_displace_confirmed_rl_attachment') for c in model['cores']]
        return result, report
    accepted = {r['relation'] for r in planned['same_row']}
    report['applied_relations'] = sorted(accepted)
    report['unapplied_relations'] = [dict(id=r['relation'], reason=r['reason'])
                                   for r in planned['skipped_relations']]
    if accepted:
        result.update(branch_row_plan=planned, display_rows=dict(planned['display_rows']))
        by_members = {frozenset(b['members']): b for b in planned['bridge_alignment']}
        for pattern in result['patterns']:
            if 'bridge_alignment' in pattern:
                pattern['bridge_alignment'] = deepcopy(by_members[frozenset(pattern['members'])])
        result.pop('signature', None)
        result['signature'] = sha256(json.dumps(dict(plan=result, devices=devices),
            sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()
    return result, report


def measure_cores(placement, comps, index, model):
    """Measure one source-derived manifest on reference and new coordinates."""
    records, missing = [], []
    eligible = {n for n in model.get('applied_relations', ())}
    unapplied = {r['id']: r['reason'] for r in model.get('unapplied_relations', ())}
    for core in model.get('cores', ()):
        if core['id'] in unapplied and core['id'] not in eligible:
            continue
        try:
            roles = core['roles']
            names = [roles[k] for k in ('reference', 'output', 'reference_head', 'output_head')]
            for n in names:
                if comps[index[n]]['nodes'] != core['terminal_contract'][n]:
                    raise ValueError('terminal_contract_changed')
            positions = [tuple(placement.pos_of(index[n])) for n in names]
            if not all(isfinite(v) for p in positions for v in p):
                raise ValueError('nonfinite_core_position')
            ref, out, head_ref, head_out = positions
            row_distance = abs(ref[1] - out[1])
            order_error = int((ref[0] - out[0]) * (head_ref[0] - head_out[0]) < -EPS)
            pin_error = 0.0
            for head, tail in core['branches']:
                a = comps[index[head]]
                b = comps[index[tail]]
                pin_error += abs(placement.pin_world(index[head], _PINS[a['symbol']][0])[0]
                                 - placement.pin_world(index[tail], _PINS[b['symbol']][0])[0])
            control_pairs = []
            vectors = {'east': (1, 0), 'west': (-1, 0), 'north': (0, -1), 'south': (0, 1)}
            for a, b in ((names[0], names[1]), (names[2], names[3])):
                pin = _PINS[comps[index[a]]['symbol']][1]
                points = [tuple(placement.pin_world(index[n], pin)) for n in (a, b)]
                if not all(isfinite(v) for p in points for v in p):
                    raise ValueError('nonfinite_control_point')
                errors = sum(sum((points[1-k][axis] - points[k][axis]) *
                    vectors[placement.pin_dir(index[n], pin)][axis] for axis in (0, 1)) <= EPS
                    for k, n in enumerate((a, b)))
                control_pairs.append(dict(members=[a, b], pin=pin, errors=errors,
                    same_height=abs(points[0][1]-points[1][1]) <= EPS))
            interleaving = sum(1 for c in comps if c['symbol'] in _PINS
                and c['id'] not in names
                and min(ref[0], out[0]) + EPS < placement.pos_of(index[c['id']])[0] < max(ref[0], out[0]) - EPS
                and abs(placement.pos_of(index[c['id']])[1] - ref[1]) < EPS
                and row_distance < EPS)
            records.append(dict(id=core['id'], roles=deepcopy(roles), row_errors=int(row_distance > EPS),
                row_distance=row_distance / 100, order_errors=order_error,
                interleaving=interleaving, support_pin_error=pin_error / 100,
                control_facing_errors=sum(r['errors'] for r in control_pairs), control_pairs=control_pairs))
        except (KeyError, ValueError, IndexError, TypeError, AttributeError) as exc:
            missing.append(dict(id=core['id'], reason=str(exc)))
    usable = bool(records) and not missing
    fields = ('row_errors', 'row_distance', 'order_errors', 'interleaving', 'support_pin_error',
              'control_facing_errors')
    return dict(version=VERSION, count=len(model.get('cores', ())),
        applicable=len(model.get('cores', ())) - len(unapplied), measurable=usable,
        records=records, missing=missing, skipped=deepcopy(model.get('skipped', ())),
        unapplied=deepcopy(model.get('unapplied_relations', ())),
        **{k: sum(r[k] for r in records) if usable else None for k in fields})


def core_rank(measurement):
    if not measurement.get('count') or not measurement.get('applicable', measurement.get('count')):
        return (0, 0, 0, 0, 0, 0)
    if not measurement.get('measurable'):
        return None
    keys = ('row_errors', 'order_errors', 'interleaving', 'row_distance',
            'control_facing_errors', 'support_pin_error')
    if any(type(measurement.get(k)) not in (int, float) or not isfinite(measurement[k])
           or measurement[k] < 0 for k in keys):
        return None
    return tuple(round(measurement[k], 8) for k in
                 keys)
