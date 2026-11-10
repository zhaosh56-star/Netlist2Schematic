"""Small, provenance-aware feedback for the next diagnosis; no validation text."""
from __future__ import annotations

import hashlib
import re

# method-v4.1 §4.1: labels that count as "did not qualify" for the per-direction rule.
NOT_QUALIFIED = ('below_threshold', 'minibatch_fail', 'smoke_fail', 'safeguard_breached', 'remeasure_failed')
DIRECTION_LIMIT = 2            # this many non-qualifying attempts: change direction or state the difference
DIRECTION_CHARS = 48
# Labels SNAdapter's v4 history vocabulary does not know (rendered here).
V41_LABELS = ('remeasure_failed',)
IMPLEMENTATION_FAILURE_KINDS = ('timeout', 'empty_diff', 'workspace_error', 'malformed_report')

OUTCOMES = {
    'hypothesis_refuted': 'implementer reports the hypothesis did not hold in source/local checks',
    'already_satisfied': 'implementer reports the proposed behavior already exists',
    'hypothesis_inconclusive': 'local checks were inconclusive; the hypothesis is not disproved',
    'mechanism_changed_gain_insufficient': 'the mechanical behavior check changed as predicted, but gain was insufficient',
    'mechanism_not_observed': 'the mechanical behavior check did not show the predicted change',
    'mechanism_unknown': 'mechanism was not established; insufficient gain alone does not refute the hypothesis',
    'execution_unavailable': 'evaluation did not produce usable evidence; the hypothesis is untested',
    'implementation_failed': 'implementation did not finish; the hypothesis is untested',
    'implementation_timeout': 'implementation hit the per-card time limit; the hypothesis is untested',
    'implementation_empty_diff': 'the implementer left no change (empty diff); the hypothesis is untested',
    'implementation_workspace_error': 'the worktree or Agent failed; the hypothesis is untested',
    'implementation_malformed_report': 'the preflight report was missing or malformed; the hypothesis is untested',
    'remeasure_not_confirmed': 'qualified once, but a re-measurement did not confirm the gain (first measurement likely high)',
    'guardrail_breached': 'a pre-registered safeguard blocked adoption',
}


def feedback(rec: dict, label: str) -> dict:
    """Whitelist discrete validation conclusions and pre-measurement implementer reports only."""
    out = {}
    if label == 'preflight_skipped':
        report = rec.get('preflight') or {}
        reason = report.get('reason')
        out['outcome'] = {'refuted': 'hypothesis_refuted', 'already_satisfied': 'already_satisfied',
                          'inconclusive': 'hypothesis_inconclusive'}.get(reason, 'hypothesis_inconclusive')
        out['implementation_feedback'] = {
            'source': 'implementer_report_before_measurement',
            'summary': str(report.get('summary') or '')[:1200],
            'checks': [{k: str(c.get(k) or '')[:600] for k in ('kind', 'target', 'observation')}
                       for c in (report.get('checks') or [])[:4] if isinstance(c, dict)],
        }
    elif label == 'run_failed':
        out['outcome'] = 'execution_unavailable'
    elif label == 'implementation_failed':
        kind = rec.get('failure_kind')
        out['outcome'] = f'implementation_{kind}' if kind in IMPLEMENTATION_FAILURE_KINDS else 'implementation_failed'
    elif label == 'remeasure_failed':
        out['outcome'] = 'remeasure_not_confirmed'
    elif label == 'safeguard_breached':
        out['outcome'] = 'guardrail_breached'
    elif label in ('below_threshold', 'smoke_fail', 'minibatch_fail'):
        status = (((rec.get('mechanism') or {}).get('mechanical') or {}).get('status'))
        if status == 'changed':
            out['outcome'] = 'mechanism_changed_gain_insufficient'
        elif status == 'unchanged' or (rec.get('smoke') or {}).get('verdict') == 'fail':
            out['outcome'] = 'mechanism_not_observed'
        else:
            out['outcome'] = 'mechanism_unknown'
    return out


def feedback_text(entry: dict) -> str:
    text = OUTCOMES.get(entry.get('outcome'), '')
    # Only source/local-check reports emitted before any measurement can carry free text.
    report = entry.get('implementation_feedback') or {}
    if entry.get('label') == 'preflight_skipped' and report.get('source') == 'implementer_report_before_measurement':
        summary = ' '.join(str(report.get('summary') or '').split())[:500]
        checks = [' '.join(f"{c.get('kind')}: {c.get('target')}: {c.get('observation')}".split())[:300]
                  for c in report.get('checks', [])[:2] if c.get('kind') in ('source', 'test', 'probe')]
        text += '; implementer-reported: ' + summary
        if checks:
            text += '; checks: ' + ' | '.join(checks)
    return text


