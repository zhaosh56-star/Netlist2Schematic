"""A layout projection, never an electrically contracted netlist."""
from collections import Counter
from copy import deepcopy
from itertools import product

from netlist2schematic.plugins.passive_skeleton.modules import build_modules

VERSION = 'active-first-passive-modules-v3'
ACTIVE = frozenset(('nmos', 'pmos', 'npn', 'pnp'))
PASSIVE = frozenset(('resistor', 'capacitor', 'inductor', 'diode'))
CHANNELS = {'nmos': ('D', 'S'), 'pmos': ('D', 'S'), 'npn': ('C', 'E'), 'pnp': ('C', 'E')}


def compile_plan(par, devices, comps, nets, structs):
    """Keep every real terminal; only unambiguous R/L edges imply row order.

    C, D, feedback and sense paths remain typed two-ended attachments. In
    particular, two sense resistors never merge two output nodes in this view.
    """
    from netlist2schematic.plugins.layout_shared.branch_levels import _rl_chains, _conduction_orders
    result = build_modules(par, comps, nets)
    parts = {c['id']: c for c in comps}
    bridges, skipped = _rl_chains(devices, comps)
    _, bridges, cycles, _ = _conduction_orders(devices, bridges)
    unique_rl = {frozenset(b['members']): b for b in bridges}
    for group in result['groups']:
        group['role'] = 'bridge'
        live = [n for n in group['ends'] if group['anchors'][n]]
        bridge = unique_rl.get(frozenset(group['members']))
        if bridge is not None:
            group['role'] = 'conduction_inline'
            group['conduction'] = deepcopy(bridge)
        elif len(live) == 1:
            rail = next((n for n in group['ends'] if par.power_marker_sym(n)), None)
            bank = group['module']['kind'] == 'parallel' or len(group['paths']) > 1
            group['role'] = ('terminal_bank' if bank else 'rail_attachment') if rail else 'port_extension'
        elif not live:
            group['role'] = 'unanchored'
        if len(live)==2 and len(group['paths'])==1:
            ends=[[(parts[t[0]]['symbol'],t[1]) for t in group['anchors'][n]] for n in group['ends']]
            source_side=any(len(side)==1 and side[0][0] in ('voltage-source','current-source') for side in ends)
            control_side=any(len(side)==1 and side[0][0] in ACTIVE and side[0][1] in ('G','B') for side in ends)
            if source_side and control_side:group['role']='source_control_chain'
        group['signature'] = group['module_signature']
        if len(live)==1:
            net=live[0]
            conductive=[t for t in group['anchors'][net] if t[1] in CHANNELS.get(parts[t[0]]['symbol'],())]
            if len(conductive)==1:
                group['owner_port']=dict(net=net,terminal=list(conductive[0]))
    # Corresponding banks require a real core pair as well as equal path roles.
    # Shared VDD, GND, CLK or a model name alone cannot create a family.
    pairs = set()
    for record in structs:
        if record['kind'] not in ('differential_pair', 'cross_coupled', 'current_mirror', 'latch'):
            continue
        roles = record.get('roles', {})
        candidates = [(roles.get('left'), roles.get('right'))]
        if len(record.get('devices', ())) == 2:
            candidates.append(tuple(record['devices']))
        for a, b in candidates:
            if a in parts and b in parts and parts[a]['symbol'] in ACTIVE and parts[b]['symbol'] in ACTIVE:
                pairs.add(frozenset((a, b)))
    families = []
    banks = [g for g in result['groups'] if g['role'] == 'terminal_bank']
    for k, a in enumerate(banks):
        for b in banks[k+1:]:
            aa = [a['owner_port']['terminal']] if a.get('owner_port') else []
            bb = [b['owner_port']['terminal']] if b.get('owner_port') else []
            if (len(aa) == len(bb) == 1 and aa[0][1] == bb[0][1]
                    and frozenset((aa[0][0], bb[0][0])) in pairs
                    and a['signature'] == b['signature']
                    and {n for n in a['ends'] if par.power_marker_sym(n)} ==
                        {n for n in b['ends'] if par.power_marker_sym(n)}):
                families.append(dict(groups=[a['id'], b['id']], hosts=[aa[0][0], bb[0][0]],
                    evidence='confirmed_core_pair_and_matching_attachment_paths'))
    used=Counter(gid for f in families for gid in f['groups'])
    ambiguous=[f for f in families if any(used[gid]>1 for gid in f['groups'])]
    families=[f for f in families if all(used[gid]==1 for gid in f['groups'])]
    result.update(version=VERSION, families=families, ambiguous_families=ambiguous, rl_skipped=skipped+cycles,
        core_ids=[c['id'] for c in comps if c['symbol'] not in PASSIVE],
        applicable=any(c['symbol'] in ACTIVE for c in comps) and result['passive_count'] > 0
            and result['covered_count'] == result['passive_count'],
        electrical_node_merges=[], projection_only=True)
    return result


