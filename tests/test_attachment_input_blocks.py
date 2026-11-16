"""Verified BJT leaves, collector-side parents and neutral tail placement."""
from copy import deepcopy
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_passive_placement import convert, resistor, source
from tests.test_attachment_flow import candidate
from netlist2schematic.plugins.attachment import attachment_input_blocks as inputs
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment.attachment_selection import select_attachment_result
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.plugins.attachment.attachment_sources import build_source_groups


def fixture(*, rename=False, dual=False, reversed_source=False, policy='all'):
    devices = {}; cells = {}
    for bank, columns in [('u', ((10, 9), (5, 3))), ('v', ((7, 6), (2, 1)))]:
        junction = 'collector_' + bank
        for k, cols in enumerate(columns):
            emitter = 'em_' + bank + str(k)
            for arm, col in zip(('a', 'b'), cols):
                collector = junction if (k == 0 and arm == 'b') or (k == 1 and arm == 'a') else 'vcc'
                name = bank + str(k) + arm
                devices[name] = dict(cls='BJT', type='NPN', model='N', c=collector,
                    b='common_input' if arm == 'a' else 'reference_' + bank + str(k), e=emitter)
                cells[name] = 1, col
            name = 't' + bank + str(k)
            devices[name] = source('ISRC', 'pool' if reversed_source else emitter,
                                  emitter if reversed_source else 'pool')
        devices['f'+bank] = dict(cls='BJT', type='NPN', model='N', c='vcc', b=junction, e='output')
        devices['r'+bank] = resistor('vcc', junction)
    cells.update(fu=(1, 8), fv=(1, 4), ru=(0, 6), rv=(0, 5),
                 tu0=(2, 7), tu1=(2, 5), tv0=(2, 6), tv1=(2, 4))
    if dual:
        devices = {n: {k: 'PNP' if k == 'type' and v == 'NPN' else 'vss' if v == 'vcc' else v
                       for k, v in d.items()} for n, d in devices.items()}
    names = {n:n for n in devices}
    if rename:
        names = {n:'unit'+str(100-k) for k,n in enumerate(devices)}
        devices = {names[n]: {k:'signal_'+v if k in ('a','b','c','e','p','n')
            and v not in ('vcc','vss') else v for k,v in d.items()}
            for n,d in reversed(list(devices.items()))}
        cells = {names[n]:cell for n,cell in cells.items()}
    pl, comps, nets, index, _ = convert(devices, cells)
    structs, levels = analyze_structures(devices), compute_levels(devices)
    plan = ai.build_attachment_plan(devices, comps, nets, index, structs, levels, pl.row_of, policy=policy)
    return devices, ai._install(pl, comps, index, plan), comps, nets, index, plan, names


def state(pl):
    return deepcopy((pl.cell_of,pl.orient,pl.row_of,pl.comp_at,pl.column_x,pl.row_y,pl.rng.getstate(),
                     getattr(pl,'blocks',None),getattr(pl,'layout_macro_blocks',None)))


_UNSET = object()


def seed(f, *, safe=_UNSET, allowance=8, expired=False):
    d, pl, c, n, i, plan, names = f
    budget = ai.new_attachment_budget(max_candidates=allowance)
    report = dict(actions=[],candidate_trials=0)
    args = (structured.par,pl,pl,c,n,i,plan,RoutingOptions(),budget,
            perf_counter()+(-1 if expired else 1),report)
    with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar')), \
         patch('netlist2schematic.layout.structured_placement._anneal',side_effect=AssertionError('SA')):
        if safe is _UNSET:
            result = ai._input_block_seed(*args)
        else:
            with patch.object(ai,'_checked_static',return_value=safe):
                result = ai._input_block_seed(*args)
    return result,report,budget


