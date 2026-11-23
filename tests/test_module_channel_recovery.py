"""Changed guests cannot invalidate frozen nets or silently move an active frame."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from time import perf_counter
import unittest
from unittest.mock import patch

from tests.test_passive_modules import fixture
from tests.test_series_alignment import PAR
from netlist2schematic.flow import run_flow
from netlist2schematic.recovery import module_channels
from netlist2schematic.plugins.passive_skeleton.placement import pack
from netlist2schematic.routing.routing_rules import RoutingOptions, build_scene
from netlist2schematic.routing.sa_routing import final_route
from netlist2schematic.routing.sa_routing import validate_final_routes


def case():
    _, core, comps, nets, index, plan=fixture()
    options=RoutingOptions()
    placement, report=pack(PAR,core,comps,nets,index,plan,options,seconds=2)
    assert placement is not None,report
    routed=final_route(PAR,nets,placement,index,options=options,deadline=perf_counter()+5)
    scene=build_scene(PAR,nets,placement,index,options=options)
    failed=plan['groups'][0]['ends'][0]
    completed={net:segments for net,segments in routed[0] if net!=failed}
    checkpoint=dict(scene=scene,completed=completed,failed_net=failed,pending=[],
                    margin=150.,options=options,branch={})
    metadata=dict(candidate_id='2',structs=[],layout_branches=dict(applied=True),
                  passive_skeleton=report,local_rescue={})
    return (placement,comps,nets,index,metadata),checkpoint,options,routed


def move_group(placement,group,index,dy=-400):
    current=deepcopy(placement)
    for name in group['members']:
        current.passive_local_offsets[index[name]][1]+=dy
    guide=current.passive_module_guides[group['id']]
    guide['internal_routes']={net:[((a[0],a[1]+dy),(b[0],b[1]+dy)) for a,b in lines]
                              for net,lines in guide['internal_routes'].items()}
    guide['routes']=deepcopy(guide['internal_routes'])
    guide['ports']=[(p[0],p[1]+dy) for p in guide['ports']]
    return current


class ModuleChannelRecoveryTests(unittest.TestCase):
    def test_moved_module_can_finish_real_routing_and_pass_independent_validation(self):
        context,checkpoint,options,_=case()
        before=deepcopy(context)
        result,report=module_channels.rescue(context,checkpoint,options,
                                           deadline=perf_counter()+5,max_trials=64)
        self.assertIsNotNone(result,report)
        placement,comps,nets,index,metadata,routed=result
        checked=validate_final_routes(PAR,nets,placement,index,routed[0],options=options)
        self.assertTrue(checked[2]['valid'])
        self.assertTrue(metadata['local_rescue']['active_layout_preserved'])
        self.assertFalse(metadata['local_rescue']['layout_preserved'])
        for i,c in enumerate(comps):
            if c['symbol'] in ('nmos','pmos','npn','pnp'):
                self.assertEqual(placement.pos_of(i),before[0].pos_of(i))
                self.assertEqual(placement.orient[i],before[0].orient[i])
        self.assertEqual(context[0].passive_module_guides,before[0].passive_module_guides)

    def test_rebuilt_checkpoint_preserves_unrelated_complete_nets_and_active_geometry(self):
        context,checkpoint,options,_=case()
        before,comps,nets,index,metadata=context
        group=metadata['passive_skeleton']['plan']['groups'][0]
        trial=move_group(before,group,index)
        updated=module_channels._checkpoint(PAR,before,trial,comps,nets,index,metadata,
                                            checkpoint,options,perf_counter()+3)
        self.assertIsNotNone(updated)
        self.assertTrue(updated['repositioned_nets'])
        for net in set(checkpoint['completed'])-set(updated['repositioned_nets']):
            self.assertEqual(checkpoint['completed'][net],updated['completed'][net])
        for i,c in enumerate(comps):
            if c['symbol'] in ('nmos','pmos','npn','pnp'):
                self.assertEqual(before.pos_of(i),trial.pos_of(i))
                self.assertEqual(before.orient[i],trial.orient[i])

    def test_corrupt_frozen_net_is_rejected(self):
        context,checkpoint,options,_=case()
        before,comps,nets,index,metadata=context
        checkpoint['completed']['ga']=[(before.pin_world(index['a'],'G'),
                                         before.pin_world(index['b'],'G'))]
        trial=move_group(before,metadata['passive_skeleton']['plan']['groups'][0],index)
        self.assertIsNone(module_channels._checkpoint(PAR,before,trial,comps,nets,index,metadata,
            checkpoint,options,perf_counter()+3))

    def test_expired_budget_or_zero_trials_rolls_back_every_position_and_guide(self):
        for limit,stop in ((16,perf_counter()-1),(0,perf_counter()+3)):
            context,checkpoint,options,_=case()
            placement=context[0]
            saved=deepcopy((placement.cell_of,placement.orient,placement.passive_local_offsets,
                            placement.passive_module_guides,checkpoint['completed'],context[4]))
            result,report=module_channels.rescue(context,checkpoint,options,deadline=stop,max_trials=limit)
            self.assertIsNone(result)
            self.assertNotEqual(report['status'],'valid')
            self.assertEqual(saved,(placement.cell_of,placement.orient,placement.passive_local_offsets,
                                   placement.passive_module_guides,checkpoint['completed'],context[4]))

    def test_fixed_module_is_never_relocated(self):
        context,checkpoint,options,_=case()
        group=context[4]['passive_skeleton']['plan']['groups'][0]
        context[0].fixed.add(context[3][group['members'][0]])
        with patch.object(module_channels,'slot_trials',side_effect=AssertionError('fixed guest moved')):
            result,_=module_channels.rescue(context,checkpoint,options,deadline=perf_counter()+3)
        self.assertIsNone(result)

    def test_saved_coordinate_rescue_does_not_opt_into_channel_changes(self):
        context,checkpoint,options,routed=case()
        with patch('netlist2schematic.recovery.local_rescue.rescue_routing',
                   return_value=(*deepcopy(context),deepcopy(routed))), \
                patch.object(module_channels,'rescue',side_effect=AssertionError('saved guest moved')), \
                redirect_stdout(StringIO()):
            result=run_flow._try_local_rescue(context,checkpoint,options,'auto',3,[])
        self.assertIsNotNone(result)


if __name__=='__main__':unittest.main()
