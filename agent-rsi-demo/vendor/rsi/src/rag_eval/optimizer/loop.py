"""Main v3 optimization loop orchestration.

Implements the §2.2 A→B state machine, §1 Ladder-gated screening,
milestone confirmation, rollback, validation-pool growth, and §2.5 halt
conditions from method-v3.md.  All SN-specific logic is behind SystemAdapter.

Concurrency (binding plan §2.6): per-card work — workspace, implement,
materialize, smoke, any smoke repair (§2.1 失败后的处理), screening measurement, and
any rebase + re-measurement — runs in a thread pool of ``b_cards`` workers.
Every decision (Ladder gate, provisional adoption, milestone confirmation,
state transitions) is taken on the main thread, one finished candidate at a
time, in completion order. Workers never touch loop state; they get immutable
snapshots (parent version, validation-pool ids) and return a CandidateRecord.

Durability (binding plan §2.7): with ``state_dir`` the main thread rewrites
``state.json`` atomically after every transition (round start, the round's
cards, each decision, each confirmation, round end) and writes
``rounds.jsonl`` / ``ledger.json`` (§2.4) via ``ledger_io``. A rerun resumes
from it; adapter calls of interrupted work replay under deterministic keys.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import ledger_io

from .adapter import (
    BudgetExhausted,
    Card,
    Diff,
    EvalResult,
    EvaluationUnavailable,
    SmokeResult,
    SystemAdapter,
    Version,
)
from .ledger import Lineage, MilestoneState, NodeState
from .stats import (
    alpha_warning,
    attribution_result,
    confirm_decision,
    ladder_m,
    mean_delta,
    paired_differences,
    paired_se,
    screen_decision,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol configuration (preregistered before any data is seen)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProtocolConfig:
    """All parameters preregistered before the experiment starts.

    Notation matches method-v3.md §1 and §2.2.
    """
    mde: float                     # minimum detectable effect δ
    sigma_d: float                 # planned per-task paired-diff SD
    ladder_c: float                # Ladder scale constant (default 1.0)
    K_max: int                     # pre-registered max screening submissions
    T: float                       # milestone trigger threshold (2–4×MDE recommended)
    n_fresh: int                   # tasks per fresh batch
    z_screen: float = 0.84         # one-sided z for screening
    z_confirm: float = 1.65        # one-sided z for confirmation
    b_cards: int = 4               # parallel implementers (candidates in flight) per round
    k_screen: int = 3              # eval repetitions per task at screening
    k_smoke: int = 1               # eval repetitions for smoke sample
    n_smoke_sample: int = 5        # search-set tasks sampled for smoke check
    # metric → max_delta for the default SystemAdapter.safeguards. Systems with their
    # own pre-registered safeguards (SN: the iteration protocol) require it empty.
    safeguard_bounds: dict = field(default_factory=dict)
    max_confirmations: int = 10    # §7 α accumulation upper bound
    predicate_coverage_floor: float = 0.60  # §6 min fraction of cards with predicate
    # §2.3 analyst case selection (accountant-owned, fixed rule): tasks scoring
    # below the threshold on the parent's search runs, worst first, plus a few
    # deterministically chosen passing tasks for contrast.
    analyst_failure_threshold: float = 0.5
    analyst_max_failures: int = 12
    analyst_n_controls: int = 3
    # K_c: cards requested from the analyst per round (§2.1 step 1). Cards
    # beyond b_cards are the refill queue (§2.1 补位). None → b_cards.
    analyst_cards: Optional[int] = None
    # §2.5 流程故障: consecutive candidates whose smoke run itself failed
    # (crash / not admitted) before the loop halts as a process failure.
    max_consecutive_run_failures: int = 3
    # §2.5 停机: consecutive rounds with no screen pass ('no_candidates') and
    # consecutive rejected confirmations ('no_improvement') before the loop halts.
    max_rounds_without_screen_pass: int = 2
    max_consecutive_rejections: int = 2
    # §2.2 归因: minimum slice size |S| on the fresh batch; below it the
    # attribution label is 'insufficient'.
    attribution_n_min: int = 5
    # §2.1 失败后的处理: repairs granted to a card whose behavioural smoke
    # failed (predicate did not move as predicted); 0 disables. A generic-smoke
    # failure or a smoke run failure is never repaired (process failure).
    smoke_repair_attempts: int = 1
    # §2.1 逐级加码: per consecutive no-adoption round under the same parent the
    # analyst's failure-case cap grows by this step (capped by the search set).
    analyst_max_failures_step: int = 0


# ---------------------------------------------------------------------------
# Per-round book-keeping
# ---------------------------------------------------------------------------

# Candidate verdicts recorded in RoundResult.candidates (stable vocabulary).
CANDIDATE_VERDICTS = ("smoke_fail", "screen_fail", "screen_pass", "unmeasurable",
                      "stale_parent", "error")


@dataclass
class CandidateRecord:
    """One card tracked through implement → smoke → screen (→ rebase → ...).

    Filled by a worker thread; read by the main thread only after the
    worker's future has completed.
    """
    card: Card
    card_index: int
    parent: Version                     # version this candidate is built against
    candidate: Optional[Version] = None
    diff: Optional[Diff] = None
    smoke: Optional[SmokeResult] = None
    eval_result: Optional[EvalResult] = None   # candidate on the validation pool
    ref_result: Optional[EvalResult] = None    # parent on the same pool (paired)
    safeguards: Optional[dict] = None          # SystemAdapter.safeguards of that pair
    pool_size: int = 0                  # validation-pool size the measurement used
    # 'measured' | 'error' | 'smoke_fail' | 'stale_parent' | 'built' (resume:
    # rebuilt by replay without measurement because its parent is stale)
    stage: str = "measured"
    reason: Optional[str] = None
    rebased_from: Optional[str] = None
    screen_evaluations: int = 0
    n_unpaired: int = 0                 # pool tasks dropped: unscored on either side
    delta_val: Optional[float] = None
    se_val: Optional[float] = None
    screen_verdict: Optional[str] = None
    timings: dict = field(default_factory=dict)
    resumed: bool = False               # re-submitted after a restart (adapter calls replay)
    started_at: float = 0.0
    finished_at: float = 0.0
    # §2.1 smoke repair: 1 for the original attempt, n+1 after the n-th repair.
    attempt: int = 1
    repair_of: Optional[str] = None     # content hash of the failed attempt this one repairs
    prior_attempts: list = field(default_factory=list)   # superseded attempts (recorded first)
    smoke_evals: Optional[tuple] = None  # (candidate, parent) smoke results, while a repair may follow
    repair_failed: bool = False          # the repair step itself failed (no repaired candidate)


@dataclass
class RoundResult:
    """Summary of one optimization round.

    Field names are stable (§2.4 integrity rule): W5 persists them.
    """
    round_id: int
    cards_generated: int
    smoke_passed: int
    screen_passed: int
    milestone_triggered: bool
    confirmation_verdict: Optional[str]   # last of `confirmations`, or None
    rolled_back: bool
    halt_reason: Optional[str]
    # One dict per card that entered the pipeline, in decision order. Keys:
    # card_ref, title, card_index, verdict (CANDIDATE_VERDICTS), reason, parent,
    # candidate, candidate_id, rebased_from, smoke_verdict, delta_val, se_val, score_val,
    # best_before, ladder_m, val_pool_size, screen_evaluations, n_unpaired, safeguards
    # (SystemAdapter.safeguards of the screen pair, or None when not screened),
    # timings, has_predicate, resumed, halted (True: recorded because the
    # loop halted on a process failure, not a decision on the card), attempt
    # (1, or n+1 after n smoke repairs), repair_of (candidate hash the attempt
    # repairs), repaired (attempt > 1), superseded (True: a smoke-failed attempt
    # a repair of the same card follows; §2.4 every attempt is recorded).
    candidates: list = field(default_factory=list)
    # One dict per milestone confirmation this round. Keys: verdict,
    # delta_fresh, se_fresh, n_fresh, n_unpaired, k_fresh, fresh_batch_index,
    # segment_start, segment_end, segment_steps, cumulative_delta_val,
    # safeguards_end (SystemAdapter.safeguards of end vs start on the batch),
    # cost_start, cost_end, attribution, val_pool_size,
    # ladder_m, best_score.
    confirmations: list = field(default_factory=list)
    val_pool_size: int = 0                # at end of round
    ladder_m: float = 0.0                 # m in force at end of round
    best_score: float = 0.0               # best at end of round
    cards_with_predicate: int = 0         # §6 predicate attachment
    started_at: Optional[str] = None      # ISO-8601 UTC
    finished_at: Optional[str] = None
    # §2.3 diagnosis record (诊断预算和实际消耗记入运行记录): parent,
    # no_adoption_rounds, max_failures, failures_shown, failures_new,
    # rotation_cycle_restarted, controls_shown, adapter (the adapter's own
    # analysis stats, e.g. SN evidence budget and file), and at round end
    # pointer_moved / no_adoption_rounds_after.
    analysis: dict = field(default_factory=dict)


@dataclass
class LoopResult:
    """Full result returned after the loop terminates."""
    # 'fresh_exhausted' | 'no_improvement' | 'no_candidates' | 'max_confirmations'
    # | 'max_rounds' | 'budget_exhausted' | 'process_failure'
    halt_reason: str
    lineage: Lineage
    rounds: list[RoundResult]
    alpha_note: dict      # from alpha_warning()


# ---------------------------------------------------------------------------
# Halt-condition tracker
# ---------------------------------------------------------------------------

class HaltTracker:
    def __init__(self) -> None:
        self.consecutive_no_screen_pass: int = 0
        self.consecutive_rejected_confirmations: int = 0
        self.fresh_batches_remaining: int = 0
        self.confirmations_used: int = 0

    def record_round(self, screen_passed: int, confirmation_verdict: Optional[str] = None) -> None:
        if screen_passed == 0:
            self.consecutive_no_screen_pass += 1
        else:
            self.consecutive_no_screen_pass = 0
        if confirmation_verdict is not None:
            self.record_confirmation(confirmation_verdict)

    def record_confirmation(self, verdict: str) -> None:
        if verdict == 'rejected':
            self.consecutive_rejected_confirmations += 1
        elif verdict in ('confirmed', 'insufficient'):
            self.consecutive_rejected_confirmations = 0
        if verdict in ('confirmed', 'rejected', 'insufficient'):
            self.confirmations_used += 1

    def can_confirm(self, max_confirmations: int) -> bool:
        return self.fresh_batches_remaining > 0 and self.confirmations_used < max_confirmations

    def halt_reason(self, config: 'ProtocolConfig') -> Optional[str]:
        # R3: only stop when fresh batches exhausted, not on progress metrics
        if self.fresh_batches_remaining <= 0:
            return 'fresh_exhausted'
        return None


RUN_FAILURE = 'smoke_run_failed'


def _paired(a: EvalResult, b: EvalResult, task_ids: list[str]) -> tuple[list[float], list[float], int]:
    """Scores of tasks scored on both sides, plus how many were dropped.

    A missing score is not computable, never 0 (§2.3): unscored tasks leave
    the paired difference instead of entering it as zeros.
    """
    xs, ys = [], []
    for tid in task_ids:
        x, y = a.per_task_scores.get(tid), b.per_task_scores.get(tid)
        if x is not None and y is not None:
            xs.append(x)
            ys.append(y)
    return xs, ys, len(task_ids) - len(xs)


def _failure_halt_reason(exc: EvaluationUnavailable) -> str:
    return 'budget_exhausted' if isinstance(exc, BudgetExhausted) else 'process_failure'


# ---------------------------------------------------------------------------
# Smoke check helper
# ---------------------------------------------------------------------------

def _run_smoke(
    adapter: SystemAdapter,
    candidate: Version,
    parent: Version,
    card: Card,
    smoke_task_ids: list[str],
) -> SmokeResult:
    """The §2.1 step 3 smoke verdict (see ``_smoke``)."""
    return _smoke(adapter, candidate, parent, card, smoke_task_ids)[0]


def _smoke(
    adapter: SystemAdapter,
    candidate: Version,
    parent: Version,
    card: Card,
    smoke_task_ids: list[str],
) -> tuple[SmokeResult, Optional[EvalResult], Optional[EvalResult]]:
    """Run the cheap pre-screen behavioral check on n_smoke_sample tasks.

    Returns (verdict, candidate smoke result, parent smoke result); the
    results are None for a generic smoke (only a behavioural failure is
    repaired, and the repair reads them).

    With a predicate (§2.1 step 3a): check the feature rate moved in the
    predicted direction; fail the candidate if it did not.

    Without a predicate (§2.1 step 3b): run a generic smoke — the candidate
    must produce non-empty trajectories on every smoke task (i.e. it runs
    without infrastructure failure). Verdict is 'unchecked' for the
    behavioral dimension but the candidate is still discarded on infra fail.

    EvaluationUnavailable (incl. BudgetExhausted) propagates: a missing
    measurement is a process failure, not evidence against the candidate.
    """
    # R3: Generic smoke only, behavioral checks disabled.
    try:
        cand_eval, _ = adapter.evaluate_pair(candidate, parent, smoke_task_ids, 1, purpose='smoke')
    except EvaluationUnavailable:
        raise
    except Exception as exc:  # candidate-induced failure during smoke
        log.warning("generic smoke infra failure: %s", exc)
        return SmokeResult(
            candidate_version=candidate.content_hash,
            card_ref=card.failure_mode_desc[:40],
            behavioral_check_ran=False,
            feature_present_before=None,
            feature_present_after=None,
            verdict='fail',
        ), None, None
    ran = all(bool(cand_eval.trajectories.get(tid)) for tid in smoke_task_ids)
    return SmokeResult(
        candidate_version=candidate.content_hash,
        card_ref=card.failure_mode_desc[:40],
        behavioral_check_ran=False,
        feature_present_before=None,
        feature_present_after=None,
        verdict='pass' if ran else 'fail',
    ), None, None


def _check_safeguards(adapter: SystemAdapter, candidate: EvalResult, reference: EvalResult,
                      config: ProtocolConfig) -> dict:
    check = adapter.safeguards(candidate, reference, config.safeguard_bounds)
    if not check["ok"]:
        log.warning("safeguards block the decision: %s", ", ".join(check["reasons"]))
    return check


# ---------------------------------------------------------------------------
# Worker-side pipeline (runs in the thread pool; touches no loop state)
# ---------------------------------------------------------------------------

def _stop(rec: CandidateRecord, stage: str, reason: Optional[str] = None) -> CandidateRecord:
    rec.stage = stage
    rec.reason = reason
    rec.finished_at = time.monotonic()
    rec.timings['total_s'] = rec.finished_at - rec.started_at
    return rec


def _build_and_measure(adapter: SystemAdapter, config: ProtocolConfig, rec: CandidateRecord,
                       smoke_ids: list[str], pool: list[str], measure: bool = True) -> CandidateRecord:
    """workspace → implement → materialize → smoke → screening measurement.

    measure=False (resume of a candidate whose parent is already stale): stop
    after materialize with stage 'built'; the decision rebases it, so no
    measurement is spent on the stale version.
    """
    rec.started_at = time.monotonic()
    t0 = rec.started_at
    for step in ('workspace', 'implement', 'materialize'):
        try:
            if step == 'workspace':
                ws = adapter.workspace(rec.parent, rec.card)
            elif step == 'implement':
                rec.diff = adapter.implement(rec.card, ws)
            else:
                rec.candidate = adapter.materialize(rec.diff, rec.parent)
        except EvaluationUnavailable:
            raise
        except Exception as exc:  # per-card failure: skip card, refill
            log.warning("%s failed on card %r: %s", step, rec.card.failure_mode_desc[:40], exc)
            rec.timings['implement_s'] = time.monotonic() - t0
            return _stop(rec, 'error', f"{step}: {type(exc).__name__}: {exc}")
    rec.timings['implement_s'] = time.monotonic() - t0
    if not measure:
        return _stop(rec, 'built')
    return _smoke_and_screen(adapter, config, rec, smoke_ids, pool)


def _smoke_and_screen(adapter: SystemAdapter, config: ProtocolConfig, rec: CandidateRecord,
                      smoke_ids: list[str], pool: list[str]) -> CandidateRecord:
    t0 = time.monotonic()
    try:
        rec.smoke, *evals = _smoke(adapter, rec.candidate, rec.parent, rec.card, smoke_ids)
        rec.smoke_evals = tuple(evals)
    except BudgetExhausted:
        raise
    except EvaluationUnavailable as exc:
        # The smoke run itself failed (candidate crash or run not admitted):
        # generic smoke exists to catch exactly this, so it is this card's
        # smoke failure, not a loop halt. Repeats halt via the run-failure count.
        rec.timings['smoke_s'] = rec.timings.get('smoke_s', 0.0) + time.monotonic() - t0
        return _stop(rec, 'smoke_fail', f"{RUN_FAILURE}: {exc}")
    except Exception as exc:
        rec.timings['smoke_s'] = rec.timings.get('smoke_s', 0.0) + time.monotonic() - t0
        return _stop(rec, 'error', f"smoke: {type(exc).__name__}: {exc}")
    rec.timings['smoke_s'] = rec.timings.get('smoke_s', 0.0) + time.monotonic() - t0
    if rec.smoke.verdict == 'fail':
        # §2.1 失败后的处理: a behavioural failure (the predicate did not move as
        # predicted) most likely means the change did not take effect: repair.
        if rec.smoke.behavioral_check_ran and rec.attempt - 1 < config.smoke_repair_attempts:
            return _repair(adapter, config, rec, smoke_ids, pool)
        rec.smoke_evals = None
        return _stop(rec, 'smoke_fail')
    rec.smoke_evals = None
    return _screen(adapter, config, rec, pool)


def _repair(adapter: SystemAdapter, config: ProtocolConfig, rec: CandidateRecord,
            smoke_ids: list[str], pool: list[str]) -> CandidateRecord:
    """§2.1 smoke repair: one more attempt of the same card on the same parent.

    The smoke-failed attempt is kept as a superseded attempt (the main thread
    records it first, §2.4 integrity); the record then continues as attempt
    n+1: ``adapter.repair`` (smoke feedback from the search-set smoke sample)
    → ``materialize`` → smoke → screening, exactly like the original attempt.
    ``repair`` returning None (unsupported) leaves the smoke failure standing.
    A per-card exception of the repair step ends the card as 'error' with
    ``repair_failed`` (its history label stays smoke_fail).
    """
    t0 = time.monotonic()
    cand_eval, parent_eval = rec.smoke_evals or (None, None)
    rec.smoke_evals = None
    attempt = rec.attempt + 1
    error = None
    try:
        diff = adapter.repair(rec.card, rec.parent, rec.candidate, smoke=rec.smoke,
                              candidate_smoke=cand_eval, parent_smoke=parent_eval, attempt=attempt)
    except EvaluationUnavailable:
        raise
    except Exception as exc:  # per-card failure of the repair
        log.warning("repair failed on card %r: %s", rec.card.failure_mode_desc[:40], exc)
        diff, error = None, f"repair: {type(exc).__name__}: {exc}"
    if diff is None and error is None:
        return _stop(rec, 'smoke_fail')
    failed = copy.copy(rec)
    failed.prior_attempts, failed.timings = [], dict(rec.timings)
    _stop(failed, 'smoke_fail')
    rec.prior_attempts.append(failed)
    rec.attempt, rec.repair_of = attempt, rec.candidate.content_hash
    rec.diff, rec.candidate, rec.smoke, rec.rebased_from = diff, None, None, None
    rec.timings, rec.started_at = {}, t0
    if error is None:
        try:
            rec.candidate = adapter.materialize(diff, rec.parent)
        except EvaluationUnavailable:
            raise
        except Exception as exc:
            error = f"repair materialize: {type(exc).__name__}: {exc}"
    rec.timings['repair_s'] = time.monotonic() - t0
    if error is not None:
        rec.repair_failed = True
        return _stop(rec, 'error', error)
    return _smoke_and_screen(adapter, config, rec, smoke_ids, pool)


def _screen(adapter: SystemAdapter, config: ProtocolConfig, rec: CandidateRecord,
            pool: list[str]) -> CandidateRecord:
    """Paired candidate-vs-parent measurement on the validation pool (§2.2 A)."""
    if not rec.started_at:
        rec.started_at = time.monotonic()
    t0 = time.monotonic()
    try:
        rec.eval_result, rec.ref_result = adapter.evaluate_pair(
            rec.candidate, rec.parent, pool, config.k_screen, purpose='screen')
        rec.safeguards = _check_safeguards(adapter, rec.eval_result, rec.ref_result, config)
    except EvaluationUnavailable:
        raise
    except Exception as exc:
        rec.timings['screen_s'] = rec.timings.get('screen_s', 0.0) + time.monotonic() - t0
        return _stop(rec, 'error', f"screen: {type(exc).__name__}: {exc}")
    rec.timings['screen_s'] = rec.timings.get('screen_s', 0.0) + time.monotonic() - t0
    rec.pool_size = len(pool)
    rec.screen_evaluations += 1
    return _stop(rec, 'measured')


def _rebase_and_measure(adapter: SystemAdapter, config: ProtocolConfig, rec: CandidateRecord,
                        new_parent: Version, smoke_ids: list[str],
                        pool: list[str]) -> CandidateRecord:
    """§2.2 A precondition: re-apply onto the current parent, re-smoke, re-screen."""
    t0 = time.monotonic()
    reason = None
    try:
        rebased = adapter.rebase(rec.candidate, new_parent)
    except EvaluationUnavailable:
        raise
    except Exception as exc:
        rebased, reason = None, f"rebase: {type(exc).__name__}: {exc}"
    rec.timings['rebase_s'] = rec.timings.get('rebase_s', 0.0) + time.monotonic() - t0
    if rebased is None:
        return _stop(rec, 'stale_parent', reason or f"parent moved to {new_parent.content_hash}")
    rec.rebased_from = rec.candidate.content_hash
    rec.candidate = rebased
    rec.parent = new_parent
    rec.eval_result = rec.ref_result = rec.safeguards = None
    return _smoke_and_screen(adapter, config, rec, smoke_ids, pool)


# ---------------------------------------------------------------------------
# Main optimization loop
# ---------------------------------------------------------------------------

def run_optimization(
    adapter: SystemAdapter,
    seed: Version,
    config: ProtocolConfig,
    search_set_ids: list[str],
    val_pool_ids: list[str],
    fresh_batch_ids: list[list[str]],
    smoke_sample_ids: list[str],
    max_rounds: int = 50,
    state_dir: Optional[str | Path] = None,
    key_generation: Optional[int] = None,
) -> LoopResult:
    """Run the full v3 optimization loop.

    Parameters
    ----------
    adapter : SystemAdapter
        SN-specific four-capability adapter. Must be thread-safe: per-card
        work for up to ``config.b_cards`` candidates runs concurrently.
    seed : Version
        Starting parent version (the harness baseline).
    config : ProtocolConfig
        All pre-registered parameters.
    search_set_ids : list[str]
        Search-set task ids (analyst reads failures here).
    val_pool_ids : list[str]
        Initial validation pool task ids (used for screening). The loop works
        on a copy that grows as fresh batches are folded in; the caller's
        list is not mutated.
    fresh_batch_ids : list[list[str]]
        Ordered list of fresh batches; each consumed once on confirmation.
    smoke_sample_ids : list[str]
        Search-set tasks used for the cheap smoke check (n_smoke_sample of them).
    max_rounds : int
        Hard outer bound on rounds (counted over the whole run, across resumes).
    state_dir : str | Path, optional
        Durable run directory (binding plan §2.7). ``state.json`` is rewritten
        atomically after every state transition; ``rounds.jsonl`` and the
        §2.4 ``ledger.json`` are written there too. If ``state.json`` already
        exists the run resumes from it: the config hash, seed and the
        arguments above (partitions, as originally passed) must match, else
        ValueError (pre-registered parameters are read-only, §3 step 0).
        A resumed round does not call analyse again; cards already decided
        are skipped; in-flight cards are re-submitted on their original
        parent so adapter calls replay under deterministic keys.
    key_generation : int, optional
        Evaluation request-key generation (see SystemAdapter.key_generation).
        None keeps the persisted value (new run: the adapter's own value).
        A larger value is an operator-requested retry after a process-failure
        halt: it is recorded, the consecutive run-failure counter is reset,
        and the adapter issues new measurement keys. Lowering it is refused.

    Returns
    -------
    LoopResult
    """
    return _Run(adapter, seed, config, search_set_ids, val_pool_ids, fresh_batch_ids,
                smoke_sample_ids, state_dir=state_dir, key_generation=key_generation).run(max_rounds)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class _RoundProgress:
    """The open round: its RoundResult plus what resume needs (binding plan §2.7)."""
    round: RoundResult
    cards: Optional[list[Card]] = None        # None until analyse returned (then persisted)
    submitted: list[int] = field(default_factory=list)       # card indices, submission order
    inflight: dict[int, str] = field(default_factory=dict)   # card index -> parent hash of its attempt
    stop_new: bool = False                    # a milestone ran this round: no new cards
    owed_refills: int = 0                     # refills decided but not submitted (halted first)


class _Run:
    """Loop state; every method runs on the main thread."""

    def __init__(self, adapter: SystemAdapter, seed: Version, config: ProtocolConfig,
                 search_set_ids: list[str], val_pool_ids: list[str],
                 fresh_batch_ids: list[list[str]], smoke_sample_ids: list[str],
                 *, state_dir: Optional[str | Path] = None,
                 key_generation: Optional[int] = None) -> None:
        self.adapter = adapter
        self.seed = seed
        self.config = config
        self.config_dict, self.config_sha256 = ledger_io.config_digest(config)
        self.search_set_ids = list(search_set_ids)
        self.initial_pool = list(val_pool_ids)
        self.pool: list[str] = list(val_pool_ids)
        self.fresh_batch_ids = [list(b) for b in fresh_batch_ids]
        self.smoke_ids = list(smoke_sample_ids)
        self.lineage = Lineage()
        # Real Version objects by content hash: adapters need their metadata (SN:
        # candidate_id), which the lineage's hash-only nodes do not carry.
        self.versions: dict[str, Version] = {seed.content_hash: seed}
        # Card of every adopted version (attribution reads its predicate).
        self.version_cards: dict[str, Card] = {}
        # Validation-pool results of parents, per (version hash, pool size).
        # The pool only grows, so its size identifies it. Not persisted: after
        # a restart the adapter replays measurements under deterministic keys.
        self.val_cache: dict[tuple[str, int], EvalResult] = {}
        self.run_failures = 0  # consecutive smoke-run failures (§2.5 流程故障)
        self.milestone = MilestoneState(
            cumulative_delta_val=0.0,
            best_score=float('-inf'),
            last_certified_id=None,
            current_provisional_id=None,
            segment_start_best=float('-inf'),
        )
        self.halt = HaltTracker()
        self.halt.fresh_batches_remaining = len(fresh_batch_ids)
        self.m = ladder_m(config.sigma_d, config.K_max, len(self.pool), config.ladder_c)
        self.rounds: list[RoundResult] = []
        self.round_counter = 0
        self.current: Optional[_RoundProgress] = None
        self.baseline_done = False
        self.best_stale = False          # pool grew; best not yet re-measured
        # §2.3 history: one entry per hypothesis processed (labels only reach the analyst).
        self.history: list[dict] = []
        # §2.1 逐级加码: consecutive no-adoption rounds of the current parent (restarts
        # at 0 whenever the pointer moves), and per parent the failing search tasks
        # already shown to an analyst in the current rotation cycle.
        self.no_adoption: dict = dict(parent=None, rounds=0)
        self.analyst_shown: dict[str, list[str]] = {}
        self.aborted_attempts: list[dict] = []   # halted records moved aside on retry
        self.halt_reason: Optional[str] = None
        self.started_at = _now()
        self.sessions: list[dict] = []
        self.key_generation = 0
        self.key_generation_log: list[dict] = []
        self.state_dir = Path(state_dir) if state_dir is not None else None
        # Safeguards in force (pre-registered; recorded, and a resume must see the same).
        self.safeguards_in_force = adapter.describe_safeguards(config.safeguard_bounds)

        resumed = False
        if self.state_dir is not None and (self.state_dir / ledger_io.STATE_FILE).exists():
            self._restore(ledger_io.read_json(self.state_dir / ledger_io.STATE_FILE))
            resumed = True
        self._apply_key_generation(key_generation, resumed)

    # -- persistence (binding plan §2.7) ----------------------------------

    def _partitions(self) -> dict:
        return dict(search=self.search_set_ids, initial_pool=self.initial_pool,
                    fresh_batches=self.fresh_batch_ids, smoke=self.smoke_ids)

    def _apply_key_generation(self, requested: Optional[int], resumed: bool) -> None:
        if requested is None:
            requested = self.key_generation if resumed else self.adapter.key_generation
        if type(requested) is not int or requested < 0:
            raise ValueError(f"key_generation must be a non-negative int, got {requested!r}")
        if requested < self.key_generation:
            raise ValueError(f"key_generation {requested} is lower than the persisted "
                             f"{self.key_generation}; generations only grow")
        if requested != self.key_generation or not self.key_generation_log:
            if resumed and requested > self.key_generation:
                # Operator-requested retry: earlier failures are not counted again.
                self.run_failures = 0
            self.key_generation_log.append(dict(generation=requested, at=_now(),
                                                round_counter=self.round_counter))
        self.key_generation = requested
        self.adapter.set_key_generation(requested)

    def _state(self) -> dict:
        cur = None
        if self.current is not None:
            cur = dict(
                round=asdict(self.current.round),
                cards=(None if self.current.cards is None
                       else [ledger_io.card_to_dict(c) for c in self.current.cards]),
                submitted=list(self.current.submitted),
                inflight={str(i): h for i, h in self.current.inflight.items()},
                stop_new=self.current.stop_new,
                owed_refills=self.current.owed_refills,
            )
        ms = self.milestone
        return dict(
            schema_version=ledger_io.STATE_SCHEMA_VERSION,
            config=self.config_dict,
            config_sha256=self.config_sha256,
            seed=self.seed.content_hash,
            partitions=self._partitions(),
            safeguards=self.safeguards_in_force,
            versions={h: ledger_io.version_metadata(v) for h, v in self.versions.items()},
            version_cards={h: ledger_io.card_to_dict(c) for h, c in self.version_cards.items()},
            lineage=self.lineage.to_records(),
            milestone=asdict(ms),
            baseline_done=self.baseline_done,
            best_stale=self.best_stale,
            validation_pool=self.pool,
            ladder_m=self.m,
            fresh_batches_used=len(self.fresh_batch_ids) - self.halt.fresh_batches_remaining,
            halt_tracker=dict(vars(self.halt)),
            round_counter=self.round_counter,
            rounds=[asdict(r) for r in self._finished_rounds()],
            current_round=cur,
            run_failures=self.run_failures,
            history=self.history,
            no_adoption=self.no_adoption,
            analyst_shown=self.analyst_shown,
            aborted_attempts=self.aborted_attempts,
            key_generation=self.key_generation,
            key_generation_log=self.key_generation_log,
            halt_reason=self.halt_reason,
            started_at=self.started_at,
            updated_at=_now(),
            sessions=self.sessions,
        )

    def _finished_rounds(self) -> list[RoundResult]:
        if self.current is None:
            return list(self.rounds)
        return [r for r in self.rounds if r is not self.current.round]

    def _save(self) -> None:
        if self.state_dir is not None:
            ledger_io.atomic_write_json(self.state_dir / ledger_io.STATE_FILE, self._state())

    def _restore(self, st: dict) -> None:
        if st.get("schema_version") != ledger_io.STATE_SCHEMA_VERSION:
            raise ValueError(f"state.json schema {st.get('schema_version')!r} is not "
                             f"{ledger_io.STATE_SCHEMA_VERSION}")
        if st.get("config_sha256") != self.config_sha256:
            raise ValueError(
                "ProtocolConfig differs from the one this run was started with (config sha256 "
                f"{st.get('config_sha256')} != {self.config_sha256}); pre-registered parameters "
                "are read-only (method-v3 §3 step 0)")
        if st.get("seed") != self.seed.content_hash:
            raise ValueError(f"seed {self.seed.content_hash!r} differs from the run's seed {st.get('seed')!r}")
        recorded = st.get("safeguards")
        if recorded is not None and recorded != self.safeguards_in_force:
            raise ValueError(f"safeguards in force {self.safeguards_in_force!r} differ from the run's {recorded!r}")
        if st.get("partitions") != self._partitions():
            raise ValueError("search/validation/fresh/smoke ids differ from the ones this run was "
                             "started with; pass the original partitions to resume")
        self.versions = {h: Version(content_hash=h, metadata=meta) for h, meta in st["versions"].items()}
        self.version_cards = {h: ledger_io.card_from_dict(c) for h, c in st.get("version_cards", {}).items()}
        self.lineage = Lineage.from_records(st["lineage"])
        ms = dict(st["milestone"])
        for k in ("best_score", "segment_start_best"):
            if ms.get(k) is None:
                ms[k] = float('-inf')
        self.milestone = MilestoneState(**ms)
        self.baseline_done = st["baseline_done"]
        self.best_stale = st.get("best_stale", False)
        self.pool = list(st["validation_pool"])
        self.m = st["ladder_m"]
        self.halt = HaltTracker()
        for k, v in st["halt_tracker"].items():
            setattr(self.halt, k, v)
        self.round_counter = st["round_counter"]
        self.rounds = [RoundResult(**r) for r in st["rounds"]]
        self.run_failures = st.get("run_failures", 0)
        self.history = list(st.get("history", []))
        # Absent in states written before §2.1 escalation: first tier.
        self.no_adoption = dict(st.get("no_adoption") or dict(parent=None, rounds=0))
        self.analyst_shown = {h: list(v) for h, v in (st.get("analyst_shown") or {}).items()}
        self.aborted_attempts = list(st.get("aborted_attempts", []))
        self.key_generation = st.get("key_generation", 0)
        self.key_generation_log = list(st.get("key_generation_log", []))
        self.started_at = st.get("started_at", self.started_at)
        self.sessions = list(st.get("sessions", []))
        self.halt_reason = None   # re-evaluated by the loop
        cur = st.get("current_round")
        if cur is not None:
            rr = RoundResult(**cur["round"])
            # Records written because the loop halted on a process failure are
            # not decisions: move them aside (still in the ledger) and retry.
            kept = []
            for c in rr.candidates:
                if c.get("halted"):
                    self.aborted_attempts.append(dict(c, round_id=rr.round_id))
                    if (c.get("smoke_verdict") not in (None, 'fail')
                            and c.get("verdict") != 'smoke_fail'):
                        rr.smoke_passed -= 1
                else:
                    kept.append(c)
            rr.candidates = kept
            rr.halt_reason = None
            self.current = _RoundProgress(
                round=rr,
                cards=None if cur["cards"] is None else [ledger_io.card_from_dict(c) for c in cur["cards"]],
                submitted=list(cur["submitted"]),
                inflight={int(i): h for i, h in cur["inflight"].items()},
                stop_new=cur["stop_new"],
                owed_refills=cur.get("owed_refills", 0),
            )
            self.rounds.append(rr)

    def _context(self) -> dict:
        return dict(
            search_set_size=len(self.search_set_ids),
            initial_validation_pool_size=len(self.initial_pool),
            validation_pool_size=len(self.pool),
            smoke_sample_size=len(self.smoke_ids),
            fresh_batches_total=len(self.fresh_batch_ids),
            fresh_batches_used=len(self.fresh_batch_ids) - self.halt.fresh_batches_remaining,
            ladder_m=self.m,
            best_score=self.milestone.best_score,
            halt_reason=self.halt_reason,
            alpha_note=alpha_warning(self.halt.confirmations_used),
            safeguards=self.safeguards_in_force,
            key_generation=self.key_generation,
            key_generation_log=self.key_generation_log,
            timings=dict(started_at=self.started_at, updated_at=_now(), sessions=self.sessions,
                         rounds=[dict(round_id=r.round_id, started_at=r.started_at,
                                      finished_at=r.finished_at) for r in self.rounds]),
            open_round=self.current.round.round_id if self.current is not None else None,
        )

    def _write_ledger(self) -> None:
        if self.state_dir is None:
            return
        ledger = ledger_io.build_ledger(
            rounds=[asdict(r) for r in self.rounds], lineage=self.lineage.to_records(),
            config=self.config_dict, config_sha256=self.config_sha256,
            context=self._context(), aborted_attempts=self.aborted_attempts)
        ledger_io.atomic_write_json(self.state_dir / ledger_io.LEDGER_FILE, ledger)

    def _sync_rounds(self) -> None:
        if self.state_dir is not None:
            ledger_io.sync_rounds_jsonl(self.state_dir / ledger_io.ROUNDS_FILE,
                                        [asdict(r) for r in self._finished_rounds()])

    # -- state helpers ------------------------------------------------------

    def pointer(self) -> Version:
        """The current parent pointer (latest non-rolled-back lineage node)."""
        return self.versions[self.lineage.current_parent().version_hash]

    def val_eval(self, version: Version, purpose: str = 'screen') -> EvalResult:
        key = (version.content_hash, len(self.pool))
        if key not in self.val_cache:
            self.val_cache[key] = self.adapter.evaluate(version, list(self.pool),
                                                        self.config.k_screen, purpose=purpose)
        return self.val_cache[key]

    def _finish(self, reason: str) -> LoopResult:
        self.halt_reason = reason
        self._save()
        self._sync_rounds()
        self._write_ledger()
        return LoopResult(reason, self.lineage, self.rounds,
                          alpha_warning(self.halt.confirmations_used))

    def history_for(self, parent: Version) -> list[dict]:
        """§2.3: labels of hypotheses processed under *parent*; no scores, deltas or task ids.

        Entries tried on nodes of a rolled-back segment are listed under the
        certified node the pointer returned to (``_milestone_confirm``).
        """
        out = []
        for e in self.history:
            if e["parent"] != parent.content_hash:
                continue
            item = dict(title=e["title"], label=e["label"])
            # §2.1 失败后的处理: discrete details only (labels, safeguard key names, flags).
            if e.get("smoke"):
                item["smoke"] = e["smoke"]
            if e.get("safeguards_breached"):
                item["safeguards_breached"] = list(e["safeguards_breached"])
            if e.get("repaired"):
                item["repaired"] = True
            if e.get("rolled_back_segment"):
                item["rolled_back_segment"] = True
            if e.get("attribution"):
                item["attribution"] = e["attribution"]
                item["attribution_possibly_biased"] = bool(e.get("attribution_possibly_biased"))
            out.append(item)
        return out

    def no_adoption_rounds(self, parent: Version) -> int:
        """§2.1 逐级加码: consecutive no-adoption rounds under *parent* (0 unless it is the pointer's count)."""
        na = self.no_adoption
        return na.get('rounds', 0) if na.get('parent') == parent.content_hash else 0

    def _count_no_adoption(self, round_res: RoundResult) -> None:
        """Update the §2.1 no-adoption count at the end of a finished round.

        A no-adoption round ends with the parent pointer where it started,
        with no adoption and no confirmation in between. Any pointer movement
        (adoption, confirmation, or a rollback, even one returning to the
        round's own start) restarts the count at 0 for the new pointer and
        clears its rotation record: the next diagnosis uses the first tier
        (restart-on-pointer-change; a certified node returned to after a
        rollback does not continue an earlier count).
        """
        end = self.pointer().content_hash
        start = round_res.analysis.get('parent')
        stayed = (start == end and round_res.screen_passed == 0
                  and not round_res.milestone_triggered)
        if stayed:
            same = self.no_adoption.get('parent') == end
            self.no_adoption = dict(parent=end, rounds=(self.no_adoption.get('rounds', 0) if same else 0) + 1)
        else:
            self.no_adoption = dict(parent=end, rounds=0)
            self.analyst_shown.pop(end, None)
        if round_res.analysis:
            round_res.analysis.update(pointer_moved=not stayed,
                                      no_adoption_rounds_after=self.no_adoption['rounds'])

    # -- top level ----------------------------------------------------------

    def run(self, max_rounds: int) -> LoopResult:
        self.sessions.append(dict(started_at=_now(), key_generation=self.key_generation,
                                  resumed_at_round=self.round_counter if self.baseline_done else None))
        if not self.baseline_done:
            seed = self.seed
            if self.lineage.current_parent() is None:
                # Seed node: certified by definition (it is the baseline).
                self.lineage.add_provisional(version_hash=seed.content_hash, parent_hash=None,
                                             diff_ref=None, card_ref=None, predicate_ref=None,
                                             agent_cost=0.0)
                self.lineage.certify(seed.content_hash, fresh_result={})
                self.milestone.last_certified_id = seed.content_hash
                self.milestone.current_provisional_id = seed.content_hash
            self._save()
            try:
                seed_eval = self.val_eval(seed, purpose='baseline')
            except EvaluationUnavailable as exc:
                log.error("baseline evaluation unavailable: %s", exc)
                return self._finish(_failure_halt_reason(exc))
            self.milestone.best_score = _mean_score(seed_eval)
            self.milestone.segment_start_best = self.milestone.best_score
            self.baseline_done = True
            self._save()
        else:
            self._sync_rounds()

        while True:
            if self.current is None:
                reason = self.halt.halt_reason(self.config)
                if reason is not None:
                    return self._finish(reason)
                if self.round_counter >= max_rounds:
                    return self._finish('max_rounds')
                self.round_counter += 1
                round_res = RoundResult(round_id=self.round_counter, cards_generated=0,
                                        smoke_passed=0, screen_passed=0,
                                        milestone_triggered=False, confirmation_verdict=None,
                                        rolled_back=False, halt_reason=None, started_at=_now())
                self.rounds.append(round_res)
                self.current = _RoundProgress(round=round_res)
                self._save()
            progress = self.current
            round_res = progress.round
            try:
                if self.best_stale:
                    self._refresh_best()
                failure = self._round(progress)
            except EvaluationUnavailable as exc:
                log.error("evaluation unavailable in round %d: %s", round_res.round_id, exc)
                failure = _failure_halt_reason(exc)
            round_res.val_pool_size = len(self.pool)
            round_res.ladder_m = self.m
            round_res.best_score = self.milestone.best_score
            if failure is not None:
                # The round stays open in state.json: a retry resumes it.
                round_res.halt_reason = failure
                return self._finish(failure)
            self.halt.record_round(round_res.screen_passed)
            self._count_no_adoption(round_res)
            round_res.finished_at = _now()
            self.current = None
            self._save()
            self._sync_rounds()
            self._write_ledger()

    def _round(self, progress: _RoundProgress) -> Optional[str]:
        """One round; returns a failure halt reason or None."""
        round_res = progress.round
        if progress.cards is None:
            parent_v = self.pointer()
            # --- 1. Diagnosis: analyst reads search-set failures ---
            # Search-set runs share k with screening but are their own measurement: a screen
            # runs only the validation pool, so a newly adopted parent is measured here first.
            parent_eval_search = self.adapter.evaluate(parent_v, self.search_set_ids,
                                                       self.config.k_screen, purpose='diagnose')
            # §2.1 逐级加码: rotation over failing tasks not yet shown under this
            # parent, and a larger case cap per consecutive no-adoption round.
            n_stuck = self.no_adoption_rounds(parent_v)
            shown = self.analyst_shown.get(parent_v.content_hash, [])
            max_failures = min(len(self.search_set_ids),
                               self.config.analyst_max_failures + n_stuck * self.config.analyst_max_failures_step)
            selected, roles = select_analysis_cases(parent_eval_search, self.config, shown=shown,
                                                    max_failures=max_failures)
            n_cards = self.config.analyst_cards or self.config.b_cards
            cards = self.adapter.analyse(selected, n_cards, case_roles=roles,
                                         parent=parent_v, round_id=round_res.round_id,
                                         history=self.history_for(parent_v),
                                         no_adoption_rounds=n_stuck)
            failures = [t for t, r in roles.items() if r == 'failure']
            restarted = _rotation_restarts(parent_eval_search, self.config, shown)
            before = set() if restarted else set(shown)
            self.analyst_shown[parent_v.content_hash] = (
                [] if restarted else list(shown)) + [t for t in failures if t not in before]
            stats = getattr(self.adapter, 'last_analysis', None)
            round_res.analysis = dict(
                parent=parent_v.content_hash, no_adoption_rounds=n_stuck, max_failures=max_failures,
                failures_shown=len(failures), failures_new=sum(t not in before for t in failures),
                rotation_cycle_restarted=restarted, controls_shown=len(roles) - len(failures),
                adapter=dict(stats) if isinstance(stats, dict) else None)
            progress.cards = list(cards)
            round_res.cards_generated = len(cards)
            round_res.cards_with_predicate = sum(c.predicate is not None for c in cards)
            self._save()   # the round's cards: a resume never calls analyse again
        if not progress.cards:
            return None
        # --- 2–5. Parallel candidates, sequential decisions ---
        return self._pipeline(progress)

    # -- parallel candidate pipeline ---------------------------------------

    def _pipeline(self, progress: _RoundProgress) -> Optional[str]:
        """Implement/smoke/screen up to b cards concurrently; decide in completion order.

        Refill (§2.1 补位): every non-adopted outcome frees a slot for the next
        unused card, built on the parent pointer at submission time. After a
        milestone this round no new card is submitted; candidates already in
        flight finish and go through the parent-hash rule (rebase/re-screen).
        On EvaluationUnavailable nothing new is submitted and in-flight workers
        are drained (recorded, not decided) before the round returns.

        Resume: cards already decided are skipped; cards in flight at the
        crash are re-submitted on the parent they were built against, so
        workspace/implement/materialize/smoke replay. If that parent is no
        longer the pointer the candidate is only rebuilt (no measurement) and
        then rebased. Never-submitted cards stay queued.
        """
        round_res = progress.round
        cards = progress.cards
        b = max(1, self.config.b_cards)
        submitted = set(progress.submitted)
        resume_started = bool(progress.submitted)
        queue = [(i, c) for i, c in enumerate(cards) if i not in submitted]
        resume = list(progress.inflight.items())
        pending: dict[Future, CandidateRecord] = {}
        failure: Optional[str] = None

        with ThreadPoolExecutor(max_workers=b, thread_name_prefix='v3-cand') as pool_ex:
            def submit(idx: int, card: Card, parent: Version, *, measure: bool = True,
                       resumed: bool = False) -> None:
                rec = CandidateRecord(card=card, card_index=idx, parent=parent, resumed=resumed)
                if idx not in submitted:
                    submitted.add(idx)
                    progress.submitted.append(idx)
                progress.inflight[idx] = parent.content_hash
                fut = pool_ex.submit(_build_and_measure, self.adapter, self.config, rec,
                                     self.smoke_ids, list(self.pool), measure)
                pending[fut] = rec

            def submit_next_card() -> None:
                idx, card = queue.pop(0)
                submit(idx, card, self.pointer())

            # A milestone due but not completed before a restart / failure.
            try:
                if self._confirm_if_due(round_res):
                    progress.stop_new = True
            except EvaluationUnavailable as exc:
                log.error("evaluation unavailable in pending milestone: %s", exc)
                failure = _failure_halt_reason(exc)
            if failure is None:
                pointer = self.pointer().content_hash
                for idx, parent_hash in resume:
                    submit(idx, cards[idx], self.versions[parent_hash],
                           measure=(parent_hash == pointer), resumed=True)
                # Initial fill only for a pipeline that never submitted: after an
                # adoption fewer than b may be in flight, and only a 'refill'
                # decision starts the next card (resume must not run ahead).
                while queue and len(pending) < b and not progress.stop_new and not resume_started:
                    submit_next_card()
                while queue and progress.owed_refills and not progress.stop_new:
                    submit_next_card()
                    progress.owed_refills -= 1
                progress.owed_refills = 0
            self._save()

            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                finished: list[CandidateRecord] = []
                for fut in done:
                    rec = pending.pop(fut)
                    exc = fut.exception()
                    if exc is None:
                        finished.append(rec)
                        continue
                    if not isinstance(exc, EvaluationUnavailable):
                        raise exc
                    log.error("evaluation unavailable for card %r: %s",
                              rec.card.failure_mode_desc[:40], exc)
                    failure = failure or _failure_halt_reason(exc)
                    self._record(round_res, rec, 'error', f"{type(exc).__name__}: {exc}", halted=True)
                    self._save()
                # Several may finish between two wake-ups: keep completion order.
                finished.sort(key=lambda r: r.finished_at)
                for rec in finished:
                    if failure is not None:
                        self._record(round_res, rec, 'error', f"halted: {failure}", halted=True)
                        self._save()
                        continue
                    try:
                        action = self._decide(rec, round_res)
                        if action == 'adopted':
                            self._save()
                            if self._confirm_if_due(round_res):
                                action = 'milestone'
                    except EvaluationUnavailable as exc:  # milestone / re-measurement
                        log.error("evaluation unavailable while deciding: %s", exc)
                        failure = _failure_halt_reason(exc)
                        self._save()
                        continue
                    if (rec.reason or '').startswith(RUN_FAILURE):
                        self.run_failures += 1
                        if self.run_failures >= self.config.max_consecutive_run_failures:
                            log.error("%d consecutive smoke-run failures; halting", self.run_failures)
                            failure = 'process_failure'
                            if action == 'refill' and not progress.stop_new and queue:
                                progress.owed_refills += 1   # a retry starts the next card
                            self._save()
                            continue
                    elif rec.smoke is not None:
                        self.run_failures = 0
                    if action == 'rebase':
                        fut = pool_ex.submit(_rebase_and_measure, self.adapter, self.config, rec,
                                             self.pointer(), self.smoke_ids, list(self.pool))
                        pending[fut] = rec
                    elif action == 'smoke_screen':
                        fut = pool_ex.submit(_smoke_and_screen, self.adapter, self.config, rec,
                                             self.smoke_ids, list(self.pool))
                        pending[fut] = rec
                    elif action == 'rescreen':
                        fut = pool_ex.submit(_screen, self.adapter, self.config, rec,
                                             list(self.pool))
                        pending[fut] = rec
                    elif action == 'milestone':
                        progress.stop_new = True
                    elif action == 'refill' and not progress.stop_new and queue and len(pending) < b:
                        submit_next_card()
                    self._save()
        return failure

    def _decide(self, rec: CandidateRecord, round_res: RoundResult) -> str:
        """Apply the §2.2 A decision to one finished candidate.

        Returns 'refill' (discarded), 'adopted', 'rebase', 'smoke_screen' or
        'rescreen' (measurement missing or not decidable against the current
        parent pointer / validation pool). The milestone check follows an
        adoption in the caller (``_confirm_if_due``).
        """
        if rec.stage == 'built':
            self.versions[rec.candidate.content_hash] = rec.candidate
            return 'rebase' if rec.parent.content_hash != self.pointer().content_hash else 'smoke_screen'
        if rec.stage != 'measured':
            self._record(round_res, rec, rec.stage, rec.reason)
            return 'refill'
        self.versions[rec.candidate.content_hash] = rec.candidate
        pointer = self.pointer()
        if rec.parent.content_hash != pointer.content_hash:
            return 'rebase'
        if rec.pool_size != len(self.pool):
            # Same parent, but a confirmation grew the pool since the
            # measurement: best and m now refer to the enlarged pool.
            return 'rescreen'

        cand_ev, ref_ev = rec.eval_result, rec.ref_result
        self.val_cache.setdefault((pointer.content_hash, len(self.pool)), ref_ev)
        xs, ys, rec.n_unpaired = _paired(cand_ev, ref_ev, self.pool)
        if len(xs) < 2:
            self._record(round_res, rec, 'unmeasurable', 'fewer than 2 tasks scored on both sides')
            return 'refill'
        d = paired_differences(xs, ys)
        rec.delta_val = mean_delta(d)
        rec.se_val = paired_se(d)
        score = _mean_score(cand_ev)
        best_before = self.milestone.best_score
        if rec.safeguards is None:
            rec.safeguards = _check_safeguards(self.adapter, cand_ev, ref_ev, self.config)
        rec.screen_verdict = screen_decision(
            rec.delta_val, rec.se_val, self.config.z_screen,
            best_before, score, self.m, rec.safeguards['ok'],
        )
        self._record(round_res, rec, rec.screen_verdict, None,
                     score_val=score, best_before=best_before)
        if rec.screen_verdict != 'screen_pass':
            return 'refill'

        # --- Provisional adoption: parent pointer advances, best climbs ---
        round_res.screen_passed += 1
        cand = rec.candidate
        self.lineage.add_provisional(
            version_hash=cand.content_hash,
            parent_hash=pointer.content_hash,
            diff_ref=rec.diff.content[:60] if rec.diff else None,
            card_ref=rec.card.failure_mode_desc[:60],
            predicate_ref=(rec.card.predicate.boolean_expression if rec.card.predicate else None),
            agent_cost=sum(cand_ev.costs.values()),
        )
        self.version_cards[cand.content_hash] = rec.card
        self.val_cache[(cand.content_hash, len(self.pool))] = cand_ev
        self.milestone.current_provisional_id = cand.content_hash
        self.milestone.cumulative_delta_val += rec.delta_val
        self.milestone.best_score = score
        return 'adopted'

    _HISTORY_LABEL = {'smoke_fail': 'smoke_fail', 'screen_fail': 'screen_fail',
                      'screen_pass': 'provisional_adopted', 'unmeasurable': 'unmeasurable'}

    def _record(self, round_res: RoundResult, rec: CandidateRecord, verdict: str,
                reason: Optional[str], *, score_val: Optional[float] = None,
                best_before: Optional[float] = None, halted: bool = False) -> None:
        # §2.4 integrity: the card's superseded (smoke-failed, then repaired)
        # attempts first, each its own record; they never enter the history.
        for prior in rec.prior_attempts:
            round_res.candidates.append(self._attempt_record(
                prior, 'smoke_fail', f"repaired: attempt {prior.attempt + 1} follows",
                halted=halted, superseded=True))
        rec.prior_attempts = []
        if rec.smoke is not None and rec.smoke.verdict != 'fail' and verdict != 'smoke_fail':
            round_res.smoke_passed += 1
        if (verdict == 'error' and rec.delta_val is None and rec.eval_result is not None
                and rec.ref_result is not None and rec.pool_size):
            # Measured but undecided (halted): keep Δ for selection accounting.
            xs, ys, rec.n_unpaired = _paired(rec.eval_result, rec.ref_result, self.pool[:rec.pool_size])
            if len(xs) >= 2:
                d = paired_differences(xs, ys)
                rec.delta_val, rec.se_val = mean_delta(d), paired_se(d)
        round_res.candidates.append(self._attempt_record(rec, verdict, reason, score_val=score_val,
                                                         best_before=best_before, halted=halted))
        if halted:
            return   # not a decision: the card stays in flight for a retry
        if self.current is not None:
            self.current.inflight.pop(rec.card_index, None)
        label = self._HISTORY_LABEL.get(verdict)
        if label is None and rec.repair_failed:
            label = 'smoke_fail'   # the repair produced no candidate: the smoke failure stands
        if label is not None:
            self.history.append(self._history_entry(round_res, rec, label))

    def _attempt_record(self, rec: CandidateRecord, verdict: str, reason: Optional[str], *,
                        score_val: Optional[float] = None, best_before: Optional[float] = None,
                        halted: bool = False, superseded: bool = False) -> dict:
        return dict(
            card_ref=rec.card.ref or rec.card.failure_mode_desc[:60],
            title=rec.card.failure_mode_desc,
            card_index=rec.card_index,
            verdict=verdict,
            reason=reason,
            parent=rec.parent.content_hash,
            candidate=rec.candidate.content_hash if rec.candidate else None,
            candidate_id=(rec.candidate.metadata or {}).get("candidate_id") if rec.candidate else None,
            rebased_from=rec.rebased_from,
            smoke_verdict=rec.smoke.verdict if rec.smoke else None,
            delta_val=rec.delta_val,
            se_val=rec.se_val,
            score_val=score_val,
            best_before=best_before,
            ladder_m=self.m,
            val_pool_size=rec.pool_size or None,
            screen_evaluations=rec.screen_evaluations,
            n_unpaired=rec.n_unpaired,
            safeguards=rec.safeguards,
            timings=dict(rec.timings),
            has_predicate=rec.card.predicate is not None,
            resumed=rec.resumed,
            halted=halted,
            attempt=rec.attempt,
            repair_of=rec.repair_of,
            repaired=rec.attempt > 1,
            superseded=superseded,
        )

    @staticmethod
    def _history_entry(round_res: RoundResult, rec: CandidateRecord, label: str) -> dict:
        """One §2.3 history entry, with the §2.1 失败后的处理 discrete details.

        screen_fail carries the smoke conclusion ('behaviour_changed': the
        behavioural smoke passed; 'unchecked': no behaviour check ran);
        unmeasurable carries the names of the breached safeguards (never their
        values); a repaired attempt is marked ``repaired``.
        """
        entry = dict(round_id=round_res.round_id, parent=rec.parent.content_hash,
                     candidate=rec.candidate.content_hash if rec.candidate else None,
                     card_ref=rec.card.ref, title=rec.card.failure_mode_desc, label=label)
        if rec.attempt > 1:
            entry['repaired'] = True
        if label == 'screen_fail' and rec.smoke is not None:
            entry['smoke'] = ('behaviour_changed' if rec.smoke.behavioral_check_ran
                              and rec.smoke.verdict == 'pass' else 'unchecked')
        if label == 'unmeasurable':
            breached = _breached_safeguards(rec.safeguards)
            if breached:
                entry['safeguards_breached'] = breached
        return entry

    # -- milestone confirmation ---------------------------------------------

    def _confirm_if_due(self, round_res: RoundResult) -> bool:
        """§2.2 B trigger: run the confirmation when the open segment reached T.

        Called after every adoption and when a pipeline (re)starts, so a
        confirmation interrupted by a crash or process failure runs on resume.
        """
        ms = self.milestone
        if ms.current_provisional_id == ms.last_certified_id:
            return False
        if ms.cumulative_delta_val < self.config.T:
            return False
        if not self.halt.can_confirm(self.config.max_confirmations):
            # No fresh batch / confirmation budget left: the segment stays
            # provisional and the loop halts at the next round boundary.
            log.warning("milestone triggered but no confirmation available; segment stays provisional")
            return False
        round_res.milestone_triggered = True
        conf = self._milestone_confirm()
        round_res.confirmations.append(conf)
        round_res.confirmation_verdict = conf['verdict']
        if conf['verdict'] != 'confirmed':
            round_res.rolled_back = True
        self.halt.record_confirmation(conf['verdict'])
        self._save()
        self._refresh_best()
        self._save()
        return True

    def _refresh_best(self) -> None:
        """Re-measure the parent pointer on the (grown) pool; best and the segment start follow it."""
        ms = self.milestone
        ms.best_score = _mean_score(self.val_eval(self.pointer()))
        ms.segment_start_best = ms.best_score
        self.best_stale = False
        for rnd in reversed(self.rounds):
            pending = [c for c in rnd.confirmations if c.get('best_score') is None]
            if pending:
                pending[-1]['best_score'] = ms.best_score
                break

    def _milestone_confirm(self) -> dict:
        """Fresh-batch confirmation of segment end vs segment start (§2.2 B).

        Applies the state-transition table, folds the batch into the
        validation pool (every outcome) and recomputes m. Best is re-measured
        on the enlarged pool by ``_refresh_best`` (marked stale until then, so
        a crash in between re-measures on resume).
        """
        cfg, ms = self.config, self.milestone
        batch_index = len(self.fresh_batch_ids) - self.halt.fresh_batches_remaining
        fresh_ids = list(self.fresh_batch_ids[batch_index])
        start_v = self.versions[ms.last_certified_id]
        end_v = self.versions[ms.current_provisional_id]
        segment = self.lineage.provisional_segment(ms.last_certified_id)
        cumulative = ms.cumulative_delta_val
        end_eval, start_eval = self.adapter.evaluate_pair(end_v, start_v, fresh_ids,
                                                          cfg.k_screen, purpose='confirm')
        xs, ys, n_unpaired = _paired(end_eval, start_eval, fresh_ids)
        safeguards = _check_safeguards(self.adapter, end_eval, start_eval, cfg)
        if len(xs) < 2:
            delta_fresh = se_fresh = None
            verdict = 'unmeasurable'  # consumed and folded in like every outcome
        else:
            d = paired_differences(xs, ys)
            delta_fresh = mean_delta(d)
            se_fresh = paired_se(d)
            # A safeguard breach or an unobserved safeguard: unmeasurable -> rollback (§2.2).
            verdict = confirm_decision(delta_fresh, se_fresh, cfg.z_confirm, safeguards['ok'])
        attribution = self._attribution(end_v, end_eval, start_eval, fresh_ids)

        # Every fallible step is done: apply the transition (no exception
        # below leaves a half-applied state to persist).
        self.halt.fresh_batches_remaining -= 1
        if verdict == 'confirmed':
            self.lineage.certify(end_v.content_hash, fresh_result={
                'delta_fresh': delta_fresh, 'se_fresh': se_fresh, 'fresh_ids': fresh_ids,
            })
            ms.last_certified_id = end_v.content_hash
            new_pointer = end_v
        else:
            # rejected / insufficient / unmeasurable: the whole provisional
            # segment is discarded; the pointer returns to the last certified node.
            for node in segment:
                self.lineage.rollback(node.version_hash)
            new_pointer = start_v
        ms.current_provisional_id = new_pointer.content_hash
        ms.cumulative_delta_val = 0.0
        ms.n_confirmations += 1

        # §2.3 history: the segment's adopted hypotheses now carry the outcome.
        seg_hashes = {n.version_hash for n in segment}
        for e in self.history:
            if e['label'] == 'provisional_adopted' and e.get('candidate') in seg_hashes:
                e['label'] = verdict
                if verdict == 'unmeasurable' and _breached_safeguards(safeguards):
                    e['safeguards_breached'] = _breached_safeguards(safeguards)   # names only
                if e['candidate'] == end_v.content_hash:
                    e['attribution'] = attribution['label']
                    e['attribution_possibly_biased'] = bool(attribution.get('possibly_biased'))
        if verdict != 'confirmed':
            # §2.1 失败后的处理: every hypothesis tried on a node of the rolled-back
            # segment (adopted or not) is listed under the certified node the
            # pointer returns to, marked as tried in a rolled-back segment.
            for e in self.history:
                if e['parent'] in seg_hashes:
                    e.setdefault('tried_under', e['parent'])
                    e['parent'] = start_v.content_hash
                    e['rolled_back_segment'] = True

        # Fold F_i into the validation pool (all outcomes), then recompute m.
        # Best is re-measured on the enlarged pool (_refresh_best). On rollback
        # the reference is the last certified node, not the old best.
        known = set(self.pool)
        self.pool.extend(t for t in fresh_ids if t not in known)
        self.m = ladder_m(cfg.sigma_d, cfg.K_max, len(self.pool), cfg.ladder_c)
        self.best_stale = True
        return dict(
            verdict=verdict,
            delta_fresh=delta_fresh,
            se_fresh=se_fresh,
            n_fresh=len(fresh_ids),
            n_unpaired=n_unpaired,
            k_fresh=cfg.k_screen,
            fresh_batch_index=batch_index,
            segment_start=start_v.content_hash,
            segment_end=end_v.content_hash,
            segment_steps=len(segment),
            cumulative_delta_val=cumulative,
            safeguards_end=safeguards,
            cost_start=sum(start_eval.costs.values()),
            cost_end=sum(end_eval.costs.values()),
            attribution=attribution,
            val_pool_size=len(self.pool),
            ladder_m=self.m,
            best_score=None,   # set by _refresh_best
        )

    def _attribution(self, end_v: Version, end_eval: EvalResult, start_eval: EvalResult,
                     fresh_ids: list[str]) -> dict:
        """§2.2 归因 on F_i for the segment-end card (descriptive; never gates adoption).

        S = fresh tasks where the segment START's run is NOT yet in the state the
        prediction aims for: predicate True for a 'decrease' prediction, False
        for an 'increase' one (otherwise r_p would be 1 by construction and an
        'increase' could never move). r_p / r_c are the predicate's rate over S
        on the start / end runs; Δ_S / Δ_¬S come from the paired scores. None-valued features are not computable and leave both
        S and ¬S. The start run that chooses S is the same run whose scores
        enter d_i, so the slice is NOT independent: the label is recorded with
        possibly_biased=True (§2.2: "可能有偏，仅供参考").
        """
        card = self.version_cards.get(end_v.content_hash)
        if card is None or card.predicate is None:
            return dict(label='unattributed', possibly_biased=False,
                        reason='segment-end card has no predicate')
        pred = card.predicate
        base = dict(predicted_direction=pred.predicted_direction, independent_parent_slice=False,
                    n_min=self.config.attribution_n_min)
        try:
            f_start = self.adapter.features(start_eval.trajectories, pred)
            f_end = self.adapter.features(end_eval.trajectories, pred)
        except Exception as exc:  # descriptive only: never halts or blocks the confirmation
            return dict(base, label='insufficient', possibly_biased=False,
                        reason=f"predicate features not computable: {type(exc).__name__}: {exc}")

        def rate(values: list[Optional[bool]]) -> Optional[float]:
            vals = [v for v in values if v is not None]
            return sum(vals) / len(vals) if vals else None

        def delta(ids: list[str]) -> tuple[Optional[float], int]:
            xs, ys, _ = _paired(end_eval, start_eval, ids)
            return (mean_delta(paired_differences(xs, ys)) if xs else None), len(xs)

        target_absent = pred.predicted_direction != 'increase'  # value marking "not there yet"
        S = [t for t in fresh_ids if f_start.get(t) is target_absent]
        not_S = [t for t in fresh_ids if f_start.get(t) is (not target_absent)]
        r_p = rate([f_start.get(t) for t in S])
        r_c = rate([f_end.get(t) for t in S])
        delta_S, n_S = delta(S)
        delta_not_S, n_not_S = delta(not_S)
        reason = ("slice chosen on the same segment-start run whose scores enter d_i "
                  "(no independent parent run on F_i)")
        result = attribution_result(r_p, r_c, delta_S, delta_not_S, pred.predicted_direction,
                                    self.config.attribution_n_min, n_S,
                                    independent_parent_slice=False, bias_reason=reason)
        return dict(base, **result, r_p=r_p, r_c=r_c, delta_S=delta_S, delta_notS=delta_not_S,
                    slice_size=n_S, not_slice_size=n_not_S,
                    n_not_computable=len(fresh_ids) - len(S) - len(not_S))


# ---------------------------------------------------------------------------
# Analyst case selection (§2.3)
# ---------------------------------------------------------------------------

def _failing(search_eval: EvalResult, config: ProtocolConfig) -> list[str]:
    """Failing search tasks, worst first (ties by task id)."""
    scores = search_eval.per_task_scores
    return sorted((t for t, s in scores.items() if s < config.analyst_failure_threshold),
                  key=lambda t: (scores[t], t))


def _rotation_restarts(search_eval: EvalResult, config: ProtocolConfig,
                       shown: Optional[list[str]]) -> bool:
    """True when every failing task was already shown: the rotation starts a new cycle."""
    seen = set(shown or ())
    return bool(seen) and all(t in seen for t in _failing(search_eval, config))


def select_analysis_cases(
    search_eval: EvalResult, config: ProtocolConfig, *,
    shown: Optional[list[str]] = None, max_failures: Optional[int] = None,
) -> tuple[dict[str, list], dict[str, str]]:
    """Pick the search cases the analyst sees; returns (trajectories, roles).

    Failures: score < analyst_failure_threshold, worst first (ties by task id),
    at most *max_failures* (default analyst_max_failures).
    Controls: passing tasks ordered by sha256(task_id), so the choice is fixed
    and does not depend on which candidate is being diagnosed. The analyst
    receives only these trajectory stubs and a failure/control label, no scores.

    §2.1 轮换选题: failing tasks in *shown* (already shown to an analyst under
    this parent) come after the unshown ones, each group worst first; once
    every failing task was shown the rotation starts a new cycle (``shown``
    ignored, ``_rotation_restarts``).
    """
    scores = search_eval.per_task_scores
    cap = config.analyst_max_failures if max_failures is None else max_failures
    failing = _failing(search_eval, config)
    seen = set() if _rotation_restarts(search_eval, config, shown) else set(shown or ())
    failures = ([t for t in failing if t not in seen] + [t for t in failing if t in seen])[:cap]
    passing = sorted((t for t, s in scores.items() if s >= config.analyst_failure_threshold),
                     key=lambda t: hashlib.sha256(t.encode()).hexdigest())[:config.analyst_n_controls]
    roles = {**{t: 'failure' for t in failures}, **{t: 'control' for t in passing}}
    trajectories = {t: search_eval.trajectories.get(t, []) for t in roles}
    return trajectories, roles


def _breached_safeguards(check: Optional[dict]) -> list[str]:
    """Names of the breached safeguards of a SystemAdapter.safeguards result (no values)."""
    prefix = 'safeguard_breached:'
    return sorted({r[len(prefix):] for r in (check or {}).get('reasons') or []
                   if isinstance(r, str) and r.startswith(prefix)})


# ---------------------------------------------------------------------------
# Small score helpers
# ---------------------------------------------------------------------------

def _mean_score(ev: EvalResult) -> float:
    scores = list(ev.per_task_scores.values())
    return sum(scores) / len(scores) if scores else 0.0


