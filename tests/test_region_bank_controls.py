"""Local clock/bias banks preserve the full net and real conduction anchors."""
from copy import deepcopy
from time import perf_counter
from unittest import TestCase
from unittest.mock import patch

from netlist2schematic.layout.structured_placement import par
from netlist2schematic.plugins.region import region_bank_controls as banks
from netlist2schematic.plugins.region.region_layout import measure_regions
from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.routing_rules import RoutingOptions, build_scene
from tests.test_region_placement import region_seed
from tests.test_region_layout import comparator


def example(factory=comparator):
    pl, comps, nets, index, plan, *_ = region_seed(factory)
    meta = {'layout_regions': dict(applied=True, plan=plan,
                                   initialization={'trials': 0, 'elapsed_seconds': 0})}
    return pl, comps, nets, index, meta


def snapshot(pl):
    return deepcopy((pl.cell_of, pl.orient, pl.comp_at, pl.row_of, pl.column_x,
                     pl.row_y, getattr(pl, 'offset_x', None), pl.rng.getstate()))


def apply(case, **kwargs):
    return banks.refine_control_banks(par, *case, **kwargs)


def report(meta):
    return meta['layout_regions']['bank_control_refinement']


class RegionBankControlTests(TestCase):
    def test_five_terminal_control_retains_all_ports_and_shortens_each_local_access(self):
        f = example(); pl, comps, nets, index, meta = f
        before, original_meta, original_nets = snapshot(pl), deepcopy(meta), deepcopy(nets)
        scene = build_scene(par, nets, pl, index)
        self.assertEqual(len(scene.pins['clock']), 5)
        with patch('netlist2schematic.routing.orthogonal_router._find_path', side_effect=AssertionError('A*')):
            trial, out = apply(f)
        r = report(out)
        self.assertTrue(r['applied']); self.assertEqual(len(r['actions']), 2)
        self.assertEqual(r['candidates'], 2)
        self.assertTrue(all(a['after']['span'] == a['before']['span']-60 for a in r['actions']))
        self.assertTrue(all(a['after']['facing_errors'] == 0 for a in r['actions']))
        for i, comp in enumerate(comps):
            for pin in ('D', 'S'):
                self.assertEqual(trial.pin_world(i, pin), pl.pin_world(i, pin))
        after = build_scene(par, nets, trial, index)
        self.assertEqual(len(after.pins['clock']), 5)
        self.assertEqual(after.fixed_segments['hi'], scene.fixed_segments['hi'])
        self.assertEqual(after.fixed_segments['lo'], scene.fixed_segments['lo'])
        self.assertEqual(_static_faults(par, trial, nets, index, RoutingOptions(), None)['count'], 0)
        self.assertEqual((trial.cell_of, trial.row_of, trial.comp_at, trial.column_x, trial.row_y),
                         (pl.cell_of, pl.row_of, pl.comp_at, pl.column_x, pl.row_y))
        self.assertEqual(snapshot(pl), before); self.assertEqual(meta, original_meta)
        self.assertEqual(nets, original_nets); self.assertEqual(trial.rng.getstate(), pl.rng.getstate())

    def test_stale_control_stubs_cannot_stand_in_for_the_relocated_real_pin(self):
        from netlist2schematic.routing.sa_routing import routing_errors
        f = example(); p, c, n, i, _ = f
        old = build_scene(par, n, p, i)
        trial, _ = apply(f)
        errors = routing_errors(list(old.fixed_segments.items()), n, trial, i)
        self.assertTrue(any(e.startswith('clock: disconnected original pin') for e in errors))

    def test_unchanged_conduction_measurements_and_organization_are_measured_on_same_plan(self):
        f = example(); p, c, n, i, m = f
        before = measure_regions(p, c, i, m['layout_regions']['plan'])
        trial, out = apply(f)
        after = out['layout_regions']['metrics']
        self.assertEqual(after['coverage'], before['coverage'])
        for key in ('interleaving', 'order_errors', 'pin_axis_error', 'junction_error'):
            self.assertLessEqual(after[key], before[key])

    def test_shared_clock_without_port_banks_has_no_pair_privilege(self):
        f = example(); f[4]['layout_regions']['plan']['port_banks'] = []
        p, out = apply(f)
        self.assertIs(p, f[0]); self.assertFalse(report(out)['applied'])
        self.assertEqual(report(out)['stop_reason'], 'no_supported_control_bank')

    def test_incomplete_parent_branch_target_or_supply_evidence_is_rejected(self):
        for mode in ('parent', 'source_branch', 'target_branch', 'target_pin', 'target_net',
                     'ownership', 'attachment', 'source_pin', 'control', 'supply', 'third_member'):
            with self.subTest(mode=mode):
                p, c, n, i, m = example(); plan = m['layout_regions']['plan']
                plan['port_banks'] = plan['port_banks'][:1]
                bank = plan['port_banks'][0]
                source = next(r for r in plan['regions'] if r['id'] == bank['source_region'])
                target = next(r for r in plan['regions'] if r['id'] == bank['target_region'])
                a, b = source['members']
                if mode == 'parent': source['parent'] = None
                elif mode == 'source_branch': source['branches'] = []
                elif mode == 'target_branch': target['branches'] = []
                elif mode == 'target_pin': bank['ports'][0]['targets'][0]['pin'] = 'G'
                elif mode == 'target_net': bank['ports'][0]['targets'][0]['device'] = 'tail'
                elif mode == 'ownership': plan['ownership'][a] = 'unrelated'
                elif mode == 'attachment': source['attachments'][0]['role'] = 'unknown'
                elif mode == 'source_pin': bank['ports'][0]['pin'] = 'G'
                elif mode == 'control': c[i[b]]['nodes']['G'] = 'other'
                elif mode == 'supply': c[i[b]]['nodes']['S'] = 'other'
                else: source['members'].append('tail')
                model = banks.compile_control_banks(par, c, n, i, plan)
                self.assertFalse(model['groups']); self.assertTrue(model['skipped'])

    def test_duplicate_equivalent_bank_is_deduplicated(self):
        p, c, n, i, m = example(); plan = m['layout_regions']['plan']
        plan['port_banks'].append(deepcopy(plan['port_banks'][0]))
        model = banks.compile_control_banks(par, c, n, i, plan)
        self.assertEqual(len(model['groups']), 2)

    def test_ambiguous_member_claims_are_not_paired_arbitrarily(self):
        f = example(); p, c, n, i, m = f
        # A malformed plan with two leaf owners is rejected by the compiler,
        # before geometry or a direction preference can choose a winner.
        plan = m['layout_regions']['plan']; bank = deepcopy(plan['port_banks'][0])
        source = deepcopy(next(r for r in plan['regions'] if r['id'] == bank['source_region']))
        source['id'] += '-ambiguous'; bank['source_region'] = source['id']
        plan['regions'].append(source); plan['port_banks'].append(bank)
        parent = next(r for r in plan['regions'] if r['id'] == source['parent'])
        parent['children'].append(source['id'])
        compiled = banks.compile_control_banks(par, c, n, i, plan)
        self.assertEqual(len(compiled['groups']), 2)
        self.assertTrue(any(s['reason'] == 'unproven_bank_or_branch' for s in compiled['skipped']))

    def test_fixed_cells_fixed_ids_and_role_guards_protect_the_whole_bank(self):
        for kind in ('fixed_index', 'fixed_name', 'fixed_cell', 'role'):
            with self.subTest(kind=kind):
                f = example(); p, c, n, i, m = f
                group = banks.compile_control_banks(par, c, n, i, m['layout_regions']['plan'])['groups'][0]
                name = group['members'][0]; j = i[name]
                if kind == 'fixed_index': p.fixed.add(j)
                elif kind == 'fixed_name': p.fixed.add(name)
                elif kind == 'fixed_cell': p.fixed_cells[j] = p.cell_of[j]
                else: m['role_adjustments'] = [dict(device=name, reason='clock_access')]
                trial, out = apply(f)
                self.assertFalse(any(a['bank'] == group['id'] for a in report(out)['actions']))
                for member in group['members']:
                    k = i[member]
                    self.assertEqual((trial.pos_of(k), trial.orient[k]), (p.pos_of(k), p.orient[k]))
                self.assertTrue(any(s['reason'] == 'fixed_or_role_protected' for s in report(out)['skipped']))

    def test_orientation_lock_or_insufficient_grid_offset_rolls_back(self):
        for kind in ('locked', 'grid'):
            f = example(); p = f[0]; before = snapshot(p)
            if kind == 'locked':
                p.allowed_orientations = lambda j: [p.orient[j]]
                trial, out = apply(f)
                del p.allowed_orientations
            else:
                with patch('netlist2schematic.plugins.region.region_controls._grid_offset', return_value=None):
                    trial, out = apply(f)
            self.assertIs(trial, p); self.assertFalse(report(out)['applied'])
            self.assertEqual(snapshot(p), before)

    def test_nonhorizontal_bank_is_skipped(self):
        f = example(); p, c, n, i, m = f
        g = banks.compile_control_banks(par, c, n, i, m['layout_regions']['plan'])['groups'][0]
        p.cell_of[i[g['members'][0]]] = (3, p.cell_of[i[g['members'][0]]][1])
        trial, out = apply(f)
        self.assertTrue(any(s['reason'] == 'not_a_horizontal_local_pair' for s in report(out)['skipped']))
        self.assertFalse(any(a['bank'] == g['id'] for a in report(out)['actions']))

    def test_static_oracle_rejection_keeps_current_pose(self):
        f = example(); p, c, n, i, m = f
        # The complete original scene oracle, not a route-length estimate,
        # determines whether an otherwise attractive mirror can be committed.
        with patch.object(banks, '_static_safe', return_value=False):
            trial, out = apply(f)
        self.assertIs(trial, p); self.assertFalse(report(out)['applied'])
        self.assertEqual(report(out)['static_rejections'], 2)
        self.assertEqual(out['layout_regions']['local_budget']['candidates'], 2)

    def test_timeout_during_static_check_rolls_back_and_keeps_consumed_trial(self):
        f = example(); before = snapshot(f[0])
        with patch.object(banks, '_static_safe', side_effect=RoutingFailure('expired', {})):
            trial, out = apply(f)
        self.assertIs(trial, f[0]); self.assertEqual(snapshot(trial), before)
        self.assertEqual(report(out)['stop_reason'], 'local_budget')
        self.assertEqual(out['layout_regions']['local_budget']['candidates'], 1)

    def test_a_static_check_returning_after_deadline_cannot_commit(self):
        f = example(); clock = [0.0]
        def late(*args): clock[0] = 2.0; return True
        with patch.object(banks, 'perf_counter', side_effect=lambda: clock[0]), patch.object(banks, '_static_safe', side_effect=late):
            trial, out = apply(f)
        self.assertIs(trial, f[0]); self.assertFalse(report(out)['actions'])
        self.assertEqual(out['layout_regions']['local_budget']['elapsed_seconds'], 2)
        self.assertEqual(report(out)['candidates'], 1)

    def test_exhausted_shared_time_trials_or_deadline_never_restart_budget(self):
        for kw in ({'budget':dict(seconds=1,max_candidates=128,elapsed_seconds=1,candidates=0)},
                   {'budget':dict(seconds=1,max_candidates=128,elapsed_seconds=0,candidates=128)},
                   {'deadline':perf_counter()-1}):
            f = example()
            with patch.object(banks, '_face_control', side_effect=AssertionError('exhausted')):
                trial, out = apply(f, **kw)
            self.assertIs(trial, f[0]); self.assertEqual(report(out)['candidates'], 0)
            self.assertEqual(report(out)['stop_reason'], 'local_budget')

    def test_last_shared_trial_is_used_once_without_reset(self):
        f = example(); budget=dict(seconds=1,max_candidates=128,elapsed_seconds=0,candidates=127)
        trial, out = apply(f, budget=budget)
        self.assertEqual(report(out)['candidates'], 1)
        self.assertEqual(len(report(out)['actions']), 1)
        self.assertEqual(out['layout_regions']['local_budget']['candidates'], 128)
        self.assertEqual(budget['candidates'], 127)

    def test_reapplication_is_stable_and_ledger_is_inherited(self):
        f = example(); first, meta = apply(f)
        second, out = banks.refine_control_banks(par, first, *f[1:4], meta)
        self.assertEqual(snapshot(first), snapshot(second))
        self.assertFalse(report(out)['applied'])
        self.assertEqual(out['layout_regions']['local_budget']['candidates'], 2)

    def test_disabled_or_unapplied_regions_return_without_compilation(self):
        for meta in ({}, {'layout_regions':dict(applied=False)}):
            f = example(); f = (*f[:4], meta)
            with patch.object(banks, 'compile_control_banks', side_effect=AssertionError('off')):
                trial, out = apply(f)
            self.assertIs(trial, f[0]); self.assertEqual(out, meta)

    def test_polarity_dual_recognizes_banks_but_respects_its_different_existing_rows(self):
        def dual():
            devices = comparator()
            for d in devices.values():
                d['type'] = d['model'] = 'PMOS' if d['type'] == 'NMOS' else 'NMOS'
                d['bulk'] = 'hi' if d['type'] == 'PMOS' else 'lo'
                for pin in ('d','g','s'):
                    d[pin] = {'hi':'lo','lo':'hi'}.get(d[pin], d[pin])
            return devices
        f = example(dual)
        trial, out = apply(f)
        self.assertEqual(len(report(out)['model']['groups']), 2)
        self.assertIs(trial, f[0])
        self.assertTrue(all(s['reason'] == 'not_a_horizontal_local_pair' for s in report(out)['skipped']))

    def test_translation_mirror_renaming_and_reordering_keep_terminal_evidence(self):
        results=[]
        for mode in ('none','translate','mirror','renamed_reordered'):
            if mode == 'renamed_reordered':
                def factory():
                    ds=comparator(); names={n:'device_'+str(90-k) for k,n in enumerate(ds)}
                    return {names[n]:dict(d,**{pin:'signal_'+d[pin] if d[pin] not in ('hi','lo') else d[pin]
                                               for pin in ('d','g','s')}) for n,d in reversed(list(ds.items()))}
                f=example(factory)
            else: f=example()
            p,c,n,i,m=f
            if mode == 'translate':
                p.column_x=[x+200 for x in p.column_x];p.row_y=[y-200 for y in p.row_y]
            elif mode == 'mirror':
                p.set_state([(r,len(p.column_x)-1-col) for r,col in p.cell_of],[1-o for o in p.orient])
            trial,out=apply(f)
            self.assertTrue(report(out)['applied'], mode)
            results.append(sorted((a['before']['span']-a['after']['span'],a['after']['facing_errors'])
                                  for a in report(out)['actions']))
        self.assertTrue(all(r == results[0] for r in results))

    def test_bjt_control_bank_uses_b_c_e_geometry_and_preserves_both_conduction_pins(self):
        for symbol in ('npn','pnp'):
            with self.subTest(symbol=symbol):
                p,c,n,i,m=example();plan=m['layout_regions']['plan']
                members={port['device'] for bank in plan['port_banks'] for port in bank['ports']}
                pins={'D':'C','G':'B','S':'E'}
                for name in members:
                    comp=c[i[name]];comp['symbol']=symbol
                    comp['nodes']={pins.get(pin,pin):net for pin,net in comp['nodes'].items()}
                n={net:[(name,pins.get(pin,pin) if name in members else pin) for name,pin in ports]
                   for net,ports in n.items()}
                for bank in plan['port_banks']:
                    for port in bank['ports']:port['pin']='C'
                for region in plan['regions']:
                    for attachment in region.get('attachments',[]):
                        if attachment['device'] in members:attachment['pin']='C'
                trial,out=apply((p,c,n,i,m))
                self.assertTrue(report(out)['applied'])
                for name in members:
                    for pin in ('C','E'):self.assertEqual(trial.pin_world(i[name],pin),p.pin_world(i[name],pin))
                self.assertEqual(len(build_scene(par,n,trial,i).pins['clock']),5)

    def test_flow_orders_controls_bank_tail_preflight_then_original_whole_net_router(self):
        from contextlib import ExitStack, redirect_stdout
        import io
        from netlist2schematic.flow.aesthetic_flow import select_layout
        f=example();p,c,n,i,m=f;events=[];original_bank=banks.refine_control_banks
        m.update(candidate_id=2,candidate_seed=71,structs=[])
        def controls(*args,**kw):
            events.append('controls');meta=deepcopy(args[5]);quota=deepcopy(kw['budget'])
            quota['candidates']+=1;meta['layout_regions']['local_budget']=quota
            return args[1],meta
        def bank(*args,**kw):
            events.append('bank');self.assertEqual(kw['budget']['candidates'],1)
            return original_bank(*args,**kw)
        def tail(*args,**kw):
            events.append('tail');self.assertEqual(kw['budget']['candidates'],3)
            return args[1],args[5]
        def preflight(*args,**kw):
            events.append('preflight');return args[1],dict(static_verification=dict(complete=True,valid=True))
        def route(*args,**kw):
            events.append('whole_net_router');self.assertEqual(len(args[1]['clock']),5)
            raise RoutingFailure('unit routing stop',{'reason':'unit_test'})
        with ExitStack() as stack:
            for target,callback in (
                ('netlist2schematic.layout.structured_placement.create_candidates',lambda *a,**k:iter([(p,c,n,i,m)])),
                ('netlist2schematic.plugins.region.region_controls.refine_region_controls',controls),
                ('netlist2schematic.plugins.region.region_bank_controls.refine_control_banks',bank),
                ('netlist2schematic.plugins.region.region_tails.refine_region_tails',tail),
                ('netlist2schematic.plugins.preflight.placement_preflight.repair_static_ports',preflight),
                ('netlist2schematic.flow.aesthetic_flow.final_route',route)):
                stack.enter_context(patch(target,side_effect=callback))
            stack.enter_context(redirect_stdout(io.StringIO()))
            with self.assertRaises(RoutingFailure):
                select_layout('unused.cir',iterations=0,region_layout='auto',port_preflight='repair')
        self.assertEqual(events,['controls','bank','tail','preflight','whole_net_router'])

    def test_default_and_reference_flow_do_not_invoke_bank_pass(self):
        from contextlib import redirect_stdout
        import io
        from netlist2schematic.flow.aesthetic_flow import select_layout
        for policy,identifier in (('off',2),('auto',1)):
            f=example();p,c,n,i,m=f;m.update(candidate_id=identifier,candidate_seed=71,structs=[])
            if policy=='off':m.pop('layout_regions')
            with (patch('netlist2schematic.layout.structured_placement.create_candidates',return_value=iter([(p,c,n,i,m)])),
                  patch('netlist2schematic.flow.aesthetic_flow.final_route',side_effect=RoutingFailure('stop',{})),
                  patch.object(banks,'refine_control_banks',side_effect=AssertionError('bypassed')),
                  redirect_stdout(io.StringIO())):
                with self.assertRaises(RoutingFailure):
                    select_layout('unused.cir',iterations=0,region_layout=policy)
