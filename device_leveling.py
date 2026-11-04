#!/usr/bin/env python3
"""device_leveling.py -- 通用模拟电路分层原型（只读网表，不改任何现有代码）

原则：
- 只有有源器件（MOS D-S、BJT C-E）占"主层"——它们定义直流电流通路的电位台阶。
- R/C/I/V/E 不占主层，按"挂靠规则"贴到有源层旁边：
    R  串联在主通路上 → 两端有源层之间；偏置/反馈(一端接栅) → 挂在栅器件的半层
    C  一律隔直不进传导图；挂靠到两端电位较高的那侧
    I  电流源 → 比它供电的那级高半层（从 VDD 灌下来）
    V  电压源 → 贴它驱动的器件同层（栅/偏置侧）
    E  VCVS(运放) → 按输出端电流路径归层（宏器件）

分层算法（有源器件）：
  1. 传导图只含有源管 D-S/C-E 有向边（PMOS S->D, NMOS D->S, PNP E->C, NPN C->E）。
  2. Tarjan SCC 缩点成 DAG。
  3. 源集合：供电别名(VDD/VCC…) → 电流源灌向节点(伪源) → 无入边分量 → 度最大。
  4. DAG 最长路径定"网络层"（源=层0）。
  5. 器件层 = min(进水口网络层, 出水口网络层) + 1：管子贴在它最高的那头下面一格；
     死胡同端(未达)自动忽略。栅极不参与定层——栅是控制端，不载流，可在任意层。
"""
import argparse
import os
from pathlib import Path
import sys
from collections import deque

from netlist_core import (SRC_ALIASES, SNK_ALIASES, NetlistError,
                          SourceLine, classify_tokens, parse_netlist, terminal_nets)


def classify(name, tok):
    """Compatibility wrapper; all real parsing uses the shared reader."""
    return classify_tokens(tok, {}, SourceLine(Path('<input>'), 1, ' '.join(tok)))


def build_active_edges(devices):
    """只含有源管传导边。R/C/源件一律不进传导图。"""
    edges = []
    for n, d in devices.items():
        c = d['cls']
        if c == 'MOS':
            if d['type'] == 'PMOS':
                edges.append((d['s'], d['d'], n))
            elif d['type'] == 'NMOS':
                edges.append((d['d'], d['s'], n))
            else:
                edges.append((d['d'], d['s'], n))
                edges.append((d['s'], d['d'], n))
        elif c == 'BJT':
            if d['type'] == 'PNP':
                edges.append((d['e'], d['c'], n))
            else:
                edges.append((d['c'], d['e'], n))
    return edges


def active_terminal_nets(devices):
    """有源管 D/S（或 C/E）碰到的网络 + 源件端子（伪源识别用）。"""
    nets = set()
    for d in devices.values():
        c = d['cls']
        if c == 'MOS':
            nets.update((d['d'], d['s']))
        elif c == 'BJT':
            nets.update((d['c'], d['e']))
        elif c in ('ISRC', 'VSRC'):
            nets.update((d['p'], d['n']))
        elif c == 'VCVS':
            nets.update((d['op'], d['on']))
    return nets


def tarjan_scc(nodes, adj):
    """Deterministic Tarjan condensation, independent of hash iteration order."""
    # Preserve support for deep transistor chains from the original prototype.
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 2 * len(nodes) + 100))
    idx, low, comp = {}, {}, {}
    stack, onst = [], set()
    next_index, next_component = 0, 0

    def visit(v):
        nonlocal next_index, next_component
        idx[v] = low[v] = next_index
        next_index += 1
        stack.append(v)
        onst.add(v)
        for w in sorted(adj.get(v, ())):
            if w not in idx:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in onst:
                low[v] = min(low[v], idx[w])
        if low[v] == idx[v]:
            while True:
                w = stack.pop()
                onst.remove(w)
                comp[w] = next_component
                if w == v:
                    break
            next_component += 1

    for v in sorted(nodes):
        if v not in idx:
            visit(v)
    return comp


def condense(comp, edges):
    ncomp = len(set(comp.values())) if comp else 0
    cadj = {c: set() for c in range(ncomp)}
    for a, b, _ in edges:
        if comp[a] != comp[b]:
            cadj[comp[a]].add(comp[b])
    return cadj, ncomp


def longest_levels(cadj, ncomp, src):
    indeg = {c: 0 for c in range(ncomp)}
    for c in cadj:
        for d in sorted(cadj[c]):
            indeg[d] += 1
    level = {c: -1 for c in range(ncomp)}
    for s in src:
        if s in level:
            level[s] = 0
    q = deque([c for c in range(ncomp) if indeg[c] == 0])
    order = []
    while q:
        c = q.popleft(); order.append(c)
        for d in sorted(cadj[c]):
            indeg[d] -= 1
            if indeg[d] == 0:
                q.append(d)
    for c in order:
        if level[c] < 0:
            continue
        for d in sorted(cadj[c]):
            level[d] = max(level[d], level[c] + 1)
    return level


def current_source_seed_nets(devices):
    seeds = set()
    for n, d in devices.items():
        if d['cls'] == 'ISRC':
            if d['p'].lower() in SRC_ALIASES:
                seeds.add(d['n'])
            elif d['n'].lower() in SNK_ALIASES:
                seeds.add(d['p'])
    return seeds


