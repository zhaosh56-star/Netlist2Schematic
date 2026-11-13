"""Route policy propagation and saved replay stay independent of placement."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from netlist2schematic.flow import run_flow


class RouteTradeoffFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'rc.cir'
        self.source.write_text('R1 input out 1k\nC1 out 0 1n\n.end\n', encoding='utf8')
        with redirect_stdout(io.StringIO()):
            self.original = run_flow.run(self.source, iterations=0, candidate_id='1',
                quality_seconds=10, fallback='off', simplify_routes='off',
                out_dir=self.root/'original')
        self.saved = self.root/'original/rc-placement.json'

    def test_balanced_saved_mode_skips_search_and_preserves_devices(self):
        before = self.saved.read_bytes()
        with patch('netlist2schematic.routing.orthogonal_router.route_scene', side_effect=AssertionError('A*')), \
                patch('netlist2schematic.flow.run_flow._normal_result', side_effect=AssertionError('SA')), \
                redirect_stdout(io.StringIO()):
            new = run_flow.simplify_placement(self.saved, out_dir=self.root/'balanced',
                                             route_tradeoff='balanced')
        self.assertTrue(new['validation']['valid'])
        self.assertEqual(new['route_simplification']['tradeoff']['policy'], 'balanced')
        self.assertEqual(new['placement'], self.original['placement'])
        self.assertEqual(new['layout_grid'], self.original['layout_grid'])
        self.assertEqual(self.saved.read_bytes(), before)

    def test_replay_requires_same_tradeoff(self):
        from netlist2schematic.postprocess.simplify_routes import simplify_routes
        with redirect_stdout(io.StringIO()):
            first = run_flow.simplify_placement(self.saved, out_dir=self.root/'first',
                                               route_tradeoff='balanced')
        saved = self.root/'first/rc-placement.json'
        with patch('netlist2schematic.postprocess.simplify_routes.simplify_routes', side_effect=AssertionError('replay')), \
                redirect_stdout(io.StringIO()):
            replay = run_flow.simplify_placement(saved, out_dir=self.root/'replay',
                                                route_tradeoff='balanced')
        self.assertTrue(replay['route_simplification']['verified_saved_replay'])
        with patch('netlist2schematic.postprocess.simplify_routes.simplify_routes', wraps=simplify_routes) as simplify, \
                redirect_stdout(io.StringIO()):
            strict = run_flow.simplify_placement(saved, out_dir=self.root/'strict')
        self.assertEqual(simplify.call_count, 1)
        self.assertEqual(strict['routes'], first['routes'])
        self.assertNotIn('verified_saved_replay', strict['route_simplification'])

    def test_invalid_policy_is_rejected_before_output(self):
        with self.assertRaisesRegex(ValueError, 'route_tradeoff'):
            run_flow.run(self.source, out_dir=self.root/'invalid', route_tradeoff='loose')
        self.assertFalse((self.root/'invalid').exists())
        with self.assertRaisesRegex(ValueError, 'route_tradeoff'):
            run_flow.simplify_placement(self.saved, route_tradeoff='loose')

    def test_cli_balanced_propagates_to_saved_modes(self):
        for flag, function in [('--simplify-placement', 'simplify_placement'),
                               ('--reroute-placement', 'reroute_placement')]:
            with patch.object(run_flow, function) as entry:
                self.assertEqual(run_flow.main([flag, str(self.saved),
                                               '--route-tradeoff', 'balanced']), 0)
            self.assertEqual(entry.call_args.kwargs['route_tradeoff'], 'balanced')


if __name__ == '__main__':
    unittest.main()
