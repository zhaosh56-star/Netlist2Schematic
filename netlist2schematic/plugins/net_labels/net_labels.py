#!/usr/bin/env python3
"""Optional net-label views of already validated, fully wired schematics.

The original placement, full routes and quality remain untouched.  A separate
manifest embeds their snapshot and an independently checked display model.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import re
from statistics import median
import sys
import tempfile
from time import perf_counter

from netlist2schematic.common.sa_common import ROOT, read_json, write_json, resource_root, source_path

FORMAT = 'net-label-manifest-v1'
VERSION = 'net-label-plugin-v1'


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _file_hash(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def _sources(source):
    """Fingerprint includes, including empty model files not yielding tokens."""
    found = {}
    pattern = re.compile(r'''^\.(?:include|inc)\s+(?:"([^"]+)"|'([^']+)'|(\S+))\s*$''', re.I)

    def visit(path):
        path = path.resolve()
        if str(path) in found:
            return
        found[str(path)] = _file_hash(path)
        for raw in path.read_text(encoding='utf-8-sig').splitlines():
            text = raw.strip()
            if not text or text.startswith('*'):
                continue
            text = re.split(r'[$;]', text, maxsplit=1)[0].strip()
            match = pattern.fullmatch(text)
            if match:
                target = Path(next(v for v in match.groups() if v is not None))
                visit(target if target.is_absolute() else path.parent/target)
    visit(Path(source))
    return dict(sorted(found.items()))


def _resource_hashes(par, comps):
    symbols = {component['symbol'] for component in comps} | {'ground', 'vdd-port'}
    definitions = resource_root()/'packages'/'components'/'definitions'
    paths = [Path(par.GEO), *(definitions/(symbol+'.json') for symbol in sorted(symbols))]
    return {str(path.resolve()): _file_hash(path) for path in paths}


def _runtime_hashes():
    return {name: _file_hash(source_path(name, root=ROOT)) for name in (
        'net_labels.py', 'net_label_core.py', 'net_label_validation.py', 'sa_render.py')}


def _check_files(files, label):
    if not isinstance(files, dict) or not files:
        raise ValueError(f'Missing {label} fingerprints')
    for name, expected in files.items():
        if _file_hash(name) != expected:
            raise ValueError(f'{label} changed: {name}')


def _scale(par, placement):
    gaps = [b-a for attr in ('column_x', 'row_y')
            for a, b in zip(getattr(placement, attr, ()), getattr(placement, attr, ())[1:]) if b>a]
    return median(gaps) if gaps else par.CELL


def _terminals(scene, comps):
    return {net: [{'component': comps[pin.component]['id'], 'pin': pin.pin,
                   'point': list(pin.point)} for pin in pins]
            for net, pins in scene.pins.items()}


def _load_context(path):
    from netlist2schematic.common.sa_saved import load_placement, saved_routing_options
    from netlist2schematic.routing.sa_routing import validate_final_routes
    from netlist2schematic.routing.routing_rules import build_scene
    from netlist2schematic.rendering.presentation import configure_presentation
    from netlist2schematic.rendering.sa_render import net_label_render_hints
    data, source, par, placement, comps, nets, index = load_placement(path)
    if not isinstance(data.get('routes'), dict):
        raise ValueError('Net-label views require saved complete routes; no rerouting is performed')
    options = saved_routing_options(data)
    routed, metrics, validation = validate_final_routes(
        par, nets, placement, index, list(data['routes'].items()), options=options)
    scene = build_scene(par, nets, placement, index, options=options)
    configure_presentation(placement, source, data.get('source_cell'), comps, nets, 'clean')
    hints = net_label_render_hints(par, placement, comps, nets, index, routed, validation)
    return (data, source, par, placement, comps, nets, index, routed, metrics, validation,
            scene, hints, _terminals(scene, comps))


def _fingerprint(original, dependencies, runtime, seconds):
    return _digest({'version': VERSION, 'original': original, 'dependencies': dependencies,
                    'runtime': runtime, 'seconds': seconds})


def _safe_base(base):
    if not isinstance(base, str) or not base or Path(base).name != base or base in ('.', '..'):
        raise ValueError('Invalid net-label artifact base')
    return base


def _artifact_base(base):
    """Keep source titles in metadata, not in Windows' bounded artifact paths."""
    base = _safe_base(base)
    return base if len(base) <= 56 else base[:40]+'-'+sha256(base.encode('utf-8')).hexdigest()[:12]


