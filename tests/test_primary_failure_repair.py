"""Normal primary results are protected; only actual failures use unused time."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import tempfile
from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from netlist2schematic.flow import aesthetic_flow,run_flow
from netlist2schematic.recovery import primary_recovery
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.common.sa_common import load_local
from tests.test_candidate_rescue import snapshot
from tests.test_reserved_flow import candidate,valid


class PrimaryRepairTests(unittest.TestCase):
    def test_spacing_cannot_displace_an_explicitly_fixed_device(self):
        from tests.test_passive_modules import fixture
        _,core,comps,nets,index,_=fixture()
        core.fixed.add(0)
        own_index={c['id']:i for i,c in enumerate(core.comps)}
        context=(core,core.comps,{},own_index,{})
        self.assertFalse(primary_recovery._spacing_allowed(context,RoutingOptions(),1.25))

    def test_real_failed_packing_resumes_its_saved_frame_without_annealing(self):
        from netlist2schematic.layout import structured_placement as sp
        from netlist2schematic.plugins.passive_skeleton import candidates
        actual=candidates.pack
        def fail_once(*args,**kwargs):
            packed,report=actual(*args,**kwargs)
            self.assertIsNotNone(packed)
            report.update(applied=False,stop_reason='local_budget')
            return None,report
        failures=[]
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'circuit.cir'
            path.write_text('.model N NMOS\nM1 out inp 0 0 N\nR1 vdd out 1k\n.end\n',encoding='utf8')
            with patch.object(candidates,'pack',side_effect=fail_once):
                generated=list(sp.create_candidates(path,iterations=0,candidate_ids=(2,),
                    passive_skeleton='auto',packing_failures=failures))
            self.assertIsNone(generated[0][0]);self.assertEqual(len(failures),1)
            with patch.object(sp,'_anneal',side_effect=AssertionError('SA restarted')):
                resumed=failures[0]['resume'](perf_counter()+3)
        self.assertIsNotNone(resumed[0])
        self.assertEqual(resumed[4]['candidate_id'],2)
        self.assertTrue(resumed[4]['passive_skeleton']['primary_packing_resume']['no_sa_restart'])
        json.dumps(resumed[4])

    def test_only_primary_contexts_are_ranked_even_if_reference_has_more_progress(self):
        rows=[snapshot(1,completed=('a','b')),snapshot(3,completed=('b',)),snapshot(2,completed=('a',))]
        self.assertEqual([row['candidate_id'] for row in primary_recovery.rank(rows)],['2','3'])
        self.assertEqual(primary_recovery.context_for_recovery(rows)['candidate_id'],'2')

    def test_shared_deadline_first_verified_result_and_failed_transactions_do_not_mutate_source(self):
        rows=[snapshot(2),snapshot(3)]
        original=deepcopy([row['context'][4] for row in rows])
        clock=SimpleNamespace(now=100.)
        calls=[]
        def local(context,checkpoint,options,policy,seconds,stages,**kwargs):
            ident=context[0].identifier;calls.append((ident,kwargs['deadline']))
            clock.now+=seconds
            context[4]['private_trial']=True
            if ident==2:
                stages.append(dict(stage='local_rescue',status='failed'))
                return None
            stages.append(dict(stage='local_rescue',status='valid'))
            return (*context,valid())
        def spacing(context,**kwargs):
            self.assertLessEqual(kwargs['deadline'],110.)
            clock.now=kwargs['deadline']
            raise RoutingFailure('blocked',dict(reason='no_path'))
        with patch.object(primary_recovery,'perf_counter',side_effect=lambda:clock.now), \
                patch.object(run_flow,'_try_local_rescue',side_effect=local), \
                patch('netlist2schematic.recovery.spacing_retry.retry_spacing',side_effect=spacing):
            result,report,runtime=primary_recovery.repair(rows,deadline=120.)
        self.assertEqual(result[4]['candidate_id'],3)
        self.assertEqual(result[4]['routing_stage'],'primary_repair')
        self.assertEqual([i for i,_ in calls],[2,3])
        self.assertLessEqual(clock.now,120.)
        self.assertEqual([row['context'][4] for row in rows],original)
        self.assertEqual(report['selected_candidate'],'3')
        json.dumps(report)

    def test_expired_budget_never_calls_search_or_resumes_packing(self):
        row=dict(candidate_id='2',kind='packing',context=None,checkpoint=None,
                 resume=lambda until:(_ for _ in ()).throw(AssertionError('expired resume')),options=RoutingOptions())
        with patch.object(primary_recovery,'perf_counter',return_value=10.), \
                patch.object(primary_recovery,'final_route',side_effect=AssertionError('expired route')):
            result,report,_=primary_recovery.repair([row],deadline=9.)
        self.assertIsNone(result)
        self.assertEqual(report['stop_reason'],'time_budget')

    def test_resume_uses_runtime_module_checkpoint_without_new_annealing(self):
        context=candidate(2)
        seen=[]
        def resume(until):
            seen.append(until)
            return deepcopy(context)
        row=dict(candidate_id='2',kind='packing',context=None,checkpoint=None,
                 resume=resume,options=RoutingOptions())
        with patch.object(primary_recovery,'final_route',return_value=valid()), \
                patch('netlist2schematic.layout.structured_placement._anneal',side_effect=AssertionError('SA restarted')), \
                patch('netlist2schematic.layout.layout_quality.evaluate_quality',return_value=dict(score=1)):
            result,report,_=primary_recovery.repair([row],deadline=perf_counter()+2)
        self.assertIsNotNone(result)
        self.assertEqual(len(seen),1)
        self.assertEqual(report['attempts'][0]['action'],'resume_module_insertion')


class MainFlowProtectionTests(unittest.TestCase):
    def test_controlled_normal_failures_recover_with_real_geometry_and_no_candidate_one(self):
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/'circuit.cir'
            source.write_text('.model N NMOS\nM1 out inp 0 0 N\nR1 vdd out 1k\n.end\n',encoding='utf8')
            failure=RoutingFailure('controlled normal-search rejection',dict(reason='no_path'))
            with patch.object(aesthetic_flow,'final_route',side_effect=failure) as normal_router, \
                    redirect_stdout(StringIO()):
                data=run_flow.run(source,iterations=0,quality_seconds=5,fallback='off',
                    local_rescue='off',out_dir=Path(temporary)/'out',simplify_routes='off')
            self.assertEqual(normal_router.call_count,2)
            self.assertEqual(data['routing_stage'],'primary_repair')
            self.assertIn(str(data['quality']['selected_candidate']),('2','3'))
            self.assertTrue(data['validation']['valid'])
            self.assertFalse(data['primary_repair']['reference_enabled'])
            self.assertEqual(data['recovery']['stages'][0]['status'],'failed')
            self.assertNotIn('resume',json.dumps(data['primary_repair']))

    def test_normal_success_never_enters_primary_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/'circuit.cir';source.write_text('R1 in out 1k\n.end\n',encoding='utf8')
            with redirect_stdout(StringIO()):
                normal=run_flow._normal_result(source,load_local('place_and_route','place-and-route.py'),
                    'layered','structured',None,0,17,3,'2',None)
            with patch.object(run_flow,'_normal_result',return_value=normal), \
                    patch.object(primary_recovery,'repair',side_effect=AssertionError('normal output changed')), \
                    redirect_stdout(StringIO()):
                result=run_flow.run(source,iterations=0,out_dir=Path(temporary)/'out',simplify_routes='off')
            self.assertTrue(result['validation']['valid'])
            self.assertEqual([s['stage'] for s in result['recovery']['stages']],['normal'])

    def test_repaired_primary_is_delivered_without_independent_recovery_or_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/'circuit.cir';source.write_text('R1 in out 1k\n.end\n',encoding='utf8')
            with redirect_stdout(StringIO()):
                normal=run_flow._normal_result(source,load_local('place_and_route','place-and-route.py'),
                    'layered','structured',None,0,17,3,'2',None)
            failure=RoutingFailure('both failed',dict(reason='primary_failed'))
            failure._primary_repair_deadline=perf_counter()+5
            failure._primary_placements=[dict(candidate_id='2',context=deepcopy(normal[:5]),checkpoint=None,
                                              options=RoutingOptions())]
            repaired=deepcopy(normal);repaired[4]['routing_stage']='primary_repair'
            with patch.object(run_flow,'_normal_result',side_effect=failure), \
                    patch.object(primary_recovery,'repair',return_value=(repaired,dict(status='valid'),failure._primary_placements)), \
                    patch.object(run_flow,'_try_candidate_rescues',side_effect=AssertionError('extra rescue')), \
                    patch.object(run_flow,'_try_local_rescue',side_effect=AssertionError('extra rescue')), \
                    patch('netlist2schematic.recovery.spacing_retry.retry_spacing',side_effect=AssertionError('extra spacing')), \
                    redirect_stdout(StringIO()):
                data=run_flow.run(source,iterations=0,out_dir=Path(temporary)/'out',simplify_routes='off')
            self.assertEqual(data['routing_stage'],'primary_repair')
            self.assertTrue(data['validation']['valid'])
            self.assertEqual([s['stage'] for s in data['recovery']['stages']],['normal','primary_repair'])


if __name__=='__main__':unittest.main()
