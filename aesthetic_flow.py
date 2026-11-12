"""Compare a bounded collection of layouts using actual validated routes."""
from __future__ import annotations

from copy import deepcopy
from math import isfinite
from time import perf_counter

from candidate_budget import routing_deadline, validate_policy
from sa_common import load_local
from sa_routing import RoutingValidationError, final_route


_PLACEMENT_BUDGET_MESSAGES = frozenset({
    'Quality budget exhausted before placement initialization',
    'Quality budget exhausted during legacy placement initialization',
})

SYMMETRY_SELECTION = 'device-symmetry-v1'
SYMMETRY_LENGTH_RATIO = 1.25


def _select_symmetric_result(results):
    """Select within a real-wire budget, retaining the original quality score.

    The reference is the original-Q winner of this invocation, before route
    simplification.  It is not a historical output or an unrouted estimate.
    """
    reference = min(results, key=lambda item: item[0])
    length = reference[6][1]['wirelength']
    limit = length * SYMMETRY_LENGTH_RATIO
    eligible = [item for item in results
                if item[6][1]['wirelength'] <= limit + 1e-9]

    def measurement(item):
        return item[5].get('layout_symmetry', {}).get('current', {})

    comparable = [item for item in eligible
                  if measurement(item).get('relation_count', 0) > 0]
    if comparable:
        def key(item):
            score = measurement(item)
            return (score['failed_relations'], score['residual_bucket'], *item[0])
        selected = min(comparable, key=key)
    else:
        selected = reference
    report = {
        'policy': SYMMETRY_SELECTION if comparable else 'balanced-v1',
        'reference_candidate': reference[8],
        'reference_wirelength': length, 'length_ratio': SYMMETRY_LENGTH_RATIO,
        'wirelength_limit': limit, 'reference_score': reference[7]['score'],
        'reference_stage': 'before_route_simplification',
        'selected_candidate': selected[8],
        'selected_wirelength': selected[6][1]['wirelength'],
        'eligible_candidates': [item[8] for item in eligible],
        'excluded_candidates': [item[8] for item in results if item not in eligible],
        'reason': 'symmetry_then_original_quality' if comparable else 'no_symmetry_relations',
        'ranking': ['failed_relations', 'residual_bucket', 'original_quality',
                    'crossings', 'wirelength', 'candidate_id'],
    }
    return selected, report