def _publish(manifest, context, destination, *, protected=(), input_hashes=None):
    from netlist2schematic.rendering.sa_render import render_svg
    (_, _, par, placement, comps, nets, index, routed, _, validation,
     _, _, _) = context
    destination = Path(destination).expanduser().resolve()/manifest['fingerprint'][:16]
    base = _safe_base(manifest['base'])
    names = {'json': base+'-netlabels.json', 'clean_svg': base+'-netlabels-clean.svg',
             'color_svg': base+'-netlabels-color.svg'}
    protected = {Path(path).resolve() for path in protected}
    if any((destination/name).resolve() in protected for name in names.values()):
        raise ValueError('Net-label output must not overwrite its input')
    # The public API returns the same JSON types on first generation and replay.
    manifest = json.loads(json.dumps(manifest, ensure_ascii=False, allow_nan=False))
    manifest['artifacts'] = {kind: str(destination/name) for kind, name in names.items()}
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.net-label-stage-', dir=destination) as folder:
        staging = Path(folder)
        for clean, key in ((True, 'clean_svg'), (False, 'color_svg')):
            render_svg(par, placement, comps, nets, index, routed,
                manifest['verified_full_metrics'], staging/names[key], clean=clean,
                validation=validation, net_label_view=manifest['view'])
        write_json(staging/names['json'], manifest)
        _check_files(manifest['dependencies']['source_files'], 'Netlist/include')
        _check_files(manifest['dependencies']['resource_files'], 'Symbol resource')
        for path, expected in (input_hashes or {}).items():
            if _file_hash(path) != expected:
                raise ValueError(f'Source net-label input changed during rendering: {path}')
        for key, name in names.items():
            os.replace(staging/name, destination/name)
    return manifest


def generate(placement_json, out_dir=None, *, seconds=5):
    """Generate an optional display view; never invoke layout, A* or shortening."""
    from netlist2schematic.plugins.net_labels.net_label_core import build_view
    from netlist2schematic.plugins.net_labels.net_label_validation import validate_view
    if type(seconds) not in (int, float) or not isfinite(seconds) or seconds < 0:
        raise ValueError('Net-label seconds must be finite and non-negative')
    path = Path(placement_json).expanduser().resolve()
    before = _file_hash(path)
    started = perf_counter()
    context = _load_context(path)
    (data, source, par, placement, comps, _, _, routed, metrics, _,
     scene, hints, terminal_ids) = context
    dependencies = {'source_files': _sources(source), 'resource_files': _resource_hashes(par, comps)}
    runtime = _runtime_hashes()
    fingerprint = _fingerprint(data, dependencies, runtime, seconds)
    base = _artifact_base(path.stem.removesuffix('-placement'))
    destination = Path(out_dir) if out_dir else ROOT/'output'/'net-labels'/base
    cached = destination/fingerprint[:16]/(base+'-netlabels.json')
    if cached.is_file():
        # Cache reuse still goes through the full and independent display audits.
        result = redraw(cached, out_dir=destination, _cache=True)
        if _file_hash(path) != before:
            raise ValueError('Source placement changed during net-label verification')
        result['reused'] = True
        return result
    initialization_seconds = perf_counter()-started
    view = build_view(scene, routed, scale=_scale(par, placement), terminal_ids=terminal_ids,
                      text_obstacles=hints['text_obstacles'], seconds=seconds)
    if view.get('scale') != _scale(par, placement):
        raise ValueError('Net-label scale differs from the physical placement axes')
    view['render_hints'] = deepcopy(hints)
    checked = validate_view(scene, routed, view, terminal_ids,
                            text_obstacles=hints['text_obstacles'])
    view['validation'] = checked
    view['display_metrics'] = deepcopy(checked['metrics'])
    manifest = {'format': FORMAT, 'version': VERSION, 'fingerprint': fingerprint, 'base': base,
        'original': deepcopy(data), 'original_placement': str(path), 'original_file_sha256': before,
        'verified_full_metrics': metrics, 'dependencies': dependencies, 'runtime_hashes': runtime,
        'policy': {'mode': 'auto', 'seconds': seconds}, 'view': view,
        'initialization_seconds': round(initialization_seconds, 6),
        'elapsed_seconds': round(perf_counter()-started, 6),
        'note': 'Display wire length is not an A* routing improvement; full routes are preserved.'}
    manifest['view_sha256'] = _digest(view)
    if _file_hash(path) != before:
        raise ValueError('Source placement changed during net-label planning')
    return _publish(manifest, context, destination, protected=(path, source), input_hashes={path: before})


