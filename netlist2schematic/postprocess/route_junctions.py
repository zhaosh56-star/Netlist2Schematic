"""Small three-terminal junction patterns on immutable pin escapes.

The ordinary simplifier owns all acceptance, cycle, coverage, geometric and
budget checks. This generator reads only existing Scene geometry and offers
one complete net at a time; it never searches, moves pins or changes rules.
"""
from itertools import combinations

from netlist2schematic.routing.routing_rules import EPS,canonical_topology,check_deadline

VERSION='aligned-three-terminal-junctions-v1'
PATTERN='aligned_three_terminal_T'
MAX_PER_NET=3


def _length(runs):
    return sum(abs(a[0]-b[0])+abs(a[1]-b[1]) for a,b in runs)


def aligned_T_candidates(scene,routed,deadline=None):
    """Two aligned escapes form a trunk; the third projects inside it.

    All fixed segments stay in the proposed tree, including the original
    source-to-escape stubs. The third escape is an ordinary point of the new
    stem, so its former branch degree need not remain frozen. More than three
    terminals, power nets and degenerate repeated escapes are left unchanged.
    """
    families=[]
    for net,segments in routed:
        check_deadline(deadline,'simplify_T_extraction')
        pins=scene.pins.get(net,())
        if net in scene.power_nets or len(pins)!=3:continue
        if len({(p.component,p.pin) for p in pins})!=3 or len({p.escape for p in pins})!=3:continue
        patterns=[]
        for i,j in combinations(range(3),2):
            check_deadline(deadline,'simplify_T_generation')
            k=next(t for t in range(3) if t not in (i,j))
            a,b,c=(pins[t].escape for t in (i,j,k));junction=None
            if a[1]==b[1] and min(a[0],b[0])<=c[0]<=max(a[0],b[0]):
                junction=(c[0],a[1])
            elif a[0]==b[0] and min(a[1],b[1])<=c[1]<=max(a[1],b[1]):
                junction=(a[0],c[1])
            if junction is None:continue
            patterns.append((a,b,c,junction))
        if patterns:
            # This cheap ordering is only an estimate. Exact per-net topology
            # is built lazily when the family gets its turn, so a long list of
            # other nets cannot spend the first operation's whole quota before
            # ordinary chains receive any candidate opportunity.
            raw_length=_length(segments)
            fixed_length=_length(scene.fixed_segments.get(net,()))
            estimate=min(abs(a[0]-b[0])+abs(a[1]-b[1])+abs(c[0]-p[0])+abs(c[1]-p[1])
                         for a,b,c,p in patterns)+fixed_length
            families.append(((-max(0.0,raw_length-estimate),-raw_length,
                              tuple(sorted(p.escape for p in pins)),str(net)),net,segments,pins,patterns))
    for _,net,segments,pins,patterns in sorted(families,key=lambda row:row[0]):
        check_deadline(deadline,'simplify_T_sort')
        original=canonical_topology(segments,deadline=deadline)
        old_length=_length(original['runs']);seen=set()
        for a,b,c,junction in patterns:
            check_deadline(deadline,'simplify_T_candidate')
            proposal=list(scene.fixed_segments.get(net,()))+[(a,b)]
            if c!=junction:proposal.append((c,junction))
            topology=canonical_topology(proposal,deadline=deadline);identity=tuple(topology['runs'])
            new_length=_length(topology['runs'])
            if identity in seen or identity==tuple(original['runs']) or new_length>old_length+EPS:continue
            seen.add(identity)
            yield dict(net=net,segments=topology['segments'],pattern=PATTERN,
                    junction=list(junction),trunk=[list(a),list(b)],
                    terminals=[dict(component=p.component,pin=p.pin) for p in pins],
                    old_chain_length=old_length,new_chain_length=new_length,
                    shortcut_identity=(net,identity))
