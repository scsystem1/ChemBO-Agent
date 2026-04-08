"""
Local RAG implementation for chemistry knowledge retrieval.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel, Field, field_validator

from config.settings import Settings
from knowledge.llm_adapter import RAGLLMAdapter

logger = logging.getLogger(__name__)


DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
RRF_K = 60
MIN_CHUNK_SIZE = 160
DEFAULT_CHUNK_SIZE = 2600
DEFAULT_TOP_K = 5

COLLECTION_ORD = "ord"
COLLECTION_REVIEWS = "reviews"
COLLECTION_TEXTBOOKS = "textbooks"
COLLECTION_SUPPLEMENTARY = "supplementary"
ALL_COLLECTIONS = (
    COLLECTION_ORD,
    COLLECTION_REVIEWS,
    COLLECTION_TEXTBOOKS,
    COLLECTION_SUPPLEMENTARY,
)

REACTION_FAMILY_ALIASES = {
    "DAR": ["direct arylation", "c-h arylation", "c-h activation", "direct arylation reaction"],
    "BH": ["buchwald-hartwig", "buchwald hartwig", "amination", "c-n cross coupling"],
    "SUZUKI": ["suzuki", "suzuki-miyaura", "boronic acid cross coupling"],
    "NEGISHI": ["negishi", "organozinc cross coupling"],
    "STILLE": ["stille", "organostannane cross coupling"],
    "MITSUNOBU": ["mitsunobu"],
    "DEOXYFLUORINATION": ["deoxyfluorination", "fluorination"],
    "PHOTOREDOX_NI": ["photoredox", "nickel dual catalysis", "ni photoredox"],
    "SCR": ["solid catalyst reaction", "surface chemistry"],
    "OCM": ["oxidative coupling of methane", "ocm"],
}

CHEMISTRY_SYNONYM_MAP = {
    "pd(oac)2": ["palladium acetate", "pd acetate"],
    "xphos": ["biaryl phosphine ligand", "x-phos"],
    "sphos": ["s-phos", "biaryl phosphine"],
    "dmac": ["dma", "dimethylacetamide"],
    "dmf": ["dimethylformamide"],
    "nmp": ["n-methylpyrrolidone"],
    "cs2co3": ["cesium carbonate"],
    "k2co3": ["potassium carbonate"],
    "koac": ["potassium acetate"],
    "pivalate": ["opiv", "carboxylate base"],
    "cmd": ["concerted metalation deprotonation"],
    "oa": ["oxidative addition"],
    "tm": ["transmetalation"],
    "re": ["reductive elimination"],
}

FAMILY_FROM_FILENAME = {
    "buchwald_hartwig": "BH",
    "ch_activation": "DAR",
    "direct_arylation": "DAR",
    "suzuki": "SUZUKI",
    "negishi": "NEGISHI",
    "stille": "STILLE",
    "mitsunobu": "MITSUNOBU",
    "deoxyfluorination": "DEOXYFLUORINATION",
    "photoredox_ni": "PHOTOREDOX_NI",
    "ocm": "OCM",
    "scr": "SCR",
}


class ReactionQuery(BaseModel):
    """User-facing reaction query."""

    reaction_text: str = ""
    description: str = ""
    reaction_family: str = ""
    focus_terms: list[str] = Field(default_factory=list)

    @field_validator("reaction_text", "description", "reaction_family")
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("focus_terms")
    @classmethod
    def _normalize_focus_terms(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        items: list[str] = []
        for raw in value:
            cleaned = str(raw or "").strip()
            if cleaned and cleaned not in seen:
                items.append(cleaned)
                seen.add(cleaned)
        return items

    def to_text(self) -> str:
        pieces = [self.reaction_text, self.description, self.reaction_family]
        pieces.extend(self.focus_terms)
        return "\n".join(piece for piece in pieces if piece).strip()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="python")


class PrecedentQuery(BaseModel):
    """Condition-centric precedent retrieval query."""

    reaction_family: str = ""
    reaction_smiles: str = ""
    description: str = ""
    substrate_roles: list[str] = Field(default_factory=list)
    known_fixed_context: list[dict[str, str]] = Field(default_factory=list)
    variable_roles: list[str] = Field(default_factory=list)
    must_match_roles: list[str] = Field(default_factory=lambda: ["reaction_family"])
    query_mode: str = "optimize_conditions"

    @field_validator("reaction_family", "reaction_smiles", "description", "query_mode")
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        return str(value or "").strip()

    def to_filter_dict(self) -> dict[str, Any]:
        where: dict[str, Any] = {"chunk_view": "condition_summary"}
        family = self.reaction_family.strip().upper()
        if family:
            where["reaction_family"] = {"$in": [family]}
        return where

    def to_text(self) -> str:
        pieces = [self.reaction_family, self.reaction_smiles, self.description]
        pieces.extend(self.substrate_roles)
        pieces.extend(item.get("value", "") for item in self.known_fixed_context)
        pieces.extend(self.variable_roles)
        return " ".join(piece for piece in pieces if piece).strip()


class MechanismQuery(BaseModel):
    """Mechanism-oriented retrieval query."""

    reaction_family: str = ""
    description: str = ""
    focus_aspects: list[str] = Field(default_factory=list)

    @field_validator("reaction_family", "description")
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("focus_aspects")
    @classmethod
    def _normalize_focus_aspects(cls, value: list[str]) -> list[str]:
        return _dedupe_strs([str(item or "").strip() for item in value])

    def to_text(self) -> str:
        pieces = [self.reaction_family, self.description]
        pieces.extend(self.focus_aspects)
        return " ".join(piece for piece in pieces if piece).strip()


class PropertyQuery(BaseModel):
    """Property-oriented retrieval query."""

    reagent_names: list[str] = Field(default_factory=list)
    property_types: list[str] = Field(default_factory=list)

    @field_validator("reagent_names", "property_types")
    @classmethod
    def _normalize_lists(cls, value: list[str]) -> list[str]:
        return _dedupe_strs([str(item or "").strip() for item in value])

    def to_text(self) -> str:
        return " ".join(item for item in self.reagent_names + self.property_types if item).strip()


class ReactionRetrievalPlan(BaseModel):
    """Three-path retrieval plan derived from a ChemBO problem spec."""

    precedent: PrecedentQuery = Field(default_factory=PrecedentQuery)
    mechanism: MechanismQuery = Field(default_factory=MechanismQuery)
    property: PropertyQuery = Field(default_factory=PropertyQuery)
    active_goals: list[str] = Field(default_factory=lambda: ["precedent", "mechanism"])
    prefer_sources: list[str] = Field(default_factory=lambda: ["ord", "reviews"])

    @classmethod
    def from_problem_spec(cls, problem_spec: dict[str, Any]) -> "ReactionRetrievalPlan":
        retrieval = dict(problem_spec.get("retrieval", {}) or {})
        reaction = dict(problem_spec.get("reaction", {}) or {})
        variables = [item for item in problem_spec.get("variables", []) if isinstance(item, dict)]
        family = str(reaction.get("family") or problem_spec.get("reaction_type") or "").strip().upper()
        goals = _dedupe_strs([str(item).strip().lower() for item in retrieval.get("goals", ["precedent", "mechanism"])])
        prefer_sources = _dedupe_strs([str(item).strip().lower() for item in retrieval.get("prefer_sources", ["ord", "reviews"])])
        must_match_roles = _dedupe_strs([str(item).strip().lower() for item in retrieval.get("must_match_roles", ["reaction_family"])])
        variable_roles = _dedupe_strs(
            [
                str(variable.get("role", "other")).strip().lower()
                for variable in variables
                if str(variable.get("role", "other")).strip().lower() not in {"", "other"}
            ]
        )
        fixed_context = [
            {"role": str(item.get("role", "")).strip().lower(), "value": str(item.get("value", "")).strip()}
            for item in reaction.get("known_fixed_context", [])
            if isinstance(item, dict) and str(item.get("value", "")).strip()
        ]
        substrate_roles = _dedupe_strs(
            [
                str(item.get("role", "")).strip().lower()
                for item in reaction.get("substrates", [])
                if isinstance(item, dict) and str(item.get("role", "")).strip()
            ]
        )
        focus_aspects = _default_mechanism_focus(variable_roles, family)
        reagent_names = _default_property_reagents(fixed_context, variables)
        return cls(
            precedent=PrecedentQuery(
                reaction_family=family,
                reaction_smiles=str(reaction.get("reaction_smiles", "")).strip(),
                description=str(problem_spec.get("description", "")).strip(),
                substrate_roles=substrate_roles,
                known_fixed_context=fixed_context,
                variable_roles=variable_roles,
                must_match_roles=must_match_roles or ["reaction_family"],
                query_mode=str(retrieval.get("query_mode", "optimize_conditions")).strip().lower() or "optimize_conditions",
            ),
            mechanism=MechanismQuery(
                reaction_family=family,
                description=str(problem_spec.get("description", "")).strip(),
                focus_aspects=focus_aspects,
            ),
            property=PropertyQuery(
                reagent_names=reagent_names,
                property_types=_default_property_types(variable_roles),
            ),
            active_goals=goals or ["precedent", "mechanism"],
            prefer_sources=prefer_sources or ["ord", "reviews"],
        )

    def to_legacy_query(self) -> ReactionQuery:
        return ReactionQuery(
            reaction_text=self.precedent.to_text(),
            description=self.mechanism.to_text(),
            reaction_family=self.precedent.reaction_family,
            focus_terms=self.precedent.variable_roles + self.mechanism.focus_aspects,
        )


@dataclass
class LocalRAGConfig:
    persist_dir: str = "./data/local_rag"
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    reranker_model: str = DEFAULT_RERANKER_MODEL
    top_k: int = DEFAULT_TOP_K
    enable_hyde: bool = True
    enable_contextual_compression: bool = True
    enable_llm_rerank: bool = False
    enable_local_rerank: bool = False
    chunk_size: int = DEFAULT_CHUNK_SIZE

    @classmethod
    def from_settings(cls, settings: Settings) -> "LocalRAGConfig":
        return cls(
            persist_dir=str(getattr(settings, "chromadb_persist_dir", "./data/local_rag")),
            embedding_model=str(getattr(settings, "rag_embedding_model", DEFAULT_EMBEDDING_MODEL)),
            reranker_model=str(getattr(settings, "rag_reranker_model", DEFAULT_RERANKER_MODEL)),
            top_k=int(getattr(settings, "rag_top_k", DEFAULT_TOP_K)),
            enable_hyde=bool(getattr(settings, "rag_enable_hyde", True)),
            enable_contextual_compression=bool(getattr(settings, "rag_enable_contextual_compression", True)),
            enable_llm_rerank=bool(getattr(settings, "rag_enable_llm_rerank", False)),
            enable_local_rerank=bool(getattr(settings, "rag_enable_local_rerank", False)),
        )


@dataclass
class StoredChunk:
    chunk_id: str
    content: str
    metadata: dict[str, Any]
    embedding: np.ndarray


@dataclass
class RetrievedChunk:
    chunk_id: str
    content: str
    collection: str
    metadata: dict[str, Any] = field(default_factory=dict)
    compressed_content: str = ""
    dense_score: float = 0.0
    sparse_score: float = 0.0
    fusion_score: float = 0.0
    rerank_score: float = 0.0

    def source_locator(self) -> str:
        if self.collection == COLLECTION_ORD:
            reaction_id = self.metadata.get("reaction_id", "")
            return f"ORD:{reaction_id}" if reaction_id else "ORD"
        page_start = self.metadata.get("page_start")
        page_end = self.metadata.get("page_end")
        if page_start and page_end and page_start != page_end:
            return f"{self.metadata.get('source_file', '')}:pp.{page_start}-{page_end}"
        if page_start:
            return f"{self.metadata.get('source_file', '')}:p.{page_start}"
        return str(self.metadata.get("source_file", self.collection))

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "content": self.content,
            "collection": self.collection,
            "metadata": dict(self.metadata),
            "compressed_content": self.compressed_content,
            "dense_score": self.dense_score,
            "sparse_score": self.sparse_score,
            "fusion_score": self.fusion_score,
            "rerank_score": self.rerank_score,
        }


@dataclass
class RetrievalResult:
    query: ReactionQuery
    normalized_query: str
    expanded_terms: list[str] = field(default_factory=list)
    alternate_queries: list[str] = field(default_factory=list)
    hyde_text: str = ""
    chunks: list[RetrievedChunk] = field(default_factory=list)
    method: str = "hybrid"
    total_candidates: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query.to_dict(),
            "normalized_query": self.normalized_query,
            "expanded_terms": list(self.expanded_terms),
            "alternate_queries": list(self.alternate_queries),
            "hyde_text": self.hyde_text,
            "chunks": [chunk.to_dict() for chunk in self.chunks],
            "method": self.method,
            "total_candidates": self.total_candidates,
            "notes": list(self.notes),
        }


@dataclass
class RoleEvidence:
    role: str
    top_values: list[str]
    supporting_chunks: list[RetrievedChunk]
    confidence: float
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "top_values": list(self.top_values),
            "supporting_chunks": [chunk.to_dict() for chunk in self.supporting_chunks],
            "confidence": self.confidence,
            "notes": list(self.notes),
        }


@dataclass
class EvidenceBundle:
    plan: ReactionRetrievalPlan
    role_evidence: dict[str, RoleEvidence]
    mechanism_chunks: list[RetrievedChunk]
    mechanism_summary: str
    property_chunks: list[RetrievedChunk]
    precedent_result: RetrievalResult
    mechanism_result: RetrievalResult
    property_result: RetrievalResult
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.model_dump(mode="python"),
            "role_evidence": {key: value.to_dict() for key, value in self.role_evidence.items()},
            "mechanism_chunks": [chunk.to_dict() for chunk in self.mechanism_chunks],
            "mechanism_summary": self.mechanism_summary,
            "property_chunks": [chunk.to_dict() for chunk in self.property_chunks],
            "precedent_result": self.precedent_result.to_dict(),
            "mechanism_result": self.mechanism_result.to_dict(),
            "property_result": self.property_result.to_dict(),
            "notes": list(self.notes),
        }

    def to_kb_context_string(self) -> str:
        lines: list[str] = []
        if self.mechanism_summary:
            lines.append(f"[Mechanism] {self.mechanism_summary}")
        for role, evidence in self.role_evidence.items():
            if evidence.top_values:
                lines.append(
                    f"[Prior:{role}] High-frequency precedent values: {', '.join(evidence.top_values[:5])} "
                    f"(confidence={evidence.confidence:.2f})"
                )
            for note in evidence.notes[:2]:
                lines.append(f"  -> {note}")
        return "\n".join(lines) if lines else "[No retrieval evidence available]"


def tokenize_chemistry_text(text: str) -> list[str]:
    lowered = str(text or "").lower()
    lowered = lowered.replace("–", "-").replace("—", "-")
    tokens = re.findall(r"[a-z0-9][a-z0-9_\-+/().%]*", lowered)
    return [token for token in tokens if len(token) > 1]


def split_sentences(text: str) -> list[str]:
    chunks = re.split(r"(?<=[.!?])\s+", str(text or "").strip())
    return [chunk.strip() for chunk in chunks if chunk.strip()]


def _normalize_reagent_name(name: str) -> str:
    lowered = str(name or "").lower().strip()
    lowered = re.sub(r"\s*\(.*?\)", "", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _domain_labels_from_variable(variable: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for entry in variable.get("domain", []):
        if isinstance(entry, dict):
            label = entry.get("label") or entry.get("name") or entry.get("value")
            if label is not None:
                labels.append(str(label))
        else:
            labels.append(str(entry))
    return [label for label in labels if label]


def _default_mechanism_focus(variable_roles: list[str], reaction_family: str) -> list[str]:
    role_to_focus = {
        "ligand": "ligand effect",
        "base": "base effect",
        "solvent": "solvent effect",
        "catalyst_precursor": "catalyst activation",
        "support": "support effect",
        "metal_primary": "active metal role",
        "metal_promoter": "promoter effect",
        "metal_selector": "selector effect",
        "temperature": "temperature sensitivity",
        "concentration": "concentration effect",
        "contact_time": "residence time effect",
        "flow_rate": "feed composition effect",
    }
    focus: list[str] = []
    for role in variable_roles:
        if role in role_to_focus:
            focus.append(role_to_focus[role])
    if reaction_family == "DAR":
        focus.append("cmd mechanism")
    if reaction_family == "BH":
        focus.append("oxidative addition and reductive elimination")
    return _dedupe_strs(focus[:5])


def _default_property_reagents(fixed_context: list[dict[str, str]], variables: list[dict[str, Any]]) -> list[str]:
    names = [str(item.get("value", "")).strip() for item in fixed_context if str(item.get("value", "")).strip()]
    reagent_roles = {"ligand", "base", "solvent", "catalyst_precursor", "support", "additive", "oxidant", "reductant"}
    for variable in variables:
        if str(variable.get("role", "other")).strip().lower() not in reagent_roles:
            continue
        names.extend(_domain_labels_from_variable(variable)[:2])
    return _dedupe_strs(names[:8])


def _default_property_types(variable_roles: list[str]) -> list[str]:
    property_types = ["pKa", "steric", "electronic effect", "solubility"]
    if "solvent" in variable_roles:
        property_types.extend(["polarity", "coordinating ability"])
    if {"metal_primary", "metal_promoter", "metal_selector"} & set(variable_roles):
        property_types.extend(["redox behavior", "oxygen mobility"])
    return _dedupe_strs(property_types)


def _looks_like_heading(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if len(stripped) < 6 or len(stripped) > 140:
        return False
    if stripped.startswith("#"):
        return True
    if re.fullmatch(r"[A-Z][A-Z0-9\s\-:]{5,}", stripped):
        return True
    if re.match(r"^(?:\d+\.|[A-Z]\.)\s+[A-Z]", stripped):
        return True
    return False


def chunk_text_with_structure(
    text: str,
    source_file: str,
    source_type: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    extra_metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Split text into structure-aware chunks."""
    raw_blocks = [block.strip() for block in re.split(r"\n\s*\n", str(text or "")) if block.strip()]
    if not raw_blocks:
        return []

    current_page: int | None = None
    current_section = "Introduction"
    paragraph_records: list[dict[str, Any]] = []

    for block in raw_blocks:
        page_match = re.match(r"^\[Page\s+(\d+)\]\s*(.*)$", block, flags=re.DOTALL)
        if page_match:
            current_page = int(page_match.group(1))
            block = str(page_match.group(2) or "").strip()
            if not block:
                continue
        if _looks_like_heading(block):
            current_section = block.lstrip("#").strip()
            continue
        paragraph_records.append({"text": block, "page": current_page, "section": current_section})

    chunks: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_len = 0
    chunk_counter = 0

    def finalize(allow_small: bool = False) -> None:
        nonlocal current, current_len, chunk_counter
        if not current:
            return
        joined = "\n\n".join(item["text"] for item in current).strip()
        if len(joined) < MIN_CHUNK_SIZE and not allow_small:
            current = []
            current_len = 0
            return
        page_values = [item["page"] for item in current if item["page"] is not None]
        section = current[0]["section"] if current else current_section
        page_start = min(page_values) if page_values else ""
        page_end = max(page_values) if page_values else ""
        chunk_counter += 1
        chunk_id = hashlib.sha256(
            f"{source_file}:{section}:{page_start}:{page_end}:{chunk_counter}:{joined}".encode("utf-8")
        ).hexdigest()[:16]
        metadata = {
            "source_file": source_file,
            "source_type": source_type,
            "section_title": section,
            "chunk_index": chunk_counter,
        }
        if page_values:
            metadata["page_start"] = page_start
            metadata["page_end"] = page_end
        if extra_metadata:
            metadata.update(extra_metadata)
        chunks.append(
            {
                "id": chunk_id,
                "text": f"[Section: {section}]\n{joined}",
                "metadata": metadata,
            }
        )
        current = current[-1:] if len(current) > 1 else []
        current_len = sum(len(item["text"]) for item in current)

    for paragraph in paragraph_records:
        paragraph_len = len(paragraph["text"])
        if current and current_len + paragraph_len > chunk_size:
            finalize()
        current.append(paragraph)
        current_len += paragraph_len
    finalize(allow_small=not chunks)
    return chunks


