"""A real geometry oracle for optional port-to-core whitespace compression."""
from copy import deepcopy
from time import perf_counter
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from netlist2schematic.common.sa_common import load_local
from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.plugins.region import region_compaction as compact
from netlist2schematic.routing.routing_rules import build_scene
from netlist2schematic.routing.sa_routing import validate_final_routes, RoutingValidationError
from netlist2schematic.routing.orthogonal_router import RoutingFailure


def fixture(*, mirror=False, shift=0, names=('A','B'), independent=False):
    """Two finite MOS symbols, real fixed exits and power markers; no A*."""
    from tests.test_region_layout import mos
    doc=to_document({names[0]:mos('link','first','gnd'), names[1]:mos('link','second','gnd')})
    comps,nets=doc['components'],doc['nets']
    index={c['id']:i for i,c in enumerate(comps)}
    par=load_local('place_and_route','place-and-route.py')
    pl=par.Placement.__new__(par.Placement)
    pl.comps=comps
    pl.column_x=[64+shift,364+shift]
    pl.row_y=[150]
    pl.cell_of=[(0,1),(0,0)] if mirror else [(0,0),(0,1)]
    pl.orient=[1 if mirror else 0]*2
    pl.comp_at={cell:i for i,cell in enumerate(pl.cell_of)}
    pl.offset_x=[0.,0.]
    pl.rows,pl.cols=1,2
    scene=build_scene(par,nets,pl,index)
    routes=[(net,list(segments)) for net,segments in scene.fixed_segments.items() if net not in scene.power_nets]
    exits=[pin.escape for pin in scene.pins['link']]
    dict(routes)['link'].append(tuple(exits))
    result=validate_final_routes(par,nets,pl,index,routes)
    wing=dict(id='wing',members=[names[0]],children=[],parent='parent',
        attachments=[dict(device=names[0],pin='D',net='link',target_region='core')])
    if independent:wing['attachments']=[]
    core=dict(id='core',members=[names[1]],children=[],parent='parent')
    plan=dict(supported=True,signature='two-real-terminals',regions=[wing,core],
        ownership={names[0]:'wing',names[1]:'core'},coverage={},series_axes=[])
    return par,pl,comps,nets,index,result,plan


def run(f,**kwargs):
    par,pl,comps,nets,index,result,plan=f
    return compact.compact_region_routes(par,pl,comps,nets,index,result,plan,**kwargs)


