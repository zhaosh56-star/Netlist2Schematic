#!/usr/bin/env python3
"""Isolated GT/off/auto region-layout runs with independent final-output checks.

This tool never changes inputs or engine defaults. A quantitative pass is not
visual approval. Diagnostic overlays are separate from validated flow SVGs.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from html import escape
import json
from math import isfinite
from pathlib import Path
import subprocess
import sys
import tempfile
from time import perf_counter
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
import compare_symmetry_layouts as common
from compare_passive_layouts import VERIFY_LOCK, _wire_paths
from compare_structure_layouts import _detour_summary
from netlist_core import NetlistError
from sa_common import load_local, read_json, write_json

FORMAT = 'region-layout-comparison-v1'
POLICIES = ('off', 'auto')
# Dataset selectors are only for validation coverage, never layout decisions.
CORE_CASES = ('data16', 'data24')
DEFAULT_SEEDS = (20261029,)
DEFAULT_CORE_SEEDS = (20261030, 20261031)
SVG = '{http://www.w3.org/2000/svg}'


def relative_link(output, path):
    try:
        return common._relative(output, path)
    except ValueError:  # An explicitly chosen output folder can be on another drive.
        return Path(path).resolve().as_uri()


def figure(output, title, path, extra=''):
    return '<figure><figcaption>'+escape(title)+'</figcaption><a href="'+escape(relative_link(output, path))+'"><img loading="lazy" src="'+escape(relative_link(output, path))+'"></a>'+extra+'</figure>'


def runtime_snapshot():
    return {p: h for p, h in common._snapshot().items() if 'tests' not in Path(p).parts}


def check_signature(signature):
    if common._input_changes(signature['source_files']):
        raise ValueError('Original input, include, provenance or GT changed')
    if runtime_snapshot() != signature['code_files']:
        raise ValueError('Runtime code or symbol resources changed during comparison')


def build_measurement_plan(source, cell, devices, structs, comps, nets, index):
    """Use source-derived rows, never either contender's movable coordinates."""
    from device_leveling import compute_levels
    from layout_intent import build_intent
    from region_layout import build_region_plan
    levels = compute_levels(devices)
    legacy = load_local('place_layered', 'place-layered.py')
    conduction = {n for d in devices.values() if d['cls'] in ('MOS', 'BJT')
                  for n in (d.get('d'), d.get('s'), d.get('c'), d.get('e')) if n is not None}
    attachments = legacy.attach_info(devices, levels['net_levels'], conduction)
    rows, _ = legacy._allocate_rows(devices, comps, index, levels['net_levels'],
                                    levels['device_levels'], attachments)
    evidence, relations = build_intent(source, cell, devices, structs, comps, nets, index)
    return build_region_plan(devices, evidence=evidence, relations=relations,
                             display_rows={name: rows[index[name]] for name in devices},
                             comps=comps, index=index)


