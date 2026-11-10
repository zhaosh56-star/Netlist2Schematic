#!/usr/bin/env python3
"""Publish root-owned fixed-5s/auto timing records; never run optimization."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

from compare_route_scheduling import _rel
from compare_route_tracks import _json, _resolve, _sha, _write

FORMAT = 'auto-simplification-budget-comparison-v1'
DEFAULT_OUTPUT = ROOT / 'output/auto-simplify-budget'
DEFAULT_SOURCES = {
    **{str(i): f'output/{i}/{i}-placement.json' for i in range(1, 5)},
    '5': 'output/5-dut/5-dut-placement.json',
    '6': 'output/6/6-placement.json',
    '7': 'output/7/7-placement.json',
    '23': 'output/23/operational-amplifier-dut-placement.json',
}


def _classification(data):
    report = data.get('route_simplification')
    previous = data.get('budget_comparison_input', {}).get('previous_simplification')
    if isinstance(previous, dict) and previous.get('version'):
        return 'continued'
    if data.get('route_probe') or data.get('source_route_simplification'):
        return 'manual_or_previously_simplified'
    if report is None:
        if data.get('snapshot_status') == 'before_simplification':
            return 'confirmed_before_simplification_snapshot'
        return 'legacy_unrecorded'
    if (report.get('mode') == 'off'
            or report.get('stop_reason') in ('disabled', 'zero_budget')):
        # A disabled second pass does not undo simplification of its input.
        if data.get('simplified_from'):
            return 'continued_disabled_copy'
        return 'explicitly_disabled_or_zero'
    return 'continued'


def _source_summary(path):
    path = _resolve(path)
    data = _json(path)
    source = Path(data.get('source_netlist', ''))
    source = (source if source.is_absolute() else path.parent / source).resolve()
    matches = (source.is_file() and data.get('source_sha256') is not None
               and _sha(source) == data['source_sha256'])
    classification = _classification(data)
    return {'placement_json': str(path), 'placement_sha256': _sha(path),
            'source_netlist': str(source), 'source_cell': data.get('source_cell'),
            'source_hash_matches': bool(matches),
            'validation_flag': data.get('validation', {}).get('valid') is True,
            'has_routes': isinstance(data.get('routes'), dict),
            'classification': classification,
            'raw_claim': classification in ('confirmed_before_simplification_snapshot',
                                             'explicitly_disabled_or_zero'),
            'independently_revalidated_by_this_publisher': False,
            'metrics': data.get('metrics'),
            'recorded_version': (data.get('route_simplification') or {}).get('version'),
            'comparison_cache_was_moved': 'budget_comparison_input' in data,
            'previous_simplification_version': (data.get('budget_comparison_input', {}).get(
                'previous_simplification') or {}).get('version'),
            'simplified_from': data.get('simplified_from'),
            'rerouted_from': data.get('rerouted_from')}


def discover_sources():
    """Read-only scan; no parsing into components, validation or rendering."""
    command = ['rg', '-l', '--hidden', '--no-ignore', '-g', '*.json',
               '-g', '!**/node_modules/**', '-g', '!**/site-packages/**',
               r'\x22routes\x22\s*:', 'output', '.cache']
    process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
    if process.returncode not in (0, 1):
        raise ValueError(process.stderr.strip() or 'Source scan failed')
    counts, candidates = Counter(), []
    for name in process.stdout.splitlines():
        path = ROOT / name
        counts['json_files_with_routes_key'] += 1
        try:
            data = _json(path)
        except (ValueError, OSError):
            counts['unreadable'] += 1
            continue
        if (not isinstance(data, dict) or not isinstance(data.get('routes'), dict)
                or data.get('validation', {}).get('valid') is not True):
            counts['not_a_saved_valid_result'] += 1
            continue
        kind = _classification(data)
        counts[kind] += 1
        if kind == 'continued':
            continue
        row = _source_summary(path)
        if row['source_hash_matches']:
            candidates.append(row)
        else:
            counts['source_hash_missing_or_mismatch'] += 1
    return {'format': 'saved-route-source-exploration-v1',
            'generated_utc': datetime.now(timezone.utc).isoformat(),
            'no_optimization_or_validation_run': True, 'counts': dict(counts),
            'candidates': candidates,
            'note': ('off-check的输入仍可能已经简化，不能把禁用第二次简化当成原始路线；'
                     '无简化记录的旧文件只能标为legacy_unrecorded，不能证明绝无历史处理。')}


def _policy(record):
    policy = record.get('policy', record.get('requested_policy'))
    if policy in ('fixed5', 'fixed', '5', 5):
        return 'fixed5'
    if policy == 'auto':
        return 'auto'
    raise ValueError(f'Unknown/missing timing policy: {policy!r}')


def _read_records(path):
    rows = _json(path)
    rows = rows.get('records', rows.get('cases', [])) if isinstance(rows, dict) else rows
    if not isinstance(rows, list):
        raise ValueError('Execution records must be a list or an object containing records')
    pairs = {}
    for record in rows:
        source = _resolve(record['source'])
        policy = _policy(record)
        pair = pairs.setdefault(str(source), {'case': str(record.get('case', source.stem)),
                                               'source': _source_summary(source), 'results': {}})
        declared = record.get('input_kind', record.get('source_kind'))
        if record.get('original_source'):
            pair['source']['original_placement_json'] = record['original_source']
        if declared is not None:
            previous = pair.setdefault('declared_input_kind', declared)
            if previous != declared:
                raise ValueError('Two policies declare different provenance for the same input')
            pair['source']['execution_record_input_kind'] = declared
            if declared == 'continued':
                pair['source'].update(classification='continued', raw_claim=False)
            if declared in ('raw', 'before_simplification', 'raw_before_simplification'):
                if pair['source']['classification'] in ('continued', 'continued_disabled_copy',
                                                         'manual_or_previously_simplified'):
                    raise ValueError('A raw-input declaration conflicts with recorded simplification history')
                pair['source'].update(classification='root_captured_before_simplification', raw_claim=True,
                                      classification_evidence='root_owned_execution_record')
        if policy in pair['results']:
            raise ValueError(f'Duplicate {policy} record for the same input JSON: {source}')
        if record.get('status') == 'valid':
            saved = _json(record['after']['placement_json'])
            if record.get('report') != saved.get('route_simplification'):
                raise ValueError('Output and execution report disagree; refuse mixed timing records')
            expected = record.get('source_files', {}).get(str(source))
            if expected is not None and _sha(source) != expected:
                raise ValueError('Input JSON changed after the recorded execution')
            record['source_positions_and_axes_unchanged'] = (
                saved.get('placement') == _json(source).get('placement')
                and saved.get('layout_grid') == _json(source).get('layout_grid'))
        pair['results'][policy] = record
    for pair in pairs.values():
        fixed, auto = (pair['results'].get(policy) for policy in ('fixed5', 'auto'))
        pair['same_input_pair_complete'] = bool(fixed and auto)
        pair['valid'] = bool(fixed and auto and fixed.get('status') == 'valid'
                             and auto.get('status') == 'valid'
                             and pair['source']['source_hash_matches']
                             and pair['source']['validation_flag']
                             and fixed['before'].get('valid') and auto['before'].get('valid')
                             and fixed['after'].get('valid') and auto['after'].get('valid')
                             and fixed['before']['metrics'] == auto['before']['metrics']
                             and fixed.get('source_positions_and_axes_unchanged')
                             and auto.get('source_positions_and_axes_unchanged'))
    return list(pairs.values())


def _image(output, row, caption):
    image = Path(row['clean_svg'])
    if not image.is_file():
        return f'<figure><figcaption>{escape(caption)}</figcaption><p>此起点没有保存SVG。</p></figure>'
    return (f'<figure><figcaption>{escape(caption)}</figcaption>'
            f'<a href="{_rel(output, image)}"><img src="{_rel(output, image)}" loading="lazy"></a>'
            f'<p><a href="{_rel(output, row["placement_json"])}">布局JSON</a></p></figure>')


def publish(output, pairs, records_path=None, exploration=None, raw_skips=None):
    valid = [pair for pair in pairs if pair['valid']]
    snapshots = {json.dumps(row.get('code_files'), sort_keys=True)
                 for pair in pairs for row in pair['results'].values() if row.get('code_files')}
    report = {'format': FORMAT, 'generated_utc': datetime.now(timezone.utc).isoformat(),
              'state': 'complete' if pairs and len(valid) == len(pairs) else 'awaiting_or_failed_records',
              'default_production_seconds': 5, 'publisher_runs_optimization': False,
              'execution_records': str(records_path) if records_path else None,
              'execution_records_sha256': _sha(records_path) if records_path else None,
              'presentation_tool_sha256': _sha(__file__), 'pair_count': len(pairs),
              'valid_pairs': len(valid), 'same_runtime_snapshot': len(snapshots) == 1,
              'raw_pairs': sum(pair['source']['raw_claim'] for pair in pairs),
              'continued_pairs': sum(pair['source']['classification'] == 'continued' for pair in pairs),
              'raw_skips': raw_skips or [],
              'notice': ('每对fixed5/auto分别从同一个保存JSON独立开始。主图1～7及图23均按continued标注，'
                         '不冒充未简化路线，也不是从头flow对照。根新捕获的before_simplification起点另列证据。'
                         '默认预算仍为5秒，auto仅显式启用；候选上限仍512。'),
              'source_exploration': exploration, 'pairs': pairs}
    _write(output / 'comparison.json', report)
    summary_rows = []
    for pair in pairs:
        fixed, auto = (pair['results'].get(policy) for policy in ('fixed5', 'auto'))
        if not pair['valid']:
            continue
        fm, am = fixed['after']['metrics'], auto['after']['metrics']
        kind = '原始合法布线' if pair['source']['raw_claim'] else '成品继续'
        summary_rows.append(f'<tr><td>{escape(pair["case"])}</td><td>{kind}</td>'
                            f'<td>{fm["wirelength"]:g} / {fm["crossings"]} / {fm["bends"]}</td>'
                            f'<td>{auto["report"]["budget_seconds"]}</td>'
                            f'<td>{auto["report"]["elapsed_seconds"]}</td>'
                            f'<td>{am["wirelength"]:g} / {am["crossings"]} / {am["bends"]}</td>'
                            f'<td>{escape(str(auto["report"].get("stop_reason")))}</td></tr>')
    sections = []
    for pair in pairs:
        title = f'{pair["case"]}：' + ('原始合法布线捕获' if pair['source']['raw_claim'] else '已有成品继续')
        figures, table = [], []
        for policy in ('fixed5', 'auto'):
            row = pair['results'].get(policy)
            if not row or row.get('status') != 'valid':
                table.append(f'<tr><td>{policy}</td><td colspan="7">未完成或失败</td></tr>')
                continue
            detail, metrics = row['report'], row['after']['metrics']
            table.append(f'<tr><td>{policy}</td><td>{detail.get("budget_seconds")}</td>'
                         f'<td>{detail.get("elapsed_seconds")}</td>'
                         f'<td>{detail.get("budget_selection", {}).get("estimation_seconds", 0)}</td>'
                         f'<td>{metrics["wirelength"]:g}</td>'
                         f'<td>{metrics["crossings"]}</td><td>{metrics["bends"]}</td>'
                         f'<td>{escape(str(detail.get("stop_reason")))}</td></tr>')
            figures.append(_image(output, row['after'], policy))
        auto = pair['results'].get('auto', {})
        selection = auto.get('report', {}).get('budget_selection', {})
        sections.append(f'<section><h2>{escape(title)}</h2><p>同一输入完整配对：'
                        f'{pair["same_input_pair_complete"]}；已验收且位置、朝向和物理轴未变：{pair["valid"]}。</p>'
                        '<table><thead><tr><th>策略</th><th>解析预算秒</th><th>实际缩线秒</th><th>估计开销秒</th><th>线长</th>'
                        '<th>交叉</th><th>折角</th><th>停止原因</th></tr></thead><tbody>'
                        + ''.join(table) + '</tbody></table><div class="pair">' + ''.join(figures)
                        + '</div><details><summary>auto预算选择依据</summary><pre>'
                        + escape(json.dumps(selection, ensure_ascii=False, indent=2)) + '</pre></details></section>')
    if not pairs:
        sections.append('<section><p>等待根独占执行fixed5与auto后提供验证记录；本发布工具不会启动计算。</p></section>')
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>安全缩线：固定5秒与auto预算对照</title><style>
body{font:15px/1.65 system-ui;margin:24px auto;max-width:1600px;padding:0 20px;color:#18212b;background:#f4f6f8}
section{background:white;padding:20px;margin:22px 0;border:1px solid #d8dfe6;border-radius:8px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:20px}
figure{margin:0}figcaption{font-weight:600}img{width:100%;height:600px;object-fit:contain;border:1px solid #ddd}a{color:#1d4ed8}
table{border-collapse:collapse;margin:16px 0}td,th{padding:5px 12px;border:1px solid #d8dfe6;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}
@media(max-width:850px){.pair{grid-template-columns:1fr}}</style><h1>安全缩线预算：fixed5与auto</h1><p>NOTICE</p>
<p>auto可选择5／10／15／20／30秒，原几何规则、接受条件及最多512候选保持原样。实际提前触及候选上限或收敛时可以少于所选预算。
auto不保证每张图都优于固定5秒；增加时间不等于增加候选上限。</p><p><a href="comparison.json">完整记录</a>；仅发布已有数据，不运行SA、A*、简化或重绘。</p>SECTIONS</html>'''
    summary = ('<table><thead><tr><th>案例</th><th>起点</th><th>fixed5线长／交叉／折角</th>'
               '<th>auto预算秒</th><th>auto实际缩线秒</th><th>auto线长／交叉／折角</th><th>auto停止</th>'
               '</tr></thead><tbody>' + ''.join(summary_rows) + '</tbody></table>') if pairs else ''
    if raw_skips:
        sections.append('<section><details><summary>未纳入的原始起点</summary><pre>'
                        + escape(json.dumps(raw_skips, ensure_ascii=False, indent=2))
                        + '</pre></details></section>')
    (output / 'index.html').write_text(page.replace('NOTICE', escape(report['notice'])).replace(
        'SECTIONS', summary + '\n'.join(sections)), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records', type=Path,
                        help='Root-owned verified timing records; default: output/run-records.json')
    parser.add_argument('--out-dir', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--scan-sources', action='store_true', help='Read-only source/provenance scan')
    args = parser.parse_args(argv)
    output = _resolve(args.out_dir)
    output.mkdir(parents=True, exist_ok=True)
    exploration_path = output / 'source-exploration.json'
    if args.scan_sources:
        exploration = discover_sources()
        exploration['default_continued_sources'] = {
            case: _source_summary(path) for case, path in DEFAULT_SOURCES.items()}
        _write(exploration_path, exploration)
    else:
        exploration = _json(exploration_path) if exploration_path.is_file() else None
    records_path = _resolve(args.records or output / 'run-records.json')
    pairs = _read_records(records_path) if records_path.is_file() else []
    wrapper = _json(records_path) if records_path.is_file() else {}
    raw_skips = wrapper.get('raw_skips', []) if isinstance(wrapper, dict) else []
    report = publish(output, pairs, records_path if records_path.is_file() else None,
                     exploration, raw_skips)
    print(f'Published without optimization: {report["valid_pairs"]}/{report["pair_count"]} pairs; '
          f'{output / "index.html"}', flush=True)
    return int(bool(pairs) and report['valid_pairs'] != report['pair_count'])


if __name__ == '__main__':
    raise SystemExit(main())
