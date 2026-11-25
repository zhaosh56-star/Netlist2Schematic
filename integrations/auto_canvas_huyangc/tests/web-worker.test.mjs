import { test } from 'node:test';
import assert from 'node:assert/strict';
import { planWithPython } from '../web/auto-layout.worker.mjs';

const request={type:'plan',runId:9,documentIds:['a','b'],project:{schemaVersion:58}};
const progress={cell:1,cells:2,evaluations:0,budget:0};
function fixture(states) {
  const messages=[],calls=[];
  return {messages,calls,options:{token:'local-token',pause:async()=>{},post:m=>messages.push(m),
    fetcher:async(url,options)=>{
      calls.push({url,options});
      const item=states.shift();
      if (item instanceof Error) throw item;
      assert.ok(item,'Unexpected API call');
      return {ok:item.code===undefined || item.code<400,status:item.code || 200,json:async()=>item.body || item};
    }}};
}

test('native Worker protocol returns all verified Cells and real request identity',async()=>{
  const results=request.documentIds.map(documentId=>({documentId,result:{edits:[],groups:[],diagnostics:[]},milliseconds:2}));
  const f=fixture([{jobId:'job',status:'queued',progress},
    {status:'running',progress},{status:'succeeded',runId:9,progress,results}]);
  await planWithPython(request,f.options);
  assert.equal(f.messages.at(-1).type,'result');
  assert.equal(f.messages.at(-1).runId,9);
  assert.deepEqual(f.messages.at(-1).results,results);
  assert.equal(f.calls[0].options.headers['x-netlist2schematic-token'],'local-token');
  assert.deepEqual(JSON.parse(f.calls[0].options.body),request);
});

test('backend failure does not silently invoke the original layout algorithm',async()=>{
  const f=fixture([{code:503,body:{message:'Python unavailable'}}]);
  await planWithPython(request,f.options);
  assert.deepEqual(f.messages,[{type:'failed',runId:9,message:'Python unavailable'}]);
  assert.equal(f.calls.length,1);
});

test('different run or missing Cell is never returned for commit',async()=>{
  for (const bad of [{runId:99,results:[]},{runId:9,results:[]}]) {
    const f=fixture([{jobId:'job',status:'queued'},
      {status:'succeeded',...bad},{status:'cancelled'}]);
    await planWithPython(request,f.options);
    assert.equal(f.messages.at(-1).type,'failed');
    assert.equal(f.calls.at(-1).options.method,'DELETE');
  }
});

test('heartbeat loss reports failure and cancels only its owned job',async()=>{
  const f=fixture([{jobId:'mine',status:'running'},new Error('Disconnected'),{status:'cancelled'}]);
  await planWithPython(request,f.options);
  assert.equal(f.messages.at(-1).type,'failed');
  assert.equal(f.calls.at(-1).url,'/api/netlist2schematic/jobs/mine');
  assert.equal(f.calls.at(-1).options.method,'DELETE');
});

test('per-Cell refusal remains visible beside another successful Cell',async()=>{
  const results=[{documentId:'a',result:{edits:[],groups:[],diagnostics:[{severity:'error',message:'Unsupported'}]}},
    {documentId:'b',result:{edits:[{kind:'move_instance'}],groups:[],diagnostics:[]}}];
  const f=fixture([{jobId:'job',status:'succeeded',runId:9,results}]);
  await planWithPython(request,f.options);
  assert.deepEqual(f.messages.at(-1).results,results);
});

test('an editable draft preserves its warning instead of claiming complete routing',async()=>{
  const results=request.documentIds.map(documentId=>({documentId,resultKind:'editable_draft',
    result:{edits:[{kind:'move_instance'}],groups:[],diagnostics:[{severity:'warning',
      code:'NETLIST2SCHEMATIC_EDITABLE_DRAFT',message:'14 connections remain; finish the flightlines.'}]}}));
  const f=fixture([{jobId:'draft',status:'succeeded',stage:'draft_ready',runId:9,results}]);
  await planWithPython(request,f.options);
  assert.equal(f.messages.at(-1).type,'result');
  assert.deepEqual(f.messages.at(-1).results,results);
});
