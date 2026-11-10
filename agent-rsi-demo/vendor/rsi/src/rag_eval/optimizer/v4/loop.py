"""Method-v4.1 coordinator: calibration, pipelined rounds, re-measurement before adoption, ablation.

Workers in stages.py receive snapshots and return records. Only this coordinator
updates versions, persistent stage records and the adopted pointer. Implementation,
measurement and review pools are independent. Diagnostics and next-round analysis
are speculative and consumed only under the matching version/round identity.

Stopping (method-v4.1 §4.6): only the time budget, the spend budget
(``budget_exhausted``) and consecutive run failures stop a run. When the remaining
time cannot fit one more card plus one re-measurement, no new card starts; in-flight
cards finish and the round is decided. An analysis that yields no card counts as a
run failure, so a stuck analyst cannot loop until the budget ends. SIGTERM (CLI)
raises ``RunStopRequested``: remote work is cancelled and the run halts ``stopped``.
The adapter owns deadline checks and actual execution admission; shutdown wakes
pollers before joining and publishes completed materializations for recovery.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import logging
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .. import ledger_io
from ..adapter import AgentRequestFailed, BudgetExhausted, EvalResult, EvaluationUnavailable, SystemAdapter, Version
from ..loop import select_analysis_cases
from . import agent_context, calibrate, decide, stages
from .config import V4Config, digest

log = logging.getLogger(__name__)

SCHEMA = "v4-state-4"
STATE_FILE, LEDGER_FILE = "state.json", "ledger.json"
FAILURE_HALTS = ("process_failure", "budget_exhausted")
STOPPED = "stopped"
TERMINAL = frozenset({"measured", "minibatch_fail", "smoke_fail", "run_failed", "error", "preflight_skipped"})


class RunStopRequested(BaseException):
    """Operator stop (SIGTERM): cancel remote work and halt ``stopped``.

    A BaseException, like KeyboardInterrupt, so that no ``except Exception`` swallows it.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mean(values) -> Optional[float]:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


@dataclass(frozen=True)
class Snapshot:
    """Immutable inputs of one worker."""
    cfg: V4Config
    parent: Version
    parent_val: EvalResult          # the parent's cached results on the whole validation set
    smoke: tuple
    mb: tuple
    rest: tuple
    rerun_var: float
    seed_aa: Optional[EvalResult]
    families: dict
    safeguard_noise: dict           # calibration: per-attempt A/A variance per safeguard


# ---------------------------------------------------------------------------
# Main-thread loop state
# ---------------------------------------------------------------------------

