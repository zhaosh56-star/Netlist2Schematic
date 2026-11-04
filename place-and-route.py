#!/usr/bin/env python3
"""Demo M0-M2: netlist -> parse -> random placement -> SA placement ->
coarse Manhattan-MST routing -> metrics + SVG board.

SA moves: relocate a component to a free cell / swap two components of the
same family (nmos<->pmos, npn<pnp allowed) / change orientation (mirror only
for mos/bjt). After placement, each net is routed as a Manhattan MST built
by Prim on full pin-to-pin Manhattan distances (min-total-distance seed is
Prim's first edge), then L-shaped; metrics: total wire length, crossings,
bends. Power nets (VDD/0/GND/VSS) are excluded from placement, routing and
energy; the SVG only draws a cosmetic stub + vdd/ground glyph at each
power-connected pin.

Run: python3 place-and-route.py example/mmba88q6zx.cir  -> demo-board.svg
"""
import json, math, os, random, sys
from sa_common import load_local, read_json, resource_root

HERE = os.path.dirname(os.path.abspath(__file__))
parse_netlist = load_local("parse_netlist", "parse-netlist.py")

GEO = os.path.join(HERE, "symbol-geometry.json")

N = 12
CELL = 150
W = H = N * CELL
SEED = 20261026
SA_ITERS = 20000
SNAPSHOTS = [1000, 2000, 3000]   # save placement at these iterations
T0, T1 = 40.0, 0.5

FAMILY = {  # swappable groups
    "nmos": "mos", "pmos": "mos", "npn": "bjt", "pnp": "bjt",
}
SWAPPABLE = {"mos": {"nmos", "pmos"}, "bjt": {"npn", "pnp"}}
MIRRORABLE = {"nmos", "pmos", "npn", "pnp"}
POWER_NETS = {"VDD": "vdd-port", "0": "ground", "GND": "ground", "VSS": "ground"}

def power_marker_sym(net):
    """Case-insensitive power-net lookup: netlists from the wild use
    vdd/gnds/VDD! etc. Returns the marker symbol id or None."""
    if net in POWER_NETS:
        return POWER_NETS[net]
    nl = net.lower()
    if nl.rstrip('!') in ("vdd", "vcc", "vpwr", "avdd", "dvdd"):
        return "vdd-port"
    if nl.rstrip('!') in ("0", "gnd", "vss", "gnds", "agnd", "dgnd"):
        return "ground"
    return None

GEO_DOC = read_json(GEO)["symbols"]

def pin_pos(sym, orient_idx, pin_name):
    """Symbol-local pin coords for a given orientation variant."""
    for p in GEO_DOC[sym]["orientations"][orient_idx]["pins"]:
        if p["name"] == pin_name:
            return p["at"]["x"], p["at"]["y"]
    raise KeyError(f"{sym} orient {orient_idx} has no pin {pin_name}")

# ---------------- placement model ----------------
class Placement:
    def __init__(self, comps, rng):
        self.comps = comps            # list of dicts (includes power markers)
        self.rng = rng
        self.orient = [0] * len(comps)
        self.cell_of = [None] * len(comps)
        self.comp_at = {}
        self.grid_size = max(N, math.ceil(math.sqrt(len(comps))))
        cells = [(r, c) for r in range(self.grid_size) for c in range(self.grid_size)]
        rng.shuffle(cells)
        for i, cell in enumerate(cells[:len(comps)]):
            self.cell_of[i] = cell
            self.comp_at[cell] = i
        for i, comp in enumerate(comps):
            nvar = len(GEO_DOC[comp["symbol"]]["orientations"])
            self.orient[i] = rng.randrange(nvar)

    def n_orients(self, i):
        return len(GEO_DOC[self.comps[i]["symbol"]]["orientations"])

    def pos_of(self, i):
        r, c = self.cell_of[i]
        if hasattr(self, 'column_x') and hasattr(self, 'row_y'):
            return self.column_x[c], self.row_y[r]
        return c * CELL + CELL / 2, r * CELL + CELL / 2

    def pin_world(self, i, pin_name):
        cx, cy = self.pos_of(i)
        lx, ly = pin_pos(self.comps[i]["symbol"], self.orient[i], pin_name)
        return cx + lx, cy + ly

    def pin_dir(self, i, pin_name):
        for p in GEO_DOC[self.comps[i]["symbol"]]["orientations"][self.orient[i]]["pins"]:
            if p["name"] == pin_name:
                return p["direction"]
        raise KeyError(f"no pin {pin_name}")

# ---------------- coarse routing (Manhattan MST per net) ----------------
DIR_VEC = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}

def mst_edges(points):
    """Prim over complete graph with Manhattan metric. Returns index pairs."""
    n = len(points)
    if n < 2:
        return []
    in_tree = {0}
    edges = []
    dist = [abs(points[0][0]-points[k][0]) + abs(points[0][1]-points[k][1])
            for k in range(n)]
    parent = [0] * n
    for _ in range(n - 1):
        k = min((k for k in range(n) if k not in in_tree), key=lambda k: dist[k])
        edges.append((parent[k], k))
        in_tree.add(k)
        for j in range(n):
            if j not in in_tree:
                d = abs(points[k][0]-points[j][0]) + abs(points[k][1]-points[j][1])
                if d < dist[j]:
                    dist[j], parent[j] = d, k
    return edges

MARGIN = 12  # clearance halo around component bodies: grazing/passing wires
             # inside the halo are penalized; escape stubs extend past it

def l_path(a, b):
    """L-shaped Manhattan path a->b (horizontal first)."""
    if a == b:
        return [a]
    if a[0] == b[0] or a[1] == b[1]:
        return [a, b]
    return [a, (b[0], a[1]), b]

def escape_pt(net, i, pin, pl, net_rects):
    """Extend outward from the pin along its direction, in pin-grid steps,
    until the stub tip clears every own-component body box plus MARGIN."""
    x, y = pl.pin_world(i, pin)
    dx, dy = DIR_VEC[pl.pin_dir(i, pin)]
    for _ in range(60):
        if not any(x0 - MARGIN <= x <= x1 + MARGIN and
                   y0 - MARGIN <= y <= y1 + MARGIN
                   for x0, y0, x1, y1 in net_rects[net]):
            return (x, y)
        x += dx * 2
        y += dy * 2
    return (x, y)

def channel_l_path(a, b, pl, comp_index):
    """L-path with bend preference for channel centerlines between columns."""
    if a == b or a[0] == b[0] or a[1] == b[1]:
        return l_path(a, b)
    # candidate bends: (b.x, a.y) and (a.x, b.y)
    cand1 = (b[0], a[1])
    cand2 = (a[0], b[1])
    # score each candidate: prefer bend at channel center between columns
    def chan_score(p):
        col = p[0] / CELL - 0.5
        frac = col - int(col)
        # best if bend is near column boundary (frac near 0 or 1)
        return min(frac, 1 - frac)
    s1, s2 = chan_score(cand1), chan_score(cand2)
    if s1 < s2:
        return [a, cand1, b]
    return [a, cand2, b]

def z_path_detours(a, b, rect):
    """Z candidates a->b clearing rect: push the mid lane to the rect's edge,
    both orientations (horizontal-mid / vertical-mid), near edge first.
    Edge-grazing is clean (seg_hits_rect uses strict <)."""
    x0, y0, x1, y1 = rect
    ys = sorted((y0, y1), key=lambda t: abs(a[1] - t))
    xs = sorted((x0, x1), key=lambda t: abs(a[0] - t))
    return [[a, (a[0], y), (b[0], y), b] for y in ys] + \
           [[a, (x, a[1]), (x, b[1]), b] for x in xs]

def l_path_avoid_all(a, b, rects):
    """L or Z a->b that clears EVERY rect (own + foreign halos alike); no
    endpoint exemptions — escape points already sit outside their own halos.
    Both L orders first; then Z-detours around each rect blocking the first
    candidate (nearest first, both orientations); first fully clean wins;
    nothing clears -> the first L candidate."""
    c1, c2 = (b[0], a[1]), (a[0], b[1])
    first = None
    for cand in (c1, c2):
        path = [a] + ([cand] if cand != a and cand != b else []) + [b]
        if first is None:
            first = path
        if all(path_ok(path, r) for r in rects):
            return path
    dirty = sorted((r for r in rects if not path_ok(first, r)),
                   key=lambda r: (r[0] + r[2] - a[0] - b[0]) ** 2 +
                                 (r[1] + r[3] - a[1] - b[1]) ** 2)
    for r in dirty:
        for z in z_path_detours(a, b, r):
            if all(path_ok(z, rr) for rr in rects):
                return z
    return first

