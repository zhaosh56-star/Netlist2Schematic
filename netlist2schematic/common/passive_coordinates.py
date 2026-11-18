"""Optional post-SA guest coordinates; logical cells remain the layout skeleton.

Offsets are local to the saved public axes. They may cross row/column midpoints:
actual body, fixed-exit and marker validation decides whether a guest fits.
This representation does not grant geometry or fixed-device exemptions.
"""
from math import isfinite

PASSIVE = frozenset(('resistor', 'capacitor', 'inductor', 'diode'))
SOURCES = frozenset(('voltage-source', 'current-source'))
LAYOUT_GUESTS = PASSIVE | SOURCES


def validate_passive_offsets(placement, comps):
    values = getattr(placement, 'passive_local_offsets', None)
    if values is None:
        return
    if not isinstance(values, (list, tuple)) or len(values) != len(comps):
        raise ValueError('Invalid passive local offset count')
    for comp, pair in zip(comps, values):
        if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                or any(type(v) not in (int, float) or not isfinite(v) for v in pair)):
            raise ValueError('Invalid passive local offset for ' + comp['id'])
        if any(pair) and comp['symbol'] not in LAYOUT_GUESTS:
            raise ValueError('Local offsets are only supported for passive devices and independent sources: ' + comp['id'])
