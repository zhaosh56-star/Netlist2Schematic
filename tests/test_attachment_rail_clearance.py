"""Initial supply exits must not block unrelated local structure proposals."""
from copy import deepcopy
from pathlib import Path
import tempfile
from time import perf_counter
import unittest
from unittest.mock import patch

from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment import attachment_rail_clearance as rails
from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
from netlist2schematic.routing.routing_rules import RoutingOptions
from tests.test_attachment_sources import freeze
from tests.test_attachment_sources_e2e import plan_for
from tests.test_passive_placement import convert, mos, resistor, source


def fixture(kind='R', *, rail='vdd', rail_first=False, copies=1, reverse_order=False):
    devices, cells = {}, {}
    top = structured.par.power_marker_sym(rail) == 'vdd-port'
    for k in range(copies):
        q, name, signal = f'q{k}', f'load{k}', f'signal{k}'
        devices[q] = mos(f'output{k}', signal)
        poles = (rail, signal) if rail_first else (signal, rail)
        devices[name] = source(kind, *poles) if kind in ('VSRC', 'ISRC') else resistor(*poles, kind)
        cells[q] = (2, 2 + 3*k)
        cells[name] = (0 if top else 4, 2 + 3*k)
    if reverse_order:
        devices = dict(reversed(list(devices.items())))
    # Flexible's old class permits only two orientations. Install its ordinary
    # attachment freedoms first, then choose a real inward direction; diode
    # geometry uses a different orientation order from R/C/L.
    pl, comps, nets, index, _ = convert(devices, cells, {n:0 for n in devices})
    plan = plan_for(devices, pl, comps, nets, index)
    pl = ai._install(pl, comps, index, plan)
    os = list(pl.orient)
    for k in range(copies):
        i = index[f'load{k}']
        candidates=[]
        for orientation in pl.allowed_orientations(i):
            pins=structured.par.GEO_DOC[comps[i]['symbol']]['orientations'][orientation]['pins']
            directions={p['name']:structured.par.DIR_VEC[p['direction']] for p in pins}
            if any((-structured.par.STUB_LEN if structured.par.power_marker_sym(n)=='vdd-port' else structured.par.STUB_LEN)*directions[p][1]<0
                   for p,n in comps[i]['nodes'].items() if structured.par.power_marker_sym(n)):
                candidates.append(orientation)
        os[i] = candidates[0] if candidates else 0
    pl.set_state(pl.cell_of,os)
    return devices, pl, comps, nets, index, plan


def normalize(f, *, budget=None, until=None):
    _, pl, comps, nets, index, _ = f
    budget = ai.new_attachment_budget() if budget is None else budget
    until = perf_counter()+1 if until is None else until
    with patch('netlist2schematic.routing.orthogonal_router._find_path', side_effect=AssertionError('Astar forbidden')):
        result, report = rails.normalize_initial_rails(structured.par, pl, comps, nets,
            index, budget, until, options=RoutingOptions())
    return result, report, budget


def static(f, pl=None):
    return _static_faults(structured.par, pl or f[1], f[3], f[4], RoutingOptions(), None)


