"""Real-port core cleanup, transactional and independent of GT names."""
from copy import deepcopy
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_attachment_cores import fixture
from tests.test_passive_placement import convert, resistor
from netlist2schematic.plugins.attachment import attachment_cores as cores
from netlist2schematic.plugins.attachment import attachment_core_access as access
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl, _bounds
from netlist2schematic.plugins.preflight.placement_preflight import _static_faults


def installed(**kwargs):
    d, pl, c, n, i, s, lv, readable, model, names = fixture(**kwargs)
    cells = list(pl.cell_of)
    for name, cell in dict(head_a=(1, 4), head_b=(1, 7), reference=(4, 2), output=(4, 3)).items():
        cells[i[names[name]]] = cell
    pl.set_state(cells, pl.orient)
    orient = list(pl.orient)
    for a, b in (('head_a', 'head_b'), ('reference', 'output')):
        orient[i[names[a]]] = 0; orient[i[names[b]]] = 1
    pl.set_state(cells, orient)
    plan = ai.build_attachment_plan(d, c, n, i, s, lv, pl.row_of, policy='all')
    pl = ai._install(pl, c, i, plan)
    pl.offset_x = [0.0]*len(c)
    return pl, c, n, i, plan, names


def state(pl):
    return deepcopy((pl.cell_of, pl.orient, pl.offset_x, pl.row_of, pl.comp_at,
                     pl.column_x, pl.row_y, pl.rng.getstate()))


_UNSET = object()


def shared_installed(**kwargs):
    devices,_,_,_,_,_,_,_,_,names=fixture(**kwargs)
    head=devices[names['head_a']]
    bjt=head['cls']=='BJT'
    control,drain,source=('b','c','e') if bjt else ('g','d','s')
    hub=head[control];rail=head[source];returned=devices[names['head_b']][drain]
    devices['control_cap']=resistor(rail,hub,'C')
    devices['return_r']=resistor(returned,hub)
    cells={names[n]:p for n,p in dict(head_a=(1,4),head_b=(1,7),reference=(4,2),
        output=(4,3),load=(5,7)).items()}
    cells.update(control_cap=(0,2),return_r=(3,3))
    from netlist2schematic.recognition.circuit_structures import analyze_structures
    from netlist2schematic.recognition.device_leveling import compute_levels
    structs=analyze_structures(devices)
    pl,cs,nets,index,_=convert(devices,cells,records=structs)
    plan=ai.build_attachment_plan(devices,cs,nets,index,structs,compute_levels(devices),pl.row_of,policy='all')
    pl=ai._install(pl,cs,index,plan);pl.offset_x=[0.]*len(cs)
    for left,right in (('head_a','head_b'),('reference','output')):
        pl.orient[index[names[left]]]=0;pl.orient[index[names[right]]]=1
    return pl,cs,nets,index,plan,names


def finish(f, *, safe=_UNSET, allowance=12, expired=False):
    pl, c, n, i, plan, _ = f
    budget = ai.new_attachment_budget(max_candidates=allowance)
    report = dict(actions=[], accepted=0, candidates=0)
    until = perf_counter()+(-1 if expired else 1)
    args = (structured.par, pl, pl, c, n, i, plan, RoutingOptions(), budget, until, report)
    with patch('netlist2schematic.routing.orthogonal_router._find_path', side_effect=AssertionError('Astar')):
        if safe is _UNSET:
            result = ai._core_access_finish(*args, hpwl_limit=_hpwl(structured.par, pl, n, i)*1.15)
        else:
            with patch.object(ai, '_checked_static', return_value=safe):
                result = ai._core_access_finish(*args, hpwl_limit=_hpwl(structured.par, pl, n, i)*1.15)
    return result, report, budget


