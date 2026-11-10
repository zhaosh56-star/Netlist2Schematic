#!/usr/bin/env python3
"""Render recognized teaching circuits using project symbols and verified wires.

Fixed coordinates belong only to these illustrations. Production recognition,
placement, routing, recovery and rendering algorithms are not modified.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from html import escape
from io import StringIO
import json
from math import isfinite
from pathlib import Path
import random
import sys
from time import perf_counter
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from circuit_structures import _LABELS, analyze_structures
from netlist_core import parse_netlist, to_document
from routing_rules import RoutingOptions, build_scene
from sa_common import configure_resources, load_local, read_json, resource_root, write_json
from sa_geometry import validate_geometry
from sa_render import render_svg
from sa_routing import RoutingValidationError, final_route, validate_final_routes
from orthogonal_router import RoutingFailure
from simplify_routes import _covers_fixed, simplify_routes
from structure_gallery_cases import CASES
import device_leveling as leveling
import vf2_match

SVG = 'http://www.w3.org/2000/svg'
ET.register_namespace('', SVG)


def placement(par, comps, positions, scale):
    if set(positions) != {comp['id'] for comp in comps}:
        raise ValueError('Teaching positions do not match source device IDs')
    pl = par.Placement.__new__(par.Placement)
    pl.comps = comps
    pl.rng = random.Random(20261106)
    pl.cell_of = [tuple(positions[c['id']][:2]) for c in comps]
    pl.orient = [positions[c['id']][2] for c in comps]
    if len(set(pl.cell_of)) != len(comps):
        raise ValueError('Duplicate teaching cells')
    pl.row_of = [r for r, _ in pl.cell_of]
    pl.cols = max(c for _, c in pl.cell_of) + 1
    pl.comp_at = {cell: i for i, cell in enumerate(pl.cell_of)}
    pl.column_x = [120 + 220 * scale * col for col in range(pl.cols)]
    pl.row_y = [100 + 180 * scale * row for row in range(max(pl.row_of) + 1)]
    return pl


def recognize(case, source):
    devices = parse_netlist(source)
    records = analyze_structures(devices)
    with redirect_stdout(StringIO()):
        legacy = vf2_match.analyze(source)
    matches = records if case['engine'] == 'structured' else legacy
    targets = [record for record in matches if record['kind'] == case['kind']
               and set(record['devices']) == set(case['focus'])]
    if not targets:
        raise ValueError(f"Expected recognition missing: {case['kind']} {case['focus']}")
    return devices, records, legacy, targets[0]


def route(case, par, comps, nets, index, records, options, seconds, cache):
    key = json.dumps([case['netlist'], case['positions']], sort_keys=True)
    if key in cache:
        scale, result, report, stage, origin = deepcopy(cache[key])
        pl = placement(par, comps, case['positions'], scale)
        checked = validate_final_routes(par, nets, pl, index, result[0], options=options)
        if checked[1] != result[1]:
            raise ValueError('Reused teaching route metric mismatch')
        return pl, result, report, stage, scale, origin
    failures = []
    for scale in (1.0, 1.5, 2.0):
        pl = placement(par, comps, case['positions'], scale)
        stage = 'normal'
        try:
            result = final_route(par, nets, pl, index, options=options,
                                 deadline=perf_counter() + seconds)
        except (RoutingFailure, RoutingValidationError) as exc:
            failures.append(str(exc))
            checkpoint = getattr(exc, '_rescue_context', None)
            if checkpoint is None:
                continue
            from local_rescue import rescue_routing
            context = (pl, comps, nets, index, {'structs': records, 'candidate_id': 'teaching'})
            try:
                *context, result = rescue_routing(context, checkpoint,
                    routing_options=options, seconds=seconds)
            except (RoutingFailure, RoutingValidationError) as rescue_exc:
                failures.append(str(rescue_exc))
                continue
            pl = context[0]
            stage = 'local_rescue'
        *result, report = simplify_routes(par, nets, pl, index, result,
                                          options=options, seconds=2)
        scene = build_scene(par, nets, pl, index, options=options)
        if not all(_covers_fixed(scene, name, segments) for name, segments in result[0]):
            raise ValueError('A teaching route lost a fixed pin corridor')
        checked = validate_final_routes(par, nets, pl, index, result[0], options=options)
        if checked[1] != result[1]:
            raise ValueError('Final teaching route metrics do not match')
        cache[key] = deepcopy((scale, result, report, stage, case['slug']))
        return pl, result, report, stage, scale, None
    raise ValueError('No fully verified teaching route: ' + '; '.join(failures))


def annotate(case, par, pl, comps, nets, index, source_svg, destination, options):
    tree = ET.parse(source_svg)
    root = tree.getroot()
    focus = set(case['focus'])
    background = ET.Element(f'{{{SVG}}}g', {'data-gallery-highlights': 'true'})
    xs, ys = [], []
    for i, component in enumerate(comps):
        x, y = pl.pos_of(i)
        box = par.GEO_DOC[component['symbol']]['orientations'][pl.orient[i]]['viewBoxAABB']
        xs.extend((x + box['x'] - 9, x + box['x'] + box['width'] + 9,
                   x + box['x'] + box['width'] + 8 + len(component['id']) * 7.5))
        ys.extend((y + box['y'] - 9, y + max(box['y'] + box['height'] + 14, 32) + 4))
    for name in case['focus']:
        i = index[name]
        x, y = pl.pos_of(i)
        box = par.GEO_DOC[comps[i]['symbol']]['orientations'][pl.orient[i]]['viewBoxAABB']
        ET.SubElement(background, f'{{{SVG}}}rect', {
            'x': par.fmt(x + box['x'] - 9), 'y': par.fmt(y + box['y'] - 9),
            'width': par.fmt(box['width'] + 18), 'height': par.fmt(box['height'] + 18),
            'rx': '9', 'fill': '#dbeafe', 'stroke': '#2563eb', 'stroke-width': '1.2'})
    # The first child is the opaque white canvas. Put highlights behind wires.
    root.insert(1, background)
    for node in root.iter():
        name = node.get('data-component')
        if name:
            node.set('data-gallery-member', str(name in focus).lower())
            if name not in focus:
                node.set('opacity', '0.5')
    scene = build_scene(par, nets, pl, index, options=options)
    for _, rect in scene.marker_rects:
        xs.extend((rect[0], rect[2])); ys.extend((rect[1], rect[3]))
    for node in list(root):
        if node.tag == f'{{{SVG}}}text' and (node.text or '').startswith('color: WL='):
            root.remove(node)
        if node.get('data-net'):
            for point in node.get('points', '').split():
                x, y = map(float, point.split(','))
                xs.append(x); ys.append(y)
    for name, pins in scene.pins.items():
        if len(pins) != 1:
            continue
        pin = pins[0]
        x, y = pin.escape
        dx, dy = pin.direction
        anchor = 'start' if dx > 0 else 'end' if dx < 0 else 'middle'
        text = ET.SubElement(root, f'{{{SVG}}}text', {
            'x': par.fmt(x + dx * 8),
            'y': par.fmt(y - 6 if dx else y + (-10 if dy < 0 else 17)),
            'text-anchor': anchor, 'font-family': 'monospace', 'font-size': '12',
            'fill': '#334155', 'stroke': '#fff', 'stroke-width': '3', 'paint-order': 'stroke'})
        text.text = name
        text_x = x + dx * 8
        left = text_x - (len(name) * 7.5 if anchor == 'end' else
                         len(name) * 3.75 if anchor == 'middle' else 0)
        xs.extend((left, left + len(name) * 7.5))
        ys.extend((float(text.get('y')) - 12, float(text.get('y')) + 4))
    # Crop only the explanatory view: omit the debug title's minimum canvas
    # width so vertical two-device examples remain centered and readable.
    x0, y0, x1, y1 = min(xs) - 30, min(ys) - 25, max(xs) + 30, max(ys) + 25
    root.set('viewBox', ' '.join(par.fmt(v) for v in (x0, y0, x1-x0, y1-y0)))
    root.set('width', par.fmt(x1-x0)); root.set('height', par.fmt(y1-y0))
    canvas = next(child for child in root if child.tag == f'{{{SVG}}}rect')
    for key, value in (('x', x0), ('y', y0), ('width', x1-x0), ('height', y1-y0)):
        canvas.set(key, par.fmt(value))
    title = ET.Element(f'{{{SVG}}}title')
    title.text = case['title'] + '：蓝框为识别成员，淡色为必要上下文'
    root.insert(0, title)
    tree.write(destination, encoding='utf-8', xml_declaration=True)


def generate(case, output, par, options, seconds, cache):
    started = perf_counter()
    directory = output / case['slug']
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / 'example.cir'
    text = '* Recognition teaching example; not a qualified simulation circuit.\n' + case['netlist']
    if source.exists() and source.read_text(encoding='utf-8') != text:
        raise ValueError(f'Refusing to overwrite a changed gallery netlist: {source}')
    source.write_text(text, encoding='utf-8')
    source_hash = sha256(source.read_bytes()).hexdigest()
    devices, records, legacy, target = recognize(case, source)
    doc = to_document(devices)
    comps, nets = doc['components'], doc['nets']
    index = {c['id']: i for i, c in enumerate(comps)}
    pl, result, report, stage, scale, reused_from = route(
        case, par, comps, nets, index, records, options, seconds, cache)
    routed, metrics, validation = result
    pl.routing_stage = stage
    render_svg(par, pl, comps, nets, index, routed, metrics, directory / 'diagram-clean.svg',
               clean=True, structs=(), validation=validation)
    render_svg(par, pl, comps, nets, index, routed, metrics, directory / 'diagram-color.svg',
               clean=False, structs=(), validation=validation)
    annotate(case, par, pl, comps, nets, index, directory / 'diagram-color.svg',
             directory / 'diagram.svg', options)
    levels = leveling.compute_levels(devices)
    data = {'format_version': 1, 'source_netlist': str(source), 'source_cell': None,
            'source_sha256': source_hash, 'analog_canvas_root': str(resource_root()),
            'mode': 'layered', 'layout_style': 'teaching_fixed', 'seed': 20261106,
            'iterations': 0, 'routing_stage': stage, 'structs': [],
            'levels': levels['device_levels'],
            'layout_grid': {'column_x': list(pl.column_x), 'row_y': list(pl.row_y)},
            'placement': {c['id']: {'row': pl.cell_of[i][0], 'col': pl.cell_of[i][1],
                                   'orient': pl.orient[i]} for i, c in enumerate(comps)},
            'routes': dict(routed), 'metrics': metrics, 'validation': validation,
            'route_simplification': report,
            'gallery': {'manual_teaching_layout': True, 'engine': case['engine'],
                        'target': target, 'recognition_file': 'recognition.json',
                        'axis_scale': scale, 'routing_reused_from': reused_from}}
    if sha256(source.read_bytes()).hexdigest() != source_hash:
        raise ValueError('Teaching source changed during routing')
    write_json(directory / 'example-placement.json', data)
    write_json(directory / 'recognition.json', {'target': target,
        'structured': records, 'legacy': legacy})
    row = {key: deepcopy(case[key]) for key in
           ('slug', 'title', 'kind', 'engine', 'focus', 'roles', 'rule', 'note')}
    row.update(valid=True, confidence=target.get('confidence', 'legacy'), target=target,
               structured_kinds=sorted({r['kind'] for r in records}),
               legacy_kinds=sorted({r['kind'] for r in legacy}),
               component_count=len(comps), metrics=metrics, stage=stage,
               elapsed_seconds=round(perf_counter() - started, 6),
               source_sha256=source_hash, axis_scale=scale,
               diagram=f"{case['slug']}/diagram.svg",
               clean=f"{case['slug']}/diagram-clean.svg",
               color=f"{case['slug']}/diagram-color.svg",
               netlist=f"{case['slug']}/example.cir",
               placement=f"{case['slug']}/example-placement.json",
               recognition=f"{case['slug']}/recognition.json")
    write_json(directory / 'gallery-entry.json', row)
    return row


def write_index(output, rows):
    cards, links = [], []
    for row in rows:
        slug = escape(row['slug'], quote=True)
        title, kind = escape(row['title']), escape(row['kind'])
        category = ('旧VF2附录' if row['engine'] == 'legacy' else
                    '辅助关系' if row['confidence'] == 'relation' else '结构块')
        roles = ''.join(f'<tr><td><code>{escape(name)}</code></td><td>{escape(role)}</td>'
                        f'<td>{"蓝框成员" if name in row["focus"] else "淡色上下文"}</td></tr>'
                        for name, role in row['roles'].items())
        record = escape(json.dumps(row['target'], ensure_ascii=False, indent=2))
        metrics = row['metrics']
        cards.append(f'''<article id="{slug}" data-engine="{row['engine']}" data-confidence="{row['confidence']}">
<header><span class="badge">{category}</span><span class="check">完整验收通过</span>
<h2>{title}</h2><code>{kind}</code></header>
<a class="picture" href="{row['diagram']}" target="_blank" title="打开SVG放大查看"><img src="{row['diagram']}" alt="{title}的真实连接示例" loading="lazy"></a>
<div class="body"><p class="rule">{escape(row['rule'])}</p><p>{escape(row['note'])}</p>
<table><thead><tr><th>器件</th><th>作用</th><th>识别范围</th></tr></thead><tbody>{roles}</tbody></table>
<p class="metrics">线长 {metrics['wirelength']:.0f} · 交叉 {metrics['crossings']} · 折角 {metrics['bends']} · {row['component_count']}器件</p>
<nav><a href="{row['diagram']}" target="_blank">放大示意图</a><a href="{row['clean']}" target="_blank">黑白图</a>
<a href="{row['netlist']}" target="_blank">网表</a><a href="{row['recognition']}" target="_blank">全部识别记录</a>
<a href="{row['placement']}" target="_blank">布局与线路JSON</a></nav>
<details><summary>查看本条实际识别结果</summary><pre>{record}</pre></details></div></article>''')
        links.append(f'<a href="#{slug}">{title}</a>')
    page = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>当前子结构识别图鉴</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f6fa;color:#1f2937;font:15px/1.7 system-ui,"Microsoft YaHei",sans-serif}
main{max-width:1500px;margin:auto;padding:28px 22px}h1{font-size:29px;margin:0 0 8px}h2{font-size:20px;margin:7px 0 3px}
.intro,.toolbar{background:#fff;padding:20px 24px;border:1px solid #dce4ed;border-radius:12px;margin-bottom:18px}.intro p{margin:7px 0}.intro strong{color:#1d4ed8}
.toolbar{position:sticky;top:0;z-index:2;display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:12px 18px}
input,select{font:inherit;border:1px solid #cbd5e1;border-radius:7px;padding:7px 10px}input{min-width:260px;flex:1}.contents{display:flex;flex-wrap:wrap;gap:7px 15px;margin-top:12px}
a{color:#1d4ed8;text-decoration:none}a:hover{text-decoration:underline}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}
article{background:#fff;border:1px solid #dce4ed;border-radius:12px;overflow:hidden;scroll-margin-top:90px}article[hidden]{display:none}
article header{padding:18px 22px;border-bottom:1px solid #edf0f4}.badge{background:#dbeafe;color:#1e40af;border-radius:20px;padding:3px 10px;font-size:12px}.check{float:right;font-size:12px;color:#166534}
article[data-confidence=relation] .badge{background:#fef3c7;color:#92400e}article[data-engine=legacy] .badge{background:#ede9fe;color:#6d28d9}
.picture{display:block;background:white;padding:10px}.picture img{display:block;width:100%;height:350px;object-fit:contain}.body{padding:0 22px 20px}.rule{font-weight:600}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid #e5e7eb;padding:6px 8px;text-align:left}
code{font:13px/1.6 ui-monospace,Consolas,monospace;color:#475569}nav{display:flex;gap:14px;flex-wrap:wrap;font-size:13px}pre{white-space:pre-wrap;word-break:break-word;background:#f8fafc;padding:12px;font-size:12px}summary{cursor:pointer;margin-top:14px;color:#475569}.metrics{color:#64748b;font-size:12px}
@media(max-width:850px){.grid{grid-template-columns:1fr}main{padding:16px 12px}.picture img{height:330px}input{min-width:180px}}
</style></head><body><main>
<section class="intro"><h1>当前子结构识别图鉴</h1>
<p><strong>默认识别器15类，展示16张图（正、负反馈分别展示）；另附旧VF2额外4类，共20张。</strong></p>
<p>蓝框表示本条识别记录的器件成员，淡色器件是必要上下文。单端信号出口标注网名；相同颜色的完整导线属于同一网，电源/地沿用现有标记。</p>
<p>本页采用固定教学布局，使用项目真实符号、当前路由器与完整连接/几何验收，跳过SA。它展示“结构怎样连接”，不冒充正常300秒自动布局的效果，也不表示电路功能已仿真。</p>
<p>“辅助关系”是relation，不是独占高置信结构块；“旧VF2附录”不是新识别器的新增类型。当前结构模板以MOS及无源/运放代理为主，BJT支持解析、分层和绘图，但没有对应的BJT结构模板。</p>
<p>CAS_N/CAS_P虽然在旧源码中有定义，但未列入启用的TEMPLATES，因此没有把它们当作已启用模板另列。</p>
<details><summary>目录：逐项跳转</summary><div class="contents">CONTENTS</div></details></section>
<section class="toolbar"><input id="search" type="search" placeholder="搜索中文名称、类型、器件或识别规则" aria-label="搜索结构">
<select id="category" aria-label="筛选类别"><option value="all">全部示例</option><option value="structured">默认识别器</option><option value="high">结构块</option><option value="relation">辅助关系</option><option value="legacy">旧VF2附录</option></select><span id="count"></span></section>
<section class="grid">CARDS</section>
<p>识别、布局、路线与验收记录：<a href="manifest.json">manifest.json</a>。复现：<code>python tools/generate_structure_gallery.py</code>。</p>
</main><script>
const search=document.querySelector('#search'),category=document.querySelector('#category'),cards=[...document.querySelectorAll('article')];
function filter(){const q=search.value.trim().toLowerCase(),c=category.value;let count=0;for(const card of cards){const ok=(!q||card.textContent.toLowerCase().includes(q))&&(c==='all'||card.dataset.engine===c||card.dataset.confidence===c);card.hidden=!ok;if(ok)count++;}document.querySelector('#count').textContent=count+' / '+cards.length+' 张';}
search.addEventListener('input',filter);category.addEventListener('change',filter);filter();
</script></body></html>'''
    (output / 'index.html').write_text(page.replace('CONTENTS', ''.join(links))
                                     .replace('CARDS', '\n'.join(cards)), encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', default=str(ROOT / 'output/structure-gallery'))
    parser.add_argument('--seconds-per-attempt', type=float, default=12)
    parser.add_argument('--only', help='Regenerate one slug, then refresh the existing index')
    args = parser.parse_args(argv)
    if not isfinite(args.seconds_per_attempt) or args.seconds_per_attempt <= 0:
        parser.error('seconds-per-attempt must be positive and finite')
    selected = [c for c in CASES if args.only is None or c['slug'] == args.only]
    if not selected:
        parser.error('unknown example slug')
    output = Path(args.out_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    configure_resources()
    par = load_local('place_and_route', 'place-and-route.py')
    validate_geometry(par.GEO_DOC)
    options = RoutingOptions()
    cache, errors = {}, []
    for case in selected:
        print(f"Drawing {case['slug']} ...", flush=True)
        try:
            row = generate(case, output, par, options, args.seconds_per_attempt, cache)
        except (ValueError, OSError) as exc:
            errors.append({'slug': case['slug'], 'error': str(exc)})
            print(f"FAILED {case['slug']}: {exc}", flush=True)
        else:
            print(f"OK {case['slug']}: {row['metrics']}; {row['elapsed_seconds']:.2f}s", flush=True)
    rows = [read_json(output / case['slug'] / 'gallery-entry.json') for case in CASES
            if (output / case['slug'] / 'gallery-entry.json').is_file()]
    modern = {r['kind'] for r in rows if r['engine'] == 'structured'}
    old = {kind for r in rows for kind in r['legacy_kinds']}
    complete = len(rows) == len(CASES) and modern == set(_LABELS) and old >= set(vf2_match.TEMPLATES)
    manifest = {'format_version': 1, 'layout_policy': 'fixed_teaching_examples_no_sa',
                'parameters': asdict(options), 'example_count': len(rows),
                'structured_kinds': sorted(modern), 'legacy_kinds': sorted(old),
                'structured_kind_count': len(modern), 'all_types_covered': complete,
                'expected_structured_kinds': sorted(_LABELS),
                'expected_legacy_kinds': sorted(vf2_match.TEMPLATES),
                'recognizer_sha256': {name: sha256((ROOT / name).read_bytes()).hexdigest()
                                      for name in ('circuit_structures.py', 'vf2_match.py')},
                'examples': rows, 'generation_errors': errors}
    write_json(output / 'manifest.json', manifest)
    write_index(output, rows)
    print(f'Gallery: {output / "index.html"}; {len(rows)} examples; coverage={complete}', flush=True)
    return int(bool(errors) or (not args.only and not complete))


if __name__ == '__main__':
    raise SystemExit(main())
