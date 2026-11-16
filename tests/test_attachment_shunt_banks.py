"""Owned power geometry and compact load banks on MOS/BJT ports."""
from copy import deepcopy
from math import inf
from pathlib import Path
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_attachment_ports import installed
from tests.test_passive_placement import mos,resistor
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment.attachment_shunt_banks import (
    bank_trials,finish_shunts,reserve_finish,measure_banks)
from netlist2schematic.plugins.attachment.attachment_physical import owned_footprints
from netlist2schematic.layout.structured_placement import par
from netlist2schematic.routing.routing_rules import RoutingOptions,build_scene
from netlist2schematic.routing.orthogonal_router import RoutingFailure


def fixture(kind='nmos', renamed=False, reverse=False):
    positive = kind in ('nmos','npn')
    rail = 'VDD' if positive else '0'
    source = '0' if positive else 'VDD'
    if kind in ('nmos','pmos'):
        device=mos('load','control',source)
        device.update(type=kind.upper(),bulk=source)
    else:
        device=dict(cls='BJT',type=kind.upper(),c='load',b='control',e=source,model=kind.upper())
    names=('host','res','cap','coil') if not renamed else ('x','v','u','w')
    h,r,c,l=names
    ds={h:device,r:resistor(rail,'load'),c:resistor('load',rail,'C'),l:resistor(rail,'load','L')}
    if reverse:ds=dict(reversed(list(ds.items())))
    cells={h:(2,3),r:(0,1),c:(0,2),l:(0,3)}
    case=installed(ds,cells)
    for name,ori in ((r,0 if positive else 2),(c,2 if positive else 0),(l,0 if positive else 2)):
        case[0].orient[case[3][name]]=ori
    return (*case,names)


def snapshot(pl):
    return deepcopy({k:v for k,v in vars(pl).items() if k!='rng'}),pl.rng.getstate()


def finish(case, answer='real', candidates=0, maximum=128):
    pl,cs,nets,index,plan,_=case
    budget=ai.new_attachment_budget(seconds=1,max_candidates=maximum)
    budget['candidates']=candidates;reserve_finish(plan,budget)
    report=dict(candidates=0,accepted=0,actions=[])
    if answer=='real':
        out=finish_shunts(par,pl,cs,nets,index,plan,RoutingOptions(),budget,
            perf_counter()+1,report,hpwl_limit=inf)
    else:
        with patch.object(ai,'_checked_static',return_value=answer):
            out=finish_shunts(par,pl,cs,nets,index,plan,RoutingOptions(),budget,
                perf_counter()+1,report,hpwl_limit=inf)
    return out,budget,report


