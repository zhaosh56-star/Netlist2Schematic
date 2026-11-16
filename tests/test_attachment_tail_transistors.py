"""Independent tail axes use source contracts, legal states and one old ledger."""
from copy import deepcopy
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_passive_placement import mos,resistor,convert
from tests.test_attachment_flow import candidate
from netlist2schematic.plugins.attachment import attachment_tail_transistors as tails
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment.attachment_selection import select_attachment_result,measure_candidate
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl,_bounds
from netlist2schematic.layout.structured_placement import par
from netlist2schematic.routing.routing_rules import RoutingOptions


def devices_for(*,bjt=False,dual=False):
    devices=dict(a=mos('oa','ga','ta'),b=mos('ob','gb','ta'),
        c=mos('oc','gc','tb'),d=mos('od','gd','tb'),u=mos('ta','bias'),v=mos('tb','bias'),
        ra=resistor('vdd','oa'),rb=resistor('vdd','ob'),rc=resistor('vdd','oc'),rd=resistor('vdd','od'),
        unrelated=resistor('separate','0'))
    for n,row in devices.items():
        if row['cls']=='MOS' and bjt:
            devices[n]=dict(cls='BJT',type='NPN',c=row['d'],b=row['g'],e=row['s'],model='NPN')
    if dual:
        for row in devices.values():
            if row['cls']=='MOS':row['type']='PMOS';row['s']='vdd' if row['s']=='0' else row['s'];row['bulk']='vdd'
            elif row['cls']=='BJT':row['type']='PNP';row['e']='vdd' if row['e']=='0' else row['e']
            elif row['cls']=='PASSIVE':
                for p in ('a','b'):row[p]='0' if row[p]=='vdd' else 'vdd' if row[p]=='0' else row[p]
    return devices


def compile_model(devices,rows=None):
    c=to_document(devices)['components'];rows=rows or {v['id']:2 if v['id'] in ('u','v') else 1 for v in c}
    return tails.compile_tails(devices,c,analyze_structures(devices),rows)


def fixture(*,bjt=False,dual=False,rename=False,policy='all'):
    devices=devices_for(bjt=bjt,dual=dual)
    cells=dict(a=(1,1),b=(1,2),c=(1,4),d=(1,5),u=(2,3),v=(2,2),
        ra=(0,1),rb=(0,2),rc=(0,4),rd=(0,5),unrelated=(0,7))
    if dual:cells={n:(3-r,c) for n,(r,c) in cells.items()}
    if rename:
        names={n:'unit'+str(90-k) for k,n in enumerate(devices)}
        devices={names[n]:{k:'signal_'+v if k in ('d','g','s','c','b','e','a') and v not in ('0','vdd') else v
            for k,v in row.items()} for n,row in reversed(list(devices.items()))}
        cells={names[n]:cell for n,cell in cells.items()}
    pl,c,n,i,_=convert(devices,cells)
    plan=ai.build_attachment_plan(devices,c,n,i,analyze_structures(devices),compute_levels(devices),pl.row_of,policy=policy)
    pl=ai._install(pl,c,i,plan)
    if dual:
        # Mirror related loads; the unrelated fixture only permits 0/1,
        # so use its legal horizontal orientation for the reversed rail.
        for j,comp in enumerate(c):
            if comp['symbol']=='resistor':pl.orient[j]=2 if 2 in pl.allowed_orientations(j) else 1
    return pl,c,n,i,plan


