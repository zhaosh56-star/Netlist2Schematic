"""Keep confirmed row-local children intact inside movable composite blocks.

Recognition and motion ownership are different things.  A union of overlapping
motifs is useful for a collective move, but does not protect its children.  This
module compiles independent, nested row intervals; it never reserves a parent's
whole two-dimensional bounding box or changes a conduction row.
"""
from __future__ import annotations

from netlist2schematic.recognition.transistor_topology import common_kind

from collections import defaultdict
from copy import deepcopy

VERSION = 'structure-child-intervals-v1'
ACTIVE_KINDS = frozenset(('differential_pair', 'current_mirror', 'cross_coupled',
                         'complementary_branch', 'output_pair', 'latch', 'folded_core'))


def compile_hierarchy(structures, comps, index, row_of, blocks):
    """Return an auditable plan and extra guards not implied by motion blocks.

    Only confirmed active motifs (or a confirmed composite's explicit roles)
    supply constraints. Partial overlaps cannot be made into a hierarchy: keep
    them as reported relations rather than arbitrarily choosing by instance ID.
    """
    groups, skipped = {}, []
    owner = {i: block for block in blocks for i in block}

    def add(record, names, role=None):
        ids = [index[name] for name in names if name in index]
        by_row = defaultdict(list)
        for i in ids:
            by_row[row_of[i]].append(i)
        if (len(ids) == 2 and len(by_row) > 1
                and (role is not None or common_kind(record['kind']) in
                     ('differential_pair', 'cross_coupled', 'current_mirror'))):
            skipped.append({'structure': record['id'], 'role': role,
                            'members': [comps[i]['id'] for i in ids],
                            'reason': 'different_fixed_rows'})
        for row, members in by_row.items():
            if len(members) < 2:
                continue
            key = frozenset(members)
            group = groups.setdefault(key, {'row': row,
                'members': [comps[i]['id'] for i in sorted(key)], 'evidence': []})
            group['evidence'].append({'structure': record['id'], 'kind': record['kind'],
                                     'parent': record.get('parent'), 'role': role})

    for record in structures:
        if record.get('confidence', 'high') != 'high' or common_kind(record['kind']) not in ACTIVE_KINDS:
            continue
        add(record, record['devices'])
        if record['kind'] == 'folded_core':
            # These pairs have two-sided port evidence in the confirmed core;
            # an arbitrary common bias or common supply is insufficient.
            for role in ('input', 'folded', 'bias'):
                names = [record.get('roles', {}).get(role + '_' + side)
                         for side in ('left', 'right')]
                if all(name in index for name in names):
                    add(record, names, role)

    ambiguous = set()
    keys = list(groups)
    for n, left in enumerate(keys):
        for right in keys[n + 1:]:
            if left & right and not (left <= right or right <= left):
                ambiguous.update((left, right))
    guards = []
    for members, group in groups.items():
        if members in ambiguous:
            group['protection'] = 'ambiguous_overlap'
            continue
        containing = owner[next(iter(members))]
        row_members = {i for i in containing if row_of[i] == group['row']}
        if members < row_members:
            group['protection'] = 'child_guard'
            guards.append(sorted(members))
        else:
            group['protection'] = 'existing_block'
    return {'version': VERSION, 'groups': list(groups.values()), 'skipped': skipped,
            'guard_count': len(guards)}, guards


def order_children(values, guards):
    """Stable hierarchical packing, preserving the barycentric order of units."""
    ranks = {i: n for n, i in enumerate(values)}
    sets = [frozenset(group) for group in guards if set(group) <= set(values)]

    def pack(members):
        children = [s for s in sets if s < members]
        children = [s for s in children if not any(s < other for other in children)]
        covered = set().union(*children) if children else set()
        units = children + [frozenset((i,)) for i in members - covered]
        units.sort(key=lambda s: (sum(ranks[i] for i in s) / len(s), min(ranks[i] for i in s)))
        return [i for unit in units for i in (pack(unit) if len(unit) > 1 else unit)]

    return pack(frozenset(values))


def hierarchy_report(placement, plan=None):
    """Measure actual coordinates; reporting never repairs or moves a device."""
    plan = plan if plan is not None else getattr(placement, 'structure_hierarchy', None)
    if plan is None:
        return None
    result = deepcopy(plan)
    index = {comp['id']: i for i, comp in enumerate(placement.comps)}
    interlopers = gaps = 0
    for group in result['groups']:
        members = {index[name] for name in group['members'] if name in index}
        by_row = defaultdict(list)
        for i in members:
            by_row[placement.cell_of[i][0]].append(placement.cell_of[i][1])
        violations = []
        for row, cols in by_row.items():
            for i, (r, col) in enumerate(placement.cell_of):
                if r == row and min(cols) < col < max(cols) and i not in members:
                    violations.append(placement.comps[i]['id'])
            gaps += max(cols) - min(cols) + 1 - len(cols)
        group['interlopers'] = violations
        group['actual_rows'] = sorted(by_row)
        group['positions'] = {placement.comps[i]['id']: list(placement.pos_of(i)) for i in members}
        interlopers += len(violations)
    result['metrics'] = {'interleaving': interlopers, 'empty_or_foreign_slots': gaps,
                         'different_fixed_rows': sum(s['reason'] == 'different_fixed_rows'
                                                     for s in result['skipped'])}
    return result
