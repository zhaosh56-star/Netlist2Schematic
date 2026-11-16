"""Terminal-derived parents align stacks without swallowing shared tails/bias."""
from copy import deepcopy
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_layout_evidence import bank,mos
from tests.test_passive_placement import convert
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment import attachment_regulation as reg
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.layout.structured_placement import par
from netlist2schematic.routing.routing_rules import RoutingOptions


def circuit(*,dual=False,bjt=False,renamed=False):
    devices=bank()
    devices.update(qa=mos('out_left','load_bias','vdd','PMOS'),
                   qb=mos('out_right','load_bias','vdd','PMOS'))
    cells=dict(a=(4,1),b=(4,2),ua=(3,4),ub=(3,2),qa=(1,1),qb=(1,2),
        ra=(2,4),rb=(2,5),pa=(1,4),pb=(1,5),t=(5,2))
    if dual:
        for d in devices.values():
            d['type']='PMOS' if d['type']=='NMOS' else 'NMOS'
            for k in ('d','g','s','bulk'):
                if d.get(k) in ('0','vdd'):d[k]={'0':'vdd','vdd':'0'}[d[k]]
        cells={n:(6-r,c) for n,(r,c) in cells.items()}
    if bjt:
        devices={n:dict(cls='BJT',type='NPN' if d['type']=='NMOS' else 'PNP',
                       c=d['d'],b=d['g'],e=d['s']) for n,d in devices.items()}
    formals=('ip','im');mapping={n:n for n in devices}
    if renamed:
        mapping={n:'device'+str(88-i) for i,n in enumerate(devices)}
        devices={mapping[n]:{k:'node_'+v if k in ('d','g','s','c','b','e')
            and v not in ('0','vdd') else v for k,v in d.items()}
            for n,d in reversed(list(devices.items()))}
        cells={mapping[n]:v for n,v in cells.items()};formals=('node_ip','node_im')
    return devices,cells,formals,mapping


def build(devices,cells,formals=('ip','im')):
    structs=analyze_structures(devices)
    pl,comps,nets,index,_=convert(devices,cells,records=structs)
    plan=ai.build_attachment_plan(devices,comps,nets,index,structs,compute_levels(devices),
        pl.row_of,policy='all',formal_ports=formals)
    return devices,ai._install(pl,comps,index,plan),comps,nets,index,plan


def fixture(**kwargs):
    d,c,f,_=circuit(**kwargs)
    return build(d,c,f)


def finish(case,*,safe='real',budget=None,until=None):
    ds,pl,cs,nets,index,plan=case
    budget=budget if budget is not None else ai.new_attachment_budget()
    report=dict(candidates=0,accepted=0,actions=[])
    args=(par,pl,cs,nets,index,plan,RoutingOptions(),budget,
          until if until is not None else perf_counter()+1,report)
    if safe=='real':result=reg.finish_regions(*args,hpwl_limit=float('inf'))
    else:
        with patch.object(ai,'_checked_static',return_value=safe):
            result=reg.finish_regions(*args,hpwl_limit=float('inf'))
    return result,report,budget


