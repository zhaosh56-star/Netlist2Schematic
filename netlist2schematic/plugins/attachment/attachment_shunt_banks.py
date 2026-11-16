"""Pack proven parallel loads near one actual conduction port.

One common signal node, one confirmed supply, and one unique D/S or C/E
anchor establish the relation. Shared bias/supply names do not establish it.
"""
from copy import deepcopy
from itertools import islice
from math import ceil
from statistics import median
from time import perf_counter

VERSION = 'physical-shunt-banks-v1'
PASSIVE = frozenset(('resistor', 'capacitor', 'inductor'))
ACTIVE = frozenset(('nmos', 'pmos', 'npn', 'pnp'))
EPS = 1e-8


def compile_banks(comps, plan):
    by_id = {c['id']: c for c in comps}
    junctions = plan['readability_plan']['junctions']
    managed = set(plan.get('source_managed_members', ()))
    banks, skipped = [], []
    for group in plan.get('groups', ()):
        if (group.get('kind') != 'parallel_bank' or len(group['members']) < 2
                or len(group.get('owner_junctions', ())) != 1):
            continue
        owner = group['owner_junctions'][0]
        nodes = set(by_id[group['members'][0]]['nodes'].values())
        rails = nodes-{owner}
        if len(rails) != 1 or junctions.get(owner, {}).get('is_rail'):
            continue
        rail = next(iter(rails))
        if not junctions.get(rail, {}).get('is_rail'):
            continue
        ports = list(junctions[owner].get('conduction_ports', ()))
        if len(ports) != 1 or by_id[ports[0][0]]['symbol'] not in ACTIVE:
            skipped.append(dict(group=group['id'], reason='no_unique_conduction_anchor'))
            continue
        if managed.intersection(group['members']):
            skipped.append(dict(group=group['id'], reason='source_managed_guests'))
            continue
        if any(by_id[n]['symbol'] not in PASSIVE or len(by_id[n]['nodes']) != 2
               or set(by_id[n]['nodes'].values()) != nodes for n in group['members']):
            continue
        host, pin = ports[0]
        banks.append(dict(group=group['id'], members=list(group['members']), owner_net=owner,
            rail_net=rail, anchor=[host, pin], anchor_symbol=by_id[host]['symbol'],
            terminals={n: dict(by_id[n]['nodes']) for n in group['members']},
            control_interfaces=deepcopy(junctions[owner].get('control_ports', ())),
            evidence='exact_parallel_loads_on_unique_conduction_port_and_verified_supply'))
    return dict(version=VERSION, banks=banks, skipped=skipped)


def _contract(comps, index, bank):
    try:
        host, pin = bank['anchor']
        return (comps[index[host]]['symbol'] == bank['anchor_symbol']
            and comps[index[host]]['nodes'][pin] == bank['owner_net']
            and all(comps[index[n]]['symbol'] in PASSIVE and comps[index[n]]['nodes'] == pins
                    for n, pins in bank['terminals'].items()))
    except (KeyError, IndexError, TypeError):
        return False


def measure_banks(pl, comps, index, model):
    records, missing = [], []
    for bank in model.get('banks', ()):
        if not _contract(comps, index, bank):
            missing.append(dict(group=bank['group'], reason='terminal_or_symbol_contract_changed'))
            continue
        host, pin = bank['anchor']
        direction = pl.pin_dir(index[host], pin)
        axis = 1 if direction in ('north', 'south') else 0
        facing = {'north': 'south', 'south': 'north', 'east': 'west', 'west': 'east'}[direction]
        anchor = pl.pin_world(index[host], pin)
        points, errors = [], 0
        for n in bank['members']:
            i = index[n]
            p = next(p for p, net in comps[i]['nodes'].items() if net == bank['owner_net'])
            points.append(pl.pin_world(i, p))
            errors += pl.pin_dir(i, p) != facing
        med = median(p[axis] for p in points)
        records.append(dict(group=bank['group'], members=bank['members'], anchor=bank['anchor'],
            owner_pin_alignment_error=sum(abs(p[axis]-med) for p in points), direction_errors=errors,
            owner_distance=sum(abs(p[0]-anchor[0])+abs(p[1]-anchor[1]) for p in points)/len(points)/100,
            transverse_span=max(p[1-axis] for p in points)-min(p[1-axis] for p in points),
            actual_positions={n: list(pl.pos_of(index[n])) for n in bank['members']}))
    valid = bool(records) and not missing
    return dict(version=VERSION, count=len(model.get('banks', ())), measured=len(records),
        measurable=valid, records=records, missing=missing, skipped=deepcopy(model.get('skipped', ())),
        **{k: sum(r[k] for r in records) if valid else None for k in
            ('owner_pin_alignment_error', 'direction_errors', 'owner_distance', 'transverse_span')})


