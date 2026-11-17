"""Independent electrical and geometric verification of failure-only labels.

This representation has no fictitious complete-route baseline. Physical groups
are reconstructed from real pins; same-net labels supply the missing links.
The ordinary complete-wire verifier and display-only label plugin stay strict.
"""
from copy import deepcopy
from dataclasses import asdict
import json
from math import isfinite
from time import perf_counter

from netlist2schematic.plugins.net_labels.net_label_validation import (
    canonical_routes, covered, display_routes, label_position, lead_error,
    local_groups, point, text_error, wire_metrics)
from netlist2schematic.rendering.sa_render import _text_box, net_label_render_hints
from netlist2schematic.routing.routing_rules import (
    EPS, analyze_routes, build_scene, check_deadline, on_segment, static_shape_conflicts)

VERSION = 'labeled-recovery-v1'
REPRESENTATION = 'wires_and_labels'


def is_labeled_layout(data):
    validation = data.get('validation')
    return (data.get('route_representation') == REPRESENTATION
            or data.get('routing_stage') == 'labeled_recovery'
            or isinstance(validation, dict) and validation.get('router') == VERSION)


def terminals(scene, comps):
    return {net: [dict(component=comps[p.component]['id'], pin=p.pin, point=list(p.point))
                  for p in pins] for net, pins in scene.pins.items()}


def group_records(scene, routes, comps, deadline=None):
    identities = terminals(scene, comps)
    by_net, records = {}, []
    for net in sorted(scene.pins):
        _, groups = local_groups(scene, net, routes[net], identities, deadline)
        if not groups or any(not group['terminals'] for group in groups):
            raise ValueError('Labeled recovery contains a terminal-free island')
        by_net[net] = groups
        records.extend(dict(net=net, id=group['id'],
                            terminals=[list(term) for term in group['terminals']])
                       for group in groups)
    return by_net, records


def _same(first, second):
    return json.loads(json.dumps(first, sort_keys=True)) == json.loads(json.dumps(second, sort_keys=True))


def drawing_hints(par, placement, comps, nets, index, routes, scene, groups):
    hints=net_label_render_hints(par,placement,comps,nets,index,list(routes.items()),
                                {'parameters':asdict(scene.options)})
    tagged={net for net,items in groups.items() if len(items)>1}
    # Replaced formal-port text is not drawn, and must not invisibly exclude
    # the recovery label which now expresses that same original network.
    hints['port_text']=[label for label in hints['port_text'] if label['net'] not in tagged]
    hints['text_obstacles']=[label['bounds'] for field in ('component_text','supply_text','port_text')
                             for label in hints[field]]
    return hints


