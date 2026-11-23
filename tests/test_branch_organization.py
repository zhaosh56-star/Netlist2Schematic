"""Column identity, region ownership, and rollback across ordinary SA actions."""
from copy import deepcopy
from pathlib import Path
import random
import unittest
from unittest.mock import patch

from netlist2schematic.layout import structured_placement as sp
from netlist2schematic.netlist.netlist_core import to_document
from netlist2schematic.plugins.organization import branch_layout as org
from netlist2schematic.flow.run_flow import _validate_branch_policy


def mos(d, g, s, kind='nmos'):
    return dict(cls='MOS',type=kind,d=d,g=g,s=s,bulk='0',model=kind)


def frame(devices, positions, structs=None):
    doc=to_document(devices);cc,nn=doc['components'],doc['nets']
    ii={c['id']:i for i,c in enumerate(cc)}
    pl=sp.StructuredPlacement(cc,random.Random(73),[positions[c['id']][0] for c in cc],cols=10)
    pl.column_x=[64+124*c for c in range(pl.cols)]
    pl.row_y=[64+126*r for r in range(max(r for r,c in positions.values())+1)]
    pl.blocks=[[i] for i in range(len(cc))];pl.block_of={i:i for i in range(len(cc))}
    pl.role_orders=[];pl.fixed=set()
    pl.set_state([positions[c['id']] for c in cc],[0]*len(cc))
    return pl,cc,nn,ii,list(structs if structs is not None else sp._structures(devices))


