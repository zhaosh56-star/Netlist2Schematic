"""Fresh, serial source-only dataset validation with no automatic bus output.

Run from any working directory. Historical placements and GT coordinates are
never inputs to the child flow. Ground truth is read only for the report.
"""
from contextlib import redirect_stdout
from copy import deepcopy
from hashlib import sha256
from html import escape
import argparse
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from netlist2schematic.common.sa_saved import load_placement,saved_routing_options
from netlist2schematic.netlist.netlist_core import _logical_lines,_partition,parse_netlist
from netlist2schematic.recovery.labeled_geometry import is_labeled_layout,verify_saved
from netlist2schematic.routing.routing_rules import build_scene
from netlist2schematic.routing.sa_routing import validate_final_routes
from netlist2schematic.rendering.render_clean import render
from measure_schematic_wires import svg_routes,measure

PYTHON=ROOT/'.venv/Scripts/python.exe'
SCHEDULE=(dict(wall=60,normal=40,rescue=3,spacing=2,labels=10,simplify=3),
          dict(wall=120,normal=75,rescue=10,spacing=10,labels=20,simplify=3))
read=lambda p:json.loads(Path(p).read_text(encoding='utf-8'))
write=lambda p,d:Path(p).write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8')
digest=lambda p:sha256(Path(p).read_bytes()).hexdigest()


def engine_hash():
    paths=sorted((ROOT/'netlist2schematic').rglob('*.py'))+[ROOT/'symbol-geometry.json',ROOT/'run_flow.py']
    paths+=sorted((ROOT/'analog-canvas-main/packages/components/definitions').glob('*.json'))
    return sha256('\n'.join(str(p.relative_to(ROOT))+' '+digest(p) for p in paths).encode()).hexdigest()


def inventory():
    rows=[]
    for dataset in ('test_data','test_data2'):
        for source in sorted((ROOT/dataset).rglob('*')):
            if not source.is_file() or source.suffix.lower() not in ('.cir','.spi','.sp','.net'):
                continue
            gt=source.with_suffix('.svg')
            row=dict(tag=f'c{len(rows)+1:02d}',dataset=dataset,source=str(source),
                source_sha256=digest(source),ground_truth=str(gt) if gt.exists() else None,
                gt_sha256=digest(gt) if gt.exists() else None,status='pending',attempts=[])
            try:
                _,cells=_partition(list(_logical_lines(source)),root_path=source)
                cell='dut' if 'dut' in cells else next(iter(cells)) if len(cells)==1 else None
                if len(cells)>1 and cell is None:raise ValueError('Multiple cells require an explicit selection')
                devices=parse_netlist(source,cell=cell)
                if not devices:raise ValueError('Empty circuit')
                row.update(cell=cell,device_count=len(devices),
                           passive_count=sum(d['cls']=='PASSIVE' for d in devices.values()))
            except (ValueError,OSError) as exc:row.update(status='input_error',message=str(exc))
            rows.append(row)
    return rows


def strokes(path):
    return sorted(n.get('points','') for n in ET.parse(path).getroot().iter()
                  if n.tag.split('}')[-1]=='polyline')


