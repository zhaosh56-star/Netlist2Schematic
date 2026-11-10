"""Shared module loading and read-only Analog Canvas resource locations."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys

if os.name == 'nt':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')

ROOT = Path(__file__).resolve().parent
_resource_root = ROOT / "analog-canvas-main"


def load_local(name, filename):
    """Load hyphenated legacy scripts once, with ordinary import semantics."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
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
