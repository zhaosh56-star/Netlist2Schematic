"""Web-only candidate orchestration; call the independent engine's public APIs.

The optional sidecar reads existing RoutingFailure checkpoints. It neither
patches engine functions nor changes SA, A* costs, spacing, or geometry rules.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
from time import perf_counter


def run(args):
    adapter_repository=Path(__file__).resolve().parents[2]
    sys.path.insert(0,str(adapter_repository))
    sys.path.insert(0,str(args.engine_root.resolve()))
    from integrations.auto_canvas_huyangc.drafts import make_draft,write_atomic,choose_payloads,selection_key
    from integrations.auto_canvas_huyangc.engine_export import export_context
    from netlist2schematic.common.sa_common import configure_resources,load_local,resource_root
    from netlist2schematic.flow import run_flow as flow
    from integrations.auto_canvas_huyangc.candidate_schedule import run_candidates,POLICY
    from netlist2schematic.routing.routing_rules import RoutingOptions
    from netlist2schematic.flow.aesthetic_flow import _clean_primary_results
    from netlist2schematic.plugins.organization.branch_layout import select_results

    started=perf_counter();deadline=started+args.seconds
    profile=json.loads(args.profile.read_text(encoding='utf8'))
    source=args.engine_root/'layout-input.cir'
    source_digest=sha256(source.read_bytes()).hexdigest()
    args.out_dir.mkdir(parents=True,exist_ok=True)
    configure_resources(args.engine_root/'analog-canvas-main')
    par=load_local('place_and_route','place-and-route.py')
    options=RoutingOptions();capture_stages=[];complete=[]
    budget_policy=getattr(args,'layout_budget','legacy')

    def check_source():
        if sha256(source.read_bytes()).hexdigest()!=source_digest:
            raise ValueError('Algorithm input changed while preparing an editable result')

    def publish(payload):
        check_source()
        payload.update(source_hash=profile['source']['sha256'],sidecar_version='web-candidates-2-3-v2',
                       scheduling_policy=POLICY)
        path=args.out_dir/('candidate-'+str(payload['selected_candidate'])+'-wire-plan.json')
        if path.is_file():
            previous=json.loads(path.read_text(encoding='utf8'))
            if selection_key(previous)<=selection_key(payload):return
        write_atomic(path,payload)

    def capture(rows,identifier,*,deadline):
        rows=deepcopy(rows)
        rows=[r for r in rows if r.get('context') is not None and str(r.get('candidate_id'))==identifier]
        rows.sort(key=lambda r:-len((r.get('checkpoint') or {}).get('completed',{})))
        for row in rows[:4]:
            if perf_counter()>=deadline:break
            try:
                payload=make_draft(par,row['context'],row.get('checkpoint'),profile,row.get('options') or options,
                    identifier,deadline=min(deadline,perf_counter()+2))
                publish(payload)
            except (ValueError,KeyError) as error:
                capture_stages.append(dict(candidate_id=identifier,stage='draft_checkpoint_rejected',message=str(error)))

    def normal(identifier,seconds):
        return flow._normal_result(source,par,'layered','structured',None,args.iterations,args.seed,
            seconds,identifier,options,passive_layout='flexible',port_preflight='repair',
            column_layout='auto',passive_skeleton='auto',branch_layout='auto',
            simplify_policy='off',capture_failed_candidate=True,
            **({'layout_budget':budget_policy} if budget_policy!='legacy' else {}))

    def completed(result,identifier):
        placement,comps,nets,index,metadata,routed=result
        payload=export_context(par,placement,comps,nets,index,*routed,profile,options,metadata)
        payload['validation']=dict(payload['validation'],geometry_valid=True,complete=True)
        payload['result_kind']='complete';publish(payload)
        quality=deepcopy(metadata['quality']['details'])
        key=(quality['score'],routed[1]['crossings'],routed[1]['wirelength'],identifier)
        complete.append(deepcopy((key,*result[:5],routed,quality,identifier)))

    _,schedule=run_candidates(normal,seconds=max(0.,deadline-perf_counter()),
        check_source=check_source,on_failure=capture,on_result=completed,
        **({'layout_budget':budget_policy} if budget_policy!='legacy' else {}))
    records=schedule['candidates'];stages=[*schedule['stages'],*capture_stages]

    # Use the engine's unchanged, shared five-second cleanup and final routing
    # priorities for complete candidates. Draft scores never compete with them.
    selection=None;selected_path=None
    if complete and perf_counter()<deadline-1:
        complete,cleanup=_clean_primary_results(source,None,par,complete,records,options,'safe',5,'strict',
                                              budget_limit=max(0.,deadline-perf_counter()-1))
        selected,selection=select_results(complete)
        _,placement,comps,nets,index,metadata,routed,quality,identifier=selected
        payload=export_context(par,placement,comps,nets,index,*routed,profile,options,metadata)
        payload.update(result_kind='complete',selected_candidate=identifier)
        payload['validation']=dict(payload['validation'],geometry_valid=True,complete=True)
        publish(payload)
        selected_path=args.out_dir/('candidate-'+str(identifier)+'-wire-plan.json')
        # Normal, loadable algorithm artifacts retain their original semantics.
        if perf_counter()<deadline-1:
            data={**metadata,'format_version':1,'source_netlist':str(source),'source_cell':None,
                'source_sha256':source_digest,'analog_canvas_root':str(resource_root()),'mode':'layered',
                'seed':args.seed,'iterations':args.iterations,'supply_marker_policy':par.CURRENT_POLICY,
                'placement':flow._saved_positions(placement,comps)}
            flow._finish(data,par,placement,comps,nets,index,'layout-input',args.out_dir,
                protected_paths={source},options=options,routed_result=routed,simplify_routes='safe',
                simplify_seconds=5,formal_output='off')
    chosen=choose_payloads(args.out_dir,profile['source']['sha256'])
    if chosen is None:
        reasons=[]
        for record in records:
            for stage in record.get('stages',[]):
                for candidate in stage.get('diagnostics',{}).get('candidates',[]):
                    packing=candidate.get('passive_skeleton')
                    if packing is not None and not packing.get('applied'):
                        usage=packing.get('normal_packing_budget',{})
                        reasons.append(f"candidate {record['candidate_id']}: attachment placement "
                            f"{packing.get('stop_reason','incomplete')}"
                            +(f" ({usage['stop_reason']})" if usage.get('stop_reason') else ''))
        message='No complete safe all-device layout was available'
        if reasons:message+='; '+'; '.join(reasons)
        write_atomic(args.out_dir/'editable-engine-report.json',dict(status='failed',candidate_order=['2','3'],
            candidates=records,stages=stages,scheduling=schedule,reason='no_safe_full_device_pose_or_routes',
            layout_budget_policy=budget_policy,message=message))
        raise ValueError(message)
    payload,path=chosen
    if selected_path is not None:
        path=selected_path;payload=json.loads(path.read_text(encoding='utf8'))
    write_atomic(args.out_dir/'editable-engine-report.json',dict(status=payload['result_kind'],
        candidate_order=['2','3'],selected_candidate=payload['selected_candidate'],selection=selection,
        candidates=records,stages=stages,scheduling=schedule,
        layout_budget_policy=budget_policy,
        seconds=round(perf_counter()-started,6),wire_plan=str(path),
        reference_candidate_used=False,bus_used=False))
    print(json.dumps(dict(status=payload['result_kind'],selected_candidate=payload['selected_candidate'],
                          seconds=round(perf_counter()-started,6))))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine-root',type=Path,required=True)
    parser.add_argument('--profile',type=Path,required=True)
    parser.add_argument('--out-dir',type=Path,required=True)
    parser.add_argument('--seconds',type=float,required=True)
    parser.add_argument('--iterations',type=int,default=20000)
    parser.add_argument('--seed',type=int,default=20261029)
    parser.add_argument('--layout-budget',choices=('legacy','adaptive'),default='legacy')
    args=parser.parse_args()
    run(args)


if __name__=='__main__':main()
