"""Generic three-port controller and exact sensing-bank organization."""
from copy import deepcopy
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_passive_placement import mos,resistor,convert
from netlist2schematic.plugins.attachment import attachment_controllers as controllers
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
from netlist2schematic.routing.routing_rules import RoutingOptions


def fixture(*,rename=False,bjt=False):
    devices=dict(a=mos('shared','drive'),b=mos('shared','drive'),
        r=resistor('sense','shared'),s=resistor('shared','sense'),
        controller=dict(cls='VCVS',type='E',op='drive',on='0',ip='sense',
            **{'in':'reference'},value='100k'),other=resistor('unrelated','0'))
    cells=dict(a=(3,3),b=(3,4),r=(2,2),s=(2,1),controller=(1,1),other=(0,7))
    orientation=dict(r=0,s=0)
    if bjt:
        for name in ('a','b'):
            devices[name]=dict(cls='BJT',type='NPN',c='shared',b='drive',e='0',model='NPN')
    if rename:
        keys={n:'part'+str(17-k) for k,n in enumerate(devices)}
        devices={keys[n]:{k:('node_'+v if k in ('d','g','s','c','b','e','a','op','ip','in')
            and v not in ('0','vdd') else v) for k,v in row.items()}
            for n,row in reversed(list(devices.items()))}
        cells={keys[n]:cell for n,cell in cells.items()}
        orientation={keys[n]:o for n,o in orientation.items()}
    pl,c,n,i,_=convert(devices,cells,orientation)
    plan=ai.build_attachment_plan(devices,c,n,i,analyze_structures(devices),compute_levels(devices),pl.row_of)
    pl=ai._install(pl,c,i,plan)
    bank=plan['controller_clusters']['groups'][0]['members']
    for name in bank:
        j=i[name]
        sense=next(pin for pin,net in c[j]['nodes'].items() if net==plan['controller_clusters']['groups'][0]['sense_net'])
        for o in pl.allowed_orientations(j):
            pl.orient[j]=o
            if pl.pin_dir(j,sense)=='south':break
    return pl,c,n,i,plan


def finish(pl,c,n,i,plan,*,budget=None,until=None,safe=True):
    budget=budget or ai.new_attachment_budget();report=dict(candidates=0,accepted=0,actions=[])
    until=perf_counter()+2 if until is None else until
    with patch.object(ai,'_checked_static',return_value=safe):
        result=controllers.finish_controllers(structured.par,pl,c,n,i,plan,RoutingOptions(),budget,
            until,report,hpwl_limit=_hpwl(structured.par,pl,n,i)*1.15)
    return result,budget,report