class V4Run:
    def __init__(self, adapter: SystemAdapter, seed: Version, cfg: V4Config, *, search: list[str],
                 validation: list[str], test_size: Optional[int] = None, families: Optional[dict] = None,
                 state_dir: Optional[str | Path] = None, key_generation: Optional[int] = None,
                 clock=time.monotonic, calibration_bundle: Optional[dict] = None, frozen_config: Optional[dict] = None) -> None:
        self.adapter, self.seed, self.cfg = adapter, seed, cfg
        self.calibration_bundle = calibration_bundle
        self.frozen_config = frozen_config
        self.forecast = None
        observable = getattr(adapter, "observable_safeguards", None)
        if callable(observable):
            missing = sorted(set(cfg.safeguards) - set(observable()))
            if missing:
                raise ValueError(f"the adapter cannot observe the configured safeguard(s) {missing}")
        self.clock = clock
        self.state_dir = Path(state_dir) if state_dir is not None else None
        validation = sorted(set(validation))
        if families is None:
            families = adapter.task_families(validation)
        self.families = {t: f for t, f in (families or {}).items() if t in set(validation)}
        smoke = sorted(search, key=lambda c: (hashlib.sha256(c.encode()).hexdigest(), c))[:cfg.smoke_size]
        self.partitions = dict(search=sorted(search), validation=validation, smoke=smoke, test_size=test_size,
                               families=len(set(self.families.values())) or None)
        mb, rest = self.split_for(seed.content_hash)
        self.partitions.update(seed_minibatch=mb, seed_rest=rest)
        self.versions: dict[str, Version] = {seed.content_hash: seed}
        self.lineage: list[dict] = [dict(version=seed.content_hash, parent=None, round=0, kind="seed",
                                         components=[])]
        self.calibration: Optional[dict] = None
        self.feasibility: Optional[dict] = None
        self.rounds: list[dict] = []
        self.current: Optional[dict] = None
        self.history: list[dict] = []
        self.analyst_shown: dict[str, list[str]] = {}
        self.no_adoption = 0              # analyst context only (consecutive rounds without adoption); never a stop
        self.run_failures = 0
        self.failure_detail: Optional[str] = None
        self.failure_kind: Optional[str] = None
        self.stop_reason: Optional[str] = None
        self.halt_reason: Optional[str] = None
        self.ablation: Optional[dict] = None
        self.cumulative: Optional[dict] = None
        self.elapsed_before = 0.0
        self.sessions: list[dict] = []
        self.key_generation = 0
        self.key_generation_log: list[dict] = []
        self.started_at = _now()
        # In memory only (replayed on resume): calibration results, validation caches, prefetch.
        self.cal_results: Optional[dict] = None
        self._val_cache: dict[tuple, EvalResult] = {}
        self._diagnostics: dict[str, EvalResult] = {}
        self._prefetch: dict[str, Future] = {}
        self._prefetch_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="v4-prefetch")
        self._analysis_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="v4-analysis")
        self._analyses: dict[tuple, Future] = {}
        self._pools = {name: ThreadPoolExecutor(max_workers=cfg.b, thread_name_prefix=f"v4-{name}")
                       for name in ("implementation", "measurement", "review")}
        self._jobs: dict[Future, dict] = {}
        self._results: dict[str, EvalResult] = {}
        self._session_start = clock()
        resumed = self.state_dir is not None and (self.state_dir / STATE_FILE).exists()
        if resumed:
            self._restore(ledger_io.read_json(self.state_dir / STATE_FILE))
        self._apply_key_generation(key_generation, resumed)

    # -- partitions ----------------------------------------------------------

    def split_for(self, parent_hash: str) -> tuple[list[str], list[str]]:
        """(minibatch, rest) of one parent: fixed rule, seeded by the parent's hash (§4.2).

        The validation measurement is always minibatch ∪ rest, so a minibatch stage (when
        enabled) is reused by stage 2.
        """
        return decide.minibatch_split(self.partitions["validation"], self.cfg.minibatch_fraction,
                                      self.cfg.minibatch_min, seed=f"v4-minibatch:{parent_hash}",
                                      families=self.families or None)

    # -- persistence --------------------------------------------------------

    def elapsed(self) -> float:
        return self.elapsed_before + (self.clock() - self._session_start)

    def _state(self) -> dict:
        return dict(schema_version=SCHEMA, config_sha256=digest(self.cfg), seed=self.seed.content_hash,
                    partitions=self.partitions, family_of=self.families, calibration=self.calibration,
                    feasibility=self.feasibility, forecast=self.forecast,
                    versions={h: ledger_io.version_metadata(v) for h, v in self.versions.items()},
                    lineage=self.lineage, rounds=self.rounds, current_round=self.current, history=self.history,
                    analyst_shown=self.analyst_shown, no_adoption=self.no_adoption, run_failures=self.run_failures,
                    failure_detail=self.failure_detail, failure_kind=self.failure_kind,
                    stop_reason=self.stop_reason, ablation=self.ablation,
                    cumulative=self.cumulative, halt_reason=self.halt_reason,
                    elapsed_s=self.elapsed(), sessions=self.sessions, key_generation=self.key_generation,
                    key_generation_log=self.key_generation_log, started_at=self.started_at, updated_at=_now())

    def save(self) -> None:
        if self.state_dir is not None:
            ledger_io.atomic_write_json(self.state_dir / STATE_FILE, self._state())

    def _restore(self, st: dict) -> None:
        if st.get("schema_version") != SCHEMA:
            raise ValueError(f"state.json schema {st.get('schema_version')!r} is not {SCHEMA} "
                             "(older v4 runs are not migrated; start a new run directory)")
        if st.get("config_sha256") != digest(self.cfg):
            raise ValueError("the v4 config differs from the one this run started with (pre-registered, read-only)")
        if st.get("seed") != self.seed.content_hash:
            raise ValueError(f"seed {self.seed.content_hash!r} differs from the run's seed {st.get('seed')!r}")
        recorded = dict(st["partitions"])
        same = ("search", "validation", "smoke", "seed_minibatch", "seed_rest")
        if any(recorded.get(k) != self.partitions.get(k) for k in same) or st.get("family_of") != self.families:
            raise ValueError("search/validation/smoke ids or task families differ from the ones this run started with")
        self.partitions = recorded
        self.versions = {h: Version(content_hash=h, metadata=m) for h, m in st["versions"].items()}
        for name in ("calibration", "feasibility", "forecast", "lineage", "rounds", "history", "analyst_shown", "sessions",
                     "key_generation_log", "started_at", "stop_reason", "ablation", "cumulative", "failure_detail", "failure_kind"):
            setattr(self, name, st.get(name, getattr(self, name)))
        self.current = st.get("current_round")
        self.no_adoption, self.run_failures = st.get("no_adoption", 0), st.get("run_failures", 0)
        self.key_generation = st.get("key_generation", 0)
        self.elapsed_before = st.get("elapsed_s", 0.0)
        self.halt_reason = st.get("halt_reason")

    def _apply_key_generation(self, requested: Optional[int], resumed: bool) -> None:
        if requested is None:
            requested = self.key_generation if resumed else self.adapter.key_generation
        if requested < self.key_generation:
            raise ValueError("key_generation only grows")
        if requested != self.key_generation or not self.key_generation_log:
            if resumed and requested > self.key_generation:
                self.run_failures = 0
            self.key_generation_log.append(dict(generation=requested, at=_now(), rounds=len(self.rounds)))
        self.key_generation = requested
        self.adapter.set_key_generation(requested)

    def write_ledger(self) -> None:
        if self.state_dir is None:
            return
        ledger_io.atomic_write_json(self.state_dir / LEDGER_FILE, self.ledger())

    def _all_records(self) -> list[dict]:
        rounds = self.rounds + ([self.current] if self.current else [])
        return [r for rd in rounds for r in (rd.get("records") or {}).values()]

    def ledger(self) -> dict:
        rounds = self.rounds + ([self.current] if self.current else [])
        cand = self._all_records()
        sums = {k: sum((r.get("timings") or {}).get(k, 0.0) for r in cand)
                for k in ("agent_s", "smoke_s", "minibatch_s", "validation_s", "mechanism_s", "safeguards_s",
                          "remeasure_s")}
        stage_counts: dict = {}
        for r in cand:
            stage_counts[r.get("stage")] = stage_counts.get(r.get("stage"), 0) + 1
        by_cache: dict = {}
        for r in cand:
            for stage, key in (("minibatch_s", "cache_mb"), ("validation_s", "cache_rest")):
                if stage not in (r.get("timings") or {}):
                    continue
                c = r.get(key) or {}
                cls = "cold" if c.get("cache_misses") else "warm" if c.get("cache_hits") else "unknown"
                row = by_cache.setdefault(stage, {}).setdefault(cls, dict(n=0, total_s=0.0))
                row["n"] += 1
                row["total_s"] += r["timings"][stage]
        for stage in by_cache.values():
            for row in stage.values():
                row["mean_s"] = row["total_s"] / row["n"]
        remeasures = [dict(round=rd["round"], card_index=r["card_index"], passed=r["remeasure"].get("passed"),
                           status=r["remeasure"].get("status"))
                      for rd in rounds for r in (rd.get("records") or {}).values() if r.get("remeasure")]
        failures: dict = {}
        for r in cand:
            if r.get("failure_kind"):
                failures[r["failure_kind"]] = failures.get(r["failure_kind"], 0) + 1
        flagged = [dict(round=rd["round"], card_index=r["card_index"], flags=r["flags"])
                   for rd in rounds for r in (rd.get("records") or {}).values() if r.get("flags")]
        return dict(kind="method-v4-ledger", method_version="v4.1", config=dataclasses.asdict(self.cfg),
                    config_sha256=digest(self.cfg),
                    variants=self.cfg.variants(), seed=self.seed.content_hash,
                    partitions={k: (len(v) if isinstance(v, list) else v) for k, v in self.partitions.items()},
                    deviations=[d for d in DEVIATIONS if not (self.calibration_bundle and ("calibration still runs" in d or "SN keeps v2" in d))],
                    calibration=self.calibration, feasibility=self.feasibility, forecast=self.forecast,
                    frozen_config=self.frozen_config,
                    rounds=rounds, lineage=self.lineage, stop_reason=self.stop_reason, ablation=self.ablation,
                    halt_reason=self.halt_reason, failure_detail=self.failure_detail, failure_kind=self.failure_kind,
                    cumulative=self.cumulative,
                    remeasures=remeasures, implementation_failures=failures, flagged=flagged,
                    card_estimate=self._card_estimate() if self.calibration else None,
                    time_account=dict(total_wall_s=self.elapsed(),
                                      calibration_s=(self.calibration or {}).get("wall_s"),
                                      rounds=[dict(round=rd["round"], wall_s=rd.get("wall_s"),
                                                   analysis_s=rd.get("analysis_s"), diagnosis_s=rd.get("diagnosis_s"),
                                                   decision_s=rd.get("decision_s"))
                                              for rd in rounds],
                                      candidate_sums_s=sums, stage_seconds_by_cache=by_cache,
                                      ablation_s=(self.ablation or {}).get("wall_s")),
                    stage_counts=stage_counts, key_generation_log=self.key_generation_log, sessions=self.sessions)

    # -- helpers ------------------------------------------------------------

    def pointer(self) -> Version:
        return self.versions[self.lineage[-1]["version"]]

    def history_for(self, parent: Version) -> list[dict]:
        """History for the analyst: all hypotheses tried in this run (§4.1), including earlier parents."""
        out = []
        for e in self.history:
            row = {k: v for k, v in e.items() if k != "parent"}
            # Mark entries from earlier parents so the analyst knows this parent may already contain that change.
            if e["parent"] != parent.content_hash:
                row["earlier_parent"] = True
            out.append(row)
        for node in self.lineage:
            if node.get("parent") is not None:
                rd = next((r for r in self.rounds + ([self.current] if self.current else [])
                           if r["round"] == node["round"]), None)
                if rd is not None:
                    for index in node.get("card_indices", []):
                        card = rd["cards"][index]
                        out.append(dict(label="current_change", title=card["failure_mode_desc"],
                                        change=card["change_hypothesis"], version=node["version"],
                                        direction=card.get("direction")))
            if node["version"] == parent.content_hash:
                break
        return out

    def _node(self, h: str) -> Optional[dict]:
        return next((n for n in self.lineage if n["version"] == h), None)

    def measure_val(self, version: Version, split_of: str) -> EvalResult:
        """*version* on the validation set split as *split_of*'s minibatch + rest, k=1 each; replayable."""
        key = (version.content_hash, split_of)
        if key not in self._val_cache:
            mb, rest = self.split_for(split_of)
            self._val_cache[key] = decide.combine(self.adapter.evaluate_subset(version, None, "mb", mb, 1)[0],
                                                  self.adapter.evaluate_subset(version, None, "rest", rest, 1)[0])
        return self._val_cache[key]

    def remeasured_val(self, version: Version, split_of: str) -> EvalResult:
        """The first measurement merged with one re-measurement (two attempts per task); replayable.

        ``remeasure_subset`` uses its own request key and returns every attempt of the
        version on that subset (first + re-measurement); mb and rest run concurrently.
        """
        key = (version.content_hash, split_of, "remeasure")
        if key not in self._val_cache:
            self.measure_val(version, split_of)          # the first measurement must exist (replay)
            mb, rest = self.split_for(split_of)
            remeasure = getattr(self.adapter, "remeasure_subset", None)
            if not callable(remeasure):     # adapters without the tagged request: k=2 on the same subset
                def remeasure(v, subset, ids):
                    return self.adapter.evaluate_subset(v, None, subset, ids, 2)[0]
            futures = [self._pools["measurement"].submit(remeasure, version, subset, ids)
                       for subset, ids in (("mb", mb), ("rest", rest))]
            merged = decide.combine(*[f.result() for f in futures])
            eligible = [t for t, values in decide.attempts_by_task(merged).items() if len(values) >= 2]
            # Reconstructed caches after resume obey the same two-score contract.
            self._val_cache[key] = decide.restrict(merged, eligible)
        return self._val_cache[key]

    def parent_val(self, version: Version) -> EvalResult:
        """The cached validation results of a lineage node (the seed: its calibration A/A)."""
        h = version.content_hash
        if h == self.seed.content_hash:
            return decide.combine(self.cal_results["mb"], self.cal_results["rest"])
        node = self._node(h)
        if node is None:
            raise KeyError(f"{h!r} is not a lineage node")
        if node.get("remeasured"):
            return self.remeasured_val(version, node["parent"])
        return self.measure_val(version, node["parent"])

    def _snapshot(self, parent: Version) -> Snapshot:
        cal = self.calibration
        mb, rest = self.split_for(parent.content_hash)
        return Snapshot(cfg=self.cfg, parent=parent, parent_val=self.parent_val(parent),
                        smoke=tuple(self.partitions["smoke"]), mb=tuple(mb), rest=tuple(rest),
                        rerun_var=cal["rerun_var"],
                        seed_aa=decide.combine(self.cal_results["mb"], self.cal_results["rest"]),
                        families=dict(self.families), safeguard_noise=dict(cal.get("safeguard_noise") or {}))

    def diagnosis_sample(self, version: Version) -> list[str]:
        search = self.partitions["search"]
        ranked = sorted(search, key=lambda t: hashlib.sha256(f"diag:{version.content_hash}:{t}".encode()).hexdigest())
        return sorted(ranked[:self.cfg.diagnose_size] if self.cfg.diagnose_size else ranked)

    def _diagnose(self, version: Version) -> EvalResult:
        h = version.content_hash
        if h not in self._diagnostics:
            future = self._prefetch.pop(h, None)
            if future is not None:
                try:
                    self._diagnostics[h] = future.result()
                except EvaluationUnavailable:
                    # Speculation is optional, but an actual required diagnosis must succeed.
                    self.adapter.check_runtime()
            if h not in self._diagnostics:
                self._diagnostics[h] = self.adapter.evaluate_search(version, self.diagnosis_sample(version),
                                                                     self.cfg.diagnose_k)
        return self._diagnostics[h]

    def _start_prefetch(self, version: Version) -> bool:
        h = version.content_hash
        if self._out_of_time() or h in self._prefetch or h in self._diagnostics:
            return False
        self._prefetch[h] = self._prefetch_pool.submit(self.adapter.evaluate_search, version,
                                                      self.diagnosis_sample(version), self.cfg.diagnose_k)
        return True

    def _analysis(self, parent, round_id, history, shown, no_adoption):
        start = time.monotonic()
        search_eval = self._diagnostics[parent.content_hash]
        selected, roles = select_analysis_cases(search_eval, self.cfg, shown=shown)
        advice = ((self.frozen_config or {}).get('advisor') or {}).get('failure_modes') if round_id == 1 else None
        try:
            cards = self.adapter.analyse(selected, self.cfg.n_cards, case_roles=roles, parent=parent,
                                         round_id=round_id, history=history, no_adoption_rounds=no_adoption,
                                         **({'calibration_advice': advice} if advice else {}),
                                         case_scores={t: search_eval.per_task_scores[t] for t in selected
                                                      if t in search_eval.per_task_scores})
        except TimeoutError as exc:
            raise AgentRequestFailed('analyst agent timeout; inspect the existing request before resuming') from exc
        out = []
        lookup = getattr(self.adapter, "card_direction", None)
        for card in cards:
            row = ledger_io.card_to_dict(card)
            raw = None
            if callable(lookup):
                try:
                    raw = lookup(card)
                except Exception as exc:  # noqa: BLE001  (a missing tag is inferred from the title)
                    log.warning("card direction unavailable for %r: %s", card.ref, exc)
            row["direction"], row["direction_inferred"] = agent_context.card_direction(raw, card.failure_mode_desc)
            out.append(row)
        return dict(cards=out, roles=roles, wall_s=time.monotonic() - start)

    def _remaining(self) -> float:
        return self.cfg.time_budget_hours * 3600 - self.elapsed()

    def _out_of_time(self) -> bool:
        return self._remaining() <= 0

    def _card_estimate(self) -> dict:
        """Seconds of one more card and one re-measurement (§4.6): observed means, else calibration / config."""
        recs = self._all_records()
        agent = _mean((r.get("timings") or {}).get("agent_s") for r in recs if r.get("candidate"))
        stage_means = {k: _mean((r.get("timings") or {}).get(k) for r in recs)
                       for k in ("smoke_s", "minibatch_s", "validation_s", "mechanism_s")}
        remeasure = _mean((r.get("timings") or {}).get("remeasure_s") for r in recs)
        val_cal = calibrate.validation_seconds(self.calibration, self.cfg)
        validation = stage_means["validation_s"] if stage_means["validation_s"] is not None else val_cal
        card = ((agent if agent is not None else self.cfg.agent_estimate_minutes * 60) + validation
                + (stage_means["smoke_s"] or 0.0) * self.cfg.smoke + (stage_means["minibatch_s"] or 0.0) * self.cfg.minibatch
                + (stage_means["mechanism_s"] or 0.0))
        again = (remeasure if remeasure is not None else validation) if self.cfg.remeasure else 0.0
        return dict(card_s=card, remeasure_s=again, needed_s=card + again,
                    source="observed" if agent is not None else "calibration/config")

    def _no_new_cards(self) -> bool:
        return self._remaining() < self._card_estimate()["needed_s"]

    # -- top level ----------------------------------------------------------

    def run(self) -> str:
        self.adapter.configure_runtime(remaining=self._remaining,
                                       max_concurrent_executions=self.cfg.max_concurrent_executions or 3)
        self.sessions.append(dict(started_at=_now(), key_generation=self.key_generation, rounds=len(self.rounds)))
        if self.halt_reason == "process_failure":
            for rec in (self.current or {}).get("records", {}).values():
                if rec.get("halted"):
                    rec["stage"] = rec.pop("failed_stage", rec["stage"])
                    rec.pop("halted", None)
                    rec.pop("label", None)
            self.run_failures = 0
            self.failure_detail = None
            self.failure_kind = None
        self.halt_reason = None
        self.cumulative = None            # recomputed at this session's halt
        interrupted = stopped = False
        try:
            return self._run()
        except AgentRequestFailed as exc:
            self.run_failures += 1
            self.failure_kind = 'agent_request'
            self.failure_detail = str(exc)[:500]
            log.error('Agent request failed: %s', exc)
            return self._halt('process_failure')
        except RunStopRequested:
            # Operator stop: cancel owned remote work (below) and end the run for good.
            stopped = True
            self.halt_reason = STOPPED
            log.warning("stop requested: cancelling executions and archiving Agents")
            return STOPPED
        except BaseException as exc:
            interrupted = not isinstance(exc, Exception)
            raise
        finally:
            # Wake pollers before joining. A resumable failure or interruption (SIGINT) preserves
            # durable remote work; completed runs, exhausted budgets and stops cancel owned work.
            if not stopped and (interrupted or self.halt_reason == "process_failure"):
                self.adapter.stop_polling()
            else:
                self.adapter.cancel_pending()
            if stopped:
                self._cumulative_check()      # existing measurements only; work was just cancelled
            for pool in [*self._pools.values(), self._prefetch_pool, self._analysis_pool]:
                pool.shutdown(wait=True, cancel_futures=True)
            for future, job in self._jobs.items():
                if future.done() and not future.cancelled() and future.exception() is None:
                    self._complete_job(job, future)
            self._jobs.clear()
            self.sessions[-1]["ended_at"] = _now()
            if stopped:
                self.sessions[-1]["stopped"] = True
            report = getattr(self.adapter, "cancellation_report", None)
            if report:
                self.sessions[-1]["cancellation"] = copy.deepcopy(report)
            self.save()
            self.write_ledger()

    def _run(self) -> str:
        try:
            self._calibrate()
        except EvaluationUnavailable as exc:
            log.error("calibration unavailable: %s", exc)
            return self._halt("time_budget" if self._out_of_time() else _failure(exc))
        while True:
            if self.current is None:
                reason = self.stop_reason or self._stop_reason()
                if reason:
                    self.stop_reason = reason
                    self.save()
                    try:
                        self._ablate()
                    except EvaluationUnavailable as exc:
                        log.error("ablation unavailable: %s", exc)
                        return self._halt("time_budget" if self._out_of_time() else _failure(exc))
                    return self._halt(reason)
                self.current = dict(round=len(self.rounds) + 1, parent=self.pointer().content_hash,
                                    started_at=_now(), cards=None, records={}, decision=None,
                                    started_elapsed=self.elapsed())
                self.save()
            try:
                failure = self._round(self.current)
            except EvaluationUnavailable as exc:
                log.error("evaluation unavailable in round %s: %s", self.current["round"], exc)
                failure = "time_budget" if self._out_of_time() else _failure(exc)
            if failure is not None:
                return self._halt(failure)
            rd = self.current
            rd["finished_at"], rd["wall_s"] = _now(), self.elapsed() - rd["started_elapsed"]
            self.rounds.append(rd)
            if self.calibration_bundle and self.frozen_config:
                from .calibration_session import forecast
                self.forecast = forecast(self.frozen_config, self.calibration_bundle, self.ledger())
                rd["forecast"] = self.forecast
            self.current = None
            self.save()
            self.write_ledger()

    def _halt(self, reason: str) -> str:
        self.halt_reason = reason
        self._cumulative_check()
        self.save()
        self.write_ledger()
        return reason

    def _stop_reason(self) -> Optional[str]:
        """§4.6: the time budget, including "one more card + one re-measurement does not fit"."""
        if self._out_of_time():
            return "time_budget"
        if self._no_new_cards():
            return "time_budget"
        return None

    def _cumulative_check(self) -> None:
        """§4.5 cumulative check: the final version against the seed on the safeguards, existing measurements.

        Warns only; never rolls back.
        """
        if self.calibration is None or self.cal_results is None or self.cumulative is not None:
            return
        if len(self.lineage) < 2:
            self.cumulative = dict(status="skipped", reason="nothing adopted")
            return
        try:
            final = self.versions[self.final_version()]
            split_of = (self.ablation or {}).get("split_of") or self.lineage[-1]["parent"]
            observed = self.adapter.observe_safeguards(self._val_of(final, split_of), self.parent_val(self.seed)) or {}
            verdict = decide.safeguard_verdict(observed, self.cfg.safeguards,
                                               noise=self.calibration.get("safeguard_noise"), z=self.cfg.safeguard_z)
            self.cumulative = dict(status="checked", final=final.content_hash, safeguards=verdict,
                                   warning=bool(verdict["breached"]))
            if verdict["breached"]:
                log.warning("cumulative check: the final version breaches %s against the seed (no rollback)",
                            ", ".join(verdict["breached"]))
        except Exception as exc:  # noqa: BLE001  (a warning only; never blocks the halt)
            self.cumulative = dict(status="unavailable", reason=f"{type(exc).__name__}: {exc}"[:300])

    def _calibrate(self) -> None:
        if self.calibration_bundle is not None:
            from .calibration_session import import_baseline, forecast
            self.cal_results = import_baseline(self.adapter, self.seed, self.cfg, self.partitions,
                                               self.calibration_bundle, self.state_dir)
            self._diagnostics[self.seed.content_hash] = self.cal_results["search"]
            if self.calibration is None:
                self.calibration = copy.deepcopy(self.calibration_bundle["calibration"])
                self.calibration["sizes"] = dict(search=len(self.calibration_bundle["diagnosis"]),
                    mb=len(self.partitions["seed_minibatch"]), rest=len(self.partitions["seed_rest"]),
                    test=self.partitions.get("test_size"))
                self.calibration["validation_size"] = len(self.partitions["validation"])
                self.calibration["summary"] = self.calibration_bundle["summary"]
            if self.frozen_config:
                self.forecast = forecast(self.frozen_config, self.calibration_bundle)
            self.save()
            return
        p = self.partitions
        t0 = self.clock()
        self.cal_results, walls = calibrate.measure(self.adapter, self.seed, self.cfg, self.diagnosis_sample(self.seed),
                                                    p["seed_minibatch"], p["seed_rest"])
        self._diagnostics[self.seed.content_hash] = self.cal_results["search"]
        if self.calibration is not None:
            return   # resumed: derived values are part of the run record and never recomputed
        val = decide.combine(self.cal_results["mb"], self.cal_results["rest"])
        observed = self.adapter.observe_safeguards(val, val)
        sizes = dict(search=len(self.diagnosis_sample(self.seed)), mb=len(p["seed_minibatch"]), rest=len(p["seed_rest"]),
                     test=p.get("test_size"), smoke=len(p["smoke"]))
        profiles = {name: stages.cache(self.adapter, r) for name, r in self.cal_results.items()}
        cal = calibrate.derive(self.cal_results, walls, self.cfg, safeguards=observed, sizes=sizes, profiles=profiles)
        cal["wall_s"] = self.clock() - t0
        self.calibration = cal
        self.feasibility = calibrate.feasibility(cal, self.cfg)
        log.info("%s", calibrate.render(self.feasibility, cal))
        self.save()

    # -- one round ----------------------------------------------------------

    def _round(self, rd: dict) -> Optional[str]:
        parent = self.versions[rd["parent"]]
        if rd["cards"] is None:
            t0 = self.clock()
            prefetched = parent.content_hash in self._prefetch or parent.content_hash in self._diagnostics
            self._diagnose(parent)
            rd.update(diagnosis_s=self.clock() - t0, diagnosis_prefetched=prefetched)
            key = (parent.content_hash, rd["round"])
            future = self._analyses.pop(key, None)
            if future is not None:
                try:
                    result = future.result()
                except EvaluationUnavailable:
                    self.adapter.check_runtime()
                    result = None
            else:
                result = None
            if result is None:
                result = self._analysis(parent, rd["round"], self.history_for(parent),
                                        self.analyst_shown.get(parent.content_hash, []), self.no_adoption)
            shown = self.analyst_shown.get(parent.content_hash, [])
            failures = [t for t, role in result["roles"].items() if role == "failure"]
            self.analyst_shown[parent.content_hash] = list(dict.fromkeys(shown + failures))
            mb, rest = self.split_for(parent.content_hash)
            rd.update(cards=result["cards"], analysis_s=result["wall_s"], analysis_prefetched=future is not None,
                      split=dict(minibatch=mb, rest=rest),
                      analysis=dict(failures_shown=len(failures), controls_shown=len(result["roles"]) - len(failures)))
            if not rd["cards"]:
                # §4.6: a stuck analyst is a run failure, not a reason to loop until the budget ends.
                rd["analysis_empty"] = True
                self.run_failures += 1
                self.save()
                if self.run_failures >= self.cfg.max_consecutive_run_failures:
                    self.failure_detail = f"{self.run_failures} consecutive analyses (or other runs) failed; last: no cards"
                    return "process_failure"
            self.save()
        if rd.get("decision") is None:
            failure = self._pipeline(rd, parent)
            if failure:
                return failure
            self._decide(rd, parent)
        return None

    def _next_stage(self, stage: str):
        cfg = self.cfg
        if stage == "built":
            return stages.smoke if cfg.smoke else stages.minibatch if cfg.minibatch else stages.validation
        if stage == "smoke_pass":
            return stages.minibatch if cfg.minibatch else stages.validation
        return stages.validation          # minibatch_pass; validated without its in-memory result

    def _pipeline(self, rd: dict, parent: Version) -> Optional[str]:
        cards = [ledger_io.card_from_dict(c) for c in rd["cards"]]
        records = rd["records"]
        for i, card in enumerate(cards):
            records.setdefault(str(i), dict(card_index=i, uid=f"{rd['round']}:{i}", title=card.failure_mode_desc,
                                            direction=rd["cards"][i].get("direction"),
                                            direction_inferred=rd["cards"][i].get("direction_inferred"),
                                            card_ref=card.ref, parent=parent.content_hash, stage="pending", timings={}))
        snap = self._snapshot(parent)
        failure = None
        prefetched = set(rd.get("prefetched", []))
        merges = rd.setdefault("merges", {})

        def active_uids():
            return {j.get("uid") for j in self._jobs.values()}

        def count(pool):
            return sum(j["pool"] == pool for j in self._jobs.values())

        def submit(rec, pool, fn, *args):
            rec.setdefault("started_at", _now())
            fut = self._pools[pool].submit(fn, self.adapter, *args)
            self._jobs[fut] = dict(uid=rec["uid"], record=rec, stage=rec["stage"], pool=pool, kind=fn.__name__)

        while True:
            for fut in [f for f in self._jobs if f.done()]:
                job = self._jobs.pop(fut)
                rec = job.get("record")
                try:
                    completed = self._complete_job(job, fut)
                    if completed is not None and completed["stage"] in TERMINAL:
                        self.run_failures = 0
                except Exception as exc:
                    unavailable = exc if isinstance(exc, EvaluationUnavailable) else EvaluationUnavailable(
                        f"{type(exc).__name__}: {exc}")
                    failure = self._record_failure(unavailable) or failure
                    if rec is not None:
                        rec["failed_stage"] = job["stage"]
                        if failure:
                            rec["halted"] = f"{type(exc).__name__}: {exc}"
                        else:
                            stages.finish(rec, "run_failed", str(exc), label="run_failed")
                    elif job.get("merge_key") and job["round"] == rd["round"]:
                        merges[job["merge_key"]] = dict(of=job["of"], adopt=False, skipped=f"measurement: {exc}")
                self.save()
            if self._out_of_time():
                self.adapter.cancel_pending()
                for rec in records.values():
                    if rec["stage"] not in TERMINAL:
                        rec["unmeasured"] = "time budget"
                rd["not_started"] = [r["card_index"] for r in records.values() if "started_at" not in r]
                return None
            if failure:
                return failure
            active = active_uids()
            # §4.6: no new card when one more card and one re-measurement no longer fit.
            if any(r["stage"] == "pending" and "started_at" not in r and not r.get("unmeasured")
                   for r in records.values()) and self._no_new_cards():
                rd["card_estimate"] = self._card_estimate()
                for rec in records.values():
                    if rec["stage"] == "pending" and "started_at" not in rec and rec["uid"] not in active:
                        rec["unmeasured"] = "time budget (not started)"
                rd["not_started"] = [r["card_index"] for r in records.values() if "started_at" not in r]
            ordered = sorted(records.values(), key=lambda r: (-(r.get("delta_mb") or 0), r["card_index"]))
            for rec in ordered:
                if rec["uid"] in active or rec.get("unmeasured"):
                    continue
                stage = rec["stage"]
                if stage in TERMINAL:
                    continue
                card = cards[rec["card_index"]]
                cand = self.versions.get(rec.get("candidate"))
                if stage in ("pending", "repair"):
                    if count("implementation") < self.cfg.b:
                        submit(rec, "implementation", stages.build, snap, copy.deepcopy(rec), card, cand)
                elif stage in ("built", "smoke_pass", "minibatch_pass", "validated"):
                    if stage == "validated" and cand.content_hash in self._results:
                        if count("review") < self.cfg.b:
                            submit(rec, "review", stages.review, snap, copy.deepcopy(rec), card, cand,
                                   self._results[cand.content_hash])
                    elif count("measurement") < self.cfg.b:
                        submit(rec, "measurement", self._next_stage(stage), snap, copy.deepcopy(rec), card, cand)
            qualified = [r for r in records.values() if r["stage"] == "measured" and r["qualification"]["qualified"]]
            best = decide.pick_best(qualified, self.cfg.tie_fraction)
            if best and len(prefetched) < self.cfg.speculative_prefetches:
                if self._start_prefetch(self.versions[best["candidate"]]):
                    prefetched.add(best["candidate"])
                    rd["prefetched"] = sorted(prefetched)
            if self.cfg.merge and len(qualified) >= 2 and count("measurement") < self.cfg.b:
                second = max((r for r in qualified if r is not best), key=lambda r: (r["delta_val"], -r["card_index"]))
                key = f"{best['candidate']}|{second['candidate']}"
                if key not in merges and not any(j.get("merge_key") == key for j in self._jobs.values()):
                    fut = self._pools["measurement"].submit(stages.merge, self.adapter, snap,
                            copy.deepcopy(best), copy.deepcopy(second), self.versions[best["candidate"]],
                            self.versions[second["candidate"]])
                    self._jobs[fut] = dict(merge_key=key, pool="measurement", parent=parent.content_hash,
                                           round=rd["round"], of=[best["card_index"], second["card_index"]])
            # Next analysis is speculative: only the same parent and exact next round may consume it.
            key = (parent.content_hash, rd["round"] + 1)
            if (self.cfg.speculative_analysis and cards and key not in self._analyses
                    and all(r["stage"] != "pending" or r["uid"] in active_uids() or r.get("unmeasured")
                            for r in records.values())):
                history = self.history_for(parent)
                for rec in records.values():
                    known = self._history_entry(rec, adopted=False)
                    if known is not None and known["label"] != "qualified_not_adopted":
                        history.append({k: v for k, v in known.items() if k != "parent"})
                    elif not rec.get("unmeasured"):
                        history.append(dict(title=rec["title"], label="in_progress", direction=rec.get("direction")))
                self._analyses[key] = self._analysis_pool.submit(self._analysis, parent, rd["round"] + 1,
                    history, list(self.analyst_shown.get(parent.content_hash, [])), self.no_adoption)
            unfinished = [r for r in records.values() if r["stage"] not in TERMINAL and not r.get("unmeasured")]
            current_merges = any(j.get("merge_key") and j["round"] == rd["round"] for j in self._jobs.values())
            if not unfinished and not current_merges:
                return None
            self.save()
            if self._jobs:
                wait(list(self._jobs), timeout=.02, return_when=FIRST_COMPLETED)
            else:
                raise RuntimeError("unfinished v4 records have no runnable stage")

    def _complete_job(self, job, future):
        """Publish a worker result on the coordinator, including results received during shutdown."""
        result = future.result()
        if job.get("merge_key"):
            row, version, val = result
            if version is not None:
                self.versions[version.content_hash] = version
                if val is not None:
                    self._val_cache[(version.content_hash, job["parent"])] = val
            for rd in self.rounds + ([self.current] if self.current else []):
                if rd["round"] == job["round"]:
                    rd.setdefault("merges", {})[job["merge_key"]] = row
            return None
        rec = job["record"]
        if job.get("kind") == "build":
            result, version = result
            if version is not None:
                self.versions[version.content_hash] = version
        elif job.get("kind") == "validation":
            result, val = result
            self._results[result["candidate"]] = val
            self._val_cache[(result["candidate"], result["parent"])] = val
        result["card_index"] = rec["card_index"]
        if rec.get("unmeasured"):
            result["unmeasured"] = rec["unmeasured"]
        if rec.get("started_at"):          # the worker's copy predates the coordinator's start stamp
            result.setdefault("started_at", rec["started_at"])
        rec.clear()
        rec.update(result)
        return rec

    def _record_failure(self, exc):
        from ..adapter import EvaluationCancelled
        if isinstance(exc, EvaluationCancelled):
            return "time_budget" if self._out_of_time() else "process_failure"
        if isinstance(exc, BudgetExhausted):
            return "budget_exhausted"
        self.run_failures += 1
        if self.run_failures >= self.cfg.max_consecutive_run_failures:
            self.failure_detail = f"{self.run_failures} consecutive run failures; last: {exc}"[:500]
            return "process_failure"
        return None

    # -- round-end decision (§4.4) ------------------------------------------

    def _remeasure(self, rec: dict, parent: Version) -> dict:
        """Re-measure *rec*'s candidate once on the whole validation set and re-judge it with the same tier."""
        start = time.monotonic()
        snap = self._snapshot(parent)
        ids = list(snap.mb) + list(snap.rest)
        cand = self.versions[rec["candidate"]]
        try:
            merged = self.remeasured_val(cand, parent.content_hash)
            attempts = decide.attempts_by_task(merged)
            eligible = [t for t in ids if len(attempts.get(t, ())) >= 2]
            single = len(ids) - len(eligible)
            d = decide.paired_delta(merged, snap.parent_val, ids, rerun_var=snap.rerun_var, families=snap.families)
            stages.check_dropped(d, len(ids), "re-measurement")
            sg, _ = stages.observe(self.adapter, merged, decide.restrict(snap.parent_val, eligible), snap)
        except Exception as exc:
            unavailable = exc if isinstance(exc, EvaluationUnavailable) else EvaluationUnavailable(
                f"{type(exc).__name__}: {exc}")
            stages.timed(rec, "remeasure_s", start)
            if self._record_failure(unavailable):
                raise unavailable from (None if unavailable is exc else exc)
            return dict(status="run_failed", passed=False, reason=str(exc)[:300])
        self.run_failures = 0
        stages.timed(rec, "remeasure_s", start)
        tier_ = (rec.get("mechanism") or {}).get("tier")
        q = decide.qualification(d["delta"], d["se"], tier_, sg, c=self.cfg.c, c_mech=self.cfg.c_mech)
        return dict(status="measured", passed=q["qualified"], delta_val=d["delta"], se_val=d["se"],
                    se_own=d["se_own"], se_floor=d["se_floor"], se_method=d["se_method"], n_val=d["n"],
                    dropped_val=d["dropped"], single_attempt_tasks=single, safeguards=sg, qualification=q, tier=tier_,
                    first=dict(delta_val=rec.get("delta_val"), se_val=rec.get("se_val")))

    def _decide(self, rd: dict, parent: Version) -> None:
        t0 = self.clock()
        records = list(rd["records"].values())
        qualified = [r for r in records if r["stage"] == "measured" and r["parent"] == parent.content_hash
                     and r["qualification"]["qualified"]]
        order, pool = [], list(qualified)
        while pool:
            nxt = decide.pick_best(pool, self.cfg.tie_fraction)
            order.append(nxt)
            pool.remove(nxt)
        decision = dict(qualified=[r["card_index"] for r in qualified], order=[r["card_index"] for r in order],
                        best=order[0]["card_index"] if order else None, remeasured=[], merge=None, adopted=None)
        chosen = None
        for rec in order:
            if not self.cfg.remeasure:
                chosen = rec
                break
            rm = rec.get("remeasure")
            if rm is None:
                if self._out_of_time():
                    decision["remeasure_skipped"] = "time budget"
                    break
                rm = rec["remeasure"] = self._remeasure(rec, parent)
                self.save()
            decision["remeasured"].append(rec["card_index"])
            if rm.get("passed"):
                chosen = rec
                break
        adopted, node = None, None
        if chosen is not None:
            rm = chosen.get("remeasure") or {}
            adopted = self.versions[chosen["candidate"]]
            node = dict(kind="candidate", components=[chosen["candidate"]],
                        delta_val=rm.get("delta_val", chosen["delta_val"]), se_val=rm.get("se_val", chosen["se_val"]),
                        delta_first=chosen["delta_val"], tier=chosen["mechanism"]["tier"],
                        flags=list(chosen.get("flags") or []), remeasured=bool(rm),
                        card_indices=[chosen["card_index"]])
            if self.cfg.merge and len(qualified) >= 2 and chosen is order[0]:
                second = max((r for r in qualified if r is not chosen), key=lambda r: (r["delta_val"], -r["card_index"]))
                key = f"{chosen['candidate']}|{second['candidate']}"
                decision["merge"] = m = rd.get("merges", {}).get(key) or dict(
                    of=[chosen["card_index"], second["card_index"]], adopt=False, skipped="not complete before the decision")
                if m.get("adopt"):
                    adopted = self.versions[m["candidate"]]
                    node = dict(kind="merge", components=[chosen["candidate"], m["candidate"]],
                                delta_val=m["delta_val"], se_val=m["se_val"], tier=None, remeasured=False,
                                flags=list(chosen.get("flags") or []),
                                card_indices=sorted([chosen["card_index"], second["card_index"]]))
        in_lineage = set(node["card_indices"]) if node else set()
        for r in records:
            self._label(r, parent, adopted=r["card_index"] in in_lineage)
        if adopted is not None:
            self.lineage.append(dict(node, version=adopted.content_hash, parent=parent.content_hash,
                                     round=rd["round"], elapsed_s=self.elapsed()))
            decision["adopted"] = adopted.content_hash
            self.no_adoption = 0
        else:
            self.no_adoption += 1
        rd["decision"] = decision
        rd["decision_s"] = self.clock() - t0
        self.save()

    def _label(self, rec: dict, parent: Version, *, adopted: bool) -> None:
        """Publish the outcome once the round's decision is final."""
        entry = self._history_entry(rec, adopted=adopted)
        if entry is not None:
            rec["label"] = entry["label"]
            self.history.append(entry)

    def _history_entry(self, rec: dict, *, adopted: bool) -> Optional[dict]:
        stage = rec["stage"]
        if rec.get("unmeasured") or stage not in TERMINAL:
            return None
        if stage == "preflight_skipped":
            label = "preflight_skipped"
        elif stage == "error":
            label = "implementation_failed"
        elif stage == "run_failed":
            label = "run_failed"
        elif stage == "smoke_fail":
            label = "smoke_fail"
        elif stage == "minibatch_fail":
            label = rec.get("label") or "minibatch_fail"
        elif adopted:
            label = "adopted"
        elif rec["qualification"]["qualified"]:
            label = ("remeasure_failed" if rec.get("remeasure") and not rec["remeasure"].get("passed")
                     else "qualified_not_adopted")
        elif "safeguard_breached" in rec["qualification"]["reasons"]:
            label = "safeguard_breached"
        else:
            label = "below_threshold"
        entry = dict(parent=rec["parent"], title=rec["title"], label=label, direction=rec.get("direction"))
        if rec.get("direction_inferred"):
            entry["direction_inferred"] = True
        entry.update(agent_context.feedback(rec, label))
        sg = rec.get("safeguards") or {}
        if label == "safeguard_breached" and sg.get("breached"):
            entry["safeguards_breached"] = list(sg["breached"])
        if label == "remeasure_failed" and ((rec["remeasure"].get("safeguards") or {}).get("breached")):
            entry["safeguards_breached"] = list(rec["remeasure"]["safeguards"]["breached"])
        if rec.get("repaired"):
            entry["repaired"] = True
        if rec.get("failure_kind") and label == "implementation_failed":
            entry["failure_kind"] = rec["failure_kind"]
        if decide.NEEDS_HUMAN_REVIEW in (rec.get("flags") or []):
            entry["needs_human_review"] = True
        tier_ = ((rec.get("mechanism") or {}).get("tier"))
        if stage == "measured" and tier_:
            entry["mechanism"] = tier_
        return entry

    # -- ablation pruning (§4.2) --------------------------------------------

    def _rebuild(self, skip: set[int]) -> Optional[Version]:
        """The final version without the lineage units in *skip* (0 = first adopted node); None on conflict."""
        units = self.lineage[1:]
        first = min(skip)
        base = self.versions[self.lineage[first]["version"]]      # everything before the first skipped unit
        for j in range(first + 1, len(units)):
            if j in skip:
                continue
            for component in units[j]["components"]:
                self.adapter.check_runtime()
                try:
                    base = self.adapter.rebase(self.versions[component], base)
                except EvaluationUnavailable:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("ablation rebase failed: %s", exc)
                    return None
                if base is None:
                    return None
                self.versions[base.content_hash] = base
        return base

    def _val_of(self, version: Version, split_of: str) -> EvalResult:
        if self._node(version.content_hash) is not None:
            return self.parent_val(version)
        return self.measure_val(version, split_of)

    def _validation_seconds(self) -> float:
        st = (self.feasibility or {}).get("stage_seconds") or {}
        return float(st.get("minibatch") or 0.0) + float(st.get("validation_rest") or 0.0)

    def _ablate(self) -> None:
        if self.ablation is not None and self.ablation.get("status") in ("done", "skipped"):
            return
        units = self.lineage[1:]
        if not self.cfg.ablation:
            self.ablation = dict(status="skipped", reason="disabled")
        elif not units:
            self.ablation = dict(status="skipped", reason="nothing adopted")
        if self.ablation is not None and self.ablation.get("status") == "skipped":
            self.save()
            return
        final = self.lineage[-1]
        ab = self.ablation or dict(status="running", units={}, split_of=final["parent"], final=final["version"],
                                   started_elapsed=self.elapsed())
        if "budget" not in ab:
            # One measurement per unit except the last (removing it leaves its parent, already cached),
            # plus the re-measurement of the pruned version.
            measured = len(units)
            needed = ((measured + self.cfg.ablation_parallel - 1) // self.cfg.ablation_parallel + 1) * self._validation_seconds()
            ab["budget"] = dict(validation_seconds=self._validation_seconds(), measurements=measured,
                                needed_s=needed, remaining_s=self._remaining())
            if needed > self._remaining():
                self.ablation = dict(status="skipped", reason="time budget", budget=ab["budget"])
                self.save()
                return
        self.ablation = ab
        self.save()
        final_v = self.versions[final["version"]]
        final_val = self.parent_val(final_v)
        ids = self.partitions["validation"]
        cal = self.calibration
        kill = self.cfg.ablation_kill
        # Build the independent versions on the coordinator; workers only measure immutable versions.
        jobs = []
        for i, node in enumerate(units):
            if str(i) in ab["units"]:
                continue
            if self._out_of_time():
                ab.update(status="skipped", reason="time budget")
                self.save()
                return
            row = dict(index=i + 1, version=node["version"], round=node["round"], kind=node["kind"],
                       card_indices=node.get("card_indices", []))
            try:
                ablated = self._rebuild({i})
            except Exception as exc:
                failure = self._record_failure(exc)
                if failure:
                    raise (exc if isinstance(exc, EvaluationUnavailable)
                           else EvaluationUnavailable(f"ablation: {type(exc).__name__}: {exc}"))
                ab["units"][str(i)] = dict(row, status="run_failed", reason=str(exc), prune=False)
                continue
            if ablated is None:
                ab["units"][str(i)] = dict(row, status="conflict", prune=False)
            else:
                jobs.append((i, row, ablated))
        mb, rest = self.split_for(ab["split_of"])
        def evaluate(version):
            self.adapter.check_runtime()
            # Known lineage results are captured on the coordinator, not read through a shared mutable cache.
            return decide.combine(self.adapter.evaluate_subset(version, None, "mb", mb, 1)[0],
                                  self.adapter.evaluate_subset(version, None, "rest", rest, 1)[0])
        with ThreadPoolExecutor(max_workers=self.cfg.ablation_parallel, thread_name_prefix="v4-ablation") as pool:
            pending = {}
            try:
                for i, row, version in jobs:
                    if self._node(version.content_hash) is not None:
                        fut = Future()
                        fut.set_result(self.parent_val(version))
                    else:
                        fut = pool.submit(evaluate, version)
                    pending[fut] = (i, row, version)
                while pending:
                    done, _ = wait(pending, timeout=.05, return_when=FIRST_COMPLETED)
                    if self._out_of_time():
                        self.adapter.cancel_pending()
                        for fut in pending:
                            fut.cancel()
                        ab.update(status="skipped", reason="time budget")
                        self.save()
                        return
                    for fut in done:
                        i, row, version = pending.pop(fut)
                        try:
                            val = fut.result()
                            d = decide.paired_delta(val, final_val, ids, rerun_var=cal["rerun_var"], families=self.families)
                            row.update(status="measured", ablated=version.content_hash, delta=d["delta"], se=d["se"],
                                       n=d["n"], prune=d["delta"] is not None and d["se"] is not None
                                       and d["delta"] >= -kill * d["se"])
                            self.run_failures = 0
                        except Exception as exc:
                            failure = self._record_failure(exc)
                            row.update(status="run_failed", reason=str(exc), prune=False)
                            if failure:
                                # Leave this unit retryable; completed units remain in the ledger.
                                raise (exc if isinstance(exc, EvaluationUnavailable)
                                       else EvaluationUnavailable(f"ablation: {type(exc).__name__}: {exc}"))
                        ab["units"][str(i)] = row
                        self.save()
            except BaseException as exc:
                # Context-manager shutdown joins workers; wake them before leaving the block.
                if self._out_of_time() or isinstance(exc, BudgetExhausted):
                    self.adapter.cancel_pending()
                else:
                    self.adapter.stop_polling()
                for fut in pending:
                    fut.cancel()
                raise
        pruned = sorted(int(k) for k, r in ab["units"].items() if r.get("prune"))
        ab["pruned"] = [i + 1 for i in pruned]
        if not pruned:
            ab.update(status="done", accepted=False, reason="every adopted change contributes")
        else:
            if self._out_of_time():
                ab.update(status="done", accepted=False, reason="time budget before prune validation")
                self.save()
                return
            try:
                version = self._rebuild(set(pruned))
                if version is None:
                    ab.update(status="done", accepted=False, reason="rebase conflict while removing the pruned changes")
                else:
                    val = self._val_of(version, ab["split_of"])
                    d = decide.paired_delta(val, final_val, ids, rerun_var=cal["rerun_var"], families=self.families)
                    ok = d["delta"] is not None and d["se"] is not None and d["delta"] >= -kill * d["se"]
                    ab.update(status="done", candidate=version.content_hash,
                              check=dict(delta=d["delta"], se=d["se"], n=d["n"]), accepted=ok,
                              reason=None if ok else "the pruned version is worse on the validation set")
                    if ok:
                        ab["final"] = version.content_hash
                        for i in pruned:
                            self._mark_pruned(units[i])
            except Exception as exc:
                failure = self._record_failure(exc)
                if failure:
                    raise (exc if isinstance(exc, EvaluationUnavailable)
                           else EvaluationUnavailable(f"ablation: {type(exc).__name__}: {exc}"))
                ab.update(status="done", accepted=False, reason=f"pruned version unavailable: {exc}")
        ab["wall_s"] = self.elapsed() - ab.get("started_elapsed", self.elapsed())
        self.ablation = ab
        self.save()

    def _mark_pruned(self, unit: dict) -> None:
        for rd in self.rounds:
            if rd["round"] != unit["round"]:
                continue
            for r in (rd.get("records") or {}).values():
                if r["card_index"] in unit.get("card_indices", []):
                    r["pruned"] = True
                    self.history.append(dict(parent=unit["parent"], title=r["title"], label="pruned"))

    def final_version(self) -> str:
        """The endpoint's final version: the pruned version when ablation accepted one, else the pointer."""
        return final_version(self.lineage, self.ablation)


def final_version(lineage: list[dict], ablation: Optional[dict]) -> str:
    ab = ablation or {}
    return ab["final"] if ab.get("status") == "done" and ab.get("accepted") else lineage[-1]["version"]


def _failure(exc: EvaluationUnavailable) -> str:
    return "budget_exhausted" if isinstance(exc, BudgetExhausted) else "process_failure"


# Recorded in every ledger: where this implementation departs from method-v4.1.md.
DEVIATIONS = [
    "SN keeps v2's 60/40 search/validation split decided at iteration creation (deferred split: D2); "
    "the test set is the iteration's registered sealed selection",
    "calibration still runs inside `v4 run` (seed A/A on the validation set at calibration_k, diagnosis sample "
    "at diagnose_k); no separate probe sample P / diagnosis sample D yet (D2)",
    "the seed's calibration A/A stands in for the parent's A/A in the mechanical check and the reviewer's control pairs",
    "safeguards come from the v4 config key `safeguards` (default tokens_per_task_ratio +50%, answer_produced_rate "
    "-2 points), not from the iteration protocol; the protocol no longer needs registered safeguards",
    "answer_produced_rate counts observed output status success; only proven exhausted infrastructure failures "
    "are dropped from paired comparisons (at most 10% of the full validation set)",
    "the remaining-time rule (one card + one re-measurement) uses observed means, else the calibration's validation "
    "wall and agent_estimate_minutes",
    "an analysis that yields no card counts as a run failure (max_consecutive_run_failures halts the run)",
    "ablation removes one lineage node at a time; a merge node (two cards) is removed as one unit",
    "the analyst sees categorical validation outcomes, mechanism tiers and per-direction summaries, never "
    "validation scores/cases or reviewer prose",
]
