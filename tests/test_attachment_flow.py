"""Opt-in integration, common evidence, and original routing-quality guardrails."""
from pathlib import Path
import unittest
from unittest.mock import patch

from netlist2schematic.flow import run_flow
from netlist2schematic.plugins.attachment.attachment_selection import organization_rank, select_attachment_result


def candidate(identifier, *, score=10, embedded=0, violations=0, length=1000,
              crossings=0, signature='same', measurable=True):
    m = {'supported': True, 'measurable': measurable, 'violations': violations,
         'active_order_errors': 0, 'embedded_members': embedded,
         'shape_error': 1, 'pin_axis_error': 0, 'owner_distance': 2}
    report = {'policy': 'passive', 'plan_signature': signature, 'metrics': m}
    metrics = {'wirelength': length, 'crossings': crossings, 'bends': 10}
    return ((score, crossings, length, identifier), None, [], {}, {},
            {'layout_attachments': report}, ([], metrics, {'valid': True}),
            {'score': score}, identifier)


class AttachmentFlowTests(unittest.TestCase):
    def test_new_slot_layout_is_not_overwritten_by_old_feedback(self):
        try:
            from tests.test_aesthetic_flow import CandidateSelectionTests, candidate as seed, route_result
        except ModuleNotFoundError:
            from test_aesthetic_flow import CandidateSelectionTests, candidate as seed, route_result
        rows = [seed(i) for i in (1, 2, 3)]
        for row in rows[1:]:
            row[4]['layout_attachments'] = {'plan': {'supported': True, 'signature': 'slots'},
                'budget': {'seconds_limit': 1, 'max_candidates': 128, 'spent_seconds': .8, 'candidates': 100}}
        seen_budgets = []
        def refine(par, pl, comps, nets, index, metadata, **kwargs):
            seen_budgets.append(dict(kwargs['budget']))
            return pl, metadata
        def measure(pl, comps, index, metadata, plan, **kwargs):
            metadata.setdefault('layout_attachments', {'plan': plan})
            return metadata
        quality = {'score': 10, 'per_net': {'signal': {'detour_factor': 3, 'excess_detour': 900}}}
        with patch('netlist2schematic.plugins.attachment.attachment_integration.refine_attachments', side_effect=refine), \
                patch('netlist2schematic.plugins.attachment.attachment_selection.measure_candidate', side_effect=measure), \
                patch('netlist2schematic.layout.structured_placement.feedback_candidate', side_effect=AssertionError('old feedback overwrote slots')), \
                patch('netlist2schematic.layout.layout_quality.evaluate_quality', return_value=quality):
            _, routed = CandidateSelectionTests().select(rows, [route_result()] * 3, attachment_layout='passive')
        self.assertEqual([1, 2, 3], [call.args[2].identifier for call in routed.call_args_list])
        self.assertEqual(len(seen_budgets), 2)
        self.assertTrue(all(b['spent_seconds'] == .8 and b['candidates'] == 100 for b in seen_budgets))

    def test_modes_and_experimental_conflicts_fail_before_reading_input(self):
        for policy in ('passive', 'all'):
            for name in ('readability_layout', 'structure_layout', 'region_layout', 'symmetry_layout'):
                with self.subTest(policy=policy, name=name), self.assertRaisesRegex(ValueError, 'cannot be combined'):
                    run_flow.run('missing.cir', attachment_layout=policy, **{name: 'auto'})
            for kwargs in ({'mode': 'baseline'}, {'layout_style': 'legacy'}, {'passive_layout': 'fixed'}):
                with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, 'requires'):
                    run_flow.run('missing.cir', attachment_layout=policy, **kwargs)
        with self.assertRaisesRegex(ValueError, 'attachment_layout'):
            run_flow.run('missing.cir', attachment_layout='auto')

    def test_saved_inputs_cannot_request_new_placement(self):
        for flag in ('--reroute-placement', '--simplify-placement'):
            for policy in ('passive', 'all'):
                with self.subTest(flag=flag, policy=policy), patch('sys.stderr'), self.assertRaises(SystemExit) as raised:
                    run_flow.main([flag, 'saved.json', '--attachment-layout', policy])
                self.assertEqual(raised.exception.code, 2)

    def test_better_integration_can_win_without_rewriting_quality(self):
        old = candidate('1')
        new = candidate('2', score=12, embedded=3, length=1100)
        chosen, report = select_attachment_result([old, new])
        self.assertIs(chosen, new)
        self.assertEqual(chosen[7]['score'], 12)
        self.assertEqual(report['reference_candidate'], '1')
        self.assertEqual(report['wirelength_limit'], 1150)

    def test_real_wire_and_crossing_caps_are_independent_of_new_score(self):
        old = candidate('1')
        for new in (candidate('2', score=12, embedded=3, length=1151),
                    candidate('2', score=12, embedded=3, crossings=1)):
            self.assertIs(select_attachment_result([old, new])[0], old)
        old = candidate('1', crossings=2)
        permitted = candidate('2', score=12, embedded=3, crossings=3)
        rejected = candidate('3', score=12, embedded=4, crossings=4)
        self.assertIs(select_attachment_result([old, permitted, rejected])[0], permitted)

    def test_uncovered_or_different_evidence_cannot_win(self):
        old = candidate('1')
        for new in (candidate('2', score=12, embedded=3, signature='other'),
                    candidate('2', score=12, embedded=3, measurable=False)):
            self.assertIs(select_attachment_result([old, new])[0], old)
        self.assertIsNone(organization_rank({}))
        bad = candidate('2', score=12, embedded=3)
        bad[5]['layout_attachments']['metrics']['shape_error'] = float('nan')
        self.assertIsNone(organization_rank(bad[5]['layout_attachments']))

    def test_no_organization_gain_keeps_reference(self):
        old, new = candidate('1'), candidate('2', score=12)
        self.assertIs(select_attachment_result([old, new])[0], old)

    def test_source_only_gain_can_win_without_hiding_missing_source_measurement(self):
        old, new = candidate('1'), candidate('2', score=11)
        for row, distance in ((old, 8), (new, 2)):
            report = row[5]['layout_attachments']
            report['policy'] = 'all'
            report['metrics'].update(source_count=1, source_measurable=True,
                                     source_errors=0, source_distance=distance)
        self.assertIs(select_attachment_result([old, new])[0], new)
        metrics = new[5]['layout_attachments']['metrics']
        for bad in (None, float('nan'), -1):
            with self.subTest(distance=bad):
                metrics['source_distance'] = bad
                self.assertIs(select_attachment_result([old, new])[0], old)
        metrics['source_distance'] = 2
        metrics['source_measurable'] = False
        self.assertIs(select_attachment_result([old, new])[0], old)
        metrics['source_measurable'] = True
        metrics['source_count'] = 0
        chosen, report = select_attachment_result([old, new])
        self.assertIs(chosen, old)
        self.assertEqual(report['candidates'][1]['reason'], 'different_or_missing_source_evidence')

    def test_passive_gain_does_not_hide_source_role_regression(self):
        old, new = candidate('1'), candidate('2', score=11, embedded=4)
        for row, errors in ((old, 0), (new, 1)):
            row[5]['layout_attachments']['metrics'].update(source_count=1,
                source_measurable=True, source_errors=errors, source_distance=2)
        chosen, report = select_attachment_result([old, new])
        self.assertIs(chosen, old)
        self.assertEqual(report['candidates'][1]['reason'], 'source_organization_regression')

    def test_absent_source_relationship_is_neutral_but_not_unknown(self):
        row = candidate('1')[5]['layout_attachments']
        previous = organization_rank(row)
        row['policy'] = 'all'
        self.assertIsNone(organization_rank(row))
        row['metrics'].update(source_count=0, source_measurable=False,
                               source_errors=None, source_distance=None)
        self.assertEqual(organization_rank(row), previous)

    def test_disabled_policy_and_reference_keep_original_random_stream(self):
        from netlist2schematic.layout import structured_placement as placement
        source = Path(__file__).resolve().parents[1] / 'examples/1.cir'
        old = next(placement.create_candidates(source, iterations=30, seed=7))
        for policy in ('off', 'passive', 'all'):
            with patch('netlist2schematic.routing.orthogonal_router.route_scene', side_effect=AssertionError('SA called A*')):
                reference = next(placement.create_candidates(source, iterations=30, seed=7,
                    attachment_layout=policy))
            self.assertEqual(old[0].cell_of, reference[0].cell_of)
            self.assertEqual(old[0].orient, reference[0].orient)
            self.assertEqual(old[0].rng.getstate(), reference[0].rng.getstate())
            self.assertEqual(old[4], reference[4])


if __name__ == '__main__':
    unittest.main()
