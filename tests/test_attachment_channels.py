"""Topology-only parallel channels, independent from any GT circuit names."""
from copy import deepcopy
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_attachment_ports import installed
from tests.test_passive_placement import mos, resistor
from netlist2schematic.plugins.attachment import attachment_channels as channels
from netlist2schematic.plugins.attachment import attachment_integration as integration
from netlist2schematic.plugins.attachment.attachment_selection import organization_rank
from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
from netlist2schematic.routing.routing_rules import RoutingOptions


def fixture(*, bjt=False, rename=False):
    p=lambda d:dict(cls='MOS',type='PMOS',model='PMOS',d=d,g='control',s='vdd',bulk='vdd')
    devices=dict(upper_a=p('left'),upper_b=p('right'),lower_a=mos('left','ga'),lower_b=mos('right','gb'),
        a=resistor('left','mid0','L'),b=resistor('mid0','mid1','C'),c=resistor('mid1','right'),
        bypass=resistor('left','right','C'),arm_a=resistor('left','control'),
        arm_b=resistor('right','control'),outsider=resistor('outside','0'))
    cells=dict(upper_a=(0,1),upper_b=(0,6),lower_a=(1,1),lower_b=(1,6),
        a=(0,2),b=(0,3),c=(0,4),bypass=(1,3),arm_a=(2,0),arm_b=(2,7),outsider=(3,8))
    if bjt:
        for name in ('upper_a','upper_b','lower_a','lower_b'):
            d=devices[name];devices[name]=dict(cls='BJT',type='PNP' if name.startswith('upper') else 'NPN',
                model='Q',c=d['d'],b=d['g'],e=d['s'])
    if rename:
        names={n:'unit_'+str(90-k) for k,n in enumerate(devices)}
        changed={}
        for n,d in reversed(list(devices.items())):
            changed[names[n]]={k:('signal_'+v if k in ('a','b','c','d','g','s','e') and v not in ('0','vdd') else v)
                               for k,v in d.items()}
        devices=changed;cells={names[n]:v for n,v in cells.items()}
    return devices,installed(devices,cells)


def trial_for(f):
    pl,comps,nets,index,plan=f;bank,=plan['parallel_channels']['banks']
    trial,action=next(channels.channel_trials(structured.par,pl,comps,index,plan,bank,
        options=RoutingOptions(),initialization=True))
    return trial,action


def state(pl):
    return deepcopy((pl.cell_of,pl.orient,pl.row_of,pl.comp_at,pl.column_x,pl.row_y,pl.rng.getstate()))


