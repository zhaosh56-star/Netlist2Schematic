"""Topology-only conduction columns, transactional trials and guarded selection."""
from copy import deepcopy
from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import patch

from tests.test_passive_placement import base_placement, resistor
from tests.test_series_alignment import device, fixture, PAR
from netlist2schematic.plugins.columns import column_layout as columns
from netlist2schematic.layout import series_alignment as series
from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.routing.orthogonal_router import RoutingFailure


def dual_stack():
    devices, cells = {}, {}
    for prefix, col in (('a', 2), ('b', 6)):
        devices[prefix+'1'] = device('pmos', prefix+'top', 'c1', 'vdd')
        devices[prefix+'2'] = device('pmos', prefix+'mid', 'c2', prefix+'top')
        devices[prefix+'3'] = device('nmos', prefix+'mid', 'c3', prefix+'low')
        devices[prefix+'4'] = device('nmos', prefix+'low', 'c4', '0')
        for row in range(1, 5):cells[prefix+str(row)] = (row, col+(prefix=='a' and row>2))
    devices['side'] = device('nmos', 'vdd', 'access', 'amid');cells['side'] = (1, 3)
    pl, comps, nets, index = base_placement(devices, cells)
    return pl, comps, nets, index, dict(structs=[], layout_style='structured', candidate_id=2)


def plan(case):
    pl, comps, nets, index, metadata = case
    return columns.build_column_plan(PAR, pl, comps, nets, index, metadata.get('structs', ()))


def refine(case, **kwargs):
    pl, comps, nets, index, metadata = case
    return columns.refine_columns(PAR, pl, comps, nets, index, metadata, plan(case), RoutingOptions(), **kwargs)


class ColumnEvidenceTests(unittest.TestCase):
    def test_repeated_multilevel_controls_select_backbone_at_fork(self):
        result = plan(dual_stack())
        paths = [c['members'] for c in result['columns']]
        self.assertIn(['a1','a2','a3','a4'], paths)
        self.assertIn(['b1','b2','b3','b4'], paths)
        self.assertIn(['side'], paths)
        fork = next(e for e in result['edges'] if e['a']=='a2' and e['b']=='a3')
        self.assertFalse(fork['unique'])
        self.assertEqual(fork['evidence'], 'repeated_multilevel_controls')
        self.assertEqual(result['side_accesses'][0]['tap'], 'side')
        names = [n for c in result['columns'] for n in c['members']]
        self.assertEqual(len(names), len(set(names)))

    def test_common_bias_without_repeated_levels_cannot_choose_fork(self):
        case = dual_stack()
        for c in case[1]:
            c['nodes'][columns.CONTROLS[c['symbol']]] = 'common'
        result = plan(case)
        self.assertFalse(any(e['net']=='amid' for e in result['edges']))
        self.assertTrue(any(e['reason']=='unconfirmed_fork' for e in result['skipped']))

    def test_unique_continuations_work_for_all_four_polarities(self):
        for kind in ('nmos','pmos','npn','pnp'):
            with self.subTest(kind=kind):
                result = plan(fixture(kind))
                self.assertEqual(len(result['edges']),1)
                self.assertEqual(result['edges'][0]['evidence'],'unique_active_continuation')

    def test_common_tail_is_shared_anchor_not_one_column_extension(self):
        devices = {'l':device('nmos','dl','gl','tail'), 'r':device('nmos','dr','gr','tail'),
                   't':device('nmos','tail','gt','0')}
        pl, comps, nets, index = base_placement(devices, {'l':(1,1),'r':(1,3),'t':(2,2)})
        result = columns.build_column_plan(PAR,pl,comps,nets,index,
            [dict(kind='differential_pair',roles=dict(left='l',right='r'))])
        self.assertEqual(result['edges'],[])
        self.assertEqual(result['shared_junction_anchors'],['tail'])

    def test_supply_and_clock_do_not_join_unrelated_columns(self):
        devices={'l':device('nmos','x','clk','0'),'r':device('nmos','y','clk','0')}
        pl, comps, nets, index=base_placement(devices,{'l':(1,1),'r':(2,3)})
        result=columns.build_column_plan(PAR,pl,comps,nets,index)
        self.assertFalse(result['supported'])

    def test_equal_fork_is_not_resolved_by_longest_path_or_location(self):
        devices={'u':device('nmos','top','gu','j'),'l':device('nmos','j','gl','0'),
                 'r':device('nmos','j','gr','0')}
        pl, comps, nets, index=base_placement(devices,{'u':(1,2),'l':(2,1),'r':(2,3)})
        result=columns.build_column_plan(PAR,pl,comps,nets,index)
        self.assertEqual(result['edges'],[])

    def test_renaming_reordering_translation_and_mirror_preserve_evidence(self):
        original=dual_stack(); expected={(e['a'],e['b'],e['evidence']) for e in plan(original)['edges']}
        for mirror in (False,True):
            pl,comps,nets,index,data=deepcopy(original)
            mapping={c['id']:'renamed_'+str(k) for k,c in enumerate(reversed(comps))}
            for c in comps:
                c['id']=mapping[c['id']]
                c['nodes']={p:('signal_'+n if n not in ('vdd','0') else n) for p,n in c['nodes'].items()}
            newnets={('signal_'+n if n not in ('vdd','0') else n):[(mapping[a],p) for a,p in ts] for n,ts in reversed(list(nets.items()))}
            pl.comps=comps
            if mirror:pl.set_state([(r,pl.cols-1-c) for r,c in pl.cell_of],pl.orient)
            pl.column_x=[x+1000 for x in pl.column_x];pl.row_y=[y+500 for y in pl.row_y]
            newindex={c['id']:i for i,c in enumerate(comps)}
            result=columns.build_column_plan(PAR,pl,comps,newnets,newindex)
            inverse={b:a for a,b in mapping.items()}
            self.assertEqual({(inverse[e['a']],inverse[e['b']],e['evidence']) for e in result['edges']},expected)

    def test_row_plan_change_is_incompatible(self):
        case=dual_stack(); p=plan(case); pl,comps,_,index,_=case
        trial=deepcopy(pl);cells=list(pl.cell_of);cells[index['a1']]=(0,2);trial.set_state(cells,pl.orient)
        self.assertFalse(columns.compatible_plan(p,trial,comps,index))


