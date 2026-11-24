"""Local negative-supply cap, with explicit saved-layout interpretation.

VEE is a supply *role*, never an alias for electrical node 0. The external
Analog Canvas resources and the historical geometry cache remain read-only.
"""
from copy import deepcopy
from math import ceil
from pathlib import Path
from types import SimpleNamespace

CURRENT_POLICY = 'negative-supply-cap-v1'
LEGACY_POLICY = 'legacy-v1'
SYMBOL = 'vee-port'


def is_negative_supply(net):
    return str(net).casefold().rstrip('!') == 'vee'


def symbol_definition():
    return {'id': SYMBOL, 'name': 'Negative supply',
            'viewBox': {'x': -12, 'y': -2, 'width': 24, 'height': 24},
            'pins': [{'name': 'P', 'at': {'x': 0, 'y': 0},
                      'direction': 'north', 'leadLength': 10}],
            'primitives': [
                {'kind': 'line', 'from': {'x': 0, 'y': 0},
                 'to': {'x': 0, 'y': 18.5}, 'stroke': 'foreground', 'strokeWidth': 2.5},
                {'kind': 'polygon', 'points': [{'x': -10, 'y': 17.64},
                    {'x': 10, 'y': 17.64}, {'x': 10, 'y': 20.88},
                    {'x': -10, 'y': 20.88}], 'fill': 'foreground', 'stroke': 'none'}],
            'variants': [], 'defaultVariantId': None, 'labelVisibility': 'hidden'}


def symbol_geometry():
    symbol = symbol_definition()
    pins = deepcopy(symbol['pins'])
    return {**{key: deepcopy(symbol[key]) for key in
               ('id', 'name', 'viewBox', 'pins', 'defaultVariantId')},
            'hiddenPins': [], 'variantIds': [], 'orientationPolicy': 'fixed',
            'orientations': [{'rotation': 0, 'mirror': 'none',
                'pins': [{k: v for k, v in pin.items() if k != 'leadLength'} for pin in pins],
                'viewBoxAABB': deepcopy(symbol['viewBox']),
                'pinSpread': {'minX': 0, 'maxX': 0, 'minY': 0, 'maxY': 0}}]}


def saved_module(par, data):
    """Bind legacy interpretation without mutating the shared engine module."""
    policy = data.get('supply_marker_policy', LEGACY_POLICY)
    if policy not in (LEGACY_POLICY, CURRENT_POLICY):
        raise ValueError(f'Unsupported supply_marker_policy: {policy!r}')
    if policy == LEGACY_POLICY:
        par = SimpleNamespace(**vars(par))
        par.power_marker_sym = par.legacy_power_marker_sym
    return par, policy


def resource_files(par, comps):
    """Hash the procedural symbol only when this interpretation actually uses it."""
    if any(par.power_marker_sym(net) == SYMBOL for comp in comps
           for net in comp.get('nodes', {}).values()):
        return [Path(__file__).resolve()]
    return []


