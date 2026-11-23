"""Short, reproducible previews and fresh-flow comparisons of branch layouts."""
from __future__ import annotations

import argparse
import base64
from contextlib import redirect_stdout
from hashlib import sha256
from html import escape
import io
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from tools.run_dataset import inventory,verify
from netlist2schematic.layout import structured_placement as sp
from netlist2schematic.plugins.organization import branch_layout as org
from netlist2schematic.rendering.sa_render import render_svg
from netlist2schematic.netlist.netlist_core import parse_netlist

SVG='{http://www.w3.org/2000/svg}'


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')


def image_uri(path):
    if not path or not Path(path).is_file():return None
    return 'data:image/svg+xml;base64,'+base64.b64encode(Path(path).read_bytes()).decode()


def annotate(path,pl,index,plan):
    tree=ET.parse(path);root=tree.getroot()
    layer=ET.SubElement(root,SVG+'g',{'data-diagnostic':'branch-regions','pointer-events':'none'})
    colors=('#2563eb','#db2777','#059669','#d97706','#7c3aed','#0891b2')
    for k,r in enumerate(plan['regions']):
        points=[pl.pos_of(index[n]) for n in r['members'] if n in index]
        if not points:continue
        xs,ys=zip(*points);x0,y0=min(xs)-28,min(ys)-36
        color=colors[k%len(colors)]
        ET.SubElement(layer,SVG+'rect',dict(x=str(x0),y=str(y0),width=str(max(xs)-x0+40),
            height=str(max(ys)-y0+42),fill=color,**{'fill-opacity':'.035','stroke':color,'stroke-dasharray':'6 5'}))
        ET.SubElement(layer,SVG+'text',dict(x=str(x0+2),y=str(y0+12),fill=color,
            **{'font-size':'11','font-family':'sans-serif'})).text='R'+str(k+1)+' '+r['kind']
    for lane in plan['lanes']:
        if len(lane['members'])<2:continue
        points=[pl.pos_of(index[n]) for n in lane['members'] if n in index]
        for a,b in zip(points,points[1:]):
            ET.SubElement(layer,SVG+'line',dict(x1=str(a[0]),y1=str(a[1]),x2=str(b[0]),y2=str(b[1]),
                stroke='#f97316',**{'stroke-width':'3','stroke-dasharray':'5 6','opacity':'.55'}))
    tree.write(path,encoding='unicode')


def preview(row,policy,identifier,folder,args):
    folder.mkdir(parents=True,exist_ok=True)
    with redirect_stdout(io.StringIO()):
        pl,cc,nn,ii,data=next(sp.create_candidates(row['source'],cell=row['cell'],iterations=0,
            seed=args.seed,candidate_ids=(identifier,),passive_skeleton='auto',branch_layout=policy,
            deadline=perf_counter()+8))
    report=data.get('layout_branches',data.get('passive_skeleton',{}).get('layout_branches',{}))
    result=dict(policy=policy,candidate=identifier,kind='layout_preview_only',
        complete_wiring=False,module_insertion_complete=pl is not None,report=report)
    if pl is None:
        result['reason']=data.get('passive_skeleton',{}).get('stop_reason','generation_failed')
        write(folder/'preview.json',result);return result
    devices=parse_netlist(row['source'],cell=row['cell'])
    plan=org.build_plan(sp.par,pl,cc,nn,ii,devices,data.get('structs',()))
    if report.get('plan'):plan=report['plan']
    metrics=org.measure(pl,ii,plan)
    path=folder/'layout-preview.svg'
    render_svg(sp.par,pl,cc,nn,ii,[],dict(wirelength=0,crossings=0,bends=0,body_pen=0,overlap=0,proximity=0),path,clean=True)
    tree=ET.parse(path)
    for text in tree.getroot().iter(SVG+'text'):
        if text.text and 'WL=' in text.text:text.text='LAYOUT PREVIEW - NO SIGNAL ROUTES'
    tree.write(path,encoding='unicode');annotate(path,pl,ii,plan)
    result.update(svg=str(path),measurement=metrics,plan=plan,
        positions={c['id']:dict(cell=list(pl.cell_of[i]),orient=pl.orient[i],point=list(pl.pos_of(i))) for i,c in enumerate(cc)})
    write(folder/'preview.json',result)
    return result


