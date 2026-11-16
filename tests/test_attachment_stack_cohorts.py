"""A proven folded continuation may share same-row owners without splitting them."""
from copy import deepcopy
import json
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_layout_evidence import mos
from tests.test_passive_placement import convert
from tests.test_attachment_stack_columns import arrange
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment import attachment_stack_columns as columns
from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.routing import orthogonal_router


def fixture(*, dual=False, rename=False):
    devices = {}
    for side in range(2):
        for prefix, drain, gate, source, typ in (
            ('top', 'p'+str(side), 'ub', 'vdd', 'PMOS'),
            ('upper', 'out'+str(side), 'uc', 'p'+str(side), 'PMOS'),
            ('fold', 'out'+str(side), 'lc', 'j'+str(side), 'NMOS'),
            ('bias', 'j'+str(side), 'lb', '0', 'NMOS'),
            ('input', 'j'+str(side), 'sig'+str(side), 'tail', 'PMOS')):
            devices[prefix+str(side)] = mos(drain, gate, source, typ)
    devices['supply'] = mos('tail', 'supply_bias', 'vdd', 'PMOS')
    if dual:
        for d in devices.values():
            d['type'] = 'NMOS' if d['type'] == 'PMOS' else 'PMOS'
            for p in ('d','g','s','bulk'):
                if d[p] in ('vdd','0'): d[p] = {'vdd':'0','0':'vdd'}[d[p]]
    levels = compute_levels(devices)
    cols = dict(top0=1, top1=2, upper0=2, upper1=1, fold0=1, fold1=2,
        bias0=1, bias1=2, input0=4, input1=5, supply=5)
    cells = {n:(levels['device_levels'][n], cols[n]) for n in devices}
    names = {n:n for n in devices}
    if rename:
        names = {n:'component'+str(80-i) for i,n in enumerate(devices)}
        devices = {names[n]:{k:('wire_'+v if k in ('d','g','s','bulk') and v not in ('vdd','0') else v)
            for k,v in d.items()} for n,d in reversed(list(devices.items()))}
        cells = {names[n]:c for n,c in cells.items()}
        levels = compute_levels(devices)
    structs = analyze_structures(devices)
    pl, comps, nets, index, _ = convert(devices, cells, records=structs)
    plan = ai.build_attachment_plan(devices,comps,nets,index,structs,levels,pl.row_of,policy='all')
    pl.set_state([(plan['display_rows'][c['id']], pl.cell_of[i][1]) for i,c in enumerate(comps)],pl.orient)
    return devices, ai._install(pl,comps,index,plan), comps,nets,index,plan,structs,names


