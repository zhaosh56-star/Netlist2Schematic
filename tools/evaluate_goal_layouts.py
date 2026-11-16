#!/usr/bin/env python3
"""Small paired goal pilots; classify outcomes without a dataset veto gate.

GT images are linked only for human inspection. The engine receives the cir,
never the GT image. This intentionally does not launch the full dataset flow.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import redirect_stdout
from hashlib import sha256
from html import escape
import io
import json
from math import isfinite
from pathlib import Path
from statistics import mean, median
import subprocess
import sys
from time import perf_counter
import warnings
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare_symmetry_layouts import discover_cases, _select_cell
from compare_attachment_integration import build_measurement_plan
from compare_region_layouts import build_measurement_plan as build_region_measurement_plan
from netlist2schematic.common.sa_common import engine_source_files, configure_resources, resource_root
from netlist2schematic.common.sa_saved import load_placement, saved_routing_options
from netlist2schematic.netlist.netlist_core import parse_netlist
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.routing.sa_routing import validate_final_routes
from netlist2schematic.rendering.render_clean import render
from netlist2schematic.plugins.attachment.attachment_integration import measure_attachments


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf8')


def inventory(cases):
    rows = []
    for case in cases:
        source = Path(case['netlist'])
        row = dict(case=case['case'], source=str(source), ground_truth=case.get('ground_truth'))
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                cell = _select_cell(source, case.get('cell'))
                devices = parse_netlist(source, cell=cell)
                from netlist2schematic.netlist.netlist_core import to_document
                document = to_document(devices)
                comps,nets = document['components'],document['nets']
                index = {c['id']:i for i,c in enumerate(comps)}
                plan = build_measurement_plan(source,cell,comps,nets,index,'all')
            rows.append(dict(row, status='parsed', cell=cell, devices=len(devices),
                device_classes=dict(Counter(d['cls'] for d in devices.values())),
                structures=dict(Counter(s['kind'] for s in analyze_structures(devices))),
                layout_evidence=dict(passive_groups=len(plan['groups']),
                    host_access_links=len(plan['host_access']['links']),
                    skipped_host_roles=len(plan['host_access']['skipped']),
                    paired_load_relations=len(plan['paired_banks']),
                    source_groups=len(plan['source_groups'])),
                source_sha256=sha256(source.read_bytes()).hexdigest(),
                warnings=sorted(set(str(w.message) for w in caught))))
        except (OSError, ValueError) as exc:
            rows.append(dict(row, status='input_error', error=str(exc)))
    return rows


def verify(path):
    with warnings.catch_warnings(record=True), redirect_stdout(io.StringIO()):
        data, source, par, pl, comps, nets, index = load_placement(path)
        _, metrics, validation = validate_final_routes(
            par, nets, pl, index, list(data['routes'].items()), options=saved_routing_options(data))
        plan = build_measurement_plan(source, data.get('source_cell'), comps, nets, index, 'all')
        organization = measure_attachments(pl, comps, index, plan)
        from netlist2schematic.plugins.region.region_layout import measure_regions
        from netlist2schematic.plugins.region.region_controls import measure_saved_controls
        from netlist2schematic.plugins.region.region_route_ports import measure_saved_paired_controls
        devices=parse_netlist(source,cell=data.get('source_cell'))
        region_plan=build_region_measurement_plan(source,data.get('source_cell'),devices,
            analyze_structures(devices),comps,nets,index)
        regions=measure_regions(pl,comps,index,region_plan)
        controls=measure_saved_controls(pl,comps,index,region_plan)
        paired_controls=measure_saved_paired_controls(pl,comps,index,region_plan)
    if not validation['valid']:
        raise ValueError('complete route validation failed')
    if data['metrics'] != metrics:
        raise ValueError('saved metrics differ from independently verified routes')
    stem=path.name.removesuffix('-placement.json')
    drawings=[path.with_name(stem+'-layered.svg'),path.with_name(stem+'-clean.svg')]
    visible=[]
    canvas=None
    for drawing in drawings:
        root=ET.parse(drawing).getroot()
        if drawing==drawings[-1]:
            viewbox=[float(v) for v in root.attrib['viewBox'].split()]
            canvas=dict(width=viewbox[2],height=viewbox[3])
        visible.append(sorted((e.attrib['data-net'],e.attrib['points']) for e in root.iter()
            if e.tag=='{http://www.w3.org/2000/svg}polyline' and 'data-net' in e.attrib))
        ids=[e.attrib['data-component'] for e in root.iter() if 'data-component' in e.attrib]
        if sorted(ids) != sorted(c['id'] for c in comps):
            raise ValueError('SVG component set differs from source')
    if visible[0] != visible[1]:
        raise ValueError('colored and clean SVG route geometry differs')
    return dict(metrics=metrics, organization=organization, regions=regions, control_networks=controls,
        paired_controls=paired_controls,
        region_types=sorted({r['kind'] for r in region_plan['regions'] if not r['children']}),
        region_plan_signature=region_plan['signature'],validation=validation,
        applied_plugins=dict(attachment=bool(data.get('layout_attachments',{}).get('applied')),
                             region=bool(data.get('layout_regions',{}).get('applied'))),
        routing_stage=data.get('routing_stage'), candidate=data.get('candidate_id'),
        placement_json=str(path.resolve()), components=len(comps), dual_svg_routes_identical=True,canvas=canvas,
        source_sha256=sha256(source.read_bytes()).hexdigest(), plan_signature=plan['signature'],
        coverage=dict(rl_axes=len((plan.get('branch_row_plan') or {}).get('bridge_alignment',())),
                      paired_loads=len(plan.get('paired_banks',())),sources=len(plan.get('source_groups',()))))


def execute(case, policy, output, params, code_fingerprint):
    source = Path(case['netlist'])
    cell = _select_cell(source, case.get('cell'))
    signature = dict(source_sha256=sha256(source.read_bytes()).hexdigest(), cell=cell,
                     policy=policy, params=params, code=code_fingerprint)
    fingerprint = sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:16]
    directory = output/'runs'/case['case']/policy/fingerprint
    directory.mkdir(parents=True, exist_ok=True)
    record = dict(case=case['case'], policy=policy, signature=signature, directory=str(directory.resolve()))
    command = [sys.executable, '-X', 'utf8', '-B', str(ROOT/'run_flow.py'), str(source),
        '--candidate-id', str(params.get('candidate_id',2)),
        '--region-layout' if params.get('experiment')=='region' else '--attachment-layout', policy, '--fallback', 'off',
        '--local-rescue', 'off', '--iterations', str(params['iterations']),
        '--quality-seconds', str(params['quality_seconds']), '--seed', str(params['seed']),
        '--simplify-seconds', str(params['simplify_seconds']), '--out-dir', str(directory)]
    if cell:
        command += ['--cell', cell]
    stem = source.stem+('-'+cell if cell else '')
    placement = directory/(stem+'-placement.json')
    record_file = directory/'run-record.json'
    if record_file.exists():
        previous = json.loads(record_file.read_text(encoding='utf8'))
        if previous.get('signature') == signature and previous.get('status') == 'valid':
            previous['verified'] = verify(placement)
            previous['reused'] = True
            return previous
    record.update(command=command, status='running', reused=False)
    write_json(record_file, record)
    print('START', case['case'], policy, flush=True)
    started = perf_counter()
    try:
        with (directory/'flow.log').open('w', encoding='utf8') as log:
            completed = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                       timeout=params['quality_seconds']+params['simplify_seconds']+8)
        if completed.returncode:
            diagnostics = directory/'routing-diagnostics.json'
            if diagnostics.exists():
                record['diagnostics'] = json.loads(diagnostics.read_text(encoding='utf8'))
            diagnostic = record.get('diagnostics', {})
            reason = diagnostic.get('reason') or diagnostic.get('diagnostics', {}).get('reason')
            record.update(status='timeout' if reason in ('quality_time_budget','time_budget') else 'failed',
                          returncode=completed.returncode)
        else:
            record.update(status='valid', verified=verify(placement), returncode=0)
            view = directory/(stem+'-review.svg')
            with redirect_stdout(io.StringIO()):
                render(placement,view,presentation='clean')
            record['svg'] = str(view.resolve())
    except subprocess.TimeoutExpired:
        record.update(status='timeout', reason='supervisory_limit')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        record.update(status='failed', error=str(exc))
    record['elapsed_seconds'] = round(perf_counter()-started, 6)
    if sha256(source.read_bytes()).hexdigest() != signature['source_sha256']:
        record.update(status='input_changed', reason='source_modified_during_run')
    write_json(record_file, record)
    print('END', case['case'], policy, record['status'], record['elapsed_seconds'], flush=True)
    return record


def paired_summary(records):
    """No coverage, error, regression or timeout is silently counted as a gain."""
    by_case = {}
    for row in records:
        by_case.setdefault(row['case'], {})[row['policy']] = row
    pairs = []
    for name, pair in sorted(by_case.items()):
        old, new = pair.get('off', {}), pair.get('all', pair.get('auto',{}))
        experiment=(new or old).get('signature',{}).get('params',{}).get('experiment','attachment')
        row = dict(case=name, before_status=old.get('status', 'not_run'),
                   after_status=new.get('status', 'not_run'),experiment=experiment,
                   after_policy=new.get('policy','auto' if experiment=='region' else 'all'))
        if old.get('status') != 'valid' or new.get('status') != 'valid':
            row.update(category='unpaired', reason='both_sides_must_be_valid')
            pairs.append(row)
            continue
        a, b = old['verified'], new['verified']
        access_a, access_b = a['organization'].get('host_access', {}), b['organization'].get('host_access', {})
        row['types'] = sorted({g['kind'] for g in a['organization'].get('groups', [])}
            | {'source:'+s['kind'] for s in a['organization'].get('sources', [])})
        if experiment=='region':
            row['types']+=['region:'+t for t in a.get('region_types',())]
        if a.get('plan_signature') != b.get('plan_signature') or a['source_sha256'] != b['source_sha256']:
            row.update(category='uncertain', reason='different_source_or_relationships')
        elif experiment=='region':
            from netlist2schematic.plugins.region.region_layout import region_rank
            ra,rb=a.get('regions',{}),b.get('regions',{})
            if a.get('region_plan_signature') != b.get('region_plan_signature'):
                row.update(category='uncertain',reason='different_region_evidence')
            elif not ra.get('supported') or not rb.get('supported'):
                row.update(category='not_applicable',reason='no_confirmed_region')
            elif not ra.get('measurable') or not rb.get('measurable'):
                row.update(category='uncertain',reason='unmeasurable_region')
            elif not b.get('applied_plugins',{}).get('region'):
                row.update(category='not_applied',reason='region_candidate_kept_original_seed')
            else:
                before,after=region_rank(ra),region_rank(rb)
                row.update(category='improved' if after<before else 'regressed' if after>before else 'unchanged',
                    region_before=ra,region_after=rb)
        elif not access_a.get('count') or not access_b.get('count'):
            row.update(category='not_applicable', reason='no_host_access_evidence')
        elif not access_a.get('measurable') or not access_b.get('measurable') or access_a['count'] != access_b['count']:
            row.update(category='uncertain', reason='incomplete_or_changed_evidence')
        else:
            before = access_a['errors'], round(access_a['backward_distance'],8)
            after = access_b['errors'], round(access_b['backward_distance'],8)
            row.update(category='improved' if after < before else 'regressed' if after > before else 'unchanged',
                       access_before=list(before), access_after=list(after), evidence_count=access_a['count'])
        row['metrics_before'], row['metrics_after'] = a['metrics'], b['metrics']
        row['canvas_before'],row['canvas_after']=a.get('canvas'),b.get('canvas')
        row['organization_dimensions'] = {}
        for key in ('violations','active_order_errors','embedded_members','shape_error',
                    'pin_axis_error','owner_distance','source_errors','source_distance','tail_center_error'):
            x,y = a['organization'].get(key), b['organization'].get(key)
            if (type(x) not in (int,float) or type(y) not in (int,float)
                    or not isfinite(x) or not isfinite(y)):
                change='not_applicable'
            else:
                difference=(y-x)*(-1 if key=='embedded_members' else 1)
                change='improved' if difference < -1e-8 else 'regressed' if difference > 1e-8 else 'unchanged'
            row['organization_dimensions'][key] = dict(before=x,after=y,change=change)
        row['dimension_evaluations']={'host_access':row['category'] if experiment=='attachment' else 'not_applicable'}
        if experiment=='region':
            row['dimension_evaluations']['regions']=row['category']
        for dimension,count_a,count_b,keys in (
                ('conduction_axis',a.get('coverage',{}).get('rl_axes',0),b.get('coverage',{}).get('rl_axes',0),('pin_axis_error',)),
                ('source_placement',a['organization'].get('source_count',0),b['organization'].get('source_count',0),('source_errors','source_distance')),
                ('tail_center',a['organization'].get('tail_center_count',0),b['organization'].get('tail_center_count',0),('tail_center_error',))):
            values_a=tuple(a['organization'].get(k) for k in keys)
            values_b=tuple(b['organization'].get(k) for k in keys)
            if a.get('plan_signature') != b.get('plan_signature'):
                change='uncertain'
            elif not count_a and not count_b:
                change='not_applicable'
            elif count_a != count_b or any(type(v) not in (int,float) or not isfinite(v) for v in (*values_a,*values_b)):
                change='uncertain'
            else:
                before=tuple(round(v,8) for v in values_a)
                after=tuple(round(v,8) for v in values_b)
                change='improved' if after < before else 'regressed' if after > before else 'unchanged'
            row['dimension_evaluations'][dimension]=change
        row['wirelength_ratio'] = b['metrics']['wirelength']/a['metrics']['wirelength'] if a['metrics']['wirelength'] else None
        row['crossing_delta'] = b['metrics']['crossings']-a['metrics']['crossings']
        row['bends_delta'] = b['metrics']['bends']-a['metrics']['bends']
        row['routing_stages'] = [a['routing_stage'], b['routing_stage']]
        # Old limits are diagnostic flags, not a dataset-wide veto.
        row['review_flags'] = [label for label, value in (
            ('wirelength_over_115_percent', row['wirelength_ratio'] is not None and row['wirelength_ratio'] > 1.15),
            ('extra_crossings', row['crossing_delta'] > 0),
            ('extra_bends', row['bends_delta'] > 0),
            ('fallback_stage_changed', a['routing_stage'] != b['routing_stage'])) if value]
        pairs.append(row)
    eligible = [r for r in pairs if r['category'] in ('improved', 'regressed', 'unchanged')]
    ratios = [r['wirelength_ratio'] for r in eligible if type(r['wirelength_ratio']) in (int, float)
              and isfinite(r['wirelength_ratio'])]
    type_names = sorted({t for r in pairs for t in r.get('types', ())})
    by_type = {name:dict(Counter(r['category'] for r in pairs if name in r.get('types', ()))) for name in type_names}
    dimensions=('host_access','conduction_axis','source_placement','tail_center','regions')
    dimension_counts={name:dict(Counter(r.get('dimension_evaluations',{}).get(name,'unpaired') for r in pairs)) for name in dimensions}
    return dict(pairs=pairs, counts=dict(Counter(r['category'] for r in pairs)), by_type=by_type,
        dimension_counts=dimension_counts,
        primary_dimension='experiment_organization_not_overall_visual_quality',
        eligible_pairs=len(eligible), wirelength_ratio_mean=mean(ratios) if ratios else None,
        wirelength_ratio_median=median(ratios) if ratios else None,
        publication_decision='not_requested_small_pilot', independent_visual_review_required=True)


def publish(output, cases, records, dataset, params, code):
    summary = paired_summary(records)
    write_json(output/'comparison.json', dict(summary, parameters=params, records=records,
        inventory=dataset, code_fingerprint=code, scope='short_candidate2_pilot_not_full_dataset_validation'))
    record_map = {(r['case'], r['policy']): r for r in records}
    def image_path(path):
        return Path(path).resolve().as_uri()
    rows = []
    for pair in summary['pairs']:
        case = next(c for c in cases if c['case'] == pair['case'])
        panels = []
        after_policy='auto' if params.get('experiment')=='region' else 'all'
        for label, path in [('GT', case.get('ground_truth'))]+[(p, record_map.get((case['case'], p), {}).get('svg')) for p in ('off', after_policy)]:
            panels.append('<figure><figcaption>'+escape(label)+'</figcaption>'+
                ('<a href="'+escape(image_path(path),quote=True)+'"><img src="'+escape(image_path(path), quote=True)+'"></a>'
                 if path else '<p>无合法成品，见日志</p>')+'</figure>')
        rows.append('<section><h2>'+escape(case['case'])+' · '+escape(pair['category'])+'</h2><p>'+escape(case['title'])+
            '</p><div class="panels">'+''.join(panels)+'</div><pre>'+escape(json.dumps(pair, ensure_ascii=False, indent=2))+'</pre></section>')
    gallery = ''.join('<figure><figcaption>'+escape(r['case'])+' · '+escape(r['status'])+'</figcaption><img src="'+
        escape(image_path(r['ground_truth']), quote=True)+'"></figure>' for r in dataset if r.get('ground_truth'))
    document = '<!doctype html><html lang="zh"><meta charset="utf-8"><title>Goal 小样本对照</title><style>'+\
        'body{font:16px system-ui;margin:24px;background:#f5f6fa}section{background:white;margin:20px 0;padding:16px}'+\
        '.panels{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}figure{margin:0}img{width:100%;height:510px;object-fit:contain;background:white}'+\
        'pre{white-space:pre-wrap;font-size:13px}.gallery{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:20px}.gallery img{height:300px}</style>'+\
        '<h1>Goal：'+('区域组织' if params.get('experiment')=='region' else '宿主端口侧向组织')+'，小样本对照</h1><p>显式候选，短预算同参运行；不代表完整数据集验收。GT仅用于视觉参考。'+\
        '电气与几何逐图复验；美观收益、退步、不适用和失败分别记录。默认开关保持原值。</p><pre>'+\
        escape(json.dumps(dict(summary, pairs=None, parameters=params), ensure_ascii=False, indent=2))+'</pre>'+''.join(rows)+\
        '<h2>25份GT与解析清单</h2><div class="gallery">'+gallery+'</div></html>'
    (output/'index.html').write_text(document, encoding='utf8')


def publish_goal_overview(root):
    """One stable entry, with each case counted once and versions kept visible."""
    paths=[p for p in root.glob('iteration*/comparison.json') if p.parent.name.removeprefix('iteration').isdigit()]
    if not paths:
        return
    progress_path=root/'progress.json'
    budget=(json.loads(progress_path.read_text('utf8')).get('evidence',{}).get(
        'authorized_complex_validation_budget',{}) if progress_path.is_file() else {})
    initial_seconds=budget.get('initial_seconds',60)
    maximum_seconds=budget.get('max_seconds',initial_seconds)
    if any(type(value) not in (int,float) or not isfinite(value) or value<=0
           for value in (initial_seconds,maximum_seconds)):
        raise ValueError('Invalid goal validation budget record')
    budget_note=f'用户确认每轮只跑一张，先使用{initial_seconds:g}秒。'
    if budget.get('extension_authorized') and maximum_seconds>initial_seconds:
        budget_note+=f'失败且诊断显示延时有搜索价值时，可将同一张图上限提高到{maximum_seconds:g}秒；分别保留两次记录。'
    paths.sort(key=lambda p:int(p.parent.name.removeprefix('iteration')))
    latest,rounds={},[]
    inventory_rows=[]
    for path in paths:
        data=json.loads(path.read_text(encoding='utf8'))
        if data.get('report_kind')=='single_case':
            continue
        inventory_rows=data['inventory']
        records={(r['case'],r['policy']):r for r in data['records']}
        rounds.append(dict(iteration=path.parent.name,scope=data['scope'],parameters=data['parameters'],
            counts=data['counts'],dimension_counts=data.get('dimension_counts',{})))
        for pair in data['pairs']:
            latest[pair['case']]=dict(iteration=path.parent.name,pair=pair,
                off=records.get((pair['case'],'off')),all=records.get((pair['case'],pair.get('after_policy','all'))))
    write_json(root/'latest-evidence.json',dict(
        scope='latest_per_case_from_different_pilot_versions_not_a_frozen_release_benchmark',
        unique_cases=len(latest),rounds=rounds,cases=latest,full_goal_completed=False,default_promoted=False))
    labels={'improved':'改善','regressed':'退步','unchanged':'未变','not_applicable':'不适用',
            'unpaired':'无完整成对结果','uncertain':'不确定','timeout':'超时','failed':'失败','not_run':'未运行','not_applied':'未采用增强'}
    def cost(pair,side):
        metrics=pair.get('metrics_'+side)
        return (f"{metrics['wirelength']:.0f} / {metrics['crossings']} / {metrics['bends']}" if metrics
                else labels.get(pair[side+'_status'],pair[side+'_status']))
    cells=[]
    for name,item in sorted(latest.items()):
        pair=item['pair']
        link=item['iteration']+'/index.html'
        changes=pair.get('dimension_evaluations',{})
        dimensions='；'.join(title+'：'+labels.get(changes.get(key),'无成对结果') for key,title in (
            ('host_access','入口侧'),('conduction_axis','导通轴'),('source_placement','源位置'),('tail_center','尾源居中'),('regions','功能区')))
        flags='、'.join(pair.get('review_flags',())) or '—'
        review=item['all'].get('svg') if item.get('all') else None
        href=Path(review).as_uri() if review else link
        cells.append('<tr><td><a href="'+escape(href,quote=True)+'">'+escape(name)+'</a></td><td>'+escape(item['iteration'])+
            '</td><td>'+escape(cost(pair,'before'))+'</td><td>'+escape(cost(pair,'after'))+'</td><td>'+escape(dimensions)+
            '</td><td>'+escape(flags)+'</td></tr>')
    round_links=''.join('<li><a href="'+r['iteration']+'/index.html">'+r['iteration']+'</a>：'+
        escape(json.dumps(r['dimension_counts'],ensure_ascii=False))+'</li>' for r in rounds)
    diagnostics=''.join('<li><a href="'+escape(p.relative_to(root).as_posix(),quote=True)+'">'+
        escape(title)+'</a></li>' for folder,title in (
            ('cached-region-compaction','已有合法线路：核心附件空隙收紧，不运行 SA/A*'),
            ('region-seed-preview','初排组织预览：无完整线路，不计成功出图'),
            ('port-bank-and-tail-preview','端口顺序及尾源规整：SA后静态预览，尚未布线'))
        if (p:=root/folder/'index.html').is_file())
    single_runs=[]
    for p in sorted(root.glob('iteration*/single-case-result.json')):
        row=json.loads(p.read_text('utf8'))
        record=row['record']
        single_runs.append(dict(iteration=p.parent.name,case=record['case'],status=record['status'],
            elapsed_seconds=record.get('elapsed_seconds'),metrics=record.get('verified',{}).get('metrics'),
            parameters=row['parameters'],link=p.parent.name+'/index.html'))
    single_html=''.join('<li><a href="'+escape(r['link'],quote=True)+'">'+
        escape(r['iteration']+' / '+r['case'])+'</a>：'+escape(json.dumps(r,ensure_ascii=False))+'</li>' for r in single_runs)
    write_json(root/'single-run-evidence.json',dict(scope='independent_complete_flow_not_paired_gain',runs=single_runs))
    gallery=''.join('<figure><figcaption>'+escape(r['case'])+'</figcaption><a href="'+Path(r['ground_truth']).as_uri()+
        '"><img src="'+Path(r['ground_truth']).as_uri()+'"></a></figure>' for r in inventory_rows if r.get('ground_truth'))
    document='<!doctype html><html lang="zh"><meta charset="utf-8"><title>电路图组织 Goal 进展</title><style>'+\
        'body{font:16px system-ui;margin:24px;background:#f5f6fa}table{border-collapse:collapse;width:100%;background:white}'+\
        'th,td{padding:10px;border:1px solid #ccd2dc;text-align:left}.gallery{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}'+\
        'figure{margin:0}img{width:100%;height:320px;object-fit:contain;background:white}li{margin:10px 0}</style>'+\
        '<h1>电路图组织：Goal 进展</h1><p>GT总结 → 通用规则 → 短对照 → 分类型审阅。25份GT均已解析、浏览；完整25图flow与总体目标尚未验收。'+\
        '默认附件策略仍为off。</p><p>早期配对记录为同参候选2短跑：600次迭代参数、正常阶段12秒、缩线1秒、关闭恢复链。'+\
        '各成功成品重新执行完整连接/几何验收、核对双SVG，并独立重绘。超时保留诊断。</p>'+\
        '<p>下表按样例只展示最新一组证据，来自不同迭代版本，不能作为同一冻结版本的发布成绩；重复样例不重复累计收益。'+\
        '不适用与未改变均不计为改善。数据列依次为线长／交叉／折角。</p>'+\
        '<table><tr><th>样例／新图</th><th>来源</th><th>off</th><th>开启版</th><th>独立组织指标</th><th>成本复核项</th></tr>'+''.join(cells)+'</table>'+\
        '<h2>后续每轮单图完整运行</h2><p>'+escape(budget_note)+'以下记录不是off/auto同参对照，'+\
        '证明实际完成及验收；不能据此计算单项策略收益。预览不放入此表。</p><ul>'+single_html+'</ul>'+\
        '<h2>各轮记录与三栏对照</h2><ul>'+round_links+'</ul>'+\
        '<h2>独立诊断，区别于完整 flow 对照</h2><ul>'+diagnostics+'</ul>'+\
        '<p>实际实现与限制见 <a href="'+(ROOT/'docs/attachment-integration.md').as_uri()+'">附件联合布局说明</a>；'+\
        '<a href="progress.json">完整目标与待办</a>；<a href="off-and-cost-audit.json">关闭版一致性与估价开销检查</a>。</p>'+\
        '<h2>25份GT</h2><p>只作视觉参考；算法不读取GT坐标。</p><div class="gallery">'+gallery+'</div></html>'
    (root/'index.html').write_text(document,encoding='utf8')


def seed_previews(cases,output,seed,*,iterations=0,finish=False):
    """Render diagnostic layouts only, explicitly carrying no success assertion."""
    from copy import deepcopy
    from netlist2schematic.layout.structured_placement import create_candidates
    from netlist2schematic.plugins.region.region_layout import measure_regions
    from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
    from netlist2schematic.routing.routing_rules import RoutingOptions
    from netlist2schematic.rendering.sa_render import render_svg
    from netlist2schematic.flow.run_flow import _saved_positions
    from compare_region_layouts import annotated_svg
    from netlist2schematic.common.sa_common import load_local
    output.mkdir(parents=True,exist_ok=True)
    files=engine_source_files()+[Path(__file__),ROOT/'symbol-geometry.json']
    files+=sorted((resource_root()/'packages/components/definitions').glob('*.json'))
    fingerprints={str(p.resolve()):sha256(p.read_bytes()).hexdigest() for p in files}
    input_hashes={str(Path(case['netlist']).resolve()):sha256(Path(case['netlist']).read_bytes()).hexdigest()
                  for case in cases}
    write_json(output/'execution-inputs.json',dict(scope='unrouted_layout_preview',
        files=fingerprints,inputs=input_hashes,seed=seed,candidate_id=2,iterations=iterations,
        sa_called=iterations>0,astar_called=False,gt_coordinates_used=False,
        stage='before_routing' if finish else 'initial_placement'))
    par=load_local('place_and_route','place-and-route.py')
    records,sections=[],[]
    for case in cases:
        source=Path(case['netlist'])
        cell=_select_cell(source,case.get('cell'))
        variants={}
        for policy in ('off','auto'):
            variants[policy]=list(create_candidates(source,cell=cell,iterations=iterations,seed=seed,
                                                    region_layout=policy))[1]
        plan=variants['auto'][4].get('layout_regions',{}).get('plan',{})
        panels=[('GT：仅供组织方式参考',case.get('ground_truth'))]
        for policy,(pl,comps,nets,index,metadata) in variants.items():
            if finish and policy=='auto' and metadata.get('layout_regions',{}).get('applied'):
                from netlist2schematic.plugins.region.region_controls import refine_region_controls
                from netlist2schematic.plugins.region.region_tails import refine_region_tails
                until=perf_counter()+1
                pl,metadata=refine_region_controls(par,pl,comps,nets,index,metadata,deadline=until)
                pl,metadata=refine_region_tails(par,pl,comps,nets,index,metadata,deadline=until)
            folder=output/case['case']/policy
            folder.mkdir(parents=True,exist_ok=True)
            image=folder/'layout-preview.svg'
            measured=measure_regions(pl,comps,index,plan)
            faults=_static_faults(par,pl,nets,index,RoutingOptions(),None)
            faults={k:v for k,v in faults.items() if k!='hard'}
            render_svg(par,pl,comps,nets,index,[],dict(wirelength=0,crossings=0,bends=0,
                overlap=0,proximity=0,body_pen=0),image,clean=True,
                validation={'valid':False,'reason':'unrouted_layout_preview'})
            annotated_svg(image,image,pl,comps,index,plan)
            tree=ET.parse(image)
            root=tree.getroot()
            root.set('data-diagnostic','unrouted-layout-preview')
            x,y,*_=map(float,root.attrib['viewBox'].split())
            ET.SubElement(root,'{http://www.w3.org/2000/svg}text',
                {'x':str(x+10),'y':str(y+20),'fill':'#b91c1c','font-size':'15'}).text='未布线布局预览 · 非成功成品'
            tree.write(image,encoding='utf8',xml_declaration=True)
            record=dict(case=case['case'],policy=policy,source=str(source),
                source_sha256=sha256(source.read_bytes()).hexdigest(),cell=cell,
                seed=seed,iterations=iterations,candidate_id=2,sa_called=iterations>0,astar_called=False,
                validation={'valid':False,'reason':'not_routed'},
                scope='before_routing_preview' if finish else 'initial_placement_only',
                placement=_saved_positions(pl,comps),
                layout_grid=dict(column_x=list(pl.column_x),row_y=list(pl.row_y)),
                layout_regions=deepcopy(metadata.get('layout_regions')),measurement=measured,
                static_port_check=faults)
            write_json(folder/'layout-preview.json',record)
            records.append(record)
            panels.append((policy+'：候选2，'+('SA与局部收尾后' if finish else '仅初排'),image))
        pictures=''.join('<figure><figcaption>'+escape(title)+'</figcaption><img src="'+
            Path(path).resolve().as_uri()+'"></figure>' for title,path in panels if path)
        sections.append('<section><h2>'+escape(case['case']+' '+case['title'])+
            '</h2><div class="panels">'+pictures+'</div><pre>'+escape(json.dumps(
                {r['policy']:dict(measurement=r['measurement'],static_port_check=r['static_port_check'])
                 for r in records if r['case']==case['case']},ensure_ascii=False,indent=2))+'</pre></section>')
    write_json(output/'comparison.json',dict(scope='unrouted_layout_preview',records=records))
    for filename,digest in {**fingerprints,**input_hashes}.items():
        if sha256(Path(filename).read_bytes()).hexdigest()!=digest:
            raise ValueError('Preview input or engine changed during diagnostic rendering')
    (output/'index.html').write_text('<!doctype html><html lang="zh"><meta charset="utf-8">'+
        '<title>区域布局预览</title><style>body{font:16px system-ui;margin:24px}section{margin:28px 0}'+
        '.panels{display:flex;gap:14px}figure{flex:1;margin:0}img{width:100%;height:520px;object-fit:contain}'+
        'pre{white-space:pre-wrap;font-size:12px}</style><h1>区域布局预览</h1><p>只比较候选2的'+
        ('SA及局部收尾后布局' if finish else '零迭代初始布局')+'；不运行 A* 或恢复，'+
        '不代表布线成功、最终选中或最终美观收益。框线只表示归属。'+
        '同时列出静态端口冲突；完整连接验收须在真正布线后另做。GT 坐标不输入算法。</p>'+''.join(sections)+'</html>',encoding='utf8')
    print(output/'index.html',flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', nargs='+', default=['data07', 'data14', 'data18'])
    parser.add_argument('--out-dir', type=Path, default=ROOT/'output/schematic-goal/iteration1')
    parser.add_argument('--iterations', type=int, default=600)
    parser.add_argument('--experiment',choices=('attachment','region'),default='attachment')
    parser.add_argument('--seed',type=int,default=20261029)
    parser.add_argument('--candidate-id',type=int,choices=(1,2,3),default=2)
    parser.add_argument('--quality-seconds', type=float, default=12)
    parser.add_argument('--refresh-only',action='store_true',help='Revalidate saved pilot outputs; never launch SA or A*')
    parser.add_argument('--seed-preview-only',action='store_true',help='Unrouted zero-SA region previews, never successful flow outputs')
    parser.add_argument('--layout-preview-only',action='store_true',help='Requested SA steps and local tail finish, never A* or successful flow outputs')
    args = parser.parse_args(argv)
    if not 0 <= args.iterations or not 0 < args.quality_seconds <= 30:
        parser.error('pilot needs nonnegative iterations and 0 < quality seconds <= 30')
    all_cases = discover_cases()
    cases = [c for name in args.cases for c in all_cases if c['case'] == name]
    if len(cases) != len(set(args.cases)):
        parser.error('unknown or repeated case')
    if args.seed_preview_only or args.layout_preview_only:
        if args.refresh_only or args.experiment!='region' or args.seed_preview_only and args.layout_preview_only:
            parser.error('region seed preview cannot be mixed with refresh or attachment pilots')
        seed_previews(cases,args.out_dir.resolve(),args.seed,
            iterations=args.iterations if args.layout_preview_only else 0,finish=args.layout_preview_only)
        publish_goal_overview(args.out_dir.resolve().parent)
        return
    params = dict(iterations=args.iterations, quality_seconds=args.quality_seconds,
                  simplify_seconds=1, seed=args.seed, candidate_id=args.candidate_id,experiment=args.experiment)
    configure_resources()
    files = engine_source_files()+[Path(__file__), ROOT/'symbol-geometry.json', ROOT/'requirements.txt']
    files += sorted((resource_root()/'packages/components/definitions').glob('*.json'))
    fingerprints = {str(p.resolve()):sha256(p.read_bytes()).hexdigest() for p in files}
    code = sha256(''.join(str(p)+sha256(p.read_bytes()).hexdigest() for p in files).encode()).hexdigest()
    dataset = inventory([c for c in all_cases if c['case'].startswith('data')])
    output = args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.refresh_only:
        saved=json.loads((output/'comparison.json').read_text(encoding='utf8'))
        records=saved['records']
        names={r['case'] for r in records}
        cases=[c for c in all_cases if c['case'] in names]
        for record in records:
            diagnostic=record.get('diagnostics',{})
            reason=diagnostic.get('reason') or diagnostic.get('diagnostics',{}).get('reason')
            if record['status']=='failed' and reason in ('quality_time_budget','time_budget'):
                record.update(original_reported_status='failed',status='timeout',
                    classification_reason='engine_reported_deadline_exhaustion')
            if record['status']=='valid':
                record['verified']=verify(Path(record['verified']['placement_json']))
                view=Path(record['directory'])/(Path(record['verified']['placement_json']).name.removesuffix('-placement.json')+'-review.svg')
                with redirect_stdout(io.StringIO()):
                    render(record['verified']['placement_json'],view,presentation='clean')
                record['svg']=str(view)
        publish(output,cases,records,dataset,saved['parameters'],saved['code_fingerprint'])
        write_json(output/'measurement-inputs.json',dict(files=fingerprints,scope='revalidation_only',
            historical_execution_fingerprint=saved['code_fingerprint']))
        publish_goal_overview(output.parent)
        print(output/'index.html',flush=True)
        return
    write_json(output/'execution-inputs.json', dict(files=fingerprints,code_fingerprint=code,
        parameters=params,inputs={r['source']:r.get('source_sha256') for r in dataset}))
    records = []
    for case in cases:
        for policy in ('off', 'auto' if args.experiment=='region' else 'all'):
            records.append(execute(case, policy, output, params, code))
            publish(output, cases, records, dataset, params, code)
    print(output/'index.html', flush=True)
    publish_goal_overview(output.parent)


if __name__ == '__main__':
    main()
