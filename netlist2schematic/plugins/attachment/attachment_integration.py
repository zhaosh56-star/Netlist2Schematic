"""Optional, transactional junction slots for attachments inside active groups.

The source plan is immutable.  Initialisation may insert display columns (and,
for a multi-path attachment, a necessary display row); annealing only changes
cells and orientations.  Attachment membership never joins active ownership.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
import json
from math import isfinite
from statistics import median
from time import perf_counter

from netlist2schematic.layout.passive_placement import FlexiblePassivePlacement
from netlist2schematic.layout.structured_placement import StructuredPlacement
from netlist2schematic.plugins.readability.readability_layout import (build_readability_plan, measure_readability,
                                junction_point)

VERSION = 'attachment-integration-v43'
ACTIVE = frozenset(('nmos', 'pmos', 'npn', 'pnp'))
PASSIVE = frozenset(('resistor', 'capacitor', 'inductor', 'diode'))
EPS = 1e-8


def new_attachment_budget(*, seconds=1.0, max_candidates=128):
    if (type(seconds) not in (int, float) or not isfinite(seconds)
            or not 0 <= seconds <= 1 or type(max_candidates) is not int
            or not 0 <= max_candidates <= 128):
        raise ValueError('attachment budget requires 0..1 seconds and 0..128 candidates')
    return dict(seconds_limit=float(seconds), max_candidates=max_candidates,
                spent_seconds=0.0, candidates=0)


def _begin_budget(budget, deadline):
    budget = new_attachment_budget() if budget is None else budget
    for key in ('seconds_limit', 'max_candidates', 'spent_seconds', 'candidates'):
        if key not in budget:
            raise ValueError('incomplete attachment budget')
    new_attachment_budget(seconds=budget['seconds_limit'], max_candidates=budget['max_candidates'])
    if (type(budget['spent_seconds']) not in (int, float) or not isfinite(budget['spent_seconds'])
            or budget['spent_seconds'] < 0 or type(budget['candidates']) is not int or budget['candidates'] < 0):
        raise ValueError('invalid attachment budget usage')
    remaining = 0.0 if budget.get('exhausted') else max(0.0, budget['seconds_limit']-budget['spent_seconds'])
    started = perf_counter()
    until = started+remaining
    if deadline is not None:
        until = min(until, deadline)
    return budget, started, until


def _end_budget(budget, started):
    # A single geometric validation may finish just after its deadline. Keep
    # the actual time visible; a later stage receives no fresh allowance.
    budget['spent_seconds'] += max(0.0, perf_counter()-started)
    budget.pop('candidate_ceiling', None)


def _candidate_limit(budget):
    return min(budget['max_candidates']-budget.get('band_compaction_reserve',0)
              -budget.get('tail_transistor_reserve',0)-budget.get('shunt_bank_reserve',0)
              -budget.get('series_path_reserve',0)-budget.get('path_bank_reserve',0)
              -budget.get('regulation_reserve',0)-budget.get('stage_finish_reserve',0)
              -budget.get('stack_column_reserve',0)-budget.get('control_shunt_reserve',0)
              -budget.get('tail_source_axis_reserve',0),
              budget.get('candidate_ceiling',budget['max_candidates']),
              budget.get('stage_candidate_ceiling',budget['max_candidates']))


def _has_budget(budget, until):
    return not budget.get('exhausted') and perf_counter() < until and budget['candidates'] < _candidate_limit(budget)


def _reserve_control_finish(plan,budget):
    count=len(plan.get('control_nodes',{}).get('nodes',()))
    if count:
        reserve=min(24,budget['max_candidates']//4,8*count+8)
        budget['control_finish_reserve']=reserve
        budget['candidate_ceiling']=max(budget['candidates'],budget['max_candidates']-reserve
                                        -budget.get('band_compaction_reserve',0))


def build_attachment_plan(devices, comps, nets, index, structs, levels, row_of, *, policy='passive',
                          formal_ports=()):
    """Compile real terminal roles; no placement or random state is changed."""
    if policy not in ('passive', 'all'):
        raise ValueError('attachment policy must be passive or all')
    readable = build_readability_plan(devices, comps, nets, index, structs, levels, row_of)
    from netlist2schematic.plugins.attachment.attachment_cores import compile_cores, plan_core_rows
    readable, core_rows = plan_core_rows(devices, comps, index, levels, readable,
        compile_cores(comps, structs, readable['junctions']))
    from netlist2schematic.layout.structured_placement import _relations
    _, _, blocks = _relations(devices, structs, index, [readable['display_rows'][c['id']] for c in comps])
    from netlist2schematic.plugins.attachment.attachment_input_blocks import compile_input_blocks, owner_blocks
    from netlist2schematic.plugins.attachment.attachment_outputs import compile_output_groups
    input_blocks=compile_input_blocks(devices,comps,structs,readable['display_rows'],formal_ports=formal_ports)
    output_merges=compile_output_groups(comps,readable['display_rows'],input_blocks)
    repeated_stacks={'groups':[],'applicable_ids':[],'rows_applied':False,'status':'policy_disabled'}
    if policy=='all':
        from netlist2schematic.plugins.attachment.attachment_stacks import plan_stack_rows
        readable,repeated_stacks=plan_stack_rows(devices,comps,index,levels,readable,core_rows,
            [[comps[i]['id'] for i in block] for block in blocks]+
            [p['members'] for p in input_blocks['pairs']]+[g['members'] for g in output_merges['groups']])
        if repeated_stacks['rows_applied']:
            _,_,blocks=_relations(devices,structs,index,[readable['display_rows'][c['id']] for c in comps])
            input_blocks=compile_input_blocks(devices,comps,structs,readable['display_rows'],formal_ports=formal_ports)
            output_merges=compile_output_groups(comps,readable['display_rows'],input_blocks)
    rows = readable['display_rows']
    owners = [sorted(comps[i]['id'] for i in block) for block in blocks]
    protected = [{comps[i]['id'] for i in b} for b in blocks if len(b)>1]
    mos_conflicts = [p for p in input_blocks['pairs'] if p.get('kind')=='differential_pair'
        and any(set(p['members']) & block for block in protected)]
    input_blocks['skipped'].extend(dict(id=p['id'], reason='preexisting_MOS_owner') for p in mos_conflicts)
    input_blocks['pairs'] = [p for p in input_blocks['pairs'] if p not in mos_conflicts]
    conflicts = [g for g in output_merges['groups'] if any(set(g['members']) & b for b in protected)]
    output_merges['skipped'].extend(dict(id=g['id'],reason='preexisting_output_owner') for g in conflicts)
    output_merges['groups'] = [g for g in output_merges['groups'] if g not in conflicts]
    leaves = list(input_blocks['pairs'])+list(output_merges['groups'])
    new_owners = owner_blocks(blocks, comps, dict(input_blocks,pairs=leaves))
    if new_owners is not None:
        owners = new_owners
    elif input_blocks['pairs']:
        input_blocks['skipped'].append(dict(reason='existing_owner_overlap',
            pairs=[p['id'] for p in input_blocks['pairs']]))
        input_blocks.update(pairs=[], banks=[], tails=[])
        output_merges['skipped'].extend(dict(id=g['id'],reason='existing_owner_overlap') for g in output_merges['groups'])
        output_merges['groups'] = []
    from netlist2schematic.plugins.attachment.attachment_folded import compile_folded,owner_lists
    folded_cores=compile_folded(devices,comps,structs,rows,owners)
    owners=owner_lists(owners,folded_cores)
    from netlist2schematic.plugins.attachment.attachment_stages import compile_stages
    conduction_stages=compile_stages(comps,readable['junctions'],rows,owners,formal_ports=formal_ports)
    if conduction_stages['global_layout_eligible']:
        taken=set(conduction_stages['ownership'])
        owners=[o for o in owners if not set(o)&taken]+[c['members'] for c in conduction_stages['stages']]
    # Unique physical continuation only. A common gate, a fork and a tail
    # shared by two sources do not establish one geometric current axis.
    host_axes, neighbours = [], defaultdict(set)
    by_id = {c['id']: c for c in comps}
    for net, junction in readable['junctions'].items():
        ports = junction['conduction_ports']
        if junction['is_rail'] or len(ports) != 2 or ports[0][0] == ports[1][0]:
            continue
        (a, pa), (b, pb) = ports
        drain = ('D', 'C')
        source = ('S', 'E')
        direct = (pa in drain and pb in source) or (pb in drain and pa in source)
        complementary = (pa in drain and pb in drain
            and {by_id[a]['symbol'], by_id[b]['symbol']} in ({'nmos', 'pmos'}, {'npn', 'pnp'}))
        if direct or complementary:
            host_axes.append(dict(a=a, pin_a=pa, b=b, pin_b=pb, net=net))
            neighbours[a].add(b); neighbours[b].add(a)
    # At a proven folded fork, input-D is side access while bias-D/folded-S
    # retain the main branch identity. Do not align the input to that axis.
    axis_keys={frozenset(((a['a'],a['pin_a']),(a['b'],a['pin_b']))) for a in host_axes}
    for core in folded_cores['groups']:
        if not core.get('rail_supply_derived'):continue
        for bias,fold in zip(core['bias'],core['folded']):
            key=frozenset(((bias,'D'),(fold,'S')))
            if key not in axis_keys:
                host_axes.append(dict(a=bias,pin_a='D',b=fold,pin_b='S',net=by_id[bias]['nodes']['D'],
                    evidence='verified_RL_fed_folded_continuation',core=core['id']))
                axis_keys.add(key)
    lanes, seen = [], set()
    for name in sorted(neighbours):
        if name in seen:
            continue
        pending, lane = [name], set()
        while pending:
            node = pending.pop()
            if node in lane:
                continue
            lane.add(node); pending.extend(neighbours[node]-lane)
        seen.update(lane)
        lanes.append(sorted(lane))
    groups = []
    # A parent bridge owns its paths for proposals, without making a rigid block.
    for pattern in readable['patterns']:
        if pattern.get('parent') or not pattern.get('owner_junctions'):
            continue
        group = deepcopy(pattern)
        ports = sorted({tuple(port) for net in group['owner_junctions']
                        for port in readable['junctions'][net]['active_ports']})
        hosts = sorted({name for name, _ in ports})
        group.update(host_ports=[list(p) for p in ports], host_members=hosts,
            host_blocks=[block for block in owners if set(block) & set(hosts)],
            host_lanes=[lane for lane in lanes if set(lane) & set(hosts)],
            confidence='high', requires_interior=(len(group['owner_junctions']) >= 2
                and group['kind'] in ('series_chain', 'multi_path_bridge', 'two_arm_join',
                                      'single_attachment', 'parallel_bank')))
        groups.append(group)
    from netlist2schematic.plugins.attachment.attachment_port_banks import compile_banks
    port_banks = compile_banks(comps, groups)
    bank_ids = {bank['group'] for bank in port_banks['banks']}
    for group in groups:
        if group['id'] in bank_ids:
            group.update(requires_interior=False, row_mode='same_host_port_bank')
    active_rows = {c['id']: rows[c['id']] for c in comps if c['symbol'] in ACTIVE}
    active_bands = set(active_rows.values())
    reserved_rows = sorted({rows[c['id']] for c in comps if c['symbol'] in PASSIVE
                            and rows[c['id']] not in active_bands})
    source_model = None
    source_groups = []
    if policy == 'all':
        from netlist2schematic.plugins.attachment.attachment_sources import build_source_groups
        source_model = build_source_groups(devices, comps, nets, index, display_rows=rows)
        for source_group in source_model['groups']:
            source_group = deepcopy(source_group)
            hosts = set(source_group['host_members'])
            source_group.update(host_blocks=[block for block in owners if set(block) & hosts],
                host_lanes=[], requires_interior=False)
            source_groups.append(source_group)
    source_managed = sorted({n for g in source_groups for n in g['members']})
    plan = dict(version=VERSION, policy=policy,
        supported=bool(groups or source_groups or core_rows['cores'] or input_blocks['pairs'] or folded_cores['groups']), groups=groups,
        core_rows=core_rows, input_blocks=input_blocks, output_merges=output_merges,folded_cores=folded_cores,
        port_banks=port_banks,conduction_stages=conduction_stages,repeated_stacks=repeated_stacks,
        readability_plan=readable, branch_row_plan=deepcopy(readable.get('branch_row_plan')),
        display_rows=deepcopy(rows), active_rows=active_rows,
        coverage=deepcopy(readable['coverage']), sources=[], host_axes=host_axes,
        reserved_passive_rows=reserved_rows,
        preserve_feedback_rows=any(s['kind'] == 'feedback_network' for s in structs))
    plan.update(source_model=source_model, source_groups=source_groups,
                source_managed_members=source_managed)
    from netlist2schematic.plugins.attachment.attachment_access import build_access_links
    plan['host_access'] = build_access_links(comps, plan)
    from netlist2schematic.plugins.attachment.attachment_pairs import build_paired_banks
    plan['paired_banks'] = build_paired_banks(comps, structs, plan)
    from netlist2schematic.plugins.attachment.attachment_taps import compile_taps
    plan['conduction_taps'] = compile_taps(comps,plan)
    from netlist2schematic.plugins.attachment.attachment_channels import compile_channels
    plan['parallel_channels'] = compile_channels(comps, plan)
    from netlist2schematic.plugins.attachment.attachment_control_nodes import compile_nodes
    plan['control_nodes']=compile_nodes(comps,plan)
    # A connection from a shared control hub into a multi-host conduction
    # junction is a port connection, not a two-branch interior stripe.
    returns={r['group'] for node in plan['control_nodes']['nodes'] for r in node['returns']}
    for group in plan['groups']:
        if group['id'] in returns:
            group.update(row_mode='control_connection',requires_interior=False)
    from netlist2schematic.plugins.attachment.attachment_controllers import compile_controllers
    plan['controller_clusters'] = compile_controllers(comps, plan)
    from netlist2schematic.plugins.attachment.attachment_tail_transistors import compile_tails
    plan['tail_transistors'] = compile_tails(devices,comps,structs,rows,policy=policy)
    from netlist2schematic.plugins.attachment.attachment_shunt_banks import compile_banks as compile_shunts
    plan['shunt_banks'] = compile_shunts(comps, plan)
    from netlist2schematic.plugins.attachment.attachment_series_paths import compile_paths
    plan['physical_series_paths'] = compile_paths(comps, plan)
    from netlist2schematic.plugins.attachment.attachment_path_banks import compile_banks as compile_path_banks
    plan['physical_path_banks'] = compile_path_banks(comps, plan)
    from netlist2schematic.plugins.attachment.attachment_regulation import compile_regions
    plan['regulation_regions'] = compile_regions(devices,comps,structs,rows,formal_ports=formal_ports)
    from netlist2schematic.plugins.attachment.attachment_stack_columns import compile_columns
    plan['stack_columns'] = compile_columns(comps,plan,owners,structs)
    from netlist2schematic.plugins.attachment.attachment_control_shunts import compile_shunts
    plan['control_rail_shunts'] = compile_shunts(comps,plan,owners)
    plan['supported'] = plan['supported'] or bool(plan['tail_transistors']['groups'])
    plan['supported'] = plan['supported'] or bool(plan['regulation_regions']['groups'])
    plan['supported'] = plan['supported'] or conduction_stages['global_layout_eligible']
    plan['supported'] = plan['supported'] or repeated_stacks['rows_applied']
    plan['supported'] = plan['supported'] or bool(plan['stack_columns']['banks'])
    plan['supported'] = plan['supported'] or bool(plan['control_rail_shunts']['groups'])
    plan['signature'] = sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return plan


def _motion_groups(plan):
    managed = set(plan.get('source_managed_members', ()))
    cells=plan.get('conduction_stages',{})
    stage_groups=[dict(id=c['id'],members=c['members'],host_blocks=[],host_lanes=[])
                  for c in cells.get('stages',())] if cells.get('global_layout_eligible') else []
    return [g for g in plan['groups'] if not managed.intersection(g['members'])] + list(plan.get('source_groups', ())) + stage_groups


def _passive_motion_groups(plan):
    managed = set(plan.get('source_managed_members', ()))
    return [g for g in plan['groups'] if not managed.intersection(g['members'])]


def _envelope_ids(group, index):
    return [index[n] for n in group['host_members'] if n in index]


def _role_band(pl, group, index, plan):
    """Use actual junction access bands, never an entire owner rectangle."""
    readable = plan['readability_plan']
    bands, points = [], []
    for net in group['owner_junctions']:
        junction = readable['junctions'][net]
        ports = junction.get('conduction_ports') or junction.get('control_ports') or ()
        ports = [port for port in ports if port[0] in index]
        if not ports:
            continue
        rows = [pl.cell_of[index[name]][0] for name, _ in ports]
        bands.append((min(rows), max(rows)))
        point = junction_point(pl, index, readable, net)
        if point is not None:
            points.append(point)
        # A shared single junction has its own real terminal span; it does not
        # inherit the unrelated mirror/output members of a host's owner block.
        if len(group['owner_junctions']) == 1:
            points = [pl.pin_world(index[name], pin) for name, pin in ports]
    if not points or not bands:
        return None
    lo_r, hi_r = max(a for a, _ in bands), min(b for _, b in bands)
    if lo_r > hi_r:
        # A genuine bridge between disjoint access rows uses the intervening
        # band, with active display order preserved by the immutable policy.
        lo_r, hi_r = hi_r, lo_r
    extra = getattr(pl, 'attachment_junction_rows', {}).get(group['id'], ())
    allowed_rows = set(range(lo_r, hi_r+1)) | set(extra)
    return min(p[0] for p in points), max(p[0] for p in points), lo_r, hi_r, allowed_rows


def _embedded(pl, group, index, plan=None):
    plan = plan or getattr(pl, 'attachment_plan', None)
    band = _role_band(pl, group, index, plan) if plan else None
    if band is None:
        return []
    lo_x, hi_x, lo_r, hi_r, allowed_rows = band
    result = []
    physical = False
    if hasattr(pl, 'passive_local_offsets'):
        from netlist2schematic.plugins.attachment.attachment_physical import floating, physical_member_in_band
        physical = any(floating(pl, index[name]) for name in group['members'])
    for name in group['members']:
        if physical:
            if physical_member_in_band(pl, index[name], band):
                result.append(name)
            continue
        row, col = pl.cell_of[index[name]]
        x, _ = pl.pos_of(index[name])
        horizontal = lo_x+EPS < x < hi_x-EPS and row in allowed_rows
        vertical = lo_r < row < hi_r and lo_x-EPS <= x <= hi_x+EPS
        if horizontal or vertical:
            result.append(name)
    if group.get('requires_interior'):
        if len(result) != len(group['members']) or _port_errors(pl, pl.comps, index, plan, group):
            return []
        for path in _group_paths(group):
            a = junction_point(pl, index, plan['readability_plan'], path['path'][0])
            b = junction_point(pl, index, plan['readability_plan'], path['path'][-1])
            if a is None or b is None:
                continue
            axis = _path_axis(pl, index, path, a, b)
            direction = 1 if b[axis] >= a[axis] else -1
            points = [pl.pos_of(index[n]) for n in path['members']]
            if any(direction*(q[axis]-p[axis]) <= EPS for p, q in zip(points, points[1:])):
                return []
    return result


def _order_errors(pl, comps, index, plan):
    original = plan['active_rows']
    names = sorted(original)
    errors = 0
    for n, a in enumerate(names):
        for b in names[n+1:]:
            delta = original[a]-original[b]
            current = pl.cell_of[index[a]][0]-pl.cell_of[index[b]][0]
            errors += (delta == 0 and current != 0) or (delta != 0 and delta*current <= 0)
    return errors


def _source_chain_errors(pl, comps, index, group):
    from netlist2schematic.plugins.attachment.attachment_sources import source_chain_errors
    return source_chain_errors(pl, comps, index, group)


def _group_paths(group):
    if group.get('paths'):
        return group['paths']
    if len(group.get('path', ())) == len(group['members'])+1:
        return [dict(members=group['members'], path=group['path'])]
    return []


def _port_errors(pl, comps, index, plan, group):
    errors = 0
    if len(group.get('owner_junctions', ())) == 1:
        owner = group['owner_junctions'][0]
        anchor = junction_point(pl, index, plan['readability_plan'], owner)
        members = {index[n] for n in group['members']}
        by_net = defaultdict(list)
        for i in members:
            for pin, net in comps[i]['nodes'].items():
                by_net[net].append((i, pin))
        vectors = {'north': (0,-1), 'south': (0,1), 'west': (-1,0), 'east': (1,0)}
        for i in members:
            for pin, net in comps[i]['nodes'].items():
                # A bank's common end is a bus, not a serial continuation
                # toward the next member. Its real owner still faces anchor.
                if group.get('kind') == 'parallel_bank' and net != owner:
                    continue
                other = [(j,p) for j,p in by_net[net] if j != i]
                target = anchor if net == owner else pl.pos_of(other[0][0]) if len(other) == 1 else None
                if target is None:
                    continue
                center = pl.pos_of(i)
                vector = vectors.get(pl.pin_dir(i, pin), (0,0))
                errors += (target[0]-center[0])*vector[0]+(target[1]-center[1])*vector[1] <= EPS
        return errors
    for path in _group_paths(group):
        a = junction_point(pl, index, plan['readability_plan'], path['path'][0])
        b = junction_point(pl, index, plan['readability_plan'], path['path'][-1])
        if a is None or b is None:
            continue
        axis = _path_axis(pl, index, path, a, b)
        direction = 1 if b[axis] >= a[axis] else -1
        for k, name in enumerate(path['members']):
            nodes = comps[index[name]]['nodes']
            before = [pin for pin, net in nodes.items() if net == path['path'][k]]
            after = [pin for pin, net in nodes.items() if net == path['path'][k+1]]
            if len(before) != 1 or len(after) != 1:
                errors += 1
                continue
            p = pl.pin_world(index[name], before[0]); q = pl.pin_world(index[name], after[0])
            errors += direction*(q[axis]-p[axis]) <= EPS
    return errors


def _path_axis(pl, index, path, a, b):
    points = [pl.pos_of(index[n]) for n in path['members']]
    spans = [max(p[k] for p in points)-min(p[k] for p in points) for k in (0, 1)]
    if max(spans) > EPS:
        return 0 if spans[0] >= spans[1] else 1
    return 0 if abs(b[0]-a[0]) >= abs(b[1]-a[1]) else 1


def _mirror_orientation(pl, i):
    old = pl.orient[i]
    center = pl.pos_of(i)
    pins = tuple(pl.comps[i]['nodes'])
    original = {pin: pl.pin_world(i, pin) for pin in pins}
    choices = []
    for orientation in pl.allowed_orientations(i):
        pl.orient[i] = orientation
        cost = sum(abs(pl.pin_world(i, pin)[0]+p[0]-2*center[0])
                   +abs(pl.pin_world(i, pin)[1]-p[1]) for pin, p in original.items())
        choices.append((cost, orientation != old, orientation))
    pl.orient[i] = old
    return min(choices)[-1]


def measure_attachments(pl, comps, index, plan):
    """Measure the same evidence against old, new, or reconstructed geometry."""
    result = dict(version=VERSION, plan_signature=plan.get('signature'),
        supported=bool(plan.get('supported')), measurable=False, group_count=len(plan.get('groups', ())),
        member_count=len({n for g in plan.get('groups', ()) for n in g['members']}),
        interior_member_count=len({n for g in plan.get('groups', ())
                                  if g.get('requires_interior') for n in g['members']}),
        violations=None, shape_error=None, pin_axis_error=None, owner_distance=None, port_direction_errors=None,
        embedded_groups=0, embedded_members=0, active_order_errors=None, groups=[],
        source_count=len(plan.get('source_groups', ())), source_errors=None,
        source_distance=None, source_measurable=False, source_raw_errors=None,
        source_chain_errors=None, tail_center_count=0, tail_center_error=None, sources=[])
    if not plan.get('supported'):
        return result
    try:
        physical = measure_readability(pl, comps, index, plan['readability_plan'])
        if plan['groups'] and not physical.get('measurable'):
            return result
        from netlist2schematic.plugins.attachment.attachment_access import build_access_links, measure_access
        result['host_access'] = measure_access(pl, comps, index,
            plan.get('host_access') or build_access_links(comps, plan))
        if result['host_access']['count'] and not result['host_access']['measurable']:
            return result
        source = None
        if plan.get('source_groups'):
            from netlist2schematic.plugins.attachment.attachment_sources import measure_source_groups
            source = measure_source_groups(pl, comps, {}, index, plan['source_model'])
            if not source['measurable']:
                result['sources'] = source['records']
                return result
        for i in index.values():
            if not all(isfinite(v) for v in pl.pos_of(i)):
                return result
        details = [dict(id=g['id'], kind=g['kind'], members=list(g['members']),
                        requires_interior=g.get('requires_interior', False),
                        port_direction_errors=_port_errors(pl, comps, index, plan, g),
                        embedded_members=_embedded(pl, g, index, plan)) for g in plan['groups']]
        embedded = {n for g in details if g['requires_interior'] for n in g['embedded_members']}
        host_error = sum(abs(pl.pin_world(index[a['a']], a['pin_a'])[0]
                             -pl.pin_world(index[a['b']], a['pin_b'])[0])/100 for a in plan.get('host_axes', ()))
        port_errors = sum(g['port_direction_errors'] for g in details)
        result.update(measurable=True, violations=(physical.get('violations') or 0)+port_errors,
            port_direction_errors=port_errors,
            shape_error=physical.get('shape_error') or 0.0,
            pin_axis_error=(physical.get('pin_axis_error') or 0.0)+host_error,
            owner_distance=physical.get('owner_locality') or 0.0,
            embedded_groups=sum(bool(g['embedded_members']) and g['requires_interior'] for g in details),
            embedded_members=len(embedded), active_order_errors=_order_errors(pl, comps, index, plan),
            groups=details)
        if source is not None:
            tails=[r for r in source['records'] if r['kind']=='direct_tail' and len(r['links'])==1]
            result.update(source_errors=source['source_errors'], source_distance=source['source_distance'],
                source_measurable=True, source_raw_errors=source['raw_source_errors'],
                source_chain_errors=source['chain_errors'], sources=source['records'],
                tail_center_count=len(tails),tail_center_error=(sum(abs(r['links'][0]['point'][0]
                    -r['anchor']['point'][0])/100 for r in tails)/len(tails) if tails else None))
    except (KeyError, IndexError, TypeError, ValueError):
        return result
    return result


def attachment_rank(metrics):
    if not metrics.get('measurable'):
        return None
    from netlist2schematic.plugins.attachment.attachment_access import access_terms
    access = access_terms(metrics)
    if access is None:
        return None
    passive = (metrics['active_order_errors'], metrics['violations']+access[0], -metrics['embedded_members'],
               metrics['shape_error']+metrics['pin_axis_error']+access[1], metrics['owner_distance'])
    return passive + ((metrics['source_errors'], metrics['source_distance']) if metrics['source_count'] else (0, 0))


def _source_guards_preserved(before, after):
    if (not before.get('measurable') or not after.get('measurable')
            or not before['source_count'] or before['source_count'] != after['source_count']):
        return False
    if any(after[k] > before[k]+EPS for k in ('violations', 'active_order_errors',
            'shape_error', 'pin_axis_error', 'owner_distance')):
        return False
    if after['embedded_members'] < before['embedded_members']:
        return False
    from netlist2schematic.plugins.attachment.attachment_access import access_terms
    old_access, new_access = access_terms(before), access_terms(after)
    if old_access is None or new_access is None or new_access > old_access:
        return False
    return True


def _source_improves(before, after):
    if not _source_guards_preserved(before, after):
        return False
    return (after['source_errors'], after['source_distance']) < (before['source_errors'], before['source_distance'])


def score_attachments(pl, comps, index, plan):
    metrics = measure_attachments(pl, comps, index, plan)
    if not metrics['measurable']:
        return dict(weighted_total=0.0, metrics=metrics)
    missing = max(0, metrics['interior_member_count']-metrics['embedded_members'])
    value = (1.25*metrics['violations'] + 2.5*metrics['active_order_errors']
             + metrics['shape_error'] + 2.5*metrics['pin_axis_error']
             + .35*metrics['owner_distance'] + .75*missing)
    from netlist2schematic.plugins.attachment.attachment_access import access_terms
    access = access_terms(metrics)
    value += 1.25*access[0] + access[1]
    if metrics['source_count']:
        value += 1.25*metrics['source_errors'] + .5*metrics['source_distance']
    return dict(weighted_total=value, metrics=metrics)


def _fixed_ids(pl, index):
    return {index[n] if isinstance(n, str) and n in index else n
            for n in set(getattr(pl, 'fixed', ())) | set(getattr(pl, 'fixed_cells', {}))}


class AttachmentPlacement(FlexiblePassivePlacement):
    """An opt-in policy; all proposal state is cells plus orientations."""
    def set_state(self, cells, orientations):
        self.cell_of = [tuple(cell) for cell in cells]
        self.orient = list(orientations)
        self.row_of = [row for row, _ in self.cell_of]
        self.comp_at = {cell: i for i, cell in enumerate(self.cell_of)}

    def allowed_orientations(self, i):
        if i in getattr(self, 'attachment_orientation_ids', ()):
            return list(range(self.n_orients(i)))
        return super().allowed_orientations(i)

    def blocks_contiguous(self):
        permitted = getattr(self, 'attachment_guests', {})
        local_offsets = getattr(self, 'passive_local_offsets', None)
        for block in (self.blocks + getattr(self, 'layout_macro_blocks', [])
                      + getattr(self, 'hierarchy_guards', [])):
            own = frozenset(block)
            groups = permitted.get(own, ())
            by_row = defaultdict(list)
            for i in block:
                by_row[self.cell_of[i][0]].append(self.cell_of[i][1])
            for row, columns in by_row.items():
                for col in range(min(columns), max(columns)+1):
                    occupant = self.comp_at.get((row, col))
                    if occupant is not None and occupant not in own:
                        if local_offsets is not None and any(local_offsets[occupant]):
                            continue
                        name = self.comps[occupant]['id']
                        if not any(name in group['members'] and name in _embedded(
                                self, group, self.attachment_index, self.attachment_plan) for group in groups):
                            return False
        if local_offsets is None:
            return True
        from netlist2schematic.plugins.attachment.attachment_physical import floating_leaves_clear
        from netlist2schematic.layout.structured_placement import par
        return floating_leaves_clear(par, self, permitted,
            lambda group: _embedded(self, group, self.attachment_index, self.attachment_plan))

    def valid_state(self):
        if hasattr(self, 'passive_local_offsets'):
            from netlist2schematic.common.passive_coordinates import validate_passive_offsets
            try:
                validate_passive_offsets(self, self.comps)
            except ValueError:
                return False
        if (len(self.comp_at) != len(self.comps) or len(self.cell_of) != len(self.comps)
                or self.comp_at != {c: i for i, c in enumerate(self.cell_of)}
                or self.row_of != [r for r, _ in self.cell_of]):
            return False
        if (tuple(self.column_x), tuple(self.row_y)) != self.attachment_axes:
            return False
        for i, (row, col) in enumerate(self.cell_of):
            if (type(row) is not int or type(col) is not int or not 0 <= row < len(self.row_y)
                    or not 0 <= col < self.cols or self.orient[i] not in self.allowed_orientations(i)):
                return False
        if any(self.cell_of[i][0] != row for i, row in self.fixed_rows.items()):
            return False
        if any(self.cell_of[i][0] not in self.attachment_source_rows for i in self.attachment_source_ids):
            return False
        for i, (cell, orient, point) in self.attachment_fixed.items():
            if self.cell_of[i] != cell or self.orient[i] != orient or self.pos_of(i) != point:
                return False
        if not self.blocks_contiguous():
            return False
        index = self.attachment_index
        for axis, limit in self.attachment_axis_limits:
            error = abs(self.pin_world(index[axis['a']], axis['pin_a'])[0]
                        -self.pin_world(index[axis['b']], axis['pin_b'])[0])
            if error > limit+EPS:
                return False
        for identifier, members in self.attachment_required_embeddings.items():
            group = next(g for g in self.attachment_plan['groups'] if g['id'] == identifier)
            if not set(members) <= set(_embedded(self, group, index)):
                return False
            if _port_errors(self, self.comps, index, self.attachment_plan, group) > self.attachment_port_limits.get(identifier, 0):
                return False
        for group in self.attachment_plan['groups']:
            if (group['id'] in self.attachment_port_limits and _port_errors(self, self.comps,
                    index, self.attachment_plan, group) > self.attachment_port_limits[group['id']]):
                return False
        limits = self.attachment_shape_limits
        if limits:
            measured = measure_readability(self, self.comps, index, self.attachment_plan['readability_plan'])
            for pattern in measured.get('patterns', ()):
                if pattern['id'] in limits and any(pattern[key] > value+EPS
                        for key, value in limits[pattern['id']].items()):
                    return False
        if getattr(self, 'attachment_channel_limits', {}):
            from netlist2schematic.plugins.attachment.attachment_channels import measure_channels, channel_rank, bodies_clear
            from netlist2schematic.layout.structured_placement import par
            model = self.attachment_plan.get('parallel_channels', {})
            for bank in model.get('banks', ()):
                limit = self.attachment_channel_limits.get(bank['id'])
                if limit is not None:
                    rank = channel_rank(measure_channels(self, self.comps, index, dict(model, banks=[bank])))
                    if rank is None or rank > limit:
                        return False
            if not bodies_clear(par, self, self.attachment_channel_halo):
                return False
        if self.attachment_plan.get('source_groups'):
            from netlist2schematic.plugins.attachment.attachment_sources import measure_source_groups
            measured = measure_source_groups(self, self.comps, {}, index, self.attachment_plan['source_model'])
            if not measured['measurable']:
                return False
            if any(r['id'] in self.attachment_source_error_limits
                   and r['source_errors'] > self.attachment_source_error_limits[r['id']]
                   for r in measured['records']):
                return False
            for group in self.attachment_plan['source_groups']:
                if (group['id'] in self.attachment_source_port_limits
                        and _port_errors(self, self.comps, index, self.attachment_plan, group)
                            > self.attachment_source_port_limits[group['id']]):
                    return False
                if (group['id'] in self.attachment_source_chain_limits
                        and _source_chain_errors(self, self.comps, index, group)
                            > self.attachment_source_chain_limits[group['id']]):
                    return False
        return True

    def propose(self):
        count = self.attachment_move_counts
        if self.rng.random() < .25:
            count['joint_attempts'] += 1
            before = list(self.cell_of), list(self.orient)
            groups = _motion_groups(self.attachment_plan) + list(getattr(self,'attachment_stack_bundles',()))
            if not groups:
                return None
            group = self.rng.choice(groups)
            if group['id'] in {g['id'] for g in self.attachment_plan.get('source_groups', ())}:
                from netlist2schematic.plugins.attachment.attachment_sources import source_group_states
                count['source_attempts'] += 1
                model = dict(self.attachment_plan['source_model'], groups=[group])
                choices = list(source_group_states(self, self.comps, {}, self.attachment_index,
                                                   model, max_candidates=8))
                if not choices:
                    return None
                cells, orientations, _ = self.rng.choice(choices)
                self.set_state(cells, orientations)
                if not self.valid_state():
                    count['slot_guard_rejections'] += 1
                    self.set_state(*before)
                    return None
                count['joint_legal'] += 1; count['source_legal'] += 1
                return before
            # Movement bundles are local and non-transitive. They do not alter
            # semantic ownership, slot eligibility, or the annealer snapshot.
            ids = {self.attachment_index[n] for n in group['members']}
            for block in group['host_blocks']:
                ids.update(self.attachment_index[n] for n in block)
            for lane in group.get('host_lanes', ()):
                ids.update(self.attachment_index[n] for n in lane)
            if ids & set(self.attachment_fixed):
                return None
            cells, orientations = list(self.cell_of), list(self.orient)
            if self.rng.random() < .5:
                action = 'translation'
                dc = self.rng.choice((-2, -1, 1, 2))
                for i in ids:
                    row, col = cells[i]
                    cells[i] = row, col+dc
            else:
                action = 'mirror'
                lo, hi = min(cells[i][1] for i in ids), max(cells[i][1] for i in ids)
                for i in ids:
                    row, col = cells[i]
                    cells[i] = row, lo+hi-col
                    if self.comps[i]['symbol'] in ('nmos', 'pmos', 'npn', 'pnp'):
                        alternatives = self.allowed_orientations(i)
                        if 1-orientations[i] in alternatives:
                            orientations[i] = 1-orientations[i]
                    elif i in self.attachment_orientation_ids:
                        orientations[i] = _mirror_orientation(self, i)
            self.set_state(cells, orientations)
            if not self.valid_state():
                count['slot_guard_rejections'] += 1
                self.set_state(*before)
                return None
            count['joint_legal'] += 1
            count[action+'_legal'] += 1
            return before
        count['ordinary_attempts'] += 1
        before = list(self.cell_of), list(self.orient)
        if self.passive_indices:
            old = super().propose()
        else:
            old = StructuredPlacement.propose(self)
        if old is None or not self.valid_state():
            count['ordinary_rejections'] += 1
            self.set_state(*before)
            return None
        count['ordinary_legal'] += 1
        return old


def _install(pl, comps, index, plan, *, required=None):
    clone = AttachmentPlacement.__new__(AttachmentPlacement)
    clone.__dict__ = deepcopy(pl.__dict__)
    clone.attachment_plan, clone.attachment_index = deepcopy(plan), dict(index)
    clone.attachment_axes = tuple(clone.column_x), tuple(clone.row_y)
    clone.cols = len(clone.column_x)
    fixed = _fixed_ids(clone, index)
    managed = {index[n] for n in plan.get('source_managed_members', ())}
    source_ids = {index[g['device']] for g in plan.get('source_groups', ())} - fixed
    movable = {i for i, c in enumerate(comps) if c['symbol'] in PASSIVE and i not in fixed} | source_ids
    clone.attachment_source_ids = frozenset(source_ids)
    clone.attachment_source_rows = tuple(range(len(clone.row_y)))
    clone.source_orientation_ids = frozenset(getattr(clone, 'source_orientation_ids', ())) | source_ids
    clone.passive_indices = tuple(i for i in sorted(movable) if comps[i]['symbol'] in PASSIVE and i not in managed)
    clone._passive_set = frozenset(clone.passive_indices)
    clone.fixed_rows = {i: cell[0] for i, cell in enumerate(clone.cell_of) if i not in movable}
    clone.attachment_fixed = {i: (tuple(clone.cell_of[i]), clone.orient[i], tuple(clone.pos_of(i)))
                              for i in _fixed_ids(clone, index)}
    clone.attachment_required_embeddings = dict(required or {})
    clone.attachment_shape_limits = getattr(clone, 'attachment_shape_limits', {})
    clone.attachment_port_limits = getattr(clone, 'attachment_port_limits', {})
    clone.attachment_source_error_limits = getattr(clone, 'attachment_source_error_limits', {})
    clone.attachment_source_port_limits = getattr(clone, 'attachment_source_port_limits', {})
    clone.attachment_source_chain_limits = getattr(clone, 'attachment_source_chain_limits', {})
    clone.attachment_axis_limits = getattr(clone, 'attachment_axis_limits', ())
    clone.attachment_stack_bundles = getattr(clone, 'attachment_stack_bundles', ())
    clone.attachment_junction_rows = getattr(clone, 'attachment_junction_rows', {})
    clone.attachment_row_origin = getattr(clone, 'attachment_row_origin', tuple(range(len(clone.row_y))))
    clone.attachment_orientation_ids = frozenset(index[n] for group in
        list(plan['groups'])+list(plan.get('source_groups', ())) for n in group['members'])
    guest_map = defaultdict(list)
    for group in list(plan['groups'])+list(plan.get('source_groups', ())):
        ids = {index[n] for n in group['members']}
        for block in (getattr(clone, 'blocks', ()) + getattr(clone, 'layout_macro_blocks', [])
                      + getattr(clone, 'hierarchy_guards', [])):
            names = {comps[i]['id'] for i in block}
            if names & set(group['host_members']):
                guest_map[frozenset(block)].append(group)
    clone.attachment_guests = {key: tuple(value) for key, value in guest_map.items()}
    clone.attachment_move_counts = getattr(clone, 'attachment_move_counts', {name: 0 for name in (
        'joint_attempts', 'joint_legal', 'translation_legal', 'mirror_legal',
        'slot_guard_rejections', 'ordinary_attempts', 'ordinary_legal', 'ordinary_rejections',
        'source_attempts', 'source_legal')})
    clone.attachment_move_counts.setdefault('source_attempts', 0)
    clone.attachment_move_counts.setdefault('source_legal', 0)
    # Flexible instances already have these. Plain/source-only seeds do not.
    clone.passive_neighbors = getattr(clone, 'passive_neighbors', {i: () for i in clone.passive_indices})
    clone.passive_relations = getattr(clone, 'passive_relations',
                                     dict(series=[], parallel=[], feedback=[], skipped={}))
    clone.move_counts = getattr(clone, 'move_counts', {name: 0 for name in (
        'total_proposals', 'passive_attempts', 'passive_legal', 'passive_moves',
        'passive_swaps', 'passive_cross_row', 'passive_rejected', 'legacy_attempts',
        'legacy_legal', 'legacy_rejected')})
    clone.set_state(clone.cell_of, clone.orient)
    return clone


def _insert_axis(pl, axis_name, cut, amount, anchor):
    """Insert common display slots; callers validate fixed world positions."""
    axis = list(getattr(pl, axis_name))
    pitch = median([b-a for a, b in zip(axis, axis[1:])]) if len(axis) > 1 else 112
    pitch = max(100, pitch)
    if anchor == 'left':
        start = axis[cut-1] if cut else axis[0]-pitch
        added = [start+pitch*(n+1) for n in range(amount)]
        values = axis[:cut]+added+[x+amount*pitch for x in axis[cut:]]
    else:
        end = axis[cut] if cut < len(axis) else axis[-1]+pitch
        added = [end-pitch*(amount-n) for n in range(amount)]
        values = [x-amount*pitch for x in axis[:cut]]+added+axis[cut:]
    coordinate = 1 if axis_name == 'column_x' else 0
    cells = [tuple(v+amount if k == coordinate and v >= cut else v
                   for k, v in enumerate(cell)) for cell in pl.cell_of]
    pl.fixed_cells = {i: tuple(v+amount if k == coordinate and v >= cut else v
                              for k, v in enumerate(cell))
                      for i, cell in getattr(pl, 'fixed_cells', {}).items()}
    setattr(pl, axis_name, values)
    if axis_name == 'row_y' and hasattr(pl, 'attachment_junction_rows'):
        pl.attachment_junction_rows = {key: tuple(r+amount if r >= cut else r for r in rows)
                                      for key, rows in pl.attachment_junction_rows.items()}
        pl.attachment_row_origin = (pl.attachment_row_origin[:cut]+(None,)*amount
                                    +pl.attachment_row_origin[cut:])
    pl.cols = len(pl.column_x)
    pl.set_state(cells, pl.orient)


def _orientation(pl, i, horizontal):
    preferred = 1 if horizontal else 0
    return preferred if preferred in pl.allowed_orientations(i) else pl.orient[i]


def _orient_path(pl, comps, index, plan, path, *, axis=None):
    """Point real terminal nets along the drawn path, without swapping pins."""
    nodes = path.get('path', ())
    if len(nodes) != len(path['members'])+1:
        return
    a = junction_point(pl, index, plan['readability_plan'], nodes[0])
    b = junction_point(pl, index, plan['readability_plan'], nodes[-1])
    if a is None or b is None:
        return
    axis = _path_axis(pl, index, path, a, b) if axis is None else axis
    direction = 1 if b[axis] >= a[axis] else -1
    for k, name in enumerate(path['members']):
        i = index[name]
        before = [pin for pin, net in comps[i]['nodes'].items() if net == nodes[k]]
        after = [pin for pin, net in comps[i]['nodes'].items() if net == nodes[k+1]]
        if len(before) != 1 or len(after) != 1:
            continue
        choices = []
        for orientation in pl.allowed_orientations(i):
            pl.orient[i] = orientation
            p, q = pl.pin_world(i, before[0]), pl.pin_world(i, after[0])
            choices.append(((direction*(q[axis]-p[axis]) <= EPS,
                             abs(q[1-axis]-p[1-axis]), orientation), orientation))
        if choices:
            pl.orient[i] = min(choices)[1]


def _rehome_related_shunts(pl, comps, index, plan, moving_group):
    """A moved stack may need its own shunt outside the new interior lane."""
    active_nets = set(moving_group['owner_junctions'])
    fixed = _fixed_ids(pl, index)
    for group in plan['groups']:
        if (group['kind'] != 'single_attachment' or len(group['members']) != 1
                or len(group['owner_junctions']) != 1
                or not active_nets.intersection(group['owner_junctions'])
                or set(group['members']).intersection(plan.get('source_managed_members', ()))):
            continue
        i = index[group['members'][0]]
        if i in fixed:
            continue
        point = junction_point(pl, index, plan['readability_plan'], group['owner_junctions'][0])
        if point is None:
            continue
        occupied = {cell for j, cell in enumerate(pl.cell_of) if j != i}
        candidates = sorted(((r, c) for r in range(len(pl.row_y)) for c in range(pl.cols)
                             if (r, c) not in occupied), key=lambda cell: (
            abs(pl.column_x[cell[1]]-point[0])+abs(pl.row_y[cell[0]]-point[1]), cell))
        original = list(pl.cell_of), list(pl.orient)
        for target in candidates[:32]:
            cells = list(pl.cell_of); cells[i] = target
            pl.set_state(cells, pl.orient)
            allowed = True
            for block in pl.blocks + getattr(pl, 'layout_macro_blocks', []):
                columns = [pl.cell_of[j][1] for j in block if pl.cell_of[j][0] == target[0]]
                if i not in block and columns and min(columns) <= target[1] <= max(columns):
                    allowed &= group['members'][0] in _embedded(pl, group, index, plan)
            if not allowed:
                pl.set_state(*original)
                continue
            net = group['owner_junctions'][0]
            pin = next(pin for pin, value in comps[i]['nodes'].items() if value == net)
            choices = []
            for orientation in pl.allowed_orientations(i):
                pl.orient[i] = orientation
                rejected = _rail_marker_direction_conflicts(pl, i)
                if rejected:
                    diagnostic = getattr(pl, 'attachment_slot_diagnostics', None)
                    if diagnostic is not None:
                        diagnostic['rail_direction_rejections'] += 1
                        available = max(0, 8-len(diagnostic['examples']))
                        diagnostic['examples'].extend(rejected[:available])
                    continue
                p = pl.pin_world(i, pin)
                choices.append((abs(p[0]-point[0])+abs(p[1]-point[1]), orientation))
            if not choices:
                pl.set_state(*original)
                continue
            pl.orient[i] = min(choices)[1]
            break
        else:
            pl.set_state(*original)


def _slot_specs(pl, group, index):
    if not group.get('requires_interior') or group.get('row_mode') == 'conduction_bridge':
        return
    hosts = _envelope_ids(group, index)
    if len(hosts) < 2:
        return
    columns = sorted({pl.cell_of[i][1] for i in hosts})
    if len(columns) < 2:
        return
    band = _role_band(pl, group, index, pl.attachment_plan)
    if band is None:
        return
    host_rows = [band[2], band[3]]
    if group['kind'] == 'multi_path_bridge':
        paths = sorted(group['paths'], key=lambda p: (-len(p['members']), tuple(p['members'])))
    else:
        paths = [dict(members=group['members'], path=group.get('path', []))]
    width, depth = max(len(p['members']) for p in paths), len(paths)
    cuts = sorted(range(columns[0]+1, columns[-1]+1),
                  key=lambda c: (abs(c-median(columns)), c))
    rows = sorted(range(min(host_rows), max(host_rows)+1),
                  key=lambda r: (abs(r-median(host_rows)), r))
    extra_rows = max(0, depth-(max(host_rows)-min(host_rows)+1))
    for cut in cuts:
        for row in rows:
            if row+depth-1 > max(host_rows)+extra_rows:
                continue
            # Try the existing space and minimal openings at every promising
            # cut; one large old gap must not consume the whole search budget.
            for amount in range(width+1):
                if cut+width >= pl.cols+amount:
                    continue
                for anchor in (('left',) if amount == 0 else ('left', 'right')):
                    yield dict(cut=cut, row=row, width=width, added_columns=amount,
                               depth=depth, added_rows=extra_rows,
                               row_cut=max(host_rows)+1, paths=paths, anchor=anchor)


def _joint_slot(pl, comps, index, plan, group, spec):
    trial = deepcopy(pl)
    trial.attachment_slot_diagnostics = dict(rail_direction_rejections=0, examples=[])
    # Every member moves together; no active outsider is evicted from a cell.
    if spec['added_columns']:
        _insert_axis(trial, 'column_x', spec['cut'], spec['added_columns'], spec['anchor'])
    if spec['added_rows']:
        _insert_axis(trial, 'row_y', spec['row_cut'], spec['added_rows'], spec['anchor'])
        trial.attachment_junction_rows = dict(trial.attachment_junction_rows)
        trial.attachment_junction_rows[group['id']] = tuple(range(
            spec['row_cut'], spec['row_cut']+spec['added_rows']))
    cells, orientations = list(trial.cell_of), list(trial.orient)
    lanes = group.get('host_lanes', ())
    if len(lanes) == 2:
        lanes = sorted(lanes, key=lambda lane: (median(cells[index[n]][1] for n in lane), tuple(lane)))
        for lane, column in zip(lanes, (spec['cut']-1, spec['cut']+spec['width'])):
            if len({cells[index[n]][0] for n in lane}) != len(lane):
                continue
            for name in lane:
                cells[index[name]] = cells[index[name]][0], column
    # Path ordering must see the new actual host endpoints, not the old seed.
    trial.set_state(cells, orientations)
    for lane, path in enumerate(spec['paths']):
        names = list(path['members'])
        ends = path.get('path', ())
        if len(ends) >= 2:
            a = junction_point(trial, index, plan['readability_plan'], ends[0])
            b = junction_point(trial, index, plan['readability_plan'], ends[-1])
            if a is not None and b is not None and a[0] > b[0]:
                names.reverse()
        inset = (spec['width']-len(names))//2
        for step, name in enumerate(names):
            i = index[name]
            cells[i] = spec['row']+lane, spec['cut']+inset+step
            orientations[i] = _orientation(trial, i, group['kind'] != 'parallel_bank')
    trial.set_state(cells, orientations)
    for path in _group_paths(group):
        _orient_path(trial, comps, index, plan, path, axis=0)
    _rehome_related_shunts(trial, comps, index, plan, group)
    return _install(trial, comps, index, plan, required=pl.attachment_required_embeddings)


def _fixed_world_safe(original, trial, index):
    return all(trial.pos_of(i) == original.pos_of(i) and trial.orient[i] == original.orient[i]
               for i in _fixed_ids(original, index))


def _static(par, pl, nets, index, options, until):
    from netlist2schematic.plugins.symmetry.symmetry_layout import _static_safe
    from netlist2schematic.routing.routing_rules import RoutingOptions
    from netlist2schematic.routing.orthogonal_router import RoutingFailure
    try:
        return _static_safe(par, pl, nets, index, options or RoutingOptions(), until)
    except RoutingFailure as exc:
        if exc.diagnostics.get('reason') == 'quality_time_budget':
            return None
        raise


def _checked_static(par, pl, nets, index, options, until):
    # Keep the transaction boundary safe even when an alternate static oracle
    # raises the shared router's deadline exception directly.
    from netlist2schematic.routing.orthogonal_router import RoutingFailure
    try:
        result=_static(par, pl, nets, index, options, until)
        return None if until is not None and perf_counter()>=until else result
    except RoutingFailure as exc:
        if exc.diagnostics.get('reason') == 'quality_time_budget':
            return None
        raise


def _release_empty_rows(par, pl, original, comps, nets, index, plan, options, budget, until, report):
    if plan.get('preserve_feedback_rows'):
        return pl, False
    protected = {r for rows in pl.attachment_junction_rows.values() for r in rows}
    protected.update(plan['display_rows'][n] for g in plan['groups']
                     if g.get('row_mode') == 'conduction_bridge' for n in g['members'])
    active_rows = [pl.cell_of[i][0] for i, c in enumerate(comps) if c['symbol'] in ACTIVE]
    if not active_rows:
        return pl, False
    empty = [r for r in range(min(active_rows)+1, max(active_rows))
             if r not in protected and r not in pl.row_of
             and pl.attachment_row_origin[r] in plan.get('reserved_passive_rows', ())]
    for row in reversed(empty):
        if not _has_budget(budget, until):
            break
        budget['candidates'] += 1; report['candidate_trials'] += 1
        trial = deepcopy(pl)
        delta = trial.row_y[row]-trial.row_y[row-1]
        trial.row_y = trial.row_y[:row]+[y-delta for y in trial.row_y[row+1:]]
        cells = [(r-(r > row), c) for r, c in trial.cell_of]
        trial.fixed_cells = {i: (r-(r > row), c) for i, (r,c) in trial.fixed_cells.items()}
        trial.attachment_row_origin = trial.attachment_row_origin[:row]+trial.attachment_row_origin[row+1:]
        trial.attachment_junction_rows = {key: tuple(r-(r > row) for r in rows)
                                         for key, rows in trial.attachment_junction_rows.items()}
        trial.set_state(cells, trial.orient)
        trial = _install(trial, comps, index, plan, required=pl.attachment_required_embeddings)
        if not trial.valid_state() or not _fixed_world_safe(original, trial, index):
            continue
        before, after = measure_attachments(pl, comps, index, plan), measure_attachments(trial, comps, index, plan)
        if attachment_rank(after) is None or attachment_rank(after) > attachment_rank(before):
            continue
        safe = _checked_static(par, trial, nets, index, options, until)
        if safe is None:
            budget['exhausted'] = True
            return pl, True
        if not safe:
            continue
        report['actions'].append(dict(kind='release_vacated_attachment_row', removed_row=row,
            original_display_row=pl.attachment_row_origin[row], removed_height=delta))
        pl = trial
    return pl, False


def _joint_topology_stage(par, current, original, comps, nets, index, plan, options,
                          budget, until, report, *, initialization, hpwl_limit=None, family='paired'):
    if family!='conduction':
        return _joint_topology_search(par,current,original,comps,nets,index,plan,options,
            budget,until,report,initialization=initialization,hpwl_limit=hpwl_limit,family=family)
    from netlist2schematic.plugins.attachment.attachment_budget import conduction_quota
    quota=conduction_quota(budget,until,perf_counter)
    with quota:
        result,expired=_joint_topology_search(par,current,original,comps,nets,index,plan,options,
            budget,quota.deadline,report,initialization=initialization,hpwl_limit=hpwl_limit,family=family)
    report.setdefault('joint_stage_budgets',[]).append(quota.record)
    if quota.time_yield and not budget.get('exhausted'):expired=False
    return result,expired


def _trial_state_category(trial):
    """Cheap diagnostic, not a replacement or relaxation of valid_state()."""
    if len(set(trial.cell_of))!=len(trial.cell_of):return 'slot_collision'
    if trial.comp_at!={c:i for i,c in enumerate(trial.cell_of)}:return 'occupancy_sync'
    if trial.row_of!=[r for r,c in trial.cell_of]:return 'row_sync'
    if any(trial.cell_of[i][0]!=r for i,r in trial.fixed_rows.items()):return 'fixed_row'
    return 'other_layout_contract'


def _joint_topology_search(par, current, original, comps, nets, index, plan, options,
                          budget, until, report, *, initialization, hpwl_limit=None, family='paired'):
    from netlist2schematic.plugins.attachment.attachment_pairs import paired_bank_trials
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    facts=report.setdefault('joint_stage_diagnostics',{}).setdefault(family,
        dict(considered=0,state_rejected=0,fixed_rejected=0,objective_rejected=0,hpwl_rejected=0,
             static_rejected=0,validation_timeout=0,verified=0,accepted=0,state_reasons={}))
    relations = plan.get('paired_banks', ()) if family=='paired' else (
        range(len((plan.get('branch_row_plan') or {}).get('bridge_alignment', ()))))
    for relation in relations:
        if not _has_budget(budget, until):
            break
        measured = measure_attachments(current, comps, index, plan)
        baseline_rank = attachment_rank(measured)
        best = None
        if family=='paired':
            proposals=paired_bank_trials(current,comps,index,plan,relation,
                allow_axis_growth=initialization,deadline=until)
        else:
            from netlist2schematic.plugins.attachment.attachment_conduction import conduction_trials
            proposals=conduction_trials(current,comps,index,plan,deadline=until,
                grid_step=options.grid_step if options is not None else None)
        for trial, action in proposals:
            if not _has_budget(budget, until):
                break
            budget['candidates'] += 1
            count_key = 'candidate_trials' if initialization else 'candidates'
            report[count_key] += 1
            diagnostic='paired_bank_trials' if family=='paired' else 'conduction_chain_trials'
            report[diagnostic] = report.get(diagnostic,0)+1
            facts['considered']+=1
            if not trial.valid_state():
                facts['state_rejected']+=1
                reason=_trial_state_category(trial)
                facts['state_reasons'][reason]=facts['state_reasons'].get(reason,0)+1
                continue
            if not _fixed_world_safe(original, trial, index):
                facts['fixed_rejected']+=1
                continue
            after = measure_attachments(trial, comps, index, plan)
            rank = attachment_rank(after)
            if rank is None or baseline_rank is None or rank >= baseline_rank:
                facts['objective_rejected']+=1
                continue
            if hpwl_limit is not None and _hpwl(par, trial, nets, index) > hpwl_limit+EPS:
                report['hpwl_rejections'] += 1
                facts['hpwl_rejected']+=1
                continue
            safe = _checked_static(par, trial, nets, index, options, until)
            if safe is None or perf_counter()>=until:
                facts['validation_timeout']+=1
                budget['exhausted'] = True
                return current, True
            if not safe:
                facts['static_rejected']+=1
                continue
            facts['verified']+=1
            key = rank, action['columns_added'], tuple(trial.cell_of), tuple(trial.orient)
            if best is None or key < best[0]:
                best = key, trial, action
        if best:
            _, current, action = best
            report['actions'].append(action)
            facts['accepted']+=1
            if not initialization:
                report['accepted'] += 1
        elif family=='conduction':
            break  # The next pass uses fresh coordinates only after a commit.
    return current, False


def _tap_slot_stage(par,current,original,comps,nets,index,plan,options,budget,until,report,
                    *,initialization,hpwl_limit=None):
    """Use only leftover shared allowance; keep every verified earlier action."""
    from netlist2schematic.plugins.attachment.attachment_taps import tap_trials,measure_taps
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    report.setdefault('tap_candidates',0)
    model=plan.get('conduction_taps',{})
    for relation in model.get('groups',()):
        if not _has_budget(budget,until):break
        local_model=dict(groups=[relation])
        access=measure_taps(current,comps,index,local_model)
        if not access['measurable'] or not access['errors']:continue
        baseline=measure_attachments(current,comps,index,plan)
        baseline_rank=attachment_rank(baseline)
        if baseline_rank is None:continue
        for trial,action in tap_trials(current,comps,index,plan,relation,
                allow_row_split=initialization,grid_step=options.grid_step if options else 2,deadline=until):
            if not _has_budget(budget,until):break
            budget['candidates']+=1;report['tap_candidates']+=1
            report['candidate_trials' if initialization else 'candidates']+=1
            if not trial.valid_state() or not _fixed_world_safe(original,trial,index):continue
            after=measure_attachments(trial,comps,index,plan)
            rank=attachment_rank(after)
            if rank is None or rank>baseline_rank:continue
            after_access=measure_taps(trial,comps,index,local_model)
            if (not after_access['measurable'] or
                    (after_access['errors'],after_access['outside_distance'])>=
                    (access['errors'],access['outside_distance'])):continue
            if hpwl_limit is not None and _hpwl(par,trial,nets,index)>hpwl_limit+EPS:continue
            safe=_checked_static(par,trial,nets,index,options,until)
            if safe is None:
                budget['exhausted']=True;report['tap_stop_reason']='attachment_budget';break
            if not safe:continue
            action['before']=access;action['after']=after_access
            current=trial;report['actions'].append(action)
            if initialization:
                report['state_legal_trials']+=1;report['legal_trials']+=1
            else:report['accepted']+=1
            break
    return current


def _parallel_channel_stage(par, current, original, comps, nets, index, plan, options,
                            budget, until, report, *, initialization, hpwl_limit=None):
    from netlist2schematic.plugins.attachment.attachment_channels import (measure_channels,
        channel_rank, channel_trials)
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    from netlist2schematic.routing.routing_rules import RoutingOptions
    options = options or RoutingOptions()
    model = plan.get('parallel_channels', {})
    if hpwl_limit is None:
        hpwl_limit = _hpwl(par, current, nets, index)*1.15
    for bank in model.get('banks', ()):
        if not _has_budget(budget, until):
            break
        local = dict(model, banks=[bank])
        before = measure_channels(current, comps, index, local)
        rank_before = channel_rank(before)
        if rank_before is None or not any(rank_before):
            continue
        legacy_before = attachment_rank(measure_attachments(current, comps, index, plan))
        if legacy_before is None:
            continue
        for trial, action in channel_trials(par, current, comps, index, plan, bank,
                options=options, initialization=initialization, deadline=until):
            if not _has_budget(budget, until):
                break
            budget['candidates'] += 1
            report['channel_candidates'] = report.get('channel_candidates', 0)+1
            report['candidate_trials' if initialization else 'candidates'] += 1
            if not trial.valid_state() or not _fixed_world_safe(original, trial, index):
                continue
            after = measure_channels(trial, comps, index, local)
            rank_after = channel_rank(after)
            legacy_after = attachment_rank(measure_attachments(trial, comps, index, plan))
            # Keep the existing violation/axis/inclusion terms. Node access may
            # increase owner distance; HPWL and final routed quality stay separate.
            if (rank_after is None or rank_after >= rank_before or legacy_after is None
                    or legacy_after[:4] > legacy_before[:4]
                    or _hpwl(par, trial, nets, index) > hpwl_limit+EPS):
                continue
            safe = _checked_static(par, trial, nets, index, options, until)
            if safe is None:
                budget['exhausted'] = True
                report['channel_stop_reason'] = 'attachment_budget'
                break
            if not safe:
                continue
            action.update(before=before, after=after,
                          old_organization_before=legacy_before, old_organization_after=legacy_after)
            trial.attachment_channel_limits = dict(getattr(trial, 'attachment_channel_limits', {}))
            trial.attachment_channel_limits[bank['id']] = rank_after
            trial.attachment_channel_halo = options.halo
            current = trial
            report['actions'].append(action)
            if initialization:
                report['state_legal_trials'] += 1
                report['legal_trials'] += 1
            else:
                report['accepted'] += 1
            break
    return current


def _control_host_world(pl,index,model):
    """Dependency snapshot: real terminal coordinates and exit directions."""
    result={}
    for node in model.get('nodes',()):
        try:
            result[node['net']]=tuple((name,pin,tuple(pl.pin_world(index[name],pin)),
                pl.pin_dir(index[name],pin)) for name,pin in sorted(node['hosts']))
        except (KeyError,TypeError,ValueError,IndexError):
            result[node['net']]=None
    return result


def _refresh_shared_control_hosts(par,current,original,comps,nets,index,plan,options,
                                  budget,until,report,checkpoint,*,hpwl_limit):
    """Invalidate only RC access results whose host moved after their finish.

    Earlier repair remains in place. This is a conditional use of the old
    remaining ledger, after all host-moving finishers; it opens no new budget.
    """
    from netlist2schematic.plugins.attachment.attachment_control_nodes import measure_nodes
    model=plan.get('control_nodes',{})
    actual=_control_host_world(current,index,model)
    changed=[n for n in model.get('nodes',()) if actual.get(n['net']) is not None
             and checkpoint.get(n['net']) is not None and actual[n['net']]!=checkpoint[n['net']]]
    detail=dict(version='shared-control-host-refresh-v1',changed_nodes=[n['net'] for n in changed],
        trials=0,accepted=0,status='unchanged_hosts',budget_reset=False,
        host_checkpoint=deepcopy(checkpoint),final_hosts=deepcopy(actual))
    report['shared_control_host_refresh']=detail
    if not changed:return current
    local=dict(plan,control_nodes=dict(model,nodes=changed))
    detail['before']=measure_nodes(current,comps,index,local['control_nodes'],plan['readability_plan']['junctions'])
    if not _has_budget(budget,until):
        detail['status']='remaining_budget_exhausted';detail['after']=deepcopy(detail['before'])
        return current
    start=budget['candidates'];accepted=report['accepted'];first=len(report['actions'])
    current=_shared_control_finish(par,current,original,comps,nets,index,local,options,
        budget,until,report,hpwl_limit=hpwl_limit)
    for action in report['actions'][first:]:action['phase']='final_host_refresh'
    detail.update(trials=budget['candidates']-start,accepted=report['accepted']-accepted,
        status='improved' if report['accepted']>accepted else 'no_verified_improvement',
        after=measure_nodes(current,comps,index,local['control_nodes'],plan['readability_plan']['junctions']))
    return current


def _shared_control_finish(par,current,original,comps,nets,index,plan,options,budget,until,report,*,hpwl_limit):
    from netlist2schematic.plugins.attachment.attachment_control_nodes import (
        measure_nodes,node_rank,node_trials,related_source_trials)
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    from netlist2schematic.routing.routing_rules import RoutingOptions
    options=options or RoutingOptions()
    model=plan.get('control_nodes',{});junctions=plan['readability_plan']['junctions']
    for node in model.get('nodes',()):
        if not _has_budget(budget,until):break
        # Sources remain separate guests. Repair an unsafe default marker or
        # reversed verified rail access without claiming a tail or copying nets.
        for trial,action in related_source_trials(current,comps,index,plan,node,until):
            if not _has_budget(budget,until):break
            source=index[action['source']]
            if not _rail_marker_direction_conflicts(current,source):break
            budget['candidates']+=1;report['candidates']+=1
            report['control_source_candidates']=report.get('control_source_candidates',0)+1
            if not trial.valid_state() or not _fixed_world_safe(original,trial,index):continue
            if _hpwl(par,trial,nets,index)>hpwl_limit+EPS:continue
            before=measure_attachments(current,comps,index,plan)
            after=measure_attachments(trial,comps,index,plan)
            if not after.get('measurable') or after.get('source_errors',0)>before.get('source_errors',0):continue
            safe=_checked_static(par,trial,nets,index,options,until)
            if safe is None:budget['exhausted']=True;break
            if not safe:continue
            action.update(source_distance_before=before.get('source_distance'),
                          source_distance_after=after.get('source_distance'))
            current=trial;report['actions'].append(action);report['accepted']+=1
            break
        local=dict(model,nodes=[node]);before=measure_nodes(current,comps,index,local,junctions)
        rank=node_rank(before)
        if rank is None:continue
        legacy=attachment_rank(measure_attachments(current,comps,index,plan))
        for trial,action in node_trials(current,comps,index,plan,node,until):
            if not _has_budget(budget,until):break
            budget['candidates']+=1;report['candidates']+=1
            report['control_node_candidates']=report.get('control_node_candidates',0)+1
            if not trial.valid_state() or not _fixed_world_safe(original,trial,index):continue
            measured=measure_nodes(trial,comps,index,local,junctions);candidate=node_rank(measured)
            old_rank=attachment_rank(measure_attachments(trial,comps,index,plan))
            if (candidate is None or candidate>=rank or old_rank is None or legacy is None
                    or old_rank[:4]>legacy[:4] or _hpwl(par,trial,nets,index)>hpwl_limit+EPS):continue
            safe=_checked_static(par,trial,nets,index,options,until)
            if safe is None:budget['exhausted']=True;break
            if not safe:continue
            action.update(before=before,after=measured)
            current=trial;report['actions'].append(action);report['accepted']+=1
            break
    return current


def initialize_attachments(pl, devices, comps, nets, index, plan, *, deadline=None,
                           budget=None, estimate=None, options=None):
    """Leave part of the same ledger for safe finishing after SA."""
    from netlist2schematic.plugins.attachment.attachment_budget import initialization_quota
    budget,started,until=_begin_budget(budget,deadline)
    quota=initialization_quota(budget,until,perf_counter)
    with quota:
        current,report=_initialize_attachments(pl,devices,comps,nets,index,plan,
            deadline=quota.deadline,budget=budget,estimate=estimate,options=options,
            _budget_window=(budget,started,quota.deadline))
    rail = report.get('initial_rail_clearance')
    if rail and rail.get('applied'):
        rail['retained_by_initialization'] = current is not pl
        if current is pl:
            rail['trial_actions'] = rail['actions']
            rail['trial_static_verification'] = rail.get('static_verification')
            rail.update(applied=False, rolled_back=True,
                        actions=[], stop_reason='initialization_returned_original',
                        static_verification=dict(valid=None, complete=False,
                            basis='original_seed_restored'))
    report['phase_budget']=quota.record
    # The class may have copied the ledger before the stage scope restored
    # temporary limits. Save only the restored shared state for SA/finishing.
    if isinstance(current,AttachmentPlacement):current.attachment_budget=deepcopy(budget)
    return current,report


def _initialize_attachments(pl, devices, comps, nets, index, plan, *, deadline=None,
                           budget=None, estimate=None, options=None,_budget_window=None):
    """Open and fill real interior slots, using a single bounded budget ledger."""
    budget, started, until = _budget_window or _begin_budget(budget, deadline)
    from netlist2schematic.plugins.attachment.attachment_compaction import reserve_finish
    reserve_finish(plan,budget)
    from netlist2schematic.plugins.attachment.attachment_tail_transistors import reserve_finish as reserve_tails
    reserve_tails(plan,budget,pl,index)
    from netlist2schematic.plugins.attachment.attachment_shunt_banks import reserve_finish as reserve_shunts
    reserve_shunts(plan,budget)
    from netlist2schematic.plugins.attachment.attachment_series_paths import reserve_finish as reserve_paths
    reserve_paths(plan,budget)
    from netlist2schematic.plugins.attachment.attachment_path_banks import reserve_finish as reserve_path_banks
    reserve_path_banks(plan,budget)
    from netlist2schematic.plugins.attachment.attachment_regulation import reserve_finish as reserve_regulation
    reserve_regulation(plan,budget)
    from netlist2schematic.plugins.attachment.attachment_stages import reserve_finish as reserve_stages
    reserve_stages(plan,budget)
    from netlist2schematic.plugins.attachment.attachment_stack_columns import reserve_finish as reserve_columns
    reserve_columns(plan,budget)
    from netlist2schematic.plugins.attachment.attachment_control_shunts import reserve_finish as reserve_control_shunts
    reserve_control_shunts(plan,budget)
    _reserve_control_finish(plan,budget)
    report = dict(version=VERSION, applied=False, status='skipped', candidate_trials=0,
                  source_candidate_trials=0, source_applied=False, passive_applied=False,
                  interior_candidate_trials=0, local_candidate_trials=0, group_trials={},
                  state_legal_trials=0, legal_trials=0, static_rejections=0,
                  slot_guard_rejections=0, rail_direction_rejections=0, actions=[], budget=budget)
    if not plan.get('supported'):
        report['reason'] = 'no_supported_attachments'
        _end_budget(budget, started)
        return pl, report
    if not _has_budget(budget, until):
        report['reason'] = 'attachment_budget'
        _end_budget(budget, started)
        return pl, report
    from netlist2schematic.layout.structured_placement import par
    current = _install(pl, comps, index, plan)
    before = measure_attachments(pl, comps, index, plan)
    report['before'] = deepcopy(before)
    common = estimate or (lambda p: score_attachments(p, comps, index, plan)['weighted_total'])
    report['estimate_before'] = common(current)
    report['estimate_basis'] = 'same_attachment_objective'
    from netlist2schematic.plugins.attachment.attachment_rail_clearance import normalize_initial_rails
    current, rail = normalize_initial_rails(par, current, comps, nets, index,
        budget, until, options=options)
    report['initial_rail_clearance'] = rail
    report['geometry_candidate_trials'] = rail['trials']
    report['candidate_trials'] += rail['trials']
    repeated=plan.get('repeated_stacks',{})
    if repeated.get('rows_applied') and all(current.cell_of[index[c['id']]][0]
            ==repeated['target_rows'][c['id']] for c in comps if c['symbol'] in ACTIVE):
        report['actions'].append(dict(kind='repeated_stack_display_rows',
            changes=deepcopy(repeated['changes']),original_electrical_levels_unchanged=True,
            scope='initial_display_row_plan_only; full geometry and routing checked later'))
    current = _input_block_seed(par, current, pl, comps, nets, index, plan,
        options, budget, until, report)
    current = _folded_seed(par,current,pl,comps,nets,index,plan,options,budget,until,report)
    from netlist2schematic.plugins.attachment.attachment_stages import arrange_stages
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    current=arrange_stages(par,current,comps,nets,index,plan,options,budget,until,report,
        initialization=True,hpwl_limit=_hpwl(par,pl,nets,index)*1.15)
    from netlist2schematic.plugins.attachment.attachment_stack_columns import arrange_columns
    current=arrange_columns(par,current,comps,nets,index,plan,options,budget,until,report,
        initialization=True,hpwl_limit=_hpwl(par,pl,nets,index)*1.15)
    current, expired = _joint_topology_stage(par,current,pl,comps,nets,index,plan,
        options,budget,until,report,initialization=True,family='conduction')
    if expired:
        _end_budget(budget,started)
        report.update(status='rolled_back',reason='attachment_budget',actions=[],after=deepcopy(before))
        return pl,report
    groups = sorted((g for g in _passive_motion_groups(plan) if g.get('requires_interior')), key=lambda g: (
        {'multi_path_bridge': 0, 'series_chain': 1, 'two_arm_join': 2,
         'parallel_bank': 3, 'single_attachment': 4}.get(g['kind'], 5), g['id']))
    for group in groups:
        if set(index[n] for n in group['members']) & _fixed_ids(pl, index):
            continue
        measured = measure_attachments(current, comps, index, plan)
        best = None
        specs = _slot_specs(current, group, index)
        if specs is None:
            continue
        allowance = max(1, (budget['max_candidates']-budget['candidates'])//max(1, len(groups)))
        for local, spec in enumerate(specs):
            if local >= allowance or not _has_budget(budget, until):
                break
            budget['candidates'] += 1; report['candidate_trials'] += 1
            report['interior_candidate_trials'] += 1
            report['group_trials'][group['id']] = report['group_trials'].get(group['id'], 0)+1
            trial = _joint_slot(current, comps, index, plan, group, spec)
            report['rail_direction_rejections'] += trial.attachment_slot_diagnostics['rail_direction_rejections']
            if not trial.valid_state() or not _fixed_world_safe(pl, trial, index):
                report['slot_guard_rejections'] += 1
                continue
            after = measure_attachments(trial, comps, index, plan)
            if not after['measurable'] or attachment_rank(after) >= attachment_rank(measured):
                continue
            report['state_legal_trials'] += 1
            safe = _checked_static(par, trial, nets, index, options, until)
            if safe is None:
                budget['exhausted'] = True
                _end_budget(budget, started)
                report.update(status='rolled_back', reason='attachment_budget', actions=[], after=deepcopy(before))
                return pl, report
            if not safe:
                report['static_rejections'] += 1
                continue
            report['legal_trials'] += 1
            cost = common(trial)
            rank = attachment_rank(after)
            key = rank[:4], spec['added_columns']+spec['added_rows'], rank[4:], cost
            if best is None or key < best[0]:
                best = key, trial, spec, after
        if best is not None:
            _, current, spec, after = best
            current.attachment_required_embeddings = {
                g['id']: tuple(_embedded(current, g, index)) for g in plan['groups']
                if g.get('requires_interior') and _embedded(current, g, index)}
            report['actions'].append(dict(kind='joint_junction_slot', group=group['id'],
                members=list(group['members']), column_cut=spec['cut'], columns_added=spec['added_columns'],
                rows_added=spec['added_rows'], junction_rows=list(current.attachment_junction_rows.get(group['id'], ())),
                slot_diagnostics=deepcopy(current.attachment_slot_diagnostics),
                row=spec['row'], anchor=spec['anchor'], embedded_members=_embedded(current, group, index)))
    # Allocate proven shared-node path channels before independent shunt trials
    # can consume the same bounded ledger. No extra SA or routing allowance.
    current = _parallel_channel_stage(par, current, pl, comps, nets, index, plan,
        options, budget, until, report, initialization=True)
    current, expired = _joint_topology_stage(par, current, pl, comps, nets, index, plan,
        options, budget, until, report, initialization=True)
    if expired:
        _end_budget(budget, started)
        report.update(status='rolled_back', reason='attachment_budget', actions=[], after=deepcopy(before))
        return pl, report
    from netlist2schematic.plugins.attachment.attachment_local import enum_local_attachment_trials
    local_groups = [g for g in _passive_motion_groups(plan) if not g.get('requires_interior')
                    and len(g['owner_junctions']) == 1]
    for group in local_groups:
        if not _has_budget(budget, until):
            break
        measured = measure_attachments(current, comps, index, plan)
        best = None
        allowance = max(1, (budget['max_candidates']-budget['candidates'])//max(1, len(local_groups)))
        for local, (trial, action) in enumerate(enum_local_attachment_trials(
                current, comps, index, plan, group, until)):
            if local >= allowance or not _has_budget(budget, until):
                break
            budget['candidates'] += 1; report['candidate_trials'] += 1
            report['local_candidate_trials'] += 1
            report['group_trials'][group['id']] = report['group_trials'].get(group['id'], 0)+1
            if not trial.valid_state() or not _fixed_world_safe(pl, trial, index):
                report['slot_guard_rejections'] += 1
                continue
            after = measure_attachments(trial, comps, index, plan)
            if attachment_rank(after) is None or attachment_rank(after) >= attachment_rank(measured):
                continue
            report['state_legal_trials'] += 1
            safe = _checked_static(par, trial, nets, index, options, until)
            if safe is None:
                budget['exhausted'] = True
                _end_budget(budget, started)
                report.update(status='rolled_back', reason='attachment_budget', actions=[], after=deepcopy(before))
                return pl, report
            if not safe:
                report['static_rejections'] += 1
                continue
            report['legal_trials'] += 1
            key = attachment_rank(after), common(trial)
            if best is None or key < best[0]:
                best = key, trial, action
        if best:
            _, current, action = best
            report['actions'].append(action)
    if plan.get('source_groups'):
        from netlist2schematic.plugins.attachment.attachment_sources import source_group_trials
        report['source_candidate_trials'] = 0
        for group in plan['source_groups']:
            if not _has_budget(budget, until):
                break
            measured = measure_attachments(current, comps, index, plan)
            best = None
            allowance = max(1, (budget['max_candidates']-budget['candidates'])//len(plan['source_groups']))
            model = dict(plan['source_model'], groups=[group])
            for trial, action in source_group_trials(current, comps, nets, index, model,
                    deadline=until, max_candidates=min(128, allowance)):
                if not _has_budget(budget, until):
                    break
                budget['candidates'] += 1; report['candidate_trials'] += 1
                report['source_candidate_trials'] += 1
                report['group_trials'][group['id']] = report['group_trials'].get(group['id'], 0)+1
                if not trial.valid_state() or not _fixed_world_safe(pl, trial, index):
                    report['slot_guard_rejections'] += 1
                    continue
                after = measure_attachments(trial, comps, index, plan)
                if not _source_improves(measured, after):
                    continue
                report['state_legal_trials'] += 1
                safe = _checked_static(par, trial, nets, index, options, until)
                if safe is None:
                    budget['exhausted'] = True
                    _end_budget(budget, started)
                    report.update(status='rolled_back', reason='attachment_budget', actions=[], after=deepcopy(before))
                    return pl, report
                if not safe:
                    report['static_rejections'] += 1
                    continue
                report['legal_trials'] += 1
                key = after['source_errors'], after['source_distance'], attachment_rank(after), common(trial)
                if best is None or key < best[0]:
                    best = key, trial, action
            if best:
                _, current, action = best
                report['actions'].append(action)
    if report['actions']:
        current, expired = _release_empty_rows(par, current, pl, comps, nets, index, plan,
                                               options, budget, until, report)
        if expired:
            _end_budget(budget, started)
            report.update(status='rolled_back', reason='attachment_budget', actions=[], after=deepcopy(before))
            return pl, report
    # Existing tap repair keeps its priority; the new finish receives only the
    # remaining original ledger after these previously implemented steps.
    budget.pop('candidate_ceiling',None)
    current=_tap_slot_stage(par,current,pl,comps,nets,index,plan,options,budget,until,report,
                            initialization=True)
    if report['actions']:
        applied = {action.get('group') for action in report['actions']}
        applied.update(group for action in report['actions'] for group in action.get('groups', ()))
        # Moving a host can create a valid interior relation even when its
        # passive guests did not move. Preserve that achieved relation too;
        # otherwise annealing can undo it simply because no guest action exists.
        host_created={g['id']:list(_embedded(current,g,index)) for g in _passive_motion_groups(plan)
            if g.get('requires_interior') and _embedded(current,g,index) and any(
                current.pos_of(index[n])!=pl.pos_of(index[n]) or current.orient[index[n]]!=pl.orient[index[n]]
                for n in g.get('host_members',()))}
        protected_groups = [g for g in _passive_motion_groups(plan) if g['id'] in applied or any(
            current.cell_of[index[n]] != pl.cell_of[index[n]] or current.orient[index[n]] != pl.orient[index[n]]
            for n in g['members']) or g['id'] in host_created]
        for identifier,names in host_created.items():
            current.attachment_required_embeddings[identifier]=sorted(set(names)|set(
                current.attachment_required_embeddings.get(identifier,())))
        report['host_created_embeddings_protected']=host_created
        protected = {n for g in protected_groups for n in g['members']}
        shapes = measure_readability(current, comps, index, plan['readability_plan'])
        current.attachment_shape_limits = {g['id']: {key: g[key] for key in
            ('orientation_errors', 'order_errors', 'shape_error')} for g in shapes['patterns']
            if set(g['members']) <= protected and g['kind'] in ('series_chain', 'parallel_bank', 'two_arm_join')}
        current.attachment_axis_limits = tuple((deepcopy(axis), abs(
            current.pin_world(index[axis['a']], axis['pin_a'])[0]
            -current.pin_world(index[axis['b']], axis['pin_b'])[0])) for axis in plan.get('host_axes', ()))
        current.attachment_port_limits = {g['id']: _port_errors(current, comps, index, plan, g)
                                         for g in protected_groups}
        if plan.get('source_groups'):
            from netlist2schematic.plugins.attachment.attachment_sources import measure_source_groups
            source = measure_source_groups(current, comps, nets, index, plan['source_model'])
            current.attachment_source_error_limits = {r['id']: r['source_errors'] for r in source['records']}
            current.attachment_source_port_limits = {g['id']: _port_errors(current, comps, index, plan, g)
                                                     for g in plan['source_groups']}
            current.attachment_source_chain_limits = {g['id']: _source_chain_errors(current, comps, index, g)
                                                      for g in plan['source_groups']}
        report['source_applied'] = any(a['kind'] == 'source_group_attachment' for a in report['actions'])
        report['passive_applied'] = any(a['kind'] not in ('source_group_attachment','neutral_stage_layout',
            'repeated_stack_display_rows','repeated_stack_columns') for a in report['actions'])
        report['active_stage_applied'] = any(a['kind']=='neutral_stage_layout' for a in report['actions'])
        report['repeated_rows_applied']=any(a['kind']=='repeated_stack_display_rows' for a in report['actions'])
        report['stack_columns_applied']=any(a['kind']=='repeated_stack_columns' for a in report['actions'])
        report.update(applied=True, status='applied', reason='junction_organization',
            after=measure_attachments(current, comps, index, plan), estimate_after=common(current))
        report['estimate_ratio'] = report['estimate_after']/report['estimate_before'] if report['estimate_before'] else None
        _end_budget(budget, started)
        current.attachment_budget = deepcopy(budget)
        report['display_row_origin'] = list(current.attachment_row_origin)
        return current, report
    report.update(reason='no_legal_organization_improvement', after=deepcopy(before))
    _end_budget(budget, started)
    return pl, report


def _folded_seed(par,current,original,comps,nets,index,plan,options,budget,until,report):
    from netlist2schematic.plugins.attachment.attachment_folded import folded_trials,measure_folded,folded_rank
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    model=plan.get('folded_cores',{})
    before=measure_folded(current,comps,index,model)
    detail=dict(before=before,candidates=0,applied_ids=[],actions=[],skipped=[])
    report['folded_initialization']=detail
    groups=sorted(model.get('groups',()),key=lambda g:(mean_row(current,g,index),
        sum(current.pos_of(index[n])[0] for n in g['members'])))
    limit=_hpwl(par,current,nets,index)*1.15
    allowance=max(1,8//max(1,len(groups)))
    for group in groups:
        if {index[n] for n in group['members']}&_fixed_ids(current,index):
            detail['skipped'].append(dict(id=group['id'],reason='fixed_core_member'))
            continue
        baseline=folded_rank(measure_folded(current,comps,index,model))
        if baseline is None:break
        best=None
        for number,(trial,action) in enumerate(folded_trials(current,comps,index,model,group,deadline=until)):
            if number>=allowance or detail['candidates']>=8 or not _has_budget(budget,until):break
            budget['candidates']+=1;report['candidate_trials']+=1;detail['candidates']+=1
            trial=_install(trial,comps,index,plan)
            if not trial.valid_state() or not _fixed_world_safe(original,trial,index):continue
            measured=measure_folded(trial,comps,index,model);rank=folded_rank(measured)
            hpwl=_hpwl(par,trial,nets,index)
            if rank is None or rank>baseline or hpwl>limit+EPS:continue
            safe=_checked_static(par,trial,nets,index,options,until)
            if safe is None:
                budget['exhausted']=True;break
            if not safe:continue
            key=rank,hpwl
            if best is None or key<best[0]:best=key,trial,dict(action,before=baseline,after=measured)
        if best:
            _,current,action=best
            detail['applied_ids'].append(group['id']);detail['actions'].append(action)
            report['actions'].append(action)
    detail['after']=measure_folded(current,comps,index,model)
    return current


def mean_row(pl,group,index):
    return sum(pl.cell_of[index[n]][0] for n in group['members'])/len(group['members'])


def _input_block_seed(par, current, original, comps, nets, index, plan, options,
                       budget, until, report):
    from netlist2schematic.plugins.attachment.attachment_input_blocks import (
        input_trials, measure_input_blocks, input_rank)
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    model = plan.get('input_blocks', {})
    from netlist2schematic.plugins.attachment.attachment_outputs import measure_outputs,output_rank
    output_model = plan.get('output_merges',{})
    before = measure_input_blocks(current, comps, index, model)
    output_before = measure_outputs(current,comps,index,output_model,model)
    report['input_initialization'] = dict(before=before, output_before=output_before, candidates=0, applied=False)
    if not model.get('pairs') or input_rank(before) is None:
        return current
    hpwl_limit = _hpwl(par, current, nets, index)*1.15
    best = None
    for trial, action in input_trials(current, comps, index, model,
            move_tails=plan['policy']=='all', deadline=until,output_model=output_model):
        if not _has_budget(budget, until) or report['input_initialization']['candidates'] >= 8:
            break
        budget['candidates'] += 1; report['candidate_trials'] += 1
        report['input_initialization']['candidates'] += 1
        trial = _install(trial, comps, index, plan)
        if not trial.valid_state() or not _fixed_world_safe(original, trial, index):
            continue
        measured = measure_input_blocks(trial, comps, index, model)
        rank = input_rank(measured)
        output_after = measure_outputs(trial,comps,index,output_model,model)
        output_terms = output_rank(output_after)
        hpwl = _hpwl(par, trial, nets, index)
        # A newly verified shared-source MOS pair may already be contiguous.
        # Install its leaf without inventing a positional improvement. This
        # makes subsequent existing block moves/undo protect it as one unit.
        missing_mos = [p['id'] for p in model['pairs'] if p.get('kind')=='differential_pair'
            and {index[n] for n in p['members']} not in [set(b) for b in current.blocks]
            and {index[n] for n in p['members']} in [set(b) for b in trial.blocks]]
        same_world = (trial.cell_of==current.cell_of and trial.orient==current.orient
            and trial.column_x==current.column_x and trial.row_y==current.row_y
            and all(tuple(trial.pos_of(i))==tuple(current.pos_of(i)) for i in range(len(comps))))
        policy_only = bool(missing_mos and same_world
            and (rank,output_terms)==(input_rank(before),output_rank(output_before)))
        if (rank is None or output_terms is None or output_rank(output_before) is None
                or ((rank,output_terms) >= (input_rank(before),output_rank(output_before)) and not policy_only)
                or hpwl > hpwl_limit+EPS):
            continue
        safe = _checked_static(par, trial, nets, index, options, until)
        if safe is None:
            budget['exhausted'] = True
            break
        if not safe:
            continue
        key = rank, output_terms, hpwl
        if best is None or key < best[0]:
            detail = dict(action, before=before, after=measured,output_after=output_after)
            if missing_mos:
                detail.update(installed_mos_leaves=missing_mos,policy_only_adoption=policy_only)
            best = key, trial, detail
    if best:
        _, current, action = best
        report['actions'].append(action)
        report['input_initialization'].update(applied=True, after=action['after'],output_after=action['output_after'])
    return current


def _core_access_finish(par, current, original, comps, nets, index, plan, options,
                        budget, until, report, *, hpwl_limit):
    from netlist2schematic.plugins.attachment.attachment_cores import measure_cores, core_rank
    from netlist2schematic.plugins.attachment.attachment_core_access import access_trials
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    model = plan.get('core_rows', {})
    report['core_access_before'] = measure_cores(current, comps, index, model)
    report['core_access_trials'] = 0
    for core in model.get('cores', ()):
        before = measure_cores(current, comps, index, model)
        old_rank = core_rank(before)
        if old_rank is None:
            continue
        primary = attachment_rank(measure_attachments(current, comps, index, plan))
        best = None
        for trial, action in access_trials(current, comps, index, model, core,
                grid_step=options.grid_step if options is not None else 2, deadline=until):
            if not _has_budget(budget, until) or report['core_access_trials'] >= 12:
                break
            budget['candidates'] += 1; report['candidates'] += 1; report['core_access_trials'] += 1
            if not trial.valid_state() or not _fixed_world_safe(original, trial, index):
                continue
            after = measure_cores(trial, comps, index, model)
            rank = core_rank(after)
            legacy = attachment_rank(measure_attachments(trial, comps, index, plan))
            hpwl = _hpwl(par, trial, nets, index)
            if (rank is None or rank >= old_rank or legacy is None or primary is None
                    or legacy[:4] > primary[:4] or hpwl > hpwl_limit+EPS):
                continue
            safe = _checked_static(par, trial, nets, index, options, until)
            if safe is None:
                budget['exhausted'] = True
                report['reason'] = 'attachment_budget'
                break
            if not safe:
                continue
            key = rank, hpwl
            if best is None or key < best[0]:
                best = key, trial, dict(action, before=before, after=after)
        if best:
            _, current, action = best
            report['actions'].append(action); report['accepted'] += 1
    report['core_access_after'] = measure_cores(current, comps, index, model)
    return current


def refine_attachments(par, pl, comps, nets, index, metadata, *, options=None,
                       deadline=None, budget=None):
    """Share the old remainder, including time for an evidenced tail finish."""
    from contextlib import nullcontext
    from netlist2schematic.plugins.attachment.attachment_budget import before_tail_quota
    saved=metadata.get('layout_attachments',{});plan=saved.get('plan')
    budget=budget if budget is not None else saved.get('budget',getattr(pl,'attachment_budget',None))
    budget,started,until=_begin_budget(budget,deadline)
    quota=(before_tail_quota(budget,until,perf_counter)
           if plan and plan.get('tail_transistors',{}).get('groups') else None)
    with quota if quota is not None else nullcontext():
        current,result=_refine_attachments(par,pl,comps,nets,index,metadata,options=options,
            deadline=deadline,budget=budget,_budget_window=(budget,started,until),_prefix_quota=quota)
    if quota is not None:
        result['layout_attachments']['refinement']['before_reserved_finish_budget']=quota.record
    if isinstance(current,AttachmentPlacement):current.attachment_budget=deepcopy(budget)
    return current,result


def _refine_attachments(par, pl, comps, nets, index, metadata, *, options=None,
                       deadline=None, budget=None,_budget_window=None,_prefix_quota=None):
    """A slot-preserving, fixed-axis local finish; never opens another budget."""
    result = deepcopy(metadata)
    saved = result.get('layout_attachments', {})
    plan = saved.get('plan')
    budget = budget if budget is not None else saved.get('budget', getattr(pl, 'attachment_budget', None))
    budget, started, until = _budget_window or _begin_budget(budget, deadline)
    shared_until=until
    if _prefix_quota is not None:until=_prefix_quota.deadline
    if plan:
        from netlist2schematic.plugins.attachment.attachment_compaction import reserve_finish
        reserve_finish(plan,budget)
        from netlist2schematic.plugins.attachment.attachment_tail_transistors import reserve_finish as reserve_tails
        reserve_tails(plan,budget,pl,index)
        from netlist2schematic.plugins.attachment.attachment_shunt_banks import reserve_finish as reserve_shunts
        reserve_shunts(plan,budget)
        from netlist2schematic.plugins.attachment.attachment_series_paths import reserve_finish as reserve_paths
        reserve_paths(plan,budget)
        from netlist2schematic.plugins.attachment.attachment_path_banks import reserve_finish as reserve_path_banks
        reserve_path_banks(plan,budget)
        from netlist2schematic.plugins.attachment.attachment_regulation import reserve_finish as reserve_regulation
        reserve_regulation(plan,budget)
        from netlist2schematic.plugins.attachment.attachment_stages import reserve_finish as reserve_stages
        reserve_stages(plan,budget)
        from netlist2schematic.plugins.attachment.attachment_stack_columns import reserve_finish as reserve_columns
        reserve_columns(plan,budget)
        from netlist2schematic.plugins.attachment.attachment_control_shunts import reserve_finish as reserve_control_shunts
        reserve_control_shunts(plan,budget)
        _reserve_control_finish(plan,budget)
    report = dict(version=VERSION, applied=False, candidates=0, accepted=0, actions=[], budget=budget)
    # An unsafe early scene can reject every initial attachment proposal. SA
    # may repair that unrelated conflict later. Recheck the actual post-SA
    # seed before adopting the existing policy; never reuse an unsafe seed or
    # open another ledger. Established AttachmentPlacement seeds are unchanged.
    if (plan and plan.get('supported') and not isinstance(pl, AttachmentPlacement)
            and str(metadata.get('candidate_id')) not in ('0', '1')):
        resumed = dict(installed=False, reason='shared_attachment_budget')
        report['deferred_seed'] = resumed
        if (_has_budget(budget, until) or (budget.get('tail_transistor_reserve',0)
                and not budget.get('exhausted') and perf_counter()<until
                and budget['candidates']<budget['max_candidates']-budget.get('band_compaction_reserve',0))):
            trial = _install(pl, comps, index, plan)
            resumed['reason'] = 'invalid_post_sa_state'
            if trial.valid_state() and not _order_errors(trial, comps, index, plan):
                safe = _checked_static(par, trial, nets, index, options, until)
                if safe is None:
                    budget['exhausted'] = True
                    resumed['reason'] = 'shared_attachment_budget'
                elif safe:
                    pl = trial
                    resumed.update(installed=True, reason='post_sa_static_valid',
                        rows_axes_poses_unchanged=True)
                else:
                    resumed['reason'] = 'post_sa_static_conflict'
    if not plan or not plan.get('supported') or not isinstance(pl, AttachmentPlacement):
        report['reason'] = 'no_attachment_seed'
    else:
        current = deepcopy(pl)
        baseline = measure_attachments(current, comps, index, plan)
        report['before'] = baseline
        from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
        baseline_hpwl = _hpwl(par, pl, nets, index)
        report.update(estimated_hpwl_before=baseline_hpwl, estimated_hpwl_limit=baseline_hpwl*1.15,
                      hpwl_rejections=0)
        from netlist2schematic.plugins.attachment.attachment_regulation import finish_regions
        current=finish_regions(par,current,comps,nets,index,plan,options,budget,until,report,
            hpwl_limit=baseline_hpwl*1.15)
        current = _parallel_channel_stage(par, current, pl, comps, nets, index, plan,
            options, budget, until, report, initialization=False, hpwl_limit=baseline_hpwl*1.15)
        current=_tap_slot_stage(par,current,pl,comps,nets,index,plan,options,budget,until,report,
                                initialization=False,hpwl_limit=baseline_hpwl*1.15)
        current, expired = _joint_topology_stage(par,current,pl,comps,nets,index,plan,
            options,budget,until,report,initialization=False,hpwl_limit=baseline_hpwl*1.15,family='conduction')
        if expired:
            report['reason']='attachment_budget'
        current, expired = _joint_topology_stage(par, current, pl, comps, nets, index, plan,
            options, budget, until, report, initialization=False, hpwl_limit=baseline_hpwl*1.15)
        if expired:
            report['reason'] = 'attachment_budget'
        baseline = measure_attachments(current, comps, index, plan)
        passive_groups = _passive_motion_groups(plan)
        for group_number, group in enumerate(passive_groups):
            if not _has_budget(budget, until):
                break
            ids = {index[n] for n in group['members']}
            if ids & set(current.attachment_fixed):
                continue
            from netlist2schematic.plugins.attachment.attachment_local import enum_local_attachment_trials
            if len(group['owner_junctions']) == 1:
                proposals = enum_local_attachment_trials(current, comps, index, plan, group, until)
            else:
                def shifted():
                    for dr, dc in ((0,-1),(0,1),(-1,0),(1,0)):
                        trial = deepcopy(current)
                        cells = list(trial.cell_of)
                        for i in ids:
                            r, c = cells[i]; cells[i] = r+dr, c+dc
                        trial.set_state(cells, trial.orient)
                        yield trial, dict(kind='attachment_local_move', group=group['id'], delta=[dr,dc])
                proposals = shifted()
            if report.get('deferred_seed', {}).get('installed'):
                # A late repair has only the old remainder. Share it among
                # remaining guests rather than letting the first ray family
                # consume all opportunities for the other port attachments.
                from itertools import islice
                remaining = max(0, budget['max_candidates']-budget['candidates']
                    -budget.get('band_compaction_reserve', 0)-budget.get('tail_transistor_reserve',0))
                count = len(passive_groups)-group_number+len(plan.get('source_groups', ()))
                allowance = max(1, remaining//max(1, count))
                report.setdefault('deferred_group_limits', {})[group['id']] = allowance
                proposals = islice(proposals, allowance)
            for trial, action in proposals:
                if not _has_budget(budget, until):
                    break
                budget['candidates'] += 1; report['candidates'] += 1
                if not trial.valid_state():
                    continue
                measured = measure_attachments(trial, comps, index, plan)
                # Local shape/locality is a benefit even if the outer bbox is unchanged.
                if attachment_rank(measured) is None or attachment_rank(measured) >= attachment_rank(baseline):
                    continue
                if _hpwl(par, trial, nets, index) > baseline_hpwl*1.15+EPS:
                    report['hpwl_rejections'] += 1
                    continue
                safe = _checked_static(par, trial, nets, index, options, until)
                if safe is None:
                    budget['exhausted'] = True
                    report['reason'] = 'attachment_budget'
                    break
                if not safe:
                    continue
                current, baseline = trial, measured
                report['accepted'] += 1
                report['actions'].append(action)
        if plan.get('source_groups'):
            from netlist2schematic.plugins.attachment.attachment_sources import source_group_trials, source_axis_trials
            report['source_candidates'] = 0
            from netlist2schematic.plugins.attachment.attachment_tail_source_finish import reserve_finish as reserve_tail_sources
            reserve_tail_sources(current,comps,index,plan,budget,until)
            report['tail_source_reserved_trials']=budget.get('tail_source_axis_reserve',0)
            for group in plan['source_groups']:
                if not _has_budget(budget, until):
                    break
                model = dict(plan['source_model'], groups=[group])
                remaining = min(128, max(0, budget['max_candidates']-budget['candidates']))
                def source_proposals():
                    yield from source_axis_trials(current,comps,index,model,deadline=until,
                        grid_step=options.grid_step if options is not None else 2)
                    # Resolve 'current' after an accepted axis adjustment.
                    yield from source_group_trials(current,comps,nets,index,model,
                        deadline=until,max_candidates=remaining)
                for trial, action in source_proposals():
                    if not _has_budget(budget, until):
                        break
                    budget['candidates'] += 1; report['candidates'] += 1; report['source_candidates'] += 1
                    if not trial.valid_state():
                        continue
                    measured = measure_attachments(trial, comps, index, plan)
                    if not _source_improves(baseline, measured):
                        continue
                    if _hpwl(par, trial, nets, index) > baseline_hpwl*1.15+EPS:
                        report['hpwl_rejections'] += 1
                        continue
                    safe = _checked_static(par, trial, nets, index, options, until)
                    if safe is None:
                        budget['exhausted'] = True
                        report['reason'] = 'attachment_budget'
                        break
                    if not safe:
                        continue
                    current, baseline = trial, measured
                    report['accepted'] += 1
                    report['actions'].append(action)
        budget.pop('candidate_ceiling',None)
        current=_shared_control_finish(par,current,pl,comps,nets,index,plan,options,
            budget,until,report,hpwl_limit=baseline_hpwl*1.15)
        control_host_checkpoint=_control_host_world(current,index,plan.get('control_nodes',{}))
        current=_core_access_finish(par,current,pl,comps,nets,index,plan,options,
            budget,until,report,hpwl_limit=baseline_hpwl*1.15)
        from netlist2schematic.plugins.attachment.attachment_controllers import finish_controllers
        current=finish_controllers(par,current,comps,nets,index,plan,options,
            budget,until,report,hpwl_limit=baseline_hpwl*1.15)
        if _prefix_quota is not None:
            _prefix_quota.__exit__(None,None,None)
            report['before_reserved_finish_budget']=_prefix_quota.record
            until=shared_until
        from netlist2schematic.plugins.attachment.attachment_tail_transistors import finish_tails
        current=finish_tails(par,current,comps,nets,index,plan,options,
            budget,until,report,result,hpwl_limit=baseline_hpwl*1.15)
        from netlist2schematic.plugins.attachment.attachment_compaction import finish_compaction
        current=finish_compaction(par,current,comps,nets,index,plan,options,
            budget,until,report,result)
        from netlist2schematic.plugins.attachment.attachment_port_banks import finish_banks
        current=finish_banks(par,current,comps,nets,index,plan,options,
            budget,until,report,hpwl_limit=baseline_hpwl*1.15)
        from netlist2schematic.plugins.attachment.attachment_shunt_banks import finish_shunts
        current=finish_shunts(par,current,comps,nets,index,plan,options,
            budget,until,report,hpwl_limit=baseline_hpwl*1.15)
        from netlist2schematic.plugins.attachment.attachment_series_paths import finish_paths
        current=finish_paths(par,current,comps,nets,index,plan,options,
            budget,until,report,hpwl_limit=baseline_hpwl*1.15)
        from netlist2schematic.plugins.attachment.attachment_path_banks import finish_banks as finish_path_banks
        current=finish_path_banks(par,current,comps,nets,index,plan,options,
            budget,until,report,hpwl_limit=baseline_hpwl*1.15)
        from netlist2schematic.plugins.attachment.attachment_stages import arrange_stages
        current=arrange_stages(par,current,comps,nets,index,plan,options,budget,until,report,
            initialization=False,hpwl_limit=baseline_hpwl*1.15)
        from netlist2schematic.plugins.attachment.attachment_stack_columns import arrange_columns
        current=arrange_columns(par,current,comps,nets,index,plan,options,budget,until,report,
            initialization=False,hpwl_limit=baseline_hpwl*1.15)
        from netlist2schematic.plugins.attachment.attachment_control_shunts import finish_shunts as finish_control_shunts
        current=finish_control_shunts(par,current,comps,nets,index,plan,options,budget,until,report,
            hpwl_limit=baseline_hpwl*1.15)
        current=_refresh_shared_control_hosts(par,current,pl,comps,nets,index,plan,options,
            budget,until,report,control_host_checkpoint,hpwl_limit=baseline_hpwl*1.15)
        from netlist2schematic.plugins.attachment.attachment_tap_offsets import finish_bands as finish_tap_bands
        current=finish_tap_bands(par,current,comps,nets,index,plan,options,
            budget,until,report,hpwl_limit=baseline_hpwl*1.15)
        from netlist2schematic.plugins.attachment.attachment_tail_source_finish import finish_axes as finish_tail_source_axes
        current=finish_tail_source_axes(par,current,comps,nets,index,plan,options,
            budget,until,report,hpwl_limit=baseline_hpwl*1.15)
        from netlist2schematic.plugins.attachment.attachment_source_axis import finish_source_axes
        current=finish_source_axes(par,current,comps,nets,index,plan,options,
            budget,until,report,hpwl_limit=baseline_hpwl*1.15)
        result['layout_grid'] = dict(column_x=list(current.column_x),row_y=list(current.row_y))
        baseline=measure_attachments(current,comps,index,plan)
        report.update(applied=bool(report['accepted']), after=baseline)
        pl = current
    saved.update(refinement=report, budget=budget)
    if report.get('deferred_seed', {}).get('installed') and report['applied']:
        saved.update(applied=True, deferred_seed_applied=True)
    if plan:
        saved['metrics'] = measure_attachments(pl, comps, index, plan)
    result['layout_attachments'] = saved
    _end_budget(budget, started)
    return pl, result


def finalize_attachment_state(pl, checkpoint):
    """Keep later legacy orientation/floor helpers inside the frozen contract.

    The checkpoint is captured after SA. This is a state guard, not another
    placement or search pass; authoritative geometry/routing still follows.
    """
    if not isinstance(checkpoint, AttachmentPlacement):
        return pl, dict(version=VERSION, status='not_applicable', reverted=False)
    safe = isinstance(pl, AttachmentPlacement) and pl.valid_state()
    if safe:
        safe = (pl.attachment_plan == checkpoint.attachment_plan
                and pl.attachment_axes == checkpoint.attachment_axes
                and pl.attachment_required_embeddings == checkpoint.attachment_required_embeddings
                and pl.attachment_shape_limits == checkpoint.attachment_shape_limits
                and pl.attachment_port_limits == checkpoint.attachment_port_limits
                and pl.attachment_source_ids == checkpoint.attachment_source_ids
                and pl.attachment_source_rows == checkpoint.attachment_source_rows
                and pl.attachment_source_error_limits == checkpoint.attachment_source_error_limits
                and pl.attachment_source_port_limits == checkpoint.attachment_source_port_limits
                and pl.attachment_source_chain_limits == checkpoint.attachment_source_chain_limits
                and getattr(pl,'attachment_stack_bundles',()) == getattr(checkpoint,'attachment_stack_bundles',())
                and pl.fixed_rows == checkpoint.fixed_rows)
    if safe:
        return pl, dict(version=VERSION, status='kept', reverted=False,
                        scope='frozen_axes_slots_and_fixed_world')
    return deepcopy(checkpoint), dict(version=VERSION, status='restored_checkpoint', reverted=True,
        reason='post_sa_change_violated_attachment_contract', scope='frozen_axes_slots_and_fixed_world')


def _rail_marker_direction_conflicts(pl, i):
    """Describe terminal orders that fold a default vertical rail stub back."""
    from netlist2schematic.layout.structured_placement import par
    nodes = pl.comps[i]['nodes']
    conflicts = []
    for rail_pin, net in nodes.items():
        symbol = par.power_marker_sym(net)
        if not symbol:
            continue
        rail = pl.pin_world(i, rail_pin)
        sign = -1 if symbol == 'vdd-port' else 1
        for pin, other in nodes.items():
            if pin != rail_pin and not par.power_marker_sym(other):
                signal = pl.pin_world(i, pin)
                if sign*(rail[1]-signal[1]) < -EPS:
                    conflicts.append(dict(device=pl.comps[i]['id'], orientation=pl.orient[i],
                        rail_net=net, rail_pin=rail_pin, rail_point=list(rail),
                        signal_net=other, signal_pin=pin, signal_point=list(signal),
                        marker_direction='north' if sign < 0 else 'south',
                        reason='default_rail_stub_reverses_signal_terminal_order'))
    return conflicts
