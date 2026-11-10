#!/usr/bin/env python3
"""Verify v8 candidate-crossing prechecks on fixed saved routes, without SA/A*."""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

from compare_route_scheduling import _detours, _rel
from compare_route_tracks import (PRESERVED_FIELDS, _code_hashes, _json, _no_search,
                                 _resolve, _sha, _write, execute as _execute)
from compare_track_fairness import _focus_svg, _focus_table, _outer_facts

FORMAT = 'candidate-clearance-comparison-v8'
DEFAULT_CASES = {
    **{str(i): f'output/{i}/{i}-placement.json' for i in range(1, 5)},
    '5': 'output/5-dut/5-dut-placement.json',
    '6': 'output/6/6-placement.json',
    '7': 'output/7/7-placement.json',
    'data23-v5': ('output/structure-layout-comparison/runs/data23/off/'
                  '463fe7976a881a0f/operational-amplifier-dut-placement.json'),
    'data23-v6': ('output/route-scheduling-v6/data23/'
                  'operational-amplifier-dut-placement.json'),
    'data23-v7-v6': ('output/track-fairness-v7/data23-v6/'
                     'operational-amplifier-dut-placement.json'),
}
NOTICE = ('主组为图1～7及图23旧v5成品；额外两组分别从图23的v6成品、'
          'v6后又完成v7的成品继续，各给v8独立5秒。三份图23起始线路不同、历史预算不同，'
          '不能当成同一次flow或同一份未简化线路的版本竞赛。'
          '全部固定保存布局和几何规则，不运行SA、A*和恢复链。')


