"""Organize the active frame before SA; keep ownership separate from relations.

No circuit-name rules, device-count thresholds, electrical contraction or A*.
Confirmed continuation paths are lanes. Regions own lanes; pairs, shared nets
and module interfaces remain references rather than transitive rigid unions.
"""
from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
import json
from statistics import median
from time import perf_counter

ACTIVE = frozenset(('nmos', 'pmos', 'npn', 'pnp'))
CONTROL = frozenset(('G', 'B', 'IN+', 'IN-'))
CHANNEL = frozenset(('D', 'S', 'C', 'E'))
VERSION = 'branch-organization-v1'


def _identity(prefix, members):
    return prefix + ':' + '|'.join(sorted(members))


def build_plan(par, pl, comps, nets, index, devices, structs=(), *, bridges=(), source_devices=None):
    from netlist2schematic.plugins.columns.column_layout import build_column_plan
    from netlist2schematic.plugins.region.region_layout import build_region_plan
    from netlist2schematic.plugins.layout_shared.layout_evidence import build_layout_evidence

    columns = build_column_plan(par, pl, comps, nets, index, structs, inline_bridges=bridges)
    lanes = deepcopy(columns['columns'])
    lane_by_id = {row['id']: row for row in lanes}
    lane_of = {name: row['id'] for row in lanes for name in row['members']}
    source = source_devices if source_devices is not None else devices
    evidence = build_layout_evidence(source)
    complete = build_region_plan(source, evidence=evidence)
    regions, owner, skipped = [], {}, []

    def add(names, kind, basis):
        names = set(names) & set(lane_of)
        keys = {lane_of[n] for n in names}
        if not keys:
            return
        expanded = {n for k in keys for n in lane_by_id[k]['members']}
        existing = {owner[k] for k in keys if k in owner}
        if existing:
            if len(existing) != 1 or any(k not in owner for k in keys):
                skipped.append(dict(kind=kind, members=sorted(names), reason='overlapping_region_evidence'))
            return
        identity = _identity('region', expanded)
        regions.append(dict(id=identity, kind=kind, members=sorted(expanded),
                            lanes=sorted(keys), evidence=deepcopy(basis)))
        owner.update({k: identity for k in keys})

    # Full terminal-role recognizers supply optional stronger evidence. A lane
    # disputed by two leaves is never assigned by iteration order.
    leaves = [r for r in complete['regions'] if not r.get('children')]
    claims = defaultdict(set)
    for r in leaves:
        for name in r['members']:
            if name in lane_of:
                claims[lane_of[name]].add(r['id'])
    for r in leaves:
        names = [n for n in r['members'] if n in lane_of and len(claims[lane_of[n]]) == 1]
        add(names, r['kind'], dict(kind='complete_terminal_roles', source=r['id']))
    for lane, values in claims.items():
        if len(values) > 1:
            skipped.append(dict(lane=lane, reason='lane_crosses_recognized_leaves', regions=sorted(values)))

    priority = {'latch': 0, 'folded_core': 1, 'cross_coupled': 2,
                'differential_pair': 3, 'bjt_differential_pair': 3,
                'current_mirror': 4, 'bjt_current_mirror': 4}
    relations = list(structs) + list(evidence.get('relations', ()))
    for row in sorted(relations, key=lambda r: (priority.get(r['kind'], 9), r.get('id', ''))):
        kind = row['kind']
        if kind not in priority or row.get('confidence', 'high') != 'high':
            continue
        names = row.get('members', row.get('devices', ()))
        # A multi-output mirror is an interface fanout, not an arbitrary series
        # of paired regions. Preserve its original recognition record.
        if 'mirror' in kind and len(names) != 2:
            skipped.append(dict(kind=kind, members=list(names), reason='shared_reference_interface'))
            continue
        add(names, kind, dict(kind='confirmed_local_structure', source=row.get('id')))
    for cohort in columns.get('corresponding_paths', ()):
        add([n for path in cohort for n in path], 'corresponding_stacks',
            dict(kind='repeated_multilevel_controls'))
    for lane in lanes:
        if lane['id'] not in owner:
            add(lane['members'], 'conduction_stack' if len(lane['members']) > 1 else 'unassigned_branch',
                dict(kind='confirmed_continuation' if len(lane['members']) > 1 else 'singleton_no_function_claim'))

    device_owner = {n: owner[k] for n, k in lane_of.items()}
    weights = defaultdict(float)
    interfaces = []
    for net, terminals in sorted(nets.items()):
        if par.power_marker_sym(net):
            continue
        pins = [(n, p) for n, p in terminals if n in device_owner]
        endpoints = sorted({device_owner[n] for n, _ in pins})
        if len(endpoints) < 2:
            continue
        local = defaultdict(float)
        for k, (a, pa) in enumerate(pins):
            for b, pb in pins[k+1:]:
                ra, rb = device_owner[a], device_owner[b]
                if ra == rb:
                    continue
                # Common gate/bias/clock fanout alone does not pull regions
                # together. Real channel and channel-to-control ports do.
                w = 4. if pa in CHANNEL and pb in CHANNEL else (
                    2. if (pa in CHANNEL and pb in CONTROL or pb in CHANNEL and pa in CONTROL) else 0.)
                if w:
                    key = tuple(sorted((ra, rb)))
                    local[key] = max(local[key], w / max(1, len(endpoints)-1))
        for key, value in local.items():
            weights[key] += value
        interfaces.append(dict(net=net, terminals=[list(p) for p in pins], regions=endpoints,
                               ordering_evidence=bool(local)))
    # Explicit parent/child region order is a hint, never a second owner tree.
    hints = []
    by_complete = {r['id']: r for r in complete['regions']}
    def descend(identity):
        row = by_complete.get(identity, {})
        if row.get('children'):
            return [n for child in row['children'] for n in descend(child)]
        return row.get('members', ())
    for edge in complete.get('edges', ()):
        if edge.get('kind') == 'region_order':
            for identity in edge.get('regions', ()):
                for n in descend(identity):
                    key = device_owner.get(n)
                    if key and key not in hints:
                        hints.append(key)
    plan = dict(version=VERSION, supported=bool(columns['edges'] or any(len(r['lanes']) > 1 for r in regions)),
        lanes=lanes, regions=regions, ownership=device_owner, continuation_edges=columns['edges'],
        interfaces=interfaces, region_edges=[dict(a=a, b=b, weight=w) for (a, b), w in sorted(weights.items())],
        order_hints=hints, skipped=skipped, original_rows={n: pl.cell_of[index[n]][0] for n in lane_of},
        terminal_contract={c['id']: dict(symbol=c['symbol'], nodes=deepcopy(c['nodes'])) for c in comps},
        coverage=dict(active_devices=len(lane_of), column_devices=sum(len(r['members']) for r in lanes if len(r['members']) > 1),
                      local_regions=sum(r['kind'] != 'unassigned_branch' for r in regions),
                      unassigned_devices=[n for r in regions if r['kind'] == 'unassigned_branch' for n in r['members']]),
        scope='all_circuit_sizes_topology_only')
    plan['signature'] = sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return plan


