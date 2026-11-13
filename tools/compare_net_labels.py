#!/usr/bin/env python3
"""Serial, independently checked net-label views of existing complete routes.

This tool never runs layout, A*, recovery or route simplification. A fresh batch
owns a new run directory; historical outputs are never reported as fresh runs.
"""
from __future__ import annotations

import argparse
from collections import Counter
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
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]

FORMAT = 'net-label-comparison-v1'
DEFAULT_OUTPUT = ROOT / 'output/net-label-comparison'
EXAMPLE_SOURCES = {
    **{str(i): f'output/{i}/{i}-placement.json' for i in range(1, 5)},
    '5': 'output/5-dut/5-dut-placement.json',
    '6': 'output/6/6-placement.json',
    '7': 'output/7/7-placement.json',
}


def _resolve(path):
    path = Path(path).expanduser()
    return (path if path.is_absolute() else ROOT / path).resolve()


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def discover_sources():
    """Find the seven main examples and this batch's supported dataset outputs."""
    cases, skips = [], []
    for case, source in EXAMPLE_SOURCES.items():
        path = ROOT / source
        if path.is_file():
            cases.append({'case': case, 'title': '图' + case,
                          'family': 'examples', 'placement_json': str(path.resolve())})
        else:
            skips.append({'case': case, 'reason': 'saved_result_missing',
                          'placement_json': str(path)})
    summary = ROOT / 'output/test-data-flexible/summary.json'
    if summary.is_file():
        for record in _read(summary).get('cases', []):
            info = record.get('case_info', {})
            if record.get('status') != 'valid':
                skips.append({'case': info.get('case'), 'title': info.get('title'),
                              'reason': record.get('status'), 'error': record.get('error')})
                continue
            path = _resolve(record['verified']['placement_json'])
            cases.append({'case': info['case'], 'title': info['title'],
                          'family': info['family'], 'placement_json': str(path),
                          'ground_truth': info.get('ground_truth')})
    else:
        for directory in sorted((ROOT / 'output/test-data-flexible/runs').glob('data*')):
            files = list(directory.glob('*-placement.json'))
            if len(files) != 1:
                continue
            data = _read(files[0])
            source = Path(data.get('source_netlist', ''))
            truth = source.with_suffix('.svg')
            cases.append({'case': directory.name, 'title': source.stem,
                          'family': 'test_data', 'placement_json': str(files[0].resolve()),
                          'ground_truth': str(truth) if truth.is_file() else None})
    return cases, skips


def _source_files(cases):
    from compare_passive_layouts import _source_files as netlist_files
    frozen = {}
    for case in cases:
        placement = _resolve(case['placement_json'])
        frozen[str(placement)] = _sha(placement)
        data = _read(placement)
        source = Path(data['source_netlist'])
        source = source if source.is_absolute() else placement.parent / source
        frozen.update(netlist_files(source.resolve()))
        stem = placement.name.removesuffix('-placement.json')
        for suffix in ('-clean.svg', '-layered.svg', '-baseline.svg'):
            image = placement.with_name(stem + suffix)
            if image.is_file():
                frozen[str(image)] = _sha(image)
        if case.get('ground_truth'):
            truth = _resolve(case['ground_truth'])
            if truth.is_file():
                frozen[str(truth)] = _sha(truth)
    return dict(sorted(frozen.items()))


def _code_files():
    from netlist2schematic.common.sa_common import configure_resources, engine_source_files, resource_root
    configure_resources()
    files = set(engine_source_files(root=ROOT))
    files.add(Path(__file__).resolve())
    files.add(ROOT / 'symbol-geometry.json')
    files.update((resource_root() / 'packages/components/definitions').glob('*.json'))
    return {str(p): _sha(p) for p in sorted(files)}


def _changed(frozen):
    return [path for path, digest in frozen.items()
            if not Path(path).is_file() or _sha(path) != digest]


