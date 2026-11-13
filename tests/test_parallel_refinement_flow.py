"""Parallel and pin alignment spend one shared, bounded pre-routing budget."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from netlist2schematic.flow import aesthetic_flow
from netlist2schematic.layout import structured_placement

PARALLEL_SOURCE = '''.subckt main vdd
Mleft la lb 0 0 nmos
Mright lb la 0 0 nmos
Rl vdd la 1k
Cl vdd la 1p
Ll vdd la 1u
Rr vdd lb 1k
Cr vdd lb 1p
Lr vdd lb 1u
.ends main
.model nmos nmos
.end
'''


class ParallelRefinementFlowTests(unittest.TestCase):
    def route(self, *args, **kwargs):
        return [], {'wirelength': 100, 'crossings': 0, 'bends': 0}, {'valid': True, 'routing_seconds': 0}

    def test_parallel_and_branch_refinement_share_deadline_and_candidate_count(self):
        calls = []
        hpwl = []
        def parallel(par, pl, comps, nets, index, metadata, **kwargs):
            from netlist2schematic.plugins.layout_shared.branch_alignment import _hpwl
            hpwl.append(_hpwl(par, pl, nets, index))
            calls.append(('parallel', kwargs))
            metadata = deepcopy(metadata)
            metadata['parallel_attachment_refinement'] = {'candidates': 17}
            return pl, metadata
        def branch(par, pl, comps, nets, index, metadata, **kwargs):
            calls.append(('branch', kwargs))
            return pl, metadata
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'parallel.cir'
            path.write_text(PARALLEL_SOURCE, encoding='utf-8')
            candidates = list(structured_placement.create_candidates(path, cell='main', iterations=30,
                              structure_layout='auto', structure_phase=3))
            with patch('netlist2schematic.layout.structured_placement.create_candidates', return_value=iter(candidates)), \
                 patch('netlist2schematic.plugins.layout_shared.attachment_placement.refine_parallel_attachments', side_effect=parallel), \
                 patch('netlist2schematic.plugins.layout_shared.branch_alignment.regularize_branch_layout', side_effect=branch), \
                 patch('netlist2schematic.flow.aesthetic_flow.final_route', side_effect=self.route), \
                 patch('netlist2schematic.layout.layout_quality.evaluate_quality', return_value={'score': 1., 'per_net': {}}):
                aesthetic_flow.select_layout(path, cell='main', iterations=30, structure_layout='auto')
        self.assertEqual([name for name, _ in calls], ['parallel', 'branch', 'parallel', 'branch'])
        for i in (0, 2):
            self.assertEqual(calls[i][1]['max_candidates'], 128)
            self.assertEqual(calls[i+1][1]['max_candidates'], 111)
            self.assertEqual(calls[i][1]['deadline'], calls[i+1][1]['deadline'])
            self.assertLessEqual(calls[i+1][1]['seconds'], 1)
            self.assertEqual(calls[i+1][1]['hpwl_reference'], hpwl[i//2])

    def test_no_bank_uses_the_original_branch_call_path(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'source.cir'
            path.write_text('R1 a b 1k\n.end\n', encoding='utf-8')
            calls = []
            def branch(par, pl, comps, nets, index, metadata, **kwargs):
                calls.append(kwargs)
                return pl, metadata
            with patch('netlist2schematic.plugins.layout_shared.attachment_placement.refine_parallel_attachments', side_effect=AssertionError('no parallel bank')), \
                 patch('netlist2schematic.plugins.layout_shared.branch_alignment.regularize_branch_layout', side_effect=branch), \
                 patch('netlist2schematic.flow.aesthetic_flow.final_route', side_effect=self.route), \
                 patch('netlist2schematic.layout.layout_quality.evaluate_quality', return_value={'score': 1., 'per_net': {}}):
                aesthetic_flow.select_layout(path, iterations=30, structure_layout='auto')
            self.assertEqual(len(calls), 2)
            self.assertTrue(all('seconds' not in call and 'max_candidates' not in call
                                and 'hpwl_reference' not in call for call in calls))


if __name__ == '__main__':
    unittest.main()
