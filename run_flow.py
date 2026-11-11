#!/usr/bin/env python3
"""Run the independent SA flow using read-only Analog Canvas symbols."""
from __future__ import annotations

import argparse
from copy import deepcopy, copy
from dataclasses import asdict
import hashlib
import json
from math import isfinite
import random
from pathlib import Path
import sys
import tempfile
import os
from time import perf_counter

from sa_common import ROOT, configure_resources, load_local, read_json, resource_root, write_json
from sa_render import render_svg
from sa_routing import final_route, validate_final_routes, RoutingValidationError
from sa_geometry import validate_geometry
from sa_saved import load_placement, saved_routing_options, is_bus_layout, BUS_ROUTER
from orthogonal_router import RoutingFailure

ROUTING_FAILURES = (RoutingFailure, RoutingValidationError)


def run(path, *, mode='layered', iterations=20000, seed=20261029,
        out_dir=None, cell=None, analog_canvas_root=None, routing_options=None,
        layout_style='structured', quality_seconds=300, candidate_id=None,
        fallback=None, spacing_scale=1.5, spacing_seconds=60, fallback_seconds=60,
        local_rescue=None, rescue_seconds=60, simplify_routes='safe', simplify_seconds=5,
        passive_layout='flexible', symmetry_layout='off', structure_layout='off', structure_phase=3,
        candidate_budget='sequential', port_preflight='off', presentation='legacy', net_labels='off'):
    if iterations < 0:
        raise ValueError('iterations must be non-negative')
    if layout_style not in ('structured', 'legacy'):
        raise ValueError(f'Unknown layout style: {layout_style}')
    if passive_layout not in ('flexible', 'fixed'):
        raise ValueError('passive_layout must be flexible or fixed')
    if symmetry_layout not in ('auto', 'off'):
        raise ValueError('symmetry_layout must be auto or off')
    if structure_layout not in ('auto', 'off'):
        raise ValueError('structure_layout must be auto or off')
    if type(structure_phase) is not int or structure_phase not in (1, 2, 3):
        raise ValueError('structure_phase must be 1, 2 or 3')
    from candidate_budget import validate_policy
    validate_policy(candidate_budget)
    if port_preflight not in ('off','repair'):
        raise ValueError('port_preflight must be off or repair')
    if presentation not in ('legacy','clean'):
        raise ValueError('presentation must be legacy or clean')
    if net_labels not in ('off', 'auto'):
        raise ValueError('net_labels must be off or auto')
    if mode not in ('baseline', 'layered'):
        raise ValueError(f'Unknown layout mode: {mode}')
    if candidate_id is not None and (mode != 'layered' or layout_style != 'structured'):
        raise ValueError('candidate_id is available only for structured layered layouts')
    if fallback not in (None, 'auto', 'off', 'only'):
        raise ValueError('fallback must be auto, off or only')
    policy = fallback if fallback is not None else ('off' if candidate_id is not None else 'auto')
    rescue_policy = _rescue_policy(local_rescue, policy == 'auto')
    _validate_rescue_seconds(rescue_seconds)
    _validate_simplification(simplify_routes, simplify_seconds)
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
    destination = Path(out_dir).expanduser().resolve() if out_dir else ROOT / 'output' / label
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
                                        seed, quality_seconds, candidate_id, options,
                                        passive_layout=passive_layout, symmetry_layout=symmetry_layout,
                                        structure_layout=structure_layout, structure_phase=structure_phase,
                                        candidate_budget=candidate_budget,port_preflight=port_preflight)
            except ROUTING_FAILURES as exc:
                if getattr(exc, 'diagnostics', {}).get('reason') == 'power_connections':
                    raise
                _check_source(source, source_hash)
                stages.append(_failed_stage('normal', exc, perf_counter() - started))
                if policy == 'off' and rescue_policy == 'off':
                    raise
                context = deepcopy(getattr(exc, 'retry_context', None))
                checkpoint = deepcopy(getattr(exc, '_rescue_context', None))
                retry_options = getattr(exc, 'retry_options', None) or options
                result = _try_local_rescue(context, checkpoint, retry_options,
                                           rescue_policy, rescue_seconds, stages)
                _check_source(source, source_hash)
                if result is None and policy == 'off':
                    if stages[-1]['status'] == 'failed':
                        raise RoutingFailure('No verified route completed in the recovery stages',
                            {'reason': 'recovery_failed', 'stages': deepcopy(stages)}) from exc
                    raise
                if result is None and context is not None and spacing_seconds > 0:
                    from spacing_retry import retry_spacing
                    print(f'Normal routing failed; spacing retry x{spacing_scale:g}, '
                          f'budget={spacing_seconds:g}s', flush=True)
                    started = perf_counter()
                    try:
                        result = retry_spacing(deepcopy(context), routing_options=retry_options,
                                               scale=spacing_scale, seconds=spacing_seconds)
                    except ROUTING_FAILURES as retry_exc:
                        stages.append(_failed_stage('spacing_retry', retry_exc,
                                                    perf_counter() - started))
                    else:
                        stages.append(_valid_stage('spacing_retry', result,
                                                   perf_counter() - started))
                elif result is None:
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
    metadata.setdefault('passive_layout_policy', 'fixed')
    metadata.setdefault('passive_layout', {
        'version': 'fixed-rows-v1', 'policy': 'fixed',
        'reason': 'deterministic_bus' if metadata['routing_stage'] == 'bus_fallback'
                  else 'saved_candidate'})
    metadata['recovery'] = {'policy': policy, 'local_rescue': rescue_policy,
                            'rescue_seconds': rescue_seconds, 'stages': stages}
    if len(set(placement.cell_of)) != len(comps) or any(p is None for p in placement.cell_of):
        raise ValueError('Placement contains unassigned or duplicate cells')
    data = {
        'format_version': 1, 'source_netlist': str(source), 'source_cell': cell,
        'source_sha256': source_hash,
        'analog_canvas_root': str(resource_root()), 'mode': mode,
        'seed': seed, 'iterations': iterations, **metadata,
        'requested_passive_layout': passive_layout,
        'requested_symmetry_layout': symmetry_layout,
        'requested_structure_layout': structure_layout,
        'requested_structure_phase': structure_phase,
        'requested_candidate_budget': candidate_budget,
        'requested_port_preflight': port_preflight,
        **({'presentation':{'style':'clean'}} if presentation=='clean' else {}),
        'placement': _saved_positions(placement, comps),
    }
    return _finish(data, par, placement, comps, nets, comp_index, base, destination,
                   protected_paths={source}, options=options, routed_result=routed_result,
                   simplify_routes=simplify_routes, simplify_seconds=simplify_seconds,
                   net_labels=net_labels)


