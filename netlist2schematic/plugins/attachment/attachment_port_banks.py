"""Small passive banks around two visible ports of one host.

The ordinary grid identifies guests; actual footprint-based post-SA coordinates
need not lie on grid-cell centers. No GT, search router, random source, new axis
or clearance exemption is used here.
"""
from copy import deepcopy
from math import ceil
from statistics import median
from time import perf_counter

VERSION = 'host-port-banks-v1'
PASSIVE = frozenset(('resistor', 'capacitor', 'inductor'))
VECTORS = {'west': (-1, 0), 'east': (1, 0), 'north': (0, -1), 'south': (0, 1)}
EPS = 1e-8


def compile_banks(comps, groups):
    by_id = {c['id']: c for c in comps}
    banks, skipped = [], []
    for group in groups:
        if group.get('kind') != 'parallel_bank' or len(group.get('members', ())) < 2:
            continue
        hosts = group.get('host_members', ())
        if len(hosts) != 1 or hosts[0] not in by_id or by_id[hosts[0]]['symbol'] != 'opamp':
            continue
        host = by_id[hosts[0]]
        if set(host['nodes']) != {'OUT', 'IN+', 'IN-'}:
            continue
        ports = sorted({p for name, p in group['host_ports'] if name == host['id']})
        if len(ports) != 2 or 'OUT' not in ports or len(set(ports) & {'IN+', 'IN-'}) != 1:
            skipped.append(dict(group=group['id'], reason='ambiguous_visible_host_ports'))
            continue
        input_pin = next(p for p in ports if p != 'OUT')
        endpoints = {host['nodes'][p] for p in ports}
        names = group['members']
        if len(endpoints) != 2 or any(by_id[n]['symbol'] not in PASSIVE
                or len(by_id[n]['nodes']) != 2 or set(by_id[n]['nodes'].values()) != endpoints for n in names):
            skipped.append(dict(group=group['id'], reason='not_exact_two_terminal_bank'))
            continue
        banks.append(dict(group=group['id'], host=host['id'], input_pin=input_pin,
            output_pin='OUT', members=list(names),
            terminals={n: {p: net for p, net in by_id[n]['nodes'].items()} for n in names},
            host_terminals={p: host['nodes'][p] for p in ports},
            evidence='exact_parallel_paths_between_distinct_visible_ports_of_one_host'))
    return dict(version=VERSION, banks=banks, skipped=skipped)


def _contract(comps, index, bank):
    try:
        host = comps[index[bank['host']]]
        return (host['symbol'] == 'opamp'
            and all(host['nodes'][p] == n for p, n in bank['host_terminals'].items())
            and all(comps[index[n]]['symbol'] in PASSIVE
                and comps[index[n]]['nodes'] == pins for n, pins in bank['terminals'].items()))
    except (KeyError, IndexError, TypeError):
        return False


def measure_banks(pl, comps, index, model):
    records, missing = [], []
    for bank in model.get('banks', ()):
        if not _contract(comps, index, bank):
            missing.append(bank['group'])
            continue
        host = index[bank['host']]
        vector = VECTORS[pl.pin_dir(host, bank['output_pin'])]
        axis = 0 if vector[0] else 1
        points = {net: [] for net in bank['host_terminals'].values()}
        errors = 0
        for name in bank['members']:
            i = index[name]
            for pin, net in comps[i]['nodes'].items():
                points[net].append(pl.pin_world(i, pin)[axis])
                want = vector if net == bank['host_terminals']['OUT'] else tuple(-v for v in vector)
                errors += VECTORS[pl.pin_dir(i, pin)] != want
        residual = sum(sum(abs(v-median(values)) for v in values) for values in points.values())
        records.append(dict(group=bank['group'], host=bank['host'], members=bank['members'],
            direction_errors=errors, parallel_pin_error=residual,
            actual_positions={n: list(pl.pos_of(index[n])) for n in bank['members']}))
    return dict(version=VERSION, count=len(model.get('banks', ())), measured=len(records),
        measurable=bool(records) and not missing, records=records, missing=missing,
        direction_errors=sum(r['direction_errors'] for r in records) if not missing else None,
        parallel_pin_error=sum(r['parallel_pin_error'] for r in records) if not missing else None)


def _snap(value, step, anchor):
    return anchor + round((value-anchor) / step) * step


