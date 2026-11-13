"""Structure-first selection never bypasses route quality or replay policy."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from netlist2schematic.plugins.structure.structure_selection import select_structured_result


def result(identifier, length=100, crossings=1, score=10, interleaving=0, order=0, span=1):
    metrics={'wirelength':length,'crossings':crossings,'bends':2}
    measurement={'interleaving':interleaving,'role_order':order,'normalized_span':span}
    metadata={'structure_layout':{'policy':'auto','metrics':measurement}}
    return ((score,crossings,length,str(identifier)),None,[],{}, {},metadata,
            ([],metrics,{'valid':True}),{'score':score},str(identifier))


class StructureSelectionTests(unittest.TestCase):
    def test_better_blocks_can_win_with_more_wire_within_fifteen_percent(self):
        old=result(1,interleaving=2)
        new=result(2,length=115,score=20)
        winner,report=select_structured_result([old,new])
        self.assertIs(new,winner)
        self.assertEqual('1',report['reference_candidate'])
        self.assertAlmostEqual(115,report['wirelength_limit'])
        self.assertEqual('structure_improved_within_wire_budget',report['reason'])

    def test_longer_than_cap_is_rejected(self):
        old=result(1,interleaving=2)
        winner,_=select_structured_result([old,result(2,length=115.01,score=20)])
        self.assertIs(old,winner)

    def test_zero_crossing_reference_cannot_gain_a_crossing(self):
        old=result(1,crossings=0,interleaving=2)
        winner,_=select_structured_result([old,result(2,crossings=1,score=20)])
        self.assertIs(old,winner)

    def test_nonzero_reference_allows_at_most_one_extra_crossing(self):
        old=result(1,interleaving=2)
        allowed=result(2,crossings=2,score=20)
        forbidden=result(3,crossings=3,score=20,span=.5)
        winner,_=select_structured_result([old,allowed,forbidden])
        self.assertIs(allowed,winner)

    def test_no_structure_gain_retains_original_quality(self):
        old=result(1)
        new=result(2,score=20)
        winner,report=select_structured_result([new,old])
        self.assertIs(old,winner)
        self.assertEqual('original_quality_no_structure_gain',report['reason'])

    def test_reducing_interleaving_cannot_introduce_new_role_conflict(self):
        old=result(1,interleaving=2)
        new=result(2,interleaving=0,order=1,score=20)
        self.assertIs(old,select_structured_result([old,new])[0])

    def test_same_structure_uses_original_quality_and_is_order_independent(self):
        old=result(1,interleaving=3)
        a=result(2,score=20)
        b=result(3,score=15)
        self.assertIs(b,select_structured_result([old,a,b])[0])
        self.assertIs(b,select_structured_result([b,a,old])[0])

    def test_parallel_coherence_is_not_hidden_by_shorter_owner_distance(self):
        old=result(1)
        new=result(2,score=20,length=110)
        old[5]['structure_layout']['metrics'].update(parallel_group_error=2,attachment_score=.1)
        new[5]['structure_layout']['metrics'].update(parallel_group_error=0,attachment_score=.3)
        self.assertIs(new,select_structured_result([old,new])[0])

    def test_parallel_coherence_cannot_bypass_actual_wirelength_cap(self):
        old=result(1)
        new=result(2,score=20,length=116)
        old[5]['structure_layout']['metrics']['parallel_group_error']=2
        self.assertIs(old,select_structured_result([old,new])[0])

    def test_selection_never_mutates_saved_candidates(self):
        rows=[result(1,interleaving=1),result(2,score=20)]
        before=deepcopy(rows)
        select_structured_result(rows)
        self.assertEqual(before,rows)


class StructurePublicInterfaceTests(unittest.TestCase):
    def test_invalid_policy_is_rejected_before_reading_netlist(self):
        from netlist2schematic.flow import run_flow
        with self.assertRaisesRegex(ValueError,'structure_layout'):
            run_flow.run('absent.cir',structure_layout='wrong')

    def test_cli_forwards_opt_in_without_changing_symmetry_default(self):
        from netlist2schematic.flow import run_flow
        with patch('netlist2schematic.flow.run_flow.run') as run:
            run_flow.main(['example.cir','--structure-layout','auto'])
        self.assertEqual('auto',run.call_args.kwargs['structure_layout'])
        self.assertEqual('off',run.call_args.kwargs['symmetry_layout'])

    def test_cli_default_is_off(self):
        from netlist2schematic.flow import run_flow
        with patch('netlist2schematic.flow.run_flow.run') as run:
            run_flow.main(['example.cir'])
        self.assertEqual('off',run.call_args.kwargs['structure_layout'])


if __name__=='__main__':
    unittest.main()
