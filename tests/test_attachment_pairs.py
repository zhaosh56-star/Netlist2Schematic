"""Matching load groups receive joint space from topology, with full rollback."""
from copy import deepcopy
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from netlist2schematic.plugins.attachment import attachment_integration as integration
from netlist2schematic.plugins.attachment.attachment_pairs import paired_bank_trials
from tests.test_attachment_ports import installed
from tests.test_passive_placement import mos, resistor


def fixture(*, dual=False, renamed=False):
    devices = dict(a=mos('left','right'), b=mos('right','left'))
    rail = '0' if dual else 'vdd'
    for prefix, net in (('x','left'),('y','right')):
        for suffix,kind in (('r','R'),('c','C'),('l','L')):
            devices[prefix+suffix] = resistor(rail,net,kind)
    if dual:
        for name in ('a','b'):
            devices[name].update(type='PMOS',s='vdd',bulk='vdd',model='PMOS')
    cells = dict(a=(2,3),b=(2,4),xr=(0,0),xc=(1,0),xl=(3,0),
                 yr=(0,7),yc=(1,7),yl=(3,7))
    if renamed:
        names = {n:'device_'+str(71-k) for k,n in enumerate(devices)}
        nets = {'left':'node_q','right':'node_p'}
        devices = {names[n]:{k:nets.get(v,v) if isinstance(v,str) else v for k,v in d.items()}
                   for n,d in reversed(list(devices.items()))}
        cells = {names[n]:c for n,c in cells.items()}
    pl,comps,nets,index,plan = installed(devices,cells)
    return devices,pl,comps,nets,index,plan


class PairedBankTests(unittest.TestCase):
    def test_proven_pair_gets_matching_load_banks_without_rail_grouping(self):
        _,pl,comps,_,index,plan = fixture()
        self.assertEqual(len(plan['paired_banks']),1)
        relation, = plan['paired_banks']
        frozen = deepcopy((pl.cell_of,pl.orient,pl.column_x,pl.row_y,pl.rng.getstate()))
        trials = list(paired_bank_trials(pl,comps,index,plan,relation))
        self.assertTrue(trials)
        self.assertLessEqual(len(trials),8)
        legal = [(p,a) for p,a in trials if p.valid_state()]
        self.assertTrue(legal)
        for trial,action in legal:
            metric = integration.measure_attachments(trial,comps,index,plan)
            self.assertEqual(metric['host_access']['errors'],0)
            self.assertGreaterEqual(abs(action['branch_columns'][0]-action['branch_columns'][1]),4)
            for group in plan['groups']:
                self.assertEqual(len({trial.cell_of[index[n]][0] for n in group['members']}),1)
            self.assertEqual([trial.cell_of[index[n]][0] for n in ('a','b')],[2,2])
        self.assertEqual((pl.cell_of,pl.orient,pl.column_x,pl.row_y,pl.rng.getstate()),frozen)

    def test_renaming_and_reordering_keep_evidence_and_front_side_feasible(self):
        for renamed in (False,True):
            _,pl,comps,_,index,plan = fixture(renamed=renamed)
            self.assertEqual(len(plan['paired_banks']),1)
            trials = list(paired_bank_trials(pl,comps,index,plan,plan['paired_banks'][0]))
            self.assertTrue(any(p.valid_state() and integration.measure_attachments(
                p,comps,index,plan)['host_access']['errors']==0 for p,_ in trials))

    def test_mirrored_initial_layout_keeps_a_safe_joint_arrangement(self):
        _,pl,comps,_,index,plan = fixture()
        mirrored = deepcopy(pl)
        orientations = [integration._mirror_orientation(pl,i) for i in range(len(comps))]
        mirrored.set_state([(r,pl.cols-1-c) for r,c in pl.cell_of],orientations)
        trials = list(paired_bank_trials(mirrored,comps,index,plan,plan['paired_banks'][0]))
        self.assertTrue(any(p.valid_state() and integration.measure_attachments(
            p,comps,index,plan)['host_access']['errors']==0 for p,_ in trials))

    def test_polarity_dual_uses_south_facing_collector_side(self):
        _,pl,comps,_,index,plan = fixture(dual=True)
        self.assertEqual(len(plan['paired_banks']),1)
        trials = [(p,a) for p,a in paired_bank_trials(pl,comps,index,plan,plan['paired_banks'][0]) if p.valid_state()]
        self.assertTrue(trials)
        for trial,action in trials:
            self.assertEqual(action['direction'],'south')
            self.assertGreater(action['row'],2)
            self.assertEqual(integration.measure_attachments(trial,comps,index,plan)['host_access']['errors'],0)

    def test_same_rail_and_same_models_without_a_core_cannot_create_a_pair(self):
        devices,_,_,_,_,_ = fixture()
        devices['b']['g'] = 'independent_input'
        # Rebuild on a source with the two cross links absent.
        devices['a']['g'] = 'other_input'
        cells = dict(a=(2,3),b=(2,4),xr=(0,0),xc=(1,0),xl=(3,0),yr=(0,7),yc=(1,7),yl=(3,7))
        _,_,_,_,plan = installed(devices,cells)
        self.assertEqual(plan['paired_banks'],[])

    def test_fixed_hosts_are_not_displaced_and_exhausted_budget_is_a_noop(self):
        devices,pl,comps,nets,index,plan = fixture()
        for n in ('a','b'):
            i=index[n]; pl.fixed.add(i);pl.fixed_cells[i]=pl.cell_of[i]
        points={i:pl.pos_of(i) for i in pl.fixed}
        result,report=integration.initialize_attachments(pl,devices,comps,nets,index,plan)
        self.assertEqual({i:result.pos_of(i) for i in pl.fixed},points)
        self.assertFalse(any(a['kind']=='paired_load_slots' for a in report['actions']))
        result,report=integration.initialize_attachments(pl,devices,comps,nets,index,plan,
            budget=integration.new_attachment_budget(seconds=0))
        self.assertIs(result,pl)
        self.assertEqual(report['candidate_trials'],0)

    def test_paired_stage_commits_only_static_safe_state_and_never_uses_astar(self):
        devices,pl,comps,nets,index,plan = fixture()
        with patch('netlist2schematic.routing.orthogonal_router.route_scene',
                   side_effect=AssertionError('no A* in trial placement')):
            result,report=integration.initialize_attachments(pl,devices,comps,nets,index,plan)
        self.assertTrue(report['applied'])
        self.assertTrue(any(a['kind']=='paired_load_slots' for a in report['actions']))
        self.assertTrue(result.valid_state())
        self.assertEqual(report['after']['host_access']['errors'],0)
        self.assertLessEqual(report['budget']['candidates'],128)
        self.assertEqual(list(paired_bank_trials(pl,comps,index,plan,plan['paired_banks'][0],
                                               deadline=time.perf_counter()-1)),[])

    def test_off_candidates_never_build_the_new_evidence(self):
        from netlist2schematic.layout.structured_placement import create_candidates
        source=Path(__file__).resolve().parent/'fixtures/aesthetic-baseline/1.cir'
        with patch('netlist2schematic.plugins.attachment.attachment_access.build_access_links',
                   side_effect=AssertionError('off must bypass access evidence')), patch(
                'netlist2schematic.plugins.attachment.attachment_pairs.build_paired_banks',
                side_effect=AssertionError('off must bypass paired load evidence')):
            self.assertEqual(len(list(create_candidates(source,iterations=0,attachment_layout='off'))),3)


if __name__ == '__main__':
    unittest.main()
