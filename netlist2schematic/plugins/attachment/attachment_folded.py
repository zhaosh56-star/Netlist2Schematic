"""Verified folded parents reference three small leaves, never one six-device owner.

The input fork is side access. Only the bias-D/folded-S continuation remains
an alignment target. No circuit title, instance name or GT coordinate is read.
"""
from collections import Counter,defaultdict
from copy import deepcopy
from itertools import product
from math import isfinite
from statistics import mean
from time import perf_counter

VERSION = 'folded-attachment-leaves-v2'
EPS = 1e-8


def compile_folded(devices, comps, structs, rows, owners):
    result = dict(version=VERSION,groups=[],skipped=[])
    if not any(s.get('kind') in ('folded_core','differential_pair') for s in structs):
        return result
    from netlist2schematic.plugins.layout_shared.layout_evidence import build_layout_evidence
    evidence=build_layout_evidence(devices,structs)
    from netlist2schematic.plugins.attachment.attachment_folded_evidence import detect_folded_RL
    detected=detect_folded_RL(devices,comps,evidence)
    result['rail_supply_detection']=deepcopy(detected)
    parts={c['id']:c for c in comps}
    pairs={frozenset(s['devices']):s for s in evidence['structures'] if s['kind']=='differential_pair'}
    known={}
    for s in list(evidence['structures'])+detected['records']:
        if s['kind']!='folded_core' or s['confidence']!='high':
            continue
        roles=s.get('roles',{})
        names=[roles.get(prefix+'_'+side) for prefix in ('input','folded','bias') for side in ('left','right')]
        reason=None
        if len(set(names))!=6 or any(n not in devices or devices[n].get('cls')!='MOS' for n in names):
            reason='incomplete_folded_members'
        elif frozenset(names[:2]) not in pairs:
            reason='unverified_input_child'
        else:
            inp,fold,bias=[names[n:n+2] for n in (0,2,4)]
            typ=devices[inp[0]]['type'];opposite='PMOS' if typ=='NMOS' else 'NMOS'
            power=lambda net:evidence['net_roles'].get(net,{}).get('role')=='power'
            valid=(devices[inp[1]]['type']==typ and all(devices[n]['type']==opposite for n in fold+bias)
                and devices[fold[0]]['g']==devices[fold[1]]['g']
                and devices[bias[0]]['g']==devices[bias[1]]['g'])
            valid &= all(devices[i]['d']==devices[f]['s']==devices[b]['d']
                and (power(devices[b]['s']) or b in detected['validated_supplies']) and not power(devices[i]['d'])
                for i,f,b in zip(inp,fold,bias))
            if not valid:
                reason='folded_terminal_roles_not_proven'
            elif any(len({rows[n] for n in leaf})!=1 for leaf in (inp,fold,bias)):
                reason='folded_leaf_rows_incompatible'
            elif any(set(o)&set(names) and not set(o)<=set(names) for o in owners):
                reason='existing_owner_extends_beyond_core'
            elif any(r['kind'] in ('latch','cross_coupled') and set(r['devices'])&set(names)
                     for r in evidence['structures'] if r['confidence']=='high'):
                reason='incompatible_overlapping_core'
        if reason:
            result['skipped'].append(dict(id=s['id'],reason=reason));continue
        branch_key=frozenset(zip(inp,fold,bias));key=frozenset(names)
        if key in known:
            if known[key]!=branch_key:
                result['skipped'].append(dict(id=s['id'],reason='ambiguous_folded_branch_roles'))
                result['groups']=[g for g in result['groups'] if set(g['members'])!=set(names)]
            continue
        known[key]=branch_key
        pair=pairs[frozenset(inp)]
        signal={net for n in inp for net in parts[n]['nodes'].values() if not power(net)}
        guests=sorted(c['id'] for c in comps if c['symbol'] in ('resistor','capacitor','inductor','diode',
            'current-source','voltage-source') and signal.intersection(c['nodes'].values()))
        result['groups'].append(dict(id='folded_leaves:'+'|'.join(sorted(names)),
            members=sorted(names),inputs=inp,folded=fold,bias=bias,leaves=[inp,fold,bias],
            guest_members=guests,source_structure=s['id'],input_child=pair['id'],
            roles=deepcopy(roles),evidence=dict(kind='validated_input_child_and_two_supply_fed_fold_junctions',
                input_pair=deepcopy(pair.get('validation_evidence',{})),
                branch_ports=[dict(input=i,folded=f,bias=b,junction=devices[i]['d']) for i,f,b in zip(inp,fold,bias)]),
            terminal_contract={n:deepcopy(parts[n]['nodes']) for n in names}))
        paths={b:deepcopy(detected['validated_supplies'][b]) for b in bias
               if b in detected['validated_supplies']}
        if any(p['members'] for p in paths.values()):
            group=result['groups'][-1]
            supply_members=sorted({n for p in paths.values() for n in p['members']})
            group.update(rail_supply_derived=True,supply_paths=paths,supply_members=supply_members,
                supply_symbols={n:parts[n]['symbol'] for n in supply_members})
            group['evidence']['kind']='validated_input_and_exclusive_RL_rail_fold_junctions'
            group['evidence']['supply_paths']=deepcopy(paths)
            group['terminal_contract'].update({n:deepcopy(parts[n]['nodes']) for n in supply_members})
    claims=Counter(n for g in result['groups'] for n in g['members'])
    overlap=[g for g in result['groups'] if any(claims[n]>1 for n in g['members'])]
    result['skipped'].extend(dict(id=g['id'],reason='ambiguous_folded_leaf_ownership') for g in overlap)
    result['groups']=[g for g in result['groups'] if g not in overlap]
    return result


