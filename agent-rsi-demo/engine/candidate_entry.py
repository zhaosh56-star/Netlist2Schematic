#!/usr/bin/env python3
"""Editable generation entry point for the circuit RSI experiment.

The baseline uses the original free-grid SA and the real final router,
geometry validator and symbol renderer. The reference profile invokes the
current project's default structured workflow. New agent ideas belong here or
in the algorithm modules, while the evaluator lives outside the candidate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from run_flow import run

ROOT = Path(__file__).resolve().parent


def generate(source, destination, *, seed=20261029, iterations=None, cell=None,
             profile='baseline'):
    source = Path(source).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    if profile not in ('baseline', 'reference'):
        raise ValueError('profile must be baseline or reference')
    if iterations is None:
        iterations = 200 if profile == 'baseline' else 20000
    if type(iterations) is not int or iterations < 0:
        raise ValueError('iterations must be a non-negative integer')
    # Explicit bundled resource path makes runs independent of a parent
    # checkout or an inherited ANALOG_CANVAS_ROOT environment variable.
    common = dict(iterations=iterations, seed=seed, out_dir=destination, cell=cell,
                  analog_canvas_root=ROOT / 'analog-canvas-main')
    if profile == 'baseline':
        # Intentional starting point: no structural placement, no post-route
        # simplification. Still produces actual symbols and verified routes.
        # Agent improvements may change these choices or add new algorithms.
        run(source, mode='baseline', simplify_routes='off',
            quality_seconds=30, rescue_seconds=5, spacing_seconds=5,
            fallback_seconds=10, **common)
    else:
        # Present-day gongzuo capability: structured layered SA candidates,
        # full routing/recovery, safe route simplification and native SVGs.
        run(source, **common)
    placements = sorted(destination.glob('*-placement.json'))
    if len(placements) != 1:
        raise ValueError('generation must produce exactly one placement JSON in an empty output directory')
    receipt = {
        'profile': profile, 'seed': seed, 'iterations': iterations,
        'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'placement_json': placements[0].name,
        'svg_files': [p.name for p in sorted(destination.glob('*.svg'))],
        'note': 'Generator receipt only; protected evaluator must independently verify outputs.',
    }
    (destination / 'generation.json').write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return placements[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description='Real-symbol schematic candidate generation')
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--seed', type=int, default=20261029)
    parser.add_argument('--iterations', type=int)
    parser.add_argument('--cell')
    parser.add_argument('--profile', choices=('baseline', 'reference'), default='baseline')
    args = parser.parse_args(argv)
    try:
        if args.out.exists() and any(args.out.iterdir()):
            raise ValueError('output directory must be empty; use a new directory for every measurement')
        placement = generate(args.input, args.out, seed=args.seed, iterations=args.iterations,
                             cell=args.cell, profile=args.profile)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'Generation failed: {exc}', file=sys.stderr)
        return 1
    print(f'Placement: {placement}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
