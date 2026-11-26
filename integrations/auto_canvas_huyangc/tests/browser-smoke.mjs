// Real deployed-editor test with Chrome DevTools; no frontend dependency install.
// node browser-smoke.mjs --origin URL --input FILE --out-dir EMPTY --chrome EXE --jobs-root DIR
import { spawn, spawnSync } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync, existsSync, readdirSync } from 'node:fs';
import { resolve, join } from 'node:path';
import assert from 'node:assert/strict';

const flags=new Map();
for(let i=2;i<process.argv.length;i+=2) flags.set(process.argv[i],process.argv[i+1]);
for(const key of ['--origin','--input','--out-dir','--chrome','--jobs-root'])
  if(!flags.get(key)) throw new Error('Missing '+key);
const origin=flags.get('--origin'),input=resolve(flags.get('--input')),out=resolve(flags.get('--out-dir'));
if(existsSync(out) && readdirSync(out).length) throw new Error('Choose an empty browser-test output directory');
mkdirSync(out,{recursive:true});
const profile=join(out,'profile'),downloads=join(out,'downloads');
mkdirSync(downloads);
const errors=[],steps=[];
const wait=ms=>new Promise(r=>setTimeout(r,ms));
async function until(fn,seconds=20) {
  const deadline=Date.now()+seconds*1000;
  while(Date.now()<deadline) {const value=await fn();if(value) return value;await wait(100);}
  throw new Error('Timed out waiting for browser state');
}
class CDP {
  constructor(ws){this.ws=ws;this.next=0;this.pending=new Map();this.events=[];
    ws.addEventListener('message',event=>{const m=JSON.parse(event.data);
      if(m.id){const p=this.pending.get(m.id);if(!p)return;this.pending.delete(m.id);m.error?p.reject(new Error(JSON.stringify(m.error))):p.resolve(m.result);}
      else {this.events.push(m);if(m.method==='Runtime.exceptionThrown')errors.push(m.params);}
    });
  }
  static async open(url){const ws=new WebSocket(url);await new Promise((r,j)=>{ws.addEventListener('open',r,{once:true});ws.addEventListener('error',j,{once:true});});return new CDP(ws);}
  call(method,params={}){return new Promise((resolveCall,reject)=>{const id=++this.next;
    const timer=setTimeout(()=>{this.pending.delete(id);reject(new Error('CDP timeout '+method));},30000);
    this.pending.set(id,{resolve:r=>{clearTimeout(timer);resolveCall(r);},reject:e=>{clearTimeout(timer);reject(e);}});
    this.ws.send(JSON.stringify({id,method,params}));});}
  async evaluate(expression){const r=await this.call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true,userGesture:true});
    if(r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;}
}

