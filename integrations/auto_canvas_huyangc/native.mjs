// Native project IO belongs here, never in the independent Python engine.
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { resolve, dirname, basename } from 'node:path';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { registerProjectSources } from './source-loader.mjs';
import { markerSeats } from './native-marker-seats.mjs';
import { verifyVisibleRouting } from './visible-routing.mjs';

const [operation, ...args] = process.argv.slice(2);
const flags = new Map();
for (let i = 0; i < args.length; i += 2) {
  if (!args[i]?.startsWith('--') || args[i + 1] === undefined)
    throw new Error('Expected --key value arguments');
  flags.set(args[i], args[i + 1]);
}
for (const key of ['--project-root', '--runtime', '--input', '--output'])
  if (!flags.has(key)) throw new Error(`Missing ${key}`);
registerProjectSources(flags.get('--project-root'), flags.get('--runtime'));
const protocol = await import('@icm/project-protocol');
const symbols = await import('@icm/symbols');
const derived = await import('@icm/derived');
const model = await import('@icm/model');

const input = resolve(flags.get('--input'));
const output = resolve(flags.get('--output'));
if (input === output) throw new Error('Output must not overwrite the input project');
const source = readFileSync(input);
const sourceSha256 = createHash('sha256').update(source).digest('hex');
if (operation === 'import-spice') {
  const { importSpiceSources } = await import('@icm/spice');
  const name = basename(input);
  const imported = await importSpiceSources([{path:name,bytes:new Uint8Array(source)}],name,{});
  if (!imported.successful || !imported.project)
    throw new Error('NATIVE_SPICE_IMPORT_FAILED: '+JSON.stringify(imported.diagnostics));
  mkdirSync(dirname(output),{recursive:true});
  writeFileSync(output,protocol.serializeProject(imported.project));
  console.log(JSON.stringify({status:'valid',output,documents:imported.project.documents.length}));
  process.exit(0);
}
const project = protocol.parseProject(source.toString('utf8'));
const selected = flags.get('--cell');
const cells = selected ? project.documents.filter(d=>d.name.toLowerCase()===selected.toLowerCase() ||
  d.netlist.name.toLowerCase()===selected.toLowerCase()) : [];
if (selected && cells.length!==1) throw new Error('Cell name is absent or ambiguous');
const documentId = flags.get('--document-id') || cells[0]?.id || project.topDocumentId;
const document = project.documents.find(d => d.id === documentId);
if (!document) throw new Error(`Unknown Document ${documentId}`);
const resolver = symbols.createProjectSymbolResolver(project, symbols.builtInSymbols);
const { classifyNets } = await import(pathToFileURL(resolve(flags.get('--project-root'),
  'packages/auto-layout/src/net-roles.ts')).href);
const netRoles=classifyNets(document,resolver).byNet;

function symbolSnapshot(instance) {
  const found = resolver.resolve(instance.symbolId, instance.symbolVariantId);
  if (!found) throw new Error(`Unresolved symbol ${instance.symbolId}`);
  const local = { ...document, instances: document.instances.map(i => i.id === instance.id
    ? { ...i, placement: { position: { x: 0, y: 0 }, rotation: 0, mirror: 'none' } } : i) };
  const pins = symbols.drawnPinNames(found).flatMap(name => {
    if (!derived.isVisibleEndpoint(local, resolver,
      { kind: 'terminal', instanceId: instance.id, pinName: name })) return [];
    const geometry = derived.resolveEndpointConnection(local, resolver,
      { kind: 'terminal', instanceId: instance.id, pinName: name });
    if (!geometry) return [];
    const direction = Object.entries({ east: [1,0], west: [-1,0], north: [0,-1], south: [0,1] })
      .find(([, [x,y]]) => geometry.outward?.x === x && geometry.outward?.y === y)?.[0];
    if (!direction) throw new Error(`Nonorthogonal pin ${instance.id}.${name}`);
    return [{ name, at: geometry.contactPoint, landing: geometry.gridLanding, direction }];
  });
  return { definition: found.definition, variant: found.variant || null, pins };
}

