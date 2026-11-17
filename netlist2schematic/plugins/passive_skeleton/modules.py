"""Two-terminal series/parallel modules, without merging electrical nodes.

External taps, supplies and open ports are boundaries. Reductions only build
a layout tree; every leaf retains its original pins and both original nets.
Non-series/parallel bridges are reported rather than flattened arbitrarily.
"""
from collections import defaultdict
from copy import deepcopy
from hashlib import sha256

from netlist2schematic.common.passive_coordinates import PASSIVE
from netlist2schematic.plugins.passive_embedding.passive_embedding import build_plan


def reverse(tree):
    result = deepcopy(tree)
    result['ends'].reverse()
    if result['kind'] == 'series':
        result['children'] = [reverse(c) for c in reversed(result['children'])]
    elif result['kind'] == 'parallel':
        result['children'] = [reverse(c) for c in result['children']]
    return result


def face(tree, start):
    return deepcopy(tree) if tree['ends'][0] == start else reverse(tree)


def signature(tree):
    if tree['kind'] == 'part':
        return ('part', tree['symbol'])
    children = [signature(c) for c in tree['children']]
    if tree['kind'] == 'parallel':
        children.sort()
    return (tree['kind'], tuple(children))


def members(tree):
    if tree['kind'] == 'part':
        return [tree['device']]
    return [name for child in tree['children'] for name in members(child)]


def _node(kind, ends, children):
    # Associativity does not manufacture an extra block or a false boundary.
    flattened = []
    for child in children:
        flattened.extend(child['children'] if child['kind'] == kind else [child])
    if kind == 'parallel':
        flattened.sort(key=lambda c: (signature(c), tuple(members(c))))
    return dict(kind=kind, ends=list(ends), children=flattened)


def _reduce(edges, ends):
    edges = deepcopy(edges)
    while len(edges) > 1:
        pairs = defaultdict(list)
        for edge in edges:
            pairs[tuple(sorted(edge['ends']))].append(edge)
        parallel = next(((pair, bank) for pair, bank in sorted(pairs.items()) if len(bank) > 1), None)
        if parallel is not None:
            pair, bank = parallel
            selected = {id(e) for e in bank}
            edges = [e for e in edges if id(e) not in selected]
            edges.append(_node('parallel', pair, [face(e, pair[0]) for e in bank]))
            continue
        incident = defaultdict(list)
        for edge in edges:
            for net in edge['ends']:
                incident[net].append(edge)
        joint = next((net for net in sorted(incident) if net not in ends and len(incident[net]) == 2), None)
        if joint is None:
            return None
        a, b = incident[joint]
        start = next(n for n in a['ends'] if n != joint)
        finish = next(n for n in b['ends'] if n != joint)
        if start == finish:
            return None
        edges = [e for e in edges if e is not a and e is not b]
        edges.append(_node('series', (start, finish), [face(a, start), face(b, joint)]))
    if not edges or set(edges[0]['ends']) != set(ends):
        return None
    return face(edges[0], ends[0])


def build_modules(par, comps, nets):
    """Maximal unambiguous two-port modules, cut at *all* external taps.

    Equal boundary pairs may form a parallel bank. Sharing just one supply,
    bias node or signal junction cannot glue unrelated modules together.
    """
    legacy = build_plan(par, comps, nets)
    parts = {c['id']: c for c in comps}
    all_passives = {n for n, c in parts.items() if c['symbol'] in PASSIVE}
    eligible = {n for n in all_passives if len(parts[n]['nodes']) == 2
                and len(set(parts[n]['nodes'].values())) == 2}
    boundary = {net for net, terminals in nets.items() if par.power_marker_sym(net)
                or any(n not in eligible for n, pin in terminals) or len(terminals) == 1}
    unseen = set(eligible)
    pieces = defaultdict(list)
    skipped = [dict(members=[n], reason='not_a_distinct_two_terminal_part')
               for n in sorted(all_passives-eligible)]
    while unseen:
        todo = [min(unseen)]; found = set()
        while todo:
            name = todo.pop()
            if name in found:
                continue
            found.add(name)
            for net in parts[name]['nodes'].values():
                if net not in boundary:
                    todo.extend(n for n, pin in nets[net] if n in eligible and n not in found)
        unseen -= found
        ends = sorted({net for name in found for net in parts[name]['nodes'].values() if net in boundary})
        if len(ends) != 2:
            skipped.append(dict(members=sorted(found), ports=ends,
                                reason='closed_or_multiport_passive_module'))
            continue
        pieces[tuple(ends)].extend(sorted(found))
    old_groups = {frozenset(g['members']): g for g in legacy['groups']}
    groups = []
    for ends, names in sorted(pieces.items()):
        leaves = [dict(kind='part', device=name, symbol=parts[name]['symbol'],
                       ends=list(parts[name]['nodes'].values()), pins=deepcopy(parts[name]['nodes']))
                  for name in names]
        tree = _reduce(leaves, ends)
        if tree is None:
            skipped.append(dict(members=sorted(names), ports=list(ends), reason='not_series_parallel'))
            continue
        if frozenset(names) in old_groups:
            group = deepcopy(old_groups[frozenset(names)])
        else:
            group = dict(id=sha256(repr((ends, sorted(names))).encode()).hexdigest()[:16],
                         kind='series_parallel', members=sorted(names), paths=[], ends=list(ends),
                         evidence='two_port_series_parallel_reduction_without_external_tap')
            group['anchors'] = {net: [list(t) for t in nets[net] if t[0] not in names
                                      and t[0] not in all_passives]
                                if not par.power_marker_sym(net) else [] for net in ends}
        group.update(module=tree, module_signature=signature(tree),
                     boundary_ports={net: [list(t) for t in nets[net] if t[0] in names] for net in ends},
                     internal_nets=sorted({net for n in names for net in parts[n]['nodes'].values()}-set(ends)),
                     electrical_edges=leaves)
        groups.append(group)
    return dict(version='passive-modules-v2', groups=groups, skipped=skipped,
                passive_count=len(all_passives), covered_count=sum(len(g['members']) for g in groups),
                terminals=legacy['terminals'], electrical_node_merges=[])
