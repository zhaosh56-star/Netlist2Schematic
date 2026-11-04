"""Both SVG styles render the same validated final route."""
from __future__ import annotations

from collections import defaultdict
from html import escape
from pathlib import Path


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
               *, clean=False, structs=(), validation=None):
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
        xs.extend((cx + box['x'], cx + box['x'] + box['width'],
                   cx + label_x + len(comp['id']) * 7.5))
        ys.extend((cy + box['y'], cy + box['y'] + box['height'], cy + label_y + 4))
    stubs, power_markers, power_runs = [], [], {}
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
    if bus_layout:
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
    if bus_layout:
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
        for pin in orientation['pins']:
            parts.append(f'<circle cx="{fmt(pin["at"]["x"])}" cy="{fmt(pin["at"]["y"])}" r="3" fill="#fff" stroke="#111" stroke-width="1.2"/>')
        label_x, label_y = labels[i]
        parts.append(f'<text x="{fmt(label_x)}" y="{fmt(label_y)}" font-family="monospace" font-size="12" fill="#111">{escape(comp["id"])}</text></g>')
    parts.append(f'<text x="{fmt(x0+16)}" y="{fmt(y0+22)}" font-family="monospace" font-size="12" fill="#111">{title}</text>')
    destination = Path(out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{fmt(width)}" height="{fmt(height)}" viewBox="{fmt(x0)} {fmt(y0)} {fmt(width)} {fmt(height)}">\n'
        + '\n'.join(parts) + '\n</svg>\n', encoding='utf-8')
    return destination
