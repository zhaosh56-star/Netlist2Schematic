"""Conservative drawing relationships, separate from functional recognition.

These records describe local grouping/order without altering device semantics,
electrical nets, conduction levels, or the functional matcher's owner graph.
Explicit composites are the only macro blocks; shared bias never unions cores.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from itertools import combinations
from statistics import fmean

VERSION = 'structure-layout-v1'
_ACTIVE = frozenset(('MOS', 'BJT'))
_FUNCTIONAL = frozenset(('differential_pair', 'current_mirror', 'cross_coupled',
                         'complementary_branch', 'output_pair', 'latch', 'folded_core'))
_AUXILIARY = frozenset(('series_branch', 'series_chain', 'parallel_passives',
                        'feedback_network', 'opamp_core'))
_MACROS = frozenset(('latch', 'folded_core'))
_PRIORITY = {'differential_pair': 100, 'bjt_differential_pair': 100,
             'complementary_branch': 95, 'output_pair': 90,
             'current_mirror': 85, 'bjt_current_mirror': 85,
             'cross_coupled': 80, 'matched_branch_pair': 75,
             'paired_load': 70, 'parallel_replicas': 65}


def _rail(net):
    from netlist_core import SRC_ALIASES, SNK_ALIASES
    value = str(net).lower().rstrip('!')
    return value in SRC_ALIASES or value in SNK_ALIASES or value in (
        'gnd', 'gnds', 'vss', 'agnd', 'dgnd', 'vdd', 'vcc', 'vpwr', 'avdd', 'dvdd')


def _ports(device):
    if device.get('cls') == 'MOS':
        return device['d'], device['g'], device['s']
    if device.get('cls') == 'BJT':
        return device['c'], device['b'], device['e']
    return None


def _members(roles, keys):
    return [roles[key] for key in keys if roles.get(key)]


def build_layout_relations(devices, structs=(), *, validated_evidence=None):
    """Return JSON drawing intent; neither input nor functional records mutate."""
    if validated_evidence is not None:
        return _validated_relations(devices, validated_evidence)
    if not structs:
        from circuit_structures import analyze_structures
        structs = analyze_structures(devices)
    records, seen, sources = [], {}, {}

    def add(kind, members, roles, *, evidence=None, rigid=False, macro=False, layout=None):
        members = sorted({name for name in members if name in devices})
        if len(members) < 2:
            return None
        identity = kind, tuple(members)
        if identity in seen:
            return seen[identity]
        row = {'id': 'layout:' + kind + ':' + '|'.join(members), 'kind': kind,
               'members': members, 'roles': deepcopy(roles),
               'evidence': deepcopy(evidence or {}), 'confidence': 'high' if rigid or macro else 'relation',
               'rigid': bool(rigid), 'macro': bool(macro), 'parent': None,
               'children': [], 'owned_members': [], 'layout': deepcopy(layout or {})}
        records.append(row)
        seen[identity] = row
        return row

    for structure in structs:
        kind, roles = structure.get('kind'), deepcopy(structure.get('roles') or {})
        if kind not in _FUNCTIONAL | _AUXILIARY:
            continue
        if kind in _FUNCTIONAL and structure.get('confidence', 'high') != 'high':
            continue
        members = structure.get('devices', structure.get('members', ()))
        layout = {'pairs': [], 'same_row': [], 'branches': [], 'orders': []}
        if kind in ('differential_pair', 'cross_coupled'):
            pair = _members(roles, ('left', 'right'))
            if len(pair) == 2:
                layout['pairs'] = [pair]
                layout['orders'] = [pair]
        elif kind == 'current_mirror':
            reference = roles.get('reference')
            outputs = roles.get('outputs', ())
            layout['same_row'] = [[reference, *outputs]] if reference else []
            if reference and len(outputs) == 1:
                layout['pairs'] = [[reference, outputs[0]]]
        elif kind in ('complementary_branch', 'output_pair', 'series_branch'):
            branch = _members(roles, ('upper', 'lower'))
            if len(branch) == 2:
                layout['branches'] = [branch]
        elif kind == 'latch':
            for part in ('upper', 'lower'):
                pair = _members(roles, ('left_' + part, 'right_' + part))
                if len(pair) == 2:
                    layout['pairs'].append(pair)
                    layout['orders'].append(pair)
            for side in ('left', 'right'):
                branch = _members(roles, (side + '_upper', side + '_lower'))
                if len(branch) == 2:
                    layout['branches'].append(branch)
        elif kind == 'folded_core':
            for prefix in ('input', 'folded', 'bias'):
                pair = _members(roles, (prefix + '_left', prefix + '_right'))
                if len(pair) == 2:
                    layout['pairs'].append(pair)
                    # Input and folded/output subcores may use different axes.
                    if prefix != 'input':
                        layout['orders'].append(pair)
        else:
            layout.update(deepcopy(structure.get('layout') or {}))
        rigid = (kind in _FUNCTIONAL - _MACROS and not (
            kind == 'current_mirror' and (len(roles.get('references', ())) > 1
                                          or len(roles.get('outputs', ())) != 1)))
        row = add(kind, members, roles, rigid=rigid, macro=kind in _MACROS,
                  evidence={'kind': 'functional_roles' if kind in _FUNCTIONAL else 'existing_attachment',
                            'source_structure': structure.get('id'),
                            'nets': deepcopy(structure.get('nets', {}))}, layout=layout)
        if row is not None and structure.get('id'):
            sources[structure['id']] = row

    # Give a folded core local two-device owners, rather than making its input
    # pair and output stack one transitive disjoint-set component.
    for parent in list(records):
        if parent['kind'] == 'folded_core':
            for pair in parent['layout']['pairs']:
                add('matched_branch_pair', pair, {'left': pair[0], 'right': pair[1]},
                    rigid=True, evidence={'kind': 'explicit_composite_roles',
                                          'source_structure': parent['evidence']['source_structure']},
                    layout={'pairs': [pair], 'orders': [pair]})

    # Strict BJT seeds: a shared supply alone, or an unfed emitter node, does not
    # establish an input pair. Visible collector/base roles remain distinct.
    emitters, mirror_banks = defaultdict(list), defaultdict(list)
    voltage_rails = set()
    for name, dev in devices.items():
        if dev.get('cls') == 'BJT':
            emitters[(dev['type'], dev['e'])].append(name)
            mirror_banks[(dev['type'], dev['e'], dev['b'])].append(name)
        elif dev.get('cls') == 'VSRC':
            if _rail(dev['n']):
                voltage_rails.add(dev['p'])
            if _rail(dev['p']):
                voltage_rails.add(dev['n'])
    for (_, source), names in emitters.items():
        names = sorted(names)
        if len(names) != 2 or _rail(source) or source in voltage_rails:
            continue
        a, b = (devices[name] for name in names)
        if a['c'] == b['c'] or a['b'] == b['b']:
            continue
        tails = sorted(name for name, dev in devices.items() if name not in names and (
            (dev.get('cls') == 'ISRC' and source in (dev['p'], dev['n']))
            or (dev.get('cls') == 'MOS' and dev['d'] == source)
            or (dev.get('cls') == 'BJT' and dev['c'] == source)))
        if tails:
            add('bjt_differential_pair', names,
                {'left': names[0], 'right': names[1], 'tail': tails, 'source_net': source},
                rigid=True, evidence={'kind': 'two_emitters_and_tail', 'tail': tails},
                layout={'pairs': [names], 'orders': [names]})
    for (_, source, control), names in mirror_banks.items():
        refs = sorted(name for name in names if devices[name]['c'] == control)
        outputs = sorted(name for name in names if devices[name]['c'] != control)
        if not refs or not outputs:
            continue
        pairs = [[refs[0], outputs[0]]] if len(refs) == len(outputs) == 1 else []
        add('bjt_current_mirror', names,
            {'reference': refs[0], 'references': refs, 'outputs': outputs,
             'shared_source': source, 'shared_control': control}, rigid=bool(pairs),
            evidence={'kind': 'base_collector_reference'},
            layout={'pairs': pairs, 'same_row': [sorted(names)]})

    # Full visible-port identity is a stronger repeatability proof than sharing
    # an arbitrary rail. Large banks remain soft groups, never arbitrary pairs.
    exact = defaultdict(list)
    for name, dev in devices.items():
        ports = _ports(dev)
        if ports:
            exact[(dev['cls'], dev['type'], *ports)].append(name)
    for signature, names in exact.items():
        if len(names) < 2:
            continue
        names = sorted(names)
        add('parallel_replicas', names, {'members': names}, rigid=len(names) == 2,
            evidence={'kind': 'identical_visible_ports', 'ports': list(signature[2:])},
            layout={'pairs': [names] if len(names) == 2 else [], 'same_row': [names]})

    # A pair of loads needs a pre-existing pair of branches. Sources/control
    # must coincide, while each load's drain/collector reaches exactly its own
    # anchor; common bias groups of three or more are not arbitrarily paired.
    anchors = []
    for row in records:
        if row['kind'] in ('differential_pair', 'bjt_differential_pair', 'cross_coupled',
                           'matched_branch_pair', 'latch', 'folded_core'):
            for pair in row.get('layout', {}).get('pairs', ()):
                if len(pair) == 2 and all(name in devices and _ports(devices[name]) for name in pair):
                    anchors.append((pair, row['id']))
    active = {name: dev for name, dev in devices.items() if dev.get('cls') in _ACTIVE}
    proposals = {}
    for (left, right), anchor_id in anchors:
        first, second = devices[left], devices[right]
        for port in ('d', 's') if first['cls'] == 'MOS' else ('c', 'e'):
            x, y = first[port], second[port]
            if x == y or _rail(x) or _rail(y):
                continue
            users = []
            for net in (x, y):
                users.append([name for name, dev in active.items() if name not in (left, right)
                              and _ports(dev)[0] == net])
            if len(users[0]) != 1 or len(users[1]) != 1:
                continue
            a, b = users[0][0], users[1][0]
            aa, bb = active[a], active[b]
            pa, pb = _ports(aa), _ports(bb)
            if (aa['cls'], aa['type']) != (bb['cls'], bb['type']) or pa[1:] != pb[1:]:
                continue
            if not _rail(pa[2]) or pa[0] == pa[1] or pb[0] == pb[1]:
                continue
            identity = frozenset((a, b))
            proposals.setdefault(identity, (a, b, left, right, port, anchor_id))
    partners = defaultdict(set)
    for a, b, *_ in proposals.values():
        partners[a].add(b)
        partners[b].add(a)
    for a, b, left, right, port, anchor_id in proposals.values():
        if len(partners[a]) != 1 or len(partners[b]) != 1:
            continue
        source, control = _ports(active[a])[2], _ports(active[a])[1]
        add('paired_load', [a, b],
            {'left': a, 'right': b, 'anchor_pair': [left, right],
             'anchor_pins': [port.upper(), port.upper()],
             'shared_source': source, 'shared_control': control}, rigid=True,
            evidence={'kind': 'one_load_per_paired_branch', 'anchor_relation': anchor_id},
            layout={'pairs': [[a, b]], 'orders': [[a, b]]})

    # Only explicit functional composites establish parenthood. A newly inferred
    # load relation never becomes a parent through its shared bias or anchors.
    source_by_id = {row.get('id'): row for row in structs}
    for row in records:
        source = source_by_id.get(row['evidence'].get('source_structure'))
        parent = sources.get(source.get('parent')) if source else None
        if parent and parent['macro'] and set(row['members']) <= set(parent['members']):
            row['parent'] = parent['id']
        elif row['kind'] == 'matched_branch_pair':
            parent = sources.get(row['evidence'].get('source_structure'))
            if parent and parent['macro']:
                row['parent'] = parent['id']
        if row['parent']:
            next(parent for parent in records if parent['id'] == row['parent'])['children'].append(row['id'])
    # A tail is external evidence, not an owned member of its input-pair block.
    tail_users = defaultdict(set)
    pair_members = {name for row in records for pair in row.get('layout', {}).get('pairs', ()) for name in pair}
    for row in records:
        if row['kind'] in ('differential_pair', 'bjt_differential_pair'):
            for name in row['roles'].get('tail', ()):
                tail_users[name].add(row['id'])
    for row in records:
        if row['kind'] in ('differential_pair', 'bjt_differential_pair'):
            row['roles']['exclusive_tails'] = [name for name in row['roles'].get('tail', ())
                                              if len(tail_users[name]) == 1 and name not in pair_members]

    # Disjoint lowest-level ownership replaces transitive union. Soft banks and
    # macros own no local cells; macros still have explicit collective moves.
    for name in devices:
        choices = [row for row in records if row['rigid'] and not row['macro'] and name in row['members']]
        if choices:
            chosen = min(choices, key=lambda row: (len(row['members']), -_PRIORITY.get(row['kind'], 0), row['id']))
            chosen['owned_members'].append(name)
    for row in records:
        row['owned_members'].sort()
        row['children'] = sorted(set(row['children']))
    return sorted(records, key=lambda row: row['id'])


def _validated_relations(devices, evidence):
    """Resolve leaf ownership without reintroducing legacy, rejected matches."""
    if not isinstance(evidence, dict) or not isinstance(evidence.get('relations'), list):
        raise ValueError('Validated layout evidence must contain relations')
    records = deepcopy(evidence['relations'])
    priorities = {**_PRIORITY, 'corresponding_branch_pair': 75}
    by_id = {}
    for row in records:
        members = row.get('members', ())
        if (not isinstance(row.get('id'), str) or row['id'] in by_id
                or len(members) != len(set(members)) or not set(members) <= set(devices)):
            raise ValueError('Invalid or duplicate validated layout relation')
        by_id[row['id']] = row
        row['owned_members'] = []
        row['children'] = []
        if row.get('macro') and row.get('kind') == 'paired_branches':
            row.setdefault('layout', {})['orders'] = deepcopy(row.get('layout', {}).get('pairs', []))
    for row in records:
        parent = row.get('parent')
        if parent is None:
            continue
        if (parent not in by_id or not by_id[parent].get('macro')
                or not set(row['members']) <= set(by_id[parent]['members'])):
            raise ValueError('Invalid explicit layout parent')
        chain, ancestor = {row['id']}, parent
        while ancestor is not None:
            if ancestor in chain:
                raise ValueError('Cyclic explicit layout parents')
            chain.add(ancestor)
            ancestor = by_id.get(ancestor, {}).get('parent')
        by_id[parent]['children'].append(row['id'])
    for name in devices:
        owners = [row for row in records if row.get('rigid') and not row.get('macro')
                  and name in row['members']]
        if owners:
            chosen = min(owners, key=lambda row: (len(row['members']),
                         -priorities.get(row['kind'], 0), row['id']))
            chosen['owned_members'].append(name)
    for row in records:
        row['owned_members'].sort()
        row['children'].sort()
    return sorted(records, key=lambda row: row['id'])


def placement_blocks(comps, index, relations):
    """Return disjoint local owners plus explicit, potentially nested macros."""
    local = [[index[name] for name in row.get('owned_members', ()) if name in index]
             for row in relations]
    local = [members for members in local if members]
    owned = {i for block in local for i in block}
    local.extend([i] for i in range(len(comps)) if i not in owned)
    macros = [[index[name] for name in row.get('members', ()) if name in index]
              for row in relations if row.get('macro')]
    return local, [members for members in macros if len(members) > 1]


def measure_layout_relations(pl, comps, index, relations):
    """Independent physical grouping metrics, without routing or old Q edits."""
    interleaving = role_order = 0
    spans, details = [], []
    positions = {name: pl.pos_of(i) for name, i in index.items()}
    by_id = {row['id']: row for row in relations}
    counted = set()
    for relation in relations:
        members = [name for name in relation.get('members', ()) if name in positions]
        if len(members) < 2:
            continue
        # Non-owning overlapping leaf relations describe connection intent;
        # evaluating them as additional rigid boxes recreates the old conflict.
        assess = relation.get('macro') or len(relation.get('owned_members', ())) > 1
        identity = tuple(sorted(members))
        if not assess or identity in counted:
            continue
        counted.add(identity)
        local_interleaving = 0
        for row in sorted({pl.cell_of[index[name]][0] for name in members}):
            row_members = [name for name in members if pl.cell_of[index[name]][0] == row]
            lo, hi = min(pl.cell_of[index[name]][1] for name in row_members), max(
                pl.cell_of[index[name]][1] for name in row_members)
            local_interleaving += sum(name not in members and pl.cell_of[i][0] == row
                                      and lo < pl.cell_of[i][1] < hi for name, i in index.items())
        normal = reverse = 0
        for order in relation.get('layout', {}).get('orders', ()):
            values = [name for name in order if name in positions]
            for a, b in combinations(values, 2):
                if pl.cell_of[index[a]][0] != pl.cell_of[index[b]][0]:
                    continue
                normal += positions[a][0] > positions[b][0]
                reverse += positions[a][0] < positions[b][0]
        inversions = min(normal, reverse)
        xs, ys = zip(*(positions[name] for name in members))
        span = max(xs)-min(xs)+max(ys)-min(ys)
        normalized = span / (100.0 * max(1, len(members)-1))
        spans.append((span, normalized))
        interleaving += local_interleaving
        role_order += inversions
        details.append({'id': relation['id'], 'kind': relation['kind'],
                        'members': members, 'interleaving': local_interleaving,
                        'role_order': inversions, 'span': span, 'normalized_span': normalized})
    return {'interleaving': interleaving, 'role_order': role_order,
            'span': sum(span for span, _ in spans),
            'normalized_span': sum(value for _, value in spans), 'relations': details,
            'relation_count': len(relations), 'measured_blocks': len(details)}


def prepare_structure_placement(pl, comps, index, relations):
    """Repack existing rows into disjoint owners, preserving axes and orientation."""
    local, macros = placement_blocks(comps, index, relations)
    pl.blocks = local
    pl.block_of = {i: n for n, block in enumerate(local) for i in block}
    pl.layout_macro_blocks = macros
    pl.motion_blocks = local + [macro for macro in macros if macro not in local]
    pl.layout_relations = deepcopy(relations)
    pl.role_orders = [[[index[name] for name in order if name in index]
                       for order in relation.get('layout', {}).get('orders', ())]
                      for relation in relations if relation.get('macro')]
    # Move whole local owners together in every row. Compact order follows their
    # existing geometric centre, rather than arbitrary net or device names.
    cells = list(pl.cell_of)
    fixed = set(getattr(pl, 'fixed', ())) | set(getattr(pl, 'fixed_cells', {}))
    for row in sorted({r for r, _ in cells}):
        groups = [sorted((i for i in block if cells[i][0] == row), key=lambda i: cells[i][1])
                  for block in local]
        groups = [group for group in groups if group]
        # Fixed cells are never displaced during repacking. If any occur, leave
        # that row intact; proposals/validation keep it protected subsequently.
        if any(i in fixed for group in groups for i in group):
            continue
        groups.sort(key=lambda group: (fmean(cells[i][1] for i in group),
                                       min(cells[i][1] for i in group)))
        # Nest only explicit composite membership. Shared bias/feedback never
        # takes part in this row packing and cannot fuse independent cores.
        for macro in sorted(macros, key=len):
            macro_set = set(macro)
            contained = [group for group in groups if set(group) <= macro_set]
            if len(contained) > 1:
                merged = [i for group in contained for i in group]
                groups = [group for group in groups if group not in contained]
                groups.append(merged)
                groups.sort(key=lambda group: (fmean(cells[i][1] for i in group),
                                               min(cells[i][1] for i in group)))
        occupied = sorted(cells[i][1] for group in groups for i in group)
        ordered = [i for group in groups for i in group]
        for col, i in zip(occupied, ordered):
            cells[i] = (row, col)
    pl.set_state(cells, pl.orient)
    return pl
