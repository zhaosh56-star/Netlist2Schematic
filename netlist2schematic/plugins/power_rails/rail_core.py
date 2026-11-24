"""Small, deterministic supply-rail construction; no layout or path search.

Only coplanar north-facing terminals of the *same* supply may share a rail.
The native scene is never mutated.  Supply geometry has an additional audit
because the ordinary signal-body validator deliberately skips supply wires.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from math import floor, isfinite
from time import perf_counter

from netlist2schematic.routing.routing_rules import (
    EPS, Pin, analyze_routes, canonical_topology, check_deadline, _escape,
    _inflate, _in_corridor, segment_distance, segment_hits_rect,
    static_shape_conflicts,
)
from netlist2schematic.rendering.sa_render import _overlap, _text_box

VERSION = 'local-vdd-rails-v1'
MAX_TRIALS = 128


def hints_for(hints, groups):
    removed = {tuple(identity) for group in groups for identity in group['ports']}
    result = deepcopy(hints)
    result['supply_text'] = [row for row in result['supply_text']
                             if tuple(row['identity']) not in removed]
    result['text_obstacles'] = [row['bounds'] for key in
        ('component_text', 'supply_text', 'port_text') for row in result[key]]
    return result


def _point(value):
    if (not isinstance(value, (tuple, list)) or len(value) != 2 or
            any(type(v) not in (int, float) or not isfinite(v) for v in value)):
        raise ValueError('Supply coordinates must be finite coordinate pairs')
    return tuple(value)


def _group_geometry(group, ports, geometry, options, scale):
    identities = group.get('ports')
    if not isinstance(identities, list) or len(identities) < 2:
        raise ValueError('A shared rail requires at least two original terminals')
    keys = [tuple(value) for value in identities]
    if len(set(keys)) != len(keys) or any(key not in ports for key in keys):
        raise ValueError('Duplicate or unknown shared supply terminal')
    members = [ports[key] for key in keys]
    net = group.get('net')
    if any(p['net'] != net or p['symbol'] != 'vdd-port' or
           tuple(p['direction']) != (0, -1) for p in members):
        raise ValueError('Shared supply terminals must have the same VDD net and upward direction')
    if len({p['point'][1] for p in members}) != 1:
        raise ValueError('First-version shared rails require equal terminal height')
    xs = sorted(p['point'][0] for p in members)
    if xs[0] == xs[-1] or xs[-1]-xs[0] > 6*scale+EPS or any(
            b-a > 3*scale+EPS for a, b in zip(xs, xs[1:])):
        raise ValueError('Shared rail is outside the local span limit')
    y = group.get('rail_y')
    if type(y) not in (int, float) or not isfinite(y) or y > min(p['escape'][1] for p in members)-options.grid_step+EPS:
        raise ValueError('Shared rail must remain outside every original pin escape')
    if min(p['escape'][1] for p in members)-y > 2*scale+EPS:
        raise ValueError('Shared rail cannot make a remote detour')
    anchor = _point(group.get('anchor'))
    if not xs[0] <= anchor[0] <= xs[-1] or anchor[1] >= y:
        raise ValueError('Shared supply symbol must lie above its own rail')
    local = geometry['pin']
    origin = (anchor[0]-local[0], anchor[1]-local[1])
    b = geometry['box']
    rect = _inflate((origin[0]+b[0], origin[1]+b[1], origin[0]+b[2], origin[1]+b[3]), options.halo)
    entry = _escape(anchor, (0, 1), rect, options.grid_step)
    if y < entry[1]-EPS or y-anchor[1] > scale+EPS:
        raise ValueError('Shared symbol needs a real unobstructed pin-entry corridor')
    branches = [(tuple(p['point']), (p['point'][0], y)) for p in members]
    connector = ((anchor[0], y), anchor)
    segments = canonical_topology([*branches, ((xs[0], y), (xs[-1], y)), connector])['segments']
    return dict(members=members, origin=origin, rect=rect, entry=(anchor, entry),
                connector=connector, segments=segments, anchor=anchor)


def validate_view(scene, routed, terminals, geometry, view, hints, *, scale, deadline=None):
    """Independently reconstruct and audit every changed lead and glyph.

    Original signals and unselected supply ports come from the immutable scene,
    never from the view.  Same-net identity grants no body/marker exemption.
    """
    if view.get('version') != VERSION or view.get('scale') != scale:
        raise ValueError('Unsupported shared-supply view or changed physical pitch')
    groups = view.get('groups')
    if not isinstance(groups, list):
        raise ValueError('Shared-supply groups must be a list')
    ports = {(p['component_id'], p['pin']): p for p in terminals}
    seen, shapes = set(), []
    for group in groups:
        check_deadline(deadline, 'power_rail_geometry')
        shape = _group_geometry(group, ports, geometry, scene.options, scale)
        for member in shape['members']:
            identity = member['component_id'], member['pin']
            if identity in seen:
                raise ValueError('A supply terminal belongs to more than one rail')
            seen.add(identity)
        expected = [[list(a), list(b)] for a, b in shape['segments']]
        if group.get('segments') != expected:
            raise ValueError('Shared-supply lines differ from the real terminals and symbol pin')
        shapes.append(shape)
    rebuilt = deepcopy(scene)
    rebuilt.fixed_segments = {net: list(lines) for net, lines in scene.fixed_segments.items()
                              if net not in scene.power_nets}
    rebuilt.marker_rects, rebuilt.marker_components = [], []
    untouched = [p for p in terminals if (p['component_id'], p['pin']) not in seen]
    for p in untouched:
        rebuilt.fixed_segments.setdefault(p['net'], []).extend(
            tuple(map(tuple, segment)) for segment in p.get('segments', [p['stub']]))
        rebuilt.marker_rects.append((p['net'], tuple(p['marker_rect'])))
        rebuilt.marker_components.append(p['component'])
    all_shapes = [(p['net'], tuple(p['marker_rect']), None) for p in untouched]
    for number, (group, shape) in enumerate(zip(groups, shapes)):
        rebuilt.fixed_segments.setdefault(group['net'], []).extend(shape['segments'])
        rebuilt.marker_rects.append((group['net'], shape['rect']))
        rebuilt.marker_components.append(None)
        rebuilt.all_pins.setdefault(group['net'], []).append(shape['anchor'])
        all_shapes.append((group['net'], shape['rect'], number))
    if static_shape_conflicts(rebuilt, deadline=deadline):
        raise ValueError('Shared supply symbol overlaps a device protection zone')
    # Preserve all original signal constraints, adding the actual supply lines.
    checked = analyze_routes(routed, rebuilt, deadline=deadline)
    if checked['violations']:
        first = checked['violations'][0]
        raise ValueError('Shared-supply geometry: '+first['kind'])
    text_hints = hints_for(hints, groups)
    old_texts = text_hints['text_obstacles']
    occupied_texts = list(old_texts)
    all_lines = [line for _, lines in routed for line in lines]
    all_lines.extend(line for net in rebuilt.power_nets for line in rebuilt.fixed_segments.get(net, ()))
    for number, (group, shape) in enumerate(zip(groups, shapes)):
        check_deadline(deadline, 'power_rail_geometry')
        net = group['net']
        corridor_scene = deepcopy(scene)
        corridor_scene.pins = {net: [Pin(net, p['component'], p['pin'], tuple(p['point']),
            tuple(p['escape']), tuple(p['direction'])) for p in shape['members']]}
        for a, b in shape['segments']:
            for component, rect in scene.body_rects:
                if not corridor_scene.body_allowed(net, a, b, component, rect):
                    raise ValueError('Shared supply lead enters a device protection zone')
            for owner, rect, identity in all_shapes:
                if segment_hits_rect(a, b, rect) and not (
                        identity == number and _in_corridor(a, b, rect, shape['entry'])):
                    raise ValueError('Shared supply lead enters a supply-symbol protection zone')
            if any(segment_hits_rect(a, b, _inflate(box, 3)) for box in old_texts):
                raise ValueError('Shared supply lead overlaps existing text')
        for owner, rect, identity in all_shapes:
            if identity != number and _overlap(rect, shape['rect']):
                raise ValueError('Shared supply symbol overlaps another supply symbol')
        if any(segment_hits_rect(a, b, shape['rect']) for _, lines in routed for a, b in lines):
            raise ValueError('New supply symbol overlaps a signal wire')
        if any(_overlap(shape['rect'], box, 3) for box in old_texts):
            raise ValueError('New supply symbol overlaps existing text')
        # Separate local rails of the same net remain distinct visible groups.
        others = [tuple(map(tuple, line)) for p in untouched if p['net'] == net
                  for line in p.get('segments', [p['stub']])]
        others.extend(line for i, (g, s) in enumerate(zip(groups, shapes))
                      if i != number and g['net'] == net for line in s['segments'])
        if any(segment_distance(a, b, c, d) < scene.options.center_spacing-EPS
               for a, b in shape['segments'] for c, d in others):
            raise ValueError('Independent local supply groups would collide')
        topology = canonical_topology(shape['segments'], [*map(tuple, (p['point'] for p in shape['members'])), shape['anchor']])
        pending, visited = [shape['anchor']], {shape['anchor']}
        while pending:
            for point in topology['adjacency'].get(pending.pop(), ()):
                if point not in visited:
                    pending.append(point); visited.add(point)
        if any(tuple(p['point']) not in visited for p in shape['members']):
            raise ValueError('A real supply pin is disconnected from its shared symbol')
        label = group.get('label', {})
        if label.get('text') != net or label.get('anchor') not in ('start', 'middle', 'end'):
            raise ValueError('Shared-supply text must name its exact original network')
        point = _point((label.get('x'), label.get('y')))
        bounds = _text_box(*point, net, label['anchor'])
        if list(bounds) != label.get('bounds'):
            raise ValueError('Shared supply text bounds do not match its visible text')
        if (any(_overlap(bounds, box, 3) for box in occupied_texts) or
                any(_overlap(bounds, rect, 3) for _, rect in rebuilt.body_rects+rebuilt.marker_rects) or
                any(segment_hits_rect(a, b, _inflate(bounds, 3)) for a, b in all_lines)):
            raise ValueError('Shared supply text is not in a free readable position')
        if min(abs(point[0]-shape['anchor'][0])+abs(point[1]-shape['anchor'][1]),
               abs(point[0]-shape['origin'][0])+abs(point[1]-shape['origin'][1])) > scale:
            raise ValueError('Shared supply label is detached from its symbol')
        occupied_texts.append(bounds)
    return {'valid': True, 'signal_routes_unchanged': True, 'groups': len(groups),
            'merged_terminals': len(seen), 'markers_before': len(terminals),
            'markers_after': len(terminals)-len(seen)+len(groups),
            'markers_removed': len(seen)-len(groups),
            'power_wirelength': sum(abs(a[0]-b[0])+abs(a[1]-b[1])
                for net in rebuilt.power_nets for a, b in canonical_topology(rebuilt.fixed_segments.get(net, ()))['runs'])}


def build_view(scene, routed, terminals, geometry, hints, *, scale, seconds=5):
    from netlist2schematic.routing.orthogonal_router import RoutingFailure
    if type(seconds) not in (int, float) or not isfinite(seconds) or seconds < 0:
        raise ValueError('Shared-supply seconds must be finite and non-negative')
    started = perf_counter()
    deadline = started+seconds
    view = {'version': VERSION, 'scale': scale, 'groups': []}
    eligible = defaultdict(list)
    for p in terminals:
        if p['symbol'] == 'vdd-port' and tuple(p['direction']) == (0, -1):
            eligible[p['net'], p['point'][1]].append(p)
    candidates = []
    for (net, _), members in sorted(eligible.items()):
        members = sorted(members, key=lambda p: (p['point'][0], p['component_id'], p['pin']))
        for count in range(min(8, len(members)), 1, -1):
            for start in range(len(members)-count+1):
                group = members[start:start+count]
                xs = [p['point'][0] for p in group]
                if xs[-1] > xs[0] and xs[-1]-xs[0] <= 6*scale and all(b-a <= 3*scale for a,b in zip(xs,xs[1:])):
                    candidates.append((net, group))
    candidates.sort(key=lambda row: (-len(row[1]), row[1][-1]['point'][0]-row[1][0]['point'][0],
                                     row[1][0]['point'][1], row[1][0]['point'][0], row[0]))
    # Give partial local merges a chance even when a wide row is obstructed.
    # Trying every large overlapping subgroup first can exhaust the quota.
    largest, remaining, covered = [], [], set()
    for row in candidates:
        key = row[0], row[1][0]['point'][1]
        if key not in covered:
            largest.append(row); covered.add(key)
        else:
            remaining.append(row)
    candidates = largest + [row for row in remaining if len(row[1]) == 2] + [
        row for row in remaining if len(row[1]) != 2]
    used, attempted, rejected = set(), 0, Counter()
    stop = 'candidates_exhausted'
    try:
        for net, members in candidates:
            ids = [(p['component_id'], p['pin']) for p in members]
            if used.intersection(ids):
                continue
            xs = [p['point'][0] for p in members]
            step = scene.options.grid_step
            midpoint = floor((xs[0]+xs[-1])/2/step)*step
            nearest = min(xs, key=lambda x: (abs(x-midpoint), x))
            preferred = nearest if abs(nearest-midpoint) < 2*scene.options.cross_clearance else midpoint
            xchoices = list(dict.fromkeys([preferred, *sorted(xs,key=lambda x:(abs(x-midpoint),x)), midpoint]))[:8]
            accepted = False
            for offset in (0, scene.options.center_spacing, 2*scene.options.center_spacing, 4*scene.options.center_spacing):
                y = floor((min(p['escape'][1] for p in members)-2*step-offset)/step)*step
                for x in xchoices:
                    if attempted >= MAX_TRIALS:
                        stop = 'candidate_limit'; break
                    check_deadline(deadline, 'power_rail_planning')
                    attempted += 1
                    lift = scene.options.halo+4*step+max(0, geometry['box'][3]-geometry['pin'][1])
                    anchor = (x, y-lift)
                    group = dict(net=net, ports=[list(identity) for identity in ids], rail_y=y, anchor=list(anchor))
                    try:
                        shape = _group_geometry(group, {(p['component_id'],p['pin']):p for p in members}, geometry, scene.options, scale)
                    except ValueError as exc:
                        rejected[str(exc)] += 1
                        continue
                    group['segments'] = [[list(a),list(b)] for a,b in shape['segments']]
                    rect = shape['rect']
                    # Nearby alternatives on either side or above the true glyph.
                    labels = [(rect[2]+4, shape['origin'][1]+8, 'start'),
                              (rect[0]-4, shape['origin'][1]+8, 'end'),
                              (x, rect[1]-4, 'middle')]
                    for lx, ly, align in labels:
                        group['label'] = dict(text=net,x=lx,y=ly,anchor=align,
                                             bounds=list(_text_box(lx,ly,net,align)))
                        trial = {**view, 'groups': [*view['groups'], group]}
                        try:
                            validate_view(scene, routed, terminals, geometry, trial, hints, scale=scale, deadline=deadline)
                        except RoutingFailure:
                            raise
                        except ValueError as exc:
                            rejected[str(exc)] += 1
                            continue
                        view = deepcopy(trial); used.update(ids); accepted=True
                        break
                    if accepted or attempted >= MAX_TRIALS:
                        break
                if accepted or attempted >= MAX_TRIALS:
                    break
            if attempted >= MAX_TRIALS:
                stop = 'candidate_limit'
                break
    except RoutingFailure:
        stop = 'time_budget'
    view['status'] = 'merged' if view['groups'] else 'unchanged'
    view['diagnostics'] = dict(attempted=attempted, rejected=dict(rejected), stop_reason=stop,
                               elapsed_seconds=round(perf_counter()-started,6))
    return view