def measure(pl, index, plan, *, pins=True):
    cells = {n: pl.cell_of[index[n]] for n in plan['ownership'] if n in index}
    broken = sum(len({cells[n][1] for n in lane['members'] if n in cells}) > 1 for lane in plan['lanes'])
    by_row = defaultdict(list)
    for n, (row, col) in cells.items():
        by_row[row].append((col, plan['ownership'][n]))
    interleaving, directions, scatter = 0, defaultdict(set), 0.
    for values in by_row.values():
        ranges = defaultdict(list)
        for col, owner in sorted(values):
            ranges[owner].append(col)
        for owner, columns in ranges.items():
            scatter += max(columns)-min(columns)+1-len(columns)
            interleaving += sum(min(columns) < col < max(columns) and other != owner for col, other in values)
        owners = sorted(ranges)
        for k, a in enumerate(owners):
            for b in owners[k+1:]:
                if max(ranges[a]) < min(ranges[b]):directions[a, b].add(1)
                elif max(ranges[b]) < min(ranges[a]):directions[a, b].add(-1)
    inversions = sum(len(sides) > 1 for sides in directions.values())
    # Pairwise consistency alone misses A<B, B<C, C<A across three rows.
    adj, indegree = defaultdict(set), {r['id']: 0 for r in plan['regions']}
    for (a, b), sides in directions.items():
        for side in sides:
            u, v = (a, b) if side == 1 else (b, a)
            if v not in adj[u]:adj[u].add(v);indegree[v] += 1
    queue = [n for n, d in indegree.items() if not d]; visited = 0
    while queue:
        n = queue.pop();visited += 1
        for other in adj[n]:
            indegree[other] -= 1
            if not indegree[other]:queue.append(other)
    cyclic = visited != len(indegree)
    error = 0.
    if pins:
        for edge in plan['continuation_edges']:
            if edge['a'] in index and edge['b'] in index:
                error += abs(pl.pin_world(index[edge['a']], edge['pin_a'])[0]-
                             pl.pin_world(index[edge['b']], edge['pin_b'])[0])
    return dict(column_breaks=broken, region_interleaving=interleaving, order_reversals=inversions,
                cyclic_region_order=int(cyclic), region_holes=scatter, true_pin_error=error,
                active_devices=len(cells), covered_column_devices=plan['coverage']['column_devices'])


