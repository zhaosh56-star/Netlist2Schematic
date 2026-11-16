"""Parallel passive paths on a proven pair of conduction junctions.

Semantic groups keep their ownership. Only a bounded initial transaction
allocates common display channels; SA never changes physical axes.
"""
from bisect import bisect_right
from collections import defaultdict
from copy import deepcopy
from math import ceil, isfinite
from statistics import mean
from time import perf_counter

VERSION = 'parallel-junction-channels-v1'
EPS = 1e-8


def compile_channels(comps, plan):
    by_id = {c['id']: c for c in comps}
    control_at = defaultdict(list)
    for comp in comps:
        for pin, net in comp['nodes'].items():
            if pin in ('G', 'B'):
                control_at[net].append([comp['id'], pin, net])
    axes = defaultdict(list)
    junctions = plan['readability_plan']['junctions']
    for axis in plan.get('host_axes', ()):
        axes[axis['net']].append(axis)
    families = defaultdict(list)
    for group in plan.get('groups', ()):
        ends = group.get('owner_junctions', ())
        if len(ends) == 2 and group.get('row_mode') != 'conduction_bridge':
            families[tuple(sorted(ends))].append(group)
    banks, skipped = [], []
    for ends, groups in sorted(families.items()):
        if not any(g['kind'] == 'multi_path_bridge' for g in groups):
            continue
        hosts, paths, seen = [], [], set()
        try:
            for net in ends:
                candidates = axes[net]
                if len(candidates) != 1 or junctions[net]['is_rail']:
                    raise ValueError('no_unique_conduction_axis')
                axis = candidates[0]
                ports = sorted(((axis['a'], axis['pin_a']), (axis['b'], axis['pin_b'])))
                if set(ports) != set(map(tuple, junctions[net]['conduction_ports'])):
                    raise ValueError('extra_conduction_terminal')
                hosts.append(dict(net=net, hosts=ports))
            for group in groups:
                alternatives = group.get('paths') if group['kind'] == 'multi_path_bridge' else [group]
                for path in alternatives:
                    members, nodes = list(path['members']), list(path['path'])
                    if not members or len(nodes) != len(members)+1 or set((nodes[0], nodes[-1])) != set(ends):
                        raise ValueError('unresolved_parallel_path')
                    if seen.intersection(members) or len(set(members)) != len(members):
                        raise ValueError('shared_path_member')
                    terminals = []
                    for k, name in enumerate(members):
                        pins = []
                        for net in nodes[k:k+2]:
                            matches = [pin for pin, value in by_id[name]['nodes'].items() if value == net]
                            if len(matches) != 1:
                                raise ValueError('ambiguous_passive_terminal')
                            pins.append(matches[0])
                        terminals.append([name, *pins])
                    seen.update(members)
                    paths.append(dict(group=group['id'], members=members, path=nodes, terminals=terminals))
            if len(paths) < 2 or len(paths) > 8:
                raise ValueError('parallel_channel_capacity')
            for path in paths:
                path['control_ports'] = sorted(port for net in path['path'][1:-1] for port in control_at[net]
                                               if port[0] not in seen)
            banks.append(dict(id='parallel-channel:'+'|'.join(sorted(g['id'] for g in groups)),
                groups=sorted(g['id'] for g in groups), members=sorted(seen), ends=hosts, paths=paths,
                evidence='independent_passive_paths_between_two_unique_conduction_junctions'))
        except (ValueError, KeyError, TypeError) as exc:
            skipped.append(dict(groups=sorted(g['id'] for g in groups), reason=str(exc)))
    return dict(version=VERSION, banks=banks, skipped=skipped)


