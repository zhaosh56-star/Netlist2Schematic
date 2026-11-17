#!/usr/bin/env python3
"""Redraw a saved SA placement as a black-and-white SVG (including legacy JSON)."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from netlist2schematic.rendering.sa_render import render_svg, segs_to_paths
from netlist2schematic.routing.sa_routing import final_route, validate_final_routes
from netlist2schematic.common.sa_saved import (load_placement, saved_routing_options, is_bus_layout,
                      check_source_hash, BUS_ROUTER)


def render(jpath, out=None, *, analog_canvas_root=None, presentation=None):
    source_json = Path(jpath).resolve()
    data, source_netlist, par, pl, comps, nets, comp_index = load_placement(
        source_json, analog_canvas_root=analog_canvas_root)
    options = saved_routing_options(data)
    if 'routes' in data:
        routed, metrics, validation = validate_final_routes(
            par, nets, pl, comp_index, list(data['routes'].items()), options=options)
    else:
        if is_bus_layout(data):
            from netlist2schematic.recovery.fallback_bus import reroute_fallback
            routed, metrics, validation = reroute_fallback(
                par, pl, comps, nets, comp_index, data, routing_options=options)
        else:
            routed, metrics, validation = final_route(par, nets, pl, comp_index, options=options)
    if is_bus_layout(data):
        validation['router'] = BUS_ROUTER
        pl.routing_stage = 'bus_fallback'
    destination = Path(out) if out else source_json.with_name(
        source_json.name.replace('-placement.json', '-clean.svg'))
    if destination.resolve() == source_json:
        raise ValueError('Output SVG must differ from the source placement JSON')
    if destination.resolve() == Path(source_netlist).resolve():
        raise ValueError('Output SVG must differ from the source netlist')
    check_source_hash(source_netlist, data.get('source_sha256'))
    if presentation is not None:
        from netlist2schematic.rendering.presentation import configure_presentation
        configure_presentation(pl,source_netlist,data.get('source_cell'),comps,nets,presentation)
        check_source_hash(source_netlist,data.get('source_sha256'))
    render_svg(par, pl, comps, nets, comp_index, routed, metrics, destination,
               clean=True,
               structs=(data.get('layout_evidence', {}).get('structures', [])
                        if data.get('structure_display') == 'validated' else data.get('structs', [])),
               validation=validation)
    print(f'Wrote {destination.resolve()}  {metrics}', flush=True)
    return metrics


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('placement_json')
    parser.add_argument('output_svg', nargs='?')
    parser.add_argument('--analog-canvas-root')
    parser.add_argument('--presentation',choices=('legacy','clean'),
                        help='Override drawing style without moving devices or rerouting')
    args = parser.parse_args(argv)
    try:
        render(args.placement_json, args.output_svg, analog_canvas_root=args.analog_canvas_root,
               **({'presentation':args.presentation} if args.presentation is not None else {}))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, f'Error: {exc}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
