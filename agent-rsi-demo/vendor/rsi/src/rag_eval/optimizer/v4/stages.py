"""Independent v4 workers. Only the coordinator writes persistent state.

Records are private copies. Results and versions return to the coordinator;
evaluation calls replay their deterministic keys after interruption.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from ..adapter import Diff, EvaluationUnavailable, ImplementationDeclined, SmokeResult
from . import calibrate, decide, mechanism

# method-v4.1 §4.5: more than this share of the compared tasks dropped (no score on a side
# after the adapter's infrastructure retries) makes the measurement a run failure.
MAX_DROPPED_FRACTION = 0.10
# method-v4.1 §4.1: implementation failure classes (recorded, fed back to the analyst).
IMPLEMENTATION_FAILURES = ("timeout", "empty_diff", "workspace_error", "malformed_report")


def timed(rec, name, start):
    rec.setdefault('timings', {})[name] = rec.get('timings', {}).get(name, 0.0) + time.monotonic() - start


def finish(rec, stage, reason=None, **extra):
    rec.update(stage=stage, reason=reason, finished_at=datetime.now(timezone.utc).isoformat(), **extra)
    return rec


def diff_fields(diff):
    text = (diff.content if diff is not None else '') or ''
    count = sum(ln[:1] in '+-' and not ln.startswith(('+++', '---')) for ln in text.splitlines())
    return dict(diff_lines=count, diff_excerpt=text[:20000], diff_truncated=len(text) > 20000)


def cache(adapter, result):
    try:
        return calibrate.cache_summary(adapter.timing_profile(result))
    except Exception:
        return None


def failure_kind(exc: BaseException) -> str:
    """One of IMPLEMENTATION_FAILURES for an implementer exception (adapter-independent text rules)."""
    text = str(exc).lower()
    if isinstance(exc, TimeoutError) and 'agent' in text:
        return 'timeout'
    if 'preflight' in text or 'report' in text and ('malformed' in text or 'missing' in text):
        return 'malformed_report'
    return 'workspace_error'


def build(adapter, snap, rec, card, candidate=None):
    start = time.monotonic()
    try:
        if rec['stage'] == 'repair':
            ce, pe = adapter.evaluate_pair(candidate, snap.parent, list(snap.smoke), 1, purpose='smoke')
            result = SmokeResult(candidate.content_hash, card.ref or '', True, None, None, 'fail')
            diff = adapter.repair(card, snap.parent, candidate, smoke=result, candidate_smoke=ce,
                                  parent_smoke=pe, attempt=rec['attempt'] + 1)
            if diff is None:
                return finish(rec, 'smoke_fail', 'behaviour did not change (no repair)'), candidate
            rec.setdefault('prior_attempts', []).append(dict(attempt=rec['attempt'],
                                                             candidate=rec['candidate'], smoke=rec['smoke']))
            rec['attempt'] += 1
            rec['repaired'] = True
        else:
            diff = adapter.implement(card, adapter.workspace(snap.parent, card))
            rec['attempt'] = 1
        if diff.metadata.get('preflight'):
            rec['preflight'] = diff.metadata['preflight']
        if not (diff.content or '').strip():
            return finish(rec, 'error', 'implement: the implementer left no change', failure_kind='empty_diff'), candidate
        candidate = adapter.materialize(diff, snap.parent)
        rec.update(stage='built', candidate=candidate.content_hash,
                   touches_preparation=adapter.touches_preparation(diff), **diff_fields(diff))
        return rec, candidate
    except ImplementationDeclined as exc:
        if rec['stage'] == 'repair':
            rec.setdefault('prior_attempts', []).append(dict(attempt=rec['attempt'],
                candidate=rec.get('candidate'), smoke=rec.get('smoke')))
            rec['attempt'] += 1
        else:
            rec['attempt'] = 1
        rec.pop('candidate', None)
        rec['preflight'] = exc.report
        return finish(rec, 'preflight_skipped', exc.report['reason'], label='preflight_skipped'), None
    except EvaluationUnavailable:
        raise
    except Exception as exc:
        return finish(rec, 'error', f'implement: {type(exc).__name__}: {exc}', failure_kind=failure_kind(exc)), candidate
    finally:
        timed(rec, 'agent_s', start)


def smoke(adapter, snap, rec, card, candidate):
    start = time.monotonic()
    try:
        ce, pe = adapter.evaluate_pair(candidate, snap.parent, list(snap.smoke), 1, purpose='smoke')
        if not all(ce.trajectories.get(t) for t in snap.smoke):
            raise EvaluationUnavailable('smoke tasks without trajectories')
        check = (mechanism.smoke_behaviour(adapter, card.predicate, ce, pe) if snap.cfg.smoke_behaviour_check
                 else dict(verdict='unchecked', reason='behaviour check disabled'))
        rec['smoke'] = check
        if check['verdict'] == 'fail':
            if rec['attempt'] <= snap.cfg.smoke_repair_attempts:
                rec['stage'] = 'repair'
                return rec
            return finish(rec, 'smoke_fail', 'behaviour did not change as predicted')
        rec['stage'] = 'smoke_pass'
        return rec
    finally:
        timed(rec, 'smoke_s', start)


def observe(adapter, candidate, reference, snap):
    """(verdict, infrastructure info) of the configured safeguards (§4.5), noise-aware."""
    if (candidate.context or {}).get('v41_policy') or (reference.context or {}).get('v41_policy'):
        shared = set(candidate.per_task_scores) & set(reference.per_task_scores)
        candidate, reference = decide.restrict(candidate, shared), decide.restrict(reference, shared)
    observed = adapter.observe_safeguards(candidate, reference) or {}
    verdict = decide.safeguard_verdict(observed, snap.cfg.safeguards, noise=snap.safeguard_noise,
                                       z=snap.cfg.safeguard_z)
    return verdict, observed.get('infrastructure')


def safeguards(adapter, rec, candidate, parent, snap):
    start = time.monotonic()
    try:
        return observe(adapter, candidate, parent, snap)
    finally:
        timed(rec, 'safeguards_s', start)


def check_dropped(d, n_tasks, what):
    """§4.5 infrastructure rule: too many tasks without a score on a side is a run failure."""
    if d['n'] == 0 or d['dropped'] > MAX_DROPPED_FRACTION * n_tasks:
        raise EvaluationUnavailable(f'{what}: {d["dropped"]} of {n_tasks} tasks without a paired score '
                                    f'(limit {MAX_DROPPED_FRACTION:.0%}): run failure')


def minibatch(adapter, snap, rec, card, candidate):
    start = time.monotonic()
    cm, _ = adapter.evaluate_subset(candidate, None, 'mb', list(snap.mb), 1)
    timed(rec, 'minibatch_s', start)
    rec['cache_mb'] = cache(adapter, cm)
    pm = decide.restrict(snap.parent_val, snap.mb)
    d = decide.paired_delta(cm, pm, snap.mb, rerun_var=snap.rerun_var, families=snap.families)
    full_n = len(snap.mb) + len(snap.rest)
    known_infra = all((cm.error_classes.get(t) == 'infra_exhausted'
                       or pm.error_classes.get(t) == 'infra_exhausted') for t in snap.mb)
    if d['n'] == 0 and known_infra and d['dropped'] <= MAX_DROPPED_FRACTION * full_n:
        # No minibatch effect is inferred. The full validation result will decide
        # whether enough comparable tasks remain under the original denominator.
        rec['minibatch_note'] = 'no observable minibatch: known exhausted infrastructure; continue to full validation'
    else:
        check_dropped(d, full_n, 'minibatch')
    # §4.2: the minibatch only records its safeguards; it never eliminates on them.
    sg, _ = safeguards(adapter, rec, cm, pm, snap)
    rec.update(delta_mb=d['delta'], se_mb=d['se'], n_mb=d['n'], safeguards_mb=sg)
    if d['se'] is not None and d['delta'] < -snap.cfg.minibatch_kill * d['se']:
        return finish(rec, 'minibatch_fail', 'delta_mb below -kill*SE_mb')
    rec['stage'] = 'minibatch_pass'
    return rec


def validation(adapter, snap, rec, card, candidate):
    """Stage 2 (k=1 on the whole validation set) and, when it cannot change the outcome, stage 3 without review.

    Review all candidates that can qualify with mechanism evidence, including
    those above c·SE: their review can still reveal gaming and require a flag.
    """
    start = time.monotonic()
    cm, _ = adapter.evaluate_subset(candidate, None, 'mb', list(snap.mb), 1)
    cr, _ = adapter.evaluate_subset(candidate, None, 'rest', list(snap.rest), 1)
    timed(rec, 'validation_s', start)
    rec['cache_rest'] = cache(adapter, cr)
    cv = decide.combine(cm, cr)
    ids = list(snap.mb) + list(snap.rest)
    d = decide.paired_delta(cv, snap.parent_val, ids, rerun_var=snap.rerun_var, families=snap.families)
    check_dropped(d, len(ids), 'validation')
    sg, infra = safeguards(adapter, rec, cv, snap.parent_val, snap)
    rec.update(delta_val=d['delta'], se_val=d['se'], se_own=d['se_own'], se_floor=d['se_floor'],
               se_method=d['se_method'], n_val=d['n'], dropped_val=d['dropped'], own_failures=d['own_failures'],
               safeguards=sg, stage='validated')
    if infra is not None:
        rec['infrastructure'] = infra
    kw = dict(c=snap.cfg.c, c_mech=snap.cfg.c_mech)
    confirmed = decide.qualification(d['delta'], d['se'], 'confirmed', sg, **kw)
    unconfirmed = decide.qualification(d['delta'], d['se'], 'unconfirmed', sg, **kw)
    review_band = (d['delta'] is not None and d['se'] is not None
                   and d['delta'] >= snap.cfg.c_mech * d['se'])
    if review_band and snap.cfg.review:
        return rec, cv                      # stage 3 can relax the threshold or flag gaming
    # The local check reuses measured trajectories; it costs no new evaluation or reviewer.
    start = time.monotonic()
    mechanical = mechanism.mechanical(adapter, card.predicate, cv, snap.parent_val, snap.seed_aa)
    timed(rec, 'mechanism_s', start)
    reason = ('cannot qualify even with a confirmed mechanism' if not confirmed['qualified']
              else 'qualifies without the relaxation' if unconfirmed['qualified'] else 'review disabled')
    tier_, flags = decide.tier(mechanical.get('status'), 'none')
    return qualify(rec, snap.cfg, dict(mechanical=mechanical, reviewer=dict(conclusion='none', reason=reason),
                                       tier=tier_, flags=flags)), cv


def qualify(rec, cfg, mech):
    q = decide.qualification(rec['delta_val'], rec['se_val'], mech['tier'], rec['safeguards'],
                             c=cfg.c, c_mech=cfg.c_mech)
    flags = sorted(set(mech.get('flags') or []) | set(q.get('flags') or []))
    return finish(rec, 'measured', None if q['qualified'] else ','.join(q['reasons']),
                  mechanism=mech, qualification=q, flags=flags)


def review(adapter, snap, rec, card, candidate, cv):
    start = time.monotonic()
    try:
        mech = dict(mechanical=mechanism.mechanical(adapter, card.predicate, cv, snap.parent_val, snap.seed_aa),
                    reviewer=dict(conclusion='none', reason='not reviewed'))
        if snap.cfg.review:
            key = f'review/{candidate.content_hash}'
            pairs = mechanism.sample_pairs(cv, snap.parent_val, snap.seed_aa, list(snap.mb) + list(snap.rest),
                                           key=key, n_pairs=snap.cfg.review_pairs, n_controls=snap.cfg.review_controls)
            verdicts, error = mechanism.review(adapter, card, Diff(rec.get('diff_excerpt') or ''), candidate,
                                               pairs, key=key)
            conclusion = decide.reviewer_conclusion(verdicts, pairs, min_effect=snap.cfg.review_min_effect,
                                                    max_control_effect=snap.cfg.review_max_control_effect)
            if error:
                conclusion.update(conclusion='none', reason=f'review failed: {error}')
            mech['reviewer'] = dict(conclusion, pairs=mechanism.ledger_view(pairs), verdicts=verdicts)
        mech['tier'], mech['flags'] = decide.tier(mech['mechanical'].get('status'), mech['reviewer']['conclusion'])
        return qualify(rec, snap.cfg, mech)
    finally:
        timed(rec, 'mechanism_s', start)


def merge(adapter, snap, best, second, best_version, second_version):
    out = dict(of=[best['card_index'], second['card_index']], adopt=False)
    adapter.check_runtime()
    try:
        version = adapter.rebase(second_version, best_version)
    except EvaluationUnavailable:
        raise
    except Exception as exc:
        return dict(out, skipped=f'rebase: {exc}'), None, None
    if version is None:
        return dict(out, skipped='rebase conflict'), None, None
    adapter.check_runtime()
    mb = adapter.evaluate_subset(version, None, 'mb', list(snap.mb), 1)[0]
    rest = adapter.evaluate_subset(version, None, 'rest', list(snap.rest), 1)[0]
    val = decide.combine(mb, rest)
    d = decide.paired_delta(val, snap.parent_val, list(snap.mb) + list(snap.rest),
                           rerun_var=snap.rerun_var, families=snap.families)
    sg, _ = safeguards(adapter, out, val, snap.parent_val, snap)
    return dict(out, candidate=version.content_hash, delta_val=d['delta'], se_val=d['se'], n_val=d['n'],
                safeguards=sg, adopt=d['delta'] is not None and d['delta'] > best['delta_val'] and sg['ok']), version, val
