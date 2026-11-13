#!/usr/bin/env python3
"""Isolated stage comparisons: saved-route tradeoffs, then optional layout work.

The engine and all netlists are inputs. This tool never changes their defaults.
Independent validation and numeric gates do not constitute visual approval.
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
from time import perf_counter
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tools')]
import compare_region_layouts as region
import compare_symmetry_layouts as common
from compare_passive_layouts import VERIFY_LOCK
from netlist2schematic.netlist.netlist_core import NetlistError
from netlist2schematic.common.sa_common import load_local, read_json, write_json

FORMAT = 'readability-comparison-v1'
POLICIES = ('off', 'auto')
DEFAULT_BASELINE = ROOT/'output/region-layout-comparison/comparison.json'


def actual_candidates(data):
    """Initialization trial tables are not whole-graph router attempts."""
    unique = {}
    for record in common._candidate_records(data):
        if record.get('candidate_id') is None:
            continue
        key = (str(record['candidate_id']), record.get('status'), record.get('routing_stage'))
        # Keep the richest duplicate, since quality metadata may summarize it.
        if key not in unique or len(record) > len(unique[key]):
            unique[key] = deepcopy(record)
    return [unique[key] for key in sorted(unique, key=str)]


def saved_baselines(path):
    """Only the selected seed's valid off result is a stage-one source."""
    data = read_json(path)
    result = {}
    for case in data.get('cases', ()):
        record = case.get('results', {}).get('off', {})
        if record.get('status') == 'valid':
            value = record.get('verified', {}).get('placement_json')
            if value:
                result[(case['case'], int(case['seed']))] = Path(value).resolve()
    return result


def command_for(signature, directory):
    p = signature['parameters']
    command = [sys.executable, '-X', 'utf8', '-B', '-u', str(ROOT/'run_flow.py')]
    if p['stage'] == 1:
        command += ['--simplify-placement', signature['placement_snapshot']]
    else:
        command += [signature['source_netlist'], '--readability-layout', signature['policy'],
            '--readability-phase', str(p['stage']), '--region-layout', 'off',
            '--structure-layout', 'off', '--symmetry-layout', 'off',
            '--passive-layout', 'flexible', '--seed', str(p['seed']),
            '--iterations', str(p['iterations']), '--fallback', 'auto',
            '--local-rescue', 'auto', '--candidate-budget', p['candidate_budget'],
            '--port-preflight', 'off']
        if signature['cell']:
            command += ['--cell', signature['cell']]
        if p.get('candidate_id') is not None:
            command += ['--candidate-id', str(p['candidate_id'])]
        for key in ('quality_seconds', 'rescue_seconds', 'spacing_seconds', 'fallback_seconds'):
            command += ['--'+key.replace('_', '-'), str(p[key])]
    command += ['--out-dir', str(directory), '--simplify-routes', 'safe',
                '--simplify-seconds', str(p['simplify_seconds']),
                '--route-tradeoff', 'balanced' if signature['policy'] == 'auto' else 'strict',
                '--net-labels', 'off', '--presentation', p['presentation']]
    return command


def build_measurement_plan(source, cell, comps, nets, index):
    from netlist2schematic.recognition.circuit_structures import analyze_structures
    from netlist2schematic.recognition.device_leveling import compute_levels
    from netlist2schematic.netlist.netlist_core import parse_netlist
    from netlist2schematic.plugins.readability.readability_layout import build_readability_plan
    devices = parse_netlist(source, cell=cell)
    structs, levels = analyze_structures(devices), compute_levels(devices)
    legacy = load_local('place_layered', 'place-layered.py')
    conduction = {n for d in devices.values() if d['cls'] in ('MOS', 'BJT')
                  for n in (d.get('d'), d.get('s'), d.get('c'), d.get('e')) if n is not None}
    attachments = legacy.attach_info(devices, levels['net_levels'], conduction)
    rows, _ = legacy._allocate_rows(devices, comps, index, levels['net_levels'],
                                    levels['device_levels'], attachments)
    return build_readability_plan(devices, comps, nets, index, structs, levels, rows)


