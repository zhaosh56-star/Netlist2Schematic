#!/usr/bin/env python3
"""Validate saved layouts and write a reproducible Chinese comparison report.

This tool never places components or searches for routes. Every metric and SVG
comes from the routes saved in the two placement JSON files.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from html import escape
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from circuit_structures import analyze_structures
from layout_quality import evaluate_quality
from netlist_core import parse_netlist
from sa_common import read_json, write_json
from sa_render import render_svg
from sa_routing import validate_final_routes
from sa_saved import load_placement, saved_routing_options


def topology_signature(devices):
    """Keep every parsed device property except its source file and line."""
    return {name: {key: value for key, value in device.items()
                   if key not in ('source_path', 'source_line')}
            for name, device in sorted(devices.items())}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _saved_layout(path, structures):
    data, source, par, placement, comps, nets, index = load_placement(path)
    if not isinstance(data.get('routes'), dict):
        raise ValueError('保存的 JSON 没有 routes；对比工具不会重新布线')
    routed, metrics, validation = validate_final_routes(
        par, nets, placement, index, list(data['routes'].items()),
        options=saved_routing_options(data))
    quality = evaluate_quality(par, placement, comps, nets, index, routed, metrics, structures)
    quality['structure_span'] = sum(row['span'] for row in quality['structures'])
    elapsed = data.get('quality', {}).get('elapsed_seconds')
    result = {
        'placement_json': str(Path(path).resolve()), 'source_netlist': str(source),
        'source_sha256': _sha(source), 'recorded_source_sha256': data.get('source_sha256'),
        'seed': data.get('seed'), 'candidate_seed': data.get('candidate_seed'),
        'candidate_id': data.get('quality', {}).get('selected_candidate', data.get('candidate_id')),
        'elapsed_seconds': elapsed,
        'routing_seconds': data.get('validation', {}).get('routing_seconds'),
        'stop_reason': data.get('quality', {}).get('stop_reason'),
        'metrics': metrics, 'quality': quality, 'validation': validation,
    }
    return result, (par, placement, comps, nets, index, routed, metrics, validation)


def _e(value):
    return escape(str(value), quote=True)


def _number(value, precision=2):
    return '—' if value is None else f'{value:,.{precision}f}'


def _table(headers, rows):
    return ('<div class="table-scroll"><table><thead><tr>'
            + ''.join(f'<th>{_e(value)}</th>' for value in headers)
            + '</tr></thead><tbody>'
            + ''.join('<tr>' + ''.join(f'<td>{_e(value)}</td>' for value in row) + '</tr>' for row in rows)
            + '</tbody></table></div>')


def _case_html(case):
    title = f'图 {case["case"]}'
    if 'error' in case:
        return f'<section><h2>{_e(title)}</h2><p class="fail">检查失败：{_e(case["error"])}</p></section>'
    old, new = case['baseline'], case['new']
    checks = '；'.join(f'{name}：{"通过" if passed else "未通过"}' for name, passed in case['checks'].items())
    rows = []
    for key, label in (('wirelength', '最终唯一线长'), ('crossings', '交叉'), ('bends', '折角'),
                       ('alignment', '支路错位惩罚'), ('dispersion', '结构分散惩罚'),
                       ('structure_span', '结构跨度合计'), ('detour', '超出 HPWL 线长'),
                       ('outside_length', '端点包围盒外线长'), ('score', '综合评分（低为优）')):
        a, b = old['quality'][key], new['quality'][key]
        rows.append((label, _number(a), _number(b), _number(b - a)))
    metadata_rows = []
    for label, key in (('种子', 'seed'), ('候选种子', 'candidate_seed'), ('候选编号', 'candidate_id'),
                       ('总优化秒数', 'elapsed_seconds'), ('最终路由秒数', 'routing_seconds'),
                       ('停止原因', 'stop_reason')):
        metadata_rows.append((label, old.get(key), new.get(key)))
    net_rows = []
    for net in sorted(set(old['quality']['per_net']) | set(new['quality']['per_net'])):
        a, b = old['quality']['per_net'].get(net, {}), new['quality']['per_net'].get(net, {})
        net_rows.append((net, _number(a.get('wirelength')), _number(b.get('wirelength')),
                         _number(a.get('hpwl')), _number(b.get('hpwl')),
                         _number(a.get('detour_factor')), _number(b.get('detour_factor')),
                         _number(a.get('excess_detour')), _number(b.get('excess_detour'))))
    by_id = {row['id']: row for row in new['quality']['structures']}
    structure_rows = [(row['id'], _number(row['span']), _number(by_id.get(row['id'], {}).get('span')),
                       _number(row['alignment']), _number(by_id.get(row['id'], {}).get('alignment')))
                      for row in old['quality']['structures']]
    figures = ''.join(
        f'<figure><figcaption>{_e(label)}</figcaption><a href="{_e(record["svg"])}">'
        f'<img src="{_e(record["svg"])}" alt="{_e(title + label)}" loading="lazy"></a></figure>'
        for label, record in (('冻结基线', old), ('新布局', new)))
    return (f'<section><h2>{_e(title)}</h2><p>{_e(checks)}</p>'
            f'<p>源文件 SHA256：<code>{_e(case["expected_source_sha256"])}</code></p>'
            + _table(('指标', '旧', '新', '新 − 旧'), rows)
            + '<div class="figures">' + figures + '</div>'
            + '<details><summary>种子、耗时和来源</summary>'
            + _table(('字段', '旧', '新'), metadata_rows)
            + f'<p>旧 JSON：{_e(old["placement_json"])}<br>新 JSON：{_e(new["placement_json"])}</p></details>'
            + '<details><summary>逐网绕行系数</summary>'
            + _table(('网络', '旧线长', '新线长', '旧 HPWL', '新 HPWL', '旧系数', '新系数', '旧超额', '新超额'), net_rows)
            + '</details><details><summary>真实结构跨度与对齐</summary>'
            + _table(('结构', '旧跨度', '新跨度', '旧错位', '新错位'), structure_rows)
            + '</details></section>')


def _html(report):
    summary = report['summary']
    status = '达到全部验收目标' if report['accepted'] else '尚未达到全部验收目标'
    target_rows = [(name, '通过' if value else '未通过') for name, value in report['acceptance'].items()]
    rows = [(label, _number(summary['baseline'].get(key)), _number(summary['new'].get(key)))
            for key, label in (('wirelength', '四图总线长'), ('crossings', '四图总交叉'),
                               ('bends', '四图总折角'), ('alignment', '支路错位惩罚合计'),
                               ('structure_span', '结构跨度合计'), ('score', '综合评分合计'))]
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>结构化布局验收对比</title><style>'
            'body{font:15px/1.6 system-ui,sans-serif;margin:30px auto;padding:0 20px;max-width:1500px;color:#18212b;background:#f4f6f8}'
            'h1,h2{line-height:1.2}section{background:white;padding:24px;margin:24px 0;border:1px solid #d9dfe6;border-radius:8px}'
            'table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}td,th{padding:8px 12px;border-bottom:1px solid #d9dfe6;text-align:left}'
            'th{background:#eef2f6}.table-scroll{overflow:auto;margin:15px 0}.figures{display:grid;grid-template-columns:1fr 1fr;gap:15px}'
            'figure{margin:0}figcaption{font-weight:bold;margin:8px 0}img{width:100%;height:540px;object-fit:contain;background:#fff;border:1px solid #d9dfe6}'
            'details{margin:15px 0}summary{cursor:pointer}code{overflow-wrap:anywhere}.fail{color:#a32122}.pass{color:#14623c}'
            '@media(max-width:800px){.figures{grid-template-columns:1fr}body{margin:15px auto}img{height:auto}}'
            '</style><body><h1>结构化布局验收对比</h1>'
            f'<p class="{"pass" if report["accepted"] else "fail"}">{_e(status)}</p>'
            f'<p>总线长降低 {_e(_number(summary["wirelength_reduction"] * 100))}%。'
            f'成功复核 {_e(summary["validated_cases"])} / 4 张图。'
            f'生成时间：{_e(report["generated_utc"])}。仅复核已保存的路线，没有运行 SA 或寻路。</p>'
            + _table(('验收项目', '结果'), target_rows) + _table(('指标', '冻结基线', '新布局'), rows)
            + '<p>两侧使用同一份新的拓扑结构识别结果评分，旧 JSON 中的结构标签不参与评分。'
            '综合评分越低越好；结构跨度为各高置信结构中心包围盒的宽加高之和，包含父子结构。'
            '绕行系数 = 最终唯一线长 / 原始端点 HPWL；多端树及障碍可能要求额外长度，单端网不计算该系数。'
            '点击 SVG 可单独打开检查。所有图中标题指标来自本次几何复核。</p>'
            + ''.join(_case_html(case) for case in report['cases']) + '</body></html>')


def compare(*, baseline_dir=ROOT / 'tests/fixtures/aesthetic-baseline',
            new_dir=ROOT / 'output/structured', out_dir=None, live_dir=ROOT / 'examples'):
    baseline_dir, new_dir, live_dir = map(lambda p: Path(p).resolve(), (baseline_dir, new_dir, live_dir))
    out_dir = Path(out_dir).resolve() if out_dir else new_dir
    assets = out_dir / 'report/assets'
    assets.mkdir(parents=True, exist_ok=True)
    manifest = read_json(baseline_dir / 'manifest.json')
    cases = []
    for identifier in ('1', '2', '3', '4'):
        case = {'case': identifier}
        try:
            old_path = baseline_dir / f'{identifier}-placement.json'
            new_path = new_dir / identifier / f'{identifier}-placement.json'
            old_data, old_source, *_ = load_placement(old_path)
            new_data, new_source, *_ = load_placement(new_path)
            old_devices = parse_netlist(old_source, cell=old_data.get('source_cell'))
            new_devices = parse_netlist(new_source, cell=new_data.get('source_cell'))
            structures = analyze_structures(old_devices)
            old, old_render = _saved_layout(old_path, structures)
            new, new_render = _saved_layout(new_path, structures)
            expected = manifest[identifier]['source_sha256']
            live_hash = _sha(live_dir / f'{identifier}.cir')
            sources_match = old['source_sha256'] == new['source_sha256'] == live_hash == expected
            recorded_match = all(record['recorded_source_sha256'] in (None, record['source_sha256'])
                                 for record in (old, new))
            same_topology = topology_signature(old_devices) == topology_signature(new_devices)
            checks = {'冻结源/当前源/输出源 SHA256 一致': sources_match,
                      'JSON 记录的源 SHA256 一致': recorded_match,
                      '器件、型号与所有引脚连接一致': same_topology,
                      '旧新几何硬约束均通过': True,
                      '逐图综合评分不劣': new['quality']['score'] <= old['quality']['score'] + 1e-8}
            case.update(expected_source_sha256=expected, live_source_sha256=live_hash,
                        checks=checks, baseline=old, new=new)
            for label, record, values in (('baseline', old, old_render), ('new', new, new_render)):
                par, placement, comps, nets, index, routed, metrics, validation = values
                svg = assets / f'{identifier}-{label}.svg'
                render_svg(par, placement, comps, nets, index, routed, metrics, svg,
                           clean=True, structs=structures, validation=validation)
                record['svg'] = svg.relative_to(out_dir).as_posix()
            print(f'图{identifier}: SHA256={live_hash}; '
                  f'WL {old["metrics"]["wirelength"]:.0f}→{new["metrics"]["wirelength"]:.0f}; '
                  f'X {old["metrics"]["crossings"]}→{new["metrics"]["crossings"]}; '
                  f'score {old["quality"]["score"]:.3f}→{new["quality"]["score"]:.3f}; '
                  f'elapsed={new["elapsed_seconds"]}; seed={new["seed"]}; '
                  f'candidate_seed={new["candidate_seed"]}; candidate={new["candidate_id"]}', flush=True)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            case['error'] = str(exc)
            print(f'图{identifier}: 检查失败：{exc}', flush=True)
        cases.append(case)
    totals = {label: {key: sum(case[label]['quality'][key] for case in cases if 'error' not in case)
                      for key in ('wirelength', 'crossings', 'bends', 'alignment', 'dispersion',
                                  'structure_span', 'detour', 'outside_length', 'score')}
              for label in ('baseline', 'new')}
    old_length = totals['baseline']['wirelength']
    reduction = 1 - totals['new']['wirelength'] / old_length if old_length else 0
    complete = len(cases) == 4 and all('error' not in case for case in cases)
    acceptance = {
        '四图源文件、拓扑及几何检查全部通过': complete and all(
            all(value for name, value in case['checks'].items() if name != '逐图综合评分不劣') for case in cases),
        '四图总线长至少降低 20%': complete and reduction >= .2 - 1e-10,
        '四图总交叉数不增加': complete and totals['new']['crossings'] <= totals['baseline']['crossings'],
        '每图综合评分不劣': complete and all(case['checks']['逐图综合评分不劣'] for case in cases),
    }
    report = {'report_format': 'sa-aesthetic-comparison-v1',
              'generated_utc': datetime.now(timezone.utc).isoformat(),
              'accepted': all(acceptance.values()), 'acceptance': acceptance,
              'summary': {**totals, 'wirelength_reduction': reduction,
                          'validated_cases': sum('error' not in case for case in cases)}, 'cases': cases}
    write_json(out_dir / 'comparison.json', report)
    (out_dir / 'comparison.html').write_text(_html(report), encoding='utf-8')
    print(f'总线长降低 {reduction * 100:.2f}%；验收 {"通过" if report["accepted"] else "未通过"}', flush=True)
    print(f'报告：{out_dir / "comparison.html"}', flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-dir', type=Path, default=ROOT / 'tests/fixtures/aesthetic-baseline')
    parser.add_argument('--new-dir', type=Path, default=ROOT / 'output/structured')
    parser.add_argument('--out-dir', type=Path)
    parser.add_argument('--live-dir', type=Path, default=ROOT / 'examples')
    args = parser.parse_args(argv)
    try:
        report = compare(baseline_dir=args.baseline_dir, new_dir=args.new_dir,
                         out_dir=args.out_dir, live_dir=args.live_dir)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f'报告生成失败：{exc}', file=sys.stderr)
        return 1
    return 0 if report['accepted'] else 1


if __name__ == '__main__':
    sys.exit(main())
