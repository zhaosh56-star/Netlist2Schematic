"""Topology based schematic structures, with explicit roles and ownership.

This module deliberately does not use instance names, drawing coordinates or the
legacy VF2 matcher's greedy disjoint selection.  Relations may overlap; a device
has exactly one owning *high confidence* structure, chosen at the lowest level.
"""
from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
from itertools import combinations

from netlist2schematic.netlist.netlist_core import SNK_ALIASES, SRC_ALIASES


_LABELS = {
    'differential_pair': '差分对', 'current_mirror': '电流镜',
    'cross_coupled': '交叉耦合对', 'complementary_branch': '互补支路',
    'output_pair': '互补输出级', 'latch': '锁存核心',
    'common_bias_pair': '共偏置关系', 'common_source_bank': '公共源支路',
    'common_gate_pair': '共栅关系', 'series_branch': '串接支路',
    'folded_core': '折叠共栅核心', 'series_chain': '串联链',
    'parallel_passives': '并联支路', 'feedback_network': '运放反馈',
    'opamp_core': '运放反馈核心',
}
_OWN_PRIORITY = {
    'complementary_branch': 100, 'series_chain': 100,
    'parallel_passives': 100, 'differential_pair': 90,
    'current_mirror': 85, 'output_pair': 80, 'series_branch': 70,
    'cross_coupled': 60, 'feedback_network': 40,
    'folded_core': 30, 'latch': 20, 'opamp_core': 10,
}


def _terminals(device):
    fields = {'MOS': ('d', 'g', 's', 'bulk'), 'BJT': ('c', 'b', 'e'),
              'PASSIVE': ('a', 'b'), 'VSRC': ('p', 'n'),
              'ISRC': ('p', 'n'), 'VCVS': ('op', 'on', 'ip', 'in')}
    return [(pin, device[pin]) for pin in fields.get(device.get('cls'), ())
            if pin in device]


def _topological_keys(devices):
    """Refined bipartite colours make asymmetric role order name independent."""
    incident = defaultdict(list)
    colours = {}
    for name, device in devices.items():
        colours[('d', name)] = str((device.get('cls'), device.get('type')))
        for pin, net in _terminals(device):
            incident[net].append((name, pin))
    for net in incident:
        colours[('n', net)] = ('high' if net in SRC_ALIASES else
                               'low' if net in SNK_ALIASES else 'signal')
    for _ in range(4):
        next_colours = {}
        for name, device in devices.items():
            neighbors = sorted((pin, colours[('n', net)])
                               for pin, net in _terminals(device))
            raw = repr((colours[('d', name)], neighbors)).encode()
            next_colours[('d', name)] = sha256(raw).hexdigest()
        for net, pins in incident.items():
            neighbors = sorted((pin, colours[('d', name)]) for name, pin in pins)
            raw = repr((colours[('n', net)], neighbors)).encode()
            next_colours[('n', net)] = sha256(raw).hexdigest()
        colours = next_colours
    return {name: (colours[('d', name)], name.casefold(), name) for name in devices}