def seg_hits_rect(a, b, r):
    """Does axis segment a-b pass through rect (x0,y0,x1,y1)? Touches count."""
    x0, y0, x1, y1 = r
    if a[0] == b[0]:  # vertical
        return x0 < a[0] < x1 and max(min(a[1], b[1]), y0) < min(max(a[1], b[1]), y1)
    return y0 < a[1] < y1 and max(min(a[0], b[0]), x0) < min(max(a[0], b[0]), x1)

def seg_touches_rect(a, b, r):
    """Like seg_hits_rect but grazing the rect's border also counts."""
    x0, y0, x1, y1 = r
    if a[0] == b[0]:  # vertical
        return x0 <= a[0] <= x1 and max(min(a[1], b[1]), y0) < min(max(a[1], b[1]), y1)
    return y0 <= a[1] <= y1 and max(min(a[0], b[0]), x0) < min(max(a[0], b[0]), x1)

def path_ok_touch(path, rect):
    return all(not seg_touches_rect(p, q, rect) for p, q in zip(path, path[1:]))

def path_ok(path, rect):
    return all(not seg_hits_rect(p, q, rect) for p, q in zip(path, path[1:]))

# ---- lane occupancy (final pass only): segments of nets already routed ----
def lanes_add(lanes, net, a, b):
    """Register segment a-b of net into the lane table keyed by (axis, track)."""
    if a[0] == b[0]:
        key = ("v", a[0]); lo, hi = sorted((a[1], b[1]))
    else:
        key = ("h", a[1]); lo, hi = sorted((a[0], b[0]))
    if lo != hi:
        lanes.setdefault(key, []).append((lo, hi, net))

def lane_dirty(a, b, lanes, net):
    """Does segment a-b share a track with a FOREIGN net's segment (overlap>0)?"""
    if a[0] == b[0]:
        key = ("v", a[0]); lo, hi = sorted((a[1], b[1]))
    else:
        key = ("h", a[1]); lo, hi = sorted((a[0], b[0]))
    for lo2, hi2, n2 in lanes.get(key, ()):
        if n2 != net and min(hi, hi2) > max(lo, lo2):
            return True
    return False

def path_lane_dirty(path, lanes, net):
    return any(lane_dirty(p, q, lanes, net) for p, q in zip(path, path[1:]))

def path_rect_dirty(path, rects):
    return any(not path_ok(path, r) for r in rects)

def lane_halo_margin(seg, d, halos):
    """Prefer sliding away from devices: score = clearance between the moved
    segment's new track and the halo edge on the movement side (0 when the
    seg grazes the halo, larger when it lands outside / farther away)."""
    if seg[0][0] == seg[1][0]:              # vertical seg moves in x
        t, lo, hi = seg[0][0] + d, min(seg[0][1], seg[1][1]), max(seg[0][1], seg[1][1])
        edges = lambda r: (r[0], r[2])
        span = lambda r: (r[1], r[3])
    else:                                   # horizontal seg moves in y
        t, lo, hi = seg[0][1] + d, min(seg[0][0], seg[1][0]), max(seg[0][0], seg[1][0])
        edges = lambda r: (r[1], r[3])
        span = lambda r: (r[0], r[2])
    best = 0.0
    for r in halos:
        rlo, rhi = edges(r)
        if not (rlo <= t <= rhi):
            continue                        # moved seg lands outside this halo
        s0, s1 = span(r)
        if min(hi, s1) <= max(lo, s0):
            continue                        # no shared span along the track
        edge = rlo if d > 0 else rhi        # the halo side we approach from
        best = min(best, abs(t - edge)) if best else abs(t - edge)
    return best

def lane_shift(a, b, lanes, net, halos, gaps=(9, -9, 21, -21),
               score_halos=None):
    """Off-lane dodge: slide the leg(s) that sit on a foreign lane sideways by
    a small gap, auto-splicing short connector legs (whole-leg move => no
    stranded joints). First fully clean (lanes + halos) candidate wins.
    score_halos: gap ordering is scored against this (fuller) halo list so a
    dodge away from the own-pin halo wins over one diving into it."""
    score_halos = score_halos if score_halos is not None else halos
    if a[0] == b[0] or a[1] == b[1]:      # straight: slide the one leg
        try_gaps = sorted(gaps, key=lambda g: -lane_halo_margin((a, b), g, score_halos))
        for g in try_gaps:
            if a[0] == b[0]:
                p = [(a[0] + g, a[1]), (b[0] + g, b[1])]
                path = [a, p[0], p[1], b]
            else:
                p = [(a[0], a[1] + g), (b[0], b[1] + g)]
                path = [a, p[0], p[1], b]
            clean = [path[0]]
            for q in path[1:]:
                if q != clean[-1]:
                    clean.append(q)
            if len(clean) > 1 and not path_lane_dirty(clean, lanes, net) \
                    and not path_rect_dirty(clean, halos):
                return clean
        return None
    c1, c2 = (b[0], a[1]), (a[0], b[1])
    for cand in (c1, c2):
        legs = [(a, cand), (cand, b)]
        dirty = [lane_dirty(p, q, lanes, net) for p, q in legs]
        if not any(dirty):
            continue                      # handled by plain L selection
        base = [a] + ([cand] if cand != a and cand != b else []) + [b]
        dk = 0 if (dirty[0] and len(base) == 3) else (1 if dirty[1] else None)
        dpq = (base[dk], base[dk + 1]) if dk is not None and dk < len(base) - 1 else None
        try_gaps = sorted(gaps, key=lambda g: -lane_halo_margin(dpq, g, score_halos)) if dpq else gaps
        for g in try_gaps:
            if dirty[0] and len(base) == 3:
                k = 0                     # first leg is the dirty one
            elif dirty[1]:
                k = 1
            else:
                continue
            if k >= len(base) - 1:
                continue
            p, q = base[k], base[k + 1]
            if p[0] == q[0]:
                p2, q2 = (p[0] + g, p[1]), (q[0] + g, q[1])
            else:
                p2, q2 = (p[0], p[1] + g), (q[0], q[1] + g)
            path = base[:k] + [p2, q2] + base[k + 2:]
            if path[0] != a:              # k=0 dodge dropped the a anchor:
                path = [a] + path         # splice back the short a -> p2 leg
            if path[-1] != b:             # k=1 dodge dropped the b anchor:
                path = path + [b]         # splice back the short q2 -> b leg
            clean = [path[0]]
            for qq in path[1:]:
                if qq != clean[-1]:
                    clean.append(qq)
            if len(clean) > 1 and not path_lane_dirty(clean, lanes, net) \
                    and not path_rect_dirty(clean, halos):
                return clean
    return None

def l_path_dogleg(a, b, ea, eb, pl, comp_index, own_rect=None, halos=None):
    """Straight when pins are aligned; otherwise escape both pins outward
    past the body box and join the escape points with an L. Final pass
    (own_rect/halos given): the join must clear every device halo, own and
    foreign alike — both L orders, then Z-detours around blocking halos
    (nearest first, either orientation); escape points already sit outside
    their own halos, so no endpoint exemption is needed."""
    if a == b or a[0] == b[0] or a[1] == b[1]:
        return l_path(a, b)
    path = [a]
    if ea != a:
        path.append(ea)
    if eb != b:
        path.append(eb)
    path.append(b)
    out = [path[0]]
    rects = None
    if own_rect is not None or halos:
        rects = list(own_rect) if isinstance(own_rect, list) \
            else ([own_rect] if own_rect else [])
        rects += list(halos or ())
    for p in path[1:]:
        if rects is not None:
            # final pass: escape points sit outside every halo, so the join
            # must clear ALL of them — no endpoint exemption
            seg_fn = lambda u, v: l_path_avoid_all(u, v, rects)
        else:
            seg_fn = lambda u, v: channel_l_path(u, v, pl, comp_index)
        for q in seg_fn(out[-1], p)[1:]:
            if q != out[-1]:
                out.append(q)
    return out

