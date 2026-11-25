from argparse import Namespace
import json
from pathlib import Path
import tempfile
import unittest

from integrations.auto_canvas_huyangc.cli import REPOSITORY_ROOT, run
from integrations.auto_canvas_huyangc.editable_adapter import validate_layout_budget_capability
from integrations.auto_canvas_huyangc.web_jobs import refusal_message


class LayoutBudgetAdapterTests(unittest.TestCase):
    def test_adaptive_engine_is_checked_before_starting_a_job(self):
        validate_layout_budget_capability(REPOSITORY_ROOT,'adaptive')
        with tempfile.TemporaryDirectory() as temporary:
            validate_layout_budget_capability(Path(temporary),'legacy')
            with self.assertRaisesRegex(ValueError,'does not support layout_budget'):
                validate_layout_budget_capability(Path(temporary),'adaptive')

    def test_legacy_complete_cli_does_not_silently_enable_adaptive_budget(self):
        with self.assertRaisesRegex(ValueError,'requires the editable'):
            run(Namespace(result_policy='complete',layout_budget='adaptive'))

    def test_attachment_failures_are_shown_without_traceback_or_budget_banner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            message='No complete safe all-device layout; candidate 3: attachment placement no_legal_slot'
            (root/'adapter-report.json').write_text(json.dumps(dict(status='failed',message=message)),encoding='utf8')
            self.assertEqual(refusal_message(root,ValueError('core-flow failed; Candidate 2: normal budget=83s')),message)


if __name__=='__main__':unittest.main()
