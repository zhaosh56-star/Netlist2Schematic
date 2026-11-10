"""Focused checks for actual-code proposals, without real model requests."""
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import coding_agent as ca


class Response:
    def __init__(self, value): self.value = json.dumps(value).encode()
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, limit): return self.value


class CodingAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'engine').mkdir()
        self.entry = self.root / 'engine' / 'candidate_entry.py'
        self.entry.write_text('DEFAULT_PROFILE = "baseline"\n', encoding='utf-8')

    def tearDown(self): self.temp.cleanup()

    def test_patch_changes_real_code_and_records_diff(self):
        report = ca.apply_edits(self.root, [{'path': 'engine/candidate_entry.py', 'content': 'DEFAULT_PROFILE = "reference"\n'}])
        self.assertIn('reference', self.entry.read_text())
        self.assertIn('reference', report['diff'])
        self.assertNotEqual(report['files'][0]['before_sha256'], report['files'][0]['after_sha256'])

    def test_disallowed_or_invalid_patch_is_rejected_before_any_write(self):
        for path, text in [('engine/../rules.py', 'x=1'), ('engine/routing_rules.py', 'x=1'), ('verifier.py', 'x=1'), ('engine/strategies/new.py', 'not valid code!')]:
            with self.subTest(path=path), self.assertRaises(ca.CodingAgentError):
                ca.apply_edits(self.root, [{'path': 'engine/candidate_entry.py', 'content': 'x=2\n'}, {'path': path, 'content': text}])
            self.assertIn('baseline', self.entry.read_text())

    def test_smoke_is_explicitly_synthetic(self):
        agent = ca.CodingAgent(mode='smoke')
        card = agent.analyse({})
        report = agent.implement(card, self.root, {})
        self.assertTrue(card['synthetic'])
        self.assertTrue(report['synthetic'])
        self.assertEqual(report['requests'], [])
        self.assertIn('reference', self.entry.read_text())

    def test_http_implementation_applies_checked_model_code(self):
        result = {'summary': 'new helper', 'edits': [{'path': 'engine/strategies/helper.py', 'content': 'def improve(x):\n    return x\n'}]}
        config = {'roles': {'analyst': {'provider': 'anthropic', 'model': 'test-model', 'api_key_env': 'AGENT_TEST_KEY'}}}
        agent = ca.CodingAgent(config)
        response = Response({'content': [{'type': 'text', 'text': json.dumps(result)}]})
        with patch.dict(os.environ, {'AGENT_TEST_KEY': 'not-real-key'}), patch('urllib.request.urlopen', return_value=response):
            report = agent.implement({'change_hypothesis': 'helper'}, self.root, {})
        self.assertTrue((self.root / 'engine/strategies/helper.py').is_file())
        self.assertFalse(report['synthetic'])
        self.assertNotIn('not-real-key', json.dumps(report))

    def test_cli_uses_tools_disabled_and_applies_json_edits(self):
        config = {'roles': {'analyst': {'model': 'sonnet'}}}
        result = {'summary': 'code change', 'edits': [{'path': 'engine/candidate_entry.py', 'content': 'DEFAULT_PROFILE="reference"\n'}]}
        completed = type('Completed', (), {'returncode': 0, 'stdout': json.dumps({'result': json.dumps(result)}), 'stderr': ''})()
        with patch('shutil.which', return_value='claude'), patch('subprocess.run', return_value=completed) as invoke:
            report = ca.CodingAgent(config, mode='claude').implement({}, self.root, {})
        command = invoke.call_args.args[0]
        self.assertEqual(command[command.index('--tools') + 1], '')
        self.assertIn('reference', self.entry.read_text())
        self.assertEqual(report['mode'], 'claude')

    def test_analyst_reads_baseline_sa_helpers_and_frozen_rules(self):
        (self.root / 'engine/place-and-route.py').write_text('def sa():\n    return 12\n', encoding='utf-8')
        (self.root / 'engine/routing_rules.py').write_text('MIN_GAP = 6\n', encoding='utf-8')
        helper = self.root / 'engine/strategies/accepted.py'
        helper.parent.mkdir()
        helper.write_text('def accepted():\n    return 42\n', encoding='utf-8')
        agent = ca.CodingAgent()
        card = {'failure_mode_desc': 'wire detour', 'change_hypothesis': 'reuse helper'}
        with patch.object(agent, '_call', side_effect=[
            {'read_files': ['engine/place-and-route.py', 'engine/routing_rules.py', 'engine/strategies/accepted.py']}, card,
        ]) as invoke:
            self.assertEqual(agent.analyse({}, workspace=self.root), card)
        second = invoke.call_args_list[1].args[2]
        self.assertIn('return 12', second['code_files']['engine/place-and-route.py'])
        self.assertIn('return 42', second['code_files']['engine/strategies/accepted.py'])
        self.assertIn('MIN_GAP = 6', second['code_files']['engine/routing_rules.py'])
        self.assertFalse(ca.allowed_path('engine/routing_rules.py'))
        self.assertEqual(agent.source_reads[0]['status'], 'read')

    def test_implementer_reads_and_continues_editing_an_existing_helper(self):
        helper = self.root / 'engine/strategies/accepted.py'
        helper.parent.mkdir()
        helper.write_text('value = 1\n', encoding='utf-8')
        agent = ca.CodingAgent()
        with patch.object(agent, '_call', side_effect=[
            {'read_files': ['engine/strategies/accepted.py']},
            {'summary': 'continue previous mechanism', 'edits': [{'path': 'engine/strategies/accepted.py', 'content': 'value = 2\n'}]},
        ]) as invoke:
            report = agent.implement({}, self.root, {})
        self.assertEqual(invoke.call_args_list[1].args[2]['code_files']['engine/strategies/accepted.py'], 'value = 1\n')
        self.assertEqual(helper.read_text(), 'value = 2\n')
        self.assertEqual(report['source_reads'][0]['status'], 'read')

    def test_source_request_error_returns_to_model_without_partial_or_outside_content(self):
        (self.root / 'engine/big.py').write_text('#' + 'large source' * 1000, encoding='utf-8')
        agent = ca.CodingAgent({'max_context_chars': 3000})
        card = {'failure_mode_desc': 'test', 'change_hypothesis': 'test'}
        with patch.object(agent, '_call', side_effect=[
            {'read_files': ['engine/../secret.py']}, {'read_files': ['engine/big.py']}, card,
        ]) as invoke:
            agent.analyse({}, workspace=self.root)
        self.assertIn('source_read_error', invoke.call_args_list[1].args[2])
        self.assertIn('source_read_error', invoke.call_args_list[2].args[2])
        self.assertEqual(invoke.call_args_list[2].args[2]['code_files'], {})
        self.assertEqual([event['status'] for event in agent.source_reads], ['error', 'error'])

    def test_source_round_limit_is_bounded(self):
        agent = ca.CodingAgent({'max_source_rounds': 1})
        with patch.object(agent, '_call', return_value={'read_files': ['engine/candidate_entry.py']}), self.assertRaises(ca.CodingAgentError):
            agent.analyse({}, workspace=self.root)


if __name__ == '__main__': unittest.main()
