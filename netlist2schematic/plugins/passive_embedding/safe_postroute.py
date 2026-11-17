"""One bounded passive-only route trial after a complete legal incumbent.

The original placement and wires are a transaction boundary. Rejection,
exhaustion and routing failure always return that exact incumbent.
"""
from copy import deepcopy
from math import isfinite
from time import perf_counter

from netlist2schematic.plugins.passive_embedding.passive_embedding import build_plan,measure,refine,rank
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.sa_routing import final_route,validate_final_routes,RoutingValidationError

VERSION='safe-postroute-passives-v1'
EPS=1e-8


def nonworse(before,after):
    return all(after[k]<=before[k]+EPS for k in ('wirelength','crossings','bends'))


def optimize(par,pl,comps,nets,index,metadata,routed,options=None,*,seconds=5.,postprocess=None):
    if type(seconds) not in (int,float) or not isfinite(seconds) or seconds<0:
        raise ValueError('passive postroute seconds must be finite and non-negative')
    started=perf_counter();deadline=started+seconds
    plan=build_plan(par,comps,nets);before=measure(pl,comps,index,plan)
    report=dict(version=VERSION,policy='safe',plan=deepcopy(plan),applied=False,before=before,current=before,
        before_metrics=deepcopy(routed[1]),after_metrics=deepcopy(routed[1]),route_attempts=0,
        seconds_limit=seconds,actions=[],trials=0,protected_active_world_coordinates=True)
    def finish(reason,placement=pl,data=metadata,result=routed):
        output=deepcopy(data)
        report.update(stop_reason=reason,elapsed_seconds=round(perf_counter()-started,6))
        output['passive_embedding']=deepcopy(report)
        return placement,output,result
    if not plan['groups']:return finish('no_passive_groups')
    if seconds==0:return finish('budget_exhausted')
    try:
        validate_final_routes(par,nets,pl,index,list(routed[0]),options=options,deadline=deadline)
    except RoutingFailure as exc:
        if exc.diagnostics.get('reason')!='quality_time_budget':raise
        return finish('budget_before_proposal')
    try:
        trial,data=refine(par,pl,comps,nets,index,metadata,plan,options,
            seconds=min(1.,max(0,deadline-perf_counter())),deadline=deadline,max_trials=128)
        proposal=data['passive_embedding'];report['proposal']=deepcopy(proposal)
        report.update(trials=proposal['trials'],proposal_applied=proposal['applied'])
        if not proposal['applied']:return finish('no_legal_proposal')
        if perf_counter()>=deadline:return finish('budget_after_proposal')
        report['route_attempts']=1
        candidate=final_route(par,nets,trial,index,options=options,deadline=deadline)
        if postprocess is not None:
            data,candidate=postprocess(data,trial,candidate,max(0.,deadline-perf_counter()))
        if perf_counter()>=deadline:return finish('budget_after_proposed_route')
        report['proposed_metrics']=deepcopy(candidate[1])
        if not nonworse(routed[1],candidate[1]):return finish('actual_route_regression')
        current=measure(trial,comps,index,plan)
        if rank(current)>=rank(before):return finish('no_organization_gain')
        report.update(applied=True,current=current,after_metrics=deepcopy(candidate[1]),actions=deepcopy(proposal['actions']))
        return finish('accepted',trial,data,candidate)
    except (RoutingFailure,RoutingValidationError) as exc:
        report['diagnostics']=deepcopy(exc.diagnostics)
        return finish('proposal_route_failed')
