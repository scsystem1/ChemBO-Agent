"""
Typed knowledge-state helpers for the refactored ChemBO knowledge system.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


SOURCE_STATUS_VALUES = {
    "ok",
    "available_no_result",
    "unavailable",
    "network_error",
    "timeout",
    "auth_error",
    "filtered_out",
    "internal_error",
}

FACET_MECHANISTIC = "mechanistic_hypothesis"
FACET_PRECEDENT = "precedent"
FACET_COMPOSITION = "composition_or_reagent_effect"
FACET_WINDOW = "operating_window"
FACET_SELECTIVITY = "selectivity_driver"
FACET_FAILURE = "failure_mode_or_side_reaction"
FACET_SCOPE = "scope_or_transferability"
FACET_CONSTRAINT = "constraint_or_safety"

ALL_KNOWLEDGE_FACETS = {
    FACET_MECHANISTIC,
    FACET_PRECEDENT,
    FACET_COMPOSITION,
    FACET_WINDOW,
    FACET_SELECTIVITY,
    FACET_FAILURE,
    FACET_SCOPE,
    FACET_CONSTRAINT,
}

PRIOR_VALUE_PREFERENCE = "value_preference"
PRIOR_VALUE_AVOIDANCE = "value_avoidance"
PRIOR_WINDOW = "operating_window"
PRIOR_INTERACTION = "interaction_hint"
PRIOR_CONSTRAINT = "hard_constraint"
PRIOR_FAILURE = "failure_mode"

ALL_PRIOR_TYPES = {
    PRIOR_VALUE_PREFERENCE,
    PRIOR_VALUE_AVOIDANCE,
    PRIOR_WINDOW,
    PRIOR_INTERACTION,
    PRIOR_CONSTRAINT,
    PRIOR_FAILURE,
}

HOMOGENEOUS_CROSS_COUPLING_FAMILIES = {
    "DAR",
    "BH",
    "SUZUKI",
    "NEGISHI",
    "STILLE",
    "MITSUNOBU",
    "DEOXYFLUORINATION",
    "PHOTOREDOX_NI",
    "ULLMANN_TYPE",
}
HETEROGENEOUS_CATALYSIS_FAMILIES = {
    "OCM",
    "SCR",
    "DEHYDROGENATION",
    "REFORMING",
    "AMMONIA_SYNTHESIS",
}

PROFILE_FACETS = {
    "homogeneous_cross_coupling": [
        FACET_MECHANISTIC,
        FACET_PRECEDENT,
        FACET_COMPOSITION,
        FACET_WINDOW,
        FACET_SELECTIVITY,
        FACET_FAILURE,
        FACET_SCOPE,
    ],
    "heterogeneous_catalysis": [
        FACET_MECHANISTIC,
        FACET_PRECEDENT,
        FACET_COMPOSITION,
        FACET_WINDOW,
        FACET_SELECTIVITY,
        FACET_FAILURE,
        FACET_CONSTRAINT,
    ],
    "generic_fallback": [
        FACET_MECHANISTIC,
        FACET_PRECEDENT,
        FACET_COMPOSITION,
        FACET_WINDOW,
        FACET_FAILURE,
    ],
}


@dataclass
class KnowledgeSourceStatus:
    source: str
    query_id: str
    status: str
    error_type: str = ""
    message: str = ""
    result_count: int = 0
    filtered_count: int = 0
    latency_ms: float = 0.0
    facet: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["latency_ms"] = round(float(self.latency_ms), 3)
        return payload


@dataclass
class EvidenceRecord:
    evidence_id: str
    facet: str
    scope: str
    target_family: str
    evidence_family: str
    family_match: bool
    variables: list[str]
    source_type: str
    source_id: str
    citation: str
    support_strength: float
    snippet: str
    query_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["support_strength"] = round(float(self.support_strength), 4)
        return payload


@dataclass
class ServedPrior:
    prior_id: str
    prior_type: str
    targets: list[str]
    payload: dict[str, Any]
    scope: str
    confidence: float
    support_count: int
    evidence_ids: list[str]
    applicable_nodes: list[str]
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["confidence"] = round(float(self.confidence), 4)
        return payload


def infer_knowledge_profile(reaction_family: str) -> str:
    family = str(reaction_family or "").strip().upper()
    if family in HETEROGENEOUS_CATALYSIS_FAMILIES:
        return "heterogeneous_catalysis"
    if family in HOMOGENEOUS_CROSS_COUPLING_FAMILIES:
        return "homogeneous_cross_coupling"
    return "generic_fallback"


def required_facets_for_profile(profile: str) -> list[str]:
    return list(PROFILE_FACETS.get(str(profile or "").strip(), PROFILE_FACETS["generic_fallback"]))


def classify_evidence_scope(
    *,
    target_family: str,
    evidence_family: str,
    source_type: str,
    metadata: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    metadata = dict(metadata or {})
    target = str(target_family or "").strip().upper()
    evidence = str(evidence_family or "").strip().upper()
    if target and evidence and target == evidence:
        return "target", True
    if source_type == "local_rag" and evidence and target and evidence != target:
        return "analogous", False
    covered = [
        str(item).strip().upper()
        for item in metadata.get("covered_families", [])
        if str(item).strip()
    ]
    if target and target in covered:
        return "target", True
    if target and covered:
        return "analogous", False
    return "general", False


def confidence_label(score: float) -> str:
    value = float(score or 0.0)
    if value >= 0.75:
        return "high"
    if value >= 0.45:
        return "medium"
    return "low"


def build_derived_targets(problem_spec: dict[str, Any]) -> list[dict[str, Any]]:
    variables = [item for item in problem_spec.get("variables", []) if isinstance(item, dict)]
    role_to_names: dict[str, list[str]] = {}
    for variable in variables:
        name = str(variable.get("name") or "").strip()
        role = str(variable.get("role") or "").strip().lower()
        if not name or not role:
            continue
        role_to_names.setdefault(role, []).append(name)

    derived: list[dict[str, Any]] = []
    if {"metal_primary", "support"} <= set(role_to_names):
        derived.append(
            {
                "name": "catalyst_tuple",
                "kind": "derived",
                "description": "Joint catalyst-composition signature over metal/support roles.",
                "depends_on": role_to_names.get("metal_primary", [])
                + role_to_names.get("metal_promoter", [])
                + role_to_names.get("metal_selector", [])
                + role_to_names.get("support", []),
            }
        )
    if role_to_names.get("flow_rate"):
        lowered_names = {name.lower(): name for name in sum(role_to_names.values(), [])}
        methane_name = next((value for key, value in lowered_names.items() if "ch4" in key or "methane" in key), "")
        oxygen_name = next((value for key, value in lowered_names.items() if "o2" in key or "oxygen" in key), "")
        if methane_name and oxygen_name:
            derived.append(
                {
                    "name": "CH4_O2_ratio",
                    "kind": "derived",
                    "description": "Methane-to-oxygen feed ratio derived from flow variables.",
                    "depends_on": [methane_name, oxygen_name],
                }
            )
    if role_to_names.get("temperature") and role_to_names.get("contact_time"):
        derived.append(
            {
                "name": "severity",
                "kind": "derived",
                "description": "Derived operating severity over temperature/contact-time conditions.",
                "depends_on": role_to_names.get("temperature", []) + role_to_names.get("contact_time", []),
            }
        )
    return derived


def build_coverage_report(
    *,
    target_family: str,
    profile: str,
    required_facets: list[str],
    evidence_records: list[dict[str, Any]],
    served_priors: list[dict[str, Any]],
    source_health: list[dict[str, Any]],
) -> dict[str, Any]:
    evidence_by_facet: dict[str, list[dict[str, Any]]] = {}
    for record in evidence_records:
        evidence_by_facet.setdefault(str(record.get("facet") or ""), []).append(record)

    prior_counts: dict[str, int] = {}
    for prior in served_priors:
        payload = prior.get("payload", {}) if isinstance(prior.get("payload"), dict) else {}
        for facet in payload.get("supporting_facets", []) or []:
            facet_name = str(facet or "").strip()
            if facet_name:
                prior_counts[facet_name] = prior_counts.get(facet_name, 0) + 1

    facets: dict[str, Any] = {}
    coverage_gaps: list[dict[str, Any]] = []
    unavailable_sources = [
        item for item in source_health if str(item.get("status", "")).strip() in {"unavailable", "auth_error"}
    ]

    for facet in required_facets:
        records = evidence_by_facet.get(facet, [])
        target_count = sum(1 for item in records if str(item.get("scope", "")) == "target")
        analogous_count = sum(1 for item in records if str(item.get("scope", "")) == "analogous")
        general_count = sum(1 for item in records if str(item.get("scope", "")) == "general")
        prior_count = int(prior_counts.get(facet, 0))
        if facet == FACET_PRECEDENT:
            status = "sufficient" if target_count >= 2 or prior_count >= 1 else ("weak" if target_count >= 1 or records else "missing")
        else:
            status = "sufficient" if target_count >= 1 or prior_count >= 1 else ("weak" if analogous_count or general_count else "missing")
        note = ""
        if status != "sufficient":
            if facet == FACET_PRECEDENT and target_count == 0 and (analogous_count or general_count):
                note = "No target-family precedent evidence; falling back to analogous/general evidence only."
            elif unavailable_sources:
                note = "External sources were unavailable; coverage is based on offline evidence only."
            else:
                note = "Insufficient evidence to serve reliable optimization priors."
            coverage_gaps.append({"facet": facet, "status": status, "note": note})
        facets[facet] = {
            "status": status,
            "target_evidence": target_count,
            "analogous_evidence": analogous_count,
            "general_evidence": general_count,
            "served_priors": prior_count,
            "note": note,
        }

    return {
        "target_family": str(target_family or "").strip().upper(),
        "knowledge_profile": str(profile or "").strip(),
        "required_facets": list(required_facets),
        "facets": facets,
        "coverage_gaps": coverage_gaps,
        "sources_unavailable": len(unavailable_sources),
    }


def build_node_digests(
    *,
    evidence_records: list[dict[str, Any]],
    served_priors: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    priors = [dict(item) for item in served_priors if isinstance(item, dict)]
    evidence = [dict(item) for item in evidence_records if isinstance(item, dict)]

    def _prior_digest(prior: dict[str, Any]) -> dict[str, Any]:
        payload = prior.get("payload", {}) if isinstance(prior.get("payload"), dict) else {}
        return {
            "reference_id": prior.get("prior_id"),
            "prior_id": prior.get("prior_id"),
            "card_id": prior.get("prior_id"),
            "category": prior.get("prior_type"),
            "claim": prior.get("summary") or payload.get("summary") or "",
            "confidence": confidence_label(float(prior.get("confidence", 0.0) or 0.0)),
            "confidence_score": float(prior.get("confidence", 0.0) or 0.0),
            "variables_affected": list(prior.get("targets", [])),
            "scope": prior.get("scope", "general"),
            "citation": "",
            "support_count": int(prior.get("support_count", 0) or 0),
            "evidence_ids": list(prior.get("evidence_ids", [])),
        }

    def _evidence_digest(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "reference_id": record.get("evidence_id"),
            "prior_id": "",
            "card_id": record.get("evidence_id"),
            "category": record.get("facet"),
            "claim": record.get("snippet", ""),
            "confidence": confidence_label(float(record.get("support_strength", 0.0) or 0.0)),
            "confidence_score": float(record.get("support_strength", 0.0) or 0.0),
            "variables_affected": list(record.get("variables", [])),
            "scope": record.get("scope", "general"),
            "citation": record.get("citation", ""),
            "support_count": 1,
            "evidence_ids": [record.get("evidence_id")],
        }

    nodes = {
        "hypothesis_generation": [],
        "warm_start": [],
        "select_candidate": [],
        "run_bo_iteration": [],
        "result_interpretation": [],
        "memory": [],
        "default": [],
    }

    prior_digests = [_prior_digest(item) for item in priors]
    evidence_digests = [_evidence_digest(item) for item in evidence]

    for digest in prior_digests:
        prior = next((item for item in priors if item.get("prior_id") == digest.get("prior_id")), {})
        applicable = prior.get("applicable_nodes", []) if isinstance(prior.get("applicable_nodes"), list) else []
        for node in applicable:
            if node in nodes:
                nodes[node].append(digest)
        nodes["default"].append(digest)

    for digest in evidence_digests:
        category = str(digest.get("category") or "")
        if category in {FACET_MECHANISTIC, FACET_SELECTIVITY, FACET_FAILURE, FACET_SCOPE}:
            nodes["hypothesis_generation"].append(digest)
            nodes["result_interpretation"].append(digest)
        if category in {FACET_FAILURE, FACET_CONSTRAINT}:
            nodes["warm_start"].append(digest)
            nodes["select_candidate"].append(digest)
        nodes["memory"].append(digest)
        nodes["default"].append(digest)

    for key, values in nodes.items():
        values.sort(
            key=lambda item: (
                {"high": 0, "medium": 1, "low": 2}.get(str(item.get("confidence") or "low"), 99),
                -1 * float(item.get("confidence_score", 0.0) or 0.0),
                str(item.get("claim") or ""),
            )
        )
        nodes[key] = values[:12]
    return nodes


def normalize_digest_node_name(node_name: str) -> str:
    normalized = str(node_name or "").strip()
    mapping = {
        "generate_hypotheses": "hypothesis_generation",
        "hypothesis_generation": "hypothesis_generation",
        "warm_start": "warm_start",
        "select_candidate": "select_candidate",
        "run_bo_iteration": "run_bo_iteration",
        "interpret_results": "result_interpretation",
        "result_interpretation": "result_interpretation",
        "memory": "memory",
        "default": "default",
    }
    return mapping.get(normalized, "default")


def select_guidance_for_node(
    knowledge_state: dict[str, Any] | None,
    node_name: str,
    *,
    max_items: int = 10,
) -> list[dict[str, Any]]:
    state = dict(knowledge_state or {})
    digests = state.get("knowledge_digests", {}) if isinstance(state.get("knowledge_digests"), dict) else {}
    digest_node = normalize_digest_node_name(node_name)
    candidates = digests.get(digest_node) or digests.get("default") or []
    if not isinstance(candidates, list):
        return []
    return [dict(item) for item in candidates[: max(int(max_items or 0), 0)] if isinstance(item, dict)]


def knowledge_mode_for_node(
    coverage_report: dict[str, Any] | None,
    served_priors: list[dict[str, Any]] | None,
    *,
    node_name: str,
) -> str:
    priors = [dict(item) for item in (served_priors or []) if isinstance(item, dict)]
    if not priors:
        return "knowledge_gap"
    digest_node = normalize_digest_node_name(node_name)
    applicable = [
        prior
        for prior in priors
        if str(prior.get("scope") or "general") == "target"
        and digest_node in {
            normalize_digest_node_name(item)
            for item in (prior.get("applicable_nodes", []) if isinstance(prior.get("applicable_nodes"), list) else [])
        }
    ]
    if not applicable:
        return "knowledge_gap"
    coverage = dict(coverage_report or {})
    facets = coverage.get("facets", {}) if isinstance(coverage.get("facets"), dict) else {}
    precedent = facets.get(FACET_PRECEDENT, {}) if isinstance(facets.get(FACET_PRECEDENT), dict) else {}
    precedent_status = str(precedent.get("status") or "missing").strip().lower()
    if precedent_status == "sufficient":
        return "knowledge_guided"
    return "coverage_first"


def score_candidate_with_priors(
    candidate: dict[str, Any],
    served_priors: list[dict[str, Any]] | None,
    *,
    node_name: str,
) -> dict[str, Any]:
    digest_node = normalize_digest_node_name(node_name)
    priors = [
        dict(item)
        for item in (served_priors or [])
        if isinstance(item, dict)
        and str(item.get("scope") or "general") == "target"
        and digest_node in {
            normalize_digest_node_name(entry)
            for entry in (item.get("applicable_nodes", []) if isinstance(item.get("applicable_nodes"), list) else [])
        }
    ]
    breakdown = {
        "value_preference": 0.0,
        "value_avoidance": 0.0,
        "operating_window": 0.0,
        "interaction_hint": 0.0,
        "hard_constraint": 0.0,
        "failure_mode": 0.0,
    }
    applied_prior_ids: list[str] = []
    risk_prior_ids: list[str] = []
    hard_conflict_ids: list[str] = []

    for prior in priors:
        prior_type = str(prior.get("prior_type") or "").strip()
        prior_id = str(prior.get("prior_id") or "").strip()
        payload = prior.get("payload", {}) if isinstance(prior.get("payload"), dict) else {}
        confidence = float(prior.get("confidence", 0.0) or 0.0)
        contribution = 0.0
        is_risk = False
        is_constraint_conflict = False

        if prior_type == PRIOR_VALUE_PREFERENCE:
            scores = payload.get("value_scores", {}) if isinstance(payload.get("value_scores"), dict) else {}
            preferred_values = payload.get("preferred_values", []) if isinstance(payload.get("preferred_values"), list) else []
            max_score = max((float(value or 0.0) for value in scores.values()), default=0.0)
            for target in prior.get("targets", []) or []:
                candidate_value = candidate.get(target)
                if candidate_value is None:
                    continue
                matched_score = None
                for preferred in preferred_values:
                    if _knowledge_values_match(candidate_value, preferred):
                        matched_score = float(scores.get(str(preferred), max_score or 1.0) or 0.0)
                        break
                if matched_score is None and scores:
                    for value, raw_score in scores.items():
                        if _knowledge_values_match(candidate_value, value):
                            matched_score = float(raw_score or 0.0)
                            break
                if matched_score is None:
                    continue
                scaled = matched_score / max(max_score, 1.0)
                contribution = max(contribution, max(0.2, confidence * max(scaled, 0.35)))
            breakdown[PRIOR_VALUE_PREFERENCE] += contribution

        elif prior_type == PRIOR_VALUE_AVOIDANCE:
            avoided_values = payload.get("avoided_values", []) if isinstance(payload.get("avoided_values"), list) else []
            for target in prior.get("targets", []) or []:
                candidate_value = candidate.get(target)
                if candidate_value is None:
                    continue
                if any(_knowledge_values_match(candidate_value, avoided) for avoided in avoided_values):
                    contribution = -1.0 * max(0.25, confidence)
                    is_risk = True
                    break
            breakdown[PRIOR_VALUE_AVOIDANCE] += contribution

        elif prior_type == PRIOR_WINDOW:
            allowed_values = payload.get("allowed_values", []) if isinstance(payload.get("allowed_values"), list) else []
            window_matches: list[bool] = []
            for target in prior.get("targets", []) or []:
                candidate_value = candidate.get(target)
                if candidate_value is None or not allowed_values:
                    continue
                window_matches.append(any(_knowledge_values_match(candidate_value, allowed) for allowed in allowed_values))
            if window_matches:
                contribution = 0.7 * confidence if all(window_matches) else -0.3 * confidence
            breakdown[PRIOR_WINDOW] += contribution

        elif prior_type == PRIOR_INTERACTION:
            preferred_combination = payload.get("preferred_combination", {}) if isinstance(payload.get("preferred_combination"), dict) else {}
            if preferred_combination and all(
                key in candidate and _knowledge_values_match(candidate.get(key), value)
                for key, value in preferred_combination.items()
            ):
                contribution = 1.1 * confidence
            breakdown[PRIOR_INTERACTION] += contribution

        elif prior_type == PRIOR_CONSTRAINT:
            allowed_by_target = payload.get("allowed_values_by_target", {}) if isinstance(payload.get("allowed_values_by_target"), dict) else {}
            disallowed_by_target = payload.get("disallowed_values_by_target", {}) if isinstance(payload.get("disallowed_values_by_target"), dict) else {}
            for target, disallowed_values in disallowed_by_target.items():
                if target in candidate and any(_knowledge_values_match(candidate.get(target), value) for value in (disallowed_values or [])):
                    contribution = -1.2
                    is_constraint_conflict = True
                    break
            if not is_constraint_conflict:
                for target, allowed_values in allowed_by_target.items():
                    if target in candidate and allowed_values and not any(
                        _knowledge_values_match(candidate.get(target), value) for value in allowed_values
                    ):
                        contribution = -1.2
                        is_constraint_conflict = True
                        break
            breakdown[PRIOR_CONSTRAINT] += contribution

        elif prior_type == PRIOR_FAILURE:
            risky_values = payload.get("risky_values_by_target", {}) if isinstance(payload.get("risky_values_by_target"), dict) else {}
            for target, values in risky_values.items():
                if target in candidate and any(_knowledge_values_match(candidate.get(target), value) for value in (values or [])):
                    contribution = -0.8 * max(confidence, 0.25)
                    is_risk = True
                    break
            breakdown[PRIOR_FAILURE] += contribution

        if contribution:
            applied_prior_ids.append(prior_id)
            if is_risk:
                risk_prior_ids.append(prior_id)
            if is_constraint_conflict:
                hard_conflict_ids.append(prior_id)

    total = round(sum(float(value) for value in breakdown.values()), 4)
    return {
        "applied_prior_ids": _dedupe_ids(applied_prior_ids),
        "risk_prior_ids": _dedupe_ids(risk_prior_ids),
        "hard_conflict_ids": _dedupe_ids(hard_conflict_ids),
        "knowledge_score_breakdown": {
            **{key: round(float(value), 4) for key, value in breakdown.items()},
            "total": total,
        },
    }


def _knowledge_values_match(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    if _coerce_number(left) is not None and _coerce_number(right) is not None:
        return abs(float(_coerce_number(left)) - float(_coerce_number(right))) <= 1e-9
    return str(left).strip().lower() == str(right).strip().lower()


def _coerce_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dedupe_ids(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if not cleaned or cleaned in seen:
            continue
        deduped.append(cleaned)
        seen.add(cleaned)
    return deduped


def empty_knowledge_state(problem_spec: dict[str, Any] | None = None) -> dict[str, Any]:
    family = ""
    if isinstance(problem_spec, dict):
        reaction = problem_spec.get("reaction", {}) if isinstance(problem_spec.get("reaction"), dict) else {}
        family = str(reaction.get("family") or problem_spec.get("reaction_type") or "").strip().upper()
    profile = infer_knowledge_profile(family)
    return {
        "target_family": family,
        "knowledge_profile": profile,
        "derived_targets": build_derived_targets(problem_spec or {}),
        "source_health": [],
        "coverage_report": build_coverage_report(
            target_family=family,
            profile=profile,
            required_facets=required_facets_for_profile(profile),
            evidence_records=[],
            served_priors=[],
            source_health=[],
        ),
        "evidence_records": [],
        "served_priors": [],
        "knowledge_digests": build_node_digests(evidence_records=[], served_priors=[]),
    }


__all__ = [
    "ALL_KNOWLEDGE_FACETS",
    "ALL_PRIOR_TYPES",
    "FACET_COMPOSITION",
    "FACET_CONSTRAINT",
    "FACET_FAILURE",
    "FACET_MECHANISTIC",
    "FACET_PRECEDENT",
    "FACET_SCOPE",
    "FACET_SELECTIVITY",
    "FACET_WINDOW",
    "EvidenceRecord",
    "KnowledgeSourceStatus",
    "PRIOR_CONSTRAINT",
    "PRIOR_FAILURE",
    "PRIOR_INTERACTION",
    "PRIOR_VALUE_AVOIDANCE",
    "PRIOR_VALUE_PREFERENCE",
    "PRIOR_WINDOW",
    "ServedPrior",
    "build_coverage_report",
    "build_derived_targets",
    "build_node_digests",
    "classify_evidence_scope",
    "confidence_label",
    "empty_knowledge_state",
    "infer_knowledge_profile",
    "knowledge_mode_for_node",
    "normalize_digest_node_name",
    "required_facets_for_profile",
    "score_candidate_with_priors",
    "select_guidance_for_node",
]
