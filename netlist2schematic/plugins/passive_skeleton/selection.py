"""Compare the same passive relations on every complete legal candidate."""
from netlist2schematic.plugins.passive_skeleton.plan import measure,organization_key


def select_result(results, plan, column_plan=None, *, primary=False):
    reference=min(results,key=lambda r:r[0]);old=reference[6][1]
    cap=old['crossings']+(1 if old['crossings'] else 0)
    eligible=[r for r in results if r[6][1]['wirelength']<=old['wirelength']*1.15+1e-8
              and r[6][1]['crossings']<=cap and (primary or r[6][1]['bends']<=old['bends'])]
    metrics={r[8]:measure(r[1],r[2],r[4],plan) for r in results}
    columns={}
    if column_plan is not None and column_plan.get('supported'):
        from netlist2schematic.plugins.columns.column_layout import measure_columns,column_key
        columns={r[8]:measure_columns(r[1],r[4],column_plan) for r in results}
        # Passive neatness cannot buy a broken main column or reversed sides.
        guard=('column_breaks','order_reversals','side_intrusions')
        eligible=[r for r in eligible if all(columns[r[8]][k]<=columns[reference[8]][k] for k in guard)]
        primary=lambda r:column_key(columns[r[8]])
    else:
        primary=lambda r:()
    rank=lambda r:primary(r)+organization_key(metrics[r[8]])
    winner=min(eligible,key=lambda r:rank(r)+r[0])
    better=rank(winner)<rank(reference)
    selected=winner if better else reference
    return selected,dict(policy='active-first-primary-selection-v1' if primary else plan['version'],
        construction_version=plan['version'],reference_candidate=reference[8],selected_candidate=selected[8],
        reason='typed_attachment_organization' if better else 'original_quality',
        wirelength_limit=old['wirelength']*1.15,crossings_limit=cap,bends_limit=None if primary else old['bends'],
        measurements=metrics,active_columns=columns,active_structure_priority=True,
        bends_policy='soft_quality_cost' if primary else 'nonincrease',
        reference_pool='preferred_candidates_only' if primary else 'all_valid_candidates')
