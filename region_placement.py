"""Bounded horizontal region seeds and reversible, row-preserving proposals.

The source plan is immutable drawing evidence.  Only leaf owners are hard
blocks; regions and branch identities remain deformable during annealing.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from itertools import islice, permutations, product
import random
from statistics import fmean
import time


ACTIVE = frozenset(('nmos', 'pmos', 'npn', 'pnp'))
MAX_INITIAL_TRIALS = 128
INITIAL_SECONDS = 1.0


def _leaves(plan):
    return [region for region in plan.get('regions', ())
            if not region.get('children') and region.get('members')]


def _local_blocks(comps, index, relations, plan):
    from layout_relations import placement_blocks
    local, _ = placement_blocks(comps, index, relations)
    owner = {name: region['id'] for region in _leaves(plan)
             for name in region.get('members', ())}
    result = []
    for block in local:
        pieces = defaultdict(list)
        for i in block:
            # Ungrouped/external members cannot fuse two region leaves.
            pieces[owner.get(comps[i]['id'], ('outside', i))].append(i)
        result.extend(pieces.values())
    return result


def _region_order(plan):
    leaves = _leaves(plan)
    by_id = {region['id']: region for region in plan.get('regions', ())}
    leaf_ids = {region['id'] for region in leaves}

    def descend(identity):
        if identity in leaf_ids:
            return [identity]
        return [leaf for child in by_id.get(identity, {}).get('children', ())
                for leaf in descend(child)]

    order = []
    for edge in plan.get('edges', ()):
        if edge.get('kind') == 'region_order':
            for identity in edge.get('regions', ()):
                for leaf in descend(identity):
                    if leaf not in order:
                        order.append(leaf)
    order.extend(region['id'] for region in leaves if region['id'] not in order)
    return order


def _lanes(region, row_of, index, axes, mirror, local_blocks=()):
    """Reuse columns across rows, preserving real stacks and pair side hints."""
    ids = [index[name] for name in region['members'] if name in index]
    parent = {i: i for i in ids}
    rows = {i: {row_of[i]} for i in ids}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for axis in axes:
        a, b = index.get(axis.get('a')), index.get(axis.get('b'))
        if a not in parent or b not in parent:
            continue
        a, b = find(a), find(b)
        # The first rollout retains source rows, even when a genuine series
        # edge has two endpoints on one row.  Never create a duplicate cell.
        if a != b and not rows[a] & rows[b]:
            parent[b] = a
            rows[a].update(rows[b])
    groups = defaultdict(list)
    for i in ids:
        groups[find(i)].append(i)
    side = defaultdict(list)
    for pair in region.get('pairs', ()):
        if len(pair) == 2:
            for name, value in zip(pair, (-1, 1)):
                if name in index and index[name] in parent:
                    side[find(index[name])].append(value)
    # Branches provide initial side identity, not a hard same-column edge.
    branches = region.get('branches', ())
    if len(branches) == 2:
        for branch, value in zip(branches, (-1, 1)):
            for name in branch.get('members', ()):
                if name in index and index[name] in parent:
                    side[find(index[name])].append(value)
    desired = {root: fmean(side[root]) if side[root] else 0 for root in groups}
    ordered = sorted(groups, key=lambda root: (-len(rows[root]),
                     -bool(side[root]), desired[root], min(groups[root])))
    width = max(Counter(row_of[i] for i in ids).values(), default=1)
    # Graph colouring by row incidence. Width is bounded by the leaf size.
    while width <= max(1, len(ids)):
        occupied, lanes = set(), {}
        for root in ordered:
            target = (desired[root] + 1) * (width - 1) / 2
            choices = sorted(range(width), key=lambda col: (abs(col-target), col))
            col = next((col for col in choices
                        if all((row_of[i], col) not in occupied for i in groups[root])), None)
            if col is None:
                break
            for i in groups[root]:
                lanes[i] = col
                occupied.add((row_of[i], col))
        else:
            # Lane colouring places series components, while disjoint local
            # owners are the hard units. A dense row can otherwise interleave
            # two pairs whose semantic side hints point toward opposite lanes.
            # Reorder only this leaf's existing row slots, keeping its global
            # band and preserving already coherent rows exactly.
            by_row = defaultdict(list)
            for block in local_blocks:
                parts = defaultdict(list)
                for i in block:
                    if i in lanes:
                        parts[row_of[i]].append(i)
                for row, members in parts.items():
                    by_row[row].append(sorted(members, key=lambda i: lanes[i]))
            for row, owners in by_row.items():
                slots = sorted(lanes[i] for owner in owners for i in owner)
                owners.sort(key=lambda owner: (fmean(lanes[i] for i in owner),
                                               min(lanes[i] for i in owner)))
                ordered_members = [i for owner in owners for i in owner]
                for i, col in zip(ordered_members, slots):
                    lanes[i] = col
            return ({i: width-1-col for i, col in lanes.items()} if mirror < 0 else lanes), width
        width += 1
    raise ValueError('Unable to assign distinct region cells')


def _contiguous(cells, blocks):
    occupied = {cell: i for i, cell in enumerate(cells)}
    for block in blocks:
        members = set(block)
        rows = defaultdict(list)
        for i in block:
            rows[cells[i][0]].append(cells[i][1])
        for row, cols in rows.items():
            if any(occupied.get((row, col)) not in members and (row, col) in occupied
                   for col in range(min(cols), max(cols)+1)):
                return False
    return True


def _pack_seed(comps, nets, index, row_of, plan, blocks, order, mirror):
    leaves = {region['id']: region for region in _leaves(plan)}
    cells = [None] * len(comps)
    start, channels = 0, set()
    for identity in order:
        region = leaves[identity]
        sign = mirror.get(identity, 1) if isinstance(mirror, dict) else mirror
        lanes, width = _lanes(region, row_of, index, plan.get('series_axes', ()), sign, blocks)
        for i, col in lanes.items():
            cells[i] = (row_of[i], start+col)
        channels.add(start+width)
        start += width+1
    cols = max(1, start-1)
    # Sources and passives stay free: place them near connected active members
    # without filling the one-column region channels or splitting a leaf owner.
    adjacent = defaultdict(set)
    for net, endpoints in nets.items():
        members = [index[name] for name, _ in endpoints if name in index]
        for i in members:
            adjacent[i].update(j for j in members if j != i and cells[j] is not None)
    for i, cell in enumerate(cells):
        if cell is not None:
            continue
        target = fmean(cells[j][1] for j in adjacent[i]) if adjacent[i] else (cols-1)/2
        occupied = {cell for cell in cells if cell is not None}
        candidates = sorted(range(cols), key=lambda col: (abs(col-target), col))
        for col in candidates:
            if col in channels or (row_of[i], col) in occupied:
                continue
            # A protected local pair may have an intentional empty slot.
            if any(i not in block and len(values := [cells[j][1] for j in block
                   if cells[j] is not None and cells[j][0] == row_of[i]]) > 1
                   and min(values) < col < max(values) for block in blocks):
                continue
            cells[i] = (row_of[i], col)
            break
        else:
            cells[i] = (row_of[i], cols)
            cols += 1
    if not _contiguous(cells, blocks):
        return None
    return cells, cols


def _physical_axes(devices, plan, structs=None):
    """Deduplicate real pin relations; generic multiway nodes are junctions."""
    from structured_placement import par
    from region_layout import _source_axis_weights
    if structs is None:
        from circuit_structures import analyze_structures
        structs = analyze_structures(devices)
    source_weights = _source_axis_weights(devices, {'structures': structs})
    by_net, axes = defaultdict(list), {}
    for name, dev in devices.items():
        pins = ('d', 's') if dev['cls'] == 'MOS' else ('c', 'e') if dev['cls'] == 'BJT' else ()
        for pin in pins:
            by_net[dev[pin]].append((name, pin.upper()))

    def retain(axis):
        key = tuple(sorted(((axis['a'], axis['pin_a']), (axis['b'], axis['pin_b']))))
        if key not in axes or axes[key]['weight'] < axis['weight']:
            axes[key] = axis

    for axis in plan.get('series_axes', ()):
        row = deepcopy(axis)
        # The plan already preserves the old source coefficient.  A physical
        # relation is replaced once rather than added to its generic source.
        row['weight'] = row.get('weight', 2.5)
        retain(row)
    for net, endpoints in by_net.items():
        if par.power_marker_sym(net) or len(endpoints) != 2:
            continue
        (a, pa), (b, pb) = endpoints
        if a == b:
            continue
        da, db = devices[a], devices[b]
        serial = (pa in ('D', 'C')) != (pb in ('D', 'C'))
        output = pa in ('D', 'C') and pb in ('D', 'C') and da['cls'] == db['cls'] and da['type'] != db['type']
        if serial or output:
            retain(dict(a=a, b=b, pin_a=pa, pin_b=pb, net=net,
                        weight=max(2.5, source_weights.get(frozenset((a, b)), 0)),
                        source='unique_generic_conduction'))
    return list(axes.values())


def _region_choices(plan, variant):
    """Round-robin orders at every independent local mirror template."""
    order = _region_order(plan)
    orders = [order]
    reversible = all(edge.get('reversible', False) for edge in plan.get('edges', ())
                     if edge.get('kind') == 'region_order')
    if reversible:
        reverse = list(reversed(order))
        if reverse not in orders:
            orders.append(reverse)
        for permutation in islice(permutations(order), MAX_INITIAL_TRIALS):
            candidate = list(permutation)
            if candidate not in orders:
                orders.append(candidate)
            # Keep room for independent mirrors even on sources containing
            # several unrelated complete region motifs.
            if len(orders) >= 8:
                break
    if variant == 2:
        orders.reverse()
    # Uniform and one-leaf mirrors are visited before higher-order templates.
    # This also mirrors unordered attachment banks and complete CMFB subcores.
    masks = [tuple([1]*len(order)), tuple([-1]*len(order))]
    masks.extend(tuple(-1 if n == i else 1 for n in range(len(order))) for i in range(len(order)))
    masks.extend(islice(product((1, -1), repeat=len(order)), MAX_INITIAL_TRIALS))
    seen = set()
    for mask in masks:
        if mask in seen:
            continue
        seen.add(mask)
        mirrors = dict(zip(order, mask))
        for region_order in orders:
            yield region_order, mirrors


def _region_pairs(plan, index):
    pairs, seen = [], set()
    for region in _leaves(plan):
        for pair in region.get('pairs', ()):
            if len(pair) == 2 and all(name in index for name in pair):
                identity = frozenset(pair)
                if identity not in seen:
                    seen.add(identity)
                    pairs.append((index[pair[0]], index[pair[1]], 'region_pair'))
    return pairs


def _configure_regions(pl, plan, index, blocks, physical_axes):
    pl.blocks = deepcopy(blocks)
    pl.block_of = {i: n for n, block in enumerate(blocks) for i in block}
    pl.layout_macro_blocks = []
    pl.motion_blocks = pl.blocks
    pl.role_orders = []
    pl.region_plan = deepcopy(plan)
    pl.region_index = dict(index)
    pl.region_series_axes = deepcopy(physical_axes)
    actions = ('region_translate', 'subregion_exchange', 'series_translate', 'group_mirror')
    pl.region_move_counts = {kind: {action: 0 for action in actions} for kind in ('attempts', 'legal')}


def _orient_controls(pl, nets, index):
    from structured_placement import legacy
    fixed = set(getattr(pl, 'fixed', ())) | set(getattr(pl, 'fixed_cells', {}))
    for i, comp in enumerate(pl.comps):
        if i not in fixed and comp['symbol'] in legacy.MIRROR_ORIENTS:
            pin = 'G' if 'G' in comp['nodes'] else 'B'
            peers = [index[name] for name, _ in nets.get(comp['nodes'].get(pin), ())
                     if name != comp['id'] and name in index]
            if peers:
                pl.orient[i] = int(fmean(pl.cell_of[j][1] for j in peers) > pl.cell_of[i][1])


def initialize_regions(devices, comps, nets, index, row_of, structs, seed, variant,
                       plan, *, relations=(), deadline=None, baseline=None, started=None):
    """Return (placement, alignment, pairs, report), or a transactional fallback.

    The caller's old initializer/RNG is untouched until a complete legal seed
    has been found.  No SA-time proposal may grow the resulting grid.
    """
    started = time.perf_counter() if started is None else started
    until = min(started+INITIAL_SECONDS, deadline) if deadline is not None else started+INITIAL_SECONDS
    report = {'status': 'skipped', 'trials': 0, 'max_trials': MAX_INITIAL_TRIALS,
              'seconds_limit': INITIAL_SECONDS, 'channel_columns': 1,
              'packing_rejections': 0, 'fixed_rejections': 0}
    if not plan.get('supported') or not _leaves(plan):
        report['reason'] = 'no_supported_regions'
        return None, report
    blocks = _local_blocks(comps, index, relations, plan)
    physical_axes = _physical_axes(devices, plan, structs)
    from structured_placement import StructuredPlacement, _estimate
    from region_layout import measure_regions, region_rank
    if baseline is None:
        from structured_placement import _initial_placement, dl, legacy
        levels = dl.compute_levels(devices)
        att = legacy.attach_info(devices, levels['net_levels'], set())
        baseline = _initial_placement(devices, comps, nets, index, levels, att,
            structs, seed, variant, row_override=row_of)
    pairs = _region_pairs(plan, index)
    reference = deepcopy(baseline[0])
    _configure_regions(reference, plan, index, blocks, physical_axes)
    baseline_energy, baseline_estimate = _estimate(reference, nets, index, [], pairs)
    baseline_metrics = measure_regions(reference, comps, index, plan)
    baseline_rank = region_rank(baseline_metrics)
    report.update(baseline_metrics=baseline_metrics, baseline_energy=baseline_energy,
                  baseline_rank=list(baseline_rank),
                  baseline_estimated_wirelength=baseline_estimate['estimated_wirelength'],
                  comparison='shared_region_relations_and_original_geometric_estimate',
                  orders_tried=[], mirror_templates=[], candidates=[])
    fixed = set(getattr(baseline[0], 'fixed', ())) | set(getattr(baseline[0], 'fixed_cells', {}))
    best = None
    for region_order, mirrors in islice(_region_choices(plan, variant), MAX_INITIAL_TRIALS):
        if time.perf_counter() >= until:
            report['reason'] = 'initialization_budget'
            break
        report['trials'] += 1
        if region_order not in report['orders_tried']:
            report['orders_tried'].append(list(region_order))
        if mirrors not in report['mirror_templates']:
            report['mirror_templates'].append(dict(mirrors))
        trial = _pack_seed(comps, nets, index, row_of, plan, blocks, region_order, mirrors)
        if trial is None:
            report['packing_rejections'] += 1
            continue
        cells, cols = trial
        if any(cells[i] != baseline[0].cell_of[i] for i in fixed):
            report['fixed_rejections'] += 1
            continue
        pl = StructuredPlacement(comps, random.Random(seed), row_of, cols=cols,
                                 fixed_cells=getattr(baseline[0], 'fixed_cells', {}))
        pl.fixed = set(fixed)
        column_pitch, row_pitch = ((110, 112), (124, 126), (100, 106))[variant]
        pl.column_x = [64+c*column_pitch for c in range(cols)]
        pl.row_y = [64+r*row_pitch for r in range(max(row_of, default=0)+1)]
        _configure_regions(pl, plan, index, blocks, physical_axes)
        orient = [baseline[0].orient[i] if i in fixed else 0 for i in range(len(comps))]
        pl.set_state(cells, orient)
        _orient_controls(pl, nets, index)
        metrics = measure_regions(pl, comps, index, plan)
        rank = region_rank(metrics)
        cost, estimated = _estimate(pl, nets, index, [], pairs)
        # Compare the old seed with exactly the new physical relationships;
        # the superseded folded same-column/outer-pair targets never enter.
        improved = (rank < baseline_rank and all((metrics.get(field) or 0) <=
                    (baseline_metrics.get(field) or 0) for field in ('interleaving', 'order_errors')))
        key = (*rank, cost)
        report['candidates'].append(dict(rank=list(rank), energy=cost,
            estimated_wirelength=estimated['estimated_wirelength'],
            order=report['orders_tried'].index(region_order),
            mirror=report['mirror_templates'].index(mirrors), organization_improved=improved))
        if improved and (best is None or key < best[0]):
            best = (key, pl, list(region_order), dict(mirrors), metrics, cost, estimated)
    report['elapsed_seconds'] = time.perf_counter()-started
    # A budget expiry cannot expose a partially packed or uncertified seed.
    if best is None or time.perf_counter() >= until:
        if time.perf_counter() >= until:
            report['reason'] = 'initialization_budget'
        else:
            report.setdefault('reason', 'no_organization_improvement' if report['candidates'] else 'no_legal_region_seed')
        return None, report
    key, pl, chosen_order, mirrors, metrics, cost, estimated = best
    pl.region_initialization = report
    pl.region_seed_state = (list(pl.cell_of), list(pl.orient))
    pl.region_seed_positions = [pl.pos_of(i) for i in range(len(comps))]
    report.update(status='applied', reason='region_seed', columns=pl.cols,
                  series_axes=deepcopy(physical_axes),
                  region_order=chosen_order, mirrors=mirrors,
                  selected_metrics=metrics, selected_rank=list(key[:-1]), selected_energy=cost,
                  selected_estimated_wirelength=estimated['estimated_wirelength'],
                  elapsed_seconds=time.perf_counter()-started)
    # Same-axis costs come exclusively from validated physical series_axes.
    # Do not send these through the old logical-column misalignment sum too.
    return (pl, [], pairs, report), report


def score_region_placement(pl, index, plan):
    from region_layout import measure_regions
    metrics = measure_regions(pl, pl.comps, index, plan)
    violations = (metrics.get('interleaving') or 0)+(metrics.get('order_errors') or 0)
    empty = metrics.get('dispersion') or 0
    serial, seen = 0.0, set()
    for axis in getattr(pl, 'region_series_axes', plan.get('series_axes', ())):
        a, b = axis.get('a'), axis.get('b')
        pa, pb = axis.get('pin_a'), axis.get('pin_b')
        if a not in index or b not in index or pa not in pl.comps[index[a]]['nodes'] or pb not in pl.comps[index[b]]['nodes']:
            continue
        key = tuple(sorted(((a, pa), (b, pb))))
        if key in seen:
            continue
        seen.add(key)
        serial += axis.get('weight', 2.5)*abs(pl.pin_world(index[a], pa)[0]-
                   pl.pin_world(index[b], pb)[0])/100
    value = 1.25*violations+.15*empty+serial
    return value, {'metrics': metrics, 'weighted_total': value,
                   'weighted_series_pin_error': serial,
                   'weights': {'violations': 1.25, 'dispersion': .15}}


def propose_region_move(pl):
    """A normal cells/orient undo pair; every other state is immutable/derived."""
    plan, index, rng = pl.region_plan, pl.region_index, pl.rng
    before = (list(pl.cell_of), list(pl.orient))
    leaves = _leaves(plan)
    groups = [[index[name] for name in region['members'] if name in index
               and pl.comps[index[name]]['symbol'] in ACTIVE] for region in leaves]
    groups = [group for group in groups if group]
    if not groups:
        return None
    cells = list(pl.cell_of)
    action = rng.randrange(4)
    action_name = ('region_translate', 'subregion_exchange', 'series_translate', 'group_mirror')[action]
    counts = pl.region_move_counts
    counts['attempts'][action_name] += 1
    orient = list(pl.orient)
    if action == 3:
        # Reflect a complete semantic subregion, so interface-connected local
        # cores can change side without independently scrambling their pairs.
        candidates = list(groups)
        for region in plan.get('regions', ()):
            if region.get('children'):
                group = [index[name] for name in region.get('members', ()) if name in index
                         and pl.comps[index[name]]['symbol'] in ACTIVE]
                if group and group not in candidates:
                    candidates.append(group)
        group = rng.choice(candidates)
        fixed = set(getattr(pl, 'fixed', ())) | set(getattr(pl, 'fixed_cells', {}))
        if fixed.intersection(group):
            return None
        lo, hi = min(cells[i][1] for i in group), max(cells[i][1] for i in group)
        for i in group:
            cells[i] = (cells[i][0], lo+hi-cells[i][1])
            if 1-orient[i] in pl.allowed_orientations(i):
                orient[i] = 1-orient[i]
        moves = []
    elif action == 1 and len(groups) > 1:
        a, b = rng.sample(range(len(groups)), 2)
        # Swap separate leaf intervals, even when their row signatures differ.
        delta = min(cells[i][1] for i in groups[b])-min(cells[i][1] for i in groups[a])
        moves = [(groups[a], delta), (groups[b], -delta)]
    else:
        group = rng.choice(groups)
        if action == 2:
            members = set(group)
            adjacency = defaultdict(set)
            for axis in plan.get('series_axes', ()):
                a, b = index.get(axis.get('a')), index.get(axis.get('b'))
                if a in members and b in members:
                    adjacency[a].add(b)
                    adjacency[b].add(a)
            if not adjacency:
                return None
            todo, selected = [rng.choice(sorted(adjacency))], set()
            while todo:
                i = todo.pop()
                if i not in selected:
                    selected.add(i)
                    todo.extend(adjacency[i]-selected)
            group = sorted(selected)
        lo, hi = min(cells[i][1] for i in group), max(cells[i][1] for i in group)
        shift = rng.randint(-lo, pl.cols-1-hi)
        moves = [(group, shift)]
    if action != 3 and not any(shift for _, shift in moves):
        return None
    fixed = set(getattr(pl, 'fixed', ())) | set(getattr(pl, 'fixed_cells', {}))
    for group, shift in moves:
        if fixed.intersection(group):
            return None
        for i in group:
            cells[i] = (cells[i][0], cells[i][1]+shift)
    if (any(not 0 <= col < pl.cols for _, col in cells)
            or len(set(cells)) != len(cells)):
        return None
    if cells == before[0] and orient == before[1]:
        return None
    pl.set_state(cells, orient)
    if not pl.blocks_contiguous() or (hasattr(pl, 'valid_state') and not pl.valid_state()):
        pl.set_state(*before)
        return None
    counts['legal'][action_name] += 1
    return before
