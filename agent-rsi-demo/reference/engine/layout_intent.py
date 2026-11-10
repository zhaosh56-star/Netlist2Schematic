"""Build one source-derived evidence table for all candidate measurements."""
from __future__ import annotations


def build_intent(source, cell, devices, baseline_structs, comps, nets, index, phase=3):
    from presentation import prepare_presentation
    from layout_evidence import build_layout_evidence, extend_branch_evidence
    from layout_relations import build_layout_relations
    ports = prepare_presentation(source, cell, comps, nets)
    evidence = build_layout_evidence(devices, baseline_structs,
                                     formal_ports=ports['named_ports'])
    relations = build_layout_relations(devices, validated_evidence=evidence)
    if phase >= 2:
        # Original row preferences depend only on the source, not on which
        # candidate happens to be measured. This includes the off reference.
        from device_leveling import compute_levels
        from branch_levels import plan_branch_rows
        from sa_common import load_local
        legacy = load_local('place_layered', 'place-layered.py')
        levels = compute_levels(devices)
        conduction = {net for dev in devices.values() if dev['cls'] in ('MOS', 'BJT')
                      for net in (dev.get('d'), dev.get('s'), dev.get('c'), dev.get('e'))
                      if net is not None}
        attachments = legacy.attach_info(devices, levels['net_levels'], conduction)
        original_rows, _ = legacy._allocate_rows(devices, comps, index,
            levels['net_levels'], levels['device_levels'], attachments)
        _, row_plan = plan_branch_rows(devices, levels, original_rows, relations, comps, index)
        evidence = extend_branch_evidence(devices, evidence,
                                          bridge_alignment=row_plan.get('bridge_alignment', ()))
        relations = build_layout_relations(devices, validated_evidence=evidence)
    evidence['phase'] = phase
    return evidence, relations
