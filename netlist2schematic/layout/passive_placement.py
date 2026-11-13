"""Local two-dimensional passive moves with the existing active constraints.

Only ``cell_of`` and ``orient`` describe mutable placement. Current rows and
occupancy are rebuilt on every restore, including SA calibration and reheats.
Regularity terms are inexpensive SA estimates, never routing or validation.
"""
from __future__ import annotations

from copy import deepcopy
from math import isfinite
from statistics import fmean

from netlist2schematic.layout.structured_placement import StructuredPlacement, par

VERSION = 'flexible-passives-v1'
PASSIVE_KINDS = frozenset(('R', 'C', 'L', 'D'))
DISTANCE_UNIT = 100.0
WEIGHTS = {'series_adjacency': .5, 'parallel_alignment': 1.0,
           'feedback_locality': .25}
NEIGHBOR_OFFSETS = tuple((dr, dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1)
                         if (dr, dc) != (0, 0))


def _axis(values, extra, fallback):
    result = list(values)
    if (not result or any(type(v) not in (int, float) or not isfinite(v) for v in result)
            or any(a >= b for a, b in zip(result, result[1:]))):
        raise ValueError('Passive layout requires finite increasing physical axes')
    pitch = result[-1] - result[-2] if len(result) > 1 else fallback
    if type(pitch) not in (int, float) or not isfinite(pitch) or pitch <= 0:
        raise ValueError('Passive layout requires a positive finite fallback pitch')
    for _ in range(extra):
        following = result[-1] + pitch
        if not isfinite(following) or following <= result[-1]:
            raise ValueError('Expanded passive layout axis is invalid')
        result.append(following)
    return result


def _pins(pl, i, net):
    return tuple(sorted(pin for pin, name in pl.comps[i].get('nodes', {}).items()
                        if name == net))


def _relations(pl, structs, index, passive):
    """Compile deduplicated terminal references, without changing recognition."""
    series, parallel, feedback = set(), set(), set()
    skipped = {'series': 0, 'parallel': 0, 'feedback': 0}
    for record in structs:
        kind = record.get('kind')
        roles = record.get('roles', {})
        if kind == 'series_chain':
            ordered = [index[name] for name in roles.get('ordered', ()) if name in index]
            for first, second in zip(ordered, ordered[1:]):
                if first not in passive or second not in passive or first == second:
                    skipped['series'] += 1
                    continue
                first, second = sorted((first, second))
                common = (set(pl.comps[first].get('nodes', {}).values())
                          & set(pl.comps[second].get('nodes', {}).values()))
                if len(common) != 1:
                    skipped['series'] += 1
                    continue
                net = next(iter(common))
                a, b = _pins(pl, first, net), _pins(pl, second, net)
                if a and b:
                    series.add((first, a, second, b))
                else:
                    skipped['series'] += 1
        elif kind == 'parallel_passives':
            members = tuple(sorted({index[name] for name in record.get('devices', ())
                                    if name in index}))
            if len(members) < 2 or not set(members) <= passive:
                skipped['parallel'] += 1
                continue
            nets = set(pl.comps[members[0]].get('nodes', {}).values())
            if len(nets) != 2 or any(set(pl.comps[i].get('nodes', {}).values()) != nets
                                     for i in members):
                skipped['parallel'] += 1
                continue
            a, b = sorted(nets)
            terminals = tuple((i, _pins(pl, i, a), _pins(pl, i, b)) for i in members)
            if any(len(pa) != 1 or len(pb) != 1 for _, pa, pb in terminals):
                skipped['parallel'] += 1
                continue
            parallel.add(terminals)
        elif kind == 'feedback_network':
            amplifier = index.get(roles.get('amplifier'))
            port = {'plus': 'IN+', 'minus': 'IN-'}.get(roles.get('input'))
            if (amplifier is None or port is None
                    or port not in pl.comps[amplifier].get('nodes', {})
                    or 'OUT' not in pl.comps[amplifier].get('nodes', {})):
                skipped['feedback'] += 1
                continue
            chain = [index[name] for name in roles.get('series', ()) if name in index]
            wanted = []
            if chain:
                wanted.extend(((chain[0], 'OUT'), (chain[-1], port)))
            wanted.extend((index[name], port) for name in roles.get('shunts', ())
                          if name in index)
            for component, target in wanted:
                if component not in passive:
                    skipped['feedback'] += 1
                    continue
                net = pl.comps[amplifier]['nodes'][target]
                pins = _pins(pl, component, net)
                if pins:
                    # Shared devices can serve more than one distinct E port.
                    # Only genuinely identical device/terminal/port links dedup.
                    feedback.add((component, pins, amplifier, target))
                else:
                    skipped['feedback'] += 1
    return {'series': tuple(sorted(series)), 'parallel': tuple(sorted(parallel)),
            'feedback': tuple(sorted(feedback)), 'skipped': skipped}