def rank(metrics):
    return (metrics['column_breaks'], metrics['region_interleaving'],
            metrics['order_reversals']+metrics['cyclic_region_order'], metrics['region_holes'])


def valid(pl):
    plan = getattr(pl, 'branch_plan', None)
    if not plan:
        return True
    index = getattr(pl, 'branch_index', None) or {c['id']: i for i, c in enumerate(pl.comps)}
    if any(n not in index or pl.cell_of[index[n]][0] != row for n, row in plan['original_rows'].items()):
        return False
    m = measure(pl, index, plan, pins=False)
    return not any(m[k] for k in ('column_breaks', 'region_interleaving', 'order_reversals', 'cyclic_region_order'))


def _orders(pl, index, plan):
    regions = plan['regions']
    centers = {r['id']: median(pl.cell_of[index[n]][1] for n in r['members']) for r in regions}
    baseline = sorted(centers, key=lambda r: (centers[r], r))
    choices = [baseline]
    hints = plan['order_hints']
    if hints:choices.insert(0, hints+[r for r in baseline if r not in hints])
    # A weighted graph traversal is a generic fallback when no full circuit
    # architecture is known. Adjacent switches then reduce its bandwidth.
    graph = defaultdict(dict)
    for e in plan['region_edges']:
        graph[e['a']][e['b']] = e['weight'];graph[e['b']][e['a']] = e['weight']
    pending, walk = set(baseline), []
    while pending:
        root = min(pending, key=lambda n: (len(graph[n]), centers[n], n))
        stack = [root]
        while stack:
            n = stack.pop()
            if n not in pending:continue
            pending.remove(n);walk.append(n)
            stack.extend(sorted((m for m in graph[n] if m in pending),
                key=lambda m: (graph[n][m], -centers[m], m)))
    choices.append(walk)
    def cost(order):
        pos = {n: k for k, n in enumerate(order)}
        return sum(e['weight']*abs(pos[e['a']]-pos[e['b']]) for e in plan['region_edges'])
    for initial in list(choices):
        order = list(initial)
        for _ in range(4):
            changed = False
            for k in range(len(order)-1):
                trial = list(order);trial[k], trial[k+1] = trial[k+1], trial[k]
                if cost(trial) < cost(order)-1e-9:order=trial;changed=True
            if not changed:break
        choices.append(order)
    seen = set()
    for order in choices:
        for reverse in (False, True):
            value = tuple(reversed(order)) if reverse else tuple(order)
            if value not in seen:seen.add(value);yield value


