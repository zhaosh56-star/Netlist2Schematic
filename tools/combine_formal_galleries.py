"""Combine existing formal galleries without rerunning layout or restyling SVGs."""
from __future__ import annotations

import argparse
from hashlib import sha256
from html import escape
from pathlib import Path
import re
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from netlist2schematic.common.sa_common import read_json, write_json
from netlist2schematic.rendering.formal_svg import VERSION
from tools.compare_power_rails import _image_uri, _image_viewer


def combine(galleries, destination):
    destination = Path(destination).expanduser().resolve()
    sources = [Path(p).expanduser().resolve() for p in galleries]
    if any(destination == p or destination.is_relative_to(p) or p.is_relative_to(destination) for p in sources):
        raise ValueError('Combined gallery needs its own separate directory')
    rows, groups, used_tags = [], [], set()
    destination.mkdir(parents=True, exist_ok=True)
    for source in sources:
        data = read_json(source/'manifest.json')
        if data.get('version') != VERSION or not data.get('all_electrical_geometry_unchanged'):
            raise ValueError('Expected a verified formal gallery: '+str(source))
        name = data.get('dataset_name') or ('test_data2' if 'data2' in source.name else source.name)
        if not re.fullmatch(r'[A-Za-z0-9_-]+', name) or name in {g['name'] for g in groups}:
            raise ValueError('Invalid or duplicate gallery name')
        for row in data['cases']:
            if row['tag'] in used_tags:
                raise ValueError('Duplicate figure tag: '+row['tag'])
            used_tags.add(row['tag'])
            original = source/'figures'/(row['tag']+'.svg')
            if sha256(original.read_bytes()).hexdigest() != row['formal_svg_sha256']:
                raise ValueError('Formal SVG changed: '+str(original))
        target = destination/name
        # Copies only requested generated galleries, with no moves or deletions.
        shutil.copytree(source, target, dirs_exist_ok=True)
        group = dict(name=name, count=data['count'], original_gallery=str(source),
                     native_run_status=data.get('native_run_status', 'complete'),
                     expected_count=data.get('requested_input_count', data['count']),
                     merged_cases=data['merged_cases'], markers_removed=data['markers_removed'])
        groups.append(group)
        for row in data['cases']:
            image = target/'figures'/(row['tag']+'.svg')
            if sha256(image.read_bytes()).hexdigest() != row['formal_svg_sha256']:
                raise ValueError('Copied formal SVG differs')
            copy = dict(row, dataset=name, formal_svg=str(image),
                        original_formal_svg=row['formal_svg'])
            original_record = Path(row['shared_vdd_json']).resolve()
            copy['original_shared_vdd_json'] = str(original_record)
            copy['shared_vdd_json'] = str(target/original_record.relative_to(source))
            if row.get('formal_record'):
                copy['original_formal_record'] = row['formal_record']
                copy['formal_record'] = str(target/Path(row['formal_record']).resolve().relative_to(source))
            rows.append(copy)
    rows.sort(key=lambda r: r['tag'])
    write_json(destination/'manifest.json', dict(format='combined-formal-galleries-v1', version=VERSION,
               count=len(rows), groups=groups, all_svg_copies_identical=True, cases=rows))
    css = '''body{margin:0;background:#f3f5f7;color:#17202b;font:14px Arial,"Microsoft YaHei",sans-serif}
nav{padding:24px 5%;background:white;border-bottom:1px solid #dde2e8}h1{font-size:24px;margin:0 0 12px}
nav p{color:#536171}nav a{color:#275c8a;margin-right:15px}.jump{line-height:2.2}
button{padding:7px 15px;margin:5px 6px 5px 0;border:1px solid #bac8d5;border-radius:5px;background:white;cursor:pointer}
button.active{color:white;background:#275c8a;border-color:#275c8a}
main{max-width:1400px;margin:auto;padding:24px}article{background:white;padding:20px;margin-bottom:24px;border-radius:8px}
article header{display:flex;gap:14px;flex-wrap:wrap;align-items:baseline;margin-bottom:10px}h2{font-size:18px;margin:0}
header span{flex:1;color:#536171;overflow-wrap:anywhere}.download{color:#275c8a}
article img{display:block;width:100%;max-height:800px;object-fit:contain;cursor:zoom-in}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:20px}.pair img{height:620px}h3{text-align:center;color:#536171;font-size:14px}
@media(max-width:800px){main{padding:8px}.pair{grid-template-columns:1fr}.pair img{height:auto}}
@media print{body{background:white}nav,.download{display:none}main{padding:0}article{padding:0;page-break-after:always}article img{max-height:85vh}}
'''
    progress = ''.join('<p>正在补齐 '+escape(g['name'])+'：已导出 '+str(g['count'])+' / '+str(g['expected_count'])+' 张。</p>'
                       for g in groups if g['native_run_status'] != 'complete' or g['count'] != g['expected_count'])
    nav = ('<nav><h1>test_data + test_data2 · 正式电路图</h1><p>共 '+str(len(rows))
           +' 张。GT 风格字体与下标；无引脚白圈、背景虚框或指标标题；已使用原 VDD 局部合并插件。</p>'
           +progress
           +'<p><a href="index.html">正式图集</a><a href="comparison.html">与 GT 对照</a>'
           +'<a href="formal-figures-svg.zip" download>下载全部 SVG</a></p><div>'
           +'<button class="active" data-filter="all">全部 '+str(len(rows))+'</button>'
           +''.join('<button data-filter="'+g['name']+'">'+escape(g['name'])+' · '+str(g['count'])+'</button>' for g in groups)
           +'</div><div class="jump">'
           +''.join('<a data-dataset="'+r['dataset']+'" href="#'+r['tag']+'">'+r['tag'].upper()+'</a>' for r in rows)
           +'</div></nav>')
    filters = '''<script>document.querySelectorAll('[data-filter]').forEach(button=>button.addEventListener('click',()=>{
const key=button.dataset.filter;
document.querySelectorAll('[data-dataset]').forEach(node=>node.hidden=key!=='all'&&node.dataset.dataset!==key);
document.querySelectorAll('[data-filter]').forEach(other=>other.classList.toggle('active',other===button));
}));</script>'''
    formal, compared = [], []
    for row in rows:
        source = Path(row['source'])
        title = row['tag'].upper()+' · '+source.stem.replace('-', ' ')
        picture = ('<a href="#embedded-image-viewer" data-svg-preview><img loading="lazy" alt="'
                   +escape(title, quote=True)+'" src="'+_image_uri(row['formal_svg'])+'"></a>')
        header = ('<article id="'+row['tag']+'" data-dataset="'+row['dataset']+'"><header><h2>'
                  +row['tag'].upper()+'</h2><span>'+escape(row['dataset']+' / '+source.stem.replace('-', ' '))
                  +'</span><a class="download" href="'+row['dataset']+'/figures/'+row['tag']+'.svg" download>SVG</a></header>')
        formal.append(header+picture+'</article>')
        gt = source.with_suffix('.svg')
        gt_picture = ('<img loading="lazy" alt="GT '+row['tag']+'" src="'+_image_uri(gt)+'">') if gt.is_file() else '<p>GT unavailable</p>'
        compared.append(header+'<div class="pair"><section><h3>GT</h3>'+gt_picture
                        +'</section><section><h3>正式版</h3>'+picture+'</section></div></article>')
    def page(body):
        return _image_viewer('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
               '<meta name="viewport" content="width=device-width,initial-scale=1">'
               '<title>全部正式电路图</title><style>'+css+'</style></head><body>'+nav+'<main>'
               +body+'</main>'+filters)+'</body></html>'
    (destination/'index.html').write_text(page(''.join(formal)), encoding='utf-8')
    (destination/'comparison.html').write_text(page(''.join(compared)), encoding='utf-8')
    (destination/'README.txt').write_text(
        '正式电路图合集\n打开 index.html 浏览或按数据集筛选；comparison.html 为 GT 对照。\n'
        'test_data/ 和 test_data2/ 保存各自正式图、矢量 SVG 和验收记录。\n'
        '本次 test_data 从原网表按当前默认 flow 重新运行，test_data2 保留上一轮正式图。\n', encoding='utf-8')
    with zipfile.ZipFile(destination/'formal-figures-svg.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        files = [destination/'index.html', destination/'comparison.html', destination/'README.txt']
        for group in groups:
            files += sorted((destination/group['name']/'figures').glob('*.svg'))
        for file in files:
            archive.write(file, file.relative_to(destination).as_posix())
    return dict(count=len(rows), groups=groups, directory=str(destination))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--galleries', nargs='+', required=True)
    parser.add_argument('--out-dir', required=True)
    args = parser.parse_args(argv)
    try:
        result = combine(args.galleries, args.out_dir)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f'Error: {exc}\n')
    print('Combined formal gallery:', result['count'], 'figures;', result['directory'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
