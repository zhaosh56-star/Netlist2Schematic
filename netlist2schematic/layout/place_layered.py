#!/usr/bin/env python3
"""Layer-constrained simulated annealing using Analog Canvas symbol geometry.

MOS/BJT devices stay in their assigned rows and may only mirror horizontally.
Passive devices and sources attach to those rows. Rendering is owned by run_flow.
"""
import math
import os
import random
import time
from collections import Counter, defaultdict

from netlist2schematic.recognition import device_leveling as dl
from netlist2schematic.recognition import vf2_match
from netlist2schematic.common.sa_common import load_local

pn = load_local('parse_netlist', 'parse-netlist.py')
par = load_local('place_and_route', 'place-and-route.py')
GEO_DOC = par.GEO_DOC
N, CELL = par.N, par.CELL
COLS = N
POWER_NETS = par.POWER_NETS
SA_ITERS = 20000
T0, T1 = 40.0, 0.5
SEED = 20261029
BOX_W_REL = 0.05
SYM_SLACK = 1
SYM_W_REL = float(os.environ.get('SYM_W_REL', '0.10'))
PASSIVE_MID_COL = 1
MIRROR_ORIENTS = {'nmos': [0, 1], 'pmos': [0, 1], 'npn': [0, 1], 'pnp': [0, 1]}
LOCK_ORIENTS = {'opamp': [0], 'vcvs': [0]}
VERT_ORIENTS = {}
FLIP_ORIENTS = {symbol: [0, 1] for symbol in (
    'resistor', 'capacitor', 'inductor', 'diode', 'current-source', 'voltage-source')}


def find_structures(path, cell=None):
    raw = vf2_match.analyze(path, cell=cell)
    structs = [{'kind': row['kind'], 'devices': row['devices']} for row in raw]
    pairs = [tuple(row['devices']) for row in raw
             if len(row['devices']) == 2 and row['kind'] in
             ('差分对', '电流镜', 'CMOS互补', '交叉耦合')]
    return structs, pairs


def net_is_pwr(net):
    return par.power_marker_sym(net) == 'vdd-port'


def net_is_gnd(net):
    return par.power_marker_sym(net) == 'ground'


def attach_info(devices, net_lv, in_conduction):
    """Describe non-active devices independently of the final row numbers."""
    info = {}
    for name, device in sorted(devices.items()):
        cls = device['cls']
        if cls in ('MOS', 'BJT'):
            continue
        if cls == 'PASSIVE':
            a, b = device['a'], device['b']
            la, lb = net_lv.get(a, -1), net_lv.get(b, -1)
            pwr = net_is_pwr(a) or net_is_pwr(b)
            gnd = net_is_gnd(a) or net_is_gnd(b)
            if gnd:
                other = b if net_is_gnd(a) else a
                info[name] = {'mode': 'gnd', 'anchor_net': other, 'horiz': not pwr}
            elif pwr:
                other = b if net_is_pwr(a) else a
                info[name] = {'mode': 'pwr', 'anchor_net': other}
            elif a in in_conduction and b not in in_conduction:
                info[name] = {'mode': 'gate', 'anchor_net': a, 'row': la}
            elif b in in_conduction and a not in in_conduction:
                info[name] = {'mode': 'gate', 'anchor_net': b, 'row': lb}
            elif la >= 0 and lb >= 0 and la != lb:
                info[name] = {'mode': 'mid', 'span_lo': min(la, lb), 'span_hi': max(la, lb)}
            else:
                info[name] = {'mode': 'horiz', 'anchor_net': a, 'row': max(la, lb, 0)}
        elif cls == 'ISRC':
            mode = 'pwrsrc' if net_is_pwr(device['p']) else 'sink'
            info[name] = {'mode': mode,
                          'anchor_net': device['n'] if mode == 'pwrsrc' else device['p']}
        elif cls == 'VSRC':
            mode = 'pwrsrc' if net_is_pwr(device['p']) else 'vsink'
            info[name] = {'mode': mode, 'anchor_net': device['p']}
        elif cls == 'VCVS':
            info[name] = {'mode': 'vcvs', 'anchor_net': device['op']}
        else:
            raise ValueError(f'No attachment rule for {name}: {cls}')
    return info


