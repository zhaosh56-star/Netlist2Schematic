"""Final-cell tails use old geometry/proposals and one shared opportunity."""
from copy import deepcopy
from pathlib import Path
from time import perf_counter
import unittest
from unittest.mock import patch

from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment import attachment_tail_source_finish as tails
from netlist2schematic.plugins.attachment.attachment_sources import source_axis_trials,source_group_trials
from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
from netlist2schematic.routing.routing_rules import RoutingOptions
from tests.test_attachment_sources import fixture as source_fixture,freeze
from tests.test_attachment_sources_e2e import plan_for
from tests.test_passive_placement import mos,resistor,source


def fixture(kind='nmos',reverse=False,bias_col=4,repeats=1):
    ds,cells={},{}
    upper=kind in ('pmos','pnp');rail='vdd' if upper else '0'
    for k in range(repeats):
        name=lambda n:n if k==0 else n+str(k)
        net=lambda n:n if k==0 else n+str(k)
        for n,col in (('a',2),('b',5)):
            if kind in ('nmos','pmos'):
                row=mos(net('out_'+n),net('in_'+n),net('tail'))
                row.update(type=kind.upper(),model=kind.upper(),bulk=rail)
            else:row=dict(cls='BJT',type=kind.upper(),model=kind.upper(),c=net('out_'+n),b=net('in_'+n),e=net('tail'))
            ds[name(n)]=row;cells[name(n)]=(3 if upper else 1,col+6*k)
        poles=(rail,net('tail')) if reverse else (net('tail'),rail)
        ds[name('bias')]=source('ISRC',*poles);cells[name('bias')]=(1 if upper else 3,bias_col+6*k)
        ds[name('load')]=resistor(net('out_a'),'0','C');cells[name('load')]=(0,6*k)
    pl,cs,nets,index,model=source_fixture(ds,cells)
    os=list(pl.orient)
    for k in range(repeats):os[index['bias' if not k else 'bias'+str(k)]]=0 if reverse==upper else 2
    pl.set_state(pl.cell_of,os)
    plan=plan_for(ds,pl,cs,nets,index);pl=ai._install(pl,cs,index,plan)
    return pl,cs,nets,index,plan


def finish(f,*,limit=8,safe='real',hpwl_limit=None,expired=False,budget=None):
    budget=ai.new_attachment_budget(max_candidates=limit) if budget is None else budget
    report=dict(candidates=0,accepted=0,actions=[])
    until=perf_counter()+(-1 if expired else 1)
    kwargs=dict(hpwl_limit=_hpwl(structured.par,f[0],f[2],f[3])*1.15 if hpwl_limit is None else hpwl_limit)
    with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar')):
        if safe=='real':result=tails.finish_axes(structured.par,*f,RoutingOptions(),budget,until,report,**kwargs)
        else:
            with patch.object(ai,'_checked_static',return_value=safe):
                result=tails.finish_axes(structured.par,*f,RoutingOptions(),budget,until,report,**kwargs)
    return result,report,budget


