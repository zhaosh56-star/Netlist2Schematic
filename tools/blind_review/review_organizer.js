'use strict';
(() => {
  const C=window.ReviewCore,key=JSON.parse(document.getElementById('key-data').textContent),responses=new Map();
  const el=id=>document.getElementById(id);
  const sourceNames={gt:'GT 更好',algorithm:'算法更好',equal:'一样好',undetermined:'无法判断',unanswered:'未作答'};
  const outcomeOrder=['gt','algorithm','equal','undetermined'];
  function log(text){el('import-log').textContent+=text+'\n';}
  const current=()=>C.aggregate([...responses.values()],key);
  function render() {
    const data=current();el('participants').textContent=data.participants;el('answered-votes').textContent=data.answered;el('unanswered-votes').textContent=data.total.unanswered;
    el('source-chart').innerHTML=C.chart(outcomeOrder.map(k=>data.total[k]),outcomeOrder.map(k=>sourceNames[k]),'真实来源偏好 · 按已评选择次数');
    const decisive=data.total.gt+data.total.algorithm;
    el('binary-preference').textContent=`仅比较明确的二选一：算法 ${data.total.algorithm} 次（${C.percent(data.total.algorithm,decisive)}），GT ${data.total.gt} 次（${C.percent(data.total.gt,decisive)}）；一样好与无法判断单列。`;
    el('group-results').innerHTML=key.groups.map(group=>{
      const n=data.groups[group.id], answered=outcomeOrder.reduce((s,k)=>s+n[k],0), count=key.questions.filter(q=>q.group===group.id).length;
      return `<tr><td>${C.esc(group.label)}</td><td>${count}</td><td>${answered}</td><td>${n.gt}</td><td>${n.algorithm}</td><td>${n.equal}</td><td>${n.undetermined}</td><td>${C.percent(n.algorithm,n.gt+n.algorithm)}</td></tr>`;
    }).join('');
    const filter=el('case-filter').value;
    const questions=data.questions.filter(q=>filter==='all'||q.group===filter).sort((a,b)=>b.gt-a.gt || b.reasons.length-a.reasons.length || a.case.localeCompare(b.case));
    el('case-results').innerHTML=questions.map(q=>`<tr><td>${C.esc(q.case.toUpperCase())}</td><td>${q.id}</td><td>${q.device_count}</td><td>${q.gt}</td><td>${q.algorithm}</td><td>${q.equal}</td><td>${q.undetermined}</td><td>${q.unanswered}</td><td>${q.reasons.length}</td></tr>`).join('');
    const reasons=data.records.filter(r=>r.reason.trim()&&(filter==='all'||r.group===filter));
    el('reason-results').innerHTML=reasons.length ? reasons.map(r=>`<tr><td>${C.esc(r.case.toUpperCase())}</td><td>${C.esc(r.evaluator_code||r.participant_id.slice(0,8))}</td><td>${C.esc(r.choice)}</td><td>${sourceNames[r.result]}</td><td class="reason-text">${C.esc(r.reason)}</td></tr>`).join('') : '<tr><td colspan="5" class="empty">尚无可选原因记录</td></tr>';
    el('export-analysis').disabled=!responses.size;
  }
  async function importFiles(files) {
    for(const file of files) {
      try {
        const value=await C.readFeedback(file), status=C.mergeResponse(responses,value,key);
        log(`${file.name}：${status==='added'?'已导入':status==='updated'?'已更新同一评测者的较新反馈':'重复或较旧反馈，已忽略'}`);
      } catch(error){log(`${file.name}：未导入 · ${error.message}`);}
    }
    render();
  }
  function reportHtml(data,graphic) {
    const groups=key.groups.map(g=>{const n=data.groups[g.id];return `<tr><td>${C.esc(g.label)}</td><td>${n.gt}</td><td>${n.algorithm}</td><td>${n.equal}</td><td>${n.undetermined}</td><td>${n.unanswered}</td></tr>`;}).join('');
    const reasons=data.records.filter(r=>r.reason.trim()).map(r=>`<tr><td>${C.esc(r.case.toUpperCase())}</td><td>${C.esc(r.evaluator_code||r.participant_id.slice(0,8))}</td><td>${sourceNames[r.result]}</td><td>${C.esc(r.reason)}</td></tr>`).join('');
    return `<!doctype html><meta charset="utf-8"><title>电路图盲测汇总</title><style>body{font:15px/1.7 Arial,Microsoft YaHei,sans-serif;max-width:1150px;margin:35px auto;padding:20px}svg{width:100%;height:auto}table{width:100%;border-collapse:collapse}td,th{padding:9px;border:1px solid #ddd;text-align:left}td:last-child{white-space:pre-wrap;overflow-wrap:anywhere}</style><h1>电路图盲测汇总</h1><p>评测者 ${data.participants} 人；已评 ${data.answered} 次；未作答 ${data.total.unanswered} 次。每位评测者每题最多计 1 次。</p>${graphic}<h2>分组统计</h2><table><tr><th>分组</th><th>GT 更好</th><th>算法更好</th><th>一样好</th><th>无法判断</th><th>未作答</th></tr>${groups}</table><h2>原因记录</h2><table><tr><th>案例</th><th>评测者</th><th>真实选择</th><th>原因</th></tr>${reasons}</table>`;
  }
  function analysisReport() {
    const data=current(), graphic=C.chart(outcomeOrder.map(k=>data.total[k]),outcomeOrder.map(k=>sourceNames[k]),'真实来源偏好 · 按已评选择次数');
    return reportHtml(data,graphic)+`<script id="analysis-data" type="application/json">${C.safeJson(data)}</script>`;
  }
  const excluded=(key.exclusions||[]).map(x=>x.toUpperCase()),unmerged=key.questions.filter(q=>q.variant==='off').map(q=>q.case);
  el('study-note').textContent=`本版共 ${key.questions.length} 题；排除项：${excluded.join('、')||'无'}。${unmerged.length?'未合并 VDD 版本：'+unmerged.join('、')+'。':''}按清单中的器件数量分组，电源/地标记不计作器件。刷新页面后需重新导入反馈文件。`;
  for(const group of key.groups){const option=document.createElement('option');option.value=group.id;option.textContent=group.label;el('case-filter').append(option);}
  el('case-filter').onchange=render;
  el('feedback-files').onchange=async event=>{const files=[...event.target.files];event.target.value='';await importFiles(files);};
  el('clear-imports').onclick=()=>{if(responses.size&&!confirm('清空本页已导入的数据？原始反馈文件不会删除。'))return;responses.clear();el('import-log').textContent='';render();};
  el('export-analysis').onclick=()=>{if(responses.size)C.download('schematic-review-analysis.html',analysisReport(),'text/html;charset=utf-8');};
  window.BlindReviewAdmin={importFiles,getAggregate:current,makeReport:analysisReport};
  render();
})();
