"""Row plans are applied before SA, with the original reference untouched."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from netlist2schematic.layout import structured_placement as structured
from netlist2schematic.recognition.device_leveling import compute_levels
from netlist2schematic.netlist.netlist_core import parse_netlist

LNA='''
.model N nmos
L1 low high 1n
L2 bottom 0 1n
L3 vdd top 1n
M1 low vin bottom bottom N
M2 top bias high high N
'''


class BranchRowIntegrationTests(unittest.TestCase):
    def candidates(self,text,**kwargs):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'test.cir';source.write_text(text,encoding='utf8')
            levels=compute_levels(parse_netlist(source))
            items=list(structured.create_candidates(source,iterations=120,seed=71,**kwargs))
        return items,levels

    def test_rl_continuation_and_gap_precede_annealing(self):
        items,original=self.candidates(LNA,structure_layout='auto',structure_phase=2)
        reference=items[0]
        self.assertNotIn('branch_row_plan',reference[4])
        for pl,comps,nets,index,meta in items[1:]:
            rows=[r for r,c in pl.cell_of]
            self.assertLess(rows[index['M2']],rows[index['M1']])
            self.assertEqual(original['device_levels'],meta['levels'])
            plan=meta['branch_row_plan']
            self.assertLess(plan['rows']['M2'],plan['attachment_rows']['L1'])
            self.assertLess(plan['attachment_rows']['L1'],plan['rows']['M1'])
            self.assertEqual(2,meta['structure_layout']['phase'])
            self.assertTrue(pl.valid_state())
            for order in plan['strict_orders']:
                self.assertLess(rows[index[order['upper']]],rows[index[order['lower']]])

    def test_phase_one_remains_replayable_and_does_not_modify_rows(self):
        off,_=self.candidates(LNA,structure_layout='off')
        first,_=self.candidates(LNA,structure_layout='auto',structure_phase=1)
        for a,b in zip(off,first):
            self.assertEqual([r for r,c in a[0].cell_of],[r for r,c in b[0].cell_of])
            self.assertNotIn('branch_row_plan',b[4])

    def test_off_never_invokes_branch_solver(self):
        with patch('netlist2schematic.plugins.layout_shared.branch_levels.plan_branch_rows',side_effect=AssertionError('off branch solver')):
            self.candidates(LNA,structure_layout='off')

    def test_original_reference_matches_all_rollout_phases(self):
        off,_=self.candidates(LNA,structure_layout='off')
        for phase in (1,2,3):
            on,_=self.candidates(LNA,structure_layout='auto',structure_phase=phase)
            self.assertEqual(off[0][0].cell_of,on[0][0].cell_of)
            self.assertEqual(off[0][0].orient,on[0][0].orient)
            self.assertEqual(off[0][4],on[0][4])


if __name__=='__main__':unittest.main()
