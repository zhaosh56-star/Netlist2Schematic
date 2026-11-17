"""Read semantic conductor centerlines, never guess wires from SVG artwork."""
from collections import defaultdict,Counter
from itertools import combinations
from math import hypot,isfinite
import re
import xml.etree.ElementTree as ET

EPS=1e-6

def point(value):
    return tuple(round(float(x),6) for x in value)

def sub(a,b):return a[0]-b[0],a[1]-b[1]
def dot(a,b):return a[0]*b[0]+a[1]*b[1]
def cross(a,b):return a[0]*b[1]-a[1]*b[0]
def length(segment):return hypot(*sub(*segment))

def on(p,segment):
    a,b=segment;r=sub(b,a);q=sub(p,a);size=hypot(*r)
    return size>EPS and abs(cross(r,q))<=EPS*size and -EPS*size<=dot(q,r)<=dot(r,r)+EPS*size

def crossing(first,second):
    a,b=first;c,d=second;r=sub(b,a);s=sub(d,c);den=cross(r,s)
    if abs(den)<=1e-10*hypot(*r)*hypot(*s):return None
    t=cross(sub(c,a),s)/den;u=cross(sub(c,a),r)/den
    et=EPS/hypot(*r);eu=EPS/hypot(*s)
    if not (-et<=t<=1+et and -eu<=u<=1+eu):return None
    return point((a[0]+t*r[0],a[1]+t*r[1])),et<t<1-et and eu<u<1-eu

def merge(segments):
    groups={}
    for a,b in segments:
        a,b=point(a),point(b)
        if a>b:a,b=b,a
        d=sub(b,a);size=hypot(*d)
        if size<=EPS:continue
        u=d[0]/size,d[1]/size;n=-u[1],u[0];offset=dot(n,a)
        key=round(u[0],9),round(u[1],9),round(offset,6)
        group=groups.setdefault(key,dict(u=u,n=n,offset=offset,intervals=[]))
        group['intervals'].append(sorted((dot(group['u'],a),dot(group['u'],b))))
    result=[]
    for key,g in sorted(groups.items()):
        intervals=sorted(g['intervals']);joined=[]
        for lo,hi in intervals:
            if joined and lo<=joined[-1][1]+EPS:joined[-1][1]=max(joined[-1][1],hi)
            else:joined.append([lo,hi])
        for lo,hi in joined:
            result.append(tuple(point((g['u'][0]*t+g['n'][0]*g['offset'],
                                       g['u'][1]*t+g['n'][1]*g['offset'])) for t in (lo,hi)))
    return result

def topology(segments):
    runs=merge(segments);cuts=[set(s) for s in runs]
    for i,j in combinations(range(len(runs)),2):
        hit=crossing(runs[i],runs[j])
        if hit:cuts[i].add(hit[0]);cuts[j].add(hit[0])
        else:
            for p in runs[i]:
                if on(p,runs[j]):cuts[j].add(p)
            for p in runs[j]:
                if on(p,runs[i]):cuts[i].add(p)
    adjacency=defaultdict(set)
    for (a,b),ps in zip(runs,cuts):
        ps=sorted(ps,key=lambda p:dot(sub(p,a),sub(b,a)))
        for x,y in zip(ps,ps[1:]):
            if x==y:continue
            adjacency[x].add(y);adjacency[y].add(x)
    bends=[]
    for p,neighbors in adjacency.items():
        if len(neighbors)!=2:continue
        a,b=neighbors;x=sub(a,p);y=sub(b,p)
        if abs(cross(x,y))>1e-8*hypot(*x)*hypot(*y):bends.append(p)
    return dict(runs=runs,wirelength=sum(map(length,runs)),bends=bends,
                junctions=[p for p,n in adjacency.items() if len(n)>2])

def measure(nets):
    graphs={str(net):topology(segments) for net,segments in nets.items() if segments}
    cross_points=[];contacts=[];overlaps=[]
    for first,second in combinations(sorted(graphs),2):
        proper=set();touch=set();shared=[]
        for a in graphs[first]['runs']:
            for b in graphs[second]['runs']:
                hit=crossing(a,b)
                if hit:
                    (proper if hit[1] else touch).add(hit[0]);continue
                # Collinear shared intervals are contacts, not crossings.
                shared_points={p for p in a if on(p,b)}|{p for p in b if on(p,a)}
                if len(shared_points)>1 and max(hypot(*sub(x,y)) for x,y in combinations(shared_points,2))>EPS:
                    shared.append(sorted(shared_points))
                elif shared_points:touch.update(shared_points)
        cross_points.extend(dict(nets=[first,second],point=p) for p in sorted(proper))
        contacts.extend(dict(nets=[first,second],point=p) for p in sorted(touch-proper))
        overlaps.extend(dict(nets=[first,second],points=p) for p in shared)
    total=sum(g['wirelength'] for g in graphs.values())
    return dict(metrics=dict(wirelength=round(total,6),crossings=len(cross_points),
        bends=sum(len(g['bends']) for g in graphs.values())),
        wirelength_per_40_units=round(total/40,6),net_count=len(graphs),
        per_net={net:dict(wirelength=round(g['wirelength'],6),bends=len(g['bends']),
            junctions=len(g['junctions']),merged_run_count=len(g['runs'])) for net,g in graphs.items()},
        crossing_points=cross_points,foreign_contacts=contacts,collinear_overlaps=overlaps,
        nonorthogonal_runs=sum(a[0]!=b[0] and a[1]!=b[1] for g in graphs.values() for a,b in g['runs']))

def svg_routes(path):
    root=ET.parse(path).getroot();nets=defaultdict(list);routes=[];warnings=[]
    def visit(element,layers=(),transforms=()):
        layer=element.get('data-layer');layers=layers+((layer,) if layer else ())
        transforms=transforms+((element.get('transform'),) if element.get('transform') else ())
        if 'routes' in layers and element.get('data-net-id'):
            if transforms:raise ValueError('Semantic route transform needs explicit support: '+str(transforms))
            if element.tag.split('}')[-1]!='polyline':raise ValueError('Unknown semantic conductor element')
            numbers=[float(x) for x in re.findall(r'[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?',element.get('points',''))]
            if len(numbers)%2 or len(numbers)<4 or not all(map(isfinite,numbers)):
                raise ValueError('Malformed semantic route points')
            ps=[point(numbers[k:k+2]) for k in range(0,len(numbers),2)]
            segments=[(a,b) for a,b in zip(ps,ps[1:]) if a!=b]
            net=element.get('data-net-id');nets[net].extend(segments)
            routes.append(dict(id=element.get('data-object-id'),net=net,
                presentation=element.get('data-route-presentation'),segments=segments))
        for child in element:visit(child,layers,transforms)
    visit(root)
    if not routes:raise ValueError('No explicit semantic centerlines in GT')
    result=measure(nets)
    symbol_counts=Counter(n.get('data-symbol-id') for n in root.iter() if n.get('data-symbol-id'))
    component_counts={k:v for k,v in symbol_counts.items() if k not in ('ground','vdd-port','port','port-filled')}
    result.update(status='measured',method='semantic_net_centerlines_euclidean_v1',
        scope='all_visible_routes_including_power_connections',units='shared_symbol_coordinate_units',
        route_count=len(routes),routes=routes,source=str(path),
        symbol_counts=dict(symbol_counts),component_counts=component_counts,
        component_count=sum(component_counts.values()),
        net_label_count=sum(n.get('data-kind') in ('net-label','port-label') for n in root.iter()),
        electrical_equivalence_verified=False,
        note='显式网络中心线；含供电；斜线按实际长度计；不含符号、文字和描边修饰')
    return result
