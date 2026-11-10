"""Native SVG contract: preserve real graphics while scoring label readability.

This is a deterministic geometry/text-box audit, not an image or aesthetic LLM
judge. Renderer changes must preserve native data-component/data-net metadata.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math
from pathlib import Path
import re
from unicodedata import east_asian_width
import xml.etree.ElementTree as ET


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('non-finite SVG coordinate')
    return result


def canonical(value):
    tokens = re.findall(r'[-+]?(?:\d*\.)?\d+(?:[eE][-+]?\d+)?|[A-Za-z]+|[^\s,]', value)
    out = []
    for token in tokens:
        try:
            val = number(token)
            out.append('0' if val == 0 else format(val, '.12g'))
        except ValueError:
            out.append(token)
    return ' '.join(out)


def overlap(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def wire_hits(a, b, box):
    x0, y0, x1, y1 = box
    return ((x0 <= a[0] <= x1 and max(min(a[1], b[1]), y0) < min(max(a[1], b[1]), y1))
            if a[0] == b[0] else
            (y0 <= a[1] <= y1 and max(min(a[0], b[0]), x0) < min(max(a[0], b[0]), x1)))


def inspect_svg(path, bodies):
    path = Path(path)
    if path.stat().st_size > 8000000:
        raise ValueError('SVG exceeds native contract size limit')
    raw = path.read_text(encoding='utf-8-sig')
    if '<!DOCTYPE' in raw or '<!ENTITY' in raw:
        raise ValueError('SVG external entities are not supported')
    root = ET.fromstring(raw)
    tag = lambda e: e.tag.rsplit('}', 1)[-1]
    if tag(root) != 'svg':
        raise ValueError('not an SVG canvas')
    vb = [number(v) for v in root.attrib.get('viewBox', '').split()]
    if len(vb) != 4 or vb[2] <= 0 or vb[3] <= 0:
        raise ValueError('SVG requires a finite positive viewBox')
    for dimension in ('width', 'height'):
        if dimension in root.attrib and number(root.attrib[dimension].removesuffix('px')) <= 0:
            raise ValueError('SVG display dimensions must be positive')
    canvas = (vb[0], vb[1], vb[0] + vb[2], vb[1] + vb[3])
    primitives, components, nets, labels = Counter(), {}, defaultdict(list), []
    supported = {'svg', 'g', 'path', 'line', 'polyline', 'polygon', 'rect', 'circle', 'ellipse', 'text', 'tspan'}

    def walk(element, transforms=(), component=None, offset=(0., 0.), paint=()):
        kind, attrs = tag(element), element.attrib
        if kind not in supported:
            raise ValueError('unsupported SVG element: ' + kind)
        if any(k.lower().startswith('on') or k in ('filter', 'mask', 'clip-path') for k in attrs):
            raise ValueError('native SVG contract forbids hidden graphics or active styling')
        if 'style' in attrs:
            declarations = [p.split(':', 1) for p in attrs['style'].split(';') if p.strip()]
            if any(len(p) != 2 or p[0].strip() not in ('stroke-linecap', 'stroke-linejoin', 'stroke-miterlimit', 'fill-rule') for p in declarations):
                raise ValueError('unsupported native SVG style declaration')
        if attrs.get('display') == 'none' or attrs.get('visibility') == 'hidden' or any(
                number(attrs.get(key, 1)) < .7 for key in ('opacity', 'stroke-opacity', 'fill-opacity')):
            raise ValueError('invisible native SVG content')
        local_paint = tuple(sorted((k, v) for k, v in attrs.items() if k in
                                  ('stroke', 'fill', 'stroke-width', 'opacity', 'stroke-opacity', 'fill-opacity')))
        if kind in ('svg', 'g') and local_paint:
            paint = (*paint, local_paint)
        transform = attrs.get('transform')
        if transform:
            transforms = (*transforms, canonical(transform))
            match = re.fullmatch(r'translate\(\s*([-\d.eE+]+)[,\s]+([-\d.eE+]+)\s*\)', transform)
            if match:
                offset = (offset[0] + number(match[1]), offset[1] + number(match[2]))
            elif kind == 'text':
                raise ValueError('text may only use translation transforms')
        if 'data-component' in attrs:
            component = attrs['data-component']
            if component in components:
                raise ValueError('duplicate SVG component')
            components[component] = {'labels': [], 'transform': transforms}
        if kind == 'text':
            if len(element):
                raise ValueError('native electrical labels must use plain text')
            text = ''.join(element.itertext()).strip()
            if not text:
                return
            size = number(attrs.get('font-size', 12))
            if not 10 <= size <= 24 or attrs.get('fill', '#111').lower() in ('white', '#fff', '#ffffff', 'none', 'transparent'):
                raise ValueError('labels must remain visible and readable')
            if component:
                components[component]['labels'].append(text)
            # Metric titles are diagnostics, not electrical labels.
            if not text.startswith(('clean:', 'color:')):
                x, y = offset[0] + number(attrs.get('x', 0)), offset[1] + number(attrs.get('y', 0))
                width = sum(size if east_asian_width(c) in ('W', 'F') else size * .64 for c in text)
                anchor = attrs.get('text-anchor', 'start')
                left = x - width if anchor == 'end' else x - width / 2 if anchor == 'middle' else x
                labels.append((left, y - size, left + width, y + size * .25))
            return
        if 'data-net' in attrs:
            if kind != 'polyline' or transforms:
                raise ValueError('signal routes require untransformed native polylines')
            points = [number(x) for x in re.split(r'[,\s]+', attrs.get('points', '').strip()) if x]
            if len(points) < 4 or len(points) % 2:
                raise ValueError('invalid signal SVG polyline')
            if number(attrs.get('stroke-width', 0)) != 2.5 or attrs.get('stroke', '').lower() in ('', 'none', 'white', '#fff', '#ffffff', 'transparent'):
                raise ValueError('signal wires must preserve visible physical width')
            coordinates = list(zip(points[::2], points[1::2]))
            nets[attrs['data-net']].extend(zip(coordinates, coordinates[1:]))
        elif kind not in ('svg', 'g', 'tspan'):
            # Canvas backgrounds and optional structure outlines are presentation.
            background = kind == 'rect' and element is list(root)[0] and not component
            outline = kind == 'rect' and 'stroke-dasharray' in attrs
            pin_dot = component and kind == 'circle' and attrs.get('r') == '3' and attrs.get('fill') == '#fff'
            if not (background or outline or pin_dot):
                geometry = tuple(sorted((k, canonical(v)) for k, v in attrs.items()
                                        if k in ('d', 'x', 'y', 'x1', 'y1', 'x2', 'y2', 'width', 'height', 'cx', 'cy', 'r', 'rx', 'ry', 'points')))
                primitives[(component, kind, transforms, geometry, paint, local_paint)] += 1
        for child in element:
            walk(child, transforms, component, offset, paint)

    walk(root)
    for name, data in components.items():
        if not any(re.search(r'(?<![\w])' + re.escape(name) + r'(?![\w])', text) for text in data['labels']):
            raise ValueError('missing readable device label: ' + name)
    # Components and routes must fit the displayed canvas; labels get a soft
    # clipping penalty so an existing imperfect presentation remains measurable.
    points = [p for segments in nets.values() for segment in segments for p in segment]
    bounds = list(bodies.values())
    if points:
        bounds.append((min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)))
    for box in bounds:
        if box[0] < canvas[0] - .01 or box[1] < canvas[1] - .01 or box[2] > canvas[2] + .01 or box[3] > canvas[3] + .01:
            raise ValueError('SVG canvas clips actual electrical geometry')
    collisions = sum(overlap(a, b) for i, a in enumerate(labels) for b in labels[i + 1:])
    body_hits = sum(overlap(label, body) for label in labels for body in bodies.values())
    # Maximal unique collinear runs prevent SVG subdivision/duplication from
    # gaming the label/wire count without changing physical geometry.
    grouped = defaultdict(list)
    for net, values in nets.items():
        for a, b in values:
            if a[0] != b[0] and a[1] != b[1]:
                raise ValueError('non-orthogonal signal SVG')
            horizontal = a[1] == b[1]
            grouped[(net, horizontal, a[1] if horizontal else a[0])].append(
                sorted((a[0], b[0]) if horizontal else (a[1], b[1])))
    wires = []
    for (_, horizontal, axis), intervals in grouped.items():
        merged = []
        for lo, hi in sorted(intervals):
            if merged and lo <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        wires.extend((((lo, axis), (hi, axis)) if horizontal else ((axis, lo), (axis, hi))) for lo, hi in merged)
    wire_count = sum(wire_hits(a, b, label) for label in labels for a, b in wires)
    clipped = sum(box[0] < canvas[0] or box[1] < canvas[1] or box[2] > canvas[2] or box[3] > canvas[3] for box in labels)
    if bounds:
        area = max(1., (max(b[2] for b in bounds) - min(b[0] for b in bounds)) * (max(b[3] for b in bounds) - min(b[1] for b in bounds)))
    else:
        area = 1.
    canvas_ratio = vb[2] * vb[3] / area
    penalty = 2. * collisions + body_hits + .2 * wire_count + 2. * clipped + .05 * max(0., canvas_ratio - 2.)
    return {'primitives': primitives, 'components': set(components), 'nets': dict(nets),
            'readability': {'label_overlaps': collisions, 'label_body_hits': body_hits,
                            'label_wire_hits': wire_count, 'clipped_labels': clipped,
                            'canvas_ratio': canvas_ratio, 'penalty': penalty}}


def audit(candidate, canonical, bodies, normalize_segments):
    actual, expected = inspect_svg(candidate, bodies), inspect_svg(canonical, bodies)
    if actual['components'] != expected['components'] or actual['primitives'] != expected['primitives']:
        raise ValueError('candidate SVG changed/missing actual device symbols, power graphics or junctions')
    if set(actual['nets']) != set(expected['nets']):
        raise ValueError('candidate SVG signal net set differs from accepted routes')
    for name in actual['nets']:
        if normalize_segments(actual['nets'][name]) != normalize_segments(expected['nets'][name]):
            raise ValueError('candidate SVG differs from accepted signal routes: ' + name)
    return actual['readability']
