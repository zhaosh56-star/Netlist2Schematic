// Supply stubs may be joined by explicit native names, but a label must not
// conceal a missing wire between a real device pin and its local naming owner.
import { conductorGraph } from './visible-routing.mjs';
const key=p=>`${p.x},${p.y}`;

export function verifySupplyConductors(segments,terminals,owners) {
  const graph=conductorGraph(segments,[...terminals,...owners]);
  const named=new Set(owners.map(p=>graph.components.get(key(p))));
  if(graph.componentCount>1 && [...graph.components.values()].some(c=>!named.has(c)))
    throw new Error('NATIVE_SUPPLY_CONNECTION_OPEN: a supply wire group has no connected naming owner');
  return {connected:true,physicalComponents:graph.componentCount,namingOwners:owners.length};
}

export function verifySupplyPresentation(document,resolver,plan,derived) {
  if(!plan.powerPresentation)return {status:'legacy-plan-without-supply-artwork',checked:false};
  if(plan.powerPresentation.version!=='native-supply-glyph-v1')throw new Error('NATIVE_SUPPLY_ARTWORK_VERSION');
  const close=(a,b)=>a && b && Math.abs(a.x-b.x)<1e-7 && Math.abs(a.y-b.y)<1e-7;
  const add=(a,b)=>({x:a.x+b.x,y:a.y+b.y});
  const objects=new Map((document.drafting?.objects ?? []).map(o=>[o.id,o]));
  const expectedIds=new Set();
  const routing=plan.powerPresentation.glyphs.length?derived.resolveDocumentRoutingGeometry(document,resolver):null;
  for(const glyph of plan.powerPresentation.glyphs) {
    const net=document.nets.find(n=>n.id===glyph.netId),owner=glyph.owner;
    const instance=owner.kind==='terminal'?document.instances.find(i=>i.id===owner.instanceId):null;
    const junction=owner.kind==='junction'?document.junctions.find(j=>j.id===owner.junctionId):null;
    const position=instance?.placement?.position ?? junction?.position;
    if(!net || !position || !plan.powerGeometry?.[glyph.netId] ||
       (instance && !net.terminals.some(t=>t.instanceId===instance.id && t.pinName===owner.pinName)) ||
       (junction && junction.netId!==net.id))throw new Error('NATIVE_SUPPLY_ARTWORK_WRONG_OWNER');
    const contact=derived.resolveEndpointConnection(document,resolver,owner)?.contactPoint;
    if(!close(contact,add(position,glyph.anchorOffset)))throw new Error('NATIVE_SUPPLY_ARTWORK_DETACHED');
    let attached=false;
    if(!glyph.strokes?.length)throw new Error('NATIVE_SUPPLY_ARTWORK_MISSING');
    for(const expected of glyph.strokes) {
      const object=objects.get(expected.objectId);
      if(expectedIds.has(expected.objectId))throw new Error('NATIVE_SUPPLY_ARTWORK_DUPLICATE');
      expectedIds.add(expected.objectId);
      const ownerId=instance?.id ?? junction?.id;
      if(!object || object.kind!=='arrow' || object.waypoints?.length || object.curveControls?.some(Boolean) ||
         object.from.kind!=='object' || object.to.kind!=='object' ||
         object.from.objectId!==ownerId || object.to.objectId!==ownerId ||
         object.styleOverride?.arrowStart!=='none' || object.styleOverride?.arrowEnd!=='none' ||
         object.styleOverride?.strokeScale!==expected.strokeScale)
        throw new Error('NATIVE_SUPPLY_ARTWORK_MISSING_OR_CHANGED');
      const geometry=derived.resolveDraftingObjectGeometry(document,resolver,object,routing);
      if(geometry.kind!=='arrow' || geometry.diagnostics.length ||
         !close(geometry.from,add(position,expected.fromOffset)) ||
         !close(geometry.to,add(position,expected.toOffset)))throw new Error('NATIVE_SUPPLY_ARTWORK_DETACHED');
      attached ||= close(geometry.from,contact) || close(geometry.to,contact);
    }
    if(!attached)throw new Error('NATIVE_SUPPLY_ARTWORK_DETACHED');
  }
  for(const id of objects.keys())if(/^n2s-supply-ink-[0-9a-f]{18}$/u.test(id) && !expectedIds.has(id))
    throw new Error('NATIVE_SUPPLY_ARTWORK_ORPHAN');
  return {status:'valid',checked:true,glyphs:plan.powerPresentation.glyphs.length,strokes:expectedIds.size};
}