def _geometry(pl, comps, index, bank):
    bands = []
    for end in bank['ends']:
        directions = {}
        for name, pin in end['hosts']:
            i = index[name]
            if comps[i]['nodes'].get(pin) != end['net']:
                raise ValueError('host_terminal_changed')
            point, direction = tuple(pl.pin_world(i, pin)), pl.pin_dir(i, pin)
            if direction not in ('north', 'south') or direction in directions or not all(map(isfinite, point)):
                raise ValueError('nonunique_vertical_access')
            directions[direction] = point
        if set(directions) != {'north', 'south'} or directions['south'][1] >= directions['north'][1]-EPS:
            raise ValueError('conduction_ports_do_not_face')
        bands.append((directions['south'][1], directions['north'][1]))
    lo, hi = max(v[0] for v in bands), min(v[1] for v in bands)
    if hi <= lo+EPS:
        raise ValueError('no_common_access_band')
    records = []
    for path in bank['paths']:
        points, outside, orientations = [], 0, 0
        for k, (name, before, after) in enumerate(path['terminals']):
            i = index[name]
            if (comps[i]['nodes'].get(before), comps[i]['nodes'].get(after)) != tuple(path['path'][k:k+2]):
                raise ValueError('passive_terminal_changed')
            a, b = tuple(pl.pin_world(i, before)), tuple(pl.pin_world(i, after))
            if not all(map(isfinite, (*a, *b))):
                raise ValueError('invalid_passive_point')
            orientations += abs(a[1]-b[1]) > EPS
            points.append(tuple(pl.pos_of(i)))
            if k == 0:
                outside += max(lo-a[1], 0, a[1]-hi)
            if k == len(path['terminals'])-1:
                outside += max(lo-b[1], 0, b[1]-hi)
        # Actual external control taps supply an order; unanchored alternatives
        # are exchangeable, not assigned an invented functional sign.
        controls = []
        ports = path.get('control_ports')
        if ports is None:
            ports = [(c['id'], pin, value) for net in path['path'][1:-1] for c in comps
                     for pin, value in c['nodes'].items()
                     if c['id'] not in bank['members'] and value == net and pin in ('G', 'B')]
        for name, pin, net in ports:
            if comps[index[name]]['nodes'].get(pin) != net:
                raise ValueError('control_terminal_changed')
            controls.append(pl.pin_world(index[name], pin)[1])
        records.append(dict(group=path['group'], members=path['members'],
            y=mean(p[1] for p in points), shape_error=(max(p[1] for p in points)-min(p[1] for p in points))/100,
            outside_distance=outside/100, orientation_errors=orientations,
            control_y=mean(controls) if controls else None))
    return lo, hi, records


def measure_channels(pl, comps, index, model):
    records, missing = [], []
    for bank in model.get('banks', ()):
        try:
            lo, hi, paths = _geometry(pl, comps, index, bank)
            origins = [[pl.pos_of(index[name])[0] for name, _ in end['hosts']] for end in bank['ends']]
            left, right = sorted(origins, key=mean)
            low_x, high_x = max(left), min(right)
            interior_errors = sum(not low_x+EPS < pl.pos_of(index[name])[0] < high_x-EPS
                                  for name in bank['members'])
            order_errors = 0
            for k, a in enumerate(paths):
                for b in paths[k+1:]:
                    ay = (lo+hi)/2 if a['control_y'] is None else a['control_y']
                    by = (lo+hi)/2 if b['control_y'] is None else b['control_y']
                    if abs(ay-by) > EPS and (a['y']-b['y'])*(ay-by) <= EPS:
                        order_errors += 1
            records.append(dict(id=bank['id'], groups=bank['groups'], band=[lo, hi], paths=paths,
                outside_errors=sum(p['outside_distance'] > EPS for p in paths),
                outside_distance=sum(p['outside_distance'] for p in paths),
                interior_errors=interior_errors,
                order_errors=order_errors, orientation_errors=sum(p['orientation_errors'] for p in paths),
                shape_error=sum(p['shape_error'] for p in paths)))
        except (KeyError, ValueError, TypeError, IndexError, AttributeError) as exc:
            missing.append(dict(id=bank['id'], reason=str(exc)))
    valid = bool(records) and not missing
    keys = ('outside_errors', 'outside_distance', 'interior_errors', 'order_errors', 'orientation_errors', 'shape_error')
    return dict(version=VERSION, count=len(model.get('banks', ())), measurable=valid,
        records=records, missing=missing, skipped=deepcopy(model.get('skipped', ())),
        **{key: sum(r[key] for r in records) if valid else None for key in keys})


def channel_rank(measurement):
    if not measurement.get('count'):
        return (0, 0, 0, 0, 0)
    if not measurement.get('measurable'):
        return None
    return (measurement['orientation_errors'], measurement['interior_errors'], measurement['outside_errors'],
            measurement['order_errors'], round(measurement['shape_error']+measurement['outside_distance'], 8))


def bodies_clear(par, pl, halo):
    """Cheap geometric state guard on newly dense layouts, not a route search.

It uses the original inflated symbol boxes. Exact fixed-stub and power-marker
checks remain in the independent static and final geometric verifiers.
"""
    boxes = []
    for i, comp in enumerate(pl.comps):
        x, y = pl.pos_of(i)
        box = par.GEO_DOC[comp['symbol']]['orientations'][pl.orient[i]]['viewBoxAABB']
        boxes.append((x+box['x']-halo, y+box['y']-halo,
                      x+box['x']+box['width']+halo, y+box['y']+box['height']+halo))
    boxes.sort(key=lambda b:b[0])
    for k, a in enumerate(boxes):
        for b in boxes[k+1:]:
            if b[0] >= a[2]-EPS:
                break
            if b[1] < a[3]-EPS and a[1] < b[3]-EPS:
                return False
    return True