class BjtInputBlockTests(unittest.TestCase):
    def test_four_proven_pairs_and_two_collector_banks_keep_unique_leaf_ownership(self):
        d,pl,c,n,i,plan,names = fixture(); model = plan['input_blocks']
        self.assertEqual(len(model['pairs']),4); self.assertEqual(len(model['banks']),2)
        members = [v for p in model['pairs'] for v in p['members']]
        self.assertEqual(len(members),len(set(members)))
        self.assertTrue(all(len(b['pairs'])==2 and len(b['members'])==4 for b in model['banks']))
        self.assertNotIn('fu',members); self.assertNotIn('tu0',members)
        self.assertEqual(len(model['tails']),4)

    def test_common_base_or_supply_does_not_create_a_collector_parent(self):
        d,pl,c,n,i,plan,names = fixture()
        removed = {name:v for name,v in d.items() if name not in ('fu','fv')}
        document = structured.to_document(removed)
        rows = {comp['id']:1 for comp in document['components']}
        model = inputs.compile_input_blocks(removed,document['components'],analyze_structures(removed),rows)
        self.assertEqual(len(model['pairs']),4); self.assertEqual(model['banks'],[])

    def test_emitter_followers_merging_outputs_are_not_misidentified_as_an_input_pair(self):
        _,_,_,_,_,plan,_ = fixture()
        self.assertFalse(any(set(p['members'])=={'fu','fv'} for p in plan['input_blocks']['pairs']))

    def test_clamped_or_power_emitter_nodes_are_not_accepted_as_input_pairs(self):
        d,pl,c,n,i,plan,names = fixture()
        d['clamp'] = source('VSRC','em_u0','vss')
        doc = structured.to_document(d)
        rows = {comp['id']:1 for comp in doc['components']}
        model = inputs.compile_input_blocks(d,doc['components'],analyze_structures(d),rows)
        self.assertFalse(any(set(p['members'])=={'u0a','u0b'} for p in model['pairs']))

    def test_incompatible_existing_rows_skip_only_that_pair_without_rewriting_levels(self):
        d,pl,c,n,i,plan,names = fixture(); levels = compute_levels(d); before = deepcopy(levels)
        rows = {name:pl.row_of[j] for name,j in i.items()}; rows['u0a'] = 3
        model = inputs.compile_input_blocks(d,c,analyze_structures(d),rows)
        self.assertEqual(len(model['pairs']),3)
        self.assertTrue(any(s['reason']=='input_pair_rows_incompatible' for s in model['skipped']))
        self.assertEqual(levels,before)

    def test_renaming_reordering_and_polarity_dual_preserve_topology_evidence(self):
        for kwargs in ({'rename':True},{'dual':True},{'rename':True,'dual':True}):
            with self.subTest(kwargs=kwargs):
                d,pl,c,n,i,plan,names = fixture(**kwargs); model = plan['input_blocks']
                self.assertEqual(len(model['pairs']),4); self.assertEqual(len(model['banks']),2)
                self.assertEqual({frozenset(p['members']) for p in model['pairs']},
                    {frozenset((names[b+str(k)+'a'],names[b+str(k)+'b'])) for b in ('u','v') for k in (0,1)})

    def test_unknown_remote_source_node_stays_signal_with_original_poles(self):
        d,pl,c,n,i,plan,names = fixture(reversed_source=True)
        before = deepcopy((d,c,n))
        self.assertEqual(plan['source_groups'],[])
        self.assertFalse(structured.par.power_marker_sym('pool'))
        result,report,budget = seed((d,pl,c,n,i,plan,names))
        self.assertTrue(report['input_initialization']['applied'])
        self.assertTrue(all(t['remote_net']=='pool' and t['pin']=='-' for t in plan['input_blocks']['tails']))
        self.assertTrue(all(t['remote_role']!='power' for t in plan['input_blocks']['tails']))
        self.assertEqual(build_source_groups(d,c,n,i)['groups'],[])
        self.assertEqual((d,c,n),before)
        self.assertFalse(report['actions'][0]['new_power_roles'])

    def test_seed_removes_interleaving_and_preserves_rows_axes_rng_and_original_data(self):
        f = fixture(); d,pl,c,n,i,plan,names = f; before = state(pl)
        old = inputs.measure_input_blocks(pl,c,i,plan['input_blocks'])
        result,report,budget = seed(f)
        new = inputs.measure_input_blocks(result,c,i,plan['input_blocks'])
        self.assertTrue(report['input_initialization']['applied'])
        self.assertLess(inputs.input_rank(new),inputs.input_rank(old))
        self.assertEqual(new['pair_interleaving'],0); self.assertEqual(new['bank_interleaving'],0)
        self.assertEqual(new['collector_outside_errors'],0)
        self.assertEqual((result.row_of,result.column_x,result.row_y),(pl.row_of,pl.column_x,pl.row_y))
        self.assertEqual(result.rng.getstate(),pl.rng.getstate()); self.assertEqual(state(pl),before)
        self.assertLessEqual(budget['candidates'],8)

    def test_tail_matching_uses_existing_free_columns_and_preserves_polarity(self):
        f = fixture(); d,pl,c,n,i,plan,names = f
        result,report,budget = seed(f)
        old = inputs.measure_input_blocks(pl,c,i,plan['input_blocks'])
        new = inputs.measure_input_blocks(result,c,i,plan['input_blocks'])
        self.assertLess(new['tail_distance'],old['tail_distance'])
        for tail in plan['input_blocks']['tails']:
            self.assertEqual(result.row_of[i[tail['device']]],pl.row_of[i[tail['device']]])
            self.assertIn(result.cell_of[i[tail['device']]][1],range(pl.cols))
            self.assertEqual(c[i[tail['device']]]['nodes'],tail['terminal_contract'])

    def test_passive_mode_keeps_source_cells_and_orientations(self):
        f = fixture(policy='passive'); d,pl,c,n,i,plan,names = f
        result,report,budget = seed(f)
        for tail in plan['input_blocks']['tails']:
            j = i[tail['device']]
            self.assertEqual((result.cell_of[j],result.orient[j]),(pl.cell_of[j],pl.orient[j]))

    def test_fixed_pair_rolls_back_and_fixed_tail_stays_at_its_real_position(self):
        f = fixture(); d,pl,c,n,i,plan,names = f
        pl.fixed.add(i['u0a']); before = state(pl)
        result,report,budget = seed(f)
        self.assertEqual(state(result),before); self.assertFalse(report['actions'])
        f = fixture(); d,pl,c,n,i,plan,names = f
        pl.fixed.add(i['tu0']); point = pl.pos_of(i['tu0']); orient = pl.orient[i['tu0']]
        result,report,budget = seed(f)
        self.assertEqual((result.pos_of(i['tu0']),result.orient[i['tu0']]),(point,orient))

    def test_static_failure_deadline_and_exhausted_budget_restore_every_state(self):
        for kwargs in ({'safe':False},{'safe':None},{'expired':True},{'allowance':0}):
            with self.subTest(kwargs=kwargs):
                f = fixture(); before = state(f[1]); result,report,budget = seed(f,**kwargs)
                self.assertEqual(state(result),before); self.assertEqual(state(f[1]),before)
                self.assertFalse(report['actions']); self.assertLessEqual(budget['candidates'],budget['max_candidates'])

    def test_existing_nonoverlapping_owners_are_preserved_and_overlap_is_rejected(self):
        comps = [dict(id=n) for n in ('old_a','old_b','new_a','new_b')]
        model = dict(pairs=[dict(members=['new_a','new_b'])])
        self.assertEqual(inputs.owner_blocks([[0,1],[2],[3]],comps,model),[['old_a','old_b'],['new_a','new_b']])
        self.assertIsNone(inputs.owner_blocks([[0,2],[1],[3]],comps,model))

    def test_measurement_is_translation_and_mirror_invariant(self):
        d,pl,c,n,i,plan,names = fixture()
        class View:
            cell_of = pl.cell_of
            def pos_of(self,j):
                x,y = pl.pos_of(j); return 4000-x,y+93
            def pin_world(self,j,pin):
                x,y = pl.pin_world(j,pin); return 4000-x,y+93
        self.assertEqual(inputs.input_rank(inputs.measure_input_blocks(pl,c,i,plan['input_blocks'])),
                         inputs.input_rank(inputs.measure_input_blocks(View(),c,i,plan['input_blocks'])))

    def test_changed_pair_follower_tail_or_missing_positions_are_unknown(self):
        d,pl,c,n,i,plan,names = fixture()
        for name,pin in (('u0a','B'),('fu','B'),('tu0','+')):
            changed = deepcopy(c); changed[i[name]]['nodes'][pin] = 'changed'
            bad = inputs.measure_input_blocks(pl,changed,i,plan['input_blocks'])
            self.assertFalse(bad['measurable']); self.assertIsNone(inputs.input_rank(bad))
        self.assertFalse(inputs.measure_input_blocks(pl,c,i,{})['measurable'])

    def test_leaf_and_parent_integrity_survive_existing_sa_moves_and_undo(self):
        f = fixture(); d,pl,c,n,i,plan,names = f; result,report,budget = seed(f)
        self.assertTrue(result.valid_state())
        with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar')):
            for k in range(150):
                before = deepcopy((result.cell_of,result.orient,result.row_of,result.comp_at))
                old = result.propose()
                if old is not None:
                    self.assertTrue(result.valid_state())
                    if k%2:
                        result.set_state(*old)
                        self.assertEqual((result.cell_of,result.orient,result.row_of,result.comp_at),before)
                m = inputs.measure_input_blocks(result,c,i,plan['input_blocks'])
                self.assertEqual(m['pair_interleaving'],0); self.assertEqual(m['bank_interleaving'],0)

    def test_input_gain_can_win_but_missing_evidence_and_route_caps_still_reject(self):
        old,new = candidate('1'),candidate('2',score=11,length=1100)
        m = dict(count=4,bank_count=2,measurable=True,row_errors=0,pair_interleaving=1,
            bank_interleaving=7,collector_outside_errors=2,tail_distance=8,normalized_span=10)
        old[5]['layout_attachments']['input_block_organization'] = deepcopy(m)
        m.update(pair_interleaving=0,bank_interleaving=0,collector_outside_errors=0,tail_distance=2)
        new[5]['layout_attachments']['input_block_organization'] = deepcopy(m)
        self.assertIs(select_attachment_result([old,new])[0],new)
        new[6][1]['crossings'] = 1
        self.assertIs(select_attachment_result([old,new])[0],old)
        new[6][1]['crossings'] = 0
        del new[5]['layout_attachments']['input_block_organization']
        self.assertIs(select_attachment_result([old,new])[0],old)

    def test_no_bjt_inputs_keeps_the_original_path_and_source_roles(self):
        d = dict(r=resistor('a','b')); pl,c,n,i,_ = convert(d,dict(r=(0,0)))
        model = inputs.compile_input_blocks(d,c,[],{'r':0})
        self.assertEqual(model['pairs'],[]); self.assertEqual(model['banks'],[])
        self.assertEqual(list(inputs.input_trials(pl,c,i,model)),[])

    def test_real_local_load_is_a_guest_but_common_supply_does_not_qualify_a_load(self):
        d,pl,c,n,i,plan,names = fixture(); model = plan['input_blocks']
        left = next(b for b in model['banks'] if b['net']=='collector_u')
        self.assertIn('ru',left['guest_members']); self.assertNotIn('rv',left['guest_members'])
        result,report,budget = seed((d,pl,c,n,i,plan,names))
        cells = list(result.cell_of)
        slots = sorted(cells[i[n]][1] for n in left['members'])
        # Insert the actual collector load by opening a hole inside this bank.
        bank_ids = {i[n] for n in left['members']}
        for j in bank_ids:
            r,col = cells[j]
            cells[j] = r,col+2 if col>slots[0] else col
        cells[i['ru']] = 1,slots[0]+1
        result.set_state(cells,result.orient)
        measured = inputs.measure_input_blocks(result,c,i,model)
        bank = next(b for b in measured['banks'] if b['id']==left['id'])
        self.assertIn('ru',bank['related_guests_inside']); self.assertNotIn('ru',bank['interlopers'])

    def test_existing_role_orders_and_independent_macro_motion_are_not_overwritten(self):
        d,pl,c,n,i,plan,names = fixture()
        old_order = [[[i['fu'],i['fv']]]]
        old_macro = [[i['fu'],i['fv']]]
        old_motion = [[i['ru'],i['rv']]]
        pl.role_orders = deepcopy(old_order)
        pl.layout_macro_blocks = deepcopy(old_macro)
        pl.motion_blocks = deepcopy(old_motion)
        before = state(pl)
        trial,action = next(inputs.input_trials(pl,c,i,plan['input_blocks'],move_tails=True))
        self.assertEqual(trial.role_orders[:len(old_order)],old_order)
        self.assertIn(old_macro[0],trial.layout_macro_blocks)
        self.assertIn(old_macro[0],trial.motion_blocks)
        self.assertIn(old_motion[0],trial.motion_blocks)
        self.assertEqual(state(pl),before)


if __name__=='__main__':
    unittest.main()
