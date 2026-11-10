"""Durable loop state and the on-disk evidence ledger (binding plan §2.7, method-v3 §2.4).

Files in a run's ``state_dir``:

``state.json``
    The complete loop state, rewritten atomically (temp file + ``os.replace``)
    after every state transition. ``run_optimization`` resumes from it.
``rounds.jsonl``
    One JSON line per finished round (``RoundResult`` as a dict). Kept in sync
    with ``state.json`` on every round end and on resume (missing lines are
    appended; a torn last line is dropped), so it never holds duplicates.
``ledger.json``
    The three-section evidence ledger (§2.4), rewritten at every round end and
    when the run halts. Utility, selection and mechanism are reported side by
    side and never summed.

No model clients, no SN imports.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections import Counter
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .adapter import Card, Predicate, Version

STATE_SCHEMA_VERSION = 1
LEDGER_SCHEMA_VERSION = 1

STATE_FILE = "state.json"
ROUNDS_FILE = "rounds.jsonl"
LEDGER_FILE = "ledger.json"

# Labels the analyst may see (§2.3: labels only, no scores).
HISTORY_LABELS = ("smoke_fail", "screen_fail", "provisional_adopted", "confirmed",
                  "rejected", "insufficient", "unmeasurable")
ATTRIBUTION_LABELS = ("supported", "refuted", "insufficient", "unattributed")

INTEGRITY_RULE = (
    "Every attempt is recorded, including smoke_fail, error, stale_parent and attempts aborted "
    "by a process-failure halt; failed records are never deleted; field names are only added, "
    "never renamed (method-v3 §2.4)."
)


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _finite(obj: Any) -> Any:
    """Non-finite floats become None (strict JSON); containers recurse."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    return obj


def dumps(obj: Any, **kw: Any) -> str:
    return json.dumps(_finite(obj), allow_nan=False, ensure_ascii=False, **kw)


