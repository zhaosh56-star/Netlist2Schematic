"""Optional layout limits; no application-specific concepts or routing costs."""
from dataclasses import asdict, dataclass
from math import ceil, isfinite
from time import perf_counter


VERSION = 'attachment-layout-budget-v1'


@dataclass(frozen=True)
class LayoutBudget:
    mode: str = 'legacy'

    def __post_init__(self):
        if self.mode not in ('legacy', 'adaptive'):
            raise ValueError('layout_budget must be legacy or adaptive')

    @property
    def adaptive(self):
        return self.mode == 'adaptive'

    def for_plan(self, plan):
        groups = plan.get('groups', ())
        modules = len(groups)
        guests = len({name for group in groups for name in group['members']})
        complexity = modules + ceil(max(0, guests-modules)/3)
        tier = 0 if complexity <= 2 else 1 if complexity <= 5 else 2
        return dict(version=VERSION, policy=self.mode, modules=modules, guests=guests,
                    complexity=complexity,
                    normal_seconds=(3., 6., 10.)[tier] if modules else 0.,
                    normal_trials=(512, 1536, 3072)[tier] if modules else 0,
                    recovery_seconds=(10., 15., 20.)[tier] if modules else 0.,
                    recovery_trials=(2048, 4096, 8192)[tier] if modules else 0)

    def snapshot(self):
        return dict(version=VERSION, **asdict(self))


def layout_budget(value=None):
    return value if isinstance(value, LayoutBudget) else LayoutBudget(value or 'legacy')


class PackingBudget:
    """One parent-clipped time/count ledger, shared across packing attempts."""
    def __init__(self, seconds, trials, parent_deadline=None, *, clock=perf_counter):
        if type(seconds) not in (int, float) or not isfinite(seconds) or seconds < 0:
            raise ValueError('Invalid packing seconds')
        if type(trials) is not int or trials < 0:
            raise ValueError('Invalid packing trials')
        if parent_deadline is not None and (type(parent_deadline) not in (int, float)
                                           or not isfinite(parent_deadline)):
            raise ValueError('Invalid packing parent deadline')
        self.clock = clock
        self.started = clock()
        self.requested_seconds = seconds
        self.parent_deadline = parent_deadline
        self.deadline = min(self.started+seconds, parent_deadline) if parent_deadline is not None else self.started+seconds
        self.max_trials = trials
        self.used_trials = 0
        self.attempts = []

    def allocate(self, remaining_attempts=1):
        return (max(0., self.deadline-self.clock())/remaining_attempts,
                max(0, self.max_trials-self.used_trials)//remaining_attempts)

    def charge(self, report):
        count = report.get('trials', 0)
        if type(count) is not int or count < 0 or self.used_trials+count > self.max_trials:
            raise ValueError('Packing attempt exceeded its trial ledger')
        self.used_trials += count

    def stop_reason(self, report=None):
        now = self.clock()
        if self.parent_deadline is not None and now >= self.parent_deadline:
            return 'parent_deadline'
        if self.used_trials >= self.max_trials:
            return 'trial_budget'
        if now >= self.deadline:
            return 'time_budget'
        if report and report.get('applied'):
            return 'completed'
        if report and report.get('stop_reason') in ('no_legal_slot', 'no_safe_reservation',
                                                   'guest_is_fixed_core_member', 'final_static_conflict'):
            return 'search_exhausted'
        return 'attempts_exhausted'

    def snapshot(self, reason):
        return dict(requested_seconds=self.requested_seconds,
                    allocated_seconds=max(0., self.deadline-self.started),
                    elapsed_seconds=max(0., self.clock()-self.started),
                    max_trials=self.max_trials, trials=self.used_trials,
                    remaining_seconds=max(0., self.deadline-self.clock()),
                    remaining_trials=max(0, self.max_trials-self.used_trials),
                    stop_reason=reason, attempts=list(self.attempts), budget_reset=False)