def _normal_result(source, par, mode, layout_style, cell, iterations, seed,
                   quality_seconds, candidate_id, options, *, passive_layout='flexible',
                   symmetry_layout='off', structure_layout='off', structure_phase=3,
                   candidate_budget='sequential',port_preflight='off'):
    if mode == 'layered' and layout_style == 'structured':
        from aesthetic_flow import select_layout
        return select_layout(source, cell=cell, iterations=iterations, seed=seed,
                             quality_seconds=quality_seconds, routing_options=options,
                             candidate_id=candidate_id, passive_layout=passive_layout,
                             symmetry_layout=symmetry_layout, structure_layout=structure_layout,
                             structure_phase=structure_phase,candidate_budget=candidate_budget,
                             port_preflight=port_preflight)
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
    metadata['passive_layout_policy'] = 'fixed'
    metadata['passive_layout'] = {'version': 'fixed-rows-v1', 'policy': 'fixed',
                                  'reason': 'legacy_layout'}
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


def _saved_positions(placement, comps):
    rows={comp['id']:{'row':placement.cell_of[i][0],'col':placement.cell_of[i][1],
                      'orient':placement.orient[i]} for i,comp in enumerate(comps)}
    if hasattr(placement,'offset_x'):
        if len(placement.offset_x)!=len(comps) or any(type(x) not in (int,float) or not isfinite(x)
                for x in placement.offset_x):
            raise ValueError('Invalid placement horizontal offsets')
        for i,comp in enumerate(comps):rows[comp['id']]['dx']=placement.offset_x[i]
    return rows