def select_layout(path, *, cell=None, iterations=20000, seed=20261029,
                  quality_seconds=300, routing_options=None, candidate_id=None,
                  passive_layout='flexible', symmetry_layout='off', structure_layout='off',
                  structure_phase=3, candidate_budget='sequential',port_preflight='off',
                  region_layout='off', readability_layout='off', readability_phase=5,
                  route_tradeoff='strict'):
    validate_policy(candidate_budget)
    if port_preflight not in ('off','repair'):
        raise ValueError('port_preflight must be off or repair')
    if type(quality_seconds) not in (int, float) or not isfinite(quality_seconds) or quality_seconds <= 0:
        raise ValueError('quality_seconds must be positive and finite')
    if candidate_id is not None and str(candidate_id) not in ('0', '1', '2', '3'):
        raise ValueError('candidate_id must be one of 0, 1, 2, 3')
    if passive_layout not in ('flexible', 'fixed'):
        raise ValueError('passive_layout must be flexible or fixed')
    if symmetry_layout not in ('auto', 'off'):
        raise ValueError('symmetry_layout must be auto or off')
    if structure_layout not in ('auto', 'off'):
        raise ValueError('structure_layout must be auto or off')
    if region_layout not in ('auto', 'off'):
        raise ValueError('region_layout must be auto or off')
    if region_layout == 'auto' and (structure_layout == 'auto' or symmetry_layout == 'auto'):
        raise ValueError('region_layout auto cannot be combined with structure_layout auto or symmetry_layout auto')
    from run_flow import _validate_readability_policy, _validate_route_tradeoff
    _validate_readability_policy(readability_layout, readability_phase,
                                 region_layout, structure_layout, symmetry_layout)
    _validate_route_tradeoff(route_tradeoff)
    if type(structure_phase) is not int or structure_phase not in (1, 2, 3):
        raise ValueError('structure_phase must be 1, 2 or 3')
    from structured_placement import create_candidates, feedback_candidate
    from layout_quality import evaluate_quality
    from orthogonal_router import RoutingFailure
    from routing_rules import RoutingOptions
    started = perf_counter()
    deadline = started + quality_seconds
    par = load_local('place_and_route', 'place-and-route.py')
    records, best = [], None
    shared_relations = None
    shared_region_plan = None
    shared_readability_plan = None
    valid_results = []
    retry_context = retry_options = rescue_context = None
    candidate_data_error = False
    effective_options = routing_options if routing_options is not None else RoutingOptions()
    include_legacy = str(candidate_id) == '0'
    budget_policy = candidate_budget if candidate_id is None else 'sequential'
    candidate_limit = 1 if include_legacy else 3
    candidate_policy = 'legacy_reference_replay' if include_legacy else 'structured_only'
    stop_reason = 'candidates_exhausted'
    try:
        candidates = create_candidates(path, cell=cell, iterations=iterations,
                                       seed=seed, deadline=deadline, passive_layout=passive_layout,
                                       include_legacy=include_legacy, structure_layout=structure_layout,
                                       structure_phase=structure_phase, region_layout=region_layout,
                                       readability_layout=readability_layout,
                                       readability_phase=readability_phase,
                                       **({'routing_options': effective_options}
                                          if readability_layout == 'auto' and readability_phase >= 5 else {}))
        for number, item in enumerate(candidates):
            if number >= candidate_limit:
                stop_reason = 'candidate_limit'
                break
            placement, comps, nets, index, metadata = item
            identifier = str(metadata.get('candidate_id', 0 if include_legacy else number + 1))
            if region_layout == 'auto' and metadata.get('layout_regions', {}).get('plan') is not None:
                shared_region_plan = deepcopy(metadata['layout_regions']['plan'])
            if readability_layout == 'auto' and metadata.get('layout_readability', {}).get('plan') is not None:
                shared_readability_plan = deepcopy(metadata['layout_readability']['plan'])
            replay_feedback = str(candidate_id) == '3'
            if candidate_id is not None and str(candidate_id) != identifier and not replay_feedback:
                continue
            generated_at = perf_counter()
            if generated_at >= deadline:
                stop_reason = 'time_budget'
                break
            candidate_deadline = routing_deadline(started, quality_seconds, identifier,
                                                  policy=budget_policy)
            budget_record = {'policy': budget_policy,
                             'cutoff_seconds': candidate_deadline - started}
            if budget_policy == 'reserved' and generated_at >= candidate_deadline:
                records.append({'candidate_id': identifier,
                                'seed': metadata.get('candidate_seed', seed),
                                'status': 'skipped', 'reason': 'candidate_time_budget',
                                'candidate_budget': dict(budget_record,
                                    stop_reason='candidate_time_budget',
                                    phase='after_generation')})
                continue
            # Reserve candidate 3 for measured congestion, independently of its
            # position in the generator (the default sequence starts at 1).
            # when a prior route has a substantial, clearly excessive detour.
            # Small unavoidable local loops still get the compact third seed.
            if (readability_layout == 'auto' and readability_phase >= 3
                    and identifier in ('2', '3') and shared_readability_plan is not None):
                from readability_selection import reference_seed
                reused = reference_seed(par, placement, comps, nets, index, metadata,
                                        valid_results, shared_readability_plan, phase=readability_phase)
                if reused is not None:
                    placement, comps, nets, index, metadata = reused
            feedback_replaced = False
            readability_feedback_state = None
            readability_feedback_statistics = {}
            feedback_best = best
            local_refinement_deadline = min(candidate_deadline, perf_counter()+1)
            if readability_layout == 'auto' and identifier == '3':
                from readability_selection import feedback_compatible
                compatible = [row for row in valid_results if feedback_compatible(row[5], metadata)]
                feedback_best = min(compatible, key=lambda row: row[0]) if compatible else None
                if feedback_best is not None and best is not None:
                    reference_metrics, proposal_metrics = best[6][1], feedback_best[6][1]
                    cross_cap = reference_metrics['crossings'] + (2 if route_tradeoff == 'balanced'
                        else 1 if reference_metrics['crossings'] else 0)
                    if (proposal_metrics['wirelength'] > reference_metrics['wirelength']*1.15+1e-8
                            or proposal_metrics['crossings'] > cross_cap):
                        feedback_best = None
                        metadata.setdefault('layout_readability', {})['feedback_skipped'] = (
                            'reference_outside_original_quality_routing_limits')
            if (region_layout == 'auto' and identifier == '3'
                    and shared_region_plan is not None and shared_region_plan.get('supported')):
                compatible = [row for row in valid_results
                              if _region_feedback_compatible(row[5], metadata)]
                feedback_best = min(compatible, key=lambda row: row[0]) if compatible else None
            if identifier == '3' and feedback_best is not None:
                detail = feedback_best[7]
                severe = any((row.get('detour_factor') or 0) >= 2.5
                             and row.get('excess_detour', 0) >= 500
                             for row in detail.get('per_net', {}).values())
                if readability_layout == 'auto' and readability_phase >= 5:
                    from readability_routing import feedback_readability
                    proposal = feedback_readability(par, feedback_best[1], feedback_best[2],
                        feedback_best[3], feedback_best[4], feedback_best[5], feedback_best[6],
                        options=effective_options, deadline=local_refinement_deadline,
                        statistics=readability_feedback_statistics)
                    if proposal is not None:
                        placement, comps, nets, index, metadata = proposal
                        metadata['candidate_id'] = int(identifier)
                        feedback_replaced = True
                        readability_feedback_state = deepcopy((placement, metadata))
                    severe = False
                if severe:
                    proposal = feedback_candidate(feedback_best[1], feedback_best[2], feedback_best[3], feedback_best[4],
                                                  feedback_best[5], detail)
                    if proposal is not None and (structure_layout == 'off' or
                            proposal[4].get('branch_row_plan', {}).get('rows') ==
                            metadata.get('branch_row_plan', {}).get('rows')):
                        placement, comps, nets, index, metadata = proposal
                        metadata['candidate_id'] = int(identifier)
                        metadata['candidate_seed'] = feedback_best[5].get('candidate_seed', seed)
                        feedback_replaced = True
            if readability_layout == 'auto' and identifier in ('2', '3') and readability_phase >= 3:
                from readability_refinement import refine_readability
                placement, metadata = refine_readability(
                    par, placement, comps, nets, index, metadata, options=effective_options,
                    deadline=local_refinement_deadline, seconds=max(0, local_refinement_deadline-perf_counter()),
                    max_candidates=max(0, 128-readability_feedback_statistics.get('candidates', 0)),
                    phase=min(readability_phase, 4))
                if readability_feedback_state is not None:
                    placement, metadata = _preserve_readability_feedback(
                        par, placement, comps, nets, index, metadata, readability_feedback_state,
                        effective_options, local_refinement_deadline)
            # Apply to the actual layout after feedback replacement, before
            # capturing a checkpoint.  Keep candidate 1 as the original seed.
            if symmetry_layout == 'auto' and identifier in ('1', '2', '3'):
                from symmetry_layout import regularize_symmetry, report_symmetry
                if identifier in ('2', '3'):
                    placement, metadata = regularize_symmetry(
                        par, placement, comps, nets, index, metadata,
                        options=effective_options, deadline=candidate_deadline)
                else:
                    metadata = deepcopy(metadata)
                    metadata['layout_symmetry'] = report_symmetry(
                        placement, comps, index, metadata, policy=symmetry_layout,
                        reason='reference_candidate', grid_step=effective_options.grid_step)
            if structure_layout == 'auto' and identifier != '0':
                from layout_relations import build_layout_relations, measure_layout_relations
                if shared_relations is None:
                    from netlist_core import parse_netlist
                    devices = parse_netlist(path, cell=cell)
                    evidence = metadata.get('layout_evidence')
                    if evidence is None:
                        from layout_intent import build_intent
                        evidence, _ = build_intent(path, cell, devices, metadata.get('structs', ()),
                                                   comps, nets, index, structure_phase)
                    shared_relations = build_layout_relations(devices, validated_evidence=evidence)
                metadata = deepcopy(metadata)
                metadata['layout_relations'] = deepcopy(shared_relations)
                metadata['structure_display'] = 'validated'
                if 'layout_evidence' not in metadata:
                    metadata['layout_evidence'] = deepcopy(evidence)
                    metadata['layout_evidence']['consumption'] = {
                        'policy': 'report_only' if identifier == '1' else 'candidate_layout',
                        'candidate_id': int(identifier), 'stages': []}
                if feedback_replaced:
                    from layout_relations import prepare_structure_placement
                    prepare_structure_placement(placement, comps, index, shared_relations)
                    metadata['layout_evidence']['consumption'].update(
                        policy='candidate_layout', candidate_id=int(identifier))
                    metadata['layout_evidence']['consumption']['stages'].append({
                        'stage': 'feedback_blocks', 'status': 'applied',
                        'relation_ids': [row['id'] for row in shared_relations
                                         if row.get('rigid') or row.get('macro')]})
                if structure_phase >= 3:
                    from structure_selection import prepare_branch_context
                    metadata = prepare_branch_context(path, cell, placement, comps, nets, index, metadata)
                    if identifier in ('2', '3'):
                        from branch_alignment import regularize_branch_layout
                        parallel_groups = metadata.get('attachment_measurement_groups', ())
                        if any(row.get('kind') == 'parallel_load' for row in parallel_groups):
                            from attachment_placement import refine_parallel_attachments, measure_parallel_coherence
                            from branch_alignment import _hpwl
                            micro_deadline = min(candidate_deadline, perf_counter()+1)
                            micro_hpwl = _hpwl(par, placement, nets, index)
                            placement, metadata = refine_parallel_attachments(
                                par, placement, comps, nets, index, metadata,
                                options=effective_options, deadline=micro_deadline,
                                seconds=1, max_candidates=128)
                            used = metadata.get('parallel_attachment_refinement', {}).get('candidates', 0)
                            placement, metadata = regularize_branch_layout(
                                par, placement, comps, nets, index, metadata,
                                options=effective_options, deadline=micro_deadline,
                                seconds=max(0, micro_deadline-perf_counter()), max_candidates=max(0, 128-used),
                                hpwl_reference=micro_hpwl)
                            metadata['parallel_attachment_refinement']['current'] = measure_parallel_coherence(
                                placement, index, parallel_groups)
                        else:
                            placement, metadata = regularize_branch_layout(
                                par, placement, comps, nets, index, metadata,
                                options=effective_options, deadline=candidate_deadline)
                        consumption = metadata['layout_evidence'].setdefault('consumption', {
                            'policy': 'candidate_layout', 'candidate_id': int(identifier), 'stages': []})
                        consumption['stages'].append({'stage': 'post_alignment', 'status': 'applied',
                            'relation_ids': [row['id'] for row in shared_relations],
                            'position_changes': deepcopy(metadata.get('branch_alignment', {}).get('actions', []))})
                report = metadata.setdefault('structure_layout', {
                    'policy': 'auto', 'version': 'structure-layout-v1', 'phase': structure_phase,
                    'applied': False})
                report['reference_candidate'] = identifier == '1'
                from structure_selection import measure_structure
                report['metrics'] = measure_structure(par, placement, comps, nets, index,
                                                     metadata, structure_phase)
            if port_preflight == 'repair':
                from placement_preflight import repair_static_ports
                placement, preflight_report = repair_static_ports(
                    par,placement,comps,nets,index,effective_options,deadline=candidate_deadline)
                metadata=deepcopy(metadata)
                metadata['port_preflight']=preflight_report
                if structure_layout=='auto' and 'structure_layout' in metadata:
                    from structure_selection import measure_structure
                    metadata['structure_layout']['metrics']=measure_structure(
                        par,placement,comps,nets,index,metadata,structure_phase)
            if region_layout == 'auto' and shared_region_plan is not None:
                metadata = _measure_region_candidate(placement, comps, index, metadata,
                                                     shared_region_plan, identifier)
            if readability_layout == 'auto' and shared_readability_plan is not None:
                from readability_selection import measure_candidate
                metadata = measure_candidate(par, placement, comps, nets, index, metadata,
                    shared_readability_plan, phase=readability_phase, stage='before_final_routing')
            routing_at = perf_counter()
            print(f'Quality candidate {identifier}: final routing, '
                  f'budget remaining={candidate_deadline-routing_at:.1f}s', flush=True)
            record = {'candidate_id': identifier, 'seed': metadata.get('candidate_seed', seed),
                      'annealing': deepcopy(metadata.get('annealing', {})),
                      'passive_layout_policy': metadata.get('passive_layout_policy', 'fixed'),
                      'passive_layout': deepcopy(metadata.get('passive_layout', {})),
                      'candidate_budget': dict(budget_record,
                          remaining_seconds_at_start=candidate_deadline-routing_at)}
            if 'layout_symmetry' in metadata:
                record['layout_symmetry'] = deepcopy(metadata['layout_symmetry'])
            if 'structure_layout' in metadata:
                record['structure_layout'] = deepcopy(metadata['structure_layout'])
            if 'layout_regions' in metadata:
                record['layout_regions'] = deepcopy(metadata['layout_regions'])
            if 'layout_readability' in metadata:
                record['layout_readability'] = deepcopy(metadata['layout_readability'])
            if 'parallel_attachment_refinement' in metadata:
                record['parallel_attachment_refinement'] = deepcopy(metadata['parallel_attachment_refinement'])
            if 'port_preflight' in metadata:
                record['port_preflight']=deepcopy(metadata['port_preflight'])
            if budget_policy == 'reserved' and routing_at >= candidate_deadline:
                global_expired = routing_at >= deadline
                reason = 'quality_time_budget' if global_expired else 'candidate_time_budget'
                record.update(status='skipped', reason=reason)
                record['candidate_budget'].update(stop_reason=reason, phase='before_routing')
                records.append(record)
                if global_expired:
                    stop_reason = 'time_budget'
                    break
                continue
            # A router or suspended producer may mutate/reuse its own buffers.
            # Keep only the last actually attempted candidate, after any feedback
            # replacement, and never serialize this executable placement object.
            retry_context = deepcopy((placement, comps, nets, index, metadata))
            retry_options = deepcopy(effective_options)
            # A checkpoint belongs to this exact routing attempt.  In
            # particular, a later failure without a checkpoint must never
            # recover an earlier candidate's already-routed wires.
            rescue_context = None
            try:
                routed, metrics, validation = final_route(
                    par, nets, placement, index, options=effective_options, deadline=candidate_deadline)
                quality = evaluate_quality(par, placement, comps, nets, index,
                                           routed, metrics, metadata.get('structs', []))
                record.update(status='valid', metrics=deepcopy(metrics), score=quality['score'],
                              quality=deepcopy(quality),
                              routing_seconds=validation.get('routing_seconds', 0))
                if metadata.get('feedback'):
                    record['feedback'] = deepcopy(metadata['feedback'])
                key = (quality['score'], metrics['crossings'], metrics['wirelength'], identifier)
                frozen = deepcopy((key, placement, comps, nets, index, metadata,
                                   (routed, metrics, validation), quality, identifier))
                valid_results.append(frozen)
                if best is None or key < best[0] or (replay_feedback and identifier == '3'):
                    # Candidate producers and routers may reuse their buffers.
                    # Freeze the complete incumbent, including the final wires.
                    best = frozen
                print(f"Quality candidate {identifier}: score={quality['score']:.3f}, "
                      f"WL={metrics['wirelength']:.0f}, X={metrics['crossings']}", flush=True)
            except (RoutingFailure, RoutingValidationError) as exc:
                if getattr(exc, 'diagnostics', {}).get('reason') == 'power_connections':
                    raise
                rescue_context = deepcopy(getattr(exc, '_rescue_context', None))
                diagnostics = getattr(exc, 'diagnostics', {})
                local_timeout = (diagnostics.get('reason') == 'quality_time_budget'
                                 and budget_policy == 'reserved'
                                 and candidate_deadline < deadline
                                 and perf_counter() < deadline)
                if local_timeout:
                    diagnostics = deepcopy(diagnostics)
                    diagnostics.update(reason='candidate_time_budget',
                                       original_reason='quality_time_budget')
                    record['candidate_budget']['stop_reason'] = 'candidate_time_budget'
                record.update(status='failed', message=str(exc), diagnostics=diagnostics)
                print(f'Quality candidate {identifier}: {exc}', flush=True)
                if diagnostics.get('reason') == 'quality_time_budget':
                    stop_reason = 'time_budget'
                    records.append(record)
                    break
            except ValueError:
                # Malformed geometry or scoring data is not a search failure,
                # even if an earlier candidate happened to route successfully.
                candidate_data_error = True
                raise
            records.append(record)
            if candidate_id is not None and str(candidate_id) == identifier:
                stop_reason = 'replay_complete'
                break
    except ValueError as exc:
        if candidate_data_error or type(exc) is not ValueError:
            raise
        if str(exc) in _PLACEMENT_BUDGET_MESSAGES:
            records.append({'status': 'generation_stopped', 'message': str(exc),
                            'diagnostics': {'reason': 'quality_time_budget'}})
            stop_reason = 'time_budget'
        elif best is None:
            raise
        else:
            # Preserve a verified incumbent if later candidate generation stops;
            # with no verified candidate, ordinary parse/data errors propagate.
            records.append({'status': 'generation_stopped', 'message': str(exc)})
            stop_reason = 'generation_stopped'
    if stop_reason == 'candidates_exhausted' and perf_counter() >= deadline:
        stop_reason = 'time_budget'
    if best is None or (candidate_id is not None and best[8] != str(candidate_id)):
        failure = RoutingFailure('No verified layout candidate completed within the quality budget',
                                 {'reason': stop_reason, 'candidates': records,
                                  'requested_candidate': candidate_id,
                                  'quality_seconds': quality_seconds,
                                  'candidate_policy': candidate_policy,
                                  'candidate_limit': candidate_limit,
                                  'legacy_reference_enabled': include_legacy,
                                  'candidate_budget_policy': budget_policy,
                                  'requested_candidate_budget': candidate_budget,
                                  'requested_symmetry_layout': symmetry_layout})
        # A valid incumbent from replay prerequisites must not silently trigger
        # recovery for a failed explicitly requested candidate.
        if best is None:
            failure.retry_context = retry_context
            failure.retry_options = retry_options or deepcopy(effective_options)
            if rescue_context is not None:
                failure._rescue_context = rescue_context
        raise failure
    # Feedback above deliberately uses the original-Q incumbent.  Only after
    # all routing attempts do device symmetry and the 125% wire cap choose the
    # delivered candidate. Explicit replay always returns its requested ID.
    symmetry_selection = structure_selection = region_selection = readability_selection = None
    if readability_layout == 'auto' and shared_readability_plan is not None:
        from readability_selection import measure_candidate, select_readable_result
        for result in valid_results:
            measured = measure_candidate(par, result[1], result[2], result[3], result[4], result[5],
                shared_readability_plan, phase=readability_phase, stage='before_route_simplification')
            result[5].update(measured)
            for record in records:
                if record.get('candidate_id') == result[8]:
                    record['layout_readability'] = deepcopy(measured['layout_readability'])
        if candidate_id is None:
            best, readability_selection = select_readable_result(valid_results, tradeoff=route_tradeoff)
    elif region_layout == 'auto' and shared_region_plan is not None:
        # The unmodified reference was generated/routed before region analysis.
        # Measure it now with the exact same immutable evidence as candidates 2/3.
        for result in valid_results:
            measured = _measure_region_candidate(result[1], result[2], result[4],
                                                  result[5], shared_region_plan, result[8])
            result[5].update(measured)
            for record in records:
                if record.get('candidate_id') == result[8]:
                    record['layout_regions'] = deepcopy(measured['layout_regions'])
        if candidate_id is None:
            from region_layout import select_region_result
            best, region_selection = select_region_result(valid_results)
    elif structure_layout == 'auto' and candidate_id is None:
        from structure_selection import select_structured_result
        best, structure_selection = select_structured_result(valid_results)
    elif symmetry_layout == 'auto' and candidate_id is None:
        best, symmetry_selection = _select_symmetric_result(valid_results)
    _, placement, comps, nets, index, metadata, routed_result, quality, identifier = best
    metadata.update(layout_style='structured', quality={
        'policy': 'balanced-v1', 'budget_seconds': quality_seconds,
        'candidate_policy': candidate_policy, 'candidate_limit': candidate_limit,
        'legacy_reference_enabled': include_legacy,
        'candidate_budget_policy': budget_policy,
        'requested_candidate_budget': candidate_budget,
        'elapsed_seconds': round(perf_counter()-started, 6), 'stop_reason': stop_reason,
        'selected_candidate': identifier, 'score': quality['score'],
        'details': quality, 'candidates': records})
    metadata['quality']['selection_policy'] = (
        readability_selection['policy'] if readability_selection else
        region_selection['policy'] if region_selection else
        structure_selection['policy'] if structure_selection else
        symmetry_selection['policy'] if symmetry_selection else
        'explicit_candidate_replay' if candidate_id is not None else 'balanced-v1')
    if symmetry_selection is not None:
        metadata['quality']['symmetry_selection'] = symmetry_selection
    if structure_selection is not None:
        metadata['quality']['structure_selection'] = structure_selection
    if region_selection is not None:
        metadata['quality']['region_selection'] = region_selection
    if readability_selection is not None:
        metadata['quality']['readability_selection'] = readability_selection
    return placement, comps, nets, index, metadata, routed_result