def analysis_history(adapter, history: list[dict], budget: int, parent_id: str):
    """Keep the complete adopted inventory in its own immutable file; trim only old trials."""
    current = [e for e in history if e.get('label') == 'current_change']
    recent = [e for e in history if e.get('label') != 'current_change']
    # Parent identifiers are not filesystem paths, even for non-SN adapters.
    tag = hashlib.sha256(parent_id.encode()).hexdigest()[:20]
    lines = ['# Changes included in this parent',
             'This inventory follows the adopted lineage, not a history of rejected proposals.',
             'Descriptions record the intended changes; acceptance does not prove every claimed mechanism.', '']
    for entry in current:
        title = ' '.join(str(entry.get('title') or '').split())[:160]
        change = ' '.join(str(entry.get('change') or '').split())[:600]
        lines.append(f'- {title}: {change}')
    if not current:
        lines.append('No changes have been adopted in this run yet.')
    path, digest, _ = adapter._write_evidence(f'adopted-{tag}.md', '\n'.join(lines) + '\n')
    pointer = f'Current adopted changes: read {path} (sha256 {digest}). This inventory is not trimmed.\n\n'
    summary = render_direction_summary(recent)
    text, shown, omitted = render_history(recent, max(0, budget - len(pointer) - len(summary)))
    return pointer + summary + text, shown, omitted


# ---------------------------------------------------------------------------
# Directions (method-v4.1 §4.1)
# ---------------------------------------------------------------------------

def normalise_direction(text: str) -> str:
    words = re.findall(r'[^\W_]+', str(text or '').casefold(), flags=re.UNICODE)
    return '-'.join(words)[:DIRECTION_CHARS].strip('-') or 'unnamed'


def card_direction(raw, title: str) -> tuple[str, bool]:
    """(direction, inferred): the analyst's tag, else a normalisation of the card title (marked inferred)."""
    if isinstance(raw, str) and raw.strip():
        return normalise_direction(raw), False
    return normalise_direction(' '.join(str(title or '').split()[:6])), True


def direction_summary(history: list[dict]) -> list[dict]:
    """Per direction: its attempts' labels (in order) and whether the change-direction rule applies."""
    rows: dict[str, dict] = {}
    for e in history:
        label = e.get('label')
        if label in (None, 'current_change'):
            continue
        if e.get('direction'):
            d, inferred = normalise_direction(e['direction']), bool(e.get('direction_inferred'))
        else:
            d, inferred = card_direction(None, e.get('title'))
        row = rows.setdefault(d, dict(direction=d, labels=[], inferred=True))
        row['labels'].append(label)
        row['inferred'] = row['inferred'] and inferred
    for row in rows.values():
        row['not_qualified'] = sum(label in NOT_QUALIFIED for label in row['labels'])
        row['change_required'] = row['not_qualified'] >= DIRECTION_LIMIT
    return list(rows.values())


def render_direction_summary(history: list[dict]) -> str:
    rows = direction_summary(history)
    if not rows:
        return ''
    lines = ['## History by direction',
             'Each card names a short `direction` (the same idea keeps the same direction). Outcomes per direction, '
             'oldest first:']
    for row in rows:
        mark = ' (inferred from titles)' if row['inferred'] else ''
        lines.append(f"- {row['direction']}{mark}: tried {len(row['labels'])}x: {', '.join(row['labels'])}")
    stuck = [r['direction'] for r in rows if r['change_required']]
    if stuck:
        lines.append(f"REQUIRED: the direction(s) {', '.join(stuck)} already have {DIRECTION_LIMIT} or more attempts "
                     "that did not qualify. Choose a different direction, or state in the card's mechanism the "
                     "essential difference from the earlier attempts of that direction.")
    return '\n'.join(lines) + '\n\n'


def _line(entry: dict):
    from ..sn_adapter import V4_HISTORY_LABELS, _history_line
    label = entry.get('label')
    if label in V41_LABELS:
        title = ' '.join(str(entry.get('title') or '(untitled)').split())[:160]
        line = f'- {title} -> {label}'
        text = feedback_text(entry)
        if text:
            line += '; ' + text
        if entry.get('earlier_parent') is True:
            line += "; tried on an earlier parent (that parent's change may already be in this one)"
        line += '\n'
    elif label in V4_HISTORY_LABELS:
        line = _history_line(entry, 'v4')
    else:
        return None
    if not line:
        return None
    extra = ''
    if entry.get('direction'):
        extra += f"; direction: {normalise_direction(entry['direction'])}"
    if entry.get('needs_human_review') is True:
        extra += '; flagged for human review (possible score gaming reported; not a rejection)'
    return line[:-1] + extra + '\n'


def render_history(history: list[dict], budget_chars: int) -> tuple[str, int, int]:
    """SNAdapter's v4 history section plus v4.1 labels and directions (oldest entries trimmed first)."""
    lines = [line for line in (_line(e) for e in history) if line is not None]
    total = len(lines)

    def section(kept):
        note = f'[{total - len(kept)} older entr(ies) omitted]\n' if len(kept) < total else ''
        return ('Hypotheses already tried in this run, on this parent and on earlier ones (outcome labels only, '
                'oldest first). Do not duplicate adopted or in-progress changes. Refuted hypotheses need new evidence; '
                'implementation/run failures and inconclusive checks do not refute a direction. '
                'A qualified_not_adopted card may be reconsidered on this parent:\n' + note + ''.join(kept) + '\n')
    while lines and len(section(lines)) > budget_chars:
        lines.pop(0)
    if not lines:
        return '', 0, total
    return section(lines), len(lines), total - len(lines)
