"""Shared control-node accessories, without claiming a functional pair.

Only passive guests and separately anchored sources move. Existing active
positions, orientations, axes and net incidence remain unchanged.
"""
from copy import deepcopy
from math import isfinite
from statistics import median
from time import perf_counter

VERSION = 'shared-control-access-v1'
ACTIVE = frozenset(('nmos','pmos','npn','pnp'))
EPS = 1e-8


def compile_nodes(comps, plan):
    by_id={c['id']:c for c in comps};junctions=plan['readability_plan']['junctions']
    nodes=[];skipped=[]
    for net,junction in sorted(junctions.items()):
        hosts=junction.get('control_ports',())
        if (junction['is_rail'] or len(hosts)<2 or junction['conduction_ports']
                or junction['source_ports']):continue
        symbols={by_id[n]['symbol'] for n,_ in hosts}
        supplies={by_id[n]['nodes'].get('S',by_id[n]['nodes'].get('E')) for n,_ in hosts}
        if (len(symbols)!=1 or not symbols<=ACTIVE or len(supplies)!=1
                or not junctions.get(next(iter(supplies)),{}).get('is_rail')):continue
        shunts=[];returns=[]
        for group in plan['groups']:
            ends=group.get('endpoint_junctions',())
            if len(ends)!=2 or net not in ends:continue
            other=next((n for n in ends if n!=net),None)
            if other is None:continue
            if (junctions.get(other,{}).get('is_rail')
                    and group['kind'] in ('single_attachment','parallel_bank')):
                for name in group['members']:
                    pins=by_id[name]['nodes']
                    if len(pins)==2 and set(pins.values())=={net,other}:
                        shunts.append(dict(member=name,rail=other,group=group['id']))
            elif (group['kind'] in ('single_attachment','series_chain')
                    and junctions.get(other,{}).get('conduction_ports')):
                returns.append(dict(group=group['id'],members=list(group['members']),path=list(group['path']),other=other))
        if not shunts:continue
        members=[s['member'] for s in shunts]+[n for r in returns for n in r['members']]
        if len(set(members))!=len(members):
            skipped.append(dict(net=net,reason='shared_passive_members'));continue
        nodes.append(dict(net=net,hosts=sorted(map(list,hosts)),shunts=shunts,returns=returns,
            members=members,evidence='actual_shared_controls_common_rail_and_terminal_mapped_accessories'))
    return dict(version=VERSION,nodes=nodes,skipped=skipped)


def _point(pl,comps,index,ports,net):
    points=[]
    for name,pin in ports:
        i=index[name]
        if comps[i]['nodes'].get(pin)!=net:raise ValueError('terminal_changed')
        p=tuple(pl.pin_world(i,pin))
        if not all(map(isfinite,p)):raise ValueError('nonfinite_terminal')
        points.append(p)
    if not points:raise ValueError('missing_junction_ports')
    return tuple(median(p[k] for p in points) for k in (0,1))


def _pin(comp,net):
    pins=[p for p,n in comp['nodes'].items() if n==net]
    if len(pins)!=1:raise ValueError('ambiguous_passive_terminal')
    return pins[0]


def _rail_direction(net):
    from netlist2schematic.netlist.netlist_core import SRC_ALIASES,SNK_ALIASES
    name=str(net).casefold()
    if name in SRC_ALIASES:return 'north'
    if name in SNK_ALIASES:return 'south'
    raise ValueError('unclassified_rail')


def measure_nodes(pl,comps,index,model,junctions):
    records=[];missing=[]
    for node in model.get('nodes',()):
        try:
            hub=_point(pl,comps,index,node['hosts'],node['net'])
            errors=0;distance=0.;details=[]
            for shunt in node['shunts']:
                i=index[shunt['member']];direction=_rail_direction(shunt['rail'])
                rail=_pin(comps[i],shunt['rail']);signal=_pin(comps[i],node['net'])
                p=pl.pin_world(i,signal);q=pl.pin_world(i,rail)
                sign=-1 if direction=='north' else 1
                bad=int(pl.pin_dir(i,rail)!=direction)+int(sign*(q[1]-hub[1])<=EPS)
                errors+=bad;distance+=abs(p[0]-hub[0])+abs(p[1]-hub[1])
                details.append(dict(member=shunt['member'],kind='rail_shunt',point=p,rail_direction=direction,errors=bad))
            for path in node['returns']:
                names=list(path['members'])
                if path['path'][0]!=node['net']:names.reverse()
                other=_point(pl,comps,index,junctions[path['other']]['conduction_ports'],path['other'])
                first,last=index[names[0]],index[names[-1]]
                a=pl.pin_world(first,_pin(comps[first],node['net']))
                b=pl.pin_world(last,_pin(comps[last],path['other']))
                distance+=sum(abs(a[k]-hub[k])+abs(b[k]-other[k]) for k in (0,1))
                details.append(dict(members=names,kind='control_connection',entry=a,exit=b,other=other))
            records.append(dict(net=node['net'],hub=hub,shunt_direction_errors=errors,
                endpoint_distance=distance/100,details=details))
        except (KeyError,ValueError,IndexError,TypeError,AttributeError) as exc:
            missing.append(dict(net=node['net'],reason=str(exc)))
    usable=bool(records) and not missing
    return dict(version=VERSION,count=len(model.get('nodes',())),measurable=usable,
        shunt_direction_errors=sum(r['shunt_direction_errors'] for r in records) if usable else None,
        endpoint_distance=sum(r['endpoint_distance'] for r in records) if usable else None,
        records=records,missing=missing,skipped=deepcopy(model.get('skipped',())))


