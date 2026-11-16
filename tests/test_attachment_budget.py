"""Soft yields preserve the original time/count ledger and verified states."""
from copy import deepcopy
from time import perf_counter
import unittest
from unittest.mock import patch

from netlist2schematic.plugins.attachment import attachment_budget as phases
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.layout.structured_placement import par
from netlist2schematic.routing.routing_rules import RoutingOptions
from tests.test_attachment_conduction import fixture as conduction_fixture
from tests.test_attachment_tail_transistors import fixture as tail_fixture


class Clock:
    now=1000.
    def __call__(self):return self.now


class StageLedgerTests(unittest.TestCase):
    def test_initialization_leaves_time_and_count_without_resetting_usage(self):
        b=ai.new_attachment_budget();b.update(spent_seconds=.2,candidates=20);clock=Clock()
        with phases.initialization_quota(b,1000.8,clock) as stage:
            self.assertAlmostEqual(stage.deadline,1000.48)
            self.assertEqual(stage.ceiling,101)
            b['candidates']+=3;clock.now+=.1
        self.assertEqual(b['candidates'],23);self.assertEqual(b['spent_seconds'],.2)
        self.assertNotIn('stage_candidate_ceiling',b)
        self.assertEqual(stage.record['candidates_used'],3)
        self.assertFalse(stage.record['shared_budget_reset'])

    def test_nested_conduction_limit_restores_parent_ceiling(self):
        b=ai.new_attachment_budget();clock=Clock()
        with phases.initialization_quota(b,1001.,clock):
            parent=b['stage_candidate_ceiling']
            with phases.conduction_quota(b,1000.6,clock) as stage:
                self.assertEqual(stage.ceiling,16);b['candidates']=16
                with patch.object(ai,'perf_counter',new=clock):self.assertFalse(ai._has_budget(b,1000.6))
            self.assertEqual(stage.record['status'],'candidate_quota')
            self.assertEqual(b['stage_candidate_ceiling'],parent)
            with patch.object(ai,'perf_counter',new=clock):self.assertTrue(ai._has_budget(b,1000.6))
        self.assertNotIn('stage_candidate_ceiling',b)

    def test_soft_timeout_yields_only_the_old_remainder(self):
        b=ai.new_attachment_budget();clock=Clock()
        with phases.conduction_quota(b,1001.,clock) as stage:
            clock.now=1000.251;b['exhausted']=True;b['candidates']=1
        self.assertTrue(stage.time_yield);self.assertFalse(b['exhausted'])
        self.assertEqual(stage.record['status'],'time_quota');self.assertEqual(b['max_candidates'],128)

    def test_shared_deadline_or_final_count_cannot_be_revived(self):
        for mode in ('time','count'):
            b=ai.new_attachment_budget();clock=Clock()
            with phases.conduction_quota(b,1001.,clock) as stage:
                b['exhausted']=True
                if mode=='time':clock.now=1001.001
                else:b['candidates']=128;clock.now=1000.3
            self.assertTrue(b['exhausted']);self.assertFalse(stage.time_yield)
            self.assertEqual(stage.record['status'],'shared_budget')

    def test_unknown_early_checker_failure_is_not_mistaken_for_soft_timeout(self):
        b=ai.new_attachment_budget();clock=Clock()
        with phases.conduction_quota(b,1001.,clock) as stage:
            b['exhausted']=True;clock.now+=.01
        self.assertTrue(b['exhausted']);self.assertFalse(stage.time_yield)

    def test_previously_exhausted_ledger_never_reopens(self):
        b=ai.new_attachment_budget();b['exhausted']=True;clock=Clock()
        with phases.conduction_quota(b,1001.,clock):clock.now+=.3
        self.assertTrue(b['exhausted'])

    def test_exception_restores_inherited_limits_without_swallowing_failure(self):
        b=ai.new_attachment_budget();b['stage_candidate_ceiling']=17;clock=Clock()
        with self.assertRaisesRegex(RuntimeError,'oracle'):
            with phases.conduction_quota(b,1001.,clock) as stage:raise RuntimeError('oracle')
        self.assertEqual(b['stage_candidate_ceiling'],17);self.assertEqual(stage.record['status'],'error')

    def test_closing_a_prefix_before_suffix_is_idempotent(self):
        b=ai.new_attachment_budget();clock=Clock();stage=phases.before_tail_quota(b,1001.,clock)
        with stage:
            clock.now+=.651;b['exhausted']=True;stage.__exit__(None,None,None)
            record=deepcopy(stage.record);self.assertFalse(b['exhausted'])
            clock.now+=.1;b['candidates']+=1
        self.assertEqual(stage.record,record);self.assertNotIn('stage_candidate_ceiling',b)

    def test_custom_small_allowance_and_outer_deadline_do_not_add_time(self):
        b=ai.new_attachment_budget(seconds=.1,max_candidates=1);clock=Clock()
        with phases.initialization_quota(b,1000.04,clock) as stage:
            self.assertAlmostEqual(stage.deadline,1000.024);self.assertEqual(stage.allowance,0)
        self.assertEqual(stage.record['status'],'candidate_quota')

    def test_expired_outer_deadline_is_not_replaced_by_current_time(self):
        b=ai.new_attachment_budget();clock=Clock()
        with phases.initialization_quota(b,0.,clock) as stage:self.assertEqual(stage.deadline,0.)
        self.assertEqual(stage.record['status'],'shared_budget')


