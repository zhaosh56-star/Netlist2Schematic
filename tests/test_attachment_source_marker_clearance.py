"""Early source access repair uses actual foreign markers and original budgets."""
from copy import deepcopy
from time import perf_counter
from unittest import TestCase
from unittest.mock import patch

from netlist2schematic.layout.structured_placement import par
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment import attachment_rail_clearance as rails
from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
from netlist2schematic.routing.routing_rules import build_scene, RoutingOptions
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from tests.test_attachment_sources import freeze
from tests.test_attachment_sources_e2e import plan_for
from tests.test_passive_placement import convert, mos, source, resistor


def fixture(family='ISRC', rail='0', *, first=False, reverse_order=False, prefix='', extra_reversed=False):
    ground=par.power_marker_sym(rail)=='ground'
    names={n:prefix+n for n in ('owner','a','b','src')}
    signal=prefix+'signal'
    ds=dict(owner=mos('o','g',rail),
        a=mos('a',signal if family=='VSRC' else 'ga','0' if family=='VSRC' else signal),
        b=mos('b','gb','0' if family=='VSRC' else signal),
        src=source(family,rail if first else signal,signal if first else rail))
    if not ground:
        for name in ('owner','a','b'):
            ds[name].update(type='PMOS',bulk=rail,model='PMOS')
            if family=='VSRC':ds[name]['s']=rail
    cells=dict(owner=(0 if ground else 2,1),a=(4,0),b=(4,3),src=(1,1))
    if extra_reversed:
        names['load']=prefix+'load'
        ds['load']=resistor('unused_signal','vdd');cells['load']=(3,6)
    ds={names[n]:d for n,d in (reversed(list(ds.items())) if reverse_order else ds.items())}
    cells={names[n]:c for n,c in cells.items()}
    orientation=(2 if first else 0) if ground else (0 if first else 2)
    pl,comps,nets,index,_=convert(ds,cells,{names['src']:orientation})
    plan=plan_for(ds,pl,comps,nets,index);pl=ai._install(pl,comps,index,plan)
    scene=build_scene(par,nets,pl,index)
    owners=[index[n] for net,ports in nets.items() if par.power_marker_sym(net) for n,p in ports]
    marker=next(rect for (_,rect),i in zip(scene.marker_rects,owners) if i==index[names['owner']])
    # The vertical source envelope and fixed exit hit this foreign marker.
    # A real horizontal source envelope has five units of halo clearance.
    pl.row_y[1]=marker[3]+32 if ground else marker[1]-32
    pl=ai._install(pl,comps,index,plan)
    return ds,pl,comps,nets,index,plan,names


def run(f, *, budget=None, until=None):
    ds,pl,comps,nets,index,*_=f
    budget=ai.new_attachment_budget() if budget is None else budget
    with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('No Astar')):
        result,detail=rails.normalize_initial_rails(par,pl,comps,nets,index,budget,
            perf_counter()+1 if until is None else until,options=RoutingOptions())
    return result,detail,budget


def faults(f,pl=None):
    return _static_faults(par,pl or f[1],f[3],f[4],RoutingOptions(),None)['violations']


