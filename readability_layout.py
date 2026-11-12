"""Opt-in source-derived passive networks and comparable drawing measurements.

Junctions may contain several real terminals. They are soft geometric anchors,
never transitive device owners or claims of circuit function. This module does
not parse new syntax, alter levels, route wires, or enable another layout mode.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
from itertools import combinations
import json
from math import inf, isfinite
from statistics import median
from time import perf_counter

VERSION = 'source-readability-v1'
MAX_TRIALS = 128
SECONDS = 1.0
UNIT = 100.0
VIOLATION_WEIGHT = 1.25
EPS = 1e-8
_PASSIVE = frozenset(('resistor', 'capacitor', 'inductor', 'diode'))
_ACTIVE = frozenset(('MOS', 'BJT'))
_CONTROL = frozenset(('G', 'B', 'IN+', 'IN-'))
_CONDUCTION = frozenset(('D', 'S', 'C', 'E', 'OUT'))


def _rail(net):
    from netlist_core import SRC_ALIASES, SNK_ALIASES
    return str(net).casefold() in SRC_ALIASES | SNK_ALIASES


def _rows(row_of, comps, index):
    if isinstance(row_of, dict):
        return {name: int(row_of[name]) for name in index}
    return {comp['id']: int(row_of[i]) for i, comp in enumerate(comps)}


def _canonical_path(members, path):
    forward = (tuple(path), tuple(members))
    backward = (tuple(reversed(path)), tuple(reversed(members)))
    chosen = min(forward, backward)
    return list(chosen[1]), list(chosen[0])


def _source_records(devices, junctions):
    all_sources, accepted, rejected = [], [], []
    for name, dev in sorted(devices.items()):
        if dev.get('cls') not in ('ISRC', 'VSRC'):
            continue
        all_sources.append(dict(device=name, cls=dev['cls'], p=dev['p'], n=dev['n']))
        rail_pins = [(pin, dev[field]) for pin, field in (('+', 'p'), ('-', 'n')) if _rail(dev[field])]
        if len(rail_pins) != 1:
            rejected.append(dict(device=name, reason='source_requires_one_verified_rail'))
            continue
        rail_pin, rail_net = rail_pins[0]
        signal_pin = '-' if rail_pin == '+' else '+'
        net = dev['n' if signal_pin == '-' else 'p']
        junction = junctions.get(net, {})
        ports = junction.get('active_ports', [])
        source_ports = junction.get('source_ports', [])
        if len(source_ports) != 1:
            rejected.append(dict(device=name, reason='source_signal_node_not_exclusive'))
            continue
        kind, host_pair = None, None
        if dev['cls'] == 'ISRC':
            if (len(ports) == 2 and all(pin in ('S', 'E') for _, pin in ports)
                    and len({devices[member].get('type') for member, _ in ports}) == 1):
                members = [member for member, _ in ports]
                controls = [devices[member].get('g', devices[member].get('b')) for member in members]
                drains = [devices[member].get('d', devices[member].get('c')) for member in members]
                if len(set(controls)) == len(set(drains)) == 2:
                    kind, host_pair = 'tail_source', members
            references = []
            for member in sorted({member for member, _ in ports}):
                host = devices[member]
                if ((host.get('cls') == 'MOS' and host.get('d') == host.get('g') == net)
                        or (host.get('cls') == 'BJT' and host.get('c') == host.get('b') == net)):
                    references.append(member)
            if (kind is None and len(references) == 1 and all(
                    member == references[0] or pin in _CONTROL for member, pin in ports)):
                kind = 'diode_bias_source'
                ports = [port for port in ports if port[0] == references[0]]
        elif ports and all(pin in _CONTROL for _, pin in ports):
            kind = 'gate_voltage_source'
        if kind is None:
            rejected.append(dict(device=name, reason='source_host_roles_not_unique'))
            continue
        record = dict(id='readability:source:' + name, device=name, kind=kind,
            signal_pin=signal_pin, rail_pin=rail_pin, net=net, rail_net=rail_net,
            host_ports=deepcopy(ports), confidence='high')
        if host_pair:
            record['host_pair'] = host_pair
        accepted.append(record)
    return all_sources, accepted, rejected


def build_readability_plan(devices, comps, nets, index, structs, levels, row_of):
    """Compile terminal roles once; all candidates must measure this same plan."""
    if set(devices) != set(index) or any(index.get(row['id']) != i for i, row in enumerate(comps)):
        raise ValueError('readability inputs must cover the same component IDs')
    incidence, passive_at = defaultdict(list), defaultdict(list)
    all_passive = {name for name, dev in devices.items() if dev.get('cls') == 'PASSIVE'}
    passive = {name for name, dev in devices.items()
               if dev.get('cls') == 'PASSIVE' and dev.get('type') in ('R', 'L', 'C')}
    by_id = {row['id']: row for row in comps}
    for row in comps:
        for pin, net in row.get('nodes', {}).items():
            incidence[net].append([row['id'], pin])
            if row['id'] in passive:
                passive_at[net].append(row['id'])
    junctions = {}
    for net, ports in sorted(incidence.items()):
        active = sorted(port for port in ports if devices[port[0]].get('cls') in _ACTIVE
                        or devices[port[0]].get('cls') in ('VCVS', 'OPAMP')
                        and port[1] not in ('VDD', 'VSS'))
        source = sorted(port for port in ports if devices[port[0]].get('cls') in ('ISRC', 'VSRC'))
        conductive = [port for port in active if port[1] in _CONDUCTION]
        junctions[net] = dict(net=net, ports=deepcopy(active), active_ports=active,
            conduction_ports=conductive, control_ports=[port for port in active if port[1] in _CONTROL],
            source_ports=source, is_rail=_rail(net),
            role='rail' if _rail(net) else 'conduction' if conductive else 'control' if active else 'boundary')
    patterns, ambiguities, used, links = [], [], set(), {}

    def add(kind, members, *, path=(), endpoint_junctions=(), **extra):
        members = list(members)
        identifier = 'readability:' + kind + ':' + '|'.join(sorted(members))
        if any(row['id'] == identifier for row in patterns):
            return next(row for row in patterns if row['id'] == identifier)
        owners = [net for net in endpoint_junctions
                  if junctions.get(net, {}).get('active_ports') and not _rail(net)]
        row = dict(id=identifier, kind=kind, members=members, path=list(path),
            endpoint_junctions=list(endpoint_junctions), owner_junctions=owners,
            parent=None, children=[], **extra)
        patterns.append(row)
        used.update(members)
        return row

    # Exact equal endpoints establish a bank regardless of its instance names,
    # or whether its non-rail junction has one or several conduction terminals.
    banks = defaultdict(list)
    for name in sorted(passive):
        dev = devices[name]
        if dev['a'] != dev['b']:
            banks[tuple(sorted((dev['a'], dev['b'])))].append(name)
    bank_members = set()
    for ends, names in sorted(banks.items()):
        if len(names) >= 2:
            add('parallel_bank', names, endpoint_junctions=ends, nets=list(ends))
            bank_members.update(names)

    def internal(net):
        return (not _rail(net) and len(passive_at[net]) == 2
                and len(incidence[net]) == 2 and len(set(passive_at[net])) == 2)

    # Maximal paths have internal degree exactly two in the entire visible
    # graph. A control tap, a source or another passive makes a real boundary.
    walked, paths = set(), []
    boundaries = sorted(net for net in passive_at if not internal(net))
    for start in boundaries:
        for first in sorted(passive_at[start]):
            if first in walked:
                continue
            ordered, path, current, name = [], [start], start, first
            while name not in walked:
                walked.add(name)
                ordered.append(name)
                dev = devices[name]
                following = dev['b'] if dev['a'] == current else dev['a']
                path.append(following)
                if not internal(following):
                    break
                name = next(item for item in passive_at[following] if item != name)
                current = following
            if path[0] == path[-1]:
                if not set(ordered) <= bank_members:
                    ambiguities.append(dict(kind='series_chain', members=sorted(ordered), reason='closed_passive_path'))
                continue
            ordered, path = _canonical_path(ordered, path)
            paths.append(dict(members=ordered, path=path))
    for name in sorted(passive - walked - bank_members):
        ambiguities.append(dict(kind='series_chain', members=[name], reason='unanchored_passive_cycle'))
    chain_records = {}
    for path in paths:
        if len(path['members']) >= 2:
            record = add('series_chain', path['members'], path=path['path'],
                         endpoint_junctions=(path['path'][0], path['path'][-1]))
            chain_records[tuple(path['members'])] = record
    parallel_paths = defaultdict(list)
    for path in paths:
        ends = path['path'][0], path['path'][-1]
        parallel_paths[ends].append(path)
    for ends, alternatives in sorted(parallel_paths.items()):
        if len(alternatives) < 2 or not any(len(row['members']) > 1 for row in alternatives):
            continue
        members = [name for row in alternatives for name in row['members']]
        if len(set(members)) != len(members):
            ambiguities.append(dict(kind='multi_path_bridge', members=sorted(set(members)), reason='shared_path_interior'))
            continue
        record = add('multi_path_bridge', members, endpoint_junctions=ends, paths=deepcopy(alternatives))
        for alternative in alternatives:
            child = chain_records.get(tuple(alternative['members']))
            if child:
                child['parent'] = record['id']
                record['children'].append(child['id'])

    # Two equal resistor arms into a gate-only node. This is a drawing role,
    # not a CMFB/function inference and not a union of the controlled devices.
    for net, junction in sorted(junctions.items()):
        if _rail(net) or not junction['control_ports'] or junction['conduction_ports'] or junction['source_ports']:
            continue
        arms = sorted(name for name in passive_at[net] if devices[name]['type'] == 'R')
        if len(arms) != 2:
            continue
        a, b = (devices[name] for name in arms)
        ends = [dev['b'] if dev['a'] == net else dev['a'] for dev in (a, b)]
        if (a.get('value') != b.get('value') or a.get('value') is None or len(set(ends)) != 2
                or not all(junctions.get(end, {}).get('conduction_ports') for end in ends)):
            continue
        add('two_arm_join', arms, path=(ends[0], net, ends[1]),
            endpoint_junctions=ends, center_junction=net, nets=[*ends, net])

    # A singleton is attached only by an actual terminal at a junction. Rail
    # names do not turn all loads on that rail into a common owner.
    for name in sorted(all_passive - used):
        dev = devices[name]
        ends = [dev['a'], dev['b']]
        anchored = [net for net in ends if junctions.get(net, {}).get('active_ports') and not _rail(net)]
        if anchored:
            add('single_attachment', [name], path=ends, endpoint_junctions=ends,
                nets=ends, role='bridge' if len(anchored) == 2 else 'single_end')

    # Reuse only the existing unique R/L conduction proof. No old relation
    # list means no symmetry, mirror row cohorts or exclusive-tail rewriting.
    original_rows = _rows(row_of, comps, index)
    display_rows = dict(original_rows)
    from branch_levels import plan_branch_rows
    _, row_plan = plan_branch_rows(devices, levels, [original_rows[row['id']] for row in comps], [], comps, index)
    valid_bridges = row_plan.get('bridge_alignment', ())
    series_axes = []
    if valid_bridges:
        display_rows = dict(row_plan['display_rows'])
        for bridge in valid_bridges:
            for connection in bridge.get('connections', ()):
                first, second = connection['pins']
                series_axes.append(dict(a=first[0], pin_a=first[1], b=second[0], pin_b=second[1],
                    net=connection['net'], weight=2.5, source='unique_rl_continuation'))
            matches = [row for row in patterns if set(row['members']) == set(bridge['members'])]
            if not matches:
                matches = [add('series_chain', bridge['members'], path=bridge['path'],
                    endpoint_junctions=(bridge['path'][0], bridge['path'][-1]))]
            for row in matches:
                row['row_mode'] = 'conduction_bridge'
                row['bridge_alignment'] = deepcopy(bridge)

    for pattern in patterns:
        paths_to_link = pattern.get('paths') if pattern['kind'] == 'multi_path_bridge' else (
            [dict(members=pattern['members'], path=pattern['path'])] if pattern.get('path')
            and len(pattern['path']) == len(pattern['members']) + 1 else [])
        if pattern['kind'] == 'parallel_bank':
            paths_to_link = [dict(members=[name], path=pattern['nets']) for name in pattern['members']]
        for path in paths_to_link:
            for name, net in ((path['members'][0], path['path'][0]), (path['members'][-1], path['path'][-1])):
                if not junctions.get(net, {}).get('active_ports') or _rail(net):
                    continue
                pins = [pin for pin, node in by_id[name]['nodes'].items() if node == net]
                if len(pins) == 1:
                    links[(name, pins[0], net)] = dict(member=name, pin=pins[0], net=net,
                        junction=net, kind=pattern['kind'])
        if pattern['kind'] == 'two_arm_join':
            for name in pattern['members']:
                for pin, net in by_id[name]['nodes'].items():
                    if junctions.get(net, {}).get('active_ports'):
                        links[(name, pin, net)] = dict(member=name, pin=pin, net=net, junction=net, kind='two_arm_join')

    source_devices, sources, rejected_sources = _source_records(devices, junctions)
    # Connected signal components are compactness contexts only, never rigid
    # blocks. Removing rails prevents unrelated circuits from fusing here.
    parent = {name: name for name in devices}
    def find(name):
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name
    for net, ports in sorted(incidence.items()):
        names = sorted({name for name, _ in ports})
        if _rail(net) or not names:
            continue
        first = find(names[0])
        for name in names[1:]:
            parent[find(name)] = first
    groups = defaultdict(list)
    for name in sorted(devices):
        groups[find(name)].append(name)
    compact_groups = [dict(id='readability:context:' + '|'.join(names), members=names)
                      for names in sorted(groups.values()) if len(names) > 1]
    covered = sorted({name for row in patterns for name in row['members']})
    plan = dict(version=VERSION, supported=bool(patterns or sources or valid_bridges),
        patterns=patterns, junctions=junctions, links=list(links.values()), sources=sources,
        source_devices=source_devices, rejected_sources=rejected_sources,
        series_axes=series_axes, compact_groups=compact_groups,
        branch_row_plan=deepcopy(row_plan) if valid_bridges else None,
        original_rows=original_rows, display_rows=display_rows, ambiguities=ambiguities,
        coverage=dict(passive_devices=len(all_passive), recognized_passives=len(covered),
            unassigned_passives=sorted(all_passive - set(covered)), pattern_count=len(patterns),
            source_devices=len(source_devices), recognized_sources=len(sources),
            unassigned_sources=sorted(set(row['device'] for row in source_devices) - set(row['device'] for row in sources)),
            junction_anchors=sum(bool(row['active_ports']) and not row['is_rail'] for row in junctions.values()),
            series_axes=len(series_axes), ambiguity_count=len(ambiguities)))
    plan['signature'] = sha256(json.dumps(dict(plan=plan, devices=devices), sort_keys=True,
        separators=(',', ':'), default=str).encode()).hexdigest()
    return plan


def _point(pl, index, endpoint):
    values = tuple(pl.pin_world(index[endpoint[0]], endpoint[1]))
    if len(values) != 2 or not all(isfinite(value) for value in values):
        raise ValueError('invalid physical endpoint')
    return values


def junction_point(pl, index, plan, net):
    """Median of real conductive pins, or controls at a control-only junction."""
    row = plan.get('junctions', {}).get(net, {})
    if row.get('is_rail'):
        return None
    ports = row.get('conduction_ports') or row.get('control_ports') or ()
    if not ports:
        return None
    points = [_point(pl, index, port) for port in ports]
    return median(point[0] for point in points), median(point[1] for point in points)


def _component_pin(comps, index, name, net):
    pins = [pin for pin, value in comps[index[name]]['nodes'].items() if value == net]
    if len(pins) != 1:
        raise ValueError('ambiguous passive endpoint')
    return pins[0]


def _axis_direction(pl, index, plan, ends, axis):
    points = [junction_point(pl, index, plan, net) for net in ends]
    if len(points) == 2 and all(point is not None for point in points):
        difference = points[1][axis] - points[0][axis]
        return 1 if difference > EPS else -1 if difference < -EPS else 0
    return 0


def _chain_shape(pl, comps, index, plan, pattern):
    names, path = pattern['members'], pattern['path']
    centers = [pl.pos_of(index[name]) for name in names]
    candidates = []
    axes = (1,) if pattern.get('row_mode') == 'conduction_bridge' else (0, 1)
    for axis in axes:
        perpendicular = 1-axis
        orientation = 0
        for name in names:
            pins = list(comps[index[name]]['nodes'])
            a, b = (_point(pl, index, (name, pin)) for pin in pins[:2])
            orientation += abs(a[perpendicular]-b[perpendicular]) > EPS
        steps = [b[axis]-a[axis] for a, b in zip(centers, centers[1:])]
        direction = _axis_direction(pl, index, plan, (path[0], path[-1]), axis)
        order = (sum(step*direction <= EPS for step in steps) if direction else
                 min(sum(step <= EPS for step in steps), sum(step >= -EPS for step in steps)))
        alignment = (max(point[perpendicular] for point in centers)-min(point[perpendicular] for point in centers))/UNIT
        for first, second, net in zip(names, names[1:], path[1:-1]):
            pa = _point(pl, index, (first, _component_pin(comps, index, first, net)))
            pb = _point(pl, index, (second, _component_pin(comps, index, second, net)))
            alignment += abs(pa[perpendicular]-pb[perpendicular])/UNIT
        projected = [point[axis] for point in centers]
        grid = getattr(pl, 'column_x' if axis == 0 else 'row_y', ())
        pitch = median(b-a for a, b in zip(grid, grid[1:])) if len(grid) > 1 else UNIT
        spread = max(0, max(projected)-min(projected)-pitch*max(0, len(names)-1))/UNIT
        candidates.append(dict(axis=axis, orientation_errors=orientation, order_errors=order,
                               shape_error=alignment+spread))
    return min(candidates, key=lambda row: (VIOLATION_WEIGHT*(row['orientation_errors']+row['order_errors'])+row['shape_error'], row['axis']))


def _bank_shape(pl, comps, index, pattern):
    # Reuse the existing endpoint/spacing bank geometry without installing its
    # attachment_model or enabling its old structural path.
    from attachment_placement import measure_parallel_coherence
    group = dict(id=pattern['id'], kind='parallel_load', members=pattern['members'],
                 rail=pattern['nets'][0], node=pattern['nets'][1])
    record = measure_parallel_coherence(pl, index, [group])['groups'][0]
    candidates, names = [], pattern['members']
    points = [pl.pos_of(index[name]) for name in names]
    for axis, key in ((0, 'horizontal'), (1, 'vertical')):
        wrong = 0
        for name in names:
            pins = list(comps[index[name]]['nodes'])
            a, b = (_point(pl, index, (name, pin)) for pin in pins[:2])
            wrong += abs(a[axis]-b[axis]) > EPS
        # The reused coherence includes a fractional orientation penalty; use
        # the explicit 1.25 violation coefficient once in the new objective.
        shape = max(0.0, record[key]-wrong/len(names))
        candidates.append(dict(axis=axis, orientation_errors=wrong, order_errors=0,
                               shape_error=shape, legacy_coherence=record['coherence']))
    return min(candidates, key=lambda row: (VIOLATION_WEIGHT*row['orientation_errors']+row['shape_error'], row['axis']))


def measure_readability(pl, comps, index, plan):
    """Measure the source plan, including unknown coverage, without mutation."""
    coverage = deepcopy(plan.get('coverage', {}))
    metrics = dict(version=VERSION, plan_signature=plan.get('signature'), supported=bool(plan.get('supported')),
        passive_supported=bool(plan.get('patterns') or plan.get('series_axes')),
        source_supported=bool(plan.get('sources')),
        measurable=False, coverage=coverage, ambiguity_count=len(plan.get('ambiguities', ())),
        violations=None, orientation_errors=None, order_errors=None, interleaving=None,
        shape_error=None, owner_locality=None, source_locality=None, compactness=None,
        pin_axis_error=None, patterns=[], sources=[])
    if not metrics['supported']:
        return metrics
    orientation, order, interleaving, shape, axis_error = 0, 0, 0, 0.0, 0.0
    missing, records = [], []
    try:
        for name in index:
            point = tuple(pl.pos_of(index[name]))
            if len(point) != 2 or not all(isfinite(value) for value in point):
                raise ValueError('invalid component position')
        for pattern in plan.get('patterns', ()):
            kind, names = pattern['kind'], pattern['members']
            if any(name not in index for name in names):
                missing.append(pattern['id'])
                continue
            detail = dict(id=pattern['id'], kind=kind, members=list(names), orientation_errors=0,
                          order_errors=0, shape_error=0.0, interleaving=0)
            if kind == 'parallel_bank':
                detail.update(_bank_shape(pl, comps, index, pattern))
            elif kind == 'series_chain':
                detail.update(_chain_shape(pl, comps, index, plan, pattern))
            elif kind == 'two_arm_join':
                # Arms meet at one control node and remain alongside the two
                # actual output junctions, with no forced functional sign.
                ends = [junction_point(pl, index, plan, net) for net in pattern['endpoint_junctions']]
                center = junction_point(pl, index, plan, pattern['center_junction'])
                points = [pl.pos_of(index[name]) for name in names]
                if all(point is not None for point in ends) and center is not None:
                    axis = 0 if abs(ends[1][0]-ends[0][0]) >= abs(ends[1][1]-ends[0][1]) else 1
                    detail['axis'] = axis
                    detail['shape_error'] = abs(points[0][1-axis]-points[1][1-axis])/UNIT
                    if (ends[1][axis]-ends[0][axis])*(points[1][axis]-points[0][axis]) <= EPS:
                        detail['order_errors'] += 1
                    for name in names:
                        pins = list(comps[index[name]]['nodes'])
                        a, b = (_point(pl, index, (name, pin)) for pin in pins[:2])
                        detail['orientation_errors'] += abs(a[1-axis]-b[1-axis]) > EPS
            elif kind == 'multi_path_bridge':
                ends = [junction_point(pl, index, plan, net) for net in pattern['endpoint_junctions']]
                if all(point is not None for point in ends):
                    axis = 0 if abs(ends[1][0]-ends[0][0]) >= abs(ends[1][1]-ends[0][1]) else 1
                    lo, hi = sorted(point[axis] for point in ends)
                    detail['axis'] = axis
                    detail['shape_error'] = sum(max(0, lo-pl.pos_of(index[name])[axis],
                        pl.pos_of(index[name])[axis]-hi) for name in names)/(UNIT*len(names))
            # Intrusion is assessed on one stripe only, not a parent rectangle
            # that would forbid legitimate nested paths and nearby branches.
            if kind in ('parallel_bank', 'series_chain') and len(names) > 1:
                axis = detail.get('axis', 0)
                points = [pl.pos_of(index[name]) for name in names]
                lo, hi = min(point[axis] for point in points), max(point[axis] for point in points)
                band = median(point[1-axis] for point in points)
                detail['interleaving'] = sum(name not in names and lo < pl.pos_of(i)[axis] < hi
                    and abs(pl.pos_of(i)[1-axis]-band) < UNIT*.4 for name, i in index.items())
            orientation += detail['orientation_errors']
            order += detail['order_errors']
            interleaving += detail['interleaving']
            shape += detail['shape_error']
            records.append(detail)
        distances = []
        for link in plan.get('links', ()):
            target = junction_point(pl, index, plan, link['junction'])
            if target is not None:
                point = _point(pl, index, (link['member'], link['pin']))
                distances.append((abs(point[0]-target[0])+abs(point[1]-target[1]))/UNIT)
        source_distances, source_records = [], []
        for source in plan.get('sources', ()):
            hosts = [_point(pl, index, endpoint) for endpoint in source['host_ports']]
            point = _point(pl, index, (source['device'], source['signal_pin']))
            target = median(p[0] for p in hosts), median(p[1] for p in hosts)
            distance = (abs(point[0]-target[0])+abs(point[1]-target[1]))/UNIT
            source_distances.append(distance)
            source_records.append(dict(device=source['device'], kind=source['kind'], locality=distance))
        for axis in plan.get('series_axes', ()):
            a = _point(pl, index, (axis['a'], axis['pin_a']))
            b = _point(pl, index, (axis['b'], axis['pin_b']))
            axis_error += axis.get('weight', 1.0)*abs(a[0]-b[0])/UNIT
            order += a[1] >= b[1]
        spans = []
        for group in plan.get('compact_groups', ()):
            points = [pl.pos_of(index[name]) for name in group['members']]
            xs, ys = zip(*points)
            width, height = max(xs)-min(xs), max(ys)-min(ys)
            spans.append((width+height)/UNIT/max(1, len(points)-1)
                         + .1*width*height/(UNIT*UNIT*max(1, len(points))))
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
        coverage['measurement_error'] = type(error).__name__ + ': ' + str(error)
        return metrics
    coverage.update(measured_patterns=len(records), missing_patterns=missing,
        measured_owner_links=len(distances), planned_owner_links=len(plan.get('links', ())),
        measured_sources=len(source_distances), planned_sources=len(plan.get('sources', ())))
    metrics.update(measurable=not missing, violations=orientation+order+interleaving,
        orientation_errors=orientation, order_errors=order, interleaving=interleaving,
        shape_error=shape/max(1, len(records)), owner_locality=sum(distances)/len(distances) if distances else None,
        source_locality=sum(source_distances)/len(source_distances) if source_distances else None,
        compactness=sum(spans)/len(spans) if spans else None, pin_axis_error=axis_error,
        patterns=records, sources=source_records)
    if not metrics['passive_supported']:
        for field in ('violations', 'orientation_errors', 'order_errors', 'interleaving',
                      'shape_error', 'owner_locality', 'pin_axis_error'):
            metrics[field] = None
    return metrics


def has_strong_passive_plan(plan):
    """Whether topology warrants changing initialization and the SA objective.

    A lone attachment only identifies an endpoint to measure. Sources likewise
    stay available to local refinement without changing the global search.
    """
    if not isinstance(plan, dict):
        return False
    if plan.get('series_axes'):
        return True
    strong_kinds = {'parallel_bank', 'series_chain', 'multi_path_bridge', 'two_arm_join'}
    return any(pattern.get('kind') in strong_kinds
               and len(set(pattern.get('members', ()))) >= 2
               for pattern in plan.get('patterns', ()))


def readability_rank(metrics):
    if not metrics.get('supported') or not metrics.get('measurable'):
        return (inf, inf, inf, inf)
    if not metrics.get('passive_supported', True):
        return (0, 0, metrics.get('source_locality') or 0.0, metrics.get('compactness') or 0.0)
    required = ('violations', 'shape_error', 'pin_axis_error')
    if any(not isinstance(metrics.get(key), (int, float)) or not isfinite(metrics[key]) for key in required):
        return (inf, inf, inf, inf)
    return (metrics['violations'], metrics['shape_error']+metrics['pin_axis_error'],
            (metrics.get('owner_locality') or 0.0)+(metrics.get('source_locality') or 0.0),
            metrics.get('compactness') or 0.0)


def score_readability(pl, comps, index, plan, *, phase=2):
    metrics = measure_readability(pl, comps, index, plan)
    if not metrics.get('measurable'):
        return dict(metrics=metrics, weighted_total=0.0, applied=False, reason='unknown_measurement')
    value = ((VIOLATION_WEIGHT*metrics['violations']+metrics['shape_error']
              + metrics['pin_axis_error']+(metrics.get('owner_locality') or 0.0)
              + .15*(metrics.get('compactness') or 0.0)) if metrics['passive_supported'] else 0.0)
    if phase >= 3:
        value += metrics.get('source_locality') or 0.0
    return dict(metrics=metrics, weighted_total=value, applied=True,
                weights=dict(violations=VIOLATION_WEIGHT, shape=1.0, pin_axis=1.0,
                             owner_locality=1.0, compactness=.15, source_locality=int(phase >= 3)))


def _hpwl(pl, nets, index):
    total = 0.0
    for net, endpoints in nets.items():
        if _rail(net) or len(endpoints) < 2:
            continue
        points = [_point(pl, index, endpoint) for endpoint in endpoints]
        xs, ys = zip(*points)
        total += max(xs)-min(xs)+max(ys)-min(ys)
    return total


def _targets(pl, index, plan, pattern, *, deadline=None):
    names, kind = pattern['members'], pattern['kind']
    anchors = [junction_point(pl, index, plan, net) for net in pattern['owner_junctions']]
    anchors = [point for point in anchors if point is not None]
    centers = [pl.pos_of(index[name]) for name in names]
    center = (median(point[0] for point in anchors or centers), median(point[1] for point in anchors or centers))
    cols, rows = len(pl.column_x), len(pl.row_y)
    candidates = []
    for axis in (0, 1):
        if pattern.get('row_mode') == 'conduction_bridge' and axis != 1:
            continue
        if kind == 'multi_path_bridge':
            paths = sorted(pattern['paths'], key=lambda row: (-len(row['members']), tuple(row['members'])))
            width, depth = max(len(row['members']) for row in paths), len(paths)
        else:
            width, depth = len(names), 1
        row_choices = sorted(range(rows), key=lambda row: (abs(pl.row_y[row]-center[1]), row))
        col_choices = sorted(range(cols), key=lambda col: (abs(pl.column_x[col]-center[0]), col))
        for row in row_choices:
            for col in col_choices:
                if deadline is not None and perf_counter() >= deadline:
                    return
                if len(candidates) >= MAX_TRIALS*4:
                    break
                mapping = {}
                if pattern.get('row_mode') == 'conduction_bridge':
                    mapping = {name: (plan['display_rows'][name], col) for name in names}
                elif kind == 'multi_path_bridge':
                    for offset, path in enumerate(paths):
                        inset = (width-len(path['members']))//2
                        members = path['members']
                        direction = _axis_direction(pl, index, plan, (path['path'][0], path['path'][-1]), axis)
                        if direction < 0:
                            members = list(reversed(members))
                        for step, name in enumerate(members):
                            mapping[name] = (row+offset, col+inset+step) if axis == 0 else (row+inset+step, col+offset)
                else:
                    ordered = names
                    if pattern.get('path') and _axis_direction(pl, index, plan,
                            (pattern['path'][0], pattern['path'][-1]), axis) < 0:
                        ordered = list(reversed(names))
                    for step, name in enumerate(ordered):
                        mapping[name] = (row, col+step) if axis == 0 else (row+step, col)
                cells = [mapping[name] for name in names]
                if any(not (0 <= r < rows and 0 <= c < cols) for r, c in cells):
                    continue
                distance = sum(abs(pl.column_x[c]-center[0])+abs(pl.row_y[r]-center[1]) for r, c in cells)/len(cells)
                # A bank's member axes are perpendicular to the bank itself;
                # series paths, arms and bridges run along their member axes.
                horizontal_symbol = axis == (1 if kind == 'parallel_bank' else 0)
                candidates.append((distance, tuple(cells), horizontal_symbol))
            if len(candidates) >= MAX_TRIALS*4:
                break
    seen = set()
    for _, cells, horizontal in sorted(candidates):
        key = cells, horizontal
        if key not in seen:
            seen.add(key)
            yield list(cells), horizontal


def prepare_readability_placement(pl, devices, comps, nets, index, plan, *, deadline=None,
                                   max_trials=MAX_TRIALS, seconds=SECONDS, estimate=None):
    """Try bounded, non-rigid passive transactions; timeout restores the input.

    The common default estimator is HPWL plus the same readability objective
    for old and new states. A caller may supply a broader common estimator.
    """
    if type(max_trials) is not int or not 0 <= max_trials <= MAX_TRIALS:
        raise ValueError('readability initializer requires 0..128 trials')
    if type(seconds) not in (int, float) or not isfinite(seconds) or not 0 <= seconds <= SECONDS:
        raise ValueError('readability initializer requires 0..1 seconds')
    started = perf_counter()
    until = min(started+seconds, deadline) if deadline is not None else started+seconds
    strong = has_strong_passive_plan(plan)
    report = dict(version=VERSION, status='skipped', applied=False, actions=[], trials=0,
        max_trials=max_trials, seconds_limit=seconds, common_estimator='provided' if estimate else 'hpwl_plus_readability',
        hpwl_ratio_limit=1.15, plan_signature=plan.get('signature'), sa_enabled=strong)
    if not plan.get('supported'):
        report['reason'] = 'no_supported_readability_relations'
        return pl, report
    if strong and (perf_counter() >= until or max_trials == 0):
        report['reason'] = 'initialization_budget'
        return pl, report
    clone = deepcopy(pl)
    clone.readability_plan, clone.readability_index = deepcopy(plan), dict(index)
    clone.readability_phase = 2
    clone.readability_sa_enabled = strong
    before = measure_readability(clone, comps, index, plan)
    report['before'] = deepcopy(before)
    if not strong:
        report.update(status='report_only', reason='weak_relations_report_only',
            after=deepcopy(before), elapsed_seconds=perf_counter()-started)
        clone.readability_initialization = deepcopy(report)
        return clone, report
    if not before.get('measurable'):
        report['reason'] = 'unknown_measurement'
        return pl, report
    common = estimate if estimate is not None else lambda placement: _hpwl(placement, nets, index)/UNIT + score_readability(placement, comps, index, plan)['weighted_total']
    initial_cost, initial_hpwl = common(clone), _hpwl(clone, nets, index)
    current, current_cost = before, initial_cost
    report.update(common_estimate_before=initial_cost, estimated_hpwl_before=initial_hpwl)
    from attachment_placement import _available, _legal, _orientations
    fixed = {index[value] if isinstance(value, str) and value in index else value
             for value in set(getattr(pl, 'fixed', ())) | set(getattr(pl, 'fixed_cells', {}))}
    patterns = sorted(plan.get('patterns', ()), key=lambda row: (
        {'multi_path_bridge': 0, 'parallel_bank': 1, 'series_chain': 2, 'two_arm_join': 3, 'single_attachment': 4}[row['kind']], row['id']))
    allowance = max(1, max_trials//max(1, len(patterns)))
    prepared_members = set()
    if getattr(clone, 'passive_policy', None) == 'flexible':
        for pattern in patterns:
            names = pattern['members']
            ids = {index[name] for name in names}
            if ids & fixed or set(names) & prepared_members:
                continue
            saved = list(clone.cell_of), list(clone.orient)
            best, best_metrics, best_cost, local_trials = None, None, current_cost, 0
            for targets, horizontal in _targets(clone, index, plan, pattern, deadline=until):
                if perf_counter() >= until or report['trials'] >= max_trials or local_trials >= allowance:
                    break
                if not _available(clone, names, targets, index):
                    continue
                local_trials += 1
                report['trials'] += 1
                cells, orientations = list(saved[0]), _orientations(clone, names, index, horizontal)
                for name, target in zip(names, targets):
                    cells[index[name]] = target
                clone.set_state(cells, orientations)
                if _legal(clone, pl, comps, fixed):
                    measured = measure_readability(clone, comps, index, plan)
                    hpwl = _hpwl(clone, nets, index)
                    cost = common(clone)
                    if (readability_rank(measured) < readability_rank(current)
                            and hpwl <= initial_hpwl*1.15+EPS and cost <= current_cost+EPS
                            and (best is None or (readability_rank(measured), cost) < (readability_rank(best_metrics), best_cost))):
                        best, best_metrics, best_cost = (list(cells), list(orientations)), measured, cost
                clone.set_state(*saved)
            if best:
                clone.set_state(*best)
                current, current_cost = best_metrics, best_cost
                prepared_members.update(names)
                report['actions'].append(dict(pattern=pattern['id'], members=list(names),
                    changes=[dict(device=name, before=list(saved[0][index[name]]), after=list(clone.cell_of[index[name]]),
                        orientation_before=saved[1][index[name]], orientation_after=clone.orient[index[name]])
                        for name in names if saved[0][index[name]] != clone.cell_of[index[name]]
                        or saved[1][index[name]] != clone.orient[index[name]]]))
    if perf_counter() >= until:
        report.update(reason='initialization_budget', status='rolled_back', actions=[],
                      after=deepcopy(before), elapsed_seconds=perf_counter()-started)
        return pl, report
    report.update(status='prepared' if report['actions'] else 'unchanged',
        applied=bool(report['actions']), reason='readability_seed' if report['actions'] else 'no_safe_readability_gain',
        after=deepcopy(current), common_estimate_after=current_cost,
        estimated_hpwl_after=_hpwl(clone, nets, index), elapsed_seconds=perf_counter()-started)
    clone.readability_initialization = deepcopy(report)
    return clone, report
