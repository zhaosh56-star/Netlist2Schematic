"""Mid-band guest slots on proven two-terminal conduction junctions.

No new functional labels, current levels, annealing terms or routing rules.
Inserting a display row leaves every existing physical row exactly in place.
"""
from __future__ import annotations

from bisect import bisect_left
from copy import deepcopy
from math import isfinite
from time import perf_counter

VERSION='conduction-tap-slots-v1'
EPS=1e-8
VECTORS={'north':-1,'south':1}


def compile_taps(comps,plan):
    """Reuse unique D/S, C/E or complementary D/D, C/C host-axis evidence."""
    by_id={c['id']:c for c in comps};junctions=plan.get('readability_plan',{}).get('junctions',{});axes={}
    for axis in plan.get('host_axes',()):axes.setdefault(axis['net'],[]).append(axis)
    groups=[]
    for group in plan.get('groups',()):
        ends=group.get('owner_junctions',())
        if (group.get('kind') not in ('series_chain','two_arm_join') or len(ends)!=2
                or group.get('row_mode')=='conduction_bridge'):continue
        records=[]
        for net in ends:
            candidates=axes.get(net,())
            if len(candidates)!=1 or junctions.get(net,{}).get('is_rail'):break
            axis=candidates[0];hosts={(axis['a'],axis['pin_a']),(axis['b'],axis['pin_b'])}
            if hosts!=set(map(tuple,junctions.get(net,{}).get('conduction_ports',()))) or len(hosts)!=2:break
            guests=[(n,pin) for n in group['members'] for pin,value in by_id[n]['nodes'].items() if value==net]
            if len(guests)!=1:break
            records.append(dict(net=net,hosts=sorted(hosts),guest=guests[0]))
        if len(records)==2:
            groups.append(dict(group=group['id'],members=list(group['members']),ends=records,
                evidence='two_unique_conduction_junctions_and_passive_path'))
    return dict(version=VERSION,groups=groups)


def _bands(pl,comps,index,relation):
    records=[]
    for endpoint in relation['ends']:
        points={}
        for name,pin in endpoint['hosts']:
            i=index[name]
            if comps[i]['nodes'].get(pin)!=endpoint['net']:raise ValueError('host_terminal_changed')
            p=tuple(pl.pin_world(i,pin));direction=pl.pin_dir(i,pin)
            if direction not in VECTORS or direction in points or not all(isfinite(x) for x in p):
                raise ValueError('nonunique_vertical_access')
            points[direction]=p
        if set(points)!=set(VECTORS) or points['south'][1]>=points['north'][1]-EPS:
            raise ValueError('conduction_ports_do_not_face')
        name,pin=endpoint['guest'];i=index[name]
        if comps[i]['nodes'].get(pin)!=endpoint['net']:raise ValueError('guest_terminal_changed')
        p=tuple(pl.pin_world(i,pin))
        if not all(isfinite(x) for x in p):raise ValueError('invalid_guest_point')
        lo,hi=points['south'][1],points['north'][1]
        records.append(dict(net=endpoint['net'],guest=list(endpoint['guest']),band=[lo,hi],
                            point=p,outside=max(lo-p[1],0,p[1]-hi)))
    lo,hi=max(r['band'][0] for r in records),min(r['band'][1] for r in records)
    if lo>=hi-EPS:raise ValueError('no_common_access_band')
    return lo,hi,records


def measure_taps(pl,comps,index,model):
    records=[];missing=[]
    for relation in model.get('groups',()):
        try:
            lo,hi,ends=_bands(pl,comps,index,relation)
            records.append(dict(group=relation['group'],members=relation['members'],band=[lo,hi],
                endpoints=ends,errors=sum(r['outside']>EPS for r in ends),
                outside_distance=sum(r['outside'] for r in ends)/100))
        except (KeyError,ValueError,TypeError,IndexError,AttributeError) as exc:
            missing.append(dict(group=relation['group'],reason=str(exc)))
    valid=bool(records) and not missing
    return dict(version=VERSION,count=len(model.get('groups',())),measurable=valid,
        errors=sum(r['errors'] for r in records) if valid else None,
        outside_distance=sum(r['outside_distance'] for r in records) if valid else None,
        records=records,missing=missing)


def _split_row(trial,y):
    """Insert an empty row index, without translating a pre-existing device."""
    cut=bisect_left(trial.row_y,y)
    if cut<len(trial.row_y) and abs(trial.row_y[cut]-y)<EPS:return cut,False
    if cut==0 or cut==len(trial.row_y):return None,False
    trial.row_y=list(trial.row_y);trial.row_y.insert(cut,y)
    cells=[(r+(r>=cut),c) for r,c in trial.cell_of]
    trial.fixed_cells={i:(r+(r>=cut),c) for i,(r,c) in getattr(trial,'fixed_cells',{}).items()}
    trial.attachment_junction_rows={g:tuple(r+(r>=cut) for r in rows)
        for g,rows in trial.attachment_junction_rows.items()}
    origins=trial.attachment_row_origin
    trial.attachment_row_origin=origins[:cut]+(None,)+origins[cut:]
    trial.rows=len(trial.row_y);trial.set_state(cells,trial.orient)
    return cut,True


def tap_trials(pl,comps,index,plan,relation,*,allow_row_split,grid_step=2,deadline=None):
    """Keep existing x slots; all guests move, every non-guest stays in place."""
    from netlist2schematic.plugins.attachment.attachment_integration import _fixed_ids,_install,_group_paths,_orient_path
    ids={index[n] for n in relation['members']}
    if ids&_fixed_ids(pl,index):return
    try:lo,hi,_=_bands(pl,comps,index,relation)
    except (KeyError,ValueError,TypeError,IndexError,AttributeError):return
    target=round((lo+hi)/2/grid_step)*grid_step
    existing=sorted((y for y in pl.row_y if lo+EPS<y<hi-EPS),key=lambda y:(abs(y-target),y))
    choices=existing[:2]
    if allow_row_split and lo+EPS<target<hi-EPS and not any(abs(y-target)<EPS for y in choices):
        choices.insert(0,target)
    group=next(g for g in plan['groups'] if g['id']==relation['group'])
    for y in choices[:3]:
        if deadline is not None and perf_counter()>=deadline:return
        trial=deepcopy(pl);row,inserted=_split_row(trial,y)
        if row is None:continue
        if any(trial.comp_at.get((row,trial.cell_of[i][1])) not in (None,*ids) for i in ids):continue
        cells=list(trial.cell_of)
        for i in ids:cells[i]=row,cells[i][1]
        trial.set_state(cells,trial.orient)
        for path in _group_paths(group):_orient_path(trial,comps,index,plan,path,axis=0)
        trial.attachment_junction_rows[group['id']]=tuple(sorted(set(
            trial.attachment_junction_rows.get(group['id'],()))|{row}))
        trial=_install(trial,comps,index,plan,required=pl.attachment_required_embeddings)
        if trial.cell_of==pl.cell_of and trial.orient==pl.orient and trial.row_y==pl.row_y:continue
        yield trial,dict(kind='conduction_tap_slot',group=group['id'],members=relation['members'],
            physical_row_y=y,row_added=inserted,band=[lo,hi],host_evidence=relation['ends'],
            stage='initialization' if allow_row_split else 'refinement')
