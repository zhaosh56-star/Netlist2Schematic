"""Converge a proven pair's shared controls, freezing all conduction points.

Only a two-terminal common G/B net can be replaced by a direct line. Existing
foreign routes stay frozen; complete geometry and connectivity validation is
authoritative. There is no SA, A* or inferred pair from shared bias alone.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from math import isfinite
from time import perf_counter

from netlist2schematic.plugins.region.region_controls import _face_control
from netlist2schematic.plugins.region.region_layout import measure_regions
from netlist2schematic.plugins.symmetry.symmetry_layout import _static_safe
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.routing_rules import EPS, RoutingOptions
from netlist2schematic.routing.sa_routing import validate_final_routes, RoutingValidationError

VERSION = 'region-paired-control-routes-v1'
ACTIVE = frozenset(('nmos', 'pmos', 'npn', 'pnp'))


def paired_control_relations(comps, nets, index, plan):
    """A leaf pair is required; same model, supply or bias is insufficient."""
    from netlist2schematic.layout.structured_placement import par
    found, skipped = {}, []
    rails = set(plan.get('power_interfaces', ()))
    for region in plan.get('regions', ()):
        if region.get('children'):
            continue
        pairs=list(region.get('pairs', ()))
        pairs.extend(r['members'] for r in region.get('shared_control_pairs', ())
                     if r.get('role')=='common_reference' and r.get('members'))
        for pair in pairs:
            if len(pair) != 2 or len(set(pair)) != 2 or any(n not in index for n in pair):
                continue
            a, b = (comps[index[name]] for name in pair)
            if a['symbol'] not in ACTIVE or a['symbol'] != b['symbol']:
                continue
            pin = 'G' if 'G' in a['nodes'] else 'B'
            net = a['nodes'].get(pin)
            if net != b['nodes'].get(pin):
                continue
            if net in rails or par.power_marker_sym(net):
                skipped.append(dict(members=list(pair), net=net, reason='supply_control'))
                continue
            if set(map(tuple, nets.get(net, ()))) != {(pair[0], pin), (pair[1], pin)}:
                skipped.append(dict(members=list(pair), net=net, reason='extra_control_net_terminals'))
                continue
            key = tuple(sorted(pair)), net
            found.setdefault(key, dict(members=list(key[0]), pin=pin, net=net,
                evidence=('complete_common_reference_role_and_exact_two_control_terminals'
                    if any(set(pair)==set(r.get('members',())) for r in region.get('shared_control_pairs',()))
                    else 'explicit_leaf_pair_and_exact_two_control_terminals')))
    return dict(version=VERSION, relations=list(found.values()), skipped=skipped)


def measure_paired_controls(pl, index, model):
    records, missing = [], []
    vectors = {'west': (-1, 0), 'east': (1, 0), 'north': (0, -1), 'south': (0, 1)}
    for relation in model['relations']:
        try:
            a, b = (index[n] for n in relation['members'])
            pin = relation['pin']
            first, second = pl.pin_world(a, pin), pl.pin_world(b, pin)
            points = first, second
            errors = sum(sum((points[1-k][axis]-points[k][axis])*
                         vectors[pl.pin_dir(i, pin)][axis] for axis in (0, 1)) <= EPS
                         for k, i in enumerate((a, b)))
            records.append(dict(net=relation['net'], members=relation['members'],
                facing_errors=errors, same_height=abs(first[1]-second[1]) <= EPS,
                control_span=sum(abs(first[k]-second[k]) for k in (0, 1))))
        except (KeyError, ValueError, IndexError, TypeError, AttributeError) as exc:
            missing.append(dict(net=relation['net'], reason=str(exc)))
    usable = bool(records) and not missing
    return dict(version=VERSION, relation_count=len(model['relations']), measurable=usable,
                facing_errors=sum(r['facing_errors'] for r in records) if usable else None,
                records=records, missing=missing)


def measure_saved_paired_controls(pl, comps, index, plan):
    nets = defaultdict(list)
    for comp in comps:
        for pin, net in comp.get('nodes', {}).items():
            nets[net].append((comp['id'], pin))
    result = measure_paired_controls(pl, index, paired_control_relations(comps, nets, index, plan))
    result['plan_signature'] = plan.get('signature')
    return result


def regularize_control_routes(par, pl, comps, nets, index, routed_result, plan, *,
                              options=None, deadline=None, seconds=1.0, max_candidates=12,
                              prevalidated=False, protected_ids=()):
    """Keep the last verified result; only immediate final-route users may trust it.

    Saved-layout callers must leave ``prevalidated`` false. The existing route
    compactor and this pass share one postroute window in the main flow.
    """
    if type(seconds) not in (int, float) or not isfinite(seconds) or seconds < 0:
        raise ValueError('paired-control seconds must be finite and nonnegative')
    if type(max_candidates) is not int or max_candidates < 0:
        raise ValueError('paired-control candidate limit must be a nonnegative integer')
    started = perf_counter()
    until = started+seconds
    if deadline is not None:
        until = min(until, deadline)
    options = options or RoutingOptions()
    routes, metrics, validation = deepcopy(routed_result)
    historical = deepcopy(validation)
    if not prevalidated:
        routes, metrics, validation = validate_final_routes(par, nets, pl, index, routes,
                                                           options=options, deadline=deadline)
    elif not validation.get('valid'):
        raise ValueError('paired-control pass requires validated original routes')
    model = paired_control_relations(comps, nets, index, plan or {})
    report = dict(version=VERSION, applied=False, candidates=0, actions=[], skipped=list(model['skipped']),
        before=deepcopy(metrics), after=deepcopy(metrics), model=model,
        seconds_limit=seconds, max_candidates=max_candidates,
        controls_before=measure_paired_controls(pl, index, model),
        stop_reason='completed_pair_pass', sa_called=False, astar_called=False,
        stage='after_region_compaction_before_quality', conductor_points_frozen=True)
    current = pl
    if not plan or not plan.get('supported') or not model['relations']:
        report['stop_reason'] = 'no_supported_control_pair'
    elif getattr(pl, 'routing_stage', '') == 'bus_fallback':
        report['stop_reason'] = 'constructive_bus_unchanged'
    else:
        fixed = {index.get(n, n) for n in set(getattr(pl, 'fixed', ())) |
                 set(getattr(pl, 'fixed_cells', {})) | set(protected_ids)}
        ordered = sorted(model['relations'], key=lambda r: tuple(sorted(
            (current.pin_world(index[n], r['pin'])) for n in r['members'])))
        for relation in ordered:
            if perf_counter() >= until or report['candidates'] >= max_candidates:
                report['stop_reason'] = 'time_budget' if perf_counter() >= until else 'candidate_limit'
                break
            ids = [index[n] for n in relation['members']]
            if set(ids) & fixed:
                report['skipped'].append(dict(net=relation['net'], reason='fixed_or_role_protected'))
                continue
            if current.cell_of[ids[0]][0] != current.cell_of[ids[1]][0]:
                report['skipped'].append(dict(net=relation['net'], reason='different_display_rows'))
                continue
            report['candidates'] += 1
            trial = deepcopy(current)
            trial.offset_x = list(getattr(trial, 'offset_x', [0.0]*len(comps)))
            conductor_pin = 'S' if comps[ids[0]]['symbol'] in ('nmos', 'pmos') else 'E'
            ids.sort(key=lambda i: current.pin_world(i, conductor_pin)[0])
            if not all(_face_control(trial, i, direction, options.grid_step)
                       for i, direction in zip(ids, ('east', 'west'))):
                report['skipped'].append(dict(net=relation['net'], reason='cannot_preserve_both_conduction_points'))
                continue
            points = [tuple(trial.pin_world(i, relation['pin'])) for i in ids]
            if abs(points[0][1]-points[1][1]) > EPS or points[1][0]-points[0][0] <= EPS:
                report['skipped'].append(dict(net=relation['net'], reason='no_horizontal_control_gap'))
                continue
            if hasattr(trial, 'valid_state') and not trial.valid_state():
                report['skipped'].append(dict(net=relation['net'], reason='state_constraint'))
                continue
            # Shared-control cleanup changes only this net. Every existing
            # conduction route and unrelated signal stays geometrically fixed.
            proposal = deepcopy(dict(routes))
            proposal[relation['net']] = [(points[0], points[1])]
            try:
                if not _static_safe(par, trial, nets, index, options, until):
                    report['skipped'].append(dict(net=relation['net'], reason='static_geometry'))
                    continue
                new_routes, new_metrics, new_validation = validate_final_routes(
                    par, nets, trial, index, list(proposal.items()), options=options, deadline=until)
            except RoutingValidationError:
                report['skipped'].append(dict(net=relation['net'], reason='complete_geometry'))
                continue
            except RoutingFailure:
                report['stop_reason'] = 'time_budget';break
            before_org, after_org = (measure_regions(p, comps, index, plan) for p in (current, trial))
            fields = ('interleaving', 'order_errors', 'pin_axis_error', 'junction_error')
            if (not after_org.get('measurable') or not before_org.get('measurable')
                    or any(after_org[k] > before_org[k]+EPS for k in fields)):
                report['skipped'].append(dict(net=relation['net'], reason='physical_region_regression'))
                continue
            if (any(new_metrics[k] > metrics[k]+EPS for k in ('wirelength', 'crossings', 'bends'))
                    or not any(new_metrics[k] < metrics[k]-EPS for k in ('wirelength', 'bends'))):
                report['skipped'].append(dict(net=relation['net'], reason='no_safe_cost_improvement'))
                continue
            changed = [dict(device=comps[i]['id'], orient_before=current.orient[i], orient_after=trial.orient[i],
                dx_before=getattr(current, 'offset_x', [0.0]*len(comps))[i], dx_after=trial.offset_x[i])
                for i in ids if (current.pos_of(i), current.orient[i]) != (trial.pos_of(i), trial.orient[i])]
            report['actions'].append(dict(net=relation['net'], members=relation['members'],
                pose_changes=changed, metrics_before=deepcopy(metrics), metrics_after=deepcopy(new_metrics),
                origin_axis_drift_before=before_org['axis_drift'], origin_axis_drift_after=after_org['axis_drift'],
                actual_conduction_points_unchanged=True))
            current, routes, metrics, validation = trial, new_routes, new_metrics, new_validation
            validation = {**historical, **validation}
            if 'search' in historical:
                validation['search_predates_paired_control_ports'] = True
    report.update(applied=bool(report['actions']), after=deepcopy(metrics),
        controls_after=measure_paired_controls(current, index, model), elapsed_seconds=perf_counter()-started)
    return current, (routes, metrics, validation), report