if (operation === 'snapshot') {
  const definitions = Object.fromEntries(document.instances.map(i => [i.id, symbolSnapshot(i)]));
  const markerSymbols = {};
  for (const id of ['ground', 'vdd-port']) {
    const found = resolver.resolve(id);
    if (found) markerSymbols[id] = { definition: found.definition, variant: found.variant || null };
  }
  const logical = derived.resolveCommittedDocumentLogicalNets(document);
  const data = { protocolVersion: 1, nativeModelVersion: model.CURRENT_MODEL_SCHEMA_VERSION,
    source: { path: input, sha256: sourceSha256 }, documentId, project,
    symbols: definitions, markerSymbols, logicalNets: logical.groups,
    netRoles:Object.fromEntries(netRoles) };
  mkdirSync(dirname(output), { recursive: true });
  writeFileSync(output, JSON.stringify(data, null, 2) + '\n');
  console.log(JSON.stringify({ status: 'valid', documentId, instances: document.instances.length,
    nets: document.nets.length, output }));
} else if (operation === 'apply') {
  if (!flags.has('--plan')) throw new Error('Missing --plan');
  const plan = JSON.parse(readFileSync(resolve(flags.get('--plan')), 'utf8'));
  if (plan.protocolVersion !== 1 || plan.source.sha256 !== sourceSha256 ||
      plan.projectId !== project.id || plan.documentId !== documentId ||
      plan.expectedRevision !== document.revision ||
      (plan.nativeModelVersion !== undefined && plan.nativeModelVersion !== model.CURRENT_MODEL_SCHEMA_VERSION))
    throw new Error('STALE_INPUT: source, project, Document or revision changed');
  const { executeTransaction } = await import('@icm/edit-engine');
  const { projectElectricalGraph, compareElectricalGraphs } = await import('@icm/netlist');
  const transaction = { transactionId: plan.transactionId, documentId,
    expectedRevision: plan.expectedRevision, actor: { kind: 'agent', id: 'netlist2schematic-adapter' },
    edits: plan.edits };
  for (const change of plan.symbolOverrides || []) {
    const instance=document.instances.find(i=>i.id===change.instanceId);
    const declaration=document.netlist.terminals.find(t=>t.interfaceInstanceIds.includes(change.instanceId));
    const net=document.nets.find(n=>n.terminals.some(t=>t.instanceId===change.instanceId && t.pinName==='P'));
    const group=derived.resolveCommittedDocumentLogicalNets(document).byBaseNetId.get(net?.id);
    const power=['supply','ground'].includes(netRoles.get(net?.id)) ||
      group?.powerDomain==='vdd' || group?.powerDomain==='ground' ||
      /^(?:0|vdd!?|vcc!?|avdd|dvdd|vpwr|vee|vss!?|gnd!?|agnd|dgnd|gnds)$/iu.test(group?.name || declaration?.name || '');
    if (!instance || !['port','port-filled'].includes(instance.symbolId) ||
        change.original!==instance.symbolId || change.symbolId!=='vdd-port' || !declaration || !power)
      throw new Error('Unsupported power-interface presentation override');
  }
  const result = executeTransaction(document, transaction,
    { symbolResolver: resolver, externalSubcircuitDefinitions: project.externalSubcircuitDefinitions });
  if (!result.ok) throw new Error('NATIVE_TRANSACTION_REJECTED: ' + JSON.stringify({
    error: result.error, diagnostics: result.diagnostics }));
  const auditModule = await import(pathToFileURL(resolve(flags.get('--project-root'),
    'packages/auto-layout/src/routing-audit.ts')).href);
  const seating = markerSeats(result.document, plan, {resolver,derived,model,executeTransaction,
    auditRouting:auditModule.auditRouting,externalSubcircuitDefinitions:project.externalSubcircuitDefinitions});
  const after = seating.document;
  for (const instance of document.instances) {
    const copy = after.instances.find(i => i.id === instance.id);
    const expectedSymbol=(plan.symbolOverrides || []).find(c=>c.instanceId===instance.id)?.symbolId || instance.symbolId;
    if (!copy || copy.symbolId !== expectedSymbol || copy.reference !== instance.reference ||
        JSON.stringify(copy.importProvenance) !== JSON.stringify(instance.importProvenance) ||
        JSON.stringify(copy.netlist) !== JSON.stringify(instance.netlist))
      throw new Error(`Native device identity or model changed: ${instance.id}`);
  }
  const next = { ...project, documents: project.documents.map(d => d.id === documentId ? after : d) };
  const beforeGraph = projectElectricalGraph(project);
  const afterGraph = projectElectricalGraph(next);
  if (beforeGraph.status !== 'ready' || afterGraph.status !== 'ready' ||
      compareElectricalGraphs(beforeGraph.graph, afterGraph.graph) !== 'equal') {
    const failure={status:'failed',code:'NATIVE_ELECTRICAL_EQUIVALENCE_FAILED',documentId,
      message:'Native electrical connectivity or exported terminal order changed; the drawing was preserved.',
      details:{beforeStatus:beforeGraph.status,afterStatus:afterGraph.status,
        beforePorts:beforeGraph.graph?.labels.filter(s=>s.startsWith('port:')),
        afterPorts:afterGraph.graph?.labels.filter(s=>s.startsWith('port:'))}};
    if(flags.has('--report'))writeFileSync(resolve(flags.get('--report')),JSON.stringify(failure,null,2)+'\n');
    throw new Error(failure.code+': '+failure.message);
  }
  const beforeCell=projectElectricalGraph({...project,topDocumentId:documentId});
  const afterCell=projectElectricalGraph({...next,topDocumentId:documentId});
  if (beforeCell.status!=='ready' || afterCell.status!=='ready' ||
      compareElectricalGraphs(beforeCell.graph,afterCell.graph)!=='equal')
    throw new Error('NATIVE_CELL_ELECTRICAL_EQUIVALENCE_FAILED');
  const serialized = protocol.serializeProject(next);
  const roundtrip = protocol.parseProject(serialized);
  const canonical = value => Array.isArray(value) ? value.map(canonical) :
    value && typeof value==='object' ? Object.fromEntries(Object.keys(value).sort().map(k=>[k,canonical(value[k])])) : value;
  for (const untouched of project.documents.filter(d=>d.id!==documentId)) {
    const copy=roundtrip.documents.find(d=>d.id===untouched.id);
    if (JSON.stringify(canonical(copy))!==JSON.stringify(canonical(untouched)))
      throw new Error('UNSELECTED_DOCUMENT_CHANGED');
  }
  const saved = roundtrip.documents.find(d => d.id === documentId);
  const savedResolver = symbols.createProjectSymbolResolver(roundtrip, symbols.builtInSymbols);
  for (const pin of plan.expectedPins) {
    const actual = derived.resolveEndpointConnection(saved, savedResolver, pin.endpoint);
    const seat = seating.actions.find(a=>a.instanceId===pin.endpoint.instanceId && a.pinName===pin.endpoint.pinName);
    const expected = seat?.expectedPin || pin.position;
    if (!actual || Math.abs(actual.contactPoint.x - expected.x) > 1e-7 ||
        Math.abs(actual.contactPoint.y - expected.y) > 1e-7)
      throw new Error(`NATIVE_PIN_GEOMETRY_MISMATCH: ${JSON.stringify(pin.endpoint)}`);
  }
  const graph = projectElectricalGraph(roundtrip);
  if (graph.status !== 'ready' || compareElectricalGraphs(beforeGraph.graph, graph.graph) !== 'equal')
    throw new Error('NATIVE_ROUNDTRIP_ELECTRICAL_MISMATCH');
  const audit = auditModule.auditRouting(saved, savedResolver);
  if (audit.unsafeProblems.length)
    throw new Error('NATIVE_ROUTE_AUDIT_FAILED: ' + JSON.stringify(audit.unsafeProblems));
  const visibleSignals=verifyVisibleRouting(saved,savedResolver,plan,derived);
  let svg;
  if (flags.has('--svg')) {
    const { renderDocumentSvg } = await import('@icm/render-svg');
    svg = renderDocumentSvg(saved, savedResolver);
  }
  // No success artifact is written until protocol, graph, pins and routes all pass.
  mkdirSync(dirname(output), { recursive: true });
  writeFileSync(output, serialized);
  if (svg !== undefined) writeFileSync(resolve(flags.get('--svg')), svg);
  const report = { status: 'valid', documentId, sourceSha256, output,
    edits: plan.edits.length, revision: saved.revision, electricalEquivalent: true,
    originalInstancesPreserved: true, modelParametersPreserved: true,
    unselectedDocumentsPreserved:true,
    exactPinGeometry: true, roundtripVerified: true, nativeAudit: audit,
    visibleSignals,
    powerInterfacePresentationChanges:plan.symbolOverrides || [],
    nativeMarkerSeats:{actions:seating.actions,attempts:seating.attempts,transactions:seating.transactions} };
  if (flags.has('--report')) writeFileSync(resolve(flags.get('--report')), JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify(report));
} else {
  throw new Error(`Unknown operation ${operation}`);
}
