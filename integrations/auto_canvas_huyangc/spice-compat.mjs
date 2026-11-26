// Bounded import adaptation shared by the browser and native CLI. This is not
// an engine parser: native compilation still owns hierarchy/model validation.
export const SPICE_COMPAT_VERSION = 'spice-import-v1';

function codePart(line) {
  if (/^\s*\*/u.test(line)) return '';
  let quote = null;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (quote) {
      if (c === quote && line[i - 1] !== '\\') quote = null;
    } else if (c === '"' || c === "'") quote = c;
    else if (c === ';' || c === '$' || line.slice(i, i + 2) === '//') return line.slice(0, i);
  }
  return line;
}

function logicalLines(lines) {
  const records = [];
  for (let i = 0; i < lines.length; i++) {
    const text = codePart(lines[i]).trim();
    if (!text) continue;
    if (text.startsWith('+') && records.length) {
      const previous = records.at(-1);
      previous.text += ' ' + text.slice(1).trim();
      previous.lines.push(i);
    } else records.push({ text, lines: [i] });
  }
  return records;
}

function fail(path, record, message) {
  throw new Error(`N2S_SPICE_COMPAT: ${path}:${record.lines[0] + 1}: ${message}`);
}

// Accept only complete key=value parameters; quoted/braced values may contain
// spaces. Do not turn a malformed node list into an apparently valid opamp.
function parameterTail(text) {
  let remaining = text.trim();
  while (remaining) {
    const key = /^[A-Za-z_][\w.]*\s*=\s*/u.exec(remaining);
    if (!key) return false;
    remaining = remaining.slice(key[0].length);
    if (!remaining) return false;
    const first = remaining[0];
    let end = 0;
    if (first === '"' || first === "'" || first === '{') {
      const close = first === '{' ? '}' : first;
      let depth = 1;
      for (end = 1; end < remaining.length; end++) {
        if (first === '{' && remaining[end] === '{') depth++;
        if (remaining[end] === close && remaining[end - 1] !== '\\' && --depth === 0) break;
      }
      if (end === remaining.length) return false;
      end++;
      if (remaining[end] && !/\s/u.test(remaining[end])) return false;
    } else {
      const value = /^[^\s=]+/u.exec(remaining);
      if (!value) return false;
      end = value[0].length;
    }
    remaining = remaining.slice(end).trim();
  }
  return true;
}

function firstLineIsDevice(record, declared) {
  if (!record || record.lines[0] !== 0) return false;
  const words = record.text.split(/\s+/u), first = words[0];
  const value = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?[a-z]*$/iu;
  if (/^[rclvi]\S+$/iu.test(first) && words.length >= 4)
    return value.test(words[3]) || (/^[vi]/iu.test(first) && /^dc$/iu.test(words[3]) && value.test(words[4] || ''));
  if (/^m\S+$/iu.test(first) && words.length >= 6)
    return /^(?:nmos|pmos)$/iu.test(words[5]) || declared.has(words[5].toLowerCase());
  if (/^e\S+$/iu.test(first))
    return /(?:^|\s)opamp(?:\s|$)/iu.test(record.text) || value.test(words[5] || '');
  // Numbered instances are unambiguous structural cards even when their
  // models are unknown; leave that error to native compilation, not title loss.
  return /^[mdqx][\w.-]*\d[\w.-]*$/iu.test(first) && words.length >= 4;
}

