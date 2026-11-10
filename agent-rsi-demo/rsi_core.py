"""Use the supplied method-v4.1 coordinator for the portable circuit task.

The vendored loop, calibration, stages and statistical decisions are copied
without modification.  This module only supplies the import path, circuit-run
configuration, portable state I/O and a recorded demonstration round limit.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / 'vendor' / 'rsi' / 'src'
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

from rag_eval.optimizer.adapter import (  # noqa: E402
    AgentRequestFailed, BudgetExhausted, Card, Diff, EvalResult,
    EvaluationCancelled, EvaluationUnavailable, ImplementationDeclined,
    Predicate, SmokeResult, SystemAdapter, Version,
)
from rag_eval.optimizer.v4.config import V4Config, validate  # noqa: E402
from rag_eval.optimizer.v4.loop import V4Run  # noqa: E402
from rag_eval.optimizer import ledger_io
from atomic_io import write_text

SOURCE = {
    'repository': 'https://github.com/Xi0ng-Jun/SN_benchmark.git',
    'branch': 'feat/method-v4.1',
    'commit': 'c01964cccb89e0cbf91717bd46994ff93c189f9b',
    'entrypoint': 'rag_eval.optimizer.v4.loop.V4Run',
}


class CircuitV4Run(V4Run):
    """Original coordinator plus a declared, optional operator round limit."""

    def __init__(self, *args, max_rounds=None, **kwargs):
        self.demo_max_rounds = max_rounds
        super().__init__(*args, **kwargs)
        if self.stop_reason == 'demo_round_limit' and (max_rounds is None or len(self.rounds) < max_rounds):
            self.stop_reason = None

    def _stop_reason(self):
        reason = super()._stop_reason()
        if reason:
            return reason
        if self.demo_max_rounds is not None and len(self.rounds) >= self.demo_max_rounds:
            return 'demo_round_limit'
        return None

    def save(self):
        if self.state_dir is not None:
            write_text(self.state_dir / 'state.json', ledger_io.dumps(self._state(), indent=1) + '\n')

    def write_ledger(self):
        if self.state_dir is not None:
            write_text(self.state_dir / 'ledger.json', ledger_io.dumps(self.ledger(), indent=1) + '\n')

    def ledger(self):
        result = super().ledger()
        result['circuit_demo'] = {
            'method_core': SOURCE,
            'max_rounds': self.demo_max_rounds,
            'declared_variant': 'operator round limit added outside the unmodified core',
            'state_io': 'atomic fsync and bounded Windows sharing retries; state contents unchanged',
            'domain_policy': {'regression': getattr(self.adapter, 'regression', 'adapter-defined'),
                              'score': 'geometry-plus-native-svg-readability-v2',
                              'candidate_preflight_and_bounded_repair': hasattr(self.adapter, 'regression_check')},
            'note': 'Original statistical core, circuit adapter gates and local paired seed/final endpoint; SN workbench is not used.',
        }
        return result


def make_config(*, time_budget_hours=1.0, overrides=None):
    """Freeze method settings; safeguards are observed by the circuit adapter."""
    values = dict(
        time_budget_hours=time_budget_hours,
        calibration_k=2,
        b=1,
        cards_per_analysis=1,
        max_concurrent_executions=1,
        diagnose_size=0,
        smoke=True,
        smoke_size=1,
        minibatch=False,
        safeguards={'hard_errors': 0.0, 'crossings_ratio': 0.0, 'wirelength_ratio': 0.15},
        review=False,
        remeasure=True,
        merge=False,
        ablation=False,
        speculative_analysis=False,
        speculative_prefetches=0,
        agent_estimate_minutes=0.01,
    )
    overrides = dict(overrides or {})
    allowed = {field.name for field in dataclasses.fields(V4Config)}
    unknown = sorted(set(overrides) - allowed)
    if unknown:
        raise ValueError('Unknown method-v4.1 settings: ' + ', '.join(unknown))
    values.update(overrides)
    return validate(V4Config(**values))


def run_core(adapter, seed, *, search, validation, state_dir, max_rounds=3,
             time_budget_hours=1.0, config=None, test_size=None, resume=False):
    """Run actual v4.1 calibration, code candidates, paired validation and adoption.

    ``adapter`` must implement SystemAdapter plus evaluate_subset. ``seed`` is a
    Version whose content hash identifies the baseline source snapshot.
    ``config`` holds optional V4Config field overrides, never model credentials.
    Returns a serialisable summary; full state and ledger are persisted by core.
    """
    search, validation = list(search), list(validation)
    if not search or len(set(validation)) < 2:
        raise ValueError('The circuit demo requires a search set and at least two validation tasks')
    if len(set(search)) != len(search) or len(set(validation)) != len(validation):
        raise ValueError('Task identifiers must be unique within each partition')
    if set(search) & set(validation):
        raise ValueError('Search and validation partitions must be disjoint')
    if max_rounds is not None and (type(max_rounds) is not int or max_rounds < 0):
        raise ValueError('max_rounds must be a non-negative integer or None')
    cfg = make_config(time_budget_hours=time_budget_hours, overrides=config)
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    identity = {
        'method_core': SOURCE,
        'max_rounds': max_rounds,
        'variant': 'circuit task adapter and explicit demonstration round limit',
    }
    identity_path = state_dir / 'demo-method.json'
    if identity_path.exists():
        previous = json.loads(identity_path.read_text(encoding='utf-8'))
        if previous != identity and not (resume and previous.get('method_core') == SOURCE):
            raise ValueError('The circuit demo round limit or vendored method differs from this run')
        if previous != identity:
            history_path = state_dir / 'round-limit-history.json'
            history = json.loads(history_path.read_text(encoding='utf-8')) if history_path.exists() else []
            history.append({'from': previous['max_rounds'], 'to': max_rounds})
            history_path.write_text(json.dumps(history, indent=2), encoding='utf-8')
            identity_path.write_text(json.dumps(identity, indent=2), encoding='utf-8')
    else:
        identity_path.write_text(json.dumps(identity, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    runner = CircuitV4Run(adapter, seed, cfg, search=search, validation=validation,
                         state_dir=state_dir, test_size=test_size, max_rounds=max_rounds)
    status = runner.run()
    ledger = runner.ledger()
    return {'status': status, 'final_version': runner.final_version(),
            'lineage': runner.lineage, 'ledger': ledger}