def steiner_refine(nodes, edges, blocked, strict=False):
    """Iteratively merge MST edge pairs that share a node: if the median
    point of the three endpoints (the optimal Steiner point in the
    rectangle of the common edges) shortens the tree and clears every
    component body, replace the two edges with three edges to that
    point. Repeats until no new Steiner point is generated.
    strict=True (final pass only): the candidate must also clear every
    blocked halo (body+MARGIN, boundary inclusive); a dirty median is
    nudged outward along the 4 axis directions in 2px steps up to 20px,
    first clean and still-shorter spot wins; all dirty -> keep the two
    MST edges."""
    edges = list(edges)
    def dist(i, j):
        return abs(nodes[i][0] - nodes[j][0]) + abs(nodes[i][1] - nodes[j][1])
    def in_body(p):
        return any(x0 <= p[0] <= x1 and y0 <= p[1] <= y1
                   for x0, y0, x1, y1 in blocked)
    def in_halo(p):
        # closed-interval test: the halo boundary counts as inside, since a
        # Steiner point exactly on the halo edge still routes its legs along
        # the edge, which body_pen would flag
        return any(x0 - MARGIN <= p[0] <= x1 + MARGIN and
                   y0 - MARGIN <= p[1] <= y1 + MARGIN
                   for x0, y0, x1, y1 in blocked)
    def nudge(s):
        # outward candidates along +-x/+-y, 2px steps, up to 20px
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            for step in range(2, 22, 2):
                q = (s[0] + dx * step, s[1] + dy * step)
                if not in_body(q) and not in_halo(q):
                    yield q
                    break  # next direction
    while True:
        adj = {}
        for ei, (i, j) in enumerate(edges):
            adj.setdefault(i, []).append(ei)
            adj.setdefault(j, []).append(ei)
        applied = False
        for u, eis in adj.items():
            for a in range(len(eis)):
                for b in range(a + 1, len(eis)):
                    v = edges[eis[a]][0] if edges[eis[a]][1] == u else edges[eis[a]][1]
                    w = edges[eis[b]][0] if edges[eis[b]][1] == u else edges[eis[b]][1]
                    xs = sorted((nodes[u][0], nodes[v][0], nodes[w][0]))
                    ys = sorted((nodes[u][1], nodes[v][1], nodes[w][1]))
                    s = (xs[1], ys[1])
                    old = dist(u, v) + dist(u, w)
                    def total(p):
                        return (abs(p[0]-nodes[u][0]) + abs(p[1]-nodes[u][1])
                                + abs(p[0]-nodes[v][0]) + abs(p[1]-nodes[v][1])
                                + abs(p[0]-nodes[w][0]) + abs(p[1]-nodes[w][1]))
                    new = total(s)
                    if new < old and not in_body(s) and (not strict or not in_halo(s)):
                        cands = [s]
                    elif strict and new < old and not in_body(s):
                        # median dirty against a halo: try nudged spots
                        cands = [q for q in nudge(s) if total(q) < old]
                    else:
                        cands = []
                    for s in cands:
                        si = len(nodes)
                        nodes.append(s)
                        edges[eis[a]] = (si, v)
                        edges[eis[b]] = (si, w)
                        edges.append((si, u))
                        applied = True
                        break
                    if applied:
                        break
                if applied:
                    break
            if applied:
                break
        if not applied:
            return edges

last_halos = []

def _cross_lanes(lanes, a, b, net):
    """别网轨道中与 a-b 垂直（可能 proper cross）的所有段，端点即线段两端。"""
    if a[0] == b[0]:   # a-b 垂直，查水平轨道
        x = a[0]
        lo, hi = sorted((a[1], b[1]))
        for (axis, track), entries in lanes.items():
            if axis != 'h':
                continue
            for lo2, hi2, n2 in entries:
                if n2 == net:
                    continue
                if lo < track < hi and lo2 < x < hi2:
                    yield ((lo2, track), (hi2, track)), n2
    else:              # a-b 水平，查垂直轨道
        y = a[1]
        lo, hi = sorted((a[0], b[0]))
        for (axis, track), entries in lanes.items():
            if axis != 'v':
                continue
            for lo2, hi2, n2 in entries:
                if n2 == net:
                    continue
                if lo < track < hi and lo2 < y < hi2:
                    yield ((track, lo2), (track, hi2)), n2


def dedup_last_leg(segs, lanes=None, net=None, protected_points=()):
    """同网末腿去冗余：一条分支的"最后一腿"若被本网其它走线完全覆盖，
    就把它截到拐点处（= 把斯坦纳点沿膨出方向平移相同距离，纯删线不加线，
    因此不可能引入新的穿体/交叉；重合段消失，共线清除）。
    仅截到 T 型相接：覆盖段本身不动，交汇处由分支变 T 接，连通性保持。
    安全约束（防误削）：
      1) 只处理横平竖直腿，斜线跳过；
      2) 改名 tip→corner 后，所有还连着 tip 的线段必须仍然横平竖直，
         否则等于把别的腿掰斜，拒绝；
      3) tip 必须是交汇点（度数≥3，即斯坦纳点）——只许斯坦纳点滑向拐点，
         不许拐点滑向斯坦纳点，否则会把整条分支往主干里熨平；
      4) 改名会把连着 tip 的其它腿拉长（或压出零长），新露出的部分不许
         踩进别网已占的轨道（lanes），否则会产生新交叉（nosource N1 案例：
         短腿被截后，下方的腿被拉长 8px，恰好横穿别网横干）。
    返回 (segs, removed_px, n_trunc) 供调用方修正线长/折角统计。"""
    out = [s for s in segs if s[0] != s[1]]
    removed = 0
    n_trunc = 0
    for _ in range(4):                      # 截断可能暴露新的冗余腿
        # 端点度数：只有交汇点（斯坦纳点）才允许滑动
        deg = {}
        for a, b in out:
            deg[a] = deg.get(a, 0) + 1
            deg[b] = deg.get(b, 0) + 1
        found = None
        for a, b in out:
            if a[0] != b[0] and a[1] != b[1]:
                continue                        # 斜线不参与
            for tip, corner in ((b, a), (a, b)):     # 分支尽头 → 拐点
                if tip in protected_points:
                    continue
                if deg.get(tip, 0) < 3:
                    continue                    # tip 必须是斯坦纳点
                axis = 'h' if tip[1] == corner[1] else 'v'
                lo, hi = (sorted((tip[0], corner[0])) if axis == 'h'
                          else sorted((tip[1], corner[1])))
                if lo == hi:
                    continue
                covered = any(
                    (c, d) != (a, b) and (c, d) != (b, a)
                    and seg_overlap_len(tip, corner, c, d) == hi - lo
                    for c, d in out)
                if not covered:
                    continue
                # 改名合法性 + 别网轨道冲突检查
                ok = True
                for c, d in out:
                    if (c, d) == (a, b) or (c, d) == (b, a):
                        continue
                    if c == tip:
                        other = d
                    elif d == tip:
                        other = c
                    else:
                        continue
                    if other[0] != corner[0] and other[1] != corner[1]:
                        ok = False            # 会被掰斜，拒绝
                        break
                    if lanes is not None and net is not None:
                        # 改名后这条腿变成 corner→other：tip↔corner 段消失，
                        # corner→other 是新露出的部分。别网轨道横在同一条
                        # 轨道上（lane_dirty）或横穿新段（proper cross）都
                        # 算新冲突，拒绝。旧段 tip→other 改名前就在，不算。
                        if lane_dirty(corner, other, lanes, net):
                            ok = False
                            break
                        for (lo2, hi2), other_net in _cross_lanes(
                                lanes, corner, other, net):
                            if seg_cross(corner, other,
                                         (lo2[0], lo2[1]),
                                         (hi2[0], hi2[1])):
                                ok = False
                                break
                        if not ok:
                            break
                if ok:
                    found = (tip, corner)
                    break
            if found:
                break
        if not found:
            break
        tip, corner = found
        removed += abs(tip[0] - corner[0]) + abs(tip[1] - corner[1])
        n_trunc += 1
        # tip 全局改名为 corner：原腿两端都成 corner（被删），
        # 恰好终结在 tip 的其它分支端点跟着滑到 corner，衔接不丢。
        out = [((corner if s[0] == tip else s[0]),
                (corner if s[1] == tip else s[1])) for s in out]
        out = [s for s in out if s[0] != s[1]]
    return out, removed, n_trunc


