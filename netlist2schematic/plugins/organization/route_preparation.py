"""Repair actual static shape overlaps without breaking organized columns."""
from copy import deepcopy
from math import ceil
from time import perf_counter

from netlist2schematic.routing.routing_rules import build_scene,static_shape_conflicts
from netlist2schematic.plugins.preflight.placement_preflight import _static_faults
from netlist2schematic.layout.series_alignment import _fixed_ids

VERSION='branch-route-preparation-v2'


def _prepare(par,pl,comps,nets,index,metadata,options,*,deadline=None,seconds=.25,max_trials=16):
    started=perf_counter();stop=min(deadline,started+seconds) if deadline is not None else started+seconds
    data=deepcopy(metadata);report=dict(version=VERSION,applied=False,trials=0,actions=[],stop_reason='already_safe')
    current=pl
    if perf_counter()>=stop:
        report['stop_reason']='time_budget'
    else:
        scene=build_scene(par,nets,current,index,options,deadline=stop)
        faults=list(static_shape_conflicts(scene,stop))
        report['before_shape_conflicts']=len(faults)
        if faults and getattr(pl,'passive_module_guides',None):
            report['stop_reason']='inserted_module_geometry_locked'
        elif faults:
            fixed=_fixed_ids(pl,comps);cap=4*max(options.cross_clearance,options.center_spacing,options.grid_step)
            total=0.
            for _ in range(4):
                scene=build_scene(par,nets,current,index,options,deadline=stop)
                faults=list(static_shape_conflicts(scene,stop))
                if not faults:break
                bodies=dict(scene.body_rects);proposals=[]
                for identity,fault in faults:
                    if fault['kind']=='marker_body_halo':
                        number=fault['marker'];a=scene.marker_components[number];b=fault['component']
                        ra=scene.marker_rects[number][1];rb=bodies[b]
                    elif fault['kind']=='body_overlap':
                        a,b=fault['components'];ra,rb=bodies[a],bodies[b]
                    else:continue
                    for axis,dimension in ((0,1),(1,0)):
                        ca,cb=current.cell_of[a][dimension],current.cell_of[b][dimension]
                        if ca==cb:continue
                        boundary=max(ca,cb)
                        if any(current.cell_of[i][dimension]>=boundary for i in fixed):continue
                        lower,upper=(ra,rb) if ca<cb else (rb,ra)
                        amount=ceil((max(0.,lower[axis+2]-upper[axis])+options.grid_step)/options.grid_step)*options.grid_step
                        if total+amount>cap:continue
                        proposals.append((amount,axis,boundary,fault))
                incumbent=None
                for amount,axis,boundary,fault in sorted(proposals,key=lambda p:p[:3]):
                    if perf_counter()>=stop or report['trials']>=max_trials:break
                    report['trials']+=1;trial=deepcopy(current)
                    values=trial.column_x if axis==0 else trial.row_y
                    for k in range(boundary,len(values)):values[k]+=amount
                    if not trial.blocks_contiguous():continue
                    checked=_static_faults(par,trial,nets,index,options,stop)
                    remaining=list(static_shape_conflicts(build_scene(par,nets,trial,index,options,deadline=stop),stop))
                    # Intermediate shape progress stays private. Only a fully
                    # statically verified frame may ever replace the original.
                    if len(remaining)>=len(faults):continue
                    key=(len(remaining),checked['count'],amount,axis,boundary)
                    if incumbent is None or key<incumbent[0]:
                        incumbent=(key,trial,amount,dict(axis='x' if axis==0 else 'y',boundary=boundary,
                            added=amount,reason=fault['kind']),checked)
                if incumbent is None:break
                _,current,amount,action,checked=incumbent;total+=amount;report['actions'].append(action)
                if not checked['count'] and not checked['hard']:
                    report.update(applied=True,stop_reason='minimal_shape_spacing',added_spacing=total)
                    break
            if not report['applied']:
                current=pl;report['actions']=[];report['stop_reason']='no_complete_static_repair'
    report['elapsed_seconds']=round(perf_counter()-started,6)
    data['layout_branches']['route_preparation']=report
    from netlist2schematic.routing.regional_routing import VERSION as ROUTING_VERSION
    policy=dict(version=ROUTING_VERSION,active=report['applied'] or getattr(pl,'branch_routing',{}).get('active',False),ownership=deepcopy(data['layout_branches']['plan']['ownership']),
        source_plan=data['layout_branches']['plan']['signature'],
        phases='internal_then_cross_channel_then_shared_controls',search='local_then_global_safe_interfaces')
    current.branch_routing=deepcopy(policy)
    data['layout_branches']['routing_policy']=policy
    data['layout_grid']=dict(column_x=list(current.column_x),row_y=list(current.row_y))
    return current,data


def prepare(par,pl,comps,nets,index,metadata,options,*,deadline=None,seconds=.25,max_trials=16):
    from netlist2schematic.routing.orthogonal_router import RoutingFailure
    started=perf_counter()
    try:
        return _prepare(par,pl,comps,nets,index,metadata,options,deadline=deadline,seconds=seconds,max_trials=max_trials)
    except RoutingFailure as exc:
        if exc.diagnostics.get('reason')!='quality_time_budget' or (deadline is not None and perf_counter()>=deadline):raise
        # A local repair timeout cannot leak a partly stretched frame.
        trial,data=_prepare(par,pl,comps,nets,index,metadata,options,deadline=deadline,seconds=0,max_trials=0)
        data['layout_branches']['route_preparation'].update(stop_reason='time_budget',
            elapsed_seconds=round(perf_counter()-started,6))
        return trial,data
