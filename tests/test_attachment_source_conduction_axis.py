"""Neutral source axes: real ports, shared budget, no SA changes or Astar."""
from copy import deepcopy
from pathlib import Path
from time import perf_counter
import unittest
from unittest.mock import patch

from netlist2schematic.plugins.attachment import attachment_source_axis as axes
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment.attachment_sources import (
    build_source_groups, measure_source_groups, source_axis_trials)
from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
from tests.test_attachment_sources import fixture, freeze, tail_devices
from tests.test_attachment_sources_e2e import plan_for
from tests.test_passive_placement import mos, resistor, source


def devices(kind='nmos', reverse=False, listener=True):
    pin = 'D' if kind in ('nmos', 'pmos') else 'C'
    geometry = structured.par.GEO_DOC[kind]['orientations'][0]
    direction = next(p['direction'] for p in geometry['pins'] if p['name']==pin)
    rail = 'vdd' if direction=='north' else '0'
    def active(output, control):
        ground = 'vdd' if kind in ('pmos','pnp') else '0'
        if kind in ('nmos','pmos'):
            row = mos(output, control, ground)
            row.update(type=kind.upper(),model=kind.upper(),bulk=ground)
            return row
        return dict(cls='BJT',type=kind.upper(),c=output,b=control,e=ground,model=kind.upper())
    poles = (rail,'hub') if direction=='north' else ('hub',rail)
    if reverse:poles=poles[::-1]
    result = dict(host=active('hub','input'),drive=source('ISRC',*poles))
    if listener:result['listener']=active('other','hub')
    return result, direction


def make(kind='nmos', reverse=False, listener=True, offset=24):
    ds,direction=devices(kind,reverse,listener)
    cells=dict(host=(2,3),drive=(0 if direction=='north' else 4,3),listener=(2,0))
    pl,cs,nets,index,model=fixture(ds,{n:cells[n] for n in ds})
    pl.orient[index['drive']]=2 if reverse else 0
    pl.offset_x=[0.0]*len(cs);pl.offset_x[index['drive']]=offset
    return ds,pl,cs,nets,index,model


def installed(**kwargs):
    ds,pl,cs,nets,index,model=make(**kwargs)
    plan=plan_for(ds,pl,cs,nets,index)
    pl=ai._install(pl,cs,index,plan)
    return ds,pl,cs,nets,index,plan


def finish(f, *, candidates=8, safe='real', expired=False, hpwl_limit=None):
    ds,pl,cs,nets,index,plan=f
    budget=ai.new_attachment_budget(max_candidates=candidates)
    report=dict(candidates=0,accepted=0,actions=[])
    until=perf_counter()+(-1 if expired else 1)
    kwargs=dict(hpwl_limit=_hpwl(structured.par,pl,nets,index)*1.15 if hpwl_limit is None else hpwl_limit)
    with patch('netlist2schematic.routing.orthogonal_router.route_scene',side_effect=AssertionError('Astar')):
        if safe=='real':
            result=axes.finish_source_axes(structured.par,pl,cs,nets,index,plan,RoutingOptions(),budget,until,report,**kwargs)
        else:
            with patch.object(ai,'_checked_static',return_value=safe):
                result=axes.finish_source_axes(structured.par,pl,cs,nets,index,plan,RoutingOptions(),budget,until,report,**kwargs)
    return result,report,budget


