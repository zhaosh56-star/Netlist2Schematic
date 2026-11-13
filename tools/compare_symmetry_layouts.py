#!/usr/bin/env python3
"""Run isolated, resumable symmetry off/auto comparisons against visual references.

Ground-truth SVGs are visual references only. Successful generated pictures are
independently checked against their original netlists and saved geometry rules.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import escape
from importlib.metadata import version
import json
from math import isfinite
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

from compare_passive_layouts import (DEFAULT_CASES as EXAMPLE_CASES, VERIFY_LOCK,
    _candidate_records, _digest, _provenance, _sha, _source_files, _stop,
    _verify_saved)
from netlist2schematic.netlist.netlist_core import _logical_lines, _partition, parse_netlist
from netlist2schematic.common.sa_common import configure_resources, read_json, resource_root, write_json
from netlist2schematic.common.sa_saved import load_placement, saved_routing_options

FORMAT = 'symmetry-layout-comparison-v1'
POLICIES = ('off', 'auto')
DEFAULT_PARAMETERS = {'seed': 20261029, 'iterations': 20000,
    'quality_seconds': 300, 'rescue_seconds': 60, 'spacing_seconds': 60,
    'fallback_seconds': 60, 'simplify_seconds': 5, 'jobs': 2}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _resolve(path):
    path = Path(path).expanduser()
    return (path if path.is_absolute() else ROOT / path).resolve()


def discover_cases():
    cases = [dict(case, title='图' + case['case'], family='examples')
             for case in EXAMPLE_CASES if case['case'] in tuple(map(str, range(1, 8)))]
    number = 0
    for group in ('1-10', '11-15', '16+'):
        for source in sorted((ROOT / 'test_data' / group).glob('*.cir')):
            number += 1
            truth = source.with_suffix('.svg')
            cases.append({'case': f'data{number:02}', 'title': source.stem,
                'family': 'test_data/' + group, 'netlist': str(source),
                'ground_truth': str(truth) if truth.is_file() else None})
    return cases


def _snapshot():
    from netlist2schematic.common.sa_common import engine_source_files
    configure_resources()
    paths = set(engine_source_files(root=ROOT))
    for folder in ('tools', 'tests'):
        paths.update((ROOT / folder).rglob('*.py'))
    paths.update((ROOT / 'symbol-geometry.json', ROOT / 'requirements.txt'))
    paths.update((resource_root() / 'packages/components/definitions').rglob('*.json'))
    return {str(path.resolve()): _sha(path) for path in sorted(paths)}


def _freeze_inputs(cases):
    """Freeze the whole batch before queueing, including invalid/empty inputs."""
    files = {}
    for case in cases:
        source = _resolve(case['netlist'])
        try:
            files.update(_source_files(source))
        except (ValueError, OSError, UnicodeError):
            if source.is_file():
                files[str(source)] = _sha(source)
        try:
            _, provenance_files = _provenance(case)
            files.update(provenance_files)
        except (ValueError, OSError, KeyError, TypeError):
            for name in case.get('provenance', ()):
                path = _resolve(name)
                if path.is_file():
                    files[str(path)] = _sha(path)
        if case.get('ground_truth'):
            truth = _resolve(case['ground_truth'])
            if truth.is_file():
                files[str(truth)] = _sha(truth)
    return dict(sorted(files.items()))


def _input_changes(files):
    changed = []
    for path, expected in files.items():
        try:
            if _sha(path) != expected:
                changed.append(path)
        except OSError:
            changed.append(path)
    return changed


def _select_cell(source, requested=None):
    """Use a sole declared cell, never guess between multiple subcircuits."""
    if requested is not None:
        cell = requested
    else:
        _, cells = _partition(_logical_lines(source))
        if len(cells) > 1:
            raise ValueError('Multiple subcircuits require an explicit cell in --manifest: '
                             + ', '.join(sorted(cells)))
        cell = next(iter(cells), None)
    if not parse_netlist(source, cell=cell):
        raise ValueError('No supported devices in the selected circuit')
    return cell


def _signature(case, policy, parameters, code):
    source = _resolve(case['netlist'])
    cell = _select_cell(source, case.get('cell'))
    provenance, extra = _provenance(case)
    sources = {**_source_files(source), **extra}
    if case.get('ground_truth'):
        truth = _resolve(case['ground_truth'])
        sources[str(truth)] = _sha(truth)
    return {'format': FORMAT, 'case': case['case'], 'policy': policy,
        'source_netlist': str(source), 'cell': cell,
        'ground_truth': str(_resolve(case['ground_truth'])) if case.get('ground_truth') else None,
        'source_files': dict(sorted(sources.items())), 'provenance': provenance,
        'parameters': dict(parameters), 'code_files': code,
        'runtime': {'python': sys.version, 'executable': sys.executable,
            'networkx': version('networkx'), 'SYM_W_REL': os.environ.get('SYM_W_REL'),
            'ANALOG_CANVAS_ROOT': str(resource_root())}}


def _check_inputs(signature):
    for path, expected in signature['source_files'].items():
        if _sha(path) != expected:
            raise ValueError(f'Original input or visual reference changed: {path}')
    if _snapshot() != signature['code_files']:
        raise ValueError('Project Python code or symbol resources changed during comparison')


def _command(signature, directory):
    p = signature['parameters']
    command = [sys.executable, '-X', 'utf8', '-B', '-u', str(ROOT / 'run_flow.py'),
        signature['source_netlist'], '--out-dir', str(directory),
        '--symmetry-layout', signature['policy'], '--seed', str(p['seed']),
        '--iterations', str(p['iterations']), '--fallback', 'auto', '--local-rescue', 'auto']
    for key in ('quality_seconds', 'rescue_seconds', 'spacing_seconds',
                'fallback_seconds', 'simplify_seconds'):
        command += ['--' + key.replace('_', '-'), str(p[key])]
    if signature['cell']:
        command += ['--cell', signature['cell']]
    return command


def _pair_names(value, index):
    if isinstance(value, dict):
        names = value.get('devices') or value.get('names') or [value.get('left'), value.get('right')]
    else:
        names = value
    if isinstance(names, (list, tuple)) and len(names) == 2:
        return [name for name in names if isinstance(name, str) and name in index]
    return []


def _annotate(clean, destination, pl, index, relations, measurement):
    """Diagnostic overlay only: never changes the validated official SVG."""
    root = ET.parse(clean).getroot()
    ns = '{http://www.w3.org/2000/svg}'
    overlay = ET.SubElement(root, ns + 'g', {'data-diagnostic': 'symmetry-axes',
        'fill': 'none', 'stroke-width': '1.5', 'pointer-events': 'none'})
    x0, y0, width, height = map(float, root.get('viewBox').split())
    palette = ('#a21caf', '#047857', '#c2410c', '#0369a1')
    measured = measurement.get('groups', [])
    for number, group in enumerate(measured):
        if not isinstance(group, dict):
            continue
        color = palette[number % len(palette)]
        axis = group.get('axis_x')
        if isinstance(axis, (int, float)) and isfinite(axis):
            ET.SubElement(overlay, ns + 'line', {'x1': str(axis), 'x2': str(axis),
                'y1': str(y0 + 8), 'y2': str(y0 + height - 8),
                'stroke': color, 'stroke-dasharray': '8 5', 'opacity': '.6'})
        reference = relations[number] if number < len(relations) else {}
        pairs = group.get('pairs') or (reference.get('pairs', []) if isinstance(reference, dict) else [])
        for pair in pairs:
            names = _pair_names(pair, index)
            if len(names) != 2:
                continue
            a, b = (pl.pos_of(index[name]) for name in names)
            ET.SubElement(overlay, ns + 'line', {'x1': str(a[0]), 'y1': str(a[1]),
                'x2': str(b[0]), 'y2': str(b[1]), 'stroke': color,
                'stroke-dasharray': '3 3', 'opacity': '.55'})
            for x, y in (a, b):
                ET.SubElement(overlay, ns + 'circle', {'cx': str(x), 'cy': str(y),
                    'r': '6', 'stroke': color, 'fill': '#fff', 'fill-opacity': '.3'})
    ET.indent(root)
    ET.ElementTree(root).write(destination, encoding='utf-8', xml_declaration=True)


def _symmetry_facts(path, verified):
    from netlist2schematic.plugins.symmetry.symmetry_layout import build_symmetry_groups, measure_symmetry
    with VERIFY_LOCK:
        data, _, _, placement, comps, _, index = load_placement(path)
        groups = build_symmetry_groups(comps, data.get('structs', ()))
        options = saved_routing_options(data)
        measured = measure_symmetry(placement, comps, index, groups, grid_step=options.grid_step)
        annotated = path.with_name(path.name.removesuffix('-placement.json') + '-symmetry-annotated.svg')
        _annotate(verified['clean_svg'], annotated, placement, index, groups, measured)
    report = data.get('layout_symmetry') or {}
    quality = data.get('quality') or {}
    return {'measurement': measured, 'layout_symmetry': report,
        'annotated_svg': str(annotated), 'quality_selection': quality,
        'attempted_candidate_ids': sorted({str(r['candidate_id'])
            for r in _candidate_records(data)
            if 'candidate_id' in r and r.get('status') in ('valid', 'failed')})}


def _verify(path, signature):
    verified = _verify_saved(path, signature)
    verified.update(_symmetry_facts(path, verified))
    return verified


def execute(case, policy, output, parameters, code, *, force=False, rerun_failed=False,
            expected_inputs=None):
    signature = _signature(case, policy, parameters, code)
    if expected_inputs is not None and any(expected_inputs.get(path) != digest
            for path, digest in signature['source_files'].items()):
        raise ValueError('Batch input changed while this case was queued')
    fingerprint = _digest(signature)
    base = output / 'runs' / case['case'] / policy
    base.mkdir(parents=True, exist_ok=True)
    if not force:
        for path in sorted(base.glob('*/run-record.json'), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                record = read_json(path)
                if record.get('fingerprint') != fingerprint or record.get('status') == 'running':
                    continue
                _check_inputs(signature)
                if record.get('status') == 'valid':
                    checked = _verify(Path(record['verified']['placement_json']), signature)
                    record.update(verified=checked, reused=True)
                    print(f"REUSE {case['case']}/{policy}", flush=True)
                    return record
                if record.get('status') == 'failed' and not rerun_failed:
                    record['reused'] = True
                    return record
            except (ValueError, OSError, KeyError, TypeError):
                continue
    directory = base / fingerprint[:16]
    if directory.exists():
        directory = base / (fingerprint[:16] + '-' + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True)
    command = _command(signature, directory)
    record = {'format': FORMAT, 'fingerprint': fingerprint, 'signature': signature,
        'case': case['case'], 'policy': policy, 'command': command,
        'directory': str(directory), 'started_utc': _now(), 'status': 'running', 'reused': False}
    write_json(directory / 'run-record.json', record)
    write_json(base / 'latest.json', {'directory': str(directory), 'fingerprint': fingerprint})
    print(f"START {case['case']}/{policy}: {directory}", flush=True)
    started = perf_counter()
    timeout = sum(parameters[key] for key in ('quality_seconds', 'rescue_seconds',
        'spacing_seconds', 'fallback_seconds', 'simplify_seconds')) + 120
    try:
        with (directory / 'flow.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _stop(process)
                raise ValueError(f'Supervisory timeout after {timeout:g}s')
        record['returncode'] = returncode
        _check_inputs(signature)
        diagnostic = directory / 'routing-diagnostics.json'
        if diagnostic.is_file():
            record['diagnostics'] = read_json(diagnostic)
        if returncode:
            raise ValueError(f'Flow exited with code {returncode}; see flow.log')
        stem = Path(signature['source_netlist']).stem + ('-' + signature['cell'] if signature['cell'] else '')
        record['verified'] = _verify(directory / (stem + '-placement.json'), signature)
        _check_inputs(signature)
        record['status'] = 'valid'
    except (ValueError, OSError, KeyError, TypeError, ImportError) as exc:
        record.update(status='failed', error=f'{type(exc).__name__}: {exc}')
    record.update(elapsed_seconds=round(perf_counter() - started, 6), finished_utc=_now())
    write_json(directory / 'run-record.json', record)
    print(f"END {case['case']}/{policy}: {record['status']}; {record['elapsed_seconds']:.2f}s", flush=True)
    return record


def _relative(output, path):
    return Path(os.path.relpath(path, output)).as_posix()


def _e(value):
    return escape(str(value), quote=True)


def _table(headers, rows):
    return ('<div class="scroll"><table><thead><tr>'
        + ''.join(f'<th>{_e(x)}</th>' for x in headers) + '</tr></thead><tbody>'
        + ''.join('<tr>' + ''.join(f'<td>{_e(v)}</td>' for v in row) + '</tr>' for row in rows)
        + '</tbody></table></div>')


def _figure(output, title, image, extra=''):
    link = _e(_relative(output, image))
    return f'<figure><figcaption>{_e(title)}</figcaption><a href="{link}"><img src="{link}" loading="lazy"></a>{extra}</figure>'


def publish(output, cases, records, code, parameters, frozen_inputs=None):
    rows, sections = [], []
    for case in cases:
        results = {policy: records[(case['case'], policy)] for policy in POLICIES
                   if (case['case'], policy) in records}
        row = {**case, 'results': results}
        if len(results) == 2 and all(r['status'] == 'valid' for r in results.values()):
            row['same_input_version'] = (results['off'].get('signature', {}).get('source_files')
                                        == results['auto'].get('signature', {}).get('source_files'))
        if row.get('same_input_version'):
            old, new = (results[p]['verified'] for p in POLICIES)
            row['delta'] = {key: new['metrics'][key] - old['metrics'][key]
                            for key in ('wirelength', 'crossings', 'bends')}
            row['delta']['quality'] = new['quality']['score'] - old['quality']['score']
            for key in ('failed_relations', 'residual_bucket', 'residual'):
                a, b = old['measurement'].get(key), new['measurement'].get(key)
                if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                    row['delta'][key] = b - a
        rows.append(row)
        figures, metrics, reports = [], [], {}
        if case.get('ground_truth'):
            figures.append(_figure(output, 'Ground truth：仅作视觉参考', _resolve(case['ground_truth'])))
        else:
            figures.append('<figure><figcaption>Ground truth</figcaption><p>本例没有配套参考图</p></figure>')
        for policy in POLICIES:
            record = results.get(policy)
            if record is None:
                figures.append(f'<figure><figcaption>{policy}</figcaption><p>等待运行</p></figure>')
                continue
            if record['status'] != 'valid':
                log = (f'<a href="{_e(_relative(output, Path(record["directory"]) / "flow.log"))}">日志</a>'
                       if record.get('directory') else '')
                figures.append(f'<figure><figcaption>{policy}：{_e(record["status"])}</figcaption>'
                               f'<p>{_e(record.get("error", ""))}</p>{log}</figure>')
                continue
            value, report = record['verified'], record['verified']['layout_symmetry']
            m, s = value['metrics'], value['measurement']
            links = '<p>' + ' · '.join(f'<a href="{_e(_relative(output, value[k]))}">{label}</a>'
                for k, label in (('placement_json', 'JSON'), ('color_svg', '彩色'),
                                 ('annotated_svg', '诊断叠加图'))) + '</p>'
            figures.append(_figure(output, f'{policy} · {value["routing_stage"]} · 候选{value["selected_candidate"]}', value['clean_svg'], links))
            metrics.append((policy, m['wirelength'], m['crossings'], m['bends'],
                round(value['quality']['score'], 3),
                f'{value["width"]:g}×{value["height"]:g}', s.get('relation_count'),
                s.get('failed_relations'), s.get('residual_bucket'), s.get('residual'),
                ','.join(value['attempted_candidate_ids']), record.get('elapsed_seconds'),
                report.get('status', report.get('reason', report.get('applied', '详见记录')))))
            reports[policy] = {'layout_symmetry': report, 'quality_selection': value['quality_selection']}
        title = case.get('title', case['case'])
        sections.append(f'<section id="{_e(case["case"])}"><h2>{_e(case["case"])} · {_e(title)}</h2>'
            f'<p><code>{_e(case["netlist"])}</code></p>'
            + _table(('模式', '线长', '交叉', '折角', '真实Q', '画布', '对称关系数',
                '未满足关系', '残差桶', '残差', '实际布线候选', '总秒数', '对称实际状态'), metrics)
            + '<div class="figures">' + ''.join(figures) + '</div>'
            + '<details><summary>对称执行与25%参考候选筛选的原始记录</summary><pre>'
            + _e(json.dumps(reports, ensure_ascii=False, indent=2)) + '</pre></details></section>')
    report = {'format': FORMAT, 'generated_utc': _now(), 'parameters': parameters,
        'uses_default_parameters': all(parameters.get(k) == v for k, v in DEFAULT_PARAMETERS.items()),
        'uses_default_budgets': all(parameters.get(k) == DEFAULT_PARAMETERS[k] for k in
            ('quality_seconds', 'rescue_seconds', 'spacing_seconds', 'fallback_seconds', 'simplify_seconds')),
        'code_files': code, 'expected_runs': len(cases) * 2, 'completed_runs': len(records),
        'valid_runs': sum(r['status'] == 'valid' for r in records.values()),
        'skipped_runs': sum(r['status'] == 'skipped' for r in records.values()),
        'input_files': frozen_inputs or {}, 'input_changes': _input_changes(frozen_inputs or {}),
        'ground_truth_electrically_verified': False, 'cases': rows}
    write_json(output / 'comparison.json', report)
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>对称布局 off / auto 对照</title><style>body{font:14px/1.6 system-ui;margin:24px auto;padding:0 18px;max-width:1800px;background:#f4f6f8;color:#18212b}section{background:white;border:1px solid #d7dee6;padding:18px;margin:22px 0;border-radius:8px}.figures{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}figure{margin:10px 0}figcaption{font-weight:600}img{width:100%;height:480px;object-fit:contain;border:1px solid #ddd}.scroll{overflow:auto}table{border-collapse:collapse;white-space:nowrap;font-size:12px}th,td{padding:6px 9px;border-bottom:1px solid #ddd;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#1d4ed8}code{overflow-wrap:anywhere}@media(max-width:1050px){.figures{grid-template-columns:1fr}img{height:560px}}</style>
<h1>Ground truth / symmetry off / symmetry auto</h1>
<p>两种模式都从同一网表重新运行完整flow。成功图独立复验原引脚、固定出线、连通与几何，核对两种SVG线路和独立重绘。参考SVG仅用于视觉分析，没有声明它们通过当前电气或几何验收。</p>
<p>诊断叠加图用虚线显示局部轴，用细虚线连接配对器件原点；它不是正式输出。对称残差使用算法定义的真实引脚主干几何计算。无对称关系或未实际执行时如实记录，不把auto开关等同于一定改变。</p>
<p>默认预算为300+60+60+60+5秒，实际本次参数如下；缩短预算结果不能当作默认预算结论。完整签名、来源、实际候选及失败原因见<a href="comparison.json">comparison.json</a>。</p>
<pre>PARAMETERS</pre>SECTIONS</html>'''
    html = html.replace('PARAMETERS', _e(json.dumps(parameters, ensure_ascii=False, indent=2)))
    (output / 'index.html').write_text(html.replace('SECTIONS', '\n'.join(sections)), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', type=Path, default=ROOT / 'output/symmetry-comparison')
    parser.add_argument('--manifest', type=Path, help='Explicit case/netlist/cell/ground_truth/provenance objects')
    parser.add_argument('--cases', nargs='+', help='Case IDs (1..7, data01..data25) or exact dataset stems')
    parser.add_argument('--list-cases', action='store_true')
    parser.add_argument('--jobs', type=int, choices=(1, 2, 3, 4, 5, 6), default=2)
    for name in ('seed', 'iterations'):
        parser.add_argument('--' + name, type=int, default=DEFAULT_PARAMETERS[name])
    for name in ('quality_seconds', 'rescue_seconds', 'spacing_seconds', 'fallback_seconds', 'simplify_seconds'):
        parser.add_argument('--' + name.replace('_', '-'), type=float, default=DEFAULT_PARAMETERS[name])
    parser.add_argument('--force', action='store_true', help='Create fresh attempt directories; preserve previous output')
    parser.add_argument('--rerun-failed', action='store_true')
    args = parser.parse_args(argv)
    parameters = {name: getattr(args, name) for name in DEFAULT_PARAMETERS}
    if args.iterations < 0 or not isfinite(args.quality_seconds) or args.quality_seconds <= 0:
        parser.error('iterations must be non-negative; quality-seconds must be positive and finite')
    if any(not isfinite(parameters[k]) or parameters[k] < 0 for k in
           ('rescue_seconds', 'spacing_seconds', 'fallback_seconds', 'simplify_seconds')):
        parser.error('recovery and simplification budgets must be non-negative and finite')
    cases = read_json(args.manifest) if args.manifest else discover_cases()
    if not isinstance(cases, list) or any(not isinstance(c, dict) for c in cases):
        parser.error('manifest must be a list of case objects')
    identifiers = [str(c.get('case', '')) for c in cases]
    if len(set(identifiers)) != len(cases) or any(not name or any(
            x not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for x in name)
            for name in identifiers):
        parser.error('case identifiers must be unique, nonempty and filename safe')
    cases = [{**case, 'case': str(case['case'])} for case in cases]
    if args.list_cases:
        for case in cases:
            print(f'{case["case"]}\t{case["netlist"]}', flush=True)
        return 0
    if args.cases:
        lookup = {alias: case['case'] for case in cases
                  for alias in (case['case'], Path(case['netlist']).stem)}
        missing = set(args.cases) - set(lookup)
        if missing:
            parser.error('unknown cases: ' + ', '.join(sorted(missing)))
        selected = {lookup[name] for name in args.cases}
        cases = [case for case in cases if case['case'] in selected]
    output = args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    code, records, frozen_inputs = _snapshot(), {}, _freeze_inputs(cases)
    publish(output, cases, records, code, parameters, frozen_inputs)
    with ThreadPoolExecutor(max_workers=args.jobs) as workers:
        futures = {workers.submit(execute, case, policy, output, parameters, code,
            force=args.force, rerun_failed=args.rerun_failed,
            expected_inputs=frozen_inputs): (case, policy)
            for case in cases for policy in POLICIES}
        for future in as_completed(futures):
            case, policy = futures[future]
            try:
                record = future.result()
            except (ValueError, OSError, KeyError, TypeError) as exc:
                # Bad inputs never enter layout/recovery, and remain distinct
                # from routing attempts that exhausted their actual budgets.
                record = {'case': case['case'], 'policy': policy, 'status': 'skipped',
                    'reason': 'input_validation_error', 'error': f'{type(exc).__name__}: {exc}'}
                print(f'SKIP {case["case"]}/{policy}: {record["error"]}', flush=True)
            records[(case['case'], policy)] = record
            publish(output, cases, records, code, parameters, frozen_inputs)
    report = publish(output, cases, records, code, parameters, frozen_inputs)
    print(f'Complete: {report["valid_runs"]} valid, {report["skipped_runs"]} skipped, '
          f'{report["completed_runs"]}/{report["expected_runs"]} completed; {output / "index.html"}', flush=True)
    return int(bool(report['input_changes']) or any(r['status'] == 'failed' for r in records.values()))


if __name__ == '__main__':
    raise SystemExit(main())
