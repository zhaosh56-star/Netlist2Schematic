"""Translate native topology/resources, without changing engine source code."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shutil

PROTOCOL_VERSION = 1
CLASSES = {
    'resistor': ('R', 'resistor', ('1', '2')),
    'capacitor': ('C', 'capacitor', ('1', '2')),
    'inductor': ('L', 'inductor', ('1', '2')),
    'diode': ('D', 'diode', ('A', 'K')),
    'voltage-source': ('V', 'voltage-source', ('+', '-')),
    'current-source': ('I', 'current-source', ('+', '-')),
}
POSITIVE_ALIASES = ('vdd', 'vcc', 'avdd', 'dvdd', 'vpwr', 'vdd!', 'vcc!')
LOW_ALIASES = ('vss', 'vss!', 'gnds', 'agnd', 'dgnd', 'gnd', 'gnd!')
MARKERS = frozenset(('ground', 'vdd-port', 'vdd'))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def transform(point, rotation, mirror):
    x, y = point
    for _ in range(rotation // 90):
        x, y = -y, x
    if mirror in ('horizontal', 'both'):
        x = -x
    if mirror in ('vertical', 'both'):
        y = -y
    return x, y


def canonical_geometry(original, definition):
    result = deepcopy(original)
    symbol = definition['symbol']
    variant = next((v for v in symbol.get('variants', [])
                    if v['id'] == symbol.get('defaultVariantId')), {})
    visible = [p for p in symbol['pins'] if p['name'] not in variant.get('hiddenPinNames', [])]
    result.update(viewBox=deepcopy(symbol['viewBox']), pins=deepcopy(visible))
    directions = {'east': (1, 0), 'west': (-1, 0), 'north': (0, -1), 'south': (0, 1)}
    for pose in result['orientations']:
        rotation, mirror = pose['rotation'], pose['mirror']
        pins = []
        for pin in visible:
            x, y = transform((pin['at']['x'], pin['at']['y']), rotation, mirror)
            direction = transform(directions[pin['direction']], rotation, mirror)
            pins.append(dict(name=pin['name'], at=dict(x=x, y=y),
                             direction=next(k for k, v in directions.items() if v == direction)))
        box = symbol['viewBox']
        corners = [transform((x, y), rotation, mirror)
                   for x in (box['x'], box['x']+box['width'])
                   for y in (box['y'], box['y']+box['height'])]
        xs, ys = [p[0] for p in corners], [p[1] for p in corners]
        pose.update(pins=pins, viewBoxAABB=dict(x=min(xs), y=min(ys),
                    width=max(xs)-min(xs), height=max(ys)-min(ys)),
                    pinSpread=dict(minX=min(p['at']['x'] for p in pins),
                        maxX=max(p['at']['x'] for p in pins), minY=min(p['at']['y'] for p in pins),
                        maxY=max(p['at']['y'] for p in pins)))
    return result


def compile_snapshot(snapshot):
    if snapshot.get('protocolVersion') != PROTOCOL_VERSION:
        raise ValueError('Unsupported native adapter protocol version')
    project = snapshot['project']
    document = next((d for d in project['documents'] if d['id'] == snapshot['documentId']), None)
    if document is None:
        raise ValueError('Selected Document is absent')
    if document.get('constraints'):
        raise ValueError('Native layout constraints require an explicit adapter policy; input left unchanged')
    if document.get('inductorCouplings'):
        raise ValueError('Coupled inductors are not supported by this engine adapter')
    for group in snapshot.get('logicalNets', []):
        if group.get('conflicts'):
            raise ValueError('Conflicting native net identities cannot be laid out')
        if len(group['baseNetIds']) > 1:
            raise ValueError('Multiple Base Nets in one Logical Net need explicit consolidation; input left unchanged')
    names, terminal_nets, used = {}, {}, set()
    groups = {base: group for group in snapshot.get('logicalNets', []) for base in group['baseNetIds']}
    for net in sorted(document['nets'], key=lambda n: n['id']):
        group = groups.get(net['id'], {})
        hint = group.get('name')
        if not hint:
            hint = next((e.get('sourceName') for e in document.get('connectivityEvidence', [])
                         if e.get('netId') == net['id'] and e.get('kind') == 'net-name-hint'), None)
        if hint and hint.casefold() == 'vee':
            alias = 'vee'
        elif hint and hint.casefold() == '0':
            alias = '0'
        elif group.get('powerDomain') == 'ground' or snapshot.get('netRoles', {}).get(net['id']) == 'ground':
            preferred = hint.casefold() if hint else None
            alias = (preferred if preferred in LOW_ALIASES and preferred not in used else
                     next((n for n in LOW_ALIASES if n not in used), None))
            if alias is None:
                raise ValueError('Too many distinct low supply rails for the engine vocabulary')
        elif group.get('powerDomain') == 'vdd' or snapshot.get('netRoles', {}).get(net['id']) == 'supply' or (hint and hint.casefold() in
                 ('vdd', 'vcc', 'avdd', 'dvdd', 'vpwr', 'vdd!', 'vcc!')):
            preferred = hint.casefold() if hint else None
            alias = (preferred if preferred in POSITIVE_ALIASES and preferred not in used else
                     next((n for n in POSITIVE_ALIASES if n not in used), None))
            if alias is None:
                raise ValueError('Too many distinct supply nets for the canonical engine role vocabulary')
        else:
            alias = hint if hint and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_+.!-]*', hint) else 'n_'+digest(net['id'])[:12]
        alias = alias.casefold()
        if alias in used:
            if group.get('name') or alias in ('0', 'vee', *POSITIVE_ALIASES):
                raise ValueError('Distinct native nets cannot share a layout alias')
            # Import hints are non-electrical. Repeated hints must never merge
            # independent native Nets at the case-insensitive SPICE boundary.
            alias = 'n_'+digest(net['id'])[:12]
        used.add(alias)
        names[net['id']] = alias
        for terminal in net['terminals']:
            if terminal.get('memberId'):
                raise ValueError('Bus members require a separate native adapter')
            key = (terminal['instanceId'], terminal['pinName'])
            if key in terminal_nets:
                raise ValueError('A native pin belongs to more than one Base Net')
            terminal_nets[key] = net['id']
    definitions, components, markers, ports, statements = {}, {}, [], [], []
    models, model_keys = [], {}
    for number, instance in enumerate(sorted(document['instances'], key=lambda i: i['id']), 1):
        resolved = snapshot['symbols'][instance['id']]
        symbol_id = instance['symbolId']
        if symbol_id in ('port', 'port-filled'):
            if len(resolved['pins']) != 1 or resolved['pins'][0]['name'] != 'P':
                raise ValueError('A native Cell Pin needs one scalar P terminal')
            net_id = terminal_nets.get((instance['id'], 'P'))
            if net_id is None:
                raise ValueError('Native Cell Pin has no declared network')
            declaration = next((p for p in document['netlist']['terminals']
                                if instance['id'] in p.get('interfaceInstanceIds', [])), None)
            if declaration is None:
                raise ValueError('Native Cell Pin is missing its interface declaration')
            alias = names[net_id]
            if alias in ('0', 'vee', *POSITIVE_ALIASES, *LOW_ALIASES):
                markers.append(dict(instance_id=instance['id'], pin='P', net_id=net_id,
                    symbol_id='vdd-port', original_symbol_id=symbol_id, interface=True))
            else:
                direction=declaration['direction']
                if direction not in ('input','output'):
                    role=snapshot.get('netRoles',{}).get(net_id)
                    if role in ('input','output'):direction=role
                ports.append(dict(instance_id=instance['id'], pin='P', net_id=net_id,
                    symbol_id=symbol_id, direction=direction,
                    terminal_id=declaration['id'],
                    symbol=deepcopy(resolved)))
            continue
        if symbol_id in MARKERS:
            if len(resolved['pins']) != 1:
                raise ValueError('A power marker must have exactly one drawn pin')
            pin = resolved['pins'][0]['name']
            net_id = terminal_nets.get((instance['id'], pin))
            if net_id is None:
                raise ValueError('An unconnected power marker cannot be assigned a supply role')
            markers.append(dict(instance_id=instance['id'], pin=pin, net_id=net_id,
                                symbol_id=symbol_id))
            continue
        binding = instance.get('netlist', {}).get('binding', {})
        electrical = next((d.get('electrical', {}) for d in project.get('componentDefinitions', [])
                           if d['symbol']['id'] == symbol_id), {})
        kind = binding.get('deviceClass') or electrical.get('deviceClass')
        if not binding or electrical.get('targetPolicy') == 'child-cell':
            raise ValueError(f'Unsupported native instance binding: {instance["id"]}')
        if kind == 'mos':
            polarity = (electrical.get('mosBulkClass') or
                        instance.get('importProvenance', {}).get('modelType', symbol_id)).casefold()
            if polarity not in ('nmos', 'pmos'):
                raise ValueError('MOS polarity must be declared by its native symbol/model')
            prefix, canonical, pins = 'M', polarity, ('D', 'G', 'S')
        elif kind == 'bjt':
            polarity = instance.get('importProvenance', {}).get('modelType', symbol_id).casefold()
            if polarity not in ('npn', 'pnp'):
                raise ValueError('BJT polarity must be declared by its native symbol/model')
            prefix, canonical, pins = 'Q', polarity, ('C', 'B', 'E')
        elif kind == 'vcvs' and {p['name'] for p in resolved['pins']} == {'OUT', 'IN+', 'IN-'}:
            prefix, canonical, pins = 'E', 'opamp', ('OUT', 'IN+', 'IN-')
        elif kind in CLASSES:
            prefix, canonical, pins = CLASSES[kind]
        else:
            raise ValueError(f'Unsupported native device class: {kind!r}')
        visible = {p['name']: p for p in resolved['pins']}
        if set(visible) != set(pins):
            raise ValueError(f'Drawn native pins do not match the supported {canonical} interface')
        for pin in visible.values():
            if pin['at'] != pin['landing']:
                raise ValueError('Off-grid artwork contacts need an explicit landing adapter')
        native_symbol = resolved['definition']
        selected_pins = [dict(name=p, at=deepcopy(visible[p]['at']), direction=visible[p]['direction'],
                              presentation=dict(visibility='visible')) for p in pins]
        canonical_definition = dict(symbol=dict(id=canonical, name=canonical,
            viewBox=deepcopy(native_symbol['viewBox']), pins=selected_pins,
            primitives=deepcopy((resolved.get('variant') or {}).get('primitives', native_symbol['primitives'])),
            variants=[dict(id='adapter-visible', hiddenPinNames=[])], defaultVariantId='adapter-visible'))
        if canonical in definitions and digest({k: definitions[canonical]['symbol'][k] for k in ('pins', 'viewBox')}) != digest({k: canonical_definition['symbol'][k] for k in ('pins','viewBox')}):
            raise ValueError(f'Heterogeneous {canonical} geometries require separate engine symbol types')
        definitions[canonical] = canonical_definition
        engine_id = prefix+str(number)
        nodes = []
        for pin in pins:
            original_net = terminal_nets.get((instance['id'], pin))
            nodes.append(names[original_net] if original_net else 'floating_'+digest((instance['id'], pin))[:12])
        if prefix == 'M':
            bulk = terminal_nets.get((instance['id'], 'B'))
            nodes.append(names[bulk] if bulk else 'bulk_'+digest(instance['id'])[:12])
        if prefix in ('M', 'Q', 'D'):
            model_key = (canonical, digest(binding))
            if model_key not in model_keys:
                model_keys[model_key] = 'layout_model_'+digest(model_key)[:12]
                model_type = {'nmos':'NMOS','pmos':'PMOS','npn':'NPN','pnp':'PNP','diode':'D'}[canonical]
                models.append('.model '+model_keys[model_key]+' '+model_type)
            tail = model_keys[model_key]
        elif prefix == 'E':
            nodes = [nodes[0], '0', nodes[1], nodes[2]]
            tail = '1'
        else:
            params = instance.get('netlist', {}).get('parameters', {})
            value = params.get('dc' if prefix in ('V','I') else 'value', '1')
            tail = value if re.fullmatch(r'[+-]?(?:[0-9.]+)(?:[eE][+-]?[0-9]+)?[a-zA-Z]*', value) else '1'
        parameters = []
        if prefix in ('M', 'Q'):
            for key, value in sorted(instance.get('netlist', {}).get('parameters', {}).items()):
                if key.casefold() in ('w','l','m','nf') and re.fullmatch(r'[0-9.eE+-]+[a-zA-Z]*', value):
                    parameters.append(key.upper()+'='+value)
        statements.append(' '.join([engine_id, *nodes, tail, *parameters]))
        components[engine_id] = dict(instance_id=instance['id'], symbol_id=symbol_id,
                                     canonical=canonical, pins=list(pins))
    if not components:
        raise ValueError('No supported electrical devices in the selected Document')
    for canonical, key in (('ground', 'ground'), ('vdd-port', 'vdd-port')):
        found = snapshot['markerSymbols'].get(key)
        if not found:
            raise ValueError(f'The native resolver lacks {key}')
        definition = deepcopy(found['definition'])
        definition['id'] = canonical
        definitions[canonical] = dict(symbol=definition)
    return dict(protocol_version=PROTOCOL_VERSION, document_id=document['id'],
        expected_revision=document['revision'], project_id=project['id'],
        source=deepcopy(snapshot['source']), components=components, net_names=names,
        markers=markers, ports=ports, definitions=definitions,
        netlist='* Layout-only topology; native model parameters are preserved in the output project.\n'+
                '\n'.join(models+statements+['.end'])+'\n')


def stage_engine(engine_root: Path, folder: Path, profile: dict) -> dict:
    engine_root, folder = engine_root.resolve(), folder.resolve()
    if folder.exists():
        raise ValueError('Engine staging directory must be new')
    folder.mkdir(parents=True)
    files = sorted((engine_root/'netlist2schematic').rglob('*'))
    hashes = {}
    for source in files + sorted(engine_root.glob('*.py')):
        if not source.is_file() or ('__pycache__' in source.parts) or source.suffix not in ('.py', '.json'):
            continue
        relative = source.relative_to(engine_root)
        target = folder/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        hashes[str(relative)] = hashlib.sha256(source.read_bytes()).hexdigest()
    geometry = json.loads((engine_root/'symbol-geometry.json').read_text(encoding='utf-8'))
    original_definitions = engine_root/'analog-canvas-main/packages/components/definitions'
    destination = folder/'analog-canvas-main/packages/components/definitions'
    destination.mkdir(parents=True)
    for symbol in geometry['symbols']:
        definition = profile['definitions'].get(symbol)
        if definition is None:
            definition = json.loads((original_definitions/(symbol+'.json')).read_text(encoding='utf-8'))
        (destination/(symbol+'.json')).write_bytes((json.dumps(definition, ensure_ascii=False, indent=2)+'\n').encode('utf-8'))
        geometry['symbols'][symbol] = canonical_geometry(geometry['symbols'][symbol], definition)
    (folder/'symbol-geometry.json').write_bytes((json.dumps(geometry, ensure_ascii=False, indent=2)+'\n').encode('utf-8'))
    source = folder/'layout-input.cir'
    source.write_bytes(profile['netlist'].encode('utf-8'))
    report = dict(source_root=str(engine_root), source_sha256=hashes,
                  geometry_sha256=digest(geometry), source_code_modified=False)
    (folder/'engine-snapshot.json').write_bytes((json.dumps(report, indent=2)+'\n').encode('utf-8'))
    return report
