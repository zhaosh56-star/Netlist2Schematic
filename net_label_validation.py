"""Independent checks for a labelled view of an already valid routed circuit.

The ordinary router contract is deliberately unchanged.  This module checks
the second drawing contract: physical local islands plus explicit same-net
labels must reproduce exactly the original visible terminal connectivity.
"""
from __future__ import annotations

from collections import defaultdict
from heapq import heappop, heappush
from math import ceil, isfinite

from routing_rules import (EPS, analyze_routes, canonical_topology, check_deadline,
                           intersection, merge_runs, on_segment,
                           segment_distance, segment_hits_rect)
from sa_render import _overlap, _text_box, _wire_in_box

VERSION = 'net-label-view-v1'
MAX_GROUPS = 4
FONT_SIZE = 14
TEXT_GAP = 6
LEAD_LENGTHS = (24, 36, 48)


def point(value):
    if (not isinstance(value, (tuple, list)) or len(value) != 2 or
            any(type(v) not in (int, float) or not isfinite(v) for v in value)):
        raise ValueError('Net-label point must contain two finite numbers')
    return tuple(value)


def edge(a, b):
    return tuple(sorted((tuple(a), tuple(b))))


def canonical_routes(routed, deadline=None):
    if not isinstance(routed,(dict,list,tuple)):
        raise ValueError('Malformed labelled route collection')
    rows = list(routed.items()) if isinstance(routed, dict) else list(routed)
    if any(not isinstance(row,(tuple,list)) or len(row)!=2 for row in rows):
        raise ValueError('Malformed labelled route record')
    if any(not isinstance(row[0],str) for row in rows):
        raise ValueError('Malformed labelled net identity')
    if len({n for n, _ in rows}) != len(rows):
        raise ValueError('Duplicate net in labelled routes')
    result = {}
    for net, runs in rows:
        if not isinstance(net, str) or not isinstance(runs, (list, tuple)):
            raise ValueError('Malformed labelled route record')
        result[net] = merge_runs(runs, deadline=deadline)
    return dict(sorted(result.items()))


def anchors(scene, net):
    result = {tuple(p) for p in scene.all_pins.get(net, ())}
    result.update(p.escape for p in scene.pins.get(net, ()))
    result.update(v for e in scene.fixed_segments.get(net, ()) for v in e)
    return result


def covered(segment, runs):
    return any(on_segment(segment[0], a, b) and on_segment(segment[1], a, b)
               for a, b in runs)


def remove_segments(scene, net, lines, removed, deadline=None):
    cuts = anchors(scene, net) | {p for e in removed for p in e}
    topology = canonical_topology(lines, pins=cuts, deadline=deadline)
    return [e for e in topology['segments'] if not covered(e, removed)]


def terminals_for(net, terminal_ids):
    rows = terminal_ids.get(net, ())
    result = []
    identities = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('component'), str) or not isinstance(row.get('pin'), str):
            raise ValueError('Malformed original terminal identity')
        identity = (row['component'], row['pin'])
        if identity in identities:
            raise ValueError('Duplicate original terminal identity')
        identities.add(identity)
        result.append((identity, point(row['point'])))
    return sorted(result)


def local_groups(scene, net, segments, terminal_ids, deadline=None):
    """Return local physical groups, including legitimate isolated single pins."""
    terms = terminals_for(net, terminal_ids)
    topology = canonical_topology(segments, pins=anchors(scene, net), deadline=deadline)
    adjacency = {p: set(ns) for p, ns in topology['adjacency'].items()}
    for _, p in terms:
        adjacency.setdefault(p, set())
    remaining = set(adjacency)
    groups = []
    while remaining:
        check_deadline(deadline, 'net_label_components')
        start = min(remaining)
        remaining.remove(start)
        found, todo = {start}, [start]
        while todo:
            for neighbor in adjacency[todo.pop()]:
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    found.add(neighbor)
                    todo.append(neighbor)
        members = sorted(identity for identity, p in terms if p in found)
        lines = [e for e in topology['segments'] if e[0] in found and e[1] in found]
        groups.append({'points': found, 'segments': lines, 'terminals': members})
    groups.sort(key=lambda group: (min(group['points']), group['terminals']))
    for number, group in enumerate(groups):
        group['id'] = f'{net}:g{number}'
    return topology, groups