def route(nets, pl, comp_index, avoid_own=False):
    global last_halos
    """Return per-net routed segments + aggregate metrics. avoid_own adds the
    own-body halo避让 (L→Z) — used only for the final render pass, not in SA."""
    all_segs = []          # (net, [(p,q), ...]) axis-aligned unit segments
    total_wl = 0
    bends = 0
    body_pen = 0
    # body AABBs per net (oriented viewBox of each member component)
    net_rects = {}
    for net, members in nets.items():
        rs = []
        for cid, _ in members:
            i = comp_index[cid]
            cx, cy = pl.pos_of(i)
            bb = GEO_DOC[pl.comps[i]["symbol"]]["orientations"][pl.orient[i]]["viewBoxAABB"]
            rs.append((cx + bb["x"], cy + bb["y"],
                       cx + bb["x"] + bb["width"], cy + bb["y"] + bb["height"]))
        net_rects[net] = rs
    # global body AABBs: Steiner points must not land inside any component
    all_rects = []
    for i in range(len(pl.comps)):
        cx, cy = pl.pos_of(i)
        bb = GEO_DOC[pl.comps[i]["symbol"]]["orientations"][pl.orient[i]]["viewBoxAABB"]
        all_rects.append((cx + bb["x"], cy + bb["y"],
                          cx + bb["x"] + bb["width"], cy + bb["y"] + bb["height"]))
    # inflated halos for every component (final pass): legs that merely run
    # THROUGH a halo are dirty even when both endpoints sit outside it
    all_halos = [(x0 - MARGIN, y0 - MARGIN, x1 + MARGIN, y1 + MARGIN)
                 for x0, y0, x1, y1 in all_rects]
    # final pass only: long nets route first (they dodge dearest), and every
    # routed net registers its segments so later nets steer clear of the
    # occupied lanes instead of landing on them (SA path is untouched).
    net_items = list(nets.items())
    lanes = {}
    if avoid_own:
        def _net_manhattan(item):
            _, members = item
            pts = [pl.pin_world(comp_index[cid], pin) for cid, pin in members]
            return sum(abs(pts[i][0] - pts[j][0]) + abs(pts[i][1] - pts[j][1])
                       for i, j in mst_edges(pts)) if len(pts) > 1 else 0
        net_items.sort(key=_net_manhattan, reverse=True)
    for net, members in net_items:
        pins = [(comp_index[cid], pin) for cid, pin in members]
        pts = [pl.pin_world(i, pin) for i, pin in pins]
        esc = [escape_pt(net, i, pin, pl, net_rects) for i, pin in pins]
        # own-component halo per pin (inflated body), to stop the join from
        # sweeping back through the body the pin just escaped from. Only
        # computed for the final pass (avoid_own); SA skips it entirely.
        own_halo = []
        if avoid_own:
            for i, pin in pins:
                cx, cy = pl.pos_of(i)
                bb = GEO_DOC[pl.comps[i]["symbol"]]["orientations"][pl.orient[i]]["viewBoxAABB"]
                own_halo.append((cx + bb["x"] - MARGIN, cy + bb["y"] - MARGIN,
                                 cx + bb["x"] + bb["width"] + MARGIN,
                                 cy + bb["y"] + bb["height"] + MARGIN))
        segs = []
        # RSMT: MST over escape points, then iterative Steiner refinement
        # (strict: Steiner spots must also clear every device halo; a dirty
        # median is nudged outward, all dirty -> keep the MST edges)
        nodes = list(esc)
        edges = steiner_refine(nodes, mst_edges(nodes), all_rects,
                               strict=avoid_own)
        # halos of components NOT on this net: the join must not sweep them
        foreign_halos = None
        if avoid_own:
            own_set = {comp_index[cid] for cid, _ in members}
            foreign_halos = [(all_rects[k][0] - MARGIN, all_rects[k][1] - MARGIN,
                              all_rects[k][2] + MARGIN, all_rects[k][3] + MARGIN)
                             for k in range(len(all_rects)) if k not in own_set]
        for i, j in edges:
            # the own-halo to respect is that of whichever endpoint is a pin;
            # every OTHER component's inflated halo (own-net members included)
            # is foreign to this edge: legs must not sweep them. Pin-pin edges
            # respect both pins' own halos; Steiner-Steiner edges respect all.
            own = None
            halos = None
            if avoid_own:
                halos = list(foreign_halos)
                if i < len(pins) and j < len(pins):
                    own = [own_halo[i], own_halo[j]]
                    skip = {pins[i][0], pins[j][0]}
                elif i < len(pins):
                    own = own_halo[i]
                    skip = {pins[i][0]}
                elif j < len(pins):
                    own = own_halo[j]
                    skip = {pins[j][0]}
                else:
                    skip = set()
                halos += [all_halos[k] for k in range(len(all_halos))
                          if k not in skip]
                own_list = own if isinstance(own, list) else ([own] if own else [])
            # lane table: foreign nets' segments are occupied tracks.
            # candidate order: clean L (either order) -> off-lane dodge
            # (slide the dirty leg +-10/+-20) -> give up and stay.
            if avoid_own:
                a, b = nodes[i], nodes[j]
                if a == b or a[0] == b[0] or a[1] == b[1]:
                    path = l_path(a, b)
                    if path_lane_dirty(path, lanes, net) \
                            or path_rect_dirty(path, halos + own_list):
                        dodged = lane_shift(a, b, lanes, net, halos + own_list,
                                            score_halos=all_halos)
                        if dodged and not path_rect_dirty(dodged, halos + own_list) \
                                and not path_lane_dirty(dodged, lanes, net):
                            path = dodged
                        else:
                            # both endpoints are escapes (outside every halo),
                            # so a mid Z around the blocking halo is legal —
                            # last resort before keeping the dirty straight
                            zb = l_path_avoid_all(a, b, halos + own_list)
                            if not path_rect_dirty(zb, halos + own_list) \
                                    and not path_lane_dirty(zb, lanes, net):
                                path = zb
                else:
                    c1, c2 = (b[0], a[1]), (a[0], b[1])
                    picked = None
                    for cand in (c1, c2):
                        pth = [a] + ([cand] if cand != a and cand != b else []) + [b]
                        if not path_rect_dirty(pth, halos + own_list) \
                                and not path_lane_dirty(pth, lanes, net):
                            picked = pth
                            break
                    if picked is None:
                        picked = lane_shift(a, b, lanes, net, halos + own_list,
                                            score_halos=all_halos)
                    if picked is None:
                        picked = l_path_dogleg(a, b, a, b, pl, comp_index,
                                               own, halos=halos)
                    path = picked
            else:
                path = l_path_dogleg(nodes[i], nodes[j], nodes[i], nodes[j],
                                     pl, comp_index, own, halos=halos)
            # reattach the pin->escape stub when an endpoint is a pin's
            # escape point (pin nodes are the first len(pins) nodes)
            for k, pt in ((i, nodes[i]), (j, nodes[j])):
                if k < len(pins) and pt != pts[k]:
                    for a, b in zip(l_path(pts[k], pt), l_path(pts[k], pt)[1:]):
                        wl = abs(a[0]-b[0]) + abs(a[1]-b[1])
                        if wl:
                            segs.append((a, b))
                            total_wl += wl
            bends += max(0, len(path) - 2)
            for a, b in zip(path, path[1:]):
                wl = abs(a[0]-b[0]) + abs(a[1]-b[1])
                if wl:
                    segs.append((a, b))
                    total_wl += wl
        if avoid_own:                    # publish this net's lanes
            for a, b in segs:
                lanes_add(lanes, net, a, b)
        all_segs.append((net, segs))
    if avoid_own:
        deduped = []
        for net, segs in all_segs:
            # 只给别网轨道：lane_dirty 会把本网既有段也算进去，而改名前的
            # 旧段本来就活着，不应挡住合法的斯坦纳滑动（2chy f3 案例）。
            foreign_lanes = {k: [(lo, hi, n2) for lo, hi, n2 in v if n2 != net]
                             for k, v in lanes.items()}
            segs2, removed, n_trunc = dedup_last_leg(
                segs, lanes=foreign_lanes, net=net,
                protected_points={pl.pin_world(comp_index[cid], pin)
                                  for cid, pin in nets[net]})
            total_wl -= removed
            bends -= n_trunc
            deduped.append((net, segs2))
        all_segs = deduped
    # crossings between segments of different nets (exclude shared endpoints)
    flat = [(net, a, b) for net, segs in all_segs for a, b in segs]
    cross = 0
    overlap = 0   # total px of same-direction collinear overlap between nets
    prox = 0      # px of parallel foreign wire closer than 4px (avoid_own only)
    for i in range(len(flat)):
        n1, a1, b1 = flat[i]
        for j in range(i + 1, len(flat)):
            n2, a2, b2 = flat[j]
            if n1 == n2:
                continue
            if seg_cross(a1, b1, a2, b2):
                cross += 1
            overlap += seg_overlap_len(a1, b1, a2, b2)
            if avoid_own:
                prox += seg_parallel_prox(a1, b1, a2, b2)
    # body penetrations: a segment may touch the bodies of the two components
    # it connects (its own pins live there); any other body it passes through
    # is a penalty. Endpoint-exemption only applies to segments that TOUCH
    # that body's own pins; a segment whose midpoint sits inside the body is
    # always a penalty (the 2emy IN- case: both endpoints outside halo but
    # wire passes straight through).
    for net, a, b in flat:
        endpoints = {a, b}
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        for x0, y0, x1, y1 in all_rects:
            # halo: inflate the body by MARGIN so grazing/passing wires count
            hx0, hy0, hx1, hy1 = x0 - MARGIN, y0 - MARGIN, x1 + MARGIN, y1 + MARGIN
            if a[0] == b[0]:  # vertical
                hit = (hx0 < a[0] < hx1 and
                       max(min(a[1], b[1]), hy0) < min(max(a[1], b[1]), hy1))
                through = (x0 < a[0] < x1 and y0 < mid[1] < y1)
            else:  # horizontal
                hit = (hy0 < a[1] < hy1 and
                       max(min(a[0], b[0]), hx0) < min(max(a[0], b[0]), hx1))
                through = (x0 < mid[0] < x1 and y0 < a[1] < y1)
            if not (hit or through):
                continue
            if through:
                body_pen += 1          # 穿体无豁免
            elif not any(x0 < p[0] < x1 and y0 < p[1] < y1 for p in endpoints):
                body_pen += 1          # 擦边但端点是自家引脚才豁免
    last_halos = all_halos          # stash for render_board's settle stage
    return all_segs, {"wirelength": total_wl, "crossings": cross, "bends": bends,
                      "body_pen": body_pen, "overlap": overlap, "proximity": prox}