def analyze_structures(devices):
    """Return JSON serializable semantic records for canonical netlist devices.

    ``confidence`` is ``high`` or ``relation``. ``roles`` contains device IDs,
    lists of IDs, or the feedback port string ``plus``/``minus``. ``devices``
    includes only actual members (not an external tail used as evidence).
    ``parent``/``children`` describe blocks; ``owner`` maps each member to its
    unique lowest-level high-confidence owner, or None for unowned relations.
    ``layout`` contains optional role-derived alignment/ordering suggestions.
    The original devices and their connections are never modified.
    """
    keys = _topological_keys(devices)
    ordered = lambda names: sorted(set(names), key=lambda name: keys[name])
    records = []
    seen = {}

    def add(kind, members, roles, nets=None, confidence='high', layout=None):
        members = ordered(members)
        identity = (kind, tuple(sorted(members)))
        if identity in seen:
            return seen[identity]
        identifier = kind + ':' + '|'.join(sorted(members))
        record = {'id': identifier, 'kind': kind, 'display_name': _LABELS[kind],
                  'devices': members, 'confidence': confidence, 'roles': roles,
                  'nets': nets or {}, 'parent': None, 'children': [], 'owner': {},
                  'layout': layout or {}}
        records.append(record)
        seen[identity] = record
        return record

    def parent(child, block):
        if child['id'] == block['id']:
            return
        # Explicit composites can supersede a previously discovered block.
        if child['parent'] is not None:
            old = next((r for r in records if r['id'] == child['parent']), None)
            if old and child['id'] in old['children']:
                old['children'].remove(child['id'])
        child['parent'] = block['id']
        if child['id'] not in block['children']:
            block['children'].append(child['id'])

    mos = {name: dev for name, dev in devices.items() if dev.get('cls') == 'MOS'}
    sources = defaultdict(list)
    drains = defaultdict(list)
    for name, dev in mos.items():
        sources[dev['s']].append(name)
        drains[dev['d']].append(name)
    voltage_rails = set()
    for dev in devices.values():
        if dev.get('cls') == 'VSRC':
            if dev['n'] in SNK_ALIASES:
                voltage_rails.add(dev['p'])
            if dev['p'] in SNK_ALIASES:
                voltage_rails.add(dev['n'])
    rails = set(SRC_ALIASES) | set(SNK_ALIASES) | voltage_rails
    rails.update(net for net, users in sources.items() if len(users) >= 3)

    # A pair needs a real tail, not merely two devices sharing a supply rail.
    differential = []
    for source, names in sources.items():
        for polarity in ('NMOS', 'PMOS'):
            pair = ordered(n for n in names if mos[n]['type'] == polarity)
            if source in rails or len(pair) != 2:
                continue
            left, right = pair
            a, b = mos[left], mos[right]
            if a['d'] == b['d'] or a['g'] == b['g']:
                continue
            tails = ordered([n for n, dev in mos.items()
                             if n not in pair and dev['d'] == source] +
                            [n for n, dev in devices.items()
                             if dev.get('cls') == 'ISRC' and source in (dev['p'], dev['n'])])
            if not tails:
                continue
            differential.append(add('differential_pair', pair,
                                    {'left': left, 'right': right, 'tail': tails},
                                    {'source': source, 'left_drain': a['d'],
                                     'right_drain': b['d']},
                                    layout={'align_rows': [pair], 'aligned': [],
                                            'branches': [[left], [right]]}))

    bias_groups = defaultdict(list)
    for name, dev in mos.items():
        bias_groups[(dev['type'], dev['s'], dev['g'])].append(name)
    common_bias = []
    for (polarity, source, gate), names in bias_groups.items():
        names = ordered(names)
        if len(names) < 2:
            continue
        references = ordered(n for n in names if mos[n]['d'] == gate)
        outputs = ordered(n for n in names if mos[n]['d'] != gate)
        if references and outputs:
            add('current_mirror', names,
                {'reference': references[0], 'references': references, 'outputs': outputs},
                {'source': source, 'bias': gate},
                layout={'align_rows': [names], 'aligned': [], 'branches': []})
        elif len({mos[n]['d'] for n in names}) > 1:
            common_bias.append(add('common_bias_pair', names, {'members': names},
                                   {'source': source, 'bias': gate}, 'relation'))

    for source, names in sources.items():
        if len(names) >= 3:
            names = ordered(names)
            add('common_source_bank', names, {'members': names},
                {'source': source}, 'relation')

    cross_pairs = []
    for left, right in combinations(ordered(mos), 2):
        a, b = mos[left], mos[right]
        if (a['type'] == b['type'] and a['s'] == b['s'] and a['d'] != b['d']
                and a['g'] == b['d'] and b['g'] == a['d']):
            cross_pairs.append(add('cross_coupled', [left, right],
                                   {'left': left, 'right': right},
                                   {'left_output': a['d'], 'right_output': b['d']},
                                   layout={'align_rows': [[left, right]],
                                           'aligned': [], 'branches': []}))

    complementary = []
    for drain, names in drains.items():
        uppers = ordered(n for n in names if mos[n]['type'] == 'PMOS')
        lowers = ordered(n for n in names if mos[n]['type'] == 'NMOS')
        for upper in uppers:
            for lower in lowers:
                a, b = mos[upper], mos[lower]
                if a['g'] == b['g']:
                    kind = 'complementary_branch'
                elif (len(uppers) == len(lowers) == 1
                      and a['s'] in rails and b['s'] in rails):
                    kind = 'output_pair'
                else:
                    continue
                complementary.append(add(kind, [upper, lower],
                                         {'upper': upper, 'lower': lower},
                                         {'output': drain, 'upper_gate': a['g'],
                                          'lower_gate': b['g']},
                                         layout={'align_columns': [[upper, lower]],
                                                 'aligned': [[upper, lower]],
                                                 'branches': [[upper, lower]]}))

    for cross in cross_pairs:
        left, right = cross['roles']['left'], cross['roles']['right']
        branches = []
        for name in (left, right):
            matches = [r for r in complementary
                       if r['kind'] == 'complementary_branch' and name in r['devices']]
            if len(matches) == 1:
                branches.append(matches[0])
        if len(branches) != 2 or branches[0]['id'] == branches[1]['id']:
            continue
        roles = {side + '_' + position: branch['roles'][position]
                 for side, branch in zip(('left', 'right'), branches)
                 for position in ('upper', 'lower')}
        members = [n for branch in branches for n in branch['devices']]
        block = add('latch', members, roles, dict(cross['nets']),
                    layout={'align_columns': [b['devices'] for b in branches],
                            'aligned': [b['devices'] for b in branches],
                            'branches': [[roles['left_upper'], roles['left_lower']],
                                         [roles['right_upper'], roles['right_lower']]]})
        for child in [cross, *branches]:
            parent(child, block)

    # Folded cores retain input, common-bias and common-gate branch roles.
    for pair in differential:
        left, right = pair['roles']['left'], pair['roles']['right']
        a, b = mos[left], mos[right]
        opposite = 'NMOS' if a['type'] == 'PMOS' else 'PMOS'
        for bias in common_bias:
            bias_left = [n for n in bias['devices']
                         if mos[n]['type'] == opposite and mos[n]['d'] == a['d']]
            bias_right = [n for n in bias['devices']
                          if mos[n]['type'] == opposite and mos[n]['d'] == b['d']]
            if len(bias_left) != 1 or len(bias_right) != 1:
                continue
            for folded_left in ordered(sources.get(a['d'], [])):
                for folded_right in ordered(sources.get(b['d'], [])):
                    fa, fb = mos[folded_left], mos[folded_right]
                    if fa['type'] != opposite or fb['type'] != opposite or fa['g'] != fb['g']:
                        continue
                    roles = {'input_left': left, 'input_right': right,
                             'folded_left': folded_left, 'folded_right': folded_right,
                             'bias_left': bias_left[0], 'bias_right': bias_right[0]}
                    members = list(roles.values())
                    if len(set(members)) != 6:
                        continue
                    block = add('folded_core', members, roles,
                                {'left_fold': a['d'], 'right_fold': b['d'],
                                 'cascode_bias': fa['g']},
                                layout={'align_columns': [[left, bias_left[0]],
                                                          [right, bias_right[0]]],
                                        'aligned': [[left, bias_left[0]],
                                                    [right, bias_right[0]]],
                                        'branches': [[left, folded_left, bias_left[0]],
                                                     [right, folded_right, bias_right[0]]]})
                    parent(pair, block)
                    parent(bias, block)
                    relation = add('common_gate_pair', [folded_left, folded_right],
                                   {'left': folded_left, 'right': folded_right},
                                   {'bias': fa['g']}, 'relation')
                    parent(relation, block)

    for upstream, a in mos.items():
        for downstream in sources.get(a['d'], []):
            b = mos[downstream]
            if upstream == downstream or a['type'] != b['type']:
                continue
            # A local conduction relation is useful without stealing a core's owner.
            upper = upstream if a['type'] == 'PMOS' else downstream
            lower = downstream if a['type'] == 'PMOS' else upstream
            add('series_branch', [upstream, downstream],
                {'upper': upper, 'lower': lower},
                {'junction': a['d']}, 'relation',
                {'aligned': [[upstream, downstream]],
                 'align_columns': [[upstream, downstream]],
                 'branches': [[upper, lower]]})

    _passive_structures(devices, ordered, add, parent)

    # Assign ownership after every composite is known. Relations never own.
    owners = {}
    for name in devices:
        candidates = [r for r in records if r['confidence'] == 'high' and name in r['devices']]
        if candidates:
            winner = min(candidates, key=lambda r: (len(r['devices']),
                         -_OWN_PRIORITY.get(r['kind'], 0), r['id']))
            owners[name] = winner['id']
    for record in records:
        record['owner'] = {name: owners.get(name) for name in record['devices']}
        record['children'].sort()
    return sorted(records, key=lambda record: record['id'])


