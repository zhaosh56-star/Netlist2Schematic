"""Bounded failure-only insertion around the actual active skeleton.

Ordinary packing remains the fast path. These trials translate whole modules,
optionally use their wider template, and transact at most two blocking guests.
All proposals share the caller's deadline and full-check count.
"""
from collections import Counter
from copy import deepcopy
from itertools import zip_longest
from time import perf_counter

from netlist2schematic.routing.routing_rules import EPS, build_scene, check_deadline, segment_distance
from .terminal_slots import is_terminal, _targets, _channels, _score
from .module_connections import check_guides

VERSION = 'failed-module-2d-v2'


def anchor_profiles(par, frame, comps, index, group, options):
    """A shared node can be approached beside any of its real host ports.

    Keep explicit owner roles and common-tail centering. Otherwise the first
    conductive host is only the original proposal, not a permanent placement
    constraint. All hosts remain in the net and in the placement score.
    """
    from .placement import _anchor_geometry
    original = deepcopy(group)
    yield original
    owner = group.get('owner_port')
    if (not is_terminal(group) or group.get('lock_access_anchor')
            or owner and owner.get('evidence') != 'unique_conduction_port'
            or group.get('source_node_anchor')):
        return
    _, first, _, live = _anchor_geometry(par, frame, comps, index, group, options)
    terminals = sorted(group['anchors'][live], key=lambda t: (
        frame.pin_world(index[t[0]], t[1]), frame.pin_dir(index[t[0]], t[1]), t[1]))
    count = 1
    for terminal in terminals:
        if tuple(terminal) == tuple(first):
            continue
        alternate = deepcopy(group)
        alternate.pop('slot_ports', None)
        alternate['owner_port'] = dict(terminal=terminal)
        ports = _anchor_geometry(par, frame, comps, index, alternate, options)
        alternate = deepcopy(group)
        alternate['slot_ports'] = ports
        yield alternate
        count += 1
        if count >= 4:
            break


def park_groups(frame, groups, index):
    """Remove only the selected module geometry from a private trial frame."""
    trial = deepcopy(frame)
    members = {index[n] for group in groups for n in group['members']}
    for group in groups:
        getattr(trial, 'passive_module_guides', {}).pop(group['id'], None)
    far_x = max(frame.pos_of(i)[0] for i in range(len(frame.comps)))+500
    far_y = max(frame.pos_of(i)[1] for i in range(len(frame.comps)))+500
    for number, i in enumerate(sorted(members)):
        x, y = trial.pos_of(i)
        trial.passive_local_offsets[i][0] += far_x+220*number-x
        trial.passive_local_offsets[i][1] += far_y-y
    return trial


def placement_trials(par, frame, comps, nets, index, group, options, *, deadline,
                     ignored=(), widen=True):
    """Interleave nearby 2-D slots and standard/wider profiles deterministically."""
    from .placement import (_anchor_geometry, _geometry, _translated, _approach_paths,
                            slot_trials, _guest_forbidden_strips)
    check_deadline(deadline, 'module_2d_targets')
    original = deepcopy(group)
    saved = getattr(frame, 'passive_module_guides', {}).get(group['id'], {}).get('spacing', {})
    original['spacing_policy'] = saved.get('policy', group.get('spacing_policy',
        getattr(frame, 'passive_spacing_policy', 'compact')))
    original['extra_keepout'] = saved.get('extra_keepout', group.get('extra_keepout', 0))
    spacing_profiles = [original]
    if widen:
        if original['spacing_policy'] == 'compact':
            spacing_profiles.append(dict(original, spacing_policy='standard'))
        spacing_profiles.append(dict(original, spacing_policy='standard',
            extra_keepout=max(original['extra_keepout'], options.center_spacing)))
    profiles = [profile for anchored in anchor_profiles(par, frame, comps, index, original, options)
                for spacing in spacing_profiles
                for profile in [dict(anchored, spacing_policy=spacing['spacing_policy'],
                                     extra_keepout=spacing.get('extra_keepout', 0))]]
    scene = build_scene(par, nets, frame, index, options=options, deadline=deadline)
    channels = _channels(scene, frame.layout_guest_ids)

    def proposals(profile):
        if not is_terminal(profile):
            yield from slot_trials(par, frame, comps, index, profile, options)
            return
        vector, anchor, _, live = _anchor_geometry(par, frame, comps, index, profile, options)
        geometry = _geometry(par, frame, comps, index, profile, vector, options)
        targets = _targets(par, frame, comps, index, profile, geometry, anchor, live,
            scene, channels, options, deadline, ignored=ignored,
            forbidden=_guest_forbidden_strips(par,frame,comps,index,group),max_states=512)
        for target in targets:
            check_deadline(deadline, 'module_2d_trials')
            trial = _translated(frame, geometry, target, options.grid_step)
            port = trial.passive_module_guides[group['id']]['ports'][group['ends'].index(live)]
            for path in _approach_paths(par, trial, index, anchor, port, options):
                candidate = deepcopy(trial)
                guide = candidate.passive_module_guides[group['id']]
                guide['access_anchor'] = list(anchor)
                guide['routes'].setdefault(live, []).extend((a, b) for a, b in path if a != b)
                yield candidate

    seen = set()
    for row in zip_longest(*(proposals(profile) for profile in profiles)):
        for trial in row:
            if trial is None:
                continue
            guide = trial.passive_module_guides[group['id']]
            key = (tuple((trial.pos_of(index[n]), trial.orient[index[n]]) for n in group['members']),
                   tuple((net, tuple(segments)) for net, segments in sorted(guide['routes'].items())))
            if key in seen:
                continue
            seen.add(key)
            yield trial


