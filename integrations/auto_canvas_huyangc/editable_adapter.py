"""Budgeted web adapter: two primary candidates, then a safe editable draft."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys
from time import perf_counter

from .cli import ADAPTER_ROOT,find_node,write_json
from .drafts import choose_payloads
from .native_plan import build_plan
from .profile import compile_snapshot,stage_engine
from .runtime import run_bounded


def validate_layout_budget_capability(engine, mode):
    if mode not in ('legacy','adaptive'):
        raise ValueError('Unknown layout budget policy')
    if mode=='legacy':return
    import ast
    engine=Path(engine)
    required=(('netlist2schematic/flow/run_flow.py','_normal_result'),
              ('netlist2schematic/recovery/primary_recovery.py','repair'))
    try:
        supported=(engine/'netlist2schematic/common/layout_budget.py').is_file()
        for relative,name in required:
            tree=ast.parse((engine/relative).read_text(encoding='utf8'))
            function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name==name)
            supported=supported and 'layout_budget' in {a.arg for a in function.args.kwonlyargs}
    except (OSError,ValueError,StopIteration):
        supported=False
    if not supported:
        raise ValueError('The selected engine does not support layout_budget; select the adapt engine or use --layout-budget legacy')


def run_editable(args):
    started=perf_counter();limit=getattr(args,'total_seconds',None) or 300.
    deadline=started+limit
    source=args.input.resolve();destination=args.out_dir.resolve()
    target=args.project_root.resolve();engine=args.engine_root.resolve();runtime=args.runtime.resolve()
    budget_policy=getattr(args,'layout_budget','legacy')
    validate_layout_budget_capability(engine,budget_policy)
    if not source.is_file():raise ValueError('Input native project does not exist')
    if destination==source.parent or source.is_relative_to(destination):
        raise ValueError('Output must be separate from the input project directory')
    if destination.exists() and any(destination.iterdir()):raise ValueError('Choose an empty output directory')
    manifest=runtime/'runtime-manifest.json'
    if not manifest.is_file():raise ValueError('Prepare the isolated native runtime first')
    if json.loads(manifest.read_text(encoding='utf8'))['target_lock_sha256']!=sha256((target/'pnpm-lock.yaml').read_bytes()).hexdigest():
        raise ValueError('Target dependency lock changed; prepare a matching isolated runtime')
    node=find_node(target,args.node)
    digest=sha256(source.read_bytes()).hexdigest()
    private=destination/'private';logs=destination/'logs'
    private.mkdir(parents=True);logs.mkdir()
    report=dict(format='auto-canvas-native-adapter-v1',status='running',source=str(source),source_sha256=digest,
        target_root=str(target),engine_root=str(engine),output_dir=str(destination),steps=[],
        target_checkout_modified=False,core_source_modified=False,result_policy='editable',
        layout_budget_policy=budget_policy,
        budget=dict(total_seconds=limit,includes='snapshot_candidates_export_and_native_validation'))

    def remaining(reserve=0.):return max(0.,deadline-perf_counter()-reserve)

    def execute(stage,command,cap,*,reserve=0.,allow_timeout=False):
        seconds=min(cap,remaining(reserve))
        if seconds<=0:raise ValueError('Editable result total time budget exhausted before '+stage)
        print('Adapter: '+stage,flush=True);before=perf_counter()
        path=logs/(stage+'.log')
        with path.open('w',encoding='utf8') as stream:
            code=run_bounded(command,seconds=seconds,cwd=engine,stdout=stream,stderr=stream)
        report['steps'].append(dict(stage=stage,returncode=code,allocated_seconds=seconds,
            seconds=round(perf_counter()-before,6),log=str(path),command=command))
        if code and not(code==124 and allow_timeout):
            summary=destination/'engine-result/editable-engine-report.json'
            if stage=='core-flow' and summary.is_file():
                failure=json.loads(summary.read_text(encoding='utf8'))
                if failure.get('status')=='failed' and failure.get('message'):
                    raise ValueError(failure['message'])
            raise ValueError(f'{stage} failed (exit {code}); '+path.read_text(encoding='utf8',errors='replace')[-2200:])
        return code

    def native(operation,output,extra=()):
        command=[node,'--experimental-transform-types',str(ADAPTER_ROOT/'native.mjs'),operation,
            '--project-root',str(target),'--runtime',str(runtime),'--input',str(source),'--output',str(output)]
        if args.document_id:command+=['--document-id',args.document_id]
        if getattr(args,'cell',None):command+=['--cell',args.cell]
        return command+list(map(str,extra))

    try:
        snapshot_path=private/'native-snapshot.json';profile_path=private/'adapter-profile.json'
        execute('native-snapshot',native('snapshot',snapshot_path),20,reserve=min(40.,limit*.2))
        snapshot=json.loads(snapshot_path.read_text(encoding='utf8'))
        profile=compile_snapshot(snapshot);write_json(profile_path,profile)
        stage=private/'engine';report['engine_snapshot']=stage_engine(engine,stage,profile)
        core=destination/'engine-result'
        finishing=min(40.,max(4.,limit*.15))
        seconds=min(240.,remaining(finishing))
        if seconds<=0:raise ValueError('No time remains for either primary candidate')
        execute('core-flow',[sys.executable,'-X','utf8','-B',str(ADAPTER_ROOT/'engine_runner.py'),
            '--engine-root',str(stage),'--profile',str(profile_path),'--out-dir',str(core),
            '--seconds',str(seconds),'--iterations',str(args.iterations),'--seed',str(args.seed),
            '--layout-budget',budget_policy],
            seconds+1,reserve=max(2.,finishing-1),allow_timeout=True)
        chosen=choose_payloads(core,digest)
        if chosen is None:raise ValueError('No safe full-device layout was available for manual routing')
        wire,path=chosen
        # A finished sidecar's original-engine selection outranks emergency
        # checkpoint sorting. After a hard timeout, use the last atomic files.
        summary=core/'editable-engine-report.json'
        if summary.is_file():
            selection=json.loads(summary.read_text(encoding='utf8'))
            selected=Path(selection.get('wire_plan','')).resolve()
            if selected.is_file():
                if selected.parent!=core.resolve():raise ValueError('Candidate output escaped its directory')
                wire=json.loads(selected.read_text(encoding='utf8'))
                if wire.get('source_hash')!=digest:raise ValueError('Stale candidate selection')
        wire_path=private/'engine-wire-plan.json';write_json(wire_path,wire)
        draft=wire.get('result_kind')=='editable_draft'
        plan=build_plan(snapshot,profile,wire,allow_draft=True)
        plan['nativeModelVersion']=snapshot['nativeModelVersion']
        plan_path=destination/'native-plan.json';write_json(plan_path,plan)
        base=source.stem.removesuffix('.icproj')
        output=destination/(base+'-layout.icproj.json');svg=destination/(base+'-layout.svg')
        validation_path=destination/'native-validation.json'
        extra=['--plan',plan_path,'--svg',svg,'--report',validation_path]
        if draft:extra+=['--allow-draft','true']
        execute('native-apply-and-verify',native('apply',output,extra),60,reserve=1)
        if sha256(source.read_bytes()).hexdigest()!=digest:raise ValueError('Input native project changed during the run')
        for relative,expected in report['engine_snapshot']['source_sha256'].items():
            if sha256((stage/relative).read_bytes()).hexdigest()!=expected:
                raise ValueError('Staged core source changed during the run')
        validation=json.loads(validation_path.read_text(encoding='utf8'))
        report.update(status=validation['status'],complete=not draft,input_unchanged=True,
            native_project=str(output),native_svg=str(svg),validation=validation,
            engine_metrics=None if draft else wire['metrics'],draft_metrics=wire['metrics'] if draft else None,
            draft=wire.get('draft'),selected_candidate=wire['selected_candidate'],
            layout_budget=wire.get('layout_budget'),
            routing_stage=wire['routing_stage'],native_grid=plan['nativeGrid'],original_grid=plan['originalGrid'])
    except (OSError,ValueError,KeyError) as exc:
        report.update(status='failed',message=str(exc));raise
    finally:
        report['seconds']=round(perf_counter()-started,6)
        write_json(destination/'adapter-report.json',report)
    print(json.dumps({k:report[k] for k in ('status','complete','selected_candidate','seconds','native_project')},ensure_ascii=False))
    return report
