"""Measure one attachment manifest on every candidate; keep routing Q unchanged."""
from copy import copy, deepcopy
from math import isfinite


VERSION = 'attachment-selection-v13'


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
    from netlist2schematic.plugins.attachment.attachment_taps import compile_taps,measure_taps
    report['conduction_tap_access']=measure_taps(measured_placement,comps,index,
        plan.get('conduction_taps') or compile_taps(comps,plan))
    from netlist2schematic.plugins.attachment.attachment_channels import compile_channels, measure_channels
    report['parallel_channel_access'] = measure_channels(measured_placement, comps, index,
        plan.get('parallel_channels') or compile_channels(comps, plan))
    from netlist2schematic.plugins.attachment.attachment_control_nodes import compile_nodes,measure_nodes
    report['shared_control_access']=measure_nodes(measured_placement,comps,index,
        plan.get('control_nodes') or compile_nodes(comps,plan),plan['readability_plan']['junctions'])
    from netlist2schematic.plugins.attachment.attachment_cores import measure_cores
    report['active_core_organization'] = measure_cores(measured_placement, comps, index,
                                                     plan.get('core_rows', {}))
    from netlist2schematic.plugins.attachment.attachment_input_blocks import measure_input_blocks
    report['input_block_organization'] = measure_input_blocks(measured_placement, comps, index,
                                                             plan.get('input_blocks', {}))
    from netlist2schematic.plugins.attachment.attachment_outputs import measure_outputs
    report['output_merge_organization'] = measure_outputs(measured_placement,comps,index,
        plan.get('output_merges',{}),plan.get('input_blocks',{}))
    from netlist2schematic.plugins.attachment.attachment_folded import measure_folded
    report['folded_organization']=measure_folded(measured_placement,comps,index,plan.get('folded_cores',{}))
    from netlist2schematic.plugins.attachment.attachment_controllers import measure_controllers
    report['controller_organization']=measure_controllers(measured_placement,comps,index,
        plan.get('controller_clusters',{}))
    from netlist2schematic.plugins.attachment.attachment_tail_transistors import measure_tails
    report['tail_transistor_organization']=measure_tails(measured_placement,comps,index,
        plan.get('tail_transistors',{}))
    from netlist2schematic.plugins.attachment.attachment_port_banks import measure_banks
    report['host_port_bank_organization']=measure_banks(measured_placement,comps,index,
        plan.get('port_banks',{}))
    from netlist2schematic.plugins.attachment.attachment_shunt_banks import measure_banks as measure_shunts
    report['physical_shunt_organization']=measure_shunts(measured_placement,comps,index,
        plan.get('shunt_banks',{}))
    from netlist2schematic.plugins.attachment.attachment_series_paths import measure_paths
    report['physical_path_organization']=measure_paths(measured_placement,comps,index,
        plan.get('physical_series_paths',{}))
    from netlist2schematic.plugins.attachment.attachment_path_banks import measure_banks as measure_path_banks
    report['physical_path_bank_organization']=measure_path_banks(measured_placement,comps,index,
        plan.get('physical_path_banks',{}))
    from netlist2schematic.plugins.attachment.attachment_regulation import measure_regions
    report['regulation_region_organization']=measure_regions(measured_placement,comps,index,
        plan.get('regulation_regions',{}))
    from netlist2schematic.plugins.attachment.attachment_stages import measure_stages
    report['conduction_stage_organization']=measure_stages(measured_placement,comps,index,
        plan.get('conduction_stages',{}))
    from netlist2schematic.plugins.attachment.attachment_stacks import measure_stacks
    report['repeated_stack_organization']=measure_stacks(measured_placement,comps,index,
        plan.get('repeated_stacks',{}))
    from netlist2schematic.plugins.attachment.attachment_stack_columns import measure_columns
    report['stack_column_organization']=measure_columns(measured_placement,comps,index,
        plan.get('stack_columns',{}))
    from netlist2schematic.plugins.attachment.attachment_control_shunts import measure_shunts
    report['control_rail_shunt_organization']=measure_shunts(measured_placement,comps,index,
        plan.get('control_rail_shunts',{}))
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
    from netlist2schematic.plugins.attachment.attachment_access import access_terms
    access = access_terms(metrics)
    if access is None:
        return None
    taps=report.get('conduction_tap_access',{})
    tap_rank=(0,0)
    if taps.get('count'):
        if not taps.get('measurable'):return None
        tap_rank=(taps['errors'],round(taps['outside_distance'],8))
    from netlist2schematic.plugins.attachment.attachment_channels import channel_rank
    parallel_rank = channel_rank(report.get('parallel_channel_access', {}))
    if parallel_rank is None:
        return None
    from netlist2schematic.plugins.attachment.attachment_control_nodes import node_rank
    control_rank=node_rank(report.get('shared_control_access',{}))
    if control_rank is None:return None
    path_terms=()
    expected=report.get('plan',{}).get('physical_series_paths',{}).get('paths',())
    if expected:
        from netlist2schematic.plugins.attachment.attachment_series_paths import path_rank
        measured=report.get('physical_path_organization',{})
        if measured.get('count')!=len(expected):return None
        path_terms=path_rank(measured)
        if path_terms is None:return None
    bank_terms=()
    expected_banks=report.get('plan',{}).get('physical_path_banks',{}).get('banks',())
    if expected_banks:
        from netlist2schematic.plugins.attachment.attachment_path_banks import bank_rank
        measured=report.get('physical_path_bank_organization',{})
        if measured.get('count')!=len(expected_banks):return None
        bank_terms=bank_rank(measured)
        if bank_terms is None:return None
    stage_terms=()
    stage_model=report.get('plan',{}).get('conduction_stages',{})
    if stage_model.get('global_layout_eligible'):
        from netlist2schematic.plugins.attachment.attachment_stages import stage_rank
        measured=report.get('conduction_stage_organization',{})
        if measured.get('count')!=len(stage_model['stages']):return None
        stage_terms=stage_rank(measured)
        if stage_terms is None:return None
    repeated_terms=()
    expected=report.get('plan',{}).get('repeated_stacks',{}).get('applicable_ids',())
    if expected:
        from netlist2schematic.plugins.attachment.attachment_stacks import stack_rank
        measured=report.get('repeated_stack_organization',{})
        if measured.get('count')!=len(expected):return None
        repeated_terms=stack_rank(measured)
        if repeated_terms is None:return None
    from netlist2schematic.plugins.attachment.attachment_stack_columns import column_rank
    column_measurement=report.get('stack_column_organization',{})
    if column_measurement.get('count',0) != len(report.get('plan',{}).get('stack_columns',{}).get('banks',())):
        return None
    column_terms=column_rank(column_measurement)
    if column_terms is None:return None
    from netlist2schematic.plugins.attachment.attachment_control_shunts import shunt_rank
    shunt_measurement=report.get('control_rail_shunt_organization',{})
    if shunt_measurement.get('count',0)!=len(report.get('plan',{}).get('control_rail_shunts',{}).get('groups',())):
        return None
    shunt_terms=shunt_rank(shunt_measurement)
    if shunt_terms is None:return None
    return repeated_terms+column_terms+shunt_terms+stage_terms+(round(metrics['active_order_errors'], 8), round(metrics['violations']+access[0], 8),
            -metrics['embedded_members'], round(metrics['shape_error'] + metrics['pin_axis_error']+access[1], 8)) + path_terms + bank_terms + (
            round(metrics['owner_distance'], 8),) + source_rank + tap_rank + parallel_rank + control_rank


