"""Paired pilot reporting separates quality tradeoffs from hard failures."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.evaluate_goal_layouts import paired_summary,publish_goal_overview


def record(case, policy, *, errors=0, distance=0, count=1, length=100, crossings=0, status='valid'):
    return dict(case=case, policy=policy, status=status, verified=dict(
        metrics=dict(wirelength=length, crossings=crossings, bends=2),
        organization=dict(host_access=dict(count=count, errors=errors, backward_distance=distance,
                                          measurable=bool(count)), groups=[dict(kind='parallel_bank')]),
        source_sha256='same_source', plan_signature='same_plan', routing_stage='normal'))


class GoalReportTests(unittest.TestCase):
    def test_improvement_and_regression_are_separate_not_dataset_veto(self):
        rows = [record('one','off',errors=1), record('one','all',length=130,crossings=1),
                record('two','off'), record('two','all',errors=1)]
        result = paired_summary(rows)
        self.assertEqual(result['counts'], {'improved':1, 'regressed':1})
        self.assertEqual(result['eligible_pairs'], 2)
        improved, = [r for r in result['pairs'] if r['category']=='improved']
        self.assertEqual(improved['review_flags'], ['wirelength_over_115_percent','extra_crossings'])
        self.assertEqual(result['by_type']['parallel_bank'], {'improved':1,'regressed':1})
        self.assertEqual(result['publication_decision'], 'not_requested_small_pilot')

    def test_not_applicable_timeout_and_failure_are_not_improvements(self):
        rows = [record('none','off',count=0), record('none','all',count=0),
                record('timeout','off'), record('timeout','all',status='timeout'),
                record('failed','off'), record('failed','all',status='failed')]
        result = paired_summary(rows)
        self.assertEqual(result['eligible_pairs'],0)
        self.assertEqual(result['counts'],{'unpaired':2,'not_applicable':1})
        self.assertIsNone(result['wirelength_ratio_mean'])
        self.assertEqual(next(r for r in result['pairs'] if r['case']=='timeout')['after_status'],'timeout')

    def test_changed_evidence_is_uncertain_and_excluded_from_mean(self):
        old, new = record('one','off'), record('one','all',length=1)
        new['verified']['plan_signature'] = 'changed'
        result = paired_summary([old,new])
        self.assertEqual(result['counts'],{'uncertain':1})
        self.assertIsNone(result['wirelength_ratio_mean'])

    def test_sources_are_counted_in_their_own_relation_type(self):
        old, new = record('one','off'), record('one','all')
        for row in (old,new):
            row['verified']['organization']['sources']=[dict(kind='direct_tail')]
        saved = deepcopy([old,new])
        result = paired_summary([old,new])
        self.assertEqual(result['by_type']['source:direct_tail'],{'unchanged':1})
        self.assertEqual([old,new],saved)

    def test_overview_distinguishes_single_case_report_from_paired_pilots(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);a=root/'iteration1';b=root/'iteration2'
            a.mkdir();b.mkdir()
            (a/'comparison.json').write_text(json.dumps(dict(inventory=[],records=[],parameters={},
                scope='paired_pilot',counts={},pairs=[],dimension_counts={})),encoding='utf8')
            (b/'comparison.json').write_text(json.dumps(dict(report_kind='single_case',pair={})),encoding='utf8')
            single=dict(parameters={'quality_seconds':50},record=dict(case='one',status='valid',
                        elapsed_seconds=20,verified={'metrics':dict(wirelength=100,crossings=0,bends=1)}))
            (b/'single-case-result.json').write_text(json.dumps(single),encoding='utf8')
            publish_goal_overview(root)
            paired=json.loads((root/'latest-evidence.json').read_text('utf8'))
            singles=json.loads((root/'single-run-evidence.json').read_text('utf8'))
            self.assertEqual(len(paired['rounds']),1)
            self.assertEqual(singles['runs'][0]['case'],'one')
            self.assertIn('iteration2/index.html',(root/'index.html').read_text('utf8'))

    def test_unknown_malformed_paired_report_is_not_silently_skipped(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);(root/'iteration1').mkdir()
            (root/'iteration1/comparison.json').write_text('{}',encoding='utf8')
            with self.assertRaises(KeyError):publish_goal_overview(root)

    def test_overview_reads_explicit_extension_authorization_without_changing_run_parameters(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);(root/'iteration1').mkdir()
            (root/'iteration1/comparison.json').write_text(json.dumps(dict(report_kind='single_case')),encoding='utf8')
            for authorized in (False,True):
                budget=dict(initial_seconds=60,max_seconds=120,extension_authorized=authorized)
                (root/'progress.json').write_text(json.dumps(dict(evidence=dict(
                    authorized_complex_validation_budget=budget))),encoding='utf8')
                publish_goal_overview(root)
                html=(root/'index.html').read_text('utf8')
                self.assertIn('先使用60秒',html)
                self.assertEqual('提高到120秒' in html,authorized)
                self.assertEqual(json.loads((root/'progress.json').read_text('utf8'))['evidence'][
                    'authorized_complex_validation_budget'],budget)

    def test_invalid_goal_budget_does_not_silently_fabricate_authorization(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);(root/'iteration1').mkdir()
            (root/'iteration1/comparison.json').write_text(json.dumps(dict(report_kind='single_case')),encoding='utf8')
            (root/'progress.json').write_text(json.dumps(dict(evidence=dict(
                authorized_complex_validation_budget=dict(initial_seconds='120',max_seconds=120,
                    extension_authorized=True)))),encoding='utf8')
            with self.assertRaisesRegex(ValueError,'budget'):publish_goal_overview(root)


if __name__ == '__main__':
    unittest.main()