def validate_view(par, nets, placement, index, view, *, options=None, deadline=None):
    """Verify a partial physical forest plus labels, never saved success flags."""
    started = perf_counter()
    if not isinstance(view, dict) or view.get('version') != VERSION:
        raise ValueError('Unsupported labeled-recovery representation')
    scene = build_scene(par, nets, placement, index, options=options, deadline=deadline)
    routes = canonical_routes(view.get('base_routes', {}), deadline)
    if set(routes) != set(scene.pins):
        raise ValueError('Labeled recovery changed the visible signal-network set')
    for net, pins in scene.pins.items():
        for pin in pins:
            if pin.point != pin.escape and not covered(pin.stub, routes[net]):
                raise ValueError('Labeled recovery omitted or moved a fixed pin exit')
    by_net, groups = group_records(scene, routes, placement.comps, deadline)
    if not _same(view.get('groups'), groups):
        raise ValueError('Labeled recovery terminal groups do not match the source pins')
    baseline = analyze_routes(list(routes.items()), scene, deadline=deadline)
    shapes = [fault for _, fault in static_shape_conflicts(scene, deadline)]
    if baseline['violations'] or shapes:
        raise ValueError('Labeled recovery physical geometry is invalid: ' +
                         str((baseline['violations'] + shapes)[:4]))
    hints = drawing_hints(par,placement,placement.comps,nets,index,routes,scene,by_net)
    # Text and symbol positions are reconstructed, not trusted output metadata.
    if not _same(view.get('render_hints'), hints):
        raise ValueError('Labeled recovery drawing hints do not match the current layout')
    expected = {(net, group['id']) for net, items in by_net.items() if len(items) > 1
                for group in items}
    labels = view.get('labels')
    if not isinstance(labels, list):
        raise ValueError('Labeled recovery labels must be a list')
    seen, previous = set(), []
    for label in labels:
        check_deadline(deadline, 'labeled_recovery_labels')
        if not isinstance(label, dict):
            raise ValueError('Malformed recovery label')
        net = label.get('net')
        identity = (net, label.get('group_id'))
        if identity not in expected or identity in seen or label.get('text') != net:
            raise ValueError('Missing, duplicate or wrong-network recovery label')
        group = next(g for g in by_net[net] if g['id'] == identity[1])
        anchor, tip = point(label.get('anchor')), point(label.get('tip'))
        if anchor not in group['points'] and not any(on_segment(anchor, a, b)
                                                    for a, b in group['segments']):
            raise ValueError('Recovery label does not touch its declared physical group')
        delta = tip[0]-anchor[0], tip[1]-anchor[1]
        if (delta[0] == 0) == (delta[1] == 0):
            raise ValueError('Recovery label requires a nonzero orthogonal lead')
        direction = tuple(0 if value == 0 else (1 if value > 0 else -1) for value in delta)
        x, y, align = label_position(tip, direction)
        if any(type(label.get(k)) not in (int, float) or not isfinite(label[k])
               for k in ('x', 'y')) or (label['x'], label['y'], label.get('anchor_style')) != (x, y, align):
            raise ValueError('Recovery label text position was altered')
        bounds = _text_box(x, y, net, align, size=12)
        if not _same(label.get('bounds'), list(bounds)):
            raise ValueError('Recovery label text bounds were altered')
        error = lead_error(scene, net, anchor, tip, routes, previous)
        error = error or text_error(scene, bounds, routes, previous,
                                    hints['text_obstacles'], own_label=label)
        if error:
            raise ValueError('Unsafe recovery label: '+error)
        seen.add(identity)
        previous.append(label)
    if seen != expected:
        raise ValueError('A disconnected physical group is missing its same-net label')
    visible = display_routes(view)
    analysis = analyze_routes(visible, scene, deadline=deadline)
    if analysis['violations']:
        raise ValueError('Recovery label leads violate geometry: '+str(analysis['violations'][:4]))
    metrics = wire_metrics(dict(visible), analysis, deadline)
    if scene.power_connections is not None:
        from netlist2schematic.routing.routing_rules import merge_runs
        metrics['power_wirelength'] = sum(abs(a[0]-b[0])+abs(a[1]-b[1])
            for net in scene.power_nets for a,b in merge_runs(scene.fixed_segments.get(net,())))
    if 'display_metrics' in view and not _same(view['display_metrics'], metrics):
        raise ValueError('Saved recovery display metrics do not match its actual wires')
    check_deadline(deadline, 'labeled_recovery_final_validation')
    validation = dict(valid=True, router=VERSION, connectivity_mode=REPRESENTATION,
        wire_only_complete=not expected, signal_nets=len(scene.pins),
        label_count=len(labels), labeled_nets=len({net for net, _ in expected}),
        original_pins_preserved=True, fixed_exits_preserved=True,
        groups_recomputed=True, parameters=asdict(scene.options), violations=[],
        power_connections_checked=analysis['power_connections_checked'],
        routing_seconds=round(perf_counter()-started, 6))
    return visible, metrics, validation


def verify_saved(par, nets, placement, index, data, *, options=None, deadline=None):
    from netlist2schematic.plugins.net_labels.net_labels import _check_files, _sources, _resource_hashes
    report = data.get('labeled_recovery', {})
    if not isinstance(report, dict) or report.get('version') != VERSION:
        raise ValueError('Missing labeled-recovery metadata')
    dependencies = report.get('dependencies', {})
    _check_files(dependencies.get('sources'), 'recovery source files')
    _check_files(dependencies.get('resources'), 'recovery symbol resources')
    if not _same(_sources(data['source_netlist']), dependencies['sources']):
        raise ValueError('Recovery source/include file set changed')
    if not _same(_resource_hashes(par, placement.comps), dependencies['resources']):
        raise ValueError('Recovery symbol-resource set changed')
    routed, metrics, validation = validate_view(par, nets, placement, index, report.get('view'),
                                               options=options, deadline=deadline)
    if 'routes' in data and not _same(canonical_routes(data['routes']), canonical_routes(dict(routed))):
        raise ValueError('Saved visible routes disagree with the recovery label plan')
    return routed, metrics, validation
