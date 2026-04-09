"""
Multi-source knowledge augmentation pipeline for ChemBO.
"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from config.settings import Settings
from core.problem_loader import VALID_VARIABLE_ROLES
from knowledge.connectors import (
    LocalRAGConnector,
    PubChemConnector,
    SemanticScholarConnector,
    WebSearchConnector,
)
from knowledge.knowledge_card import (
    VALID_ACTIONABLE_FOR,
    KnowledgeCard,
    KnowledgeEvidence,
    build_cards_from_evidence_bundle,
    cards_to_structured_priors,
    format_cards_for_context,
)
from knowledge.leakage_filter import FilteredChunk, LeakageFilter
from knowledge.llm_adapter import RAGLLMAdapter
from knowledge.local_rag import (
    EvidenceBundle,
    MechanismQuery,
    PrecedentQuery,
    PropertyQuery,
    ReactionQuery,
    ReactionRetrievalPlan,
    RetrievedChunk as LocalRetrievedChunk,
    RetrievalResult,
    RoleEvidence,
    _offline_compress,
    tokenize_chemistry_text,
)

logger = logging.getLogger(__name__)


VALID_QUERY_INTENTS = {
    "mechanism",
    "precedent",
    "reagent_property",
    "selectivity",
    "side_reaction",
    "substrate_scope",
    "solvent_effect",
    "temperature_or_concentration",
}
REQUIRED_QUERY_INTENTS = [
    "mechanism",
    "precedent",
    "reagent_property",
    "selectivity",
    "side_reaction",
    "substrate_scope",
    "solvent_effect",
    "temperature_or_concentration",
]
REAGENT_LIKE_ROLES = {
    "ligand",
    "base",
    "solvent",
    "catalyst_precursor",
    "additive",
    "oxidant",
    "reductant",
}
WEB_ALLOWED_INTENTS = {
    "precedent",
    "selectivity",
    "side_reaction",
    "substrate_scope",
    "temperature_or_concentration",
}
PROPERTY_LIKE_INTENTS = {"reagent_property", "solvent_effect"}
SOURCE_PRIORITY = {
    "local_rag": 4,
    "semantic_scholar": 3,
    "pubchem": 2,
    "web": 1,
}
OUTCOME_PATTERN = re.compile(
    r"(?:yield|conversion|selectivity|ee|er|dr)\s*[:=]?\s*\d+(?:\.\d+)?\s*%",
    re.IGNORECASE,
)
ROLE_ALIASES = {
    "catalyst": "catalyst_precursor",
    "temp": "temperature",
    "temperature_c": "temperature",
    "conc": "concentration",
    "concentration_m": "concentration",
}
INTENT_ALIASES = {
    "mechanistic": "mechanism",
    "property": "reagent_property",
    "reagent_prior": "reagent_property",
    "side_reactions": "side_reaction",
    "scope": "substrate_scope",
    "solvent": "solvent_effect",
    "temperature": "temperature_or_concentration",
    "concentration": "temperature_or_concentration",
}


@dataclass
class EntityHint:
    name: str = ""
    smiles: str = ""
    role: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "smiles": self.smiles,
            "role": self.role,
        }


@dataclass
class RetrievalQuery:
    id: str
    intent: str
    query_text: str
    target_sources: list[str]
    focus_roles: list[str] = field(default_factory=list)
    entities: list[EntityHint] = field(default_factory=list)
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "intent": self.intent,
            "query_text": self.query_text,
            "target_sources": list(self.target_sources),
            "focus_roles": list(self.focus_roles),
            "entities": [item.to_dict() for item in self.entities],
            "rationale": self.rationale,
        }


@dataclass
class AggregatedChunk:
    content: str
    source_type: str
    source_id: str
    query_ids: list[str]
    intents: list[str]
    relevance_score: float
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def collection(self) -> str:
        return str(self.metadata.get("collection", "")).strip()

    @property
    def query(self) -> str:
        return str(self.metadata.get("query_text", "")).strip()

    @property
    def short_source(self) -> str:
        short_source = str(self.metadata.get("short_source", "")).strip()
        if short_source:
            return short_source
        return self.source_id


@dataclass
class EvidenceSnippet:
    snippet_id: str
    chunk_ref: str
    text: str
    source_type: str
    source_id: str
    citation: str
    query_ids: list[str]
    intents: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snippet_id": self.snippet_id,
            "chunk_ref": self.chunk_ref,
            "text": self.text,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "citation": self.citation,
            "query_ids": list(self.query_ids),
            "intents": list(self.intents),
            "metadata": dict(self.metadata),
        }


def generate_retrieval_queries(
    problem_spec: dict[str, Any],
    settings: Settings,
    llm_adapter: RAGLLMAdapter | None = None,
) -> tuple[list[RetrievalQuery], list[str]]:
    adapter = llm_adapter or RAGLLMAdapter(settings=settings)
    validation_notes: list[str] = []
    problem_payload = _problem_summary_payload(problem_spec)
    heuristic_queries = _generate_heuristic_queries(problem_spec)
    response_queries: list[RetrievalQuery] = []

    system_prompt = (
        "You are a chemistry retrieval planner for a reaction-optimization agent. "
        "Generate 8-12 structured retrieval queries that jointly build a full picture of the target reaction. "
        "You must cover mechanism, precedents, reagent properties, selectivity drivers, side reactions, "
        "substrate scope, solvent effects, and temperature/concentration sensitivity. "
        "Return strict JSON with key 'queries'. Each query must contain: "
        "id, intent, query_text, target_sources, focus_roles, entities, rationale. "
        "Allowed intents: mechanism, precedent, reagent_property, selectivity, side_reaction, substrate_scope, "
        "solvent_effect, temperature_or_concentration. "
        "Allowed sources: local_rag, semantic_scholar, pubchem, web. "
        "Always include local_rag. Use online sources selectively based on the query and entities. "
        "Do not include dataset paths, yields, conversions, or experimental outcomes."
    )
    user_prompt = (
        "Plan retrieval queries for this reaction optimization problem.\n"
        f"{json.dumps(problem_payload, ensure_ascii=False, indent=2)}"
    )
    response = adapter.invoke_json(
        "generate_retrieval_queries",
        system_prompt,
        user_prompt,
        {"queries": []},
        max_tokens_override=int(getattr(settings, "augmentation_llm_max_tokens", 4096)),
    )

    raw_queries = response.payload.get("queries", []) if isinstance(response.payload, dict) else []
    if response.used_fallback:
        validation_notes.append(f"Query planner fallback: {response.error or 'invalid response'}")
    entity_index = _collect_entities_by_role(problem_spec)
    for raw_query in raw_queries if isinstance(raw_queries, list) else []:
        query = _normalize_retrieval_query(raw_query, entity_index)
        if query is None:
            continue
        query.target_sources = _normalize_target_sources(query, validation_notes)
        response_queries.append(query)

    missing_intents = [intent for intent in REQUIRED_QUERY_INTENTS if intent not in {item.intent for item in response_queries}]
    if missing_intents or len(response_queries) < 8:
        validation_notes.append("Supplemented retrieval queries with heuristic defaults to reach coverage and count targets.")

    merged_queries = _merge_queries_with_heuristics(response_queries, heuristic_queries, validation_notes)
    final_queries = merged_queries[:12]
    for index, query in enumerate(final_queries, start=1):
        query.id = f"Q{index}"
    return final_queries, validation_notes


def execute_multi_source(
    queries: list[RetrievalQuery],
    problem_spec: dict[str, Any],
    settings: Settings,
) -> tuple[list[AggregatedChunk], dict[str, Any]]:
    del problem_spec
    local_connector = LocalRAGConnector(settings=settings)
    s2_connector = SemanticScholarConnector(api_key=settings.semantic_scholar_api_key)
    pubchem_connector = PubChemConnector()
    web_connector = WebSearchConnector(
        api_key=settings.tavily_api_key,
        include_domains=list(settings.web_search_domains or []) or None,
    )

    chunks: list[AggregatedChunk] = []
    retrieval_failures: list[str] = []
    per_query_counts: dict[str, dict[str, int]] = {}

    for query in queries:
        counts = {"local_rag": 0, "semantic_scholar": 0, "pubchem": 0, "web": 0}
        if "local_rag" in query.target_sources:
            try:
                local_chunks = local_connector.search(query.query_text, top_k=3)
                counts["local_rag"] = len(local_chunks)
                chunks.extend(_wrap_connector_chunks(local_chunks, query))
            except Exception as exc:  # pragma: no cover - defensive
                retrieval_failures.append(f"{query.id}:local_rag:{type(exc).__name__}: {exc}")
        if "semantic_scholar" in query.target_sources:
            try:
                s2_chunks = s2_connector.search(query.query_text, max_results=3)
                counts["semantic_scholar"] = len(s2_chunks)
                chunks.extend(_wrap_connector_chunks(s2_chunks, query))
            except Exception as exc:  # pragma: no cover - defensive
                retrieval_failures.append(f"{query.id}:semantic_scholar:{type(exc).__name__}: {exc}")
        if "pubchem" in query.target_sources:
            for entity in _pubchem_entities_for_query(query)[:2]:
                try:
                    lookup_name = entity.name or entity.smiles
                    pubchem_chunks = pubchem_connector.lookup_compound(lookup_name, fallback_smiles=entity.smiles)
                    counts["pubchem"] += len(pubchem_chunks)
                    chunks.extend(_wrap_connector_chunks(pubchem_chunks, query))
                except Exception as exc:  # pragma: no cover - defensive
                    retrieval_failures.append(f"{query.id}:pubchem:{type(exc).__name__}: {exc}")
        if "web" in query.target_sources:
            try:
                web_chunks = web_connector.search(query.query_text, max_results=2)
                counts["web"] = len(web_chunks)
                chunks.extend(_wrap_connector_chunks(web_chunks, query))
            except Exception as exc:  # pragma: no cover - defensive
                retrieval_failures.append(f"{query.id}:web:{type(exc).__name__}: {exc}")
        per_query_counts[query.id] = counts

    chunk_counts = {source: 0 for source in SOURCE_PRIORITY}
    for chunk in chunks:
        chunk_counts[chunk.source_type] = chunk_counts.get(chunk.source_type, 0) + 1
    return chunks, {
        "retrieval_failures": retrieval_failures,
        "per_query_chunk_counts": per_query_counts,
        "retrieved_by_source": chunk_counts,
    }


def deduplicate_chunks(chunks: list[AggregatedChunk]) -> list[AggregatedChunk]:
    by_source_id: dict[tuple[str, str], AggregatedChunk] = {}
    for chunk in chunks:
        key = (chunk.source_type, chunk.source_id)
        existing = by_source_id.get(key)
        if existing is None:
            by_source_id[key] = chunk
            continue
        merged = _merge_aggregated_chunks(existing, chunk)
        by_source_id[key] = merged

    sorted_chunks = sorted(by_source_id.values(), key=_aggregated_chunk_sort_key, reverse=True)
    deduped: list[AggregatedChunk] = []
    for chunk in sorted_chunks:
        duplicate = None
        for kept in deduped:
            if _token_overlap_ratio(chunk.content, kept.content) > 0.8:
                duplicate = kept
                break
        if duplicate is None:
            deduped.append(chunk)
            continue
        merged = _merge_aggregated_chunks(duplicate, chunk)
        deduped[deduped.index(duplicate)] = merged

    return deduped[:60]


def filter_and_sanitize(
    chunks: list[AggregatedChunk],
    problem_spec: dict[str, Any],
    settings: Settings,
) -> tuple[list[FilteredChunk], dict[str, Any]]:
    leakage_filter = LeakageFilter(problem_spec, strict_mode=bool(getattr(settings, "leakage_filter_strict", True)))
    filtered = leakage_filter.filter_chunks(chunks)
    usable = [item for item in filtered if item.is_usable and item.content.strip()]
    by_source: dict[str, int] = defaultdict(int)
    usable_by_source: dict[str, int] = defaultdict(int)
    for item in filtered:
        by_source[item.source_type] += 1
        if item.is_usable:
            usable_by_source[item.source_type] += 1
    summary = {
        "total": len(filtered),
        "usable": len(usable),
        "safe": sum(1 for item in filtered if item.leakage_risk == "safe"),
        "partial": sum(1 for item in filtered if item.leakage_risk == "partial"),
        "blocked": sum(1 for item in filtered if item.leakage_risk == "blocked"),
        "discarded": sum(1 for item in filtered if not item.is_usable),
        "by_source": dict(by_source),
        "usable_by_source": dict(usable_by_source),
    }
    return usable, summary


def build_evidence_snippets(
    filtered_chunks: list[FilteredChunk],
    queries: list[RetrievalQuery],
    settings: Settings,
    llm_adapter: RAGLLMAdapter | None = None,
) -> tuple[list[EvidenceSnippet], list[str]]:
    adapter = llm_adapter or RAGLLMAdapter(settings=settings)
    notes: list[str] = []
    selected = _select_snippet_candidates(filtered_chunks, queries, int(getattr(settings, "augmentation_snippet_cap", 36)))
    if not selected:
        return [], ["No filtered chunks available for snippet compression."]

    char_budget = int(getattr(settings, "augmentation_chunk_char_budget", 900))
    chunk_items: list[dict[str, Any]] = []
    ref_to_chunk: dict[str, FilteredChunk] = {}
    for index, filtered in enumerate(selected, start=1):
        ref = f"C{index:02d}"
        original = filtered.original
        focus_roles = _normalize_role_list(original.metadata.get("focus_roles", []))
        ref_to_chunk[ref] = filtered
        chunk_items.append(
            {
                "chunk_ref": ref,
                "source_type": original.source_type,
                "short_source": original.short_source,
                "query_ids": list(original.query_ids),
                "intents": list(original.intents),
                "focus_roles": focus_roles,
                "sanitized_content": filtered.content[:char_budget],
            }
        )

    system_prompt = (
        "You are compressing retrieved chemistry chunks into evidence snippets for downstream knowledge-card synthesis. "
        "Process each chunk independently. Do not merge information across chunk_ref values. "
        "Do not invent unsupported claims. Do not output any explicit numerical yield, conversion, selectivity, ee, er, or dr values. "
        "For each chunk, return at most one snippet, 1-2 sentences long, focused on mechanism, properties, precedents, selectivity, "
        "side reactions, substrate scope, solvent effects, or temperature/concentration sensitivity."
    )
    user_prompt = (
        "Compress these sanitized chunks.\n"
        "Return strict JSON with key 'snippets', where each item has: "
        "chunk_ref, use, snippet, kept_points, drop_reason.\n"
        f"{json.dumps(chunk_items, ensure_ascii=False, indent=2)}"
    )
    response = adapter.invoke_json(
        "build_evidence_snippets",
        system_prompt,
        user_prompt,
        {"snippets": []},
        max_tokens_override=int(getattr(settings, "augmentation_llm_max_tokens", 4096)),
    )
    raw_snippets = response.payload.get("snippets", []) if isinstance(response.payload, dict) else []
    snippets = _normalize_evidence_snippets(raw_snippets, ref_to_chunk)
    if response.used_fallback:
        notes.append(f"Snippet compression fallback: {response.error or 'invalid response'}")
    if not snippets:
        notes.append("Snippet compression yielded no usable snippets; falling back to heuristic compression.")
        snippets = _heuristic_snippets(ref_to_chunk)
    return snippets[: int(getattr(settings, "augmentation_snippet_cap", 36))], notes


def condense_and_build_cards(
    snippets: list[EvidenceSnippet],
    filtered_chunks: list[FilteredChunk],
    problem_spec: dict[str, Any],
    settings: Settings,
    llm_adapter: RAGLLMAdapter | None = None,
) -> tuple[list[KnowledgeCard], list[str]]:
    adapter = llm_adapter or RAGLLMAdapter(settings=settings)
    notes: list[str] = []
    if not snippets:
        return [], ["No evidence snippets available for knowledge-card synthesis."]

    family = _reaction_family(problem_spec)
    grouped = _group_snippets_for_prompt(snippets)
    system_prompt = (
        "You are synthesizing chemistry knowledge into traceable knowledge cards for Bayesian optimization. "
        "Return strict JSON with key 'cards'. Each card must contain: "
        "title, category, claim, confidence, reaction_families, variables_affected, actionable_for, scope, tags, evidence_refs. "
        "Allowed categories: mechanistic, reagent_prior, constraint, property, empirical_analogy, methodology. "
        "variables_affected must use reaction variable roles, not variable names. "
        "evidence_refs must reference provided snippet_id values exactly. "
        "Do not include explicit numerical yields, conversions, selectivities, ee, er, or dr values. "
        "If the evidence supports it, prioritize 2-4 reagent_prior cards for ligand/base/solvent/catalyst_precursor."
    )
    user_prompt = (
        "Synthesize 4-15 knowledge cards from these snippets.\n"
        f"REACTION_CONTEXT:\n{json.dumps({'reaction_family': family, 'variables': _variable_role_summary(problem_spec)}, ensure_ascii=False, indent=2)}\n\n"
        f"SNIPPETS_BY_INTENT:\n{json.dumps(grouped, ensure_ascii=False, indent=2)}"
    )
    response = adapter.invoke_json(
        "synthesize_knowledge_cards",
        system_prompt,
        user_prompt,
        {"cards": []},
        max_tokens_override=int(getattr(settings, "augmentation_llm_max_tokens", 4096)),
    )
    snippet_map = {snippet.snippet_id: snippet for snippet in snippets}
    cards = _normalize_cards_from_response(response.payload.get("cards", []) if isinstance(response.payload, dict) else [], snippet_map, family)
    if response.used_fallback:
        notes.append(f"Knowledge-card synthesis fallback: {response.error or 'invalid response'}")
    if cards:
        return cards[:15], notes

    fallback_cards = _local_rag_fallback_cards(filtered_chunks, problem_spec, adapter)
    if fallback_cards:
        notes.append("Knowledge-card synthesis produced no valid cards; used local-rag fallback card builder.")
        return fallback_cards[:15], notes
    notes.append("Knowledge-card synthesis produced no valid cards.")
    return [], notes


def run_knowledge_augmentation(
    problem_spec: dict[str, Any],
    settings: Settings,
) -> tuple[list[dict[str, Any]], dict[str, Any], str, dict[str, Any]]:
    llm_adapter = RAGLLMAdapter(settings=settings)
    try:
        queries, query_notes = generate_retrieval_queries(problem_spec, settings, llm_adapter=llm_adapter)
        retrieved_chunks, retrieval_meta = execute_multi_source(queries, problem_spec, settings)
        deduplicated_chunks = deduplicate_chunks(retrieved_chunks)
        filtered_chunks, filter_summary = filter_and_sanitize(deduplicated_chunks, problem_spec, settings)
        snippets, snippet_notes = build_evidence_snippets(filtered_chunks, queries, settings, llm_adapter=llm_adapter)
        cards, card_notes = condense_and_build_cards(snippets, filtered_chunks, problem_spec, settings, llm_adapter=llm_adapter)
        card_payloads = [card.to_dict() for card in cards]
        artifacts = {
            "queries": [query.to_dict() for query in queries],
            "query_validation_notes": query_notes,
            "retrieval_failures": retrieval_meta.get("retrieval_failures", []),
            "chunk_counts": {
                "retrieved_total": len(retrieved_chunks),
                "deduplicated_total": len(deduplicated_chunks),
                "usable_after_filter": len(filtered_chunks),
                "retrieved_by_source": retrieval_meta.get("retrieved_by_source", {}),
            },
            "per_query_chunk_counts": retrieval_meta.get("per_query_chunk_counts", {}),
            "leakage_filter_summary": filter_summary,
            "snippet_count": len(snippets),
            "card_count": len(card_payloads),
            "card_generation_notes": snippet_notes + card_notes,
        }
        kb_context = format_cards_for_context(card_payloads)
        kb_priors = cards_to_structured_priors(card_payloads, problem_spec.get("variables", []))
        return card_payloads, artifacts, kb_context, kb_priors
    except Exception as exc:  # pragma: no cover - defensive runtime fallback
        logger.warning("Knowledge augmentation failed; continuing without cards: %s", exc)
        artifacts = {
            "queries": [],
            "query_validation_notes": [],
            "retrieval_failures": [],
            "chunk_counts": {},
            "leakage_filter_summary": {},
            "snippet_count": 0,
            "card_count": 0,
            "card_generation_notes": [f"Knowledge augmentation failed: {type(exc).__name__}: {exc}"],
        }
        return [], artifacts, "", {"warm_start_bias": {}, "soft_priors": {}, "notes": []}


def _problem_summary_payload(problem_spec: dict[str, Any]) -> dict[str, Any]:
    variables = []
    for variable in problem_spec.get("variables", []) if isinstance(problem_spec.get("variables"), list) else []:
        if not isinstance(variable, dict):
            continue
        variables.append(
            {
                "name": str(variable.get("name", "")).strip(),
                "role": _normalize_role_name(variable.get("role")),
                "type": str(variable.get("type", "categorical")).strip(),
                "domain_preview": _domain_preview(variable)[:5],
                "description": str(variable.get("description", "")).strip(),
            }
        )
    reaction = problem_spec.get("reaction", {}) if isinstance(problem_spec.get("reaction"), dict) else {}
    return {
        "description": str(problem_spec.get("description") or problem_spec.get("raw_description") or "").strip(),
        "reaction_type": _reaction_family(problem_spec),
        "reaction_family": str(reaction.get("family", "")).strip().upper(),
        "reaction_smiles": str(reaction.get("reaction_smiles", "")).strip(),
        "substrates": reaction.get("substrates", []),
        "known_fixed_context": reaction.get("known_fixed_context", []),
        "variables": variables,
    }


def _generate_heuristic_queries(problem_spec: dict[str, Any]) -> list[RetrievalQuery]:
    family = _reaction_family(problem_spec) or "reaction"
    description = str(problem_spec.get("description") or problem_spec.get("raw_description") or "").strip()
    plan = ReactionRetrievalPlan.from_problem_spec(problem_spec)
    entities_by_role = _collect_entities_by_role(problem_spec)
    variable_roles = [role for role in plan.precedent.variable_roles if role != "other"]
    reagent_roles = [role for role in variable_roles if role in REAGENT_LIKE_ROLES]

    intent_specs: list[tuple[str, str, list[str], list[EntityHint]]] = [
        (
            "mechanism",
            f"{family} mechanism and catalytic cycle with emphasis on {'/'.join(variable_roles[:3]) or 'key condition effects'}",
            variable_roles[:3],
            _entities_for_roles(entities_by_role, reagent_roles[:2]),
        ),
        (
            "precedent",
            f"{family} literature precedents for condition optimization involving {'/'.join(variable_roles[:4]) or 'core reaction variables'}",
            variable_roles[:4],
            _entities_for_roles(entities_by_role, reagent_roles[:3]),
        ),
        (
            "reagent_property",
            f"Electronic, steric, acidity, basicity, and solubility properties relevant to {family} optimization",
            reagent_roles[:4],
            _entities_for_roles(entities_by_role, reagent_roles[:4]),
        ),
        (
            "selectivity",
            f"{family} selectivity drivers and condition-dependent selectivity trends",
            variable_roles[:4],
            _entities_for_roles(entities_by_role, reagent_roles[:2]),
        ),
        (
            "side_reaction",
            f"Common side reactions and suppression strategies in {family} chemistry",
            variable_roles[:4],
            _entities_for_roles(entities_by_role, reagent_roles[:2]),
        ),
        (
            "substrate_scope",
            f"{family} substrate scope and functional group tolerance under related conditions",
            variable_roles[:3],
            _entities_for_roles(entities_by_role, []),
        ),
        (
            "solvent_effect",
            f"Solvent polarity, coordination, and solvent choice effects in {family}",
            ["solvent"] if "solvent" in variable_roles else variable_roles[:2],
            _entities_for_roles(entities_by_role, ["solvent"]),
        ),
        (
            "temperature_or_concentration",
            f"Temperature and concentration sensitivity in {family} condition optimization",
            [role for role in variable_roles if role in {"temperature", "concentration"}] or variable_roles[:2],
            _entities_for_roles(entities_by_role, []),
        ),
    ]

    heuristic_queries: list[RetrievalQuery] = []
    for index, (intent, base_text, focus_roles, entities) in enumerate(intent_specs, start=1):
        query_text = base_text if not description else f"{base_text}. Context: {description[:220]}"
        heuristic_queries.append(
            RetrievalQuery(
                id=f"H{index}",
                intent=intent,
                query_text=query_text,
                target_sources=_normalize_target_sources(
                    RetrievalQuery(id=f"H{index}", intent=intent, query_text=query_text, target_sources=["local_rag", "semantic_scholar"], focus_roles=focus_roles, entities=entities),
                    [],
                ),
                focus_roles=focus_roles,
                entities=entities,
                rationale=f"Heuristic coverage query for {intent}.",
            )
        )

    for role in variable_roles:
        if len(heuristic_queries) >= 12:
            break
        entities = _entities_for_roles(entities_by_role, [role])
        query_text = f"{family} optimization sensitivity to {role.replace('_', ' ')} choices and how they affect mechanism and practical outcomes"
        heuristic_queries.append(
            RetrievalQuery(
                id=f"H{len(heuristic_queries) + 1}",
                intent="reagent_property" if role in REAGENT_LIKE_ROLES else "temperature_or_concentration",
                query_text=query_text,
                target_sources=_normalize_target_sources(
                    RetrievalQuery(
                        id=f"H{len(heuristic_queries) + 1}",
                        intent="reagent_property" if role in REAGENT_LIKE_ROLES else "temperature_or_concentration",
                        query_text=query_text,
                        target_sources=["local_rag", "semantic_scholar", "pubchem"],
                        focus_roles=[role],
                        entities=entities,
                    ),
                    [],
                ),
                focus_roles=[role],
                entities=entities,
                rationale=f"Heuristic role-specific query for {role}.",
            )
        )
    return heuristic_queries


def _merge_queries_with_heuristics(
    llm_queries: list[RetrievalQuery],
    heuristic_queries: list[RetrievalQuery],
    notes: list[str],
) -> list[RetrievalQuery]:
    seen_keys: set[tuple[str, str]] = set()
    deduped: list[RetrievalQuery] = []
    for query in llm_queries + heuristic_queries:
        key = (query.intent, query.query_text.strip().lower())
        if key in seen_keys:
            continue
        deduped.append(query)
        seen_keys.add(key)

    final_queries: list[RetrievalQuery] = []
    for intent in REQUIRED_QUERY_INTENTS:
        match = next((query for query in deduped if query.intent == intent and query not in final_queries), None)
        if match is not None:
            final_queries.append(match)
        else:
            fallback = next((query for query in heuristic_queries if query.intent == intent), None)
            if fallback is not None:
                notes.append(f"Added heuristic query for missing intent '{intent}'.")
                final_queries.append(fallback)
    for query in deduped:
        if query in final_queries:
            continue
        final_queries.append(query)
        if len(final_queries) >= 12:
            break
    while len(final_queries) < 8:
        extra = next((query for query in heuristic_queries if query not in final_queries), None)
        if extra is None:
            break
        final_queries.append(extra)
    return final_queries


def _normalize_retrieval_query(raw_query: Any, entity_index: dict[str, list[EntityHint]]) -> RetrievalQuery | None:
    if not isinstance(raw_query, dict):
        return None
    intent = _normalize_intent_name(raw_query.get("intent"))
    query_text = str(raw_query.get("query_text", "")).strip()
    if not intent or not query_text:
        return None
    focus_roles = _normalize_role_list(raw_query.get("focus_roles", []))
    entities = _normalize_entities(raw_query.get("entities", []))
    if not entities and focus_roles:
        entities = _entities_for_roles(entity_index, focus_roles)
    return RetrievalQuery(
        id=str(raw_query.get("id", "")).strip() or "Q",
        intent=intent,
        query_text=query_text,
        target_sources=[str(item).strip() for item in raw_query.get("target_sources", []) if str(item).strip()],
        focus_roles=focus_roles,
        entities=entities,
        rationale=str(raw_query.get("rationale", "")).strip(),
    )


def _normalize_target_sources(query: RetrievalQuery, notes: list[str]) -> list[str]:
    raw_sources = [str(source).strip().lower() for source in query.target_sources]
    valid_sources = [source for source in raw_sources if source in SOURCE_PRIORITY]
    invalid_sources = [source for source in raw_sources if source not in SOURCE_PRIORITY]
    for source in invalid_sources:
        notes.append(f"{query.id}: dropped invalid connector suggestion '{source}'.")

    normalized = ["local_rag"]
    has_entity = any(item.name or item.smiles for item in query.entities)
    for source in valid_sources:
        if source == "local_rag":
            continue
        if source == "pubchem" and not (query.intent in PROPERTY_LIKE_INTENTS and has_entity):
            notes.append(f"{query.id}: removed pubchem suggestion because the query lacks property-style entity support.")
            continue
        if source == "web" and query.intent not in WEB_ALLOWED_INTENTS:
            notes.append(f"{query.id}: removed web suggestion because intent '{query.intent}' is not web-routed.")
            continue
        if source not in normalized:
            normalized.append(source)

    if not any(source != "local_rag" for source in normalized):
        if query.intent in PROPERTY_LIKE_INTENTS and has_entity:
            normalized.append("pubchem")
            notes.append(f"{query.id}: added pubchem as default online source.")
        else:
            normalized.append("semantic_scholar")
            notes.append(f"{query.id}: added semantic_scholar as default online source.")
    return normalized


def _wrap_connector_chunks(chunks: list[Any], query: RetrievalQuery) -> list[AggregatedChunk]:
    wrapped: list[AggregatedChunk] = []
    for chunk in chunks:
        metadata = dict(getattr(chunk, "metadata", {}) or {})
        focus_roles = _normalize_role_list(list(query.focus_roles) + list(metadata.get("focus_roles", [])))
        metadata["focus_roles"] = focus_roles
        metadata["short_source"] = getattr(chunk, "short_source", "") or _build_short_source_from_metadata(getattr(chunk, "source_type", ""), metadata)
        metadata["citation"] = _build_citation(getattr(chunk, "source_type", ""), metadata)
        wrapped.append(
            AggregatedChunk(
                content=str(getattr(chunk, "content", "") or ""),
                source_type=str(getattr(chunk, "source_type", "") or "unknown"),
                source_id=str(getattr(chunk, "source_id", "") or metadata.get("document_id") or metadata.get("url") or "unknown"),
                query_ids=[query.id],
                intents=[query.intent],
                relevance_score=float(getattr(chunk, "relevance_score", 0.0) or 0.0),
                metadata=metadata,
            )
        )
    return wrapped


def _merge_aggregated_chunks(left: AggregatedChunk, right: AggregatedChunk) -> AggregatedChunk:
    winner = left if _aggregated_chunk_sort_key(left) >= _aggregated_chunk_sort_key(right) else right
    loser = right if winner is left else left
    metadata = dict(winner.metadata)
    duplicate_ids = list(metadata.get("duplicate_source_ids", []))
    duplicate_ids.append(loser.source_id)
    metadata["duplicate_source_ids"] = _dedupe_preserve(duplicate_ids)
    metadata["focus_roles"] = _normalize_role_list(list(left.metadata.get("focus_roles", [])) + list(right.metadata.get("focus_roles", [])))
    return AggregatedChunk(
        content=winner.content,
        source_type=winner.source_type,
        source_id=winner.source_id,
        query_ids=_dedupe_preserve(list(left.query_ids) + list(right.query_ids)),
        intents=_dedupe_preserve(list(left.intents) + list(right.intents)),
        relevance_score=max(float(left.relevance_score), float(right.relevance_score)),
        metadata=metadata,
    )


def _aggregated_chunk_sort_key(chunk: AggregatedChunk) -> tuple[int, int, float]:
    return (
        len(set(chunk.query_ids)),
        SOURCE_PRIORITY.get(chunk.source_type, 0),
        float(chunk.relevance_score),
    )


def _token_overlap_ratio(left: str, right: str) -> float:
    left_tokens = set(tokenize_chemistry_text(left))
    right_tokens = set(tokenize_chemistry_text(right))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(min(len(left_tokens), len(right_tokens)), 1)


def _select_snippet_candidates(
    filtered_chunks: list[FilteredChunk],
    queries: list[RetrievalQuery],
    cap: int,
) -> list[FilteredChunk]:
    sorted_chunks = sorted(filtered_chunks, key=_filtered_chunk_sort_key, reverse=True)
    per_query: dict[str, list[FilteredChunk]] = defaultdict(list)
    for chunk in sorted_chunks:
        original = chunk.original
        for query_id in original.query_ids:
            per_query[query_id].append(chunk)

    selected: list[FilteredChunk] = []
    seen_keys: set[tuple[str, str]] = set()
    for query in queries:
        taken = 0
        for chunk in per_query.get(query.id, []):
            key = (chunk.original.source_type, chunk.original.source_id)
            if key in seen_keys:
                continue
            selected.append(chunk)
            seen_keys.add(key)
            taken += 1
            if taken >= 3 or len(selected) >= cap:
                break
        if len(selected) >= cap:
            break

    if len(selected) < cap:
        for chunk in sorted_chunks:
            key = (chunk.original.source_type, chunk.original.source_id)
            if key in seen_keys:
                continue
            selected.append(chunk)
            seen_keys.add(key)
            if len(selected) >= cap:
                break
    return selected[:cap]


def _filtered_chunk_sort_key(chunk: FilteredChunk) -> tuple[int, int, float]:
    original = chunk.original
    return (
        len(set(original.query_ids)),
        SOURCE_PRIORITY.get(original.source_type, 0),
        float(original.relevance_score),
    )


def _normalize_evidence_snippets(raw_snippets: Any, ref_to_chunk: dict[str, FilteredChunk]) -> list[EvidenceSnippet]:
    if not isinstance(raw_snippets, list):
        return []
    normalized: list[EvidenceSnippet] = []
    for raw_item in raw_snippets:
        if not isinstance(raw_item, dict):
            continue
        chunk_ref = str(raw_item.get("chunk_ref", "")).strip()
        if not chunk_ref or chunk_ref not in ref_to_chunk:
            continue
        if raw_item.get("use") is False:
            continue
        text = str(raw_item.get("snippet", "")).strip()
        if not text:
            continue
        filtered = ref_to_chunk[chunk_ref]
        original = filtered.original
        normalized.append(
            EvidenceSnippet(
                snippet_id=f"S{len(normalized) + 1:02d}",
                chunk_ref=chunk_ref,
                text=text,
                source_type=original.source_type,
                source_id=original.source_id,
                citation=str(original.metadata.get("citation", "")).strip(),
                query_ids=list(original.query_ids),
                intents=list(original.intents),
                metadata={
                    **original.metadata,
                    "focus_roles": _normalize_role_list(original.metadata.get("focus_roles", [])),
                    "kept_points": [str(item).strip() for item in raw_item.get("kept_points", []) if str(item).strip()],
                },
            )
        )
    return normalized


def _heuristic_snippets(ref_to_chunk: dict[str, FilteredChunk]) -> list[EvidenceSnippet]:
    snippets: list[EvidenceSnippet] = []
    for chunk_ref, filtered in ref_to_chunk.items():
        original = filtered.original
        hint_text = " ".join(list(original.intents) + list(original.metadata.get("focus_roles", [])))
        text = _offline_compress(hint_text, filtered.content, max_sentences=2).strip()
        if not text:
            continue
        snippets.append(
            EvidenceSnippet(
                snippet_id=f"S{len(snippets) + 1:02d}",
                chunk_ref=chunk_ref,
                text=text,
                source_type=original.source_type,
                source_id=original.source_id,
                citation=str(original.metadata.get("citation", "")).strip(),
                query_ids=list(original.query_ids),
                intents=list(original.intents),
                metadata={**original.metadata, "focus_roles": _normalize_role_list(original.metadata.get("focus_roles", []))},
            )
        )
    return snippets


def _group_snippets_for_prompt(snippets: list[EvidenceSnippet]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for snippet in snippets:
        primary_intent = snippet.intents[0] if snippet.intents else "general"
        grouped[primary_intent].append(
            {
                "snippet_id": snippet.snippet_id,
                "chunk_ref": snippet.chunk_ref,
                "source_type": snippet.source_type,
                "citation": snippet.citation,
                "text": snippet.text,
                "focus_roles": _normalize_role_list(snippet.metadata.get("focus_roles", [])),
                "intents": list(snippet.intents),
            }
        )
    return dict(grouped)


def _normalize_cards_from_response(
    raw_cards: Any,
    snippet_map: dict[str, EvidenceSnippet],
    family: str,
) -> list[KnowledgeCard]:
    if not isinstance(raw_cards, list):
        return []
    normalized: list[KnowledgeCard] = []
    for raw_card in raw_cards:
        if not isinstance(raw_card, dict):
            continue
        evidence_refs = [str(item).strip() for item in raw_card.get("evidence_refs", []) if str(item).strip() in snippet_map]
        if not evidence_refs:
            continue
        payload = {
            "title": str(raw_card.get("title", "")).strip(),
            "category": _normalize_card_category(raw_card.get("category")),
            "claim": str(raw_card.get("claim", "")).strip(),
            "confidence": _normalize_confidence(raw_card.get("confidence")),
            "reaction_families": [family] if family else [],
            "variables_affected": _normalize_role_list(raw_card.get("variables_affected", [])),
            "actionable_for": _normalize_actionable_for(raw_card.get("actionable_for", []), _normalize_card_category(raw_card.get("category"))),
            "scope": _normalize_scope(raw_card.get("scope")),
            "leakage_state": "passed",
            "tags": _normalize_tag_list(raw_card.get("tags", [])),
            "evidence": [_snippet_to_evidence(snippet_map[ref]) for ref in evidence_refs[:3]],
        }
        _attach_reagent_prior_top_values(payload)
        reaction_families = [str(item).strip().upper() for item in raw_card.get("reaction_families", []) if str(item).strip()]
        if reaction_families:
            payload["reaction_families"] = reaction_families
        if _contains_outcome_numbers(payload["claim"]):
            continue
        try:
            normalized.append(KnowledgeCard.from_dict(payload))
        except Exception:
            continue
    return normalized


def _snippet_to_evidence(snippet: EvidenceSnippet) -> KnowledgeEvidence:
    metadata = dict(snippet.metadata)
    metadata["snippet_id"] = snippet.snippet_id
    metadata["chunk_ref"] = snippet.chunk_ref
    return KnowledgeEvidence(
        source_type=snippet.source_type,
        document_id=str(metadata.get("document_id") or metadata.get("source_file") or snippet.source_id),
        chunk_id=snippet.chunk_ref,
        locator=str(metadata.get("locator") or snippet.chunk_ref),
        citation=snippet.citation,
        snippet=snippet.text[:500],
        metadata=metadata,
    )


def _local_rag_fallback_cards(
    filtered_chunks: list[FilteredChunk],
    problem_spec: dict[str, Any],
    llm_adapter: RAGLLMAdapter | None,
) -> list[KnowledgeCard]:
    local_chunks = [_filtered_to_local_chunk(item) for item in filtered_chunks if item.original.source_type == "local_rag"]
    if not local_chunks:
        return []

    plan = ReactionRetrievalPlan.from_problem_spec(problem_spec)
    precedent_chunks = [chunk for chunk in local_chunks if "precedent" in chunk.metadata.get("intents", [])]
    mechanism_chunks = [chunk for chunk in local_chunks if "mechanism" in chunk.metadata.get("intents", [])]
    property_chunks = [chunk for chunk in local_chunks if any(intent in {"reagent_property", "solvent_effect", "temperature_or_concentration"} for intent in chunk.metadata.get("intents", []))]

    if not (precedent_chunks or mechanism_chunks or property_chunks):
        return []

    mechanism_text = " ".join((chunk.compressed_content or chunk.content) for chunk in mechanism_chunks[:3]).strip()
    mechanism_summary = _offline_compress(plan.mechanism.to_text(), mechanism_text, max_sentences=2) if mechanism_text else ""
    bundle = EvidenceBundle(
        plan=plan,
        role_evidence=_build_role_evidence_from_local_chunks(precedent_chunks, plan.precedent.variable_roles),
        mechanism_chunks=mechanism_chunks,
        mechanism_summary=mechanism_summary,
        property_chunks=property_chunks,
        precedent_result=RetrievalResult(query=ReactionQuery(reaction_family=plan.precedent.reaction_family), normalized_query=plan.precedent.to_text(), chunks=precedent_chunks),
        mechanism_result=RetrievalResult(query=ReactionQuery(reaction_family=plan.mechanism.reaction_family), normalized_query=plan.mechanism.to_text(), chunks=mechanism_chunks),
        property_result=RetrievalResult(query=ReactionQuery(), normalized_query=plan.property.to_text(), chunks=property_chunks),
        notes=["local-rag fallback"],
    )
    return build_cards_from_evidence_bundle(bundle, problem_spec, llm_adapter=llm_adapter)


def _filtered_to_local_chunk(filtered: FilteredChunk) -> LocalRetrievedChunk:
    original = filtered.original
    metadata = dict(original.metadata)
    metadata["intents"] = list(original.intents)
    return LocalRetrievedChunk(
        chunk_id=str(metadata.get("chunk_id") or original.source_id),
        content=filtered.content,
        collection=str(metadata.get("collection") or "supplementary"),
        metadata=metadata,
        compressed_content=filtered.content[:600],
        dense_score=float(metadata.get("dense_score", 0.0) or 0.0),
        sparse_score=float(metadata.get("sparse_score", 0.0) or 0.0),
        fusion_score=float(metadata.get("fusion_score", original.relevance_score) or 0.0),
        rerank_score=float(metadata.get("rerank_score", original.relevance_score) or 0.0),
    )


def _build_role_evidence_from_local_chunks(
    chunks: list[LocalRetrievedChunk],
    variable_roles: list[str],
) -> dict[str, RoleEvidence]:
    role_to_field = {
        "ligand": "ligands_norm",
        "base": "bases_norm",
        "solvent": "solvents_norm",
        "catalyst_precursor": "catalysts_norm",
    }
    role_evidence: dict[str, RoleEvidence] = {}
    for role in [item for item in variable_roles if item in role_to_field]:
        field = role_to_field[role]
        counts: dict[str, int] = {}
        supporting: list[LocalRetrievedChunk] = []
        for chunk in chunks:
            try:
                values = json.loads(chunk.metadata.get(field) or "[]")
            except (TypeError, json.JSONDecodeError):
                values = []
            normalized = [str(value).strip().lower() for value in values if str(value).strip()]
            if not normalized:
                continue
            supporting.append(chunk)
            for value in normalized:
                counts[value] = counts.get(value, 0) + 1
        if not counts:
            continue
        ordered = [item for item, _ in sorted(counts.items(), key=lambda entry: (-entry[1], entry[0]))[:8]]
        confidence = min(0.9, 0.3 + 0.12 * len(supporting) + 0.05 * min(len(ordered), 4))
        notes = [f"Most common {role.replace('_', ' ')}: {ordered[0]} ({counts[ordered[0]]} precedents)"]
        role_evidence[role] = RoleEvidence(
            role=role,
            top_values=ordered,
            supporting_chunks=supporting[:5],
            confidence=round(confidence, 3),
            notes=notes,
        )
    return role_evidence


def _collect_entities_by_role(problem_spec: dict[str, Any]) -> dict[str, list[EntityHint]]:
    entities: dict[str, list[EntityHint]] = defaultdict(list)
    reaction = problem_spec.get("reaction", {}) if isinstance(problem_spec.get("reaction"), dict) else {}
    for item in reaction.get("known_fixed_context", []) if isinstance(reaction.get("known_fixed_context"), list) else []:
        if not isinstance(item, dict):
            continue
        role = _normalize_role_name(item.get("role"))
        value = str(item.get("value", "")).strip()
        if role and value:
            entities[role].append(EntityHint(name=value, smiles=value if _looks_like_smiles(value) else "", role=role))
    for variable in problem_spec.get("variables", []) if isinstance(problem_spec.get("variables"), list) else []:
        if not isinstance(variable, dict):
            continue
        role = _normalize_role_name(variable.get("role"))
        if not role:
            continue
        smiles_map = variable.get("smiles_map", {}) if isinstance(variable.get("smiles_map"), dict) else {}
        for entry in variable.get("domain", [])[:3] if isinstance(variable.get("domain"), list) else []:
            if isinstance(entry, dict):
                name = str(entry.get("label") or entry.get("name") or entry.get("value") or "").strip()
                smiles = str(entry.get("smiles") or "").strip()
            else:
                name = str(entry).strip()
                smiles = str(smiles_map.get(name) or "").strip()
            if not name and not smiles:
                continue
            if not smiles and _looks_like_smiles(name):
                smiles = name
            entities[role].append(EntityHint(name=name, smiles=smiles, role=role))
    deduped: dict[str, list[EntityHint]] = {}
    for role, items in entities.items():
        seen: set[tuple[str, str]] = set()
        deduped[role] = []
        for item in items:
            key = (item.name.lower(), item.smiles.lower())
            if key in seen:
                continue
            deduped[role].append(item)
            seen.add(key)
    return deduped


def _entities_for_roles(entity_index: dict[str, list[EntityHint]], roles: list[str]) -> list[EntityHint]:
    entities: list[EntityHint] = []
    for role in roles:
        entities.extend(entity_index.get(role, [])[:2])
    return _dedupe_entities(entities)


def _dedupe_entities(entities: list[EntityHint]) -> list[EntityHint]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[EntityHint] = []
    for item in entities:
        key = (item.name.lower(), item.smiles.lower(), item.role)
        if key in seen:
            continue
        deduped.append(item)
        seen.add(key)
    return deduped


def _normalize_entities(raw_entities: Any) -> list[EntityHint]:
    if not isinstance(raw_entities, list):
        return []
    normalized: list[EntityHint] = []
    for raw_entity in raw_entities:
        if not isinstance(raw_entity, dict):
            continue
        role = _normalize_role_name(raw_entity.get("role"))
        name = str(raw_entity.get("name", "")).strip()
        smiles = str(raw_entity.get("smiles", "")).strip()
        if not name and not smiles:
            continue
        if not role:
            role = "other"
        normalized.append(EntityHint(name=name, smiles=smiles, role=role))
    return _dedupe_entities(normalized)


def _pubchem_entities_for_query(query: RetrievalQuery) -> list[EntityHint]:
    candidates = [item for item in query.entities if item.role in REAGENT_LIKE_ROLES and (item.name or item.smiles)]
    return _dedupe_entities(candidates)


def _domain_preview(variable: dict[str, Any]) -> list[str]:
    preview: list[str] = []
    for entry in variable.get("domain", []) if isinstance(variable.get("domain"), list) else []:
        if isinstance(entry, dict):
            value = entry.get("label") or entry.get("name") or entry.get("value")
        else:
            value = entry
        text = str(value or "").strip()
        if text:
            preview.append(text)
    return preview


def _normalize_role_name(role: Any) -> str:
    cleaned = str(role or "").strip().lower()
    cleaned = ROLE_ALIASES.get(cleaned, cleaned)
    if cleaned in VALID_VARIABLE_ROLES:
        return cleaned
    return ""


def _normalize_role_list(values: Any) -> list[str]:
    raw_values = values if isinstance(values, list) else [values]
    normalized: list[str] = []
    for value in raw_values:
        role = _normalize_role_name(value)
        if role and role not in normalized:
            normalized.append(role)
    return normalized


def _normalize_intent_name(intent: Any) -> str:
    cleaned = str(intent or "").strip().lower()
    cleaned = INTENT_ALIASES.get(cleaned, cleaned)
    return cleaned if cleaned in VALID_QUERY_INTENTS else ""


def _reaction_family(problem_spec: dict[str, Any]) -> str:
    reaction = problem_spec.get("reaction", {}) if isinstance(problem_spec.get("reaction"), dict) else {}
    return str(reaction.get("family") or problem_spec.get("reaction_type") or "").strip().upper()


def _variable_role_summary(problem_spec: dict[str, Any]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for variable in problem_spec.get("variables", []) if isinstance(problem_spec.get("variables"), list) else []:
        if not isinstance(variable, dict):
            continue
        summary.append(
            {
                "name": str(variable.get("name", "")).strip(),
                "role": _normalize_role_name(variable.get("role")),
                "type": str(variable.get("type", "")).strip() or "categorical",
            }
        )
    return summary


def _build_short_source_from_metadata(source_type: str, metadata: dict[str, Any]) -> str:
    if source_type == "semantic_scholar":
        title = str(metadata.get("title", "")).strip()
        year = str(metadata.get("year", "")).strip()
        return f"S2:{title} ({year})".strip()
    if source_type == "pubchem":
        return f"PubChem:CID_{metadata.get('cid', '?')}"
    if source_type == "web":
        return str(metadata.get("url", "")).strip()
    if source_type == "local_rag":
        collection = str(metadata.get("collection", "")).strip()
        source_file = str(metadata.get("source_file", "")).strip()
        return f"LocalRAG:{collection}/{source_file}".strip("/")
    return str(metadata.get("document_id") or metadata.get("source_file") or "").strip()


def _build_citation(source_type: str, metadata: dict[str, Any]) -> str:
    if source_type == "semantic_scholar":
        doi = str(metadata.get("doi", "")).strip()
        title = str(metadata.get("title", "")).strip()
        return doi or title
    if source_type == "pubchem":
        cid = str(metadata.get("cid", "")).strip()
        return f"PubChem CID {cid}" if cid else "PubChem"
    if source_type == "web":
        return str(metadata.get("url", "")).strip()
    return str(metadata.get("document_title") or metadata.get("source_file") or metadata.get("document_id") or "").strip()


def _normalize_card_category(category: Any) -> str:
    cleaned = str(category or "").strip().lower()
    aliases = {
        "mechanism": "mechanistic",
        "mechanistic": "mechanistic",
        "reagent_prior": "reagent_prior",
        "reagent": "reagent_prior",
        "constraint": "constraint",
        "property": "property",
        "empirical_analogy": "empirical_analogy",
        "analogy": "empirical_analogy",
        "methodology": "methodology",
        "method": "methodology",
    }
    return aliases.get(cleaned, "property")


def _normalize_confidence(confidence: Any) -> str:
    cleaned = str(confidence or "").strip().lower()
    return cleaned if cleaned in {"high", "medium", "low"} else "medium"


def _normalize_scope(scope: Any) -> str:
    cleaned = str(scope or "").strip().lower()
    return cleaned if cleaned in {"target", "analogous", "general"} else "general"


def _normalize_actionable_for(values: Any, category: str) -> list[str]:
    raw_values = values if isinstance(values, list) else [values]
    normalized = [str(value).strip() for value in raw_values if str(value).strip() in VALID_ACTIONABLE_FOR]
    if normalized:
        return _dedupe_preserve(normalized)
    defaults = {
        "mechanistic": ["hypothesis_generation", "result_interpretation"],
        "reagent_prior": ["warm_start", "hypothesis_generation"],
        "constraint": ["warm_start", "bo_config"],
        "property": ["hypothesis_generation", "warm_start"],
        "empirical_analogy": ["hypothesis_generation", "warm_start"],
        "methodology": ["bo_config", "reconfiguration"],
    }
    return defaults.get(category, ["hypothesis_generation"])


def _normalize_tag_list(values: Any) -> list[str]:
    raw_values = values if isinstance(values, list) else [values]
    return _dedupe_preserve([str(value).strip() for value in raw_values if str(value).strip()])


def _contains_outcome_numbers(text: str) -> bool:
    return bool(OUTCOME_PATTERN.search(str(text or "")))


def _attach_reagent_prior_top_values(payload: dict[str, Any]) -> None:
    if payload.get("category") != "reagent_prior":
        return
    variables_affected = [str(item).strip() for item in payload.get("variables_affected", []) if str(item).strip()]
    evidence = payload.get("evidence", [])
    if not evidence or not variables_affected:
        return
    role_to_field = {
        "ligand": "ligands_norm",
        "base": "bases_norm",
        "solvent": "solvents_norm",
        "catalyst_precursor": "catalysts_norm",
    }
    for role in variables_affected:
        field = role_to_field.get(role)
        if not field:
            continue
        collected: list[str] = []
        for item in evidence:
            metadata = getattr(item, "metadata", {}) if isinstance(item, KnowledgeEvidence) else item.get("metadata", {})
            raw_values = metadata.get(field, "[]")
            try:
                values = json.loads(raw_values) if isinstance(raw_values, str) else list(raw_values)
            except (TypeError, ValueError, json.JSONDecodeError):
                values = []
            collected.extend(str(value).strip() for value in values if str(value).strip())
        top_values = _dedupe_preserve(collected)
        if top_values:
            first_metadata = evidence[0].metadata if isinstance(evidence[0], KnowledgeEvidence) else evidence[0].setdefault("metadata", {})
            first_metadata["top_values"] = top_values[:8]
            break


def _looks_like_smiles(text: str) -> bool:
    value = str(text or "").strip()
    if len(value) < 4:
        return False
    return bool(re.search(r"[A-Z][A-Za-z0-9@\+\-\[\]\(\)=#/\\\.]{3,}", value))


def _dedupe_preserve(values: list[Any]) -> list[Any]:
    seen: list[Any] = []
    deduped: list[Any] = []
    for value in values:
        if value in seen:
            continue
        deduped.append(value)
        seen.append(value)
    return deduped


__all__ = [
    "AggregatedChunk",
    "EntityHint",
    "EvidenceSnippet",
    "RetrievalQuery",
    "build_evidence_snippets",
    "condense_and_build_cards",
    "deduplicate_chunks",
    "execute_multi_source",
    "filter_and_sanitize",
    "generate_retrieval_queries",
    "run_knowledge_augmentation",
]