class SourceConductionAxisTests(unittest.TestCase):
    def test_all_MOS_BJT_polarities_and_source_pole_orders_use_actual_ports(self):
        for kind in ('nmos','pmos','npn','pnp'):
            for reverse in (False,True):
                with self.subTest(kind=kind,reverse=reverse):
                    ds,pl,cs,nets,index,model=make(kind,reverse)
                    group=model['groups'][0];before=freeze(pl)
                    anchor=axes.conduction_anchor(pl,cs,index,group)
                    self.assertIsNotNone(anchor)
                    trial,action=next(axes.axis_trials(pl,cs,index,group))
                    self.assertEqual(trial.pin_world(index['drive'],group['signal_pin'])[0],anchor['point'][0])
                    self.assertEqual(action['axis_error_after'],0)
                    self.assertEqual(group['kind'],'neutral_junction_source')
                    self.assertFalse(group['functional_role_claimed'])
                    self.assertEqual(freeze(pl),before)
                    self.assertEqual((trial.cell_of,trial.orient,trial.row_of,trial.comp_at,trial.column_x,trial.row_y),
                                     (pl.cell_of,pl.orient,pl.row_of,pl.comp_at,pl.column_x,pl.row_y))
                    for n in ds:
                        if n!='drive':self.assertEqual(trial.pos_of(index[n]),pl.pos_of(index[n]))

    def test_control_fanout_cannot_pull_the_conduction_target_to_the_median(self):
        ds,pl,cs,nets,index,model=make();group=model['groups'][0]
        before=axes.conduction_anchor(pl,cs,index,group)
        cells=list(pl.cell_of);cells[index['listener']]=(2,1);pl.set_state(cells,pl.orient)
        after=axes.conduction_anchor(pl,cs,index,group)
        self.assertEqual(before['point'],after['point'])

    def test_old_junction_score_definition_is_not_replaced_by_the_new_axis(self):
        ds,pl,cs,nets,index,model=make(offset=-24);group=model['groups'][0]
        before=measure_source_groups(pl,cs,nets,index,model)
        trial,_=next(axes.axis_trials(pl,cs,index,group))
        after=measure_source_groups(trial,cs,nets,index,model)
        self.assertGreater(after['source_distance'],before['source_distance'])
        self.assertNotIn('selection',before['records'][0]['anchor'])
        f=installed(offset=-24);result,report,budget=finish(f)
        self.assertEqual(report['source_conduction_axis']['accepted'],1)
        self.assertEqual(result.pin_world(f[4]['drive'],f[5]['source_groups'][0]['signal_pin'])[0],
                         result.pin_world(f[4]['host'],'D')[0])

    def test_fixed_source_ids_and_fixed_cells_never_change(self):
        for mode in ('index','name','cell'):
            ds,pl,cs,nets,index,model=make();i=index['drive']
            if mode=='index':pl.fixed.add(i)
            elif mode=='name':pl.fixed.add('drive')
            else:pl.fixed_cells[i]=pl.cell_of[i]
            before=freeze(pl)
            self.assertEqual([],list(axes.axis_trials(pl,cs,index,model['groups'][0])))
            self.assertEqual(freeze(pl),before)

    def test_multiple_conduction_hosts_other_sources_passive_taps_and_voltage_drive_skip(self):
        for mode in ('drain','source','passive','voltage','emitter'):
            ds,_=devices()
            if mode=='drain':ds['listener']['d']='hub'
            elif mode=='source':ds['another']=source('ISRC','vdd','hub')
            elif mode=='passive':ds['tap']=resistor('hub','0')
            elif mode=='voltage':ds['drive']['cls']='VSRC'
            else:ds['listener']['s']='hub'
            cells={n:cell for n,cell in dict(host=(2,3),drive=(0,3),listener=(2,0),another=(0,6),tap=(4,6)).items() if n in ds}
            pl,cs,nets,index,model=fixture(ds,cells)
            for group in model['groups']:
                self.assertIsNone(axes.conduction_anchor(pl,cs,index,group),mode)
                self.assertEqual([],list(axes.axis_trials(pl,cs,index,group)),mode)

    def test_stale_source_host_or_fanout_incidence_is_not_silently_accepted(self):
        ds,pl,cs,nets,index,model=make();group=model['groups'][0]
        for who,pin in (('drive','+'),('host','D'),('listener','G')):
            changed=deepcopy(cs);changed[index[who]]['nodes'][pin]='wrong'
            self.assertIsNone(axes.conduction_anchor(pl,changed,index,group))

    def test_nonfacing_horizontal_and_wrong_side_sources_are_not_folded_back(self):
        for mode in ('flip','horizontal','wrong_side'):
            ds,pl,cs,nets,index,model=make();group=model['groups'][0];i=index['drive']
            if mode=='flip':pl.orient[i]=2
            elif mode=='horizontal':pl.orient[i]=1
            else:
                cells=list(pl.cell_of);cells[i]=(2,5);pl.set_state(cells,pl.orient)
            self.assertIsNone(axes.conduction_anchor(pl,cs,index,group),mode)

    def test_grid_clipping_and_already_aligned_state_are_stable(self):
        ds,pl,cs,nets,index,model=make();i=index['drive']
        cells=list(pl.cell_of);cells[i]=(0,0);pl.set_state(cells,pl.orient)
        trial,action=next(axes.axis_trials(pl,cs,index,model['groups'][0],grid_step=8))
        self.assertLessEqual(abs(trial.offset_x[i]),60)
        self.assertEqual(trial.offset_x[i]%8,0)
        self.assertLess(action['axis_error_after'],action['axis_error_before'])
        ds,pl,cs,nets,index,model=make();trial,_=next(axes.axis_trials(pl,cs,index,model['groups'][0]))
        self.assertEqual([],list(axes.axis_trials(trial,cs,index,model['groups'][0])))

    def test_translation_and_horizontal_mirror_preserve_the_actual_axis(self):
        for transform in ('translate','mirror'):
            ds,pl,cs,nets,index,model=make()
            if transform=='translate':
                pl.column_x=[x+37 for x in pl.column_x];pl.row_y=[y-43 for y in pl.row_y]
            else:
                offsets=[-v for v in pl.offset_x]
                pl.set_state([(r,pl.cols-1-c) for r,c in pl.cell_of],
                             [ai._mirror_orientation(pl,i) for i in range(len(cs))])
                pl.offset_x=offsets
            group=model['groups'][0];trial,_=next(axes.axis_trials(pl,cs,index,group))
            anchor=axes.conduction_anchor(trial,cs,index,group)
            self.assertEqual(trial.pin_world(index['drive'],group['signal_pin'])[0],anchor['point'][0])

    def test_instance_and_net_renaming_and_reorder_do_not_change_geometric_result(self):
        ds,pl,cs,nets,index,model=make();names={'host':'Z8','drive':'K3','listener':'A7'}
        renamed={names[n]:{k:('node_'+v if k in ('d','g','s','bulk','c','b','e','p','n') and v not in ('0','vdd') else v)
                           for k,v in row.items()} for n,row in reversed(list(ds.items()))}
        other=fixture(renamed,{names[n]:pl.cell_of[index[n]] for n in ds})
        alt,c2,n2,i2,m2=other;alt.offset_x=[0.0]*len(c2);alt.offset_x[i2[names['drive']]]=24
        first,_=next(axes.axis_trials(pl,cs,index,model['groups'][0]))
        second,_=next(axes.axis_trials(alt,c2,i2,m2['groups'][0]))
        for n in ds:self.assertEqual(first.pos_of(index[n]),second.pos_of(i2[names[n]]))

    def test_axis_analysis_and_trials_never_read_files_or_call_Astar(self):
        ds,pl,cs,nets,index,model=make()
        with patch.object(Path,'read_text',side_effect=AssertionError('file/GT access')),patch(
                'netlist2schematic.routing.orthogonal_router.route_scene',side_effect=AssertionError('Astar')):
            self.assertIsNotNone(axes.conduction_anchor(pl,cs,index,model['groups'][0]))
            self.assertTrue(list(axes.axis_trials(pl,cs,index,model['groups'][0])))

    def test_tail_axis_and_legacy_source_score_are_unchanged(self):
        pl,cs,nets,index,model=fixture(tail_devices(),dict(a=(1,2),b=(1,5),bias=(3,4),load=(0,0)))
        before=freeze(pl);trial,action=next(source_axis_trials(pl,cs,index,model))
        self.assertEqual(action['kind'],'source_tail_axis')
        self.assertIsNone(axes.conduction_anchor(pl,cs,index,model['groups'][0]))
        self.assertEqual([],list(axes.axis_trials(pl,cs,index,model['groups'][0])))
        self.assertEqual(freeze(pl),before)

    def test_real_static_oracle_accepts_one_axis_without_changing_the_input(self):
        f=installed();before=freeze(f[1]);nodes=deepcopy(f[2])
        result,report,budget=finish(f)
        self.assertEqual(report['source_conduction_axis']['accepted'],1)
        self.assertEqual(report['accepted'],1);self.assertEqual(budget['candidates'],1)
        self.assertEqual(freeze(f[1]),before);self.assertEqual(f[2],nodes)
        self.assertTrue(result.valid_state())

    def test_legacy_placement_without_offset_attribute_initializes_only_the_trial(self):
        f=installed(offset=0);pl=f[1]
        del pl.offset_x
        before=freeze(pl)
        result,report,budget=finish(f)
        self.assertEqual(report['accepted'],1)
        self.assertFalse(hasattr(pl,'offset_x'))
        self.assertEqual(freeze(pl),before)
        self.assertTrue(hasattr(result,'offset_x'))
        group=f[5]['source_groups'][0]
        anchor=axes.conduction_anchor(result,f[2],f[4],group)
        self.assertEqual(result.pin_world(f[4]['drive'],group['signal_pin'])[0],anchor['point'][0])

    def test_expired_or_zero_shared_budget_does_not_open_an_independent_budget(self):
        for kwargs in (dict(candidates=0),dict(expired=True)):
            f=installed();before=freeze(f[1])
            with patch.object(axes,'conduction_anchor',side_effect=AssertionError('expired work')):
                result,report,budget=finish(f,**kwargs)
            self.assertIs(result,f[1]);self.assertEqual(freeze(result),before)
            self.assertEqual(budget['candidates'],0)
            self.assertEqual(report['source_conduction_axis']['stop_reason'],'shared_budget')

    def test_failed_static_check_or_deadline_rolls_back_the_complete_trial(self):
        for outcome in (False,None):
            f=installed();before=freeze(f[1]);result,report,budget=finish(f,safe=outcome)
            self.assertIs(result,f[1]);self.assertEqual(freeze(result),before)
            self.assertEqual(report['accepted'],0)
            self.assertEqual(budget['candidates'],1)
            if outcome is None:self.assertTrue(budget['exhausted'])

    def test_hpwl_guard_rejects_even_a_geometrically_possible_alignment(self):
        f=installed();before=freeze(f[1]);result,report,budget=finish(f,hpwl_limit=0)
        self.assertIs(result,f[1]);self.assertEqual(freeze(result),before)
        self.assertEqual(report['source_conduction_axis']['hpwl_rejections'],1)

    def test_multiple_sources_commit_from_the_latest_state(self):
        ds,_=devices(listener=False)
        ds['other_host']=mos('second','second_input');ds['other_drive']=source('ISRC','vdd','second')
        pl,cs,nets,index,model=fixture(ds,dict(host=(2,3),drive=(0,3),other_host=(2,6),other_drive=(0,6)))
        pl.offset_x=[0.0]*len(cs)
        for n in ('drive','other_drive'):pl.offset_x[index[n]]=24
        plan=plan_for(ds,pl,cs,nets,index);pl=ai._install(pl,cs,index,plan)
        result,report,budget=finish((ds,pl,cs,nets,index,plan))
        self.assertEqual(report['accepted'],2)
        for group in plan['source_groups']:
            anchor=axes.conduction_anchor(result,cs,index,group)
            self.assertEqual(result.pin_world(index[group['device']],group['signal_pin'])[0],anchor['point'][0])
        limited,r,b=finish((ds,pl,cs,nets,index,plan),candidates=1)
        self.assertEqual(r['accepted'],1);self.assertEqual(b['candidates'],1)

    def test_final_stage_observes_the_latest_host_after_shared_control_refresh(self):
        ds,pl,cs,nets,index,plan=installed();calls=[]
        def refresh(par,current,original,comps,nets,index,plan,options,budget,until,report,checkpoint,**kwargs):
            calls.append('refresh');trial=deepcopy(current)
            trial.offset_x[index['host']]+=10
            return trial
        original_finish=axes.finish_source_axes
        def wrapped(*args,**kwargs):
            calls.append('source_axis');return original_finish(*args,**kwargs)
        metadata=dict(candidate_id='2',layout_attachments=dict(plan=plan,budget=ai.new_attachment_budget()))
        with patch('netlist2schematic.plugins.attachment.attachment_sources.source_group_trials',return_value=iter(())),patch(
                'netlist2schematic.plugins.attachment.attachment_sources.source_axis_trials',return_value=iter(())),patch.object(
                ai,'_refresh_shared_control_hosts',side_effect=refresh),patch.object(axes,'finish_source_axes',side_effect=wrapped):
            result,report=ai.refine_attachments(structured.par,pl,cs,nets,index,metadata,options=RoutingOptions())
        self.assertEqual(calls,['refresh','source_axis'])
        group=plan['source_groups'][0];anchor=axes.conduction_anchor(result,cs,index,group)
        self.assertEqual(result.pin_world(index['drive'],group['signal_pin'])[0],anchor['point'][0])
        self.assertEqual(report['layout_attachments']['refinement']['source_conduction_axis']['accepted'],1)


if __name__=='__main__':unittest.main()
