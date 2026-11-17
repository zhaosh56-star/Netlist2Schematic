"""Fresh-netlist, layout-preserving recovery and independent label verification."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from netlist2schematic.common.sa_common import load_local
from netlist2schematic.common.sa_saved import load_placement, saved_routing_options
from netlist2schematic.flow import run_flow
from netlist2schematic.recovery import labeled_recovery as recovery
from netlist2schematic.recovery import labeled_geometry as geometry
from netlist2schematic.rendering.render_clean import render
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.routing_rules import RoutingOptions, build_scene, analyze_routes
from time import perf_counter


class LabelRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.folder=Path(self.temporary.name);self.source=self.folder/'unknown.cir'
        self.source.write_text('R1 input output 1k\nC1 output 0 1n\n.end\n',encoding='utf-8')
        self.par=load_local('place_and_route','place-and-route.py')
        with redirect_stdout(io.StringIO()):
            self.normal=run_flow._normal_result(self.source,self.par,'layered','structured',
                None,0,17,10,'1',None)

    def recover(self,context=None,seconds=5):
        with patch.object(recovery,'_route_net',side_effect=RoutingFailure('blocked',{'reason':'no_path'})), \
             patch.object(recovery,'_nearby',side_effect=lambda scene,routes,*args:routes), \
             patch('netlist2schematic.recovery.fallback_bus.create_fallback',side_effect=AssertionError('bus forbidden')):
            return recovery.recover(context or self.normal[:5],source=self.source,seconds=seconds)

    def export(self,result):
        placement,comps,nets,index,metadata,routed=result
        data=dict(metadata,source_netlist=str(self.source),mode='layered',
                  placement=run_flow._saved_positions(placement,comps))
        with redirect_stdout(io.StringIO()):
            return run_flow._finish(data,self.par,placement,comps,nets,index,'unknown',self.folder/'out',
                routed_result=routed,simplify_routes='off')

    def check(self,data,view=None):
        result=self.last
        return geometry.validate_view(self.par,result[2],result[0],result[3],
            data if view is None else view,options=RoutingOptions())

    def test_unknown_input_uses_only_current_layout_and_never_reads_a_saved_result(self):
        original=deepcopy(self.normal[0].cell_of),deepcopy(self.normal[0].orient)
        with patch('netlist2schematic.common.sa_saved.load_placement',side_effect=AssertionError('history read')):
            result=self.recover()
        self.assertEqual('labeled_recovery',result[4]['routing_stage'])
        self.assertEqual(original,(result[0].cell_of,result[0].orient))
        self.assertTrue(result[4]['labeled_recovery']['physical_layout_preserved'])
        self.assertEqual(2,result[5][2]['label_count'])
        self.assertFalse(result[5][2]['wire_only_complete'])
        json.dumps(result[4])

    def test_save_redraw_reroute_and_simplify_preserve_the_same_current_layout(self):
        data=self.export(self.recover());saved=self.folder/'out/unknown-placement.json'
        with patch('netlist2schematic.routing.orthogonal_router.route_scene',side_effect=AssertionError('no Astar replay')), \
             patch('netlist2schematic.flow.run_flow.final_route',side_effect=AssertionError('no rerouting')), \
             redirect_stdout(io.StringIO()):
            render(saved,self.folder/'redrawn.svg')
            repeated=run_flow.reroute_placement(saved,out_dir=self.folder/'rerouted')
            simplified=run_flow.simplify_placement(saved,out_dir=self.folder/'simplified')
        for field in ('placement','routes','metrics','route_representation'):
            self.assertEqual(data[field],repeated[field],field)
            self.assertEqual(data[field],simplified[field],field)
        self.assertEqual((self.folder/'out/unknown-clean.svg').read_bytes(),(self.folder/'redrawn.svg').read_bytes())

    def test_missing_wrong_duplicate_or_floating_labels_are_rejected(self):
        self.last=self.recover();view=self.last[4]['labeled_recovery']['view']
        changes=(lambda v:v['labels'].pop(),
                 lambda v:v['labels'][0].update(text='wrong'),
                 lambda v:v['labels'].append(deepcopy(v['labels'][0])),
                 lambda v:v['labels'][0].update(anchor=[9999,9999]),
                 lambda v:v['labels'][0].update(net='unknown'),
                 lambda v:v['groups'][0].update(terminals=[]),
                 lambda v:v['labels'][0].update(bounds=[0,0,1,1]),
                 lambda v:v['display_metrics'].update(wirelength=0))
        for change in changes:
            with self.subTest(change=change):
                altered=deepcopy(view);change(altered)
                with self.assertRaises(ValueError):self.check(altered)

    def test_original_pin_exit_cannot_be_removed_even_if_labels_are_present(self):
        self.last=self.recover();view=deepcopy(self.last[4]['labeled_recovery']['view'])
        view['base_routes']['output']=[]
        with self.assertRaisesRegex(ValueError,'fixed pin exit'):self.check(view)

    def test_saved_flags_do_not_establish_correctness_and_route_tampering_is_rejected(self):
        data=self.export(self.recover());saved=self.folder/'out/unknown-placement.json'
        data['routes']['output']=[];data['validation']['valid']=True
        saved.write_text(json.dumps(data),encoding='utf-8')
        with self.assertRaises(ValueError):
            with redirect_stdout(io.StringIO()):render(saved,self.folder/'bad.svg')

    def test_include_changes_are_rejected_without_changing_root_netlist(self):
        include=self.folder/'models.inc';include.write_text('* original\n',encoding='utf-8')
        self.source.write_text('.include models.inc\nR1 input output 1k\nC1 output 0 1n\n.end\n',encoding='utf-8')
        self.export(self.recover());include.write_text('* changed\n',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'source files changed'):
            render(self.folder/'out/unknown-placement.json',self.folder/'bad.svg')

    def test_more_than_four_groups_are_covered_without_dropping_any_terminal(self):
        self.source.write_text(''.join(f'R{i} shared leaf{i} 1k\n' for i in range(6))+'.end\n',encoding='utf-8')
        pn=load_local('parse_netlist','parse-netlist.py');doc=pn.parse(self.source)
        comps,nets=doc['components'],doc['nets'];index={c['id']:i for i,c in enumerate(comps)}
        pl=self.par.Placement.__new__(self.par.Placement);pl.comps=comps
        pl.cell_of=[(0,i) for i in range(6)];pl.comp_at={c:i for i,c in enumerate(pl.cell_of)}
        pl.orient=[0]*6;pl.column_x=[150+240*i for i in range(6)];pl.row_y=[150]
        result=self.recover((pl,comps,nets,index,dict(candidate_id=2)))
        labels=result[4]['labeled_recovery']['view']['labels']
        self.assertEqual(6,len(labels));self.assertEqual({'shared'},{x['text'] for x in labels})
        self.assertEqual(6,len({x['group_id'] for x in labels}))
        self.assertTrue(result[5][2]['valid'])

    def test_expansion_preserves_logical_rows_columns_and_orientations(self):
        self.source.write_text('R1 a b 1k\nR2 c d 1k\n.end\n',encoding='utf-8')
        pn=load_local('parse_netlist','parse-netlist.py');doc=pn.parse(self.source)
        comps,nets=doc['components'],doc['nets'];index={c['id']:i for i,c in enumerate(comps)}
        pl=self.par.Placement.__new__(self.par.Placement);pl.comps=comps
        pl.cell_of=[(0,0),(0,1)];pl.comp_at={c:i for i,c in enumerate(pl.cell_of)}
        pl.orient=[0,0];pl.column_x=[150,170];pl.row_y=[150]
        before=deepcopy(pl.cell_of),deepcopy(pl.orient)
        result=self.recover((pl,comps,nets,index,dict(candidate_id=2)))
        self.assertEqual(before,(result[0].cell_of,result[0].orient))
        self.assertGreater(result[4]['labeled_recovery']['scale'],1)
        self.assertTrue(result[5][2]['valid'])

    def test_bad_input_or_no_layout_fails_without_constructing_a_bus(self):
        with patch('netlist2schematic.recovery.fallback_bus.create_fallback',side_effect=AssertionError('bus')):
            with self.assertRaises(RoutingFailure):
                recovery.recover(None,source=self.source,seconds=1)
            with self.assertRaisesRegex(ValueError,'Bus generation is disabled'):
                run_flow.run(self.source,fallback='only')

    def test_normal_success_does_not_invoke_label_recovery(self):
        with patch.object(run_flow,'_normal_result',return_value=deepcopy(self.normal)), \
             patch.object(run_flow,'_try_label_recovery',side_effect=AssertionError('unnecessary labels')), \
             redirect_stdout(io.StringIO()):
            data=run_flow.run(self.source,out_dir=self.folder/'normal',simplify_routes='off')
        self.assertEqual('normal',data['routing_stage']);self.assertNotIn('labeled_recovery',data)

    def test_failed_native_stages_reach_labels_and_never_a_bus(self):
        failure=RoutingFailure('blocked',dict(reason='no_path'));failure.retry_context=deepcopy(self.normal[:5])
        with patch.object(run_flow,'_normal_result',side_effect=failure), \
             patch.object(recovery,'_route_net',side_effect=RoutingFailure('blocked',dict(reason='no_path'))), \
             patch.object(recovery,'_nearby',side_effect=lambda scene,routes,*args:routes), \
             patch('netlist2schematic.recovery.spacing_retry.retry_spacing',side_effect=RoutingFailure('spacing',dict(reason='no_path'))), \
             patch('netlist2schematic.recovery.fallback_bus.create_fallback',side_effect=AssertionError('bus forbidden')), \
             redirect_stdout(io.StringIO()):
            data=run_flow.run(self.source,out_dir=self.folder/'result',local_rescue='off',
                spacing_seconds=1,label_seconds=5,simplify_routes='off')
        self.assertEqual('labeled_recovery',data['routing_stage'])
        self.assertTrue(data['recovery']['bus_disabled'])
        self.assertEqual(['normal','local_rescue','spacing_retry','labeled_recovery'],
                         [s['stage'] for s in data['recovery']['stages']])

    def test_hard_net_yields_and_later_easy_network_is_still_completed(self):
        self.source.write_text('R1 a x1 1k\nR2 a x2 1k\nR3 b x3 1k\nR4 b x4 1k\n.end\n',encoding='utf-8')
        doc=load_local('parse_netlist','parse-netlist.py').parse(self.source)
        comps,nets=doc['components'],doc['nets'];index={c['id']:i for i,c in enumerate(comps)}
        pl=self.par.Placement.__new__(self.par.Placement);pl.comps=comps
        pl.cell_of=[(0,i) for i in range(4)];pl.comp_at={c:i for i,c in enumerate(pl.cell_of)}
        pl.orient=[0]*4;pl.column_x=[150+180*i for i in range(4)];pl.row_y=[150]
        scene=build_scene(self.par,nets,pl,index)
        routes=recovery._seed(scene,pl,None,perf_counter()+5)
        visited=[]
        def attempt(scene,net,*args,**kwargs):
            visited.append(net)
            if net=='a':raise RoutingFailure('hard',{'reason':'state_budget'})
            first,last=scene.pins[net]
            return [first.stub,last.stub,(first.escape,last.escape)],{}
        report={'net_attempts':[]}
        with patch.object(recovery,'_route_net',side_effect=attempt):
            recovery._continue(scene,routes,None,comps,perf_counter()+5,report)
        self.assertEqual(['a','b'],visited)
        self.assertEqual(['deferred','connected'],[row['status'] for row in report['net_attempts']])
        self.assertEqual([],analyze_routes(list(routes.items()),scene)['violations'])

    def test_changed_include_or_resource_fingerprint_is_rejected(self):
        include=self.folder/'models.inc';include.write_text('* models\n',encoding='utf-8')
        self.source.write_text('.include "models.inc"\nR1 input output 1k\nC1 output 0 1n\n.end\n',encoding='utf-8')
        data=self.export(self.recover());saved=self.folder/'out/unknown-placement.json'
        d,_,par,pl,comps,nets,index=load_placement(saved)
        with patch('netlist2schematic.plugins.net_labels.net_labels._resource_hashes',return_value={'other':'hash'}):
            with self.assertRaisesRegex(ValueError,'resource set changed'):
                geometry.verify_saved(par,nets,pl,index,d,options=saved_routing_options(d))
        include.write_text('* changed\n',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'source files changed'):
            geometry.verify_saved(par,nets,pl,index,d,options=saved_routing_options(d))

    def test_subdivision_preserves_electrical_groups_and_metrics(self):
        result=self.recover();view=deepcopy(result[4]['labeled_recovery']['view'])
        for net,lines in view['base_routes'].items():
            view['base_routes'][net]=[part for a,b in lines
                for middle in [((a[0]+b[0])/2,(a[1]+b[1])/2)] for part in ((a,middle),(middle,b))]
        _,metrics,valid=geometry.validate_view(self.par,result[2],result[0],result[3],view)
        self.assertTrue(valid['valid']);self.assertEqual(result[5][1],metrics)

    def test_tiny_budget_is_finite_and_cannot_trigger_bus(self):
        with patch('netlist2schematic.recovery.fallback_bus.create_fallback',side_effect=AssertionError('bus')):
            with self.assertRaises(RoutingFailure):self.recover(seconds=0.000001)
        for seconds in (0,-1,float('nan')):
            with self.assertRaises(ValueError):self.recover(seconds=seconds)


if __name__=='__main__':unittest.main()