class FlexiblePassivePlacement(StructuredPlacement):
    """Preserve active moves while adding bounded cross-row R/C/L/D proposals."""

    passive_policy = 'flexible'

    def set_state(self, cells, orientations):
        self.cell_of = [tuple(cell) for cell in cells]
        self.orient = list(orientations)
        self.comp_at = {cell: i for i, cell in enumerate(self.cell_of)}
        self.row_of = [row for row, _ in self.cell_of]

    def valid_state(self):
        if (len(self.cell_of) != len(self.comps) or len(self.orient) != len(self.comps)
                or len(self.comp_at) != len(self.comps)):
            return False
        if (self.comp_at != {cell: i for i, cell in enumerate(self.cell_of)}
                or self.row_of != [row for row, _ in self.cell_of]):
            return False
        if any(len(cell) != 2 or any(type(v) is not int for v in cell)
               or not 0 <= cell[0] < len(self.row_y)
               or not 0 <= cell[1] < self.cols for cell in self.cell_of):
            return False
        if any(self.cell_of[i][0] != row for i, row in self.fixed_rows.items()):
            return False
        if any(self.cell_of[i] != tuple(cell)
               for i, cell in getattr(self, 'fixed_cells', {}).items()):
            return False
        if any(self.orient[i] not in self.allowed_orientations(i)
               for i in range(len(self.comps))):
            return False
        return self.blocks_contiguous()

    def _passive_move(self, index, target_cell):
        """Propose one passive move/swap, returning the ordinary SA undo pair."""
        count = self.move_counts
        count['passive_attempts'] += 1
        before = (list(self.cell_of), list(self.orient))
        self.last_proposal = None
        try:
            target = tuple(target_cell)
        except TypeError:
            target = ()
        if (index not in self._passive_set or len(target) != 2
                or any(type(v) is not int for v in target)
                or not 0 <= target[0] < len(self.row_y) or not 0 <= target[1] < self.cols
                or target == self.cell_of[index]):
            count['passive_rejected'] += 1
            return None
        other = self.comp_at.get(target)
        if other is not None and other not in self._passive_set:
            count['passive_rejected'] += 1
            return None
        cells = list(self.cell_of)
        origin = cells[index]
        cells[index] = target
        if other is not None:
            cells[other] = origin
        self.set_state(cells, self.orient)
        if not self.valid_state():
            self.set_state(*before)
            count['passive_rejected'] += 1
            return None
        operation = 'passive_swap' if other is not None else 'passive_move'
        count['passive_legal'] += 1
        count[operation + 's'] += 1
        crossed = origin[0] != target[0]
        count['passive_cross_row'] += int(crossed)
        self.last_proposal = {'operation': operation, 'components':
                             (index,) if other is None else (index, other),
                             'cross_row': crossed}
        return before

    def propose(self):
        self.move_counts['total_proposals'] += 1
        self.last_proposal = None
        if self.rng.random() < .5:
            i = self.rng.choice(self.passive_indices)
            row, col = self.cell_of[i]
            target = None
            neighbors = self.passive_neighbors[i]
            if self.rng.random() >= .9 and neighbors:
                neighbor = self.rng.choice(neighbors)
                nr, nc = self.cell_of[neighbor]
                choices = [(nr+dr, nc+dc) for dr, dc in NEIGHBOR_OFFSETS
                           if 0 <= nr+dr < len(self.row_y) and 0 <= nc+dc < self.cols
                           and (nr+dr, nc+dc) not in self.comp_at]
                if choices:
                    target = self.rng.choice(choices)
            if target is None:
                dr, dc = self.rng.choice(NEIGHBOR_OFFSETS)
                target = row+dr, col+dc
            return self._passive_move(i, target)
        self.move_counts['legacy_attempts'] += 1
        before = (list(self.cell_of), list(self.orient))
        previous = super().propose()
        # Original actions only move within each component's current row. The
        # cross-row actions derive that row rather than overwriting its origin.
        self.set_state(self.cell_of, self.orient)
        if previous is None or not self.valid_state():
            self.set_state(*before)
            self.move_counts['legacy_rejected'] += 1
            return None
        self.move_counts['legacy_legal'] += 1
        self.last_proposal = {'operation': 'legacy', 'components': tuple(
            i for i, (cell, orient) in enumerate(zip(before[0], before[1]))
            if cell != self.cell_of[i] or orient != self.orient[i]), 'cross_row': False}
        return previous


