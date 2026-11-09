#!/usr/bin/env python3
"""Isolated, resumable GT/off/auto comparisons with final-output release gates."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from html import escape
import json
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
from sa_common import read_json, write_json, load_local

FORMAT = 'structure-layout-comparison-v1'


def runtime_snapshot(stage):
    """Freeze runtime dependencies; later-stage, unimported modules are excluded."""
    excluded=set()
    if stage==1:excluded.update(('branch_levels.py','branch_alignment.py','attachment_placement.py','presentation.py','placement_preflight.py','candidate_budget.py'))
    elif stage==2:excluded.update(('branch_alignment.py','attachment_placement.py','presentation.py','placement_preflight.py','candidate_budget.py'))
    return {p:h for p,h in common._snapshot().items()
            if 'tests' not in Path(p).parts and Path(p).name not in excluded}


def check_signature(signature):
    if common._input_changes(signature['source_files']):raise ValueError('Original input changed')
    if runtime_snapshot(signature['parameters']['structure_phase'])!=signature['code_files']:
        raise ValueError('Runtime code or symbol resources changed during comparison')


def verify_row_plan(data, pl, index, active):
    plan=data.get('branch_row_plan',{})
    protected=set(active)
    protected.update(order['lower'] for order in plan.get('tail_orders',()))
    if any(pl.cell_of[index[name]][0]!=plan['rows'][name]
           for name in protected if name in plan.get('rows',{})):
        raise ValueError('Saved fixed rows differ from the branch plan')
    for order in plan.get('strict_orders',()):
        if pl.cell_of[index[order['upper']]][0]>=pl.cell_of[index[order['lower']]][0]:
            raise ValueError('Saved placement violates a directed branch constraint')
    for group in plan.get('same_row',()):
        if len({pl.cell_of[index[name]][0] for name in group['members']})>1:
            raise ValueError('Saved placement violates a same-row branch relation')


def verify(path, signature):
    from circuit_structures import analyze_structures
    from layout_quality import evaluate_quality
    from layout_relations import build_layout_relations, measure_layout_relations
    from netlist_core import parse_netlist
    from routing_rules import build_scene
    from sa_routing import validate_final_routes
    from sa_saved import load_placement, saved_routing_options
    from simplify_routes import _covers_fixed
    with VERIFY_LOCK:
        data, source, par, pl, comps, nets, index = load_placement(path)
        if str(source) != signature['source_netlist'] or data.get('source_cell') != signature['cell']:
            raise ValueError('Saved result refers to a different source/cell')
        options = saved_routing_options(data)
        routed, metrics, checked = validate_final_routes(
            par, nets, pl, index, list(data['routes'].items()), options=options)
        if not checked['valid'] or metrics != data['metrics']:
            raise ValueError('Saved metrics differ from independent verification')
        scene = build_scene(par, nets, pl, index, options=options)
        if not all(_covers_fixed(scene, net, runs) for net, runs in routed):
            raise ValueError('Saved result omits a fixed original-pin exit')
        stem = path.name.removesuffix('-placement.json')
        clean, color = path.with_name(stem+'-clean.svg'), path.with_name(stem+'-layered.svg')
        svg, wires = _wire_paths(clean)
        if wires != _wire_paths(color)[1]:
            raise ValueError('SVGs do not share the same routes')
        renderer = load_local('structure_comparison_redraw', 'render-clean.py')
        with tempfile.TemporaryDirectory(prefix='structure-redraw-') as folder:
            redraw = Path(folder)/'redraw.svg'
            renderer.render(path, redraw)
            if redraw.read_bytes() != clean.read_bytes():
                raise ValueError('Independent redraw differs from saved clean SVG')
        devices = parse_netlist(source, cell=signature['cell'])
        structs = analyze_structures(devices)
        relations = build_layout_relations(devices, structs)
        from structure_selection import prepare_branch_context, measure_structure
        context=deepcopy(data)
        context['layout_relations']=relations
        phase=signature['parameters']['structure_phase']
        if phase>=3:context=prepare_branch_context(source,signature['cell'],pl,comps,nets,index,context)
        measurement = measure_structure(par,pl,comps,nets,index,context,phase)
        active = {name: pl.cell_of[index[name]][0] for name, d in devices.items()
                  if d['cls'] in ('MOS', 'BJT')}
        if any(pl.orient[index[name]] not in (0, 1) for name in active):
            raise ValueError('Unsupported active orientation')
        # A new row plan is checked against its declared directed constraints,
        # rather than falsely comparing it to the deliberately superseded levels.
        plan = data.get('branch_row_plan', {})
        verify_row_plan(data,pl,index,active)
        return {'valid': True, 'placement_json': str(path), 'clean_svg': str(clean),
            'color_svg': str(color), 'metrics': metrics,
            'quality': evaluate_quality(par, pl, comps, nets, index, routed, metrics, structs),
            'measurement': measurement, 'structure_layout': data.get('structure_layout'),
            'branch_row_plan': plan, 'component_count': len(comps),
            'width': float(svg.get('width')), 'height': float(svg.get('height')),
            'active_rows': active, 'placement': data['placement'],
            'selected_candidate': data.get('quality', {}).get('selected_candidate'),
            'routing_stage': data.get('routing_stage', 'normal'),
            'attempted_candidate_ids': sorted({str(r['candidate_id'])
                for r in common._candidate_records(data) if 'candidate_id' in r}),
            'route_simplification': data.get('route_simplification'),
            'routing_seconds': data.get('validation', {}).get('routing_seconds'),
            'quality_seconds': data.get('quality', {}).get('elapsed_seconds'),
            'recovery_stages': data.get('recovery', {}).get('stages', []),
            'all_original_pins_connected': True, 'all_fixed_stubs_preserved': True,
            'color_and_clean_routes_identical': True, 'independent_redraw_identical': True}


def execute(case, policy, output, parameters, code, frozen):
    try:
        signature = common._signature(case, policy, parameters, code)
    except (ValueError, OSError, KeyError) as exc:
        return {'case': case['case'], 'policy': policy, 'status': 'skipped',
                'reason': 'input_validation_error', 'error': str(exc)}
    signature['format'] = FORMAT
    if any(frozen.get(p) != h for p, h in signature['source_files'].items()):
        raise ValueError('Batch input changed')
    fingerprint = common._digest(signature)
    base = output/'runs'/case['case']/policy
    base.mkdir(parents=True, exist_ok=True)
    for saved in sorted(base.glob('*/run-record.json'), key=lambda p:p.stat().st_mtime, reverse=True):
        try: record = read_json(saved)
        except (ValueError,OSError): continue
        if record.get('fingerprint') == fingerprint and record.get('status') in ('valid', 'failed'):
            check_signature(signature)
            if record['status'] == 'valid':
                record['verified'] = verify(Path(record['verified']['placement_json']), signature)
            record['reused'] = True
            print(f'REUSE {case["case"]}/{policy}', flush=True)
            return record
    directory = base/fingerprint[:16]
    if directory.exists(): directory = base/(fingerprint[:16]+'-'+uuid.uuid4().hex[:8])
    directory.mkdir()
    p = parameters
    command = [sys.executable, '-X', 'utf8', '-B', '-u', str(ROOT/'run_flow.py'),
        signature['source_netlist'], '--out-dir', str(directory),
        '--structure-layout', policy, '--symmetry-layout', 'off',
        '--structure-phase', str(p['structure_phase']),
        '--seed', str(p['seed']), '--iterations', str(p['iterations']),
        '--fallback', 'auto', '--local-rescue', 'auto']
    for k in ('quality_seconds','rescue_seconds','spacing_seconds','fallback_seconds','simplify_seconds'):
        command += ['--'+k.replace('_','-'), str(p[k])]
    if signature['cell']: command += ['--cell', signature['cell']]
    if p.get('candidate_id') is not None: command += ['--candidate-id', str(p['candidate_id'])]
    record = {'format': FORMAT, 'case':case['case'], 'policy':policy,
        'signature':signature, 'fingerprint':fingerprint, 'directory':str(directory),
        'command':command, 'status':'running', 'reused':False}
    write_json(directory/'run-record.json', record)
    print(f'START {case["case"]}/{policy}', flush=True)
    start = perf_counter()
    try:
        with (directory/'flow.log').open('w',encoding='utf8') as log:
            process = subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
            try:
                record['returncode'] = process.wait(timeout=sum(p[k] for k in (
                    'quality_seconds','rescue_seconds','spacing_seconds','fallback_seconds','simplify_seconds'))+120)
            except subprocess.TimeoutExpired:
                common._stop(process)
                raise ValueError('Supervisory timeout')
        check_signature(signature)
        if record['returncode']:
            diagnostic=directory/'routing-diagnostics.json'
            if diagnostic.is_file(): record['diagnostics']=read_json(diagnostic)
            raise ValueError('Flow failed; see flow.log')
        stem=Path(signature['source_netlist']).stem+('-'+signature['cell'] if signature['cell'] else '')
        record['verified']=verify(directory/(stem+'-placement.json'),signature)
        record['status']='valid'
    except (ValueError,OSError,KeyError,TypeError) as exc:
        record.update(status='failed',error=str(exc))
    record['elapsed_seconds']=round(perf_counter()-start,6)
    write_json(directory/'run-record.json',record)
    print(f'END {case["case"]}/{policy}: {record["status"]} {record["elapsed_seconds"]:.2f}s',flush=True)
    return record


def gates(old, new):
    a,b=old['metrics'],new['metrics']
    violation=lambda x:x['measurement'].get('interleaving',0)+x['measurement'].get('role_order',0)
    return {'wirelength_115_percent': b['wirelength'] <= a['wirelength']*1.15+1e-9,
        'crossings_limit': b['crossings'] <= (0 if a['crossings']==0 else a['crossings']+1),
        'no_new_structure_violations': all(new['measurement'].get(k,0)<=old['measurement'].get(k,0)
                                          for k in ('interleaving','role_order')),
        'no_new_bus_fallback': new['routing_stage']!='bus_fallback' or old['routing_stage']=='bus_fallback'}


def publish(output,cases,records,parameters,code,frozen):
    rows,html=[],[]
    old_cross=new_cross=0
    for case in cases:
        result={p:records[(case['case'],p)] for p in ('off','auto') if (case['case'],p) in records}
        row={**case,'results':result}
        row['same_input_version']=(len(result)==2 and
            result['off'].get('signature',{}).get('source_files')==
            result['auto'].get('signature',{}).get('source_files'))
        if row['same_input_version'] and all(r['status']=='valid' for r in result.values()):
            old,new=(result[p]['verified'] for p in ('off','auto'))
            row['gates']=gates(old,new)
            old_cross+=old['metrics']['crossings'];new_cross+=new['metrics']['crossings']
        rows.append(row)
        figures=[]
        truth=case.get('ground_truth')
        figures.append(common._figure(output,'Ground truth（视觉参考）',truth) if truth else '<figure>无GT</figure>')
        table=[]
        for policy in ('off','auto'):
            r=result.get(policy,{})
            if r.get('status')=='valid':
                v=r['verified'];m=v['metrics'];s=v['measurement']
                figures.append(common._figure(output,policy,v['clean_svg'],
                    '<p><a href="'+escape(common._relative(output,v['placement_json']))+'">JSON</a></p>'))
                table.append([policy,m['wirelength'],m['crossings'],m['bends'],s.get('interleaving'),
                    s.get('role_order'),s.get('normalized_span'),f"{v['width']}×{v['height']}",v['routing_stage'],r['elapsed_seconds']])
            else:
                figures.append('<figure>'+escape(policy+': '+r.get('status','pending')+' '+r.get('error',''))+'</figure>')
        html.append('<section><h2>'+escape(case['case']+' '+case.get('title',''))+'</h2><div class="figures">'+''.join(figures)+'</div>'+common._table(
            ['模式','线长','交叉','折角','穿插','顺序冲突','跨度','宽×高','阶段','秒'],table)+
            '<pre>'+escape(json.dumps(row.get('gates',{}),ensure_ascii=False,indent=2))+'</pre></section>')
    complete=len(records)==len(cases)*2
    valid=sum(r['status']=='valid' for r in records.values())
    paired_status = all(len(row['results']) == 2 and (
        all(r['status'] == 'valid' for r in row['results'].values()) or
        all(r['status'] == 'skipped' and r.get('reason') == 'input_validation_error'
            for r in row['results'].values())) for row in rows)
    report={'format':FORMAT,'parameters':parameters,'code_files':code,'input_files':frozen,
        'input_changes':common._input_changes(frozen),'cases':rows,'expected_runs':len(cases)*2,
        'completed_runs':len(records),'valid_runs':valid,
        'skipped_runs':sum(r['status']=='skipped' for r in records.values()),
        'failed_runs':sum(r['status']=='failed' for r in records.values()),
        'ground_truth_electrically_verified':False,
        'release_gates':{'complete':complete,'inputs_unchanged':not common._input_changes(frozen),
            'paired_status_consistent':paired_status,
            'paired_sources_identical':all(row['same_input_version'] for row in rows
                if len(row['results'])==2 and all(r['status']=='valid' for r in row['results'].values())),
            'no_routing_failures':all(r['status']!='failed' for r in records.values()),
            'paired_gates':all(all(row.get('gates',{}).values()) for row in rows),
            'total_crossings_nonincreasing':new_cross<=old_cross,
            'crossings_off':old_cross,'crossings_auto':new_cross}}
    write_json(output/'comparison.json',report)
    page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>结构布局逐阶段对照</title>
<style>body{font:14px/1.6 system-ui;background:#f5f6f8;margin:24px}section{background:white;padding:20px;margin:24px 0}.figures{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}figure{margin:0}img{width:100%;height:460px;object-fit:contain}table{border-collapse:collapse}td,th{padding:6px 10px;border-bottom:1px solid #ddd}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>
<h1>结构布局：GT / off / auto</h1><p>GT仅作视觉参照。两种策略均独立完整运行，成品复验连接、几何、固定出线、两种SVG和独立重绘。以下门槛针对最终简化后的图，不是SA估计。</p>'''
    (output/'index.html').write_text(page+'<pre>'+escape(json.dumps(report['release_gates'],ensure_ascii=False,indent=2))+'</pre>'+''.join(html)+'</html>',encoding='utf8')
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir',type=Path,default=ROOT/'output/structure-layout-comparison')
    parser.add_argument('--cases',nargs='+')
    parser.add_argument('--jobs',type=int,choices=range(1,7),default=4)
    parser.add_argument('--candidate-id',choices=('1','2','3'))
    parser.add_argument('--stage-label',default='stage1')
    parser.add_argument('--structure-phase',type=int,choices=(1,2,3),default=3)
    for k,v in common.DEFAULT_PARAMETERS.items():
        if k=='jobs':continue
        parser.add_argument('--'+k.replace('_','-'),type=int if k in ('seed','iterations') else float,default=v)
    args=parser.parse_args(argv)
    parameters={k:getattr(args,k) for k in common.DEFAULT_PARAMETERS}
    parameters.update(candidate_id=args.candidate_id,stage_label=args.stage_label,structure_phase=args.structure_phase)
    cases=common.discover_cases()
    if args.cases:
        missing=set(args.cases)-{c['case'] for c in cases}
        if missing:parser.error('Unknown cases: '+', '.join(sorted(missing)))
        cases=[c for c in cases if c['case'] in args.cases]
    if not cases:parser.error('No selected cases')
    output=args.out_dir.resolve();output.mkdir(parents=True,exist_ok=True)
    frozen,code=common._freeze_inputs(cases),runtime_snapshot(args.structure_phase)
    records={}
    publish(output,cases,records,parameters,code,frozen)
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures={pool.submit(execute,c,p,output,parameters,code,frozen):(c,p) for c in cases for p in ('off','auto')}
        for future in as_completed(futures):
            case,policy=futures[future]
            try:record=future.result()
            except Exception as exc:
                record={'case':case['case'],'policy':policy,'status':'failed','error':str(exc)}
            records[(case['case'],policy)]=record
            publish(output,cases,records,parameters,code,frozen)
    return 1 if any(r['status']=='failed' for r in records.values()) else 0


if __name__=='__main__':
    raise SystemExit(main())
