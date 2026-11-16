"""A shared bias source follows a proven reference access, not gate fanout."""
from copy import deepcopy
from pathlib import Path
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_attachment_sources import build, fixture, freeze
from tests.test_passive_placement import mos, resistor, source
from netlist2schematic.plugins.attachment.attachment_sources import (
    source_group_anchor, source_group_trials, source_axis_trials, measure_source_groups)
from netlist2schematic.plugins.attachment.attachment_integration import _mirror_orientation
from netlist2schematic.layout.structured_placement import par
from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
from netlist2schematic.routing.routing_rules import RoutingOptions


def devices(kind='nmos', reverse=False):
    def active(d, g):
        if kind in ('nmos', 'pmos'):
            rail='vdd' if kind=='pmos' else '0'
            comp=mos(d,g,rail); comp.update(type=kind.upper(),model=kind.upper(),bulk=rail)
            return comp
        return dict(cls='BJT',type=kind.upper(),model=kind.upper(),c=d,b=g,e='vdd' if kind=='pnp' else '0')
    rail='0' if kind in ('pmos','pnp') else 'vdd'
    return dict(reference=active('bias','bias'),out_a=active('oa','bias'),out_b=active('ob','bias'),
        bias=source('ISRC','bias' if reverse else rail,rail if reverse else 'bias'))


def make(kind='nmos',reverse=False):
    return fixture(devices(kind,reverse),dict(reference=(2,1),out_a=(2,5),out_b=(3,7),bias=(0,7)))


