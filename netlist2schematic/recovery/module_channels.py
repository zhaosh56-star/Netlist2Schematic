"""Failure-only passive-module channel changes on an unchanged active frame.

Only new-layout recovery invokes this module. Saved-layout rerouting never
opts in. Unrelated completed nets stay frozen; all new scenes and routes pass
the original geometry and connectivity validators before publication.
"""
from copy import deepcopy
from time import perf_counter

from netlist2schematic.common.sa_common import load_local
from netlist2schematic.layout.series_alignment import _fixed_ids
from netlist2schematic.plugins.passive_skeleton.placement import slot_trials, _guest_guards
from netlist2schematic.plugins.passive_skeleton.module_connections import check_guides, complete_routes
from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
from netlist2schematic.routing.routing_rules import build_scene, analyze_routes, check_deadline
from netlist2schematic.routing.orthogonal_router import RoutingFailure, _priority
from netlist2schematic.routing.sa_routing import RoutingValidationError, routing_errors

VERSION = 'module-channel-recovery-v1'


def eligible(context, checkpoint):
    if not context or len(context) != 5 or not checkpoint:
        return False
    placement, _, _, _, metadata = context
    return bool(metadata.get('layout_branches', {}).get('applied')
        and metadata.get('passive_skeleton', {}).get('applied')
        and metadata['passive_skeleton'].get('plan', {}).get('groups')
        and getattr(placement, 'passive_module_guides', None)
        and checkpoint.get('failed_net'))


def _checkpoint(par, before, trial, comps, nets, index, metadata, checkpoint, options, deadline):
    """Drop exactly the moved terminals' nets and validate the frozen remainder."""
    scene = build_scene(par, nets, trial, index, options=options, deadline=deadline)
    if getattr(trial, 'branch_routing', None):
        scene.branch_routing = deepcopy(trial.branch_routing)
        scene.branch_routing['active'] = True
        scene.component_names = {i: c['id'] for i, c in enumerate(comps)}
    changed = {i for i in range(len(comps))
               if before.pos_of(i) != trial.pos_of(i) or before.orient[i] != trial.orient[i]}
    affected = {net for i in changed for net in comps[i]['nodes'].values()}
    frozen = {net: deepcopy(segments) for net, segments in checkpoint['completed'].items()
              if net not in affected}
    for net, segments in frozen.items():
        if routing_errors([(net, segments)], {net: nets[net]}, trial, index, deadline=deadline):
            return None
    seeds = complete_routes(par, trial, nets, index, scene, deadline=deadline)
    for net, segments in seeds.items():
        if net in affected or net not in frozen:
            frozen[net] = segments
    occupancy = {net: list(segments) for net, segments in scene.fixed_segments.items()}
    occupancy.update(frozen)
    if analyze_routes(list(occupancy.items()), scene, deadline=deadline)['violations']:
        return None
    pending = sorted(set(scene.pins)-set(frozen), key=lambda net: _priority(scene, net))
    failed = (checkpoint['failed_net'] if checkpoint['failed_net'] in pending else pending[0]) if pending else None
    return dict(scene=scene, completed=frozen, failed_net=failed,
                pending=[net for net in pending if net != failed], branch=None,
                margin=checkpoint['margin'], options=deepcopy(options),
                repositioned_nets=sorted(affected), complete=not pending)


def _complete(context, checkpoint, options, deadline):
    """An ordinary checked guide can itself finish the remaining connections."""
    from dataclasses import asdict
    from netlist2schematic.routing.sa_routing import validate_final_routes
    from netlist2schematic.layout.layout_quality import evaluate_quality
    placement,comps,nets,index,metadata=deepcopy(context)
    par=load_local('place_and_route','place-and-route.py')
    routed=validate_final_routes(par,nets,placement,index,
        sorted(checkpoint['completed'].items()),options=options,deadline=deadline)
    check_deadline(deadline,'module_channel_quality')
    quality=evaluate_quality(par,placement,comps,nets,index,routed[0],routed[1],metadata.get('structs',()))
    check_deadline(deadline,'module_channel_quality')
    metadata['routing_stage']='local_rescue'
    metadata['local_rescue']=dict(version=VERSION,status='valid',completed_by='verified_module_guides')
    metadata['quality']=dict(policy='balanced-v1',selected_candidate=str(metadata.get('candidate_id','unknown')),
                             score=quality['score'],details=quality,candidates=[])
    routed[2].update(routing_stage='local_rescue',parameters=asdict(options))
    return placement,comps,nets,index,metadata,routed


