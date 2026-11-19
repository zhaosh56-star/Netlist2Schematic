"""Relative display tiers follow real correspondence, never instance names."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.test_branch_levels import mos, bjt, passive, current
from tests.test_series_alignment import PAR
from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.plugins.columns.branch_rows import plan_branch_correspondence, compatible_rows


def mirror(kind='MOS'):
    make = mos if kind == 'MOS' else bjt
    devices = dict(reference=make('bias','0','bias'), output=make('join','0','bias'),
                   cascode=make('port','join','cascode_control'))
    return devices, dict(reference=0,output=1,cascode=0)


def stacks(diode=False):
    first, second = ('ref_port','ref_join') if diode else ('control_a','control_b')
    devices = dict(load=mos('main_port','vdd','load_control','PMOS'),
        upper=mos('main_port','main_join',first),lower=mos('main_join','0',second),
        ref_upper=mos('ref_port','ref_join',first),ref_lower=mos('ref_join','0',second))
    return devices, dict(load=0,upper=1,lower=2,ref_upper=0,ref_lower=1)


def run_plan(devices, rows, *, records=None, **kwargs):
    document=to_document(devices);comps,nets=document['components'],document['nets']
    index={c['id']:i for i,c in enumerate(comps)}
    result,report=plan_branch_correspondence(PAR,devices,comps,nets,index,[rows[c['id']] for c in comps],
        analyze_structures(devices) if records is None else records,**kwargs)
    return {n:result[i] for n,i in index.items()},report


class CorrespondingBranchEvidenceTests(unittest.TestCase):
    def assert_safe(self,before,after,report):
        for order in report['strict_orders']:
            self.assertLess(after[order['upper']],after[order['lower']])
        for a,b in report['preserved_same_rows']:
            self.assertEqual(after[a],after[b])
        for column in report['columns']:
            deltas={after[n]-before[n] for n in column['members']}
            self.assertEqual(len(deltas),1)
            if column['locked_reason']:self.assertEqual(deltas,{0})
        self.assertEqual(report['electrical_edges_added'],[])
        json.dumps(report,allow_nan=False)

    def test_unique_mirror_reference_follows_longer_mos_or_bjt_column(self):
        for kind in ('MOS','BJT'):
            with self.subTest(kind=kind):
                devices,rows=mirror(kind);old=deepcopy(devices);levels=compute_levels(devices)
                result,report=run_plan(devices,rows)
                self.assertEqual(result,dict(reference=1,output=1,cascode=0))
                self.assertEqual(report['actions'][0]['members'],['reference'])
                self.assertEqual(devices,old);self.assertEqual(compute_levels(devices),levels)
                self.assert_safe(rows,result,report)

    def test_repeated_stack_moves_as_a_whole_with_two_control_roles(self):
        for diode in (False,True):
            devices,rows=stacks(diode);result,report=run_plan(devices,rows)
            self.assertEqual(result,dict(load=0,upper=1,lower=2,ref_upper=1,ref_lower=2))
            self.assertEqual(report['actions'][0]['members'],['ref_upper','ref_lower'])
            self.assertTrue(any(r['kind']=='corresponding_stack' for r in report['relations']))
            self.assert_safe(rows,result,report)

    def test_same_physical_pair_is_not_counted_twice_for_mirror_and_stack(self):
        devices,rows=stacks(True);result,report=run_plan(devices,rows)
        pair=next(r for r in report['relations'] if {r['a'],r['b']}=={'ref_lower','lower'})
        self.assertEqual(set(pair['kinds']),{'current_mirror','corresponding_stack'})
        self.assertEqual(report['before_error'],2);self.assertEqual(report['after_error'],0)

    def test_shared_gate_without_role_or_repeated_stack_is_insufficient(self):
        devices,rows=stacks()
        for device in devices.values():device['g']='clock'
        result,report=run_plan(devices,rows)
        self.assertEqual(result,rows);self.assertEqual(report['relations'],[])

    def test_shared_gate_on_complementary_devices_does_not_collapse_inverter(self):
        devices=dict(p=mos('out','vdd','control','PMOS'),n=mos('out','0','control'))
        rows=dict(p=0,n=1);result,report=run_plan(devices,rows)
        self.assertEqual(result,rows);self.assertEqual(report['relations'],[])

    def test_multi_output_different_target_rows_are_ambiguous(self):
        devices,rows=mirror()
        devices.update(extra=mos('extra_join','0','bias'),prefix=mos('p','mid','p_control'),
                       middle=mos('mid','extra_join','m_control'))
        rows.update(extra=2,prefix=0,middle=1)
        result,report=run_plan(devices,rows)
        self.assertEqual(result,rows)
        self.assertTrue(any(r['reason']=='ambiguous_target_rows' for r in report['skipped']))
        self.assert_safe(rows,result,report)

    def test_multi_output_tail_and_output_roles_must_agree_even_with_short_tail_column(self):
        for aligned in (False,True):
            with self.subTest(aligned=aligned):
                devices,rows=mirror()
                devices.update(tail=mos('tail_node','0','bias'),
                    input_a=mos('da','tail_node','input_a'),input_b=mos('db','tail_node','input_b'))
                rows.update(tail=1 if aligned else 2,input_a=0 if aligned else 1,input_b=0 if aligned else 1)
                result,report=run_plan(devices,rows)
                self.assertEqual(result['reference'],1 if aligned else 0)
                if not aligned:
                    self.assertEqual(result,rows)
                    self.assertTrue(any(r['reason']=='ambiguous_target_rows' for r in report['skipped']))
                self.assert_safe(rows,result,report)

    def test_multiple_reference_devices_are_not_chosen_by_name(self):
        devices,rows=mirror();devices['other_ref']=deepcopy(devices['reference']);rows['other_ref']=0
        result,report=run_plan(devices,rows)
        self.assertEqual(result,rows)
        self.assertTrue(any(r['reason']=='ambiguous_mirror_reference' for r in report['skipped']))

    def test_equal_strength_roots_are_not_arbitrarily_anchored(self):
        devices,rows=mirror();devices.pop('cascode');rows.pop('cascode')
        result,report=run_plan(devices,rows)
        self.assertEqual(result,rows);self.assertFalse(report['applied'])

    def test_fixed_and_supply_connected_columns_remain_unchanged(self):
        devices,rows=mirror()
        result,report=run_plan(devices,rows,fixed=('reference',))
        self.assertEqual(result,rows)
        self.assertTrue(any(c['locked_reason']=='fixed_member' for c in report['columns']))
        devices['reference']['d']=devices['reference']['g']='vdd';devices['output']['g']='vdd'
        result,report=run_plan(devices,rows)
        self.assertEqual(result,rows)
        self.assertTrue(any(c['members']==['reference'] and c['locked_reason']=='supply_connected_column'
                            for c in report['columns']))

    def test_an_already_aligned_peer_is_not_broken_for_a_third_device(self):
        devices,rows=mirror();devices['peer']=mos('peer','0','bias');rows['peer']=0
        result,report=run_plan(devices,rows)
        self.assertEqual(result,rows)
        self.assertTrue(any(r['reason']=='existing_same_row_conflict' for r in report['skipped']))
        self.assert_safe(rows,result,report)

    def test_real_side_branch_order_blocks_an_unsafe_translation(self):
        devices,rows=stacks()
        # The distinguished three-stage path is longer than its side access;
        # a tied fork would already be rejected by the original column planner.
        devices['lower']['s']='main_last';devices['ref_lower']['s']='ref_last'
        devices['bottom']=mos('main_last','0','control_c');rows['bottom']=3
        devices['ref_bottom']=mos('ref_last','0','control_c');rows['ref_bottom']=2
        devices['side']=mos('ref_join','0','side_control');rows['side']=1
        result,report=run_plan(devices,rows)
        self.assertEqual(result,rows)
        self.assertTrue(any(r['reason']=='conduction_order_conflict' for r in report['skipped']))
        self.assert_safe(rows,result,report)

    def test_source_drain_cycle_is_not_given_an_arbitrary_tier(self):
        devices=dict(a=mos('a','b','ga'),b=mos('b','a','gb'));rows=dict(a=0,b=1)
        result,report=run_plan(devices,rows)
        self.assertEqual(result,rows)
        self.assertTrue(any(c['locked_reason']=='original_conduction_cycle' for c in report['columns']))

    def test_polarity_duals_already_aligned_stay_aligned(self):
        for kind,make in (('PMOS',mos),('PNP',bjt)):
            devices=dict(ref=make('bias','vdd','bias',kind),out=make('join','vdd','bias',kind),
                         cas=make('port','join','control',kind))
            levels=compute_levels(devices)
            rows={n:levels['device_levels'][n]-1 for n in devices}
            result,report=run_plan(devices,rows)
            self.assertEqual(result,rows)
            self.assert_safe(rows,result,report)

    def test_rename_reorder_and_row_translation_preserve_actions(self):
        devices,rows=stacks(True);expected,report=run_plan(devices,rows)
        names={n:'changed'+str(i) for i,n in enumerate(reversed(list(devices)))}
        fields={'d','s','g','bulk'}
        rename_net=lambda n:n if n in ('0','vdd') else 'node_'+n
        mapped={names[n]:{k:rename_net(v) if k in fields else v for k,v in d.items()}
                for n,d in reversed(list(devices.items()))}
        shifted={names[n]:row+4 for n,row in rows.items()}
        result,other=run_plan(mapped,shifted)
        self.assertEqual({n:result[names[n]]-4 for n in rows},expected)
        self.assertEqual([a['delta'] for a in other['actions']],[a['delta'] for a in report['actions']])

    def test_deadline_validation_and_no_routing_or_random_state(self):
        devices,rows=mirror();old=deepcopy(devices)
        with patch('netlist2schematic.plugins.columns.branch_rows.build_column_plan',side_effect=AssertionError('expired')):
            result,report=run_plan(devices,rows,deadline=0)
        self.assertEqual(result,rows);self.assertEqual(report['stop_reason'],'deadline')
        for value in (float('nan'),float('inf'),True):
            with self.assertRaises(ValueError):run_plan(devices,rows,deadline=value)
        with patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('No A* during row planning')):
            result,report=run_plan(devices,rows)
        self.assertEqual(devices,old);self.assert_safe(rows,result,report)


class CorrespondingBranchIntegrationTests(unittest.TestCase):
    def source(self,folder,*,guests=True):
        text='''.model NM NMOS
MREF bias bias 0 0 NM
MOUT join bias 0 0 NM
MCAS port control join 0 NM
'''
        if guests:text+='IREF vdd feed DC 1m\nRFEED feed mid 1k\nLFEED mid bias 1m\n'
        path=Path(folder)/'case.cir';path.write_text(text,encoding='utf8');return path

    def test_source_and_rl_modules_follow_aligned_core_then_pass_real_routing(self):
        from netlist2schematic.layout.structured_placement import create_candidates
        from netlist2schematic.routing.sa_routing import final_route
        with tempfile.TemporaryDirectory() as folder:
            path=self.source(folder)
            item=next(create_candidates(path,iterations=0,passive_skeleton='auto',candidate_ids=(2,)))
            pl,cs,ns,ix,data=item
            self.assertIsNotNone(pl,data)
            self.assertTrue(data['layout_branch_rows']['applied'])
            self.assertEqual(pl.row_of[ix['MREF']],pl.row_of[ix['MOUT']])
            self.assertLess(pl.row_of[ix['MCAS']],pl.row_of[ix['MOUT']])
            self.assertTrue(data['passive_skeleton']['active_core_preserved'])
            self.assertIn('IREF',data['passive_skeleton']['source_layout']['guests'])
            self.assertTrue(final_route(PAR,ns,pl,ix)[2]['valid'])

    def test_no_guest_circuits_use_the_same_default_pre_sa_plan(self):
        from netlist2schematic.layout.structured_placement import create_candidates
        with tempfile.TemporaryDirectory() as folder:
            path=self.source(folder,guests=False)
            for pl,cs,ns,ix,data in create_candidates(path,iterations=0,passive_skeleton='auto',candidate_ids=(2,3)):
                self.assertEqual(pl.row_of[ix['MREF']],pl.row_of[ix['MOUT']])
                self.assertTrue(data['layout_branch_rows']['applied'])
                self.assertTrue(compatible_rows(data['layout_branch_rows'],pl,ix))

    def test_reference_off_and_columns_off_keep_original_row_rules(self):
        from netlist2schematic.layout.structured_placement import create_candidates
        with tempfile.TemporaryDirectory() as folder:
            path=self.source(folder,guests=False)
            for args in (dict(passive_skeleton='auto',candidate_ids=(1,)),
                         dict(passive_skeleton='off',candidate_ids=(2,3)),
                         dict(passive_skeleton='auto',skeleton_columns=False,candidate_ids=(2,3))):
                with patch('netlist2schematic.plugins.columns.branch_rows.plan_branch_correspondence',side_effect=AssertionError('disabled')):
                    for item in create_candidates(path,iterations=0,**args):
                        self.assertNotIn('layout_branch_rows',item[4])

    def test_feedback_contract_rejects_original_rows_and_accepts_planned_rows(self):
        devices,rows=mirror();after,report=run_plan(devices,rows)
        comps=to_document(devices)['components'];ix={c['id']:i for i,c in enumerate(comps)}
        old=SimpleNamespace(cell_of=[(rows[c['id']],0) for c in comps])
        new=SimpleNamespace(cell_of=[(after[c['id']],0) for c in comps])
        self.assertFalse(compatible_rows(report,old,ix))
        self.assertTrue(compatible_rows(report,new,ix))


if __name__=='__main__':unittest.main()
