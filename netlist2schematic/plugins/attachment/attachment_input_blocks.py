"""Consume verified input pairs as small leaves and BJT collector-side banks.

Evidence comes from the existing terminal validator. Collector banks require
actual collector-to-base access, not shared supply, bias or input names. A
tail's remote node stays an ordinary net when its power role is unknown.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from itertools import product
from math import isfinite
from statistics import mean
from time import perf_counter

VERSION = 'verified-input-blocks-v2'
EPS = 1e-8


def compile_input_blocks(devices, comps, structs, rows, *, formal_ports=()):
    result = dict(version=VERSION, pairs=[], banks=[], tails=[], emitter_merges=[], skipped=[])
    if sum(d.get('cls') in ('BJT','MOS') for d in devices.values()) < 2:
        return result
    from netlist2schematic.plugins.layout_shared.layout_evidence import build_layout_evidence
    evidence = build_layout_evidence(devices, structs, formal_ports=formal_ports)
    result['evidence_diagnostics'] = deepcopy(evidence['diagnostics'])
    result['formal_port_evidence'] = deepcopy(formal_ports)
    by_id = {c['id']: c for c in comps}
    roles = evidence['net_roles']
    power = lambda net: roles.get(net, {}).get('role') == 'power'
    incidence = defaultdict(list)
    for c in comps:
        for pin, net in c['nodes'].items():
            incidence[net].append((c['id'], pin))
    for relation in evidence['relations']:
        kind = relation['kind']
        if kind not in ('bjt_differential_pair','differential_pair'):
            continue
        # Ordinary MOS pairs already have legacy owners. This rollout only
        # fills a missing shared-source leaf, never replaces a known core.
        names = relation['members']
        family = {devices[n]['cls'] for n in names}
        is_mos = family == {'MOS'}
        if is_mos and not relation['roles'].get('validated_shared_source'):
            continue
        if len(names) != 2 or len({rows[n] for n in names}) != 1:
            result['skipped'].append(dict(id=relation['id'], reason='input_pair_rows_incompatible'))
            continue
        result['pairs'].append(dict(id=relation['id'], members=list(names),
            roles=deepcopy(relation['roles']), evidence=deepcopy(relation['evidence']),
            terminal_contract={n: deepcopy(by_id[n]['nodes']) for n in names}))
        result['pairs'][-1]['kind'] = 'differential_pair'
        result['pairs'][-1]['family'] = 'MOS' if is_mos else 'BJT'
        if is_mos:
            # Shared-source selection depends on the other users, tail and
            # comparable branch incidence, not only the two selected devices.
            result['pairs'][-1]['evidence_contract'] = {
                c['id']:dict(symbol=c['symbol'],nodes=deepcopy(c['nodes'])) for c in comps}
    ownership = Counter(n for pair in result['pairs'] for n in pair['members'])
    result['pairs'] = [p for p in result['pairs'] if all(ownership[n] == 1 for n in p['members'])]
    for pair in result['pairs']:
        signal_nodes = {net for n in pair['members'] for net in by_id[n]['nodes'].values() if not power(net)}
        pair['guest_members'] = sorted(c['id'] for c in comps
            if c['symbol'] in ('resistor','capacitor','inductor','diode','current-source','voltage-source')
            and signal_nodes.intersection(c['nodes'].values()))
    at_collector = defaultdict(list)
    for pair in result['pairs']:
        if any(by_id[n]['symbol'] not in ('npn','pnp') for n in pair['members']):
            continue
        for name in pair['members']:
            net = devices[name]['c']
            if not power(net):
                at_collector[net].append((pair['id'], name))
        tails = pair['roles'].get('exclusive_tails', ())
        net = pair['roles']['source_net']
        if len(tails) == 1 and devices[tails[0]]['cls'] == 'ISRC':
            tail = tails[0]; d = devices[tail]
            pin = '+' if d['p'] == net else '-' if d['n'] == net else None
            if pin and set(incidence[net]) == {(n, 'E') for n in pair['members']} | {(tail, pin)}:
                other = d['n'] if pin == '+' else d['p']
                result['tails'].append(dict(device=tail, pair=pair['id'], pin=pin,
                    members=list(pair['members']), source_net=net, remote_net=other,
                    remote_role=roles.get(other, {}).get('role', 'unknown'),
                    terminal_contract=deepcopy(by_id[tail]['nodes'])))
    pair_by_id = {p['id']: p for p in result['pairs']}
    for net, users in sorted(at_collector.items()):
        identifiers = [p for p, _ in users]
        if not 2 <= len(identifiers) <= 4 or len(set(identifiers)) != len(identifiers):
            continue
        if len({rows[n] for p in identifiers for n in pair_by_id[p]['members']}) != 1:
            continue
        selected = {n for _, n in users}
        if len({devices[n]['type'] for n in selected}) != 1:
            continue
        collectors = {n for n, pin in incidence[net] if devices[n]['cls'] == 'BJT' and pin == 'C'}
        followers = [n for n, pin in incidence[net] if devices[n]['cls'] == 'BJT'
                     and pin == 'B' and power(devices[n]['c']) and not power(devices[n]['e'])]
        if (collectors != selected or len(followers) != 1
                or devices[followers[0]]['type'] != devices[next(iter(selected))]['type']):
            continue
        result['banks'].append(dict(id='input_collector_bank:' + net, net=net,
            pairs=identifiers, collector_members=dict(users), follower=followers[0],
            follower_contract=deepcopy(by_id[followers[0]]['nodes']),
            members=[n for p in identifiers for n in pair_by_id[p]['members']],
            guest_members=sorted({n for p in identifiers for n in pair_by_id[p]['guest_members']}),
            evidence='two_or_more_verified_pairs_share_collector_access_to_one_collector_supplied_bjt_base'))
    claims = Counter(p for bank in result['banks'] for p in bank['pairs'])
    ambiguous = [b for b in result['banks'] if any(claims[p] > 1 for p in b['pairs'])]
    result['skipped'].extend(dict(id=b['id'], reason='ambiguous_collector_bank_parent') for b in ambiguous)
    result['banks'] = [b for b in result['banks'] if b not in ambiguous]
    if result['banks']:
        from netlist2schematic.plugins.layout_shared.layout_evidence import extend_branch_evidence
        branches = extend_branch_evidence(devices,evidence)
        result['emitter_merges'] = [deepcopy(r) for r in branches['relations'] if r['kind']=='common_emitter_merge']
    return result


def owner_blocks(blocks, comps, model):
    """Reuse an identical confirmed leaf; preserve every unrelated existing owner."""
    pairs = [set(p['members']) for p in model.get('pairs', ())]
    protected = [set(comps[i]['id'] for i in block) for block in blocks if len(block) > 1]
    # The common recognizer may already own this exact leaf. Reusing it
    # must not look like a conflicting partial overlap.
    if any(a & b and a != b for a in pairs for b in protected):
        return None
    taken = set().union(*pairs) if pairs else set()
    return [sorted(comps[i]['id'] for i in block) for block in blocks
            if not any(comps[i]['id'] in taken for i in block)] + [sorted(p) for p in pairs]


def measure_input_blocks(pl, comps, index, model):
    records, missing = [], []
    pair_rows = {}
    for pair in model.get('pairs', ()):
        try:
            contract = pair.get('evidence_contract')
            if contract is not None and (len(contract)!=len(comps) or any(
                    comps[index[n]]['symbol']!=row['symbol'] or comps[index[n]]['nodes']!=row['nodes']
                    for n,row in contract.items())):
                raise ValueError('input_evidence_contract_changed')
            for n, contract in pair['terminal_contract'].items():
                if comps[index[n]]['nodes'] != contract:
                    raise ValueError('input_terminal_contract_changed')
            points = [pl.pos_of(index[n]) for n in pair['members']]
            if not all(isfinite(v) for p in points for v in p):
                raise ValueError('nonfinite_input_pair')
            row = [pl.cell_of[index[n]][0] for n in pair['members']]
            pair_rows[pair['id']] = dict(pair=pair, points=points, row=row)
            lo, hi = sorted(p[0] for p in points)
            inside = [c['id'] for c in comps if c['id'] not in pair['members']
                and pl.cell_of[index[c['id']]][0] == row[0] and lo+EPS < pl.pos_of(index[c['id']])[0] < hi-EPS]
            interlopers = [n for n in inside if n not in pair.get('guest_members', ())]
            records.append(dict(id=pair['id'], members=pair['members'], row_errors=int(row[0] != row[1]),
                interleaving=len(interlopers), interlopers=interlopers,
                related_guests_inside=[n for n in inside if n not in interlopers], span=(hi-lo)/100))
        except (KeyError, ValueError, IndexError, TypeError, AttributeError) as exc:
            missing.append(dict(id=pair['id'], reason=str(exc)))
    banks = []
    for bank in model.get('banks', ()):
        try:
            if comps[index[bank['follower']]]['nodes'] != bank['follower_contract']:
                raise ValueError('collector_follower_contract_changed')
            member_ids = [index[n] for n in bank['members']]
            xs = [pl.pos_of(i)[0] for i in member_ids]
            row = pl.cell_of[member_ids[0]][0]
            inside = [c['id'] for c in comps if c['id'] not in bank['members']
                and pl.cell_of[index[c['id']]][0] == row and min(xs)+EPS < pl.pos_of(index[c['id']])[0] < max(xs)-EPS]
            interlopers = [n for n in inside if n not in bank.get('guest_members', ())]
            center = (min(xs)+max(xs))/2
            inward = 0
            for pid in bank['pairs']:
                pair = pair_rows[pid]['pair']; n = bank['collector_members'][pid]
                other = next(v for v in pair['members'] if v != n)
                inward += abs(pl.pos_of(index[n])[0]-center) > abs(pl.pos_of(index[other])[0]-center)+EPS
            banks.append(dict(id=bank['id'], members=bank['members'], interleaving=len(interlopers),
                interlopers=interlopers, related_guests_inside=[n for n in inside if n not in interlopers],
                collector_outside_errors=inward, span=(max(xs)-min(xs))/100))
        except (KeyError, ValueError, IndexError, TypeError, AttributeError) as exc:
            missing.append(dict(id=bank['id'], reason=str(exc)))
    tails = []
    for tail in model.get('tails', ()):
        try:
            i = index[tail['device']]
            if comps[i]['nodes'] != tail['terminal_contract']:
                raise ValueError('tail_terminal_contract_changed')
            target = mean(pl.pin_world(index[n], 'E')[0] for n in tail['members'])
            point = pl.pin_world(i, tail['pin'])
            if not all(isfinite(v) for v in (target, *point)):
                raise ValueError('nonfinite_tail_anchor')
            tails.append(dict(device=tail['device'], remote_net=tail['remote_net'], remote_role=tail['remote_role'],
                distance=abs(point[0]-target)/100, source_pin=tail['pin'], target_x=target))
        except (KeyError, ValueError, IndexError, TypeError, AttributeError) as exc:
            missing.append(dict(id=tail['device'], reason=str(exc)))
    usable = bool(records) and not missing
    return dict(version=VERSION, count=len(model.get('pairs', ())), bank_count=len(model.get('banks', ())),
        measurable=usable, pairs=records, banks=banks, tails=tails, missing=missing,
        row_errors=sum(p['row_errors'] for p in records) if usable else None,
        pair_interleaving=sum(p['interleaving'] for p in records) if usable else None,
        bank_interleaving=sum(b['interleaving'] for b in banks) if usable else None,
        collector_outside_errors=sum(b['collector_outside_errors'] for b in banks) if usable else None,
        normalized_span=sum(p['span'] for p in records)+sum(b['span'] for b in banks) if usable else None,
        tail_distance=sum(t['distance'] for t in tails) if usable else None,
        skipped=deepcopy(model.get('skipped', ())))


def input_rank(measured):
    if not measured.get('count'):
        return (0, 0, 0, 0, 0, 0)
    keys = ('row_errors', 'pair_interleaving', 'bank_interleaving', 'collector_outside_errors',
            'tail_distance', 'normalized_span')
    if not measured.get('measurable') or any(type(measured.get(k)) not in (int, float)
            or not isfinite(measured[k]) or measured[k] < 0 for k in keys):
        return None
    return tuple(round(measured[k], 8) for k in keys)


def input_trials(pl, comps, index, model, *, move_tails=False, deadline=None, output_model=None):
    from netlist2schematic.plugins.layout_shared.layout_relations import prepare_structure_placement
    from netlist2schematic.plugins.attachment.attachment_integration import _fixed_ids
    pairs = model.get('pairs', ())
    if not pairs:
        return
    output_model = output_model or {}
    fixed = _fixed_ids(pl,index)
    outputs = [g for g in output_model.get('groups',()) if not any(index[n] in fixed for n in g['members'])]
    skipped_outputs = [g['id'] for g in output_model.get('groups',()) if g not in outputs]
    active_output_model = dict(output_model,groups=outputs)
    leaves = list(pairs)+list(outputs)
    blocks = owner_blocks(pl.blocks, comps, dict(model,pairs=leaves))
    if blocks is None:
        return
    owned = {n for p in leaves for n in p['members']}
    if owned & {comps[i]['id'] for i in _fixed_ids(pl, index)}:
        return
    relations = [dict(id=p['id'], kind=p.get('kind','bjt_differential_pair'), members=p['members'],
        owned_members=p['members'], rigid=True, macro=False,
        layout=dict(pairs=[p['members']], orders=[])) for p in pairs]
    relations.extend(dict(id=g['id'],kind='common_emitter_merge',members=g['members'],
        owned_members=g['members'],rigid=True,macro=False,layout={}) for g in outputs)
    relations.extend(dict(id='retained_owner:'+'|'.join(b), kind='retained_owner', members=b,
        owned_members=b, rigid=True, macro=False, layout={}) for b in blocks if not set(b) <= owned)
    relations.extend(dict(id=b['id'], kind='input_collector_bank', members=b['members'],
        owned_members=[], rigid=False, macro=True, layout={}) for b in model.get('banks', ()))
    base = deepcopy(pl)
    previous_scoring_relations = deepcopy(getattr(pl, 'layout_relations', ()))
    previous_role_orders = deepcopy(getattr(pl, 'role_orders', ()))
    previous_macros = deepcopy(getattr(pl, 'layout_macro_blocks', ()))
    previous_motion = deepcopy(getattr(pl, 'motion_blocks', ()))
    prepare_structure_placement(base, comps, index, relations)
    # Consume only the existing leaf/block/order actions and costs. Do not
    # implicitly turn on the separate structure-layout experiment or its terms.
    base.layout_relations = previous_scoring_relations
    base.role_orders = list(previous_role_orders) + base.role_orders
    base.layout_macro_blocks = list(previous_macros) + [
        block for block in base.layout_macro_blocks if block not in previous_macros]
    base.motion_blocks = base.blocks + [
        block for block in base.layout_macro_blocks if block not in base.blocks]
    base.motion_blocks.extend(block for block in previous_motion if block not in base.motion_blocks
                              and not any(comps[i]['id'] in owned for i in block))
    pair_by_id = {p['id']: p for p in pairs}
    # Banks reference leaves; they never absorb tails, followers or a common rail.
    banks = model.get('banks', ())
    seen = set()
    for flips in product((False, True), repeat=min(3, len(banks))):
        if deadline is not None and perf_counter() >= deadline:
            return
        trial = deepcopy(base); cells = list(trial.cell_of)
        for number, bank in enumerate(banks):
            children = sorted(bank['pairs'], key=lambda pid: mean(cells[index[n]][1] for n in pair_by_id[pid]['members']))
            if number < len(flips) and flips[number]:
                children.reverse()
            slots = sorted(cells[index[n]][1] for n in bank['members'])
            midpoint = mean(slots)
            order = []
            for k, pid in enumerate(children):
                collector = bank['collector_members'][pid]
                other = next(n for n in pair_by_id[pid]['members'] if n != collector)
                pair_slots = slots[2*k:2*k+2]
                names = [other, collector] if mean(pair_slots) < midpoint else [collector, other]
                for name, col in zip(names, pair_slots):
                    cells[index[name]] = cells[index[name]][0], col
                order.extend(index[n] for n in names)
            trial.role_orders.append([order])
        trial.set_state(cells, trial.orient)
        from netlist2schematic.plugins.attachment.attachment_outputs import arrange_outputs
        output_pose_changes = arrange_outputs(trial,comps,index,active_output_model,model)
        cells = list(trial.cell_of)
        tail_changes = []; tail_pose_changes = []
        if move_tails:
            groups = defaultdict(list)
            for t in model.get('tails', ()):
                i = index[t['device']]
                if i not in _fixed_ids(pl, index):
                    groups[t['remote_net'], cells[i][0]].append(t)
            for group in groups.values():
                group.sort(key=lambda t: mean(trial.pin_world(index[n], 'E')[0] for n in t['members']))
                ids = {index[t['device']] for t in group}
                row = cells[next(iter(ids))][0]
                slots = [col for col in range(trial.cols) if trial.comp_at.get((row, col)) in (None, *ids)]
                # Monotone minimum-distance matching on the existing axis.
                # Its remote common net remains in the global HPWL check.
                dp = {(0, 0): (0.0, 0, ())}
                for j, col in enumerate(slots, 1):
                    dp[0, j] = (0.0, 0, ())
                    for k in range(1, min(j, len(group))+1):
                        choices = []
                        if (k, j-1) in dp:
                            choices.append(dp[k, j-1])
                        if (k-1, j-1) in dp:
                            t = group[k-1]; i = index[t['device']]
                            x = trial.pin_world(i, t['pin'])[0] + trial.column_x[col] - trial.column_x[cells[i][1]]
                            distance = sum(abs(x-trial.pin_world(index[n], 'E')[0]) for n in t['members'])
                            old = dp[k-1, j-1]
                            choices.append((old[0]+distance, old[1]+abs(col-cells[i][1]), old[2]+(col,)))
                        if choices:
                            dp[k, j] = min(choices)
                selected = dp.get((len(group), len(slots)))
                if selected is None:
                    continue
                for t, col in zip(group, selected[2]):
                    i = index[t['device']]
                    if cells[i][1] != col:
                        tail_changes.append(t['device'])
                    cells[i] = cells[i][0], col
            trial.set_state(cells, trial.orient)
            for group in groups.values():
                for tail in group:
                    i = index[tail['device']]
                    target = tuple(mean(trial.pin_world(index[n], 'E')[a] for n in tail['members']) for a in (0, 1))
                    original = trial.orient[i]; choices = []
                    for orient in trial.allowed_orientations(i):
                        trial.orient[i] = orient
                        point = trial.pin_world(i, tail['pin'])
                        direction = trial.pin_dir(i, tail['pin'])
                        vector = {'east': (1, 0), 'west': (-1, 0), 'north': (0, -1), 'south': (0, 1)}[direction]
                        backwards = sum((target[a]-point[a])*vector[a] for a in (0, 1)) < -EPS
                        choices.append((backwards, sum(abs(target[a]-point[a]) for a in (0, 1)), orient != original, orient))
                    trial.orient[i] = min(choices)[-1]
                    if trial.orient[i] != original:
                        tail_pose_changes.append(tail['device'])
        key = tuple(trial.cell_of), tuple(trial.orient)
        if key in seen:
            continue
        seen.add(key)
        yield trial, dict(kind='input_block_seed', version=VERSION,
            pair_ids=[p['id'] for p in pairs], bank_ids=[b['id'] for b in banks],
            output_groups=[g['id'] for g in outputs],output_pose_changed=output_pose_changes,
            skipped_fixed_output_groups=skipped_outputs,
            tail_order_changed=tail_changes, tail_pose_changed=tail_pose_changes, new_power_roles=False,
            rows_unchanged=True, axes_unchanged=True,
            repacked_devices=[comps[i]['id'] for i in range(len(comps)) if trial.cell_of[i] != pl.cell_of[i]])