def _infer_reaction_family(source_name: str, category_key: str = "") -> str:
    lowered = f"{source_name} {category_key}".lower()
    for hint, family in FAMILY_FROM_FILENAME.items():
        if hint in lowered:
            return family
    return category_key.upper().strip()


def _is_ord_reaction_json(path: Path) -> bool:
    """Return True for ORD reaction payloads we actually want to ingest."""
    return path.name.endswith("_reactions.json")


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_identifier_value(
    identifiers: list[dict[str, Any]] | None,
    preferred_types: tuple[str, ...] = ("NAME", "REACTION_SMILES", "SMILES", "CUSTOM"),
) -> str:
    if not identifiers:
        return ""
    for preferred in preferred_types:
        for item in identifiers:
            if str(item.get("type", "")).upper() == preferred:
                value = str(item.get("value", "")).strip()
                if value:
                    return value
    for item in identifiers:
        value = str(item.get("value", "")).strip()
        if value:
            return value
    return ""


def _extract_measurement_yield(product: dict[str, Any]) -> float | None:
    for measurement in product.get("measurements", []) or []:
        if str(measurement.get("type", "")).upper() != "YIELD":
            continue
        percentage = measurement.get("percentage", {}) or {}
        value = _safe_float(percentage.get("value"))
        if value is not None:
            return value
    return None