def annotated_svg(source_svg, target, pl, comps, index, plan):
    """Independent explanatory overlay; never part of electrical validation."""
    tree = ET.parse(source_svg)
    root = tree.getroot()
    overlay = ET.SubElement(root, SVG+'g', {'data-diagnostic': 'region-layout',
                                            'pointer-events': 'none'})
    palette = ('#2563eb', '#dc2626', '#059669', '#7c3aed', '#d97706', '#0891b2')
    for number, region in enumerate(plan.get('regions', ())):
        if region.get('children'):
            continue
        members = [index[name] for name in region.get('members', ()) if name in index]
        if not members:
            continue
        xs, ys = zip(*(pl.pos_of(i) for i in members))
        x0, y0, x1, y1 = min(xs)-30, min(ys)-35, max(xs)+40, max(ys)+45
        color = palette[number % len(palette)]
        group = ET.SubElement(overlay, SVG+'g', {'data-region': str(region['id'])})
        ET.SubElement(group, SVG+'rect', {'x': str(x0), 'y': str(y0),
            'width': str(x1-x0), 'height': str(y1-y0), 'fill': color,
            'fill-opacity': '.035', 'stroke': color, 'stroke-width': '1.5',
            'stroke-dasharray': '7 5'})
        ET.SubElement(group, SVG+'title').text = str(region.get('kind', 'region')) + ': ' + ', '.join(region.get('members', ()))
        ET.SubElement(group, SVG+'text', {'x': str(x0+3), 'y': str(y0+14),
            'fill': color, 'font-size': '11', 'font-family': 'sans-serif'}).text = str(region['id'])
        for branch in region.get('branches', ()):
            names = branch.get('members', ()) if isinstance(branch, dict) else branch
            points = [pl.pos_of(index[name]) for name in names if name in index]
            if points:
                x, y = min(points, key=lambda p: (p[1], p[0]))
                ET.SubElement(group, SVG+'text', {'x': str(x+22), 'y': str(y-12),
                    'fill': color, 'font-size': '12'}).text = 'branch '+str(branch.get('id', '')) if isinstance(branch, dict) else 'branch'
    for relation in plan.get('series_axes', ()):
        a, b = relation.get('a'), relation.get('b')
        if a not in index or b not in index:
            continue
        try:
            pa = pl.pin_world(index[a], relation['pin_a'])
            pb = pl.pin_world(index[b], relation['pin_b'])
        except (KeyError, TypeError):
            continue
        ET.SubElement(overlay, SVG+'line', {'x1': str(pa[0]), 'y1': str(pa[1]),
            'x2': str(pb[0]), 'y2': str(pb[1]), 'stroke': '#be123c',
            'stroke-opacity': '.6', 'stroke-width': '2', 'stroke-dasharray': '4 4'})
    target.parent.mkdir(parents=True, exist_ok=True)
    tree.write(target, encoding='utf-8', xml_declaration=True)


def verify(path, signature):
    from circuit_structures import analyze_structures
    from layout_quality import evaluate_quality
    from netlist_core import parse_netlist
    from region_layout import measure_regions
    from routing_rules import build_scene
    from sa_routing import validate_final_routes
    from sa_saved import load_placement, saved_routing_options
    from simplify_routes import _covers_fixed
    with VERIFY_LOCK:
        data, source, par, pl, comps, nets, index = load_placement(path)
        if str(source) != signature['source_netlist'] or data.get('source_cell') != signature['cell']:
            raise ValueError('Saved result refers to another source or cell')
        routed, metrics, checked = validate_final_routes(par, nets, pl, index,
            list(data['routes'].items()), options=saved_routing_options(data))
        if not checked['valid'] or metrics != data['metrics']:
            raise ValueError('Saved metrics/routes failed independent verification')
        scene = build_scene(par, nets, pl, index, options=saved_routing_options(data))
        if not all(_covers_fixed(scene, net, runs) for net, runs in routed):
            raise ValueError('A fixed original-pin exit was omitted')
        stem = path.name.removesuffix('-placement.json')
        clean, color = path.with_name(stem+'-clean.svg'), path.with_name(stem+'-layered.svg')
        svg, wires = _wire_paths(clean)
        if wires != _wire_paths(color)[1]:
            raise ValueError('Color and clean SVG routes differ')
        renderer = load_local('region_comparison_redraw', 'render-clean.py')
        with tempfile.TemporaryDirectory(prefix='region-redraw-') as folder:
            redraw = Path(folder)/'redraw.svg'
            renderer.render(path, redraw)
            if redraw.read_bytes() != clean.read_bytes():
                raise ValueError('Independent redraw differs from saved clean SVG')
        devices = parse_netlist(source, cell=signature['cell'])
        structs = analyze_structures(devices)
        plan = build_measurement_plan(source, signature['cell'], devices, structs, comps, nets, index)
        measured = measure_regions(pl, comps, index, plan)
        quality = evaluate_quality(par, pl, comps, nets, index, routed, metrics, structs)
        annotated = path.with_name(stem+'-regions.svg')
        annotated_svg(clean, annotated, pl, comps, index, plan)
        active = {name: pl.cell_of[index[name]][0] for name, d in devices.items()
                  if d['cls'] in ('MOS', 'BJT')}
        if any(pl.orient[index[name]] not in (0, 1) for name in active):
            raise ValueError('Unsupported active orientation')
        return {'valid': True, 'placement_json': str(path), 'clean_svg': str(clean),
            'color_svg': str(color), 'annotated_svg': str(annotated), 'metrics': metrics,
            'quality': quality, 'route_detours': _detour_summary(quality),
            'measurement': measured, 'measurement_plan': plan,
            'measurement_plan_signature': plan.get('signature'),
            'layout_regions': data.get('layout_regions'),
            'component_count': len(comps), 'width': float(svg.get('width')),
            'height': float(svg.get('height')), 'active_rows': active,
            'placement': data['placement'],
            'selected_candidate': data.get('quality', {}).get('selected_candidate', data.get('candidate_id')),
            'routing_stage': data.get('routing_stage', 'normal'),
            'attempted_candidate_ids': sorted({str(r['candidate_id'])
                for r in common._candidate_records(data) if 'candidate_id' in r}),
            'candidate_records': list(common._candidate_records(data)),
            'route_simplification': data.get('route_simplification'),
            'routing_seconds': data.get('validation', {}).get('routing_seconds'),
            'quality_seconds': data.get('quality', {}).get('elapsed_seconds'),
            'recovery_stages': data.get('recovery', {}).get('stages', []),
            'all_original_pins_connected': True, 'all_fixed_stubs_preserved': True,
            'color_and_clean_routes_identical': True, 'independent_redraw_identical': True}


