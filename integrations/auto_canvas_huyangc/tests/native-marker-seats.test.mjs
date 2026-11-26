import test from 'node:test';
import assert from 'node:assert/strict';
import {markerSeats} from '../native-marker-seats.mjs';

function scene({blocked=false,valid=false}={}) {
  const pin=(instanceId,pinName='P')=>({kind:'terminal',instanceId,pinName});
  const document={id:'doc',revision:0,instances:[
    {id:'host',symbolId:'device',placement:{position:{x:0,y:-20},rotation:0,mirror:'none'}},
    {id:'cap',symbolId:'marker',placement:{position:{x:0,y:20},rotation:0,mirror:'none'}}],
    routes:[{id:'lead',netId:'rail',start:pin('host'),legs:[{to:{kind:'endpoint',endpoint:pin('cap')}}]},
      {id:'signal',netId:'signal',start:{kind:'junction',junctionId:'a'},
        legs:[{to:{kind:'endpoint',endpoint:{kind:'junction',junctionId:'b'}}}]}]};
  const plan={transactionId:'plan',powerMarkers:[{instanceId:'cap',pinName:'P'}],
    pinEscapes:[{endpoint:pin('host'),position:{x:0,y:-8}}]};
  const point=(doc,e)=>e.kind==='junction'?{x:e.junctionId==='a'?-100:100,y:20}:
    (()=>{const p=doc.instances.find(i=>i.id===e.instanceId).placement.position;
      return {x:p.x,y:p.y-(e.instanceId==='cap'?20:0)};})();
  const context={
    resolver:{resolve:id=>({definition:{viewBox:id==='marker'?
      {x:-8,y:-20,width:16,height:22}:{x:-5,y:-4,width:10,height:8}}})},
    derived:{resolveEndpointPoint:(doc,_resolver,e)=>point(doc,e),resolveEndpointConnection:(doc,_resolver,e)=>
      ({gridLanding:point(doc,e),outward:e.kind==='terminal'?{x:0,y:e.instanceId==='cap'?-1:1}:null})},
    model:{transformPoint:(p,t)=>({x:p.x+t.x,y:p.y+t.y})},
    executeTransaction:(doc,t)=>{
      const next=structuredClone(doc);next.revision++;
      for(const edit of t.edits)
        if(edit.kind==='move_instance')next.instances.find(i=>i.id===edit.instanceId).placement.position=edit.position;
        else next.routes=next.routes.map(r=>r.id===edit.route.id?edit.route:r);
      return {ok:true,document:next};
    },
    auditRouting:doc=>{
      if(valid)return {unsafeProblems:[]};
      const p=doc.instances.find(i=>i.id==='cap').placement.position;
      return {unsafeProblems: blocked || Math.abs(p.y-20)<2 || p.y<12 || p.y>20
        ? ['Route signal crosses Instance cap']:[]};
    }
  };
  return {document,plan,context};
}

test('a small cap conflict is resolved without a whole-symbol jump or device movement',()=>{
  const {document,plan,context}=scene(),before=structuredClone(document);
  const r=markerSeats(document,plan,context);
  assert.deepEqual(r.audit.unsafeProblems,[]);
  assert.deepEqual(r.actions[0].position,{x:0,y:18});
  assert.deepEqual(r.document.instances[0],before.instances[0]);
  assert.deepEqual(r.document.routes[1],before.routes[1]);
  assert.deepEqual(document,before);
  assert.ok(r.attempts<=64);
});

test('failed seats roll back and retain their geometry error',()=>{
  const {document,plan,context}=scene({blocked:true}),before=structuredClone(document);
  const r=markerSeats(document,plan,context);
  assert.deepEqual(r.document,before);
  assert.equal(r.actions.length,0);
  assert.equal(r.audit.unsafeProblems.length,1);
  assert.ok(r.attempts<=64);
});

test('valid drawings do not enter marker seating',()=>{
  const {document,plan,context}=scene({valid:true});
  const r=markerSeats(document,plan,context);
  assert.equal(r.document,document);
  assert.equal(r.attempts,0);
});