class LayeredPlacement(par.Placement):
    """Grid placement with immutable device rows and optional fixed positions."""

    def __init__(self, comps, rng, row_of, fixed_cells=None, cols=None):
        self.comps = comps
        self.rng = rng
        self.row_of = list(row_of)
        self.cols = COLS if cols is None else cols
        self.fixed_cells = dict(fixed_cells or {})
        self.fixed = set(self.fixed_cells)
        self.orient = [0] * len(comps)
        self.cell_of = [None] * len(comps)
        self.comp_at = {}
        if len(self.row_of) != len(comps) or any(
                not isinstance(row, int) or row < 0 for row in self.row_of):
            raise ValueError('Every component must have a non-negative integer row')
        if self.cols < 1:
            raise ValueError('The placement grid must have at least one column')
        for index, cell in sorted(self.fixed_cells.items()):
            if index < 0 or index >= len(comps):
                raise ValueError(f'Unknown fixed component index: {index}')
            row, col = cell
            if row != self.row_of[index] or not 0 <= col < self.cols:
                raise ValueError(f'Fixed cell is outside the assigned row/grid: {cell}')
            if cell in self.comp_at:
                raise ValueError(f'Two fixed components occupy {cell}')
            self.cell_of[index] = cell
            self.comp_at[cell] = index
        self._place_initial()
        for index in range(len(comps)):
            self.orient[index] = rng.choice(self.allowed_orientations(index))

    def allowed_orientations(self, index):
        symbol = self.comps[index]['symbol']
        allowed = (MIRROR_ORIENTS.get(symbol) or LOCK_ORIENTS.get(symbol)
                   or FLIP_ORIENTS.get(symbol) or list(range(self.n_orients(index))))
        if any(orientation >= self.n_orients(index) for orientation in allowed):
            raise ValueError(f'The geometry table lacks required orientations for {symbol}')
        return allowed

    def _place_initial(self):
        rows = defaultdict(list)
        for index, row in enumerate(self.row_of):
            if index not in self.fixed:
                rows[row].append(index)
        for row, indices in sorted(rows.items()):
            cols = [col for col in range(self.cols) if (row, col) not in self.comp_at]
            if len(cols) < len(indices):
                raise ValueError(f'Row {row} has more components than available columns')
            self.rng.shuffle(cols)
            for index, col in zip(indices, cols):
                self.cell_of[index] = (row, col)
                self.comp_at[(row, col)] = index

    def try_move(self):
        if not self.comps:
            return None
        rng = self.rng
        index = rng.randrange(len(self.comps))
        if rng.random() < 0.75:
            if index in self.fixed:
                return None
            row, col = self.cell_of[index]
            next_col = rng.randrange(self.cols)
            if next_col == col:
                return None
            other = self.comp_at.get((row, next_col))
            if other in self.fixed:
                return None
            if other is None:
                del self.comp_at[(row, col)]
                self.cell_of[index] = (row, next_col)
                self.comp_at[(row, next_col)] = index
                return ('relocate', index, (row, col))
            old_cell, new_cell = self.cell_of[index], self.cell_of[other]
            self.cell_of[index], self.cell_of[other] = new_cell, old_cell
            self.comp_at[old_cell], self.comp_at[new_cell] = other, index
            return ('swap', index, other)
        old = self.orient[index]
        alternatives = [value for value in self.allowed_orientations(index) if value != old]
        if not alternatives:
            return None
        self.orient[index] = rng.choice(alternatives)
        return ('orient', index, old)

    def undo(self, move):
        kind, index, previous = move
        if kind == 'swap':
            a, b = self.cell_of[index], self.cell_of[previous]
            self.cell_of[index], self.cell_of[previous] = b, a
            self.comp_at[a], self.comp_at[b] = previous, index
        elif kind == 'relocate':
            del self.comp_at[self.cell_of[index]]
            self.cell_of[index] = previous
            self.comp_at[previous] = index
        else:
            self.orient[index] = previous


def box_area_pen(pl, structs, comp_index):
    result = 0.0
    for struct in structs:
        cells = [pl.cell_of[comp_index[name]] for name in struct['devices']]
        if cells:
            rows, cols = zip(*cells)
            result += (max(cols) - min(cols) + 1) * (max(rows) - min(rows) + 1)
    return result


def sym_pen(pl, pairs, comp_index):
    return sum(max(0, abs(pl.cell_of[comp_index[a]][1] -
                          pl.cell_of[comp_index[b]][1]) - SYM_SLACK) for a, b in pairs)


