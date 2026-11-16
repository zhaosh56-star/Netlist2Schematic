"""Small control-node networks, arranged after SA without changing terminals.

A proven transistor control port may have an open passive chain and a return
path to a conduction node.  These are relative port roles, not circuit names.
The pass moves only those attachments and, for a confirmed same-row pair,
may face controls outward while preserving both real conduction-pin points.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from itertools import product
from math import fsum
from time import perf_counter

from netlist2schematic.plugins.layout_shared.branch_alignment import _grid_offset
from netlist2schematic.plugins.preflight.placement_preflight import _wire_estimate
from netlist2schematic.plugins.region.region_layout import measure_regions
from netlist2schematic.plugins.symmetry.symmetry_layout import _static_safe
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.routing_rules import RoutingOptions

VERSION = 'region-control-networks-v2'
ACTIVE = frozenset(('nmos', 'pmos', 'npn', 'pnp'))
PASSIVE = frozenset(('resistor', 'capacitor', 'inductor'))
EPS = 1e-8


def compile_control_networks(comps, nets, index, plan):
    """Require unique paths; rails, taps, loops and shared membership stop us."""
    from netlist2schematic.layout.structured_placement import par
    rails = set(plan.get('power_interfaces', ()))
    groups, skipped = [], []

    def is_rail(net):
        return net in rails or bool(par.power_marker_sym(net))

    def walk(name, start):
        members, path, visited = [], [start], set()
        while name in index and len(members) < 8:
            comp = comps[index[name]]
            nodes = comp['nodes']
            if (name in visited or comp['symbol'] not in PASSIVE or len(nodes) != 2
                    or len(set(nodes.values())) != 2 or path[-1] not in nodes.values()):
                return None
            visited.add(name)
            members.append(name)
            other = next(net for net in nodes.values() if net != path[-1])
            if other in path or is_rail(other):
                return None
            path.append(other)
            remaining = [(n, p) for n, p in nets.get(other, ()) if n != name]
            if not remaining:
                return dict(kind='open_chain', members=members, path=path)
            if (len(remaining) == 1 and remaining[0][0] in index
                    and comps[index[remaining[0][0]]]['symbol'] in PASSIVE):
                name = remaining[0][0]
                continue
            if remaining and all(n in index and comps[index[n]]['symbol'] in ACTIVE
                                 and p in ('D', 'C') for n, p in remaining):
                return dict(kind='conduction_return', members=members, path=path,
                            ports=[list(port) for port in sorted(remaining)])
            return None
        return None

    owners = plan.get('ownership', {})
    for comp in comps:
        host = comp['id']
        if comp['symbol'] not in ACTIVE or host not in owners:
            continue
        pin = 'G' if 'G' in comp['nodes'] else 'B'
        net = comp['nodes'].get(pin)
        ports = set(map(tuple, nets.get(net, ())))
        others = sorted(ports - {(host, pin)})
        if (is_rail(net) or len(others) != 2 or any(n not in index
                or comps[index[n]]['symbol'] not in PASSIVE for n, _ in others)):
            continue
        paths = [walk(n, net) for n, _ in others]
        if (any(p is None for p in paths)
                or {p['kind'] for p in paths} != {'open_chain', 'conduction_return'}):
            skipped.append(dict(host=host, reason='control_paths_not_unique_open_and_return'))
            continue
        external = next(p for p in paths if p['kind'] == 'open_chain')
        returned = next(p for p in paths if p['kind'] == 'conduction_return')
        # Longer return chains require their own path-shape planner.  Do not
        # truncate them or pretend that the unhandled members are organized.
        if len(returned['members']) != 1:
            skipped.append(dict(host=host, reason='multi_member_return_not_supported'))
            continue
        members = external['members'] + returned['members']
        if len(members) != len(set(members)):
            skipped.append(dict(host=host, reason='overlapping_control_paths'))
            continue
        groups.append(dict(host=host, pin=pin, net=net, members=members,
                           external=external, returned=returned))
    claims = defaultdict(list)
    for group in groups:
        for name in group['members']:
            claims[name].append(group['host'])
    ambiguous = {host for hosts in claims.values() if len(hosts) > 1 for host in hosts}
    skipped.extend(dict(host=host, reason='shared_passive_ownership') for host in sorted(ambiguous))
    return dict(version=VERSION, groups=[g for g in groups if g['host'] not in ambiguous], skipped=skipped)


def measure_control_networks(pl, comps, index, model):
    """Same real-port relations measure the reference and the new layout."""
    records, missing = [], []
    for group in model['groups']:
        try:
            host = index[group['host']]
            origin = pl.pin_world(host, group['pin'])
            direction = pl.pin_dir(host, group['pin'])
            vector = {'west': (-1, 0), 'east': (1, 0), 'north': (0, -1), 'south': (0, 1)}[direction]
            backward, distance, shape, entry_errors = 0.0, 0.0, 0, 0
            for chain in (group['external'], group['returned']):
                previous = origin
                centers = []
                for k, name in enumerate(chain['members']):
                    i = index[name]
                    nodes = comps[i]['nodes']
                    before = next(p for p, n in nodes.items() if n == chain['path'][k])
                    after = next(p for p, n in nodes.items() if n == chain['path'][k+1])
                    entry, exit = pl.pin_world(i, before), pl.pin_world(i, after)
                    if not k:
                        direction = pl.pin_dir(i, before)
                        outward = {'west': (-1, 0), 'east': (1, 0),
                                   'north': (0, -1), 'south': (0, 1)}[direction]
                        entry_errors += int(sum((origin[a]-entry[a])*outward[a] for a in (0, 1)) < -EPS)
                    distance += abs(previous[0]-entry[0])+abs(previous[1]-entry[1])
                    previous = exit
                    centers.append(pl.pos_of(i))
                    backward += max(0, -sum((entry[a]-origin[a])*vector[a] for a in (0, 1)))
                if chain['kind'] == 'conduction_return':
                    points = [pl.pin_world(index[n], p) for n, p in chain['ports']]
                    anchor = tuple(fsum(p[a] for p in points)/len(points) for a in (0, 1))
                    distance += abs(previous[0]-anchor[0])+abs(previous[1]-anchor[1])
                elif len(centers) > 1:
                    # A chain can be horizontal or vertical.  Nonmonotone
                    # order and a zigzag of origins are independent errors.
                    for axis in (0, 1):
                        values = [p[axis] for p in centers]
                        if max(values)-min(values) <= EPS:
                            other = [p[1-axis] for p in centers]
                            shape += int(other != sorted(other) and other != sorted(other, reverse=True))
                            break
                    else:
                        shape += 1
            records.append(dict(host=group['host'], members=group['members'],
                                backward_distance=backward, endpoint_distance=distance, shape_errors=shape,
                                host_entry_reversals=entry_errors))
        except (KeyError, ValueError, StopIteration, IndexError) as exc:
            missing.append(dict(host=group['host'], reason=str(exc)))
    usable = bool(records) and not missing
    return dict(version=VERSION, group_count=len(model['groups']), measurable=usable,
        backward_distance=fsum(r['backward_distance'] for r in records) if usable else None,
        endpoint_distance=fsum(r['endpoint_distance'] for r in records) if usable else None,
        host_entry_reversals=sum(r['host_entry_reversals'] for r in records) if usable else None,
        shape_errors=sum(r['shape_errors'] for r in records) if usable else None,
        records=records, missing=missing)


def measure_saved_controls(pl, comps, index, plan):
    """Remeasure actual saved coordinates; never run a placement proposal."""
    nets = defaultdict(list)
    for comp in comps:
        for pin, net in comp.get('nodes', {}).items():
            nets[net].append((comp['id'], pin))
    model = compile_control_networks(comps, nets, index, plan)
    return measure_control_networks(pl, comps, index, model)


def _orient_between(pl, i, nodes, first, last, start, end):
    before = next(p for p, n in nodes.items() if n == first)
    after = next(p for p, n in nodes.items() if n == last)
    choices = []
    old = pl.orient[i]
    for orient in pl.allowed_orientations(i):
        pl.orient[i] = orient
        a, b = pl.pin_world(i, before), pl.pin_world(i, after)
        cost = sum(abs(a[k]-start[k])+abs(b[k]-end[k]) for k in (0, 1))
        vector = {'west': (-1, 0), 'east': (1, 0),
                  'north': (0, -1), 'south': (0, 1)}[pl.pin_dir(i, before)]
        reverse = sum((start[k]-a[k])*vector[k] for k in (0, 1)) < -EPS
        # If both far ends lie on one side, Manhattan cost alone ties the two
        # horizontal orientations. Preserve short access to the proven host;
        # the distant return can use the existing orthogonal router.
        choices.append((cost, reverse, sum(abs(a[k]-start[k]) for k in (0, 1)), orient != old, orient))
    pl.orient[i] = min(choices)[-1]


def _face_control(pl, i, direction, step):
    """Mirror only when *both* conduction endpoints retain their exact point."""
    comp = pl.comps[i]
    pin = 'G' if 'G' in comp['nodes'] else 'B'
    pins = ('D', 'S') if 'D' in comp['nodes'] else ('C', 'E')
    original = [tuple(pl.pin_world(i, p)) for p in pins]
    old_orient, old_dx = pl.orient[i], pl.offset_x[i]
    for orient in (pl.allowed_orientations(i) if hasattr(pl, 'allowed_orientations') else (0, 1)):
        pl.orient[i], pl.offset_x[i] = orient, old_dx
        if pl.pin_dir(i, pin) != direction:
            continue
        desired = old_dx+original[0][0]-pl.pin_world(i, pins[0])[0]
        dx = _grid_offset(pl, i, desired, step)
        if dx is None or abs(dx-desired) > EPS:
            continue
        pl.offset_x[i] = dx
        if all(all(abs(a-b) <= EPS for a, b in zip(pl.pin_world(i, p), point))
               for p, point in zip(pins, original)):
            return True
    pl.orient[i], pl.offset_x[i] = old_orient, old_dx
    return False


def _trials(pl, comps, index, groups, step):
    hosts = sorted((index[g['host']] for g in groups), key=lambda i: pl.pos_of(i)[0])
    if len(hosts) == 2 and pl.cell_of[hosts[0]][0] != pl.cell_of[hosts[1]][0]:
        return
    faced = deepcopy(pl)
    faced.offset_x = list(getattr(pl, 'offset_x', [0.0]*len(comps)))
    # Do not change the default SA domain or swap node identities. The bounded
    # finish may rotate only the port-proven R/L/C members through the actual
    # geometry table, including 180/270 degrees when their pin order needs it.
    faced.terminal_orientation_ids = set(getattr(faced, 'terminal_orientation_ids', ())) | {
        index[name] for group in groups for name in group['members']}
    if len(hosts) == 2:
        for i, direction in zip(hosts, ('west', 'east')):
            if not _face_control(faced, i, direction, step):
                return
    by_host = {index[g['host']]: g for g in groups}
    # Try the real source/emitter side first, then the opposite vertical lane.
    source_pin = 'S' if 'S' in comps[hosts[0]]['nodes'] else 'E'
    first_sign = 1 if faced.pin_dir(hosts[0], source_pin) == 'south' else -1
    for vertical in (first_sign, -first_sign):
        for gaps in product((1, 2), repeat=len(hosts)):
            trial = deepcopy(faced)
            cells, ids, targets = list(trial.cell_of), set(), []
            good = True
            for i, gap in zip(hosts, gaps):
                group = by_host[i]
                row, col = trial.cell_of[i]
                direction = trial.pin_dir(i, group['pin'])
                if direction not in ('west', 'east'):
                    good = False; break
                lane = col+(-gap if direction == 'west' else gap)
                locations = [(row+vertical*(k+1), lane) for k in range(len(group['external']['members']))]
                names = group['external']['members']+group['returned']['members']
                locations += [(row, lane)]
                for name, location in zip(names, locations):
                    ids.add(index[name]);targets.append(location);cells[index[name]] = location
            if (not good or len(targets) != len(set(targets)) or any(not (0 <= r < len(pl.row_y)
                    and 0 <= c < len(pl.column_x)) for r, c in targets)
                    or any(pl.comp_at.get(cell) not in ids | {None} for cell in targets)):
                continue
            trial.set_state(cells, trial.orient)
            trial.row_of = [r for r, _ in trial.cell_of]
            for i in ids:
                trial.offset_x[i] = 0.0
            for group in groups:
                host = index[group['host']]
                origin = trial.pin_world(host, group['pin'])
                for chain in (group['external'], group['returned']):
                    names = chain['members']
                    for k, name in enumerate(names):
                        i = index[name]
                        start = origin if not k else trial.pos_of(index[names[k-1]])
                        if k+1 < len(names):
                            end = trial.pos_of(index[names[k+1]])
                        elif chain['kind'] == 'conduction_return':
                            points = [trial.pin_world(index[n], p) for n, p in chain['ports']]
                            end = tuple(fsum(p[a] for p in points)/len(points) for a in (0, 1))
                        else:
                            x, y = trial.pos_of(i);end = (x, y+vertical*100)
                        _orient_between(trial, i, comps[i]['nodes'], chain['path'][k], chain['path'][k+1], start, end)
            yield trial


def refine_region_controls(par, pl, comps, nets, index, metadata, *, options=None, deadline=None, budget=None):
    """Use the original region initialization/finish quota, no route search."""
    started = perf_counter()
    result = deepcopy(metadata)
    saved = result.get('layout_regions', {})
    if not saved.get('applied'):
        return pl, result
    plan = saved.get('plan', {})
    model = compile_control_networks(comps, nets, index, plan)
    before = measure_control_networks(pl, comps, index, model)
    report = dict(version=VERSION, applied=False, model=model, before=before, after=deepcopy(before),
        candidates=0, actions=[], skipped=list(model['skipped']), static_rejections=0,
        estimate_rejections=0, stop_reason='no_legal_improvement', stage='after_sa_before_tail_and_astar')
    saved['control_refinement'] = report
    initial = saved.get('initialization', {})
    quota = deepcopy(budget if budget is not None else saved.get('local_budget', dict(seconds=1.0,
        max_candidates=128, elapsed_seconds=initial.get('elapsed_seconds', 0), candidates=initial.get('trials', 0))))
    report['budget_before'] = deepcopy(quota)
    if not model['groups'] or getattr(pl, 'passive_policy', None) != 'flexible':
        report.update(stop_reason='no_supported_control_network' if not model['groups'] else 'fixed_passive_rows',
                      elapsed_seconds=perf_counter()-started)
        quota['elapsed_seconds'] += report['elapsed_seconds']
        saved['local_budget'] = quota
        return pl, result
    until = started+max(0, quota['seconds']-quota['elapsed_seconds'])
    if deadline is not None:
        until = min(until, deadline)
    fixed = {index.get(i, i) for i in set(getattr(pl, 'fixed', ())) | set(getattr(pl, 'fixed_cells', {}))}
    options = options or RoutingOptions()
    groups_by_host = {g['host']: g for g in model['groups']}
    sets, seen = [], set()
    pairs = {tuple(sorted(pair)) for region in plan.get('regions', ()) if not region.get('children')
             for pair in region.get('pairs', ()) if len(pair) == 2 and len(set(pair)) == 2
             and all(n in groups_by_host for n in pair)}
    claims = defaultdict(set)
    for pair in pairs:
        for name in pair:
            claims[name].add(pair)
    for pair in sorted(pairs):
        if all(len(claims[name]) == 1 for name in pair):
            sets.append([groups_by_host[n] for n in pair]);seen.update(pair)
    report['skipped'].extend(dict(host=name, reason='ambiguous_control_pair')
                             for name in sorted(claims) if len(claims[name]) > 1)
    sets += [[g] for g in model['groups'] if g['host'] not in seen]
    current = pl
    for groups in sets:
        moving = {index[n] for g in groups for n in [g['host'], *g['members']]}
        if moving & fixed:
            report['skipped'].append(dict(hosts=[g['host'] for g in groups], reason='explicitly_fixed_member'))
            continue
        if perf_counter() >= until or quota['candidates'] >= quota['max_candidates']:
            report['stop_reason'] = 'local_budget';break
        previous = measure_control_networks(current, comps, index, model)
        estimate = _wire_estimate(current, nets, index, par)
        organization = measure_regions(current, comps, index, plan)
        best = None
        for trial in _trials(current, comps, index, groups, options.grid_step):
            if perf_counter() >= until or quota['candidates'] >= quota['max_candidates']:
                report['stop_reason'] = 'local_budget';break
            report['candidates'] += 1;quota['candidates'] += 1
            if (len(set(trial.cell_of)) != len(comps) or not trial.blocks_contiguous()
                    or hasattr(trial, 'valid_state') and not trial.valid_state()):
                continue
            if any((trial.pos_of(i), trial.orient[i]) != (current.pos_of(i), current.orient[i])
                   for i in range(len(comps)) if i not in moving):
                continue
            after = measure_control_networks(trial, comps, index, model)
            if (not after['measurable'] or any(after[k] > previous[k]+EPS
                    for k in ('backward_distance', 'shape_errors', 'host_entry_reversals'))
                    or after['endpoint_distance'] > previous['endpoint_distance']+EPS
                    or not (after['endpoint_distance'] < previous['endpoint_distance']-EPS
                            or after['host_entry_reversals'] < previous['host_entry_reversals'])):
                continue
            measured = measure_regions(trial, comps, index, plan)
            if any(measured[k] > organization[k]+EPS for k in ('interleaving', 'order_errors', 'pin_axis_error')):
                continue
            cost = _wire_estimate(trial, nets, index, par)
            if cost > estimate*1.15+EPS:
                report['estimate_rejections'] += 1;continue
            try:
                if not _static_safe(par, trial, nets, index, options, until):
                    report['static_rejections'] += 1;continue
            except RoutingFailure:
                report['stop_reason'] = 'local_budget';break
            key = (after['backward_distance'], after['host_entry_reversals'],
                   after['shape_errors'], after['endpoint_distance'], cost)
            if best is None or key < best[0]:
                best = key, trial, after
        if best:
            _, trial, after = best
            changes = [dict(device=comps[i]['id'], before=list(current.cell_of[i]), after=list(trial.cell_of[i]),
                            orient_before=current.orient[i], orient_after=trial.orient[i],
                            dx_before=getattr(current, 'offset_x', [0.0]*len(comps))[i], dx_after=trial.offset_x[i])
                       for i in sorted(moving) if (trial.pos_of(i), trial.orient[i]) != (current.pos_of(i), current.orient[i])]
            report['actions'].append(dict(hosts=[g['host'] for g in groups], changes=changes,
                                          before=previous, after=after))
            current = trial
    report.update(applied=bool(report['actions']), after=measure_control_networks(current, comps, index, model),
                  elapsed_seconds=perf_counter()-started)
    if report['applied'] and report['stop_reason'] == 'no_legal_improvement':
        report['stop_reason'] = 'improved'
    quota['elapsed_seconds'] += report['elapsed_seconds']
    saved['local_budget'] = quota
    return current, result