class FinalTailSourceAxisTests(unittest.TestCase):
    def test_reservation_uses_one_of_five_existing_trials_and_can_finish_at128(self):
        f=fixture();budget=ai.new_attachment_budget();budget['candidates']=123
        tails.reserve_finish(f[0],f[1],f[3],f[4],budget,perf_counter()+1)
        self.assertEqual(budget['tail_source_axis_reserve'],1)
        self.assertEqual(ai._candidate_limit(budget),127)
        budget['candidates']=127
        result,report,budget=finish(f,budget=budget)
        self.assertEqual(report['final_tail_axis']['reserved_trials'],1)
        self.assertEqual(report['accepted'],1);self.assertEqual(budget['candidates'],128)
        self.assertNotIn('tail_source_axis_reserve',budget)
        self.assertTrue(result.valid_state())

    def test_small_expired_or_other_stage_reserved_opportunity_is_not_stolen(self):
        f=fixture()
        for remaining in (0,1,2):
            b=ai.new_attachment_budget();b['candidates']=128-remaining
            tails.reserve_finish(f[0],f[1],f[3],f[4],b,perf_counter()+1)
            self.assertNotIn('tail_source_axis_reserve',b)
        b=ai.new_attachment_budget();b.update(candidates=110,candidate_ceiling=112,series_path_reserve=4)
        tails.reserve_finish(f[0],f[1],f[3],f[4],b,perf_counter()+1)
        self.assertNotIn('tail_source_axis_reserve',b)
        self.assertEqual(ai._candidate_limit(b),112)
        with patch.object(tails,'tail_ports',side_effect=AssertionError('expired construction')):
            tails.reserve_finish(f[0],f[1],f[3],f[4],b,perf_counter()-1)

    def test_old_budget_formula_is_identical_without_the_new_optional_reserve(self):
        keys=('band_compaction_reserve','tail_transistor_reserve','shunt_bank_reserve','series_path_reserve',
              'path_bank_reserve','regulation_reserve','stage_finish_reserve','stack_column_reserve','control_shunt_reserve')
        for k in range(len(keys)+1):
            b=ai.new_attachment_budget();b.update({n:2 for n in keys[:k]});b['candidate_ceiling']=120;b['stage_candidate_ceiling']=124
            self.assertEqual(ai._candidate_limit(b),min(128-2*k,120,124))
            b['tail_source_axis_reserve']=1
            self.assertEqual(ai._candidate_limit(b),min(128-2*k-1,120,124))

    def test_early_clipped_axis_then_grid_move_requires_a_fresh_final_cell_offer(self):
        f=fixture(bias_col=1);pl,cs,nets,index,plan=f
        early,_=next(source_axis_trials(pl,cs,index,plan['source_model']))
        self.assertEqual(early.offset_x[index['bias']],60)
        before=ai.measure_attachments(early,cs,index,plan)
        moved=next(t for t,a in source_group_trials(early,cs,nets,index,plan['source_model'])
            if t.cell_of[index['bias']]!=early.cell_of[index['bias']]
            and ai._source_improves(before,ai.measure_attachments(t,cs,index,plan))
            and ai._checked_static(structured.par,t,nets,index,RoutingOptions(),perf_counter()+1))
        self.assertEqual(moved.offset_x[index['bias']],0)
        result,report,_=finish((moved,cs,nets,index,plan))
        self.assertEqual(report['accepted'],1)
        self.assertEqual(report['actions'][0]['kind'],'source_tail_axis_finish')
        self.assertEqual(report['actions'][0]['axis_error_after'],0)

    def test_all_MOS_BJT_polarities_and_source_pole_orders_use_real_S_E(self):
        for kind in ('nmos','pmos','npn','pnp'):
            for reverse in (False,True):
                with self.subTest(kind=kind,reverse=reverse):
                    f=fixture(kind,reverse);old=freeze(f[0]);result,report,_=finish(f)
                    self.assertEqual(report['accepted'],1)
                    g=f[4]['source_groups'][0];anchor=tails.tail_anchor(result,f[1],f[3],g)
                    self.assertEqual(result.pin_world(f[3]['bias'],g['signal_pin'])[0],anchor['point'][0])
                    self.assertEqual(freeze(f[0]),old)
                    self.assertEqual((result.cell_of,result.orient,result.row_of,result.comp_at,result.column_x,result.row_y),
                        (f[0].cell_of,f[0].orient,f[0].row_of,f[0].comp_at,f[0].column_x,f[0].row_y))

    def test_fixed_source_id_name_cell_never_moves_and_fixed_hosts_can_stay(self):
        for mode in ('index','name','cell'):
            f=fixture();pl,cs,nets,index,plan=f;i=index['bias']
            if mode=='index':pl.fixed.add(i)
            elif mode=='name':pl.fixed.add('bias')
            else:pl.fixed_cells[i]=pl.cell_of[i]
            b=ai.new_attachment_budget();tails.reserve_finish(pl,cs,index,plan,b,perf_counter()+1)
            self.assertNotIn('tail_source_axis_reserve',b)
            old=freeze(pl);result,report,_=finish(f)
            self.assertIs(result,pl);self.assertEqual(freeze(pl),old);self.assertEqual(report['accepted'],0)
        f=fixture();pl,cs,nets,index,plan=f;pl.fixed={index['a'],index['b']};pl=ai._install(pl,cs,index,plan)
        result,report,_=finish((pl,cs,nets,index,plan));self.assertEqual(report['accepted'],1)
        for name in ('a','b'):self.assertEqual(result.pos_of(index[name]),pl.pos_of(index[name]))

    def test_stale_nonexclusive_shared_or_voltage_source_contracts_are_rejected(self):
        for mode in ('passive_tap','third_source','same_control','changed_source','changed_host','voltage'):
            f=fixture();pl,cs,nets,index,plan=f;g=plan['source_groups'][0]
            if mode=='passive_tap':cs[index['load']]['nodes']['1']='tail'
            elif mode=='third_source':cs[index['load']]['symbol']='current-source';cs[index['load']]['nodes']={'+':'tail','-':'0'}
            elif mode=='same_control':cs[index['b']]['nodes']['G']=cs[index['a']]['nodes']['G']
            elif mode=='changed_source':cs[index['bias']]['nodes']['+']='other'
            elif mode=='changed_host':cs[index['a']]['nodes']['S']='other'
            else:cs[index['bias']]['symbol']='voltage-source'
            self.assertIsNone(tails.tail_ports(cs,index,g),mode)
            self.assertIsNone(tails.tail_anchor(pl,cs,index,g),mode)

    def test_horizontal_wrong_side_and_reverse_geometry_is_not_exempted(self):
        for mode in ('horizontal','reverse','wrong_side'):
            f=fixture();pl,cs,nets,index,plan=f;g=plan['source_groups'][0]
            if mode=='horizontal':pl.orient[index['bias']]=1
            elif mode=='reverse':pl.orient[index['bias']]=2
            else:
                cells=list(pl.cell_of);cells[index['bias']]=(0,4);pl.set_state(cells,pl.orient)
            self.assertIsNone(tails.tail_anchor(pl,cs,index,g),mode)
            result,report,_=finish(f);self.assertIs(result,pl);self.assertEqual(report['accepted'],0)

    def test_zero_expired_and_consumed_shared_budget_remains_a_noop(self):
        for kwargs in (dict(limit=0),dict(expired=True)):
            f=fixture();old=freeze(f[0]);result,report,budget=finish(f,**kwargs)
            self.assertIs(result,f[0]);self.assertEqual(freeze(result),old);self.assertEqual(budget['candidates'],0)
        f=fixture();b=ai.new_attachment_budget(max_candidates=1);b['candidates']=1
        result,report,budget=finish(f,budget=b)
        self.assertIs(result,f[0]);self.assertEqual(budget['candidates'],1)

    def test_failed_or_expired_static_verification_is_atomic(self):
        for response in (False,None):
            f=fixture();old=freeze(f[0]);result,report,b=finish(f,safe=response)
            self.assertIs(result,f[0]);self.assertEqual(freeze(result),old);self.assertEqual(report['accepted'],0)
            self.assertEqual(b['candidates'],1)
            if response is None:self.assertTrue(b['exhausted'])

    def test_post_validation_deadline_does_not_commit_a_partial_source(self):
        f=fixture();old=freeze(f[0]);until=perf_counter()+1;late=[False]
        def check(*args):late[0]=True;return True
        b=ai.new_attachment_budget();r=dict(candidates=0,accepted=0,actions=[])
        with patch.object(ai,'_checked_static',side_effect=check),patch.object(tails,'perf_counter',side_effect=lambda:until+1 if late[0] else until-.5):
            result=tails.finish_axes(structured.par,*f,RoutingOptions(),b,until,r,hpwl_limit=9999)
        self.assertIs(result,f[0]);self.assertEqual(freeze(result),old);self.assertTrue(b['exhausted'])

    def test_original_source_score_and_nonincreasing_estimate_guard_still_apply(self):
        f=fixture();result,r,b=finish(f)
        self.assertEqual(r['accepted'],1)
        self.assertLess(r['actions'][0]['source_distance_after'],r['actions'][0]['source_distance_before'])
        self.assertLessEqual(r['actions'][0]['estimated_wire_after'],r['actions'][0]['estimated_wire_before'])
        old=freeze(f[0]);result,r,b=finish(f,hpwl_limit=0)
        self.assertIs(result,f[0]);self.assertEqual(freeze(result),old)
        self.assertEqual(r['final_tail_axis']['hpwl_rejections'],1)

    def test_existing_passive_offsets_and_all_non_source_real_pins_are_preserved(self):
        f=fixture();pl,cs,nets,index,plan=f
        pl.passive_local_offsets=[[0.,0.] for c in cs];pl.passive_local_offsets[index['load']]=[14,22]
        old=freeze(pl);result,r,_=finish(f);self.assertEqual(r['accepted'],1)
        self.assertEqual(result.passive_local_offsets,pl.passive_local_offsets)
        self.assertEqual(freeze(pl),old)
        for c in cs:
            if c['id']=='bias':continue
            for p in c['nodes']:self.assertEqual(result.pin_world(index[c['id']],p),pl.pin_world(index[c['id']],p))

    def test_multiple_tails_commit_on_latest_state_under_one_shared_limit(self):
        f=fixture(repeats=2);result,r,b=finish(f);self.assertEqual(r['accepted'],2)
        for g in f[4]['source_groups']:
            a=tails.tail_anchor(result,f[1],f[3],g)
            self.assertEqual(result.pin_world(f[3][g['device']],g['signal_pin'])[0],a['point'][0])
        result,r,b=finish(f,limit=1);self.assertEqual(r['accepted'],1);self.assertEqual(b['candidates'],1)

    def test_repeated_finish_is_stable_and_releases_unused_reservation(self):
        f=fixture();result,_,_=finish(f);old=freeze(result)
        b=ai.new_attachment_budget();b['tail_source_axis_reserve']=1
        again,r,b=finish((result,*f[1:]),budget=b)
        self.assertIs(again,result);self.assertEqual(freeze(again),old);self.assertEqual(b['candidates'],0)
        self.assertNotIn('tail_source_axis_reserve',b)

    def test_renaming_reordering_translation_and_mirror_preserve_axis_rule(self):
        f=fixture();result,_,_=finish(f)
        for mode in ('translate','mirror'):
            other=fixture();pl,cs,nets,index,plan=other
            if mode=='translate':pl.column_x=[x+37 for x in pl.column_x];pl.row_y=[y-43 for y in pl.row_y]
            else:pl.set_state([(r,pl.cols-1-c) for r,c in pl.cell_of],[ai._mirror_orientation(pl,i) for i in range(len(cs))])
            pl=ai._install(pl,cs,index,plan);second,r,_=finish((pl,cs,nets,index,plan))
            self.assertEqual(r['accepted'],1)
            self.assertEqual(r['actions'][0]['axis_error_after'],0)
        pl,cs,nets,index,plan=f;names={n:'Unit'+str(30-k) for k,n in enumerate(index)}
        changed=deepcopy(cs)
        for c in changed:
            c['id']=names[c['id']];c['nodes']={pin:net if net in ('0','vdd') else 'signal_'+net for pin,net in c['nodes'].items()}
        g=deepcopy(plan['source_groups'][0]);g['device']=names[g['device']];g['members']=[names[n] for n in g['members']]
        g['host_ports']=[(names[n],p) for n,p in g['host_ports']];g['attachment_net']='signal_'+g['attachment_net']
        g['source_pins']={p:n if n in ('0','vdd') else 'signal_'+n for p,n in g['source_pins'].items()}
        self.assertIsNotNone(tails.tail_ports(changed,{names[n]:i for n,i in index.items()},g))
        changed.reverse();self.assertIsNotNone(tails.tail_ports(changed,{c['id']:i for i,c in enumerate(changed)},g))

    def test_rule_uses_no_files_or_GT_and_is_not_called_from_SA(self):
        f=fixture();pl,cs,nets,index,plan=f
        with patch.object(Path,'read_text',side_effect=AssertionError('file/GT')):
            self.assertIsNotNone(tails.tail_anchor(pl,cs,index,plan['source_groups'][0]))
            b=ai.new_attachment_budget();tails.reserve_finish(pl,cs,index,plan,b,perf_counter()+1)
        with patch.object(tails,'finish_axes',side_effect=AssertionError('postSA only')):
            for _ in range(20):
                undo=pl.propose()
                if undo is not None:pl.set_state(*undo)

    def test_order_is_host_refresh_band_tail_then_neutral_source_axis(self):
        from netlist2schematic.plugins.attachment import attachment_tap_offsets,attachment_source_axis
        f=fixture();calls=[]
        original=tails.finish_axes
        def refresh(*args,**kwargs):calls.append('refresh');return args[1]
        def band(*args,**kwargs):calls.append('band');return args[1]
        def tail(*args,**kwargs):calls.append('tail');return original(*args,**kwargs)
        def neutral(*args,**kwargs):calls.append('neutral');return args[1]
        metadata=dict(candidate_id='2',layout_attachments=dict(plan=f[4],budget=ai.new_attachment_budget()))
        with patch.object(ai,'_refresh_shared_control_hosts',side_effect=refresh),patch.object(attachment_tap_offsets,'finish_bands',side_effect=band),patch.object(tails,'finish_axes',side_effect=tail),patch.object(attachment_source_axis,'finish_source_axes',side_effect=neutral):
            ai.refine_attachments(structured.par,*f[:4],metadata,options=RoutingOptions())
        self.assertEqual(calls,['refresh','band','tail','neutral'])


if __name__=='__main__':unittest.main()
