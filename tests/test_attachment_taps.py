"""Actual two-sided junctions, fixed-world split rows, no SA/A* in trials."""
from copy import deepcopy
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_attachment_ports import installed
from tests.test_passive_placement import mos,resistor
from netlist2schematic.plugins.attachment import attachment_taps as taps
from netlist2schematic.plugins.attachment import attachment_integration as integration
from netlist2schematic.plugins.attachment.attachment_selection import organization_rank
from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
from netlist2schematic.routing.routing_rules import RoutingOptions


def fixture(*,sense=False):
    if sense:
        p=lambda d:dict(cls='MOS',type='PMOS',model='PMOS',d=d,g='center',s='vdd',bulk='vdd')
        devices=dict(pa=p('left'),pb=p('right'),a=mos('left','ga'),b=mos('right','gb'),
                     r=resistor('left','center'),c=resistor('right','center'))
        cells=dict(pa=(0,1),pb=(0,4),a=(1,1),b=(1,4),r=(0,2),c=(0,3))
        orientations=dict(r=3,c=1)
    else:
        devices=dict(a=mos('left','ga'),b=mos('right','gb'),
                     u=mos('outa','ba','left'),v=mos('outb','bb','right'),
                     r=resistor('left','mid'),c=resistor('mid','right','C'))
        cells=dict(u=(1,1),v=(1,4),a=(2,1),b=(2,4),r=(1,2),c=(1,3))
        orientations=dict(r=3,c=3)
    result=installed(devices,cells,{n:o%2 for n,o in orientations.items()})
    pl,_,_,index,_=result
    desired=list(pl.orient)
    for name,orientation in orientations.items():desired[index[name]]=orientation
    pl.set_state(pl.cell_of,desired)
    return result


def model(f):return f[4]['conduction_taps']


def snapshot(pl):
    return ({k:deepcopy(v) for k,v in pl.__dict__.items() if k!='rng'},pl.rng.getstate())


