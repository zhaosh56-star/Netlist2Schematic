"""method-v4 §2.3 calibration (v4.1: no k_val rule; safeguard A/A noise): seed A/A runs, derived quantities and the feasibility card.

``measure`` runs (or, on resume, replays) the seed's A/A measurements:
search sample (k = diagnose_k, the diagnosis measurement), minibatch and rest
(subset measurements at calibration_k, unpaired). ``derive`` turns them into
the run's calibrated quantities; ``feasibility`` into the card printed before
the first round. The test set is never touched here (§4.1).
"""
from __future__ import annotations

import math
import statistics
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Optional

from ..adapter import BudgetExhausted, EvaluationCancelled, EvalResult, SystemAdapter, Version
from .config import V4Config
from .decide import attempts_by_task, combine, safeguard_noise


def measure(adapter: SystemAdapter, seed: Version, cfg: V4Config, search: list[str], mb: list[str],
            rest: list[str]) -> tuple[dict[str, EvalResult], dict[str, float]]:
    """Seed A/A on minibatch and rest, and the seed's diagnosis sample (k = diagnose_k), concurrently.

    Returns (results, wall seconds per stage). The diagnosis sample is round 1's diagnosis.
    """
    k = cfg.calibration_k
    jobs = dict(
        search=lambda: adapter.evaluate_search(seed, search, cfg.diagnose_k),
        mb=lambda: adapter.evaluate_subset(seed, None, "mb", mb, k)[0],
        rest=lambda: adapter.evaluate_subset(seed, None, "rest", rest, k)[0],
    )
    walls: dict[str, float] = {}

    def timed(name):
        t0 = time.monotonic()
        try:
            return jobs[name]()
        finally:
            walls[name] = time.monotonic() - t0
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="v4-cal") as pool:
        futures = {name: pool.submit(timed, name) for name in jobs}
        results = {}
        pending = {f: name for name, f in futures.items()}
        try:
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    results[pending.pop(future)] = future.result()
        except BaseException as exc:
            # Wake sibling pollers before joining; a hard interruption keeps durable work replayable.
            if isinstance(exc, (BudgetExhausted, EvaluationCancelled)):
                adapter.cancel_pending()
            else:
                adapter.stop_polling()
            for future in futures.values():
                future.cancel()
            raise
    return results, walls


def _aa(result: EvalResult) -> dict:
    """Rerun noise and task variance from tasks with at least two attempts."""
    per_task = {t: v for t, v in attempts_by_task(result).items() if len(v) >= 2}
    diffs = [v[0] - statistics.mean(v[1:]) for v in per_task.values()]
    means = [sum(v) / len(v) for v in per_task.values()]
    n = len(diffs)
    if n < 2:
        return dict(n=n, rerun_var=None, task_var=None, aa_sd=None, aa_se=None, constant_fraction=None,
                    mean=None)
    # All k attempts contribute; with k=2 this equals mean(d²/2).
    rerun_var = statistics.mean(statistics.variance(v) for v in per_task.values())
    k_mean = statistics.mean(len(v) for v in per_task.values())
    task_var = max(0.0, statistics.variance(means) - rerun_var / k_mean)
    constant = sum(1 for v in per_task.values() if len(set(v)) == 1 and v[0] in (0.0, 1.0))
    aa_sd = statistics.stdev(diffs)
    return dict(n=n, rerun_var=rerun_var, task_var=task_var, aa_sd=aa_sd, aa_se=aa_sd / math.sqrt(n),
                aa_delta=sum(diffs) / n, constant_fraction=constant / n, mean=statistics.mean(means))


def cache_summary(profile: list[dict]) -> Optional[dict]:
    """Sum of an adapter ``timing_profile``: executions, wall seconds, cache hits / misses; None when empty."""
    if not profile:
        return None
    return dict(executions=len(profile), wall_s=max(p["wall_s"] for p in profile),
                cases=sum(p.get("cases", 0) for p in profile),
                cache_hits=sum(p.get("cache_hits", 0) for p in profile),
                cache_misses=sum(p.get("cache_misses", 0) for p in profile),
                lock_waited_s=max(p.get("lock_waited_s", 0.0) for p in profile))


