"""Module-internal compaction keeps the routing contract and a bounded fallback."""
from copy import deepcopy
from math import hypot
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from tests.test_passive_modules import fixture, nested
from tests.test_passive_placement import mos, resistor
from tests import test_module_packing_recovery as reuse_tests
from tests.test_series_alignment import PAR
from netlist2schematic.plugins.passive_skeleton import placement as packing
from netlist2schematic.plugins.passive_skeleton.module_geometry import template, can_compact
from netlist2schematic.plugins.passive_skeleton.module_connections import check_guides
from netlist2schematic.plugins.passive_skeleton.plan import measure
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.routing.sa_routing import final_route, validate_final_routes


def series():
    return dict(a=mos('left','ga'), b=mos('right','gb'),
                r=resistor('left','join'), c=resistor('join','right','C'))


def parallel():
    return dict(a=mos('left','ga'), b=mos('right','gb'),
                r=resistor('left','right'), c=resistor('left','right','C'),
                l=resistor('left','right','L'))


def layouts(devices, direction=(1,0), options=None, **kwargs):
    options=options or RoutingOptions()
    _,core,cs,ns,ix,plan=fixture(devices)
    frame=packing.make_frame(PAR,core,cs,ix,options)
    group=plan['groups'][0]
    values={p:template(PAR,frame,cs,ix,group['module'],direction,options,spacing=p,**kwargs)
            for p in ('compact','standard')}
    return frame,cs,ns,ix,plan,values