def _preserve_readability_feedback(par, pl, comps, nets, index, metadata, saved, options, deadline):
    """Later source/spacing refinements cannot silently undo a better escape."""
    from orthogonal_router import RoutingFailure
    from readability_routing import escape_risk
    from routing_rules import build_scene
    report = saved[1]['layout_readability']['routing_feedback']
    result = None
    reason = 'refinement_deadline'
    if perf_counter() < deadline:
        try:
            scene = build_scene(par, nets, pl, index, options=options, deadline=deadline)
            groups = [[(index[name] if isinstance(name, str) else name, pin)
                       for name, pin in group] for group in report['branch']['port_groups']]
            result = escape_risk(scene, report['net'], groups, deadline)
            reason = 'escape_risk_regression'
        except RoutingFailure:
            pass
    if result is None or tuple(result['key']) > tuple(report['after_risk']['key']):
        pl, metadata = saved
        metadata['layout_readability']['feedback_order_check'] = {
            'action': 'rolled_back_later_refinement', 'reason': reason,
            'feedback_risk_preserved': True}
    else:
        metadata['layout_readability']['feedback_order_check'] = {
            'action': 'kept_later_refinement', 'actual_risk': result,
            'feedback_risk_preserved': True}
    return pl, metadata


def _region_feedback_compatible(source, target):
    """An old/reference placement must never replace a region seed."""
    a, b = source.get('layout_regions', {}), target.get('layout_regions', {})
    ap, bp = a.get('plan', {}), b.get('plan', {})
    return bool(a.get('applied') and b.get('applied') and ap.get('signature')
                and ap['signature'] == bp.get('signature'))


def _measure_region_candidate(placement, comps, index, metadata, plan, identifier):
    from region_layout import measure_regions
    metadata = deepcopy(metadata)
    report = metadata.setdefault('layout_regions', {
        'policy': 'auto', 'version': plan.get('version'), 'applied': False,
        'reason': 'reference_candidate' if str(identifier) == '1' else 'report_only'})
    report.update(plan=deepcopy(plan), plan_signature=plan.get('signature'),
                  reference_candidate=str(identifier) == '1',
                  metrics=measure_regions(placement, comps, index, plan),
                  measurement_stage='before_route_simplification')
    return metadata