def _rescue_policy(value, automatic):
    if value not in (None, 'auto', 'off'):
        raise ValueError('local_rescue must be auto or off')
    return value if value is not None else ('auto' if automatic else 'off')


def _validate_rescue_seconds(seconds):
    if type(seconds) not in (int, float) or not isfinite(seconds) or seconds < 0:
        raise ValueError('rescue_seconds must be finite and >= 0')


def _validate_simplification(policy, seconds):
    if policy not in ('safe', 'off'):
        raise ValueError('simplify_routes must be safe or off')
    from simplify_budget import validate_simplify_seconds
    validate_simplify_seconds(seconds)


def _routes_sha256(routed):
    """Fingerprint drawn geometry independently of segment cuts and ordering."""
    from routing_rules import merge_runs
    canonical = [(net, [[[float(value) for value in point] for point in segment]
                        for segment in merge_runs(segments)])
                 for net, segments in sorted(routed)]
    return hashlib.sha256(json.dumps(canonical, separators=(',', ':'),
                                    ensure_ascii=True).encode('utf-8')).hexdigest()


def _simplification_context_sha256(data, par):
    geometry = hashlib.sha256(json.dumps(par.GEO_DOC, sort_keys=True,
        separators=(',', ':'), ensure_ascii=True).encode('utf-8')).hexdigest()
    context = {key: data.get(key) for key in
               ('source_sha256', 'source_cell', 'placement', 'layout_grid', 'power_connections')}
    context['symbol_geometry_sha256'] = geometry
    return hashlib.sha256(json.dumps(context, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=True).encode('utf-8')).hexdigest()


def _postprocess_routes(data, par, placement, comps, nets, comp_index, routed_result,
                        options, policy, seconds, *, allow_verified_replay=False):
    """Postprocess one selected route; candidate selection is already finished."""
    routed, metrics, validation = routed_result
    data = deepcopy(data)
    automatic = seconds == 'auto'
    reason = ('bus_fallback' if is_bus_layout({**data, 'validation': validation}) else
              'disabled' if policy == 'off' else 'zero_budget' if seconds == 0 else None)
    if reason is not None:
        data['route_simplification'] = {
            'mode': policy, 'status': 'skipped', 'budget_seconds': 0 if automatic else seconds,
            'budget_selection': {'mode': 'auto' if automatic else 'fixed',
                                 'requested_seconds': seconds, 'status': 'skipped',
                                 'selected_seconds': 0 if automatic else seconds},
            'before_metrics': deepcopy(metrics), 'after_metrics': deepcopy(metrics),
            'changed_nets': [], 'changed_net_count': 0, 'candidate_counts': {},
            'elapsed_seconds': 0.0, 'stop_reason': reason}
        return data, routed_result
    from simplify_routes import simplify_routes as simplify, VERSION
    effective_options = options or saved_routing_options({'validation': validation})
    routing_parameters = json.loads(json.dumps(asdict(effective_options)))
    # A saved result can have stopped at its finite budget. Reprocessing that
    # exact verified geometry with unchanged inputs must not keep optimizing it
    # indefinitely. A changed route, layout, resource, rule or budget invalidates
    # this replay shortcut; the caller has already revalidated saved geometry.
    previous = data.get('route_simplification')
    context_sha = _simplification_context_sha256(data, par)
    from simplify_budget import AUTO_CONFIG, ESTIMATOR_VERSION, estimate_simplify_budget
    previous_selection = previous.get('budget_selection', {}) if isinstance(previous, dict) else {}
    if not isinstance(previous_selection, dict):
        previous_selection = {}
    if automatic:
        selection_matches = (previous_selection.get('mode') == 'auto'
            and previous_selection.get('estimator_version') == ESTIMATOR_VERSION
            and previous_selection.get('config') == AUTO_CONFIG
            and previous_selection.get('selected_seconds') in AUTO_CONFIG['levels']
            and previous.get('budget_seconds') == previous_selection.get('selected_seconds'))
    else:
        selection_matches = (previous_selection.get('mode', 'fixed') == 'fixed'
            and isinstance(previous, dict) and previous.get('budget_seconds') == seconds)
    if (allow_verified_replay and isinstance(previous, dict)
            and previous.get('version') == VERSION
            and selection_matches
            and previous.get('routing_parameters') == routing_parameters
            and previous.get('context_sha256') == context_sha
            and previous.get('output_routes_sha256') == _routes_sha256(routed)):
        data['route_simplification'] = deepcopy(previous)
        data['route_simplification']['verified_saved_replay'] = True
        return data, routed_result
    selection = (estimate_simplify_budget(placement, comps, nets, comp_index, routed)
                 if automatic else {'mode': 'fixed', 'selected_seconds': seconds})
    selection = deepcopy(selection)
    selection.update(requested_seconds=seconds, input_routes_sha256=_routes_sha256(routed))
    allocated_seconds = selection['selected_seconds']
    simplified, final_metrics, checked, report = simplify(
        par, nets, placement, comp_index, routed_result,
        options=effective_options, seconds=allocated_seconds)
    final_validation = deepcopy(validation)
    final_validation.update(checked)
    # Search diagnostics describe the original selected router invocation, not
    # shortcut enumeration. Keep that provenance even when validation replaces
    # some fields with freshly checked geometry.
    if 'search' in validation:
        final_validation['search'] = deepcopy(validation['search'])
    if 'router' in validation:
        final_validation['router'] = validation['router']
    from layout_quality import evaluate_quality
    final_quality = evaluate_quality(par, placement, comps, nets, comp_index,
                                     simplified, final_metrics, data.get('structs', []))
    report = deepcopy(report)
    report.update(mode=policy, routing_parameters=routing_parameters,
                  budget_selection=selection,
                  context_sha256=context_sha, output_routes_sha256=_routes_sha256(simplified),
                  quality_before=deepcopy(data.get('quality')), quality_after=deepcopy(final_quality))
    quality = deepcopy(data.get('quality')) if isinstance(data.get('quality'), dict) else {}
    quality.update(score=final_quality['score'], details=final_quality,
                   candidate_metrics_stage='before_route_simplification')
    data['quality'] = quality
    data['route_simplification'] = report
    return data, (simplified, final_metrics, final_validation)


