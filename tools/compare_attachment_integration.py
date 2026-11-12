#!/usr/bin/env python3
"""Compare off/passive/all attachment placement with frozen inputs and budgets.

This runner does not enable any engine default, normalize a netlist, or modify
an existing result. Each policy owns an isolated, resumable fingerprint folder.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from html import escape
import json
from math import isfinite
from pathlib import Path
import re
import shutil
import subprocess
import sys
from time import perf_counter
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
import compare_region_layouts as region
import compare_symmetry_layouts as common
from compare_passive_layouts import VERIFY_LOCK
from netlist_core import NetlistError
from sa_common import load_local, read_json, write_json

FORMAT = 'attachment-integration-comparison-v1'
POLICIES = ('off', 'passive', 'all')
CORE_CASES = ('data10', 'data12', 'data14', 'data15')
DEFAULT_SEEDS = (20261029,)
DEFAULT_CORE_SEEDS = (20261030, 20261031)
FLOW_PARAMETER_KEYS = (
    'seed', 'iterations', 'quality_seconds', 'rescue_seconds', 'spacing_seconds',
    'fallback_seconds', 'simplify_seconds', 'candidate_id', 'candidate_budget',
    'presentation', 'jobs', 'measurement_policy')


def schedule(cases, seeds, core_seeds):
    return [(case, seed) for case in cases for seed in sorted(set(seeds) |
            (set(core_seeds) if case['case'] in CORE_CASES else set()))]


def actual_candidates(data):
    """Exclude initialization trials and repeated summaries of a route attempt."""
    unique = {}
    for row in common._candidate_records(data):
        if row.get('candidate_id') is None:
            continue
        key = (str(row['candidate_id']), row.get('status'), row.get('routing_stage'))
        if key not in unique or len(row) > len(unique[key]):
            unique[key] = deepcopy(row)
    return [unique[key] for key in sorted(unique, key=str)]


def command_for(signature, directory):
    p = signature['parameters']
    command = [sys.executable, '-X', 'utf8', '-B', '-u', str(ROOT / 'run_flow.py'),
        signature['source_netlist'], '--out-dir', str(directory),
        '--attachment-layout', signature['policy'], '--layout-style', 'structured',
        '--readability-layout', 'off', '--region-layout', 'off',
        '--structure-layout', 'off', '--symmetry-layout', 'off',
        '--passive-layout', 'flexible', '--route-tradeoff', 'strict',
        '--candidate-budget', p['candidate_budget'], '--presentation', p['presentation'],
        '--port-preflight', 'off', '--net-labels', 'off',
        '--fallback', 'auto', '--local-rescue', 'auto', '--simplify-routes', 'safe',
        '--seed', str(p['seed']), '--iterations', str(p['iterations'])]
    for name in ('quality_seconds', 'rescue_seconds', 'spacing_seconds',
                 'fallback_seconds', 'simplify_seconds'):
        command += ['--' + name.replace('_', '-'), str(p[name])]
    if signature.get('cell'):
        command += ['--cell', signature['cell']]
    if p.get('candidate_id') is not None:
        command += ['--candidate-id', str(p['candidate_id'])]
    return command


def build_measurement_plan(source, cell, comps, nets, index, policy):
    """Identify once from original rows; measure every contender by this plan."""
    from attachment_integration import build_attachment_plan
    from circuit_structures import analyze_structures
    from device_leveling import compute_levels
    from netlist_core import parse_netlist
    devices = parse_netlist(source, cell=cell)
    structs, levels = analyze_structures(devices), compute_levels(devices)
    legacy = load_local('place_layered', 'place-layered.py')
    conduction = {node for device in devices.values() if device['cls'] in ('MOS', 'BJT')
                  for node in (device.get('d'), device.get('s'), device.get('c'), device.get('e'))
                  if node is not None}
    attachments = legacy.attach_info(devices, levels['net_levels'], conduction)
    rows, _ = legacy._allocate_rows(devices, comps, index, levels['net_levels'],
                                    levels['device_levels'], attachments)
    return build_attachment_plan(devices, comps, nets, index, structs, levels, rows, policy=policy)


def strict_simplification_audit(data):
    report = data.get('route_simplification') or {}
    policy = (report.get('tradeoff') or {}).get('policy', report.get('tradeoff_policy', 'strict'))
    before = report.get('before_metrics') or {}
    after = data['metrics']
    if policy != 'strict':
        return {'valid': False, 'policy': policy, 'reason': 'comparison_requires_strict'}
    if not before:
        allowed = data.get('routing_stage') == 'bus_fallback' or report.get('status') == 'skipped'
        return {'valid': allowed, 'policy': policy, 'applicable': False,
                'reason': report.get('stop_reason', 'no_simplification_report')}
    checks = {key + '_nonincreasing': after[key] <= before[key] + 1e-8
              for key in ('wirelength', 'crossings', 'bends') if key in before}
    return {'valid': len(checks) == 3 and all(checks.values()), 'policy': policy,
            'applicable': True, 'before': before, 'after': after, 'checks': checks}


def checked_payload(value):
    """Runtime RNGs/budget objects are not silently stringified into a report."""
    json.dumps(value, ensure_ascii=False, allow_nan=False)
    return value


def verify(path, signature):
    """Reuse the original full validator, SVG checks and exact saved redraw."""
    from attachment_selection import measure_candidate
    from sa_saved import load_placement
    result = region.verify(path, signature)
    data = read_json(path)
    result['strict_simplification_audit'] = strict_simplification_audit(data)
    if not result['strict_simplification_audit']['valid']:
        raise ValueError('Saved result failed the strict simplification audit')
    result['candidate_records'] = actual_candidates(data)
    result['attempted_candidate_ids'] = sorted({str(row['candidate_id']) for row in result['candidate_records']})
    result['layout_attachments'] = deepcopy(data.get('layout_attachments'))
    result['layout_grid'] = deepcopy(data.get('layout_grid'))
    result['routes_digest'] = common._digest(data['routes'])
    result['placement_digest'] = common._digest({'placement': data['placement'], 'layout_grid': data.get('layout_grid')})
    with VERIFY_LOCK:
        _, source, _, pl, comps, nets, index = load_placement(path)
        plan = build_measurement_plan(source, signature['cell'], comps, nets, index,
                                      signature['parameters']['measurement_policy'])
        measured = measure_candidate(pl, comps, index, data, plan,
            stage='independent_comparison', policy=signature['parameters']['measurement_policy'])
        result['attachment_measurement'] = measured['layout_attachments']['metrics']
        result['attachment_measurement_context'] = {
            'placement_slot_contract': measured['layout_attachments'].get('placement_slot_contract'),
            'measurement_stage': measured['layout_attachments'].get('measurement_stage')}
        result['attachment_measurement_plan'] = plan
        result['attachment_plan_signature'] = common._digest(plan)
        result['connectivity_digest'] = common._digest({c['id']: c['nodes'] for c in comps})
    return checked_payload(result)


def input_signature(case, seed, policy, parameters, code):
    execution = {key: parameters[key] for key in FLOW_PARAMETER_KEYS if key in parameters}
    execution['seed'] = seed
    signature = common._signature(case, policy, execution, code)
    signature['format'] = FORMAT
    signature['engine_flags'] = {'attachment_layout': policy, 'readability_layout': 'off',
        'region_layout': 'off', 'structure_layout': 'off', 'symmetry_layout': 'off',
        'passive_layout': 'flexible', 'route_tradeoff': 'strict', 'port_preflight': 'off',
        'net_labels': 'off', 'fallback': 'auto', 'local_rescue': 'auto'}
    return signature


def copy_saved_output(path, directory):
    """Verification sidecars belong to the new comparison, not the old image."""
    stem = path.name.removesuffix('-placement.json')
    directory.mkdir(parents=True, exist_ok=True)
    for suffix in ('-placement.json', '-clean.svg', '-layered.svg'):
        shutil.copy2(path.with_name(stem + suffix), directory / (stem + suffix))
    return directory / path.name


def import_compatible_off(reference, audit, signature, output):
    """Explicit historical baseline, backed by off-state and output rechecks.

    The old execution signature/time are retained. This is not a new flow run,
    and the state audit is not presented as wall-clock routing equivalence.
    """
    old = reference.get('signature', {})
    if reference.get('status') != 'valid' or old.get('policy') != 'off' or signature['policy'] != 'off':
        raise ValueError('Only a valid off result can be imported as a historical baseline')
    if old.get('source_netlist') != signature['source_netlist'] or old.get('cell') != signature['cell'] or old.get('source_files') != signature['source_files']:
        raise ValueError('Historical off input, cell or provenance does not match')
    keys = [key for key in FLOW_PARAMETER_KEYS if key != 'measurement_policy']
    if any(old.get('parameters', {}).get(key) != signature['parameters'].get(key) for key in keys):
        raise ValueError('Historical off seed, budget or execution parameters differ')
    if old.get('engine_flags') != signature['engine_flags']:
        raise ValueError('Historical off engine policy flags differ')
    resource_files = {path: digest for path, digest in signature['code_files'].items()
        if not path.endswith('.py')}
    if any(old.get('code_files', {}).get(path) != digest for path, digest in resource_files.items()):
        raise ValueError('Historical off symbol resources or dependency specification differ')
    row = next((row for row in audit['cases'] if row['case'] == reference['case']), None)
    if row is None or row.get('status') != 'valid' or not row.get('checks') or not all(row['checks'].values()):
        raise ValueError('Current off-state audit does not cover this valid historical input')
    fingerprint = common._digest(signature)
    directory = output / 'imports' / reference['case'] / str(signature['parameters']['seed']) / fingerprint[:16]
    original = Path(reference['verified']['placement_json'])
    copied = copy_saved_output(original, directory)
    verified = verify(copied, signature)
    for key in ('metrics', 'placement', 'layout_grid', 'routes_digest'):
        if key in reference['verified'] and verified.get(key) != reference['verified'][key]:
            raise ValueError('Historical off artifact differs from its saved comparison record: ' + key)
    region.check_signature(signature)
    record = deepcopy(reference)
    record.update(verified=verified, reused=True, reuse_kind='historical_compatible_off',
        import_fingerprint=fingerprint, validation_signature=signature,
        historical_baseline=True, fresh_flow_executed=False,
        historical_placement_json=str(original), historical_placement_sha256=common._sha(original),
        compatibility_evidence={'audit_path': audit['_path'], 'audit_sha256': audit['_sha256'],
            'baseline_commit': audit.get('baseline_commit'), 'parameters': audit.get('parameters'),
            'same_case_candidate_states': row['checks'],
            'scope': 'No-deadline initial placement/SA state equivalence; saved historical wires independently revalidated. No claim of identical timed routing.'})
    write_json(directory / 'import-record.json', checked_payload(record))
    print(f'IMPORT historical off {reference["case"]}/{signature["parameters"]["seed"]}: revalidated', flush=True)
    return record


def load_compatible_off(report_path, audit_path, code):
    if report_path is None:
        return {}, None
    audit = read_json(audit_path)
    if audit.get('passed') is not True or audit.get('input_changes') or not audit.get('cases'):
        raise ValueError('Historical off import requires a completed passing compatibility audit')
    expected = {path: digest for path, digest in code.items() if Path(path).parent == ROOT and
                (Path(path).suffix == '.py' or Path(path).name == 'symbol-geometry.json')}
    if audit.get('current_files') != expected:
        raise ValueError('Off-state audit does not describe the current frozen root engine')
    audit.update(_path=str(audit_path.resolve()), _sha256=common._sha(audit_path))
    return report_off_references(report_path), audit


def execute(case, seed, policy, output, parameters, code, frozen, *, retry_failed=False, reuse_only=False,
            historical_off=None, compatibility_audit=None):
    try:
        signature = input_signature(case, seed, policy, parameters, code)
    except (ValueError, OSError, KeyError) as exc:
        unsupported = isinstance(exc, NetlistError) or str(exc).startswith((
            'No supported devices', 'Multiple subcircuits require'))
        return {'case': case['case'], 'seed': seed, 'policy': policy,
            'status': 'skipped' if unsupported else 'failed',
            'reason': 'input_validation_error' if unsupported else 'input_or_resource_error', 'error': str(exc)}
    if any(frozen.get(path) != digest for path, digest in signature['source_files'].items()):
        raise ValueError('Batch input changed before this run')
    region.check_signature(signature)
    if policy == 'off' and historical_off is not None:
        return import_compatible_off(historical_off, compatibility_audit, signature, output)
    fingerprint = common._digest(signature)
    base = output / 'runs' / case['case'] / str(seed) / policy
    for saved in sorted(base.glob('*/run-record.json'), key=lambda p: p.stat().st_mtime, reverse=True):
        record = read_json(saved)
        allowed = ('valid',) if retry_failed else ('valid', 'failed')
        if record.get('fingerprint') != fingerprint or record.get('status') not in allowed:
            continue
        if record['status'] == 'valid':
            record['verified'] = verify(Path(record['verified']['placement_json']), signature)
        record['reused'] = True
        record['reuse_kind'] = 'identical_fingerprint_revalidated'
        print(f'REUSE {case["case"]}/{seed}/{policy}: {record["status"]}', flush=True)
        return record
    if reuse_only:
        return {'case': case['case'], 'seed': seed, 'policy': policy, 'status': 'not_run',
                'reason': 'matching_validated_cache_unavailable', 'fingerprint': fingerprint}
    base.mkdir(parents=True, exist_ok=True)
    directory = base / fingerprint[:16]
    if directory.exists():
        directory = base / (fingerprint[:16] + '-' + uuid.uuid4().hex[:8])
    directory.mkdir()
    command = command_for(signature, directory)
    record = {'format': FORMAT, 'case': case['case'], 'seed': seed, 'policy': policy,
        'signature': signature, 'fingerprint': fingerprint, 'directory': str(directory),
        'command': command, 'status': 'running', 'reused': False}
    write_json(directory / 'run-record.json', record)
    print(f'START {case["case"]}/{seed}/{policy}', flush=True)
    started = perf_counter()
    try:
        with (directory / 'flow.log').open('w', encoding='utf8') as log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            limit = 120 + sum(parameters[key] for key in (
                'quality_seconds', 'rescue_seconds', 'spacing_seconds', 'fallback_seconds', 'simplify_seconds'))
            try:
                record['returncode'] = process.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                common._stop(process)
                raise ValueError('Supervisory timeout; engine output was not marked successful')
        region.check_signature(signature)
        if record['returncode']:
            diagnostic = directory / 'routing-diagnostics.json'
            if diagnostic.is_file():
                record['diagnostics'] = read_json(diagnostic)
            raise ValueError('Flow failed; see flow.log')
        stem = Path(signature['source_netlist']).stem + ('-' + signature['cell'] if signature['cell'] else '')
        record['verified'] = verify(directory / (stem + '-placement.json'), signature)
        record['status'] = 'valid'
    except (ValueError, OSError, KeyError, TypeError, ImportError) as exc:
        record.update(status='failed', error=str(exc))
    record['elapsed_seconds'] = round(perf_counter() - started, 6)
    write_json(directory / 'run-record.json', record)
    print(f'END {case["case"]}/{seed}/{policy}: {record["status"]} {record["elapsed_seconds"]:.2f}s', flush=True)
    return record


def source_gates(old, new):
    """Unavailable source measurements cannot be interpreted as zero errors."""
    counts = old.get('source_count'), new.get('source_count')
    same_count = all(type(value) is int and value >= 0 for value in counts) and counts[0] == counts[1]
    if same_count and counts[0] == 0:
        return {'same_source_count': True, 'source_measurement_available': True,
                'source_errors_nonincreasing': True}
    measured = same_count and all(value.get('source_measurable') is True and all(
        type(value.get(key)) in (int, float) and isfinite(value[key]) and value[key] >= 0
        for key in ('source_errors', 'source_distance')) for value in (old, new))
    return {'same_source_count': same_count, 'source_measurement_available': bool(measured),
            'source_errors_nonincreasing': bool(measured and new['source_errors'] <= old['source_errors'] + 1e-8)}


def gates(old, new, *, policy='passive'):
    a, b = old['metrics'], new['metrics']
    om, nm = old.get('attachment_measurement', {}), new.get('attachment_measurement', {})
    same_plan = bool(old.get('attachment_plan_signature')) and old.get('attachment_plan_signature') == new.get('attachment_plan_signature')
    organization = same_plan
    if om.get('supported') or nm.get('supported'):
        organization = bool(om.get('measurable') and nm.get('measurable')) and all(
            isinstance(om.get(key), (float, int)) and isinstance(nm.get(key), (float, int))
            and nm[key] <= om[key] + 1e-8 for key in ('violations', 'active_order_errors'))
    checks = {'wirelength_115_percent': b['wirelength'] <= 1.15 * a['wirelength'] + 1e-8,
        'crossings_limit': b['crossings'] <= (0 if a['crossings'] == 0 else a['crossings'] + 1),
        'same_measurement_plan': same_plan, 'no_new_structure_violations': organization,
        'same_original_connectivity': bool(old.get('connectivity_digest')) and old.get('connectivity_digest') == new.get('connectivity_digest'),
        'strict_simplification_passed': bool(old.get('strict_simplification_audit', {}).get('valid')) and bool(new.get('strict_simplification_audit', {}).get('valid')),
        'no_new_bus_fallback': new['routing_stage'] != 'bus_fallback' or old['routing_stage'] == 'bus_fallback'}
    if policy == 'all':
        checks.update(source_gates(om, nm))
    return checks


def report_off_references(path):
    if path is None:
        return {}
    data = read_json(path)
    return {(row['case'], int(row.get('seed', DEFAULT_SEEDS[0]))): row['results']['off']
            for row in data.get('cases', ()) if row.get('results', {}).get('off', {}).get('status') == 'valid'}


def off_compatibility(current, reference):
    """An older runtime is evidence only, never silently a fresh engine run."""
    if current.get('status') != 'valid' or reference is None:
        return {'status': 'not_available', 'reason': 'both_valid_off_results_required'}
    a, b = reference['verified'], current['verified']
    old_signature, new_signature = reference.get('signature', {}), current.get('signature', {})
    fields = ('seed', 'iterations', 'quality_seconds', 'rescue_seconds', 'spacing_seconds',
              'fallback_seconds', 'simplify_seconds', 'candidate_id', 'candidate_budget', 'presentation', 'jobs')
    same_parameters = all(old_signature.get('parameters', {}).get(key) ==
                          new_signature.get('parameters', {}).get(key) for key in fields)
    try:
        old_data = read_json(a['placement_json'])
        new_data = read_json(b['placement_json'])
        from sa_saved import load_placement, saved_routing_options
        from sa_routing import validate_final_routes
        with VERIFY_LOCK:
            _, _, par, pl, _, nets, index = load_placement(Path(a['placement_json']))
            _, metrics, validation = validate_final_routes(par, nets, pl, index,
                list(old_data['routes'].items()), options=saved_routing_options(old_data))
        old_valid = bool(validation['valid'] and metrics == old_data['metrics'])
        checks = {'same_source_files': old_signature.get('source_files') == new_signature.get('source_files'),
            'same_execution_parameters': same_parameters, 'old_routes_revalidated': old_valid,
            'same_placement': old_data.get('placement') == new_data.get('placement') and old_data.get('layout_grid') == new_data.get('layout_grid'),
            'same_routes': old_data['routes'] == new_data['routes'], 'same_metrics': old_data['metrics'] == new_data['metrics']}
        return {'status': 'identical' if all(checks.values()) else 'different', 'checks': checks,
                'same_runtime': old_signature.get('code_files') == new_signature.get('code_files'),
                'reference': a['placement_json'], 'reused_as_fresh': False}
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return {'status': 'reference_invalid', 'error': str(exc), 'reused_as_fresh': False}


def run_regressions(output, code):
    folder = output / 'regressions' / (common._digest(code)[:16] + '-' + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    command = [sys.executable, '-X', 'utf8', '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py']
    record = {'command': command, 'code_files': code, 'status': 'running', 'log': str(folder / 'tests.log')}
    write_json(folder / 'record.json', record)
    started = perf_counter()
    with (folder / 'tests.log').open('w', encoding='utf8') as log:
        process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        try:
            record['returncode'] = process.wait(timeout=1800)
        except subprocess.TimeoutExpired:
            common._stop(process)
            record.update(status='failed', error='Regression supervisory timeout')
    text = (folder / 'tests.log').read_text('utf8', errors='replace')
    found = re.search(r'Ran\s+(\d+)\s+tests?\s+in\s+([\d.]+)s', text)
    if found:
        record.update(test_count=int(found.group(1)), reported_seconds=float(found.group(2)))
    record['runtime_unchanged'] = region.runtime_snapshot() == code
    record['elapsed_seconds'] = round(perf_counter() - started, 6)
    if record.get('status') != 'failed':
        record['status'] = 'passed' if record.get('returncode') == 0 and found and record['runtime_unchanged'] else 'failed'
    write_json(folder / 'record.json', record)
    return record


def publish(output, jobs, records, parameters, code, frozen, *, regression=None, compatibility=None):
    policies = parameters['policies']
    rows, sections = [], []
    pairs = {policy: [] for policy in POLICIES[1:]}
    for case, seed in jobs:
        results = {policy: records[(case['case'], seed, policy)] for policy in policies
                   if (case['case'], seed, policy) in records}
        row = {**case, 'seed': seed, 'results': results, 'comparisons': {}}
        for policy in POLICIES[1:]:
            old, new = results.get('off', {}), results.get(policy, {})
            if old.get('status') == new.get('status') == 'valid':
                same_input = old['signature']['source_files'] == new['signature']['source_files']
                checks = gates(old['verified'], new['verified'], policy=policy)
                checks['same_input_version'] = same_input
                row['comparisons'][policy] = checks
                pairs[policy].append((old['verified'], new['verified'], checks))
        if compatibility and (case['case'], seed) in compatibility:
            row['off_compatibility'] = compatibility[(case['case'], seed)]
        figures = [region.figure(output, 'GT（视觉参考）', case['ground_truth'])
                   if case.get('ground_truth') else '<figure>本例没有 GT 图</figure>']
        table, details = [], {}
        for policy in policies:
            result = results.get(policy, {})
            if result.get('status') != 'valid':
                figures.append('<figure>' + escape(policy + ': ' + result.get('status', 'pending') + ' ' + result.get('error', result.get('reason', ''))) + '</figure>')
                continue
            v, m = result['verified'], result['verified']['metrics']
            links = '<p>' + ' · '.join('<a href="' + escape(region.relative_link(output, v[key])) + '">' + label + '</a>'
                for key, label in (('placement_json', 'JSON'), ('color_svg', '彩色图'), ('annotated_svg', '原结构标注')) if key in v) + '</p>'
            figures.append(region.figure(output, policy + ' / strict', v['clean_svg'], links))
            am = v.get('attachment_measurement', {})
            table.append([policy, m['wirelength'], m['crossings'], m['bends'],
                v.get('route_detours', {}).get('maximum_factor'), f'{v["width"]}×{v["height"]}',
                am.get('owner_distance'), am.get('embedded_members'), am.get('active_order_errors'),
                am.get('source_count'), am.get('source_errors'), am.get('source_distance'),
                v.get('selected_candidate'), ','.join(v.get('attempted_candidate_ids', [])),
                v['routing_stage'], result.get('elapsed_seconds'),
                '历史 off 复验' if result.get('historical_baseline') else '缓存复验' if result.get('reused') else '本次运行'])
            details[policy] = {key: v.get(key) for key in ('layout_attachments', 'attachment_measurement',
                'candidate_records', 'route_detours', 'strict_simplification_audit', 'route_simplification', 'recovery_stages')}
        sections.append('<section><h2>' + escape(f'{case["case"]} · seed {seed} · ' + case.get('title', '')) +
            '</h2><div class="figures">' + ''.join(figures) + '</div>' + common._table(
                ['策略', '线长', '交叉', '折角', '最大整网绕行比', '尺寸', '附件距离', '嵌入成员',
                 '有源顺序违规', '已识别源', '源位置违规', '源归属距离', '保留候选', '实际尝试候选', '阶段', '耗时', '来源'], table) +
            '<pre>' + escape(json.dumps(row['comparisons'], ensure_ascii=False, indent=2)) + '</pre>' +
            '<details><summary>实际测量、候选与恢复记录</summary><pre>' + escape(json.dumps(details, ensure_ascii=False, indent=2)) + '</pre></details></section>')
        rows.append(row)
    policy_gates, totals = {}, {}
    for policy in POLICIES[1:]:
        eligible = pairs[policy]
        sums = {side: {metric: sum(pair[j]["metrics"][metric] for pair in eligible)
                       for metric in ('wirelength', 'crossings', 'bends')}
                for j, side in enumerate(('off', policy))}
        totals[policy] = sums
        complete = 'off' in policies and policy in policies and all(
            (case['case'], seed, 'off') in records and (case['case'], seed, policy) in records
            for case, seed in jobs)
        consistent = complete and all(
            records[(case['case'], seed, 'off')]['status'] == records[(case['case'], seed, policy)]['status']
            and records[(case['case'], seed, policy)]['status'] in ('valid', 'skipped')
            for case, seed in jobs)
        policy_gates[policy] = {'complete': complete, 'paired_status_consistent': consistent,
            'has_successful_pairs': bool(eligible),
            'per_case_gates_passed': bool(eligible) and all(all(pair[2].values()) for pair in eligible),
            'total_crossings_nonincreasing': bool(eligible) and sums[policy]['crossings'] <= sums['off']['crossings']}
    counts = {status: sum(row.get('status') == status for row in records.values())
              for status in ('valid', 'skipped', 'failed', 'not_run', 'running')}
    checks = {'all_requested_runs_complete': len(records) == len(jobs) * len(policies) and not counts['not_run'] and not counts['running'],
        'inputs_unchanged': not common._input_changes(frozen),
        'runtime_unchanged': region.runtime_snapshot() == code,
        'no_failed_runs': counts['failed'] == 0}
    if regression is not None:
        checks['full_regression_passed'] = regression.get('status') == 'passed'
    requested = [policy for policy in POLICIES[1:] if policy in policies]
    report = {'format': FORMAT, 'parameters': parameters, 'code_files': code, 'input_files': frozen,
        'expected_runs': len(jobs) * len(policies), 'completed_runs': len(records),
        'valid_runs': counts['valid'], 'skipped_runs': counts['skipped'], 'failed_runs': counts['failed'],
        'not_run': counts['not_run'], 'running_runs': counts['running'],
        'historical_off_imports': sum(bool(row.get('historical_baseline')) for row in records.values()),
        'fresh_flow_runs': sum(row.get('status') in ('valid', 'failed') and not row.get('reused') for row in records.values()),
        'totals_by_off_comparison': totals, 'cases': rows, 'batch_checks': checks,
        'policy_release_gates': policy_gates,
        'quantitative_gates_passed': bool(requested) and all(checks.values()) and all(all(policy_gates[p].values()) for p in requested),
        'full_suite': bool(parameters.get('full_discovered_suite')),
        'full_regression': regression or {'status': 'not_run'}, 'visual_review': 'pending',
        'default_promotion_approved': False, 'ground_truth_electrically_verified': False,
        'crossing_policy': 'Strict shortening throughout. Final per-case crossings: zero stays zero, otherwise off + 1; paired total cannot increase.',
        'off_compatibility': list((compatibility or {}).values())}
    write_json(output / 'comparison.json', report)
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>附件融入布局对照</title>
<style>body{font:14px/1.6 system-ui;margin:24px;background:#f5f6f8}section{background:white;padding:20px;margin:24px 0}.figures{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}figure{margin:0}img{width:100%;height:460px;object-fit:contain}td,th{padding:5px 8px;border-bottom:1px solid #ddd}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>
<h1>附件融入布局：GT / off / passive / all</h1>
<p>三侧使用相同种子、迭代数、时间预算与 strict 安全缩线；其它实验布局关闭。passive 与 all 分别对 off 验收。只重用完全相同指纹并重新验收的缓存，旧版本关闭结果仅作兼容证据。</p>
<p>每个成功结果独立复验原引脚连接、固定出线、几何合法性、两种 SVG 线路及保存后的重绘。附件指标由原网表生成的共同计划重新测量。实际候选列表不包含初排试摆。</p>
<p>发布门槛：逐图线长不超过关闭版 115%；零交叉保持零，其余至多增加 1；同批总交叉不增加，不新增失败或总线降级。GT 只作视觉参考；数值通过不等于视觉通过，也不会自动启用功能。显式候选与小样例探针不作为完整发布验收。</p>'''
    summary = {key: report[key] for key in ('expected_runs', 'completed_runs', 'valid_runs', 'skipped_runs',
        'failed_runs', 'not_run', 'batch_checks', 'policy_release_gates', 'totals_by_off_comparison', 'full_regression')}
    (output / 'index.html').write_text(page + '<pre>' + escape(json.dumps(summary, ensure_ascii=False, indent=2)) + '</pre>' + ''.join(sections) + '</html>', encoding='utf8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', type=Path, default=ROOT / 'output/attachment-integration-comparison')
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--policies', nargs='+', choices=POLICIES, default=list(POLICIES))
    parser.add_argument('--seeds', nargs='+', type=int, default=list(DEFAULT_SEEDS))
    parser.add_argument('--core-seeds', nargs='*', type=int, default=list(DEFAULT_CORE_SEEDS))
    parser.add_argument('--jobs', type=int, choices=range(1, 4), default=2)
    parser.add_argument('--candidate-id', choices=('1', '2', '3'))
    parser.add_argument('--candidate-budget', choices=('sequential', 'reserved'), default='sequential')
    parser.add_argument('--presentation', choices=('legacy', 'clean'), default='clean')
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--reuse-only', action='store_true')
    parser.add_argument('--plan-only', action='store_true')
    parser.add_argument('--status-only', action='store_true')
    parser.add_argument('--off-reference-report', type=Path)
    parser.add_argument('--compatible-off-report', type=Path)
    parser.add_argument('--off-compatibility-audit', type=Path)
    parser.add_argument('--run-regressions', action='store_true')
    parser.add_argument('--regressions-only', action='store_true')
    for name, value in common.DEFAULT_PARAMETERS.items():
        if name not in ('jobs', 'seed'):
            parser.add_argument('--' + name.replace('_', '-'), type=int if name == 'iterations' else float, default=value)
    args = parser.parse_args(argv)
    if bool(args.compatible_off_report) != bool(args.off_compatibility_audit):
        parser.error('--compatible-off-report and --off-compatibility-audit must be supplied together')
    output = args.out_dir.resolve()
    if args.status_only:
        if not (output / 'comparison.json').is_file():
            plan = read_json(output / 'batch-plan.json')
            print(json.dumps({'status': 'planned_not_started', 'expected_runs': plan['expected_runs'],
                'completed_runs': 0, 'parameters': plan['parameters']}, ensure_ascii=False, indent=2))
            return 0
        report = read_json(output / 'comparison.json')
        print(json.dumps({key: report.get(key) for key in ('expected_runs', 'completed_runs', 'valid_runs',
            'skipped_runs', 'failed_runs', 'not_run', 'policy_release_gates')}, ensure_ascii=False, indent=2))
        return 0
    if args.iterations < 0:
        parser.error('iterations must be non-negative')
    for name in ('quality_seconds', 'rescue_seconds', 'spacing_seconds', 'fallback_seconds', 'simplify_seconds'):
        value = getattr(args, name)
        if not isfinite(value) or value < 0 or value == 0 and name in ('quality_seconds', 'fallback_seconds'):
            parser.error(name + ' must be finite and positive, or non-negative for optional stages')
    policies = [policy for policy in POLICIES if policy in args.policies]
    cases = common.discover_cases()
    if args.cases:
        missing = set(args.cases) - {case['case'] for case in cases}
        if missing:
            parser.error('Unknown cases: ' + ', '.join(sorted(missing)))
        cases = [case for case in cases if case['case'] in args.cases]
    jobs = schedule(cases, args.seeds, args.core_seeds)
    parameters = {key: getattr(args, key) for key in common.DEFAULT_PARAMETERS if key != 'seed'}
    parameters.update(seeds=args.seeds, core_seeds=args.core_seeds, policies=policies,
        candidate_id=args.candidate_id, candidate_budget=args.candidate_budget,
        presentation=args.presentation, measurement_policy='all' if 'all' in policies else 'passive',
        full_discovered_suite=args.cases is None and args.candidate_id is None
            and set(DEFAULT_SEEDS) <= set(args.seeds) and set(DEFAULT_CORE_SEEDS) <= set(args.core_seeds))
    output.mkdir(parents=True, exist_ok=True)
    code, frozen = region.runtime_snapshot(), common._freeze_inputs(cases)
    historical_off, compatibility_audit = load_compatible_off(args.compatible_off_report,
        args.off_compatibility_audit, code)
    if args.compatible_off_report:
        frozen[str(args.compatible_off_report.resolve())] = common._sha(args.compatible_off_report)
        frozen[str(args.off_compatibility_audit.resolve())] = common._sha(args.off_compatibility_audit)
    plan = {'format': FORMAT, 'parameters': parameters, 'case_count': len(cases),
        'case_seed_pairs': len(jobs), 'expected_runs': len(jobs) * len(policies),
        'cases': [{**case, 'seed': seed} for case, seed in jobs],
        'code_files': code, 'input_files': frozen,
        'serial_budget_hours': len(jobs) * len(policies) * sum(parameters[key] for key in (
            'quality_seconds', 'rescue_seconds', 'spacing_seconds', 'fallback_seconds', 'simplify_seconds')) / 3600}
    write_json(output / 'batch-plan.json', plan)
    if args.plan_only:
        print(json.dumps({key: plan[key] for key in ('case_count', 'case_seed_pairs', 'expected_runs', 'serial_budget_hours', 'parameters')}, ensure_ascii=False, indent=2))
        return 0
    regression = run_regressions(output, code) if args.run_regressions or args.regressions_only else None
    if args.regressions_only:
        print(json.dumps(regression, ensure_ascii=False, indent=2))
        return int(regression['status'] != 'passed')
    records, compatibility = {}, {}
    references = report_off_references(args.off_reference_report)
    publish(output, jobs, records, parameters, code, frozen, regression=regression)
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(execute, case, seed, policy, output, parameters, code, frozen,
            retry_failed=args.retry_failed, reuse_only=args.reuse_only,
            historical_off=historical_off.get((case['case'], seed)) if policy == 'off' else None,
            compatibility_audit=compatibility_audit): (case, seed, policy)
            for case, seed in jobs for policy in policies}
        for future in as_completed(futures):
            case, seed, policy = futures[future]
            try:
                record = future.result()
            except Exception as exc:
                record = {'case': case['case'], 'seed': seed, 'policy': policy, 'status': 'failed', 'error': str(exc)}
            records[(case['case'], seed, policy)] = record
            if policy == 'off' and args.off_reference_report:
                compatibility[(case['case'], seed)] = off_compatibility(record, references.get((case['case'], seed)))
            publish(output, jobs, records, parameters, code, frozen, regression=regression, compatibility=compatibility)
    return int(any(row['status'] in ('failed', 'not_run') for row in records.values()) or
               regression is not None and regression['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
