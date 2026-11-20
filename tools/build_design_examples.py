"""Make small, real before/after schematics for the default layout design points.

The starting placements are controlled teaching fixtures, not past releases.
Only the named layout method is run, then both sides use the same final router.
No dataset optimization, SA sweep, or modification of flow defaults is involved.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import redirect_stdout
from copy import deepcopy
from hashlib import sha256
from html import escape
import inspect
import io
import json
from pathlib import Path
import sys
from time import perf_counter
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from netlist2schematic.common.sa_common import write_json
from netlist2schematic.netlist.netlist_core import parse_netlist, to_document
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.routing.routing_rules import RoutingOptions, build_scene
from netlist2schematic.routing.sa_routing import final_route, validate_final_routes
from netlist2schematic.rendering.sa_render import render_svg
from netlist2schematic.plugins.passive_skeleton.plan import compile_plan
from netlist2schematic.plugins.passive_skeleton.placement import pack
from netlist2schematic.plugins.columns.column_layout import build_column_plan, refine_columns
from netlist2schematic.plugins.columns.branch_rows import plan_branch_correspondence
from netlist2schematic.plugins.layout_shared.branch_levels import plan_branch_rows
from netlist2schematic.layout import series_alignment
from netlist2schematic.postprocess.simplify_routes import simplify_routes
from tests.test_passive_placement import base_placement, mos, resistor, source
from tests.test_series_alignment import PAR, prepare

OPTIONS = RoutingOptions()


def frame(devices, cells, orientations=None):
    return base_placement(devices, cells, orientations=orientations)


def reference_rows():
    devices = dict(MREF=mos('bias', 'bias'), MOUT=mos('join', 'bias'),
                   MCAS=mos('output', 'cascode', 'join'))
    before, comps, nets, index = frame(devices, dict(MREF=(0, 5), MOUT=(1, 2), MCAS=(0, 2)))
    rows, report = plan_branch_correspondence(PAR, devices, comps, nets, index,
        before.row_of, analyze_structures(devices))
    after = deepcopy(before)
    after.set_state([(rows[i], col) for i, (_, col) in enumerate(before.cell_of)], after.orient)
    assert report['applied']
    return devices, before, after, comps, nets, index, report


def main_columns():
    devices = dict(M1=mos('top', 'g1', 'j1'), M2=mos('j1', 'g2', 'j2'),
                   M3=mos('j2', 'g3'), M4=mos('side', 'j1'))
    before, comps, nets, index = frame(devices, dict(M1=(0, 4), M2=(1, 2), M3=(2, 4), M4=(1, 6)))
    plan = build_column_plan(PAR, before, comps, nets, index, analyze_structures(devices))
    after, report = refine_columns(PAR, before, comps, nets, index,
        dict(structs=analyze_structures(devices), layout_style='structured', candidate_id=2),
        plan, OPTIONS)
    assert report['layout_columns']['applied'], report
    return devices, before, after, comps, nets, index, report


def aligned_pins():
    devices = dict(M1=mos('top', 'g1', 'join'), M2=mos('join', 'g2'))
    before, comps, nets, index = frame(devices, dict(M1=(0, 4), M2=(1, 1)))
    metadata = prepare(before, comps, nets, index, devices)
    after, report = series_alignment.refine_series_alignment(PAR, before, comps, nets, index,
                                                           metadata, OPTIONS)
    assert report['series_alignment']['applied'], report
    return devices, before, after, comps, nets, index, report


def child_structure():
    from netlist2schematic.layout import structured_placement as structured
    devices = dict(M1=mos('out1', 'inp', 'tail'), M2=mos('out2', 'inn', 'tail'),
                   MTAIL=mos('tail', 'bias'), MAUX=mos('aux', 'control'))
    before, comps, nets, index = frame(devices, dict(M1=(0, 1), M2=(0, 5), MTAIL=(1, 3), MAUX=(0, 3)))
    levels = compute_levels(devices)
    conduction = {d[k] for d in devices.values() for k in ('d', 's')}
    attachments = structured.legacy.attach_info(devices, levels['net_levels'], conduction)
    records = analyze_structures(devices)
    after, _, _ = structured._initial_placement(devices, comps, nets, index, levels, attachments,
        records, 83, 1, passive_layout='fixed')
    assert after.blocks_contiguous()
    assert abs(after.cell_of[index['M1']][1]-after.cell_of[index['M2']][1]) == 1
    return devices, before, after, comps, nets, index, dict(structures=records,
        explanation='Confirmed pair retains row-local contiguity; unrelated device cannot split it.')


def short_wire():
    devices = dict(M1=mos('out1', 'input'), M2=mos('out2', 'input'))
    placement, comps, nets, index = frame(devices, dict(M1=(1, 2), M2=(1, 5)), dict(M1=1, M2=0))
    routed = final_route(PAR, nets, placement, index, options=OPTIONS, deadline=perf_counter()+5)
    scene = build_scene(PAR, nets, placement, index, OPTIONS)
    a, b = sorted(scene.pins['input'], key=lambda pin: pin.point[0])
    left = a.escape[0]+24, a.escape[1]
    right = b.escape[0]-24, b.escape[1]
    top = min(rect[1] for _, rect in scene.body_rects)-70
    path = [a.point, a.escape, left, (left[0], top), (right[0], top), right, b.escape, b.point]
    segments = [(x, y) for x, y in zip(path, path[1:]) if x != y]
    old = [(net, segments if net == 'input' else lines) for net, lines in routed[0]]
    old = validate_final_routes(PAR, nets, placement, index, old, OPTIONS)
    shortened = simplify_routes(PAR, nets, placement, index, old, options=OPTIONS, seconds=1)
    assert shortened[1]['bends'] < old[1]['bends']
    report = dict(simplification=shortened[3], _route_overrides={'before': old, 'after': shortened[:3]})
    return devices, placement, deepcopy(placement), comps, nets, index, report


def insert(devices, before_cells, core_cells, *, inline=False):
    before, comps, nets, index = frame(devices, before_cells)
    plan = compile_plan(PAR, devices, comps, nets, analyze_structures(devices))
    assert plan['applicable'], plan
    core_devices = {n: devices[n] for n in core_cells}
    core, _, _, _ = frame(core_devices, core_cells)
    if inline:
        bridges = [g['conduction'] for g in plan['groups'] if g.get('conduction')]
        rows, report = plan_branch_rows(devices, compute_levels(devices), before.row_of,
                                        [], comps, index, bridges=bridges)
        core_rows = sorted({rows[index[n]] for n in core_devices})
        mapping = {value: i*2 for i, value in enumerate(core_rows)}
        cells = {n: (mapping[rows[index[n]]], core_cells[n][1]) for n in core_devices}
        core, _, _, _ = frame(core_devices, cells)
    else:
        report = {}
    after, packing = pack(PAR, core, comps, nets, index, plan, OPTIONS)
    assert after is not None, packing
    report.update(plan=plan, packing=packing)
    return devices, before, after, comps, nets, index, report


def inline_rlc():
    devices = dict(M1=mos('top', 'g1', 'up'), M2=mos('down', 'g2'),
                   R1=resistor('up', 'r'), L1=resistor('r', 'l', 'L'), C1=resistor('l', 'down', 'C'))
    return insert(devices, dict(M1=(0, 2), M2=(0, 6), R1=(2, 1), L1=(3, 4), C1=(2, 7)),
                  dict(M1=(0, 3), M2=(2, 3)), inline=True)


def parallel_module():
    devices = dict(M1=mos('left', 'g1'), M2=mos('right', 'g2'),
                   R1=resistor('left', 'middle'), R2=resistor('middle', 'right'),
                   C1=resistor('middle', 'right', 'C'))
    return insert(devices, dict(M1=(1, 1), M2=(1, 5), R1=(4, 1), R2=(3, 4), C1=(4, 7)),
                  dict(M1=(1, 1), M2=(1, 5)))


def inline_source():
    devices = dict(M1=mos('top', 'g1', 'up'), M2=mos('down', 'g2'),
                   V1=source('VSRC', 'up', 'down'))
    return insert(devices, dict(M1=(0, 2), M2=(1, 6), V1=(3, 1)),
                  dict(M1=(0, 3), M2=(2, 3)), inline=True)


def tail_source():
    devices = dict(M1=mos('out1', 'inp', 'tail'), M2=mos('out2', 'inn', 'tail'),
                   I1=source('ISRC', 'tail'))
    return insert(devices, dict(M1=(1, 2), M2=(1, 4), I1=(3, 7)),
                  dict(M1=(1, 2), M2=(1, 4)))


def terminal_2d():
    from tests.test_terminal_slots import fixture, incumbent, refine
    devices, cells, guests = fixture('V')
    before, comps, nets, index, plan = incumbent(devices, cells)
    after, report = refine((before, comps, nets, index, plan))
    assert report['applied'], report
    assert all(before.pos_of(index[n]) == after.pos_of(index[n]) for n in cells)
    return devices, before, after, comps, nets, index, report


def netlist_text(devices):
    lines = ['* Controlled teaching example; original connections are identical in both views.',
             '.model NMOS NMOS', '.model PMOS PMOS']
    for name, device in devices.items():
        if device['cls'] == 'MOS':
            lines.append(' '.join((name, device['d'], device['g'], device['s'], device['bulk'], device['type'])))
        elif device['cls'] == 'PASSIVE':
            lines.append(' '.join((name, device['a'], device['b'], device['value'])))
        else:
            lines.append(' '.join((name, device['p'], device['n'], device['value'])))
    return '\n'.join(lines)+'\n'


CASES = [
    dict(id='rows', title='没有直接接 VDD，MOS 怎么找到对应行？', factory=reference_rows,
         before='MREF 是独立短参考支路，从自己的起点计层，结果比对应的 MOUT 高一行。',
         after='识别出 MREF→MOUT 的电流镜角色；保留较长的 MCAS—MOUT 主列，将短参考支路移到对应行。',
         takeaway='先看支路角色，再修正显示行；不是所有共 G 的管子都拉到同一行。',
         limits='只有对应证据明确、上下先后不冲突才移动；原电气层级和网表不改。',
         codes=['plugins/columns/branch_rows.py', 'plugins/columns/row_estimate.py']),
    dict(id='columns', title='把长主支路保成一列，短旁支放到旁边', factory=main_columns,
         before='M1—M2—M3 本来是一条连续导通支路，但 M2 偏到左边，读起来像三段分散的连接。',
         after='将三只主支路器件规整到同一列；只接入局部节点的 M4 留在旁侧，不混进主列。',
         takeaway='先看清“一根主干”，再看旁边接了什么。',
         limits='依据唯一导通延伸或重复支路证据；有歧义的分叉不强行选成主干。',
         codes=['plugins/columns/column_layout.py']),
    dict(id='children', title='已确认的差分对，不让无关器件插到中间', factory=child_structure,
         before='M1、M2 共用 tail，并有真实尾管，但 MAUX 插在两只输入管之间，把一个小结构读散了。',
         after='识别输入对与尾部关系，初排中先放好这个局部块；MAUX 放到块外，SA 也继续保护同行小块的完整性。',
         takeaway='相关的小结构先保持完整，不能只保住一个大包围盒。',
         limits='仅保护已确认的局部角色和同行区间；共 VDD/GND 本身不能认作差分对。',
         codes=['recognition/circuit_structures.py', 'layout/structure_hierarchy.py',
                'layout/structured_placement.py']),
    dict(id='pins', title='对齐的是实际 D/S 引脚，不只是器件中心', factory=aligned_pins,
         before='上下两只 MOS 明确串接，却横向错开，连接中出现不必要的横线和拐角。',
         after='按真正相接的 S/D 引脚检查横向偏差，找到合法位置后将这段支路拉直。',
         takeaway='明确串联的地方优先直上直下。',
         limits='保留有源行号、固定器件及合法镜像；额外导通分叉存在时不假装它是唯一串联。',
         codes=['layout/series_alignment.py']),
    dict(id='inline-rlc', title='跨过 R/L/C，仍保留有源骨架的上下关系', factory=inline_rlc,
         before='RLC 把 M1 与 M2 的原导通图分成两段；若只各自计层，两只 MOS 会落到同一行，无源件散落在下方。',
         after='确认 R—L—C 是一条唯一二端串链，建立显示用的延续关系；先排好上下 MOS，再腾空间把整条链放进去。',
         takeaway='布局时“看穿这条附件链”，绘图时每个器件和内部节点都保留。',
         limits='C 只建立显示关系，不被当作直流短路；分叉、控制抽头与闭环按真实边界处理。',
         codes=['plugins/passive_skeleton/plan.py', 'plugins/layout_shared/branch_levels.py',
                'plugins/passive_skeleton/placement.py']),
    dict(id='parallel', title='串联、并联附件作为一个小模块插入', factory=parallel_module,
         before='同一两端网络里的 R1、R2、C1 各占一个远处的格子，看不出“R1 串联（R2 并联 C1）”。',
         after='先识别完整的二端串并联模块，再按真实尺寸预留通道；R2 与 C1 共用两端，R1 只出现一次。',
         takeaway='不是把无源件随手塞空位，而是把一小段电路整体放进去。',
         limits='中间有真实外部抽头就保留为模块边界；无法归约的网络不强行组成二端模块。',
         codes=['plugins/passive_skeleton/modules.py', 'plugins/passive_skeleton/module_geometry.py',
                'plugins/passive_skeleton/placement.py']),
    dict(id='inline-source', title='支路中的电压源，也按两端模块处理', factory=inline_source,
         before='V1 虽然串在两只 MOS 之间，却被独立摆到角落，主支路被拉成一个大绕行。',
         after='根据两侧真实导通端确认上下宿主，把 V1 插入这段连接中；同样的模块机制也支持独立电流源。',
         takeaway='V/I 和 RLC 可以复用插入方法，不再一律甩到图边缘。',
         limits='正负端、源值、电流箭头都保留；不是根据电压源正负号去猜电流层级。',
         codes=['plugins/passive_skeleton/source_modules.py', 'plugins/passive_skeleton/placement.py']),
    dict(id='tail-source', title='尾电流源跟着公共 S 节点，靠近输入对', factory=tail_source,
         before='I1 连接的是两只输入管共同的 tail 节点，却被摆到右下角，需要很长的引线。',
         after='把共同 S 节点作为附件锚点，在输入对下方安排尾源，尽量靠近这个公共接点。',
         takeaway='源器件跟随它服务的节点，而不是跟随全图的某一条边。',
         limits='共享或归属不明的源保守处理；不能仅凭“是电流源”就认作尾源。',
         codes=['plugins/passive_skeleton/source_modules.py', 'plugins/passive_skeleton/placement.py']),
    dict(id='terminal-2d', title='端部附件不必严格落在 MOS 的棋盘格上', factory=terminal_2d,
         before='一维通道已经安全放下 VREF，但它仍被挤到远处；只沿原方向找位很难靠近输入 G 极。',
         after='MOS 框架保持不动，附件用真实端口周围的二维空位继续找位置；本例线长、折角下降，但多了 1 处合法交叉，说明局部靠近不保证每项指标都下降。',
         takeaway='有源保持骨架，无源和源器件用更自由的间距贴近接入端口。',
         limits='模块整体平移，内部不拆散；仅在完整静态合法的原插入上改进，失败保留原位置。',
         codes=['plugins/passive_skeleton/terminal_slots.py']),
    dict(id='short-wire', title='器件不动，把合法的大绕线安全收紧', factory=short_wire,
         before='两个 G 极之间的线已经电气正确，但多绕了一个大拱形；固定引脚出线仍须保留。',
         after='使用现有缩线候选把自由部分拉直，完整复验后才替换；两个 MOS、固定出线和所有连接都保持。',
         takeaway='先保证能接通，再删除不必要的绕行和拐角。',
         limits='默认 strict：整图线长、交叉、折角均不能增加，至少一项改善；有避障用途的大圈不能随便删。',
         codes=['postprocess/simplify_routes.py', 'postprocess/route_reattachment.py',
                'postprocess/route_tracks.py']),
]


def defaults_audit():
    from netlist2schematic.flow import run_flow
    defaults = {n: p.default for n, p in inspect.signature(run_flow.run).parameters.items()
                if p.default is not inspect.Parameter.empty}
    keys = ['mode', 'layout_style', 'passive_layout', 'iterations', 'quality_seconds',
            'port_preflight', 'simplify_routes', 'simplify_seconds', 'route_tradeoff',
            'structure_layout', 'region_layout', 'readability_layout', 'attachment_layout',
            'symmetry_layout', 'formal_output', 'power_rails', 'net_labels']
    selected = {key: defaults[key] for key in keys}
    selected.update(effective_column_layout=run_flow._resolve_column_policy(None),
                    effective_passive_skeleton=run_flow._resolve_skeleton_policy(None),
                    final_selection=['bends', 'crossings', 'wirelength'])
    return selected


def export_case(case, destination, route_seconds):
    folder = destination/case['id']
    folder.mkdir(parents=True, exist_ok=True)
    started = perf_counter()
    devices, before, after, comps, nets, index, report = case['factory']()
    source = netlist_text(devices)
    (folder/'example.cir').write_text(source, encoding='utf-8')
    parsed = to_document(parse_netlist(folder/'example.cir'))
    assert parsed['nets'] == nets
    assert {c['id']: c['nodes'] for c in parsed['components']} == {c['id']: c['nodes'] for c in comps}
    original = to_document(devices)
    overrides = report.pop('_route_overrides', {})
    results = {}
    for side, placement in [('before', before), ('after', after)]:
        routed = overrides.get(side)
        if routed is None:
            routed = final_route(PAR, nets, placement, index, options=OPTIONS,
                                 deadline=perf_counter()+route_seconds)
        checked = validate_final_routes(PAR, nets, placement, index, routed[0], options=OPTIONS)
        assert checked[2]['valid'] and checked[1] == routed[1]
        render_svg(PAR, placement, comps, nets, index, checked[0], checked[1], folder/(side+'.svg'),
                   clean=True, validation=checked[2])
        positions = {c['id']: dict(row=placement.cell_of[i][0], col=placement.cell_of[i][1],
            orient=placement.orient[i], point=list(placement.pos_of(i))) for i, c in enumerate(comps)}
        results[side] = dict(placement=positions, metrics=checked[1], validation=checked[2],
                            routes=checked[0], svg_sha256=sha256((folder/(side+'.svg')).read_bytes()).hexdigest())
    # Use one physical scale on both sides, with independent centering only.
    drawings = {side: ET.parse(folder/(side+'.svg')) for side in ('before', 'after')}
    boxes = {side: list(map(float, tree.getroot().get('viewBox').split())) for side, tree in drawings.items()}
    width, height = max(b[2] for b in boxes.values()), max(b[3] for b in boxes.values())
    ET.register_namespace('', 'http://www.w3.org/2000/svg')
    for side, tree in drawings.items():
        from netlist2schematic.rendering.formal_svg import geometry_signature
        original_geometry = geometry_signature(tree.getroot())
        # Presentation only: make tiny teaching examples legible on a projector.
        for parent in tree.getroot().iter():
            for element in list(parent):
                if element.tag.rsplit('}', 1)[-1] != 'text':
                    continue
                if ''.join(element.itertext()).startswith('clean: WL='):
                    parent.remove(element)
                else:
                    element.set('font-size', '18')
                    element.set('font-family', 'Arial,Microsoft YaHei,sans-serif')
                    element.set('font-weight', '600')
        x, y, w, h = boxes[side]
        tree.getroot().set('viewBox', f'{x+(w-width)/2:g} {y+(h-height)/2:g} {width:g} {height:g}')
        tree.getroot().set('width', f'{width:g}')
        tree.getroot().set('height', f'{height:g}')
        assert geometry_signature(tree.getroot()) == original_geometry
        tree.write(folder/(side+'.svg'), encoding='utf-8')
        results[side]['svg_sha256'] = sha256((folder/(side+'.svg')).read_bytes()).hexdigest()
    assert to_document(devices) == original
    sources = {path: sha256((ROOT/'netlist2schematic'/path).read_bytes()).hexdigest() for path in case['codes']}
    record = dict(id=case['id'], title=case['title'], example_type='controlled_real_method',
        description='Constructed starting placement; real named method and independent validation. Layout pairs use the same A*; the shortcut pair uses a constructed verified detour. Not a full-flow/version benchmark.',
        device_count=len(comps), source_netlist=source, connections_unchanged=True,
        elapsed_seconds=round(perf_counter()-started, 4), method_report=report, results=results,
        comparison_view=dict(equal_physical_scale=True, width=width, height=height, independently_centered=True,
                             teaching_font_size=18, electrical_drawing_geometry_unchanged=True),
        source_hashes=sources)
    write_json(folder/'record.json', record)
    return record


def image(path):
    return 'data:image/svg+xml;base64,'+base64.b64encode(Path(path).read_bytes()).decode('ascii')


STYLE = '''
:root{font-family:"Microsoft YaHei","Noto Sans SC",Arial,sans-serif;color:#162638;background:#f3f6fa;line-height:1.7}*{box-sizing:border-box}body{margin:0}header{background:#142e4a;color:white;padding:32px max(24px,calc((100vw - 1280px)/2))}header h1{font-size:30px;line-height:1.35;margin:0 0 12px}header p{max-width:1000px;color:#d7e5f5;margin:8px 0}main{max-width:1328px;margin:auto;padding:24px}.toolbar{position:sticky;top:0;background:#fffffff5;border-bottom:1px solid #dce4ee;z-index:3;padding:10px 24px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}button,a.button{font:inherit;border:1px solid #ccd8e6;border-radius:8px;background:white;padding:7px 14px;color:#183b65;cursor:pointer;text-decoration:none}a{color:#245ca3}.summary,.method{background:white;border:1px solid #dce4ee;border-radius:14px;margin-bottom:24px;overflow:hidden}.summary{padding:22px 26px}.summary p{margin:5px 0}.chips{display:flex;gap:8px;flex-wrap:wrap}.chips a{background:#edf4ff;text-decoration:none;padding:6px 12px;border-radius:8px}.head{padding:22px 26px 14px}.head h2{margin:0;font-size:23px}.badge{color:#187755;background:#e6f6ee;padding:3px 8px;font-size:13px;border-radius:5px;margin-left:10px;white-space:nowrap}.lead{font-size:18px;color:#3f566e;margin:8px 0 0}.pair{display:grid;grid-template-columns:1fr 1fr;gap:0;border-top:1px solid #e2e9f1;border-bottom:1px solid #e2e9f1}.panel{padding:16px;min-width:0}.panel+ .panel{border-left:1px solid #e2e9f1}.panel h3{margin:0 0 10px;font-size:17px}.before h3{color:#9c5820}.after h3{color:#147251}.picture{height:355px;background:#fff;display:flex;align-items:center;justify-content:center;border:1px solid #edf0f4;border-radius:8px;cursor:zoom-in}.picture img{max-width:100%;max-height:100%;object-fit:contain}.explain{min-height:84px;margin:12px 0 8px;font-size:16px}.metrics{font-family:ui-monospace,Consolas,monospace;color:#64768a;font-size:13px}.tail{padding:14px 26px 20px}.tail p{margin:5px 0}.limits{font-size:14px;color:#697b8e}details{margin-top:10px;font-size:14px}pre{background:#f5f7fa;padding:12px;overflow:auto;font-family:Consolas,monospace;font-size:13px;line-height:1.5}code{font-family:Consolas,monospace}.note{border-left:4px solid #e0aa45;background:#fff9ed;padding:12px 16px}.number{color:#59789c;margin-right:10px}dialog{border:none;border-radius:12px;max-width:96vw;width:96vw;height:94vh;padding:20px}dialog::backdrop{background:#0d1b2ee6}.zoom{height:calc(100% - 50px);display:flex;align-items:center;justify-content:center;overflow:auto}.zoom img{width:auto;height:100%;max-width:none}.zoom-head{display:flex;justify-content:space-between;gap:20px}.lecture .method{display:none}.lecture .method.active{display:block}.lecture .picture{height:47vh}.lecture .summary{display:none}[hidden]{display:none!important}#position{margin-left:auto;font-size:14px;color:#64768a}.failure{color:#a33131}.foot{color:#698098;text-align:center;font-size:13px;margin:24px}li{margin:6px 0}@media(max-width:760px){header h1{font-size:24px}main{padding:12px}.head,.tail{padding:16px}.pair{grid-template-columns:1fr}.panel+.panel{border-left:0;border-top:1px solid #e2e9f1}.picture{height:300px}.explain{min-height:0}.badge{display:inline-block}#position{margin-left:0}}@media print{@page{size:A4 landscape;margin:12mm}body{background:white}header{padding:15px;color:#152c48;background:white}header p{color:#456}.toolbar,dialog,details,.summary{display:none}main{padding:0;max-width:none}.method,.lecture .method{display:block!important;break-inside:avoid;break-after:page;border:0;margin:0}.picture,.lecture .picture{height:95mm}.head h2{font-size:21px}.explain{font-size:14px;min-height:52px}.tail{padding-top:5px}.lead{font-size:16px}}
'''


APP = '''
(()=>{const cards=[...document.querySelectorAll('.method')],position=document.getElementById('position');let current=0;
function show(){cards.forEach((c,i)=>c.classList.toggle('active',i===current));position.textContent=(current+1)+' / '+cards.length;}
function move(delta){current=Math.max(0,Math.min(cards.length-1,current+delta));show();if(document.body.classList.contains('lecture'))window.scrollTo({top:document.querySelector('.toolbar').offsetTop,behavior:'smooth'});else cards[current].scrollIntoView({behavior:'smooth',block:'start'});}
document.getElementById('mode').onclick=()=>{document.body.classList.toggle('lecture');document.getElementById('mode').textContent=document.body.classList.contains('lecture')?'查看全部对照':'逐页讲解';show();};
document.getElementById('previous').onclick=()=>move(-1);document.getElementById('next').onclick=()=>move(1);document.getElementById('print').onclick=()=>window.print();
document.querySelectorAll('.chips a').forEach((a,i)=>a.onclick=e=>{current=i;show();if(document.body.classList.contains('lecture')){e.preventDefault();move(0);}});
document.addEventListener('keydown',e=>{if(document.getElementById('zoom').open)return;if(e.key==='ArrowRight')move(1);if(e.key==='ArrowLeft')move(-1);});
const dialog=document.getElementById('zoom');document.querySelectorAll('.picture img').forEach(img=>img.onclick=()=>{document.getElementById('zoom-title').textContent=img.alt;document.getElementById('zoom-image').src=img.src;dialog.showModal();});document.getElementById('close-zoom').onclick=()=>dialog.close();show();})();
'''


def build_page(destination, cases, records):
    sections, links = [], []
    for number, case in enumerate(cases, 1):
        record = records[case['id']]
        links.append(f'<a href="#{case["id"]}">{number:02d} {escape(case["title"])}</a>')
        if record.get('status') == 'failed':
            sections.append(f'<article class="method" id="{case["id"]}"><div class="head"><h2>{number:02d} {escape(case["title"])}</h2><p class="failure">本次小例未完成：{escape(record["error"])}。未把失败试案画成成功图。</p></div></article>')
            continue
        panels = []
        for side, label in [('before', '使用前：没有体现这项处理的起点'), ('after', '使用后：真实方法处理后的结果')]:
            metrics = record['results'][side]['metrics']
            text = f'信号线长 {metrics["wirelength"]:g} · 交叉 {metrics["crossings"]} · 折角 {metrics["bends"]} · 独立验收通过'
            panels.append(f'<section class="panel {side}"><h3>{label}</h3><div class="picture"><img src="{image(destination/case["id"]/(side+".svg"))}" alt="{escape(case["title"])} · {label}"></div><p class="explain">{escape(case[side])}</p><p class="metrics">{text}</p></section>')
        sources = '\n'.join('netlist2schematic/'+path for path in case['codes'])
        sections.append(f'<article class="method" id="{case["id"]}"><div class="head"><h2><span class="number">{number:02d}</span>{escape(case["title"])}<span class="badge">当前默认 · 条件触发</span></h2><p class="lead">{escape(case["takeaway"])}</p></div><div class="pair">{"".join(panels)}</div><div class="tail"><p><strong>什么时候用：</strong>{escape(case["limits"])}</p><p class="limits">同一份网表 · {record["device_count"]} 个器件 · 同一连接与几何验收。教学起点人为构造，非完整 flow 的版本性能对比。标注为讲解统一放大，不改变器件或线路。</p><details><summary>本例网表与实现位置</summary><pre>{escape(record["source_netlist"])}</pre><pre>{escape(sources)}</pre></details></div></article>')
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>默认布局设计点 · 最小前后对照</title><style>{STYLE}</style></head><body>
<header><h1>默认布局中的几个具体设计点</h1><p>用小电路说明：MOS 如何找到对应层；RLC、独立电流源和电压源如何插入有源框架。</p><p>2026-11-19 源码核对 · 所有图片内嵌，可只转发这个 HTML · 点击图片放大，或使用“逐页讲解”与打印。</p></header>
<nav class="toolbar"><button id="mode">逐页讲解</button><button id="previous">← 上一项</button><button id="next">下一项 →</button><button id="print">打印 / 保存为 PDF</button><span id="position"></span></nav>
<main><section class="summary"><p><strong>一句话：</strong>先保住 MOS/BJT 的上下层次和主列，再按真实连接把 RLC/V/I 及其串并联小模块插进去；端部附件可用更自由的位置。</p><p class="note">这里展示真实算法在受控小例上的局部处理，不把整套 flow 当作前后变量。两边都完整布线并独立验收；不是手绘目标图，也不代表每张复杂电路都会得到相同收益。每对图片的单位缩放相同，仅整体平移居中。</p><p>默认先运行候选2、3，均失败才运行候选1。最终选图仍按折角、交叉、信号线长；本页为了单独讲清方法，不执行整批 SA 或候选竞争。</p><div class="chips">{"".join(links)}</div></section>{"".join(sections)}
<section class="summary"><h2>讲解时可以这样收尾</h2><p>“这些规则都根据连接拓扑和真实端口工作，不读取 GT 坐标，也不按图号指定位置。遇到分叉或归属不明确的地方会保守跳过；每张成功图仍要通过完整连接与几何检查。”</p><p>本页不把实验对称、区域/结构增强、旧附件实验当作默认能力。正式字体、VDD 合并和网标仍需显式调用，它们不属于这里的布局处理。</p></section></main>
<dialog id="zoom"><div class="zoom-head"><strong id="zoom-title"></strong><button id="close-zoom">关闭</button></div><div class="zoom"><img id="zoom-image" alt="放大后的对照图"></div></dialog><script>{APP}</script></body></html>'''


def build(out_dir, selected=None, route_seconds=5):
    destination = Path(out_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    cases = [case for case in CASES if selected is None or case['id'] in selected]
    if not cases or (selected and set(selected)-{case['id'] for case in CASES}):
        raise ValueError('Unknown or empty design-example selection')
    records = {}
    for case in cases:
        print('Example:', case['id'], flush=True)
        try:
            with redirect_stdout(io.StringIO()):
                record = export_case(case, destination, route_seconds)
            record['status'] = 'passed'
        except Exception as exc:
            record = dict(status='failed', error=str(exc), exception=type(exc).__name__)
            write_json(destination/case['id']/'failure.json', record)
        records[case['id']] = record
        print(' ', record['status'], record.get('error', ''), flush=True)
    (destination/'index.html').write_text(build_page(destination, cases, records), encoding='utf-8')
    manifest = dict(format='default-layout-design-examples-v1', defaults=defaults_audit(),
                    no_flow_defaults_changed=True, no_dataset_runs=True, cases=records)
    write_json(destination/'manifest.json', manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', default=str(ROOT/'output/design-points-gallery'))
    parser.add_argument('--cases', nargs='+', choices=[case['id'] for case in CASES])
    parser.add_argument('--route-seconds', type=float, default=5)
    args = parser.parse_args(argv)
    report = build(args.out_dir, args.cases, args.route_seconds)
    print('Guide:', Path(args.out_dir).resolve()/'index.html')
    return int(any(case['status'] != 'passed' for case in report['cases'].values()))


if __name__ == '__main__':
    raise SystemExit(main())
