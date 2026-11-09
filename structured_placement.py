"""Topology driven, row preserving layout candidates for the final router.

This module deliberately uses only geometric estimates during annealing.  The
caller compares the resulting candidates with the independently validated final
router.  It never edits the source circuit or writes generated artifacts.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import contextlib
import copy
import io
import math
import random
import statistics
import time

import device_leveling as dl
from netlist_core import parse_netlist, to_document
from sa_common import load_local

par = load_local('place_and_route', 'place-and-route.py')
legacy = load_local('place_layered', 'place-layered.py')


def _structures(devices):
    from circuit_structures import analyze_structures
    return analyze_structures(devices)


def _members(record, index):
    return [index[name] for name in record.get('devices', ()) if name in index]


def _active(device):
    return device['cls'] in ('MOS', 'BJT')


def _terminals(device):
    cls = device['cls']
    if cls == 'MOS':
        return (device['d'], device['g'], device['s'])
    if cls == 'BJT':
        return (device['c'], device['b'], device['e'])
    if cls == 'PASSIVE':
        return (device['a'], device['b'])
    if cls in ('VSRC', 'ISRC'):
        return (device['p'], device['n'])
    return (device['op'], device['ip'], device['in'])


def _canonical_keys(devices):
    """Refine connectivity colours without using instance or signal-net names."""
    incidence = defaultdict(list)
    colours = {}
    for name, device in devices.items():
        colours[name] = (device['cls'], device.get('type', device.get('symbol', '')))
        for role, net in enumerate(_terminals(device)):
            incidence[net].append((name, role))
    for _ in range(4):
        descriptions = {}
        for name, device in devices.items():
            neighbors = []
            for net in _terminals(device):
                rail = par.power_marker_sym(net)
                neighbors.append((rail or '', tuple(sorted(
                    (colours[other], role) for other, role in incidence[net]))))
            descriptions[name] = (colours[name], tuple(neighbors))
        palette = {value: i for i, value in enumerate(sorted(set(descriptions.values())))}
        colours = {name: palette[value] for name, value in descriptions.items()}
    return {name: (colours[name], devices[name]['cls'], devices[name].get('type', ''))
            for name in devices}


def _relations(devices, structs, index, row_of):
    """Return alignment relations and disjoint blocks, retaining motif roles."""
    alignment = []
    pairs = []
    parent = list(range(len(index)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(values):
        if values:
            a = find(values[0])
            for b in values[1:]:
                parent[find(b)] = a

    for record in structs:
        values = _members(record, index)
        kind = record['kind']
        roles = record.get('roles', {})
        high = record.get('confidence', 'high') == 'high'
        if high and kind not in ('series_chain', 'parallel_passives', 'feedback_network', 'opamp_core'):
            union(values)
        if kind in ('complementary_branch', 'output_pair'):
            a, b = roles.get('upper'), roles.get('lower')
            if a in index and b in index:
                alignment.append((index[a], index[b], 3.5))
        elif kind == 'latch':
            for side in ('left', 'right'):
                a, b = roles.get(side + '_upper'), roles.get(side + '_lower')
                if a in index and b in index:
                    alignment.append((index[a], index[b], 4.5))
        elif kind == 'folded_core':
            for side in ('left', 'right'):
                names = [roles.get(prefix + '_' + side)
                         for prefix in ('input', 'folded', 'bias')]
                names = [name for name in names if name in index]
                for a, b in zip(names, names[1:]):
                    if row_of[index[a]] != row_of[index[b]]:
                        alignment.append((index[a], index[b], 3.0))
        if kind in ('differential_pair', 'cross_coupled'):
            a, b = roles.get('left'), roles.get('right')
            if a in index and b in index:
                pairs.append((index[a], index[b], kind))
        elif kind == 'current_mirror':
            a = roles.get('reference')
            for b in roles.get('outputs', ()):
                if a in index and b in index:
                    pairs.append((index[a], index[b], kind))

    # Current-path series joins are more valuable than a generic gate affinity.
    by_net = defaultdict(list)
    active_types = {}
    for name, device in devices.items():
        if device['cls'] == 'MOS':
            active_types[index[name]] = device['type']
            by_net[device['d']].append((index[name], 'd'))
            by_net[device['s']].append((index[name], 's'))
        elif device['cls'] == 'BJT':
            active_types[index[name]] = device['type']
            by_net[device['c']].append((index[name], 'd'))
            by_net[device['e']].append((index[name], 's'))
    for net, members in by_net.items():
        if par.power_marker_sym(net):
            continue
        for a, pa in members:
            for b, pb in members:
                opposite_drains = (pa == pb == 'd' and len(members) == 2
                                   and active_types[a] != active_types[b])
                if a < b and (pa != pb or opposite_drains) and row_of[a] != row_of[b]:
                    alignment.append((a, b, 2.5 / max(1, len(members) - 1)))
    blocks = defaultdict(list)
    for i in range(len(index)):
        blocks[find(i)].append(i)
    return alignment, pairs, list(blocks.values())


class StructuredPlacement(legacy.LayeredPlacement):
    """Keep active rows and semantic blocks while proposing collective moves."""

    def set_state(self, cells, orientations):
        self.cell_of = list(cells)
        self.orient = list(orientations)
        self.comp_at = {cell: i for i, cell in enumerate(self.cell_of)}

    def blocks_contiguous(self):
        for block in self.blocks + getattr(self, 'layout_macro_blocks', []):
            by_row = defaultdict(list)
            for i in block:
                by_row[self.row_of[i]].append(self.cell_of[i][1])
            for row, cols in by_row.items():
                lo, hi = min(cols), max(cols)
                if any(self.comp_at.get((row, c)) not in (None, *block)
                       for c in range(lo, hi + 1)):
                    return False
        return True

    def propose(self):
        """Block translation, mirror, branch exchange, or internal refinement."""
        before = (list(self.cell_of), list(self.orient))
        rng = self.rng
        action = rng.random()
        if action < .28:
            block = rng.choice(getattr(self, 'motion_blocks', self.blocks))
            lo = min(self.cell_of[i][1] for i in block)
            hi = max(self.cell_of[i][1] for i in block)
            shift = rng.randint(-lo, self.cols - 1 - hi)
            if not shift:
                return None
            moved = {i: (self.row_of[i], self.cell_of[i][1] + shift) for i in block}
            if any(self.comp_at.get(cell) not in (None, *block) for cell in moved.values()):
                return None
            cells = list(self.cell_of)
            for i, cell in moved.items():
                cells[i] = cell
            self.set_state(cells, self.orient)
        elif action < .46:
            block = rng.choice(getattr(self, 'motion_blocks', self.blocks))
            lo = min(self.cell_of[i][1] for i in block)
            hi = max(self.cell_of[i][1] for i in block)
            cells = list(self.cell_of)
            for i in block:
                cells[i] = (self.row_of[i], lo + hi - self.cell_of[i][1])
            if len(set(cells)) != len(cells):
                return None
            orient = list(self.orient)
            for i in block:
                if self.comps[i]['symbol'] in legacy.MIRROR_ORIENTS:
                    orient[i] = 1 - orient[i]
            self.set_state(cells, orient)
        elif action < .66 and len(self.blocks) > 1:
            # Equal row signatures permit an actual multirow block swap.
            a, b = rng.sample(self.blocks, 2)
            rows_a = Counter(self.row_of[i] for i in a)
            rows_b = Counter(self.row_of[i] for i in b)
            if rows_a != rows_b:
                return None
            delta = min(self.cell_of[i][1] for i in b) - min(self.cell_of[i][1] for i in a)
            cells = list(self.cell_of)
            for block, shift in ((a, delta), (b, -delta)):
                for i in block:
                    cells[i] = (self.row_of[i], self.cell_of[i][1] + shift)
            if any(not 0 <= c < self.cols for _, c in cells) or len(set(cells)) != len(cells):
                return None
            self.set_state(cells, self.orient)
        else:
            i = rng.randrange(len(self.comps))
            if rng.random() < .40:
                alternatives = [v for v in self.allowed_orientations(i) if v != self.orient[i]]
                if not alternatives:
                    return None
                self.orient[i] = rng.choice(alternatives)
            else:
                row, col = self.cell_of[i]
                next_col = rng.randrange(self.cols)
                if col == next_col:
                    return None
                other = self.comp_at.get((row, next_col))
                if other is not None and self.block_of[i] != self.block_of[other]:
                    return None
                self.cell_of[i] = (row, next_col)
                if other is not None:
                    self.cell_of[other] = (row, col)
                self.comp_at = {cell: j for j, cell in enumerate(self.cell_of)}
        if getattr(self, 'layout_relations', None) and any(
                self.cell_of[i] != before[0][i] or self.orient[i] != before[1][i]
                for i in set(getattr(self, 'fixed', ())) | set(getattr(self, 'fixed_cells', {}))):
            self.set_state(*before)
            return None
        if not self.blocks_contiguous():
            self.set_state(*before)
            return None
        return before


def _opamp_seed(devices, structs, index, row_of, variant):
    """Feedback is a local loop around the actual upper/lower input port."""
    cells = {}
    orientations = {}
    cores = [s for s in structs if s['kind'] == 'opamp_core']
    if not cores:
        return None
    width = 0
    for core_no, core in enumerate(cores):
        amplifier = core.get('roles', {}).get('amplifier')
        if amplifier not in index:
            continue
        feedbacks = [s for s in structs if s['kind'] == 'feedback_network'
                     and s.get('roles', {}).get('amplifier') == amplifier]
        longest = max((len(s.get('roles', {}).get('series', ())) for s in feedbacks), default=1)
        right = max(2, longest)
        offset = core_no * (right + 3)
        cells[index[amplifier]] = (2, offset + right - 1)
        orientations[index[amplifier]] = 0
        for feedback in feedbacks:
            roles = feedback.get('roles', {})
            positive = roles.get('input') == 'plus'
            row = 3 if positive else 1
            series = [name for name in roles.get('series', ()) if name in index]
            for step, name in enumerate(series):
                # OUT is on the right; the ordered chain travels back left.
                cells[index[name]] = (row, offset + right - step)
                orientations[index[name]] = 1
            end_col = offset + max(0, right - len(series))
            shunts = [name for name in roles.get('shunts', ()) if name in index]
            for step, name in enumerate(shunts):
                cells[index[name]] = (4 if positive else 0, end_col + step)
                orientations[index[name]] = 0
        width = max(width, offset + right + 2)
    if not cells:
        return None
    occupied = set(cells.values())
    for name in devices:
        i = index[name]
        if i not in cells:
            cell = next((r, c) for r in range(5) for c in range(max(width, 3))
                        if (r, c) not in occupied)
            cells[i] = cell
            occupied.add(cell)
    for i, (row, _) in cells.items():
        row_of[i] = row
    return cells, orientations, max(c for _, c in cells.values()) + 2


def _passive_profile(pl, devices, nets, index, structs, variant, policy):
    if policy == 'fixed' or not any(d['cls'] == 'PASSIVE' for d in devices.values()):
        return pl
    from passive_placement import make_flexible
    column_pitch, row_pitch = ((110, 112), (124, 126), (100, 106))[variant]
    return make_flexible(pl, devices, nets, index, structs,
                         column_pitch=column_pitch, row_pitch=row_pitch)


def _initial_placement(devices, comps, nets, index, levels, att, structs, seed, variant,
                       *, passive_layout='fixed', row_override=None, bridge_alignment=()):
    if passive_layout not in ('flexible', 'fixed'):
        raise ValueError('passive_layout must be flexible or fixed')
    keys = _canonical_keys(devices)
    row_of, _ = legacy._allocate_rows(devices, comps, index, levels['net_levels'],
                                      levels['device_levels'], att)
    if row_override is not None:
        row_of = list(row_override)
    opamp = None
    if not levels['device_levels']:
        opamp = _opamp_seed(devices, structs, index, row_of, variant)
        if opamp is None:
            # A passive graph has no current hierarchy: keep it locally compact.
            ordered = sorted(devices, key=lambda name: keys[name])
            row_of = [1] * len(comps)
            for n, name in enumerate(ordered):
                row_of[index[name]] = n // max(2, math.ceil(math.sqrt(len(comps))))
    alignment, pairs, blocks = _relations(devices, structs, index, row_of)
    for bridge in bridge_alignment:
        names = [bridge.get('upper'), *bridge.get('members', ()), bridge.get('lower')]
        names = [name for name in names if name in index]
        alignment.extend((index[a], index[b], 2.5) for a, b in zip(names, names[1:]))
    counts = Counter(row_of)
    cols = max(counts.values()) + (2 if variant != 2 else 3)
    if opamp:
        cols = max(cols, opamp[2])
    pl = StructuredPlacement(comps, random.Random(seed), row_of, cols=cols)
    pl.blocks = blocks
    pl.block_of = {i: n for n, block in enumerate(blocks) for i in block}
    pl.role_orders = []
    for record in structs:
        roles = record.get('roles', {})
        if record['kind'] == 'folded_core':
            ordered = [roles.get(key) for key in
                       ('input_left', 'folded_left', 'folded_right', 'input_right')]
            if all(name in index for name in ordered):
                pl.role_orders.append([[index[name] for name in ordered]])
        elif record['kind'] == 'latch':
            rows = [[roles.get(a), roles.get(b)] for a, b in
                    (('left_upper', 'right_upper'), ('left_lower', 'right_lower'))]
            if all(name in index for row in rows for name in row):
                pl.role_orders.append([[index[name] for name in row] for row in rows])
    # Physical row pitch leaves both symbol halos and a real routing channel.
    column_pitch, row_pitch = ((110, 112), (124, 126), (100, 106))[variant]
    pl.column_x = [64 + c * column_pitch for c in range(cols)]
    pl.row_y = [64 + r * row_pitch for r in range(max(row_of) + 1)]
    if opamp:
        # The amplifier is wider than a MOS.  Reserve extra width near its column.
        amp_cols = {opamp[0][index[s['roles']['amplifier']]][1]
                    for s in structs if s['kind'] == 'opamp_core'
                    and s.get('roles', {}).get('amplifier') in index}
        for c in range(1, cols):
            extra = 18 if c in amp_cols or c - 1 in amp_cols else 0
            pl.column_x[c] = pl.column_x[c - 1] + column_pitch + extra
        cells = [opamp[0][i] for i in range(len(comps))]
        orientations = [opamp[1].get(i, 0) for i in range(len(comps))]
        pl.set_state(cells, orientations)
        return _passive_profile(pl, devices, nets, index, structs, variant, passive_layout), alignment, pairs

    # Seed branch coordinates from retained roles before the barycentric sweep.
    desired = {}
    priority = {'latch': 4, 'folded_core': 4, 'output_pair': 3,
                'complementary_branch': 2, 'differential_pair': 1, 'current_mirror': 1}
    rank_priority = {}
    for record in sorted(structs, key=lambda s: priority.get(s['kind'], 0)):
        roles, kind = record.get('roles', {}), record['kind']
        assignments = []
        if kind == 'latch':
            assignments = [(roles.get('left_upper'), -1), (roles.get('left_lower'), -1),
                           (roles.get('right_upper'), 1), (roles.get('right_lower'), 1)]
        elif kind == 'folded_core':
            assignments = [(roles.get('input_left'), -2), (roles.get('folded_left'), -1),
                           (roles.get('bias_left'), -1), (roles.get('input_right'), 2),
                           (roles.get('folded_right'), 1), (roles.get('bias_right'), 1)]
        elif kind in ('complementary_branch', 'output_pair'):
            assignments = [(roles.get('upper'), 0), (roles.get('lower'), 0)]
        elif kind in ('differential_pair', 'cross_coupled'):
            assignments = [(roles.get('left'), -1), (roles.get('right'), 1)]
        elif kind == 'current_mirror':
            assignments = [(roles.get('reference'), -.5)] + [
                (name, .5 + n) for n, name in enumerate(roles.get('outputs', ()))]
        for name, value in assignments:
            if name in index:
                desired[index[name]] = value
                rank_priority[index[name]] = priority.get(kind, 0)

    affinity = defaultdict(list)
    for net, members in nets.items():
        if par.power_marker_sym(net):
            continue
        ids = sorted({index[name] for name, _ in members})
        for a in ids:
            for b in ids:
                if a != b:
                    affinity[a].append((b, 1 / max(1, len(ids) - 1)))
    for a, b, weight in alignment:
        affinity[a].append((b, 4 * weight))
        affinity[b].append((a, 4 * weight))
    by_row = defaultdict(list)
    for i, row in enumerate(row_of):
        by_row[row].append(i)
    continuous = {}
    block_order = sorted(range(len(blocks)), key=lambda n: min(keys[comps[i]['id']] for i in blocks[n]))
    if variant == 1:
        block_order.reverse()
    block_rank = {block: n for n, block in enumerate(block_order)}
    for i in range(len(comps)):
        continuous[i] = block_rank[pl.block_of[i]] * 2 + desired.get(i, 0)
    for sweep in range(8):
        rows = sorted(by_row, reverse=bool(sweep % 2))
        for row in rows:
            row_blocks = defaultdict(list)
            for i in by_row[row]:
                row_blocks[pl.block_of[i]].append(i)
            barycenters = {}
            for owner, values in row_blocks.items():
                weighted = [(continuous[b], w) for a in values for b, w in affinity[a]
                            if row_of[b] != row]
                barycenters[owner] = (sum(x * w for x, w in weighted) / sum(w for _, w in weighted)
                                      if weighted else sum(continuous[a] for a in values) / len(values))
            ordered_blocks = sorted(row_blocks, key=lambda b: (barycenters[b], block_rank[b]))
            ordered = []
            for block in ordered_blocks:
                values = row_blocks[block]
                values.sort(key=lambda a: (desired.get(a, 0), continuous[a], keys[comps[a]['id']]))
                ordered.extend(values)
            offset = (cols - len(ordered)) // 2
            for col, i in enumerate(ordered, offset):
                continuous[i] = col
    cells = [(row_of[i], round(continuous[i])) for i in range(len(comps))]
    pl.set_state(cells, [0] * len(comps))
    # Gate side points toward the other terminals of its own control net.
    for i, comp in enumerate(comps):
        if comp['symbol'] in legacy.MIRROR_ORIENTS:
            gate = 'G' if 'G' in comp['nodes'] else 'B'
            members = nets.get(comp['nodes'].get(gate), ())
            others = [index[name] for name, _ in members if name != comp['id']]
            if others:
                target = sum(pl.cell_of[j][1] for j in others) / len(others)
                pl.orient[i] = int(target > pl.cell_of[i][1])
        elif comp['symbol'] in legacy.FLIP_ORIENTS:
            pl.orient[i] = 0
    return _passive_profile(pl, devices, nets, index, structs, variant, passive_layout), alignment, pairs


def _estimate(pl, nets, index, alignment, pairs):
    """A cheap independent objective, with all costs in units of 100 px."""
    wirelength = 0.0
    direction = 0.0
    bends = 0
    boxes = []
    connections = []
    for net, members in nets.items():
        if par.power_marker_sym(net) or len(members) < 2:
            continue
        pins = [pl.pin_world(index[name], pin) for name, pin in members]
        xs, ys = zip(*pins)
        span = max(xs) - min(xs) + max(ys) - min(ys)
        wirelength += span
        bends += max(0, len(set(xs)) - 1) + max(0, len(set(ys)) - 1)
        boxes.append((net, min(xs), min(ys), max(xs), max(ys)))
        # This penalizes a gate facing away, without prescribing its orientation.
        for (name, pin), (x, y) in zip(members, pins):
            if pin in ('G', 'B', 'IN+', 'IN-', 'OUT'):
                others = [p for p in pins if p != (x, y)]
                if not others:
                    continue
                target = statistics.mean(p[0] for p in others)
                dx = {'east': 1, 'west': -1}.get(pl.pin_dir(index[name], pin), 0)
                direction += max(0, -dx * (target - x)) * .25
        # Between-layer inversions approximate crossings before routing.
        ids = sorted({index[name] for name, _ in members})
        for a in ids:
            above = [b for b in ids if pl.row_of[b] > pl.row_of[a]]
            if above:
                b = min(above, key=lambda b: (pl.row_of[b] - pl.row_of[a],
                                               abs(pl.cell_of[b][1] - pl.cell_of[a][1])))
                connections.append((net, a, b))
    crossings = 0
    for n, (net, a, b) in enumerate(connections):
        ra, ca = pl.cell_of[a]
        rb, cb = pl.cell_of[b]
        for other, c, d in connections[n + 1:]:
            if net == other or len({a, b, c, d}) < 4:
                continue
            rc, cc = pl.cell_of[c]
            rd, cd = pl.cell_of[d]
            if ra == rc and rb == rd and (ca - cc) * (cb - cd) < 0:
                crossings += 1
    misalignment = sum(weight * abs(pl.cell_of[a][1] - pl.cell_of[b][1])
                       for a, b, weight in alignment)
    dispersion = 0.0
    for block in pl.blocks:
        by_row = defaultdict(list)
        for i in block:
            by_row[pl.row_of[i]].append(pl.cell_of[i][1])
        for cols in by_row.values():
            dispersion += max(cols) - min(cols) + 1 - len(cols)
    pair_spread = sum(max(0, abs(pl.cell_of[a][1] - pl.cell_of[b][1]) -
                            (3 if kind == 'differential_pair' else 2))
                      for a, b, kind in pairs)
    congestion = 0.0
    for n, (_, x0, y0, x1, y1) in enumerate(boxes):
        for _, a0, b0, a1, b1 in boxes[n + 1:]:
            overlap_x = max(0, min(x1, a1) - max(x0, a0))
            overlap_y = max(0, min(y1, b1) - max(y0, b0))
            congestion += overlap_x * overlap_y / 10000
    role_inversions = 0
    for template in pl.role_orders:
        normal = reverse = 0
        for order in template:
            for n, a in enumerate(order):
                for b in order[n + 1:]:
                    ca, cb = pl.cell_of[a][1], pl.cell_of[b][1]
                    normal += ca > cb
                    reverse += ca < cb
        role_inversions += min(normal, reverse)
    value = (wirelength + direction + 150 * crossings + 15 * bends
             + 100 * misalignment + 65 * dispersion + 100 * pair_spread
             + 125 * role_inversions + 8 * congestion) / 100
    metrics = {'estimated_wirelength': wirelength, 'estimated_crossings': crossings,
                   'estimated_bends': bends, 'branch_misalignment': misalignment,
                   'structure_dispersion': dispersion, 'pair_spread': pair_spread,
                   'role_order_inversions': role_inversions,
                   'estimated_congestion': congestion, 'gate_backtracking': direction,
                   'normalized_energy': value}
    if getattr(pl, 'passive_policy', None) == 'flexible':
        from passive_placement import score_passives
        regularity = score_passives(pl)
        value += regularity['weighted_total']
        metrics.update(passive_regularization=regularity, normalized_energy=value)
    if getattr(pl, 'layout_relations', None):
        from layout_relations import measure_layout_relations
        regularity = measure_layout_relations(pl, pl.comps, index, pl.layout_relations)
        value += (regularity['interleaving'] + 1.25 * regularity['role_order']
                  + .15 * regularity['normalized_span'])
        metrics.update(structure_regularization=regularity, normalized_energy=value)
    if hasattr(pl, 'attachment_model'):
        from attachment_placement import score_attachments
        attachments = score_attachments(pl)
        value += attachments['weighted_total']
        metrics.update(attachment_regularization=attachments, normalized_energy=value)
    if getattr(pl, 'branch_connections', None):
        costs=[]
        for bridge in pl.branch_connections:
            names=[bridge.get('upper'), *bridge.get('members',()), bridge.get('lower')]
            ys=[pl.pos_of(index[name])[1] for name in names if name in index]
            costs.append(sum(max(0,a-b) for a,b in zip(ys,ys[1:]))/100.)
        backtracking=sum(costs)/max(1,len(costs))
        value+=backtracking
        metrics.update(branch_backtracking=backtracking,normalized_energy=value)
    return value, metrics


def _calibrated_temperature(deltas, target=.7):
    """Solve the sampled uphill acceptance instead of using a fixed temperature."""
    positive = [delta for delta in deltas if delta > 1e-9]
    if not positive:
        return 1.0
    lo, hi = 1e-9, max(positive) * 100
    for _ in range(45):
        middle = (lo + hi) / 2
        acceptance = sum(math.exp(-delta / middle) for delta in positive) / len(positive)
        if acceptance < target:
            lo = middle
        else:
            hi = middle
    return (lo + hi) / 2


def _enforce_geometry_floor(pl, halo=12, channel_tracks=2, track_spacing=10):
    """Expand compact pitches only where actual oriented symbols need room.

    Each neighboring row/column reserves both body extents, the two halos and
    two routing tracks.  Empty logical rows/columns keep their base pitch.
    """
    extents_x, extents_y = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    for i, comp in enumerate(pl.comps):
        row, col = pl.cell_of[i]
        bbox = par.GEO_DOC[comp['symbol']]['orientations'][pl.orient[i]]['viewBoxAABB']
        left, right = max(0, -bbox['x']), max(0, bbox['x'] + bbox['width'])
        top, bottom = max(0, -bbox['y']), max(0, bbox['y'] + bbox['height'])
        extents_x[col][0] = max(extents_x[col][0], left)
        extents_x[col][1] = max(extents_x[col][1], right)
        extents_y[row][0] = max(extents_y[row][0], top)
        extents_y[row][1] = max(extents_y[row][1], bottom)
    clearance = 2 * halo + channel_tracks * track_spacing
    for axis, extents in ((pl.column_x, extents_x), (pl.row_y, extents_y)):
        for previous in range(len(axis) - 1):
            if previous not in extents or previous + 1 not in extents:
                continue
            floor = extents[previous][1] + extents[previous + 1][0] + clearance
            floor = math.ceil(floor / 2) * 2
            missing = max(0, axis[previous] + floor - axis[previous + 1])
            if missing:
                for later in range(previous + 1, len(axis)):
                    axis[later] += missing


def _orient_latch_clock_access(pl, devices, structs, index):
    """Expose a clocked latch tail toward its reset/precharge clock bank.

    The shared differential source and the two latch lower sources establish
    the hierarchy.  Its tail control then matches the top reset/precharge
    controls.  A generic net centroid also contains drain terminals and can
    otherwise favor the opposite mirror merely to save a few estimated pixels.
    """
    adjustments = []
    for latch in (row for row in structs if row['kind'] == 'latch'):
        roles = latch.get('roles', {})
        lowers = [roles.get(side + '_lower') for side in ('left', 'right')]
        if not all(name in devices and devices[name]['cls'] == 'MOS' for name in lowers):
            continue
        internal = {devices[name]['s'] for name in lowers}
        for dp in (row for row in structs if row['kind'] == 'differential_pair'):
            pair = [name for name in dp['devices'] if name in devices]
            if (len(pair) != 2 or any(devices[name]['cls'] != 'MOS' for name in pair)
                    or {devices[name]['d'] for name in pair} != internal):
                continue
            for name in dp.get('roles', {}).get('tail', ()):
                if name not in index or devices[name]['cls'] != 'MOS':
                    continue
                tail = index[name]
                clock = devices[name]['g']
                bank = [index[other] for other, dev in devices.items()
                        if other != name and dev['cls'] == 'MOS' and dev['g'] == clock
                        and pl.cell_of[index[other]][0] < pl.cell_of[tail][0]
                        and other not in latch['devices']]
                if len(bank) < 2:
                    continue
                target = statistics.median(pl.pin_world(i, 'G')[0] for i in bank)
                center = pl.pos_of(tail)[0]
                if target == center:
                    continue
                before = pl.orient[tail]
                after = int(target > center)
                pl.orient[tail] = after
                adjustments.append({'kind': 'latch_clock_access', 'device': name,
                                    'clock': clock, 'clock_bank': [pl.comps[i]['id'] for i in bank],
                                    'orientation_before': before, 'orientation_after': after,
                                    'clock_bank_median_x': target})
    return adjustments


def _anneal(pl, nets, index, alignment, pairs, iterations, deadline):
    evaluate = lambda: _estimate(pl, nets, index, alignment, pairs)
    current, _ = evaluate()
    deltas = []
    for _ in range(64):
        if deadline is not None and time.perf_counter() >= deadline:
            break
        old = pl.propose()
        if old is not None:
            candidate, _ = evaluate()
            deltas.append(candidate - current)
            pl.set_state(*old)
    initial = _calibrated_temperature(deltas)
    best = current
    state = (list(pl.cell_of), list(pl.orient))
    accepted = proposed = uphill_accepted = uphill = reheats = completed = 0
    last_improvement = 0
    stop = 'iterations'
    for step in range(iterations):
        if deadline is not None and step % 32 == 0 and time.perf_counter() >= deadline:
            stop = 'quality_budget'
            break
        completed = step + 1
        fraction = step / max(1, iterations - 1)
        temperature = initial * (.015 ** fraction)
        if step - last_improvement > max(300, iterations // 4) and reheats < 2:
            reheats += 1
            last_improvement = step
            pl.set_state(*state)
            current = best
        if reheats and step - last_improvement < 100:
            temperature = max(temperature, initial * .25)
        old = pl.propose()
        if old is None:
            continue
        proposed += 1
        candidate, _ = evaluate()
        delta = candidate - current
        uphill += delta > 0
        if delta <= 0 or pl.rng.random() < math.exp(-delta / max(temperature, 1e-9)):
            accepted += 1
            uphill_accepted += delta > 0
            current = candidate
            if candidate < best:
                best = candidate
                state = (list(pl.cell_of), list(pl.orient))
                last_improvement = step
        else:
            pl.set_state(*old)
    pl.set_state(*state)
    _, metrics = evaluate()
    return best, metrics, {'initial_temperature': initial, 'target_initial_uphill_acceptance': .7,
                           'sampled_positive_deltas': sum(d > 0 for d in deltas),
                           'steps_completed': completed, 'steps_budget': iterations,
                           'proposed': proposed, 'accepted': accepted,
                           'acceptance_rate': accepted / max(1, proposed),
                           'uphill_acceptance_rate': uphill_accepted / max(1, uphill),
                           'reheats': reheats, 'stop_reason': stop}


def feedback_candidate(placement, comps, nets, index, metadata, quality):
    """Open a local row channel for a measured detouring multi-terminal net.

    Return ``None`` when no such net spans a row boundary.  Otherwise return a
    separately owned five-tuple, preserving every logical cell and orientation.
    The caller still has to route and validate this proposal, and retains its
    incumbent if the additional space does not improve the measured result.
    """
    per_net = quality.get('details', quality).get('per_net', {})
    candidates = []
    for net, metrics in per_net.items():
        factor = metrics.get('detour_factor')
        if (factor is None or factor <= 1.5 or metrics.get('terminals', 0) < 3
                or par.power_marker_sym(net)):
            continue
        ids = sorted({index[name] for name, _ in nets.get(net, ()) if name in index})
        if len(ids) < 2:
            continue
        rows = [placement.cell_of[i][0] for i in ids]
        if min(rows) == max(rows):
            continue
        excess = metrics.get('excess_detour', metrics.get('wirelength', 0))
        candidates.append((excess, factor, net, rows))
    if not candidates:
        return None
    _, factor, net, terminal_rows = max(candidates, key=lambda row: (row[0], row[1], row[2]))
    saved_grid = metadata.get('layout_grid', {})
    row_count = max(row for row, _ in placement.cell_of) + 1
    row_y = list(getattr(placement, 'row_y', saved_grid.get('row_y', ())))
    if not row_y:
        row_y = [par.CELL * row + par.CELL / 2 for row in range(row_count)]
    cols = max(col for _, col in placement.cell_of) + 1
    column_x = list(getattr(placement, 'column_x', saved_grid.get('column_x', ())))
    if not column_x:
        column_x = [par.CELL * col + par.CELL / 2 for col in range(cols)]
    # A narrow cut separating many terminals is the most useful place for one
    # additional set of horizontal tracks.  No device changes its logical layer.
    def cut_pressure(cut):
        above = sum(row <= cut for row in terminal_rows)
        below = len(terminal_rows) - above
        return above * below / max(1, row_y[cut + 1] - row_y[cut])

    cut = max(range(min(terminal_rows), max(terminal_rows)),
              key=lambda cut: (cut_pressure(cut), -cut))
    clone = copy.deepcopy(placement)
    clone.column_x = column_x
    clone.row_y = [value + (32 if row > cut else 0) for row, value in enumerate(row_y)]
    result_metadata = copy.deepcopy(metadata)
    origin = str(metadata.get('candidate_id', 'unknown'))
    result_metadata.update(candidate_id='feedback', layout_style='structured',
                           layout_grid={'column_x': list(clone.column_x),
                                        'row_y': list(clone.row_y)},
                           feedback_from_net=net, feedback_row_cut=cut,
                           feedback={'source_candidate': origin, 'net': net, 'row_cut': cut,
                                     'added_channel_height': 32,
                                     'source_detour_factor': factor,
                                     'action': 'expand_local_row_channel'})
    if hasattr(clone, 'blocks'):
        alignment = [(index[row['devices'][0]], index[row['devices'][1]], row['weight'])
                     for row in metadata.get('alignment_relations', ())]
        energy, estimated = _estimate(clone, nets, index, alignment, [])
        result_metadata.update(energy=energy, optimization_metrics=estimated)
    else:
        result_metadata['optimization_metrics'] = {
            'scope': 'pre_feedback_legacy_estimate',
            'source': copy.deepcopy(metadata.get('optimization_metrics', {}))}
    if getattr(clone, 'passive_policy', None) == 'flexible':
        from passive_placement import passive_diagnostics
        result_metadata['passive_layout'] = passive_diagnostics(clone)
        result_metadata['passive_layout']['metrics_stage'] = 'after_feedback_axis_change'
    return clone, comps, nets, index, result_metadata


def create_candidates(path, *, cell=None, iterations=20000, seed=20261029, deadline=None,
                      passive_layout='flexible', include_legacy=False,
                      structure_layout='off', structure_phase=3):
    """Yield three structured seeds, optionally preceded by legacy candidate 0.

    Structured seeds retain IDs 1--3, independent random streams and their
    existing proposal budgets. Legacy parsing/SA runs only when explicitly
    requested; structured initialization uses canonical source data directly.
    A suspended generator never mutates any previously yielded placement.
    """
    if iterations < 0:
        raise ValueError('iterations must be non-negative')
    if passive_layout not in ('flexible', 'fixed'):
        raise ValueError('passive_layout must be flexible or fixed')
    if type(include_legacy) is not bool:
        raise ValueError('include_legacy must be boolean')
    if structure_layout not in ('off', 'auto'):
        raise ValueError('structure_layout must be off or auto')
    if type(structure_phase) is not int or structure_phase not in (1, 2, 3):
        raise ValueError('structure_phase must be 1, 2 or 3')
    if deadline is not None and time.perf_counter() >= deadline:
        raise ValueError('Quality budget exhausted before placement initialization')
    devices = parse_netlist(path, cell=cell)
    structs = _structures(devices)
    document = to_document(devices)
    comps, nets = list(document['components']), dict(document['nets'])
    if not comps:
        raise ValueError('The selected netlist contains no supported devices')
    index = {component['id']: number for number, component in enumerate(comps)}
    if include_legacy:
        # Keep the explicit reference identical to the old four-candidate flow.
        # Its old VF2 diagnostic includes deliberately superseded broad matches.
        with contextlib.redirect_stdout(io.StringIO()):
            base = legacy.create_placement(path, cell=cell, iterations=iterations, seed=seed,
                                           deadline=deadline)
        pl, comps, nets, index, metadata = base
        metadata['legacy_structs'] = metadata['structs']
        metadata['structs'] = copy.deepcopy(structs)
        metadata['candidate_id'] = 0
        metadata['candidate_seed'] = seed
        metadata['layout_style'] = 'legacy_reference'
        metadata['passive_layout_policy'] = 'fixed'
        metadata['passive_layout'] = {'version': 'fixed-rows-v1', 'policy': 'fixed',
                                      'reason': 'legacy_reference'}
        metadata['layout_grid'] = {'column_x': [par.CELL * c + par.CELL / 2 for c in range(pl.cols)],
                                   'row_y': [par.CELL * r + par.CELL / 2 for r in range(max(pl.row_of) + 1)]}
        metadata['annealing'] = {'schedule': 'legacy', 'proposed_budget': iterations,
                                  'completed_steps': getattr(pl, 'sa_iterations_completed', iterations),
                                  'stop_reason': getattr(pl, 'sa_stop_reason', 'iterations')}
        yield pl, comps, nets, index, metadata
    levels = dl.compute_levels(devices)
    conduction = {net for device in devices.values() if _active(device)
                  for net in (device.get('d'), device.get('s'), device.get('c'), device.get('e'))
                  if net is not None}
    att = legacy.attach_info(devices, levels['net_levels'], conduction)
    evidence = evidence_relations = None
    if structure_layout == 'auto':
        from layout_intent import build_intent
        evidence, evidence_relations = build_intent(
            path, cell, devices, structs, comps, nets, index, structure_phase)
    for variant in range(3):
        if deadline is not None and time.perf_counter() >= deadline:
            return
        candidate_seed = seed + (variant + 1) * 104729
        layout_relations = row_plan = None
        candidate_structs = structs
        if structure_layout == 'auto' and variant != 0:
            layout_relations = copy.deepcopy(evidence_relations)
            candidate_structs = evidence['structures']
            if structure_phase >= 2:
                from branch_levels import plan_branch_rows
                original_rows, _ = legacy._allocate_rows(devices, comps, index,
                    levels['net_levels'], levels['device_levels'], att)
                _, row_plan = plan_branch_rows(devices, levels, original_rows,
                                               layout_relations, comps, index)
        placement, alignment, pairs = _initial_placement(
            devices, comps, nets, index, levels, att, candidate_structs, candidate_seed, variant,
            passive_layout=passive_layout,
            row_override=[row_plan['rows'][c['id']] for c in comps] if row_plan else None,
            bridge_alignment=row_plan.get('bridge_alignment', ()) if row_plan else ())
        if layout_relations is not None:
            from layout_relations import prepare_structure_placement
            prepare_structure_placement(placement, comps, index, layout_relations)
            # Existing weights/actions consume additional validated hints; the
            # baseline initializer and its random stream remain untouched.
            known = {frozenset((a, b)) for a, b, _ in alignment}
            known_pairs = {frozenset((a, b)) for a, b, _ in pairs}
            for relation in layout_relations:
                for branch in relation.get('layout', {}).get('branches', ()):
                    for a, b in zip(branch, branch[1:]):
                        if a in index and b in index:
                            key = frozenset((index[a], index[b]))
                            if key not in known and placement.row_of[index[a]] != placement.row_of[index[b]]:
                                alignment.append((index[a], index[b], 3.0))
                                known.add(key)
                for pair in relation.get('layout', {}).get('pairs', ()):
                    if len(pair) == 2 and all(name in index for name in pair):
                        a, b = (index[name] for name in pair)
                        if frozenset((a, b)) not in known_pairs:
                            pairs.append((a, b, relation['kind']))
                            known_pairs.add(frozenset((a, b)))
        if row_plan is not None:
            placement.branch_connections=copy.deepcopy(row_plan.get('bridge_alignment',[]))
        attachment_report = None
        if layout_relations is not None and structure_phase >= 3:
            from attachment_placement import prepare_attachments
            placement, attachment_report = prepare_attachments(
                placement, devices, comps, nets, index, layout_relations)
        # Leave most of the global budget to final routing, which is authoritative.
        local_deadline = min(deadline, time.perf_counter() + 12) if deadline else None
        energy, metrics, annealing = _anneal(placement, nets, index, alignment, pairs,
                                             iterations // 3, local_deadline)
        role_adjustments = _orient_latch_clock_access(placement, devices, candidate_structs, index)
        _enforce_geometry_floor(placement)
        energy, metrics = _estimate(placement, nets, index, alignment, pairs)
        metadata = {'source_note': levels['source_note'], 'levels': levels['device_levels'],
                    'attach': copy.deepcopy(att), 'structs': copy.deepcopy(structs),
                    'sym_pairs': [(comps[a]['id'], comps[b]['id']) for a, b, _ in pairs],
                    'energy': energy, 'optimization_metrics': metrics,
                    'layout_grid': {'column_x': list(placement.column_x),
                                    'row_y': list(placement.row_y)},
                    'candidate_id': variant + 1, 'candidate_seed': candidate_seed,
                    'layout_style': 'structured', 'annealing': annealing,
                    'role_adjustments': role_adjustments,
                    'alignment_relations': [{'devices': [comps[a]['id'], comps[b]['id']],
                                              'weight': weight}
                                             for a, b, weight in alignment]}
        if layout_relations is not None:
            from layout_relations import measure_layout_relations, VERSION as STRUCTURE_VERSION
            metadata['layout_relations'] = copy.deepcopy(layout_relations)
            metadata['structure_layout'] = {'version': STRUCTURE_VERSION, 'policy': 'auto',
                                            'phase': structure_phase, 'metrics': measure_layout_relations(
                                                placement, comps, index, layout_relations),
                                            'metrics_stage': 'after_sa_and_geometry_floor'}
        if evidence is not None and variant:
            metadata['layout_evidence'] = copy.deepcopy(evidence)
            metadata['layout_evidence']['consumption'] = {
                'policy': 'candidate_layout' if variant else 'report_only',
                'candidate_id': variant + 1,
                'stages': ([{'stage': 'initial_blocks', 'status': 'applied',
                             'relation_ids': [row['id'] for row in layout_relations
                                              if row.get('rigid') or row.get('macro')]}]
                           if variant else [])}
            if variant and row_plan is not None:
                metadata['layout_evidence']['consumption']['stages'].append({
                    'stage': 'row_plan', 'status': 'applied',
                    'relation_ids': sorted({row['relation'] for row in row_plan.get('same_row', ())
                                            if row.get('relation')})})
            if variant and attachment_report is not None:
                metadata['layout_evidence']['consumption']['stages'].append({
                    'stage': 'attachments', 'status': 'applied',
                    'relation_ids': [row['id'] for row in layout_relations
                                     if row['kind'] in ('port_attachment', 'series_chain',
                                                        'parallel_passives', 'feedback_network')],
                    'position_changes': copy.deepcopy(attachment_report.get('actions', []))})
        if row_plan is not None:
            metadata['branch_row_plan'] = copy.deepcopy(row_plan)
        if attachment_report is not None:
            metadata['attachment_placement'] = copy.deepcopy(attachment_report)
        metadata['passive_layout_policy'] = getattr(placement, 'passive_policy', 'fixed')
        if metadata['passive_layout_policy'] == 'flexible':
            from passive_placement import passive_diagnostics
            metadata['passive_layout'] = passive_diagnostics(placement)
            metadata['passive_layout']['metrics_stage'] = 'after_sa_and_geometry_floor'
        else:
            metadata['passive_layout'] = {'version': 'fixed-rows-v1', 'policy': 'fixed',
                                          'reason': 'requested_fixed' if passive_layout == 'fixed'
                                          else 'no_passive_devices'}
        yield placement, comps, nets, index, metadata
