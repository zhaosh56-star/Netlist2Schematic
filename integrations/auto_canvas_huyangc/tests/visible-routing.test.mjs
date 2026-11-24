import { test } from 'node:test';
import assert from 'node:assert/strict';
import { conductorGraph, verifySignalConductors } from '../visible-routing.mjs';
const p=(x,y)=>({x,y});

test('a connected tree may receive a leaf port without introducing a loop',()=>{
  const core=[[[0,0],[0,30]],[[0,20],[50,20]]];
  const result=verifySignalConductors(core,[...core,[[24,20],[24,-10]]],
    [p(0,0),p(0,30),p(50,20),p(24,-10)]);
  assert.equal(result.cycles,0);assert.equal(result.connected,true);
});
test('the extra L that leaves and rejoins its own tree is rejected',()=>{
  const core=[[[0,0],[0,30]],[[0,20],[50,20]]];
  assert.throws(()=>verifySignalConductors(core,[...core,[[0,24],[24,24]],[[24,24],[24,-10]]],
    [p(0,0),p(0,30),p(50,20),p(24,-10)]),/LOOP_ADDED/);
});
test('logical membership cannot conceal a missing drawn connection',()=>{
  const core=[[[0,0],[100,0]]];
  assert.throws(()=>verifySignalConductors(core,[[[0,0],[40,0]],[[60,0],[100,0]]],
    [p(0,0),p(100,0)]),/DISCONNECTED/);
});
test('unconnected wire islands and removed original escape tails are rejected',()=>{
  assert.throws(()=>verifySignalConductors([[[0,0],[100,0]]],[[[0,0],[100,0]],[[200,0],[220,0]]],
    [p(0,0),p(100,0)]),/ISLAND_ADDED/);
  assert.throws(()=>verifySignalConductors([[[0,0],[100,0]]],[[[0,0],[80,0]]],
    [p(0,0),p(80,0)]),/CORE_SIGNAL_REMOVED/);
});
test('geometric topology ignores collinear slicing and duplicate intervals',()=>{
  const a=conductorGraph([[[0,0],[100,0]],[[50,0],[50,30]]]);
  const b=conductorGraph([[[0,0],[50,0]],[[50,0],[100,0]],[[0,0],[100,0]],[[50,0],[50,30]]]);
  assert.equal(a.cycles,b.cycles);assert.equal(a.componentCount,b.componentCount);
});
test('existing same-net rings are preserved while additional rings are rejected',()=>{
  const core=[[[0,0],[40,0]],[[40,0],[40,40]],[[40,40],[0,40]],[[0,40],[0,0]]];
  assert.equal(verifySignalConductors(core,core,[p(0,0),p(40,40)]).cycles,1);
});
