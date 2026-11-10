"""Public reader and graph regressions using small, self-contained circuits."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import device_leveling as dl
import netlist_core as core
import vf2_match

spec = importlib.util.spec_from_file_location('netlist_reader_test', ROOT / 'parse-netlist.py')
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def source(self, text, name='circuit.spi'):
        path = self.directory / name
        path.write_text(text, encoding='utf-8')
        return path

    def test_every_supported_device_uses_one_component_set(self):
        path = self.source('''.model drainDevice NMOS(level=1)
.model bipolar PNP (bf=80)
Mkeep Drain Gate Source Bulk drainDevice W=2u
Qkeep C B E bipolar AREA=2
R1 IN out 1k
C1 OUT 0 1p
L1 in OUT 2n
D1 out 0 Dunknown
V1 IN 0 DC 1
I1 VDD Drain 10u
E1 ampOUT VSS IN OUT 100
.end
''')
        devices = core.parse_netlist(path)
        document = reader.parse(path)
        self.assertEqual(set(devices), {component['id'] for component in document['components']})
        self.assertEqual(devices, dl.parse_netlist(path))
        self.assertEqual(devices['Mkeep']['type'], 'NMOS')
        self.assertEqual(devices['Mkeep']['d'], 'drain')
        self.assertEqual(devices['Qkeep']['type'], 'PNP')
        self.assertEqual(devices['D1']['cls'], 'PASSIVE')
        symbols = {component['id']: component for component in document['components']}
        self.assertEqual(symbols['D1']['nodes'], {'A': 'out', 'K': '0'})
        self.assertEqual(symbols['E1']['nodes'], {'OUT': 'ampout', 'IN+': 'in', 'IN-': 'out'})
        self.assertIn(('R1', '2'), document['nets']['out'])
        self.assertIn(('C1', '1'), document['nets']['out'])
        self.assertTrue({'ampout', 'vss', 'in', 'out'} <= dl.compute_levels(devices)['net_levels'].keys())

    def test_nested_model_includes_continuations_and_source_locations(self):
        self.source('.include "second.inc"\n', 'models.inc')
        models = self.source('.model b PNP\n.model n NMOS\n', 'second.inc')
        path = self.source('''.include "models.inc"
M1 D G S S n
+ L=1u W=2u ; continuation parameters
Q1 out IN 0 b AREA=2
''')
        devices = core.parse_netlist(path)
        self.assertEqual(devices['M1']['source_line'], 2)
        self.assertEqual(devices['M1']['source_path'], str(path))
        self.assertEqual(devices['Q1']['type'], 'PNP')
        self.assertEqual(devices['M1']['type'], 'NMOS')
        self.assertTrue(models.exists())

    def test_include_cycle_and_missing_file_are_location_errors(self):
        path = self.source('.include "loop.inc"\nR1 in 0 1k\n')
        self.source('.include "circuit.spi"\n', 'loop.inc')
        with self.assertRaisesRegex(core.NetlistError, r'circuit\.spi:1:.*recursive \.include'):
            core.parse_netlist(path)
        path.write_text('.include "missing.inc"\n', encoding='utf-8')
        with self.assertRaisesRegex(core.NetlistError, r'circuit\.spi:1:.*cannot read'):
            core.parse_netlist(path)

    def test_subcircuit_selection_is_explicit_and_case_insensitive(self):
        path = self.source('''.model modelA NMOS
.subckt Stage IN OUT
M1 OUT IN 0 0 modelA
.ends stage
.subckt other X
R2 X 0 1k
.ends other
''')
        with self.assertRaisesRegex(core.NetlistError, '--cell'):
            core.parse_netlist(path)
        self.assertEqual(set(core.parse_netlist(path, cell='STAGE')), {'M1'})
        with self.assertRaisesRegex(core.NetlistError, 'unknown subcircuit'):
            core.parse_netlist(path, cell='missing')

    def test_sky130_mos_mapping_and_local_model_override(self):
        path = self.source('''XM1 D G S S sky130_fd_pr__nfet_01v8 W=2 L=1
XM2 D G VDD VDD sky130_fd_pr__pfet_01v8
.model unusual PMOS
M3 D G VDD VDD unusual
''')
        devices = core.parse_netlist(path)
        self.assertEqual([d['type'] for d in devices.values()], ['NMOS', 'PMOS', 'PMOS'])

    def test_unknown_devices_and_polarities_never_disappear(self):
        cases = [
            ('G1 a 0 in 0 1m', 'unsupported device'),
            ('M1 a in 0 0 mysterious', 'cannot determine MOS polarity'),
            ('Q1 a in 0 mysterious', 'cannot determine BJT polarity'),
            ('X1 a b c d user_cell', 'unsupported X instance'),
        ]
        for text, message in cases:
            with self.subTest(text=text):
                path = self.source('* comment\n' + text + '\n')
                with self.assertRaisesRegex(core.NetlistError, r'circuit\.spi:2:.*' + message):
                    core.parse_netlist(path)

    def test_non_grounded_and_nonlinear_e_proxies_are_rejected(self):
        for statement in ('E1 out ref in 0 100', 'E1 out 0 POLY(2) a b c d 1'):
            with self.subTest(statement=statement):
                path = self.source(statement)
                with self.assertRaises(core.NetlistError):
                    core.parse_netlist(path)

    def test_bjt_optional_substrate_and_area_do_not_shift_model(self):
        path = self.source('''.model bp PNP
.model bn NPN
Q1 c b e bp AREA=2
Q2 c b e substrate bn 2
''')
        devices = core.parse_netlist(path)
        self.assertEqual(devices['Q1']['type'], 'PNP')
        self.assertEqual(devices['Q2']['type'], 'NPN')
        self.assertEqual(devices['Q2']['substrate'], 'substrate')

    def test_duplicate_and_malformed_devices_are_source_errors(self):
        path = self.source('R1 in 0 1k\nr1 out 0 2k\n')
        with self.assertRaisesRegex(core.NetlistError, r':2: duplicate device ID'):
            core.parse_netlist(path)
        path = self.source('M1 in out 0\n')
        with self.assertRaisesRegex(core.NetlistError, r':1: M1: expected'):
            core.parse_netlist(path)

    def test_simulation_control_lines_are_not_mistaken_for_devices(self):
        path = self.source('''.control
run
print v(out)
.endc
R1 in 0 1k
.tran 1n 1u
''')
        self.assertEqual(set(core.parse_netlist(path)), {'R1'})

    def test_resistor_loaded_and_disconnected_active_branches_get_levels(self):
        path = self.source('''VDD vdd 0 1.8
RD vdd out 10k
M1 out in 0 0 NMOS
M2 isolated gin tail tail NMOS
''')
        result = dl.compute_levels(core.parse_netlist(path))
        self.assertEqual(result['device_levels'], {'M1': 1, 'M2': 1})
        self.assertIn('branch-root', result['source_note'])
        self.assertGreaterEqual(result['net_levels']['out'], 0)

    def test_bjt_is_layered_but_not_used_as_a_mos_template(self):
        path = self.source('Q1 out in 0 NPN\nQ2 out2 in2 0 NPN\n')
        devices = core.parse_netlist(path)
        self.assertEqual(dl.compute_levels(devices)['device_levels'], {'Q1': 1, 'Q2': 1})
        self.assertEqual(len(vf2_match.build_graph(devices)), 0)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(vf2_match.analyze(path), [])

    def test_net_and_device_names_have_separate_vf2_namespaces(self):
        path = self.source('m1 m1 g1 tail tail NMOS\nm2 d2 g2 tail tail NMOS\n')
        devices = core.parse_netlist(path)
        graph = vf2_match.build_graph(devices)
        self.assertIn('m1', graph)
        self.assertIn(('net', 'm1'), graph)
        self.assertNotEqual(graph.nodes['m1'], graph.nodes[('net', 'm1')])
        matches = vf2_match.find_matches(graph, vf2_match.DP_N, devices)
        self.assertEqual(matches[0][0], ['m1', 'm2'])

    def test_real_project_inputs_parse_without_node_or_frontend(self):
        corpus = ROOT / 'analog-canvas-main'
        cases = [('netlists/native-rc-filters/circuit.spi', None, 3),
                 ('netlists/native-rlc-filter/circuit.spi', None, 4),
                 ('netlists/native-common-source/circuit.spi', None, 5),
                 ('fixtures/simulation-acceptance/ota-5t.spi', 'ota_5t', 6)]
        for relative, cell, count in cases:
            with self.subTest(relative=relative):
                devices = core.parse_netlist(corpus / relative, cell=cell)
                self.assertEqual(len(devices), count)
                self.assertTrue(all(level >= 1 for level in dl.compute_levels(devices)['device_levels'].values()))

    def test_levels_and_vf2_are_reproducible_across_hash_seeds(self):
        path = self.source('M1 d1 g1 tail tail NMOS\nM2 d2 g2 tail tail NMOS\nM3 d3 g3 tail tail NMOS\n')
        script = '''import contextlib,io,json,sys
import device_leveling as dl, vf2_match
with contextlib.redirect_stdout(io.StringIO()):
    result = vf2_match.analyze(sys.argv[1])
print(json.dumps({'matches': result, 'levels': dl.compute_levels(dl.parse_netlist(sys.argv[1]))['device_levels']}, sort_keys=True))
'''
        outputs = []
        for seed in ('1', '7', '99'):
            environment = dict(os.environ, PYTHONHASHSEED=seed, PYTHONIOENCODING='utf-8')
            outputs.append(subprocess.check_output([sys.executable, '-c', script, str(path)], cwd=ROOT, env=environment, text=True, encoding='utf-8'))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], outputs[2])

    def test_parse_cli_from_another_working_directory_and_error_exit(self):
        path = self.source('.subckt Stage in out\nR1 in out 1k\n.ends Stage\n')
        environment = dict(os.environ, PYTHONIOENCODING='utf-8')
        command = [sys.executable, str(ROOT / 'parse-netlist.py'), str(path), '--cell', 'Stage']
        success = subprocess.run(command, cwd=self.directory, env=environment,
                                 capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(json.loads(success.stdout)['components'][0]['id'], 'R1')
        failure = subprocess.run(command[:-2], cwd=self.directory, env=environment,
                                 capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(failure.returncode, 2)
        self.assertIn('--cell', failure.stderr)

    def test_scc_cycles_and_longest_path_remain_supported(self):
        adjacency = {'vdd': ['a', 'b'], 'a': ['c'], 'b': ['c'], 'c': ['d'], 'd': ['c']}
        components = dl.tarjan_scc(set(adjacency), adjacency)
        self.assertEqual(components['c'], components['d'])
        edges = [(start, end, str(index)) for index, (start, ends) in enumerate(adjacency.items()) for end in ends]
        dag, count = dl.condense(components, edges)
        levels = dl.longest_levels(dag, count, {components['vdd']})
        self.assertEqual(levels[components['c']], 2)


if __name__ == '__main__':
    unittest.main()