class ParallelChannelTests(unittest.TestCase):
    def test_unique_junctions_compile_paths_and_related_control_arms_without_ownership_union(self):
        _,f=fixture();pl,comps,nets,index,plan=f
        model=plan['parallel_channels'];bank,=model['banks']
        self.assertEqual(sorted(len(p['members']) for p in bank['paths']),[1,2,3])
        self.assertEqual(len(bank['members']),6)
        self.assertEqual(len(bank['groups']),2)
        self.assertNotIn('upper_a',bank['members'])
        self.assertEqual(model,channels.compile_channels(list(reversed(comps)),plan))

    def test_extra_conduction_terminal_missing_axis_and_shared_interior_are_explicitly_skipped(self):
        _,f=fixture();_,comps,_,_,plan=f
        for mode in ('axis','third','shared'):
            altered=deepcopy(plan)
            if mode=='axis':altered['host_axes']=[]
            if mode=='third':altered['readability_plan']['junctions']['left']['conduction_ports'].append(['other','D'])
            if mode=='shared':
                g=next(g for g in altered['groups'] if g['kind']=='multi_path_bridge')
                g['paths'].append(deepcopy(g['paths'][0]))
            model=channels.compile_channels(comps,altered)
            self.assertEqual(model['banks'],[]);self.assertTrue(model['skipped'])

    def test_common_bias_without_parallel_paths_does_not_create_a_bank(self):
        _,f=fixture();plan=deepcopy(f[4]);plan['groups']=[g for g in plan['groups'] if g['kind']!='multi_path_bridge']
        self.assertEqual(channels.compile_channels(f[1],plan)['banks'],[])

    def test_real_geometry_allocates_three_nonoverlapping_rows_and_actual_inner_columns(self):
        _,f=fixture();pl,comps,nets,index,plan=f;before=state(pl)
        with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar')):
            trial,action=trial_for(f)
        self.assertTrue(trial.valid_state());self.assertGreater(action['gap_added'],0)
        self.assertEqual(channels.channel_rank(channels.measure_channels(trial,comps,index,plan['parallel_channels'])),(0,0,0,0,0))
        self.assertEqual(_static_faults(structured.par,trial,nets,index,RoutingOptions(),None)['count'],0)
        self.assertEqual(state(pl),before)
        bank,=plan['parallel_channels']['banks']
        for name in bank['members']:self.assertTrue(1<trial.cell_of[index[name]][1]<6)
        for name in ('upper_a','upper_b','lower_a','lower_b'):
            i=index[name];self.assertEqual(trial.orient[i],pl.orient[i])
            self.assertEqual(trial.pos_of(i)[0],pl.pos_of(i)[0])
        self.assertLess(trial.pos_of(index['upper_a'])[1],trial.pos_of(index['lower_a'])[1])

    def test_external_control_tap_orders_arms_before_unanchored_paths(self):
        _,f=fixture();trial,_=trial_for(f)
        metric=channels.measure_channels(trial,f[1],f[3],f[4]['parallel_channels'])
        paths=metric['records'][0]['paths']
        anchored=next(p for p in paths if p['control_y'] is not None)
        self.assertTrue(all(anchored['y']<p['y'] for p in paths if p['control_y'] is None))

    def test_bjt_and_reordered_renamed_netlist_have_equivalent_channels(self):
        for options in ({},{'bjt':True},{'rename':True}):
            with self.subTest(options=options):
                _,f=fixture(**options);trial,_=trial_for(f)
                self.assertTrue(trial.valid_state())
                self.assertEqual(channels.channel_rank(channels.measure_channels(trial,f[1],f[3],f[4]['parallel_channels'])),(0,0,0,0,0))
                self.assertEqual(_static_faults(structured.par,trial,f[2],f[3],RoutingOptions(),None)['count'],0)

    def test_mirror_and_translation_preserve_measured_relations(self):
        _,f=fixture();trial,_=trial_for(f)
        class View:
            def pos_of(self,i):
                x,y=trial.pos_of(i);return 3000-x,y+91
            def pin_world(self,i,pin):
                x,y=trial.pin_world(i,pin);return 3000-x,y+91
            def pin_dir(self,i,pin):
                d=trial.pin_dir(i,pin);return {'east':'west','west':'east'}.get(d,d)
        a=channels.measure_channels(trial,f[1],f[3],f[4]['parallel_channels'])
        b=channels.measure_channels(View(),f[1],f[3],f[4]['parallel_channels'])
        self.assertEqual(channels.channel_rank(a),channels.channel_rank(b))

    def test_changed_terminal_and_nonfacing_host_are_unknown_not_zero(self):
        _,f=fixture();pl,comps,nets,index,plan=f
        changed=deepcopy(comps);changed[index['upper_a']]['nodes']['D']='changed'
        a=channels.measure_channels(pl,changed,index,plan['parallel_channels'])
        self.assertFalse(a['measurable']);self.assertIsNone(a['outside_errors']);self.assertTrue(a['missing'])
        class View:
            def pos_of(self,i):return pl.pos_of(i)
            def pin_world(self,i,p):return pl.pin_world(i,p)
            def pin_dir(self,i,p):return 'north'
        self.assertIsNone(channels.channel_rank(channels.measure_channels(View(),comps,index,plan['parallel_channels'])))

    def test_fixed_passive_and_fixed_lower_host_prevent_unsafe_expansion(self):
        for name in ('a','lower_a'):
            _,f=fixture();pl,comps,nets,index,plan=f
            pl.fixed={index[name]};pl.fixed_cells={index[name]:pl.cell_of[index[name]]}
            pl=integration._install(pl,comps,index,plan)
            budget=integration.new_attachment_budget()
            report=dict(actions=[],candidate_trials=0,state_legal_trials=0,legal_trials=0)
            result=integration._parallel_channel_stage(structured.par,pl,pl,comps,nets,index,plan,
                RoutingOptions(),budget,perf_counter()+1,report,initialization=True)
            self.assertIs(result,pl);self.assertEqual(report['actions'],[])

    def test_deadline_zero_budget_and_failed_static_transaction_restore_every_state(self):
        _,f=fixture();pl,comps,nets,index,plan=f;before=state(pl)
        bank,=plan['parallel_channels']['banks']
        self.assertEqual(list(channels.channel_trials(structured.par,pl,comps,index,plan,bank,
            options=RoutingOptions(),initialization=True,deadline=perf_counter()-1)),[])
        for response in (False,None):
            report=dict(actions=[],candidate_trials=0,state_legal_trials=0,legal_trials=0)
            budget=integration.new_attachment_budget(max_candidates=1)
            with patch.object(integration,'_checked_static',return_value=response):
                result=integration._parallel_channel_stage(structured.par,pl,pl,comps,nets,index,plan,
                    RoutingOptions(),budget,perf_counter()+1,report,initialization=True)
            self.assertIs(result,pl);self.assertEqual(state(pl),before)
            self.assertEqual(report['actions'],[]);self.assertLessEqual(budget['candidates'],1)

    def test_finish_never_expands_axes_and_uses_only_saved_rows(self):
        _,f=fixture();pl,comps,nets,index,plan=f;bank,=plan['parallel_channels']['banks']
        self.assertEqual(list(channels.channel_trials(structured.par,pl,comps,index,plan,bank,
            options=RoutingOptions(),initialization=False)),[])
        current,_=trial_for(f)
        for trial,_ in channels.channel_trials(structured.par,current,comps,index,plan,bank,
                options=RoutingOptions(),initialization=False):
            self.assertEqual(trial.row_y,current.row_y);self.assertEqual(trial.column_x,current.column_x)

    def test_dense_body_guard_and_channel_limits_survive_sa_restore(self):
        _,f=fixture();pl,comps,nets,index,plan=f
        report=dict(actions=[],candidate_trials=0,state_legal_trials=0,legal_trials=0)
        current=integration._parallel_channel_stage(structured.par,pl,pl,comps,nets,index,plan,
            RoutingOptions(),integration.new_attachment_budget(),perf_counter()+1,report,initialization=True)
        self.assertTrue(report['actions']);self.assertTrue(current.valid_state())
        axes=deepcopy((current.column_x,current.row_y))
        for _ in range(100):
            previous=list(current.cell_of),list(current.orient)
            undo=current.propose()
            if undo is not None:
                self.assertTrue(current.valid_state())
                self.assertTrue(channels.bodies_clear(structured.par,current,12))
                current.set_state(*undo)
            self.assertEqual((current.cell_of,current.orient),previous)
            self.assertEqual((current.column_x,current.row_y),axes)
        old=list(current.cell_of);cells=list(old)
        cells[index['outsider']]=cells[index['upper_a']]
        current.set_state(cells,current.orient)
        self.assertFalse(current.valid_state())

    def test_a_host_column_is_not_a_passive_slot_even_if_its_drain_pin_extends_outward(self):
        _,f=fixture();pl,comps,nets,index,plan=f
        trial,_=trial_for(f)
        cells=list(trial.cell_of)
        row,_=cells[index['bypass']]
        cells[index['bypass']]=row,cells[index['lower_a']][1]
        trial.set_state(cells,trial.orient)
        measured=channels.measure_channels(trial,comps,index,plan['parallel_channels'])
        self.assertEqual(measured['interior_errors'],1)
        self.assertGreater(channels.channel_rank(measured),(0,0,0,0,0))

    def test_existing_sa_objective_is_unchanged_and_unknown_channel_cannot_win_selection(self):
        _,f=fixture();pl,comps,nets,index,plan=f
        legacy=deepcopy(plan);legacy.pop('parallel_channels')
        self.assertEqual(integration.score_attachments(pl,comps,index,plan),integration.score_attachments(pl,comps,index,legacy))
        base=integration.measure_attachments(pl,comps,index,plan)
        report=dict(metrics=base,parallel_channel_access=dict(count=1,measurable=False))
        self.assertIsNone(organization_rank(report))


if __name__=='__main__':unittest.main()
