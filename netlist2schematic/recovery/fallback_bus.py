"""Deterministic, spacious orthogonal recovery without SA or global A*.

Every device owns a vertical slab, every visible terminal owns a private upward
lane, and every multi-terminal signal net owns a horizontal bus.  Power markers
use explicit, saved connection paths and their actual symbol pin anchors.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass
from math import ceil, floor, isfinite
from pathlib import Path
import random
from time import perf_counter

from netlist2schematic.recognition import device_leveling as dl
from netlist2schematic.netlist.netlist_core import parse_netlist, to_document
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.common.sa_common import load_local

VERSION = 'constructive-bus-v1'


def _check_deadline(deadline, stage):
    if deadline is not None:
        if type(deadline) not in (int, float) or not isfinite(deadline):
            raise ValueError('The fallback deadline must be a finite absolute time')
        if perf_counter() >= deadline:
            from netlist2schematic.routing.orthogonal_router import RoutingFailure
            raise RoutingFailure('Constructive fallback stopped at its time budget',
                                 {'router': VERSION, 'reason': 'quality_time_budget',
                                  'stage': stage})


def _invalid(message, **details):
    from netlist2schematic.routing.sa_routing import RoutingValidationError
    raise RoutingValidationError(message, {'router': VERSION, 'reason': 'fallback_geometry',
                                           **details})


@dataclass(frozen=True)
class _Port:
    component: int
    component_id: str
    pin: str
    net: str
    point: tuple
    escape: tuple
    direction: tuple


def _ports(par, placement, comps, nets, index, options, deadline):
    ports = []
    for net, members in sorted(nets.items()):
        _check_deadline(deadline, 'pin_geometry')
        for name, pin in sorted(members, key=lambda member: (member[0].casefold(), member[1])):
            i = index[name]
            cx, cy = placement.pos_of(i)
            box = par.GEO_DOC[comps[i]['symbol']]['orientations'][placement.orient[i]]['viewBoxAABB']
            rect = (cx + box['x'] - options.halo, cy + box['y'] - options.halo,
                    cx + box['x'] + box['width'] + options.halo,
                    cy + box['y'] + box['height'] + options.halo)
            point = tuple(placement.pin_world(i, pin))
            direction = tuple(par.DIR_VEC[placement.pin_dir(i, pin)])
            step = options.grid_step
            if direction == (1, 0):
                escape = (max(point[0], rect[2] + step), point[1])
            elif direction == (-1, 0):
                escape = (min(point[0], rect[0] - step), point[1])
            elif direction == (0, 1):
                escape = (point[0], max(point[1], rect[3] + step))
            elif direction == (0, -1):
                escape = (point[0], min(point[1], rect[1] - step))
            else:
                _invalid('A fallback pin has an unsupported direction',
                         component_id=name, pin=pin, direction=direction)
            ports.append(_Port(i, name, pin, net, point, escape, direction))
    return ports


def _pitch(par, comps, options):
    """Reserve rule clearances and complete symbol/marker envelopes."""
    step = options.grid_step
    required = max(32.0, options.center_spacing + step, options.cross_clearance + step)
    for comp in comps:
        box = par.GEO_DOC[comp['symbol']]['orientations'][0]['viewBoxAABB']
        half_width = max(-box['x'], box['x'] + box['width'])
        half_height = max(-box['y'], box['y'] + box['height'])
        required = max(required, (half_width + options.halo + step) / 2,
                       (half_height + options.halo + step) / 2)
    for symbol in ('vdd-port', 'ground'):
        geometry = par.GEO_DOC[symbol]['orientations'][0]
        pin = geometry['pins'][0]['at']
        box = geometry['viewBoxAABB']
        horizontal = max(abs(box['x'] - pin['x']),
                         abs(box['x'] + box['width'] - pin['x']))
        top, bottom = box['y'] - pin['y'], box['y'] + box['height'] - pin['y']
        approach = max(0, bottom) if symbol == 'vdd-port' else max(0, -top)
        required = max(required, horizontal + options.halo + step,
                       approach + options.halo + step,
                       (max(abs(top), abs(bottom)) + options.halo + step) / 3)
    return ceil(required / step) * step


def _lanes(placement, ports, pitch, deadline):
    groups = defaultdict(list)
    for port in ports:
        groups[(port.component, port.direction)].append(port)
    lanes = {}
    for (component, direction), members in sorted(groups.items()):
        _check_deadline(deadline, 'private_lanes')
        members.sort(key=lambda port: (port.point[1], port.point[0], port.pin))
        # The supported canonical symbols have one N/S pin, at most two W/E
        # pins.  A different saved orientation is checked by final validation.
        for rank, port in enumerate(members):
            factor = (2 + rank) if direction in ((0, -1), (1, 0)) else (3 + rank)
            sign = -1 if direction in ((0, -1), (-1, 0)) else 1
            lanes[(component, port.pin)] = placement.pos_of(component)[0] + sign * factor * 2 * pitch
    values = sorted(lanes.values())
    if any(b - a < 2 * pitch - 1e-8 for a, b in zip(values, values[1:])):
        _invalid('The saved fallback placement does not provide distinct terminal lanes',
                 stage='private_lanes')
    return lanes


def _body_top(par, placement, comps, halo):
    return min(placement.pos_of(i)[1]
               + par.GEO_DOC[comp['symbol']]['orientations'][placement.orient[i]]['viewBoxAABB']['y']
               - halo for i, comp in enumerate(comps))


def _power_connections(par, ports, lanes, power_y, pitch, deadline):
    connections = []
    for port in ports:
        symbol = par.power_marker_sym(port.net)
        if symbol is None:
            continue
        _check_deadline(deadline, 'power_connections')
        lane = lanes[(port.component, port.pin)]
        anchor = (lane + pitch, power_y + (pitch if symbol == 'ground' else -pitch))
        points = [port.point, port.escape, (lane, port.escape[1]),
                  (lane, power_y), (anchor[0], power_y), anchor]
        segments = [[list(a), list(b)] for a, b in zip(points, points[1:]) if a != b]
        connections.append({'component_id': port.component_id, 'pin': port.pin,
                            'net': port.net, 'symbol': symbol,
                            'anchor': list(anchor), 'segments': segments})
    return connections


def _bus_ceiling(par, placement, comps, ports, connections, options, pitch):
    coordinates = [_body_top(par, placement, comps, options.halo)]
    coordinates.extend(port.escape[1] for port in ports)
    for connection in connections:
        geometry = par.GEO_DOC[connection['symbol']]['orientations'][0]
        local_pin = geometry['pins'][0]['at']
        origin_y = connection['anchor'][1] - local_pin['y']
        coordinates.append(origin_y + geometry['viewBoxAABB']['y'] - options.halo)
        coordinates.extend(point[1] for segment in connection['segments'] for point in segment)
    return floor((min(coordinates) - 4 * pitch) / options.grid_step) * options.grid_step


def _signal_routes(par, ports, lanes, ceiling, pitch, saved_buses, deadline):
    by_net = defaultdict(list)
    for port in ports:
        if par.power_marker_sym(port.net) is None:
            by_net[port.net].append(port)
    multi = [net for net in sorted(by_net) if len(by_net[net]) > 1]
    if saved_buses is not None:
        if (not isinstance(saved_buses, dict) or set(saved_buses) != set(multi)
                or any(type(value) not in (int, float) or not isfinite(value)
                       for value in saved_buses.values())):
            _invalid('Saved fallback bus coordinates do not match the signal nets',
                     stage='signal_buses')
        buses = dict(saved_buses)
    else:
        buses = {net: ceiling - number * pitch for number, net in enumerate(multi)}
    routed = []
    for net in sorted(by_net):
        _check_deadline(deadline, 'signal_buses')
        members = sorted(by_net[net], key=lambda port: (port.component_id.casefold(), port.pin))
        if len(members) == 1:
            port = members[0]
            routed.append((net, [(port.point, port.escape)]))
            continue
        segments, xs = [], []
        y = buses[net]
        for port in members:
            x = lanes[(port.component, port.pin)]
            points = [port.point, port.escape, (x, port.escape[1]), (x, y)]
            segments.extend((a, b) for a, b in zip(points, points[1:]) if a != b)
            xs.append(x)
        segments.append(((min(xs), y), (max(xs), y)))
        routed.append((net, segments))
    return routed, buses


def _validate(par, placement, comps, nets, index, routed, options, deadline, started):
    from netlist2schematic.routing.sa_routing import validate_final_routes
    _check_deadline(deadline, 'final_validation')
    routed, metrics, validation = validate_final_routes(
        par, nets, placement, index, routed, options=options, deadline=deadline)
    _check_deadline(deadline, 'final_validation')
    validation.update(router=VERSION, routing_stage='bus_fallback',
                      routing_seconds=round(perf_counter() - started, 6))
    return routed, metrics, validation


def create_fallback(path, *, cell=None, seed=20261029, routing_options=None, deadline=None):
    """Repack once into deterministic slabs and return fully verified routes."""
    started = perf_counter()
    _check_deadline(deadline, 'parse_start')
    options = routing_options or RoutingOptions()
    source = Path(path).expanduser().resolve()
    devices = parse_netlist(source, cell=cell)
    doc = to_document(devices)
    comps, nets = doc['components'], doc['nets']
    if not comps:
        raise ValueError('The selected netlist contains no supported devices')
    index = {comp['id']: i for i, comp in enumerate(comps)}
    _check_deadline(deadline, 'conduction_levels')
    levels = dl.compute_levels(devices)
    legacy = load_local('place_layered', 'place-layered.py')
    par = legacy.par
    conduction = {net for device in devices.values() if device['cls'] in ('MOS', 'BJT')
                  for net in (device.get('d'), device.get('s'), device.get('c'), device.get('e'))
                  if net is not None}
    attach = legacy.attach_info(devices, levels['net_levels'], conduction)
    rows, _ = legacy._allocate_rows(devices, comps, index, levels['net_levels'],
                                     levels['device_levels'], attach)
    pitch = _pitch(par, comps, options)
    order = sorted(range(len(comps)), key=lambda i: (rows[i], comps[i]['id'].casefold(), comps[i]['id']))
    column = {i: rank for rank, i in enumerate(order)}
    placement = par.Placement.__new__(par.Placement)
    placement.comps = comps
    placement.rng = random.Random(seed)
    placement.cell_of = [(rows[i], column[i]) for i in range(len(comps))]
    placement.comp_at = {value: i for i, value in enumerate(placement.cell_of)}
    placement.orient = [0] * len(comps)
    placement.cols = len(comps)
    placement.row_of = list(rows)
    placement.column_x = [8 * pitch + rank * 24 * pitch for rank in range(len(comps))]
    placement.row_y = [8 * pitch + row * 10 * pitch for row in range(max(rows) + 1)]
    _check_deadline(deadline, 'construct_layout')
    ports = _ports(par, placement, comps, nets, index, options, deadline)
    lanes = _lanes(placement, ports, pitch, deadline)
    power_y = floor((min([_body_top(par, placement, comps, options.halo)]
                        + [port.escape[1] for port in ports]) - 4 * pitch)
                    / options.grid_step) * options.grid_step
    connections = _power_connections(par, ports, lanes, power_y, pitch, deadline)
    placement.power_connections = deepcopy(connections)
    ceiling = _bus_ceiling(par, placement, comps, ports, connections, options, pitch)
    routed, buses = _signal_routes(par, ports, lanes, ceiling, pitch, None, deadline)
    metadata = {'layout_style': 'bus_fallback', 'routing_stage': 'bus_fallback',
                'source_note': levels['source_note'], 'levels': levels['device_levels'],
                'attach': attach, 'structs': [], 'sym_pairs': [], 'energy': None,
                'layout_grid': {'column_x': list(placement.column_x), 'row_y': list(placement.row_y)},
                'power_connections': deepcopy(connections),
                'fallback': {'version': VERSION, 'pitch': pitch, 'lane_spacing': 2 * pitch,
                             'component_spacing': 24 * pitch, 'row_pitch': 10 * pitch,
                             'component_order': [comps[i]['id'] for i in order],
                             'power_plane_y': power_y, 'bus_ceiling_y': ceiling,
                             'bus_y': buses, 'routing_parameters': asdict(options)}}
    result = _validate(par, placement, comps, nets, index, routed, options, deadline, started)
    return placement, comps, nets, index, metadata, result


def reroute_fallback(par, placement, comps, nets, index, metadata, *, routing_options=None,
                     deadline=None):
    """Rebuild saved lanes/buses while preserving every saved placement value."""
    started = perf_counter()
    _check_deadline(deadline, 'saved_fallback_start')
    config = metadata.get('fallback', {})
    if not isinstance(config, dict) or config.get('version') != VERSION:
        raise ValueError('Unsupported or missing saved constructive fallback version')
    pitch = config.get('pitch')
    if type(pitch) not in (int, float) or not isfinite(pitch) or pitch <= 0:
        raise ValueError('Saved fallback pitch must be positive and finite')
    if routing_options is None:
        if metadata.get('validation', {}).get('parameters') is not None:
            from netlist2schematic.common.sa_saved import saved_routing_options
            routing_options = saved_routing_options(metadata)
        else:
            parameters = dict(config.get('routing_parameters', {}))
            if 'margins' in parameters:
                parameters['margins'] = tuple(parameters['margins'])
            routing_options = RoutingOptions(**parameters)
    connections = metadata.get('power_connections')
    if not isinstance(connections, list):
        raise ValueError('Saved fallback power connections must be a list')
    if hasattr(placement, 'power_connections') and placement.power_connections != connections:
        raise ValueError('Placement and metadata power connections do not match')
    if not hasattr(placement, 'power_connections'):
        placement.power_connections = deepcopy(connections)
    # Check saved explicit power paths before using their marker envelopes.
    # Normal scene construction does not add power terminals to signal pins.
    from netlist2schematic.routing.routing_rules import build_scene
    prepared = build_scene(par, nets, placement, index, options=routing_options, deadline=deadline)
    ports = _ports(par, placement, comps, nets, index, routing_options, deadline)
    lanes = _lanes(placement, ports, pitch, deadline)
    ceiling = _bus_ceiling(par, placement, comps, ports, prepared.power_connections,
                           routing_options, pitch)
    routed, _ = _signal_routes(par, ports, lanes, ceiling, pitch, config.get('bus_y'), deadline)
    return _validate(par, placement, comps, nets, index, routed, routing_options, deadline, started)
