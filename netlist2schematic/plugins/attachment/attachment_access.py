"""Soft attachment access preferences derived from real host terminals.

The passive must face its host and also occupy a useful side of the host pin.
This module measures the latter without changing routing or geometric rules.
Shared nodes with incompatible host roles remain explicitly unclassified.
"""
from __future__ import annotations

from math import isfinite

VERSION = 'attachment-host-access-v1'
VECTORS = {'north': (0, -1), 'south': (0, 1), 'west': (-1, 0), 'east': (1, 0)}
UNIT = 100.0
EPS = 1e-8
ACTIVE = frozenset(('nmos', 'pmos', 'npn', 'pnp'))


def build_access_links(comps, plan):
    """Compile unique guest-to-junction links, never infer a host from a name."""
    by_id = {c['id']: c for c in comps}
    junctions = plan['readability_plan']['junctions']
    links, skipped = {}, []
    for group in plan['groups']:
        for net in group['owner_junctions']:
            junction = junctions[net]
            ports = junction['conduction_ports'] or junction['control_ports']
            # One real host, or several equivalent channel ends. Shared bias
            # controls and mixed D/S junctions cannot define one preferred side.
            if not ports or (len(ports) > 1 and (
                    len({pin for _, pin in ports}) != 1
                    or ports[0][1] not in ('D', 'S', 'C', 'E')
                    or len({by_id[name]['symbol'] for name, _ in ports}) != 1
                    or by_id[ports[0][0]]['symbol'] not in ACTIVE)):
                skipped.append(dict(group=group['id'], net=net,
                                    reason='ambiguous_host_access_roles'))
                continue
            hosts = tuple(sorted(tuple(port) for port in ports))
            for name in group['members']:
                pins = [pin for pin, other in by_id[name]['nodes'].items() if other == net]
                if not pins:
                    continue  # Only an actual path endpoint uses this junction.
                if len(pins) != 1:
                    skipped.append(dict(group=group['id'], member=name, net=net,
                                        reason='nonunique_guest_terminal'))
                    continue
                key = (name, pins[0], net, hosts)
                link = links.setdefault(key, dict(member=name, pin=pins[0], net=net,
                    hosts=[list(port) for port in hosts], groups=[]))
                if group['id'] not in link['groups']:
                    link['groups'].append(group['id'])
    return dict(version=VERSION, links=[links[key] for key in sorted(links)], skipped=skipped)


def measure_access(pl, comps, index, evidence):
    """Backward projection in 100px units; it is a preference, never a ban."""
    links = evidence.get('links', ())
    records, missing = [], []
    for link in links:
        try:
            member = index[link['member']]
            if comps[member]['nodes'][link['pin']] != link['net']:
                raise ValueError('guest_terminal_changed')
            point = tuple(pl.pin_world(member, link['pin']))
            if len(point) != 2 or not all(isfinite(v) for v in point):
                raise ValueError('invalid_guest_geometry')
            projections = []
            for name, pin in link['hosts']:
                host = index[name]
                if comps[host]['nodes'][pin] != link['net']:
                    raise ValueError('host_terminal_changed')
                origin = tuple(pl.pin_world(host, pin))
                vector = VECTORS[pl.pin_dir(host, pin)]
                if len(origin) != 2 or not all(isfinite(v) for v in origin):
                    raise ValueError('invalid_host_geometry')
                projections.append(sum((p-o)*v for p, o, v in zip(point, origin, vector)))
            if not projections:
                raise ValueError('missing_host')
            records.append(dict(member=link['member'], pin=link['pin'], net=link['net'],
                hosts=link['hosts'], groups=link['groups'],
                backward_distance=sum(max(0.0, -v) for v in projections)/len(projections)/UNIT,
                violation=any(v < -EPS for v in projections)))
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            missing.append(dict(member=link['member'], net=link['net'], reason=str(exc)))
    measurable = bool(links) and not missing
    return dict(version=VERSION, count=len(links), measured=len(records), measurable=measurable,
        errors=sum(r['violation'] for r in records) if measurable else None,
        backward_distance=sum(r['backward_distance'] for r in records)/len(records) if measurable else None,
        records=records, missing=missing, skipped=evidence.get('skipped', []))


def access_terms(metrics):
    """Return neutral terms only for evidence with no applicable links."""
    access = metrics.get('host_access')
    if access is None:  # Compatibility with older measurement-only records.
        return 0, 0.0
    if not access['count']:
        return 0, 0.0
    if not access['measurable']:
        return None
    return access['errors'], access['backward_distance']
