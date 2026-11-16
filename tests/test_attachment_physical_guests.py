"""Floating guests are measured/protected by actual position, not stale slots."""
from copy import deepcopy
import unittest

from tests.test_attachment_integration import bridge_fixture
from netlist2schematic.plugins.attachment import attachment_integration as ai
from netlist2schematic.plugins.attachment.attachment_physical import physical_member_in_band


def fixture():
    _, old, comps, nets, index, plan = bridge_fixture()
    pl = ai._install(old, comps, index, plan)
    group = next(g for g in plan['groups'] if g.get('requires_interior'))
    return pl, comps, nets, index, plan, group


def place(pl, index, name, point, orientation=None):
    i = index[name]
    offsets = list(getattr(pl, 'passive_local_offsets', [(0,0)]*len(pl.comps)))
    old = offsets[i]
    x,y = pl.pos_of(i)
    offsets[i] = point[0]-x+old[0], point[1]-y+old[1]
    pl.passive_local_offsets = offsets
    if orientation is not None:
        pl.orient[i] = orientation


class PhysicalGuestTests(unittest.TestCase):
    def test_proven_bridge_fits_from_different_reference_rows(self):
        pl, comps, _, index, _, group = fixture()
        row_y = pl.row_y[pl.cell_of[index['a']][0]]
        place(pl,index,'r',(350,row_y),3)
        place(pl,index,'c',(600,row_y),3)
        self.assertEqual({'r','c'}, set(ai._embedded(pl,group,index)))
        self.assertTrue(pl.blocks_contiguous())
        self.assertTrue(pl.valid_state())
        self.assertEqual(0, pl.cell_of[index['r']][0])

    def test_unrelated_physical_intrusion_is_rejected_despite_outside_slot(self):
        pl, _, _, index, _, _ = fixture()
        y = pl.row_y[pl.cell_of[index['a']][0]]
        place(pl,index,'outsider',(450,y),1)
        self.assertEqual(2, pl.cell_of[index['outsider']][0])
        self.assertFalse(pl.blocks_contiguous())
        self.assertFalse(pl.valid_state())

    def test_old_inside_slot_does_not_reserve_a_physical_body_that_moved_away(self):
        pl, _, _, index, _, _ = fixture()
        cells = list(pl.cell_of);cells[index['outsider']] = (1,3)
        pl.set_state(cells,pl.orient)
        self.assertFalse(pl.blocks_contiguous())
        place(pl,index,'outsider',(1100,800),1)
        self.assertTrue(pl.blocks_contiguous())
        self.assertTrue(pl.valid_state())

    def test_valid_reference_row_cannot_hide_an_out_of_band_actual_position(self):
        pl, _, _, index, _, group = fixture()
        cells = list(pl.cell_of)
        cells[index['r']] = (1,3);cells[index['c']] = (1,4)
        pl.set_state(cells,pl.orient)
        pl.orient[index['r']] = pl.orient[index['c']] = 3
        self.assertEqual({'r','c'}, set(ai._embedded(pl,group,index)))
        pl.attachment_required_embeddings = {group['id']: ('r','c')}
        place(pl,index,'r',(440,500))
        self.assertEqual([], ai._embedded(pl,group,index))
        self.assertFalse(pl.valid_state())

    def test_multirow_parent_does_not_create_a_large_physical_exclusion_rectangle(self):
        pl, _, _, index, _, _ = fixture()
        pl.layout_macro_blocks = [[index[n] for n in ('a','b','outsider')]]
        place(pl,index,'outsider',(450,pl.row_y[2]),1)
        self.assertTrue(pl.blocks_contiguous())

    def test_float_and_grid_branch_use_identical_membership_at_same_world_point(self):
        pl, _, _, index, _, group = fixture()
        cells = list(pl.cell_of);cells[index['r']]=(1,3);cells[index['c']]=(1,4)
        pl.set_state(cells,pl.orient)
        pl.orient[index['r']] = pl.orient[index['c']] = 3
        old = ai._embedded(pl,group,index)
        moved = deepcopy(pl)
        points = {n: pl.pos_of(index[n]) for n in ('r','c')}
        cells=list(moved.cell_of);cells[index['r']]=(0,0);cells[index['c']]=(0,7)
        moved.set_state(cells,moved.orient)
        for n,p in points.items():place(moved,index,n,p)
        self.assertEqual(old,ai._embedded(moved,group,index))

    def test_translation_uses_translated_corridors(self):
        pl, _, _, index, plan, group = fixture()
        band = ai._role_band(pl,group,index,plan)
        place(pl,index,'r',(350,pl.row_y[1]),3)
        self.assertTrue(physical_member_in_band(pl,index['r'],band))
        pl.column_x=[v+37 for v in pl.column_x]
        pl.row_y=[v-53 for v in pl.row_y]
        translated=ai._role_band(pl,group,index,plan)
        self.assertTrue(physical_member_in_band(pl,index['r'],translated))


if __name__=='__main__':unittest.main()
