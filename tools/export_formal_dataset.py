"""Export a separate formal gallery from valid dataset results and shared VDD views."""
from __future__ import annotations

import argparse
from hashlib import sha256
from html import escape
from pathlib import Path
import re
import shutil
import sys
from time import perf_counter
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from netlist2schematic.common.sa_common import read_json, write_json
from netlist2schematic.plugins.formal_output.formal_output import generate as generate_formal
from netlist2schematic.rendering.formal_svg import VERSION
from tools.compare_power_rails import _image_uri, _image_viewer


def export(summary, out_dir, *, rail_seconds=5, dataset_name=None):
    summary = Path(summary).resolve()
    destination = Path(out_dir).resolve()
    inputs = read_json(summary)
    dataset_name = dataset_name or Path(inputs.get('input_dir', 'test_data2')).name
    if not re.fullmatch(r'[A-Za-z0-9_-]+', dataset_name):
        raise ValueError('Invalid dataset name')
    zip_name = dataset_name+'-formal-svg.zip'
    cases = inputs.get('cases')
    if not isinstance(cases, list) or not cases:
        raise ValueError('Expected a completed dataset summary with cases')
    tags = [case.get('tag', '') for case in cases]
    if len(set(tags)) != len(tags) or any(not re.fullmatch(r'[A-Za-z0-9_-]+', t) for t in tags):
        raise ValueError('Invalid or duplicate dataset case tags')
    rows, sections, comparisons = [], [], []
    started = perf_counter()
    destination.mkdir(parents=True, exist_ok=True)
    for case in cases:
        if case.get('status') != 'valid':
            raise ValueError('Formal export requires a valid saved result: '+case['tag'])
        tag = case['tag']
        placement = Path(case['placement']).resolve()
        original_hash = sha256(placement.read_bytes()).hexdigest()
        source = Path(case['source']).resolve()
        source_hash = sha256(source.read_bytes()).hexdigest()
        one_started = perf_counter()
        title = tag.upper()+' · '+source.stem.replace('-', ' ')
        formal = generate_formal(placement, destination/'records'/tag/'formal-output',
                                 rail_seconds=rail_seconds, title=title)
        merged, style = formal['power_snapshot'], formal['style']
        target = destination/'figures'/(tag+'.svg')
        if target.resolve() in {placement,source,source.with_suffix('.svg')}:
            raise ValueError('Formal gallery cannot overwrite its input')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(formal['artifacts']['svg'], target)
        if sha256(placement.read_bytes()).hexdigest() != original_hash or sha256(source.read_bytes()).hexdigest() != source_hash:
            raise ValueError('Input changed during formal export: '+tag)
        gt = source.with_suffix('.svg')
        row = dict(tag=tag, source=str(source), source_sha256=source_hash,
                   input_placement=str(placement), placement_sha256=original_hash,
                   formal_svg=str(target), formal_svg_sha256=sha256(target.read_bytes()).hexdigest(),
                   shared_vdd_json=formal['artifacts']['power_json'], power_validation=merged['view']['validation'],
                   formal_record=formal['artifacts']['json'],
                   power_status=merged['view']['status'], native_metrics=formal['native_metrics'],
                   routing_stage=formal['routing_stage'],
                   selected_candidate=formal['selected_candidate'],
                   formal_style=style, input_unchanged=True, seconds=round(perf_counter()-one_started, 4))
        write_json(destination/'records'/(tag+'.json'), row)
        rows.append(row)
        uri = _image_uri(target)
        img = ('<a href="#embedded-image-viewer" data-svg-preview><img alt="'+escape(title, quote=True)
               +'" src="'+uri+'"></a>')
        sections.append('<article id="'+tag+'"><header><h2>'+escape(tag.upper())+'</h2><span>'
                        +escape(source.stem.replace('-', ' '))+'</span><a class="download" href="figures/'
                        +tag+'.svg" download>SVG</a></header>'+img+'</article>')
        gt_img = ('<img alt="Ground truth" src="'+_image_uri(gt)+'">') if gt.is_file() else '<p>GT unavailable</p>'
        comparisons.append('<article id="'+tag+'"><header><h2>'+escape(tag.upper())+'</h2><span>'+escape(source.stem)
                           +'</span></header><div class="pair"><section><h3>GT</h3>'+gt_img
                           +'</section><section><h3>正式版</h3>'+img+'</section></div></article>')
        print(f"{tag}: {merged['view']['status']}; markers {row['power_validation']['markers_before']}"
              f" -> {row['power_validation']['markers_after']}; formal labels {len(style['labels'])}", flush=True)
    total = dict(version=VERSION, dataset_name=dataset_name, input_summary=str(summary), count=len(rows),
                 native_run_status=inputs.get('status', 'complete'),
                 requested_input_count=inputs.get('requested_input_count', len(cases)),
                 elapsed_seconds=round(perf_counter()-started, 4),
                 merged_cases=sum(r['power_status'] == 'merged' for r in rows),
                 markers_removed=sum(r['power_validation']['markers_removed'] for r in rows),
                 all_inputs_unchanged=True, all_electrical_geometry_unchanged=True, cases=rows)
    write_json(destination/'manifest.json', total)
    css = '''body{margin:0;background:#f3f5f7;color:#17202b;font:14px Arial,"Microsoft YaHei",sans-serif}
nav{padding:24px 5%;background:white;border-bottom:1px solid #dde2e8}h1{margin:0 0 12px;font-size:24px}
nav p{margin:8px 0;color:#536171}nav a{color:#275c8a;margin-right:14px}.jump{line-height:2.2}
main{max-width:1280px;margin:auto;padding:24px}article{background:white;padding:20px;margin-bottom:24px;border-radius:8px}
article header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:10px}h2{font-size:18px;margin:0}
header span{flex:1;color:#536171;overflow-wrap:anywhere}.download{color:#275c8a}
article img{width:100%;max-height:780px;object-fit:contain;display:block;cursor:zoom-in}.pair{display:grid;grid-template-columns:1fr 1fr;gap:20px}
h3{text-align:center;color:#536171;font-size:14px}.pair img{height:620px}
@media(max-width:800px){main{padding:8px}.pair{grid-template-columns:1fr}.pair img{height:auto}}
@media print{body{background:white}nav,.download{display:none}main{padding:0;max-width:none}article{border:0;padding:0;page-break-after:always}article img{max-height:85vh}article header span{font-size:11px}}
'''
    nav = ('<nav><h1>'+escape(dataset_name)+' · 正式电路图</h1><p>'+str(len(rows))
           +' 张；字体与下标比例参照 GT；无引脚白圈、背景虚框和指标标题；使用已验收的局部合并 VDD 插件。</p>'
           +'<p>器件位置和信号导线保留，不能安全合并的供电端保留独立标记。图片已内嵌，可点击放大。</p>'
           +'<p><a href="index.html">正式图集</a><a href="comparison.html">与 GT 对照</a>'
           +'<a href="'+zip_name+'" download>下载 SVG 图包</a></p><div class="jump">'
           +''.join('<a href="#'+t+'">'+t.upper()+'</a>' for t in tags)+'</div></nav>')
    def page(body):
        return _image_viewer('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                             '<meta name="viewport" content="width=device-width,initial-scale=1">'
                             '<title>'+escape(dataset_name)+' 正式电路图</title><style>'+css+'</style></head><body>'+nav+'<main>'
                             +body+'</main>')+'</body></html>'
    (destination/'index.html').write_text(page(''.join(sections)), encoding='utf-8')
    (destination/'comparison.html').write_text(page(''.join(comparisons)), encoding='utf-8')
    (destination/'README.txt').write_text(
        dataset_name+' 正式图集\n打开 index.html 查看正式图，comparison.html 查看 GT 对照。\n'
        'figures/ 下为可直接用于文档的矢量 SVG，records/ 保存供电合并验收与文字布局记录。\n'
        '未重新运行布局、SA、A* 或缩线；主 flow 的默认显示设置不变。\n'
        '使用与 GT 相同的字体回退列表；本机未安装的字体由浏览器使用后续字体替代。\n', encoding='utf-8')
    with zipfile.ZipFile(destination/zip_name, 'w', zipfile.ZIP_DEFLATED) as archive:
        for file in [*sorted((destination/'figures').glob('*.svg')),
                     destination/'index.html', destination/'comparison.html', destination/'README.txt']:
            archive.write(file, file.relative_to(destination).as_posix())
    return total


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--summary', required=True, help='Completed dataset summary.json')
    parser.add_argument('--out-dir', required=True)
    parser.add_argument('--rail-seconds', type=float, default=5)
    parser.add_argument('--dataset-name', help='Gallery title and archive name (default: input directory name)')
    args = parser.parse_args(argv)
    try:
        if args.rail_seconds < 0:
            raise ValueError('rail-seconds cannot be negative')
        result = export(args.summary, args.out_dir, rail_seconds=args.rail_seconds, dataset_name=args.dataset_name)
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, f'Error: {exc}\n')
    print(f"Formal export: {result['count']} figures; {result['merged_cases']} shared-VDD cases; "
          f"{result['markers_removed']} markers removed; {result['elapsed_seconds']:.2f}s")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
