"""Local HTML comparison of actual verified schematics and RSI decisions."""
from __future__ import annotations

from html import escape
import json
from pathlib import Path


def build_report(output):
    output = Path(output).resolve()
    summary_path = output / 'summary.json'
    summary = json.loads(summary_path.read_text(encoding='utf-8')) if summary_path.exists() else {}
    ledger_path = output / 'method' / 'ledger.json'
    ledger = json.loads(ledger_path.read_text(encoding='utf-8')) if ledger_path.exists() else {}
    identity_path = output / 'run.json'
    identity = json.loads(identity_path.read_text(encoding='utf-8')) if identity_path.exists() else {}
    baseline = identity.get('seed_version')
    final = summary.get('final_version', baseline)
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>电路图 Agent RSI 实验</title>',
             '<style>body{font:15px/1.6 system-ui;margin:24px auto;max-width:1500px;padding:0 20px;background:#f5f6f8;color:#17212a}section{background:white;padding:20px;margin:20px 0;border:1px solid #ddd;border-radius:8px}table{border-collapse:collapse;width:100%}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:left}.figs{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}figure{margin:0}img{width:100%;height:380px;object-fit:contain;background:white;border:1px solid #ddd}code{overflow-wrap:anywhere}.bad{color:#ac2929} @media(max-width:900px){.figs{grid-template-columns:1fr}} </style>',
             '<h1>电路图 Agent RSI 实验</h1>',
             '<p>真实器件与原始连接；冻结规则独立验收。点击图片打开完整 SVG。图形表格展示第0次测量；RSI采用还使用校准与复测种子，详见ledger。</p>',
             '<p>模式：<b>' + escape(str(identity.get('mode', 'unknown'))) + '</b>；状态：' + escape(str(summary.get('status', 'in progress'))) + '</p>']
    if identity.get('mode') == 'smoke':
        parts.append('<p class="bad">这是 synthetic 接线检查：固定源码举措，没有调用真实模型，不是 Claude 自主改进的证据。</p>')
    parts.append('<p>实际采用原 RSI v4.1 核心；轮次上限为额外声明的演示变体。主指标越高越好；实际布局 Q 越低越好。</p>')
    parts.append('<p>候选 SA 步数：' + escape(str(identity.get('iterations', '?'))) + '；参考步数：' + escape(str(summary.get('reference_iterations', '尚未测量'))) + '。不同预算的参考差距不代表只由算法造成。</p>')
    rounds = ledger.get('rounds', [])
    parts.append('<section><h2>每轮举措与采用记录</h2><table><tr><th>轮</th><th>诊断/举措</th><th>决定</th></tr>')
    for item in rounds:
        cards = item.get('cards') or []
        parts.append('<tr><td>' + escape(str(item.get('round'))) + '</td><td><pre>' + escape(json.dumps(cards, ensure_ascii=False, indent=2)) + '</pre></td><td><pre>' + escape(json.dumps(item.get('decision'), ensure_ascii=False, indent=2)) + '</pre></td></tr>')
    parts.append('</table><p><a href="method/ledger.json">完整 RSI ledger</a> · <a href="summary.json">运行摘要</a></p></section>')

    def rows_for(namespace, version):
        if not version:
            return {}
        folder = output / namespace / version[:20]
        return {p.parent.parent.name: json.loads(p.read_text(encoding='utf-8'))
                for p in folder.glob('*/0/measurement.json')}

    before = rows_for('measurements', baseline)
    after = rows_for('measurements', final)
    reference = {}
    for path in (output / 'reference').glob('*/*/0/measurement.json'):
        reference[path.parent.parent.name] = json.loads(path.read_text(encoding='utf-8'))
    reached = []
    for task in sorted(set(before) | set(after) | set(reference)):
        parts.append('<section><h2>' + escape(task) + '</h2><table><tr><th>版本</th><th>合法</th><th>总Q</th><th>展示惩罚</th><th>线长</th><th>交叉</th><th>折角</th></tr>')
        figures = []
        items = [('初始基础版', before.get(task)), ('最终采用版', after.get(task)), ('当前 gongzuo 参考', reference.get(task))]
        for label, row in items:
            if not row:
                continue
            q = row.get('quality', {})
            values = [label, '通过' if row.get('valid') else row.get('error', '失败'),
                      q.get('score'), q.get('presentation', {}).get('penalty', '旧评分'), q.get('wirelength'), q.get('crossings'), q.get('bends')]
            parts.append('<tr>' + ''.join('<td>' + escape(str(v)) + '</td>' for v in values) + '</tr>')
            svg = output / row['artifacts'] / 'verified' / row.get('clean_svg', 'clean.svg')
            if row.get('valid') and svg.exists():
                relative = svg.relative_to(output).as_posix()
                figures.append('<figure><figcaption>' + escape(label) + '</figcaption><a href="' + escape(relative, quote=True) + '"><img src="' + escape(relative, quote=True) + '"></a></figure>')
        parts.append('</table><div class="figs">' + ''.join(figures) + '</div>')
        candidate, target = after.get(task), reference.get(task)
        if candidate and target:
            matched = bool(candidate.get('valid') and target.get('valid') and
                           candidate['quality']['score'] <= target['quality']['score'] + 1e-9 and
                           candidate['quality']['crossings'] <= target['quality']['crossings'])
            reached.append(matched)
            parts.append('<p>本图达到参考的 Q/交叉水平：' + ('是' if matched else '否') + '。</p>')
        parts.append('</section>')
    if reached:
        parts.append('<p>已对照样例达到参考：' + str(sum(reached)) + '/' + str(len(reached)) + '。仅针对这些实际测量样例和预算，不代表所有电路。</p>')
    endpoint = output / 'endpoint.json'
    if endpoint.exists():
        result = json.loads(endpoint.read_text(encoding='utf-8'))
        if 'paired_delta' in result:
            parts.append('<section><h2>封存集初始版→最终版配对分差</h2><pre>' + escape(json.dumps(result['paired_delta'], indent=2, ensure_ascii=False)) + '</pre></section>')
    parts.append('</html>')
    path = output / 'report.html'
    path.write_text('\n'.join(parts), encoding='utf-8')
    return path

