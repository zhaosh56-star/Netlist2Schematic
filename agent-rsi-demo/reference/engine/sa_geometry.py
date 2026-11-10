"""Derive orientation geometry from the canonical default symbol variant."""
from __future__ import annotations

import argparse

from sa_common import ROOT, configure_resources, read_json, resource_root, write_json

DIRECTIONS = {'east': (1, 0), 'west': (-1, 0), 'north': (0, -1), 'south': (0, 1)}


def transform(point, rotation, mirror):
    x, y = point
    for _ in range((rotation % 360) // 90):
        x, y = -y, x
    if mirror in ('horizontal', 'both'):
        x = -x
    if mirror in ('vertical', 'both'):
        y = -y
    return x, y


def regenerate_geometry():
    """Keep copied orientation indices, recompute all facts from canonical pins."""
    document = read_json(ROOT / 'symbol-geometry.json')
    for symbol_id, geometry in document['symbols'].items():
        symbol = read_json(resource_root() / 'packages/components/definitions' / f'{symbol_id}.json')['symbol']
        variant = next((item for item in symbol.get('variants', [])
                        if item['id'] == symbol.get('defaultVariantId')), {})
        pins = [pin for pin in symbol['pins'] if pin['name'] not in variant.get('hiddenPinNames', [])]
        box = symbol['viewBox']
        for orientation in geometry['orientations']:
            rotation, mirror = orientation['rotation'], orientation['mirror']
            converted = []
            for pin in pins:
                x, y = transform((pin['at']['x'], pin['at']['y']), rotation, mirror)
                vector = transform(DIRECTIONS[pin['direction']], rotation, mirror)
                direction = next(name for name, value in DIRECTIONS.items() if value == vector)
                converted.append({'name': pin['name'], 'at': {'x': x, 'y': y}, 'direction': direction})
            orientation['pins'] = converted
            corners = [transform((x, y), rotation, mirror)
                       for x in (box['x'], box['x'] + box['width'])
                       for y in (box['y'], box['y'] + box['height'])]
            x0, x1 = min(p[0] for p in corners), max(p[0] for p in corners)
            y0, y1 = min(p[1] for p in corners), max(p[1] for p in corners)
            orientation['viewBoxAABB'] = {'x': x0, 'y': y0, 'width': x1-x0, 'height': y1-y0}
            orientation['pinSpread'] = {
                'minX': min(p['at']['x'] for p in converted),
                'maxX': max(p['at']['x'] for p in converted),
                'minY': min(p['at']['y'] for p in converted),
                'maxY': max(p['at']['y'] for p in converted)}
    return document


def validate_geometry(geometry):
    expected = regenerate_geometry()['symbols']
    for symbol_id, current in geometry.items():
        if current['orientations'] != expected[symbol_id]['orientations']:
            raise ValueError(f'Geometry cache does not match symbol {symbol_id!r}; '
                             'run sa_geometry.py with the same --analog-canvas-root first')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Rebuild orientation geometry from Analog Canvas symbols.')
    parser.add_argument('--analog-canvas-root')
    arguments = parser.parse_args()
    configure_resources(arguments.analog_canvas_root)
    write_json(ROOT / 'symbol-geometry.json', regenerate_geometry())
    print('Regenerated symbol-geometry.json from canonical definitions')