def owner_lists(owners, model):
    taken={n for g in model.get('groups',()) for n in g['members']}
    return [o for o in owners if not set(o)&taken]+[list(leaf) for g in model.get('groups',()) for leaf in g['leaves']]


def _control_target(pl, comps, index, name, other):
    net=comps[index[name]]['nodes']['G']
    points=[pl.pin_world(index[c['id']],pin) for c in comps if c['id']!=name
            for pin,n in c['nodes'].items() if n==net]
    if points:
        return tuple(mean(p[a] for p in points) for a in (0,1))
    point=pl.pos_of(index[name]);op=pl.pos_of(index[other])
    return point[0]+(-100 if point[0]<op[0] else 100),point[1]


def _orient_towards(pl, i, pin, target):
    old=pl.orient[i];choices=[]
    for orientation in pl.allowed_orientations(i):
        pl.orient[i]=orientation;point=pl.pin_world(i,pin)
        vec={'east':(1,0),'west':(-1,0),'north':(0,-1),'south':(0,1)}[pl.pin_dir(i,pin)]
        backwards=sum((target[a]-point[a])*vec[a] for a in (0,1)) < -EPS
        choices.append((backwards,sum(abs(point[a]-target[a]) for a in (0,1)),orientation!=old,orientation))
    pl.orient[i]=min(choices)[-1]


def measure_folded(pl, comps, index, model):
    records=[];missing=[]
    for g in model.get('groups',()):
        try:
            if any(comps[index[n]]['nodes']!=contract for n,contract in g['terminal_contract'].items()):
                raise ValueError('folded_terminal_contract_changed')
            if any(comps[index[n]]['symbol']!=symbol for n,symbol in g.get('supply_symbols',{}).items()):
                raise ValueError('folded_RL_supply_symbol_changed')
            points=[pl.pos_of(index[n]) for n in g['members']]
            if not all(isfinite(v) for p in points for v in p):
                raise ValueError('nonfinite_folded_position')
            row=pl.cell_of[index[g['inputs'][0]]][0]
            xs=[pl.pos_of(index[n])[0] for n in g['inputs']]
            interlopers=[c['id'] for c in comps if c['id'] not in g['inputs']+g['guest_members']
                and pl.cell_of[index[c['id']]][0]==row and min(xs)+EPS<pl.pos_of(index[c['id']])[0]<max(xs)-EPS]
            errors=sum(len({pl.cell_of[index[n]][0] for n in leaf})!=1 for leaf in g['leaves'])
            fold_dx=pl.pos_of(index[g['folded'][0]])[0]-pl.pos_of(index[g['folded'][1]])[0]
            bias_dx=pl.pos_of(index[g['bias'][0]])[0]-pl.pos_of(index[g['bias'][1]])[0]
            pin_error=sum(abs(pl.pin_world(index[b],'D')[0]-pl.pin_world(index[f],'S')[0])
                          for b,f in zip(g['bias'],g['folded']))/100
            access=0
            for name,other in (g['inputs'],list(reversed(g['inputs']))):
                target=_control_target(pl,comps,index,name,other);p=pl.pin_world(index[name],'G')
                vec={'east':1,'west':-1}.get(pl.pin_dir(index[name],'G'),0)
                access+=bool((target[0]-p[0])*vec < -EPS)
            records.append(dict(id=g['id'],members=g['members'],row_errors=errors,
                input_interleaving=len(interlopers),interlopers=interlopers,
                branch_order_errors=int(fold_dx*bias_dx<0),input_access_errors=access,
                input_span=(max(xs)-min(xs))/100,main_pin_error=pin_error,
                region_span=(max(p[0] for p in points)-min(p[0] for p in points))/100))
        except (KeyError,ValueError,TypeError,IndexError,AttributeError) as exc:
            missing.append(dict(id=g['id'],reason=str(exc)))
    usable=bool(records) and not missing
    keys=('row_errors','input_interleaving','branch_order_errors','input_access_errors','input_span','main_pin_error','region_span')
    return dict(version=VERSION,count=len(model.get('groups',())),measurable=usable,records=records,missing=missing,
        skipped=deepcopy(model.get('skipped',())),**{k:sum(r[k] for r in records) if usable else None for k in keys})


