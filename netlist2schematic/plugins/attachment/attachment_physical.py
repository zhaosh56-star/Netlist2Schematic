"""Actual guest regions and owned envelopes for post-SA local coordinates."""
from collections import defaultdict

EPS = 1e-8
ACTIVE = frozenset(('nmos', 'pmos', 'npn', 'pnp'))


def floating(placement, i):
    offsets = getattr(placement, 'passive_local_offsets', None)
    return offsets is not None and any(offsets[i])


def row_corridor(placement, row):
    axis = placement.row_y
    if not 0 <= row < len(axis):
        return None
    before = (axis[row]-axis[row-1])/2 if row else (axis[1]-axis[0])/2 if len(axis)>1 else 75
    after = (axis[row+1]-axis[row])/2 if row+1 < len(axis) else before
    return axis[row]-before, axis[row]+after


def physical_member_in_band(placement, i, band):
    """Use the same port-derived allowed rows, now as physical access corridors.

    This does not expand to an owner's whole multi-row rectangle. It merely
    stops a guest's old row index from standing in for its actual y position.
    Exact body, stub and marker clearance remains independently enforced.
    """
    lo_x, hi_x, lo_row, hi_row, allowed = band
    x, y = placement.pos_of(i)
    horizontal = lo_x+EPS < x < hi_x-EPS and any(
        interval is not None and interval[0]-EPS <= y <= interval[1]+EPS
        for interval in (row_corridor(placement, row) for row in allowed))
    vertical = (placement.row_y[lo_row]+EPS < y < placement.row_y[hi_row]-EPS
                and lo_x-EPS <= x <= hi_x+EPS)
    return horizontal or vertical


def body_rect(par, placement, i):
    x, y = placement.pos_of(i)
    box = par.GEO_DOC[placement.comps[i]['symbol']]['orientations'][placement.orient[i]]['viewBoxAABB']
    return x+box['x'], y+box['y'], x+box['x']+box['width'], y+box['y']+box['height']


def floating_leaves_clear(par, placement, permitted, embedded):
    if not hasattr(placement, 'passive_local_offsets'):
        return True
    guests = [i for i in range(len(placement.comps)) if floating(placement, i)]
    if not guests:
        return True
    boxes = {i: body_rect(par, placement, i) for i in range(len(placement.comps))}
    for block in placement.blocks:
        own = frozenset(block)
        by_row = defaultdict(list)
        for i in block:
            if placement.comps[i]['symbol'] in ACTIVE:
                by_row[placement.cell_of[i][0]].append(i)
        for members in by_row.values():
            ordered = sorted(members, key=lambda i: placement.pos_of(i)[0])
            for left, right in zip(ordered, ordered[1:]):
                a, b = boxes[left], boxes[right]
                strip = a[2], max(a[1], b[1]), b[0], min(a[3], b[3])
                if strip[0] >= strip[2]-EPS or strip[1] >= strip[3]-EPS:
                    continue
                for i in guests:
                    if i in own:
                        continue
                    box = boxes[i]
                    inside = max(box[0], strip[0]) < min(box[2], strip[2])-EPS and max(
                        box[1], strip[1]) < min(box[3], strip[3])-EPS
                    if inside and not any(placement.comps[i]['id'] in group['members']
                            and placement.comps[i]['id'] in embedded(group)
                            for group in permitted.get(own, ())):
                        return False
    return True


def owned_footprints(par, scene, nets, index):
    """Body + actual owned marker + fixed exits, without same-net exemptions."""
    if scene.power_connections is not None:
        raise ValueError('Owned footprint packing requires standard power geometry')
    result = defaultdict(list)
    for owner, rect in scene.body_rects:
        result[owner].append(rect)
    marker_owners = [index[name] for net, members in nets.items() if par.power_marker_sym(net)
                     for name, pin in members]
    if len(marker_owners) != len(scene.marker_rects):
        raise ValueError('Power marker ownership does not match the source terminals')
    for (_, rect), owner in zip(scene.marker_rects, marker_owners):
        result[owner].append(rect)
    for net, members in nets.items():
        if net in scene.power_nets:
            pairs = [(index[name], segment) for (name, pin), segment in zip(members, scene.fixed_segments[net])]
        else:
            pairs = [(pin.component, pin.stub) for pin in scene.pins.get(net, ())]
        for owner, (a, b) in pairs:
            result[owner].append((min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])))
    return {owner: (min(r[0] for r in rects), min(r[1] for r in rects),
                    max(r[2] for r in rects), max(r[3] for r in rects)) for owner, rects in result.items()}