def derive(results: dict[str, EvalResult], walls: dict[str, float], cfg: V4Config, *, safeguards: Optional[dict],
           sizes: dict[str, int], profiles: Optional[dict] = None) -> dict:
    """Calibrated quantities (§2.3 step 3–4). Pure."""
    val = _aa(combine(results["mb"], results["rest"]))
    search = _aa(results["search"])
    if val["rerun_var"] is None:
        raise ValueError("calibration needs at least two validation tasks with two scored attempts")
    tasks = dict(search=sizes["search"], mb=sizes["mb"], rest=sizes["rest"])
    per_task = {s: (walls.get(s) or 0.0) / max(1, tasks[s]) for s in tasks}
    # method-v4.1 §4.2: candidates are always measured once (k=1); no k_val rule.
    return dict(validation=val, search=search,
                rerun_var=val["rerun_var"], se_floor_rule="sqrt(sum_i rerun_var*(1/k_c,i + 1/k_p,i)) / n",
                walls=dict(walls), seconds_per_task=per_task, sizes=dict(sizes), safeguards=safeguards,
                safeguard_noise=safeguard_noise(safeguards),
                safeguard_se_rule="sqrt(sum_i var*(1/k_s,i + 1/k_r,i)) / n per safeguard (ratio: log scale)",
                infrastructure_failure_rate=_infra(safeguards), cache=dict(profiles or {}))


def _cold_warm(cal: dict, stage: dict, sizes: dict) -> tuple[dict, dict, str]:
    """(cold, warm, note) per-stage seconds from the calibration's cache profile."""
    cache = cal.get("cache") or {}
    known = {s: c for s, c in cache.items() if c and (c.get("cache_hits") or c.get("cache_misses"))}
    if not known:
        return dict(stage), dict(stage), "observed calibration walls; no cache profile (cold = warm = observed)"
    walls, n = cal.get("walls") or {}, dict(search=sizes["search"], mb=sizes["mb"], rest=sizes["rest"])
    rate = {s: walls.get(s, 0.0) / max(1, n[s]) for s in known}   # seconds per task, as observed
    warm_rates = [rate[s] for s, c in known.items() if not c.get("cache_misses")]
    cold_rates = [rate[s] for s, c in known.items() if c.get("cache_misses")]
    cold_rate = max(cold_rates) if cold_rates else None
    warm_rate = min(warm_rates) if warm_rates else None
    scale = (warm_rate / cold_rate) if warm_rate and cold_rate else None
    cold, warm = dict(stage), dict(stage)
    for key in ("smoke", "minibatch", "validation_rest"):
        if scale is not None:
            warm[key] = stage[key] * min(1.0, scale) if cold_rates else stage[key]
            cold[key] = stage[key] / min(1.0, scale) if not cold_rates else stage[key]
    if scale is None:
        note = ("calibration was all cold: cold = observed, warm unknown (= observed)" if cold_rates
                else "calibration was all warm: warm = observed, cold unknown (= observed)")
    else:
        note = f"cold/warm from calibration cache profile (warm/cold seconds per task {scale:.2f})"
    return cold, warm, note


def _infra(safeguards: Optional[dict]) -> Optional[float]:
    """Seed infrastructure failure rate (information only, not a safeguard in v4.1)."""
    row = ((safeguards or {}).get("infrastructure") or {}).get("subject") or {}
    total = row.get("total") if isinstance(row, dict) else None
    return row["failed"] / total if total and not row.get("unobserved") else None


def validation_seconds(cal: Optional[dict], cfg: V4Config) -> float:
    """One k=1 measurement of the whole validation set, from the calibration walls (k attempts)."""
    summary = (cal or {}).get("summary")
    if summary:
        return summary["exec_overhead_s"] + (cal or {}).get("validation_size", summary["n_probe"]) * summary["per_task_s"]
    walls = (cal or {}).get("walls") or {}
    return (float(walls.get("mb") or 0.0) + float(walls.get("rest") or 0.0)) / max(1, cfg.calibration_k)