def atomic_write_json(path: str | Path, obj: Any) -> None:
    """Write *obj* as JSON to *path* atomically: temp file in the same directory, fsync, os.replace.

    A crash leaves either the previous file or the new one, never a torn file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = dumps(obj, indent=1, sort_keys=False)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: str | Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def config_digest(config: Any) -> tuple[dict, str]:
    """(asdict(config), sha256 of its canonical JSON)."""
    data = asdict(config) if is_dataclass(config) else dict(config)
    canonical = json.dumps(_finite(data), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return data, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Object (de)serialisation
# ---------------------------------------------------------------------------

def _require_json(value: Any, what: str) -> Any:
    if callable(value):
        raise TypeError(
            f"{what} is a callable and cannot be persisted in state.json; give predicates as "
            "serialisable specs (e.g. v2 signature dicts) or run without state_dir"
        )
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{what} is not JSON-serialisable and cannot be persisted: {exc}") from exc
    return value


def predicate_to_dict(predicate: Optional[Predicate]) -> Optional[dict]:
    if predicate is None:
        return None
    return dict(boolean_expression=_require_json(predicate.boolean_expression, "Predicate.boolean_expression"),
                predicted_direction=predicate.predicted_direction,
                predicted_threshold=predicate.predicted_threshold)


def predicate_from_dict(data: Optional[dict]) -> Optional[Predicate]:
    if data is None:
        return None
    return Predicate(boolean_expression=data["boolean_expression"],
                     predicted_direction=data["predicted_direction"],
                     predicted_threshold=data.get("predicted_threshold"))


def card_to_dict(card: Card) -> dict:
    return dict(failure_mode_desc=card.failure_mode_desc, change_hypothesis=card.change_hypothesis,
                predicate=predicate_to_dict(card.predicate), ref=card.ref)


def card_from_dict(data: dict) -> Card:
    return Card(failure_mode_desc=data["failure_mode_desc"], change_hypothesis=data["change_hypothesis"],
                predicate=predicate_from_dict(data.get("predicate")), ref=data.get("ref"))


def version_metadata(version: Version) -> dict:
    return _require_json(dict(version.metadata or {}), f"Version {version.content_hash!r} metadata")


# ---------------------------------------------------------------------------
# rounds.jsonl
# ---------------------------------------------------------------------------

def sync_rounds_jsonl(path: str | Path, rounds: Iterable[dict]) -> int:
    """Make *path* hold one line per finished round, in order; returns lines appended.

    Lines already present (by round_id) are kept; a torn last line (crash
    mid-append) is dropped first. Never rewrites earlier lines.
    """
    path = Path(path)
    present: set[int] = set()
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        good: list[str] = []
        for line in lines:
            try:
                present.add(int(json.loads(line)["round_id"]))
                good.append(line if line.endswith("\n") else line + "\n")
            except (ValueError, KeyError, TypeError):
                break   # torn tail; everything after it is dropped
        if len(good) != len(lines) or (lines and not lines[-1].endswith("\n")):
            atomic_text(path, "".join(good))
    appended = 0
    with open(path, "a", encoding="utf-8") as fh:
        for rnd in rounds:
            if int(rnd["round_id"]) in present:
                continue
            fh.write(dumps(rnd) + "\n")
            appended += 1
        fh.flush()
        os.fsync(fh.fileno())
    return appended


def atomic_text(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Ledger (§2.4)
# ---------------------------------------------------------------------------

SCREEN_VERDICTS = ("screen_pass", "screen_fail", "unmeasurable")


def _confirmations(rounds: list[dict]) -> list[dict]:
    return [dict(c, round_id=r["round_id"]) for r in rounds for c in r.get("confirmations") or []]


def build_ledger(
    *,
    rounds: list[dict],
    lineage: list[dict],
    config: dict,
    config_sha256: str,
    context: dict,
    aborted_attempts: Optional[list[dict]] = None,
) -> dict:
    """Three separate evidence sections plus context and integrity (method-v3 §2.4).

    *rounds*: RoundResult dicts in order (the open round, if any, included).
    *lineage*: lineage node records. *context*: pool sizes, halt reason, α
    note, timings etc., copied through. *aborted_attempts*: records of
    attempts aborted by a process-failure halt and retried later.
    """
    aborted_attempts = list(aborted_attempts or [])
    z_s = config.get("z_screen", 0.84)
    confs = _confirmations(rounds)
    by_end = {}
    for c in confs:
        by_end.setdefault(c.get("segment_end"), c)   # first confirmation of that end

    # -- utility --------------------------------------------------------
    milestones = []
    for c in confs:
        if c.get("verdict") != "confirmed":
            continue
        cost_s, cost_e = c.get("cost_start"), c.get("cost_end")
        milestones.append(dict(
            round_id=c["round_id"], segment_start=c.get("segment_start"), segment_end=c.get("segment_end"),
            delta_fresh=c.get("delta_fresh"), se_fresh=c.get("se_fresh"),
            k_fresh=c.get("k_fresh"), n_fresh=c.get("n_fresh"), n_unpaired=c.get("n_unpaired"),
            fresh_batch_index=c.get("fresh_batch_index"),
            provisional_steps_in_segment=c.get("segment_steps"),
            safeguards_end=c.get("safeguards_end"),
            cost_start=cost_s, cost_end=cost_e,
            cost_change=(cost_e - cost_s) if cost_s is not None and cost_e is not None else None,
        ))
    utility = dict(
        certified_milestones=milestones,
        n_certified_milestones=len(milestones),
        lineage=[dict(version_hash=n["version_hash"], parent_hash=n.get("parent_hash"),
                      state=n.get("state"), card_ref=n.get("card_ref")) for n in lineage],
        note="Sealed-set curve is produced by the end step (W6); provisional nodes are hollow points.",
    )

    # -- selection ------------------------------------------------------
    attempts = [dict(c, round_id=r["round_id"]) for r in rounds for c in r.get("candidates") or []]
    screened = []
    ladder_blocked = 0
    for a in attempts:
        if a.get("verdict") not in SCREEN_VERDICTS and a.get("delta_val") is None:
            continue
        conf = by_end.get(a.get("candidate")) if a.get("verdict") == "screen_pass" else None
        d_val, d_fresh = a.get("delta_val"), (conf or {}).get("delta_fresh")
        blocked = False
        if (a.get("verdict") == "screen_fail" and d_val is not None and a.get("se_val") is not None
                and a.get("score_val") is not None and a.get("best_before") is not None):
            blocked = (d_val - z_s * a["se_val"] > 0.0
                       and a["score_val"] <= a["best_before"] + (a.get("ladder_m") or 0.0))
        ladder_blocked += blocked
        screened.append(dict(
            round_id=a["round_id"], card_ref=a.get("card_ref"), candidate=a.get("candidate"),
            parent=a.get("parent"), verdict=a.get("verdict"), halted=bool(a.get("halted")),
            delta_val=d_val, se_val=a.get("se_val"), score_val=a.get("score_val"),
            best_before=a.get("best_before"), ladder_m=a.get("ladder_m"),
            val_pool_size=a.get("val_pool_size"), ladder_blocked=blocked,
            confirmation_verdict=(conf or {}).get("verdict"),
            delta_fresh=d_fresh,
            shrinkage=(d_val - d_fresh) if d_val is not None and d_fresh is not None else None,
            safeguards=a.get("safeguards"),
        ))
    segment_confirmations = []
    for c in confs:
        cum, d_fresh = c.get("cumulative_delta_val"), c.get("delta_fresh")
        segment_confirmations.append(dict(
            round_id=c["round_id"], verdict=c.get("verdict"), segment_start=c.get("segment_start"),
            segment_end=c.get("segment_end"), segment_steps=c.get("segment_steps"),
            cumulative_delta_val=cum, delta_fresh=d_fresh, se_fresh=c.get("se_fresh"),
            segment_shrinkage=(cum - d_fresh) if cum is not None and d_fresh is not None else None,
            safeguards_end=c.get("safeguards_end"),
        ))
    adopted = [n for n in lineage if n.get("parent_hash") is not None]
    rolled = [n for n in adopted if n.get("state") == "rolled_back"]
    selection = dict(
        screened_candidates=screened,
        n_evaluated_on_validation_pool=len(screened),
        verdict_counts=dict(Counter(a.get("verdict") for a in attempts)),
        confirmation_verdict_counts=dict(Counter(c.get("verdict") for c in confs)),
        ladder_blocked=ladder_blocked,
        ladder_blocked_rule="screen_fail with Δ_val − z_s·SE_val > 0 but score_val ≤ best + m",
        segment_confirmations=segment_confirmations,
        provisional_adopted=len(adopted),
        provisional_rolled_back=len(rolled),
        share_provisional_rolled_back=(len(rolled) / len(adopted)) if adopted else None,
    )

    # -- mechanism ------------------------------------------------------
    attributions = []
    for c in confs:
        attr = dict(c.get("attribution") or dict(label="unattributed", possibly_biased=False,
                                                  reason="not recorded"))
        attributions.append(dict(round_id=c["round_id"], segment_end=c.get("segment_end"),
                                 confirmation_verdict=c.get("verdict"), **attr))
    certified_attr = [a for a in attributions if a["confirmation_verdict"] == "confirmed"]
    n_supported = sum(a.get("label") == "supported" for a in certified_attr)
    cards = sum(r.get("cards_generated") or 0 for r in rounds)
    with_pred = sum(r.get("cards_with_predicate") or 0 for r in rounds)
    floor = config.get("predicate_coverage_floor", 0.60)
    rate = (with_pred / cards) if cards else None
    mechanism = dict(
        attributions=attributions,
        label_counts=dict(Counter(a.get("label") for a in attributions)),
        n_certified_milestones=len(certified_attr),
        n_certified_supported=n_supported,
        n_certified_supported_possibly_biased=sum(
            a.get("label") == "supported" and bool(a.get("possibly_biased")) for a in certified_attr),
        share_supported_among_certified=(n_supported / len(certified_attr)) if certified_attr else None,
        predicate_attachment=dict(
            cards_generated=cards, cards_with_predicate=with_pred, rate=rate, floor=floor,
            meets_floor=(rate is not None and rate >= floor),
        ),
        counts_for_mechanism_comparison=(rate is not None and rate >= floor),
        granularity="segment: attribution is computed for the segment-end card only (§2.2, §7)",
    )

    # -- integrity ------------------------------------------------------
    attempted_cards = {(a["round_id"], a.get("card_index")) for a in attempts}
    integrity = dict(
        rule=INTEGRITY_RULE,
        n_attempts_recorded=len(attempts),
        attempts_by_verdict=dict(Counter(a.get("verdict") for a in attempts)),
        n_halted_attempts=sum(bool(a.get("halted")) for a in attempts),
        n_aborted_attempts_retried=len(aborted_attempts),
        aborted_attempts=aborted_attempts,
        cards_generated=cards,
        cards_not_attempted=max(0, cards - len(attempted_cards)),
        n_confirmation_attempts=len(confs),
    )

    return dict(
        schema_version=LEDGER_SCHEMA_VERSION,
        utility=utility,
        selection=selection,
        mechanism=mechanism,
        context=dict(context, config=config, config_sha256=config_sha256),
        integrity=integrity,
        note="Three evidence sections are reported separately and never summed (method-v3 §2.4).",
    )
