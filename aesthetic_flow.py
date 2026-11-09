"""Compare a bounded collection of layouts using actual validated routes."""
from __future__ import annotations

from copy import deepcopy
from math import isfinite
from time import perf_counter

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
                  passive_layout='flexible', symmetry_layout='off'):
    if type(quality_seconds) not in (int, float) or not isfinite(quality_seconds) or quality_seconds <= 0:
        raise ValueError('quality_seconds must be positive and finite')
    if candidate_id is not None and str(candidate_id) not in ('0', '1', '2', '3'):
        raise ValueError('candidate_id must be one of 0, 1, 2, 3')
    if passive_layout not in ('flexible', 'fixed'):
        raise ValueError('passive_layout must be flexible or fixed')
    if symmetry_layout not in ('auto', 'off'):
        raise ValueError('symmetry_layout must be auto or off')
    from structured_placement import create_candidates, feedback_candidate
    from layout_quality import evaluate_quality
    from orthogonal_router import RoutingFailure
    from routing_rules import RoutingOptions
    started = perf_counter()
    deadline = started + quality_seconds
    par = load_local('place_and_route', 'place-and-route.py')
    records, best = [], None
    valid_results = []
    retry_context = retry_options = rescue_context = None
    candidate_data_error = False
    effective_options = routing_options if routing_options is not None else RoutingOptions()
    include_legacy = str(candidate_id) == '0'
    candidate_limit = 1 if include_legacy else 3
    candidate_policy = 'legacy_reference_replay' if include_legacy else 'structured_only'
    stop_reason = 'candidates_exhausted'
    try:
        candidates = create_candidates(path, cell=cell, iterations=iterations,
                                       seed=seed, deadline=deadline, passive_layout=passive_layout,
                                       include_legacy=include_legacy)
        for number, item in enumerate(candidates):
            if number >= candidate_limit:
                stop_reason = 'candidate_limit'
                break
            placement, comps, nets, index, metadata = item
            identifier = str(metadata.get('candidate_id', 0 if include_legacy else number + 1))
            replay_feedback = str(candidate_id) == '3'
            if candidate_id is not None and str(candidate_id) != identifier and not replay_feedback:
                continue
            if perf_counter() >= deadline:
                stop_reason = 'time_budget'
                break
            # Reserve candidate 3 for measured congestion, independently of its
            # position in the generator (the default sequence starts at 1).
            # when a prior route has a substantial, clearly excessive detour.
            # Small unavoidable local loops still get the compact third seed.
            if identifier == '3' and best is not None:
                detail = best[7]
                severe = any((row.get('detour_factor') or 0) >= 2.5
                             and row.get('excess_detour', 0) >= 500
                             for row in detail.get('per_net', {}).values())
                if severe:
                    proposal = feedback_candidate(best[1], best[2], best[3], best[4],
                                                  best[5], detail)
                    if proposal is not None:
                        placement, comps, nets, index, metadata = proposal
                        metadata['candidate_id'] = int(identifier)
                        metadata['candidate_seed'] = best[5].get('candidate_seed', seed)
            # Apply to the actual layout after feedback replacement, before
            # capturing a checkpoint.  Keep candidate 1 as the original seed.
            if symmetry_layout == 'auto' and identifier in ('1', '2', '3'):
                from symmetry_layout import regularize_symmetry, report_symmetry
                if identifier in ('2', '3'):
                    placement, metadata = regularize_symmetry(
                        par, placement, comps, nets, index, metadata,
                        options=effective_options, deadline=deadline)
                else:
                    metadata = deepcopy(metadata)
                    metadata['layout_symmetry'] = report_symmetry(
                        placement, comps, index, metadata, policy=symmetry_layout,
                        reason='reference_candidate', grid_step=effective_options.grid_step)
            print(f'Quality candidate {identifier}: final routing, '
                  f'budget remaining={deadline-perf_counter():.1f}s', flush=True)
            record = {'candidate_id': identifier, 'seed': metadata.get('candidate_seed', seed),
                      'annealing': deepcopy(metadata.get('annealing', {})),
                      'passive_layout_policy': metadata.get('passive_layout_policy', 'fixed'),
                      'passive_layout': deepcopy(metadata.get('passive_layout', {}))}
            if 'layout_symmetry' in metadata:
                record['layout_symmetry'] = deepcopy(metadata['layout_symmetry'])
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
                    par, nets, placement, index, options=effective_options, deadline=deadline)
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
    symmetry_selection = None
    if symmetry_layout == 'auto' and candidate_id is None:
        best, symmetry_selection = _select_symmetric_result(valid_results)
    _, placement, comps, nets, index, metadata, routed_result, quality, identifier = best
    metadata.update(layout_style='structured', quality={
        'policy': 'balanced-v1', 'budget_seconds': quality_seconds,
        'candidate_policy': candidate_policy, 'candidate_limit': candidate_limit,
        'legacy_reference_enabled': include_legacy,
        'elapsed_seconds': round(perf_counter()-started, 6), 'stop_reason': stop_reason,
        'selected_candidate': identifier, 'score': quality['score'],
        'details': quality, 'candidates': records})
    metadata['quality']['selection_policy'] = (
        symmetry_selection['policy'] if symmetry_selection else
        'explicit_candidate_replay' if candidate_id is not None else 'balanced-v1')
    if symmetry_selection is not None:
        metadata['quality']['symmetry_selection'] = symmetry_selection
    return placement, comps, nets, index, metadata, routed_result
