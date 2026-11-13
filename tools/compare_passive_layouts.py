#!/usr/bin/env python3
"""Run and independently verify fixed/flexible passive layout comparisons.

Every run owns an isolated fingerprinted directory. Existing output/1..7 images
and input files are never changed, and interrupted comparisons can be resumed.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
from html import escape
from importlib.metadata import version
import json
from math import isfinite
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from threading import Lock
from time import perf_counter
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.layout.layout_quality import evaluate_quality
from netlist2schematic.netlist.netlist_core import parse_netlist
from netlist2schematic.routing.routing_rules import build_scene
from netlist2schematic.common.sa_common import (configure_resources, engine_source_files,
                                               load_local, read_json, resource_root, write_json)
from netlist2schematic.routing.sa_routing import validate_final_routes
from netlist2schematic.common.sa_saved import load_placement, saved_routing_options
from netlist2schematic.postprocess.simplify_routes import _covers_fixed

FORMAT = 'passive-layout-comparison-v1'
VERIFY_LOCK = Lock()
DEFAULT_CASES = [
    *[{'case': str(i), 'netlist': f'examples/{i}.cir'} for i in range(1, 5)],
    {'case': '5', 'netlist': 'examples/5.cir', 'cell': 'dut'},
    {'case': '6', 'netlist': 'output/6/input/6.cir',
     'provenance': ['output/6/input-normalization.json']},
    {'case': '7', 'netlist': 'output/7/input/7.cir',
     'provenance': ['output/7/input-models.json']},
    {'case': 'mixed-devices', 'netlist': 'output/mixed-devices/input/mixed-devices.cir',
     'provenance': ['output/mixed-devices/input-models.json']},
]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def _resolve(path):
    value = Path(path).expanduser()
    return (value if value.is_absolute() else ROOT / value).resolve()


def _source_files(source):
    """Include empty model files too, rather than only expanded source lines."""
    found = {}
    pattern = re.compile(r'''^\.(?:include|inc)\s+(?:"([^"]+)"|'([^']+)'|(\S+))\s*$''', re.I)

    def visit(path, ancestors=()):
        path = path.resolve()
        if path in ancestors or len(ancestors) >= 32:
            raise ValueError(f'Recursive or excessively deep include: {path}')
        if str(path) in found:
            return
        found[str(path)] = _sha(path)
        logical = []
        for raw in path.read_text(encoding='utf-8-sig').splitlines():
            text = raw.strip()
            if not text or text.startswith('*'):
                continue
            text = re.split(r'[$;]', text, maxsplit=1)[0].strip()
            if text.startswith('+') and logical:
                logical[-1] += ' ' + text[1:].strip()
            elif text:
                logical.append(text)
        for text in logical:
            match = pattern.match(text)
            if match:
                target = Path(next(v for v in match.groups() if v is not None))
                visit(target if target.is_absolute() else path.parent / target,
                      (*ancestors, path))

    visit(source)
    return dict(sorted(found.items()))


def _provenance(case):
    facts, files = [], {}
    for name in case.get('provenance', []):
        path = _resolve(name)
        data = read_json(path)
        files[str(path)] = _sha(path)
        for path_key, hash_key in (
                ('original_netlist', 'original_sha256'),
                ('source_netlist', 'source_sha256'),
                ('run_netlist', 'run_sha256'), ('model_file', 'model_file_sha256')):
            if data.get(path_key):
                target = Path(data[path_key]).expanduser()
                target = target if target.is_absolute() else path.parent / target
                target = target.resolve()
                actual = _sha(target)
                if data.get(hash_key) is not None and actual != data[hash_key]:
                    raise ValueError(f'Provenance hash mismatch: {target}')
                files[str(target)] = actual
        facts.append({'file': str(path), 'sha256': files[str(path)], 'record': data})
    return facts, files


def _code_snapshot():
    configure_resources()
    paths = engine_source_files(root=ROOT) + [ROOT / 'symbol-geometry.json',
            ROOT / 'requirements.txt', Path(__file__).resolve()]
    paths += list((resource_root() / 'packages/components/definitions').glob('*.json'))
    return {str(path.resolve()): _sha(path) for path in sorted(set(paths))}


def _signature(case, policy, parameters, code, candidate_id=None):
    source = _resolve(case['netlist'])
    # Parse before starting an expensive process; unsupported input is not a
    # routing failure and must not be transformed into a fallback picture.
    parse_netlist(source, cell=case.get('cell'))
    facts, extra_files = _provenance(case)
    files = {**_source_files(source), **extra_files}
    return {'format': FORMAT, 'case': case['case'], 'policy': policy,
            'source_netlist': str(source), 'cell': case.get('cell'),
            'source_files': dict(sorted(files.items())), 'provenance': facts,
            'parameters': {**parameters, 'candidate_id': candidate_id},
            'code_files': code,
            'runtime': {'python': sys.version, 'executable': sys.executable,
                        'networkx': version('networkx'),
                        'SYM_W_REL': os.environ.get('SYM_W_REL'),
                        'ANALOG_CANVAS_ROOT': str(resource_root())}}


def _check_inputs(signature):
    for path, expected in signature['source_files'].items():
        if _sha(path) != expected:
            raise ValueError(f'Input changed during comparison: {path}')
    if _code_snapshot() != signature['code_files']:
        raise ValueError('Flow code or symbol resources changed during comparison')


def _wire_paths(path):
    root = ET.parse(path).getroot()
    return root, [(node.get('data-net'), node.get('points'))
                  for node in root.iter() if node.get('data-net')]


def _candidate_records(value):
    """Recovery may move original attempts into prior-stage diagnostics."""
    rows = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == 'candidates' and isinstance(child, list):
                rows.extend(row for row in child if isinstance(row, dict))
            rows.extend(_candidate_records(child))
    elif isinstance(value, list):
        for child in value:
            rows.extend(_candidate_records(child))
    return rows


def _verify_saved(path, signature):
    with VERIFY_LOCK:
        data, source, par, pl, comps, nets, index = load_placement(path)
        if source != Path(signature['source_netlist']) or data.get('source_cell') != signature['cell']:
            raise ValueError('Published placement refers to a different input or cell')
        options = saved_routing_options(data)
        routed, metrics, checked = validate_final_routes(
            par, nets, pl, index, list(data['routes'].items()), options=options)
        if not checked['valid'] or metrics != data['metrics']:
            raise ValueError('Saved metrics do not match independently verified routes')
        scene = build_scene(par, nets, pl, index, options=options)
        if not all(_covers_fixed(scene, net, runs) for net, runs in routed):
            raise ValueError('A fixed original-pin escape is missing')
        stem = path.name.removesuffix('-placement.json')
        clean = path.with_name(stem + '-clean.svg')
        color = path.with_name(stem + '-layered.svg')
        clean_root, clean_paths = _wire_paths(clean)
        _, color_paths = _wire_paths(color)
        if clean_paths != color_paths:
            raise ValueError('Color and clean SVG use different signal wires')
        renderer = load_local('passive_comparison_redraw', 'render-clean.py')
        with tempfile.TemporaryDirectory(prefix='passive-comparison-redraw-') as tmp:
            redraw = Path(tmp) / 'redraw.svg'
            renderer.render(path, redraw)
            if redraw.read_bytes() != clean.read_bytes():
                raise ValueError('Independent redraw differs from published clean SVG')
        devices = parse_netlist(source, cell=data.get('source_cell'))
        levels = compute_levels(devices)['device_levels']
        if data.get('levels') != levels:
            raise ValueError('Saved active device levels do not match the source topology')
        active = {name: pl.cell_of[index[name]][0] for name in levels}
        for first, first_level in levels.items():
            for second, second_level in levels.items():
                if first_level < second_level and active[first] >= active[second]:
                    raise ValueError('Active conduction layer order changed')
        if any(pl.orient[index[name]] not in (0, 1) for name in levels):
            raise ValueError('An active device has an unsupported orientation')
        structures = analyze_structures(devices)
        quality = evaluate_quality(par, pl, comps, nets, index, routed, metrics, structures)
        points = [pl.pos_of(i) for i in range(len(comps))]
        regularity = data.get('optimization_metrics', {}).get('passive_regularization')
        attempts = _candidate_records(data)
        attempted_new = sorted({str(row['candidate_id']) for row in attempts
                                if str(row.get('candidate_id')) in ('1', '2', '3')
                                and row.get('status') in ('valid', 'failed')})
        attempted_flexible = any(row.get('passive_layout_policy') == 'flexible'
                                 for row in attempts)
        return {'valid': True, 'placement_json': str(path), 'clean_svg': str(clean),
                'color_svg': str(color), 'metrics': metrics, 'quality': quality,
                'width': float(clean_root.get('width')), 'height': float(clean_root.get('height')),
                'height_to_width': float(clean_root.get('height')) / float(clean_root.get('width')),
                'component_center_height': max(y for x, y in points) - min(y for x, y in points),
                'occupied_rows': sorted({row for row, col in pl.cell_of}),
                'active_rows': active, 'component_count': len(comps),
                'selected_candidate': data.get('quality', {}).get('selected_candidate', data.get('candidate_id')),
                'routing_stage': data.get('routing_stage') or 'normal',
                'requested_passive_layout': data.get('requested_passive_layout'),
                'passive_layout_policy': data.get('passive_layout_policy', 'fixed'),
                'passive_layout': data.get('passive_layout'),
                'passive_regularization': regularity,
                'attempted_new_candidates': attempted_new,
                'attempted_flexible_candidate': attempted_flexible,
                'quality_seconds': data.get('quality', {}).get('elapsed_seconds'),
                'routing_seconds': data.get('validation', {}).get('routing_seconds'),
                'simplification_seconds': data.get('route_simplification', {}).get('elapsed_seconds'),
                'recovery_stages': data.get('recovery', {}).get('stages', []),
                'all_original_pins_connected': True, 'all_fixed_stubs_preserved': True,
                'color_and_clean_routes_identical': True, 'independent_redraw_identical': True}


def _command(signature, directory):
    params = signature['parameters']
    command = [sys.executable, '-X', 'utf8', '-B', '-u', str(ROOT / 'run_flow.py'),
               signature['source_netlist'], '--out-dir', str(directory),
               '--passive-layout', signature['policy'], '--seed', str(params['seed']),
               '--iterations', str(params['iterations']),
               '--quality-seconds', str(params['quality_seconds']),
               '--rescue-seconds', str(params['rescue_seconds']),
               '--spacing-seconds', str(params['spacing_seconds']),
               '--fallback-seconds', str(params['fallback_seconds']),
               '--simplify-seconds', str(params['simplify_seconds'])]
    if signature['cell']:
        command += ['--cell', signature['cell']]
    if params['candidate_id'] is not None:
        command += ['--candidate-id', str(params['candidate_id']),
                    '--fallback', 'off', '--local-rescue', 'auto']
    else:
        command += ['--fallback', 'auto', '--local-rescue', 'auto']
    return command


def _stop(process):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        process.kill()
    process.wait()


def execute(case, policy, output, parameters, code, *, force=False,
            rerun_failed=False, candidate_id=None):
    signature = _signature(case, policy, parameters, code, candidate_id)
    fingerprint = _digest(signature)
    group = 'replay' if candidate_id is not None else 'default'
    base = output / group / case['case'] / policy
    base.mkdir(parents=True, exist_ok=True)
    if not force:
        prior = sorted(base.glob('*/run-record.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in prior:
            try:
                record = read_json(path)
                if record.get('fingerprint') != fingerprint or record.get('status') == 'running':
                    continue
                _check_inputs(signature)
                if record.get('status') == 'valid':
                    verified = _verify_saved(Path(record['verified']['placement_json']), signature)
                    record.update(verified=verified, reused=True)
                    print(f"REUSE {group}/{case['case']}/{policy}", flush=True)
                    return record
                if not rerun_failed:
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
              'case': case['case'], 'policy': policy, 'group': group,
              'candidate_id': candidate_id, 'command': command, 'directory': str(directory),
              'started_utc': _now(), 'status': 'running', 'reused': False}
    write_json(directory / 'run-record.json', record)
    write_json(base / 'latest.json', {'directory': str(directory), 'fingerprint': fingerprint})
    print(f"START {group}/{case['case']}/{policy}: {directory}", flush=True)
    start = perf_counter()
    timeout = sum(parameters[k] for k in ('quality_seconds', 'rescue_seconds',
                  'spacing_seconds', 'fallback_seconds', 'simplify_seconds')) + 120
    try:
        with (directory / 'flow.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            try:
                code_result = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _stop(process)
                raise ValueError(f'Supervisory process timeout after {timeout:g}s')
        record['returncode'] = code_result
        _check_inputs(signature)
        if code_result:
            diagnostic = directory / 'routing-diagnostics.json'
            record['diagnostics'] = read_json(diagnostic) if diagnostic.is_file() else None
            raise ValueError(f'Flow exited with code {code_result}; see flow.log')
        source = Path(signature['source_netlist'])
        stem = source.stem + ('-' + signature['cell'] if signature['cell'] else '')
        record['verified'] = _verify_saved(directory / (stem + '-placement.json'), signature)
        record['status'] = 'valid'
    except (ValueError, OSError, KeyError, TypeError) as exc:
        record.update(status='failed', error=str(exc))
    record.update(elapsed_seconds=round(perf_counter() - start, 6), finished_utc=_now())
    write_json(directory / 'run-record.json', record)
    print(f"END {group}/{case['case']}/{policy}: {record['status']}; {record['elapsed_seconds']:.2f}s", flush=True)
    return record


def _relative(output, path):
    return Path(path).relative_to(output).as_posix()


def _table(headers, rows):
    e = lambda value: escape(str(value), quote=True)
    return ('<div class="scroll"><table><thead><tr>' + ''.join(f'<th>{e(x)}</th>' for x in headers)
            + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(f'<td>{e(v)}</td>' for v in row)
            + '</tr>' for row in rows) + '</tbody></table></div>')


def _pair_html(case, output, key='results'):
    e = lambda value: escape(str(value), quote=True)
    results = case.get(key, {})
    figures, metadata = [], []
    for policy in ('fixed', 'flexible'):
        record = results.get(policy)
        if record is None:
            figures.append(f'<figure><figcaption>{policy}</figcaption><p>尚未运行</p></figure>')
            continue
        if record.get('status') != 'valid':
            log = _relative(output, Path(record['directory']) / 'flow.log')
            figures.append(f'<figure><figcaption>{policy}：失败</figcaption>'
                           f'<p>{e(record.get("error"))}</p><a href="{e(log)}">日志</a></figure>')
            continue
        value = record['verified']
        image = _relative(output, value['clean_svg'])
        placement = _relative(output, value['placement_json'])
        log = _relative(output, Path(record['directory']) / 'flow.log')
        figures.append(f'<figure><figcaption>{policy} · {e(value["routing_stage"])} · '
                       f'候选 {e(value["selected_candidate"])}</figcaption><a href="{e(image)}">'
                       f'<img src="{e(image)}" loading="lazy" alt="{policy}原理图"></a>'
                       f'<p><a href="{e(placement)}">JSON</a> · <a href="{e(log)}">日志</a></p></figure>')
        metadata.append((policy, value['metrics']['wirelength'], value['metrics']['crossings'],
            value['metrics']['bends'], round(value['quality']['score'], 3),
            f"{value['width']:.0f} × {value['height']:.0f}",
            value['component_center_height'], value['passive_layout_policy'],
            ','.join(value['attempted_new_candidates']) or '未进入新候选', record['elapsed_seconds']))
    diagnostics = {policy: record['verified'].get('passive_layout') for policy, record in results.items()
                   if record.get('status') == 'valid'}
    soft = {policy: record['verified'].get('passive_regularization') for policy, record in results.items()
            if record.get('status') == 'valid'}
    details = ('<details><summary>无源动作和软评分记录</summary><p>move_counts 是包含温度采样的合法提案次数，'
               '不是最终接受的移动次数。</p><pre>'
               + e(json.dumps({'passive_layout': diagnostics, 'passive_regularization': soft},
                              ensure_ascii=False, indent=2)) + '</pre></details>')
    return (_table(('策略', '线长', '交叉', '折角', '真实Q', '画布宽×高', '器件中心高度',
                    '选中策略', '进入布线的新候选', '本次总秒数'), metadata)
            + '<div class="figures">' + ''.join(figures) + '</div>' + details)


def publish(output, cases, records, code, parameters):
    rows = []
    for spec in cases:
        identifier = spec['case']
        row = {'case': identifier, 'netlist': str(_resolve(spec['netlist'])),
               'cell': spec.get('cell'),
               'results': {policy: records[(identifier, policy, None)]
                           for policy in ('fixed', 'flexible')
                           if (identifier, policy, None) in records}}
        replay = {policy: records[(identifier, policy, 1)] for policy in ('fixed', 'flexible')
                  if (identifier, policy, 1) in records}
        if replay:
            row['capability_replay'] = replay
        if len(row['results']) == 2 and all(r['status'] == 'valid' for r in row['results'].values()):
            old, new = (row['results'][p]['verified'] for p in ('fixed', 'flexible'))
            row['delta'] = {key: new['metrics'][key] - old['metrics'][key]
                            for key in ('wirelength', 'crossings', 'bends')}
            row['delta']['quality'] = new['quality']['score'] - old['quality']['score']
            row['delta']['height'] = new['height'] - old['height']
        rows.append(row)
    report = {'format': FORMAT, 'generated_utc': _now(), 'parameters': parameters,
              'code_files': code, 'completed_runs': len(records),
              'expected_default_runs': 2 * len(cases),
              'valid_runs': sum(r['status'] == 'valid' for r in records.values()),
              'all_default_runs_completed': all(len(r['results']) == 2 for r in rows),
              'cases': rows}
    write_json(output / 'comparison.json', report)
    sections = []
    for case in rows:
        title = '混合器件' if case['case'] == 'mixed-devices' else '图' + case['case']
        body = f'<section id="case-{escape(case["case"])}"><h2>{escape(title)}</h2>'
        body += '<p>两侧使用相同源文件、种子、迭代数和预算，结果均来自本目录独立完整flow。</p>'
        body += _pair_html(case, output)
        if case.get('capability_replay'):
            body += ('<details open><summary>额外候选1回放：能力验证，不是默认flow结果</summary>'
                     '<p>默认flexible运行未让新候选实际进入布线，另给候选1完整预算进行对照；'
                     '只允许固定候选局部救援，不进入扩距或总线重排。失败会如实保留。</p>'
                     + _pair_html(case, output, 'capability_replay') + '</details>')
        sections.append(body + '</section>')
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>无源跨行布局对照</title>
<style>body{font:15px/1.65 system-ui,sans-serif;margin:28px auto;padding:0 20px;max-width:1550px;color:#18212b;background:#f4f6f8}section{background:white;padding:22px;margin:22px 0;border:1px solid #d9dfe6;border-radius:10px}table{border-collapse:collapse;width:100%;white-space:nowrap;font-size:13px}th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #d9dfe6}.scroll{overflow:auto}.figures{display:grid;grid-template-columns:1fr 1fr;gap:16px}figure{margin:12px 0}figcaption{font-weight:600;margin-bottom:10px}img{display:block;width:100%;height:560px;object-fit:contain;border:1px solid #e5e7eb;background:white}a{color:#1d4ed8}code{overflow-wrap:anywhere}summary{cursor:pointer}details{margin:20px 0}@media(max-width:850px){.figures{grid-template-columns:1fr}img{height:460px}}</style>
<h1>无源器件 fixed / flexible 独立完整flow对照</h1>
<p>各成功结果均重新检查原引脚、几何规则、固定出线，确认两种SVG线路一致且独立重绘与黑白图字节一致。没有用历史主图代替失败结果，没有覆盖 output/1～7。</p>
<p>候选0仍为旧参考；新候选1～3才可能采用无源二维动作。时间预算或候选胜者可能导致默认结果仍为fixed，界面会明确显示。允许某些指标上升，不把“运行成功”当作“所有图均更美观”。</p>
<p>完整来源、源码/符号及include哈希、软评分、运行命令、失败诊断见 <a href="comparison.json">comparison.json</a>。点击图片单独放大。</p>
SECTIONS</html>'''
    (output / 'index.html').write_text(html.replace('SECTIONS', '\n'.join(sections)), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', type=Path, default=ROOT / 'output/passive-flex-comparison')
    parser.add_argument('--manifest', type=Path, help='JSON list of case/netlist/cell/provenance objects')
    parser.add_argument('--cases', nargs='+', help='Selected case identifiers; default all eight')
    parser.add_argument('--jobs', type=int, choices=(1, 2), default=1)
    parser.add_argument('--seed', type=int, default=20261029)
    parser.add_argument('--iterations', type=int, default=20000)
    parser.add_argument('--quality-seconds', type=float, default=300)
    parser.add_argument('--rescue-seconds', type=float, default=60)
    parser.add_argument('--spacing-seconds', type=float, default=60)
    parser.add_argument('--fallback-seconds', type=float, default=60)
    parser.add_argument('--simplify-seconds', type=float, default=5)
    parser.add_argument('--force', action='store_true', help='Keep prior runs, create fresh attempt directories')
    parser.add_argument('--rerun-failed', action='store_true')
    parser.add_argument('--no-replay', action='store_true', help='Skip supplementary candidate1 capability checks')
    args = parser.parse_args(argv)
    if args.iterations < 0 or not isfinite(args.quality_seconds) or args.quality_seconds <= 0:
        parser.error('iterations must be non-negative and quality-seconds positive and finite')
    if any(not isfinite(getattr(args, key)) or getattr(args, key) < 0 for key in
           ('rescue_seconds', 'spacing_seconds', 'fallback_seconds', 'simplify_seconds')):
        parser.error('recovery and simplification budgets must be non-negative and finite')
    cases = read_json(args.manifest) if args.manifest else DEFAULT_CASES
    if not isinstance(cases, list) or any(not isinstance(c, dict) for c in cases):
        parser.error('manifest must be a list of case objects')
    cases = [{**case, 'case': str(case['case'])} for case in cases]
    if len({c['case'] for c in cases}) != len(cases) or any(
            not re.fullmatch(r'[A-Za-z0-9_-]+', c['case']) for c in cases):
        parser.error('case identifiers must be unique filename-safe labels')
    if args.cases:
        missing = set(args.cases) - {c['case'] for c in cases}
        if missing:
            parser.error('unknown cases: ' + ', '.join(sorted(missing)))
        cases = [c for c in cases if c['case'] in args.cases]
    output = args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    parameters = {name: getattr(args, name) for name in ('seed', 'iterations', 'quality_seconds',
                   'rescue_seconds', 'spacing_seconds', 'fallback_seconds', 'simplify_seconds', 'jobs')}
    code = _code_snapshot()
    records = {}
    publish(output, cases, records, code, parameters)
    with ThreadPoolExecutor(max_workers=args.jobs) as workers:
        futures = {workers.submit(execute, case, policy, output, parameters, code,
                                  force=args.force, rerun_failed=args.rerun_failed):
                   (case['case'], policy, None)
                   for case in cases for policy in ('fixed', 'flexible')}
        for future in as_completed(futures):
            key = futures[future]
            try:
                records[key] = future.result()
            except (ValueError, OSError, KeyError, TypeError) as exc:
                # Setup failures have no successful artifact; preserve an honest
                # failed record with its own log so the report remains usable.
                directory = output / 'setup-errors' / (key[0] + '-' + key[1])
                directory.mkdir(parents=True, exist_ok=True)
                (directory / 'flow.log').write_text(str(exc) + '\n', encoding='utf-8')
                records[key] = {'case': key[0], 'policy': key[1], 'status': 'failed',
                                'directory': str(directory), 'error': str(exc)}
            publish(output, cases, records, code, parameters)
        if not args.no_replay:
            extra = []
            for case in cases:
                normal = records[(case['case'], 'flexible', None)]
                value = normal.get('verified', {})
                attempts = _candidate_records(normal.get('diagnostics', {}))
                attempted = value.get('attempted_new_candidates') or [
                    row['candidate_id'] for row in attempts
                    if str(row.get('candidate_id')) in ('1', '2', '3')
                    and row.get('status') in ('valid', 'failed')]
                # A valid fallback may have no normal candidate beyond zero.
                # A completely failed run is equally eligible when its source
                # parsed correctly and diagnostics prove normal routing ran.
                eligible = normal.get('status') == 'valid' or bool(attempts)
                if eligible and not attempted:
                    devices = parse_netlist(_resolve(case['netlist']), cell=case.get('cell'))
                    if any(d['cls'] == 'PASSIVE' for d in devices.values()):
                        extra.append(case)
            futures = {workers.submit(execute, case, policy, output, parameters, code,
                                      force=args.force, rerun_failed=args.rerun_failed, candidate_id=1):
                       (case['case'], policy, 1) for case in extra for policy in ('fixed', 'flexible')}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    records[key] = future.result()
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    directory = output / 'setup-errors' / (key[0] + '-' + key[1] + '-replay1')
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / 'flow.log').write_text(str(exc) + '\n', encoding='utf-8')
                    records[key] = {'case': key[0], 'policy': key[1], 'status': 'failed',
                                    'directory': str(directory), 'error': str(exc)}
                publish(output, cases, records, code, parameters)
    report = publish(output, cases, records, code, parameters)
    print(f'Comparison: {output / "index.html"}; valid {report["valid_runs"]}/{len(records)}', flush=True)
    return int(any(record['status'] != 'valid' for key, record in records.items() if key[2] is None))


if __name__ == '__main__':
    raise SystemExit(main())