def _extract_temperature(conditions: dict[str, Any]) -> float | None:
    temp = (conditions.get("temperature") or {}).get("setpoint", {}) or {}
    return _safe_float(temp.get("value"))


def _extract_reaction_time(outcomes: list[dict[str, Any]]) -> str:
    if not outcomes:
        return ""
    reaction_time = outcomes[0].get("reaction_time", {}) or {}
    value = _safe_float(reaction_time.get("value"))
    units = str(reaction_time.get("units", "")).strip()
    if value is None:
        return ""
    if units:
        return f"{value:g} {units.lower()}"
    return f"{value:g}"


def _normalize_component_text(component: dict[str, Any]) -> str:
    identifiers = component.get("identifiers", []) or []
    value = _extract_identifier_value(identifiers)
    if value:
        return value
    return str(component.get("reaction_role", "")).strip()


def _extract_ord_lists(inputs: dict[str, Any]) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {
        "reactants": [],
        "catalysts": [],
        "ligands": [],
        "bases": [],
        "solvents": [],
        "reagents": [],
    }
    for group_name, payload in (inputs or {}).items():
        group_lower = str(group_name).lower()
        components = payload.get("components", []) or []
        for component in components:
            text = _normalize_component_text(component)
            if not text:
                continue
            role = str(component.get("reaction_role", "")).upper()
            if "ligand" in group_lower:
                buckets["ligands"].append(text)
            elif "base" in group_lower:
                buckets["bases"].append(text)
            elif "solvent" in group_lower or role == "SOLVENT":
                buckets["solvents"].append(text)
            elif any(marker in group_lower for marker in ("catalyst", "metal", "pd_", "ni_", "cocatalyst")) or role == "CATALYST":
                buckets["catalysts"].append(text)
            elif role == "REAGENT":
                buckets["reagents"].append(text)
            else:
                buckets["reactants"].append(text)
    for key, values in buckets.items():
        deduped: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value not in seen:
                deduped.append(value)
                seen.add(value)
        buckets[key] = deduped
    return buckets


def chunk_ord_records(records: list[dict[str, Any]], source_name: str, reaction_family: str = "") -> list[dict[str, Any]]:
    """Convert nested ORD records into structured condition and semantic views."""
    chunks: list[dict[str, Any]] = []
    inferred_family = _infer_reaction_family(source_name, reaction_family)

    for record in records:
        reaction = record.get("reaction", record)
        reaction_id = (
            str(record.get("reaction_id") or reaction.get("reaction_id") or "").strip()
            or "unknown_reaction"
        )
        reaction_smiles = _extract_identifier_value(reaction.get("identifiers", []), preferred_types=("REACTION_SMILES", "SMILES", "NAME", "CUSTOM"))
        inputs = _extract_ord_lists(reaction.get("inputs", {}) or {})
        outcomes = reaction.get("outcomes", []) or []
        desired_products = [product for outcome in outcomes for product in (outcome.get("products", []) or []) if product.get("is_desired_product")]
        yield_value = next((_extract_measurement_yield(product) for product in desired_products if _extract_measurement_yield(product) is not None), None)
        temperature_c = _extract_temperature(reaction.get("conditions", {}) or {})
        reaction_time = _extract_reaction_time(outcomes)
        notes_payload = reaction.get("notes") or {}
        if isinstance(notes_payload, dict):
            procedure_details = str(notes_payload.get("procedure_details", "")).strip()
        else:
            procedure_details = str(notes_payload).strip()
        dataset_name = str(record.get("dataset_name", "")).strip()
        ligands_norm = [_normalize_reagent_name(item) for item in inputs["ligands"][:4]]
        bases_norm = [_normalize_reagent_name(item) for item in inputs["bases"][:4]]
        solvents_norm = [_normalize_reagent_name(item) for item in inputs["solvents"][:4]]
        catalysts_norm = [_normalize_reagent_name(item) for item in inputs["catalysts"][:4]]
        metadata = {
            "source_file": source_name,
            "source_type": "ord",
            "document_id": source_name,
            "reaction_family": inferred_family,
            "reaction_id": reaction_id,
            "dataset_id": str(record.get("dataset_id", "")).strip(),
            "dataset_name": dataset_name,
            "source_relation": str(record.get("source_relation", "")).strip(),
            "temperature_c": temperature_c,
            "yield_percent": yield_value,
            "ligands_norm": json.dumps(ligands_norm),
            "bases_norm": json.dumps(bases_norm),
            "solvents_norm": json.dumps(solvents_norm),
            "catalysts_norm": json.dumps(catalysts_norm),
            "has_yield": yield_value is not None,
        }
        condition_parts: list[str] = []
        if inferred_family:
            condition_parts.append(f"Family:{inferred_family}")
        if inputs["catalysts"]:
            condition_parts.append(f"Cat:{';'.join(inputs['catalysts'][:2])}")
        if inputs["ligands"]:
            condition_parts.append(f"Lig:{';'.join(inputs['ligands'][:2])}")
        if inputs["bases"]:
            condition_parts.append(f"Base:{';'.join(inputs['bases'][:2])}")
        if inputs["solvents"]:
            condition_parts.append(f"Solv:{';'.join(inputs['solvents'][:2])}")
        if temperature_c is not None:
            condition_parts.append(f"Temp:{temperature_c:g}C")
        if reaction_time:
            condition_parts.append(f"Time:{reaction_time}")
        if yield_value is not None:
            condition_parts.append(f"Yield:{yield_value:g}%")
        if reaction_smiles:
            condition_parts.append(f"SMILES:{reaction_smiles[:120]}")
        condition_text = " | ".join(part for part in condition_parts if part).strip()
        if condition_text:
            chunks.append(
                {
                    "id": hashlib.sha256(f"cond:{reaction_id}".encode("utf-8")).hexdigest()[:16],
                    "text": f"{condition_text} | [ORD:{reaction_id}]",
                    "metadata": {**metadata, "chunk_view": "condition_summary"},
                }
            )

        semantic_parts: list[str] = []
        if inferred_family:
            semantic_parts.append(f"Reaction family: {inferred_family}")
        if reaction_smiles:
            semantic_parts.append(f"Reaction SMILES: {reaction_smiles}")
        if inputs["reactants"]:
            semantic_parts.append(f"Reactants: {', '.join(inputs['reactants'][:6])}")
        if inputs["catalysts"]:
            semantic_parts.append(f"Catalyst: {', '.join(inputs['catalysts'][:3])}")
        if inputs["ligands"]:
            semantic_parts.append(f"Ligands: {', '.join(inputs['ligands'][:3])}")
        if inputs["bases"]:
            semantic_parts.append(f"Bases: {', '.join(inputs['bases'][:3])}")
        if inputs["solvents"]:
            semantic_parts.append(f"Solvents: {', '.join(inputs['solvents'][:3])}")
        if temperature_c is not None:
            semantic_parts.append(f"Temperature: {temperature_c:g} C")
        if reaction_time:
            semantic_parts.append(f"Reaction time: {reaction_time}")
        if yield_value is not None:
            semantic_parts.append(f"Yield: {yield_value:g}%")
        if procedure_details:
            semantic_parts.append(f"Procedure: {procedure_details[:600]}")
        if dataset_name:
            semantic_parts.append(f"Dataset: {dataset_name}")
        semantic_text = ". ".join(part.strip().rstrip(".") for part in semantic_parts if part).strip()
        if semantic_text:
            chunks.append(
                {
                    "id": hashlib.sha256(f"sem:{reaction_id}".encode("utf-8")).hexdigest()[:16],
                    "text": f"{semantic_text}. [Source: ORD {reaction_id}]",
                    "metadata": {**metadata, "chunk_view": "semantic_summary"},
                }
            )
    return chunks


