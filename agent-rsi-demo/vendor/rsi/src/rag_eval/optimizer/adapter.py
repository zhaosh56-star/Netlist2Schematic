"""System adapter interface and canonical data objects for the v3 protocol.

Defines the four-capability ABC (§2.6) and the agent-role methods (§2.3) from
method-v3.md.  All model, SDK, and SN-specific code lives behind these
interfaces; the method-core package never imports a model client directly.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class Version:
    """Frozen harness snapshot identified by a content hash."""
    content_hash: str
    metadata: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Predicate:
    """Mechanically computable trajectory predicate (sandboxed).

    boolean_expression may be a callable or a serialisable spec; callers must
    sandbox it before execution (no file/network/reflection access allowed).
    """
    boolean_expression: Any            # callable or serialisable spec
    predicted_direction: str           # 'decrease' | 'increase'
    predicted_threshold: Optional[float] = None


@dataclass(frozen=True)
class Card:
    """Analysis-agent output: failure-mode description, change hypothesis, optional predicate."""
    failure_mode_desc: str
    change_hypothesis: str
    predicate: Optional[Predicate] = None
    ref: Optional[str] = None          # target-system card id (SN: v2 hypothesis id)


@dataclass(frozen=True)
class Diff:
    """Implementer output: textual or structured diff against the parent version."""
    content: str
    test_refs: tuple[str, ...] = ()
    metadata: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class EvalResult:
    """Per-version evaluation result for a batch of tasks."""
    per_task_scores: dict[str, float]        # task_id → mean score over k runs
    trajectories: dict[str, list[Any]]       # task_id → list of k trajectories
    costs: dict[str, float]                  # task_id → aggregate cost
    error_classes: dict[str, Optional[str]]  # task_id → error class or None
    safeguard_values: dict[str, float] = field(default_factory=dict, compare=False)
    # Adapter-private provenance of the result (SN: candidate id and the scope of
    # each task), read back by the same adapter, e.g. by ``safeguards``.
    context: dict = field(default_factory=dict, compare=False)
    # Per admitted attempt, task_id -> score (method-v4 calibration reads rerun noise
    # from it; the per-task mean above is the mean over these). Empty when unknown.
    attempt_scores: list = field(default_factory=list, compare=False)


@dataclass
class SmokeResult:
    """Result of the §2.2 cheap-discard smoke check."""
    candidate_version: str
    card_ref: str
    behavioral_check_ran: bool
    feature_present_before: Optional[bool]   # None when no predicate
    feature_present_after: Optional[bool]    # None when no predicate
    verdict: str                             # 'pass' | 'fail' | 'unchecked'


PURPOSES = ("baseline", "diagnose", "screen", "smoke", "confirm", "sealed")


class ImplementationDeclined(RuntimeError):
    """The implementer reported an evidenced preflight skip, before measurement."""

    def __init__(self, report: dict):
        self.report = report
        super().__init__(report.get("summary", "implementation declined"))


class EvaluationUnavailable(RuntimeError):
    """An evaluation could not produce results (infrastructure, timeout, refusal).

    The loop stops with a process-failure reason rather than treating the
    candidate as rejected: missing measurements are not evidence (§2.5).
    """


class AgentRequestFailed(RuntimeError):
    """A durable Agent request failed; measurement retries cannot repair it."""


class BudgetExhausted(EvaluationUnavailable):
    """The frozen run budget does not admit the requested evaluation."""


class EvaluationCancelled(EvaluationUnavailable):
    """Cooperative cancellation or the optimisation deadline stopped work."""


class SystemAdapter(abc.ABC):
    """Four-capability interface the method-core package depends on (§2.6).

    All SN-specific, model-client, and filesystem concerns live in subclasses.
    """

    def configure_runtime(self, *, remaining: Callable[[], float],
                          max_concurrent_executions: int) -> None:
        """Optional per-run deadline and actual execution admission contract.

        Simple adapters may finish their synchronous calls normally. Runtime
        adapters check ``remaining`` before dispatch and during bounded waits.
        """

    def check_runtime(self) -> None:
        """Raise EvaluationCancelled when configured work must stop (optional)."""

    def cancel_pending(self) -> None:
        """Request cancellation of this run's owned work (optional, idempotent)."""

    def stop_polling(self) -> None:
        """Stop local waiters after process interruption; preserve remote receipts."""

    @abc.abstractmethod
    def materialize(self, diff: Diff, parent: Version) -> Version:
        """Apply diff to parent; return a new frozen Version."""

    @abc.abstractmethod
    def evaluate(
        self,
        version: Version,
        task_ids: list[str],
        k: int,
        *,
        purpose: str = "screen",
    ) -> EvalResult:
        """Scores (and trajectory stubs) of one version on task_ids, k runs each.

        *purpose* (one of PURPOSES) tells the adapter which partition is meant,
        so it never has to guess from the id set: 'baseline' / 'diagnose' /
        'screen' read the search set or validation pool. Fresh batches and
        smoke samples are only evaluated in pairs (``evaluate_pair``).
        An adapter may run fewer new attempts when earlier ones already exist
        (top-up semantics); it must raise rather than silently change k.
        """

    def evaluate_pair(
        self,
        version: Version,
        reference: Version,
        task_ids: list[str],
        k: int,
        *,
        purpose: str,
    ) -> tuple[EvalResult, EvalResult]:
        """(version, reference) on the same task_ids, k runs each, for paired d_i.

        purpose: 'smoke' (candidate vs parent on the smoke sample, k=1),
        'screen' (candidate vs parent on the validation pool), 'confirm'
        (segment end vs last certified node on one fresh batch; consumes it).
        Default: two independent ``evaluate`` calls, adequate for systems
        without paired dispatch. Adapters whose runtime pairs runs override.
        """
        return (self.evaluate(version, task_ids, k, purpose=purpose),
                self.evaluate(reference, task_ids, k, purpose=purpose))

    def rebase(self, candidate: Version, new_parent: Version) -> Optional[Version]:
        """Re-apply *candidate*'s change onto *new_parent* (§2.2 A precondition).

        Returns the rebased, frozen Version, or None when unsupported or on
        conflict — the loop then discards the candidate and refills from the
        card queue.
        """
        return None

    @abc.abstractmethod
    def features(
        self,
        trajectories: dict[str, list[Any]],
        predicate: Optional[Predicate],
    ) -> dict[str, Optional[bool]]:
        """Evaluate predicate against each task's trajectories.

        Returns task_id → bool, or None when not computable for that task
        (missing observation); callers drop None from rates.  Must return {}
        when predicate is None.
        """

    @abc.abstractmethod
    def workspace(self, version: Version, card: Optional[Card] = None) -> Any:
        """Return an isolated workspace handle, derived from *version*, for implementing *card*.

        Systems whose versions are tree nodes (SN) create the child node here;
        the handle then identifies it for implement() and materialize().
        """

    @abc.abstractmethod
    def analyse(
        self,
        search_set_trajectories: dict[str, list[Any]],
        n_cards: int,
        *,
        case_roles: Optional[dict[str, str]] = None,
        parent: Optional[Version] = None,
        round_id: Optional[int] = None,
        history: Optional[list[dict]] = None,
        no_adoption_rounds: int = 0,
    ) -> list[Card]:
        """Run the analyst agent on search-set trajectories only (§2.3 isolation).

        The loop passes only accountant-selected cases; *case_roles* labels each
        'failure' or 'control'. No scores are passed. *parent* is the version
        being diagnosed and *round_id* the loop round; adapters use them to bind
        cards to the parent and to derive restart-stable request keys.

        *history* (§2.3 历史标签): hypotheses already processed under this
        parent, oldest first, as ``{'title', 'label'}`` with label one of
        smoke_fail / screen_fail / provisional_adopted / confirmed / rejected /
        insufficient / unmeasurable, plus optional ``attribution`` (supported /
        refuted / insufficient / unattributed) and
        ``attribution_possibly_biased``. Never scores, deltas or task ids.
        §2.1 失败后的处理 adds optional discrete details: ``smoke``
        ('behaviour_changed' / 'unchecked', on screen_fail), ``safeguards_breached``
        (safeguard key names, on unmeasurable), ``repaired`` (the outcome is
        that of a smoke-repair attempt) and ``rolled_back_segment`` (tried on a
        node of a segment that was rolled back).

        *no_adoption_rounds* (§2.1 逐级加码): consecutive rounds under this
        parent that ended without the parent pointer moving. N >= 1 asks the
        analyst for different failure modes / components than the history
        titles; adapters may widen their evidence budget per round.

        Returns up to n_cards Cards.
        """

    @abc.abstractmethod
    def implement(self, card: Card, workspace: Any) -> Diff:
        """Run the implementer agent for one card; return a Diff."""

    def repair(
        self,
        card: Card,
        parent: Version,
        failed: Version,
        *,
        smoke: SmokeResult,
        candidate_smoke: EvalResult,
        parent_smoke: EvalResult,
        attempt: int,
    ) -> Optional[Diff]:
        """Smoke repair (§2.1 失败后的处理): a further attempt of *card* after a behavioural smoke fail.

        *failed* (a frozen child of *parent*) did not move its predicate in the
        predicted direction on the smoke sample. The implementer gets one more
        attempt on a fresh, unfrozen child of the same *parent* that starts from
        the failed change, plus the smoke tasks' (search set) predicate values
        and diagnostic views of parent and *failed*, built from
        ``candidate_smoke`` / ``parent_smoke`` trajectories only: never scores,
        gold or task ids (§2.3). *attempt* is 2 for the first repair.

        Returns the Diff of that child, which the loop freezes with
        ``materialize(diff, parent)`` and smokes again; None when the system
        does not support repairs (the smoke failure then stands).
        """
        return None

    # -- method-v4 capabilities (method-v4.md §3.2, §6) ------------------------

    def evaluate_subset(
        self,
        version: Version,
        reference: Optional[Version],
        subset: str,
        task_ids: list[str],
        k: int,
    ) -> tuple[EvalResult, Optional[EvalResult]]:
        """method-v4: *version* on a fixed validation subset ('mb' or 'rest'), k attempts.

        With a *reference* (the version's parent) the reference is topped up to
        the same k on the same tasks and both results come back for a paired
        read; without one (calibration of the seed) only *version* runs. Results
        must be cacheable per (version, task, attempt): an existing attempt is
        reused, never repeated.
        """
        raise NotImplementedError("this adapter does not implement method-v4 subsets")

    def observe_safeguards(self, candidate: EvalResult, reference: EvalResult) -> dict:
        """method-v4: point observations of the safeguards of one pair, no verdict.

        Returns ``{key: {"direction": "higher_is_better" | "lower_is_better",
        "subject": float | None, "reference": float | None}}``; None = unobserved.
        Default: ``safeguard_values`` of both results, lower is better.
        """
        keys = set(candidate.safeguard_values) | set(reference.safeguard_values)
        return {k: dict(direction="lower_is_better", subject=candidate.safeguard_values.get(k),
                        reference=reference.safeguard_values.get(k)) for k in sorted(keys)}

    def evaluate_search(self, version: Version, task_ids: list[str], k: int) -> EvalResult:
        """method-v4 diagnosis: *version* alone on a search-set sample, k attempts (default: ``evaluate``)."""
        return self.evaluate(version, task_ids, k, purpose="diagnose")

    def touches_preparation(self, diff: Optional[Diff]) -> Optional[bool]:
        """method-v4 §6 cost estimate: whether *diff* invalidates the system's preparation cache; None if unknown."""
        return None

    def task_families(self, task_ids: list[str]) -> dict[str, str]:
        """method-v4: task -> family label (SN: paper) for stratified minibatches and clustered SE; {} if none."""
        return {}

    def timing_profile(self, result: EvalResult) -> list[dict]:
        """method-v4 §6 (optional cost estimate): per execution behind *result*,
        ``{wall_s, cases, cache_hits, cache_misses, lock_waited_s}``; [] when unknown."""
        return []

    def review(self, card: Card, diff: Optional[Diff], pairs: list[dict], *, candidate: Version,
               key: str) -> Optional[list[dict]]:
        """method-v4 §3.2 stage 3: one independent reviewer reads anonymous trajectory pairs.

        *pairs*: ``{"pair": "p1", "task": id, "A": [trajectory refs], "B": [trajectory refs]}``,
        shuffled by the accountant; which side is the candidate is not passed.
        ``task`` is for loading the trajectories only: the reviewer must not
        learn task ids, scores, gold or which side is the candidate. *candidate*
        is the version under review (the system may bind the reviewer to it). *key* is a
        restart-stable identity of this review. Returns one dict per pair
        ``{pair, effect, changed_side ('A'|'B'|None), side_effect, gaming, note}``,
        or None when the system has no reviewer (the mechanism tier then rests
        on the mechanical check alone).
        """
        return None

    # -- safeguards (§1 保护指标) --------------------------------------------

    def safeguards(self, candidate: EvalResult, reference: EvalResult, bounds: dict) -> dict:
        """Pre-registered safeguards of one paired decision (screen / confirmation).

        *candidate* and *reference* come from the same ``evaluate_pair`` call;
        *bounds* is ``ProtocolConfig.safeguard_bounds``. Returns
        ``{source, ok, reasons, checks}``: ``ok`` False (a breach, or a
        safeguard that could not be observed) makes the decision unmeasurable
        (§2.2). ``checks`` holds one row per safeguard with its observed values.

        Default: each bound is a maximum of ``candidate.safeguard_values[metric]``
        (absent counts as 0.0). Systems with their own pre-registered
        safeguards (SN: the iteration protocol) override this.
        """
        checks = []
        for metric, max_delta in (bounds or {}).items():
            value = candidate.safeguard_values.get(metric, 0.0)
            checks.append(dict(key=metric, margin=max_delta, subject=value, breached=value > max_delta))
        reasons = [f"safeguard_breached:{c['key']}" for c in checks if c["breached"]]
        return dict(source="protocol_config", ok=not reasons, reasons=reasons, checks=checks)

    def describe_safeguards(self, bounds: dict) -> dict:
        """Which safeguards are in force for this run (recorded in state.json and the ledger)."""
        return dict(source="protocol_config", bounds=dict(bounds or {}))

    # -- evaluation request generation (operator retry) ----------------------

    @property
    def key_generation(self) -> int:
        """Generation suffix for restart-stable evaluation request keys (default 0).

        Systems that replay a stored receipt per request key (SN/v2) also
        replay a *failed* result forever. After a process-failure halt the
        operator may request a retry; the loop then bumps the generation (it
        is persisted in state.json) and calls ``set_key_generation`` so the
        adapter issues fresh measurement keys. Adapters without keyed replay
        ignore it.
        """
        return getattr(self, "_key_generation", 0)

    def set_key_generation(self, generation: int) -> None:
        if type(generation) is not int or generation < 0:
            raise ValueError(f"key_generation must be a non-negative int, got {generation!r}")
        self._key_generation = generation
