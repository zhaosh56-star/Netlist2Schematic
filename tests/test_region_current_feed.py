"""Explicit current-fed folded cores retain port roles and exclusive ownership."""
from copy import deepcopy
from unittest import TestCase,main
from unittest.mock import patch

from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.plugins.region.region_layout import build_region_plan,measure_regions
from tests.test_region_layout import ota,Position
from tests.test_region_placement import region_seed


def current_fed(*,reverse=False):
    devices=ota(False)
    del devices['feed']
    devices['supply']=dict(cls='ISRC',type='I',p='source' if reverse else 'hi',
                           n='hi' if reverse else 'source',value='1m')
    return devices


class CurrentFedRegionTests(TestCase):
    def test_current_feed_is_real_lowest_leaf_member_with_explicit_pole(self):
        for reverse in (False,True):
            devices=current_fed(reverse=reverse)
            before=deepcopy(devices)
            plan=build_region_plan(devices)
            self.assertTrue(plan['supported'])
            self.assertEqual(plan['coverage']['recognized_devices'],10)
            self.assertEqual(plan['coverage']['recognized_fraction'],1)
            self.assertEqual(plan['coverage']['auxiliary_devices'],['supply'])
            leaf=next(r for r in plan['regions'] if r['id']==plan['ownership']['supply'])
            self.assertEqual(leaf['kind'],'folded_input')
            self.assertEqual(set(leaf['members']),{'ia','ib','supply'})
            j=next(j for j in plan['junctions'] if j['kind']=='shared_feed')
            self.assertEqual(j['tail_pins'],{'supply':'+' if reverse else '-'})
            self.assertEqual(devices,before)

    def test_no_folded_extension_for_voltage_floating_tapped_or_shared_source(self):
        for mode in ('voltage','floating','tapped','shared','shorted'):
            devices=current_fed()
            if mode=='voltage':devices['supply'].update(cls='VSRC',type='V')
            elif mode=='floating':devices['supply']['p']='floating'
            elif mode=='tapped':
                devices['tap']=dict(cls='PASSIVE',type='R',a='source',b='input0',value='1k')
            elif mode=='shared':devices['another_source']=deepcopy(devices['supply'])
            else:devices['supply'].update(p='source',n='source')
            with self.subTest(mode=mode):
                plan=build_region_plan(devices)
                self.assertFalse(plan['supported'])
                self.assertEqual(plan['coverage']['recognized_devices'],0)

    def test_measurement_uses_actual_plus_minus_terminal_instead_of_mos_drain(self):
        devices=current_fed()
        plan=build_region_plan(devices)
        comps=to_document(devices)['components']
        index={c['id']:i for i,c in enumerate(comps)}
        pl=Position(index,{name:(num*100,0) for num,name in enumerate(index)})
        original=pl.pin_world
        def pin(i,terminal):
            if i==index['supply']:
                self.assertEqual(terminal,'-')
            return original(i,terminal)
        with patch.object(pl,'pin_world',side_effect=pin) as read:
            metrics=measure_regions(pl,comps,index,plan)
        self.assertTrue(metrics['measurable'])
        self.assertIn(((index['supply'],'-'),{}),[(c.args,c.kwargs) for c in read.call_args_list])

    def test_rename_reorder_and_polarity_dual_keep_role_members_and_current_pin(self):
        base=current_fed()
        for dual in (False,True):
            devices={}
            names={name:'unit'+str(100-i) for i,name in enumerate(base)}
            nets={value:'node'+str(i) for i,value in enumerate(sorted({v for d in base.values()
                for k,v in d.items() if k in ('d','g','s','bulk','p','n')}))}
            for name in reversed(list(base)):
                row=deepcopy(base[name])
                for k in ('d','g','s','bulk','p','n'):
                    if k in row:row[k]=nets[row[k]]
                if dual and row['cls']=='MOS':
                    row['type']='PMOS' if row['type']=='NMOS' else 'NMOS'
                    row['model']=row['type']
                devices[names[name]]=row
            plan=build_region_plan(devices)
            self.assertTrue(plan['supported'])
            self.assertEqual(plan['coverage']['recognized_fraction'],1)
            self.assertEqual(plan['ownership'][names['supply']],plan['ownership'][names['ia']])
            self.assertEqual(next(j['tail_pins'] for j in plan['junctions'] if 'tail_pins' in j),
                             {names['supply']:'-'})

    def test_region_initialization_keeps_all_source_rows_and_no_router_is_called(self):
        with patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('A*')):
            pl,comps,nets,index,plan,_,_,_,rows=region_seed(current_fed)
        self.assertTrue(plan['supported'])
        self.assertEqual(pl.row_of,rows)
        self.assertEqual(len(set(pl.cell_of)),len(comps))
        self.assertLess(max(r for r,_ in pl.cell_of),len(pl.row_y))
        self.assertTrue(measure_regions(pl,comps,index,plan)['measurable'])
        self.assertTrue(pl.blocks_contiguous())


if __name__=='__main__':
    main()
