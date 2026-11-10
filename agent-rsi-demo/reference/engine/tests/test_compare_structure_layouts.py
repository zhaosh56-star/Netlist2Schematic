import importlib.util
from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

spec=importlib.util.spec_from_file_location('structure_compare',Path(__file__).resolve().parents[1]/'tools/compare_structure_layouts.py')
comparison=importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


def verified(length=100,crossings=0,interleaving=0,stage='normal'):
    return {'metrics':{'wirelength':length,'crossings':crossings},
        'measurement':{'interleaving':interleaving,'role_order':0},'routing_stage':stage}


class FinalOutputGateTests(unittest.TestCase):
    def test_row_plan_checks_tail_strict_order_and_same_layer(self):
        from types import SimpleNamespace
        pl=SimpleNamespace(cell_of=[(1,0),(1,1),(2,0)])
        idx={'a':0,'b':1,'tail':2}
        plan={'rows':{'a':1,'b':1,'tail':2},'tail_orders':[{'lower':'tail'}],
            'strict_orders':[{'upper':'a','lower':'tail'}],
            'same_row':[{'members':['a','b']}]}
        comparison.verify_row_plan({'branch_row_plan':plan},pl,idx,{'a','b'})
        pl.cell_of[2]=(1,2)
        with self.assertRaises(ValueError):comparison.verify_row_plan({'branch_row_plan':plan},pl,idx,{'a','b'})
    def test_final_length_and_crossings_are_independently_bounded(self):
        self.assertTrue(all(comparison.gates(verified(crossings=1),verified(115,2)).values()))
        self.assertFalse(comparison.gates(verified(),verified(116))['wirelength_115_percent'])
        self.assertFalse(comparison.gates(verified(),verified(crossings=1))['crossings_limit'])

    def test_new_bus_or_structure_regression_blocks_release(self):
        gates=comparison.gates(verified(),verified(interleaving=1,stage='bus_fallback'))
        self.assertFalse(gates['no_new_bus_fallback'])
        self.assertFalse(gates['no_new_structure_violations'])

    def test_asymmetric_parse_skip_cannot_be_mistaken_for_success(self):
        with tempfile.TemporaryDirectory() as folder:
            cases=[{'case':'x'}]
            records={('x','off'):{'status':'skipped','reason':'input_validation_error'},
                     ('x','auto'):{'status':'failed','error':'bad'}}
            report=comparison.publish(Path(folder),cases,records,{}, {}, {})
            self.assertFalse(report['release_gates']['paired_status_consistent'])
            self.assertFalse(report['release_gates']['no_routing_failures'])

    def test_incomplete_batch_cannot_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            report=comparison.publish(Path(folder),[{'case':'x'}],{}, {}, {}, {})
            self.assertFalse(report['release_gates']['complete'])
            self.assertFalse(report['release_gates']['paired_status_consistent'])


def evidence_fixture():
    return {'version':'test-evidence-v1',
            'structures':[{'id':'neutral:a|b','kind':'complementary_common_drain','devices':['a','b']}],
            'relations':[{'id':'new:a|b','kind':'complementary_common_drain',
                          'members':['a','b'],'roles':{'upper':'a','lower':'b'},
                          'layout':{'branches':[['a','b']]},'owned_members':[]}],
            'diagnostics':{'shared_source_banks':[{'reason':'ambiguous_formal_controls','source':'tail'}],
                           'supply_conflicts':['mixed'], 'downgraded_outputs':['old-output']},
            'branch_evidence':{'skipped_branches':[{'reason':'non_unique_channel_join'}]}}