def command_for(signature, directory):
    p = signature['parameters']
    command = [sys.executable, '-X', 'utf8', '-B', '-u', str(ROOT/'run_flow.py'),
        signature['source_netlist'], '--out-dir', str(directory),
        '--region-layout', signature['policy'], '--structure-layout', 'off',
        '--symmetry-layout', 'off', '--passive-layout', 'flexible',
        '--seed', str(p['seed']), '--iterations', str(p['iterations']),
        '--fallback', 'auto', '--local-rescue', 'auto', '--net-labels', 'off',
        '--candidate-budget', p['candidate_budget'], '--port-preflight', 'off',
        '--presentation', p['presentation'], '--simplify-routes', 'safe']
    for key in ('quality_seconds', 'rescue_seconds', 'spacing_seconds',
                'fallback_seconds', 'simplify_seconds'):
        command += ['--'+key.replace('_', '-'), str(p[key])]
    if signature['cell']:
        command += ['--cell', signature['cell']]
    if p.get('candidate_id') is not None:
        command += ['--candidate-id', str(p['candidate_id'])]
    return command


def execute(case, seed, policy, output, parameters, code, frozen, *, retry_failed=False, reuse_only=False):
    p = dict(parameters, seed=seed)
    try:
        signature = common._signature(case, policy, p, code)
    except (ValueError, OSError, KeyError) as exc:
        unsupported = isinstance(exc, NetlistError) or str(exc).startswith((
            'No supported devices', 'Multiple subcircuits require'))
        return {'case': case['case'], 'seed': seed, 'policy': policy,
            'status': 'skipped' if unsupported else 'failed',
            'reason': 'input_validation_error' if unsupported else 'input_or_resource_error',
            'error': str(exc)}
    signature['format'] = FORMAT
    if any(frozen.get(p) != h for p, h in signature['source_files'].items()):
        raise ValueError('Batch input changed')
    fingerprint = common._digest(signature)
    base = output/'runs'/case['case']/str(seed)/policy
    base.mkdir(parents=True, exist_ok=True)
    for saved in sorted(base.glob('*/run-record.json'), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            record = read_json(saved)
        except (ValueError, OSError):
            continue
        reusable = ('valid',) if retry_failed else ('valid', 'failed')
        if record.get('fingerprint') == fingerprint and record.get('status') in reusable:
            check_signature(signature)
            if record['status'] == 'valid':
                record['verified'] = verify(Path(record['verified']['placement_json']), signature)
            record['reused'] = True
            print(f'REUSE {case["case"]}/{seed}/{policy}', flush=True)
            return record
    if reuse_only:
        return None
    directory = base/fingerprint[:16]
    if directory.exists():
        directory = base/(fingerprint[:16]+'-'+uuid.uuid4().hex[:8])
    directory.mkdir()
    command = command_for(signature, directory)
    record = {'format': FORMAT, 'case': case['case'], 'seed': seed, 'policy': policy,
        'signature': signature, 'fingerprint': fingerprint, 'directory': str(directory),
        'command': command, 'status': 'running', 'reused': False}
    write_json(directory/'run-record.json', record)
    print(f'START {case["case"]}/{seed}/{policy}', flush=True)
    started = perf_counter()
    try:
        with (directory/'flow.log').open('w', encoding='utf8') as log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            try:
                record['returncode'] = process.wait(timeout=sum(p[k] for k in (
                    'quality_seconds', 'rescue_seconds', 'spacing_seconds',
                    'fallback_seconds', 'simplify_seconds'))+120)
            except subprocess.TimeoutExpired:
                common._stop(process)
                raise ValueError('Supervisory timeout')
        check_signature(signature)
        if record['returncode']:
            diagnostic = directory/'routing-diagnostics.json'
            if diagnostic.is_file():
                record['diagnostics'] = read_json(diagnostic)
            raise ValueError('Flow failed; see flow.log')
        stem = Path(signature['source_netlist']).stem+('-'+signature['cell'] if signature['cell'] else '')
        record['verified'] = verify(directory/(stem+'-placement.json'), signature)
        record['status'] = 'valid'
    except (ValueError, OSError, KeyError, TypeError) as exc:
        record.update(status='failed', error=str(exc))
    record['elapsed_seconds'] = round(perf_counter()-started, 6)
    write_json(directory/'run-record.json', record)
    print(f'END {case["case"]}/{seed}/{policy}: {record["status"]} {record["elapsed_seconds"]:.2f}s', flush=True)
    return record


def gates(old, new):
    a, b = old['metrics'], new['metrics']
    same_plan = bool(old.get('measurement_plan_signature')) and old.get('measurement_plan_signature') == new.get('measurement_plan_signature')
    om, nm = old['measurement'], new['measurement']
    if not om.get('supported') and not nm.get('supported'):
        organization = same_plan
    else:
        organization = bool(om.get('measurable') and nm.get('measurable')) and all(
            isinstance(om.get(k), (float, int)) and isinstance(nm.get(k), (float, int))
            and nm[k] <= om[k] for k in ('interleaving', 'order_errors'))
    return {'wirelength_115_percent': b['wirelength'] <= a['wirelength']*1.15+1e-9,
        'crossings_limit': b['crossings'] <= (0 if a['crossings'] == 0 else a['crossings']+1),
        'same_measurement_plan': same_plan,
        'no_new_region_violations': organization,
        'active_rows_unchanged': old.get('active_rows') == new.get('active_rows')
            or new['routing_stage'] == 'bus_fallback' or old['routing_stage'] == 'bus_fallback',
        'no_new_bus_fallback': new['routing_stage'] != 'bus_fallback' or old['routing_stage'] == 'bus_fallback'}


def schedule(cases, seeds, core_seeds):
    return [(case, seed) for case in cases for seed in sorted(set(seeds) |
            (set(core_seeds) if case['case'] in CORE_CASES else set()))]


def region_was_evaluated(verified_result):
    report = verified_result.get('layout_regions') or {}
    if report.get('applied'):
        return True
    return any((record.get('layout_regions') or {}).get('applied') and
               record.get('status') in ('valid', 'failed')
               for record in verified_result.get('candidate_records', ()))


def publish(output, jobs, records, parameters, code, frozen):
    rows, sections = [], []
    sums = {p: {'wirelength': 0, 'crossings': 0, 'bends': 0} for p in POLICIES}
    for case, seed in jobs:
        results = {p: records[(case['case'], seed, p)] for p in POLICIES
                   if (case['case'], seed, p) in records}
        row = {**case, 'seed': seed, 'results': results}
        row['same_input_version'] = len(results) == 2 and (
            results['off'].get('signature', {}).get('source_files') ==
            results['auto'].get('signature', {}).get('source_files'))
        paired = len(results) == 2 and all(r['status'] == 'valid' for r in results.values())
        if paired and row['same_input_version']:
            row['gates'] = gates(*(results[p]['verified'] for p in POLICIES))
            for p in POLICIES:
                for key in sums[p]:
                    sums[p][key] += results[p]['verified']['metrics'][key]
        if 'auto' in results and results['auto']['status'] == 'valid':
            row['region_candidate_evaluated'] = region_was_evaluated(results['auto']['verified'])
        rows.append(row)
        figures = [figure(output, 'GT：视觉参考', case['ground_truth'])
                   if case.get('ground_truth') else '<figure>本例无GT</figure>']
        table, details = [], {}
        for policy in POLICIES:
            result = results.get(policy, {})
            if result.get('status') != 'valid':
                figures.append('<figure>'+escape(policy+': '+result.get('status', 'pending')+' '+
                                                result.get('error', ''))+'</figure>')
                continue
            v = result['verified']; m, s = v['metrics'], v['measurement']
            links = '<p>'+' · '.join('<a href="'+escape(relative_link(output, v[k]))+'">'+label+'</a>'
                for k, label in (('placement_json', 'JSON'), ('color_svg', '彩色'),
                                 ('annotated_svg', '区域与支路叠加图')) if k in v)+'</p>'
            figures.append(figure(output, policy, v['clean_svg'], links))
            table.append([policy, m['wirelength'], m['crossings'], m['bends'],
                s.get('interleaving'), s.get('order_errors'),
                s.get('pin_axis_error'), s.get('dispersion'),
                v.get('route_detours', {}).get('maximum_factor'),
                f'{v["width"]}×{v["height"]}', v.get('selected_candidate'),
                ','.join(v.get('attempted_candidate_ids', [])), v['routing_stage'],
                result.get('elapsed_seconds')])
            details[policy] = {k: v.get(k) for k in ('measurement', 'measurement_plan',
                'layout_regions', 'candidate_records', 'recovery_stages', 'route_detours')}
        sections.append('<section><h2>'+escape(f'{case["case"]} · seed {seed} · '+case.get('title', ''))+
            '</h2><div class="figures">'+''.join(figures)+'</div>'+common._table(
                ['模式', '线长', '交叉', '折角', '穿插', '顺序冲突', '引脚偏差', '区域跨度',
                 '最大整网绕行比', '尺寸', '选中候选', '实际尝试候选', '阶段', '秒'], table)+
            '<pre>'+escape(json.dumps(row.get('gates', {}), ensure_ascii=False, indent=2))+'</pre>'+
            '<details><summary>识别覆盖、区域使用、候选与逐网绕行</summary><pre>'+
            escape(json.dumps(details, ensure_ascii=False, indent=2))+'</pre></details></section>')
    consistent = all(len(row['results']) == 2 and (
        all(r['status'] == 'valid' for r in row['results'].values()) or
        all(r['status'] == 'skipped' and r.get('reason') == 'input_validation_error'
            for r in row['results'].values())) for row in rows)
    checks = {'complete': len(records) == len(jobs)*2,
        'inputs_unchanged': not common._input_changes(frozen),
        'paired_status_consistent': consistent,
        'has_successful_pairs': any('gates' in row for row in rows),
        'paired_sources_identical': all(row['same_input_version'] for row in rows
            if len(row['results']) == 2 and all(r['status'] == 'valid' for r in row['results'].values())),
        'no_routing_failures': all(r['status'] != 'failed' for r in records.values()),
        'paired_gates': all(all(row['gates'].values()) for row in rows if 'gates' in row),
        'total_crossings_nonincreasing': sums['auto']['crossings'] <= sums['off']['crossings']}
    report = {'format': FORMAT, 'parameters': parameters, 'code_files': code,
        'input_files': frozen, 'input_changes': common._input_changes(frozen),
        'expected_runs': len(jobs)*2, 'completed_runs': len(records),
        'valid_runs': sum(r['status'] == 'valid' for r in records.values()),
        'skipped_runs': sum(r['status'] == 'skipped' for r in records.values()),
        'failed_runs': sum(r['status'] == 'failed' for r in records.values()),
        'totals': sums, 'cases': rows, 'release_gates': checks,
        'quantitative_gates_passed': all(checks.values()),
        'full_suite': bool(parameters.get('full_discovered_suite')),
        'ground_truth_electrically_verified': False,
        'visual_review': 'pending', 'default_promotion_approved': False}
    write_json(output/'comparison.json', report)
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>区域布局对照</title>
<style>body{font:14px/1.6 system-ui;background:#f5f6f8;margin:24px}section{background:white;padding:20px;margin:24px 0}.figures{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}figure{margin:0}img{width:100%;height:470px;object-fit:contain}table{border-collapse:collapse}td,th{padding:6px 8px;border-bottom:1px solid #ddd}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>
<h1>区域布局：GT / off / auto</h1>
<p>GT仅作相对组织与视觉参考，不作为布局输入或数值最优解。两侧均保留完整导线，使用同一源网表关系清单重新测量。
成功图独立复验电气连接、几何、固定出线、双SVG及重绘。叠加框与虚线只在独立诊断图显示，不表示真实导线或硬禁区。</p>
<p>门槛检查最终缩线后的成品；覆盖不足不能解释为组织已全部正确。applied不等于选中了新候选。
部分样例或数值达标不自动批准默认开启，仍需完整回归和视觉检查。</p>'''
    (output/'index.html').write_text(page+'<pre>'+escape(json.dumps({
        'release_gates': checks, 'totals': sums, 'completed': f'{len(records)}/{len(jobs)*2}',
        'visual_review': 'pending'}, ensure_ascii=False, indent=2))+'</pre>'+''.join(sections)+'</html>', encoding='utf8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', type=Path, default=ROOT/'output/region-layout-comparison')
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--seeds', nargs='+', type=int, default=list(DEFAULT_SEEDS))
    parser.add_argument('--core-seeds', nargs='*', type=int, default=list(DEFAULT_CORE_SEEDS))
    parser.add_argument('--jobs', type=int, choices=range(1, 7), default=2)
    parser.add_argument('--policies', nargs='+', choices=POLICIES, default=list(POLICIES))
    parser.add_argument('--candidate-id', choices=('1', '2', '3'))
    parser.add_argument('--candidate-budget', choices=('sequential', 'reserved'), default='sequential')
    parser.add_argument('--presentation', choices=('legacy', 'clean'), default='clean')
    parser.add_argument('--retry-failed', action='store_true')
    for key, default in common.DEFAULT_PARAMETERS.items():
        if key not in ('jobs', 'seed'):
            parser.add_argument('--'+key.replace('_', '-'), type=int if key == 'iterations' else float, default=default)
    args = parser.parse_args(argv)
    if args.iterations < 0:
        parser.error('iterations must be non-negative')
    for key in ('quality_seconds', 'fallback_seconds', 'rescue_seconds', 'spacing_seconds', 'simplify_seconds'):
        value = getattr(args, key)
        if not isfinite(value) or value < 0 or value == 0 and key in ('quality_seconds', 'fallback_seconds'):
            parser.error(key+' must be finite and '+('positive' if key in ('quality_seconds', 'fallback_seconds') else 'non-negative'))
    cases = common.discover_cases()
    if args.cases:
        missing = set(args.cases)-{c['case'] for c in cases}
        if missing:
            parser.error('Unknown cases: '+', '.join(sorted(missing)))
        cases = [c for c in cases if c['case'] in args.cases]
    if not cases:
        parser.error('No selected cases')
    parameters = {k: getattr(args, k) for k in common.DEFAULT_PARAMETERS if k != 'seed'}
    parameters.update(seeds=args.seeds, core_seeds=args.core_seeds, candidate_id=args.candidate_id,
        candidate_budget=args.candidate_budget, presentation=args.presentation,
        full_discovered_suite=args.cases is None and args.candidate_id is None)
    output = args.out_dir.resolve(); output.mkdir(parents=True, exist_ok=True)
    jobs = schedule(cases, args.seeds, args.core_seeds)
    frozen, code = common._freeze_inputs(cases), runtime_snapshot()
    records = {}
    for case, seed in jobs:
        for policy in POLICIES:
            if policy not in args.policies:
                record = execute(case, seed, policy, output, parameters, code, frozen, reuse_only=True)
                if record is not None:
                    records[(case['case'], seed, policy)] = record
    publish(output, jobs, records, parameters, code, frozen)
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(execute, case, seed, policy, output, parameters, code, frozen,
                               retry_failed=args.retry_failed): (case, seed, policy)
                   for case, seed in jobs for policy in POLICIES if policy in args.policies}
        for future in as_completed(futures):
            case, seed, policy = futures[future]
            try:
                record = future.result()
            except Exception as exc:
                record = {'case': case['case'], 'seed': seed, 'policy': policy,
                          'status': 'failed', 'error': str(exc)}
            records[(case['case'], seed, policy)] = record
            publish(output, jobs, records, parameters, code, frozen)
    return int(any(r['status'] == 'failed' for r in records.values()))


if __name__ == '__main__':
    raise SystemExit(main())