class RegionCompactionTests(unittest.TestCase):
    def test_closes_only_proven_attachment_gap_with_full_independent_geometry(self):
        f=fixture()
        old=deepcopy((f[1].__dict__,f[5],f[6]))
        with patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('A*')):
            pl,result,report=run(f)
        self.assertTrue(report['applied'])
        self.assertLess(result[1]['wirelength'],f[5][1]['wirelength'])
        self.assertEqual(result[1]['crossings'],f[5][1]['crossings'])
        self.assertEqual(result[1]['bends'],f[5][1]['bends'])
        self.assertEqual(pl.cell_of,f[1].cell_of)
        self.assertEqual(pl.orient,f[1].orient)
        self.assertEqual(pl.row_y,f[1].row_y)
        self.assertEqual((f[1].__dict__,f[5],f[6]),old)
        checked=validate_final_routes(f[0],f[3],pl,f[4],result[0])
        self.assertEqual(checked[1],result[1])
        for i in range(2):
            dx=pl.pos_of(i)[0]-f[1].pos_of(i)[0]
            for pin in f[2][i]['nodes']:
                before,after=f[1].pin_world(i,pin),pl.pin_world(i,pin)
                self.assertEqual(after,(before[0]+dx,before[1]))

    def test_independent_regions_keep_their_channel(self):
        f=fixture(independent=True)
        pl,result,report=run(f)
        self.assertIs(pl,f[1])
        self.assertEqual(result[1],f[5][1])
        self.assertFalse(report['applied'])
        self.assertEqual(report['candidates'],0)

    def test_fixed_world_position_is_not_compressed_and_quota_is_bounded(self):
        f=fixture()
        f[1].fixed={1}
        pl,result,report=run(f,max_candidates=1)
        self.assertFalse(report['applied'])
        self.assertEqual(pl.pos_of(1),f[1].pos_of(1))
        self.assertEqual(report['rejections'],['fixed_position'])
        self.assertEqual(report['stop_reason'],'candidate_limit')
        self.assertEqual(result[1],f[5][1])

    def test_renaming_mirror_and_translation_preserve_legal_improvement(self):
        outcomes=[]
        for mirrored,shift,names in ((False,0,('A','B')),(True,200,('zeta','alpha'))):
            f=fixture(mirror=mirrored,shift=shift,names=names)
            pl,result,report=run(f)
            self.assertTrue(report['applied'])
            self.assertTrue(result[2]['valid'])
            outcomes.append(result[1])
        self.assertEqual(outcomes[0],outcomes[1])

    def test_original_bad_routes_are_not_trusted_from_valid_flag(self):
        f=list(fixture())
        routes,metrics,validation=deepcopy(f[5])
        routes=[(net,segments) for net,segments in routes if net!='link']
        f[5]=(routes,metrics,validation)
        with self.assertRaises(RoutingValidationError):run(f)

    def test_too_close_mapped_tracks_are_rejected_by_original_oracle(self):
        f=list(fixture())
        routes=deepcopy(dict(f[5][0]))
        scene=build_scene(f[0],f[3],f[1],f[4])
        # Two real foreign tracks have exactly the legal 10-unit spacing.
        # Compressing their strip would violate that spacing, at any trial size.
        for net,x,y in [('first',174,280),('second',184,310)]:
            q=scene.pins[net][0].escape
            routes[net]+=[(q,(q[0],y)),((q[0],y),(x,y)),((x,y),(x,400))]
        f[5]=validate_final_routes(f[0],f[3],f[1],f[4],list(routes.items()))
        pl,result,report=run(f)
        self.assertIs(pl,f[1])
        self.assertFalse(report['applied'])
        self.assertGreater(report['candidates'],0)
        self.assertTrue(all(s=='complete_geometry_rejected' for s in report['rejections']))
        self.assertEqual(result[1],f[5][1])

    def test_search_history_stays_explicitly_before_compression(self):
        f=list(fixture())
        f[5][2].update(search={'old_coordinates':[64,364]},routing_seconds=.25)
        _,result,report=run(f,prevalidated=True)
        self.assertTrue(report['applied'])
        self.assertEqual(result[2]['search'],f[5][2]['search'])
        self.assertEqual(result[2]['routing_seconds'],.25)
        self.assertEqual(result[2]['search_geometry_stage'],'before_region_compaction')

    def test_timeout_discards_unverified_trial_and_keeps_valid_incumbent(self):
        f=fixture()
        original=compact.validate_final_routes
        count=0
        def expire(*args,**kwargs):
            nonlocal count
            count+=1
            if count>1:raise RoutingFailure('deadline',{'reason':'quality_time_budget'})
            return original(*args,**kwargs)
        with patch.object(compact,'validate_final_routes',side_effect=expire):
            pl,result,report=run(f)
        self.assertIs(pl,f[1])
        self.assertEqual(result[1],f[5][1])
        self.assertEqual(report['stop_reason'],'time_budget')
        self.assertFalse(report['applied'])

    def test_zero_budget_does_not_try_a_transform_but_still_validates_saved_input(self):
        f=fixture()
        with patch.object(compact,'validate_final_routes',wraps=compact.validate_final_routes) as verify:
            pl,result,report=run(f,seconds=0)
        self.assertEqual(verify.call_count,1)
        self.assertIs(pl,f[1])
        self.assertEqual(report['candidates'],0)
        self.assertEqual(report['stop_reason'],'time_budget')

    def test_deadline_does_not_reset_or_allow_late_trial(self):
        f=fixture()
        pl,result,report=run(f,prevalidated=True,deadline=perf_counter()-1)
        self.assertIs(pl,f[1])
        self.assertEqual(result[1],f[5][1])
        self.assertEqual(report['stop_reason'],'time_budget')

    def test_once_converged_reapplication_is_stable(self):
        f=list(fixture())
        pl,result,report=run(f)
        f[1],f[5]=pl,result
        again,routed,repeat=run(f)
        self.assertEqual(again.column_x,pl.column_x)
        self.assertEqual(routed[1],result[1])
        self.assertFalse(repeat['applied'])

    def test_saved_grid_redraw_and_dual_svg_keep_the_compacted_real_pins(self):
        from netlist2schematic.common.sa_saved import load_placement
        from netlist2schematic.flow.run_flow import _finish,_saved_positions
        from netlist2schematic.rendering.render_clean import render
        from tools.evaluate_goal_layouts import verify
        f=fixture(names=('M0','M1'))
        pl,result,report=run(f)
        self.assertTrue(report['applied'])
        with TemporaryDirectory(prefix='region-coordinate-save-') as folder:
            root=Path(folder)
            source=root/'source.cir'
            source.write_text('.model NMOS nmos\nM0 link first gnd lo NMOS\nM1 link second gnd lo NMOS\n.end\n',encoding='utf8')
            data=dict(source_netlist=str(source),source_cell=None,mode='layered',
                placement=_saved_positions(pl,f[2]),layout_grid=dict(column_x=pl.column_x,row_y=pl.row_y),
                structs=[],layout_regions=dict(plan=f[6],route_compaction=report))
            with (patch('netlist2schematic.flow.run_flow.final_route',side_effect=AssertionError('rerouted')),
                  redirect_stdout(io.StringIO())):
                _finish(data,f[0],pl,f[2],f[3],f[4],'source',root/'out',
                        routed_result=result,simplify_routes='off',protected_paths={source})
                saved=root/'out/source-placement.json'
                restored=load_placement(saved)
                checked=verify(saved)
                render(saved,root/'redraw.svg')
            self.assertEqual(restored[3].column_x,pl.column_x)
            self.assertEqual(restored[0]['placement'],data['placement'])
            self.assertEqual(checked['metrics'],result[1])
            self.assertTrue(checked['dual_svg_routes_identical'])
            self.assertEqual(json.loads(saved.read_text('utf8'))['metrics'],result[1])


if __name__=='__main__':
    unittest.main()