def reserve_finish(plan, budget):
    count = len(plan.get('shunt_banks', {}).get('banks', ()))
    if not count or budget.get('shunt_bank_finish_done'):
        return
    reserve = min(12, 4*count, budget['max_candidates']//8)
    budget['shunt_bank_reserve'] = reserve


def bank_trials(par, pl, comps, nets, index, bank, options, deadline=None, diagnostic=None):
    diagnostic = {} if diagnostic is None else diagnostic
    if deadline is not None and perf_counter() >= deadline:
        diagnostic['stop_reason'] = 'deadline'
        return
    if not _contract(comps, index, bank):
        diagnostic['stop_reason'] = 'changed_contract'
        return
    from netlist2schematic.plugins.attachment.attachment_integration import _fixed_ids
    ids = [index[n] for n in bank['members']]
    if _fixed_ids(pl, index).intersection(ids):
        diagnostic['stop_reason'] = 'fixed_guest'
        return
    if getattr(pl, 'power_connections', None) is not None:
        diagnostic['stop_reason'] = 'custom_power_geometry'
        return
    host, host_pin = bank['anchor']
    host_i = index[host]
    direction = pl.pin_dir(host_i, host_pin)
    symbol = par.power_marker_sym(bank['rail_net'])
    if (symbol not in ('vdd-port', 'ground') or direction not in ('north', 'south')
            or direction != ('north' if symbol == 'vdd-port' else 'south')):
        diagnostic['stop_reason'] = 'rail_access_direction_not_supported'
        return
    sign = -1 if direction == 'north' else 1
    facing = 'south' if sign < 0 else 'north'
    template = deepcopy(pl)
    owner_pins = {}
    for i in ids:
        pin = next(p for p, net in comps[i]['nodes'].items() if net == bank['owner_net'])
        owner_pins[i] = pin
        choices = []
        for ori in pl.allowed_orientations(i):
            pins = {p['name']: p for p in par.GEO_DOC[comps[i]['symbol']]['orientations'][ori]['pins']}
            if pins[pin]['direction'] == facing:
                choices.append(ori)
        if not choices:
            diagnostic['stop_reason'] = 'no_legal_orientation'
            return
        template.orient[i] = min(choices, key=lambda o: (o != pl.orient[i], o))
    from netlist2schematic.routing.routing_rules import build_scene
    from netlist2schematic.plugins.attachment.attachment_physical import owned_footprints
    scene = build_scene(par, nets, template, index, options=options, deadline=deadline)
    diagnostic['scene_templates'] = diagnostic.get('scene_templates', 0)+1
    envelopes = owned_footprints(par, scene, nets, index)
    anchor = template.pin_world(host_i, host_pin)
    relative, pin_offsets = {}, {}
    for i in ids:
        point = template.pin_world(i, owner_pins[i])
        origin = template.pos_of(i)
        pin_offsets[i] = point[0]-origin[0], point[1]-origin[1]
        rect = envelopes[i]
        relative[i] = rect[0]-point[0], rect[1]-point[1], rect[2]-point[0], rect[3]-point[1]
    ordered = sorted(ids, key=lambda i: (pl.pos_of(i)[0], comps[i]['symbol'], pl.pos_of(i)[1]))
    step = options.grid_step
    gap = max(options.center_spacing, step)
    host_front = -envelopes[host_i][1] if sign < 0 else envelopes[host_i][3]
    seen = set()
    for alignment in ('symbol_midpoints', 'owner_pins'):
        inner = min((-relative[i][3] if sign < 0 else relative[i][1])
            + (sign*pin_offsets[i][1] if alignment == 'symbol_midpoints' else 0) for i in ids)
        for normal_gap in (gap, 2*gap):
            raw = host_front+normal_gap-inner
            common = anchor[1] + sign*ceil((raw-sign*anchor[1])/step)*step
            transverse_gap = gap
            width = sum(relative[i][2]-relative[i][0] for i in ids) + transverse_gap*(len(ids)-1)
            for shift in (0., -width/2, width/2):
                if deadline is not None and perf_counter() >= deadline:
                    diagnostic['stop_reason'] = 'deadline'
                    return
                cursor = anchor[0]-width/2+shift
                targets = {}
                for i in ordered:
                    at = anchor[0]+ceil((cursor-relative[i][0]-anchor[0])/step)*step
                    targets[i] = at-pin_offsets[i][0], common-(pin_offsets[i][1] if alignment == 'owner_pins' else 0)
                    cursor = at+relative[i][2]+transverse_gap
                key = tuple(targets[i] for i in ids)
                if key in seen:
                    continue
                seen.add(key)
                trial = deepcopy(template)
                offsets = list(getattr(trial, 'passive_local_offsets', [(0,0)]*len(comps)))
                for i in ids:
                    x, y = pl.pos_of(i); ox, oy = offsets[i]
                    offsets[i] = targets[i][0]-x+ox, targets[i][1]-y+oy
                trial.passive_local_offsets = offsets
                diagnostic['generated'] = diagnostic.get('generated', 0)+1
                yield trial, dict(kind='physical_parallel_shunt_bank', version=VERSION,
                    group=bank['group'], members=bank['members'], anchor=bank['anchor'],
                    alignment=alignment, normal_gap=normal_gap, transverse_gap=transverse_gap,
                    positions={comps[i]['id']: list(targets[i]) for i in ids},
                    source='actual_owned_body_fixed_exit_and_power_marker_envelopes',
                    logical_cells_and_axes_preserved=True)


def finish_shunts(par, current, comps, nets, index, plan, options, budget, until, report, *, hpwl_limit):
    from netlist2schematic.plugins.attachment import attachment_integration as engine
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    from netlist2schematic.routing.routing_rules import RoutingOptions
    from netlist2schematic.routing.orthogonal_router import RoutingFailure
    model = plan.get('shunt_banks', {})
    reserve = budget.pop('shunt_bank_reserve', 0)
    if not model.get('banks'):
        return current
    budget['shunt_bank_finish_done'] = True
    options = options or RoutingOptions()
    detail = dict(version=VERSION, trials=0, accepted=0, actions=[], generation=[],
        reserved_trials=reserve, shared_budget_reset=False,
        before=measure_banks(current, comps, index, model))
    report['physical_shunt_finish'] = detail
    for number, bank in enumerate(model['banks']):
        remaining = max(0, budget['max_candidates']-budget['candidates'])
        allowance = min(12, remaining//max(1,len(model['banks'])-number))
        diagnostic = dict(group=bank['group'], scene_templates=0, generated=0,
            allowance=allowance, trials=0)
        detail['generation'].append(diagnostic)
        if not allowance or not engine._has_budget(budget, until):
            detail['stop_reason'] = 'shared_attachment_budget'
            break
        old_rank = engine.attachment_rank(engine.measure_attachments(current, comps, index, plan))
        try:
            for trial, action in islice(bank_trials(par,current,comps,nets,index,bank,options,until,diagnostic), allowance):
                if not engine._has_budget(budget, until):
                    break
                budget['candidates'] += 1; report['candidates'] += 1; detail['trials'] += 1
                diagnostic['trials'] += 1
                if not trial.valid_state() or not engine._fixed_world_safe(current,trial,index):
                    continue
                rank = engine.attachment_rank(engine.measure_attachments(trial,comps,index,plan))
                if old_rank is None or rank is None or rank >= old_rank:
                    continue
                if _hpwl(par,trial,nets,index) > hpwl_limit+EPS:
                    continue
                safe = engine._checked_static(par,trial,nets,index,options,until)
                if safe is None:
                    budget['exhausted'] = True; detail['stop_reason'] = 'shared_attachment_budget'
                    break
                if not safe:
                    continue
                current = trial
                detail['accepted'] += 1;report['accepted'] += 1
                detail['actions'].append(action);report['actions'].append(action)
                break
        except RoutingFailure as exc:
            if exc.diagnostics.get('reason') != 'quality_time_budget':
                raise
            budget['exhausted'] = True
            detail['stop_reason'] = 'shared_attachment_budget'
            break
    detail['after'] = measure_banks(current,comps,index,model)
    detail.setdefault('stop_reason','considered_banks')
    return current
