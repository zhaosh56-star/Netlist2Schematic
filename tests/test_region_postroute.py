"""Keep earlier repairs ahead of channel compression and respect saved poses."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_region_compaction import fixture
from netlist2schematic.flow import aesthetic_flow,run_flow
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.routing.orthogonal_router import RoutingFailure


def metadata(f,**kwargs):
    return dict(candidate_id=2,structs=[],layout_grid=dict(column_x=f[1].column_x,row_y=f[1].row_y),
                layout_regions=dict(policy='auto',applied=True,plan=deepcopy(f[6])),**kwargs)


def finish(f,meta=None,**kwargs):
    return aesthetic_flow._postroute_regions(f[0],f[1],f[2],f[3],f[4],
        meta if meta is not None else metadata(f),f[5],options=RoutingOptions(),
        deadline=perf_counter()+2,**kwargs)


class RegionPostrouteTests(unittest.TestCase):
    def test_old_repairs_precede_channels_and_all_three_share_time_and_trials(self):
        f=fixture(independent=True);events=[];deadlines=[];quotas=[]
        meta=metadata(f,role_adjustments=[dict(device='A',kind='protected')]);before=deepcopy(meta)
        def compactor(par,pl,comps,nets,index,result,plan,**kw):
            mode=kw.get('channel_mode','attachments');events.append(mode)
            deadlines.append(kw['deadline']);self.assertTrue(kw['prevalidated'])
            if mode=='independent':quotas.append(kw['max_candidates'])
            return pl,result,dict(applied=False,candidates=3 if mode=='attachments' else 4)
        def controls(par,pl,comps,nets,index,result,plan,**kw):
            events.append('controls');deadlines.append(kw['deadline']);quotas.append(kw['max_candidates'])
            self.assertEqual(kw['protected_ids'],['A']);self.assertLessEqual(kw['seconds'],1)
            return pl,result,dict(applied=False,candidates=2)
        with patch('netlist2schematic.plugins.region.region_compaction.compact_region_routes',side_effect=compactor), \
             patch('netlist2schematic.plugins.region.region_route_ports.regularize_control_routes',side_effect=controls):
            pl,new,result=finish(f,meta)
        self.assertEqual(events,['attachments','controls','independent'])
        self.assertEqual(quotas,[9,7]);self.assertEqual(len(set(deadlines)),1)
        self.assertEqual(new['layout_regions']['postroute_budget']['candidates'],9)
        self.assertEqual(meta,before);self.assertEqual(result[1],f[5][1])

    def test_new_step_cannot_steal_earlier_repairs_trial_allowance(self):
        f=fixture(independent=True);events=[]
        def compactor(*args,**kw):
            self.assertNotIn('channel_mode',kw);events.append('attachments')
            return args[1],args[5],dict(applied=False,candidates=11)
        def controls(*args,**kw):
            events.append('controls');self.assertEqual(kw['max_candidates'],1)
            return args[1],args[5],dict(applied=False,candidates=1)
        with patch('netlist2schematic.plugins.region.region_compaction.compact_region_routes',side_effect=compactor), \
             patch('netlist2schematic.plugins.region.region_route_ports.regularize_control_routes',side_effect=controls):
            _,meta,_=finish(f)
        self.assertEqual(events,['attachments','controls'])
        self.assertEqual(meta['layout_regions']['channel_compaction']['stop_reason'],'candidate_limit')
        self.assertEqual(meta['layout_regions']['postroute_budget']['candidates'],12)

    def test_earlier_time_exhaustion_skips_new_search_and_keeps_valid_routes(self):
        f=fixture(independent=True);clock=[0.0];events=[]
        def compactor(*args,**kw):
            clock[0]=1.01;events.append('attachments')
            return args[1],args[5],dict(applied=False,candidates=2)
        def controls(*args,**kw):
            self.assertEqual(kw['seconds'],0);events.append('controls')
            return args[1],args[5],dict(applied=False,candidates=0)
        with patch.object(aesthetic_flow,'perf_counter',side_effect=lambda:clock[0]), \
             patch('netlist2schematic.plugins.region.region_compaction.compact_region_routes',side_effect=compactor), \
             patch('netlist2schematic.plugins.region.region_route_ports.regularize_control_routes',side_effect=controls):
            pl,meta,routes=aesthetic_flow._postroute_regions(*f[:5],metadata(f),f[5],
                options=RoutingOptions(),deadline=10)
        self.assertEqual(events,['attachments','controls']);self.assertIs(pl,f[1])
        self.assertEqual(meta['layout_regions']['channel_compaction']['stop_reason'],'time_budget')
        self.assertEqual(routes[1],f[5][1])

    def test_unapplied_reference_off_bus_and_missing_plan_never_start_cleanup(self):
        f=fixture(independent=True)
        cases=[metadata(f),metadata(f),metadata(f),metadata(f),metadata(f),{}]
        cases[0]['candidate_id']=1
        cases[1]['layout_regions']['applied']=False
        cases[2]['layout_regions']['policy']='off'
        cases[3]['layout_regions']['plan']['supported']=False
        cases[4]['routing_stage']='bus_fallback'
        with patch('netlist2schematic.plugins.region.region_compaction.compact_region_routes',
                   side_effect=AssertionError('unexpected cleanup')):
            for meta in cases:
                with self.subTest(meta=meta):
                    pl,new,routes=finish(f,meta)
                    self.assertIs(pl,f[1]);self.assertIs(new,meta);self.assertIs(routes,f[5])

    def test_real_postroute_preserves_roles_then_updates_measured_coordinates(self):
        f=fixture(independent=True);original=deepcopy(f[1].__dict__)
        with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('A*')):
            pl,meta,routes=finish(f)
        self.assertTrue(meta['layout_regions']['channel_compaction']['applied'])
        self.assertFalse(meta['layout_regions']['route_compaction']['applied'])
        self.assertEqual(meta['layout_grid']['column_x'],pl.column_x)
        self.assertLess(routes[1]['wirelength'],f[5][1]['wirelength'])
        self.assertEqual(f[1].__dict__,original)
        self.assertLessEqual(meta['layout_regions']['postroute_budget']['candidates'],12)

    def test_default_rescue_and_saved_coordinate_callers_do_not_reorganize(self):
        f=fixture(independent=True);meta=metadata(f)
        result=(f[1],f[2],f[3],f[4],meta,f[5]);stages=[]
        with patch('netlist2schematic.recovery.local_rescue.rescue_routing',return_value=deepcopy(result)), \
             patch.object(aesthetic_flow,'_postroute_regions',side_effect=AssertionError('saved positions changed')), \
             redirect_stdout(io.StringIO()):
            output=run_flow._try_local_rescue(tuple(result[:5]),{},RoutingOptions(),'auto',1,stages)
        self.assertEqual(output[0].column_x,f[1].column_x)
        self.assertNotIn('channel_compaction',output[4]['layout_regions'])

    def test_fresh_region_rescue_has_the_same_bounded_cleanup_before_export(self):
        f=fixture(independent=True,names=('M0','M1'));meta=metadata(f)
        meta['quality']=dict(score=999,details={},candidates=[dict(candidate_id='2',
            metrics=deepcopy(f[5][1]),score=999)])
        result=(f[1],f[2],f[3],f[4],meta,f[5])
        with TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source.cir'
            original=b'.model NMOS nmos\nM0 link first gnd lo NMOS\nM1 link second gnd lo NMOS\n.end\n'
            source.write_bytes(original)
            exc=RoutingFailure('normal cutoff',{'reason':'time_budget'})
            exc.retry_context=deepcopy(result[:5]);exc.retry_options=RoutingOptions()
            exc._rescue_context={'marker':object()}
            with patch.object(run_flow,'_normal_result',side_effect=exc), \
                 patch('netlist2schematic.recovery.local_rescue.rescue_routing',return_value=deepcopy(result)), \
                 patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('A*')), \
                 redirect_stdout(io.StringIO()):
                saved=run_flow.run(source,iterations=0,candidate_id='2',region_layout='auto',
                    fallback='off',local_rescue='auto',rescue_seconds=2,simplify_routes='off',out_dir=root/'result')
            self.assertEqual(saved['routing_stage'],'local_rescue')
            self.assertTrue(saved['layout_regions']['channel_compaction']['applied'])
            self.assertEqual(saved['layout_regions']['postroute_budget']['stage'],'local_rescue')
            self.assertEqual(saved['local_rescue']['geometry_stage'],'before_region_postroute')
            self.assertLess(saved['metrics']['wirelength'],f[5][1]['wirelength'])
            self.assertEqual(source.read_bytes(),original)
            self.assertEqual(saved['quality']['details']['wirelength'],saved['metrics']['wirelength'])
            self.assertEqual(saved['quality']['candidates'][0]['metrics'],saved['metrics'])
            self.assertEqual(saved['quality']['candidates'][0]['score'],saved['quality']['score'])
            self.assertTrue(saved['local_rescue']['postroute_layout_changed'])
            self.assertEqual(saved['layout_regions']['postroute_quality']['before']['candidates'][0]['metrics'],
                             f[5][1])
