"""Control-facing choices preserve proven cells and share the old ledger."""
from copy import deepcopy
import json
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_attachment_stages import fixture
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment import attachment_stages as stages
from netlist2schematic.plugins.attachment import attachment_stage_access as access
from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.routing.routing_rules import RoutingOptions


def organized(**kwargs):
    f=list(fixture(**kwargs))
    for trial,_ in stages.stage_trials(f[1],f[2],f[4],f[5]['conduction_stages']):
        if (stages.stage_rank(stages.measure_stages(trial,f[2],f[4],f[5]['conduction_stages']))
                ==(0,0,0,0.,0.) and not any(trial.orient)):
            f[1]=trial;return f
    raise AssertionError('fixture has no coherent all-west access pose')


def improve(f,*,budget=None,until=None,max_trials=8):
    d,pl,cs,nets,index,plan=f
    budget=ai.new_attachment_budget() if budget is None else budget
    report=dict(actions=[],candidate_trials=0,candidates=0,accepted=0)
    result,detail=access.improve_access(structured.par,pl,cs,nets,index,plan,RoutingOptions(),
        budget,perf_counter()+1 if until is None else until,report,
        initialization=True,hpwl_limit=float('inf'),max_trials=max_trials)
    return result,detail,budget,report


def fake_cost(par,pl,*args,**kwargs):
    def peek(current):return dict(cost=100-sum(current.orient),estimated_hpwl=0.,escape_risk=0.)
    def cost(current):return peek(current)['cost']
    cost.peek=peek
    return cost,dict(version='test-cost',applied=True)


