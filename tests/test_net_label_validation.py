"""Independent checks must reject invalid or misleading net-label displays."""
from copy import deepcopy
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from routing_rules import Pin, Scene, canonical_topology
from net_label_core import build_view
from net_label_validation import display_routes, validate_view
from sa_render import _text_box


def source_fixture():
    first = Pin('a',0,'P',(0,0),(20,0),(1,0))
    last = Pin('a',1,'P',(1000,0),(980,0),(-1,0))
    scene = Scene(pins={'a':[first,last]}, all_pins={'a':[first.point,last.point]},
                  fixed_segments={'a':[first.stub,last.stub]})
    routed = [('a',[((0,0),(1000,0))])]
    terminals = {'a':[{'component':'U0','pin':'P','point':first.point},
                      {'component':'U1','pin':'P','point':last.point}]}
    return scene,routed,terminals


class NetLabelValidationTests(unittest.TestCase):
    def setUp(self):
        self.scene,self.original,self.terminals = source_fixture()
        self.view = build_view(self.scene,self.original,scale=100,terminal_ids=self.terminals,seconds=5)
        self.assertEqual(2,len(self.view['labels']), 'fixture needs a valid independently checked split')

    def check(self, view=None, **kwargs):
        return validate_view(self.scene,self.original,self.view if view is None else view,
                             self.terminals,**kwargs)

    def rejected(self, change):
        view = deepcopy(self.view)
        change(view)
        with self.assertRaises(ValueError):
            self.check(view)

    def test_valid_label_equivalence_restores_exact_original_terminal_group(self):
        result = self.check()
        self.assertTrue(result['valid'])
        self.assertEqual(2,len(result['groups']))
        self.assertLess(result['metrics']['wirelength'],result['original_metrics']['wirelength'])

    def test_missing_label_is_rejected_even_when_saved_success_is_true(self):
        def alter(view):
            view['labels'].pop()
            view['validation'] = {'valid':True}
        self.rejected(alter)

    def test_duplicate_label_for_one_island_is_rejected(self):
        self.rejected(lambda view:view['labels'].append(deepcopy(view['labels'][0])))

    def test_wrong_display_name_cannot_claim_original_net_identity(self):
        self.rejected(lambda view:view['labels'][0].update(text='other_net'))

    def test_foreign_internal_net_is_rejected(self):
        self.rejected(lambda view:view['labels'][0].update(net='other_net'))

    def test_unknown_group_reference_is_rejected(self):
        self.rejected(lambda view:view['labels'][0].update(group_id='invented'))

    def test_missing_group_is_rejected(self):
        self.rejected(lambda view:view['groups'].pop())

    def test_wrong_terminal_metadata_is_rejected(self):
        self.rejected(lambda view:view['groups'][0].update(terminals=[['invented','P']]))

    def test_duplicate_terminal_across_groups_is_rejected(self):
        def alter(view):
            view['groups'][1]['terminals'] = deepcopy(view['groups'][0]['terminals'])
        self.rejected(alter)

    def test_fixed_exit_cannot_be_removed_and_hidden_by_a_label(self):
        def alter(view):
            view['base_routes']['a'] = []
            view['validation'] = {'valid':True}
            view['display_metrics'] = {'wirelength':0,'crossings':0}
        self.rejected(alter)

    def test_new_wire_not_present_in_source_is_rejected(self):
        self.rejected(lambda view:view['base_routes']['a'].append(((200,200),(300,200))))

    def test_unknown_routed_net_is_rejected(self):
        self.rejected(lambda view:view['base_routes'].update(extra=[((200,200),(300,200))]))

    def test_missing_omission_record_is_rejected(self):
        self.rejected(lambda view:view.update(omitted_paths=[]))

    def test_fabricated_omission_outside_source_is_rejected(self):
        def alter(view):
            view['omitted_paths'][0]['path'] = [(0,200),(1000,200)]
        self.rejected(alter)

    def test_floating_label_anchor_is_rejected(self):
        self.rejected(lambda view:view['labels'][0].update(anchor=[100,100]))

    def test_non_orthogonal_label_leader_is_rejected(self):
        def alter(view):
            anchor = view['labels'][0]['anchor']
            view['labels'][0]['tip'] = [anchor[0]+24,anchor[1]+24]
        self.rejected(alter)

    def test_invisible_or_forged_text_bounds_are_rejected(self):
        self.rejected(lambda view:view['labels'][0].update(bounds=[0,0,0,0]))

    def test_non_finite_coordinates_are_rejected(self):
        self.rejected(lambda view:view['labels'][0].update(x=float('nan')))

    def test_unknown_view_version_is_rejected(self):
        self.rejected(lambda view:view.update(version='unknown-future-format'))

    def test_invalid_label_collection_is_rejected(self):
        self.rejected(lambda view:view.update(labels=None))

    def test_invalid_base_collection_is_rejected(self):
        self.rejected(lambda view:view.update(base_routes=None))

    def test_local_two_pitch_relationship_cannot_be_split(self):
        self.rejected(lambda view:view.update(scale=1000))

    def test_group_identity_cannot_be_reused_for_another_island(self):
        def alter(view):
            view['groups'][1]['id'] = view['groups'][0]['id']
        self.rejected(alter)

    def test_blank_original_net_label_is_rejected(self):
        self.rejected(lambda view:view['labels'][0].update(text=''))

    def test_text_touching_preexisting_text_is_rejected(self):
        with self.assertRaises(ValueError):
            self.check(text_obstacles=[self.view['labels'][0]['bounds']])

    def test_label_near_device_protection_zone_is_rejected(self):
        self.scene.body_rects.append((99,tuple(self.view['labels'][0]['bounds'])))
        with self.assertRaises(ValueError):
            self.check()

    def test_label_near_supply_marker_is_rejected(self):
        self.scene.marker_rects.append(('vdd',tuple(self.view['labels'][0]['bounds'])))
        with self.assertRaises(ValueError):
            self.check()

    def test_actual_visible_metrics_are_recomputed(self):
        view = deepcopy(self.view)
        view['display_metrics'] = {'wirelength':-1,'crossings':-1,'bends':-1}
        view['validation'] = {'valid':False}
        result = self.check(view)
        self.assertTrue(result['valid'])
        self.assertGreater(result['metrics']['wirelength'],0)
        self.assertGreaterEqual(result['metrics']['crossings'],0)

    def test_display_routes_include_the_physical_label_leaders(self):
        runs = canonical_topology(dict(display_routes(self.view))['a'])['runs']
        from routing_rules import on_segment
        for label in self.view['labels']:
            anchor,tip = tuple(label['anchor']),tuple(label['tip'])
            self.assertTrue(any(on_segment(anchor,a,b) and on_segment(tip,a,b) for a,b in runs))

    def test_validation_deadline_is_enforced(self):
        with self.assertRaises(ValueError):
            self.check(deadline=time.perf_counter()-1)

    def test_new_cut_endpoint_near_a_foreign_crossing_is_rejected(self):
        original = deepcopy(self.original)
        original.append(('b',[((500,-100),(500,100))]))
        first,last = Pin('b',2,'P',(500,-100),(500,-80),(0,1)),Pin('b',3,'P',(500,100),(500,80),(0,-1))
        self.scene.pins['b'] = [first,last]
        self.scene.all_pins['b'] = [first.point,last.point]
        self.scene.fixed_segments['b'] = [first.stub,last.stub]
        self.terminals['b'] = [{'component':'U2','pin':'P','point':first.point},
                               {'component':'U3','pin':'P','point':last.point}]
        view = deepcopy(self.view)
        view['base_routes']['b'] = deepcopy(original[-1][1])
        view['base_routes']['a'] = [((0,0),(495,0)),((520,0),(1000,0))]
        view['omitted_paths'][0]['path'] = [(495,0),(520,0)]
        for label in view['labels']:
            anchor = (495,0) if label['anchor'][0] < 500 else (520,0)
            tip = (anchor[0],24)
            label.update(anchor=anchor,tip=tip,x=tip[0]+8,y=tip[1]+4,anchor_style='start')
            label['bounds'] = _text_box(label['x'],label['y'],label['text'],'start',14)
        with self.assertRaises(ValueError):
            validate_view(self.scene,original,view,self.terminals)


if __name__ == '__main__':
    unittest.main()
