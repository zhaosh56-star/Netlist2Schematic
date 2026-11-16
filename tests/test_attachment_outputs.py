"""Neutral output leaves preserve topology, input owners and old display rows."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from tests.test_attachment_input_blocks import fixture, seed, state
from tests.test_attachment_flow import candidate
from tests.test_passive_placement import convert
from netlist2schematic.plugins.attachment import attachment_outputs as outputs
from netlist2schematic.plugins.attachment import attachment_input_blocks as inputs
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment.attachment_selection import select_attachment_result
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.recognition.device_leveling import compute_levels


def rebuild(devices, original):
    old = original[1]
    cells = {c['id']: old.cell_of[j] for j,c in enumerate(original[2]) if c['id'] in devices}
    for name in devices:
        cells.setdefault(name,(3,len(cells)))
    pl,comps,nets,index,_ = convert(devices,cells)
    plan = ai.build_attachment_plan(devices,comps,nets,index,analyze_structures(devices),
        compute_levels(devices),pl.row_of,policy='all')
    return devices,ai._install(pl,comps,index,plan),comps,nets,index,plan,original[-1]


class EmitterOutputLeafTests(unittest.TestCase):
    def test_proven_outputs_form_one_neutral_leaf_separate_from_input_and_tail_owners(self):
        _,_,_,_,_,plan,_ = fixture()
        model = plan['output_merges']
        self.assertEqual(len(model['groups']),1)
        group = model['groups'][0]
        self.assertEqual(set(group['members']),{'fu','fv'})
        self.assertIsNone(group['function_claim'])
        self.assertEqual(len(set(group['bank_refs'].values())),2)
        self.assertEqual(len(plan['input_blocks']['pairs']),4)
        owned = {n for p in plan['input_blocks']['pairs'] for n in p['members']}
        self.assertTrue(owned.isdisjoint(group['members']))
        self.assertFalse(any(n.startswith('t') for n in group['members']))

    def test_common_supply_and_emitter_without_verified_parent_access_is_insufficient(self):
        f = fixture()
        d = {n:v for n,v in f[0].items() if n in ('fu','fv')}
        new = rebuild(d,f)
        self.assertEqual(new[5]['output_merges']['groups'],[])

    def test_only_one_bank_does_not_create_an_output_pair(self):
        f = fixture();d = deepcopy(f[0])
        del d['v0b'];del d['tv0']
        new = rebuild(d,f)
        self.assertEqual(len(new[5]['input_blocks']['banks']),1)
        self.assertEqual(new[5]['output_merges']['groups'],[])

    def test_shared_control_split_emitters_and_three_followers_are_not_two_way_leaves(self):
        for mutate in ('same_base','split_output','three_outputs'):
            f=fixture();d=deepcopy(f[0])
            if mutate=='same_base':d['fv']['b']=d['fu']['b']
            elif mutate=='split_output':d['fv']['e']='other_output'
            else:d['third']=dict(d['fu'],b='another_control')
            with self.subTest(case=mutate):
                self.assertEqual(rebuild(d,f)[5]['output_merges']['groups'],[])

    def test_two_independently_confirmed_supply_nodes_are_allowed(self):
        f=fixture();d=deepcopy(f[0]);d['fv']['c']='avdd'
        new=rebuild(d,f)
        self.assertEqual(len(new[5]['output_merges']['groups']),1)
        self.assertEqual(new[0],d)

    def test_renaming_reordering_and_polarity_dual_preserve_neutral_members(self):
        for kwargs in ({'rename':True},{'dual':True},{'rename':True,'dual':True}):
            with self.subTest(**kwargs):
                d,pl,c,n,i,plan,names=fixture(**kwargs)
                group=plan['output_merges']['groups'][0]
                self.assertEqual(set(group['members']),{names['fu'],names['fv']})
                self.assertIsNone(group['function_claim'])

    def test_incompatible_existing_rows_skip_output_without_losing_input_pairs(self):
        d,pl,c,n,i,plan,_=fixture()
        rows={name:pl.row_of[j] for name,j in i.items()};rows['fv']+=1
        model=outputs.compile_output_groups(c,rows,plan['input_blocks'])
        self.assertEqual(model['groups'],[])
        self.assertEqual(model['skipped'][0]['reason'],'output_merge_rows_incompatible')
        self.assertEqual(len(plan['input_blocks']['pairs']),4)

    def test_same_row_seed_shortens_output_span_with_original_axes_rng_and_validation(self):
        f=fixture();d,pl,c,n,i,plan,_=f;before=state(pl)
        old=outputs.measure_outputs(pl,c,i,plan['output_merges'],plan['input_blocks'])
        result,report,budget=seed(f)
        new=outputs.measure_outputs(result,c,i,plan['output_merges'],plan['input_blocks'])
        self.assertTrue(report['input_initialization']['applied'])
        self.assertLess(outputs.output_rank(new),outputs.output_rank(old))
        self.assertEqual(new['interleaving'],0)
        self.assertEqual(new['row_errors'],0)
        self.assertEqual((result.row_of,result.row_y,result.column_x),(pl.row_of,pl.row_y,pl.column_x))
        self.assertTrue(result.valid_state());self.assertEqual(state(pl),before)
        self.assertEqual(result.rng.getstate(),pl.rng.getstate())

    def test_fixed_output_preserves_the_original_fixed_row_fallback_and_input_trials(self):
        f=fixture();d,pl,c,n,i,plan,_=f
        pl.fixed.add(i['fu']);point=pl.pos_of(i['fu']);angle=pl.orient[i['fu']]
        result,report,budget=seed(f)
        self.assertEqual((result.pos_of(i['fu']),result.orient[i['fu']]),(point,angle))
        original=list(inputs.input_trials(pl,c,i,plan['input_blocks']))
        current=list(inputs.input_trials(pl,c,i,plan['input_blocks'],output_model=plan['output_merges']))
        self.assertEqual(len(original),len(current))
        self.assertTrue(current)
        for (old,_),(new,action) in zip(original,current):
            self.assertEqual((old.cell_of,old.orient),(new.cell_of,new.orient))
            self.assertTrue(action['skipped_fixed_output_groups'])

    def test_complete_rollback_when_static_check_or_deadline_prevents_commit(self):
        for kwargs in ({'safe':False},{'expired':True},{'allowance':0}):
            f=fixture();before=state(f[1])
            result,report,budget=seed(f,**kwargs)
            with self.subTest(**kwargs):
                self.assertEqual(state(result),before)
                self.assertEqual(state(f[1]),before)
                self.assertFalse(report['actions'])

    def test_output_measurement_is_translation_and_mirror_invariant(self):
        _,pl,c,n,i,plan,_=fixture()
        class View:
            cell_of=pl.cell_of
            def pos_of(self,j):
                x,y=pl.pos_of(j);return 3000-x,y+90
            def pin_world(self,j,pin):
                x,y=pl.pin_world(j,pin);return 3000-x,y+90
        measure=lambda obj:outputs.output_rank(outputs.measure_outputs(obj,c,i,
            plan['output_merges'],plan['input_blocks']))
        self.assertEqual(measure(pl),measure(View()))

    def test_changed_terminals_missing_parent_and_nonfinite_points_remain_unknown(self):
        _,pl,c,n,i,plan,_=fixture()
        changed=deepcopy(c);changed[i['fu']]['nodes']['B']='wrong'
        model=deepcopy(plan['input_blocks']);model['banks']=[]
        class BadView:
            cell_of=pl.cell_of
            def pos_of(self,j):return float('nan'),0
            def pin_world(self,j,pin):return pl.pin_world(j,pin)
        for obj,comps,parents in ((pl,changed,plan['input_blocks']),(pl,c,model),(BadView(),c,plan['input_blocks'])):
            measured=outputs.measure_outputs(obj,comps,i,plan['output_merges'],parents)
            self.assertFalse(measured['measurable']);self.assertIsNone(outputs.output_rank(measured))

    def test_existing_sa_proposals_and_rejection_preserve_each_separate_local_leaf(self):
        f=fixture();_,_,c,n,i,plan,_=f;pl,_,_=seed(f)
        with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar')):
            for k in range(100):
                before=deepcopy((pl.cell_of,pl.orient,pl.row_of,pl.comp_at))
                previous=pl.propose()
                if previous is not None:
                    self.assertTrue(pl.valid_state())
                    if k%2:
                        pl.set_state(*previous)
                        self.assertEqual((pl.cell_of,pl.orient,pl.row_of,pl.comp_at),before)
                m=outputs.measure_outputs(pl,c,i,plan['output_merges'],plan['input_blocks'])
                self.assertEqual(m['interleaving'],0)
                self.assertEqual(inputs.measure_input_blocks(pl,c,i,plan['input_blocks'])['bank_interleaving'],0)

    def test_output_organization_can_win_only_with_common_evidence_and_original_route_caps(self):
        old,new=candidate('1'),candidate('2',score=12,length=1100)
        for row,span in ((old,6),(new,1)):
            row[5]['layout_attachments']['output_merge_organization']=dict(count=1,measurable=True,
                row_errors=0,interleaving=0,emitter_span=span,bank_access_distance=5)
        self.assertIs(select_attachment_result([old,new])[0],new)
        self.assertEqual(new[7]['score'],12)
        new[6][1]['crossings']=1
        self.assertIs(select_attachment_result([old,new])[0],old)
        new[6][1]['crossings']=0
        new[5]['layout_attachments']['output_merge_organization']['count']=0
        self.assertIs(select_attachment_result([old,new])[0],old)


if __name__=='__main__':
    unittest.main()
