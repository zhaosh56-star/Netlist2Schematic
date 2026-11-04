#!/usr/bin/env python3
"""Run the independent SA flow using read-only Analog Canvas symbols."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
from math import isfinite
import random
from pathlib import Path
import sys
import tempfile
import os
from time import perf_counter

from sa_common import ROOT, configure_resources, load_local, read_json, resource_root, write_json
from sa_render import render_svg
from sa_routing import final_route, RoutingValidationError
from sa_geometry import validate_geometry
from sa_saved import load_placement, saved_routing_options, is_bus_layout, BUS_ROUTER
from orthogonal_router import RoutingFailure

ROUTING_FAILURES = (RoutingFailure, RoutingValidationError)


def run(path, *, mode='layered', iterations=20000, seed=20261029,
        out_dir=None, cell=None, analog_canvas_root=None, routing_options=None,
        layout_style='structured', quality_seconds=300, candidate_id=None,
        fallback=None, spacing_scale=1.5, spacing_seconds=60, fallback_seconds=60):
    if iterations < 0:
        raise ValueError('iterations must be non-negative')
    if layout_style not in ('structured', 'legacy'):
        raise ValueError(f'Unknown layout style: {layout_style}')
    if mode not in ('baseline', 'layered'):
        raise ValueError(f'Unknown layout mode: {mode}')
    if candidate_id is not None and (mode != 'layered' or layout_style != 'structured'):
        raise ValueError('candidate_id is available only for structured layered layouts')
    if fallback not in (None, 'auto', 'off', 'only'):
        raise ValueError('fallback must be auto, off or only')
    policy = fallback if fallback is not None else ('off' if candidate_id is not None else 'auto')
    if policy == 'only' and candidate_id is not None:
        raise ValueError('fallback only cannot be combined with candidate_id')
    for name, value, minimum, inclusive in (
            ('quality_seconds', quality_seconds, 0, False),
            ('spacing_scale', spacing_scale, 1, False),
            ('spacing_seconds', spacing_seconds, 0, True),
            ('fallback_seconds', fallback_seconds, 0, False)):
        if type(value) not in (int, float) or not isfinite(value) or (
                value < minimum if inclusive else value <= minimum):
            raise ValueError(f'{name} must be finite and {">=" if inclusive else ">"} {minimum}')
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f'Netlist not found: {source}')
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    base = source.stem + ('-' + cell if cell else '')
    label = source.parent.name if source.stem.lower() == 'circuit' else source.stem
    if cell:
        label += '-' + cell
    destination = Path(out_dir).expanduser().resolve() if out_dir else ROOT / 'output' / f'{label}-{mode}'
    _, diagnostic_path = _output_targets(base, mode, destination, {source})
    configure_resources(analog_canvas_root)
    par = load_local('place_and_route', 'place-and-route.py')
    validate_geometry(par.GEO_DOC)
    options = routing_options
    if mode == 'baseline' or layout_style == 'legacy':
        from routing_rules import RoutingOptions
        options = options or RoutingOptions(crossing_cost=500.0, bend_cost=30.0)
    stages = []
    try:
        if policy == 'only':
            result = _bus_result(source, cell, seed, options, fallback_seconds, stages,
                                 trigger='forced')
        else:
            started = perf_counter()
            try:
                result = _normal_result(source, par, mode, layout_style, cell, iterations,
                                        seed, quality_seconds, candidate_id, options)
            except ROUTING_FAILURES as exc:
                _check_source(source, source_hash)
                stages.append(_failed_stage('normal', exc, perf_counter() - started))
                if policy == 'off':
                    raise
                context = getattr(exc, 'retry_context', None)
                retry_options = getattr(exc, 'retry_options', None) or options
                result = None
                if context is not None and spacing_seconds > 0:
                    from spacing_retry import retry_spacing
                    print(f'Normal routing failed; spacing retry x{spacing_scale:g}, '
                          f'budget={spacing_seconds:g}s', flush=True)
                    started = perf_counter()
                    try:
                        result = retry_spacing(context, routing_options=retry_options,
                                               scale=spacing_scale, seconds=spacing_seconds)
                    except ROUTING_FAILURES as retry_exc:
                        stages.append(_failed_stage('spacing_retry', retry_exc,
                                                    perf_counter() - started))
                    else:
                        stages.append(_valid_stage('spacing_retry', result,
                                                   perf_counter() - started))
                else:
                    stages.append({'stage': 'spacing_retry', 'status': 'skipped',
                                   'reason': 'disabled' if spacing_seconds == 0 else 'no_attempted_layout'})
                _check_source(source, source_hash)
                if result is None:
                    result = _bus_result(source, cell, seed, options, fallback_seconds, stages,
                                         trigger='normal_routing_failed')
            else:
                stages.append(_valid_stage('normal', result, perf_counter() - started))
        _check_source(source, source_hash)
    except ROUTING_FAILURES as exc:
        _record_failure(exc, diagnostic_path, source)
        raise
    placement, comps, nets, comp_index, metadata, routed_result = result
    metadata = deepcopy(metadata)
    metadata.setdefault('routing_stage', 'normal')
    metadata['recovery'] = {'policy': policy, 'stages': stages}
    if len(set(placement.cell_of)) != len(comps) or any(p is None for p in placement.cell_of):
        raise ValueError('Placement contains unassigned or duplicate cells')
    data = {
        'format_version': 1, 'source_netlist': str(source), 'source_cell': cell,
        'source_sha256': source_hash,
        'analog_canvas_root': str(resource_root()), 'mode': mode,
        'seed': seed, 'iterations': iterations, **metadata,
        'placement': {comp['id']: {'row': placement.cell_of[i][0],
                                  'col': placement.cell_of[i][1],
                                  'orient': placement.orient[i]}
                      for i, comp in enumerate(comps)},
    }
    return _finish(data, par, placement, comps, nets, comp_index, base, destination,
                   protected_paths={source}, options=options, routed_result=routed_result)


def _normal_result(source, par, mode, layout_style, cell, iterations, seed,
                   quality_seconds, candidate_id, options):
    if mode == 'layered' and layout_style == 'structured':
        from aesthetic_flow import select_layout
        return select_layout(source, cell=cell, iterations=iterations, seed=seed,
                             quality_seconds=quality_seconds, routing_options=options,
                             candidate_id=candidate_id)
    elif mode == 'layered':
        layered = load_local('place_layered', 'place-layered.py')
        placement, comps, nets, comp_index, metadata = layered.create_placement(
            source, cell=cell, iterations=iterations, seed=seed)
    else:
        pn = load_local('parse_netlist', 'parse-netlist.py')
        doc = pn.parse(source, cell=cell)
        comps, nets = doc['components'], doc['nets']
        if not comps:
            raise ValueError('No supported devices in the selected circuit')
        comp_index = {comp['id']: i for i, comp in enumerate(comps)}
        placement = par.Placement(comps, random.Random(seed))
        energy, signal_nets, _ = par.sa(placement, nets, comp_index, iterations=iterations)
        _, optimization_metrics = par.route(signal_nets, placement, comp_index)
        metadata = dict(source_note='baseline', levels={}, attach={}, structs=[],
                        sym_pairs=[], energy=energy, optimization_metrics=optimization_metrics)
    metadata['layout_style'] = 'legacy'
    try:
        routed_result = final_route(par, nets, placement, comp_index, options=options)
    except ROUTING_FAILURES as exc:
        exc.retry_context = deepcopy((placement, comps, nets, comp_index, metadata))
        exc.retry_options = options
        raise
    return placement, comps, nets, comp_index, metadata, routed_result


def _check_source(source, expected_hash):
    if hashlib.sha256(Path(source).read_bytes()).hexdigest() != expected_hash:
        raise ValueError('Source netlist changed while the flow was running; rerun with the current input')


def _failed_stage(stage, exc, seconds):
    return {'stage': stage, 'status': 'failed', 'message': str(exc),
            'elapsed_seconds': round(seconds, 6),
            'diagnostics': deepcopy(getattr(exc, 'diagnostics', {}))}


def _valid_stage(stage, result, seconds):
    return {'stage': stage, 'status': 'valid', 'elapsed_seconds': round(seconds, 6),
            'metrics': deepcopy(result[5][1])}


def _bus_result(source, cell, seed, options, seconds, stages, *, trigger):
    from fallback_bus import create_fallback
    print(f'Constructive bus fallback: budget={seconds:g}s; no SA or A*', flush=True)
    started = perf_counter()
    try:
        result = create_fallback(source, cell=cell, seed=seed, routing_options=options,
                                 deadline=started + seconds)
    except ROUTING_FAILURES as exc:
        stages.append(_failed_stage('bus_fallback', exc, perf_counter() - started))
        raise RoutingFailure('No verified route completed in the recovery stages',
                             {'reason': 'recovery_failed', 'stages': deepcopy(stages)}) from exc
    stages.append(_valid_stage('bus_fallback', result, perf_counter() - started))
    result[4].setdefault('fallback', {}).update(trigger=trigger, budget_seconds=seconds,
        elapsed_seconds=round(perf_counter() - started, 6),
        prior_stages=deepcopy(stages[:-1]))
    return result


def _output_targets(base, mode, destination, protected_paths=()):
    """Check every artifact path before routing or recording a failure."""
    destination = Path(destination).resolve()
    if mode not in ('baseline', 'layered'):
        raise ValueError(f'Invalid saved layout mode: {mode!r}')
    if not base or base in ('.', '..') or any(character in base for character in '/\\:\0'):
        raise ValueError('Output base must be a single filename component')
    names = (f'{base}-{mode}.svg', f'{base}-clean.svg', f'{base}-placement.json')
    protected = {Path(path).resolve() for path in protected_paths}
    diagnostic_path = destination / 'routing-diagnostics.json'
    if any((destination / name).resolve() in protected for name in (*names, diagnostic_path.name)):
        raise ValueError('Output artifacts must differ from source netlist and placement JSON')
    if diagnostic_path.exists():
        try:
            previous_diagnostic = read_json(diagnostic_path)
        except (ValueError, OSError):
            previous_diagnostic = None
        if not isinstance(previous_diagnostic, dict) or previous_diagnostic.get('diagnostic_format') != 'sa-routing-v1':
            raise ValueError(f'Refusing to overwrite an unrelated diagnostics file: {diagnostic_path}')
    return names, diagnostic_path


def _record_failure(exc, diagnostic_path, source):
    diagnostics = getattr(exc, 'diagnostics', None)
    if diagnostics is not None:
        write_json(diagnostic_path, {
            'diagnostic_format': 'sa-routing-v1',
            'valid': False, 'message': str(exc),
            'source_netlist': str(source), 'diagnostics': diagnostics})


def _finish(data, par, placement, comps, nets, comp_index, base, destination,
            *, protected_paths=(), options=None, routed_result=None):
    """Route and validate before staging any success artifacts."""
    destination = Path(destination).resolve()
    names, diagnostic_path = _output_targets(base, data.get('mode', 'layered'), destination, protected_paths)
    try:
        routed, metrics, validation = (routed_result if routed_result is not None else
            final_route(par, nets, placement, comp_index, options=options))
    except ValueError as exc:
        _record_failure(exc, diagnostic_path, data['source_netlist'])
        raise
    if data.get('source_sha256') and hashlib.sha256(Path(data['source_netlist']).read_bytes()).hexdigest() != data['source_sha256']:
        raise ValueError('Source netlist changed while the flow was running; rerun with the current input')
    data = dict(data, routes={net: segments for net, segments in routed},
                metrics=metrics, validation=validation)
    placement.routing_stage = data.get('routing_stage', 'normal')
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.sa-stage-', dir=destination) as directory:
        staging = Path(directory)
        for clean, name in ((False, names[0]), (True, names[1])):
            render_svg(par, placement, comps, nets, comp_index, routed, metrics,
                       staging / name, clean=clean, structs=data.get('structs', []),
                       validation=validation)
        write_json(staging / names[2], data)
        for name in names:
            os.replace(staging / name, destination / name)
    # A successful rerun supersedes only the diagnostic generated by this tool.
    if diagnostic_path.is_file():
        diagnostic_path.unlink()
    print(f'Final: {metrics}', flush=True)
    print(f"Router: {validation.get('router', 'orthogonal-a-star')}; "
          f"seconds={validation.get('routing_seconds', 0):.3f}", flush=True)
    for name in names:
        print(f'Wrote {destination / name}', flush=True)
    return data


def reroute_placement(jpath, out_dir=None, analog_canvas_root=None, routing_options=None):
    source_json = Path(jpath).expanduser().resolve()
    data, source, par, placement, comps, nets, comp_index = load_placement(
        source_json, analog_canvas_root=analog_canvas_root)
    base = source_json.stem.removesuffix('-placement')
    destination = (Path(out_dir).expanduser().resolve() if out_dir else
                   ROOT / 'output' / f'{base}-rerouted')
    metadata = {key: value for key, value in data.items()
                if key not in ('routes', 'metrics', 'validation')}
    metadata.update(source_netlist=str(source), analog_canvas_root=str(resource_root()),
                    rerouted_from=str(source_json))
    options = routing_options or saved_routing_options(data)
    routed_result = None
    if is_bus_layout(data):
        from fallback_bus import reroute_fallback
        routed_result = reroute_fallback(par, placement, comps, nets, comp_index, metadata,
                                          routing_options=options, deadline=perf_counter() + 60)
    return _finish(metadata, par, placement, comps, nets, comp_index, base, destination,
                   protected_paths={source, source_json},
                   options=options, routed_result=routed_result)


def main(argv=None, default_mode='layered'):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('netlist', nargs='?', help='SPICE input file (.cir/.sp/.spi)')
    parser.add_argument('--reroute-placement', help='Reuse saved coordinates; skip SA and reroute')
    parser.add_argument('--mode', choices=('baseline', 'layered'), default=default_mode)
    parser.add_argument('--layout-style', choices=('structured', 'legacy'), default='structured')
    parser.add_argument('--quality-seconds', type=float, default=300)
    parser.add_argument('--candidate-id', help='Replay one structured candidate identifier')
    parser.add_argument('--fallback', choices=('auto', 'off', 'only'),
                        help='Default: auto for netlists, off for candidate replay')
    parser.add_argument('--spacing-scale', type=float, default=1.5)
    parser.add_argument('--spacing-seconds', type=float, default=60)
    parser.add_argument('--fallback-seconds', type=float, default=60)
    parser.add_argument('--iterations', type=int, default=20000)
    parser.add_argument('--seed', type=int, default=20261029)
    parser.add_argument('--out-dir')
    parser.add_argument('--cell', help='Explicit .subckt name; no recursive expansion')
    parser.add_argument('--analog-canvas-root', help='Read-only original project directory')
    args = parser.parse_args(argv)
    try:
        if bool(args.netlist) == bool(args.reroute_placement):
            parser.error('provide either a netlist or --reroute-placement JSON')
        if args.reroute_placement:
            if args.fallback in ('auto', 'only'):
                parser.error('--reroute-placement preserves coordinates; it cannot request fallback relayout')
            reroute_placement(args.reroute_placement, out_dir=args.out_dir,
                              analog_canvas_root=args.analog_canvas_root)
        else:
            run(args.netlist, mode=args.mode, iterations=args.iterations, seed=args.seed,
                out_dir=args.out_dir, cell=args.cell, analog_canvas_root=args.analog_canvas_root,
                layout_style=args.layout_style, quality_seconds=args.quality_seconds, candidate_id=args.candidate_id,
                fallback=args.fallback, spacing_scale=args.spacing_scale,
                spacing_seconds=args.spacing_seconds, fallback_seconds=args.fallback_seconds)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f'Error: {exc}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
