'use strict';
(() => {
  const C=window.ReviewCore;
  const outcomes=['gt','algorithm','equal','undetermined'];
  const names={gt:'GT 更好',algorithm:'算法更好',equal:'一样好',undetermined:'无法判断',unanswered:'未作答'};
  function decodeKey(study) {
    return JSON.parse(C.decoder.decode(Uint8Array.from(atob(study.report_key_b64),c=>c.charCodeAt(0))));
  }
  function build(study,value) {
    const key=decodeKey(study);C.validateFeedback(value,key);
    const data=C.aggregate([value],key), total=value.answers.length;
    const byId=new Map(key.questions.map(q=>[q.id,q]));
    const questions=new Map(study.questions.map(q=>[q.id,q]));
    const records=new Map(data.records.map(r=>[r.question_id,r]));
    const groupNames=Object.fromEntries(key.groups.map(g=>[g.id,g.label]));
    const graphic=C.chart(outcomes.map(k=>data.total[k]),outcomes.map(k=>names[k]),'GT 与算法 · 已评选择占比');
    const displayedTime=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(new Date(value.exported_at))+'（北京时间 UTC+8）';
    const decisive=data.total.gt+data.total.algorithm;
    const binary=decisive ? `在明确选择一方的 ${decisive} 次判断中，算法占 ${C.percent(data.total.algorithm,decisive)}，GT 占 ${C.percent(data.total.gt,decisive)}。` : '没有明确选择一方的判断，暂不计算二选一偏好占比。';
    const groupRows=key.groups.map(group=>{
      const n=data.groups[group.id], count=key.questions.filter(q=>q.group===group.id).length;
      const done=outcomes.reduce((sum,k)=>sum+n[k],0), decisive=n.gt+n.algorithm;
      return `<tr><td>${C.esc(group.label)}</td><td>${done} / ${count}</td><td>${n.gt}</td><td>${n.algorithm}</td><td>${n.equal}</td><td>${n.undetermined}</td><td>${decisive?C.percent(n.algorithm,decisive):'—'}</td></tr>`;
    }).join('');
    const rows=value.answers.map((answer,index)=>{
      const q=byId.get(answer.question_id), record=records.get(q.id);
      const a=q.slot_origins[answer.a_slot]==='gt'?'GT':'算法',b=q.slot_origins[answer.b_slot]==='gt'?'GT':'算法';
      const choice=answer.choice ? `${answer.choice} · ${C.choiceNames[C.choices.indexOf(answer.choice)]}` : '未作答';
      return `<tr><td>${index+1}</td><td><a href="#context-${q.id}">${C.esc(q.case.toUpperCase())}</a><small>${q.id}</small></td><td>${q.device_count}</td><td>${a}</td><td>${b}</td><td>${choice}</td><td>${names[record.result]}</td><td class="reason">${answer.reason.trim()?C.esc(answer.reason):'—'}</td></tr>`;
    }).join('');
    const contexts=value.answers.map((answer,index)=>{
      const q=byId.get(answer.question_id), source=questions.get(q.id), record=records.get(q.id);
      const labelA=q.slot_origins[answer.a_slot]==='gt'?'GT':'算法';
      const labelB=q.slot_origins[answer.b_slot]==='gt'?'GT':'算法';
      return `<details id="context-${q.id}"><summary>第 ${index+1} 题 · ${C.esc(q.case.toUpperCase())} · ${C.esc(groupNames[q.group])} · ${names[record.result]}</summary><div class="context-content"><div class="pair"><section><h3>图 A（${labelA}）</h3><img alt="图 A" src="${source.images[answer.a_slot]}"></section><section><h3>图 B（${labelB}）</h3><img alt="图 B" src="${source.images[answer.b_slot]}"></section></div><p><b>原因：</b>${answer.reason.trim()?C.esc(answer.reason).replace(/\n/g,'<br>'):'未填写'}</p><h3>原始网表${source.cell?' · '+C.esc(source.cell):''}</h3><pre>${C.esc(source.netlist)}</pre></div></details>`;
    }).join('');
    const dataScripts=`<script id="feedback-data" type="application/json">${C.safeJson(value)}</script><script id="analysis-data" type="application/json">${C.safeJson(data)}</script>`;
    return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>电路图盲测评估报告</title><style>
*{box-sizing:border-box}body{font:15px/1.7 Arial,"Microsoft YaHei",sans-serif;color:#243247;background:#f3f5f9;margin:0}main{max-width:1320px;margin:30px auto;padding:0 20px}header,.card{padding:25px;background:white;border:1px solid #dce3ed;border-radius:10px;margin-bottom:20px}h1{margin:0 0 12px;font-size:26px}h2{font-size:20px;margin:0 0 14px}h3{font-size:15px}.muted{color:#68768b;font-size:13px}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:20px 0}.stat{padding:15px;background:#f3f6fb;border-radius:7px}.stat strong{display:block;font-size:27px}.stat span{font-size:13px;color:#68768b}.chart{max-width:950px;margin:auto}.chart svg{width:100%;height:auto}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}td,th{border-bottom:1px solid #e2e7ef;text-align:left;padding:11px 9px;white-space:nowrap}th{background:#f6f8fb;color:#536882}td small{display:block;color:#8390a0}.reason{white-space:pre-wrap;min-width:190px;max-width:430px;overflow-wrap:anywhere}a{color:#3266bd}.pair{display:grid;grid-template-columns:1fr 1fr;gap:20px}.pair img{width:100%;height:540px;object-fit:contain}.pair h3{text-align:center}details{border:1px solid #dce3ed;border-radius:8px;margin:12px 0;background:white}summary{padding:14px;cursor:pointer;color:#345784}.context-content{padding:0 18px 18px}pre{background:#f6f8fc;padding:15px;max-height:360px;overflow:auto;font:13px/1.6 Consolas,monospace}.actions{display:flex;gap:18px;flex-wrap:wrap;margin-top:14px}
@media(max-width:750px){main{padding:0 10px;margin:12px auto}header,.card{padding:17px}.pair{grid-template-columns:1fr}.pair img{height:auto}.stat{padding:10px}.stat strong{font-size:22px}}
@media print{body{background:white}header,.card{border:0;padding:0}main{margin:0;max-width:none}.chart{max-width:780px}details{break-inside:avoid}.actions{display:none}}
</style></head><body><main><header><h1>电路图盲测评估报告</h1><p>评测者：${C.esc(value.evaluator_code||value.participant_id.slice(0,8))} · ${data.answered===total?'已完成全部评测':'提前结束评测'}</p><p class="muted">报告时间：${C.esc(displayedTime)} · 评测编号：${C.esc(value.participant_id)}<br>本报告在评测结束后揭示图片来源。未作答不计入偏好占比，“一样好”和“无法判断”单列。</p><div class="stats"><div class="stat"><strong>${data.answered} / ${total}</strong><span>已评题目</span></div><div class="stat"><strong>${data.total.unanswered}</strong><span>未评题目</span></div><div class="stat"><strong>${C.percent(data.answered,total)}</strong><span>完成比例</span></div></div><nav class="actions"><a href="#overall">总体偏好</a><a href="#groups">分组统计</a><a href="#details">评选细节</a><a href="#contexts">图片与网表</a></nav></header>
<section class="card" id="overall"><h2>总体偏好占比</h2><div class="chart">${graphic}</div><p>${binary}</p><p class="muted">总体柱状图以所有已评选择为分母；上述二选一比例只比较明确选 GT 或算法的判断。</p></section>
<section class="card" id="groups"><h2>按器件数量分组</h2><div class="table-wrap"><table><thead><tr><th>分组</th><th>已评 / 总题数</th><th>GT 更好</th><th>算法更好</th><th>一样好</th><th>无法判断</th><th>算法二选一占比</th></tr></thead><tbody>${groupRows}</tbody></table></div></section>
<section class="card" id="details"><h2>逐题评选细节</h2><div class="table-wrap"><table><thead><tr><th>顺序</th><th>案例 / 匿名题号</th><th>器件数</th><th>图 A 来源</th><th>图 B 来源</th><th>原始选择</th><th>对应评价</th><th>原因（可选）</th></tr></thead><tbody>${rows}</tbody></table></div></section>
<section class="card" id="contexts"><h2>图片与原始网表</h2><p class="muted">点击展开相应题目。图片和网表已内嵌，单独打开这份 HTML 即可查看。</p>${contexts}</section><p class="muted">本 HTML 同时内嵌原始选择数据和解盲统计，可直接回传作为反馈，无需其他文件。</p></main>${dataScripts}</body></html>`;
  }
  window.ReviewReport={decodeKey,build,names,outcomes};
})();