class UnifiedEvidenceComparisonTests(unittest.TestCase):
    def test_saved_evidence_is_preferred_and_deeply_copied(self):
        saved=evidence_fixture()
        metadata={'layout_evidence':saved,'candidate_id':1}
        before=deepcopy(metadata)
        with mock.patch('layout_intent.build_intent',side_effect=AssertionError('do not replace saved evidence')):
            result=comparison._comparison_evidence('unused.cir',None,{},[],[],{},metadata)
        self.assertEqual(saved,result)
        result['relations'][0]['members'].append('other')
        self.assertEqual(before,metadata)

    def test_bad_saved_evidence_is_not_silently_replaced_with_old_recognition(self):
        for saved in ([],{}, {'structures':[],'relations':None}):
            with self.subTest(saved=saved):
                with self.assertRaisesRegex(ValueError,'layout_evidence'):
                    comparison._comparison_evidence('unused.cir',None,{},[],[],{},
                                                      {'layout_evidence':saved})

    def test_off_rebuild_uses_source_intent_and_never_saved_candidate_coordinates(self):
        evidence=evidence_fixture()
        devices={'a':{'cls':'MOS'}}
        original=[{'kind':'output_pair','devices':['a','b']}]
        comps=[{'id':'a'},{'id':'b'}]
        metadata={'placement':{'a':{'row':100}},'branch_row_plan':{'bridge_alignment':['candidate-specific']}}
        before=deepcopy(metadata)
        with mock.patch('layout_intent.build_intent',return_value=(evidence,[])) as builder:
            result=comparison._comparison_evidence('source.cir','dut',devices,original,comps,{},metadata,phase=2)
        builder.assert_called_once_with('source.cir','dut',devices,original,comps,{}, {'a':0,'b':1},phase=2)
        self.assertEqual(evidence,result)
        self.assertEqual(before,metadata)

    def test_corrected_relations_reach_measurement_after_branch_context_without_mutation(self):
        evidence=evidence_fixture()
        original=[{'id':'old-output','kind':'output_pair','devices':['a','b']}]
        metadata={'layout_evidence':evidence,'layout_relations':[{'kind':'obsolete'}],
                  'structs':original,'candidate_id':1}
        before=deepcopy(metadata)
        relations=deepcopy(evidence['relations'])
        measured={'relation_count':1,'measured_blocks':0,'branch_geometry':{'hard_error':17}}
        pl=SimpleNamespace(cell_of=[(1,1),(2,1)],orient=[0,0])
        snapshot=deepcopy(vars(pl))
        with mock.patch('layout_relations.build_layout_relations',return_value=relations) as build, \
                mock.patch('structure_selection.prepare_branch_context',side_effect=lambda *args:args[-1]) as prepare, \
                mock.patch('structure_selection.measure_structure',return_value=measured) as measure:
            context,result,summary=comparison._measure_comparison(
                'source.cir','dut',None,pl,[{'id':'a'},{'id':'b'}],{}, {'a':0,'b':1},
                {'a':{},'b':{}},original,metadata,3)
        self.assertEqual(evidence,build.call_args.kwargs['validated_evidence'])
        self.assertEqual(relations,prepare.call_args.args[-1]['layout_relations'])
        self.assertEqual(relations,measure.call_args.args[-2]['layout_relations'])
        self.assertNotIn('output_pair',summary['structure_kinds'])
        self.assertEqual('report_only',summary['consumption']['policy'])
        self.assertEqual(measured,result)
        self.assertEqual(snapshot,vars(pl))
        self.assertEqual(before,metadata)
        context['layout_evidence']['relations'][0]['members'].append('changed')
        self.assertEqual(before,metadata)

    def test_phase_one_uses_same_evidence_without_branch_geometry_pass(self):
        metadata={'layout_evidence':evidence_fixture()}
        with mock.patch('layout_relations.build_layout_relations',return_value=[]), \
                mock.patch('structure_selection.prepare_branch_context',side_effect=AssertionError('phase 1')), \
                mock.patch('structure_selection.measure_structure',return_value={'relation_count':0}):
            _,result,summary=comparison._measure_comparison(
                'source.cir',None,None,None,[],{}, {},{},[],metadata,1)
        self.assertEqual({'relation_count':0},result)
        self.assertEqual('saved',summary['source'])

    def test_off_and_saved_auto_use_the_same_calibrated_measurement_catalog(self):
        evidence=evidence_fixture()
        saved=deepcopy(evidence)
        saved['consumption']={'policy':'report_only','candidate_id':1,'stages':[]}
        structs=[{'kind':'output_pair','devices':['a','b']}]
        def measure(*args):
            relations=args[-2]['layout_relations']
            return {'relation_count':len(relations),'kinds':[row['kind'] for row in relations]}
        with mock.patch('layout_intent.build_intent',return_value=(evidence,[])) as source, \
                mock.patch('layout_relations.build_layout_relations',
                           side_effect=lambda *args,**kw:deepcopy(kw['validated_evidence']['relations'])), \
                mock.patch('structure_selection.measure_structure',side_effect=measure):
            off=comparison._measure_comparison('source',None,None,None,[],{}, {},{},structs,{},1)
            auto=comparison._measure_comparison('source',None,None,None,[],{}, {},{},structs,
                                                {'layout_evidence':saved},1)
        source.assert_called_once()
        self.assertEqual(off[1],auto[1])
        self.assertEqual(off[0]['layout_relations'],auto[0]['layout_relations'])
        self.assertEqual({'output_pair':1},off[2]['baseline_structure_kinds'])
        self.assertEqual({'complementary_common_drain':1},off[2]['structure_kinds'])

    def test_stage_one_freezes_the_presentation_reader_used_for_formal_evidence(self):
        files={str(Path('D:/project')/name):'hash' for name in (
            'layout_evidence.py','layout_intent.py','presentation.py','branch_levels.py','tests/test_x.py')}
        with mock.patch.object(comparison.common,'_snapshot',return_value=files):
            snapshot=comparison.runtime_snapshot(1)
        self.assertTrue(any(Path(path).name=='presentation.py' for path in snapshot))
        self.assertTrue(any(Path(path).name=='layout_evidence.py' for path in snapshot))
        self.assertFalse(any(Path(path).name=='branch_levels.py' for path in snapshot))

    def test_summary_does_not_equate_recognition_or_applied_constraints_with_device_motion(self):
        evidence=evidence_fixture()
        evidence['consumption']={'policy':'candidate_layout','candidate_id':2,
            'stages':[{'stage':'initial_blocks','relation_ids':['new:a|b'],'status':'applied'}],
            'position_changes':[]}
        summary=comparison._recognition_summary(evidence,{'relation_count':1,'measured_blocks':0},{},source='saved')
        self.assertEqual(3,summary['ambiguity_count'])
        self.assertEqual({'complementary_common_drain':1},summary['structure_kinds'])
        self.assertEqual(evidence['consumption'],summary['consumption'])
        self.assertEqual([],summary['consumption']['position_changes'])
        self.assertIn('does not prove',summary['consumption_note'])
        summary['consumption']['stages'][0]['status']='changed'
        self.assertEqual('applied',evidence['consumption']['stages'][0]['status'])

    def test_detour_summary_retains_complete_per_net_data_and_labels_its_scope(self):
        quality={'detour':40,'outside_length':20,'detour_reference':'terminal_half_perimeter_bounding_box',
                 'per_net':{'signal':{'wirelength':140,'hpwl':100,'detour_factor':1.4,
                                     'excess_detour':40,'bends':3},
                            'single':{'detour_factor':None,'excess_detour':0}}}
        summary=comparison._detour_summary(quality)
        self.assertEqual('whole_net',summary['scope'])
        self.assertEqual(1.4,summary['maximum_factor'])
        self.assertEqual(quality['per_net'],summary['per_net'])
        self.assertEqual(40,summary['excess_detour'])
        summary['per_net']['signal']['hpwl']=1
        self.assertEqual(100,quality['per_net']['signal']['hpwl'])
        self.assertIsNone(comparison._detour_summary({'per_net':{'single':{'detour_factor':None}}})['maximum_factor'])

    def test_unsupported_x_instances_remain_skipped_before_any_flow(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'unsupported.cir'
            path.write_text('X1 a b c d e missing_model\n',encoding='utf-8')
            with mock.patch.object(comparison.subprocess,'Popen',side_effect=AssertionError('no flow')):
                result=comparison.execute({'case':'bad','netlist':str(path)},'auto',Path(folder),{}, {}, {})
        self.assertEqual('skipped',result['status'])
        self.assertEqual('input_validation_error',result['reason'])

    def test_publish_preserves_recognition_and_branch_and_whole_net_details(self):
        value=verified()
        value.update(metrics={'wirelength':100,'crossings':0,'bends':2},width=200,height=100,
                     clean_svg='clean.svg',placement_json='saved.json',
                     recognition_summary={'structure_kinds':{'differential_pair':1},
                                          'consumption':{'policy':'report_only','stages':[]}},
                     route_detours={'scope':'whole_net','excess_detour':20,'maximum_factor':1.25,
                                    'per_net':{'n':{'detour_factor':1.25}}})
        value['measurement']['branch_geometry']={'hard_error':13}
        records={(key,mode):{'status':'valid','signature':{'source_files':{}},
                             'verified':deepcopy(value),'elapsed_seconds':1}
                 for key in ('example',) for mode in ('off','auto')}
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)
            for record in records.values():
                record['verified']['placement_json']=str(output/'saved.json')
                record['verified']['clean_svg']=str(output/'clean.svg')
            comparison.publish(output,[{'case':'example'}],records,{}, {}, {})
            report=json.loads((output/'comparison.json').read_text(encoding='utf-8'))
            page=(output/'index.html').read_text(encoding='utf-8')
        saved=report['cases'][0]['results']['off']['verified']
        self.assertEqual({'hard_error':13},saved['measurement']['branch_geometry'])
        self.assertEqual('report_only',saved['recognition_summary']['consumption']['policy'])
        self.assertEqual(1.25,saved['route_detours']['per_net']['n']['detour_factor'])
        self.assertIn('最大整网绕行比',page)
        self.assertIn('recognition',page)
        self.assertIn('不是某条支路',page)

    def test_real_saved_off_result_is_measured_with_source_rebuilt_evidence_and_identical_routes(self):
        import run_flow
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'small.cir'
            source.write_text('R1 inp out 1k\nR2 out 0 2k\n',encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                original=run_flow.run(source,iterations=0,seed=13,layout_style='legacy',
                    fallback='off',quality_seconds=5,simplify_seconds=0,out_dir=root/'result')
            saved=root/'result/small-placement.json'
            before=saved.read_bytes()
            signature={'source_netlist':str(source.resolve()),'cell':None,'parameters':{'structure_phase':3}}
            with contextlib.redirect_stdout(io.StringIO()):
                result=comparison.verify(saved,signature)
            self.assertTrue(result['valid'])
            self.assertEqual('source_rebuild',result['recognition_summary']['source'])
            self.assertEqual('report_only',result['recognition_summary']['consumption']['policy'])
            self.assertEqual(original['metrics'],result['metrics'])
            self.assertEqual(original['placement'],result['placement'])
            self.assertEqual(before,saved.read_bytes())
            self.assertIn('per_net',result['route_detours'])
            self.assertTrue(result['layout_evidence']['relations'])
            self.assertIn('branch_geometry',result['measurement'])
            annotated=json.loads(before)
            annotated['layout_evidence']=deepcopy(result['layout_evidence'])
            annotated['layout_evidence']['consumption']={'policy':'report_only','candidate_id':1,'stages':[]}
            saved.write_text(json.dumps(annotated),encoding='utf-8')
            annotated_before=saved.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()):
                with_saved=comparison.verify(saved,signature)
            self.assertEqual('saved',with_saved['recognition_summary']['source'])
            self.assertEqual(result['measurement'],with_saved['measurement'])
            self.assertEqual(result['quality'],with_saved['quality'])
            self.assertEqual(result['placement'],with_saved['placement'])
            self.assertEqual(annotated_before,saved.read_bytes())


if __name__=='__main__':unittest.main()