def _try_local_rescue(context, checkpoint, options, policy, seconds, stages):
    """Only the top-level failed-normal path may invoke this recovery stage."""
    reason = ('disabled' if policy == 'off' or seconds == 0 else
              'no_attempted_layout' if context is None else
              'no_failure_checkpoint' if checkpoint is None else None)
    if reason is not None:
        stages.append({'stage': 'local_rescue', 'status': 'skipped', 'reason': reason})
        return None
    from local_rescue import rescue_routing
    print(f'Normal routing failed; local rescue, budget={seconds:g}s', flush=True)
    started = perf_counter()
    try:
        result = rescue_routing(deepcopy(context), deepcopy(checkpoint),
                                routing_options=deepcopy(options), seconds=seconds)
        if not result[5][2].get('valid', False):
            raise RoutingFailure('Local rescue did not pass final validation',
                                 {'reason': 'local_rescue_validation'})
    except ROUTING_FAILURES as exc:
        if getattr(exc, 'diagnostics', {}).get('reason') == 'power_connections':
            raise
        stages.append(_failed_stage('local_rescue', exc, perf_counter() - started))
        return None
    stages.append(_valid_stage('local_rescue', result, perf_counter() - started))
    result[4]['routing_stage'] = 'local_rescue'
    result[4].setdefault('local_rescue', {})['prior_stages'] = deepcopy(stages[:-1])
    return result


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
            *, protected_paths=(), options=None, routed_result=None,
            simplify_routes='safe', simplify_seconds=5, allow_verified_replay=False,
            input_hashes=None, net_labels='off'):
    """Route and validate before staging any success artifacts."""
    destination = Path(destination).resolve()
    _validate_simplification(simplify_routes, simplify_seconds)
    if net_labels not in ('off', 'auto'):
        raise ValueError('net_labels must be off or auto')
    names, diagnostic_path = _output_targets(base, data.get('mode', 'layered'), destination, protected_paths)
    try:
        routed, metrics, validation = (routed_result if routed_result is not None else
            final_route(par, nets, placement, comp_index, options=options))
    except ValueError as exc:
        _record_failure(exc, diagnostic_path, data['source_netlist'])
        raise
    source = Path(data['source_netlist'])
    expected_hash = data.get('source_sha256') or hashlib.sha256(source.read_bytes()).hexdigest()
    _check_source(source, expected_hash)
    data = deepcopy(data)
    data['source_sha256'] = expected_hash
    data, (routed, metrics, validation) = _postprocess_routes(
        data, par, placement, comps, nets, comp_index, (routed, metrics, validation), options,
        simplify_routes, simplify_seconds, allow_verified_replay=allow_verified_replay)
    if isinstance(data.get('layout_symmetry'), dict):
        # Recovery may enlarge physical axes. Saved rerouting/redraw never
        # regularizes devices; this only measures the positions being exported.
        from symmetry_layout import report_symmetry
        data['layout_symmetry'] = report_symmetry(
            placement, comps, comp_index, data,
            policy=data['layout_symmetry'].get('policy', 'auto'),
            grid_step=validation.get('parameters', {}).get('grid_step', 2.0))
        data['layout_symmetry']['measurement_stage'] = 'final_output'
    if data.get('requested_structure_layout') == 'auto' or isinstance(data.get('structure_layout'), dict):
        from layout_relations import build_layout_relations, measure_layout_relations
        from netlist_core import parse_netlist
        relations = data.get('layout_relations')
        if relations is None:
            devices = parse_netlist(source, cell=data.get('source_cell'))
            evidence = data.get('layout_evidence')
            if evidence is None:
                from layout_intent import build_intent
                evidence, relations = build_intent(source, data.get('source_cell'), devices,
                    data.get('structs', ()), comps, nets, comp_index,
                    data.get('requested_structure_phase', 3))
                evidence['consumption'] = {'policy': 'report_only',
                    'reason': 'saved_or_recovery_coordinates', 'stages': []}
                data['layout_evidence'] = evidence
            else:
                relations = build_layout_relations(devices, validated_evidence=evidence)
        data['layout_relations'] = relations
        report = data.setdefault('structure_layout', {
            'policy': 'auto', 'version': 'structure-layout-v1',
            'phase': data.get('requested_structure_phase',1), 'applied': False})
        phase=data.get('requested_structure_phase',report.get('phase',1))
        from structure_selection import prepare_branch_context, measure_structure
        if phase>=3:
            data=prepare_branch_context(source,data.get('source_cell'),placement,comps,nets,comp_index,data)
            report=data['structure_layout']
        report['metrics'] = measure_structure(par,placement,comps,nets,comp_index,data,phase)
        report['measurement_stage'] = 'final_output'
        if isinstance(data.get('branch_alignment'),dict) and phase>=3:
            data['branch_alignment']['current']=report['metrics']['branch_geometry']
            data['branch_alignment']['measurement_stage']='final_output'
        if isinstance(data.get('parallel_attachment_refinement'),dict) and phase>=3:
            data['parallel_attachment_refinement']['current']=report['metrics']['parallel_groups']
            data['parallel_attachment_refinement']['measurement_stage']='final_output'
    if isinstance(data.get('attachment_placement'),dict):
        from attachment_placement import score_attachments
        measured=copy(placement)
        measured.attachment_model={key:deepcopy(data['attachment_placement'].get(key,[]))
                                   for key in ('groups','links')}
        measured.attachment_index=dict(comp_index)
        data['attachment_placement']['current']=score_attachments(measured)
        data['attachment_placement']['measurement_stage']='final_output'
    if 'presentation' in data:
        from presentation import configure_presentation
        data['presentation']=configure_presentation(placement,source,data.get('source_cell'),comps,nets,
                                                     data['presentation'].get('style','legacy'))
    _check_source(source, expected_hash)
    for path, expected in (input_hashes or {}).items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise ValueError('Source placement JSON changed while the flow was running')
    data = dict(data, routes={net: segments for net, segments in routed},
                metrics=metrics, validation=validation)
    placement.routing_stage = data.get('routing_stage', 'normal')
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.sa-stage-', dir=destination) as directory:
        staging = Path(directory)
        for clean, name in ((False, names[0]), (True, names[1])):
            render_svg(par, placement, comps, nets, comp_index, routed, metrics,
                       staging / name, clean=clean,
                       structs=(data.get('layout_evidence', {}).get('structures', [])
                                if data.get('structure_display') == 'validated' else data.get('structs', [])),
                       validation=validation)
        write_json(staging / names[2], data)
        _check_source(source, expected_hash)
        for path, expected in (input_hashes or {}).items():
            if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
                raise ValueError('Source placement JSON changed while the flow was running')
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
    if net_labels == 'auto':
        # This optional view runs only after the original successful artifacts
        # are saved.  Display failures cannot replace their validation/Q.
        try:
            from net_labels import generate
            view_result = generate(destination/names[2], out_dir=destination/'net-labels')
            plugin_diagnostics = destination/'net-labels'/'plugin-diagnostics.json'
            if plugin_diagnostics.is_file():
                plugin_diagnostics.unlink()
            print(f"Net-label view: {view_result['view']['status']}; "
                  f"{view_result['artifacts']['json']}", flush=True)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            write_json(destination/'net-labels'/'plugin-diagnostics.json', {
                'status': 'failed', 'source_placement': str(destination/names[2]),
                'error': str(exc), 'full_route_result_preserved': True})
            print(f'Net-label view failed; full routes preserved: {exc}', flush=True)
    return data


