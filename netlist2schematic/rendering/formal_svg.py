"""Textbook typography for a verified shared-VDD SVG; electrical shapes stay put.

This last-stage renderer changes text only; native electrical geometry is preserved.
The font stack and mathematical label proportions match the supplied GT style.
"""
from __future__ import annotations

from collections import Counter
from math import hypot
from pathlib import Path
import xml.etree.ElementTree as ET

from netlist2schematic.rendering.sa_render import _overlap, _wire_in_box

VERSION = 'formal-schematic-v2'
NS = 'http://www.w3.org/2000/svg'
FONT_FAMILY = "'ICM Round Period','Metropolis','DejaVu Sans',Arial,'Helvetica Neue',Helvetica,sans-serif"
FONT_SIZE = 15.116
SUBSCRIPT_SIZE = 11.48816
SUBSCRIPT_DY = 5.05479
SUBSCRIPT_DX = .528455


def math_runs(text, kind):
    """Keep the exact identifier, styling a variable's suffix as a subscript."""
    if len(text) < 2:
        return text, ''
    if kind == 'component':
        prefix = 2 if text[:1].casefold() == 'x' and text[1:2].isalpha() else 1
        return text[:prefix], text[prefix:]
    if text[:1] in 'VvIi':
        return text[:1], text[1:]
    return text, ''  # CLK, EN, A, etc. are port names, not component variables.


def _advance(text, size):
    narrow = "ilI1.,'|!"
    wide = 'MWmw@'
    return sum(size * (.36 if c in narrow else .92 if c in wide else
                       1.05 if ord(c) > 255 else .66) for c in text)


def text_bounds(x, y, text, align, kind):
    base, suffix = math_runs(text, kind)
    width = _advance(base, FONT_SIZE)
    if suffix:
        width += SUBSCRIPT_DX + _advance(suffix, SUBSCRIPT_SIZE)
    left = x-width if align == 'end' else x-width/2 if align == 'middle' else x
    # Include italic overhang, accents, and the lower subscript baseline.
    return left-2, y-FONT_SIZE*1.12, left+width+3, y+(8.5 if suffix else 4)


def geometry_signature(root):
    """Compare all nontext drawing geometry, including symbol transforms."""
    ignored = {'text', 'style', 'title', 'metadata', 'desc'}
    def signature(node, parent=None):
        tag = node.tag.rsplit('}', 1)[-1]
        if tag in ignored or parent is root and tag == 'rect':
            return None
        children = tuple(s for child in node if (s := signature(child, node)) is not None)
        if node is root:
            return 'drawing', children
        return tag, tuple(sorted(node.attrib.items())), node.text and node.text.strip() or '', children
    return signature(root)


def _drawing_environment(context, view):
    from netlist2schematic.plugins.power_rails.rail_core import _group_geometry
    par, placement = context['par'], context['placement']
    boxes, components, supplies, wires = [], {}, [], []
    for i, comp in enumerate(context['comps']):
        x, y = placement.pos_of(i)
        b = par.GEO_DOC[comp['symbol']]['orientations'][placement.orient[i]]['viewBoxAABB']
        box = x+b['x'], y+b['y'], x+b['x']+b['width'], y+b['y']+b['height']
        components[comp['id']] = box
        boxes.append(box)
    hidden = {tuple(p) for group in view['groups'] for p in group['ports']}
    ports = {(p['component_id'], p['pin']): p for p in context['terminals']}
    for identity, port in ports.items():
        if identity in hidden:
            continue
        tip = port['stub'][1]
        b = par.GEO_DOC[port['symbol']]['orientations'][0]['viewBoxAABB']
        box = tip[0]+b['x'], tip[1]+b['y'], tip[0]+b['x']+b['width'], tip[1]+b['y']+b['height']
        boxes.append(box)
        supplies.append((port['net'], box))
        wires.extend(port.get('segments', [port['stub']]))
    for group in view['groups']:
        shape = _group_geometry(group, ports, context['geometry'],
                                context['scene'].options, context['scale'])
        x, y = shape['origin']
        b = context['geometry']['box']
        box = x+b[0], y+b[1], x+b[2], y+b[3]
        boxes.append(box)
        supplies.append((group['net'], box))
        wires.extend(shape['segments'])
    for _, lines in context['routed']:
        wires.extend(lines)
    return boxes, components, supplies, wires


def _box_candidates(box, kind):
    x0, y0, x1, y1 = box
    cx, cy = (x0+x1)/2, (y0+y1)/2
    for extra in (0, 10, 20, 36, 56, 80, 112, 150):
        gap = 8+extra
        if kind in ('supply', 'shared-supply'):
            yield cx, y0-14-extra, 'middle'
        yield x1+gap, cy+3, 'start'
        yield x0-gap, cy+3, 'end'
        yield cx, y0-14-extra, 'middle'
        yield cx, y1+FONT_SIZE+gap, 'middle'
        yield x1+gap, y0-14-extra, 'start'
        yield x0-gap, y0-14-extra, 'end'


