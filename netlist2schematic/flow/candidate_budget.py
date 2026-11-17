"""Optional absolute deadlines that reserve time for later structured seeds."""
from math import isfinite

FRACTIONS = {'1': .70, '2': .85, '3': 1.0}
PRIMARY_FRACTIONS = {'2': .40, '3': .80, '1': 1.0}


def validate_policy(policy):
    if policy not in ('sequential', 'reserved'):
        raise ValueError('candidate_budget must be sequential or reserved')


def routing_deadline(started, seconds, identifier, *, policy='sequential', replay=False):
    validate_policy(policy)
    if any(type(x) not in (int, float) or not isfinite(x) for x in (started, seconds)) or seconds <= 0:
        raise ValueError('Candidate budget requires a finite start and positive duration')
    fraction = FRACTIONS.get(str(identifier), 1.) if policy == 'reserved' and not replay else 1.
    return started + seconds * fraction


def primary_deadline(started, seconds, identifier):
    """Bound both preferred candidates and leave time for failure-only seed 1.

    Cutoffs are cumulative: time saved by an earlier candidate is available to
    the next one. The total quality budget is never reset for the fallback.
    """
    routing_deadline(started, seconds, identifier)
    return started + seconds * PRIMARY_FRACTIONS[str(identifier)]
