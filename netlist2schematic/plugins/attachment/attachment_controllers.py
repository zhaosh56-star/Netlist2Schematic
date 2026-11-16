"""Bounded same-row moves of a controller and its proven sensing bank.

This is a port contract, not a functional CMFB classification. The controller
must drive only real G/B terminals, and one unambiguous input must be connected
to an exact passive bank whose other end reaches real D/C terminals. Shared
supplies, names and model similarity never establish this contract.
"""
from collections import defaultdict
from copy import deepcopy
from math import isfinite
from statistics import median
from time import perf_counter

VERSION = 'controller-sensing-cluster-v1'
ACTIVE = frozenset(('nmos', 'pmos', 'npn', 'pnp'))
PASSIVE = frozenset(('resistor', 'capacitor', 'inductor', 'diode'))
VECTORS = {'north': (0,-1), 'south': (0,1), 'west': (-1,0), 'east': (1,0)}
EPS = 1e-8


def compile_controllers(comps, plan):
    by_id = {c['id']: c for c in comps}
    incidence = defaultdict(list)
    for c in comps:
        for pin, net in c['nodes'].items():
            incidence[net].append((c['id'], pin))
    result = dict(version=VERSION, groups=[], skipped=[])
    for device in comps:
        if device['symbol'] != 'opamp':
            continue
        name, nodes = device['id'], device['nodes']
        if set(nodes) != {'IN+', 'IN-', 'OUT'} or len(set(nodes.values())) != 3:
            continue
        controls = [list(p) for p in incidence[nodes['OUT']] if p[0] != name]
        if not controls or any(by_id[n]['symbol'] not in ACTIVE or p not in ('G','B') for n,p in controls):
            continue
        matches = []
        for group in plan.get('groups', ()):
            members = group['members']
            if (group['kind'] != 'parallel_bank' or len(members) < 2
                    or len(group.get('owner_junctions', ())) != 2
                    or any(by_id[n]['symbol'] not in PASSIVE or len(by_id[n]['nodes']) != 2 for n in members)):
                continue
            pins = [p for p in ('IN+','IN-') if nodes[p] in group['owner_junctions']]
            if len(pins) != 1:
                continue
            pin = pins[0]; sense = nodes[pin]
            other = next(n for n in group['owner_junctions'] if n != sense)
            expected = {(name,pin)} | {(n,p) for n in members for p,v in by_id[n]['nodes'].items() if v == sense}
            if (set(incidence[sense]) != expected or any(set(by_id[n]['nodes'].values()) != {sense,other} for n in members)):
                continue
            hosts = [list(p) for p in incidence[other] if p[0] not in members]
            if not hosts or any(by_id[n]['symbol'] not in ACTIVE or p not in ('D','C') for n,p in hosts):
                continue
            matches.append(dict(id='controller_cluster:'+name+':'+group['id'], controller=name,
                input_pin=pin, sense_net=sense, signal_net=other, output_net=nodes['OUT'],
                members=list(members), passive_group=group['id'], hosts=hosts, controls=controls,
                evidence='exclusive_input_exact_parallel_bank_channel_node_and_control_only_output'))
        if len(matches) == 1:
            result['groups'].extend(matches)
        elif matches:
            result['skipped'].append(dict(controller=name, reason='ambiguous_sensing_banks'))
    counts = defaultdict(int)
    for group in result['groups']:
        for n in group['members']: counts[n] += 1
    shared = [g for g in result['groups'] if any(counts[n] > 1 for n in g['members'])]
    result['skipped'].extend(dict(id=g['id'],reason='shared_sensing_bank') for g in shared)
    result['groups'] = [g for g in result['groups'] if g not in shared]
    return result


def _point(pl, comps, index, name, pin, net):
    i = index[name]
    if comps[i]['nodes'].get(pin) != net:
        raise ValueError('terminal_contract_changed')
    point = tuple(pl.pin_world(i,pin))
    if len(point) != 2 or not all(isfinite(x) for x in point):
        raise ValueError('invalid_terminal_geometry')
    return point


def _center(points):
    return tuple(median(p[k] for p in points) for k in (0,1))


def _backward(origin, target, direction):
    vector = VECTORS[direction]
    return max(0.0, -sum((t-p)*d for p,t,d in zip(origin,target,vector)))


