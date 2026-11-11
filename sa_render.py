"""Both SVG styles render the same validated final route."""
from __future__ import annotations

from collections import defaultdict
from html import escape
from pathlib import Path
from unicodedata import east_asian_width


def _text_box(x, y, text, anchor='start', size=14):
    width = sum(size if east_asian_width(char) in ('W', 'F') else size*.64 for char in text)
    left = x-width if anchor == 'end' else x-width/2 if anchor == 'middle' else x
    return left, y-size, left+width, y+size*.25


def _overlap(a, b, margin=0):
    return (max(a[0]-margin, b[0]) < min(a[2]+margin, b[2])
            and max(a[1]-margin, b[1]) < min(a[3]+margin, b[3]))


def _wire_in_box(a, b, box, margin=4):
    x0, y0, x1, y1 = box
    x0, y0, x1, y1 = x0-margin, y0-margin, x1+margin, y1+margin
    if a[0] == b[0]:
        return x0 <= a[0] <= x1 and max(min(a[1], b[1]), y0) <= min(max(a[1], b[1]), y1)
    return y0 <= a[1] <= y1 and max(min(a[0], b[0]), x0) <= min(max(a[0], b[0]), x1)


def _choose_text(text, candidates, bodies, wires, occupied, *, anchor_point=None):
    best = None
    for order, (x, y, align) in enumerate(candidates):
        box = _text_box(x, y, text, align)
        body_hits = sum(_overlap(box, body, 4) for body in bodies)
        text_hits = sum(_overlap(box, other, 3) for other in occupied)
        wire_hits = sum(_wire_in_box(a, b, box) for a, b in wires)
        distance = abs(x-anchor_point[0])+abs(y-anchor_point[1]) if anchor_point else 0
        key = body_hits*10000 + text_hits*2000 + wire_hits*100 + distance, order
        if best is None or key < best[0]:
            best = key, {'x': x, 'y': y, 'anchor': align, 'text': text, 'bounds': box}
    return best[1] if best else None


def _near_box(box):
    x0, y0, x1, y1 = box
    cx, cy = (x0+x1)/2, (y0+y1)/2
    for extra in (0, 12, 24, 40):
        gap = 8+extra
        yield x1+gap, cy+4, 'start'
        yield x0-gap, cy+4, 'end'
        yield cx, y0-gap, 'middle'
        yield cx, y1+14+gap, 'middle'
        yield x1+gap, y0-gap, 'start'
        yield x0-gap, y0-gap, 'end'


def _near_pin(point, direction):
    x, y = point
    for extra in (0, 12, 24):
        gap = 10+extra
        values = [(x+gap, y-8, 'start'), (x-gap, y-8, 'end'),
                  (x+gap, y+20, 'start'), (x-gap, y+20, 'end'),
                  (x, y-gap, 'middle'), (x, y+14+gap, 'middle')]
        if direction == 'west':
            values = [values[1], values[0], values[3], values[2], *values[4:]]
        elif direction == 'north':
            values = [values[4], *values[:4], values[5]]
        elif direction == 'south':
            values = [values[5], *values[:5]]
        yield from values


