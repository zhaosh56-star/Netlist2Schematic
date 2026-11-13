import unittest
from netlist2schematic.flow.candidate_budget import routing_deadline


class CandidateBudgetTests(unittest.TestCase):
    def test_reserved_deadlines_are_absolute_and_include_prior_generation_time(self):
        self.assertEqual([310,355,400],[routing_deadline(100,300,i,policy='reserved') for i in (1,2,3)])

    def test_sequential_and_explicit_replay_keep_the_whole_deadline(self):
        for i in (0,1,2,3):
            self.assertEqual(400,routing_deadline(100,300,i))
            self.assertEqual(400,routing_deadline(100,300,i,policy='reserved',replay=True))

    def test_invalid_policy_and_budget(self):
        for kwargs in ({'policy':'wrong'},{'seconds':float('nan')},{'seconds':0},{'started':True}):
            args={'started':100,'seconds':300,'identifier':1,**kwargs}
            with self.assertRaises(ValueError):routing_deadline(**args)


if __name__=='__main__':unittest.main()
