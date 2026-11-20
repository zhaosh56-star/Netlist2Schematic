'use strict';
(() => {
  const C=window.ReviewCore, study=JSON.parse(document.getElementById('study-data').textContent);
  const byId=new Map(study.questions.map(q=>[q.id,q])), storageKey='schematic-blind:'+study.study_id;
  const el=id=>document.getElementById(id);
  const random=()=>{const a=new Uint32Array(1);crypto.getRandomValues(a);return a[0]/4294967296;};
  const uuid=()=>crypto.randomUUID ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(16)),x=>x.toString(16).padStart(2,'0')).join('');
  function shuffled(values){const copy=[...values];for(let i=copy.length-1;i>0;i--){const j=Math.floor(random()*(i+1));[copy[i],copy[j]]=[copy[j],copy[i]];}return copy;}
  function fresh() {
    const order=study.groups.flatMap(g=>shuffled(study.questions.filter(q=>q.group===g.id).map(q=>q.id)));
    return {format:'blind-review-progress-v1',study_id:study.study_id,participant_id:uuid(),evaluator_code:'',started_at:new Date().toISOString(),order,current:order[0],
      a_slots:Object.fromEntries(order.map(id=>[id,random()<.5?0:1])),
      answers:Object.fromEntries(order.map(id=>[id,{choice:null,reason:'',updated_at:null}]))};
  }
  function valid(value) {
    if(!value || value.format!=='blind-review-progress-v1' || value.study_id!==study.study_id || typeof value.participant_id!=='string' || !value.participant_id) return false;
    if(!Array.isArray(value.order) || value.order.length!==byId.size || new Set(value.order).size!==byId.size || !byId.has(value.current))return false;
    const answersValid=value.order.every(id=>byId.has(id)&&[0,1].includes(value.a_slots?.[id])&&value.answers?.[id]&&
      (value.answers[id].choice===null||C.choices.includes(value.answers[id].choice))&&typeof value.answers[id].reason==='string'&&value.answers[id].reason.length<=2000);
    if(!answersValid)return false;
    if(value.sealed_feedback){try{C.validateFeedback(value.sealed_feedback,window.ReviewReport.decodeKey(study));if(value.sealed_feedback.participant_id!==value.participant_id)return false;}catch{return false;}}
    return true;
  }
  let state, storageAvailable=true;
  try { const saved=JSON.parse(localStorage.getItem(storageKey)||'null');state=valid(saved)?saved:fresh(); }
  catch { state=fresh();storageAvailable=false; }
  if(state.sealed_feedback)for(const answer of state.sealed_feedback.answers){state.answers[answer.question_id]={choice:answer.choice,reason:answer.reason,updated_at:answer.updated_at};state.a_slots[answer.question_id]=answer.a_slot;}
  const countAnswered=()=>Object.values(state.answers).filter(a=>C.choices.includes(a.choice)).length;
  function persist() {
    try {localStorage.setItem(storageKey,JSON.stringify(state));storageAvailable=true;}
    catch {storageAvailable=false;}
    el('save-state').textContent=storageAvailable?'进度已保存在本机':'无法自动保存，请及时下载反馈';
    el('save-state').classList.toggle('error',!storageAvailable);
    updateProgress();
  }
  function updateProgress() {
    const n=countAnswered(), total=study.questions.length;
    el('progress-text').textContent=`已评 ${n} / ${total} 题 · ${state.sealed_feedback?'本次答卷已结束':'可随时结束'}`;
    el('progress-fill').style.width=(100*n/total)+'%';
    el('complete-notice').hidden=n!==total||!!state.sealed_feedback;
    el('finalized-notice').hidden=!state.sealed_feedback;
    el('finish-top').textContent=state.sealed_feedback?'重新下载报告':'结束评测 / 下载报告';
    el('reason').readOnly=!!state.sealed_feedback;
    el('evaluator-code').disabled=!!state.sealed_feedback;
    el('clear-answer').disabled=!!state.sealed_feedback;
    for(const button of document.querySelectorAll('[data-choice]'))button.disabled=!!state.sealed_feedback;
    const active=byId.get(state.current).group;
    for(const button of document.querySelectorAll('[data-group]')) {
      const qs=study.questions.filter(q=>q.group===button.dataset.group);
      const done=qs.filter(q=>state.answers[q.id].choice!==null).length;
      button.querySelector('small').textContent=`${done} / ${qs.length}`;
      button.classList.toggle('active',button.dataset.group===active);
    }
    for(const button of document.querySelectorAll('[data-question]')) {
      button.classList.toggle('answered',state.answers[button.dataset.question].choice!==null);
      button.classList.toggle('current',button.dataset.question===state.current);
      button.setAttribute('aria-current',button.dataset.question===state.current?'step':'false');
    }
  }
  function renderQuestion() {
    const q=byId.get(state.current), index=state.order.indexOf(q.id), answer=state.answers[q.id];
    const group=study.groups.find(g=>g.id===q.group);
    el('question-title').textContent=`第 ${index+1} 题 / ${study.questions.length}`;
    el('question-meta').textContent=`${group.label} · 本题 ${q.device_count} 个器件`;
    for(const side of ['A','B']) {
      const image=el('image-'+side.toLowerCase()),error=el('error-'+side.toLowerCase());
      image.hidden=false;error.hidden=true;
      image.onerror=()=>{image.hidden=true;error.hidden=false;};
      image.src=q.images[side==='A'?state.a_slots[q.id]:1-state.a_slots[q.id]];
    }
    el('netlist-summary').textContent=`本题原始网表 · ${q.netlist.split(/\r?\n/).length} 行（展开查看）`;
    el('netlist-text').textContent=q.netlist;
    el('cell-label').textContent=q.cell?`评测范围：子电路 ${q.cell}`:'评测范围：顶层电路';
    el('reason').value=answer.reason;el('reason-count').textContent=`${answer.reason.length} / 2000`;
    for(const button of document.querySelectorAll('[data-choice]')) {
      const selected=button.dataset.choice===answer.choice;button.classList.toggle('selected',selected);button.setAttribute('aria-pressed',String(selected));
    }
    el('previous').disabled=index===0;
    el('next').textContent=index===study.questions.length-1?'查看结果 / 结束':'下一题';
    const grid=el('question-grid');grid.replaceChildren();
    for(const id of state.order.filter(id=>byId.get(id).group===q.group)) {
      const button=document.createElement('button');button.dataset.question=id;button.textContent=String(state.order.indexOf(id)+1).padStart(2,'0');
      button.setAttribute('aria-label','前往第 '+(state.order.indexOf(id)+1)+' 题');button.onclick=()=>go(id);grid.append(button);
    }
    updateProgress();
  }
  function go(id) {
    if(!byId.has(id))return;state.current=id;persist();renderQuestion();
    const top=el('question-title').getBoundingClientRect().top+window.scrollY-document.querySelector('.topbar').offsetHeight-18;
    window.scrollTo({top:Math.max(0,top),behavior:'auto'});
  }
  function next() {const i=state.order.indexOf(state.current);if(i+1>=state.order.length)showResults();else go(state.order[i+1]);}
  function choose(choice) {if(state.sealed_feedback)return;const a=state.answers[state.current];a.choice=choice;a.updated_at=new Date().toISOString();persist();renderChoice();}
  function renderChoice(){for(const b of document.querySelectorAll('[data-choice]')){const selected=b.dataset.choice===state.answers[state.current].choice;b.classList.toggle('selected',selected);b.setAttribute('aria-pressed',String(selected));}}
  function feedback() {
    if(state.sealed_feedback)return JSON.parse(JSON.stringify(state.sealed_feedback));
    const now=new Date().toISOString();
    const answers=state.order.map((id,position)=>{
      const q=byId.get(id),a=state.answers[id],slot=state.a_slots[id];
      return {question_id:id,display_position:position+1,group:q.group,device_count:q.device_count,choice:a.choice,reason:a.reason,
        a_slot:slot,b_slot:1-slot,a_image_sha256:q.image_hashes[slot],b_image_sha256:q.image_hashes[1-slot],netlist_sha256:q.netlist_sha256,updated_at:a.updated_at};
    });
    const n=countAnswered();
    return {format:'blind-schematic-feedback-v1',study_id:study.study_id,study_fingerprint:study.fingerprint,
      participant_id:state.participant_id,evaluator_code:state.evaluator_code,started_at:state.started_at,exported_at:now,
      status:n===byId.size?'completed':'stopped_early',question_count:byId.size,answered_count:n,unanswered_count:byId.size-n,
      elapsed_wall_seconds:Math.max(0,Math.round((Date.parse(now)-Date.parse(state.started_at))/1000)),
      browser_timezone:Intl.DateTimeFormat().resolvedOptions().timeZone||'',question_order:[...state.order],answers};
  }
  function makeReport(value){return window.ReviewReport.build(study,value);}
  function showResults() {
    const value=feedback(), n=C.counts(value.answers);
    el('result-answered').textContent=value.answered_count;el('result-unanswered').textContent=value.unanswered_count;
    el('result-completion').textContent=C.percent(value.answered_count,value.question_count);
    if(state.sealed_feedback){const report=window.ReviewReport,key=report.decodeKey(study),data=C.aggregate([value],key);el('result-chart').innerHTML=C.chart(report.outcomes.map(k=>data.total[k]),report.outcomes.map(k=>report.names[k]),'GT 与算法 · 已评选择占比');}
    else el('result-chart').innerHTML=C.chart(C.choices.map(k=>n[k]));
    el('close-results').textContent=state.sealed_feedback?'返回查看（答卷已锁定）':'返回继续评测';
    el('download-feedback').textContent=state.sealed_feedback?'重新下载同一份 HTML 报告':'结束并下载 HTML 报告';
    el('report-explanation').textContent=state.sealed_feedback?'本次评测已结束，答卷已锁定。下载的 HTML 已包含占比图、逐题详情、图片和网表，可直接发回组织者。':'点击下方按钮会结束本次答卷，并下载一个 HTML 报告，其中包含 GT／算法占比、分组统计、逐题选择及原因。报告会揭示图片来源，下载后答卷将锁定。';
    el('results-hint').textContent=state.sealed_feedback?'未评题不计入选择占比。“无法判断”单独保留。':'未评题不计入选择占比。“无法判断”单独保留。现在仍可返回继续答题。';
    if(!el('results-dialog').open)el('results-dialog').showModal();
  }
  let zoom=1, baseWidth=600;
  function sizeZoom() {
    const image=el('zoom-image'),area=el('zoom-area');
    if(!image.naturalWidth || !image.naturalHeight)return;
    baseWidth=Math.min(area.clientWidth-32,(area.clientHeight-32)*image.naturalWidth/image.naturalHeight);
    image.style.width=Math.max(40,baseWidth*zoom)+'px';el('zoom-level').textContent=Math.round(zoom*100)+'%';
  }
  function showZoom(side) {
    el('zoom-title').textContent='本题图 '+side;zoom=1;
    const image=el('zoom-image');image.onload=sizeZoom;image.src=el('image-'+side.toLowerCase()).src;
    el('zoom-dialog').showModal();requestAnimationFrame(sizeZoom);
  }
  for(const group of study.groups) {
    const button=document.createElement('button');button.className='group-button';button.dataset.group=group.id;
    const name=document.createElement('span');name.textContent=group.label;button.append(name,document.createElement('small'));
    button.onclick=()=>{const ids=state.order.filter(id=>byId.get(id).group===group.id);if(ids.length)go(ids.find(id=>state.answers[id].choice===null)||ids[0]);};
    el('group-buttons').append(button);
  }
  document.querySelectorAll('[data-choice]').forEach(b=>b.onclick=()=>choose(b.dataset.choice));
  document.querySelectorAll('[data-zoom]').forEach(b=>b.onclick=()=>showZoom(b.dataset.zoom));
  el('image-a').onclick=()=>showZoom('A');el('image-b').onclick=()=>showZoom('B');
  el('reason').addEventListener('input',()=>{if(state.sealed_feedback){el('reason').value=state.answers[state.current].reason;return;}const a=state.answers[state.current];a.reason=el('reason').value.slice(0,2000);a.updated_at=new Date().toISOString();el('reason-count').textContent=`${a.reason.length} / 2000`;persist();});
  el('evaluator-code').value=state.evaluator_code||'';
  el('evaluator-code').oninput=()=>{if(state.sealed_feedback)return;state.evaluator_code=el('evaluator-code').value.slice(0,64);persist();};
  el('clear-answer').onclick=()=>{if(state.sealed_feedback)return;state.answers[state.current].choice=null;state.answers[state.current].updated_at=new Date().toISOString();persist();renderChoice();};
  el('previous').onclick=()=>{const i=state.order.indexOf(state.current);if(i>0)go(state.order[i-1]);};el('next').onclick=next;
  el('next-unanswered').onclick=()=>{const i=state.order.indexOf(state.current);for(let step=1;step<=state.order.length;step++){const id=state.order[(i+step)%state.order.length];if(state.answers[id].choice===null){go(id);return;}}showResults();};
  el('finish-top').onclick=showResults;el('close-results').onclick=()=>el('results-dialog').close();
  el('download-feedback').onclick=()=>{const value=feedback(),html=makeReport(value);state.sealed_feedback=value;persist();renderQuestion();showResults();C.download('schematic-review-report-'+value.participant_id.slice(0,8)+'.html',html,'text/html;charset=utf-8');el('download-status').textContent='HTML 报告已生成，请将下载的这一份文件发回组织者。可以直接打开查看统计图和所有评选细节。';};
  el('restart').onclick=()=>{if(countAnswered() && !confirm('这会清空本机当前评测进度。请先下载需要保留的反馈。是否开始新的评测？'))return;state=fresh();el('evaluator-code').value='';persist();renderQuestion();};
  el('close-zoom').onclick=()=>el('zoom-dialog').close();
  el('zoom-in').onclick=()=>{zoom=Math.min(5,zoom+.25);sizeZoom();};el('zoom-out').onclick=()=>{zoom=Math.max(.5,zoom-.25);sizeZoom();};el('zoom-fit').onclick=()=>{zoom=1;sizeZoom();};
  document.addEventListener('keydown',event=>{if(event.ctrlKey||event.metaKey||event.altKey||el('results-dialog').open||el('zoom-dialog').open||event.target.matches('input,textarea,select,[contenteditable]'))return;const key=event.key.toUpperCase();if(C.choices.includes(key)){choose(key);event.preventDefault();}else if(event.key==='ArrowRight'){next();event.preventDefault();}else if(event.key==='ArrowLeft'){el('previous').click();event.preventDefault();}});
  window.addEventListener('pagehide',persist);
  window.BlindReview={getFeedback:feedback,makeReport,showResults};
  persist();renderQuestion();
})();