class InitialRailClearanceTests(unittest.TestCase):
    def test_passive_families_preserve_original_nodes_and_clear_real_rail_faults(self):
        for kind in ('R', 'C', 'L', 'D'):
            for rail, first in (('vdd', False), ('vcc', True), ('0', False), ('vss', True)):
                with self.subTest(kind=kind,rail=rail,first=first):
                    f=fixture(kind,rail=rail,rail_first=first)
                    before=freeze(f[1]);nodes=deepcopy(f[2]);nets=deepcopy(f[3])
                    self.assertTrue(any(v['kind']=='power_stub_body' for v in static(f)['violations']))
                    result,report,budget=normalize(f)
                    self.assertTrue(report['applied']);self.assertEqual(report['trials'],1)
                    self.assertEqual(static(f,result)['count'],0)
                    self.assertEqual(result.cell_of,f[1].cell_of)
                    self.assertEqual(result.row_of,f[1].row_of)
                    self.assertEqual(result.comp_at,f[1].comp_at)
                    self.assertEqual(result.column_x,f[1].column_x);self.assertEqual(result.row_y,f[1].row_y)
                    self.assertEqual(result.comps,nodes);self.assertEqual(f[3],nets)
                    self.assertEqual(result.rng.getstate(),f[1].rng.getstate())
                    self.assertEqual(freeze(f[1]),before);self.assertEqual(budget['candidates'],1)

    def test_voltage_and_current_source_both_pole_roles_use_real_pin_directions(self):
        for kind in ('VSRC','ISRC'):
            for rail in ('vdd','0'):
                for first in (False,True):
                    with self.subTest(kind=kind,rail=rail,first=first):
                        f=fixture(kind,rail=rail,rail_first=first);old=deepcopy(f[2])
                        result,report,_=normalize(f)
                        self.assertTrue(report['applied']);self.assertEqual(static(f,result)['count'],0)
                        self.assertEqual(result.comps,old)
                        pin='+' if first else '-'
                        self.assertEqual(result.pin_dir(f[4]['load0'],pin),'north' if rail=='vdd' else 'south')

    def test_outward_safe_seed_returns_same_object_without_hpwl_or_static_work(self):
        f=fixture();i=f[4]['load0'];f[1].orient[i]=2
        before=freeze(f[1])
        with patch.object(ai,'_checked_static',side_effect=AssertionError('unneeded static')), \
             patch('netlist2schematic.plugins.layout_shared.branch_alignment._hpwl',side_effect=AssertionError('unneeded score')):
            result,report,budget=normalize(f)
        self.assertIs(result,f[1]);self.assertEqual(freeze(result),before)
        self.assertEqual(report['stop_reason'],'already_outward');self.assertEqual(budget['candidates'],0)

    def test_unknown_supply_name_does_not_become_power_evidence(self):
        f=fixture(rail='unclassified_reference')
        result,report,budget=normalize(f)
        self.assertIs(result,f[1]);self.assertEqual(report['stop_reason'],'already_outward')
        self.assertEqual(budget['candidates'],0)

    def test_several_reversed_exits_are_one_atomic_joint_trial(self):
        f=fixture(copies=3);before=freeze(f[1])
        self.assertEqual(sum(v['kind']=='power_stub_body' for v in static(f)['violations']),3)
        result,report,budget=normalize(f)
        self.assertTrue(report['applied']);self.assertEqual(report['trials'],1)
        self.assertEqual(len(report['actions']),3);self.assertEqual(static(f,result)['count'],0)
        self.assertEqual(budget['candidates'],1);self.assertEqual(freeze(f[1]),before)

    def test_fixed_numeric_and_named_devices_are_not_rotated(self):
        for style in ('numeric','named','cell'):
            with self.subTest(style=style):
                f=fixture();i=f[4]['load0']
                if style=='cell':f[1].fixed_cells={i:f[1].cell_of[i]}
                else:f[1].fixed={i if style=='numeric' else 'load0'}
                before=freeze(f[1]);result,report,budget=normalize(f)
                self.assertIs(result,f[1]);self.assertEqual(freeze(result),before)
                self.assertEqual(report['stop_reason'],'fixed_or_active_rail_conflict')
                self.assertEqual(budget['candidates'],0)

    def test_orientation_lock_is_respected_without_adding_a_new_permission(self):
        f=fixture();pl=f[1];old=list(pl.orient)
        with patch.object(pl,'allowed_orientations',side_effect=lambda i:[old[i]]):
            result,report,budget=normalize(f)
        self.assertIs(result,pl);self.assertEqual(pl.orient,old)
        self.assertEqual(report['stop_reason'],'no_allowed_outward_orientation')
        self.assertEqual(budget['candidates'],0)

    def test_active_rail_conflict_cannot_gain_a_rotation(self):
        f=fixture();i=f[4]['q0'];f[2][i]['nodes']['S']='vdd'
        result,report,budget=normalize(f)
        self.assertIs(result,f[1]);self.assertEqual(report['stop_reason'],'fixed_or_active_rail_conflict')
        self.assertEqual(budget['candidates'],0)

    def test_custom_power_connections_are_left_to_their_existing_contract(self):
        f=fixture();f[1].power_connections=[];before=freeze(f[1])
        with patch.object(rails,'inward_pins',side_effect=AssertionError('custom rails')):
            result,report,budget=normalize(f)
        self.assertIs(result,f[1]);self.assertEqual(freeze(result),before)
        self.assertEqual(report['stop_reason'],'custom_power_connections');self.assertEqual(budget['candidates'],0)

    def test_passive_policy_does_not_gain_source_orientation_actions(self):
        f=fixture('VSRC');ds,pl,cs,nets,index,_=f
        passive=plan_for(ds,pl,cs,nets,index,'passive')
        pl=ai._install(pl,cs,index,passive);before=freeze(pl)
        result,report,budget=normalize((ds,pl,cs,nets,index,passive))
        self.assertIs(result,pl);self.assertEqual(freeze(pl),before)
        self.assertEqual(report['stop_reason'],'unmanaged_source_rail_conflict')
        self.assertEqual(budget['candidates'],0)

    def test_unrelated_real_body_collision_is_not_excused_or_partially_committed(self):
        f=fixture(copies=2);pl=f[1];i,j=f[4]['q0'],f[4]['q1']
        pl.offset_x=[0.]*len(f[2]);pl.offset_x[i]=170.;pl.offset_x[j]=-170.
        before=freeze(pl);self.assertTrue(any(v['kind']=='body_overlap' for v in static(f)['violations']))
        result,report,budget=normalize(f)
        self.assertIs(result,pl);self.assertFalse(report['applied']);self.assertEqual(freeze(pl),before)
        self.assertGreater(report['geometry_rejections'],0);self.assertLessEqual(budget['candidates'],8)

    def test_last_existing_trial_can_commit_without_obtaining_a_new_allowance(self):
        f=fixture();budget=ai.new_attachment_budget();budget['candidates']=127
        result,report,budget=normalize(f,budget=budget)
        self.assertTrue(report['applied']);self.assertEqual(budget['candidates'],128)
        self.assertEqual(budget['max_candidates'],128);self.assertEqual(budget['spent_seconds'],0.)

    def test_stage_and_finish_reservations_are_preserved(self):
        f=fixture();budget=ai.new_attachment_budget()
        budget.update(candidates=120,stage_candidate_ceiling=121,band_compaction_reserve=4)
        result,report,budget=normalize(f,budget=budget)
        self.assertTrue(report['applied']);self.assertEqual(budget['candidates'],121)
        self.assertEqual(budget['stage_candidate_ceiling'],121);self.assertEqual(budget['band_compaction_reserve'],4)
        again,second,budget=normalize(f,budget=budget)
        self.assertIs(again,f[1]);self.assertEqual(second['trials'],0);self.assertEqual(budget['candidates'],121)

    def test_zero_exhausted_or_expired_budget_does_no_geometry_work(self):
        for mode in ('zero_count','zero_time','used','expired','exhausted'):
            with self.subTest(mode=mode):
                f=fixture();budget=ai.new_attachment_budget(max_candidates=0 if mode=='zero_count' else 128)
                if mode=='used':budget['candidates']=128
                if mode=='exhausted':budget['exhausted']=True
                if mode=='zero_time':budget['seconds_limit']=0.
                until=perf_counter()-1 if mode in ('expired','zero_time') else perf_counter()+1
                before=freeze(f[1])
                with patch.object(rails,'inward_pins',side_effect=AssertionError('expired construction')):
                    result,report,_=normalize(f,budget=budget,until=until)
                self.assertIs(result,f[1]);self.assertEqual(freeze(result),before);self.assertEqual(report['trials'],0)

    def test_late_static_acceptance_is_rolled_back(self):
        f=fixture();before=freeze(f[1]);until=perf_counter()+1;late=[False]
        def checker(*args,**kwargs):late[0]=True;return True
        with patch.object(ai,'_checked_static',side_effect=checker),patch.object(rails,'perf_counter',side_effect=lambda:until+1 if late[0] else until-.5):
            result,report,budget=normalize(f,until=until)
        self.assertIs(result,f[1]);self.assertEqual(freeze(result),before)
        self.assertEqual(report['stop_reason'],'time_budget');self.assertEqual(budget['candidates'],1)

    def test_timed_out_static_oracle_keeps_original_and_consumed_trial(self):
        f=fixture();before=freeze(f[1])
        with patch.object(ai,'_checked_static',return_value=None):
            result,report,budget=normalize(f)
        self.assertIs(result,f[1]);self.assertEqual(freeze(result),before)
        self.assertEqual(report['stop_reason'],'time_budget');self.assertEqual(budget['candidates'],1)

    def test_hpwl_guard_rejects_an_orientation_before_static_work(self):
        f=fixture();before=freeze(f[1])
        with patch('netlist2schematic.plugins.layout_shared.branch_alignment._hpwl',side_effect=lambda par,pl,nets,index:100. if pl is f[1] else 200.),patch.object(ai,'_checked_static',side_effect=AssertionError('HPWL rejected')):
            result,report,budget=normalize(f)
        self.assertIs(result,f[1]);self.assertEqual(freeze(result),before)
        self.assertEqual(report['hpwl_rejections'],report['trials']);self.assertGreater(budget['candidates'],0)

    def test_failed_joint_trials_stop_at_eight_without_escaping_a_partial_state(self):
        f=fixture(copies=3);before=freeze(f[1])
        with patch.object(ai,'_checked_static',return_value=False):
            result,report,budget=normalize(f)
        self.assertIs(result,f[1]);self.assertEqual(freeze(result),before)
        self.assertEqual(report['trials'],8);self.assertEqual(budget['candidates'],8)
        self.assertEqual(report['actions'],[]);self.assertEqual(report['stop_reason'],'trial_limit')

    def test_row_column_local_offsets_and_unrelated_orientations_remain_identical(self):
        f=fixture();pl=f[1];pl.offset_x=[4.]*len(f[2]);pl.passive_local_offsets=[(0.,0.) for _ in f[2]]
        pl.passive_local_offsets[f[4]['load0']]=(6.,0.)
        old=freeze(pl);result,report,_=normalize(f)
        self.assertTrue(report['applied'])
        for k in ('cell_of','comp_at','row_of','row_y','column_x','offset_x','passive_local_offsets','fixed_rows','attachment_axes'):
            self.assertEqual(getattr(result,k),old[k])
        self.assertEqual(result.orient[f[4]['q0']],pl.orient[f[4]['q0']])

    def test_repeat_is_idempotent_and_does_not_read_any_GT_or_files(self):
        f=fixture()
        with patch.object(Path,'read_text',side_effect=AssertionError('filesystem/GT forbidden')):
            result,report,_=normalize(f)
            again,second,budget=normalize((f[0],result,*f[2:]))
        self.assertTrue(report['applied']);self.assertIs(again,result)
        self.assertEqual(second['stop_reason'],'already_outward');self.assertEqual(budget['candidates'],0)

    def test_renaming_row_order_translation_and_horizontal_mirror_preserve_capability(self):
        for reverse,mirror in ((False,False),(True,False),(True,True)):
            with self.subTest(reverse=reverse,mirror=mirror):
                f=fixture(reverse_order=reverse);ds,pl,cs,nets,index,plan=f
                rename={n:'unit_'+str(k+50) for k,n in enumerate(ds)}
                # Rename all identities and nonrail nets consistently, then
                # rebuild the ordinary plan; physical geometry stays generic.
                remap={n:deepcopy(d) for n,d in ds.items()}
                for d in remap.values():
                    for key in ('d','g','a','b'):
                        if key in d and not structured.par.power_marker_sym(d[key]):d[key]='local_'+d[key]
                cells={rename[n]:pl.cell_of[index[n]] for n in ds}
                orientations={rename[n]:pl.orient[index[n]] for n in ds}
                remap={rename[n]:d for n,d in remap.items()}
                raw,cs,ns,ix,_=convert(remap,cells,orientations)
                raw.column_x=[x+38 for x in raw.column_x];raw.row_y=[y-52 for y in raw.row_y]
                if mirror:
                    raw.set_state([(r,raw.cols-1-c) for r,c in raw.cell_of],
                        [1-o if cs[i]['symbol']=='nmos' else o for i,o in enumerate(raw.orient)])
                pm=plan_for(remap,raw,cs,ns,ix);installed=ai._install(raw,cs,ix,pm)
                changed,report,_=normalize((remap,installed,cs,ns,ix,pm))
                self.assertTrue(report['applied']);self.assertEqual(_static_faults(structured.par,changed,ns,ix,RoutingOptions(),None)['count'],0)

    def test_initializer_clears_fault_before_first_existing_structure_stage(self):
        f=fixture();events=[];before=freeze(f[1])
        original=ai._input_block_seed
        def observe(par,current,original_pl,comps,nets,index,plan,options,budget,until,report):
            events.append(not rails.inward_pins(par,current,comps))
            return original(par,current,original_pl,comps,nets,index,plan,options,budget,until,report)
        with patch.object(ai,'_input_block_seed',side_effect=observe):
            result,report=ai.initialize_attachments(f[1],f[0],*f[2:5],f[5])
        self.assertEqual(events,[True])
        after=freeze(f[1]);after.pop('attachment_budget',None);before.pop('attachment_budget',None)
        self.assertEqual(after,before)
        self.assertEqual(report['geometry_candidate_trials'],1)
        self.assertLessEqual(report['budget']['candidates'],128)
        self.assertLessEqual(report['budget']['spent_seconds'],1.)

    def test_later_initializer_rollback_also_retracts_early_orientation_adoption(self):
        f=fixture();before=freeze(f[1])
        with patch.object(ai,'_joint_topology_stage',side_effect=lambda par,current,*a,**k:(current,True)):
            result,report=ai.initialize_attachments(f[1],f[0],*f[2:5],f[5])
        self.assertIs(result,f[1])
        after=freeze(result);after.pop('attachment_budget',None);before.pop('attachment_budget',None)
        self.assertEqual(after,before)
        self.assertEqual(result.attachment_budget,report['budget'])
        self.assertFalse(report['initial_rail_clearance']['applied'])
        self.assertTrue(report['initial_rail_clearance']['rolled_back'])
        self.assertEqual(report['initial_rail_clearance']['actions'],[])
        self.assertIsNone(report['initial_rail_clearance']['static_verification']['valid'])
        self.assertTrue(report['initial_rail_clearance']['trial_static_verification']['valid'])
        self.assertEqual(report['budget']['candidates'],1)

    def test_off_and_reference_candidate_never_call_the_new_stage(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'input.cir'
            path.write_text('.model CORE nmos\nM_ANY out ctrl 0 0 CORE\nR_ANY ctrl vdd 1k\n',encoding='utf8')
            with patch.object(rails,'normalize_initial_rails',side_effect=AssertionError('off/reference changed')):
                list(structured.create_candidates(path,iterations=0,seed=7,attachment_layout='off'))
                gen=structured.create_candidates(path,iterations=0,seed=7,attachment_layout='all')
                result=next(gen);self.assertEqual(result[-1]['candidate_id'],1);gen.close()


if __name__=='__main__':unittest.main()
