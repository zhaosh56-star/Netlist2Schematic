#!/usr/bin/env python3
"""Export a read-only GT/baseline/calibrated/branch recognition gallery.

This command does not create a placement or call SA, A*, recovery or route
simplification. SVGs in the gallery are existing visual references only.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from html import escape
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
FORMAT = 'layout-evidence-recognition-gallery-v1'


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _code_snapshot():
    paths = list(ROOT.glob('*.py')) + [Path(__file__).resolve(), ROOT / 'symbol-geometry.json']
    return {str(p): _sha(p) for p in sorted(paths)}


def _changes(files):
    return [name for name, expected in files.items()
            if not Path(name).is_file() or _sha(name) != expected]


def _summary(structures, relations):
    return {'structures': len(structures),
            'structure_kinds': dict(sorted(Counter(r['kind'] for r in structures).items())),
            'high_multidevice': sum(r.get('confidence') == 'high' and
                len(r.get('devices', r.get('members', ()))) >= 2 for r in structures),
            'auxiliary_relations': sum(r.get('confidence') == 'relation' for r in structures),
            'layout_relations': len(relations),
            'layout_kinds': dict(sorted(Counter(r['kind'] for r in relations).items()))}


def _historic_index():
    """Link saved images without rerunning their electrical verification."""
    candidates = [ROOT / 'output/structure-layout-comparison/comparison.json',
                  ROOT / 'output/structure-layout-comparison/first-pass-comparison.json',
                  ROOT / 'output/symmetry-comparison/comparison.json']
    found = {}
    for path in candidates:
        if not path.is_file():
            continue
        try:
            document = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            continue
        for case in document.get('cases', ()):
            if case['case'] in found:
                continue
            for policy in ('off', 'auto'):
                record = case.get('results', {}).get(policy, {})
                verified = record.get('verified', {})
                image = Path(verified.get('clean_svg', ''))
                signature = record.get('signature', {})
                sources = signature.get('source_files', {})
                if record.get('status') != 'valid' or not image.is_file() or not sources:
                    continue
                found[case['case']] = {'clean_svg': str(image),
                    'placement_json': verified.get('placement_json'), 'comparison_file': str(path),
                    'policy': policy, 'source_netlist': signature.get('source_netlist'),
                    'source_files': sources, 'source_files_unchanged': not _changes(sources),
                    'original_code_files': signature.get('code_files', {}),
                    'original_record_status': record.get('status'),
                    'revalidated_in_this_export': False}
                break
    return found


def _bridge_evidence(devices, comps, index, relations):
    # Pure graph/row planning supplies existing strict R/L path evidence. No
    # placement object is initialized and none of these rows are drawn/applied.
    from branch_levels import plan_branch_rows
    from device_leveling import compute_levels
    from sa_common import load_local
    legacy = load_local('recognition_gallery_layered', 'place-layered.py')
    levels = compute_levels(devices)
    conduction = {net for dev in devices.values() if dev['cls'] in ('MOS', 'BJT')
                  for field in ('d', 's', 'c', 'e') if (net := dev.get(field)) is not None}
    attached = legacy.attach_info(devices, levels['net_levels'], conduction)
    rows, _ = legacy._allocate_rows(devices, comps, index, levels['net_levels'],
                                    levels['device_levels'], attached)
    _, plan = plan_branch_rows(devices, levels, rows, relations, comps, index)
    return {'bridge_alignment': plan['bridge_alignment'],
            'skipped_bridges': plan['skipped_bridges'],
            'topology_only': True, 'rows_applied': False}


def _recognize(case):
    import compare_symmetry_layouts as common
    from circuit_structures import analyze_structures
    from layout_evidence import build_layout_evidence, extend_branch_evidence
    from layout_relations import build_layout_relations
    from netlist_core import NetlistError, parse_netlist, to_document
    from presentation import prepare_presentation

    source = common._resolve(case['netlist'])
    record = {**case, 'source_netlist': str(source)}
    try:
        cell = common._select_cell(source, case.get('cell'))
        devices = parse_netlist(source, cell=cell)
    except (NetlistError, ValueError, OSError) as exc:
        return {**record, 'status': 'skipped', 'reason': 'input_validation_error', 'error': str(exc)}
    try:
        baseline = analyze_structures(devices)
        before = deepcopy((devices, baseline))
        doc = to_document(devices)
        comps, nets = doc['components'], doc['nets']
        index = {c['id']: i for i, c in enumerate(comps)}
        ports = prepare_presentation(source, cell, comps, nets)['named_ports']
        base_relations = build_layout_relations(devices, baseline)
        calibrated = build_layout_evidence(devices, baseline, formal_ports=ports)
        bridge = _bridge_evidence(devices, comps, index, calibrated['relations'])
        extended = extend_branch_evidence(devices, calibrated,
                                          bridge_alignment=bridge['bridge_alignment'])
        # Also exercise the common owner resolver, not an ad-hoc report-only
        # ownership rule. It remains a read-only relationship calculation.
        resolved = build_layout_relations(devices, validated_evidence=extended)
        if (devices, baseline) != before:
            raise ValueError('Evidence analysis mutated canonical input or baseline structures')
        return {**record, 'status': 'recognized', 'source_cell': cell,
            'component_count': len(devices), 'formal_ports': ports,
            'baseline': {'structures': baseline, 'relations': base_relations,
                         'summary': _summary(baseline, base_relations)},
            'calibrated': {**calibrated,
                'summary': _summary(calibrated['structures'], calibrated['relations'])},
            'extended': {**extended, 'resolved_relations': resolved,
                'summary': _summary(extended['structures'], resolved)},
            'strict_RL_evidence': bridge, 'baseline_unchanged': True,
            'recognition_applied_to_layout': False,
            'SA_called': False, 'Astar_called': False}
    except Exception as exc:
        return {**record, 'status': 'error', 'reason': 'recognition_export_error', 'error': str(exc)}


def _url(output, path):
    return quote(os.path.relpath(Path(path), output).replace('\\', '/'), safe='/.:')


def _table(headers, rows):
    return '<table><thead><tr>'+''.join('<th>'+escape(str(h))+'</th>' for h in headers)+\
           '</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+escape(str(v))+'</td>'
           for v in row)+'</tr>' for row in rows)+'</tbody></table>'


def _figure(output, label, path):
    if not path or not Path(path).is_file():
        return '<figure><figcaption>'+escape(label)+'</figcaption><p>无可用图像</p></figure>'
    link = escape(_url(output, path), quote=True)
    return '<figure><figcaption>'+escape(label)+'</figcaption><a href="'+link+\
           '"><img loading="lazy" src="'+link+'" alt="'+escape(label, quote=True)+'"></a></figure>'


def _details(label, value):
    return '<details><summary>'+escape(label)+'</summary><pre>'+\
           escape(json.dumps(value, ensure_ascii=False, indent=2))+'</pre></details>'


def _html(report, output):
    summary = report['summary']
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>子结构识别证据对照</title><style>',
        'body{font:15px/1.6 system-ui,sans-serif;margin:24px;background:#f5f6f8;color:#18202a}',
        'section{background:white;border:1px solid #d7dde5;border-radius:10px;margin:24px 0;padding:18px}',
        'table{border-collapse:collapse;width:100%;margin:12px 0}td,th{border:1px solid #d7dde5;padding:6px;text-align:left;vertical-align:top}',
        'figure{margin:0;padding:10px;border:1px solid #ddd;min-width:0}img{width:100%;height:360px;object-fit:contain;background:white}',
        '.images{display:grid;grid-template-columns:1fr 1fr;gap:14px}.note{background:#fff4d8;padding:14px}',
        'pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.45 monospace}summary{cursor:pointer;font-weight:600}',
        '@media(max-width:700px){.images{grid-template-columns:1fr}}',
        '</style><h1>子结构识别证据对照</h1>',
        '<p class="note">此页只做识别分析：未运行SA、A*、恢复链或布线简化，未生成新的布局图。',
        '左图GT仅作视觉参考，右图是历史生成图，不代表校准或扩展已用于该图。',
        '类别计数包含重叠与父子记录；high是算法置信标签，不是人工准确率或功能仿真结论。</p>',
        '<p>案例 '+str(summary['cases'])+'；成功识别 '+str(summary['recognized'])+'；原输入跳过 '+
        str(summary['skipped'])+'；分析错误 '+str(summary['errors'])+'。<a href="comparison.json">下载完整JSON与哈希</a></p>',
        '<p>输入保持不变：'+str(report['inputs_unchanged'])+'；分析代码保持不变：'+str(report['code_unchanged'])+'</p>']
    for record in report['cases']:
        parts.append('<section id="case-'+escape(record['case'], quote=True)+'"><h2>'+escape(
            record['case']+' '+record.get('title', ''))+'</h2><p>'+escape(record['status'])+
            ' · <a href="'+escape(_url(output, record['source_netlist']), quote=True)+'">源网表</a></p>')
        history = record.get('historical_result') or {}
        parts.append('<div class="images">'+_figure(output, 'GT：视觉参考', record.get('ground_truth'))+
            _figure(output, '历史生成图：'+history.get('policy', '无')+'，本次未重布线', history.get('clean_svg'))+'</div>')
        if record['status'] != 'recognized':
            parts.append('<p>'+escape(record.get('error', ''))+'</p></section>')
            continue
        rows = []
        for key, label in (('baseline', '原识别'), ('calibrated', '阶段二：证据校准'), ('extended', '阶段三：支路扩展')):
            s = record[key]['summary']
            rows.append([label, s['structures'], s['high_multidevice'], s['auxiliary_relations'],
                json.dumps(s['structure_kinds'], ensure_ascii=False),
                json.dumps(s['layout_kinds'], ensure_ascii=False)])
        parts.append(_table(['视图', '识别记录', 'high且多器件', '辅助关系', '结构kind计数', '布局关系kind计数'], rows))
        parts.append(_details('歧义、降级、共享/专属尾源与供电证据', {
            'diagnostics': record['calibrated']['diagnostics'],
            'net_roles': record['calibrated']['net_roles'],
            'branch_extension': record['extended'].get('branch_evidence'),
            'recognition_applied_to_layout': False}))
        for key, label in (('baseline', '原识别角色'), ('calibrated', '校准角色'), ('extended', '扩展及所有权角色')):
            parts.append(_details(label, {'structures': record[key]['structures'],
                'relations': record[key].get('resolved_relations', record[key]['relations'])}))
        parts.append(_details('输入哈希、归一化来源及历史图版本', {
            'source_files': record.get('source_files'), 'provenance': record.get('provenance'),
            'historical_result': history}))
        parts.append('</section>')
    return ''.join(parts)+'</html>'


def export(output, *, selected=None):
    code = _code_snapshot()
    import compare_symmetry_layouts as common
    cases = common.discover_cases()
    if selected:
        wanted = set(selected)
        unknown = wanted - {case['case'] for case in cases}
        if unknown:
            raise ValueError('Unknown cases: '+', '.join(sorted(unknown)))
        cases = [case for case in cases if case['case'] in wanted]
    frozen = common._freeze_inputs(cases)
    historical = _historic_index()
    records = []
    for case in cases:
        record = _recognize(case)
        source = Path(record['source_netlist'])
        try:
            facts, extra = common._provenance(case)
            record['source_files'] = {**common._source_files(source), **extra}
            record['provenance'] = facts
        except (ValueError, OSError) as exc:
            record['source_files'] = {str(source): _sha(source)} if source.is_file() else {}
            record['provenance_error'] = str(exc)
            if record['status'] == 'recognized':
                record.update(status='error', error=str(exc), reason='provenance_validation_error')
        record['historical_result'] = historical.get(case['case'])
        if record.get('ground_truth'):
            record['ground_truth'] = str(common._resolve(record['ground_truth']))
        records.append(record)
    input_changes, code_changes = _changes(frozen), _changes(code)
    report = {'format': FORMAT, 'created_at': datetime.now(timezone.utc).isoformat(),
        'parameters': {'cases': [c['case'] for c in cases], 'full_discovered_suite': selected is None},
        'SA_called': False, 'Astar_called': False, 'generated_new_layout_images': False,
        'validated_full_route': False, 'ground_truth_electrically_verified': False,
        'accuracy_measured': False, 'code_files': code, 'input_files': frozen,
        'input_changes': input_changes, 'code_changes': code_changes,
        'inputs_unchanged': not input_changes, 'code_unchanged': not code_changes,
        'summary': {'cases': len(records), 'recognized': sum(r['status'] == 'recognized' for r in records),
                    'skipped': sum(r['status'] == 'skipped' for r in records),
                    'errors': sum(r['status'] == 'error' for r in records)}, 'cases': records}
    output = Path(output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / 'comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    (output / 'index.html').write_text(_html(report, output), encoding='utf-8')
    return report


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', type=Path,
        default=ROOT / 'output/structure-incremental-comparison/recognition')
    parser.add_argument('--cases', nargs='+', help='案例编号，例如 1 7 data03 data21；省略时导出全部32例')
    args = parser.parse_args()
    report = export(args.out_dir, selected=args.cases)
    print(json.dumps({'output': str(args.out_dir), **report['summary'],
        'inputs_unchanged': report['inputs_unchanged'], 'code_unchanged': report['code_unchanged'],
        'SA_called': False, 'Astar_called': False}, ensure_ascii=False))
    return 0 if report['summary']['errors'] == 0 and report['inputs_unchanged'] and report['code_unchanged'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
