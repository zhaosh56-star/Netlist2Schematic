"""Offline native JSON in/out; the core engine remains an independent dependency."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from math import isfinite
import shutil
import subprocess
import sys
from time import perf_counter

from .native_plan import build_plan
from .profile import compile_snapshot, stage_engine
from .runtime import run_bounded

ADAPTER_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = ADAPTER_ROOT.parents[1]


def write_json(path, value):
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2)+'\n').encode('utf-8'))


def find_node(project_root, supplied):
    candidates = ([supplied] if supplied else []) + [
        project_root/'.local/preview/runtime/node.exe', shutil.which('node')]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            result = subprocess.run([str(candidate), '--version'], capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and int(result.stdout.strip().lstrip('v').split('.')[0]) >= 24:
                return str(Path(candidate).resolve())
    raise ValueError('Node 24+ is required; use --node or the target preview runtime')


def run(args):
    if getattr(args,'layout_budget','legacy')!='legacy' and getattr(args,'result_policy','complete')!='editable':
        raise ValueError('Adaptive layout budget requires the editable result policy')
    if getattr(args,'result_policy','complete')=='editable':
        from .editable_adapter import run_editable
        return run_editable(args)
    project_root, engine_root = args.project_root.resolve(), args.engine_root.resolve()
    source, destination = args.input.resolve(), args.out_dir.resolve()
    if not source.is_file():
        raise ValueError('Input native project does not exist')
    if destination == source.parent or source.is_relative_to(destination):
        raise ValueError('Output must be separate from the input project directory')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Choose an empty output directory; existing results are preserved')
    runtime = args.runtime.resolve()
    runtime_manifest = runtime/'runtime-manifest.json'
    if not runtime_manifest.is_file():
        raise ValueError('Prepare the isolated native runtime first: python -m '
                         'integrations.auto_canvas_huyangc.bootstrap_runtime --project-root <target>')
    dependencies = json.loads(runtime_manifest.read_text(encoding='utf-8'))
    target_lock = hashlib.sha256((project_root/'pnpm-lock.yaml').read_bytes()).hexdigest()
    if dependencies['target_lock_sha256'] != target_lock:
        raise ValueError('Target dependency lock changed; prepare a matching isolated runtime')
    node = find_node(project_root, args.node)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    destination.mkdir(parents=True, exist_ok=True)
    private, logs = destination/'private', destination/'logs'
    private.mkdir(); logs.mkdir()
    report = dict(format='auto-canvas-native-adapter-v1', status='running',
        source=str(source), source_sha256=source_hash, target_root=str(project_root),
        engine_root=str(engine_root), output_dir=str(destination), steps=[],
        target_checkout_modified=False, core_source_modified=False)
    started = perf_counter()

    def execute(stage, command, seconds):
        print(f'Adapter: {stage}', flush=True)
        before = perf_counter()
        log = logs/(stage+'.log')
        with log.open('w', encoding='utf-8') as stream:
            code = run_bounded(command, seconds=seconds, cwd=engine_root,
                               stdout=stream, stderr=stream)
        report['steps'].append(dict(stage=stage, returncode=code,
            seconds=round(perf_counter()-before, 6), log=str(log), command=command))
        if code != 0:
            tail = log.read_text(encoding='utf-8', errors='replace')[-3500:]
            raise ValueError(f'{stage} failed (exit {code}); see {log}\n{tail}')

    def native(operation, output, extra=()):
        command = [node, '--experimental-transform-types', str(ADAPTER_ROOT/'native.mjs'),
            operation, '--project-root', str(project_root), '--runtime', str(runtime),
            '--input', str(source), '--output', str(output)]
        if args.document_id:
            command += ['--document-id', args.document_id]
        if getattr(args, 'cell', None):
            command += ['--cell', args.cell]
        return command+list(extra)

    try:
        snapshot_path, profile_path = private/'native-snapshot.json', private/'adapter-profile.json'
        execute('native-snapshot', native('snapshot', snapshot_path), 60)
        snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))
        profile = compile_snapshot(snapshot)
        write_json(profile_path, profile)
        stage = private/'engine'
        report['engine_snapshot'] = stage_engine(engine_root, stage, profile)
        core_output = destination/'engine-result'
        execute('core-flow', [sys.executable, '-X', 'utf8', '-B', str(stage/'run_flow.py'),
            str(stage/'layout-input.cir'), '--out-dir', str(core_output),
            '--iterations', str(args.iterations), '--seed', str(args.seed),
            '--time-seconds', str(args.engine_seconds), '--formal-output', 'off'], args.engine_seconds+10)
        saved = core_output/'layout-input-placement.json'
        wire_plan_path = private/'engine-wire-plan.json'
        execute('core-geometry', [sys.executable, '-X', 'utf8', '-B', str(ADAPTER_ROOT/'engine_export.py'),
            '--engine-root', str(stage), '--placement', str(saved), '--profile', str(profile_path),
            '--output', str(wire_plan_path)], 30)
        wire_plan = json.loads(wire_plan_path.read_text(encoding='utf-8'))
        native_plan = build_plan(snapshot, profile, wire_plan)
        native_plan['nativeModelVersion'] = snapshot['nativeModelVersion']
        native_plan_path = destination/'native-plan.json'
        write_json(native_plan_path, native_plan)
        name = source.stem.removesuffix('.icproj')
        native_output = destination/(name+'-layout.icproj.json')
        svg = destination/(name+'-layout.svg')
        validation_path = destination/'native-validation.json'
        execute('native-apply-and-verify', native('apply', native_output,
            ['--plan', str(native_plan_path), '--svg', str(svg), '--report', str(validation_path)]), 60)
        validation = json.loads(validation_path.read_text(encoding='utf-8'))
        if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
            raise ValueError('Input native project changed during the run')
        for relative, expected in report['engine_snapshot']['source_sha256'].items():
            if hashlib.sha256((stage/relative).read_bytes()).hexdigest() != expected:
                raise ValueError('Staged core source changed during the run')
        report.update(status='valid', input_unchanged=True, validation=validation,
            native_project=str(native_output), native_svg=str(svg),
            engine_metrics=wire_plan['metrics'], selected_candidate=wire_plan['selected_candidate'],
            routing_stage=wire_plan['routing_stage'], native_grid=native_plan['nativeGrid'],
            original_grid=native_plan['originalGrid'])
    except (ValueError, OSError, KeyError) as exc:
        report.update(status='failed', message=str(exc),
                      input_unchanged=hashlib.sha256(source.read_bytes()).hexdigest() == source_hash)
        raise
    finally:
        report['seconds'] = round(perf_counter()-started, 6)
        write_json(destination/'adapter-report.json', report)
    print(json.dumps({k:report[k] for k in ('status','native_project','native_svg','selected_candidate',
                                           'routing_stage','engine_metrics','seconds')}, ensure_ascii=False))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True, help='Native .icproj.json')
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--engine-root', type=Path, default=REPOSITORY_ROOT)
    parser.add_argument('--runtime', type=Path, default=REPOSITORY_ROOT/'.cache/auto-canvas-runtime')
    parser.add_argument('--node', type=Path)
    selection=parser.add_mutually_exclusive_group()
    selection.add_argument('--document-id')
    selection.add_argument('--cell',help='Select a unique native Cell/Document name without expanding hierarchy')
    parser.add_argument('--engine-seconds', type=float, default=720)
    parser.add_argument('--result-policy',choices=('complete','editable'),default='complete')
    parser.add_argument('--layout-budget',choices=('legacy','adaptive'),default='legacy')
    parser.add_argument('--total-seconds',type=float,default=300,
                        help='Editable policy total budget, including conversion and validation')
    parser.add_argument('--iterations', type=int, default=20000)
    parser.add_argument('--seed', type=int, default=20261029)
    args = parser.parse_args(argv)
    if (not isfinite(args.engine_seconds) or args.engine_seconds <= 0 or args.iterations < 0
            or not isfinite(args.total_seconds) or args.total_seconds<=0):
        parser.error('Engine budget must be positive and iterations nonnegative')
    try:
        run(args)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f'Adapter error: {exc}\n')
    return 0