def seg_parallel_prox(a, b, c, d, gap=4):
    """平行贴线长度(px)：两条同方向、垂直距离严格落在 (0, gap) 内的
    异网段，返回它们投影重叠的长度；其余情况返回 0。只作观测指标，
    不进 SA 能量。"""
    ah = a[1] == b[1]
    ch = c[1] == d[1]
    if ah != ch:
        return 0
    if ah:
        dist = abs(a[1] - c[1])
        if not 0 < dist < gap:
            return 0
        lo1, hi1 = sorted((a[0], b[0])); lo2, hi2 = sorted((c[0], d[0]))
    else:
        dist = abs(a[0] - c[0])
        if not 0 < dist < gap:
            return 0
        lo1, hi1 = sorted((a[1], b[1])); lo2, hi2 = sorted((c[1], d[1]))
    return max(0, min(hi1, hi2) - max(lo1, lo2))

def _clip_at_halo(a, b, halos, pins=None):
    """段 a-b 若撞 halo：只有"恰好一端在矩形内"的腿允许回缩——
    内端点不动（它要么本来就是引脚点，要么在 pins 里：推拉产生的端点），
    外端拉到矩形边外 1px；横穿/穿过矩形边的段一律拒绝（None）。
    引脚/推拉端点是布线时允许贴近 halo 的唯一端点。"""
    def inside(p, h):
        return h[0] <= p[0] <= h[2] and h[1] <= p[1] <= h[3]
    for h in halos:
        if not seg_hits_rect(a, b, h):
            continue
        ina, inb = inside(a, h), inside(b, h)
        if ina == inb:
            return None          # 两端都在内（含横穿）或穿过边：不合法
        pin, far = (a, b) if ina else (b, a)
        if pins is not None and pin not in pins:
            return None          # 内端点既不是引脚也不是推拉端点
        if a[0] == b[0]:         # 竖腿
            far = (pin[0], h[1] - 1) if far[1] < h[1] else (pin[0], h[3] + 1)
        elif a[1] == b[1]:       # 横腿
            far = (h[0] - 1, pin[1]) if far[0] < h[0] else (h[2] + 1, pin[1])
        else:
            return None
        a, b = (pin, far) if ina else (far, pin)
        if a == b:
            return None
    return (a, b)


