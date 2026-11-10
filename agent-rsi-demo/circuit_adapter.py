"""Circuit domain adapter for the vendored, unmodified RSI v4.1 method core."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import difflib

from rsi_core import Card, Diff, EvalResult, SystemAdapter, Version
from rag_eval.optimizer.adapter import (AgentRequestFailed, EvaluationUnavailable,
                                       BudgetExhausted, EvaluationCancelled, ImplementationDeclined)
from coding_agent import CodingAgent
from runtime_control import RuntimeControl, DeadlineExceeded, OperationCancelled
from input_bundle import inventory, check_bundle
from regression_gate import run_gate
from atomic_io import write_json


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def runtime_files(engine):
    engine = Path(engine)
    files = list(engine.glob('*.py')) + [engine / 'symbol-geometry.json']
    files += list((engine / 'strategies').rglob('*.py')) if (engine / 'strategies').is_dir() else []
    files += list((engine / 'analog-canvas-main' / 'packages' / 'components' / 'definitions').glob('*.json'))
    files += [p for p in (engine / 'analog-canvas-main' / 'LICENSE.md',) if p.is_file()]
    return sorted(files)


def copy_runtime(source, destination):
    source, destination = Path(source), Path(destination)
    for path in runtime_files(source):
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())


def tree_hash(engine):
    engine = Path(engine)
    entries = [(p.relative_to(engine).as_posix(), sha(p)) for p in runtime_files(engine)]
    return hashlib.sha256(json.dumps(entries, separators=(',', ':')).encode()).hexdigest()


def load_suite(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    cases, input_identities = {}, set()
    for case in data['cases']:
        name = case['id']
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', name) or name in cases:
            raise ValueError('case IDs must be unique simple names')
        source = (root / case['input']).resolve()
        if not source.is_relative_to(root / 'benchmarks' / 'corpus') or not source.is_file():
            raise ValueError('case input must be a bundled benchmark corpus file')
        if case['split'] not in ('search', 'validation', 'test'):
            raise ValueError('unknown task split')
        bundle = inventory(source, boundary=root / 'benchmarks' / 'corpus')
        identity = (tuple(sorted(bundle['files'].values())), (case.get('cell') or '').lower())
        if identity in input_identities:
            raise ValueError('duplicate input/cell across benchmark tasks or partitions')
        input_identities.add(identity)
        cases[name] = {**case, 'source': str(source), 'source_sha256': sha(source), 'bundle': bundle}
    if len([c for c in cases.values() if c['split'] == 'validation']) < 2:
        raise ValueError('RSI v4 calibration requires at least two validation tasks')
    return cases, int(data.get('seed', 20261029))


class CircuitAdapter(SystemAdapter):
    def __init__(self, root, output, *, suite, agent_config, mode='api', iterations=200,
                 case_timeout=180, max_source_chars=120000, regression='compat'):
        self.root, self.output = Path(root).resolve(), Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.cases, self.seed_value = load_suite(self.root, suite)
        self.iterations, self.case_timeout = iterations, case_timeout
        self.agent, self.mode = CodingAgent(agent_config, mode=mode), mode
        self.max_source_chars = max_source_chars
        if regression not in ('compat', 'full', 'off'):
            raise ValueError('unknown regression mode')
        self.regression = regression
        self.versions = {}
        self._lock = threading.RLock()
        self._regression_lock = threading.RLock()
        self._process_limit = threading.BoundedSemaphore(1)
        self._remaining = None
        self.outer_remaining = None
        self.runtime = RuntimeControl()
        self.agent.bind_runtime(self.runtime, self._agent_event)
        self._history = []
        # A logical boundary: it detects accidental/tampered protected inputs;
        # it is deliberately not represented as an operating-system sandbox.
        paths = runtime_files(self.root / 'reference' / 'engine')
        paths += [self.root / 'verify_layout.py', self.root / 'circuit_adapter.py', self.root / 'coding_agent.py', self.root / 'rsi_core.py', self.root / 'OPTIMIZATION_SCOPE.md', self.root / 'PROJECT_CONTEXT.md', Path(suite).resolve()]
        paths += list((self.root / 'vendor' / 'rsi' / 'src').rglob('*.py'))
        paths += [self.root / name for name in ('input_bundle.py', 'svg_audit.py', 'runtime_control.py',
                  'model_http_worker.py', 'regression_gate.py', 'run_demo.py', 'atomic_io.py')]
        paths += [p for p in (self.root / 'reference' / 'engine' / 'tests').rglob('*')
                  if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc']
        for case in self.cases.values():
            paths += [Path(case['bundle']['boundary']) / name for name in case['bundle']['files']]
        self.protected = {str(p): sha(p) for p in paths}
        manifest = self.output / 'protected-manifest.json'
        if manifest.exists() and json.loads(manifest.read_text(encoding='utf-8')) != self.protected:
            raise EvaluationUnavailable('protected source or dataset differs from this frozen run')
        write_json(manifest, self.protected)

    def check_runtime(self):
        try:
            self.runtime.check()
        except DeadlineExceeded as exc:
            raise BudgetExhausted(str(exc)) from exc
        except OperationCancelled as exc:
            raise EvaluationCancelled(str(exc)) from exc
        for path, digest in self.protected.items():
            if not Path(path).is_file() or sha(path) != digest:
                raise EvaluationUnavailable('protected evaluator, symbols or benchmark inputs changed')
        for case in self.cases.values():
            try:
                check_bundle(case['bundle'])
            except (ValueError, OSError) as exc:
                raise EvaluationUnavailable(str(exc)) from exc

    def configure_runtime(self, *, remaining, max_concurrent_executions):
        if self.outer_remaining:
            outer, inner = self.outer_remaining, remaining
            remaining = (lambda: min(outer(), inner())) if inner else outer
        self._remaining = remaining
        self.runtime = RuntimeControl(remaining)
        self.agent.bind_runtime(self.runtime, self._agent_event)

    def cancel_pending(self):
        self.runtime.cancel()

    def stop_polling(self):
        self.runtime.cancel()

    def _run_process(self, command, **kwargs):
        while not self._process_limit.acquire(timeout=.1):
            self.check_runtime()
        try:
            return self.runtime.run(command, **kwargs)
        except DeadlineExceeded as exc:
            raise BudgetExhausted(str(exc)) from exc
        except OperationCancelled as exc:
            raise EvaluationCancelled(str(exc)) from exc
        finally:
            self._process_limit.release()

    def _agent_event(self, event):
        write_json(self.output / 'agent' / ('event-' + uuid.uuid4().hex + '.json'), event)

    def _feedback(self, diagnostic):
        path = self.output / 'search-feedback.json'
        with self._lock:
            rows = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
            rows.append(diagnostic)
            rows = rows[-12:]
            while len(rows) > 1 and len(json.dumps(rows, ensure_ascii=False)) > 60000:
                rows.pop(0)
            if len(json.dumps(rows, ensure_ascii=False)) > 60000:
                rows = [{'scope': diagnostic.get('scope'), 'diagnostic_excerpt': json.dumps(diagnostic, ensure_ascii=False)[:50000], 'truncated': True}]
            write_json(path, rows)

    def regression_check(self, version):
        with self._regression_lock:
            return self._regression_check(version)

    def _regression_check(self, version):
        directory = self.output / 'regression' / version.content_hash[:20]
        try:
            return run_gate(self, self.engine_for(version), directory, self.regression)
        except subprocess.TimeoutExpired:
            result = {'passed': False, 'mode': self.regression, 'diagnostic': 'candidate compatibility tests timed out'}
            write_json(directory / 'regression.json', result)
            return result

    def create_seed(self, source_engine=None):
        engine = self.output / 'seed' / 'engine'
        if not engine.exists():
            copy_runtime(source_engine or self.root / 'engine', engine)
        return self._freeze(engine, parent=None, metadata={'kind': 'baseline', 'from_engine': str(source_engine) if source_engine else None})

    def _freeze(self, engine, parent, metadata):
        digest = tree_hash(engine)
        target = self.output / 'versions' / digest[:20] / 'engine'
        with self._lock:
            identity_path = target.parent / 'version.json'
            if identity_path.exists() and json.loads(identity_path.read_text(encoding='utf-8'))['hash'] != digest:
                raise EvaluationUnavailable('source hash directory collision')
            if not target.exists():
                copy_runtime(engine, target)
            version = Version(digest, metadata={'engine': str(target), 'parent': parent, **metadata})
            self.versions[digest] = version
            write_json(target.parent / 'version.json', {'hash': digest, 'parent': parent, 'metadata': metadata})
        return version

    def engine_for(self, version):
        path = self.output / 'versions' / version.content_hash[:20] / 'engine'
        if not path.is_dir() or tree_hash(path) != version.content_hash:
            raise EvaluationUnavailable('candidate snapshot missing or modified after freezing')
        return path

    def workspace(self, version, card=None):
        self.check_runtime()
        directory = self.output / 'workspaces' / uuid.uuid4().hex[:20]
        directory.mkdir(parents=True, exist_ok=False)
        copy_runtime(self.engine_for(version), directory / 'engine')
        return {'directory': str(directory), 'parent': version.content_hash}

    def _code_context(self, engine):
        # Full source of the small entry and selected core modules, not fragments
        # masquerading as complete editable files. The agent can also add helpers.
        helpers = [p.relative_to(engine).as_posix() for p in sorted((Path(engine) / 'strategies').rglob('*.py'))] if (Path(engine) / 'strategies').is_dir() else []
        names = ['candidate_entry.py', *helpers, 'netlist_core.py', 'place-layered.py', 'place-and-route.py', 'run_flow.py', 'structured_placement.py', 'orthogonal_router.py',
                 'netlist_core.py', 'route_tracks.py', 'route_patterns.py', 'passive_placement.py',
                 'sa_render.py', 'presentation.py', 'layout_relations.py']
        files, remaining = {}, self.max_source_chars
        for name in names:
            path = Path(engine) / name
            if path.is_file():
                content = path.read_text(encoding='utf-8-sig')
                if len(content) <= remaining:
                    files['engine/' + name] = content
                    remaining -= len(content)
        return {'code_files': files, 'available_files': [p.relative_to(engine).as_posix() for p in runtime_files(engine)],
                'source_budget_chars': self.max_source_chars,
                'source_manifest': [{'path': 'engine/' + p.relative_to(engine).as_posix(), 'characters': len(p.read_text(encoding='utf-8-sig'))} for p in runtime_files(engine) if p.suffix == '.py'],
                'project_context': (self.root / 'PROJECT_CONTEXT.md').read_text(encoding='utf-8-sig'),
                'improvement_scope': (self.root / 'OPTIMIZATION_SCOPE.md').read_text(encoding='utf-8-sig')}

    def analyse(self, search_set_trajectories, n_cards, *, case_roles=None, parent=None,
                round_id=None, history=None, no_adoption_rounds=0, case_scores=None, calibration_advice=None):
        self.check_runtime()
        context = {'search_trajectories': search_set_trajectories, 'case_roles': case_roles or {},
                   'round': round_id, 'history_labels': history or [], 'no_adoption_rounds': no_adoption_rounds,
                   'search_scores': case_scores or {}, 'calibration_advice': calibration_advice,
                   **self._code_context(self.engine_for(parent))}
        feedback = self.output / 'search-feedback.json'
        context['search_repair_feedback'] = json.loads(feedback.read_text(encoding='utf-8')) if feedback.exists() else []
        try:
            draft = self.agent.analyse(context, workspace=self.engine_for(parent).parent)
        except (DeadlineExceeded, OperationCancelled):
            self.check_runtime()
            raise
        except Exception as exc:
            raise AgentRequestFailed(f'coding analyst request failed: {type(exc).__name__}: {exc}') from exc
        write_json(self.output / 'agent' / ('analysis-' + uuid.uuid4().hex + '.json'), draft)
        return [Card(draft['failure_mode_desc'], draft['change_hypothesis'], ref=draft.get('ref'))][:n_cards]

    def implement(self, card, workspace):
        self.check_runtime()
        path = Path(workspace['directory'])
        report = None
        repairs = 0 if self.mode == 'smoke' else int(self.agent.config.get('execution_repair_attempts', 1))
        for attempt in range(repairs + 1):
            context = self._code_context(path / 'engine')
            if report is not None:
                context['execution_repair_feedback'] = failure
            try:
                report = self.agent.implement({'failure_mode_desc': card.failure_mode_desc,
                                               'change_hypothesis': card.change_hypothesis}, path, context)
            except (DeadlineExceeded, OperationCancelled):
                self.check_runtime()
                raise
            except Exception as exc:
                write_json(path / 'failed-agent-report.json', {'error': str(exc), 'attempt': attempt})
                raise AgentRequestFailed(f'coding implementation request failed: {type(exc).__name__}: {exc}') from exc
            provisional = self._freeze(path / 'engine', workspace['parent'], {'kind': 'preflight'})
            gate = self.regression_check(provisional)
            smoke_tasks = sorted(t for t, c in self.cases.items() if c['split'] == 'search')[:1]
            smoke = [self._attempt(provisional, task, 0) for task in smoke_tasks] if gate['passed'] else []
            if gate['passed'] and all(r['valid'] for r in smoke):
                report['preflight'] = {'regression': gate, 'search_smoke': smoke, 'repair_attempt': attempt}
                break
            failure = {'scope': 'public compatibility and search only', 'regression': gate,
                       'smoke': smoke, 'diff': report.get('diff', '')[:12000]}
            self._feedback(failure)
            write_json(path / ('failed-preflight-' + str(attempt) + '.json'), failure)
            if attempt == repairs:
                raise ImplementationDeclined({'reason': 'preflight_failed', 'summary': 'candidate failed compatibility/search smoke after bounded repair', 'evidence': failure})
        # Repair diffs must describe the whole change against the real parent.
        original = self.engine_for(Version(workspace['parent']))
        lines, files = [], []
        for current in runtime_files(path / 'engine'):
            relative = current.relative_to(path / 'engine')
            before = original / relative
            if current.suffix == '.py' and (not before.exists() or current.read_bytes() != before.read_bytes()):
                lines.extend(difflib.unified_diff(before.read_text(encoding='utf-8-sig').splitlines(True) if before.exists() else [],
                    current.read_text(encoding='utf-8-sig').splitlines(True), fromfile='parent/engine/' + relative.as_posix(),
                    tofile='candidate/engine/' + relative.as_posix()))
                files.append({'path': 'engine/' + relative.as_posix(), 'before_sha256': sha(before) if before.exists() else None,
                              'after_sha256': sha(current)})
        report['diff'] = ''.join(lines)
        report['files'] = files
        self.check_runtime()
        if self.agent.config.get('roles', {}).get('reviewer'):
            report['review'] = self.agent.review({'card': {'failure_mode_desc': card.failure_mode_desc, 'change_hypothesis': card.change_hypothesis}, 'diff': report.get('diff'), **context})
        write_json(path / 'agent-report.json', report)
        (path / 'change.diff').write_text(report.get('diff', ''), encoding='utf-8')
        if not report.get('files'):
            raise ValueError('implementer made no source change')
        return Diff(report['diff'], metadata={'workspace': str(path), 'report': report,
                                               'parent': workspace['parent']})

    def materialize(self, diff, parent):
        self.check_runtime()
        if diff.metadata['parent'] != parent.content_hash:
            raise ValueError('implementation parent mismatch')
        workspace = Path(diff.metadata['workspace'])
        result = self._freeze(workspace / 'engine', parent.content_hash,
                              {'report': diff.metadata['report'], 'diff': diff.content})
        if result.content_hash == parent.content_hash:
            raise ValueError('candidate source did not change')
        return result

    def features(self, trajectories, predicate):
        return {}  # No invented mechanical predicate or mechanism confirmation.

    def observable_safeguards(self):
        return ['hard_errors', 'crossings_ratio', 'wirelength_ratio']

    def _environment(self):
        env = dict(os.environ)
        for key in list(env):
            if ('KEY' in key.upper() and ('API' in key.upper() or 'SECRET' in key.upper())) or key.upper().endswith('_TOKEN'):
                env.pop(key, None)
        env.pop('PYTHONPATH', None)
        env.pop('ANALOG_CANVAS_ROOT', None)
        env['PYTHONIOENCODING'] = 'utf-8'
        return env

    def _attempt(self, version, name, attempt, *, profile=None, namespace='measurements'):
        self.check_runtime()
        engine = self.engine_for(version) if profile is None else self.root / 'reference' / 'engine'
        case = self.cases[name]
        base = self.output / namespace / version.content_hash[:20] / name / str(attempt)
        result_path = base / 'measurement.json'
        recipe = {'iterations': self.iterations, 'profile': profile or 'candidate', 'input_sha256': case['source_sha256'],
                  'bundle_sha256': case['bundle']['sha256'], 'seed': self.seed_value + attempt,
                  'score_policy': 'geometry-plus-native-svg-readability-v2'}
        if result_path.exists():
            cached = json.loads(result_path.read_text(encoding='utf-8'))
            if cached.get('recipe') != recipe:
                raise EvaluationUnavailable('cached measurement budget or input differs; use a new run directory')
            return cached
        generated, verified = base / 'generated', base / 'verified'
        if generated.exists() and any(generated.iterdir()):
            generated = base / ('generated-retry-' + uuid.uuid4().hex[:8])
        generated.mkdir(parents=True, exist_ok=True)
        seed = self.seed_value + attempt
        command = [sys.executable, '-B', str(engine / 'candidate_entry.py'), '--input', case['source'],
                   '--out', str(generated), '--seed', str(seed), '--iterations', str(self.iterations)]
        if profile:
            command += ['--profile', profile]
        if case.get('cell'):
            command += ['--cell', case['cell']]
        started = time.monotonic()
        timeout = min(self.case_timeout, max(0.1, self._remaining())) if self._remaining else self.case_timeout
        phase = 'generation'
        try:
            with (base / 'generation.log').open('w', encoding='utf-8') as log:
                process = self._run_process(command, cwd=engine, env=self._environment(), stdout=log,
                                         stderr=subprocess.STDOUT, timeout=timeout)
            placements = list(generated.glob('*-placement.json'))
            if process.returncode != 0 or len(placements) != 1 or len(list(generated.glob('*.svg'))) < 2:
                raise ValueError('generator failed or did not produce exactly one placement; inspect generation.log')
            verification_command = [sys.executable, '-B', str(self.root / 'verify_layout.py'),
                                    '--engine', str(self.root / 'reference' / 'engine'),
                                    '--layout', str(placements[0]), '--input', case['source'], '--out', str(verified),
                                    '--candidate-dir', str(generated), '--bundle', str(base / 'input-bundle.json')]
            write_json(base / 'input-bundle.json', case['bundle'])
            if case.get('cell'):
                verification_command += ['--cell', case['cell']]
            with (base / 'verification.log').open('w', encoding='utf-8') as log:
                phase = 'verification'
                verification = self._run_process(verification_command, cwd=self.root, env=self._environment(),
                                              stdout=log, stderr=subprocess.STDOUT, timeout=75)
            report_path = verified / 'verification.json'
            if not report_path.exists():
                raise EvaluationUnavailable('independent verifier did not produce a report')
            try:
                report = json.loads(report_path.read_text(encoding='utf-8'))
                if not isinstance(report, dict) or type(report.get('valid')) is not bool:
                    raise ValueError('missing verification status')
            except (ValueError, TypeError) as exc:
                raise EvaluationUnavailable('independent verifier returned malformed report') from exc
            if verification.returncode not in (0, 1, 2):
                raise EvaluationUnavailable('independent verifier crashed')
            if report.get('unavailable') or verification.returncode == 2:
                raise EvaluationUnavailable(report.get('error', 'verification unavailable'))
            if verification.returncode and report.get('valid'):
                raise ValueError('verifier exit status is inconsistent')
        except subprocess.TimeoutExpired:
            if phase == 'verification':
                write_json(base / 'infrastructure-error.json', {'error': 'independent verification timed out', 'retry_on_resume': True})
                raise EvaluationUnavailable('independent verification timed out; not a scored candidate failure')
            report = {'valid': False, 'score': 0.0, 'error': 'generation or verification timed out'}
        except ValueError as exc:
            report = {'valid': False, 'score': 0.0, 'error': str(exc)}
        self.check_runtime()
        if tree_hash(engine) != (version.content_hash if profile is None else self.reference_hash):
            raise EvaluationUnavailable('generator changed its frozen source during execution')
        result = {**report, 'task': name, 'seed': seed, 'attempt': attempt,
                  'source_sha256': case['source_sha256'], 'version': version.content_hash,
                  'wall_seconds': time.monotonic() - started,
                  'artifacts': base.relative_to(self.output).as_posix(), 'profile': profile or 'candidate', 'recipe': recipe}
        if not result['valid']:
            diagnostic = {}
            for name in ('generation.log', 'verification.log'):
                log = base / name
                if log.exists():
                    diagnostic[name] = log.read_text(encoding='utf-8', errors='replace')[-10000:]
            for path in generated.glob('*diagnostics*.json'):
                diagnostic[path.name] = path.read_text(encoding='utf-8', errors='replace')[:10000]
            result['diagnostic'] = diagnostic
        write_json(result_path, result)
        return result

    def evaluate(self, version, task_ids, k, *, purpose='screen'):
        gate = self.regression_check(version)
        if not gate['passed']:
            raise EvaluationUnavailable('source version does not pass the frozen compatibility gate')
        scores, traces, errors, attempts, measurements = {}, {}, {}, [{} for _ in range(k)], {}
        for task in task_ids:
            if task not in self.cases:
                raise EvaluationUnavailable('unknown task requested')
            if purpose != 'sealed' and self.cases[task]['split'] == 'test':
                raise EvaluationUnavailable('sealed tests cannot be used in the optimisation loop')
            rows = [self._attempt(version, task, i) for i in range(k)]
            measurements[task] = rows
            scores[task] = sum(row['score'] for row in rows) / k
            errors[task] = None if all(row['valid'] for row in rows) else 'invalid_schematic'
            traces[task] = []
            for i, row in enumerate(rows):
                attempts[i][task] = row['score']
                trace = {'valid': row['valid'], 'metrics': row.get('metrics'), 'quality': row.get('quality'),
                         'error': row.get('error'), 'diagnostic': row.get('diagnostic'),
                         'seed': row['seed'], 'artifacts': row['artifacts']}
                # Search geometry and actual SVG are evidence the analyst may inspect.
                if self.cases[task]['split'] == 'search' and row['valid']:
                    folder = self.output / row['artifacts'] / 'verified'
                    layout = json.loads((folder / 'verified-placement.json').read_text(encoding='utf-8'))
                    trace['layout'] = {key: layout.get(key) for key in ('placement', 'routes', 'layout_grid')}
                    svg = (folder / row.get('clean_svg', 'clean.svg')).read_text(encoding='utf-8')
                    trace['svg'] = svg if len(svg) < 50000 else svg[:50000] + '\n<!-- truncated -->'
                traces[task].append(trace)
        return EvalResult(scores, traces, {t: 0.0 for t in task_ids}, errors,
                          context={'measurements': measurements}, attempt_scores=attempts)

    def evaluate_subset(self, version, reference, subset, task_ids, k):
        if subset not in ('mb', 'rest') or any(self.cases[t]['split'] != 'validation' for t in task_ids):
            raise EvaluationUnavailable('invalid validation subset')
        subject = self.evaluate(version, task_ids, k)
        return subject, self.evaluate(reference, task_ids, k) if reference else None

    def observe_safeguards(self, candidate, reference):
        cs, ps = candidate.context.get('measurements', {}), reference.context.get('measurements', {})
        shared = sorted(set(cs) & set(ps))
        if not shared:
            if not cs and not ps:
                return {key: {'kind': 'ratio', 'direction': 'lower_is_better', 'subject': 1.0, 'reference': 1.0}
                        for key in self.observable_safeguards()}
            raise EvaluationUnavailable('safeguard comparison has no matched tasks')
        invalid = any(not row['valid'] for task in shared for row in cs[task])
        crossings, wirelength = [1.0], [1.0]
        for task in shared:
            if any(not r['valid'] for r in ps[task]) or any(not r['valid'] for r in cs[task]):
                continue
            for metric, values in (('crossings', crossings), ('wirelength', wirelength)):
                parent_rows = {r['seed']: r for r in ps[task]}
                matched = [r for r in cs[task] if r['seed'] in parent_rows]
                if not matched:
                    raise EvaluationUnavailable('safeguards have no matched seeds')
                for row in matched:
                    c = row['quality'][metric]
                    p = parent_rows[row['seed']]['quality'][metric]
                    values.append((1.0 + c) / (1.0 + p))
        values = {'hard_errors': 1.0 + float(invalid), 'crossings_ratio': max(crossings),
                  'wirelength_ratio': max(wirelength)}
        # Ratios with no tasks field are strict observed maxima. In particular a
        # single invalid schematic cannot hide in an average or a 2-task rule.
        return {key: {'kind': 'ratio', 'direction': 'lower_is_better', 'subject': value, 'reference': 1.0}
                for key, value in values.items()}

    def task_families(self, task_ids):
        return {name: self.cases[name]['family'] for name in task_ids}

    def reference(self, tasks, iterations=20000):
        self.reference_hash = tree_hash(self.root / 'reference' / 'engine')
        version = Version(self.reference_hash)
        original = self.iterations
        self.iterations = iterations
        try:
            return {task: self._attempt(version, task, 0, profile='reference', namespace='reference') for task in tasks}
        finally:
            self.iterations = original
