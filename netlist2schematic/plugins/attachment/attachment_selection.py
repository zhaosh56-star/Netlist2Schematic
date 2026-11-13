"""Measure one attachment manifest on every candidate; keep routing Q unchanged."""
from copy import copy, deepcopy
from math import isfinite


VERSION = 'attachment-selection-v2'


def _measurement_view(placement, comps, index, report, plan):
    """Restore display-slot evidence without restoring an annealing policy.

    Rerouting and spacing retries retain logical rows. A bus fallback can
    rearrange them, so an old slot must not count as a new interior placement.
    """
    live = getattr(placement, 'attachment_junction_rows', None)
    saved = report.get('placement_slot_contract', {})
    groups = {g['id']: g for g in plan.get('groups', ())}
    previous_plan = report.get('plan', {})
    previous_groups = {g['id']: g for g in previous_plan.get('groups', ())}
    current_rows = {c['id']: placement.cell_of[i][0] for i, c in enumerate(comps)}
    restored = {}
    records = {}
    for identifier, group in groups.items():
        names = sorted(set(group.get('members', ())) | set(group.get('host_members', ())))
        rows = {name: current_rows[name] for name in names if name in index}
        previous = saved.get('groups', {}).get(identifier, {})
        same_evidence = (saved.get('plan_signature') == plan['signature'] or (
            saved.get('plan_signature') == previous_plan.get('signature')
            and previous_groups.get(identifier) == group))
        if live is not None:
            slots = live.get(identifier, ())
        elif same_evidence and previous.get('device_rows') == rows:
            slots = previous.get('rows', ())
        else:
            slots = ()
        slots = sorted(set(r for r in slots if type(r) is int and 0 <= r < len(placement.row_y)))
        if slots:
            restored[identifier] = tuple(slots)
            records[identifier] = {'rows': slots, 'device_rows': rows}
    report['placement_slot_contract'] = {
        'version': 1, 'plan_signature': plan['signature'], 'groups': records,
    }
    if live is not None:
        return placement
    view = copy(placement)
    view.attachment_junction_rows = restored
    return view


def measure_candidate(placement, comps, index, metadata, plan, *,
                      stage='final_output', policy='passive'):
    result = deepcopy(metadata)
    report = result.setdefault('layout_attachments', {'policy': policy, 'applied': False})
    report.update(policy=policy, measurement_stage=stage,
                  reference_candidate=str(metadata.get('candidate_id')) == '1',
                  routing_stage=metadata.get('routing_stage', 'normal'))
    # The saved plan describes source-derived row order. Inserted/released
    # display rows are measured independently, including after recovery.
    report['actual_display_rows'] = {comp['id']: placement.cell_of[i][0]
                                     for i, comp in enumerate(comps)}
    if plan is None:
        report['metrics'] = {'supported': False, 'measurable': False,
                             'reason': 'no_saved_attachment_plan'}
        return result
    from netlist2schematic.plugins.attachment.attachment_integration import measure_attachments
    measured_placement = _measurement_view(placement, comps, index, report, plan)
    report.update(plan=deepcopy(plan), plan_signature=plan['signature'],
                  metrics=measure_attachments(measured_placement, comps, index, plan))
    return result


def organization_rank(report):
    metrics = report.get('metrics', {})
    keys = ('violations', 'active_order_errors', 'embedded_members',
            'shape_error', 'pin_axis_error', 'owner_distance')
    if not metrics.get('supported') or not metrics.get('measurable', False):
        return None
    if any(type(metrics.get(key)) not in (int, float) or not isfinite(metrics[key]) for key in keys):
        return None
    count = metrics.get('source_count', 0 if report.get('policy') != 'all' else None)
    if type(count) is not int or count < 0:
        return None
    source_rank = (0, 0)
    if count:
        if not metrics.get('source_measurable'):
            return None
        source_keys = ('source_errors', 'source_distance')
        if any(type(metrics.get(key)) not in (int, float) or not isfinite(metrics[key])
               or metrics[key] < 0 for key in source_keys):
            return None
        source_rank = tuple(round(metrics[key], 8) for key in source_keys)
    return (round(metrics['active_order_errors'], 8), round(metrics['violations'], 8),
            -metrics['embedded_members'], round(metrics['shape_error'] + metrics['pin_axis_error'], 8),
            round(metrics['owner_distance'], 8)) + source_rank


def select_attachment_result(results):
    """Use real route limits before comparing organization, never overwrite Q."""
    reference = min(results, key=lambda row: row[0])
    original = reference[5].get('layout_attachments', {})
    signature = original.get('plan_signature')
    reference_rank = organization_rank(original)
    metrics = reference[6][1]
    length_cap = metrics['wirelength'] * 1.15
    cross_cap = metrics['crossings'] + (1 if metrics['crossings'] else 0)
    selected = reference
    considered = []
    for row in results:
        report = row[5].get('layout_attachments', {})
        rank = organization_rank(report)
        routes = row[6][1]
        reason = None
        if not signature or signature != report.get('plan_signature'):
            reason = 'different_or_missing_evidence'
        elif reference_rank is None or rank is None:
            reason = 'unmeasurable_relationships'
        elif report['metrics'].get('source_count', 0) != original['metrics'].get('source_count', 0):
            reason = 'different_or_missing_source_evidence'
        elif routes['wirelength'] > length_cap + 1e-8:
            reason = 'wirelength_limit'
        elif routes['crossings'] > cross_cap:
            reason = 'crossing_limit'
        elif (report['metrics'].get('source_count', 0) > 0
              and report['metrics']['source_errors'] > original['metrics']['source_errors']):
            reason = 'source_organization_regression'
        elif rank >= reference_rank:
            reason = 'no_organization_gain'
        else:
            incumbent_rank = organization_rank(selected[5].get('layout_attachments', {}))
            if selected is reference or (rank, row[0]) < (incumbent_rank, selected[0]):
                selected = row
        considered.append({'candidate_id': row[8], 'rank': list(rank) if rank is not None else None,
                           'eligible': reason is None, 'reason': reason})
    return selected, {
        'policy': VERSION, 'reference_candidate': reference[8], 'selected_candidate': selected[8],
        'wirelength_limit': length_cap, 'crossing_limit': cross_cap,
        'reference_metrics': deepcopy(metrics), 'measurement_stage': 'before_route_simplification',
        'reason': 'verified_organization_gain' if selected is not reference else 'original_quality_winner',
        'candidates': considered,
    }
