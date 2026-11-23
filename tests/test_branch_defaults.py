"""Main-flow promotion preserves saved coordinates and explicit legacy choices."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from netlist2schematic.flow import run_flow
from netlist2schematic.plugins.organization import branch_layout


class BranchDefaultTests(unittest.TestCase):
    def test_cli_default_auto_and_explicit_choices_reach_main_flow(self):
        for flags, expected in (([], 'auto'), (['--branch-layout', 'auto'], 'auto'),
                                (['--branch-layout', 'off'], 'off'),
                                (['--column-layout', 'off'], 'off')):
            with self.subTest(flags=flags), patch.object(run_flow, 'run') as run:
                self.assertEqual(run_flow.main(['unused.cir', *flags]), 0)
                self.assertEqual(run.call_args.kwargs['branch_layout'], expected)

    def test_baseline_legacy_and_other_experiments_do_not_inherit_default(self):
        options = [('--mode', 'baseline'), ('--layout-style', 'legacy'),
                   ('--structure-layout', 'auto'), ('--symmetry-layout', 'auto'),
                   ('--region-layout', 'auto'), ('--readability-layout', 'auto'),
                   ('--attachment-layout', 'passive'), ('--attachment-layout', 'all')]
        for flags in options:
            with self.subTest(flags=flags), patch.object(run_flow, 'run') as run:
                self.assertEqual(run_flow.main(['unused.cir', *flags]), 0)
                self.assertEqual(run.call_args.kwargs['branch_layout'], 'off')

    def test_saved_operations_never_initialize_layout_by_default(self):
        for flag, method in (('--reroute-placement', 'reroute_placement'),
                             ('--simplify-placement', 'simplify_placement')):
            with self.subTest(flag=flag), patch.object(run_flow, method) as saved, \
                    patch.object(branch_layout, 'initialize', side_effect=AssertionError('saved relayout')):
                self.assertEqual(run_flow.main([flag, 'saved.json']), 0)
                saved.assert_called_once()
                self.assertNotIn('branch_layout', saved.call_args.kwargs)

    def test_saved_operations_reject_explicit_relayout(self):
        for flag, method in (('--reroute-placement', 'reroute_placement'),
                             ('--simplify-placement', 'simplify_placement')):
            with self.subTest(flag=flag), patch.object(run_flow, method) as saved, \
                    redirect_stderr(StringIO()), self.assertRaises(SystemExit) as caught:
                run_flow.main([flag, 'saved.json', '--branch-layout', 'auto'])
            self.assertEqual(caught.exception.code, 2)
            saved.assert_not_called()

    def test_explicit_conflicting_combinations_still_fail_before_layout(self):
        for flags in (('--mode', 'baseline'), ('--layout-style', 'legacy'),
                      ('--column-layout', 'off'), ('--region-layout', 'auto')):
            with self.subTest(flags=flags), patch.object(run_flow, 'run') as run, \
                    redirect_stderr(StringIO()), self.assertRaises(SystemExit) as caught:
                run_flow.main(['unused.cir', '--branch-layout', 'auto', *flags])
            self.assertEqual(caught.exception.code, 2)
            run.assert_not_called()

    def test_python_api_resolves_same_default_without_changing_legacy_paths(self):
        class BeforeLayout(RuntimeError):
            pass

        source = Path(__file__).parent / 'fixtures/aesthetic-baseline/4.cir'
        for options, expected in (({}, 'auto'), ({'branch_layout': 'off'}, 'off'),
                                  ({'column_layout': 'off'}, 'off'),
                                  ({'mode': 'baseline'}, 'off'),
                                  ({'layout_style': 'legacy'}, 'off'),
                                  ({'structure_layout': 'auto'}, 'off')):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temporary, \
                    patch.object(run_flow, '_normal_result', side_effect=BeforeLayout) as normal, \
                    redirect_stdout(StringIO()), self.assertRaises(BeforeLayout):
                run_flow.run(source, out_dir=temporary, fallback='off', **options)
            self.assertEqual(normal.call_args.kwargs.get('branch_layout', 'off'), expected)


if __name__ == '__main__':
    unittest.main()
