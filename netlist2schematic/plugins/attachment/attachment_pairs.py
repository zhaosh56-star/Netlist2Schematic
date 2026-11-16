"""Finite joint slots for matching load banks on an already confirmed pair."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from statistics import mean
from time import perf_counter

from netlist2schematic.netlist.netlist_core import SRC_ALIASES, SNK_ALIASES

VERSION = 'paired-load-slots-v1'
REVERSE = {'north':'south', 'south':'north', 'east':'west', 'west':'east'}


def build_paired_banks(comps, structs, plan):
    by_id = {c['id']: c for c in comps}
    banks = {}
    for group in plan['groups']:
        if group['kind'] != 'parallel_bank' or len(group['members']) < 2 or len(group['owner_junctions']) != 1:
            continue
        symbols = Counter(by_id[n]['symbol'] for n in group['members'])
        if not set(symbols) <= {'resistor','capacitor','inductor'} or not ({'resistor','inductor'} & set(symbols)):
            continue
        links = [r for r in plan['host_access']['links'] if group['id'] in r['groups']]
        hosts = {tuple(h) for r in links for h in r['hosts']}
        if len(links) != len(group['members']) or len(hosts) != 1:
            continue
        host, pin = next(iter(hosts))
        if pin not in ('D','C'):
            continue
        owner = group['owner_junctions'][0]
        endpoints = set(by_id[group['members'][0]]['nodes'].values())
        rails = endpoints-{owner}
        if len(rails) != 1:
            continue
        rail = next(iter(rails))
        polarity = by_id[host]['symbol']
        if not ((polarity in ('nmos','npn') and rail in SRC_ALIASES)
                or (polarity in ('pmos','pnp') and rail in SNK_ALIASES)):
            continue
        banks.setdefault(host, []).append(dict(group=group['id'], host=host, pin=pin,
            owner=owner, rail=rail, symbols=dict(symbols)))
    relations, seen = [], set()
    for core in structs:
        names = set(core['devices'])
        if core['kind'] not in ('cross_coupled','differential_pair') or core.get('confidence') != 'high' or len(names) != 2:
            continue
        if any(len(banks.get(n, ())) != 1 for n in names):
            continue  # No arbitrary choice between multiple attachments.
        left, right = [banks[n][0] for n in sorted(names)]
        key = tuple(sorted(names))
        if (key in seen or left['rail'] != right['rail'] or left['symbols'] != right['symbols']
                or by_id[left['host']]['symbol'] != by_id[right['host']]['symbol']):
            continue
        relations.append(dict(id='paired-bank:'+'|'.join(key), core=core['id'],
                              banks=[left,right], evidence='confirmed_pair_and_matching_drain_loads'))
        seen.add(key)
    return relations


def paired_bank_trials(pl, comps, index, plan, relation, *, allow_axis_growth=False, deadline=None):
    """Keep rows and branch identity; make room for both banks in one trial."""
    from netlist2schematic.plugins.attachment.attachment_integration import _install, _insert_axis
    groups = {g['id']: g for g in plan['groups']}
    banks = sorted(relation['banks'], key=lambda b: pl.pos_of(index[b['host']])[0])
    hosts = [index[b['host']] for b in banks]
    if pl.cell_of[hosts[0]][0] != pl.cell_of[hosts[1]][0] or pl.pos_of(hosts[0])[0] == pl.pos_of(hosts[1])[0]:
        return
    facing = [pl.pin_dir(i, bank['pin']) for i, bank in zip(hosts,banks)]
    if facing[0] not in ('north','south') or facing[0] != facing[1]:
        return
    direction = facing[0]
    groups_ordered = [groups[b['group']] for b in banks]
    lanes = []
    for bank, group in zip(banks,groups_ordered):
        choices = [lane for lane in group.get('host_lanes', ()) if bank['host'] in lane]
        if len(choices) > 1:
            return
        lanes.append(set(choices[0]) if choices else {bank['host']})
    if lanes[0] & lanes[1]:
        return
    widths = [len(g['members']) for g in groups_ordered]
    total = sum(widths)+1  # One column remains between adjacent load banks.
    base = deepcopy(pl)
    extra = max(0,total-len(base.column_x))
    if extra and not allow_axis_growth:
        return
    if extra:
        _insert_axis(base,'column_x',len(base.column_x),extra,'left')
    center = mean(pl.cell_of[i][1] for i in hosts)
    starts = sorted(range(len(base.column_x)-total+1),
                    key=lambda c: (abs(c+(total-1)/2-center), c))[:4]
    for gap in (1,2):
        row = base.cell_of[hosts[0]][0]+(-gap if direction=='north' else gap)
        if not 0 <= row < len(base.row_y):
            continue
        for start in starts:
            if deadline is not None and perf_counter() >= deadline:
                return
            trial = deepcopy(base)
            cells, orientations = list(trial.cell_of), list(trial.orient)
            columns = [start+(widths[0]-1)//2, start+widths[0]+1+(widths[1]-1)//2]
            for bank,lane,col in zip(banks,lanes,columns):
                delta = col-trial.cell_of[index[bank['host']]][1]
                for name in lane:
                    i = index[name]
                    cells[i] = cells[i][0], cells[i][1]+delta
            trial.set_state(cells,orientations)
            valid = True
            for number,(bank,group) in enumerate(zip(banks,groups_ordered)):
                members = sorted(group['members'], key=lambda n: (comps[index[n]]['symbol'],n))
                if number:
                    members.reverse()  # Corresponding component types face each other.
                first = start if number==0 else start+widths[0]+1
                for offset,name in enumerate(members):
                    i = index[name]
                    cells[i] = row, first+offset
                    pin, = [p for p,n in comps[i]['nodes'].items() if n==bank['owner']]
                    options = []
                    for orientation in trial.allowed_orientations(i):
                        trial.orient[i] = orientation
                        if trial.pin_dir(i,pin) == REVERSE[direction]:
                            options.append(orientation)
                    if not options:
                        valid = False
                        break
                    orientations[i] = min(options, key=lambda o: (o != pl.orient[i],o))
                if not valid:
                    break
            if not valid:
                continue
            trial.set_state(cells,orientations)
            trial = _install(trial,comps,index,plan,required=pl.attachment_required_embeddings)
            if cells == pl.cell_of and orientations == pl.orient:
                continue
            yield trial,dict(kind='paired_load_slots', relation=relation['id'],
                groups=[b['group'] for b in banks], hosts=[b['host'] for b in banks],
                members=[n for g in groups_ordered for n in g['members']],
                direction=direction, row=row, gap=gap, columns_added=extra,
                branch_columns=columns, stage='initialization' if allow_axis_growth else 'refinement')