def measure_controllers(pl, comps, index, model):
    records, missing = [], []
    for group in model.get('groups', ()):
        try:
            name=group['controller']; i=index[name]
            if comps[i]['symbol'] != 'opamp': raise ValueError('controller_type_changed')
            sense = _point(pl,comps,index,name,group['input_pin'],group['sense_net'])
            output = _point(pl,comps,index,name,'OUT',group['output_net'])
            if any(comps[index[n]]['symbol'] not in ACTIVE or p not in ('D','C') for n,p in group['hosts']):
                raise ValueError('channel_host_changed')
            if any(comps[index[n]]['symbol'] not in ACTIVE or p not in ('G','B') for n,p in group['controls']):
                raise ValueError('control_host_changed')
            hosts = [_point(pl,comps,index,n,p,group['signal_net']) for n,p in group['hosts']]
            controls = [_point(pl,comps,index,n,p,group['output_net']) for n,p in group['controls']]
            if not hosts or not controls: raise ValueError('missing_host_or_control')
            errors, backward, distance = 0, 0.0, 0.0
            for n in group['members']:
                j=index[n]
                if comps[j]['symbol'] not in PASSIVE or len(comps[j]['nodes']) != 2:
                    raise ValueError('passive_contract_changed')
                for net,target in ((group['sense_net'],sense),(group['signal_net'],_center(hosts))):
                    pins=[p for p,v in comps[j]['nodes'].items() if v==net]
                    if len(pins)!=1: raise ValueError('passive_endpoint_changed')
                    pin=pins[0];point=_point(pl,comps,index,n,pin,net)
                    amount=_backward(point,target,pl.pin_dir(j,pin))
                    errors+=amount>EPS;backward+=amount
                    distance+=sum(abs(a-b) for a,b in zip(point,target))
                    if net==group['sense_net']:
                        amount=_backward(sense,point,pl.pin_dir(i,group['input_pin']))
                        errors+=amount>EPS;backward+=amount
            amount=_backward(output,_center(controls),pl.pin_dir(i,'OUT'))
            errors+=amount>EPS;backward+=amount
            span=max(p[0] for p in controls+[output])-min(p[0] for p in controls+[output])
            span+=max(p[1] for p in controls+[output])-min(p[1] for p in controls+[output])
            records.append(dict(id=group['id'], access_errors=errors, backward_distance=backward/100,
                port_distance=distance/100, output_span=span/100))
        except (KeyError,IndexError,ValueError,TypeError,AttributeError) as exc:
            missing.append(dict(id=group.get('id'),reason=str(exc)))
    measurable=bool(model.get('groups')) and not missing
    return dict(version=VERSION,count=len(model.get('groups',())),measurable=measurable,
        records=records,missing=missing,**{key:sum(r[key] for r in records) if measurable else None
            for key in ('access_errors','backward_distance','port_distance','output_span')})


def controller_rank(measured):
    if not measured.get('count'):return (0,0,0,0)
    keys=('access_errors','backward_distance','port_distance','output_span')
    if not measured.get('measurable') or any(type(measured.get(k)) not in (int,float)
            or not isfinite(measured[k]) or measured[k]<0 for k in keys):return None
    # One long backward detour must not beat several small port-side errors
    # merely because it has fewer offending terminals. Distances share the
    # existing 100px unit; this is a local estimate, never the routed Q score.
    return (round(sum(measured[k] for k in keys[1:]),8),
            measured['access_errors'],round(measured['output_span'],8),
            round(measured['port_distance'],8))


