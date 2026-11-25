// Independent checks of drawn conductors. Logical Net membership alone is
// insufficient: it also describes pins whose connection has not been drawn.
const key=p=>`${p.x},${p.y}`;
const point=p=>Array.isArray(p)?{x:p[0],y:p[1]}:p;
const on=(p,a,b)=>(a.x===b.x && p.x===a.x && p.y>=Math.min(a.y,b.y) && p.y<=Math.max(a.y,b.y)) ||
  (a.y===b.y && p.y===a.y && p.x>=Math.min(a.x,b.x) && p.x<=Math.max(a.x,b.x));

export function conductorGraph(segments,contacts=[]) {
  const lines=segments.map(([a,b])=>[point(a),point(b)]).filter(([a,b])=>key(a)!==key(b));
  for(const [a,b] of lines)if(a.x!==b.x && a.y!==b.y)throw new Error('NATIVE_NONORTHOGONAL_SIGNAL');
  const cuts=lines.map(([a,b])=>new Map([[key(a),a],[key(b),b]]));
  lines.forEach(([a,b],i)=>{
    for(const p of contacts)if(on(p,a,b))cuts[i].set(key(p),p);
    for(let j=0;j<i;j++) {
      const [c,d]=lines[j];
      const points=[a,b,c,d];
      if((a.x===b.x)!==(c.x===d.x))points.push(a.x===b.x?{x:a.x,y:c.y}:{x:c.x,y:a.y});
      for(const p of points)if(on(p,a,b)&&on(p,c,d)){cuts[i].set(key(p),p);cuts[j].set(key(p),p);}
    }
  });
  const graph=new Map();
  const vertex=p=>{const k=key(p);if(!graph.has(k))graph.set(k,new Set());return k;};
  for(const p of contacts)vertex(p);
  for(const cut of cuts){
    const points=[...cut.values()].sort((a,b)=>a.x-b.x || a.y-b.y);
    for(let i=1;i<points.length;i++){const a=vertex(points[i-1]),b=vertex(points[i]);graph.get(a).add(b);graph.get(b).add(a);}
  }
  const components=new Map();let count=0,edges=0;
  for(const [p,neighbors] of graph){
    edges+=neighbors.size;
    if(components.has(p))continue;
    const todo=[p];components.set(p,count);
    while(todo.length){const p=todo.pop();for(const n of graph.get(p))if(!components.has(n)){components.set(n,count);todo.push(n);}}
    count++;
  }
  return {components,cycles:edges/2-graph.size+count,componentCount:count};
}

function covered(a,b,segments) {
  if(key(a)===key(b))return true;
  const vertical=a.x===b.x,lo=vertical?Math.min(a.y,b.y):Math.min(a.x,b.x),hi=vertical?Math.max(a.y,b.y):Math.max(a.x,b.x);
  const intervals=segments.filter(([c,d])=>vertical?c.x===d.x&&c.x===a.x:c.y===d.y&&c.y===a.y)
    .map(([c,d])=>vertical?[Math.min(c.y,d.y),Math.max(c.y,d.y)]:[Math.min(c.x,d.x),Math.max(c.x,d.x)])
    .sort((a,b)=>a[0]-b[0]);
  let end=lo;
  for(const [start,stop] of intervals){if(start>end)break;if(stop>=end)end=stop;if(end>=hi)return true;}
  return false;
}

export function verifySignalConductors(coreSegments,drawnSegments,contacts,{allowIncomplete=false}={}) {
  const base=coreSegments.map(([a,b])=>[point(a),point(b)]);
  const drawn=drawnSegments.map(([a,b])=>[point(a),point(b)]);
  const graph=conductorGraph(drawn,contacts),original=conductorGraph(base);
  const components=new Set(contacts.map(p=>graph.components.get(key(p)))).size;
  if(components>1 && !allowIncomplete)
    throw new Error('NATIVE_VISIBLE_SIGNAL_DISCONNECTED: drawn wires do not connect every signal pin');
  for(const [a,b] of base)if(!covered(a,b,drawn))
    throw new Error(`NATIVE_CORE_SIGNAL_REMOVED: ${key(a)} to ${key(b)}`);
  if(graph.cycles>original.cycles)
    throw new Error('NATIVE_SIGNAL_LOOP_ADDED: the port adapter introduced a wire cycle');
  const joined=new Set(contacts.map(p=>graph.components.get(key(p))));
  if([...graph.components.values()].some(c=>!joined.has(c)))
    throw new Error('NATIVE_SIGNAL_ISLAND_ADDED: wire island has no signal terminal');
  return {connected:components<=1,components,coreCovered:true,cycles:graph.cycles,originalCycles:original.cycles,terminals:contacts.length};
}

export function verifyVisibleRouting(document,resolver,plan,derived,{allowIncomplete=false}={}) {
  if(!plan.signalGeometry)return {status:'legacy-plan-without-geometry',checked:false};
  const results=[];
  for(const [netId,expected] of Object.entries(plan.signalGeometry)) {
    const contacts=expected.terminals.map(endpoint=>{
      const resolved=derived.resolveEndpointConnection(document,resolver,endpoint);
      if(!resolved)throw new Error('NATIVE_SIGNAL_PIN_MISSING');
      return resolved.contactPoint;
    });
    const segments=document.routes.filter(r=>r.netId===netId).flatMap(route=>{
      const geometry=derived.resolveRouteGeometry(document,resolver,route);
      if(!geometry)throw new Error('NATIVE_SIGNAL_ROUTE_UNRESOLVED');
      return geometry.centerline.slice(1).map((p,i)=>[geometry.centerline[i],p]);
    });
    results.push({netId,...verifySignalConductors(expected.coreSegments,segments,contacts,{allowIncomplete})});
  }
  const remaining=results.reduce((sum,n)=>sum+Math.max(0,n.components-1),0);
  return {status:remaining?'incomplete':'valid',checked:true,nets:results,remainingConnections:remaining};
}
