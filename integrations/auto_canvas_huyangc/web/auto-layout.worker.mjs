// Drop-in implementation of the editor's existing Worker message protocol.
// Its native session still fences revisions and owns the one-step transaction.
const API_ROOT = '/api/netlist2schematic';
const API_TOKEN = '__N2S_TOKEN__';

export async function planWithPython(request, {post, fetcher=fetch, pause=ms=>new Promise(r=>setTimeout(r,ms)), token=API_TOKEN} = {}) {
  const runId = request?.runId;
  let jobId;
  const api = async (path, options={}) => {
    const response=await fetcher(API_ROOT+path,{
      ...options,cache:'no-store',credentials:'same-origin',
      headers:{'content-type':'application/json','x-netlist2schematic-token':token,...options.headers},
    });
    const result=await response.json();
    if (!response.ok) throw new Error(result.message || `Local Python service returned ${response.status}`);
    return result;
  };
  try {
    if (request?.type!=='plan') throw new Error('Expected Auto Layout plan request');
    const submitted=await api('/jobs',{method:'POST',body:JSON.stringify(request)});
    jobId=submitted.jobId;
    let state=submitted;
    while (true) {
      if (state.progress) post({type:'progress',runId,...state.progress});
      if (state.status==='succeeded') {
        if (!Array.isArray(state.results) || state.results.length!==request.documentIds.length ||
            state.runId!==runId || request.documentIds.some(id=>!state.results.some(r=>r.documentId===id)))
          throw new Error('Python service returned results for a different request');
        post({type:'result',runId,results:state.results});
        return;
      }
      if (state.status==='failed' || state.status==='cancelled')
        throw new Error(state.message || 'Python layout was cancelled or failed');
      // Polling is also a lease: terminating the Worker stops heartbeats.
      // The server then kills this job's owned Python process tree.
      await pause(500);
      state=await api('/jobs/'+jobId);
    }
  } catch (error) {
    if (jobId) {
      try {await api('/jobs/'+jobId,{method:'DELETE'});} catch { /* lease also expires */ }
    }
    post({type:'failed',runId,message:error instanceof Error?error.message:String(error)});
  }
}

if (typeof self!=='undefined' && typeof self.postMessage==='function') {
  let running=false;
  self.onmessage=event=>{
    if (running) return;
    running=true;
    void planWithPython(event.data,{post:message=>self.postMessage(message)});
  };
}
