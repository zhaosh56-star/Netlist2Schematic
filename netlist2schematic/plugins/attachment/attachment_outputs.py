"""Neutral emitter-output leaves with confirmed, separate collector-bank access.

No output function, differential pair, rail, row or route is invented. Only a
canonical common-emitter-merge relation with two distinct verified parent-bank
interfaces may become a two-device local owner.
"""
from collections import Counter
from copy import deepcopy
from math import isfinite
from statistics import mean

VERSION = 'emitter-merge-leaves-v1'
EPS = 1e-8


def compile_output_groups(comps, rows, input_model):
    by_id = {c['id']: c for c in comps}
    banks = input_model.get('banks', ())
    followers = {}
    for bank in banks:
        followers.setdefault(bank['follower'], []).append(bank)
    taken = {n for p in input_model.get('pairs', ()) for n in p['members']}
    groups, skipped = [], []
    for relation in input_model.get('emitter_merges', ()):
        names = relation['members']
        record = dict(id=relation['id'], members=list(names))
        if len(names) != 2 or set(names) & taken or any(len(followers.get(n, ())) != 1 for n in names):
            skipped.append(dict(record, reason='output_merge_bank_access_not_unique'))
            continue
        first, second = (by_id[n] for n in names)
        refs = [followers[n][0] for n in names]
        if (first['symbol'] not in ('npn','pnp') or first['symbol'] != second['symbol']
                or first['nodes']['E'] != second['nodes']['E']
                or first['nodes']['B'] == second['nodes']['B']
                or refs[0]['id'] == refs[1]['id']
                or any(by_id[n]['nodes'] != b['follower_contract'] or by_id[n]['nodes']['B'] != b['net']
                       for n,b in zip(names,refs))):
            skipped.append(dict(record, reason='output_merge_terminal_roles_not_corresponding'))
            continue
        if rows[names[0]] != rows[names[1]]:
            skipped.append(dict(record, reason='output_merge_rows_incompatible'))
            continue
        signal_nodes = {first['nodes']['E'],first['nodes']['B'],second['nodes']['B']}
        guests = sorted(c['id'] for c in comps
            if c['symbol'] in ('resistor','capacitor','inductor','diode','current-source','voltage-source')
            and signal_nodes.intersection(c['nodes'].values()))
        groups.append(dict(record, kind='common_emitter_merge', output_net=first['nodes']['E'],
            terminal_contract={n:deepcopy(by_id[n]['nodes']) for n in names},
            bank_refs={n:b['id'] for n,b in zip(names,refs)}, guest_members=guests,
            evidence=deepcopy(relation['evidence']), function_claim=None,
            row_policy='existing_same_row_only'))
    ownership = Counter(n for g in groups for n in g['members'])
    overlap = [g for g in groups if any(ownership[n] != 1 for n in g['members'])]
    skipped.extend(dict(id=g['id'],reason='ambiguous_output_leaf_ownership') for g in overlap)
    return dict(version=VERSION,groups=[g for g in groups if g not in overlap],skipped=skipped)


def measure_outputs(pl, comps, index, model, input_model):
    records, missing = [], []
    banks = {b['id']:b for b in input_model.get('banks', ())}
    for group in model.get('groups', ()):
        try:
            for name, contract in group['terminal_contract'].items():
                if comps[index[name]]['nodes'] != contract:
                    raise ValueError('output_terminal_contract_changed')
            positions = [pl.pos_of(index[n]) for n in group['members']]
            pins = [pl.pin_world(index[n],'E') for n in group['members']]
            if not all(isfinite(v) for p in positions+pins for v in p):
                raise ValueError('nonfinite_output_position')
            rows = [pl.cell_of[index[n]][0] for n in group['members']]
            inside = [c['id'] for c in comps if c['id'] not in group['members']
                and pl.cell_of[index[c['id']]][0] == rows[0]
                and min(p[0] for p in positions)+EPS < pl.pos_of(index[c['id']])[0] < max(p[0] for p in positions)-EPS]
            unrelated = [n for n in inside if n not in group['guest_members']]
            access = 0.0
            for name, identifier in group['bank_refs'].items():
                bank = banks[identifier]
                if comps[index[name]]['nodes'] != bank['follower_contract']:
                    raise ValueError('output_bank_contract_changed')
                anchor = tuple(mean(pl.pin_world(index[n],'C')[a] for n in bank['collector_members'].values()) for a in (0,1))
                control = pl.pin_world(index[name],'B')
                access += sum(abs(anchor[a]-control[a]) for a in (0,1))/100
            records.append(dict(id=group['id'],members=group['members'],
                row_errors=int(rows[0] != rows[1]),interleaving=len(unrelated),interlopers=unrelated,
                related_guests_inside=[n for n in inside if n not in unrelated],
                emitter_span=sum(abs(pins[0][a]-pins[1][a]) for a in (0,1))/100,
                bank_access_distance=access))
        except (KeyError,ValueError,IndexError,TypeError,AttributeError) as exc:
            missing.append(dict(id=group['id'],reason=str(exc)))
    usable = bool(records) and not missing
    fields = ('row_errors','interleaving','emitter_span','bank_access_distance')
    return dict(version=VERSION,count=len(model.get('groups',())),measurable=usable,records=records,
        missing=missing,skipped=deepcopy(model.get('skipped',())),
        **{k:sum(r[k] for r in records) if usable else None for k in fields})


def output_rank(measured):
    if not measured.get('count'):
        return (0,0,0,0)
    keys = ('row_errors','interleaving','emitter_span','bank_access_distance')
    if not measured.get('measurable') or any(type(measured.get(k)) not in (int,float)
            or not isfinite(measured[k]) or measured[k] < 0 for k in keys):
        return None
    return tuple(round(measured[k],8) for k in keys)


def arrange_outputs(trial, comps, index, model, input_model):
    """Use current bank centroids; keep old rows and the selected column slots."""
    banks = {b['id']:b for b in input_model.get('banks', ())}
    changed = []
    for group in model.get('groups', ()):
        members = list(group['members'])
        center = lambda n: mean(trial.pos_of(index[v])[0] for v in banks[group['bank_refs'][n]]['members'])
        members.sort(key=lambda n:(center(n),trial.pos_of(index[n])[0]))
        slots = sorted(trial.cell_of[index[n]][1] for n in members)
        cells = list(trial.cell_of)
        for name,col in zip(members,slots):
            i = index[name];cells[i] = cells[i][0],col
        trial.set_state(cells,trial.orient)
        for name in members:
            i = index[name];bank = banks[group['bank_refs'][name]]
            target = tuple(mean(trial.pin_world(index[n],'C')[a] for n in bank['collector_members'].values()) for a in (0,1))
            original = trial.orient[i];choices = []
            for orient in trial.allowed_orientations(i):
                trial.orient[i] = orient
                point = trial.pin_world(i,'B')
                direction = trial.pin_dir(i,'B')
                vector = {'east':(1,0),'west':(-1,0),'north':(0,-1),'south':(0,1)}[direction]
                backwards = sum((target[a]-point[a])*vector[a] for a in (0,1)) < -EPS
                choices.append((backwards,sum(abs(target[a]-point[a]) for a in (0,1)),orient != original,orient))
            trial.orient[i] = min(choices)[-1]
            if original != trial.orient[i]:
                changed.append(name)
    return changed
