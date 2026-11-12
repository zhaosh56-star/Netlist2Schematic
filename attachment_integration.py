"""Optional, transactional junction slots for attachments inside active groups.

The source plan is immutable.  Initialisation may insert display columns (and,
for a multi-path attachment, a necessary display row); annealing only changes
cells and orientations.  Attachment membership never joins active ownership.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
import json
from math import isfinite
from statistics import median
from time import perf_counter

from passive_placement import FlexiblePassivePlacement
from structured_placement import StructuredPlacement
from readability_layout import (build_readability_plan, measure_readability,
                                junction_point)

VERSION = 'attachment-integration-v2'
ACTIVE = frozenset(('nmos', 'pmos', 'npn', 'pnp'))
PASSIVE = frozenset(('resistor', 'capacitor', 'inductor', 'diode'))
EPS = 1e-8


def new_attachment_budget(*, seconds=1.0, max_candidates=128):
    if (type(seconds) not in (int, float) or not isfinite(seconds)
            or not 0 <= seconds <= 1 or type(max_candidates) is not int
            or not 0 <= max_candidates <= 128):
        raise ValueError('attachment budget requires 0..1 seconds and 0..128 candidates')
    return dict(seconds_limit=float(seconds), max_candidates=max_candidates,
                spent_seconds=0.0, candidates=0)


def _begin_budget(budget, deadline):
    budget = new_attachment_budget() if budget is None else budget
    for key in ('seconds_limit', 'max_candidates', 'spent_seconds', 'candidates'):
        if key not in budget:
            raise ValueError('incomplete attachment budget')
    new_attachment_budget(seconds=budget['seconds_limit'], max_candidates=budget['max_candidates'])
    if (type(budget['spent_seconds']) not in (int, float) or not isfinite(budget['spent_seconds'])
            or budget['spent_seconds'] < 0 or type(budget['candidates']) is not int or budget['candidates'] < 0):
        raise ValueError('invalid attachment budget usage')
    remaining = 0.0 if budget.get('exhausted') else max(0.0, budget['seconds_limit']-budget['spent_seconds'])
    started = perf_counter()
    until = started+remaining
    if deadline is not None:
        until = min(until, deadline)
    return budget, started, until


def _end_budget(budget, started):
    # A single geometric validation may finish just after its deadline. Keep
    # the actual time visible; a later stage receives no fresh allowance.
    budget['spent_seconds'] += max(0.0, perf_counter()-started)


def _has_budget(budget, until):
    return not budget.get('exhausted') and perf_counter() < until and budget['candidates'] < budget['max_candidates']


def build_attachment_plan(devices, comps, nets, index, structs, levels, row_of, *, policy='passive'):
    """Compile real terminal roles; no placement or random state is changed."""
    if policy not in ('passive', 'all'):
        raise ValueError('attachment policy must be passive or all')
    readable = build_readability_plan(devices, comps, nets, index, structs, levels, row_of)
    rows = readable['display_rows']
    from structured_placement import _relations
    _, _, blocks = _relations(devices, structs, index, [rows[c['id']] for c in comps])
    owners = [sorted(comps[i]['id'] for i in block) for block in blocks]
    # Unique physical continuation only. A common gate, a fork and a tail
    # shared by two sources do not establish one geometric current axis.
    host_axes, neighbours = [], defaultdict(set)
    by_id = {c['id']: c for c in comps}
    for net, junction in readable['junctions'].items():
        ports = junction['conduction_ports']
        if junction['is_rail'] or len(ports) != 2 or ports[0][0] == ports[1][0]:
            continue
        (a, pa), (b, pb) = ports
        drain = ('D', 'C')
        source = ('S', 'E')
        direct = (pa in drain and pb in source) or (pb in drain and pa in source)
        complementary = (pa in drain and pb in drain
            and {by_id[a]['symbol'], by_id[b]['symbol']} in ({'nmos', 'pmos'}, {'npn', 'pnp'}))
        if direct or complementary:
            host_axes.append(dict(a=a, pin_a=pa, b=b, pin_b=pb, net=net))
            neighbours[a].add(b); neighbours[b].add(a)
    lanes, seen = [], set()
    for name in sorted(neighbours):
        if name in seen:
            continue
        pending, lane = [name], set()
        while pending:
            node = pending.pop()
            if node in lane:
                continue
            lane.add(node); pending.extend(neighbours[node]-lane)
        seen.update(lane)
        lanes.append(sorted(lane))
    groups = []
    # A parent bridge owns its paths for proposals, without making a rigid block.
    for pattern in readable['patterns']:
        if pattern.get('parent') or not pattern.get('owner_junctions'):
            continue
        group = deepcopy(pattern)
        ports = sorted({tuple(port) for net in group['owner_junctions']
                        for port in readable['junctions'][net]['active_ports']})
        hosts = sorted({name for name, _ in ports})
        group.update(host_ports=[list(p) for p in ports], host_members=hosts,
            host_blocks=[block for block in owners if set(block) & set(hosts)],
            host_lanes=[lane for lane in lanes if set(lane) & set(hosts)],
            confidence='high', requires_interior=(len(group['owner_junctions']) >= 2
                and group['kind'] in ('series_chain', 'multi_path_bridge', 'two_arm_join',
                                      'single_attachment', 'parallel_bank')))
        groups.append(group)
    active_rows = {c['id']: rows[c['id']] for c in comps if c['symbol'] in ACTIVE}
    active_bands = set(active_rows.values())
    reserved_rows = sorted({rows[c['id']] for c in comps if c['symbol'] in PASSIVE
                            and rows[c['id']] not in active_bands})
    source_model = None
    source_groups = []
    if policy == 'all':
        from attachment_sources import build_source_groups
        source_model = build_source_groups(devices, comps, nets, index, display_rows=rows)
        for source_group in source_model['groups']:
            source_group = deepcopy(source_group)
            hosts = set(source_group['host_members'])
            source_group.update(host_blocks=[block for block in owners if set(block) & hosts],
                host_lanes=[], requires_interior=False)
            source_groups.append(source_group)
    source_managed = sorted({n for g in source_groups for n in g['members']})
    plan = dict(version=VERSION, policy=policy, supported=bool(groups or source_groups), groups=groups,
        readability_plan=readable, branch_row_plan=deepcopy(readable.get('branch_row_plan')),
        display_rows=deepcopy(rows), active_rows=active_rows,
        coverage=deepcopy(readable['coverage']), sources=[], host_axes=host_axes,
        reserved_passive_rows=reserved_rows,
        preserve_feedback_rows=any(s['kind'] == 'feedback_network' for s in structs))
    plan.update(source_model=source_model, source_groups=source_groups,
                source_managed_members=source_managed)
    plan['signature'] = sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return plan


def _motion_groups(plan):
    managed = set(plan.get('source_managed_members', ()))
    return [g for g in plan['groups'] if not managed.intersection(g['members'])] + list(plan.get('source_groups', ()))


def _passive_motion_groups(plan):
    managed = set(plan.get('source_managed_members', ()))
    return [g for g in plan['groups'] if not managed.intersection(g['members'])]


def _envelope_ids(group, index):
    return [index[n] for n in group['host_members'] if n in index]


def _role_band(pl, group, index, plan):
    """Use actual junction access bands, never an entire owner rectangle."""
    readable = plan['readability_plan']
    bands, points = [], []
    for net in group['owner_junctions']:
        junction = readable['junctions'][net]
        ports = junction.get('conduction_ports') or junction.get('control_ports') or ()
        ports = [port for port in ports if port[0] in index]
        if not ports:
            continue
        rows = [pl.cell_of[index[name]][0] for name, _ in ports]
        bands.append((min(rows), max(rows)))
        point = junction_point(pl, index, readable, net)
        if point is not None:
            points.append(point)
        # A shared single junction has its own real terminal span; it does not
        # inherit the unrelated mirror/output members of a host's owner block.
        if len(group['owner_junctions']) == 1:
            points = [pl.pin_world(index[name], pin) for name, pin in ports]
    if not points or not bands:
        return None
    lo_r, hi_r = max(a for a, _ in bands), min(b for _, b in bands)
    if lo_r > hi_r:
        # A genuine bridge between disjoint access rows uses the intervening
        # band, with active display order preserved by the immutable policy.
        lo_r, hi_r = hi_r, lo_r
    extra = getattr(pl, 'attachment_junction_rows', {}).get(group['id'], ())
    allowed_rows = set(range(lo_r, hi_r+1)) | set(extra)
    return min(p[0] for p in points), max(p[0] for p in points), lo_r, hi_r, allowed_rows


def _embedded(pl, group, index, plan=None):
    plan = plan or getattr(pl, 'attachment_plan', None)
    band = _role_band(pl, group, index, plan) if plan else None
    if band is None:
        return []
    lo_x, hi_x, lo_r, hi_r, allowed_rows = band
    result = []
    for name in group['members']:
        row, col = pl.cell_of[index[name]]
        x, _ = pl.pos_of(index[name])
        horizontal = lo_x+EPS < x < hi_x-EPS and row in allowed_rows
        vertical = lo_r < row < hi_r and lo_x-EPS <= x <= hi_x+EPS
        if horizontal or vertical:
            result.append(name)
    if group.get('requires_interior'):
        if len(result) != len(group['members']) or _port_errors(pl, pl.comps, index, plan, group):
            return []
        for path in _group_paths(group):
            a = junction_point(pl, index, plan['readability_plan'], path['path'][0])
            b = junction_point(pl, index, plan['readability_plan'], path['path'][-1])
            if a is None or b is None:
                continue
            axis = _path_axis(pl, index, path, a, b)
            direction = 1 if b[axis] >= a[axis] else -1
            points = [pl.pos_of(index[n]) for n in path['members']]
            if any(direction*(q[axis]-p[axis]) <= EPS for p, q in zip(points, points[1:])):
                return []
    return result


def _order_errors(pl, comps, index, plan):
    original = plan['active_rows']
    names = sorted(original)
    errors = 0
    for n, a in enumerate(names):
        for b in names[n+1:]:
            delta = original[a]-original[b]
            current = pl.cell_of[index[a]][0]-pl.cell_of[index[b]][0]
            errors += (delta == 0 and current != 0) or (delta != 0 and delta*current <= 0)
    return errors


def _source_chain_errors(pl, comps, index, group):
    from attachment_sources import source_chain_errors
    return source_chain_errors(pl, comps, index, group)


def _group_paths(group):
    if group.get('paths'):
        return group['paths']
    if len(group.get('path', ())) == len(group['members'])+1:
        return [dict(members=group['members'], path=group['path'])]
    return []


def _port_errors(pl, comps, index, plan, group):
    errors = 0
    if len(group.get('owner_junctions', ())) == 1:
        owner = group['owner_junctions'][0]
        anchor = junction_point(pl, index, plan['readability_plan'], owner)
        members = {index[n] for n in group['members']}
        by_net = defaultdict(list)
        for i in members:
            for pin, net in comps[i]['nodes'].items():
                by_net[net].append((i, pin))
        vectors = {'north': (0,-1), 'south': (0,1), 'west': (-1,0), 'east': (1,0)}
        for i in members:
            for pin, net in comps[i]['nodes'].items():
                other = [(j,p) for j,p in by_net[net] if j != i]
                target = anchor if net == owner else pl.pos_of(other[0][0]) if len(other) == 1 else None
                if target is None:
                    continue
                center = pl.pos_of(i)
                vector = vectors.get(pl.pin_dir(i, pin), (0,0))
                errors += (target[0]-center[0])*vector[0]+(target[1]-center[1])*vector[1] <= EPS
        return errors
    for path in _group_paths(group):
        a = junction_point(pl, index, plan['readability_plan'], path['path'][0])
        b = junction_point(pl, index, plan['readability_plan'], path['path'][-1])
        if a is None or b is None:
            continue
        axis = _path_axis(pl, index, path, a, b)
        direction = 1 if b[axis] >= a[axis] else -1
        for k, name in enumerate(path['members']):
            nodes = comps[index[name]]['nodes']
            before = [pin for pin, net in nodes.items() if net == path['path'][k]]
            after = [pin for pin, net in nodes.items() if net == path['path'][k+1]]
            if len(before) != 1 or len(after) != 1:
                errors += 1
                continue
            p = pl.pin_world(index[name], before[0]); q = pl.pin_world(index[name], after[0])
            errors += direction*(q[axis]-p[axis]) <= EPS
    return errors


def _path_axis(pl, index, path, a, b):
    points = [pl.pos_of(index[n]) for n in path['members']]
    spans = [max(p[k] for p in points)-min(p[k] for p in points) for k in (0, 1)]
    if max(spans) > EPS:
        return 0 if spans[0] >= spans[1] else 1
    return 0 if abs(b[0]-a[0]) >= abs(b[1]-a[1]) else 1


def _mirror_orientation(pl, i):
    old = pl.orient[i]
    center = pl.pos_of(i)
    pins = tuple(pl.comps[i]['nodes'])
    original = {pin: pl.pin_world(i, pin) for pin in pins}
    choices = []
    for orientation in pl.allowed_orientations(i):
        pl.orient[i] = orientation
        cost = sum(abs(pl.pin_world(i, pin)[0]+p[0]-2*center[0])
                   +abs(pl.pin_world(i, pin)[1]-p[1]) for pin, p in original.items())
        choices.append((cost, orientation != old, orientation))
    pl.orient[i] = old
    return min(choices)[-1]


def measure_attachments(pl, comps, index, plan):
    """Measure the same evidence against old, new, or reconstructed geometry."""
    result = dict(version=VERSION, plan_signature=plan.get('signature'),
        supported=bool(plan.get('supported')), measurable=False, group_count=len(plan.get('groups', ())),
        member_count=len({n for g in plan.get('groups', ()) for n in g['members']}),
        interior_member_count=len({n for g in plan.get('groups', ())
                                  if g.get('requires_interior') for n in g['members']}),
        violations=None, shape_error=None, pin_axis_error=None, owner_distance=None, port_direction_errors=None,
        embedded_groups=0, embedded_members=0, active_order_errors=None, groups=[],
        source_count=len(plan.get('source_groups', ())), source_errors=None,
        source_distance=None, source_measurable=False, source_raw_errors=None,
        source_chain_errors=None, sources=[])
    if not plan.get('supported'):
        return result
    try:
        physical = measure_readability(pl, comps, index, plan['readability_plan'])
        if plan['groups'] and not physical.get('measurable'):
            return result
        source = None
        if plan.get('source_groups'):
            from attachment_sources import measure_source_groups
            source = measure_source_groups(pl, comps, {}, index, plan['source_model'])
            if not source['measurable']:
                result['sources'] = source['records']
                return result
        for i in index.values():
            if not all(isfinite(v) for v in pl.pos_of(i)):
                return result
        details = [dict(id=g['id'], kind=g['kind'], members=list(g['members']),
                        requires_interior=g.get('requires_interior', False),
                        port_direction_errors=_port_errors(pl, comps, index, plan, g),
                        embedded_members=_embedded(pl, g, index, plan)) for g in plan['groups']]
        embedded = {n for g in details if g['requires_interior'] for n in g['embedded_members']}
        host_error = sum(abs(pl.pin_world(index[a['a']], a['pin_a'])[0]
                             -pl.pin_world(index[a['b']], a['pin_b'])[0])/100 for a in plan.get('host_axes', ()))
        port_errors = sum(g['port_direction_errors'] for g in details)
        result.update(measurable=True, violations=(physical.get('violations') or 0)+port_errors,
            port_direction_errors=port_errors,
            shape_error=physical.get('shape_error') or 0.0,
            pin_axis_error=(physical.get('pin_axis_error') or 0.0)+host_error,
            owner_distance=physical.get('owner_locality') or 0.0,
            embedded_groups=sum(bool(g['embedded_members']) and g['requires_interior'] for g in details),
            embedded_members=len(embedded), active_order_errors=_order_errors(pl, comps, index, plan),
            groups=details)
        if source is not None:
            result.update(source_errors=source['source_errors'], source_distance=source['source_distance'],
                source_measurable=True, source_raw_errors=source['raw_source_errors'],
                source_chain_errors=source['chain_errors'], sources=source['records'])
    except (KeyError, IndexError, TypeError, ValueError):
        return result
    return result


def attachment_rank(metrics):
    if not metrics.get('measurable'):
        return None
    passive = (metrics['active_order_errors'], metrics['violations'], -metrics['embedded_members'],
               metrics['shape_error']+metrics['pin_axis_error'], metrics['owner_distance'])
    return passive + ((metrics['source_errors'], metrics['source_distance']) if metrics['source_count'] else (0, 0))


def _source_improves(before, after):
    if (not before.get('measurable') or not after.get('measurable')
            or not before['source_count'] or before['source_count'] != after['source_count']):
        return False
    if any(after[k] > before[k]+EPS for k in ('violations', 'active_order_errors',
            'shape_error', 'pin_axis_error', 'owner_distance')):
        return False
    if after['embedded_members'] < before['embedded_members']:
        return False
    return (after['source_errors'], after['source_distance']) < (before['source_errors'], before['source_distance'])


def score_attachments(pl, comps, index, plan):
    metrics = measure_attachments(pl, comps, index, plan)
    if not metrics['measurable']:
        return dict(weighted_total=0.0, metrics=metrics)
    missing = max(0, metrics['interior_member_count']-metrics['embedded_members'])
    value = (1.25*metrics['violations'] + 2.5*metrics['active_order_errors']
             + metrics['shape_error'] + 2.5*metrics['pin_axis_error']
             + .35*metrics['owner_distance'] + .75*missing)
    if metrics['source_count']:
        value += 1.25*metrics['source_errors'] + .5*metrics['source_distance']
    return dict(weighted_total=value, metrics=metrics)


def _fixed_ids(pl, index):
    return {index[n] if isinstance(n, str) and n in index else n
            for n in set(getattr(pl, 'fixed', ())) | set(getattr(pl, 'fixed_cells', {}))}


class AttachmentPlacement(FlexiblePassivePlacement):
    """An opt-in policy; all proposal state is cells plus orientations."""
    def set_state(self, cells, orientations):
        self.cell_of = [tuple(cell) for cell in cells]
        self.orient = list(orientations)
        self.row_of = [row for row, _ in self.cell_of]
        self.comp_at = {cell: i for i, cell in enumerate(self.cell_of)}

    def allowed_orientations(self, i):
        if i in getattr(self, 'attachment_orientation_ids', ()):
            return list(range(self.n_orients(i)))
        return super().allowed_orientations(i)

    def blocks_contiguous(self):
        permitted = getattr(self, 'attachment_guests', {})
        for block in self.blocks + getattr(self, 'layout_macro_blocks', []):
            own = frozenset(block)
            groups = permitted.get(own, ())
            by_row = defaultdict(list)
            for i in block:
                by_row[self.cell_of[i][0]].append(self.cell_of[i][1])
            for row, columns in by_row.items():
                for col in range(min(columns), max(columns)+1):
                    occupant = self.comp_at.get((row, col))
                    if occupant is not None and occupant not in own:
                        name = self.comps[occupant]['id']
                        if not any(name in group['members'] and name in _embedded(
                                self, group, self.attachment_index, self.attachment_plan) for group in groups):
                            return False
        return True

    def valid_state(self):
        if (len(self.comp_at) != len(self.comps) or len(self.cell_of) != len(self.comps)
                or self.comp_at != {c: i for i, c in enumerate(self.cell_of)}
                or self.row_of != [r for r, _ in self.cell_of]):
            return False
        if (tuple(self.column_x), tuple(self.row_y)) != self.attachment_axes:
            return False
        for i, (row, col) in enumerate(self.cell_of):
            if (type(row) is not int or type(col) is not int or not 0 <= row < len(self.row_y)
                    or not 0 <= col < self.cols or self.orient[i] not in self.allowed_orientations(i)):
                return False
        if any(self.cell_of[i][0] != row for i, row in self.fixed_rows.items()):
            return False
        if any(self.cell_of[i][0] not in self.attachment_source_rows for i in self.attachment_source_ids):
            return False
        for i, (cell, orient, point) in self.attachment_fixed.items():
            if self.cell_of[i] != cell or self.orient[i] != orient or self.pos_of(i) != point:
                return False
        if not self.blocks_contiguous():
            return False
        index = self.attachment_index
        for axis, limit in self.attachment_axis_limits:
            error = abs(self.pin_world(index[axis['a']], axis['pin_a'])[0]
                        -self.pin_world(index[axis['b']], axis['pin_b'])[0])
            if error > limit+EPS:
                return False
        for identifier, members in self.attachment_required_embeddings.items():
            group = next(g for g in self.attachment_plan['groups'] if g['id'] == identifier)
            if not set(members) <= set(_embedded(self, group, index)):
                return False
            if _port_errors(self, self.comps, index, self.attachment_plan, group) > self.attachment_port_limits.get(identifier, 0):
                return False
        for group in self.attachment_plan['groups']:
            if (group['id'] in self.attachment_port_limits and _port_errors(self, self.comps,
                    index, self.attachment_plan, group) > self.attachment_port_limits[group['id']]):
                return False
        limits = self.attachment_shape_limits
        if limits:
            measured = measure_readability(self, self.comps, index, self.attachment_plan['readability_plan'])
            for pattern in measured.get('patterns', ()):
                if pattern['id'] in limits and any(pattern[key] > value+EPS
                        for key, value in limits[pattern['id']].items()):
                    return False
        if self.attachment_plan.get('source_groups'):
            from attachment_sources import measure_source_groups
            measured = measure_source_groups(self, self.comps, {}, index, self.attachment_plan['source_model'])
            if not measured['measurable']:
                return False
            if any(r['id'] in self.attachment_source_error_limits
                   and r['source_errors'] > self.attachment_source_error_limits[r['id']]
                   for r in measured['records']):
                return False
            for group in self.attachment_plan['source_groups']:
                if (group['id'] in self.attachment_source_port_limits
                        and _port_errors(self, self.comps, index, self.attachment_plan, group)
                            > self.attachment_source_port_limits[group['id']]):
                    return False
                if (group['id'] in self.attachment_source_chain_limits
                        and _source_chain_errors(self, self.comps, index, group)
                            > self.attachment_source_chain_limits[group['id']]):
                    return False
        return True

    def propose(self):
        count = self.attachment_move_counts
        if self.rng.random() < .25:
            count['joint_attempts'] += 1
            before = list(self.cell_of), list(self.orient)
            groups = _motion_groups(self.attachment_plan)
            if not groups:
                return None
            group = self.rng.choice(groups)
            if group['id'] in {g['id'] for g in self.attachment_plan.get('source_groups', ())}:
                from attachment_sources import source_group_states
                count['source_attempts'] += 1
                model = dict(self.attachment_plan['source_model'], groups=[group])
                choices = list(source_group_states(self, self.comps, {}, self.attachment_index,
                                                   model, max_candidates=8))
                if not choices:
                    return None
                cells, orientations, _ = self.rng.choice(choices)
                self.set_state(cells, orientations)
                if not self.valid_state():
                    count['slot_guard_rejections'] += 1
                    self.set_state(*before)
                    return None
                count['joint_legal'] += 1; count['source_legal'] += 1
                return before
            # Movement bundles are local and non-transitive. They do not alter
            # semantic ownership, slot eligibility, or the annealer snapshot.
            ids = {self.attachment_index[n] for n in group['members']}
            for block in group['host_blocks']:
                ids.update(self.attachment_index[n] for n in block)
            for lane in group.get('host_lanes', ()):
                ids.update(self.attachment_index[n] for n in lane)
            if ids & set(self.attachment_fixed):
                return None
            cells, orientations = list(self.cell_of), list(self.orient)
            if self.rng.random() < .5:
                action = 'translation'
                dc = self.rng.choice((-2, -1, 1, 2))
                for i in ids:
                    row, col = cells[i]
                    cells[i] = row, col+dc
            else:
                action = 'mirror'
                lo, hi = min(cells[i][1] for i in ids), max(cells[i][1] for i in ids)
                for i in ids:
                    row, col = cells[i]
                    cells[i] = row, lo+hi-col
                    if self.comps[i]['symbol'] in ('nmos', 'pmos', 'npn', 'pnp'):
                        alternatives = self.allowed_orientations(i)
                        if 1-orientations[i] in alternatives:
                            orientations[i] = 1-orientations[i]
                    elif i in self.attachment_orientation_ids:
                        orientations[i] = _mirror_orientation(self, i)
            self.set_state(cells, orientations)
            if not self.valid_state():
                count['slot_guard_rejections'] += 1
                self.set_state(*before)
                return None
            count['joint_legal'] += 1
            count[action+'_legal'] += 1
            return before
        count['ordinary_attempts'] += 1
        before = list(self.cell_of), list(self.orient)
        if self.passive_indices:
            old = super().propose()
        else:
            old = StructuredPlacement.propose(self)
        if old is None or not self.valid_state():
            count['ordinary_rejections'] += 1
            self.set_state(*before)
            return None
        count['ordinary_legal'] += 1
        return old


def _install(pl, comps, index, plan, *, required=None):
    clone = AttachmentPlacement.__new__(AttachmentPlacement)
    clone.__dict__ = deepcopy(pl.__dict__)
    clone.attachment_plan, clone.attachment_index = deepcopy(plan), dict(index)
    clone.attachment_axes = tuple(clone.column_x), tuple(clone.row_y)
    clone.cols = len(clone.column_x)
    fixed = _fixed_ids(clone, index)
    managed = {index[n] for n in plan.get('source_managed_members', ())}
    source_ids = {index[g['device']] for g in plan.get('source_groups', ())} - fixed
    movable = {i for i, c in enumerate(comps) if c['symbol'] in PASSIVE and i not in fixed} | source_ids
    clone.attachment_source_ids = frozenset(source_ids)
    clone.attachment_source_rows = tuple(range(len(clone.row_y)))
    clone.source_orientation_ids = frozenset(getattr(clone, 'source_orientation_ids', ())) | source_ids
    clone.passive_indices = tuple(i for i in sorted(movable) if comps[i]['symbol'] in PASSIVE and i not in managed)
    clone._passive_set = frozenset(clone.passive_indices)
    clone.fixed_rows = {i: cell[0] for i, cell in enumerate(clone.cell_of) if i not in movable}
    clone.attachment_fixed = {i: (tuple(clone.cell_of[i]), clone.orient[i], tuple(clone.pos_of(i)))
                              for i in _fixed_ids(clone, index)}
    clone.attachment_required_embeddings = dict(required or {})
    clone.attachment_shape_limits = getattr(clone, 'attachment_shape_limits', {})
    clone.attachment_port_limits = getattr(clone, 'attachment_port_limits', {})
    clone.attachment_source_error_limits = getattr(clone, 'attachment_source_error_limits', {})
    clone.attachment_source_port_limits = getattr(clone, 'attachment_source_port_limits', {})
    clone.attachment_source_chain_limits = getattr(clone, 'attachment_source_chain_limits', {})
    clone.attachment_axis_limits = getattr(clone, 'attachment_axis_limits', ())
    clone.attachment_junction_rows = getattr(clone, 'attachment_junction_rows', {})
    clone.attachment_row_origin = getattr(clone, 'attachment_row_origin', tuple(range(len(clone.row_y))))
    clone.attachment_orientation_ids = frozenset(index[n] for group in
        list(plan['groups'])+list(plan.get('source_groups', ())) for n in group['members'])
    guest_map = defaultdict(list)
    for group in list(plan['groups'])+list(plan.get('source_groups', ())):
        ids = {index[n] for n in group['members']}
        for block in getattr(clone, 'blocks', ()) + getattr(clone, 'layout_macro_blocks', []):
            names = {comps[i]['id'] for i in block}
            if names & set(group['host_members']):
                guest_map[frozenset(block)].append(group)
    clone.attachment_guests = {key: tuple(value) for key, value in guest_map.items()}
    clone.attachment_move_counts = getattr(clone, 'attachment_move_counts', {name: 0 for name in (
        'joint_attempts', 'joint_legal', 'translation_legal', 'mirror_legal',
        'slot_guard_rejections', 'ordinary_attempts', 'ordinary_legal', 'ordinary_rejections',
        'source_attempts', 'source_legal')})
    clone.attachment_move_counts.setdefault('source_attempts', 0)
    clone.attachment_move_counts.setdefault('source_legal', 0)
    # Flexible instances already have these. Plain/source-only seeds do not.
    clone.passive_neighbors = getattr(clone, 'passive_neighbors', {i: () for i in clone.passive_indices})
    clone.passive_relations = getattr(clone, 'passive_relations',
                                     dict(series=[], parallel=[], feedback=[], skipped={}))
    clone.move_counts = getattr(clone, 'move_counts', {name: 0 for name in (
        'total_proposals', 'passive_attempts', 'passive_legal', 'passive_moves',
        'passive_swaps', 'passive_cross_row', 'passive_rejected', 'legacy_attempts',
        'legacy_legal', 'legacy_rejected')})
    clone.set_state(clone.cell_of, clone.orient)
    return clone


def _insert_axis(pl, axis_name, cut, amount, anchor):
    """Insert common display slots; callers validate fixed world positions."""
    axis = list(getattr(pl, axis_name))
    pitch = median([b-a for a, b in zip(axis, axis[1:])]) if len(axis) > 1 else 112
    pitch = max(100, pitch)
    if anchor == 'left':
        start = axis[cut-1] if cut else axis[0]-pitch
        added = [start+pitch*(n+1) for n in range(amount)]
        values = axis[:cut]+added+[x+amount*pitch for x in axis[cut:]]
    else:
        end = axis[cut] if cut < len(axis) else axis[-1]+pitch
        added = [end-pitch*(amount-n) for n in range(amount)]
        values = [x-amount*pitch for x in axis[:cut]]+added+axis[cut:]
    coordinate = 1 if axis_name == 'column_x' else 0
    cells = [tuple(v+amount if k == coordinate and v >= cut else v
                   for k, v in enumerate(cell)) for cell in pl.cell_of]
    pl.fixed_cells = {i: tuple(v+amount if k == coordinate and v >= cut else v
                              for k, v in enumerate(cell))
                      for i, cell in getattr(pl, 'fixed_cells', {}).items()}
    setattr(pl, axis_name, values)
    if axis_name == 'row_y' and hasattr(pl, 'attachment_junction_rows'):
        pl.attachment_junction_rows = {key: tuple(r+amount if r >= cut else r for r in rows)
                                      for key, rows in pl.attachment_junction_rows.items()}
        pl.attachment_row_origin = (pl.attachment_row_origin[:cut]+(None,)*amount
                                    +pl.attachment_row_origin[cut:])
    pl.cols = len(pl.column_x)
    pl.set_state(cells, pl.orient)


def _orientation(pl, i, horizontal):
    preferred = 1 if horizontal else 0
    return preferred if preferred in pl.allowed_orientations(i) else pl.orient[i]


def _orient_path(pl, comps, index, plan, path, *, axis=None):
    """Point real terminal nets along the drawn path, without swapping pins."""
    nodes = path.get('path', ())
    if len(nodes) != len(path['members'])+1:
        return
    a = junction_point(pl, index, plan['readability_plan'], nodes[0])
    b = junction_point(pl, index, plan['readability_plan'], nodes[-1])
    if a is None or b is None:
        return
    axis = _path_axis(pl, index, path, a, b) if axis is None else axis
    direction = 1 if b[axis] >= a[axis] else -1
    for k, name in enumerate(path['members']):
        i = index[name]
        before = [pin for pin, net in comps[i]['nodes'].items() if net == nodes[k]]
        after = [pin for pin, net in comps[i]['nodes'].items() if net == nodes[k+1]]
        if len(before) != 1 or len(after) != 1:
            continue
        choices = []
        for orientation in pl.allowed_orientations(i):
            pl.orient[i] = orientation
            p, q = pl.pin_world(i, before[0]), pl.pin_world(i, after[0])
            choices.append(((direction*(q[axis]-p[axis]) <= EPS,
                             abs(q[1-axis]-p[1-axis]), orientation), orientation))
        if choices:
            pl.orient[i] = min(choices)[1]


def _rehome_related_shunts(pl, comps, index, plan, moving_group):
    """A moved stack may need its own shunt outside the new interior lane."""
    active_nets = set(moving_group['owner_junctions'])
    fixed = _fixed_ids(pl, index)
    for group in plan['groups']:
        if (group['kind'] != 'single_attachment' or len(group['members']) != 1
                or len(group['owner_junctions']) != 1
                or not active_nets.intersection(group['owner_junctions'])
                or set(group['members']).intersection(plan.get('source_managed_members', ()))):
            continue
        i = index[group['members'][0]]
        if i in fixed:
            continue
        point = junction_point(pl, index, plan['readability_plan'], group['owner_junctions'][0])
        if point is None:
            continue
        occupied = {cell for j, cell in enumerate(pl.cell_of) if j != i}
        candidates = sorted(((r, c) for r in range(len(pl.row_y)) for c in range(pl.cols)
                             if (r, c) not in occupied), key=lambda cell: (
            abs(pl.column_x[cell[1]]-point[0])+abs(pl.row_y[cell[0]]-point[1]), cell))
        original = list(pl.cell_of), list(pl.orient)
        for target in candidates[:32]:
            cells = list(pl.cell_of); cells[i] = target
            pl.set_state(cells, pl.orient)
            allowed = True
            for block in pl.blocks + getattr(pl, 'layout_macro_blocks', []):
                columns = [pl.cell_of[j][1] for j in block if pl.cell_of[j][0] == target[0]]
                if i not in block and columns and min(columns) <= target[1] <= max(columns):
                    allowed &= group['members'][0] in _embedded(pl, group, index, plan)
            if not allowed:
                pl.set_state(*original)
                continue
            net = group['owner_junctions'][0]
            pin = next(pin for pin, value in comps[i]['nodes'].items() if value == net)
            choices = []
            for orientation in pl.allowed_orientations(i):
                pl.orient[i] = orientation
                rejected = _rail_marker_direction_conflicts(pl, i)
                if rejected:
                    diagnostic = getattr(pl, 'attachment_slot_diagnostics', None)
                    if diagnostic is not None:
                        diagnostic['rail_direction_rejections'] += 1
                        available = max(0, 8-len(diagnostic['examples']))
                        diagnostic['examples'].extend(rejected[:available])
                    continue
                p = pl.pin_world(i, pin)
                choices.append((abs(p[0]-point[0])+abs(p[1]-point[1]), orientation))
            if not choices:
                pl.set_state(*original)
                continue
            pl.orient[i] = min(choices)[1]
            break
        else:
            pl.set_state(*original)


def _slot_specs(pl, group, index):
    if not group.get('requires_interior'):
        return
    hosts = _envelope_ids(group, index)
    if len(hosts) < 2:
        return
    columns = sorted({pl.cell_of[i][1] for i in hosts})
    if len(columns) < 2:
        return
    band = _role_band(pl, group, index, pl.attachment_plan)
    if band is None:
        return
    host_rows = [band[2], band[3]]
    if group['kind'] == 'multi_path_bridge':
        paths = sorted(group['paths'], key=lambda p: (-len(p['members']), tuple(p['members'])))
    else:
        paths = [dict(members=group['members'], path=group.get('path', []))]
    width, depth = max(len(p['members']) for p in paths), len(paths)
    cuts = sorted(range(columns[0]+1, columns[-1]+1),
                  key=lambda c: (abs(c-median(columns)), c))
    rows = sorted(range(min(host_rows), max(host_rows)+1),
                  key=lambda r: (abs(r-median(host_rows)), r))
    extra_rows = max(0, depth-(max(host_rows)-min(host_rows)+1))
    for cut in cuts:
        for row in rows:
            if row+depth-1 > max(host_rows)+extra_rows:
                continue
            # Try the existing space and minimal openings at every promising
            # cut; one large old gap must not consume the whole search budget.
            for amount in range(width+1):
                if cut+width >= pl.cols+amount:
                    continue
                for anchor in (('left',) if amount == 0 else ('left', 'right')):
                    yield dict(cut=cut, row=row, width=width, added_columns=amount,
                               depth=depth, added_rows=extra_rows,
                               row_cut=max(host_rows)+1, paths=paths, anchor=anchor)


def _joint_slot(pl, comps, index, plan, group, spec):
    trial = deepcopy(pl)
    trial.attachment_slot_diagnostics = dict(rail_direction_rejections=0, examples=[])
    # Every member moves together; no active outsider is evicted from a cell.
    if spec['added_columns']:
        _insert_axis(trial, 'column_x', spec['cut'], spec['added_columns'], spec['anchor'])
    if spec['added_rows']:
        _insert_axis(trial, 'row_y', spec['row_cut'], spec['added_rows'], spec['anchor'])
        trial.attachment_junction_rows = dict(trial.attachment_junction_rows)
        trial.attachment_junction_rows[group['id']] = tuple(range(
            spec['row_cut'], spec['row_cut']+spec['added_rows']))
    cells, orientations = list(trial.cell_of), list(trial.orient)
    lanes = group.get('host_lanes', ())
    if len(lanes) == 2:
        lanes = sorted(lanes, key=lambda lane: (median(cells[index[n]][1] for n in lane), tuple(lane)))
        for lane, column in zip(lanes, (spec['cut']-1, spec['cut']+spec['width'])):
            if len({cells[index[n]][0] for n in lane}) != len(lane):
                continue
            for name in lane:
                cells[index[name]] = cells[index[name]][0], column
    # Path ordering must see the new actual host endpoints, not the old seed.
    trial.set_state(cells, orientations)
    for lane, path in enumerate(spec['paths']):
        names = list(path['members'])
        ends = path.get('path', ())
        if len(ends) >= 2:
            a = junction_point(trial, index, plan['readability_plan'], ends[0])
            b = junction_point(trial, index, plan['readability_plan'], ends[-1])
            if a is not None and b is not None and a[0] > b[0]:
                names.reverse()
        inset = (spec['width']-len(names))//2
        for step, name in enumerate(names):
            i = index[name]
            cells[i] = spec['row']+lane, spec['cut']+inset+step
            orientations[i] = _orientation(trial, i, group['kind'] != 'parallel_bank')
    trial.set_state(cells, orientations)
    for path in _group_paths(group):
        _orient_path(trial, comps, index, plan, path, axis=0)
    _rehome_related_shunts(trial, comps, index, plan, group)
    return _install(trial, comps, index, plan, required=pl.attachment_required_embeddings)


def _fixed_world_safe(original, trial, index):
    return all(trial.pos_of(i) == original.pos_of(i) and trial.orient[i] == original.orient[i]
               for i in _fixed_ids(original, index))


def _static(par, pl, nets, index, options, until):
    from symmetry_layout import _static_safe
    from routing_rules import RoutingOptions
    from orthogonal_router import RoutingFailure
    try:
        return _static_safe(par, pl, nets, index, options or RoutingOptions(), until)
    except RoutingFailure as exc:
        if exc.diagnostics.get('reason') == 'quality_time_budget':
            return None
        raise


def _checked_static(par, pl, nets, index, options, until):
    # Keep the transaction boundary safe even when an alternate static oracle
    # raises the shared router's deadline exception directly.
    from orthogonal_router import RoutingFailure
    try:
        return _static(par, pl, nets, index, options, until)
    except RoutingFailure as exc:
        if exc.diagnostics.get('reason') == 'quality_time_budget':
            return None
        raise


def _release_empty_rows(par, pl, original, comps, nets, index, plan, options, budget, until, report):
    if plan.get('preserve_feedback_rows'):
        return pl, False
    protected = {r for rows in pl.attachment_junction_rows.values() for r in rows}
    protected.update(plan['display_rows'][n] for g in plan['groups']
                     if g.get('row_mode') == 'conduction_bridge' for n in g['members'])
    active_rows = [pl.cell_of[i][0] for i, c in enumerate(comps) if c['symbol'] in ACTIVE]
    if not active_rows:
        return pl, False
    empty = [r for r in range(min(active_rows)+1, max(active_rows))
             if r not in protected and r not in pl.row_of
             and pl.attachment_row_origin[r] in plan.get('reserved_passive_rows', ())]
    for row in reversed(empty):
        if not _has_budget(budget, until):
            break
        budget['candidates'] += 1; report['candidate_trials'] += 1
        trial = deepcopy(pl)
        delta = trial.row_y[row]-trial.row_y[row-1]
        trial.row_y = trial.row_y[:row]+[y-delta for y in trial.row_y[row+1:]]
        cells = [(r-(r > row), c) for r, c in trial.cell_of]
        trial.fixed_cells = {i: (r-(r > row), c) for i, (r,c) in trial.fixed_cells.items()}
        trial.attachment_row_origin = trial.attachment_row_origin[:row]+trial.attachment_row_origin[row+1:]
        trial.attachment_junction_rows = {key: tuple(r-(r > row) for r in rows)
                                         for key, rows in trial.attachment_junction_rows.items()}
        trial.set_state(cells, trial.orient)
        trial = _install(trial, comps, index, plan, required=pl.attachment_required_embeddings)
        if not trial.valid_state() or not _fixed_world_safe(original, trial, index):
            continue
        before, after = measure_attachments(pl, comps, index, plan), measure_attachments(trial, comps, index, plan)
        if attachment_rank(after) is None or attachment_rank(after) > attachment_rank(before):
            continue
        safe = _checked_static(par, trial, nets, index, options, until)
        if safe is None:
            budget['exhausted'] = True
            return pl, True
        if not safe:
            continue
        report['actions'].append(dict(kind='release_vacated_attachment_row', removed_row=row,
            original_display_row=pl.attachment_row_origin[row], removed_height=delta))
        pl = trial
    return pl, False


def initialize_attachments(pl, devices, comps, nets, index, plan, *, deadline=None,
                           budget=None, estimate=None, options=None):
    """Open and fill real interior slots, using a single bounded budget ledger."""
    budget, started, until = _begin_budget(budget, deadline)
    report = dict(version=VERSION, applied=False, status='skipped', candidate_trials=0,
                  source_candidate_trials=0, source_applied=False, passive_applied=False,
                  interior_candidate_trials=0, local_candidate_trials=0, group_trials={},
                  state_legal_trials=0, legal_trials=0, static_rejections=0,
                  slot_guard_rejections=0, rail_direction_rejections=0, actions=[], budget=budget)
    if not plan.get('supported'):
        report['reason'] = 'no_supported_attachments'
        _end_budget(budget, started)
        return pl, report
    if not _has_budget(budget, until):
        report['reason'] = 'attachment_budget'
        _end_budget(budget, started)
        return pl, report
    from structured_placement import par
    current = _install(pl, comps, index, plan)
    before = measure_attachments(pl, comps, index, plan)
    report['before'] = deepcopy(before)
    common = estimate or (lambda p: score_attachments(p, comps, index, plan)['weighted_total'])
    report['estimate_before'] = common(pl)
    groups = sorted((g for g in _passive_motion_groups(plan) if g.get('requires_interior')), key=lambda g: (
        {'multi_path_bridge': 0, 'series_chain': 1, 'two_arm_join': 2,
         'parallel_bank': 3, 'single_attachment': 4}.get(g['kind'], 5), g['id']))
    for group in groups:
        if set(index[n] for n in group['members']) & _fixed_ids(pl, index):
            continue
        measured = measure_attachments(current, comps, index, plan)
        best = None
        specs = _slot_specs(current, group, index)
        if specs is None:
            continue
        allowance = max(1, (budget['max_candidates']-budget['candidates'])//max(1, len(groups)))
        for local, spec in enumerate(specs):
            if local >= allowance or not _has_budget(budget, until):
                break
            budget['candidates'] += 1; report['candidate_trials'] += 1
            report['interior_candidate_trials'] += 1
            report['group_trials'][group['id']] = report['group_trials'].get(group['id'], 0)+1
            trial = _joint_slot(current, comps, index, plan, group, spec)
            report['rail_direction_rejections'] += trial.attachment_slot_diagnostics['rail_direction_rejections']
            if not trial.valid_state() or not _fixed_world_safe(pl, trial, index):
                report['slot_guard_rejections'] += 1
                continue
            after = measure_attachments(trial, comps, index, plan)
            if not after['measurable'] or attachment_rank(after) >= attachment_rank(measured):
                continue
            report['state_legal_trials'] += 1
            safe = _checked_static(par, trial, nets, index, options, until)
            if safe is None:
                budget['exhausted'] = True
                _end_budget(budget, started)
                report.update(status='rolled_back', reason='attachment_budget', actions=[], after=deepcopy(before))
                return pl, report
            if not safe:
                report['static_rejections'] += 1
                continue
            report['legal_trials'] += 1
            cost = common(trial)
            rank = attachment_rank(after)
            key = rank[:4], spec['added_columns']+spec['added_rows'], rank[4:], cost
            if best is None or key < best[0]:
                best = key, trial, spec, after
        if best is not None:
            _, current, spec, after = best
            current.attachment_required_embeddings = {
                g['id']: tuple(_embedded(current, g, index)) for g in plan['groups']
                if g.get('requires_interior') and _embedded(current, g, index)}
            report['actions'].append(dict(kind='joint_junction_slot', group=group['id'],
                members=list(group['members']), column_cut=spec['cut'], columns_added=spec['added_columns'],
                rows_added=spec['added_rows'], junction_rows=list(current.attachment_junction_rows.get(group['id'], ())),
                slot_diagnostics=deepcopy(current.attachment_slot_diagnostics),
                row=spec['row'], anchor=spec['anchor'], embedded_members=_embedded(current, group, index)))
    from attachment_local import enum_local_attachment_trials
    local_groups = [g for g in _passive_motion_groups(plan) if not g.get('requires_interior')
                    and len(g['owner_junctions']) == 1]
    for group in local_groups:
        if not _has_budget(budget, until):
            break
        measured = measure_attachments(current, comps, index, plan)
        best = None
        allowance = max(1, (budget['max_candidates']-budget['candidates'])//max(1, len(local_groups)))
        for local, (trial, action) in enumerate(enum_local_attachment_trials(
                current, comps, index, plan, group, until)):
            if local >= allowance or not _has_budget(budget, until):
                break
            budget['candidates'] += 1; report['candidate_trials'] += 1
            report['local_candidate_trials'] += 1
            report['group_trials'][group['id']] = report['group_trials'].get(group['id'], 0)+1
            if not trial.valid_state() or not _fixed_world_safe(pl, trial, index):
                report['slot_guard_rejections'] += 1
                continue
            after = measure_attachments(trial, comps, index, plan)
            if attachment_rank(after) is None or attachment_rank(after) >= attachment_rank(measured):
                continue
            report['state_legal_trials'] += 1
            safe = _checked_static(par, trial, nets, index, options, until)
            if safe is None:
                budget['exhausted'] = True
                _end_budget(budget, started)
                report.update(status='rolled_back', reason='attachment_budget', actions=[], after=deepcopy(before))
                return pl, report
            if not safe:
                report['static_rejections'] += 1
                continue
            report['legal_trials'] += 1
            key = attachment_rank(after), common(trial)
            if best is None or key < best[0]:
                best = key, trial, action
        if best:
            _, current, action = best
            report['actions'].append(action)
    if plan.get('source_groups'):
        from attachment_sources import source_group_trials
        report['source_candidate_trials'] = 0
        for group in plan['source_groups']:
            if not _has_budget(budget, until):
                break
            measured = measure_attachments(current, comps, index, plan)
            best = None
            allowance = max(1, (budget['max_candidates']-budget['candidates'])//len(plan['source_groups']))
            model = dict(plan['source_model'], groups=[group])
            for trial, action in source_group_trials(current, comps, nets, index, model,
                    deadline=until, max_candidates=min(128, allowance)):
                if not _has_budget(budget, until):
                    break
                budget['candidates'] += 1; report['candidate_trials'] += 1
                report['source_candidate_trials'] += 1
                report['group_trials'][group['id']] = report['group_trials'].get(group['id'], 0)+1
                if not trial.valid_state() or not _fixed_world_safe(pl, trial, index):
                    report['slot_guard_rejections'] += 1
                    continue
                after = measure_attachments(trial, comps, index, plan)
                if not _source_improves(measured, after):
                    continue
                report['state_legal_trials'] += 1
                safe = _checked_static(par, trial, nets, index, options, until)
                if safe is None:
                    budget['exhausted'] = True
                    _end_budget(budget, started)
                    report.update(status='rolled_back', reason='attachment_budget', actions=[], after=deepcopy(before))
                    return pl, report
                if not safe:
                    report['static_rejections'] += 1
                    continue
                report['legal_trials'] += 1
                key = after['source_errors'], after['source_distance'], attachment_rank(after), common(trial)
                if best is None or key < best[0]:
                    best = key, trial, action
            if best:
                _, current, action = best
                report['actions'].append(action)
    if report['actions']:
        current, expired = _release_empty_rows(par, current, pl, comps, nets, index, plan,
                                               options, budget, until, report)
        if expired:
            _end_budget(budget, started)
            report.update(status='rolled_back', reason='attachment_budget', actions=[], after=deepcopy(before))
            return pl, report
    if report['actions']:
        applied = {action.get('group') for action in report['actions']}
        protected_groups = [g for g in _passive_motion_groups(plan) if g['id'] in applied or any(
            current.cell_of[index[n]] != pl.cell_of[index[n]] or current.orient[index[n]] != pl.orient[index[n]]
            for n in g['members'])]
        protected = {n for g in protected_groups for n in g['members']}
        shapes = measure_readability(current, comps, index, plan['readability_plan'])
        current.attachment_shape_limits = {g['id']: {key: g[key] for key in
            ('orientation_errors', 'order_errors', 'shape_error')} for g in shapes['patterns']
            if set(g['members']) <= protected and g['kind'] in ('series_chain', 'parallel_bank', 'two_arm_join')}
        current.attachment_axis_limits = tuple((deepcopy(axis), abs(
            current.pin_world(index[axis['a']], axis['pin_a'])[0]
            -current.pin_world(index[axis['b']], axis['pin_b'])[0])) for axis in plan.get('host_axes', ()))
        current.attachment_port_limits = {g['id']: _port_errors(current, comps, index, plan, g)
                                         for g in protected_groups}
        if plan.get('source_groups'):
            from attachment_sources import measure_source_groups
            source = measure_source_groups(current, comps, nets, index, plan['source_model'])
            current.attachment_source_error_limits = {r['id']: r['source_errors'] for r in source['records']}
            current.attachment_source_port_limits = {g['id']: _port_errors(current, comps, index, plan, g)
                                                     for g in plan['source_groups']}
            current.attachment_source_chain_limits = {g['id']: _source_chain_errors(current, comps, index, g)
                                                      for g in plan['source_groups']}
        report['source_applied'] = any(a['kind'] == 'source_group_attachment' for a in report['actions'])
        report['passive_applied'] = any(a['kind'] != 'source_group_attachment' for a in report['actions'])
        report.update(applied=True, status='applied', reason='junction_organization',
            after=measure_attachments(current, comps, index, plan), estimate_after=common(current))
        report['estimate_ratio'] = report['estimate_after']/report['estimate_before'] if report['estimate_before'] else None
        _end_budget(budget, started)
        current.attachment_budget = deepcopy(budget)
        report['display_row_origin'] = list(current.attachment_row_origin)
        return current, report
    report.update(reason='no_legal_organization_improvement', after=deepcopy(before))
    _end_budget(budget, started)
    return pl, report


def refine_attachments(par, pl, comps, nets, index, metadata, *, options=None,
                       deadline=None, budget=None):
    """A slot-preserving, fixed-axis local finish; never opens another budget."""
    result = deepcopy(metadata)
    saved = result.get('layout_attachments', {})
    plan = saved.get('plan')
    budget = budget if budget is not None else saved.get('budget', getattr(pl, 'attachment_budget', None))
    budget, started, until = _begin_budget(budget, deadline)
    report = dict(version=VERSION, applied=False, candidates=0, accepted=0, actions=[], budget=budget)
    if not plan or not plan.get('supported') or not isinstance(pl, AttachmentPlacement):
        report['reason'] = 'no_attachment_seed'
    else:
        current = deepcopy(pl)
        baseline = measure_attachments(current, comps, index, plan)
        report['before'] = baseline
        from branch_alignment import _hpwl
        baseline_hpwl = _hpwl(par, pl, nets, index)
        report.update(estimated_hpwl_before=baseline_hpwl, estimated_hpwl_limit=baseline_hpwl*1.15,
                      hpwl_rejections=0)
        for group in _passive_motion_groups(plan):
            if not _has_budget(budget, until):
                break
            ids = {index[n] for n in group['members']}
            if ids & set(current.attachment_fixed):
                continue
            from attachment_local import enum_local_attachment_trials
            if len(group['owner_junctions']) == 1:
                proposals = enum_local_attachment_trials(current, comps, index, plan, group, until)
            else:
                def shifted():
                    for dr, dc in ((0,-1),(0,1),(-1,0),(1,0)):
                        trial = deepcopy(current)
                        cells = list(trial.cell_of)
                        for i in ids:
                            r, c = cells[i]; cells[i] = r+dr, c+dc
                        trial.set_state(cells, trial.orient)
                        yield trial, dict(kind='attachment_local_move', group=group['id'], delta=[dr,dc])
                proposals = shifted()
            for trial, action in proposals:
                if not _has_budget(budget, until):
                    break
                budget['candidates'] += 1; report['candidates'] += 1
                if not trial.valid_state():
                    continue
                measured = measure_attachments(trial, comps, index, plan)
                # Local shape/locality is a benefit even if the outer bbox is unchanged.
                if attachment_rank(measured) is None or attachment_rank(measured) >= attachment_rank(baseline):
                    continue
                if _hpwl(par, trial, nets, index) > baseline_hpwl*1.15+EPS:
                    report['hpwl_rejections'] += 1
                    continue
                safe = _checked_static(par, trial, nets, index, options, until)
                if safe is None:
                    budget['exhausted'] = True
                    report['reason'] = 'attachment_budget'
                    break
                if not safe:
                    continue
                current, baseline = trial, measured
                report['accepted'] += 1
                report['actions'].append(action)
        if plan.get('source_groups'):
            from attachment_sources import source_group_trials
            report['source_candidates'] = 0
            for group in plan['source_groups']:
                if not _has_budget(budget, until):
                    break
                model = dict(plan['source_model'], groups=[group])
                remaining = min(128, max(0, budget['max_candidates']-budget['candidates']))
                for trial, action in source_group_trials(current, comps, nets, index, model,
                        deadline=until, max_candidates=remaining):
                    if not _has_budget(budget, until):
                        break
                    budget['candidates'] += 1; report['candidates'] += 1; report['source_candidates'] += 1
                    if not trial.valid_state():
                        continue
                    measured = measure_attachments(trial, comps, index, plan)
                    if not _source_improves(baseline, measured):
                        continue
                    if _hpwl(par, trial, nets, index) > baseline_hpwl*1.15+EPS:
                        report['hpwl_rejections'] += 1
                        continue
                    safe = _checked_static(par, trial, nets, index, options, until)
                    if safe is None:
                        budget['exhausted'] = True
                        report['reason'] = 'attachment_budget'
                        break
                    if not safe:
                        continue
                    current, baseline = trial, measured
                    report['accepted'] += 1
                    report['actions'].append(action)
        report.update(applied=bool(report['accepted']), after=baseline)
        pl = current
    saved.update(refinement=report, budget=budget)
    if plan:
        saved['metrics'] = measure_attachments(pl, comps, index, plan)
    result['layout_attachments'] = saved
    _end_budget(budget, started)
    return pl, result


def finalize_attachment_state(pl, checkpoint):
    """Keep later legacy orientation/floor helpers inside the frozen contract.

    The checkpoint is captured after SA. This is a state guard, not another
    placement or search pass; authoritative geometry/routing still follows.
    """
    if not isinstance(checkpoint, AttachmentPlacement):
        return pl, dict(version=VERSION, status='not_applicable', reverted=False)
    safe = isinstance(pl, AttachmentPlacement) and pl.valid_state()
    if safe:
        safe = (pl.attachment_plan == checkpoint.attachment_plan
                and pl.attachment_axes == checkpoint.attachment_axes
                and pl.attachment_required_embeddings == checkpoint.attachment_required_embeddings
                and pl.attachment_shape_limits == checkpoint.attachment_shape_limits
                and pl.attachment_port_limits == checkpoint.attachment_port_limits
                and pl.attachment_source_ids == checkpoint.attachment_source_ids
                and pl.attachment_source_rows == checkpoint.attachment_source_rows
                and pl.attachment_source_error_limits == checkpoint.attachment_source_error_limits
                and pl.attachment_source_port_limits == checkpoint.attachment_source_port_limits
                and pl.attachment_source_chain_limits == checkpoint.attachment_source_chain_limits
                and pl.fixed_rows == checkpoint.fixed_rows)
    if safe:
        return pl, dict(version=VERSION, status='kept', reverted=False,
                        scope='frozen_axes_slots_and_fixed_world')
    return deepcopy(checkpoint), dict(version=VERSION, status='restored_checkpoint', reverted=True,
        reason='post_sa_change_violated_attachment_contract', scope='frozen_axes_slots_and_fixed_world')


def _rail_marker_direction_conflicts(pl, i):
    """Describe terminal orders that fold a default vertical rail stub back."""
    from structured_placement import par
    nodes = pl.comps[i]['nodes']
    conflicts = []
    for rail_pin, net in nodes.items():
        symbol = par.power_marker_sym(net)
        if not symbol:
            continue
        rail = pl.pin_world(i, rail_pin)
        sign = -1 if symbol == 'vdd-port' else 1
        for pin, other in nodes.items():
            if pin != rail_pin and not par.power_marker_sym(other):
                signal = pl.pin_world(i, pin)
                if sign*(rail[1]-signal[1]) < -EPS:
                    conflicts.append(dict(device=pl.comps[i]['id'], orientation=pl.orient[i],
                        rail_net=net, rail_pin=rail_pin, rail_point=list(rail),
                        signal_net=other, signal_pin=pin, signal_point=list(signal),
                        marker_direction='north' if sign < 0 else 'south',
                        reason='default_rail_stub_reverses_signal_terminal_order'))
    return conflicts
