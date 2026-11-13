"""Fixed-layout acceptance with an independent geometry oracle."""
from __future__ import annotations

from collections import defaultdict
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from netlist2schematic.common.sa_common import load_local
from netlist2schematic.common.sa_saved import load_placement
from netlist2schematic.flow import run_flow

NS = {'svg': 'http://www.w3.org/2000/svg'}


def independent_runs(segments):
    """Union intervals on the same track, ignoring arbitrary segment cuts."""
    tracks = defaultdict(list)
    for raw_a, raw_b in segments:
        a, b = tuple(raw_a), tuple(raw_b)
        if a == b:
            continue
        if a[0] == b[0]:
            tracks['v', a[0]].append(tuple(sorted((a[1], b[1]))))
        elif a[1] == b[1]:
            tracks['h', a[1]].append(tuple(sorted((a[0], b[0]))))
        else:
            raise AssertionError(f'Diagonal segment: {a}, {b}')
    merged = []
    for (axis, track), intervals in sorted(tracks.items()):
        current = None
        for lo, hi in sorted(intervals):
            if current is not None and lo <= current[1]:
                current = (current[0], max(current[1], hi))
            else:
                if current is not None:
                    merged.append((axis, track, *current))
                current = (lo, hi)
        if current is not None:
            merged.append((axis, track, *current))
    return merged


def structure_points(segments, pins):
    """Derive real ends/turns/junctions without production topology helpers."""
    runs = independent_runs(segments)
    candidates = set(map(tuple, pins))
    for axis, track, lo, hi in runs:
        candidates.update(((lo, track), (hi, track)) if axis == 'h'
                          else ((track, lo), (track, hi)))
    for axis, track, lo, hi in runs:
        if axis != 'h':
            continue
        for other, x, bottom, top in runs:
            if other == 'v' and lo <= x <= hi and bottom <= track <= top:
                candidates.add((x, track))
    result = set(map(tuple, pins))
    for x, y in candidates:
        headings = set()
        for axis, track, lo, hi in runs:
            if axis == 'h' and y == track and lo <= x <= hi:
                if lo < x:
                    headings.add('w')
                if x < hi:
                    headings.add('e')
            elif axis == 'v' and x == track and lo <= y <= hi:
                if lo < y:
                    headings.add('n')
                if y < hi:
                    headings.add('s')
        if headings and headings not in ({'w', 'e'}, {'n', 's'}):
            result.add((x, y))
    return result


def independent_foreign_checks(routes, pins, spacing=10, quiet=12):
    """Prove separation and legal X geometry directly from drawn coordinates."""
    runs = {net: independent_runs(segments) for net, segments in routes.items()}
    protected = {net: structure_points(segments, pins.get(net, ()))
                 for net, segments in routes.items()}
    crosses = set()
    names = sorted(routes)
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            for axis, track, lo, hi in runs[first]:
                for other, other_track, other_lo, other_hi in runs[second]:
                    label = f'{first}/{second}: {(axis, track, lo, hi)} / {(other, other_track, other_lo, other_hi)}'
                    if axis == other:
                        interval_gap = max(lo - other_hi, other_lo - hi, 0)
                        distance = max(abs(track-other_track), interval_gap)
                        if distance < spacing:
                            raise AssertionError(f'Parallel/endpoint clearance violation: {label}')
                        continue
                    horizontal, vertical = (((track, lo, hi), (other_track, other_lo, other_hi))
                                            if axis == 'h' else
                                            ((other_track, other_lo, other_hi), (track, lo, hi)))
                    y, left, right = horizontal
                    x, bottom, top = vertical
                    dx = max(left-x, x-right, 0)
                    dy = max(bottom-y, y-top, 0)
                    if dx == 0 and dy == 0:
                        if not left < x < right or not bottom < y < top:
                            raise AssertionError(f'Foreign endpoint or T contact: {label}')
                        for px, py in protected[first] | protected[second]:
                            if max(abs(px-x), abs(py-y)) < quiet:
                                raise AssertionError(f'Crossing near a structural point at {(px, py)}: {label}')
                        crosses.add((first, second, x, y))
                    elif max(dx, dy) < spacing:
                        raise AssertionError(f'Foreign corner/endpoint clearance violation: {label}')
    return crosses


class RerouteAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def command(self, script, *arguments):
        return subprocess.run([sys.executable, '-B', str(ROOT / script), *map(str, arguments)],
                              cwd=self.directory, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=360)

    def saved_input(self, name):
        fixture = ROOT / 'tests/fixtures' / f'fixed-{name}-placement.json'
        data = json.loads(fixture.read_text(encoding='utf8'))
        data['source_netlist'] = str((fixture.parent / data['source_netlist']).resolve())
        saved = self.directory / f'{name}-placement.json'
        saved.write_text(json.dumps(data), encoding='utf8')
        return saved

    def check_fixed_case(self, name):
        source = self.saved_input(name)
        original_bytes = source.read_bytes()
        original_hash = hashlib.sha256(original_bytes).hexdigest()
        original = json.loads(original_bytes)
        output = self.directory / name
        par = load_local('place_and_route', 'place-and-route.py')
        layered = load_local('place_layered', 'place-layered.py')
        with contextlib.redirect_stdout(io.StringIO()), \
                patch.object(par, 'sa', side_effect=AssertionError('Reroute must not run baseline SA')), \
                patch.object(layered, 'sa_layered', side_effect=AssertionError('Reroute must not run layered SA')):
            data = run_flow.reroute_placement(source, out_dir=output)
        self.assertEqual(original['placement'], data['placement'])
        self.assertEqual(original_hash, hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertTrue(data['validation']['valid'])
        self.assertEqual(0, data['metrics']['overlap'])
        self.assertEqual(0, data['metrics']['proximity'])
        self.assertEqual(0, data['metrics']['body_pen'])
        saved, _, par, placement, comps, nets, index = load_placement(output / f'{name}-placement.json')
        pins = {net: {placement.pin_world(index[cid], pin) for cid, pin in members}
                for net, members in nets.items() if not par.power_marker_sym(net)}
        crosses = independent_foreign_checks(saved['routes'], pins)
        self.assertEqual(len(crosses), saved['metrics']['crossings'])
        from netlist2schematic.routing.routing_rules import build_scene, analyze_routes
        scene = build_scene(par, nets, placement, index)
        inspection = analyze_routes(list(saved['routes'].items()), scene)
        self.assertEqual([], inspection['violations'])
        color = ET.parse(output / f'{name}-layered.svg').getroot()
        clean = ET.parse(output / f'{name}-clean.svg').getroot()
        extract = lambda svg: [(item.attrib['data-net'], item.attrib['points'])
                               for item in svg.findall('svg:polyline[@data-net]', NS)]
        self.assertEqual(extract(color), extract(clean))
        self.assertTrue(extract(clean))
        for _, _, x, y in crosses:
            for circle in clean.findall('svg:circle', NS):
                self.assertNotEqual((x, y), (float(circle.attrib['cx']), float(circle.attrib['cy'])),
                                    'Foreign X must not carry a junction dot')
        self.assertFalse(clean.findall('.//*[@data-crossing]'), 'No crossing symbol is requested')
        return data

    def test_original_two_fixed_layout_has_no_foreign_overlap(self):
        self.check_fixed_case('2')

    def test_original_three_fixed_layout_has_no_close_m5_m6_wires(self):
        self.check_fixed_case('3')

    def test_old_valid_flag_does_not_bypass_new_geometry_validation(self):
        output = self.directory / 'old-redraw.svg'
        command = self.command('render-clean.py', self.saved_input('3'), output)
        self.assertNotEqual(0, command.returncode, command.stdout + command.stderr)
        self.assertFalse(output.exists())

    def test_budget_failure_reports_diagnostic_and_preserves_success_files(self):
        from netlist2schematic.routing.orthogonal_router import RoutingFailure
        destination = self.directory / 'existing'
        destination.mkdir()
        originals = {}
        for name in ('2-layered.svg', '2-clean.svg', '2-placement.json'):
            originals[name] = ('keep ' + name).encode()
            (destination / name).write_bytes(originals[name])
        failure = RoutingFailure('Search budget exhausted', {
            'reason': 'max_expansions', 'net': 'f2', 'budget': 1, 'blocking_nets': ['f3']})
        with patch('netlist2schematic.routing.orthogonal_router.route_scene', side_effect=failure):
            with self.assertRaisesRegex(ValueError, 'budget'):
                run_flow.reroute_placement(self.saved_input('2'), out_dir=destination)
        for name, content in originals.items():
            self.assertEqual(content, (destination / name).read_bytes())
        diagnostic = json.loads((destination / 'routing-diagnostics.json').read_text(encoding='utf8'))
        self.assertFalse(diagnostic['valid'])
        self.assertEqual('max_expansions', diagnostic['diagnostics']['reason'])

    def test_cli_requires_exactly_one_input_mode(self):
        neither = self.command('run_flow.py')
        both = self.command('run_flow.py', ROOT / 'examples/2.cir', '--reroute-placement',
                            self.saved_input('2'))
        self.assertNotEqual(0, neither.returncode)
        self.assertNotEqual(0, both.returncode)

    def test_real_search_node_budget_is_enforced(self):
        from netlist2schematic.routing.orthogonal_router import route_scene, RoutingFailure
        from netlist2schematic.routing.routing_rules import build_scene, RoutingOptions
        _, _, par, placement, _, nets, index = load_placement(self.saved_input('2'))
        scene = build_scene(par, nets, placement, index, options=RoutingOptions(max_nodes=1))
        with self.assertRaises(RoutingFailure) as failure:
            route_scene(scene)
        self.assertEqual('graph_node_budget', failure.exception.diagnostics['reason'])
        self.assertGreater(failure.exception.diagnostics['graph_nodes'], 1)

    def test_diagnostic_path_cannot_overwrite_input_placement(self):
        source = self.saved_input('2')
        protected = self.directory / 'routing-diagnostics.json'
        source.rename(protected)
        original = protected.read_bytes()
        with patch('netlist2schematic.routing.orthogonal_router.route_scene', side_effect=AssertionError('Search should not start')):
            with self.assertRaises(ValueError):
                run_flow.reroute_placement(protected, out_dir=self.directory)
        self.assertEqual(original, protected.read_bytes())

    def test_existing_user_diagnostic_file_is_preserved(self):
        source = self.saved_input('2')
        destination = self.directory / 'user-output'
        destination.mkdir()
        protected = destination / 'routing-diagnostics.json'
        original = b'{"notes":"This belongs to the user"}\n'
        protected.write_bytes(original)
        with patch('netlist2schematic.routing.orthogonal_router.route_scene', side_effect=AssertionError('Search should not start')):
            with self.assertRaises(ValueError):
                run_flow.reroute_placement(source, out_dir=destination)
        self.assertEqual(original, protected.read_bytes())

    def test_validation_failure_writes_serializable_diagnostic(self):
        destination = self.directory / 'invalid-candidate'
        # Connected-network geometry is intentionally absent; the validator's
        # internal topology sets must not leak into the JSON error envelope.
        with patch('netlist2schematic.routing.orthogonal_router.route_scene', return_value=([('f2', [])], {})):
            with self.assertRaisesRegex(ValueError, 'failed validation'):
                run_flow.reroute_placement(self.saved_input('2'), out_dir=destination)
        data = json.loads((destination / 'routing-diagnostics.json').read_text(encoding='utf8'))
        self.assertFalse(data['valid'])
        self.assertTrue(data['diagnostics']['structure_errors'])
        self.assertFalse((destination / '2-clean.svg').exists())

    def test_sa_estimator_does_not_call_final_astar(self):
        layered = load_local('place_layered', 'place-layered.py')
        with contextlib.redirect_stdout(io.StringIO()), \
                patch('netlist2schematic.routing.orthogonal_router.route_scene', side_effect=AssertionError('A* entered SA')):
            placement, comps, _, _, _ = layered.create_placement(
                ROOT / 'examples/3.cir', iterations=3, seed=123)
        self.assertEqual(len(comps), len(set(placement.cell_of)))


if __name__ == '__main__':
    unittest.main()
