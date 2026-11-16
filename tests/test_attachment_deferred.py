"""Resume fixed-axis attachment finishing after SA repairs an unsafe seed."""
from copy import deepcopy
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_passive_placement import convert,mos,resistor
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.layout.structured_placement import par
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.routing.routing_rules import RoutingOptions


def fixture(*,count=1,rename=False,candidate='2'):
    devices={'host':mos('out','control')};cells={'host':(2,3)}
    for k in range(count):
        devices['guest'+str(k)]=resistor('control','boundary'+str(k))
        cells['guest'+str(k)]=(0,k)
    if rename:
        names={n:'device'+str(40-k) for k,n in enumerate(devices)}
        devices={names[n]:{k:'wire_'+v if k in ('d','g','s','a','b') and v not in ('0','vdd') else v
            for k,v in row.items()} for n,row in reversed(list(devices.items()))}
        cells={names[n]:c for n,c in cells.items()}
    pl,comps,nets,index,_=convert(devices,cells)
    plan=ai.build_attachment_plan(devices,comps,nets,index,analyze_structures(devices),
        compute_levels(devices),pl.row_of,policy='passive')
    budget=ai.new_attachment_budget();budget.update(candidates=102,spent_seconds=.25)
    metadata=dict(candidate_id=candidate,layout_attachments=dict(policy='passive',applied=False,
        initialization=dict(applied=False,static_rejections=20),plan=plan,budget=budget))
    return pl,comps,nets,index,metadata


def finish(f,*,safe=True,deadline=None,budget=None):
    pl,c,n,i,m=f
    with patch.object(ai,'_checked_static',return_value=safe), \
         patch('netlist2schematic.layout.structured_placement._anneal',side_effect=AssertionError('SA forbidden')), \
         patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar forbidden')):
        return ai.refine_attachments(par,pl,c,n,i,m,options=RoutingOptions(),
            deadline=perf_counter()+1 if deadline is None else deadline,budget=budget)


