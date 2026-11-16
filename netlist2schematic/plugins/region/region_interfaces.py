"""Nested fanout ordering for port-proven banks beside a functional core.

Longer vertical access belongs farther outside; shorter access stays inside.
This is a relative port rule, independent of device IDs or named signals.
"""
from __future__ import annotations

from collections import defaultdict
from math import isfinite

EPS=1e-8


def compile_port_banks(regions,comps,rails):
    by_id={comp['id']:comp for comp in comps}
    by_region={region['id']:region for region in regions}
    result=[]
    for region in regions:
        if region.get('children'):
            continue
        groups=defaultdict(list)
        for attachment in region.get('attachments',()):
            groups[(attachment.get('target_region'),attachment.get('branch'))].append(attachment)
        for (target_id,branch),attachments in groups.items():
            target=by_region.get(target_id)
            if (len(attachments)<2 or not target or target.get('children')
                    or not region.get('parent') or region.get('parent')!=target.get('parent')
                    or len({a.get('device') for a in attachments})!=len(attachments)):
                continue
            ports=[]
            for a in attachments:
                name,pin,net=a.get('device'),a.get('pin'),a.get('net')
                if (name not in by_id or name not in region['members'] or pin not in ('D','C')
                        or by_id[name]['nodes'].get(pin)!=net or net in rails):
                    break
                targets=[dict(device=member,pin=p) for member in target['members']
                    for p,n in by_id[member]['nodes'].items() if n==net and p in ('D','S','C','E')]
                if not targets:
                    break
                ports.append(dict(device=name,pin=pin,net=net,targets=targets))
            else:
                result.append(dict(kind='nested_conduction_fanout',source_region=region['id'],
                    target_region=target_id,branch=branch,ports=ports))
    return result


def measure_port_banks(pl,index,banks):
    records,missing=[],[]
    for bank in banks:
        points=[]
        try:
            for entry in bank['ports']:
                point=tuple(pl.pin_world(index[entry['device']],entry['pin']))
                targets=[tuple(pl.pin_world(index[t['device']],t['pin'])) for t in entry['targets']]
                if not all(isfinite(v) for p in [point,*targets] for v in p):
                    raise ValueError('nonfinite port bank geometry')
                points.append((entry,point,targets))
        except (KeyError,IndexError,TypeError,ValueError,AttributeError) as exc:
            missing.append(dict(source_region=bank.get('source_region'),reason=str(exc)))
            continue
        target_x=[p[0] for _,_,targets in points for p in targets]
        xs=[point[0] for _,point,_ in points]
        west=all(x<min(target_x)-EPS for x in xs)
        east=all(x>max(target_x)+EPS for x in xs)
        errors,pairs=0,0
        samples=[]
        if not west and not east:
            # Moving a bank onto/across its branch cannot hide the relation.
            errors=1
            reason='bank_straddles_target_branch'
        else:
            reason='nested_access_order'
            for entry,point,targets in points:
                span=min(abs(point[1]-target[1]) for target in targets)
                outer=(-point[0]) if west else point[0]
                samples.append(dict(device=entry['device'],vertical_span=span,outer_coordinate=outer))
            for n,a in enumerate(samples):
                for b in samples[n+1:]:
                    if abs(a['vertical_span']-b['vertical_span'])<=EPS:
                        continue
                    pairs+=1
                    errors+=(a['vertical_span']-b['vertical_span'])*(a['outer_coordinate']-b['outer_coordinate'])<-EPS
        records.append(dict(source_region=bank['source_region'],target_region=bank['target_region'],
            branch=bank.get('branch'),errors=int(errors),ordered_pairs=pairs,reason=reason,samples=samples))
    return dict(relation_count=len(banks),measurable=not missing,missing=missing,records=records,
        errors=sum(r['errors'] for r in records) if not missing else None,
        ordered_pairs=sum(r['ordered_pairs'] for r in records))
