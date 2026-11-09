import importlib.util
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('structure_compare',Path(__file__).resolve().parents[1]/'tools/compare_structure_layouts.py')
comparison=importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


def verified(length=100,crossings=0,interleaving=0,stage='normal'):
    return {'metrics':{'wirelength':length,'crossings':crossings},
        'measurement':{'interleaving':interleaving,'role_order':0},'routing_stage':stage}


class FinalOutputGateTests(unittest.TestCase):
    def test_row_plan_checks_tail_strict_order_and_same_layer(self):
        from types import SimpleNamespace
        pl=SimpleNamespace(cell_of=[(1,0),(1,1),(2,0)])
        idx={'a':0,'b':1,'tail':2}
        plan={'rows':{'a':1,'b':1,'tail':2},'tail_orders':[{'lower':'tail'}],
            'strict_orders':[{'upper':'a','lower':'tail'}],
            'same_row':[{'members':['a','b']}]}
        comparison.verify_row_plan({'branch_row_plan':plan},pl,idx,{'a','b'})
        pl.cell_of[2]=(1,2)
        with self.assertRaises(ValueError):comparison.verify_row_plan({'branch_row_plan':plan},pl,idx,{'a','b'})
    def test_final_length_and_crossings_are_independently_bounded(self):
        self.assertTrue(all(comparison.gates(verified(crossings=1),verified(115,2)).values()))
        self.assertFalse(comparison.gates(verified(),verified(116))['wirelength_115_percent'])
        self.assertFalse(comparison.gates(verified(),verified(crossings=1))['crossings_limit'])

    def test_new_bus_or_structure_regression_blocks_release(self):
        gates=comparison.gates(verified(),verified(interleaving=1,stage='bus_fallback'))
        self.assertFalse(gates['no_new_bus_fallback'])
        self.assertFalse(gates['no_new_structure_violations'])

    def test_asymmetric_parse_skip_cannot_be_mistaken_for_success(self):
        with tempfile.TemporaryDirectory() as folder:
            cases=[{'case':'x'}]
            records={('x','off'):{'status':'skipped','reason':'input_validation_error'},
                     ('x','auto'):{'status':'failed','error':'bad'}}
            report=comparison.publish(Path(folder),cases,records,{}, {}, {})
            self.assertFalse(report['release_gates']['paired_status_consistent'])
            self.assertFalse(report['release_gates']['no_routing_failures'])

    def test_incomplete_batch_cannot_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            report=comparison.publish(Path(folder),[{'case':'x'}],{}, {}, {}, {})
            self.assertFalse(report['release_gates']['complete'])
            self.assertFalse(report['release_gates']['paired_status_consistent'])


if __name__=='__main__':unittest.main()