class ColumnTransactionTests(unittest.TestCase):
    def test_regularization_preserves_rows_axes_orientations_and_input(self):
        case=dual_stack();pl,comps,nets,index,data=case
        old=deepcopy(pl.cell_of);original=deepcopy(data)
        with patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('no A*')):
            result,metadata=refine(case)
        report=metadata['layout_columns']
        self.assertTrue(report['applied'])
        self.assertEqual(report['current']['column_breaks'],0)
        self.assertEqual(report['current']['side_intrusions'],0)
        self.assertEqual(pl.cell_of,old);self.assertEqual(data,original)
        self.assertEqual(result.row_of,pl.row_of);self.assertEqual(result.orient,pl.orient)
        self.assertEqual(result.row_y,pl.row_y);self.assertEqual(result.column_x,pl.column_x)
        self.assertEqual(len(result.comp_at),len(comps))
        self.assertEqual(result.cell_of[index['side']],pl.cell_of[index['side']])
        self.assertEqual(metadata['optimization_metrics']['measurement_stage'],'after_column_layout')

    def test_fixed_devices_cannot_move(self):
        case=dual_stack();pl,comps,nets,index,_=case
        pl.fixed={c['id'] for c in comps}
        result,data=refine(case)
        self.assertEqual(result.cell_of,pl.cell_of)
        self.assertFalse(data['layout_columns']['applied'])

    def test_child_guards_are_rechecked_without_moving_unrelated_devices(self):
        case=dual_stack();pl,comps,nets,index,_=case
        pl.blocks=[[index['a3'],index['b3']]]
        result,data=refine(case)
        self.assertTrue(result.blocks_contiguous())
        self.assertEqual(result.cell_of[index['side']],pl.cell_of[index['side']])
        with patch.object(type(pl),'blocks_contiguous',return_value=False):
            rejected,report=refine(case)
        self.assertEqual(rejected.cell_of,pl.cell_of)
        self.assertFalse(report['layout_columns']['applied'])

    def test_zero_budget_and_candidate_limit_do_not_reset_or_mutate(self):
        for kwargs in (dict(seconds=0),dict(max_trials=0),dict(max_trials=1)):
            case=dual_stack();old=list(case[0].cell_of)
            result,data=refine(case,**kwargs)
            self.assertEqual(case[0].cell_of,old)
            self.assertLessEqual(data['layout_columns']['trials'],kwargs.get('max_trials',128))
            self.assertEqual(len(result.comp_at),len(case[1]))

    def test_static_timeout_rolls_back_incomplete_trial(self):
        case=dual_stack()
        failure=RoutingFailure('time',dict(reason='quality_time_budget'))
        with patch('netlist2schematic.plugins.preflight.placement_preflight._static_faults',
                   side_effect=[dict(count=0,hard=[]),failure]):
            result,data=refine(case)
        self.assertEqual(result.cell_of,case[0].cell_of)
        self.assertFalse(data['layout_columns']['applied'])
        self.assertEqual(data['layout_columns']['stop_reason'],'time_budget')

    def test_later_series_alignment_cannot_dismantle_columns(self):
        case=dual_stack();result,data=refine(case)
        finished,metadata=series.refine_series_alignment(PAR,result,case[1],case[2],case[3],data,RoutingOptions())
        self.assertTrue(columns.preserves_columns(result,finished,case[3]))

    def test_unsupported_plan_reports_missing_coverage(self):
        devices={'l':device('nmos','x','clk','0'),'r':device('nmos','y','clk','0')}
        case=(*base_placement(devices,{'l':(1,1),'r':(2,3)}),dict(structs=[]))
        result,data=refine(case)
        self.assertEqual(data['layout_columns']['stop_reason'],'no_supported_columns')
        self.assertEqual(data['layout_columns']['current']['covered_active_count'],0)

    def test_deterministic_fixed_input(self):
        a,ad=refine(dual_stack());b,bd=refine(dual_stack())
        self.assertEqual(a.cell_of,b.cell_of)
        self.assertEqual(ad['layout_columns']['actions'],bd['layout_columns']['actions'])


