"""Final verification cannot rely on optional placement preflight having run."""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest import TestCase
from unittest.mock import patch

from tests.test_placement_preflight import GridPlacement, PAR
from tests.test_power_stub_direction import source_case
from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
from netlist2schematic.rendering.render_clean import render
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.routing_rules import (RoutingOptions, Scene, analyze_routes, build_scene,
                                                     static_shape_conflicts)
from netlist2schematic.routing.sa_routing import RoutingValidationError, validate_final_routes


def marker_fixture(*, rename='', reverse=False, mirror=False, shift=(0, 0)):
    devices = {rename+'owner': dict(cls='PASSIVE', type='R', a='vdd', b='0'),
               rename+'source': dict(cls='ISRC', type='I', p=rename+'signal', n='0', value='1m')}
    if reverse:
        devices = dict(reversed(list(devices.items())))
    doc = to_document(devices)
    comps, nets = doc['components'], doc['nets']
    index = {c['id']: i for i, c in enumerate(comps)}
    positions = {rename+'owner': (100, 200), rename+'source': (150, 174)}
    positions = {name: ((-x if mirror else x)+shift[0], y+shift[1]) for name,(x,y) in positions.items()}
    xs = sorted({p[0] for p in positions.values()}); ys = sorted({p[1] for p in positions.values()})
    pl = GridPlacement.__new__(GridPlacement)
    pl.comps = comps
    pl.cell_of = [(ys.index(positions[c['id']][1]), xs.index(positions[c['id']][0])) for c in comps]
    pl.comp_at = {cell: i for i,cell in enumerate(pl.cell_of)}
    pl.row_of = [cell[0] for cell in pl.cell_of]
    pl.orient = [(3 if mirror else 1) if c['id']==rename+'owner' else 0 for c in comps]
    pl.column_x, pl.row_y, pl.offset_x = xs, ys, [0.0]*len(comps)
    pl.fixed, pl.fixed_cells, pl.allowed, pl.blocks = set(), {}, {}, []
    return pl, comps, nets, index


def signal_stubs(pl, nets, index, options=None):
    scene = build_scene(PAR, nets, pl, index, options=options)
    return scene, [(net, segs) for net, segs in scene.fixed_segments.items() if net not in scene.power_nets]