class ControllerClusterTests(unittest.TestCase):
    def test_contract_uses_three_real_ports_and_never_claims_cmfb_or_pairs(self):
        pl,c,n,i,plan=fixture();model=plan['controller_clusters']
        self.assertEqual(len(model['groups']),1)
        group=model['groups'][0]
        self.assertEqual(set(group['members']),{'r','s'})
        self.assertEqual(group['input_pin'],'IN+')
        self.assertEqual(set(map(tuple,group['controls'])),{('a','G'),('b','G')})
        self.assertNotIn('functional_type',group)
        self.assertNotIn('owned_members',group)

    def test_shared_clock_or_bias_without_exact_bank_does_not_create_cluster(self):
        _,c,_,_,plan=fixture()
        self.assertEqual(controllers.compile_controllers(c,dict(plan,groups=[]))['groups'],[])

    def test_unknown_output_user_or_another_driver_blocks_contract(self):
        _,c,_,_,plan=fixture()
        for added in (dict(id='extra',symbol='resistor',nodes={'1':'drive','2':'else'}),
                      dict(id='extra',symbol='opamp',nodes={'OUT':'drive','IN+':'x','IN-':'y'})):
            with self.subTest(added=added['symbol']):
                self.assertEqual(controllers.compile_controllers(c+[added],plan)['groups'],[])

    def test_sensing_input_must_have_no_unaccounted_connection(self):
        _,c,_,_,plan=fixture()
        extra=dict(id='extra',symbol='resistor',nodes={'1':'sense','2':'else'})
        self.assertEqual(controllers.compile_controllers(c+[extra],plan)['groups'],[])

    def test_supply_only_or_control_only_far_end_is_not_a_channel_host(self):
        _,c,_,_,plan=fixture()
        for pin in ('G','S'):
            changed=deepcopy(c)
            for row in changed:
                if row['symbol'] in controllers.ACTIVE:
                    row['nodes']={pin:'shared','D':'another'} if pin!='D' else row['nodes']
            self.assertEqual(controllers.compile_controllers(changed,plan)['groups'],[])

    def test_two_input_banks_are_ambiguous_and_not_arbitrarily_chosen(self):
        _,c,_,_,plan=fixture();c=deepcopy(c);plan=deepcopy(plan)
        c.extend([dict(id='p',symbol='resistor',nodes={'1':'reference','2':'other_channel'}),
                  dict(id='q',symbol='capacitor',nodes={'1':'other_channel','2':'reference'}),
                  dict(id='extra',symbol='nmos',nodes={'D':'other_channel','G':'drive','S':'0'})])
        g=deepcopy(plan['groups'][0]);g.update(id='second',members=['p','q'],owner_junctions=['reference','other_channel'])
        plan['groups'].append(g)
        result=controllers.compile_controllers(c,plan)
        self.assertEqual(result['groups'],[])
        self.assertEqual(result['skipped'][0]['reason'],'ambiguous_sensing_banks')

    def test_renaming_line_order_and_bjt_keep_port_contracts_equivalent(self):
        for renamed in (False,True):
            for bjt in (False,True):
                pl,c,n,i,plan=fixture(rename=renamed,bjt=bjt)
                model=plan['controller_clusters']
                self.assertEqual(len(model['groups']),1)
                measured=controllers.measure_controllers(pl,c,i,model)
                self.assertTrue(measured['measurable'])
                self.assertEqual(measured['access_errors'],6)

    def test_measurement_is_invariant_under_translation_and_full_world_reflection(self):
        pl,c,n,i,plan=fixture();model=plan['controller_clusters']
        original=controllers.measure_controllers(pl,c,i,model)
        class Reflected:
            def pin_world(self,j,p):
                x,y=pl.pin_world(j,p);return 20-x,30-y
            def pin_dir(self,j,p):
                return dict(north='south',south='north',east='west',west='east')[pl.pin_dir(j,p)]
        after=controllers.measure_controllers(Reflected(),c,i,model)
        for key in ('access_errors','backward_distance','port_distance','output_span'):
            self.assertAlmostEqual(after[key],original[key])

    def test_changed_terminal_or_nonfinite_geometry_is_unknown_not_zero(self):
        pl,c,n,i,plan=fixture();model=plan['controller_clusters'];changed=deepcopy(c)
        changed[i['controller']]['nodes']['OUT']='wrong'
        self.assertIsNone(controllers.controller_rank(controllers.measure_controllers(pl,changed,i,model)))
        with patch.object(pl,'pin_world',return_value=(float('nan'),1)):
            self.assertIsNone(controllers.controller_rank(controllers.measure_controllers(pl,c,i,model)))
        self.assertFalse(controllers.measure_controllers(pl,c,i,dict(groups=[]))['measurable'])

    def test_long_backtracking_is_not_rewarded_for_having_fewer_bad_terminals(self):
        old=dict(count=1,measurable=True,access_errors=2,backward_distance=1,port_distance=5,output_span=4)
        far=dict(old,access_errors=1,backward_distance=5,output_span=8)
        self.assertLess(controllers.controller_rank(old),controllers.controller_rank(far))

    def test_trial_generator_preserves_rows_axes_hosts_rng_and_original_state(self):
        pl,c,n,i,plan=fixture();g=plan['controller_clusters']['groups'][0]
        before=deepcopy((pl.cell_of,pl.orient,pl.column_x,pl.row_y,pl.rng.getstate()))
        with patch('netlist2schematic.routing.orthogonal_router.route_scene',side_effect=AssertionError('Astar forbidden')):
            trials=list(controllers.controller_trials(pl,c,i,g))
        self.assertTrue(trials)
        self.assertTrue(any(t.cell_of[i['controller']]!=pl.cell_of[i['controller']]
            and any(t.cell_of[i[n]]!=pl.cell_of[i[n]] for n in g['members']) for t,_ in trials))
        for trial,action in trials:
            self.assertEqual([r for r,col in trial.cell_of],[r for r,col in pl.cell_of])
            self.assertEqual((trial.column_x,trial.row_y,trial.rng.getstate()),(pl.column_x,pl.row_y,pl.rng.getstate()))
            for name in ('a','b','other'):
                self.assertEqual((trial.cell_of[i[name]],trial.orient[i[name]]),(pl.cell_of[i[name]],pl.orient[i[name]]))
            self.assertEqual(trial.orient[i['controller']],pl.orient[i['controller']])
        self.assertEqual((pl.cell_of,pl.orient,pl.column_x,pl.row_y,pl.rng.getstate()),before)

    def test_fixed_controller_or_bank_and_deadline_have_no_trials(self):
        for name in ('controller','r'):
            pl,c,n,i,plan=fixture();pl.fixed.add(i[name])
            self.assertEqual(list(controllers.controller_trials(pl,c,i,plan['controller_clusters']['groups'][0])),[])
        pl,c,n,i,plan=fixture()
        self.assertEqual(list(controllers.controller_trials(pl,c,i,plan['controller_clusters']['groups'][0],deadline=0)),[])

    def test_finish_commits_only_legal_improvement_using_shared_trial_ledger(self):
        pl,c,n,i,plan=fixture();before=controllers.measure_controllers(pl,c,i,plan['controller_clusters'])
        result,budget,report=finish(pl,c,n,i,plan)
        self.assertGreater(report['accepted'],0)
        self.assertTrue(result.valid_state())
        self.assertLess(controllers.controller_rank(controllers.measure_controllers(result,c,i,plan['controller_clusters'])),
            controllers.controller_rank(before))
        self.assertLessEqual(budget['candidates'],8)
        self.assertEqual(budget['candidates'],report['candidates'])
        self.assertEqual(budget['max_candidates'],128)

    def test_static_rejection_and_timeout_return_original_without_partial_changes(self):
        for safe in (False,None):
            pl,c,n,i,plan=fixture();before=deepcopy((pl.cell_of,pl.orient))
            result,budget,report=finish(pl,c,n,i,plan,safe=safe)
            self.assertIs(result,pl)
            self.assertEqual((pl.cell_of,pl.orient),before)
            self.assertEqual(report['accepted'],0)
            if safe is None:self.assertTrue(budget['exhausted'])

    def test_no_remaining_quota_or_expired_time_never_opens_a_new_budget(self):
        for expired in (False,True):
            pl,c,n,i,plan=fixture();budget=ai.new_attachment_budget()
            if not expired:budget['candidates']=128
            result,budget,report=finish(pl,c,n,i,plan,budget=budget,until=0 if expired else None)
            self.assertIs(result,pl);self.assertEqual(report['candidates'],0)
            self.assertEqual(report['controller_finish']['reason'],'shared_attachment_budget')

    def test_static_oracle_receives_moved_true_pins_and_interiors_remain_valid(self):
        pl,c,n,i,plan=fixture();seen=[]
        def safe(par,trial,nets,index,options,until):
            seen.append(deepcopy(trial.cell_of))
            return ai._static(par,trial,nets,index,options,until)
        budget=ai.new_attachment_budget();report=dict(candidates=0,accepted=0,actions=[])
        with patch.object(ai,'_checked_static',side_effect=safe):
            result=controllers.finish_controllers(structured.par,pl,c,n,i,plan,RoutingOptions(),budget,
                perf_counter()+2,report,hpwl_limit=_hpwl(structured.par,pl,n,i)*1.15)
        self.assertTrue(seen)
        self.assertTrue(result.valid_state())
        self.assertGreater(report['accepted'],0)

    def test_finish_order_is_core_then_controller_then_vacant_band_compaction(self):
        import inspect
        source=inspect.getsource(ai._refine_attachments)
        self.assertLess(source.index('current=_core_access_finish'),source.index('current=finish_controllers'))
        self.assertLess(source.index('current=finish_controllers'),source.index('current=finish_compaction'))

    def test_common_saved_measurement_restores_ports_without_repositioning(self):
        from netlist2schematic.plugins.attachment.attachment_selection import measure_candidate
        pl,c,n,i,plan=fixture();before=deepcopy((pl.cell_of,pl.orient,pl.column_x,pl.row_y))
        metadata=measure_candidate(pl,c,i,dict(candidate_id='2'),plan)
        measured=metadata['layout_attachments']['controller_organization']
        self.assertTrue(measured['measurable']);self.assertEqual(measured['count'],1)
        self.assertEqual((pl.cell_of,pl.orient,pl.column_x,pl.row_y),before)


if __name__=='__main__':unittest.main()
