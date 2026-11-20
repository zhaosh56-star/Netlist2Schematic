"""Formal SVG plugin: GT typography, clean annotations and optional shared VDD.

Only complete, independently validated saved routes are accepted. No placement,
annealing, A*, recovery, or route simplification runs inside this plugin.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import sys
import tempfile
from time import perf_counter

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from netlist2schematic.common.sa_common import ROOT, read_json, write_json
from netlist2schematic.plugins.power_rails import power_rails as supply
from netlist2schematic.rendering.formal_svg import formalize_svg, VERSION as STYLE_VERSION

FORMAT = 'formal-schematic-output-v1'
VERSION = 'formal-output-v1'


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _hash(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def _options(power_rails, rail_seconds):
    if power_rails not in ('auto', 'off'):
        raise ValueError('formal power_rails must be auto or off')
    if type(rail_seconds) not in (int, float) or not isfinite(rail_seconds) or rail_seconds < 0:
        raise ValueError('formal rail_seconds must be finite and non-negative')
    return dict(power_rails=power_rails, rail_seconds=rail_seconds)


def _identity(power, policy, title):
    return dict(version=VERSION, style_version=STYLE_VERSION,
                original_sha256=power['original_file_sha256'], dependencies=power['dependencies'],
                policy=policy, title=title,
                power_geometry={k:power['view'][k] for k in ('version', 'scale', 'groups')})


def _snapshot(manifest):
    return {key:value for key,value in manifest.items()
            if key not in ('snapshot_sha256', 'artifacts', 'elapsed_seconds')}


def _check_power(power, context):
    supply._check_dependencies(power['dependencies'])
    if supply._dependencies(context['source'], context['par'], context['comps']) != power['dependencies']:
        raise ValueError('Formal output source/resource list is incomplete')
    if supply._digest(power['view']) != power['view_sha256']:
        raise ValueError('Formal output supply-view fingerprint mismatch')
    expected = supply._digest(dict(version=supply.VERSION, original=power['original'],
        dependencies=power['dependencies'], seconds=power['policy']['seconds']))
    if expected != power['fingerprint']:
        raise ValueError('Formal output supply snapshot fingerprint mismatch')
    checked = supply._validate(context, power['view'])
    if checked != power['view']['validation'] or context['metrics'] != power['verified_full_metrics']:
        raise ValueError('Formal output independent route/supply validation differs')


def _commit(power, context, stage, destination, policy, title, *, inputs, started, expected=None):
    _check_power(power, context)
    base = power['base']
    if not isinstance(base, str) or Path(base).name != base or base in ('', '.', '..'):
        raise ValueError('Invalid formal artifact basename')
    fingerprint = _digest(_identity(power, policy, title))
    target = destination/fingerprint[:16]
    names = dict(svg=base+'-formal.svg', json=base+'-formal.json')
    protected = {Path(p).resolve() for p in inputs} | {Path(context['source']).resolve()}
    if any((target/name).resolve() in protected for name in names.values()):
        raise ValueError('Formal output cannot overwrite its input')
    style = formalize_svg(power['artifacts']['clean_svg'], stage/names['svg'],
                          context, power['view'], title=title)
    svg_sha256 = _hash(stage/names['svg'])
    if expected is not None and (svg_sha256 != expected['svg_sha256'] or
                                  _digest(style) != _digest(expected['style'])):
        raise ValueError('Formal redraw differs from the saved verified drawing')
    # Retain the existing power plugin's complete replayable records. Relocate
    # artifact links before staging ends; no path into a temporary tree escapes.
    power = deepcopy(power)
    relocated = {}
    for key, value in power['artifacts'].items():
        relative = Path(value).resolve().relative_to(stage.resolve())
        relocated[key] = str(target/relative)
    power_json_in_stage = Path(power['artifacts']['json'])
    power['artifacts'] = relocated
    write_json(power_json_in_stage, power)
    manifest = dict(format=FORMAT, version=VERSION, fingerprint=fingerprint,
        style_version=STYLE_VERSION, policy=policy, title=title,
        original_placement=power['original_placement'],
        original_file_sha256=power['original_file_sha256'],
        resolved_source_netlist=str(context['source']), native_metrics=context['metrics'],
        routing_stage=context['data'].get('routing_stage', 'normal'),
        selected_candidate=context['data'].get('quality', {}).get('selected_candidate'),
        power_snapshot=power, style=style, svg_sha256=svg_sha256,
        validation=dict(valid=True, native=context['validation'],
                        power=power['view']['validation'], text=style['text_validation'],
                        electrical_geometry_unchanged=style['electrical_geometry_unchanged']),
        elapsed_seconds=round(perf_counter()-started, 6),
        note='Extra formal display only; native layout, signal routes, metrics and quality remain authoritative.')
    manifest['snapshot_sha256'] = _digest(_snapshot(manifest))
    manifest['artifacts'] = {key:str(target/name) for key,name in names.items()}
    manifest['artifacts']['power_json'] = relocated['json']
    write_json(stage/names['json'], manifest)
    supply._check_dependencies(power['dependencies'])
    for path, expected_hash in inputs.items():
        if _hash(path) != expected_hash:
            raise ValueError('Formal output input changed during rendering')
    target.mkdir(parents=True, exist_ok=True)
    for file in sorted(p for p in stage.rglob('*') if p.is_file()):
        relative = file.relative_to(stage)
        output = target/relative
        if not output.resolve().is_relative_to(target.resolve()):
            raise ValueError('Formal artifact escapes the output directory')
        output.parent.mkdir(parents=True, exist_ok=True)
        os.replace(file, output)
    return manifest


def generate(placement_json, out_dir=None, *, power_rails='auto', rail_seconds=5, title=None):
    """Generate a separate formal SVG from already successful native results."""
    policy = _options(power_rails, rail_seconds)
    source = Path(placement_json).expanduser().resolve()
    before = _hash(source)
    context = supply._load_context(source)
    destination = Path(out_dir).expanduser().resolve() if out_dir is not None else ROOT/'output/formal-output'/source.stem.removesuffix('-placement')
    destination.mkdir(parents=True, exist_ok=True)
    started = perf_counter()
    with tempfile.TemporaryDirectory(prefix='.formal-stage-', dir=destination) as directory:
        stage = Path(directory)
        assert stage.resolve().is_relative_to(destination.resolve())
        power = supply.generate(source, stage/'power-rails',
                                seconds=rail_seconds if power_rails == 'auto' else 0)
        label = title if title is not None else power['base']
        if not isinstance(label, str):
            raise ValueError('Formal title must be a string')
        return _commit(power, context, stage, destination, policy, label,
                       inputs={source:before}, started=started)


def redraw(formal_json, out_dir=None):
    """Revalidate and redraw the saved VDD plan; never reselect or reroute."""
    path = Path(formal_json).expanduser().resolve()
    before, started = _hash(path), perf_counter()
    manifest = read_json(path)
    if manifest.get('format') != FORMAT or manifest.get('version') != VERSION:
        raise ValueError('Unsupported formal-output snapshot')
    if _digest(_snapshot(manifest)) != manifest.get('snapshot_sha256'):
        raise ValueError('Formal-output snapshot fingerprint mismatch')
    power = manifest['power_snapshot']
    policy = _options(**manifest['policy'])
    if _digest(_identity(power, policy, manifest['title'])) != manifest.get('fingerprint'):
        raise ValueError('Formal-output identity differs')
    supply._check_dependencies(power['dependencies'])
    original = deepcopy(power['original'])
    source = Path(original.get('source_netlist') or power['resolved_source_netlist'])
    if not source.is_absolute():
        source = (Path(power['original_placement']).parent/source).resolve()
    original['source_netlist'] = str(source)
    destination = Path(out_dir).expanduser().resolve() if out_dir is not None else path.parent.parent/(power['base']+'-formal-redrawn')
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.formal-redraw-', dir=destination) as directory:
        stage = Path(directory)
        # Only the small restoration input is transient; it is not published.
        snapshot = stage/'restore.json'
        write_json(snapshot, original)
        context = supply._load_context(snapshot)
        snapshot.unlink()
        _check_power(power, context)
        rendered = supply._publish(power, context, stage/'power-rails')
        return _commit(rendered, context, stage, destination, policy, manifest['title'],
                       inputs={path:before}, started=started, expected=manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--placement', help='Native placement JSON with complete routes')
    group.add_argument('--redraw', help='Saved formal-output JSON; preserve its supply plan')
    parser.add_argument('--out-dir')
    parser.add_argument('--power-rails', choices=('auto','off'), default='auto')
    parser.add_argument('--rail-seconds', type=float, default=5)
    args = parser.parse_args(argv)
    try:
        result = redraw(args.redraw, args.out_dir) if args.redraw else generate(
            args.placement, args.out_dir, power_rails=args.power_rails, rail_seconds=args.rail_seconds)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, f'Error: {exc}\n')
    print('Formal SVG:', result['artifacts']['svg'])
    print('Formal record:', result['artifacts']['json'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