def tradeoff_audit(data, source_data=None):
    """Check reported credit against final metrics, without relaxing geometry."""
    report = data.get('route_simplification', {})
    decision = report.get('tradeoff') or {}
    before = report.get('before_metrics', {})
    initial = before.get('crossings')
    after = data['metrics']['crossings']
    policy = decision.get('policy', report.get('tradeoff_policy', 'strict'))
    if initial is None:
        return {'applicable': False, 'reason': report.get('stop_reason', 'no_simplification_report'),
                'policy': policy, 'valid': data.get('routing_stage') == 'bus_fallback'
                    or report.get('status') == 'skipped'}
    allowed = 2 if policy == 'balanced' else 0
    accepted = [c for c in report.get('accepted_changes', ()) if c.get('tradeoff')]
    decision_checks = []
    for change in accepted:
        item = change['tradeoff']
        density = item.get('density', {})
        decision_checks.append(bool(item.get('accepted')) and
            item.get('cumulative_crossing_increase', allowed+1) <= allowed and
            density.get('conflict') is None and
            (density.get('nearest_distance') is None or
             density['nearest_distance'] >= density.get('required_distance', float('inf'))-1e-8))
    checks = {'initial_anchor_matches': decision.get('baseline_crossings', initial) == initial,
              'cumulative_crossings_within_allowance': after <= initial+allowed,
              'accepted_decision_records_consistent': all(decision_checks)}
    if source_data is not None:
        checks['saved_positions_unchanged'] = data.get('placement') == source_data.get('placement')
        checks['initial_wirelength_revalidated'] = before.get('wirelength') == source_data['metrics']['wirelength']
    return {'applicable': True, 'policy': policy, 'baseline_crossings': initial,
            'final_crossings': after, 'allowed_increase': allowed, 'checks': checks,
            'valid': all(checks.values()), 'accepted_crossing_increases': decision.get(
                'accepted_with_extra_crossings', 0)}


def verify(path, signature):
    from netlist2schematic.common.sa_saved import load_placement
    result = region.verify(path, signature)
    data = read_json(path)
    result['candidate_records'] = actual_candidates(data)
    result['attempted_candidate_ids'] = sorted({str(r['candidate_id']) for r in result['candidate_records']})
    source_data = read_json(signature['placement_snapshot']) if signature['parameters']['stage'] == 1 else None
    result['tradeoff_audit'] = tradeoff_audit(data, source_data)
    if not result['tradeoff_audit']['valid']:
        raise ValueError('Final output failed independent tradeoff-record audit')
    result['layout_readability'] = deepcopy(data.get('layout_readability'))
    result['readability_measurement'] = None
    if signature['parameters']['stage'] >= 2:
        from netlist2schematic.plugins.readability.readability_layout import measure_readability
        with VERIFY_LOCK:
            _, source, par, pl, comps, nets, index = load_placement(path)
            plan = build_measurement_plan(source, signature['cell'], comps, nets, index)
            result['readability_measurement'] = measure_readability(pl, comps, index, plan)
            result['readability_measurement_plan'] = plan
            result['readability_plan_signature'] = common._digest(plan)
            if signature['parameters']['stage'] >= 3:
                from netlist2schematic.plugins.readability.readability_refinement import measure_refinements
                result['refinement_measurement'] = measure_refinements(
                    par, pl, comps, nets, index, plan, phase=min(signature['parameters']['stage'], 4))
    return result


