"""Soft stage limits inside the existing attachment time/count ledger.

No stage creates or resets a budget. A timed-out trial is discarded by its
existing caller. Only a timeout at this stage's earlier deadline can yield
to the old remainder; real shared exhaustion is never cleared.
"""
from math import floor

VERSION='attachment-stage-quotas-v1'
INITIAL_TIME_FRACTION=.60
INITIAL_CANDIDATE_FRACTION=.75
CONDUCTION_TIME_FRACTION=.25
CONDUCTION_MAX_CANDIDATES=16
BEFORE_TAIL_TIME_FRACTION=.65


class StageQuota:
    def __init__(self,budget,until,*,clock,name,time_fraction,candidate_fraction=1.,max_candidates=None):
        self.budget=budget;self.clock=clock;self.name=name
        self.started=clock();self.shared_until=until
        self.deadline=min(until,self.started+max(0.,until-self.started)*time_fraction)
        self.start_candidates=budget['candidates']
        remaining=max(0,budget['max_candidates']-self.start_candidates)
        allowance=floor(remaining*candidate_fraction)
        if max_candidates is not None:allowance=min(allowance,max_candidates)
        self.previous_ceiling=budget.get('stage_candidate_ceiling')
        self.ceiling=self.start_candidates+allowance
        if self.previous_ceiling is not None:self.ceiling=min(self.ceiling,self.previous_ceiling)
        self.allowance=max(0,self.ceiling-self.start_candidates)
        self.initially_exhausted=bool(budget.get('exhausted'))
        self.time_yield=False;self.record=None

    def __enter__(self):
        self.budget['stage_candidate_ceiling']=self.ceiling
        return self

    def __exit__(self,kind,value,traceback):
        if self.record is not None:return False
        now=self.clock();budget=self.budget
        shared_expired=now>=self.shared_until or budget['candidates']>=budget['max_candidates']
        self.time_yield=(not self.initially_exhausted and not shared_expired
            and self.deadline<self.shared_until and now>=self.deadline and kind is None)
        # Existing checkers signal timeout through exhausted=True. A nested
        # earlier deadline is a yield only when the shared deadline/count
        # still allow work. A premature/unknown checker failure stays hard.
        if self.time_yield and budget.get('exhausted'):budget['exhausted']=False
        if self.previous_ceiling is None:budget.pop('stage_candidate_ceiling',None)
        else:budget['stage_candidate_ceiling']=self.previous_ceiling
        status=('error' if kind else 'shared_budget' if shared_expired or budget.get('exhausted') else
                'time_quota' if self.time_yield else
                'candidate_quota' if budget['candidates']>=self.ceiling else 'completed')
        self.record=dict(version=VERSION,phase=self.name,status=status,
            seconds_allowance=max(0.,self.deadline-self.started),
            elapsed_seconds=max(0.,now-self.started),candidate_allowance=self.allowance,
            start_candidates=self.start_candidates,end_candidates=budget['candidates'],
            candidates_used=budget['candidates']-self.start_candidates,
            unused_shared_seconds=max(0.,self.shared_until-now),
            shared_max_candidates=budget['max_candidates'],shared_seconds_limit=budget['seconds_limit'],
            shared_budget_reset=False,soft_timeout_yielded=self.time_yield)
        return False


def initialization_quota(budget,until,clock):
    return StageQuota(budget,until,clock=clock,name='initialization',
        time_fraction=INITIAL_TIME_FRACTION,candidate_fraction=INITIAL_CANDIDATE_FRACTION)


def conduction_quota(budget,until,clock):
    return StageQuota(budget,until,clock=clock,name='joint_conduction',
        time_fraction=CONDUCTION_TIME_FRACTION,max_candidates=CONDUCTION_MAX_CANDIDATES)


def before_tail_quota(budget,until,clock):
    return StageQuota(budget,until,clock=clock,name='before_reserved_tail_finish',
        time_fraction=BEFORE_TAIL_TIME_FRACTION)