class ControlAccessTests(unittest.TestCase):
    def test_actual_existing_estimate_finds_safe_whole_cell_improvement(self):
        f=organized();original=deepcopy((f[1].cell_of,f[1].orient,f[1].blocks,f[1].rng.getstate()))
        with patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('Astar search')):
            result,detail,budget,report=improve(f)
        self.assertTrue(detail['applied']);self.assertLess(detail['after']['cost'],detail['before']['cost'])
        self.assertLess(detail['after']['escape_risk'],detail['before']['escape_risk'])
        self.assertEqual((result.cell_of,result.column_x,result.row_y),(f[1].cell_of,f[1].column_x,f[1].row_y))
        self.assertEqual(stages.stage_rank(stages.measure_stages(result,f[2],f[4],f[5]['conduction_stages'])),(0,0,0,0.,0.))
        self.assertTrue(ai._checked_static(structured.par,result,f[3],f[4],RoutingOptions(),None))
        self.assertEqual(original,(f[1].cell_of,f[1].orient,f[1].blocks,f[1].rng.getstate()))
        self.assertEqual(report['candidate_trials'],detail['trials'])
        self.assertEqual(budget['candidates'],detail['trials']);self.assertLessEqual(detail['trials'],8)
        json.dumps(detail,allow_nan=False)

    def test_proposals_flip_exactly_one_complete_cell_and_preserve_real_axis(self):
        f=organized();model=f[5]['conduction_stages'];cells={frozenset(c['members']) for c in model['stages']}
        for trial,action in access.mirror_trials(f[1],f[2],f[4],model):
            changed={c['id'] for i,c in enumerate(f[2]) if trial.orient[i]!=f[1].orient[i]}
            self.assertIn(frozenset(changed),cells)
            self.assertEqual(trial.cell_of,f[1].cell_of);self.assertEqual(trial.row_of,f[1].row_of)
            self.assertEqual(stages.stage_rank(stages.measure_stages(trial,f[2],f[4],model)),(0,0,0,0.,0.))
            self.assertEqual(trial.comp_at,{cell:i for i,cell in enumerate(trial.cell_of)})

    def test_fixed_member_freezes_its_whole_cell(self):
        f=organized();i=f[4]['ap'];f[1].fixed.add(i)
        for trial,action in access.mirror_trials(f[1],f[2],f[4],f[5]['conduction_stages']):
            self.assertFalse(set(action['members'])&{'ap','ak','an'})
            self.assertEqual(trial.orient[i],f[1].orient[i])

    def test_original_orientation_rules_can_veto_a_whole_cell(self):
        f=organized();old=f[1].allowed_orientations;i=f[4]['ap']
        f[1].allowed_orientations=lambda j:(f[1].orient[j],) if j==i else old(j)
        proposals=list(access.mirror_trials(f[1],f[2],f[4],f[5]['conduction_stages']))
        self.assertTrue(proposals)
        self.assertFalse(any('ap' in action['members'] for _,action in proposals))

    def test_foreign_runtime_owner_is_not_split(self):
        f=organized();f[1].blocks.append([f[4]['ap'],f[4]['bp']])
        self.assertEqual(list(access.mirror_trials(f[1],f[2],f[4],f[5]['conduction_stages'])),[])

    def test_real_leaf_ownership_is_installed_when_former_blocks_are_only_subsets(self):
        f=organized();f[1].blocks=[[i] for i in range(len(f[2]))]
        trial,_=next(access.mirror_trials(f[1],f[2],f[4],f[5]['conduction_stages']))
        measured=stages.measure_stages(trial,f[2],f[4],f[5]['conduction_stages'])
        self.assertEqual(measured['live_protected_cells'],4)

    def test_zero_budget_does_not_build_an_estimator(self):
        f=organized();budget=ai.new_attachment_budget();budget['candidates']=128
        with patch.object(access,'make_initializer_cost',side_effect=AssertionError('budget exhausted')):
            result,detail,budget,_=improve(f,budget=budget)
        self.assertIs(result,f[1]);self.assertEqual(detail['trials'],0)

    def test_one_remaining_trial_is_not_a_new_eight_trial_budget(self):
        f=organized();budget=ai.new_attachment_budget();budget['candidates']=127
        _,detail,budget,_=improve(f,budget=budget)
        self.assertEqual(detail['trials'],1);self.assertEqual(budget['candidates'],128)

    def test_expired_deadline_does_not_build_estimator_or_change_source(self):
        f=organized()
        with patch.object(access,'make_initializer_cost',side_effect=AssertionError('expired')):
            result,detail,_,_=improve(f,until=perf_counter()-1)
        self.assertIs(result,f[1]);self.assertEqual(detail['stop_reason'],'shared_budget')

    def test_invalid_terminal_contract_cannot_gain_from_unknown_measurement(self):
        f=organized();f[2][0]['nodes']['G']='changed'
        with patch.object(access,'make_initializer_cost',side_effect=AssertionError('invalid contract')):
            result,detail,_,_=improve(f)
        self.assertIs(result,f[1]);self.assertEqual(detail['stop_reason'],'unmeasurable_cells')

    def test_nonapplicable_model_is_not_scored_as_an_improvement(self):
        f=organized();f[5]['conduction_stages']['global_layout_eligible']=False
        with patch.object(access,'make_initializer_cost',side_effect=AssertionError('not applicable')):
            result,detail,_,_=improve(f)
        self.assertIs(result,f[1]);self.assertFalse(detail['applied'])

    def test_lower_estimate_still_requires_full_static_check(self):
        f=organized()
        with patch.object(access,'make_initializer_cost',side_effect=fake_cost), \
             patch.object(ai,'_checked_static',return_value=False):
            result,detail,_,_=improve(f)
        self.assertIs(result,f[1]);self.assertFalse(detail['applied']);self.assertGreater(detail['static_rejections'],0)

    def test_only_late_success_is_rolled_back(self):
        f=organized();clock=[perf_counter()];until=clock[0]+1
        def late(*args):clock[0]=until+1;return True
        with patch.object(access,'make_initializer_cost',side_effect=fake_cost), \
             patch.object(ai,'_checked_static',side_effect=late), \
             patch.object(ai,'perf_counter',side_effect=lambda:clock[0]), \
             patch.object(access,'perf_counter',side_effect=lambda:clock[0]):
            result,detail,_,_=improve(f,until=until)
        self.assertIs(result,f[1]);self.assertFalse(detail['applied'])

    def test_later_timeout_preserves_earlier_fully_verified_best(self):
        f=organized();clock=[perf_counter()];until=clock[0]+1;checks=[]
        def check(*args):
            checks.append(True)
            if len(checks)==2:clock[0]=until+1
            return True
        with patch.object(access,'make_initializer_cost',side_effect=fake_cost), \
             patch.object(ai,'_checked_static',side_effect=check), \
             patch.object(ai,'perf_counter',side_effect=lambda:clock[0]), \
             patch.object(access,'perf_counter',side_effect=lambda:clock[0]):
            result,detail,_,_=improve(f,until=until)
        self.assertTrue(detail['applied']);self.assertEqual(detail['accepted'],1)
        self.assertEqual(sum(a!=b for a,b in zip(result.orient,f[1].orient)),3)
        self.assertEqual(detail['stop_reason'],'shared_budget')

    def test_zero_trial_cap_does_not_claim_convergence(self):
        result,detail,_,_=improve(organized(),max_trials=0)
        self.assertFalse(detail['applied']);self.assertEqual(detail['stop_reason'],'shared_budget')

    def test_renaming_and_reordering_preserve_geometric_choice(self):
        a=organized();b=organized(rename=True)
        ra,da,_,_=improve(a);rb,db,_,_=improve(b)
        def pose(pl,cs):
            return sorted((tuple(pl.pos_of(i)),c['symbol'],pl.orient[i]) for i,c in enumerate(cs))
        self.assertEqual(pose(ra,a[2]),pose(rb,b[2]))
        self.assertAlmostEqual(da['after']['cost'],db['after']['cost'])

    def test_translation_preserves_choice_and_estimate(self):
        a=organized();b=deepcopy(a)
        b[1].column_x=[x+258 for x in b[1].column_x];b[1].row_y=[y+146 for y in b[1].row_y]
        # This represents a separately initialized translated circuit. A live
        # axis mutation without translating its frozen contract is invalid.
        self.assertFalse(b[1].valid_state())
        b[1].attachment_axes=(tuple(b[1].column_x),tuple(b[1].row_y))
        self.assertTrue(b[1].valid_state())
        ra,da,_,_=improve(a);rb,db,_,_=improve(b)
        self.assertEqual(ra.orient,rb.orient);self.assertAlmostEqual(da['after']['cost'],db['after']['cost'])

    def test_mirror_preserves_reduction_and_mirrors_result(self):
        a=organized();b=deepcopy(a);pl=b[1]
        pl.set_state([(r,len(pl.column_x)-1-c) for r,c in pl.cell_of],[1-v for v in pl.orient])
        ra,da,_,_=improve(a);rb,db,_,_=improve(b)
        self.assertTrue(da['applied']);self.assertTrue(db['applied'])
        self.assertEqual(rb.orient,[1-v for v in ra.orient])
        self.assertAlmostEqual(da['after']['cost'],db['after']['cost'])

    def test_bjt_control_access_keeps_conduction_axis_and_original_rules(self):
        f=organized(bjt=True);result,detail,_,_=improve(f)
        self.assertTrue(result.valid_state());self.assertLessEqual(detail['trials'],8)
        self.assertEqual(stages.stage_rank(stages.measure_stages(result,f[2],f[4],f[5]['conduction_stages'])),(0,0,0,0.,0.))

    def test_unavailable_estimator_keeps_original_verified_state(self):
        f=organized()
        with patch.object(access,'make_initializer_cost',return_value=(None,dict(reason='quality_time_budget'))):
            result,detail,_,_=improve(f)
        self.assertIs(result,f[1]);self.assertEqual(detail['stop_reason'],'quality_time_budget')

    def test_hpwl_guard_remains_independent_of_lower_access_estimate(self):
        f=organized();budget=ai.new_attachment_budget();report=dict(actions=[],candidate_trials=0)
        result,detail=access.improve_access(structured.par,f[1],f[2],f[3],f[4],f[5],
            RoutingOptions(),budget,perf_counter()+1,report,initialization=True,hpwl_limit=0.)
        self.assertIs(result,f[1]);self.assertGreater(detail['hpwl_rejections'],0)

    def test_existing_post_SA_axis_limits_still_allow_safe_cell_mirror(self):
        f=organized();pl=f[1]
        pl.attachment_axis_limits=tuple((deepcopy(axis),0.) for axis in f[5]['host_axes'])
        self.assertTrue(pl.valid_state())
        result,detail,_,_=improve(f)
        self.assertTrue(detail['applied']);self.assertTrue(result.valid_state())
        self.assertEqual(result.attachment_axis_limits,pl.attachment_axis_limits)

    def test_already_best_orientation_is_stable_on_repeat(self):
        f=organized();first,detail,_,_=improve(f);self.assertTrue(detail['applied'])
        f[1]=first;second,detail,_,_=improve(f)
        self.assertEqual((second.cell_of,second.orient),(first.cell_of,first.orient))
        self.assertFalse(detail['applied'])


if __name__=='__main__':unittest.main()
