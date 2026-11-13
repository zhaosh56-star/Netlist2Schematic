"""Transactional source locality and spare-channel compaction before routing.

All measurements use original visible terminals.  Sources keep their electrical
polarity; moving one drawing never replaces it with a supply marker.  The
bounded search checks fixed escape/marker geometry without routing any net.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
import json
from math import ceil, isfinite
from statistics import median
from time import perf_counter

from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
from netlist2schematic.plugins.symmetry.symmetry_layout import _static_safe

VERSION = 'readability-refinement-v1'
ACTIVE = frozenset(('nmos', 'pmos', 'npn', 'pnp'))
PASSIVE = frozenset(('resistor', 'capacitor', 'inductor', 'diode'))
SOURCE = frozenset(('current-source', 'voltage-source'))
CONTROL = frozenset(('G', 'B', 'IN+', 'IN-'))
CHANNEL = frozenset(('D', 'S', 'C', 'E'))
EPS = 1e-8


def _plan(value):
    if isinstance(value, dict) and 'layout_readability' in value:
        return value.get('layout_readability', {}).get('plan', {}) or {}
    return value or {}


def build_refinement_model(par, comps, nets, index, plan=None):
    """Resolve conservative local sources independently of region recognition."""
    plan = _plan(plan)
    by_id = {comp['id']: comp for comp in comps}
    sources, skipped = [], []
    for comp in comps:
        if comp['symbol'] not in SOURCE:
            continue
        pins = comp.get('nodes', {})
        rails = [pin for pin, net in pins.items() if par.power_marker_sym(net)]
        if len(pins) != 2 or len(rails) != 1:
            skipped.append(dict(device=comp['id'], reason='floating_or_supply_only_source'))
            continue
        rail_pin = rails[0]
        signal_pin = next(pin for pin in pins if pin != rail_pin)
        net = pins[signal_pin]
        ports = sorted({(name, pin) for name, pin in nets.get(net, ())
                        if name != comp['id'] and name in by_id})
        conducting = [(name, pin) for name, pin in ports
                      if by_id[name]['symbol'] in ACTIVE and pin in CHANNEL]
        kind, hosts = None, []
        if comp['symbol'] == 'current-source':
            if (len(ports) == len(conducting) == 2 and len({name for name, _ in ports}) == 2
                    and all(pin in ('S', 'E') for _, pin in ports)
                    and len({by_id[name]['symbol'] for name, _ in ports}) == 1):
                controls = [by_id[name]['nodes'].get('G', by_id[name]['nodes'].get('B')) for name, _ in ports]
                drains = [by_id[name]['nodes'].get('D', by_id[name]['nodes'].get('C')) for name, _ in ports]
                if len(set(controls)) == len(set(drains)) == 2:
                    kind, hosts = 'tail_source', ports
            elif len(conducting) == 1:
                name, pin = conducting[0]
                target = by_id[name]
                control = 'G' if target['symbol'] in ('nmos', 'pmos') else 'B'
                if (pin in ('D', 'C') and target['nodes'].get(control) == net
                        and all((n, p) == (name, pin) or p in CONTROL for n, p in ports)):
                    kind, hosts = 'diode_bias_source', [(name, pin)]
        elif ports and all(pin in CONTROL for _, pin in ports):
            kind, hosts = 'gate_voltage_source', ports
        if kind is None:
            skipped.append(dict(device=comp['id'], reason='nonunique_local_source_role'))
            continue
        sources.append(dict(id='source:'+comp['id'], device=comp['id'], kind=kind,
            signal_pin=signal_pin, rail_pin=rail_pin, host_ports=[list(port) for port in hosts],
            net=net, rail_net=pins[rail_pin], confidence='high'))
    identity = dict(plan_signature=plan.get('signature'), sources=sources,
        components=[dict(id=row['id'], symbol=row['symbol'], nodes=row.get('nodes', {}))
                    for row in sorted(comps, key=lambda row: row['id'])])
    signature = sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return dict(version=VERSION, signature=signature, sources=sources, skipped_sources=skipped)


def _source_anchor(pl, index, row):
    hosts = [(index[name], pin) for name, pin in row['host_ports'] if name in index]
    points = [pl.pin_world(i, pin) for i, pin in hosts]
    if not points:
        return None
    point = (median(p[0] for p in points), median(p[1] for p in points))
    directions = [pl.pin_dir(i, pin) for i, pin in hosts]
    direction = directions[0] if len(set(directions)) == 1 else None
    vector = {'north': (0, -1), 'south': (0, 1), 'west': (-1, 0), 'east': (1, 0)}.get(direction, (0, 0))
    return point, vector


def _rects(par, pl, comps, halo=12):
    result = []
    for i, comp in enumerate(comps):
        box = par.GEO_DOC[comp['symbol']]['orientations'][pl.orient[i]]['viewBoxAABB']
        x, y = pl.pos_of(i)
        result.append((x+box['x']-halo, y+box['y']-halo,
                       x+box['x']+box['width']+halo, y+box['y']+box['height']+halo))
    return result


def _union_area(rects):
    xs = sorted({x for rect in rects for x in (rect[0], rect[2])})
    area = 0.0
    for a, b in zip(xs, xs[1:]):
        intervals = sorted((r[1], r[3]) for r in rects if r[0] < b and r[2] > a)
        covered, end = 0.0, None
        for low, high in intervals:
            covered += max(0, high-max(low, end if end is not None else low))
            end = high if end is None else max(end, high)
        area += (b-a)*covered
    return area


def compaction_applicability(pl, comps, plan, model):
    """Require a nontrivial passive relation or an interior display-row band.

    Blank canvas outside occupied active rows is not a reason to alter an
    otherwise complete MOS drawing. A local source only authorizes its own
    placement, not compression of the whole core. This predicate is pure and
    independent of bounding-box area, pitch, route quality and instance names.
    """
    plan = _plan(plan)
    model = model or {}
    if plan.get('series_axes'):
        return True, 'verified_series_axes'
    by_id = {comp['id']: comp for comp in comps}
    for pattern in plan.get('patterns', ()):
        names = set(pattern.get('members', ()))
        if not names or any(name not in by_id or by_id[name]['symbol'] not in PASSIVE for name in names):
            continue
        kind = pattern.get('kind')
        if kind in ('parallel_bank', 'series_chain', 'two_arm_join', 'multi_path_bridge') and len(names) >= 2:
            return True, 'nontrivial_passive_relationship'
        if kind in ('bridge', 'rl_branch_bridge') and any(
                by_id[name]['symbol'] in ('resistor', 'inductor') for name in names):
            return True, 'nontrivial_passive_relationship'
    active_rows = sorted({pl.cell_of[i][0] for i, comp in enumerate(comps)
                          if comp['symbol'] in ACTIVE and pl.cell_of[i] is not None})
    if len(active_rows) > 1 and any(b-a > 1 for a, b in zip(active_rows, active_rows[1:])):
        return True, 'interior_display_row_band'
    if model.get('sources'):
        return False, 'local_source_only_no_core_compaction'
    return False, 'no_local_attachment_or_interior_row_band'


def measure_refinements(par, pl, comps, nets, index, metadata_or_plan=None, *, phase=4):
    """Pure, same-plan measurements for candidates, recovery and saved redraw."""
    plan = _plan(metadata_or_plan)
    model = build_refinement_model(par, comps, nets, index, plan)
    compact_applicable, compact_reason = compaction_applicability(pl, comps, plan, model)
    records, distance, errors = [], 0.0, 0
    for row in model['sources']:
        anchor = _source_anchor(pl, index, row)
        if anchor is None:
            continue
        point, vector = anchor
        i = index[row['device']]
        source_pin = pl.pin_world(i, row['signal_pin'])
        target = (point[0]+60*vector[0], point[1]+60*vector[1])
        delta = (source_pin[0]-point[0], source_pin[1]-point[1])
        wrong_side = bool(any(vector) and delta[0]*vector[0]+delta[1]*vector[1] < -EPS)
        rail_direction = 'south' if par.power_marker_sym(row['rail_net']) == 'ground' else 'north'
        rail_facing = pl.pin_dir(i, row['rail_pin']) != rail_direction
        cost = (abs(source_pin[0]-target[0])+abs(source_pin[1]-target[1]))/100 + .5*rail_facing
        distance += cost
        errors += wrong_side+rail_facing
        records.append(dict(device=row['device'], kind=row['kind'], distance=cost,
                            wrong_side=wrong_side, rail_facing_error=rail_facing,
                            anchor=list(point), target=list(target)))
    rects = _rects(par, pl, comps)
    area = ((max(r[2] for r in rects)-min(r[0] for r in rects))*
            (max(r[3] for r in rects)-min(r[1] for r in rects))) if rects else 0.0
    banks = dict(count=0, planned_count=0, measurable=False, error=None, records=[], skipped=[])
    if phase >= 4:
        from netlist2schematic.plugins.readability.readability_banks import measure_bank_outlet
        banks = measure_bank_outlet(pl, comps, index, plan)
    return dict(version=VERSION, plan_signature=model['signature'], measurable=bool(rects),
        source_count=len(records), source_distance=distance if records else None,
        source_errors=errors if records else None,
        void=max(0.0, area-_union_area(rects))/10000 if rects else None,
        compactness=area/10000 if rects else None,
        bank_outlet_count=banks['count'], bank_outlet_error=banks['error'],
        bank_outlet_planned=banks.get('planned_count', banks['count']),
        bank_outlet_measurable=banks.get('measurable', banks['error'] is not None),
        compaction_applicable=compact_applicable, compaction_reason=compact_reason,
        bank_outlets=deepcopy(banks['records']), skipped_bank_outlets=deepcopy(banks['skipped']),
        sources=records, skipped_sources=deepcopy(model['skipped_sources']))


def _passive_measurement(pl, comps, index, plan):
    if not (plan.get('patterns') or plan.get('series_axes')):
        return None
    from netlist2schematic.plugins.readability.readability_layout import measure_readability
    measured = measure_readability(pl, comps, index, plan)
    if measured.get('passive_supported') is False:
        return None
    return dict(measurable=measured.get('measurable', False), **{key: measured.get(key) for key in
            ('violations', 'shape_error', 'pin_axis_error', 'owner_locality')})


def _passive_guard(before, after):
    if before is None and after is None:
        return True, False
    if before is None or after is None:
        return False, False
    if before.get('measurable') is False or after.get('measurable') is False:
        return False, False
    improved = False
    for key, value in before.items():
        if key == 'measurable':
            continue
        current = after.get(key)
        if value is None and current is None:
            continue
        if value is None or current is None or current > value+EPS:
            return False, False
        improved |= current < value-EPS
    return True, improved


def _source_key(metrics):
    return ((metrics['source_errors'], metrics['source_distance'])
            if metrics['source_count'] else (0, 0))


def _bank_guard(before, after):
    if (before['bank_outlet_count'] != after['bank_outlet_count']
            or before['bank_outlet_planned'] != after['bank_outlet_planned']):
        return False
    if not before['bank_outlet_planned']:
        return True
    a, b = before['bank_outlet_error'], after['bank_outlet_error']
    return (type(a) in (int, float) and type(b) in (int, float)
            and isfinite(a) and isfinite(b) and b <= a+EPS)


def _compact_key(metrics):
    bank = (metrics['bank_outlet_error'] if metrics['bank_outlet_measurable'] else float('inf')) if metrics['bank_outlet_planned'] else 0
    return (bank,
            metrics['void'], metrics['compactness'])


def _fixed(pl, index):
    return {index.get(value, value) for value in
            set(getattr(pl, 'fixed', ())) | set(getattr(pl, 'fixed_cells', {}))}


def _sync(pl, cells, orient, changed=()):
    if hasattr(pl, 'offset_x'):
        pl.offset_x = list(pl.offset_x)
        for i in changed:
            if cells[i] != pl.cell_of[i]:
                pl.offset_x[i] = 0.0
    if hasattr(pl, 'set_state'):
        pl.set_state(cells, orient)
    else:
        pl.cell_of, pl.orient = list(cells), list(orient)
    pl.row_of = [row for row, _ in pl.cell_of]
    pl.comp_at = {cell: i for i, cell in enumerate(pl.cell_of)}
    pl.cols = len(pl.column_x)
    if hasattr(pl, 'fixed_rows'):
        pl.fixed_rows = dict(pl.fixed_rows)
        for i in changed:
            if i in getattr(pl, 'source_orientation_ids', ()):
                pl.fixed_rows[i] = pl.cell_of[i][0]


def _valid(original, trial, comps, index, source_ids, options):
    if len(set(trial.cell_of)) != len(comps) or trial.comp_at != {cell: i for i, cell in enumerate(trial.cell_of)}:
        return False
    if trial.row_of != [row for row, _ in trial.cell_of]:
        return False
    if any(type(value) not in (int, float) or not isfinite(value)
           for axis in (trial.row_y, trial.column_x) for value in axis):
        return False
    if any(b-a < options.grid_step-EPS for axis in (trial.row_y, trial.column_x) for a, b in zip(axis, axis[1:])):
        return False
    offsets = getattr(trial, 'offset_x', None)
    if offsets is not None:
        if len(offsets) != len(comps):
            return False
        for i, value in enumerate(offsets):
            if type(value) not in (int, float) or not isfinite(value):
                return False
            if abs(value/options.grid_step-round(value/options.grid_step)) > EPS:
                return False
            col = trial.cell_of[i][1]
            if not 0 <= col < len(trial.column_x):
                return False
            axis = trial.column_x
            left = axis[col]-axis[col-1] if col else axis[1]-axis[0] if len(axis) > 1 else 150
            right = axis[col+1]-axis[col] if col+1 < len(axis) else left
            if not -left/2-EPS <= value <= right/2+EPS:
                return False
    fixed = _fixed(original, index)
    for i, comp in enumerate(comps):
        row, col = trial.cell_of[i]
        if not (0 <= row < len(trial.row_y) and 0 <= col < len(trial.column_x)):
            return False
        if comp['symbol'] in ACTIVE and row != original.cell_of[i][0]:
            return False
        if comp['symbol'] not in ACTIVE | PASSIVE and i not in source_ids and (
                trial.cell_of[i] != original.cell_of[i] or trial.orient[i] != original.orient[i]):
            return False
        if i in fixed and (trial.cell_of[i] != original.cell_of[i] or trial.orient[i] != original.orient[i]
                           or trial.pos_of(i) != original.pos_of(i)):
            return False
        if trial.orient[i] not in trial.allowed_orientations(i):
            return False
    if hasattr(trial, 'valid_state') and not trial.valid_state():
        return False
    return not hasattr(trial, 'blocks_contiguous') or trial.blocks_contiguous()


def _source_trials(par, pl, comps, index, model, until):
    choices = []
    for row in model['sources']:
        if perf_counter() >= until:
            return
        i = index[row['device']]
        if i in _fixed(pl, index):
            continue
        anchor = _source_anchor(pl, index, row)
        if anchor is None:
            continue
        point, vector = anchor
        target = (point[0]+60*vector[0], point[1]+60*vector[1])
        host_rows = [pl.cell_of[index[name]][0] for name, _ in row['host_ports']]
        candidates = []
        for r in range(max(0, min(host_rows)-2), min(len(pl.row_y), max(host_rows)+3)):
            for c in range(len(pl.column_x)):
                if pl.comp_at.get((r, c)) not in (None, i):
                    continue
                for orientation, geometry in enumerate(par.GEO_DOC[comps[i]['symbol']]['orientations']):
                    local = next(pin['at'] for pin in geometry['pins'] if pin['name'] == row['signal_pin'])
                    pin = (pl.column_x[c]+local['x'], pl.row_y[r]+local['y'])
                    cost = abs(pin[0]-target[0])+abs(pin[1]-target[1])
                    candidates.append((cost, r, c, orientation))
        choices.append([(i, r, c, orientation, row) for _, r, c, orientation in sorted(candidates)[:32]])
    for n in range(max((len(group) for group in choices), default=0)):
        for group in choices:
            if n < len(group):
                i, r, c, orientation, row = group[n]
                trial = deepcopy(pl)
                cells, orient = list(pl.cell_of), list(pl.orient)
                cells[i], orient[i] = (r, c), orientation
                _sync(trial, cells, orient, (i,))
                yield trial, dict(kind='source_attachment', device=row['device'], role=row['kind'])


def _bank_trial_specs(pl, comps, index, plan):
    """Visit both lateral sides of real D outlets, round-robin across banks."""
    from netlist2schematic.plugins.readability.readability_banks import measure_bank_outlet
    banks = measure_bank_outlet(pl, comps, index, plan)
    groups = []
    for record in banks['records']:
        ids = [index[name] for name in record['members'] if name in index]
        if not ids or _fixed(pl, index).intersection(ids):
            continue
        host, _ = record['host_endpoint']
        row, col = pl.cell_of[index[host]]
        dx, dy = record['direction_vector']
        vertical = bool(dy)
        normal = int(dy if vertical else dx)
        common = set(pl.allowed_orientations(ids[0]))
        for i in ids[1:]:
            common.intersection_update(pl.allowed_orientations(i))
        specs = []
        for gap in (1, 2):
            axis = row+normal*gap if vertical else col+normal*gap
            for lateral in (0, -1, 1, -2, 2):
                start = (col if vertical else row)-len(ids)//2+lateral
                targets = [(axis, start+n) if vertical else (start+n, axis) for n in range(len(ids))]
                if any(not 0 <= r < len(pl.row_y) or not 0 <= c < len(pl.column_x) for r, c in targets):
                    continue
                if any(pl.comp_at.get(cell) not in (None, *ids) for cell in targets):
                    continue
                for orientation in sorted(common):
                    for ordered in (ids, list(reversed(ids))):
                        distance = sum(abs(pl.column_x[c]-record['host_point'][0])+
                                       abs(pl.row_y[r]-record['host_point'][1]) for r, c in targets)
                        specs.append((distance, list(ordered), targets, orientation,
                                      dict(kind='bank_outlet_template', pattern=record['id'],
                                           members=record['members'], direction=record['direction'],
                                           lateral=lateral, gap=gap)))
        groups.append(sorted(specs, key=lambda item: (item[0], abs(item[4]['lateral']), item[3])))
    for n in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if n < len(group):
                _, ids, targets, orientation, action = group[n]
                yield ids, targets, orientation, action


def _passive_trials(pl, comps, index, plan):
    for ids, targets, orientation, action in _bank_trial_specs(pl, comps, index, plan):
        cells, orient = list(pl.cell_of), list(pl.orient)
        for i, cell in zip(ids, targets):
            cells[i], orient[i] = cell, orientation
        trial = deepcopy(pl)
        _sync(trial, cells, orient, ids)
        yield trial, action
    for pattern in plan.get('patterns', ()):
        names = pattern.get('members', ())
        ids = [index[name] for name in names if name in index]
        if not ids or any(comps[i]['symbol'] not in PASSIVE for i in ids) or _fixed(pl, index).intersection(ids):
            continue
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            cells = list(pl.cell_of)
            for i in ids:
                r, c = cells[i]
                cells[i] = (r+dr, c+dc)
            if any(not 0 <= r < len(pl.row_y) or not 0 <= c < len(pl.column_x) for r, c in cells):
                continue
            trial = deepcopy(pl)
            _sync(trial, cells, pl.orient, ids)
            yield trial, dict(kind='passive_local', pattern=pattern.get('id'), members=list(names))


def _compaction_trials(par, pl, comps, nets, index, metadata, options):
    feedback_cut = metadata.get('feedback', {}).get('row_cut', metadata.get('feedback_row_cut'))
    rects = _rects(par, pl, comps, halo=options.halo)
    for axis_name, coordinate in (('row_y', 1), ('column_x', 0)):
        axis = list(getattr(pl, axis_name))
        for cut in range(len(axis)-1):
            if axis_name == 'row_y' and cut == feedback_cut:
                continue
            gap = axis[cut+1]-axis[cut]
            room = gap-options.grid_step
            # Preserve two wire tracks at every actual cross-cut body overlap.
            for a, first in enumerate(rects):
                if pl.cell_of[a][1-coordinate] > cut:
                    continue
                for b, second in enumerate(rects):
                    if pl.cell_of[b][1-coordinate] <= cut:
                        continue
                    other = 1-coordinate
                    if max(first[other], second[other]) < min(first[other+2], second[other+2]):
                        room = min(room, second[coordinate]-first[coordinate+2]-2*options.center_spacing)
            shift = min(room, gap*.35)
            shift = int(shift/options.grid_step)*options.grid_step
            if shift <= EPS:
                continue
            trial = deepcopy(pl)
            setattr(trial, axis_name, [value-(shift if n > cut else 0) for n, value in enumerate(axis)])
            _sync(trial, trial.cell_of, trial.orient)
            yield trial, dict(kind='axis_compaction', axis=axis_name, cut=cut, reduction=shift)
    # Shift complete direct stacks. A passive blocking a target slot may move
    # atomically to a neighbouring vacancy; active outsiders never get pushed.
    adjacency = defaultdict(set)
    for net, ports in nets.items():
        endpoints = [(index[name], pin) for name, pin in ports if name in index
                     and comps[index[name]]['symbol'] in ACTIVE and pin in CHANNEL]
        if par.power_marker_sym(net) or len(endpoints) != 2:
            continue
        if any(name in index and comps[index[name]]['symbol'] in PASSIVE | SOURCE for name, _ in ports):
            continue
        (a, pa), (b, pb) = endpoints
        output_join = (pa in ('D', 'C') and pb in ('D', 'C') and
                       {comps[a]['symbol'], comps[b]['symbol']} in ({'nmos', 'pmos'}, {'npn', 'pnp'}))
        if a != b and ((pa in ('D', 'C')) != (pb in ('D', 'C')) or output_join):
            adjacency[a].add(b); adjacency[b].add(a)
    seen = set()
    for start in sorted(adjacency):
        if start in seen:
            continue
        todo, group = [start], set()
        while todo:
            item = todo.pop()
            if item not in group:
                group.add(item); todo.extend(adjacency[item]-group)
        seen.update(group)
        for block in getattr(pl, 'blocks', ()):
            if group.intersection(block):
                group.update(block)
        if _fixed(pl, index).intersection(group):
            continue
        for delta in (-1, 1, -2, 2):
            cells, moved = list(pl.cell_of), set(group)
            for i in group:
                cells[i] = (cells[i][0], cells[i][1]+delta)
            if any(not 0 <= c < len(pl.column_x) for _, c in cells):
                continue
            targets = {cells[i] for i in group}
            blockers = [i for i in range(len(comps)) if i not in group and cells[i] in targets]
            if any(comps[i]['symbol'] not in PASSIVE or i in _fixed(pl, index) for i in blockers):
                continue
            valid = True
            for i in blockers:
                r, c = cells[i]
                occupied = {cell for j, cell in enumerate(cells) if j != i}
                free = next(((rr, cc) for dr, dc in ((0, -1), (0, 1), (-1, 0), (1, 0))
                             if 0 <= (rr := r+dr) < len(pl.row_y)
                             and 0 <= (cc := c+dc) < len(pl.column_x) and (rr, cc) not in occupied), None)
                if free is None:
                    valid = False; break
                cells[i] = free; moved.add(i)
            if valid:
                trial = deepcopy(pl)
                _sync(trial, cells, pl.orient, moved)
                yield trial, dict(kind='branch_attachment_translation',
                    members=[comps[i]['id'] for i in sorted(moved)], columns=delta)


def refine_readability(par, pl, comps, nets, index, metadata, *, options=None,
                       deadline=None, seconds=1.0, max_candidates=128, phase=3):
    """Spend one shared passive/source/compaction budget, retaining safe wins."""
    if type(phase) is not int or phase not in (3, 4):
        raise ValueError('readability refinement phase must be 3 or 4')
    if type(seconds) not in (int, float) or not isfinite(seconds) or seconds < 0:
        raise ValueError('readability refinement seconds must be finite and non-negative')
    if type(max_candidates) is not int or max_candidates < 0:
        raise ValueError('readability refinement candidate budget must be non-negative')
    from netlist2schematic.routing.routing_rules import RoutingOptions
    from netlist2schematic.routing.orthogonal_router import RoutingFailure
    options = options or RoutingOptions()
    if not isinstance(options, RoutingOptions):
        raise ValueError('readability refinement options must be RoutingOptions')
    started = perf_counter()
    until = min(started+seconds, deadline) if deadline is not None else started+seconds
    placement, result = deepcopy(pl), deepcopy(metadata)
    plan = _plan(result)
    model = build_refinement_model(par, comps, nets, index, plan)
    source_ids = {index[row['device']] for row in model['sources']}
    placement.source_orientation_ids = set(getattr(placement, 'source_orientation_ids', ())) | source_ids
    baseline = deepcopy(placement)
    current = measure_refinements(par, placement, comps, nets, index, plan, phase=phase)
    passive_current = _passive_measurement(placement, comps, index, plan)
    baseline_hpwl = _hpwl(par, placement, nets, index)
    report = dict(version=VERSION, stage='after_sa_before_final_routing', phase=phase,
        status='unchanged', model=model, before=deepcopy(current), after=deepcopy(current),
        candidates=0, accepted=0, actions=[], static_rejections=0, hpwl_rejections=0,
        legal_rejections=0, bank_rejections=0, stage_counts={}, seconds_limit=seconds, max_candidates=max_candidates,
        compaction_applicable=current['compaction_applicable'], compaction_reason=current['compaction_reason'],
        estimated_hpwl_before=baseline_hpwl, estimated_hpwl_limit=baseline_hpwl*1.15,
        estimated_hpwl_ratio_limit=1.15, passive_before=deepcopy(passive_current), passive_rejections=0)
    result.setdefault('layout_readability', {})['refinement'] = report
    if (getattr(placement, 'power_connections', None) is not None
            or not all(hasattr(placement, attr) for attr in ('column_x', 'row_y', 'cell_of', 'orient'))):
        report.update(status='skipped', stop_reason='unsupported_or_custom_power_placement')
        return placement, result
    stop, seen = 'no_improvement', set()
    stages = ['source'] if phase == 3 else ['passive', 'source', 'compact']
    for stage in stages:
        report['stage_counts'][stage] = 0
        if stage == 'compact' and not current['compaction_applicable']:
            report.setdefault('skipped_stages', {})['compact'] = current['compaction_reason']
            continue
        quota = max_candidates if phase == 3 else max(1, max_candidates//4) if stage == 'passive' else max(1, max_candidates//2) if stage == 'source' else max_candidates
        while report['stage_counts'][stage] < quota:
            if perf_counter() >= until or report['candidates'] >= max_candidates:
                stop = 'time_budget' if perf_counter() >= until else 'candidate_limit'; break
            generator = (_passive_trials(placement, comps, index, plan) if stage == 'passive' else
                         _source_trials(par, placement, comps, index, model, until) if stage == 'source' else
                         _compaction_trials(par, placement, comps, nets, index, result, options))
            accepted = False
            for trial, action in generator:
                if perf_counter() >= until or report['candidates'] >= max_candidates:
                    stop = 'time_budget' if perf_counter() >= until else 'candidate_limit'; break
                if report['stage_counts'][stage] >= quota:
                    break
                identity = (tuple(trial.cell_of), tuple(trial.orient), tuple(trial.row_y),
                            tuple(trial.column_x), tuple(getattr(trial, 'offset_x', ())))
                if identity in seen:
                    continue
                seen.add(identity)
                report['candidates'] += 1; report['stage_counts'][stage] += 1
                if not _valid(baseline, trial, comps, index, source_ids, options):
                    report['legal_rejections'] += 1; continue
                measured = measure_refinements(par, trial, comps, nets, index, plan, phase=phase)
                passive_measured = _passive_measurement(trial, comps, index, plan)
                passive_safe, passive_improved = _passive_guard(passive_current, passive_measured)
                if not passive_safe:
                    report['passive_rejections'] += 1; continue
                if not _bank_guard(current, measured):
                    report['bank_rejections'] += 1; continue
                compact_improved = _compact_key(measured) < _compact_key(current)
                improved = (_source_key(measured) < _source_key(current) if stage == 'source' else
                            _source_key(measured) <= _source_key(current)
                            and compact_improved)
                if not improved:
                    continue
                if _hpwl(par, trial, nets, index) > baseline_hpwl*1.15+EPS:
                    report['hpwl_rejections'] += 1; continue
                try:
                    safe = _static_safe(par, trial, nets, index, options, until)
                except RoutingFailure:
                    if perf_counter() < until:
                        raise
                    stop = 'time_budget'; break
                if not safe:
                    report['static_rejections'] += 1; continue
                changes = [dict(device=comp['id'], cell_before=list(placement.cell_of[i]),
                    cell_after=list(trial.cell_of[i]), orientation_before=placement.orient[i],
                    orientation_after=trial.orient[i], position_before=list(placement.pos_of(i)),
                    position_after=list(trial.pos_of(i))) for i, comp in enumerate(comps)
                    if placement.cell_of[i] != trial.cell_of[i] or placement.orient[i] != trial.orient[i]
                    or placement.pos_of(i) != trial.pos_of(i)]
                report['actions'].append(dict(action, before=deepcopy(current), after=deepcopy(measured), changes=changes))
                placement, current, passive_current = trial, measured, passive_measured
                report['accepted'] += 1; accepted = True
                break  # Rebuild from the accepted state, never a stale seed.
            if not accepted or stop in ('time_budget', 'candidate_limit'):
                break
        if stop in ('time_budget', 'candidate_limit'):
            break
    report.update(status='improved' if report['accepted'] else 'unchanged', stop_reason=stop,
        after=deepcopy(current), current=deepcopy(current), elapsed_seconds=perf_counter()-started,
        estimated_hpwl_after=_hpwl(par, placement, nets, index), passive_after=deepcopy(passive_current))
    result['layout_grid'] = dict(column_x=list(placement.column_x), row_y=list(placement.row_y))
    result['layout_readability']['refinement_metrics'] = current
    return placement, result
