"""Verify the single-questionnaire / single-HTML-report workflow in real Chrome."""
from __future__ import annotations

import argparse
from collections import Counter
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from netlist2schematic.common.sa_common import read_json,write_json


class ResultParser(HTMLParser):
    def __init__(self,target='qa-results',tag='pre'):
        super().__init__();self.target=target;self.tag=tag;self.active=False;self.parts=[]
    def handle_starttag(self,tag,attrs):
        if tag==self.tag and dict(attrs).get('id')==self.target:self.active=True
    def handle_endtag(self,tag):
        if tag==self.tag:self.active=False
    def handle_data(self,data):
        if self.active:self.parts.append(data)


def json_from_html(text,target):
    parser=ResultParser(target,'script');parser.feed(text)
    return json.loads(''.join(parser.parts))


def run_browser(chrome,html,profile,folder):
    command=[str(chrome),'--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
        '--disable-extensions','--allow-file-access-from-files','--user-data-dir='+str(profile),
        '--virtual-time-budget=7000','--dump-dom',html.as_uri()]
    result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=45,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    (folder/(html.stem+'.dom.html')).write_bytes(result.stdout)
    (folder/(html.stem+'.stderr.log')).write_bytes(result.stderr)
    parser=ResultParser();parser.feed(result.stdout.decode('utf-8',errors='replace'))
    if not parser.parts:raise AssertionError('Browser did not finish '+html.name+'; inspect DOM/logs')
    value=json.loads(''.join(parser.parts))
    if not value.get('ok'):raise AssertionError(value)
    return value


def append_test(source,script):
    before,after=source.rsplit('</body>',1)
    return before+'<script>'+script+'</script></body>'+after