def feasibility(cal: dict, cfg: V4Config) -> dict:
    """§2.3 step 5 feasibility card (advice only, never blocks).

    Stage times come from the calibration's observed walls, scaled to the
    stage's k. Cold / warm: when the adapter reports preparation-cache hits and
    misses (``timing_profile``), the calibration's stages are classified; a
    cold stage's wall is the cold estimate and the warm estimate is the cold
    one minus the preparation share, approximated by the warm stage with the
    most cases per second when one exists. A candidate that changes
    preparation code pays the cold column; any other candidate the warm one.
    Without a profile both columns are the observed values (labelled so).
    """
    sizes, walls, k_cal = cal["sizes"], cal["walls"], cfg.calibration_k
    smoke_s = walls.get("search", 0.0) * min(1.0, cfg.smoke_size / max(1, sizes["search"]))
    stage = dict(agent=cfg.agent_estimate_minutes * 60, smoke=smoke_s,
                 minibatch=walls.get("mb", 0.0) * (1 / k_cal if walls.get("mb") else 0),
                 validation_rest=walls.get("rest", 0.0) / k_cal)
    cold, warm, note = _cold_warm(cal, stage, sizes)
    per_candidate = sum(stage.values())
    budget = cfg.time_budget_hours * 3600
    # Most candidates stop at stage 0/1; the card assumes every candidate runs through (an upper bound on time).
    slots = max(1, cfg.b)
    candidates = int(slots * budget // per_candidate) if per_candidate > 0 else None
    rounds = (candidates // cfg.n_cards) if candidates is not None else None
    aa_sd = cal["validation"]["aa_sd"]
    n_test = sizes.get("test")
    half = 1.96 * aa_sd / math.sqrt(n_test) if n_test and aa_sd is not None else None
    card = dict(stage_seconds=stage, per_candidate_seconds=per_candidate,
                stage_seconds_cold=cold, stage_seconds_warm=warm,
                per_candidate_cold=sum(cold.values()), per_candidate_warm=sum(warm.values()),
                cache_note=note,
                candidates_in_budget=candidates, rounds_in_budget=rounds,
                test_half_width_95=half, test_n=n_test,
                rerun_var=cal["rerun_var"], task_var=cal["validation"]["task_var"],
                constant_fraction=cal["validation"]["constant_fraction"],
                infrastructure_failure_rate=cal.get("infrastructure_failure_rate"), warnings=[])
    if n_test is not None and n_test < 40:
        card["warnings"].append(f"test set has {n_test} tasks (method §2.3 asks for at least 40)")
    if sizes["mb"] + sizes["rest"] < 40:
        card["warnings"].append(f"validation set has {sizes['mb'] + sizes['rest']} tasks (method §2.3: at least 40)")
    mmi = cfg.min_meaningful_improvement
    if mmi is not None and half is not None and half > mmi:
        card["warnings"].append(
            f"test set too small to confirm a cumulative gain of {mmi:g}: 95% half-width ~{half:.3f}. "
            "Add test tasks, add time, or accept a coarser conclusion.")
    return card


def render(card: dict, cal: dict) -> str:
    st = card["stage_seconds"]
    lines = [
        "feasibility (advice only):",
        f"  validation: mb={cal['sizes']['mb']} rest={cal['sizes']['rest']} search={cal['sizes']['search']} "
        f"test={card['test_n'] if card['test_n'] is not None else '-'}",
        f"  candidates k=1; rerun var {card['rerun_var']:.4g}, "
        f"task var {card['task_var']:.4g}; constant tasks {card['constant_fraction']:.0%}",
        "  per candidate (s): " + ", ".join(f"{k} {v:.0f}" for k, v in st.items())
        + f" = {card['per_candidate_seconds']:.0f}",
        f"  cold cache ~{card['per_candidate_cold']:.0f} s, warm ~{card['per_candidate_warm']:.0f} s "
        f"({card['cache_note']})",
        f"  in budget: ~{card['candidates_in_budget']} candidates, ~{card['rounds_in_budget']} rounds (upper bound)",
        f"  infrastructure failure rate (seed): {card['infrastructure_failure_rate']}",
        "  test 95% half-width ~" + (f"{card['test_half_width_95']:.3f}" if card["test_half_width_95"] else "-"),
    ]
    lines += [f"  warning: {w}" for w in card["warnings"]]
    return "\n".join(lines)
