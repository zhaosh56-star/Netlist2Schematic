'use strict';
(() => {
  const choices = ['A', 'B', 'C', 'D'];
  const choiceNames = ['A 好', 'B 好', '一样好', '无法判断'];
  const colors = ['#3266bd', '#8460b6', '#328879', '#8793a5'];
  const encoder = new TextEncoder(), decoder = new TextDecoder();
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const safeJson = value => JSON.stringify(value).replace(/</g,'\\u003c').replace(/>/g,'\\u003e').replace(/&/g,'\\u0026').replace(/\u2028/g,'\\u2028').replace(/\u2029/g,'\\u2029');
  const percent = (value, total) => total ? (100*value/total).toFixed(1)+'%' : '0.0%';
  function counts(answers) {
    const result = {A:0, B:0, C:0, D:0};
    for (const answer of answers) if (choices.includes(answer.choice)) result[answer.choice]++;
    return result;
  }
  function chart(values, labels=choiceNames, title='已评题目的选择占比') {
    const total = values.reduce((a,b) => a+b, 0), width=780, height=330;
    let svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(title)}"><rect width="780" height="330" rx="12" fill="#fff"/><g font-family="Arial,Microsoft YaHei,sans-serif" fill="#253247"><text x="28" y="36" font-size="19" font-weight="600">${esc(title)}</text><text x="28" y="61" font-size="13" fill="#68768b">已评 ${total} 次；未作答不计入占比</text>`;
    values.forEach((value,i) => {
      const y=92+i*54, length=total ? 430*value/total : 0;
      svg += `<text x="28" y="${y+19}" font-size="15">${esc(labels[i])}</text><rect x="167" y="${y}" width="430" height="28" rx="5" fill="#edf1f7"/><rect x="167" y="${y}" width="${length.toFixed(3)}" height="28" rx="5" fill="${colors[i]}"/><text x="613" y="${y+20}" font-size="15">${value} · ${percent(value,total)}</text>`;
    });
    return svg+'</g></svg>';
  }
  function csv(rows, fields) {
    function cell(value) {
      let text = String(value ?? '');
      if (/^[\s]*[=+@\-]/.test(text) || /^[\t\r]/.test(text)) text="'"+text;
      return '"'+text.replace(/"/g,'""')+'"';
    }
    return '\ufeff'+[fields.map(cell).join(','), ...rows.map(row => fields.map(field=>cell(row[field])).join(','))].join('\r\n')+'\r\n';
  }
  function download(name, content, mime='application/octet-stream') {
    const blob = content instanceof Blob ? content : new Blob([content], {type:mime});
    const url = URL.createObjectURL(blob), link=document.createElement('a');
    link.href=url; link.download=name; document.body.append(link); link.click(); link.remove();
    setTimeout(()=>URL.revokeObjectURL(url), 15000);
  }
  const crcTable = Array.from({length:256}, (_,i) => {
    let value=i; for(let bit=0;bit<8;bit++) value=(value&1) ? 0xedb88320^(value>>>1) : value>>>1;
    return value>>>0;
  });
  function crc32(bytes) {
    let value=0xffffffff;
    for(const byte of bytes) value=crcTable[(value^byte)&255]^(value>>>8);
    return (value^0xffffffff)>>>0;
  }
  function join(parts) {
    const result=new Uint8Array(parts.reduce((n,p)=>n+p.length,0));
    let cursor=0; for(const part of parts){result.set(part,cursor);cursor+=part.length;} return result;
  }
  function zip(files) {
    const local=[], central=[]; let offset=0;
    const now=new Date(), year=Math.max(1980,Math.min(2107,now.getFullYear()));
    const dosTime=(now.getHours()<<11)|(now.getMinutes()<<5)|Math.floor(now.getSeconds()/2);
    const dosDate=((year-1980)<<9)|((now.getMonth()+1)<<5)|now.getDate();
    for(const [name, content] of Object.entries(files)) {
      const path=encoder.encode(name), data=typeof content==='string' ? encoder.encode(content) : content;
      const checksum=crc32(data), header=new Uint8Array(30), h=new DataView(header.buffer);
      h.setUint32(0,0x04034b50,true);h.setUint16(4,20,true);h.setUint16(6,0x800,true);
      h.setUint16(10,dosTime,true);h.setUint16(12,dosDate,true);
      h.setUint32(14,checksum,true);h.setUint32(18,data.length,true);h.setUint32(22,data.length,true);h.setUint16(26,path.length,true);
      local.push(header,path,data);
      const entry=new Uint8Array(46), c=new DataView(entry.buffer);
      c.setUint32(0,0x02014b50,true);c.setUint16(4,20,true);c.setUint16(6,20,true);c.setUint16(8,0x800,true);
      c.setUint16(12,dosTime,true);c.setUint16(14,dosDate,true);
      c.setUint32(16,checksum,true);c.setUint32(20,data.length,true);c.setUint32(24,data.length,true);
      c.setUint16(28,path.length,true);c.setUint32(42,offset,true);central.push(entry,path);
      offset+=header.length+path.length+data.length;
    }
    const directory=join(central), end=new Uint8Array(22), e=new DataView(end.buffer);
    e.setUint32(0,0x06054b50,true);e.setUint16(8,Object.keys(files).length,true);e.setUint16(10,Object.keys(files).length,true);
    e.setUint32(12,directory.length,true);e.setUint32(16,offset,true);
    return join([...local,directory,end]);
  }
  async function zipJson(buffer) {
    const bytes=new Uint8Array(buffer), view=new DataView(buffer);let offset=0;
    while(offset+30<=bytes.length && view.getUint32(offset,true)===0x04034b50) {
      const flags=view.getUint16(offset+6,true), method=view.getUint16(offset+8,true);
      const checksum=view.getUint32(offset+14,true), size=view.getUint32(offset+18,true);
      const nameLength=view.getUint16(offset+26,true), extra=view.getUint16(offset+28,true);
      const name=decoder.decode(bytes.subarray(offset+30,offset+30+nameLength));
      const start=offset+30+nameLength+extra, end=start+size;
      if((flags&9) || end>bytes.length) throw Error('该 ZIP 格式不受支持，请解压后导入 feedback.json');
      if(name.split('/').pop()==='feedback.json') {
        let data=bytes.subarray(start,end);
        if(method===8 && typeof DecompressionStream!=='undefined') {
          data=new Uint8Array(await new Response(new Blob([data]).stream().pipeThrough(new DecompressionStream('deflate-raw'))).arrayBuffer());
        } else if(method!==0) throw Error('请解压此 ZIP，再选择 feedback.json');
        if(crc32(data)!==checksum) throw Error('ZIP 中的反馈数据校验失败');
        return JSON.parse(decoder.decode(data));
      }
      offset=end;
    }
    throw Error('ZIP 中没有 feedback.json；请解压后选择该文件');
  }
  async function readFeedback(file) {
    if(file.size>20*1024*1024) throw Error('报告超过 20 MB，请选择客户下载的原始报告 HTML');
    if(/\.zip$/i.test(file.name))return zipJson(await file.arrayBuffer());
    const text=(await file.text()).replace(/^\ufeff/,'');
    return /\.html?$/i.test(file.name)?feedbackFromHtml(text):JSON.parse(text);
  }
  function feedbackFromHtml(text) {
    const document=new DOMParser().parseFromString(text,'text/html');
    const nodes=document.querySelectorAll('script#feedback-data[type="application/json"]');
    if(nodes.length!==1)throw Error('这不是已下载的评测报告 HTML（原始问卷不能作为反馈导入）');
    return JSON.parse(nodes[0].textContent);
  }
  function validateFeedback(value, key) {
    if(!value || value.format!=='blind-schematic-feedback-v1' || value.study_id!==key.study_id) throw Error('反馈不属于本版评测');
    if(typeof value.participant_id!=='string' || !value.participant_id || value.participant_id.length>160) throw Error('评测编号缺失或不合法');
    if(!Number.isFinite(Date.parse(value.exported_at))) throw Error('反馈导出时间不合法');
    if(!Array.isArray(value.answers) || value.answers.length!==key.questions.length) throw Error('反馈题目不完整');
    const known=new Map(key.questions.map(q=>[q.id,q])), seen=new Set();
    for(const answer of value.answers) {
      const q=known.get(answer.question_id);
      if(!q || seen.has(q.id)) throw Error('反馈含未知或重复题目');
      seen.add(q.id);
      if(![0,1].includes(answer.a_slot) || answer.b_slot!==1-answer.a_slot) throw Error('A/B 图片对应信息不合法');
      if(answer.choice!==null && !choices.includes(answer.choice)) throw Error('选择必须是 A/B/C/D 或未作答');
      if(typeof answer.reason!=='string' || answer.reason.length>4000) throw Error('原因字段不合法');
      if(answer.a_image_sha256!==q.image_hashes[answer.a_slot] || answer.b_image_sha256!==q.image_hashes[answer.b_slot]) throw Error('评测图片指纹不匹配');
      if(answer.device_count!==q.device_count || answer.group!==q.group) throw Error('题目分组或器件数不匹配');
    }
    return value;
  }
  function aggregate(responses, key) {
    const names=['gt','algorithm','equal','undetermined'];
    const fresh=()=>({gt:0,algorithm:0,equal:0,undetermined:0,unanswered:0});
    const total=fresh(), groups=Object.fromEntries(key.groups.map(g=>[g.id,fresh()]));
    const byQuestion=new Map(key.questions.map(q=>[q.id,{...fresh(),...q,reasons:[]}]));
    const records=[];
    for(const response of responses) for(const answer of response.answers) {
      const q=byQuestion.get(answer.question_id);
      const result=answer.choice===null ? 'unanswered' : answer.choice==='C' ? 'equal' : answer.choice==='D' ? 'undetermined' : q.slot_origins[answer.choice==='A' ? answer.a_slot : answer.b_slot];
      if(![...names,'unanswered'].includes(result)) throw Error('来源映射不合法');
      total[result]++; groups[q.group][result]++; q[result]++;
      const row={participant_id:response.participant_id,evaluator_code:response.evaluator_code||'',question_id:q.id,case:q.case,dataset:q.dataset,group:q.group,device_count:q.device_count,choice:answer.choice||'unanswered',result,reason:answer.reason,exported_at:response.exported_at};
      records.push(row); if(answer.reason.trim()) q.reasons.push(row);
    }
    return {format:'blind-schematic-analysis-v1',study_id:key.study_id,participants:responses.length,answered:names.reduce((n,k)=>n+total[k],0),total,groups,questions:[...byQuestion.values()],records};
  }
  function mergeResponse(responses, incoming, key) {
    validateFeedback(incoming,key);
    const existing=responses.get(incoming.participant_id);
    if(existing && Date.parse(existing.exported_at)>=Date.parse(incoming.exported_at)) return 'ignored';
    responses.set(incoming.participant_id,incoming);
    return existing ? 'updated' : 'added';
  }
  window.ReviewCore={choices,choiceNames,colors,esc,safeJson,percent,counts,chart,csv,download,zip,zipJson,feedbackFromHtml,readFeedback,validateFeedback,aggregate,mergeResponse,encoder,decoder};
})();
