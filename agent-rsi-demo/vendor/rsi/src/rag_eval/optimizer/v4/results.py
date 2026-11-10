"""Validated per-case v4 measurements without weakening formal v2 admission.

An infrastructure retry can replace only an unresolved external failure of its
logical measurement. It is never another independent sample of healthy tasks.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from ..adapter import EvalResult, EvaluationUnavailable

POLICY = 'v41-logical-measurements-1'


def compatible(identities):
    from rag_eval.reasoning_protocol import ensure_comparable_reasoning_protocols
    if not identities:
        return
    anchor = identities[0]
    for identity in identities[1:]:
        for key in ('bundle_id', 'suite', 'profile', 'metric', 'scorer'):
            if identity[key] != anchor[key]:
                raise EvaluationUnavailable(f'v4 measurement has incompatible {key}')
    try:
        ensure_comparable_reasoning_protocols([identity['method'] for identity in identities])
    except ValueError as exc:
        raise EvaluationUnavailable(f'v4 measurement has incompatible reasoning protocols: {exc}') from exc


def failure_kind(output):
    """An exact external-error allowlist with saved producer phase evidence."""
    status = output.get('status')
    record = output.get('product_record') or {}
    phase = record.get('failed_phase')
    kind = record.get('error_type')
    if status == 'error':
        if kind == 'partition_deadline' and phase in ('asking', 'not_started'):
            return 'execution_incomplete'
        if kind == 'PreparationInfrastructureError' and phase in ('preparation', 'preparing', 'importing'):
            return 'infra_exhausted'
        if kind in ('APIConnectionError', 'ConnectError') and phase in ('asking', 'preparation', 'preparing'):
            return 'infra_exhausted'
    if status in ('no_answer', 'clarification') or (record.get('response') or {}).get('llm_mode') == 'synthesis_failed':
        return 'candidate_unscored'
    return 'scoring_unavailable' if status == 'success' else 'unknown_failure'


def _deadline_failure(entry):
    failure = entry.failure or {}
    if (entry.execution_state in ('failed', 'timeout') and
            (failure.get('error_type'), failure.get('failed_phase')) in (
                ('execution_deadline', 'not_started'), ('partition_deadline_kill', 'deadline'))):
        return {key: failure[key] for key in ('error_type', 'failed_phase')}
    return None


def read_partition(views, entry, expected):
    """Validate the whole original artifact, then select its requested case rows."""
    from rag_eval.flip_report import _read_side
    from rag_eval.identity import fingerprint
    from rag_eval.benchmark_comparison import scorer_comparison, validate_scored_submission
    from rag_eval.workbench.run_views import PRIMARY_METRIC
    views._assert_unsealed(entry.id)
    if entry.execution_state not in ('finished', 'failed', 'timeout'):
        raise EvaluationUnavailable(f'v4 partition {entry.id} is not settled')
    if entry.scores is None or not Path(entry.scores).exists():
        failure = _deadline_failure(entry)
        if failure:
            # A producer-recorded deadline is an unresolved task, never a score.
            # Other missing/corrupt artifacts must still fail validation.
            return dict(identities=[], rows={task: dict(score=None, failure_kind='execution_incomplete',
                output=dict(status='missing', product_record=failure),
                trajectory=dict(run_id=entry.id, run_dir=str(entry.run_dir))) for task in expected})
        raise EvaluationUnavailable(f'v4 partition {entry.id} has no official scorer artifact')
    try:
        side, artifacts = _read_side(entry.run_dir, entry.scores, None)
        if artifacts is None or side.get('reason'):
            raise ValueError(side.get('reason') or 'missing bound artifacts')
        bundle = views._read_context.bundle_for_run(entry.run_dir)
        if side['_manifest'].get('identity', {}).get('source') != bundle['manifest']:
            raise ValueError('Run manifest uses a different frozen bundle')
        submission, scores = artifacts['submission'], artifacts['scores']
        validate_scored_submission(bundle, submission, scores, saved_prepared=artifacts.get('prepared'))
        if set(submission['case_ids']) != set(expected) or len(expected) != len(set(expected)):
            raise ValueError('Official case scope differs from the execution partition')
        metric = PRIMARY_METRIC.get(scores['suite'])
        if not metric or metric not in scores['metrics']:
            raise ValueError('Official primary metric is missing')
        scorer, _ = scorer_comparison(scores)
        identity = dict(bundle_id=scores['bundle_id'], suite=scores['suite'], profile=scores['profile'],
                        metric=metric, scorer=scorer, method=submission['method'])
        output_path = Path(entry.run_dir) / 'outputs.jsonl'
        output_text = '' if not output_path.exists() and _deadline_failure(entry) else output_path.read_text()
        outputs = {row['case_id']: row for row in (json.loads(line) for line in output_text.splitlines())}
        rows = {}
        for row in scores['per_case']:
            task = row['case_id']
            output = outputs.get(task) or dict(status='missing')
            value = row['metrics'].get(metric)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError('Nonfinite primary score')
            # Keep only failure evidence in the in-memory projection. Full product
            # traces remain in their immutable run artifacts; their hash is frozen.
            record = output.get('product_record') or {}
            evidence = {key: record[key] for key in ('error_type', 'error_message', 'failed_phase', 'reason')
                        if key in record}
            if (record.get('response') or {}).get('llm_mode') is not None:
                evidence['response'] = dict(llm_mode=record['response']['llm_mode'])
            observed_failure = failure_kind(output)
            kind = observed_failure if value is None else None
            if observed_failure == 'execution_incomplete' or (task not in outputs and _deadline_failure(entry)):
                kind = 'execution_incomplete'
                if task not in outputs:
                    evidence = _deadline_failure(entry)
                # ALCE scores empty/error answers numerically. Retain that
                # official artifact but do not treat an execution cutoff as a
                # model answer or freeze its placeholder score as a healthy row.
                evidence = dict(evidence, official_primary_score=value)
                value = None
            rows[task] = dict(score=value, failure_kind=kind,
                              output=dict(status=output.get('status'), product_record=evidence), trajectory=dict(run_id=entry.id, run_dir=str(entry.run_dir),
                              scores_id=scores['scores_id'], submission_id=scores['submission_id'],
                              output_identity=fingerprint(output)))
        return dict(rows=rows, identities=[identity])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise EvaluationUnavailable(f'v4 partition {entry.id}: {exc}') from exc


def read_execution(views, execution_id, expected):
    """Original partitions plus registered, explicitly replacing in-attempt retries."""
    rows, identities = {}, []
    entries = views.execution_runs(execution_id)
    for entry in entries:
        part = read_partition(views, entry, list(entry.case_ids))
        part_rows = part['rows']
        identities.extend(part['identities'])
        if entry.retries_replace:
            for run_id, selected in entry.retries:
                retry_entry = views._entry(run_id)
                retry = read_partition(views, retry_entry, list(retry_entry.case_ids))
                if not set(selected) <= set(part_rows) or not set(selected) <= set(retry['rows']):
                    raise EvaluationUnavailable('Registered retry selection differs from its partition')
                identities.extend(retry['identities'])
                for task in selected:
                    part_rows[task] = retry['rows'][task]
        if set(rows) & set(part_rows):
            raise EvaluationUnavailable('Duplicate execution partition case binding')
        for task, row in part_rows.items():
            row['trajectory']['execution_id'] = execution_id
            rows[task] = row
    if set(rows) != set(expected):
        raise EvaluationUnavailable('Execution partitions differ from the requested task scope')
    compatible(identities)
    return dict(rows=rows, identities=identities)


def resolve_slot(projections):
    """Keep first resolution; later retry executions replace only proven infra gaps."""
    rows, identities = {}, []
    for projection in projections:
        identities.extend(projection['identities'])
        for task, row in projection['rows'].items():
            prior = rows.get(task)
            if prior is None or prior['failure_kind'] in ('infra_exhausted', 'execution_incomplete'):
                rows[task] = row
    compatible(identities)
    return dict(rows=rows, identities=identities)


def result_from_slots(slots, candidate_id, scope):
    rows_by_task, attempts, identities = {}, [], []
    for root, projection in slots:
        scores = {}
        identities.extend(projection['identities'])
        for task, row in projection['rows'].items():
            if row['score'] is None and row['failure_kind'] != 'infra_exhausted':
                raise EvaluationUnavailable(f'v4 {task}: {row["failure_kind"]} (no official primary score)')
            row = dict(row, trajectory=dict(row['trajectory'], logical_attempt=root))
            rows_by_task.setdefault(task, []).append(row)
            if row['score'] is not None:
                scores[task] = row['score']
        attempts.append(scores)
    compatible(identities)
    scores, errors, trajectories, evidence = {}, {}, {}, {}
    for task, rows in rows_by_task.items():
        values = [row['score'] for row in rows if row['score'] is not None]
        if values and len(values) == len(rows):
            scores[task] = sum(values) / len(values)
        errors[task] = 'infra_exhausted' if any(row['score'] is None for row in rows) else None
        trajectories[task] = [row['trajectory'] for row in rows]
        evidence[task] = [dict(kind=row['failure_kind'], status=row['output'].get('status'),
                               product_record=row['output'].get('product_record')) for row in rows if row['failure_kind']]
    return EvalResult(scores, trajectories, {}, errors, attempt_scores=attempts,
                      context=dict(candidate_id=candidate_id, scopes={t: scope for t in rows_by_task},
                                   v41_policy=POLICY, score_identities=identities, failures=evidence))
