"""Conflicting mirror target rows use bounded real-initializer estimates."""
from copy import deepcopy
from pathlib import Path
import json
import random
import tempfile
import unittest
from unittest.mock import patch

from tests.test_branch_levels import mos, passive, current
from tests.test_corresponding_branch_rows import mirror, run_plan
from tests.test_series_alignment import PAR
from netlist2schematic.layout import structured_placement as sp
from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.plugins.columns.row_estimate import make_row_estimator, RowEstimateUnavailable


def family():
    devices, rows = mirror()
    devices.update(tail=mos('tail_node','0','bias'),
        input_a=mos('da','tail_node','ga'), input_b=mos('db','tail_node','gb'))
    rows.update(tail=2,input_a=1,input_b=1)
    return devices, rows


def estimate_for(devices, *, full_devices=None):
    doc=to_document(devices);comps,nets=doc['components'],doc['nets']
    index={c['id']:i for i,c in enumerate(comps)}
    full_devices=full_devices or devices
    levels=sp.dl.compute_levels(full_devices)
    conduction={n for d in full_devices.values() if sp._active(d)
        for n in (d.get('d'),d.get('s'),d.get('c'),d.get('e')) if n is not None}
    att=sp.legacy.attach_info(full_devices,levels['net_levels'],conduction)
    att={n:v for n,v in att.items() if n in devices}
    modules=None
    if full_devices is not devices:
        from netlist2schematic.plugins.passive_skeleton.plan import compile_plan
        full=to_document(full_devices)
        full_index={c['id']:i for i,c in enumerate(full['components'])}
        plan=compile_plan(PAR,full_devices,full['components'],full['nets'],sp._structures(full_devices))
        modules=dict(comps=full['components'],nets=full['nets'],index=full_index,plan=plan)
    return make_row_estimator(devices,comps,nets,index,levels,att,sp._structures(devices),
                               7321,1,modules=modules)


class BranchRowCostTests(unittest.TestCase):
    def assert_safe(self, before, after, report):
        for order in report['strict_orders']:
            self.assertLess(after[order['upper']],after[order['lower']])
        for a,b in report['preserved_same_rows']:
            self.assertEqual(after[a],after[b])
        for column in report['columns']:
            deltas={after[n]-before[n] for n in column['members']}
            self.assertEqual(len(deltas),1)
            if column['locked_reason']:
                self.assertEqual(deltas,{0})
        json.dumps(report,allow_nan=False)

    def test_minimum_cost_selects_a_target_without_moving_hosts(self):
        devices,rows=family();original=deepcopy(devices);seen=[]
        def cost(state,**kwargs):
            seen.append(state)
            return {0:9.,1:5.,2:2.}[state['reference']],dict(astar_calls=0)
        result,report=run_plan(devices,rows,estimate_rows=cost)
        self.assertEqual(result,{**rows,'reference':2})
        self.assertEqual({r['reference'] for r in seen},{0,1,2})
        self.assertEqual(report['target_search']['evaluated_states'],3)
        action=report['actions'][0]
        self.assertEqual(action['selection'],'minimum_estimated_cost')
        self.assertLess(action['selected_cost'],action['reference_cost'])
        self.assertEqual(devices,original);self.assert_safe(rows,result,report)

    def test_original_row_remains_an_option_and_wins_a_tie(self):
        devices,rows=family()
        for costs in ({0:1.,1:2.,2:3.},{0:1.,1:1.,2:1.}):
            with self.subTest(costs=costs):
                result,report=run_plan(devices,rows,
                    estimate_rows=lambda state,**kw:(costs[state['reference']],{}))
                self.assertEqual(result,rows);self.assertFalse(report['applied'])
                self.assertEqual(report['target_search']['stop_reason'],
                                 'current_rows_have_lowest_estimated_cost')

    def test_equal_improving_cost_prefers_smaller_motion(self):
        devices,rows=family()
        result,report=run_plan(devices,rows,
            estimate_rows=lambda state,**kw:(10. if state['reference']==0 else 1.,{}))
        self.assertEqual(result['reference'],1)
        self.assert_safe(rows,result,report)

    def test_fixed_reference_and_unambiguous_targets_do_not_invoke_estimator(self):
        for fixed in (False,True):
            devices,rows=family() if fixed else mirror()
            def forbidden(*args,**kwargs):raise AssertionError('No ambiguous movable reference')
            result,report=run_plan(devices,rows,estimate_rows=forbidden,
                                   **({'fixed':['reference']} if fixed else {}))
            self.assertFalse(report['target_search']['activated'])
            self.assertEqual(report['target_search']['evaluated_states'],0)

    def test_other_same_row_guards_remain_hard_before_cost(self):
        devices,rows=family();rows['input_a']=rows['input_b']=0
        records=sp._structures(devices)+[dict(kind='differential_pair',roles={},confidence='high',
            layout=dict(align_rows=[['reference','input_a']]))]
        calls=[]
        result,report=run_plan(devices,rows,records=records,
            estimate_rows=lambda state,**kw:(calls.append(state) or 10.,{}))
        self.assertEqual(result,rows);self.assertEqual(len(calls),1)
        self.assertTrue(all(t['status']=='hard_constraint_rejected' for t in report['target_search']['trials']))
        self.assert_safe(rows,result,report)

    def test_conflicting_multi_output_mirror_rows_are_soft(self):
        devices,rows=family();rows['reference']=1
        result,report=run_plan(devices,rows,
            estimate_rows=lambda state,**kw:(abs(state['reference']-2),{}))
        self.assertEqual(result['reference'],2)
        self.assertIn(dict(reference='reference',outputs=['output','tail']),report['soft_mirror_rows'])
        self.assertNotIn(['output','reference'],report['preserved_same_rows'])
        self.assert_safe(rows,result,report)

    def test_incomplete_budget_round_rolls_back_even_an_improving_partial_trial(self):
        devices,rows=family();seen=[]
        def cost(state,**kwargs):
            seen.append(state)
            return 10.-state['reference'],{}
        result,report=run_plan(devices,rows,estimate_rows=cost,max_estimates=2)
        self.assertEqual(result,rows);self.assertEqual(len(seen),2)
        self.assertEqual(report['target_search']['evaluated_states'],2)
        self.assertEqual(report['target_search']['stop_reason'],'local_budget')
        self.assertFalse(any(t['accepted'] for t in report['target_search']['trials']))

    def test_unavailable_or_expired_estimates_leave_original_state(self):
        devices,rows=family()
        def unavailable(*args,**kwargs):raise RowEstimateUnavailable('projection_unavailable')
        result,report=run_plan(devices,rows,estimate_rows=unavailable)
        self.assertEqual(result,rows)
        self.assertEqual(report['target_search']['stop_reason'],'incumbent_estimate_unavailable')
        for kwargs in (dict(max_estimates=0),dict(estimate_seconds=0)):
            result,report=run_plan(devices,rows,estimate_rows=lambda *a,**kw:(_ for _ in ()).throw(
                AssertionError('Budget must be checked before estimation')),**kwargs)
            self.assertEqual(result,rows)
            self.assertEqual(report['target_search']['evaluated_states'],0)

    def test_invalid_budgets_and_nonfinite_costs_are_not_success(self):
        devices,rows=family()
        for kwargs in (dict(max_estimates=True),dict(max_estimates=-1),dict(estimate_seconds=float('inf')),
                       dict(estimate_seconds=True),dict(estimate_rows=0)):
            with self.assertRaises(ValueError):run_plan(devices,rows,**kwargs)
        for value in (float('nan'),float('inf'),True):
            with self.assertRaises(ValueError):
                run_plan(devices,rows,estimate_rows=lambda *a,**kw:(value,{}))

    def test_cost_selection_is_equivalent_after_renaming_reorder_and_row_translation(self):
        devices,rows=family()
        expected,_=run_plan(devices,rows,
            estimate_rows=lambda state,**kw:(abs(state['reference']-state['tail']),{}))
        names={n:'other'+str(i) for i,n in enumerate(reversed(devices))}
        fields={'d','s','g','bulk'}
        net=lambda n:n if n in ('0','vdd') else 'changed_'+n
        changed={names[n]:{k:net(v) if k in fields else v for k,v in d.items()}
                 for n,d in reversed(list(devices.items()))}
        translated={names[n]:r+4 for n,r in rows.items()}
        result,report=run_plan(changed,translated,estimate_rows=lambda state,**kw:(
            abs(state[names['reference']]-state[names['tail']]),{}))
        self.assertEqual({n:result[names[n]]-4 for n in rows},expected)
        self.assert_safe(translated,result,report)