def execute(case, seed, policy, output, parameters, code, frozen, snapshots, *, retry_failed=False):
    p = dict(parameters, seed=seed)
    try:
        signature = common._signature(case, policy, p, code)
    except (ValueError, OSError, KeyError) as exc:
        unsupported = isinstance(exc, NetlistError) or str(exc).startswith((
            'No supported devices', 'Multiple subcircuits require'))
        return {'case': case['case'], 'seed': seed, 'policy': policy,
            'status': 'skipped' if unsupported else 'failed',
            'reason': 'input_validation_error' if unsupported else 'input_or_resource_error', 'error': str(exc)}
    signature['format'] = FORMAT
    if p['stage'] == 1:
        source = snapshots.get((case['case'], seed))
        if source is None or not source.is_file():
            return {'case': case['case'], 'seed': seed, 'policy': policy, 'status': 'not_run',
                    'reason': 'saved_off_snapshot_unavailable', 'error': 'Stage one never substitutes a new flow run'}
        signature['placement_snapshot'] = str(source)
        signature['source_files'][str(source)] = common._sha(source)
    if any(frozen.get(path) != digest for path, digest in signature['source_files'].items()):
        raise ValueError('Batch input changed')
    fingerprint = common._digest(signature)
    base = output/'runs'/case['case']/str(seed)/policy
    base.mkdir(parents=True, exist_ok=True)
    for saved in sorted(base.glob('*/run-record.json'), key=lambda q: q.stat().st_mtime, reverse=True):
        record = read_json(saved)
        if record.get('fingerprint') == fingerprint and record.get('status') in (
                ('valid',) if retry_failed else ('valid', 'failed')):
            region.check_signature(signature)
            if record['status'] == 'valid':
                record['verified'] = verify(Path(record['verified']['placement_json']), signature)
            record['reused'] = True
            print(f'REUSE {case["case"]}/{seed}/{policy}', flush=True)
            return record
    directory = base/fingerprint[:16]
    if directory.exists():
        directory = base/(fingerprint[:16]+'-'+uuid.uuid4().hex[:8])
    directory.mkdir()
    command = command_for(signature, directory)
    record = {'format': FORMAT, 'stage': p['stage'], 'case': case['case'], 'seed': seed,
        'policy': policy, 'signature': signature, 'fingerprint': fingerprint,
        'directory': str(directory), 'command': command, 'status': 'running', 'reused': False}
    write_json(directory/'run-record.json', record)
    print(f'START stage{p["stage"]}/{case["case"]}/{seed}/{policy}', flush=True)
    started = perf_counter()
    try:
        with (directory/'flow.log').open('w', encoding='utf8') as log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            limit = p['simplify_seconds']+120
            if p['stage'] > 1:
                limit += sum(p[k] for k in ('quality_seconds', 'rescue_seconds', 'spacing_seconds', 'fallback_seconds'))
            try:
                record['returncode'] = process.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                common._stop(process)
                raise ValueError('Supervisory timeout')
        region.check_signature(signature)
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


def gates(old, new, stage):
    a, b = old['metrics'], new['metrics']
    checks = {'wirelength_115_percent': b['wirelength'] <= 1.15*a['wirelength']+1e-8,
              'crossings_within_off_plus_two': b['crossings'] <= a['crossings']+2,
              'tradeoff_audits_passed': bool(old.get('tradeoff_audit', {}).get('valid')) and
                                      bool(new.get('tradeoff_audit', {}).get('valid')),
              'no_new_bus_fallback': new['routing_stage'] != 'bus_fallback' or old['routing_stage'] == 'bus_fallback'}
    if stage == 1:
        checks['saved_placement_unchanged'] = old.get('placement') == new.get('placement')
        checks['same_source_plan'] = bool(old.get('measurement_plan_signature')) and old.get(
            'measurement_plan_signature') == new.get('measurement_plan_signature')
    else:
        checks['same_source_plan'] = bool(old.get('readability_plan_signature')) and old.get(
            'readability_plan_signature') == new.get('readability_plan_signature')
    return checks


