// Native marker footprints can differ from the engine's visual-marker policy.
// Repair only those presentation seats; electrical devices never move here.
export function markerSeats(document, plan, context) {
  const { resolver, derived, model, executeTransaction, auditRouting, externalSubcircuitDefinitions } = context;
  let current = document;
  let audit = auditRouting(current, resolver);
  const transactions = [], actions = [];
  const deadline = performance.now() + 5000;
  let attempts = 0;
  const endpointKey = e => e.kind === 'terminal' ? `${e.instanceId}/${e.pinName}` : `junction/${e.junctionId}`;
  const bounds = (doc, endpoint) => {
    if (endpoint.kind !== 'terminal') return null;
    const instance = doc.instances.find(i => i.id === endpoint.instanceId);
    const box = resolver.resolve(instance.symbolId, instance.symbolVariantId).definition.viewBox;
    const points = [box.x,box.x+box.width].flatMap(x => [box.y,box.y+box.height].map(y =>
      model.transformPoint({x,y},instance.placement.position,instance.placement)));
    return {x0:Math.min(...points.map(p=>p.x)),x1:Math.max(...points.map(p=>p.x)),
      y0:Math.min(...points.map(p=>p.y)),y1:Math.max(...points.map(p=>p.y))};
  };
  const escape = (doc, endpoint, geometry) => {
    const p = geometry.gridLanding, d = geometry.outward;
    if (!d) return p;
    const b = bounds(doc, endpoint);
    const distance = Math.max(12, d.x > 0 ? b.x1-p.x+4 : d.x < 0 ? p.x-b.x0+4 :
      d.y > 0 ? b.y1-p.y+4 : p.y-b.y0+4);
    return {x:p.x+d.x*distance,y:p.y+d.y*distance};
  };
  const on = (p,a,b) => ((a.x===b.x && p.x===a.x && p.y>=Math.min(a.y,b.y) && p.y<=Math.max(a.y,b.y)) ||
    (a.y===b.y && p.y===a.y && p.x>=Math.min(a.x,b.x) && p.x<=Math.max(a.x,b.x)));
  const intersects = (a,b,c,d) => {
    if ([a,b].some(p=>on(p,c,d)) || [c,d].some(p=>on(p,a,b))) return true;
    if ((a.x===b.x)===(c.x===d.x)) return false;
    const p=a.x===b.x ? {x:a.x,y:c.y} : {x:c.x,y:a.y};
    return on(p,a,b) && on(p,c,d);
  };
  const separated = (a,b,c,d) => {
    if (intersects(a,b,c,d)) return false;
    if (a.x===b.x && c.x===d.x && Math.abs(a.x-c.x)<10 &&
        Math.min(Math.max(a.y,b.y),Math.max(c.y,d.y))>=Math.max(Math.min(a.y,b.y),Math.min(c.y,d.y))) return false;
    if (a.y===b.y && c.y===d.y && Math.abs(a.y-c.y)<10 &&
        Math.min(Math.max(a.x,b.x),Math.max(c.x,d.x))>=Math.max(Math.min(a.x,b.x),Math.min(c.x,d.x))) return false;
    return true;
  };
  const pointsOf = (doc, route) => [derived.resolveEndpointPoint(doc,resolver,route.start),
    ...route.legs.map(l=>l.to.kind==='bend'? l.to.position : derived.resolveEndpointPoint(doc,resolver,l.to.endpoint))];
  for (const marker of plan.powerMarkers || []) {
    if (!audit.unsafeProblems.length || attempts>=64 || performance.now()>=deadline) break;
    const end = {kind:'terminal',instanceId:marker.instanceId,pinName:marker.pinName};
    const instance = current.instances.find(i=>i.id===marker.instanceId);
    const incident = current.routes.filter(r=>endpointKey(r.start)===endpointKey(end) ||
      endpointKey(r.legs.at(-1).to.endpoint)===endpointKey(end));
    if (!audit.unsafeProblems.some(p=>p.includes(marker.instanceId) || incident.some(r=>p.includes(r.id)))) continue;
    if (!instance?.placement || incident.length!==1) continue;
    const route=incident[0];
    const other=endpointKey(route.start)===endpointKey(end)?route.legs.at(-1).to.endpoint:route.start;
    const symbol=resolver.resolve(instance.symbolId,instance.symbolVariantId).definition;
    const span=Math.max(24,Math.ceil((Math.max(symbol.viewBox.width,symbol.viewBox.height)+8)/2)*2);
    const offsets=[[-span,0],[span,0],[0,span],[0,-span],[-span,span],[span,span],[-span,-span],[span,-span]];
    let accepted=false;
    for (const [dx,dy] of offsets) {
      if (accepted || attempts>=64 || performance.now()>=deadline) break;
      const position={x:instance.placement.position.x+dx,y:instance.placement.position.y+dy};
      const moved={...current,instances:current.instances.map(i=>i.id===instance.id?
        {...i,placement:{...i.placement,position}}:i)};
      const a=derived.resolveEndpointConnection(moved,resolver,other);
      const b=derived.resolveEndpointConnection(moved,resolver,end);
      if (!a || !b) continue;
      const ae=escape(moved,other,a),be=escape(moved,end,b);
      const middles=[[{x:be.x,y:ae.y}],[{x:ae.x,y:be.y}],
        [{x:ae.x-span,y:ae.y},{x:ae.x-span,y:be.y}],
        [{x:ae.x+span,y:ae.y},{x:ae.x+span,y:be.y}]];
      for (const middle of middles) {
        if (attempts>=64 || performance.now()>=deadline) break;
        const raw=[a.gridLanding,ae,...middle,be,b.gridLanding];
        const path=raw.filter((p,i)=>!i || p.x!==raw[i-1].x || p.y!==raw[i-1].y);
        const foreign=current.routes.filter(r=>r.netId!==route.netId).map(r=>pointsOf(current,r));
        if (path.slice(1).some((p,i)=>foreign.some(w=>w.slice(1).some((q,j)=>!separated(path[i],p,w[j],q))))) continue;
        const legs=path.slice(1).map((p,i)=>({id:`${route.id}-seat-leg-${i}`,mode:'manual',
          to:i===path.length-2 ? {kind:'endpoint',endpoint:end}:
            {kind:'bend',bendId:`${route.id}-seat-bend-${i}`,position:p}}));
        const edits=[{kind:'move_instance',instanceId:instance.id,position},
          {kind:'set_route_path',route:{...route,start:other,legs}}];
        const transaction={transactionId:`${plan.transactionId}-marker-${attempts++}`,
          documentId:current.id,expectedRevision:current.revision,actor:{kind:'agent',id:'native-marker-adapter'},edits};
        const trial=executeTransaction(current,transaction,{symbolResolver:resolver,externalSubcircuitDefinitions});
        if (!trial.ok) continue;
        const nextAudit=auditRouting(trial.document,resolver);
        if (nextAudit.unsafeProblems.length>=audit.unsafeProblems.length ||
            nextAudit.unsafeProblems.some(p=>!audit.unsafeProblems.includes(p))) continue;
        const pin=derived.resolveEndpointPoint(trial.document,resolver,end);
        actions.push({instanceId:instance.id,pinName:marker.pinName,previous:instance.placement.position,
          position,expectedPin:pin,reason:'native-marker-footprint',removed:audit.unsafeProblems.filter(p=>!nextAudit.unsafeProblems.includes(p))});
        transactions.push(transaction);
        current=trial.document;audit=nextAudit;accepted=true;break;
      }
    }
  }
  return {document:current,audit,transactions,actions,attempts};
}
