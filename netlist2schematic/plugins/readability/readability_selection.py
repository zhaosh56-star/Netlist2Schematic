"""Shared evidence measurement and opt-in layout selection; original Q is retained."""
from copy import deepcopy
from math import isfinite
import hashlib
import json

VERSION = 'readability-selection-v1'


def _geometry_key(pl, comps):
    state = [(comp['id'], list(pl.cell_of[i]), pl.orient[i], list(pl.pos_of(i)))
             for i, comp in enumerate(comps)]
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def reference_seed(par, placement, comps, nets, index, metadata, results, plan, *, phase=3):
    """Reuse verified geometry in a new candidate slot when its intent is compatible.

    A better passive template or changed active display row is authoritative;
    an old candidate can never replace it just because its wire score is lower.
    """
    if not results or plan is None:
        return None
    # Keep candidate three's independent seed. Repeatedly cloning the same
    # successful core consumed both experimental slots and erased diversity.
    if str(metadata.get('candidate_id')) != '2':
        return None
    from netlist2schematic.plugins.readability.readability_layout import measure_readability
    from netlist2schematic.plugins.readability.readability_refinement import measure_refinements
    target = measure_readability(placement, comps, index, plan)
    locality = measure_refinements(par, placement, comps, nets, index, plan, phase=min(phase, 4))
    active_rows = sorted({plan.get('display_rows', {}).get(c['id']) for c in comps
                          if c['symbol'] in ('nmos', 'pmos', 'npn', 'pnp')
                          and type(plan.get('display_rows', {}).get(c['id'])) is int})
    reserved_band = bool(active_rows and len(active_rows) < active_rows[-1]-active_rows[0]+1)
    if not locality.get('source_count', 0) and not (phase >= 4 and reserved_band
                                                  and plan.get('patterns')):
        return None
    if target.get('passive_supported') and target.get('measurable') is False:
        return None
    target_shape = tuple(_number(target.get(k)) for k in ('violations',)) + (
        _number(target.get('shape_error')) + _number(target.get('pin_axis_error')),)
    active = {'nmos', 'pmos', 'npn', 'pnp'}
    fixed = {index[i] if isinstance(i, str) else i for i in
             set(getattr(placement, 'fixed', ())) | set(getattr(placement, 'fixed_cells', {}))}
    display = plan.get('display_rows', {})
    target_local = measure_refinements(par, placement, comps, nets, index, plan,
                                      phase=min(phase, 4)) if phase >= 4 else None
    bridge_members = {name for pattern in plan.get('patterns', ())
                      if pattern.get('row_mode') == 'conduction_bridge'
                      for name in pattern.get('members', ())}
    for row in sorted(results, key=lambda value: value[0]):
        if row[2] != comps or row[3] != nets or row[4] != index:
            continue
        source = row[1]
        if any(comp['id'] not in display or source.cell_of[i][0] != display[comp['id']]
               for i, comp in enumerate(comps)
               if comp['symbol'] in active or comp['id'] in bridge_members):
            continue
        if any(source.pos_of(i) != placement.pos_of(i) or source.orient[i] != placement.orient[i]
               for i in fixed):
            continue
        measured = measure_readability(source, comps, index, plan)
        if target.get('passive_supported'):
            values = [measured.get(k) for k in ('violations', 'shape_error', 'pin_axis_error')]
            if measured.get('measurable') is False or any(type(v) not in (int, float)
                    or not isfinite(v) for v in values):
                continue
            if values[0] > target_shape[0] or values[1]+values[2] > target_shape[1] + 1e-8:
                continue
        local = measure_refinements(par, source, comps, nets, index, plan, phase=min(phase, 4))
        if target_local is not None and target_local.get('bank_outlet_planned', 0):
            target_error, source_error = target_local.get('bank_outlet_error'), local.get('bank_outlet_error')
            if (type(target_error) not in (int, float) or type(source_error) not in (int, float)
                    or not isfinite(target_error) or not isfinite(source_error)
                    or source_error > target_error + 1e-8):
                continue
        if phase == 3 and not local.get('source_count', 0):
            continue
        clone, updated = deepcopy(source), deepcopy(row[5])
        clone.readability_plan, clone.readability_index = deepcopy(plan), dict(index)
        updated['candidate_id'] = metadata['candidate_id']
        updated['layout_readability'] = deepcopy(metadata['layout_readability'])
        updated['layout_readability'].update(applied=True,
            reference_seed={'source_candidate': row[8], 'source_score': row[7]['score'],
                'source_candidate_seed': row[5].get('candidate_seed'),
                'generated_candidate_seed': metadata.get('candidate_seed'),
                'generated_annealing': deepcopy(metadata.get('annealing')),
                'generated_initialization': deepcopy(metadata['layout_readability'].get('initialization')),
                'generated_initializer_escape_risk': deepcopy(metadata['layout_readability'].get('initializer_escape_risk')),
                'source_geometry_sha256': _geometry_key(source, comps),
                'source_readability_applied': bool(row[5].get('layout_readability', {}).get('applied')),
                'source_metrics': measured, 'generated_metrics': target,
                'reason': 'verified_geometry_same_rows_and_no_passive_shape_regression'},
            initialization={'status': 'verified_reference_clone', 'source_candidate': row[8]})
        updated['layout_readability'].pop('initializer_escape_risk', None)
        updated['layout_grid'] = {'column_x': list(clone.column_x), 'row_y': list(clone.row_y)}
        updated['energy_stage'] = 'source_sa_before_readability_refinement'
        if 'branch_row_plan' in metadata:
            updated['branch_row_plan'] = deepcopy(metadata['branch_row_plan'])
        return clone, deepcopy(comps), deepcopy(nets), dict(index), updated
    return None