def _presentation_labels(par, placement, comps, nets, index, routed, stubs,
                         power_markers, power_runs, supply_markers):
    bodies, wires, occupied = [], [], []
    for i, comp in enumerate(comps):
        x, y = placement.pos_of(i)
        box = par.GEO_DOC[comp['symbol']]['orientations'][placement.orient[i]]['viewBoxAABB']
        bodies.append((x+box['x'], y+box['y'], x+box['x']+box['width'], y+box['y']+box['height']))
    for _, box, _, _ in supply_markers:
        bodies.append(box)
    for _, runs in routed:
        wires.extend(runs)
    wires.extend((tip, endpoint) for _, tip, endpoint in stubs)
    for runs in power_runs.values():
        wires.extend(runs)
    components = []
    for i, comp in enumerate(comps):
        label = _choose_text(comp['id'], _near_box(bodies[i]), bodies, wires, occupied,
                             anchor_point=placement.pos_of(i))
        components.append(label)
        occupied.append(label['bounds'])
    supplies = []
    names = getattr(placement, 'presentation_supply_names', {})
    names = names if isinstance(names, dict) else {}
    for net, box, point, identity in supply_markers:
        text = names.get(net)
        if not isinstance(text, str) or not text:
            text = 'GND' if par.power_marker_sym(net) == 'ground' else net
        label = _choose_text(text, _near_box(box), bodies, wires, occupied, anchor_point=point)
        label.update(net=net, identity=identity)
        supplies.append(label)
        occupied.append(label['bounds'])
    ports = []
    mapping = getattr(placement, 'presentation_ports', {})
    mapping = mapping if isinstance(mapping, dict) else {}
    for net, record in sorted(mapping.items()):
        if (net not in nets or par.power_marker_sym(net) or not isinstance(record, dict)
                or record.get('source') not in ('formal', 'voltage_source')
                or not isinstance(record.get('name'), str) or not record['name']):
            continue
        choices = []
        for name, pin in sorted(nets[net]):
            if name not in index:
                continue
            point = placement.pin_world(index[name], pin)
            direction = placement.pin_dir(index[name], pin)
            label = _choose_text(record['name'], _near_pin(point, direction), bodies,
                                 wires, occupied, anchor_point=point)
            body_hits = sum(_overlap(label['bounds'], body, 4) for body in bodies)
            text_hits = sum(_overlap(label['bounds'], other, 3) for other in occupied)
            wire_hits = sum(_wire_in_box(a, b, label['bounds']) for a, b in wires)
            choices.append(((body_hits, text_hits, wire_hits,
                             abs(label['x']-point[0])+abs(label['y']-point[1]), name, pin),
                            label, point, name, pin))
        if not choices:
            continue
        _, label, point, name, pin = min(choices, key=lambda value: value[0])
        label.update(net=net, point=tuple(point), component=name, pin=pin)
        ports.append(label)
        occupied.append(label['bounds'])
    return components, supplies, ports


def _clean_text(label, fmt, *, kind, attributes='', local_origin=None):
    x, y = label['x'], label['y']
    if local_origin:
        x, y = x-local_origin[0], y-local_origin[1]
    bounds = ' '.join(fmt(value) for value in label['bounds'])
    return (f'<text data-presentation-kind="{kind}" data-label-box="{bounds}"{attributes} '
            f'x="{fmt(x)}" y="{fmt(y)}" text-anchor="{label["anchor"]}" '
            f'font-family="monospace" font-size="14" fill="#111">{escape(label["text"])}</text>')


def net_label_render_hints(par, placement, comps, nets, index, routed, validation):
    """Freeze clean text before a display-only wire transformation."""
    stubs, markers, power_runs, supplies = [], [], {}, []
    custom = getattr(placement, 'power_connections', None)
    if custom is None:
        for net, members in nets.items():
            symbol = par.power_marker_sym(net)
            if not symbol:
                continue
            for cid, pin in members:
                endpoint = placement.pin_world(index[cid], pin)
                tip = (endpoint[0], endpoint[1] + (-par.STUB_LEN if symbol == 'vdd-port' else par.STUB_LEN))
                stubs.append((symbol, tip, endpoint))
                box = par.GEO_DOC[symbol]['orientations'][0]['viewBoxAABB']
                supplies.append((net, (tip[0]+box['x'], tip[1]+box['y'],
                    tip[0]+box['x']+box['width'], tip[1]+box['y']+box['height']), tip, (cid, pin)))
    else:
        from routing_rules import RoutingOptions, build_scene, canonical_topology
        scene = build_scene(par, nets, placement, index,
                            options=RoutingOptions(**validation.get('parameters', {})))
        for connection in scene.power_connections:
            symbol, anchor = connection['symbol'], connection['anchor']
            local = par.GEO_DOC[symbol]['orientations'][0]['pins'][0]['at']
            origin = (anchor[0]-local['x'], anchor[1]-local['y'])
            box = par.GEO_DOC[symbol]['orientations'][0]['viewBoxAABB']
            label = (origin[0]+box['x']+box['width']+6, origin[1]+8)
            markers.append((connection, origin, label))
            power_runs.setdefault(connection['net'], []).extend(connection['segments'])
            supplies.append((connection['net'], (origin[0]+box['x'], origin[1]+box['y'],
                origin[0]+box['x']+box['width'], origin[1]+box['y']+box['height']),
                anchor, (connection['component_id'], connection['pin'])))
        power_runs = {net: canonical_topology(runs)['segments'] for net, runs in power_runs.items()}
    component_text, supply_text, port_text = _presentation_labels(
        par, placement, comps, nets, index, routed, stubs, markers, power_runs, supplies)
    return {'component_text': component_text, 'supply_text': supply_text, 'port_text': port_text,
            'text_obstacles': [row['bounds'] for row in component_text+supply_text+port_text]}


