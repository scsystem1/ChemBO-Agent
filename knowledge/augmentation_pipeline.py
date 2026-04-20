"""
Multi-source knowledge augmentation pipeline for ChemBO.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from config.settings import Settings
from core.prompt_utils import compact_json
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
    format_cards_for_context,
)
from knowledge.knowledge_state import (
    FACET_COMPOSITION,
    FACET_CONSTRAINT,
    FACET_FAILURE,
    FACET_MECHANISTIC,
    FACET_PRECEDENT,
    FACET_SCOPE,
    FACET_SELECTIVITY,
    FACET_WINDOW,
    PRIOR_CONSTRAINT,
    PRIOR_FAILURE,
    PRIOR_INTERACTION,
    PRIOR_VALUE_AVOIDANCE,
    PRIOR_VALUE_PREFERENCE,
    PRIOR_WINDOW,
    EvidenceRecord,
    KnowledgeSourceStatus,
    ServedPrior,
    build_coverage_report,
    build_derived_targets,
    build_node_digests,
    classify_evidence_scope,
    confidence_label,
    empty_knowledge_state,
    infer_knowledge_profile,
    required_facets_for_profile,
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
    FACET_MECHANISTIC,
    FACET_PRECEDENT,
    FACET_COMPOSITION,
    FACET_WINDOW,
    FACET_SELECTIVITY,
    FACET_FAILURE,
    FACET_SCOPE,
    FACET_CONSTRAINT,
}
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
    FACET_PRECEDENT,
    FACET_SELECTIVITY,
    FACET_FAILURE,
    FACET_SCOPE,
    FACET_CONSTRAINT,
}
PROPERTY_LIKE_INTENTS = {FACET_COMPOSITION, FACET_WINDOW}
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
    "mechanism": FACET_MECHANISTIC,
    "mechanistic": FACET_MECHANISTIC,
    "precedent": FACET_PRECEDENT,
    "reagent_property": FACET_COMPOSITION,
    "property": FACET_COMPOSITION,
    "reagent_prior": FACET_COMPOSITION,
    "selectivity": FACET_SELECTIVITY,
    "selectivity_driver": FACET_SELECTIVITY,
    "side_reaction": FACET_FAILURE,
    "side_reactions": FACET_FAILURE,
    "failure_mode_or_side_reaction": FACET_FAILURE,
    "substrate_scope": FACET_SCOPE,
    "scope": FACET_SCOPE,
    "scope_or_transferability": FACET_SCOPE,
    "solvent_effect": FACET_COMPOSITION,
    "solvent": FACET_COMPOSITION,
    "temperature_or_concentration": FACET_WINDOW,
    "temperature": FACET_WINDOW,
    "concentration": FACET_WINDOW,
    "operating_window": FACET_WINDOW,
    "constraint_or_safety": FACET_CONSTRAINT,
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
    family = _reaction_family(problem_spec)
    profile = infer_knowledge_profile(family)
    required_facets = required_facets_for_profile(profile)
    retrieval_plan = ReactionRetrievalPlan.from_problem_spec(problem_spec)
    problem_payload = _problem_summary_payload(problem_spec)
    problem_payload["knowledge_profile"] = profile
    problem_payload["required_facets"] = list(required_facets)
    problem_payload["derived_targets"] = build_derived_targets(problem_spec)
    problem_payload["retrieval_plan"] = retrieval_plan.model_dump(mode="python")
    heuristic_queries = _generate_heuristic_queries(problem_spec, required_facets=required_facets)
    response_queries: list[RetrievalQuery] = []

    system_prompt = (
        "You are a chemistry retrieval planner for a reaction-optimization agent. "
        "Generate 6-12 structured retrieval queries that jointly build a full picture of the target reaction. "
        "Use the provided knowledge_profile and required_facets to decide coverage, and keep the query set "
        "family-aware rather than forcing a homogeneous-organic template onto every reaction family. "
        "Return strict JSON with key 'queries'. Each query must contain: "
        "id, intent, query_text, target_sources, focus_roles, entities, rationale. "
        "Allowed intents: mechanistic_hypothesis, precedent, composition_or_reagent_effect, operating_window, "
        "selectivity_driver, failure_mode_or_side_reaction, scope_or_transferability, constraint_or_safety. "
        "Allowed sources: local_rag, semantic_scholar, pubchem, web. "
        "Always include local_rag. Use online sources selectively based on the query and entities. "
        "Do not include dataset paths, yields, conversions, or experimental outcomes."
    )
    user_prompt = (
        "Plan retrieval queries for this reaction optimization problem.\n"
        f"{compact_json(problem_payload)}"
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

    missing_intents = [intent for intent in required_facets if intent not in {item.intent for item in response_queries}]
    if missing_intents or len(response_queries) < min(len(required_facets), 8):
        validation_notes.append("Supplemented retrieval queries with heuristic defaults to reach coverage and count targets.")

    merged_queries = _merge_queries_with_heuristics(
        response_queries,
        heuristic_queries,
        validation_notes,
        required_intents=required_facets,
    )
    final_queries = merged_queries[:12]
    for index, query in enumerate(final_queries, start=1):
        query.id = f"Q{index}"
    return final_queries, validation_notes


def execute_multi_source(
    queries: list[RetrievalQuery],
    problem_spec: dict[str, Any],
    settings: Settings,
) -> tuple[list[AggregatedChunk], dict[str, Any]]:
    local_connector = LocalRAGConnector(settings=settings)
    s2_connector = SemanticScholarConnector(api_key=settings.semantic_scholar_api_key)
    pubchem_connector = PubChemConnector()
    web_connector = WebSearchConnector(
        api_key=settings.tavily_api_key,
        include_domains=list(settings.web_search_domains or []) or None,
    )

    family = _reaction_family(problem_spec)
    profile = infer_knowledge_profile(family)
    retrieval_plan = ReactionRetrievalPlan.from_problem_spec(problem_spec)
    chunks: list[AggregatedChunk] = []
    retrieval_failures: list[str] = []
    per_query_counts: dict[str, dict[str, int]] = {}
    source_health: list[dict[str, Any]] = []

    for query in queries:
        counts = {"local_rag": 0, "semantic_scholar": 0, "pubchem": 0, "web": 0}
        if "local_rag" in query.target_sources:
            started = time.perf_counter()
            local_chunks = []
            try:
                local_collections = _facet_collections(
                    query.intent,
                    retrieval_plan.prefer_sources,
                    profile=profile,
                    source="local_rag",
                )
                local_where = _local_where_clause(query.intent, family)
                local_chunks = local_connector.search(
                    query.query_text,
                    top_k=5,
                    collections=local_collections,
                    where=local_where,
                )
            except Exception as exc:  # pragma: no cover - defensive
                retrieval_failures.append(f"{query.id}:local_rag:{type(exc).__name__}: {exc}")
                local_connector.last_status = {
                    "status": "internal_error",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "result_count": 0,
                }
            counts["local_rag"] = len(local_chunks)
            chunks.extend(_wrap_connector_chunks(local_chunks, query))
            source_health.append(
                _source_status_payload(
                    source="local_rag",
                    query=query,
                    connector_status=getattr(local_connector, "last_status", {}),
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                )
            )
            failure = _status_to_failure_message(source_health[-1])
            if failure:
                retrieval_failures.append(failure)
        if "semantic_scholar" in query.target_sources:
            started = time.perf_counter()
            s2_chunks = []
            try:
                s2_chunks = s2_connector.search(query.query_text, max_results=3)
            except Exception as exc:  # pragma: no cover - defensive
                retrieval_failures.append(f"{query.id}:semantic_scholar:{type(exc).__name__}: {exc}")
                s2_connector.last_status = {
                    "status": "internal_error",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "result_count": 0,
                }
            counts["semantic_scholar"] = len(s2_chunks)
            chunks.extend(_wrap_connector_chunks(s2_chunks, query))
            status_payload = _source_status_payload(
                source="semantic_scholar",
                query=query,
                connector_status=getattr(s2_connector, "last_status", {}),
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
            source_health.append(status_payload)
            failure = _status_to_failure_message(status_payload)
            if failure:
                retrieval_failures.append(failure)
        if "pubchem" in query.target_sources:
            started = time.perf_counter()
            pubchem_messages: list[dict[str, Any]] = []
            for entity in _pubchem_entities_for_query(query)[:2]:
                try:
                    lookup_name = entity.name or entity.smiles
                    pubchem_chunks = pubchem_connector.lookup_compound(lookup_name, fallback_smiles=entity.smiles)
                    counts["pubchem"] += len(pubchem_chunks)
                    chunks.extend(_wrap_connector_chunks(pubchem_chunks, query))
                except Exception as exc:  # pragma: no cover - defensive
                    retrieval_failures.append(f"{query.id}:pubchem:{type(exc).__name__}: {exc}")
                    pubchem_connector.last_status = {
                        "status": "internal_error",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "result_count": 0,
                    }
                pubchem_messages.append(dict(getattr(pubchem_connector, "last_status", {})))
            status_payload = _source_status_payload(
                source="pubchem",
                query=query,
                connector_status=_merge_connector_statuses(pubchem_messages, counts["pubchem"]),
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
            source_health.append(status_payload)
            failure = _status_to_failure_message(status_payload)
            if failure:
                retrieval_failures.append(failure)
        if "web" in query.target_sources:
            started = time.perf_counter()
            web_chunks = []
            try:
                web_chunks = web_connector.search(query.query_text, max_results=2)
            except Exception as exc:  # pragma: no cover - defensive
                retrieval_failures.append(f"{query.id}:web:{type(exc).__name__}: {exc}")
                web_connector.last_status = {
                    "status": "internal_error",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "result_count": 0,
                }
            counts["web"] = len(web_chunks)
            chunks.extend(_wrap_connector_chunks(web_chunks, query))
            status_payload = _source_status_payload(
                source="web",
                query=query,
                connector_status=getattr(web_connector, "last_status", {}),
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
            source_health.append(status_payload)
            failure = _status_to_failure_message(status_payload)
            if failure:
                retrieval_failures.append(failure)
        per_query_counts[query.id] = counts

    chunk_counts = {source: 0 for source in SOURCE_PRIORITY}
    for chunk in chunks:
        chunk_counts[chunk.source_type] = chunk_counts.get(chunk.source_type, 0) + 1
    return chunks, {
        "retrieval_failures": _dedupe_preserve(retrieval_failures),
        "per_query_chunk_counts": per_query_counts,
        "retrieved_by_source": chunk_counts,
        "source_health": source_health,
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

    ref_to_chunk: dict[str, FilteredChunk] = {}
    for index, filtered in enumerate(selected, start=1):
        ref_to_chunk[f"C{index:02d}"] = filtered
    heuristic_snippets = _heuristic_snippets(ref_to_chunk)

    if not bool(getattr(settings, "augmentation_llm_snippet_compression", False)):
        return heuristic_snippets[: int(getattr(settings, "augmentation_snippet_cap", 36))], notes

    char_budget = int(getattr(settings, "augmentation_chunk_char_budget", 900))
    chunk_items: list[dict[str, Any]] = []
    for index, filtered in enumerate(selected[: min(len(selected), 6)], start=1):
        ref = f"C{index:02d}"
        original = filtered.original
        focus_roles = _normalize_role_list(original.metadata.get("focus_roles", []))
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
        f"{compact_json(chunk_items)}"
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
        return heuristic_snippets[: int(getattr(settings, "augmentation_snippet_cap", 36))], notes
    merged = {snippet.chunk_ref: snippet for snippet in heuristic_snippets}
    for snippet in snippets:
        merged[snippet.chunk_ref] = snippet
    ordered_refs = sorted(merged)
    return [merged[ref] for ref in ordered_refs][: int(getattr(settings, "augmentation_snippet_cap", 36))], notes


def build_evidence_records(
    snippets: list[EvidenceSnippet],
    problem_spec: dict[str, Any],
) -> list[EvidenceRecord]:
    family = _reaction_family(problem_spec)
    records: list[EvidenceRecord] = []
    for index, snippet in enumerate(snippets, start=1):
        metadata = dict(snippet.metadata)
        evidence_family = str(metadata.get("reaction_family", "") or "").strip().upper()
        scope, family_match = classify_evidence_scope(
            target_family=family,
            evidence_family=evidence_family,
            source_type=snippet.source_type,
            metadata=metadata,
        )
        variables = _normalize_role_list(metadata.get("focus_roles", []))
        if not variables:
            variables = _infer_variables_from_text(snippet.text, problem_spec)
        records.append(
            EvidenceRecord(
                evidence_id=f"ev_{index:03d}",
                facet=_primary_facet(snippet.intents),
                scope=scope,
                target_family=family,
                evidence_family=evidence_family,
                family_match=family_match,
                variables=variables,
                source_type=snippet.source_type,
                source_id=snippet.source_id,
                citation=snippet.citation,
                support_strength=_support_strength(snippet.source_type, scope, snippet.query_ids),
                snippet=snippet.text,
                query_ids=list(snippet.query_ids),
                metadata=metadata,
            )
        )
    return records


def serve_knowledge_priors(
    evidence_records: list[EvidenceRecord],
    problem_spec: dict[str, Any],
) -> tuple[list[ServedPrior], list[str]]:
    variables = [item for item in problem_spec.get("variables", []) if isinstance(item, dict)]
    priors: list[ServedPrior] = []
    notes: list[str] = []
    family = _reaction_family(problem_spec)

    for index, constraint in enumerate(problem_spec.get("constraints", []) if isinstance(problem_spec.get("constraints"), list) else [], start=1):
        text = str(constraint or "").strip()
        if not text:
            continue
        priors.append(
            ServedPrior(
                prior_id=f"kp_constraint_{index:02d}",
                prior_type=PRIOR_CONSTRAINT,
                targets=[],
                payload={"text": text, "supporting_facets": [FACET_CONSTRAINT], "summary": text},
                scope="target",
                confidence=1.0,
                support_count=1,
                evidence_ids=[],
                applicable_nodes=["warm_start", "select_candidate", "run_bo_iteration"],
                summary=text,
            )
        )

    target_records = [record for record in evidence_records if record.scope == "target"]
    risky_values_by_target: dict[str, list[str]] = {}
    for variable in variables:
        name = str(variable.get("name") or "").strip()
        role = _normalize_role_name(variable.get("role"))
        domain = [str(item) for item in variable.get("domain", [])]
        if not name or not domain:
            continue
        value_scores: dict[str, float] = {}
        negative_scores: dict[str, float] = {}
        negative_support_ids: list[str] = []
        supporting_ids: list[str] = []
        supporting_facets: set[str] = set()
        for record in target_records:
            matched_values = _extract_values_for_variable(record, variable)
            if not matched_values:
                continue
            supporting_ids.append(record.evidence_id)
            supporting_facets.add(record.facet)
            for matched in matched_values:
                value_scores[matched] = value_scores.get(matched, 0.0) + float(record.support_strength)
                if _looks_negative(record.snippet):
                    negative_scores[matched] = negative_scores.get(matched, 0.0) + float(record.support_strength)
                    negative_support_ids.append(record.evidence_id)
        if value_scores:
            ordered_values = [
                item
                for item, _score in sorted(value_scores.items(), key=lambda entry: (-entry[1], entry[0]))
            ]
            confidence = min(0.95, 0.35 + 0.12 * len(_dedupe_preserve(supporting_ids)) + 0.08 * min(len(ordered_values), 3))
            if role in {"temperature", "concentration", "contact_time", "flow_rate", "pressure"}:
                allowed_values = ordered_values[: min(4, len(ordered_values))]
                priors.append(
                    ServedPrior(
                        prior_id=f"kp_window_{name}",
                        prior_type=PRIOR_WINDOW,
                        targets=[name],
                        payload={
                            "allowed_values": allowed_values,
                            "supporting_facets": sorted(supporting_facets or {FACET_WINDOW}),
                            "summary": f"Evidence favors {name} values {', '.join(allowed_values)}.",
                        },
                        scope="target",
                        confidence=confidence,
                        support_count=len(_dedupe_preserve(supporting_ids)),
                        evidence_ids=_dedupe_preserve(supporting_ids)[:6],
                        applicable_nodes=["warm_start", "select_candidate", "run_bo_iteration"],
                        summary=f"Evidence favors {name} values {', '.join(allowed_values)}.",
                    )
                )
            else:
                priors.append(
                    ServedPrior(
                        prior_id=f"kp_pref_{name}",
                        prior_type=PRIOR_VALUE_PREFERENCE,
                        targets=[name],
                        payload={
                            "preferred_values": ordered_values[: min(4, len(ordered_values))],
                            "value_scores": {key: round(float(score), 4) for key, score in value_scores.items()},
                            "supporting_facets": sorted(supporting_facets or {FACET_PRECEDENT}),
                            "summary": f"Target-family evidence prefers {name} values {', '.join(ordered_values[:3])}.",
                        },
                        scope="target",
                        confidence=confidence,
                        support_count=len(_dedupe_preserve(supporting_ids)),
                        evidence_ids=_dedupe_preserve(supporting_ids)[:6],
                        applicable_nodes=["warm_start", "hypothesis_generation", "select_candidate", "run_bo_iteration"],
                        summary=f"Target-family evidence prefers {name} values {', '.join(ordered_values[:3])}.",
                    )
                )
        if negative_scores:
            avoided_values = [
                item
                for item, _score in sorted(negative_scores.items(), key=lambda entry: (-entry[1], entry[0]))
            ][:3]
            risky_values_by_target[name] = avoided_values
            priors.append(
                ServedPrior(
                    prior_id=f"kp_avoid_{name}",
                    prior_type=PRIOR_VALUE_AVOIDANCE,
                    targets=[name],
                    payload={
                        "avoided_values": avoided_values,
                        "value_scores": {key: round(float(score), 4) for key, score in negative_scores.items()},
                        "supporting_facets": [FACET_FAILURE],
                        "summary": f"Negative evidence flags {name} values {', '.join(avoided_values)}.",
                    },
                    scope="target",
                    confidence=min(0.85, 0.3 + 0.1 * len(avoided_values)),
                    support_count=len(_dedupe_preserve(negative_support_ids)),
                    evidence_ids=_dedupe_preserve(negative_support_ids)[:6],
                    applicable_nodes=["warm_start", "select_candidate", "run_bo_iteration", "result_interpretation"],
                    summary=f"Negative evidence flags {name} values {', '.join(avoided_values)}.",
                )
            )

    interaction_counter: dict[tuple[str, str, str, str], float] = {}
    interaction_support: dict[tuple[str, str, str, str], list[str]] = {}
    for record in target_records:
        per_variable: dict[str, list[str]] = {}
        for variable in variables:
            values = _extract_values_for_variable(record, variable)
            if values:
                per_variable[str(variable.get("name") or "").strip()] = values[:1]
        names = sorted(name for name in per_variable if name)
        for left_index, left_name in enumerate(names):
            for right_name in names[left_index + 1 :]:
                left_value = per_variable[left_name][0]
                right_value = per_variable[right_name][0]
                key = (left_name, right_name, left_value, right_value)
                interaction_counter[key] = interaction_counter.get(key, 0.0) + float(record.support_strength)
                interaction_support.setdefault(key, []).append(record.evidence_id)
    if interaction_counter:
        top_interaction = max(interaction_counter.items(), key=lambda item: (item[1], item[0]))[0]
        left_name, right_name, left_value, right_value = top_interaction
        priors.append(
            ServedPrior(
                prior_id=f"kp_interaction_{left_name}_{right_name}",
                prior_type=PRIOR_INTERACTION,
                targets=[left_name, right_name],
                payload={
                    "preferred_combination": {left_name: left_value, right_name: right_value},
                    "supporting_facets": [FACET_PRECEDENT, FACET_COMPOSITION],
                    "summary": f"Evidence recurrently pairs {left_name}={left_value} with {right_name}={right_value}.",
                },
                scope="target",
                confidence=min(0.8, 0.32 + 0.15 * interaction_counter[top_interaction]),
                support_count=len(_dedupe_preserve(interaction_support.get(top_interaction, []))),
                evidence_ids=_dedupe_preserve(interaction_support.get(top_interaction, []))[:6],
                applicable_nodes=["warm_start", "select_candidate", "run_bo_iteration"],
                summary=f"Evidence recurrently pairs {left_name}={left_value} with {right_name}={right_value}.",
            )
        )

    failure_records = [
        record for record in target_records
        if record.facet == FACET_FAILURE or _looks_negative(record.snippet)
    ]
    if failure_records:
        top_failure = sorted(failure_records, key=lambda item: item.support_strength, reverse=True)[0]
        priors.append(
            ServedPrior(
                prior_id="kp_failure_01",
                prior_type=PRIOR_FAILURE,
                targets=list(top_failure.variables),
                payload={
                    "text": top_failure.snippet,
                    "supporting_facets": [FACET_FAILURE],
                    "risky_values_by_target": risky_values_by_target,
                    "summary": top_failure.snippet[:220],
                },
                scope=top_failure.scope,
                confidence=min(0.82, 0.4 + 0.15 * len(failure_records)),
                support_count=len(failure_records),
                evidence_ids=[record.evidence_id for record in failure_records[:6]],
                applicable_nodes=["warm_start", "select_candidate", "result_interpretation"],
                summary=top_failure.snippet[:220],
            )
        )

    if not any(prior.prior_type == PRIOR_VALUE_PREFERENCE for prior in priors):
        notes.append(f"No target-scoped value-preference priors were served for {family or 'this reaction family'}.")
    return priors, notes


def build_compatibility_cards(
    *,
    problem_spec: dict[str, Any],
    evidence_records: list[EvidenceRecord],
    served_priors: list[ServedPrior],
) -> list[KnowledgeCard]:
    family = _reaction_family(problem_spec)
    evidence_lookup = {item.evidence_id: item for item in evidence_records}
    cards: list[KnowledgeCard] = []
    for prior in served_priors:
        category = {
            PRIOR_VALUE_PREFERENCE: "reagent_prior",
            PRIOR_VALUE_AVOIDANCE: "constraint",
            PRIOR_WINDOW: "constraint",
            PRIOR_INTERACTION: "methodology",
            PRIOR_CONSTRAINT: "constraint",
            PRIOR_FAILURE: "constraint",
        }.get(prior.prior_type, "property")
        evidence = [
            _evidence_record_to_card_evidence(evidence_lookup[evidence_id])
            for evidence_id in prior.evidence_ids
            if evidence_id in evidence_lookup
        ]
        try:
            cards.append(
                KnowledgeCard(
                    title=(prior.summary[:80] or prior.prior_id).strip(),
                    category=category,
                    claim=prior.summary or str(prior.payload.get("summary") or prior.prior_id),
                    confidence=confidence_label(prior.confidence),
                    reaction_families=[family] if family else [],
                    variables_affected=list(prior.targets),
                    actionable_for=[
                        action
                        for action in prior.applicable_nodes
                        if action in VALID_ACTIONABLE_FOR
                    ],
                    scope=prior.scope if prior.scope in {"target", "analogous", "general"} else "general",
                    leakage_state="passed",
                    tags=[
                        f"prior_type:{prior.prior_type}",
                        f"support_count:{prior.support_count}",
                    ],
                    evidence=evidence,
                )
            )
        except Exception:
            continue

    mechanistic_records = [
        record for record in evidence_records
        if record.facet in {FACET_MECHANISTIC, FACET_SELECTIVITY, FACET_FAILURE}
    ]
    for record in mechanistic_records[:4]:
        try:
            cards.append(
                KnowledgeCard(
                    title=f"{record.facet.replace('_', ' ').title()} evidence",
                    category="mechanistic" if record.facet == FACET_MECHANISTIC else "property",
                    claim=record.snippet[:280],
                    confidence=confidence_label(record.support_strength),
                    reaction_families=[family] if family else [],
                    variables_affected=list(record.variables),
                    actionable_for=["hypothesis_generation", "result_interpretation"],
                    scope=record.scope if record.scope in {"target", "analogous", "general"} else "general",
                    leakage_state="passed",
                    tags=[f"facet:{record.facet}", f"source:{record.source_type}"],
                    evidence=[_evidence_record_to_card_evidence(record)],
                )
            )
        except Exception:
            continue

    deduped: list[KnowledgeCard] = []
    seen: set[tuple[str, str]] = set()
    for card in cards:
        key = (card.category, card.claim.strip().lower())
        if key in seen:
            continue
        deduped.append(card)
        seen.add(key)
    return deduped[:15]


def merge_knowledge_cards(
    llm_cards: list[KnowledgeCard],
    compatibility_cards: list[KnowledgeCard],
) -> list[KnowledgeCard]:
    merged: list[KnowledgeCard] = []
    seen: set[tuple[str, str]] = set()
    for card in llm_cards + compatibility_cards:
        key = (card.category, card.claim.strip().lower())
        if key in seen:
            continue
        merged.append(card)
        seen.add(key)
    return merged[:15]


def knowledge_state_to_retrieval_artifacts(
    *,
    knowledge_state: dict[str, Any],
    queries: list[RetrievalQuery],
    retrieval_meta: dict[str, Any],
    query_notes: list[str],
    filter_summary: dict[str, Any],
    snippet_count: int,
    card_payloads: list[dict[str, Any]],
    card_notes: list[str],
    retrieved_total: int,
    deduplicated_total: int,
    usable_after_filter: int,
) -> dict[str, Any]:
    served_priors = knowledge_state.get("served_priors", []) if isinstance(knowledge_state.get("served_priors"), list) else []
    evidence_records = knowledge_state.get("evidence_records", []) if isinstance(knowledge_state.get("evidence_records"), list) else []
    return {
        "queries": [query.to_dict() for query in queries],
        "query_validation_notes": list(query_notes),
        "retrieval_failures": list(retrieval_meta.get("retrieval_failures", [])),
        "source_health": list(knowledge_state.get("source_health", [])),
        "chunk_counts": {
            "retrieved_total": retrieved_total,
            "deduplicated_total": deduplicated_total,
            "usable_after_filter": usable_after_filter,
            "retrieved_by_source": retrieval_meta.get("retrieved_by_source", {}),
        },
        "per_query_chunk_counts": retrieval_meta.get("per_query_chunk_counts", {}),
        "leakage_filter_summary": filter_summary,
        "coverage_report": knowledge_state.get("coverage_report", {}),
        "snippet_count": snippet_count,
        "card_count": len(card_payloads),
        "served_prior_count": len(served_priors),
        "evidence_record_count": len(evidence_records),
        "knowledge_profile": knowledge_state.get("knowledge_profile", ""),
        "card_generation_notes": list(card_notes),
    }


def served_priors_to_legacy_cache(
    *,
    served_priors: list[ServedPrior],
    variables: list[dict[str, Any]],
    coverage_report: dict[str, Any],
) -> dict[str, Any]:
    confidence_floor = 0.1
    warm_start_bias: dict[str, dict[str, float]] = {}
    variable_lookup: dict[str, dict[str, Any]] = {}
    for variable in variables:
        name = str(variable.get("name") or "").strip()
        if not name:
            continue
        variable_lookup[name] = variable
        domain = [str(item) for item in variable.get("domain", [])]
        if domain:
            warm_start_bias[name] = {entry: confidence_floor for entry in domain}

    value_preferences: list[dict[str, Any]] = []
    value_avoidances: list[dict[str, Any]] = []
    operating_windows: list[dict[str, Any]] = []
    interactions: list[dict[str, Any]] = []
    constraints: list[dict[str, Any]] = []
    failure_modes: list[dict[str, Any]] = []
    notes: list[str] = []

    for prior in served_priors:
        payload = dict(prior.payload)
        notes.append(prior.summary or str(payload.get("summary") or prior.prior_id))
        if prior.prior_type == PRIOR_VALUE_PREFERENCE:
            value_preferences.append(prior.to_dict())
            preferred_values = payload.get("value_scores", {}) if isinstance(payload.get("value_scores"), dict) else {}
            for target in prior.targets:
                if target not in warm_start_bias:
                    continue
                for value, score in preferred_values.items():
                    if value in warm_start_bias[target]:
                        warm_start_bias[target][value] += float(score)
        elif prior.prior_type == PRIOR_VALUE_AVOIDANCE:
            value_avoidances.append(prior.to_dict())
        elif prior.prior_type == PRIOR_WINDOW:
            operating_windows.append(prior.to_dict())
            allowed_values = [str(item) for item in payload.get("allowed_values", [])]
            for target in prior.targets:
                if target not in warm_start_bias or not allowed_values:
                    continue
                for value in allowed_values:
                    if value in warm_start_bias[target]:
                        warm_start_bias[target][value] += max(0.15, float(prior.confidence))
        elif prior.prior_type == PRIOR_INTERACTION:
            interactions.append(prior.to_dict())
        elif prior.prior_type == PRIOR_CONSTRAINT:
            constraints.append(prior.to_dict())
        elif prior.prior_type == PRIOR_FAILURE:
            failure_modes.append(prior.to_dict())

    return {
        "warm_start_bias": warm_start_bias,
        "continuous_priors": {},
        "hard_constraints": constraints,
        "soft_priors": {"notes": _dedupe_preserve([str(note).strip() for note in notes if str(note).strip()])},
        "known_interactions": interactions,
        "value_preferences": value_preferences,
        "value_avoidances": value_avoidances,
        "operating_windows": operating_windows,
        "interactions": interactions,
        "constraints": constraints,
        "failure_modes": failure_modes,
        "coverage": coverage_report,
        "diagnostics": {
            "served_prior_count": len(served_priors),
            "value_preference_count": len(value_preferences),
            "operating_window_count": len(operating_windows),
            "interaction_count": len(interactions),
            "constraint_count": len(constraints),
            "failure_mode_count": len(failure_modes),
        },
        "prior_granularity": "typed_served_priors",
        "fallback_reason": None,
        "notes": _dedupe_preserve([str(note).strip() for note in notes if str(note).strip()]),
    }


def _primary_facet(intents: list[str]) -> str:
    for intent in intents:
        normalized = _normalize_intent_name(intent)
        if normalized:
            return normalized
    return FACET_MECHANISTIC


def _support_strength(source_type: str, scope: str, query_ids: list[str]) -> float:
    source_weight = {
        "local_rag": 0.78,
        "semantic_scholar": 0.62,
        "pubchem": 0.55,
        "web": 0.35,
    }.get(str(source_type or ""), 0.3)
    scope_weight = {"target": 1.0, "analogous": 0.65, "general": 0.4}.get(str(scope or ""), 0.35)
    query_bonus = min(0.15, 0.03 * len(set(query_ids or [])))
    return round(min(0.98, source_weight * scope_weight + query_bonus), 4)


def _infer_variables_from_text(text: str, problem_spec: dict[str, Any]) -> list[str]:
    lowered = str(text or "").lower()
    matched: list[str] = []
    for variable in problem_spec.get("variables", []) if isinstance(problem_spec.get("variables"), list) else []:
        if not isinstance(variable, dict):
            continue
        name = str(variable.get("name") or "").strip()
        role = _normalize_role_name(variable.get("role"))
        if role and role.replace("_", " ") in lowered and role not in matched:
            matched.append(role)
            continue
        if name and name.lower() in lowered and name not in matched:
            matched.append(name)
    return matched


def _extract_values_for_variable(record: EvidenceRecord, variable: dict[str, Any]) -> list[str]:
    metadata = dict(record.metadata)
    name = str(variable.get("name") or "").strip()
    role = _normalize_role_name(variable.get("role"))
    domain = [str(item) for item in variable.get("domain", [])]
    if not name or not domain:
        return []

    candidates: list[str] = []
    role_to_field = {
        "ligand": "ligands_norm",
        "base": "bases_norm",
        "solvent": "solvents_norm",
        "catalyst_precursor": "catalysts_norm",
    }
    field = role_to_field.get(role or "")
    if field:
        try:
            raw_values = json.loads(metadata.get(field) or "[]")
        except (TypeError, json.JSONDecodeError):
            raw_values = []
        candidates.extend(str(item).strip() for item in raw_values if str(item).strip())
    if role in {"temperature", "concentration", "contact_time", "pressure"}:
        value = metadata.get(f"{role}_c", metadata.get(role))
        if value is not None:
            candidates.append(str(value).strip())
    normalized_text = f"{record.snippet}\n{json.dumps(metadata, ensure_ascii=False)}".lower()
    matches: list[str] = []
    for domain_value in domain:
        domain_text = str(domain_value).strip()
        lowered_domain = domain_text.lower()
        if lowered_domain in {candidate.lower() for candidate in candidates}:
            matches.append(domain_text)
            continue
        if lowered_domain and lowered_domain in normalized_text:
            matches.append(domain_text)
            continue
        if role in {"temperature", "concentration", "contact_time", "pressure", "flow_rate"}:
            if _numericish_match(domain_text, candidates, normalized_text):
                matches.append(domain_text)
    return _dedupe_preserve(matches)


def _numericish_match(domain_value: str, candidates: list[str], normalized_text: str) -> bool:
    try:
        numeric = float(domain_value)
    except (TypeError, ValueError):
        return False
    for candidate in candidates:
        try:
            if abs(float(candidate) - numeric) <= 1e-6:
                return True
        except (TypeError, ValueError):
            continue
    return any(token == domain_value for token in re.findall(r"\d+(?:\.\d+)?", normalized_text))


def _looks_negative(text: str) -> bool:
    lowered = str(text or "").lower()
    markers = [
        "decompos",
        "over-oxid",
        "poor",
        "low productivity",
        "inactive",
        "deactivation",
        "sinter",
        "coking",
        "undesired",
        "suppression",
        "co2",
        "cox",
    ]
    return any(marker in lowered for marker in markers)


def _evidence_record_to_card_evidence(record: EvidenceRecord) -> KnowledgeEvidence:
    return KnowledgeEvidence(
        source_type=record.source_type,
        document_id=str(record.metadata.get("document_id") or record.source_id),
        chunk_id=record.evidence_id,
        locator=str(record.metadata.get("locator") or record.source_id),
        citation=record.citation,
        snippet=record.snippet[:500],
        metadata=dict(record.metadata),
    )


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
        f"REACTION_CONTEXT:\n{compact_json({'reaction_family': family, 'variables': _variable_role_summary(problem_spec)})}\n\n"
        f"SNIPPETS_BY_INTENT:\n{compact_json(grouped)}"
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
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], str, dict[str, Any]]:
    llm_adapter = RAGLLMAdapter(settings=settings)
    try:
        family = _reaction_family(problem_spec)
        profile = infer_knowledge_profile(family)
        queries, query_notes = generate_retrieval_queries(problem_spec, settings, llm_adapter=llm_adapter)
        retrieved_chunks, retrieval_meta = execute_multi_source(queries, problem_spec, settings)
        deduplicated_chunks = deduplicate_chunks(retrieved_chunks)
        filtered_chunks, filter_summary = filter_and_sanitize(deduplicated_chunks, problem_spec, settings)
        snippets, snippet_notes = build_evidence_snippets(filtered_chunks, queries, settings, llm_adapter=llm_adapter)
        evidence_records = build_evidence_records(snippets, problem_spec)
        served_priors, serving_notes = serve_knowledge_priors(evidence_records, problem_spec)
        llm_cards: list[KnowledgeCard] = []
        card_notes: list[str] = []
        if llm_adapter.available:
            llm_cards, card_notes = condense_and_build_cards(
                snippets,
                filtered_chunks,
                problem_spec,
                settings,
                llm_adapter=llm_adapter,
            )
        else:
            card_notes.append("Knowledge-card synthesis fallback: LLM unavailable")
        compatibility_cards = build_compatibility_cards(
            problem_spec=problem_spec,
            evidence_records=evidence_records,
            served_priors=served_priors,
        )
        merged_cards = merge_knowledge_cards(llm_cards, compatibility_cards)
        card_payloads = [card.to_dict() for card in merged_cards]
        source_health = retrieval_meta.get("source_health", [])
        coverage_report = build_coverage_report(
            target_family=family,
            profile=profile,
            required_facets=required_facets_for_profile(profile),
            evidence_records=[item.to_dict() for item in evidence_records],
            served_priors=[item.to_dict() for item in served_priors],
            source_health=source_health,
        )
        knowledge_state = {
            "target_family": family,
            "knowledge_profile": profile,
            "derived_targets": build_derived_targets(problem_spec),
            "source_health": source_health,
            "coverage_report": coverage_report,
            "evidence_records": [item.to_dict() for item in evidence_records],
            "served_priors": [item.to_dict() for item in served_priors],
            "knowledge_digests": build_node_digests(
                evidence_records=[item.to_dict() for item in evidence_records],
                served_priors=[item.to_dict() for item in served_priors],
            ),
        }
        artifacts = knowledge_state_to_retrieval_artifacts(
            knowledge_state=knowledge_state,
            queries=queries,
            retrieval_meta=retrieval_meta,
            query_notes=query_notes,
            filter_summary=filter_summary,
            snippet_count=len(snippets),
            card_payloads=card_payloads,
            card_notes=snippet_notes + card_notes + serving_notes,
            retrieved_total=len(retrieved_chunks),
            deduplicated_total=len(deduplicated_chunks),
            usable_after_filter=len(filtered_chunks),
        )
        kb_context = format_cards_for_context(card_payloads)
        kb_priors = served_priors_to_legacy_cache(
            served_priors=served_priors,
            variables=problem_spec.get("variables", []),
            coverage_report=coverage_report,
        )
        return knowledge_state, card_payloads, artifacts, kb_context, kb_priors
    except Exception as exc:  # pragma: no cover - defensive runtime fallback
        logger.warning("Knowledge augmentation failed; continuing without cards: %s", exc)
        knowledge_state = empty_knowledge_state(problem_spec)
        artifacts = {
            "queries": [],
            "query_validation_notes": [],
            "retrieval_failures": [],
            "source_health": [],
            "chunk_counts": {},
            "leakage_filter_summary": {},
            "coverage_report": knowledge_state.get("coverage_report", {}),
            "snippet_count": 0,
            "card_count": 0,
            "served_prior_count": 0,
            "card_generation_notes": [f"Knowledge augmentation failed: {type(exc).__name__}: {exc}"],
        }
        return knowledge_state, [], artifacts, "", {"warm_start_bias": {}, "soft_priors": {}, "notes": []}


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


def _facet_specs_for_problem(
    *,
    family: str,
    profile: str,
    requested_facets: list[str],
    variable_roles: list[str],
    reagent_roles: list[str],
    entities_by_role: dict[str, list[EntityHint]],
) -> list[tuple[str, str, list[str], list[EntityHint]]]:
    if profile == "heterogeneous_catalysis":
        heterogeneous_focus = {
            FACET_MECHANISTIC: (
                f"{family} mechanism including active site, oxygen-species chemistry, and rate-limiting steps",
                [role for role in variable_roles if role in {"metal_primary", "metal_promoter", "metal_selector", "support"}][:4]
                or variable_roles[:4],
                _entities_for_roles(entities_by_role, ["metal_primary", "support", "metal_promoter"]),
            ),
            FACET_PRECEDENT: (
                f"{family} target-family catalyst and operating-condition precedents for optimization",
                variable_roles[:5],
                _entities_for_roles(entities_by_role, ["metal_primary", "metal_promoter", "metal_selector", "support"]),
            ),
            FACET_COMPOSITION: (
                f"Effect of catalyst composition, support, and promoter identity on {family} performance",
                [role for role in variable_roles if role in {"metal_primary", "metal_promoter", "metal_selector", "support"}][:4],
                _entities_for_roles(entities_by_role, ["metal_primary", "metal_promoter", "metal_selector", "support"]),
            ),
            FACET_WINDOW: (
                f"{family} operating window across temperature, contact time, feed ratio, and pressure",
                [role for role in variable_roles if role in {"temperature", "contact_time", "flow_rate", "pressure"}][:4],
                _entities_for_roles(entities_by_role, []),
            ),
            FACET_SELECTIVITY: (
                f"{family} selectivity control and C2/COx tradeoff drivers under variable conditions",
                variable_roles[:5],
                _entities_for_roles(entities_by_role, ["metal_primary", "support"]),
            ),
            FACET_FAILURE: (
                f"Catalyst deactivation, over-oxidation, or failure modes in {family}",
                variable_roles[:4],
                _entities_for_roles(entities_by_role, ["metal_primary", "support"]),
            ),
            FACET_SCOPE: (
                f"Transferability of {family} catalyst trends across related catalyst compositions and feeds",
                variable_roles[:4],
                _entities_for_roles(entities_by_role, ["metal_primary", "support"]),
            ),
            FACET_CONSTRAINT: (
                f"Safety, transport, and operating constraints that bound feasible {family} conditions",
                [role for role in variable_roles if role in {"temperature", "flow_rate", "pressure", "contact_time"}][:4],
                _entities_for_roles(entities_by_role, []),
            ),
        }
        return [
            (facet, *heterogeneous_focus[facet])
            for facet in requested_facets
            if facet in heterogeneous_focus
        ]

    homogeneous_focus = {
        FACET_MECHANISTIC: (
            f"{family} mechanism and catalytic cycle with emphasis on {'/'.join(variable_roles[:3]) or 'key condition effects'}",
            variable_roles[:3],
            _entities_for_roles(entities_by_role, reagent_roles[:2]),
        ),
        FACET_PRECEDENT: (
            f"{family} literature precedents for condition optimization involving {'/'.join(variable_roles[:4]) or 'core reaction variables'}",
            variable_roles[:4],
            _entities_for_roles(entities_by_role, reagent_roles[:3]),
        ),
        FACET_COMPOSITION: (
            f"Composition, reagent-property, ligand/base/solvent, and catalyst effects relevant to {family} optimization",
            reagent_roles[:4] or variable_roles[:3],
            _entities_for_roles(entities_by_role, reagent_roles[:4]),
        ),
        FACET_WINDOW: (
            f"Temperature, concentration, and operating-window sensitivity in {family} condition optimization",
            [role for role in variable_roles if role in {"temperature", "concentration", "pressure"}] or variable_roles[:2],
            _entities_for_roles(entities_by_role, []),
        ),
        FACET_SELECTIVITY: (
            f"{family} selectivity drivers and condition-dependent selectivity trends",
            variable_roles[:4],
            _entities_for_roles(entities_by_role, reagent_roles[:2]),
        ),
        FACET_FAILURE: (
            f"Common side reactions, decomposition pathways, and suppression strategies in {family} chemistry",
            variable_roles[:4],
            _entities_for_roles(entities_by_role, reagent_roles[:2]),
        ),
        FACET_SCOPE: (
            f"{family} substrate scope, transferability, and functional-group tolerance under related conditions",
            variable_roles[:3],
            _entities_for_roles(entities_by_role, []),
        ),
        FACET_CONSTRAINT: (
            f"Constraint, safety, and practical operating limits relevant to {family} optimization",
            variable_roles[:3],
            _entities_for_roles(entities_by_role, []),
        ),
    }
    return [
        (facet, *homogeneous_focus[facet])
        for facet in requested_facets
        if facet in homogeneous_focus
    ]


def _facet_collections(
    facet: str,
    prefer_sources: list[str],
    *,
    profile: str,
    source: str,
) -> list[str] | None:
    if source != "local_rag":
        return None
    preferred = [_local_source_to_collection(item) for item in (prefer_sources or []) if item in {"ord", "reviews", "textbooks", "supplementary"}]
    if facet == FACET_PRECEDENT:
        return _dedupe_preserve([item for item in preferred if item == "ord"] + ["ord"])
    if facet == FACET_MECHANISTIC:
        return _dedupe_preserve([item for item in preferred if item in {"reviews", "textbooks", "supplementary"}] + ["reviews", "textbooks"])
    if facet == FACET_COMPOSITION:
        tail = ["reviews", "textbooks"] if profile != "heterogeneous_catalysis" else ["ord", "reviews", "textbooks", "supplementary"]
        return _dedupe_preserve(preferred + tail)
    if facet == FACET_WINDOW:
        tail = ["ord", "reviews"] if profile != "heterogeneous_catalysis" else ["ord", "reviews", "supplementary"]
        return _dedupe_preserve(preferred + tail)
    if facet in {FACET_SELECTIVITY, FACET_FAILURE, FACET_SCOPE, FACET_CONSTRAINT}:
        return _dedupe_preserve(preferred + ["reviews", "textbooks", "supplementary", "ord"])
    return None


def _local_where_clause(facet: str, family: str) -> dict[str, Any] | None:
    target_family = str(family or "").strip().upper()
    if facet == FACET_PRECEDENT and target_family:
        return {"reaction_family": {"$in": [target_family]}}
    return None


def _source_status_payload(
    *,
    source: str,
    query: RetrievalQuery,
    connector_status: dict[str, Any],
    latency_ms: float,
) -> dict[str, Any]:
    status = KnowledgeSourceStatus(
        source=source,
        query_id=query.id,
        status=str(connector_status.get("status") or "available_no_result"),
        error_type=str(connector_status.get("error_type") or ""),
        message=str(connector_status.get("message") or ""),
        result_count=int(connector_status.get("result_count", 0) or 0),
        filtered_count=int(connector_status.get("filtered_count", 0) or 0),
        latency_ms=float(latency_ms or 0.0),
        facet=query.intent,
    )
    return status.to_dict()


def _merge_connector_statuses(statuses: list[dict[str, Any]], result_count: int) -> dict[str, Any]:
    if result_count > 0:
        return {"status": "ok", "error_type": "", "message": "", "result_count": result_count}
    normalized = [item for item in statuses if isinstance(item, dict)]
    if not normalized:
        return {"status": "available_no_result", "error_type": "", "message": "No entities to query.", "result_count": 0}
    for preferred in ("network_error", "timeout", "auth_error", "unavailable", "internal_error"):
        matched = next((item for item in normalized if str(item.get("status") or "") == preferred), None)
        if matched is not None:
            return {
                "status": preferred,
                "error_type": str(matched.get("error_type") or preferred),
                "message": str(matched.get("message") or ""),
                "result_count": 0,
            }
    return {
        "status": "available_no_result",
        "error_type": "",
        "message": str(normalized[-1].get("message") or "No matching results."),
        "result_count": 0,
    }


def _status_to_failure_message(status: dict[str, Any]) -> str:
    state = str(status.get("status") or "")
    if state in {"ok", "available_no_result"}:
        return ""
    error_type = str(status.get("error_type") or state or "unknown")
    message = str(status.get("message") or "").strip()
    suffix = f":{message}" if message else ""
    return f"{status.get('query_id')}:{status.get('source')}:{error_type}{suffix}"


def _local_source_to_collection(source: str) -> str:
    mapping = {
        "ord": "ord",
        "reviews": "reviews",
        "textbooks": "textbooks",
        "supplementary": "supplementary",
    }
    return mapping.get(str(source).strip().lower(), str(source).strip().lower())


def _generate_heuristic_queries(
    problem_spec: dict[str, Any],
    *,
    required_facets: list[str] | None = None,
) -> list[RetrievalQuery]:
    family = _reaction_family(problem_spec) or "reaction"
    description = str(problem_spec.get("description") or problem_spec.get("raw_description") or "").strip()
    plan = ReactionRetrievalPlan.from_problem_spec(problem_spec)
    profile = infer_knowledge_profile(family)
    entities_by_role = _collect_entities_by_role(problem_spec)
    variable_roles = [role for role in plan.precedent.variable_roles if role != "other"]
    reagent_roles = [role for role in variable_roles if role in REAGENT_LIKE_ROLES]
    requested_facets = required_facets or required_facets_for_profile(profile)
    intent_specs = _facet_specs_for_problem(
        family=family,
        profile=profile,
        requested_facets=requested_facets,
        variable_roles=variable_roles,
        reagent_roles=reagent_roles,
        entities_by_role=entities_by_role,
    )

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
                intent=FACET_COMPOSITION if role in REAGENT_LIKE_ROLES else FACET_WINDOW,
                query_text=query_text,
                target_sources=_normalize_target_sources(
                    RetrievalQuery(
                        id=f"H{len(heuristic_queries) + 1}",
                        intent=FACET_COMPOSITION if role in REAGENT_LIKE_ROLES else FACET_WINDOW,
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
    *,
    required_intents: list[str],
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
    for intent in required_intents:
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
        metadata["query_text"] = query.query_text
        metadata["facet"] = query.intent
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
        filtered_refs: list[str] = []
        for ref in evidence_refs:
            snippet = snippet_map[ref]
            evidence_family = str(snippet.metadata.get("reaction_family", "") or "").strip().upper()
            if family and snippet.source_type == "local_rag" and evidence_family and evidence_family != family:
                continue
            filtered_refs.append(ref)
        if not filtered_refs:
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
            "evidence": [_snippet_to_evidence(snippet_map[ref]) for ref in filtered_refs[:3]],
        }
        _attach_reagent_prior_top_values(payload)
        reaction_families = [str(item).strip().upper() for item in raw_card.get("reaction_families", []) if str(item).strip()]
        if reaction_families:
            payload["reaction_families"] = reaction_families
        if family:
            if any(
                str(snippet_map[ref].metadata.get("reaction_family", "") or "").strip().upper() == family
                for ref in filtered_refs
            ):
                payload["scope"] = "target"
            elif payload["scope"] == "target":
                payload["scope"] = "analogous"
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
    precedent_chunks = [chunk for chunk in local_chunks if FACET_PRECEDENT in chunk.metadata.get("intents", [])]
    mechanism_chunks = [chunk for chunk in local_chunks if FACET_MECHANISTIC in chunk.metadata.get("intents", [])]
    property_chunks = [
        chunk
        for chunk in local_chunks
        if any(intent in {FACET_COMPOSITION, FACET_WINDOW, FACET_SELECTIVITY, FACET_FAILURE} for intent in chunk.metadata.get("intents", []))
    ]

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
