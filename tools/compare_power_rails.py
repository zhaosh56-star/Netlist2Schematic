"""Compare existing native SVGs with optional shared-VDD views; no SA or A*."""
from __future__ import annotations

import argparse
import base64
from hashlib import sha256
from html import escape
import mimetypes
from pathlib import Path
import re
import sys
from time import perf_counter
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))

from netlist2schematic.common.sa_common import read_json,write_json
from netlist2schematic.plugins.power_rails.power_rails import generate


def _image_uri(path):
    """Keep SVGs as vector images, with all bytes inside the report HTML."""
    mime = mimetypes.guess_type(str(path))[0]
    if not mime or not mime.startswith('image/'):
        raise ValueError(f'Unsupported report image: {path}')
    return 'data:'+mime+';base64,'+base64.b64encode(Path(path).read_bytes()).decode('ascii')


def _image_viewer(document):
    if 'id="embedded-image-viewer"' in document:
        return document
    return document + '''
<style>
#embedded-image-viewer{width:95vw;height:92vh;max-width:calc(100vw - 32px);max-height:calc(100vh - 32px);padding:0;border:0;border-radius:10px;background:white}
#embedded-image-viewer::backdrop{background:rgba(0,0,0,.65)}
#embedded-image-viewer header{height:44px;box-sizing:border-box;padding:8px 12px;display:flex;gap:12px;align-items:center;justify-content:space-between;border-bottom:1px solid #ddd}
#embedded-image-viewer header span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#embedded-image-viewer img{display:block;width:100%;height:calc(100% - 44px);object-fit:contain}
#embedded-image-viewer button{cursor:pointer;padding:4px 12px;white-space:nowrap}
</style>
<dialog id="embedded-image-viewer" aria-label="图片放大预览">
<header><span></span><button type="button">关闭 / Esc</button></header><img alt="">
</dialog>
<script>
(() => {
  const viewer = document.getElementById('embedded-image-viewer');
  document.addEventListener('click', event => {
    const link = event.target.closest('a[data-svg-preview]');
    if (!link) return;
    const picture = link.querySelector('img');
    if (!picture) return;
    event.preventDefault();
    viewer.querySelector('img').src = picture.src;
    viewer.querySelector('img').alt = picture.alt;
    viewer.querySelector('header span').textContent = picture.alt || '图片预览';
    viewer.showModal();
  });
  viewer.querySelector('button').addEventListener('click', () => viewer.close());
  viewer.addEventListener('click', event => { if (event.target === viewer) viewer.close(); });
})();
</script>
'''


def embed_report_images(html_path):
    """Upgrade an existing report without rerunning any circuit processing."""
    path = Path(html_path).resolve()
    document = path.read_text(encoding='utf-8')
    replacements = {}
    def embedded(match):
        prefix, quote, source = match.group(1, 2, 3)
        if source.startswith('data:image/'):
            return match.group(0)
        uri = urlsplit(source)
        if uri.scheme == 'file':
            image = Path(url2pathname(uri.path))
            if uri.netloc and uri.netloc != 'localhost':
                image = Path('//'+uri.netloc+str(image))
        elif not uri.scheme:
            image = path.parent / unquote(uri.path)
        else:
            raise ValueError('Report embedding only reads local images: '+source)
        replacements[source] = _image_uri(image)
        return prefix+quote+replacements[source]+quote
    document = re.sub(r'(<img\b[^>]*?\bsrc=)(["\'])(.*?)\2',embedded,document,flags=re.I)
    for source in replacements:
        document = document.replace('href="'+escape(source,quote=True)+'"',
                                    'href="#embedded-image-viewer" data-svg-preview')
    document = _image_viewer(document)
    # Read all assets before replacing the page, so a missing image preserves it.
    path.write_text(document,encoding='utf-8')
    return {'embedded_images':len(replacements),'html_bytes':path.stat().st_size}


