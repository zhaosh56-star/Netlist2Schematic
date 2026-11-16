"""Three-terminal T changes preserve real pins and the old safe scheduler."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from netlist2schematic.postprocess import route_junctions as junctions
from netlist2schematic.postprocess import simplify_routes as shortcuts
from netlist2schematic.routing.routing_rules import Pin,Scene,RoutingOptions,canonical_topology,intersection,build_scene
from netlist2schematic.routing.sa_routing import _verify,validate_final_routes,RoutingValidationError
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from tests.test_simplify_routes import run_case


def fixture(*,rotation=0,mirror=False,shift=(0,0),foreign=None,obstacle=None,rename=False,
            split=False,reverse=False,fourth=False,outside=False,unaligned=False):
    def transform(point,vector=False):
        x,y=point
        if mirror:x=-x
        for _ in range(rotation):x,y=-y,x
        return (x,y) if vector else (x+shift[0],y+shift[1])
    positions=[(0,0),(100,0),(150 if outside else 50,100)]
    escapes=[(0,20),(100,30 if unaligned else 20),(positions[2][0],80)]
    raw=[((0,0),(0,80)),((100,0),(100,80)),((0,80),(positions[2][0] if outside else 100,80)),
         (positions[2],escapes[2])]
    net='arbitrary_77' if rename else 'a';name=lambda i:('unit_'+str(91-i) if rename else 'device'+str(i))
    pins={net:[Pin(net,i,'P',transform(p),transform(e),transform(v,True))
        for i,(p,e,v) in enumerate(zip(positions,escapes,((0,1),(0,1),(0,-1))))]}
    nets={net:[(name(i),'P') for i in range(3)]};index={name(i):i for i in range(3)}
    if fourth:
        positions.append((150,80));p=Pin(net,3,'P',transform(positions[3]),transform((130,80)),transform((-1,0),True))
        pins[net].append(p);nets[net].append((name(3),'P'));index[name(3)]=3
        raw.append(((100,80),(150,80)))
    routes=[(net,[(transform(a),transform(b)) for a,b in raw])]
    if foreign:
        other='other';offset=len(positions)
        points=([(25,-40),(25,120)] if foreign=='crossing' else
                [(25,20),(25,40)] if foreign=='contact' else [(25,0),(25,40)])
        positions.extend(points);pins[other]=[];nets[other]=[]
        for i,p in enumerate(points,offset):
            pins[other].append(Pin(other,i,'P',transform(p),transform(p),transform((0,1),True)))
            nets[other].append((name(i),'P'));index[name(i)]=i
        routes.append((other,[(transform(points[0]),transform(points[1]))]))
    if split:
        routes=[(n,[(a,((a[0]+b[0])/2,(a[1]+b[1])/2)) for a,b in lines]
            +[(((a[0]+b[0])/2,(a[1]+b[1])/2),b) for a,b in lines]) for n,lines in routes]
    if reverse:
        routes=[(n,[(b,a) for a,b in reversed(lines)]) for n,lines in reversed(routes)]
        pins={n:list(reversed(p)) for n,p in pins.items()};nets={n:list(reversed(v)) for n,v in nets.items()}
    scene=Scene(options=RoutingOptions(),pins=pins,
        all_pins={n:[p.point for p in ps] for n,ps in pins.items()},
        fixed_segments={n:[p.stub for p in ps if p.point!=p.escape] for n,ps in pins.items()})
    if obstacle:
        corners=[transform((x,y)) for x in (40,60) for y in (30,50)]
        rect=(min(x for x,y in corners),min(y for x,y in corners),max(x for x,y in corners),max(y for x,y in corners))
        if obstacle=='body':scene.body_rects.append((999,rect))
        else:scene.marker_rects.append(('power',rect));scene.power_nets.add('power')
    placement=SimpleNamespace(comps=[],pin_world=lambda i,p:transform(positions[i]))
    def cross(a,b,c,d):
        point=intersection(a,b,c,d);return point is not None and point not in (a,b,c,d)
    par=SimpleNamespace(power_marker_sym=lambda n:None,GEO_DOC={},MARGIN=12,seg_cross=cross,
        seg_overlap_len=lambda *a:0,seg_parallel_prox=lambda *a,**kw:0,seg_hits_rect=lambda *a:False)
    checked,metrics,_=_verify(par,nets,placement,index,routes,scene)
    return par,nets,placement,index,(checked,metrics,dict(valid=True,router='original-router')),scene


class JunctionPatternTests(unittest.TestCase):
    def assert_valid(self,f,result):
        checked,metrics,analysis=_verify(*f[:4],result[0],f[5])
        self.assertEqual(result[1],metrics)
        for k in ('wirelength','crossings','bends'):self.assertLessEqual(metrics[k],f[4][1][k])
        self.assertTrue(result[2]['valid'])

    def test_three_aligned_escapes_replace_both_free_arms_at_once(self):
        f=fixture();before=deepcopy((f[4],f[5].pins,f[5].fixed_segments))
        with patch('netlist2schematic.routing.orthogonal_router._find_path',side_effect=AssertionError('Astar forbidden')):
            result=run_case(f,seconds=1)
        self.assert_valid(f,result)
        self.assertEqual(result[1]['wirelength'],220)
        self.assertEqual(result[3]['patterns'][junctions.PATTERN]['accepted'],1)
        self.assertEqual((f[4],f[5].pins,f[5].fixed_segments),before)

    def test_fixed_escape_keeps_its_position_but_can_stop_being_a_branch(self):
        f=fixture();scene=f[5];p=scene.pins['a'][2]
        old=canonical_topology(f[4][0][0][1],pins={p.escape})['adjacency']
        proposal=next(junctions.aligned_T_candidates(scene,f[4][0]))
        new=canonical_topology(proposal['segments'],pins={p.escape})['adjacency']
        self.assertEqual(len(old[p.escape]),3);self.assertEqual(len(new[p.escape]),2)
        self.assertTrue(shortcuts._covers_fixed(scene,'a',proposal['segments']))
        self.assertEqual(p.point,(50,100));self.assertEqual(p.escape,(50,80))

    def test_all_four_orientations_and_horizontal_mirror_keep_validity(self):
        for rotation in range(4):
            for mirror in (False,True):
                with self.subTest(rotation=rotation,mirror=mirror):
                    f=fixture(rotation=rotation,mirror=mirror,shift=(47,-31))
                    result=run_case(f,seconds=1);self.assert_valid(f,result)
                    self.assertEqual(result[1]['wirelength'],220)

    def test_names_netlist_order_and_artificial_segment_cuts_do_not_change_candidate_geometry(self):
        for rename in (False,True):
            for split in (False,True):
                f=fixture(rename=rename,split=split,reverse=True)
                result=run_case(f,seconds=1);self.assert_valid(f,result)
                self.assertEqual(result[1]['wirelength'],220)
                self.assertEqual(result[3]['patterns'][junctions.PATTERN]['accepted'],1)

    def test_bodies_and_power_markers_reject_illegal_T(self):
        for obstacle in ('body','marker'):
            f=fixture(obstacle=obstacle);proposal=next(junctions.aligned_T_candidates(f[5],f[4][0]))
            with self.assertRaises(RoutingValidationError):
                _verify(*f[:4],[(proposal['net'],proposal['segments'])],f[5])
            result=run_case(f,seconds=1);self.assert_valid(f,result)
            self.assertEqual(result[3]['patterns'][junctions.PATTERN]['accepted'],0)

    def test_legal_existing_crossing_can_move_without_increasing_count(self):
        f=fixture(foreign='crossing');result=run_case(f,seconds=1)
        self.assert_valid(f,result)
        self.assertEqual(result[1]['wirelength'],f[4][1]['wirelength']-60)
        self.assertEqual(result[1]['crossings'],1)

    def test_strict_mode_does_not_buy_shorter_T_with_new_crossing(self):
        f=fixture(foreign='new_crossing');result=run_case(f,seconds=1)
        self.assert_valid(f,result)
        self.assertEqual(result[1]['crossings'],0)
        self.assertEqual(result[3]['patterns'][junctions.PATTERN]['accepted'],0)

    def test_T_cannot_touch_another_networks_real_endpoint(self):
        f=fixture(foreign='contact');p=next(junctions.aligned_T_candidates(f[5],f[4][0]))
        trial=[(n,p['segments'] if n==p['net'] else lines) for n,lines in f[4][0]]
        with self.assertRaises(RoutingValidationError):_verify(*f[:4],trial,f[5])
        result=run_case(f,seconds=1);self.assert_valid(f,result)
        self.assertEqual(result[3]['patterns'][junctions.PATTERN]['accepted'],0)

    def test_fourth_terminal_is_not_lost_by_a_three_terminal_pattern(self):
        f=fixture(fourth=True)
        self.assertEqual(list(junctions.aligned_T_candidates(f[5],f[4][0])),[])
        result=run_case(f,seconds=1);self.assert_valid(f,result)
        self.assertEqual(len(f[1]['a']),4)
        self.assertEqual(result[3]['patterns'][junctions.PATTERN]['considered'],0)

    def test_unaligned_ports_or_projection_outside_trunk_do_not_trigger(self):
        for option in ('unaligned','outside'):
            f=fixture(**{option:True})
            self.assertEqual(list(junctions.aligned_T_candidates(f[5],f[4][0])),[])

    def test_power_net_and_repeated_escape_are_not_treated_as_T(self):
        f=fixture();f[5].power_nets.add('a')
        self.assertEqual(list(junctions.aligned_T_candidates(f[5],f[4][0])),[])
        f=fixture();p=f[5].pins['a'][1]
        f[5].pins['a'][1]=Pin(p.net,p.component,p.pin,p.point,f[5].pins['a'][0].escape,p.direction)
        self.assertEqual(list(junctions.aligned_T_candidates(f[5],f[4][0])),[])

    def test_T_and_old_chains_alternate_without_changing_reserved_stages(self):
        f=fixture()
        def generate(label):
            for i in range(20):yield dict(net='a',segments=[],label=label,number=i)
        with patch.object(shortcuts,'aligned_T_candidates',side_effect=lambda *a:generate('T')), \
             patch.object(shortcuts,'_chain_shortcut_candidates',side_effect=lambda *a:generate('chain')):
            proposals=shortcuts._shortcut_candidates(f[5],f[4][0])
            self.assertEqual([next(proposals)['label'] for _ in range(6)],['T','chain']*3)
        result=run_case(f,seconds=1);report=result[3]
        self.assertEqual(report['phase_order'],list(shortcuts.PHASES));self.assertEqual(len(report['stages']),4)
        self.assertEqual([r['candidate_limit'] for r in report['stages']],[128,256,384,512])
        self.assertLessEqual(report['candidate_counts']['considered'],512)

    def test_many_nets_do_not_force_exact_topology_work_before_first_yield(self):
        f=fixture();scene=deepcopy(f[5]);routes=list(f[4][0])
        for number in range(1,30):
            name='wire'+str(number);shift=250*number
            scene.pins[name]=[Pin(name,p.component+3*number,p.pin,(p.point[0]+shift,p.point[1]),
                (p.escape[0]+shift,p.escape[1]),p.direction) for p in scene.pins['a']]
            scene.fixed_segments[name]=[p.stub for p in scene.pins[name]]
            routes.append((name,[((a[0]+shift,a[1]),(b[0]+shift,b[1])) for a,b in f[4][0][0][1]]))
        with patch.object(junctions,'canonical_topology',wraps=canonical_topology) as exact:
            first=next(junctions.aligned_T_candidates(scene,routes))
        self.assertEqual(first['net'],'a');self.assertEqual(exact.call_count,2)

    def test_zero_seconds_and_expired_generation_keep_original_route(self):
        f=fixture()
        with patch.object(shortcuts,'aligned_T_candidates',side_effect=AssertionError('disabled')):
            result=run_case(f,seconds=0)
        self.assertEqual(result[:2],f[4][:2])
        with self.assertRaises(RoutingFailure):
            next(junctions.aligned_T_candidates(f[5],f[4][0],deadline=0))

    def test_late_validator_cannot_commit_an_unfinished_T(self):
        f=fixture()
        class Clock:
            now=1000.
            def __call__(self):return self.now
        clock=Clock();actual=_verify;calls=0
        def late(*args,**kwargs):
            nonlocal calls
            calls+=1;result=actual(*args,**kwargs)
            if calls>1:clock.now=kwargs['deadline']+.001
            return result
        with patch.object(shortcuts,'perf_counter',new=clock), \
             patch('netlist2schematic.routing.routing_rules.perf_counter',new=clock), \
             patch.object(shortcuts,'_verify',side_effect=late):
            result=run_case(f,seconds=1)
        self.assertEqual(result[:2],f[4][:2]);self.assertEqual(result[3]['candidate_counts']['accepted'],0)

    def test_timeout_after_one_T_keeps_that_verified_gain(self):
        a=fixture();b=fixture(shift=(250,0),rename=True)
        scene=deepcopy(a[5]);other=next(iter(b[1]))
        scene.pins[other]=[Pin(p.net,p.component+3,p.pin,p.point,p.escape,p.direction) for p in b[5].pins[other]]
        scene.all_pins[other]=b[5].all_pins[other];scene.fixed_segments[other]=b[5].fixed_segments[other]
        nets={**a[1],**b[1]};index={**a[3],**{n:i+3 for n,i in b[3].items()}}
        pl=SimpleNamespace(comps=[],pin_world=lambda i,p:a[2].pin_world(i,p) if i<3 else b[2].pin_world(i-3,p))
        routes=a[4][0]+b[4][0];checked,metrics,_=_verify(a[0],nets,pl,index,routes,scene)
        f=(a[0],nets,pl,index,(checked,metrics,dict(valid=True)),scene)
        class Clock:
            now=1000.
            def __call__(self):return self.now
        clock=Clock();calls=0
        def late(*args,**kwargs):
            nonlocal calls
            calls+=1;value=_verify(*args,**kwargs)
            if calls>2:clock.now=kwargs['deadline']+.001
            return value
        with patch.object(shortcuts,'perf_counter',new=clock), \
             patch('netlist2schematic.routing.routing_rules.perf_counter',new=clock), \
             patch.object(shortcuts,'_verify',side_effect=late):
            result=run_case(f,seconds=1)
        self.assert_valid(f,result)
        self.assertEqual(result[1]['wirelength'],metrics['wirelength']-60)
        self.assertEqual(result[3]['patterns'][junctions.PATTERN]['accepted'],1)

    def test_accepted_T_is_stable_when_simplified_again(self):
        f=fixture();first=run_case(f,seconds=1)
        second=run_case((*f[:4],first[:3],f[5]),seconds=1)
        self.assertEqual(first[:2],second[:2])
        self.assertEqual(second[3]['patterns'][junctions.PATTERN]['accepted'],0)

    def test_branch_move_never_introduces_cycle_or_loses_fixed_coverage(self):
        f=fixture();result=run_case(f,seconds=1)
        old=shortcuts._cycle_rank(dict(f[4][0])['a']);new=shortcuts._cycle_rank(dict(result[0])['a'])
        self.assertLessEqual(new,old)
        self.assertTrue(shortcuts._covers_fixed(f[5],'a',dict(result[0])['a']))

    def test_real_scene_builder_accepts_same_T_with_actual_symbol_halos(self):
        from tests.test_simplify_acceptance import schematic_par,FixturePlacement
        par=schematic_par();c=[dict(id=n,symbol='terminal') for n in ('left','right','stem')]
        pl=FixturePlacement(c,[(0,-10),(100,-10),(50,110)],[3,3,2]);index={v['id']:i for i,v in enumerate(c)}
        nets={'signal':[(v['id'],'P') for v in c]};scene=build_scene(par,nets,pl,index)
        pins=scene.pins['signal'];y=pins[2].escape[1]
        routes=[('signal',[(pins[0].point,(0,y)),(pins[1].point,(100,y)),((0,y),(100,y)),pins[2].stub])]
        checked,m,_=_verify(par,nets,pl,index,routes,scene)
        result=run_case((par,nets,pl,index,(checked,m,dict(valid=True)),scene),seconds=1)
        routed,actual,validation=validate_final_routes(par,nets,pl,index,result[0])
        self.assertTrue(validation['valid']);self.assertEqual(actual,result[1])
        self.assertLess(actual['wirelength'],m['wirelength'])


if __name__=='__main__':unittest.main()
