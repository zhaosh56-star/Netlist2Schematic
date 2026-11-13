"""Physical D-outlet preference for explicitly proved passive load banks.

This optional closing-stage measurement is separate from the phase-two passive
objective. It changes no placement, source polarity, symbol or routing state.
"""
from __future__ import annotations

from collections import defaultdict
from math import isfinite

from netlist2schematic.netlist.netlist_core import SRC_ALIASES, SNK_ALIASES

VERSION = 'readability-bank-outlet-v1'
UNIT = 100.0
_RLC = frozenset(('resistor', 'inductor', 'capacitor'))
_CHANNEL = {'nmos': frozenset(('D', 'S')), 'pmos': frozenset(('D', 'S')),
            'npn': frozenset(('C', 'E')), 'pnp': frozenset(('C', 'E'))}
_SOURCE = frozenset(('current-source', 'voltage-source'))
_VECTORS = {'north': (0, -1), 'south': (0, 1), 'west': (-1, 0), 'east': (1, 0)}


def _rail_role(net):
    value = str(net).casefold()
    return 'supply' if value in SRC_ALIASES else 'return' if value in SNK_ALIASES else None


def _physical_point(pl, index, name, pin):
    point = tuple(pl.pin_world(index[name], pin))
    if len(point) != 2 or not all(type(value) in (int, float) and isfinite(value) for value in point):
        raise ValueError('invalid physical pin coordinates')
    return point


def measure_bank_outlet(pl, comps, index, plan):
    """Return mean backward pin projection in 100-pixel units.

    A measured bank has exact equal endpoints, includes R or L, and connects a
    verified opposite rail to one MOS drain. Gates at the drain junction are
    allowed, while another channel terminal, source or driven output is not.
    Pure capacitive shunts deliberately receive no outlet-side preference.

    ``count`` is measured banks; ``planned_count`` is proved applicable banks.
    No evidence, or incomplete physical coverage, produces ``error=None``.
    """
    by_id = {row['id']: row for row in comps}
    incidence = defaultdict(list)
    for row in comps:
        for pin, net in row.get('nodes', {}).items():
            incidence[net].append((row['id'], pin))
    records, skipped, planned_count, seen = [], [], 0, set()

    def skip(pattern, reason, **details):
        skipped.append(dict(id=pattern.get('id'), members=list(pattern.get('members', ())),
                            reason=reason, **details))

    for pattern in plan.get('patterns', ()):
        if pattern.get('kind') != 'parallel_bank':
            continue
        names = list(pattern.get('members', ()))
        if len(names) < 2 or len(set(names)) != len(names) or any(name not in by_id for name in names):
            skip(pattern, 'invalid_bank_members')
            continue
        identity = tuple(sorted(names))
        if identity in seen:
            continue
        seen.add(identity)
        if any(by_id[name].get('symbol') not in _RLC for name in names):
            skip(pattern, 'non_rlc_member')
            continue
        if not any(by_id[name]['symbol'] in ('resistor', 'inductor') for name in names):
            skip(pattern, 'capacitive_shunt_bank')
            continue
        endpoint_sets = [set(by_id[name].get('nodes', {}).values()) for name in names]
        ends = endpoint_sets[0]
        if (len(ends) != 2 or any(values != ends for values in endpoint_sets)
                or set(pattern.get('nets', ends)) != ends):
            skip(pattern, 'bank_endpoint_mismatch')
            continue
        rails = [net for net in ends if _rail_role(net)]
        if len(rails) != 1:
            skip(pattern, 'bank_requires_one_verified_rail')
            continue
        rail = rails[0]
        signal = next(net for net in ends if net != rail)
        channel, driven = [], []
        for name, pin in incidence[signal]:
            symbol = by_id[name].get('symbol')
            if pin in _CHANNEL.get(symbol, ()):
                channel.append((name, pin))
            elif symbol in _SOURCE or symbol in ('opamp', 'vcvs') and pin == 'OUT':
                driven.append((name, pin))
        if driven:
            skip(pattern, 'externally_driven_junction', endpoints=[list(value) for value in driven])
            continue
        if len(channel) != 1 or channel[0][1] != 'D' or by_id[channel[0][0]].get('symbol') not in ('nmos', 'pmos'):
            skip(pattern, 'nonunique_mos_drain_anchor', endpoints=[list(value) for value in channel])
            continue
        host, pin = channel[0]
        symbol = by_id[host]['symbol']
        if (symbol == 'nmos' and _rail_role(rail) != 'supply'
                or symbol == 'pmos' and _rail_role(rail) != 'return'):
            skip(pattern, 'not_opposite_rail_load')
            continue
        planned_count += 1
        try:
            host_point = _physical_point(pl, index, host, pin)
            direction = pl.pin_dir(index[host], pin)
            if direction not in _VECTORS:
                raise ValueError('unknown drain outward direction')
            vector = _VECTORS[direction]
            members = []
            for name in names:
                pins = [key for key, net in by_id[name]['nodes'].items() if net == signal]
                if len(pins) != 1:
                    raise ValueError('nonunique bank signal terminal')
                point = _physical_point(pl, index, name, pins[0])
                projection = sum((point[axis]-host_point[axis])*vector[axis] for axis in (0, 1))
                members.append(dict(device=name, pin=pins[0], point=list(point),
                    projection=projection, error=max(0.0, -projection)/UNIT))
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
            skip(pattern, 'missing_physical_geometry', detail=type(error).__name__ + ': ' + str(error))
            continue
        error = sum(row['error'] for row in members)/len(members)
        records.append(dict(id=pattern.get('id'), kind='parallel_bank_outlet',
            members=names, nets=sorted(ends), rail_net=rail, signal_net=signal,
            host_endpoint=[host, pin], host_symbol=symbol, host_point=list(host_point),
            direction=direction, direction_vector=list(vector), member_endpoints=members,
            error=error, violations=sum(row['projection'] < -1e-8 for row in members)))
    complete = bool(records) and len(records) == planned_count
    return dict(version=VERSION, count=len(records), planned_count=planned_count,
        measurable=complete,
        error=sum(row['error'] for row in records)/len(records) if complete else None,
        records=records, skipped=skipped, distance_unit=UNIT)