class JointStageTests(unittest.TestCase):
    def stage(self,*,mode='state',timeout=None,start=0):
        _,pl,c,n,i,plan=conduction_fixture();trial=deepcopy(pl)
        if mode=='state':
            cells=list(trial.cell_of);cells[1]=cells[0];trial.set_state(cells,trial.orient)
        trial._test_proposal=True;clock=Clock();b=ai.new_attachment_budget();b['candidates']=start
        report=dict(candidate_trials=0,candidates=0,actions=[],accepted=0,hpwl_rejections=0)
        original=deepcopy((pl.cell_of,pl.orient,pl.row_y,pl.column_x))
        def proposals(*args,**kwargs):
            for _ in range(100):yield deepcopy(trial),dict(columns_added=0)
        def static(*args,**kwargs):
            if timeout:
                clock.now=1001.001 if timeout=='shared' else args[-1]+.001
                return True if timeout=='soft_true' else None
            return False
        def measure(value,*args):return (0,) if getattr(value,'_test_proposal',False) else (1,)
        with patch.object(ai,'perf_counter',new=clock), \
             patch('netlist2schematic.plugins.attachment.attachment_conduction.conduction_trials',side_effect=proposals), \
             patch.object(ai,'measure_attachments',side_effect=measure), \
             patch.object(ai,'attachment_rank',side_effect=lambda v:v), \
             patch.object(ai,'_fixed_world_safe',return_value=mode!='fixed'), \
             patch.object(ai,'_checked_static',side_effect=static) as validate, \
             patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar forbidden')):
            result,expired=ai._joint_topology_stage(par,pl,pl,c,n,i,plan,RoutingOptions(),b,1001.,report,
                initialization=True,family='conduction')
        self.assertEqual((pl.cell_of,pl.orient,pl.row_y,pl.column_x),original)
        self.assertIs(result,pl)
        return b,report,expired,validate.call_count

    def test_many_bad_joint_candidates_leave_opportunity_for_other_families(self):
        b,r,expired,calls=self.stage(start=5)
        self.assertFalse(expired);self.assertEqual(b['candidates'],21);self.assertFalse(b.get('exhausted'))
        self.assertEqual(r['joint_stage_budgets'][0]['status'],'candidate_quota');self.assertEqual(calls,0)
        facts=r['joint_stage_diagnostics']['conduction']
        self.assertEqual(facts['considered'],16);self.assertEqual(facts['state_rejected'],16)
        self.assertEqual(facts['state_reasons'],dict(slot_collision=16))

    def test_fixed_rejection_is_separate_from_state_and_geometry(self):
        _,r,_,calls=self.stage(mode='fixed');facts=r['joint_stage_diagnostics']['conduction']
        self.assertEqual(facts['fixed_rejected'],facts['considered']);self.assertEqual(calls,0)
        self.assertEqual(facts['state_rejected'],0)

    def test_static_rejection_is_counted_without_becoming_a_commit(self):
        _,r,expired,calls=self.stage(mode='static');facts=r['joint_stage_diagnostics']['conduction']
        self.assertGreater(calls,0);self.assertEqual(facts['static_rejected'],calls)
        self.assertEqual(facts['accepted'],0);self.assertEqual(r['actions'],[]);self.assertFalse(expired)

    def test_soft_validation_timeout_discards_trial_but_not_later_stage_budget(self):
        b,r,expired,calls=self.stage(mode='static',timeout='soft')
        self.assertFalse(expired);self.assertFalse(b['exhausted']);self.assertEqual(calls,1)
        self.assertEqual(r['actions'],[]);self.assertEqual(r['joint_stage_diagnostics']['conduction']['validation_timeout'],1)

    def test_real_shared_validation_timeout_remains_exhausted(self):
        b,r,expired,calls=self.stage(mode='static',timeout='shared')
        self.assertTrue(expired);self.assertTrue(b['exhausted']);self.assertEqual(calls,1)
        self.assertEqual(r['actions'],[])

    def test_validator_returning_true_after_soft_deadline_cannot_commit(self):
        b,r,expired,calls=self.stage(mode='static',timeout='soft_true')
        self.assertFalse(expired);self.assertEqual(calls,1);self.assertFalse(b['exhausted'])
        self.assertEqual(r['actions'],[]);self.assertEqual(r['joint_stage_diagnostics']['conduction']['accepted'],0)