class HashingEmbedder:
    """Deterministic fallback embedder when sentence-transformers is unavailable."""

    def __init__(self, dim: int = 384, name: str = "hashing-fallback"):
        self.dim = dim
        self.name = name

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        return np.vstack([self._encode(text) for text in texts]) if texts else np.zeros((0, self.dim))

    def encode_query(self, text: str) -> np.ndarray:
        return self._encode(text)

    def _encode(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dim, dtype=float)
        for token in tokenize_chemistry_text(text):
            index = hash(token) % self.dim
            vector[index] += 1.0
        norm = np.linalg.norm(vector)
        if norm > 0:
            vector /= norm
        return vector


class SentenceTransformerEmbedder:
    """Lazy sentence-transformer backend."""

    def __init__(self, model_name: str):
        self.model_name = model_name
        self.name = model_name
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            try:
                self._model = SentenceTransformer(self.model_name, local_files_only=True)
            except Exception:
                self._model = SentenceTransformer(self.model_name)
        return self._model

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 1))
        return np.asarray(self.model.encode(texts, show_progress_bar=False), dtype=float)

    def encode_query(self, text: str) -> np.ndarray:
        return np.asarray(self.model.encode([text], show_progress_bar=False)[0], dtype=float)


class TokenOverlapReranker:
    def score(self, query: str, chunks: list[RetrievedChunk]) -> list[float]:
        query_tokens = set(tokenize_chemistry_text(query))
        scores: list[float] = []
        for chunk in chunks:
            chunk_tokens = set(tokenize_chemistry_text(chunk.content))
            overlap = len(query_tokens & chunk_tokens)
            denom = max(len(query_tokens), 1)
            scores.append(overlap / denom)
        return scores


class CrossEncoderReranker:
    def __init__(self, model_name: str):
        self.model_name = model_name
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self.model_name)
        return self._model

    def score(self, query: str, chunks: list[RetrievedChunk]) -> list[float]:
        pairs = [(query, chunk.content) for chunk in chunks]
        return [float(score) for score in self.model.predict(pairs)]


class BM25Index:
    """Lightweight chemistry-aware BM25 implementation."""

    def __init__(self):
        self.doc_tokens: list[list[str]] = []
        self.doc_ids: list[str] = []
        self.doc_freqs: dict[str, int] = {}
        self.avgdl: float = 0.0

    def build(self, documents: list[dict[str, str]]) -> None:
        self.doc_tokens = []
        self.doc_ids = []
        self.doc_freqs = {}
        total_len = 0

        for document in documents:
            tokens = tokenize_chemistry_text(document.get("text", ""))
            self.doc_tokens.append(tokens)
            self.doc_ids.append(document["id"])
            total_len += len(tokens)
            for token in set(tokens):
                self.doc_freqs[token] = self.doc_freqs.get(token, 0) + 1

        self.avgdl = total_len / max(len(self.doc_tokens), 1)

    def search(self, query_texts: list[str], top_k: int = 20) -> list[tuple[str, float]]:
        if not self.doc_tokens:
            return []
        scores = np.zeros(len(self.doc_tokens), dtype=float)
        for query_text in query_texts:
            query_tokens = tokenize_chemistry_text(query_text)
            if not query_tokens:
                continue
            query_counts = Counter(query_tokens)
            for index, doc_tokens in enumerate(self.doc_tokens):
                token_counts = Counter(doc_tokens)
                doc_len = len(doc_tokens) or 1
                total_score = 0.0
                for token, qf in query_counts.items():
                    if token not in token_counts:
                        continue
                    df = self.doc_freqs.get(token, 0)
                    if df <= 0:
                        continue
                    idf = math.log(1 + (len(self.doc_tokens) - df + 0.5) / (df + 0.5))
                    freq = token_counts[token]
                    k1 = 1.5
                    b = 0.75
                    denom = freq + k1 * (1 - b + b * doc_len / max(self.avgdl, 1.0))
                    total_score += qf * idf * ((freq * (k1 + 1)) / max(denom, 1e-9))
                scores[index] = max(scores[index], total_score)

        top_indices = np.argsort(scores)[::-1][:top_k]
        results: list[tuple[str, float]] = []
        for idx in top_indices:
            if scores[idx] > 0:
                results.append((self.doc_ids[idx], float(scores[idx])))
        return results


def _cosine_similarity(left: np.ndarray, right: np.ndarray) -> float:
    left_norm = np.linalg.norm(left)
    right_norm = np.linalg.norm(right)
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return float(np.dot(left, right) / (left_norm * right_norm))


def _metadata_matches(metadata: dict[str, Any], where: dict[str, Any] | None) -> bool:
    if not where:
        return True
    for key, expected in where.items():
        actual = metadata.get(key)
        if isinstance(expected, dict) and "$in" in expected:
            if actual not in expected["$in"]:
                return False
        elif actual != expected:
            return False
    return True


def _offline_compress(query: str, content: str, max_sentences: int = 3) -> str:
    sentences = split_sentences(content)
    if not sentences:
        return content[:400]
    query_tokens = set(tokenize_chemistry_text(query))
    if not query_tokens:
        return " ".join(sentences[:max_sentences])
    scored: list[tuple[float, int, str]] = []
    for index, sentence in enumerate(sentences):
        sentence_tokens = set(tokenize_chemistry_text(sentence))
        overlap = len(query_tokens & sentence_tokens)
        score = overlap / max(len(query_tokens), 1)
        if overlap > 0:
            scored.append((score, index, sentence))
    if not scored:
        return " ".join(sentences[:max_sentences])
    scored.sort(key=lambda item: (-item[0], item[1]))
    selected_indices = sorted(index for _, index, _ in scored[:max_sentences])
    return " ".join(sentences[index] for index in selected_indices)


def _extract_pdf_document(pdf_path: Path) -> dict[str, Any]:
    import fitz

    doc = fitz.open(str(pdf_path))
    page_blocks: list[str] = []
    try:
        for page_number, page in enumerate(doc, start=1):
            text = page.get_text("text")
            if text.strip():
                page_blocks.append(f"[Page {page_number}]\n{text}")
    finally:
        doc.close()
    full_text = "\n\n".join(page_blocks)
    doi_match = re.search(r"10\.\d{4,9}/[^\s)]+", full_text[:4000])
    title = pdf_path.stem
    for line in full_text[:1200].splitlines():
        cleaned = line.strip()
        if cleaned and not cleaned.startswith("[Page") and len(cleaned) > 12:
            title = cleaned[:240]
            break
    return {"text": full_text, "doi": doi_match.group(0).rstrip(".") if doi_match else "", "title": title}


