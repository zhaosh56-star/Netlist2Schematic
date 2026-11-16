"""Adjacent proven port attachments need no full empty column of isolation."""
from copy import deepcopy
import unittest

from netlist2schematic.plugins.region.region_placement import _leaves, _region_gap, _lanes
from tests.test_region_placement import region_seed
from tests.test_region_layout import comparator, ota, renamed


class RegionSpacingTests(unittest.TestCase):
    def test_comparator_access_leaves_abut_core_with_rows_and_pins_intact(self):
        pl,comps,nets,index,plan,_,_,_,rows=region_seed(comparator)
        core=next(r for r in _leaves(plan) if r['kind']=='comparator_core')
        wings=[r for r in _leaves(plan) if r['kind']=='clocked_rail_attachments']
        self.assertEqual(pl.cols,6)
        for wing in wings:
            self.assertEqual(_region_gap(wing,core,comps,index,nets),0)
            self.assertEqual(_region_gap(core,wing,comps,index,nets),0)
            # A supply role survives arbitrary net renaming, even if it is
            # not a built-in VDD/GND alias.
            self.assertEqual(_region_gap(wing,core,comps,index,nets,
                {a['net'] for a in wing['attachments']}),1)
        self.assertEqual(pl.row_of,rows)
        self.assertEqual(len(set(pl.cell_of)),len(comps))
        self.assertTrue(pl.blocks_contiguous())

    def test_independent_folded_input_output_and_detector_keep_channel(self):
        _,comps,nets,index,plan,_,_,_,_=region_seed(ota)
        leaves=_leaves(plan)
        for a in leaves:
            for b in leaves:
                if a['id']!=b['id']:
                    self.assertEqual(_region_gap(a,b,comps,index,nets),1)

    def test_shared_clock_stale_target_missing_member_and_supply_do_not_remove_gap(self):
        _,comps,nets,index,plan,_,_,_,_=region_seed(comparator)
        core=next(r for r in _leaves(plan) if r['kind']=='comparator_core')
        wing=next(r for r in _leaves(plan) if r['kind']=='clocked_rail_attachments')
        for mode in ('clock','stale','missing','supply','parent'):
            wrong=deepcopy(wing)
            if mode=='clock':
                for a in wrong['attachments']:a.update(pin='G',net='clock')
            elif mode=='stale':wrong['attachments'][0]['net']='unknown'
            elif mode=='missing':wrong['attachments']=wrong['attachments'][:1]
            elif mode=='supply':wrong['attachments'][0].update(pin='S',net='hi')
            else:wrong['parent']='other_parent'
            self.assertEqual(_region_gap(wrong,core,comps,index,nets),1)

    def test_renaming_source_order_and_polarity_dual_keep_only_needed_leaf_widths(self):
        for dual in (False,True):
            devices,_=renamed(comparator(),dual)
            pl,comps,nets,index,plan,_,_,_,rows=region_seed(lambda:devices)
            width=sum(_lanes(leaf,rows,index,plan['series_axes'],1)[1] for leaf in _leaves(plan))
            self.assertEqual(pl.cols,width)
            self.assertEqual(pl.row_of,rows)
            self.assertTrue(pl.blocks_contiguous())
            self.assertTrue(all(g['columns']==0 for g in pl.region_initialization['region_gaps']))


if __name__=='__main__':
    unittest.main()