def bank_trials(par, pl, comps, index, bank, options, deadline=None):
    if deadline is not None and perf_counter() >= deadline:
        return
    if not _contract(comps, index, bank):
        return
    from netlist2schematic.plugins.attachment.attachment_integration import _fixed_ids
    ids = [index[n] for n in bank['members']]
    if _fixed_ids(pl, index).intersection(ids):
        return
    host = index[bank['host']]
    vector = VECTORS[pl.pin_dir(host, bank['output_pin'])]
    if VECTORS[pl.pin_dir(host, bank['input_pin'])] != tuple(-v for v in vector):
        return
    axis = 0 if vector[0] else 1
    cross = 1-axis
    sign = vector[axis]
    step = options.grid_step
    clearance = max(options.center_spacing, step)
    host_box = par.GEO_DOC[comps[host]['symbol']]['orientations'][pl.orient[host]]['viewBoxAABB']
    center = pl.pos_of(host)
    host_edges = (center[cross] + host_box['x' if cross == 0 else 'y'] - options.halo,
        center[cross] + host_box['x' if cross == 0 else 'y']
        + host_box['width' if cross == 0 else 'height'] + options.halo)
    layouts = {}
    for i in ids:
        if deadline is not None and perf_counter() >= deadline:
            return
        choices = []
        for orientation in pl.allowed_orientations(i):
            geom = par.GEO_DOC[comps[i]['symbol']]['orientations'][orientation]
            pins = {p['name']: p for p in geom['pins']}
            terminal = {}
            for pin, net in comps[i]['nodes'].items():
                target = vector if net == bank['host_terminals']['OUT'] else tuple(-v for v in vector)
                if VECTORS[pins[pin]['direction']] != target:
                    break
                terminal[net] = pins[pin]['at']['x' if axis == 0 else 'y']
            else:
                box = geom['viewBoxAABB']
                lo = box['x' if cross == 0 else 'y'] - options.halo
                hi = lo + box['width' if cross == 0 else 'height'] + 2*options.halo
                choices.append((orientation, terminal, lo, hi))
        if not choices:
            return
        layouts[i] = min(choices, key=lambda c: (c[0] != pl.orient[i], c[0]))
    # Thinner footprints nearest the host; identical guests are exchangeable.
    ordered = sorted(ids, key=lambda i: (layouts[i][3]-layouts[i][2], comps[i]['symbol'],
        pl.pos_of(i)[cross], pl.pos_of(i)[axis]))
    input_net = bank['host_terminals'][bank['input_pin']]
    output_net = bank['host_terminals']['OUT']
    a = pl.pin_world(host, bank['input_pin'])[axis]
    b = pl.pin_world(host, bank['output_pin'])[axis]
    mean_a = sum(layouts[i][1][input_net] for i in ids)/len(ids)
    mean_b = sum(layouts[i][1][output_net] for i in ids)/len(ids)
    centers = ((a+b-mean_a-mean_b)/2, a-sign*step-mean_a, b+sign*step-mean_b)
    seen = set()
    side_hint = pl.pin_world(host, bank['input_pin'])[cross] - center[cross]
    preferred = -1 if side_hint < 0 else 1
    for side in (preferred, -preferred):
        for gap in (clearance, 2*clearance):
            for primary in centers:
                if deadline is not None and perf_counter() >= deadline:
                    return
                primary = _snap(primary, step, center[axis])
                edge = host_edges[0 if side < 0 else 1]
                targets = {}
                for i in ordered:
                    _, _, lo, hi = layouts[i]
                    raw = edge + side*gap - (hi if side < 0 else lo)
                    at = center[cross] + side*ceil(side*(raw-center[cross])/step)*step
                    xy = [0.0, 0.0]
                    xy[axis], xy[cross] = primary, at
                    targets[i] = tuple(xy)
                    edge = at + (lo if side < 0 else hi)
                key = tuple(targets[i] for i in ids)
                if key in seen:
                    continue
                seen.add(key)
                trial = deepcopy(pl)
                offsets = list(getattr(trial, 'passive_local_offsets', [(0, 0)]*len(comps)))
                for i in ids:
                    x, y = pl.pos_of(i)
                    ox, oy = offsets[i]
                    offsets[i] = (targets[i][0] - x + ox, targets[i][1] - y + oy)
                    trial.orient[i] = layouts[i][0]
                trial.passive_local_offsets = offsets
                yield trial, dict(kind='same_host_passive_band', group=bank['group'],
                    members=list(bank['members']), host=bank['host'], side=side, gap=gap,
                    coordinate_mode='passive_local_offsets', logical_cells_and_axes_preserved=True,
                    positions={comps[i]['id']: list(targets[i]) for i in ids})


def finish_banks(par, current, comps, nets, index, plan, options, budget, until, report, *, hpwl_limit):
    model = plan.get('port_banks', {})
    if not model.get('banks'):
        return current
    from netlist2schematic.plugins.attachment import attachment_integration as engine
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    from netlist2schematic.routing.routing_rules import RoutingOptions
    options = options or RoutingOptions()
    summary = dict(version=VERSION, trials=0, accepted=0, actions=[],
        before=measure_banks(current, comps, index, model), shared_budget_reset=False)
    report['port_bank_finish'] = summary
    for bank in model['banks']:
        if not engine._has_budget(budget, until):
            summary['stop_reason'] = 'shared_attachment_budget'
            break
        if set(bank['members']).intersection(plan.get('source_managed_members', ())):
            summary.setdefault('skipped', []).append(dict(group=bank['group'], reason='source_managed_guests'))
            continue
        before = engine.measure_attachments(current, comps, index, plan)
        for trial, action in bank_trials(par, current, comps, index, bank, options, until):
            if not engine._has_budget(budget, until):
                summary['stop_reason'] = 'shared_attachment_budget'
                break
            budget['candidates'] += 1
            report['candidates'] += 1
            summary['trials'] += 1
            if not trial.valid_state() or not engine._fixed_world_safe(current, trial, index):
                continue
            measured = engine.measure_attachments(trial, comps, index, plan)
            old_rank, new_rank = engine.attachment_rank(before), engine.attachment_rank(measured)
            if old_rank is None or new_rank is None or new_rank >= old_rank:
                continue
            if _hpwl(par, trial, nets, index) > hpwl_limit+EPS:
                continue
            safe = engine._checked_static(par, trial, nets, index, options, until)
            if safe is None:
                budget['exhausted'] = True
                summary['stop_reason'] = 'shared_attachment_budget'
                break
            if not safe:
                continue
            current = trial
            summary['accepted'] += 1
            summary['actions'].append(action)
            report['accepted'] += 1
            report['actions'].append(action)
            break
    summary['after'] = measure_banks(current, comps, index, model)
    summary.setdefault('stop_reason', 'completed')
    return current