class ReferenceSourceTests(unittest.TestCase):
    def test_MOS_BJT_and_polarity_duals_keep_neutral_role_but_use_actual_conduction_port(self):
        for kind in ('nmos','pmos','npn','pnp'):
            pl,cs,nets,index,model=make(kind); group,=model['groups']
            pin='D' if kind in ('nmos','pmos') else 'C'
            self.assertEqual('neutral_junction_source',group['kind'])
            self.assertFalse(group['functional_role_claimed'])
            self.assertEqual(['out_a','out_b','reference'],group['host_members'])
            anchor=source_group_anchor(pl,cs,index,group)
            self.assertEqual(list(pl.pin_world(index['reference'],pin)),anchor['point'])
            self.assertEqual('unique_diode_reference',anchor['selection'])
            self.assertEqual(4,len(anchor['host_points']))
            self.assertNotEqual(anchor['junction_center'],anchor['point'])

    def test_gate_fanout_movement_cannot_pull_anchor_away_from_reference(self):
        pl,cs,nets,index,model=make(); group=model['groups'][0]
        before=source_group_anchor(pl,cs,index,group)
        cells=list(pl.cell_of);cells[index['out_a']]=(2,6);cells[index['out_b']]=(3,6)
        pl.set_state(cells,pl.orient)
        after=source_group_anchor(pl,cs,index,group)
        self.assertEqual(before['point'],after['point'])
        self.assertNotEqual(before['junction_center'],after['junction_center'])

    def test_reversing_source_poles_preserves_host_evidence_and_original_connections(self):
        anchors=[]
        for reverse in (False,True):
            ds=devices(reverse=reverse);original=deepcopy(ds)
            pl,cs,nets,index,model=make(reverse=reverse);group=model['groups'][0]
            anchors.append(source_group_anchor(pl,cs,index,group)['point'])
            self.assertEqual('+' if reverse else '-',group['signal_pin'])
            self.assertEqual(ds,original)
            self.assertEqual(group['source_pins'],cs[index['bias']]['nodes'])
        self.assertEqual(*anchors)

    def test_two_references_extra_conduction_or_non_diode_access_are_ambiguous(self):
        for mode in ('second_diode','other_drain','non_diode','source_terminal'):
            ds=devices()
            if mode=='second_diode':ds['out_a']['d']='bias'
            elif mode=='other_drain':ds['out_a'].update(d='bias',g='separate')
            elif mode=='non_diode':ds['reference']['g']='separate'
            else:ds['extra']=mos('extra','extra_gate','bias')
            model,_,_,_=build(ds)
            self.assertNotIn('reference_anchor',model['groups'][0],mode)

    def test_passive_taps_other_sources_and_voltage_drive_keep_original_median_rule(self):
        for mode in ('passive','source','voltage'):
            ds=devices()
            if mode=='passive':ds['tap']=resistor('bias','0')
            elif mode=='source':ds['another']=source('ISRC','bias','0')
            else:ds['bias']=source('VSRC','bias','0')
            cells=dict(reference=(2,1),out_a=(2,5),out_b=(3,7),bias=(0,7),tap=(4,8),another=(0,8))
            pl,cs,nets,index,model=fixture(ds,{n:cells[n] for n in ds})
            for group in model['groups']:
                self.assertNotIn('reference_anchor',group)
                self.assertNotIn('selection',source_group_anchor(pl,cs,index,group))

    def test_renaming_and_netlist_reordering_keep_geometrically_equivalent_evidence(self):
        original=devices();names=dict(reference='Z9',out_a='A3',out_b='B2',bias='Q1')
        changed={names[n]:{k:('node_'+v if k in ('d','g','s','a','b','p','n') and v not in ('0','vdd') else v)
            for k,v in d.items()} for n,d in reversed(list(original.items()))}
        first=make();second=fixture(changed,{names[n]:first[0].cell_of[first[3][n]] for n in original})
        for pl,cs,nets,index,model in (first,second):
            self.assertEqual(1,len(model['groups']))
        a=source_group_anchor(first[0],first[1],first[3],first[4]['groups'][0])
        b=source_group_anchor(second[0],second[1],second[3],second[4]['groups'][0])
        self.assertEqual(a['point'],b['point']);self.assertEqual(a['preferred_direction'],b['preferred_direction'])

    def test_horizontal_mirror_uses_mirrored_reference_pin(self):
        pl,cs,nets,index,model=make();group=model['groups'][0]
        before=source_group_anchor(pl,cs,index,group)
        pl.set_state([(r,pl.cols-1-c) for r,c in pl.cell_of],[_mirror_orientation(pl,i) for i in range(len(cs))])
        after=source_group_anchor(pl,cs,index,group)
        self.assertEqual(list(pl.pin_world(index['reference'],'D')),after['point'])
        self.assertNotEqual(before['point'][0],after['point'][0])

    def test_translation_and_local_reference_offset_are_reflected_in_real_anchor(self):
        pl,cs,nets,index,model=make();group=model['groups'][0]
        before=source_group_anchor(pl,cs,index,group)
        pl.column_x=[x+37 for x in pl.column_x];pl.row_y=[y-53 for y in pl.row_y]
        after=source_group_anchor(pl,cs,index,group)
        self.assertEqual([before['point'][0]+37,before['point'][1]-53],after['point'])
        pl.offset_x=list(getattr(pl,'offset_x',[0.]*len(cs)));pl.offset_x[index['reference']]+=12
        shifted=source_group_anchor(pl,cs,index,group)
        self.assertEqual(after['point'][0]+12,shifted['point'][0])

    def test_contract_tampering_and_wrong_source_poles_are_not_silent_fallbacks(self):
        pl,cs,nets,index,model=make();group=model['groups'][0]
        for mode in ('symbol','control','fanout'):
            changed=deepcopy(cs)
            if mode=='symbol':changed[index['reference']]['symbol']='pmos'
            elif mode=='control':changed[index['reference']]['nodes']['G']='wrong'
            else:changed[index['out_a']]['nodes']['G']='wrong'
            self.assertIsNone(source_group_anchor(pl,changed,index,group))
            self.assertFalse(measure_source_groups(pl,changed,nets,index,model)['measurable'])
        changed=deepcopy(cs);changed[index['bias']]['nodes']['+']='wrong'
        self.assertFalse(measure_source_groups(pl,changed,nets,index,model)['measurable'])

    def test_neutral_reference_does_not_gain_tail_centering_or_new_axis_freedom(self):
        pl,cs,nets,index,model=make()
        self.assertEqual([],list(source_axis_trials(pl,cs,index,model)))

    def test_existing_bounded_source_proposals_improve_reference_access_without_moving_hosts(self):
        pl,cs,nets,index,model=make();before=freeze(pl)
        original=measure_source_groups(pl,cs,nets,index,model)['source_distance']
        found=[]
        for trial,action in source_group_trials(pl,cs,nets,index,model,max_candidates=24):
            self.assertEqual((pl.column_x,pl.row_y),(trial.column_x,trial.row_y))
            for n in ('reference','out_a','out_b'):
                self.assertEqual((pl.pos_of(index[n]),pl.orient[index[n]]),(trial.pos_of(index[n]),trial.orient[index[n]]))
            self.assertEqual(len(trial.cell_of),len(set(trial.cell_of)))
            if measure_source_groups(trial,cs,nets,index,model)['source_distance']<original:
                found.append(_static_faults(par,trial,nets,index,RoutingOptions(),None)['count']==0)
        self.assertEqual(before,freeze(pl));self.assertTrue(any(found))

    def test_fixed_source_expired_budget_and_zero_candidates_still_skip(self):
        pl,cs,nets,index,model=make();pl.fixed.add(index['bias'])
        self.assertEqual([],list(source_group_trials(pl,cs,nets,index,model)))
        pl.fixed.clear()
        self.assertEqual([],list(source_group_trials(pl,cs,nets,index,model,deadline=perf_counter()-1)))
        self.assertEqual([],list(source_group_trials(pl,cs,nets,index,model,max_candidates=0)))

    def test_source_anchor_and_proposals_never_read_GT_or_call_search(self):
        pl,cs,nets,index,model=make()
        with patch.object(Path,'read_text',side_effect=AssertionError('no files')),patch(
                'netlist2schematic.routing.orthogonal_router.route_scene',side_effect=AssertionError('no Astar')):
            self.assertIsNotNone(source_group_anchor(pl,cs,index,model['groups'][0]))
            self.assertTrue(list(source_group_trials(pl,cs,nets,index,model,max_candidates=4)))


if __name__=='__main__':unittest.main()
