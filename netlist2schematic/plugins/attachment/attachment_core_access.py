"""Bounded after-SA access trials for a terminal-proven local mirror core.

Rows, axes, passive/source guests and unrelated devices remain fixed. Only
explicit branch roles may change columns. Facing-only trials retain every
conduction point using the existing geometry-aware mirror/offset helper.
"""
from collections import Counter
from copy import deepcopy
from time import perf_counter

from netlist2schematic.plugins.attachment.attachment_cores import _PINS, measure_cores
from netlist2schematic.plugins.layout_shared.branch_alignment import _grid_offset
from netlist2schematic.plugins.region.region_controls import _face_control

VERSION = 'local-core-access-v1'
EPS = 1e-8


def _world(pl, i):
    return tuple(pl.cell_of[i]), pl.orient[i], tuple(pl.pos_of(i))


def access_trials(pl, comps, index, model, core, *, grid_step=2, deadline=None):
    """A small deterministic set, never a name/GT-dependent placement template."""
    from netlist2schematic.plugins.attachment.attachment_integration import _fixed_ids
    if not measure_cores(pl, comps, index, dict(model, cores=[core]))['measurable']:
        return
    claimed = Counter(n for item in model.get('cores', ()) for n in item['terminal_contract'])
    if any(claimed[n] > 1 for n in core['terminal_contract']):
        return
    roles = core['roles']
    tails = [index[roles[k]] for k in ('reference', 'output')]
    heads = [index[roles[k]] for k in ('reference_head', 'output_head')]
    if pl.cell_of[tails[0]][0] != pl.cell_of[tails[1]][0]:
        return
    fixed = _fixed_ids(pl, index)
    variants = [({}, 'face_controls')]
    variants.append(({h: pl.cell_of[t][1] for h, t in zip(heads, tails)}, 'heads_to_mirror'))
    variants.append(({t: pl.cell_of[h][1] for h, t in zip(heads, tails)}, 'mirror_to_heads'))
    variants.extend(({h: pl.cell_of[t][1]}, 'one_branch_head') for h, t in zip(heads, tails))
    seen = set()
    for columns, strategy in variants:
        if deadline is not None and perf_counter() >= deadline:
            return
        if any(i in fixed and pl.cell_of[i][1] != col for i, col in columns.items()):
            continue
        trial = deepcopy(pl)
        trial.offset_x = list(getattr(pl, 'offset_x', [0.0]*len(comps)))
        cells = list(trial.cell_of)
        for i, col in columns.items():
            cells[i] = cells[i][0], col
            if cells[i] != pl.cell_of[i]:
                trial.offset_x[i] = 0.0
        if len(set(cells)) != len(cells):
            continue
        trial.set_state(cells, trial.orient)
        usable = True
        for pair in (tails, heads):
            if trial.cell_of[pair[0]][0] != trial.cell_of[pair[1]][0]:
                continue
            ordered = sorted(pair, key=lambda i: trial.pos_of(i)[0])
            if (abs(trial.pos_of(ordered[1])[0]-trial.pos_of(ordered[0])[0]) < EPS
                    or not all(_face_control(trial, i, d, grid_step)
                               for i, d in zip(ordered, ('east', 'west')))):
                usable = False
                break
        if not usable:
            continue
        # A corresponding fork is still a fork. This trial only brings the
        # explicit pair of branch endpoints closer; it does not align all users.
        for h, t in zip(heads, tails):
            if h not in columns or h in fixed:
                continue
            head_pin = _PINS[comps[h]['symbol']][0]
            tail_pin = _PINS[comps[t]['symbol']][0]
            delta = trial.pin_world(t, tail_pin)[0] - trial.pin_world(h, head_pin)[0]
            dx = _grid_offset(trial, h, trial.offset_x[h]+delta, grid_step)
            if dx is not None:
                trial.offset_x[h] = dx
        if any(_world(trial, i) != _world(pl, i) for i in fixed):
            continue
        key = tuple(trial.cell_of), tuple(trial.orient), tuple(trial.offset_x)
        if key in seen:
            continue
        seen.add(key)
        conductors_frozen = all(trial.pin_world(i, p) == pl.pin_world(i, p)
            for i in tails+heads for p in (_PINS[comps[i]['symbol']][0], _PINS[comps[i]['symbol']][2]))
        yield trial, dict(kind='local_core_access', version=VERSION, core=core['id'],
            strategy=strategy, members=list(core['terminal_contract']),
            changed_devices=[comps[i]['id'] for i in tails+heads if _world(trial, i) != _world(pl, i)],
            rows_unchanged=True, axes_unchanged=True, facing_only_conduction_points_frozen=conductors_frozen)
