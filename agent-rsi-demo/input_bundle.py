"""Freeze a SPICE source and all recursive .include/.inc dependencies."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


class InputBundleChanged(ValueError):
    pass


def inventory(source, *, boundary=None):
    source = Path(source).resolve()
    boundary = Path(boundary).resolve() if boundary else source.parent
    files = {}
    pattern = re.compile(r'''^\.(?:include|inc)\s+(?:"([^"]+)"|'([^']+)'|(\S+))\s*$''', re.I)

    def visit(path, ancestors=()):
        path = path.resolve()
        if not path.is_relative_to(boundary):
            raise ValueError('include dependency must stay inside the bundled corpus')
        if path in ancestors or len(ancestors) >= 32:
            raise ValueError('recursive or excessively nested include')
        raw = path.read_bytes()
        files[path.relative_to(boundary).as_posix()] = hashlib.sha256(raw).hexdigest()
        logical = []
        for raw_line in raw.decode('utf-8-sig').splitlines():
            line = raw_line.strip()
            if not line or line.startswith('*'):
                continue
            line = re.split(r'[$;]', line, maxsplit=1)[0].strip()
            if line.startswith('+'):
                if not logical:
                    raise ValueError('continuation without preceding statement')
                logical[-1] += ' ' + line[1:].strip()
            elif line:
                logical.append(line)
        for line in logical:
            if line.split()[0].lower() not in ('.include', '.inc'):
                continue
            match = pattern.fullmatch(line)
            if not match:
                raise ValueError('malformed include directive')
            target = next(x for x in match.groups() if x is not None)
            child = Path(target)
            visit(child if child.is_absolute() else path.parent / child, (*ancestors, path))

    visit(source)
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {'boundary': str(boundary), 'source': source.relative_to(boundary).as_posix(),
            'files': files, 'sha256': digest}


def check_bundle(bundle):
    boundary = Path(bundle['boundary'])
    try:
        current = inventory(boundary / bundle['source'], boundary=boundary)
    except (OSError, ValueError) as exc:
        raise InputBundleChanged('frozen SPICE dependency is inaccessible or changed') from exc
    if current != bundle:
        raise InputBundleChanged('SPICE source/include bundle changed after freezing')