class CompactModuleGeometryTests(unittest.TestCase):
    def test_series_only_removes_redundant_join_padding(self):
        _,_,_,ix,_,values=layouts(series())
        def distance(geo):
            a,b=(geo['positions'][ix[n]] for n in ('r','c'))
            return hypot(a[0]-b[0],a[1]-b[1])
        self.assertEqual(distance(values['standard']),80.)
        self.assertEqual(distance(values['compact']),72.)
        self.assertEqual(values['standard']['extent']-values['compact']['extent'],8.)
        self.assertEqual(values['compact']['positions'][ix['r']],values['standard']['positions'][ix['r']])
        self.assertTrue(values['compact']['spacing']['changed'])

    def test_parallel_uses_adjacent_widths_instead_of_largest_uniform_pitch(self):
        frame,cs,_,ix,plan,_=layouts(parallel(),direction=(0,1))
        tree=deepcopy(plan['groups'][0]['module'])
        tree['children'].sort(key=lambda c:('r','c','l').index(c['device']))
        values={p:template(PAR,frame,cs,ix,tree,(0,1),RoutingOptions(),spacing=p)
                for p in ('compact','standard')}
        compact=[values['compact']['positions'][ix[n]][0] for n in ('r','c','l')]
        standard=[values['standard']['positions'][ix[n]][0] for n in ('r','c','l')]
        self.assertEqual([b-a for a,b in zip(standard,standard[1:])],[68.,68.])
        self.assertEqual([b-a for a,b in zip(compact,compact[1:])],[48.,52.])
        self.assertEqual(values['compact']['extent'],values['standard']['extent'])
        self.assertLess(values['compact']['envelope'][2]-values['compact']['envelope'][0],
                        values['standard']['envelope'][2]-values['standard']['envelope'][0])

    def test_all_directions_and_nested_connections_pass_original_internal_checks(self):
        for devices in (series(),parallel(),nested()):
            for direction in ((1,0),(-1,0),(0,1),(0,-1)):
                with self.subTest(direction=direction,count=len(devices)):
                    frame,cs,ns,ix,plan,values=layouts(devices,direction)
                    group=plan['groups'][0]
                    geo=packing._geometry(PAR,frame,cs,ix,group,direction,RoutingOptions())
                    pl=packing._translated(frame,geo,(2000.,2000.),2.)
                    self.assertEqual(check_guides(PAR,pl,ns,ix,RoutingOptions()),[])
                    self.assertEqual(set(values['compact']['positions']),set(values['standard']['positions']))
                    self.assertLessEqual(values['compact']['extent'],values['standard']['extent'])

    def test_custom_halo_grid_and_keepout_do_not_lower_the_contract(self):
        options=RoutingOptions(halo=16,grid_step=4,cross_clearance=20,edge_clearance=10)
        for devices in (series(),parallel(),nested()):
            frame,cs,ns,ix,plan,_=layouts(devices,options=options)
            geo=packing._geometry(PAR,frame,cs,ix,plan['groups'][0],(1,0),options)
            pl=packing._translated(frame,geo,(2000.,2000.),options.grid_step)
            self.assertEqual(check_guides(PAR,pl,ns,ix,options),[])
        _,_,_,ix,_,values=layouts(series(),extra_keepout=12)
        self.assertEqual(abs(values['compact']['positions'][ix['r']][0]-
                             values['compact']['positions'][ix['c']][0]),96.)

    def test_source_interfaces_and_composite_series_buses_keep_their_padding(self):
        frame,cs,ns,ix,plan,_=layouts(nested())
        source=dict(id='source',symbol='voltage-source',nodes={'1':'a','2':'b'})
        cs=cs+[source];ix={c['id']:i for i,c in enumerate(cs)}
        def part(n):
            return dict(kind='part',device=n,ends=['a','b'],pins={'1':'a','2':'b'})
        for kind in ('series','parallel'):
            tree=dict(kind=kind,children=[part('r'),part('source')])
            self.assertFalse(can_compact(tree,cs,ix))
        # The series-to-parallel interface itself is left alone; compaction
        # inside a passive child remains allowed and is checked independently.
        compact=template(PAR,frame,frame.comps,{c['id']:i for i,c in enumerate(frame.comps)},
            plan['groups'][0]['module'],(1,0),RoutingOptions())
        self.assertTrue(all(set(a['devices'])=={'r2','l'} for a in compact['spacing']['actions']
                            if a['kind']=='series_join'))

    def test_parallel_reversal_preserves_mirrored_relative_geometry(self):
        frame,cs,_,ix,plan,values=layouts(parallel(),direction=(0,1))
        tree=deepcopy(plan['groups'][0]['module']);tree['children'].reverse()
        reflected=template(PAR,frame,cs,ix,tree,(0,1),RoutingOptions())
        for name in ('r','c','l'):
            x,y=values['compact']['positions'][ix[name]]
            self.assertEqual(reflected['positions'][ix[name]],(-x,y))

    def test_invalid_profile_is_rejected(self):
        frame,cs,_,ix,plan,_=layouts(series())
        with self.assertRaises(ValueError):
            template(PAR,frame,cs,ix,plan['groups'][0]['module'],(1,0),RoutingOptions(),spacing='tiny')

    def test_instance_net_renaming_and_component_reorder_preserve_template(self):
        frame,cs,_,ix,plan,values=layouts(nested())
        names={c['id']:'unit'+str(k) for k,c in enumerate(reversed(cs))}
        changed=[{**c,'id':names[c['id']], 'nodes':{p:'n_'+n for p,n in c['nodes'].items()}}
                 for c in reversed(cs)]
        mapped={c['id']:i for i,c in enumerate(changed)}
        tree=deepcopy(plan['groups'][0]['module'])
        def rename(node):
            node['ends']=['n_'+n for n in node['ends']]
            if node['kind']=='part':
                node['device']=names[node['device']]
                node['pins']={p:'n_'+n for p,n in node['pins'].items()}
            else:
                for child in node['children']:rename(child)
        rename(tree)
        inverse={v:k for k,v in names.items()}
        state=SimpleNamespace(orient=[frame.orient[ix[inverse[c['id']]]] for c in changed],
            n_orients=lambda i:len(PAR.GEO_DOC[changed[i]['symbol']]['orientations']))
        result=template(PAR,state,changed,mapped,tree,(1,0),RoutingOptions())
        for name in plan['groups'][0]['members']:
            self.assertEqual(result['positions'][mapped[names[name]]],values['compact']['positions'][ix[name]])
        self.assertEqual(result['extent'],values['compact']['extent'])

    def test_translation_keeps_inductor_pins_and_wires_rigid_on_nondividing_grid(self):
        options=RoutingOptions(grid_step=4)
        frame,cs,ns,ix,plan,_=layouts(parallel(),options=options)
        geo=packing._geometry(PAR,frame,cs,ix,plan['groups'][0],(1,0),options)
        before=deepcopy(geo)
        pl=packing._translated(frame,geo,(2001.,1999.),options.grid_step)
        for i,point in geo[0].items():
            self.assertEqual(tuple(pl.pos_of(i)[k]-point[k] for k in (0,1)),(2000.,2000.))
        self.assertEqual(check_guides(PAR,pl,ns,ix,options),[])
        self.assertEqual(geo,before)