/** Inputs have already passed the native Spectre/source conversion stage. */
export function prepareSpiceCompatibility(inputs) {
  const files = inputs.map(input => {
    const text = new TextDecoder('utf-8', { fatal: true }).decode(input.bytes);
    const lines = text.split(/\r\n?|\n/u);
    return { input, lines, records: logicalLines(lines), additions: [] };
  });
  const all = files.flatMap(file => file.records.map(record => ({ file, record })));
  const declared = new Set();
  const identifiers = new Set();
  for (const { record } of all) {
    const words = record.text.split(/\s+/u);
    identifiers.add(words[0].toLowerCase());
    if (/^\.(?:model|subckt)$/iu.test(words[0]) && words[1]) {
      identifiers.add(words[1].toLowerCase());
      if (/^\.model$/iu.test(words[0])) declared.add(words[1].toLowerCase());
    }
  }
  const unique = preferred => {
    let name = preferred, suffix = 2;
    while (identifiers.has(name.toLowerCase())) name = `${preferred}_${suffix++}`;
    identifiers.add(name.toLowerCase());
    return name;
  };
  const changes = [], builtInBodies = [], lineOffsets = {};
  const report = (file, record, value) => changes.push({ path: file.input.path,
    line: record.lines[0] + 1, ...value });

  // Only the unambiguous flat-export typo is repaired. Named/multiple ENDS,
  // real cells, and a nonterminal ENDS remain errors in the native compiler.
  if (!all.some(({ record }) => /^\.subckt\b/iu.test(record.text))) {
    const ends = all.filter(({ record }) => /^\.ends\b/iu.test(record.text));
    if (ends.length === 1) {
      const { file, record } = ends[0];
      if (/^\.ends$/iu.test(record.text) && file.records.at(-1) === record) {
        file.lines[record.lines[0]] = '.end';
        report(file, record, { code: 'flat_terminal_ends', message: 'Terminal unnamed .ends interpreted as .end in memory.' });
      }
    }
  }

  for (const file of files) {
    const helpers = new Map();
    for (const record of file.records) {
      const words = record.text.split(/\s+/u);
      // Exact, explicitly typed model names only. Existing declarations win,
      // including names whose spelling does not match their declared polarity.
      if (/^m/iu.test(words[0]) && words.length >= 6 && /^(?:nmos|pmos)$/iu.test(words[5])) {
        const name = words[5].toLowerCase();
        if (!declared.has(name)) {
          file.additions.push(`.model ${words[5]} ${name}`);
          declared.add(name);
          report(file, record, { code: 'explicit_mos_model', model: words[5], type: name });
        }
      }
      if (!/^e/iu.test(words[0])) continue;
      const named = [...record.text.matchAll(/(?:^|\s)opamp(?=\s|$)/giu)].find(match =>
        parameterTail(record.text.slice(match.index + match[0].length)));
      if (!named) continue; // Numeric and expression VCVS cards stay native.
      const head = record.text.slice(0, named.index).trim().split(/\s+/u);
      const tail = record.text.slice(named.index + named[0].length).trim();
      if (![4, 6].includes(head.length) || !parameterTail(tail) ||
          head.some(word => !/^[^\s=(){}"';]+$/u.test(word)))
        fail(file.input.path, record, 'Expected Ename [VDD VSS] IN+ IN- OUT opamp [key=value].');
      const hasSupplies = head.length === 6;
      const count = hasSupplies ? 5 : 3;
      let helper = helpers.get(count);
      if (!helper) {
        helper = { name: unique(`__n2s_opamp_${count}`), hasSupplies };
        helpers.set(count, helper);
      }
      const reference = unique('X' + head[0]);
      file.lines[record.lines[0]] = [reference, ...head.slice(1), helper.name, tail].filter(Boolean).join(' ');
      for (const index of record.lines.slice(1)) file.lines[index] = '* continuation adapted by Netlist2Schematic';
      const signals = head.slice(hasSupplies ? 3 : 1);
      report(file, record, { code: 'named_opamp', originalReference: head[0], nativeReference: reference,
        terminals: { ...(hasSupplies ? { VDD: head[1], VSS: head[2] } : {}),
          'IN+': signals[0], 'IN-': signals[1], OUT: signals[2] } });
    }
    // Append declarations before a final END. Existing cell/body source line
    // numbers stay unchanged, as do input bytes supplied by the caller.
    let insertion = file.lines.length;
    while (insertion && !codePart(file.lines[insertion - 1]).trim()) insertion--;
    if (insertion && /^\s*\.end\s*$/iu.test(codePart(file.lines[insertion - 1]))) insertion--;
    for (const helper of helpers.values()) {
      const ports = [...(helper.hasSupplies ? ['VDD', 'VSS'] : []), 'VIP', 'VIN', 'VOUT'];
      const line = insertion + file.additions.length + 1;
      file.additions.push(`.subckt ${helper.name} ${ports.join(' ')}`, `.ends ${helper.name}`);
      builtInBodies.push({ source: { path: file.input.path, line }, name: helper.name, kind: 'subcircuit',
        symbolId: 'opamp', target: 'opamp', referencePrefix: 'X', ports: [
          ...(helper.hasSupplies ? [{ supply: 'VDD', unboundAtGround: false },
            { supply: 'VSS', unboundAtGround: false }] : []),
          { pinName: 'IN+' }, { pinName: 'IN-' }, { pinName: 'OUT' }] });
    }
    file.lines.splice(insertion, 0, ...file.additions);
    if (firstLineIsDevice(file.records[0], declared)) {
      file.lines.unshift('* Netlist2Schematic: source starts with a device, not a SPICE title');
      lineOffsets[file.input.path] = 1;
      for (const body of builtInBodies)
        if (body.source.path === file.input.path) body.source.line++;
      report(file, file.records[0], { code: 'titleless_deck', compiledLineOffset: 1,
        message: 'Inserted an in-memory title to preserve the first device.' });
    }
  }
  return { inputs: files.map(file => changes.some(c => c.path === file.input.path)
      ? { ...file.input, bytes: new TextEncoder().encode(file.lines.join('\n')) } : file.input),
    builtInBodies, report: { version: SPICE_COMPAT_VERSION, changes, lineOffsets } };
}

export function prepareCompatibleSources(nativePrepare, sources) {
  const native = nativePrepare(sources);
  const compat = prepareSpiceCompatibility(native.inputs);
  return { ...native, inputs: compat.inputs,
    builtInBodies: [...native.builtInBodies.map(body => ({ ...body, source: { ...body.source,
      line: body.source.line + (compat.report.lineOffsets[body.source.path] || 0) } })),
      ...compat.builtInBodies], compatibility: compat.report };
}
