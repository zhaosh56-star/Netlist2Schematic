"""Bounded label assignment around real owners; no electrical geometry moves."""
from math import hypot
from time import perf_counter

from netlist2schematic.rendering.sa_render import _overlap, _wire_in_box

VERSION='nearby-owner-labels-v1'


def box_distance(a,b):
    return hypot(max(b[0]-a[2],a[0]-b[2],0.),max(b[1]-a[3],a[1]-b[3],0.))


def local_candidates(box,size):
    x0,y0,x1,y1=box;cx,cy=(x0+x1)/2,(y0+y1)/2
    for gap in (6.,10.,16.,24.,36.,48.):
        for slide in (0.,-8.,8.,-16.,16.,-24.,24.,-32.,32.,-40.,40.):
            yield cx+slide,y0-gap-8.5,'middle'
            yield cx+slide,y1+gap+size*1.12,'middle'
            yield x0-gap-3,cy+3+slide,'end'
            yield x1+gap+2,cy+3+slide,'start'
        # Small caps and short passive symbols can be boxed in along all four
        # axes by their leads. Use the adjacent diagonal pockets as well.
        yield x0-gap-3,y0-gap-8.5,'end'
        yield x1+gap+2,y0-gap-8.5,'start'
        yield x0-gap-3,y1+gap+size*1.12,'end'
        yield x1+gap+2,y1+gap+size*1.12,'start'


def assign(records,component_boxes,boxes,wires,bounds_of,extended,*,font_size,
           seconds=1.,max_states=20000):
    """Joint finite assignment, prioritizing the labels with the fewest slots."""
    started=perf_counter();deadline=started+seconds
    pools=[]
    for record in records:
        own=record.get('owner_box')
        component=record.get('component') if record['kind']=='component' else None
        candidates=list(record['candidates'])
        if own is not None:
            candidates=list(local_candidates(own,font_size))+candidates
        # Ordinary port slots are supplemented only when needed, preserving
        # actual pin anchors. Component names never move to a global ring.
        elif record['kind']=='port':
            candidates+=list(extended(record['anchor']))
        seen=set();pool=[]
        for order,(x,y,align) in enumerate(candidates):
            key=(x,y,align)
            if key in seen:continue
            seen.add(key)
            bounds=bounds_of(x,y,record['text'],align,record['kind'])
            gap=box_distance(bounds,own) if own is not None else hypot(x-record['anchor'][0],y-record['anchor'][1])
            limit=(4.25 if record['kind'] in ('supply','shared-supply') else 2.5)*font_size
            if own is not None and gap>limit:continue
            if component is not None and any(box_distance(bounds,b)<gap-1e-8
                    for name,b in component_boxes.items() if name!=component):continue
            if any(_overlap(bounds,b,2) for b in boxes):continue
            if any(_wire_in_box(a,b,bounds,margin=2) for a,b in wires):continue
            pool.append(((0,0,gap,order),x,y,align,bounds,0,0,0))
        pool.sort(key=lambda item:item[0])
        # Keep several directions available instead of letting one row of
        # almost identical nearby slots consume all options.
        pools.append(pool[:64])
    missing=[dict(kind=r['kind'],text=r['text'],component=r.get('component'))
             for r,p in zip(records,pools) if not p]
    if missing:
        raise ValueError('No nearby readable label position: '+repr(missing))
    selected={};states=0

    def solve(pending):
        nonlocal states
        if not pending:return True
        if states>=max_states or perf_counter()>=deadline:return False
        options=[]
        occupied=[item[4] for item in selected.values()]
        for i in pending:
            available=[item for item in pools[i] if not any(_overlap(item[4],b,2) for b in occupied)]
            if not available:return False
            options.append((len(available),records[i]['anchor'][1],records[i]['anchor'][0],i,available))
        _,_,_,i,available=min(options,key=lambda item:item[:4])
        remaining=[j for j in pending if j!=i]
        for item in available:
            states+=1;selected[i]=item
            if solve(remaining):return True
            del selected[i]
            if states>=max_states or perf_counter()>=deadline:break
        return False

    if not solve(list(range(len(records)))):
        raise ValueError('Nearby label assignment exhausted its finite search; labels were not detached from owners')
    return [selected[i] for i in range(len(records))],dict(version=VERSION,
        states=states,max_component_gap=2.5*font_size,max_supply_gap=4.25*font_size,
        ownership_violations=0,component_names_global_search=False)