def push_apart(routed, halos, gap=4, max_shift=16, rounds=6):
    """贴线后处理（仅最终布线）：两条异网平行段距离 <gap 时，把其中一条
    轨道按"区间"（端点×障碍物×异网 T 型连接点切分）沿垂直方向平移
    2/4/...px，直到间距 >=gap。整条轨道能推就推整条；推不动就只推
    贴线的子区间（小膨出）。候选必须：不撞任何器件 halo（连到 halo 内
    引脚点的腿自动回缩到 halo 边）、不与别网同轨重叠、不产生新的交叉、
    不制造新的贴线、推动的网保持连通。全都失败就维持原样。
    返回 (新 routed, 推开的轨道数)。"""
    def intervals_of(segs, a, b):
        """轨道上的子区间列表（按 x/y 升序），每个区间是被关键点切出来的
        可独立平移的最小单位；关键点 = 段端点 ∪ 非本轨道段的挂靠点。"""
        horiz = a[1] == b[1]
        track = a[1] if horiz else a[0]
        on = []
        off_at = {}   # 坐标 -> True：该点有离轨段挂着
        for x, y in segs:
            if horiz:
                if x[1] == y[1] == track:
                    on.append((x, y))
                else:
                    for p in (x, y):
                        if p[1] == track:
                            off_at[p[0]] = True
            else:
                if x[0] == y[0] == track:
                    on.append((x, y))
                else:
                    for p in (x, y):
                        if p[0] == track:
                            off_at[p[1]] = True
        # 在轨段并成连续链后，用端点+挂靠点切区间
        cuts = set()
        for x, y in on:
            cuts.add(x[0] if horiz else x[1])
            cuts.add(y[0] if horiz else y[1])
        for c in off_at:
            cuts.add(c)
        cuts = sorted(cuts)
        ivs = []
        for i in range(len(cuts) - 1):
            lo, hi = cuts[i], cuts[i + 1]
            if lo == hi:
                continue
            # 该区间必须在轨段覆盖内
            mid = (lo + hi) / 2
            ok = False
            for x, y in on:
                l1, h1 = sorted((x[0] if horiz else x[1], y[0] if horiz else y[1]))
                if l1 <= mid <= h1:
                    ok = True
                    break
            if ok:
                ivs.append((lo, hi))
        return horiz, track, ivs, off_at

    def try_shift_interval(routed, net, horiz, track, lo, hi, dxy, sub=None):
        """把 net 轨道上 [lo,hi] 区间平移 dxy（含两端的短连接段），校验后
        返回新 routed 或 None。sub=(s1,s2) 时只平移区间里投影与 [s1,s2]
        重叠的部分，跨界段在 sub 边界处断开（断开点接短连接段）。"""
        d = dxy[1] if horiz else dxy[0]
        orig = {frozenset((a, b)) for n, segs in routed if n == net for a, b in segs}
        # 本次推拉在轨道上引入的端点（子区间界、区间端）——回缩时允许埋在 halo 里
        coords = {lo, hi} | (set(sub) if sub else set())
        push_pts = ({(c, track) for c in coords} | {(c, track + d) for c in coords}
                    if horiz else
                    {(track, c) for c in coords} | {(track + d, c) for c in coords})
        new_routed = []
        for net2, segs in routed:
            if net2 != net:
                new_routed.append((net2, segs))
                continue
            new_segs = []
            for x, y in segs:
                if horiz and x[1] == y[1] == track:
                    l1, h1 = sorted((x[0], y[0]))
                    if sub and min(h1, sub[1]) - max(l1, sub[0]) <= 0:
                        new_segs.append((x, y))
                        continue
                    # 与 [lo,hi]（有 sub 时再∩sub）的重叠部分平移
                    o1, o2 = max(l1, lo, sub[0]) if sub else max(l1, lo), \
                             min(h1, hi, sub[1]) if sub else min(h1, hi)
                    if o1 < o2:
                        if l1 < o1:
                            new_segs.append(((l1, track), (o1, track)))
                        new_segs.append(((o1, track + d), (o2, track + d)))
                        if o1 > l1:
                            new_segs.append(((o1, track), (o1, track + d)))
                        if h1 > o2:
                            new_segs.append(((o2, track), (h1, track)))
                        if o2 < h1:
                            new_segs.append(((o2, track), (o2, track + d)))
                        # 去重（连接段可能被加两次）
                        if o1 == lo and o2 == hi and l1 == lo and h1 == hi:
                            new_segs = [s for s in new_segs
                                        if not (s[0][1] == s[1][1] == track
                                                and s[0][0] == o1 and s[1][0] == o2)]
                        continue
                    new_segs.append((x, y))
                elif not horiz and x[0] == y[0] == track:
                    l1, h1 = sorted((x[1], y[1]))
                    if sub and min(h1, sub[1]) - max(l1, sub[0]) <= 0:
                        new_segs.append((x, y))
                        continue
                    o1, o2 = max(l1, lo, sub[0]) if sub else max(l1, lo), \
                             min(h1, hi, sub[1]) if sub else min(h1, hi)
                    if o1 < o2:
                        if l1 < o1:
                            new_segs.append(((track, l1), (track, o1)))
                        new_segs.append(((track + d, o1), (track + d, o2)))
                        if o1 > l1:
                            new_segs.append(((track, o1), (track + d, o1)))
                        if h1 > o2:
                            new_segs.append(((track, o2), (track, h1)))
                        if o2 < h1:
                            new_segs.append(((track, o2), (track + d, o2)))
                        if o1 == lo and o2 == hi and l1 == lo and h1 == hi:
                            new_segs = [s for s in new_segs
                                        if not (s[0][0] == s[1][0] == track
                                                and s[0][1] == o1 and s[1][1] == o2)]
                        continue
                    new_segs.append((x, y))
                else:
                    # 离轨段：只有端点恰是区间端点的腿才跟着伸缩
                    x2, y2 = x, y
                    if horiz:
                        if x[1] == track and x[0] in (lo, hi):
                            x2 = (x[0], x[1] + d)
                        if y[1] == track and y[0] in (lo, hi):
                            y2 = (y[0], y[1] + d)
                    else:
                        if x[0] == track and x[1] in (lo, hi):
                            x2 = (x[0] + d, x[1])
                        if y[0] == track and y[1] in (lo, hi):
                            y2 = (y[0] + d, y[1])
                    if x2 != y2:
                        new_segs.append((x2, y2))
            new_routed.append((net2, new_segs))
        # 把推动网的每条新段做 halo 回缩，缩不了就失败
        fixed_routed = []
        for net2, segs in new_routed:
            if net2 != net:
                fixed_routed.append((net2, segs))
                continue
            fixed = []
            for p, q in segs:
                if frozenset((p, q)) in orig:
                    fixed.append((p, q))          # 原样保留的段本来就合法
                    continue
                if any(seg_hits_rect(p, q, h) for h in halos):
                    r = _clip_at_halo(p, q, halos, pins=push_pts)
                    if r is None:
                        return None
                    p, q = r
                fixed.append((p, q))
            fixed_routed.append((net2, fixed))
        flat_new = [(n, p, q) for n, segs in fixed_routed for p, q in segs]
        check = [(p, q) for n, segs in fixed_routed if n == net for p, q in segs]
        for p, q in check:
            if frozenset((p, q)) not in orig:
                for h in halos:
                    if seg_hits_rect(p, q, h):
                        return None
            for n2, c, d2 in flat_new:
                if n2 == net:
                    continue
                if seg_cross(p, q, c, d2) or seg_overlap_len(p, q, c, d2):
                    return None
                if seg_parallel_prox(p, q, c, d2, gap):
                    return None
        # 连通性：推动网段端点图必须仍把原轨道两端连起来
        adj = {}
        for p, q in check:
            adj.setdefault(p, []).append(q)
            adj.setdefault(q, []).append(p)
        # 简单 BFS 全连通：所有端点一个分量
        if adj:
            start = next(iter(adj))
            seen = {start}
            stack = [start]
            while stack:
                u = stack.pop()
                for v in adj.get(u, []):
                    if v not in seen:
                        seen.add(v)
                        stack.append(v)
            if len(seen) != len(adj):
                return None
        return fixed_routed

    moved = 0
    for _ in range(rounds):
        flat = [(net, a, b) for net, segs in routed for a, b in segs]
        hit = None
        for i in range(len(flat)):
            for j in range(i + 1, len(flat)):
                n1, a1, b1 = flat[i]
                n2, a2, b2 = flat[j]
                if n1 != n2 and seg_parallel_prox(a1, b1, a2, b2, gap):
                    hit = (flat[i], flat[j])
                    break
            if hit:
                break
        if not hit:
            break
        done = False
        for tgt, other in ((hit[0], hit[1]), (hit[1], hit[0])):
            n, a, b = tgt
            for idx, (net2, segs) in enumerate(routed):
                if net2 != n:
                    continue
                if (a, b) not in segs and (b, a) not in segs:
                    continue
                horiz, track, ivs, off_at = intervals_of(segs, a, b)
                otrack = other[2][1] if horiz else other[2][0]
                away = 1 if track > otrack else -1
                deltas = [away * k for k in (2, 4, 6, 8, 10, 12, 14, 16)
                          if k <= max_shift and abs(track + away * k - otrack) >= gap]
                # 贴线涉及的在轨区间（按与对方投影重叠的切出来）
                if horiz:
                    o_lo, o_hi = sorted((other[1][0], other[2][0]))
                else:
                    o_lo, o_hi = sorted((other[1][1], other[2][1]))
                cand = [iv for iv in ivs
                        if min(iv[1], o_hi) - max(iv[0], o_lo) > 0]
                # 先试整条轨道，再试贴线区间（小到大）；区间内只推
                # 与贴线投影重叠的那几段，不牵连整段
                order = ([(min(c[0] for c in ivs), max(c[1] for c in ivs), None)]
                         if ivs else []) + \
                        sorted(((iv[0], iv[1], (max(iv[0], o_lo), min(iv[1], o_hi)))
                                for iv in cand), key=lambda t: t[1] - t[0])
                tried = set()
                for iv0, iv1, sub in order:
                    iv = (iv0, iv1)
                    if iv in tried:
                        continue
                    tried.add(iv)
                    for dlt in deltas:
                        dxy = (0, dlt) if horiz else (dlt, 0)
                        nr = try_shift_interval(routed, n, horiz, track,
                                                iv[0], iv[1], dxy, sub)
                        if nr is not None:
                            routed = nr
                            moved += 1
                            done = True
                            break
                    if done:
                        break
                if done:
                    break
            if done:
                break
        if not done:
            break
    return routed, moved



def seg_overlap_len(a, b, c, d):
    """Collinear overlap length (px) of two same-direction, same-track segments.
    Endpoints touching give 0. Different tracks or directions give 0."""
    ah = a[1] == b[1]
    ch = c[1] == d[1]
    if ah != ch:
        return 0
    if ah:
        if a[1] != c[1]:
            return 0
        lo1, hi1 = sorted((a[0], b[0])); lo2, hi2 = sorted((c[0], d[0]))
    else:
        if a[0] != c[0]:
            return 0
        lo1, hi1 = sorted((a[1], b[1])); lo2, hi2 = sorted((c[1], d[1]))
    return max(0, min(hi1, hi2) - max(lo1, lo2))

def seg_cross(a, b, c, d):
    """Proper crossing of two axis-aligned segments (shared endpoints don't count)."""
    ah = a[1] == b[1]
    ch = c[1] == d[1]
    if ah == ch:
        return False  # parallel (collinear overlap not counted here)
    if ah:
        (hx1, hy), (hx2, _) = sorted([a, b])
        (vx, vy1), (_, vy2) = sorted([c, d], key=lambda p: p[1])
    else:
        (hx1, hy), (hx2, _) = sorted([c, d])
        (vx, vy1), (_, vy2) = sorted([a, b], key=lambda p: p[1])
    if not (hx1 < vx < hx2 and vy1 < hy < vy2):
        return False
    # touching at an endpoint of either segment is a T-join, not a crossing
    for p in (a, b):
        if p == (vx, hy):
            return False
    for p in (c, d):
        if p == (vx, hy):
            return False
    return True