def _no_search(stack):
    from netlist2schematic.common.sa_common import load_local
    par = load_local('place_and_route', 'place-and-route.py')
    for name in ('netlist2schematic.routing.orthogonal_router.route_scene', 'netlist2schematic.routing.orthogonal_router._find_path',
                 'netlist2schematic.routing.sa_routing.final_route', 'netlist2schematic.flow.run_flow.final_route',
                 'netlist2schematic.flow.run_flow._normal_result', 'netlist2schematic.flow.run_flow._try_local_rescue',
                 'netlist2schematic.layout.structured_placement._anneal', 'netlist2schematic.recovery.fallback_bus.create_fallback',
                 'netlist2schematic.postprocess.simplify_routes.simplify_routes'):
        stack.enter_context(patch(name, side_effect=AssertionError('Unexpected optimization: ' + name)))
    stack.enter_context(patch.object(par, 'sa', side_effect=AssertionError('Unexpected baseline SA')))


def _wire_paths(path):
    root = ET.parse(path).getroot()
    return [(node.get('data-net'), node.get('points'))
            for node in root.iter() if node.get('data-net') is not None]


def execute(case, destination, seconds, frozen, code):
    from netlist2schematic.plugins.net_labels import net_labels
    source = _resolve(case['placement_json'])
    original = _read(source)
    original_hash = _sha(source)
    record = {'case': case['case'], 'case_info': case, 'source': str(source),
              'source_sha256_before': original_hash, 'seconds': seconds,
              'status': 'running', 'started_utc': datetime.now(timezone.utc).isoformat(),
              'source_metrics': original.get('metrics'),
              'optimization_disabled': ['SA', 'A*', 'recovery', 'simplification']}
    destination.mkdir(parents=True, exist_ok=True)
    _write(destination / 'run-record.json', record)
    log = io.StringIO()
    started = perf_counter()
    try:
        if _changed(frozen) or _code_files() != code:
            raise ValueError('Input or runtime changed during comparison')
        with ExitStack() as stack, redirect_stdout(log):
            _no_search(stack)
            manifest = net_labels.generate(source, out_dir=destination / 'view', seconds=seconds)
            if manifest['original'] != original:
                raise ValueError('Plugin changed the saved full-route snapshot')
            view = manifest['view']
            if not view.get('validation', {}).get('valid'):
                raise ValueError('Plugin did not provide a completely validated display view')
            artifacts = manifest['artifacts']
            clean, color = Path(artifacts['clean_svg']), Path(artifacts['color_svg'])
            if _wire_paths(clean) != _wire_paths(color):
                raise ValueError('The two display SVGs use different signal geometry')
            replay = net_labels.redraw(artifacts['json'], out_dir=destination / 'redraw')
            if any(Path(replay['artifacts'][kind]).read_bytes() != Path(artifacts[kind]).read_bytes()
                   for kind in ('clean_svg', 'color_svg')):
                raise ValueError('Independent display redraw changed the saved view')
            if _changed(frozen) or _code_files() != code:
                raise ValueError('Protected input or runtime changed')
        metrics = view['display_metrics']
        before = original['metrics']
        labels = view.get('labels', [])
        improved = bool(labels and metrics['wirelength'] < before['wirelength'] - 1e-8
                        and metrics['crossings'] <= before['crossings'])
        if labels and not improved:
            raise ValueError('A labelled view did not meet its length/crossing acceptance rule')
        record.update(status='valid', view_status=view.get('status'),
                      display_metrics=metrics, label_count=len(labels),
                      groups=view.get('groups', []),
                      omitted_paths=view.get('omitted_paths', []),
                      display_validation=view['validation'],
                      diagnostics=view.get('diagnostics', {}), improved=improved,
                      artifacts=artifacts, fingerprint=manifest['fingerprint'],
                      snapshot_unchanged=True, colour_and_clean_routes_identical=True,
                      independent_redraw_identical=True)
    except (ValueError, OSError, KeyError, TypeError, AssertionError) as exc:
        record.update(status='failed', error=str(exc))
    record.update(elapsed_seconds=round(perf_counter() - started, 6),
                  source_sha256_after=_sha(source),
                  finished_utc=datetime.now(timezone.utc).isoformat())
    record['source_unchanged'] = record['source_sha256_before'] == record['source_sha256_after']
    (destination / 'flow.log').write_text(log.getvalue(), encoding='utf-8')
    record['log'] = str(destination / 'flow.log')
    record['run_record'] = str(destination / 'run-record.json')
    _write(destination / 'run-record.json', record)
    return record