def folded_rank(measured):
    if not measured.get('count'):
        return (0,)*7
    keys=('row_errors','input_interleaving','branch_order_errors','input_access_errors','input_span','main_pin_error','region_span')
    if not measured.get('measurable') or any(type(measured.get(k)) not in (int,float)
            or not isfinite(measured[k]) or measured[k]<0 for k in keys):
        return None
    return tuple(round(measured[k],8) for k in keys)


def _align_bias(trial, index, group):
    members=group['bias'];ids={index[n] for n in members}
    cells=list(trial.cell_of);row=cells[next(iter(ids))][0];choices=[]
    for b,f in zip(members,group['folded']):
        j=index[b];target=trial.pin_world(index[f],'S')[0];point=trial.pin_world(j,'D')[0]
        cols=[c for c in range(trial.cols) if trial.comp_at.get((row,c)) in (None,*ids)]
        choices.append(sorted(cols,key=lambda c:(abs(point+trial.column_x[c]-trial.column_x[cells[j][1]]-target),c))[:4])
    best=None
    fold_sign=trial.cell_of[index[group['folded'][0]]][1]-trial.cell_of[index[group['folded'][1]]][1]
    for cols in product(*choices):
        if len(set(cols))!=2 or (cols[0]-cols[1])*fold_sign<0:
            continue
        candidate=list(cells)
        for n,col in zip(members,cols):candidate[index[n]]=(row,col)
        trial.set_state(candidate,trial.orient)
        if not trial.blocks_contiguous():continue
        error=sum(abs(trial.pin_world(index[b],'D')[0]-trial.pin_world(index[f],'S')[0])
                  for b,f in zip(members,group['folded']))
        key=error,sum(abs(cells[index[n]][1]-col) for n,col in zip(members,cols)),cols
        if best is None or key<best[0]:best=key,candidate
    trial.set_state(best[1] if best else cells,trial.orient)


