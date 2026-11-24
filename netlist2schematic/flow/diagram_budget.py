"""Outer diagram deadline; independent candidate stages allocate their own caps."""
from math import isfinite
from time import perf_counter

# Three possible 120+30+30+30s pipelines, a 60s bus cap, and 30s
# for parsing, shared cleanup and verified export. Per-stage limits are
# independent; this is only the outer CLI/process-tree protection.
DEFAULT_SECONDS = 720.0


def validate_seconds(seconds):
    if type(seconds) not in (int, float) or not isfinite(seconds) or seconds < 0:
        raise ValueError('time_seconds must be finite and non-negative (0 disables the total cap)')
    return float(seconds)


class DiagramBudget:
    def __init__(self, seconds=DEFAULT_SECONDS, *, clock=None):
        self.limit = validate_seconds(seconds)
        self.clock = clock or perf_counter
        self.started = self.clock()
        self.deadline = self.started + self.limit if self.limit else None
        self.stages = []

    def remaining(self, reserve=0.):
        if self.deadline is None:
            return float('inf')
        return max(0., self.deadline-self.clock()-reserve)

    def allocate(self, stage, requested, *, reserve=0.):
        seconds = min(requested, self.remaining(reserve))
        self.stages.append(dict(stage=stage, requested_seconds=requested,
                                allocated_seconds=seconds))
        return seconds

    def snapshot(self):
        return dict(policy='single_attempt_shared_deadline_v1',
                    limit_seconds=self.limit, stages=list(self.stages),
                    elapsed_seconds=round(self.clock()-self.started, 6))