class LocalCoreAccessTests(unittest.TestCase):
    def test_facing_only_retains_every_conduction_point_axes_and_unrelated_device(self):
        pl, c, n, i, plan, names = installed(); before = state(pl)
        model = plan['core_rows']; core, = model['cores']
        trial, action = next(access.access_trials(pl, c, i, model, core))
        self.assertEqual(action['strategy'], 'face_controls')
        self.assertTrue(action['facing_only_conduction_points_frozen'])
        self.assertEqual(cores.measure_cores(trial, c, i, model)['control_facing_errors'], 0)
        for name in core['terminal_contract']:
            for pin in (cores._PINS[c[i[name]]['symbol']][0], cores._PINS[c[i[name]]['symbol']][2]):
                self.assertEqual(trial.pin_world(i[name], pin), pl.pin_world(i[name], pin))
        self.assertEqual(trial.pos_of(i['load']), pl.pos_of(i['load']))
        self.assertEqual((trial.column_x, trial.row_y), (pl.column_x, pl.row_y))
        self.assertEqual(state(pl), before)

    def test_corresponding_heads_use_real_drains_and_bounded_offsets(self):
        pl, c, n, i, plan, names = installed(); model = plan['core_rows']; core, = model['cores']
        trial, action = next((t, a) for t, a in access.access_trials(pl, c, i, model, core)
                             if a['strategy']=='heads_to_mirror')
        metric = cores.measure_cores(trial, c, i, model)
        self.assertEqual(metric['support_pin_error'], 0)
        self.assertEqual(metric['control_facing_errors'], 0)
        self.assertEqual(trial.row_of, pl.row_of)
        self.assertTrue(trial.valid_state())
        for j, dx in enumerate(trial.offset_x):
            low, high = _bounds(trial, j)
            self.assertTrue(low <= dx <= high)
            self.assertEqual(dx % 2, 0)
        self.assertEqual(trial.cell_of[i['load']], pl.cell_of[i['load']])

    def test_foreign_occupants_are_not_displaced_to_make_a_template_fit(self):
        pl, c, n, i, plan, names = installed()
        cells = list(pl.cell_of); cells[i['load']] = 1, 2; pl.set_state(cells, pl.orient)
        before = state(pl); model = plan['core_rows']; core, = model['cores']
        trials = list(access.access_trials(pl, c, i, model, core))
        self.assertFalse(any(a['strategy']=='heads_to_mirror' for _, a in trials))
        self.assertTrue(all(t.cell_of[i['load']] == (1, 2) for t, _ in trials))
        self.assertEqual(state(pl), before)

    def test_bjt_dual_renamed_reordered_cases_get_equivalent_legal_access(self):
        for kwargs in ({}, {'bjt': True}, {'dual': True}, {'bjt': True, 'dual': True}, {'rename': True}):
            with self.subTest(kwargs=kwargs):
                f = installed(**kwargs); pl, c, n, i, plan, names = f
                result, report, budget = finish(f)
                self.assertTrue(report['accepted'])
                self.assertTrue(result.valid_state())
                self.assertEqual(_static_faults(structured.par, result, n, i, RoutingOptions(), None)['count'], 0)
                self.assertEqual(cores.measure_cores(result, c, i, plan['core_rows'])['control_facing_errors'], 0)
                self.assertLessEqual(budget['candidates'], 12)

    def test_static_rejection_timeout_and_no_allowance_roll_back_every_field(self):
        for kwargs in ({'safe': False}, {'safe': None}, {'allowance': 0}, {'expired': True}):
            with self.subTest(kwargs=kwargs):
                f = installed(); before = state(f[0])
                result, report, budget = finish(f, **kwargs)
                self.assertEqual(state(result), before)
                self.assertEqual(state(f[0]), before)
                self.assertFalse(report['accepted'])
                self.assertLessEqual(budget['candidates'], budget['max_candidates'])

    def test_fixed_devices_keep_both_origin_and_orientation(self):
        pl, c, n, i, plan, names = installed()
        pl.fixed = {i[n] for n in ('reference', 'output', 'head_a', 'head_b')}
        model = plan['core_rows']; core, = model['cores']
        self.assertEqual(list(access.access_trials(pl, c, i, model, core)), [])

    def test_shared_head_or_changed_terminal_does_not_gain_guessed_access(self):
        pl, c, n, i, plan, names = installed(); model = deepcopy(plan['core_rows']); core, = model['cores']
        duplicate = deepcopy(core); duplicate['id'] += ':other'; model['cores'].append(duplicate)
        self.assertEqual(list(access.access_trials(pl, c, i, model, core)), [])
        changed = deepcopy(c); changed[i['head_a']]['nodes']['G'] = 'different'
        self.assertEqual(list(access.access_trials(pl, changed, i, plan['core_rows'], core)), [])

    def test_unaligned_rows_and_unrepresentable_mirror_offset_skip(self):
        pl, c, n, i, plan, names = installed(); model = plan['core_rows']; core, = model['cores']
        self.assertEqual(list(access.access_trials(pl, c, i, model, core, grid_step=3)), [])
        self.assertTrue(list(access.access_trials(pl, c, i, model, core, grid_step=4)))
        cells = list(pl.cell_of); cells[i['reference']] = 3, 2; pl.set_state(cells, pl.orient)
        self.assertEqual(list(access.access_trials(pl, c, i, model, core)), [])

    def test_best_safe_trial_is_committed_and_all_old_input_state_remains(self):
        f = installed(); pl, c, n, i, plan, names = f; before = state(pl)
        result, report, budget = finish(f)
        self.assertEqual(state(pl), before)
        self.assertEqual(report['accepted'], 1)
        self.assertEqual(report['actions'][0]['strategy'], 'heads_to_mirror')
        self.assertEqual(report['core_access_after']['support_pin_error'], 0)
        self.assertEqual(result.pos_of(i['load']), pl.pos_of(i['load']))

    def test_core_trials_only_spend_the_remaining_shared_budget(self):
        pl, c, n, i, plan, names = installed()
        budget = ai.new_attachment_budget(max_candidates=12); budget.update(candidates=11, spent_seconds=.3)
        report = dict(actions=[], accepted=0, candidates=0)
        ai._core_access_finish(structured.par, pl, pl, c, n, i, plan, RoutingOptions(), budget,
            perf_counter()+1, report, hpwl_limit=_hpwl(structured.par, pl, n, i)*1.15)
        self.assertEqual(budget['candidates'], 12)
        self.assertEqual(budget['spent_seconds'], .3)
        self.assertEqual(report['core_access_trials'], 1)

    def test_initial_control_repair_still_precedes_core_access(self):
        pl, c, n, i, plan, names = installed(); order = []
        metadata = dict(layout_attachments=dict(plan=plan, budget=ai.new_attachment_budget(max_candidates=12)))
        def shared(par, current, *args, **kwargs):
            order.append('existing_shared_control'); return current
        def core(par, current, *args, **kwargs):
            order.append('new_core_access'); return current
        with patch.object(ai, '_shared_control_finish', side_effect=shared), \
             patch.object(ai, '_core_access_finish', side_effect=core):
            ai.refine_attachments(structured.par, pl, c, n, i, metadata, options=RoutingOptions())
        self.assertEqual(order, ['existing_shared_control', 'new_core_access'])

    def test_real_RC_finish_measures_the_final_host_junction(self):
        from netlist2schematic.plugins.attachment.attachment_control_nodes import measure_nodes
        for kwargs in ({},{'rename':True},{'bjt':True}):
            with self.subTest(kwargs=kwargs):
                pl,cs,nets,index,plan,names=shared_installed(**kwargs)
                from netlist2schematic.plugins.region.region_controls import _face_control
                for name,direction in (('reference','east'),('output','west')):
                    self.assertTrue(_face_control(pl,index[names[name]],direction,2))
                before=state(pl)
                # Keep the reference branch still so the real accepted move
                # relocates the heads and their shared control hub.
                pl.fixed={index[names[n]] for n in ('reference','output')}
                metadata=dict(candidate_id=2,layout_attachments=dict(policy='all',applied=True,
                    plan=plan,budget=ai.new_attachment_budget()))
                with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar')):
                    result,md=ai.refine_attachments(structured.par,pl,cs,nets,index,metadata,options=RoutingOptions())
                actions=md['layout_attachments']['refinement']['actions']
                core=[a for a in actions if a['kind']=='local_core_access']
                guest=[a for a in actions if a['kind']=='shared_control_access']
                self.assertTrue(core);self.assertTrue(guest)
                self.assertLess(actions.index(core[-1]),actions.index(guest[-1]))
                measured=measure_nodes(result,cs,index,plan['control_nodes'],plan['readability_plan']['junctions'])
                actual_hub=measured['records'][0]['hub']
                self.assertEqual(guest[-1]['after']['records'][0]['hub'][0],actual_hub[0])
                self.assertNotEqual(guest[-1]['after']['records'][0]['hub'][0],
                    measure_nodes(pl,cs,index,plan['control_nodes'],plan['readability_plan']['junctions'])['records'][0]['hub'][0])
                self.assertEqual(cores.measure_cores(result,cs,index,plan['core_rows'])['support_pin_error'],0)
                refresh=md['layout_attachments']['refinement']['shared_control_host_refresh']
                self.assertEqual(refresh['status'],'improved')
                self.assertEqual(refresh['changed_nodes'],[plan['control_nodes']['nodes'][0]['net']])
                self.assertLess(refresh['after']['endpoint_distance'],refresh['before']['endpoint_distance'])
                self.assertFalse(refresh['budget_reset'])
                self.assertEqual(state(pl),before);self.assertTrue(result.valid_state())
                budget=md['layout_attachments']['budget']
                self.assertLessEqual(budget['candidates'],128);self.assertEqual(budget['seconds_limit'],1)

    def test_fixed_core_keeps_its_world_but_RC_can_still_refine(self):
        pl,cs,nets,index,plan,names=shared_installed()
        ids=[index[names[n]] for n in ('head_a','head_b','reference','output')]
        pl.fixed=set(ids)
        old=[(pl.cell_of[i],pl.orient[i],pl.pos_of(i)) for i in ids]
        metadata=dict(candidate_id=2,layout_attachments=dict(policy='all',applied=True,
            plan=plan,budget=ai.new_attachment_budget()))
        result,md=ai.refine_attachments(structured.par,pl,cs,nets,index,metadata,options=RoutingOptions())
        self.assertEqual([(result.cell_of[i],result.orient[i],result.pos_of(i)) for i in ids],old)
        self.assertFalse(any(a['kind']=='local_core_access' for a in md['layout_attachments']['refinement']['actions']))
        self.assertTrue(any(a['kind']=='shared_control_access' for a in md['layout_attachments']['refinement']['actions']))


