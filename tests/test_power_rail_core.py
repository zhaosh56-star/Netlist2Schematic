"""Real-port, glyph-entry and transactional safety for optional VDD sharing."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from netlist2schematic.plugins.power_rails import rail_core as core
from netlist2schematic.routing.routing_rules import Scene, RoutingOptions


def fixture(xs=(0, 150), nets=None, shift=(0, 0)):
    options = RoutingOptions()
    scene = Scene(options=options)
    terminals = []
    ox, oy = shift
    for i, x in enumerate(xs):
        x += ox
        net = (nets or ['VDD']*len(xs))[i]
        p = dict(component_id=f'P{i}', component=i, pin='S', net=net, symbol='vdd-port',
            point=(x,100+oy), escape=(x,86+oy), direction=(0,-1),
            stub=((x,100+oy),(x,76+oy)), marker_rect=(x-24,62+oy,x+24,112+oy))
        terminals.append(p)
        scene.body_rects.append((i,(x-20,88+oy,x+20,150+oy)))
        scene.marker_rects.append((net,p['marker_rect']))
        scene.marker_components.append(i)
        scene.fixed_segments.setdefault(net,[]).append(p['stub'])
        scene.all_pins.setdefault(net,[]).append(p['point'])
        scene.power_nets.add(net)
    geometry=dict(pin=(0,20),box=(-12,-2,12,24))
    hints=dict(component_text=[],supply_text=[],port_text=[],text_obstacles=[])
    return scene, [], terminals, geometry, hints


class PowerRailCoreTests(unittest.TestCase):
    def build(self, values=None, **kwargs):
        values = values or fixture()
        return core.build_view(*values, scale=100, **kwargs)

    def check(self, values, view):
        scene,routed,terminals,geometry,hints=values
        return core.validate_view(scene,routed,terminals,geometry,view,hints,scale=100)

    def test_two_and_three_original_terminals_share_one_real_symbol(self):
        for xs in ((0,150),(0,150,300)):
            with self.subTest(xs=xs):
                values=fixture(xs)
                before=deepcopy(values)
                view=self.build(values)
                checked=self.check(values,view)
                self.assertEqual(len(xs)-1,checked['markers_removed'])
                self.assertEqual(1,len(view['groups']))
                self.assertEqual(before,values)

    def test_voltage_domains_do_not_merge_even_with_same_symbol(self):
        view=self.build(fixture(nets=['AVDD','DVDD']))
        self.assertEqual([],view['groups'])

    def test_ground_and_non_upward_and_different_height_are_untouched(self):
        for update in (dict(symbol='ground'),dict(direction=(1,0)),dict(point=(150,102))):
            values=fixture()
            values[2][1].update(update)
            self.assertEqual([],self.build(values)['groups'])

    def test_signal_wall_is_never_crossed_and_failed_trials_roll_back(self):
        values=fixture()
        values[1].append(('control',[((75,-300),(75,110))]))
        before=deepcopy(values)
        view=self.build(values)
        self.assertFalse(view['groups'])
        self.assertTrue(view['diagnostics']['rejected'])
        self.assertEqual(before,values)

    def test_partial_merge_when_large_group_is_blocked(self):
        values=fixture((0,100,200))
        values[1].append(('control',[((150,-300),(150,110))]))
        view=self.build(values)
        self.assertEqual(1,len(view['groups']))
        self.assertEqual([['P0','S'],['P1','S']],view['groups'][0]['ports'])
        self.check(values,view)

    def test_a_body_between_pins_blocks_the_rail(self):
        values=fixture()
        values[0].body_rects.append((2,(60,-300,90,130)))
        self.assertFalse(self.build(values)['groups'])

    def test_untouched_supply_symbol_is_an_obstacle(self):
        values=fixture((0,150,75),['VDD','VDD','AVDD'])
        # Its glyph and body sit in the candidate corridor; same glyph shape
        # does not grant another voltage domain an exception.
        values[0].body_rects[2]=(2,(60,-300,90,150))
        self.assertFalse(self.build(values)['groups'])

    def test_zero_and_interrupted_budget_return_current_verified_state(self):
        view=self.build(seconds=0)
        self.assertEqual([],view['groups'])
        self.assertEqual('time_budget',view['diagnostics']['stop_reason'])
        self.check(fixture(),view)

    def test_timeout_after_one_merge_keeps_only_that_verified_merge(self):
        from netlist2schematic.routing.orthogonal_router import RoutingFailure
        values=fixture((0,150,300,450),['VDD','VDD','AVDD','AVDD'])
        original=core.validate_view
        calls=[]
        def interrupt(*args,**kwargs):
            calls.append(1)
            if len(calls)>1:raise RoutingFailure('test timeout',{'reason':'quality_time_budget'})
            return original(*args,**kwargs)
        with patch.object(core,'validate_view',side_effect=interrupt):
            view=self.build(values)
        self.assertEqual(1,len(view['groups']))
        self.assertEqual('time_budget',view['diagnostics']['stop_reason'])
        self.check(values,view)

    def test_tight_pitch_can_skip_instead_of_crashing(self):
        values=fixture((0,40))
        view=core.build_view(*values,scale=20)
        self.assertEqual('unchanged',view['status'])

    def test_custom_clearances_and_true_marker_pin_offset(self):
        values=fixture()
        values[0].options=RoutingOptions(halo=16,grid_step=4,cross_clearance=20)
        for p in values[2]:
            p['escape']=(p['point'][0],80)
        view=self.build(values)
        self.assertEqual(1,len(view['groups']))
        self.check(values,view)
        group=view['groups'][0]
        shape=core._group_geometry(group,{(p['component_id'],p['pin']):p for p in values[2]},
                                   values[3],values[0].options,100)
        self.assertEqual(group['anchor'][1]-20,shape['origin'][1])

    def test_translation_and_instance_net_rename_preserve_sharing(self):
        for shift in ((0,0),(-70,42),(200,-300)):
            values=fixture(shift=shift)
            for p in values[2]:
                p['component_id']='renamed'+p['component_id']
            view=self.build(values)
            self.assertEqual(1,len(view['groups']))
            self.check(values,view)
        values=fixture(nets=['AVDD','AVDD'])
        view=self.build(values)
        self.assertEqual('AVDD',view['groups'][0]['net'])

    def test_deleted_lead_wrong_net_text_and_displaced_anchor_are_rejected(self):
        values=fixture();view=self.build(values)
        for mutate in (
            lambda g:g['segments'].pop(),
            lambda g:g.update(net='DVDD'),
            lambda g:g['label'].update(text='DVDD'),
            lambda g:g.update(anchor=[999,0]),
            lambda g:g['ports'].append(['unknown','S']),
        ):
            damaged=deepcopy(view);mutate(damaged['groups'][0])
            with self.assertRaises(ValueError):self.check(values,damaged)

    def test_duplicate_group_and_floating_text_are_rejected(self):
        values=fixture();view=self.build(values)
        damaged=deepcopy(view);damaged['groups'].append(deepcopy(damaged['groups'][0]))
        with self.assertRaises(ValueError):self.check(values,damaged)
        damaged=deepcopy(view);label=damaged['groups'][0]['label'];label['x']+=1000
        label['bounds']=list(core._text_box(label['x'],label['y'],label['text'],label['anchor']))
        with self.assertRaises(ValueError):self.check(values,damaged)

    def test_new_wire_avoids_existing_component_text(self):
        values=fixture()
        values[4]['component_text']=[dict(bounds=[60,-300,90,110],text='blocked')]
        values[4]['text_obstacles']=[[60,-300,90,110]]
        self.assertFalse(self.build(values)['groups'])

    def test_repeat_is_deterministic_without_path_search(self):
        with patch('netlist2schematic.routing.orthogonal_router._find_path',
                   side_effect=AssertionError('Shared supply must not call A*')):
            one=self.build();two=self.build()
        self.assertEqual(one['groups'],two['groups'])


if __name__=='__main__':unittest.main()