def _pin_candidates(point):
    x, y = point
    for gap in (12, 22, 34, 50, 72, 102, 144):
        yield x+gap, y-14, 'start'
        yield x-gap, y-14, 'end'
        yield x+gap, y+FONT_SIZE+12, 'start'
        yield x-gap, y+FONT_SIZE+12, 'end'
        yield x, y-gap-4, 'middle'
        yield x, y+FONT_SIZE+gap, 'middle'


def _extended_candidates(point):
    """Dense ports may need a diagonal pocket, beyond the original axial slots."""
    x, y = point
    directions = ((-1, -1), (1, -1), (-1, 1), (1, 1),
                  (-.5, -1), (.5, -1), (-1, -.5), (1, -.5),
                  (-1, .5), (1, .5), (-.5, 1), (.5, 1),
                  (0, -1), (0, 1), (-1, 0), (1, 0))
    for radius in (24, 40, 64, 96, 144, 208, 288, 384, 512):
        for dx, dy in directions:
            yield x+dx*radius, y+dy*radius, ('end' if dx < 0 else 'start' if dx > 0 else 'middle')


def _choose_position(record, boxes, wires, occupied):
    best = None
    def consider(candidates, offset=0):
        nonlocal best
        for order, (x, y, align) in enumerate(candidates, offset):
            bounds = text_bounds(x, y, record['text'], align, record['kind'])
            body_hits = sum(_overlap(bounds, box, 2) for box in boxes)
            text_hits = sum(_overlap(bounds, box, 2) for box in occupied)
            wire_hits = sum(_wire_in_box(a, b, bounds, margin=2) for a, b in wires)
            distance = hypot(x-record['anchor'][0], y-record['anchor'][1])
            key = body_hits+text_hits, wire_hits, distance, order
            if best is None or key < best[0]:
                best = key, x, y, align, bounds, body_hits, text_hits, wire_hits
    consider(record['candidates'])
    if best is None or any(best[-3:]):
        consider(_extended_candidates(record['anchor']), len(record['candidates']))
    return best


def _label_records(root, context, component_boxes, supplies):
    parents = {c: p for p in root.iter() for c in p}
    records = []
    for element in list(root.iter('{'+NS+'}text')):
        kind = element.get('data-presentation-kind')
        if not kind:
            continue
        text = ''.join(element.itertext())
        net = element.get('data-power-net-label')
        if kind == 'supply' and net == '0':
            parents[element].remove(element)  # The real ground glyph identifies node 0.
            continue
        origin, component = (0, 0), None
        current = parents.get(element)
        while current is not None:
            if current.get('data-component') is not None:
                component = current.get('data-component')
                i = context['index'][component]
                origin = context['placement'].pos_of(i)
                break
            if current.get('data-power-rail-net') is not None:
                net = current.get('data-power-rail-net')
            current = parents.get(current)
        if kind in ('supply', 'shared-supply'):
            text = getattr(context['placement'], 'presentation_supply_names', {}).get(net, text)
            if str(net).casefold() in ('vdd', 'vcc', 'vee', 'vee!') and text.casefold() == str(net).casefold():
                text = text.upper()  # SPICE is case-insensitive; use the conventional cap name.
        x = float(element.get('x', 0))+origin[0]
        y = float(element.get('y', 0))+origin[1]
        if component and kind=='component':
            box = component_boxes[component]
            candidates = list(_box_candidates(box, kind))
            anchor = (box[0]+box[2])/2, (box[1]+box[3])/2
        elif kind in ('supply', 'shared-supply'):
            choices = [box for name, box in supplies if name == net]
            if not choices:
                raise ValueError('Formal supply label lacks a real matching symbol')
            # Clean-view hints retain the actual terminal identity even when
            # the old text location was displaced. Never use a displaced
            # label to guess which of several same-net caps owns it.
            hints=[h for h in context.get('hints',{}).get('supply_text',())
                if h.get('net')==net and abs(h['x']-x)<.01 and abs(h['y']-y)<.01]
            terminal=None
            if kind=='supply' and len(hints)==1:
                identity=tuple(hints[0]['identity'])
                terminal=next((p for p in context['terminals']
                    if (p['component_id'],p['pin'])==identity),None)
            if terminal is not None:
                tip=terminal['stub'][1]
                b=context['par'].GEO_DOC[terminal['symbol']]['orientations'][0]['viewBoxAABB']
                box=(tip[0]+b['x'],tip[1]+b['y'],tip[0]+b['x']+b['width'],tip[1]+b['y']+b['height'])
            else:
                box = min(choices, key=lambda b: hypot(x-(b[0]+b[2])/2, y-(b[1]+b[3])/2))
            candidates = list(_box_candidates(box, kind))
            anchor = (box[0]+box[2])/2, box[1]
        else:
            anchor = float(element.get('data-anchor-x', x)), float(element.get('data-anchor-y', y))
            candidates = list(_pin_candidates(anchor))
        candidates.append((x, y, element.get('text-anchor', 'start')))
        records.append(dict(element=element, kind=kind, text=text, origin=origin,
                            candidates=candidates, anchor=anchor, component=component,
                            owner_box=box if kind in ('component','supply','shared-supply') else None))
    return sorted(records, key=lambda r: (
        0 if r['kind'] in ('supply', 'shared-supply') else 1 if r['component'] else 2,
        r['anchor'][1], r['anchor'][0], r['text']))


