"""Folded forks keep an input child and separate corresponding load leaves."""
from copy import deepcopy
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_passive_placement import convert,mos,resistor,source
from tests.test_attachment_flow import candidate
from netlist2schematic.plugins.attachment import attachment_folded as folded
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment.attachment_selection import select_attachment_result
from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.routing.routing_rules import RoutingOptions


def fixture(*,rename=False,dual=False,policy='all',feedback=False):
    def p(d,g,s='vdd'):
        return dict(cls='MOS',type='PMOS',model='P',d=d,g=g,s=s,bulk='vdd')
    devices=dict(ba=p('ja','bp'),bb=p('jb','bp'),ia=mos('ja','ga','tail'),ib=mos('jb','gb','tail'),
        fa=p('oa','bf','ja'),fb=p('ob','bf','jb'),it=source('ISRC','tail','0'),
        r=resistor('oa','ob'),unrelated=resistor('other','0'))
    cells=dict(ba=(1,2),bb=(1,3),ia=(3,1),fa=(3,2),fb=(3,3),ib=(3,4),
        it=(5,2),r=(2,2),unrelated=(0,6))
    if feedback:
        devices['fa']['d']=devices['fb']['d']='out'
        devices['r']=resistor('sense','out')
        devices['r2']=resistor('out','sense');cells['r2']=(2,3)
        devices['amp']=dict(cls='VCVS',type='E',op='control',on='0',ip='sense',
            **{'in':'reference'},value='10k');cells['amp']=(1,1)
    if dual:
        for d in devices.values():
            if d['cls']=='MOS':
                d['type']='NMOS' if d['type']=='PMOS' else 'PMOS'
                for k in ('s','bulk'):d[k]='0' if d[k]=='vdd' else 'vdd' if d[k]=='0' else d[k]
            elif d['cls']=='ISRC':d['p'],d['n']='vdd','tail'
        cells={n:(6-r,c) for n,(r,c) in cells.items()}
    names={n:n for n in devices}
    if rename:
        names={n:'unit'+str(70-k) for k,n in enumerate(devices)}
        devices={names[n]:{k:'signal_'+v if k in ('d','g','s','a','b','p','n') and v not in ('0','vdd') else v
            for k,v in d.items()} for n,d in reversed(list(devices.items()))}
        cells={names[n]:v for n,v in cells.items()}
    core=[names[n] for n in ('ba','bb','ia','ib','fa','fb')]
    structs=analyze_structures(devices)
    pl,c,n,i,_=convert(devices,cells,blocks=[core],records=structs)
    pl.role_orders=[[[i[names[n]] for n in ('ia','fa','fb','ib')]]]
    plan=ai.build_attachment_plan(devices,c,n,i,structs,compute_levels(devices),pl.row_of,policy=policy)
    return devices,ai._install(pl,c,i,plan),c,n,i,plan,names


def state(pl):
    return deepcopy((pl.cell_of,pl.orient,pl.row_of,pl.comp_at,pl.column_x,pl.row_y,
        pl.blocks,getattr(pl,'layout_macro_blocks',None),pl.role_orders,pl.rng.getstate()))


_UNSET=object()


def seed(f,*,static=_UNSET,expired=False,allowance=8):
    d,pl,c,n,i,plan,_=f;budget=ai.new_attachment_budget(max_candidates=allowance)
    report=dict(actions=[],candidate_trials=0)
    args=(structured.par,pl,pl,c,n,i,plan,RoutingOptions(),budget,
          perf_counter()+(-1 if expired else 1),report)
    with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar')), \
         patch.object(structured,'_anneal',side_effect=AssertionError('SA')):
        if static is _UNSET:result=ai._folded_seed(*args)
        else:
            with patch.object(ai,'_checked_static',return_value=static):result=ai._folded_seed(*args)
    return result,report,budget