def sa_layered(pl, nets, comp_index, structs, pairs, iterations=SA_ITERS, deadline=None):
    if iterations < 0:
        raise ValueError('iterations must be non-negative')
    route_nets = {net: list(members) for net, members in nets.items()
                  if par.power_marker_sym(net) is None}

    def energy():
        _, metrics = par.route(route_nets, pl, comp_index)
        wirelength = metrics['wirelength']
        value = (wirelength + 500 * metrics['crossings'] + 30 * metrics['bends']
                 + par.BODY_PEN_W * metrics['body_pen']
                 + par.OVERLAP_W * metrics['overlap']
                 + BOX_W_REL * wirelength * box_area_pen(pl, structs, comp_index)
                 + SYM_W_REL * wirelength * sym_pen(pl, pairs, comp_index))
        return value

    current = best = energy()
    best_state = (list(pl.cell_of), list(pl.orient))
    pl.sa_iterations_completed = 0
    pl.sa_stop_reason = 'iterations'
    for iteration in range(iterations):
        if deadline is not None and iteration % 32 == 0 and time.perf_counter() >= deadline:
            pl.sa_stop_reason = 'quality_budget'
            break
        pl.sa_iterations_completed = iteration + 1
        if iteration and iteration % 1000 == 0:
            print(f'SA {iteration}/{iterations}: best energy={best:.0f}', flush=True)
        temperature = T0 * (T1 / T0) ** (iteration / iterations)
        move = pl.try_move()
        if move is None:
            continue
        candidate = energy()
        if candidate <= current or pl.rng.random() < math.exp((current - candidate) / temperature):
            current = candidate
            if candidate < best:
                best = candidate
                best_state = (list(pl.cell_of), list(pl.orient))
        else:
            pl.undo(move)
    pl.cell_of, pl.orient = best_state
    pl.comp_at = {cell: index for index, cell in enumerate(pl.cell_of)}
    return best, route_nets


def _allocate_rows(devices, comps, comp_index, net_lv, dev_level, att):
    """Allocate separate gap rows for attachments without assuming dense levels."""
    row_of = [None] * len(comps)
    fixed_names = []
    pure_passive = not dev_level
    if pure_passive:
        for name, attachment in att.items():
            mode = attachment['mode']
            row_of[comp_index[name]] = (1 if mode in ('pwrsrc', 'pwr', 'gate', 'mid') else
                                       5 if mode in ('sink', 'vsink', 'gnd') else 3)
        return row_of, fixed_names

    levels = sorted(set(dev_level.values()))
    if any(level < 1 for level in levels):
        raise ValueError('An active device has no reachable conduction layer')

    def nearest_level(level):
        return min(levels, key=lambda value: (abs(value - max(level, 1)), value))

    gap_before = set()
    attachment_level = {}
    for name, attachment in att.items():
        mode = attachment['mode']
        if mode in ('gate', 'pwr'):
            level = nearest_level(attachment.get('row', net_lv.get(attachment['anchor_net'], 1)))
            attachment_level[name] = level
            gap_before.add(level)
        elif mode == 'mid':
            low, high = attachment['span_lo'], attachment['span_hi']
            # Choose a genuine boundary inside the span, nearest its midpoint.
            boundaries = [level for level in levels if low < level <= high]
            midpoint = (low + high + 1) / 2
            level = min(boundaries, key=lambda value: (abs(value - midpoint), value)) \
                if boundaries else nearest_level(high)
            attachment_level[name] = level
            gap_before.add(level)
            fixed_names.append(name)

    row_map = {}
    next_row = 1
    for index, level in enumerate(levels):
        next_row = max(next_row, level)
        if index and level in gap_before:
            next_row += 1
        row_map[level] = next_row
        next_row += 1
    for name, level in dev_level.items():
        row_of[comp_index[name]] = row_map[level]
    for name, attachment in att.items():
        mode = attachment['mode']
        if mode == 'pwrsrc':
            row = 0
        elif mode in ('sink', 'vsink') or (mode == 'gnd' and not attachment['horiz']):
            row = next_row
        elif mode in ('gate', 'pwr', 'mid'):
            row = row_map[attachment_level[name]] - 1
        else:
            # Ground-following passives, horizontal passives and E proxies use
            # a real active layer even when their anchor is an isolated net.
            level = net_lv.get(attachment.get('anchor_net'), attachment.get('row', 1))
            row = row_map[nearest_level(level)]
        row_of[comp_index[name]] = row
    return row_of, fixed_names