def redraw(manifest_json, out_dir=None, *, _cache=False):
    """Revalidate the saved display graph and render it without replanning."""
    from netlist2schematic.plugins.net_labels.net_label_validation import validate_view
    path = Path(manifest_json).expanduser().resolve()
    original_hash = _file_hash(path)
    manifest = read_json(path)
    if not isinstance(manifest, dict) or manifest.get('format') != FORMAT or manifest.get('version') != VERSION:
        raise ValueError('Unsupported net-label manifest')
    original = manifest.get('original')
    if not isinstance(original, dict) or not isinstance(original.get('routes'), dict):
        raise ValueError('Net-label manifest requires its complete original snapshot')
    dependencies = manifest.get('dependencies', {})
    _check_files(dependencies.get('source_files'), 'Netlist/include')
    _check_files(dependencies.get('resource_files'), 'Symbol resource')
    expected = _fingerprint(original, dependencies, manifest['runtime_hashes'], manifest['policy']['seconds'])
    if manifest.get('fingerprint') != expected or manifest.get('view_sha256') != _digest(manifest.get('view')):
        raise ValueError('Net-label snapshot fingerprints do not match')
    # Restoring an immutable copy avoids requiring the original placement file
    # to remain at its old path.  Existing source/resource checks still apply.
    with tempfile.TemporaryDirectory(prefix='net-label-restore-') as folder:
        restored = Path(folder)/'placement.json'
        restored_data = deepcopy(original)
        source = Path(restored_data.get('source_netlist', ''))
        if not source.is_absolute():
            origin = Path(manifest.get('original_placement', path))
            restored_data['source_netlist'] = str((origin.parent/source).resolve())
        write_json(restored, restored_data)
        context = _load_context(restored)
    (_, source, par, placement, comps, _, _, routed, metrics, _, scene, hints, terminal_ids) = context
    if (_sources(source) != dependencies['source_files'] or
            _resource_hashes(par, comps) != dependencies['resource_files']):
        raise ValueError('Saved net-label dependencies do not describe all source and symbol files')
    if metrics != manifest.get('verified_full_metrics'):
        raise ValueError('Saved full-route metrics differ from independent verification')
    view = manifest['view']
    if view.get('scale') != _scale(par, placement):
        raise ValueError('Saved net-label scale differs from the physical placement axes')
    if _digest(view.get('render_hints')) != _digest(hints):
        raise ValueError('Saved device and supply label positions differ from the original snapshot')
    checked = validate_view(scene, routed, view, terminal_ids, text_obstacles=hints['text_obstacles'])
    if checked['metrics'] != view.get('display_metrics'):
        raise ValueError('Saved display metrics differ from independent verification')
    if _file_hash(path) != original_hash:
        raise ValueError('Source net-label manifest changed during verification')
    destination = Path(out_dir) if out_dir else path.parent.parent/(manifest['base']+'-redrawn')
    return _publish(manifest, context, destination,
                    protected=(source,) if _cache else (path, source),
                    input_hashes={path: original_hash})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--placement', help='Saved complete placement and routes; no SA or A*')
    inputs.add_argument('--redraw', help='Revalidate a saved net-label view without replanning')
    parser.add_argument('--out-dir', help='Independent output root, grouped by input/strategy fingerprint')
    args = parser.parse_args(argv)
    try:
        result = (redraw(args.redraw, args.out_dir) if args.redraw else
                  generate(args.placement, args.out_dir))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, f'Net-label view failed: {exc}\n')
    print(f"Net-label view: {result['view']['status']}; {result['artifacts']['json']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
