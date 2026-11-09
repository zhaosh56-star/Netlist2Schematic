"""Restore immutable placement coordinates for rerouting and independent redraw."""
from __future__ import annotations

from pathlib import Path
from copy import deepcopy
import hashlib
import random
from math import isfinite

from sa_common import configure_resources, load_local, read_json
from sa_geometry import validate_geometry

BUS_ROUTER = 'constructive-bus-v1'


def is_bus_layout(data):
    validation = data.get('validation')
    return (data.get('layout_style') == 'bus_fallback'
            or data.get('routing_stage') == 'bus_fallback'
            or isinstance(validation, dict) and validation.get('router') == BUS_ROUTER)


def check_source_hash(source, expected):
    if expected is not None and hashlib.sha256(Path(source).read_bytes()).hexdigest() != expected:
        raise ValueError('Source netlist does not match saved source_sha256')


def saved_routing_options(data):
    from routing_rules import RoutingOptions
    validation = data.get('validation', {})
    if not isinstance(validation, dict):
        raise ValueError('Saved validation must be an object')
    parameters = validation.get('parameters')
    if parameters is None:
        return RoutingOptions(crossing_cost=500.0, bend_cost=30.0)
    if not isinstance(parameters, dict):
        raise ValueError('Saved routing parameters must be an object')
    parameters = dict(parameters)
    # Older files were produced with the original final-router cost policy.
    if 'crossing_cost' not in parameters:
        parameters['crossing_cost'] = 500.0
    if 'bend_cost' not in parameters:
        parameters['bend_cost'] = 30.0
    try:
        if 'margins' in parameters:
            parameters['margins'] = tuple(parameters['margins'])
        return RoutingOptions(**parameters)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'Invalid saved routing parameters: {exc}') from exc


def load_placement(jpath, *, analog_canvas_root=None):
    source_json = Path(jpath).expanduser().resolve()
    data = read_json(source_json)
    if not isinstance(data, dict):
        raise ValueError('Saved placement must be an object')
    configure_resources(analog_canvas_root or data.get('analog_canvas_root'))
    par = load_local('place_and_route', 'place-and-route.py')
    validate_geometry(par.GEO_DOC)
    pn = load_local('parse_netlist', 'parse-netlist.py')
    source = data.get('source_netlist')
    if source:
        source = Path(source).expanduser()
        if not source.is_absolute():
            source = source_json.parent / source
    else:
        source = source_json.with_name(source_json.name.replace('-placement.json', '.cir'))
    source = source.resolve()
    check_source_hash(source, data.get('source_sha256'))
    document = pn.parse(source, cell=data.get('source_cell'))
    comps, nets = document['components'], document['nets']
    index = {component['id']: i for i, component in enumerate(comps)}
    saved = data.get('placement', {})
    if set(saved) != set(index):
        raise ValueError('Saved placement device IDs do not match the source netlist')
    placement = par.Placement.__new__(par.Placement)
    placement.comps = comps
    placement.rng = random.Random(data.get('seed', 20261029))
    placement.cell_of = [(saved[c['id']]['row'], saved[c['id']]['col']) for c in comps]
    placement.orient = [saved[c['id']]['orient'] for c in comps]
    for i, component in enumerate(comps):
        if any(type(value) is not int or value < 0 for value in placement.cell_of[i]):
            raise ValueError(f"Invalid row/column for {component['id']}: expected non-negative integers")
        if type(placement.orient[i]) is not int or not 0 <= placement.orient[i] < len(
                par.GEO_DOC[component['symbol']]['orientations']):
            raise ValueError(f"Invalid orientation for {component['id']}")
    if len(set(placement.cell_of)) != len(comps):
        raise ValueError('Saved placement contains duplicate cells')
    placement.comp_at = {position: i for i, position in enumerate(placement.cell_of)}
    placement.routing_stage = data.get('routing_stage', 'normal')
    bus_layout = is_bus_layout(data)
    if bus_layout:
        fallback = data.get('fallback')
        if not isinstance(fallback, dict) or fallback.get('version') != BUS_ROUTER:
            raise ValueError('Saved bus layout has an unsupported construction version')
        if 'power_connections' not in data:
            raise ValueError('Saved bus layout is missing power_connections')
        if not isinstance(data.get('layout_grid'), dict):
            raise ValueError('Saved bus layout is missing its physical layout_grid')
    if 'power_connections' in data:
        if not isinstance(data['power_connections'], list):
            raise ValueError('Saved power_connections must be a list')
        # The scene builder independently checks the full schema and every
        # connection against the source netlist before routing or rendering.
        placement.power_connections = deepcopy(data['power_connections'])
    grid = data.get('layout_grid')
    if grid is not None:
        for key, attribute, axis in (('column_x', 'column_x', 1), ('row_y', 'row_y', 0)):
            values = grid.get(key) if isinstance(grid, dict) else None
            if not isinstance(values, list) or not values or any(
                    type(value) not in (int, float) or not isfinite(value) for value in values):
                raise ValueError(f'Invalid physical layout coordinates: {key}')
            if any(a >= b for a, b in zip(values, values[1:])):
                raise ValueError(f'Physical layout coordinates must increase: {key}')
            if max(position[axis] for position in placement.cell_of) >= len(values):
                raise ValueError(f'Physical layout coordinates do not cover the placement: {key}')
            setattr(placement, attribute, list(values))
    if any('dx' in saved[component['id']] for component in comps):
        offsets = [saved[component['id']].get('dx', 0) for component in comps]
        for i, value in enumerate(offsets):
            if type(value) not in (int, float) or not isfinite(value):
                raise ValueError(f"Invalid horizontal offset for {comps[i]['id']}")
            col = placement.cell_of[i][1]
            axis = getattr(placement, 'column_x', None)
            left = ((axis[col]-axis[col-1])/2 if axis and col else
                    (axis[1]-axis[0])/2 if axis and len(axis)>1 else par.CELL/2)
            right = ((axis[col+1]-axis[col])/2 if axis and col+1<len(axis) else left)
            if value < -left-1e-9 or value > right+1e-9:
                raise ValueError(f"Horizontal offset crosses a column midpoint for {comps[i]['id']}")
        placement.offset_x = offsets
    return data, source, par, placement, comps, nets, index
