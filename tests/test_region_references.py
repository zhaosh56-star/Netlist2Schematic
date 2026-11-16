"""Common-reference roles organize comparisons without fusing their owners."""
from copy import deepcopy
from unittest import TestCase

from netlist2schematic.plugins.region.region_layout import build_region_plan, measure_regions
from netlist2schematic.plugins.region.region_references import reference_lane_hints, measure_reference_access
from netlist2schematic.plugins.region.region_route_ports import paired_control_relations
from netlist2schematic.plugins.region.region_placement import _lanes
from netlist2schematic.netlist.netlist_core import to_document
from tests.test_region_layout import ota, renamed, Position, positions_for


def detector(plan):
    return next(r for r in plan['regions'] if r['kind']=='feedback_detector')


def semantics(plan,inverse=None):
    inverse=inverse or {}
    return sorted(tuple(sorted((inverse.get(b['sensor'],b['sensor']),
                                inverse.get(b['reference'],b['reference']),
                                inverse.get(b['feed'],b['feed'])) for b in r['comparisons']))
                  for region in plan['regions'] for r in region.get('reference_access',()))


class ReferenceAccessTests(TestCase):
    def test_full_detector_retains_reference_roles_and_original_local_comparisons(self):
        devices=ota();before=deepcopy(devices);plan=build_region_plan(devices);region=detector(plan)
        roles=region['reference_access'][0]
        self.assertEqual({b['reference'] for b in roles['comparisons']},{'ra','rb'})
        self.assertEqual({b['sensor'] for b in roles['comparisons']},{'ca','cb'})
        self.assertEqual({b['feed'] for b in roles['comparisons']},{'fa','fb'})
        self.assertEqual({frozenset(p) for p in region['pairs']},
                         {frozenset(('ca','ra')),frozenset(('cb','rb'))})
        self.assertNotIn(frozenset(('ra','rb')),{frozenset(p) for p in region['pairs']})
        self.assertEqual(len(plan['ownership']),21)
        self.assertEqual(devices,before)

    def test_shared_reference_is_not_inferred_when_complete_feedback_evidence_is_missing(self):
        for devices in (ota(False),{k:v for k,v in ota().items() if k!='dr'}):
            plan=build_region_plan(devices)
            self.assertFalse(any(r.get('reference_access') for r in plan['regions']))

    def test_role_semantics_survive_renaming_source_reorder_and_polarity_dual(self):
        old=build_region_plan(ota())
        for dual in (False,True):
            changed,inverse=renamed(ota(),dual=dual)
            self.assertEqual(semantics(old),semantics(build_region_plan(changed),inverse))

    def test_references_seed_between_sensors_for_both_local_mirrors(self):
        devices=ota();plan=build_region_plan(devices);region=detector(plan)
        document=to_document(devices);c=document['components'];index={r['id']:i for i,r in enumerate(c)}
        rows={index[n]:(0 if n in ('fa','fb') else 2 if n in ('ds','dr') else 1) for n in region['members']}
        for sign in (1,-1):
            lanes,width=_lanes(region,rows,index,[],sign)
            bounds=sorted(lanes[index[n]] for n in ('ca','cb'))
            self.assertTrue(all(bounds[0]<lanes[index[n]]<bounds[1] for n in ('ra','rb')))
            for pair in region['pairs']:
                self.assertEqual(abs(lanes[index[pair[0]]]-lanes[index[pair[1]]]),1)

    def test_inside_measure_is_mirror_and_translation_invariant_and_uses_real_pins(self):
        devices=ota();plan=build_region_plan(devices);region=detector(plan)
        names=list(devices);index={n:i for i,n in enumerate(names)}
        points=positions_for(devices)
        points.update(ca=(0,200),ra=(100,200),rb=(200,200),cb=(300,200))
        for mirrored,shift in ((False,0),(True,400)):
            moved={n:(x+shift,y-70) for n,(x,y) in points.items()}
            p=Position(names,moved,mirrored=mirrored)
            m=measure_reference_access(p,index,[region])
            self.assertEqual(m['inside_errors'],0)
            self.assertEqual(m['records'][0]['reference_span'],100)
        p=Position(names,points,pin_offsets={('ra','G'):(250,0)})
        m=measure_reference_access(p,index,[region])
        self.assertEqual(m['inside_errors'],1)

    def test_outer_reference_is_a_discrete_order_error_not_a_missing_relation(self):
        devices=ota();plan=build_region_plan(devices);names=list(devices);index={n:i for i,n in enumerate(names)}
        points=positions_for(devices)
        points.update(ra=(0,200),ca=(100,200),rb=(200,200),cb=(300,200))
        p=Position(names,points)
        good=measure_regions(p,[{'id':n} for n in names],index,plan)
        legacy=deepcopy(plan)
        for region in legacy['regions']:region.pop('reference_access',None)
        old=measure_regions(p,[{'id':n} for n in names],index,legacy)
        self.assertEqual(good['order_errors'],old['order_errors']+1)
        self.assertEqual(good['reference_access']['inside_errors'],1)

    def test_missing_roles_or_nonfinite_points_are_unknown_not_a_zero_error(self):
        devices=ota();plan=build_region_plan(devices);region=detector(plan);names=list(devices)
        index={n:i for i,n in enumerate(names)};points=positions_for(devices)
        for mode in ('missing_feed','invalid_role','nonfinite'):
            changed=deepcopy(region);indices=dict(index);moved=deepcopy(points)
            if mode=='missing_feed':indices.pop('fa')
            elif mode=='invalid_role':changed['reference_access'][0]['comparisons'][0]['reference']='ca'
            p=Position(names,moved)
            if mode=='nonfinite':p.points['ra']=(float('nan'),200)
            result=measure_reference_access(p,indices,[changed])
            self.assertFalse(result['measurable']);self.assertIsNone(result['inside_errors'])
            self.assertTrue(result['missing'])

    def test_common_reference_controls_are_optional_interface_not_a_new_hard_pair(self):
        devices=ota();plan=build_region_plan(devices);doc=to_document(devices)
        c,n=doc['components'],doc['nets'];index={r['id']:i for i,r in enumerate(c)}
        evidence=paired_control_relations(c,n,index,plan)
        refs=next(r for r in evidence['relations'] if set(r['members'])=={'ra','rb'})
        self.assertEqual(refs['evidence'],'complete_common_reference_role_and_exact_two_control_terminals')
        legacy=deepcopy(plan)
        for region in legacy['regions']:region.pop('shared_control_pairs',None)
        self.assertFalse(any(set(r['members'])=={'ra','rb'}
            for r in paired_control_relations(c,n,index,legacy)['relations']))

    def test_legacy_and_nonfeedback_regions_explicitly_have_no_applicable_relation(self):
        plan=build_region_plan(ota(False));names=list(ota(False));index={n:i for i,n in enumerate(names)}
        result=measure_reference_access(Position(names,positions_for(ota(False))),index,plan['regions'])
        self.assertEqual(result['relation_count'],0)
        self.assertFalse(result['measurable']);self.assertIsNone(result['inside_errors'])
