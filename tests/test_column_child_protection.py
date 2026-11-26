"""Through-columns cannot tear apart confirmed row-local transistor children."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from netlist2schematic.plugins.columns import column_layout as columns
from netlist2schematic.plugins.columns import local_groups as children
from netlist2schematic.layout import series_alignment
from netlist2schematic.routing.routing_rules import RoutingOptions
from tests.test_passive_placement import base_placement
from tests.test_series_alignment import PAR, device


def mirror(a, b, kind='current_mirror'):
    return dict(kind=kind, devices=[a,b], roles=dict(reference=a, outputs=[b]))


def cyclic_frame(family='bjt'):
    """Four real mirrors demand cyclic neighbors across four putative columns."""
    p, n = ('pnp','npn') if family == 'bjt' else ('pmos','nmos')
    devices, cells = {}, {}
    for k in range(4):
        devices['u'+str(k)] = device(p, 'top'+str(k), 'top'+str(k//2*2), 'vdd')
        devices['m'+str(k)] = device(p, 'join'+str(k), 'control'+str(k), 'top'+str(k))
        devices['l'+str(k)] = device(n, 'join'+str(k), 'join'+str(0 if k in (0,3) else 1), '0')
        for row, prefix in enumerate(('u','m','l')):cells[prefix+str(k)] = (row,k+1)
    records = [mirror('u0','u1'), mirror('u2','u3'), mirror('l0','l3'), mirror('l1','l2')]
    return (*base_placement(devices,cells), records)


def make_plan(case):
    pl,cc,nn,ii,ss = case
    return columns.build_column_plan(PAR,pl,cc,nn,ii,ss)


class HandoffEvidenceTests(unittest.TestCase):
    def test_bjt_and_mos_cycle_releases_common_terminal_joins_only(self):
        for family in ('bjt','mos'):
            case = cyclic_frame(family)
            result = make_plan(case)
            self.assertEqual({(e['a'],e['b']) for e in result['junction_handoffs']},
                             {('m'+str(k),'l'+str(k)) for k in range(4)})
            self.assertEqual({(e['a'],e['b']) for e in result['edges']},
                             {('u'+str(k),'m'+str(k)) for k in range(4)})
            links,_ = series_alignment.discover_series(PAR,*case[:4])
            self.assertTrue({(e['upper'],e['lower']) for e in links} <=
                            {(e['a'],e['b']) for e in result['edges']})
            self.assertEqual(len({n for c in result['columns'] for n in c['members']}),12)

    def test_acyclic_pair_demands_and_common_supply_alone_keep_columns(self):
        case=cyclic_frame()
        for records in ([],case[4][:-1]):
            plan=make_plan((*case[:4],records))
            self.assertFalse(plan['junction_handoffs'])
            self.assertEqual(len(plan['edges']),8)

    def test_explicit_composite_and_repeated_stack_evidence_not_released(self):
        case=cyclic_frame();pl,cc,nn,ii,records=case
        plain=make_plan((*case[:4],[]))
        groups=children.local_groups(pl,cc,ii,records)
        for flag in ('explicit','repeated'):
            edges=deepcopy(plain['edges'])
            for e in edges:
                if e['a'].startswith('m'):
                    if flag=='explicit':e['explicit']=True
                    else:e['evidence']='repeated_multilevel_controls'
            kept,released=children.release_conflicting_junctions(edges,{c['id']:c for c in cc},groups)
            self.assertFalse(released);self.assertEqual(kept,edges)

    def test_renaming_reordering_translation_mirror_and_polarity_dual(self):
        original=cyclic_frame()
        expected={(e['a'],e['b']) for e in make_plan(original)['junction_handoffs']}
        for dual in (False,True):
            pl,cc,nn,ii,ss=deepcopy(original)
            names={c['id']:'renamed'+str(k) for k,c in enumerate(reversed(cc))}
            inverse={v:k for k,v in names.items()}
            signal=lambda n:n if n in ('vdd','0') else 'wire_'+n
            for c in cc:
                c['id']=names[c['id']]
                c['nodes']={p:signal(n) for p,n in c['nodes'].items()}
                if dual:
                    c['symbol']={'pnp':'npn','npn':'pnp'}[c['symbol']]
                    c['nodes']['C'],c['nodes']['E']=c['nodes']['E'],c['nodes']['C']
            nn={signal(n):[(names[a],({'C':'E','E':'C'}.get(p,p) if dual else p)) for a,p in ts]
                for n,ts in reversed(list(nn.items()))}
            ss=[mirror(names[s['roles']['reference']],names[s['roles']['outputs'][0]]) for s in reversed(ss)]
            # Reorder the actual component/state arrays as well as netlist rows.
            cells=[(r,pl.cols-1-c) for r,c in reversed(pl.cell_of)]
            pl.comps=cc=list(reversed(cc));pl.row_of=[r for r,c in cells]
            pl.blocks=[[i] for i in range(len(cc))]
            pl.set_state(cells,list(reversed(pl.orient)))
            pl.column_x=[x+800 for x in pl.column_x];pl.row_y=[y+600 for y in pl.row_y]
            ii={c['id']:i for i,c in enumerate(cc)}
            result=make_plan((pl,cc,nn,ii,ss))
            self.assertEqual({(inverse[e['a']],inverse[e['b']]) for e in result['junction_handoffs']},expected)

    def test_ambiguous_overlaps_do_not_arbitrarily_choose_a_pair(self):
        case=cyclic_frame();pl,cc,nn,ii,ss=case
        ss=[mirror('u0','u1'),mirror('u1','u2')]
        groups=children.local_groups(pl,cc,ii,ss)
        self.assertFalse(groups)
        self.assertFalse(make_plan((*case[:4],ss))['junction_handoffs'])

    def test_multi_output_mirror_keeps_group_not_arbitrary_pair(self):
        pl,cc,nn,ii,_=cyclic_frame()
        record=dict(kind='current_mirror',devices=['u0','u1','u2'],
                    roles=dict(reference='u0',outputs=['u1','u2']))
        groups=children.local_groups(pl,cc,ii,[record])
        self.assertEqual([set(g['members']) for g in groups],[{'u0','u1','u2'}])


def close_child():
    devices=dict(a=device('nmos','top','ga','j'),b=device('nmos','j','j','0'),
                 c=device('nmos','out','j','0'))
    case=base_placement(devices,dict(a=(0,5),b=(1,2),c=(1,3)),blocks=[['b','c']])
    return (*case,[mirror('b','c')])


class ChildSpanTransactionTests(unittest.TestCase):
    def test_blank_columns_still_count_as_separating_a_child(self):
        pl,cc,nn,ii,ss=close_child();groups=children.local_groups(pl,cc,ii,ss)
        trial=deepcopy(pl);cells=list(pl.cell_of);cells[ii['c']]=(1,5);trial.set_state(cells,pl.orient)
        self.assertTrue(trial.blocks_contiguous())
        bad=children.expanded_groups(pl,trial,ii,groups)
        self.assertEqual(bad[0]['members'],['b','c'])
        self.assertEqual(bad[0]['before']['column_span'],1)
        self.assertEqual(bad[0]['after']['column_span'],3)

    def test_refinement_cannot_sacrifice_child_to_straighten_a_column(self):
        pl,cc,nn,ii,ss=close_child();plan=make_plan((pl,cc,nn,ii,ss))
        cells=list(pl.cell_of);cells[ii['b']]=(1,5)
        original=deepcopy(pl.cell_of)
        with patch.object(columns,'_proposals',return_value=iter([({ii['b']},cells)])), \
             patch('netlist2schematic.routing.sa_routing.final_route',side_effect=AssertionError('no A*')):
            after,report=columns.refine_columns(PAR,pl,cc,nn,ii,dict(structs=ss),plan,RoutingOptions())
        self.assertEqual(after.cell_of,original);self.assertEqual(pl.cell_of,original)
        self.assertEqual(report['layout_columns']['local_child_protection']['rejected_expansions'],1)
        self.assertFalse(report['layout_columns']['applied'])

    def test_whole_child_translation_remains_legal(self):
        pl,cc,nn,ii,ss=close_child();groups=children.local_groups(pl,cc,ii,ss)
        trial=deepcopy(pl);cells=list(pl.cell_of)
        for n in ('b','c'):cells[ii[n]]=(1,cells[ii[n]][1]+1)
        trial.set_state(cells,pl.orient)
        self.assertFalse(children.expanded_groups(pl,trial,ii,groups))

    def test_series_finalizer_inherits_the_child_protection(self):
        pl,cc,nn,ii,ss=close_child();pl.column_plan=make_plan((pl,cc,nn,ii,ss))
        trial=deepcopy(pl);cells=list(pl.cell_of);cells[ii['c']]=(1,6);trial.set_state(cells,pl.orient)
        # Moving c changes no continuation edge; only the new child guard rejects it.
        self.assertEqual(columns.column_key(columns.measure_columns(pl,ii,pl.column_plan)),
                         columns.column_key(columns.measure_columns(trial,ii,pl.column_plan)))
        self.assertFalse(columns.preserves_columns(pl,trial,ii))
        self.assertFalse(series_alignment._valid_trial(trial,pl,{ii['c']},set()))

    def test_nonuniform_axes_cannot_stretch_pair_at_equal_column_span(self):
        pl,cc,nn,ii,ss=close_child();pl.column_x[4:]=[700,900,1100,1300]
        trial=deepcopy(pl);cells=list(pl.cell_of)
        cells[ii['b']]=(1,5);cells[ii['c']]=(1,6);trial.set_state(cells,pl.orient)
        self.assertTrue(children.expanded_groups(pl,trial,ii,children.local_groups(pl,cc,ii,ss)))

    def test_fixed_rows_orientation_and_state_stay_transactional(self):
        pl,cc,nn,ii,ss=close_child();pl.fixed={c['id'] for c in cc}
        after,data=columns.refine_columns(PAR,pl,cc,nn,ii,dict(structs=ss),make_plan((pl,cc,nn,ii,ss)))
        self.assertEqual(after.cell_of,pl.cell_of);self.assertEqual(after.orient,pl.orient)
        self.assertEqual(after.row_of,pl.row_of);self.assertEqual(len(after.comp_at),len(cc))


if __name__=='__main__':unittest.main()