def make_flexible(placement, devices, nets, index, structs, *, column_pitch=110,
                  row_pitch=112):
    """Copy an existing seed without consuming RNG; append two columns/one row.

    No-passive seeds retain the original object and exact random sequence.
    Already-converted placements are returned unchanged, without growing axes.
    """
    if isinstance(placement, FlexiblePassivePlacement):
        return placement
    passive = tuple(sorted(index[name] for name, device in devices.items()
                           if device.get('cls') == 'PASSIVE'
                           and device.get('type') in PASSIVE_KINDS
                           and index[name] not in getattr(placement, 'fixed', ())))
    if not passive:
        return placement
    if set(index) != {c['id'] for c in placement.comps}:
        raise ValueError('Passive source IDs do not match the placement')
    clone = FlexiblePassivePlacement.__new__(FlexiblePassivePlacement)
    clone.__dict__ = deepcopy(placement.__dict__)
    clone.passive_policy = 'flexible'
    clone.passive_indices = passive
    clone._passive_set = frozenset(passive)
    clone.fixed_rows = {i: cell[0] for i, cell in enumerate(clone.cell_of)
                        if i not in clone._passive_set}
    clone.column_x = _axis(clone.column_x, 2, column_pitch)
    clone.row_y = _axis(clone.row_y, 1, row_pitch)
    clone.cols = len(clone.column_x)
    clone.passive_neighbors = {}
    for i in passive:
        connected = set()
        for net in clone.comps[i].get('nodes', {}).values():
            if par.power_marker_sym(net):
                continue
            connected.update(index[name] for name, _ in nets.get(net, ())
                             if name in index and index[name] != i)
        clone.passive_neighbors[i] = tuple(sorted(connected))
    clone.passive_relations = _relations(clone, structs, index, clone._passive_set)
    clone.move_counts = {name: 0 for name in (
        'total_proposals', 'passive_attempts', 'passive_legal', 'passive_moves',
        'passive_swaps', 'passive_cross_row', 'passive_rejected',
        'legacy_attempts', 'legacy_legal', 'legacy_rejected')}
    clone.last_proposal = None
    clone.set_state(clone.cell_of, clone.orient)
    if not clone.valid_state():
        raise ValueError('Initial flexible-passive seed violates placement constraints')
    return clone


def _distance(a, b):
    return abs(a[0]-b[0]) + abs(a[1]-b[1])


def score_passives(placement):
    """Return normalized, deduplicated soft estimates, without any search."""
    relations = getattr(placement, 'passive_relations', {})
    series = []
    for first, pa, second, pb in relations.get('series', ()):
        series.append(min(_distance(placement.pin_world(first, a), placement.pin_world(second, b))
                          for a in pa for b in pb) / DISTANCE_UNIT)
    parallels = []
    for group in relations.get('parallel', ()):
        ends = [(placement.pin_world(i, pa[0]), placement.pin_world(i, pb[0]))
                for i, pa, pb in group]
        candidates = []
        for coordinate in (1, 0):
            # Vertical members align each net's y; horizontal members align x.
            a = [first[coordinate] for first, _ in ends]
            b = [second[coordinate] for _, second in ends]
            spread = ((max(a)-min(a)) + (max(b)-min(b))) / (2 * DISTANCE_UNIT)
            bad_axis = fmean(abs(first[1-coordinate]-second[1-coordinate]) > 1e-9
                             for first, second in ends)
            candidates.append(spread + bad_axis)
        parallels.append(min(candidates))
    feedback = []
    for component, pins, amplifier, target in relations.get('feedback', ()):
        endpoint = placement.pin_world(amplifier, target)
        feedback.append(min(_distance(placement.pin_world(component, pin), endpoint)
                            for pin in pins) / DISTANCE_UNIT)
    values = {'series_adjacency': fmean(series) if series else 0.0,
              'parallel_alignment': fmean(parallels) if parallels else 0.0,
              'feedback_locality': fmean(feedback) if feedback else 0.0}
    values['weighted_total'] = sum(values[name] * weight for name, weight in WEIGHTS.items())
    values['relation_counts'] = {'series': len(series), 'parallel': len(parallels),
                                'feedback': len(feedback)}
    return values


def passive_diagnostics(placement):
    """JSON-safe policy, current regularity and cumulative proposal counters."""
    if not isinstance(placement, FlexiblePassivePlacement):
        return {'policy': 'fixed', 'version': VERSION, 'movable_ids': [],
                'reason': 'no_flexible_passive_placement'}
    return {'policy': 'flexible', 'version': VERSION,
            'scope': 'sa_estimate_only',
            'movable_ids': [placement.comps[i]['id'] for i in placement.passive_indices],
            'fixed_rows': {placement.comps[i]['id']: row
                           for i, row in placement.fixed_rows.items()},
            'grid_rows': len(placement.row_y), 'grid_columns': placement.cols,
            'move_counts': dict(placement.move_counts),
            'counter_scope': 'all_proposals_including_temperature_sampling',
            'counter_meaning': 'legal proposals can subsequently be rejected by annealing',
            'regularity': score_passives(placement), 'weights': dict(WEIGHTS),
            'distance_unit': DISTANCE_UNIT,
            'skipped_relations': dict(placement.passive_relations['skipped'])}