# ---------------- SA ----------------
# Power nets are excluded everywhere: no marker components, no routing, no
# energy contribution. The SVG only draws a cosmetic stub + vdd/ground glyph
# at each power-connected pin so the board stays readable.
AREA_W = 400  # reward per cell^2 of occupied bounding box
VDD_UP_W = 150  # bias per row for VDD-connected comps to sit higher
BODY_PEN_W = 1000  # penalty per segment passing through a component body
OVERLAP_W = 15  # penalty per px of collinear diff-net overlap (33px ≈ 1 crossing)

def occupied_bbox_area(pl):
    cells = [c for c in pl.cell_of if c is not None]
    rows = [r for r, _ in cells]
    cols = [c for _, c in cells]
    return (max(rows) - min(rows) + 1) * (max(cols) - min(cols) + 1)

def energy(pl, nets, comp_index, vdd_rows=()):
    _, m = route(nets, pl, comp_index)
    e = (m["wirelength"] + 500 * m["crossings"] + 30 * m["bends"]
         + BODY_PEN_W * m["body_pen"]
         + OVERLAP_W * m["overlap"]
         + AREA_W * occupied_bbox_area(pl)
         + VDD_UP_W * sum(pl.cell_of[i][0] for i in vdd_rows))
    return e, m

def sa(pl, nets, comp_index, iterations=None):
    rng = pl.rng
    iterations = SA_ITERS if iterations is None else iterations
    # signal nets only; power nets excluded from routing and energy
    route_nets = {net: list(members) for net, members in nets.items()
                  if not power_marker_sym(net)}
    # comps touching VDD get a "sit higher" bias (full nets, before filtering)
    vdd_rows = {comp_index[comp_id]
                for net, members in nets.items() if power_marker_sym(net) == "vdd-port"
                for comp_id, _pin in members}
    cur_e, cur_m = energy(pl, route_nets, comp_index, vdd_rows)
    best_e, best_state = cur_e, (list(pl.cell_of), list(pl.orient))
    snaps = {}   # iter -> ((cell_of, orient), metrics)
    n_comp = len(pl.comps)
    for it in range(iterations):
        if it and it % 1000 == 0:
            print(f"SA {it}/{iterations}: E={best_e:.0f}", flush=True)
        # snapshot the state at iteration `it` before this round's move
        if it in SNAPSHOTS:
            snaps[it] = ((list(pl.cell_of), list(pl.orient)), dict(cur_m))
        t = T0 * (T1 / T0) ** (it / iterations)
        move = rng.random()
        undo = None
        if move < 0.45:  # relocate to a free cell
            i = rng.randrange(n_comp)
            old = pl.cell_of[i]
            grid_size = getattr(pl, "grid_size", N)
            r, c = rng.randrange(grid_size), rng.randrange(grid_size)
            if (r, c) in pl.comp_at:
                continue
            del pl.comp_at[old]
            pl.cell_of[i] = (r, c)
            pl.comp_at[(r, c)] = i
            undo = ("relocate", i, old)
        elif move < 0.75:  # swap same-family pair
            i, j = rng.randrange(n_comp), rng.randrange(n_comp)
            if i == j:
                continue
            fi, fj = FAMILY.get(pl.comps[i]["symbol"]), FAMILY.get(pl.comps[j]["symbol"])
            if fi is None or fi != fj:
                continue
            ci, cj = pl.cell_of[i], pl.cell_of[j]
            pl.cell_of[i], pl.cell_of[j] = cj, ci
            pl.comp_at[ci], pl.comp_at[cj] = j, i
            undo = ("swap", i, j)
        else:  # change orientation
            i = rng.randrange(n_comp)
            if pl.n_orients(i) < 2:
                continue
            old = pl.orient[i]
            pl.orient[i] = rng.randrange(pl.n_orients(i))
            if pl.orient[i] == old:
                continue
            undo = ("orient", i, old)
        new_e, new_m = energy(pl, route_nets, comp_index, vdd_rows)
        if new_e <= cur_e or rng.random() < math.exp((cur_e - new_e) / t):
            cur_e, cur_m = new_e, new_m
            if new_e < best_e:
                best_e, best_state = new_e, (list(pl.cell_of), list(pl.orient))
        else:
            if undo[0] == "relocate":
                _, i, old = undo
                cur = pl.cell_of[i]
                del pl.comp_at[cur]
                pl.cell_of[i] = old
                pl.comp_at[old] = i
            elif undo[0] == "swap":
                _, i, j = undo
                ci, cj = pl.cell_of[i], pl.cell_of[j]
                pl.cell_of[i], pl.cell_of[j] = cj, ci
                pl.comp_at[ci], pl.comp_at[cj] = j, i
            else:
                _, i, old = undo
                pl.orient[i] = old
    pl.cell_of, pl.orient = best_state
    pl.comp_at = {cell: i for i, cell in enumerate(pl.cell_of)}
    return best_e, route_nets, snaps

# ---------------- SVG output ----------------
DEFS = os.path.join(HERE, "..", "packages", "components", "definitions")

def load_def(symbol_id):
    return read_json(resource_root() / "packages" / "components" / "definitions"
                     / (symbol_id + ".json"))["symbol"]

def effective_symbol(sym):
    vid = sym.get("defaultVariantId")
    if not vid:
        return sym
    variant = next((v for v in sym["variants"] if v["id"] == vid), None)
    if not variant:
        return sym
    hidden_pins = set(variant.get("hiddenPinNames", []))
    hidden_parts = set(variant.get("hiddenPrimitiveParts", []))
    out = dict(sym)
    out["pins"] = [p for p in sym["pins"] if p["name"] not in hidden_pins]
    prims = [p for p in sym["primitives"] if p.get("part") not in hidden_parts]
    out["primitives"] = prims + variant.get("additionalPrimitives", [])
    return out

