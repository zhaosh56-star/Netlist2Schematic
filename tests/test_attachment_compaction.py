"""Vacated-band finish keeps row identities and shares the original ledger."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_attachment_ports import installed
from tests.test_passive_placement import mos,resistor
from netlist2schematic.plugins.attachment import attachment_compaction as compact
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.flow.run_flow import _saved_positions
from netlist2schematic.common.sa_saved import load_placement
from netlist2schematic.plugins.preflight.placement_preflight import _static_faults


def fixture(*, rows=1, vacate=True, rename=False):
    devices=dict(Mu=mos('u','gu'),Md=mos('d','gd'),Ra=resistor('d','0'))
    cells=dict(Mu=(1,2),Md=(2+rows,2),Ra=(2,4))
    if rows==2:
        devices['Rb']=resistor('d','0','C');cells['Rb']=(3,5)
    names={name:name for name in devices}
    if rename:
        names={n:'unit'+str(50-k) for k,n in enumerate(devices)}
        devices={names[n]:{k:'signal_'+v if k in ('d','g','s','a','b') and v!='0' else v
            for k,v in d.items()} for n,d in reversed(list(devices.items()))}
        cells={names[n]:v for n,v in cells.items()}
    pl,c,n,i,plan=installed(devices,cells)
    if vacate:
        cells=list(pl.cell_of)
        for name in ('Ra','Rb'):
            if name in names:
                j=i[names[name]];cells[j]=(2+rows,cells[j][1])
        pl.set_state(cells,pl.orient)
    return pl,c,n,i,plan,names


def state(pl):
    return deepcopy((pl.cell_of,pl.orient,pl.row_of,pl.comp_at,pl.column_x,pl.row_y,
        pl.attachment_axes,pl.fixed_rows,pl.attachment_plan,pl.rng.getstate()))


_UNSET=object()


def finish(f, *, static=_UNSET, budget=None, expired=False, metadata=None, options=None):
    pl,c,n,i,plan,_=f
    budget=ai.new_attachment_budget() if budget is None else budget
    compact.reserve_finish(plan,budget)
    report=dict(candidates=0,accepted=0,actions=[])
    until=perf_counter()+(-1 if expired else 1)
    args=(structured.par,pl,c,n,i,plan,options or RoutingOptions(),budget,until,report,metadata or {})
    with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar')), \
         patch.object(structured.StructuredPlacement,'propose',side_effect=AssertionError('SA')):
        if static is _UNSET:
            result=compact.finish_compaction(*args)
        elif isinstance(static,list):
            with patch.object(ai,'_checked_static',side_effect=static):
                result=compact.finish_compaction(*args)
        else:
            with patch.object(ai,'_checked_static',return_value=static):
                result=compact.finish_compaction(*args)
    return result,report,budget


class VacatedBandTests(unittest.TestCase):
    def test_reclaims_a_vacated_passive_reservation_without_changing_any_logical_state(self):
        f=fixture();pl,c,n,i,plan,names=f;before=state(pl)
        result,report,budget=finish(f)
        self.assertEqual(report['accepted'],1)
        self.assertEqual(report['band_compaction']['removed_height'],140)
        self.assertEqual(result.cell_of,pl.cell_of);self.assertEqual(result.orient,pl.orient)
        self.assertEqual((result.row_of,result.comp_at,result.column_x),(pl.row_of,pl.comp_at,pl.column_x))
        self.assertTrue(result.valid_state());self.assertEqual(state(pl),before)
        self.assertEqual(result.rng.getstate(),pl.rng.getstate())
        self.assertEqual(result.attachment_plan,plan)
        self.assertEqual(result.attachment_axes,(tuple(result.column_x),tuple(result.row_y)))

    def test_ordinary_trials_cannot_consume_the_reserved_finish_allowance(self):
        f=fixture();budget=ai.new_attachment_budget();budget['spent_seconds']=.3
        compact.reserve_finish(f[4],budget)
        self.assertEqual(budget['band_compaction_reserve'],2)
        budget['candidates']=126
        self.assertFalse(ai._has_budget(budget,perf_counter()+1))
        result,report,budget=finish(f,budget=budget)
        self.assertEqual(report['accepted'],1);self.assertEqual(budget['candidates'],127)
        self.assertEqual(budget['max_candidates'],128);self.assertEqual(budget['spent_seconds'],.3)
        self.assertTrue(budget['band_compaction_finish_done'])
        self.assertNotIn('band_compaction_reserve',budget)

    def test_control_finish_and_compaction_have_separate_slices_of_one_ledger(self):
        f=fixture();plan=deepcopy(f[4]);plan['control_nodes']={'nodes':[{}]}
        budget=ai.new_attachment_budget();compact.reserve_finish(plan,budget)
        ai._reserve_control_finish(plan,budget)
        self.assertEqual(budget['control_finish_reserve'],16)
        self.assertEqual(budget['candidate_ceiling'],110)
        budget['candidates']=110
        self.assertFalse(ai._has_budget(budget,perf_counter()+1))
        budget.pop('candidate_ceiling')
        self.assertTrue(ai._has_budget(budget,perf_counter()+1))
        budget['candidates']=126
        self.assertFalse(ai._has_budget(budget,perf_counter()+1))

    def test_occupied_reservation_is_not_removed_or_hidden_by_a_zero_metric(self):
        f=fixture(vacate=False);before=state(f[0])
        with patch.object(ai,'_checked_static',side_effect=AssertionError('no geometry trial expected')):
            result,report,budget=finish(f)
        self.assertEqual(state(result),before);self.assertEqual(report['candidates'],0)
        self.assertEqual(report['band_compaction']['stop_reason'],'no_eligible_vacated_band')

    def test_an_empty_unreserved_row_or_feedback_region_is_not_a_compaction_license(self):
        for mode in ('no_reservation','feedback_region'):
            f=fixture()
            if mode=='no_reservation':f[4]['reserved_passive_rows']=[]
            else:f[4]['preserve_feedback_rows']=True
            before=state(f[0]);result,report,budget=finish(f)
            with self.subTest(mode=mode):
                self.assertEqual(state(result),before);self.assertEqual(report['candidates'],0)

    def test_slot_and_explicit_feedback_channel_boundaries_are_protected(self):
        for mode in ('slot','near_slot','feedback_left','feedback_right'):
            f=fixture();metadata={}
            if mode=='slot':f[0].attachment_junction_rows={'channel':(2,)}
            elif mode=='near_slot':f[0].attachment_junction_rows={'channel':(3,)}
            elif mode=='feedback_left':metadata={'feedback_row_cut':1}
            else:metadata={'feedback':{'row_cut':2,'added_channel_height':32}}
            before=state(f[0]);result,report,budget=finish(f,metadata=metadata)
            with self.subTest(mode=mode):
                self.assertEqual(state(result),before);self.assertEqual(report['candidates'],0)
                self.assertTrue(report['band_compaction']['skipped'])

    def test_fixed_device_below_the_band_prevents_axis_motion(self):
        f=fixture();pl,c,n,i,plan,names=f
        pl.fixed.add(i['Md']);pl=ai._install(pl,c,i,plan)
        f=(pl,c,n,i,plan,names);before=state(pl)
        result,report,budget=finish(f)
        self.assertEqual(state(result),before);self.assertEqual(report['accepted'],0)
        self.assertTrue(all(s['reason']=='state_or_fixed_world' for s in report['band_compaction']['skipped']))

    def test_fixed_device_above_the_band_keeps_its_true_point(self):
        f=fixture();pl,c,n,i,plan,names=f
        pl.fixed.add(i['Mu']);pl=ai._install(pl,c,i,plan);point=pl.pos_of(i['Mu'])
        result,report,budget=finish((pl,c,n,i,plan,names))
        self.assertEqual(report['accepted'],1);self.assertEqual(result.pos_of(i['Mu']),point)

    def test_static_failure_or_timeout_discards_the_unfinished_trial(self):
        for response in (False,None):
            f=fixture();before=state(f[0]);result,report,budget=finish(f,static=response)
            with self.subTest(response=response):
                self.assertEqual(state(result),before);self.assertEqual(state(f[0]),before)
                self.assertEqual(report['accepted'],0)
                if response is None:
                    self.assertTrue(budget['exhausted'])
                    self.assertEqual(report['band_compaction']['stop_reason'],'time_budget')

    def test_late_positive_oracle_cannot_commit_after_the_deadline(self):
        f=fixture();before=state(f[0])
        with patch.object(compact,'perf_counter',return_value=perf_counter()+10):
            result,report,budget=finish(f,static=True)
        self.assertEqual(state(result),before);self.assertEqual(report['accepted'],0)
        self.assertEqual(report['band_compaction']['stop_reason'],'time_budget')

    def test_real_geometry_rejects_both_compressions_in_a_crowded_band(self):
        f=fixture();pl,c,n,i,plan,_=f
        pl.row_y=[60+70*r for r in range(len(pl.row_y))]
        pl.attachment_axes=tuple(pl.column_x),tuple(pl.row_y)
        self.assertEqual(_static_faults(structured.par,pl,n,i,RoutingOptions(),None)['count'],0)
        before=state(pl);result,report,budget=finish(f)
        self.assertEqual(report['accepted'],0);self.assertEqual(state(result),before)
        self.assertEqual(report['candidates'],2)
        self.assertTrue(all(s['reason']=='static_geometry' for s in report['band_compaction']['skipped']))

    def test_real_geometry_can_reject_full_pitch_and_accept_only_the_partial_shrink(self):
        f=fixture();pl,c,n,i,plan,_=f
        pl.row_y=[60+80*r for r in range(len(pl.row_y))]
        pl.attachment_axes=tuple(pl.column_x),tuple(pl.row_y)
        self.assertEqual(_static_faults(structured.par,pl,n,i,RoutingOptions(),None)['count'],0)
        result,report,budget=finish(f)
        self.assertEqual(report['accepted'],1);self.assertEqual(report['candidates'],2)
        self.assertEqual(report['actions'][0]['removed_height'],40)
        self.assertEqual(report['band_compaction']['skipped'][0]['reason'],'static_geometry')
        self.assertEqual(_static_faults(structured.par,result,n,i,RoutingOptions(),None)['count'],0)

    def test_more_than_one_band_inherits_the_last_accepted_axis(self):
        f=fixture(rows=2);before=state(f[0]);result,report,budget=finish(f)
        self.assertEqual(report['accepted'],2)
        self.assertEqual(report['actions'][1]['before_axis'],report['actions'][0]['after_axis'])
        self.assertEqual(result.row_y,report['actions'][-1]['after_axis'])
        self.assertEqual(state(f[0]),before);self.assertTrue(result.valid_state())
        self.assertLessEqual(budget['candidates'],128)

    def test_timeout_after_one_accept_keeps_that_safe_transaction(self):
        f=fixture(rows=2);result,report,budget=finish(f,static=[True,None])
        self.assertEqual(report['accepted'],1)
        self.assertEqual(result.row_y,report['actions'][0]['after_axis'])
        self.assertEqual(report['band_compaction']['stop_reason'],'time_budget')
        self.assertTrue(result.valid_state())

    def test_exhausted_ledger_or_deadline_never_resets_or_retries_without_limit(self):
        for mode in ('deadline','candidates'):
            f=fixture();budget=ai.new_attachment_budget()
            if mode=='candidates':budget['candidates']=128
            before=state(f[0]);result,report,budget=finish(f,budget=budget,expired=mode=='deadline')
            with self.subTest(mode=mode):
                self.assertEqual(state(result),before);self.assertEqual(report['accepted'],0)
                self.assertEqual(report['candidates'],0);self.assertLessEqual(budget['candidates'],128)

    def test_renaming_reordering_translation_and_mirror_preserve_the_axis_operation(self):
        base=fixture();expected,_,_=finish(base)
        for renamed,shift,mirrored in ((True,0,False),(False,37,True),(True,37,True)):
            f=fixture(rename=renamed);pl,c,n,i,plan,_=f
            pl.row_y=[y+shift for y in pl.row_y]
            if mirrored:
                pl.column_x=[3000-x for x in reversed(pl.column_x)]
                cells=[(r,len(pl.column_x)-1-col) for r,col in pl.cell_of]
                pl.set_state(cells,[1-x if comp['symbol'] in ('nmos','pmos') else x
                    for x,comp in zip(pl.orient,c)])
            pl.attachment_axes=tuple(pl.column_x),tuple(pl.row_y)
            result,report,budget=finish(f)
            with self.subTest(renamed=renamed,shift=shift,mirrored=mirrored):
                self.assertEqual(report['accepted'],1)
                self.assertEqual(result.row_y,[y+shift for y in expected.row_y])

    def test_custom_grid_rounds_axis_changes_without_changing_original_pin_geometry(self):
        f=fixture();pl,c,n,i,plan,_=f
        result,report,budget=finish(f,options=RoutingOptions(grid_step=8))
        self.assertEqual(report['accepted'],1)
        self.assertEqual(report['actions'][0]['removed_height']%8,0)
        for j,comp in enumerate(c):
            for pin in comp['nodes']:
                old=pl.pin_world(j,pin);new=result.pin_world(j,pin)
                self.assertEqual(old[0]-pl.pos_of(j)[0],new[0]-result.pos_of(j)[0])
                self.assertEqual(old[1]-pl.pos_of(j)[1],new[1]-result.pos_of(j)[1])

    def test_saved_coordinates_and_all_visible_pins_replay_without_placement_or_astar(self):
        f=fixture();result,report,budget=finish(f);pl,c,n,i,plan,_=f
        self.assertEqual(report['accepted'],1)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source.cir'
            source.write_text('* topology fixture\n.model n NMOS\nMu u gu 0 0 n\nMd d gd 0 0 n\nRa d 0 1k\n',encoding='utf8')
            payload=dict(source_netlist=str(source),source_sha256=sha256(source.read_bytes()).hexdigest(),
                mode='layered',placement=_saved_positions(result,c),
                layout_grid=dict(column_x=list(result.column_x),row_y=list(result.row_y)),
                validation=dict(valid=False,reason='geometry_replay_only'))
            path=root/'placement.json';path.write_text(json.dumps(payload),encoding='utf8')
            with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar')), \
                 patch.object(structured,'_anneal',side_effect=AssertionError('SA')):
                _,_,_,replay,rc,rn,ri=load_placement(path)
            for name,j in i.items():
                self.assertEqual(result.pos_of(j),replay.pos_of(ri[name]))
                for pin in c[j]['nodes']:
                    self.assertEqual(result.pin_world(j,pin),replay.pin_world(ri[name],pin))


if __name__=='__main__':
    unittest.main()