def _svg_geometry(path):
    from netlist2schematic.routing.routing_rules import merge_runs
    document=ET.parse(path).getroot()
    signals,components={},{}
    for node in document.iter():
        if node.get('data-component'):
            components[node.get('data-component')]=node.get('transform')
        if node.get('data-net') is not None and node.tag.endswith('polyline'):
            points=[tuple(map(float,p.split(','))) for p in node.get('points','').split()]
            signals.setdefault(node.get('data-net'),[]).extend(zip(points,points[1:]))
    return {'signals':{net:merge_runs(lines) for net,lines in signals.items()},'components':components}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--placements',nargs='+',required=True)
    parser.add_argument('--gt',nargs='*',help='Optional GT SVGs in the same order')
    parser.add_argument('--out-dir',default=str(ROOT/'output/power-rail-comparison'))
    args=parser.parse_args(argv)
    if args.gt and len(args.gt)!=len(args.placements):
        parser.error('--gt must have one SVG for each placement')
    destination=Path(args.out_dir).resolve();destination.mkdir(parents=True,exist_ok=True)
    rows,sections=[],[]
    def picture(path,title):
        path=Path(path) if path else None
        return ('<div><h3>'+escape(title)+'</h3>'+('<a href="#embedded-image-viewer" data-svg-preview><img alt="'+
            escape(title,quote=True)+'" src="'+_image_uri(path)+'"></a>' if path and path.is_file() else '<p>No image</p>')+'</div>')
    for i,value in enumerate(args.placements):
        path=Path(value).resolve();before=sha256(path.read_bytes()).hexdigest()
        data=read_json(path);source=Path(data['source_netlist'])
        if not source.is_absolute():source=(path.parent/source).resolve()
        gt=Path(args.gt[i]).resolve() if args.gt else source.with_suffix('.svg')
        started=perf_counter()
        try:
            result=generate(path,destination/f'case{i+1:02d}')
            validation=result['view']['validation']
            row=dict(placement=str(path),gt=str(gt),status=result['view']['status'],
                     view_json=result['artifacts']['json'],svg=result['artifacts']['clean_svg'],
                     validation=validation,diagnostics=result['view']['diagnostics'],
                     native_metrics=result['verified_full_metrics'],seconds=round(perf_counter()-started,4))
            summary=(f"供电标记 {validation['markers_before']} → {validation['markers_after']}；"
                     f"共享组 {validation['groups']}；器件与信号线不动；{row['seconds']:.2f}秒")
            new_image=result['artifacts']['clean_svg']
            base=path.stem.removesuffix('-placement')
            native=path.with_name(base+'-clean.svg')
            if native.is_file():
                expected=_svg_geometry(native)
                if any(_svg_geometry(result['artifacts'][key])!=expected for key in ('clean_svg','color_svg')):
                    raise ValueError('Extra SVG changed native signal geometry or device transforms')
                row['svg_geometry_consistent']=True
        except (ValueError,OSError,KeyError,TypeError) as exc:
            row=dict(placement=str(path),status='skipped',error=str(exc));summary=str(exc);new_image=None
        if sha256(path.read_bytes()).hexdigest()!=before:
            raise RuntimeError('Comparison changed an original placement')
        row['original_unchanged']=True;rows.append(row)
        base=path.stem.removesuffix('-placement')
        native=path.with_name(base+'-clean.svg')
        sections.append('<section><h2>'+escape(source.stem)+'</h2><p>'+escape(summary)+'</p><div class="grid">'+
            picture(gt,'GT')+picture(native,'原图')+picture(new_image,'局部共享VDD')+'</div></section>')
        print(f"{i+1}/{len(args.placements)} {row['status']}: {summary}",flush=True)
    write_json(destination/'comparison.json',dict(version='local-vdd-rails-v1',cases=rows,
        total_markers_removed=sum(r.get('validation',{}).get('markers_removed',0) for r in rows),
        no_sa_or_astar=True))
    document = ('<!doctype html><meta charset="utf-8"><title>局部共享VDD对照</title>'
        '<style>body{font:15px system-ui;margin:24px;background:#f5f7f9}section{background:white;padding:16px;'
        'margin:24px 0;border-radius:8px}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}'
        'img{width:100%;height:640px;object-fit:contain}h2{overflow-wrap:anywhere}h3{text-align:center}'
        '</style><h1>GT / 原图 / 局部共享VDD</h1><p>只合并同网、同高度、方向合适且通道空闲的局部组。'
        '未通过检查则保留原标记；不调用SA或A*。SVG图片已内嵌，可单独复制本HTML。点击图片放大。</p>'+''.join(sections))
    (destination/'index.html').write_text(_image_viewer(document),encoding='utf-8')
    print(destination/'index.html')
    return 0


if __name__=='__main__':raise SystemExit(main())
