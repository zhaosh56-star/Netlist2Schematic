"""Local guest coordinates survive save/redraw and axis-only recovery."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from netlist2schematic.common.passive_coordinates import validate_passive_offsets
from netlist2schematic.common.sa_saved import load_placement
from netlist2schematic.flow.run_flow import _saved_positions


class SavedPassiveLocalCoordinates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder = Path(self.tmp.name)
        self.source = folder/'simple.cir'
        self.source.write_text('R1 a 0 1k\nR2 a b 2k\n', encoding='utf8')
        self.path = folder/'simple-placement.json'
        self.data = dict(source_netlist=str(self.source), placement={
            'R1': dict(row=0, col=0, orient=0), 'R2': dict(row=1, col=1, orient=1)},
            layout_grid=dict(column_x=[100,200,300], row_y=[100,200]))

    def load(self):
        self.path.write_text(json.dumps(self.data), encoding='utf8')
        return load_placement(self.path)

    def test_legacy_coordinates_and_serialization_are_unchanged(self):
        _, _, _, pl, comps, _, _ = self.load()
        self.assertFalse(hasattr(pl, 'passive_local_offsets'))
        self.assertEqual(self.data['placement'], _saved_positions(pl, comps))

    def test_large_local_xy_is_not_forced_to_a_grid_center(self):
        self.data['placement']['R1']['local_offset'] = [130, -75]
        _, _, _, pl, comps, _, index = self.load()
        self.assertEqual((230,25), pl.pos_of(index['R1']))
        self.assertEqual([130,-75], _saved_positions(pl, comps)['R1']['local_offset'])
        self.assertEqual((0,0), pl.cell_of[index['R1']])
        self.assertEqual((200,200), pl.pos_of(index['R2']))

    def test_hidden_malformed_offsets_are_rejected(self):
        for value in (True, 'xy', [], [1], [1,2,3], [True,0], [float('nan'),0], [0,float('inf')]):
            self.data['placement']['R1']['local_offset'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load()

    def test_active_devices_cannot_use_guest_offset_exemption(self):
        _, _, _, pl, comps, _, _ = self.load()
        comps[0]['symbol'] = 'nmos'
        pl.passive_local_offsets = [(0,20),(0,0)]
        with self.assertRaisesRegex(ValueError, 'only supported for passive'):
            validate_passive_offsets(pl, comps)
        with self.assertRaises(ValueError):
            _saved_positions(pl, comps)

    def test_old_dx_and_local_offset_are_composed_once(self):
        self.data['placement']['R1'].update(dx=20, local_offset=[130,-75])
        _, _, _, pl, comps, _, index = self.load()
        original = pl.pin_world(index['R1'], '1')
        self.assertEqual((250,25), pl.pos_of(index['R1']))
        self.data['placement'] = _saved_positions(pl, comps)
        _, _, _, again, _, _, ai = self.load()
        self.assertEqual(original, again.pin_world(ai['R1'], '1'))

    def test_spacing_enlarges_axes_without_scaling_or_losing_guest_offsets(self):
        from netlist2schematic.recovery import spacing_retry
        self.data['placement']['R1']['local_offset'] = [130,-75]
        self.data['placement']['R2']['local_offset'] = [-70,-45]
        _, _, _, pl, comps, nets, index = self.load()
        before = deepcopy((pl.cell_of, pl.orient, pl.passive_local_offsets, pl.column_x, pl.row_y))
        def route(par, ns, trial, idx, **kwargs):
            self.assertEqual(pl.passive_local_offsets, trial.passive_local_offsets)
            self.assertEqual((230,25), trial.pos_of(idx['R1']))
            self.assertEqual((180,205), trial.pos_of(idx['R2']))
            return [], dict(wirelength=0,crossings=0,bends=0), dict(valid=True)
        with patch.object(spacing_retry, 'final_route', side_effect=route), patch.object(
                spacing_retry, 'evaluate_quality', return_value=dict(score=0)):
            spacing_retry.retry_spacing((pl,comps,nets,index,{}),scale=1.5)
        self.assertEqual(before, (pl.cell_of, pl.orient, pl.passive_local_offsets, pl.column_x, pl.row_y))


if __name__ == '__main__':
    unittest.main()
