"""Bounded local jobs for the existing editor Worker protocol."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import queue
import sys
import threading
from time import monotonic, perf_counter
import uuid

from .cli import ADAPTER_ROOT, write_json
from .runtime import run_bounded

TERMINAL = frozenset(('succeeded', 'failed', 'cancelled'))


def refusal_message(output, error):
    """Prefer structured diagnostics over escaped graph/traceback fragments."""
    path=output/'native-validation.json'
    if path.is_file():
        try:
            report=json.loads(path.read_text(encoding='utf8'))
            if report.get('status')=='failed' and report.get('message'):
                return str(report['message'])[:1000]
        except (OSError,ValueError):
            pass
    for relative in ('adapter-report.json','engine-result/editable-engine-report.json'):
        path=output/relative
        if path.is_file():
            try:
                report=json.loads(path.read_text(encoding='utf8'))
                if report.get('status')=='failed' and report.get('message'):
                    return str(report['message'])[:1000]
            except (OSError,ValueError):
                pass
    lines=str(error).splitlines()
    for line in lines:
        if line.startswith('Adapter error: ') or line.startswith('Error: '):
            return line.split(': ',1)[1][:1000]
    return (lines[0] if lines else 'Layout verification failed; the drawing was preserved.')[:1000]


def validate_request(value):
    if not isinstance(value, dict) or value.get('type') != 'plan':
        raise ValueError('Expected an Auto Layout plan request')
    if type(value.get('runId')) is not int or value['runId'] < 0:
        raise ValueError('runId must be a nonnegative integer')
    project, ids = value.get('project'), value.get('documentIds')
    if not isinstance(project, dict) or not isinstance(project.get('documents'), list):
        raise ValueError('A native in-memory Project is required')
    if not isinstance(ids, list) or not 1 <= len(ids) <= 32 or any(
            not isinstance(i, str) or not i or len(i) > 200 for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('Choose 1 to 32 distinct Documents')
    known = {d.get('id') for d in project['documents'] if isinstance(d, dict) and isinstance(d.get('id'), str)}
    if not set(ids) <= known:
        raise ValueError('Requested Document is absent from the Project')
    # Paths, budgets and executable names are server configuration, never request fields.
    return {k: value[k] for k in ('type', 'runId', 'project', 'documentIds')}


@dataclass(frozen=True)
class WebJobConfig:
    project_root: Path
    engine_root: Path
    runtime: Path
    node: str
    jobs_root: Path
    engine_seconds: float = 300
    iterations: int = 20000
    seed: int = 20261029
    lease_seconds: float = 8
    queue_limit: int = 4
    layout_budget: str = 'adaptive'


@dataclass
class Job:
    id: str
    request: dict
    folder: Path
    state: str = 'queued'
    stage: str = 'queued'
    cell: int = 1
    last_seen: float = field(default_factory=monotonic)
    finished_at: float | None = None
    results: list = field(default_factory=list)
    message: str = ''
    execution_seconds: float = 0.
    cancel: threading.Event = field(default_factory=threading.Event)


class JobCancelled(Exception):
    pass


class JobManager:
    def __init__(self, config: WebJobConfig, *, start=True):
        self.config = config
        self.jobs = {}
        self.lock = threading.RLock()
        self.pending = queue.Queue()
        self.closed = threading.Event()
        self.threads = []
        config.jobs_root.mkdir(parents=True, exist_ok=True)
        if start:
            for target in (self._worker, self._reaper):
                thread = threading.Thread(target=target, daemon=True)
                thread.start()
                self.threads.append(thread)

    def submit(self, payload):
        request = validate_request(payload)
        with self.lock:
            if self.closed.is_set():
                raise RuntimeError('The layout service is stopping')
            if sum(j.state not in TERMINAL for j in self.jobs.values()) >= self.config.queue_limit:
                raise RuntimeError('The local layout queue is full; wait or cancel an existing run')
            # Bound memory only. Saved run directories are intentionally retained.
            completed = sorted((j for j in self.jobs.values() if j.state in TERMINAL),
                               key=lambda j: j.finished_at or 0)
            for old in completed[:-60]:
                del self.jobs[old.id]
            ident = uuid.uuid4().hex
            folder = self.config.jobs_root/ident
            folder.mkdir()
            write_json(folder/'worker-request.json', request)
            job = Job(ident, request, folder)
            self.jobs[ident] = job
            self.pending.put(job)
            return self._view(job)

    def _view(self, job):
        result = dict(jobId=job.id, status=job.state, stage=job.stage,
            runId=job.request['runId'], leaseSeconds=self.config.lease_seconds,
            totalSeconds=self.config.engine_seconds,executionSeconds=job.execution_seconds,
            progress=dict(cell=job.cell, cells=len(job.request['documentIds']), evaluations=0, budget=0))
        if job.state == 'succeeded':
            result['results'] = job.results
        if job.message:
            result['message'] = job.message
        return result

    def get(self, ident, *, touch=True):
        with self.lock:
            job = self.jobs[ident]
            if touch and job.state not in TERMINAL:
                job.last_seen = monotonic()
            return self._view(job)

    def cancel_job(self, ident, reason='Cancelled by the editor'):
        with self.lock:
            job = self.jobs[ident]
            if job.state not in TERMINAL:
                job.cancel.set()
                job.state = 'cancelled'
                job.message = reason
                job.finished_at = monotonic()
            return self._view(job)

    def expire(self, now=None):
        now = monotonic() if now is None else now
        with self.lock:
            for job in list(self.jobs.values()):
                if job.state not in TERMINAL and now-job.last_seen > self.config.lease_seconds:
                    self.cancel_job(job.id, 'Editor disconnected or cancelled; lease expired')

    def _reaper(self):
        while not self.closed.wait(0.25):
            self.expire()

    def _worker(self):
        while not self.closed.is_set():
            try:
                job = self.pending.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                with self.lock:
                    if job.cancel.is_set():
                        continue
                    job.state = 'running'
                self._run(job)
                with self.lock:
                    if not job.cancel.is_set():
                        job.state='succeeded'
                        job.stage=('draft_ready' if any(r.get('resultKind')=='editable_draft' for r in job.results)
                                   else 'verified')
            except JobCancelled:
                self.cancel_job(job.id)
            except Exception as exc:
                with self.lock:
                    if not job.cancel.is_set():
                        job.state, job.message = 'failed', str(exc)[-2000:]
            finally:
                with self.lock:
                    job.finished_at = monotonic()
                    write_json(job.folder/'web-job.json', self._view(job))
                self.pending.task_done()

    def _command(self, job, label, command, seconds):
        if job.cancel.is_set():
            raise JobCancelled()
        if seconds<=0:raise ValueError('The web layout total budget is exhausted before '+label)
        with self.lock:
            job.stage = label
        log = job.folder/(label+'.log')
        with log.open('w', encoding='utf8') as stream:
            code = run_bounded(command, seconds=seconds, cancel_event=job.cancel,
                cwd=ADAPTER_ROOT.parents[1], stdout=stream, stderr=stream)
        if job.cancel.is_set():
            raise JobCancelled()
        if code:
            tail = log.read_text(encoding='utf8', errors='replace')[-2000:]
            raise ValueError(f'{label} failed (exit {code}): {tail}')

    def _native(self, operation, input_path, output_path, *extra):
        c = self.config
        return [c.node, '--experimental-transform-types', str(ADAPTER_ROOT/'web-native.mjs'),
            operation, '--project-root', str(c.project_root), '--runtime', str(c.runtime),
            '--input', str(input_path), '--output', str(output_path), *map(str, extra)]

    def _run(self, job):
        c = self.config
        execution_started=perf_counter();deadline=execution_started+c.engine_seconds
        source = job.folder/'source.icproj.json'
        self._command(job, 'native-input', self._native('input',
            job.folder/'worker-request.json', source), min(30.,c.engine_seconds*.1))
        results = []
        for index, ident in enumerate(job.request['documentIds']):
            started = perf_counter()
            with self.lock:
                job.cell = index+1
            output = job.folder/f'cell-{index+1}'
            try:
                available=max(0.,deadline-perf_counter())/(len(job.request['documentIds'])-index)
                atomic_reserve=min(20.,available*.1)
                adapter_seconds=max(0.,available-atomic_reserve)
                self._command(job, f'cell-{index+1}-python', [sys.executable,'-X','utf8','-B',
                    '-m','integrations.auto_canvas_huyangc','--project-root',str(c.project_root),
                    '--engine-root',str(c.engine_root),'--runtime',str(c.runtime),'--node',c.node,
                    '--input',str(source),'--document-id',ident,'--out-dir',str(output),
                    '--engine-seconds',str(c.engine_seconds),'--iterations',str(c.iterations),
                    '--seed',str(c.seed),'--result-policy','editable',
                    '--total-seconds',str(adapter_seconds),
                    '--layout-budget',c.layout_budget], adapter_seconds)
                report = json.loads((output/'adapter-report.json').read_text(encoding='utf8'))
                if report['status'] not in ('valid','editable_draft'):
                    raise ValueError('The adapter did not produce a verified result')
                browser_result = output/'browser-result.json'
                extra=['--allow-draft','true'] if report['status']=='editable_draft' else []
                self._command(job, f'cell-{index+1}-atomic-verify', self._native('result', source,
                    browser_result, '--plan',output/'native-plan.json',
                    '--validation',output/'native-validation.json','--expected',report['native_project'],*extra),
                    min(60.,max(0.,deadline-perf_counter())))
                item = json.loads(browser_result.read_text(encoding='utf8'))
                if perf_counter()>deadline:
                    raise ValueError('Web layout total budget exhausted before the result could be returned')
                results.append(dict(documentId=ident, result=item['result'],
                    resultKind=item.get('resultKind','complete'),
                    milliseconds=round((perf_counter()-started)*1000, 3)))
            except ValueError as exc:
                # The editor's existing import session can apply other valid Cells
                # as one undo step, while keeping this Cell and its refusal visible.
                results.append(dict(documentId=ident, milliseconds=round((perf_counter()-started)*1000,3),
                    result=dict(edits=[],groups=[],diagnostics=[dict(severity='error',
                        code='NETLIST2SCHEMATIC_REFUSED',rule='python-adapter',message=refusal_message(output,exc))])))
        with self.lock:
            if job.cancel.is_set():
                raise JobCancelled()
            job.execution_seconds=round(perf_counter()-execution_started,6)
            job.results = results

    def close(self):
        self.closed.set()
        with self.lock:
            for job in self.jobs.values():
                if job.state not in TERMINAL:
                    self.cancel_job(job.id, 'Layout service stopped')
        for thread in self.threads:
            thread.join(timeout=5)