class RowEstimatorIntegrationTests(unittest.TestCase):
    def test_real_initializer_uses_fresh_state_and_no_sa_or_astar(self):
        devices,rows=family();original=deepcopy(devices);rng=random.getstate()
        estimator=estimate_for(devices)
        with patch.object(sp,'_anneal',side_effect=AssertionError('No SA in row trials')), \
             patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('No A*')):
            first=estimator(rows);second=estimator(rows)
        self.assertEqual(first,second);self.assertEqual(random.getstate(),rng)
        self.assertEqual(devices,original)
        self.assertEqual(first[1]['objective'],'existing_structured_SA_estimate')
        self.assertTrue(all('point' in p and 'orientation' in p
                            for p in first[1]['initial_placement'].values()))
        with self.assertRaises(RowEstimateUnavailable):estimator(rows,deadline=0)

    def test_rlc_and_source_geometry_are_included_without_mutating_module_plan(self):
        devices,rows=family()
        full={**devices,'r':passive('vdd','mid'),
              'l':passive('mid','feed','L'),'i':current('feed','bias'),
              'c':passive('port','0','C')}
        estimator=estimate_for(devices,full_devices=full)
        with patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('No A*')):
            cost,metrics=estimator(rows)
        projection=metrics['module_projection']
        self.assertEqual(set(projection['guests']),{'r','l','i','c'})
        self.assertEqual(set(projection['initial_positions']),{'r','l','i','c'})
        self.assertEqual(projection['crossing_estimate'],'active_core_only')
        self.assertEqual(projection['legality'],'deferred_to_original_packing_and_routing')
        self.assertEqual(metrics['astar_calls'],0)
        json.dumps(metrics,allow_nan=False)

    def test_default_candidate_runs_estimation_then_keeps_new_rows_during_sa(self):
        text='''.model NM NMOS
.model PM PMOS
MREF bias bias 0 0 NM
MOUT join bias 0 0 NM
MCAS port control join 0 NM
MTAIL tail bias 0 0 NM
MINA da ga tail 0 NM
MINB db gb tail 0 NM
MLOADA da la vdd vdd PM
MLOADB db lb vdd vdd PM
'''
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'case.cir';source.write_text(text,encoding='utf8')
            before=source.read_bytes()
            for item in sp.create_candidates(source,iterations=90,passive_skeleton='auto',candidate_ids=(2,3)):
                pl,comps,nets,index,data=item
                report=data['layout_branch_rows']
                self.assertTrue(report['target_search']['activated'])
                self.assertEqual(report['target_search']['evaluated_states'],3)
                self.assertEqual({n:pl.row_of[index[n]] for n in report['active_rows_after']},
                                 report['active_rows_after'])
                self.assertEqual(source.read_bytes(),before)


if __name__=='__main__':unittest.main()
