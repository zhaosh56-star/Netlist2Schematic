"""Optional shared-VDD view of a verified schematic, without moving devices.

Run with ``python -m netlist2schematic.plugins.power_rails.power_rails``.
Original placements, signal routes and quality scores retain their meaning.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from statistics import median
import tempfile
from time import perf_counter
import xml.etree.ElementTree as ET

from netlist2schematic.common.sa_common import ROOT, read_json, resource_root, write_json
from netlist2schematic.plugins.power_rails.rail_core import (
    VERSION, build_view, hints_for, validate_view, _group_geometry,
)

FORMAT = 'shared-vdd-view-v1'


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _file_hash(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def _dependencies(source, par, comps):
    sources = {}
    pattern = re.compile(r'''^\.(?:include|inc)\s+(?:"([^"]+)"|'([^']+)'|(\S+))\s*$''', re.I)
    def visit(path):
        path = Path(path).resolve()
        if str(path) in sources:
            return
        sources[str(path)] = _file_hash(path)
        for raw in path.read_text(encoding='utf-8-sig').splitlines():
            text = raw.strip()
            if not text or text.startswith('*'):
                continue
            match = pattern.fullmatch(re.split(r'[$;]', text, maxsplit=1)[0].strip())
            if match:
                target = Path(next(v for v in match.groups() if v is not None))
                visit(target if target.is_absolute() else path.parent/target)
    visit(source)
    definitions = resource_root()/'packages'/'components'/'definitions'
    symbols = {c['symbol'] for c in comps} | {'vdd-port', 'ground'}
    from netlist2schematic.common.supply_markers import resource_files
    files = [Path(par.GEO), *(definitions/(symbol+'.json') for symbol in sorted(symbols)),
             *resource_files(par, comps)]
    return {'source_files': dict(sorted(sources.items())),
            'resource_files': {str(p.resolve()): _file_hash(p) for p in files}}


def _check_dependencies(dependencies):
    for kind in ('source_files', 'resource_files'):
        files = dependencies.get(kind)
        if not isinstance(files, dict) or not files:
            raise ValueError('Missing shared-supply source/resource fingerprints')
        for path, expected in files.items():
            if _file_hash(path) != expected:
                raise ValueError(f'Shared-supply dependency changed: {path}')


def _load_context(path):
    from netlist2schematic.common.sa_saved import load_placement, saved_routing_options
    from netlist2schematic.routing.sa_routing import validate_final_routes
    from netlist2schematic.routing.routing_rules import build_scene, _escape, _inflate
    from netlist2schematic.rendering.presentation import configure_presentation
    from netlist2schematic.rendering.sa_render import net_label_render_hints
    data, source, par, placement, comps, nets, index = load_placement(path)
    if not isinstance(data.get('routes'), dict):
        raise ValueError('Shared VDD needs saved complete routes; no layout or A* is run')
    options = saved_routing_options(data)
    routed, metrics, validation = validate_final_routes(
        par, nets, placement, index, list(data['routes'].items()), options=options)
    scene = build_scene(par, nets, placement, index, options=options)
    if scene.power_connections is not None:
        raise ValueError('First-version shared VDD preserves custom power_connections; no merging is attempted')
    configure_presentation(placement, source, data.get('source_cell'), comps, nets, 'clean')
    hints = net_label_render_hints(par, placement, comps, nets, index, routed, validation)
    terminals = []
    bodies = dict(scene.body_rects)
    for net, members in nets.items():
        symbol = par.power_marker_sym(net)
        if not symbol:
            continue
        for cid, pin in members:
            component = index[cid]
            point = tuple(placement.pin_world(component, pin))
            direction = tuple(par.DIR_VEC[placement.pin_dir(component, pin)])
            tip = (point[0], point[1]+(-par.STUB_LEN if symbol == 'vdd-port' else par.STUB_LEN))
            segments = None
            if symbol == 'vee-port':
                from netlist2schematic.common.supply_markers import negative_connection
                record = negative_connection(par, placement, component, pin, net,
                    halo=options.halo, grid_step=options.grid_step)
                tip, segments = record['anchor'], record['segments']
            b = par.GEO_DOC[symbol]['orientations'][0]['viewBoxAABB']
            terminals.append(dict(component_id=cid, component=component, pin=pin, net=net, symbol=symbol,
                point=point, direction=direction, escape=_escape(point, direction, bodies[component], options.grid_step),
                **({'segments': segments} if segments is not None else {}),
                stub=(point, tip), marker_rect=_inflate((tip[0]+b['x'], tip[1]+b['y'],
                    tip[0]+b['x']+b['width'], tip[1]+b['y']+b['height']), options.halo)))
    geom = par.GEO_DOC['vdd-port']['orientations'][0]
    p, b = geom['pins'][0], geom['viewBoxAABB']
    if p['direction'] != 'south':
        raise ValueError('Shared-VDD symbol must have its documented south-facing input')
    geometry = dict(pin=(p['at']['x'],p['at']['y']), box=(b['x'],b['y'],b['x']+b['width'],b['y']+b['height']))
    gaps = [b-a for attr in ('column_x','row_y') for a,b in
            zip(getattr(placement,attr,()),getattr(placement,attr,())[1:]) if b>a]
    return dict(data=data, source=source, par=par, placement=placement, comps=comps, nets=nets,
                index=index, routed=routed, metrics=metrics, validation=validation,
                scene=scene, terminals=terminals, geometry=geometry, hints=hints,
                scale=median(gaps) if gaps else par.CELL)


def _validate(context, view):
    return validate_view(context['scene'], context['routed'], context['terminals'],
        context['geometry'], view, context['hints'], scale=context['scale'])


def _overlay_svg(path, context, view):
    from netlist2schematic.rendering.sa_render import _clean_text
    from netlist2schematic.routing.routing_rules import canonical_topology
    par, scene = context['par'], context['scene']
    ns = 'http://www.w3.org/2000/svg'
    ET.register_namespace('', ns)
    tree = ET.parse(path)
    root = tree.getroot()
    root.set('data-power-expression', 'shared-local-vdd')
    layer = ET.Element('{'+ns+'}g', {'data-power-rail-layer': VERSION})
    ports = {(p['component_id'],p['pin']):p for p in context['terminals']}
    bounds = []
    fmt = par.fmt
    for group in view['groups']:
        shape = _group_geometry(group, ports, context['geometry'], scene.options, context['scale'])
        node = ET.SubElement(layer, '{'+ns+'}g', {'data-power-rail-net':group['net']})
        for a,b in shape['segments']:
            ET.SubElement(node,'{'+ns+'}polyline',dict(points=f'{fmt(a[0])},{fmt(a[1])} {fmt(b[0])},{fmt(b[1])}',
                fill='none',stroke='#111',**{'stroke-width':fmt(scene.options.wire_width)}))
            bounds.extend(((*a,*a),(*b,*b)))
        for x,y in canonical_topology(shape['segments'])['junction_points']:
            ET.SubElement(node,'{'+ns+'}circle',dict(cx=fmt(x),cy=fmt(y),r='3',fill='#111'))
        x,y=shape['origin']
        svg = ('<g xmlns="'+ns+'" data-shared-power-symbol="vdd-port" transform="translate('+fmt(x)+' '+fmt(y)+')">'+
               par.svg_symbol_parts(par.effective_symbol(par.load_def('vdd-port')),0,'none')+'</g>')
        node.append(ET.fromstring(svg))
        text = _clean_text(group['label'],fmt,kind='shared-supply')
        node.append(ET.fromstring('<g xmlns="'+ns+'">'+text+'</g>'))
        bounds.extend((shape['rect'],group['label']['bounds']))
    root.insert(1,layer)  # Real supply geometry behind unchanged signal/device layers.
    if bounds:
        x,y,w,h=map(float,root.attrib['viewBox'].split())
        left=min(x,min(b[0] for b in bounds)-24); top=min(y,min(b[1] for b in bounds)-24)
        right=max(x+w,max(b[2] for b in bounds)+24); bottom=max(y+h,max(b[3] for b in bounds)+24)
        root.set('viewBox',' '.join(map(fmt,(left,top,right-left,bottom-top))))
        root.set('width',fmt(right-left));root.set('height',fmt(bottom-top))
        background=root.find('{'+ns+'}rect')
        if background is not None:
            for key,value in zip(('x','y','width','height'),(left,top,right-left,bottom-top)):
                background.set(key,fmt(value))
    tree.write(path,encoding='utf-8',xml_declaration=False)


def _publish(manifest, context, out_dir, *, protected=(), input_hashes=None):
    from netlist2schematic.rendering.sa_render import render_svg
    destination=Path(out_dir).expanduser().resolve()/manifest['fingerprint'][:16]
    base=manifest['base']
    if not isinstance(base,str) or Path(base).name!=base or base in ('','.','..'):
        raise ValueError('Invalid shared-supply artifact basename')
    names=dict(json=base+'-power-rails.json',clean_svg=base+'-power-rails-clean.svg',
               color_svg=base+'-power-rails-color.svg')
    protected={Path(p).resolve() for p in protected}
    if any((destination/name).resolve() in protected for name in names.values()):
        raise ValueError('Shared-supply output cannot overwrite its input')
    manifest=json.loads(json.dumps(manifest,ensure_ascii=False,allow_nan=False))
    manifest['artifacts']={key:str(destination/name) for key,name in names.items()}
    groups=manifest['view']['groups']
    hidden={tuple(p) for group in groups for p in group['ports']}
    display_nets={net:[member for member in members if tuple(member) not in hidden]
                  for net,members in context['nets'].items()}
    display_nets={net:members for net,members in display_nets.items() if members}
    hints=hints_for(context['hints'],groups)
    destination.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.power-rail-stage-',dir=destination) as folder:
        stage=Path(folder)
        for clean,key in ((True,'clean_svg'),(False,'color_svg')):
            render_svg(context['par'],context['placement'],context['comps'],display_nets,context['index'],
                context['routed'],context['metrics'],stage/names[key],clean=clean,
                validation=context['validation'],presentation_hints=hints)
            _overlay_svg(stage/names[key],context,manifest['view'])
        write_json(stage/names['json'],manifest)
        _check_dependencies(manifest['dependencies'])
        for path,expected in (input_hashes or {}).items():
            if _file_hash(path)!=expected:
                raise ValueError('Shared-supply input changed during rendering')
        for name in names.values():
            os.replace(stage/name,destination/name)
    return manifest


def generate(placement_json, out_dir=None, *, seconds=5):
    """Verify a saved native result, then publish an independent VDD view."""
    path=Path(placement_json).expanduser().resolve()
    before=_file_hash(path);started=perf_counter()
    context=_load_context(path)
    dependencies=_dependencies(context['source'],context['par'],context['comps'])
    view=build_view(context['scene'],context['routed'],context['terminals'],context['geometry'],
                    context['hints'],scale=context['scale'],seconds=seconds)
    view['validation']=_validate(context,view)
    original=deepcopy(context['data'])
    # Also bind geometry and labels themselves: redraw never reselects rails.
    fingerprint=_digest(dict(version=VERSION,original=original,dependencies=dependencies,seconds=seconds))
    base=path.stem.removesuffix('-placement')
    if len(base)>56:
        base=base[:40]+'-'+sha256(base.encode('utf-8')).hexdigest()[:12]
    manifest=dict(format=FORMAT,version=VERSION,base=base,fingerprint=fingerprint,
        original=original,original_placement=str(path),original_file_sha256=before,
        resolved_source_netlist=str(context['source']),
        dependencies=dependencies,policy=dict(seconds=seconds),view=view,view_sha256=_digest(view),
        verified_full_metrics=context['metrics'],elapsed_seconds=round(perf_counter()-started,6),
        note='Only supply presentation changes; native signal routes and quality are preserved.')
    if _file_hash(path)!=before:
        raise ValueError('Original placement changed during shared-supply planning')
    destination=Path(out_dir) if out_dir else ROOT/'output'/'power-rails'/base
    return _publish(manifest,context,destination,protected=(path,context['source']),input_hashes={path:before})


def redraw(manifest_json, out_dir=None):
    """Recheck source, resources and saved real connections, without replanning."""
    path=Path(manifest_json).expanduser().resolve();before=_file_hash(path)
    manifest=read_json(path)
    if manifest.get('format')!=FORMAT or manifest.get('version')!=VERSION:
        raise ValueError('Unsupported shared-VDD manifest')
    _check_dependencies(manifest.get('dependencies',{}))
    expected=_digest(dict(version=VERSION,original=manifest['original'],
        dependencies=manifest['dependencies'],seconds=manifest['policy']['seconds']))
    if expected!=manifest.get('fingerprint') or _digest(manifest['view'])!=manifest.get('view_sha256'):
        raise ValueError('Shared-supply snapshot fingerprint mismatch')
    original=deepcopy(manifest['original'])
    if not original.get('source_netlist'):
        original['source_netlist']=manifest['resolved_source_netlist']
    source=Path(original.get('source_netlist',''))
    if not source.is_absolute():
        original['source_netlist']=str((Path(manifest['original_placement']).parent/source).resolve())
    with tempfile.TemporaryDirectory(prefix='power-rail-restore-') as folder:
        snapshot=Path(folder)/'placement.json';write_json(snapshot,original)
        context=_load_context(snapshot)
    if _dependencies(context['source'],context['par'],context['comps'])!=manifest['dependencies']:
        raise ValueError('Shared-supply dependency list is incomplete')
    checked=_validate(context,manifest['view'])
    if checked!=manifest['view'].get('validation') or context['metrics']!=manifest.get('verified_full_metrics'):
        raise ValueError('Shared-supply metrics differ from independent verification')
    destination=Path(out_dir) if out_dir else path.parent.parent/(manifest['base']+'-redrawn')
    return _publish(manifest,context,destination,protected=(path,context['source']),input_hashes={path:before})


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    inputs=parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--placement');inputs.add_argument('--redraw')
    parser.add_argument('--out-dir')
    args=parser.parse_args(argv)
    try:
        result=redraw(args.redraw,args.out_dir) if args.redraw else generate(args.placement,args.out_dir)
    except (ValueError,OSError,KeyError,TypeError) as exc:
        parser.exit(2,f'Shared VDD failed; original result preserved: {exc}\n')
    print(f"Shared VDD: {result['view']['status']}; {result['view']['validation']['markers_removed']} fewer markers; {result['artifacts']['json']}")
    return 0


if __name__=='__main__':
    raise SystemExit(main())