def reroute_placement(jpath, out_dir=None, analog_canvas_root=None, routing_options=None,
                      *, local_rescue=None, rescue_seconds=60,
                      simplify_routes='safe', simplify_seconds=5,presentation=None, net_labels='off'):
    rescue_policy = _rescue_policy(local_rescue, False)
    _validate_rescue_seconds(rescue_seconds)
    _validate_simplification(simplify_routes, simplify_seconds)
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
    if presentation is not None:
        if presentation not in ('legacy','clean'):raise ValueError('presentation must be legacy or clean')
        metadata['presentation']={'style':presentation}
    options = routing_options or saved_routing_options(data)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    routed_result = None
    if is_bus_layout(data):
        from fallback_bus import reroute_fallback
        routed_result = reroute_fallback(par, placement, comps, nets, comp_index, metadata,
                                          routing_options=options, deadline=perf_counter() + 60)
    elif rescue_policy == 'auto' and rescue_seconds > 0:
        _, diagnostic_path = _output_targets(base, data.get('mode', 'layered'), destination,
                                             {source, source_json})
        stages = []
        started = perf_counter()
        try:
            routed_result = final_route(par, nets, placement, comp_index, options=options)
        except ROUTING_FAILURES as exc:
            if getattr(exc, 'diagnostics', {}).get('reason') == 'power_connections':
                raise
            _check_source(source, source_hash)
            stages.append(_failed_stage('normal', exc, perf_counter() - started))
            # These coordinates are the entire recovery boundary: fixed
            # rerouting never invokes spacing or constructive relayout.
            context = deepcopy((placement, comps, nets, comp_index, metadata))
            result = _try_local_rescue(context, getattr(exc, '_rescue_context', None),
                                       options, rescue_policy, rescue_seconds, stages)
            _check_source(source, source_hash)
            if result is None:
                failure = RoutingFailure('No verified fixed-placement route completed',
                    {'reason': 'recovery_failed', 'stages': deepcopy(stages)})
                _record_failure(failure, diagnostic_path, source)
                raise failure from exc
            placement, comps, nets, comp_index, metadata, routed_result = result
            metadata['recovery'] = {'policy': 'off', 'local_rescue': rescue_policy,
                                    'rescue_seconds': rescue_seconds, 'stages': stages}
        else:
            _check_source(source, source_hash)
            stages.append(_valid_stage('normal',
                (placement, comps, nets, comp_index, metadata, routed_result),
                perf_counter() - started))
            metadata['routing_stage'] = 'normal'
            metadata['recovery'] = {'policy': 'off', 'local_rescue': rescue_policy,
                                    'rescue_seconds': rescue_seconds, 'stages': stages}
    return _finish(metadata, par, placement, comps, nets, comp_index, base, destination,
                   protected_paths={source, source_json},
                   options=options, routed_result=routed_result,
                   simplify_routes=simplify_routes, simplify_seconds=simplify_seconds,
                   net_labels=net_labels)


