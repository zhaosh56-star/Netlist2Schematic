"""Pure method-v4.1 decision rules: paired Δ with the SE floor, safeguards, tiers, adoption.

No adapter, no I/O. Everything the loop decides is a function of these.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

from ..adapter import EvalResult


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def combine(*results: Optional[EvalResult]) -> EvalResult:
    """Union of results on disjoint task sets (mb ∪ rest = the validation set)."""
    parts = [r for r in results if r is not None]
    scores, trajectories, costs, errors, values, attempts = {}, {}, {}, {}, {}, []
    context: dict = {}
    for r in parts:
        scores.update(r.per_task_scores)
        trajectories.update(r.trajectories)
        costs.update(r.costs)
        errors.update(r.error_classes)
        values.update(r.safeguard_values)
        attempts.extend(r.attempt_scores)
        for k, v in (r.context or {}).items():
            if k == "score_identities":
                context.setdefault(k, []).extend(v)
            elif isinstance(v, dict) and isinstance(context.get(k), dict):
                context[k] = dict(context[k], **v)
            elif k not in context:
                context[k] = v
    return EvalResult(per_task_scores=scores, trajectories=trajectories, costs=costs, error_classes=errors,
                      safeguard_values=values, context=context, attempt_scores=attempts)


def restrict(result: EvalResult, task_ids: Iterable[str]) -> EvalResult:
    """The part of *result* on *task_ids* (scores, trajectories, attempts and the scopes of those tasks)."""
    keep = set(task_ids)
    context = dict(result.context or {})
    if isinstance(context.get("scopes"), dict):
        context["scopes"] = {t: s for t, s in context["scopes"].items() if t in keep}
    return EvalResult(per_task_scores={t: v for t, v in result.per_task_scores.items() if t in keep},
                      trajectories={t: v for t, v in result.trajectories.items() if t in keep},
                      costs={t: v for t, v in result.costs.items() if t in keep},
                      error_classes={t: v for t, v in result.error_classes.items() if t in keep},
                      safeguard_values=dict(result.safeguard_values), context=context,
                      attempt_scores=[{t: v for t, v in a.items() if t in keep} for a in result.attempt_scores or []])


def merge_attempts(*results: Optional[EvalResult]) -> EvalResult:
    """Several measurements of one version on the same tasks: every attempt kept (re-measurement, §4.4).

    ``combine`` unions disjoint task sets; this concatenates the attempts of each task, so
    the per-task mean is over all attempts and ``paired_delta``'s SE floor sees k = 2.
    Tasks without attempt detail fall back to the mean of the per-task scores.
    """
    parts = [r for r in results if r is not None]
    attempts = [dict(a) for r in parts for a in (r.attempt_scores or [])]
    trajectories: dict = {}
    for r in parts:
        for t, v in (r.trajectories or {}).items():
            trajectories.setdefault(t, []).extend(v or [])
    by_task: dict[str, list[float]] = {}
    for a in attempts:
        for t, v in a.items():
            if v is not None:
                by_task.setdefault(t, []).append(float(v))
    scores = {t: sum(v) / len(v) for t, v in by_task.items()}
    for t in {t for r in parts for t in r.per_task_scores}:
        if t not in scores:
            vals = [r.per_task_scores[t] for r in parts if r.per_task_scores.get(t) is not None]
            if vals:
                scores[t] = sum(vals) / len(vals)
    tasks = set(scores) | set(trajectories) | {t for r in parts for t in r.error_classes}
    errors = {t: None if t in scores else next((r.error_classes.get(t) for r in parts if r.error_classes.get(t)),
                                               "unscored") for t in tasks}
    costs = {t: sum(r.costs.get(t, 0.0) for r in parts) for t in tasks}
    context: dict = {}
    for r in parts:
        for k, v in (r.context or {}).items():
            if k == "score_identities":
                context.setdefault(k, []).extend(v)
            elif isinstance(v, dict) and isinstance(context.get(k), dict):
                context[k] = dict(context[k], **v)
            elif k not in context:
                context[k] = v
    values: dict = {}
    for r in parts:
        values.update(r.safeguard_values or {})
    return EvalResult(per_task_scores=scores, trajectories=trajectories, costs=costs, error_classes=errors,
                      safeguard_values=values, context=context, attempt_scores=attempts)


def attempts_by_task(result: EvalResult) -> dict[str, list[float]]:
    """task -> its per-attempt scores (from ``attempt_scores``)."""
    out: dict[str, list[float]] = {}
    for attempt in result.attempt_scores or []:
        for task, value in attempt.items():
            if value is not None:
                out.setdefault(task, []).append(float(value))
    return out


# ---------------------------------------------------------------------------
# Paired Δ and SE (§1, §2.3: SE = max(own SE, A/A floor))
# ---------------------------------------------------------------------------

def own_score(result: EvalResult, task: str) -> Optional[float]:
    """Return only an observed scorer value; a missing judge result stays missing.

    Answer production is measured separately by the safeguard. An admitted output
    (including no_answer/error) does not establish the primary metric's value.
    """
    return result.per_task_scores.get(task)


def paired_delta(candidate: EvalResult, reference: EvalResult, task_ids: Iterable[str], *,
                 rerun_var: Optional[float] = None, k_candidate: int = 1, k_reference: int = 1,
                 families: Optional[dict] = None) -> dict:
    """Δ = mean(s_c − s_p) over tasks scored on both sides; SE floored by the A/A estimate.

    Own SE: clustered by family (CR1, as v2 noise.paired_means) when *families*
    gives two or more clusters among the paired tasks, else iid. The floor is
    the SE a zero effect would have from rerun noise alone:
    sqrt(Σ_i rerun_var·(1/k_c,i + 1/k_p,i)) / n, with k_·,i the attempts actually
    averaged for task i (nominal k when attempts are not reported).

    Missing primary scores are excluded from pairing, including admitted outputs
    without a judge result. Answer production is a separate safeguard.
    """
    identities = [identity for result in (candidate, reference)
                  for identity in (result.context or {}).get("score_identities", [])]
    if identities:
        from .results import compatible
        compatible(identities)
    ids = list(task_ids)
    ca, ra = attempts_by_task(candidate), attempts_by_task(reference)
    diffs, floor_terms, paired_ids = [], [], []
    own = 0
    for t in ids:
        x, y = own_score(candidate, t), own_score(reference, t)
        if x is None or y is None:
            continue
        own += (t not in candidate.per_task_scores) + (t not in reference.per_task_scores)
        diffs.append(x - y)
        paired_ids.append(t)
        if rerun_var is not None:
            kc, kr = len(ca.get(t, ())) or k_candidate, len(ra.get(t, ())) or k_reference
            floor_terms.append(rerun_var * (1 / kc + 1 / kr))
    n = len(diffs)
    out = dict(n=n, dropped=len(ids) - n, own_failures=own, delta=None, se_own=None, se_floor=None, se=None,
               se_method=None)
    if n == 0:
        return out
    delta = sum(diffs) / n
    groups: dict = {}
    for t, d in zip(paired_ids, diffs):
        groups.setdefault((families or {}).get(t, t) if families else t, []).append(d)
    g = len(groups)
    if families and 1 < g < n:
        scores = [sum(d - delta for d in grp) for grp in groups.values()]
        se_own, out["se_method"] = math.sqrt(g / (g - 1) * sum(x * x for x in scores)) / n, "cluster_cr1"
    else:
        se_own = math.sqrt(sum((d - delta) ** 2 for d in diffs) / (n - 1)) / math.sqrt(n) if n > 1 else None
        out["se_method"] = "iid"
    se_floor = math.sqrt(sum(floor_terms)) / n if floor_terms else None
    known = [s for s in (se_own, se_floor) if s is not None]
    out.update(delta=delta, se_own=se_own, se_floor=se_floor, se=max(known) if known else None)
    return out


# ---------------------------------------------------------------------------
# Safeguards (method-v4.1 §4.5: noise-aware, never blocking when unobserved)
# ---------------------------------------------------------------------------

SAFEGUARD_KINDS = ("ratio", "proportion")
MIN_CHANGED_TASKS = 2          # a proportion safeguard needs a change on at least this many tasks


def _finite(v) -> bool:
    return type(v) in (int, float) and math.isfinite(v)


def _values(values, log: bool) -> list[float]:
    out = []
    for v in values or ():
        if not _finite(v):
            continue
        if log:
            if v > 0:
                out.append(math.log(v))
        else:
            out.append(float(v))
    return out


def paired_safeguard_means(detail: dict) -> tuple[dict, Optional[float], Optional[float]]:
    """Equal task weights after pairing, regardless of each task's attempt count.

    Retain raw attempts on the shared observable tasks for the A/A SE estimate.
    """
    paired = {}
    means = {"subject": [], "reference": []}
    for task, row in detail.items():
        subject, reference = _values(row.get("subject"), False), _values(row.get("reference"), False)
        if not subject or not reference:
            continue
        paired[task] = dict(subject=subject, reference=reference)
        means["subject"].append(sum(subject) / len(subject))
        means["reference"].append(sum(reference) / len(reference))
    def mean(side):
        values = means[side]
        return sum(values) / len(values) if values else None
    return paired, mean("subject"), mean("reference")


def safeguard_noise(observed: Optional[dict]) -> dict:
    """Per-attempt A/A variance of each safeguard (calibration; the analogue of ``rerun_var``).

    *observed*: an adapter observation of the seed against itself, whose ``tasks``
    detail holds per task the per-attempt values of the subject side. The variance
    is the mean within-task sample variance over tasks with at least two attempts;
    ratio safeguards on the log scale.
    """
    out = {}
    for key, o in (observed or {}).items():
        if not isinstance(o, dict) or o.get("kind") not in SAFEGUARD_KINDS:
            continue
        log = o["kind"] == "ratio"
        terms = []
        for row in (o.get("tasks") or {}).values():
            vals = _values((row or {}).get("subject"), log)
            if len(vals) >= 2:
                mean = sum(vals) / len(vals)
                terms.append(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1))
        out[key] = dict(var=sum(terms) / len(terms) if terms else None, tasks=len(terms),
                        scale="log" if log else "linear")
    return out


def safeguard_se(tasks: Optional[dict], var: float, *, log: bool) -> Optional[float]:
    """SE of a safeguard difference from rerun noise alone: sqrt(Σ_i var·(1/k_s,i + 1/k_r,i)) / n.

    Same form as the SE floor of ``paired_delta``; k_·,i are the attempts actually observed.
    """
    terms = []
    for row in (tasks or {}).values():
        ks, kr = len(_values((row or {}).get("subject"), log)), len(_values((row or {}).get("reference"), log))
        if ks and kr:
            terms.append(var * (1 / ks + 1 / kr))
    return math.sqrt(sum(terms)) / len(terms) if terms else None


def _harmful_tasks(tasks: Optional[dict], higher_is_better: bool) -> Optional[int]:
    if not tasks:
        return None
    count = 0
    for row in tasks.values():
        s, r = _values((row or {}).get("subject"), False), _values((row or {}).get("reference"), False)
        if not s or not r:
            continue
        ms, mr = sum(s) / len(s), sum(r) / len(r)
        count += (ms < mr) if higher_is_better else (ms > mr)
    return count


def safeguard_verdict(observed: Optional[dict], tolerances: dict, *, noise: Optional[dict] = None,
                      z: float = 2.0) -> dict:
    """Breached iff harm > tolerance + z·SE_metric (§4.5). Only the configured *tolerances* keys are judged.

    observed: key -> {kind ratio|proportion, direction, subject, reference, tasks?}. Ratio
    safeguards (subject = candidate/reference ratio, reference = 1) are judged on the log
    scale: harm = log(s/r), limit log(1 + tolerance) + z·SE_log. Proportion safeguards:
    harm = r − s (higher_is_better) and the change must involve >= 2 tasks. SE_metric is
    ``safeguard_se`` with the calibration's per-attempt variance (*noise*); without one it
    is 0 and the row says so (``se_source='none'``). An unobserved safeguard is flagged and
    never blocks (``ok`` only reflects breaches).
    """
    rows, breached, unobserved = [], [], []
    for key in sorted(tolerances):
        o = (observed or {}).get(key) or {}
        tol = tolerances[key]
        kind, s, r = o.get("kind"), o.get("subject"), o.get("reference")
        higher = o.get("direction") == "higher_is_better"
        row = dict(key=key, kind=kind, direction=o.get("direction"), subject=s, reference=r, tolerance=tol,
                   harm=None, se=None, se_source=None, threshold=None, changed_tasks=None, breached=None)
        known = kind in SAFEGUARD_KINDS and _finite(s) and _finite(r) and (kind != "ratio" or (s > 0 and r > 0))
        if not known:
            unobserved.append(key)
            rows.append(row)
            continue
        var = ((noise or {}).get(key) or {}).get("var")
        se = safeguard_se(o.get("tasks"), var, log=kind == "ratio") if var is not None else None
        source = "calibration" if se is not None else "none"
        se = se if se is not None else 0.0
        if kind == "ratio":
            harm = math.log(r / s) if higher else math.log(s / r)
            threshold = math.log1p(tol) + z * se
        else:
            harm = (r - s) if higher else (s - r)
            threshold = tol + z * se
        over = harm > threshold
        changed = None
        if kind == "proportion":
            changed = _harmful_tasks(o.get("tasks"), higher)
            if changed is not None:
                over = over and changed >= MIN_CHANGED_TASKS
        row.update(harm=harm, se=se, se_source=source, threshold=threshold, changed_tasks=changed, breached=over)
        rows.append(row)
        if over:
            breached.append(key)
    return dict(ok=not breached, breached=breached, unobserved=unobserved, rows=rows, z=z)


# ---------------------------------------------------------------------------
# Mechanism tier (§4.3): the reviewer can only lower the threshold, never veto
# ---------------------------------------------------------------------------

TIERS = ("confirmed", "unconfirmed")
NEEDS_HUMAN_REVIEW = "needs_human_review"


def reviewer_conclusion(verdicts: Optional[list[dict]], pairs: list[dict], *, min_effect: int,
                        max_control_effect: int) -> dict:
    """'effective' / 'suspicious' / 'none' from per-pair verdicts and the accountant's pair key.

    *pairs* are the accountant's records (``pair``, ``kind`` candidate|control,
    ``candidate_side``). A candidate pair counts as "took effect" only when the
    reviewer names the candidate's side; a control pair counts when the reviewer
    claims any effect. More than ``max_control_effect`` such controls make the
    review untrusted (conclusion 'none'). Gaming with a note on any candidate pair
    makes it 'suspicious' (``tier`` turns that into a flag, never a veto).
    """
    if verdicts is None:
        return dict(conclusion="none", trusted=None, reason="no reviewer")
    by_pair = {v.get("pair"): v for v in verdicts if isinstance(v, dict)}
    missing = [p["pair"] for p in pairs if p["pair"] not in by_pair]
    controls = [p for p in pairs if p["kind"] == "control"]
    candidates = [p for p in pairs if p["kind"] == "candidate"]
    control_effects = sum(bool(by_pair.get(p["pair"], {}).get("effect")) for p in controls)
    effects = sum(bool(by_pair.get(p["pair"], {}).get("effect"))
                  and by_pair[p["pair"]].get("changed_side") == p["candidate_side"] for p in candidates)
    wrong_side = sum(bool(by_pair.get(p["pair"], {}).get("effect"))
                     and by_pair[p["pair"]].get("changed_side") not in (None, p["candidate_side"]) for p in candidates)
    gaming = [p["pair"] for p in candidates if by_pair.get(p["pair"], {}).get("gaming")
              and str(by_pair[p["pair"]].get("note") or "").strip()]
    side_effects = sum(bool(by_pair.get(p["pair"], {}).get("side_effect")) for p in candidates)
    base = dict(effects=effects, wrong_side=wrong_side, control_effects=control_effects, gaming=gaming,
                side_effects=side_effects, missing=missing)
    if missing and len(missing) == len(pairs):
        return dict(base, conclusion="none", trusted=None, reason="no verdicts")
    if control_effects > max_control_effect:
        return dict(base, conclusion="none", trusted=False, reason="control pairs judged changed")
    if gaming:
        return dict(base, conclusion="suspicious", trusted=True)
    if effects >= min_effect:
        return dict(base, conclusion="effective", trusted=True)
    return dict(base, conclusion="none", trusted=True)


def tier(mechanical: Optional[str], reviewer: str) -> tuple[str, list[str]]:
    """(tier, flags). Reported gaming: unconfirmed (no relaxation, even when the mechanical
    check changed) and the flag ``needs_human_review``; never a veto. Otherwise confirmed
    when the mechanical check changed or the reviewer found the change effective."""
    if reviewer == "suspicious":
        return "unconfirmed", [NEEDS_HUMAN_REVIEW]
    if mechanical == "changed" or reviewer == "effective":
        return "confirmed", []
    return "unconfirmed", []


# ---------------------------------------------------------------------------
# Adoption (§4.4)
# ---------------------------------------------------------------------------

def qualification(delta: Optional[float], se: Optional[float], mechanism_tier: Optional[str],
                  safeguards: dict, *, c: float, c_mech: float) -> dict:
    """Δ > 0, Δ >= tier coefficient·SE, no safeguard breached. Unobserved safeguards are a flag only."""
    coefficient = c_mech if mechanism_tier == "confirmed" else c
    threshold = coefficient * se if se is not None else None
    reasons = []
    if delta is None or se is None:
        reasons.append("not_measurable")
    elif delta <= 0:
        reasons.append("not_positive")
    elif delta < threshold:
        reasons.append("below_threshold")
    if safeguards.get("breached"):
        reasons.append("safeguard_breached")
    flags = ["safeguard_unobserved"] if safeguards.get("unobserved") else []
    return dict(qualified=not reasons, coefficient=coefficient, threshold=threshold, reasons=reasons, flags=flags)


def pick_best(records: list[dict], tie_fraction: float) -> Optional[dict]:
    """Largest Δ_val; among those within tie_fraction·SE of it, the smallest diff (then card order)."""
    if not records:
        return None
    top = max(records, key=lambda r: r["delta_val"])
    close = [r for r in records if top["delta_val"] - r["delta_val"] < tie_fraction * (top["se_val"] or 0)
             or r is top]
    return min(close, key=lambda r: (r.get("diff_lines") if r.get("diff_lines") is not None else math.inf,
                                     -r["delta_val"], r["card_index"]))


def minibatch_split(validation: list[str], fraction: float, minimum: int, *, seed: str,
                    families: Optional[dict[str, str]] = None) -> tuple[list[str], list[str]]:
    """Fixed minibatch (§3.2; v4 first implementation: one per run) and the rest of the validation set.

    Proportional allocation over families when given: tasks ordered by
    (rank within their family / family size, hash), the first n taken; else by
    hash only. n = max(minimum, ceil(fraction·N)), at most N − 1.
    """
    import hashlib
    ids = sorted(set(validation))
    if len(ids) < 2:
        raise ValueError("the validation set needs at least 2 tasks for a minibatch and a rest")
    n = min(len(ids) - 1, max(minimum, math.ceil(fraction * len(ids))))
    h = lambda t: hashlib.sha256(f"{seed}:{t}".encode()).hexdigest()  # noqa: E731
    fam = families or {}
    groups: dict[str, list[str]] = {}
    for t in ids:
        groups.setdefault(fam.get(t, t if not fam else "_"), []).append(t)
    rank = {}
    for members in groups.values():
        members.sort(key=h)
        for i, t in enumerate(members):
            rank[t] = (i + 0.5) / len(members)
    ordered = sorted(ids, key=lambda t: (rank[t], h(t))) if fam else sorted(ids, key=h)
    mb = sorted(ordered[:n])
    return mb, sorted(set(ids) - set(mb))