def verify(path,log):
    data,_,par,pl,comps,nets,index=load_placement(path)
    if data.get('routing_stage')=='bus_fallback':raise AssertionError('BUS output is forbidden')
    options=saved_routing_options(data)
    with log.open('a',encoding='utf-8') as stream,redirect_stdout(stream):
        if is_labeled_layout(data):routes,metrics,validation=verify_saved(par,nets,pl,index,data,options=options)
        else:routes,metrics,validation=validate_final_routes(par,nets,pl,index,list(data['routes'].items()),options=options)
        redraw=path.parent/'independent-clean.svg';render(path,redraw)
    assert validation['valid'] and metrics==data['metrics']
    stem=path.name.replace('-placement.json','')
    assert strokes(redraw)==strokes(path.parent/(stem+'-clean.svg'))==strokes(path.parent/(stem+'-layered.svg'))
    report=data.get('labeled_recovery',{})
    if report:
        assert not report.get('history_used') and not report.get('bus_used')
        assert set(report['source_placement'])==set(data['placement'])
        for ident,before in report['source_placement'].items():
            after=report['final_placement'][ident]
            assert all(before[k]==after[k]==data['placement'][ident][k] for k in ('row','col','orient'))
            actual=pl.pos_of(index[ident])
            assert all(abs(actual[k]-after['xy'][k])<1e-6 for k in (0,1))
            if report['physical_layout_preserved']:
                assert all(abs(actual[k]-before['xy'][k])<1e-6 for k in (0,1))
    scene=build_scene(par,nets,pl,index,options=options)
    signal={net:list(lines) for net,lines in routes}
    actual=measure(signal)['metrics']
    for key in ('wirelength','crossings','bends'):assert abs(actual[key]-metrics[key])<1e-5,(key,actual,metrics)
    visible=deepcopy(signal)
    for net in scene.power_nets:visible[net]=list(scene.fixed_segments.get(net,()))
    return dict(placement=str(path),routing_stage=data.get('routing_stage','normal'),
        metrics=metrics,visible_metrics=measure(visible)['metrics'],
        selected_candidate=data.get('quality',{}).get('selected_candidate'),
        labels=validation.get('label_count',0),labeled_nets=validation.get('labeled_nets',0),
        wire_only_complete=validation.get('wire_only_complete',True),scale=report.get('scale',1),
        spacing_scale=data.get('spacing_retry',{}).get('scale',1),
        logical_layout_preserved=True,independent_validation=True,svg_consistency=True)


def metric(value):
    return ' / '.join(f'{value[k]:g}' for k in ('wirelength','crossings','bends')) if value else '—'


