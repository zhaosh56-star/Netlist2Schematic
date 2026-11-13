#!/usr/bin/env python3
"""netlist2schematic.recognition.vf2_match.py -- 用 VF2 子图同构找模拟电路基础子模块（二部图建模，ALIGN 方式）

建模（照搬 ALIGN align/schema/graph.py）：
  节点 = 器件 + 网（二部图，两类节点）
  边   = 器件—网，边上带 pin={引脚名} 集合
  node_match: 器件比 model，网比网
  edge_match: 比 pin 集合是否相等
  → 独占过滤免费得到：网是节点，度就是端点数，VF2 拓扑匹配自动检查

模板（照搬 ALIGN user_template.sp 的子电路定义）：
  每个模板也是一张二部图：模板器件 + 模板网，边标引脚名。
  差分对 DP_N/DP_P、电流镜 SCM_N/SCM_P、CMOS 互补 INV、
  交叉耦合 CCP_N/CCP_P、并联组 PAR_N/PAR_P、共源共栅 CAS_N/CAS_P。
"""
import argparse
import os
import sys
from collections import defaultdict

import networkx as nx
from networkx.algorithms.isomorphism import GraphMatcher

from netlist2schematic.recognition import device_leveling as dl

# ---------- 二部图构建（照搬 ALIGN graph.py）----------
# 节点: 器件名 -> {inst: model}   网名 -> {} (无 inst 键)
# 边:   (器件, 网) -> pin={...}

def build_graph(devices):
    G = nx.Graph()
    for n, d in devices.items():
        if d['cls'] != 'MOS':
            continue
        model = 'P' if d.get('type') in ('PMOS', 'PNP') else 'N'
        pins = {'D': d['d'], 'G': d['g'], 'S': d['s']}
        # NMOS_S 型：体端 bulk 接源端时并入 S 引脚集合
        if d['cls'] == 'MOS' and d.get('bulk') == d['s']:
            pins['B'] = d['bulk']
        G.add_node(n, inst=model)
        for pin, net in pins.items():
            net_node = ('net', net)  # separate net and device namespaces
            if G.has_edge(n, net_node):
                G[n][net_node]['pin'].add(pin)
            else:
                G.add_edge(n, net_node, pin={pin})
    return G


def node_match(x, y):
    """ALIGN default_node_match 的简化版：器件比 model，网比网（都无 inst）"""
    xi, yi = x.get('inst'), y.get('inst')
    if yi is None:                # 模板网节点：大图对应方必须是网
        return xi is None
    return xi == yi               # 模板器件：model 相等


def edge_match(x, y):
    """模板 pin ⊆ 大图 pin（单边）：允许同器件多引脚共网（体端）"""
    return y.get('pin', set()) <= x.get('pin', set())


# ---------- 模板（照搬 user_template.sp 的连接关系）----------
# 每个模板 = list of (模板器件名, model, {pin: 模板网})

def _tpl(*devs):
    """devs: [(name, model, {D:net, G:net, S:net}), ...] → 二部图"""
    G = nx.Graph()
    for name, model, pins in devs:
        G.add_node(name, inst=model)
        for pin, net in pins.items():
            if G.has_edge(name, net):
                G[name][net]['pin'].add(pin)
            else:
                G.add_edge(name, net, pin={pin})
    return G


def _n(i):  # 模板内部网命名
    return f'@n{i}'

# 差分对：两管共源，栅漏各异  （DP 简化版）
#   M0 DAp GAp S S   M1 DAn GAn S S
DP_N = _tpl(('m0', 'N', {'D': 'DA', 'G': 'GA', 'S': 'S'}),
            ('m1', 'N', {'D': 'DB', 'G': 'GB', 'S': 'S'}))
DP_P = _tpl(('m0', 'P', {'D': 'DA', 'G': 'GA', 'S': 'S'}),
            ('m1', 'P', {'D': 'DB', 'G': 'GB', 'S': 'S'}))

# 电流镜（SCM 两管，M0 二极管连接：D=G=DA）
#   M0 DA DA S   M1 DB DA S
SCM_N = _tpl(('m0', 'N', {'D': 'DA', 'G': 'DA', 'S': 'S'}),
             ('m1', 'N', {'D': 'DB', 'G': 'DA', 'S': 'S'}))
SCM_P = _tpl(('m0', 'P', {'D': 'DA', 'G': 'DA', 'S': 'S'}),
             ('m1', 'P', {'D': 'DB', 'G': 'DA', 'S': 'S'}))

# CMOS 互补（INV）：一 N 一 P，栅同网 i，漏同网 zn
#   M0 zn i SN   M1 zn i SP
INV = _tpl(('m0', 'N', {'D': 'zn', 'G': 'i', 'S': 'SN'}),
           ('m1', 'P', {'D': 'zn', 'G': 'i', 'S': 'SP'}))

