"""Bounded normal-stage choices around an already legal passive packing.

Keep the complete incumbent throughout. End attachments may translate in two
dimensions; inter-branch bridges stay in their typed corridor. Inline branches,
coupled banks, fixed guests and the active frame are never reorganized here.
No annealing or path search is called by this module.
"""
from copy import deepcopy
from math import isfinite
from statistics import median
from time import perf_counter

from netlist2schematic.routing.routing_rules import EPS, build_scene, check_deadline
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from .terminal_slots import is_terminal, _targets, _channels, _score, _rigid_move

VERSION = 'normal-module-choice-v1'


def _mode(group):
    if is_terminal(group):
        return 'terminal_2d'
    if group['role'] == 'bridge' and all(group['anchors'][n] for n in group['ends']):
        return 'bridge_corridor'
    return None


def _profile(frame, group):
    group = deepcopy(group)
    saved = frame.passive_module_guides[group['id']].get('spacing', {})
    group['spacing_policy'] = saved.get('policy', group.get('spacing_policy', 'compact'))
    group['extra_keepout'] = saved.get('extra_keepout', group.get('extra_keepout', 0))
    return group


def _proposals(par, frame, comps, nets, index, group, options, scene, channels, deadline):
    from .placement import (_anchor_geometry, _geometry, _translated,
                            _approach_paths, _guest_forbidden_strips, slot_trials)
    group = _profile(frame, group)
    vector, anchor, other, live = _anchor_geometry(par, frame, comps, index, group, options)
    if _mode(group) == 'bridge_corridor':
        # Preserve the inter-host direction and limit the trial to nearby
        # channels. In particular, do not reuse the failure search's exterior.
        if not vector[0] or other is None:
            return
        points = [frame.pin_world(index[n], p) for n, p in (anchor, other)]
        pitches = [b-a for axis in (frame.column_x, frame.row_y) for a, b in zip(axis, axis[1:])]
        radius = max(2*options.cross_clearance, median(pitches) if pitches else 120.)
        for candidate in slot_trials(par, frame, comps, index, group, options):
            ports = candidate.passive_module_guides[group['id']]['ports']
            if any(not min(p[k] for p in points)-radius <= port[k] <= max(p[k] for p in points)+radius
                   for port in ports for k in (0, 1)):
                continue
            yield candidate
        return
    geometry = _geometry(par, frame, comps, index, group, vector, options)
    targets = _targets(par, frame, comps, index, group, geometry, anchor, live,
        scene, channels, options, deadline,
        forbidden=_guest_forbidden_strips(par, frame, comps, index, group), max_states=64)
    for target in targets[:8]:
        check_deadline(deadline, 'normal_module_choice')
        trial = _translated(frame, geometry, target, options.grid_step)
        end = group['ends'].index(live)
        guide = trial.passive_module_guides[group['id']]
        for path in _approach_paths(par, trial, index, anchor, guide['ports'][end], options):
            candidate = deepcopy(trial)
            candidate.passive_module_guides[group['id']]['routes'].setdefault(live, []).extend(
                (a, b) for a, b in path if a != b)
            yield candidate


def _identity(frame, index, group):
    guide = frame.passive_module_guides[group['id']]
    return (tuple((frame.pos_of(index[n]), frame.orient[index[n]]) for n in group['members']),
            tuple((net, tuple((tuple(a), tuple(b)) for a, b in segments))
                  for net, segments in sorted(guide['routes'].items())))


def _position_key(frame, index, group):
    return tuple((frame.pos_of(index[n]), frame.orient[index[n]]) for n in group['members'])


def _improves(before_key, before, after_key, after):
    # Access safety must not improve at the expense of longer, more costly
    # estimated connections. Use the existing estimator and original weights.
    return (after_key < before_key
            and after['estimated_cost'] <= before['estimated_cost']+EPS
            and after['interface_access_risks'] <= before['interface_access_risks']
            and after['estimated_channel_obstructions'] <= before['estimated_channel_obstructions']
            and after['centering_error'] <= before['centering_error']+EPS)


