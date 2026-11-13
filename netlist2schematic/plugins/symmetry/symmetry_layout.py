"""Bounded device-only symmetry regularization after SA, before final routing.

Relations come from visible port topology and high-confidence motif roles.  This
module never invokes an annealer or a router and never changes physical axes.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from itertools import combinations, product
from math import ceil, isfinite
from statistics import median
from time import perf_counter

VERSION = 'device-symmetry-v1'
_ACTIVE = frozenset(('nmos', 'pmos', 'npn', 'pnp'))
_PASSIVE = frozenset(('resistor', 'capacitor', 'inductor', 'diode'))
_RAILS = frozenset(('0', 'gnd', 'gnds', 'vss', 'agnd', 'dgnd', 'vdd', 'vcc',
                    'vpwr', 'avdd', 'dvdd'))
_EPS = 1e-8


def _rail(net):
    return str(net).lower().rstrip('!') in _RAILS


def _canonical(comps):
    result = {}
    for comp in comps:
        symbol, nodes = comp.get('symbol'), comp.get('nodes') or {}
        if not nodes or not comp.get('id'):
            continue
        if symbol in ('nmos', 'pmos') and {'D', 'G', 'S'} <= nodes.keys():
            result[comp['id']] = dict(cls='MOS', type=symbol.upper(), d=nodes['D'],
                                      g=nodes['G'], s=nodes['S'], bulk=nodes['S'])
        elif symbol in ('npn', 'pnp') and {'C', 'B', 'E'} <= nodes.keys():
            result[comp['id']] = dict(cls='BJT', type=symbol.upper(), c=nodes['C'],
                                      b=nodes['B'], e=nodes['E'])
        elif symbol in _PASSIVE and ({'A', 'K'} if symbol == 'diode' else {'1', '2'}) <= nodes.keys():
            first, second = ('A', 'K') if symbol == 'diode' else ('1', '2')
            result[comp['id']] = dict(cls='PASSIVE', type={
                'resistor': 'R', 'capacitor': 'C', 'inductor': 'L', 'diode': 'D'}[symbol],
                a=nodes[first], b=nodes[second])
        elif symbol in ('current-source', 'voltage-source') and {'+', '-'} <= nodes.keys():
            current = symbol == 'current-source'
            result[comp['id']] = dict(cls='ISRC' if current else 'VSRC',
                                      type='I' if current else 'V',
                                      p=nodes['+'], n=nodes['-'])
    return result


def _pin_pairs(left, right, net_map):
    """Match passive endpoint roles by their connected nets, not endpoint names."""
    names = sorted(set(left['nodes']) & set(right['nodes']))
    variants = [list(zip(names, names))]
    if left['symbol'] in ('resistor', 'capacitor', 'inductor') and len(names) == 2:
        variants.append(list(zip(names, reversed(names))))
    def score(candidate):
        return sum(left['nodes'][a] == right['nodes'][b]
                   or net_map.get(left['nodes'][a]) == right['nodes'][b]
                   for a, b in candidate)
    return [list(item) for item in max(variants, key=score)]


def build_symmetry_groups(comps, structs=()):
    """Build bounded, conservative paired branches without reading source files."""
    by_id = {row['id']: row for row in comps if row.get('id') and row.get('nodes')}
    if not by_id:
        return []
    if not structs:
        from netlist2schematic.recognition.circuit_structures import analyze_structures
        structs = analyze_structures(_canonical(comps))
    seeds = []

    def seed(kind, pairs, tails=(), *, axis_family=None):
        pairs = [list(pair) for pair in pairs if len(pair) == 2
                 and all(name in by_id for name in pair)
                 and by_id[pair[0]]['symbol'] == by_id[pair[1]]['symbol']]
        if pairs:
            seeds.append({'kind': kind, 'pairs': pairs,
                          'tails': [name for name in tails if name in by_id],
                          'axis_family': axis_family})

    for record in structs:
        if record.get('confidence', 'high') != 'high':
            continue
        kind, roles = record.get('kind'), record.get('roles') or {}
        if kind in ('differential_pair', 'cross_coupled'):
            seed(kind, [(roles.get('left'), roles.get('right'))], roles.get('tail', ()))
        elif kind == 'current_mirror':
            references = roles.get('references', [roles.get('reference')])
            outputs = roles.get('outputs', ())
            if len(references) == len(outputs) == 1:
                seed(kind, [(references[0], outputs[0])])
        elif kind == 'latch':
            seed(kind, [(roles.get('left_' + part), roles.get('right_' + part))
                        for part in ('upper', 'lower')])
        elif kind == 'folded_core':
            # A folded input pair and its inner output branches use two axes.
            seed(kind, [(roles.get('input_left'), roles.get('input_right'))],
                 axis_family='folded_input')
            seed(kind, [(roles.get(prefix + '_left'), roles.get(prefix + '_right'))
                        for prefix in ('folded', 'bias')], axis_family='folded_output')

    # BJT differential pairs require exactly two emitters on a non-rail node,
    # separate collectors/bases, and a genuine current/tail device at that node.
    canonical = _canonical(comps)
    emitter_users = defaultdict(list)
    driven_rails = set()
    for name, dev in canonical.items():
        if dev['cls'] == 'BJT':
            emitter_users[(dev['type'], dev['e'])].append(name)
        elif dev['cls'] == 'VSRC':
            if _rail(dev['n']):
                driven_rails.add(dev['p'])
            if _rail(dev['p']):
                driven_rails.add(dev['n'])
    for (_, common), names in emitter_users.items():
        if len(names) != 2 or _rail(common) or common in driven_rails:
            continue
        a, b = (canonical[name] for name in names)
        if a['c'] == b['c'] or a['b'] == b['b']:
            continue
        tails = [name for name, dev in canonical.items() if name not in names and (
            (dev['cls'] == 'ISRC' and common in (dev['p'], dev['n']))
            or (dev['cls'] == 'MOS' and dev['d'] == common)
            or (dev['cls'] == 'BJT' and dev['c'] == common))]
        if tails:
            seed('bjt_differential_pair', [names], tails)

    incidence = defaultdict(list)
    for name, comp in by_id.items():
        for pin, net in comp['nodes'].items():
            incidence[net].append((name, pin))

    def pair_identities(row):
        return {frozenset(pair) for pair in row['pairs']}

    def bind(mapping, x, y):
        # Reflection is an involution. Recording *both* directions also protects
        # shared branch nodes when arbitrary input-pair labels are reversed.
        if ((_rail(x) or _rail(y)) and x != y
                or mapping.get(x, y) != y or mapping.get(y, x) != x):
            return False
        mapping[x], mapping[y] = y, x
        return True

    def expand(row, extra_blocked=frozenset()):
        pairs, tails = list(row['pairs']), list(row['tails'])
        used = {name for pair in pairs for name in pair} | set(tails)
        base_pairs = pair_identities(row)
        blocked = set(extra_blocked) - used
        for other in seeds:
            other_pairs = pair_identities(other)
            # Explicit composites may contain their own child seed. Independent
            # input/gain/CMFB stages are boundaries even when a gate joins them.
            if other_pairs <= base_pairs:
                continue
            blocked.update(name for pair in other['pairs'] for name in pair if name not in used)
            blocked.update(name for name in other['tails'] if name not in used)
        net_map, valid_seed = {}, True
        for a, b in pairs:
            for pin in sorted(set(by_id[a]['nodes']) & set(by_id[b]['nodes'])):
                valid_seed &= bind(net_map, by_id[a]['nodes'][pin], by_id[b]['nodes'][pin])
        # The reference/output asymmetry of a current mirror is meaningful;
        # keep its local pairing without extending it into surrounding circuitry.
        extend = row['kind'] != 'current_mirror' and valid_seed
        for _ in range(3 if extend else 0):
            proposed = {}
            for left_net, right_net in net_map.items():
                if left_net == right_net:
                    continue
                left, right = defaultdict(set), defaultdict(set)
                for net, target in ((left_net, left), (right_net, right)):
                    for name, pin in incidence[net]:
                        comp = by_id[name]
                        if name in used or name in blocked or comp['symbol'] not in _ACTIVE | _PASSIVE:
                            continue
                        if row['axis_family'] == 'folded_input' and comp['symbol'] in _ACTIVE:
                            continue
                        role = '*' if comp['symbol'] in ('resistor', 'capacitor', 'inductor') else pin
                        target[(comp['symbol'], role)].add(name)
                for key in sorted(set(left) & set(right)):
                    if len(left[key]) != 1 or len(right[key]) != 1:
                        continue
                    a, b = next(iter(left[key])), next(iter(right[key]))
                    if a == b:
                        continue
                    mapping = dict(net_map)
                    port_pairs = _pin_pairs(by_id[a], by_id[b], net_map)
                    if not all(bind(mapping, by_id[a]['nodes'][pa], by_id[b]['nodes'][pb])
                               for pa, pb in port_pairs):
                        continue
                    proposed.setdefault(frozenset((a, b)), (a, b, mapping))
            # Collect a whole depth before claiming devices or nets. Selecting a
            # first match while scanning named nets would make renaming change
            # partners. All conflicting partner or port-map claims are rejected.
            partners, claims = defaultdict(set), defaultdict(set)
            for a, b, mapping in proposed.values():
                partners[a].add(b)
                partners[b].add(a)
                for x, y in mapping.items():
                    claims[x].add(y)
            ambiguous = {name for name, targets in partners.items() if len(targets) > 1}
            conflicting_nets = {net for net, targets in claims.items() if len(targets) > 1}
            added = [(a, b, mapping) for a, b, mapping in proposed.values()
                     if a not in ambiguous and b not in ambiguous
                     and not (set(mapping) - set(net_map)) & conflicting_nets]
            added.sort(key=lambda candidate: tuple(sorted(candidate[:2])))
            if not added or len(used) + 2*len(added) > 16:
                # A depth which exceeds capacity is discarded as a whole.
                break
            for a, b, mapping in added:
                used.update((a, b))
                pairs.append([a, b])
                net_map.update(mapping)
        return dict(row, pairs=pairs, tails=tails, net_map=net_map,
                    seed_pairs=base_pairs)

    for row in seeds:
        if row['axis_family'] == 'folded_input':
            identities = {frozenset(pair) for pair in row['pairs']}
            row['tails'] = sorted(set(row['tails']) | {
                name for other in seeds if {frozenset(pair) for pair in other['pairs']} == identities
                for name in other['tails']})
    expanded = [expand(row) for row in seeds]
    # A weak auxiliary device reached from two independent cores has no unique
    # ownership. Do not use it to force those cores onto a common axis.
    ambiguous_members = set()
    for i, first in enumerate(expanded):
        for second in expanded[i+1:]:
            aa, bb = first['seed_pairs'], second['seed_pairs']
            if aa <= bb or bb <= aa:
                continue
            first_members = {name for pair in first['pairs'] for name in pair}
            second_members = {name for pair in second['pairs'] for name in pair}
            base_members = {name for pair in aa | bb for name in pair}
            ambiguous_members.update((first_members & second_members) - base_members)
    if ambiguous_members:
        expanded = [expand(row, ambiguous_members) for row in seeds]
    # A composite supersedes duplicate child pairing, preserving tail evidence.
    expanded.sort(key=lambda row: (-len(row['pairs']), row['kind'], repr(row['pairs'])))
    groups = []
    owned_pairs = set()
    for row in expanded:
        identities = {frozenset(pair) for pair in row['pairs']}
        if identities and identities <= owned_pairs:
            for group in groups:
                if identities <= {frozenset(pair) for pair in group['pairs']}:
                    group['tails'] = sorted(set(group['tails']) | set(row['tails']))
                    group['members'] = sorted(set(group['members']) | set(row['tails']))
                    break
            continue
        # A folded input axis may not be expanded by a contained differential
        # pair's outer fan-out. Keep the explicit axis families authoritative.
        if row['axis_family'] is None and any(
                other['axis_family'] is not None and identities & {
                    frozenset(pair) for pair in other['pairs']} for other in expanded):
            continue
        members = sorted({name for pair in row['pairs'] for name in pair} | set(row['tails']))
        group = {'id': 'symmetry:' + '|'.join(members), 'kind': row['kind'],
                 'scope': 'local' if len(row['pairs']) == 1 else 'composite',
                 'axis_family': row['axis_family'], 'members': members,
                 'pairs': row['pairs'], 'tails': row['tails'],
                 'pin_pairs': {a + '|' + b: _pin_pairs(by_id[a], by_id[b], row['net_map'])
                               for a, b in row['pairs']}}
        if len(row['pairs']) > 1 and {
                name for name, comp in by_id.items() if comp['symbol'] in _ACTIVE} <= set(members):
            group['scope'] = 'global'
        groups.append(group)
        owned_pairs.update(identities)
    return sorted(groups, key=lambda group: group['id'])


def _position(pl, i):
    return tuple(float(value) for value in pl.pos_of(i))


def _pair_geometry(pl, comps, index, group, a, b):
    mappings = group.get('pin_pairs', {}).get(a + '|' + b)
    if not mappings:
        mappings = [[pin, pin] for pin in sorted(
            set(comps[index[a]].get('nodes', {})) & set(comps[index[b]].get('nodes', {})))]
    pins = [(tuple(pl.pin_world(index[a], pa)), tuple(pl.pin_world(index[b], pb)))
            for pa, pb in mappings]
    if not pins:
        pins = [(_position(pl, index[a]), _position(pl, index[b]))]
    return mappings, pins


def _tail_point(pl, comps, index, name):
    comp = comps[index[name]]
    pin = next((p for p in ('D', 'C', '+', '1') if p in comp.get('nodes', {})), None)
    return tuple(pl.pin_world(index[name], pin)) if pin else _position(pl, index[name])


def measure_symmetry(placement, comps, index, groups, grid_step=2.0):
    """Measure physical visible-pin reflection, independent of wire geometry."""
    if type(grid_step) not in (int, float) or not isfinite(grid_step) or grid_step <= 0:
        raise ValueError('symmetry grid_step must be positive and finite')
    errors, records = [], []
    for group in groups:
        pairs = [(a, b) for a, b in group.get('pairs', ()) if a in index and b in index]
        tails = [name for name in group.get('tails', ()) if name in index]
        if not pairs:
            continue
        geometry = [(a, b, *_pair_geometry(placement, comps, index, group, a, b)) for a, b in pairs]
        # Device origins define the proposed axis, while all matched original
        # pins define error; a mirrored MOS's +/-10 channel offset is retained.
        centers = [(_position(placement, index[a])[0] + _position(placement, index[b])[0]) / 2
                   for a, b in pairs]
        centers.extend(_tail_point(placement, comps, index, name)[0] for name in tails)
        axis = float(median(centers))
        pair_records, tail_records = [], []
        for a, b, mappings, pins in geometry:
            error = sum(abs(left[0] + right[0] - 2 * axis)
                        + abs(left[1] - right[1]) for left, right in pins) / len(pins)
            errors.append(error)
            pair_records.append({'devices': [a, b], 'left': {'device': a,
                'position': list(_position(placement, index[a])),
                'pins': [list(left) for left, _ in pins]}, 'right': {'device': b,
                'position': list(_position(placement, index[b])),
                'pins': [list(right) for _, right in pins]},
                'pin_pairs': mappings, 'error': error,
                'midpoint_x': (_position(placement, index[a])[0] + _position(placement, index[b])[0]) / 2})
        for name in tails:
            point = _tail_point(placement, comps, index, name)
            error = abs(point[0] - axis)
            errors.append(error)
            tail_records.append({'device': name, 'point': list(point), 'error': error})
        records.append({'id': group['id'], 'kind': group.get('kind'),
                        'scope': group.get('scope', 'local'),
                        'axis_family': group.get('axis_family'),
                        'members': list(group.get('members', ())), 'axis_x': axis,
                        'pairs': pair_records, 'tails': tail_records})
    residual = sum(errors) / max(1, len(errors))
    return {'failed_relations': sum(error > grid_step + _EPS for error in errors),
            'residual_bucket': max(0, ceil((residual - _EPS) / grid_step)),
            'residual': residual, 'relation_count': len(errors), 'groups': records}


def report_symmetry(placement, comps, index, metadata, *, policy='auto', reason=None,
                    grid_step=2.0):
    """Refresh a saved report without discarding the regularization history."""
    previous = metadata.get('layout_symmetry') if isinstance(metadata, dict) else None
    report = deepcopy(previous) if isinstance(previous, dict) else {}
    groups = report.get('relations')
    if not isinstance(groups, list):
        groups = build_symmetry_groups(comps, metadata.get('structs', ()))
    current = measure_symmetry(placement, comps, index, groups, grid_step)
    report.update(version=VERSION, policy=policy, relations=deepcopy(groups), current=current)
    report.setdefault('before', deepcopy(current))
    report.setdefault('after', deepcopy(current))
    report.setdefault('actions', [])
    report.setdefault('status', 'measured' if groups else 'skipped')
    if reason is not None:
        report['reason'] = reason
        report.setdefault('stop_reason', reason)
    elif not groups:
        report.setdefault('stop_reason', 'no_high_confidence_relations')
    return report


def _key(measurement):
    return (measurement['failed_relations'], measurement['residual_bucket'], measurement['residual'])


def _hpwl(par, pl, nets, index):
    value = 0.0
    for net, members in nets.items():
        if par.power_marker_sym(net) or len(members) < 2:
            continue
        pins = [pl.pin_world(index[name], pin) for name, pin in members]
        xs, ys = zip(*pins)
        value += max(xs) - min(xs) + max(ys) - min(ys)
    return value


def _set_state(pl, cells, orientations):
    if hasattr(pl, 'set_state'):
        pl.set_state(cells, orientations)
    else:
        pl.cell_of, pl.orient = list(cells), list(orientations)
        pl.comp_at = {tuple(cell): i for i, cell in enumerate(cells)}
    if hasattr(pl, 'row_of'):
        pl.row_of = [cell[0] for cell in cells]


def _valid_state(pl, original, comps, protected):
    if len(set(pl.cell_of)) != len(comps):
        return False
    for i, (row, col) in enumerate(pl.cell_of):
        if not (0 <= row < len(pl.row_y) and 0 <= col < len(pl.column_x)):
            return False
        if comps[i]['symbol'] not in _PASSIVE and row != original.cell_of[i][0]:
            return False
        if i in getattr(pl, 'fixed', ()) or i in getattr(pl, 'fixed_cells', {}):
            if pl.cell_of[i] != original.cell_of[i] or pl.orient[i] != original.orient[i]:
                return False
        if comps[i]['id'] in protected and pl.orient[i] != original.orient[i]:
            return False
        if comps[i]['symbol'] in _ACTIVE and pl.orient[i] not in (0, 1):
            return False
        if hasattr(pl, 'allowed_orientations') and pl.orient[i] not in pl.allowed_orientations(i):
            return False
    if hasattr(pl, 'valid_state') and not pl.valid_state():
        return False
    return not hasattr(pl, 'blocks_contiguous') or pl.blocks_contiguous()


def _static_safe(par, pl, nets, index, options, deadline):
    from netlist2schematic.routing.routing_rules import analyze_routes, build_scene, segment_hits_rect
    scene = build_scene(par, nets, pl, index, options=options, deadline=deadline)
    # Full fixed-port and marker geometry remains authoritative. No missing-net
    # connectivity is checked here: signal wiring has not yet been constructed.
    result = analyze_routes(list(scene.fixed_segments.items()), scene, deadline=deadline)
    if result['violations']:
        return False
    for number, (a, first) in enumerate(scene.body_rects):
        for _, second in scene.body_rects[number + 1:]:
            if max(first[0], second[0]) < min(first[2], second[2]) - _EPS and max(
                    first[1], second[1]) < min(first[3], second[3]) - _EPS:
                return False
    # The default marker's declared entry shares its own component's exit
    # corridor. Protect every foreign body, without rejecting that entry merely
    # because the two inflated boxes touch along the power stub.
    marker_components = [index[name] for net, members in nets.items()
                         if par.power_marker_sym(net) for name, _ in members]
    for number, (_, marker) in enumerate(scene.marker_rects):
        owner = marker_components[number] if number < len(marker_components) else None
        if any(component != owner and max(marker[0], body[0]) < min(marker[2], body[2]) - _EPS and max(
                marker[1], body[1]) < min(marker[3], body[3]) - _EPS
               for component, body in scene.body_rects):
            return False
    for net in scene.power_nets:
        for a, b in scene.fixed_segments.get(net, ()):
            owners = {index[name] for name, pin in nets[net]
                      if tuple(pl.pin_world(index[name], pin)) in (a, b)}
            if any(component not in owners and segment_hits_rect(a, b, body)
                   for component, body in scene.body_rects):
                return False
            if any(not (owner == net and number < len(marker_components)
                        and marker_components[number] in owners)
                   and segment_hits_rect(a, b, marker)
                   for number, (owner, marker) in enumerate(scene.marker_rects)):
                return False
    return True


def _orientation_pairs(par, pl, comps, index, a, b, protected):
    ia, ib = index[a], index[b]
    def allowed(i):
        if comps[i]['id'] in protected or i in getattr(pl, 'fixed', ()):
            return [pl.orient[i]]
        if comps[i]['symbol'] in _ACTIVE:
            return [0, 1]
        if hasattr(pl, 'allowed_orientations'):
            return list(pl.allowed_orientations(i))
        return list(range(len(par.GEO_DOC[comps[i]['symbol']]['orientations'])))
    return product(allowed(ia), allowed(ib))


def _proposals(par, pl, comps, index, groups, protected, deadline=None):
    """Stable whole-group and pair transactions over the existing column axes."""
    columns = len(pl.column_x)
    for group in groups:
        if deadline is not None and perf_counter() >= deadline:
            return
        measured = measure_symmetry(pl, comps, index, [group])['groups']
        if not measured:
            continue
        row = measured[0]
        axes = sorted({row['axis_x'], *(p['midpoint_x'] for p in row['pairs']),
                       *(t['point'][0] for t in row['tails'])},
                      key=lambda axis: (abs(axis-row['axis_x']), axis))
        members = {index[name] for name in group['members'] if name in index}
        # A collective reflection repairs multiple rows without forcing the
        # already-rejected intermediate placement of a single pair.
        for axis in axes[:6]:
            for handedness in (0, 1):
                if deadline is not None and perf_counter() >= deadline:
                    return
                cells, orientations = list(pl.cell_of), list(pl.orient)
                taken = {cell for i, cell in enumerate(cells) if i not in members}
                valid = True
                for a, b in group['pairs']:
                    ia, ib = index[a], index[b]
                    ra, ca = cells[ia]
                    rb, cb = cells[ib]
                    left_first = pl.pos_of(ia)[0] <= pl.pos_of(ib)[0]
                    choices = []
                    for x, y in combinations(range(columns), 2):
                        if deadline is not None and perf_counter() >= deadline:
                            return
                        if not left_first:
                            x, y = y, x
                        if (ra, x) in taken or (rb, y) in taken:
                            continue
                        error = abs(pl.column_x[x] + pl.column_x[y] - 2*axis)
                        movement = abs(x-ca) + abs(y-cb)
                        choices.append((error, movement, x, y))
                    if not choices:
                        valid = False
                        break
                    _, _, x, y = min(choices)
                    cells[ia], cells[ib] = (ra, x), (rb, y)
                    taken.update((cells[ia], cells[ib]))
                    if comps[ia]['symbol'] in _ACTIVE:
                        desired = (handedness, 1-handedness)
                        if not left_first:
                            desired = desired[::-1]
                        for i, value in zip((ia, ib), desired):
                            if comps[i]['id'] not in protected and i not in getattr(pl, 'fixed', ()):
                                orientations[i] = value
                if valid:
                    for name in group['tails']:
                        i = index[name]
                        r, old = cells[i]
                        offset = _tail_point(pl, comps, index, name)[0] - pl.pos_of(i)[0]
                        choices = [(abs(pl.column_x[c]+offset-axis), abs(c-old), c)
                                   for c in range(columns) if (r, c) not in taken]
                        if choices:
                            c = min(choices)[2]
                            cells[i] = (r, c)
                            taken.add((r, c))
                    yield cells, orientations, {'kind': 'paired_structure', 'group': group['id'],
                                                'target_axis': axis}
        for a, b in group['pairs']:
            ia, ib = index[a], index[b]
            # Mirror/orientation changes are evaluated at actual visible pins.
            for oa, ob in _orientation_pairs(par, pl, comps, index, a, b, protected):
                orientations = list(pl.orient)
                orientations[ia], orientations[ib] = oa, ob
                yield list(pl.cell_of), orientations, {'kind': 'paired_orientation', 'devices': [a, b]}
            ra, ca = pl.cell_of[ia]
            rb, cb = pl.cell_of[ib]
            outside = {cell for i, cell in enumerate(pl.cell_of) if i not in (ia, ib)}
            rows = [(ra, rb)]
            if comps[ia]['symbol'] in _PASSIVE and comps[ib]['symbol'] in _PASSIVE:
                rows.extend((r, r) for r in sorted({ra, rb}) if (r, r) != (ra, rb))
            targets = []
            for rr, ss in rows:
                for cc, dd in product(range(columns), repeat=2):
                    if deadline is not None and perf_counter() >= deadline:
                        return
                    if (rr, cc) == (ss, dd) or (rr, cc) in outside or (ss, dd) in outside:
                        continue
                    if (cc-ca)*(cb-ca) < 0 and abs(cb-ca) > 0:
                        continue
                    axis_error = abs(pl.column_x[cc] + pl.column_x[dd] - 2*row['axis_x'])
                    height_error = abs(pl.row_y[rr]-pl.row_y[ss])
                    targets.append((axis_error+height_error, abs(cc-ca)+abs(dd-cb), rr, ss, cc, dd))
            for _, _, rr, ss, cc, dd in sorted(targets)[:16]:
                cells = list(pl.cell_of)
                cells[ia], cells[ib] = (rr, cc), (ss, dd)
                yield cells, list(pl.orient), {'kind': 'paired_position', 'devices': [a, b]}


def regularize_symmetry(par, placement, comps, nets, index, metadata, *, options=None,
                        deadline=None, seconds=1.0, max_candidates=128, max_rounds=2):
    """Return an independent incumbent; accepted transactions strictly improve."""
    if type(seconds) not in (int, float) or not isfinite(seconds) or seconds < 0:
        raise ValueError('symmetry seconds must be finite and non-negative')
    if type(max_candidates) is not int or max_candidates < 0 or type(max_rounds) is not int or max_rounds < 0:
        raise ValueError('symmetry search budgets must be non-negative integers')
    from netlist2schematic.routing.routing_rules import RoutingOptions
    options = options or RoutingOptions()
    started = perf_counter()
    until = min(started+seconds, deadline) if deadline is not None else started+seconds
    pl, metadata = deepcopy(placement), deepcopy(metadata)
    report = report_symmetry(pl, comps, index, metadata, grid_step=options.grid_step)
    report.pop('reason', None)
    report.update(status='unchanged', actions=[], candidates=0, accepted=0,
                  before=deepcopy(report['current']), after=deepcopy(report['current']),
                  source_optimization_stage='after_sa_before_final_routing',
                  limits={'seconds': seconds, 'rounds': max_rounds,
                          'candidates': max_candidates, 'estimated_hpwl_ratio': 1.25})
    groups = report['relations']
    if not groups:
        report.update(status='skipped', stop_reason='no_high_confidence_relations')
    elif not all(hasattr(pl, attr) for attr in ('cell_of', 'orient', 'column_x', 'row_y')):
        report.update(status='skipped', stop_reason='unsupported_placement')
    elif perf_counter() >= until or max_candidates == 0 or max_rounds == 0:
        report.update(stop_reason='time_budget' if perf_counter() >= until else
                      'candidate_limit' if max_candidates == 0 else 'round_limit')
    else:
        original = deepcopy(pl)
        protected = {row.get('device') for row in metadata.get('role_adjustments', ())
                     if isinstance(row, dict) and row.get('device')}
        baseline_hpwl = _hpwl(par, pl, nets, index)
        current = report['current']
        stop = 'no_improvement'
        seen = {(tuple(pl.cell_of), tuple(pl.orient))}
        for _ in range(max_rounds):
            improved = False
            proposals = iter(_proposals(par, pl, comps, index, groups, protected, until))
            while True:
                if perf_counter() >= until:
                    stop = 'time_budget'
                    break
                if report['candidates'] >= max_candidates:
                    stop = 'candidate_limit'
                    break
                try:
                    cells, orientations, action = next(proposals)
                except StopIteration:
                    if perf_counter() >= until:
                        stop = 'time_budget'
                    break
                fingerprint = (tuple(cells), tuple(orientations))
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                report['candidates'] += 1
                trial = deepcopy(pl)
                _set_state(trial, cells, orientations)
                if not _valid_state(trial, original, comps, protected):
                    continue
                measured = measure_symmetry(trial, comps, index, groups, options.grid_step)
                if _key(measured) >= _key(current):
                    continue
                if _hpwl(par, trial, nets, index) > 1.25*baseline_hpwl + _EPS:
                    continue
                try:
                    safe = _static_safe(par, trial, nets, index, options, until)
                except Exception as exc:
                    from netlist2schematic.routing.orthogonal_router import RoutingFailure
                    if isinstance(exc, RoutingFailure) and perf_counter() >= until:
                        stop = 'time_budget'
                        break
                    raise
                if not safe:
                    continue
                action = dict(action, before=deepcopy(current), after=deepcopy(measured))
                action['changes'] = [{'device': comps[i]['id'], 'from': list(pl.cell_of[i]),
                    'to': list(trial.cell_of[i]), 'orientation_before': pl.orient[i],
                    'orientation_after': trial.orient[i]} for i in range(len(comps))
                    if pl.cell_of[i] != trial.cell_of[i] or pl.orient[i] != trial.orient[i]]
                pl, current = trial, measured
                report['actions'].append(action)
                report['accepted'] += 1
                improved = True
                # Every following transaction starts at the accepted state;
                # stale macro proposals must not undo another group's repair.
                proposals = iter(_proposals(par, pl, comps, index, groups, protected, until))
            if stop in ('time_budget', 'candidate_limit') or not improved:
                break
        else:
            stop = 'round_limit'
        report.update(status='improved' if report['accepted'] else 'unchanged',
                      stop_reason=stop, after=deepcopy(current), current=deepcopy(current),
                      estimated_hpwl_before=baseline_hpwl,
                      estimated_hpwl_after=_hpwl(par, pl, nets, index))
        metadata['layout_grid'] = {'column_x': list(pl.column_x), 'row_y': list(pl.row_y)}
    report['elapsed_seconds'] = perf_counter()-started
    metadata['layout_symmetry'] = report
    return pl, metadata