def _passive_structures(devices, ordered, add, parent):
    passives = {name: dev for name, dev in devices.items()
                if dev.get('cls') == 'PASSIVE'}
    endpoints = defaultdict(list)
    adjacency = defaultdict(list)
    for name, dev in passives.items():
        endpoints[tuple(sorted((dev['a'], dev['b'])))].append(name)
        adjacency[dev['a']].append((dev['b'], name))
        adjacency[dev['b']].append((dev['a'], name))
    parallels = []
    for nets, names in endpoints.items():
        if len(names) > 1:
            names = ordered(names)
            parallels.append(add('parallel_passives', names, {'members': names},
                                 {'a': nets[0], 'b': nets[1]}))

    # Maximal passive series chains also exist without an amplifier.  A net
    # touching an active/source terminal is an endpoint, even if its passive
    # degree happens to be two. Parallel edges never become a fake chain.
    external_nets = {net for dev in devices.values() if dev.get('cls') != 'PASSIVE'
                     for _, net in _terminals(dev)}
    internal = {net for net, edges in adjacency.items()
                if len(edges) == 2 and len({other for other, _ in edges}) == 2
                and net not in external_nets and net not in SRC_ALIASES
                and net not in SNK_ALIASES}
    visited_edges = set()
    starting_nets = sorted(net for net in adjacency if net not in internal)
    for start in starting_nets:
        for neighbor, edge in adjacency[start]:
            if edge in visited_edges:
                continue
            chain, chain_nets = [edge], [start, neighbor]
            previous, node = edge, neighbor
            while node in internal:
                other, following = next((n, e) for n, e in adjacency[node] if e != previous)
                if following in chain:
                    break
                chain.append(following)
                chain_nets.append(other)
                previous, node = following, other
            visited_edges.update(chain)
            if len(chain) > 1:
                # The direction is meaningful only when associated with an E
                # port; feedback discovery below replaces this neutral order.
                add('series_chain', chain, {'ordered': chain},
                    {'start': chain_nets[0], 'end': chain_nets[-1], 'path': chain_nets},
                    layout={'ordered': chain, 'branches': [chain]})

    def paths(start, target):
        found = []
        # A finite topology cap protects dense non-feedback passive meshes.
        stack = [(start, [], [start])]
        visits = 0
        while stack and visits < 4096 and len(found) < 32:
            node, names, visited = stack.pop()
            visits += 1
            if node == target:
                found.append((names, visited))
                continue
            choices = sorted(adjacency[node], key=lambda item: ordered([item[1]])[0])
            for other, name in reversed(choices):
                if other in visited or other in SNK_ALIASES or other in SRC_ALIASES:
                    continue
                stack.append((other, [*names, name], [*visited, other]))
        return sorted(found, key=lambda item: (len(item[0]), tuple(item[0])))

    for amplifier in ordered(n for n, dev in devices.items() if dev.get('cls') == 'VCVS'):
        dev = devices[amplifier]
        feedback = []
        positive, negative = [], []
        for port, field in (('plus', 'ip'), ('minus', 'in')):
            input_net = dev[field]
            for series, path_nets in paths(dev['op'], input_net):
                if not series:
                    continue
                shunts = ordered(n for n, passive in passives.items()
                                 if n not in series and
                                 ((passive['a'] == input_net and passive['b'] in SNK_ALIASES)
                                  or (passive['b'] == input_net and passive['a'] in SNK_ALIASES)))
                members = [*series, *shunts]
                block = add('feedback_network', members,
                            {'amplifier': amplifier, 'series': series,
                             'shunts': shunts, 'input': port},
                            {'output': dev['op'], 'input': input_net,
                             'path': path_nets},
                            layout={'ordered': series, 'branches': [series, shunts]})
                block['display_name'] = '正反馈网络' if port == 'plus' else '负反馈网络'
                if len(series) > 1:
                    chain = add('series_chain', series, {'ordered': series},
                                {'start': dev['op'], 'end': input_net, 'path': path_nets},
                                layout={'ordered': series, 'branches': [series]})
                    chain['roles']['ordered'] = series
                    chain['nets'] = {'start': dev['op'], 'end': input_net, 'path': path_nets}
                    chain['layout'] = {'ordered': series, 'branches': [series]}
                    parent(chain, block)
                for parallel in parallels:
                    if set(parallel['devices']).issubset(members):
                        parent(parallel, block)
                feedback.append(block)
                (positive if port == 'plus' else negative).extend(members)
        if feedback:
            all_members = [amplifier, *(n for block in feedback for n in block['devices'])]
            core = add('opamp_core', all_members,
                       {'amplifier': amplifier, 'positive_feedback': ordered(positive),
                        'negative_feedback': ordered(negative)},
                       {'output': dev['op'], 'plus': dev['ip'], 'minus': dev['in']})
            for block in feedback:
                parent(block, core)


def structure_owners(structures):
    """Flatten per-record owner maps; relations with no strong owner map to None."""
    return {name: owner for record in structures for name, owner in record['owner'].items()}
