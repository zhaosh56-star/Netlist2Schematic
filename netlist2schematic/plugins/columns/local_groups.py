"""Local children take precedence over an incompatible through-column.

Only recognized, unambiguous row-local motifs supply protection. A junction
may remain an electrical connection without forcing two functional blocks to
share a column. No names, GT coordinates or circuit-size thresholds are used.
"""
from collections import defaultdict
from copy import deepcopy
from hashlib import sha256

import networkx as nx

from netlist2schematic.layout.structure_hierarchy import compile_hierarchy
from netlist2schematic.recognition.transistor_topology import CHANNELS, common_kind

VERSION = 'local-children-before-columns-v1'
EPS = 1e-8


def local_groups(pl, comps, index, structures):
    records = []
    for source in structures:
        record = deepcopy(source)
        # Historical BJT tags and current shared tags describe the same motif.
        # Normalize only this evidence view, never the caller's saved records.
        record['kind'] = common_kind(record['kind'])
        # Public topology fixtures sometimes contain only role declarations.
        if 'devices' not in record:
            roles = record.get('roles', {})
            names = []
            for value in roles.values():
                names.extend(value if isinstance(value, (list, tuple)) else [value])
            record['devices'] = [n for n in names if isinstance(n, str) and n in index]
        record['devices'] = [n for n in record['devices']
                             if n in index and comps[index[n]]['symbol'] in CHANNELS]
        record.setdefault('id', record['kind'] + ':' + repr(sorted(record['devices'])))
        records.append(record)
    hierarchy, _ = compile_hierarchy(records, comps, index,
        [cell[0] for cell in pl.cell_of], getattr(pl, 'blocks', [[i] for i in range(len(comps))]))
    confirmed = [g for g in hierarchy['groups'] if g['protection'] != 'ambiguous_overlap']
    leaves = [g for g in confirmed if not any(set(h['members']) < set(g['members']) for h in confirmed)]
    result = []
    for group in leaves:
        names = sorted(group['members'])
        identity = sha256(repr(tuple(names)).encode()).hexdigest()[:16]
        result.append(dict(id='local:' + identity, members=names,
                           row=group['row'], evidence=deepcopy(group['evidence'])))
    return result


def measure_groups(pl, index, groups):
    result = []
    for group in groups:
        ids = [index[n] for n in group['members']]
        cols = [pl.cell_of[i][1] for i in ids]
        xs = [pl.pos_of(i)[0] for i in ids]
        result.append(dict(id=group['id'], members=list(group['members']),
            column_span=max(cols)-min(cols), horizontal_span=max(xs)-min(xs),
            rows=sorted({pl.cell_of[i][0] for i in ids})))
    return result


def expanded_groups(before, trial, index, groups):
    """Per child, not a summed score: improving one cannot sacrifice another."""
    a, b = measure_groups(before, index, groups), measure_groups(trial, index, groups)
    return [dict(id=x['id'], members=x['members'], before=x, after=y)
            for x, y in zip(a, b) if y['rows'] != x['rows']
            or y['column_span'] > x['column_span']
            or y['horizontal_span'] > x['horizontal_span'] + EPS]


def release_conflicting_junctions(edges, active, groups):
    """Break only weak common-terminal joins in cyclic adjacency demands.

    A set of row-local pairs may demand A-B-C-D-A neighboring columns. No
    linear column order can honor that cycle. If a column traverses a simple
    complementary D-D/C-C junction between confirmed children, treat that
    junction as a handoff. True D-S/C-E series, inline attachments, explicit
    composite roles and repeated-stack evidence remain column constraints.
    All equivalent weak joins are released together; names never break ties.
    """
    graph = nx.Graph()
    graph.add_nodes_from(active)
    graph.add_edges_from((e['a'], e['b']) for e in edges)
    owner = {n: frozenset(component) for component in nx.connected_components(graph) for n in component}
    adjacency = nx.Graph()
    pair_nodes = defaultdict(set)
    for group in groups:
        if len(group['members']) != 2:
            continue
        a, b = group['members']
        if owner[a] != owner[b]:
            adjacency.add_edge(owner[a], owner[b])
            pair_nodes[owner[a]].add(a)
            pair_nodes[owner[b]].add(b)
    cyclic = set()
    for component in nx.biconnected_components(adjacency):
        if len(component) >= 3 and adjacency.subgraph(component).number_of_edges() >= len(component):
            cyclic.update(component)
    if not cyclic:
        return edges, []
    retained, released = [], []
    for edge in edges:
        a, b = edge['a'], edge['b']
        weak = (owner[a] in cyclic and edge.get('unique') and not edge.get('explicit')
                and not edge.get('via') and edge['pin_a'] == edge['pin_b']
                and active[a]['symbol'] != active[b]['symbol']
                and edge['evidence'] == 'unique_active_continuation')
        if weak:
            split = graph.subgraph(owner[a]).copy()
            split.remove_edge(a, b)
            sides = list(nx.connected_components(split))
            weak = len(sides) == 2 and all(s & pair_nodes[owner[a]] for s in sides)
        if weak:
            released.append(dict(edge, reason='cyclic_local_pair_column_conflict',
                handling='electrical_handoff_not_fixed_column',
                local_children=[g['id'] for g in groups if set(g['members']) & owner[a]]))
        else:
            retained.append(edge)
    return retained, released