def host_pairs(pl, comps, index, group):
    """Prefer cross-branch conduction ports at corresponding heights.

    A feedback node can contain a drain and a remote gate. Picking only its
    nearest gate would hide the actual span between the two active branches.
    """
    a, b = group['ends']
    first, last = group['anchors'][a], group['anchors'][b]
    def key(pair):
        (na, pa), (nb, pb) = pair
        ia, ib = index[na], index[nb]
        ca = pa in CHANNELS.get(comps[ia]['symbol'], ())
        cb = pb in CHANNELS.get(comps[ib]['symbol'], ())
        xa, ya = pl.pin_world(ia, pa); xb, yb = pl.pin_world(ib, pb)
        return (na == nb, not (ca and cb), abs(pl.cell_of[ia][0]-pl.cell_of[ib][0]),
                abs(xa-xb)+abs(ya-yb), xa, ya, xb, yb)
    return sorted(product(first, last), key=key)


def measure(pl, comps, index, plan):
    from netlist2schematic.plugins.passive_embedding.passive_embedding import measure as regularity
    from netlist2schematic.plugins.passive_skeleton.module_geometry import template
    from netlist2schematic.plugins.passive_embedding.passive_embedding import pitch, _box_distance
    from netlist2schematic.routing.routing_rules import RoutingOptions
    from netlist2schematic.common.sa_common import load_local
    from statistics import median
    # Old path metrics apply only to disjoint unbranched paths. Nested modules
    # must not duplicate a shared resistor by enumerating all terminal paths.
    flat = {**plan, 'groups':[g for g in plan['groups'] if g['kind'] != 'series_parallel']}
    base = regularity(pl, comps, index, flat)
    par = None
    for g in plan['groups']:
        if g['kind'] != 'series_parallel':
            continue
        if par is None:
            par = load_local('place_and_route', 'place-and-route.py')
        choices=[];scale=pitch(pl)
        for direction in ((1,0),(-1,0),(0,1),(0,-1)):
            options=RoutingOptions(**plan.get('geometry_parameters',{}))
            geo=template(par,pl,comps,index,g['module'],direction,options,
                         extra_keepout=options.cross_clearance if plan.get('version','').endswith('v2') else 0)
            if geo is None:
                continue
            offsets=[tuple(pl.pos_of(i)[k]-p[k] for k in (0,1)) for i,p in geo['positions'].items()]
            shift=tuple(median(p[k] for p in offsets) for k in (0,1))
            error=sum(sum(abs(pl.pos_of(i)[k]-p[k]-shift[k]) for k in (0,1)) for i,p in geo['positions'].items())/scale
            expected={i:{p['name']:p['direction'] for p in par.GEO_DOC[comps[i]['symbol']]['orientations'][geo['orientations'][i]]['pins']}
                      for i in geo['positions']}
            wrong=sum(pl.pin_dir(i,p)!=expected[i][p] for i in geo['positions'] for p in comps[i]['nodes'])
            choices.append((wrong,error))
        wrong,error=min(choices) if choices else (len(g['members']),float(len(g['members'])))
        distance=0.
        for net in g['ends']:
            hosts=[pl.pin_world(index[n],p) for n,p in g['anchors'][net]]
            if hosts:
                distance+=sum(_box_distance(pl.pin_world(index[n],p),hosts) for n,p in g['boundary_ports'][net])/scale
        points=[pl.pos_of(index[n]) for n in g['members']]
        span=sum(max(p[k] for p in points)-min(p[k] for p in points) for k in (0,1))/scale
        record=dict(id=g['id'],kind=g['kind'],members=g['members'],direction_errors=wrong,
                    shape_error=error,access_distance=distance,span=span,anchored=any(g['anchors'].values()))
        base['groups'].append(record)
        for key in ('direction_errors','shape_error','access_distance','span'):
            base[key]+=record[key]
    base['group_count']=len(plan['groups'])
    inline = []
    for g in plan['groups']:
        if g['role'] != 'conduction_inline':
            continue
        hosts = [t for ts in g['anchors'].values() for t in ts if comps[index[t[0]]]['symbol'] in ACTIVE]
        if len(hosts) == 2:
            a, b = (pl.pin_world(index[n], p) for n, p in hosts)
            points = [a, b] + [pl.pos_of(index[n]) for n in g['members']]
            inline.append(max(p[0] for p in points)-min(p[0] for p in points))
    family_errors = []
    by_id = {g['id']: g for g in plan['groups']}
    for family in plan['families']:
        profiles = []
        hosts = sorted(family['hosts'], key=lambda n: pl.pos_of(index[n])[0])
        for host in hosts:
            group = next(by_id[gid] for gid in family['groups'] if by_id[gid]['owner_port']['terminal'][0]==host)
            net,terminal=group['owner_port']['net'],group['owner_port']['terminal']
            hx, hy = pl.pin_world(index[host], terminal[1]); sign = 1 if host == hosts[0] else -1
            profile = []
            for name in group['members']:
                x, y = pl.pos_of(index[name])
                # Compare actual positions, not orientation IDs under reflection.
                profile.append((comps[index[name]]['symbol'], round(sign*(x-hx), 6), round(y-hy, 6)))
            profiles.append(sorted(profile))
        family_errors.append(sum(abs(a[1]-b[1])+abs(a[2]-b[2]) for a,b in zip(*profiles)))
    return dict(regularity=base, inline_error=sum(inline), family_error=sum(family_errors),
        role_count=len(plan['groups']), family_count=len(plan['families']), covered_count=plan['covered_count'])


def organization_key(metrics):
    m = metrics['regularity']
    return (round(metrics['inline_error'], 5), round(metrics['family_error'], 5),
            m['direction_errors'], round(m['shape_error'], 5), round(m['access_distance'], 5))
