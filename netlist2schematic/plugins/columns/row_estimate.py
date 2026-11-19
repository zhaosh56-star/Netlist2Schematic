"""Bounded pre-SA row trials use the existing placement objective, never A*.

Each trial gets the same independently seeded initializer. Module projection
reuses the real RLC/V/I templates and reservation rules; it is an estimate,
not a replacement for the later static checks, packing or final routing.
"""
from copy import deepcopy
from time import perf_counter


class RowEstimateUnavailable(Exception):
    """No complete estimate was obtained within the local trial budget."""


def make_row_estimator(devices, comps, nets, index, levels, att, structures, seed, variant, *,
                       passive_layout='fixed', modules=None, options=None):
    """Build a lazy evaluator; merely creating it consumes no random draws.

    modules, when present, contains the original full components/nets/index
    and compiled two-terminal plan, while the main arguments describe the
    active core. Guest bookkeeping rows are excluded from crossing estimates.
    """
    from netlist2schematic.layout import structured_placement as sp
    from netlist2schematic.routing.routing_rules import RoutingOptions

    options = options or RoutingOptions()

    def estimate(rows, *, deadline=None):
        def check():
            if deadline is not None and perf_counter() >= deadline:
                raise RowEstimateUnavailable('local_time_budget')

        check()
        core, alignment, pairs = sp._initial_placement(
            devices, comps, nets, index, levels, att, structures, seed, variant,
            passive_layout=passive_layout, row_override=[rows[c['id']] for c in comps])
        check()
        value, core_metrics = sp._estimate(core, nets, index, alignment, pairs)
        metrics = deepcopy(core_metrics)
        projection = dict(policy='none', groups=0, guest_count=0)
        if modules is not None:
            from netlist2schematic.plugins.passive_skeleton.placement import make_frame, reserve, slot_trials
            full_comps, full_nets, full_index, plan = (
                modules[key] for key in ('comps', 'nets', 'index', 'plan'))
            frame = make_frame(sp.par, core, full_comps, full_index, options)
            fixed = {i for i,c in enumerate(full_comps)
                     if i in frame.fixed or c['id'] in frame.fixed or i in frame.fixed_cells}
            groups, expansions = reserve(sp.par, frame, full_comps, full_index, plan, options, fixed)
            check()
            if groups is None:
                raise RowEstimateUnavailable('no_module_reservation_estimate')
            for group in groups:
                check()
                frame = next(slot_trials(sp.par, frame, full_comps, full_index, group, options), None)
                if frame is None:
                    raise RowEstimateUnavailable('no_module_projection')
            remap = {i:full_index[c['id']] for i,c in enumerate(comps)}
            _, full = sp._estimate(frame, full_nets, full_index,
                [(remap[a],remap[b],w) for a,b,w in alignment],
                [(remap[a],remap[b],kind) for a,b,kind in pairs])
            # Retain exactly the core's SA structural and crossing costs.
            # Guest cells only anchor serialization: their parked row numbers
            # must never create fictitious crossing/role-order penalties.
            additions = (full['estimated_wirelength']-core_metrics['estimated_wirelength']
                + full['gate_backtracking']-core_metrics['gate_backtracking']
                + 15*(full['estimated_bends']-core_metrics['estimated_bends'])
                + 8*(full['estimated_congestion']-core_metrics['estimated_congestion']))/100
            value += additions
            for key in ('estimated_wirelength', 'gate_backtracking', 'estimated_bends', 'estimated_congestion'):
                metrics[key] = full[key]
            projection = dict(policy='real_module_templates_and_reservations', groups=len(groups),
                guest_count=plan['guest_count'], guests=list(plan['guest_ids']),
                expansions=expansions, extra_energy=additions,
                initial_positions={n:list(frame.pos_of(full_index[n])) for n in plan['guest_ids']},
                legality='deferred_to_original_packing_and_routing',
                crossing_estimate='active_core_only')
        check()
        metrics.update(normalized_energy=value, module_projection=projection,
            initial_placement={c['id']:dict(cell=list(core.cell_of[i]), orientation=core.orient[i],
                                          point=list(core.pos_of(i))) for i,c in enumerate(comps)},
            objective='existing_structured_SA_estimate', sa_iterations=0, astar_calls=0)
        return value, metrics

    return estimate
