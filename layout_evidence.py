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
        if (_power(roles, source) or pa[ca[2]] != source or pb[cb[2]] != source
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
                diagnostics['rejected_pairs'].append({'id': row.get('id'), 'reason': 'unverified_pair_source'})
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
            record['reason'] = 'no_distinct_branches_or_tail'
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
    invalid = {r['id'] for r in diagnostics['rejected_pairs']}
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
            'net_roles': roles, 'formal_ports': list(formals.values()),
            'external_controls': {'formal': sorted(formal_controls), 'voltage_source': sorted(driven_controls)},
            'diagnostics': diagnostics}


