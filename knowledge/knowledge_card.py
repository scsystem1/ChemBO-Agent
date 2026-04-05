"""
Knowledge card models for the rebuilt knowledge system.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from knowledge.llm_adapter import RAGLLMAdapter
    from knowledge.local_rag import EvidenceBundle, RetrievedChunk


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


def build_cards_from_evidence_bundle(
    bundle: "EvidenceBundle",
    problem_spec: dict[str, Any],
    llm_adapter: "RAGLLMAdapter | None" = None,
) -> list[KnowledgeCard]:
    """Convert a retrieval evidence bundle into traceable knowledge cards."""
    del llm_adapter  # Reserved for future card synthesis enhancements.

    cards: list[KnowledgeCard] = []
    family = (
        str(problem_spec.get("reaction", {}).get("family", "")).strip().upper()
        or str(problem_spec.get("reaction_type", "")).strip().upper()
        or bundle.plan.precedent.reaction_family
    )
    scope = "target" if str(problem_spec.get("reaction", {}).get("reaction_smiles", "")).strip() else "general"

    if bundle.mechanism_summary:
        cards.append(
            KnowledgeCard(
                title=f"{family or 'Reaction'} mechanism guidance",
                category="mechanistic",
                claim=bundle.mechanism_summary,
                confidence=_confidence_label(max(len(bundle.mechanism_chunks), 1) / 3.0),
                reaction_families=[family] if family else [],
                actionable_for=["hypothesis_generation", "bo_config", "result_interpretation", "reconfiguration"],
                scope=scope,
                tags=["source:local_rag", "path:mechanism"],
                evidence=[_chunk_to_evidence(chunk) for chunk in bundle.mechanism_chunks[:3]],
            )
        )

    for role, role_evidence in bundle.role_evidence.items():
        if not role_evidence.top_values:
            continue
        claim = (
            f"Precedent retrieval most frequently reports {', '.join(role_evidence.top_values[:3])} "
            f"for {role.replace('_', ' ')}."
        )
        evidence = [_chunk_to_evidence(chunk) for chunk in role_evidence.supporting_chunks[:3]]
        if evidence:
            evidence[0].metadata.setdefault("role", role)
            evidence[0].metadata["top_values"] = list(role_evidence.top_values[:8])
            evidence[0].metadata["role_confidence"] = float(role_evidence.confidence)
        cards.append(
            KnowledgeCard(
                title=f"{role.replace('_', ' ').title()} precedent prior",
                category="reagent_prior",
                claim=claim,
                confidence=_confidence_label(role_evidence.confidence),
                reaction_families=[family] if family else [],
                variables_affected=[role],
                actionable_for=["warm_start", "hypothesis_generation", "reconfiguration"],
                scope="target",
                tags=["source:local_rag", "path:precedent", f"role:{role}"],
                evidence=evidence,
            )
        )

    for chunk in bundle.property_chunks[:2]:
        snippet = chunk.compressed_content or chunk.content
        cards.append(
            KnowledgeCard(
                title=f"Property note from {chunk.source_locator()}",
                category="property",
                claim=snippet[:260],
                confidence="low",
                reaction_families=[family] if family else [],
                actionable_for=["hypothesis_generation", "warm_start"],
                scope="general",
                tags=["source:local_rag", "path:property"],
                evidence=[_chunk_to_evidence(chunk)],
            )
        )

    return cards


def cards_to_structured_priors(
    cards: list[dict[str, Any] | KnowledgeCard],
    variables: list[dict[str, Any]],
) -> dict[str, Any]:
    """Project knowledge cards into the legacy structured-prior format."""
    validated = _validate_cards(cards)
    priors = {
        "warm_start_bias": {},
        "continuous_priors": {},
        "hard_constraints": [],
        "soft_priors": [],
        "known_interactions": [],
        "prior_granularity": "coarse",
        "fallback_reason": None,
    }

    mechanistic_claims = [card.claim for card in validated if card.category == "mechanistic"]
    property_claims = [card.claim for card in validated if card.category == "property"]
    priors["soft_priors"] = mechanistic_claims[:3] + property_claims[:2]

    role_cards: dict[str, list[KnowledgeCard]] = {}
    for card in validated:
        if card.category != "reagent_prior":
            continue
        for role in card.variables_affected:
            role_cards.setdefault(role, []).append(card)

    for variable in variables:
        role = str(variable.get("role", "other")).strip().lower()
        labels = _domain_labels(variable)
        if not labels:
            continue
        matching_cards = role_cards.get(role, [])
        if not matching_cards:
            continue
        scores = {label: 0.0 for label in labels}
        for card in matching_cards:
            top_values = _card_top_values(card)
            base_weight = _confidence_score(card.confidence)
            for index, value in enumerate(top_values[:8]):
                decayed = base_weight * max(0.2, 1.0 - 0.12 * index)
                for label in labels:
                    if _labels_match(label, value):
                        scores[label] += decayed
        if max(scores.values(), default=0.0) <= 0:
            continue
        floor = min(max(_confidence_score(card.confidence) for card in matching_cards) * 0.05, 0.1)
        priors["warm_start_bias"][str(variable.get("name", ""))] = _normalize_weights(
            {label: (score if score > 0 else floor) for label, score in scores.items()}
        )

    if not validated:
        priors["fallback_reason"] = "No knowledge cards available."
    elif not priors["warm_start_bias"]:
        priors["fallback_reason"] = "Knowledge cards available but no role-specific warm-start priors matched."
    else:
        priors["prior_granularity"] = "targeted"
    return priors


def _validate_cards(cards: list[dict[str, Any] | KnowledgeCard]) -> list[KnowledgeCard]:
    validated: list[KnowledgeCard] = []
    for item in cards:
        try:
            card = item if isinstance(item, KnowledgeCard) else KnowledgeCard.from_dict(item)
        except Exception:
            continue
        validated.append(card)
    return validated


def _chunk_to_evidence(chunk: "RetrievedChunk") -> KnowledgeEvidence:
    metadata = dict(chunk.metadata)
    citation = str(metadata.get("doi") or metadata.get("document_title") or metadata.get("source_file") or "").strip()
    return KnowledgeEvidence(
        source_type=str(metadata.get("source_type") or chunk.collection or "local_rag"),
        document_id=str(metadata.get("document_id") or metadata.get("source_file") or chunk.collection),
        chunk_id=chunk.chunk_id,
        locator=chunk.source_locator(),
        citation=citation,
        snippet=(chunk.compressed_content or chunk.content)[:500],
        metadata=metadata,
    )


def _domain_labels(variable: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for entry in variable.get("domain", []):
        if isinstance(entry, dict):
            label = entry.get("label") or entry.get("name") or entry.get("value")
            if label:
                labels.append(str(label))
        else:
            labels.append(str(entry))
    return [label for label in labels if label]


def _labels_match(label: str, value: str) -> bool:
    left = str(label or "").strip().lower()
    right = str(value or "").strip().lower()
    return bool(left and right and (left == right or left in right or right in left))


def _card_top_values(card: KnowledgeCard) -> list[str]:
    if not card.evidence:
        return []
    metadata = card.evidence[0].metadata
    raw = metadata.get("top_values", [])
    return [str(item).strip() for item in raw if str(item).strip()]


def _confidence_score(confidence: str) -> float:
    return {"high": 0.9, "medium": 0.65, "low": 0.35}.get(str(confidence).strip().lower(), 0.35)


def _confidence_label(score: float) -> str:
    if score >= 0.75:
        return "high"
    if score >= 0.45:
        return "medium"
    return "low"


def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    total = sum(max(float(value), 0.0) for value in weights.values())
    if total <= 0:
        count = max(len(weights), 1)
        return {key: 1.0 / count for key in weights}
    return {key: max(float(value), 0.0) / total for key, value in weights.items()}


__all__ = [
    "KnowledgeCard",
    "KnowledgeEvidence",
    "VALID_ACTIONABLE_FOR",
    "VALID_CARD_CATEGORIES",
    "VALID_CARD_SCOPES",
    "VALID_CONFIDENCES",
    "VALID_LEAKAGE_STATES",
    "build_cards_from_evidence_bundle",
    "cards_to_structured_priors",
    "format_cards_for_context",
]
