"""Convert validated orthogonal wires to native typed edits, without routing."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import json
from math import gcd
from .profile import transform


def stable_id(kind, value):
    digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:18]
    return 'n2s-'+kind+'-'+digest


def point(value):
    result = tuple(value)
    if len(result) != 2 or any(type(v) not in (int, float) or int(v) != v for v in result):
        raise ValueError('Native route points must be exactly representable integers')
    return tuple(map(int, result))


def on_segment(p, a, b):
    return ((a[0] == b[0] == p[0] and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])) or
            (a[1] == b[1] == p[1] and min(a[0], b[0]) <= p[0] <= max(a[0], b[0])))


def continuation_exit(source, direction, segments):
    """Compatibility for older geometry exports without explicit Pin.escape."""
    candidates=[]
    for a,b in segments:
        a,b=point(a),point(b)
        if not on_segment(source,a,b):
            continue
        for p in (a,b):
            delta=(p[0]-source[0],p[1]-source[1])
            if delta[0]*direction[1] == delta[1]*direction[0] and sum(x*y for x,y in zip(delta,direction))>0:
                candidates.append(p)
    return min(candidates,key=lambda p:abs(p[0]-source[0])+abs(p[1]-source[1])) if candidates else source


def attach_to_last_contact(path, existing):
    """Only add the suffix after the last meeting with the existing same-net tree.

    Keeping a prefix that leaves and rejoins that tree creates a wire loop.
    Collinear overlap counts as contact, independently of segment slicing.
    """
    path=[point(p) for p in path]
    path=[p for i,p in enumerate(path) if not i or p!=path[i-1]]
    last=None
    for i,(a,b) in enumerate(zip(path,path[1:])):
        contacts=[]
        for c,d in existing:
            c,d=point(c),point(d)
            contacts.extend(p for p in (a,b,c,d) if on_segment(p,a,b) and on_segment(p,c,d))
            if (a[0]==b[0])!=(c[0]==d[0]):
                p=(a[0],c[1]) if a[0]==b[0] else (c[0],a[1])
                if on_segment(p,a,b) and on_segment(p,c,d):contacts.append(p)
        if contacts:
            last=(i,max(contacts,key=lambda p:abs(p[0]-a[0])+abs(p[1]-a[1])))
    if last is None:
        raise ValueError('The formal port extension does not meet its original signal tree')
    i,contact=last
    result=[contact,*path[i+1:]]
    return [p for i,p in enumerate(result) if not i or p!=result[i-1]]


def native_routes(net_id, segments, contacts):
    """Split only same-net intersections; compress degree-two paths into bends."""
    lines = [(point(a), point(b)) for a, b in segments if a != b]
    for a, b in lines:
        if a[0] != b[0] and a[1] != b[1]:
            raise ValueError('A native signal route must be orthogonal')
    cuts = [set((a, b)) for a, b in lines]
    for i, (a, b) in enumerate(lines):
        for p in contacts:
            if on_segment(p, a, b):
                cuts[i].add(p)
        for j, (c, d) in enumerate(lines[:i]):
            for p in (a, b, c, d):
                if on_segment(p, a, b) and on_segment(p, c, d):
                    cuts[i].add(p); cuts[j].add(p)
            if (a[0] == b[0]) != (c[0] == d[0]):
                p = (a[0], c[1]) if a[0] == b[0] else (c[0], a[1])
                if on_segment(p, a, b) and on_segment(p, c, d):
                    cuts[i].add(p); cuts[j].add(p)
    graph = defaultdict(set)
    for points in cuts:
        ordered = sorted(points)
        for a, b in zip(ordered, ordered[1:]):
            graph[a].add(b); graph[b].add(a)
    for p in contacts:
        if lines and p not in graph:
            raise ValueError('A native terminal is absent from its planned wire tree')
    stops = {p for p in graph if len(graph[p]) != 2 or p in contacts}
    if graph and not stops:
        raise ValueError('A route-only loop without a terminal cannot be exported')
    endpoints, edits = {}, []
    for p in sorted(stops):
        if p in contacts:
            members = sorted(contacts[p], key=lambda e: (e['instanceId'], e['pinName']))
            endpoints[p] = members[0]
            for other in members[1:]:
                edits.append(dict(kind='connect_endpoints', **{'from': members[0]}, to=other))
        else:
            ident = stable_id('junction', [net_id, p])
            endpoints[p] = dict(kind='junction', junctionId=ident)
            edits.append(dict(kind='add_junction', junctionId=ident, netId=net_id,
                              position=dict(x=p[0], y=p[1])))
    seen = set()
    for start in sorted(stops):
        for neighbor in sorted(graph[start]):
            edge = tuple(sorted((start, neighbor)))
            if edge in seen:
                continue
            seen.add(edge)
            path, previous, current = [start], start, neighbor
            while current not in stops:
                path.append(current)
                following = next(p for p in graph[current] if p != previous)
                seen.add(tuple(sorted((current, following))))
                previous, current = current, following
            path.append(current)
            clean = [path[0]]
            for i, p in enumerate(path[1:-1], 1):
                a, b = path[i-1], path[i+1]
                if not (a[0] == p[0] == b[0] or a[1] == p[1] == b[1]):
                    clean.append(p)
            clean.append(path[-1])
            rid = stable_id('route', [net_id, clean])
            legs = [dict(id=stable_id('leg', [rid, i]), mode='manual',
                         to=dict(kind='bend', bendId=stable_id('bend', [rid, i]),
                                 position=dict(x=p[0], y=p[1])))
                    for i, p in enumerate(clean[1:-1])]
            legs.append(dict(id=stable_id('leg', [rid, len(legs)]), mode='manual',
                             to=dict(kind='endpoint', endpoint=endpoints[current])))
            edits.append(dict(kind='set_route_path', route=dict(id=rid, netId=net_id,
                         start=endpoints[start], legs=legs)))
    return edits


def build_plan(snapshot, profile, wire_plan):
    if wire_plan.get('validation', {}).get('valid') is not True:
        raise ValueError('Cannot export an unverified engine route')
    doc = next(d for d in snapshot['project']['documents'] if d['id'] == snapshot['documentId'])
    instances = {i['id']: i for i in doc['instances']}
    grid = doc['presentation']['grid']
    persisted = [(c['position']['x'], c['position']['y']) for c in wire_plan['components']]
    persisted += [p['origin'] for p in wire_plan['power']]
    persisted += [p for segments in wire_plan['routes'].values() for segment in segments for p in segment]
    for p in persisted:
        for coordinate in point(p):
            grid = gcd(grid, coordinate)
    edits = [dict(kind='set_document_grid', grid=grid)] if doc['presentation']['grid'] != grid else []
    removed = {r['id'] for r in doc['routes']} | {j['id'] for j in doc['junctions']}
    for annotation in doc['annotations']:
        anchor = annotation.get('anchor', {})
        if any(anchor.get(k) in removed for k in ('routeId', 'junctionId', 'objectId')):
            fallback = anchor.get('fallbackPosition')
            if fallback is None:
                raise ValueError('An old geometry-anchored annotation has no safe fallback')
            copy = deepcopy(annotation)
            copy['anchor'] = dict(kind='free', position=fallback)
            edits.append(dict(kind='upsert_schematic_annotation', annotation=copy))
    edits.extend(dict(kind='remove_route_geometry', routeId=r['id']) for r in doc['routes'])
    edits.extend(dict(kind='remove_junction', junctionId=j['id']) for j in doc['junctions'])
    expected_pins, power_markers, symbol_overrides = [], [], []
    pin_escapes = []
    native_nets = {alias: net_id for net_id, alias in profile['net_names'].items()}
    contacts = defaultdict(lambda: defaultdict(list))
    signal_routes = deepcopy(wire_plan['routes'])
    hosts = defaultdict(list)

    def place(ident, position, rotation, mirror, *, symbol_id=None):
        x, y = point((position['x'], position['y']))
        pose = dict(position=dict(x=x, y=y), rotation=rotation, mirror=mirror)
        old = instances.get(ident)
        if old is None:
            if symbol_id is None:
                raise ValueError('A new native marker needs a canonical symbol')
            edits.append(dict(kind='add_instance', instance=dict(id=ident, symbolId=symbol_id, placement=pose)))
        elif old['placement'] is None:
            edits.append(dict(kind='place_instance', instanceId=ident, placement=pose))
        else:
            for kind, name, value in (('rotate_instance','rotation',rotation),
                                      ('mirror_instance','mirror',mirror),('move_instance','position',pose['position'])):
                if old['placement'][name] != value:
                    edits.append(dict(kind=kind, instanceId=ident, **{name: value}))

    def annotation(ident, kind, binding, position, offset, *, net_id=None):
        item = dict(id=stable_id('label', [ident, kind]), kind=kind, binding=binding,
            anchor=dict(kind='object', objectId=ident, localOffset=offset,
                        fallbackPosition=dict(x=position['x']+offset['x'], y=position['y']+offset['y'])),
            alignment='start', rotation=0, locked=False)
        if net_id is not None:
            item['netId'] = net_id
        edits.append(dict(kind='upsert_schematic_annotation', annotation=item))

    for component in wire_plan['components']:
        ident = component['instance_id']
        place(ident, component['position'], component['rotation'], component['mirror'])
        if not any(a.get('binding') == dict(kind='instance-reference', instanceId=ident)
                   for a in doc['annotations']):
            annotation(ident, 'instance-label', dict(kind='instance-reference', instanceId=ident),
                       component['position'], dict(x=24, y=-8))
        for pin in component['pins']:
            endpoint = dict(kind='terminal', instanceId=ident, pinName=pin['name'])
            p = point(pin['at'])
            expected_pins.append(dict(endpoint=endpoint, position=dict(x=p[0], y=p[1])))
            if 'escape' in pin:
                escape=point(pin['escape'])
                pin_escapes.append(dict(endpoint=endpoint,position=dict(x=escape[0],y=escape[1])))
            if pin['net'] in native_nets:
                contacts[pin['net']][p].append(endpoint)
                hosts[pin['net']].append(dict(component=component, pin=pin))
    pools = defaultdict(list)
    for marker in profile['markers']:
        pools[marker['net_id']].append(marker)
    power_routes = defaultdict(list)
    deferred_labels = []
    # Native export orders formal pins by their electrical power domains.
    # A decorative cap must never promote another formal Net to VDD:
    # that changes the positional Cell contract even if every wire is intact.
    logical_groups = {n:g for g in snapshot.get('logicalNets', []) for n in g['baseNetIds']}
    protected_formals = {t['netId'] for t in doc.get('netlist', {}).get('terminals', [])
                         if logical_groups.get(t['netId'], {}).get('powerDomain', 'none') != 'vdd'}
    for item in wire_plan['power']:
        net_id = native_nets[item['net']]
        if net_id in protected_formals:
            p = point(item['anchor'])
            power_routes[item['net']].extend(item['segments'])
            if pools[net_id]:
                marker = pools[net_id].pop(0)
                ident = marker['instance_id']
                if not marker.get('interface'):
                    raise ValueError('An unclassified existing power marker needs explicit native power evidence')
                pin = snapshot['symbols'][ident]['pins'][0]
                toward = next((point(b if point(a)==p else a) for a,b in item['segments']
                               if p in (point(a),point(b))), None)
                vectors = {'east':(1,0),'west':(-1,0),'north':(0,-1),'south':(0,1)}
                direction = ((toward[0]>p[0])-(toward[0]<p[0]),
                             (toward[1]>p[1])-(toward[1]<p[1])) if toward else (0,1)
                rotation = next(r for r in (0,90,180,270)
                                if transform(vectors[pin['direction']],r,'none')==direction)
                local = transform((pin['at']['x'],pin['at']['y']),rotation,'none')
                place(ident,dict(x=p[0]-local[0],y=p[1]-local[1]),rotation,'none')
                endpoint = dict(kind='terminal',instanceId=ident,pinName=pin['name'])
                contacts[item['net']][p].append(endpoint)
                expected_pins.append(dict(endpoint=endpoint,position=dict(x=p[0],y=p[1])))
                power_markers.append(dict(instanceId=ident,pinName=pin['name'],netId=net_id,
                                          style='formal-power-port'))
            else:
                # The original Base Net already carries electrical identity.
                # A junction with a bound name draws a local named supply stub
                # without introducing a new power-domain/name claim.
                junction = stable_id('junction',[net_id,p])
                deferred_labels.append(dict(kind='upsert_schematic_annotation',annotation=dict(
                    id=stable_id('label',[junction,'net-label']),kind='net-label',netId=net_id,
                    binding=dict(kind='net-name',netId=net_id),
                    anchor=dict(kind='object',objectId=junction,localOffset=dict(x=12,y=-8),
                                fallbackPosition=dict(x=p[0]+12,y=p[1]-8)),
                    alignment='start',rotation=0,locked=False)))
            continue
        canonical = item['symbol']
        # The engine can draw VSS as a ground-style glyph while keeping its
        # Net distinct from 0. Native Ground has stronger node-0 semantics;
        # use a named downward cap for every separate low rail instead.
        true_ground = canonical == 'ground' and item['net'] == '0'
        negative = canonical == 'vee-port' or (canonical == 'ground' and not true_ground)
        found = snapshot['markerSymbols']['vdd-port' if negative else canonical]
        actual_symbol = found['definition']['id']
        marker_pin = found['definition']['pins'][0]['name']
        if pools[net_id]:
            marker = pools[net_id].pop(0)
            if marker['symbol_id'] != actual_symbol or marker['pin'] != marker_pin:
                raise ValueError('Existing native supply marker geometry differs from the selected profile')
            ident = marker['instance_id']
            if marker.get('original_symbol_id') not in (None, actual_symbol):
                edits.append(dict(kind='set_instance_symbol',instanceId=ident,symbolId=actual_symbol))
                symbol_overrides.append(dict(instanceId=ident,original=marker['original_symbol_id'],
                    symbolId=actual_symbol,reason='native-power-interface'))
        else:
            ident = stable_id('power', [net_id, item['component_id'], item['pin']])
            group = next((g for g in snapshot['logicalNets'] if net_id in g['baseNetIds']), {})
            label = group.get('name') or next((e.get('sourceName') for e in doc.get('connectivityEvidence', [])
                      if e.get('netId') == net_id and e.get('kind') == 'net-name-hint'), item['net'])
            edits.append(dict(kind='upsert_connectivity_evidence', evidence=dict(
                id=stable_id('claim', ident), kind='name-claim', name=label, netId=net_id,
                owner=dict(kind='power-marker', objectId=ident), scope=group.get('scope', 'local'),
                powerDomain='ground' if true_ground else 'vdd')))
        rotation = 180 if negative else 0
        if negative:
            # Rotating the native cap reverses its own true pin offset. Keep
            # the engine's real anchor, rather than using its drawing origin.
            local = found['definition']['pins'][0]['at']
            origin = [item['anchor'][0]+local['x'], item['anchor'][1]+local['y']]
        else:
            origin = item['origin']
        place(ident, dict(zip(('x','y'), origin)), rotation, 'none', symbol_id=actual_symbol)
        if not true_ground and not any(
                a.get('anchor', {}).get('objectId') == ident and a.get('binding', {}).get('kind') == 'net-name'
                for a in doc['annotations']):
            annotation(ident, 'net-label', dict(kind='net-name', netId=net_id),
                       dict(zip(('x','y'), origin)), dict(x=14, y=4 if negative else -4), net_id=net_id)
        endpoint = dict(kind='terminal', instanceId=ident, pinName=marker_pin)
        p = point(item['anchor'])
        expected_pins.append(dict(endpoint=endpoint, position=dict(x=p[0], y=p[1])))
        contacts[item['net']][p].append(endpoint)
        power_routes[item['net']].extend(item['segments'])
        power_markers.append(dict(instanceId=ident, pinName=marker_pin, netId=net_id,
                                 style='ground' if true_ground else 'negative-cap' if negative else 'positive-cap'))
    directions={'east':(1,0),'west':(-1,0),'north':(0,-1),'south':(0,1)}
    counts=defaultdict(int)
    for port in profile.get('ports', []):
        alias=profile['net_names'][port['net_id']]
        choices=hosts[alias]
        if not choices:
            # An unused formal interface has no conductor to attach to. Its
            # existing tray/placement state is retained, never tied elsewhere.
            continue
        controls={'G','B','IN+','IN-'}
        outputs={'D','C','OUT'}
        wanted=outputs if port['direction']=='output' else controls
        selected=min(choices,key=lambda h:(h['pin']['name'] not in wanted,
            h['component']['instance_id'],h['pin']['name']))
        host,pin=selected['component'],selected['pin']
        direction=directions[pin['direction']]
        raw_pin=port['symbol']['pins'][0]
        base=directions[raw_pin['direction']]
        rotation=next(r for r in (0,90,180,270) if transform(base,r,'none')==(-direction[0],-direction[1]))
        box=port['symbol']['definition']['viewBox']
        span=max(48,int(max(box['width'],box['height']))+24)
        number=counts[alias];counts[alias]+=1
        offset=0 if number==0 else ((number+1)//2)*span*(1 if number%2 else -1)
        source=point(pin['at'])
        local=transform((raw_pin['at']['x'],raw_pin['at']['y']),rotation,'none')
        def blocked(candidate):
            corners=[transform((x,y),rotation,'none') for x in (box['x'],box['x']+box['width'])
                     for y in (box['y'],box['y']+box['height'])]
            ox,oy=candidate[0]-local[0],candidate[1]-local[1]
            x0,x1=min(p[0] for p in corners)+ox,max(p[0] for p in corners)+ox
            y0,y1=min(p[1] for p in corners)+oy,max(p[1] for p in corners)+oy
            for segments in [*signal_routes.values(),*power_routes.values()]:
                for a,b in segments:
                    if a[0]==b[0] and x0<=a[0]<=x1 and max(min(a[1],b[1]),y0)<=min(max(a[1],b[1]),y1):return True
                    if a[1]==b[1] and y0<=a[1]<=y1 and max(min(a[0],b[0]),x0)<=min(max(a[0],b[0]),x1):return True
            return False
        step=max(24,int(min(box['width'],box['height']))+12)
        seats=[offset+d*step for d in (0,1,-1,2,-2,3,-3)]
        for possible in seats:
            candidate=(source[0]+direction[0]*span-direction[1]*possible,
                       source[1]+direction[1]*span+direction[0]*possible)
            if not blocked(candidate):
                offset=possible;break
        anchor=(source[0]+direction[0]*span-direction[1]*offset,
                source[1]+direction[1]*span+direction[0]*offset)
        origin=(anchor[0]-local[0],anchor[1]-local[1])
        ident=port['instance_id']
        place(ident,dict(x=origin[0],y=origin[1]),rotation,'none')
        if not any(a.get('binding',{}).get('kind')=='cell-terminal-name' and
                   a['binding'].get('terminalId')==port['terminal_id'] for a in doc['annotations']):
            annotation(ident,'instance-label',dict(kind='cell-terminal-name',terminalId=port['terminal_id']),
                       dict(x=origin[0],y=origin[1]),dict(x=0,y=-18))
        endpoint=dict(kind='terminal',instanceId=ident,pinName=port['pin'])
        expected_pins.append(dict(endpoint=endpoint,position=dict(x=anchor[0],y=anchor[1])))
        contacts[alias][point(anchor)].append(endpoint)
        if offset:
            exit_point=point(pin['escape']) if 'escape' in pin else continuation_exit(source,direction,signal_routes.get(alias,[]))
            corner=(exit_point[0],anchor[1]) if direction[0] else (anchor[0],exit_point[1])
            path=[source,exit_point,corner,anchor]
        else:
            path=[source,anchor]
        path=attach_to_last_contact(path,[*signal_routes.get(alias,[]),[source,source]])
        signal_routes.setdefault(alias,[]).extend([[a,b] for a,b in zip(path,path[1:]) if a!=b])
        power_markers.append(dict(instanceId=ident,pinName=port['pin'],netId=port['net_id'],style='signal-port'))
    for net, segments in {**signal_routes, **power_routes}.items():
        if net not in native_nets:
            continue
        # Disconnected power stubs are deliberately equivalent by their
        # existing native Net; no long artificial power bus is invented.
        edits.extend(native_routes(native_nets[net], segments, contacts[net]))
    edits.extend(deferred_labels)
    for edit in edits:
        p = edit.get('position') or edit.get('instance',{}).get('placement',{}).get('position')
        if p:
            for coordinate in point((p['x'],p['y'])):grid=gcd(grid,coordinate)
    if grid != doc['presentation']['grid']:
        if edits and edits[0]['kind']=='set_document_grid':edits[0]['grid']=grid
        else:edits.insert(0,dict(kind='set_document_grid',grid=grid))
    if len(edits) > 1024:
        raise ValueError('Native transaction edit limit exceeded')
    return dict(protocolVersion=1, source=deepcopy(profile['source']),
                projectId=profile['project_id'], documentId=doc['id'], expectedRevision=doc['revision'],
                transactionId=stable_id('transaction', [profile['source']['sha256'], edits]),
                edits=edits, expectedPins=expected_pins, engineMetrics=wire_plan['metrics'],
                powerMarkers=power_markers,symbolOverrides=symbol_overrides,
                pinEscapes=pin_escapes,signalGeometry={native_nets[alias]:dict(
                    coreSegments=deepcopy(segments),
                    terminals=[endpoint for members in contacts[alias].values() for endpoint in members])
                    for alias,segments in wire_plan['routes'].items() if alias in native_nets},
                nativeGrid=grid, originalGrid=doc['presentation']['grid'])
