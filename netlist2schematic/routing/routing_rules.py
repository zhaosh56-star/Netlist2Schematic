"""One geometric contract for routing and validating orthogonal schematics.

Coordinates describe wire centre lines.  Purely collinear subdivisions carry no
topological meaning; pins, bends, branches and open ends do.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from math import ceil, isfinite
from time import perf_counter

EPS = 1e-8
Point = tuple[float, float]
Segment = tuple[Point, Point]


def check_deadline(deadline, stage='validation'):
    if deadline is None:
        return
    if type(deadline) not in (int, float) or not isfinite(deadline):
        raise ValueError('The validation deadline must be a finite absolute time')
    if perf_counter() >= deadline:
        from netlist2schematic.routing.orthogonal_router import RoutingFailure
        raise RoutingFailure('Validation stopped at the quality time budget',
                             {'reason': 'quality_time_budget', 'stage': stage})


@dataclass(frozen=True)
class RoutingOptions:
    wire_width: float = 2.5
    edge_clearance: float = 6.0
    grid_step: float = 2.0
    halo: float = 12.0
    cross_clearance: float = 12.0
    crossing_cost: float = 150.0
    bend_cost: float = 15.0
    max_nodes: int = 100000
    max_expansions: int = 200000
    max_rounds: int = 4
    margins: tuple[float, ...] = (150.0, 300.0, 600.0)

    def __post_init__(self):
        for name in ('wire_width', 'grid_step', 'cross_clearance'):
            if type(getattr(self, name)) not in (int, float) or not isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f'{name} must be positive and finite')
        for value in (self.edge_clearance, self.halo):
            if type(value) not in (int, float) or not isfinite(value) or value < 0:
                raise ValueError('Clearances must be finite and non-negative')
        for name in ('crossing_cost', 'bend_cost'):
            value = getattr(self, name)
            if type(value) not in (int, float) or not isfinite(value) or value < 0:
                raise ValueError(f'{name} must be finite and non-negative')
        if any(type(value) is not int or value < 1 for value in (self.max_nodes, self.max_expansions)) or type(self.max_rounds) is not int or self.max_rounds < 0:
            raise ValueError('Invalid routing search budget')
        if not self.margins or any(type(value) not in (int, float) or not isfinite(value) or value <= 0
                                   for value in self.margins):
            raise ValueError('Routing margins must be positive and finite')

    @property
    def center_spacing(self):
        return ceil((self.wire_width + self.edge_clearance) / self.grid_step) * self.grid_step


@dataclass(frozen=True)
class Pin:
    net: str
    component: int
    pin: str
    point: Point
    escape: Point
    direction: Point

    @property
    def stub(self):
        return self.point, self.escape


def point_in_rect(point, rect):
    return rect[0] - EPS <= point[0] <= rect[2] + EPS and rect[1] - EPS <= point[1] <= rect[3] + EPS


def segment_hits_rect(a, b, rect):
    if a[0] == b[0]:
        return (rect[0] - EPS <= a[0] <= rect[2] + EPS
                and max(min(a[1], b[1]), rect[1]) <= min(max(a[1], b[1]), rect[3]) + EPS)
    if a[1] == b[1]:
        return (rect[1] - EPS <= a[1] <= rect[3] + EPS
                and max(min(a[0], b[0]), rect[0]) <= min(max(a[0], b[0]), rect[2]) + EPS)
    raise ValueError(f'Non-orthogonal segment: {a} -> {b}')


def on_segment(point, a, b):
    return ((a[0] == b[0] == point[0] and min(a[1], b[1]) <= point[1] <= max(a[1], b[1]))
            or (a[1] == b[1] == point[1] and min(a[0], b[0]) <= point[0] <= max(a[0], b[0])))


def point_segment_distance(point, a, b):
    """L-infinity distance, a conservative square clearance envelope."""
    return max(max(min(a[0], b[0]) - point[0], point[0] - max(a[0], b[0]), 0),
               max(min(a[1], b[1]) - point[1], point[1] - max(a[1], b[1]), 0))


def segment_distance(a, b, c, d):
    return max(max(min(a[0], b[0]) - max(c[0], d[0]), min(c[0], d[0]) - max(a[0], b[0]), 0),
               max(min(a[1], b[1]) - max(c[1], d[1]), min(c[1], d[1]) - max(a[1], b[1]), 0))


def intersection(a, b, c, d):
    if (a[0] == b[0]) == (c[0] == d[0]):
        return None
    vertical, horizontal = ((a, b), (c, d)) if a[0] == b[0] else ((c, d), (a, b))
    point = (vertical[0][0], horizontal[0][1])
    return point if on_segment(point, *vertical) and on_segment(point, *horizontal) else None


def merge_runs(segments, deadline=None):
    groups = defaultdict(list)
    for number, (raw_a, raw_b) in enumerate(segments):
        if number % 256 == 0:
            check_deadline(deadline, 'canonical_topology')
        a, b = tuple(raw_a), tuple(raw_b)
        if len(a) != 2 or len(b) != 2 or any(type(value) not in (int, float) or not isfinite(value) for value in (*a, *b)):
            raise ValueError(f'Invalid route coordinates: {a} -> {b}')
        if a == b:
            continue
        if a[0] == b[0]:
            groups[('v', a[0])].append(tuple(sorted((a[1], b[1]))))
        elif a[1] == b[1]:
            groups[('h', a[1])].append(tuple(sorted((a[0], b[0]))))
        else:
            raise ValueError(f'Non-orthogonal route segment: {a} -> {b}')
    result = []
    for (axis, track), intervals in sorted(groups.items()):
        merged = []
        for low, high in sorted(intervals):
            if merged and low <= merged[-1][1] + EPS:
                merged[-1] = (merged[-1][0], max(high, merged[-1][1]))
            else:
                merged.append((low, high))
        for low, high in merged:
            result.append(((track, low), (track, high)) if axis == 'v'
                          else ((low, track), (high, track)))
    return sorted(result)


def canonical_topology(segments, pins=(), deadline=None):
    runs = merge_runs(segments, deadline=deadline)
    points = {tuple(point) for point in pins}
    points.update(point for segment in runs for point in segment)
    operations = 0
    for i, (a, b) in enumerate(runs):
        for c, d in runs[i + 1:]:
            operations += 1
            if operations % 256 == 0:
                check_deadline(deadline, 'canonical_topology')
            point = intersection(a, b, c, d)
            if point is not None:
                points.add(point)
    adjacency = defaultdict(set)
    for a, b in runs:
        check_deadline(deadline, 'canonical_topology')
        cuts = sorted(point for point in points if on_segment(point, a, b))
        for p, q in zip(cuts, cuts[1:]):
            adjacency[p].add(q)
            adjacency[q].add(p)
    structures = {tuple(point) for point in pins}
    for point, neighbors in adjacency.items():
        if len(neighbors) != 2:
            structures.add(point)
        else:
            a, b = tuple(neighbors)
            if (a[0] == point[0]) != (b[0] == point[0]):
                structures.add(point)
    # Walk through only non-semantic, degree-two collinear nodes.
    simplified = set()
    visited = set()
    for start in sorted(structures):
        for neighbor in sorted(adjacency.get(start, ())):
            edge = frozenset((start, neighbor))
            if edge in visited:
                continue
            visited.add(edge)
            previous, current = start, neighbor
            while current not in structures:
                target = next(iter(adjacency[current] - {previous}))
                visited.add(frozenset((current, target)))
                previous, current = current, target
            simplified.add(tuple(sorted((start, current))))
    simple_adj = defaultdict(set)
    for a, b in simplified:
        simple_adj[a].add(b)
        simple_adj[b].add(a)
    return {'segments': sorted(simplified), 'runs': runs,
            'structure_points': structures, 'adjacency': dict(simple_adj),
            'pin_points': {tuple(point) for point in pins},
            'junction_points': {point for point, neighbors in adjacency.items() if len(neighbors) > 2}}


@dataclass
class PreparedOccupancy:
    topologies: dict


@dataclass
class Scene:
    options: RoutingOptions = field(default_factory=RoutingOptions)
    body_rects: list = field(default_factory=list)
    marker_rects: list = field(default_factory=list)
    pins: dict = field(default_factory=dict)
    fixed_segments: dict = field(default_factory=dict)
    power_nets: set = field(default_factory=set)
    bounds: tuple = (0, 0, 100, 100)
    all_pins: dict = field(default_factory=dict)
    power_connections: list | None = None
    power_corridors: dict = field(default_factory=dict)
    power_markers: list = field(default_factory=list)

    def prepare_occupied(self, occupied):
        if isinstance(occupied, PreparedOccupancy):
            return occupied
        return PreparedOccupancy({net: canonical_topology(segments, self.all_pins.get(net, ()))
                                  for net, segments in occupied.items()})

    def body_allowed(self, net, a, b, component, rect):
        if not segment_hits_rect(a, b, rect):
            return True
        # Only the exact declared pin corridor through its own halo is exempt.
        if a[0] == b[0]:
            clipped = ((a[0], max(min(a[1], b[1]), rect[1])),
                       (a[0], min(max(a[1], b[1]), rect[3])))
        else:
            clipped = ((max(min(a[0], b[0]), rect[0]), a[1]),
                       (min(max(a[0], b[0]), rect[2]), a[1]))
        return any(pin.component == component and on_segment(clipped[0], *pin.stub)
                   and on_segment(clipped[1], *pin.stub) for pin in self.pins.get(net, ()))

    def edge_check(self, net, a, b, occupied):
        a, b = tuple(a), tuple(b)
        blockers, crossings = set(), 0
        if a == b:
            return True, 0, blockers
        if a[0] != b[0] and a[1] != b[1]:
            return False, 0, {'non_orthogonal'}
        for component, rect in self.body_rects:
            if not self.body_allowed(net, a, b, component, rect):
                blockers.add(f'body:{component}')
        for owner, rect in self.marker_rects:
            if segment_hits_rect(a, b, rect):
                blockers.add(owner)
        for owner, segments in self.fixed_segments.items():
            if owner != net and any(segment_distance(a, b, c, d) < self.options.center_spacing - EPS
                                    for c, d in segments):
                blockers.add(owner)
        for owner, points in self.all_pins.items():
            if owner != net and any(point_segment_distance(point, a, b) < self.options.cross_clearance - EPS
                                    for point in points):
                blockers.add(owner)
        prepared = self.prepare_occupied(occupied)
        for owner, topology in prepared.topologies.items():
            if owner == net:
                continue
            # Final validation protects branches more than parallel tracks.
            # Enforce the same keepout before the track-spacing early exit.
            if any(point_segment_distance(point, a, b) < self.options.cross_clearance - EPS
                   for point in topology['junction_points']):
                blockers.add(owner)
            for c, d in topology['runs']:
                if segment_distance(a, b, c, d) >= self.options.center_spacing - EPS:
                    continue
                if (a[0] == b[0]) == (c[0] == d[0]):
                    blockers.add(owner)
                    continue
                # A short A* edge may approach or end exactly at an X.  The
                # vertex check forces straight continuation across its zone.
                projected = (a[0], c[1]) if a[0] == b[0] else (c[0], a[1])
                inside = on_segment(projected, c, d) and projected != c and projected != d
                clear = not any(max(abs(projected[0]-p[0]), abs(projected[1]-p[1])) < self.options.cross_clearance - EPS
                                for p in topology['structure_points'])
                if not inside or not clear:
                    blockers.add(owner)
                    continue
                point = intersection(a, b, c, d)
                if point is not None and point != a:
                    crossings += 1
        return not blockers, crossings, blockers

    def vertex_check(self, net, point, occupied):
        point = tuple(point)
        blockers = set()
        for owner, topology in self.prepare_occupied(occupied).topologies.items():
            if owner != net and any(point_segment_distance(point, a, b) < self.options.cross_clearance - EPS
                                    for a, b in topology['runs']):
                blockers.add(owner)
        for owner, points in self.all_pins.items():
            if owner != net and any(max(abs(point[0]-p[0]), abs(point[1]-p[1])) < self.options.cross_clearance - EPS
                                    for p in points):
                blockers.add(owner)
        return not blockers, 0, blockers


def _inflate(rect, amount):
    return rect[0]-amount, rect[1]-amount, rect[2]+amount, rect[3]+amount


def _power_error(message, **details):
    from netlist2schematic.routing.sa_routing import RoutingValidationError
    raise RoutingValidationError('Invalid power_connections: ' + message,
                                 {'reason': 'power_connections', 'violations': [
                                     {'kind': 'power_connection', 'message': message, **details}]})


def _point(value, label):
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(type(v) not in (int, float) or not isfinite(v) for v in value)):
        _power_error(f'{label} must be a finite coordinate pair')
    return tuple(value)


def _escape(point, direction, rect, step):
    if direction[0]:
        edge = rect[2] + step if direction[0] > 0 else rect[0] - step
        return (max(point[0], edge) if direction[0] > 0 else min(point[0], edge), point[1])
    edge = rect[3] + step if direction[1] > 0 else rect[1] - step
    return (point[0], max(point[1], edge) if direction[1] > 0 else min(point[1], edge))


def _along(vector, direction):
    return (vector[0] * direction[1] == vector[1] * direction[0]
            and vector[0] * direction[0] + vector[1] * direction[1] > 0)


def _rect_clip(a, b, rect):
    if a[0] == b[0]:
        return ((a[0], max(min(a[1], b[1]), rect[1])),
                (a[0], min(max(a[1], b[1]), rect[3])))
    return ((max(min(a[0], b[0]), rect[0]), a[1]),
            (min(max(a[0], b[0]), rect[2]), a[1]))


def _in_corridor(a, b, rect, corridor):
    return all(on_segment(p, *corridor) for p in _rect_clip(a, b, rect))


def _normalize_power_connections(par, nets, placement, comp_index, scene, bodies, deadline):
    raw = getattr(placement, 'power_connections', None)
    if raw is None:
        return None
    if not isinstance(raw, list):
        _power_error('expected a list')
    expected = {(cid, pin): (net, par.power_marker_sym(net))
                for net, members in nets.items() if par.power_marker_sym(net)
                for cid, pin in members}
    seen, normalized = set(), []
    for number, record in enumerate(raw):
        check_deadline(deadline, 'power_schema')
        if not isinstance(record, dict):
            _power_error('each connection must be an object', connection=number)
        if any(not isinstance(record.get(key), str) for key in ('component_id', 'pin', 'net', 'symbol')):
            _power_error('component_id, pin, net and symbol must be strings', connection=number)
        identity = record['component_id'], record['pin']
        if identity not in expected or identity in seen:
            _power_error('unknown or duplicate source power pin', connection=number,
                         component_id=identity[0], pin=identity[1])
        seen.add(identity)
        net, symbol = expected[identity]
        if (record['net'], record['symbol']) != (net, symbol):
            _power_error('net or symbol does not match the source power pin', connection=number)
        anchor = _point(record.get('anchor'), 'anchor')
        raw_segments = record.get('segments')
        if not isinstance(raw_segments, list) or not raw_segments:
            _power_error('segments must be a non-empty ordered path', connection=number)
        segments = []
        for segment in raw_segments:
            if not isinstance(segment, (list, tuple)) or len(segment) != 2:
                _power_error('each segment must have two endpoints', connection=number)
            a, b = (_point(point, 'segment endpoint') for point in segment)
            if a == b or (a[0] != b[0] and a[1] != b[1]):
                _power_error('power path segments must be non-zero and orthogonal', connection=number)
            if segments and segments[-1][1] != a:
                _power_error('power path segments must form one ordered connected path', connection=number)
            segments.append((a, b))
        component = comp_index[identity[0]]
        source = tuple(placement.pin_world(component, identity[1]))
        if segments[0][0] != source or segments[-1][1] != anchor or source == anchor:
            _power_error('path endpoints must be the original pin and its marker anchor', connection=number)
        direction = tuple(par.DIR_VEC[placement.pin_dir(component, identity[1])])
        first = segments[0]
        if not _along((first[1][0]-first[0][0], first[1][1]-first[0][1]), direction):
            _power_error('path must leave the original pin in its declared direction', connection=number)
        operations = 0
        for i, (a, b) in enumerate(segments):
            for j in range(i + 1, len(segments)):
                operations += 1
                if operations % 256 == 0:
                    check_deadline(deadline, 'power_schema')
                c, d = segments[j]
                if segment_distance(a, b, c, d) > EPS:
                    continue
                if j == i + 1:
                    # Adjacent collinear subdivisions are fine; a reversal is not.
                    if ((a[0] == b[0]) != (c[0] == d[0])
                            or (b[0]-a[0])*(d[0]-c[0]) + (b[1]-a[1])*(d[1]-c[1]) > 0):
                        continue
                _power_error('power connection must be a simple path without loops or branches', connection=number)
        orientation = par.GEO_DOC[symbol]['orientations'][0]
        marker_pin = orientation['pins'][0]
        origin = anchor[0]-marker_pin['at']['x'], anchor[1]-marker_pin['at']['y']
        box = orientation['viewBoxAABB']
        marker_rect = _inflate((origin[0]+box['x'], origin[1]+box['y'],
                               origin[0]+box['x']+box['width'], origin[1]+box['y']+box['height']), scene.options.halo)
        marker_direction = tuple(par.DIR_VEC[marker_pin['direction']])
        marker_escape = _escape(anchor, marker_direction, marker_rect, scene.options.grid_step)
        last = segments[-1]
        if not (_along((last[0][0]-anchor[0], last[0][1]-anchor[1]), marker_direction)
                and on_segment(marker_escape, *last)):
            _power_error('last segment must approach the real marker pin through its outward port', connection=number)
        escape = _escape(source, direction, bodies[component], scene.options.grid_step)
        scene.power_corridors[identity] = Pin(net, component, identity[1], source, escape, direction)
        scene.power_markers.append({'identity': identity, 'net': net, 'rect': marker_rect,
                                    'entry': (marker_escape, anchor), 'origin': origin})
        normalized.append({'component_id': identity[0], 'pin': identity[1], 'net': net,
                           'symbol': symbol, 'anchor': anchor, 'segments': segments})
    missing = sorted(set(expected)-seen)
    if missing:
        _power_error('missing source power connections', missing=[list(identity) for identity in missing])
    return normalized


def build_scene(par, nets, placement, comp_index, options=None, deadline=None):
    options = options or RoutingOptions()
    scene = Scene(options=options)
    bodies = {}
    for index, component in enumerate(placement.comps):
        check_deadline(deadline, 'scene')
        cx, cy = placement.pos_of(index)
        box = par.GEO_DOC[component['symbol']]['orientations'][placement.orient[index]]['viewBoxAABB']
        rect = (cx+box['x'], cy+box['y'], cx+box['x']+box['width'], cy+box['y']+box['height'])
        bodies[index] = _inflate(rect, options.halo)
        scene.body_rects.append((index, bodies[index]))
    scene.power_connections = _normalize_power_connections(
        par, nets, placement, comp_index, scene, bodies, deadline)
    custom = ({(record['component_id'], record['pin']): record for record in scene.power_connections}
              if scene.power_connections is not None else None)
    for net, members in nets.items():
        symbol = par.power_marker_sym(net)
        points = []
        fixed = []
        for component_id, pin_name in members:
            check_deadline(deadline, 'scene')
            component = comp_index[component_id]
            point = tuple(placement.pin_world(component, pin_name))
            points.append(point)
            if symbol:
                if custom is not None:
                    record = custom[component_id, pin_name]
                    fixed.extend(record['segments'])
                    continue
                tip = (point[0], point[1] + (-par.STUB_LEN if symbol == 'vdd-port' else par.STUB_LEN))
                fixed.append((point, tip))
                box = par.GEO_DOC[symbol]['orientations'][0]['viewBoxAABB']
                marker = (tip[0]+box['x'], tip[1]+box['y'],
                          tip[0]+box['x']+box['width'], tip[1]+box['y']+box['height'])
                scene.marker_rects.append((net, _inflate(marker, options.halo)))
                continue
            direction = tuple(par.DIR_VEC[placement.pin_dir(component, pin_name)])
            rect = bodies[component]
            if direction[0]:
                edge = rect[2] + options.grid_step if direction[0] > 0 else rect[0] - options.grid_step
                escape = (max(point[0], edge) if direction[0] > 0 else min(point[0], edge), point[1])
            else:
                edge = rect[3] + options.grid_step if direction[1] > 0 else rect[1] - options.grid_step
                escape = (point[0], max(point[1], edge) if direction[1] > 0 else min(point[1], edge))
            pin = Pin(net, component, pin_name, point, escape, direction)
            scene.pins.setdefault(net, []).append(pin)
            if point != escape:
                fixed.append(pin.stub)
        scene.all_pins[net] = points
        scene.fixed_segments[net] = fixed
        if symbol:
            scene.power_nets.add(net)
    if custom is not None:
        scene.marker_rects.extend((marker['net'], marker['rect']) for marker in scene.power_markers)
    rectangles = [rect for _, rect in scene.body_rects + scene.marker_rects]
    if rectangles:
        scene.bounds = (min(r[0] for r in rectangles), min(r[1] for r in rectangles),
                        max(r[2] for r in rectangles), max(r[3] for r in rectangles))
    if custom is not None and scene.power_connections:
        points = [point for record in scene.power_connections for segment in record['segments'] for point in segment]
        scene.bounds = (min(scene.bounds[0], *(p[0] for p in points)),
                        min(scene.bounds[1], *(p[1] for p in points)),
                        max(scene.bounds[2], *(p[0] for p in points)),
                        max(scene.bounds[3], *(p[1] for p in points)))
    return scene


def _audit_power_connections(scene, add, deadline):
    if scene.power_connections is None:
        return
    operations = 0
    for record, marker in zip(scene.power_connections, scene.power_markers):
        check_deadline(deadline, 'power_geometry')
        identity = record['component_id'], record['pin']
        pin = scene.power_corridors[identity]
        for number, (a, b) in enumerate(record['segments']):
            for component, rect in scene.body_rects:
                operations += 1
                if operations % 256 == 0:
                    check_deadline(deadline, 'power_geometry')
                if segment_hits_rect(a, b, rect) and not (
                        component == pin.component and _in_corridor(a, b, rect, pin.stub)):
                    add('power_body', (record['net'],), a, component=component,
                        connection=list(identity), segment=(a, b))
            for other in scene.power_markers:
                if not segment_hits_rect(a, b, other['rect']):
                    continue
                own_entry = (other['identity'] == identity and number == len(record['segments']) - 1
                             and _in_corridor(a, b, other['rect'], marker['entry']))
                if not own_entry:
                    add('power_marker', (record['net'], other['net']), a,
                        connection=list(identity), marker=list(other['identity']), segment=(a, b))
        rect = marker['rect']
        for component, body in scene.body_rects:
            if (max(rect[0], body[0]) <= min(rect[2], body[2]) + EPS
                    and max(rect[1], body[1]) <= min(rect[3], body[3]) + EPS):
                add('power_marker_body', (record['net'],), record['anchor'],
                    connection=list(identity), component=component)
    for i, marker in enumerate(scene.power_markers):
        for other in scene.power_markers[i + 1:]:
            operations += 1
            if operations % 256 == 0:
                check_deadline(deadline, 'power_geometry')
            a, b = marker['rect'], other['rect']
            if max(a[0], b[0]) <= min(a[2], b[2]) + EPS and max(a[1], b[1]) <= min(a[3], b[3]) + EPS:
                add('power_marker_overlap', (marker['net'], other['net']),
                    markers=[list(marker['identity']), list(other['identity'])])


def analyze_routes(routed, scene, deadline=None):
    """Full wire separation contract, independent of arbitrary subdivisions."""
    routes = dict(routed)
    for net in scene.power_nets:
        routes[net] = scene.fixed_segments.get(net, [])
    check_deadline(deadline, 'geometry_validation')
    topologies = {net: canonical_topology(segments, scene.all_pins.get(net, ()), deadline=deadline)
                  for net, segments in routes.items()}
    violations, seen = [], set()
    crossing_points = set()
    overlap = proximity = 0.0

    def add(kind, nets, point=None, **details):
        names = tuple(sorted(str(net) for net in nets))
        location = tuple(point) if point is not None else None
        key = kind, names, location
        if key not in seen:
            seen.add(key)
            violations.append({'kind': kind, 'nets': list(names), 'point': location, **details})

    _audit_power_connections(scene, add, deadline)
    operations = 0
    for net, topology in topologies.items():
        check_deadline(deadline, 'geometry_validation')
        if net in scene.power_nets:
            continue
        for a, b in topology['segments']:
            for component, rect in scene.body_rects:
                operations += 1
                if operations % 256 == 0:
                    check_deadline(deadline, 'geometry_validation')
                if not scene.body_allowed(net, a, b, component, rect):
                    add('body', (net,), a, component=component, segment=(a, b))
            for owner, rect in scene.marker_rects:
                if segment_hits_rect(a, b, rect):
                    add('marker', (net, owner), a, segment=(a, b))
    names = sorted(topologies)
    for i, net in enumerate(names):
        check_deadline(deadline, 'geometry_validation')
        first = topologies[net]
        for owner in names[i+1:]:
            second = topologies[owner]
            for a, b in first['runs']:
                for c, d in second['runs']:
                    operations += 1
                    if operations % 256 == 0:
                        check_deadline(deadline, 'geometry_validation')
                    parallel = (a[0] == b[0]) == (c[0] == d[0])
                    distance = segment_distance(a, b, c, d)
                    if parallel:
                        vertical = a[0] == b[0]
                        axis = 1 if vertical else 0
                        shared = max(0, min(max(a[axis], b[axis]), max(c[axis], d[axis]))
                                     - max(min(a[axis], b[axis]), min(c[axis], d[axis])))
                        track_distance = abs(a[1-axis] - c[1-axis])
                        if shared and track_distance < EPS:
                            overlap += shared
                        elif shared and track_distance < scene.options.center_spacing - EPS:
                            proximity += shared
                        if distance < scene.options.center_spacing - EPS:
                            add('overlap' if shared and track_distance < EPS else 'wire_clearance',
                                (net, owner), a, distance=distance, segments=((a,b),(c,d)))
                        continue
                    point = intersection(a, b, c, d)
                    if point is None:
                        if distance < scene.options.center_spacing - EPS:
                            add('wire_clearance', (net, owner), a, distance=distance, segments=((a,b),(c,d)))
                        continue
                    proper = point not in (a, b, c, d)
                    if proper and net not in scene.power_nets and owner not in scene.power_nets:
                        crossing_points.add((net, owner, point))
                    clear = proper and not any(max(abs(point[0]-p[0]), abs(point[1]-p[1])) < scene.options.cross_clearance - EPS
                                               for p in first['structure_points'] | second['structure_points'])
                    fixed_cross = any(on_segment(point, *segment)
                                      for name in (net, owner) for segment in scene.fixed_segments.get(name, ()))
                    if not clear or fixed_cross:
                        add('crossing_clearance' if proper else 'foreign_contact', (net,owner), point)
            # Endpoints, bends and branches need a larger readable keepout than
            # merely parallel tracks; this also catches near misses at corners.
            for source, target, points in ((net, owner, first['pin_points'] | first['junction_points']),
                                          (owner, net, second['pin_points'] | second['junction_points'])):
                for point in points:
                    if any(point_segment_distance(point, a, b) < scene.options.cross_clearance - EPS
                           for a, b in topologies[target]['runs']):
                        add('structure_clearance', (net, owner), point, owner=source)
            for source, target in ((net, owner), (owner, net)):
                for a, b in topologies[source]['runs']:
                    for c, d in scene.fixed_segments.get(target, ()):
                        if segment_distance(a,b,c,d) < scene.options.center_spacing - EPS:
                            add('fixed_stub_clearance', (net,owner), c, segment=(a,b))
    return {'violations': violations, 'crossings': len(crossing_points),
            'overlap': overlap, 'proximity': proximity,
            'body_violations': sum(item['kind'] in ('body','marker','power_body','power_marker',
                                                   'power_marker_body','power_marker_overlap') for item in violations),
            'power_connections_checked': len(scene.power_connections or ()),
            'topologies': topologies}
