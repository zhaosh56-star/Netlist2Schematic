"""Retry quotas, static geometry reuse and pre-insertion shape preparation."""
from copy import deepcopy
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_passive_modules import fixture
from tests.test_passive_placement import mos, resistor
from tests import test_regional_routing as regional_tests
from tests.test_series_alignment import PAR
from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.plugins.passive_skeleton import placement, packing_recovery
from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
from netlist2schematic.routing.routing_rules import RoutingOptions


def report(reason='local_budget', trials=8):
    return dict(stop_reason=reason, trials=trials, elapsed_seconds=.1, actions=[], applied=reason=='packed')


class PackingRecoveryTests(unittest.TestCase):
    def shape(self):
        core, cc, nn, ii, metadata = regional_tests.ShapePreparationTests().scene()
        devices = dict(upper=mos('a', 'ga'), lower=mos('b', 'gb'), guest=resistor('a', 'b'))
        doc = to_document(devices)
        comps, nets = doc['components'], doc['nets']
        return core, comps, nets, {c['id']: i for i, c in enumerate(comps)}, metadata['layout_branches']

    def test_shape_repair_precedes_packing_without_changing_core_cells_or_orientations(self):
        core, cs, ns, ix, branch = self.shape()
        old = deepcopy((core.cell_of, core.orient, core.row_y))
        self.assertGreater(_static_faults(PAR, core, {n:[t for t in ts if t[0]!='guest'] for n,ts in ns.items()},
            {c['id']:i for i,c in enumerate(core.comps)}, RoutingOptions(), perf_counter()+2)['count'], 0)
        def pack(par, current, *args, **kwargs):
            names = {c['id']:i for i,c in enumerate(current.comps)}
            nets = {n:[t for t in ts if t[0] in names] for n,ts in ns.items()}
            self.assertEqual(_static_faults(par, current, nets, names, RoutingOptions(), perf_counter()+2)['count'], 0)
            return current, report('packed')
        with patch.object(placement, 'pack', side_effect=pack) as packed:
            fixed, result, data, branch = packing_recovery.prepare_and_pack(PAR, core, cs, ns, ix, {},
                RoutingOptions(), branch, local_deadline=perf_counter()+1, candidate_deadline=perf_counter()+4)
        self.assertEqual(packed.call_count, 1)
        self.assertTrue(branch['core_preparation']['applied'])
        self.assertEqual(fixed.cell_of, old[0]); self.assertEqual(fixed.orient, old[1])
        self.assertEqual(core.row_y, old[2])
        self.assertFalse(data['packing_recovery']['retried'])

    def test_exhausted_packing_retries_once_under_original_candidate_deadline(self):
        core, cs, ns, ix, branch = self.shape()
        stop = perf_counter()+2
        def prepare(par, current, *args, **kwargs):
            return current, dict(layout_branches=dict(route_preparation=dict(applied=False)))
        with patch('netlist2schematic.plugins.organization.route_preparation.prepare', side_effect=prepare), \
                patch.object(placement, 'pack', side_effect=[(None,report()), (core,report('packed',12))]) as packed:
            fixed, result, data, _ = packing_recovery.prepare_and_pack(PAR, core, cs, ns, ix, {},
                RoutingOptions(), branch, local_deadline=perf_counter()+.1, candidate_deadline=stop)
        self.assertIs(result, core); self.assertIs(fixed, core)
        self.assertEqual(packed.call_count, 2)
        self.assertLessEqual(packed.call_args.kwargs['deadline'], stop)
        self.assertTrue(packed.call_args.kwargs['skip_rejected_geometry'])
        self.assertEqual(data['packing_recovery']['total_trials'], 20)

    def test_exhaustive_failure_or_elapsed_global_deadline_does_not_retry(self):
        for reason, stop in (('no_legal_slot', perf_counter()+3), ('local_budget', perf_counter()-1)):
            core, cs, ns, ix, branch = self.shape()
            with patch('netlist2schematic.plugins.organization.route_preparation.prepare',
                       return_value=(core,dict(layout_branches=dict(route_preparation=dict(applied=False))))), \
                    patch.object(placement,'pack',return_value=(None,report(reason))) as packed:
                _, result, _, _ = packing_recovery.prepare_and_pack(PAR, core, cs, ns, ix, {},
                    RoutingOptions(),branch,local_deadline=perf_counter()+.1,candidate_deadline=stop)
            self.assertIsNone(result); self.assertEqual(packed.call_count,1)

    def test_later_static_preparation_does_not_disable_the_repaired_core_policy(self):
        from netlist2schematic.plugins.organization.route_preparation import prepare
        core,comps,nets,index,branch=self.shape()
        cc=core.comps;ii={c['id']:i for i,c in enumerate(cc)}
        nn={n:[t for t in ts if t[0] in ii] for n,ts in nets.items()}
        repaired,data=prepare(PAR,core,cc,nn,ii,dict(layout_branches=branch),RoutingOptions())
        self.assertTrue(repaired.branch_routing['active'])
        again,after=prepare(PAR,repaired,cc,nn,ii,data,RoutingOptions())
        self.assertEqual(again.row_y,repaired.row_y)
        self.assertFalse(after['layout_branches']['route_preparation']['applied'])
        self.assertTrue(again.branch_routing['active'])


class GeometryReuseTests(unittest.TestCase):
    def execute(self, static, guides, spacing_policy='standard'):
        _, core, cs, ns, ix, plan = fixture()
        # This fixture tests alternatives within one template. Auto-template
        # fallback is covered separately by the compact-spacing regressions.
        plan['spacing_policy']=spacing_policy
        def slots(par, state, *args):
            for _ in range(2):
                current=deepcopy(state)
                current.passive_module_guides={}
                yield current
        with patch.object(placement,'reserve',return_value=(deepcopy(plan['groups']),[])), \
                patch.object(placement,'slot_trials',side_effect=slots), \
                patch('netlist2schematic.plugins.preflight.placement_preflight._static_faults',side_effect=static) as checked, \
                patch('netlist2schematic.plugins.passive_skeleton.module_connections.check_guides',side_effect=guides) as wires, \
                patch('netlist2schematic.plugins.passive_skeleton.terminal_slots.refine_terminals',
                      side_effect=lambda par,pl,*a,**kw:(pl,dict(trials=0))):
            result, data = placement.pack(PAR,core,cs,ns,ix,plan,RoutingOptions(),skip_rejected_geometry=True)
        return result,data,checked.call_count,wires.call_count

    def test_duplicate_rejected_positions_do_not_repeat_the_static_oracle(self):
        result,data,checks,wires = self.execute(
            lambda *a:dict(count=1,hard=set(),violations=[dict(kind='body')]),lambda *a:[])
        self.assertIsNone(result)
        self.assertEqual(checks,1);self.assertEqual(wires,0)
        self.assertEqual(data['repeated_static_rejections_skipped'],1)

    def test_statically_valid_geometry_keeps_alternative_wire_guides(self):
        outcomes=iter(([dict(kind='crossing_clearance')],[]))
        result,data,checks,wires = self.execute(
            lambda *a:dict(count=0,hard=set(),violations=[]),
            lambda *a:next(outcomes))
        self.assertIsNotNone(result)
        self.assertEqual(wires,2)
        self.assertEqual(data['repeated_static_rejections_skipped'],0)
        self.assertGreaterEqual(checks,3)


if __name__ == '__main__':unittest.main()