# 交叉耦合（CCP）：甲漏=乙栅、乙漏=甲栅，共源
#   M0 DA DB S   M1 DB DA S
CCP_N = _tpl(('m0', 'N', {'D': 'DA', 'G': 'DB', 'S': 'S'}),
             ('m1', 'N', {'D': 'DB', 'G': 'DA', 'S': 'S'}))
CCP_P = _tpl(('m0', 'P', {'D': 'DA', 'G': 'DB', 'S': 'S'}),
             ('m1', 'P', {'D': 'DB', 'G': 'DA', 'S': 'S'}))

# 并联组：同型，D/G/S 全同
PAR_N = _tpl(('m0', 'N', {'D': 'D', 'G': 'G', 'S': 'S'}),
             ('m1', 'N', {'D': 'D', 'G': 'G', 'S': 'S'}))
PAR_P = _tpl(('m0', 'P', {'D': 'D', 'G': 'G', 'S': 'S'}),
             ('m1', 'P', {'D': 'D', 'G': 'G', 'S': 'S'}))

# 共源共栅：同型串联，甲漏=乙源，且中间网独占（度=2）
CAS_N = _tpl(('m0', 'N', {'D': 'DA', 'G': 'GA', 'S': _n(0)}),
             ('m1', 'N', {'D': _n(0), 'G': 'GB', 'S': 'SB'}))
CAS_P = _tpl(('m0', 'P', {'D': 'DA', 'G': 'GA', 'S': _n(0)}),
             ('m1', 'P', {'D': _n(0), 'G': 'GB', 'S': 'SB'}))

# ---------- 新模板：参数化 ----------
def _mirror_tpl(model, n):
    """多路电流镜：m0 二极管参考管(D=G=DA)，其余 m1..mn 漏各异、栅接 DA、共源 S
       （照搬 CMB_NMOS_2/3/4：M0 DA DA S / M1..Mn D? DA S）"""
    devs = [('m0', model, {'D': 'DA', 'G': 'DA', 'S': 'S'})]
    devs += [(f'm{i}', model, {'D': f'D{i}', 'G': 'DA', 'S': 'S'}) for i in range(1, n + 1)]
    return _tpl(*devs)

CMB3_N = _mirror_tpl('N', 2)   # 3 管：1 参考 + 2 输出
CMB3_P = _mirror_tpl('P', 2)
CMB4_N = _mirror_tpl('N', 3)   # 4 管
CMB4_P = _mirror_tpl('P', 3)

# 二极管连接负载（DCL）：单管 D=G（照搬 basic_template.sp DCL_NMOS/PMOS: M1 D D S B）
DCL_N = _tpl(('m0', 'N', {'D': 'D', 'G': 'D', 'S': 'S'}))
DCL_P = _tpl(('m0', 'P', {'D': 'D', 'G': 'D', 'S': 'S'}))

# 传输门（tgate）：N、P 源漏互并，栅各异
#   M0 D GA S   M1 D GB S
TGATE = _tpl(('m0', 'N', {'D': 'D', 'G': 'GA', 'S': 'S'}),
             ('m1', 'P', {'D': 'D', 'G': 'GB', 'S': 'S'}))

# 并联组（同源同漏，栅可异）——比"三网全同"宽，覆盖 M2/M3 型
PSG_N = _tpl(('m0', 'N', {'D': 'D', 'G': 'GA', 'S': 'S'}),
             ('m1', 'N', {'D': 'D', 'G': 'GB', 'S': 'S'}))
PSG_P = _tpl(('m0', 'P', {'D': 'D', 'G': 'GA', 'S': 'S'}),
             ('m1', 'P', {'D': 'D', 'G': 'GB', 'S': 'S'}))

TEMPLATES = {
    '交叉耦合':  [CCP_N, CCP_P],          # 最特异，先查
    '传输门':    [TGATE],                 # N/P 源漏互并
    '多路电流镜': [CMB4_N, CMB4_P, CMB3_N, CMB3_P],  # 大的先查（4 管先于 3 管）
    '电流镜':    [SCM_N, SCM_P],          # 含二极管参考管
    '并联组':    [PSG_N, PSG_P],          # 同源同漏（栅可异）
    'CMOS互补':  [INV],                   # P-N 对
    '差分对':    [DP_N, DP_P],
    '二极管负载': [DCL_N, DCL_P],         # 兜底（最宽，1 管）
}

# 需要"同结构内器件不重复报"的种类
DISJOINT_KINDS = {'差分对', 'CMOS互补', '并联组', '二极管负载', '多路电流镜'}