_REAL=object()
def finish(f,*,safe=_REAL,metadata=None,used=None,deadline=None):
    pl,c,n,i,plan=f;budget=ai.new_attachment_budget();budget['spent_seconds']=.25
    tails.reserve_finish(plan,budget,pl,i)
    reserve=budget.get('tail_transistor_reserve',0)
    budget['candidates']=128-reserve if used is None else used
    report=dict(candidates=0,accepted=0,actions=[])
    args=(par,pl,c,n,i,plan,RoutingOptions(),budget,
        perf_counter()+1 if deadline is None else deadline,report,metadata or {})
    with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar forbidden')):
        if safe is _REAL:
            result=tails.finish_tails(*args,hpwl_limit=_hpwl(par,pl,n,i)*1.15)
        else:
            with patch.object(ai,'_checked_static',return_value=safe):
                result=tails.finish_tails(*args,hpwl_limit=_hpwl(par,pl,n,i)*1.15)
    return result,budget,report


class ExclusiveTailTests(unittest.TestCase):
    def test_calibrated_pairs_keep_two_exclusive_tails_and_no_common_bias_owner(self):
        pl,c,n,i,plan=fixture();model=plan['tail_transistors']
        self.assertEqual(len(model['groups']),2)
        self.assertEqual({g['tail'] for g in model['groups']},{'u','v'})
        self.assertEqual({g['net'] for g in model['groups']},{'ta','tb'})
        self.assertTrue(all('owned_members' not in g for g in model['groups']))

    def test_passive_policy_does_not_enable_active_tail_finish(self):
        f=fixture(policy='passive');self.assertEqual(f[-1]['tail_transistors']['groups'],[])
        result,budget,report=finish(f)
        self.assertIs(result,f[0]);self.assertEqual(report['candidates'],0)
        self.assertNotIn('tail_transistor_reserve',budget)

    def test_unknown_supply_diode_or_opposite_polarity_tail_is_not_claimed(self):
        for kind in ('unknown','diode','opposite'):
            d=devices_for()
            if kind=='unknown':d['u']['s']='remote'
            elif kind=='diode':d['u']['g']='ta'
            else:d['u']['type']='PMOS';d['u']['bulk']='vdd'
            self.assertNotIn('u',{g['tail'] for g in compile_model(d)['groups']})

    def test_third_shared_source_is_not_assigned_to_one_arbitrary_pair(self):
        d=devices_for();d['extra']=mos('third_out','third_gate','ta')
        self.assertNotIn('u',{g['tail'] for g in compile_model(d)['groups']})

    def test_real_mirror_reference_and_outputs_keep_their_foreign_owner(self):
        d=devices_for();d['reference']=mos('bias','bias')
        model=compile_model(d)
        self.assertFalse(model['groups'])
        self.assertTrue(any(s['reason']=='nonexclusive_or_reserved_tail' for s in model['skipped']))

    def test_latch_conduction_context_is_protected_but_shared_rails_are_not_context(self):
        for related in (False,True):
            d=devices_for()
            for name,drain,gate,source,p in (
                ('lp','la','lb','vdd',True),('rp','lb','la','vdd',True),
                ('ln','la','lb','oa' if related else '0',False),
                ('rn','lb','la','ob' if related else '0',False)):
                d[name]=dict(cls='MOS',type='PMOS' if p else 'NMOS',model='P' if p else 'N',
                    d=drain,g=gate,s=source,bulk='vdd' if p else '0')
            model=compile_model(d)
            self.assertEqual('u' in {g['tail'] for g in model['groups']},not related)

    def test_bjt_polarity_dual_and_renaming_preserve_exclusive_contracts(self):
        for bjt in (False,True):
            for dual in (False,True):
                for rename in (False,True):
                    f=fixture(bjt=bjt,dual=dual,rename=rename)
                    measured=tails.measure_tails(f[0],f[1],f[3],f[-1]['tail_transistors'])
                    self.assertEqual(measured['count'],2);self.assertTrue(measured['measurable'])
                    self.assertEqual(measured['row_errors'],0);self.assertEqual(measured['port_direction_errors'],0)

    def test_bad_pair_rows_and_wrong_tail_order_are_not_replanned(self):
        d=devices_for();rows={n:2 if n in ('u','v') else 1 for n in d}
        rows['a']=0;self.assertNotIn('u',{g['tail'] for g in compile_model(d,rows)['groups']})
        rows['a']=1;rows['u']=0
        self.assertNotIn('u',{g['tail'] for g in compile_model(d,rows)['groups']})

    def test_fixed_runtime_owner_and_clock_access_skip_only_the_protected_tail(self):
        for mode in ('fixed','owner','clock'):
            f=fixture();pl,c,n,i,plan=f;meta={}
            if mode=='fixed':pl.fixed.add(i['u'])
            elif mode=='owner':pl.layout_macro_blocks=[[i['u'],i['unrelated']]]
            else:meta=dict(role_adjustments=[dict(kind='latch_clock_access',device='u')])
            groups,skipped=tails.eligible_groups(pl,i,plan['tail_transistors'],meta)
            self.assertEqual([g['tail'] for g in groups],['v']);self.assertTrue(skipped)
            result,budget,report=finish(f,metadata=meta,safe=True)
            self.assertEqual((result.cell_of[i['u']],result.orient[i['u']],result.pos_of(i['u'])),
                (pl.cell_of[i['u']],pl.orient[i['u']],pl.pos_of(i['u'])))

    def test_static_valid_finish_improves_actual_ports_without_moving_other_devices(self):
        f=fixture();pl,c,n,i,plan=f;before=tails.measure_tails(pl,c,i,plan['tail_transistors'])
        original=deepcopy((pl.cell_of,pl.orient,pl.column_x,pl.row_y,pl.rng.getstate()))
        result,budget,report=finish(f)
        after=tails.measure_tails(result,c,i,plan['tail_transistors'])
        self.assertTrue(result.valid_state());self.assertLess(tails.tail_rank(after),tails.tail_rank(before))
        self.assertEqual(after['axis_error'],0)
        for name in set(i)-{'u','v'}:
            self.assertEqual((result.pos_of(i[name]),result.orient[i[name]]),(pl.pos_of(i[name]),pl.orient[i[name]]))
        self.assertEqual([r for r,col in result.cell_of],[r for r,col in pl.cell_of])
        self.assertEqual((result.column_x,result.row_y,result.rng.getstate()),(pl.column_x,pl.row_y,pl.rng.getstate()))
        self.assertEqual((pl.cell_of,pl.orient,pl.column_x,pl.row_y,pl.rng.getstate()),original)
        for name in ('u','v'):
            lo,hi=_bounds(result,i[name]);self.assertLessEqual(lo,result.offset_x[i[name]]);self.assertLessEqual(result.offset_x[i[name]],hi)

    def test_tail_exchange_is_atomic_and_never_uses_foreign_occupants(self):
        f=fixture();pl,c,n,i,plan=f;cells=list(pl.cell_of)
        cells[i['u']]=(2,4);cells[i['v']]=(2,1);pl.set_state(cells,pl.orient)
        groups,_=tails.eligible_groups(pl,i,plan['tail_transistors'])
        proposals=list(tails.tail_trials(pl,c,i,groups[0],groups))
        swaps=[(trial,a) for trial,a in proposals if a['atomic_swap']]
        self.assertTrue(swaps)
        for trial,a in swaps:
            self.assertEqual(set(a['tails']),{'u','v'});self.assertEqual(len(trial.comp_at),len(c))
            self.assertEqual(trial.cell_of[i['v']],pl.cell_of[i['u']])
        pl.fixed.add(i['v']);groups,_=tails.eligible_groups(pl,i,plan['tail_transistors'])
        for trial,a in tails.tail_trials(pl,c,i,groups[0],groups):
            self.assertEqual(trial.cell_of[i['v']],pl.cell_of[i['v']]);self.assertNotIn('v',a['tails'])

    def test_static_rejection_and_timeout_rollback_all_tail_state(self):
        for safe in (False,None):
            f=fixture();pl=f[0];before=deepcopy((pl.cell_of,pl.orient,getattr(pl,'offset_x',None)))
            result,budget,report=finish(f,safe=safe)
            self.assertIs(result,pl);self.assertEqual(report['accepted'],0)
            self.assertEqual((pl.cell_of,pl.orient,getattr(pl,'offset_x',None)),before)
            if safe is None:self.assertTrue(budget['exhausted'])

    def test_shared_trial_count_and_compaction_reserve_are_not_reset(self):
        f=fixture();pl,c,n,i,plan=f;budget=ai.new_attachment_budget()
        budget.update(band_compaction_reserve=2);tails.reserve_finish(plan,budget,pl,i)
        self.assertEqual(budget['tail_transistor_reserve'],8)
        budget['candidates']=118;self.assertFalse(ai._has_budget(budget,perf_counter()+1))
        report=dict(candidates=0,accepted=0,actions=[])
        with patch.object(ai,'_checked_static',return_value=True):
            result=tails.finish_tails(par,pl,c,n,i,plan,RoutingOptions(),budget,perf_counter()+1,report,{},
                hpwl_limit=_hpwl(par,pl,n,i)*1.15)
        self.assertLessEqual(budget['candidates'],126);self.assertEqual(budget['candidates'],118+report['candidates'])
        self.assertEqual(budget['band_compaction_reserve'],2)
        tails.reserve_finish(plan,budget,pl,i);self.assertNotIn('tail_transistor_reserve',budget)

    def test_zero_budget_or_expired_deadline_never_starts_a_proposal(self):
        for used,deadline in ((128,None),(120,0)):
            f=fixture();result,budget,report=finish(f,used=used,deadline=deadline,safe=True)
            self.assertIs(result,f[0]);self.assertEqual(report['candidates'],0)

    def test_missing_or_changed_geometry_is_unknown_not_perfect(self):
        f=fixture();pl,c,n,i,plan=f;model=plan['tail_transistors']
        changed=deepcopy(c);changed[i['u']]['nodes']['D']='different'
        self.assertIsNone(tails.tail_rank(tails.measure_tails(pl,changed,i,model)))
        with patch.object(pl,'pin_world',return_value=(float('nan'),0)):
            self.assertIsNone(tails.tail_rank(tails.measure_tails(pl,c,i,model)))
        self.assertFalse(tails.measure_tails(pl,c,i,dict(groups=[]))['measurable'])

    def test_actual_metrics_are_invariant_under_world_translation_and_horizontal_mirror(self):
        pl,c,n,i,plan=fixture();model=plan['tail_transistors'];original=tails.measure_tails(pl,c,i,model)
        class View:
            cell_of=pl.cell_of
            def pin_world(self,j,p):
                x,y=pl.pin_world(j,p);return 73-x,y-29
            def pin_dir(self,j,p):
                return dict(east='west',west='east',north='north',south='south')[pl.pin_dir(j,p)]
        mirrored=tails.measure_tails(View(),c,i,model)
        self.assertEqual(tails.tail_rank(original),tails.tail_rank(mirrored))

    def test_real_static_finish_also_works_for_bjt_and_polarity_dual(self):
        for bjt in (False,True):
            for dual in (False,True):
                f=fixture(bjt=bjt,dual=dual)
                result,budget,report=finish(f)
                with self.subTest(bjt=bjt,dual=dual):
                    self.assertTrue(result.valid_state())
                    measured=tails.measure_tails(result,f[1],f[3],f[-1]['tail_transistors'])
                    self.assertEqual(measured['axis_error'],0)
                    self.assertGreater(report['accepted'],0)

    def test_transaction_records_inherit_the_last_accepted_tail_state(self):
        f=fixture();result,budget,report=finish(f,safe=True)
        actions=report['tail_transistor_finish']['actions']
        self.assertEqual(len(actions),2)
        self.assertEqual(actions[0]['after'],actions[1]['before'])
        self.assertLess(actions[1]['after']['axis_error'],actions[1]['before']['axis_error'])

    def test_reserved_tail_trials_can_resume_an_unseeded_post_sa_layout(self):
        devices=devices_for();installed,c,n,i,plan=fixture()
        cells={row['id']:installed.cell_of[j] for j,row in enumerate(c)}
        pl,c,n,i,_=convert(devices,cells)
        budget=ai.new_attachment_budget();budget.update(candidates=120,spent_seconds=.4)
        tails.reserve_finish(plan,budget,pl,i)
        self.assertFalse(ai._has_budget(budget,perf_counter()+1))
        metadata=dict(candidate_id='2',layout_attachments=dict(policy='all',applied=False,
            plan=plan,budget=budget))
        result,meta=ai.refine_attachments(par,pl,c,n,i,metadata,options=RoutingOptions(),deadline=perf_counter()+1)
        report=meta['layout_attachments']['refinement']
        self.assertTrue(report['deferred_seed']['installed'])
        self.assertEqual(report['tail_transistor_finish']['after']['axis_error'],0)
        self.assertLessEqual(meta['layout_attachments']['budget']['candidates'],128)
        self.assertTrue(result.valid_state())

    def test_full_atomic_exchange_corrects_two_misassigned_tail_slots(self):
        f=fixture();pl,c,n,i,plan=f;cells=list(pl.cell_of)
        cells[i['u']]=(2,4);cells[i['v']]=(2,1);pl.set_state(cells,pl.orient)
        result,budget,report=finish(f)
        self.assertEqual(tails.measure_tails(result,c,i,plan['tail_transistors'])['axis_error'],0)
        self.assertTrue(any(a['atomic_swap'] for a in report['tail_transistor_finish']['actions']))
        self.assertTrue(result.valid_state())

    def test_nonfinite_control_pin_is_unknown_even_when_D_and_S_are_finite(self):
        pl,c,n,i,plan=fixture();original=pl.pin_world
        def broken(j,p):
            return (float('nan'),0) if j==i['u'] and p=='G' else original(j,p)
        with patch.object(pl,'pin_world',side_effect=broken):
            self.assertIsNone(tails.tail_rank(tails.measure_tails(pl,c,i,plan['tail_transistors'])))

    def test_old_quality_and_missing_common_evidence_guards_still_apply(self):
        def row(id,error,**kw):
            r=candidate(id,**kw);r[5]['layout_attachments']['tail_transistor_organization']=dict(count=2,
                measurable=True,row_errors=0,port_direction_errors=0,axis_error=error,control_backtracking=0)
            return r
        old=row('1',2);new=row('2',0,score=11,length=1100)
        chosen,report=select_attachment_result([old,new]);self.assertIs(chosen,new)
        costly=row('2',0,score=11,length=1200)
        self.assertIs(select_attachment_result([old,costly])[0],old)
        crossed=row('2',0,score=11,crossings=1)
        self.assertIs(select_attachment_result([old,crossed])[0],old)
        missing=row('2',0,score=11);missing[5]['layout_attachments']['tail_transistor_organization']['count']=1
        self.assertIs(select_attachment_result([old,missing])[0],old)

    def test_saved_measurement_and_finish_order_do_not_reorganize_saved_positions(self):
        import inspect
        pl,c,n,i,plan=fixture();before=deepcopy((pl.cell_of,pl.orient))
        metadata=measure_candidate(pl,c,i,dict(candidate_id='2'),plan,policy='all')
        self.assertTrue(metadata['layout_attachments']['tail_transistor_organization']['measurable'])
        self.assertEqual((pl.cell_of,pl.orient),before)
        source=inspect.getsource(ai._refine_attachments)
        self.assertLess(source.index('current=finish_controllers'),source.index('current=finish_tails'))
        self.assertLess(source.index('current=finish_tails'),source.index('current=finish_compaction'))


if __name__=='__main__':unittest.main()