def _relative(output, path):
    return escape(os.path.relpath(_resolve(path), output).replace(os.sep, '/'), quote=True)


def _metrics(value):
    return '／'.join(str(value.get(key, '—')) for key in ('wirelength', 'crossings', 'bends'))


def _image(output, path, caption):
    if not path or not Path(path).is_file():
        return '<figure><figcaption>' + escape(caption) + '</figcaption><p>未保存此图。</p></figure>'
    url = _relative(output, path)
    return f'<figure><figcaption>{escape(caption)}</figcaption><a href="{url}"><img src="{url}" loading="lazy"></a></figure>'


def publish(output, cases, records, skips, *, batch_id, code, frozen, seconds, current=None):
    valid = [r for r in records if r['status'] == 'valid']
    report = {'format': FORMAT, 'generated_utc': datetime.now(timezone.utc).isoformat(),
              'batch_id': batch_id, 'state': 'complete' if len(records) == len(cases) else 'running',
              'current': current, 'seconds_per_case': seconds, 'case_count': len(cases),
              'counts': dict(Counter(r['status'] for r in records)),
              'valid_views': len(valid), 'improved_views': sum(r['improved'] for r in valid),
              'skipped_inputs': skips, 'source_files': frozen, 'code_files': code,
              'changed_sources': _changed(frozen), 'records': records,
              'no_layout_astar_recovery_or_simplification': True,
              'electrical_metrics_are_not_replaced_by_display_metrics': True}
    _write(output / 'comparison.json', report)
    _write(output / 'source-list.json', {'cases': cases, 'skipped_inputs': skips,
                                        'source_files': frozen})
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8">',
             '<title>局部连线＋同名网标对照</title>',
             '<style>body{font:15px system-ui;margin:24px;background:#f3f5f8;color:#172335}article{background:white;border-radius:8px;padding:20px;margin:20px 0}section{display:flex;gap:16px}figure{flex:1;min-width:0;margin:0}img{width:100%;height:500px;object-fit:contain;background:white}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd4de;padding:7px;text-align:left}pre{white-space:pre-wrap}a{color:#1556a5}small{color:#526278}@media(max-width:900px){section{display:block}figure{margin-bottom:16px}}</style>',
             '<h1>局部连线＋同名网标：已保存结果对照</h1>',
             '<p>不重新布局、A*、恢复或缩线。保留完整导线原图；网标版只改变连接的表达方式。可见线长下降不是A*算法线长优化。GT仅作视觉参考。</p>',
             f'<p>本批共{len(cases)}份：已完成{len(records)}份，合法展示{len(valid)}份，安全省线{report["improved_views"]}份。当前：{escape(str(current or "无"))}。每份插件预算{seconds:g}秒，计时串行执行。</p>',
             '<p><a href="comparison.json">完整记录</a> · <a href="source-list.json">冻结输入清单</a></p>']
    for record in records:
        case = record['case_info']
        source = Path(record['source'])
        stem = source.name.removesuffix('-placement.json')
        original_svg = source.with_name(stem + '-clean.svg')
        parts += [f'<article id="{escape(case["case"], quote=True)}"><h2>{escape(case["case"])} — {escape(case["title"])}</h2>',
                  f'<p>{escape(record["status"])}，耗时{record["elapsed_seconds"]:.3f}秒；<a href="{_relative(output, record["run_record"])}">执行记录</a> · <a href="{_relative(output, record["log"])}">日志</a></p>']
        if record['status'] != 'valid':
            parts += [f'<pre>{escape(record.get("error", ""))}</pre></article>']
            continue
        artifacts = record['artifacts']
        parts += [f'<p>原完整线路：{_metrics(record["source_metrics"])}；网标版可见线路：{_metrics(record["display_metrics"])}（线长／交叉／折角）。标签{record["label_count"]}个；状态：{escape(str(record["view_status"]))}；实际省线：{record["improved"]}。</p>',
                  f'<p><a href="{_relative(output, source)}">原JSON</a> · <a href="{_relative(output, artifacts["json"])}">网标JSON</a> · <a href="{_relative(output, artifacts["color_svg"])}">彩色网标SVG</a></p><section>']
        if case.get('ground_truth'):
            parts += [_image(output, case['ground_truth'], 'GT（视觉参考）')]
        parts += [_image(output, original_svg, '原完整导线'),
                  _image(output, artifacts['clean_svg'], '局部连线＋同名网标'), '</section>']
        if record['groups']:
            parts += ['<h3>局部组的真实端子</h3><table><tr><th>网络</th><th>局部组</th><th>端子</th></tr>']
            for group in record['groups']:
                terminals = ', '.join('.'.join(map(str, terminal)) if isinstance(terminal, (list, tuple))
                                      else str(terminal) for terminal in group.get('terminals', []))
                parts += [f'<tr><td>{escape(str(group.get("net")))}</td><td>{escape(str(group.get("id", group.get("group_id", ""))))}</td><td>{escape(terminals)}</td></tr>']
            parts += ['</table>']
        parts += [f'<details><summary>停止原因、跳过原因与诊断</summary><pre>{escape(json.dumps(record["diagnostics"], ensure_ascii=False, indent=2))}</pre></details></article>']
    if skips:
        parts += ['<h2>未运行的输入</h2><p>这些输入没有可用的合法完整线路，不进入展示恢复。</p><pre>',
                  escape(json.dumps(skips, ensure_ascii=False, indent=2)), '</pre>']
    parts += ['</html>']
    (output / 'index.html').write_text('\n'.join(parts), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--case', action='append', dest='selected',
                        help='Only execute selected example IDs or dataXX IDs; repeatable')
    parser.add_argument('--seconds', type=float, default=5)
    args = parser.parse_args(argv)
    if not isfinite(args.seconds) or args.seconds <= 0:
        parser.error('--seconds must be positive and finite')
    output = _resolve(args.out_dir)
    cases, skips = discover_sources()
    if args.selected:
        wanted = set(args.selected)
        unknown = wanted - {c['case'] for c in cases}
        if unknown:
            parser.error('No saved supported result for: ' + ', '.join(sorted(unknown)))
        cases = [c for c in cases if c['case'] in wanted]
    if not cases:
        parser.error('No saved complete-route results found')
    output.mkdir(parents=True, exist_ok=True)
    batch_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    frozen, code, records = _source_files(cases), _code_files(), []
    for case in cases:
        publish(output, cases, records, skips, batch_id=batch_id, code=code,
                frozen=frozen, seconds=args.seconds, current=case['case'])
        print('START', case['case'], flush=True)
        row = execute(case, output / 'runs' / batch_id / case['case'], args.seconds, frozen, code)
        records.append(row)
        print('END', case['case'], row['status'], row.get('view_status'),
              row['elapsed_seconds'], flush=True)
        if _changed(frozen) or _code_files() != code:
            break
    report = publish(output, cases, records, skips, batch_id=batch_id, code=code,
                     frozen=frozen, seconds=args.seconds)
    print('REPORT', output / 'index.html', flush=True)
    return int(bool(report['changed_sources'] or any(r['status'] != 'valid' for r in records)
                    or len(records) != len(cases)))


if __name__ == '__main__':
    raise SystemExit(main())
