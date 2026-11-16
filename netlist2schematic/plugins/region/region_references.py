"""Relative access order for two proven comparisons sharing a reference.

Reference legs belong to their original local comparisons.  Their common
control is an interface, not an extra hard block or an ownership union.
"""
from __future__ import annotations
from math import isfinite

VERSION = 'paired-reference-access-v1'


def reference_lane_hints(region):
    """Normalized role positions; the ordinary lane allocator owns the grid."""
    hints = {}
    for relation in region.get('reference_access', ()):
        branches = relation.get('comparisons', ())
        if len(branches) != 2:
            continue
        names = [b.get(role) for b in branches for role in ('sensor', 'reference', 'feed')]
        if None in names or len(set(names)) != 6:
            continue
        for branch, values in zip(branches, ((-1, -1/3, -2/3), (1, 1/3, 2/3))):
            for role, value in zip(('sensor', 'reference', 'feed'), values):
                hints[branch[role]] = value
    return hints


def measure_reference_access(pl, index, regions):
    """Use real control points; missing/ambiguous relations never score zero."""
    records, missing = [], []
    for region in regions:
        for relation in region.get('reference_access', ()):
            try:
                branches = relation['comparisons']
                if len(branches) != 2:
                    raise ValueError('two_comparisons_required')
                names = [b[role] for b in branches for role in ('sensor', 'reference', 'feed')]
                if len(set(names)) != 6:
                    raise ValueError('overlapping_comparison_roles')
                if any(name not in index for name in names):
                    raise ValueError('missing_comparison_member')
                pin = relation.get('control_pin', 'G')
                sensors = [tuple(pl.pin_world(index[b['sensor']], pin)) for b in branches]
                references = [tuple(pl.pin_world(index[b['reference']], pin)) for b in branches]
                if any(not isfinite(value) for point in sensors+references for value in point):
                    raise ValueError('nonfinite_control_point')
                lo, hi = sorted(p[0] for p in sensors)
                errors = sum(not (lo+1e-8 < p[0] < hi-1e-8) for p in references)
                records.append(dict(region=region['id'], references=[b['reference'] for b in branches],
                    sensors=[b['sensor'] for b in branches], net=relation['net'],
                    inside_errors=errors, reference_span=abs(references[0][0]-references[1][0]),
                    same_height=max(p[1] for p in sensors+references)-min(p[1] for p in sensors+references)<1e-8))
            except (KeyError, ValueError, IndexError, TypeError, AttributeError) as exc:
                missing.append(dict(region=region.get('id'), reason=str(exc)))
    return dict(version=VERSION, relation_count=len(records)+len(missing),
        measurable=bool(records) and not missing, records=records, missing=missing,
        inside_errors=sum(r['inside_errors'] for r in records) if records and not missing else None)