def _align_attachments(pl, devices, comps, comp_index, att):
    """Put same-row passive followers beside active anchors when space permits."""
    active_by_net = defaultdict(list)
    for name, device in devices.items():
        if device['cls'] in ('MOS', 'BJT'):
            for net in (device.get('d'), device.get('s'), device.get('c'), device.get('e')):
                if net is not None:
                    active_by_net[net].append(name)
    for name, attachment in att.items():
        index = comp_index[name]
        mode = attachment['mode']
        symbol = comps[index]['symbol']
        if symbol not in FLIP_ORIENTS:
            continue
        if mode in ('horiz', 'gnd', 'sink', 'vsink', 'pwrsrc'):
            pl.orient[index] = 1
        elif mode in ('gate', 'pwr', 'mid'):
            pl.orient[index] = 0
        if index in pl.fixed or mode not in ('horiz', 'gnd'):
            continue
        for active in active_by_net.get(attachment.get('anchor_net'), []):
            anchor = comp_index[active]
            if pl.row_of[anchor] != pl.row_of[index]:
                continue
            row, col = pl.cell_of[anchor]
            target = next(((row, candidate) for candidate in (col - 1, col + 1)
                           if 0 <= candidate < pl.cols and (row, candidate) not in pl.comp_at), None)
            if target is not None:
                del pl.comp_at[pl.cell_of[index]]
                pl.cell_of[index] = target
                pl.comp_at[target] = index
                break


def create_placement(path, *, cell=None, iterations=SA_ITERS, seed=SEED, deadline=None):
    """Return placement, parsed circuit and metadata; do not write output files."""
    if iterations < 0:
        raise ValueError('iterations must be non-negative')

    def check_initialization_budget():
        if deadline is not None and time.perf_counter() >= deadline:
            raise ValueError('Quality budget exhausted during legacy placement initialization')

    check_initialization_budget()
    doc = pn.parse(path, cell=cell)
    comps, nets = list(doc['components']), dict(doc['nets'])
    if not comps:
        raise ValueError('The selected netlist contains no supported devices')
    comp_index = {component['id']: index for index, component in enumerate(comps)}
    devices = dl.parse_netlist(path, cell=cell)
    if set(devices) != set(comp_index):
        raise ValueError('The parser and layer model disagree on the component set')
    check_initialization_budget()
    levels = dl.compute_levels(devices)
    net_lv, dev_level = levels['net_levels'], levels['device_levels']
    conduction = set()
    for device in devices.values():
        if device['cls'] == 'MOS':
            conduction.update((device['d'], device['s']))
        elif device['cls'] == 'BJT':
            conduction.update((device['c'], device['e']))
    att = attach_info(devices, net_lv, conduction)
    row_of, fixed_names = _allocate_rows(devices, comps, comp_index, net_lv, dev_level, att)
    counts = Counter(row_of)
    global COLS
    COLS = max(counts.values()) + 1
    fixed_cells = {}
    next_fixed_col = defaultdict(lambda: PASSIVE_MID_COL)
    for name in sorted(fixed_names):
        index = comp_index[name]
        row = row_of[index]
        fixed_cells[index] = (row, next_fixed_col[row])
        next_fixed_col[row] += 1
    pl = LayeredPlacement(comps, random.Random(seed), row_of, fixed_cells, cols=COLS)
    _align_attachments(pl, devices, comps, comp_index, att)
    check_initialization_budget()
    structs, pairs = find_structures(path, cell=cell)
    check_initialization_budget()
    print(f'网格: COLS={COLS}; 分层 [源:{levels["source_note"]}]: {dev_level}', flush=True)
    best, route_nets = sa_layered(pl, nets, comp_index, structs, pairs,
                                 iterations=iterations, deadline=deadline)
    _, metrics = par.route(route_nets, pl, comp_index)
    metadata = {
        'source_note': levels['source_note'], 'levels': dev_level,
        'attach': att, 'structs': structs, 'sym_pairs': pairs,
        'energy': best, 'optimization_metrics': metrics,
        'fixed_positions': {comps[index]['id']: {'row': row, 'col': col}
                            for index, (row, col) in fixed_cells.items()},
    }
    return pl, comps, nets, comp_index, metadata


def main(argv=None):
    from netlist2schematic.flow import run_flow
    return run_flow.main(argv, default_mode='layered')


if __name__ == '__main__':
    raise SystemExit(main())