class ProtectedStackCohortTests(unittest.TestCase):
    def test_full_branch_includes_verified_fold_but_not_input_or_supply(self):
        f=fixture(); model=f[5]['stack_columns']
        self.assertEqual(len(model['banks']),1)
        self.assertEqual({frozenset(l['members']) for l in model['lanes']},
            {frozenset(prefix+str(side) for prefix in ('top','upper','fold','bias')) for side in range(2)})
        edges=[a for l in model['lanes'] for a in l['edges'] if a.get('evidence')=='verified_folded_main_branch']
        self.assertEqual(len(edges),2)
        self.assertEqual({tuple(a['side_access'][0]) for a in edges},{('input0','D'),('input1','D')})
        self.assertTrue(model['banks'][0]['retained_row_cohorts'])
        self.assertFalse(model['banks'][0]['ownership_claim'])

    def test_atomic_alignment_preserves_owners_rows_guests_and_true_pins(self):
        f=fixture(); before=deepcopy(f[1].__dict__)
        with patch.object(orthogonal_router,'_find_path',side_effect=AssertionError('no Astar')):
            result,report,budget=arrange(f,safe='real')
        self.assertTrue(report['stack_column_arrangement']['applied'])
        self.assertEqual(columns.measure_columns(result,f[2],f[4],f[5]['stack_columns'])['pin_axis_error'],0)
        self.assertEqual(result.blocks,before['blocks'])
        self.assertEqual([r for r,c in result.cell_of],[r for r,c in before['cell_of']])
        self.assertEqual(result.attachment_axes,before['attachment_axes'])
        for name in ('input0','input1','supply'):
            j=f[4][name]
            self.assertEqual((result.cell_of[j],result.orient[j]),(before['cell_of'][j],before['orient'][j]))
        self.assertLessEqual(budget['candidates'],16)
        self.assertTrue(result.valid_state())

    def test_renaming_reordering_and_polarity_dual_retain_two_real_lanes(self):
        ordinary=fixture(); expected={frozenset(l['members']) for l in ordinary[5]['stack_columns']['lanes']}
        for dual,rename in ((True,False),(False,True),(True,True)):
            with self.subTest(dual=dual,rename=rename):
                f=fixture(dual=dual,rename=rename)
                wanted={frozenset(f[7][n] for n in lane) for lane in expected}
                self.assertEqual({frozenset(l['members']) for l in f[5]['stack_columns']['lanes']},wanted)
                result,_,_=arrange(f)
                self.assertEqual(columns.measure_columns(result,f[2],f[4],f[5]['stack_columns'])['pin_axis_error'],0)

    def test_fork_without_calibrated_fold_contract_remains_a_stop(self):
        f=fixture(); plan=deepcopy(f[5]);plan['folded_cores']['groups']=[]
        m=columns.compile_columns(f[2],plan,[],f[6])
        self.assertFalse(any('bias0' in l['members'] or 'bias1' in l['members'] for l in m['lanes']))
        self.assertFalse(any(a.get('evidence')=='verified_folded_main_branch' for l in m['lanes'] for a in l['edges']))

    def test_mixed_row_and_unrelated_same_row_owners_are_still_not_split(self):
        f=fixture()
        for owner in (['top0','fold1'],['input0','upper1']):
            m=columns.compile_columns(f[2],f[5],[owner],[])
            self.assertFalse(m['banks'])
            self.assertIn('protected_owner_crosses_lane_boundary',{s['reason'] for s in m['skipped']})

    def test_corrupt_roles_ports_or_terminal_snapshot_do_not_authorize_fork(self):
        f=fixture()
        for mutation in ('role','contract','junction','evidence'):
            plan=deepcopy(f[5]);g=plan['folded_cores']['groups'][0]
            if mutation=='role':g['bias'][0]='input0'
            elif mutation=='contract':g['terminal_contract']['fold0']['S']='wrong'
            elif mutation=='junction':plan['readability_plan']['junctions']['j0']['conduction_ports'].remove(['fold0','S'])
            else:g['evidence']['kind']='common_bias_only'
            with self.subTest(mutation=mutation):
                axes,_=columns._folded_continuations(f[2],plan)
                self.assertEqual(axes,[])

    def test_declared_drain_shunt_is_side_access_not_a_new_lane_member(self):
        f=fixture();plan=deepcopy(f[5]);comps=deepcopy(f[2])
        for side in range(2):
            name='shunt'+str(side)
            comps.append(dict(id=name,symbol='nmos',nodes=dict(D='j'+str(side),G='feedback',S='0')))
            plan['readability_plan']['junctions']['j'+str(side)]['conduction_ports'].append([name,'D'])
        axes,_=columns._folded_continuations(comps,plan)
        self.assertEqual(len(axes),2)
        self.assertTrue(all(any(n.startswith('shunt') for n,p in a['side_access']) for a in axes))
        self.assertFalse(any(a['a'].startswith('shunt') or a['b'].startswith('shunt') for a in axes))
        # Another source or supply is ambiguous even at the same node.
        for bad in ('different_supply','source_port'):
            altered=deepcopy(comps);p=deepcopy(plan)
            if bad=='different_supply':altered[-1]['nodes']['S']='unrelated_supply'
            else:p['readability_plan']['junctions']['j1']['conduction_ports'][-1][1]='S'
            self.assertEqual(columns._folded_continuations(altered,p)[0],[])

    def test_rejected_geometry_or_estimated_wire_rolls_back_full_bank(self):
        f=fixture();state=deepcopy({k:v for k,v in f[1].__dict__.items() if k!='rng'})
        rng=f[1].rng.getstate()
        for opts in (dict(safe=False),dict(cap=0)):
            result,_,_=arrange(f,**opts)
            self.assertIs(result,f[1])
            self.assertEqual({k:v for k,v in result.__dict__.items() if k!='rng'},state)
            self.assertEqual(result.rng.getstate(),rng)

    def test_duplicate_axis_sources_are_not_extra_edges_or_a_cycle(self):
        f=fixture();plan=deepcopy(f[5])
        axes,_=columns._folded_continuations(f[2],plan)
        plan['host_axes'].extend(deepcopy(axes))
        model=columns.compile_columns(f[2],plan,[],f[6])
        self.assertEqual(len(model['lanes']),2)
        self.assertTrue(all(len(l['edges'])==3 and len(l['members'])==4 for l in model['lanes']))

    def test_motion_rejection_undo_and_replay_use_existing_state_interface(self):
        f=fixture();pl,_,_=arrange(f)
        for k in range(80):
            old=pl.propose()
            if old is not None and k%3:pl.set_state(*old)
            self.assertTrue(pl.valid_state())
            self.assertEqual(pl.comp_at,{c:i for i,c in enumerate(pl.cell_of)})
            self.assertEqual(pl.row_of,[r for r,c in pl.cell_of])
            self.assertEqual(columns.measure_columns(pl,f[2],f[4],f[5]['stack_columns'])['pin_axis_error'],0)

    def test_saved_model_translation_and_mirror_preserve_topology_measurement(self):
        f=fixture();pl,_,_=arrange(f)
        before=columns.column_rank(columns.measure_columns(pl,f[2],f[4],f[5]['stack_columns']))
        moved=deepcopy(pl);moved.column_x=[x+74 for x in pl.column_x];moved.row_y=[y-26 for y in pl.row_y]
        moved.set_state([(r,moved.cols-1-c) for r,c in moved.cell_of],[1-o for o in moved.orient])
        after=columns.column_rank(columns.measure_columns(moved,f[2],f[4],json.loads(json.dumps(f[5]['stack_columns']))))
        self.assertEqual(before,after)

    def test_compile_does_not_draw_random_or_mutate_saved_plan(self):
        f=fixture();state=deepcopy(f[5]);rng=f[1].rng.getstate()
        columns.compile_columns(f[2],f[5],[],f[6])
        self.assertEqual(f[5],state);self.assertEqual(f[1].rng.getstate(),rng)


if __name__=='__main__': unittest.main()
