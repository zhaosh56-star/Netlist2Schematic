"""A generic bank's deeper accesses go outside its shallower accesses."""
from copy import deepcopy
from unittest import TestCase,main

from netlist2schematic.plugins.region.region_interfaces import compile_port_banks,measure_port_banks
from tests.test_region_layout import Position,comparator
from netlist2schematic.plugins.region.region_layout import build_region_plan


def case(*,reverse=False,mirror=False,shift=(0,0)):
    comps=[dict(id=name,symbol=symbol,nodes=nodes) for name,symbol,nodes in (
        ('far','pmos',dict(D='low',G='clock',S='rail')),
        ('near','pmos',dict(D='high',G='clock',S='rail')),
        ('lower','nmos',dict(D='low',G='bias0',S='floor')),
        ('upper','nmos',dict(D='high',G='bias1',S='floor')))]
    index={c['id']:i for i,c in enumerate(comps)}
    bank=dict(id='bank',parent='parent',children=[],members=['far','near'],attachments=[
        dict(device=name,pin='D',net=net,target_region='core',branch='one')
        for name,net in [('far','low'),('near','high')]])
    core=dict(id='core',parent='parent',children=[],members=['lower','upper'])
    banks=compile_port_banks([bank,core],comps,{'rail','floor'})
    positions=dict(far=(100 if reverse else 0,0),near=(0 if reverse else 100,0),
                   lower=(200,200),upper=(200,50))
    points={name:(point[0]+shift[0],point[1]+shift[1]) for name,point in positions.items()}
    pl=Position(index,points,mirrored=mirror)
    return pl,index,banks,[bank,core],comps


class PortBankTests(TestCase):
    def test_deeper_port_outside_is_good_but_reversed_port_order_costs_one(self):
        for reverse in (False,True):
            pl,index,banks,_,_=case(reverse=reverse)
            report=measure_port_banks(pl,index,banks)
            self.assertTrue(report['measurable'])
            self.assertEqual(report['relation_count'],1)
            self.assertEqual(report['ordered_pairs'],1)
            self.assertEqual(report['errors'],int(reverse))

    def test_translation_horizontal_and_vertical_mirror_do_not_change_error(self):
        for reverse in (False,True):
            for mirror in (False,True):
                pl,index,banks,_,_=case(reverse=reverse,mirror=mirror,shift=(400,-200))
                for vertical_sign in (1,-1):
                    before=deepcopy(pl.points)
                    pl.points={name:(x,vertical_sign*y) for name,(x,y) in before.items()}
                    self.assertEqual(measure_port_banks(pl,index,banks)['errors'],int(reverse))
                    pl.points=before

    def test_supply_clock_and_stale_ports_cannot_create_a_bank(self):
        for mode in ('clock','supply','stale','parent','missing_target'):
            _,_,_,regions,comps=case()
            if mode=='clock':
                for a in regions[0]['attachments']:a.update(pin='G',net='clock')
            elif mode=='supply':
                for a in regions[0]['attachments']:a['net']='rail'
            elif mode=='stale':regions[0]['attachments'][0]['net']='unconnected'
            elif mode=='parent':regions[1]['parent']='other'
            else:comps[2]['nodes']['D']='other'
            self.assertEqual(compile_port_banks(regions,comps,{'rail','floor'}),[])

    def test_moving_bank_across_core_does_not_hide_a_bad_relation(self):
        pl,index,banks,_,_=case()
        pl.points['far']=(250,0)
        report=measure_port_banks(pl,index,banks)
        self.assertTrue(report['measurable'])
        self.assertEqual(report['errors'],1)
        self.assertEqual(report['records'][0]['reason'],'bank_straddles_target_branch')

    def test_unmeasurable_geometry_is_unknown_not_zero(self):
        pl,index,banks,_,_=case()
        del index['upper']
        report=measure_port_banks(pl,index,banks)
        self.assertFalse(report['measurable'])
        self.assertIsNone(report['errors'])
        self.assertEqual(len(report['missing']),1)

    def test_equal_depth_does_not_invent_a_direction_or_a_gain(self):
        pl,index,banks,_,_=case(reverse=True)
        pl.points['lower']=(200,50)
        report=measure_port_banks(pl,index,banks)
        self.assertEqual(report['ordered_pairs'],0)
        self.assertEqual(report['errors'],0)

    def test_instance_net_rename_and_reorder_preserve_errors(self):
        pl,index,banks,regions,comps=case(reverse=True)
        names={name:'part'+str(91-i) for name,i in index.items()}
        nets={n:'node'+str(i) for i,n in enumerate(sorted({n for c in comps for n in c['nodes'].values()}))}
        comps=[dict(c,id=names[c['id']],nodes={p:nets[n] for p,n in c['nodes'].items()}) for c in reversed(comps)]
        for region in regions:
            region['members']=[names[n] for n in reversed(region['members'])]
            for a in region.get('attachments',()):a.update(device=names[a['device']],net=nets[a['net']])
        banks=compile_port_banks(regions,comps,{nets['rail'],nets['floor']})
        index={c['id']:i for i,c in enumerate(comps)}
        pl=Position(index,{names[n]:p for n,p in pl.points.items()})
        self.assertEqual(measure_port_banks(pl,index,banks)['errors'],1)

    def test_complete_core_compiles_both_banks_with_real_conduction_targets(self):
        plan=build_region_plan(comparator())
        self.assertEqual(len(plan['port_banks']),2)
        self.assertEqual(plan['coverage']['port_banks'],2)
        for bank in plan['port_banks']:
            self.assertEqual(len(bank['ports']),2)
            self.assertTrue(all(t['pin'] in ('D','S','C','E') for a in bank['ports'] for t in a['targets']))

    def test_real_seed_respects_nested_banks_without_changing_current_rows(self):
        from tests.test_region_placement import region_seed
        from netlist2schematic.plugins.region.region_layout import measure_regions
        from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
        from netlist2schematic.layout.structured_placement import par
        from netlist2schematic.routing.routing_rules import RoutingOptions
        pl,comps,nets,index,plan,_,_,_,rows=region_seed(comparator)
        report=measure_regions(pl,comps,index,plan)
        self.assertEqual(report['port_bank_order']['ordered_pairs'],2)
        self.assertEqual(report['port_bank_order']['errors'],0)
        self.assertEqual(report['order_errors'],0)
        self.assertEqual(pl.row_of,rows)
        self.assertEqual(_static_faults(par,pl,nets,index,RoutingOptions(),None)['count'],0)


if __name__=='__main__':
    main()
