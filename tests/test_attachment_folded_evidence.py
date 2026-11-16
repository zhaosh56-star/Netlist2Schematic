"""Unique R/L supply evidence preserves folded branch identity without GT."""
from copy import deepcopy
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_passive_placement import mos,resistor,source,convert
from tests.test_attachment_folded import seed,state
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment import attachment_folded as folded
from netlist2schematic.plugins.attachment.attachment_folded_evidence import detect_folded_RL
from netlist2schematic.plugins.layout_shared.layout_evidence import build_layout_evidence
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.netlist.netlist_core import to_document,SRC_ALIASES
from netlist2schematic.layout.structured_placement import par
from netlist2schematic.routing.routing_rules import RoutingOptions


def circuit(*,dual=False,rename=False,long_chain=False):
    def p(d,g,s):return dict(cls='MOS',type='PMOS',model='P',d=d,g=g,s=s,bulk='vdd')
    d=dict(ia=mos('ja','ga','tail'),ib=mos('jb','gb','tail'),
        fa=p('oa','fold_bias','ja'),fb=p('ob','fold_bias','jb'),
        ba=p('ja','supply_bias','sa'),bb=p('jb','supply_bias','sb'),
        rail_a=resistor('vdd','sa'),rail_b=resistor('vdd','sb'),
        it=source('ISRC','tail','0'),bridge=resistor('oa','ob'),unrelated=resistor('other','0'))
    cells=dict(ba=(1,2),bb=(1,3),ia=(3,1),fa=(3,2),fb=(3,3),ib=(3,4),
        rail_a=(0,2),rail_b=(0,3),it=(5,2),bridge=(2,5),unrelated=(0,6))
    if long_chain:
        d['rail_a']['b']='mid_a';d['rail_b']['b']='mid_b'
        d.update(chain_a=resistor('mid_a','sa','L'),chain_b=resistor('mid_b','sb','L'))
        cells.update(chain_a=(0,0),chain_b=(0,1))
    if dual:
        for row in d.values():
            if row['cls']=='MOS':
                row['type']='NMOS' if row['type']=='PMOS' else 'PMOS'
                for k in ('s','bulk'):row[k]='0' if row[k]=='vdd' else 'vdd' if row[k]=='0' else row[k]
            elif row['cls']=='PASSIVE':
                for k in ('a','b'):row[k]='0' if row[k]=='vdd' else 'vdd' if row[k]=='0' else row[k]
            elif row['cls']=='ISRC':row['p'],row['n']='vdd','tail'
        cells={n:(6-r,c) for n,(r,c) in cells.items()}
    names={n:n for n in d}
    if rename:
        names={n:'component'+str(80-k) for k,n in enumerate(d)}
        d={names[n]:{k:'node_'+v if k in ('d','g','s','a','b','p','n') and v not in ('0','vdd') else v
            for k,v in row.items()} for n,row in reversed(list(d.items()))}
        cells={names[n]:cell for n,cell in cells.items()}
    return d,cells,names


def build(d,cells,names=None,*,reflect_rails=False):
    structs=analyze_structures(d);pl,c,n,i,_=convert(d,cells,records=structs)
    plan=ai.build_attachment_plan(d,c,n,i,structs,compute_levels(d),pl.row_of,policy='all')
    pl=ai._install(pl,c,i,plan)
    if reflect_rails:
        for j,comp in enumerate(c):
            if comp['symbol']=='resistor':pl.orient[j]=2 if 2 in pl.allowed_orientations(j) else 1
    return d,pl,c,n,i,plan,names or {n:n for n in d}


def model(d,cells):return build(d,cells)[5]['folded_cores']


