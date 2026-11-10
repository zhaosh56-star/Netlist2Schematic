"""Statistical machinery for v3 paired-difference decisions.

Implements §1 decision statistics, §2.2 A/B screen and confirm decisions,
Ladder-inspired threshold m (§1), sample-size planning (§2.2), and attribution
label logic (§2.2) from method-v3.md.  No external dependencies beyond stdlib.
"""
from __future__ import annotations

import math
import statistics
from typing import Optional


# ---------------------------------------------------------------------------
# Core paired-difference statistics
# ---------------------------------------------------------------------------

def paired_differences(
    scores_cand: list[float],
    scores_parent: list[float],
) -> list[float]:
    """Return per-task d_i = s_cand(i) - s_parent(i).

    Both lists must have the same length and correspond to the same ordered
    task sequence.
    """
    if len(scores_cand) != len(scores_parent):
        raise ValueError(
            f"scores_cand length {len(scores_cand)} != scores_parent length"
            f" {len(scores_parent)}"
        )
    return [c - p for c, p in zip(scores_cand, scores_parent)]


def mean_delta(differences: list[float]) -> float:
    """Return the mean of the paired differences Δ = mean(d_i)."""
    if not differences:
        raise ValueError("differences must not be empty")
    return sum(differences) / len(differences)


def paired_se(
    differences: list[float],
    families: Optional[dict[int, str]] = None,
) -> float:
    """Standard error of mean(d_i), optionally cluster-robust by task family.

    Parameters
    ----------
    differences:
        Ordered list of d_i values.
    families:
        Optional mapping from task index (0-based) to family label.  When
        provided and more than one cluster is present, uses the CR1 cluster-
        robust sandwich estimator matching the noise.py convention:
            SE = sqrt( g/(g-1) * sum_j(score_j^2) ) / n
        where score_j = sum_{i in j}(d_i - delta) and g = number of clusters.
        Falls back to iid SE when g <= 1 or all tasks are in the same cluster.
    """
    n = len(differences)
    if n < 2:
        raise ValueError("need at least 2 differences to estimate SE")
    delta = mean_delta(differences)
    if families is None or len(set(families.values())) <= 1:
        # iid: sample std / sqrt(n)
        sd = statistics.stdev(differences)
        return sd / math.sqrt(n)
    # Build clusters
    groups: dict[str, list[float]] = {}
    for i, d in enumerate(differences):
        fam = families.get(i, str(i))
        groups.setdefault(fam, []).append(d)
    g = len(groups)
    if g <= 1:
        sd = statistics.stdev(differences)
        return sd / math.sqrt(n)
    # CR1 cluster-robust sandwich (matches noise.py paired_means)
    scores = [sum(d - delta for d in grp) for grp in groups.values()]
    return math.sqrt(g / (g - 1) * sum(s * s for s in scores)) / n


# ---------------------------------------------------------------------------
# Ladder-inspired threshold
# ---------------------------------------------------------------------------

def ladder_m(sigma: float, K: int, n_val: int, c: float = 1.0) -> float:
    """Ladder-inspired screening threshold m = c·σ·√(log(2K)/n_val).

    If K<=1 or n_val<=1, returns the floor 0.02 (no meaningful Ladder guarantee).
    σ is the per-task paired-difference SD estimated from the search set.
    K is the pre-registered maximum number of screening submissions.
    """
    if K < 2 or n_val < 2 or sigma <= 0.0:
        return 0.02
    return max(0.02, c * sigma * math.sqrt(math.log(2 * K) / n_val))


# ---------------------------------------------------------------------------
# Decision rules
# ---------------------------------------------------------------------------

def screen_decision(
    delta_val: float,
    se_val: float,
    z_s: float,
    best_score: float,
    candidate_score: float,
    m: float,
    safeguards_ok: bool,
) -> str:
    """§2.2 A screening verdict: 'screen_pass' | 'screen_fail' | 'unmeasurable'.

    screen_pass iff lower_val > 0 AND score_val(candidate) > best + m AND safeguards_ok.
    unmeasurable when safeguards_ok is False (caller determined safeguard breach).
    """
    if not safeguards_ok:
        return 'unmeasurable'
    lower_val = delta_val - z_s * se_val
    if lower_val > 0.0 and candidate_score > best_score + m:
        return 'screen_pass'
    return 'screen_fail'


def confirm_decision(
    delta_fresh: float,
    se_fresh: float,
    z_c: float,
    safeguards_ok: bool,
) -> str:
    """§2.2 B confirmation verdict: 'confirmed' | 'rejected' | 'insufficient' | 'unmeasurable'.

    confirmed  iff lower = Δ - z_c·SE > 0
    rejected   iff upper = Δ + z_c·SE ≤ 0
    insufficient otherwise
    unmeasurable if safeguards violated
    """
    if not safeguards_ok:
        return 'unmeasurable'
    lower = delta_fresh - z_c * se_fresh
    upper = delta_fresh + z_c * se_fresh
    if lower > 0.0:
        return 'confirmed'
    if upper <= 0.0:
        return 'rejected'
    return 'insufficient'


