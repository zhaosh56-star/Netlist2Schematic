"""Shared evidence measurement and opt-in layout selection; original Q is retained."""
from copy import deepcopy
from math import isfinite

VERSION = 'readability-selection-v1'


def feedback_compatible(source, target):
    a, b = source.get('layout_readability', {}), target.get('layout_readability', {})
    return bool(a.get('applied') and b.get('applied') and a.get('plan', {}).get('signature')
                and a.get('plan', {}).get('signature') == b.get('plan', {}).get('signature'))


def measure_candidate(par, pl, comps, nets, index, metadata, plan, *, phase=5, stage='final_output'):
    result = deepcopy(metadata)
    report = result.setdefault('layout_readability', {'policy': 'auto', 'applied': False})
    report.update(phase=phase, measurement_stage=stage)
    if plan is None:
        report['metrics'] = {'measurable': False, 'reason': 'no_saved_readability_plan'}
        return result
    from readability_layout import measure_readability
    report['plan'] = deepcopy(plan)
    report['plan_signature'] = plan['signature']
    report['metrics'] = measure_readability(pl, comps, index, plan)
    if phase >= 3:
        from readability_refinement import measure_refinements
        report['refinement_metrics'] = measure_refinements(par, pl, comps, nets, index, result,
                                                          phase=min(phase, 4))
    return result


def _number(value):
    return float(value) if type(value) in (int, float) and isfinite(value) else 0.0


def organization_rank(report):
    metrics = report.get('metrics', {})
    refinement = report.get('refinement_metrics', {})
    coverage = metrics.get('coverage', {})
    passive = bool(metrics.get('passive_supported', coverage.get('pattern_count', 0)))
    source = bool(refinement.get('source_count', 0))
    compact = report.get('phase', 2) >= 4 and refinement.get('measurable', False)
    if passive and (metrics.get('measurable') is False or any(
            type(metrics.get(key)) not in (int, float) or not isfinite(metrics[key])
            for key in ('violations', 'shape_error', 'pin_axis_error'))):
        return None
    if source and (refinement.get('measurable') is False or any(
            type(refinement.get(key)) not in (int, float) or not isfinite(refinement[key])
            for key in ('source_errors', 'source_distance'))):
        return None
    if not (passive or source or compact):
        return None
    return (round(_number(metrics.get('violations')), 8) if passive else 0,
            round(_number(metrics.get('shape_error')) + _number(metrics.get('pin_axis_error')), 8)
            if passive else 0,
            round(_number(metrics.get('owner_locality')), 8) if passive else 0,
            round(_number(refinement.get('source_errors')), 8) if source else 0,
            round(_number(refinement.get('source_distance')), 8) if source else 0,
            round(_number(refinement.get('void')), 8) if compact else 0,
            round(_number(refinement.get('compactness')), 8) if compact else 0)


def select_readable_result(results, *, tradeoff='strict'):
    if tradeoff not in ('strict', 'balanced'):
        raise ValueError('route_tradeoff must be strict or balanced')
    reference = min(results, key=lambda item: item[0])
    baseline = reference[6][1]
    crossing_limit = baseline['crossings'] + (2 if tradeoff == 'balanced' else
                                             1 if baseline['crossings'] else 0)
    length_limit = baseline['wirelength'] * 1.15
    signature = reference[5].get('layout_readability', {}).get('plan_signature')
    original_rank = organization_rank(reference[5].get('layout_readability', {}))
    eligible, improved = [], []
    for row in results:
        metrics, report = row[6][1], row[5].get('layout_readability', {})
        if (metrics['wirelength'] > length_limit + 1e-8 or metrics['crossings'] > crossing_limit
                or report.get('plan_signature') != signature):
            continue
        eligible.append(row)
        rank = organization_rank(report)
        if rank is not None and original_rank is not None and rank < original_rank:
            improved.append((rank, row[0], row))
    chosen = min(improved, key=lambda item: (item[0], item[1]))[2] if improved else reference
    return chosen, {'policy': VERSION, 'reference_candidate': reference[8],
        'selected_candidate': chosen[8], 'reference_stage': 'before_route_simplification',
        'reference_metrics': deepcopy(baseline), 'wirelength_limit': length_limit,
        'crossing_limit': crossing_limit, 'route_tradeoff': tradeoff,
        'eligible_candidates': [row[8] for row in eligible],
        'reason': 'readability_gain_within_routing_limits' if improved else 'original_quality_no_readability_gain',
        'reference_rank': original_rank,
        'selected_rank': organization_rank(chosen[5].get('layout_readability', {})),
        'original_quality_score_unchanged': True}