def protected_pairs(scene, routes, terminal_ids, scale, deadline=None):
    """Pairs whose original routed distance is at most two physical pitches."""
    result = {}
    for net, runs in routes.items():
        if net in scene.power_nets:
            continue
        terms = terminals_for(net, terminal_ids)
        adjacency = canonical_topology(runs, pins=anchors(scene, net), deadline=deadline)['adjacency']
        pairs = []
        for number, (identity, source) in enumerate(terms):
            check_deadline(deadline, 'net_label_local_pairs')
            distances, todo = {source: 0.}, [(0., source)]
            while todo:
                distance, p = heappop(todo)
                if distance != distances[p] or distance > 2*scale + EPS:
                    continue
                for q in adjacency.get(p, ()):
                    following = distance + abs(p[0]-q[0]) + abs(p[1]-q[1])
                    if following <= 2*scale + EPS and following < distances.get(q, float('inf')):
                        distances[q] = following
                        heappush(todo, (following, q))
            pairs.extend((identity, other) for other, target in terms[number+1:]
                         if distances.get(target, float('inf')) <= 2*scale + EPS)
        result[net] = pairs
    return result


def label_position(tip, direction):
    x, y = tip
    if direction == (1, 0):
        return x+8, y+4, 'start'
    if direction == (-1, 0):
        return x-8, y+4, 'end'
    if direction == (0, -1):
        return x, y-12, 'middle'
    if direction == (0, 1):
        return x, y+22, 'middle'
    raise ValueError('Invalid net-label lead direction')


def wire_metrics(routes, analysis=None, deadline=None):
    metrics = dict(wirelength=0., crossings=0, bends=0, body_pen=0, overlap=0., proximity=0.)
    for net, lines in routes.items():
        topology = canonical_topology(lines, deadline=deadline)
        metrics['wirelength'] += sum(abs(a[0]-b[0])+abs(a[1]-b[1]) for a, b in topology['runs'])
        for p, ns in topology['adjacency'].items():
            if len(ns) == 2:
                a, b = sorted(ns)
                metrics['bends'] += int((a[0] == p[0]) != (b[0] == p[0]))
    if analysis is not None:
        metrics.update(crossings=analysis['crossings'], body_pen=analysis['body_violations'],
                       overlap=analysis['overlap'], proximity=analysis['proximity'])
    return metrics


def display_routes(view):
    """Canonical visible wires, including every actual label leader."""
    routes = canonical_routes(view['base_routes'])
    for label in view.get('labels', ()):
        net = label['net']
        if net not in routes:
            raise ValueError('Net-label leader names an unknown network')
        routes[net].append((point(label['anchor']), point(label['tip'])))
    return [(net, merge_runs(lines)) for net, lines in sorted(routes.items())]


def _touch_only_at_anchor(a, b, c, d):
    """Allow a same-island T at a, without overlap or a second contact."""
    if (a[0] == b[0]) == (c[0] == d[0]):
        vertical = a[0] == b[0]
        if a[not vertical] != c[not vertical]:
            return True
        axis = int(vertical)
        low = max(min(a[axis], b[axis]), min(c[axis], d[axis]))
        high = min(max(a[axis], b[axis]), max(c[axis], d[axis]))
        return high < low-EPS or (abs(high-low) <= EPS and on_segment(a, c, d)
                                  and abs(a[axis]-low) <= EPS)
    hit = intersection(a, b, c, d)
    return hit is None or hit == a


