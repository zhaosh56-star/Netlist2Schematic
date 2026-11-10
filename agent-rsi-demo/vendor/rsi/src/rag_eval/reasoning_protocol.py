"""Explicit reasoning protocol variants used by Notebook generation runs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


STANDARD_REASONING_PROTOCOL = "reasoning-v1"
NO_CLARIFICATION_PROTOCOL = "reasoning-no-clarification-v1"
AUTO_CONFIRM_POLICY = {"name": "cover-all-options", "version": "v1"}
_ALL_OPTIONS_PREFIX = "同时考虑以下所有可能："
_ANSWER_MAX_CHARS = 2000


@dataclass(frozen=True)
class AutoConfirmation:
    answers: list[dict[str, str]]
    decisions: list[dict[str, Any]]
    policy: dict[str, str]


def protocol_identity(protocol: str) -> dict[str, Any]:
    """Return the public identity fragment for one explicit runner protocol."""
    if protocol == STANDARD_REASONING_PROTOCOL:
        return {"name": STANDARD_REASONING_PROTOCOL, "comparison_category": "controlled-rerun"}
    if protocol == NO_CLARIFICATION_PROTOCOL:
        return {
            "name": NO_CLARIFICATION_PROTOCOL,
            "comparison_category": "controlled-rerun",
            "auto_confirm_policy": dict(AUTO_CONFIRM_POLICY),
        }
    raise ValueError("Unknown reasoning protocol: " + str(protocol))


def ensure_comparable_reasoning_protocols(methods: list[dict[str, Any]]) -> None:
    """Reject a comparison that would merge standard and forced-answer SN."""
    protocols = set()
    for method in methods:
        if method.get("kind") != "sn":
            continue
        configuration = method.get("configuration") or {}
        if configuration.get("mode") != "reasoning":
            continue
        declared = configuration.get("reasoning_protocol")
        name = declared.get("name") if isinstance(declared, dict) else STANDARD_REASONING_PROTOCOL
        protocols.add(name)
    if len(protocols) > 1:
        raise ValueError("Standard reasoning and reasoning-no-clarification cannot enter one comparison")


def _ambiguity_value(row: Any, key: str, default: Any):
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def auto_confirm_contract(contract: Any) -> AutoConfirmation:
    """Build deterministic answers without consulting benchmark labels.

    Required ambiguities keep contract order. Nonempty options are joined with
    the fixed policy prefix and full-width semicolon separator; rows without
    options use the model-resolved wording verbatim. The SN answer model caps
    one answer at 2,000 characters, so an oversized fallback is rejected
    rather than silently truncated.
    """
    answers: list[dict[str, str]] = []
    decisions: list[dict[str, Any]] = []
    resolved_question = str(getattr(contract, "resolved_question", "")).strip()
    for raw in getattr(contract, "ambiguities", ()):
        required = _ambiguity_value(raw, "required", True)
        if required is False:
            continue
        ambiguity_id = str(_ambiguity_value(raw, "id", "")).strip()
        question = str(_ambiguity_value(raw, "question", ""))
        options = _ambiguity_value(raw, "options", ()) or ()
        options = [str(option).strip() for option in options if str(option).strip()]
        if options:
            answer = _ALL_OPTIONS_PREFIX + "；".join(options)
            source = "all_options"
        else:
            answer = resolved_question
            source = "resolved_question_fallback"
        if not ambiguity_id or not answer:
            raise ValueError("Auto-confirm policy cannot answer an ambiguity without an id and text")
        if len(answer) > _ANSWER_MAX_CHARS:
            raise ValueError("Auto-confirm answer exceeds SN's 2000-character limit")
        answers.append({"id": ambiguity_id, "answer": answer})
        decisions.append({
            "id": ambiguity_id,
            "question": question,
            "options": list(_ambiguity_value(raw, "options", ()) or ()),
            "selected_answer": answer,
            "answer_source": source,
        })
    return AutoConfirmation(answers=answers, decisions=decisions, policy=dict(AUTO_CONFIRM_POLICY))