def _pack(pl, index, plan, order, mirror):
    by_id = {r['id']: r for r in plan['regions']}
    lanes = {r['id']: r for r in plan['lanes']}
    cells = list(pl.cell_of);right = defaultdict(lambda: -1)
    for owner in order:
        region = by_id[owner]
        keys = sorted(region['lanes'], key=lambda k: (median(pl.cell_of[index[n]][1] for n in lanes[k]['members']), k), reverse=mirror)
        for key in keys:
            ids = [index[n] for n in lanes[key]['members']]
            col = max(right[pl.cell_of[i][0]]+1 for i in ids)
            for i in ids:
                row = pl.cell_of[i][0];cells[i] = (row, col);right[row] = col
    occupied = {cells[index[n]] for n in plan['ownership']}
    for i, comp in enumerate(pl.comps):
        if comp['id'] in plan['ownership']:continue
        row, old = cells[i]
        col = min((c for c in range(max(pl.cols, max(right.values(), default=0)+3)) if (row, c) not in occupied),
                  key=lambda c: (abs(c-old), c))
        cells[i] = (row, col);occupied.add(cells[i])
    return cells


def initialize(par, pl, comps, nets, index, devices, structs, alignment, *, bridges=(),
               source_devices=None, options=None, deadline=None, seconds=.5, max_trials=64):
    from netlist2schematic.plugins.preflight.placement_preflight import _static_faults, _wire_estimate
    from netlist2schematic.routing.routing_rules import RoutingOptions
    from netlist2schematic.routing.orthogonal_router import RoutingFailure
    from netlist2schematic.layout.series_alignment import _fixed_ids
    started = perf_counter();stop = min(deadline, started+seconds) if deadline else started+seconds
    plan = build_plan(par, pl, comps, nets, index, devices, structs, bridges=bridges, source_devices=source_devices)
    report = dict(version=VERSION, policy='auto', plan=plan, applied=False, trials=0, actions=[],
                  seconds_limit=seconds, max_trials=max_trials, before=measure(pl, index, plan))
    best = None; fixed = _fixed_ids(pl, comps)
    reason = 'no_supported_relations' if not plan['supported'] else 'no_legal_seed'
    options = options or RoutingOptions()
    # Attachment estimator edges are layout affinities, never additional nets
    # in the electrical/geometric scene (they can reuse real terminals).
    real_nets = defaultdict(list)
    for comp in comps:
        for pin, net in comp['nodes'].items():real_nets[net].append((comp['id'],pin))
    wire = _wire_estimate(pl, nets, index, par)
    if plan['supported'] and seconds > 0 and max_trials > 0:
        for order in _orders(pl, index, plan):
            for mirror in (False, True):
                if perf_counter() >= stop or report['trials'] >= max_trials:
                    reason = 'time_budget' if perf_counter() >= stop else 'trial_limit';break
                report['trials'] += 1
                cells = _pack(pl, index, plan, order, mirror)
                if len(set(cells)) != len(cells) or any(cells[i] != pl.cell_of[i] for i in fixed):continue
                trial = deepcopy(pl)
                trial.cols = max(pl.cols, max(c for _, c in cells)+2)
                pitch = median(b-a for a, b in zip(pl.column_x, pl.column_x[1:])) if len(pl.column_x)>1 else 110.
                while len(trial.column_x) < trial.cols:trial.column_x.append(trial.column_x[-1]+pitch)
                trial.set_state(cells, trial.orient)
                trial.branch_plan = deepcopy(plan);trial.branch_index = dict(index)
                if not valid(trial) or not trial.blocks_contiguous():continue
                estimated = _wire_estimate(trial, nets, index, par)
                if estimated > wire*1.25+1e-8:continue
                measured = measure(trial, index, plan)
                key = rank(measured)+(estimated, max(c for _, c in cells), tuple(cells))
                if best is not None and key >= best[0]:continue
                try:
                    faults = _static_faults(par, trial, real_nets, index, options, stop)
                except RoutingFailure as exc:
                    if exc.diagnostics.get('reason') != 'quality_time_budget':raise
                    reason='time_budget';break
                if faults['count'] or faults['hard']:continue
                best = (key, trial, order, mirror)
            if perf_counter() >= stop or report['trials'] >= max_trials:break
    current = pl
    # A legal old seed remains a candidate, but must satisfy the same contracts.
    baseline = deepcopy(pl);baseline.branch_plan = deepcopy(plan);baseline.branch_index = dict(index)
    baseline_ok = plan['supported'] and valid(baseline) and baseline.blocks_contiguous()
    baseline_key = rank(report['before'])+(wire, max(c for _, c in pl.cell_of), tuple(pl.cell_of))
    if best is not None and (not baseline_ok or best[0] < baseline_key):
        _, current, order, mirror = best
        report.update(order=list(order), local_mirror=mirror, stop_reason='organized')
    elif baseline_ok and seconds > 0 and max_trials > 0:
        current=baseline;report['stop_reason']='existing_seed_already_organized'
    else:report['stop_reason']=reason
    report['applied'] = bool(getattr(current, 'branch_plan', None))
    if report['applied']:
        # Filter misleading folded/fork affinities only in the new mode. The
        # existing weights are retained for confirmed continuation relations.
        confirmed = {frozenset((e['a'], e['b'])) for e in plan['continuation_edges']}
        alignment = [(a, b, w) for a, b, w in alignment
                     if comps[a]['symbol'] not in ACTIVE or comps[b]['symbol'] not in ACTIVE
                     or frozenset((comps[a]['id'], comps[b]['id'])) in confirmed]
        for e in plan['continuation_edges']:
            a, b = index[e['a']], index[e['b']]
            if not any({a,b} == {x,y} for x,y,w in alignment):alignment.append((a,b,3.))
        report['actions'] = [dict(device=c['id'], before=list(pl.cell_of[i]), after=list(current.cell_of[i]))
                             for i,c in enumerate(comps) if pl.cell_of[i] != current.cell_of[i]]
    report.update(current=measure(current, index, plan), estimated_wire_before=wire,
                  estimated_wire_after=_wire_estimate(current, nets, index, par),
                  elapsed_seconds=round(perf_counter()-started,6))
    current.branch_report = deepcopy(report)
    return current, alignment, report


