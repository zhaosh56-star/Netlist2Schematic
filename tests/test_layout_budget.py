from copy import deepcopy
import json
from pathlib import Path
import tempfile
from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from netlist2schematic.common.layout_budget import LayoutBudget, PackingBudget, layout_budget
from netlist2schematic.plugins.passive_skeleton import packing_budget, placement
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.routing_rules import RoutingOptions
from tests.test_passive_modules import fixture
from tests.test_series_alignment import PAR


class Clock:
    def __init__(self): self.now=100.
    def __call__(self): return self.now


def plan(sizes):
    count=0;groups=[]
    for size in sizes:
        groups.append(dict(members=[f'd{n}' for n in range(count,count+size)]));count+=size
    return dict(groups=groups)


def result(reason='no_legal_slot', trials=2):
    return dict(stop_reason=reason,trials=trials,applied=reason=='packed',elapsed_seconds=.1)


class LayoutBudgetTests(unittest.TestCase):
    def test_limits_derive_from_modules_and_unique_members(self):
        for sizes,k,seconds,trials,recovery in [([1,1],2,3,512,10),
              ([1,1,2,2],5,6,1536,15),([1,2,1,1,1,1,1],8,10,3072,20)]:
            with self.subTest(sizes=sizes):
                limits=LayoutBudget('adaptive').for_plan(plan(sizes))
                self.assertEqual((limits['complexity'],limits['normal_seconds'],limits['normal_trials'],
                                  limits['recovery_seconds']),(k,seconds,trials,recovery))
                json.dumps(limits)

    def test_no_groups_have_no_extra_work_and_legacy_is_default(self):
        self.assertFalse(layout_budget().adaptive)
        self.assertEqual(layout_budget('adaptive').for_plan(plan([]))['normal_trials'],0)
        with self.assertRaises(ValueError):LayoutBudget('unknown')

    def test_phase_is_clipped_to_parent_deadline(self):
        clock=Clock();ledger=PackingBudget(10,100,103,clock=clock)
        self.assertEqual(ledger.allocate(),(3,100))
        clock.now=103
        self.assertEqual(ledger.stop_reason(),'parent_deadline')
        self.assertEqual(ledger.allocate(),(0,100))

    def test_time_and_counts_are_shared_not_reset_per_attempt(self):
        clock=Clock();ledger=PackingBudget(10,100,200,clock=clock)
        self.assertEqual(ledger.allocate(2),(5,50))
        clock.now+=2;ledger.charge(dict(trials=30))
        self.assertEqual(ledger.allocate(),(8,70))
        ledger.charge(dict(trials=70))
        self.assertEqual(ledger.stop_reason(),'trial_budget')
        self.assertFalse(ledger.snapshot('trial_budget')['budget_reset'])

    def test_count_overrun_and_invalid_limits_are_rejected(self):
        with self.assertRaises(ValueError):PackingBudget(1,-1)
        with self.assertRaises(ValueError):PackingBudget(float('nan'),1)
        with self.assertRaises(ValueError):PackingBudget(1,1).charge(dict(trials=2))


