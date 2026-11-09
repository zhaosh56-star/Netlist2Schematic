"""Bounded passive-attachment seeding and endpoint-distance soft scoring.

Attachment cohorts exist only during initial packing. They never become rigid
SA blocks: the ordinary flexible-passive proposals keep their full freedom.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from math import isfinite
from statistics import median
from time import perf_counter

VERSION = 'passive-attachments-v2'
WEIGHT = 1.0
_PASSIVE = frozenset(('resistor', 'capacitor', 'inductor', 'diode'))
_RLC = frozenset(('R', 'L', 'C'))


def _rail(net):
    from netlist_core import SRC_ALIASES, SNK_ALIASES
    name = str(net).lower().rstrip('!')
    return name in SRC_ALIASES or name in SNK_ALIASES or name in (
        'vdd', 'vcc', 'vpwr', 'avdd', 'dvdd', '0', 'gnd', 'gnds', 'vss', 'agnd', 'dgnd')


def _pins(comps, index, name, net):
    return [pin for pin, value in comps[index[name]].get('nodes', {}).items() if value == net]


def _ordered_path(comps, index, names, suggested=None):
    if not names or any(name not in index for name in names):
        return None
    endpoints = [set(comps[index[name]].get('nodes', {}).values()) for name in names]
    if any(len(values) != 2 for values in endpoints):
        return None
    if suggested and len(suggested) == len(names)+1 and all(
            {suggested[i], suggested[i+1]} == values for i, values in enumerate(endpoints)):
        return list(suggested)
    if len(names) == 1:
        return sorted(endpoints[0])
    joints = [first & second for first, second in zip(endpoints, endpoints[1:])]
    if any(len(joint) != 1 for joint in joints):
        return None
    middle = [next(iter(joint)) for joint in joints]
    first = endpoints[0] - {middle[0]}
    last = endpoints[-1] - {middle[-1]}
    if len(first) != 1 or len(last) != 1:
        return None
    path = [next(iter(first)), *middle, next(iter(last))]
    return path if all({path[i], path[i+1]} == values for i, values in enumerate(endpoints)) else None


def _model(devices, comps, nets, index, relations):
    passive = {name for name, device in devices.items() if device.get('cls') == 'PASSIVE'
               and name in index and comps[index[name]].get('symbol') in _PASSIVE}
    incidence = defaultdict(list)
    for comp in comps:
        for pin, net in comp.get('nodes', {}).items():
            incidence[net].append((comp['id'], pin))
    groups, links, known = [], {}, set()

    def link(name, net, anchor, kind):
        if name not in passive or anchor is None or anchor[0] not in index:
            return
        pins = _pins(comps, index, name, net)
        if len(pins) != 1 or comps[index[anchor[0]]].get('nodes', {}).get(anchor[1]) != net:
            return
        a, b = (name, pins[0]), tuple(anchor)
        key = tuple(sorted((a, b))), net
        links.setdefault(key, {'endpoints': [list(a), list(b)], 'net': net, 'kind': kind})

    def anchor(net, exclude=()):
        choices = {(name, pin) for name, pin in incidence[net] if name not in passive
                   and name not in exclude and not _rail(net)}
        return next(iter(choices)) if len(choices) == 1 else None

    def group(kind, names, **details):
        names = list(names)
        if not names or len(set(names)) != len(names) or not set(names) <= passive:
            return
        anchors = tuple(tuple(details[key]) for key in ('anchor', 'start', 'end') if details.get(key))
        identity = kind, tuple(names), anchors
        if identity in known:
            return
        known.add(identity)
        suffix = ''.join('@' + ':'.join(endpoint) for endpoint in anchors)
        groups.append({'id': 'attachment:' + kind + ':' + '|'.join(names) + suffix,
                       'kind': kind, 'members': names, **details})

    parallels = defaultdict(list)
    for name in sorted(passive):
        dev = devices[name]
        if dev.get('type') in _RLC and dev['a'] != dev['b']:
            parallels[tuple(sorted((dev['a'], dev['b'])))].append(name)
    load_members = set()
    for ends, names in parallels.items():
        rail = [net for net in ends if _rail(net)]
        if len(names) < 2 or len(rail) != 1:
            continue
        signal = ends[1] if ends[0] == rail[0] else ends[0]
        conductive, ambiguous = [], False
        for name, pin in incidence[signal]:
            comp = comps[index[name]]
            symbol = comp['symbol']
            if symbol in ('nmos', 'pmos', 'npn', 'pnp'):
                if pin in ('D', 'C'):
                    conductive.append((name, pin))
                elif pin in ('S', 'E'):
                    ambiguous = True
                # Extra gates/bases, including cross-coupled feedback, do not
                # become a second conduction anchor.
            elif symbol in ('voltage-source', 'current-source') or symbol in ('opamp', 'vcvs') and pin == 'OUT':
                ambiguous = True
        if ambiguous or len(conductive) != 1:
            continue
        target = conductive[0]
        group('parallel_load', sorted(names), rail=rail[0], node=signal,
              anchor=list(target), evidence='same_two_nets_and_unique_conduction_anchor')
        for name in names:
            link(name, signal, target, 'parallel_load')
        load_members.update(names)

    for relation in relations:
        kind, roles = relation.get('kind'), relation.get('roles') or {}
        names = relation.get('members', relation.get('devices', ()))
        if kind == 'parallel_passives':
            names = sorted(name for name in names if name in passive)
            if len(names) < 2 or set(names) <= load_members:
                continue
            ends = [set(comps[index[name]].get('nodes', {}).values()) for name in names]
            if len(ends[0]) != 2 or any(values != ends[0] for values in ends[1:]):
                continue
            targets = [value for net in sorted(ends[0]) if (value := anchor(net)) is not None]
            group('parallel', names, anchors=[list(value) for value in targets])
            for net in ends[0]:
                for name in names:
                    link(name, net, anchor(net), 'parallel')
        elif kind == 'series_chain':
            ordered = list(roles.get('ordered', ()))
            suggested = relation.get('evidence', {}).get('nets', {}).get('path')
            path = _ordered_path(comps, index, ordered, suggested)
            if not path or not set(ordered) <= passive:
                continue
            start, end = anchor(path[0]), anchor(path[-1])
            group('series', ordered, path=path,
                  start=list(start) if start else None, end=list(end) if end else None)
            link(ordered[0], path[0], start, 'series')
            link(ordered[-1], path[-1], end, 'series')
        elif kind == 'feedback_network':
            amplifier = roles.get('amplifier')
            if amplifier not in index or comps[index[amplifier]].get('symbol') not in ('opamp', 'vcvs'):
                continue
            input_pin = {'plus': 'IN+', 'minus': 'IN-'}.get(roles.get('input'))
            nodes = comps[index[amplifier]].get('nodes', {})
            ordered = list(roles.get('series', ()))
            if input_pin not in nodes or not ordered or not set(ordered) <= passive:
                continue
            suggested = relation.get('evidence', {}).get('nets', {}).get('path')
            path = _ordered_path(comps, index, ordered, suggested)
            if path and path[0] == nodes.get(input_pin) and path[-1] == nodes.get('OUT'):
                path, ordered = list(reversed(path)), list(reversed(ordered))
            if not path or path[0] != nodes.get('OUT') or path[-1] != nodes[input_pin]:
                continue
            first, last = (amplifier, 'OUT'), (amplifier, input_pin)
            group('feedback', ordered, path=path, start=list(first), end=list(last))
            link(ordered[0], path[0], first, 'feedback')
            link(ordered[-1], path[-1], last, 'feedback')
            for name in roles.get('shunts', ()):
                if name in passive and nodes[input_pin] in comps[index[name]].get('nodes', {}).values():
                    group('feedback_shunt', [name], anchor=list(last), node=nodes[input_pin])
                    link(name, nodes[input_pin], last, 'feedback')

    priority = {'parallel_load': 0, 'parallel': 1, 'feedback': 2, 'series': 3, 'feedback_shunt': 4}
    groups.sort(key=lambda row: (priority[row['kind']], row['id']))
    return {'version': VERSION, 'groups': groups,
            'links': sorted(links.values(), key=lambda row: (row['net'], row['endpoints']))}


def build_attachment_model(devices, comps, nets, index, relations):
    """Public, source-derived attachment table shared by reference/new seeds."""
    return _model(devices, comps, nets, index, relations)


def measure_parallel_coherence(pl, index, groups):
    """Compare horizontal/vertical banks without prescribing a rigid template.

    Horizontal banks use vertical symbols, vertical banks horizontal symbols.
    Both account for visible endpoints, extra spread, and unrelated interlopers.
    """
    records = []
    columns, rows = getattr(pl, 'column_x', ()), getattr(pl, 'row_y', ())
    pitch_x = median(b-a for a, b in zip(columns, columns[1:])) if len(columns) > 1 else 150
    pitch_y = median(b-a for a, b in zip(rows, rows[1:])) if len(rows) > 1 else 150
    for group in groups:
        if group.get('kind') != 'parallel_load':
            continue
        names = group.get('members', ())
        if len(names) < 2 or any(name not in index for name in names):
            continue
        ids = {index[name] for name in names}
        positions = [pl.pos_of(index[name]) for name in names]
        xs, ys = zip(*positions)
        mx, my = median(xs), median(ys)
        endpoint_x, endpoint_y = [], []
        vertical, horizontal = [], []
        valid = True
        for net in (group.get('rail'), group.get('node')):
            endpoints = []
            for name in names:
                pins = _pins(pl.comps, index, name, net)
                if len(pins) != 1:
                    valid = False
                    break
                endpoints.append(pl.pin_world(index[name], pins[0]))
            if not valid:
                break
            xx, yy = zip(*endpoints)
            endpoint_x.extend(abs(x-median(xx)) for x in xx)
            endpoint_y.extend(abs(y-median(yy)) for y in yy)
        if not valid:
            continue
        for name in names:
            pins = list(pl.comps[index[name]]['nodes'])
            a, b = pl.pin_world(index[name], pins[0]), pl.pin_world(index[name], pins[1])
            vertical.append(abs(a[0]-b[0]) <= 1e-8)
            horizontal.append(abs(a[1]-b[1]) <= 1e-8)
        foreign_x = foreign_y = 0
        for i in range(len(pl.comps)):
            if i in ids:
                continue
            x, y = pl.pos_of(i)
            foreign_x += min(xs) < x < max(xs) and abs(y-my) <= pitch_y*.45
            foreign_y += min(ys) < y < max(ys) and abs(x-mx) <= pitch_x*.45
        horizontal_error = ((sum(abs(y-my) for y in ys)/len(names)
                             + sum(endpoint_y)/len(endpoint_y)
                             + max(0, max(xs)-min(xs)-(len(names)-1)*pitch_x))/100
                            + 1-sum(vertical)/len(names) + foreign_x/len(names))
        vertical_error = ((sum(abs(x-mx) for x in xs)/len(names)
                           + sum(endpoint_x)/len(endpoint_x)
                           + max(0, max(ys)-min(ys)-(len(names)-1)*pitch_y))/100
                          + 1-sum(horizontal)/len(names) + foreign_y/len(names))
        records.append({'id': group['id'], 'members': list(names),
                        'horizontal': horizontal_error, 'vertical': vertical_error,
                        'coherence': min(horizontal_error, vertical_error),
                        'shape': 'horizontal_bank' if horizontal_error <= vertical_error else 'vertical_bank'})
    return {'coherence': sum(row['coherence'] for row in records)/max(1, len(records)),
            'group_count': len(records), 'groups': records, 'weight': 1.0,
            'distance_unit': 100.0}


def score_attachments(pl):
    """Mean unique endpoint/owner distance in 100px units; old weights stay put."""
    model = getattr(pl, 'attachment_model', {})
    index = getattr(pl, 'attachment_index', {})
    by_kind = defaultdict(list)
    for row in model.get('links', ()):
        a, b = row['endpoints']
        if a[0] not in index or b[0] not in index:
            continue
        first, second = pl.pin_world(index[a[0]], a[1]), pl.pin_world(index[b[0]], b[1])
        distance = (abs(first[0]-second[0])+abs(first[1]-second[1]))/100.0
        by_kind[row['kind']].append(distance)
    values = [value for rows in by_kind.values() for value in rows]
    distance = sum(values)/max(1, len(values))
    result = {'version': VERSION, 'attachment_distance': distance, 'weighted_total': WEIGHT*distance,
            'weight': WEIGHT, 'distance_unit': 100.0, 'link_count': len(values),
            'group_count': len(model.get('groups', ())),
            'by_kind': {kind: sum(rows)/len(rows) for kind, rows in sorted(by_kind.items())}}
    if any(row.get('kind') == 'parallel_load' for row in model.get('groups', ())):
        coherence = measure_parallel_coherence(pl, index, model['groups'])
        result.update(parallel_coherence=coherence['coherence'], parallel_coherence_details=coherence)
        result['weighted_total'] += coherence['coherence']
    return result


def _restore(pl, cells, orient):
    pl.set_state(cells, orient)
    pl.comp_at = {tuple(cell): i for i, cell in enumerate(pl.cell_of)}
    if hasattr(pl, 'row_of'):
        pl.row_of = [row for row, _ in pl.cell_of]


def _legal(pl, original, comps, fixed):
    if len(pl.comp_at) != len(comps) or len(set(pl.cell_of)) != len(comps):
        return False
    if pl.column_x != original.column_x or pl.row_y != original.row_y:
        return False
    for i, (row, col) in enumerate(pl.cell_of):
        if not (0 <= row < len(pl.row_y) and 0 <= col < len(pl.column_x)):
            return False
        if (i in fixed or comps[i]['symbol'] not in _PASSIVE) and (
                pl.cell_of[i] != original.cell_of[i] or pl.orient[i] != original.orient[i]):
            return False
        if hasattr(pl, 'allowed_orientations') and pl.orient[i] not in pl.allowed_orientations(i):
            return False
    if hasattr(pl, 'valid_state') and not pl.valid_state():
        return False
    return not hasattr(pl, 'blocks_contiguous') or pl.blocks_contiguous()


def _available(pl, members, targets, index):
    ids = {index[name] for name in members}
    return len(targets) == len(set(targets)) and all(
        pl.comp_at.get(cell) in ids or pl.comp_at.get(cell) is None for cell in targets)


def _orientations(pl, members, index, horizontal):
    # Use only already-allowed variants; never invent a terminal swap or 180deg
    # orientation forbidden by the current placement policy.
    result = list(pl.orient)
    for name in members:
        i = index[name]
        allowed = list(pl.allowed_orientations(i)) if hasattr(pl, 'allowed_orientations') else [pl.orient[i]]
        preferred = 1 if horizontal else 0
        result[i] = preferred if preferred in allowed else pl.orient[i]
    return result


def _targets(pl, group, index):
    members = group['members']
    length, columns = len(members), len(pl.column_x)
    if length > columns:
        return
    kind = group['kind']
    if kind in ('parallel', 'parallel_load'):
        target = group.get('anchor') or next(iter(group.get('anchors', ())), None)
        if target:
            i = index[target[0]]
            row, col = pl.cell_of[i]
            direction = pl.pin_dir(i, target[1])
            rows = [row-1, row+1] if direction == 'north' else [row+1, row-1]
            center = pl.pin_world(i, target[1])[0]
        else:
            rows = sorted({pl.cell_of[index[name]][0] for name in members})
            center = median(pl.pos_of(index[name])[0] for name in members)
        for row in rows:
            if not 0 <= row < len(pl.row_y):
                continue
            starts = sorted(range(columns-length+1), key=lambda col: (
                abs((pl.column_x[col]+pl.column_x[col+length-1])/2-center), col))
            for start in starts:
                yield [(row, start+i) for i in range(length)], False
    else:
        first = group.get('start') or group.get('anchor')
        last = group.get('end')
        reverse = not first and bool(last)
        if not first:
            first = last
        if not first:
            return
        a = index[first[0]]
        row, col = pl.cell_of[a]
        port = pl.pin_dir(a, first[1])
        # Local chains follow the actual port side. Both orientations of the
        # chain are tried as geometries, while its electrical path stays fixed.
        row_choices = [row, row-1, row+1]
        direction = -1 if port == 'west' else 1
        if last:
            b = index[last[0]]
            last_x = pl.pin_world(b, last[1])[0]
            first_x = pl.pin_world(a, first[1])[0]
            direction = -1 if last_x < first_x else 1 if last_x > first_x else direction
        for rr in row_choices:
            if not 0 <= rr < len(pl.row_y):
                continue
            starts = sorted(range(columns), key=lambda start: (abs(start-col), start))
            for start in starts:
                target_cols = [start + direction*i for i in range(length)]
                if reverse:
                    target_cols.reverse()
                if all(0 <= c < columns for c in target_cols):
                    yield [(rr, c) for c in target_cols], True


def prepare_attachments(pl, devices, comps, nets, index, relations):
    """Return an independent initial seed and report; no route search is used."""
    original, result = deepcopy(pl), deepcopy(pl)
    model = _model(devices, comps, nets, index, relations)
    result.attachment_model = model
    result.attachment_index = dict(index)
    before = score_attachments(result)
    report = {'version': VERSION, 'policy': 'soft', 'applied_stage': 'before_sa',
              'groups': deepcopy(model['groups']), 'links': deepcopy(model['links']),
              'before': before, 'after': deepcopy(before), 'actions': [], 'skipped': [],
              'weight': WEIGHT, 'status': 'unchanged'}
    if getattr(result, 'passive_policy', None) != 'flexible':
        report.update(status='skipped', reason='fixed_passive_rows')
        return result, report
    if not all(hasattr(result, key) for key in ('set_state', 'column_x', 'row_y', 'comp_at')):
        report.update(status='skipped', reason='unsupported_placement')
        return result, report
    fixed = set(getattr(result, 'fixed', ())) | set(getattr(result, 'fixed_cells', {}))
    placed = set()
    for group in model['groups']:
        members = group['members']
        ids = {index[name] for name in members}
        reason = ('fixed_member' if ids & fixed else
                  'shared_members_already_placed' if ids & placed else None)
        if reason:
            report['skipped'].append({'group': group['id'], 'reason': reason})
            continue
        saved_cells, saved_orient = list(result.cell_of), list(result.orient)
        best, best_key, trials = None, None, 0
        for targets, horizontal in _targets(result, group, index):
            trials += 1
            if trials > 128:
                break
            if not _available(result, members, targets, index):
                continue
            cells = list(saved_cells)
            for name, cell in zip(members, targets):
                cells[index[name]] = cell
            orientations = _orientations(result, members, index, horizontal)
            _restore(result, cells, orientations)
            if _legal(result, original, comps, fixed):
                score = score_attachments(result)['weighted_total']
                movement = sum(abs(cells[i][0]-saved_cells[i][0])+abs(cells[i][1]-saved_cells[i][1]) for i in ids)
                key = score, movement, tuple(targets), tuple(orientations[i] for i in sorted(ids))
                if best_key is None or key < best_key:
                    best_key, best = key, (list(cells), list(orientations))
            _restore(result, saved_cells, saved_orient)
        if best is None:
            report['skipped'].append({'group': group['id'], 'reason': 'no_safe_existing_cells', 'trials': trials})
            continue
        _restore(result, *best)
        placed.update(ids)
        changes = [{'device': comps[i]['id'], 'before': list(saved_cells[i]),
                    'after': list(result.cell_of[i]), 'orientation_before': saved_orient[i],
                    'orientation_after': result.orient[i]} for i in sorted(ids)
                   if saved_cells[i] != result.cell_of[i] or saved_orient[i] != result.orient[i]]
        report['actions'].append({'group': group['id'], 'kind': group['kind'],
                                  'changes': changes, 'trials': trials})
    report['after'] = score_attachments(result)
    report['status'] = 'prepared' if any(row['changes'] for row in report['actions']) else 'unchanged'
    return result, report


def refine_parallel_attachments(par, pl, comps, nets, index, metadata, *, options=None,
                                deadline=None, seconds=1.0, max_candidates=128):
    """Try complete parallel banks after SA, before wires exist, transactionally.

    No-bank calls return the original objects. The caller can share the absolute
    deadline and candidate allowance with physical branch alignment.
    """
    groups = metadata.get('attachment_measurement_groups',
                          getattr(pl, 'attachment_model', {}).get('groups', ()))
    groups = [row for row in groups if row.get('kind') == 'parallel_load']
    if not groups:
        return pl, metadata
    if type(seconds) not in (int, float) or not isfinite(seconds) or seconds < 0:
        raise ValueError('parallel refinement seconds must be finite and non-negative')
    if type(max_candidates) is not int or max_candidates < 0:
        raise ValueError('parallel refinement candidate budget must be a non-negative integer')
    from routing_rules import RoutingOptions
    from symmetry_layout import _static_safe
    from branch_alignment import _hpwl
    from orthogonal_router import RoutingFailure
    options = options or RoutingOptions()
    if not isinstance(options, RoutingOptions):
        raise ValueError('parallel refinement options must be RoutingOptions')
    started = perf_counter()
    until = min(started+seconds, deadline) if deadline is not None else started+seconds
    placement, metadata = deepcopy(pl), deepcopy(metadata)
    original = deepcopy(placement)
    fixed = {index[item] if isinstance(item, str) and item in index else item
             for item in set(getattr(pl, 'fixed', ())) | set(getattr(pl, 'fixed_cells', {}))}
    before = measure_parallel_coherence(placement, index, groups)
    report = {'version': VERSION, 'stage': 'after_sa_before_final_routing',
              'status': 'unchanged', 'actions': [], 'candidates': 0, 'accepted': 0,
              'static_rejections': 0, 'hpwl_rejections': 0,
              'before': deepcopy(before), 'after': deepcopy(before), 'current': deepcopy(before),
              'budget_seconds': seconds, 'max_candidates': max_candidates,
              'estimated_hpwl_ratio_limit': 1.15}
    metadata['parallel_attachment_refinement'] = report
    if (getattr(placement, 'passive_policy', None) != 'flexible'
            or not all(hasattr(placement, name) for name in ('set_state', 'column_x', 'row_y', 'comp_at'))
            or getattr(placement, 'power_connections', None) is not None):
        report.update(status='skipped', stop_reason='unsupported_or_fixed_placement',
                      elapsed_seconds=perf_counter()-started)
        return placement, metadata
    baseline_hpwl = _hpwl(par, placement, nets, index)
    report['estimated_hpwl_before'] = baseline_hpwl
    current, stop, seen = before, 'no_improvement', set()
    for group in groups:
        names = group['members']
        if any(name not in index for name in names) or len(names) < 2:
            continue
        ids = {index[name] for name in names}
        if ids & fixed:
            continue
        anchor = group.get('anchor')
        if not anchor or anchor[0] not in index:
            continue
        # Verify the table still refers to the same real two-node cohort.
        if any(set(comps[index[name]].get('nodes', {}).values()) != {
                group.get('node'), group.get('rail')} for name in names):
            continue
        ax, ay = placement.pin_world(index[anchor[0]], anchor[1])
        candidates, length = [], len(names)
        for horizontal in (False, True):
            for row in range(len(placement.row_y)-(length-1 if horizontal else 0)):
                if perf_counter() >= until:
                    break
                for col in range(len(placement.column_x)-(0 if horizontal else length-1)):
                    if perf_counter() >= until:
                        break
                    targets = [(row+i, col) if horizontal else (row, col+i) for i in range(length)]
                    if not _available(placement, names, targets, index):
                        continue
                    centers = [(placement.column_x[c], placement.row_y[r]) for r, c in targets]
                    distance = sum(abs(x-ax)+abs(y-ay) for x, y in centers)/length
                    candidates.append((distance, tuple(targets), horizontal))
        best = None
        for _, targets, horizontal in sorted(candidates):
            if perf_counter() >= until:
                stop = 'time_budget'
                break
            if report['candidates'] >= max_candidates:
                stop = 'candidate_limit'
                break
            cells, orientations = list(placement.cell_of), _orientations(placement, names, index, horizontal)
            for name, cell in zip(names, targets):
                cells[index[name]] = cell
            identity = tuple(cells), tuple(orientations)
            if identity in seen:
                continue
            seen.add(identity)
            report['candidates'] += 1
            trial = deepcopy(placement)
            _restore(trial, cells, orientations)
            if not _legal(trial, original, comps, fixed):
                continue
            measured = measure_parallel_coherence(trial, index, groups)
            if measured['coherence'] >= current['coherence']-1e-8:
                continue
            hpwl = _hpwl(par, trial, nets, index)
            if hpwl > baseline_hpwl*1.15+1e-8:
                report['hpwl_rejections'] += 1
                continue
            movement = sum(abs(cells[i][0]-placement.cell_of[i][0])
                           + abs(cells[i][1]-placement.cell_of[i][1]) for i in ids)
            key = measured['coherence'], hpwl, movement, identity
            if best is not None and key >= best[0]:
                continue
            try:
                safe = _static_safe(par, trial, nets, index, options, until)
            except RoutingFailure:
                if perf_counter() >= until:
                    stop = 'time_budget'
                    break
                raise
            if not safe:
                report['static_rejections'] += 1
                continue
            best = key, trial, measured
        if best is not None:
            previous = placement
            placement, current = best[1], best[2]
            report['accepted'] += 1
            report['actions'].append({'group': group['id'], 'after': deepcopy(current),
                'changes': [{'device': comps[i]['id'], 'from': list(previous.cell_of[i]),
                             'to': list(placement.cell_of[i]),
                             'orientation_before': previous.orient[i], 'orientation_after': placement.orient[i]}
                            for i in sorted(ids) if previous.cell_of[i] != placement.cell_of[i]
                            or previous.orient[i] != placement.orient[i]]})
        if stop in ('time_budget', 'candidate_limit'):
            break
    if report['accepted'] and stop == 'no_improvement':
        stop = 'all_groups_scanned'
    report.update(status='improved' if report['accepted'] else 'unchanged', stop_reason=stop,
                  after=deepcopy(current), current=deepcopy(current),
                  estimated_hpwl_after=_hpwl(par, placement, nets, index),
                  elapsed_seconds=perf_counter()-started)
    return placement, metadata