class LocalPackingSearch:
    def __init__(self, par, comps, nets, index, plan, options, deadline, max_trials, report):
        self.par, self.comps, self.nets, self.index = par, comps, nets, index
        self.options = options
        self.deadline, self.max_trials, self.report = deadline, max_trials, report
        self.coupled = {gid for family in plan['families'] for gid in family['groups']}
        self.blockers = Counter()
        self.seen = set()
        self.details = dict(version=VERSION, applied=False, transactions=[], trials=0,
                            max_blockers=2, max_transactions=4, max_light_states=512,
                            max_targets_per_profile=32)
        report['insertion_search'] = self.details

    def available(self):
        check_deadline(self.deadline, 'module_2d_search')
        return self.report['trials'] < self.max_trials

    def movable(self, frame, group):
        fixed = set(getattr(frame, 'fixed', ())) | set(getattr(frame, 'fixed_cells', {}))
        return group['id'] not in self.coupled and not any(
            n in fixed or self.index[n] in fixed for n in group['members'])

    def check(self, trial, placed):
        from .placement import _guest_guards
        from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
        key = (tuple((trial.pos_of(i), trial.orient[i]) for i in range(len(self.comps))),
               tuple((gid, tuple((net, tuple(segments)) for net, segments in sorted(g['routes'].items())))
                     for gid, g in sorted(getattr(trial, 'passive_module_guides', {}).items())))
        if key in self.seen or not self.available():
            return False
        self.seen.add(key)
        self.report['trials'] += 1
        self.details['trials'] += 1
        if not _guest_guards(self.par, trial, self.comps, self.index, placed):
            self.details['leaf_rejections']=self.details.get('leaf_rejections',0)+1
            return False
        faults = _static_faults(self.par, trial, self.nets, self.index, self.options, self.deadline)
        violations = faults['violations']
        if not faults['count'] and not faults['hard']:
            violations = check_guides(self.par, trial, self.nets, self.index, self.options, self.deadline)
        if not violations and not faults['hard']:
            return True
        scene = build_scene(self.par, self.nets, trial, self.index,
                            options=self.options, deadline=self.deadline)
        for fault in violations:
            owners = set(fault.get('components', ()))
            if 'component' in fault:
                owners.add(fault['component'])
            for marker in (fault.get('marker'), *fault.get('markers', ())):
                if isinstance(marker, int) and marker < len(scene.marker_components):
                    owners.add(scene.marker_components[marker])
            segment = fault.get('segment')
            if segment:
                for net in fault.get('nets', ()):
                    for pin in scene.pins.get(net, ()):
                        if segment_distance(*segment, *pin.stub) < EPS:
                            owners.add(pin.component)
                    if net in scene.power_nets:
                        owners.update(self.index[name] for name,pin in self.nets[net]
                            if tuple(trial.pin_world(self.index[name],pin)) in segment)
            self.blockers.update(owners)
        self.details['last_violations'] = deepcopy(violations[:5])
        return False

    def insert(self, frame, order, placed, depth=0):
        check_deadline(self.deadline, 'module_2d_search')
        if depth == len(order):
            return frame
        if not self.available():
            return None
        group = order[depth]
        assigned = {self.index[n] for g in placed+order[:depth] for n in g['members']}
        own = {self.index[n] for n in group['members']}
        ignored = frame.layout_guest_ids-assigned-own
        choices = []
        channels = _channels(build_scene(self.par, self.nets, frame, self.index,
            options=self.options, deadline=self.deadline), frame.layout_guest_ids)
        for number, trial in enumerate(placement_trials(self.par, frame, self.comps, self.nets,
                self.index, group, self.options, deadline=self.deadline, ignored=ignored)):
            if number >= 48 or not self.available():
                break
            if not self.check(trial, placed+order[:depth+1]):
                continue
            key, _ = _score(self.par, trial, self.comps, self.nets, self.index, group,
                            channels, self.options, self.deadline)
            choices.append((key, number, trial))
            if len(choices) >= 4:
                break
        for _, _, trial in sorted(choices, key=lambda item: item[:2]):
            result = self.insert(trial, order, placed, depth+1)
            if result is not None:
                return result
        return None

    def find(self, frame, batch, placed):
        fixed = set(getattr(frame, 'fixed', ())) | set(getattr(frame, 'fixed_cells', {}))
        if any(n in fixed or self.index[n] in fixed for g in batch for n in g['members']):
            return None
        if any(not self.movable(frame, g) for g in batch):
            # Coupled banks retain the original atomic family search.
            return self.original_batch(frame, batch, placed)
        self.blockers.clear()
        result = self.insert(frame, batch, placed)
        if result is not None:
            self.details['applied'] = True
            return result
        shared = {n for g in batch for n in g['ends'] if not self.par.power_marker_sym(n)}
        blocking = [g for g in placed if self.movable(frame, g) and (
            any(self.blockers[self.index[n]] for n in g['members']) or shared.intersection(g['ends']))]
        blocking.sort(key=lambda g: (-sum(self.blockers[self.index[n]] for n in g['members']),
            -len(shared.intersection(g['ends'])),
            tuple(frame.pos_of(self.index[n]) for n in g['members'])))
        for count in range(1, min(2, len(blocking))+1):
            moved = deepcopy(blocking[:count])
            for group in moved:
                spacing = frame.passive_module_guides[group['id']]['spacing']
                group['spacing_policy'] = spacing['policy']
                group['extra_keepout'] = spacing.get('extra_keepout', 0)
            moved_ids = {g['id'] for g in moved}
            remaining = [g for g in placed if g['id'] not in moved_ids]
            for order in (batch+moved, moved+batch):
                if len(self.details['transactions']) >= 4 or not self.available():
                    return None
                action = dict(groups=[g['id'] for g in order], accepted=False)
                self.details['transactions'].append(action)
                # Each order starts with exactly the same previous accepted frame.
                result = self.insert(park_groups(frame, moved, self.index), order, remaining)
                if result is not None:
                    action['accepted'] = True
                    self.details['applied'] = True
                    return result
        return None

    def original_batch(self, frame, batch, placed, depth=0):
        from .placement import slot_trials
        if not self.available():
            return None
        if depth == len(batch):
            return frame if self.check(frame, placed+batch) else None
        for trial in slot_trials(self.par, frame, self.comps, self.index, batch[depth], self.options):
            result = self.original_batch(trial, batch, placed, depth+1)
            if result is not None:
                return result
        return None

    def record_profiles(self, frame, report):
        self.details.pop('failed_batch',None)
        for group in report['plan']['groups']:
            spacing = frame.passive_module_guides[group['id']]['spacing']
            group['spacing_policy'] = spacing['policy']
            group['extra_keepout'] = spacing.get('extra_keepout', 0)
        self.details['profiles'] = {gid: deepcopy(g['spacing']) for gid, g in frame.passive_module_guides.items()}