def lead_error(scene, net, anchor, tip, routes, previous_labels=()):
    """No crossing is permitted on a short label leader."""
    if anchor == tip or (anchor[0] != tip[0] and anchor[1] != tip[1]):
        return 'invalid_leader'
    for owner, rect in scene.body_rects:
        if segment_hits_rect(anchor, tip, rect):
            return 'leader_body'
    for owner, rect in scene.marker_rects:
        if segment_hits_rect(anchor, tip, rect):
            return 'leader_marker'
    wires = [(name, a, b) for name, lines in routes.items() for a, b in lines]
    wires += [(name, a, b) for name in scene.power_nets for a, b in scene.fixed_segments.get(name, ())]
    wires += [(row['net'], point(row['anchor']), point(row['tip'])) for row in previous_labels]
    for name, a, b in wires:
        if name == net and on_segment(anchor, a, b):
            if not _touch_only_at_anchor(anchor, tip, a, b):
                return 'leader_same_net_overlap'
        elif segment_distance(anchor, tip, a, b) < scene.options.center_spacing-EPS:
            return 'leader_wire_clearance'
    return None


def text_error(scene, bounds, routes, labels=(), text_obstacles=(), *, own_label=None):
    if any(_overlap(bounds, rect, TEXT_GAP) for _, rect in scene.body_rects + scene.marker_rects):
        return 'label_body'
    if any(_overlap(bounds, tuple(rect), TEXT_GAP) for rect in text_obstacles):
        return 'label_existing_text'
    if any(_overlap(bounds, tuple(row['bounds']), TEXT_GAP) for row in labels):
        return 'label_text_overlap'
    lines = [e for values in routes.values() for e in values]
    lines += [e for net in scene.power_nets for e in scene.fixed_segments.get(net, ())]
    lines += [(point(row['anchor']), point(row['tip'])) for row in labels]
    if own_label is not None:
        lines.append((point(own_label['anchor']),point(own_label['tip'])))
    if any(_wire_in_box(a, b, bounds, margin=TEXT_GAP) for a, b in lines):
        return 'label_wire_overlap'
    return None


