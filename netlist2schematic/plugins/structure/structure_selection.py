"""Bounded structure-first selection; the existing wire-quality score is intact."""
from __future__ import annotations

from copy import deepcopy

VERSION = 'structure-selection-v2'
LENGTH_RATIO = 1.15


def structure_rank(metrics):
    """Use counts first, then normalized geometric dispersion."""
    return (metrics.get('interleaving', 0) + metrics.get('role_order', 0),
            metrics.get('branch_error', 0), metrics.get('input_access_errors', 0),
            metrics.get('parallel_group_error', 0),
            metrics.get('attachment_score', 0),
            metrics.get('normalized_span', metrics.get('span', 0)))


def prepare_branch_context(source, cell, pl, comps, nets, index, metadata):
    """Read-only evidence for both the original reference and new placements."""
    from netlist2schematic.rendering.presentation import prepare_presentation
    from netlist2schematic.plugins.layout_shared.branch_levels import plan_branch_rows
    from netlist2schematic.recognition.device_leveling import compute_levels
    from netlist2schematic.netlist.netlist_core import parse_netlist
    metadata=deepcopy(metadata)
    devices=parse_netlist(source,cell=cell)
    if 'layout_evidence' not in metadata:
        from netlist2schematic.plugins.layout_shared.layout_intent import build_intent
        from netlist2schematic.recognition.circuit_structures import analyze_structures
        phase = metadata.get('requested_structure_phase', metadata.get('structure_layout', {}).get('phase', 3))
        evidence, relations = build_intent(source, cell, devices,
            metadata.get('structs') or analyze_structures(devices), comps, nets, index, phase)
        metadata['layout_evidence'], metadata['layout_relations'] = evidence, relations
    ports=prepare_presentation(source,cell,comps,nets)
    metadata['external_control_ports']=sorted(p['net'] for p in ports['named_ports']
        if p['connected'] and p['role']=='control')
    if not metadata.get('branch_row_plan'):
        _,virtual=plan_branch_rows(devices,compute_levels(devices),
            [r for r,c in pl.cell_of],metadata.get('layout_relations',()),comps,index)
        metadata['branch_measurement_connections']=virtual.get('bridge_alignment',[])
    from netlist2schematic.plugins.layout_shared.attachment_placement import build_attachment_model
    attachment_model=build_attachment_model(devices,comps,nets,index,
                                           metadata.get('layout_relations',()))
    metadata['attachment_measurement_groups']=attachment_model['groups']
    return metadata


def measure_structure(par,pl,comps,nets,index,metadata,phase):
    from netlist2schematic.plugins.layout_shared.layout_relations import measure_layout_relations
    metrics=measure_layout_relations(pl,comps,index,metadata.get('layout_relations',()))
    if phase>=3:
        from netlist2schematic.plugins.layout_shared.branch_alignment import measure_branch_layout
        branch=measure_branch_layout(par,pl,comps,nets,index,metadata)
        from netlist2schematic.plugins.layout_shared.attachment_placement import measure_parallel_coherence
        parallel=measure_parallel_coherence(pl,index,metadata.get('attachment_measurement_groups',()))
        metrics.update(branch_error=branch['hard_error'],attachment_score=branch['attachment_score'],
                       input_access_errors=branch['input_access_errors'],
                       parallel_group_error=parallel['coherence'],parallel_groups=parallel,
                       branch_geometry=branch)
    return metrics


def select_structured_result(results):
    reference = min(results, key=lambda item: item[0])
    metrics = reference[6][1]
    limit = metrics['wirelength'] * LENGTH_RATIO
    crossing_limit = 0 if metrics['crossings'] == 0 else metrics['crossings'] + 1
    eligible = [item for item in results if item[6][1]['wirelength'] <= limit + 1e-9
                and item[6][1]['crossings'] <= crossing_limit]

    def measured(item):
        return item[5].get('structure_layout', {}).get('metrics', {})

    original = structure_rank(measured(reference))
    improved = [item for item in eligible if all(measured(item).get(field, 0) <=
                measured(reference).get(field, 0) for field in ('interleaving', 'role_order'))
                and structure_rank(measured(item)) < original]
    winner = min(improved, key=lambda item: (*structure_rank(measured(item)), *item[0])) if improved else reference
    return winner, {
        'policy': VERSION, 'reference_candidate': reference[8],
        'reference_metrics': deepcopy(metrics), 'reference_structure': deepcopy(measured(reference)),
        'reference_stage': 'before_route_simplification',
        'wirelength_ratio': LENGTH_RATIO, 'wirelength_limit': limit,
        'crossing_limit': crossing_limit, 'selected_candidate': winner[8],
        'eligible_candidates': [item[8] for item in eligible],
        'reason': 'structure_improved_within_wire_budget' if improved else 'original_quality_no_structure_gain',
        'ranking': ['structure_violations', 'branch_pin_error', 'input_access_errors',
                    'parallel_group_error', 'attachment_score',
                    'normalized_structure_span', 'original_quality'],
    }
