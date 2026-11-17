"""Private, independent routing checkpoints for failure-only candidate rescue.

Placements and scenes are execution objects, never output data.  Only the
scalar progress summaries below may be included in public diagnostics.
"""
from copy import deepcopy


VERSION = 'candidate-checkpoints-v1'


def capture_candidate(context, checkpoint, options, candidate_id, attempt_index):
    if context is None or checkpoint is None:
        return None
    return deepcopy(dict(context=context, checkpoint=checkpoint, options=options,
                         candidate_id=str(candidate_id), attempt_index=attempt_index))


def progress(candidate):
    checkpoint = candidate['checkpoint']
    pins = checkpoint['scene'].pins
    completed = checkpoint.get('completed', {})
    # Interrupted branches are deliberately excluded.  Rescue starts that net
    # again; a partly connected tree is not a completed network.
    terminals = sum(len(items) for items in pins.values())
    done = sum(len(pins[name]) for name in completed if name in pins)
    return dict(candidate_id=candidate['candidate_id'],
                attempt_index=candidate['attempt_index'],
                completed_nets=len(completed), total_nets=len(pins),
                completed_terminals=done, total_terminals=terminals,
                terminal_fraction=done / max(1, terminals),
                net_fraction=len(completed) / max(1, len(pins)),
                failed_net=checkpoint.get('failed_net'),
                pending_nets=len(checkpoint.get('pending', ())),
                margin=checkpoint.get('margin'))


def rank_candidates(candidates):
    """Prefer primary poses, then verified progress, with a stable recent tie.

    This schedules opportunities, not a proof that a route can be completed.
    The existing rescue still rebuilds the scene and validates all connections.
    """
    rows = [(candidate, progress(candidate)) for candidate in candidates]
    return sorted(rows, key=lambda row: (
        row[1]['candidate_id'] not in ('2', '3'),
        -row[1]['terminal_fraction'], -row[1]['net_fraction'],
        -row[1]['attempt_index'], row[1]['candidate_id']))