class FoldedAttachmentTests(unittest.TestCase):
    def test_validated_parent_references_three_disjoint_leaves_with_actual_fork_ports(self):
        d,pl,c,n,i,plan,names=fixture();model=plan['folded_cores']
        self.assertEqual(len(model['groups']),1)
        g=model['groups'][0]
        self.assertEqual([len(leaf) for leaf in g['leaves']],[2,2,2])
        owned=[name for leaf in g['leaves'] for name in leaf]
        self.assertEqual(len(owned),len(set(owned)))
        self.assertNotIn('it',owned);self.assertNotIn('r',owned)
        for a,f,b in zip(g['inputs'],g['folded'],g['bias']):
            self.assertEqual(d[a]['d'],d[f]['s']);self.assertEqual(d[a]['d'],d[b]['d'])

    def test_clamped_common_source_cannot_reenter_through_a_folded_parent(self):
        d,pl,c,n,i,plan,_=fixture();d=deepcopy(d)
        d['clamp']=source('VSRC','tail','0')
        doc=structured.to_document(d);rows={c['id']:3 for c in doc['components']}
        model=folded.compile_folded(d,doc['components'],analyze_structures(d),rows,[])
        self.assertEqual(model['groups'],[])

    def test_unknown_bias_supply_and_wrong_branch_ports_do_not_create_a_core(self):
        for mutation in ('unknown_supply','wrong_source'):
            d,pl,c,n,i,plan,_=fixture();d=deepcopy(d)
            if mutation=='unknown_supply':d['ba']['s']=d['bb']['s']='unproven_rail'
            else:d['fa']['s']='another_junction'
            doc=structured.to_document(d);rows={c['id']:3 for c in doc['components']}
            with self.subTest(mutation=mutation):
                self.assertEqual(folded.compile_folded(d,doc['components'],analyze_structures(d),rows,[])['groups'],[])

    def test_rows_must_match_within_each_leaf_and_are_never_reassigned(self):
        d,pl,c,n,i,plan,names=fixture()
        rows=dict(plan['display_rows']);rows['ia']+=1
        model=folded.compile_folded(d,c,analyze_structures(d),rows,[])
        self.assertEqual(model['groups'],[])
        self.assertTrue(any(r['reason']=='folded_leaf_rows_incompatible' for r in model['skipped']))

    def test_an_external_existing_owner_is_not_silently_cut_in_half(self):
        d,pl,c,n,i,plan,_=fixture();g=plan['folded_cores']['groups'][0]
        model=folded.compile_folded(d,c,analyze_structures(d),plan['display_rows'],[g['members']+['unrelated']])
        self.assertEqual(model['groups'],[])
        self.assertEqual(model['skipped'][0]['reason'],'existing_owner_extends_beyond_core')

    def test_equivalent_duplicate_detector_records_do_not_create_multiple_physical_owners(self):
        d,pl,c,n,i,plan,_=fixture();structs=analyze_structures(d)
        core=deepcopy(next(s for s in structs if s['kind']=='folded_core'))
        core['id']='another_detector_record'
        model=folded.compile_folded(d,c,structs+[core],plan['display_rows'],[])
        self.assertEqual(len(model['groups']),1)

    def test_overlapping_folded_cores_are_not_transitively_joined_or_arbitrarily_selected(self):
        d,pl,c,n,i,plan,_=fixture();d=deepcopy(d)
        d.update(ic=mos('ja','gc','second_tail'),id=mos('jb','gd','second_tail'),
            second_current=source('ISRC','second_tail','0'))
        doc=structured.to_document(d)
        rows={comp['id']:3 for comp in doc['components']}
        model=folded.compile_folded(d,doc['components'],analyze_structures(d),rows,[])
        self.assertEqual(model['groups'],[])
        self.assertTrue(model['skipped'])

    def test_owner_lists_preserve_noncore_blocks_and_do_not_absorb_sources(self):
        _,_,_,_,_,plan,_=fixture();model=plan['folded_cores'];g=model['groups'][0]
        before=[g['members'],['it'],['r','unrelated']]
        result=folded.owner_lists(before,model)
        self.assertIn(['it'],result);self.assertIn(['r','unrelated'],result)
        self.assertEqual(len(result),5)

    def test_a_supplied_valid_folded_contract_keeps_mirror_reference_and_output_nets(self):
        d,pl,c,n,i,plan,names=fixture()
        contract=deepcopy(next(s for s in analyze_structures(d) if s['kind']=='folded_core'))
        d=deepcopy(d)
        d['ba']['g']=d['bb']['g']='ja'
        structs=analyze_structures(d)
        mirrors=[s for s in structs if s['kind']=='current_mirror']
        self.assertTrue(mirrors)
        # The legacy detector does not emit folded_core for a mirror load.
        # Exercise the consumer with an explicit six-port contract, which it
        # still has to recheck against the actual changed terminals below.
        structs.append(contract)
        cells={comp['id']:pl.cell_of[j] for j,comp in enumerate(c)}
        core=plan['folded_cores']['groups'][0]['members']
        pl,c,n,i,_=convert(d,cells,blocks=[core],records=structs)
        plan=ai.build_attachment_plan(d,c,n,i,structs,compute_levels(d),pl.row_of,policy='all')
        pl=ai._install(pl,c,i,plan);before=deepcopy(c)
        result,report,budget=seed((d,pl,c,n,i,plan,names))
        self.assertTrue(report['folded_initialization']['applied_ids'])
        self.assertEqual(c,before)
        for mirror in mirrors:
            ref=mirror['roles']['reference']
            self.assertEqual(c[i[ref]]['nodes']['D'],c[i[ref]]['nodes']['G'])

    def test_no_folded_evidence_is_not_counted_as_successful_zero_error_coverage(self):
        d=dict(a=mos('da','ga'),b=mos('db','gb'))
        pl,c,n,i,_=convert(d,dict(a=(1,1),b=(1,2)))
        model=folded.compile_folded(d,c,analyze_structures(d),{'a':1,'b':1},[])
        measured=folded.measure_folded(pl,c,i,model)
        self.assertEqual(measured['count'],0);self.assertFalse(measured['measurable'])
        self.assertIsNone(measured['input_interleaving'])

    def test_seed_keeps_input_members_together_and_all_unrelated_positions_unchanged(self):
        f=fixture();d,pl,c,n,i,plan,names=f;before=state(pl)
        result,report,budget=seed(f);detail=report['folded_initialization']
        self.assertEqual(len(detail['applied_ids']),1)
        self.assertEqual(detail['after']['input_interleaving'],0)
        self.assertEqual(detail['after']['input_access_errors'],0)
        self.assertEqual(detail['after']['branch_order_errors'],0)
        self.assertEqual((result.row_of,result.column_x,result.row_y),(pl.row_of,pl.column_x,pl.row_y))
        for name in ('it','r','unrelated'):
            self.assertEqual((result.cell_of[i[name]],result.orient[i[name]]),(pl.cell_of[i[name]],pl.orient[i[name]]))
        self.assertEqual(state(pl),before);self.assertEqual(result.rng.getstate(),pl.rng.getstate())
        self.assertTrue(result.valid_state());self.assertLessEqual(budget['candidates'],8)

    def test_old_outboard_order_is_replaced_without_deleting_independent_orders_or_motion(self):
        f=fixture();d,pl,c,n,i,plan,_=f
        kept=[[i['r'],i['unrelated']]];pl.role_orders.append(deepcopy(kept))
        pl.layout_macro_blocks=[[i['r'],i['unrelated']]];pl.motion_blocks=deepcopy(pl.layout_macro_blocks)
        result,report,budget=seed(f)
        self.assertTrue(report['folded_initialization']['applied_ids'])
        self.assertIn(kept,result.role_orders)
        self.assertNotIn(pl.role_orders[0],result.role_orders)
        self.assertIn(pl.layout_macro_blocks[0],result.layout_macro_blocks)
        self.assertIn(pl.motion_blocks[0],result.motion_blocks)

    def test_only_applied_input_fork_alignment_is_removed_and_main_stack_is_kept(self):
        _,_,_,_,i,plan,_=fixture();g=plan['folded_cores']['groups'][0]
        ia,fa,ba=(i[g[k][0]] for k in ('inputs','folded','bias'))
        edges=[(ia,fa,3),(ia,ba,1.25),(fa,ba,3),(i['r'],i['unrelated'],4.5)]
        self.assertEqual(folded.filter_input_alignment(edges,i,plan['folded_cores'],[]),(edges,[]))
        kept,removed=folded.filter_input_alignment(edges,i,plan['folded_cores'],[g['id']])
        self.assertEqual(kept,edges[2:]);self.assertEqual(removed,edges[:2])

    def test_fixed_core_and_expired_or_failed_transactions_preserve_every_state(self):
        for mode in ('fixed','deadline','budget','static_failure','static_timeout'):
            f=fixture();d,pl,c,n,i,plan,_=f;kwargs={}
            if mode=='fixed':pl.fixed.add(i['ia'])
            elif mode=='deadline':kwargs['expired']=True
            elif mode=='budget':kwargs['allowance']=0
            elif mode=='static_failure':kwargs['static']=False
            else:kwargs['static']=None
            before=state(pl);result,report,budget=seed(f,**kwargs)
            with self.subTest(mode=mode):
                self.assertEqual(state(result),before);self.assertEqual(state(pl),before)
                self.assertFalse(report['folded_initialization']['applied_ids'])
                self.assertLessEqual(budget['candidates'],budget['max_candidates'])

    def test_polarity_dual_rename_and_netlist_order_preserve_topological_membership(self):
        for kwargs in ({'dual':True},{'rename':True},{'dual':True,'rename':True}):
            f=fixture(**kwargs);d,pl,c,n,i,plan,names=f
            with self.subTest(**kwargs):
                self.assertEqual(len(plan['folded_cores']['groups']),1)
                g=plan['folded_cores']['groups'][0]
                self.assertEqual(set(g['inputs']),{names['ia'],names['ib']})
                self.assertEqual(set(g['folded']),{names['fa'],names['fb']})
                result,report,budget=seed(f)
                self.assertTrue(report['folded_initialization']['applied_ids'])
                self.assertEqual(report['folded_initialization']['after']['input_interleaving'],0)

    def test_measurement_is_invariant_to_translation_and_horizontal_mirror(self):
        d,pl,c,n,i,plan,_=fixture()
        class View:
            cell_of=pl.cell_of
            def pos_of(self,j):
                x,y=pl.pos_of(j);return 4000-x,y+33
            def pin_world(self,j,pin):
                x,y=pl.pin_world(j,pin);return 4000-x,y+33
            def pin_dir(self,j,pin):
                return {'east':'west','west':'east'}.get(pl.pin_dir(j,pin),pl.pin_dir(j,pin))
        before=folded.measure_folded(pl,c,i,plan['folded_cores'])
        after=folded.measure_folded(View(),c,i,plan['folded_cores'])
        self.assertEqual(folded.folded_rank(before),folded.folded_rank(after))

    def test_changed_ports_missing_members_and_nonfinite_geometry_remain_unknown(self):
        d,pl,c,n,i,plan,_=fixture();changed=deepcopy(c);changed[i['ia']]['nodes']['D']='changed'
        self.assertIsNone(folded.folded_rank(folded.measure_folded(pl,changed,i,plan['folded_cores'])))
        bad_index=dict(i);bad_index.pop('ia')
        self.assertIsNone(folded.folded_rank(folded.measure_folded(pl,c,bad_index,plan['folded_cores'])))
        class Bad:
            def pos_of(self,j):return float('nan'),0
        self.assertIsNone(folded.folded_rank(folded.measure_folded(Bad(),c,i,plan['folded_cores'])))

    def test_existing_sa_moves_and_rejections_keep_input_leaf_and_fixed_rows(self):
        f=fixture();d,pl,c,n,i,plan,_=f;result,report,budget=seed(f)
        for k in range(150):
            before=deepcopy((result.cell_of,result.orient,result.row_of,result.comp_at))
            old=result.propose()
            if old is not None:
                self.assertTrue(result.valid_state())
                if k%2:
                    result.set_state(*old)
                    self.assertEqual((result.cell_of,result.orient,result.row_of,result.comp_at),before)
            m=folded.measure_folded(result,c,i,plan['folded_cores'])
            self.assertEqual(m['input_interleaving'],0);self.assertEqual(m['row_errors'],0)

    def test_host_motion_protects_an_achieved_passive_embedding_without_guest_motion(self):
        f=fixture(feedback=True);d,pl,c,n,i,plan,_=f
        before={name:(pl.cell_of[i[name]],pl.orient[i[name]]) for name in ('r','r2')}
        budget=ai.new_attachment_budget(max_candidates=8)
        with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar')):
            result,report=ai.initialize_attachments(pl,d,c,n,i,plan,budget=budget,options=RoutingOptions())
        self.assertTrue(report['folded_initialization']['applied_ids'])
        self.assertEqual({name:(result.cell_of[i[name]],result.orient[i[name]]) for name in before},before)
        embedded=report['host_created_embeddings_protected']
        self.assertEqual(set(next(iter(embedded.values()))),{'r','r2'})
        cells=list(result.cell_of);cells[i['r']]=(0,0)
        result.set_state(cells,result.orient)
        self.assertFalse(result.valid_state())

    def test_folded_organization_does_not_override_real_routing_caps_or_missing_evidence(self):
        old,new=candidate('1'),candidate('2',score=12,length=1100)
        for row,interleaving in ((old,2),(new,0)):
            row[5]['layout_attachments']['folded_organization']=dict(count=1,measurable=True,
                row_errors=0,input_interleaving=interleaving,branch_order_errors=0,
                input_access_errors=0,input_span=1,main_pin_error=0,region_span=3)
        self.assertIs(select_attachment_result([old,new])[0],new)
        self.assertEqual(new[7]['score'],12)
        new[6][1]['crossings']=1
        self.assertIs(select_attachment_result([old,new])[0],old)
        new[6][1]['crossings']=0
        del new[5]['layout_attachments']['folded_organization']
        self.assertIs(select_attachment_result([old,new])[0],old)


if __name__=='__main__':
    unittest.main()
