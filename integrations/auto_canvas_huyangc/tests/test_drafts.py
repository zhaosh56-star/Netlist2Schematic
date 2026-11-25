from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from integrations.auto_canvas_huyangc.drafts import selection_key,choose_payloads,write_atomic


def payload(candidate='2',complete=False,progress=.5,structure=None):
    return dict(selected_candidate=candidate,source_hash='source',result_kind='complete' if complete else 'editable_draft',
        validation=dict(geometry_valid=True,complete=complete),metrics=dict(bends=50,crossings=10,wirelength=1000),
        draft=dict(structure_rank=structure or [0,0,0,0],connected_terminal_fraction=progress,
            completed_nets=3,remaining_connections=4))


class DraftSelectionTests(unittest.TestCase):
    def test_complete_result_always_outranks_any_partial_progress(self):
        self.assertLess(selection_key(payload(complete=True)),selection_key(payload(progress=1)))

    def test_drafts_compare_structure_then_real_terminal_progress(self):
        self.assertLess(selection_key(payload(progress=.6)),selection_key(payload(progress=.4)))
        self.assertLess(selection_key(payload(progress=.2)),selection_key(payload(progress=.9,structure=[1,0,0,0])))

    def test_atomic_checkpoints_ignore_incomplete_temporary_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            write_atomic(folder/'candidate-2-wire-plan.json',payload())
            (folder/'candidate-3-wire-plan.json.tmp').write_text('{',encoding='utf8')
            self.assertEqual(choose_payloads(folder,'source')[0]['selected_candidate'],'2')
            self.assertFalse((folder/'candidate-2-wire-plan.json.tmp').exists())

    def test_foreign_sources_candidate_one_and_unsafe_artifacts_are_rejected(self):
        for change in ('source','candidate','geometry'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as temporary:
                value=payload()
                if change=='source':value['source_hash']='other'
                elif change=='candidate':value['selected_candidate']='1'
                else:value['validation']['geometry_valid']=False
                write_atomic(Path(temporary)/'candidate-2-wire-plan.json',value)
                with self.assertRaises(ValueError):choose_payloads(temporary,'source')

    def test_empty_checkpoint_set_does_not_invent_a_drawing(self):
        with tempfile.TemporaryDirectory() as temporary:self.assertIsNone(choose_payloads(temporary,'source'))


if __name__=='__main__':unittest.main()