class JunctionTapTests(unittest.TestCase):
    def test_real_band_slots_preserve_non_guests_and_all_host_local_geometry(self):
        f=fixture();pl,comps,nets,index,plan=f
        before=snapshot(pl);relation,=model(f)['groups']
        metric=taps.measure_taps(pl,comps,index,model(f))
        self.assertEqual(metric['errors'],2)
        with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('A*')):
            trial,action=next(taps.tap_trials(pl,comps,index,plan,relation,allow_row_split=True))
        self.assertTrue(action['row_added']);self.assertTrue(trial.valid_state())
        self.assertEqual(taps.measure_taps(trial,comps,index,model(f))['errors'],0)
        self.assertEqual(_static_faults(structured.par,trial,nets,index,RoutingOptions(),None)['count'],0)
        for name,i in index.items():
            if name not in relation['members']:
                self.assertEqual(pl.pos_of(i),trial.pos_of(i))
                for pin in comps[i]['nodes']:self.assertEqual(pl.pin_world(i,pin),trial.pin_world(i,pin))
        self.assertEqual(snapshot(pl),before)

    def test_refinement_uses_existing_row_and_never_adds_axes(self):
        f=fixture();pl,comps,nets,index,plan=f;relation,=model(f)['groups']
        self.assertEqual(list(taps.tap_trials(pl,comps,index,plan,relation,allow_row_split=False)),[])
        trial,_=next(taps.tap_trials(pl,comps,index,plan,relation,allow_row_split=True))
        cells=list(trial.cell_of)
        for n in relation['members']:cells[index[n]]=(1,cells[index[n]][1])
        trial.set_state(cells,trial.orient)
        moved,action=next(taps.tap_trials(trial,comps,index,plan,relation,allow_row_split=False))
        self.assertFalse(action['row_added']);self.assertEqual(moved.row_y,trial.row_y)
        self.assertEqual(taps.measure_taps(moved,comps,index,model(f))['errors'],0)

    def test_fixed_guest_is_skipped_fixed_host_world_coordinates_are_preserved(self):
        f=fixture();pl,comps,nets,index,plan=f;relation,=model(f)['groups']
        pl.fixed={index['r']}
        self.assertEqual(list(taps.tap_trials(pl,comps,index,plan,relation,allow_row_split=True)),[])
        pl.fixed={index['a']};pl.fixed_cells={index['a']:pl.cell_of[index['a']]}
        pl=integration._install(pl,comps,index,plan)
        trial,_=next(taps.tap_trials(pl,comps,index,plan,relation,allow_row_split=True))
        self.assertTrue(trial.valid_state());self.assertEqual(trial.pos_of(index['a']),pl.pos_of(index['a']))

    def test_cascoded_and_complementary_drain_junctions_are_geometric_not_function_labels(self):
        for sense in (False,True):
            f=fixture(sense=sense);pl,comps,nets,index,plan=f
            self.assertEqual(len(model(f)['groups']),1)
            relation,=model(f)['groups']
            trial,_=next(taps.tap_trials(pl,comps,index,plan,relation,allow_row_split=True))
            self.assertEqual(taps.measure_taps(trial,comps,index,model(f))['errors'],0)

    def test_bjt_and_polarity_dual_use_actual_collector_emitter_geometry(self):
        for dual in (False,True):
            rail='vdd' if dual else '0';kind='PNP' if dual else 'NPN'
            q=lambda c,b,e:dict(cls='BJT',type=kind,model=kind,c=c,b=b,e=e)
            devices=dict(a=q('left','ga',rail),b=q('right','gb',rail),
                u=q('outa','ba','left'),v=q('outb','bb','right'),
                r=resistor('left','mid'),c=resistor('mid','right','C'))
            rows=(1,2) if dual else (2,1)
            cells=dict(a=(rows[0],1),b=(rows[0],4),u=(rows[1],1),v=(rows[1],4),r=(1,2),c=(1,3))
            pl,comps,nets,index,plan=installed(devices,cells)
            relation,=plan['conduction_taps']['groups']
            trial,_=next(taps.tap_trials(pl,comps,index,plan,relation,allow_row_split=True))
            self.assertTrue(trial.valid_state())
            self.assertEqual(taps.measure_taps(trial,comps,index,plan['conduction_taps'])['errors'],0)
            self.assertEqual(_static_faults(structured.par,trial,nets,index,RoutingOptions(),None)['count'],0)

    def test_missing_host_changed_terminals_and_nonfacing_geometry_are_not_perfect(self):
        f=fixture();pl,comps,nets,index,plan=f
        for mode in ('missing','changed','nonfacing'):
            class View:
                def pin_world(self,i,p):return pl.pin_world(i,p)
                def pin_dir(self,i,p):return 'north' if mode=='nonfacing' else pl.pin_dir(i,p)
            changed=deepcopy(comps);idx=dict(index)
            if mode=='missing':idx.pop('u')
            if mode=='changed':changed[index['u']]['nodes']['S']='other'
            metric=taps.measure_taps(View(),changed,idx,model(f))
            self.assertFalse(metric['measurable']);self.assertIsNone(metric['errors']);self.assertTrue(metric['missing'])

    def test_third_conduction_terminal_and_control_only_end_do_not_create_unique_taps(self):
        f=fixture();plan=deepcopy(f[4])
        plan['readability_plan']['junctions']['left']['conduction_ports'].append(['extra','D'])
        self.assertEqual(taps.compile_taps(f[1],plan)['groups'],[])
        plan=deepcopy(f[4]);plan['host_axes']=[a for a in plan['host_axes'] if a['net']!='left']
        self.assertEqual(taps.compile_taps(f[1],plan)['groups'],[])

    def test_renaming_reordering_translation_and_mirror_keep_measurement(self):
        f=fixture();pl,comps,nets,index,plan=f
        relation,=model(f)['groups'];trial,_=next(taps.tap_trials(pl,comps,index,plan,relation,allow_row_split=True))
        expected=taps.measure_taps(trial,comps,index,model(f))
        class View:
            def pin_world(self,i,p):
                x,y=trial.pin_world(i,p);return 180-x,y+43
            def pin_dir(self,i,p):return trial.pin_dir(i,p)
        mirrored=taps.measure_taps(View(),comps,index,model(f))
        self.assertEqual((mirrored['errors'],mirrored['outside_distance']),
                         (expected['errors'],expected['outside_distance']))
        names={c['id']:'device'+str(30-k) for k,c in enumerate(comps)}
        altered=deepcopy(comps)
        for c in altered:
            c['id']=names[c['id']];c['nodes']={pin:('signal_'+net if net not in ('0','vdd') else net)
                                             for pin,net in c['nodes'].items()}
        renamed=deepcopy(model(f))
        for rel in renamed['groups']:
            rel['members']=[names[n] for n in rel['members']]
            for e in rel['ends']:
                e['net']='signal_'+e['net'];e['hosts']=[(names[n],p) for n,p in e['hosts']]
                e['guest']=(names[e['guest'][0]],e['guest'][1])
        metric=taps.measure_taps(trial,altered,{names[n]:i for n,i in index.items()},renamed)
        self.assertEqual((metric['errors'],metric['outside_distance']),
                         (expected['errors'],expected['outside_distance']))
        self.assertEqual(taps.compile_taps(list(reversed(comps)),plan),model(f))
        reordered=list(reversed(altered));old_index={names[n]:i for n,i in index.items()}
        class ReorderedView:
            def pin_world(self,i,p):return trial.pin_world(old_index[reordered[i]['id']],p)
            def pin_dir(self,i,p):return trial.pin_dir(old_index[reordered[i]['id']],p)
        metric=taps.measure_taps(ReorderedView(),reordered,
            {c['id']:i for i,c in enumerate(reordered)},renamed)
        self.assertEqual((metric['errors'],metric['outside_distance']),
                         (expected['errors'],expected['outside_distance']))

    def test_old_objective_tie_can_accept_strictly_better_access_without_new_sa_weights(self):
        f=fixture(sense=True);pl,comps,nets,index,plan=f
        baseline=integration.measure_attachments(pl,comps,index,plan)
        budget=integration.new_attachment_budget(max_candidates=1)
        report=dict(actions=[],candidate_trials=0,state_legal_trials=0,legal_trials=0)
        result=integration._tap_slot_stage(structured.par,pl,pl,comps,nets,index,plan,
            RoutingOptions(),budget,perf_counter()+1,report,initialization=True)
        self.assertEqual(len(report['actions']),1)
        after=integration.measure_attachments(result,comps,index,plan)
        self.assertEqual(integration.attachment_rank(after),integration.attachment_rank(baseline))
        self.assertEqual(taps.measure_taps(result,comps,index,model(f))['errors'],0)
        self.assertEqual(budget['candidates'],1)
        common=dict(metrics=baseline)
        old=dict(common,conduction_tap_access=taps.measure_taps(pl,comps,index,model(f)))
        new=dict(common,conduction_tap_access=taps.measure_taps(result,comps,index,model(f)))
        self.assertLess(organization_rank(new),organization_rank(old))

    def test_deadline_and_static_rejection_leave_original_axes_and_rng_intact(self):
        f=fixture();pl,comps,nets,index,plan=f;before=snapshot(pl)
        relation,=model(f)['groups']
        self.assertEqual(list(taps.tap_trials(pl,comps,index,plan,relation,
            allow_row_split=True,deadline=perf_counter()-1)),[])
        for response in (False,None):
            budget=integration.new_attachment_budget();report=dict(actions=[],candidate_trials=0,
                state_legal_trials=0,legal_trials=0)
            with patch.object(integration,'_checked_static',return_value=response):
                result=integration._tap_slot_stage(structured.par,pl,pl,comps,nets,index,plan,
                    RoutingOptions(),budget,perf_counter()+1,report,initialization=True)
            self.assertIs(result,pl);self.assertEqual(report['actions'],[])
            self.assertEqual(snapshot(pl),before)

    def test_no_dynamic_row_insertion_during_many_sa_proposals_or_undo(self):
        f=fixture();pl,comps,nets,index,plan=f;relation,=model(f)['groups']
        current,_=next(taps.tap_trials(pl,comps,index,plan,relation,allow_row_split=True))
        axes=deepcopy((current.column_x,current.row_y))
        for _ in range(100):
            state=list(current.cell_of),list(current.orient)
            undo=current.propose()
            if undo is not None:
                self.assertTrue(current.valid_state());current.set_state(*undo)
            self.assertEqual((current.cell_of,current.orient),state)
            self.assertEqual((current.column_x,current.row_y),axes)
