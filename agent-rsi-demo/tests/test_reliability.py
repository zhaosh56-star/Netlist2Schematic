from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch, MagicMock

from input_bundle import inventory, check_bundle
from runtime_control import RuntimeControl, DeadlineExceeded, OperationCancelled
from coding_agent import CodingAgent, CodingAgentError
from svg_audit import audit
from rsi_core import Version, EvalResult, run_core, EvaluationUnavailable
from circuit_adapter import CircuitAdapter, tree_hash
from test_rsi_core import SyntheticAdapter
import run_demo


ROOT = Path(__file__).resolve().parents[1]


class RecoveryTests(unittest.TestCase):
    def test_atomic_state_write_retries_transient_windows_sharing_lock(self):
        from atomic_io import write_json
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            path.write_text('{"version":"old"}')
            replace = Path.replace
            calls = []
            def transient(source, target):
                calls.append(1)
                if len(calls) < 3:
                    raise PermissionError('temporary sharing lock')
                return replace(source, target)
            with patch.object(Path, 'replace', transient):
                write_json(path, {'version': 'new'})
            self.assertEqual(json.loads(path.read_text())['version'], 'new')
            self.assertEqual(len(calls), 3)

    def test_baseline_cannot_overwrite_existing_adopted_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = b'{"final_version":"already-adopted"}'
            (root / 'summary.json').write_bytes(raw)
            self.assertEqual(run_demo.main(['--stage', 'baseline', '--out', tmp]), 1)
            self.assertEqual((root / 'summary.json').read_bytes(), raw)
            self.assertFalse((root / 'last-error.json').exists())

    def test_actual_core_resumes_adopted_version_for_additional_rounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = run_core(SyntheticAdapter(), Version('seed'), search=['s'], validation=['a', 'b'],
                             state_dir=tmp, max_rounds=1)
            second = run_core(SyntheticAdapter(), Version('seed'), search=['s'], validation=['a', 'b'],
                              state_dir=tmp, max_rounds=3, resume=True)
            self.assertEqual(first['final_version'], second['final_version'])
            self.assertEqual(len(second['ledger']['rounds']), 3)
            self.assertEqual(len(second['lineage']), 2)
            self.assertTrue((Path(tmp) / 'round-limit-history.json').is_file())

    def test_analyst_schema_error_is_repaired_and_recorded(self):
        agent = CodingAgent({'repair_attempts': 1})
        events = []
        agent.recorder = events.append
        card = {'failure_mode_desc': 'detour', 'change_hypothesis': 'new routing decision'}
        with patch.object(agent, '_call', side_effect=[{'bad': True}, card]) as invoke:
            self.assertEqual(agent.analyse({}), card)
        self.assertIn('repair_feedback', invoke.call_args_list[1].args[2])
        self.assertTrue(any(e['status'] == 'repair_needed' for e in events))

    def test_invalid_python_is_repaired_without_partial_first_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'engine' / 'candidate_entry.py'
            path.parent.mkdir()
            path.write_text('value=1\n', encoding='utf-8')
            agent = CodingAgent({'repair_attempts': 1})
            responses = [
                {'summary': 'broken', 'edits': [{'path': 'engine/candidate_entry.py', 'content': 'def broken('}]},
                {'summary': 'repaired', 'edits': [{'path': 'engine/candidate_entry.py', 'content': 'value=2\n'}]},
            ]
            with patch.object(agent, '_call', side_effect=responses):
                report = agent.implement({}, Path(tmp), {})
            self.assertEqual(path.read_text(), 'value=2\n')
            self.assertEqual(report['summary'], 'repaired')

    def test_from_version_checks_saved_source_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = root / 'source'
            engine.mkdir()
            (engine / 'candidate_entry.py').write_text('x=1\n')
            (engine / 'symbol-geometry.json').write_text('{}')
            digest = tree_hash(engine)
            target = root / 'versions' / digest[:20] / 'engine'
            import shutil
            shutil.copytree(engine, target)
            (root / 'summary.json').write_text(json.dumps({'final_version': digest}))
            self.assertEqual(run_demo.resolve_engine(root), target)
            (target / 'candidate_entry.py').write_text('x=2\n')
            with self.assertRaises(ValueError):
                run_demo.resolve_engine(root)

    def test_endpoint_measures_seed_and_final_on_same_sealed_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = ROOT / 'agent-config.example.json'
            identity = {'schema': 'circuit-rsi-run-v2', 'mode': 'smoke', 'suite': 'benchmarks/suite.json',
                        'seed_version': 'seed', 'iterations': 200, 'hours': 1, 'regression': 'compat',
                        'case_timeout_seconds': 180, 'config_path': str(config)}
            (root / 'run.json').write_text(json.dumps(identity))
            (root / 'summary.json').write_text(json.dumps({'status': 'demo_round_limit', 'final_version': 'improved'}))
            adapter = MagicMock()
            adapter.create_seed.return_value = Version('seed')
            adapter.cases = {'s': {'split': 'search'}, 'v1': {'split': 'validation'},
                             'v2': {'split': 'validation'}, 't': {'split': 'test'}}
            def measure(version, ids, k, **kwargs):
                score = .2 if version.content_hash == 'seed' else .8
                return EvalResult({t: score for t in ids}, {}, {}, {}, context={'measurements': {t: [] for t in ids}})
            adapter.evaluate.side_effect = measure
            with patch.object(run_demo, 'CircuitAdapter', return_value=adapter), patch.object(run_demo, 'build_report', return_value=root / 'report.html'):
                self.assertEqual(run_demo.main(['--stage', 'endpoint', '--out', tmp]), 0)
            result = json.loads((root / 'endpoint.json').read_text(encoding='utf-8'))
            self.assertAlmostEqual(result['paired_delta']['t'], .6)
            self.assertEqual([call.args[0].content_hash for call in adapter.evaluate.call_args_list], ['improved', 'seed'])
            self.assertTrue(all(call.args[1] == ['t'] and call.kwargs['purpose'] == 'sealed' for call in adapter.evaluate.call_args_list))


