"""Compose symbol-aware modules and their real-net local connections.

Series composition aligns a common axis. Parallel composition gives children
separate lanes and two common buses. Prefixes reserve fixed escapes and a
branch keepout, so buses never run through symbol pins or protection halos.
"""
from collections import defaultdict
from math import ceil

from netlist2schematic.plugins.passive_embedding.passive_embedding import VECTORS
from netlist2schematic.routing.routing_rules import _escape, _inflate


SPACING_VERSION = 'passive-module-spacing-v1'
PASSIVE_SYMBOLS = frozenset(('resistor','capacitor','inductor','diode'))


def can_compact(tree, comps, index):
    def visit(node):
        if node['kind']=='part':
            return comps[index[node['device']]]['symbol'] in PASSIVE_SYMBOLS, False
        children=[visit(c) for c in node['children']]
        pure=all(c[0] for c in children)
        own=(node['kind']=='parallel' and pure and len(children)>1) or (node['kind']=='series' and any(
            a['kind']==b['kind']=='part' and ca[0] and cb[0]
            for a,b,ca,cb in zip(node['children'],node['children'][1:],children,children[1:])))
        return pure, own or any(c[1] for c in children)
    return visit(tree)[1]


def template(par, pl, comps, index, tree, direction, options, *, extra_keepout=0,
             spacing='compact'):
    if spacing not in ('compact','standard'):
        raise ValueError('Module spacing must be compact or standard')
    actions=[]
    axis = 0 if direction[0] else 1
    cross = 1-axis
    step = options.grid_step
    def up(value):
        return ceil(value/step)*step
    def xy(along, transverse=0):
        p = [0., 0.]; p[axis] = direction[axis]*along; p[cross] = transverse
        return tuple(p)
    def shifted(layout, along, transverse):
        delta = xy(along, transverse)
        def move(p):
            return tuple(p[k]+delta[k] for k in (0, 1))
        return dict(positions={i:move(p) for i,p in layout['positions'].items()},
                    orientations=layout['orientations'], extent=layout['extent'],
                    wires={net:[(move(a), move(b)) for a,b in segments] for net,segments in layout['wires'].items()})
    def envelope(layout):
        boxes=[]
        for i,(x,y) in layout['positions'].items():
            box=par.GEO_DOC[comps[i]['symbol']]['orientations'][layout['orientations'][i]]['viewBoxAABB']
            boxes.append((x+box['x'],y+box['y'],x+box['x']+box['width'],y+box['y']+box['height']))
        return (min(b[0] for b in boxes),min(b[1] for b in boxes),
                max(b[2] for b in boxes),max(b[3] for b in boxes))
    def compose(node):
        if node['kind']=='part':
            i=index[node['device']]; symbol=comps[i]['symbol']
            incoming=next(p for p,n in node['pins'].items() if n==node['ends'][0])
            outgoing=next(p for p,n in node['pins'].items() if n==node['ends'][1])
            choices=[]
            allowed=getattr(pl,'allowed_orientations',lambda j:range(pl.n_orients(j)))
            for orient in allowed(i):
                geo=par.GEO_DOC[symbol]['orientations'][orient]
                dirs={p['name']:VECTORS[p['direction']] for p in geo['pins']}
                if dirs[incoming]!=tuple(-v for v in direction) or dirs[outgoing]!=direction:
                    continue
                a,b=(par.pin_pos(symbol,orient,p) for p in (incoming,outgoing))
                box=geo['viewBoxAABB']
                rect=_inflate((box['x'],box['y'],box['x']+box['width'],box['y']+box['height']),options.halo)
                ea=_escape(a,tuple(-v for v in direction),rect,step)
                eb=_escape(b,direction,rect,step)
                # Same-net buses need to leave the halo, not a second foreign
                # crossing keepout beyond it. Foreign geometry is still checked.
                lead_a=up(abs(ea[axis]-a[axis])+extra_keepout+step)
                lead_b=up(abs(eb[axis]-b[axis])+extra_keepout+step)
                offset=tuple(xy(lead_a)[k]-a[k] for k in (0,1))
                pa=xy(lead_a); pb=tuple(b[k]+offset[k] for k in (0,1))
                extent=up(lead_a+abs(b[axis]-a[axis])+lead_b)
                choices.append((orient!=pl.orient[i],orient,offset,pa,pb,extent))
            if not choices:
                return None
            _,orient,offset,pa,pb,extent=min(choices)
            return dict(positions={i:offset},orientations={i:orient},extent=extent,
                        wires={node['ends'][0]:[(xy(0),pa)],node['ends'][1]:[(pb,xy(extent))]})
        children=[compose(c) for c in node['children']]
        if any(c is None for c in children):
            return None
        positions={}; orientations={}; wires=defaultdict(list)
        def add(child):
            positions.update(child['positions']);orientations.update(child['orientations'])
            for net,segments in child['wires'].items():
                wires[net].extend(segments)
        if node['kind']=='series':
            cursor=0.;previous_start=0.
            for number,child in enumerate(children):
                start=cursor
                # Only an unbranched part-to-part join may share the two
                # facing exit corridors. Composite buses and source devices
                # retain their original interfaces and quiet space.
                if spacing=='compact' and number:
                    first,last=node['children'][number-1:number+1]
                    if (first['kind']==last['kind']=='part' and
                            all(comps[index[n['device']]]['symbol'] in PASSIVE_SYMBOLS for n in (first,last))):
                        before,after=envelope(children[number-1]),envelope(child)
                        high=max(direction[axis]*before[k] for k in (axis,axis+2))
                        low=min(direction[axis]*after[k] for k in (axis,axis+2))
                        minimum=previous_start+high-low+2*(options.halo+extra_keepout)
                        start=min(cursor,max(previous_start,up(minimum)))
                        if start<cursor:
                            actions.append(dict(kind='series_join',devices=[first['device'],last['device']],
                                                saved=cursor-start))
                add(shifted(child,start,0));previous_start=start;cursor=start+child['extent']
            extent=cursor
        else:
            boxes=[envelope(c) for c in children]
            gap=up(2*options.halo+max(options.center_spacing,options.cross_clearance))
            half=max(max(abs(box[cross]),abs(box[cross+2])) for box in boxes)
            pitch=ceil((2*half+gap)/(2*step))*2*step
            lanes=[(j-(len(children)-1)/2)*pitch for j in range(len(children))]
            def pure(node):
                return (comps[index[node['device']]]['symbol'] in PASSIVE_SYMBOLS
                        if node['kind']=='part' else all(pure(c) for c in node['children']))
            if spacing=='compact' and pure(node) and len(children)>1:
                adjacent=[0.]
                for left,right in zip(boxes,boxes[1:]):
                    # Leave both original halos intact. Extra caller-owned
                    # keepout also remains; no foreign through-track is
                    # implicitly reserved inside an isolated passive group.
                    minimum=left[cross+2]-right[cross]+2*(options.halo+extra_keepout)
                    adjacent.append(adjacent[-1]+ceil(minimum/(2*step))*2*step)
                span=adjacent[-1];center=span/2
                compact=[p-center for p in adjacent]
                if span<lanes[-1]-lanes[0]:
                    actions.append(dict(kind='parallel_lanes',standard_span=lanes[-1]-lanes[0],
                        compact_span=span,lanes=list(compact)))
                    lanes=compact
            extent=max(c['extent'] for c in children)
            for child,lane in zip(children,lanes):
                lead=round((extent-child['extent'])/(2*step))*step
                add(shifted(child,lead,lane))
                wires[node['ends'][0]].append((xy(0,lane),xy(lead,lane)))
                wires[node['ends'][1]].append((xy(lead+child['extent'],lane),xy(extent,lane)))
            for net,along in zip(node['ends'],(0,extent)):
                wires[net].append((xy(along,min(0,*lanes)),xy(along,max(0,*lanes))))
        return dict(positions=positions,orientations=orientations,extent=extent,wires=dict(wires))
    layout=compose(tree)
    if layout is None:
        return None
    layout.update(axis=axis,envelope=envelope(layout),ports=[xy(0),xy(layout['extent'])],
        spacing=dict(version=SPACING_VERSION,policy=spacing,changed=bool(actions),actions=actions))
    layout['wires']={net:[s for s in segments if s[0]!=s[1]] for net,segments in layout['wires'].items()}
    return layout