class SourceMarkerClearanceTests(TestCase):
    def test_both_source_families_supply_aliases_and_pole_roles_clear_actual_foreign_marker(self):
        for family in ('ISRC','VSRC'):
            for rail in ('0','vss','vdd','vcc'):
                for first in (False,True):
                    with self.subTest(family=family,rail=rail,first=first):
                        f=fixture(family,rail,first=first);ds,pl,c,n,i,plan,names=f
                        self.assertIn(i[names['src']],pl.attachment_source_ids)
                        self.assertFalse(rails.inward_pins(par,pl,c))
                        self.assertTrue(any(x['kind']=='marker_body_halo' for x in faults(f)))
                        before=freeze(pl);nodes=deepcopy(c);netcopy=deepcopy(n)
                        result,r,b=run(f)
                        self.assertTrue(r['applied'],r)
                        self.assertTrue(result.valid_state());self.assertFalse(faults(f,result))
                        self.assertEqual(freeze(pl),before);self.assertEqual(c,nodes);self.assertEqual(n,netcopy)
                        self.assertEqual(result.cell_of,pl.cell_of);self.assertEqual(result.row_of,pl.row_of)
                        self.assertEqual(result.comp_at,pl.comp_at);self.assertEqual(result.column_x,pl.column_x)
                        self.assertEqual(result.row_y,pl.row_y);self.assertEqual(result.rng.getstate(),pl.rng.getstate())
                        self.assertEqual(result.pos_of(i[names['src']]),pl.pos_of(i[names['src']]))
                        self.assertTrue(all(a['kind']=='initial_source_marker_orientation' for a in r['actions']))
                        self.assertEqual(b['candidates'],r['trials']);self.assertLessEqual(r['trials'],8)
                        for j in range(len(c)):
                            if j!=i[names['src']]:self.assertEqual(result.orient[j],pl.orient[j])

    def test_same_supply_net_does_not_exempt_another_components_marker(self):
        f=fixture();result,r,_=run(f)
        rows=r['source_marker_conflicts_before']
        self.assertTrue(rows)
        self.assertTrue(all(x['net']=='0' and x['marker_component']!=x['component'] for x in rows))
        self.assertTrue(any(x['signal_exit_pins'] for x in rows))
        self.assertFalse(faults(f,result))

    def test_foreign_markers_are_fixed_and_own_marker_follows_its_real_rotated_pin(self):
        f=fixture();p,c,n,i=f[1:5];result,r,_=run(f)
        def markers(pl):
            scene=build_scene(par,n,pl,i)
            identities=[(name,pin) for net,ports in n.items() if par.power_marker_sym(net) for name,pin in ports]
            return dict(zip(identities,[rect for _,rect in scene.marker_rects]))
        old,new=markers(p),markers(result)
        src=f[6]['src']
        self.assertNotEqual(old[(src,'-')],new[(src,'-')])
        self.assertEqual({k:v for k,v in old.items() if k[0]!=src},
                         {k:v for k,v in new.items() if k[0]!=src})
        self.assertFalse(faults(f,result))

    def test_reversed_passive_and_source_marker_collision_share_one_atomic_search(self):
        f=fixture(extra_reversed=True);before=freeze(f[1])
        self.assertTrue(rails.inward_pins(par,f[1],f[2]))
        result,r,b=run(f)
        self.assertTrue(r['applied']);self.assertEqual(freeze(f[1]),before)
        self.assertEqual({a['kind'] for a in r['actions']},
                         {'initial_outward_rail_orientation','initial_source_marker_orientation'})
        self.assertFalse(faults(f,result));self.assertLessEqual(r['trials'],8)
        self.assertEqual(b['candidates'],r['trials'])

    def test_own_marker_entry_is_not_treated_as_a_foreign_obstacle(self):
        f=fixture();pl=f[1];pl.row_y[1]+=100
        f=list(f);pl=ai._install(pl,f[2],f[4],f[5]);f[1]=pl
        with patch.object(ai,'_checked_static',side_effect=AssertionError('No full static needed')):
            result,r,b=run(f)
        self.assertIs(result,pl);self.assertFalse(r['source_marker_conflicts_before'])
        self.assertEqual(b['candidates'],0)

    def test_fixed_numeric_named_and_fixed_cell_sources_stay_unmodified(self):
        for kind in ('index','name','cell'):
            f=fixture();pl=f[1];j=f[4][f[6]['src']]
            if kind=='cell':pl.fixed_cells[j]=pl.cell_of[j]
            else:pl.fixed.add(j if kind=='index' else f[6]['src'])
            before=freeze(pl);result,r,b=run(f)
            self.assertIs(result,pl);self.assertEqual(freeze(pl),before)
            self.assertFalse(r['applied']);self.assertEqual(b['candidates'],0)

    def test_orientation_lock_keeps_original_invalid_state_without_partial_commit(self):
        f=fixture();pl=f[1];old=list(pl.orient)
        with patch.object(pl,'allowed_orientations',side_effect=lambda i:[old[i]]):
            result,r,b=run(f)
        self.assertIs(result,pl);self.assertEqual(pl.orient,old);self.assertFalse(r['applied'])
        self.assertEqual(b['candidates'],0)

    def test_passive_only_and_unmanaged_sources_gain_no_orientation_permission(self):
        for policy in ('passive','unmanaged'):
            f=list(fixture());ds,pl,c,n,i,plan,names=f
            if policy=='passive':
                plan=plan_for(ds,pl,c,n,i,'passive');pl=ai._install(pl,c,i,plan)
            else:pl.attachment_source_ids=set()
            f[1]=pl;before=freeze(pl)
            result,r,b=run(f)
            self.assertIs(result,pl);self.assertEqual(freeze(pl),before);self.assertFalse(r['applied'])
            self.assertEqual(b['candidates'],0)

    def test_exhausted_budget_or_external_deadline_does_not_build_a_scene(self):
        for mode in ('trials','deadline','zero_seconds'):
            f=fixture();budget=ai.new_attachment_budget();until=perf_counter()+1
            if mode=='trials':budget['candidates']=128
            elif mode=='zero_seconds':budget['spent_seconds']=1;until=perf_counter()-1
            else:until=perf_counter()-1
            with patch.object(rails,'build_scene',side_effect=AssertionError('exhausted')):
                result,r,b=run(f,budget=budget,until=until)
            self.assertIs(result,f[1]);self.assertEqual(r['trials'],0)

    def test_last_shared_trial_can_repair_without_new_allowance(self):
        f=fixture();budget=ai.new_attachment_budget();budget['candidates']=127
        result,r,b=run(f,budget=budget)
        self.assertTrue(r['applied']);self.assertEqual(r['trials'],1)
        self.assertEqual(b['candidates'],128);self.assertEqual(b['max_candidates'],128)

    def test_timeout_during_scene_query_returns_original_without_a_trial(self):
        f=fixture();before=freeze(f[1])
        with patch.object(rails,'build_scene',side_effect=RoutingFailure('expired',{})):
            result,r,b=run(f)
        self.assertIs(result,f[1]);self.assertEqual(freeze(result),before)
        self.assertEqual(r['stop_reason'],'time_budget');self.assertEqual(b['candidates'],0)

    def test_timeout_or_negative_full_static_verdict_never_commits(self):
        for verdict in (None,False):
            f=fixture();before=freeze(f[1])
            with patch.object(ai,'_checked_static',return_value=verdict):
                result,r,b=run(f)
            self.assertIs(result,f[1]);self.assertEqual(freeze(result),before);self.assertFalse(r['applied'])
            self.assertGreater(b['candidates'],0);self.assertLessEqual(b['candidates'],8)

    def test_post_static_deadline_expiry_rolls_back_a_successful_trial(self):
        f=fixture();started=perf_counter();clock=[started]
        def late(*a,**k):clock[0]=started+2;return True
        with patch.object(rails,'perf_counter',side_effect=lambda:clock[0]),patch.object(ai,'_checked_static',side_effect=late):
            result,r,b=run(f,until=started+1)
        self.assertIs(result,f[1]);self.assertFalse(r['applied']);self.assertEqual(b['candidates'],1)

    def test_hpwl_guard_remains_authoritative(self):
        f=fixture();pl=f[1]
        with patch('netlist2schematic.plugins.layout_shared.branch_alignment._hpwl',
                   side_effect=lambda par,p,*a:100 if p is pl else 200):
            result,r,b=run(f)
        self.assertIs(result,pl);self.assertGreater(r['hpwl_rejections'],0)
        self.assertFalse(r['applied']);self.assertLessEqual(b['candidates'],8)

    def test_custom_supply_paths_are_not_reinterpreted(self):
        f=fixture();f[1].power_connections=[]
        with patch.object(rails,'source_marker_conflicts',side_effect=AssertionError('custom path')):
            result,r,b=run(f)
        self.assertIs(result,f[1]);self.assertEqual(r['stop_reason'],'custom_power_connections')

    def test_translation_mirror_rename_and_netlist_reorder_remain_applicable(self):
        for mode in ('none','shift','mirror','rename_reorder'):
            with self.subTest(mode=mode):
                f=list(fixture(prefix='component_' if mode=='rename_reorder' else '',reverse_order=mode=='rename_reorder'))
                p=f[1]
                if mode=='shift':p.column_x=[x+246 for x in p.column_x];p.row_y=[y-122 for y in p.row_y]
                elif mode=='mirror':
                    p.set_state([(r,len(p.column_x)-1-c) for r,c in p.cell_of],
                                [1-o if comp['symbol'] in ('nmos','pmos') else o for o,comp in zip(p.orient,p.comps)])
                f[1]=ai._install(p,f[2],f[4],f[5])
                result,r,b=run(f)
                self.assertTrue(r['applied']);self.assertFalse(faults(f,result))

    def test_reapplication_of_safe_result_is_stable_without_additional_trials(self):
        f=list(fixture());result,r,b=run(f);f[1]=result;before=freeze(result);used=b['candidates']
        again,r2,b2=run(f,budget=b)
        self.assertIs(again,result);self.assertEqual(freeze(again),before)
        self.assertFalse(r2['applied']);self.assertEqual(b2['candidates'],used)
