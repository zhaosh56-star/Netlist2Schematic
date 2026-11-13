"""Shared module loading and read-only Analog Canvas resource locations."""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys

if os.name == 'nt':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')

ROOT = Path(__file__).resolve().parents[2]
_resource_root = ROOT / "analog-canvas-main"


def source_path(filename, root=None):
    """Resolve a former flat-engine filename in this or a frozen engine tree."""
    base = Path(root) if root is not None else ROOT
    manifest = base / 'netlist2schematic' / 'source_map.json'
    mapping = json.loads(manifest.read_text(encoding='utf8')) if manifest.is_file() else {}
    return base / mapping.get(filename, filename)


def engine_source_files(root=None):
    """Fingerprint actual implementations as well as compatibility entrypoints."""
    base = Path(root) if root is not None else ROOT
    files = set(base.glob('*.py'))
    package = base / 'netlist2schematic'
    if package.is_dir():
        files.update(package.rglob('*.py'))
        files.add(package / 'source_map.json')
    return sorted(files)


def load_local(name, filename):
    """Load hyphenated legacy scripts once, with ordinary import semantics."""
    if name in sys.modules:
        return sys.modules[name]
    path = source_path(filename)
    canonical = { 'parse_netlist': 'parse-netlist.py', 'place_and_route': 'place-and-route.py',
                  'place_layered': 'place-layered.py', 'render_clean': 'render-clean.py' }
    if canonical.get(name) == filename:
        module_name = '.'.join(path.relative_to(ROOT).with_suffix('').parts)
        module = importlib.import_module(module_name)
        sys.modules[name] = module
        return module
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def configure_resources(resource_root=None):
    global _resource_root
    _resource_root = Path(resource_root or os.environ.get(
        "ANALOG_CANVAS_ROOT", ROOT / "analog-canvas-main")).expanduser().resolve()
    definitions_dir = _resource_root / "packages" / "components" / "definitions"
    if not definitions_dir.is_dir():
        raise ValueError(f"Symbol definitions not found: {definitions_dir}")
    return _resource_root


def resource_root():
    return _resource_root


def read_json(path):
    def invalid_constant(value):
        raise ValueError(f'Invalid JSON numeric constant: {value}')
    with Path(path).open(encoding="utf-8-sig") as handle:
        return json.load(handle, parse_constant=invalid_constant)


def write_json(path, data):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                           encoding="utf-8")
