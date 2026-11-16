"""Passive-chain anchors come from ports, not a global canvas center or rails."""
from copy import deepcopy
from unittest import TestCase,main

from netlist2schematic.plugins.region.region_placement import _guest_anchors,_pack_seed


def fixture():
    comps=[dict(id='left',symbol='nmos'),dict(id='right',symbol='nmos'),
           dict(id='r',symbol='resistor'),dict(id='c',symbol='capacitor'),
           dict(id='source',symbol='voltage-source')]
    index={c['id']:i for i,c in enumerate(comps)}
    cells=[(2,1),(2,7),None,None,None]
    nets=dict(control=[('left','G'),('r','IN')],series=[('r','OUT'),('c','IN')],
              boundary=[('c','OUT'),('source','+')],
              power=[('source','-'),('right','S'),('left','S')])
    return comps,nets,index,cells,{'power_interfaces':['power']}


class GuestAnchorTests(TestCase):
    def test_both_members_of_unique_input_chain_find_same_real_gate_host(self):
        f=fixture()
        frozen=deepcopy(f)
        found=_guest_anchors(*f)
        self.assertEqual(found[2],{0})
        self.assertEqual(found[3],{0})
        self.assertNotIn(4,found)
        self.assertEqual(f,frozen)

    def test_two_real_endpoint_hosts_are_both_kept_without_merging_owners(self):
        f=list(fixture())
        f[1]['series'].append(('right','D'))
        found=_guest_anchors(*f)
        self.assertEqual(found[2],{0,1})
        self.assertEqual(found[3],{1})
        self.assertEqual(f[3][:2],[(2,1),(2,7)])

    def test_walk_does_not_infer_through_a_voltage_source_or_shared_supply(self):
        f=list(fixture())
        f[1]['boundary'].append(('right','D'))
        f[1]['control']=[('r','IN')]
        found=_guest_anchors(*f)
        self.assertEqual(found[2],{1})
        # Remove the real signal anchor. The shared supply is not an owner.
        f[1]['boundary'].remove(('right','D'))
        f[1]['power'].append(('r','IN'))
        self.assertNotIn(2,_guest_anchors(*f))
        self.assertNotIn(3,_guest_anchors(*f))

    def test_passive_cycles_terminate_without_inventing_an_anchor(self):
        f=list(fixture())
        f[1]['control']=[('r','IN'),('c','OUT')]
        found=_guest_anchors(*f)
        self.assertEqual(found,{})

    def test_renaming_order_and_arbitrary_rail_names_do_not_change_semantic_hosts(self):
        comps,nets,index,cells,plan=fixture()
        names={c['id']:'part'+str(99-n) for n,c in enumerate(comps)}
        reordered=[dict(c,id=names[c['id']]) for c in reversed(comps)]
        idx={c['id']:i for i,c in enumerate(reordered)}
        wires={'wire'+str(n):[(names[name],pin) for name,pin in reversed(ports)]
               for n,ports in enumerate(nets.values())}
        found=_guest_anchors(reordered,wires,idx,list(reversed(cells)),
                             {'power_interfaces':['wire3']})
        self.assertEqual(found[idx[names['r']]],{idx[names['left']]})
        self.assertEqual(found[idx[names['c']]],{idx[names['left']]})

    def test_only_port_proven_chain_can_use_its_bordering_region_channel(self):
        comps=[dict(id=name,symbol=symbol) for name,symbol in
               [('a','nmos'),('b','nmos'),('r','resistor'),('c','capacitor'),('unknown','voltage-source')]]
        index={c['id']:i for i,c in enumerate(comps)}
        plan=dict(regions=[dict(id='A',members=['a'],children=[]),dict(id='B',members=['b'],children=[])],
                  series_axes=[],power_interfaces=['power'])
        nets=dict(signal=[('a','G'),('r','IN')],chain=[('r','OUT'),('c','IN')])
        cells,cols=_pack_seed(comps,nets,index,[0,0,1,1,1],plan,[],['A','B'],1)
        self.assertEqual(cells[2],(1,0))
        self.assertEqual(cells[3],(1,1),'the chain may use the channel beside its host')
        self.assertEqual(cells[4],(1,2),'an unknown guest must leave that channel alone')
        self.assertEqual(cols,3)
        # Sharing a rail is insufficient to acquire the same access privilege.
        nets=dict(power=[('a','S'),('b','S'),('r','IN'),('c','IN')])
        cells,_=_pack_seed(comps,nets,index,[0,0,1,1,1],plan,[],['A','B'],1)
        self.assertTrue(all(cells[i][1]!=1 for i in (2,3,4)))


if __name__=='__main__':
    main()
