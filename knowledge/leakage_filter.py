"""
Leakage filtering and evidence-bundle sanitization for retrieved chemistry knowledge.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from knowledge.connectors.base import RetrievedChunk as ConnectorRetrievedChunk
from knowledge.local_rag import (
    EvidenceBundle,
    RetrievedChunk as LocalRetrievedChunk,
    RoleEvidence,
    _normalize_reagent_name,
    _offline_compress,
)

if TYPE_CHECKING:
    from knowledge.llm_adapter import RAGLLMAdapter

logger = logging.getLogger(__name__)


RISK_SAFE = "safe"
RISK_PARTIAL = "partial"
RISK_BLOCKED = "blocked"
LEAKAGE_SUMMARY_PREFIX = "leakage_filter_summary="

YIELD_PATTERN = re.compile(
    r"""
    (?:
        (?:yield|conversion|selectivity|ee|er|dr)
        \s*[:=]?\s*
        [\d]+(?:\.[\d]+)?\s*%
    )
    |
    (?:
        [\d]+(?:\.[\d]+)?\s*%
        \s*(?:yield|conversion|selectivity|ee|er|dr)
    )
    |
    (?:
        (?:yield|conversion|selectivity)\s+
        (?:of\s+|was\s+|=\s*)?
        [\d]+(?:\.[\d]+)?\s*%
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
LOOSE_YIELD_PATTERN = re.compile(
    r"(?:gave|obtained|achieved|produced|resulted\s+in|afforded)\s+[\d]+(?:\.[\d]+)?\s*%",
    re.IGNORECASE,
)
CONDITION_OUTCOME_PATTERN = re.compile(
    r"(?:at\s+\d+(?:\.\d+)?\s*°?\s*[CFK]|using\s+[\w\s\-\(\)\[\]\.,/]+)\s*[,;]\s*"
    r"[\d]+(?:\.[\d]+)?\s*%\s*(?:yield|conversion|selectivity)?",
    re.IGNORECASE,
)
OUTCOME_KEYWORDS = {"yield", "conversion", "selectivity", "ee", "er", "dr"}
YIELD_TO_QUALITATIVE = {
    (0, 20): "very low yield",
    (20, 40): "low yield",
    (40, 60): "moderate yield",
    (60, 80): "good yield",
    (80, 95): "high yield",
    (95, 101): "excellent yield",
}

_RDKIT_WARNING_EMITTED = False


@dataclass
class FilteredChunk:
    """A retrieval chunk annotated with leakage risk and sanitized content."""

    original: Any
    content: str
    leakage_risk: str = RISK_SAFE
    risk_reasons: list[str] = field(default_factory=list)
    is_usable: bool = True

    @property
    def source_type(self) -> str:
        return _chunk_source_type(self.original)

    @property
    def source_id(self) -> str:
        return _chunk_source_id(self.original)

    @property
    def metadata(self) -> dict[str, Any]:
        return _chunk_metadata(self.original)

    @property
    def query(self) -> str:
        return _chunk_query(self.original)

    @property
    def short_source(self) -> str:
        return _chunk_short_source(self.original)


class LeakageFilter:
    """Detect and sanitize reaction-outcome leakage in retrieved text."""

    def __init__(self, problem_spec: dict[str, Any], strict_mode: bool = True):
        self.problem_spec = dict(problem_spec or {})
        self.strict_mode = bool(strict_mode)
        self.reaction_type = self._extract_reaction_type(self.problem_spec)
        self.substrate_names = self._extract_substrate_names(self.problem_spec)
        self.substrate_smiles = self._canonicalize_smiles_list(self._extract_substrate_smiles(self.problem_spec))
        self.product_smiles = self._canonicalize_smiles_list(self._extract_product_smiles(self.problem_spec))
        self.known_variable_values = self._extract_known_variable_values(self.problem_spec)

        logger.info(
            "LeakageFilter initialized reaction_type=%s substrate_smiles=%s substrate_names=%s strict=%s",
            self.reaction_type or "unknown",
            len(self.substrate_smiles),
            len(self.substrate_names),
            self.strict_mode,
        )

    def filter_chunks(self, chunks: list[Any]) -> list[FilteredChunk]:
        results = [self._filter_single(chunk) for chunk in chunks]
        summary = _summarize_filtered_chunks(results)
        logger.info(
            "LeakageFilter processed %s chunks -> safe=%s partial=%s blocked=%s discarded=%s",
            summary["total"],
            summary["safe"],
            summary["partial"],
            summary["blocked"],
            summary["discarded"],
        )
        return results

    def filter_single(self, chunk: Any) -> FilteredChunk:
        return self._filter_single(chunk)

    def _filter_single(self, chunk: Any) -> FilteredChunk:
        try:
            content = _chunk_content(chunk)
            if not content.strip():
                return FilteredChunk(original=chunk, content="", leakage_risk=RISK_SAFE, is_usable=False)

            risk = RISK_SAFE
            reasons: list[str] = []
            metadata = _chunk_metadata(chunk)

            if self.substrate_smiles or self.product_smiles:
                target_smiles = set(self.substrate_smiles + self.product_smiles)
                smiles_in_text = self._canonicalize_smiles_list(self._extract_smiles_from_text(content))
                overlap = target_smiles & set(smiles_in_text)
                if overlap:
                    risk = RISK_BLOCKED
                    reasons.append(f"Contains target SMILES overlap: {sorted(overlap)}")

            if risk != RISK_BLOCKED and self.substrate_names:
                lowered = content.lower()
                matched_names = [name for name in self.substrate_names if len(name) > 3 and name in lowered]
                if matched_names:
                    risk = _max_risk(risk, RISK_PARTIAL)
                    reasons.append(f"Contains substrate names: {matched_names}")
                    if self._mentions_reaction_type(content) and self._has_yield_data(content):
                        risk = RISK_BLOCKED
                        reasons.append("Substrate name + reaction type + outcome data strongly suggests target results")

            if _is_local_rag_chunk(chunk):
                chunk_family = str(metadata.get("reaction_family", "") or metadata.get("reaction_type", "")).strip().upper()
                if chunk_family and chunk_family == self.reaction_type and bool(metadata.get("has_yield")):
                    if risk == RISK_SAFE:
                        risk = RISK_PARTIAL
                        reasons.append(f"Local precedent/result data for same reaction family '{chunk_family}'")

            if self.strict_mode and risk == RISK_SAFE and self._high_variable_overlap(content):
                risk = RISK_PARTIAL
                reasons.append("High overlap with target variable values plus outcome data")

            sanitized = content
            usable = True
            if risk == RISK_BLOCKED:
                sanitized = self._strip_all_numerical_outcomes(content)
                usable = len(_meaningful_text(sanitized)) >= 40
                if not usable:
                    reasons.append("No useful content after stripping outcomes")
            elif risk == RISK_PARTIAL:
                sanitized = self._desensitize_numerical_values(content)

            return FilteredChunk(
                original=chunk,
                content=sanitized.strip(),
                leakage_risk=risk,
                risk_reasons=reasons,
                is_usable=usable,
            )
        except Exception as exc:
            logger.warning("LeakageFilter failed on chunk '%s': %s", _chunk_source_id(chunk), exc)
            return FilteredChunk(original=chunk, content=_chunk_content(chunk), leakage_risk=RISK_SAFE, is_usable=True)

    def _extract_reaction_type(self, problem_spec: dict[str, Any]) -> str:
        reaction = problem_spec.get("reaction", {}) if isinstance(problem_spec.get("reaction"), dict) else {}
        family = str(reaction.get("family") or problem_spec.get("reaction_type") or "").strip().upper()
        return family

    def _extract_substrate_names(self, problem_spec: dict[str, Any]) -> list[str]:
        reaction = problem_spec.get("reaction", {}) if isinstance(problem_spec.get("reaction"), dict) else {}
        names: list[str] = []
        for item in reaction.get("substrates", []) if isinstance(reaction.get("substrates"), list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip().lower()
            if name and name not in names:
                names.append(name)
        return names

    def _extract_substrate_smiles(self, problem_spec: dict[str, Any]) -> list[str]:
        reaction = problem_spec.get("reaction", {}) if isinstance(problem_spec.get("reaction"), dict) else {}
        smiles: list[str] = []
        for item in reaction.get("substrates", []) if isinstance(reaction.get("substrates"), list) else []:
            if not isinstance(item, dict):
                continue
            value = str(item.get("smiles", "")).strip()
            if value:
                smiles.append(value)
        reaction_smiles = str(reaction.get("reaction_smiles", "")).strip()
        reactants, _, _ = _split_reaction_smiles(reaction_smiles)
        smiles.extend(reactants)
        return _dedupe_preserve(smiles)

    def _extract_product_smiles(self, problem_spec: dict[str, Any]) -> list[str]:
        reaction = problem_spec.get("reaction", {}) if isinstance(problem_spec.get("reaction"), dict) else {}
        smiles: list[str] = []
        product_smiles = reaction.get("product_smiles", "")
        if isinstance(product_smiles, list):
            smiles.extend(str(item).strip() for item in product_smiles if str(item).strip())
        else:
            value = str(product_smiles or "").strip()
            if value:
                smiles.append(value)
        reaction_smiles = str(reaction.get("reaction_smiles", "")).strip()
        _, _, products = _split_reaction_smiles(reaction_smiles)
        smiles.extend(products)
        return _dedupe_preserve(smiles)

    def _extract_known_variable_values(self, problem_spec: dict[str, Any]) -> set[str]:
        values: set[str] = set()
        reaction = problem_spec.get("reaction", {}) if isinstance(problem_spec.get("reaction"), dict) else {}
        for item in reaction.get("known_fixed_context", []) if isinstance(reaction.get("known_fixed_context"), list) else []:
            if not isinstance(item, dict):
                continue
            value = str(item.get("value", "")).strip().lower()
            if value:
                values.add(value)
        for variable in problem_spec.get("variables", []) if isinstance(problem_spec.get("variables"), list) else []:
            if not isinstance(variable, dict):
                continue
            for domain_item in variable.get("domain", []) if isinstance(variable.get("domain"), list) else []:
                if isinstance(domain_item, dict):
                    raw = domain_item.get("label") or domain_item.get("name") or domain_item.get("value") or ""
                else:
                    raw = domain_item
                value = str(raw or "").strip().lower()
                if value:
                    values.add(value)
            for key, value in (variable.get("smiles_map", {}) or {}).items():
                key_text = str(key or "").strip().lower()
                value_text = str(value or "").strip().lower()
                if key_text:
                    values.add(key_text)
                if value_text:
                    values.add(value_text)
        return values

    def _extract_smiles_from_text(self, text: str) -> list[str]:
        pattern = re.compile(
            r"(?<![a-zA-Z])([A-Z][A-Za-z0-9@\+\-\[\]\(\)=#/\\\.]{4,})(?![a-zA-Z])"
        )
        matches = pattern.findall(text)
        reactants, agents, products = _split_reaction_smiles(text)
        matches.extend(reactants)
        matches.extend(agents)
        matches.extend(products)
        return _dedupe_preserve(matches)

    def _canonicalize_smiles_list(self, smiles_list: list[str]) -> list[str]:
        global _RDKIT_WARNING_EMITTED
        canonical: list[str] = []
        try:
            from rdkit import Chem

            for smiles in smiles_list:
                value = str(smiles or "").strip()
                if not value:
                    continue
                molecule = Chem.MolFromSmiles(value)
                if molecule is not None:
                    canonical.append(Chem.MolToSmiles(molecule))
        except ImportError:
            if not _RDKIT_WARNING_EMITTED:
                logger.warning("RDKit is unavailable; leakage filtering will fall back to string-based SMILES matching.")
                _RDKIT_WARNING_EMITTED = True
            canonical = [str(smiles).strip() for smiles in smiles_list if str(smiles).strip()]
        return _dedupe_preserve(canonical)

    def _mentions_reaction_type(self, text: str) -> bool:
        lowered = text.lower()
        if not self.reaction_type:
            return False
        keyword_map = {
            "DAR": ["direct arylation", "c-h arylation", "c-h activation"],
            "BH": ["buchwald-hartwig", "buchwald hartwig", "amination", "c-n coupling"],
            "SUZUKI": ["suzuki", "suzuki-miyaura", "boronic acid coupling"],
            "OCM": ["oxidative coupling of methane", "ocm"],
            "SCR": ["selective catalytic reduction", "scr", "nh3-scr", "ammonia scr"],
        }
        keywords = keyword_map.get(self.reaction_type, [self.reaction_type.lower()])
        return any(keyword in lowered for keyword in keywords)

    def _has_yield_data(self, text: str) -> bool:
        return bool(YIELD_PATTERN.search(text) or LOOSE_YIELD_PATTERN.search(text) or CONDITION_OUTCOME_PATTERN.search(text))

    def _high_variable_overlap(self, text: str) -> bool:
        if not self.known_variable_values or not self._has_yield_data(text):
            return False
        lowered = text.lower()
        matches = sum(1 for value in self.known_variable_values if len(value) > 1 and value in lowered)
        threshold = min(3, len(self.known_variable_values))
        return matches >= max(threshold, 2)

    def _strip_all_numerical_outcomes(self, text: str) -> str:
        result = YIELD_PATTERN.sub("[yield data removed]", text)
        result = LOOSE_YIELD_PATTERN.sub("[outcome data removed]", result)
        result = CONDITION_OUTCOME_PATTERN.sub("[condition-outcome data removed]", result)
        result = re.sub(
            r"(\d+(?:\.\d+)?)\s*%(?!\s*(?:mol|wt|v/v|w/w|loading|catalyst|equiv))",
            "[value]%",
            result,
            flags=re.IGNORECASE,
        )
        result = re.sub(r"(?:\[[^\]]+removed\]\s*){2,}", "[data removed] ", result)
        return _collapse_spaces(result)

    def _desensitize_numerical_values(self, text: str) -> str:
        def replace_match(match: re.Match[str]) -> str:
            full = match.group(0)
            value_match = re.search(r"(\d+(?:\.\d+)?)", full)
            if not value_match:
                return full
            descriptor = _qualitative_yield(float(value_match.group(1)))
            if any(keyword in full.lower() for keyword in OUTCOME_KEYWORDS):
                return re.sub(r"\d+(?:\.\d+)?\s*%\s*(?:yield|conversion|selectivity|ee|er|dr)?", descriptor, full, flags=re.IGNORECASE)
            return descriptor

        result = YIELD_PATTERN.sub(replace_match, text)
        result = LOOSE_YIELD_PATTERN.sub(lambda match: re.sub(r"\d+(?:\.\d+)?\s*%", _qualitative_yield(75.0), match.group(0)), result)
        result = CONDITION_OUTCOME_PATTERN.sub("[condition with qualitative outcome]", result)
        return _collapse_spaces(result)


def sanitize_evidence_bundle(
    bundle: EvidenceBundle,
    problem_spec: dict[str, Any],
    llm_adapter: "RAGLLMAdapter | None" = None,
    strict_mode: bool = True,
) -> EvidenceBundle:
    """Sanitize an EvidenceBundle before downstream card synthesis."""

    leakage_filter = LeakageFilter(problem_spec, strict_mode=strict_mode)

    precedent_chunks, precedent_summary = _sanitize_local_chunks(bundle.precedent_result.chunks, leakage_filter)
    mechanism_chunks, mechanism_summary = _sanitize_local_chunks(bundle.mechanism_result.chunks, leakage_filter)
    property_chunks, property_summary = _sanitize_local_chunks(bundle.property_result.chunks, leakage_filter)

    summary = {
        "total": precedent_summary["total"] + mechanism_summary["total"] + property_summary["total"],
        "safe": precedent_summary["safe"] + mechanism_summary["safe"] + property_summary["safe"],
        "partial": precedent_summary["partial"] + mechanism_summary["partial"] + property_summary["partial"],
        "blocked": precedent_summary["blocked"] + mechanism_summary["blocked"] + property_summary["blocked"],
        "discarded": precedent_summary["discarded"] + mechanism_summary["discarded"] + property_summary["discarded"],
        "by_stream": {
            "precedent": precedent_summary,
            "mechanism": mechanism_summary,
            "property": property_summary,
        },
    }

    notes = list(bundle.notes)
    notes.append(f"{LEAKAGE_SUMMARY_PREFIX}{json.dumps(summary, ensure_ascii=False, sort_keys=True)}")
    if summary["discarded"]:
        notes.append(f"Leakage filter discarded {summary['discarded']} chunk(s) before knowledge-card synthesis.")

    new_precedent_result = replace(bundle.precedent_result, chunks=precedent_chunks)
    new_mechanism_result = replace(bundle.mechanism_result, chunks=mechanism_chunks)
    new_property_result = replace(bundle.property_result, chunks=property_chunks)

    recomputed_mechanism_summary = _compress_mechanism_summary(bundle.plan.mechanism.to_text(), mechanism_chunks, llm_adapter)
    recomputed_role_evidence = _build_role_evidence(precedent_chunks, bundle.plan.precedent.variable_roles)

    return EvidenceBundle(
        plan=bundle.plan,
        role_evidence=recomputed_role_evidence,
        mechanism_chunks=mechanism_chunks,
        mechanism_summary=recomputed_mechanism_summary,
        property_chunks=property_chunks,
        precedent_result=new_precedent_result,
        mechanism_result=new_mechanism_result,
        property_result=new_property_result,
        notes=notes,
    )


def extract_leakage_summary(notes: list[str]) -> dict[str, Any]:
    for note in notes:
        if not str(note).startswith(LEAKAGE_SUMMARY_PREFIX):
            continue
        payload = str(note)[len(LEAKAGE_SUMMARY_PREFIX) :].strip()
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _sanitize_local_chunks(
    chunks: list[LocalRetrievedChunk],
    leakage_filter: LeakageFilter,
) -> tuple[list[LocalRetrievedChunk], dict[str, int]]:
    filtered_chunks: list[FilteredChunk] = []
    sanitized_chunks: list[LocalRetrievedChunk] = []
    for chunk in chunks:
        filtered_raw = leakage_filter.filter_single(chunk)
        filtered_compressed = None
        if chunk.compressed_content:
            filtered_compressed = leakage_filter.filter_single(replace(chunk, content=chunk.compressed_content, compressed_content=""))
        combined_risk = filtered_raw.leakage_risk
        combined_reasons = list(filtered_raw.risk_reasons)
        if filtered_compressed is not None:
            combined_risk = _max_risk(combined_risk, filtered_compressed.leakage_risk)
            combined_reasons = _dedupe_preserve(combined_reasons + filtered_compressed.risk_reasons)
        usable = filtered_raw.is_usable or (filtered_compressed.is_usable if filtered_compressed is not None else False)
        if not usable:
            filtered_chunks.append(
                FilteredChunk(
                    original=chunk,
                    content="",
                    leakage_risk=combined_risk,
                    risk_reasons=combined_reasons or ["Discarded after leakage filtering"],
                    is_usable=False,
                )
            )
            continue
        sanitized_content = filtered_raw.content if filtered_raw.is_usable else (filtered_compressed.content if filtered_compressed else "")
        sanitized_compressed = ""
        if filtered_compressed is not None and filtered_compressed.is_usable:
            sanitized_compressed = filtered_compressed.content
        elif chunk.compressed_content and sanitized_content:
            sanitized_compressed = sanitized_content if len(sanitized_content) <= 600 else ""
        sanitized_chunk = replace(
            chunk,
            content=sanitized_content,
            compressed_content=sanitized_compressed,
        )
        sanitized_chunks.append(sanitized_chunk)
        filtered_chunks.append(
            FilteredChunk(
                original=chunk,
                content=sanitized_content,
                leakage_risk=combined_risk,
                risk_reasons=combined_reasons,
                is_usable=True,
            )
        )
    return sanitized_chunks, _summarize_filtered_chunks(filtered_chunks)


def _build_role_evidence(
    precedent_chunks: list[LocalRetrievedChunk],
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
        for chunk in precedent_chunks:
            try:
                values = json.loads(chunk.metadata.get(field) or "[]")
            except (TypeError, json.JSONDecodeError):
                values = []
            normalized = [_normalize_reagent_name(value) for value in values if _normalize_reagent_name(value)]
            if not normalized:
                continue
            supporting.append(chunk)
            for value in normalized:
                counts[value] = counts.get(value, 0) + 1
        if not counts:
            continue
        top_values = [item for item, _ in sorted(counts.items(), key=lambda entry: (-entry[1], entry[0]))[:8]]
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


def _compress_mechanism_summary(
    mechanism_query_text: str,
    mechanism_chunks: list[LocalRetrievedChunk],
    llm_adapter: "RAGLLMAdapter | None",
) -> str:
    if not mechanism_chunks:
        return ""
    best = mechanism_chunks[0]
    content = best.compressed_content or best.content
    adapter_available = bool(llm_adapter and getattr(llm_adapter, "available", False))
    if adapter_available:
        try:
            response = llm_adapter.compress_chunk(
                {"description": mechanism_query_text, "focus_aspects": []},
                {"content": content[:2000]},
                {"compressed_snippet": "", "kept_points": []},
            )
            snippet = str(response.payload.get("compressed_snippet", "")).strip()
            if snippet:
                return snippet
        except Exception as exc:
            logger.warning("Mechanism recompression failed during leakage sanitization: %s", exc)
    return _offline_compress(mechanism_query_text, content, max_sentences=2)


def _chunk_content(chunk: Any) -> str:
    return str(getattr(chunk, "content", "") or "")


def _chunk_metadata(chunk: Any) -> dict[str, Any]:
    metadata = getattr(chunk, "metadata", {})
    return metadata if isinstance(metadata, dict) else {}


def _chunk_source_type(chunk: Any) -> str:
    source_type = getattr(chunk, "source_type", "")
    if source_type:
        return str(source_type)
    if _is_local_rag_chunk(chunk):
        return "local_rag"
    metadata_source_type = _chunk_metadata(chunk).get("source_type")
    if metadata_source_type:
        return str(metadata_source_type)
    return "unknown"


def _chunk_source_id(chunk: Any) -> str:
    source_id = getattr(chunk, "source_id", "")
    if source_id:
        return str(source_id)
    chunk_id = getattr(chunk, "chunk_id", "")
    if chunk_id:
        return str(chunk_id)
    metadata = _chunk_metadata(chunk)
    return str(metadata.get("document_id") or metadata.get("source_file") or "unknown")


def _chunk_query(chunk: Any) -> str:
    return str(getattr(chunk, "query", "") or "")


def _chunk_short_source(chunk: Any) -> str:
    short_source = getattr(chunk, "short_source", "")
    if isinstance(short_source, str) and short_source:
        return short_source
    if hasattr(chunk, "source_locator"):
        try:
            locator = chunk.source_locator()
            if locator:
                return str(locator)
        except Exception:
            pass
    return _chunk_source_id(chunk)


def _is_local_rag_chunk(chunk: Any) -> bool:
    return isinstance(chunk, LocalRetrievedChunk) or hasattr(chunk, "collection")


def _split_reaction_smiles(text: str) -> tuple[list[str], list[str], list[str]]:
    value = str(text or "").strip()
    if not value or ">" not in value:
        return [], [], []
    parts = value.split(">")
    if len(parts) == 3:
        reactants, agents, products = parts
    elif len(parts) == 2:
        reactants, products = parts
        agents = ""
    else:
        reactants, agents, products = parts[0], parts[1], parts[-1]
    return _split_smiles_segment(reactants), _split_smiles_segment(agents), _split_smiles_segment(products)


def _split_smiles_segment(segment: str) -> list[str]:
    return [item.strip() for item in str(segment or "").split(".") if item.strip()]


def _qualitative_yield(value: float) -> str:
    for (low, high), descriptor in YIELD_TO_QUALITATIVE.items():
        if low <= value < high:
            return descriptor
    return "moderate yield"


def _summarize_filtered_chunks(chunks: list[FilteredChunk]) -> dict[str, int]:
    summary = {"total": len(chunks), "safe": 0, "partial": 0, "blocked": 0, "discarded": 0}
    for chunk in chunks:
        summary[chunk.leakage_risk] = summary.get(chunk.leakage_risk, 0) + 1
        if not chunk.is_usable:
            summary["discarded"] += 1
    return summary


def _max_risk(left: str, right: str) -> str:
    return max((left, right), key=_risk_order)


def _risk_order(risk: str) -> int:
    return {RISK_SAFE: 0, RISK_PARTIAL: 1, RISK_BLOCKED: 2}.get(str(risk), 0)


def _collapse_spaces(text: str) -> str:
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", str(text or ""))).strip()


def _meaningful_text(text: str) -> str:
    stripped = re.sub(r"\[[^\]]+\]", " ", str(text or ""))
    stripped = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", " ", stripped)
    return stripped.strip()


def _dedupe_preserve(values: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    output: list[Any] = []
    for value in values:
        key = value
        if key in seen:
            continue
        output.append(value)
        seen.add(key)
    return output


__all__ = [
    "ConnectorRetrievedChunk",
    "FilteredChunk",
    "LEAKAGE_SUMMARY_PREFIX",
    "LeakageFilter",
    "RISK_BLOCKED",
    "RISK_PARTIAL",
    "RISK_SAFE",
    "extract_leakage_summary",
    "sanitize_evidence_bundle",
]