class SharedHostRefreshTests(unittest.TestCase):
    def prepared(self):
        pl,cs,nets,index,plan,names=shared_installed()
        checkpoint=ai._control_host_world(pl,index,plan['control_nodes'])
        model=plan['core_rows'];core,=model['cores']
        moved,_=next((t,a) for t,a in access.access_trials(pl,cs,index,model,core)
                     if a['strategy']=='heads_to_mirror')
        return pl,moved,cs,nets,index,plan,checkpoint

    def refresh(self,original,current,cs,nets,index,plan,checkpoint,budget,*,expired=False):
        report=dict(actions=[],accepted=0,candidates=0)
        result=ai._refresh_shared_control_hosts(structured.par,current,original,cs,nets,index,
            plan,RoutingOptions(),budget,perf_counter()+(-1 if expired else 1),report,
            checkpoint,hpwl_limit=_hpwl(structured.par,original,nets,index)*1.15)
        return result,report

    def test_guest_only_change_does_not_repeat_a_finished_host(self):
        original,_,cs,nets,index,plan,checkpoint=self.prepared()
        guest=deepcopy(original);guest.offset_x[index['control_cap']]+=2
        budget=ai.new_attachment_budget();budget.update(candidates=17,spent_seconds=.25)
        before=deepcopy(budget)
        with patch.object(ai,'_shared_control_finish',side_effect=AssertionError('unchanged host')):
            result,report=self.refresh(original,guest,cs,nets,index,plan,checkpoint,budget)
        self.assertIs(result,guest);self.assertEqual(budget,before)
        self.assertEqual(report['shared_control_host_refresh']['status'],'unchanged_hosts')

    def test_exhausted_remaining_ledger_or_deadline_never_opens_another_budget(self):
        for expired in (False,True):
            with self.subTest(expired=expired):
                original,moved,cs,nets,index,plan,checkpoint=self.prepared()
                budget=ai.new_attachment_budget()
                budget.update(candidates=17 if expired else 128,spent_seconds=.6)
                before=deepcopy(budget);world=state(moved)
                with patch.object(ai,'_shared_control_finish',side_effect=AssertionError('no remaining budget')):
                    result,report=self.refresh(original,moved,cs,nets,index,plan,checkpoint,budget,expired=expired)
                self.assertEqual(state(result),world);self.assertEqual(budget,before)
                detail=report['shared_control_host_refresh']
                self.assertEqual(detail['status'],'remaining_budget_exhausted')
                self.assertEqual((detail['trials'],detail['accepted']),(0,0))
                self.assertEqual(detail['before'],detail['after'])

    def test_rejected_final_access_rolls_back_while_spending_only_one_remaining_trial(self):
        original,moved,cs,nets,index,plan,checkpoint=self.prepared()
        budget=ai.new_attachment_budget();budget.update(candidates=127,spent_seconds=.6)
        before=state(moved);old=state(original)
        with patch.object(ai,'_checked_static',return_value=False), \
             patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar')):
            result,report=self.refresh(original,moved,cs,nets,index,plan,checkpoint,budget)
        self.assertEqual(state(result),before);self.assertEqual(state(original),old)
        self.assertEqual(budget['candidates'],128);self.assertEqual(budget['spent_seconds'],.6)
        detail=report['shared_control_host_refresh']
        self.assertEqual((detail['trials'],detail['accepted']),(1,0))
        self.assertEqual(detail['status'],'no_verified_improvement')
        self.assertEqual(detail['before'],detail['after'])

    def test_unknown_checkpoint_does_not_guess_a_host_movement(self):
        original,moved,cs,nets,index,plan,checkpoint=self.prepared()
        checkpoint={n:None for n in checkpoint};budget=ai.new_attachment_budget()
        with patch.object(ai,'_shared_control_finish',side_effect=AssertionError('unknown checkpoint')):
            result,report=self.refresh(original,moved,cs,nets,index,plan,checkpoint,budget)
        self.assertIs(result,moved);self.assertEqual(budget['candidates'],0)
        self.assertEqual(report['shared_control_host_refresh']['changed_nodes'],[])


if __name__ == '__main__':
    unittest.main()
