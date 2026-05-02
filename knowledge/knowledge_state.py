"""
Lightweight knowledge-state helpers.
"""
from __future__ import annotations

from typing import Any


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


def infer_knowledge_profile(reaction_family: str) -> str:
    family = str(reaction_family or "").strip().upper()
    if family in HETEROGENEOUS_CATALYSIS_FAMILIES:
        return "heterogeneous_catalysis"
    if family in HOMOGENEOUS_CROSS_COUPLING_FAMILIES:
        return "homogeneous_cross_coupling"
    return "generic_fallback"


def empty_knowledge_state(problem_spec: dict[str, Any] | None = None) -> dict[str, Any]:
    family = ""
    if isinstance(problem_spec, dict):
        reaction = problem_spec.get("reaction", {}) if isinstance(problem_spec.get("reaction"), dict) else {}
        family = str(reaction.get("family") or problem_spec.get("reaction_type") or "").strip().upper()
    profile = infer_knowledge_profile(family)
    return {
        "target_family": family,
        "knowledge_profile": profile,
        "coverage_level": "gap",
        "source_health_summary": {},
    }


def knowledge_mode_from_deck(knowledge_deck: dict[str, Any] | None) -> str:
    """Infer node-independent knowledge mode from the active text deck."""
    deck = dict(knowledge_deck or {})
    cards = [dict(item) for item in deck.get("cards", []) if isinstance(item, dict)]
    active_non_constraint = [
        card
        for card in cards
        if str(card.get("status") or "active") in {"active", "validated"}
        and str(card.get("card_type") or "") != "constraint"
    ]
    if not active_non_constraint:
        return "knowledge_gap"
    summary = deck.get("build_summary", {}) if isinstance(deck.get("build_summary"), dict) else {}
    coverage = str(summary.get("coverage_level") or summary.get("coverage") or "").strip().lower()
    if coverage in {"good", "partial"}:
        return "knowledge_guided"
    return "coverage_first"


__all__ = [
    "HETEROGENEOUS_CATALYSIS_FAMILIES",
    "HOMOGENEOUS_CROSS_COUPLING_FAMILIES",
    "empty_knowledge_state",
    "infer_knowledge_profile",
    "knowledge_mode_from_deck",
]
