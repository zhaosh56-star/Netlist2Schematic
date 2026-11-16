"""Layout-only folded roles whose bias sources reach a rail through unique R/L.

Original nets/ports stay separate. Validated input roles, two exact conduction
forks, matching controls and exclusive rail paths are all required. This does
not change the baseline functional recognizer or infer amplifier direction.
"""
from collections import defaultdict
from copy import deepcopy

from netlist2schematic.netlist.netlist_core import SRC_ALIASES,SNK_ALIASES
from netlist2schematic.plugins.layout_shared.branch_levels import _rl_chains,_visible_incidence

VERSION='folded-RL-supply-evidence-v1'


def detect_folded_RL(devices,comps,evidence):
    pairs=[s for s in evidence['structures'] if s['kind']=='differential_pair' and s['confidence']=='high']
    mos={n:d for n,d in devices.items() if d['cls']=='MOS'}
    result=dict(version=VERSION,records=[],skipped=[],validated_supplies={},input_pairs_seen=len(pairs))
    if not pairs:return result
    roles=evidence['net_roles'];incidence=_visible_incidence(comps)
    chains,_=_rl_chains(devices,comps)
    by_endpoint=defaultdict(list)
    for chain in chains:
        for net in (chain['start'],chain['end']):by_endpoint[net].append(chain)

    def is_power(net):return roles.get(net,{}).get('role')=='power'

    def supply(name):
        if name in result['validated_supplies']:return result['validated_supplies'][name]
        d=mos[name];start=d['s'];expected=SRC_ALIASES if d['type']=='PMOS' else SNK_ALIASES
        if is_power(start) and str(start).casefold() in expected:
            found=[dict(bias=name,source_net=start,rail=start,members=[],path=[start],pin='S')]
        else:
            found=[]
            for chain in by_endpoint.get(start,()):
                rail=chain['end'] if chain['start']==start else chain['start']
                if not is_power(rail) or str(rail).casefold() not in expected:continue
                # A shared source, tap, drive or second path is not exclusive.
                external=[p for p in incidence[start] if p[0] not in chain['members']]
                if external!=[(name,'S')]:continue
                members=list(chain['members']);path=list(chain['path'])
                if path[0]!=start:members.reverse();path.reverse()
                found.append(dict(bias=name,source_net=start,rail=rail,members=members,path=path,pin='S'))
        if len(found)!=1:return None
        result['validated_supplies'][name]=found[0]
        return found[0]

    conduction={net:{(name,pin) for name,pin in users if pin in ('D','S','C','E')}
                for net,users in incidence.items()}
    seen=set()
    for pair in pairs:
        inputs=[pair['roles']['left'],pair['roles']['right']]
        if any(n not in mos for n in inputs):continue
        key=frozenset(inputs)
        if key in seen:continue
        seen.add(key);opposite='PMOS' if mos[inputs[0]]['type']=='NMOS' else 'NMOS'
        sides=[];reason=None
        for inp in inputs:
            fork=mos[inp]['d']
            if is_power(fork):reason='supply_is_not_a_fold_junction';break
            folded=[n for n,d in mos.items() if d['type']==opposite and d['s']==fork and d['d']!=fork]
            bias=[n for n,d in mos.items() if d['type']==opposite and d['d']==fork and d['s']!=fork]
            if len(folded)!=1 or len(bias)!=1:reason='nonunique_folded_conduction_roles';break
            f,b=folded[0],bias[0]
            if conduction.get(fork)!={(inp,'D'),(f,'S'),(b,'D')}:
                reason='extra_conduction_at_fold_junction';break
            proof=supply(b)
            if proof is None:reason='no_unique_polarized_RL_supply';break
            sides.append((inp,f,b,proof))
        if reason:
            result['skipped'].append(dict(pair=pair['id'],reason=reason));continue
        names=[n for side in sides for n in side[:3]]
        if len(set(names))!=6:reason='reused_folded_member'
        elif len({s[3]['rail'] for s in sides})!=1:reason='different_bias_supply_rails'
        elif len({mos[s[1]]['g'] for s in sides})!=1 or len({mos[s[2]]['g'] for s in sides})!=1:
            reason='unmatched_branch_controls'
        elif not any(s[3]['members'] for s in sides):
            continue  # Preserve the established direct-rail recognizer.
        if reason:
            result['skipped'].append(dict(pair=pair['id'],reason=reason));continue
        node_roles={prefix+'_'+side:s[k] for side,s in zip(('left','right'),sides)
                    for k,prefix in enumerate(('input','folded','bias'))}
        result['records'].append(dict(id='folded_RL:'+'|'.join(sorted(names)),kind='folded_core',
            confidence='high',devices=sorted(names),roles=node_roles,
            source='unique_RL_rail_layout_evidence',source_pair=pair['id'],
            supply_paths={s[2]:deepcopy(s[3]) for s in sides}))
    return result