def stacks(extra=0):
    devices=dict(ul=mos('vdd','g1','jl'),ll=mos('jl','g2','0'),
                 ur=mos('vdd','g1','jr'),lr=mos('jr','g2','0'))
    pos=dict(ul=(0,1),ll=(1,3),ur=(0,6),lr=(1,7))
    for i in range(extra):
        devices['other'+str(i)]=mos('unrelated'+str(i),'clock','0')
        pos['other'+str(i)]=(2+i//8,i%8)
    return devices,frame(devices,pos)


def prepare(devices,case,**kwargs):
    pl,cc,nn,ii,ss=case
    return org.initialize(sp.par,pl,cc,nn,ii,devices,ss,[],seconds=1.,**kwargs)


class BranchPlanTests(unittest.TestCase):
    def test_same_evidence_in_small_and_larger_circuits(self):
        edges=[]
        for extra in (0,19):
            dev,case=stacks(extra);pl,cc,nn,ii,ss=case
            p=org.build_plan(sp.par,pl,cc,nn,ii,dev,ss)
            edges.append({(e['a'],e['b']) for e in p['continuation_edges']})
            self.assertEqual(p['scope'],'all_circuit_sizes_topology_only')
        self.assertEqual(edges[0],edges[1])
        self.assertEqual(edges[0],{('ul','ll'),('ur','lr')})

    def test_common_rails_and_clock_do_not_create_regions(self):
        dev={n:mos('out'+n,'clk','0') for n in ('a','b','c')}
        pl,cc,nn,ii,ss=frame(dev,dict(a=(0,1),b=(0,3),c=(0,6)))
        p=org.build_plan(sp.par,pl,cc,nn,ii,dev,ss)
        self.assertFalse(p['supported'])
        self.assertEqual(len(p['regions']),3)
        self.assertFalse(p['region_edges'])

    def test_ambiguous_fork_is_not_forced_into_one_main_column(self):
        dev=dict(u=mos('vdd','gu','j'),a=mos('j','ga','0'),b=mos('j','gb','0'))
        pl,cc,nn,ii,ss=frame(dev,dict(u=(0,2),a=(1,1),b=(1,4)))
        p=org.build_plan(sp.par,pl,cc,nn,ii,dev,ss)
        self.assertFalse(p['continuation_edges'])

    def test_bjt_channel_paths_work_without_a_full_architecture(self):
        dev=dict(a=dict(cls='BJT',type='npn',c='vdd',b='g1',e='j',model='npn'),
                 b=dict(cls='BJT',type='npn',c='j',b='g2',e='0',model='npn'))
        pl,cc,nn,ii,ss=frame(dev,dict(a=(0,1),b=(1,4)))
        p=org.build_plan(sp.par,pl,cc,nn,ii,dev,ss)
        self.assertEqual([(e['pin_a'],e['pin_b']) for e in p['continuation_edges']],[('E','C')])
        self.assertEqual(p['coverage']['column_devices'],2)

    def test_polarity_dual_preserves_the_same_continuation_members(self):
        dev,case=stacks();pl,cc,nn,ii,ss=case
        p=org.build_plan(sp.par,pl,cc,nn,ii,dev,ss)
        dual={n:mos(d['s'],d['g'],d['d'],'pmos') for n,d in dev.items()}
        positions={n:pl.cell_of[ii[n]] for n in dev}
        cp,ccc,cnn,cii,css=frame(dual,positions)
        q=org.build_plan(sp.par,cp,ccc,cnn,cii,dual,css)
        self.assertEqual({(e['a'],e['b']) for e in p['continuation_edges']},
                         {(e['a'],e['b']) for e in q['continuation_edges']})

    def test_unique_ownership_and_child_pair_are_not_removed(self):
        dev,case=stacks();pl,cc,nn,ii,ss=case
        p=org.build_plan(sp.par,pl,cc,nn,ii,dev,ss)
        members=[n for r in p['regions'] for n in r['members']]
        self.assertEqual(sorted(members),sorted(dev))
        self.assertEqual(len(members),len(set(members)))
        self.assertEqual(len(p['regions']),1)
        self.assertEqual(len(p['regions'][0]['lanes']),2)

    def test_rename_reorder_and_translation_preserve_topology(self):
        dev,case=stacks();pl,cc,nn,ii,ss=case
        p=org.build_plan(sp.par,pl,cc,nn,ii,dev,ss)
        names={n:'instance'+str(i) for i,n in enumerate(reversed(list(dev)))}
        net=lambda n:n if n in ('vdd','0') else 'signal_'+n
        renamed={names[n]:{k:net(v) if k in ('d','g','s') else v for k,v in d.items()}
                 for n,d in reversed(list(dev.items()))}
        positions={names[n]:pl.cell_of[ii[n]] for n in dev}
        cp,ccc,cnn,cii,css=frame(renamed,positions)
        cp.column_x=[x+1000 for x in cp.column_x];cp.row_y=[y+500 for y in cp.row_y]
        q=org.build_plan(sp.par,cp,ccc,cnn,cii,renamed,css)
        invert={v:k for k,v in names.items()}
        self.assertEqual({frozenset((e['a'],e['b'])) for e in p['continuation_edges']},
                         {frozenset((invert[e['a']],invert[e['b']])) for e in q['continuation_edges']})
        self.assertEqual({frozenset(r['members']) for r in p['regions']},
                         {frozenset(invert[n] for n in r['members']) for r in q['regions']})


class BranchStateTests(unittest.TestCase):
    def test_cross_row_order_cycle_is_rejected_without_pairwise_reversal(self):
        dev={n:mos('out'+n,'in'+n,'0') for n in ('a0','a2','b0','b1','c1','c2')}
        pl,cc,nn,ii,ss=frame(dev,dict(a0=(0,0),b0=(0,1),b1=(1,0),c1=(1,1),c2=(2,0),a2=(2,1)))
        p=org.build_plan(sp.par,pl,cc,nn,ii,dev,ss)
        p['regions']=[dict(id=k,members=[n for n in dev if n[0]==k],lanes=[]) for k in ('a','b','c')]
        p['ownership']={n:n[0] for n in dev}
        m=org.measure(pl,ii,p)
        self.assertEqual(m['region_interleaving'],0)
        self.assertEqual(m['order_reversals'],0)
        self.assertEqual(m['cyclic_region_order'],1)

    def test_consistent_global_mirror_is_legal(self):
        dev,case=stacks();pl,_,report=prepare(dev,case)
        before=org.measure(pl,case[3],pl.branch_plan)
        pl.set_state([(r,pl.cols-1-c) for r,c in pl.cell_of],pl.orient)
        self.assertTrue(org.valid(pl))
        self.assertEqual(org.rank(before),org.rank(org.measure(pl,case[3],pl.branch_plan)))

    def test_initialization_aligns_lanes_without_mutating_input(self):
        dev,case=stacks();old=deepcopy(case[0].cell_of)
        out,alignment,report=prepare(dev,case)
        self.assertTrue(report['applied'])
        self.assertEqual(report['current']['column_breaks'],0)
        self.assertEqual(case[0].cell_of,old)
        self.assertEqual(out.row_of,case[0].row_of)
        self.assertTrue(out.blocks_contiguous())
        self.assertTrue(alignment)

    def test_old_moves_reject_column_break_and_recover_occupancy(self):
        dev,case=stacks();pl,_,report=prepare(dev,case)
        before=deepcopy(pl.cell_of);i=pl.branch_index['ll']
        cells=list(before);cells[i]=(cells[i][0],pl.cols-1)
        pl.set_state(cells,pl.orient)
        self.assertFalse(pl.blocks_contiguous())
        pl.set_state(before,pl.orient)
        self.assertTrue(org.valid(pl))
        self.assertEqual(pl.comp_at,{cell:i for i,cell in enumerate(before)})

    def test_sa_sampling_rejection_and_best_restore_preserve_contract(self):
        dev,case=stacks();pl,align,report=prepare(dev,case)
        with patch('netlist2schematic.routing.orthogonal_router.route_scene',side_effect=AssertionError('A* in SA')):
            for k in range(500):
                state=pl.propose()
                self.assertTrue(org.valid(pl));self.assertTrue(pl.blocks_contiguous())
                self.assertEqual(len(set(pl.cell_of)),len(pl.comps))
                if state is not None and k%2:pl.set_state(*state)
            sp._anneal(pl,case[2],case[3],align,[],250,None)
        self.assertTrue(org.valid(pl))
        self.assertEqual(pl.comp_at,{cell:i for i,cell in enumerate(pl.cell_of)})

    def test_fixed_cells_and_zero_budget_preserve_input(self):
        for limit in ('fixed','zero'):
            dev,case=stacks();pl=case[0]
            if limit=='fixed':pl.fixed=set(range(len(pl.comps)))
            if limit=='zero':
                out,_,report=org.initialize(sp.par,pl,case[1],case[2],case[3],dev,case[4],[],seconds=0)
            else:out,_,report=prepare(dev,case)
            self.assertEqual(out.cell_of,pl.cell_of)
            self.assertFalse(report['applied'])

    def test_auxiliary_estimator_nets_never_enter_static_scene(self):
        dev,case=stacks();fake='__layout_attachment_edge_probe'
        case[2][fake]=[('ul','G'),('ur','G')]
        from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
        inspected=[]
        def check(par,pl,nets,index,options,deadline):
            inspected.append(set(nets));return _static_faults(par,pl,nets,index,options,deadline)
        with patch('netlist2schematic.plugins.preflight.placement_preflight._static_faults',side_effect=check):
            out,_,report=prepare(dev,case)
        self.assertTrue(report['applied']);self.assertTrue(inspected)
        self.assertTrue(all(fake not in ns for ns in inspected))
        self.assertIn(fake,case[2])

    def test_timeout_does_not_commit_unvalidated_seed(self):
        dev,case=stacks()
        from netlist2schematic.routing.orthogonal_router import RoutingFailure
        with patch('netlist2schematic.plugins.preflight.placement_preflight._static_faults',
                   side_effect=RoutingFailure('deadline',dict(reason='quality_time_budget'))):
            out,_,report=prepare(dev,case)
        self.assertFalse(report['applied']);self.assertEqual(out.cell_of,case[0].cell_of)
        self.assertEqual(report['stop_reason'],'time_budget')

    def test_shared_budget_and_no_new_physical_rows(self):
        dev,case=stacks();out,_,report=prepare(dev,case,max_trials=1)
        self.assertLessEqual(report['trials'],1)
        self.assertEqual(out.row_y,case[0].row_y)
        self.assertEqual([r for r,c in out.cell_of],case[0].row_of)

    def test_feedback_cannot_restore_an_unorganized_old_layout(self):
        dev,case=stacks();new,_,report=prepare(dev,case)
        self.assertFalse(org.compatible(case[0],new,case[3]))
        self.assertTrue(org.compatible(deepcopy(new),new,case[3]))


class BranchIntegrationTests(unittest.TestCase):
    def test_candidate_selection_preserves_original_routing_priorities(self):
        dev,case=stacks();pl,_,report=prepare(dev,case)
        def result(identifier,bends,crossings,wire,placement=None):
            return ((),placement or deepcopy(pl),case[1],case[2],case[3],
                    dict(layout_branches=deepcopy(report)),
                    ([],dict(bends=bends,crossings=crossings,wirelength=wire),dict(valid=True)),{},identifier)
        a=result('2',5,0,1000);b=result('3',4,2,1200)
        winner,selection=org.select_results([a,b])
        self.assertEqual(winner[8],'3')
        self.assertEqual(selection['ranking'],['bends','crossings','wirelength'])
        broken=deepcopy(pl);cells=list(broken.cell_of);i=case[3]['ll'];cells[i]=(cells[i][0],broken.cols-1)
        broken.set_state(cells,broken.orient)
        winner,selection=org.select_results([a,result('3',0,0,800,broken)])
        self.assertEqual(winner[8],'2')
        self.assertEqual(selection['structure_excluded_candidates'],['3'])

    def test_saved_layout_measurement_does_not_move_devices(self):
        dev,case=stacks();pl,_,report=prepare(dev,case)
        before=deepcopy(pl.cell_of);data=dict(layout_branches=report)
        measured=org.refresh(pl,case[1],case[3],data,stage='saved_reroute')
        self.assertEqual(pl.cell_of,before)
        self.assertEqual(measured['layout_branches']['measurement_stage'],'saved_reroute')
        self.assertNotIn('measurement_stage',data['layout_branches'])

    def test_switch_validation_and_conflicting_layouts(self):
        _validate_branch_policy('auto')
        for args in [('bad',),('auto','baseline'),('auto','layered','legacy'),
                     ('auto','layered','structured','off'),('auto','layered','structured','auto','auto')]:
            with self.assertRaises(ValueError):_validate_branch_policy(*args)

    def test_reference_candidate_is_identical_and_off_does_not_load_plugin(self):
        path=Path(__file__).parent/'fixtures/aesthetic-baseline/4.cir'
        values=[]
        for policy in ('off','auto'):
            with patch.object(org,'initialize',side_effect=AssertionError('reference changed')):
                row=next(sp.create_candidates(path,candidate_ids=(1,),iterations=60,
                    branch_layout=policy,passive_layout='fixed'))
            values.append((row[0].cell_of,row[0].orient,row[4]['energy'],row[0].rng.getstate()))
        self.assertEqual(values[0],values[1])

    def test_module_frame_preserves_organization_with_new_indices(self):
        from netlist2schematic.plugins.passive_skeleton.placement import make_frame
        from netlist2schematic.routing.routing_rules import RoutingOptions
        dev,case=stacks();pl,_,_=prepare(dev,case)
        guest={'id':'Rextra','symbol':'resistor','nodes':{'1':'jl','2':'other'}}
        comps=[guest]+deepcopy(case[1]);index={c['id']:i for i,c in enumerate(comps)}
        lifted=make_frame(sp.par,pl,comps,index,RoutingOptions())
        self.assertEqual(lifted.branch_index,index)
        self.assertTrue(org.valid(lifted))
        self.assertEqual(org.measure(lifted,index,pl.branch_plan),org.measure(pl,case[3],pl.branch_plan))


if __name__=='__main__':unittest.main()
