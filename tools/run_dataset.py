#!/usr/bin/env python3
"""Run a directory of netlists serially and publish an embedded GT/SVG report.

Default mode leaves run_flow.py's optimization and recovery budgets untouched.
The explicit quick mode uses one 60-second attempt and, on failure or bus
fallback, one new 120-second attempt. No historical layout seeds the engine.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import redirect_stdout
from datetime import datetime
from hashlib import sha256
from html import escape
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from netlist2schematic.common.sa_common import engine_source_files
from netlist2schematic.netlist.netlist_core import _logical_lines,_partition,parse_netlist
from tools.compare_power_rails import _image_uri,_image_viewer,_svg_geometry

QUICK=(dict(wall=60,normal=50,rescue=1,spacing=1,bus=1,simplify=5),
       dict(wall=120,normal=90,rescue=10,spacing=5,bus=5,simplify=5))
EXTENSIONS={'.cir','.spi','.sp','.net','.spice'}


def digest(path):return sha256(Path(path).read_bytes()).hexdigest()


def write_json(path,value):
    path=Path(path);temporary=path.with_name(path.name+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')
    temporary.replace(path)


def inventory(folder,cell,start):
    paths=sorted(p.resolve() for p in folder.rglob('*') if p.is_file() and p.suffix.lower() in EXTENSIONS)
    rows=[]
    for number,path in enumerate(paths,start):
        gt=path.with_suffix('.svg')
        row=dict(tag=f'c{number:02d}',source=str(path),source_sha256=digest(path),
            ground_truth=str(gt) if gt.is_file() else None,
            gt_sha256=digest(gt) if gt.is_file() else None,status='pending',attempts=[])
        try:
            _,cells=_partition(_logical_lines(path),root_path=path)
            selected=cell
            if selected is None and len(cells)==1:selected=next(iter(cells))
            if selected is None and len(cells)>1:
                raise ValueError('Multiple subcircuits: specify --cell for this directory')
            devices=parse_netlist(path,cell=selected)
            if not devices:raise ValueError('The selected circuit contains no supported devices')
            row.update(cell=selected,device_count=len(devices),
                passive_count=sum(d['cls']=='PASSIVE' for d in devices.values()))
        except (ValueError,OSError) as exc:row.update(status='input_error',message=str(exc))
        rows.append(row)
    return rows


def verify(path):
    from netlist2schematic.common.sa_saved import load_placement,saved_routing_options
    from netlist2schematic.routing.sa_routing import validate_final_routes
    from netlist2schematic.rendering.render_clean import render
    before=digest(path)
    data,source,par,placement,comps,nets,index=load_placement(path)
    _,metrics,validation=validate_final_routes(par,nets,placement,index,
        list(data['routes'].items()),options=saved_routing_options(data))
    if not validation['valid'] or metrics!=data['metrics']:
        raise ValueError('Saved metrics or independent route validation failed')
    stem=path.stem.removesuffix('-placement')
    clean=path.with_name(stem+'-clean.svg');color=path.with_name(stem+'-layered.svg')
    independent=path.with_name(stem+'-independent-clean.svg')
    with path.with_name('flow.log').open('a',encoding='utf8') as stream,redirect_stdout(stream):
        render(path,independent)
    if not (_svg_geometry(clean)==_svg_geometry(color)==_svg_geometry(independent)):
        raise ValueError('SVG signal/device geometry differs from saved JSON')
    if digest(path)!=before:raise ValueError('Validation modified its input JSON')
    return dict(placement=str(path),clean_svg=str(clean),color_svg=str(color),metrics=metrics,
        routing_stage=data.get('routing_stage','normal'),
        selected_candidate=data.get('quality',{}).get('selected_candidate'),
        actual_candidates=[dict(id=c.get('candidate_id'),status=c.get('status'),
            reason=c.get('reason') or (c.get('diagnostics') or {}).get('reason'))
            for c in data.get('quality',{}).get('candidates',[])],
        independent_validation=True,svg_consistency=True,
        inline_branch_policy=data.get('passive_skeleton',{}).get('inline_port_policy'))


def run_attempt(row,budget,number,destination,args):
    folder=destination/'runs'/row['tag']/('default' if budget is None else str(budget['wall']))
    folder.mkdir(parents=True,exist_ok=False)
    command=[sys.executable,'-X','utf8','-B',str(ROOT/'run_flow.py'),row['source'],
        '--iterations',str(args.iterations),'--seed',str(args.seed),'--out-dir',str(folder)]
    if row.get('cell'):command+=['--cell',row['cell']]
    if budget is not None:
        for name,key in (('quality','normal'),('rescue','rescue'),('spacing','spacing'),
                         ('fallback','bus'),('simplify','simplify')):
            command+=['--'+name+'-seconds',str(budget[key])]
    log=folder/'flow.log';started=perf_counter()
    with log.open('w',encoding='utf8') as stream:
        try:
            process=subprocess.run(command,cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT,
                timeout=budget['wall'] if budget else None,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            rc=process.returncode
        except subprocess.TimeoutExpired:rc='wall_timeout'
    attempt=dict(number=number,command=command,budget=budget or 'run_flow_defaults',returncode=rc,
        elapsed_seconds=round(perf_counter()-started,4),log=str(log))
    row['attempts'].append(attempt)
    diagnostics=folder/'routing-diagnostics.json'
    if diagnostics.is_file():attempt['diagnostics_path']=str(diagnostics)
    files=list(folder.glob('*-placement.json'))
    if rc!=0 or len(files)!=1:return None
    try:
        result=verify(files[0]);attempt['verified_result']=result
        return result
    except (ValueError,AssertionError,OSError,KeyError,TypeError) as exc:
        attempt['validation_error']=str(exc)
        return None


def report(manifest,destination):
    rows=manifest['cases'];done=[r for r in rows if r['status'] not in ('pending','running')]
    valid=[r for r in rows if r['status']=='valid']
    def metric(value):
        return ' / '.join(f'{value[k]:g}' for k in ('wirelength','crossings','bends')) if value else '—'
    def picture(path,title):
        if not path:return '<figure><figcaption>'+escape(title)+'</figcaption><p>无图</p></figure>'
        path=Path(path)
        return '<figure><figcaption>'+escape(title)+'</figcaption><a href="#embedded-image-viewer" data-svg-preview><img loading="lazy" alt="'+escape(title,quote=True)+'" src="'+_image_uri(path)+'"></a></figure>'
    table=[];sections=[]
    for row in rows:
        name=Path(row['source']).stem;tag=row['tag'];seconds=sum(a['elapsed_seconds'] for a in row['attempts'])
        state=row['status']+' / '+row.get('routing_stage','—')
        table.append('<tr><td><a href="#'+tag+'">'+escape(tag+' · '+name)+'</a></td><td>'+escape(state)+'</td><td>'+metric(row.get('metrics'))+'</td><td>'+escape(str(row.get('selected_candidate') or '—'))+'</td><td>'+f'{seconds:.1f}'+'</td></tr>')
        diagnostics=''.join('<li>尝试'+str(a['number'])+'：'+str(a['elapsed_seconds'])+'秒；<a href="'+Path(a['log']).as_uri()+'">日志</a></li>' for a in row['attempts'])
        links=('<a href="'+Path(row['placement']).as_uri()+'">布局JSON</a> · <a href="'+Path(row['color_svg']).as_uri()+'">彩色SVG</a>' if row.get('placement') else escape(row.get('message','等待运行')))
        sections.append('<section id="'+tag+'" data-title="'+escape(name,quote=True)+'"><h2>'+escape(tag+' · '+name)+'</h2><p>'+escape(state)+'；最终信号指标：'+metric(row.get('metrics'))+'</p><div class="pictures">'+picture(row.get('ground_truth'),'GT（视觉参照）')+picture(row.get('clean_svg'),'当前默认布局策略')+'</div><p>'+links+'</p><details><summary>本图运行记录</summary><ul>'+diagnostics+'</ul><pre>'+escape(json.dumps({k:v for k,v in row.items() if k!='attempts'},ensure_ascii=False,indent=2))+'</pre></details></section>')
    stages=dict(Counter(r['routing_stage'] for r in valid))
    mode=('主flow原默认预算：正常300秒、局部救援60秒、扩距60秒、总线60秒、缩线5秒；不额外整图重试。'
          if manifest['budget_mode']=='default' else
          '本次显式选择限时验证：每张第一次最多60秒，失败或仅总线后备时，重新完整运行一次，最多120秒；两次合计最多约180秒。两次均保留默认布局、选图和恢复机制，只覆盖时间参数；完整阶段配额见manifest.json。这不是主flow默认预算。')
    document='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>test_data2 当前flow整批结果</title><style>body{font:15px/1.7 system-ui,sans-serif;background:#f2f5f9;color:#23324a;margin:24px}main{max-width:1780px;margin:auto}section{background:white;border:1px solid #dbe3ef;border-radius:10px;padding:18px;margin:25px 0}h2{font-size:20px;overflow-wrap:anywhere}.pictures{display:grid;grid-template-columns:1fr 1fr;gap:16px}figure{margin:0}figcaption{text-align:center;font-weight:600}img{width:100%;height:570px;object-fit:contain;background:white}table{border-collapse:collapse;background:white;width:100%}td,th{border:1px solid #dbe3ef;padding:8px;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#125aab}input{padding:10px;width:350px;max-width:90%}@media(max-width:900px){.pictures{grid-template-columns:1fr}}</style><main>'''
    document+='<h1>'+escape(Path(manifest['input_dir']).name)+'：当前flow整批结果</h1><p>进度 '+str(len(done))+' / '+str(len(rows))+'；完整验收成功 '+str(len(valid))+'；阶段分布 '+escape(str(stages))+'；状态 '+manifest['status']+'。</p><p>'+mode+'</p><p>每张都从原网表重新布局、SA与布线；候选2／3优先，均失败才候选1。默认主列、无源骨架和端口预检开启，额外网标／共享VDD保持关闭。下表为原生信号线长／交叉／折角，不含供电引线；GT仅作视觉参照，没有冒充可比的同口径GT指标。总线后备始终单独标明，不能视为普通布线成功。</p><p>每份成品从JSON独立复验，并检查黑白、彩色和独立重绘坐标一致。图片已内嵌，点击放大。<a href="manifest.json">运行清单、参数与哈希</a></p>'
    document+='<table><tr><th>样例</th><th>状态／阶段</th><th>信号线长／交叉／折角</th><th>候选</th><th>全部尝试耗时（秒）</th></tr>'+''.join(table)+'</table>'+''.join(sections)+'</main></html>'
    page=_image_viewer(document);temporary=destination/'index.html.tmp'
    temporary.write_text(page,encoding='utf8');temporary.replace(destination/'index.html')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input_dir',type=Path)
    parser.add_argument('--budget',choices=('default','quick'),default='default')
    parser.add_argument('--out-dir',type=Path)
    parser.add_argument('--cell',help='Explicit subcircuit for all inputs; otherwise select a unique definition')
    parser.add_argument('--iterations',type=int,default=20000)
    parser.add_argument('--seed',type=int,default=20261029)
    parser.add_argument('--start-number',type=int,default=1,help='Cosmetic report case-number offset')
    args=parser.parse_args(argv);folder=args.input_dir.expanduser().resolve()
    if not folder.is_dir():parser.error('Input directory does not exist')
    destination=(args.out_dir or ROOT/'output'/(folder.name+'-'+datetime.now().strftime('%Y%m%d-%H%M%S'))).expanduser().resolve()
    if (destination/'manifest.json').exists():parser.error('Output already contains a run; choose a new --out-dir')
    destination.mkdir(parents=True,exist_ok=True)
    cases=inventory(folder,args.cell,args.start_number)
    if not cases:parser.error('No netlists found')
    files=engine_source_files()+[ROOT/'symbol-geometry.json',Path(__file__).resolve()]
    files+=sorted((ROOT/'analog-canvas-main/packages/components/definitions').glob('*.json'))
    frozen={str(p):digest(p) for p in files}
    manifest=dict(format='dataset-flow-run-v1',status='running',input_dir=str(folder),
        budget_mode=args.budget,schedule=QUICK if args.budget=='quick' else 'run_flow_defaults',
        seed=args.seed,iterations=args.iterations,jobs=1,cases=cases,engine_and_resource_hashes=frozen)
    def snapshot():
        write_json(destination/'manifest.json',manifest);report(manifest,destination)
    started=perf_counter();snapshot()
    print('Inventory:',len(cases),'netlists;',args.budget,'budget;',destination,flush=True)
    for row in cases:
        if row['status']=='input_error':continue
        row['status']='running';snapshot();best=None
        print('START',row['tag'],Path(row['source']).name,flush=True)
        for number,budget in enumerate(QUICK if args.budget=='quick' else (None,),1):
            result=run_attempt(row,budget,number,destination,args)
            if result:
                if best is None or best['routing_stage']=='bus_fallback':best=result
                if result['routing_stage']!='bus_fallback':break
                print('BUS',row['tag'],'trying larger budget' if args.budget=='quick' and number==1 else 'budget exhausted',flush=True)
        if best is None:row.update(status='failed',message='No complete verified output within the selected attempts; see logs')
        else:row.update(best,status='valid')
        row['inputs_unchanged']=digest(row['source'])==row['source_sha256'] and (
            not row['ground_truth'] or digest(row['ground_truth'])==row['gt_sha256'])
        if not row['inputs_unchanged']:raise ValueError('Source or GT changed during the run')
        snapshot()
        print('DONE',row['tag'],row['status'],row.get('routing_stage'),row.get('metrics'),
              round(sum(a['elapsed_seconds'] for a in row['attempts']),2),flush=True)
    manifest.update(status='complete',elapsed_seconds=round(perf_counter()-started,3),
        engine_unchanged=all(digest(p)==h for p,h in frozen.items()),
        inputs_unchanged=all(digest(r['source'])==r['source_sha256'] and
            (not r['ground_truth'] or digest(r['ground_truth'])==r['gt_sha256']) for r in cases))
    snapshot()
    if not manifest['engine_unchanged'] or not manifest['inputs_unchanged']:
        raise ValueError('Input, engine, or resource changed during the run')
    print('COMPLETE',destination/'index.html',flush=True)
    return 0 if all(r['status']=='valid' for r in cases) else 1


if __name__=='__main__':raise SystemExit(main())
