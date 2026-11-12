"""Conservative post-route quality tradeoffs, separate from geometric legality.

These checks never replace the original route validator. Crossing identities
include both network names and coordinates so moved crossings are rechecked.
"""
from __future__ import annotations

from math import isfinite
from statistics import median

from routing_rules import EPS, check_deadline, intersection

VERSION = 'sparse_crossing_tradeoff_v1'
MAX_EXTRA_CROSSINGS = 2
CROSSING_COST = 150.0
BEND_COST = 15.0
MIN_PATH_REDUCTION = .20


def physical_pitch(par, placement):
    gaps = [b-a for name in ('column_x', 'row_y')
            for a, b in zip(getattr(placement, name, ()),
                            getattr(placement, name, ())[1:])
            if isfinite(a) and isfinite(b) and b > a]
    return float(median(gaps) if gaps else getattr(par, 'CELL', 150.0))


def crossing_identities(topologies, power_nets=(), deadline=None):
    """Count proper intersections on merged runs, ignoring artificial cuts."""
    names = sorted(net for net in topologies if net not in power_nets)
    result, operations = set(), 0
    for index, net in enumerate(names):
        check_deadline(deadline, 'simplify_tradeoff_crossings')
        for other in names[index+1:]:
            for a, b in topologies[net]['runs']:
                for c, d in topologies[other]['runs']:
                    operations += 1
                    if operations % 64 == 0:
                        check_deadline(deadline, 'simplify_tradeoff_crossings')
                    point = intersection(a, b, c, d)
                    if point is not None and point not in (a, b, c, d):
                        result.add((net, other, tuple(point)))
    check_deadline(deadline, 'simplify_tradeoff_crossings')
    return frozenset(result)


def _crossing_record(crossing):
    return {'nets': list(crossing[:2]), 'point': list(crossing[2])}


def crossing_spacing(before, after, required, deadline=None):
    """New or moved intersections must be sparse relative to every final X."""
    introduced = sorted(after-before)
    nearest, conflict, operations = None, None, 0
    for crossing in introduced:
        check_deadline(deadline, 'simplify_tradeoff_density')
        point = crossing[2]
        for other in sorted(after):
            operations += 1
            if operations % 64 == 0:
                check_deadline(deadline, 'simplify_tradeoff_density')
            if other == crossing:
                continue
            distance = max(abs(point[0]-other[2][0]), abs(point[1]-other[2][1]))
            nearest = distance if nearest is None else min(nearest, distance)
            if conflict is None and distance < required-EPS:
                conflict = {'crossing': _crossing_record(crossing),
                            'other_crossing': _crossing_record(other),
                            'distance': distance}
    check_deadline(deadline, 'simplify_tradeoff_density')
    return {'new_or_moved_crossings': [_crossing_record(c) for c in introduced],
            'removed_crossings': [_crossing_record(c) for c in sorted(before-after)],
            'required_distance': required, 'nearest_distance': nearest,
            'conflict': conflict}


def cost(metrics):
    return metrics['wirelength'] + CROSSING_COST*metrics['crossings'] + BEND_COST*metrics['bends']


def evaluate(before, after, initial_crossings, proposal, pitch,
             before_crossings, after_crossings, cross_clearance, deadline=None):
    """Assess a completely validated candidate against immutable global credit.

    The 20% test refers to the actual generator's replaced free path, never the
    whole net's length. The absolute saving is measured on final whole-graph
    geometry, so reused same-net runs cannot count twice.
    """
    delta = after['crossings']-before['crossings']
    saved = before['wirelength']-after['wirelength']
    record = {'accepted': False, 'reason': None,
              'crossings_before': before['crossings'], 'crossings_after': after['crossings'],
              'crossing_increase': delta,
              'cumulative_crossing_increase': after['crossings']-initial_crossings,
              'maximum_crossings': initial_crossings+MAX_EXTRA_CROSSINGS,
              'saved_length': saved, 'weighted_cost_before': cost(before),
              'weighted_cost_after': cost(after)}

    def reject(reason):
        record['reason'] = reason
        return record

    if (after['bends'] > before['bends'] or saved < -EPS
            or (saved <= EPS and after['bends'] >= before['bends'])):
        return reject('shape_non_improvement')
    if after['crossings'] > initial_crossings+MAX_EXTRA_CROSSINGS:
        return reject('crossing_allowance')
    if delta > 0:
        prefix = ('branch' if 'old_branch_length' in proposal else 'chain')
        old = proposal.get('old_'+prefix+'_length')
        new = proposal.get('new_'+prefix+'_length')
        if (type(old) not in (int, float) or type(new) not in (int, float)
                or not isfinite(old) or not isfinite(new) or old <= EPS or new < 0):
            return reject('missing_path_evidence')
        reduction = (old-new)/old
        required = max(2*pitch, CROSSING_COST*delta)
        record.update(old_path_length=old, new_path_length=new,
                      path_reduction=reduction, required_path_reduction=MIN_PATH_REDUCTION,
                      minimum_saved_length=required)
        if reduction < MIN_PATH_REDUCTION-EPS:
            return reject('path_reduction')
        if saved < required-EPS:
            return reject('minimum_length_saving')
    if record['weighted_cost_after'] >= record['weighted_cost_before']-EPS:
        return reject('weighted_cost')
    density = crossing_spacing(before_crossings, after_crossings, 2*cross_clearance, deadline)
    record['density'] = density
    if density['conflict'] is not None:
        return reject('crossing_density')
    record.update(accepted=True, reason='balanced_gain' if delta > 0 else 'strict_gain')
    return record