class DeadlineTests(unittest.TestCase):
    def test_owned_http_worker_preserves_utf8_prompts_and_responses(self):
        observed = []
        card = {'failure_mode_desc': '\u7ebf\u8def\u7ed5\u8fdc', 'change_hypothesis': '\u6539\u5584\u6446\u4ef6'}
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                observed.append(json.loads(self.rfile.read(int(self.headers['Content-Length'])).decode('utf-8')))
                data = json.dumps({'content': [{'type': 'text', 'text': json.dumps(card, ensure_ascii=False)}]}, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        control = RuntimeControl()
        config = {'roles': {'analyst': {'provider': 'anthropic', 'model': 'local-test',
                  'base_url': 'http://127.0.0.1:' + str(server.server_port), 'api_key_env': 'TEST_LOCAL_AGENT_KEY'}}}
        agent = CodingAgent(config)
        agent.bind_runtime(control)
        try:
            with patch.dict(os.environ, {'TEST_LOCAL_AGENT_KEY': 'dummy-local-only'}):
                self.assertEqual(agent.analyse({'goal': '\u751f\u6210\u7535\u8def\u56fe'}), card)
            self.assertIn('\u751f\u6210\u7535\u8def\u56fe', observed[0]['messages'][0]['content'])
        finally:
            control.cancel()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_wall_deadline_kills_owned_child(self):
        deadline = time.monotonic() + .2
        control = RuntimeControl(lambda: deadline - time.monotonic())
        started = time.monotonic()
        with self.assertRaises(DeadlineExceeded):
            control.run([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=30,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(control._processes)

    def test_cancellation_terminates_a_running_child(self):
        control = RuntimeControl()
        timer = threading.Timer(.2, control.cancel)
        timer.start()
        try:
            with self.assertRaises(OperationCancelled):
                control.run([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=30)
        finally:
            timer.cancel()
        self.assertFalse(control._processes)


class BundleTests(unittest.TestCase):
    def test_recursive_include_and_empty_dependency_are_frozen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'main.cir').write_text('.include "parts.inc"\n.include "empty.inc"\n')
            (root / 'parts.inc').write_text('R1 a b 1k\n')
            (root / 'empty.inc').write_text('* initially empty\n')
            bundle = inventory(root / 'main.cir', boundary=root)
            self.assertEqual(len(bundle['files']), 3)
            (root / 'parts.inc').write_text('R1 a b 999k\n')
            with self.assertRaises(ValueError):
                check_bundle(bundle)

    def test_outside_include_and_recursion_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'main.cir').write_text('.include "../outside.inc"\n')
            with self.assertRaises(ValueError):
                inventory(root / 'main.cir', boundary=root)
            (root / 'main.cir').write_text('.include "main.cir"\n')
            with self.assertRaises(ValueError):
                inventory(root / 'main.cir', boundary=root)


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.raw = '''<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120" viewBox="-20 -20 120 120">
<rect x="-20" y="-20" width="120" height="120" fill="#fff"/>
<polyline data-net="n" points="10,5 40,5" stroke="#111" stroke-width="2.5" fill="none"/>
<g data-component="R1"><rect x="0" y="0" width="10" height="10" stroke="#111" fill="none"/>
<text x="20" y="35" font-size="12" fill="#111">R1</text></g></svg>'''
        self.reference = self.root / 'reference.svg'
        self.reference.write_text(self.raw, encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_label_only_change_is_measured_without_changing_geometry(self):
        changed = self.root / 'changed.svg'
        changed.write_text(self.raw.replace('x="20" y="35"', 'x="1" y="7"'), encoding='utf-8')
        bodies = {'R1': (0, 0, 10, 10)}
        initial = audit(self.reference, self.reference, bodies, sorted)
        new = audit(changed, self.reference, bodies, sorted)
        self.assertGreater(new['penalty'], initial['penalty'])
        self.assertGreater(new['label_body_hits'], 0)

    def test_empty_svg_and_hidden_or_changed_symbols_are_rejected(self):
        changed = self.root / 'changed.svg'
        for raw in ('<svg viewBox="0 0 10 10"/>', self.raw.replace('data-component="R1"', 'data-component="R1" opacity="0"'),
                    self.raw.replace('width="10" height="10"', 'width="11" height="10"')):
            changed.write_text(raw, encoding='utf-8')
            with self.assertRaises(ValueError):
                audit(changed, self.reference, {'R1': (0, 0, 10, 10)}, sorted)


if __name__ == '__main__':
    unittest.main()