class ColumnSelectionTests(unittest.TestCase):
    def result(self,case,ident,wire=1000,crossings=0,score=1000):
        pl,comps,nets,index,data=case
        return ((score,crossings,wire,ident),pl,comps,nets,index,data,
                ([],dict(wirelength=wire,crossings=crossings),dict(valid=True)),dict(score=score),str(ident))

    def test_structure_gain_within_115_percent_can_win_old_quality(self):
        case=dual_stack();pl,data=refine(case)
        improved=(pl,*case[1:4],data)
        reference=self.result(case,1);organized=self.result(improved,2,wire=1100,score=1200)
        winner,report=columns.select_column_result([reference,organized],plan(case))
        self.assertIs(winner,organized)
        self.assertEqual(report['reason'],'column_organization_gain')

    def test_zero_crossing_and_length_guard_keep_reference(self):
        case=dual_stack();pl,data=refine(case);improved=(pl,*case[1:4],data)
        reference=self.result(case,1)
        for wire,cross in ((1151,0),(1100,1)):
            organized=self.result(improved,2,wire=wire,crossings=cross,score=1200)
            winner,_=columns.select_column_result([reference,organized],plan(case))
            self.assertIs(winner,reference)

    def test_main_flow_defaults_to_auto_and_explicit_conflicts_remain(self):
        from inspect import signature
        from netlist2schematic.flow.run_flow import run,_validate_column_policy,_resolve_column_policy,DEFAULT_COLUMN_LAYOUT
        from netlist2schematic.flow.aesthetic_flow import select_layout
        self.assertIsNone(signature(run).parameters['column_layout'].default)
        self.assertEqual(DEFAULT_COLUMN_LAYOUT,'auto')
        self.assertEqual(_resolve_column_policy(None),'auto')
        self.assertEqual(_resolve_column_policy('off'),'off')
        self.assertEqual(signature(select_layout).parameters['column_layout'].default,'off')
        for values in (('bad',),('auto','baseline','structured'),('auto','layered','structured','auto')):
            with self.assertRaises(ValueError):_validate_column_policy(*values)

    def test_saved_legacy_and_other_layout_experiments_do_not_implicitly_enable_columns(self):
        from netlist2schematic.flow.run_flow import _resolve_column_policy
        self.assertEqual(_resolve_column_policy(None,saved=True),'off')
        self.assertEqual(_resolve_column_policy(None,'baseline'),'off')
        self.assertEqual(_resolve_column_policy(None,'layered','legacy'),'off')
        for policy in ('auto','passive','all'):
            self.assertEqual(_resolve_column_policy(None,'layered','structured',policy),'off')


