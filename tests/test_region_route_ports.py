"""Real-symbol paired-control cleanup freezes all other nets; never A*."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest import TestCase
from unittest.mock import patch

from netlist2schematic.common.sa_common import load_local
from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.plugins.region import region_route_ports as ports
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.routing_rules import build_scene
from netlist2schematic.routing.sa_routing import validate_final_routes, RoutingValidationError
from tests.test_region_layout import mos


def fixture(*, mirror=False, shift=0, names=('A', 'B'), symbol='nmos', blocker=False, crossing=False):
    if symbol == 'npn':
        devices={n:dict(cls='BJT', type='NPN', c='d'+str(k), b='bias', e='gnd') for k,n in enumerate(names)}
    else:
        devices={n:mos('d'+str(k),'bias','gnd' if symbol=='nmos' else 'vdd',
                       polarity=symbol.upper(),bulk='gnd' if symbol=='nmos' else 'vdd')
                 for k,n in enumerate(names)}
    if blocker:
        devices['R']=dict(cls='PASSIVE',type='R',a='ob0',b='ob1',value='1k')
    document=to_document(devices);comps,nets=document['components'],document['nets']
    index={c['id']:i for i,c in enumerate(comps)}
    par=load_local('place_and_route','place-and-route.py')
    pl=par.Placement.__new__(par.Placement);pl.comps=comps
    pl.column_x=([64+shift,214+shift,364+shift] if blocker else [64+shift,364+shift])
    pl.row_y=[150];pl.rows=1;pl.cols=len(pl.column_x)
    cells={names[0]:(0,pl.cols-1 if mirror else 0),names[1]:(0,0 if mirror else pl.cols-1)}
    if blocker:cells['R']=(0,1)
    pl.cell_of=[cells[c['id']] for c in comps]
    pl.orient=[int(mirror) if c['symbol'] in ('nmos','pmos','npn') else 0 for c in comps]
    pl.comp_at={cell:i for i,cell in enumerate(pl.cell_of)};pl.offset_x=[0.0]*len(comps)
    scene=build_scene(par,nets,pl,index)
    routes={net:list(lines) for net,lines in scene.fixed_segments.items() if net not in scene.power_nets}
    first,second=sorted(scene.pins['bias'],key=lambda p:p.escape)
    a,b=first.escape,second.escape;y=270
    routes['bias'] += [(a,(a[0],y)),((a[0],y),(b[0],y)),((b[0],y),b)]
    if crossing:
        pin=scene.pins['d0'][0];p=pin.escape
        x=214+shift
        routes['d0'] += [(p,(x,p[1])),((x,p[1]),(x,210))]
    result=validate_final_routes(par,nets,pl,index,list(routes.items()))
    plan=dict(supported=True,signature='proven-shared-control-pair',
        ownership={n:'pair' for n in names},coverage={},series_axes=[],junctions=[],
        power_interfaces=['gnd','vdd'],regions=[dict(id='pair',kind='local_pair',members=list(names),
                                                     children=[],pairs=[list(names)])])
    return par,pl,comps,nets,index,result,plan


def run(f,**kwargs):
    par,pl,comps,nets,index,result,plan=f
    return ports.regularize_control_routes(par,pl,comps,nets,index,result,plan,**kwargs)


class PairedControlRouteTests(TestCase):
    def test_converges_shared_controls_preserves_true_pins_and_foreign_routes(self):
        f=fixture();before=deepcopy((f[1].__dict__,f[5],f[6]))
        with patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('A*')):
            pl,result,report=run(f)
        self.assertTrue(report['applied'])
        self.assertTrue(result[2]['valid'])
        self.assertLess(result[1]['wirelength'],f[5][1]['wirelength'])
        self.assertLess(result[1]['bends'],f[5][1]['bends'])
        self.assertEqual(result[1]['crossings'],f[5][1]['crossings'])
        self.assertEqual(pl.cell_of,f[1].cell_of)
        self.assertEqual((pl.column_x,pl.row_y),(f[1].column_x,f[1].row_y))
        for i in range(2):
            for pin in ('D','S'):
                self.assertEqual(pl.pin_world(i,pin),f[1].pin_world(i,pin))
        for net,segments in f[5][0]:
            if net!='bias':self.assertEqual(dict(result[0])[net],segments)
        self.assertEqual(report['controls_after']['facing_errors'],0)
        self.assertEqual((f[1].__dict__,f[5],f[6]),before)
        self.assertEqual(validate_final_routes(f[0],f[3],pl,f[4],result[0])[1],result[1])

    def test_shared_bias_alone_and_extra_terminals_do_not_create_pair(self):
        f=list(fixture())
        f[6]['regions'][0]['pairs']=[]
        self.assertFalse(run(f)[2]['applied'])
        f=list(fixture());f[3]['bias'].append((f[2][0]['id'],'D'))
        model=ports.paired_control_relations(f[2],f[3],f[4],f[6])
        self.assertEqual(model['relations'],[])
        self.assertEqual(model['skipped'][0]['reason'],'extra_control_net_terminals')

    def test_builtin_power_alias_is_excluded_even_without_inferred_rail_list(self):
        f=list(fixture())
        for comp in f[2]:comp['nodes']['G']='vdd'
        f[3]['vdd']=f[3].pop('bias')
        f[6]['power_interfaces']=[]
        model=ports.paired_control_relations(f[2],f[3],f[4],f[6])
        self.assertEqual(model['relations'],[])
        self.assertEqual(model['skipped'][0]['reason'],'supply_control')

    def test_fixed_role_protected_and_insufficient_local_offset_keep_original(self):
        for mode in ('fixed','role','offset'):
            f=list(fixture());kwargs={}
            if mode=='fixed':f[1].fixed={0}
            elif mode=='role':kwargs['protected_ids']={f[2][0]['id']}
            else:
                f[1].column_x=[64,84,364]
                f[1].cell_of=[(0,0),(0,2)];f[1].cols=3
                f[1].comp_at={cell:i for i,cell in enumerate(f[1].cell_of)}
            before=deepcopy(f[1].__dict__)
            pl,result,report=run(f,**kwargs)
            self.assertIs(pl,f[1]);self.assertFalse(report['applied'],mode)
            self.assertEqual(result[1],f[5][1]);self.assertEqual(f[1].__dict__,before)

    def test_foreign_body_rejects_straight_connection_without_touching_other_lines(self):
        f=fixture(blocker=True)
        pl,result,report=run(f)
        self.assertIs(pl,f[1]);self.assertFalse(report['applied'])
        self.assertEqual(result[0],f[5][0])
        self.assertIn('complete_geometry',[r['reason'] for r in report['skipped']])

    def test_legal_new_crossing_is_rejected_when_count_would_increase(self):
        f=fixture(crossing=True)
        self.assertEqual(f[5][1]['crossings'],0)
        pl,result,report=run(f)
        self.assertIs(pl,f[1]);self.assertFalse(report['applied'])
        self.assertEqual(result[1],f[5][1])
        self.assertIn('no_safe_cost_improvement',[r['reason'] for r in report['skipped']])

    def test_original_valid_flag_cannot_hide_disconnected_saved_routes(self):
        f=list(fixture())
        routes,metrics,validation=deepcopy(f[5])
        f[5]=([(n,s) for n,s in routes if n!='bias'],metrics,validation)
        with self.assertRaises(RoutingValidationError):run(f)

    def test_timeout_rolls_back_the_entire_unvalidated_pose(self):
        f=fixture();original=ports.validate_final_routes;count=0
        def expire(*args,**kwargs):
            nonlocal count
            count+=1
            if count>1:raise RoutingFailure('deadline',{'reason':'quality_time_budget'})
            return original(*args,**kwargs)
        before=deepcopy(f[1].__dict__)
        with patch.object(ports,'validate_final_routes',side_effect=expire):
            pl,result,report=run(f)
        self.assertIs(pl,f[1]);self.assertFalse(report['applied'])
        self.assertEqual(result[1],f[5][1]);self.assertEqual(f[1].__dict__,before)
        self.assertEqual(report['stop_reason'],'time_budget')

    def test_quota_deadline_and_reapplication_remain_bounded(self):
        f=fixture()
        for kwargs in (dict(max_candidates=0),dict(prevalidated=True,deadline=perf_counter()-1)):
            pl,result,report=run(f,**kwargs)
            self.assertFalse(report['applied']);self.assertEqual(report['candidates'],0)
        pl,result,first=run(f)
        again,repeated,report=run((f[0],pl,f[2],f[3],f[4],result,f[6]))
        self.assertFalse(report['applied']);self.assertEqual(repeated[1],result[1])
        self.assertEqual(again.pos_of(0),pl.pos_of(0))

    def test_renaming_mirror_translation_bjt_and_polarity_dual(self):
        for symbol in ('nmos','pmos','npn'):
            outcomes=[]
            for mirror,shift,names in ((False,0,('A','B')),(True,200,('zeta','alpha'))):
                f=fixture(mirror=mirror,shift=shift,names=names,symbol=symbol)
                pl,result,report=run(f)
                self.assertTrue(report['applied'],symbol)
                self.assertEqual(report['controls_after']['facing_errors'],0)
                pins=('C','E') if symbol=='npn' else ('D','S')
                for i in range(2):
                    for pin in pins:self.assertEqual(pl.pin_world(i,pin),f[1].pin_world(i,pin))
                outcomes.append(result[1])
            self.assertEqual(outcomes[0],outcomes[1],symbol)

    def test_old_search_diagnostics_stay_explicitly_before_pose_change(self):
        f=list(fixture());f[5][2].update(search={'old':True},routing_seconds=.75)
        pl,result,report=run(f,prevalidated=True)
        self.assertTrue(report['applied'])
        self.assertEqual(result[2]['search'],{'old':True})
        self.assertEqual(result[2]['routing_seconds'],.75)
        self.assertTrue(result[2]['search_predates_paired_control_ports'])

    def test_saved_result_and_independent_redraw_use_the_new_actual_gate_points(self):
        from netlist2schematic.common.sa_saved import load_placement
        from netlist2schematic.flow.run_flow import _finish,_saved_positions
        from netlist2schematic.rendering.render_clean import render
        from tools.evaluate_goal_layouts import verify
        f=fixture(names=('M0','M1'));pl,result,report=run(f)
        self.assertTrue(report['applied'])
        with TemporaryDirectory(prefix='paired-control-save-') as directory:
            root=Path(directory);source=root/'source.cir'
            source.write_text('.model NMOS nmos\nM0 d0 bias gnd gnd NMOS\nM1 d1 bias gnd gnd NMOS\n.end\n',encoding='utf8')
            data=dict(source_netlist=str(source),source_cell=None,mode='layered',
                placement=_saved_positions(pl,f[2]),layout_grid=dict(column_x=pl.column_x,row_y=pl.row_y),
                structs=[],layout_regions=dict(plan=f[6],paired_control_routes=report))
            with patch('netlist2schematic.flow.run_flow.final_route',side_effect=AssertionError('A*')), \
                 patch.object(ports,'regularize_control_routes',side_effect=AssertionError('repeated pose change')), \
                 redirect_stdout(io.StringIO()):
                _finish(data,f[0],pl,f[2],f[3],f[4],'source',root/'out',routed_result=result,
                        simplify_routes='off',protected_paths={source})
                saved=root/'out/source-placement.json';loaded=load_placement(saved)
                measured=verify(saved);render(saved,root/'redraw.svg')
            self.assertEqual(loaded[0]['placement'],data['placement'])
            self.assertEqual(measured['metrics'],result[1])
            self.assertTrue(measured['dual_svg_routes_identical'])
