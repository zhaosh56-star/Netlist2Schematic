"""Lineage tracking, node-state machine, and three-section evidence ledger.

Implements the three node states (§1), the §2.2 B state-transition table, and
the three evidence ledgers (§2.4) from method-v3.md.  No model clients or SN
imports.  All lists are append-only; field names are never renamed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class NodeState(Enum):
    PROVISIONAL = "provisional"
    CERTIFIED = "certified"
    ROLLED_BACK = "rolled_back"


@dataclass
class LineageNode:
    """One node in the improvement lineage."""
    version_hash: str
    parent_hash: Optional[str]       # None for the seed node
    diff_ref: Optional[str]
    card_ref: Optional[str]
    predicate_ref: Optional[str]     # optional per §1
    agent_cost: float
    created_at: str                  # ISO-8601 UTC
    state: NodeState


@dataclass
class MilestoneState:
    """Mutable per-run state for milestone confirmation tracking."""
    cumulative_delta_val: float
    best_score: float
    last_certified_id: Optional[str]    # version_hash of last certified node
    current_provisional_id: Optional[str]
    segment_start_best: float           # best at segment start; used on rollback
    n_confirmations: int = 0


class Lineage:
    """Append-only sequence of LineageNodes keyed by version_hash."""

    def __init__(self) -> None:
        self._nodes: list[LineageNode] = []
        self._index: dict[str, LineageNode] = {}

    def add_provisional(
        self,
        version_hash: str,
        parent_hash: Optional[str],
        diff_ref: Optional[str],
        card_ref: Optional[str],
        predicate_ref: Optional[str],
        agent_cost: float,
    ) -> LineageNode:
        """Append a new PROVISIONAL node; raises if version_hash already present."""
        if version_hash in self._index:
            raise ValueError(f"version_hash {version_hash!r} already in lineage")
        node = LineageNode(
            version_hash=version_hash,
            parent_hash=parent_hash,
            diff_ref=diff_ref,
            card_ref=card_ref,
            predicate_ref=predicate_ref,
            agent_cost=agent_cost,
            created_at=datetime.now(timezone.utc).isoformat(),
            state=NodeState.PROVISIONAL,
        )
        self._nodes.append(node)
        self._index[version_hash] = node
        return node

    def certify(self, version_hash: str, fresh_result: dict) -> None:
        """Transition node to CERTIFIED; attach fresh_result as metadata."""
        node = self._get(version_hash)
        if node.state != NodeState.PROVISIONAL:
            raise ValueError(f"cannot certify node in state {node.state!r}")
        node.state = NodeState.CERTIFIED  # LineageNode is mutable (not frozen)
        # Store fresh_result reference on node metadata via __dict__ since dataclass isn't frozen
        object.__setattr__(node, '_fresh_result', fresh_result)

    def rollback(self, version_hash: str) -> None:
        """Transition node to ROLLED_BACK."""
        node = self._get(version_hash)
        if node.state not in (NodeState.PROVISIONAL, NodeState.CERTIFIED):
            raise ValueError(f"cannot roll back node in state {node.state!r}")
        node.state = NodeState.ROLLED_BACK

    def current_parent(self) -> Optional[LineageNode]:
        """Return the most recent non-rolled-back node (provisional or certified)."""
        for node in reversed(self._nodes):
            if node.state != NodeState.ROLLED_BACK:
                return node
        return None

    def current_certified(self) -> Optional[LineageNode]:
        """Return the most recent CERTIFIED node."""
        for node in reversed(self._nodes):
            if node.state == NodeState.CERTIFIED:
                return node
        return None

    def provisional_segment(self, start_certified_id: Optional[str]) -> list[LineageNode]:
        """Return PROVISIONAL nodes appended after the given certified node hash.

        If start_certified_id is None, return all provisional nodes from the start.
        """
        if start_certified_id is None:
            cutoff = -1
        else:
            cutoff = next(
                (i for i, n in enumerate(self._nodes) if n.version_hash == start_certified_id),
                None,
            )
            if cutoff is None:
                raise ValueError(f"certified node {start_certified_id!r} not found")
        return [
            n for n in self._nodes[cutoff + 1 :]
            if n.state == NodeState.PROVISIONAL
        ]

    def _get(self, version_hash: str) -> LineageNode:
        if version_hash not in self._index:
            raise ValueError(f"unknown version_hash {version_hash!r}")
        return self._index[version_hash]

    def all_nodes(self) -> list[LineageNode]:
        """Return all nodes in append order (for sealed-set evaluation)."""
        return list(self._nodes)

    # -- persistence (binding plan §2.7) -------------------------------------

    def to_records(self) -> list[dict]:
        """JSON-safe node records in append order (state as its string value)."""
        out = []
        for n in self._nodes:
            out.append(dict(
                version_hash=n.version_hash, parent_hash=n.parent_hash, diff_ref=n.diff_ref,
                card_ref=n.card_ref, predicate_ref=n.predicate_ref, agent_cost=n.agent_cost,
                created_at=n.created_at, state=n.state.value,
                fresh_result=getattr(n, '_fresh_result', None),
            ))
        return out

    @classmethod
    def from_records(cls, records: list[dict]) -> "Lineage":
        """Inverse of to_records()."""
        lineage = cls()
        for r in records:
            node = LineageNode(
                version_hash=r["version_hash"], parent_hash=r.get("parent_hash"),
                diff_ref=r.get("diff_ref"), card_ref=r.get("card_ref"),
                predicate_ref=r.get("predicate_ref"), agent_cost=r.get("agent_cost", 0.0),
                created_at=r.get("created_at", ""), state=NodeState(r["state"]),
            )
            if r.get("fresh_result") is not None:
                object.__setattr__(node, '_fresh_result', r["fresh_result"])
            if node.version_hash in lineage._index:
                raise ValueError(f"duplicate version_hash {node.version_hash!r} in lineage records")
            lineage._nodes.append(node)
            lineage._index[node.version_hash] = node
        return lineage