class LocalRAGStore:
    """Hybrid local retrieval over ORD, reviews, textbooks, and supplementary files."""

    def __init__(
        self,
        config: LocalRAGConfig | None = None,
        settings: Settings | None = None,
        llm_adapter: RAGLLMAdapter | None = None,
        embedder: Any | None = None,
        reranker: Any | None = None,
    ):
        self.settings = settings or Settings()
        self.config = config or LocalRAGConfig.from_settings(self.settings)
        self.persist_dir = Path(self.config.persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self.llm_adapter = llm_adapter or RAGLLMAdapter(settings=self.settings)
        self._embedder = embedder
        self._reranker = reranker
        self._backend_name = "json"
        self._client = None
        self._collections: dict[str, dict[str, StoredChunk]] = {name: {} for name in ALL_COLLECTIONS}
        self._loaded_collections: set[str] = set()
        self._bm25_indices: dict[str, BM25Index] = {}
        self._init_backend()

    def _init_backend(self) -> None:
        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            self._client = chromadb.PersistentClient(
                path=str(self.persist_dir / "chromadb"),
                settings=ChromaSettings(anonymized_telemetry=False),
            )
            self._backend_name = "chromadb"
        except Exception as exc:
            logger.warning("ChromaDB unavailable, falling back to JSON persistence: %s", exc)
            self._backend_name = "json"
            self._client = None

    @property
    def backend_name(self) -> str:
        return self._backend_name

    @property
    def embedder(self):
        if self._embedder is not None:
            return self._embedder
        try:
            import sentence_transformers  # noqa: F401

            self._embedder = SentenceTransformerEmbedder(self.config.embedding_model)
        except Exception as exc:
            logger.warning("Embedding model '%s' unavailable, using hashing fallback: %s", self.config.embedding_model, exc)
            self._embedder = HashingEmbedder()
        return self._embedder

    @property
    def reranker(self):
        if self._reranker is not None:
            return self._reranker
        if not self.config.enable_local_rerank:
            return None
        try:
            import sentence_transformers  # noqa: F401

            self._reranker = CrossEncoderReranker(self.config.reranker_model)
        except Exception as exc:
            logger.warning("Cross-encoder reranker unavailable, using token-overlap fallback: %s", exc)
            self._reranker = TokenOverlapReranker()
        return self._reranker

    def build_index(
        self,
        data_dir: str | None = None,
        ord_dir: str | None = None,
        reviews_dir: str | None = None,
        textbooks_dir: str | None = None,
        supplementary_dir: str | None = None,
        clear_existing: bool = False,
    ) -> dict[str, int]:
        if data_dir:
            base = Path(data_dir)
            ord_dir = ord_dir or str(base / "ord_data")
            reviews_dir = reviews_dir or str(base / "reviews")
            textbooks_dir = textbooks_dir or str(base / "textbooks")
            supplementary_dir = supplementary_dir or str(base / "supplementary")

        if clear_existing:
            for collection in ALL_COLLECTIONS:
                self.clear_collection(collection)

        if ord_dir:
            for json_path in sorted(Path(ord_dir).glob("*.json")) if Path(ord_dir).exists() else []:
                if not _is_ord_reaction_json(json_path):
                    logger.debug("Skipping non-reaction ORD JSON file: %s", json_path.name)
                    continue
                self.ingest_ord_json(str(json_path))
        if reviews_dir:
            self.ingest_pdfs(reviews_dir, COLLECTION_REVIEWS)
        if textbooks_dir:
            self.ingest_pdfs(textbooks_dir, COLLECTION_TEXTBOOKS)
        if supplementary_dir:
            path = Path(supplementary_dir)
            if path.exists():
                self.ingest_pdfs(str(path), COLLECTION_SUPPLEMENTARY)
                self.ingest_text_files(str(path), COLLECTION_SUPPLEMENTARY, source_type="supplementary")
        self.rebuild_bm25_indices()
        return self.get_stats()

    def ingest_pdfs(self, pdf_dir: str, collection: str, extra_metadata: dict[str, Any] | None = None) -> int:
        pdf_path = Path(pdf_dir)
        if not pdf_path.exists():
            logger.warning("PDF directory does not exist: %s", pdf_dir)
            return 0

        all_chunks: list[dict[str, Any]] = []
        source_type = {
            COLLECTION_REVIEWS: "review",
            COLLECTION_TEXTBOOKS: "textbook",
            COLLECTION_SUPPLEMENTARY: "supplementary",
        }.get(collection, "document")

        for pdf_file in sorted(pdf_path.glob("*.pdf")):
            try:
                extracted = _extract_pdf_document(pdf_file)
            except Exception as exc:
                logger.warning("Failed to extract PDF '%s': %s", pdf_file.name, exc)
                continue
            metadata = {
                "document_id": pdf_file.name,
                "document_title": extracted.get("title", pdf_file.stem),
                "doi": extracted.get("doi", ""),
            }
            if extra_metadata:
                metadata.update(extra_metadata)
            chunks = chunk_text_with_structure(
                extracted.get("text", ""),
                source_file=pdf_file.name,
                source_type=source_type,
                chunk_size=self.config.chunk_size,
                extra_metadata=metadata,
            )
            all_chunks.extend(chunks)
        self._upsert_chunks(collection, all_chunks)
        return len(all_chunks)

    def ingest_text_files(self, text_dir: str, collection: str, source_type: str = "supplementary") -> int:
        directory = Path(text_dir)
        if not directory.exists():
            return 0
        all_chunks: list[dict[str, Any]] = []
        for file_path in sorted(list(directory.glob("*.md")) + list(directory.glob("*.txt"))):
            text = file_path.read_text(encoding="utf-8", errors="replace")
            chunks = chunk_text_with_structure(
                text,
                source_file=file_path.name,
                source_type=source_type,
                chunk_size=self.config.chunk_size,
                extra_metadata={"document_id": file_path.name, "document_title": file_path.stem},
            )
            all_chunks.extend(chunks)
        self._upsert_chunks(collection, all_chunks)
        return len(all_chunks)

    def ingest_ord_json(self, json_path: str, reaction_family: str = "") -> int:
        path = Path(json_path)
        if not path.exists():
            logger.warning("ORD JSON file does not exist: %s", json_path)
            return 0

        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            records = payload.get("records", []) or []
            category_key = str(payload.get("category_key", "")).strip()
        elif isinstance(payload, list):
            records = payload
            category_key = ""
        else:
            logger.warning("Unsupported ORD payload format in %s", json_path)
            return 0

        if not records:
            logger.debug("No ORD records found in %s", path.name)
            return 0

        chunks = chunk_ord_records(records, source_name=path.name, reaction_family=reaction_family or category_key)
        self._upsert_chunks(COLLECTION_ORD, chunks)
        return len(chunks)

    def get_stats(self) -> dict[str, int]:
        stats = {collection: self._collection_count(collection) for collection in ALL_COLLECTIONS}
        stats["backend"] = self.backend_name  # type: ignore[assignment]
        return stats

    def _collection_count(self, collection: str) -> int:
        self._ensure_collection_loaded(collection)
        return len(self._collections.get(collection, {}))

    def clear_collection(self, collection: str) -> None:
        self._collections[collection] = {}
        self._loaded_collections.discard(collection)
        self._bm25_indices.pop(collection, None)

        json_path = self._json_collection_path(collection)
        if json_path.exists():
            json_path.unlink()
        if self._client is not None:
            try:
                self._client.delete_collection(collection)
            except Exception:
                pass

    def rebuild_bm25_indices(self) -> None:
        self._bm25_indices = {}
        for collection in ALL_COLLECTIONS:
            self._build_bm25_index(collection)

    def search_with_plan(
        self,
        plan: ReactionRetrievalPlan,
        top_k: int | None = None,
    ) -> EvidenceBundle:
        top_k = top_k or self.config.top_k
        notes: list[str] = []

        precedent_result = RetrievalResult(
            query=plan.to_legacy_query(),
            normalized_query="",
            chunks=[],
            method="skipped",
        )
        if "precedent" in plan.active_goals:
            precedent_result = self._search_precedent(plan.precedent, top_k=max(top_k * 2, top_k))
            notes.extend(precedent_result.notes)

        mechanism_result = RetrievalResult(
            query=plan.to_legacy_query(),
            normalized_query="",
            chunks=[],
            method="skipped",
        )
        if "mechanism" in plan.active_goals:
            mechanism_result = self._search_mechanism(plan.mechanism, top_k=top_k, prefer_sources=plan.prefer_sources)
            notes.extend(mechanism_result.notes)

        property_result = RetrievalResult(
            query=plan.to_legacy_query(),
            normalized_query="",
            chunks=[],
            method="skipped",
        )
        if "property" in plan.active_goals and plan.property.reagent_names:
            property_result = self._search_property(plan.property, top_k=top_k, prefer_sources=plan.prefer_sources)
            notes.extend(property_result.notes)

        mechanism_summary = self._compress_mechanism(plan.mechanism, mechanism_result.chunks)
        role_evidence = self._build_role_evidence(precedent_result.chunks, plan.precedent.variable_roles)
        return EvidenceBundle(
            plan=plan,
            role_evidence=role_evidence,
            mechanism_chunks=mechanism_result.chunks,
            mechanism_summary=mechanism_summary,
            property_chunks=property_result.chunks,
            precedent_result=precedent_result,
            mechanism_result=mechanism_result,
            property_result=property_result,
            notes=notes,
        )

    def _search_precedent(self, query: PrecedentQuery, top_k: int) -> RetrievalResult:
        where = query.to_filter_dict()
        query_text = query.to_text()
        query_texts = [query_text] if query_text else [query.reaction_family]
        dense_results = self._dense_search(query_texts, [COLLECTION_ORD], max(top_k * 3, top_k), where=where)
        sparse_results = self._sparse_search(query_texts, [COLLECTION_ORD], max(top_k * 3, top_k), where=where)
        fused = self._fuse_ranked_results(dense_results, sparse_results, max(top_k * 2, top_k))
        fused = self._condition_rerank(fused, query)[:top_k]
        legacy_query = ReactionQuery(
            reaction_text=query.reaction_smiles or query.description,
            reaction_family=query.reaction_family,
            focus_terms=query.variable_roles,
        )
        return RetrievalResult(
            query=legacy_query,
            normalized_query=query_text,
            chunks=fused,
            method="precedent:metadata_filter+dense+sparse+rrf+condition_rerank",
            total_candidates=len({chunk.chunk_id for chunk in dense_results + sparse_results}),
            notes=[f"precedent_filter={where}"],
        )

    def _search_mechanism(
        self,
        query: MechanismQuery,
        top_k: int,
        prefer_sources: list[str] | None = None,
    ) -> RetrievalResult:
        normalized = query.to_text()
        if not normalized:
            normalized = query.reaction_family
        query_texts = [normalized] if normalized else []
        hyde_text = ""
        notes: list[str] = []
        collections = _mechanism_collections(prefer_sources)

        if self.config.enable_hyde and normalized:
            legacy_query = ReactionQuery(description=normalized, reaction_family=query.reaction_family, focus_terms=query.focus_aspects)
            hyde_default = {"hypothetical_passage": "", "cues": []}
            response = self.llm_adapter.generate_hyde(legacy_query.to_dict(), hyde_default)
            hyde_text = str(response.payload.get("hypothetical_passage", "")).strip()
            if response.used_fallback:
                notes.append(f"LLM HyDE fallback: {response.error or 'disabled'}")

        built_query_texts = self._build_query_texts(normalized, [], [], hyde_text)
        dense_results = self._dense_search(built_query_texts, collections, max(top_k * 3, top_k))
        sparse_results = self._sparse_search(built_query_texts, collections, max(top_k * 3, top_k))
        fused = self._fuse_ranked_results(dense_results, sparse_results, max(top_k * 2, top_k))[:top_k]
        if self.config.enable_contextual_compression:
            legacy_query = ReactionQuery(description=normalized, reaction_family=query.reaction_family, focus_terms=query.focus_aspects)
            for chunk in fused:
                chunk.compressed_content = self._compress_chunk(legacy_query, chunk)
        return RetrievalResult(
            query=ReactionQuery(description=normalized, reaction_family=query.reaction_family, focus_terms=query.focus_aspects),
            normalized_query=normalized,
            expanded_terms=[],
            alternate_queries=[],
            hyde_text=hyde_text,
            chunks=fused,
            method="mechanism:hyde+dense+sparse+rrf+compress",
            total_candidates=len({chunk.chunk_id for chunk in dense_results + sparse_results}),
            notes=notes,
        )

    def _search_property(
        self,
        query: PropertyQuery,
        top_k: int,
        prefer_sources: list[str] | None = None,
    ) -> RetrievalResult:
        normalized = query.to_text()
        collections = _property_collections(prefer_sources)
        sparse_results = self._sparse_search([normalized], collections, max(top_k * 2, top_k))
        final_chunks = sparse_results[:top_k]
        return RetrievalResult(
            query=ReactionQuery(description=normalized, focus_terms=query.reagent_names),
            normalized_query=normalized,
            chunks=final_chunks,
            method="property:bm25_only",
            total_candidates=len(sparse_results),
        )

    def _condition_rerank(self, chunks: list[RetrievedChunk], query: PrecedentQuery) -> list[RetrievedChunk]:
        if not chunks:
            return chunks
        fixed_by_role: dict[str, set[str]] = defaultdict(set)
        for item in query.known_fixed_context:
            role = str(item.get("role", "")).strip().lower()
            value = _normalize_reagent_name(item.get("value", ""))
            if role and value:
                fixed_by_role[role].add(value)
        role_to_field = {
            "ligand": "ligands_norm",
            "base": "bases_norm",
            "solvent": "solvents_norm",
            "catalyst_precursor": "catalysts_norm",
        }
        for chunk in chunks:
            bonus = 0.0
            for role, field in role_to_field.items():
                expected = fixed_by_role.get(role, set())
                if not expected:
                    continue
                try:
                    values = json.loads(chunk.metadata.get(field) or "[]")
                except (TypeError, json.JSONDecodeError):
                    values = []
                actual = {_normalize_reagent_name(item) for item in values}
                bonus += 0.1 * len(expected & actual)
            if chunk.metadata.get("yield_percent") is not None:
                bonus += 0.03
            chunk.rerank_score = chunk.fusion_score + bonus
        return sorted(chunks, key=lambda item: (item.rerank_score, item.fusion_score), reverse=True)

    def _build_role_evidence(
        self,
        precedent_chunks: list[RetrievedChunk],
        variable_roles: list[str],
    ) -> dict[str, RoleEvidence]:
        role_to_field = {
            "ligand": "ligands_norm",
            "base": "bases_norm",
            "solvent": "solvents_norm",
            "catalyst_precursor": "catalysts_norm",
        }
        role_evidence: dict[str, RoleEvidence] = {}
        roles = [role for role in variable_roles if role in role_to_field]
        for role in roles:
            field = role_to_field[role]
            counts: Counter[str] = Counter()
            supporting: list[RetrievedChunk] = []
            for chunk in precedent_chunks:
                try:
                    values = json.loads(chunk.metadata.get(field) or "[]")
                except (TypeError, json.JSONDecodeError):
                    values = []
                normalized = [_normalize_reagent_name(item) for item in values if _normalize_reagent_name(item)]
                if not normalized:
                    continue
                counts.update(normalized)
                supporting.append(chunk)
            if not counts:
                continue
            top_values = [item for item, _ in counts.most_common(8)]
            confidence = min(0.9, 0.3 + 0.12 * len(supporting) + 0.05 * min(len(top_values), 4))
            notes = [f"Most common {role.replace('_', ' ')}: {top_values[0]} ({counts[top_values[0]]} precedents)"]
            if len(top_values) > 1:
                notes.append(f"Alternatives: {', '.join(top_values[1:4])}")
            role_evidence[role] = RoleEvidence(
                role=role,
                top_values=top_values,
                supporting_chunks=supporting[:5],
                confidence=round(confidence, 3),
                notes=notes,
            )
        return role_evidence

    def _compress_mechanism(self, query: MechanismQuery, chunks: list[RetrievedChunk]) -> str:
        if not chunks:
            return ""
        best = chunks[0]
        content = best.compressed_content or best.content
        if self.llm_adapter.available:
            response = self.llm_adapter.compress_chunk(
                {"description": query.to_text(), "focus_aspects": query.focus_aspects},
                {"content": content[:2000]},
                {"compressed_snippet": "", "kept_points": []},
            )
            snippet = str(response.payload.get("compressed_snippet", "")).strip()
            if snippet:
                return snippet
        return _offline_compress(query.to_text(), content, max_sentences=2)

    def _fuse_ranked_results(
        self,
        dense_results: list[RetrievedChunk],
        sparse_results: list[RetrievedChunk],
        limit: int,
    ) -> list[RetrievedChunk]:
        dense_ranks = {chunk.chunk_id: rank for rank, chunk in enumerate(dense_results)}
        sparse_ranks = {chunk.chunk_id: rank for rank, chunk in enumerate(sparse_results)}
        chunk_map: dict[str, RetrievedChunk] = {}
        for chunk in dense_results + sparse_results:
            if chunk.chunk_id not in chunk_map:
                chunk_map[chunk.chunk_id] = chunk
            else:
                existing = chunk_map[chunk.chunk_id]
                existing.dense_score = max(existing.dense_score, chunk.dense_score)
                existing.sparse_score = max(existing.sparse_score, chunk.sparse_score)
        for chunk_id in set(dense_ranks) | set(sparse_ranks):
            dense_rank = dense_ranks.get(chunk_id, limit + 1)
            sparse_rank = sparse_ranks.get(chunk_id, limit + 1)
            chunk_map[chunk_id].fusion_score = 1.0 / (RRF_K + dense_rank) + 1.0 / (RRF_K + sparse_rank)
        fused = sorted(chunk_map.values(), key=lambda chunk: chunk.fusion_score, reverse=True)
        return fused[:limit]

    def search(
        self,
        query: str | ReactionQuery,
        top_k: int | None = None,
        collections: list[str] | None = None,
        where: dict[str, Any] | None = None,
    ) -> RetrievalResult:
        reaction_query = self._normalize_query_object(query)
        normalized_query = self._normalize_query_text(reaction_query)
        top_k = top_k or self.config.top_k
        collections = collections or list(ALL_COLLECTIONS)

        alternate_queries: list[str] = []
        expanded_terms: list[str] = []
        hyde_text = ""
        notes: list[str] = []

        lexical_terms, lexical_queries = self._lexical_expand(reaction_query, normalized_query)
        expanded_terms.extend(lexical_terms)
        alternate_queries.extend(lexical_queries)

        if self.config.enable_hyde:
            hyde_default = {"hypothetical_passage": "", "cues": []}
            hyde_response = self.llm_adapter.generate_hyde(reaction_query.to_dict(), hyde_default)
            hyde_text = str(hyde_response.payload.get("hypothetical_passage", "")).strip()
            if hyde_response.used_fallback:
                notes.append(f"LLM HyDE fallback: {hyde_response.error or 'disabled'}")

        query_texts = self._build_query_texts(normalized_query, alternate_queries, expanded_terms, hyde_text)
        dense_results = self._dense_search(query_texts, collections, top_k * 6, where=where)
        sparse_results = self._sparse_search(query_texts, collections, top_k * 6, where=where)

        dense_ranks = {chunk.chunk_id: rank for rank, chunk in enumerate(dense_results)}
        sparse_ranks = {chunk.chunk_id: rank for rank, chunk in enumerate(sparse_results)}
        all_ids = set(dense_ranks) | set(sparse_ranks)
        chunk_map: dict[str, RetrievedChunk] = {}
        for chunk in dense_results + sparse_results:
            if chunk.chunk_id not in chunk_map:
                chunk_map[chunk.chunk_id] = chunk
            else:
                existing = chunk_map[chunk.chunk_id]
                existing.dense_score = max(existing.dense_score, chunk.dense_score)
                existing.sparse_score = max(existing.sparse_score, chunk.sparse_score)

        for chunk_id in all_ids:
            dense_rank = dense_ranks.get(chunk_id, top_k * 6 + 1)
            sparse_rank = sparse_ranks.get(chunk_id, top_k * 6 + 1)
            chunk_map[chunk_id].fusion_score = 1.0 / (RRF_K + dense_rank) + 1.0 / (RRF_K + sparse_rank)

        fused = sorted(chunk_map.values(), key=lambda chunk: chunk.fusion_score, reverse=True)
        fused = fused[: max(top_k * 2, top_k)]

        if self.reranker and fused:
            rerank_scores = self.reranker.score(normalized_query, fused)
            for chunk, score in zip(fused, rerank_scores):
                chunk.rerank_score = float(score)
            fused.sort(key=lambda chunk: (chunk.rerank_score, chunk.fusion_score), reverse=True)

        if self.config.enable_llm_rerank and fused:
            llm_default = {"scores": []}
            payloads = [
                {
                    "chunk_id": chunk.chunk_id,
                    "collection": chunk.collection,
                    "content": chunk.content[:1500],
                    "metadata": chunk.metadata,
                }
                for chunk in fused
            ]
            rerank_response = self.llm_adapter.rerank_chunks(reaction_query.to_dict(), payloads, llm_default)
            score_map = {
                str(item.get("chunk_id")): float(item.get("score", 0.0))
                for item in rerank_response.payload.get("scores", []) or []
                if item.get("chunk_id")
            }
            for chunk in fused:
                if chunk.chunk_id in score_map:
                    chunk.rerank_score = score_map[chunk.chunk_id]
            if score_map:
                fused.sort(key=lambda chunk: (chunk.rerank_score, chunk.fusion_score), reverse=True)
            elif rerank_response.used_fallback:
                notes.append(f"LLM rerank fallback: {rerank_response.error or 'no scores'}")

        final_chunks = fused[:top_k]
        if self.config.enable_contextual_compression:
            for chunk in final_chunks:
                chunk.compressed_content = self._compress_chunk(reaction_query, chunk)

        method_parts = ["dense", "sparse", "rrf"]
        if self.config.enable_hyde:
            method_parts.append("hyde")
        if self.config.enable_contextual_compression:
            method_parts.append("compress")
        if self.reranker:
            method_parts.append("local-rerank")
        if self.config.enable_llm_rerank:
            method_parts.append("llm-rerank")

        return RetrievalResult(
            query=reaction_query,
            normalized_query=normalized_query,
            expanded_terms=_dedupe_strs(expanded_terms),
            alternate_queries=_dedupe_strs(alternate_queries),
            hyde_text=hyde_text,
            chunks=final_chunks,
            method="+".join(method_parts),
            total_candidates=len(all_ids),
            notes=notes,
        )

    def search_reaction(
        self,
        reaction_text: str,
        description: str = "",
        reaction_family: str = "",
        focus_terms: list[str] | None = None,
        top_k: int | None = None,
        collections: list[str] | None = None,
        where: dict[str, Any] | None = None,
    ) -> RetrievalResult:
        query = ReactionQuery(
            reaction_text=reaction_text,
            description=description,
            reaction_family=reaction_family,
            focus_terms=focus_terms or [],
        )
        return self.search(query, top_k=top_k, collections=collections, where=where)

    def _normalize_query_object(self, query: str | ReactionQuery) -> ReactionQuery:
        if isinstance(query, ReactionQuery):
            return query
        text = str(query).strip()
        return ReactionQuery(description=text)

    def _normalize_query_text(self, query: ReactionQuery) -> str:
        base = query.to_text()
        tokens = tokenize_chemistry_text(base)
        return " ".join(tokens) if tokens else base

    def _lexical_expand(self, query: ReactionQuery, normalized_query: str) -> tuple[list[str], list[str]]:
        expanded_terms: list[str] = []
        alternate_queries: list[str] = []
        lowered_text = f"{normalized_query} {query.reaction_family}".lower()

        family_key = query.reaction_family.upper().strip()
        if family_key in REACTION_FAMILY_ALIASES:
            family_aliases = REACTION_FAMILY_ALIASES[family_key]
            expanded_terms.extend(family_aliases)
            alternate_queries.extend(f"{normalized_query} {' '.join(family_aliases)}".strip() for _ in range(1))

        for key, values in CHEMISTRY_SYNONYM_MAP.items():
            if key in lowered_text:
                expanded_terms.extend(values)
        return _dedupe_strs(expanded_terms), _dedupe_strs(alternate_queries)

    def _build_query_texts(
        self,
        normalized_query: str,
        alternate_queries: list[str],
        expanded_terms: list[str],
        hyde_text: str,
    ) -> list[str]:
        texts = [normalized_query]
        texts.extend(alternate_queries)
        if expanded_terms:
            texts.append(f"{normalized_query} {' '.join(expanded_terms)}".strip())
        if hyde_text:
            texts.append(hyde_text)
        return _dedupe_strs([text for text in texts if text.strip()])

    def _dense_search(
        self,
        query_texts: list[str],
        collections: list[str],
        top_k: int,
        where: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        query_embeddings = [self._encode_query(query_text) for query_text in query_texts]
        results: list[RetrievedChunk] = []
        for collection in collections:
            self._ensure_collection_loaded(collection)
            for stored in self._collections.get(collection, {}).values():
                if not _metadata_matches(stored.metadata, where):
                    continue
                score = max(_cosine_similarity(query_embedding, stored.embedding) for query_embedding in query_embeddings)
                if score <= 0:
                    continue
                results.append(
                    RetrievedChunk(
                        chunk_id=stored.chunk_id,
                        content=stored.content,
                        collection=collection,
                        metadata=dict(stored.metadata),
                        dense_score=score,
                    )
                )
        results.sort(key=lambda chunk: chunk.dense_score, reverse=True)
        return results[:top_k]

    def _sparse_search(
        self,
        query_texts: list[str],
        collections: list[str],
        top_k: int,
        where: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        results: list[RetrievedChunk] = []
        for collection in collections:
            self._build_bm25_index(collection)
            bm25 = self._bm25_indices.get(collection)
            if bm25 is None:
                continue
            hits = bm25.search(query_texts, top_k=top_k * 2)
            for chunk_id, score in hits:
                stored = self._collections.get(collection, {}).get(chunk_id)
                if stored is None or not _metadata_matches(stored.metadata, where):
                    continue
                results.append(
                    RetrievedChunk(
                        chunk_id=stored.chunk_id,
                        content=stored.content,
                        collection=collection,
                        metadata=dict(stored.metadata),
                        sparse_score=score,
                    )
                )
        results.sort(key=lambda chunk: chunk.sparse_score, reverse=True)
        return results[:top_k]

    def _compress_chunk(self, query: ReactionQuery, chunk: RetrievedChunk) -> str:
        query_payload = query.to_dict()
        chunk_payload = {
            "chunk_id": chunk.chunk_id,
            "collection": chunk.collection,
            "content": chunk.content[:4000],
            "metadata": chunk.metadata,
        }
        if self.llm_adapter.available:
            response = self.llm_adapter.compress_chunk(
                query_payload,
                chunk_payload,
                default={"compressed_snippet": "", "kept_points": []},
            )
            snippet = str(response.payload.get("compressed_snippet", "")).strip()
            if snippet:
                return snippet
        return _offline_compress(query.to_text(), chunk.content)

    def _json_collection_path(self, collection: str) -> Path:
        return self.persist_dir / f"{collection}.json"

    def _save_collection_snapshot(self, collection: str) -> None:
        path = self._json_collection_path(collection)
        payload = [
            {
                "chunk_id": chunk.chunk_id,
                "content": chunk.content,
                "metadata": chunk.metadata,
                "embedding": chunk.embedding.tolist(),
            }
            for chunk in self._collections.get(collection, {}).values()
        ]
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _load_collection_snapshot(self, collection: str) -> dict[str, StoredChunk]:
        path = self._json_collection_path(collection)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Failed to decode cached collection snapshot %s", path)
            return {}
        loaded: dict[str, StoredChunk] = {}
        for item in payload if isinstance(payload, list) else []:
            chunk_id = str(item.get("chunk_id", "")).strip()
            if not chunk_id:
                continue
            loaded[chunk_id] = StoredChunk(
                chunk_id=chunk_id,
                content=str(item.get("content", "")),
                metadata=dict(item.get("metadata", {})),
                embedding=np.asarray(item.get("embedding", []), dtype=float),
            )
        return loaded

    def _ensure_collection_loaded(self, collection: str) -> None:
        if collection in self._loaded_collections:
            return

        loaded = self._load_collection_snapshot(collection)

        if not loaded and self._client is not None:
            try:
                chroma_collection = self._client.get_collection(collection)
                payload = chroma_collection.get(include=["documents", "metadatas", "embeddings"])
                loaded = {}
                for index, chunk_id in enumerate(payload.get("ids", []) or []):
                    loaded[chunk_id] = StoredChunk(
                        chunk_id=chunk_id,
                        content=(payload.get("documents") or [""])[index],
                        metadata=(payload.get("metadatas") or [{}])[index],
                        embedding=np.asarray((payload.get("embeddings") or [[]])[index], dtype=float),
                    )
            except Exception:
                pass

        self._collections[collection] = loaded
        self._loaded_collections.add(collection)

    def _upsert_chunks(self, collection: str, chunks: list[dict[str, Any]]) -> None:
        if not chunks:
            return
        self._ensure_collection_loaded(collection)
        deduped_chunks: dict[str, dict[str, Any]] = {}
        duplicate_count = 0
        for chunk in chunks:
            chunk_id = str(chunk["id"])
            if chunk_id in deduped_chunks:
                duplicate_count += 1
            deduped_chunks[chunk_id] = chunk
        if duplicate_count:
            logger.info("Deduplicated %s repeated chunk IDs before upsert into '%s'.", duplicate_count, collection)
        unique_chunks = list(deduped_chunks.values())
        texts = [chunk["text"] for chunk in unique_chunks]
        embeddings = self._encode_documents(texts)
        for chunk, embedding in zip(unique_chunks, embeddings):
            stored = StoredChunk(
                chunk_id=str(chunk["id"]),
                content=str(chunk["text"]),
                metadata=dict(chunk.get("metadata", {})),
                embedding=np.asarray(embedding, dtype=float),
            )
            self._collections[collection][stored.chunk_id] = stored

        self._save_collection_snapshot(collection)
        self._bm25_indices.pop(collection, None)

        if self._client is not None:
            try:
                chroma_collection = self._client.get_or_create_collection(collection, metadata={"hnsw:space": "cosine"})
                ids = [str(chunk["id"]) for chunk in unique_chunks]
                chroma_collection.upsert(
                    ids=ids,
                    documents=texts,
                    embeddings=[embedding.tolist() for embedding in embeddings],
                    metadatas=[dict(chunk.get("metadata", {})) for chunk in unique_chunks],
                )
            except Exception as exc:
                logger.warning("ChromaDB upsert failed for '%s': %s", collection, exc)

    def _build_bm25_index(self, collection: str) -> None:
        if collection in self._bm25_indices:
            return
        self._ensure_collection_loaded(collection)
        documents = [
            {"id": stored.chunk_id, "text": stored.content}
            for stored in self._collections.get(collection, {}).values()
        ]
        if not documents:
            return
        index = BM25Index()
        index.build(documents)
        self._bm25_indices[collection] = index

    def _encode_documents(self, texts: list[str]) -> np.ndarray:
        try:
            return self.embedder.encode_documents(texts)
        except Exception as exc:
            if isinstance(self._embedder, HashingEmbedder):
                raise
            logger.warning("Primary embedder failed during document encoding, switching to hashing fallback: %s", exc)
            self._embedder = HashingEmbedder()
            return self._embedder.encode_documents(texts)

    def _encode_query(self, text: str) -> np.ndarray:
        try:
            return self.embedder.encode_query(text)
        except Exception as exc:
            if isinstance(self._embedder, HashingEmbedder):
                raise
            logger.warning("Primary embedder failed during query encoding, switching to hashing fallback: %s", exc)
            self._embedder = HashingEmbedder()
            return self._embedder.encode_query(text)


def _dedupe_strs(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in seen:
            result.append(cleaned)
            seen.add(cleaned)
    return result


def _mechanism_collections(prefer_sources: list[str] | None) -> list[str]:
    preferred = [item for item in (prefer_sources or []) if item in {"reviews", "textbooks", "supplementary"}]
    collections = [_source_to_collection(item) for item in preferred]
    default = [COLLECTION_REVIEWS, COLLECTION_TEXTBOOKS]
    return _dedupe_strs(collections + default)


def _property_collections(prefer_sources: list[str] | None) -> list[str]:
    preferred = [_source_to_collection(item) for item in (prefer_sources or []) if item in {"ord", "reviews", "textbooks", "supplementary"}]
    return _dedupe_strs(preferred + list(ALL_COLLECTIONS))


def _source_to_collection(source: str) -> str:
    mapping = {
        "ord": COLLECTION_ORD,
        "reviews": COLLECTION_REVIEWS,
        "textbooks": COLLECTION_TEXTBOOKS,
        "supplementary": COLLECTION_SUPPLEMENTARY,
    }
    return mapping.get(str(source).strip().lower(), str(source).strip().lower())


def format_retrieval_result(result: RetrievalResult) -> str:
    lines = [
        f"Method: {result.method}",
        f"Normalized query: {result.normalized_query}",
        f"Total candidates: {result.total_candidates}",
    ]
    if result.expanded_terms:
        lines.append(f"Expanded terms: {', '.join(result.expanded_terms)}")
    if result.notes:
        lines.extend(f"Note: {note}" for note in result.notes)
    for index, chunk in enumerate(result.chunks, start=1):
        snippet = chunk.compressed_content or chunk.content
        lines.append(
            f"{index}. [{chunk.collection}] {chunk.source_locator()} "
            f"dense={chunk.dense_score:.3f} sparse={chunk.sparse_score:.3f} fusion={chunk.fusion_score:.3f}"
        )
        lines.append(f"   {snippet[:500]}")
    return "\n".join(lines)


def format_evidence_bundle(bundle: EvidenceBundle) -> str:
    lines = [
        f"Active goals: {', '.join(bundle.plan.active_goals)}",
        f"Preferred sources: {', '.join(bundle.plan.prefer_sources)}",
    ]
    if bundle.mechanism_summary:
        lines.append(f"Mechanism summary: {bundle.mechanism_summary}")
    for role, evidence in bundle.role_evidence.items():
        lines.append(
            f"Role {role}: top_values={', '.join(evidence.top_values[:5]) or 'n/a'} confidence={evidence.confidence:.2f}"
        )
        for note in evidence.notes[:2]:
            lines.append(f"  - {note}")
    if bundle.notes:
        lines.extend(f"Note: {note}" for note in bundle.notes)
    return "\n".join(lines)


__all__ = [
    "ALL_COLLECTIONS",
    "BM25Index",
    "COLLECTION_ORD",
    "COLLECTION_REVIEWS",
    "COLLECTION_SUPPLEMENTARY",
    "COLLECTION_TEXTBOOKS",
    "EvidenceBundle",
    "LocalRAGConfig",
    "LocalRAGStore",
    "MechanismQuery",
    "PrecedentQuery",
    "PropertyQuery",
    "ReactionQuery",
    "ReactionRetrievalPlan",
    "RetrievedChunk",
    "RetrievalResult",
    "RoleEvidence",
    "chunk_ord_records",
    "chunk_text_with_structure",
    "format_evidence_bundle",
    "format_retrieval_result",
    "tokenize_chemistry_text",
]