class PackingSupplementTests(unittest.TestCase):
    def setUp(self):
        _,self.core,self.comps,self.nets,self.index,self.plan=fixture()
        self.options=RoutingOptions()
        self.limits=LayoutBudget('adaptive').for_plan(self.plan)

    def supplement(self, previous, **kwargs):
        return packing_budget.supplement(PAR,self.core,self.comps,self.nets,self.index,
            self.plan,self.options,previous,self.limits,**kwargs)

    def test_zero_leftover_from_active_refinement_still_gets_its_own_trials(self):
        with patch.object(placement,'pack',return_value=(self.core,result('packed'))) as pack:
            got,report=self.supplement(result('budget_exhausted',0),deadline=perf_counter()+10)
        self.assertIs(got,self.core)
        self.assertGreater(pack.call_args.kwargs['max_trials'],128)
        self.assertFalse(pack.call_args.kwargs['refine_terminal_placements'])
        self.assertEqual(report['normal_packing_budget']['stop_reason'],'completed')

    def test_exhausted_position_generator_does_not_repeat_identical_positions(self):
        with patch.object(placement,'pack',side_effect=AssertionError('must not retry')):
            got,report=self.supplement(result())
        self.assertIsNone(got)
        self.assertFalse(report['normal_packing_budget']['attempted'])
        self.assertEqual(report['normal_packing_budget']['stop_reason'],'search_exhausted')

    def test_expired_parent_does_not_receive_a_fresh_phase(self):
        with patch.object(placement,'pack',side_effect=AssertionError('parent expired')):
            got,report=self.supplement(result('local_budget'),deadline=perf_counter()-1)
        self.assertIsNone(got)
        self.assertEqual(report['normal_packing_budget']['stop_reason'],'parent_deadline')

    def test_real_supplement_preserves_core_and_original_connections(self):
        from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
        original=deepcopy((self.core.cell_of,self.core.orient,self.nets))
        got,report=self.supplement(result('budget_exhausted',0),deadline=perf_counter()+5)
        self.assertIsNotNone(got,report['stop_reason'])
        faults=_static_faults(PAR,got,self.nets,self.index,self.options,perf_counter()+2)
        self.assertEqual(faults['count'],0)
        self.assertEqual((self.core.cell_of,self.core.orient,self.nets),original)
        self.assertEqual(report['terminal_search']['stop_reason'],'necessary_packing_only')

    def test_scale_retries_share_both_ledgers_and_keep_the_core(self):
        clock=Clock();original=deepcopy((self.core.cell_of,self.core.orient,self.core.column_x))
        limits=dict(self.limits,recovery_seconds=10,recovery_trials=100)
        calls=[]
        def ledger(*args,**kwargs):return PackingBudget(*args,clock=clock,**kwargs)
        def pack(*args,**kwargs):
            calls.append(kwargs)
            clock.now+=2
            return None,result('no_legal_slot',10)
        with patch.object(packing_budget,'PackingBudget',side_effect=ledger),patch.object(placement,'pack',side_effect=pack):
            with self.assertRaises(RoutingFailure) as caught:
                packing_budget.resume(PAR,self.core,self.comps,self.nets,self.index,self.plan,
                    self.options,result(),limits,deadline=110)
        self.assertEqual(len(calls),2)
        self.assertEqual((calls[0]['max_trials'],calls[1]['max_trials']),(50,90))
        self.assertEqual((calls[0]['seconds'],calls[1]['seconds']),(5,8))
        self.assertEqual(caught.exception.diagnostics['layout_budget']['stop_reason'],'search_exhausted')
        self.assertEqual((self.core.cell_of,self.core.orient,self.core.column_x),original)

    def test_resume_respects_fixed_coordinates(self):
        fixed=next(iter(range(len(self.core.comps))))
        self.core.fixed={fixed};self.core.fixed_cells={fixed:self.core.cell_of[fixed]}
        with patch.object(placement,'pack',return_value=(None,result())) as pack:
            with self.assertRaises(RoutingFailure):
                packing_budget.resume(PAR,self.core,self.comps,self.nets,self.index,self.plan,
                    self.options,result(),self.limits,deadline=perf_counter()+3)
        # An expanded frame is only permitted if that particular fixed point
        # stays unchanged. Regardless, the real input pose is immutable.
        self.assertEqual(self.core.cell_of[fixed],self.core.fixed_cells[fixed])
        self.assertLessEqual(pack.call_count,2)