def folded_trials(pl, comps, index, model, group, *, deadline=None):
    from netlist2schematic.plugins.layout_shared.layout_relations import prepare_structure_placement
    from netlist2schematic.plugins.attachment.attachment_integration import _fixed_ids
    core=set(group['members'])
    if core & {comps[j]['id'] for j in _fixed_ids(pl,index)}:
        return
    old_blocks=[[comps[j]['id'] for j in b] for b in pl.blocks]
    if any(set(b)&core and not set(b)<=core for b in old_blocks):
        return
    relations=[dict(id='folded_retained:'+'|'.join(b),kind='retained_owner',members=b,
        owned_members=b,rigid=True,macro=False,layout={}) for b in old_blocks if not set(b)&core]
    for k,leaf in enumerate(group['leaves']):
        relations.append(dict(id=group['id']+':'+str(k),kind='folded_leaf',members=leaf,
            owned_members=leaf,rigid=True,macro=False,layout={}))
    relations.append(dict(id=group['id'],kind='folded_parent',members=group['members'],
        owned_members=[],rigid=False,macro=True,layout=dict(orders=[group['folded'],group['bias']])))
    base=deepcopy(pl);previous_orders=deepcopy(getattr(pl,'role_orders',()))
    previous_macros=deepcopy(getattr(pl,'layout_macro_blocks',()))
    previous_motion=deepcopy(getattr(pl,'motion_blocks',()))
    previous_scoring=deepcopy(getattr(pl,'layout_relations',()))
    prepare_structure_placement(base,comps,index,relations)
    # Use the shared helper's ownership graph, then restore coordinates.
    # This seed only rearranges core members and uses genuinely vacant slots;
    # unrelated devices never get packed out of the way implicitly.
    base.set_state(pl.cell_of,pl.orient)
    obsolete={index[n] for n in group['inputs']+group['folded']}
    retained=[[order for order in orders if not(len(order)==4 and set(order)==obsolete)] for orders in previous_orders]
    base.role_orders=[orders for orders in retained if orders]+base.role_orders
    base.layout_relations=previous_scoring
    base.layout_macro_blocks=list(previous_macros)+[b for b in base.layout_macro_blocks if b not in previous_macros]
    base.motion_blocks=base.blocks+[b for b in base.layout_macro_blocks if b not in base.blocks]
    base.motion_blocks.extend(b for b in previous_motion if b not in base.motion_blocks and not set(b)&{index[n] for n in core})
    seen=set()
    for input_first,input_flip,branch_flip in product((True,False),repeat=3):
        if deadline is not None and perf_counter()>=deadline:return
        trial=deepcopy(base);cells=list(trial.cell_of)
        ordered=[list(group['inputs']),list(group['folded']),list(group['bias'])]
        if input_flip:ordered[0].reverse()
        if branch_flip:ordered[1].reverse();ordered[2].reverse()
        for row in sorted({cells[index[n]][0] for n in core}):
            leaves=[leaf for leaf in ordered if cells[index[leaf[0]]][0]==row]
            if not input_first and group['inputs'][0] in {n for leaf in leaves for n in leaf}:
                leaves=leaves[1:]+leaves[:1]
            slots=sorted(cells[index[n]][1] for n in core if cells[index[n]][0]==row)
            for name,col in zip([n for leaf in leaves for n in leaf],slots):cells[index[name]]=(row,col)
        trial.set_state(cells,trial.orient)
        for leaf in (group['folded'],group['bias']):
            target=(mean(trial.pos_of(index[n])[0] for n in leaf),trial.pos_of(index[leaf[0]])[1])
            for name in leaf:_orient_towards(trial,index[name],'G',target)
        _align_bias(trial,index,group)
        for name,other in (group['inputs'],list(reversed(group['inputs']))):
            _orient_towards(trial,index[name],'G',_control_target(trial,comps,index,name,other))
        key=tuple(trial.cell_of),tuple(trial.orient)
        if key in seen:continue
        seen.add(key)
        yield trial,dict(kind='folded_parent_children_seed',version=VERSION,core=group['id'],
            source_structure=group['source_structure'],leaves=deepcopy(group['leaves']),
            rows_unchanged=True,axes_unchanged=True,
            corrected_outboard_order=True,input_first=input_first,
            repacked_devices=[c['id'] for j,c in enumerate(comps) if trial.cell_of[j]!=pl.cell_of[j]],
            pose_changed=[c['id'] for j,c in enumerate(comps) if trial.orient[j]!=pl.orient[j]])


def filter_input_alignment(alignment, index, model, applied):
    forbidden={frozenset((index[g['inputs'][k]],index[g[prefix][k]]))
        for g in model.get('groups',()) if g['id'] in applied for k in (0,1) for prefix in ('folded','bias')}
    return ([edge for edge in alignment if frozenset(edge[:2]) not in forbidden],
            [edge for edge in alignment if frozenset(edge[:2]) in forbidden])


def filter_RL_alignment(alignment,index,model):
    """New proven continuations use true-pin host axes, not duplicate cell cost."""
    replaced={frozenset((index[b],index[f])) for g in model.get('groups',())
              if g.get('rail_supply_derived') for b,f in zip(g['bias'],g['folded'])}
    return ([edge for edge in alignment if frozenset(edge[:2]) not in replaced],
            [edge for edge in alignment if frozenset(edge[:2]) in replaced])
