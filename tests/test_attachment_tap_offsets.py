"""Offset-only junction bands use real ports, transactions and the old ledger."""
from copy import deepcopy
import json
from pathlib import Path
from time import perf_counter
import unittest
from unittest.mock import patch

from netlist2schematic.common.passive_coordinates import validate_passive_offsets
from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment import attachment_tap_offsets as bands
from netlist2schematic.plugins.attachment.attachment_taps import measure_taps
from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
from netlist2schematic.routing.routing_rules import RoutingOptions
from tests.test_attachment_ports import installed
from tests.test_attachment_taps import snapshot
from tests.test_passive_placement import resistor


def fixture(*, shunt=True, bipolar=False, dual=False, repeats=1, reverse_pins=False):
    ds,cells,orient={},{},{}
    for k in range(repeats):
        n=lambda name:f'{name}{k}'
        low,high=('PNP','NPN') if dual else ('NPN','PNP')
        def active(kind,drain,gate):
            rail='vdd' if kind=='PNP' else '0'
            if bipolar:return dict(cls='BJT',type=kind,model=kind,c=drain,b=gate,e=rail)
            return dict(cls='MOS',type='PMOS' if kind=='PNP' else 'NMOS',model=kind,
                        d=drain,g=gate,s=rail,bulk=rail)
        ds.update({n('pa'):active(high,n('left'),n('mid')),n('pb'):active(high,n('right'),n('mid')),
                   n('a'):active(low,n('left'),n('ga')),n('b'):active(low,n('right'),n('gb')),
                   n('r'):resistor(n('mid') if reverse_pins else n('left'),n('left') if reverse_pins else n('mid')),
                   n('c'):resistor(n('mid') if reverse_pins else n('right'),n('right') if reverse_pins else n('mid'))})
        upper,lower=(2,1) if dual else (1,2)
        cells.update({n('pa'):(upper,1+6*k),n('pb'):(upper,4+6*k),
                      n('a'):(lower,1+6*k),n('b'):(lower,4+6*k),
                      n('r'):(1,2+6*k),n('c'):(1,3+6*k)})
        orient.update({n('r'):1 if reverse_pins else 3,n('c'):3 if reverse_pins else 1})
        if shunt:
            ds[n('cap')]=resistor('0' if dual else 'vdd',n('mid'),'C')
            cells[n('cap')]=(3 if dual else 0,2+6*k)
            orient[n('cap')]=2 if dual else 0
    # Set full passive orientations after conversion, just as the real final
    # stage receives them; no symbol rotation is proposed by the new module.
    pl,cs,nets,index,plan=installed(ds,cells,{n:o%2 for n,o in orient.items()})
    os=list(pl.orient)
    for n,o in orient.items():os[index[n]]=o
    pl.set_state(pl.cell_of,os)
    return pl,cs,nets,index,plan


def finish(f, *, limit=8, safe='real', hpwl_limit=None, expired=False):
    pl,cs,nets,index,plan=f
    budget=ai.new_attachment_budget(max_candidates=limit)
    report=dict(candidates=0,accepted=0,actions=[])
    until=perf_counter()+(-1 if expired else 1)
    kwargs=dict(hpwl_limit=_hpwl(structured.par,pl,nets,index)*1.15 if hpwl_limit is None else hpwl_limit)
    with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar')):
        if safe=='real':
            result=bands.finish_bands(structured.par,pl,cs,nets,index,plan,RoutingOptions(),budget,until,report,**kwargs)
        else:
            with patch.object(ai,'_checked_static',return_value=safe):
                result=bands.finish_bands(structured.par,pl,cs,nets,index,plan,RoutingOptions(),budget,until,report,**kwargs)
    return result,report,budget