def rescue(context, checkpoint, options, *, deadline, max_trials=16, max_routes=2):
    report = dict(version=VERSION, status='skipped', trials=0, route_attempts=0,
                  changes=[], rejections={}, stop_reason='no_applicable_module')
    if not eligible(context, checkpoint):
        return None, report
    started = perf_counter()
    options = options if options is not None else checkpoint['options']
    par = load_local('place_and_route', 'place-and-route.py')
    before, comps, nets, index, metadata = deepcopy(context)
    plan = metadata['passive_skeleton']['plan']
    groups = plan['groups']
    fixed = _fixed_ids(before, comps)
    branch = checkpoint.get('branch') or {}
    failed = checkpoint['failed_net']
    source = branch.get('component')
    related = [g for g in groups if failed in g['ends']
               or any(failed == net for n in g['members'] for net in comps[index[n]]['nodes'].values())]
    related.sort(key=lambda g: (source not in {index[n] for n in g['members']},
                               -sum(n == failed for n in g['ends']), g['id']))
    seen = set()
    report['status'] = 'failed'
    if related:
        report['stop_reason']='channels_exhausted'
    def rejected(reason):
        report['rejections'][reason]=report['rejections'].get(reason,0)+1
    try:
        for group in related:
            members = {index[name] for name in group['members']}
            if members & fixed:
                continue
            base = deepcopy(before)
            base.passive_module_guides.pop(group['id'], None)
            # Old guest shapes must not suggest spurious obstacle channels.
            far_x = max(base.column_x)+1000
            far_y = max(base.row_y)+1000
            for number, i in enumerate(sorted(members)):
                x, y = base.pos_of(i)
                base.passive_local_offsets[i][0] += far_x+number*220-x
                base.passive_local_offsets[i][1] += far_y-y
            for trial in slot_trials(par, base, comps, index, group, options):
                check_deadline(deadline, 'module_channel_candidates')
                identity = tuple((i, trial.pos_of(i), trial.orient[i]) for i in sorted(members))
                if identity in seen or all(trial.pos_of(i) == before.pos_of(i)
                                          and trial.orient[i] == before.orient[i] for i in members):
                    continue
                seen.add(identity)
                if report['trials'] >= max_trials or report['route_attempts'] >= max_routes:
                    report['stop_reason'] = 'trial_limit'
                    return None, report
                report['trials'] += 1
                if not _guest_guards(par, trial, comps, index, groups):
                    rejected('leaf_guard')
                    continue
                faults = _static_faults(par, trial, nets, index, options, deadline)
                if faults['count'] or faults['hard']:
                    rejected('static_geometry')
                    continue
                if check_guides(par, trial, nets, index, options, deadline):
                    rejected('module_internal_routes')
                    continue
                updated = _checkpoint(par, before, trial, comps, nets, index, metadata,
                                      checkpoint, options, deadline)
                if updated is None:
                    rejected('frozen_routes')
                    continue
                from netlist2schematic.recovery.local_rescue import rescue_routing
                seconds = min(6., (deadline-perf_counter())/(max_routes-report['route_attempts']))
                if seconds <= 0:
                    break
                report['route_attempts'] += 1
                action = dict(group=group['id'], members=list(group['members']),
                    before={comps[i]['id']: list(before.pos_of(i)) for i in members},
                    after={comps[i]['id']: list(trial.pos_of(i)) for i in members},
                    preserved_nets=sorted(net for net in checkpoint['completed']
                        if net not in updated['repositioned_nets']
                        and updated['completed'].get(net) == checkpoint['completed'][net]),
                    rebuilt_nets=sorted((set(updated['scene'].pins)-set(updated['completed']))
                                        | (set(updated['scene'].pins) & set(updated['repositioned_nets']))),
                    status='failed')
                report['changes'].append(action)
                try:
                    if updated['complete']:
                        result=_complete((trial,comps,nets,index,metadata),updated,options,deadline)
                    else:
                        result = rescue_routing((trial, comps, nets, index, metadata), updated,
                                            routing_options=options, seconds=seconds)
                except (RoutingFailure, RoutingValidationError) as exc:
                    action.update(reason=getattr(exc, 'diagnostics', {}).get('reason'), message=str(exc))
                    continue
                report.update(status='valid', stop_reason='first_verified_result',
                              elapsed_seconds=round(perf_counter()-started, 6))
                action['status'] = 'valid'
                current = result[4]
                current['local_rescue'].update(layout_preserved=False, active_layout_preserved=True,
                    fixed_port_geometry_preserved=False, original_fixed_devices_preserved=True,
                    module_channels=deepcopy(report))
                current['passive_skeleton']['module_connections'] = deepcopy(trial.passive_module_guides)
                for old_action in current['passive_skeleton'].get('actions', []):
                    if old_action['group'] == group['id']:
                        old_action['positions'] = {name: dict(point=list(trial.pos_of(index[name])),
                            orientation=trial.orient[index[name]]) for name in group['members']}
                from netlist2schematic.plugins.passive_skeleton.plan import measure
                check_deadline(deadline, 'module_channel_metadata')
                current['passive_skeleton']['current'] = measure(result[0], comps, index, plan)
                check_deadline(deadline, 'module_channel_metadata')
                return result, report
    except (RoutingFailure, RoutingValidationError) as exc:
        report.update(stop_reason=getattr(exc, 'diagnostics', {}).get('reason', 'geometry_rejected'),
                      message=str(exc))
    finally:
        report['elapsed_seconds'] = round(perf_counter()-started, 6)
    return None, report