def select_attachment_result(results):
    """Use real route limits before comparing organization, never overwrite Q."""
    reference = min(results, key=lambda row: row[0])
    original = reference[5].get('layout_attachments', {})
    signature = original.get('plan_signature')
    reference_rank = organization_rank(original)
    from netlist2schematic.plugins.attachment.attachment_cores import core_rank
    reference_core = core_rank(original.get('active_core_organization', {}))
    from netlist2schematic.plugins.attachment.attachment_input_blocks import input_rank
    reference_input = input_rank(original.get('input_block_organization', {}))
    from netlist2schematic.plugins.attachment.attachment_outputs import output_rank
    reference_output = output_rank(original.get('output_merge_organization',{}))
    from netlist2schematic.plugins.attachment.attachment_folded import folded_rank
    reference_folded=folded_rank(original.get('folded_organization',{}))
    from netlist2schematic.plugins.attachment.attachment_tail_transistors import tail_rank
    reference_tail=tail_rank(original.get('tail_transistor_organization',{}))
    metrics = reference[6][1]
    length_cap = metrics['wirelength'] * 1.15
    cross_cap = metrics['crossings'] + (1 if metrics['crossings'] else 0)
    selected = reference
    considered = []
    for row in results:
        report = row[5].get('layout_attachments', {})
        rank = organization_rank(report)
        core = core_rank(report.get('active_core_organization', {}))
        inputs = input_rank(report.get('input_block_organization', {}))
        outputs = output_rank(report.get('output_merge_organization',{}))
        folded=folded_rank(report.get('folded_organization',{}))
        tail=tail_rank(report.get('tail_transistor_organization',{}))
        routes = row[6][1]
        reason = None
        if not signature or signature != report.get('plan_signature'):
            reason = 'different_or_missing_evidence'
        elif (reference_rank is None or rank is None or reference_core is None or core is None
              or reference_input is None or inputs is None or reference_output is None or outputs is None
              or reference_folded is None or folded is None or reference_tail is None or tail is None):
            reason = 'unmeasurable_relationships'
        elif any(report.get('active_core_organization', {}).get(k, 0)
                 != original.get('active_core_organization', {}).get(k, 0)
                 for k in ('count', 'applicable')):
            reason = 'different_or_missing_core_evidence'
        elif any(report.get('input_block_organization', {}).get(k, 0)
                 != original.get('input_block_organization', {}).get(k, 0)
                 for k in ('count', 'bank_count')):
            reason = 'different_or_missing_input_evidence'
        elif report.get('output_merge_organization',{}).get('count',0) != original.get('output_merge_organization',{}).get('count',0):
            reason = 'different_or_missing_output_evidence'
        elif report.get('folded_organization',{}).get('count',0) != original.get('folded_organization',{}).get('count',0):
            reason = 'different_or_missing_folded_evidence'
        elif report.get('tail_transistor_organization',{}).get('count',0) != original.get('tail_transistor_organization',{}).get('count',0):
            reason = 'different_or_missing_tail_evidence'
        elif report['metrics'].get('source_count', 0) != original['metrics'].get('source_count', 0):
            reason = 'different_or_missing_source_evidence'
        elif routes['wirelength'] > length_cap + 1e-8:
            reason = 'wirelength_limit'
        elif routes['crossings'] > cross_cap:
            reason = 'crossing_limit'
        elif (report['metrics'].get('source_count', 0) > 0
              and report['metrics']['source_errors'] > original['metrics']['source_errors']):
            reason = 'source_organization_regression'
        elif (folded, inputs, outputs, core, tail, rank) >= (reference_folded, reference_input, reference_output, reference_core, reference_tail, reference_rank):
            reason = 'no_organization_gain'
        else:
            incumbent_rank = organization_rank(selected[5].get('layout_attachments', {}))
            incumbent_core = core_rank(selected[5].get('layout_attachments', {}).get('active_core_organization', {}))
            incumbent_input = input_rank(selected[5].get('layout_attachments', {}).get('input_block_organization', {}))
            incumbent_output = output_rank(selected[5].get('layout_attachments',{}).get('output_merge_organization',{}))
            incumbent_folded=folded_rank(selected[5].get('layout_attachments',{}).get('folded_organization',{}))
            incumbent_tail=tail_rank(selected[5].get('layout_attachments',{}).get('tail_transistor_organization',{}))
            if selected is reference or (folded, inputs, outputs, core, tail, rank, row[0]) < (incumbent_folded, incumbent_input, incumbent_output, incumbent_core, incumbent_tail, incumbent_rank, selected[0]):
                selected = row
        considered.append({'candidate_id': row[8], 'rank': list(rank) if rank is not None else None,
                           'core_rank': list(core) if core is not None else None,
                           'input_rank': list(inputs) if inputs is not None else None,
                           'output_rank': list(outputs) if outputs is not None else None,
                           'folded_rank': list(folded) if folded is not None else None,
                           'tail_rank': list(tail) if tail is not None else None,
                           'eligible': reason is None, 'reason': reason})
    return selected, {
        'policy': VERSION, 'reference_candidate': reference[8], 'selected_candidate': selected[8],
        'wirelength_limit': length_cap, 'crossing_limit': cross_cap,
        'reference_metrics': deepcopy(metrics), 'measurement_stage': 'before_route_simplification',
        'reason': 'verified_organization_gain' if selected is not reference else 'original_quality_winner',
        'candidates': considered,
    }
