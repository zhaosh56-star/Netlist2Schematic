"""Export exact geometry from an isolated, unmodified engine snapshot."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine-root', type=Path, required=True)
    parser.add_argument('--placement', type=Path, required=True)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(args.engine_root.resolve()))
    from netlist2schematic.common.sa_saved import load_placement, saved_routing_options
    from netlist2schematic.routing.sa_routing import validate_final_routes
    from netlist2schematic.routing.routing_rules import build_scene
    from netlist2schematic.common.supply_markers import symbol_definition
    data, source, par, placement, comps, nets, index = load_placement(args.placement)
    options = saved_routing_options(data)
    routes, metrics, checked = validate_final_routes(par, nets, placement, index,
        list(data['routes'].items()), options=options)
    if not checked['valid']:
        raise ValueError('Core layout failed independent verification')
    scene = build_scene(par, nets, placement, index, options=options)
    profile = json.loads(args.profile.read_text(encoding='utf-8'))
    objects, power = [], []
    for component in comps:
        cid = component['id']
        number = index[cid]
        x, y = placement.pos_of(number)
        geometry = par.GEO_DOC[component['symbol']]['orientations'][placement.orient[number]]
        objects.append(dict(engine_id=cid, instance_id=profile['components'][cid]['instance_id'],
            position=dict(x=x, y=y), rotation=geometry['rotation'], mirror=geometry['mirror'],
            pins=[dict(name=pin, net=net, at=list(placement.pin_world(number, pin)),
                       direction=placement.pin_dir(number, pin))
                  for pin, net in component['nodes'].items()]))
    if scene.power_connections is not None:
        for record in scene.power_connections:
            symbol = record['symbol']
            local = par.GEO_DOC[symbol]['orientations'][0]['pins'][0]['at']
            anchor = record['anchor']
            power.append(dict(component_id=record['component_id'], pin=record['pin'],
                net=record['net'], symbol=symbol, anchor=anchor,
                origin=[anchor[0]-local['x'], anchor[1]-local['y']], segments=record['segments']))
    else:
        negative = {(c['component'], c['pin'], c['net']): c for c in scene.negative_connections}
        for net, members in nets.items():
            symbol = par.power_marker_sym(net)
            if not symbol:
                continue
            for cid, pin in members:
                point = list(placement.pin_world(index[cid], pin))
                if symbol == 'vee-port':
                    record = negative[index[cid], pin, net]
                    anchor, segments = record['anchor'], record['segments']
                    origin = anchor
                else:
                    origin = [point[0], point[1]+(-par.STUB_LEN if symbol == 'vdd-port' else par.STUB_LEN)]
                    local = par.GEO_DOC[symbol]['orientations'][0]['pins'][0]['at']
                    anchor = [origin[0]+local['x'], origin[1]+local['y']]
                    # The native Route ends at the real symbol pin, rather than
                    # extending through the glyph to its drawing origin.
                    segments = [[point, anchor]] if point != anchor else []
                power.append(dict(component_id=cid, pin=pin, net=net, symbol=symbol,
                                  anchor=anchor, origin=origin, segments=segments))
    result = dict(protocolVersion=1, components=objects, routes=dict(routes),
        power=power, metrics=metrics, validation=checked,
        negativeSymbol=symbol_definition(),
        selected_candidate=data.get('quality', {}).get('selected_candidate'),
        routing_stage=data.get('routing_stage', 'normal'),
        candidate_pipeline=data.get('candidate_pipeline'))
    args.output.write_bytes((json.dumps(result, ensure_ascii=False, indent=2)+'\n').encode('utf-8'))
    print(json.dumps(dict(status='valid', components=len(objects), power_markers=len(power),
                         selected_candidate=result['selected_candidate'], metrics=metrics)))


if __name__ == '__main__':
    main()