def check(study_dir,chrome):
    study_dir=Path(study_dir).resolve();folder=study_dir/'researcher/qa';folder.mkdir(parents=True,exist_ok=True)
    key=read_json(study_dir/'researcher/answer-key.json')
    if len(key['questions'])<5:raise ValueError('This interaction smoke check needs at least five questions (the builder also supports smaller studies)')
    source=(study_dir/'index.html').read_text(encoding='utf8')
    init="<script>if(!localStorage.getItem('blind-review-qa-stage'))localStorage.removeItem('schematic-blind:'+JSON.parse(document.getElementById('study-data').textContent).study_id);</script>"
    source=source.replace('<script>\'use strict\';',init+'<script>\'use strict\';',1)
    test=r'''
window.addEventListener('load',async()=>{
const done=value=>{const p=document.createElement('pre');p.id='qa-results';p.textContent=JSON.stringify(value);document.body.replaceChildren(p);};
const assert=(v,m)=>{if(!v)throw Error(m);};
const hash=async text=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text))),b=>b.toString(16).padStart(2,'0')).join('');
try{
const app=window.BlindReview,C=window.ReviewCore,payload=JSON.parse(document.getElementById('study-data').textContent),N=payload.questions.length;
assert(!!app,'App must initialize');let saved=JSON.parse(localStorage.getItem('blind-review-qa-stage')||'null');
if(!saved){
 await Promise.all(payload.questions.flatMap(q=>q.images.map(src=>new Promise((resolve,reject)=>{const image=new Image();image.onload=()=>image.naturalWidth&&image.naturalHeight?resolve():reject(Error('Empty image'));image.onerror=()=>reject(Error('Image failed'));image.src=src;}))));
 const initial=app.getFeedback();assert(initial.question_count===N&&initial.answered_count===0,'Initial count');
 const zero=app.makeReport(initial);assert(C.feedbackFromHtml(zero).unanswered_count===N&&!zero.includes('NaN'),'Zero-answer report');
 for(const choice of ['A','B','C','D'])document.querySelector('[data-choice="'+choice+'"]').click();
 assert(app.getFeedback().answered_count===1,'Replacing choices must not add votes');
 document.dispatchEvent(new KeyboardEvent('keydown',{key:'a',ctrlKey:true,bubbles:true}));assert(app.getFeedback().answers.find(a=>a.choice)?.choice==='D','Shortcut must not vote');
 const reason='=HYPERLINK("x") <script'+'>unsafe</script'+'>\n第二行';
 document.getElementById('reason').value=reason;document.getElementById('reason').dispatchEvent(new Event('input',{bubbles:true}));
 const first=app.getFeedback().answers.find(a=>a.choice==='D');
 assert(document.getElementById('netlist-text').textContent===payload.questions.find(q=>q.id===first.question_id).netlist,'Netlist exact');
 document.getElementById('next').click();document.querySelector('[data-choice="C"]').click();
 const value=app.getFeedback();assert(value.answered_count===2,'Two answered');
 document.getElementById('previous').click();assert(document.getElementById('reason').value===reason,'Reason after navigation');
 localStorage.setItem('blind-review-qa-stage',JSON.stringify({phase:'restore',participant:value.participant_id,order:value.question_order,assignments:value.answers.map(a=>[a.question_id,a.a_slot]),reason}));
 location.reload();return;
}
if(saved.phase==='restore'){
 const restored=app.getFeedback();assert(restored.answered_count===2&&restored.participant_id===saved.participant,'Actual reload restores votes');
 assert(JSON.stringify(restored.question_order)===JSON.stringify(saved.order),'Reload order');
 assert(JSON.stringify(restored.answers.map(a=>[a.question_id,a.a_slot]))===JSON.stringify(saved.assignments),'Reload A/B assignments');
 assert(document.getElementById('reason').value===saved.reason,'Reload reason');
 document.getElementById('next-unanswered').click();document.querySelector('[data-choice="A"]').click();
 document.getElementById('next-unanswered').click();document.querySelector('[data-choice="B"]').click();
 const f=app.getFeedback();assert(f.answered_count===4&&f.unanswered_count===N-4&&f.status==='stopped_early','Early stop');
 document.getElementById('finish-top').click();assert(document.getElementById('results-dialog').open,'Finish dialog');
 assert(document.getElementById('result-chart').textContent.includes('25.0%')&&!document.getElementById('result-chart').textContent.includes('GT'),'No sources in pre-finish chart');
 const captures=[];const original=C.download;C.download=(name,content,mime)=>captures.push({name,content,mime});
 document.getElementById('download-feedback').click();C.download=original;
 assert(captures.length===1&&captures[0].name.endsWith('.html')&&captures[0].mime.startsWith('text/html'),'Only one HTML download');
 const report=captures[0].content, decoded=C.feedbackFromHtml(report);
 assert(decoded.answered_count===4,'Embedded raw feedback');
 const doc=new DOMParser().parseFromString(report,'text/html');
 assert(doc.querySelectorAll('img').length===2*N&&doc.querySelectorAll('pre').length===N,'Self-contained report contexts');
 assert(doc.getElementById('overall').textContent.includes('GT')&&doc.getElementById('overall').textContent.includes('算法'),'Real source chart');
 assert(JSON.parse(doc.getElementById('analysis-data').textContent).answered===4,'Embedded analysis');
 assert(document.getElementById('reason').readOnly&&[...document.querySelectorAll('[data-choice]')].every(b=>b.disabled),'Seal answers after reveal');
 const before=JSON.stringify(app.getFeedback());document.querySelector('[data-choice="C"]').dispatchEvent(new Event('click',{bubbles:true}));
 assert(JSON.stringify(app.getFeedback())===before,'No changes after finalization');
 localStorage.setItem('blind-review-qa-stage',JSON.stringify({phase:'sealed',snapshot:decoded,report_hash:await hash(report)}));
 location.reload();return;
}
assert(JSON.stringify(app.getFeedback())===JSON.stringify(saved.snapshot),'Sealed feedback survives reload');
assert(document.getElementById('reason').readOnly,'Reload remains sealed');
const report=app.makeReport(app.getFeedback());assert(await hash(report)===saved.report_hash,'Repeated report is identical');
localStorage.removeItem('blind-review-qa-stage');
done({ok:true,feedback:app.getFeedback(),report,checks:[(2*N)+' images','four options','optional reason and netlist','stable randomized A/B','reload before finish','early stop','zero-answer report','single HTML download','source chart',N+' embedded contexts','embedded raw data','frozen after reveal','reload after finish','identical re-download']});
}catch(error){done({ok:false,error:String(error),stack:error.stack});}
});'''
    harness=folder/'participant-check.html';harness.write_text(append_test(source,test),encoding='utf8')
    client=run_browser(chrome,harness,ROOT/'.cache/blind-review-single-html-qa',folder)
    feedback=json_from_html(client['report'],'feedback-data');assert feedback==client['feedback']
    analysis=json_from_html(client['report'],'analysis-data');assert analysis['answered']==4
    (folder/'SYNTHETIC-report.html').write_text(client['report'],encoding='utf8')
    newer=json.loads(json.dumps(feedback));newer['exported_at']='2099-01-01T00:00:00.000Z'
    next(a for a in newer['answers'] if a['choice']=='D')['choice']='A'
    other=json.loads(json.dumps(newer));other['participant_id']='SYNTHETIC-second-respondent'
    for answer in other['answers']:
        answer['a_slot'],answer['b_slot']=answer['b_slot'],answer['a_slot']
        answer['a_image_sha256'],answer['b_image_sha256']=answer['b_image_sha256'],answer['a_image_sha256']
        if answer['choice']=='A':answer['choice']='B'
        elif answer['choice']=='B':answer['choice']='A'
    bad_study=json.loads(json.dumps(feedback));bad_study['study_id']='WRONG-STUDY'
    bad_image=json.loads(json.dumps(other));bad_image['participant_id']='SYNTHETIC-bad-image';bad_image['answers'][0]['a_image_sha256']='wrong'
    fixtures=dict(report=client['report'],original=feedback,newer=newer,other=other,bad_study=bad_study,bad_image=bad_image,question_count=len(key['questions']))
    owner_test='const fixtures='+json.dumps(fixtures,ensure_ascii=False).replace('<','\\u003c')+';'+r'''
window.addEventListener('load',async()=>{
const done=value=>{const p=document.createElement('pre');p.id='qa-results';p.textContent=JSON.stringify(value);document.body.replaceChildren(p);};
try{
const app=window.BlindReviewAdmin,C=window.ReviewCore;
const file=(name,v)=>new File(['<!doctype html><script id="feedback-data" type="application/json">'+C.safeJson(v)+'</script'+'>'],name,{type:'text/html'});
await app.importFiles([new File([fixtures.report],'original.html',{type:'text/html'}),file('newer.html',fixtures.newer),file('duplicate.html',fixtures.original),file('other.html',fixtures.other),file('wrong-study.html',fixtures.bad_study),file('wrong-image.html',fixtures.bad_image)]);
const result=app.getAggregate();if(result.participants!==2||result.answered!==8||result.total.unanswered!==2*(fixtures.question_count-4))throw Error('Deduplication or partial denominator');
const report=app.makeReport();if(!report.includes('analysis-data'))throw Error('HTML aggregate report');
done({ok:true,aggregate:result,report,log:document.getElementById('import-log').textContent});
}catch(error){done({ok:false,error:String(error),stack:error.stack});}
});'''
    owner=folder/'organizer-check.html';owner.write_text(append_test((study_dir/'researcher/index.html').read_text(encoding='utf8'),owner_test),encoding='utf8')
    admin=run_browser(chrome,owner,ROOT/'.cache/blind-review-single-html-admin-qa',folder)
    lookup={q['id']:q for q in key['questions']};expected=Counter()
    for response in (newer,other):
        for a in response['answers']:
            result='unanswered' if a['choice'] is None else 'equal' if a['choice']=='C' else 'undetermined' if a['choice']=='D' else lookup[a['question_id']]['slot_origins'][a['a_slot'] if a['choice']=='A' else a['b_slot']]
            expected[result]+=1
    assert dict(expected)=={k:v for k,v in admin['aggregate']['total'].items() if v}
    assert json_from_html(admin['report'],'analysis-data')['participants']==2
    (folder/'SYNTHETIC-analysis.html').write_text(admin['report'],encoding='utf8')
    report=dict(status='passed',delivery='one-questionnaire-html / one-report-html',participant_checks=client['checks'],
                organizer_checks=['multiple HTML imports','source decoding','opposite A/B assignments','latest-only deduplication','study and image mismatch rejection','unanswered separate','HTML aggregate report'],
                test_data_only=True,simulated_totals=dict(expected),study_id=key['study_id'])
    write_json(folder/'browser-verification.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2));return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study-dir',default=str(ROOT/'output/blind-review'))
    parser.add_argument('--chrome',default='C:/Program Files/Google/Chrome/Application/chrome.exe')
    args=parser.parse_args(argv);check(args.study_dir,Path(args.chrome).resolve());return 0


if __name__=='__main__':raise SystemExit(main())
