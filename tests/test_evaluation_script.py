"""The reusable study entrypoint packages saved drawings without layout or routing."""
from contextlib import redirect_stderr, redirect_stdout
from html.parser import HTMLParser
import io
import json
import base64
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import build_blind_review as review


class PayloadParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script' and dict(attrs).get('id') == 'study-data':
            self.active = True

    def handle_endtag(self, tag):
        if tag == 'script':
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)


class EvaluationScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.inputs = self.folder/'figures'
        self.inputs.mkdir()
        self.output = self.folder/'study'
        self.manifest = self.inputs/'manifest.json'
        self.config = self.folder/'config.json'
        # Deliberately reuse IDs from the original study to detect leaked overrides.
        cases = []
        for tag, count in (('c07', 5), ('c09', 6), ('c16', 11), ('fresh', 16)):
            source = self.inputs/(tag+'.cir')
            native = self.inputs/(tag+'-placement.json')
            algorithm = self.inputs/(tag+'-formal.svg')
            source.write_text('* </script> 原始网表\nR1 in out 1k\n', encoding='utf-8')
            native.write_text(json.dumps({'source_cell': 'main', 'placement': {
                'M'+str(index): {'row': 0, 'col': index} for index in range(count)}}), encoding='utf-8')
            for path in (source.with_suffix('.svg'), algorithm):
                path.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 20"><path d="M0 0 L20 0"/></svg>', encoding='utf-8')
            cases.append(dict(tag=tag, source=source.name, source_sha256=review.digest(source),
                              input_placement=native.name, placement_sha256=review.digest(native),
                              formal_svg=algorithm.name, formal_svg_sha256=review.digest(algorithm)))
        self.data = {'format': 'formal-gallery-v1', 'cases': cases}
        self.save_manifest()
        self.settings = dict(format=review.CONFIG_FORMAT, manifest='figures/manifest.json',
                             out_dir='study', seed=23, exclusions=['C09', 'C16'], unmerged=[])
        self.save_config()

    def save_manifest(self):
        self.manifest.write_text(json.dumps(self.data), encoding='utf-8')

    def save_config(self):
        self.config.write_text(json.dumps(self.settings), encoding='utf-8')

    def payload(self, folder=None):
        parser = PayloadParser()
        parser.feed(((folder or self.output)/'index.html').read_text(encoding='utf-8'))
        return json.loads(''.join(parser.parts))

    def run_main(self, argv):
        with redirect_stdout(io.StringIO()):
            return review.main(argv)

    def test_generic_manifest_keeps_all_cases_and_embeds_single_file_materials(self):
        self.assertEqual(0, self.run_main(['--manifest', str(self.manifest), '--out-dir', str(self.output)]))
        payload = self.payload()
        self.assertEqual(4, len(payload['questions']))
        self.assertEqual({'le5', '6to10', '11to15', 'ge16'}, {q['group'] for q in payload['questions']})
        self.assertTrue(all(len(q['images']) == 2 and all(v.startswith('data:image/svg+xml;base64,') for v in q['images'])
                            for q in payload['questions']))
        self.assertTrue(all('</script>' in q['netlist'] for q in payload['questions']))
        self.assertEqual('single-html-report-v1', payload['delivery_mode'])
        self.assertTrue(payload['report_key_b64'])
        key = review.read_json(self.output/'researcher/answer-key.json')
        self.assertEqual({'c07', 'c09', 'c16', 'fresh'}, {q['case'] for q in key['questions']})
        self.assertEqual((self.output/'index.html').read_bytes(), (self.output/'participant/index.html').read_bytes())
        self.assertFalse(list(self.output.glob('*.zip')))

    def test_optional_configuration_applies_only_explicit_study_overrides(self):
        self.assertEqual(0, self.run_main(['--config', str(self.config)]))
        check = review.read_json(self.output/'researcher/build-manifest.json')
        self.assertEqual(2, check['count'])
        self.assertEqual(['c09', 'c16'], check['exclusions'])
        self.assertEqual(str(self.config.resolve()), check['config_path'])
        self.assertEqual(review.digest(self.config), check['config_sha256'])
        self.assertEqual(23, check['seed'])
        self.assertTrue(check['input_files_unchanged'])

    def test_configuration_paths_use_the_file_directory_and_cli_can_clear_lists(self):
        settings = review.load_config(self.config)
        self.assertEqual(self.manifest.resolve(), settings['manifest'])
        self.assertEqual(self.output.resolve(), settings['out_dir'])
        other = self.folder/'other'
        self.assertEqual(0, self.run_main(['--config', str(self.config), '--out-dir', str(other),
                                         '--seed', '17', '--exclude', '--unmerged']))
        check = review.read_json(other/'researcher/build-manifest.json')
        self.assertEqual(4, check['count'])
        self.assertEqual([], check['exclusions'])
        self.assertEqual(17, check['seed'])
        self.assertFalse(self.output.exists())

    def test_requested_unmerged_variants_are_case_insensitive_and_explicit(self):
        def variant(row, folder):
            self.assertEqual('c07', row['tag'])
            return Path(row['formal_svg']), {'power_rails': 'off'}
        with patch.object(review, 'unmerged_variant', side_effect=variant) as render:
            review.build(self.manifest, self.output, unmerged=('C07',))
        self.assertEqual(1, render.call_count)
        key = review.read_json(self.output/'researcher/answer-key.json')
        self.assertEqual(['c07'], [q['case'] for q in key['questions'] if q['variant'] == 'off'])

    def test_unknown_or_duplicate_case_ids_fail_before_publishing(self):
        for kwargs in ({'exclusions': ('absent',)}, {'exclusions': ('c09', 'C09')},
                       {'unmerged': ('../escape',)}, {'exclusions': ('c07',), 'unmerged': ('c07',)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                review.build(self.manifest, self.output, **kwargs)
        self.data['cases'][1]['tag'] = 'C07'
        self.save_manifest()
        with self.assertRaises(ValueError):
            review.build(self.manifest, self.output)
        self.assertFalse((self.output/'index.html').exists())

    def test_invalid_settings_do_not_silently_build_another_study(self):
        for change in ({'format': 'v0'}, {'exclusions': 'c09'}, {'seed': True}, {'ouput_dir': 'typo'}):
            with self.subTest(change=change):
                self.config.write_text(json.dumps(dict(self.settings, **change)), encoding='utf-8')
                with self.assertRaises(ValueError):
                    review.load_config(self.config)

    def test_changed_input_rejected_without_overwriting_existing_questionnaire(self):
        review.build(self.manifest, self.output)
        previous = (self.output/'index.html').read_bytes()
        (self.inputs/'c09.cir').write_text('R2 wrong connection 2k', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'changed'):
            review.build(self.manifest, self.output)
        self.assertEqual(previous, (self.output/'index.html').read_bytes())

    def test_rebuilding_with_same_inputs_preserves_study_identity_and_questionnaire(self):
        review.build(self.manifest, self.output, seed=44)
        previous = (self.output/'index.html').read_bytes()
        another = self.folder/'rebuild'
        review.build(self.manifest, another, seed=44)
        self.assertEqual(previous, (another/'index.html').read_bytes())
        self.assertEqual(self.payload()['study_id'], self.payload(another)['study_id'])

    def test_explicit_reference_path_and_separate_output(self):
        custom = self.inputs/'references'
        custom.mkdir()
        reference = custom/'reference.svg'
        reference.write_bytes((self.inputs/'c07.svg').read_bytes())
        (self.inputs/'c07.svg').unlink()
        self.data['cases'][0]['reference_svg'] = 'references/reference.svg'
        self.save_manifest()
        review.build(self.manifest, self.output)
        self.assertEqual(4, len(self.payload()['questions']))
        with self.assertRaises(ValueError):
            review.build(self.manifest, self.inputs)

    def test_cli_reports_errors_without_a_traceback(self):
        with redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit) as failure:
            review.main(['--manifest', str(self.manifest), '--exclude', 'missing', '--out-dir', str(self.output)])
        self.assertEqual(2, failure.exception.code)
        self.assertIn('Unknown case ID', error.getvalue())
        self.assertNotIn('Traceback', error.getvalue())

    def test_no_arguments_require_input_instead_of_reusing_a_historical_study(self):
        with redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit) as failure:
            review.main([])
        self.assertEqual(2, failure.exception.code)
        self.assertIn('Provide --manifest', error.getvalue())
        self.assertFalse(self.output.exists())

    def make_pairs(self):
        pairs = self.folder/'pairs.json'
        cases = [dict(tag=row['tag'], netlist='figures/'+row['source'],
                      gt='figures/'+row['tag']+'.svg', algorithm='figures/'+row['formal_svg'],
                      device_count=count) for row, count in zip(self.data['cases'], (5, 6, 11, 16))]
        pairs.write_text(json.dumps(dict(format=review.PAIRS_FORMAT, cases=cases)), encoding='utf-8')
        return pairs, cases

    def test_arbitrary_image_pairs_do_not_require_layouts_or_model_resources(self):
        pairs, _ = self.make_pairs()
        for row in self.data['cases']:
            (self.inputs/row['input_placement']).unlink()
        with patch.object(review, 'unmerged_variant', side_effect=AssertionError('Do not redraw supplied images')):
            review.build(pairs, self.output)
        self.assertEqual(4, len(self.payload()['questions']))
        key = review.read_json(self.output/'researcher/answer-key.json')
        self.assertTrue(all(q['source_placement'] is None for q in key['questions']))

    def test_raster_and_svg_pairs_are_embedded_with_correct_mime_types(self):
        pairs, cases = self.make_pairs()
        png = self.inputs/'reference.png'
        png.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWSkAAAAASUVORK5CYII='))
        cases[0]['gt'] = 'figures/reference.png'
        pairs.write_text(json.dumps(dict(format=review.PAIRS_FORMAT, cases=cases)), encoding='utf-8')
        review.build(pairs, self.output)
        images = [src for q in self.payload()['questions'] for src in q['images']]
        self.assertEqual(1, sum(src.startswith('data:image/png;base64,') for src in images))
        self.assertEqual(7, sum(src.startswith('data:image/svg+xml;base64,') for src in images))

    def test_pairs_reject_incorrect_counts_or_supplied_hashes(self):
        pairs, cases = self.make_pairs()
        cases[0]['device_count'] = 0
        pairs.write_text(json.dumps(dict(format=review.PAIRS_FORMAT, cases=cases)), encoding='utf-8')
        with self.assertRaises(ValueError):
            review.build(pairs, self.output)
        cases[0].update(device_count=5, gt_sha256='0'*64)
        pairs.write_text(json.dumps(dict(format=review.PAIRS_FORMAT, cases=cases)), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Reference drawing changed'):
            review.build(pairs, self.output)

    def test_supplied_images_are_not_implicitly_redrawn_as_unmerged_variants(self):
        pairs, _ = self.make_pairs()
        with self.assertRaisesRegex(ValueError, 'requires a saved placement'):
            review.build(pairs, self.output, unmerged=('c07',))

    def test_root_entrypoint_runs_from_another_working_directory(self):
        command = [sys.executable, '-X', 'utf8', '-B', str(review.ROOT/'build_evaluation.py'),
                   '--config', str(self.config)]
        result = subprocess.run(command, cwd=self.folder, capture_output=True, text=True, encoding='utf-8', timeout=15,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(str(self.output/'index.html'), result.stdout)
        self.assertEqual(2, len(self.payload()['questions']))


if __name__ == '__main__':
    unittest.main()
