"""Explicit presentation labels never invent electrical ports or route edits."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from netlist_core import NetlistError, parse_netlist, to_document
from presentation import prepare_presentation


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def source(self, text, cell=None):
        path = self.root / 'circuit.cir'
        path.write_text(text, encoding='utf-8')
        doc = to_document(parse_netlist(path, cell=cell))
        return path, doc['components'], doc['nets']

    def test_selected_formals_preserve_names_without_claiming_direction(self):
        path, comps, nets = self.source('''.subckt other NotChosen
Rz NotChosen 0 1k
.ends other
.subckt DUT VIN VOUT VDD Unused params: gain=3
R1 VIN VOUT 1k
R2 VOUT 0 1k
.ends DUT
''', 'dut')
        report = prepare_presentation(path, 'DUT', comps, nets)
        ports = {port['name']: port for port in report['named_ports']}
        self.assertEqual(set(ports), {'VIN', 'VOUT', 'VDD', 'Unused'})
        self.assertTrue(all(port['direction'] == 'generic' for port in ports.values()))
        self.assertFalse(ports['Unused']['connected'])
        self.assertNotIn('unused', report['render_hints']['port_labels'])
        self.assertNotIn('vdd', report['render_hints']['port_labels'])
        self.assertEqual(report['formal_declaration']['cell'], 'DUT')
        json.dumps(report)

    def test_continuations_comments_and_spaced_parameters(self):
        path, comps, nets = self.source('''* no ports here
.subckt dut A
+ B ; comment
+ gain = 4
R1 A B 1k
.ends dut
''', 'dut')
        report = prepare_presentation(path, 'dut', comps, nets)
        self.assertEqual([port['name'] for port in report['named_ports']], ['A', 'B'])

    def test_formals_can_be_defined_in_include(self):
        included = self.root / 'included.cir'
        included.write_text('.subckt DUT IN OUT\nR1 IN OUT 1k\n.ends DUT\n', encoding='utf-8')
        path, comps, nets = self.source('.include "included.cir"\n', 'dut')
        report = prepare_presentation(path, 'dut', comps, nets)
        self.assertEqual([port['name'] for port in report['named_ports']], ['IN', 'OUT'])
        self.assertEqual(report['formal_declaration']['path'], str(included.resolve()))

    def test_flat_circuit_never_promotes_internal_named_nets(self):
        path, comps, nets = self.source('R1 vin vout 1k\nR2 vout 0 1k\n.end\n')
        self.assertEqual(prepare_presentation(path, None, comps, nets)['named_ports'], [])

    def test_explicit_source_instance_name_is_input_evidence(self):
        path, comps, nets = self.source('Vin n001 0 AC 1\nR1 n001 internal 1k\n.end\n')
        report = prepare_presentation(path, None, comps, nets)
        self.assertEqual(len(report['named_ports']), 1)
        port = report['named_ports'][0]
        self.assertEqual((port['net'], port['name'], port['direction']), ('n001', 'Vin', 'input'))
        self.assertEqual(port['evidence']['component'], 'Vin')

    def test_explicit_input_node_of_numbered_voltage_source(self):
        path, comps, nets = self.source('V1 VIN_P 0 1\nR1 VIN_P 0 1k\n.end\n')
        port = prepare_presentation(path, None, comps, nets)['named_ports'][0]
        self.assertEqual((port['name'], port['net']), ('VIN_P', 'vin_p'))

    def test_bias_supply_and_numbered_internal_sources_are_not_guessed(self):
        path, comps, nets = self.source('VDD vdd 0 1.8\nVbias vbias 0 0.6\nV1 n1 0 0.1\n.end\n')
        self.assertEqual(prepare_presentation(path, None, comps, nets)['named_ports'], [])

    def test_floating_voltage_source_is_not_guessed_as_two_ports(self):
        path, comps, nets = self.source('Vin p n 1\nR1 p n 1k\n.end\n')
        report = prepare_presentation(path, None, comps, nets)
        self.assertEqual(report['named_ports'], [])
        self.assertEqual(report['skipped_sources'][0]['reason'], 'no_unique_ground_reference')

    def test_negative_driven_terminal_retains_its_actual_pin(self):
        path, comps, nets = self.source('V1 0 VINN 1\nR1 VINN 0 1k\n.end\n')
        port = prepare_presentation(path, None, comps, nets)['named_ports'][0]
        self.assertEqual(port['evidence']['pin'], '-')
        self.assertEqual(port['net'], 'vinn')

    def test_selected_subcircuit_does_not_add_testbench_source_ports(self):
        path, comps, nets = self.source('Vin hidden 0 1\n.subckt DUT P Q\nR1 P Q 1k\n.ends DUT\n', 'dut')
        report = prepare_presentation(path, 'dut', comps, nets)
        self.assertEqual([port['name'] for port in report['named_ports']], ['P', 'Q'])

    def test_control_role_is_a_hint_not_an_input_direction_assertion(self):
        path, comps, nets = self.source('.subckt DUT Out\n.model N NMOS\nM1 drain Out 0 0 N\n.ends DUT\n', 'dut')
        port = prepare_presentation(path, 'dut', comps, nets)['named_ports'][0]
        self.assertEqual(port['role'], 'control')
        self.assertEqual(port['direction'], 'generic')

    def test_duplicate_formal_spelling_is_deduplicated_by_actual_net(self):
        path, comps, nets = self.source('.subckt DUT In IN\nR1 In 0 1k\n.ends DUT\n', 'dut')
        ports = prepare_presentation(path, 'dut', comps, nets)['named_ports']
        self.assertEqual(len(ports), 1)
        self.assertEqual(ports[0]['aliases'], ['In', 'IN'])

    def test_input_graph_and_source_bytes_are_unchanged(self):
        path, comps, nets = self.source('Vin vin 0 1\nR1 vin 0 1k\n.end\n')
        original, digest = deepcopy((comps, nets)), sha256(path.read_bytes()).hexdigest()
        report = prepare_presentation(path, None, comps, nets)
        self.assertEqual((comps, nets), original)
        self.assertEqual(sha256(path.read_bytes()).hexdigest(), digest)
        self.assertEqual(report['render_hints']['wire_geometry'], 'unchanged')

    def test_unknown_selected_cell_fails_explicitly(self):
        path, comps, nets = self.source('R1 a 0 1k\n.end\n')
        with self.assertRaises(NetlistError):
            prepare_presentation(path, 'missing', comps, nets)


if __name__ == '__main__':
    unittest.main()
