"""Source-derived, optional regions and comparable whole-layout measurements.

Only complete terminal-role motifs create regions. Shared rails and control
nets are evidence at interfaces, never edges in a transitive owner union.
This module neither places devices nor changes the original quality score.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
from itertools import combinations, product
import json
from math import inf, isfinite

VERSION = 'topological-regions-v1'
SELECTION_VERSION = 'region-selection-v1'
LENGTH_RATIO = 1.15


def _pol(dev):
    if dev.get('cls') == 'MOS':
        value = str(dev.get('type', '')).lower()
        return value if value in ('nmos', 'pmos') else None
    return None


def _opposite(value):
    return {'nmos': 'pmos', 'pmos': 'nmos'}.get(value)


def _rails(devices, evidence):
    result = {net for net, row in evidence.get('net_roles', {}).items()
              if (row.get('role') if isinstance(row, dict) else row) == 'power'}
    drains, sources, bulks = set(), defaultdict(list), defaultdict(list)
    for name, dev in devices.items():
        if _pol(dev):
            drains.add(dev['d'])
            sources[dev['s']].append(name)
            bulks[dev.get('bulk')].append(name)
    # A fed internal common source has a drain incident on it and cannot pass.
    # Bulk-only source boundaries also survive arbitrary rail renaming.
    def may_infer(net):
        row = evidence.get('net_roles', {}).get(net, {})
        return not (isinstance(row, dict) and (row.get('role') == 'signal' or row.get('conflict')))
    result.update(net for net in sources if net not in drains and len(bulks[net]) >= 2
                  and may_infer(net))
    return result


def _pair_seeds(devices, evidence, relations, rails):
    # Calibrated rejection is authoritative: an older relation list must not
    # resurrect a pair that the evidence layer deliberately removed.
    rows = evidence['relations'] if 'relations' in evidence else (relations or ())
    found = set()
    for row in rows:
        if row.get('kind') != 'differential_pair':
            continue
        names = row.get('members', row.get('devices', ()))
        if len(names) != 2 or any(name not in devices for name in names):
            continue
        a, b = (devices[name] for name in names)
        if (_pol(a) and _pol(a) == _pol(b) and a['s'] == b['s']
                and a['s'] not in rails and a['d'] != b['d'] and a['g'] != b['g']):
            found.add(tuple(sorted(names)))
    return sorted(found)


def _unique(values):
    return values[0] if len(values) == 1 else None


def _source_axis_weights(devices, evidence):
    """Keep original functional weights; max-deduplicate repeated sources."""
    result = {}
    for row in evidence.get('structures', ()):
        roles, kind = row.get('roles', {}), row.get('kind')
        pairs = []
        if kind in ('complementary_branch', 'output_pair'):
            pairs = [(roles.get('upper'), roles.get('lower'), 3.5)]
        elif kind == 'latch':
            pairs = [(roles.get(side + '_upper'), roles.get(side + '_lower'), 4.5)
                     for side in ('left', 'right')]
        elif kind == 'folded_core':
            for side in ('left', 'right'):
                lane = [roles.get(prefix + '_' + side) for prefix in ('input', 'folded', 'bias')]
                pairs.extend((a, b, 3.0) for a, b in zip(lane, lane[1:]))
        for a, b, weight in pairs:
            if a in devices and b in devices:
                key = frozenset((a, b))
                result[key] = max(weight, result.get(key, 0))
    return result


class _Builder:
    def __init__(self, devices, rails, axis_weights=None):
        self.devices, self.rails = devices, rails
        self.regions, self.ownership = [], {}
        self.axes, self.correspondences = [], []
        self.edges, self.junctions, self.ambiguities = [], [], []
        self.axis_seen = set()
        self.axis_weights = axis_weights or {}
        self.channel_degree = defaultdict(int)
        for device in devices.values():
            if _pol(device):
                self.channel_degree[device['d']] += 1
                self.channel_degree[device['s']] += 1

    def find(self, **fields):
        return sorted(name for name, dev in self.devices.items() if _pol(dev)
                      and all((_pol(dev) if field == 'polarity' else dev.get(field)) == value
                              for field, value in fields.items()))

    def ambiguity(self, kind, members, reason, **extra):
        record = dict(kind=kind, members=sorted(members), reason=reason, **extra)
        if record not in self.ambiguities:
            self.ambiguities.append(record)

    def region(self, kind, members, *, parent=None, children=(), pairs=(), branches=(),
               attachments=()):
        names = sorted(set(members))
        identifier = 'region:' + kind + ':' + '|'.join(names)
        row = dict(id=identifier, kind=kind, members=names, parent=parent,
                   children=list(children), pairs=[list(pair) for pair in pairs],
                   branches=deepcopy(list(branches)), attachments=deepcopy(list(attachments)))
        self.regions.append(row)
        if not children:
            for name in names:
                if name in self.ownership:
                    raise ValueError('region leaves must be disjoint')
                self.ownership[name] = identifier
        for child in children:
            next(item for item in self.regions if item['id'] == child)['parent'] = identifier
        return identifier

    def axis(self, a, pin_a, b, pin_b, source):
        net = self.devices[a][pin_a.lower()]
        if self.devices[b][pin_b.lower()] != net:
            raise ValueError('series axis endpoints must share a real net')
        key = tuple(sorted(((a, pin_a), (b, pin_b))))
        if key not in self.axis_seen:
            self.axis_seen.add(key)
            generic = 2.5 / max(1, self.channel_degree[net] - 1)
            weight = max(generic, self.axis_weights.get(frozenset((a, b)), 0.0))
            self.axes.append(dict(a=a, b=b, pin_a=pin_a, pin_b=pin_b, net=net,
                                  weight=weight, source=source))

    def correspondence(self, kind, pairs, source):
        self.correspondences.append(dict(id='correspondence:' + str(len(self.correspondences)),
            kind=kind, axis_mode=kind, pairs=[list(pair) for pair in pairs], weight=1.0,
            source=source))

    def order(self, regions, source):
        self.edges.append(dict(kind='region_order', regions=list(regions),
                               reversible=True, source=source))

    def available(self, names, kind):
        conflicts = sorted(set(names) & set(self.ownership))
        if conflicts:
            self.ambiguity(kind, names, 'overlapping_region_candidates', conflicts=conflicts)
            return False
        return True


def _strongarm(builder, pair):
    """Recognize polarity-dual input/latch/tail plus paired clocked rail taps."""
    devs, rails = builder.devices, builder.rails
    a, b = pair
    pol = _pol(devs[a])
    source = devs[a]['s']
    tails = [name for name in builder.find(d=source, polarity=pol)
             if devs[name]['s'] in rails]
    if not tails:
        return False
    xa, xb = devs[a]['d'], devs[b]['d']
    qa = builder.find(s=xa, polarity=pol)
    qb = builder.find(s=xb, polarity=pol)
    motifs = []
    for ta, left, right in product(tails, qa, qb):
        ya, yb = devs[left]['d'], devs[right]['d']
        if ya == yb or devs[left]['g'] != yb or devs[right]['g'] != ya:
            continue
        pa = [name for name in builder.find(d=ya, g=yb, polarity=_opposite(pol))
              if devs[name]['s'] in rails]
        pb = [name for name in builder.find(d=yb, g=ya, polarity=_opposite(pol))
              if devs[name]['s'] in rails]
        for upper_a, upper_b in product(pa, pb):
            clock = devs[ta]['g']
            if devs[upper_a]['s'] != devs[upper_b]['s']:
                continue
            rail = devs[upper_a]['s']
            taps = []
            for target in (xa, xb, ya, yb):
                taps.append(builder.find(d=target, s=rail, g=clock, polarity=_opposite(pol)))
            # A matched tap at each distinct target role is evidence of the
            # complete rail-access family, rather than merely a common clock.
            if not all(len(items) == 1 for items in taps):
                if any(taps):
                    builder.ambiguity('clocked_comparator', pair, 'nonunique_clocked_rail_taps')
                continue
            names = [a, b, ta, left, right, upper_a, upper_b, *(items[0] for items in taps)]
            if len(set(names)) == 11:
                motifs.append((ta, left, right, upper_a, upper_b, [items[0] for items in taps]))
    if not motifs:
        return False
    if len(motifs) != 1:
        builder.ambiguity('clocked_comparator', pair, 'nonunique_role_matching', count=len(motifs))
        return False
    tail, qa, qb, pa, pb, taps = motifs[0]
    names = [a, b, tail, qa, qb, pa, pb, *taps]
    if not builder.available(names, 'clocked_comparator'):
        return False
    pairs = [[a, b], [qa, qb], [pa, pb]]
    branches = [dict(id='A', members=[a, qa, pa]), dict(id='B', members=[b, qb, pb])]
    core = builder.region('comparator_core', names[:7], pairs=pairs, branches=branches)
    wings = []
    for lane, side, targets in (('A', [taps[0], taps[2]], [xa, devs[qa]['d']]),
                                ('B', [taps[1], taps[3]], [xb, devs[qb]['d']])):
        attachments = [dict(device=name, pin='D', net=net, branch=lane,
                            target_region=core, role='clocked_rail') for name, net in zip(side, targets)]
        wings.append(builder.region('clocked_rail_attachments', side, attachments=attachments,
                                     branches=[dict(id=lane, members=side)]))
    parent = builder.region('clocked_comparator', names, children=[core, *wings])
    builder.order([wings[0], core, wings[1]], parent)
    builder.correspondence('direct', pairs, parent)
    builder.correspondence('interface', [[taps[0], taps[1]], [taps[2], taps[3]]], parent)
    for first, second in ((a, qa), (b, qb)):
        builder.axis(first, 'D', second, 'S', parent)
    for first, second in ((qa, pa), (qb, pb)):
        builder.axis(first, 'D', second, 'D', parent)
    builder.junctions.append(dict(kind='shared_tail', members=[a, b], tail=[tail],
                                  net=source, source=parent))
    return True


def _detector_candidates(builder, outputs, input_nodes, polarity, excluded):
    """Two comparisons, common reference, paired diode loads, and a return."""
    devs = builder.devices
    output_set = set(outputs)
    groups = defaultdict(list)
    for name, dev in devs.items():
        if _pol(dev) == polarity and name not in excluded and dev['s'] not in builder.rails:
            groups[dev['s']].append(name)
    comparisons = []
    for node, names in sorted(groups.items()):
        if len(names) != 2:
            continue
        sensed = [name for name in names if devs[name]['g'] in output_set]
        if len(sensed) != 1:
            continue
        sensor = sensed[0]
        reference = next(name for name in names if name != sensor)
        if devs[reference]['g'] in output_set:
            continue
        feeds = [name for name in builder.find(d=node, polarity=polarity)
                 if devs[name]['s'] in builder.rails and name not in excluded]
        comparisons.append((sensor, reference, feeds))
    motifs = []
    for first, second in combinations(comparisons, 2):
        if devs[first[0]]['g'] == devs[second[0]]['g']:
            continue
        if devs[first[1]]['g'] != devs[second[1]]['g']:
            continue
        sense = devs[first[0]]['d']
        reference = devs[first[1]]['d']
        if (sense == reference or devs[second[0]]['d'] != sense
                or devs[second[1]]['d'] != reference):
            continue
        loads = [[name for name in builder.find(d=net, g=net, polarity=_opposite(polarity))
                  if devs[name]['s'] in builder.rails and name not in excluded]
                 for net in (sense, reference)]
        if not all(len(items) == 1 for items in loads):
            continue
        for returned in (sense, reference):
            actuators = [[name for name in builder.find(d=node, g=returned,
                           polarity=_opposite(polarity)) if devs[name]['s'] in builder.rails]
                         for node in input_nodes]
            if not all(len(items) == 1 for items in actuators):
                continue
            if not all(len(item[2]) == 1 for item in (first, second)):
                continue
            # Branch identity follows sensed output equality, independently
            # of the detector's eventual physical ordering.
            ordered = sorted((first, second), key=lambda item: outputs.index(devs[item[0]]['g']))
            names = [name for item in ordered for name in (*item[:2], item[2][0])]
            names += [items[0] for items in loads]
            if len(set(names)) == 8 and not (set(names) & excluded):
                motifs.append(dict(members=names, comparisons=ordered,
                    loads=[items[0] for items in loads], returned=returned,
                    actuators=[items[0] for items in actuators]))
    return motifs


def _folded(builder, pair):
    devs, rails = builder.devices, builder.rails
    a, b = pair
    pol, opposite = _pol(devs[a]), _opposite(_pol(devs[a]))
    nodes = [devs[a]['d'], devs[b]['d']]
    feeds = [name for name in builder.find(d=devs[a]['s'], polarity=pol)
             if devs[name]['s'] in rails]
    continuations = [builder.find(s=node, polarity=opposite) for node in nodes]
    motifs = []
    for feed, qa, qb in product(feeds, *continuations):
        outputs = [devs[qa]['d'], devs[qb]['d']]
        if outputs[0] == outputs[1] or devs[qa]['g'] != devs[qb]['g']:
            continue
        upper = [builder.find(d=node, polarity=pol) for node in outputs]
        for pa, pb in product(*upper):
            if devs[pa]['g'] != devs[pb]['g'] or devs[pa]['s'] == devs[pb]['s']:
                continue
            tops = [[name for name in builder.find(d=devs[p]['s'], polarity=pol)
                     if devs[name]['s'] in rails] for p in (pa, pb)]
            sinks = [[name for name in builder.find(d=node, polarity=opposite)
                      if devs[name]['s'] in rails] for node in nodes]
            for ta, tb in product(*tops):
                if devs[ta]['g'] != devs[tb]['g']:
                    continue
                sink_pairs = [(sa, sb) for sa, sb in product(*sinks)
                              if devs[sa]['g'] == devs[sb]['g']]
                base = [a, b, feed, qa, qb, pa, pb, ta, tb]
                detectors = _detector_candidates(builder, outputs, nodes, pol, set(base))
                if len(detectors) > 1:
                    builder.ambiguity('folded_feedback', pair, 'nonunique_detector_matching',
                                      count=len(detectors))
                    continue
                detector = detectors[0] if detectors else None
                if detector:
                    sink_pairs = [item for item in sink_pairs
                                  if not set(item) & set(detector['actuators'])]
                # Multiple static sink roles are electrically indistinguishable
                # for this bounded motif; do not select by name or coordinates.
                if len(sink_pairs) != 1:
                    if sink_pairs:
                        builder.ambiguity('folded_main', pair, 'nonunique_sink_matching')
                    continue
                sa, sb = sink_pairs[0]
                own = [*base, sa, sb]
                if detector:
                    own += detector['actuators'] + detector['members']
                if len(own) == len(set(own)):
                    motifs.append((feed, qa, qb, pa, pb, ta, tb, sa, sb, detector))
    if not motifs:
        return False
    if len(motifs) != 1:
        builder.ambiguity('folded_main', pair, 'nonunique_role_matching', count=len(motifs))
        return False
    feed, qa, qb, pa, pb, ta, tb, sa, sb, detector = motifs[0]
    inputs, outputs = [a, b, feed], [ta, tb, pa, pb, qa, qb, sa, sb]
    if detector:
        inputs += detector['actuators']
    names = inputs + outputs + (detector['members'] if detector else [])
    if not builder.available(names, 'folded_feedback'):
        return False
    input_pairs = [[a, b]] + ([detector['actuators']] if detector else [])
    output_pairs = [[ta, tb], [pa, pb], [qa, qb], [sa, sb]]
    input_id = builder.region('folded_input', inputs, pairs=input_pairs,
        branches=[dict(id='A', members=[a]), dict(id='B', members=[b])])
    output_id = builder.region('folded_output', outputs, pairs=output_pairs,
        branches=[dict(id='A', members=[ta, pa, qa, sa]),
                  dict(id='B', members=[tb, pb, qb, sb])])
    main = builder.region('folded_main', inputs + outputs, children=[input_id, output_id])
    order = [input_id, output_id]
    builder.correspondence('direct', output_pairs, main)
    builder.correspondence('folded', [[a, b], [qa, qb]], main)
    if detector:
        cp = detector['comparisons']
        local_pairs = [[item[0], item[1]] for item in cp]
        detector_id = builder.region('feedback_detector', detector['members'], pairs=local_pairs,
            branches=[dict(id=lane, members=[item[2][0], item[0], item[1]])
                      for lane, item in zip(('A', 'B'), cp)])
        system = builder.region('folded_feedback', names, children=[main, detector_id])
        order.append(detector_id)
        builder.correspondence('interface', [[qa, qb], [cp[0][0], cp[1][0]]], system)
        builder.edges.extend([dict(kind='sensing_interface', source_region=output_id,
            target_region=detector_id, nets=[devs[qa]['d'], devs[qb]['d']]),
            dict(kind='feedback_return', source_region=detector_id, target_region=input_id,
                 net=detector['returned'])])
        for item in cp:
            builder.junctions.append(dict(kind='shared_feed', members=list(item[:2]),
                tail=[item[2][0]], net=devs[item[0]]['s'], source=system))
        builder.correspondence('direct', input_pairs, main)
        for inp, actuator in zip((a, b), detector['actuators']):
            builder.axis(inp, 'D', actuator, 'D', main)
    builder.order(order, main)
    for top, upper, lower, sink in ((ta, pa, qa, sa), (tb, pb, qb, sb)):
        builder.axis(top, 'D', upper, 'S', main)
        builder.axis(upper, 'D', lower, 'D', main)
        builder.axis(lower, 'S', sink, 'D', main)
    # Shared-source centering is a junction term, not two incompatible axes.
    builder.junctions.append(dict(kind='shared_feed', members=[a, b], tail=[feed],
                                  net=devs[a]['s'], source=main))
    builder.junctions.extend(dict(kind='fold', net=node, input=inp, continuation=cont,
                                  source=main) for node, inp, cont in zip(nodes, (a, b), (qa, qb)))
    return True


def build_region_plan(devices, evidence=None, relations=None, display_rows=None, comps=None, index=None):
    """Build JSON intent from source topology; GT and placement are not inputs.

    ``display_rows`` records the unchanged old row contract in the signature.
    A branch member sequence is semantic, not a new vertical ordering request.
    """
    if evidence is None:
        from netlist2schematic.plugins.layout_shared.layout_evidence import build_layout_evidence
        evidence = build_layout_evidence(devices)
    rails = _rails(devices, evidence)
    weights = _source_axis_weights(devices, evidence)
    builder = _Builder(devices, rails, weights)
    seeds = _pair_seeds(devices, evidence, relations, rails)
    proposals = []
    for pair in seeds:
        for recognizer in (_strongarm, _folded):
            candidate = _Builder(devices, rails, weights)
            if recognizer(candidate, pair):
                proposals.append(candidate)
            for ambiguity in candidate.ambiguities:
                if ambiguity not in builder.ambiguities:
                    builder.ambiguities.append(ambiguity)
    # No candidate may win a disputed device just because its seed sorts first.
    conflicted = set()
    for i, j in combinations(range(len(proposals)), 2):
        overlap = set(proposals[i].ownership) & set(proposals[j].ownership)
        if overlap:
            conflicted.update((i, j))
            builder.ambiguity('region_ownership', overlap, 'overlapping_region_candidates')
    for number, candidate in enumerate(proposals):
        if number in conflicted:
            continue
        builder.regions.extend(candidate.regions)
        builder.ownership.update(candidate.ownership)
        builder.axes.extend(candidate.axes)
        builder.junctions.extend(candidate.junctions)
        builder.edges.extend(candidate.edges)
        for row in candidate.correspondences:
            row['id'] = 'correspondence:' + str(len(builder.correspondences))
            builder.correspondences.append(row)
    active = sorted(name for name, dev in devices.items() if dev.get('cls') in ('MOS', 'BJT'))
    covered = sorted(builder.ownership)
    rows = {str(name): int(value) for name, value in (display_rows or {}).items()
            if name in devices and devices[name].get('cls') in ('MOS', 'BJT')}
    plan = dict(version=VERSION, supported=bool(builder.regions), regions=builder.regions,
        ownership=builder.ownership, series_axes=builder.axes, correspondences=builder.correspondences,
        junctions=builder.junctions, edges=builder.edges, ambiguities=builder.ambiguities,
        display_rows=dict(sorted(rows.items())),
        coverage=dict(active_devices=len(active), recognized_devices=len(covered),
                      recognized_fraction=len(covered) / len(active) if active else None,
                      unassigned_active=sorted(set(active) - set(covered)),
                      seed_pairs=len(seeds), leaf_regions=sum(not row['children'] for row in builder.regions),
                      series_axes=len(builder.axes), correspondences=len(builder.correspondences),
                      ambiguity_count=len(builder.ambiguities)))
    # Identity includes named source topology so a changed unrecognized subgraph
    # cannot silently reuse a feedback plan, but excludes physical grid spacing.
    signature_source = dict(plan=plan, devices={name: dev for name, dev in sorted(devices.items())})
    plan['signature'] = sha256(json.dumps(signature_source, sort_keys=True,
                                         separators=(',', ':'), default=str).encode()).hexdigest()
    return plan


def measure_regions(pl, comps, index, plan):
    """Read physical positions and real pins; missing coverage stays unknown."""
    positions, missing = {}, []
    for name in plan.get('ownership', {}):
        try:
            point = tuple(pl.pos_of(index[name]))
            if len(point) != 2 or not all(isfinite(value) for value in point):
                raise ValueError('nonfinite position')
            positions[name] = point
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            missing.append(name)
    all_positions = {}
    for name, idx in index.items():
        try:
            all_positions[name] = tuple(pl.pos_of(idx))
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            pass
    coverage = deepcopy(plan.get('coverage', {}))
    coverage.update(positioned_devices=len(positions), missing_devices=sorted(missing),
                    measured_axes=0, missing_axes=[])
    metrics = dict(version=VERSION, plan_signature=plan.get('signature'),
                   supported=bool(plan.get('supported')), measurable=False,
                   coverage=coverage, ambiguity_count=len(plan.get('ambiguities', ())),
                   interleaving=None, order_errors=None, pin_axis_error=None,
                   axis_drift=None, dispersion=None, junction_error=None)
    if not metrics['supported'] or missing:
        return metrics
    interleaving, dispersion, order_errors, drift = 0, 0.0, 0, 0.0
    leaves = [row for row in plan.get('regions', ()) if not row.get('children')]
    by_id = {row['id']: row for row in plan.get('regions', ())}
    for region in leaves:
        members = region['members']
        by_y = defaultdict(list)
        for name in members:
            by_y[positions[name][1]].append(name)
        for y, row_members in by_y.items():
            lo, hi = min(positions[name][0] for name in row_members), max(positions[name][0] for name in row_members)
            interleaving += sum(name not in members and point[1] == y and lo < point[0] < hi
                                for name, point in all_positions.items())
        xs = [positions[name][0] for name in members]
        dispersion += (max(xs) - min(xs)) / (100.0 * max(1, len(members) - 1))
    for relation in plan.get('correspondences', ()):
        mode = relation.get('axis_mode', relation.get('kind'))
        if mode == 'interface':
            continue
        differences, centers = [], []
        for a, b in relation.get('pairs', ()):
            if a not in positions or b not in positions:
                continue
            differences.append(positions[b][0] - positions[a][0])
            centers.append((positions[a][0] + positions[b][0]) / 2)
        if differences:
            positives = sum(value > 1e-9 for value in differences)
            negatives = sum(value < -1e-9 for value in differences)
            zeros = len(differences) - positives - negatives
            order_errors += min(positives, negatives) + zeros
        if mode == 'direct' and centers:
            drift += (max(centers) - min(centers)) / 100.0
    def descendants(identifier, visiting=()):
        if identifier not in by_id or identifier in visiting:
            raise ValueError('unknown or cyclic order region')
        region = by_id[identifier]
        if not region.get('children'):
            return set(region['members'])
        return set().union(*(descendants(child, (*visiting, identifier))
                             for child in region['children']))
    # A region ordering is reversible as a whole, never independently per row.
    # Compare across rows too: one region cannot change sides in another band.
    for edge in plan.get('edges', ()):
        if edge.get('kind') != 'region_order':
            continue
        ranks = {}
        try:
            for rank, identifier in enumerate(edge['regions']):
                for name in descendants(identifier):
                    if name in ranks or name not in positions:
                        raise ValueError('overlapping or missing order member')
                    ranks[name] = rank
        except (KeyError, ValueError):
            coverage.setdefault('missing_order_regions', []).append(deepcopy(edge))
            continue
        comparisons = []
        for a, b in combinations(ranks, 2):
            if ranks[a] == ranks[b]:
                continue
            comparisons.append((ranks[b] - ranks[a]) * (positions[b][0] - positions[a][0]))
        order_errors += min(sum(value < 0 for value in comparisons),
                            sum(value > 0 for value in comparisons)) + sum(value == 0 for value in comparisons)
    pin_error = 0.0
    for axis in plan.get('series_axes', ()):
        try:
            pa = pl.pin_world(index[axis['a']], axis['pin_a'])
            pb = pl.pin_world(index[axis['b']], axis['pin_b'])
            if not all(isfinite(value) for value in (*pa, *pb)):
                raise ValueError('nonfinite pin')
            pin_error += abs(pa[0] - pb[0]) * axis.get('weight', 1.0) / 100.0
            coverage['measured_axes'] += 1
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            coverage['missing_axes'].append(deepcopy(axis))
    junction_error = 0.0
    for junction in plan.get('junctions', ()):
        if junction.get('kind') not in ('shared_tail', 'shared_feed'):
            continue
        try:
            center = sum(pl.pin_world(index[name], 'S')[0] for name in junction['members']) / len(junction['members'])
            tail = sum(pl.pin_world(index[name], 'D')[0] for name in junction['tail']) / len(junction['tail'])
            junction_error += abs(center - tail) / 100.0
        except (KeyError, IndexError, TypeError, ValueError, AttributeError, ZeroDivisionError):
            coverage.setdefault('missing_junctions', []).append(deepcopy(junction))
    complete = not (coverage['missing_axes'] or coverage.get('missing_junctions')
                    or coverage.get('missing_order_regions'))
    metrics.update(measurable=complete, interleaving=interleaving, order_errors=order_errors,
        pin_axis_error=pin_error if complete else None, axis_drift=drift,
        dispersion=dispersion, junction_error=junction_error if complete else None)
    return metrics


def region_rank(metrics):
    """No measurements is unknown, never an apparently perfect zero."""
    fields = ('interleaving', 'order_errors', 'pin_axis_error', 'axis_drift', 'dispersion')
    if (not metrics.get('supported') or not metrics.get('measurable')
            or any(not isinstance(metrics.get(key), (int, float))
                   or not isfinite(metrics[key]) for key in fields)):
        return (inf, inf, inf, inf)
    return (metrics['interleaving'] + metrics['order_errors'],
            metrics['pin_axis_error'] + metrics.get('junction_error', 0.0),
            metrics['axis_drift'], metrics['dispersion'])


def select_region_result(results):
    """Choose within the existing 115% / zero-or-plus-one crossing guard."""
    if not results:
        raise ValueError('cannot select an empty region result collection')
    reference = min(results, key=lambda item: item[0])
    wire = reference[6][1]
    limit = wire['wirelength'] * LENGTH_RATIO
    crossing_limit = 0 if wire['crossings'] == 0 else wire['crossings'] + 1
    def measured(item):
        return item[5].get('layout_regions', {}).get('metrics', {})
    baseline = measured(reference)
    comparable = lambda item: (bool(baseline.get('plan_signature'))
        and measured(item).get('plan_signature') == baseline.get('plan_signature')
        and region_rank(measured(item))[0] != inf)
    eligible = [item for item in results if item[6][1]['wirelength'] <= limit + 1e-9
                and item[6][1]['crossings'] <= crossing_limit]
    improved = [item for item in eligible if comparable(item)
                and region_rank(baseline)[0] != inf
                and all(measured(item)[key] <= baseline[key] for key in ('interleaving', 'order_errors'))
                and region_rank(measured(item)) < region_rank(baseline)]
    winner = min(improved, key=lambda item: (*region_rank(measured(item)), *item[0])) if improved else reference
    return winner, dict(policy=SELECTION_VERSION, reference_candidate=reference[8],
        reference_metrics=deepcopy(wire), reference_regions=deepcopy(baseline),
        reference_stage='before_route_simplification', wirelength_ratio=LENGTH_RATIO,
        wirelength_limit=limit, crossing_limit=crossing_limit, selected_candidate=winner[8],
        eligible_candidates=[item[8] for item in eligible],
        incomparable_candidates=[item[8] for item in results if not comparable(item)],
        reason='regions_improved_within_wire_budget' if improved else 'original_quality_no_region_gain',
        ranking=['region_interleaving_and_order', 'physical_pin_and_junction_error',
                 'direct_axis_drift', 'region_dispersion', 'original_quality'])