class TwoArmTapOffsetTests(unittest.TestCase):
    def test_actual_gap_without_public_row_moves_only_passive_offsets(self):
        f=fixture();pl,cs,nets,index,plan=f;old=snapshot(pl)
        result,report,budget=finish(f)
        self.assertEqual(report['two_arm_band_finish']['accepted'],1)
        self.assertEqual(budget['candidates'],1)
        self.assertTrue(result.valid_state())
        self.assertEqual(_static_faults(structured.par,result,nets,index,RoutingOptions(),None)['count'],0)
        self.assertEqual(snapshot(pl),old)
        self.assertEqual((result.cell_of,result.orient,result.column_x,result.row_y,result.row_of,result.comp_at),
                         (pl.cell_of,pl.orient,pl.column_x,pl.row_y,pl.row_of,pl.comp_at))
        self.assertEqual(result.rng.getstate(),pl.rng.getstate())
        self.assertEqual(measure_taps(result,cs,index,plan['conduction_taps'])['errors'],0)
        for name in ('a0','b0','pa0','pb0'):
            i=index[name]
            self.assertEqual(result.pos_of(i),pl.pos_of(i))
            for pin in cs[i]['nodes']:self.assertEqual(result.pin_world(i,pin),pl.pin_world(i,pin))

    def test_unique_common_rail_shunt_tracks_the_real_join_midpoint(self):
        f=fixture();pl,cs,nets,index,plan=f;result,report,_=finish(f)
        e=bands.band_evidence(result,cs,index,plan,plan['conduction_taps']['groups'][0])
        point=result.pin_world(index['cap0'],'2')
        self.assertEqual(point[0],e['common_x'])
        self.assertEqual(point[1],pl.pin_world(index['cap0'],'2')[1])
        self.assertEqual(len(report['actions'][0]['offsets_changed']),3)

    def test_no_shunt_and_both_passive_pin_orders_keep_topology(self):
        for reverse in (False,True):
            f=fixture(shunt=False,reverse_pins=reverse)
            before=deepcopy(f[1]);result,report,_=finish(f)
            self.assertEqual(report['accepted'],1)
            self.assertEqual(result.comps,before)
            self.assertEqual(report['actions'][0]['optional_shunt_skipped'],'no_unique_common_shunt')

    def test_MOS_BJT_and_polarity_duals_use_actual_geometry(self):
        for bipolar in (False,True):
            for dual in (False,True):
                with self.subTest(bipolar=bipolar,dual=dual):
                    f=fixture(bipolar=bipolar,dual=dual);result,report,_=finish(f)
                    self.assertEqual(report['accepted'],1)
                    self.assertEqual(measure_taps(result,f[1],f[3],f[4]['conduction_taps'])['errors'],0)
                    self.assertTrue(result.valid_state())

    def test_fixed_arm_or_fixed_cell_rejects_whole_group(self):
        for mode in ('index','name','cell'):
            f=fixture();pl,cs,nets,index,plan=f;i=index['r0']
            if mode=='index':pl.fixed.add(i)
            elif mode=='name':pl.fixed.add('r0')
            else:pl.fixed_cells[i]=pl.cell_of[i]
            old=snapshot(pl);result,report,budget=finish(f)
            self.assertIs(result,pl);self.assertEqual(budget['candidates'],0)
            self.assertEqual(snapshot(pl),old)

    def test_fixed_host_is_unchanged_and_fixed_optional_shunt_is_not_moved(self):
        f=fixture();pl,cs,nets,index,plan=f
        pl.fixed={index[n] for n in ('pa0','a0','cap0')};pl=ai._install(pl,cs,index,plan)
        result,report,_=finish((pl,cs,nets,index,plan))
        self.assertEqual(report['accepted'],1)
        for n in ('pa0','a0','cap0'):self.assertEqual(result.pos_of(index[n]),pl.pos_of(index[n]))
        self.assertEqual(report['actions'][0]['optional_shunt_skipped'],'fixed_common_shunt')

    def test_already_in_band_is_idempotent_and_preserves_existing_channel_position(self):
        f=fixture();result,_,_=finish(f);old=snapshot(result)
        again,report,budget=finish((result,*f[1:]))
        self.assertIs(again,result);self.assertEqual(budget['candidates'],0)
        self.assertEqual(report['accepted'],0);self.assertEqual(snapshot(result),old)

    def test_multiple_conduction_hosts_stale_contract_and_nonfacing_ports_skip(self):
        for mode in ('third_host','changed_host','changed_arm','flip','misaligned'):
            f=fixture();pl,cs,nets,index,plan=f;relation=plan['conduction_taps']['groups'][0]
            if mode=='third_host':cs[index['b0']]['nodes']['S']='left0'
            elif mode=='changed_host':cs[index['a0']]['nodes']['D']='other'
            elif mode=='changed_arm':cs[index['r0']]['nodes']['2']='wrong'
            elif mode=='flip':pl.orient[index['pa0']]=2
            else:
                pl.offset_x=[0.]*len(cs);pl.offset_x[index['a0']]=10
            with self.subTest(mode=mode):
                with self.assertRaises((ValueError,KeyError)):
                    bands.band_evidence(pl,cs,index,plan,relation)
                self.assertEqual(list(bands.band_trials(pl,cs,index,plan,relation)),[])

    def test_horizontal_arms_must_face_each_other_and_their_own_branch(self):
        for orient in (0,1):
            f=fixture();pl,cs,nets,index,plan=f;pl.orient[index['r0']]=orient
            self.assertEqual(list(bands.band_trials(pl,cs,index,plan,plan['conduction_taps']['groups'][0])),[])

    def test_ambiguous_shared_shunt_is_not_arbitrarily_selected(self):
        f=fixture();pl,cs,nets,index,plan=f
        extra=deepcopy(cs[index['cap0']]);extra['id']='other_cap';cs.append(extra)
        index['other_cap']=len(cs)-1
        shunt,reason=bands._common_shunt(pl,cs,index,plan,
            bands.band_evidence(pl,cs,index,plan,plan['conduction_taps']['groups'][0]),270)
        self.assertIsNone(shunt);self.assertEqual(reason,'no_unique_common_shunt')

    def test_translation_horizontal_mirror_and_nondefault_grid_are_equivalent(self):
        original=fixture();base=bands.band_evidence(original[0],original[1],original[3],original[4],original[4]['conduction_taps']['groups'][0])
        for mode in ('translate','mirror'):
            f=fixture();pl,cs,nets,index,plan=f
            if mode=='translate':
                pl.column_x=[x+37 for x in pl.column_x];pl.row_y=[y-43 for y in pl.row_y]
            else:
                pl.set_state([(r,pl.cols-1-c) for r,c in pl.cell_of],
                    [ai._mirror_orientation(pl,i) for i in range(len(cs))])
            pl=ai._install(pl,cs,index,plan)
            trial,action=next(bands.band_trials(pl,cs,index,plan,plan['conduction_taps']['groups'][0],grid_step=8))
            self.assertAlmostEqual((action['target_y']-action['band'][0])%8,0)
            self.assertTrue(trial.valid_state())
            self.assertEqual(action['band'][1]-action['band'][0],base['band'][1]-base['band'][0])

    def test_instance_net_renaming_and_reordering_preserve_offsets(self):
        f=fixture();pl,cs,nets,index,plan=f
        names={c['id']:'Part'+str(40-k) for k,c in enumerate(cs)}
        ds={};cells={};orient={}
        # Reconstruct through the public normalized device format, including
        # reverse input order; the new plan must rediscover the same relation.
        for c in reversed(cs):
            i=index[c['id']];n=names[c['id']]
            rename=lambda net:net if net in ('0','vdd') else 'node_'+net
            if c['symbol'] in ('nmos','pmos'):
                row=dict(cls='MOS',type=c['symbol'].upper(),model=c['symbol'],
                    d=rename(c['nodes']['D']),g=rename(c['nodes']['G']),s=rename(c['nodes']['S']),bulk=rename(c['nodes']['S']))
            else:row=resistor(rename(c['nodes']['1']),rename(c['nodes']['2']),'C' if c['symbol']=='capacitor' else 'R')
            ds[n]=row;cells[n]=pl.cell_of[i];orient[n]=pl.orient[i]
        alt=installed(ds,cells,{n:o%2 for n,o in orient.items()});other,c2,n2,i2,p2=alt
        os=list(other.orient)
        for n,o in orient.items():os[i2[n]]=o
        other.set_state(other.cell_of,os)
        first,_,_=finish(f);second,_,_=finish((other,c2,n2,i2,p2))
        for n in index:self.assertEqual(first.pos_of(index[n]),second.pos_of(i2[names[n]]))

    def test_no_file_or_GT_access_no_Astar_and_no_SA_proposal_hook(self):
        f=fixture();pl,cs,nets,index,plan=f
        with patch.object(Path,'read_text',side_effect=AssertionError('file/GT')),patch(
                'netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar')):
            self.assertTrue(list(bands.band_trials(pl,cs,index,plan,plan['conduction_taps']['groups'][0])))
        with patch.object(bands,'finish_bands',side_effect=AssertionError('SA must not finish bands')):
            for _ in range(20):
                undo=pl.propose()
                if undo is not None:pl.set_state(*undo)

    def test_zero_expired_or_consumed_shared_budget_does_not_start_work(self):
        for kwargs in (dict(limit=0),dict(expired=True)):
            f=fixture();old=snapshot(f[0])
            with patch.object(bands,'band_evidence',side_effect=AssertionError('expired construction')):
                result,report,budget=finish(f,**kwargs)
            self.assertIs(result,f[0]);self.assertEqual(snapshot(result),old)
            self.assertEqual(budget['candidates'],0)
        f=fixture();budget=ai.new_attachment_budget(max_candidates=1);budget['candidates']=1;report=dict(candidates=0,accepted=0,actions=[])
        result=bands.finish_bands(structured.par,*f,RoutingOptions(),budget,perf_counter()+1,report,hpwl_limit=9999)
        self.assertIs(result,f[0]);self.assertEqual(budget['candidates'],1)

    def test_static_rejection_timeout_and_actual_blocker_roll_back_atomically(self):
        for response in (False,None):
            f=fixture();old=snapshot(f[0]);result,report,budget=finish(f,safe=response)
            self.assertIs(result,f[0]);self.assertEqual(snapshot(result),old)
            self.assertEqual(report['accepted'],0)
            if response is None:self.assertTrue(budget['exhausted'])
        f=fixture();pl,cs,nets,index,plan=f
        trial,_=next(bands.band_trials(pl,cs,index,plan,plan['conduction_taps']['groups'][0]))
        # A shunt body occupying an arm is a real geometry conflict; local
        # offset representation cannot grant it a body/marker exemption.
        i=index['cap0'];at=trial.pos_of(index['r0']);old=trial.pos_of(i)
        trial.passive_local_offsets[i][0]+=at[0]-old[0]
        trial.passive_local_offsets[i][1]+=at[1]-old[1]
        self.assertFalse(ai._checked_static(structured.par,trial,nets,index,RoutingOptions(),perf_counter()+1))

    def test_rejected_joint_shunt_trial_can_fall_back_without_partially_moving_it(self):
        f=fixture();pl,cs,nets,index,plan=f;old=snapshot(pl)
        original=ai._checked_static
        def check(par,trial,nets,index,options,until):
            if trial.pos_of(index['cap0'])!=pl.pos_of(index['cap0']):return False
            return original(par,trial,nets,index,options,until)
        with patch.object(ai,'_checked_static',side_effect=check):result,report,budget=finish(f)
        self.assertEqual(report['accepted'],1);self.assertEqual(budget['candidates'],2)
        self.assertEqual(result.pos_of(index['cap0']),pl.pos_of(index['cap0']))
        self.assertEqual(len(report['actions'][0]['offsets_changed']),2)
        self.assertEqual(snapshot(pl),old)

    def test_estimate_guard_is_the_existing_115_percent_cap(self):
        f=fixture();old=_hpwl(structured.par,f[0],f[2],f[3]);result,report,_=finish(f)
        self.assertEqual(report['accepted'],1)
        self.assertGreater(report['actions'][0]['estimated_wire_after'],old)
        self.assertLessEqual(report['actions'][0]['estimated_wire_after'],old*1.15)
        result,report,_=finish(f,hpwl_limit=old)
        self.assertIs(result,f[0]);self.assertGreater(report['two_arm_band_finish']['hpwl_rejections'],0)

    def test_validation_finishing_after_deadline_is_not_committed(self):
        f=fixture();pl,cs,nets,index,plan=f;old=snapshot(pl);until=perf_counter()+1;late=[False]
        def check(*args):late[0]=True;return True
        with patch.object(ai,'_checked_static',side_effect=check),patch.object(
                bands,'perf_counter',side_effect=lambda:until+1 if late[0] else until-.5):
            budget=ai.new_attachment_budget();report=dict(candidates=0,accepted=0,actions=[])
            result=bands.finish_bands(structured.par,*f,RoutingOptions(),budget,until,report,hpwl_limit=9999)
        self.assertIs(result,pl);self.assertEqual(snapshot(pl),old)
        self.assertTrue(budget['exhausted']);self.assertEqual(report['accepted'],0)

    def test_multiple_groups_use_latest_state_and_candidate_limit_is_shared(self):
        f=fixture(repeats=2);result,report,budget=finish(f)
        self.assertEqual(report['accepted'],2);self.assertEqual(budget['candidates'],2)
        self.assertEqual(measure_taps(result,f[1],f[3],f[4]['conduction_taps'])['errors'],0)
        result,report,budget=finish(f,limit=1)
        self.assertEqual(report['accepted'],1);self.assertEqual(budget['candidates'],1)

    def test_saved_local_offset_roundtrip_preserves_all_real_ports(self):
        f=fixture();result,_,_=finish(f)
        values=json.loads(json.dumps(result.passive_local_offsets))
        restored=deepcopy(f[0]);restored.passive_local_offsets=values
        validate_passive_offsets(restored,f[1]);self.assertTrue(restored.valid_state())
        for i,c in enumerate(f[1]):
            self.assertEqual(restored.pos_of(i),result.pos_of(i))
            for pin in c['nodes']:self.assertEqual(restored.pin_world(i,pin),result.pin_world(i,pin))

    def test_late_hook_order_is_host_refresh_then_band_then_source_axis(self):
        from netlist2schematic.plugins.attachment import attachment_source_axis
        f=fixture();pl,cs,nets,index,plan=f;calls=[]
        original=bands.finish_bands
        def refresh(*args,**kwargs):calls.append('host_refresh');return args[1]
        def band(*args,**kwargs):calls.append('tap_band');return original(*args,**kwargs)
        def source(*args,**kwargs):calls.append('source_axis');return args[1]
        metadata=dict(candidate_id='2',layout_attachments=dict(plan=plan,budget=ai.new_attachment_budget()))
        with patch.object(ai,'_refresh_shared_control_hosts',side_effect=refresh),patch.object(
                bands,'finish_bands',side_effect=band),patch.object(attachment_source_axis,'finish_source_axes',side_effect=source):
            ai.refine_attachments(structured.par,pl,cs,nets,index,metadata,options=RoutingOptions())
        self.assertEqual(calls,['host_refresh','tap_band','source_axis'])


if __name__=='__main__':unittest.main()