def propose(pl):
    before = (list(pl.cell_of), list(pl.orient));rng = pl.rng
    index = pl.branch_index;plan = pl.branch_plan
    units = [r['members'] for r in plan['lanes']] + [r['members'] for r in plan['regions']]
    names = rng.choice(units);ids = {index[n] for n in names}
    cells = list(pl.cell_of)
    if rng.random() < .7:
        shift = rng.choice((-2,-1,1,2))
        for i in ids:cells[i] = (cells[i][0], cells[i][1]+shift)
    else:
        other = {index[n] for n in rng.choice(units)}
        if ids & other:return None
        a = round(median(cells[i][1] for i in ids));b = round(median(cells[i][1] for i in other))
        if a == b:return None
        for group, delta in ((ids,b-a),(other,a-b)):
            for i in group:cells[i] = (cells[i][0], cells[i][1]+delta)
    from netlist2schematic.layout.series_alignment import _fixed_ids
    fixed = _fixed_ids(pl,pl.comps)
    if (any(not 0 <= col < pl.cols for row,col in cells) or len(set(cells)) != len(cells)
            or any(cells[i] != before[0][i] for i in fixed)):return None
    pl.set_state(cells,pl.orient)
    if not pl.blocks_contiguous():pl.set_state(*before);return None
    return before


def refresh(pl, comps, index, data, *, stage):
    report = data.get('layout_branches')
    if not isinstance(report,dict) or not report.get('plan'):return data
    data=deepcopy(data);report=data['layout_branches']
    report['current']=measure(pl,index,report['plan']);report['measurement_stage']=stage
    return data


def compatible(source, target, index):
    plan = getattr(target, 'branch_plan', None)
    if not plan:return True
    other = getattr(source,'branch_plan',None)
    return bool(other and other['signature']==plan['signature'] and valid(source))


def select_results(results):
    """Protect confirmed frames, then retain bends/crossings/length ordering."""
    from netlist2schematic.flow.candidate_selection import select_result
    eligible=[];excluded=[]
    for row in results:
        report=row[5].get('layout_branches',{})
        if report.get('applied') and any(measure(row[1],row[4],report['plan'])[k]
                for k in ('column_breaks','region_interleaving','order_reversals','cyclic_region_order')):
            excluded.append(row[8])
        else:eligible.append(row)
    if not eligible:
        raise ValueError('All routed candidates violate their saved branch organization contracts')
    chosen, report = select_result(eligible,stage='after_branch_organization_and_cleanup')
    report.update(branch_organization=VERSION,structure_excluded_candidates=excluded,
                  no_device_count_threshold=True)
    return chosen,report