def validate_view(scene, original_routes, view, terminal_ids, *, text_obstacles=(), deadline=None):
    """Reconstruct all semantics from real pins and geometry, never saved flags."""
    check_deadline(deadline, 'net_label_validation')
    if not isinstance(view, dict) or view.get('version') != VERSION:
        raise ValueError('Unsupported net-label view version')
    scale = view.get('scale')
    if type(scale) not in (int, float) or not isfinite(scale) or scale <= 0:
        raise ValueError('Invalid net-label physical pitch')
    original = canonical_routes(original_routes, deadline)
    base = canonical_routes(view.get('base_routes', {}), deadline)
    if set(base) != set(original):
        raise ValueError('Labelled view changed the network set')
    baseline_analysis = analyze_routes(list(original.items()), scene, deadline=deadline)
    if baseline_analysis['violations']:
        raise ValueError('Original route geometry is not valid')
    original_metrics = wire_metrics(original, baseline_analysis, deadline)
    omissions = defaultdict(list)
    replay = {net:list(lines) for net,lines in original.items()}
    omission_rows=view.get('omitted_paths',())
    if not isinstance(omission_rows,(list,tuple)):
        raise ValueError('Malformed omitted-path list')
    for omission in omission_rows:
        if not isinstance(omission, dict) or omission.get('net') not in original:
            raise ValueError('Omission names an unknown net')
        net = omission['net']
        path = [point(p) for p in omission.get('path', ())]
        if len(path) < 2 or net in scene.power_nets:
            raise ValueError('Malformed or power-net omission')
        omitted = list(zip(path, path[1:]))
        canonical_routes({net: omitted}, deadline)
        if any(p in anchors(scene, net) for p in path[1:-1]):
            raise ValueError('Omitted path contains an original pin or fixed exit')
        source_topology = canonical_topology(replay[net], pins=anchors(scene, net), deadline=deadline)
        if any(len(source_topology['adjacency'].get(p, ())) != 2 for p in path[1:-1]):
            raise ValueError('Omitted path contains a branch')
        for p,neighbors in source_topology['adjacency'].items():
            if (p not in (path[0],path[-1]) and any(on_segment(p,a,b) for a,b in omitted)
                    and (p in anchors(scene,net) or len(neighbors)!=2)):
                raise ValueError('Omitted interval passes through an original anchor or branch')
        length=sum(abs(a[0]-b[0])+abs(a[1]-b[1]) for a,b in omitted)
        span=abs(path[0][0]-path[-1][0])+abs(path[0][1]-path[-1][1])
        crossings=set()
        for other,other_lines in replay.items():
            if other==net:
                continue
            for a,b in omitted:
                for c,d in merge_runs(other_lines,deadline=deadline):
                    hit=intersection(a,b,c,d)
                    if hit is not None and hit not in (a,b,c,d):
                        crossings.add((other,hit))
        if not (length>=6*scale-EPS or
                (length>=4*scale-EPS and length-span>=1.5*scale-EPS) or
                (length>=3*scale-EPS and len(crossings)>=3)):
            raise ValueError('Omitted path does not meet the conservative label policy')
        for a, b in omitted:
            if not covered((a,b), original[net]):
                raise ValueError('Omission is not in original routing')
            if any(segment_distance(a,b,c,d) < EPS and _parallel_overlap(a,b,c,d) > EPS
                   for c,d in scene.fixed_segments.get(net, ())):
                raise ValueError('Omission cuts an original fixed exit')
        if any(not covered(e, merge_runs(replay[net],deadline=deadline)) for e in omitted):
            raise ValueError('Duplicate or overlapping omitted paths')
        _, before_groups = local_groups(scene,net,replay[net],terminal_ids,deadline)
        after = remove_segments(scene,net,replay[net],omitted,deadline)
        _, after_groups = local_groups(scene,net,after,terminal_ids,deadline)
        if len(after_groups) != len(before_groups)+1:
            raise ValueError('Omitted path is not a bridge between local terminal groups')
        replay[net] = after
        omissions[net].extend(omitted)
    for net, lines in base.items():
        check_deadline(deadline, 'net_label_coverage')
        if any(not covered(e, original[net]) for e in lines):
            raise ValueError('Local wires are not a subset of original routing')
        if merge_runs(lines + omissions[net], deadline=deadline) != original[net]:
            raise ValueError('Visible and omitted paths do not cover original routing')
        if merge_runs(replay[net],deadline=deadline) != lines:
            raise ValueError('Omitted bridge replay differs from visible local wires')
        if any(_parallel_overlap(a,b,c,d) > EPS for a,b in lines for c,d in omissions[net]):
            raise ValueError('Omitted path is still drawn')
        if any(not covered(e, lines) for e in scene.fixed_segments.get(net, ())):
            raise ValueError('A fixed original-pin exit is missing')
    labels = view.get('labels', [])
    declared = view.get('groups', [])
    if not isinstance(labels, list) or not isinstance(declared, list):
        raise ValueError('Malformed label or local-group list')
    if any(not isinstance(g,dict) or not isinstance(g.get('net'),str) or
           not isinstance(g.get('id'),str) or not isinstance(g.get('terminals'),list)
           for g in declared):
        raise ValueError('Malformed declared local-group record')
    actual_groups, by_net = [], {}
    for net, lines in base.items():
        if net in scene.power_nets:
            if lines != original[net] or omissions[net]:
                raise ValueError('Power routing was changed')
            continue
        source_points = set(scene.all_pins.get(net, ()))
        if {p for _, p in terminals_for(net, terminal_ids)} != source_points:
            raise ValueError('Terminal identities do not describe all original pins')
        _, original_groups = local_groups(scene,net,original[net],terminal_ids,deadline)
        if len(original_groups) != 1 or not original_groups[0]['terminals']:
            raise ValueError('Original routed net is not physically connected')
        _, groups = local_groups(scene, net, lines, terminal_ids, deadline)
        if any(not group['terminals'] for group in groups):
            raise ValueError('Visible island contains no original terminal')
        if not groups or len(groups) > MAX_GROUPS:
            raise ValueError('Invalid number of local groups')
        by_net[net] = groups
        if len(groups) > 1:
            actual_groups.extend({'net':net,'id':g['id'], 'terminals':[list(i) for i in g['terminals']]}
                                 for g in groups)
        elif omissions[net]:
            raise ValueError('Omission only removes a cycle or redundant route')
    if sorted(declared, key=lambda g:(g['net'],g['id'])) != sorted(actual_groups,key=lambda g:(g['net'],g['id'])):
        raise ValueError('Saved local groups differ from physical terminal groups')
    pairs = protected_pairs(scene, original, terminal_ids, scale, deadline)
    for net, groups in by_net.items():
        ownership = {identity: g['id'] for g in groups for identity in g['terminals']}
        if any(ownership[a] != ownership[b] for a,b in pairs[net]):
            raise ValueError('Nearby original terminals were separated')
    expected = {(g['net'],g['id']) for g in actual_groups}
    actual = []
    previous = []
    for label in labels:
        check_deadline(deadline, 'net_label_attachment')
        if not isinstance(label, dict) or label.get('net') not in by_net:
            raise ValueError('Unknown label net')
        net, gid = label['net'], label.get('group_id')
        key = net, gid
        if key not in expected or key in actual:
            raise ValueError('Extra, duplicate or wrong-island label')
        if label.get('text') != net:
            raise ValueError('Visible net-label text does not match the original net')
        a,b = point(label['anchor']),point(label['tip'])
        group = next(g for g in by_net[net] if g['id']==gid)
        if not any(on_segment(a,*e) for e in group['segments']) and a not in group['points']:
            raise ValueError('Label is not attached to its local group')
        length = abs(a[0]-b[0])+abs(a[1]-b[1])
        legal_lengths = {ceil(v/scene.options.grid_step)*scene.options.grid_step for v in LEAD_LENGTHS}
        if length not in legal_lengths:
            raise ValueError('Label leader has an invalid length')
        direction = ((1 if b[0]>a[0] else -1),0) if a[1]==b[1] else (0,(1 if b[1]>a[1] else -1))
        x,y,align = label_position(b,direction)
        if (label.get('x'),label.get('y'),label.get('anchor_style')) != (x,y,align):
            raise ValueError('Label text is detached from its leader')
        bounds = _text_box(x,y,net,align,size=FONT_SIZE)
        if list(label.get('bounds',())) != list(bounds):
            raise ValueError('Label text bounds were not computed from its visible text')
        error = lead_error(scene,net,a,b,base,previous)
        if error:
            raise ValueError(error)
        error = text_error(scene,bounds,base,previous,text_obstacles,own_label=label)
        if error:
            raise ValueError(error)
        previous.append(label)
        actual.append(key)
    if set(actual) != expected:
        raise ValueError('A disconnected local group is missing its same-net label')
    visible = dict(display_routes(view))
    analysis = analyze_routes(list(visible.items()),scene,deadline=deadline)
    if analysis['violations']:
        raise ValueError('Visible geometry violates routing rules: '+str(analysis['violations'][:3]))
    metrics = wire_metrics(visible,analysis,deadline)
    if labels and (metrics['wirelength'] >= original_metrics['wirelength']-EPS or
                   metrics['crossings'] > original_metrics['crossings']):
        raise ValueError('Net-label view does not strictly reduce wirelength without extra crossings')
    metrics.update(label_count=len(labels),
                   omitted_wirelength=original_metrics['wirelength']-metrics['wirelength'])
    return {'valid':True,'metrics':metrics,'original_metrics':original_metrics,
            'groups':actual_groups,'label_count':len(labels),
            'original_pins_preserved':True,'fixed_exits_preserved':True,
            'nearby_pairs_preserved':True,'connected_by_labels':True,
            'violations':[]}


def _parallel_overlap(a,b,c,d):
    if (a[0]==b[0]) != (c[0]==d[0]):
        return 0.
    axis = 1 if a[0]==b[0] else 0
    if a[1-axis] != c[1-axis]:
        return 0.
    return max(0.,min(max(a[axis],b[axis]),max(c[axis],d[axis]))-
               max(min(a[axis],b[axis]),min(c[axis],d[axis])))
