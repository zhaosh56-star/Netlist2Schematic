"""method-v4 §3.2 stage 3: mechanical behaviour check and the reviewer's blinded pairs.

Validation trajectories stay with the accountant: the reviewer receives only
the pairs ``sample_pairs`` builds (no task ids, no scores, sides shuffled), and
only the tier goes back to any proposing Agent.
"""
from __future__ import annotations

import hashlib
import random
from typing import Any, Optional

from ..adapter import Card, EvalResult, Predicate, SystemAdapter


def _rate(values: dict) -> tuple[Optional[float], int]:
    known = [bool(v) for v in values.values() if v is not None]
    return (sum(known) / len(known) if known else None), len(known)


def attempt_view(result: EvalResult, index: int) -> dict[str, list]:
    """Trajectories of the *index*-th attempt of each task (tasks without it are left out)."""
    return {t: [trajs[index]] for t, trajs in result.trajectories.items() if trajs and len(trajs) > index}


def latest_view(result: EvalResult) -> dict[str, list]:
    return {t: [trajs[-1]] for t, trajs in result.trajectories.items() if trajs}


def mechanical(adapter: SystemAdapter, predicate: Optional[Predicate], candidate: EvalResult,
               parent: EvalResult, seed_aa: Optional[EvalResult]) -> dict:
    """'changed' / 'unchanged' / 'not_computable' / None (no behaviour expectation).

    changed: rate moved from parent to candidate in the predicted direction by
    more than the seed's A/A gap (|rate(attempt 1) − rate(attempt 2)| of the same
    predicate on the validation set). The seed stands in for "the parent's A/A":
    adopted parents run the minibatch only once, the seed was calibrated twice.
    """
    if predicate is None:
        return dict(status=None)
    rc, nc = _rate(adapter.features(latest_view(candidate), predicate))
    rp, np_ = _rate(adapter.features(latest_view(parent), predicate))
    gap = None
    if seed_aa is not None:
        a, _ = _rate(adapter.features(attempt_view(seed_aa, 0), predicate))
        b, _ = _rate(adapter.features(attempt_view(seed_aa, 1), predicate))
        gap = abs(a - b) if a is not None and b is not None else None
    out = dict(candidate_rate=rc, parent_rate=rp, n_candidate=nc, n_parent=np_, aa_gap=gap,
               direction=predicate.predicted_direction)
    if rc is None or rp is None:
        return dict(out, status="not_computable")
    move = rc - rp if predicate.predicted_direction == "increase" else rp - rc
    return dict(out, status="changed" if move > (gap or 0.0) else "unchanged")


def smoke_behaviour(adapter: SystemAdapter, predicate: Optional[Predicate], candidate: EvalResult,
                    parent: EvalResult) -> dict:
    """Stage 0 behaviour check: 'pass' / 'fail' / 'unchecked'.

    Unchecked when there is no expectation, nothing is computable, or the
    parent's rate already sits at the bound the prediction moves towards (it
    cannot move further on these tasks).
    """
    if predicate is None:
        return dict(verdict="unchecked", reason="no behaviour expectation")
    rc, _ = _rate(adapter.features(latest_view(candidate), predicate))
    rp, _ = _rate(adapter.features(latest_view(parent), predicate))
    if rc is None or rp is None:
        return dict(verdict="unchecked", reason="not computable", candidate_rate=rc, parent_rate=rp)
    increase = predicate.predicted_direction == "increase"
    if (increase and rp >= 1.0) or (not increase and rp <= 0.0):
        return dict(verdict="unchecked", reason="parent rate at the bound", candidate_rate=rc, parent_rate=rp)
    moved = rc > rp if increase else rc < rp
    return dict(verdict="pass" if moved else "fail", candidate_rate=rc, parent_rate=rp)


def sample_pairs(candidate: EvalResult, parent: EvalResult, seed_aa: Optional[EvalResult], task_ids: list[str],
                 *, key: str, n_pairs: int, n_controls: int) -> list[dict]:
    """§3.2 fixed rule: largest and smallest paired differences (n/3 each), the rest at random, plus controls.

    Returns accountant records ``{pair, kind, candidate_side, task, d, A, B}``
    in shuffled order; ``public`` strips them to what the reviewer may see.
    Controls are two attempts of the seed on one task (A/A).
    """
    rng = random.Random(hashlib.sha256(key.encode()).hexdigest())
    scored = [t for t in task_ids if t in candidate.per_task_scores and t in parent.per_task_scores
              and candidate.trajectories.get(t) and parent.trajectories.get(t)]
    by_d = sorted(scored, key=lambda t: (candidate.per_task_scores[t] - parent.per_task_scores[t], t))
    edge = n_pairs // 3
    chosen = list(dict.fromkeys(by_d[::-1][:edge] + by_d[:edge]))
    remaining = [t for t in by_d if t not in chosen]
    rng.shuffle(remaining)
    chosen += remaining[:max(0, n_pairs - len(chosen))]
    records = []
    for t in chosen:
        side = rng.choice("AB")
        cand, par = candidate.trajectories[t][-1:], parent.trajectories[t][-1:]
        records.append(dict(kind="candidate", candidate_side=side, task=t,
                            d=candidate.per_task_scores[t] - parent.per_task_scores[t],
                            A=cand if side == "A" else par, B=par if side == "A" else cand))
    if seed_aa is not None and n_controls:
        twice = sorted(t for t in task_ids if len(seed_aa.trajectories.get(t) or ()) >= 2)
        rng.shuffle(twice)
        for t in twice[:n_controls]:
            first, second = seed_aa.trajectories[t][0:1], seed_aa.trajectories[t][1:2]
            records.append(dict(kind="control", candidate_side=None, task=t, d=None, A=first, B=second))
    rng.shuffle(records)
    for i, r in enumerate(records, 1):
        r["pair"] = f"p{i}"
    return records


def public(records: list[dict]) -> list[dict]:
    """What the adapter gets: no side key, no Δ. ``task`` lets it load the rows; it must never render it."""
    return [dict(pair=r["pair"], task=r["task"], A=r["A"], B=r["B"]) for r in records]


def ledger_view(records: list[dict]) -> list[dict]:
    """What the ledger keeps of the pairs (accountant-only: task ids and Δ)."""
    return [dict(pair=r["pair"], kind=r["kind"], candidate_side=r["candidate_side"], task=r["task"], d=r["d"])
            for r in records]


def review(adapter: SystemAdapter, card: Card, diff: Any, candidate_version, records: list[dict], *,
           key: str) -> tuple[Optional[list[dict]], Optional[str]]:
    """Dispatch the reviewer; (verdicts or None, error). A failed review is 'none', never a halt."""
    try:
        return adapter.review(card, diff, public(records), candidate=candidate_version, key=key), None
    except Exception as exc:  # noqa: BLE001 - a reviewer failure only removes evidence
        from ..adapter import EvaluationUnavailable
        if isinstance(exc, EvaluationUnavailable):
            raise
        return None, f"{type(exc).__name__}: {exc}"