def execute(label, source, destination, seconds):
    from compare_passive_layouts import _verify_saved
    import run_flow

    source = _resolve(source)
    original = _json(source)
    source_hash = _sha(source)
    outer_before = _outer_facts(source)
    record = _execute(label, source, destination, seconds)
    report = record['report']
    if seconds > 0 and report.get('version') != 'orthogonal-shortcuts-v8':
        raise ValueError('This comparison requires the v8 candidate precheck implementation')
    operations = report.get('operations', {})
    precheck = report.get('candidate_precheck', {})
    if seconds > 0 and (precheck.get('passed') != sum(
            row.get('validated', 0) for row in operations.values())
            or precheck.get('rejected') != sum(
                row.get('crossing_rejected', 0) for row in operations.values())):
        raise ValueError('Candidate-precheck totals do not match the per-operation counters')
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
    saved = Path(record['after']['placement_json'])
    result = _json(saved)
    group = ('main_saved_cases' if label in ('1', '2', '3', '4', '5', '6', '7', 'data23-v5')
             else 'v6_incremental_probe' if label == 'data23-v6'
             else 'v7_after_v6_incremental_probe')
    record.update(format=FORMAT, runtime_version=report.get('version'),
                  comparison_kind='continue_saved_valid_routes', baseline_is_unsimplified=False,
                  group=group, stages=report.get('stages', []),
                  candidate_precheck=report.get('candidate_precheck', {}),
                  track_generation=report.get('track_generation', {}),
                  operation_counts=operations,
                  operation_precheck_counts={name: {
                      'completed': counts.get('validated', 0) + counts.get('crossing_rejected', 0),
                      'passed': counts.get('validated', 0),
                      'rejected': counts.get('crossing_rejected', 0)}
                      for name, counts in operations.items()},
                  per_net=_detours(record['before'], record['after']),
                  outer_geometry={'before': outer_before, 'after': _outer_facts(saved)},
                  device_positions_and_axes_unchanged=(
                      original['placement'] == result['placement']
                      and original.get('layout_grid') == result.get('layout_grid')),
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
    for which, caption in (('before', '保存起点'), ('after', 'v8安全简化后')):
        row = record[which]
        metrics = row['metrics']
        image = _rel(output, record.get('focus_svgs', {}).get(which, row['clean_svg']))
        figures.append(f'<figure><figcaption>{caption}：线长{metrics["wirelength"]:g} / '
                       f'交叉{metrics["crossings"]} / 折角{metrics["bends"]}</figcaption>'
                       f'<a href="{image}"><img src="{image}" loading="lazy"></a>'
                       f'<p><a href="{_rel(output, row["placement_json"])}">JSON</a> · '
                       f'<a href="{_rel(output, row["color_svg"])}">彩色SVG</a> · '
                       f'<a href="{_rel(output, row["clean_svg"])}">黑白成品</a></p></figure>')
    return '<div class="pair">' + ''.join(figures) + '</div>'


def _operation_table(report):
    labels = {'chain_shortcut': '直线／L形', 'leaf_reattachment': '支线重接',
              'three_segment_shortcut': '三段折线', 'track_compaction': '轨道内收'}
    rows = []
    for name, label in labels.items():
        counts = report.get('operations', {}).get(name, {})
        rejected = counts.get('crossing_rejected', 0)
        completed = counts.get('validated', 0) + rejected
        rows.append(f'<tr><td>{label}</td><td>{counts.get("considered", 0)}</td>'
                    f'<td>{completed}</td><td>{rejected}</td><td>{counts.get("validated", 0)}</td>'
                    f'<td>{counts.get("geometry_rejected", 0)}</td>'
                    f'<td>{counts.get("accepted", 0)}</td></tr>')
    return ('<table><thead><tr><th>操作</th><th>考虑</th><th>净区预筛完成</th><th>净区预筛拒绝</th>'
            '<th>进入完整验收</th><th>完整几何拒绝</th><th>接受</th></tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table>')


def publish(output, records, seconds):
    valid = [row for row in records if row['status'] == 'valid']
    same_runtime = len({json.dumps(row['code_files'], sort_keys=True) for row in valid}) <= 1
    groups = {}
    for group in ('main_saved_cases', 'v6_incremental_probe', 'v7_after_v6_incremental_probe'):
        rows = [row for row in valid if row['group'] == group]
        groups[group] = {'case_count': len(rows), **{
            which: {metric: sum(row[which]['metrics'][metric] for row in rows)
                    for metric in ('wirelength', 'crossings', 'bends')}
            for which in ('before', 'after')}}
    report = {'format': FORMAT, 'generated_utc': datetime.now(timezone.utc).isoformat(),
              'notice': NOTICE, 'simplification_seconds': seconds,
              'valid_cases': len(valid), 'case_count': len(records),
              'same_runtime_snapshot': same_runtime,
              'presentation_tool_sha256': _sha(__file__),
              'presentation_only_republish': any(
                  row.get('comparison_tool_sha256') != _sha(__file__) for row in valid),
              'all_valid': len(valid) == len(records) and same_runtime,
              'groups': groups, 'cases': records}
    _write(output / 'comparison.json', report)
    sections = []
    titles = {'data23-v5': '图23：旧v5成品起点', 'data23-v6': '图23：v6成品起点',
              'data23-v7-v6': '图23：v6后又完成v7的成品起点'}
    priority = {'data23-v7-v6': 0, 'data23-v6': 1, 'data23-v5': 2}
    ordered = sorted(enumerate(records), key=lambda item: (
        priority.get(item[1]['case'], 3), item[0]))
    for _, record in ordered:
        title = titles.get(record['case'], '图' + record['case'])
        if record['status'] != 'valid':
            sections.append(f'<section><h2>{escape(title)}</h2><p>失败：'
                            f'{escape(record["error"])}</p></section>')
            continue
        detail = record['report']
        summary = (f'来源版本{record["source_simplifier_version"]}；v8停止原因'
                   f'{detail.get("stop_reason")}；简化耗时{detail.get("elapsed_seconds")}秒。'
                   '器件位置和物理行列轴未变，完整连接与几何验收、双SVG同线、独立重绘、'
                   '关闭不改线、同版本回放稳定和源文件保护均通过。')
        precheck = escape(json.dumps(record['candidate_precheck'], ensure_ascii=False, indent=2))
        stages = escape(json.dumps(record['stages'], ensure_ascii=False, indent=2))
        sections.append(f'<section><h2>{escape(title)}</h2><p>{escape(summary)}</p>'
                        + _figures(output, record) + _focus_table(record) + _operation_table(detail)
                        + f'<details><summary>统一候选净区预筛记录</summary><pre>{precheck}</pre></details>'
                        + f'<details><summary>四阶段配额记录</summary><pre>{stages}</pre></details>'
                        + f'<p><a href="{_rel(output, Path(record["after"]["placement_json"]).parent / "verification.json")}">完整验证记录</a></p></section>')
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>候选交叉净区v8：保存成品对照</title><style>
body{font:15px/1.65 system-ui;margin:24px auto;max-width:1600px;padding:0 20px;color:#18212b;background:#f4f6f8}
section{background:white;padding:20px;margin:22px 0;border:1px solid #d8dfe6;border-radius:8px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:20px}
figure{margin:0}figcaption{font-weight:600}img{width:100%;height:650px;object-fit:contain;border:1px solid #ddd}a{color:#1d4ed8}
table{border-collapse:collapse;margin:16px 0}td,th{padding:5px 12px;border:1px solid #d8dfe6;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}
@media(max-width:850px){.pair{grid-template-columns:1fr}}</style><h1>候选交叉净区预筛v8</h1>
<p>NOTICE</p><p>默认仍为5秒／512候选，四阶段顺序与预算分配保持原样。四类简化操作共用与完整验收一致的交叉净区预筛，
提前剔除已能确认的非法候选；通过预筛不代表成功，最终仍需完整验收。</p>
<p><strong>页面首先展示当前关注的v7图23成品继续v8：</strong>它与后面的旧v5、v6起点是三个独立试验。
不同起点的历史优化次数不同，不能把追加一次5秒的结果当成全新flow一次生成的保证。</p>
<p>三组图23使用独立诊断副本：<b style="color:#c0262d">红色IREF</b>，<b style="color:#1d4ed8">蓝色net9</b>。
仅标色和强调线宽，线路坐标与对应正式成品一致。突出距离相对真实端点包围盒，包含正常固定出线，不代表全部外围导线都能删除。</p>
<p>分操作的预筛完成数等于通过预筛进入完整验收数加预筛拒绝数；阶段截止中断中的预筛只计入全局attempted，不记为完成。
全局预筛统计及前16个拒绝示例在各图的展开记录中保存。</p>
<p>SUMMARY；<a href="comparison.json">完整JSON记录</a>。</p>SECTIONS</html>'''
    summary = f'完整验收{len(valid)}/{len(records)}；同一运行代码：{same_runtime}'
    (output / 'index.html').write_text(page.replace('NOTICE', escape(NOTICE)).replace(
        'SUMMARY', escape(summary)).replace('SECTIONS', '\n'.join(sections)), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', type=Path, default=ROOT / 'output/candidate-clearance-v8')
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
