#!/usr/bin/env python3
"""Recheck and compress saved region outputs, without SA or A*.

This is a geometry-only ablation. It does not compare complete off/auto flows,
nor claim that a historical candidate is the new engine's selected winner.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from copy import deepcopy
from hashlib import sha256
from html import escape
import io
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tools')]

from compare_region_layouts import annotated_svg,build_measurement_plan
from evaluate_goal_layouts import verify,write_json
from netlist2schematic.common.sa_common import engine_source_files,resource_root
from netlist2schematic.common.sa_saved import load_placement,saved_routing_options
from netlist2schematic.flow.run_flow import _finish,_saved_positions
from netlist2schematic.layout.layout_quality import evaluate_quality
from netlist2schematic.netlist.netlist_core import parse_netlist
from netlist2schematic.plugins.region.region_compaction import compact_region_routes
from netlist2schematic.plugins.region.region_layout import measure_regions
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.rendering.render_clean import render


def execute(path,output,seconds):
    path=path.resolve()
    source_hash=sha256(path.read_bytes()).hexdigest()
    started=perf_counter()
    data,source,par,pl,comps,nets,index=load_placement(path)
    options=saved_routing_options(data)
    devices=parse_netlist(source,cell=data.get('source_cell'))
    plan=build_measurement_plan(source,data.get('source_cell'),devices,
        analyze_structures(devices),comps,nets,index)
    files=engine_source_files()+[Path(__file__),ROOT/'symbol-geometry.json']
    files+=sorted((resource_root()/'packages/components/definitions').glob('*.json'))
    hashes={str(p.resolve()):sha256(p.read_bytes()).hexdigest() for p in files}
    signature=dict(input_json=str(path),input_json_sha256=source_hash,seconds=seconds,
        source_sha256=sha256(source.read_bytes()).hexdigest(),files=hashes)
    fingerprint=sha256(json.dumps(signature,sort_keys=True).encode()).hexdigest()[:16]
    directory=output/path.stem[:36]/fingerprint
    base=path.name.removesuffix('-placement.json')
    original_positions=deepcopy(data['placement'])
    before_org=measure_regions(pl,comps,index,plan)
    changed,result,report=compact_region_routes(par,pl,comps,nets,index,
        (list(data['routes'].items()),data['metrics'],data['validation']),plan,
        options=options,seconds=seconds)
    data=deepcopy(data)
    quality=evaluate_quality(par,changed,comps,nets,index,result[0],result[1],data.get('structs',()))
    data['region_compaction_replay']=dict(signature=signature,report=report,
        historical_quality=deepcopy(data.get('quality')),historical_grid=deepcopy(data.get('layout_grid')),
        historical_simplification=deepcopy(data.get('route_simplification')),
        scope='saved_geometry_ablation_not_fresh_flow_or_candidate_selection',
        sa_called=False,astar_called=False)
    data['quality']={**data.get('quality',{}),'score':quality['score'],'details':quality,
        'candidate_metrics_stage':'historical_before_region_compaction',
        'current_metrics_stage':'region_compaction_replay'}
    data['placement']=_saved_positions(changed,comps)
    data['layout_grid']={'column_x':list(changed.column_x),'row_y':list(changed.row_y)}
    data['layout_regions']={**data.get('layout_regions',{}),'plan':plan,'route_compaction':report}
    if (set(data['placement'])!=set(original_positions) or any(
            any(data['placement'][name].get(k,0)!=old.get(k,0) for k in ('row','col','orient','dx'))
            for name,old in original_positions.items())):
        raise ValueError('compaction unexpectedly changed logical cells, orientations or offsets')
    with redirect_stdout(io.StringIO()):
        _finish(data,par,changed,comps,nets,index,base,directory,
            protected_paths={source,path},input_hashes={str(path):source_hash},
            options=options,routed_result=result,simplify_routes='off')
    saved=directory/(base+'-placement.json')
    checked=verify(saved)
    with redirect_stdout(io.StringIO()):
        render(saved,directory/(base+'-review.svg'),presentation='clean')
    annotated_svg(directory/(base+'-clean.svg'),directory/(base+'-regions.svg'),changed,comps,index,plan)
    # Explicitly recheck the original rather than trusting its old valid flag.
    old=verify(path)
    record=dict(status='valid',scope='cached_region_geometry_only',signature=signature,
        before=old,after=checked,report=report,
        organization_before=before_org,organization_after=measure_regions(changed,comps,index,plan),
        original_json_unchanged=sha256(path.read_bytes()).hexdigest()==source_hash,
        before_svg=str(path.with_name(base+'-clean.svg')),
        after_svg=str(directory/(base+'-review.svg')),
        annotated_svg=str(directory/(base+'-regions.svg')),
        elapsed_seconds=perf_counter()-started)
    write_json(directory/'run-record.json',record)
    return record


def publish(output,records):
    sections=[]
    for record in records:
        panels=[]
        if record.get('ground_truth'):
            panels.append(('GT：仅供视觉参考',record['ground_truth']))
        if record['status']=='valid':
            panels += [('原区域布局',record['before_svg']),('收紧后',record['after_svg'])]
        pictures=''.join('<figure><figcaption>'+escape(label)+'</figcaption><a href="'+
            escape(Path(path).resolve().as_uri(),quote=True)+'"><img src="'+
            escape(Path(path).resolve().as_uri(),quote=True)+'"></a></figure>' for label,path in panels)
        details=({k:record[k] for k in ('report','organization_before','organization_after','elapsed_seconds')}
                 if record['status']=='valid' else record)
        sections.append('<section><h2>'+escape(record['label'])+'</h2><div class="panels">'+pictures+
            '</div><pre>'+escape(json.dumps(details,ensure_ascii=False,indent=2))+'</pre></section>')
    write_json(output/'comparison.json',dict(scope='saved_geometry_only_no_sa_no_astar',records=records,
        defaults_changed=False,complete_goal=False))
    (output/'index.html').write_text('<!doctype html><html lang="zh"><meta charset="utf-8">'+
        '<title>核心附件间距：安全收紧验证</title><style>body{font:16px system-ui;margin:24px;background:#f5f6fa}'+
        'section{background:white;padding:16px;margin:20px 0}.panels{display:flex;gap:12px}figure{flex:1;margin:0}'+
        'img{width:100%;height:600px;object-fit:contain}pre{white-space:pre-wrap;font-size:13px}</style>'+
        '<h1>核心附件间距：安全收紧验证</h1><p>只使用已有合法线路；不运行 SA、A* 或候选选优。'+
        '附件与核心之间有证据的多余间距才可收紧；独立功能区保留通道。符号、引脚局部形状和安全规则不变。'+
        '每个结果重新完整验收、核对两种 SVG 并独立重绘。旧输入和默认开关不变。'+
        '这份报告不代表新引擎完整运行的对照收益。</p>'+''.join(sections)+'</html>',encoding='utf8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--placement',type=Path,action='append',required=True)
    parser.add_argument('--ground-truth',type=Path,action='append',default=[])
    parser.add_argument('--out-dir',type=Path,required=True)
    parser.add_argument('--seconds',type=float,default=1)
    args=parser.parse_args()
    if args.ground_truth and len(args.ground_truth)!=len(args.placement):
        parser.error('provide one GT per placement, or omit GT entirely')
    output=args.out_dir.resolve()
    output.mkdir(parents=True,exist_ok=True)
    records=[]
    for number,path in enumerate(args.placement):
        record=execute(path,output,args.seconds)
        record['label']=path.stem+' / '+str(number+1)
        if args.ground_truth:record['ground_truth']=str(args.ground_truth[number].resolve())
        records.append(record)
        publish(output,records)
        print(path.stem,record['report']['before'],record['report']['after'],flush=True)
    print(output/'index.html',flush=True)


if __name__=='__main__':
    main()
