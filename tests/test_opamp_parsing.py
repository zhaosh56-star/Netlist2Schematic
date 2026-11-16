"""Named three-signal E opamps, typed X BJTs, and narrow export compatibility."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import warnings

from netlist2schematic.netlist import netlist_core as core
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.common.sa_common import load_local
from netlist2schematic.common.sa_saved import load_placement
from netlist2schematic.flow.run_flow import _saved_positions


class NamedOpampParsingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def source(self, text):
        path = self.root/'circuit.cir'
        path.write_text(text, encoding='utf8')
        return path

    def test_five_node_export_has_three_visible_pins_and_keeps_supplies(self):
        path = self.source('Eamp VDD VSS plus minus output OPAMP gain=100\nR1 output minus 1k\n')
        devices = core.parse_netlist(path)
        amp = devices['Eamp']
        self.assertEqual((amp['ip'], amp['in'], amp['op']), ('plus', 'minus', 'output'))
        self.assertEqual((amp['vdd'], amp['vss']), ('vdd', 'vss'))
        self.assertEqual(amp['parameters'], 'gain=100')
        self.assertNotIn('on', amp)
        self.assertNotIn('value', amp)  # No invented VCVS gain.
        self.assertEqual(set(core.terminal_nets(amp)), {'vdd', 'vss', 'plus', 'minus', 'output'})
        doc = core.to_document(devices)
        self.assertEqual(doc['components'][0]['nodes'], {'OUT':'output', 'IN+':'plus', 'IN-':'minus'})
        self.assertEqual(doc['components'][0]['hidden_nodes'], {'VDD':'vdd', 'VSS':'vss'})
        self.assertFalse({'vdd', 'vss'} & doc['nets'].keys())
        structures = analyze_structures(devices)
        self.assertTrue(any(s['kind']=='opamp_core' for s in structures))
        self.assertTrue(all(level >= 1 for level in compute_levels(devices)['device_levels'].values()))

    def test_signal_only_shorthand_has_no_synthetic_supply(self):
        amp = core.parse_netlist(self.source('E1 plus minus output opamp\n'))['E1']
        self.assertEqual(core.terminal_nets(amp), ('plus', 'minus', 'output'))
        self.assertNotIn('hidden_nodes', core.to_document({'E1':amp})['components'][0])
        self.assertNotIn('vdd', amp)

    def test_legacy_e_proxy_keeps_exact_canonical_fields(self):
        path = self.source('E1 out VSS plus minus 1e6\n')
        amp = core.parse_netlist(path)['E1']
        self.assertEqual(amp, {'cls':'VCVS', 'type':'E', 'op':'out', 'on':'vss',
            'ip':'plus', 'in':'minus', 'value':'1e6', 'source_path':str(path), 'source_line':1})
        # A signal called opamp is not itself the model marker.
        amp = core.parse_netlist(self.source('E1 out 0 plus opamp 100\n'))['E1']
        self.assertEqual(amp['cls'], 'VCVS')
        self.assertEqual(amp['in'], 'opamp')
        # An ordinary E gain may also be a parameter named opamp.
        amp = core.parse_netlist(self.source('E1 out 0 plus minus opamp\n'))['E1']
        self.assertEqual(amp['cls'], 'VCVS')
        self.assertEqual(amp['value'], 'opamp')

    def test_bad_named_arities_and_legacy_nonlinear_proxies_still_fail(self):
        statements = ['E1 a b opamp', 'E1 a b c d opamp', 'E1 a b c d e f opamp',
                      'E1 out ref plus minus 100', 'E1 out 0 POLY(2) a b c d 1']
        for statement in statements:
            with self.subTest(statement=statement), self.assertRaises(core.NetlistError):
                core.parse_netlist(self.source(statement))

    def test_typed_x_bjt_preserves_ids_model_substrate_and_location(self):
        path = self.source('Xarbitrary collector base emitter PNP m=1\n'
                           'Xother c b e substrate NPN m=2\n.model custom PNP\n'
                           'Xthird c3 b3 e3 custom params: m=1\n')
        devices = core.parse_netlist(path)
        self.assertEqual(set(devices), {'Xarbitrary','Xother','Xthird'})
        self.assertEqual([d['type'] for d in devices.values()], ['PNP','NPN','PNP'])
        self.assertEqual(devices['Xother']['substrate'], 'substrate')
        self.assertEqual(devices['Xarbitrary']['source_line'], 1)
        self.assertEqual(devices['Xarbitrary']['model'], 'PNP')
        self.assertEqual(len(core.to_document(devices)['components']), 3)

    def test_unknown_or_malformed_x_is_not_guessed_from_instance_name(self):
        for statement in ['XQlooks_like_bjt c b e unknown m=1', 'X1 a b PNP m=1',
                          'X1 a b c d e NPN m=1', 'X1 a b c d opamp']:
            with self.subTest(statement=statement), self.assertRaises(core.NetlistError):
                core.parse_netlist(self.source(statement))

    def test_terminal_unnamed_flat_ends_emits_warning_without_changing_source(self):
        path = self.source('E1 VDD VSS plus minus out opamp\n.ends\n.end\n')
        original = path.read_bytes()
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter('always')
            devices = core.parse_netlist(path)
        self.assertEqual(set(devices), {'E1'})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(recorded), 1)
        self.assertIs(recorded[0].category, core.NetlistCompatibilityWarning)
        self.assertIn('circuit.cir:2', str(recorded[0].message))

    def test_named_repeated_nested_and_include_terminators_are_not_ignored(self):
        statements = ['R1 a b 1k\n.ends Main\n', 'R1 a b 1k\n.ends\nR2 b c 1k\n',
                      'R1 a b 1k\n.ends\n.ends\n',
                      '.subckt a p n\nR1 p n 1k\n.ends a\n.ends\n']
        for statement in statements:
            with self.subTest(statement=statement), self.assertRaises(core.NetlistError):
                core.parse_netlist(self.source(statement))
        (self.root/'broken.inc').write_text('R1 a b 1k\n.ends\n', encoding='utf8')
        with self.assertRaises(core.NetlistError):
            core.parse_netlist(self.source('.include broken.inc\n'))

    def test_saved_placement_preserves_hidden_supply_contract_and_rejects_tampering(self):
        source = self.source('E1 VDD VSS plus minus out opamp\nR1 out minus 1k\n')
        document = core.to_document(core.parse_netlist(source))
        par = load_local('place_and_route', 'place-and-route.py')
        import random
        placement = par.Placement(document['components'], random.Random(7))
        saved = {'source_netlist':str(source), 'placement':_saved_positions(placement, document['components'])}
        self.assertEqual(saved['placement']['E1']['hidden_nodes'], {'VDD':'vdd', 'VSS':'vss'})
        path = self.root/'circuit-placement.json'
        path.write_text(json.dumps(saved), encoding='utf8')
        restored = load_placement(path)
        self.assertEqual(restored[4][0]['hidden_nodes'], saved['placement']['E1']['hidden_nodes'])
        saved['placement']['E1']['hidden_nodes']['VDD'] = 'wrong'
        path.write_text(json.dumps(saved), encoding='utf8')
        with self.assertRaisesRegex(ValueError, 'hidden supply'):
            load_placement(path)

    def test_saved_signal_only_opamp_cannot_acquire_hidden_supply(self):
        source = self.source('E1 plus minus out opamp\n')
        saved = {'source_netlist':str(source),
                 'placement':{'E1':{'row':0, 'col':0, 'orient':0,
                                    'hidden_nodes':{'VDD':'invented', 'VSS':'0'}}}}
        path = self.root/'circuit-placement.json'
        path.write_text(json.dumps(saved), encoding='utf8')
        with self.assertRaisesRegex(ValueError, 'hidden supply'):
            load_placement(path)

    def test_three_signal_flow_and_independent_redraw(self):
        root = Path(__file__).resolve().parents[1]
        source = self.source('E1 VDD VSS plus minus out opamp\nR1 out minus 1k\n')
        output = self.root/'output'
        command = [sys.executable, '-X', 'utf8', '-B', str(root/'run_flow.py'), str(source),
                   '--iterations', '0', '--candidate-id', '1', '--quality-seconds', '5',
                   '--fallback', 'off', '--local-rescue', 'off', '--simplify-routes', 'off',
                   '--out-dir', str(output)]
        result = subprocess.run(command, cwd=self.root, capture_output=True,
                                text=True, encoding='utf8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        saved = output/'circuit-placement.json'
        data = json.loads(saved.read_text(encoding='utf8'))
        self.assertTrue(data['validation']['valid'])
        self.assertEqual(data['placement']['E1']['hidden_nodes'], {'VDD':'vdd', 'VSS':'vss'})
        restored = load_placement(saved)
        amplifier = next(comp for comp in restored[4] if comp['id'] == 'E1')
        self.assertEqual(set(amplifier['nodes']), {'IN+', 'IN-', 'OUT'})
        redraw = self.root/'redrawn.svg'
        result = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(root/'render-clean.py'),
                                 str(saved), str(redraw)], cwd=self.root, capture_output=True,
                                text=True, encoding='utf8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual(redraw.read_bytes(), (output/'circuit-clean.svg').read_bytes())


if __name__ == '__main__':
    unittest.main()
