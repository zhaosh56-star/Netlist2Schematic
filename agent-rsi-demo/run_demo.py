"""Portable, resumable real-code RSI command line."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

from circuit_adapter import CircuitAdapter, sha, tree_hash, write_json
from rsi_core import Version, run_core, BudgetExhausted, EvaluationCancelled
from build_report import build_report

ROOT = Path(__file__).resolve().parent


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def resolve_engine(source):
    source = Path(source).resolve()
    if (source / 'summary.json').exists() or (source / 'method/state.json').exists():
        version = (read(source / 'summary.json')['final_version'] if (source / 'summary.json').exists()
                   else read(source / 'method/state.json')['lineage'][-1]['version'])
        path = source / 'versions' / version[:20] / 'engine'
        if not path.exists():
            path = source / 'versions' / version / 'engine'
        if tree_hash(path) != version:
            raise ValueError('starting adopted source hash does not match')
        return path
    if (source / 'engine' / 'candidate_entry.py').exists():
        source /= 'engine'
    if not (source / 'candidate_entry.py').exists():
        raise ValueError('--from-version requires a prior run, version directory or complete engine')
    return source


def summarize_state(output, status):
    path = output / 'method' / 'state.json'
    if not path.exists():
        return
    state = read(path)
    summary = {'status': status, 'final_version': state['lineage'][-1]['version'],
               'lineage': state['lineage'], 'resumable': True}
    write_json(output / 'summary.json', summary)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Real code-changing Claude RSI with frozen schematic evaluation')
    parser.add_argument('--stage', choices=('optimize', 'baseline', 'reference', 'endpoint', 'inspect', 'report', 'export'), default='optimize')
    parser.add_argument('--mode', choices=('claude', 'api', 'smoke'))
    parser.add_argument('--config', type=Path)
    parser.add_argument('--suite', type=Path)
    parser.add_argument('--rounds', type=int, help='additional rounds on --resume; default 3')
    parser.add_argument('--hours', type=float, help='total registered run budget; unchanged on resume')
    parser.add_argument('--iterations', type=int)
    parser.add_argument('--case-timeout', type=int)
    parser.add_argument('--regression', choices=('compat', 'full', 'off'))
    parser.add_argument('--reference-iterations', type=int, default=20000)
    parser.add_argument('--reference', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--from-version', type=Path)
    parser.add_argument('--export-to', type=Path)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    output = args.out or Path('runs') / datetime.now().strftime('run-%Y%m%d-%H%M%S-%f')
    output = (output if output.is_absolute() else ROOT / output).resolve()
    adapter, owned = None, False
    started = time.monotonic()
    try:
        if args.stage == 'report':
            if not (output / 'run.json').exists():
                raise ValueError('report requires an existing run')
            print(build_report(output))
            return 0
        if args.resume and (args.stage != 'optimize' or args.from_version):
            raise ValueError('--resume is for optimize only and cannot combine with --from-version')
        existing = read(output / 'run.json') if (output / 'run.json').exists() else None
        if args.stage in ('optimize', 'baseline') and not args.resume and output.exists() and any(output.iterdir()):
            raise ValueError('optimize/baseline must use a new empty output directory')
        if args.resume and (not existing or not (output / 'method' / 'state.json').exists()):
            raise ValueError('--resume requires an existing version-2 optimisation run with state.json')
        if args.stage not in ('optimize', 'baseline') and not existing:
            raise ValueError('this stage requires an existing --out run')
        if existing and existing.get('schema') != 'circuit-rsi-run-v2':
            raise ValueError('older run is viewable with --stage report; start a new run using --from-version')
        defaults = {'mode': 'claude', 'iterations': 200, 'case_timeout': 180, 'regression': 'compat', 'hours': 1.0}
        keys = {'mode': 'mode', 'iterations': 'iterations', 'case_timeout': 'case_timeout_seconds',
                'regression': 'regression', 'hours': 'hours'}
        for attribute, key in keys.items():
            requested = getattr(args, attribute)
            frozen = existing.get(key, defaults[attribute]) if existing else defaults[attribute]
            if args.resume and requested is not None and requested != frozen:
                raise ValueError('resume cannot change registered ' + attribute + '; use --from-version for a new experiment')
            setattr(args, attribute, frozen if requested is None else requested)
        if args.hours <= 0 or args.iterations < 0 or not 1 <= args.case_timeout <= 3600 or args.reference_iterations < 0:
            raise ValueError('invalid time/iteration budget')
        args.rounds = 3 if args.rounds is None else args.rounds
        if args.rounds < 0:
            raise ValueError('rounds must be non-negative')
        suite = args.suite or (ROOT / existing['suite'] if existing else ROOT / 'benchmarks/suite.v2.json')
        config_path = args.config or (Path(existing['config_path']) if existing else ROOT / 'agent-config.example.json')
        config = read(config_path)
        config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        if args.resume and config_hash != existing['agent_config_sha256']:
            raise ValueError('resume model configuration changed; use --from-version for a fresh experiment')
        if args.stage == 'optimize' and args.mode == 'claude' and not shutil.which(config.get('claude_executable', 'claude')):
            raise ValueError('Claude Code is unavailable; install/login or use --mode api / --mode smoke')
        if args.stage == 'optimize' and args.mode == 'api':
            for role in ('analyst', 'implementer'):
                entry = config.get('roles', {}).get(role) or config.get('roles', {}).get('analyst', {})
                if not os.environ.get(entry.get('api_key_env', '')):
                    raise ValueError('missing API key environment variable for ' + role)
        state = read(output / 'method/state.json') if args.resume else None
        elapsed = state.get('elapsed_s', 0) if state else 0
        remaining_budget = args.hours * 3600 - elapsed
        if args.resume and remaining_budget <= 0:
            raise ValueError('registered budget exhausted; start a new budget with --from-version')
        deadline = started + remaining_budget
        adapter = CircuitAdapter(ROOT, output, suite=suite, agent_config=config, mode=args.mode,
                                 iterations=args.iterations, case_timeout=args.case_timeout, regression=args.regression)
        adapter.outer_remaining = lambda: deadline - time.monotonic()
        adapter.configure_runtime(remaining=None, max_concurrent_executions=1)
        seed = adapter.create_seed(resolve_engine(args.from_version) if args.from_version else None)
        if existing and seed.content_hash != existing['seed_version']:
            raise ValueError('run seed source identity changed')
        if not existing:
            identity = {'schema': 'circuit-rsi-run-v2', 'mode': args.mode,
                        'suite': Path(suite).resolve().relative_to(ROOT).as_posix(), 'suite_sha256': sha(suite),
                        'seed_version': seed.content_hash, 'iterations': args.iterations,
                        'case_timeout_seconds': args.case_timeout, 'hours': args.hours,
                        'regression': args.regression, 'config_path': str(config_path.resolve()),
                        'agent_config_sha256': config_hash, 'seed': adapter.seed_value,
                        'round_limit': args.rounds, 'synthetic': args.mode == 'smoke',
                        'score_policy': 'geometry-plus-native-svg-readability-v2',
                        'from_version': str(args.from_version) if args.from_version else None}
            write_json(output / 'run.json', identity)
        owned = True
        search = [name for name, case in adapter.cases.items() if case['split'] == 'search']
        validation = [name for name, case in adapter.cases.items() if case['split'] == 'validation']
        tests = [name for name, case in adapter.cases.items() if case['split'] == 'test']
        summary_path = output / 'summary.json'
        summary = read(summary_path) if summary_path.exists() else {}
        if args.stage == 'optimize':
            maximum = len(state['rounds']) + args.rounds if state else args.rounds
            result = run_core(adapter, seed, search=search, validation=validation, state_dir=output / 'method',
                              max_rounds=maximum, time_budget_hours=args.hours, test_size=len(tests), resume=args.resume)
            summary = {key: value for key, value in result.items() if key != 'ledger'}
            write_json(summary_path, summary)
            # Re-arm owned processes after core shutdown, retaining the SAME wall budget.
            adapter.configure_runtime(remaining=None, max_concurrent_executions=1)
        elif args.stage == 'baseline':
            adapter.evaluate(seed, search + validation, 1, purpose='baseline')
            summary = {'status': 'baseline_only', 'final_version': seed.content_hash, 'lineage': []}
            write_json(summary_path, summary)
        elif args.stage == 'inspect':
            checked = adapter.evaluate(Version(summary['final_version']), search + validation, 1, purpose='baseline')
            write_json(output / 'final-evaluation.json', {'scores': checked.per_task_scores, 'errors': checked.error_classes})
        elif args.stage == 'endpoint':
            final = Version(summary['final_version'])
            subject = adapter.evaluate(final, tests, 1, purpose='sealed')
            baseline = adapter.evaluate(seed, tests, 1, purpose='sealed')
            write_json(output / 'endpoint.json', {'tasks': tests, 'seed': baseline.context['measurements'],
                'final': subject.context['measurements'], 'scores': subject.per_task_scores,
                'errors': subject.error_classes, 'paired_delta': {t: subject.per_task_scores[t] - baseline.per_task_scores[t] for t in tests},
                'note': 'post-optimisation matched seed/final; never sent to proposing agent'})
        elif args.stage == 'export':
            if not args.export_to:
                raise ValueError('--stage export requires --export-to')
            from circuit_adapter import copy_runtime
            target = args.export_to.resolve()
            if target.exists() and any(target.iterdir()):
                raise ValueError('export destination must be empty')
            version = Version(summary['final_version'])
            copy_runtime(adapter.engine_for(version), target / 'engine')
            write_json(target / 'version.json', {'hash': version.content_hash, 'source_run': str(output)})
        if args.reference or args.stage == 'reference':
            tasks = tests if args.stage == 'endpoint' else search + validation
            reference = adapter.reference(tasks, iterations=args.reference_iterations)
            write_json(output / 'reference-summary.json', reference)
            summary['reference_iterations'] = args.reference_iterations
            write_json(summary_path, summary)
        print('状态:', summary.get('status', args.stage))
        print('报告:', build_report(output))
        return 1 if summary.get('status') in ('process_failure', 'budget_exhausted') else 0
    except KeyboardInterrupt:
        if adapter:
            adapter.cancel_pending()
        if owned:
            summarize_state(output, 'interrupted')
        print('已停止，可用 --resume 接续当前状态。', file=sys.stderr)
        return 130
    except (BudgetExhausted, EvaluationCancelled) as exc:
        if owned:
            summarize_state(output, 'budget_exhausted' if isinstance(exc, BudgetExhausted) else 'interrupted')
            write_json(output / 'last-error.json', {'error': str(exc), 'unmeasurable': True})
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        if owned:
            write_json(output / 'last-error.json', {'error': f'{type(exc).__name__}: {exc}'})
        print('运行失败:', exc, file=sys.stderr)
        return 1
    finally:
        if adapter:
            adapter.cancel_pending()


if __name__ == '__main__':
    raise SystemExit(main())
