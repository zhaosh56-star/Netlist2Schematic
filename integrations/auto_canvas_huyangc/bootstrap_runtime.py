"""Prepare pinned native-protocol dependencies outside the target checkout."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import urllib.request


def lock_entry(text: str, key: str, section: str) -> str:
    body = text.split('\n' + section + ':\n', 1)[-1]
    match = re.search(r'^  [\"\']?' + re.escape(key) + r'[\"\']?:\s*$', body, re.M)
    if not match:
        raise ValueError(f'Missing {section} lock entry: {key}')
    following = body[match.end():]
    end = re.search(r'^  \S', following, re.M)
    return following[:end.start()] if end else following


def prepare(project_root: Path, runtime: Path) -> dict:
    project_root, runtime = project_root.resolve(), runtime.resolve()
    lock_path = project_root / 'pnpm-lock.yaml'
    lock = lock_path.read_text(encoding='utf-8').replace('\r\n', '\n')
    modules = runtime / 'node_modules'
    modules.mkdir(parents=True, exist_ok=True)
    pending = []
    for folder in ('model', 'math-typesetting'):
        manifest = json.loads((project_root/'packages'/folder/'package.json').read_text(encoding='utf-8'))
        pending.extend((name, version) for name, version in manifest.get('dependencies', {}).items()
                       if not version.startswith('workspace:'))
    installed = {}
    while pending:
        name, version = pending.pop(0)
        if name in installed:
            if installed[name] != version:
                raise ValueError(f'Conflicting native runtime versions for {name}')
            continue
        if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[\w.]+)?', version):
            raise ValueError(f'Runtime version must be pinned by the target lock: {name}@{version}')
        key = f'{name}@{version}'
        package_lock = lock_entry(lock, key, 'packages')
        integrity_match = re.search(r'integrity:\s*([^\s,}]+)', package_lock)
        if not integrity_match:
            raise ValueError(f'No locked integrity for {key}')
        integrity = integrity_match[1]
        folder = modules.joinpath(*name.split('/'))
        if not folder.resolve().is_relative_to(modules.resolve()):
            raise ValueError('Runtime package escaped node_modules')
        marker = folder/'.adapter-integrity'
        if marker.exists() and marker.read_text(encoding='ascii') == integrity:
            manifest = json.loads((folder/'package.json').read_text(encoding='utf-8'))
        else:
            if folder.exists():
                raise ValueError(f'Unexpected existing runtime directory: {folder}')
            print(f'Preparing native runtime: {key}', flush=True)
            with urllib.request.urlopen(f'https://registry.npmjs.org/{name}/{version}', timeout=30) as response:
                metadata = json.load(response)
            with urllib.request.urlopen(metadata['dist']['tarball'], timeout=60) as response:
                archive = response.read()
            algorithm, expected = integrity.split('-', 1)
            digest = base64.b64encode(hashlib.new(algorithm, archive).digest()).decode('ascii')
            if digest != expected:
                raise ValueError(f'Package integrity differs from target lock: {key}')
            with tempfile.TemporaryDirectory(prefix='native-runtime-', dir=runtime) as temporary:
                safe_root = Path(temporary).resolve()
                if not safe_root.is_relative_to(runtime):
                    raise ValueError('Runtime staging escaped its cache')
                with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
                    tar.extractall(safe_root, filter='data')
                unpacked = safe_root/'package'
                manifest = json.loads((unpacked/'package.json').read_text(encoding='utf-8'))
                if manifest.get('name') != name or manifest.get('version') != version:
                    raise ValueError(f'Package identity mismatch: {key}')
                folder.parent.mkdir(parents=True, exist_ok=True)
                try:
                    unpacked.rename(folder)
                except PermissionError:
                    # Windows scanners can hold newly unpacked directories.
                    # Copy into the checked, previously absent destination.
                    if folder.exists():
                        raise
                    shutil.copytree(unpacked, folder)
                marker.write_text(integrity, encoding='ascii')
        installed[name] = version
        dependencies = manifest.get('dependencies', {})
        if dependencies:
            snapshot = lock_entry(lock, key, 'snapshots')
            for child in dependencies:
                match = re.search(r'^      [\"\']?' + re.escape(child) +
                                  r'[\"\']?:\s*[\"\']?([^\s\"\']+)', snapshot, re.M)
                if not match:
                    raise ValueError(f'Missing locked dependency {child} of {key}')
                pending.append((child, match[1].split('(')[0]))
    report = dict(target_lock_sha256=hashlib.sha256(lock_path.read_bytes()).hexdigest(),
                  dependencies=installed, target_checkout_modified=False)
    (runtime/'runtime-manifest.json').write_bytes((json.dumps(report, indent=2)+'\n').encode('utf-8'))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', required=True, type=Path)
    parser.add_argument('--runtime', type=Path, default=Path(__file__).resolve().parents[2]/'.cache/auto-canvas-runtime')
    args = parser.parse_args(argv)
    print(json.dumps(prepare(args.project_root, args.runtime), indent=2))


if __name__ == '__main__':
    main()