class RailFoldedEvidenceTests(unittest.TestCase):
    def test_separate_supply_nodes_form_one_port_proven_parent_and_three_leaves(self):
        d,cells,names=circuit();f=build(d,cells,names);g=f[5]['folded_cores']['groups'][0]
        self.assertFalse(any(s['kind']=='folded_core' for s in analyze_structures(d)))
        self.assertEqual(len(g['members']),6);self.assertEqual([len(v) for v in g['leaves']],[2,2,2])
        self.assertTrue(g['rail_supply_derived']);self.assertEqual(set(g['supply_members']),{'rail_a','rail_b'})
        self.assertEqual(g['supply_paths']['ba']['path'],['sa','vdd'])
        self.assertNotEqual(d['ba']['s'],d['bb']['s'])
        self.assertNotIn('it',g['members']);self.assertNotIn('rail_a',g['members'])

    def test_polarity_dual_renaming_and_input_order_keep_equivalent_branch_roles(self):
        for dual in (False,True):
            for rename in (False,True):
                d,cells,names=circuit(dual=dual,rename=rename);g=build(d,cells,names)[5]['folded_cores']['groups'][0]
                triples=set(zip(g['inputs'],g['folded'],g['bias']))
                self.assertEqual(triples,{(names['ia'],names['fa'],names['ba']),(names['ib'],names['fb'],names['bb'])})
                expected='0' if dual else 'vdd'
                self.assertEqual({p['rail'] for p in g['supply_paths'].values()},{expected})

    def test_ordered_RL_chain_preserves_all_original_supply_port_contracts(self):
        d,cells,names=circuit(long_chain=True);g=model(d,cells)['groups'][0]
        proof=g['supply_paths']['ba']
        self.assertEqual(proof['members'],['chain_a','rail_a'])
        self.assertEqual(proof['path'],['sa','mid_a','vdd'])
        self.assertTrue({'rail_a','rail_b','chain_a','chain_b'}<=set(g['terminal_contract']))

    def test_capacitor_or_diode_is_not_a_supply_continuation(self):
        for kind in ('C','D'):
            d,cells,_=circuit();d['rail_a']['type']=kind
            self.assertEqual(model(d,cells)['groups'],[])

    def test_tap_inside_RL_chain_stops_expansion(self):
        for kind in ('capacitor','control'):
            d,cells,_=circuit(long_chain=True)
            d['tap']=resistor('mid_a','extra','C') if kind=='capacitor' else mos('extra','mid_a')
            cells['tap']=(4,7)
            self.assertEqual(model(d,cells)['groups'],[])

    def test_shared_bias_source_or_parallel_supply_path_is_ambiguous(self):
        for kind in ('shared','parallel'):
            d,cells,_=circuit()
            if kind=='shared':d['bb']['s']='sa';del d['rail_b'];del cells['rail_b']
            else:d['extra_rail']=resistor('sa','vdd');cells['extra_rail']=(0,7)
            self.assertEqual(model(d,cells)['groups'],[])

    def test_unknown_wrong_polarity_or_different_rail_does_not_get_merged(self):
        for kind in ('unknown','wrong','different'):
            d,cells,_=circuit()
            rail=('unproven' if kind=='unknown' else '0' if kind=='wrong' else
                  next(n for n in sorted(SRC_ALIASES) if n!='vdd'))
            d['rail_b']['a']=rail
            self.assertEqual(model(d,cells)['groups'],[])

    def test_multiple_folded_candidates_and_extra_conduction_are_rejected(self):
        for kind in ('duplicate','opposite','bjt'):
            d,cells,_=circuit()
            if kind=='duplicate':d['extra']=deepcopy(d['fa']);d['extra']['d']='extra'
            elif kind=='opposite':d['extra']=mos('ja','extra')
            else:d['extra']=dict(cls='BJT',type='NPN',model='N',c='ja',b='extra',e='0')
            cells['extra']=(4,7)
            self.assertEqual(model(d,cells)['groups'],[])

    def test_passive_feedback_at_fork_is_not_mistaken_for_an_extra_conduction_branch(self):
        d,cells,_=circuit();d['feedback']=resistor('ja','oa','C');cells['feedback']=(2,6)
        self.assertEqual(len(model(d,cells)['groups']),1)

    def test_mismatched_fold_or_bias_controls_do_not_establish_correspondence(self):
        for name in ('fa','ba'):
            d,cells,_=circuit();d[name]['g']='unrelated_control'
            self.assertEqual(model(d,cells)['groups'],[])

    def test_a_clamped_input_source_cannot_bypass_calibrated_pair_evidence(self):
        d,cells,_=circuit();d['clamp']=source('VSRC','tail','0');cells['clamp']=(5,6)
        self.assertEqual(model(d,cells)['groups'],[])

    def test_external_owner_and_incompatible_rows_still_stop_parent_installation(self):
        d,cells,_=circuit();f=build(d,cells);g=f[5]['folded_cores']['groups'][0]
        rows={v['id']:f[1].row_of[j] for j,v in enumerate(f[2])}
        blocked=folded.compile_folded(d,f[2],analyze_structures(d),rows,[g['members']+['unrelated']])
        self.assertEqual(blocked['groups'],[])
        rows['ba']+=1
        self.assertEqual(folded.compile_folded(d,f[2],analyze_structures(d),rows,[])['groups'],[])

    def test_true_pin_axes_only_connect_bias_to_fold_and_reuse_old_cost_scale(self):
        d,cells,names=circuit();f=build(d,cells,names);pl,c,n,i,plan=f[1:6]
        axes=[a for a in plan['host_axes'] if a.get('evidence')=='verified_RL_fed_folded_continuation']
        self.assertEqual({(a['a'],a['pin_a'],a['b'],a['pin_b']) for a in axes},{('ba','D','fa','S'),('bb','D','fb','S')})
        self.assertFalse(any('ia' in (a['a'],a['b']) or 'ib' in (a['a'],a['b']) for a in axes))
        before=ai.score_attachments(pl,c,i,plan)['weighted_total']
        moved=deepcopy(pl);poses=list(moved.cell_of);poses[i['ba']]=(poses[i['ba']][0],0);moved.set_state(poses,moved.orient)
        self.assertGreater(ai.score_attachments(moved,c,i,plan)['weighted_total'],before)

    def test_input_axis_removed_and_duplicate_cell_axis_replaced_but_other_edges_kept(self):
        d,cells,_=circuit();f=build(d,cells);i=f[4];m=f[5]['folded_cores'];g=m['groups'][0]
        edges=[(i['ia'],i['fa'],3.),(i['ba'],i['fa'],1.25),(i['bb'],i['fb'],1.25),(i['fa'],i['unrelated'],2.)]
        kept,removed=folded.filter_input_alignment(edges,i,m,[g['id']]);self.assertEqual(len(removed),1)
        kept,replaced=folded.filter_RL_alignment(kept,i,m);self.assertEqual(len(replaced),2)
        self.assertEqual(kept,[(i['fa'],i['unrelated'],2.)])

    def test_supply_connection_or_symbol_tampering_makes_saved_measurement_unknown(self):
        d,cells,_=circuit();f=build(d,cells);pl,c,n,i,plan=f[1:6]
        self.assertTrue(folded.measure_folded(pl,c,i,plan['folded_cores'])['measurable'])
        for mode in ('net','symbol'):
            altered=deepcopy(c)
            if mode=='net':altered[i['rail_a']]['nodes']['1']='not_the_rail'
            else:altered[i['rail_a']]['symbol']='capacitor'
            measured=folded.measure_folded(pl,altered,i,plan['folded_cores'])
            self.assertFalse(measured['measurable']);self.assertIsNone(measured['main_pin_error'])

    def test_no_pair_or_no_RL_does_not_add_a_fallback_record(self):
        d,cells,_=circuit();doc=to_document(d)
        empty=detect_folded_RL(d,doc['components'],dict(structures=[],net_roles={}))
        self.assertEqual(empty['records'],[])
        d['ba']['s']=d['bb']['s']='vdd';del d['rail_a'];del d['rail_b']
        ev=build_layout_evidence(d,analyze_structures(d));det=detect_folded_RL(d,to_document(d)['components'],ev)
        self.assertEqual(det['records'],[])

    def test_static_seed_uses_disjoint_leaves_and_does_not_move_unrelated_devices(self):
        d,cells,names=circuit();f=build(d,cells,names);before=state(f[1])
        result,report,budget=seed(f)
        self.assertTrue(report['folded_initialization']['applied_ids'])
        self.assertTrue(ai._static(par,result,f[3],f[4],RoutingOptions(),perf_counter()+1))
        self.assertEqual(state(f[1]),before)
        for name in ('it','unrelated','rail_a','rail_b','bridge'):
            self.assertEqual(result.cell_of[f[4][name]],f[1].cell_of[f[4][name]])
        self.assertTrue(result.valid_state());self.assertEqual(result.row_of,f[1].row_of)

    def test_dual_seed_passes_actual_geometry_with_reflected_supply_terminals(self):
        d,cells,names=circuit(dual=True);f=build(d,cells,names,reflect_rails=True);pl,c,n,i,plan=f[1:6]
        for j,comp in enumerate(c):
            if comp['symbol']=='resistor' and 2 in pl.allowed_orientations(j):pl.orient[j]=2
        result,report,_=seed(f)
        self.assertTrue(report['folded_initialization']['applied_ids'])
        self.assertTrue(ai._static(par,result,n,i,RoutingOptions(),perf_counter()+1))
        self.assertEqual(result.row_of,pl.row_of)

    def test_mirrored_translated_seed_preserves_topology_and_actual_geometry(self):
        d,cells,names=circuit(rename=True);f=build(d,cells,names);pl,c,n,i,plan=f[1:6]
        poses=[(r,pl.cols-1-col) for r,col in pl.cell_of]
        orientations=[1-o if comp['symbol'] in ('nmos','pmos') else o for o,comp in zip(pl.orient,c)]
        pl.column_x=[x+37 for x in pl.column_x];pl.row_y=[y-23 for y in pl.row_y]
        pl.set_state(poses,orientations);pl=ai._install(pl,c,i,plan)
        f=(d,pl,c,n,i,plan,names)
        result,report,_=seed(f)
        self.assertTrue(report['folded_initialization']['applied_ids'])
        self.assertTrue(ai._static(par,result,n,i,RoutingOptions(),perf_counter()+1))
        self.assertEqual(result.row_of,pl.row_of)

    def test_full_initialization_protects_verified_pin_axes_against_later_host_moves(self):
        d,cells,names=circuit();f=build(d,cells,names);pl,c,n,i,plan=f[1:6]
        result,report=ai.initialize_attachments(pl,d,c,n,i,plan)
        self.assertTrue(report['applied']);self.assertTrue(result.valid_state())
        self.assertTrue(result.attachment_axis_limits)
        moved=deepcopy(result);positions=list(moved.cell_of)
        positions[i['ba']]=(positions[i['ba']][0],0);moved.set_state(positions,moved.orient)
        self.assertFalse(moved.valid_state())

    def test_fixed_core_and_timeout_keep_original_state_without_astar(self):
        for mode in ('fixed','timeout'):
            d,cells,names=circuit();f=build(d,cells,names);pl=f[1]
            if mode=='fixed':pl.fixed.add(f[4]['ia']);pl.fixed_cells[f[4]['ia']]=pl.cell_of[f[4]['ia']]
            before=state(pl)
            result,report,budget=seed(f,**({'expired':True} if mode=='timeout' else {}))
            self.assertIs(result,pl);self.assertEqual(state(pl),before)
            self.assertEqual(report['actions'],[])


if __name__=='__main__':unittest.main()
