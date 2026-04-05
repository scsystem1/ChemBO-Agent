"""
Knowledge card models for the rebuilt knowledge system.
"""
from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field, field_validator


VALID_CARD_CATEGORIES = {
    "mechanistic",
    "reagent_prior",
    "constraint",
    "property",
    "empirical_analogy",
    "methodology",
}

VALID_CONFIDENCES = {"high", "medium", "low"}
VALID_CARD_SCOPES = {"target", "analogous", "general"}
VALID_LEAKAGE_STATES = {"unchecked", "passed", "failed", "needs_review"}
VALID_ACTIONABLE_FOR = {
    "hypothesis_generation",
    "bo_config",
    "embedding_selection",
    "result_interpretation",
    "warm_start",
    "reconfiguration",
}


def _normalize_str_list(values: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in values:
        value = str(raw).strip()
        if not value:
            continue
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    return normalized


class KnowledgeEvidence(BaseModel):
    """Traceable evidence that supports a knowledge card claim."""

    source_type: str
    document_id: str
    chunk_id: str
    locator: str = ""
    citation: str = ""
    snippet: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_type", "document_id", "chunk_id", "snippet")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        cleaned = str(value).strip()
        if not cleaned:
            raise ValueError("Evidence text fields must be non-empty.")
        return cleaned

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="python")


class KnowledgeCard(BaseModel):
    """A traceable, qualitative unit of chemistry knowledge."""

    card_id: str = Field(default_factory=lambda: f"kc_{uuid.uuid4().hex[:12]}")
    title: str
    category: str
    claim: str
    confidence: str = "medium"
    reaction_families: list[str] = Field(default_factory=list)
    variables_affected: list[str] = Field(default_factory=list)
    actionable_for: list[str] = Field(default_factory=list)
    scope: str = "general"
    leakage_state: str = "unchecked"
    tags: list[str] = Field(default_factory=list)
    evidence: list[KnowledgeEvidence] = Field(default_factory=list)

    @field_validator("title", "claim")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        cleaned = str(value).strip()
        if not cleaned:
            raise ValueError("Card text fields must be non-empty.")
        return cleaned

    @field_validator("category")
    @classmethod
    def _validate_category(cls, value: str) -> str:
        cleaned = str(value).strip()
        if cleaned not in VALID_CARD_CATEGORIES:
            raise ValueError(f"Invalid category '{cleaned}'.")
        return cleaned

    @field_validator("confidence")
    @classmethod
    def _validate_confidence(cls, value: str) -> str:
        cleaned = str(value).strip().lower()
        if cleaned not in VALID_CONFIDENCES:
            raise ValueError(f"Invalid confidence '{cleaned}'.")
        return cleaned

    @field_validator("scope")
    @classmethod
    def _validate_scope(cls, value: str) -> str:
        cleaned = str(value).strip().lower()
        if cleaned not in VALID_CARD_SCOPES:
            raise ValueError(f"Invalid scope '{cleaned}'.")
        return cleaned

    @field_validator("leakage_state")
    @classmethod
    def _validate_leakage_state(cls, value: str) -> str:
        cleaned = str(value).strip().lower()
        if cleaned not in VALID_LEAKAGE_STATES:
            raise ValueError(f"Invalid leakage_state '{cleaned}'.")
        return cleaned

    @field_validator("reaction_families", "variables_affected", "tags")
    @classmethod
    def _validate_list_fields(cls, value: list[str]) -> list[str]:
        return _normalize_str_list(value)

    @field_validator("actionable_for")
    @classmethod
    def _validate_actionable_for(cls, value: list[str]) -> list[str]:
        cleaned = _normalize_str_list(value)
        invalid = [item for item in cleaned if item not in VALID_ACTIONABLE_FOR]
        if invalid:
            raise ValueError(f"Invalid actionable_for entries: {invalid}")
        return cleaned

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="python")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "KnowledgeCard":
        return cls.model_validate(payload)

    def to_context_string(self) -> str:
        confidence_marker = {"high": "★", "medium": "☆", "low": "○"}[self.confidence]
        scope_suffix = "" if self.scope == "target" else f" ({self.scope})"
        evidence_suffix = ""
        if self.evidence:
            evidence = self.evidence[0]
            cite = evidence.citation or evidence.document_id
            evidence_suffix = f" [{cite}]"
        return f"[{confidence_marker} {self.category.upper()}{scope_suffix}] {self.claim}{evidence_suffix}"


def format_cards_for_context(
    cards: list[dict[str, Any] | KnowledgeCard],
    current_node: str = "",
    max_cards: int = 30,
) -> str:
    """Format cards for later prompt injection."""
    validated: list[KnowledgeCard] = []
    for item in cards:
        try:
            card = item if isinstance(item, KnowledgeCard) else KnowledgeCard.from_dict(item)
        except Exception:
            continue
        if current_node and card.actionable_for and current_node not in card.actionable_for:
            continue
        validated.append(card)

    if not validated:
        return ""

    confidence_order = {"high": 0, "medium": 1, "low": 2}
    category_order = {
        "constraint": 0,
        "mechanistic": 1,
        "reagent_prior": 2,
        "property": 3,
        "empirical_analogy": 4,
        "methodology": 5,
    }
    validated.sort(
        key=lambda card: (
            confidence_order.get(card.confidence, 99),
            category_order.get(card.category, 99),
            card.title.lower(),
        )
    )
    return "\n".join(card.to_context_string() for card in validated[:max_cards])


__all__ = [
    "KnowledgeCard",
    "KnowledgeEvidence",
    "VALID_ACTIONABLE_FOR",
    "VALID_CARD_CATEGORIES",
    "VALID_CARD_SCOPES",
    "VALID_CONFIDENCES",
    "VALID_LEAKAGE_STATES",
    "format_cards_for_context",
]