def publish(output, jobs, records, parameters, code, frozen):
    rows, sections = [], []
    sums = {p: dict(wirelength=0, crossings=0, bends=0) for p in POLICIES}
    for case, seed in jobs:
        results = {p: records[(case['case'], seed, p)] for p in POLICIES if (case['case'], seed, p) in records}
        row = {**case, 'seed': seed, 'results': results}
        valid = len(results) == 2 and all(r['status'] == 'valid' for r in results.values())
        row['same_input_version'] = valid and results['off']['signature']['source_files'] == results['auto']['signature']['source_files']
        if valid and row['same_input_version']:
            row['gates'] = gates(*(results[p]['verified'] for p in POLICIES), parameters['stage'])
            for p in POLICIES:
                for key in sums[p]:
                    sums[p][key] += results[p]['verified']['metrics'][key]
        rows.append(row)
        figures = [region.figure(output, 'GT：视觉参考', case['ground_truth'])
                   if case.get('ground_truth') else '<figure>本例无GT</figure>']
        table, details = [], {}
        for p in POLICIES:
            result = results.get(p, {})
            if result.get('status') != 'valid':
                figures.append('<figure>'+escape(p+': '+result.get('status', 'pending')+' '+result.get('error', ''))+'</figure>')
                continue
            v, m = result['verified'], result['verified']['metrics']
            links = '<p>'+' · '.join('<a href="'+escape(region.relative_link(output, v[k]))+'">'+label+'</a>'
                for k, label in (('placement_json', 'JSON'), ('color_svg', '彩色'), ('annotated_svg', '关系标注')) if k in v)+'</p>'
            figures.append(region.figure(output, p+' / '+('strict' if p == 'off' else 'balanced'), v['clean_svg'], links))
            table.append([p, m['wirelength'], m['crossings'], m['bends'],
                v.get('route_detours', {}).get('maximum_factor'), f'{v["width"]}×{v["height"]}',
                v.get('selected_candidate'), ','.join(v.get('attempted_candidate_ids', [])),
                v['routing_stage'], result.get('elapsed_seconds')])
            details[p] = {key: v.get(key) for key in ('layout_readability', 'readability_measurement',
                'refinement_measurement', 'candidate_records', 'route_detours', 'tradeoff_audit',
                'route_simplification', 'recovery_stages')}
        sections.append('<section><h2>'+escape(f'{case["case"]} · seed {seed} · '+case.get('title', ''))+
            '</h2><div class="figures">'+''.join(figures)+'</div>'+common._table(
                ['策略', '线长', '交叉', '折角', '最大整网绕行比', '尺寸', '保留候选', '实际尝试候选', '阶段', '耗时'], table)+
            '<pre>'+escape(json.dumps(row.get('gates', {}), ensure_ascii=False, indent=2))+'</pre>'+
            '<details><summary>策略、真实候选、逐网绕行与验收记录</summary><pre>'+
            escape(json.dumps(details, ensure_ascii=False, indent=2))+'</pre></details></section>')
    consistent = all(len(row['results']) == 2 and (
        all(r['status'] == 'valid' for r in row['results'].values()) or
        all(r['status'] == 'skipped' and r.get('reason') == 'input_validation_error' for r in row['results'].values())) for row in rows)
    checks = {'complete': len(records) == len(jobs)*2 and all(r['status'] not in ('not_run', 'running') for r in records.values()),
        'inputs_unchanged': not common._input_changes(frozen), 'paired_status_consistent': consistent,
        'has_successful_pairs': any('gates' in row for row in rows),
        'paired_sources_identical': all(row['same_input_version'] for row in rows if len(row['results']) == 2 and
             all(r['status'] == 'valid' for r in row['results'].values())),
        'no_routing_failures': all(r['status'] != 'failed' for r in records.values()),
        'paired_gates': all(all(row['gates'].values()) for row in rows if 'gates' in row)}
    report = {'format': FORMAT, 'stage': parameters['stage'], 'parameters': parameters,
        'code_files': code, 'input_files': frozen, 'expected_runs': len(jobs)*2,
        'completed_runs': len(records), 'valid_runs': sum(r['status'] == 'valid' for r in records.values()),
        'skipped_runs': sum(r['status'] == 'skipped' for r in records.values()),
        'not_run': sum(r['status'] == 'not_run' for r in records.values()),
        'failed_runs': sum(r['status'] == 'failed' for r in records.values()),
        'totals': sums, 'cases': rows, 'release_gates': checks,
        'quantitative_gates_passed': all(checks.values()), 'visual_review': 'pending',
        'default_promotion_approved': False,
        'crossing_policy': 'Each balanced simplification: initial + 2; final comparison: off + 2. Total crossings may rise.',
        'full_suite': bool(parameters.get('full_discovered_suite')), 'ground_truth_electrically_verified': False}
    write_json(output/'comparison.json', report)
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>可读性阶段对照</title>
<style>body{font:14px/1.6 system-ui;margin:24px;background:#f5f6f8}section{background:white;padding:20px;margin:24px 0}.figures{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}figure{margin:0}img{width:100%;height:480px;object-fit:contain}td,th{padding:5px 8px;border-bottom:1px solid #ddd}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>
<h1>可读性改进：GT / off strict / auto balanced</h1>
<p>阶段1仅处理相同已保存线路，不运行SA或A*；阶段2～5使用同参完整flow。原图与网表保留。每份成功图独立检查原始引脚连接、固定出线、完整几何、双SVG与重绘。</p>
<p>允许稀疏合法交叉换取明显缩短：不能沿用“总交叉不增加”作为新策略门槛。每轮缩线最多相对初始线路净增2处，同时最终成品交叉不能超过off加2、线长不能超过off的115%。GT只是视觉参考；数值通过不代表视觉验收或默认启用。</p>'''
    (output/'index.html').write_text(page+'<pre>'+escape(json.dumps({
        'stage': parameters['stage'], 'release_gates': checks, 'totals': sums,
        'completed': f'{len(records)}/{len(jobs)*2}'}, ensure_ascii=False, indent=2))+'</pre>'+''.join(sections)+'</html>', encoding='utf8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=int, choices=range(1, 6), required=True)
    parser.add_argument('--out-dir', type=Path, default=ROOT/'output/readability-improvement')
    parser.add_argument('--baseline-report', type=Path, default=DEFAULT_BASELINE)
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--seeds', nargs='+', type=int, default=list(region.DEFAULT_SEEDS))
    parser.add_argument('--core-seeds', nargs='*', type=int, default=list(region.DEFAULT_CORE_SEEDS))
    parser.add_argument('--jobs', type=int, choices=range(1, 7), default=2)
    parser.add_argument('--candidate-id', choices=('1', '2', '3'))
    parser.add_argument('--candidate-budget', choices=('sequential', 'reserved'), default='sequential')
    parser.add_argument('--presentation', choices=('legacy', 'clean'), default='clean')
    parser.add_argument('--retry-failed', action='store_true')
    for key, value in common.DEFAULT_PARAMETERS.items():
        if key not in ('jobs', 'seed'):
            parser.add_argument('--'+key.replace('_', '-'), type=int if key == 'iterations' else float, default=value)
    args = parser.parse_args(argv)
    if args.stage == 1 and args.candidate_id is not None:
        parser.error('Stage one preserves saved geometry and does not run a candidate')
    if args.iterations < 0:
        parser.error('iterations must be non-negative')
    for key in ('quality_seconds', 'fallback_seconds', 'rescue_seconds', 'spacing_seconds', 'simplify_seconds'):
        value = getattr(args, key)
        if not isfinite(value) or value < 0 or value == 0 and key in ('quality_seconds', 'fallback_seconds'):
            parser.error(key+' must be finite and positive, or non-negative for optional stages')
    cases = common.discover_cases()
    if args.cases:
        missing = set(args.cases)-{c['case'] for c in cases}
        if missing:
            parser.error('Unknown cases: '+', '.join(sorted(missing)))
        cases = [c for c in cases if c['case'] in args.cases]
    jobs = region.schedule(cases, args.seeds, args.core_seeds)
    parameters = {k: getattr(args, k) for k in common.DEFAULT_PARAMETERS if k != 'seed'}
    parameters.update(stage=args.stage, seeds=args.seeds, core_seeds=args.core_seeds,
        candidate_id=args.candidate_id, candidate_budget=args.candidate_budget,
        presentation=args.presentation, full_discovered_suite=args.cases is None and args.candidate_id is None)
    output = args.out_dir.resolve()/f'stage{args.stage}'
    output.mkdir(parents=True, exist_ok=True)
    frozen, code = common._freeze_inputs(cases), region.runtime_snapshot()
    snapshots = saved_baselines(args.baseline_report) if args.stage == 1 else {}
    for case, seed in jobs:
        source = snapshots.get((case['case'], seed))
        if source is not None and source.is_file():
            frozen[str(source)] = common._sha(source)
    records = {}
    publish(output, jobs, records, parameters, code, frozen)
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(execute, case, seed, policy, output, parameters, code, frozen, snapshots,
                               retry_failed=args.retry_failed): (case, seed, policy)
                   for case, seed in jobs for policy in POLICIES}
        for future in as_completed(futures):
            case, seed, policy = futures[future]
            try:
                record = future.result()
            except Exception as exc:
                record = {'case': case['case'], 'seed': seed, 'policy': policy, 'status': 'failed', 'error': str(exc)}
            records[(case['case'], seed, policy)] = record
            publish(output, jobs, records, parameters, code, frozen)
    return int(any(r['status'] in ('failed', 'not_run') for r in records.values()))


if __name__ == '__main__':
    raise SystemExit(main())