def snapshot(folder,manifest):
    write(folder/'manifest.json',manifest)
    rows=manifest['cases'];done=[r for r in rows if r['status'] not in ('pending','running')]
    valid=[r for r in rows if r['status']=='valid']
    totals={kind:{k:round(sum(r.get(kind,{}).get(k,0) for r in valid),6)
                  for k in ('wirelength','crossings','bends')} for kind in ('gt_metrics','visible_metrics')}
    comparison=dict(status=manifest['status'],summary=dict(inputs=len(rows),finished=len(done),valid=len(valid),
        bus_count=sum(r.get('routing_stage')=='bus_fallback' for r in rows),
        label_recovery_count=sum(r.get('routing_stage')=='labeled_recovery' for r in rows),
        labels=sum(r.get('labels',0) for r in rows),totals=totals),cases=rows)
    write(folder/'comparison.json',comparison)
    columns=('tag','dataset','status','routing_stage','selected_candidate','labels','labeled_nets','scale','spacing_scale',
             'gt_wirelength','gt_crossings','gt_bends','visible_wirelength','visible_crossings','visible_bends',
             'logical_layout_preserved','independent_validation','elapsed_seconds','source','placement')
    with (folder/'comparison.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader()
        for row in rows:
            item={k:row.get(k) for k in columns}
            for prefix,field in (('gt','gt_metrics'),('visible','visible_metrics')):
                for key in ('wirelength','crossings','bends'):item[prefix+'_'+key]=row.get(field,{}).get(key)
            item['elapsed_seconds']=round(sum(a['seconds'] for a in row['attempts']),3)
            writer.writerow(item)
    rel=lambda p:escape(os.path.relpath(p,folder).replace('\\','/'),quote=True)
    parts=['''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>无总线恢复：全部数据集</title><style>body{font:15px "Microsoft YaHei",sans-serif;background:#eef2f7;color:#243449;margin:24px}article{background:white;padding:16px;margin:20px 0;border-radius:10px}.pictures{display:grid;grid-template-columns:1fr 1fr;gap:16px}img{width:100%;height:490px;object-fit:contain}figure{margin:0}table{border-collapse:collapse;width:100%;background:white}td,th{padding:8px;border:1px solid #cdd7e4}.note{padding:14px;background:#fff0cc;line-height:1.8}.controls{position:sticky;top:0;background:#eef2f7;padding:10px}input,select{padding:8px}a{color:#175c9d}pre{white-space:pre-wrap}</style><h1>保留布局的无总线恢复：test_data＋test_data2</h1>''']
    parts.append(f'<p>进度 {len(done)}/{len(rows)}；验收成功 {len(valid)}；总线0为硬要求；状态 {manifest["status"]}。</p>')
    parts.append('''<p class="note">每张由原网表重新生成，算法不读取GT坐标或历史成品。串行，先60秒，未完成最多再120秒，完整参数见清单。恢复图保留本次器件行列和朝向，仅允许扩大间距；同名网标补齐各局部组。表格比较全部可见导线（含供电、含网标短引线），不计文字和符号笔画；网标省略的长线不是A*线长优化，GT也可能使用网标。GT仅作视觉参照，未在本轮逐端子证明与网表等价。</p>''')
    retry=manifest.get('label_reserve_retry')
    if retry:
        tags='、'.join(retry['cases']);budget=retry['budget']
        parts.append(f'<p class="note">追加验证：{tags}在上述两轮未完成后，各补一次{budget["wall"]}秒上限的全新运行，其中网标恢复预留{budget["labels"]}秒。参数按同一规则调整，未改变算法或读取旧成品。扩距栏记录最后一次公共轴扩距倍率；原始轴及完整来源保存在布局JSON。</p>')
    parts.append('<p><a href="comparison.csv">CSV</a> · <a href="comparison.json">指标与摘要</a> · <a href="manifest.json">命令与日志清单</a></p><div class="controls"><input id="query" placeholder="筛选图号或名称"><select id="stage"><option value="">所有阶段</option><option value="labeled_recovery">仅网标恢复</option><option value="normal">仅正常出图</option></select></div>')
    parts.append('<table><tr><th>图</th><th>阶段</th><th>GT：线长／交叉／折角</th><th>生成：线长／交叉／折角</th><th>网标／最后扩距</th><th>验收</th></tr>')
    for row in rows:
        parts.append(f'<tr><td><a href="#{row["tag"]}">{row["tag"]}</a></td><td>{row.get("routing_stage",row["status"])}</td><td>{metric(row.get("gt_metrics"))}</td><td>{metric(row.get("visible_metrics"))}</td><td>{row.get("labels","—")}／{row.get("spacing_scale","—")}</td><td>{row.get("independent_validation","—")}</td></tr>')
    parts.append('</table>')
    for row in rows:
        parts.append(f'<article id="{row["tag"]}" data-stage="{row.get("routing_stage","")}" data-search="{escape(row["tag"]+" "+Path(row["source"]).name,quote=True)}"><h2>{row["tag"]} · {escape(Path(row["source"]).name)}</h2><p>{row.get("routing_stage",row["status"])}；信号指标 {metric(row.get("metrics"))}；网标 {row.get("labels",0)}；最后轴扩距 {row.get("spacing_scale",1)}。</p><div class="pictures">')
        if row.get('ground_truth'):parts.append(f'<figure><figcaption>GT</figcaption><a href="{rel(row["ground_truth"])}"><img loading="lazy" src="{rel(row["ground_truth"])}"></a></figure>')
        if row.get('placement'):
            p=Path(row['placement']);svg=p.with_name(p.name.replace('-placement.json','-clean.svg'))
            parts.append(f'<figure><figcaption>本次生成</figcaption><a href="{rel(svg)}"><img loading="lazy" src="{rel(svg)}"></a><p><a href="{rel(p)}">布局JSON</a></p></figure>')
        parts.append('</div>')
        for attempt in row['attempts']:parts.append(f'<p>{attempt["budget"]["wall"]}秒尝试：{attempt["seconds"]:.2f}秒；<a href="{rel(attempt["log"])}">日志</a>；返回{attempt["returncode"]}</p>')
        if row.get('message'):parts.append('<pre>'+escape(row['message'])+'</pre>')
        parts.append('</article>')
    parts.append('''<script>function filter(){let q=document.querySelector('#query').value.toLowerCase(),s=document.querySelector('#stage').value;for(let a of document.querySelectorAll('article'))a.hidden=!(a.dataset.search.toLowerCase().includes(q)&&(!s||a.dataset.stage===s))}document.querySelector('#query').oninput=filter;document.querySelector('#stage').onchange=filter;let p=new URLSearchParams(location.search);if(p.has('case')){document.querySelector('#query').value=p.get('case');filter()}</script></html>''')
    (folder/'index.html').write_text('\n'.join(parts),encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir',default=str(ROOT/'output/no-bus-datasets'))
    parser.add_argument('--inventory-only',action='store_true')
    args=parser.parse_args();folder=Path(args.out_dir).resolve();folder.mkdir(parents=True,exist_ok=True)
    fingerprint=engine_hash()
    if (folder/'manifest.json').exists():
        manifest=read(folder/'manifest.json')
        if manifest['engine_sha256']!=fingerprint:raise ValueError('Engine changed; use a new output directory')
    else:
        manifest=dict(status='inventory',engine_sha256=fingerprint,schedule=SCHEDULE,
                      seed=20261029,iterations=20000,history_input_used=False,cases=inventory())
        for row in manifest['cases']:
            if row.get('ground_truth'):
                try:row['gt_metrics']=svg_routes(row['ground_truth'])['metrics']
                except ValueError as exc:row['gt_measurement_error']=str(exc)
    snapshot(folder,manifest)
    print(json.dumps(dict(event='inventory',count=len(manifest['cases']),
        errors=sum(r['status']=='input_error' for r in manifest['cases'])),ensure_ascii=False),flush=True)
    if args.inventory_only:return
    manifest['status']='running';snapshot(folder,manifest)
    for row in manifest['cases']:
        if row['status'] in ('valid','input_error','failed'):continue
        for budget in SCHEDULE[len(row['attempts']):]:
            row['status']='running';snapshot(folder,manifest)
            case_folder=folder/'runs'/row['tag']/str(budget['wall']);case_folder.mkdir(parents=True,exist_ok=False)
            log=case_folder/'flow.log'
            command=[str(PYTHON),'-X','utf8','-B',str(ROOT/'run_flow.py'),row['source'],
                '--iterations','20000','--seed','20261029','--quality-seconds',str(budget['normal']),
                '--rescue-seconds',str(budget['rescue']),'--spacing-seconds',str(budget['spacing']),
                '--label-seconds',str(budget['labels']),'--simplify-seconds',str(budget['simplify']),
                '--out-dir',str(case_folder)]
            if row.get('cell'):command+=['--cell',row['cell']]
            print(json.dumps(dict(event='start',case=row['tag'],budget=budget['wall']),ensure_ascii=False),flush=True)
            started=perf_counter()
            with log.open('w',encoding='utf-8') as stream:
                process=subprocess.Popen(command,cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                try:code=process.wait(timeout=budget['wall'])
                except subprocess.TimeoutExpired:
                    if os.name=='nt':subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
                    else:process.kill()
                    process.wait();code='wall_timeout'
            attempt=dict(command=command,budget=budget,log=str(log),returncode=code,
                         seconds=round(perf_counter()-started,3))
            row['attempts'].append(attempt)
            placements=list(case_folder.glob('*-placement.json'))
            if code==0 and len(placements)==1:
                try:
                    row.update(verify(placements[0],log),status='valid')
                    break
                except (ValueError,OSError,AssertionError) as exc:attempt['verification_error']=str(exc)
            diagnostic=case_folder/'routing-diagnostics.json'
            if diagnostic.exists():attempt['diagnostics']=read(diagnostic)
            snapshot(folder,manifest)
        else:row.update(status='failed',message='60秒及120秒均未完成；未输出总线，具体原因见日志与诊断。')
        assert digest(row['source'])==row['source_sha256']
        if row.get('ground_truth'):assert digest(row['ground_truth'])==row['gt_sha256']
        snapshot(folder,manifest)
        print(json.dumps(dict(event='done',case=row['tag'],status=row['status'],
            stage=row.get('routing_stage'),labels=row.get('labels'),metrics=row.get('metrics')),ensure_ascii=False),flush=True)
    assert engine_hash()==fingerprint,'Engine or symbols changed during the batch'
    assert not any(r.get('routing_stage')=='bus_fallback' for r in manifest['cases'])
    manifest['status']='complete';snapshot(folder,manifest)
    print(json.dumps(dict(event='complete',total=len(manifest['cases']),
        valid=sum(r['status']=='valid' for r in manifest['cases'])),ensure_ascii=False),flush=True)


if __name__=='__main__':main()
