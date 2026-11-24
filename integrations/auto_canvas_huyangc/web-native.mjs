// The browser posts the in-memory model, not an invented project-file schema.
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { registerProjectSources } from './source-loader.mjs';
import { verifyVisibleRouting } from './visible-routing.mjs';

const [operation, ...args] = process.argv.slice(2);
const flags = new Map();
for (let i = 0; i < args.length; i += 2) flags.set(args[i], args[i + 1]);
for (const key of ['--project-root', '--runtime', '--input', '--output'])
  if (!flags.get(key)) throw new Error(`Missing ${key}`);
registerProjectSources(flags.get('--project-root'), flags.get('--runtime'));
const protocol = await import('@icm/project-protocol');
const model = await import('@icm/model');
const write = value => writeFileSync(flags.get('--output'), JSON.stringify(value, null, 2) + '\n');

if (operation === 'input') {
  const request = JSON.parse(readFileSync(flags.get('--input'), 'utf8'));
  if (request.type !== 'plan' || request.project?.schemaVersion !== model.CURRENT_MODEL_SCHEMA_VERSION)
    throw new Error('BROWSER_MODEL_VERSION_MISMATCH: refresh the compatible editor build');
  const project = protocol.validateProjectReadSnapshot(request.project);
  if (!Array.isArray(request.documentIds) || !request.documentIds.length ||
      new Set(request.documentIds).size !== request.documentIds.length ||
      request.documentIds.some(id => !project.documents.some(d => d.id === id)))
    throw new Error('Invalid requested Documents');
  writeFileSync(flags.get('--output'), protocol.serializeProject(project));
  console.log(JSON.stringify({status:'valid',documents:request.documentIds.length}));
} else if (operation === 'result') {
  const source = readFileSync(flags.get('--input'));
  const project = protocol.parseProject(source.toString('utf8'));
  const plan = JSON.parse(readFileSync(flags.get('--plan'), 'utf8'));
  const validation = JSON.parse(readFileSync(flags.get('--validation'), 'utf8'));
  const expectedProject = protocol.parseProject(readFileSync(flags.get('--expected'), 'utf8'));
  const original = project.documents.find(d => d.id === plan.documentId);
  if (plan.protocolVersion !== 1 || plan.source.sha256 !== createHash('sha256').update(source).digest('hex') ||
      plan.projectId !== project.id || !original || plan.expectedRevision !== original.revision ||
      plan.nativeModelVersion !== model.CURRENT_MODEL_SCHEMA_VERSION || validation.status !== 'valid' ||
      validation.sourceSha256 !== plan.source.sha256 || validation.documentId !== plan.documentId)
    throw new Error('STALE_OR_UNVERIFIED_WEB_PLAN');
  const edits = [...plan.edits, ...validation.nativeMarkerSeats.transactions.flatMap(t => t.edits)];
  if (edits.length > 1024) throw new Error('Combined native transaction exceeds 1024 edits');
  const { builtInSymbols, createProjectSymbolResolver } = await import('@icm/symbols');
  const { executeTransaction } = await import('@icm/edit-engine');
  const { projectElectricalGraph, compareElectricalGraphs } = await import('@icm/netlist');
  const derived = await import('@icm/derived');
  const { auditRouting } = await import(pathToFileURL(resolve(flags.get('--project-root'),
    'packages/auto-layout/src/routing-audit.ts')).href);
  const resolver = createProjectSymbolResolver(project, builtInSymbols);
  const committed = executeTransaction(original, {
    transactionId:plan.transactionId,documentId:original.id,expectedRevision:original.revision,
    actor:{kind:'agent',id:'netlist2schematic-web'},edits,
  },{symbolResolver:resolver,externalSubcircuitDefinitions:project.externalSubcircuitDefinitions});
  if (!committed.ok) throw new Error('ATOMIC_BROWSER_TRANSACTION_FAILED: '+JSON.stringify(committed.diagnostics));
  const next = {...project,documents:project.documents.map(d=>d.id===original.id?committed.document:d)};
  const same = (a,b) => {
    const ga=projectElectricalGraph(a),gb=projectElectricalGraph(b);
    return ga.status==='ready' && gb.status==='ready' && compareElectricalGraphs(ga.graph,gb.graph)==='equal';
  };
  if (!same(project,next) || !same({...project,topDocumentId:original.id},{...next,topDocumentId:original.id}))
    throw new Error('WEB_ELECTRICAL_EQUIVALENCE_FAILED');
  const expected=expectedProject.documents.find(d=>d.id===original.id);
  if (!expected || expected.instances.length!==committed.document.instances.length)
    throw new Error('WEB_INSTANCE_SET_CHANGED');
  for (const instance of expected.instances) {
    const actual=committed.document.instances.find(i=>i.id===instance.id);
    if (!actual || JSON.stringify(actual)!==JSON.stringify(instance))
      throw new Error('WEB_ATOMIC_INSTANCE_MISMATCH: '+instance.id);
  }
  for (const pin of plan.expectedPins) {
    const geometry=derived.resolveEndpointConnection(committed.document,resolver,pin.endpoint);
    const seat=validation.nativeMarkerSeats.actions.find(a=>a.instanceId===pin.endpoint.instanceId && a.pinName===pin.endpoint.pinName);
    const target=seat?.expectedPin || pin.position;
    if (!geometry || geometry.contactPoint.x!==target.x || geometry.contactPoint.y!==target.y)
      throw new Error('WEB_PIN_MISMATCH');
  }
  const audit=auditRouting(committed.document,resolver);
  if (audit.unsafeProblems.length || ['wireLength','crossings','bends'].some(k=>audit[k]!==validation.nativeAudit[k]))
    throw new Error('WEB_ATOMIC_ROUTE_AUDIT_FAILED: '+JSON.stringify(audit));
  const visibleSignals=verifyVisibleRouting(committed.document,resolver,plan,derived);
  const roundtrip=protocol.parseProject(protocol.serializeProject(next));
  if (!same(project,roundtrip)) throw new Error('WEB_ROUNDTRIP_FAILED');
  write({documentId:original.id,result:{edits,groups:[],diagnostics:[]},
    validation:{electricalEquivalent:true,atomicTransaction:true,nativeAudit:audit,
      visibleSignals,
      expectedRevision:original.revision,engine:'Netlist2Schematic'}});
} else throw new Error(`Unknown operation ${operation}`);
