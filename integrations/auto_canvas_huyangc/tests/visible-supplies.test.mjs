import { test } from 'node:test';
import assert from 'node:assert/strict';
import { verifySupplyConductors, verifySupplyPresentation } from '../visible-supplies.mjs';
const p=(x,y)=>({x,y});
const lines=[[[0,20],[0,0]],[[100,20],[100,0]]];
const terminals=[p(0,20),p(100,20)],owners=[p(0,0),p(100,0)];

test('separate supply stubs can be joined through their actual naming owners',()=>{
  const result=verifySupplyConductors(lines,terminals,owners);
  assert.equal(result.connected,true);assert.equal(result.physicalComponents,2);
});
test('a missing local supply lead remains an error even with correct names',()=>{
  assert.throws(()=>verifySupplyConductors(lines.slice(1),terminals,owners),/SUPPLY_CONNECTION_OPEN/);
});
test('a wire group without a naming owner and a detached wire island are rejected',()=>{
  assert.throws(()=>verifySupplyConductors(lines,terminals,owners.slice(1)),/SUPPLY_CONNECTION_OPEN/);
  assert.throws(()=>verifySupplyConductors([...lines,[[40,40],[60,40]]],terminals,owners),/SUPPLY_CONNECTION_OPEN/);
});
test('naming-owner connectivity is invariant under translations and rotations',()=>{
  for(let turns=0;turns<4;turns++) {
    const pose=raw=>{let {x,y}=Array.isArray(raw)?p(...raw):raw;for(let n=0;n<turns;n++)[x,y]=[-y,x];return p(x+50,y-90);};
    assert.equal(verifySupplyConductors(lines.map(s=>s.map(pose)),terminals.map(pose),owners.map(pose)).connected,true);
  }
});

function artwork() {
  const id='n2s-supply-ink-'+'a'.repeat(18),offset=p(0,-20);
  const anchor=localOffset=>({kind:'object',objectId:'formal',localOffset,fallbackPosition:p(0,0)});
  const document={instances:[{id:'formal',placement:{position:p(10,20)}}],junctions:[],
    nets:[{id:'rail',terminals:[{instanceId:'formal',pinName:'P'}]}],
    drafting:{objects:[{id,kind:'arrow',anchor:anchor(p(0,0)),from:anchor(p(0,0)),to:anchor(offset),
      styleOverride:{arrowHead:'none',arrowStart:'none',arrowEnd:'none',strokeScale:1}}]}};
  const plan={powerGeometry:{rail:{}},powerPresentation:{version:'native-supply-glyph-v1',glyphs:[{
    netId:'rail',owner:{kind:'terminal',instanceId:'formal',pinName:'P'},anchorOffset:p(0,0),
    strokes:[{objectId:id,fromOffset:p(0,0),toOffset:offset,strokeScale:1}]}]}};
  const derived={resolveDocumentRoutingGeometry:()=>({}),
    resolveEndpointConnection:(doc,_,owner)=>({contactPoint:doc.instances.find(i=>i.id===owner.instanceId).placement.position}),
    resolveDraftingObjectGeometry:(doc,_,object)=>{
      const position=doc.instances.find(i=>i.id===object.from.objectId).placement.position;
      const at=a=>p(position.x+a.localOffset.x,position.y+a.localOffset.y);
      return {kind:'arrow',from:at(object.from),to:at(object.to),diagnostics:[]};
    }};
  return {document,plan,derived};
}

test('supply artwork is checked against a real same-net contact and follows that owner',()=>{
  const {document,plan,derived}=artwork();
  assert.equal(verifySupplyPresentation(document,null,plan,derived).strokes,1);
  document.instances[0].placement.position=p(100,-200);
  assert.equal(verifySupplyPresentation(document,null,plan,derived).glyphs,1);
});

test('missing, moved or electrically misassigned supply artwork is rejected',()=>{
  for(const mutate of [d=>d.drafting.objects.pop(),
    d=>{d.drafting.objects[0].from.localOffset.x+=1;},
    d=>{d.nets[0].terminals=[];},
    d=>{d.drafting.objects[0].from={kind:'free',position:p(10,20)};},
    d=>{d.drafting.objects[0].styleOverride.arrowEnd='small-arrow';}]) {
    const {document,plan,derived}=artwork();mutate(document);
    assert.throws(()=>verifySupplyPresentation(document,null,plan,derived),/NATIVE_SUPPLY_ARTWORK/);
  }
});

test('orphaned adapter artwork is rejected and legacy plans remain readable',()=>{
  const {document,plan,derived}=artwork();
  document.drafting.objects.push({...document.drafting.objects[0],id:'n2s-supply-ink-'+'b'.repeat(18)});
  assert.throws(()=>verifySupplyPresentation(document,null,plan,derived),/ARTWORK_ORPHAN/);
  assert.equal(verifySupplyPresentation(document,null,{},derived).checked,false);
});
