#!/usr/bin/env python3
"""Independently verify v7 track fairness on saved layouts without SA/A*."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
from html import escape
import io
import json
from math import isfinite
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

from compare_route_scheduling import _detours, _operation_table, _rel
from compare_route_tracks import (PRESERVED_FIELDS, _code_hashes, _json, _no_search,
                                 _resolve, _sha, _write, execute as _execute)

FORMAT = 'track-fairness-comparison-v7'
DEFAULT_CASES = {
    **{str(i): f'output/{i}/{i}-placement.json' for i in range(1, 5)},
    '5': 'output/5-dut/5-dut-placement.json',
    '6': 'output/6/6-placement.json',
    '7': 'output/7/7-placement.json',
    'data23': ('output/structure-layout-comparison/runs/data23/off/'
               '463fe7976a881a0f/operational-amplifier-dut-placement.json'),
    'data23-v6': ('output/route-scheduling-v6/data23/'
                  'operational-amplifier-dut-placement.json'),
}
NOTICE = ('图1～7及图23旧v5成品作为主组；另对图23已完成v6的成品再给v7独立5秒，'
          '作为增量探查。两份图23的起始线路不同，不能把后一组当作与前一组相同总预算的竞赛。'
          '全部保持保存布局、引脚、几何规则和布线权重；不运行SA、A*或恢复链。'
          '图1～3原成品为v3、图4～7为v4，本报告也不是同一份未简化线路的v6/v7公平比较。')


def _outer_facts(saved):
    """Measure visible left/top detours without inventing a shortest-path claim."""
    from netlist2schematic.routing.routing_rules import canonical_topology
    from netlist2schematic.common.sa_saved import load_placement

    data, _, _, placement, _, nets, index = load_placement(saved)
    result = {}
    for net in ('iref', 'net9'):
        if net not in data['routes']:
            continue
        runs = canonical_topology(data['routes'][net])['runs']
        points = [point for segment in runs for point in segment]
        pins = [placement.pin_world(index[name], pin) for name, pin in nets.get(net, ())]
        if not points or not pins:
            continue
        x0, x1 = min(p[0] for p in pins), max(p[0] for p in pins)
        y0, y1 = min(p[1] for p in pins), max(p[1] for p in pins)
        left_length = top_length = 0.0
        for a, b in runs:
            length = abs(a[0]-b[0]) + abs(a[1]-b[1])
            if a[0] == b[0]:
                left_length += length if a[0] < x0 else 0
                top_length += max(0, min(max(a[1], b[1]), y0)-min(a[1], b[1]))
            else:
                top_length += length if a[1] < y0 else 0
                left_length += max(0, min(max(a[0], b[0]), x0)-min(a[0], b[0]))
        result[net] = {
            'terminal_bounds': [x0, y0, x1, y1],
            'wire_bounds': [min(p[0] for p in points), min(p[1] for p in points),
                            max(p[0] for p in points), max(p[1] for p in points)],
            'left_extension': max(0, x0-min(p[0] for p in points)),
            'top_extension': max(0, y0-min(p[1] for p in points)),
            'left_of_terminal_box_length': left_length,
            'above_terminal_box_length': top_length,
            'upper_horizontal_tracks': sorted({a[1] for a, b in runs
                                              if a[1] == b[1] and a[1] < y0}),
            'left_vertical_tracks': sorted({a[0] for a, b in runs
                                           if a[0] == b[0] and a[0] < x0}),
        }
    return result


def _focus_svg(source, destination):
    """Color an analysis copy without moving any point in the verified SVG."""
    tree = ET.parse(source)
    tree.getroot().set('aria-label', '诊断标色：红色IREF，蓝色net9；坐标与原成品一致')
    colors = {'iref': '#c0262d', 'net9': '#1d4ed8'}
    for node in tree.getroot().iter():
        net = node.get('data-net')
        if net in colors:
            node.set('stroke', colors[net])
            node.set('stroke-width', '3.5')
        elif net:
            node.set('stroke-opacity', '0.35')
    tree.write(destination, encoding='utf-8', xml_declaration=True)


def execute(label, source, destination, seconds):
    from compare_passive_layouts import _verify_saved
    from netlist2schematic.flow import run_flow

    source = _resolve(source)
    original = _json(source)
    source_hash = _sha(source)
    before_outer = _outer_facts(source)
    record = _execute(label, source, destination, seconds)
    report = record['report']
    if seconds > 0 and report.get('version') != 'orthogonal-shortcuts-v7':
        raise ValueError('This comparison requires the v7 simplifier implementation')
    netlist = Path(original['source_netlist'])
    signature = {'source_netlist': str((netlist if netlist.is_absolute()
                                       else source.parent / netlist).resolve()),
                 'cell': original.get('source_cell')}
    runtime = _code_hashes()
    log = io.StringIO()
    with ExitStack() as stack, redirect_stdout(log):
        _no_search(stack)
        disabled = run_flow.simplify_placement(
            source, out_dir=destination / 'off-check', simplify_routes='off',
            simplify_seconds=seconds)
        disabled_verified = _verify_saved(destination / 'off-check' / source.name, signature)
        for field in PRESERVED_FIELDS:
            if original.get(field) != disabled.get(field):
                raise ValueError(f'{label}: disabled simplification changed {field}')
        if (run_flow._routes_sha256(list(original['routes'].items()))
                != run_flow._routes_sha256(list(disabled['routes'].items()))
                or original['metrics'] != disabled['metrics']):
            raise ValueError(f'{label}: disabled simplification changed signal wires')
    if _sha(source) != source_hash or _code_hashes() != runtime:
        raise ValueError('Source or runtime changed during compatibility verification')
    after_path = Path(record['after']['placement_json'])
    after_data = _json(after_path)
    record.update(format=FORMAT, runtime_version=report.get('version'),
                  comparison_kind='continue_saved_valid_routes',
                  baseline_is_unsimplified=False,
                  group='v6_incremental_probe' if label == 'data23-v6' else 'main_saved_cases',
                  stages=report.get('stages', []),
                  track_generation=report.get('track_generation', {}),
                  per_net=_detours(record['before'], record['after']),
                  outer_geometry={'before': before_outer, 'after': _outer_facts(after_path)},
                  device_positions_and_axes_unchanged=(
                      original['placement'] == after_data['placement']
                      and original.get('layout_grid') == after_data.get('layout_grid')),
                  disabled_routes_unchanged=True, disabled_verified=disabled_verified,
                  comparison_tool_sha256=_sha(__file__))
    if label.startswith('data23'):
        record['focus_svgs'] = {}
        for which in ('before', 'after'):
            image = destination / f'focus-{which}.svg'
            _focus_svg(record[which]['clean_svg'], image)
            record['focus_svgs'][which] = str(image)
    (destination / 'disabled-verification.log').write_text(log.getvalue(), encoding='utf-8')
    _write(destination / 'verification.json', record)
    return record


def _figures(output, record):
    figures = []
    for which, label in (('before', '保存起点'), ('after', 'v7安全简化后')):
        row = record[which]
        metrics = row['metrics']
        image = _rel(output, record.get('focus_svgs', {}).get(which, row['clean_svg']))
        figures.append(f'<figure><figcaption>{label}：线长{metrics["wirelength"]:g} / '
                       f'交叉{metrics["crossings"]} / 折角{metrics["bends"]}</figcaption>'
                       f'<a href="{image}"><img src="{image}" loading="lazy"></a>'
                       f'<p><a href="{_rel(output, row["placement_json"])}">JSON</a> · '
                       f'<a href="{_rel(output, row["color_svg"])}">彩色SVG</a> · '
                       f'<a href="{_rel(output, row["clean_svg"])}">黑白成品</a></p></figure>')
    return '<div class="pair">' + ''.join(figures) + '</div>'


def _focus_table(record):
    rows = []
    for net in ('iref', 'net9'):
        values = record['per_net'].get(net)
        outer = record['outer_geometry']
        if not values:
            continue
        before, after = values['before'], values['after']
        old_outer, new_outer = outer['before'][net], outer['after'][net]
        rows.append(f'<tr><td>{net}</td><td>{before["wirelength"]:g} → {after["wirelength"]:g}</td>'
                    f'<td>{before["outside_length"]:g} → {after["outside_length"]:g}</td>'
                    f'<td>{old_outer["left_extension"]:g} → {new_outer["left_extension"]:g}</td>'
                    f'<td>{old_outer["top_extension"]:g} → {new_outer["top_extension"]:g}</td>'
                    f'<td>{escape(str(old_outer["upper_horizontal_tracks"]))} → '
                    f'{escape(str(new_outer["upper_horizontal_tracks"]))}</td></tr>')
    if not rows:
        return ''
    return ('<table><thead><tr><th>重点网络</th><th>线长</th><th>端点框外线长</th>'
            '<th>向左突出</th><th>向上突出</th><th>框外上方水平轨道</th></tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table>')


def publish(output, records, seconds):
    valid = [row for row in records if row['status'] == 'valid']
    same_runtime = len({json.dumps(row['code_files'], sort_keys=True) for row in valid}) <= 1
    groups = {}
    for name in ('main_saved_cases', 'v6_incremental_probe'):
        rows = [row for row in valid if row['group'] == name]
        groups[name] = {'case_count': len(rows), **{
            which: {metric: sum(row[which]['metrics'][metric] for row in rows)
                    for metric in ('wirelength', 'crossings', 'bends')}
            for which in ('before', 'after')}}
    report = {'format': FORMAT, 'generated_utc': datetime.now(timezone.utc).isoformat(),
              'notice': NOTICE, 'simplification_seconds': seconds,
              'valid_cases': len(valid), 'case_count': len(records),
              'same_runtime_snapshot': same_runtime,
              'all_valid': len(valid) == len(records) and same_runtime,
              'groups': groups, 'cases': records}
    _write(output / 'comparison.json', report)
    sections = []
    for record in records:
        label = record['case']
        title = ({'data23': '图23：从旧v5成品开始',
                  'data23-v6': '图23：从已完成v6的成品继续'}).get(label, '图' + label)
        if record['status'] != 'valid':
            sections.append(f'<section><h2>{escape(title)}</h2><p>失败：'
                            f'{escape(record["error"])}</p></section>')
            continue
        detail = record['report']
        summary = (f'来源版本{record["source_simplifier_version"]}；'
                   f'v7停止原因{detail.get("stop_reason")}；简化耗时'
                   f'{detail.get("elapsed_seconds")}秒。器件位置和物理行列轴未变；'
                   '完整连接与几何验收、双SVG同线、独立重绘、关闭不改线、'
                   '同版本回放稳定和源文件保护均通过。')
        generation = escape(json.dumps(record['track_generation'], ensure_ascii=False, indent=2))
        stages = escape(json.dumps(record['stages'], ensure_ascii=False, indent=2))
        sections.append(f'<section><h2>{escape(title)}</h2><p>{escape(summary)}</p>'
                        + _figures(output, record) + _focus_table(record)
                        + _operation_table(detail)
                        + '<details><summary>按链轮转与快速筛选记录</summary>'
                        + f'<pre>{generation}</pre></details>'
                        + f'<details><summary>四阶段配额记录</summary><pre>{stages}</pre></details>'
                        + f'<p><a href="{_rel(output, Path(record["after"]["placement_json"]).parent / "verification.json")}">完整验证记录</a></p></section>')
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>轨道内收v7：按链轮转与异网快速筛选</title><style>
body{font:15px/1.65 system-ui;margin:24px auto;max-width:1600px;padding:0 20px;color:#18212b;background:#f4f6f8}
section{background:white;padding:20px;margin:22px 0;border:1px solid #d8dfe6;border-radius:8px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:20px}
figure{margin:0}figcaption{font-weight:600}img{width:100%;height:650px;object-fit:contain;border:1px solid #ddd}a{color:#1d4ed8}
table{border-collapse:collapse;margin:16px 0}td,th{padding:5px 12px;border:1px solid #d8dfe6;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}
@media(max-width:850px){.pair{grid-template-columns:1fr}}</style><h1>轨道内收v7：按链轮转与提前筛选</h1>
<p>NOTICE</p><p>沿用四阶段调度和默认5秒／512候选预算。新策略让同一网络或不同网络的自由链轮流获得轨道候选，
并在完整验收前提前剔除违反原异网间距规则的候选。完整验收仍独立决定是否接受。</p>
<p>向左／向上突出均相对该网真实端点包围盒计算，含正常固定出线；它们用于定位外围大圈，不代表全部突出都不合法或可删除。
所有成功改动均保持线长、交叉和折角不增加；剩余绕行与预算停止如实保留。</p>
<p>两组图23使用独立诊断副本标色：<b style="color:#c0262d">红色为IREF</b>，<b style="color:#1d4ed8">蓝色为net9</b>。
仅颜色、线宽用于强调，线路坐标与对应验收成品完全相同；原黑白图和彩色图另有链接。</p>
<p>SUMMARY；<a href="comparison.json">完整JSON记录</a>。</p>SECTIONS</html>'''
    summary = f'完整验收{len(valid)}/{len(records)}；同一运行代码：{same_runtime}'
    (output / 'index.html').write_text(page.replace('NOTICE', escape(NOTICE)).replace(
        'SUMMARY', escape(summary)).replace('SECTIONS', '\n'.join(sections)), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', type=Path, default=ROOT / 'output/track-fairness-v7')
    parser.add_argument('--cases', nargs='+', choices=list(DEFAULT_CASES))
    parser.add_argument('--seconds', type=float, default=5)
    args = parser.parse_args(argv)
    if not isfinite(args.seconds) or args.seconds < 0:
        parser.error('seconds must be finite and non-negative')
    output = args.out_dir.resolve()
    sources = [_resolve(DEFAULT_CASES[label]) for label in (args.cases or DEFAULT_CASES)]
    if any(source.parent == output or source.parent in output.parents for source in sources):
        parser.error('output must not be inside an original artifact directory')
    output.mkdir(parents=True, exist_ok=True)
    records = []
    for label in args.cases or DEFAULT_CASES:
        print(f'START {label}', flush=True)
        try:
            record = execute(label, DEFAULT_CASES[label], output / label, args.seconds)
        except Exception as exc:
            record = {'case': label, 'status': 'failed', 'error': f'{type(exc).__name__}: {exc}'}
            print(f'FAIL {label}: {record["error"]}', flush=True)
        else:
            print(f'VALID {label}: {record["before"]["metrics"]} -> {record["after"]["metrics"]}', flush=True)
        records.append(record)
        publish(output, records, args.seconds)
    report = publish(output, records, args.seconds)
    print(f'Complete: {report["valid_cases"]}/{len(records)}; {output / "index.html"}', flush=True)
    return int(not report['all_valid'])


if __name__ == '__main__':
    raise SystemExit(main())
