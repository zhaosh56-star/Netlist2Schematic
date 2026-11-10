#!/usr/bin/env python3
"""Compare v6 route simplification on saved geometry, without SA or A*."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
from html import escape
import io
import json
from math import isfinite
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

from compare_route_tracks import (PRESERVED_FIELDS, _code_hashes, _json, _no_search,
                                 _resolve, _sha, _write, execute as _execute)

FORMAT = 'route-scheduling-comparison-v6'
DEFAULT_CASES = {
    **{str(i): f'output/{i}/{i}-placement.json' for i in range(1, 5)},
    '5': 'output/5-dut/5-dut-placement.json',
    '6': 'output/6/6-placement.json',
    '7': 'output/7/7-placement.json',
    'data23': ('output/structure-layout-comparison/runs/data23/off/'
               '463fe7976a881a0f/operational-amplifier-dut-placement.json'),
}
NOTICE = ('本报告对同一份保存成品继续进行后处理，布局、引脚和原始线路固定，'
          '不重新运行SA、A*或恢复链。图1～3的来源为v3，图4～7为v4，图23为v5；'
          '这些历史成品不是同一份未简化路线的v5/v6公平对照，'
          '不能把全部收益归因于调度变化。')


def _detours(before, after):
    old = before['quality'].get('per_net', {})
    new = after['quality'].get('per_net', {})
    return {name: {'before': old.get(name), 'after': new.get(name)}
            for name in sorted(set(old) | set(new))}


def execute(label, source, destination, seconds):
    """Reuse the existing independent geometric and redraw checks."""
    from compare_passive_layouts import _verify_saved
    import run_flow

    source = _resolve(source)
    original = _json(source)
    source_hash = _sha(source)
    record = _execute(label, source, destination, seconds)
    report = record['report']
    if seconds > 0 and report.get('version') != 'orthogonal-shortcuts-v6':
        raise ValueError('This comparison requires the v6 simplifier implementation')
    code = _code_hashes()
    signature = {'source_netlist': str(_resolve(record['before']['placement_json'])),
                 'cell': original.get('source_cell')}
    # _verify_saved expects the actual netlist, not the source placement file.
    netlist = Path(original['source_netlist'])
    signature['source_netlist'] = str((netlist if netlist.is_absolute()
                                      else source.parent / netlist).resolve())
    log = io.StringIO()
    with ExitStack() as stack, redirect_stdout(log):
        _no_search(stack)
        disabled = run_flow.simplify_placement(
            source, out_dir=destination / 'off-check', simplify_routes='off',
            simplify_seconds=seconds)
        disabled_path = destination / 'off-check' / source.name
        disabled_verified = _verify_saved(disabled_path, signature)
        for field in PRESERVED_FIELDS:
            if original.get(field) != disabled.get(field):
                raise ValueError(f'{label}: disabled simplification changed {field}')
        if (run_flow._routes_sha256(list(original['routes'].items()))
                != run_flow._routes_sha256(list(disabled['routes'].items()))
                or original['metrics'] != disabled['metrics']):
            raise ValueError(f'{label}: disabled simplification changed wire geometry')
    if _sha(source) != source_hash or _code_hashes() != code:
        raise ValueError('Source or runtime changed during disabled compatibility check')
    record.update(format=FORMAT, comparison_kind='continue_saved_valid_routes',
                  baseline_is_unsimplified=False, disabled_routes_unchanged=True,
                  disabled_verified=disabled_verified,
                  per_net=_detours(record['before'], record['after']),
                  runtime_version=report.get('version'),
                  stages=report.get('stages', report.get('stage_reports', [])),
                  comparison_tool_sha256=_sha(__file__))
    if label == 'data23':
        record['focus_nets'] = {name: record['per_net'].get(name)
                                for name in ('iref', 'net9')}
    (destination / 'disabled-verification.log').write_text(log.getvalue(), encoding='utf-8')
    _write(destination / 'verification.json', record)
    return record


def _rel(output, path):
    return escape(Path(os.path.relpath(path, output)).as_posix(), quote=True)


def _figures(output, record):
    figures = []
    for caption, row in (('保存的原成品', record['before']), ('v6继续安全简化', record['after'])):
        metrics = row['metrics']
        image = _rel(output, row['clean_svg'])
        figures.append(f'<figure><figcaption>{caption}：线长{metrics["wirelength"]:g} / '
                       f'交叉{metrics["crossings"]} / 折角{metrics["bends"]}</figcaption>'
                       f'<a href="{image}"><img src="{image}" loading="lazy"></a>'
                       f'<p><a href="{_rel(output, row["placement_json"])}">布局JSON</a> · '
                       f'<a href="{_rel(output, row["color_svg"])}">彩色图</a> · '
                       f'画布{row["width"]:g}×{row["height"]:g}</p></figure>')
    return '<div class="pair">' + ''.join(figures) + '</div>'


def _operation_table(report):
    labels = {'chain_shortcut': '直线／L形', 'leaf_reattachment': '支线重接',
              'three_segment_shortcut': '三段折线', 'track_compaction': '轨道内收'}
    rows = []
    for name, label in labels.items():
        counts = report.get('operations', {}).get(name, {})
        rows.append(f'<tr><td>{label}</td><td>{counts.get("considered", 0)}</td>'
                    f'<td>{counts.get("validated", 0)}</td><td>{counts.get("accepted", 0)}</td>'
                    f'<td>{counts.get("geometry_rejected", 0)}</td></tr>')
    return ('<table><thead><tr><th>操作</th><th>考虑</th><th>完整验收</th>'
            '<th>接受</th><th>几何拒绝</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table>')


def _net_table(record):
    nets = sorted(record['per_net'].items(),
                  key=lambda row: -(row[1]['before'] or {}).get('excess_detour', 0))[:6]
    rows = []
    for name, values in nets:
        before, after = values['before'] or {}, values['after'] or {}
        old_factor, new_factor = before.get('detour_factor'), after.get('detour_factor')
        factor = (f'{old_factor:.2f} → {new_factor:.2f}'
                  if old_factor is not None and new_factor is not None else '—')
        rows.append(f'<tr><td>{escape(name)}</td><td>{before.get("wirelength", 0):g} → '
                    f'{after.get("wirelength", 0):g}</td><td>{factor}</td>'
                    f'<td>{before.get("outside_length", 0):g} → '
                    f'{after.get("outside_length", 0):g}</td></tr>')
    return ('<table><thead><tr><th>原图主要绕行网</th><th>线长</th><th>线长／端点跨度</th>'
            '<th>端点包围盒外线长</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table>')


def publish(output, records, seconds):
    valid = [row for row in records if row['status'] == 'valid']
    totals = {which: {metric: sum(row[which]['metrics'][metric] for row in valid)
                       for metric in ('wirelength', 'crossings', 'bends')}
              for which in ('before', 'after')}
    report = {'format': FORMAT, 'generated_utc': datetime.now(timezone.utc).isoformat(),
              'notice': NOTICE, 'simplification_seconds': seconds,
              'valid_cases': len(valid), 'case_count': len(records),
              'all_valid': len(valid) == len(records), 'totals': totals, 'cases': records}
    _write(output / 'comparison.json', report)
    sections = []
    for record in records:
        title = '图23：operational-amplifier' if record['case'] == 'data23' else '图' + record['case']
        if record['status'] != 'valid':
            sections.append(f'<section><h2>{escape(title)}</h2><p>失败：'
                            f'{escape(record["error"])}</p></section>')
            continue
        detail = record['report']
        summary = (f'来源：{record["source_simplifier_version"]} / '
                   f'{record["source_simplifier_stop_reason"]}；'
                   f'v6停止：{detail.get("stop_reason")}；'
                   f'简化耗时{detail.get("elapsed_seconds")}秒；'
                   '完整验收、双图同线、独立重绘、关闭简化不改线、重复回放和输入保护均通过。')
        stages = escape(json.dumps(record['stages'], ensure_ascii=False, indent=2))
        sections.append(f'<section><h2>{escape(title)}</h2><p>{escape(summary)}</p>'
                        + _figures(output, record) + _operation_table(detail) + _net_table(record)
                        + f'<details><summary>逐阶段配额与实际执行记录</summary><pre>{stages}</pre></details>'
                        + f'<p><a href="{_rel(output, Path(record["after"]["placement_json"]).parent / "verification.json")}">完整验收记录</a></p></section>')
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>缩线调度 v6：保存成品对照</title><style>
body{font:15px/1.65 system-ui;margin:24px auto;max-width:1600px;padding:0 20px;color:#18212b;background:#f4f6f8}
section{background:white;padding:20px;margin:22px 0;border:1px solid #d8dfe6;border-radius:8px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:20px}
figure{margin:0}figcaption{font-weight:600}img{width:100%;height:650px;object-fit:contain;border:1px solid #ddd}a{color:#1d4ed8}
table{border-collapse:collapse;margin:16px 0}td,th{padding:5px 14px;border:1px solid #d8dfe6;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}
@media(max-width:850px){.pair{grid-template-columns:1fr}}</style><h1>缩线调度 v6：图1～7与图23</h1>
<p>NOTICE</p><p>默认仍为5秒、最多512候选；四阶段按累计25%／50%／75%／100%的时间与候选配额前进。
前阶段没有收敛时允许让出；每次接受后始终使用最新已验收线路。仅接受线长、交叉和折角均不增加的合法改进。</p>
<p>绕行倍数使用端点包围盒半周长作为筛查参考，多端树可能自然超过该跨度；不代表所有超出长度都能删除。
阶段已激活、实际尝试和自然收敛分别记录。图23继续存在的绕行须结合完整记录判断，不宣称全部消除。</p>
<p>SUMMARY；<a href="comparison.json">完整JSON对照</a>。</p>SECTIONS</html>'''
    summary = f'完整验收{len(valid)}/{len(records)}；总线长{totals["before"]["wirelength"]:g}→{totals["after"]["wirelength"]:g}，交叉{totals["before"]["crossings"]}→{totals["after"]["crossings"]}，折角{totals["before"]["bends"]}→{totals["after"]["bends"]}'
    (output / 'index.html').write_text(page.replace('NOTICE', escape(NOTICE)).replace(
        'SUMMARY', escape(summary)).replace('SECTIONS', '\n'.join(sections)), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', type=Path, default=ROOT / 'output/route-scheduling-v6')
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
