"""Neutral cells use actual terminals, never circuit names or imagined edges."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_passive_placement import resistor,source,convert
from tests.test_layout_evidence import mos
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment import attachment_stages as stages
from netlist2schematic.plugins.attachment.attachment_selection import measure_candidate,organization_rank,select_attachment_result
from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.routing.routing_rules import RoutingOptions


def circuit(*,dual=False,bjt=False,rename=False):
    d=dict(ap=mos('sa','input','vdd','PMOS'),ak=mos('oa','broadcast','sa','PMOS'),
        an=mos('oa','input','0'),bp=mos('ob','broadcast','vdd','PMOS'),
        bn=mos('ob','oa','sb'),bk=mos('sb','broadcast','0'),
        cp=mos('oc','undriven','vdd','PMOS'),ck=mos('oc','broadcast','sc'),
        cn=mos('sc','undriven','0'),dp=mos('od','oc','vdd','PMOS'),dn=mos('od','oc','0'))
    cells=dict(ap=(1,2),ak=(2,2),an=(3,2),bp=(1,1),bn=(2,1),bk=(3,1),
        cp=(1,3),ck=(2,3),cn=(3,3),dp=(1,4),dn=(2,4))
    formals=('input','broadcast','oc','od');names={n:n for n in d}
    if dual:
        for c in d.values():
            c['type']='PMOS' if c['type']=='NMOS' else 'NMOS'
            for k in ('d','g','s','bulk'):
                if c.get(k) in ('0','vdd'):c[k]={'0':'vdd','vdd':'0'}[c[k]]
        cells={n:(4-r,c) for n,(r,c) in cells.items()}
    if bjt:d={n:dict(cls='BJT',type='NPN' if c['type']=='NMOS' else 'PNP',
        c=c['d'],b=c['g'],e=c['s']) for n,c in d.items()}
    if rename:
        names={n:'part'+str(91-i) for i,n in enumerate(d)}
        d={names[n]:{k:'wire_'+v if k in ('d','g','s','c','b','e') and v not in ('0','vdd') else v
                     for k,v in c.items()} for n,c in reversed(list(d.items()))}
        cells={names[n]:p for n,p in cells.items()};formals=tuple('wire_'+p for p in formals)
    return d,cells,formals,names


def build(d,cells,formals):
    structs=analyze_structures(d);pl,cs,nets,index,_=convert(d,cells,records=structs)
    plan=ai.build_attachment_plan(d,cs,nets,index,structs,compute_levels(d),pl.row_of,
        policy='all',formal_ports=formals)
    return d,ai._install(pl,cs,index,plan),cs,nets,index,plan


def fixture(**kwargs):
    d,c,p,_=circuit(**kwargs);return build(d,c,p)


def arrange(f,*,safe='real',budget=None,until=None,initial=True):
    d,pl,cs,nets,index,plan=f
    budget=budget if budget is not None else ai.new_attachment_budget()
    report=dict(actions=[],candidate_trials=0,candidates=0,accepted=0)
    args=(structured.par,pl,cs,nets,index,plan,RoutingOptions(),budget,
          perf_counter()+1 if until is None else until,report)
    if safe=='real':result=stages.arrange_stages(*args,initialization=initial,hpwl_limit=float('inf'))
    else:
        with patch.object(ai,'_checked_static',return_value=safe):
            result=stages.arrange_stages(*args,initialization=initial,hpwl_limit=float('inf'))
    return result,report,budget


class NeutralStageTests(unittest.TestCase):
    def test_unique_spines_cover_cells_but_shared_control_does_not_merge(self):
        f=fixture();model=f[5]['conduction_stages']
        self.assertTrue(model['global_layout_eligible'])
        self.assertEqual(len(model['stages']),4)
        self.assertEqual(model['coverage'],dict(active=11,covered=11,unassigned=[]))
        self.assertEqual({tuple(c['conduction_spine']) for c in model['stages']},
            {('ap','ak','an'),('bp','bn','bk'),('cp','ck','cn'),('dp','dn')})
        self.assertEqual(len(model['stage_edges']),2)
        self.assertEqual(len({model['ownership'][n] for n in ('ak','bp','bk','ck')}),3)

    def test_unknown_control_stays_distinct_from_unused_stage_output(self):
        f=fixture();model=f[5]['conduction_stages']
        self.assertEqual(model['unknown_control_interfaces'][0]['net'],'undriven')
        self.assertFalse(any(e['net']=='ob' for e in model['stage_edges']))
        self.assertEqual(f[0]['cp']['g'],'undriven');self.assertEqual(f[0]['bp']['d'],'ob')

    def test_polarity_dual_and_BJT_have_equivalent_coverage_and_driver_edges(self):
        for dual,bjt in ((True,False),(False,True),(True,True)):
            with self.subTest(dual=dual,bjt=bjt):
                m=fixture(dual=dual,bjt=bjt)[5]['conduction_stages']
                self.assertTrue(m['global_layout_eligible'])
                self.assertEqual(m['coverage']['covered'],11);self.assertEqual(len(m['stage_edges']),2)

    def test_rename_and_reverse_netlist_order_preserve_cells(self):
        d,c,p,names=circuit(rename=True);f=build(d,c,p);m=f[5]['conduction_stages']
        inverse={v:k for k,v in names.items()}
        self.assertEqual({frozenset(inverse[n] for n in s['members']) for s in m['stages']},
            {frozenset(s['members']) for s in fixture()[5]['conduction_stages']['stages']})

    def test_shared_gate_without_actual_stage_edge_does_not_enable_layout(self):
        d={};cells={}
        for i in range(3):
            d['p'+str(i)]=mos('out'+str(i),'shared','vdd','PMOS')
            d['n'+str(i)]=mos('out'+str(i),'shared','0')
            cells['p'+str(i)]=(1,i);cells['n'+str(i)]=(2,i)
        m=build(d,cells,('shared',))[5]['conduction_stages']
        self.assertFalse(m['global_layout_eligible']);self.assertEqual(m['stage_edges'],[])

    def test_cross_coupled_feedback_does_not_become_linear_stage_order(self):
        d=dict(ap=mos('a','b','vdd','PMOS'),an=mos('a','b','0'),
            bp=mos('b','a','vdd','PMOS'),bn=mos('b','a','0'))
        m=build(d,dict(ap=(1,1),an=(2,1),bp=(1,2),bn=(2,2)),())[5]['conduction_stages']
        self.assertTrue(m['cycle_members']);self.assertFalse(m['global_layout_eligible'])

    def test_conduction_fork_and_internal_passive_tap_block_whole_cell_claim(self):
        for mode in ('fork','tap'):
            d,c,p,_=circuit()
            d['extra']=mos('oa','eg','0') if mode=='fork' else resistor('sa','0')
            c['extra']=(0,0)
            self.assertFalse(build(d,c,p)[5]['conduction_stages']['global_layout_eligible'],mode)

    def test_clamped_drain_junction_cannot_claim_exclusive_driver(self):
        d,c,p,_=circuit();d['v']=source('VSRC','oa','0');c['v']=(0,0)
        m=build(d,c,p)[5]['conduction_stages']
        self.assertFalse(m['global_layout_eligible'])
        self.assertTrue(any(s['reason']=='external_source_at_drain_junction' for s in m['layout_skip_reasons']))

    def test_preexisting_owner_is_never_split_between_cells(self):
        f=fixture();rows={c['id']:f[1].row_of[i] for i,c in enumerate(f[2])}
        m=stages.compile_stages(f[2],f[5]['readability_plan']['junctions'],rows,
            [['ap','bp']],formal_ports=('input','broadcast','oc','od'))
        self.assertFalse(m['global_layout_eligible'])

    def test_incompatible_same_row_members_are_not_counted_as_placed(self):
        d,c,p,_=circuit();c['an']=(2,5)
        f=build(d,c,p);m=f[5]['conduction_stages']
        self.assertFalse(m['global_layout_eligible'])
        self.assertEqual(stages.measure_stages(f[1],f[2],f[4],m)['count'],0)

    def test_complete_contract_rejects_changed_external_interface(self):
        f=fixture();f[2][f[4]['cp']]['nodes']['G']='different'
        m=stages.measure_stages(f[1],f[2],f[4],f[5]['conduction_stages'])
        self.assertFalse(m['measurable']);self.assertIsNone(stages.stage_rank(m))

    def test_real_trial_solves_order_and_preserves_rows_axes_connections(self):
        f=fixture();before=stages.measure_stages(f[1],f[2],f[4],f[5]['conduction_stages'])
        with patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('Astar trial')):
            result,report,budget=arrange(f)
        detail=report['conduction_stage_arrangement']
        self.assertTrue(detail['applied'])
        self.assertLess(stages.stage_rank(detail['after']),stages.stage_rank(before))
        self.assertEqual(detail['after']['live_protected_cells'],4)
        self.assertEqual(result.row_of,f[1].row_of)
        self.assertEqual((result.column_x,result.row_y),(f[1].column_x,f[1].row_y))
        self.assertTrue(ai._checked_static(structured.par,result,f[3],f[4],RoutingOptions(),None))
        self.assertLessEqual(budget['candidates'],16)

    def test_failed_static_trials_restore_original_blocks_pose_and_random_state(self):
        f=fixture();snapshot=deepcopy((f[1].cell_of,f[1].orient,f[1].blocks,f[1].block_of,f[1].rng.getstate()))
        result,report,budget=arrange(f,safe=False)
        self.assertIs(result,f[1])
        self.assertEqual(snapshot,(result.cell_of,result.orient,result.blocks,result.block_of,result.rng.getstate()))
        self.assertFalse(report['conduction_stage_arrangement']['applied'])

    def test_fixed_members_do_not_move_or_turn_in_any_proposal(self):
        f=fixture();i=f[4]['ap'];f[1].fixed.add(i)
        proposals=list(stages.stage_trials(f[1],f[2],f[4],f[5]['conduction_stages']))
        self.assertTrue(proposals)
        for trial,_ in proposals:
            self.assertEqual((trial.pos_of(i),trial.orient[i]),(f[1].pos_of(i),f[1].orient[i]))

    def test_many_proposals_and_undo_keep_current_rows_and_cell_ownership(self):
        f=fixture();pl,report,_=arrange(f);rows=list(pl.row_of);blocks=deepcopy(pl.blocks)
        for number in range(200):
            original=(list(pl.cell_of),list(pl.orient));proposal=pl.propose()
            if proposal is not None:pl.set_state(*proposal)
            self.assertTrue(pl.valid_state());self.assertEqual(pl.row_of,rows)
            self.assertEqual(pl.comp_at,{cell:i for i,cell in enumerate(pl.cell_of)})
            self.assertEqual(pl.blocks,blocks)
            if number%2:pl.set_state(*original)

    def test_budget_limits_are_shared_and_zero_budget_is_not_no_solution(self):
        f=fixture();budget=ai.new_attachment_budget();budget['candidates']=127
        _,report,budget=arrange(f,safe=False,budget=budget)
        self.assertEqual(budget['candidates'],128)
        f=fixture();result,report,budget=arrange(f,until=perf_counter()-1)
        self.assertIs(result,f[1]);self.assertEqual(budget['candidates'],0)
        self.assertEqual(report['conduction_stage_arrangement']['stop_reason'],'shared_budget')

    def test_only_late_safe_trial_cannot_be_committed(self):
        f=fixture();clock=[100.]
        def late(*args):clock[0]=102.;return True
        with patch.object(ai,'_checked_static',side_effect=late), \
             patch.object(ai,'perf_counter',side_effect=lambda:clock[0]), \
             patch.object(stages,'perf_counter',side_effect=lambda:clock[0]):
            result,report,budget=arrange(f,until=101.)
        self.assertIs(result,f[1]);self.assertFalse(report['conduction_stage_arrangement']['applied'])

    def test_reference_candidate_is_generated_before_new_cell_compiler(self):
        d,_,_,_=circuit()
        with TemporaryDirectory() as folder:
            p=Path(folder)/'input.cir'
            lines=['* neutral fixture','.model n nmos','.model p pmos']
            for n,c in d.items():lines.append('M'+n+' '+ ' '.join(c[k] for k in ('d','g','s'))+' '+c['bulk']+' '+('n' if c['type']=='NMOS' else 'p'))
            p.write_text('\n'.join(lines)+'\n.end\n',encoding='utf8')
            with patch.object(stages,'compile_stages',side_effect=AssertionError('before reference')) as compiled:
                first=next(structured.create_candidates(p,iterations=0,attachment_layout='all'))
            compiled.assert_not_called();self.assertEqual(first[4]['candidate_id'],1)

    def test_common_measurement_requires_all_expected_cells(self):
        f=fixture();metadata=dict(candidate_id=1,layout_attachments=dict(plan=f[5],applied=False))
        measured=measure_candidate(f[1],f[2],f[4],metadata,f[5],policy='all')
        self.assertIsNotNone(organization_rank(measured['layout_attachments']))
        measured['layout_attachments']['conduction_stage_organization']['count']=0
        self.assertIsNone(organization_rank(measured['layout_attachments']))

    def test_full_initializer_installs_real_cells_without_claiming_passive_motion(self):
        d,pl,cs,nets,index,plan=fixture()
        result,report=ai.initialize_attachments(pl,d,cs,nets,index,plan,options=RoutingOptions())
        self.assertTrue(report['applied']);self.assertTrue(report['active_stage_applied'])
        self.assertFalse(report['passive_applied']);self.assertFalse(report['source_applied'])
        m=stages.measure_stages(result,cs,index,plan['conduction_stages'])
        self.assertEqual(m['live_protected_cells'],4)
        self.assertEqual(len(ai._motion_groups(plan)),4)
        self.assertEqual((result.row_of,result.column_x,result.row_y),(pl.row_of,pl.column_x,pl.row_y))

    def test_post_SA_finish_consumes_same_cells_and_remaining_budget(self):
        d,pl,cs,nets,index,plan=fixture()
        result,initial=ai.initialize_attachments(pl,d,cs,nets,index,plan,options=RoutingOptions())
        a=next(s for s in plan['conduction_stages']['stages'] if 'ap' in s['members'])
        b=next(s for s in plan['conduction_stages']['stages'] if 'bp' in s['members'])
        ac=result.cell_of[index['ap']][1];bc=result.cell_of[index['bp']][1]
        cells=list(result.cell_of)
        for cell,col in ((a,bc),(b,ac)):
            for n in cell['members']:cells[index[n]]=(cells[index[n]][0],col)
        result.set_state(cells,result.orient)
        self.assertTrue(result.valid_state())
        count=initial['budget']['candidates'];spent=initial['budget']['spent_seconds']
        meta=dict(candidate_id=2,layout_attachments=dict(plan=plan,budget=initial['budget'],applied=True))
        finished,updated=ai.refine_attachments(structured.par,result,cs,nets,index,meta,
            options=RoutingOptions(),deadline=perf_counter()+1,budget=initial['budget'])
        report=updated['layout_attachments']['refinement']['conduction_stage_arrangement']
        self.assertTrue(report['applied'])
        self.assertEqual(report['after']['flow_order_errors'],0)
        self.assertGreater(updated['layout_attachments']['budget']['candidates'],count)
        self.assertGreaterEqual(updated['layout_attachments']['budget']['spent_seconds'],spent)
        self.assertLessEqual(updated['layout_attachments']['budget']['candidates'],128)

    def test_selection_uses_common_stage_evidence_inside_original_route_limits(self):
        f=fixture();placed,_,_=arrange(f)
        def row(identifier,pl,score,length,cross):
            metadata=measure_candidate(pl,f[2],f[4],dict(candidate_id=identifier),f[5],policy='all')
            metrics=dict(wirelength=length,crossings=cross,bends=0)
            q=dict(score=score,per_net={})
            return ((score,cross,length,str(identifier)),pl,f[2],f[3],f[4],metadata,
                    ([],metrics,dict(valid=True)),q,str(identifier))
        reference=row(1,f[1],10,1000,2)
        selected,report=select_attachment_result([reference,row(2,placed,11,1100,3)])
        self.assertEqual(selected[8],'2');self.assertEqual(report['reference_candidate'],'1')
        for length,cross in ((1151,3),(1100,4)):
            selected,_=select_attachment_result([reference,row(2,placed,11,length,cross)])
            self.assertEqual(selected[8],'1')

    def test_saved_position_measurement_does_not_invent_runtime_leaf_protection(self):
        f=fixture();pl,_,_=arrange(f)
        expected=stages.measure_stages(pl,f[2],f[4],f[5]['conduction_stages'])
        saved=deepcopy(pl);del saved.blocks
        measured=stages.measure_stages(saved,f[2],f[4],f[5]['conduction_stages'])
        self.assertTrue(measured['measurable'])
        self.assertIsNone(measured['live_protected_cells'])
        self.assertEqual(stages.stage_rank(measured),stages.stage_rank(expected))

    def test_mirror_and_translation_preserve_neutral_organization_error(self):
        f=fixture();pl=f[1];model=f[5]['conduction_stages']
        expected=stages.stage_rank(stages.measure_stages(pl,f[2],f[4],model))
        moved=deepcopy(pl);moved.column_x=[x+372 for x in pl.column_x];moved.row_y=[y+194 for y in pl.row_y]
        self.assertEqual(stages.stage_rank(stages.measure_stages(moved,f[2],f[4],model)),expected)
        mirrored=deepcopy(pl)
        mirrored.set_state([(r,len(pl.column_x)-1-c) for r,c in pl.cell_of],[1-v for v in pl.orient])
        self.assertEqual(stages.stage_rank(stages.measure_stages(mirrored,f[2],f[4],model)),expected)

    def test_variable_length_inverter_cascades_need_no_clock_or_fixed_cell_count(self):
        for count in (2,5):
            with self.subTest(count=count):
                d={};cells={}
                for i in range(count):
                    control='external' if i==0 else 'out'+str(i-1)
                    d['p'+str(i)]=mos('out'+str(i),control,'vdd','PMOS')
                    d['n'+str(i)]=mos('out'+str(i),control,'0')
                    cells['p'+str(i)]=(1,count-i);cells['n'+str(i)]=(2,count-i)
                f=build(d,cells,('external','out'+str(count-1)))
                model=f[5]['conduction_stages']
                self.assertTrue(model['global_layout_eligible']);self.assertEqual(len(model['stages']),count)
                self.assertEqual(len(model['stage_edges']),count-1)
                result,report,_=arrange(f)
                self.assertTrue(report['conduction_stage_arrangement']['applied'])
                self.assertEqual(stages.measure_stages(result,f[2],f[4],model)['live_protected_cells'],count)

    def test_off_never_calls_neutral_compiler_for_any_candidate(self):
        d,_,_,_=circuit()
        with TemporaryDirectory() as folder:
            path=Path(folder)/'input.cir'
            lines=['* neutral fixture','.model n nmos','.model p pmos']
            for n,c in d.items():lines.append('M'+n+' '+' '.join(c[k] for k in ('d','g','s'))+' '+c['bulk']+' '+('n' if c['type']=='NMOS' else 'p'))
            path.write_text('\n'.join(lines)+'\n.end\n',encoding='utf8')
            with patch.object(stages,'compile_stages',side_effect=AssertionError('off compiled stages')) as compiled:
                candidates=list(structured.create_candidates(path,iterations=0,attachment_layout='off'))
            self.assertEqual(len(candidates),3);compiled.assert_not_called()


if __name__=='__main__':unittest.main()
