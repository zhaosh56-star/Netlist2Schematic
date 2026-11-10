"""Conservative, opt-in interpretation of drawing evidence.

The baseline recognizer is deliberately unchanged.  This module owns no
placement state, random stream, routing search or netlist mutation.  Its two
public builders separate semantic calibration from later branch composition.
Records describe visible-port evidence, rather than claiming circuit function
from a shared bias, a supply name or the title of an example.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from itertools import combinations

from netlist_core import SRC_ALIASES, SNK_ALIASES

VERSION = 'validated-layout-evidence-v1'
BRANCH_VERSION = 'anchored-branch-evidence-v1'
_ACTIVE = frozenset(('MOS', 'BJT'))
_CONTROL = frozenset(('G', 'B', 'IN+', 'IN-'))


def _ports(device):
    """Return real visible pins; hidden MOS bulk is not a control terminal."""
    cls = device.get('cls')
    if cls == 'MOS':
        mapping = {'D': 'd', 'G': 'g', 'S': 's'}
    elif cls == 'BJT':
        mapping = {'C': 'c', 'B': 'b', 'E': 'e'}
    elif cls == 'PASSIVE':
        mapping = {'A': 'a', 'K': 'b'} if device.get('type') == 'D' else {'1': 'a', '2': 'b'}
    elif cls in ('VSRC', 'ISRC'):
        mapping = {'+': 'p', '-': 'n'}
    elif cls == 'OPAMP':
        mapping = {'OUT': 'op', 'IN+': 'ip', 'IN-': 'in', 'VDD': 'vdd', 'VSS': 'vss'}
    elif cls == 'VCVS':
        mapping = {'OUT': 'op', 'IN+': 'ip', 'IN-': 'in'}
    else:
        mapping = {}
    return {pin: device[field] for pin, field in mapping.items() if field in device}


def _channel(device):
    if device.get('cls') == 'MOS':
        return 'D', 'G', 'S'
    if device.get('cls') == 'BJT':
        return 'C', 'B', 'E'
    return None


def _incidence(devices):
    result = defaultdict(list)
    for name in sorted(devices):
        for pin, net in _ports(devices[name]).items():
            result[net].append((name, pin))
    return result


def _formals(formal_ports):
    if isinstance(formal_ports, dict):
        formal_ports = formal_ports.get('named_ports', formal_ports.get('ports', ()))
    result = {}
    for item in formal_ports:
        if isinstance(item, str):
            result[item] = {'net': item, 'source': 'formal'}
        elif isinstance(item, dict) and item.get('net') is not None:
            result[item['net']] = deepcopy(item)
    return result


def _roles(devices, incidence, formals, supplied):
    """A source count or arbitrary grounded voltage source never proves power."""
    supplied = supplied or {}
    nodes = set(incidence) | set(supplied) | set(formals)
    actual_supplies = defaultdict(list)
    for name, device in sorted(devices.items()):
        if device.get('cls') == 'OPAMP':
            for field, pin in (('vdd', 'VDD'), ('vss', 'VSS')):
                if field in device:
                    actual_supplies[device[field]].append({'kind': 'explicit_opamp_supply',
                                                          'device': name, 'pin': pin})
    result = {}
    for net in sorted(nodes):
        evidence = []
        key = str(net).casefold()
        if key in SRC_ALIASES or key in SNK_ALIASES:
            evidence.append({'kind': 'power_alias', 'polarity': 'high' if key in SRC_ALIASES else 'low'})
        evidence.extend(actual_supplies[net])
        declaration = supplied.get(net, {})
        declared = declaration.get('role') if isinstance(declaration, dict) else declaration
        if declared not in (None, 'power', 'signal', 'unknown'):
            raise ValueError(f'Unknown net role {declared!r} for {net!r}')
        if declared in ('power', 'signal'):
            evidence.append({'kind': 'explicit_net_role', 'role': declared,
                             'details': deepcopy(declaration.get('evidence', ()))
                             if isinstance(declaration, dict) else []})
        if net in formals:
            evidence.append({'kind': 'formal_port', 'details': deepcopy(formals[net])})
        power = bool(actual_supplies[net]) or any(e['kind'] == 'power_alias' for e in evidence)
        power |= declared == 'power'
        signal = declared == 'signal'
        conflict = power and signal
        role = 'unknown' if conflict else ('power' if power else
                ('signal' if signal or net in formals else 'unknown'))
        if conflict:
            evidence.append({'kind': 'conflicting_power_and_signal_evidence'})
        result[net] = {'role': role, 'evidence': evidence, 'conflict': bool(conflict)}
    return result


def _power(roles, net):
    row = roles.get(net, {})
    return row.get('role') == 'power' or row.get('conflict', False)


def _external_controls(devices, incidence, formals, roles):
    formal, driven = set(), set()
    for net, users in incidence.items():
        controls = [(name, pin) for name, pin in users if devices[name].get('cls') != 'VSRC']
        if _power(roles, net) or not controls or any(pin not in _CONTROL for _, pin in controls):
            continue
        if net in formals and formals[net].get('source', 'formal') == 'formal':
            formal.add(net)
        explicit_signal = any(e.get('kind') == 'explicit_net_role' and e.get('role') == 'signal'
                              for e in roles.get(net, {}).get('evidence', ()))
        referenced_driver = False
        for name, _ in users:
            device = devices[name]
            if device.get('cls') != 'VSRC':
                continue
            other = device.get('n') if device.get('p') == net else device.get('p')
            if roles.get(other, {}).get('role') == 'power' and not roles[other].get('conflict'):
                referenced_driver = True
        if referenced_driver or explicit_signal and any(devices[name].get('cls') == 'VSRC' for name, _ in users):
            driven.add(net)
    return formal, driven


def _tail_sources(devices, source, members):
    tails = []
    for name, device in sorted(devices.items()):
        if name in members:
            continue
        channel = _channel(device)
        if channel and _ports(device)[channel[0]] == source:
            tails.append(name)
        elif device.get('cls') == 'ISRC' and source in (device.get('p'), device.get('n')):
            tails.append(name)
    return tails


def _signature(devices, incidence, net, members=()):
    """One-hop role evidence; names and literal signal-net names are absent."""
    own = set(members)
    return sorted((devices[name].get('cls'), devices[name].get('type'), pin, name in own)
                  for name, pin in incidence.get(net, ()))


def _structure(kind, members, roles, nets, *, confidence='high', layout=None, evidence=None):
    members = sorted(set(members))
    return {'id': kind+':'+'|'.join(members), 'kind': kind, 'display_name': kind,
            'devices': members, 'roles': deepcopy(roles), 'nets': deepcopy(nets),
            'confidence': confidence, 'parent': None, 'children': [], 'owner': {},
            'layout': deepcopy(layout or {}), 'validation_evidence': deepcopy(evidence or {})}


def _relation(kind, members, roles, *, layout=None, evidence=None, rigid=False, macro=False):
    members = sorted(set(members))
    return {'id': 'evidence:'+kind+':'+'|'.join(members), 'kind': kind, 'members': members,
            'roles': deepcopy(roles), 'evidence': deepcopy(evidence or {}),
            'confidence': 'high' if rigid or macro else 'relation', 'rigid': bool(rigid),
            'macro': bool(macro), 'parent': None, 'children': [], 'owned_members': [],
            'layout': deepcopy(layout or {})}


def build_layout_evidence(devices, baseline_structs=(), *, formal_ports=(), net_roles=None):
    """Calibrate supply/output/input-pair labels without changing the baseline.

    Existing valid two-user internal differential pairs remain valid.  For a
    shared source, exactly two externally controlled, topologically comparable
    members must be selected; ambiguous banks never generate all combinations.
    ``net_roles`` accepts net -> role or net -> {role, evidence}; the returned
    mapping always contains JSON role/evidence records.
    """
    from circuit_structures import analyze_structures
    from layout_relations import build_layout_relations

    baseline = deepcopy(list(baseline_structs) if baseline_structs else analyze_structures(devices))
    prior_exclusive = {frozenset(row.get('devices', ())): set(row['roles']['exclusive_tails'])
        for row in baseline if row.get('kind') in ('differential_pair', 'bjt_differential_pair')
        and 'exclusive_tails' in row.get('roles', {})}
    incidence, formals = _incidence(devices), _formals(formal_ports)
    roles = _roles(devices, incidence, formals, net_roles)
    voltage_constraints, voltage_graph = defaultdict(list), defaultdict(list)
    for name, dev in sorted(devices.items()):
        if dev.get('cls') == 'VSRC':
            for node, reference in ((dev['p'], dev['n']), (dev['n'], dev['p'])):
                if node != reference:
                    voltage_graph[reference].append((node, name))
    # Only a path of ideal voltage constraints to a verified reference fixes
    # absolute potential. A floating series offset is not a supply or clamp.
    frontier = [(net, net, []) for net in sorted(roles) if roles[net]['role'] == 'power']
    visited = {node for node, _, _ in frontier}
    for reference_node, root, path in frontier:
        for node, name in sorted(voltage_graph[reference_node]):
            if node in visited:
                continue
            visited.add(node)
            route = [*path, name]
            voltage_constraints[node].append({'device': name, 'reference': root,
                'path': route, 'kind': 'ideal_voltage_to_verified_reference'})
            frontier.append((node, root, route))
    formal_controls, driven_controls = _external_controls(devices, incidence, formals, roles)
    diagnostics = {'downgraded_outputs': [], 'rejected_pairs': [], 'added_pairs': [],
                   'invalid_composites': [],
                   'shared_source_banks': [], 'supply_conflicts': sorted(n for n in roles if roles[n]['conflict'])}
    structures, changes, known_pairs = [], {}, set()
    by_source, all_source_users = defaultdict(list), defaultdict(list)
    for name, device in sorted(devices.items()):
        channel = _channel(device)
        if channel:
            by_source[(device['cls'], device['type'], _ports(device)[channel[2]])].append(name)
            all_source_users[_ports(device)[channel[2]]].append(name)

    def pair_data(names, source):
        a, b = (devices[name] for name in names)
        ca, cb = _channel(a), _channel(b)
        if not ca or not cb or (a['cls'], a['type']) != (b['cls'], b['type']):
            return None
        pa, pb = _ports(a), _ports(b)
        if (_power(roles, source) or source in voltage_constraints
                or pa[ca[2]] != source or pb[cb[2]] != source
                or pa[ca[0]] == pb[cb[0]] or pa[ca[1]] == pb[cb[1]]):
            return None
        tails = _tail_sources(devices, source, names)
        return (pa, pb, ca, tails) if tails else None

    def selected_external(users):
        formal_members = [n for n in users if _ports(devices[n])[_channel(devices[n])[1]] in formal_controls]
        if len(formal_members) > 2:
            return [], 'ambiguous_formal_controls'
        external = [n for n in users if _ports(devices[n])[_channel(devices[n])[1]]
                    in formal_controls | driven_controls]
        if len(external) != 2:
            return [], 'ambiguous_external_controls'
        return external, 'formal_control_pair' if len(formal_members) == 2 else 'external_control_pair'

    for row in baseline:
        kind = row.get('kind')
        if kind in ('differential_pair', 'bjt_differential_pair'):
            names = [row.get('roles', {}).get(key) for key in ('left', 'right')]
            if not all(n in devices for n in names):
                diagnostics['rejected_pairs'].append({'id': row.get('id'), 'reason': 'missing_members'})
                continue
            source = _ports(devices[names[0]])[_channel(devices[names[0]])[2]]
            data = pair_data(names, source)
            users = by_source[(devices[names[0]]['cls'], devices[names[0]]['type'], source)]
            chosen, reason = selected_external(users)
            shared = len(all_source_users[source]) > 2
            if data is None or (shared and set(chosen) != set(names)):
                diagnostics['rejected_pairs'].append({'id': row.get('id'),
                    'reason': 'source_voltage_clamped' if source in voltage_constraints else 'unverified_pair_source'})
                continue
            row['roles'].update(source_net=source, source_users=list(all_source_users[source]), tail=data[3],
                exclusive_tails=[] if shared else list(data[3]), shared_tails=list(data[3]) if shared else [],
                validated_shared_source=shared)
            row['validation_evidence'] = {'kind': reason if shared else 'existing_two_user_pair',
                                           'source': source, 'shared': shared}
            known_pairs.add(frozenset(names))
        elif kind == 'output_pair':
            names = row.get('devices', ())
            valid = len(names) == 2 and all(n in devices and devices[n].get('cls') == 'MOS' for n in names)
            if valid:
                a, b = (devices[n] for n in names)
                output = a['d']
                valid = (a['d'] == b['d'] and {a['type'], b['type']} == {'PMOS', 'NMOS'}
                         and _power(roles, a['s']) and _power(roles, b['s'])
                         and not roles[a['s']]['conflict'] and not roles[b['s']]['conflict']
                         and output in formals and not _power(roles, output)
                         and output not in formal_controls)
            if not valid:
                old = row['id']
                row['kind'] = 'complementary_common_drain'
                row['id'] = row['kind']+':'+'|'.join(sorted(names))
                row['display_name'] = 'complementary common-drain connection'
                row['confidence'], row['owner'] = 'relation', {}
                row['validation_evidence'] = {'kind': 'neutral_common_drain',
                                              'reason': 'insufficient_verified_supply_and_external_output'}
                changes[old] = row['id']
                diagnostics['downgraded_outputs'].append({'source_structure': old, 'relation': row['id']})
        structures.append(row)

    for (family, polarity, source), users in sorted(by_source.items()):
        if len(users) < 2 or _power(roles, source):
            continue
        shared = len(all_source_users[source]) > 2
        chosen, reason = selected_external(users) if shared else (users, 'two_user_visible_pair')
        record = {'source': source, 'users': list(users), 'selected': list(chosen), 'reason': reason}
        if shared:
            diagnostics['shared_source_banks'].append(record)
        if len(chosen) != 2 or frozenset(chosen) in known_pairs:
            continue
        data = pair_data(chosen, source)
        if not data:
            record['reason'] = ('source_voltage_clamped' if source in voltage_constraints
                                else 'no_distinct_branches_or_tail')
            if source in voltage_constraints:
                diagnostics['rejected_pairs'].append({'source': source, 'members': list(chosen),
                                                       'reason': 'source_voltage_clamped'})
            continue
        pa, pb, channel, tails = data
        if shared and _signature(devices, incidence, pa[channel[0]], chosen) != _signature(
                devices, incidence, pb[channel[0]], chosen):
            record['reason'] = 'incomparable_output_branches'
            continue
        kind = 'differential_pair' if family == 'MOS' else 'bjt_differential_pair'
        left, right = sorted(chosen)
        row = _structure(kind, chosen,
            {'left': left, 'right': right, 'source_net': source, 'source_users': list(all_source_users[source]),
             'tail': tails, 'exclusive_tails': [] if shared else tails,
             'shared_tails': tails if shared else [], 'validated_shared_source': shared},
            {'source': source, 'left_drain': _ports(devices[left])[channel[0]],
             'right_drain': _ports(devices[right])[channel[0]]},
            layout={'align_rows': [[left, right]], 'branches': [[left], [right]]},
            evidence={'kind': reason, 'source': source, 'source_users': users,
                      'comparable_visible_output_incidence': True})
        structures.append(row)
        diagnostics['added_pairs'].append(row['id'])
        known_pairs.add(frozenset(chosen))

    # A rejected child cannot be reintroduced through its old high-confidence
    # parent and the parent's matched-pair expansion in build_layout_relations.
    invalid = {r['id'] for r in diagnostics['rejected_pairs'] if r.get('id') is not None}
    while True:
        changed = False
        for row in structures:
            if row['confidence'] != 'high' or not (set(row.get('children', ())) & invalid):
                continue
            old = row['id']
            row['original_kind'] = row['kind']
            row['kind'] = 'unvalidated_composite'
            row['id'] = row['kind']+':'+'|'.join(sorted(row['devices']))
            row['confidence'], row['layout'], row['owner'] = 'relation', {}, {}
            row['validation_evidence'] = {'kind': 'rejected_child_dependency',
                'rejected_children': sorted(set(row.get('children', ())) & invalid)}
            changes[old] = row['id']
            invalid.add(old)
            diagnostics['invalid_composites'].append({'id': old, 'replacement': row['id'],
                'reason': 'rejected_child_dependency'})
            changed = True
        if not changed:
            break
    valid_ids = {row['id'] for row in structures}
    for row in structures:
        parent = changes.get(row.get('parent'), row.get('parent'))
        row['parent'] = parent if parent in valid_ids else None
        row['children'] = sorted({changes.get(n, n) for n in row.get('children', ())
                                  if changes.get(n, n) in valid_ids})
        row['owner'] = {n: owner for n, owner in row.get('owner', {}).items()
                        if owner in valid_ids and not row.get('confidence') == 'relation'}
    structures.sort(key=lambda row: row['id'])
    generated = build_layout_relations(devices, structures)
    rejected_relations = {row['id'] for row in generated
        if row['kind'] in ('differential_pair', 'bjt_differential_pair')
        and frozenset(row['members']) not in known_pairs}
    relations = [row for row in generated if row['id'] not in rejected_relations
                 and row.get('evidence', {}).get('anchor_relation') not in rejected_relations]
    for row in structures:
        if row['kind'] in ('differential_pair', 'bjt_differential_pair') and not any(
                r['kind'] == row['kind'] and set(r['members']) == set(row['devices']) for r in relations):
            relations.append(_relation(row['kind'], row['devices'], row['roles'], rigid=True,
                layout={'pairs': [row['devices']], 'orders': [row['devices']]},
                evidence={'kind': 'validated_visible_pair', 'source_structure': row['id']}))
    tail_users = defaultdict(set)
    for row in structures:
        if row['kind'] in ('differential_pair', 'bjt_differential_pair'):
            for name in row['roles'].get('tail', ()):
                tail_users[name].add(row['id'])
    reserved = {name for row in relations for pair in row.get('layout', {}).get('pairs', ())
                for name in pair}
    reserved.update(name for row in relations if row['kind'] in ('current_mirror', 'bjt_current_mirror')
                    for name in row['members'])
    reserved.update(name for row in structures if row['confidence'] == 'high'
                    and row['kind'] not in ('differential_pair', 'bjt_differential_pair')
                    for name in row['devices'])
    for row in structures:
        if row['kind'] not in ('differential_pair', 'bjt_differential_pair'):
            continue
        before = prior_exclusive.get(frozenset(row['devices']))
        tails = row['roles']['tail']
        exclusive = [name for name in tails if not row['roles']['validated_shared_source']
                     and name not in reserved and len(tail_users[name]) == 1
                     and (before is None or name in before)]
        row['roles']['exclusive_tails'] = exclusive
        row['roles']['shared_tails'] = [name for name in tails if name not in exclusive]
        row['roles']['reserved_tails'] = [name for name in tails if name in reserved]
    for relation in relations:
        source = next((row for row in structures if row['id'] ==
                       relation['evidence'].get('source_structure')), None)
        if source and relation['kind'] in ('differential_pair', 'bjt_differential_pair'):
            for key in ('source_net', 'source_users', 'tail', 'exclusive_tails',
                        'shared_tails', 'reserved_tails', 'validated_shared_source'):
                if key in source['roles']:
                    relation['roles'][key] = deepcopy(source['roles'][key])
            relation['evidence']['validation'] = deepcopy(source.get('validation_evidence', {}))
    for row in structures:
        if row['kind'] == 'complementary_common_drain':
            upper, lower = row['roles'].get('upper'), row['roles'].get('lower')
            relations.append(_relation(row['kind'], row['devices'], row['roles'],
                layout={'branches': [[upper, lower]] if upper and lower else []},
                evidence={'kind': 'neutral_common_drain', 'source_structure': row['id'],
                          'nets': row.get('nets', {})}))
    return {'version': VERSION, 'structures': structures,
            'relations': sorted(relations, key=lambda row: row['id']),
            'net_roles': roles, 'voltage_constraints': dict(voltage_constraints),
            'formal_ports': list(formals.values()),
            'external_controls': {'formal': sorted(formal_controls), 'voltage_source': sorted(driven_controls)},
            'diagnostics': diagnostics}


def extend_branch_evidence(devices, evidence, *, bridge_alignment=()):
    """Extend calibrated seeds using unique, corresponding visible branches.

    This is a separate opt-in stage.  A corresponding branch is drawing
    evidence, not a claim that a whole circuit is a cascode, CMFB or buffer.
    Existing explicit composites retain their parent/child partition.  New
    ownership is deliberately left to the caller's common owner resolver.
    """
    result = deepcopy(evidence)
    records = result['relations']
    roles = result['net_roles']
    incidence = _incidence(devices)
    active = {n: d for n, d in devices.items() if _channel(d)}
    report = {'version': BRANCH_VERSION, 'added_relations': [], 'skipped_branches': [],
              'rejected_bridges': [], 'functional_claims': 'none', 'ownership': 'caller_resolves'}
    identities = {(r['kind'], tuple(sorted(r['members']))) for r in records}
    existing_ids = {r['id'] for r in records}

    def add(row, *, repeated_ports=False):
        identity = row['kind'], tuple(sorted(row['members']))
        if repeated_ports:
            identity += (row['id'],)
        if row['id'] in existing_ids or identity in identities or len(row['members']) < 2:
            return None
        identities.add(identity)
        existing_ids.add(row['id'])
        records.append(row)
        report['added_relations'].append(row['id'])
        return row

    def neighbor(name, direction):
        device = devices[name]
        out, _, src = _channel(device)
        anchor_pin, other_pin = (out, src) if direction == 'out' else (src, out)
        net = _ports(device)[anchor_pin]
        if _power(roles, net):
            return None, 'power_endpoint'
        users = [(n, pin) for n, pin in incidence[net] if n != name and n in active
                 and pin in (_channel(active[n])[0], _channel(active[n])[2])]
        if len(users) != 1 or users[0][1] != other_pin:
            return None, 'nonunique_conduction_continuation'
        other = users[0][0]
        if (active[other]['cls'], active[other]['type']) != (device['cls'], device['type']):
            return None, 'different_channel_family'
        if any(devices[n].get('cls') in ('VSRC', 'ISRC') for n, _ in incidence[net]):
            return None, 'source_at_branch_junction'
        return (other, net, anchor_pin, other_pin), None

    priority = {'differential_pair': 0, 'bjt_differential_pair': 0,
                'current_mirror': 1, 'bjt_current_mirror': 1, 'matched_branch_pair': 2}
    roots = []
    seen_roots = set()
    for row in sorted(list(records), key=lambda r: (priority.get(r['kind'], 3), r['id'])):
        for pair in row.get('layout', {}).get('pairs', ()):
            identity = frozenset(pair)
            if len(pair) == 2 and all(n in active for n in pair) and identity not in seen_roots:
                seen_roots.add(identity)
                roots.append((list(pair), row))
    explored = set()
    for initial, anchor_relation in roots:
        queue, children, members, edges = [initial], [], set(initial), []
        while queue:
            pair = queue.pop(0)
            identity = frozenset(pair)
            if identity in explored:
                continue
            explored.add(identity)
            for direction in ('out', 'source'):
                left, left_error = neighbor(pair[0], direction)
                right, right_error = neighbor(pair[1], direction)
                if not left or not right or left[0] == right[0] or left[1] == right[1]:
                    report['skipped_branches'].append({'anchor_pair': list(pair), 'direction': direction,
                        'reason': left_error or right_error or 'merged_branches'})
                    continue
                names = [left[0], right[0]]
                first, second = (active[n] for n in names)
                if (first['cls'], first['type']) != (second['cls'], second['type']):
                    continue
                control = _channel(first)[1]
                ga, gb = (_ports(active[n])[control] for n in names)
                shared_control = ga == gb
                if not shared_control and _signature(devices, incidence, ga, [*names, *pair]) != _signature(
                        devices, incidence, gb, [*names, *pair]):
                    report['skipped_branches'].append({'anchor_pair': list(pair), 'direction': direction,
                                                      'reason': 'incomparable_control_roles'})
                    continue
                if frozenset(names) in explored:
                    continue
                positive = first['type'] in ('PMOS', 'PNP')
                neighbor_above = (direction == 'out') != positive
                branches = [[n, a] if neighbor_above else [a, n] for n, a in zip(names, pair)]
                row = add(_relation('corresponding_branch_pair', names,
                    {'left': names[0], 'right': names[1], 'anchor_pair': list(pair),
                     'anchor_pins': [left[2], right[2]], 'member_pins': [left[3], right[3]],
                     'branches': branches, 'direction': direction}, rigid=True,
                    layout={'pairs': [names], 'orders': [names], 'branches': branches},
                    evidence={'kind': 'unique_corresponding_channels',
                              'anchor_relation': anchor_relation['id'],
                              'junctions': [left[1], right[1]],
                              'control_evidence': 'shared_control' if shared_control else 'matching_control_incidence',
                              'functional_claim': None}))
                if row:
                    children.append(row)
                    members.update(names)
                    edges.extend(branches)
                    queue.append(names)
        if children and not anchor_relation.get('parent'):
            macro = add(_relation('paired_branches', members,
                {'anchor_pair': initial, 'corresponding_pairs': [r['members'] for r in children],
                 'branch_edges': edges}, macro=True,
                layout={'pairs': [initial, *[r['members'] for r in children]], 'branches': edges},
                evidence={'kind': 'anchored_corresponding_branches',
                          'anchor_relation': anchor_relation['id'], 'functional_claim': None}))
            if macro:
                for child in [anchor_relation, *children]:
                    if child.get('parent') is None:
                        child['parent'] = macro['id']
                        macro['children'].append(child['id'])
                macro['children'].sort()

    # Recheck imported R/L bridge evidence against raw visible terminals.
    for bridge in bridge_alignment:
        names, path = list(bridge.get('members', ())), list(bridge.get('path', ()))
        endpoints = [bridge.get('upper'), bridge.get('lower')]
        valid = bool(names) and len(path) == len(names)+1 and len(set(path)) == len(path) and all(n in devices and
            devices[n].get('cls') == 'PASSIVE' and devices[n].get('type') in ('R', 'L') for n in names)
        if valid:
            for i, name in enumerate(names):
                pins = bridge.get('member_pins', {}).get(name, {})
                ports = _ports(devices[name])
                if (ports.get(pins.get('in')) != path[i] or ports.get(pins.get('out')) != path[i+1]):
                    valid = False
            for i, net in enumerate(path):
                expected = []
                if i:
                    expected.append((names[i-1], bridge.get('member_pins', {}).get(names[i-1], {}).get('out')))
                if i < len(names):
                    expected.append((names[i], bridge.get('member_pins', {}).get(names[i], {}).get('in')))
                endpoint = endpoints[0] if i == 0 else endpoints[1] if i == len(names) else None
                endpoint_pin = bridge.get('upper_pin') if i == 0 else bridge.get('lower_pin')
                if endpoint is not None:
                    if endpoint not in active or endpoint_pin not in (_channel(active[endpoint])[0],
                            _channel(active[endpoint])[2]) or _ports(active[endpoint]).get(endpoint_pin) != net:
                        valid = False
                    expected.append((endpoint, endpoint_pin))
                if not _power(roles, net) and set(incidence[net]) != set(expected):
                    valid = False
        for connection in bridge.get('connections', ()):
            pins = connection.get('pins', ())
            net = connection.get('net')
            if (len(pins) != 2 or any(n not in devices or _ports(devices[n]).get(pin) != net for n, pin in pins)
                    or not _power(roles, net) and set(incidence[net]) != {tuple(p) for p in pins}):
                valid = False
        if not valid or not bridge.get('connections'):
            report['rejected_bridges'].append({'id': bridge.get('id'), 'reason': 'unverified_rl_bridge'})
            continue
        branch = [n for n in (endpoints[0], *names, endpoints[1]) if n is not None]
        add(_relation('rl_branch_bridge', branch, deepcopy(bridge),
            layout={'branches': [branch], 'attachments': deepcopy(bridge['connections'])},
            evidence={'kind': 'strict_visible_rl_path', 'source_bridge': bridge.get('id')}))

    # Attachments are soft port relationships, never rigid passive/active unions.
    for name, device in sorted(devices.items()):
        if device.get('cls') != 'PASSIVE':
            continue
        ports = _ports(device)
        for pin, net in ports.items():
            if _power(roles, net):
                continue
            anchors = [(n, p) for n, p in incidence[net] if n in active or
                       devices[n].get('cls') in ('VCVS', 'OPAMP') and p not in ('VDD', 'VSS')]
            if len(anchors) != 1:
                continue
            anchor = anchors[0]
            other_net = next((value for other, value in ports.items() if other != pin), None)
            port_role = ('control' if anchor[1] in _CONTROL else
                         'output' if anchor[1] in ('D', 'C', 'OUT') else 'conduction_source')
            row = _relation('port_attachment', [name, anchor[0]],
                {'member': name, 'member_pin': pin, 'anchor': list(anchor), 'net': net,
                 'other_net': other_net, 'port_role': port_role,
                 'other_is_power': _power(roles, other_net)},
                layout={'attachments': [{'net': net, 'pins': [[name, pin], list(anchor)]}]},
                evidence={'kind': 'unique_visible_port_anchor', 'functional_claim': None})
            row['id'] += '@'+pin+':'+anchor[1]
            add(row, repeated_ports=True)

    # Equal two-resistor sensing paths into control pins; no CMFB/sign claim.
    output_pairs = []
    for row in records:
        for pair in row.get('layout', {}).get('pairs', ()):
            if len(pair) == 2 and all(n in active for n in pair):
                outputs = [_ports(active[n])[_channel(active[n])[0]] for n in pair]
                if len(set(outputs)) == 2:
                    output_pairs.append(set(outputs))
    for net, users in sorted(incidence.items()):
        controls = [(n, p) for n, p in users if p in _CONTROL]
        resistors = sorted({n for n, _ in users if devices[n].get('cls') == 'PASSIVE'
                            and devices[n].get('type') == 'R'})
        if (_power(roles, net) or not controls or len(resistors) != 2
                or any(devices[n].get('cls') == 'VSRC' for n, _ in users)):
            continue
        first, second = (devices[n] for n in resistors)
        if first.get('value') is None or first.get('value') != second.get('value'):
            continue
        ends = [next((x for x in (devices[n]['a'], devices[n]['b']) if x != net), net) for n in resistors]
        if set(ends) not in output_pairs:
            continue
        add(_relation('paired_sense_paths', [*resistors, *[n for n, _ in controls]],
            {'sensing_members': resistors, 'sense_nets': ends, 'control_net': net,
             'controlled_pins': [list(p) for p in controls]},
            evidence={'kind': 'equal_resistors_from_corresponding_outputs_to_control',
                      'functional_claim': None}))

    # Two collector-supplied BJT emitter outputs may merge without being a DP.
    emitters = defaultdict(list)
    for name, device in sorted(active.items()):
        if device['cls'] == 'BJT' and _power(roles, device['c']) and not _power(roles, device['e']):
            emitters[(device['type'], device['e'])].append(name)
    for (_, net), names in sorted(emitters.items()):
        if len(names) == 2 and devices[names[0]]['b'] != devices[names[1]]['b']:
            add(_relation('common_emitter_merge', names,
                {'members': names, 'output_net': net, 'output_pins': [[n, 'E'] for n in names]},
                layout={'same_row': [names]},
                evidence={'kind': 'two_collector_supplied_emitter_outputs', 'functional_claim': None}))

    # Clock access requires a latch, two rail reset/precharge devices and a
    # gated tail on the same explicit external control; bias fanout alone fails.
    latch_outputs = {devices[n]['d'] for row in result['structures'] if row['kind'] == 'latch'
                     for n in row['devices'] if devices[n].get('cls') == 'MOS'}
    pair_sources = {row['roles'].get('source_net', row.get('nets', {}).get('source'))
                    for row in result['structures'] if row['kind'] == 'differential_pair'}
    external = set(result['external_controls']['formal']) | set(result['external_controls']['voltage_source'])
    for net in sorted(external):
        resets, tails = [], []
        for name, device in sorted(active.items()):
            if device['cls'] != 'MOS' or device['g'] != net:
                continue
            if _power(roles, device['s']) and device['d'] in latch_outputs:
                resets.append(name)
            if device['d'] in pair_sources:
                tails.append(name)
        if len(resets) >= 2 and tails:
            add(_relation('clock_access', [*resets, *tails],
                {'control_net': net, 'reset_members': resets, 'tail_members': tails,
                 'control_pins': [[n, 'G'] for n in [*resets, *tails]]},
                evidence={'kind': 'external_latch_reset_and_tail_control', 'functional_claim': None}))
    result['relations'] = sorted(records, key=lambda row: row['id'])
    result['branch_evidence'] = report
    return result