class LifecycleTests(unittest.TestCase):
    def test_common_static_boundary_rejects_a_late_true_oracle(self):
        clock=Clock()
        def late(*args):clock.now=1000.5;return True
        with patch.object(ai,'perf_counter',new=clock),patch.object(ai,'_static',side_effect=late):
            self.assertIsNone(ai._checked_static(None,None,None,None,None,1000.4))

    def test_initialization_accounts_elapsed_once_and_removes_temporary_cap(self):
        pl,c,n,i,plan=tail_fixture();b=ai.new_attachment_budget();b['spent_seconds']=.2;clock=Clock()
        def run(*args,**kwargs):
            ledger,started,until=kwargs['_budget_window'];self.assertIs(ledger,b)
            clock.now+=.1;ai._end_budget(ledger,started)
            return pl,dict(budget=ledger)
        with patch.object(ai,'perf_counter',new=clock),patch.object(ai,'_initialize_attachments',side_effect=run):
            result,report=ai.initialize_attachments(pl,{},c,n,i,plan,budget=b)
        self.assertAlmostEqual(b['spent_seconds'],.3);self.assertEqual(b['seconds_limit'],1)
        self.assertNotIn('stage_candidate_ceiling',b);self.assertEqual(result.attachment_budget,b)
        self.assertEqual(report['phase_budget']['status'],'completed')

    def test_prefix_timeout_leaves_real_shared_time_for_tail_without_changing_state(self):
        pl,c,n,i,plan=tail_fixture();b=ai.new_attachment_budget();b['spent_seconds']=.25;clock=Clock()
        metadata=dict(candidate_id='2',layout_attachments=dict(policy='all',plan=plan,budget=b))
        seen=[];old=deepcopy((pl.cell_of,pl.orient,pl.row_y,pl.column_x))
        def slow(*args,**kwargs):
            clock.now=args[-2]+.001;b['exhausted']=True
            return args[1]
        def tail(par,current,comps,nets,index,plan,options,budget,until,report,metadata,**kwargs):
            seen.append((until,ai._has_budget(budget,until),budget['spent_seconds']))
            return current
        with patch.object(ai,'perf_counter',new=clock),patch.object(ai,'_parallel_channel_stage',side_effect=slow), \
             patch('netlist2schematic.plugins.attachment.attachment_tail_transistors.finish_tails',side_effect=tail), \
             patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar forbidden')):
            result,meta=ai.refine_attachments(par,pl,c,n,i,metadata,budget=b)
        self.assertEqual(len(seen),1);self.assertTrue(seen[0][1]);self.assertEqual(seen[0][0],1000.75)
        self.assertEqual((result.cell_of,result.orient,result.row_y,result.column_x),old)
        self.assertLessEqual(b['spent_seconds'],1);self.assertGreater(b['spent_seconds'],.25)
        self.assertEqual(meta['layout_attachments']['refinement']['before_reserved_finish_budget']['status'],'time_quota')
        self.assertNotIn('stage_candidate_ceiling',b)

    def test_zero_budget_has_no_trials_or_new_allocation(self):
        pl,c,n,i,plan=tail_fixture();b=ai.new_attachment_budget(seconds=0,max_candidates=0)
        with patch.object(ai,'_checked_static',side_effect=AssertionError('No allowance')):
            result,report=ai.initialize_attachments(pl,{},c,n,i,plan,budget=b)
        self.assertIs(result,pl);self.assertEqual(b['candidates'],0);self.assertEqual(b['seconds_limit'],0)


if __name__=='__main__':unittest.main()
