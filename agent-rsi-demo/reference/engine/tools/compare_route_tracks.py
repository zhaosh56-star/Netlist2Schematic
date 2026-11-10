#!/usr/bin/env python3
"""Validate bounded route-track compaction on saved layouts, without SA/A*."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
import hashlib
from html import escape
import io
import json
from math import isfinite
import os
from pathlib import Path
import sys
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_CASES = {
    **{str(i): f'output/{i}/{i}-placement.json' for i in range(1, 5)},
    '5': 'output/5-dut/5-dut-placement.json',
    '6': 'output/6/6-placement.json',
    '7': 'output/7/7-placement.json',
    'mixed-devices': 'output/mixed-devices/mixed-devices-placement.json',
    '6-flexible': 'output/passive-flex-comparison/default/6/flexible/9c2c4b068513c363/6-placement.json',
    '7-flexible': 'output/passive-flex-comparison/default/7/flexible/73bade1f5c202b81/7-placement.json',
}
PRESERVED_FIELDS = ('placement', 'layout_grid', 'levels', 'structs', 'routing_stage',
                    'source_netlist', 'source_cell', 'source_sha256', 'candidate_id',
                    'candidate_seed', 'energy', 'optimization_metrics',
                    'passive_layout_policy', 'passive_layout', 'recovery')


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _resolve(path):
    path = Path(path).expanduser()
    return (path if path.is_absolute() else ROOT / path).resolve()


def _code_hashes():
    names = ('simplify_routes.py', 'route_tracks.py', 'route_patterns.py',
             'route_reattachment.py', 'sa_routing.py', 'routing_rules.py',
             'run_flow.py', 'sa_saved.py', 'sa_render.py', 'symbol-geometry.json')
    return {str(ROOT / name): _sha(ROOT / name) for name in names}


def _no_search(stack):
    from sa_common import load_local
    par = load_local('place_and_route', 'place-and-route.py')
    names = ('orthogonal_router.route_scene', 'orthogonal_router._find_path',
             'run_flow.final_route', 'run_flow._normal_result',
             'run_flow._try_local_rescue', 'structured_placement._anneal',
             'fallback_bus.create_fallback')
    for name in names:
        stack.enter_context(patch(name, side_effect=AssertionError(f'Unexpected search: {name}')))
    stack.enter_context(patch.object(par, 'sa', side_effect=AssertionError('Unexpected baseline SA')))


def execute(label, source, destination, seconds):
    import run_flow
    from compare_passive_layouts import _source_files, _verify_saved

    source = _resolve(source)
    original = _json(source)
    source_stem = source.name.removesuffix('-placement.json')
    source_files = {str(source): _sha(source)}
    for suffix in ('-clean.svg', '-layered.svg'):
        image = source.with_name(source_stem + suffix)
        if image.is_file():
            source_files[str(image)] = _sha(image)
    netlist = Path(original['source_netlist'])
    if not netlist.is_absolute():
        netlist = source.parent / netlist
    source_files.update(_source_files(netlist))
    signature = {'source_netlist': str(netlist.resolve()), 'cell': original.get('source_cell')}
    code = _code_hashes()
    before = _verify_saved(source, signature)
    destination.mkdir(parents=True, exist_ok=True)
    log = io.StringIO()
    record = {'case': label, 'source': str(source), 'source_files': source_files,
              'code_files': code, 'seconds': seconds, 'status': 'running',
              'source_simplifier_version': original.get('route_simplification', {}).get('version'),
              'source_simplifier_stop_reason': original.get('route_simplification', {}).get('stop_reason'),
              'before': before}
    started = perf_counter()
    with ExitStack() as stack, redirect_stdout(log):
        _no_search(stack)
        result = run_flow.simplify_placement(source, out_dir=destination,
                                             simplify_seconds=seconds)
        saved = destination / source.name
        after = _verify_saved(saved, signature)
        for field in PRESERVED_FIELDS:
            if original.get(field) != result.get(field):
                raise ValueError(f'{label}: changed saved field {field}')
        for metric in ('wirelength', 'crossings', 'bends'):
            if result['metrics'][metric] > original['metrics'][metric] + 1e-9:
                raise ValueError(f'{label}: {metric} increased')
        if original.get('validation', {}).get('search') != result.get('validation', {}).get('search'):
            raise ValueError(f'{label}: changed original routing search diagnostics')
        replay = run_flow.simplify_placement(saved, out_dir=destination / 'replay-check',
                                             simplify_seconds=seconds)
        for field in ('placement', 'layout_grid', 'routes', 'metrics'):
            if replay.get(field) != result.get(field):
                raise ValueError(f'{label}: repeated saved replay changed {field}')
        if seconds > 0 and not replay['route_simplification'].get('verified_saved_replay'):
            raise ValueError(f'{label}: same-version saved replay did not use verified fingerprint')
    for name, expected in source_files.items():
        if _sha(name) != expected:
            raise ValueError(f'Original input/artifact changed: {name}')
    if _code_hashes() != code:
        raise ValueError('Algorithm files changed while this case was running')
    report = result['route_simplification']
    record.update(status='valid', elapsed_seconds=round(perf_counter() - started, 6),
                  after=after, report=report, no_sa_or_astar=True,
                  original_inputs_unchanged=True, preserved_fields=list(PRESERVED_FIELDS),
                  saved_replay_identical=True,
                  v4_phase_matches_saved_metrics=(report.get('v4_metrics') == original['metrics'])
                    if report.get('v4_completed') else None,
                  replay_verified=replay['route_simplification'].get('verified_saved_replay', False))
    (destination / 'verification.log').write_text(log.getvalue(), encoding='utf-8')
    _write(destination / 'verification.json', record)
    return record


def _rel(output, path):
    return Path(os.path.relpath(path, output)).as_posix()


def publish(output, records, seconds):
    report = {'format': 'route-track-compaction-regression-v1',
              'generated_utc': datetime.now(timezone.utc).isoformat(),
              'simplification_seconds': seconds, 'cases': records,
              'valid_cases': sum(r['status'] == 'valid' for r in records)}
    _write(output / 'comparison.json', report)
    sections = []
    for record in records:
        title = escape(record['case'])
        if record['status'] != 'valid':
            sections.append(f'<section><h2>{title}</h2><p>失败：{escape(record["error"])}</p></section>')
            continue
        before, after, detail = record['before'], record['after'], record['report']
        cells = []
        for caption, value in (('保存的原路线', before), ('v5安全简化', after)):
            m = value['metrics']
            image = escape(_rel(output, value['clean_svg']), quote=True)
            data = escape(_rel(output, value['placement_json']), quote=True)
            cells.append(f'<figure><figcaption>{caption}：WL={m["wirelength"]:g} / '
                         f'交叉={m["crossings"]} / 折角={m["bends"]}</figcaption>'
                         f'<a href="{image}"><img src="{image}" loading="lazy"></a>'
                         f'<p><a href="{data}">布局JSON</a>；画布{value["width"]:g}×{value["height"]:g}</p></figure>')
        accepted = detail.get('operations', {}).get('track_compaction', {}).get('accepted', 0)
        summary = (f'来源简化版本：{record["source_simplifier_version"]}（{record["source_simplifier_stop_reason"]}）；v4阶段完成：{detail.get("v4_completed")}；'
                   f'v4阶段与保存指标一致：{record["v4_phase_matches_saved_metrics"]}；'
                   f'新动作接受{accepted}次；停止原因：{detail.get("stop_reason")}；'
                   f'简化耗时：{detail.get("elapsed_seconds")}秒。')
        sections.append(f'<section><h2>{title}</h2><p>{escape(summary)}</p><div class="pair">'
                        + ''.join(cells) + '</div></section>')
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>原路径轨道收紧 v5 对照</title><style>
body{font:15px/1.65 system-ui;margin:24px auto;max-width:1500px;padding:0 20px;color:#18212b;background:#f4f6f8}
section{background:white;padding:20px;margin:22px 0;border:1px solid #d8dfe6;border-radius:8px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:20px}
figure{margin:0}figcaption{font-weight:600}img{width:100%;height:650px;object-fit:contain;border:1px solid #ddd}a{color:#1d4ed8}
@media(max-width:850px){.pair{grid-template-columns:1fr}}</style><h1>原路径轨道收紧 v5：保存路线后处理对照</h1>
<p>全部跳过SA、A*和恢复链，保持器件、物理坐标、引脚及原几何规则。旧步骤稳定后才启用新动作，仍共用5秒、4轮、512候选预算。</p>
<p>每份成功产物均独立复验连接、固定出线及几何规则，确认彩色/黑白SVG线路一致、独立重绘一致、保存结果重复回放稳定。原输入和主图保持原字节。</p>
<p>旧来源可能是v3，或曾因预算截止而尚未收紧完成；版本、原停止原因和v4阶段指标是否一致分别注明，不把旧图当成同版本稳定结果。完整记录见<a href="comparison.json">comparison.json</a>。</p>SECTIONS</html>'''
    (output / 'index.html').write_text(html.replace('SECTIONS', '\n'.join(sections)), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', type=Path, default=ROOT / 'output/track-compaction-v5')
    parser.add_argument('--cases', nargs='+', choices=list(DEFAULT_CASES))
    parser.add_argument('--seconds', type=float, default=5)
    args = parser.parse_args(argv)
    if not isfinite(args.seconds) or args.seconds < 0:
        parser.error('seconds must be non-negative and finite')
    output = args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    records = []
    selected = args.cases or list(DEFAULT_CASES)
    for label in selected:
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
    return int(report['valid_cases'] != len(records))


if __name__ == '__main__':
    raise SystemExit(main())
