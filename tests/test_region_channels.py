"""Independent channel capacity with the original geometry oracle; never A*."""
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_region_compaction import fixture,run
from netlist2schematic.plugins.region import region_compaction as compact
from netlist2schematic.routing.routing_rules import Scene,RoutingOptions,build_scene
from netlist2schematic.routing.sa_routing import validate_final_routes,RoutingValidationError
from netlist2schematic.routing.orthogonal_router import RoutingFailure


class IndependentChannelTests(unittest.TestCase):
    def test_explicit_mode_preserves_a_channel_without_merging_or_distorting_symbols(self):
        f=fixture(independent=True)
        before=deepcopy((f[1].__dict__,f[5],f[6]))
        with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('A*')):
            pl,result,report=run(f,channel_mode='independent')
        self.assertTrue(report['applied'])
        self.assertEqual(report['reference_pitch'],300)
        for action in report['actions']:
            self.assertGreaterEqual(action['remaining_strip'],150)
            self.assertGreaterEqual(action['remaining_strip'],action['reserved_width'])
        self.assertLess(result[1]['wirelength'],f[5][1]['wirelength'])
        self.assertEqual((result[1]['crossings'],result[1]['bends']),
                         (f[5][1]['crossings'],f[5][1]['bends']))
        self.assertEqual((pl.cell_of,pl.orient,pl.row_y,pl.offset_x),
                         (f[1].cell_of,f[1].orient,f[1].row_y,f[1].offset_x))
        for i in range(len(f[2])):
            dx=pl.pos_of(i)[0]-f[1].pos_of(i)[0]
            for pin in f[2][i]['nodes']:
                old,new=f[1].pin_world(i,pin),pl.pin_world(i,pin)
                self.assertEqual(new,(old[0]+dx,old[1]))
        self.assertEqual(validate_final_routes(f[0],f[3],pl,f[4],result[0])[1],result[1])
        self.assertEqual((f[1].__dict__,f[5],f[6]),before)

    def test_old_attachment_scope_does_not_silently_include_independent_regions(self):
        f=fixture(independent=True)
        pl,result,report=run(f)
        self.assertIs(pl,f[1]);self.assertFalse(report['applied'])
        self.assertEqual(report['candidates'],0)
        f=fixture(independent=False)
        pl,result,report=run(f,channel_mode='independent')
        self.assertIs(pl,f[1]);self.assertFalse(report['applied'])
        self.assertEqual(report['candidates'],0)

    def test_track_reservation_ignores_segmentation_and_keeps_custom_clearances(self):
        scene=Scene(options=RoutingOptions(grid_step=3,cross_clearance=13))
        segments=[((x,0),(x,60)) for x in range(30,240,10)]
        divided=[piece for a,b in segments for piece in [(a,(a[0],20)),((a[0],20),b)]]
        whole=compact._channel_reservation(scene,[('arbitrary',segments)],0,300,124)
        split=compact._channel_reservation(scene,[('renamed',divided+[segments[0],((10,2),(10,2))])],0,300,124)
        self.assertEqual(whole,split)
        reserve,tracks=whole
        self.assertEqual(tracks,21)
        self.assertGreaterEqual(reserve,2*13+(tracks-1)*scene.options.center_spacing)
        self.assertEqual(reserve%3,0)

    def test_mirror_translation_instance_and_signal_renames_keep_equivalent_improvement(self):
        outcomes=[]
        for mirrored,shift,names in [(False,0,('A','B')),(True,200,('zeta','alpha'))]:
            f=list(fixture(independent=True,mirror=mirrored,shift=shift,names=names))
            mapping={'link':'changed_signal','first':'external_1','second':'external_2'}
            for c in f[2]:
                c['nodes']={pin:mapping.get(net,net) for pin,net in c['nodes'].items()}
            f[3]={mapping.get(net,net):terminals for net,terminals in f[3].items()}
            routes=[(mapping.get(net,net),segments) for net,segments in f[5][0]]
            f[5]=validate_final_routes(f[0],f[3],f[1],f[4],routes)
            pl,result,report=run(f,channel_mode='independent')
            self.assertTrue(report['applied']);self.assertTrue(result[2]['valid'])
            outcomes.append(result[1])
        self.assertEqual(outcomes[0],outcomes[1])

    def test_mos_polarity_and_bjt_use_the_same_translation_and_geometry_checks(self):
        from tests.test_region_route_ports import fixture as paired
        for symbol in ('nmos','pmos','npn'):
            with self.subTest(symbol=symbol):
                f=list(paired(symbol=symbol))
                names=list(f[4]);f[6].update(ownership={names[0]:'one',names[1]:'two'},
                    regions=[dict(id='one',members=[names[0]],children=[]),
                             dict(id='two',members=[names[1]],children=[])])
                pl,result,report=run(f,channel_mode='independent')
                self.assertTrue(report['applied'])
                self.assertTrue(validate_final_routes(f[0],f[3],pl,f[4],result[0])[2]['valid'])
                self.assertEqual(build_scene(f[0],f[3],pl,f[4]).power_nets,
                                 build_scene(f[0],f[3],f[1],f[4]).power_nets)

    def test_fixed_position_zero_budget_and_deadline_keep_the_original(self):
        for kwargs in (dict(seconds=0),dict(deadline=perf_counter()-1),dict(max_candidates=1)):
            with self.subTest(kwargs=kwargs):
                f=fixture(independent=True);f[1].fixed={1}
                pl,result,report=run(f,channel_mode='independent',
                                     prevalidated='deadline' in kwargs,**kwargs)
                self.assertIs(pl,f[1]);self.assertFalse(report['applied'])
                self.assertEqual(result[1],f[5][1]);self.assertLessEqual(report['candidates'],1)

    def test_capacity_is_not_a_spacing_exemption_and_unverified_trials_roll_back(self):
        f=fixture(independent=True)
        original=compact.validate_final_routes
        calls=[]
        def reject_trial(*args,**kwargs):
            calls.append(args[2])
            if len(calls)>1:
                raise RoutingValidationError('foreign tracks too close',{'reason':'spacing'})
            return original(*args,**kwargs)
        with patch.object(compact,'validate_final_routes',side_effect=reject_trial):
            pl,result,report=run(f,channel_mode='independent')
        self.assertGreater(len(calls),1);self.assertIs(pl,f[1])
        self.assertFalse(report['applied']);self.assertEqual(result[1],f[5][1])

    def test_real_foreign_track_spacing_is_still_authoritative(self):
        f=list(fixture(independent=True))
        routes=deepcopy(dict(f[5][0]));scene=build_scene(f[0],f[3],f[1],f[4])
        for net,x,y in [('first',174,280),('second',184,310)]:
            q=scene.pins[net][0].escape
            routes[net]+=[(q,(q[0],y)),((q[0],y),(x,y)),((x,y),(x,400))]
        f[5]=validate_final_routes(f[0],f[3],f[1],f[4],list(routes.items()))
        pl,result,report=run(f,channel_mode='independent')
        self.assertIn('complete_geometry_rejected',report['rejections'])
        self.assertEqual(validate_final_routes(f[0],f[3],pl,f[4],result[0])[1],result[1])

    def test_reference_pitch_can_be_reused_without_chasing_compressed_axes(self):
        f=list(fixture(independent=True))
        pl,result,report=run(f,channel_mode='independent')
        f[1],f[5]=pl,result
        again,routes,repeat=run(f,channel_mode='independent',reference_pitch=report['reference_pitch'])
        self.assertFalse(repeat['applied'])
        self.assertEqual(again.column_x,pl.column_x);self.assertEqual(routes[1],result[1])

    def test_bad_input_and_invalid_modes_do_not_acquire_a_success_flag(self):
        for mode in ('wrong',None,True):
            with self.assertRaises(ValueError):run(fixture(),channel_mode=mode)
        for pitch in (0,-1,True,float('inf'),float('nan')):
            with self.assertRaises(ValueError):run(fixture(),channel_mode='independent',reference_pitch=pitch)
        f=list(fixture(independent=True));routes,metrics,validation=deepcopy(f[5])
        f[5]=([r for r in routes if r[0]!='link'],metrics,validation)
        with self.assertRaises(RoutingValidationError):run(f,channel_mode='independent')

    def test_saved_compacted_axes_redraw_without_rerouting_or_reapplying_layout(self):
        from netlist2schematic.flow.run_flow import _finish,_saved_positions
        from netlist2schematic.common.sa_saved import load_placement
        from netlist2schematic.rendering.render_clean import render
        from tools.evaluate_goal_layouts import verify
        f=fixture(independent=True,names=('M0','M1'))
        pl,result,report=run(f,channel_mode='independent')
        with TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source.cir'
            source.write_text('.model NMOS nmos\nM0 link first gnd lo NMOS\nM1 link second gnd lo NMOS\n.end\n',encoding='utf8')
            data=dict(source_netlist=str(source),mode='layered',structs=[],
                placement=_saved_positions(pl,f[2]),layout_grid=dict(column_x=pl.column_x,row_y=pl.row_y),
                layout_regions=dict(plan=f[6],channel_compaction=report))
            with redirect_stdout(io.StringIO()), \
                patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('A*')), \
                patch.object(compact,'compact_region_routes',side_effect=AssertionError('relayout')):
                _finish(data,f[0],pl,f[2],f[3],f[4],'source',root/'result',routed_result=result,
                        simplify_routes='off',protected_paths={source})
                path=root/'result/source-placement.json'
                restored=load_placement(path);checked=verify(path);render(path,root/'redraw.svg')
            self.assertEqual(restored[3].column_x,pl.column_x)
            self.assertEqual(restored[0]['layout_regions']['channel_compaction']['reference_pitch'],300)
            self.assertTrue(checked['dual_svg_routes_identical'])
            self.assertEqual(checked['metrics'],result[1])