# ---------------------------------------------------------------------------
# Sample-size planning
# ---------------------------------------------------------------------------

def required_n_fresh(
    sigma_d: float,
    mde: float,
    z_c: float = 1.65,
    z_power: float = 0.84,
) -> int:
    """Minimum n_fresh tasks for the given MDE (single-step conservative basis).

    n >= ceil((z_c + z_power)^2 * sigma_d^2 / mde^2).
    sigma_d is the conservative planned SD of per-task paired differences.
    """
    if sigma_d <= 0.0 or mde <= 0.0:
        raise ValueError("sigma_d and mde must be positive")
    return math.ceil((z_c + z_power) ** 2 * sigma_d ** 2 / mde ** 2)


def alpha_warning(n_confirmations: int, alpha: float = 0.05) -> dict:
    """§7 α-accumulation note.

    Returns per_test_alpha and familywise_error = 1-(1-alpha)^n_confirmations.
    n_confirmations=0 is valid (no confirmations run yet): familywise_error=0.
    """
    if n_confirmations < 0:
        raise ValueError("n_confirmations must be >= 0")
    if n_confirmations == 0:
        return dict(
            per_test_alpha=alpha,
            n_confirmations=0,
            familywise_error=0.0,
            bonferroni_alpha=None,
        )
    return dict(
        per_test_alpha=alpha,
        n_confirmations=n_confirmations,
        familywise_error=1.0 - (1.0 - alpha) ** n_confirmations,
        bonferroni_alpha=alpha / n_confirmations,
    )


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

def attribution_label(
    r_p: float,
    r_c: float,
    delta_S: float,
    delta_notS: float,
    predicted_direction: str,
    n_min: int,
    slice_size: int,
    *,
    independent_parent_slice: bool,
) -> str:
    """§2.2 归因: 'supported' | 'refuted' | 'insufficient'.

    Must be called with independent_parent_slice=True (slice determined from
    an independent parent run, not the cached screening run) to avoid the
    systematic false-positive described in §7.
    """
    if not independent_parent_slice:
        raise ValueError(
            "Attribution requires independent_parent_slice=True to avoid "
            "systematic false positives (§7 归因假阳性)"
        )
    if slice_size < n_min:
        return 'insufficient'
    return _attribution_rule(r_p, r_c, delta_S, delta_notS, predicted_direction)


def _attribution_rule(r_p: float, r_c: float, delta_S: float, delta_notS: float,
                      predicted_direction: str) -> str:
    """The §2.2 label rule on a slice that already has at least n_min tasks."""
    moved = (predicted_direction == 'decrease' and r_c < r_p) or \
            (predicted_direction == 'increase' and r_c > r_p)
    if moved and delta_S > 0.0 and delta_S > delta_notS:
        return 'supported'
    if not moved or (moved and delta_S <= 0.0):
        return 'refuted'
    # Global gain mostly outside slice
    if delta_S <= delta_notS:
        return 'refuted'
    return 'insufficient'


def attribution_result(
    r_p: Optional[float],
    r_c: Optional[float],
    delta_S: Optional[float],
    delta_notS: Optional[float],
    predicted_direction: str,
    n_min: int,
    slice_size: int,
    *,
    independent_parent_slice: bool,
    bias_reason: Optional[str] = None,
) -> dict:
    """§2.2 归因 as a record: {'label', 'possibly_biased', 'reason'}.

    Unlike ``attribution_label`` this also serves the case §2.2 allows when
    an independent parent run is not affordable: the slice was chosen on the
    same parent run whose scores enter Δ_S. The label is then computed by the
    same rule but marked ``possibly_biased=True`` ("可能有偏，仅供参考"), and
    *bias_reason* is required so the record says why.

    Not-computable inputs (None r_p / r_c / Δ_S) give 'insufficient'. An empty
    complement (Δ_¬S None) cannot carry the gain, so it compares as -inf.
    """
    if not independent_parent_slice and not bias_reason:
        raise ValueError("a slice chosen on a non-independent parent run needs bias_reason")
    if r_p is None or r_c is None or delta_S is None or slice_size < n_min:
        label = 'insufficient'
    else:
        label = _attribution_rule(r_p, r_c, delta_S,
                                  float('-inf') if delta_notS is None else delta_notS,
                                  predicted_direction)
    return dict(label=label, possibly_biased=not independent_parent_slice,
                reason=None if independent_parent_slice else bias_reason)