def segs_to_paths(segments):
    adjacency = defaultdict(set)
    for a, b in segments:
        if a != b:
            adjacency[a].add(b)
            adjacency[b].add(a)
    visited = set()
    paths = []
    for start in sorted(adjacency, key=lambda p: (len(adjacency[p]), p)):
        for neighbor in sorted(adjacency[start]):
            if frozenset((start, neighbor)) in visited:
                continue
            path = [start, neighbor]
            visited.add(frozenset((start, neighbor)))
            while True:
                previous, current = path[-2:]
                choices = [p for p in adjacency[current]
                           if frozenset((current, p)) not in visited]
                if not choices:
                    break
                vertical = previous[0] == current[0]
                choices.sort(key=lambda p: ((p[0] == current[0]) != vertical, p))
                target = choices[0]
                visited.add(frozenset((current, target)))
                path.append(target)
            paths.append(path)
    return paths


def render_svg(par, placement, comps, nets, comp_index, routed, metrics, out,
               *, clean=False, structs=(), validation=None, net_label_view=None):
    if net_label_view is not None:
        from net_label_validation import display_routes
        from routing_rules import canonical_topology
        # Include new leader T junctions in path extraction and branch dots;
        # merged drawing runs alone do not expose an interior branch endpoint.
        routed = [(net, canonical_topology(lines)['segments'])
                  for net, lines in display_routes(net_label_view)]
        metrics = net_label_view['display_metrics']
    clean_presentation = (net_label_view is not None or
        clean and getattr(placement, 'presentation_style', 'legacy') == 'clean')
    wire_width = (validation or {}).get('parameters', {}).get('wire_width', 2.5)
    stage = getattr(placement, 'routing_stage', 'normal')
    bus_layout = ((validation or {}).get('router') == 'constructive-bus-v1'
                  or stage == 'bus_fallback')
    definitions = {symbol: par.effective_symbol(par.load_def(symbol))
                   for symbol in sorted({comp['symbol'] for comp in comps}
                                        | {'vdd-port', 'ground'})}
    xs, ys, labels = [], [], []
    for i, comp in enumerate(comps):
        cx, cy = placement.pos_of(i)
        box = par.GEO_DOC[comp['symbol']]['orientations'][placement.orient[i]]['viewBoxAABB']
        label_x = box['x'] + box['width'] + 8
        label_y = max(box['y'] + box['height'] + 14, 32)
        labels.append((label_x, label_y))
        if clean_presentation:
            xs.extend((cx + box['x'], cx + box['x'] + box['width']))
            ys.extend((cy + box['y'], cy + box['y'] + box['height']))
        else:
            xs.extend((cx + box['x'], cx + box['x'] + box['width'],
                       cx + label_x + len(comp['id']) * 7.5))
            ys.extend((cy + box['y'], cy + box['y'] + box['height'], cy + label_y + 4))
    stubs, power_markers, power_runs = [], [], {}
    supply_markers = []
    custom_power = getattr(placement, 'power_connections', None)
    if custom_power is None:
        for net, members in nets.items():
            symbol = par.power_marker_sym(net)
            if not symbol:
                continue
            for cid, pin in members:
                endpoint = placement.pin_world(comp_index[cid], pin)
                tip = (endpoint[0], endpoint[1] + (-par.STUB_LEN if symbol == 'vdd-port' else par.STUB_LEN))
                stubs.append((symbol, tip, endpoint))
                box = par.GEO_DOC[symbol]['orientations'][0]['viewBoxAABB']
                supply_markers.append((net, (tip[0]+box['x'], tip[1]+box['y'],
                    tip[0]+box['x']+box['width'], tip[1]+box['y']+box['height']),
                    tip, (cid, pin)))
                xs.extend((tip[0] + box['x'], tip[0] + box['x'] + box['width']))
                ys.extend((tip[1] + box['y'], tip[1] + box['y'] + box['height']))
    else:
        from routing_rules import RoutingOptions, build_scene, canonical_topology
        options = RoutingOptions(**(validation or {}).get('parameters', {}))
        scene = build_scene(par, nets, placement, comp_index, options=options)
        for connection in scene.power_connections:
            symbol, anchor = connection['symbol'], connection['anchor']
            local = par.GEO_DOC[symbol]['orientations'][0]['pins'][0]['at']
            origin = (anchor[0] - local['x'], anchor[1] - local['y'])
            box = par.GEO_DOC[symbol]['orientations'][0]['viewBoxAABB']
            label = (origin[0] + box['x'] + box['width'] + 6, origin[1] + 8)
            power_markers.append((connection, origin, label))
            power_runs.setdefault(connection['net'], []).extend(connection['segments'])
            supply_markers.append((connection['net'], (origin[0]+box['x'], origin[1]+box['y'],
                origin[0]+box['x']+box['width'], origin[1]+box['y']+box['height']),
                anchor, (connection['component_id'], connection['pin'])))
            if clean_presentation:
                xs.extend((origin[0] + box['x'], origin[0] + box['x'] + box['width']))
                ys.extend((origin[1] + box['y'], origin[1] + box['y'] + box['height']))
            else:
                xs.extend((origin[0] + box['x'], origin[0] + box['x'] + box['width'],
                           label[0] + len(connection['net']) * 7.5))
                ys.extend((origin[1] + box['y'], origin[1] + box['y'] + box['height'], label[1] + 4))
            for a, b in connection['segments']:
                xs.extend((a[0], b[0])); ys.extend((a[1], b[1]))
        power_runs = {net: canonical_topology(segments)['segments']
                      for net, segments in power_runs.items()}
    for _, segments in routed:
        for a, b in segments:
            xs.extend((a[0], b[0])); ys.extend((a[1], b[1]))
    bus_labels = []
    if bus_layout and net_label_view is None:
        label_x = min(xs, default=0) - 16
        for net, segments in routed:
            if len(nets.get(net, ())) < 2 or not segments:
                continue
            horizontals = [(a, b) for a, b in segments if a[1] == b[1]]
            if not horizontals:
                continue
            bus_y = min(a[1] for a, _ in horizontals)
            bus_labels.append((net, label_x, bus_y + 4))
            xs.append(label_x - len(net) * 7.5)
            ys.extend((bus_y - 10, bus_y + 8))
    boxes = []
    if bus_layout or clean_presentation:
        structs = ()
    for structure in structs:
        if 'confidence' in structure and (structure['confidence'] != 'high' or structure.get('parent')):
            continue
        points = [placement.pos_of(comp_index[cid]) for cid in structure['devices']]
        if not points:
            continue
        box = (min(p[0] for p in points) - par.CELL * .42,
               min(p[1] for p in points) - par.CELL * .42,
               max(p[0] for p in points) + par.CELL * .42,
               max(p[1] for p in points) + par.CELL * .42)
        boxes.append((structure.get('display_name', structure['kind']), box))
        xs.extend((box[0], box[2])); ys.extend((box[1], box[3]))
    style_label = 'clean' if clean else 'color'
    if bus_layout:
        style_label += ' [bus fallback]'
    elif stage == 'spacing_retry':
        style_label += ' [spacing retry]'
    title = (f"{style_label}: WL={metrics['wirelength']:.0f} "
             f"cross={metrics['crossings']} bends={metrics['bends']} "
             f"body={metrics['body_pen']} ovl={metrics['overlap']:.0f} "
             f"prox={metrics['proximity']:.0f}")
    component_text, supply_text, port_text = [], [], []
    if clean_presentation:
        if net_label_view is not None:
            hints = net_label_view['render_hints']
            component_text, supply_text = hints['component_text'], hints['supply_text']
            tagged = {label['net'] for label in net_label_view['labels']}
            port_text = [label for label in hints['port_text'] if label['net'] not in tagged]
        else:
            component_text, supply_text, port_text = _presentation_labels(
                par, placement, comps, nets, comp_index, routed, stubs, power_markers,
                power_runs, supply_markers)
        for label in component_text + supply_text + port_text:
            xleft, ytop, xright, ybottom = label['bounds']
            xs.extend((xleft, xright)); ys.extend((ytop, ybottom))
        if net_label_view is not None:
            for label in net_label_view['labels']:
                left, top, right, bottom = label['bounds']
                xs.extend((left, right)); ys.extend((top, bottom))
        x0, y0 = min(xs, default=0)-24, min(ys, default=0)-(48 if bus_layout else 28)
        width = max(132 if bus_layout else 80, max(xs, default=100)+24-x0)
        height = max(80, max(ys, default=100)+24-y0)
    else:
        x0, y0 = min(xs, default=0) - 50, min(ys, default=0) - 70
        width = max(max(xs, default=100) + 50 - x0, len(title) * 7.5 + 32)
        height = max(120, max(ys, default=100) + 40 - y0)
    fmt = par.fmt
    parts = [f'<rect x="{fmt(x0)}" y="{fmt(y0)}" width="{fmt(width)}" height="{fmt(height)}" fill="#fff"/>']
    colors = {net: ('#000000' if clean else par.NET_COLORS[i % len(par.NET_COLORS)])
              for i, (net, _) in enumerate(routed)}
    for symbol, tip, endpoint in stubs:
        parts.append(f'<line x1="{fmt(tip[0])}" y1="{fmt(tip[1])}" x2="{fmt(endpoint[0])}" y2="{fmt(endpoint[1])}" stroke="#111" stroke-width="{fmt(wire_width)}"/>')
        parts.append(f'<g transform="translate({fmt(tip[0])} {fmt(tip[1])})">'
                     + par.svg_symbol_parts(definitions[symbol], 0, 'none') + '</g>')
    for net, segments in sorted(power_runs.items()):
        for path in segs_to_paths(segments):
            points = ' '.join(f'{fmt(p[0])},{fmt(p[1])}' for p in path)
            parts.append(f'<polyline data-power-net="{escape(net, quote=True)}" points="{points}" fill="none" stroke="#111" stroke-width="{fmt(wire_width)}" stroke-linecap="round" stroke-linejoin="round"/>')
        degree = defaultdict(int)
        for a, b in segments:
            degree[a] += 1; degree[b] += 1
        for point, count in sorted(degree.items()):
            if count > 2:
                parts.append(f'<circle cx="{fmt(point[0])}" cy="{fmt(point[1])}" r="4" fill="#111"/>')
    for connection, origin, label in power_markers:
        parts.append(f'<g data-power-component="{escape(connection["component_id"], quote=True)}" data-power-pin="{escape(connection["pin"], quote=True)}" transform="translate({fmt(origin[0])} {fmt(origin[1])})">'
                     + par.svg_symbol_parts(definitions[connection['symbol']], 0, 'none') + '</g>')
        if not clean_presentation:
            parts.append(f'<text x="{fmt(label[0])}" y="{fmt(label[1])}" font-family="monospace" font-size="12" fill="#111">{escape(connection["net"])}</text>')
    for net, segments in routed:
        for path in segs_to_paths(segments):
            points = ' '.join(f'{fmt(p[0])},{fmt(p[1])}' for p in path)
            parts.append(f'<polyline data-net="{escape(net, quote=True)}" points="{points}" fill="none" stroke="{colors[net]}" stroke-width="{fmt(wire_width)}" stroke-linecap="round" stroke-linejoin="round"/>')
        degree = defaultdict(int)
        for a, b in segments:
            degree[a] += 1; degree[b] += 1
        for point, count in sorted(degree.items()):
            if count > 2:
                parts.append(f'<circle cx="{fmt(point[0])}" cy="{fmt(point[1])}" r="4" fill="{colors[net]}"/>')
    for net, label_x, label_y in bus_labels:
        parts.append(f'<text x="{fmt(label_x)}" y="{fmt(label_y)}" text-anchor="end" font-family="monospace" font-size="12" fill="{colors[net]}">{escape(net)}</text>')
    box_colors = ['#e53935', '#1e88e5', '#43a047', '#8e24aa', '#fb8c00']
    for i, (kind, (bx0, by0, bx1, by1)) in enumerate(boxes):
        color = '#555' if clean else box_colors[i % len(box_colors)]
        parts.append(f'<rect x="{fmt(bx0)}" y="{fmt(by0)}" width="{fmt(bx1-bx0)}" height="{fmt(by1-by0)}" fill="none" stroke="{color}" stroke-width="1.5" stroke-dasharray="6 4" opacity="0.7"/>')
        parts.append(f'<text x="{fmt(bx0+4)}" y="{fmt(by0+14)}" font-family="sans-serif" font-size="12" fill="{color}">{escape(kind)}</text>')
    for i, comp in enumerate(comps):
        symbol = comp['symbol']
        cx, cy = placement.pos_of(i)
        orientation = par.GEO_DOC[symbol]['orientations'][placement.orient[i]]
        parts.append(f'<g data-component="{escape(comp["id"], quote=True)}" transform="translate({fmt(cx)} {fmt(cy)})">')
        parts.append(par.svg_symbol_parts(definitions[symbol], orientation['rotation'], orientation['mirror']))
        for pin in (() if clean_presentation else orientation['pins']):
            parts.append(f'<circle cx="{fmt(pin["at"]["x"])}" cy="{fmt(pin["at"]["y"])}" r="3" fill="#fff" stroke="#111" stroke-width="1.2"/>')
        label_x, label_y = labels[i]
        if clean_presentation:
            parts.append(_clean_text(component_text[i], fmt, kind='component', local_origin=(cx, cy))+'</g>')
        else:
            parts.append(f'<text x="{fmt(label_x)}" y="{fmt(label_y)}" font-family="monospace" font-size="12" fill="#111">{escape(comp["id"])}</text></g>')
    if clean_presentation:
        for label in supply_text:
            parts.append(_clean_text(label, fmt, kind='supply', attributes=
                f' data-power-net-label="{escape(label["net"], quote=True)}"'))
        for label in port_text:
            parts.append(_clean_text(label, fmt, kind='port', attributes=
                f' data-presentation-port="{escape(label["net"], quote=True)}"'
                f' data-anchor-component="{escape(label["component"], quote=True)}"'
                f' data-anchor-pin="{escape(label["pin"], quote=True)}"'
                f' data-anchor-x="{fmt(label["point"][0])}" data-anchor-y="{fmt(label["point"][1])}"'))
        if net_label_view is not None:
            for label in net_label_view['labels']:
                text = dict(label, anchor=label['anchor_style'])
                parts.append(_clean_text(text, fmt, kind='net-label', attributes=
                    f' data-net-label-net="{escape(label["net"], quote=True)}"'
                    f' data-net-label-group="{escape(label["group_id"], quote=True)}"'
                    f' data-net-label-anchor-x="{fmt(label["anchor"][0])}"'
                    f' data-net-label-anchor-y="{fmt(label["anchor"][1])}"'
                    f' data-net-label-tip-x="{fmt(label["tip"][0])}"'
                    f' data-net-label-tip-y="{fmt(label["tip"][1])}"'))
        if bus_layout:
            parts.append(f'<text data-presentation-stage="bus_fallback" x="{fmt(x0+12)}" y="{fmt(y0+18)}" font-family="monospace" font-size="12" fill="#555">Bus fallback</text>')
    else:
        parts.append(f'<text x="{fmt(x0+16)}" y="{fmt(y0+22)}" font-family="monospace" font-size="12" fill="#111">{title}</text>')
    destination = Path(out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    expression = ' data-wire-expression="net-labels"' if net_label_view is not None else ''
    destination.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg"{expression} width="{fmt(width)}" height="{fmt(height)}" viewBox="{fmt(x0)} {fmt(y0)} {fmt(width)} {fmt(height)}">\n'
        + '\n'.join(parts) + '\n</svg>\n', encoding='utf-8')
    return destination
