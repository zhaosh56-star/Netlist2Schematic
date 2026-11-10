#!/usr/bin/env python3
"""Compare independent additional simplification budgets on one saved result."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from html import escape
import json
from math import isfinite
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

from compare_cross_clearance import _figures, _operation_table
from compare_route_scheduling import _detours, _rel
from compare_route_tracks import _json, _resolve, _sha, _write, execute as _execute

FORMAT = 'saved-route-budget-comparison-v1'
DEFAULT_SOURCE = ROOT / 'output/23/operational-amplifier-dut-placement.json'
DEFAULT_REFERENCE = (ROOT / 'output/candidate-clearance-v8/data23-v7-v6/'
                     'operational-amplifier-dut-placement.json')


def _net_geometry(data):
    result = {}
    for net in ('iref', 'net9'):
        segments = data.get('routes', {}).get(net, [])
        points = [point for segment in segments for point in segment]
        tracks = [a[1] for a, b in segments if a[1] == b[1]]
        if points:
            result[net] = {'top_track_y': min(tracks) if tracks else None,
                           'minimum_y': min(point[1] for point in points),
                           'leftmost_x': min(point[0] for point in points)}
    return result


def _reference(path, source):
    """Read the reference as evidence, never optimize or reroute it."""
    if path is None:
        return None
    path = _resolve(path)
    data = _json(path)
    baseline = _json(source)
    if (data.get('source_sha256') != baseline.get('source_sha256')
            or data.get('source_cell') != baseline.get('source_cell')):
        raise ValueError('Reference and starting placement must use the same source netlist/cell')
    stem = path.name.removesuffix('-placement.json')
    protected = {str(path): _sha(path)}
    for suffix in ('-clean.svg', '-layered.svg'):
        image = path.with_name(stem + suffix)
        if image.is_file():
            protected[str(image)] = _sha(image)
    return {'placement_json': str(path), 'metrics': data['metrics'],
            'clean_svg': str(path.with_name(stem + '-clean.svg')),
            'version': data.get('route_simplification', {}).get('version'),
            'last_budget_seconds': data.get('route_simplification', {}).get('budget_seconds'),
            'outer_geometry': _net_geometry(data), 'protected_files': protected,
            'optimized_or_rerouted': False,
            'metric_source': 'saved_json_previously_independently_verified'}


def execute(source, destination, seconds):
    """Only called by the explicit --rerun path, never by default publishing."""
    source = _resolve(source)
    label = f'{seconds:g}s'
    record = _execute(label, source, destination, seconds)
    return _enrich(record, source)


def _enrich(record, source):
    """Read saved JSON only; do not validate, render, route or optimize again."""
    source = _resolve(source)
    original = _json(source)
    saved = Path(record['after']['placement_json'])
    after = _json(saved)
    if record.get('source') != str(source):
        raise ValueError('A reused record refers to a different starting placement')
    if record.get('report') != after.get('route_simplification'):
        raise ValueError('Saved result and execution report disagree; refuse mixed timing/geometry')
    expected = record.get('source_files', {}).get(str(source))
    if expected is not None and _sha(source) != expected:
        raise ValueError('The original placement changed since the execution record was captured')
    if Path(after.get('simplified_from', '')).resolve() != source:
        raise ValueError('Budget case did not start directly from the requested original JSON')
    record.update(format=FORMAT,
                  starting_placement=str(source),
                  starting_metrics=original['metrics'],
                  starting_simplification_seconds=original.get('route_simplification', {}).get(
                      'budget_seconds'),
                  requested_additional_seconds=record['seconds'],
                  independent_from_other_cases=True,
                  not_a_fresh_total_budget=True,
                  per_net=_detours(record['before'], record['after']),
                  outer_geometry={'before': _net_geometry(original), 'after': _net_geometry(after)},
                  device_positions_and_axes_unchanged=(
                      original['placement'] == after['placement']
                      and original.get('layout_grid') == after.get('layout_grid')),
                  stages=record['report'].get('stages', []),
                  presentation_tool_sha256=_sha(__file__),
                  no_optimization_during_report=True)
    return record


def _focus_table(record):
    rows = []
    for net in ('iref', 'net9'):
        values = record['per_net'].get(net)
        if not values:
            continue
        before, after = values['before'], values['after']
        old_geo, new_geo = record['outer_geometry']['before'][net], record['outer_geometry']['after'][net]
        rows.append(f'<tr><td>{net}</td><td>{before["wirelength"]:g} → {after["wirelength"]:g}</td>'
                    f'<td>{old_geo["top_track_y"]} → {new_geo["top_track_y"]}</td>'
                    f'<td>{old_geo["leftmost_x"]} → {new_geo["leftmost_x"]}</td></tr>')
    return ('<table><thead><tr><th>重点网络</th><th>线长</th><th>最上方水平轨道y</th>'
            '<th>最左导线x</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table>')


def publish(output, records, source, reference, records_path=None):
    baseline = _json(source)
    valid = [row for row in records if row['status'] == 'valid']
    same_runtime = len({json.dumps(row['code_files'], sort_keys=True) for row in valid}) <= 1
    direct_starts = all(row['starting_placement'] == str(source) for row in valid)
    reference_unchanged = (reference is None or all(_sha(path) == expected
        for path, expected in reference['protected_files'].items()))
    report = {'format': FORMAT, 'generated_utc': datetime.now(timezone.utc).isoformat(),
              'starting_placement': str(source), 'starting_placement_sha256': _sha(source),
              'starting_metrics': baseline['metrics'],
              'starting_simplification_seconds': baseline.get('route_simplification', {}).get(
                  'budget_seconds'),
              'notice': ('每个案例独立读取同一份已完成默认5秒缩线的成品，分别追加指定预算；'
                         '不是从头flow的总15秒／30秒，也不是前一案例结果接着优化。'
                         '生产默认仍为5秒。9953历史参考只读，不进行优化或重布。'),
              'valid_cases': len(valid), 'case_count': len(records),
              'same_runtime_snapshot': same_runtime, 'all_cases_start_from_same_json': direct_starts,
              'reference': reference, 'reference_unchanged': reference_unchanged,
              'execution_records': str(records_path) if records_path else None,
              'execution_records_sha256': _sha(records_path) if records_path else None,
              'presentation_tool_sha256': _sha(__file__),
              'report_only_no_optimization': True,
              'all_valid': len(valid) == len(records) and same_runtime and direct_starts
                           and reference_unchanged,
              'cases': records}
    _write(output / 'comparison.json', report)
    rows = []
    for record in valid:
        after = record['after']['metrics']
        detail = record['report']
        rows.append(f'<tr><td>独立追加{record["requested_additional_seconds"]:g}秒</td>'
                    f'<td>{detail.get("elapsed_seconds")}秒</td><td>{after["wirelength"]:g}</td>'
                    f'<td>{after["crossings"]}</td><td>{after["bends"]}</td>'
                    f'<td>{escape(str(detail.get("stop_reason")))}</td>'
                    f'<td>{detail.get("candidate_counts", {}).get("considered", 0)}</td></tr>')
    baseline_metrics = baseline['metrics']
    initial_row = (f'<tr><td>共同起点：当前完整flow成品</td><td>此前已缩线5秒</td>'
                   f'<td>{baseline_metrics["wirelength"]:g}</td><td>{baseline_metrics["crossings"]}</td>'
                   f'<td>{baseline_metrics["bends"]}</td><td>保存结果，不重新运行</td><td>—</td></tr>')
    reference_row = ''
    reference_note = ''
    if reference:
        metrics = reference['metrics']
        reference_row = (f'<tr><td><a href="{_rel(output, reference["clean_svg"])}">历史增量参考</a></td>'
                         f'<td>只读，不优化</td><td>{metrics["wirelength"]:g}</td>'
                         f'<td>{metrics["crossings"]}</td><td>{metrics["bends"]}</td>'
                         '<td>不同历史优化次数</td><td>—</td></tr>')
        deltas = []
        for record in valid:
            current = record['after']['metrics']
            deltas.append(f'追加{record["requested_additional_seconds"]:g}秒：'
                          f'线长{current["wirelength"]-metrics["wirelength"]:+g}，'
                          f'交叉{current["crossings"]-metrics["crossings"]:+g}，'
                          f'折角{current["bends"]-metrics["bends"]:+g}')
        reference_note = '相对历史参考，' + '；'.join(deltas) + '。线长更短不代表全部美观指标都更好。'
    sections = []
    for record in records:
        title = f'独立追加{record["case"]}'
        if record['status'] != 'valid':
            sections.append(f'<section><h2>{escape(title)}</h2><p>失败：'
                            f'{escape(record["error"])}</p></section>')
            continue
        detail = record['report']
        summary = (f'从同一原JSON独立开始；实际简化耗时{detail.get("elapsed_seconds")}秒；'
                   f'停止原因{detail.get("stop_reason")}。候选上限'
                   f'{detail.get("parameters", {}).get("max_candidates")}，未增加候选数或修改几何规则。'
                   '完整验收、双SVG同线、独立重绘、原始文件保护与相同预算的保存回放稳定均通过。')
        stages = escape(json.dumps(record['stages'], ensure_ascii=False, indent=2))
        sections.append(f'<section><h2>{escape(title)}</h2><p>{escape(summary)}</p>'
                        + _figures(output, record) + _focus_table(record) + _operation_table(detail)
                        + f'<details><summary>四阶段时间与候选配额</summary><pre>{stages}</pre></details>'
                        + f'<p><a href="{_rel(output, Path(record["after"]["placement_json"]).parent / "verification.json")}">完整验证记录</a></p></section>')
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>图23：独立追加缩线预算对照</title><style>
body{font:15px/1.65 system-ui;margin:24px auto;max-width:1600px;padding:0 20px;color:#18212b;background:#f4f6f8}
section{background:white;padding:20px;margin:22px 0;border:1px solid #d8dfe6;border-radius:8px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:20px}
figure{margin:0}figcaption{font-weight:600}img{width:100%;height:650px;object-fit:contain;border:1px solid #ddd}a{color:#1d4ed8}
table{border-collapse:collapse;margin:16px 0}td,th{padding:5px 12px;border:1px solid #d8dfe6;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}
@media(max-width:850px){.pair{grid-template-columns:1fr}}</style><h1>图23：独立追加15秒／30秒缩线</h1>
<p>NOTICE</p><p>两次试验严格串行，所有器件、行列轴、朝向和原线路起点固定，不调用SA、A*或恢复链。
每次仍最多512个候选，保留原四阶段配额和独立完整验收；只接受线长、交叉和折角均不增加的合法改善。</p>
<table><thead><tr><th>案例</th><th>实际耗时</th><th>线长</th><th>交叉</th><th>折角</th><th>停止原因</th><th>考虑候选</th></tr></thead><tbody>ROWS</tbody></table>
<p>REFERENCE_NOTE</p><p>图片直接链接已验收的正式SVG，不重新绘图。IREF和net9线长、最上方轨道坐标分别列出。
报告未收敛或候选上限不代表数学无解。默认执行本工具仅复用isolated验证记录生成报告；只有显式--rerun才会在新目录重测。</p>
<p><a href="comparison.json">完整JSON记录</a>；SUMMARY。</p>SECTIONS</html>'''
    summary = f'独立验收{len(valid)}/{len(records)}；共同起点{direct_starts}；同一代码{same_runtime}'
    (output / 'index.html').write_text(page.replace('NOTICE', escape(report['notice'])).replace(
        'ROWS', initial_row + ''.join(rows) + reference_row).replace('REFERENCE_NOTE', escape(reference_note)).replace('SUMMARY', escape(summary)).replace(
        'SECTIONS', '\n'.join(sections)), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--placement', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--reference', type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument('--seconds', nargs='+', type=float, default=[15, 30])
    parser.add_argument('--out-dir', type=Path, default=ROOT / 'output/23-budget-check')
    parser.add_argument('--reuse-records', type=Path,
                        help='Read existing verified execution records; default: isolated-run-records.json')
    parser.add_argument('--rerun', action='store_true',
                        help='Explicitly authorize new serial simplification runs in a new directory')
    args = parser.parse_args(argv)
    if any(not isfinite(seconds) or seconds <= 0 for seconds in args.seconds):
        parser.error('seconds must be positive and finite')
    if len(set(args.seconds)) != len(args.seconds):
        parser.error('duplicate budgets are not allowed')
    source = _resolve(args.placement)
    output = _resolve(args.out_dir)
    if source.parent == output or source.parent in output.parents:
        parser.error('output must not be inside the original artifact directory')
    reference = _reference(args.reference, source)
    output.mkdir(parents=True, exist_ok=True)
    if not args.rerun:
        records_path = _resolve(args.reuse_records or output / 'isolated-run-records.json')
        if not records_path.is_file():
            parser.error('verified execution records are missing; provide --reuse-records or explicitly use --rerun')
        available = _json(records_path)
        if not isinstance(available, list):
            parser.error('reused execution records must be a list')
        records = []
        for seconds in args.seconds:
            matches = [row for row in available if row.get('seconds') == seconds]
            if len(matches) != 1:
                parser.error(f'expected one verified execution record for {seconds:g}s')
            records.append(_enrich(matches[0], source))
        report = publish(output, records, source, reference, records_path)
        print(f'Republished without optimization: {report["valid_cases"]}/{len(records)}; '
              f'{output / "index.html"}', flush=True)
        return int(not report['all_valid'])
    rerun_root = output / ('rerun-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f'))
    records_path = rerun_root / 'run-records.json'
    records = []
    for seconds in args.seconds:
        label = f'{seconds:g}s'
        print(f'START independent {label}: {source}', flush=True)
        try:
            record = execute(source, rerun_root / label, seconds)
        except Exception as exc:
            record = {'case': label, 'status': 'failed', 'error': f'{type(exc).__name__}: {exc}'}
            print(f'FAIL {label}: {record["error"]}', flush=True)
        else:
            print(f'VALID {label}: {record["before"]["metrics"]} -> {record["after"]["metrics"]}; '
                  f'{record["report"]["stop_reason"]}', flush=True)
        records.append(record)
        _write(records_path, records)
        publish(output, records, source, reference, records_path)
    report = publish(output, records, source, reference, records_path)
    print(f'Complete: {report["valid_cases"]}/{len(records)}; {output / "index.html"}', flush=True)
    return int(not report['all_valid'])


if __name__ == '__main__':
    raise SystemExit(main())