export function verifyVisibleSupplies(document,resolver,plan,derived,model) {
  if(!plan.powerGeometry)return {status:'legacy-plan-without-supply-geometry',checked:false};
  const groups=derived.resolveCommittedDocumentLogicalNets(document);
  const results=[];
  for(const [netId,expected] of Object.entries(plan.powerGeometry)) {
    const net=document.nets.find(n=>n.id===netId),group=groups.byBaseNetId.get(netId);
    if(!net || !group || group.scope!==expected.scope ||
       model.foldNetName(group.name ?? '')!==model.foldNetName(expected.name))
      throw new Error('NATIVE_SUPPLY_IDENTITY_CHANGED');
    const resolve=endpoint=>{
      if(endpoint.kind==='terminal' && !net.terminals.some(t=>t.instanceId===endpoint.instanceId && t.pinName===endpoint.pinName))
        throw new Error('NATIVE_SUPPLY_TERMINAL_WRONG_NET');
      if(endpoint.kind==='junction' && !document.junctions.some(j=>j.id===endpoint.junctionId && j.netId===netId))
        throw new Error('NATIVE_SUPPLY_OWNER_WRONG_NET');
      const geometry=derived.resolveEndpointConnection(document,resolver,endpoint);
      if(!geometry)throw new Error('NATIVE_SUPPLY_PIN_MISSING');
      return geometry.contactPoint;
    };
    const terminals=expected.terminals.map(resolve),owners=[];
    for(const terminal of document.netlist?.terminals ?? []) {
      if(terminal.netId!==netId || model.foldNetName(terminal.name)!==model.foldNetName(expected.name))continue;
      for(const id of terminal.interfaceInstanceIds)
        owners.push(resolve({kind:'terminal',instanceId:id,pinName:'P'}));
    }
    for(const claim of document.connectivityEvidence) {
      if(claim.kind!=='name-claim' || claim.netId!==netId || claim.scope!==expected.scope ||
         model.foldNetName(claim.name)!==model.foldNetName(expected.name))continue;
      if(claim.owner.kind==='net-label') {
        const annotation=document.annotations.find(a=>a.id===claim.owner.annotationId);
        const binding=annotation && derived.resolveNetLabelBinding(document,resolver,annotation);
        if(!binding || binding.netId!==netId ||
           model.foldNetName(derived.resolveAnnotationName(document,annotation))!==model.foldNetName(expected.name))
          throw new Error('NATIVE_SUPPLY_LABEL_UNBOUND');
        owners.push(resolve(binding.endpoint));
      } else if(claim.owner.kind==='power-marker') {
        const instance=document.instances.find(i=>i.id===claim.owner.objectId);
        if(!instance || !['ground','vdd-port'].includes(instance.symbolId))continue;
        for(const pin of net.terminals.filter(t=>t.instanceId===instance.id))
          owners.push(resolve({kind:'terminal',...pin}));
      }
    }
    const segments=document.routes.filter(r=>r.netId===netId).flatMap(route=>{
      const geometry=derived.resolveRouteGeometry(document,resolver,route);
      if(!geometry)throw new Error('NATIVE_SUPPLY_ROUTE_UNRESOLVED');
      return geometry.centerline.slice(1).map((p,i)=>[geometry.centerline[i],p]);
    });
    results.push({netId,name:expected.name,...verifySupplyConductors(segments,terminals,owners)});
  }
  const reference=derived.assessImportReference(document,resolver);
  const pending=reference.guides.filter(g=>[g.netId,g.fromNetId,g.toNetId].some(n=>n in plan.powerGeometry));
  if(pending.length)throw new Error('NATIVE_SUPPLY_GUIDANCE_REMAINS');
  return {status:'valid',checked:true,nets:results,remainingSupplyGuides:0,
    presentation:verifySupplyPresentation(document,resolver,plan,derived)};
}