class FinalShapeValidationTests(TestCase):
    def test_real_default_marker_counterexample_is_rejected_by_final_verifier(self):
        pl, _, nets, index = marker_fixture()
        scene, routes = signal_stubs(pl, nets, index)
        # Wires alone are legal. Inflated source/marker shapes are not.
        self.assertEqual(analyze_routes(routes, scene)['violations'], [])
        old = deepcopy(pl.__dict__)
        with patch('netlist2schematic.routing.orthogonal_router._find_path', side_effect=AssertionError('Astar')):
            with self.assertRaises(RoutingValidationError) as error:
                validate_final_routes(PAR, nets, pl, index, routes)
        kinds = {v['kind'] for v in error.exception.diagnostics['violations']}
        self.assertEqual(kinds, {'body_overlap', 'marker_body_halo'})
        self.assertEqual(error.exception.diagnostics['body_violations'], 3)
        self.assertEqual(pl.__dict__, old)

    def test_preflight_and_final_shape_faults_share_identities_and_geometry(self):
        pl, _, nets, index = marker_fixture()
        scene, _ = signal_stubs(pl, nets, index)
        shared = static_shape_conflicts(scene)
        preflight = _static_faults(PAR, pl, nets, index, RoutingOptions(), None)
        self.assertEqual(len(shared), 3)
        self.assertEqual(preflight['count'], 3)
        self.assertEqual(preflight['hard'], {identity for identity,_ in shared})
        self.assertEqual(preflight['violations'], [fault for _,fault in shared])

    def test_rename_reorder_mirror_and_translation_do_not_hide_collision(self):
        for kwargs in ({'rename':'renamed_'}, {'reverse':True}, {'mirror':True},
                       {'shift':(1000,-400)}, {'rename':'a_', 'reverse':True, 'mirror':True, 'shift':(500,200)}):
            with self.subTest(kwargs=kwargs):
                pl, _, nets, index = marker_fixture(**kwargs)
                _, routes = signal_stubs(pl, nets, index)
                with self.assertRaises(RoutingValidationError) as error:
                    validate_final_routes(PAR, nets, pl, index, routes)
                self.assertIn('marker_body_halo', {v['kind'] for v in error.exception.diagnostics['violations']})

    def test_safe_own_default_marker_entry_keeps_original_routes_and_orientation(self):
        for family in ('ISRC', 'VSRC'):
            with self.subTest(family=family):
                pl, _, nets, index = source_case(family=family, orient=0)
                scene, routes = signal_stubs(pl, nets, index)
                before = deepcopy(pl.__dict__)
                self.assertEqual(static_shape_conflicts(scene), [])
                rr, metrics, result = validate_final_routes(PAR, nets, pl, index, routes)
                self.assertTrue(result['valid']); self.assertEqual(pl.__dict__, before)
                self.assertEqual(metrics['wirelength'], 18)
                self.assertEqual(rr, routes)

    def test_each_collision_identity_is_retained_even_at_coincident_coordinates(self):
        scene = Scene(body_rects=[(i,(0,0,10,10)) for i in range(3)])
        faults = static_shape_conflicts(scene)
        self.assertEqual(len(faults), 3)
        self.assertEqual({identity for identity,_ in faults},
                         {('body_overlap',0,1),('body_overlap',0,2),('body_overlap',1,2)})

    def test_exact_boundary_contact_and_configured_halo_keep_old_contract(self):
        scene = Scene(body_rects=[(0,(0,0,10,10)),(1,(10,0,20,10))])
        self.assertEqual(static_shape_conflicts(scene), [])
        scene.body_rects[1] = (1,(9,0,20,10))
        self.assertEqual(len(static_shape_conflicts(scene)), 1)
        pl, _, nets, index = marker_fixture()
        scene, routes = signal_stubs(pl, nets, index, RoutingOptions(halo=0))
        self.assertEqual(static_shape_conflicts(scene), [])
        self.assertTrue(validate_final_routes(PAR, nets, pl, index, routes, RoutingOptions(halo=0))[2]['valid'])

    def test_foreign_marker_is_not_exempted_by_matching_supply_name(self):
        scene = Scene(body_rects=[(0,(0,0,10,10)),(1,(20,0,30,10))],
            marker_rects=[('vdd',(21,1,29,9))], marker_components=[0])
        faults = static_shape_conflicts(scene)
        self.assertEqual(faults[0][0], ('marker_body_halo',0,1))
        self.assertEqual(faults[0][1]['nets'], ['vdd'])
        # Only the actual owning component gets the existing entry allowance.
        scene.marker_components = [1]
        self.assertEqual(static_shape_conflicts(scene), [])

    def test_scene_builder_preserves_real_marker_owner_order_after_net_reordering(self):
        pl, _, nets, index = marker_fixture(reverse=True)
        scene, _ = signal_stubs(pl, dict(reversed(list(nets.items()))), index)
        expected = [index[name] for net,members in reversed(list(nets.items()))
                    if PAR.power_marker_sym(net) for name,pin in members]
        self.assertEqual(scene.marker_components, expected)

    def test_shape_validation_deadline_is_enforced(self):
        scene = Scene(body_rects=[(i,(i*100,0,i*100+10,10)) for i in range(30)])
        with self.assertRaises(RoutingFailure) as error:
            static_shape_conflicts(scene, deadline=perf_counter()-1)
        self.assertEqual(error.exception.diagnostics['reason'], 'quality_time_budget')

    def test_independent_redraw_rejects_old_valid_flag_and_preserves_existing_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); source = root/'circuit.cir'
            source.write_text('* shared shape guard\nRowner vdd 0 1k\nIsource signal 0 DC 1m\n.end\n',encoding='utf8')
            saved = root/'placement.json'
            saved.write_text(json.dumps(dict(source_netlist=str(source),
                source_sha256=sha256(source.read_bytes()).hexdigest(),
                placement={'Rowner':dict(row=1,col=0,orient=1),'Isource':dict(row=0,col=1,orient=0)},
                layout_grid=dict(column_x=[100,150],row_y=[174,200]),
                routes={'signal':[[[150,154],[150,136]]]},
                validation=dict(valid=True,parameters=asdict(RoutingOptions())))),encoding='utf8')
            output = root/'existing.svg'; output.write_text('keep successful artifact',encoding='utf8')
            with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar')):
                with self.assertRaises(RoutingValidationError):
                    render(saved,output)
            self.assertEqual(output.read_text('utf8'), 'keep successful artifact')