def simplify_placement(jpath, out_dir=None, analog_canvas_root=None, routing_options=None,
                       *, simplify_routes='safe', simplify_seconds=5,presentation=None, net_labels='off'):
    """Validate and simplify saved wires without invoking any layout/router."""
    _validate_simplification(simplify_routes, simplify_seconds)
    source_json = Path(jpath).expanduser().resolve()
    original_hash = hashlib.sha256(source_json.read_bytes()).hexdigest()
    data, source, par, placement, comps, nets, comp_index = load_placement(
        source_json, analog_canvas_root=analog_canvas_root)
    if 'routes' not in data or not isinstance(data['routes'], dict):
        raise ValueError('--simplify-placement requires a saved routes object')
    base = source_json.stem.removesuffix('-placement')
    destination = (Path(out_dir).expanduser().resolve() if out_dir else
                   ROOT / 'output' / f'{base}-simplified')
    _, diagnostic_path = _output_targets(base, data.get('mode', 'layered'), destination,
                                         {source, source_json})
    options = routing_options or saved_routing_options(data)
    try:
        for net, segments in data['routes'].items():
            if not isinstance(segments, list):
                raise ValueError(f'Saved routes for {net} must be a list of segments')
        routed_result = validate_final_routes(par, nets, placement, comp_index,
            list(sorted(data['routes'].items())), options=options)
    except (TypeError, IndexError) as exc:
        raise ValueError('Malformed saved routes') from exc
    except ValueError as exc:
        _record_failure(exc, diagnostic_path, source)
        raise
    routed, metrics, checked = routed_result
    validation = deepcopy(data.get('validation', {}))
    original_router = validation.get('router')
    validation.update(checked)
    if original_router is not None:
        validation['router'] = original_router
    metadata = {key: deepcopy(value) for key, value in data.items()
                if key not in ('routes', 'metrics', 'validation')}
    metadata.update(source_netlist=str(source), analog_canvas_root=str(resource_root()),
                    simplified_from=str(source_json))
    if presentation is not None:
        if presentation not in ('legacy','clean'):raise ValueError('presentation must be legacy or clean')
        metadata['presentation']={'style':presentation}
    return _finish(metadata, par, placement, comps, nets, comp_index, base, destination,
                   protected_paths={source, source_json}, options=options,
                   routed_result=(routed, metrics, validation),
                   simplify_routes=simplify_routes, simplify_seconds=simplify_seconds,
                   allow_verified_replay=True, input_hashes={source_json: original_hash},
                   net_labels=net_labels)


