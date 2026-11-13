"""One fresh final-routing attempt after expanding only placement axis gaps.

No annealing, route reuse or output files belong to this recovery stage. Its
caller owns stage selection, final publication and aggregation of diagnostics.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from math import ceil, isfinite
from time import perf_counter

from netlist2schematic.layout.layout_quality import evaluate_quality
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from netlist2schematic.routing.routing_rules import RoutingOptions
from netlist2schematic.common.sa_common import load_local
from netlist2schematic.routing.sa_routing import RoutingValidationError, final_route


def _axis(placement, metadata, key, cells, cell_axis):
    """Read the actual physical axis, falling back to the old 150 px grid."""
    grid = metadata.get('layout_grid')
    if grid is not None and not isinstance(grid, dict):
        raise ValueError('layout_grid must be an object')
    if hasattr(placement, key):
        values = getattr(placement, key)
    elif grid is not None and key in grid:
        values = grid[key]
    else:
        values = [75.0 + 150.0 * number for number in
                  range(max(cell[cell_axis] for cell in cells) + 1)]
    if not isinstance(values, (list, tuple)) or not values or any(
            type(value) not in (int, float) or not isfinite(value) for value in values):
        raise ValueError(f'Invalid physical layout coordinates: {key}')
    if any(first >= second for first, second in zip(values, values[1:])):
        raise ValueError(f'Physical layout coordinates must increase: {key}')
    if max(cell[cell_axis] for cell in cells) >= len(values):
        raise ValueError(f'Physical layout coordinates do not cover the placement: {key}')
    return list(values)


def _expanded_axis(values, scale, grid_step):
    result = [values[0]]
    for first, second in zip(values, values[1:]):
        gap = (second - first) * scale
        if not isfinite(gap):
            raise ValueError('Expanded physical layout gap is not finite')
        grid_units = gap / grid_step
        if not isfinite(grid_units):
            raise ValueError('Expanded physical layout grid count is not finite')
        gap = ceil(grid_units) * grid_step
        coordinate = result[-1] + gap
        if not isfinite(coordinate) or coordinate <= result[-1]:
            raise ValueError('Expanded physical layout coordinates are invalid')
        result.append(coordinate)
    return result


def retry_spacing(context, *, routing_options=None, scale=1.5, seconds=60):
    """Expand a copied five-tuple and return it with one verified routing result.

    Logical cells, orientations and symbol-local pin geometry remain unchanged.
    Each original axis starts at the same coordinate; every following gap is
    multiplied by ``scale`` and rounded upward to ``grid_step``.  A new router
    receives a new deadline and no wires, search history or caches from earlier
    attempts. Ordinary invalid-input errors propagate without becoming recovery
    failures. Routing failures receive JSON-safe stage diagnostics.
    """
    if type(scale) not in (int, float) or not isfinite(scale) or scale <= 1:
        raise ValueError('spacing scale must be finite and greater than one')
    if type(seconds) not in (int, float) or not isfinite(seconds) or seconds <= 0:
        raise ValueError('spacing retry seconds must be positive and finite')
    if not isinstance(context, (list, tuple)) or len(context) != 5:
        raise ValueError('spacing retry requires a five-item placement context')
    options = deepcopy(routing_options if routing_options is not None else RoutingOptions())
    if not isinstance(options, RoutingOptions):
        raise ValueError('routing_options must be RoutingOptions')
    started = perf_counter()
    placement, comps, nets, index, metadata = deepcopy(context)
    if not isinstance(metadata, dict):
        raise ValueError('placement metadata must be an object')
    cells = placement.cell_of
    if not cells or len(cells) != len(comps) or any(
            not isinstance(cell, (tuple, list)) or len(cell) != 2 or any(
                type(value) is not int or value < 0 for value in cell)
            for cell in cells):
        raise ValueError('spacing retry requires assigned non-negative integer cells')
    if len({tuple(cell) for cell in cells}) != len(cells):
        raise ValueError('spacing retry placement contains duplicate cells')
    original = {
        'column_x': _axis(placement, metadata, 'column_x', cells, 1),
        'row_y': _axis(placement, metadata, 'row_y', cells, 0),
    }
    expanded = {key: _expanded_axis(values, scale, options.grid_step)
                for key, values in original.items()}
    placement.column_x = list(expanded['column_x'])
    placement.row_y = list(expanded['row_y'])
    metadata['layout_grid'] = deepcopy(expanded)
    metadata['routing_stage'] = 'spacing_retry'
    report = {
        'source_candidate': metadata.get('candidate_id'), 'scale': scale,
        'budget_seconds': seconds, 'grid_step': options.grid_step,
        'original_layout_grid': deepcopy(original), 'new_layout_grid': deepcopy(expanded),
        'geometry_change': 'axis_gaps_only', 'logical_cells_preserved': True,
        'orientations_preserved': True, 'local_pin_geometry_preserved': True,
    }
    source_optimization = {key: metadata.pop(key) for key in
                           ('energy', 'optimization_metrics') if key in metadata}
    if source_optimization:
        report['source_optimization'] = source_optimization
    if 'quality' in metadata:
        report['source_quality'] = metadata.pop('quality')
    # A context supplied by a saved-placement caller can contain old artifacts;
    # none of them may be presented as the result of the expanded coordinates.
    for key in ('routes', 'metrics', 'validation'):
        metadata.pop(key, None)
    metadata['spacing_retry'] = report
    par = load_local('place_and_route', 'place-and-route.py')
    deadline = perf_counter() + seconds
    try:
        routed, metrics, validation = final_route(par, nets, placement, index,
                                                  options=options, deadline=deadline)
        if not validation.get('valid', False):
            raise RoutingFailure('Spacing retry did not pass final validation',
                                 {'reason': 'spacing_retry_validation'})
    except (RoutingFailure, RoutingValidationError) as exc:
        report['elapsed_seconds'] = round(perf_counter() - started, 6)
        diagnostics = deepcopy(getattr(exc, 'diagnostics', {}))
        diagnostics.update(routing_stage='spacing_retry', spacing_retry=deepcopy(report),
                           parameters=asdict(options))
        raise RoutingFailure('Spacing retry failed: ' + str(exc), diagnostics) from exc
    quality = evaluate_quality(par, placement, comps, nets, index,
                               routed, metrics, metadata.get('structs', []))
    report['elapsed_seconds'] = round(perf_counter() - started, 6)
    report['routing_seconds'] = validation.get('routing_seconds', 0)
    metadata['quality'] = {
        'policy': 'balanced-v1', 'budget_seconds': seconds,
        'elapsed_seconds': report['elapsed_seconds'], 'stop_reason': 'spacing_retry_complete',
        'selected_candidate': str(metadata.get('candidate_id', 'unknown')),
        'score': quality['score'], 'details': quality,
        'candidates': [{'candidate_id': str(metadata.get('candidate_id', 'unknown')),
                        'seed': metadata.get('candidate_seed'), 'routing_stage': 'spacing_retry',
                        'status': 'valid', 'metrics': deepcopy(metrics), 'score': quality['score'],
                        'routing_seconds': validation.get('routing_seconds', 0)}],
    }
    return placement, comps, nets, index, metadata, (routed, metrics, validation)
