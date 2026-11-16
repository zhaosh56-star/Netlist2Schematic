"""Whole-net forecasts and bounded, independent parent transactions."""
from copy import deepcopy
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_attachment_regulation import build,circuit,fixture,finish
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment import attachment_regulation as reg
from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
from netlist2schematic.layout.structured_placement import par


def multiple():
    devices={};cells={};formals=[]
    for prefix,shift in (('first_',0),('second_',8)):
        ds,cs,ports,_=circuit()
        for n,d in ds.items():
            devices[prefix+n]={k:prefix+v if k in ('d','g','s','c','b','e')
                and v not in ('vdd','0') else v for k,v in d.items()}
            r,c=cs[n];cells[prefix+n]=(r,c+shift)
        formals.extend(prefix+p for p in ports)
    return build(devices,cells,tuple(formals))


class RegulationForecastBudgetTests(unittest.TestCase):
    def test_projected_real_pin_hpwl_matches_unchanged_orientation_trial(self):
        f=fixture();pl,cs,ns,index,plan=f[1:]
        pl.offset_x=[0.]*len(cs)
        pl.offset_x[index['ua']]=10.
        forecast=reg._wire_forecast(pl,cs,set(plan['regulation_regions']['power_nets']))
        proposals=list(reg.region_trials(pl,cs,index,plan['regulation_regions'],max_trials=16))
        self.assertTrue(proposals)
        for trial,action in proposals:
            if action['control_facing']:continue
            target={index[n]:c for n,c in action['columns'].items()}
            self.assertAlmostEqual(forecast(target),_hpwl(par,trial,ns,index))
            self.assertAlmostEqual(action['forecast_hpwl'],_hpwl(par,trial,ns,index))

    def test_expensive_scene_and_search_are_not_called_for_forecast_sorting(self):
        f=fixture();before=deepcopy((f[1].cell_of,f[1].orient,f[1].rng.getstate()))
        with patch.object(ai,'_checked_static',side_effect=AssertionError('static in generator')), \
             patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('router in generator')):
            trials=list(reg.region_trials(f[1],f[2],f[4],f[5]['regulation_regions']))
        forecasts=[a['forecast_hpwl'] for _,a in trials if not a['control_facing']]
        self.assertEqual(forecasts,sorted(forecasts))
        self.assertEqual(before,(f[1].cell_of,f[1].orient,f[1].rng.getstate()))

    def test_ranking_includes_shared_node_terminals_outside_parent(self):
        f=fixture();model=f[5]['regulation_regions'];pl=f[1]
        forecast=reg._wire_forecast(pl,f[2],set(model['power_nets']))
        before=forecast({})
        # The tail remains outside the parent, but its connection to every
        # shared-source device is included in candidate wire estimates.
        moved=deepcopy(pl);i=f[4]['t'];r,c=moved.cell_of[i]
        moved.set_state([cell if j!=i else (r,len(pl.column_x)-1)
                         for j,cell in enumerate(moved.cell_of)],moved.orient)
        changed=reg._wire_forecast(moved,f[2],set(model['power_nets']))({})
        self.assertNotEqual(before,changed)

    def test_both_disjoint_parents_get_candidate_opportunities(self):
        f=multiple();groups=f[5]['regulation_regions']['groups']
        self.assertEqual(len(groups),2)
        trials=list(reg.region_trials(f[1],f[2],f[4],f[5]['regulation_regions'],max_trials=16))
        counts={g['id']:sum(a['region']==g['id'] for _,a in trials) for g in groups}
        self.assertEqual(list(counts.values()),[8,8])
        self.assertLessEqual(len(trials),16)

    def test_disjoint_transactions_inherit_latest_accepted_positions(self):
        f=multiple();result,report,budget=finish(f)
        detail=report['regulation_finish']
        self.assertEqual(len(detail['groups']),2)
        self.assertTrue(all(g['applied'] for g in detail['groups']))
        self.assertEqual(report['accepted'],2)
        self.assertEqual(detail['trials'],16)
        self.assertEqual(budget['candidates'],16)
        self.assertEqual([g['budget']['start_candidates'] for g in detail['groups']],[0,8])
        self.assertTrue(all(not g['budget']['shared_budget_reset'] for g in detail['groups']))
        for group in f[5]['regulation_regions']['groups']:
            model=dict(f[5]['regulation_regions'],groups=[group])
            a=reg.measure_regions(f[1],f[2],f[4],model)
            b=reg.measure_regions(result,f[2],f[4],model)
            self.assertLess(b['main_pin_error'],a['main_pin_error'])

    def test_later_group_timeout_keeps_first_accepted_group(self):
        f=multiple();groups=f[5]['regulation_regions']['groups'];second=groups[1]
        clock=[100.]
        def checker(par,trial,*args):
            if any(trial.cell_of[f[4][n]]!=f[1].cell_of[f[4][n]] for n in second['members']):
                clock[0]=103.;return None
            clock[0]=100.05;return True
        with patch.object(reg,'perf_counter',side_effect=lambda:clock[0]), \
             patch.object(ai,'perf_counter',side_effect=lambda:clock[0]), \
             patch.object(ai,'_checked_static',side_effect=checker):
            result,report,budget=finish(f,until=102.)
        detail=report['regulation_finish']
        self.assertTrue(detail['groups'][0]['applied'])
        self.assertFalse(detail['groups'][1]['applied'])
        self.assertTrue(budget['exhausted'])
        self.assertEqual(report['accepted'],1)
        self.assertTrue(any(result.cell_of[f[4][n]]!=f[1].cell_of[f[4][n]] for n in groups[0]['members']))
        self.assertTrue(all(result.cell_of[f[4][n]]==f[1].cell_of[f[4][n]] for n in second['members']))

    def test_timeout_in_same_group_keeps_earlier_timely_verified_best(self):
        f=fixture();clock=[100.];accepted=[]
        def checker(par,trial,*args):
            if not accepted:
                accepted.append(deepcopy(trial));clock[0]=100.1;return True
            clock[0]=102.;return None
        with patch.object(reg,'perf_counter',side_effect=lambda:clock[0]), \
             patch.object(ai,'perf_counter',side_effect=lambda:clock[0]), \
             patch.object(ai,'_checked_static',side_effect=checker):
            result,report,budget=finish(f,until=101.)
        self.assertTrue(report['regulation_finish']['applied'])
        self.assertEqual(result.cell_of,accepted[0].cell_of)
        self.assertEqual(result.orient,accepted[0].orient)
        self.assertTrue(budget['exhausted'])

    def test_parent_quotas_respect_existing_nested_candidate_ceiling(self):
        f=multiple();budget=ai.new_attachment_budget();budget['candidates']=30
        budget['stage_candidate_ceiling']=34
        _,report,budget=finish(f,safe=False,budget=budget)
        self.assertLessEqual(report['regulation_finish']['trials'],4)
        self.assertLessEqual(budget['candidates'],34)
        self.assertEqual(budget['stage_candidate_ceiling'],34)
        self.assertEqual(budget['max_candidates'],128)

    def test_reference_physical_axes_and_fixed_tail_are_never_expanded(self):
        f=multiple();pl=f[1]
        for n in ('first_t','second_t'):pl.fixed.add(f[4][n])
        before=(list(pl.column_x),list(pl.row_y),list(pl.row_of))
        result,report,budget=finish(f)
        self.assertEqual(before,(result.column_x,result.row_y,result.row_of))
        for n in ('first_t','second_t'):
            self.assertEqual(result.pos_of(f[4][n]),pl.pos_of(f[4][n]))


if __name__=='__main__':unittest.main()
