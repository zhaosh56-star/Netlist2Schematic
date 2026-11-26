"""Comparison summaries must not equate different inputs or budgets."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.evaluate_passive_robustness import summarize,prepare


class PassiveComparisonTests(unittest.TestCase):
    def test_prepare_includes_sources_and_preserves_existing_evidence(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);engine=root/'engine';engine.mkdir()
            (engine/'run_flow.py').write_text('# frozen engine\n')
            inputs=root/'inputs';inputs.mkdir()
            (inputs/'source.cir').write_text('.title source\nI1 signal 0 1u\n.end\n')
            (inputs/'bad.cir').write_text('.title unknown\nX1 a b missing_model\n.end\n')
            output=root/'comparison'
            prepare(output,engine,[inputs])
            rows=json.loads((output/'inventory.json').read_text(encoding='utf8'))
            good=next(r for r in rows if r['included'])
            self.assertEqual(good['passive_count'],0)
            self.assertEqual(good['source_count'],1)
            self.assertEqual(sum(r['status']=='input_error' for r in rows),1)
            self.assertEqual((output/'frozen-engine/run_flow.py').read_bytes(),(engine/'run_flow.py').read_bytes())
            with self.assertRaisesRegex(ValueError,'existing evidence'):
                prepare(output,engine,[inputs])

    def receipt(self,policy,path,length=100):
        return dict(case='a',policy=policy,status='valid',seconds=1,
            signature=dict(parameters=dict(normal_seconds=40),source='frozen'),
            verified=dict(routing_stage='normal',candidate=2,placement=str(path),
                          metrics=dict(wirelength=length,crossings=0,bends=2)))

    def test_same_parameters_required_for_metric_claim(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/'p.json';path.write_text(json.dumps(dict(placement={},routes=[])))
            old=self.receipt('baseline',path);new=self.receipt('improved',path,80)
            lookup={('a','baseline'):old,('a','improved'):new}
            summary,pairs=summarize(lookup)
            self.assertEqual(summary['paired']['metrics']['wirelength']['delta'],-20)
            self.assertTrue(pairs[0]['same_routes'])
            new['signature']['parameters']['normal_seconds']=120
            summary,pairs=summarize(lookup)
            self.assertEqual(summary['paired']['both_valid'],0)
            self.assertFalse(pairs[0]['comparable'])
            new['signature']['parameters']['normal_seconds']=40
            new['signature']['cell']='another_cell'
            self.assertFalse(summarize(lookup)[1][0]['comparable'])

    def test_bus_is_separated_from_complete_non_bus(self):
        one=self.receipt('improved','unused')
        two=deepcopy(one);two['case']='b';two['verified']['routing_stage']='bus_fallback'
        three=dict(case='c',policy='improved',status='failed',seconds=2)
        summary,_=summarize({('a','improved'):one,('b','improved'):two,('c','improved'):three})
        self.assertEqual(summary['improved']['complete_non_bus'],1)
        self.assertEqual(summary['improved']['bus_fallback'],1)
        self.assertEqual(summary['improved']['status'],dict(valid=2,failed=1))

    def test_normal_choices_separate_actual_moves_from_guide_only_changes(self):
        record=self.receipt('improved','unused')
        record['verified']['packing']=dict(normal_choice=dict(eligible_count=2,trials=9,
            elapsed_seconds=.2,applied=True,actions=[dict(position_changed=True),dict(position_changed=False)]))
        summary,_=summarize({('a','improved'):record})
        self.assertEqual(summary['improved']['normal_choices'],dict(eligible=1,attempted=1,
            applied=1,moved_modules=1,trials=9,seconds=.2))


if __name__=='__main__':unittest.main()
