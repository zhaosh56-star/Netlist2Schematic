"""Build one offline questionnaire HTML that returns one self-contained report HTML."""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json
from pathlib import Path
import random
import re
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
ASSETS = Path(__file__).with_name('blind_review')
CONFIG_FORMAT = 'blind-schematic-evaluation-config-v1'
PAIRS_FORMAT = 'schematic-evaluation-pairs-v1'
VERSION = 'blind-review-builder-v1'
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from netlist2schematic.common.sa_common import read_json, write_json

GROUPS = [dict(id='le5', label='≤5 个器件', minimum=1, maximum=5),
          dict(id='6to10', label='6～10 个器件', minimum=6, maximum=10),
          dict(id='11to15', label='11～15 个器件', minimum=11, maximum=15),
          dict(id='ge16', label='≥16 个器件', minimum=16, maximum=None)]
NS = 'http://www.w3.org/2000/svg'


def group_of(count):
    if type(count) is not int or count < 1:
        raise ValueError('A study circuit must contain at least one device')
    return 'le5' if count <= 5 else '6to10' if count <= 10 else '11to15' if count <= 15 else 'ge16'


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def anonymous_svg(path):
    """Remove source-identifying metadata, keeping all visible drawing content."""
    root = ET.parse(path).getroot()
    for parent in list(root.iter()):
        for child in list(parent):
            if child.tag.rsplit('}', 1)[-1] in ('title', 'desc', 'metadata', 'script', 'foreignObject'):
                parent.remove(child)
    ids = {e.get('id'): 's'+str(i) for i, e in enumerate(root.iter()) if e.get('id')}
    classes = {c: 'c'+str(i) for i, c in enumerate(sorted({c for e in root.iter() for c in e.get('class', '').split()}))}
    for element in root.iter():
        for key in list(element.attrib):
            if key.startswith(('data-', 'aria-', 'on')):
                del element.attrib[key]
        if element.get('id'):
            element.set('id', ids[element.get('id')])
        if element.get('class'):
            element.set('class', ' '.join(classes[c] for c in element.get('class').split()))
        for key, value in list(element.attrib.items()):
            for old, new in ids.items():
                value = value.replace('url(#'+old+')', 'url(#'+new+')')
                if value == '#'+old:
                    value = '#'+new
            element.set(key, value)
        if element.tag.rsplit('}', 1)[-1] == 'style' and element.text:
            value = element.text
            for old, new in ids.items():
                value = re.sub(r'#'+re.escape(old)+r'(?![\w-])', '#'+new, value)
            for old, new in classes.items():
                value = re.sub(r'\.'+re.escape(old)+r'(?![\w-])', '.'+new, value)
            element.text = value
    root.set('role', 'img')
    if root.get('viewBox'):
        box = list(map(float, root.get('viewBox').split()))
        if len(box) == 4 and box[2] > 0 and box[3] > 0:
            root.set('width', str(box[2]))
            root.set('height', str(box[3]))
    ET.register_namespace('', NS)
    return ET.tostring(root, encoding='utf-8')


def image_asset(path):
    """Inline either a source-anonymized SVG or an ordinary raster reference."""
    path = Path(path)
    if path.suffix.casefold() == '.svg':
        return 'image/svg+xml', anonymous_svg(path)
    raw = path.read_bytes()
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png', raw
    if raw.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg', raw
    if raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        return 'image/webp', raw
    raise ValueError('Expected an SVG, PNG, JPEG or WebP image: '+str(path))


def unmerged_variant(row, folder):
    """Render any requested unmerged variant from its saved route, without search."""
    from netlist2schematic.plugins.power_rails import power_rails
    from netlist2schematic.rendering.sa_render import render_svg
    from netlist2schematic.rendering.formal_svg import formalize_svg
    from tools.compare_power_rails import _svg_geometry
    context = power_rails._load_context(row['input_placement'])
    folder.mkdir(parents=True, exist_ok=True)
    raw, formal = folder/'native-clean.svg', folder/'unmerged-formal.svg'
    render_svg(context['par'], context['placement'], context['comps'], context['nets'],
               context['index'], context['routed'], context['metrics'], raw, clean=True,
               validation=context['validation'], presentation_hints=context['hints'])
    style = formalize_svg(raw, formal, context, {'groups': []})
    if _svg_geometry(formal) != _svg_geometry(raw):
        raise ValueError('Unmerged variant changed devices or signals')
    report = dict(case=row['tag'], power_rails='off', original_marker_count=len(context['terminals']),
                  variant_marker_count=len(context['terminals']), signal_metrics=context['metrics'],
                  native_validation=context['validation'], style=style,
                  source_placement=row['input_placement'], source_sha256=row['placement_sha256'],
                  formal_svg_sha256=digest(formal), no_layout_or_routing_search=True)
    write_json(folder/'verification.json', report)
    return formal, report