class ColumnDefaultCliTests(unittest.TestCase):
    def test_cli_default_and_explicit_off_reach_main_flow(self):
        from netlist2schematic.flow import run_flow
        for flags,expected in (([], 'auto'),(['--column-layout','off'],'off')):
            with patch.object(run_flow,'run') as run:
                self.assertEqual(run_flow.main(['unused.cir',*flags]),0)
            self.assertEqual(run.call_args.kwargs['column_layout'],expected)

    def test_saved_cli_keeps_coordinates_and_rejects_explicit_auto(self):
        from netlist2schematic.flow import run_flow
        for flag,method in (('--reroute-placement','reroute_placement'),('--simplify-placement','simplify_placement')):
            with patch.object(run_flow,method) as run:
                self.assertEqual(run_flow.main([flag,'saved.json']),0)
            run.assert_called_once()
            with redirect_stdout(io.StringIO()),patch('sys.stderr',new=io.StringIO()),self.assertRaises(SystemExit):
                run_flow.main([flag,'saved.json','--column-layout','auto'])


class ColumnFlowTests(unittest.TestCase):
    def test_reference_untouched_and_finalizers_share_one_budget(self):
        from netlist2schematic.flow import aesthetic_flow
        cases=[dual_stack(),dual_stack()]
        for number,case in enumerate(cases,1):case[4]['candidate_id']=number
        original=list(cases[0][0].cell_of);events=[];budget={}
        def column(par,pl,cs,ns,ix,data,p,options=None,**kw):
            events.append('columns');budget['columns']=kw
            return pl,dict(data,layout_columns=dict(plan=p,applied=False,trials=37))
        def align(par,pl,cs,ns,ix,data,options=None,**kw):
            events.append('series'+str(data['candidate_id']));budget['series'+str(data['candidate_id'])]=kw
            return pl,data
        def route(*a,**kw):
            events.append('route');return [],dict(wirelength=1000,crossings=0,bends=0),dict(valid=True)
        with patch.object(structured,'create_candidates',return_value=iter(cases)), \
             patch.object(columns,'refine_columns',side_effect=column) as regularized, \
             patch.object(series,'refine_series_alignment',side_effect=align), \
             patch.object(aesthetic_flow,'final_route',side_effect=route) as routed, \
             patch('netlist2schematic.layout.layout_quality.evaluate_quality',return_value=dict(score=1,per_net={})), \
             redirect_stdout(io.StringIO()):
            aesthetic_flow.select_layout('unused',column_layout='auto')
        self.assertEqual(events,['series1','route','columns','series2','route'])
        self.assertEqual(cases[0][0].cell_of,original)
        self.assertEqual(regularized.call_count,1);self.assertEqual(routed.call_count,2)
        self.assertEqual(budget['series2']['max_trials'],91)
        self.assertEqual(budget['columns']['deadline'],budget['series2']['deadline'])

    def test_off_does_not_build_or_apply_column_plan(self):
        from netlist2schematic.flow import aesthetic_flow
        case=fixture()
        with patch.object(structured,'create_candidates',return_value=iter([case])), \
             patch.object(columns,'build_column_plan',side_effect=AssertionError('off analyzed columns')), \
             patch.object(aesthetic_flow,'final_route',return_value=([],dict(wirelength=10,crossings=0,bends=0),dict(valid=True))), \
             patch('netlist2schematic.layout.layout_quality.evaluate_quality',return_value=dict(score=1,per_net={})), \
             redirect_stdout(io.StringIO()):
            result=aesthetic_flow.select_layout('unused')
        self.assertNotIn('layout_columns',result[4])


if __name__=='__main__':unittest.main()
