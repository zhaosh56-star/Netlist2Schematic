"""Existing formal-port validation reaches shared-source MOS leaf ownership."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_layout_evidence import bank
from tests.test_passive_placement import convert
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment import attachment_input_blocks as inputs
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.routing.routing_rules import RoutingOptions


def fixture(*, formals=('ip','im'), interloper=False, rename=False, third_control=False):
    ds=bank();ds['t2']=deepcopy(ds['t'])
    if third_control:ds['ra']['g']='iextra'
    cells=dict(a=(4,1),b=(4,4 if interloper else 2),ra=(2,1),rb=(2,4),
        ua=(3,1),ub=(3,4),pa=(1,1),pb=(1,4),t=(5,2),t2=(5,3))
    if interloper:
        ds['extra']=dict(cls='MOS',type='NMOS',d='other',g='other_gate',s='0',bulk='0')
        cells['extra']=(4,2)
    mapping={n:n for n in ds}
    if rename:
        mapping={n:'device_'+str(90-i) for i,n in enumerate(ds)}
        ds={mapping[n]:{k:('node_'+v if k in ('d','g','s') and v not in ('0','vdd') else v)
            for k,v in d.items()} for n,d in reversed(list(ds.items()))}
        cells={mapping[n]:cell for n,cell in cells.items()}
        formals=tuple('node_'+p for p in formals)
    pl,cs,nets,index,_=convert(ds,cells)
    plan=ai.build_attachment_plan(ds,cs,nets,index,analyze_structures(ds),compute_levels(ds),
        pl.row_of,policy='all',formal_ports=formals)
    return ds,ai._install(pl,cs,index,plan),cs,nets,index,plan,mapping


def snapshot(pl):
    return deepcopy((pl.cell_of,pl.orient,pl.row_of,pl.comp_at,pl.column_x,pl.row_y,pl.blocks,pl.rng.getstate()))


def seed(f,safe='real',expired=False):
    ds,pl,cs,nets,index,plan,_=f;budget=ai.new_attachment_budget()
    report=dict(actions=[],candidate_trials=0)
    args=(structured.par,pl,pl,cs,nets,index,plan,RoutingOptions(),budget,
        perf_counter()+(-1 if expired else 1),report)
    if safe=='real':result=ai._input_block_seed(*args)
    else:
        with patch.object(ai,'_checked_static',return_value=safe):result=ai._input_block_seed(*args)
    return result,report,budget


class SharedInputTests(unittest.TestCase):
    def test_explicit_two_controls_restore_only_one_shared_source_input_leaf(self):
        ds,pl,cs,nets,index,plan,names=fixture();model=plan['input_blocks'];pair,=model['pairs']
        self.assertEqual({'a','b'},set(pair['members']))
        self.assertEqual('differential_pair',pair['kind'])
        self.assertEqual({'t','t2'},set(pair['roles']['shared_tails']))
        self.assertEqual([],pair['roles']['exclusive_tails'])
        self.assertEqual([],model['tails']);self.assertEqual([],model['banks'])
        self.assertTrue(plan['supported'])
        self.assertEqual({'a','b','ra','rb'},set(pair['roles']['source_users']))

    def test_missing_or_ambiguous_formals_do_not_invent_a_pair(self):
        for formals in ((),('ip',)):
            f=fixture(formals=formals)
            self.assertEqual([],f[5]['input_blocks']['pairs'],formals)
            self.assertTrue(f[5]['input_blocks']['evidence_diagnostics']['shared_source_banks'])
        f=fixture(formals=('ip','im','iextra'),third_control=True)
        self.assertEqual([],f[5]['input_blocks']['pairs'])

    def test_formal_output_declaration_cannot_turn_derived_feedback_into_external_control(self):
        f=fixture(formals=('ip','im','left','right'))
        self.assertEqual({'a','b'},set(f[5]['input_blocks']['pairs'][0]['members']))

    def test_ordinary_two_user_MOS_pair_keeps_its_original_owner(self):
        from tests.test_layout_evidence import mos,source
        ds=dict(a=mos('oa','ip','tail'),b=mos('ob','im','tail'),t=source('tail'))
        pl,cs,nets,index,_=convert(ds,dict(a=(1,1),b=(1,2),t=(2,1)))
        m=inputs.compile_input_blocks(ds,cs,analyze_structures(ds),{c['id']:pl.row_of[i] for i,c in enumerate(cs)},
            formal_ports=('ip','im'))
        self.assertEqual([],m['pairs'])

    def test_polarity_dual_recovers_equivalent_shared_input_roles(self):
        f=fixture();ds=deepcopy(f[0]);cells={c['id']:f[1].cell_of[i] for i,c in enumerate(f[2])}
        for d in ds.values():
            d['type']='PMOS' if d['type']=='NMOS' else 'NMOS'
            for key in ('d','g','s'):
                if d[key] in ('0','vdd'):d[key]={'0':'vdd','vdd':'0'}[d[key]]
        pl,cs,nets,index,_=convert(ds,cells)
        plan=ai.build_attachment_plan(ds,cs,nets,index,analyze_structures(ds),compute_levels(ds),pl.row_of,
            policy='all',formal_ports=('ip','im'))
        pair,=plan['input_blocks']['pairs']
        self.assertEqual({'a','b'},set(pair['members']))
        self.assertEqual({'t','t2'},set(pair['roles']['shared_tails']))

    def test_existing_protected_MOS_owner_is_not_split_to_install_new_pair(self):
        f=fixture();ds,pl,cs,nets,index,_,_=f
        structs=analyze_structures(ds)+[dict(id='existing',kind='protected_core',devices=['a','ra'],confidence='high')]
        plan=ai.build_attachment_plan(ds,cs,nets,index,structs,compute_levels(ds),pl.row_of,
            formal_ports=('ip','im'))
        self.assertEqual([],plan['input_blocks']['pairs'])
        self.assertTrue(any(s['reason']=='preexisting_MOS_owner' for s in plan['input_blocks']['skipped']))

    def test_formal_inputs_cannot_override_a_rail_or_voltage_clamped_source(self):
        for mode in ('rail','voltage'):
            f=fixture();ds=deepcopy(f[0]);cells={c['id']:f[1].cell_of[i] for i,c in enumerate(f[2])}
            if mode=='rail':
                for n in ('a','b','ra','rb'):ds[n]['s']='0'
            else:
                ds['v']=dict(cls='VSRC',type='V',p='tail',n='0',value='0');cells['v']=(0,0)
            pl,cs,nets,index,_=convert(ds,cells)
            plan=ai.build_attachment_plan(ds,cs,nets,index,analyze_structures(ds),compute_levels(ds),pl.row_of,
                formal_ports=('ip','im'))
            self.assertEqual([],plan['input_blocks']['pairs'],mode)

    def test_pair_can_install_policy_without_claiming_a_position_improvement(self):
        f=fixture();ds,pl,cs,nets,index,plan,_=f;before=snapshot(pl)
        result,report,budget=seed(f)
        self.assertEqual(1,len(report['actions']))
        action=report['actions'][0]
        self.assertTrue(action['policy_only_adoption'])
        self.assertEqual(pl.cell_of,result.cell_of);self.assertEqual(pl.orient,result.orient)
        self.assertEqual((pl.column_x,pl.row_y),(result.column_x,result.row_y))
        self.assertEqual(before,snapshot(pl));self.assertTrue(result.valid_state())
        self.assertIn({index['a'],index['b']},[set(b) for b in result.blocks])
        self.assertTrue(ai._checked_static(structured.par,result,nets,index,RoutingOptions(),None))
        self.assertEqual(1,budget['candidates'])

    def test_shared_tails_and_regulation_devices_never_join_input_ownership(self):
        f=fixture();result,report,budget=seed(f);index=f[4]
        leaf=next(b for b in result.blocks if index['a'] in b)
        self.assertEqual({index['a'],index['b']},set(leaf))
        self.assertFalse(set(leaf)&{index[n] for n in ('t','t2','ra','rb')})

    def test_many_existing_proposals_and_undo_preserve_rows_occupancy_and_pair_membership(self):
        f=fixture();pl,_,_=seed(f);rows=dict(pl.fixed_rows);index=f[4]
        legal=0
        for i in range(250):
            before=(list(pl.cell_of),list(pl.orient));proposal=pl.propose()
            if proposal is None:
                self.assertEqual(before,(pl.cell_of,pl.orient))
                continue
            legal+=1
            pl.set_state(*proposal)
            self.assertTrue(pl.valid_state())
            self.assertEqual(len(pl.cell_of),len(pl.comp_at))
            self.assertTrue(all(pl.row_of[j]==r for j,r in rows.items()))
            self.assertIn({index['a'],index['b']},[set(b) for b in pl.blocks])
            if i%2:pl.set_state(*before)
            self.assertEqual({cell:j for j,cell in enumerate(pl.cell_of)},pl.comp_at)
        self.assertGreater(legal,0)

    def test_fixed_pair_or_failed_static_checks_never_commit_new_policy(self):
        f=fixture();pl=f[1];pl.fixed.add(f[4]['a']);before=snapshot(pl)
        result,report,budget=seed(f)
        self.assertIs(result,pl);self.assertEqual(before,snapshot(pl));self.assertFalse(report['actions'])
        for safe in (False,None):
            f=fixture();before=snapshot(f[1]);result,report,budget=seed(f,safe=safe)
            self.assertIs(result,f[1]);self.assertEqual(before,snapshot(f[1]));self.assertFalse(report['actions'])
            self.assertEqual(safe is None,bool(budget.get('exhausted')))

    def test_expired_shared_budget_does_not_accept_policy(self):
        f=fixture();result,report,budget=seed(f,expired=True)
        self.assertIs(result,f[1]);self.assertFalse(report['actions']);self.assertEqual(0,budget['candidates'])

    def test_contract_includes_unselected_source_users_and_tail_evidence(self):
        f=fixture();ds,pl,cs,nets,index,plan,_=f
        for name in ('a','ra','t','t2','ua'):
            changed=deepcopy(cs);changed[index[name]]['nodes']['G']='different'
            m=inputs.measure_input_blocks(pl,changed,index,plan['input_blocks'])
            self.assertFalse(m['measurable']);self.assertIsNone(inputs.input_rank(m))

    def test_renamed_reordered_circuit_restores_equivalent_pair_and_shared_tails(self):
        for rename in (False,True):
            f=fixture(rename=rename);names=f[6];pair,=f[5]['input_blocks']['pairs']
            self.assertEqual({names['a'],names['b']},set(pair['members']))
            self.assertEqual({names['t'],names['t2']},set(pair['roles']['shared_tails']))
            result,report,_=seed(f)
            self.assertTrue(report['actions'])

    def test_member_measurements_are_translation_and_horizontal_mirror_invariant(self):
        f=fixture();pl,_,_=seed(f);cs,index,model=f[2],f[4],f[5]['input_blocks']
        original=inputs.input_rank(inputs.measure_input_blocks(pl,cs,index,model))
        for mirror in (False,True):
            class View:
                cell_of=pl.cell_of
                def pos_of(self,i):
                    x,y=pl.pos_of(i);return (-x if mirror else x)+37,y-53
            self.assertEqual(original,inputs.input_rank(inputs.measure_input_blocks(View(),cs,index,model)))

    def test_policy_adoption_does_not_read_GT_or_call_SA_or_Astar(self):
        f=fixture()
        with patch.object(Path,'read_text',side_effect=AssertionError('no files')),patch(
                'netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('no Astar')),patch(
                'netlist2schematic.layout.structured_placement._anneal',side_effect=AssertionError('no SA')):
            result,report,budget=seed(f)
        self.assertTrue(report['actions'])


class SharedInputFlowTests(unittest.TestCase):
    def make_source(self,path):
        ds=bank();ds['t2']=deepcopy(ds['t'])
        text=['.subckt cell vdd 0 ip im bias pbias','.model nn nmos','.model pp pmos']
        for name,d in ds.items():
            text.append('M'+name+' '+d['d']+' '+d['g']+' '+d['s']+' 0 '+('pp' if d['type']=='PMOS' else 'nn'))
        text.append('.ends cell');path.write_text('\n'.join(text)+'\n',encoding='utf8')

    def test_new_candidates_and_independent_measurement_share_formal_evidence(self):
        from tools.compare_attachment_integration import build_measurement_plan
        with TemporaryDirectory() as td:
            path=Path(td)/'unrelated-name.cir';self.make_source(path)
            with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('no Astar')):
                candidates=list(structured.create_candidates(path,cell='cell',iterations=0,seed=10,attachment_layout='all'))
            self.assertEqual([1,2,3],[c[4]['candidate_id'] for c in candidates])
            for pl,cs,nets,index,meta in candidates[1:]:
                plan=meta['layout_attachments']['plan'];pair,=plan['input_blocks']['pairs']
                self.assertEqual({'Ma','Mb'},set(pair['members']))
                measured=build_measurement_plan(path,'cell',cs,nets,index,'all')
                self.assertEqual(plan['signature'],measured['signature'])

    def test_reference_and_off_paths_do_not_request_attachment_formal_context(self):
        with TemporaryDirectory() as td:
            path=Path(td)/'file.cir';self.make_source(path)
            with patch('netlist2schematic.rendering.presentation.prepare_presentation',side_effect=AssertionError('new attachment evidence')):
                first=next(structured.create_candidates(path,cell='cell',iterations=0,attachment_layout='all'))
                off=list(structured.create_candidates(path,cell='cell',iterations=0,attachment_layout='off'))
            self.assertEqual(1,first[4]['candidate_id']);self.assertEqual(3,len(off))


if __name__=='__main__':unittest.main()