def main(argv=None, default_mode='layered'):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('netlist', nargs='?', help='SPICE input file (.cir/.sp/.spi)')
    parser.add_argument('--reroute-placement', help='Reuse saved coordinates; skip SA and reroute')
    parser.add_argument('--simplify-placement', help='Validate and simplify saved routes; no SA or A*')
    parser.add_argument('--mode', choices=('baseline', 'layered'), default=default_mode)
    parser.add_argument('--layout-style', choices=('structured', 'legacy'), default='structured')
    parser.add_argument('--passive-layout', choices=('flexible', 'fixed'), default='flexible',
                        help='Passive mobility for new structured candidates; saved coordinates are preserved')
    parser.add_argument('--symmetry-layout', choices=('auto', 'off'), default='off',
                        help='Device-only symmetry for new structured candidates 2/3 (default: off); saved coordinates are preserved')
    parser.add_argument('--structure-layout', choices=('auto', 'off'), default='off',
                        help='Role-aware structure layout for new candidates 2/3 (default: off)')
    parser.add_argument('--structure-phase', type=int, choices=(1, 2, 3), default=3,
                        help='Replay a structure rollout phase; ignored when structure-layout is off')
    parser.add_argument('--quality-seconds', type=float, default=300)
    parser.add_argument('--candidate-budget',choices=('sequential','reserved'),default='sequential',
                        help='Optional 70/85/100 percent deadlines; default preserves sequential routing')
    parser.add_argument('--port-preflight',choices=('off','repair'),default='off',
                        help='Repair static passive port/marker conflicts before routing; default off')
    parser.add_argument('--presentation',choices=('legacy','clean'),
                        help='Clean publication labels and bounds; default keeps saved or legacy style')
    parser.add_argument('--net-labels', choices=('off', 'auto'), default='off',
                        help='Optional extra net-label view after verified full routes (default: off)')
    parser.add_argument('--candidate-id',
                        help='Replay candidate 1, 2 or 3; 0 explicitly runs the legacy reference')
    parser.add_argument('--fallback', choices=('auto', 'off', 'only'),
                        help='Default: auto for netlists, off for candidate replay')
    parser.add_argument('--local-rescue', choices=('auto', 'off'),
                        help='Failure-only local routing; defaults to fallback policy, off for saved rerouting')
    parser.add_argument('--rescue-seconds', type=float, default=60)
    parser.add_argument('--simplify-routes', choices=('safe', 'off'), default='safe')
    from simplify_budget import parse_simplify_seconds
    parser.add_argument('--simplify-seconds', type=parse_simplify_seconds, default=5,
                        help='Fixed non-negative seconds, or auto (5-30s); default: 5')
    parser.add_argument('--spacing-scale', type=float, default=1.5)
    parser.add_argument('--spacing-seconds', type=float, default=60)
    parser.add_argument('--fallback-seconds', type=float, default=60)
    parser.add_argument('--iterations', type=int, default=20000)
    parser.add_argument('--seed', type=int, default=20261029)
    parser.add_argument('--out-dir', help='Output directory (default: project/output/<netlist label>)')
    parser.add_argument('--cell', help='Explicit .subckt name; no recursive expansion')
    parser.add_argument('--analog-canvas-root', help='Read-only original project directory')
    args = parser.parse_args(argv)
    try:
        if sum(map(bool, (args.netlist, args.reroute_placement, args.simplify_placement))) != 1:
            parser.error('provide exactly one netlist, --reroute-placement JSON or --simplify-placement JSON')
        if args.simplify_placement:
            if args.fallback in ('auto', 'only') or args.local_rescue == 'auto':
                parser.error('--simplify-placement only uses saved routes; it cannot request recovery')
            if args.port_preflight!='off':parser.error('saved coordinates cannot request port repair')
            simplify_placement(args.simplify_placement, out_dir=args.out_dir,
                               analog_canvas_root=args.analog_canvas_root,
                               simplify_routes=args.simplify_routes, simplify_seconds=args.simplify_seconds,
                               net_labels=args.net_labels,
                               **({'presentation':args.presentation} if args.presentation is not None else {}))
        elif args.reroute_placement:
            if args.fallback in ('auto', 'only'):
                parser.error('--reroute-placement preserves coordinates; it cannot request fallback relayout')
            if args.port_preflight!='off':parser.error('saved coordinates cannot request port repair')
            reroute_placement(args.reroute_placement, out_dir=args.out_dir,
                              analog_canvas_root=args.analog_canvas_root,
                              local_rescue=args.local_rescue, rescue_seconds=args.rescue_seconds,
                              simplify_routes=args.simplify_routes, simplify_seconds=args.simplify_seconds,
                              net_labels=args.net_labels,
                              **({'presentation':args.presentation} if args.presentation is not None else {}))
        else:
            run(args.netlist, mode=args.mode, iterations=args.iterations, seed=args.seed,
                out_dir=args.out_dir, cell=args.cell, analog_canvas_root=args.analog_canvas_root,
                layout_style=args.layout_style, quality_seconds=args.quality_seconds, candidate_id=args.candidate_id,
                passive_layout=args.passive_layout,
                symmetry_layout=args.symmetry_layout,
                structure_layout=args.structure_layout,
                structure_phase=args.structure_phase,
                candidate_budget=args.candidate_budget,port_preflight=args.port_preflight,
                presentation=args.presentation or 'legacy',
                fallback=args.fallback, spacing_scale=args.spacing_scale,
                spacing_seconds=args.spacing_seconds, fallback_seconds=args.fallback_seconds,
                local_rescue=args.local_rescue, rescue_seconds=args.rescue_seconds,
                simplify_routes=args.simplify_routes, simplify_seconds=args.simplify_seconds,
                net_labels=args.net_labels)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f'Error: {exc}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