class LayoutBudgetIntegrationTests(unittest.TestCase):
    def test_real_candidate_can_insert_after_active_refinement_uses_all_trials(self):
        from tests.test_passive_skeleton import PassiveSkeletonFlowTests
        from netlist2schematic.layout.structured_placement import create_candidates
        from netlist2schematic.plugins.columns import column_layout
        original=column_layout.refine_columns
        def exhaust(*args,**kwargs):
            core,metadata=original(*args,**kwargs)
            metadata['layout_columns']['trials']=128
            return core,metadata
        with tempfile.TemporaryDirectory() as directory:
            path=PassiveSkeletonFlowTests().netlist(directory)
            with patch.object(column_layout,'refine_columns',side_effect=exhaust), \
                 patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('no A* in packing')):
                candidate=next(create_candidates(path,iterations=0,passive_skeleton='auto',
                    candidate_ids=(2,),layout_budget='adaptive'))
        self.assertIsNotNone(candidate[0])
        packing=candidate[4]['passive_skeleton']
        self.assertEqual(packing['shared_local_budget']['active_trials'],128)
        self.assertEqual(packing['shared_local_budget']['packing_trials'],0)
        self.assertTrue(packing['normal_packing_budget']['attempted'])
        self.assertGreater(packing['normal_packing_budget']['trials'],0)

    def test_fast_success_is_identical_and_does_not_request_supplement(self):
        from netlist2schematic.layout.structured_placement import create_candidates
        from tests.test_passive_skeleton import PassiveSkeletonFlowTests
        with tempfile.TemporaryDirectory() as directory:
            path=PassiveSkeletonFlowTests().netlist(directory)
            before=next(create_candidates(path,iterations=0,passive_skeleton='auto',candidate_ids=(2,)))
            with patch.object(packing_budget,'supplement',side_effect=AssertionError('fast success must stay fast')):
                after=next(create_candidates(path,iterations=0,passive_skeleton='auto',candidate_ids=(2,),layout_budget='adaptive'))
        self.assertIsNotNone(before[0]);self.assertIsNotNone(after[0])
        for attribute in ('cell_of','orient','column_x','row_y','passive_local_offsets','passive_module_guides'):
            self.assertEqual(getattr(before[0],attribute),getattr(after[0],attribute))
        self.assertEqual(before[2],after[2])
        self.assertFalse(after[4]['layout_budget']['normal']['attempted'])

    def test_optional_policy_reaches_normal_engine_api(self):
        from netlist2schematic.flow.run_flow import _normal_result
        with patch('netlist2schematic.flow.aesthetic_flow.select_layout',return_value='sentinel') as call:
            self.assertEqual(_normal_result('a',None,'layered','structured',None,0,17,83,'2',None,
                layout_budget='adaptive'),'sentinel')
        self.assertEqual(call.call_args.kwargs['layout_budget'],'adaptive')

    def test_legacy_policy_does_not_change_the_old_call_shape(self):
        from netlist2schematic.flow.run_flow import _normal_result
        with patch('netlist2schematic.flow.aesthetic_flow.select_layout',return_value='sentinel') as call:
            _normal_result('a',None,'layered','structured',None,0,17,83,'2',None)
        self.assertNotIn('layout_budget',call.call_args.kwargs)

    def test_primary_resume_gets_adaptive_share_instead_of_six_second_cap(self):
        from netlist2schematic.recovery import primary_recovery
        clock=Clock();seen=[]
        def resume(deadline):
            seen.append(deadline)
            raise RoutingFailure('no slot',dict(reason='primary_packing_incomplete',layout_budget=dict(stop_reason='search_exhausted')))
        row=dict(candidate_id='2',kind='packing',context=None,checkpoint=None,
                 options=RoutingOptions(),resume=resume,packing_budget=dict(recovery_seconds=20))
        with patch.object(primary_recovery,'perf_counter',side_effect=clock):
            _,report,_=primary_recovery.repair([row],deadline=150,layout_budget='adaptive')
        self.assertEqual(seen,[120])
        self.assertEqual(report['attempts'][0]['layout_budget']['stop_reason'],'search_exhausted')


if __name__=='__main__':unittest.main()
