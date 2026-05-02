"""
Dict-based knowledge cards for the lightweight knowledge system.
"""
from __future__ import annotations

import uuid
from typing import Any


VALID_ACTIONABLE_FOR = {
    "hypothesis_generation",
    "warm_start",
    "select_candidate",
    "run_bo_iteration",
    "result_interpretation",
}
VALID_CARD_TYPES = {
    "mechanism",
    "reagent_property",
    "operating_window",
    "failure_mode",
    "constraint",
    "analogy",
    "interaction",
}
VALID_DECK_SCOPES = {"target", "campaign", "analogous", "general"}
VALID_CARD_STATUSES = {"active", "validated", "deprecated", "rejected"}
SCOPE_PRIORITY = {"target": 0, "campaign": 1, "analogous": 2, "general": 3}
TYPE_PRIORITY = {
    "constraint": 0,
    "mechanism": 1,
    "reagent_property": 2,
    "operating_window": 3,
    "failure_mode": 4,
    "interaction": 5,
    "analogy": 6,
}


def _normalize_str_list(values: list[str] | tuple[str, ...] | None) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in values or []:
        value = str(raw).strip()
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return normalized


def create_knowledge_card(
    text: str,
    card_type: str,
    *,
    scope: str = "general",
    confidence: float = 0.5,
    targets: list[str] | None = None,
    actionable_for: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    source_type: str = "llm_internal_prior",
    card_id: str | None = None,
    status: str = "active",
    created_at_iter: int = 0,
) -> dict[str, Any]:
    """Create a validated text-first knowledge card."""
    cleaned_text = _trim_card_text(str(text or "").strip())
    cleaned_type = str(card_type or "").strip()
    cleaned_scope = str(scope or "general").strip()
    cleaned_status = str(status or "active").strip()
    if not cleaned_text:
        raise ValueError("Knowledge card text must be non-empty.")
    if cleaned_type not in VALID_CARD_TYPES:
        raise ValueError(f"Invalid card_type '{cleaned_type}'.")
    if cleaned_scope not in VALID_DECK_SCOPES:
        raise ValueError(f"Invalid scope '{cleaned_scope}'.")
    if cleaned_status not in VALID_CARD_STATUSES:
        raise ValueError(f"Invalid status '{cleaned_status}'.")
    nodes = _normalize_str_list(actionable_for or [])
    invalid_nodes = [node for node in nodes if node not in VALID_ACTIONABLE_FOR]
    if invalid_nodes:
        raise ValueError(f"Invalid actionable_for entries: {invalid_nodes}")
    confidence_value = max(0.0, min(1.0, float(confidence or 0.0)))
    return {
        "card_id": card_id or f"kc_{uuid.uuid4().hex[:12]}",
        "text": cleaned_text,
        "card_type": cleaned_type,
        "scope": cleaned_scope,
        "confidence": round(confidence_value, 4),
        "status": cleaned_status,
        "targets": _normalize_str_list(targets or []),
        "actionable_for": nodes,
        "evidence_refs": _normalize_str_list(evidence_refs or []),
        "source_type": str(source_type or "llm_internal_prior").strip() or "llm_internal_prior",
        "validation": {
            "used_count": 0,
            "supported_count": 0,
            "contradicted_count": 0,
            "last_used_iter": None,
        },
        "created_at_iter": int(created_at_iter or 0),
    }


def format_deck_for_prompt(
    cards: list[dict[str, Any]],
    node_name: str,
    max_cards: int = 10,
) -> str:
    """Format active text cards for prompt injection."""
    selected = _filter_cards_for_node(cards, node_name)
    if not selected:
        return "[Active Knowledge Cards]\nNone available."
    selected = sorted(selected, key=_deck_sort_key)[: max(0, int(max_cards or 0))]
    lines = ["[Active Knowledge Cards]"]
    for index, card in enumerate(selected, start=1):
        targets = ", ".join(str(item) for item in card.get("targets", []) if str(item).strip()) or "all"
        lines.append(
            f"#{index} {card.get('card_id', '')} "
            f"({card.get('card_type', 'unknown')}, confidence={float(card.get('confidence', 0.0) or 0.0):.2f}, "
            f"scope={card.get('scope', 'general')}, targets=[{targets}]): "
            f"{str(card.get('text', '')).strip()}"
        )
    return "\n".join(lines)


def update_card_validation(
    card: dict[str, Any],
    *,
    used: bool = False,
    supported: bool | None = None,
    current_iteration: int | None = None,
) -> dict[str, Any]:
    """Update validation counters on a card and return a new dict."""
    updated = dict(card or {})
    validation = dict(updated.get("validation", {}) if isinstance(updated.get("validation"), dict) else {})
    validation.setdefault("used_count", 0)
    validation.setdefault("supported_count", 0)
    validation.setdefault("contradicted_count", 0)
    validation.setdefault("last_used_iter", None)
    if used:
        validation["used_count"] = int(validation.get("used_count", 0) or 0) + 1
        if current_iteration is not None:
            validation["last_used_iter"] = int(current_iteration)
    if supported is True:
        validation["supported_count"] = int(validation.get("supported_count", 0) or 0) + 1
    elif supported is False:
        validation["contradicted_count"] = int(validation.get("contradicted_count", 0) or 0) + 1
    updated["validation"] = validation
    return updated


def should_evict_card(card: dict[str, Any], current_iteration: int) -> bool:
    """Return True when a weak card should leave the active deck."""
    if str(card.get("status") or "active") == "rejected":
        return True
    if str(card.get("card_type") or "") == "constraint":
        return False
    validation = card.get("validation", {}) if isinstance(card.get("validation"), dict) else {}
    contradicted = int(validation.get("contradicted_count", 0) or 0)
    supported = int(validation.get("supported_count", 0) or 0)
    if contradicted >= 2 and supported == 0:
        return True
    created = int(card.get("created_at_iter", 0) or 0)
    used = int(validation.get("used_count", 0) or 0)
    return used == 0 and int(current_iteration or 0) - created >= 8


def _filter_cards_for_node(cards: list[dict[str, Any]], node_name: str) -> list[dict[str, Any]]:
    node = str(node_name or "").strip()
    selected: list[dict[str, Any]] = []
    for raw in cards or []:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("status") or "active") not in {"active", "validated"}:
            continue
        actionable = [str(item).strip() for item in raw.get("actionable_for", []) if str(item).strip()]
        if node and actionable and node not in actionable:
            continue
        if not str(raw.get("text") or "").strip():
            continue
        selected.append(dict(raw))
    return selected


def _deck_sort_key(card: dict[str, Any]) -> tuple[int, int, float, int, str]:
    return (
        0 if str(card.get("card_type") or "") == "constraint" else 1,
        SCOPE_PRIORITY.get(str(card.get("scope") or "general"), 99),
        -float(card.get("confidence", 0.0) or 0.0),
        TYPE_PRIORITY.get(str(card.get("card_type") or ""), 99),
        str(card.get("card_id") or ""),
    )


def _trim_card_text(text: str, max_words: int = 50) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(" ,.;") + "."


__all__ = [
    "VALID_ACTIONABLE_FOR",
    "VALID_CARD_STATUSES",
    "VALID_CARD_TYPES",
    "VALID_DECK_SCOPES",
    "create_knowledge_card",
    "format_deck_for_prompt",
    "should_evict_card",
    "update_card_validation",
]
