import { test } from 'node:test';
import assert from 'node:assert/strict';
import { verifySupplyConductors } from '../visible-supplies.mjs';
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