const chrome=spawn(flags.get('--chrome'),['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
  '--remote-debugging-address=127.0.0.1','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],
  {windowsHide:true,stdio:'ignore'});
let cdp;
const status=()=>cdp.evaluate(`document.querySelector('[data-testid="status"]')?.textContent || ''`);
const click=selector=>cdp.evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e)throw new Error('Missing control');if(e.disabled)throw new Error('Disabled control');e.click();return true;})()`);
const save=async name=>{
  await click('[data-testid="project-menu"] > summary');
  const mark=cdp.events.length;
  await cdp.evaluate(`(()=>{const buttons=[...document.querySelectorAll('[data-testid="project-menu"] button')];const direct=buttons.find(e=>e.getAttribute('aria-label')==='Export Project File…' || e.textContent.trim()==='Export Project File…');if(direct){direct.click();return;}const menu=buttons.find(e=>e.textContent.replace(/[\\s›]/g,'')==='Export');if(!menu)throw new Error('No export menu');menu.click();})()`);
  const direct=await cdp.evaluate(`!![...document.querySelectorAll('button')].find(e=>e.getAttribute('aria-label')==='Export Project File…')`);
  if(direct)await cdp.evaluate(`document.querySelector('button[aria-label="Export Project File…"]').click()`);
  const event=await until(()=>cdp.events.slice(mark).find(e=>e.method==='Browser.downloadProgress' && e.params.state==='completed'));
  const guid=event.params.guid;
  assert.match(guid,/^[a-zA-Z0-9-]+$/);
  const data=readFileSync(join(downloads,guid));
  writeFileSync(join(out,name+'.icproj.json'),data);
  return JSON.parse(data);
};
const canonical=v=>Array.isArray(v)?v.map(canonical):v && typeof v==='object'?Object.fromEntries(Object.keys(v).sort().filter(k=>k!=='revision').map(k=>[k,canonical(v[k])])):v;
const content=p=>canonical(p.documents);
async function screenshot(name){const snap=await cdp.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});writeFileSync(join(out,name+'.png'),Buffer.from(snap.data,'base64'));}
const jobsRoot=resolve(flags.get('--jobs-root'));
const jobIds=()=>existsSync(jobsRoot)?readdirSync(jobsRoot).filter(n=>/^[0-9a-f]{32}$/.test(n)):[];

try {
  const port=await until(()=>{const path=join(profile,'DevToolsActivePort');return existsSync(path)?readFileSync(path,'utf8').split(/\r?\n/)[0]:null;});
  const tab=await (await fetch('http://127.0.0.1:'+port+'/json/new?'+encodeURIComponent('about:blank'),{method:'PUT'})).json();
  cdp=await CDP.open(tab.webSocketDebuggerUrl);
  await cdp.call('Page.enable');await cdp.call('Runtime.enable');
  await cdp.call('Browser.setDownloadBehavior',{behavior:'allowAndName',downloadPath:downloads,eventsEnabled:true});
  await cdp.call('Page.addScriptToEvaluateOnNewDocument',{source:`localStorage.setItem('icm.editor.mode','draw');`});
  await cdp.call('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await cdp.call('Page.navigate',{url:origin+'/editor'});
  await until(()=>cdp.evaluate(`!!document.querySelector('[data-testid="schematic-canvas"]')`),30);
  const importingSpice=flags.get('--import-spice')==='true';
  const importJobs=new Set(jobIds());
  const dom=await cdp.call('DOM.getDocument');
  const file=await cdp.call('DOM.querySelector',{nodeId:dom.root.nodeId,
    selector:importingSpice?'[data-testid="import-netlist-file"]':'[data-testid="project-file"]'});
  await cdp.call('DOM.setFileInputFiles',{nodeId:file.nodeId,files:[input]});
  if(importingSpice) {
    await until(async()=>/Auto Layout (applied|left|could|failed)|import failed|failed to import/iu.test(await status()),
      Number(flags.get('--wait-seconds') || 360));
    const final=await status();console.log('SPICE import and automatic layout:',final);
    await screenshot('after');
    assert.match(final,/Auto Layout applied/u);
    const compatibility=await cdp.evaluate('globalThis.__netlist2schematicImportReport');
    assert.equal(compatibility?.version,'spice-import-v1');
    const newJob=await until(()=>jobIds().find(id=>!importJobs.has(id)));
    const job=await until(()=>{const p=join(jobsRoot,newJob,'web-job.json');return existsSync(p)?JSON.parse(readFileSync(p,'utf8')):null;});
    assert.equal(job.status,'succeeded');
    assert.ok(job.results.length>0 && job.results.every(r=>r.resultKind!=='editable_draft'));
    const after=await save('after');
    const original=JSON.parse(readFileSync(join(jobsRoot,newJob,'source.icproj.json'),'utf8'));
    const originalIds=new Set(original.documents.flatMap(d=>d.instances).map(i=>i.id));
    const devices=after.documents.flatMap(d=>d.instances).filter(i=>originalIds.has(i.id));
    assert.equal(devices.length,originalIds.size);
    if(flags.has('--expect-devices'))assert.equal(devices.length,Number(flags.get('--expect-devices')));
    for(const change of compatibility.changes.filter(c=>c.code==='named_opamp'))
      assert.ok(devices.some(i=>i.id===change.nativeReference));
    const guidance=await cdp.evaluate(`({visible:document.querySelectorAll('[data-testid="flightline"]').length,total:document.querySelector('[data-testid="editor-test-telemetry"] [data-testid="flightline-count"]')?.textContent ?? null})`);
    assert.equal(guidance.visible,0);
    if(guidance.total!==null)assert.equal(Number(guidance.total),0);
    steps.push({step:'real-spice-import-and-python-layout',status:'passed',jobId:newJob,message:final,
      devices:devices.length,compatibility,guidance});
    const undo=await cdp.evaluate(`document.querySelector('[data-testid="draw-tool-undo"]')?'[data-testid="draw-tool-undo"]':'button[aria-label="Undo"]'`);
    const redo=await cdp.evaluate(`document.querySelector('[data-testid="draw-tool-redo"]')?'[data-testid="draw-tool-redo"]':'button[aria-label="Redo"]'`);
    await click(undo);await wait(250);
    const undone=await save('undone');assert.notDeepEqual(content(undone),content(after));
    await click(redo);await wait(250);
    const redone=await save('redone');assert.deepEqual(content(redone),content(after));
    steps.push({step:'imported-project-save-undo-redo',status:'passed'});
    assert.equal(errors.length,0);
    writeFileSync(join(out,'report.json'),JSON.stringify({status:'passed',origin,input,steps,errors},null,2));
    console.log('SPICE BROWSER ACCEPTANCE PASSED',steps.length);
  } else {
  await until(()=>cdp.evaluate(`!document.querySelector('[data-testid="auto-layout-button"]')?.disabled`));
  const before=await save('before');await screenshot('before');
  const beforeGuidance=flags.get('--expect-no-flightlines')==='true'?
    await cdp.evaluate(`({visible:document.querySelectorAll('[data-testid="flightline"]').length,total:document.querySelector('[data-testid="editor-test-telemetry"] [data-testid="flightline-count"]')?.textContent ?? null})`):null;
  if(flags.get('--open-only')==='true') {
    const guidance=await cdp.evaluate(`({visible:document.querySelectorAll('[data-testid="flightline"]').length,total:document.querySelector('[data-testid="editor-test-telemetry"] [data-testid="flightline-count"]')?.textContent ?? null})`);
    assert.ok(guidance.visible>0,'A draft must retain real pending-connection guides');
    if(guidance.total!==null)assert.ok(Number(guidance.total)>0);
    steps.push({step:'saved-editable-draft-with-real-flightlines',status:'passed',...guidance});
    await cdp.evaluate(`(()=>{const b=[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='Draw');b?.click();})()`);
    await wait(250);
    await cdp.evaluate(`document.querySelector('[data-testid="flightline-hit"]').dispatchEvent(new MouseEvent('click',{bubbles:true}))`);
    const wireStatus=await status();
    assert.match(wireStatus,/Wire source/);
    steps.push({step:'pending-connection-can-start-manual-wiring',status:'passed',message:wireStatus});
    await cdp.call('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
    await cdp.call('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
    const cancelled=await save('after-wire-cancel');assert.deepEqual(content(cancelled),content(before));
    await screenshot('draft');
    writeFileSync(join(out,'report.json'),JSON.stringify({status:'passed',origin,input,steps,errors},null,2));
    console.log('EDITABLE DRAFT BROWSER CHECK PASSED',guidance.visible,'guides');
  } else {
  console.log('Browser input opened; clicking original Auto Layout button');
  const oldJobs=new Set(jobIds());
  await click('[data-testid="auto-layout-button"]');
  await until(async()=>/^Auto Layout (applied|left|could|failed)/.test(await status()),Number(flags.get('--wait-seconds') || 90));
  const final=await status();console.log('First result:',final);
  await screenshot('after');
  assert.match(final,/^Auto Layout applied/);
  if(flags.get('--expect-no-flightlines')==='true') {
    const guidance=await cdp.evaluate(`({visible:document.querySelectorAll('[data-testid="flightline"]').length,total:document.querySelector('[data-testid="editor-test-telemetry"] [data-testid="flightline-count"]')?.textContent ?? null})`);
    assert.equal(guidance.visible,0);
    if(guidance.total!==null)assert.equal(Number(guidance.total),0);
    writeFileSync(join(out,'routing-guidance.json'),JSON.stringify({before:beforeGuidance,after:guidance},null,2));
    steps.push({step:'no-pending-routing-guides',status:'passed',before:beforeGuidance,after:guidance});
  }
  const newJob=await until(()=>jobIds().find(id=>!oldJobs.has(id)));
  const job=await until(()=>{const file=join(jobsRoot,newJob,'web-job.json');return existsSync(file)?JSON.parse(readFileSync(file,'utf8')):null;});
  assert.equal(job.status,'succeeded');assert.ok(job.results.some(r=>r.result.edits.length>0));
  if(flags.get('--expect-editable-draft')==='true') {
    assert.equal(job.stage,'draft_ready');
    assert.ok(job.results.some(r=>r.resultKind==='editable_draft' &&
      r.result.diagnostics.some(d=>d.severity==='warning' && d.code==='NETLIST2SCHEMATIC_EDITABLE_DRAFT')));
    const guides=await cdp.evaluate(`document.querySelectorAll('[data-testid="flightline"]').length`);
    assert.ok(guides>0,'Incomplete output must keep its real pending connections visible');
    steps.push({step:'real-button-editable-draft-with-pending-guides',status:'passed',guides});
  }
  const after=await save('after');
  assert.notDeepEqual(content(after),content(before));
  steps.push({step:'real-button-python-result',status:'passed',jobId:newJob,message:final});
  if(flags.get('--expect-supply-glyphs')==='true') {
    const expected=job.results.flatMap(r=>r.result.edits).filter(e=>e.kind==='upsert_drafting_object' &&
      /^n2s-supply-ink-/u.test(e.object.id)).map(e=>e.object.id);
    assert.ok(expected.length>0,'The actual button result must contain supply artwork');
    const saved=new Set(after.documents.flatMap(d=>d.drafting?.objects ?? []).map(o=>o.id));
    assert.ok(expected.every(id=>saved.has(id)),'Supply artwork must survive native project export');
    const visible=await cdp.evaluate(`([...document.querySelectorAll('[data-object-id^="n2s-supply-ink-"]')].map(e=>({id:e.getAttribute('data-object-id'),width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height})))`);
    assert.ok(expected.every(id=>visible.some(v=>v.id===id && (v.width>0 || v.height>0))),
      'Supply artwork must be drawn on the actual editor canvas');
    steps.push({step:'supply-artwork-visible-and-saved',status:'passed',strokes:expected.length});
  }

  const undoSelector=await cdp.evaluate(`document.querySelector('[data-testid="draw-tool-undo"]')?'[data-testid="draw-tool-undo"]':'button[aria-label="Undo"]'`);
  const redoSelector=await cdp.evaluate(`document.querySelector('[data-testid="draw-tool-redo"]')?'[data-testid="draw-tool-redo"]':'button[aria-label="Redo"]'`);
  await click(undoSelector);await wait(250);
  const undone=await save('undone');assert.deepEqual(content(undone),content(before));
  await click(redoSelector);await wait(250);
  const redone=await save('redone');assert.deepEqual(content(redone),content(after));
  steps.push({step:'one-step-undo-and-redo',status:'passed'});
  console.log('One-step Undo and Redo verified');

  if(flags.get('--single')!=='true') {
  await click('[data-testid="auto-layout-button"]');
  await until(()=>cdp.evaluate(`!!document.querySelector('[data-testid="auto-layout-cancel"]')`));
  await click(undoSelector);
  await until(async()=>/drawing changed|Project changed|left|applied|failed/.test(await status()),90);
  const stale=await status();
  const unchanged=await save('after-stale');assert.deepEqual(content(unchanged),content(before));
  assert.match(stale,/changed/);
  steps.push({step:'edit-during-run-not-overwritten',status:'passed',message:stale});
  console.log('Revision guard verified:',stale);

  const cancelOld=new Set(jobIds());
  await click('[data-testid="auto-layout-button"]');
  await until(()=>jobIds().some(id=>!cancelOld.has(id)),15);
  await click('[data-testid="auto-layout-cancel"]');
  await until(async()=>/cancelled/.test(await status()));
  const cancelled=await save('after-cancel');assert.deepEqual(content(cancelled),content(before));
  const cancelId=jobIds().find(id=>!cancelOld.has(id));
  const terminal=await until(()=>{const file=join(jobsRoot,cancelId,'web-job.json');return existsSync(file)?JSON.parse(readFileSync(file,'utf8')):null;},15);
  assert.ok(['cancelled','succeeded'].includes(terminal.status));
  steps.push({step:'cancel-leaves-drawing-and-bounded-backend',status:'passed',jobId:cancelId,backendStatus:terminal.status});
  await screenshot('cancelled');
  }
  writeFileSync(join(out,'report.json'),JSON.stringify({status:'passed',origin,input,steps,errors},null,2));
  console.log('BROWSER ACCEPTANCE PASSED',steps.length);
  }
  }
} catch(error) {
  if(cdp){
    try{writeFileSync(join(out,'failure-dom.json'),JSON.stringify(await cdp.evaluate(`({status:document.querySelector('[data-testid="status"]')?.textContent,body:document.body.innerText,files:[...document.querySelectorAll('input[type=file]')].map(e=>({id:e.dataset.testid,accept:e.accept})),buttons:[...document.querySelectorAll('button')].map(e=>({text:e.textContent,testid:e.dataset.testid,label:e.getAttribute('aria-label'),disabled:e.disabled}))})`),null,2));await screenshot('failure');}catch{}
  }
  writeFileSync(join(out,'report.json'),JSON.stringify({status:'failed',steps,error:String(error),errors},null,2));
  throw error;
} finally {
  if(cdp){try{await cdp.call('Browser.close');}catch{}cdp.ws.close();}
  if(chrome.exitCode===null){
    if(process.platform==='win32')spawnSync('taskkill.exe',['/PID',String(chrome.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'});
    else chrome.kill();
  }
}