def negative_connection(par, placement, component, pin, net, *, halo=12, grid_step=2):
    """Exit the real pin first, then enter a downward cap from above.

    Only bounded orthogonal local patterns are used, never A*. For an upward
    pin a side lane goes around the owner's body; no direction reversal or
    blanket same-net body exemption is introduced.
    """
    from netlist2schematic.routing.routing_rules import _escape, _inflate, segment_hits_rect
    point = tuple(placement.pin_world(component, pin))
    direction = tuple(par.DIR_VEC[placement.pin_dir(component, pin)])
    bodies = []
    for i, comp in enumerate(placement.comps):
        x, y = placement.pos_of(i)
        b = par.GEO_DOC[comp['symbol']]['orientations'][placement.orient[i]]['viewBoxAABB']
        bodies.append(_inflate((x+b['x'], y+b['y'], x+b['x']+b['width'], y+b['y']+b['height']), halo))
    escape = _escape(point, direction, bodies[component], grid_step)
    span = ceil(max(32, halo+20)/grid_step)*grid_step
    distance = max(span, abs(escape[0]-point[0])+abs(escape[1]-point[1]))
    # The whole cap halo, rather than just its pin, must clear the owner.
    body = bodies[component]
    if direction == (0, 1):
        distance = max(distance, ceil((body[3]+halo+2-point[1])/grid_step)*grid_step)
    elif direction[0]:
        edge = body[2] if direction[0] > 0 else body[0]
        distance = max(distance, ceil((abs(edge-point[0])+halo+14)/grid_step)*grid_step)
    exit_point = tuple(point[k]+direction[k]*distance for k in (0, 1))
    if direction == (0, 1):
        points = [point, exit_point]
    elif direction[0]:
        points = [point, exit_point, (exit_point[0], exit_point[1]+span)]
    else:
        lanes = [grid_step*int((body[0]-halo-14)//grid_step),
                 grid_step*ceil((body[2]+halo+14)/grid_step)]
        candidates = [[point, exit_point, (x, exit_point[1]), (x, point[1]+span)] for x in lanes]
        def rank(points):
            segments = list(zip(points, points[1:]))
            hits = sum(segment_hits_rect(a, b, rect) for a, b in segments[1:] for rect in bodies)
            length = sum(abs(a[0]-b[0])+abs(a[1]-b[1]) for a, b in segments)
            return hits, length, points[-1]
        points = min(candidates, key=rank)
    return {'component_id': placement.comps[component]['id'], 'pin': pin,
            'net': net, 'symbol': SYMBOL, 'anchor': points[-1],
            'segments': list(zip(points, points[1:]))}


def audit_negative_connections(scene, add, deadline=None):
    """Independently check generated leads; exemption ends at the real exit."""
    from netlist2schematic.routing.routing_rules import (
        EPS, _in_corridor, check_deadline, segment_hits_rect)
    for record in scene.negative_connections:
        check_deadline(deadline, 'negative_supply_geometry')
        identity = [record['component_id'], record['pin']]
        # A display plugin may remove/merge positive caps and reorder its
        # obstacle list. Identify this negative cap by owner and real shape,
        # never by its former position in that list.
        own = [i for i, (net, rect) in enumerate(scene.marker_rects)
               if net == record['net'] and rect == record['marker_rect']
               and i < len(scene.marker_components)
               and scene.marker_components[i] == record['component']]
        if len(own) != 1:
            add('power_marker', [record['net']], record['anchor'],
                connection=identity, reason='negative_cap_missing_or_ambiguous')
            continue
        own_index = own[0]
        for number, (a, b) in enumerate(record['segments']):
            for component, rect in scene.body_rects:
                if segment_hits_rect(a, b, rect) and not (
                        component == record['component'] and number == 0
                        and _in_corridor(a, b, rect, record['source_corridor'])):
                    add('power_body', [record['net']], a,
                        component=component, connection=identity, segment=(a, b))
            for marker_index, (net, rect) in enumerate(scene.marker_rects):
                own_entry = (marker_index == own_index
                    and number == len(record['segments'])-1
                    and _in_corridor(a, b, rect, record['marker_entry']))
                if segment_hits_rect(a, b, rect) and not own_entry:
                    add('power_marker', [record['net'], net], a,
                        connection=identity, marker=marker_index, segment=(a, b))
        own_rect = scene.marker_rects[own_index][1]
        for component, body in scene.body_rects:
            if all(max(own_rect[k], body[k]) < min(own_rect[k+2], body[k+2])-EPS for k in (0, 1)):
                add('power_marker_body', [record['net']], record['anchor'],
                    connection=identity, component=component)
        for marker_index, (net, rect) in enumerate(scene.marker_rects):
            if marker_index != own_index and all(
                    max(own_rect[k], rect[k]) < min(own_rect[k+2], rect[k+2])-EPS for k in (0, 1)):
                add('power_marker_overlap', [record['net'], net], record['anchor'],
                    connection=identity, marker=marker_index)
