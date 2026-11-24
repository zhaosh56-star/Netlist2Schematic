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
  return {status:'valid',checked:true,nets:results,remainingSupplyGuides:0};
}
