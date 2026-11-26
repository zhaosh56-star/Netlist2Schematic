"""Serial, resumable fresh-netlist comparisons against a frozen engine.

GT is for the report only. Neither saved placements nor GT enter the flow.
Budgets are explicit and identical across engines. Every success is loaded
again and checked by the original connectivity/geometry validator.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
import json
import os
from pathlib import Path
import shutil
import subprocess
from statistics import median
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from netlist2schematic.common.sa_common import engine_source_files
from netlist2schematic.flow.process_budget import run_bounded, WORKER_ENV


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf8')
    temp.replace(path)


def prepare(directory, engine, folders):
    """Freeze code and discover inputs before edits, without running the flow."""
    from tools.run_dataset import inventory
    from netlist2schematic.netlist.netlist_core import parse_netlist
    if any((directory/name).exists() for name in ('baseline.json','inventory.json','frozen-engine')):
        raise ValueError('Choose a new comparison directory; existing evidence is preserved')
    directory.mkdir(parents=True,exist_ok=True)
    frozen=directory/'frozen-engine'
    hashes={}
    files=list(engine_source_files(engine))
    geometry=engine/'symbol-geometry.json'
    if geometry.is_file():files.append(geometry)
    for path in files:
        relative=path.relative_to(engine)
        target=frozen/relative;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,target);hashes[str(relative)]=digest(path)
    resources=list((ROOT/'analog-canvas-main/packages/components/definitions').glob('*.json'))
    if (ROOT/'symbol-geometry.json').is_file():resources.append(ROOT/'symbol-geometry.json')
    write(directory/'baseline.json',dict(kind='frozen_worktree',root=str(frozen),
        engine_hashes=hashes,resource_hashes={str(p.relative_to(ROOT)):digest(p) for p in resources}))
    rows=[]
    for folder in folders:
        for row in inventory(folder.resolve(),None,len(rows)+1):
            row.update(dataset=folder.name,included=False)
            if row['status']!='input_error':
                devices=parse_netlist(row['source'],cell=row.get('cell'))
                row['classes']=dict(Counter(d['cls'] for d in devices.values()))
                row['source_count']=sum(d['cls'] in ('ISRC','VSRC') for d in devices.values())
                row['included']=bool(row['passive_count'] or row['source_count'])
            rows.append(row)
    write(directory/'inventory.json',rows)
    print('Prepared',len(rows),'inputs;',sum(r['included'] for r in rows),'relevant;',frozen,flush=True)


def verify(path):
    from tools.run_dataset import verify as checked
    record = checked(path)
    data = json.loads(path.read_text(encoding='utf8'))
    skeleton = data.get('passive_skeleton', {})
    record['packing'] = {key: skeleton.get(key) for key in
        ('applied', 'stop_reason', 'insertion_search', 'terminal_search', 'normal_choice', 'current')}
    record['candidate'] = data.get('candidate_id') or record['selected_candidate']
    from netlist2schematic.common.sa_saved import load_placement
    _, _, _, placement, comps, _, index = load_placement(path)
    record['active_positions'] = {c['id']: dict(cell=list(placement.cell_of[i]),
        orient=placement.orient[i], point=list(placement.pos_of(i)))
        for i,c in enumerate(comps) if c.get('symbol') in ('nmos', 'pmos', 'npn', 'pnp')}
    checkpoint=skeleton.get('core_checkpoint',{})
    record['active_identity_and_orientation_preserved'] = all(
        entry['cell']==record['active_positions'][name]['cell']
        and entry['orient']==record['active_positions'][name]['orient']
        for name,entry in checkpoint.items() if name in record['active_positions']) if checkpoint else None
    return record


def summarize(lookup):
    """Count one latest receipt per case, separating equal-budget comparisons."""
    summary = {}
    for policy in sorted({policy for _,policy in lookup}):
        subset = [r for (case,key),r in lookup.items() if key==policy]
        valid = [r for r in subset if r['status']=='valid']
        summary[policy] = dict(total=len(subset), status=dict(Counter(r['status'] for r in subset)),
            stages=dict(Counter(r.get('verified', {}).get('routing_stage', 'failed') for r in subset)),
            complete_non_bus=sum(r['verified'].get('routing_stage')!='bus_fallback' for r in valid),
            bus_fallback=sum(r['verified'].get('routing_stage')=='bus_fallback' for r in valid),
            candidates=dict(Counter(str(r.get('verified',{}).get('candidate')) for r in valid)),
            median_seconds=median(r['seconds'] for r in subset if 'seconds' in r) if subset else None)
        choices = [r.get('verified',{}).get('packing',{}).get('normal_choice') or {} for r in valid]
        summary[policy]['normal_choices'] = dict(
            eligible=sum(bool(r.get('eligible_count')) for r in choices),
            attempted=sum(bool(r.get('trials')) for r in choices),
            applied=sum(bool(r.get('applied')) for r in choices),
            moved_modules=sum(bool(a.get('position_changed')) for r in choices for a in r.get('actions',[])),
            trials=sum(r.get('trials',0) for r in choices),
            seconds=sum(r.get('elapsed_seconds',0) for r in choices))
    pairs=[]
    def selected_cell(record):
        if 'cell' in record['signature']:return record['signature']['cell']
        command=record.get('command',[])
        return command[command.index('--cell')+1] if '--cell' in command else None
    for case in sorted({case for case,_ in lookup}):
        old,new=lookup.get((case,'baseline')),lookup.get((case,'improved'))
        if old is None or new is None:continue
        same=(old['signature']['parameters']==new['signature']['parameters']
              and old['signature']['source']==new['signature']['source']
              and selected_cell(old)==selected_cell(new))
        pair=dict(case=case,comparable=same,before=old['status'],after=new['status'])
        if same and old['status']==new['status']=='valid':
            before,after=old['verified']['metrics'],new['verified']['metrics']
            pair['metrics']={key:dict(before=before[key],after=after[key],delta=after[key]-before[key])
                             for key in ('wirelength','crossings','bends')}
            # Saved route identity is stronger than equality of three metrics.
            a=json.loads(Path(old['verified']['placement']).read_text(encoding='utf8'))
            b=json.loads(Path(new['verified']['placement']).read_text(encoding='utf8'))
            pair['same_positions']=(a.get('placement')==b.get('placement')
                                    and a.get('layout_grid')==b.get('layout_grid'))
            pair['same_routes']=a.get('routes')==b.get('routes')
        pairs.append(pair)
    measured=[p for p in pairs if 'metrics' in p]
    aggregate=dict(pairs=len(pairs),comparable=sum(p['comparable'] for p in pairs),
        new_successes=[p['case'] for p in pairs if p['comparable'] and p['before']!='valid' and p['after']=='valid'],
        new_failures=[p['case'] for p in pairs if p['comparable'] and p['before']=='valid' and p['after']!='valid'],
        both_valid=len(measured),same_positions=sum(p['same_positions'] for p in measured),
        same_routes=sum(p['same_routes'] for p in measured),metrics={})
    for key in ('wirelength','crossings','bends'):
        totals={side:sum(p['metrics'][key][side] for p in measured) for side in ('before','after','delta')}
        totals.update(improved=sum(p['metrics'][key]['delta']<0 for p in measured),
                      worsened=sum(p['metrics'][key]['delta']>0 for p in measured))
        aggregate['metrics'][key]=totals
    summary['paired']=aggregate
    return summary,pairs


def publish(directory, inventory):
    receipts = sorted((directory/'runs').glob('*/*/*/record.json'),key=lambda p:p.stat().st_mtime_ns)
    results = [dict(json.loads(p.read_text(encoding='utf8')),record_path=str(p.resolve())) for p in receipts]
    lookup = {}
    for r in results:
        lookup[(r['case'], r['policy'])] = r
    def picture(path):
        if not path or not Path(path).is_file():
            return '<p>尚无完整合法图；见运行记录。</p>'
        data = base64.b64encode(Path(path).read_bytes()).decode()
        return '<a href="#embedded-image-viewer" data-svg-preview><img loading="lazy" src="data:image/svg+xml;base64,'+data+'"></a>'
    summary,pairs = summarize(lookup)
    relevant = {r['tag'] for r in inventory if r.get('included')}
    attachment_summary, _ = summarize({key:r for key,r in lookup.items() if key[0] in relevant})
    body = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>无源附件鲁棒性对照</title>',
        '<style>body{font:15px/1.6 system-ui;margin:24px;background:#f4f6fa;color:#23324a}section{background:white;padding:20px;margin:24px 0}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}img{width:100%;height:540px;object-fit:contain}pre{white-space:pre-wrap}h2{overflow-wrap:anywhere}@media(max-width:900px){.grid{grid-template-columns:1fr}}</style>',
        '<h1>无源与源器件布局：GT / 本轮开始前 / 改进版</h1>',
        '<p>从原网表重新运行；GT仅作视觉参考。不同预算的运行单独记录，超时不等于数学无解。总线后备单独统计，不计作普通布线成功。</p>',
        '<p>本页 C 编号按当前文件清单排序，与历史报告可能不同，请以电路文件名为准。'
        '运行从原网表开始，旧成品和 GT 坐标不参与生成。</p>',
        '<p>图片已内嵌，点击可放大。共同成功样例 '+str(summary['paired']['both_valid'])+
        ' 张；位置及物理轴一致 '+str(summary['paired']['same_positions'])+
        ' 张；线路一致 '+str(summary['paired']['same_routes'])+' 张。</p>',
        '<table style="border-spacing:20px 4px"><tr><th>策略</th><th>已完成测试</th>'
        '<th>完整非总线</th><th>总线降级</th><th>失败</th></tr>'+''.join(
            '<tr><td>'+label+'</td><td>'+str(summary.get(key,{}).get('total',0))+
            '</td><td>'+str(summary.get(key,{}).get('complete_non_bus',0))+
            '</td><td>'+str(summary.get(key,{}).get('bus_fallback',0))+
            '</td><td>'+str(summary.get(key,{}).get('status',{}).get('failed',0))+'</td></tr>'
            for key,label in (('baseline','本轮基线'),('improved','改进版')))+'</table>',
        '<details><summary>统计详情：线长、交叉、折角及比较范围</summary><pre>'+
        escape(json.dumps(summary, ensure_ascii=False, indent=2))+'</pre></details>']
    body.append('<details><summary>仅含无源或源器件的输入：单独统计</summary><pre>'+
        escape(json.dumps(attachment_summary,ensure_ascii=False,indent=2))+'</pre></details>')
    notes_file=directory/'review-notes.json'
    if notes_file.is_file():
        body.append('<aside style="padding:16px;background:#fff6df"><b>本轮观察与限制</b><ul>')
        for note in json.loads(notes_file.read_text(encoding='utf8')):
            target=note.get('case')
            label='<a href="#'+escape(target)+'">'+escape(target)+'</a>：' if target else ''
            body.append('<li>'+label+escape(note['text'])+'</li>')
        body.append('</ul></aside>')
    body.append('<h2>逐图概览</h2><p>指标顺序为线长 / 交叉 / 折角。二维择优记录来自最终选中成品；'
                '“接入建议”表示器件坐标未变。“保留原位”表示没有接受更好的位置。'
                '每个候选内有源位置保持；最终选中候选改变时，两版的有源摆法可能不同。</p>'
                '<table style="border-spacing:16px 5px;text-align:left"><tr><th>电路</th>'
                '<th>旧版</th><th>新版</th><th>新版二维择优</th></tr>')
    for row in inventory:
        records = [lookup.get((row['tag'], key)) for key in ('baseline','improved')]
        if not any(records):
            continue
        numbers=[]
        for record in records:
            if record is None:numbers.append('待运行')
            elif record['status']!='valid':numbers.append(record['status'])
            else:
                m=record['verified']['metrics']
                numbers.append(' / '.join(str(m[k]) for k in ('wirelength','crossings','bends')))
        selection=(records[1] or {}).get('verified',{}).get('packing',{}).get('normal_choice') or {}
        moved=sum(bool(a.get('position_changed')) for a in selection.get('actions',[]))
        label=('移动 '+str(moved)+' 组' if moved else '接入建议' if selection.get('applied') else
               '保留原位' if selection.get('trials') else '未执行' if selection.get('eligible_count') else '不适用')
        body.append('<tr><td><a href="#'+row['tag']+'">'+escape(row['tag']+' '+Path(row['source']).stem)+
                    '</a></td>'+''.join('<td>'+escape(v)+'</td>' for v in numbers)+'<td>'+label+'</td></tr>')
    body.append('</table>')
    for row in inventory:
        records = [lookup.get((row['tag'], key)) for key in ('baseline','improved')]
        if not any(records) and row.get('status') != 'input_error':
            continue
        body.append('<section id="'+row['tag']+'"><h2>'+escape(row['tag']+' '+Path(row['source']).stem)+'</h2><div class="grid"><div><b>GT</b>'+picture(row.get('ground_truth'))+'</div>')
        for key, record in zip(('本轮基线', '改进版'), records):
            body.append('<div><b>'+key+'</b>')
            if record:
                v = record.get('verified', {})
                body.append('<p><a href="'+Path(record['record_path']).as_uri()+'">完整记录</a> · '
                            '<a href="'+Path(record['record_path']).with_name('flow.log').as_uri()+'">运行日志</a></p>')
                body.append(picture(v.get('formal_svg') or v.get('clean_svg')))
                body.append('<pre>'+escape(json.dumps(dict(status=record['status'], metrics=v.get('metrics'),
                    candidate=v.get('candidate'), stage=v.get('routing_stage'), seconds=record.get('seconds'),
                    budget=record['signature']['parameters']),
                    ensure_ascii=False, indent=2))+'</pre>')
                if v.get('packing',{}).get('normal_choice'):
                    body.append('<details><summary>二维择优诊断</summary><pre>'+escape(json.dumps(
                        v['packing']['normal_choice'],ensure_ascii=False,indent=2))+'</pre></details>')
            else:
                body.append('<p>'+escape(row.get('message','待验证'))+'</p>')
            body.append('</div>')
        body.append('</div></section>')
    followup=directory/'recovery-budget/comparison.json'
    if followup.is_file():
        body.insert(7,'<p><a href="#recovery-budget">查看困难样例的独立同参恢复对照</a></p>')
        extra=json.loads(followup.read_text(encoding='utf8'))
        latest={}
        for r in extra['results']:latest[r['case'],r['policy']]=r
        body.append('<section id="recovery-budget"><h2>困难样例：独立的恢复预算对照</h2>'
                    '<p>以下参数与上面的快速协议不同；旧版和新版在本组使用相同参数。'
                    '这里的成功不改写上方快速协议的失败记录，也没有更改默认flow预算。</p>'
                    '<details><summary>本组统计详情</summary><pre>'+
                    escape(json.dumps(extra['summary'],ensure_ascii=False,indent=2))+'</pre></details>')
        for case in sorted({case for case,_ in latest}):
            body.append('<h3>'+escape(case)+'</h3><div class="grid">')
            row=next(r for r in inventory if r['tag']==case)
            body.append('<div><b>GT</b>'+picture(row.get('ground_truth'))+'</div>')
            for key,label in (('baseline','同参旧版'),('improved','同参新版')):
                r=latest.get((case,key))
                if r is None:continue
                v=r.get('verified',{})
                body.append('<div><b>'+label+'</b>'+picture(v.get('formal_svg') or v.get('clean_svg'))+
                    '<pre>'+escape(json.dumps(dict(status=r['status'],stage=v.get('routing_stage'),
                     metrics=v.get('metrics'),seconds=r.get('seconds'),budget=r['signature']['parameters']),
                     ensure_ascii=False,indent=2))+'</pre></div>')
            body.append('</div>')
        body.append('</section>')
    body.append('</html>')
    from tools.compare_power_rails import _image_viewer
    temp=directory/'index.html.tmp'; temp.write_text(_image_viewer(''.join(body)), encoding='utf8'); temp.replace(directory/'index.html')
    write(directory/'comparison.json', dict(summary=summary, attachment_summary=attachment_summary,
        pairs=pairs, results=results))


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out-dir', type=Path, default=ROOT/'output/passive-robustness-goal')
    p.add_argument('--engine-root', type=Path, default=ROOT)
    p.add_argument('--policy', choices=('baseline','improved'))
    p.add_argument('--prepare', action='store_true',help='Freeze current code and inventory into a new output directory')
    p.add_argument('--input-dirs', nargs='+',type=Path,default=[ROOT/'test_data',ROOT/'test_data2'])
    p.add_argument('--cases', nargs='*')
    p.add_argument('--all-inputs', action='store_true',
                   help='Also run active-only inputs as compatibility controls')
    p.add_argument('--normal-seconds', type=float, default=40)
    p.add_argument('--recovery-seconds', type=float, default=5)
    p.add_argument('--wall-seconds', type=float, default=120)
    p.add_argument('--candidate-id')
    p.add_argument('--seed', type=int, default=20261029)
    p.add_argument('--iterations', type=int, default=20000)
    args=p.parse_args(argv)
    directory=args.out_dir.resolve(); engine=args.engine_root.resolve()
    if args.prepare:
        prepare(directory,engine,args.input_dirs)
        return
    if not args.policy:p.error('--policy is required when running comparisons')
    inventory=json.loads((directory/'inventory.json').read_text(encoding='utf8'))
    freeze=json.loads((directory/'baseline.json').read_text(encoding='utf8'))
    def check_resources():
        changed=[name for name,expected in freeze['resource_hashes'].items() if digest(ROOT/name)!=expected]
        if changed:raise ValueError('Frozen symbol resources changed: '+', '.join(changed))
    check_resources()
    hashes={str(path.relative_to(engine)):digest(path) for path in engine_source_files(engine)}
    parameters={k:getattr(args,k) for k in ('normal_seconds','recovery_seconds','wall_seconds','seed','iterations','candidate_id')}
    for row in inventory:
        if (row.get('status') == 'input_error'
                or not (row.get('included') or args.all_inputs)
                or args.cases and row['tag'] not in args.cases):
            continue
        signature=dict(engine=hashes, source=row['source_sha256'], cell=row.get('cell'),
            resources=freeze['resource_hashes'], parameters=parameters)
        identity=sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:16]
        target=directory/'runs'/row['tag']/args.policy/identity
        receipt=target/'record.json'
        if receipt.is_file():
            old=json.loads(receipt.read_text(encoding='utf8'))
            if old.get('status') in ('valid','failed') and old['signature']==signature:
                print('REUSED',row['tag'],args.policy,old['status'],flush=True)
                continue
            raise ValueError('Unfinished attempt exists; inspect its live process before retrying: '+str(receipt))
        if digest(row['source'])!=row['source_sha256']:
            raise ValueError('Input changed: '+row['source'])
        target.mkdir(parents=True, exist_ok=False)
        command=[sys.executable,'-X','utf8','-B',str(engine/'run_flow.py'),row['source'],
            '--out-dir',str(target),'--analog-canvas-root',str(ROOT/'analog-canvas-main'),
            '--quality-seconds',str(args.normal_seconds),'--time-seconds',str(args.wall_seconds),
            '--iterations',str(args.iterations),'--seed',str(args.seed),'--simplify-seconds','5']
        for flag in ('repair','rescue','spacing','fallback'):
            command+=['--'+flag+'-seconds',str(args.recovery_seconds)]
        if row.get('cell'):
            command+=['--cell',row['cell']]
        if args.candidate_id:
            command+=['--candidate-id',args.candidate_id]
        record=dict(case=row['tag'],policy=args.policy,signature=signature,command=command,status='running')
        write(receipt,record)
        print('START',row['tag'],args.policy,Path(row['source']).stem,flush=True)
        started=perf_counter()
        with (target/'flow.log').open('w',encoding='utf8') as stream:
            code=run_bounded(command,cwd=engine,stdout=stream,stderr=subprocess.STDOUT,
                seconds=args.wall_seconds+3,env=dict(os.environ,**{WORKER_ENV:'1','ANALOG_CANVAS_ROOT':str(ROOT/'analog-canvas-main')}))
        record.update(returncode=code,seconds=round(perf_counter()-started,4),status='failed',
                      completed_at=datetime.now(timezone.utc).isoformat())
        paths=list(target.glob('*-placement.json'))
        if code==0 and len(paths)==1:
            try:
                record.update(verified=verify(paths[0]),status='valid')
            except (ValueError,AssertionError,OSError,KeyError,TypeError) as error:
                record['validation_error']=str(error)
        diagnostics=target/'routing-diagnostics.json'
        if diagnostics.is_file():
            record['diagnostics']=str(diagnostics)
        record['inputs_unchanged']=digest(row['source'])==row['source_sha256']
        record['engine_unchanged']=hashes=={str(path.relative_to(engine)):digest(path)
                                          for path in engine_source_files(engine)}
        check_resources()
        if not record['inputs_unchanged'] or not record['engine_unchanged']:
            record.update(status='failed',validation_error='Input or engine changed during run')
        write(receipt,record); publish(directory,inventory)
        print('END',row['tag'],args.policy,record['status'],record['seconds'],record.get('verified',{}).get('metrics'),flush=True)
    publish(directory,inventory)


if __name__=='__main__':
    main()
