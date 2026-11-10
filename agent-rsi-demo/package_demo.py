"""Build the portable handoff without private config, environments or dev runs."""
from pathlib import Path
import argparse
import hashlib
import json
import re
import uuid
import zipfile


ROOT = Path(__file__).resolve().parent
FILES = {
    'agent-config.example.json', 'README.md', 'AGENT_SETUP.md', 'CLAUDE_START.md', 'CLAUDE.md',
    'PROJECT_CONTEXT.md', 'OPTIMIZATION_SCOPE.md', 'VALIDATION.md', 'requirements.txt', '.gitignore',
    'package_demo.py', 'run_demo.py', 'rsi_core.py', 'circuit_adapter.py', 'coding_agent.py',
    'verify_layout.py', 'svg_audit.py', 'input_bundle.py', 'runtime_control.py',
    'model_http_worker.py', 'regression_gate.py', 'atomic_io.py', 'build_report.py',
}


def build(archive, sample='ready-v2'):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', sample):
        raise ValueError('sample must name one run directory')
    paths = [ROOT / name for name in FILES]
    for name in ('engine', 'reference', 'benchmarks', 'vendor', 'wheels', 'tests'):
        paths.extend(p for p in (ROOT / name).rglob('*') if p.is_file())
    sample_dir = ROOT / 'runs' / sample
    if sample_dir.exists():
        paths.extend(p for p in sample_dir.rglob('*') if p.is_file())
    selected = []
    for path in sorted(set(paths)):
        if not path.resolve().is_relative_to(ROOT):
            raise ValueError('package input resolves outside the demo')
        parts = path.relative_to(ROOT).parts
        if any(p in ('__pycache__', '.git', '.venv', 'output', 'node_modules') for p in parts):
            continue
        if path.suffix in ('.pyc', '.tmp'):
            continue
        # Reproducible candidate versions + frozen tests are already included;
        # temporary compatibility-test overlays are unnecessary for replay.
        if len(parts) > 4 and parts[:3] == ('runs', sample, 'regression') and parts[4] == 'engine':
            continue
        selected.append(path)
    manifest = {'schema': 'circuit-rsi-portable-package-v2', 'sample': sample,
                'files': [{'path': p.relative_to(ROOT).as_posix(), 'bytes': p.stat().st_size,
                           'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in selected]}
    manifest_path = ROOT / 'PACKAGE_MANIFEST.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    selected.append(manifest_path)
    archive = Path(archive).resolve()
    temporary = archive.with_name(archive.name + '.' + uuid.uuid4().hex + '.tmp')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in selected:
            z.write(path, Path('agent-rsi-demo') / path.relative_to(ROOT))
    with zipfile.ZipFile(temporary) as z:
        if z.testzip() is not None:
            raise ValueError('archive integrity check failed')
    temporary.replace(archive)
    return {'archive': str(archive), 'files': len(selected), 'bytes': archive.stat().st_size}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', type=Path, default=ROOT.parent / 'agent-rsi-demo-portable.zip')
    parser.add_argument('--sample', default='ready-v2')
    args = parser.parse_args()
    print(json.dumps(build(args.archive, args.sample), ensure_ascii=False))