def find_matches(big, tpl, devices=None):
    """VF2 子图同构，返回 [(器件组, 完整映射)]，去重
       完整映射：{大图节点: 模板节点}（含器件和网）
       devices 给定时做器件引脚名校验（对齐 ALIGN：模板器件的 pin 名集合
       必须出现在大图器件碰模板的边的 pin 名集合里）——拓扑对了但引脚名
       不对（比如 D/G 都叫 DA 的二极管接法）不算匹配。"""
    gm = GraphMatcher(big, tpl, node_match=node_match, edge_match=edge_match)
    found = {}
    for m in gm.subgraph_isomorphisms_iter():
        devs = tuple(sorted(k for k in m if big.nodes[k].get('inst')))
        if not devs:
            continue
        if devices is not None and not _pin_names_ok(big, tpl, m, devices):
            continue
        # VF2 uses sets internally. Select roles and overlap priority explicitly
        # so a new interpreter/hash seed cannot change the placement constraints.
        rank = tuple(sorted((repr(k), repr(v)) for k, v in m.items()))
        old = found.get(devs)
        if old is None or rank < old[0]:
            found[devs] = (rank, m)
    return [(list(devs), found[devs][1]) for devs in sorted(found)]


def _pin_names_ok(big, tpl, m, devices):
    """逐器件核对引脚名：模板器件 t 在模板边 (t, net_t) 上的 pin 名集合，
       大图对应器件 g 在边 (g, net_g) 上的 pin 名集合，必须完全相等。"""
    inv = {v: k for k, v in m.items()}          # 模板节点 → 大图节点
    for t in tpl.nodes:
        if not tpl.nodes[t].get('inst'):
            continue
        g = inv[t]
        d = devices[g]
        real = {}
        if d['cls'] == 'MOS':
            real = {'D': d['d'], 'G': d['g'], 'S': d['s']}
        elif d['cls'] == 'BJT':
            real = {'C': d['c'], 'B': d['b'], 'E': d['e']}
        for net_t in tpl.neighbors(t):
            net_g = inv.get(net_t)
            if net_g is None or not big.has_edge(g, net_g):
                return False
            real_net = net_g[1] if isinstance(net_g, tuple) else net_g
            if tpl[t][net_t]['pin'] != {p for p, nt in real.items()
                                        if nt == real_net}:
                return False
    return True


def disjoint(found):
    """同结构内去重：器件已属于本结构某组则不重复报"""
    kept, used = [], set()
    for g, nets in found:
        s = set(g)
        if s & used:
            continue
        used |= s
        kept.append((g, nets))
    return kept


def mid_exclusive(G, devs, tpl):
    """共源共栅：中间网（模板里 @n0）在大图中度==2（独占串联）"""
    mid = next(n for n in tpl.nodes if str(n).startswith('@n'))
    return G.degree(mid) == 2


def analyze(path, cell=None):
    devices = dl.parse_netlist(path, cell=cell)
    G = build_graph(devices)
    ndev = sum(1 for n in G.nodes if G.nodes[n].get('inst'))
    if ndev == 0:
        print(f'{os.path.basename(path)}: 无 MOS 模板匹配器件')
        return []
    print(f'===== {os.path.basename(path)}  ({ndev} 器件) =====')
    grouped = set()
    results = []
    for kind, tpls in TEMPLATES.items():
        found = []
        for tpl in tpls:
            found.extend(find_matches(G, tpl, devices))
        if kind in DISJOINT_KINDS:      # 多模板种类：先按原始映射 disjoint 去重
            found = disjoint(found)
        for devs, m in found:
            devs_set = set(devs)
            if kind in ('交叉耦合', '传输门', '二极管负载', '多路电流镜'):
                devs_set -= grouped      # 全局只留新器件，同一行去重报
            if not devs_set:
                continue
            grouped |= devs_set
            # m: {大图节点: 模板节点}（含器件和网）
            # 器件角色：大图器件 → 模板器件名（m0/m1/...）
            role_map = {k: v for k, v in m.items() if big_inst(G, k)}
            roles = '  '.join(f'{role_map[d]}:{d}' for d in sorted(devs_set))
            # 网角色：模板网名 → 大图网名（只列有模板名的，便于看连接关系）
            net_map = {(k[1] if isinstance(k, tuple) else k): v
                       for k, v in m.items() if not big_inst(G, k)}
            nets_str = '  '.join(f'{v}:{k}' for k, v in sorted(net_map.items()))
            print(f'  {kind}: {{{" ".join(sorted(devs_set))}}}  ({roles})  [{nets_str}]')
            results.append({'kind': kind, 'devices': sorted(devs_set),
                            'roles': {d: role_map[d] for d in sorted(devs_set)},
                            'nets': {v: k for k, v in sorted(net_map.items(), key=lambda item: item[1])}})
    lone = sorted(n for n in G.nodes if G.nodes[n].get('inst') and n not in grouped)
    print(f'  未成组: {" ".join(lone) if lone else "无"}')
    print()
    return results


def big_inst(G, n):
    return n in G and G.nodes[n].get('inst')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Find MOS analog substructures with VF2.')
    parser.add_argument('netlists', nargs='+')
    parser.add_argument('--cell', help='select one .subckt without recursive expansion')
    args = parser.parse_args(argv)
    try:
        for path in args.netlists:
            analyze(path, cell=args.cell)
    except (dl.NetlistError, OSError) as exc:
        parser.exit(2, f'error: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