def formalize_svg(source_svg, destination, context, view, *, title='Circuit schematic'):
    """Restyle and repack labels around the unchanged verified electrical drawing."""
    source, destination = Path(source_svg).resolve(), Path(destination).resolve()
    if source == destination or destination.suffix.casefold() != '.svg':
        raise ValueError('Formal SVG needs a separate .svg destination')
    root = ET.parse(source).getroot()
    before = geometry_signature(root)
    for node in root.iter():
        if node.tag.endswith('}rect') and node.get('stroke-dasharray'):
            raise ValueError('Formal export requires the clean view without debug boxes')
        if node.tag.endswith('}circle') and node.get('fill', '').casefold() in ('white', '#fff', '#ffffff'):
            raise ValueError('Formal export requires the clean view without white pin circles')
    boxes, component_boxes, supplies, wires = _drawing_environment(context, view)
    occupied, labels, faults = [], [], Counter()
    records=_label_records(root, context, component_boxes, supplies)
    from netlist2schematic.rendering.nearby_labels import assign
    choices,label_assignment=assign(records,component_boxes,boxes,wires,text_bounds,
        _extended_candidates,font_size=FONT_SIZE)
    for record,best in zip(records,choices):
        _, x, y, align, bounds, body_hits, text_hits, wire_hits = best
        occupied.append(bounds)
        faults.update(body_overlaps=body_hits, text_overlaps=text_hits, wire_overlaps=wire_hits)
        element = record['element']
        for child in list(element):
            element.remove(child)
        element.text = None
        for attr in ('style', 'font-style', 'font-weight', 'transform'):
            element.attrib.pop(attr, None)
        element.set('x', context['par'].fmt(x-record['origin'][0]))
        element.set('y', context['par'].fmt(y-record['origin'][1]))
        element.set('text-anchor', align)
        element.set('font-family', FONT_FAMILY)
        element.set('font-size', str(FONT_SIZE))
        element.set('font-weight', '700')
        element.set('font-style', 'italic')
        element.set('fill', '#000')
        element.set('data-formal-text', record['text'])
        element.set('data-label-box', ' '.join(context['par'].fmt(v) for v in bounds))
        base, suffix = math_runs(record['text'], record['kind'])
        ET.SubElement(element, '{'+NS+'}tspan').text = base
        if suffix:
            sub = ET.SubElement(element, '{'+NS+'}tspan', {
                'data-text-run': 'subscript', 'font-size': str(SUBSCRIPT_SIZE)+'px',
                'dx': str(SUBSCRIPT_DX), 'dy': str(SUBSCRIPT_DY), 'font-style': 'normal'})
            sub.text = suffix
        labels.append(dict(kind=record['kind'], text=record['text'], x=x, y=y,
                           anchor=align, bounds=bounds, component=record['component'],
                           owner_box=record.get('owner_box'),owner_gap=best[0][2]))
    if faults['body_overlaps'] or faults['text_overlaps'] or faults['wire_overlaps']:
        raise ValueError('Formal text could not avoid drawing: '+str(dict(faults)))
    x, y, width, height = map(float, root.get('viewBox').split())
    bounds = [(x+24, y+24, x+width-24, y+height-24), *occupied]
    left, top = min(b[0] for b in bounds)-24, min(b[1] for b in bounds)-24
    right, bottom = max(b[2] for b in bounds)+24, max(b[3] for b in bounds)+24
    for attr, value in (('width', right-left), ('height', bottom-top)):
        root.set(attr, context['par'].fmt(value))
    root.set('viewBox', ' '.join(context['par'].fmt(v) for v in (left, top, right-left, bottom-top)))
    root.set('data-style-profile', VERSION)
    root.set('role', 'img')
    root.set('aria-labelledby', 'schematic-title')
    title_node = ET.Element('{'+NS+'}title', {'id': 'schematic-title'})
    title_node.text = title
    root.insert(0, title_node)
    background = root.find('{'+NS+'}rect')
    if background is not None:
        for attr, value in zip(('x', 'y', 'width', 'height'), (left, top, right-left, bottom-top)):
            background.set(attr, context['par'].fmt(value))
    if geometry_signature(root) != before:
        raise ValueError('Formal typography changed electrical drawing geometry')
    ET.register_namespace('', NS)
    destination.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(destination, encoding='utf-8', xml_declaration=False)
    if geometry_signature(ET.parse(destination).getroot()) != before:
        raise ValueError('Saved formal SVG geometry differs')
    return dict(version=VERSION, font_family=FONT_FAMILY, font_size=FONT_SIZE,
                subscript_size=SUBSCRIPT_SIZE, labels=labels, text_validation=dict(faults),
                label_assignment=label_assignment,
                electrical_geometry_unchanged=True, debug_boxes=0, white_pin_circles=0)
