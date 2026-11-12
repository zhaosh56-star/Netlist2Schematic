"""Cross-review regressions for attachment transactions and source-only seeds."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import attachment_integration as integration
from circuit_structures import analyze_structures
from device_leveling import compute_levels
from orthogonal_router import RoutingFailure
from passive_placement import passive_diagnostics
from tests.test_passive_placement import convert, mos, resistor, source


def plan_for(devices, comps, nets, index, pl):
    return integration.build_attachment_plan(devices, comps, nets, index,
        analyze_structures(devices), compute_levels(devices), pl.row_of)


def inserted_row_fixture():
    devices = dict(Mleft=mos('leftnode', 'ga'), Mright=mos('rightnode', 'gb'),
        Rbranch=resistor('leftnode', 'm0'), Cseries=resistor('m0', 'm1', 'C'),
        Lseries=resistor('m1', 'rightnode', 'L'), Cbypass=resistor('leftnode', 'rightnode', 'C'))
    initial, comps, nets, index, _ = convert(devices,
        dict(Mleft=(1, 1), Mright=(1, 6), Rbranch=(0, 0), Cseries=(0, 2), Lseries=(0, 4), Cbypass=(2, 0)))
    plan = plan_for(devices, comps, nets, index, initial)
    installed = integration._install(initial, comps, index, plan)
    bridge = next(group for group in plan['groups'] if group['kind'] == 'multi_path_bridge')
    for spec in integration._slot_specs(installed, bridge, index):
        trial = integration._joint_slot(installed, comps, index, plan, bridge, spec)
        if spec['added_rows'] and trial.valid_state():
            metrics = integration.measure_attachments(trial, comps, index, plan)
            if metrics['embedded_members'] == len(bridge['members']):
                return trial, comps, nets, index, plan, bridge
    raise AssertionError('The fixture needs one real inserted display row')


class AttachmentIntegrationReviewTests(unittest.TestCase):
    def test_initial_estimates_use_the_same_attachment_objective(self):
        devices = dict(a=mos('left', 'ga'), b=mos('right', 'gb'),
                       r=resistor('left', 'middle'), c=resistor('middle', 'right', 'C'))
        original, comps, nets, index, _ = convert(devices,
            dict(a=(1, 1), b=(1, 6), r=(0, 0), c=(0, 7)), blocks=[['a', 'b']])
        plan = plan_for(devices, comps, nets, index, original)
        def estimate(placement):
            return 10 + (integration.score_attachments(placement, comps, index, plan)['weighted_total']
                         if hasattr(placement, 'attachment_plan') else 0)
        expected = 10 + integration.score_attachments(original, comps, index, plan)['weighted_total']
        result, report = integration.initialize_attachments(original, devices, comps, nets,
            index, plan, estimate=estimate)
        self.assertTrue(report['applied'])
        self.assertGreater(expected, 10)
        self.assertEqual(report['estimate_before'], expected)
        self.assertEqual(report['estimate_after'], estimate(result))
        self.assertEqual(report['estimate_basis'], 'same_attachment_objective')

    def test_inserted_slot_measurements_survive_real_json_coordinate_restore(self):
        from attachment_selection import measure_candidate
        from run_flow import _saved_positions
        from sa_saved import load_placement
        trial, comps, _, index, plan, bridge = inserted_row_fixture()
        live = measure_candidate(trial, comps, index, {'candidate_id': 2}, plan)
        self.assertEqual(live['layout_attachments']['metrics']['embedded_members'], len(bridge['members']))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'source.cir'
            source.write_text('Mleft leftnode ga 0 0 NMOS\nMright rightnode gb 0 0 NMOS\n'
                'Rbranch leftnode m0 1\nCseries m0 m1 1\nLseries m1 rightnode 1\n'
                'Cbypass leftnode rightnode 1\n.end\n', encoding='utf8')
            data = dict(live, source_netlist=str(source), placement=_saved_positions(trial, comps),
                layout_grid=dict(column_x=list(trial.column_x), row_y=list(trial.row_y)))
            snapshot = Path(directory)/'source-placement.json'
            snapshot.write_text(json.dumps(data), encoding='utf8')
            saved, _, _, restored, restored_comps, _, restored_index = load_placement(snapshot)
            self.assertFalse(hasattr(restored, 'attachment_junction_rows'))
            before = deepcopy({k: v for k, v in vars(restored).items() if k != 'rng'})
            rng = restored.rng.getstate()
            measured = measure_candidate(restored, restored_comps, restored_index, saved, plan)
            self.assertEqual(measured['layout_attachments']['metrics'], live['layout_attachments']['metrics'])
            self.assertEqual({k: v for k, v in vars(restored).items() if k != 'rng'}, before)
            self.assertEqual(restored.rng.getstate(), rng)

    def test_saved_slot_rows_are_invalidated_when_participating_host_row_changes(self):
        from attachment_selection import measure_candidate
        trial, comps, _, index, plan, bridge = inserted_row_fixture()
        metadata = measure_candidate(trial, comps, index, {'candidate_id': 2}, plan)
        restored = deepcopy(trial)
        del restored.attachment_junction_rows
        cells = list(restored.cell_of)
        host = index['Mleft']
        cells[host] = (0, cells[host][1])
        restored.set_state(cells, restored.orient)
        measured = measure_candidate(restored, comps, index, metadata, plan)
        self.assertEqual(measured['layout_attachments']['metrics']['embedded_members'], 0)
        self.assertFalse(hasattr(restored, 'attachment_junction_rows'))

    def test_common_all_plan_keeps_only_identical_passive_slot_evidence(self):
        from attachment_selection import measure_candidate
        trial, comps, _, index, plan, bridge = inserted_row_fixture()
        metadata = measure_candidate(trial, comps, index, {'candidate_id': 2}, plan)
        restored = deepcopy(trial)
        del restored.attachment_junction_rows
        common = deepcopy(plan)
        common.update(policy='all', signature='common-all-evidence')
        result = measure_candidate(restored, comps, index, metadata, common, policy='all')
        self.assertEqual(result['layout_attachments']['metrics']['embedded_members'], len(bridge['members']))
        altered = next(g for g in common['groups'] if g['id'] == bridge['id'])
        altered['host_ports'] = list(reversed(altered['host_ports']))
        result = measure_candidate(restored, comps, index, metadata, common, policy='all')
        self.assertEqual(result['layout_attachments']['metrics']['embedded_members'], 0)
        self.assertFalse(hasattr(restored, 'attachment_junction_rows'))

    def test_source_only_installed_state_supports_real_passive_diagnostics(self):
        devices = dict(host=mos('out', 'control'), voltage=source('VSRC', 'control'))
        original, comps, nets, index, _ = convert(devices, dict(host=(1, 3), voltage=(3, 0)))
        plan = plan_for(devices, comps, nets, index, original)
        rng = original.rng.getstate()
        installed = integration._install(original, comps, index, plan)
        # Source-only opt-in states still pass through the ordinary candidate
        # diagnostics; empty relations must keep their complete data schema.
        diagnostic = passive_diagnostics(installed)
        self.assertEqual(diagnostic['regularity']['relation_counts'],
                         dict(series=0, parallel=0, feedback=0))
        self.assertEqual(diagnostic['skipped_relations'], {})
        self.assertEqual(installed.rng.getstate(), rng)
        self.assertEqual(installed.cell_of, original.cell_of)

    def test_static_validation_budget_expiry_rolls_back_and_closes_shared_ledger(self):
        devices = dict(a=mos('left', 'ga'), b=mos('right', 'gb'),
                       r=resistor('left', 'middle'), c=resistor('middle', 'right', 'C'))
        original, comps, nets, index, _ = convert(devices,
            dict(a=(1, 1), b=(1, 6), r=(0, 0), c=(0, 7)), blocks=[['a', 'b']])
        plan = plan_for(devices, comps, nets, index, original)
        frozen = deepcopy((original.cell_of, original.orient, original.column_x,
                           original.row_y, original.rng.getstate()))
        budget = integration.new_attachment_budget()
        expired = RoutingFailure('Validation budget expired', {'reason': 'quality_time_budget'})
        clock = [0.0]
        def expire_during_scene_validation(*args, **kwargs):
            clock[0] = 2.0
            raise expired
        # Exercise the real attachment static wrapper. Only the lower scene
        # validation and its clock are controlled, as for a real deadline.
        with patch.object(integration, 'perf_counter', side_effect=lambda: clock[0]), \
             patch('symmetry_layout._static_safe', side_effect=expire_during_scene_validation) as check:
            result, report = integration.initialize_attachments(original, devices, comps, nets,
                index, plan, budget=budget)
        self.assertGreater(check.call_count, 0)
        self.assertIs(result, original)
        self.assertFalse(report['applied'])
        self.assertGreater(budget['candidates'], 0)
        self.assertGreater(budget['spent_seconds'], 0)
        self.assertEqual((original.cell_of, original.orient, original.column_x,
                          original.row_y, original.rng.getstate()), frozen)


if __name__ == '__main__':
    unittest.main()
