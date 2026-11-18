"""Independent sources as two-terminal layout guests, never electrical shorts.

Hosts and display order come from real channel/control ports, not the source
name, voltage polarity, DC magnitude or waveform. Ambiguous/unanchored sources
stay in the original core. The existing series/parallel geometry keeps every
leaf, original node, polarity and arrow.
"""
from copy import deepcopy

from netlist2schematic.common.passive_coordinates import SOURCES
from netlist2schematic.plugins.passive_skeleton.modules import build_modules, face

ACTIVE = frozenset(('nmos', 'pmos', 'npn', 'pnp'))


def source_modules(par, comps, nets):
    """Resolve only source modules that have a real MOS/BJT service node.

    Rebuild after rejecting sources: a rejected source becomes an external
    boundary again, so a neighboring R/C/L retains its original plan.
    """
    parts = {c['id']: c for c in comps}
    sources = {n for n, c in parts.items() if c['symbol'] in SOURCES}
    selected = set(sources)
    skipped = []
    while selected:
        plan = build_modules(par, comps, nets, source_ids=selected)
        accepted = set()
        for group in plan['groups']:
            members = selected.intersection(group['members'])
            if not members:
                continue
            hosts = [t for ts in group['anchors'].values() for t in ts]
            if any(parts[n]['symbol'] in ACTIVE for n, _ in hosts):
                accepted.update(members)
        if accepted == selected:
            break
        for name in sorted(selected-accepted):
            rejection = next((r['reason'] for r in plan['skipped'] if name in r['members']),
                             'no_unambiguous_two_port_active_attachment')
            skipped.append(dict(device=name, reason=rejection, retained_in_core=True))
        selected = accepted
    else:
        plan = build_modules(par, comps, nets)
    plan['source_layout'] = dict(version='independent-source-modules-v1',
        guests=sorted(selected), retained_in_core=sorted(sources-selected), skipped=skipped,
        polarity_preserved=True, electrical_node_merges=[])
    return plan


def inline_bridges(devices, comps, nets, groups):
    """Unique series source paths between opposite-role channel hosts only."""
    from netlist2schematic.recognition.device_leveling import in_terminal, out_terminal
    from netlist2schematic.plugins.layout_shared.branch_levels import _rail
    parts = {c['id']: c for c in comps}
    accepted, skipped = [], []
    for group in groups:
        sources = [n for n in group['members'] if parts[n]['symbol'] in SOURCES]
        if not sources:
            continue
        tree = group['module']
        if tree['kind'] not in ('part', 'series') or (
                tree['kind'] == 'series' and any(c['kind'] != 'part' for c in tree['children'])):
            skipped.append(dict(members=group['members'], reason='source_module_not_unique_series'))
            continue
        hosts, roles, controls = {}, {}, {}
        for net in group['ends']:
            channels, taps, others = [], [], []
            for name, pin in nets[net]:
                if name in group['members']:
                    continue
                device = devices[name]
                if device['cls'] not in ('MOS', 'BJT'):
                    others.append((name, pin))
                elif pin in (('D', 'S') if device['cls'] == 'MOS' else ('C', 'E')):
                    channels.append([name, pin])
                elif pin == ('G' if device['cls'] == 'MOS' else 'B'):
                    taps.append([name, pin])
                else:
                    others.append((name, pin))
            if _rail(net) or len(channels) != 1 or others:
                break
            name, pin = channels[0]
            hosts[net], controls[net] = channels[0], taps
            roles[net] = 'out' if out_terminal(devices[name]) == net else (
                'in' if in_terminal(devices[name]) == net else None)
        else:
            if set(roles.values()) == {'out', 'in'}:
                start = next(n for n, role in roles.items() if role == 'out')
                oriented = face(tree, start)
                leaves = [oriented] if oriented['kind'] == 'part' else oriented['children']
                path = [leaves[0]['ends'][0]] + [leaf['ends'][1] for leaf in leaves]
                accepted.append(dict(id='source:'+'|'.join(sorted(group['members'])),
                    members=[leaf['device'] for leaf in leaves], path=path,
                    start=path[0], end=path[-1], start_role='out', end_role='in',
                    conduction_ports=deepcopy(hosts), control_taps=controls,
                    source_layout=True, layout_only=True, dc_path=False,
                    evidence='unique_channel_hosts_across_independent_source_module'))
                continue
        skipped.append(dict(members=group['members'], reason='source_has_no_unique_opposite_channel_hosts'))
    return accepted, skipped