def controller_trials(pl,comps,index,group,*,deadline=None):
    """Existing empty columns only; unchanged rows, axes, hosts and allowed turns.

    The bank retains its transverse parallel shape. No controller row change,
    new orientation permission, rigid ownership or random proposal is added.
    """
    from netlist2schematic.plugins.attachment.attachment_integration import _fixed_ids
    i=index[group['controller']];ids=[index[n] for n in group['members']]
    moving=set(ids+[i]);row=pl.cell_of[ids[0]][0];hostrow=pl.cell_of[i][0]
    if moving & _fixed_ids(pl,index) or any(pl.cell_of[j][0]!=row for j in ids):return
    ordered=sorted(ids,key=lambda j:(pl.pos_of(j)[0],pl.pos_of(j)[1]))
    hosts=[pl.pin_world(index[n],p) for n,p in group['hosts']]
    controls=[pl.pin_world(index[n],p) for n,p in group['controls']]
    target=_center(controls);other=_center(hosts)
    available=[c for c in range(pl.cols) if pl.comp_at.get((hostrow,c)) in (None,i)]
    available.sort(key=lambda c:(abs(pl.column_x[c]-target[0]),pl.column_x[c]))
    seen=set()
    for col in available:
        base=deepcopy(pl);cells=list(base.cell_of);cells[i]=(hostrow,col);base.set_state(cells,base.orient)
        sense=base.pin_world(i,group['input_pin']);lo,hi=sorted((sense[0],other[0]))
        starts=[c for c in range(pl.cols-len(ids)+1) if all(lo+EPS<pl.column_x[k]<hi-EPS
            and pl.comp_at.get((row,k)) in (None,*ids) for k in range(c,c+len(ids)))]
        starts.sort(key=lambda c:(sum(abs(c+k-pl.cell_of[j][1]) for k,j in enumerate(ordered)),c))
        for start in starts:
            if deadline is not None and perf_counter()>=deadline:return
            trial=deepcopy(base);cells=list(trial.cell_of)
            for k,j in enumerate(ordered):cells[j]=(row,start+k)
            trial.set_state(cells,trial.orient)
            for j in ids:
                choices=[];old=trial.orient[j]
                for orientation in trial.allowed_orientations(j):
                    trial.orient[j]=orientation;backward=distance=0
                    if any(trial.pin_dir(j,p) not in ('north','south') for p in comps[j]['nodes']):continue
                    for pin,net in comps[j]['nodes'].items():
                        point=trial.pin_world(j,pin);end=sense if net==group['sense_net'] else other
                        backward+=_backward(point,end,trial.pin_dir(j,pin))
                        distance+=sum(abs(a-b) for a,b in zip(point,end))
                    choices.append(((backward,distance,orientation!=old,orientation),orientation))
                trial.orient[j]=min(choices)[1] if choices else old
            key=tuple(trial.cell_of),tuple(trial.orient)
            if key in seen or key==(tuple(pl.cell_of),tuple(pl.orient)):continue
            seen.add(key)
            yield trial,dict(kind='controller_sensing_cluster',version=VERSION,group=group['id'],
                controller=group['controller'],members=list(group['members']),
                controller_column=col,passive_first_column=start,rows_unchanged=True,
                axes_unchanged=True,allowed_orientations_unchanged=True)


def finish_controllers(par,pl,comps,nets,index,plan,options,budget,until,report,*,hpwl_limit):
    from netlist2schematic.plugins.attachment import attachment_integration as ai
    from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
    model=plan.get('controller_clusters',{})
    detail=dict(version=VERSION,count=len(model.get('groups',())),candidates=0,accepted=0,
        static_rejections=0,reason='not_applicable',actions=[])
    report['controller_finish']=detail
    for group in model.get('groups',()):
        if not ai._has_budget(budget,until):detail['reason']='shared_attachment_budget';break
        local=dict(model,groups=[group]);before=measure_controllers(pl,comps,index,local)
        rank=controller_rank(before);legacy=ai.attachment_rank(ai.measure_attachments(pl,comps,index,plan))
        if rank is None or legacy is None:detail['reason']='unknown_measurement';continue
        best=None;detail['reason']='no_legal_improvement'
        for trial,action in controller_trials(pl,comps,index,group,deadline=until):
            if detail['candidates']>=8 or not ai._has_budget(budget,until):
                detail['reason']='trial_or_shared_budget';break
            budget['candidates']+=1;report['candidates']+=1;detail['candidates']+=1
            if not trial.valid_state() or not ai._fixed_world_safe(pl,trial,index):continue
            measured=measure_controllers(trial,comps,index,local);key=controller_rank(measured)
            after=ai.attachment_rank(ai.measure_attachments(trial,comps,index,plan))
            if key is None or key>=rank or after is None or after[:4]>legacy[:4]:continue
            if _hpwl(par,trial,nets,index)>hpwl_limit+EPS:continue
            safe=ai._checked_static(par,trial,nets,index,options,until)
            if safe is None:budget['exhausted']=True;detail['reason']='shared_attachment_budget';break
            if not safe:detail['static_rejections']+=1;continue
            if best is None or key<best[0]:best=(key,trial,dict(action,before=before,after=measured))
        if best:
            _,pl,action=best;detail['actions'].append(action);detail['accepted']+=1
            report['actions'].append(action);report['accepted']+=1;detail['reason']='applied'
    return pl