def node_rank(measurement):
    if not measurement.get('count'):return (0,0)
    if not measurement.get('measurable'):return None
    return measurement['shunt_direction_errors'],round(measurement['endpoint_distance'],8)


def _orient(trial,i,first,last,start,end):
    choices=[];old=trial.orient[i]
    for orient in trial.allowed_orientations(i):
        trial.orient[i]=orient;a=trial.pin_world(i,first);b=trial.pin_world(i,last)
        distance=sum(abs(a[k]-start[k])+abs(b[k]-end[k]) for k in (0,1))
        choices.append((distance,orient!=old,orient))
    trial.orient[i]=min(choices)[2]


def node_trials(pl,comps,index,plan,node,deadline=None):
    from netlist2schematic.plugins.attachment.attachment_integration import _fixed_ids
    ids={index[n] for n in node['members']}
    if ids&_fixed_ids(pl,index):return
    try:hub=_point(pl,comps,index,node['hosts'],node['net'])
    except (KeyError,ValueError,TypeError,AttributeError):return
    columns=sorted(range(len(pl.column_x)),key=lambda c:(abs(pl.column_x[c]-hub[0]),c))[:3]
    junctions=plan['readability_plan']['junctions']
    for col in columns:
        if deadline is not None and perf_counter()>=deadline:return
        trial=deepcopy(pl);cells=list(trial.cell_of);used=set();valid=True
        for shunt in node['shunts']:
            i=index[shunt['member']];direction=_rail_direction(shunt['rail']);sign=-1 if direction=='north' else 1
            candidates=sorted((r for r,y in enumerate(trial.row_y) if sign*(y-hub[1])>EPS),
                              key=lambda r:(abs(trial.row_y[r]-hub[1]),r))
            row=next((r for r in candidates if (r,col) not in used and trial.comp_at.get((r,col)) in (None,*ids)),None)
            if row is None:valid=False;break
            cells[i]=row,col;used.add((row,col));trial.set_state(cells,trial.orient)
            signal=_pin(comps[i],node['net']);rail=_pin(comps[i],shunt['rail'])
            _orient(trial,i,signal,rail,hub,(trial.column_x[col],trial.row_y[row]+sign*100))
        if not valid:continue
        for path in node['returns']:
            names=list(path['members']);nodes=list(path['path'])
            if nodes[0]!=node['net']:names.reverse();nodes.reverse()
            target=_point(trial,comps,index,junctions[path['other']]['conduction_ports'],path['other'])
            available=[r for r,y in enumerate(trial.row_y) if min(hub[1],target[1])+EPS<y<max(hub[1],target[1])-EPS
                       and (r,col) not in used and trial.comp_at.get((r,col)) in (None,*ids)]
            available.sort(key=lambda r:trial.row_y[r],reverse=target[1]<hub[1])
            if len(available)<len(names):valid=False;break
            for k,name in enumerate(names):
                row=available[round((k+1)*(len(available)+1)/(len(names)+1))-1]
                i=index[name];cells[i]=row,col;used.add((row,col));trial.set_state(cells,trial.orient)
                _orient(trial,i,_pin(comps[i],nodes[k]),_pin(comps[i],nodes[k+1]),hub,target)
        if not valid:continue
        yield trial,dict(kind='shared_control_access',net=node['net'],members=node['members'],
            hosts=node['hosts'],column=col,active_world_unchanged=True,axes_unchanged=True)


def related_source_trials(pl,comps,index,plan,node,deadline=None):
    """Independent source guests; never turn a multi-host node into a tail."""
    from netlist2schematic.plugins.attachment.attachment_integration import _fixed_ids
    endpoints={r['other'] for r in node['returns']}
    for group in plan.get('source_groups',()):
        i=index[group['device']]
        if (group['kind']!='neutral_junction_source' or len(group['members'])!=1
                or group['attachment_net'] not in endpoints or i in _fixed_ids(pl,index)):continue
        direction=_rail_direction(group['rail_net']);sign=1 if direction=='south' else -1
        ports=group['host_ports'];points=[pl.pin_world(index[n],p) for n,p in ports if p in ('D','C','S','E')]
        if not points:continue
        edge=(max if sign==1 else min)(p[1] for p in points)
        x=median(p[0] for p in points if abs(p[1]-edge)<EPS)
        candidates=[]
        for r,y in enumerate(pl.row_y):
            for c,cx in enumerate(pl.column_x):
                if pl.comp_at.get((r,c)) not in (None,i):continue
                for orient in pl.allowed_orientations(i):
                    from netlist2schematic.layout.structured_placement import par
                    geo=par.GEO_DOC[comps[i]['symbol']]['orientations'][orient]
                    rail=next(p for p in geo['pins'] if p['name']==group['rail_pin'])
                    signal=next(p for p in geo['pins'] if p['name']==group['signal_pin'])
                    point=(cx+signal['at']['x'],y+signal['at']['y'])
                    if rail['direction']!=direction or sign*(point[1]-edge)<-EPS:continue
                    candidates.append((abs(point[0]-x)+abs(point[1]-edge),r,c,orient))
        for _,r,c,orient in sorted(candidates)[:8]:
            if deadline is not None and perf_counter()>=deadline:return
            trial=deepcopy(pl);cells=list(trial.cell_of);cells[i]=r,c
            orientations=list(trial.orient);orientations[i]=orient;trial.set_state(cells,orientations)
            yield trial,dict(kind='control_connection_source_repair',source=group['device'],
                attachment_net=group['attachment_net'],rail=group['rail_net'],rail_direction=direction,
                functional_role='neutral_junction_source',active_world_unchanged=True,axes_unchanged=True)