def fresh_flow(row,policy,folder,args):
    if folder.exists():
        number=1
        while (folder/('repeat-'+str(number))).exists():number+=1
        folder=folder/('repeat-'+str(number))
    for wall,normal,rescue in ((60,40,10),(120,95,15)):
        target=folder/str(wall);target.mkdir(parents=True,exist_ok=False)
        command=[sys.executable,'-X','utf8','-B',str(ROOT/'run_flow.py'),row['source'],
            '--branch-layout',policy,'--quality-seconds',str(normal),'--fallback','off',
            '--local-rescue','auto','--rescue-seconds',str(rescue),'--simplify-seconds','5',
            '--iterations',str(args.iterations),'--seed',str(args.seed),'--out-dir',str(target),
            '--formal-output','auto']
        if row['cell']:command+=['--cell',row['cell']]
        start=perf_counter()
        with (target/'flow.log').open('w',encoding='utf8') as stream:
            try:
                proc=subprocess.run(command,cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT,timeout=wall,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                code=proc.returncode
            except subprocess.TimeoutExpired:code='wall_timeout'
        record=dict(policy=policy,command=command,budget=wall,seconds=round(perf_counter()-start,3),returncode=code)
        paths=list(target.glob('*-placement.json'))
        if code==0 and len(paths)==1:
            checked=verify(paths[0]);data=json.loads(paths[0].read_text(encoding='utf8'))
            formal=list(target.glob('formal/**/*-formal.svg'))
            record.update(status='valid',verification=checked,svg=checked['clean_svg'],
                formal_svg=str(formal[0]) if formal else None,report=data.get('layout_branches'),
                selection=data.get('quality',{}).get('routing_selection'))
            write(target/'run-record.json',record);return record
        record['status']='failed'
        diagnostics=target/'routing-diagnostics.json'
        if diagnostics.is_file():record['diagnostics']=json.loads(diagnostics.read_text(encoding='utf8'))
        write(target/'run-record.json',record)
        if wall==60:print('RETRY',row['tag'],policy,flush=True)
    return record


def publish(rows,destination):
    parts=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>支路与区域布局对照</title>',
        '<style>body{font:15px system-ui;background:#f3f5f8;margin:24px;color:#172033}section{background:white;margin:18px 0;padding:18px;border-radius:12px}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}figure{margin:0;border:1px solid #d9dee8;padding:10px}img{width:100%;height:490px;object-fit:contain}pre{white-space:pre-wrap;font-size:12px}h2{font-size:19px}a{color:#2563eb}</style>',
        '<h1>支路与区域组织：GT／原算法／新算法</h1><p>优先展示完整布线成品；初排预览折叠保存，预览不是电气连线。完整成品均独立复验保存线路。所有算法仅输入网表，GT只供看图。新网表主flow默认启用 --branch-layout auto；本工具仍显式比较off／auto，保存坐标操作不重新摆件。</p>']
    for row in rows:
        parts.append('<section id="'+row['tag']+'"><h2>'+escape(row['tag']+' '+Path(row['source']).stem)+'</h2>')
        gt=image_uri(row.get('ground_truth'))
        preview_start=len(parts)
        if row.get('preview'):
            for candidate in (2,3):
                parts.append('<h3>候选'+str(candidate)+'初排预览（未布线）</h3><div class="grid">')
                parts.append('<figure>GT'+('<img src="'+gt+'">' if gt else '<p>无GT</p>')+'</figure>')
                for policy in ('off','auto'):
                    r=row['preview'][policy][str(candidate)];uri=image_uri(r.get('svg'))
                    summary=json.dumps(dict(status=r.get('reason','preview'),metrics=r.get('measurement'),
                        applied=r.get('report',{}).get('applied')),ensure_ascii=False)
                    parts.append('<figure>'+policy+('<img src="'+uri+'">' if uri else '<p>模块插入未完成，没有完整预览。</p>')+'<pre>'+escape(summary)+'</pre></figure>')
                parts.append('</div>')
        preview_markup=parts[preview_start:]
        del parts[preview_start:]
        if row.get('flow'):
            policies=[p for p in ('off','auto') if row['flow'].get(p,{}).get('status')=='valid']
            parts.append('<h3>完整流程成品（SA＋A*＋缩线）</h3>')
            for policy,r in row['flow'].items():
                if r.get('status')!='valid':
                    parts.append('<p>'+escape(policy+'：本次未布通；耗时 '+str(r.get('seconds'))+' 秒。')+'</p>')
            parts.append('<div class="grid" style="grid-template-columns:repeat('+str(len(policies)+1)+',minmax(0,1fr))"><figure>GT'+('<img src="'+gt+'">' if gt else '')+'</figure>')
            for policy in policies:
                r=row['flow'].get(policy,{})
                if not r.get('formal_svg') and r.get('svg'):
                    files=list(Path(r['svg']).parent.glob('formal/**/*-formal.svg'))
                    if files:r['formal_svg']=str(files[0])
                uri=image_uri(r.get('formal_svg') or r.get('svg'))
                summary=dict(status=r.get('status'),seconds=r.get('seconds'),
                    metrics=r.get('verification',{}).get('metrics'),stage=r.get('verification',{}).get('routing_stage'),
                    candidate=r.get('verification',{}).get('selected_candidate'),applied=(r.get('report') or {}).get('applied'))
                if not r:
                    summary['status']='not_run'
                    missing='<p>本轮未重跑该侧完整流程，可展开查看初排。</p>'
                else:missing='<p>本次尝试未得到完整合法成品。</p>'
                parts.append('<figure>'+policy+('<img src="'+uri+'">' if uri else missing)+'<pre>'+escape(json.dumps(summary,ensure_ascii=False))+'</pre></figure>')
            parts.append('</div>')
        if preview_markup:
            parts.append('<details><summary>查看初排及区域标注（没有运行SA／A*）</summary>')
            parts.extend(preview_markup);parts.append('</details>')
        parts.append('</section>')
    parts.append('</html>');(destination/'index.html').write_text(''.join(parts),encoding='utf8')
    write(destination/'comparison.json',rows)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cases',nargs='+',default=['c04','c20','c21','c22','c24','c33'])
    p.add_argument('--phase',choices=('preview','flow'),default='preview')
    p.add_argument('--seed',type=int,default=20261029)
    p.add_argument('--iterations',type=int,default=20000)
    p.add_argument('--out-dir',type=Path,default=ROOT/'output/branch-organization')
    args=p.parse_args(argv);destination=args.out_dir.resolve();destination.mkdir(parents=True,exist_ok=True)
    manifest=destination/'comparison.json';existing=json.loads(manifest.read_text(encoding='utf8')) if manifest.is_file() else []
    with redirect_stdout(io.StringIO()):cases=inventory(ROOT/'test_data',None,1)+inventory(ROOT/'test_data2',None,26)
    by_id={r['tag']:r for r in existing}
    for case in cases:
        if case['tag'] not in args.cases:continue
        if case['status']=='input_error':raise ValueError(case['message'])
        row=by_id.setdefault(case['tag'],case)
        if row['source_sha256']!=case['source_sha256']:raise ValueError('Source changed: '+case['tag'])
        # Code fingerprint keeps independent new runs from overwriting old outputs.
        digest=sha256(json.dumps(dict(seed=args.seed,iterations=args.iterations,
            source_hash=case['source_sha256'],cell=case['cell']),sort_keys=True).encode())
        for source in sorted((ROOT/'netlist2schematic').rglob('*.py')):
            digest.update(source.relative_to(ROOT).as_posix().encode());digest.update(source.read_bytes())
        digest.update(Path(__file__).read_bytes());digest.update((ROOT/'symbol-geometry.json').read_bytes())
        fingerprint=digest.hexdigest()[:12]
        if args.phase=='preview':
            row['preview']={}
            for policy in ('off','auto'):
                row['preview'][policy]={}
                for candidate in (2,3):
                    print('PREVIEW',case['tag'],policy,candidate,flush=True)
                    row['preview'][policy][str(candidate)]=preview(case,policy,candidate,
                        destination/'preview'/case['tag']/policy/fingerprint/str(candidate),args)
        else:
            row['flow']={}
            for policy in ('off','auto'):
                print('FLOW',case['tag'],policy,flush=True)
                row['flow'][policy]=fresh_flow(case,policy,destination/'flow'/case['tag']/policy/fingerprint,args)
        publish(list(by_id.values()),destination)
    print('REPORT',destination/'index.html',flush=True)


if __name__=='__main__':main()
