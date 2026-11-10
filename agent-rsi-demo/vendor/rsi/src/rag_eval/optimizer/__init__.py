"""rag_eval.optimizer — method-v3 engine (system-agnostic core).

Public API
----------
ProtocolConfig   : pre-registered protocol parameters
run_optimization : top-level entry point
SystemAdapter    : ABC for system-specific capabilities
Version, Card, Diff, EvalResult, SmokeResult  : data objects
Lineage, NodeState, MilestoneState            : lineage state
"""

from .adapter import BudgetExhausted, EvaluationUnavailable, Card, Diff, EvalResult, Predicate, SmokeResult, Version, SystemAdapter
from .ledger import Lineage, MilestoneState, NodeState
from .loop import ProtocolConfig, LoopResult, RoundResult, run_optimization
from .stats import (
    alpha_warning,
    attribution_label,
    attribution_result,
    confirm_decision,
    ladder_m,
    mean_delta,
    paired_differences,
    paired_se,
    required_n_fresh,
    screen_decision,
)

__all__ = [
    # adapter
    "BudgetExhausted", "EvaluationUnavailable", "Card", "Diff", "EvalResult", "Predicate", "SmokeResult", "Version", "SystemAdapter",
    # ledger
    "Lineage", "MilestoneState", "NodeState",
    # loop
    "ProtocolConfig", "LoopResult", "RoundResult", "run_optimization",
    # stats
    "alpha_warning", "attribution_label", "attribution_result", "confirm_decision", "ladder_m",
    "mean_delta", "paired_differences", "paired_se", "required_n_fresh", "screen_decision",
]
