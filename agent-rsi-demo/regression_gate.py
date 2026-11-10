"""Run frozen compatibility tests against candidate source, not reference source."""
from pathlib import Path
import shutil
import sys


COMPATIBILITY = ('test_netlist', 'test_routing_rules', 'test_routing')


def overlay(source, destination):
    source, destination = Path(source), Path(destination)
    for path in source.rglob('*'):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())


def run_gate(adapter, engine, destination, mode='compat'):
    from circuit_adapter import copy_runtime, write_json
    destination = Path(destination)
    report_path = destination / 'regression.json'
    if report_path.exists():
        import json
        return json.loads(report_path.read_text(encoding='utf-8'))
    destination.mkdir(parents=True, exist_ok=True)
    if mode == 'off':
        report = {'passed': True, 'mode': mode, 'skipped': True}
        write_json(report_path, report)
        return report
    sandbox = destination / 'engine'
    copy_runtime(engine, sandbox)
    reference = adapter.root / 'reference' / 'engine'
    for name in ('tests', 'examples'):
        overlay(reference / name, sandbox / name)
    for name in ('netlists', 'fixtures'):
        path = reference / 'analog-canvas-main' / name
        if path.exists():
            overlay(path, sandbox / 'analog-canvas-main' / name)
    command = [sys.executable, '-B', '-m', 'unittest']
    command += ['discover', '-s', 'tests', '-q'] if mode == 'full' else ['-q', *COMPATIBILITY]
    env = adapter._environment()
    if mode != 'full':
        env['PYTHONPATH'] = str(sandbox / 'tests')
    with (destination / 'regression.log').open('w', encoding='utf-8') as log:
        completed = adapter._run_process(command, cwd=sandbox, env=env, stdout=log,
                                         stderr=-2, timeout=300 if mode == 'full' else 90)
    tail = (destination / 'regression.log').read_text(encoding='utf-8', errors='replace')[-12000:]
    report = {'passed': completed.returncode == 0, 'mode': mode, 'returncode': completed.returncode,
              'diagnostic': tail, 'tests': list(COMPATIBILITY) if mode == 'compat' else 'all frozen engine tests'}
    write_json(report_path, report)
    return report