def pick_sources(comp, cadj, devices):
    """Keep preferred supply seeds and cover unreached active DAG branches."""
    aliases = {comp[n] for n in comp if n.lower() in SRC_ALIASES}
    current = {comp[n] for n in current_source_seed_nets(devices) if n in comp}
    sources = aliases | current
    note = ('alias+isrc' if current else 'alias') if aliases else ('isrc-seed' if current else '')
    reached = set(sources)
    pending = list(sorted(sources))
    while pending:
        for child in sorted(cadj[pending.pop()]):
            if child not in reached:
                reached.add(child)
                pending.append(child)
    active_components = set()
    for device in devices.values():
        if device['cls'] == 'MOS':
            active_components.update((comp[device['d']], comp[device['s']]))
        elif device['cls'] == 'BJT':
            active_components.update((comp[device['c']], comp[device['e']]))
    missing = active_components - reached
    children = {child for parent in missing for child in cadj[parent] if child in missing}
    roots = missing - children
    if roots:
        sources.update(roots)
        note = note + '+branch-root' if note else 'no-in-edge'
    if not sources:
        indegree = {c: 0 for c in cadj}
        for targets in cadj.values():
            for target in targets:
                indegree[target] += 1
        sources = {c for c, degree in indegree.items() if degree == 0}
        note = 'no-in-edge' if sources else 'none'
    return sources, note


def out_terminal(d):
    if d['cls'] == 'MOS':
        return d['d'] if d['type'] == 'PMOS' else d['s']
    if d['cls'] == 'BJT':
        return d['c'] if d['type'] == 'PNP' else d['e']
    return None


def in_terminal(d):
    if d['cls'] == 'MOS':
        return d['s'] if d['type'] == 'PMOS' else d['d']
    if d['cls'] == 'BJT':
        return d['e'] if d['type'] == 'PNP' else d['c']
    return None


def compute_levels(devices):
    """Return network and active-device levels shared by CLI and layout."""
    edges = build_active_edges(devices)
    nodes = active_terminal_nets(devices)
    adjacency = {net: [] for net in sorted(nodes)}
    for start, end, _ in edges:
        adjacency[start].append(end)
    comp = tarjan_scc(nodes, adjacency)
    cadj, ncomp = condense(comp, edges)
    sources, note = pick_sources(comp, cadj, devices)
    levels = longest_levels(cadj, ncomp, sources)
    net_levels = {net: levels.get(comp.get(net), -1)
                  for net in sorted({net for d in devices.values() for net in terminal_nets(d)})}
    device_levels = {}
    for name, device in devices.items():
        if device['cls'] in ('MOS', 'BJT'):
            ends = [net_levels.get(net, -1) for net in (in_terminal(device), out_terminal(device))]
            reached = [level for level in ends if level >= 0]
            device_levels[name] = min(reached) + 1 if reached else -1
    return {'net_levels': net_levels, 'device_levels': device_levels,
            'source_note': note, 'comp': comp, 'cadj': cadj}


def level_file(path, cell=None):
    devices = parse_netlist(path, cell=cell)
    result = compute_levels(devices)
    comp, note = result['comp'], result['source_note']

    def lv(net):
        return result['net_levels'].get(net, -1)

    def dev_level(d):
        cand = [x for x in (lv(in_terminal(d)), lv(out_terminal(d))) if x >= 0]
        return min(cand) + 1 if cand else -1

    active_rows = {}
    for n, d in sorted(devices.items()):
        if d['cls'] in ('MOS', 'BJT'):
            l = dev_level(d)
            desc = (f"{n}({d['type']} D={d['d']} G={d['g']} S={d['s']})"
                    if d['cls'] == 'MOS' else
                    f"{n}({d['type']} C={d['c']} B={d['b']} E={d['e']})")
            active_rows.setdefault(l, []).append((n, desc))

    print(f'===== {os.path.basename(path)}  [源:{note}] =====')
    for l in sorted(active_rows):
        tag = f'层{l}' if l >= 0 else '未达'
        print(f'  {tag}: ' + '  '.join(d for _, d in active_rows[l]))

    # ---- 第二遍：挂靠器件 ----
    attach = []
    for n, d in sorted(devices.items()):
        c = d['cls']
        if c == 'PASSIVE':
            la, lb = lv(d['a']), lv(d['b'])
            # 有一端是栅节点(不在传导图) -> 偏置/反馈，挂在有源端的半层下
            in_a, in_b = d['a'] in comp, d['b'] in comp
            if in_a and not in_b:
                attach.append(f"{n}({d['type']} {d['a']}~{d['b']}[栅] → 挂层{la}+0.5)")
            elif in_b and not in_a:
                attach.append(f"{n}({d['type']} {d['a']}[栅]~{d['b']} → 挂层{lb}+0.5)")
            else:
                hi = max(la, lb); lo = min(la, lb)
                if la >= 0 and lb >= 0:
                    attach.append(f"{n}({d['type']} {d['a']}~{d['b']} → 层{lo}~{hi}之间)")
                else:
                    attach.append(f"{n}({d['type']} {d['a']}~{d['b']} → 参考层{hi})")
        elif c == 'ISRC':
            tgt = lv(d['n'])
            attach.append(f"{n}(I {d['p']}→{d['n']} → 挂层{tgt}-0.5,供电上方)")
        elif c == 'VSRC':
            tgt = lv(d['p'])
            attach.append(f"{n}(V {d['p']}/{d['n']} → 挂层{tgt},被驱动侧)")
        elif c == 'VCVS':
            tgt = lv(d['op'])
            attach.append(f"{n}(E out={d['op']} in={d['ip']}/{d['in']} → 宏器件层{tgt})")
    if attach:
        print('  挂靠: ' + '  '.join(attach))
    print()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Print conduction levels and passive attachments.')
    parser.add_argument('netlists', nargs='+')
    parser.add_argument('--cell', help='select one .subckt without recursive expansion')
    args = parser.parse_args(argv)
    try:
        for path in args.netlists:
            level_file(path, cell=args.cell)
    except (NetlistError, OSError) as exc:
        parser.exit(2, f'error: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