class ShuntBankTests(unittest.TestCase):
    def test_mos_and_bjt_polarity_duals_generate_real_safe_packed_geometry(self):
        for kind in ('nmos','pmos','npn','pnp'):
            case=fixture(kind);pl,cs,nets,index,plan,names=case
            self.assertEqual(1,len(plan['shunt_banks']['banks']))
            old=snapshot(pl);diag={}
            trials=list(bank_trials(par,pl,cs,nets,index,plan['shunt_banks']['banks'][0],RoutingOptions(),diagnostic=diag))
            self.assertTrue(trials,kind)
            self.assertLessEqual(len(trials),12)
            self.assertEqual(1,diag['scene_templates'])
            self.assertTrue(any(ai._checked_static(par,t,nets,index,RoutingOptions(),None) for t,_ in trials),kind)
            for trial,_ in trials:
                self.assertTrue(trial.valid_state(),kind)
                self.assertEqual(pl.cell_of,trial.cell_of)
                self.assertEqual(pl.column_x,trial.column_x)
                self.assertEqual(pl.row_y,trial.row_y)
                h=index[names[0]]
                self.assertEqual((pl.pos_of(h),pl.orient[h]),(trial.pos_of(h),trial.orient[h]))
            self.assertEqual(old,snapshot(pl))

    def test_owned_footprint_contains_exact_marker_and_stub_of_each_guest(self):
        pl,cs,nets,index,plan,names=fixture()
        scene=build_scene(par,nets,pl,index)
        env=owned_footprints(par,scene,nets,index)
        owners=[index[n] for net,m in nets.items() if par.power_marker_sym(net) for n,p in m]
        for (_,rect),owner in zip(scene.marker_rects,owners):
            a=env[owner]
            self.assertLessEqual(a[0],rect[0]);self.assertLessEqual(a[1],rect[1])
            self.assertGreaterEqual(a[2],rect[2]);self.assertGreaterEqual(a[3],rect[3])

    def test_actual_markers_remain_separated_after_compaction(self):
        case=fixture();trial,_,report=finish(case)
        self.assertEqual(1,report['accepted'])
        pl,cs,nets,index,plan,_=case
        scene=build_scene(par,nets,trial,index)
        owners=[index[n] for net,m in nets.items() if par.power_marker_sym(net) for n,p in m]
        records=list(zip(owners,scene.marker_rects))
        for number,(owner,(_,a)) in enumerate(records):
            for other,(_,b) in records[number+1:]:
                if owner!=other:
                    self.assertFalse(max(a[0],b[0])<min(a[2],b[2]) and max(a[1],b[1])<min(a[3],b[3]))

    def test_owner_midpoint_mode_preserves_old_shape_and_reduces_distance(self):
        case=fixture();out,budget,report=finish(case)
        self.assertEqual(1,report['accepted'])
        self.assertEqual('symbol_midpoints',report['actions'][0]['alignment'])
        before,after=report['physical_shunt_finish']['before'],report['physical_shunt_finish']['after']
        self.assertLess(after['owner_distance'],before['owner_distance'])
        self.assertLess(after['transverse_span'],before['transverse_span'])
        self.assertEqual(before['owner_pin_alignment_error'],after['owner_pin_alignment_error'])
        self.assertLessEqual(budget['candidates'],12)

    def test_renaming_and_row_order_leave_geometric_proposals_equivalent(self):
        outputs=[]
        for rename,reverse in ((False,False),(True,True)):
            pl,cs,nets,index,plan,_=fixture(renamed=rename,reverse=reverse)
            outputs.append([sorted(t.pos_of(index[n]) for n in plan['shunt_banks']['banks'][0]['members'])
                for t,a in bank_trials(par,pl,cs,nets,index,plan['shunt_banks']['banks'][0],RoutingOptions())])
        self.assertEqual(*outputs)

    def test_translation_preserves_relative_offsets(self):
        pl,cs,nets,index,plan,_=fixture()
        a=list(bank_trials(par,pl,cs,nets,index,plan['shunt_banks']['banks'][0],RoutingOptions()))
        moved=deepcopy(pl);moved.column_x=[v+37 for v in pl.column_x];moved.row_y=[v-53 for v in pl.row_y]
        b=list(bank_trials(par,moved,cs,nets,index,plan['shunt_banks']['banks'][0],RoutingOptions()))
        self.assertEqual([t.passive_local_offsets for t,_ in a],[t.passive_local_offsets for t,_ in b])

    def test_mirror_preserves_role_placement_within_grid_quantization(self):
        pl,cs,nets,index,plan,names=fixture()
        bank=plan['shunt_banks']['banks'][0]
        a=next(bank_trials(par,pl,cs,nets,index,bank,RoutingOptions()))[0]
        mirrored=deepcopy(pl)
        mirrored.set_state([(r,len(pl.column_x)-1-c) for r,c in pl.cell_of],pl.orient)
        mirrored.orient[index[names[0]]]=1
        b=next(bank_trials(par,mirrored,cs,nets,index,bank,RoutingOptions()))[0]
        symmetry=pl.column_x[0]+pl.column_x[-1]
        for name in names[1:]:
            x,y=a.pos_of(index[name]);xx,yy=b.pos_of(index[name])
            self.assertLessEqual(abs(x+xx-symmetry),2)
            self.assertEqual(y,yy)

    def test_supply_alone_does_not_merge_cross_connected_host_banks(self):
        ds=dict(a=mos('left','right'),b=mos('right','left'),
            r=resistor('VDD','left'),c=resistor('VDD','left','C'),
            t=resistor('VDD','right'),u=resistor('VDD','right','C'))
        pl,cs,nets,index,plan=installed(ds,dict(a=(2,2),b=(2,5),r=(0,0),c=(0,1),t=(0,6),u=(0,7)))
        banks=plan['shunt_banks']['banks']
        self.assertEqual(2,len(banks))
        self.assertEqual({'a','b'},{b['anchor'][0] for b in banks})
        self.assertTrue(all(len(b['control_interfaces'])==1 and len(b['members'])==2 for b in banks))

    def test_two_bank_reservation_is_shared_without_starving_second_bank(self):
        ds=dict(a=mos('left','right'),b=mos('right','left'),
            r=resistor('VDD','left'),c=resistor('VDD','left','C'),
            t=resistor('VDD','right'),u=resistor('VDD','right','C'))
        pl,cs,nets,index,plan=installed(ds,dict(a=(2,2),b=(2,5),r=(0,0),c=(0,1),t=(0,6),u=(0,7)))
        budget=ai.new_attachment_budget();reserve_finish(plan,budget);budget['candidates']=120
        self.assertEqual(8,budget['shunt_bank_reserve'])
        report=dict(candidates=0,accepted=0,actions=[])
        with patch.object(ai,'_checked_static',return_value=False):
            out=finish_shunts(par,pl,cs,nets,index,plan,RoutingOptions(),budget,perf_counter()+1,report,hpwl_limit=inf)
        self.assertIs(pl,out)
        self.assertEqual([4,4],[r['trials'] for r in report['physical_shunt_finish']['generation']])
        self.assertEqual(128,budget['candidates'])

    def test_custom_power_connections_do_not_get_guessed_marker_ownership(self):
        pl,cs,nets,index,plan,_=fixture()
        pl.power_connections=[]
        diag={}
        self.assertEqual([],list(bank_trials(par,pl,cs,nets,index,plan['shunt_banks']['banks'][0],
            RoutingOptions(),diagnostic=diag)))
        self.assertEqual('custom_power_geometry',diag['stop_reason'])

    def test_source_managed_members_are_not_given_second_layout_ownership(self):
        from netlist2schematic.plugins.attachment.attachment_shunt_banks import compile_banks
        pl,cs,_,_,plan,names=fixture()
        plan=deepcopy(plan);plan['source_managed_members']=[names[1]]
        self.assertEqual([],compile_banks(cs,plan)['banks'])

    def test_current_signal_and_instance_names_are_not_read_from_files(self):
        pl,cs,nets,index,plan,_=fixture()
        with patch.object(Path,'read_text',side_effect=AssertionError('no GT or source reading')),patch(
                'netlist2schematic.routing.orthogonal_router.route_scene',side_effect=AssertionError('no Astar')):
            self.assertTrue(list(bank_trials(par,pl,cs,nets,index,plan['shunt_banks']['banks'][0],RoutingOptions())))

    def test_custom_grid_halo_and_clearances_are_respected(self):
        pl,cs,nets,index,plan,_=fixture()
        options=RoutingOptions(grid_step=8,halo=18,cross_clearance=16)
        trials=list(bank_trials(par,pl,cs,nets,index,plan['shunt_banks']['banks'][0],options))
        self.assertTrue(any(ai._checked_static(par,t,nets,index,options,None) for t,_ in trials))

    def test_two_conduction_hosts_on_one_node_are_ambiguous(self):
        ds=dict(a=mos('load','ga'),b=mos('load','gb'),r=resistor('VDD','load'),c=resistor('VDD','load','C'))
        pl,cs,nets,index,plan=installed(ds,dict(a=(2,2),b=(2,5),r=(0,1),c=(0,2)))
        self.assertEqual([],plan['shunt_banks']['banks'])
        self.assertEqual('no_unique_conduction_anchor',plan['shunt_banks']['skipped'][0]['reason'])

    def test_gate_clock_or_bias_alone_does_not_establish_conduction_ownership(self):
        ds=dict(a=mos('d','gate'),r=resistor('VDD','gate'),c=resistor('VDD','gate','C'))
        pl,cs,nets,index,plan=installed(ds,dict(a=(2,3),r=(0,1),c=(0,2)))
        self.assertFalse(plan['shunt_banks']['banks'])

    def test_fixed_guest_generates_nothing_and_fixed_host_stays_put(self):
        pl,cs,nets,index,plan,names=fixture()
        bank=plan['shunt_banks']['banks'][0]
        pl.fixed.add(index[names[1]])
        self.assertEqual([],list(bank_trials(par,pl,cs,nets,index,bank,RoutingOptions())))
        pl.fixed.clear();pl.fixed.add(index[names[0]])
        self.assertTrue(all(t.pos_of(index[names[0]])==pl.pos_of(index[names[0]])
            for t,_ in bank_trials(par,pl,cs,nets,index,bank,RoutingOptions())))

    def test_failed_and_late_static_validation_return_original_incumbent(self):
        for answer in (False,None):
            case=fixture();old=snapshot(case[0]);out,budget,report=finish(case,answer)
            self.assertIs(case[0],out)
            self.assertEqual(old,snapshot(case[0]))
            self.assertFalse(report['accepted'])
            self.assertEqual(answer is None,bool(budget.get('exhausted')))

    def test_expired_constructor_does_not_build_scene(self):
        pl,cs,nets,index,plan,_=fixture()
        with patch('netlist2schematic.routing.routing_rules.build_scene',side_effect=AssertionError('expired')):
            self.assertEqual([],list(bank_trials(par,pl,cs,nets,index,plan['shunt_banks']['banks'][0],
                RoutingOptions(),deadline=perf_counter()-1)))

    def test_constructor_timeout_keeps_original_and_marks_original_budget_exhausted(self):
        case=fixture();pl,cs,nets,index,plan,_=case
        budget=ai.new_attachment_budget();report=dict(candidates=0,accepted=0,actions=[])
        with patch('netlist2schematic.routing.routing_rules.build_scene',side_effect=RoutingFailure(
                'expired',{'reason':'quality_time_budget'})):
            out=finish_shunts(par,pl,cs,nets,index,plan,RoutingOptions(),budget,perf_counter()+1,report,hpwl_limit=inf)
        self.assertIs(pl,out);self.assertTrue(budget['exhausted'])

    def test_resource_error_propagates_instead_of_becoming_recovery_success(self):
        case=fixture();pl,cs,nets,index,plan,_=case
        with patch('netlist2schematic.routing.routing_rules.build_scene',side_effect=ValueError('resource failure')):
            with self.assertRaisesRegex(ValueError,'resource failure'):
                finish_shunts(par,pl,cs,nets,index,plan,RoutingOptions(),ai.new_attachment_budget(),
                    perf_counter()+1,dict(candidates=0,accepted=0,actions=[]),hpwl_limit=inf)

    def test_reserved_candidates_are_part_of_the_existing_total(self):
        pl,cs,nets,index,plan,_=fixture()
        budget=ai.new_attachment_budget();reserve_finish(plan,budget)
        self.assertEqual(4,budget['shunt_bank_reserve'])
        budget['candidates']=124
        self.assertFalse(ai._has_budget(budget,perf_counter()+1))
        report=dict(candidates=0,accepted=0,actions=[])
        with patch.object(ai,'_checked_static',return_value=False):
            out=finish_shunts(par,pl,cs,nets,index,plan,RoutingOptions(),budget,perf_counter()+1,report,hpwl_limit=inf)
        self.assertIs(pl,out)
        self.assertEqual(128,budget['candidates'])
        self.assertEqual(4,report['physical_shunt_finish']['trials'])
        self.assertEqual(128,budget['max_candidates'])

    def test_changed_terminals_are_unmeasurable_not_zero_error(self):
        pl,cs,_,index,plan,names=fixture()
        cs=deepcopy(cs);cs[index[names[1]]]['nodes']['1']='changed'
        measurement=measure_banks(pl,cs,index,plan['shunt_banks'])
        self.assertFalse(measurement['measurable']);self.assertIsNone(measurement['owner_distance'])


if __name__=='__main__':unittest.main()