def feedback_compatible(source, target):
    a, b = source.get('layout_readability', {}), target.get('layout_readability', {})
    def eligible(metadata, report):
        return bool(report.get('applied') or (report.get('policy') == 'auto'
                    and str(metadata.get('candidate_id')) in ('2', '3')))
    return bool(eligible(source, a) and eligible(target, b) and a.get('plan', {}).get('signature')
                and a.get('plan', {}).get('signature') == b.get('plan', {}).get('signature'))


def measure_candidate(par, pl, comps, nets, index, metadata, plan, *, phase=5, stage='final_output'):
    result = deepcopy(metadata)
    report = result.setdefault('layout_readability', {'policy': 'auto', 'applied': False})
    report.update(phase=phase, measurement_stage=stage)
    if report.get('refinement', {}).get('actions') or report.get('routing_feedback', {}).get('action'):
        report['applied'] = True
    if report.get('reference_seed', {}).get('source_geometry_sha256'):
        reference = report['reference_seed']
        reference['duplicate_reference_geometry'] = reference['source_geometry_sha256'] == _geometry_key(pl, comps)
        if reference['duplicate_reference_geometry'] and not reference.get('source_readability_applied'):
            report['applied'] = False
    if plan is None:
        report['metrics'] = {'measurable': False, 'reason': 'no_saved_readability_plan'}
        return result
    from netlist2schematic.plugins.readability.readability_layout import measure_readability
    report['plan'] = deepcopy(plan)
    report['plan_signature'] = plan['signature']
    report['metrics'] = measure_readability(pl, comps, index, plan)
    if phase >= 3:
        from netlist2schematic.plugins.readability.readability_refinement import measure_refinements
        report['refinement_metrics'] = measure_refinements(par, pl, comps, nets, index, result,
                                                          phase=min(phase, 4))
    return result


def _number(value):
    return float(value) if type(value) in (int, float) and isfinite(value) else 0.0


def organization_rank(report):
    metrics = report.get('metrics', {})
    refinement = report.get('refinement_metrics', {})
    coverage = metrics.get('coverage', {})
    from netlist2schematic.plugins.readability.readability_layout import has_strong_passive_plan
    passive = bool(metrics.get('passive_supported', coverage.get('pattern_count', 0))
                   and has_strong_passive_plan(report.get('plan', {})))
    source = bool(refinement.get('source_count', 0))
    compact = (report.get('phase', 2) >= 4 and refinement.get('measurable', False)
               and refinement.get('compaction_applicable', True))
    if compact and refinement.get('bank_outlet_planned', 0) and (
            refinement.get('bank_outlet_measurable') is False
            or type(refinement.get('bank_outlet_error')) not in (int, float)
            or not isfinite(refinement['bank_outlet_error'])):
        return None
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
            round(_number(refinement.get('bank_outlet_error')), 8) if compact else 0,
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
