"""Finite, physical channels for independent paths across proven junctions.

This consumes the existing parallel-channel evidence. It never creates host
ownership, grows axes, searches routes, or reads a ground-truth drawing.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from itertools import islice
from math import ceil
from statistics import median
from time import perf_counter

VERSION = 'physical-path-banks-v1'
PASSIVE = frozenset(('resistor', 'capacitor', 'inductor'))
EPS = 1e-8


def compile_banks(comps, plan):
    by_id = {c['id']: c for c in comps}
    incidence = defaultdict(list)
    for c in comps:
        for pin, net in c['nodes'].items():
            incidence[net].append((c['id'], pin))
    candidates = plan.get('parallel_channels', {}).get('banks', ())
    membership = Counter(n for b in candidates for n in b['members'])
    managed = set(plan.get('source_managed_members', ()))
    banks, skipped = [], []
    for bank in candidates:
        reason = None
        if managed.intersection(bank['members']) or any(membership[n] != 1 for n in bank['members']):
            reason = 'shared_or_source_managed_members'
        elif any(by_id[n]['symbol'] not in PASSIVE or len(by_id[n]['nodes']) != 2
                 for n in bank['members']):
            reason = 'non_RCL_path'
        for path in bank['paths']:
            for k, net in enumerate(path['path'][1:-1], 1):
                own = set(path['members'][k-1:k+1])
                expected = {(n, p) for n in own for p, v in by_id[n]['nodes'].items() if v == net}
                extras = set(incidence[net]) - expected
                controls = {(n, p) for n, p, value in path['control_ports'] if value == net}
                if len(expected) != 2 or extras != controls:
                    reason = 'branched_or_changed_internal_node'
        if reason:
            skipped.append(dict(id=bank['id'], reason=reason))
            continue
        names = set(bank['members']) | {n for e in bank['ends'] for n, p in e['hosts']}
        names |= {n for p in bank['paths'] for n, pin, net in p['control_ports']}
        banks.append(dict(deepcopy(bank),
            terminals={n: dict(by_id[n]['nodes']) for n in sorted(names)},
            symbols={n: by_id[n]['symbol'] for n in sorted(names)}))
    return dict(version=VERSION, banks=banks, skipped=skipped)


def _window(pl, comps, index, bank):
    from netlist2schematic.plugins.attachment.attachment_channels import _geometry
    if any(comps[index[n]]['symbol'] != bank['symbols'][n] or comps[index[n]]['nodes'] != pins
           for n, pins in bank['terminals'].items()):
        raise ValueError('terminal_or_symbol_contract_changed')
    lo, hi, records = _geometry(pl, comps, index, bank)
    xs = {e['net']: median(pl.pin_world(index[n], p)[0] for n, p in e['hosts'])
          for e in bank['ends']}
    if max(xs.values()) - min(xs.values()) <= EPS:
        raise ValueError('no_horizontal_interface_span')
    return lo, hi, xs, records


def measure_banks(pl, comps, index, model):
    records, missing = [], []
    for bank in model.get('banks', ()):
        try:
            lo, hi, xs, channels = _window(pl, comps, index, bank)
            outside = directions = order = 0
            alignment = internal = 0.
            paths = []
            for path, channel in zip(bank['paths'], channels):
                sign = 1 if xs[path['path'][-1]] > xs[path['path'][0]] else -1
                pairs = []
                centers = [pl.pos_of(index[n]) for n in path['members']]
                for n, before, after in path['terminals']:
                    i = index[n]; a, b = pl.pin_world(i, before), pl.pin_world(i, after)
                    directions += pl.pin_dir(i, before) != ('west' if sign > 0 else 'east')
                    directions += pl.pin_dir(i, after) != ('east' if sign > 0 else 'west')
                    pairs.append((a, b))
                alignment += sum(abs(a[1]-b[1]) for a, b in pairs)
                alignment += sum(abs(a[1][1]-b[0][1]) for a, b in zip(pairs, pairs[1:]))
                internal += sum(abs(a[1][0]-b[0][0])+abs(a[1][1]-b[0][1])
                                for a, b in zip(pairs, pairs[1:]))
                outside += sum(not (min(xs.values())+EPS < x < max(xs.values())-EPS
                                    and lo-EPS <= y <= hi+EPS) for x, y in centers)
                order += sum(sign*(b[0]-a[0]) <= EPS for a, b in zip(centers, centers[1:]))
                paths.append(dict(group=path['group'], members=path['members'],
                    positions={n: list(pl.pos_of(index[n])) for n in path['members']},
                    control_y=channel['control_y'], y=channel['y']))
            for k, a in enumerate(paths):
                for b in paths[k+1:]:
                    ay = (lo+hi)/2 if a['control_y'] is None else a['control_y']
                    by = (lo+hi)/2 if b['control_y'] is None else b['control_y']
                    if abs(ay-by) > EPS and (a['y']-b['y'])*(ay-by) <= EPS:
                        order += 1
            records.append(dict(id=bank['id'], paths=paths, access_band=[lo, hi],
                outside_errors=outside, direction_errors=directions, order_errors=order,
                pin_alignment_error=alignment, internal_wirelength=internal,
                channel_span=max(p['y'] for p in paths)-min(p['y'] for p in paths)))
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            missing.append(dict(id=bank['id'], reason=str(exc)))
    valid = bool(records) and not missing
    return dict(version=VERSION, count=len(model.get('banks', ())), measurable=valid,
        records=records, missing=missing, skipped=deepcopy(model.get('skipped', ())),
        **{k: sum(r[k] for r in records) if valid else None for k in (
            'outside_errors', 'direction_errors', 'order_errors', 'pin_alignment_error',
            'internal_wirelength', 'channel_span')})


def bank_rank(measured):
    if not measured.get('count'):
        return (0, 0, 0, 0)
    if not measured.get('measurable'):
        return None
    return (measured['outside_errors']+measured['direction_errors']+measured['order_errors'],
        round(measured['pin_alignment_error']/100, 8),
        round(measured['internal_wirelength']/100, 8), round(measured['channel_span']/100, 8))


def reserve_finish(plan, budget):
    count = len(plan.get('physical_path_banks', {}).get('banks', ()))
    if count and not budget.get('path_bank_finish_done'):
        budget['path_bank_reserve'] = min(8, 4*count, budget['max_candidates']//8)


def bank_trials(par, pl, comps, nets, index, bank, options, deadline=None, diagnostic=None):
    diagnostic = {} if diagnostic is None else diagnostic
    if deadline is not None and perf_counter() >= deadline:
        diagnostic['stop_reason'] = 'deadline'; return
    from netlist2schematic.plugins.attachment.attachment_integration import _fixed_ids
    ids = {index[n] for n in bank['members']}
    if ids & _fixed_ids(pl, index):
        diagnostic['stop_reason'] = 'fixed_guest'; return
    if getattr(pl, 'power_connections', None) is not None:
        diagnostic['stop_reason'] = 'custom_power_geometry'; return
    try:
        lo, hi, xs, channels = _window(pl, comps, index, bank)
    except (KeyError, ValueError, IndexError, TypeError) as exc:
        diagnostic['stop_reason'] = str(exc); return
    template = deepcopy(pl); offsets = {}; step = options.grid_step
    mid = (lo+hi)/2; xmin, xmax = min(xs.values()), max(xs.values())
    ordered = sorted(zip(bank['paths'], channels), key=lambda p: (
        mid if p[1]['control_y'] is None else p[1]['control_y'], -len(p[0]['members']),
        tuple(comps[index[n]]['symbol'] for n in p[0]['members']), p[1]['y']))
    for path, channel in ordered:
        sign = 1 if xs[path['path'][-1]] > xs[path['path'][0]] else -1
        for n, before, after in path['terminals']:
            i = index[n]; choices = []
            for ori in pl.allowed_orientations(i):
                g = {p['name']: p for p in par.GEO_DOC[comps[i]['symbol']]['orientations'][ori]['pins']}
                if (g[before]['direction'] == ('west' if sign > 0 else 'east')
                        and g[after]['direction'] == ('east' if sign > 0 else 'west')
                        and abs(g[before]['at']['y']-g[after]['at']['y']) <= EPS):
                    choices.append((ori, g[before]['at']['y']))
            if not choices:
                diagnostic['stop_reason'] = 'no_legal_straight_orientation'; return
            ori, offsets[i] = min(choices, key=lambda p: (p[0] != pl.orient[i], p[0]))
            template.orient[i] = ori
    from netlist2schematic.routing.routing_rules import build_scene
    from netlist2schematic.plugins.attachment.attachment_physical import owned_footprints
    scene = build_scene(par, nets, template, index, options=options, deadline=deadline)
    diagnostic['scene_templates'] = 1
    boxes = owned_footprints(par, scene, nets, index)
    relative = {}
    for i in ids:
        x, y = template.pos_of(i); a, b, c, d = boxes[i]
        relative[i] = a-x, b-y-offsets[i], c-x, d-y-offsets[i]
    seen = set()
    for gap in sorted({0., step, options.center_spacing}):
        tracks = []; feasible = True
        for path, channel in ordered:
            members = [index[n] for n in path['members']]
            if xs[path['path'][0]] > xs[path['path'][-1]]:
                members.reverse()
            positions = [0.]
            for a, b in zip(members, members[1:]):
                positions.append(positions[-1]+relative[a][2]-relative[b][0]+gap)
            base = (xmin+xmax-positions[-1])/2
            snapped = []
            for k, x in enumerate(positions):
                want = base if not k else snapped[-1]+x-positions[k-1]
                snapped.append(xmin+ceil((want-xmin)/step)*step)
            if not all(xmin+EPS < x < xmax-EPS for x in snapped):
                feasible = False; break
            tracks.append(dict(path=path, members=members, xs=snapped,
                bottom=max(relative[i][3] for i in members), top=min(relative[i][1] for i in members)))
        if not feasible:
            continue
        rows = [0.]
        for a, b in zip(tracks, tracks[1:]):
            rows.append(rows[-1]+ceil((a['bottom']-b['top']+options.center_spacing)/step)*step)
        if rows[-1] > hi-lo-2*step+EPS:
            diagnostic['insufficient_vertical_room'] = True; continue
        # Finite offsets keep every path inside the same real access band.
        lower, upper = lo+step, hi-step-rows[-1]
        center = (lower+upper)/2
        bases = [center, lower, upper, median(c['y'] for p, c in ordered)-median(rows)]
        for value in bases:
            base = lo+round((min(upper, max(lower, value))-lo)/step)*step
            targets = {i: (x, base+y-offsets[i]) for t, y in zip(tracks, rows)
                       for i, x in zip(t['members'], t['xs'])}
            key = tuple((i, *targets[i], template.orient[i]) for i in sorted(ids))
            if key in seen or all(tuple(pl.pos_of(i)) == targets[i] and pl.orient[i] == template.orient[i] for i in ids):
                continue
            if deadline is not None and perf_counter() >= deadline:
                diagnostic['stop_reason'] = 'deadline'; return
            seen.add(key); trial = deepcopy(template)
            local = list(getattr(pl, 'passive_local_offsets', [(0, 0)]*len(comps)))
            for i, (x, y) in targets.items():
                ox, oy = local[i]; px, py = pl.pos_of(i)
                local[i] = x-px+ox, y-py+oy
            trial.passive_local_offsets = local
            diagnostic['generated'] = diagnostic.get('generated', 0)+1
            yield trial, dict(kind='physical_path_bank', version=VERSION, bank=bank['id'],
                members=bank['members'], access_band=[lo, hi], fixed_escape_gap=gap,
                positions={comps[i]['id']: list(p) for i, p in targets.items()},
                logical_cells_axes_and_hosts_preserved=True)
    diagnostic.setdefault('stop_reason', 'enumerated_or_insufficient_room')


def finish_banks(par, current, comps, nets, index, plan, options, budget, until, report, *, hpwl_limit):
    from netlist2schematic.plugins.attachment import attachment_integration as engine
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    from netlist2schematic.routing.routing_rules import RoutingOptions
    from netlist2schematic.routing.orthogonal_router import RoutingFailure
    model = plan.get('physical_path_banks', {})
    reserve = budget.pop('path_bank_reserve', 0)
    if not model.get('banks'):
        return current
    budget['path_bank_finish_done'] = True; options = options or RoutingOptions()
    detail = dict(version=VERSION, reserved_trials=reserve, trials=0, accepted=0, actions=[], generation=[],
        before=measure_banks(current, comps, index, model), shared_budget_reset=False)
    report['physical_path_bank_finish'] = detail
    for number, bank in enumerate(model['banks']):
        allowance = min(12, max(0, budget['max_candidates']-budget['candidates'])//max(1, len(model['banks'])-number))
        diagnostic = dict(id=bank['id'], allowance=allowance, generated=0, trials=0,
            state_rejected=0, old_organization_rejected=0, objective_rejected=0, hpwl_rejected=0, static_rejected=0)
        detail['generation'].append(diagnostic)
        if not allowance or not engine._has_budget(budget, until):
            detail['stop_reason'] = 'shared_attachment_budget'; break
        old = engine.attachment_rank(engine.measure_attachments(current, comps, index, plan))
        before = bank_rank(measure_banks(current, comps, index, dict(model, banks=[bank])))
        before_hpwl = _hpwl(par, current, nets, index)
        try:
            for trial, action in islice(bank_trials(par, current, comps, nets, index, bank,
                    options, until, diagnostic), allowance):
                if not engine._has_budget(budget, until):
                    detail['stop_reason'] = 'shared_attachment_budget'; break
                budget['candidates'] += 1; report['candidates'] += 1; detail['trials'] += 1; diagnostic['trials'] += 1
                if not trial.valid_state() or not engine._fixed_world_safe(current, trial, index):
                    diagnostic['state_rejected'] += 1; continue
                rank = engine.attachment_rank(engine.measure_attachments(trial, comps, index, plan))
                if old is None or rank is None or any(n > o+EPS for n, o in zip(rank[:4]+rank[5:], old[:4]+old[5:])):
                    diagnostic['old_organization_rejected'] += 1; continue
                after = bank_rank(measure_banks(trial, comps, index, dict(model, banks=[bank])))
                if before is None or after is None or after >= before:
                    diagnostic['objective_rejected'] += 1; continue
                after_hpwl = _hpwl(par, trial, nets, index)
                if after_hpwl > min(hpwl_limit, before_hpwl)+EPS:
                    diagnostic['hpwl_rejected'] += 1; continue
                safe = engine._checked_static(par, trial, nets, index, options, until)
                if safe is None:
                    budget['exhausted'] = True; detail['stop_reason'] = 'shared_attachment_budget'; break
                if not safe:
                    diagnostic['static_rejected'] += 1; continue
                action.update(owner_distance_before=old[4], owner_distance_after=rank[4],
                    estimated_wire_before=before_hpwl, estimated_wire_after=after_hpwl,
                    existing_non_distance_terms_preserved=True)
                current = trial; detail['accepted'] += 1; report['accepted'] += 1
                detail['actions'].append(action); report['actions'].append(action); break
        except RoutingFailure as exc:
            if exc.diagnostics.get('reason') != 'quality_time_budget':
                raise
            budget['exhausted'] = True; detail['stop_reason'] = 'shared_attachment_budget'; break
    detail['after'] = measure_banks(current, comps, index, model)
    detail.setdefault('stop_reason', 'considered_banks')
    return current