class PairedRegulationTests(unittest.TestCase):
    def test_two_unique_loops_and_stack_ports_define_parent_without_tail_union(self):
        f=fixture();g,=f[5]['regulation_regions']['groups']
        self.assertEqual(g['main_paths'],[['a','ua','qa'],['b','ub','qb']])
        self.assertEqual(g['regulators'],['ra','rb'])
        self.assertEqual(g['loads'],['pa','pb'])
        self.assertNotIn('t',g['members'])
        self.assertTrue(g['parent_only']);self.assertTrue(g['row_compatible'])
        self.assertFalse(any(set(b)==set(f[4][n] for n in g['members']) for b in f[1].blocks))

    def test_polarity_dual_and_BJT_keep_same_conduction_roles(self):
        for dual,bjt in ((True,False),(False,True),(True,True)):
            with self.subTest(dual=dual,bjt=bjt):
                f=fixture(dual=dual,bjt=bjt);g,=f[5]['regulation_regions']['groups']
                self.assertEqual(g['main_paths'],[['a','ua','qa'],['b','ub','qb']])
                self.assertTrue(g['row_compatible'])

    def test_rename_and_reverse_source_order_preserve_roles(self):
        d,c,ports,names=circuit(renamed=True);f=build(d,c,ports)
        g,=f[5]['regulation_regions']['groups'];inverse={v:k for k,v in names.items()}
        self.assertEqual({tuple(inverse[n] for n in p) for p in g['main_paths']},
                         {('a','ua','qa'),('b','ub','qb')})

    def test_missing_formal_disambiguation_does_not_guess_shared_source_pair(self):
        d,c,_,_=circuit();f=build(d,c,())
        self.assertEqual(f[5]['regulation_regions']['groups'],[])

    def test_common_rail_or_clock_cannot_create_region(self):
        for mode in ('ground','clock'):
            d,c,ports,_=circuit()
            if mode=='ground':
                for n in ('a','b','ra','rb'):d[n]['s']='0'
            else:d['ra']['g']=d['rb']['g']='clock'
            f=build(d,c,ports)
            self.assertEqual(f[5]['regulation_regions']['groups'],[],mode)

    def test_fork_in_main_conduction_stops_extension(self):
        d,c,ports,_=circuit();d['extra']=mos('extraout','eg','left');c['extra']=(0,0)
        f=build(d,c,ports)
        self.assertEqual(f[5]['regulation_regions']['groups'],[])

    def test_nonactive_main_tap_is_not_silently_made_same_axis(self):
        d,c,ports,_=circuit();d['tap']=dict(cls='PASSIVE',type='C',a='out_left',b='0');c['tap']=(0,0)
        f=build(d,c,ports)
        self.assertEqual(f[5]['regulation_regions']['groups'],[])

    def test_two_possible_regulators_are_ambiguous(self):
        d,c,ports,_=circuit();d['extra']=deepcopy(d['ra']);c['extra']=(0,0)
        f=build(d,c,ports)
        self.assertEqual(f[5]['regulation_regions']['groups'],[])

    def test_crossed_control_return_does_not_masquerade_as_local_regulation(self):
        d,c,ports,_=circuit();d['ua']['g'],d['ub']['g']=d['ub']['g'],d['ua']['g']
        self.assertEqual(build(d,c,ports)[5]['regulation_regions']['groups'],[])

    def test_loads_require_matching_bias_and_true_supply(self):
        for mode in ('bias','supply'):
            d,c,ports,_=circuit()
            d['pa']['g' if mode=='bias' else 's']='unrelated'
            self.assertEqual(build(d,c,ports)[5]['regulation_regions']['groups'],[],mode)

    def test_incompatible_rows_are_reported_not_counted_as_zero_success(self):
        d,c,ports,_=circuit();c['ua']=(4,4)
        f=build(d,c,ports);g,=f[5]['regulation_regions']['groups']
        self.assertFalse(g['row_compatible'])
        self.assertGreater(reg.measure_regions(f[1],f[2],f[4],f[5]['regulation_regions'])['row_errors'],0)
        self.assertEqual(list(reg.region_trials(f[1],f[2],f[4],f[5]['regulation_regions'])),[])

    def test_changed_external_terminal_invalidates_evidence_contract(self):
        f=fixture();f[2][f[4]['t']]['nodes']['G']='different'
        m=reg.measure_regions(f[1],f[2],f[4],f[5]['regulation_regions'])
        self.assertFalse(m['measurable']);self.assertIsNone(reg.region_rank(m))

    def test_translation_and_mirror_preserve_measurement(self):
        f=fixture();pl=f[1];model=f[5]['regulation_regions']
        m=reg.measure_regions(pl,f[2],f[4],model)
        moved=deepcopy(pl);moved.column_x=[x+400 for x in pl.column_x];moved.row_y=[y+222 for y in pl.row_y]
        self.assertEqual(reg.region_rank(m),reg.region_rank(reg.measure_regions(moved,f[2],f[4],model)))
        mirrored=deepcopy(pl);span=pl.column_x[0]+pl.column_x[-1]
        mirrored.column_x=[span-x for x in reversed(pl.column_x)]
        mirrored.set_state([(r,len(pl.column_x)-1-c) for r,c in pl.cell_of],[1-v for v in pl.orient])
        # Symbol mirrored origins include a common width translation; scalar
        # port errors/distances are invariant for equal-width active symbols.
        self.assertEqual(reg.region_rank(m),reg.region_rank(reg.measure_regions(mirrored,f[2],f[4],model)))

    def test_trials_preserve_rows_axes_tail_rng_and_leaf_ownership(self):
        f=fixture();pl=f[1];before=deepcopy(pl.__dict__)
        trials=list(reg.region_trials(pl,f[2],f[4],f[5]['regulation_regions'],max_trials=12))
        self.assertTrue(trials)
        for trial,_ in trials:
            self.assertEqual(trial.row_of,pl.row_of)
            self.assertEqual((trial.column_x,trial.row_y),(pl.column_x,pl.row_y))
            self.assertEqual(trial.blocks,pl.blocks)
            self.assertEqual(trial.pos_of(f[4]['t']),pl.pos_of(f[4]['t']))
            self.assertEqual(trial.comp_at,{cell:i for i,cell in enumerate(trial.cell_of)})
            self.assertEqual(trial.rng.getstate(),pl.rng.getstate())
        self.assertEqual(pl.cell_of,before['cell_of']);self.assertEqual(pl.orient,before['orient'])

    def test_fixed_core_or_external_owner_is_not_split(self):
        f=fixture();f[1].fixed.add(f[4]['a'])
        self.assertEqual(list(reg.region_trials(f[1],f[2],f[4],f[5]['regulation_regions'])),[])
        f=fixture();f[1].blocks.append([f[4]['a'],f[4]['t']])
        self.assertEqual(list(reg.region_trials(f[1],f[2],f[4],f[5]['regulation_regions'])),[])

    def test_real_safe_finish_improves_axes_without_Astar_or_geometry_relaxation(self):
        f=fixture();before=reg.measure_regions(f[1],f[2],f[4],f[5]['regulation_regions'])
        with patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('Astar trial')):
            result,report,budget=finish(f)
        detail=report['regulation_finish']
        self.assertTrue(detail['applied'])
        self.assertLess(detail['after']['main_pin_error'],before['main_pin_error'])
        self.assertTrue(ai._checked_static(par,result,f[3],f[4],RoutingOptions(),None))
        self.assertTrue(result.valid_state())
        self.assertLessEqual(budget['candidates'],16)

    def test_failed_geometry_trials_leave_original_untouched(self):
        f=fixture();before=deepcopy(f[1].cell_of)
        result,report,budget=finish(f,safe=False)
        self.assertIs(result,f[1]);self.assertEqual(result.cell_of,before)
        self.assertFalse(report['regulation_finish']['applied'])

    def test_one_remaining_trial_and_expired_deadline_never_reset_budget(self):
        f=fixture();budget=ai.new_attachment_budget();budget['candidates']=127
        result,report,budget=finish(f,safe=False,budget=budget)
        self.assertEqual(budget['candidates'],128)
        self.assertEqual(report['regulation_finish']['trials'],1)
        f=fixture();result,report,budget=finish(f,until=perf_counter()-1)
        self.assertIs(result,f[1]);self.assertEqual(budget['candidates'],0)
        self.assertEqual(report['regulation_finish']['stop_reason'],'shared_budget')

    def test_late_safe_trial_cannot_commit_after_deadline(self):
        f=fixture();clock=[100.]
        def late(*args):clock[0]=102.;return True
        with patch.object(reg,'perf_counter',side_effect=lambda:clock[0]), \
             patch.object(ai,'perf_counter',side_effect=lambda:clock[0]), \
             patch.object(ai,'_checked_static',side_effect=late):
            result,report,budget=finish(f,until=101.)
        self.assertIs(result,f[1]);self.assertFalse(report['regulation_finish']['applied'])


if __name__=='__main__':unittest.main()
