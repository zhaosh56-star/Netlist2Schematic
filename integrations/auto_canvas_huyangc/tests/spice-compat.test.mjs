import test from 'node:test';
import assert from 'node:assert/strict';
import { prepareSpiceCompatibility, prepareCompatibleSources } from '../spice-compat.mjs';

const source = (text, path = 'input.cir') => ({path,bytes:new TextEncoder().encode(text)});
const decode = input => new TextDecoder().decode(input.bytes);
const run = text => prepareSpiceCompatibility([source(text)]);

test('ordinary titled SPICE stays byte-identical, including numeric E cards', () => {
  const inputs = [source('* existing\r\nE1 out 0 plus minus 1e5\r\nR1 out 0 1k\r\n.end\r\n')];
  const result = prepareSpiceCompatibility(inputs);
  assert.equal(result.inputs[0], inputs[0]);
  assert.deepEqual(result.report.changes, []);
});

test('a first-line source is preserved by an in-memory title, never dropped', () => {
  const text = 'Iref tail VSS DC 100u\nR1 tail 0 1k\n.ends\n';
  const inputs = [source(text)], before = inputs[0].bytes.slice();
  const result = prepareSpiceCompatibility(inputs);
  assert.deepEqual(inputs[0].bytes, before);
  assert.match(decode(result.inputs[0]), /^\*[^\n]+\nIref tail VSS DC 100u\n/u);
  assert.match(decode(result.inputs[0]), /\n\.end\n$/u);
  assert.equal(result.report.lineOffsets['input.cir'], 1);
});

test('only a single terminal unnamed ENDS in a flat source can be repaired', () => {
  for (const text of ['* x\n.ends dut', '* x\n.ends\nR1 a b 1k', '* x\n.ends\n.ends',
    '.subckt dut a b\nR1 a b 1k\n.ends wrong', '.subckt dut a b\nR1 a b 1k']) {
    const result = run(text);
    assert.equal(result.report.changes.some(c=>c.code==='flat_terminal_ends'), false, text);
    assert.equal(decode(result.inputs[0]), text);
  }
});

test('another supplied file containing a cell prevents flat ENDS repair', () => {
  const result = prepareSpiceCompatibility([source('* x\n.ends'), source('.subckt a b\n.ends a','cell.inc')]);
  assert.equal(result.report.changes.length, 0);
});

test('only exact explicit MOS types get defaults; declarations across includes win', () => {
  const result = prepareSpiceCompatibility([
    source('* x\nM1 d g s b NMOS\nM2 d g s b PMOS\nM3 d g s b nch\n.end'),
    source('.model NMOS pmos\n','models.inc')]);
  assert.deepEqual(result.report.changes.map(c=>[c.model,c.type]), [['PMOS','pmos']]);
  assert.equal(decode(result.inputs[1]), '.model NMOS pmos\n');
  assert.doesNotMatch(decode(result.inputs[0]), /\.model (?:nch|NMOS) /u);
});

test('three and five terminal named E map to the same native signal pins', () => {
  const result = run('* x\nEsmall plus minus out opamp\nEfull vp vn cp cn co opamp gain = 1e5\n.end');
  assert.equal(result.builtInBodies.length, 2);
  const changes = result.report.changes.filter(c=>c.code==='named_opamp');
  assert.deepEqual(changes.map(c=>c.terminals), [
    {'IN+':'plus','IN-':'minus',OUT:'out'},
    {VDD:'vp',VSS:'vn','IN+':'cp','IN-':'cn',OUT:'co'}]);
  for (const body of result.builtInBodies) {
    const lines = decode(result.inputs[0]).split('\n');
    assert.ok(lines[body.source.line-1].startsWith('.subckt '+body.name+' '));
    assert.deepEqual(body.ports.slice(-3), [{pinName:'IN+'},{pinName:'IN-'},{pinName:'OUT'}]);
  }
  assert.match(decode(result.inputs[0]), /XEfull vp vn cp cn co \S+ gain = 1e5/u);
});

test('continuations, inline comments and quoted parameter comments keep port order', () => {
  const result = run('Efilter VDD VSS in_p\n+ in_n out opamp gain = "1;2$3" ; comment\n.end');
  const change = result.report.changes.find(c=>c.code==='named_opamp');
  assert.deepEqual(change.terminals, {VDD:'VDD',VSS:'VSS','IN+':'in_p','IN-':'in_n',OUT:'out'});
  assert.match(decode(result.inputs[0]), /gain = "1;2\$3"/u);
  const body = result.builtInBodies[0];
  assert.ok(decode(result.inputs[0]).split('\n')[body.source.line-1].startsWith('.subckt '));
});

test('native X references and helper body names never collide with user names', () => {
  const result = run('* x\nXE1 p n o useramp\nE1 p n o opamp\n.subckt __n2s_opamp_3 p n o\n.ends __n2s_opamp_3\n.end');
  assert.equal(result.report.changes.find(c=>c.code==='named_opamp').nativeReference, 'XE1_2');
  assert.equal(result.builtInBodies[0].name, '__n2s_opamp_3_2');
});

test('an ordinary control node named opamp is not mistaken for an amplifier model', () => {
  for (const text of ['* x\nE1 out 0 opamp minus 1000\n.end', '* x\nE1 out 0 plus opamp 1000\n.end'])
    assert.equal(run(text).report.changes.length, 0);
});

test('malformed named opamp terminal lists fail before being imported as VCVS', () => {
  for (const text of ['* x\nE1 a b opamp', '* x\nE1 a b c d opamp', '* x\nE1 a b c d e f opamp'])
    assert.throws(()=>run(text), /N2S_SPICE_COMPAT: input.cir:2: Expected/u);
});

test('prepared sources retain native bodies and adjust their line references only when necessary', () => {
  const inputs = [source('R1 a b 1k\n.subckt native p n\n.ends native\n.end')];
  const nativeBody = {source:{path:'input.cir',line:2},name:'native'};
  const result = prepareCompatibleSources(s=>({inputs:s,builtInBodies:[nativeBody]}), inputs);
  assert.equal(result.builtInBodies[0].source.line, 3);
  assert.equal(nativeBody.source.line, 2);
  assert.equal(decode(inputs[0]), 'R1 a b 1k\n.subckt native p n\n.ends native\n.end');
});

test('renaming nodes and instance does not change the recognized interface', () => {
  const a = run('* x\nEa VP VN A B C opamp\n.end');
  const b = run('* y\nErenamed P N first second result opamp\n.end');
  assert.deepEqual(a.builtInBodies, b.builtInBodies);
  assert.equal(b.report.changes[0].terminals.OUT, 'result');
});
