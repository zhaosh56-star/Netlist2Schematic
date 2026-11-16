"""A unique real R/L chain can share an axis, while rows and nets stay intact."""
from copy import deepcopy
import time
import unittest
from unittest.mock import patch

from netlist2schematic.plugins.attachment import attachment_integration as integration
from netlist2schematic.plugins.attachment.attachment_conduction import conduction_trials
from tests.test_attachment_ports import installed
from tests.test_passive_placement import mos, resistor


def fixture():
    devices=dict(upper=mos('out','bias','start'),lower=mos('end','input','tail'),
                 bridge=resistor('start','end','L'),bottom=resistor('tail','0','L'))
    cells=dict(upper=(1,5),lower=(3,2),bridge=(2,4),bottom=(4,1))
    pl,comps,nets,index,plan=installed(devices,cells)
    return devices,pl,comps,nets,index,plan


class ConductionAttachmentTests(unittest.TestCase):
    def test_entire_unique_conduction_chain_uses_real_pins_and_unchanged_rows(self):
        _,pl,comps,_,index,plan=fixture()
        original=deepcopy((pl.cell_of,pl.orient,pl.column_x,pl.row_y,pl.rng.getstate()))
        trials=[(p,a) for p,a in conduction_trials(pl,comps,index,plan) if p.valid_state()]
        self.assertTrue(trials)
        for trial,action in trials:
            self.assertEqual(set(action['members']),set(index))
            self.assertEqual([r for r,_ in trial.cell_of],[r for r,_ in pl.cell_of])
            self.assertEqual(len({c for _,c in trial.cell_of}),1)
            points=[trial.pin_world(index['upper'],'S')[0],trial.pin_world(index['bridge'],'1')[0],
                    trial.pin_world(index['bridge'],'2')[0],trial.pin_world(index['lower'],'D')[0],
                    trial.pin_world(index['lower'],'S')[0],trial.pin_world(index['bottom'],'1')[0]]
            self.assertEqual(max(points)-min(points),0)
            self.assertEqual(trial.pin_dir(index['bridge'],'1'),'north')
            self.assertEqual(trial.pin_dir(index['bridge'],'2'),'south')
            self.assertTrue(action['actual_pins'])
        self.assertEqual((pl.cell_of,pl.orient,pl.column_x,pl.row_y,pl.rng.getstate()),original)

    def test_generic_horizontal_slots_cannot_rotate_a_proved_vertical_bridge(self):
        _,pl,_,_,index,plan=fixture()
        group=next(g for g in plan['groups'] if 'bridge' in g['members'])
        self.assertEqual(group['row_mode'],'conduction_bridge')
        self.assertEqual(list(integration._slot_specs(pl,group,index)),[])

    def test_capacitive_and_tapped_paths_are_not_collapsed_into_one_branch(self):
        devices,_,_,_,_,_=fixture()
        devices['bridge']['type']='C'
        cells=dict(upper=(1,5),lower=(3,2),bridge=(2,4),bottom=(4,1))
        pl,comps,_,index,plan=installed(devices,cells)
        for _,action in conduction_trials(pl,comps,index,plan):
            self.assertNotIn('upper',action['members'])
            self.assertNotIn('bridge',action['members'])
        devices['bridge']['type']='L'
        devices['tap']=mos('spare','start')
        cells['tap']=(2,7)
        pl,comps,_,index,plan=installed(devices,cells)
        for _,action in conduction_trials(pl,comps,index,plan):
            self.assertNotIn('upper',action['members'])
            self.assertNotIn('tap',action['members'])

    def test_fixed_anchor_and_no_astar_with_shared_budget(self):
        devices,pl,comps,nets,index,plan=fixture()
        i=index['upper'];pl.fixed.add(i);pl.fixed_cells[i]=pl.cell_of[i]
        point,orientation=pl.pos_of(i),pl.orient[i]
        with patch('netlist2schematic.routing.orthogonal_router.route_scene',
                   side_effect=AssertionError('A* forbidden during trial')):
            result,report=integration.initialize_attachments(pl,devices,comps,nets,index,plan)
        self.assertEqual((result.pos_of(i),result.orient[i]),(point,orientation))
        self.assertTrue(result.valid_state())
        self.assertTrue(any(a['kind']=='conduction_chain_axis' for a in report['actions']))
        self.assertLessEqual(report['budget']['candidates'],128)
        self.assertEqual(list(conduction_trials(pl,comps,index,plan,deadline=time.perf_counter()-1)),[])

    def test_custom_grid_keeps_offsets_on_that_grid(self):
        _,pl,comps,_,index,plan=fixture()
        trials=list(conduction_trials(pl,comps,index,plan,grid_step=8))
        self.assertTrue(trials)
        for trial,_ in trials:
            self.assertTrue(all(abs(v/8-round(v/8))<1e-8 for v in trial.offset_x))

    def test_renaming_reordering_and_mirror_keep_axis_feasible(self):
        devices,_,_,_,_,_=fixture()
        names={n:'part_'+str(100-k) for k,n in enumerate(devices)}
        nets={n:'node_'+str(200-k) for k,n in enumerate(('out','bias','start','end','input','tail'))}
        renamed={names[n]:{k:nets.get(v,v) if isinstance(v,str) else v for k,v in d.items()}
                 for n,d in reversed(list(devices.items()))}
        cells={names[n]:c for n,c in dict(upper=(1,5),lower=(3,2),bridge=(2,4),bottom=(4,1)).items()}
        pl,comps,_,index,plan=installed(renamed,cells)
        for mirror in (False,True):
            view=deepcopy(pl)
            if mirror:
                orient=[integration._mirror_orientation(pl,i) for i in range(len(comps))]
                view.set_state([(r,pl.cols-1-c) for r,c in pl.cell_of],orient)
            trials=[p for p,_ in conduction_trials(view,comps,index,plan) if p.valid_state()]
            self.assertTrue(trials)
            self.assertTrue(any(integration.measure_attachments(p,comps,index,plan)['pin_axis_error']<1e-8 for p in trials))


if __name__=='__main__':
    unittest.main()
