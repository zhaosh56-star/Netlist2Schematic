"""Small end-to-end checks at the CLI and saved-placement interfaces."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sa_common import load_local
import run_flow


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = ROOT / 'analog-canvas-main' / 'netlists' / 'native-rc-filters' / 'circuit.spi'

    def quiet_run(self, source=None, **kwargs):
        kwargs.setdefault('layout_style', 'legacy')
        with contextlib.redirect_stdout(io.StringIO()):
            return run_flow.run(source or self.source, **kwargs)

    def command(self, script, *arguments, env=None):
        if script == 'run_flow.py' and '--layout-style' not in arguments:
            arguments = (*arguments, '--layout-style', 'legacy')
        return subprocess.run([sys.executable, str(ROOT / script), *map(str, arguments)],
                              cwd=self.directory, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=60, env=env)

    def test_cli_from_other_working_directory_writes_complete_spi_outputs(self):
        destination = self.directory / '中文输出'
        command = self.command('run_flow.py', self.source, '--iterations', 5,
                               '--seed', 123, '--out-dir', destination)
        self.assertEqual(0, command.returncode, command.stdout + command.stderr)
        data = json.loads((destination / 'circuit-placement.json').read_text(encoding='utf-8'))
        self.assertEqual(str(self.source.resolve()), data['source_netlist'])
        self.assertEqual(3, len(data['placement']))
        self.assertTrue(data['validation']['valid'])
        self.assertEqual(5, data['iterations'])
        color = ET.parse(destination / 'circuit-layered.svg').getroot()
        clean = ET.parse(destination / 'circuit-clean.svg').getroot()
        namespace = {'svg': 'http://www.w3.org/2000/svg'}
        color_routes = [(part.attrib['data-net'], part.attrib['points'])
                        for part in color.findall('svg:polyline[@data-net]', namespace)]
        clean_routes = [(part.attrib['data-net'], part.attrib['points'])
                        for part in clean.findall('svg:polyline[@data-net]', namespace)]
        self.assertEqual(color_routes, clean_routes)
        self.assertTrue(color_routes)
        self.assertEqual(3, len(clean.findall('svg:g[@data-component]', namespace)))
        title = clean.findall('svg:text', namespace)[-1].text
        self.assertIn(f"WL={data['metrics']['wirelength']:.0f}", title)

    def test_default_outputs_use_one_project_folder_per_netlist_and_cell(self):
        project = self.directory / 'project'
        source = self.directory / 'filters' / 'circuit.spi'
        source.parent.mkdir()
        source.write_text('.subckt small IN OUT\nR1 IN OUT 1k\nC1 OUT 0 1n\n.ends small\n',
                          encoding='utf-8')
        with mock.patch.object(run_flow, 'ROOT', project):
            first = self.quiet_run(source, cell='small', iterations=0)
            destination = project / 'output' / 'filters-small'
            saved = destination / 'circuit-small-placement.json'
            self.assertTrue(saved.is_file())
            self.assertTrue((destination / 'circuit-small-layered.svg').is_file())
            self.assertTrue((destination / 'circuit-small-clean.svg').is_file())
            second = self.quiet_run(source, cell='small', iterations=0)
        self.assertEqual(['filters-small'], sorted(p.name for p in (project / 'output').iterdir()))
        self.assertEqual(first['routes'], second['routes'])
        self.assertEqual(second['metrics'], json.loads(saved.read_text(encoding='utf-8'))['metrics'])

    def test_saved_spi_json_redraw_uses_recorded_source_and_identical_routes(self):
        destination = self.directory / 'saved'
        self.quiet_run(iterations=10, seed=32, out_dir=destination)
        output = self.directory / 'redrawn.svg'
        command = self.command('render-clean.py', destination / 'circuit-placement.json', output)
        self.assertEqual(0, command.returncode, command.stdout + command.stderr)
        self.assertEqual((destination / 'circuit-clean.svg').read_bytes(), output.read_bytes())

    def test_baseline_mode_generates_valid_placement_and_svg(self):
        destination = self.directory / 'baseline'
        data = self.quiet_run(mode='baseline', iterations=15, seed=10, out_dir=destination)
        self.assertEqual('baseline', data['mode'])
        self.assertTrue(data['validation']['valid'])
        self.assertEqual(3, len({(part['row'], part['col']) for part in data['placement'].values()}))
        ET.parse(destination / 'circuit-baseline.svg')
        ET.parse(destination / 'circuit-clean.svg')

    def test_same_seed_reproduces_placement_routes_and_final_metrics(self):
        first = self.quiet_run(iterations=25, seed=1234, out_dir=self.directory / 'first')
        second = self.quiet_run(iterations=25, seed=1234, out_dir=self.directory / 'second')
        for key in ('placement', 'routes', 'metrics', 'energy'):
            with self.subTest(field=key):
                self.assertEqual(first[key], second[key])

    def test_ota_is_reproducible_across_python_hash_seeds(self):
        source = ROOT / 'analog-canvas-main' / 'fixtures' / 'simulation-acceptance' / 'ota-5t.spi'
        results = []
        for hash_seed in ('11', '53'):
            destination = self.directory / ('hash-' + hash_seed)
            environment = dict(os.environ, PYTHONHASHSEED=hash_seed)
            command = self.command('run_flow.py', source, '--cell', 'ota_5t',
                                   '--iterations', 30, '--seed', 987, '--out-dir', destination,
                                   env=environment)
            self.assertEqual(0, command.returncode, command.stdout + command.stderr)
            results.append(json.loads((destination / 'ota-5t-ota_5t-placement.json')
                                      .read_text(encoding='utf-8')))
        for key in ('placement', 'routes', 'metrics', 'energy', 'structs', 'sym_pairs'):
            with self.subTest(field=key):
                self.assertEqual(results[0][key], results[1][key])

    def test_explicit_subcircuit_is_recorded_and_redrawable(self):
        source = self.directory / 'cell.spi'
        source.write_text('* selected cell\n.subckt SMALL IN OUT VSS\n'
                          'M1 OUT IN VSS VSS NMOS\nR1 OUT VSS 1k\n.ends SMALL\n',
                          encoding='utf-8')
        destination = self.directory / 'cell-output'
        data = self.quiet_run(source, cell='SMALL', iterations=3, out_dir=destination)
        self.assertEqual('SMALL', data['source_cell'])
        self.assertEqual({'M1', 'R1'}, set(data['placement']))
        command = self.command('render-clean.py', destination / 'cell-SMALL-placement.json',
                               self.directory / 'cell-redraw.svg')
        self.assertEqual(0, command.returncode, command.stdout + command.stderr)

    def test_legacy_json_without_source_or_routes_uses_neighboring_cir(self):
        source = self.directory / 'legacy.cir'
        source.write_text('* old-style RC\nV1 in 0 1\nR1 in out 1k\nC1 out 0 1n\n',
                          encoding='utf-8')
        data = self.quiet_run(source, iterations=3, out_dir=self.directory / 'generated')
        for field in ('source_netlist', 'source_cell', 'routes', 'analog_canvas_root'):
            data.pop(field, None)
        old_json = self.directory / 'legacy-placement.json'
        old_json.write_text(json.dumps(data), encoding='utf-8')
        command = self.command('render-clean.py', old_json)
        self.assertEqual(0, command.returncode, command.stdout + command.stderr)
        ET.parse(self.directory / 'legacy-clean.svg')

    def test_unknown_device_reports_error_and_writes_no_success_artifacts(self):
        source = self.directory / 'unsupported.cir'
        source.write_text('* unsupported\nZ1 a b mystery\n', encoding='utf-8')
        destination = self.directory / 'failure'
        command = self.command('run_flow.py', source, '--iterations', 0, '--out-dir', destination)
        self.assertNotEqual(0, command.returncode)
        self.assertIn('unsupported.cir', command.stderr)
        self.assertFalse(destination.exists())

    def test_redraw_cannot_overwrite_its_source_netlist(self):
        source = self.directory / 'protected.spi'
        text = '* keep source\nV1 in 0 1\nR1 in out 1k\nC1 out 0 1n\n'
        source.write_text(text, encoding='utf-8')
        destination = self.directory / 'protected-output'
        self.quiet_run(source, iterations=0, out_dir=destination)
        command = self.command('render-clean.py', destination / 'protected-placement.json', source)
        self.assertNotEqual(0, command.returncode)
        self.assertEqual(text, source.read_text(encoding='utf-8'))

    def test_redraw_rejects_nonfinite_saved_grid_coordinates(self):
        source = self.directory / 'single.cir'
        source.write_text('* one supply\nV1 vdd 0 1\n', encoding='utf-8')
        data = self.quiet_run(source, iterations=0, out_dir=self.directory / 'single-output')
        data['placement']['V1']['row'] = float('nan')
        source_json = self.directory / 'bad-placement.json'
        source_json.write_text(json.dumps(data), encoding='utf-8')
        destination = self.directory / 'invalid.svg'
        command = self.command('render-clean.py', source_json, destination)
        self.assertNotEqual(0, command.returncode)
        self.assertFalse(destination.exists())

    def test_all_six_scripts_import_without_running_their_cli(self):
        for module, filename in (
                ('parse_netlist', 'parse-netlist.py'),
                ('device_leveling', 'device_leveling.py'),
                ('vf2_match', 'vf2_match.py'),
                ('place_and_route', 'place-and-route.py'),
                ('place_layered', 'place-layered.py'),
                ('render_clean', 'render-clean.py')):
            with self.subTest(script=filename):
                self.assertIsNotNone(load_local(module, filename))


if __name__ == '__main__':
    unittest.main()