def resume_failed(par, core, comps, nets, index, plan, options, previous, *, deadline=None,
                  seconds=None, max_trials=None):
    """Use a private partial checkpoint, with one separate parent-clipped ledger."""
    from .placement import _pack_once
    from netlist2schematic.common.layout_budget import LayoutBudget, PackingBudget
    if previous.get('applied') or previous.get('stop_reason') not in (
            'local_budget', 'budget_exhausted', 'no_legal_slot'):
        return None, previous
    limits = LayoutBudget('adaptive').for_plan(plan)
    ledger = PackingBudget(limits['normal_seconds'] if seconds is None else seconds,
        limits['normal_trials'] if max_trials is None else max_trials, deadline)
    allowance, quota = ledger.allocate()
    if allowance <= 0 or quota <= 0:
        return None, previous
    checkpoint = deepcopy(getattr(previous, 'checkpoint', None))
    attempt_plan = deepcopy(previous.get('plan', plan))
    # Auto is a selection policy, not a template. A fresh resume starts at
    # standard spacing; a captured frame retains every actual placed profile.
    if attempt_plan.get('spacing_policy', 'auto') == 'auto':
        attempt_plan['spacing_policy'] = 'standard'
    result, report = _pack_once(par, core, comps, nets, index, attempt_plan, options,
        deadline=ledger.deadline, seconds=allowance, max_trials=quota,
        refine_terminal_placements=False, _resume=checkpoint, _spatial_search=True)
    ledger.charge(report)
    report.setdefault('insertion_search', dict(version=VERSION, applied=False)).update(ledger.snapshot(ledger.stop_reason(report)),
        checkpoint_resumed=checkpoint is not None, previous_stop_reason=previous['stop_reason'])
    report['previous_packing'] = dict(previous)
    return result, report