class DeferredAttachmentTests(unittest.TestCase):
    def test_static_valid_post_sa_seed_gets_existing_policy_and_actual_improvement(self):
        f=fixture();pl,c,n,i,m=f
        result,meta=finish(f);report=meta['layout_attachments']['refinement']
        self.assertTrue(report['deferred_seed']['installed'])
        self.assertTrue(report['applied']);self.assertIsInstance(result,ai.AttachmentPlacement)
        self.assertTrue(result.valid_state())
        self.assertTrue(meta['layout_attachments']['applied'])
        self.assertTrue(meta['layout_attachments']['deferred_seed_applied'])
        self.assertLess(report['after']['owner_distance'],report['before']['owner_distance'])
        self.assertFalse(m['layout_attachments']['applied'])

    def test_resumption_itself_does_not_move_cells_axes_or_rng(self):
        f=fixture();pl,c,n,i,m=f;seen=[]
        original=deepcopy((pl.cell_of,pl.orient,pl.column_x,pl.row_y,pl.rng.getstate()))
        def inspect_seed(par,trial,nets,index,options,until):
            if not seen:seen.append(deepcopy((trial.cell_of,trial.orient,trial.column_x,trial.row_y,trial.rng.getstate())))
            return False
        with patch.object(ai,'_checked_static',side_effect=inspect_seed):
            result,meta=ai.refine_attachments(par,pl,c,n,i,m,options=RoutingOptions())
        self.assertEqual(seen,[original]);self.assertIs(result,pl)
        self.assertEqual((pl.cell_of,pl.orient,pl.column_x,pl.row_y,pl.rng.getstate()),original)

    def test_reference_candidate_zero_and_one_remain_untouched(self):
        for candidate in ('0','1'):
            f=fixture(candidate=candidate);result,meta=finish(f)
            self.assertIs(result,f[0])
            self.assertEqual(meta['layout_attachments']['refinement']['reason'],'no_attachment_seed')
            self.assertNotIn('deferred_seed',meta['layout_attachments']['refinement'])

    def test_existing_policy_uses_original_refinement_path(self):
        f=fixture();pl,c,n,i,m=f;pl=ai._install(pl,c,i,m['layout_attachments']['plan'])
        result,meta=finish((pl,c,n,i,m))
        self.assertNotIn('deferred_seed',meta['layout_attachments']['refinement'])
        self.assertIsInstance(result,ai.AttachmentPlacement)

    def test_static_conflict_is_not_relabelled_as_a_safe_policy(self):
        f=fixture();result,meta=finish(f,safe=False)
        self.assertIs(result,f[0])
        report=meta['layout_attachments']['refinement']
        self.assertFalse(report['deferred_seed']['installed'])
        self.assertEqual(report['deferred_seed']['reason'],'post_sa_static_conflict')
        self.assertEqual(report['candidates'],0)

    def test_oracle_timeout_keeps_original_and_exhausts_only_shared_ledger(self):
        f=fixture();result,meta=finish(f,safe=None)
        self.assertIs(result,f[0]);r=meta['layout_attachments']['refinement']
        self.assertTrue(r['budget']['exhausted']);self.assertEqual(r['candidates'],0)
        self.assertFalse(r['deferred_seed']['installed'])

    def test_invalid_post_sa_state_is_not_adopted(self):
        f=fixture();pl,c,n,i,m=f;pl.orient[i['host']]=99
        result,meta=finish(f)
        self.assertIs(result,pl)
        self.assertEqual(meta['layout_attachments']['refinement']['deferred_seed']['reason'],'invalid_post_sa_state')

    def test_source_row_order_remains_a_precondition(self):
        devices=dict(upper=mos('out','bias','middle'),lower=mos('middle','control'),r=resistor('control','input'))
        pl,c,n,i,_=convert(devices,dict(upper=(1,3),lower=(2,3),r=(0,0)))
        plan=ai.build_attachment_plan(devices,c,n,i,analyze_structures(devices),compute_levels(devices),pl.row_of)
        cells=list(pl.cell_of);cells[i['upper']],cells[i['lower']]=cells[i['lower']],cells[i['upper']]
        pl.set_state(cells,pl.orient)
        m=dict(candidate_id='2',layout_attachments=dict(policy='passive',applied=False,
            plan=plan,budget=ai.new_attachment_budget()))
        result,meta=finish((pl,c,n,i,m))
        self.assertIs(result,pl)
        self.assertEqual(meta['layout_attachments']['refinement']['deferred_seed']['reason'],'invalid_post_sa_state')

    def test_expired_or_fully_spent_budget_never_starts_static_adoption(self):
        for key in ('candidates','spent_seconds','deadline'):
            f=fixture();b=f[-1]['layout_attachments']['budget'];deadline=None
            if key=='candidates':b['candidates']=128
            elif key=='spent_seconds':b['spent_seconds']=1
            else:deadline=0
            with patch.object(ai,'_checked_static',side_effect=AssertionError('No budget')):
                result,meta=ai.refine_attachments(par,*f[:-1],f[-1],options=RoutingOptions(),deadline=deadline)
            self.assertIs(result,f[0]);self.assertEqual(meta['layout_attachments']['refinement']['candidates'],0)

    def test_remaining_trials_and_time_are_not_reset(self):
        f=fixture();result,meta=finish(f)
        b=meta['layout_attachments']['budget'];r=meta['layout_attachments']['refinement']
        self.assertEqual(b['candidates'],102+r['candidates'])
        self.assertLessEqual(b['candidates'],128)
        self.assertGreaterEqual(b['spent_seconds'],.25)
        self.assertEqual(b['seconds_limit'],1)

    def test_six_local_groups_share_remainder_instead_of_starving_later_ones(self):
        f=fixture(count=6);result,meta=finish(f)
        report=meta['layout_attachments']['refinement'];limits=report['deferred_group_limits']
        self.assertEqual(len(limits),6)
        self.assertTrue(all(1<=value<=5 for value in limits.values()))
        self.assertLessEqual(report['candidates'],26)

    def test_fixed_guest_stays_fixed_when_a_policy_can_be_adopted(self):
        f=fixture();pl,c,n,i,m=f;j=i['guest0'];pl.fixed.add(j);pl.fixed_cells[j]=pl.cell_of[j]
        old=deepcopy((pl.cell_of[j],pl.orient[j],pl.pos_of(j)))
        result,meta=finish(f)
        self.assertEqual((result.cell_of[j],result.orient[j],result.pos_of(j)),old)
        self.assertTrue(result.valid_state())

    def test_rename_and_line_reorder_do_not_disable_resumption(self):
        for renamed in (False,True):
            f=fixture(rename=renamed);result,meta=finish(f)
            self.assertTrue(meta['layout_attachments']['refinement']['deferred_seed']['installed'])
            self.assertTrue(meta['layout_attachments']['refinement']['applied'])
            self.assertTrue(result.valid_state())

    def test_actual_static_geometry_can_admit_post_sa_port_repair(self):
        pl,c,n,i,m=fixture()
        result,meta=ai.refine_attachments(par,pl,c,n,i,m,options=RoutingOptions(),deadline=perf_counter()+1)
        self.assertTrue(meta['layout_attachments']['refinement']['deferred_seed']['installed'])
        self.assertTrue(result.valid_state())
        self.assertTrue(ai._static(par,result,n,i,RoutingOptions(),perf_counter()+1))


if __name__=='__main__':unittest.main()