def channel_trials(par, pl, comps, index, plan, bank, *, options, initialization, deadline=None):
    from netlist2schematic.plugins.attachment.attachment_integration import _fixed_ids, _install, _orient_path
    from netlist2schematic.plugins.attachment.attachment_taps import _split_row
    ids = {index[n] for n in bank['members']}
    if ids & _fixed_ids(pl, index):
        return
    try:
        lo, hi, measured = _geometry(pl, comps, index, bank)
    except (KeyError, ValueError, TypeError, IndexError, AttributeError):
        return
    center = (lo+hi)/2
    ordered = sorted(zip(bank['paths'], measured), key=lambda p: (
        center if p[1]['control_y'] is None else p[1]['control_y'], -len(p[0]['members']), tuple(p[0]['members'])))
    step = options.grid_step
    radius = {}
    for path, _ in ordered:
        heights = [par.GEO_DOC[comps[index[n]]['symbol']]['orientations'][o]['viewBoxAABB']['height']
                   for n in path['members'] for o in pl.allowed_orientations(index[n])
                   if par.GEO_DOC[comps[index[n]]['symbol']]['orientations'][o]['viewBoxAABB']['width'] >=
                      par.GEO_DOC[comps[index[n]]['symbol']]['orientations'][o]['viewBoxAABB']['height']]
        if not heights:
            return
        radius[tuple(path['members'])] = max(heights)/2+options.halo
    for reverse_neutral in (False, True):
        if deadline is not None and perf_counter() >= deadline:
            return
        order = list(ordered)
        if reverse_neutral:
            # A second deterministic alternative swaps only exchangeable paths.
            neutral = [k for k, (_, detail) in enumerate(order) if detail['control_y'] is None]
            if len(neutral) < 2:
                continue
            values = [order[k] for k in reversed(neutral)]
            for k, value in zip(neutral, values):
                order[k] = value
        half_heights = [radius[tuple(p['members'])] for p, _ in order]
        gaps = [ceil((a+b+step)/step)*step for a, b in zip(half_heights, half_heights[1:])]
        span = sum(gaps)
        needed = ceil(max(0, span+2*options.cross_clearance-(hi-lo))/step)*step
        if needed and not initialization:
            continue
        trial = deepcopy(pl)
        if needed:
            cut = bisect_right(trial.row_y, center)
            if not 0 < cut < len(trial.row_y):
                continue
            trial.row_y = [y+(needed if r >= cut else 0) for r, y in enumerate(trial.row_y)]
            lo2, hi2, _ = _geometry(trial, comps, index, bank)
        else:
            lo2, hi2 = lo, hi
        y = ceil(((lo2+hi2-span)/2)/step)*step
        rows = []
        valid = True
        for k, (path, _) in enumerate(order):
            anchors = [mean(trial.pin_world(index[name], pin)[0] for name, pin in end['hosts'])
                       for end in bank['ends']]
            # A conduction pin can lie10..20 units beyond its device origin.
            # That must not admit the host's own column as a passive slot.
            host_origins = [[trial.pos_of(index[name])[0] for name, _ in end['hosts']]
                            for end in bank['ends']]
            left, right = sorted(host_origins, key=mean)
            low_x, high_x = max(left), min(right)
            columns = [c for c, value in enumerate(trial.column_x) if low_x+EPS < value < high_x-EPS]
            if len(columns) < len(path['members']):
                valid = False
                break
            names = list(path['members'])
            if path['path'][0] != bank['ends'][0]['net']:
                names.reverse()
            if anchors[0] > anchors[1]:
                names.reverse()
            selected_columns = ([columns[len(columns)//2]] if len(names) == 1 else
                [columns[round(n*(len(columns)-1)/(len(names)-1))] for n in range(len(names))])
            if not initialization:
                choices = [r for r, value in enumerate(trial.row_y) if abs(value-y) < EPS]
                if not choices:
                    valid = False
                    break
                row = choices[0]
            else:
                row, _ = _split_row(trial, y)
                if row is None:
                    valid = False
                    break
            cells = list(trial.cell_of)
            if any(trial.comp_at.get((row, col)) not in (None, *ids) for col in selected_columns):
                valid = False
                break
            for name, col in zip(names, selected_columns):
                cells[index[name]] = row, col
            trial.set_state(cells, trial.orient)
            _orient_path(trial, comps, index, plan, path, axis=0)
            # Existing row numbers are shifted by later insertions, so resolve
            # actual membership only after the full transaction is built.
            rows.append(y)
            if k < len(gaps):
                y += gaps[k]
        if not valid:
            continue
        for group in bank['groups']:
            current_rows = {trial.cell_of[index[n]][0] for p in bank['paths'] if p['group'] == group for n in p['members']}
            trial.attachment_junction_rows[group] = tuple(sorted(
                set(trial.attachment_junction_rows.get(group, ())) | current_rows))
        trial = _install(trial, comps, index, plan, required=pl.attachment_required_embeddings)
        yield trial, dict(kind='parallel_junction_channels', group=bank['groups'][0], groups=bank['groups'],
            members=bank['members'], bank=bank['id'], row_y=rows, gap_added=needed,
            axis_before=list(pl.row_y), axis_after=list(trial.row_y),
            stage='initialization' if initialization else 'refinement')
