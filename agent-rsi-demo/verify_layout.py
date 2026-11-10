"""Independent verification worker: only imports the frozen reference engine."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import shutil
from time import perf_counter
from input_bundle import InputBundleChanged


def verify(engine, layout, source, destination, cell=None, bundle=None, candidate_dir=None):
    engine, source, destination = Path(engine).resolve(), Path(source).resolve(), Path(destination).resolve()
    sys.path.insert(0, str(engine))
    from sa_saved import load_placement
    from sa_routing import validate_final_routes, normalize_segments
    from input_bundle import check_bundle
    from svg_audit import audit
    from routing_rules import RoutingOptions, build_scene
    from sa_render import render_svg
    from circuit_structures import analyze_structures
    from netlist_core import parse_netlist
    from layout_quality import evaluate_quality
    from sa_common import write_json

    destination.mkdir(parents=True, exist_ok=True)
    if bundle is not None:
        check_bundle(bundle)
    data = json.loads(Path(layout).read_text(encoding='utf-8-sig'))
    expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    if data.get('source_sha256') != expected_hash:
        raise ValueError('candidate source hash does not match the frozen input')
    if (data.get('source_cell') or '').lower() != (cell or '').lower():
        raise ValueError('candidate selected a different subcircuit')
    if not isinstance(data.get('routes'), dict):
        raise ValueError('candidate did not supply saved routes; verifier will not route for it')
    data['source_netlist'] = str(source)
    data['analog_canvas_root'] = str(engine / 'analog-canvas-main')
    # Ignore candidate-declared rules, scores, structure hints and success flags.
    data.pop('presentation', None)
    frozen = destination / 'verified-placement.json'
    write_json(frozen, data)
    data, _, par, placement, comps, nets, index = load_placement(
        frozen, analog_canvas_root=engine / 'analog-canvas-main')
    # Original route validation checks wires against bodies, but a pair of
    # unconnected devices could otherwise overlap with no wire to trigger it.
    bodies = []
    body_map = {}
    for i, comp in enumerate(comps):
        cx, cy = placement.pos_of(i)
        box = par.GEO_DOC[comp['symbol']]['orientations'][placement.orient[i]]['viewBoxAABB']
        rect = (cx + box['x'], cy + box['y'], cx + box['x'] + box['width'], cy + box['y'] + box['height'])
        for name, other in bodies:
            if min(rect[2], other[2]) - max(rect[0], other[0]) > 1e-8 and min(rect[3], other[3]) - max(rect[1], other[1]) > 1e-8:
                raise ValueError(f"overlapping device bodies: {name} / {comp['id']}")
        bodies.append((comp['id'], rect))
        body_map[comp['id']] = rect
    started = perf_counter()
    routed, metrics, validation = validate_final_routes(
        par, nets, placement, index, list(data['routes'].items()), options=RoutingOptions(),
        deadline=perf_counter() + 60)
    scene = build_scene(par, nets, placement, index, options=RoutingOptions())
    for i, (_, rect) in enumerate(scene.marker_rects):
        halo = scene.options.halo
        body_map['@power-' + str(i)] = (rect[0] + halo, rect[1] + halo, rect[2] - halo, rect[3] - halo)
    for net in scene.power_nets:
        for i, (a, b) in enumerate(scene.fixed_segments.get(net, [])):
            body_map['@supply-' + net + '-' + str(i)] = (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
    devices = parse_netlist(source, cell=cell)
    structures = analyze_structures(devices)
    if isinstance(structures, dict):
        structures = structures.get('structures', structures.get('structs', []))
    quality = evaluate_quality(par, placement, comps, nets, index, routed, metrics, structures)
    # Both displayed SVGs are rendered from these independently accepted routes.
    render_svg(par, placement, comps, nets, index, routed, metrics,
               destination / 'clean.svg', clean=True, validation=validation)
    render_svg(par, placement, comps, nets, index, routed, metrics,
               destination / 'layered.svg', clean=False, validation=validation)
    generated = Path(candidate_dir) if candidate_dir else Path(layout).parent
    clean_files = list(generated.glob('*-clean.svg'))
    other_files = [p for p in generated.glob('*.svg') if p not in clean_files]
    if len(clean_files) != 1 or len(other_files) != 1:
        raise ValueError('candidate must provide one clean and one coloured native SVG')
    readability = audit(clean_files[0], destination / 'clean.svg', body_map, normalize_segments)
    audit(other_files[0], destination / 'layered.svg', body_map, normalize_segments)
    shutil.copyfile(clean_files[0], destination / 'candidate-clean.svg')
    shutil.copyfile(other_files[0], destination / 'candidate-layered.svg')
    quality['geometry_score'] = quality['score']
    quality['presentation'] = readability
    quality['score'] += readability['penalty']
    data.update(routes=dict(routed), metrics=metrics, validation=validation, quality=quality,
                structs=structures)
    write_json(frozen, data)
    return {'valid': True, 'metrics': metrics, 'quality': quality,
            'score': 100.0 / (100.0 + quality['score']),
            'validation': validation, 'verification_seconds': perf_counter() - started,
            'source_sha256': expected_hash, 'devices': len(comps), 'symbols': sorted({c['symbol'] for c in comps}),
            'input_bundle_sha256': bundle['sha256'] if bundle else None,
            'score_policy': 'geometry-plus-native-svg-readability-v2',
            'placement': 'verified-placement.json', 'clean_svg': 'candidate-clean.svg',
            'layered_svg': 'candidate-layered.svg'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--engine', required=True, type=Path)
    parser.add_argument('--layout', required=True, type=Path)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--cell')
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--candidate-dir', type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    try:
        bundle = json.loads(args.bundle.read_text(encoding='utf-8')) if args.bundle else None
        report = verify(args.engine, args.layout, args.input, args.out, args.cell, bundle, args.candidate_dir)
        code = 0
    except (ImportError, OSError, TimeoutError, InputBundleChanged) as exc:
        report = {'valid': False, 'unavailable': True, 'error': f'{type(exc).__name__}: {exc}'}
        code = 2
    except Exception as exc:
        unavailable = getattr(exc, 'diagnostics', {}).get('reason') == 'quality_time_budget'
        report = {'valid': False, 'error': f'{type(exc).__name__}: {exc}'}
        if unavailable:
            report['unavailable'] = True
        else:
            report['score'] = 0.0
        code = 2 if unavailable else 1
    (args.out / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
