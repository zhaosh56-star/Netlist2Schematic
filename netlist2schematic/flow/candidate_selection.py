"""Choose a verified drawing by bends, crossings, then signal wirelength.

Layout/SA quality scores remain useful diagnostics and feedback estimates.
They do not break ties or exclude an otherwise verified drawing here.
"""
from __future__ import annotations


POLICY = 'bends-crossings-wirelength-v1'
RANKING = ('bends', 'crossings', 'wirelength')


def routing_key(result):
    metrics = result[6][1]
    return (*(metrics[name] for name in RANKING), str(result[8]))


def select_result(results, *, stage='validated_routes'):
    """Use the actual final-route metrics, with a stable ID for exact ties."""
    results = list(results)
    eligible = [row for row in results if row[6][2].get('valid') is True]
    if not eligible:
        raise ValueError('Candidate selection requires a verified complete drawing')
    ordered = sorted(eligible, key=routing_key)
    selected = ordered[0]
    return selected, {
        'policy': POLICY,
        'ranking': list(RANKING),
        'tie_breaker': 'candidate_id',
        'comparison_stage': stage,
        'selected_candidate': str(selected[8]),
        'selected_metrics': {name: selected[6][1][name] for name in RANKING},
        'candidates': [
            {'candidate_id': str(row[8]),
             'metrics': {name: row[6][1][name] for name in RANKING},
             'original_quality_score': row[7].get('score')}
            for row in ordered],
        'excluded_candidates': [str(row[8]) for row in results
                                if row[6][2].get('valid') is not True],
        'quality_score_used_for_selection': False,
        'layout_preferences_used_for_selection': False,
        'relative_wirelength_or_crossing_caps_applied': False,
    }