def fmt(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")

def svg_symbol_parts(sym, rot, mirror):
    """Symbol primitives in a translate/rotate/scale group, matching geometry.ts."""
    sx, sy = {"none": (1, 1), "horizontal": (-1, 1),
              "vertical": (1, -1), "both": (-1, -1)}[mirror]
    parts = [f'<g transform="scale({sx} {sy}) rotate({rot})">']
    for prim in sym["primitives"]:
        st = prim.get("style", {})
        sw = st.get("strokeWidth", 2)
        style = (f"stroke-linecap:{st.get('lineCap','butt')};"
                 f"stroke-linejoin:{st.get('lineJoin','miter')};"
                 f"stroke-miterlimit:{st.get('miterLimit',4)}")
        k = prim["kind"]
        if k == "line":
            f, t = prim["from"], prim["to"]
            parts.append(f'<line x1="{fmt(f["x"])}" y1="{fmt(f["y"])}" x2="{fmt(t["x"])}" y2="{fmt(t["y"])}" stroke="#111" stroke-width="{sw}" style="{style}"/>')
        elif k == "polyline":
            pstr = " ".join(f'{fmt(p["x"])},{fmt(p["y"])}' for p in prim["points"])
            parts.append(f'<polyline points="{pstr}" fill="none" stroke="#111" stroke-width="{sw}" style="{style}"/>')
        elif k == "polygon":
            pstr = " ".join(f'{fmt(p["x"])},{fmt(p["y"])}' for p in prim["points"])
            fc = "#111" if prim.get("fill") == "foreground" else "none"
            stc = "none" if prim.get("stroke") == "none" else "#111"
            parts.append(f'<polygon points="{pstr}" fill="{fc}" stroke="{stc}" stroke-width="{sw}" style="{style}"/>')
        elif k == "circle":
            c = prim["center"]
            fill = "#111" if prim.get("fill") == "foreground" else "none"
            stc = "none" if prim.get("stroke") == "none" else "#111"
            parts.append(f'<circle cx="{fmt(c["x"])}" cy="{fmt(c["y"])}" r="{fmt(prim["radius"])}" fill="{fill}" stroke="{stc}" stroke-width="{sw}" style="{style}"/>')
        elif k == "path":
            parts.append(f'<path d="{prim["data"]}" fill="none" stroke="#111" stroke-width="{sw}" style="{style}"/>')
    parts.append("</g>")
    return "".join(parts)

def spread_overlaps(routed, gap=10, halos=()):
    """Settle stage (user's 3 rules): a foreign collinear overlap is resolved
    by moving the SHORTER net (total wirelength); the move slides the net's
    WHOLE collinear run on that track and drags every perpendicular connector
    endpoint along (0 new bends), preferring the direction away from device
    halos; if the whole-run move is blocked (halo or third net), fall back to
    shifting just the overlapping run with short splice legs; if every
    candidate stays dirty, keep geometry and report. Re-checks after each
    move; <=20 settle rounds. Never disconnects; same-net overlaps are left
    alone (electrically one node)."""
    def key(a, b):
        return (a, b) if a <= b else (b, a)
    segs = {}                       # net -> list of [a, b] (mutable points)
    for net, ss in routed:
        for a, b in ss:
            if a != b:
                segs.setdefault(net, []).append([a, b])
    wl_of = {net: sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in ss)
             for net, ss in segs.items()}
    stuck = set()                   # pair fingerprints we failed to fix

    def connected(net):
        """Union-find over net's segments: touching/crossing segments union.
        False if the net has been torn into >1 component (a move that
        disconnects must be rolled back, even if it clears the overlap)."""
        ss = [s for s in segs[net] if s[0] != s[1]]
        if len(ss) < 2:
            return True
        parent = list(range(len(ss)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def touch(a, b, c, d):
            ah = a[1] == b[1]
            ch = c[1] == d[1]
            if ah and ch:
                if a[1] != c[1]:
                    return False
                lo1, hi1 = sorted((a[0], b[0]))
                lo2, hi2 = sorted((c[0], d[0]))
                return min(hi1, hi2) >= max(lo1, lo2)
            if not ah and not ch:
                if a[0] != c[0]:
                    return False
                lo1, hi1 = sorted((a[1], b[1]))
                lo2, hi2 = sorted((c[1], d[1]))
                return min(hi1, hi2) >= max(lo1, lo2)
            h, v = ((a, b), (c, d)) if ah else ((c, d), (a, b))
            (hx1, hy), (hx2, _) = sorted(h)
            (vx, vy1), (_, vy2) = sorted(v, key=lambda p: p[1])
            return hx1 <= vx <= hx2 and vy1 <= hy <= vy2

        for i in range(len(ss)):
            for j in range(i + 1, len(ss)):
                if touch(*ss[i], *ss[j]):
                    pi, pj = find(i), find(j)
                    if pi != pj:
                        parent[pi] = pj
        return len({find(i) for i in range(len(ss))}) == 1

    for _ in range(20):
        found = None
        names = list(segs)
        for i in range(len(names)):
            if found:
                break
            for j in range(i + 1, len(names)):
                n1, n2 = names[i], names[j]
                for a, b in segs[n1]:
                    for c, d in segs[n2]:
                        if seg_overlap_len(a, b, c, d):
                            fp = (n1, key(a, b), n2, key(c, d))
                            if fp in stuck:
                                continue
                            found = (n1, (a, b), n2, (c, d), fp)
                            break
                    if found:
                        break
                if found:
                    break
        if not found:
            break
        n1, p1, n2, p2, fp = found
        mover = n1 if wl_of.get(n1, 0) <= wl_of.get(n2, 0) else n2
        a, b = p1 if mover == n1 else p2
        vertical = a[0] == b[0]
        # candidate shifts: +-gap then +-2*gap, ordered away-from-halos first;
        # gaps snapped to the 3px grid the router works on
        g1, g2 = round(gap / 3) * 3, round(2 * gap / 3) * 3
        cands = [(g1, 0), (-g1, 0), (g2, 0), (-g2, 0)] if vertical \
            else [(0, g1), (0, -g1), (0, g2), (0, -g2)]
        def halo_margin(dxy):
            na = (a[0] + dxy[0], a[1] + dxy[1])
            nb = (b[0] + dxy[0], b[1] + dxy[1])
            best = 1e9
            for x0, y0, x1, y1 in halos:
                if vertical:
                    if y1 < min(na[1], nb[1]) or y0 > max(na[1], nb[1]):
                        continue
                    d = min(abs(na[0] - x0), abs(na[0] - x1))
                else:
                    if x1 < min(na[0], nb[0]) or x0 > max(na[0], nb[0]):
                        continue
                    d = min(abs(na[1] - y0), abs(na[1] - y1))
                best = min(best, d)
            return best
        cands.sort(key=halo_margin, reverse=True)

        def clean(na, nb):
            if na == nb:
                return False
            if any(seg_hits_rect(na, nb, r) for r in halos):
                return False
            for n3, ss in segs.items():
                if n3 == mover:
                    continue
                for c, d in ss:
                    if seg_overlap_len(na, nb, c, d) or seg_cross(na, nb, c, d):
                        return False
            return True

        def try_shift(dxy, whole):
            """Move every seg of mover on this track (whole=True) or just the
            overlapping run (whole=False); drag connector endpoints along.
            Returns (moves, ok)."""
            track = a[0] if vertical else a[1]
            moves = []
            for s in segs[mover]:
                sa, sb = s
                on = (vertical and sa[0] == sb[0] == track) or \
                     (not vertical and sa[1] == sb[1] == track)
                if not on:
                    continue
                if not whole and key(sa, sb) != key(a, b):
                    continue
                moves.append([sa, sb,
                              (sa[0] + dxy[0], sa[1] + dxy[1]),
                              (sb[0] + dxy[0], sb[1] + dxy[1])])
            if not moves:
                return None, False
            moved_pts = {}
            for sa, sb, na, nb in moves:
                moved_pts[sa] = na
                moved_pts[sb] = nb
            base = {(m[0], m[1]) for m in moves}
            for s in segs[mover]:
                sa, sb = s
                if (sa, sb) in base:
                    continue
                na, nb = sa, sb
                if sa in moved_pts:
                    na = moved_pts[sa]
                if sb in moved_pts:
                    nb = moved_pts[sb]
                if (na, nb) != (sa, sb):
                    moves.append([sa, sb, na, nb])
            for _, _, na, nb in moves:
                if na != nb and not clean(na, nb):
                    return moves, False
            return moves, True

        done = False
        for dxy in cands:
            for whole in (True, False):
                moves, ok = try_shift(dxy, whole)
                if ok:
                    applied = []
                    for sa, sb, na, nb in moves:
                        for s in segs[mover]:
                            if s[0] == sa and s[1] == sb:
                                applied.append((s, sa, sb))
                                s[0], s[1] = na, nb
                                break
                    if not connected(mover):
                        # the slide tore this net apart: roll it back and
                        # try the next candidate (better a leftover overlap
                        # than a disconnected net)
                        for s, sa, sb in applied:
                            s[0], s[1] = sa, sb
                        continue
                    wl_of[mover] = sum(abs(s[0][0] - s[1][0]) +
                                       abs(s[0][1] - s[1][1])
                                       for s in segs[mover])
                    done = True
                    break
            if done:
                break
        if not done:
            stuck.add(fp)
            print(f"settle: 全脏退回 {fp}", flush=True)
    out = []
    for net, ss in segs.items():
        out.append((net, [(tuple(a), tuple(b)) for a, b in ss if a != b]))
    return out

NET_COLORS = ["#c62828", "#1565c0", "#2e7d32", "#6a1b9a", "#ef6c00",
              "#00838f", "#ad1457", "#558b2f", "#4527a0", "#d84315"]

STUB_LEN = 24

def render_board(pl, comps, route_nets, comp_index, defs, out, title="", nets=None):
    """Compatibility entrypoint using the shared final routing and SVG pipeline."""
    from sa_routing import final_route
    from sa_render import render_svg
    all_nets = nets if nets is not None else route_nets
    routed, metrics, validation = final_route(__import__('sys').modules[__name__], all_nets, pl, comp_index)
    render_svg(__import__('sys').modules[__name__], pl, comps, all_nets, comp_index,
               routed, metrics, out, validation=validation)
    return out

def main(argv=None):
    from run_flow import main as run_main
    return run_main(argv, default_mode='baseline')

if __name__ == "__main__":
    main()
