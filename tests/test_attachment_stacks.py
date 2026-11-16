"""Repeated-stack display evidence must survive the real candidate pipeline."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tests.test_layout_evidence import mos
from tests.test_passive_placement import resistor,convert
from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment import attachment_stacks as stacks
from netlist2schematic.plugins.attachment.attachment_selection import measure_candidate,organization_rank,select_attachment_result
from netlist2schematic.plugins.readability.readability_layout import build_readability_plan
from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.routing.routing_rules import RoutingOptions


def devices(*,legs=2,dual=False,bjt=False,rename=False,rl=False):
    d={}
    for i in range(legs):
        d['low'+str(i)]=mos('inner'+str(i),'bias','0')
        d['high'+str(i)]=mos('end'+str(i),'bias','inner'+str(i))
        head='start' if i==0 else 'end'+str(i)
        d['supply'+str(i)]=mos(head,'pcontrol','vdd','PMOS')
    d['extra']=mos('start','other','rl_node' if rl else 'end0')
    if rl:d['link']=resistor('rl_node','end0')
    if dual:
        for c in d.values():
            if c['cls']=='MOS':c['type']='PMOS' if c['type']=='NMOS' else 'NMOS'
            for k in ('d','g','s','bulk','a','b'):
                if c.get(k) in ('0','vdd'):c[k]={'0':'vdd','vdd':'0'}[c[k]]
    if bjt:d={n:dict(cls='BJT',type='NPN' if c['type']=='NMOS' else 'PNP',c=c['d'],b=c['g'],e=c['s']) for n,c in d.items()}
    mapping={n:n for n in d}
    if rename:
        mapping={n:'part'+str(60-i) for i,n in enumerate(d)}
        fields={'d','g','s','c','b','e','a'}
        d={mapping[n]:{k:'wire_'+v if k in fields and isinstance(v,str) and v not in ('0','vdd') else v
                      for k,v in c.items()} for n,c in reversed(list(d.items()))}
    return d,mapping


def fixture(**kwargs):
    d,names=devices(**kwargs);doc=to_document(d);cs,nets=doc['components'],doc['nets']
    index={c['id']:i for i,c in enumerate(cs)};levels=compute_levels(d)
    original=[levels['device_levels'].get(c['id'],1) for c in cs]
    records=analyze_structures(d)
    readable=build_readability_plan(d,cs,nets,index,records,levels,original)
    return d,cs,nets,index,levels,original,records,readable,names


def planned(f,**kwargs):
    return stacks.plan_stack_rows(f[0],f[1],f[3],f[4],f[7],dict(cores=[],applied_relations=[]),[],**kwargs)


def placed(f,rows):
    inverse={v:k for k,v in f[8].items()};cells={}
    for c in f[1]:
        n=c['id'];old=inverse[n]
        side=int(old[-1]) if old[-1].isdigit() else 0
        cells[n]=(rows[n],1+side*2)
    # A source-only R/L fixture is never passed to this placement helper.
    pl,cs,nets,index,_=convert(f[0],cells,records=f[6])
    return pl,cs,nets,index


class RepeatedStackTests(unittest.TestCase):
    def test_two_continuous_controls_required_and_true_levels_stay_unchanged(self):
        f=fixture();before=deepcopy((f[0],f[1],f[4],f[7]))
        result,model=planned(f)
        self.assertTrue(model['rows_applied']);self.assertEqual(len(model['groups']),1)
        names=f[8]
        self.assertEqual(result['display_rows'][names['high0']],result['display_rows'][names['high1']])
        self.assertEqual(result['display_rows'][names['low0']],result['display_rows'][names['low1']])
        self.assertEqual(before,(f[0],f[1],f[4],f[7]))
        self.assertIsNone(model['groups'][0]['function_claim'])
        self.assertFalse(model['groups'][0]['ownership_claim'])

    def test_one_shared_gate_and_supply_do_not_create_a_stack_pair(self):
        d=dict(a=mos('a','bias','0'),b=mos('b','bias','0'))
        self.assertEqual(stacks.detect_stacks(to_document(d)['components'],{'vdd','0'})['groups'],[])

    def test_different_second_control_stops_correspondence(self):
        d,_=devices();d['high1']['g']='other_bias'
        self.assertEqual(stacks.detect_stacks(to_document(d)['components'],{'vdd','0'})['groups'],[])

    def test_control_on_supply_is_not_evidence(self):
        d,_=devices()
        for n in ('low0','low1'):d[n]['g']='vdd'
        self.assertEqual(stacks.detect_stacks(to_document(d)['components'],{'vdd','0'})['groups'],[])

    def test_unknown_driven_node_is_not_promoted_to_a_supply_boundary(self):
        d,_=devices()
        for c in d.values():
            if c.get('s')=='0':c['s']='driven'
        self.assertEqual(stacks.detect_stacks(to_document(d)['components'],{'vdd','0','driven'})['groups'],[])

    def test_conduction_fork_stops_extension(self):
        d,_=devices();d['fork']=mos('inner0','other','0')
        self.assertEqual(stacks.detect_stacks(to_document(d)['components'],{'vdd','0'})['groups'],[])

    def test_passive_tap_inside_stack_is_not_a_unique_continuation(self):
        d,_=devices();d['tap']=resistor('inner0','0')
        self.assertEqual(stacks.detect_stacks(to_document(d)['components'],{'vdd','0'})['groups'],[])

    def test_three_legs_are_one_bank_without_arbitrary_pair_claim(self):
        f=fixture(legs=3);_,model=planned(f)
        self.assertEqual(len(model['groups']),1)
        group=model['groups'][0];self.assertFalse(group['pair_claim'])
        self.assertEqual(list(map(len,group['same_row'])),[3,3])

    def test_longest_shared_prefix_is_recorded_once_per_leg_set(self):
        d={}
        for i in (0,1):
            d['a'+str(i)]=mos('u'+str(i),'g0','0')
            d['b'+str(i)]=mos('v'+str(i),'g1','u'+str(i))
            d['c'+str(i)]=mos('w'+str(i),'g2','v'+str(i))
        model=stacks.detect_stacks(to_document(d)['components'],{'vdd','0'})
        self.assertEqual(len(model['groups']),1);self.assertEqual(len(model['groups'][0]['same_row']),3)

    def test_N_P_and_BJT_duals_all_receive_compatible_row_hints(self):
        for dual,bjt in ((True,False),(False,True),(True,True)):
            with self.subTest(dual=dual,bjt=bjt):
                f=fixture(dual=dual,bjt=bjt);result,model=planned(f)
                self.assertEqual(len(model['applicable_ids']),1)
                for stage in model['groups'][0]['same_row']:
                    self.assertEqual(len({result['display_rows'][n] for n in stage}),1)
                if not model['rows_applied']:
                    self.assertEqual(result['display_rows'],f[7]['display_rows'])

    def test_already_aligned_P_stack_bank_does_not_move_for_activation(self):
        f=fixture(dual=True);result,model=planned(f)
        self.assertTrue(model['applicable_ids']);self.assertFalse(model['rows_applied'])
        self.assertIs(result,f[7])

    def test_rename_and_reverse_source_order_preserve_row_solution(self):
        a=fixture();b=fixture(rename=True);ra,ma=planned(a);rb,mb=planned(b)
        self.assertEqual(ra['display_rows'],{old:rb['display_rows'][new] for old,new in b[8].items()})
        self.assertEqual(len(ma['applicable_ids']),len(mb['applicable_ids']))

    def test_fixed_display_row_conflict_rolls_back_whole_plan(self):
        f=fixture();n=f[8]['high1']
        result,model=planned(f,fixed_rows={n:f[7]['display_rows'][n]})
        self.assertIs(result,f[7]);self.assertFalse(model['rows_applied'])
        self.assertTrue(any(r['reason']=='fixed_display_row_conflict' for r in model['skip_reasons']))

    def test_existing_leaf_cohort_conflict_cannot_be_broken(self):
        f=fixture();names=f[8]
        result,model=stacks.plan_stack_rows(f[0],f[1],f[3],f[4],f[7],{},
            [[names['extra'],names['high1']]])
        self.assertIs(result,f[7]);self.assertFalse(model['rows_applied'])
        self.assertFalse(model['applicable_ids'])

    def test_confirmed_RL_bridge_is_preserved_without_double_insertion(self):
        f=fixture(rl=True);old=f[7].get('branch_row_plan') or {}
        self.assertTrue(old.get('bridge_alignment'))
        result,model=planned(f);new=result.get('branch_row_plan') or {}
        self.assertTrue(model['applicable_ids'])
        self.assertEqual({b['id'] for b in old['bridge_alignment']},{b['id'] for b in new['bridge_alignment']})
        self.assertEqual(len(old['inserted_rows']),len(new['inserted_rows']))
        for pattern in result['patterns']:
            if 'bridge_alignment' in pattern:
                self.assertIn(pattern['bridge_alignment'],new['bridge_alignment'])

    def test_fixed_compatible_rows_can_keep_an_already_aligned_bank(self):
        f=fixture();result,model=planned(f)
        fixed={f[8]['low1']:result['display_rows'][f[8]['low1']]}
        again,other=planned(f,fixed_rows=fixed)
        self.assertEqual(result['display_rows'],again['display_rows']);self.assertTrue(other['applicable_ids'])

    def test_common_measurement_detects_reference_error_and_new_alignment(self):
        f=fixture();result,model=planned(f)
        before,cs,_,idx=placed(f,f[7]['display_rows']);after,_,_,_=placed(f,result['display_rows'])
        a=stacks.measure_stacks(before,cs,idx,model);b=stacks.measure_stacks(after,cs,idx,model)
        self.assertEqual(a['row_errors'],2);self.assertEqual(b['row_errors'],0)
        self.assertLess(stacks.stack_rank(b),stacks.stack_rank(a))
        self.assertEqual(a['count'],b['count'])

    def test_bad_terminal_contract_is_unknown_not_zero_errors(self):
        f=fixture();result,model=planned(f);pl,cs,_,idx=placed(f,result['display_rows'])
        cs[0]['nodes']['G']='different'
        measured=stacks.measure_stacks(pl,cs,idx,model)
        self.assertFalse(measured['measurable']);self.assertIsNone(stacks.stack_rank(measured))

    def test_nonfinite_coordinates_are_unknown(self):
        f=fixture();result,model=planned(f);pl,cs,_,idx=placed(f,result['display_rows'])
        pl.column_x=[float('nan')]*len(pl.column_x)
        self.assertIsNone(stacks.stack_rank(stacks.measure_stacks(pl,cs,idx,model)))

    def test_no_model_and_unapplied_groups_remain_not_applicable(self):
        f=fixture();pl,cs,_,idx=placed(f,f[7]['display_rows'])
        measured=stacks.measure_stacks(pl,cs,idx,{})
        self.assertEqual(measured['count'],0);self.assertFalse(measured['measurable'])
        self.assertEqual(stacks.stack_rank(measured),())

    def test_translation_and_horizontal_mirror_preserve_row_metrics(self):
        f=fixture();result,model=planned(f);pl,cs,_,idx=placed(f,result['display_rows'])
        expected=stacks.stack_rank(stacks.measure_stacks(pl,cs,idx,model))
        shifted=deepcopy(pl);shifted.row_y=[y+242 for y in pl.row_y];shifted.column_x=[x+386 for x in pl.column_x]
        self.assertEqual(stacks.stack_rank(stacks.measure_stacks(shifted,cs,idx,model)),expected)
        mirrored=deepcopy(pl);mirrored.set_state([(r,len(pl.column_x)-1-c) for r,c in pl.cell_of],[1-v for v in pl.orient])
        self.assertEqual(stacks.stack_rank(stacks.measure_stacks(mirrored,cs,idx,model)),expected)

    def test_all_initializer_installs_row_contract_without_passive_claim(self):
        f=fixture();plan=ai.build_attachment_plan(f[0],f[1],f[2],f[3],f[6],f[4],f[5],policy='all')
        pl,cs,nets,index=placed(f,plan['display_rows'])
        with patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('Astar')):
            result,report=ai.initialize_attachments(pl,f[0],cs,nets,index,plan,options=RoutingOptions())
        self.assertTrue(report['repeated_rows_applied']);self.assertFalse(report['passive_applied'])
        self.assertFalse(report['source_applied']);self.assertTrue(result.valid_state())
        rows=list(result.row_of)
        for number in range(150):
            prior=(list(result.cell_of),list(result.orient));proposal=result.propose()
            if proposal is not None:result.set_state(*proposal)
            self.assertTrue(result.valid_state());self.assertEqual(result.row_of,rows)
            if number%2:result.set_state(*prior)

    def test_passive_policy_does_not_enable_new_active_row_planner(self):
        f=fixture()
        with patch.object(stacks,'plan_stack_rows',side_effect=AssertionError('passive-only')):
            plan=ai.build_attachment_plan(f[0],f[1],f[2],f[3],f[6],f[4],f[5],policy='passive')
        self.assertFalse(plan['repeated_stacks']['rows_applied'])

    def test_plan_signature_and_rows_are_deterministic(self):
        f=fixture()
        args=(f[0],f[1],f[2],f[3],f[6],f[4],f[5])
        self.assertEqual(ai.build_attachment_plan(*args,policy='all'),ai.build_attachment_plan(*args,policy='all'))

    def test_serialized_manifest_measures_saved_rows_without_replanning(self):
        f=fixture();result,model=planned(f);pl,cs,_,idx=placed(f,result['display_rows'])
        restored=json.loads(json.dumps(model,allow_nan=False))
        with patch.object(stacks,'plan_stack_rows',side_effect=AssertionError('saved measurement replanned')):
            measured=stacks.measure_stacks(pl,cs,idx,restored)
        self.assertEqual(measured,stacks.measure_stacks(pl,cs,idx,model))

    def test_postprocess_restore_keeps_new_fixed_row_contract(self):
        f=fixture();plan=ai.build_attachment_plan(f[0],f[1],f[2],f[3],f[6],f[4],f[5],policy='all')
        pl,cs,nets,idx=placed(f,plan['display_rows'])
        original,_=ai.initialize_attachments(pl,f[0],cs,nets,idx,plan,options=RoutingOptions())
        changed=deepcopy(original);cells=list(changed.cell_of);i=idx[f[8]['high1']]
        row,col=cells[i];cells[i]=(row+1,col+1);changed.set_state(cells,changed.orient)
        restored,report=ai.finalize_attachment_state(changed,original)
        self.assertTrue(report['reverted']);self.assertEqual(restored.cell_of,original.cell_of)
        self.assertEqual(restored.attachment_plan['repeated_stacks'],plan['repeated_stacks'])

    def test_selection_requires_complete_common_stack_measurement_and_real_caps(self):
        f=fixture();plan=ai.build_attachment_plan(f[0],f[1],f[2],f[3],f[6],f[4],f[5],policy='all')
        before,cs,nets,idx=placed(f,f[7]['display_rows']);after,_,_,_=placed(f,plan['display_rows'])
        def row(number,pl,length,cross):
            meta=measure_candidate(pl,cs,idx,dict(candidate_id=number),plan,policy='all')
            return ((number,),pl,cs,nets,idx,meta,([],dict(wirelength=length,crossings=cross,bends=0),dict(valid=True)),{},str(number))
        reference=row(1,before,1000,2);better=row(2,after,1100,3)
        self.assertEqual(select_attachment_result([reference,better])[0][8],'2')
        better[5]['layout_attachments']['repeated_stack_organization']['count']=0
        self.assertIsNone(organization_rank(better[5]['layout_attachments']))
        for length,cross in ((1151,3),(1100,4)):
            self.assertEqual(select_attachment_result([reference,row(2,after,length,cross)])[0][8],'1')

    def test_reference_and_off_candidates_never_run_new_planner(self):
        d,_=devices()
        with TemporaryDirectory() as folder:
            path=Path(folder)/'input.cir';lines=['* repeat fixture','.model n nmos','.model p pmos']
            for n,c in d.items():lines.append('M'+n+' '+' '.join(c[k] for k in ('d','g','s'))+' '+c['bulk']+' '+('n' if c['type']=='NMOS' else 'p'))
            path.write_text('\n'.join(lines)+'\n.end\n',encoding='utf8')
            with patch.object(stacks,'plan_stack_rows',side_effect=AssertionError('reference/off')) as call:
                first=next(structured.create_candidates(path,iterations=0,attachment_layout='all'))
                off=list(structured.create_candidates(path,iterations=0,attachment_layout='off'))
            self.assertEqual(first[4]['candidate_id'],1);self.assertEqual(len(off),3);call.assert_not_called()

    def test_both_experimental_candidates_use_new_rows_before_SA_and_keep_reference(self):
        d,_=devices()
        with TemporaryDirectory() as folder:
            path=Path(folder)/'input.cir';lines=['* row fixture','.model n nmos','.model p pmos']
            for n,c in d.items():lines.append('M'+n+' '+' '.join(c[k] for k in ('d','g','s'))+' '+c['bulk']+' '+('n' if c['type']=='NMOS' else 'p'))
            path.write_text('\n'.join(lines)+'\n.end\n',encoding='utf8')
            off=list(structured.create_candidates(path,iterations=0,attachment_layout='off'))
            with patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('SA called Astar')):
                new=list(structured.create_candidates(path,iterations=0,attachment_layout='all'))
        self.assertEqual((new[0][0].cell_of,new[0][0].orient,new[0][0].rng.getstate()),
                         (off[0][0].cell_of,off[0][0].orient,off[0][0].rng.getstate()))
        for pl,cs,nets,index,metadata in new[1:]:
            plan=metadata['layout_attachments']['plan'];model=plan['repeated_stacks']
            self.assertTrue(model['rows_applied'])
            self.assertTrue(metadata['layout_attachments']['initialization']['repeated_rows_applied'])
            self.assertEqual(metadata['levels'],compute_levels({('M'+n):v for n,v in d.items()})['device_levels'])
            self.assertEqual(stacks.measure_stacks(pl,cs,index,model)['row_errors'],0)
            self.assertEqual(metadata['branch_row_plan']['rows'],plan['display_rows'])


if __name__=='__main__':unittest.main()