def refine_modules(par, frame, comps, nets, index, plan, options, *, deadline=None,
                   seconds=1., max_trials=64):
    """Compare at most four legal alternatives per module, keeping a rollback.

    This optional window follows complete ordinary packing, including its old
    remaining-budget terminal refinement. It has one ledger within the parent
    candidate deadline, so necessary insertion never loses its original quota.
    """
    if type(seconds) not in (int, float) or not isfinite(seconds) or seconds < 0:
        raise ValueError('Normal module choice seconds must be finite and non-negative')
    if type(max_trials) is not int or max_trials < 0:
        raise ValueError('Normal module choice trial limit must be a non-negative integer')
    started = perf_counter()
    stop = min(deadline, started+seconds) if deadline is not None else started+seconds
    report = dict(version=VERSION, applied=False, trials=0, actions=[], modules=[], skipped=[],
        seconds_limit=seconds, max_trials=max_trials, max_legal_alternatives=4,
        max_trials_per_module=16, incumbent_validated=True, active_frame_preserved=True,
        budget='bounded_window_within_candidate_deadline', stop_reason='no_improvement')
    current = frame
    coupled = {gid for family in plan['families'] for gid in family['groups']}
    fixed = set(getattr(frame, 'fixed', ())) | set(getattr(frame, 'fixed_cells', {}))
    groups = []
    for group in plan['groups']:
        reason = ('coupled_family' if group['id'] in coupled else
                  'fixed_guest' if any(n in fixed or index[n] in fixed for n in group['members']) else
                  'preserve_typed_branch' if not _mode(group) else None)
        if reason:
            report['skipped'].append(dict(group=group['id'], reason=reason))
        else:
            groups.append(group)
    report['eligible_count'] = len(groups)
    def priority(group):
        guide = current.passive_module_guides[group['id']]
        length = sum(abs(a[0]-b[0])+abs(a[1]-b[1])
            for net, ss in guide['routes'].items() if not par.power_marker_sym(net) for a, b in ss)
        return (-length, tuple(sorted(current.pin_world(index[n], p)
            for ts in group['anchors'].values() for n, p in ts)))
    try:
        for group in sorted(groups, key=priority):
            check_deadline(stop, 'normal_module_choice')
            if report['trials'] >= max_trials:
                report['stop_reason'] = 'trial_budget'
                break
            scene = build_scene(par, nets, current, index, options=options, deadline=stop)
            channels = _channels(scene, current.layout_guest_ids)
            before_key, before = _score(par, current, comps, nets, index, group, channels, options, stop)
            best, best_key, best_metrics = current, before_key, before
            module = dict(group=group['id'], members=list(group['members']), mode=_mode(group),
                trials=0, legal_alternatives=0, before=before, accepted=False, stop_reason='exhausted')
            report['modules'].append(module)
            seen = {_identity(current, index, group)}
            legal_positions = {_position_key(current, index, group)}
            own = {index[n] for n in group['members']}
            timed_out = False
            try:
                for trial in _proposals(par, current, comps, nets, index, group,
                                        options, scene, channels, stop):
                    check_deadline(stop, 'normal_module_choice')
                    key = _identity(trial, index, group)
                    if key in seen:
                        continue
                    seen.add(key)
                    if module['trials'] >= 16 or report['trials'] >= max_trials:
                        module['stop_reason'] = 'trial_budget'
                        break
                    module['trials'] += 1
                    report['trials'] += 1
                    if not _rigid_move(current, trial, own):
                        continue
                    from .placement import _guest_guards
                    from .module_connections import check_guides
                    from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
                    if not _guest_guards(par, trial, comps, index, plan['groups']):
                        continue
                    faults = _static_faults(par, trial, nets, index, options, stop)
                    if faults['count'] or faults['hard'] or check_guides(par, trial, nets, index, options, stop):
                        continue
                    score, metrics = _score(par, trial, comps, nets, index, group, channels, options, stop)
                    legal_positions.add(_position_key(trial, index, group))
                    module['legal_alternatives'] = len(legal_positions)-1
                    if _improves(best_key, best_metrics, score, metrics):
                        best, best_key, best_metrics = trial, score, metrics
                    if module['legal_alternatives'] >= 4:
                        module['stop_reason'] = 'legal_alternative_limit'
                        break
            except RoutingFailure as error:
                if error.diagnostics.get('reason') != 'quality_time_budget':
                    raise
                module['stop_reason'] = report['stop_reason'] = 'time_budget'
                timed_out = True
            if best is not current:
                report['actions'].append(dict(group=group['id'], members=list(group['members']),
                    position_changed=_position_key(current, index, group) != _position_key(best, index, group),
                    before={n:list(current.pos_of(index[n])) for n in group['members']},
                    after={n:list(best.pos_of(index[n])) for n in group['members']},
                    before_metrics=before, after_metrics=best_metrics))
                current = best
                report['applied'] = module['accepted'] = True
            module['after'] = best_metrics
            if timed_out:
                break
        else:
            report['stop_reason'] = ('no_eligible_modules' if not groups else
                                     'improved' if report['applied'] else 'no_improvement')
    except RoutingFailure as error:
        if error.diagnostics.get('reason') != 'quality_time_budget':
            raise
        report['stop_reason'] = 'time_budget'
    report['elapsed_seconds'] = round(perf_counter()-started, 6)
    return current, report
