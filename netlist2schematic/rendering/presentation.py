"""Read explicit external-port names for clean schematic presentation only.

No routes, device placement, net membership, or electrical semantics change.
SPICE formal ports have no declared direction; their display stays generic.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re

from netlist2schematic.netlist.netlist_core import NetlistError, SNK_ALIASES, SRC_ALIASES, _logical_lines, _partition

VERSION = 'schematic-presentation-v1'
_INPUT_NAME = re.compile(r'^(?:v[_-]?)?(?:in(?:put)?)(?:[pn]|[+\-]|[_-]?[0-9]+|[_-][pn])?$', re.I)
_CONTROL_PINS = frozenset(('G', 'B', 'IN+', 'IN-'))


def _power(net):
    key = str(net).casefold().rstrip('!')
    if key in SRC_ALIASES or key in ('vdd', 'vcc', 'vpwr', 'avdd', 'dvdd'):
        return 'power'
    if key in SNK_ALIASES or key in ('0', 'gnd', 'gnds', 'vss', 'agnd', 'dgnd'):
        return 'ground'
    return None


def _formal_names(tokens):
    result = []
    values = tokens[2:]
    for i, token in enumerate(values):
        if token.casefold().startswith(('params:', 'parameters:')) or '=' in token:
            break
        if i+1 < len(values) and values[i+1].startswith('='):
            break
        result.append(token)
    return result


def prepare_presentation(source, cell, comps, nets):
    """Return JSON rendering hints from selected formals or explicit Vin sources.

    Flat circuits accept only a grounded voltage source whose instance or
    driven-node name explicitly matches Vin/Input naming. V1, bias sources,
    unnamed internal nets, and ungrounded two-terminal sources are not guessed.
    """
    source = Path(source).expanduser().resolve()
    lines = _logical_lines(source)
    top, cells = _partition(lines)
    requested = str(cell).casefold() if cell is not None else None
    if requested is not None and requested not in cells:
        raise NetlistError(f'{source}: unknown subcircuit {cell!r}; available: '
                           + (', '.join(sorted(cells)) or 'none'))
    comp_by_id = {str(comp['id']).casefold(): comp for comp in comps if comp.get('id')}
    actual_nets = {str(name).casefold(): str(name) for name in nets}
    terminals = defaultdict(list)
    for comp in comps:
        for pin, net in (comp.get('nodes') or {}).items():
            key = str(net).casefold()
            actual_nets.setdefault(key, str(net))
            terminals[key].append({'component': comp['id'], 'pin': pin})
    for values in terminals.values():
        values.sort(key=lambda item: (item['component'].casefold(), item['pin']))

    def record(net, name, origin, evidence):
        key = str(net).casefold()
        points = terminals.get(key, [])
        power = _power(net)
        role = power or ('control' if points and all(
            item['pin'] in _CONTROL_PINS for item in points) else 'generic')
        return {'net': actual_nets.get(key, key), 'name': name, 'aliases': [name],
                'source': origin, 'direction': 'input' if origin == 'voltage_source' else 'generic',
                'role': role, 'connected': bool(points), 'terminals': [dict(item) for item in points],
                'evidence': evidence}

    ports, by_net, skipped = [], {}, []
    declaration = None
    if requested is not None:
        control = False
        for line in lines:
            tokens = line.text.split()
            head = tokens[0].casefold()
            if head == '.control':
                control = True
            elif head == '.endc':
                control = False
            elif not control and head == '.subckt' and tokens[1].casefold() == requested:
                declaration = {'path': str(line.path), 'line': line.number, 'cell': tokens[1]}
                for name in _formal_names(tokens):
                    key = name.casefold()
                    if key in by_net:
                        if name not in by_net[key]['aliases']:
                            by_net[key]['aliases'].append(name)
                        continue
                    item = record(key, name, 'formal', dict(declaration))
                    ports.append(item)
                    by_net[key] = item
                break
    else:
        for line in top:
            tokens = line.text.split()
            if not tokens or tokens[0][0].casefold() != 'v' or len(tokens) < 4:
                continue
            comp = comp_by_id.get(tokens[0].casefold())
            if not comp or comp.get('symbol') != 'voltage-source':
                continue
            nodes = comp.get('nodes') or {}
            if (str(nodes.get('+')).casefold(), str(nodes.get('-')).casefold()) != (
                    tokens[1].casefold(), tokens[2].casefold()):
                skipped.append({'component': comp['id'], 'reason': 'source_component_mismatch'})
                continue
            p, n = tokens[1], tokens[2]
            grounded = [_power(p) == 'ground', _power(n) == 'ground']
            if sum(grounded) != 1:
                skipped.append({'component': comp['id'], 'reason': 'no_unique_ground_reference'})
                continue
            driven = n if grounded[0] else p
            pin = '-' if grounded[0] else '+'
            if _power(driven):
                continue
            name = driven if _INPUT_NAME.fullmatch(driven) else (
                tokens[0] if _INPUT_NAME.fullmatch(tokens[0]) else None)
            if name is None:
                continue
            key = driven.casefold()
            evidence = {'path': str(line.path), 'line': line.number,
                        'component': comp['id'], 'pin': pin,
                        'rule': 'explicit_input_name_and_grounded_voltage_source'}
            if key in by_net:
                if name not in by_net[key]['aliases']:
                    by_net[key]['aliases'].append(name)
                continue
            item = record(key, name, 'voltage_source', evidence)
            ports.append(item)
            by_net[key] = item
        ports.sort(key=lambda item: (item['net'].casefold(), item['name'].casefold()))

    labels = {port['net']: {'name': port['name'], 'direction': port['direction'],
                           'role': port['role'], 'source': port['source'],
                           'terminals': port['terminals']}
              for port in ports if port['connected'] and port['role'] not in ('power', 'ground')}
    return {'version': VERSION, 'source_netlist': str(source), 'source_cell': requested,
            'formal_declaration': declaration, 'named_ports': ports,
            'skipped_sources': skipped,
            'render_hints': {'port_labels': labels, 'hide_structure_boxes': True,
                             'hide_metrics': True, 'power_markers': 'existing',
                             'wire_geometry': 'unchanged'}}


def configure_presentation(placement, source, cell, comps, nets, style='legacy'):
    """Configure drawing only; regenerate labels from the verified source."""
    if style not in ('legacy','clean'):
        raise ValueError('presentation must be legacy or clean')
    report={'style':style}
    if style=='clean':report.update(prepare_presentation(source,cell,comps,nets))
    placement.presentation_style=style
    placement.presentation_ports=report.get('render_hints',{}).get('port_labels',{})
    placement.presentation_supply_names={p['net']:p['name'] for p in report.get('named_ports',())
        if p['role'] in ('power','ground')}
    return report
