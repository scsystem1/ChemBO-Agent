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
VALID_DECK_SCOPES = {"target", "analogous", "general", "campaign"}
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


def create_knowledge_card(
    text: str,
    card_type: str,
    *,
    scope: str = "general",
    confidence: float = 0.5,
    targets: list[str] | None = None,
    actionable_for: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    source_type: str = "local_rag",
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
        "source_type": str(source_type or "local_rag").strip() or "local_rag",
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


def build_knowledge_guidance(
    cards: list[dict[str, Any] | KnowledgeCard],
    *,
    current_node: str = "",
    variables: list[dict[str, Any]] | None = None,
    max_cards: int = 12,
) -> list[dict[str, Any]]:
    """Project knowledge cards into a compact prompt-friendly payload."""
    validated = _validate_cards(cards)
    if not validated:
        return []

    node = str(current_node or "").strip()
    variable_names = {
        str(variable.get("name", "")).strip()
        for variable in (variables or [])
        if str(variable.get("name", "")).strip()
    }
    variable_roles = {
        str(variable.get("role", "")).strip()
        for variable in (variables or [])
        if str(variable.get("role", "")).strip()
    }
    confidence_order = {"high": 0, "medium": 1, "low": 2}
    category_order = {
        "constraint": 0,
        "mechanistic": 1,
        "reagent_prior": 2,
        "property": 3,
        "empirical_analogy": 4,
        "methodology": 5,
    }

    ranked: list[tuple[tuple[int, int, int, str], dict[str, Any]]] = []
    for card in validated:
        if node and card.actionable_for and node not in card.actionable_for:
            continue
        variable_relevance = sum(
            1 for item in card.variables_affected if item in variable_names or item in variable_roles
        )
        citation = ""
        if card.evidence:
            evidence = card.evidence[0]
            citation = evidence.citation or evidence.document_id or evidence.locator
        payload = {
            "card_id": card.card_id,
            "category": card.category,
            "claim": card.claim,
            "confidence": card.confidence,
            "variables_affected": list(card.variables_affected),
            "scope": card.scope,
            "citation": citation,
        }
        ranked.append(
            (
                (
                    confidence_order.get(card.confidence, 99),
                    -1 * variable_relevance,
                    category_order.get(card.category, 99),
                    card.title.lower(),
                ),
                payload,
            )
        )

    ranked.sort(key=lambda item: item[0])
    return [payload for _, payload in ranked[:max_cards]]


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
    variables: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Project knowledge cards into a lightweight prior cache for legacy consumers."""
    validated = _validate_cards(cards)
    variables = variables or []
    role_map: dict[str, list[str]] = {}
    variable_lookup: dict[str, dict[str, Any]] = {}
    for variable in variables:
        name = str(variable.get("name") or "").strip()
        role = str(variable.get("role") or "").strip()
        if not name:
            continue
        variable_lookup[name] = variable
        role_map.setdefault(name.lower(), []).append(name)
        if role:
            role_map.setdefault(role.lower(), []).append(name)

    confidence_weight = {"high": 0.85, "medium": 0.55, "low": 0.3}
    warm_start_bias: dict[str, dict[str, float]] = {}
    notes: list[str] = []

    for variable in variables:
        name = str(variable.get("name") or "").strip()
        domain = [str(item) for item in variable.get("domain", [])]
        if name and domain:
            warm_start_bias[name] = {entry: 0.1 for entry in domain}

    for card in validated:
        if card.category == "mechanistic":
            notes.append(card.claim)
        if card.category != "reagent_prior":
            continue
        targets: list[str] = []
        for affected in card.variables_affected:
            targets.extend(role_map.get(str(affected).lower(), []))
        targets = _normalize_str_list(targets)
        if not targets:
            continue
        top_values = []
        for evidence in card.evidence[:1]:
            top_values.extend(evidence.metadata.get("top_values", []) if isinstance(evidence.metadata, dict) else [])
        if not top_values:
            continue
        top_values_normalized = [str(value).strip().lower() for value in top_values]
        weight = confidence_weight.get(card.confidence, 0.3)
        for target in targets:
            domain_scores = warm_start_bias.setdefault(target, {})
            variable = variable_lookup.get(target, {})
            domain = [str(item) for item in variable.get("domain", [])]
            for entry in domain:
                normalized_entry = entry.strip().lower()
                domain_scores.setdefault(entry, 0.1)
                if normalized_entry in top_values_normalized:
                    rank = top_values_normalized.index(normalized_entry)
                    domain_scores[entry] += max(0.05, weight - 0.1 * rank)
        notes.append(card.claim)

    return {
        "warm_start_bias": warm_start_bias,
        "soft_priors": {"notes": _normalize_str_list(notes)},
        "notes": _normalize_str_list(notes),
    }


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


def _confidence_label(score: float) -> str:
    if score >= 0.75:
        return "high"
    if score >= 0.45:
        return "medium"
    return "low"


__all__ = [
    "KnowledgeCard",
    "KnowledgeEvidence",
    "VALID_CARD_STATUSES",
    "VALID_CARD_TYPES",
    "VALID_ACTIONABLE_FOR",
    "VALID_CARD_CATEGORIES",
    "VALID_CARD_SCOPES",
    "VALID_DECK_SCOPES",
    "VALID_CONFIDENCES",
    "VALID_LEAKAGE_STATES",
    "build_cards_from_evidence_bundle",
    "create_knowledge_card",
    "format_deck_for_prompt",
    "format_cards_for_context",
    "should_evict_card",
    "update_card_validation",
]