def script_json(value):
    # A source netlist can contain HTML-like model syntax; never terminate a script.
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')


def render_page(template, payload, app):
    page = (ASSETS/template).read_text(encoding='utf-8')
    for key, content in (
        ('__CSS__', (ASSETS/'review.css').read_text(encoding='utf-8')),
        ('__SHARED_JS__', (ASSETS/'review_shared.js').read_text(encoding='utf-8')),
        ('__REPORT_JS__', (ASSETS/'review_report.js').read_text(encoding='utf-8')),
        ('__APP_JS__', (ASSETS/app).read_text(encoding='utf-8')),
        ('__PAYLOAD__', script_json(payload))):
        if key in ('__SHARED_JS__', '__REPORT_JS__', '__APP_JS__'):
            content = content.replace('</script', r'<\/script')
        page = page.replace(key, content)
    return page


def _case_tags(values, name):
    if not isinstance(values, (list, tuple)) or any(
        not isinstance(tag, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', tag) for tag in values
    ):
        raise ValueError(name+' must be a list of case IDs (letters, digits, _ or -)')
    normalized = tuple(tag.casefold() for tag in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError('Duplicate case ID in '+name)
    return normalized


def load_config(path):
    """Configuration paths are relative to the JSON file, never the launch folder."""
    path = Path(path).resolve()
    config = read_json(path)
    if not isinstance(config, dict) or config.get('format') != CONFIG_FORMAT:
        raise ValueError('Expected '+CONFIG_FORMAT+' configuration')
    unknown = set(config)-{'format', 'manifest', 'out_dir', 'seed', 'exclusions', 'unmerged'}
    if unknown:
        raise ValueError('Unknown evaluation setting: '+', '.join(sorted(unknown)))
    result = {}
    for field in ('manifest', 'out_dir'):
        value = config.get(field)
        if field == 'manifest' or value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ValueError('Configuration requires a nonempty '+field+' path')
            result[field] = (path.parent/value).resolve()
    seed = config.get('seed', 20261119)
    if type(seed) is not int:
        raise ValueError('seed must be an integer')
    result.update(seed=seed, exclusions=_case_tags(config.get('exclusions', []), 'exclusions'),
                  unmerged=_case_tags(config.get('unmerged', []), 'unmerged'),
                  config_path=path, config_sha256=digest(path))
    return result


def _manifest_rows(manifest_path, exclusions, unmerged):
    data = read_json(manifest_path)
    if not isinstance(data, dict) or not isinstance(data.get('cases'), list) or not data['cases']:
        raise ValueError('Expected an image-pairs or formal-gallery manifest with a nonempty cases list')
    tags = _case_tags([row.get('tag') if isinstance(row, dict) else None for row in data['cases']], 'cases')
    for name, requested in (('exclusions', exclusions), ('unmerged', unmerged)):
        missing = set(requested)-set(tags)
        if missing:
            raise ValueError('Unknown case ID in '+name+': '+', '.join(sorted(missing)))
    if set(unmerged) & set(exclusions):
        raise ValueError('An excluded case cannot request an unmerged variant')
    rows = []
    for original in data['cases']:
        if original['tag'].casefold() in exclusions:
            continue
        row = dict(original)
        if data.get('format') == PAIRS_FORMAT:
            for field, target in (('netlist', 'source'), ('gt', 'reference_svg'), ('algorithm', 'formal_svg')):
                value = row.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError('Missing '+field+': '+row['tag'])
                row[target] = str((manifest_path.parent/value).resolve())
            group_of(row.get('device_count'))
            row['source_sha256'] = row.get('netlist_sha256', digest(row['source']))
            row['formal_svg_sha256'] = row.get('algorithm_sha256', digest(row['formal_svg']))
            row['reference_sha256'] = row.get('gt_sha256', digest(row['reference_svg']))
            row.update(input_placement=None, placement_sha256=None)
            row.setdefault('dataset', str(data.get('dataset', manifest_path.parent.name)))
            rows.append(row)
            continue
        for field in ('source', 'input_placement', 'formal_svg'):
            value = row.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError('Missing '+field+': '+row['tag'])
            row[field] = str((manifest_path.parent/value).resolve())
            expected = row.get(field.replace('input_placement', 'placement')+'_sha256')
            if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
                raise ValueError('Missing or invalid '+field+' hash: '+row['tag'])
        reference = row.get('reference_svg', row.get('gt_svg'))
        if reference is not None and (not isinstance(reference, str) or not reference.strip()):
            raise ValueError('Invalid reference drawing path: '+row['tag'])
        row['reference_svg'] = str((manifest_path.parent/reference).resolve()) if reference else str(Path(row['source']).with_suffix('.svg'))
        row.setdefault('dataset', str(data.get('dataset', manifest_path.parent.name)))
        rows.append(row)
    if not rows:
        raise ValueError('No study cases remain after exclusions')
    return rows


def build(manifest_path, destination, *, seed=20261119, exclusions=(), unmerged=(), config_path=None):
    """Build an offline study from saved formal figures; case overrides are explicit."""
    manifest_path, destination = Path(manifest_path).resolve(), Path(destination).resolve()
    if destination == manifest_path.parent or manifest_path.is_relative_to(destination):
        raise ValueError('Blind study must have a separate output directory')
    if type(seed) is not int:
        raise ValueError('seed must be an integer')
    exclusions, unmerged = _case_tags(exclusions, 'exclusions'), _case_tags(unmerged, 'unmerged')
    rows = _manifest_rows(manifest_path, exclusions, unmerged)
    manifest_hash = digest(manifest_path)
    config_path = Path(config_path).resolve() if config_path is not None else None
    config_hash = digest(config_path) if config_path is not None else None
    private = destination/'researcher'
    participant = destination/'participant'
    private.mkdir(parents=True, exist_ok=True)
    participant.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    prepared, source_checks = [], {}
    for row in rows:
        source = Path(row['source'])
        native = Path(row['input_placement']) if row['input_placement'] is not None else None
        if digest(source) != row['source_sha256'] or (native is not None and digest(native) != row['placement_sha256']):
            raise ValueError('Original circuit or placement changed: '+row['tag'])
        if digest(row['formal_svg']) != row['formal_svg_sha256']:
            raise ValueError('Formal drawing changed: '+row['tag'])
        gt = Path(row['reference_svg'])
        if not gt.is_file():
            raise ValueError('Missing reference drawing: '+row['tag'])
        if row.get('reference_sha256') is not None and digest(gt) != row['reference_sha256']:
            raise ValueError('Reference drawing changed: '+row['tag'])
        if native is not None:
            native_data = read_json(native)
            if not isinstance(native_data, dict) or not isinstance(native_data.get('placement'), dict):
                raise ValueError('Expected a saved placement object: '+row['tag'])
            count = len(native_data['placement'])
        else:
            native_data, count = {'source_cell': row.get('cell')}, row['device_count']
        group_of(count)
        drawing, variant = Path(row['formal_svg']), None
        if row['tag'].casefold() in unmerged:
            if native is None:
                raise ValueError('Unmerged rendering requires a saved placement; supply the desired image instead: '+row['tag'])
            drawing, variant = unmerged_variant(row, private/'variants'/row['tag'])
        prepared.append(dict(row=row, count=count, group=group_of(count), drawing=drawing,
                             gt=gt, native=native_data, variant=variant))
        source_checks.update({str(source): digest(source), str(gt): digest(gt), row['formal_svg']: digest(row['formal_svg'])})
        if native is not None:
            source_checks[str(native)] = digest(native)
    ordered = []
    for group in GROUPS:
        members = [item for item in prepared if item['group'] == group['id']]
        rng.shuffle(members)
        ordered.extend(members)
    origins = [i % 2 for i in range(len(ordered))]
    rng.shuffle(origins)
    questions, key = [], []
    for i, item in enumerate(ordered):
        row = item['row']
        slots = [image_asset(item['gt']), image_asset(item['drawing'])]
        slot_origins = ['gt', 'algorithm']
        if origins[i]:
            slots.reverse()
            slot_origins.reverse()
        qid = f'Q{i+1:03d}'
        netlist = Path(row['source']).read_bytes().decode('utf-8')
        hashes = [sha256(raw).hexdigest() for _, raw in slots]
        questions.append(dict(id=qid, group=item['group'], device_count=item['count'],
                              cell=item['native'].get('source_cell'), netlist=netlist,
                              netlist_sha256=row['source_sha256'], image_hashes=hashes,
                              images=['data:'+mime+';base64,'+base64.b64encode(raw).decode('ascii') for mime, raw in slots]))
        key.append(dict(id=qid, case=row['tag'], dataset=row['dataset'], group=item['group'],
                        device_count=item['count'], slot_origins=slot_origins, image_hashes=hashes,
                        source_netlist=row['source'], source_sha256=row['source_sha256'],
                        source_placement=row['input_placement'], reference_svg=str(item['gt']),
                        algorithm_svg=str(item['drawing']), variant=item['variant'] and item['variant'].get('power_rails')))
    fingerprint = sha256(json.dumps([dict(id=q['id'], group=q['group'], count=q['device_count'],
        netlist=q['netlist_sha256'], images=q['image_hashes']) for q in questions], sort_keys=True).encode()).hexdigest()
    study_id = 'schematic-review-'+fingerprint[:16]
    public = dict(format='blind-schematic-study-v1', study_id=study_id, fingerprint=fingerprint,
                  title='电路图对比评测', groups=GROUPS, questions=questions)
    secret = dict(format='blind-schematic-key-v1', study_id=study_id, fingerprint=fingerprint,
                  groups=GROUPS, questions=key, exclusions=list(exclusions), seed=seed,
                  count=len(questions), device_count_rule='Visible netlist devices; supply and ground glyphs excluded',
                  created_at=datetime.now(timezone(timedelta(hours=8))).isoformat())
    report_key = dict(format=secret['format'], study_id=study_id, fingerprint=fingerprint,
        groups=GROUPS, questions=[{field:question[field] for field in
            ('id','case','dataset','group','device_count','slot_origins','image_hashes')} for question in key])
    public['report_key_b64'] = base64.b64encode(script_json(report_key).encode('utf-8')).decode('ascii')
    public['delivery_mode'] = 'single-html-report-v1'
    if digest(manifest_path) != manifest_hash or not all(digest(p)==h for p,h in source_checks.items()):
        raise ValueError('An input changed during study preparation')
    if config_path is not None and digest(config_path) != config_hash:
        raise ValueError('Evaluation configuration changed during study preparation')
    write_json(private/'answer-key.json', secret)
    questionnaire = render_page('participant.template.html', public, 'review_participant.js')
    (destination/'index.html').write_text(questionnaire, encoding='utf-8')
    (participant/'index.html').write_text(questionnaire, encoding='utf-8')
    (private/'index.html').write_text(render_page('organizer.template.html', secret, 'review_organizer.js'), encoding='utf-8')
    (participant/'使用说明.txt').write_text(
        '电路图对比评测\n\n用 Chrome 或 Edge 双击打开 index.html，无需安装软件或联网。\n'
        '每题比较图 A 与图 B，可展开原始网表；点击图片可放大。\n'
        '选择 A 好、B 好、一样好或无法判断，原因可不填。可以跳题，也可以随时结束。\n'
        '结束时点击“结束并下载 HTML 报告”，将下载的一个 HTML 文件发回组织者即可。\n'
        '报告包含 GT／算法偏好占比、逐题选择、原因、图片与网表。无需其他文件。\n'
        '答题界面不显示来源；报告会揭示来源，因此下载报告后本次答卷锁定，可重复下载。\n'
        '进度保存在当前浏览器，评测可以提前结束。\n', encoding='utf-8')
    old_archive = destination/'发给评测者.zip'
    if old_archive.is_file():
        archive_folder = private/'archive'
        archive_folder.mkdir(exist_ok=True)
        archived = archive_folder/('legacy-questionnaire-'+digest(old_archive)[:12]+'.zip')
        index = 1
        while archived.exists():
            archived = archive_folder/('legacy-questionnaire-'+digest(old_archive)[:12]+f'-{index}.zip')
            index += 1
        old_archive.replace(archived)
    group_counts = dict(Counter(q['group'] for q in questions))
    check = dict(builder_version=VERSION, study_id=study_id, count=len(questions), groups=group_counts,
                 exclusions=list(exclusions), unmerged=list(unmerged), input_manifest=str(manifest_path),
                 seed=seed, input_manifest_sha256=manifest_hash, source_hashes=source_checks,
                 config_path=str(config_path) if config_path is not None else None, config_sha256=config_hash,
                 resolved_settings=dict(manifest=str(manifest_path), out_dir=str(destination), seed=seed,
                                        exclusions=list(exclusions), unmerged=list(unmerged)),
                 customer_file='index.html', returned_file_type='html',
                 report_decoder_embedded=True, source_labels_hidden_during_quiz=True,
                 questionnaire_sha256=digest(destination/'index.html'),
                 input_files_unchanged=all(digest(p)==h for p,h in source_checks.items()))
    if not check['input_files_unchanged']:
        raise ValueError('An input changed during study preparation')
    write_json(private/'build-manifest.json', check)
    (destination/'README.txt').write_text(
        '盲测材料：一个 HTML 发出，一个 HTML 收回\n\n只把本目录的 index.html 发给客户即可。所有图片、网表、脚本已内嵌。\n'
        '客户用 Chrome 或 Edge 打开，答题后点击“结束并下载 HTML 报告”，把下载的单个 HTML 发回。\n'
        '报告可直接打开，已有 GT／算法占比图、分组统计、逐题选择与原因、图片和网表。\n'
        '来源在答题界面隐藏，结束报告揭示来源并锁定本次答卷。为离线生成报告，问卷内嵌了报告解码信息。\n'
        '原始数据也内嵌在报告中，无需客户另交 JSON、CSV、SVG 或压缩包。\n'
        '如需合并多人反馈，researcher/index.html 可选择多份报告 HTML，在本机汇总；这一步是可选的。\n'
        '同一评测编号的重复提交只保留较新的导出，未作答与“无法判断”分开统计。\n'
        '按器件数量分为 ≤5、6～10、11～15、≥16 四组。\n'
        '排除题目：'+('、'.join(tag.upper() for tag in exclusions) or '无')+'。\n'
        '使用未合并 VDD 版本：'+('、'.join(tag.upper() for tag in unmerged) or '无')+'。\n', encoding='utf-8')
    return check


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=
        'Supply an arbitrary image-pairs manifest or an existing formal gallery manifest. '
        'All cases and supplied images are kept unless explicitly overridden. '
        'This command never runs layout, SA or A*.')
    parser.add_argument('--config', help='Study configuration JSON; paths inside it are relative to that file')
    parser.add_argument('--manifest', help='Image-pairs or formal-gallery manifest; paths inside use its directory')
    parser.add_argument('--out-dir', help='Output directory (default: output/evaluation)')
    parser.add_argument('--seed', type=int, help='Reproducible question/asset ordering seed')
    parser.add_argument('--exclude', nargs='*', metavar='CASE', help='Case IDs to omit; overrides the config list')
    parser.add_argument('--unmerged', nargs='*', metavar='CASE', help='Cases to render without VDD merging; overrides the config list')
    args = parser.parse_args(argv)
    if args.manifest is None and args.config is None:
        parser.error('Provide --manifest <pairs.json or gallery manifest.json> (or --config <config.json>)')
    try:
        config_path = args.config
        settings = load_config(config_path) if config_path is not None else {}
        manifest = args.manifest or settings.get('manifest')
        destination = Path(args.out_dir or settings.get('out_dir', ROOT/'output/evaluation')).resolve()
        seed = args.seed if args.seed is not None else settings.get('seed', 20261119)
        exclusions = args.exclude if args.exclude is not None else settings.get('exclusions', ())
        unmerged = args.unmerged if args.unmerged is not None else settings.get('unmerged', ())
        result = build(manifest, destination, seed=seed, exclusions=exclusions, unmerged=unmerged,
                       config_path=config_path)
    except (ValueError, OSError, KeyError, ET.ParseError) as exc:
        parser.exit(2, f'Error: {exc}\n')
    print('Blind study:', result['count'], 'questions;', result['groups'])
    print('Send this one HTML:', destination/'index.html')
    print('Optional researcher page:', destination/'researcher/index.html')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
