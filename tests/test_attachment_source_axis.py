"""After-SA tail centering uses the real source pole and bounded dx."""
from copy import deepcopy
import time
import unittest

from netlist2schematic.plugins.attachment.attachment_sources import source_axis_trials, source_group_anchor
from netlist2schematic.plugins.attachment.attachment_integration import _mirror_orientation
from tests.test_attachment_sources import fixture, tail_devices, freeze


class SourceAxisTests(unittest.TestCase):
    def make(self, reverse=False):
        pl,comps,nets,index,model=fixture(tail_devices(reverse),
            dict(a=(1,2),b=(1,5),bias=(3,4),load=(0,0)))
        if reverse:
            pl.orient[index['bias']]=2
        return pl,comps,nets,index,model

    def test_real_source_poles_are_centered_without_changing_cells_or_orientation(self):
        for reverse in (False,True):
            pl,comps,_,index,model=self.make(reverse)
            before=freeze(pl)
            trial,action=next(source_axis_trials(pl,comps,index,model))
            group=model['groups'][0]
            anchor=source_group_anchor(trial,comps,index,group)
            self.assertEqual(trial.pin_world(index['bias'],group['signal_pin'])[0],anchor['point'][0])
            self.assertEqual(action['pin'],'-' if reverse else '+')
            self.assertEqual((trial.cell_of,trial.orient,trial.column_x,trial.row_y),
                             (pl.cell_of,pl.orient,pl.column_x,pl.row_y))
            self.assertEqual(freeze(pl),before)
            self.assertEqual([trial.offset_x[i] for n,i in index.items() if n!='bias'],[0.0,0.0,0.0])

    def test_mirror_keeps_exact_tail_axis_feasible(self):
        pl,comps,_,index,model=self.make()
        orient=[_mirror_orientation(pl,i) for i in range(len(comps))]
        pl.set_state([(r,pl.cols-1-c) for r,c in pl.cell_of],orient)
        trial,_=next(source_axis_trials(pl,comps,index,model))
        anchor=source_group_anchor(trial,comps,index,model['groups'][0])
        self.assertEqual(trial.pin_world(index['bias'],'+')[0],anchor['point'][0])

    def test_fixed_wrong_poles_ambiguous_host_and_expired_budget_are_noops(self):
        pl,comps,_,index,model=self.make()
        pl.fixed.add(index['bias'])
        self.assertEqual(list(source_axis_trials(pl,comps,index,model)),[])
        pl,comps,_,index,model=self.make()
        self.assertEqual(list(source_axis_trials(pl,comps,index,model,deadline=time.perf_counter()-1)),[])
        stale=deepcopy(comps);stale[index['bias']]['nodes']['+']='wrong'
        self.assertEqual(list(source_axis_trials(pl,stale,index,model)),[])
        neutral=deepcopy(model);neutral['groups'][0]['kind']='neutral_junction_source'
        self.assertEqual(list(source_axis_trials(pl,comps,index,neutral)),[])

    def test_unreachable_axis_is_only_approached_within_the_existing_cell(self):
        pl,comps,_,index,model=self.make()
        cells=list(pl.cell_of);cells[index['bias']]=(3,0);pl.set_state(cells,pl.orient)
        trial,_=next(source_axis_trials(pl,comps,index,model,grid_step=8))
        self.assertEqual(trial.cell_of,pl.cell_of)
        value=trial.offset_x[index['bias']]
        self.assertLessEqual(abs(value),60)
        self.assertEqual(value%8,0)

    def test_bipolar_tail_uses_actual_emitter_terminals(self):
        devices=tail_devices()
        for name in ('a','b'):
            devices[name]=dict(cls='BJT',type='NPN',c='out_'+name,b='input_'+name,e='tail',model='NPN')
        pl,comps,_,index,model=fixture(devices,dict(a=(1,2),b=(1,5),bias=(3,4),load=(0,0)))
        trial,action=next(source_axis_trials(pl,comps,index,model))
        target=sum(trial.pin_world(index[n],'E')[0] for n in ('a','b'))/2
        self.assertEqual(trial.pin_world(index['bias'],action['pin'])[0],target)


if __name__=='__main__':
    unittest.main()