class CompactModulePackingTests(unittest.TestCase):
    def test_default_compact_result_keeps_active_cells_and_passes_real_routing(self):
        _,core,cs,ns,ix,plan=fixture()
        old_cells=list(core.cell_of);old_orientation=list(core.orient)
        pl,report=packing.pack(PAR,core,cs,ns,ix,plan,RoutingOptions())
        self.assertIsNotNone(pl,report)
        self.assertEqual(report['module_spacing']['selected'],'compact')
        self.assertEqual(len(report['module_spacing']['attempts']),1)
        for i,c in enumerate(core.comps):
            self.assertEqual(pl.cell_of[ix[c['id']]],old_cells[i])
            self.assertEqual(pl.orient[ix[c['id']]],old_orientation[i])
        routed,_,validation=final_route(PAR,ns,pl,ix)
        self.assertTrue(validation['valid'])
        self.assertTrue(validate_final_routes(PAR,ns,pl,ix,routed)[2]['valid'])
        self.assertEqual(plan.get('spacing_policy'),None)
        json.dumps(report,allow_nan=False)

    def test_static_compact_rejection_rebuilds_standard_from_original_core(self):
        _,core,cs,ns,ix,plan=fixture()
        original=(deepcopy(core.cell_of),deepcopy(core.column_x),deepcopy(core.row_y))
        from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
        def check(par,pl,*args):
            if pl.passive_spacing_policy=='compact':
                return dict(count=1,hard=set(),violations=[dict(kind='body')])
            return _static_faults(par,pl,*args)
        with patch('netlist2schematic.plugins.preflight.placement_preflight._static_faults',side_effect=check):
            pl,report=packing.pack(PAR,core,cs,ns,ix,plan,RoutingOptions(),seconds=2)
        self.assertIsNotNone(pl,report)
        self.assertEqual(report['module_spacing']['selected'],'standard')
        self.assertEqual([a['profile'] for a in report['module_spacing']['attempts']],['compact','standard'])
        self.assertLessEqual(report['trials'],128)
        self.assertEqual((core.cell_of,core.column_x,core.row_y),original)
        self.assertTrue(all(g['spacing_policy']=='standard' for g in report['plan']['groups']))
        self.assertTrue(final_route(PAR,ns,pl,ix)[2]['valid'])

    def test_profiles_share_one_deadline_and_trial_pool(self):
        _,core,cs,ns,ix,plan=fixture()
        clock=SimpleNamespace(now=100.)
        calls=[]
        def once(*args,**kwargs):
            calls.append(kwargs)
            clock.now+=kwargs['seconds']
            return None,dict(trials=kwargs['max_trials'],elapsed_seconds=kwargs['seconds'],
                stop_reason='local_budget',repeated_static_rejections_skipped=0)
        with patch.object(packing,'perf_counter',side_effect=lambda:clock.now), \
                patch.object(packing,'_pack_once',side_effect=once):
            pl,report=packing.pack(PAR,core,cs,ns,ix,plan,RoutingOptions(),seconds=4,
                                  deadline=102.,max_trials=20)
        self.assertIsNone(pl)
        self.assertEqual(len(calls),2)
        self.assertEqual(sum(c['max_trials'] for c in calls),20)
        self.assertEqual(sum(c['seconds'] for c in calls),2.)
        self.assertLessEqual(clock.now,102.)
        self.assertEqual(report['trials'],20)

    def test_duplicate_static_rejections_are_shared_across_profiles(self):
        case=reuse_tests.GeometryReuseTests()
        pl,report,checks,wires=case.execute(
            lambda *a:dict(count=1,hard=set(),violations=[dict(kind='body')]),lambda *a:[],spacing_policy='auto')
        self.assertIsNone(pl)
        self.assertEqual(checks,1);self.assertEqual(wires,0)
        self.assertEqual(report['repeated_static_rejections_skipped'],3)

    def test_standard_profile_can_reproduce_old_geometry_and_old_guides_stay_readable(self):
        _,core,cs,ns,ix,plan=fixture()
        plan['spacing_policy']='standard'
        pl,report=packing.pack(PAR,core,cs,ns,ix,plan,RoutingOptions())
        self.assertIsNotNone(pl)
        self.assertEqual(report['module_spacing']['selected'],'standard')
        before=measure(pl,cs,ix,report['plan'])
        old=deepcopy(pl)
        for guide in old.passive_module_guides.values():guide.pop('spacing',None)
        del old.passive_spacing_policy
        self.assertEqual(measure(old,cs,ix,report['plan']),before)
        self.assertEqual(old.cell_of,pl.cell_of)

    def test_single_part_does_not_lose_time_to_an_identical_compact_profile(self):
        devices=dict(a=mos('left','ga'),b=mos('right','gb'),r=resistor('left','right'))
        _,core,cs,ns,ix,plan=fixture(devices)
        pl,report=packing.pack(PAR,core,cs,ns,ix,plan,RoutingOptions())
        self.assertIsNotNone(pl)
        self.assertEqual([a['profile'] for a in report['module_spacing']['attempts']],['standard'])


if __name__=='__main__':unittest.main()
