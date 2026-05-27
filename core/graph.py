"""
ChemBO Agent workflow graph.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Literal

import numpy as np
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from config.settings import Settings
from core.autobo_engine import (
    bootstrap_autobo_state,
    canonical_recorded_surrogate_model_id,
    record_autobo_result,
    resolve_recorded_surrogate_components,
    run_autobo_iteration,
    select_autobo_candidate,
    select_pure_reasoning_candidate,
)
from core.context_builder import ContextBuilder
from core.dataset_oracle import DatasetOracle
from core.problem_loader import has_structured_problem_spec, normalize_problem_spec, resolve_campaign_budget
from core.prompt_utils import compact_json
from core.state import CampaignPhase, ChemBOState, NextAction
from core.warm_start import (
    interpret_warm_start_result,
    plan_warm_start,
    run_warm_start_postmortem,
)
from core.zero_llm_ablation import resolve_zero_llm_fixed_warm_start, zero_llm_ablation_enabled
from knowledge.knowledge_card import create_knowledge_card, should_evict_card, update_card_validation
from knowledge.knowledge_state import empty_knowledge_state
from knowledge.prior_writer import write_initial_priors
from memory.memory_manager import MemoryManager
from pools.component_pools import candidate_to_key
from tools import build_retrieval_tools

logger = logging.getLogger(__name__)

_LIGHTWEIGHT_SYSTEM_MSG = (
    "You are a computation assistant for Bayesian optimization of chemical reactions. "
    "Return strict JSON only. No preamble, no prose, no markdown fences outside the JSON object."
)


def _pure_reasoning_ablation_enabled(settings: Settings) -> bool:
    return bool(getattr(settings, "pure_reasoning_ablation_enabled", False))


def _proposal_strategy_for_settings(settings: Settings) -> str:
    return "pure_reasoning_ablation" if _pure_reasoning_ablation_enabled(settings) else "autobo_adaptive"


def _zero_llm_ablation_enabled(settings: Settings) -> bool:
    return zero_llm_ablation_enabled(settings)


def _route_after_reflect(
    state: ChemBOState,
    settings: Settings,
) -> Literal["select_candidate", "run_bo_iteration", "campaign_summary"]:
    if state.get("warm_start_active") and state.get("warm_start_queue"):
        return "select_candidate"
    action = state.get("next_action", "")
    if action == NextAction.STOP.value:
        return "campaign_summary"
    if _pure_reasoning_ablation_enabled(settings):
        return "select_candidate"
    return "run_bo_iteration"


def _record_selection_outcome(
    *,
    state: ChemBOState,
    settings: Settings,
    selected: dict[str, Any],
    shortlist: list[dict[str, Any]],
    candidate: dict[str, Any],
    result_value: float,
) -> dict[str, Any]:
    if _pure_reasoning_ablation_enabled(settings):
        return {}
    return record_autobo_result(
        state=state,
        settings=settings,
        selected=selected,
        shortlist=shortlist,
        candidate=candidate,
        result_value=result_value,
    )


def _build_observation_metadata(
    *,
    state: ChemBOState,
    notes: str,
    selected: dict[str, Any],
    shortlist_record: dict[str, Any],
    payload_metadata: dict[str, Any],
    last_payload: dict[str, Any],
    best_before_result: float | None,
    response_metadata: dict[str, Any],
) -> dict[str, Any]:
    resolved_components = (
        last_payload.get("resolved_components", {})
        if isinstance(last_payload.get("resolved_components"), dict)
        else {}
    )
    if not resolved_components:
        resolved_components = resolve_recorded_surrogate_components(
            payload_metadata.get("active_model_internal") or payload_metadata.get("active_model"),
            acquisition_function=state.get("effective_config", {}).get("acquisition_function"),
        )
    af_sources = list(selected.get("af_sources", [])) if isinstance(selected.get("af_sources"), list) else []
    af_ranks = dict(selected.get("af_ranks", {})) if isinstance(selected.get("af_ranks"), dict) else {}
    metadata = {
        "notes": notes,
        "predicted_value": shortlist_record.get("predicted_value"),
        "uncertainty": shortlist_record.get("uncertainty"),
        "acquisition_value": shortlist_record.get("acquisition_value"),
        "best_before_result": best_before_result,
        "config_version": state.get("bo_config", {}).get("config_version"),
        "selection_source": selected.get("selection_source"),
        "active_model": payload_metadata.get("active_model")
        or canonical_recorded_surrogate_model_id(payload_metadata.get("active_model_internal")),
        "active_model_internal": payload_metadata.get("active_model_internal"),
        "autobo_rank": shortlist_record.get("autobo_rank"),
        "proposal_strategy": payload_metadata.get("proposal_strategy") or state.get("effective_config", {}).get("proposal_strategy"),
        "resolved_components": resolved_components,
        "af_sources": af_sources,
        "af_ranks": af_ranks,
        "af_source_count": int(selected.get("af_consensus_count", len(af_sources)) or len(af_sources)),
        "af_recommended_by_qlogei": "qlogei" in af_sources,
        "af_recommended_by_qucb": "qucb" in af_sources,
        "af_recommended_by_ts": "ts" in af_sources,
        "af_qlogei_rank": af_ranks.get("qlogei"),
        "af_qucb_rank": af_ranks.get("qucb"),
        "af_ts_rank": af_ranks.get("ts"),
        "llm_raw_selected_id": selected.get("raw_selected_id"),
        "llm_parsed_selected_id": selected.get("parsed_selected_id"),
        "llm_intended_selected_rank": selected.get("intended_selected_rank"),
        "actual_selected_rank": selected.get("actual_selected_rank", selected.get("selected_rank")),
        "selection_fallback_reason": selected.get("selection_fallback_reason"),
        "dataset_fallback_applied": selected.get("dataset_fallback_applied"),
        "evidence_validation_status": selected.get("evidence_validation_status"),
        "evidence_warning": selected.get("evidence_warning"),
        "active_descriptor_schema_id": payload_metadata.get("active_descriptor_schema_id"),
        "active_descriptor_schema": payload_metadata.get("active_descriptor_schema"),
        "selected_descriptors_by_variable": (
            (payload_metadata.get("active_descriptor_schema") or {}).get("selected_descriptors_by_variable")
            if isinstance(payload_metadata.get("active_descriptor_schema"), dict)
            else None
        ),
        "descriptor_schema_switch_info": payload_metadata.get("schema_switch_info"),
        "descriptor_diagnostics": payload_metadata.get("descriptor_diagnostics"),
    }
    metadata.update(response_metadata)
    return metadata


def _shortlist_record_for_selection(
    shortlist: list[dict[str, Any]],
    selected: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    selected_candidate = selected.get("candidate", {}) if isinstance(selected, dict) else {}
    lookup_candidate = selected_candidate if isinstance(selected_candidate, dict) and selected_candidate else candidate
    if isinstance(lookup_candidate, dict) and lookup_candidate:
        selected_key = candidate_to_key(lookup_candidate)
        for item in shortlist:
            item_candidate = item.get("candidate", {}) if isinstance(item, dict) else {}
            if isinstance(item_candidate, dict) and candidate_to_key(item_candidate) == selected_key:
                return item
    selected_index = _coerce_int(selected.get("selected_index"), default=0) if isinstance(selected, dict) else 0
    return shortlist[selected_index] if 0 <= selected_index < len(shortlist) else {}


def _bootstrap_knowledge_state(
    problem_spec: dict[str, Any],
    settings: Settings,
    llm: Any | None = None,
    invoke_json: Any | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not bool(getattr(settings, "knowledge_enabled", False)):
        artifacts: dict[str, Any] = {
            "prior_writer_disabled": True,
            "pending_evidence_questions": [],
            "card_generation_notes": ["Knowledge bootstrap disabled by settings."],
            "status": "disabled",
        }
        knowledge_state = empty_knowledge_state(problem_spec)
        knowledge_state["enabled"] = False
        knowledge_state["status"] = "disabled"
        return knowledge_state, {
            "cards": [],
            "build_summary": {
                "enabled": False,
                "status": "disabled",
                "coverage_level": "gap",
                "notes": artifacts["card_generation_notes"],
            },
        }, artifacts

    if not bool(getattr(settings, "prior_writer_enabled", True)):
        constraint_cards = _problem_constraint_cards(problem_spec)
        active_cards = _rank_and_filter_cards(
            constraint_cards,
            max_cards=int(getattr(settings, "prior_writer_max_cards", 12) or 12),
            min_hypotheses=0,
        )
        knowledge_state = empty_knowledge_state(problem_spec)
        knowledge_state["enabled"] = True
        knowledge_state["status"] = "ready"
        knowledge_state["coverage_level"] = "constraints_only" if active_cards else "gap"
        return knowledge_state, {
            "cards": active_cards,
            "build_summary": {
                "enabled": True,
                "status": "ready",
                "coverage_level": knowledge_state["coverage_level"],
                "cards_active": len(active_cards),
                "cards_from_constraints": len(constraint_cards),
                "cards_from_priors": 0,
                "needs_external_evidence_count": 0,
                "notes": ["prior_writer disabled by settings"],
            },
        }, {"prior_writer_disabled": True, "pending_evidence_questions": []}

    try:
        if llm is None or invoke_json is None:
            raise RuntimeError("prior writer requires llm and invoke_json")
        prior_cards, prior_artifacts = write_initial_priors(
            problem_spec=problem_spec,
            settings=settings,
            llm=llm,
            invoke_json=invoke_json,
        )
        constraint_cards = _problem_constraint_cards(problem_spec)
        active_cards = _rank_and_filter_cards(
            constraint_cards + prior_cards,
            max_cards=int(getattr(settings, "prior_writer_max_cards", 12) or 12),
            min_hypotheses=int(getattr(settings, "prior_writer_min_hypothesis_cards", 4) or 4),
        )
        knowledge_state = empty_knowledge_state(problem_spec)
        knowledge_state["enabled"] = True
        knowledge_state["status"] = "ready"
        knowledge_state["coverage_level"] = _assess_coverage_level(active_cards)
        pending_questions = [
            str(item.get("evidence_question") or "").strip()
            for item in prior_artifacts.get("needs_evidence", [])
            if isinstance(item, dict) and str(item.get("evidence_question") or "").strip()
        ]
        knowledge_deck = {
            "cards": active_cards,
            "build_summary": {
                "enabled": True,
                "status": "ready",
                "coverage_level": knowledge_state["coverage_level"],
                "cards_active": len(active_cards),
                "cards_from_constraints": len(constraint_cards),
                "cards_from_priors": len(prior_cards),
                "needs_external_evidence_count": len(pending_questions),
                "notes": [],
            },
        }
        return knowledge_state, knowledge_deck, {
            "prior_writer_artifacts": prior_artifacts,
            "pending_evidence_questions": pending_questions,
            "status": "ready",
        }
    except Exception as exc:  # pragma: no cover - defensive runtime fallback
        logger.warning("Knowledge bootstrap failed; continuing without prior cards: %s", exc)
        constraint_cards = _problem_constraint_cards(problem_spec)
        artifacts: dict[str, Any] = {
            "prior_writer_artifacts": {},
            "pending_evidence_questions": [],
            "card_generation_notes": [f"Knowledge bootstrap failed: {type(exc).__name__}: {exc}"],
            "status": "failed",
        }
        knowledge_state = empty_knowledge_state(problem_spec)
        knowledge_state["enabled"] = True
        knowledge_state["status"] = "failed"
        return knowledge_state, {
            "cards": constraint_cards,
            "build_summary": {
                "enabled": True,
                "status": "failed",
                "coverage_level": "constraints_only" if constraint_cards else "gap",
                "notes": artifacts["card_generation_notes"],
            },
        }, artifacts


def _problem_constraint_cards(problem_spec: dict[str, Any]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for index, constraint in enumerate(problem_spec.get("constraints", []) or [], start=1):
        text = str(constraint or "").strip()
        if not text:
            continue
        cards.append(
            create_knowledge_card(
                text=text,
                card_type="constraint",
                scope="target",
                confidence=1.0,
                targets=[],
                actionable_for=["warm_start", "select_candidate", "run_bo_iteration"],
                source_type="problem_constraint",
                card_id=f"kc_constraint_{index:02d}",
            )
        )
    if isinstance(problem_spec.get("dataset"), dict):
        cards.append(
            create_knowledge_card(
                text="Only propose candidates that correspond to rows present in the benchmark dataset.",
                card_type="constraint",
                scope="target",
                confidence=1.0,
                targets=[],
                actionable_for=["warm_start", "select_candidate", "run_bo_iteration"],
                source_type="problem_constraint",
                card_id="kc_dataset_constraint",
            )
        )
    return cards


def _rank_and_filter_cards(
    cards: list[dict[str, Any]],
    max_cards: int = 12,
    *,
    min_hypotheses: int = 4,
) -> list[dict[str, Any]]:
    valid: list[dict[str, Any]] = []
    seen_text: set[str] = set()
    for card in cards:
        if not isinstance(card, dict):
            continue
        text = str(card.get("text") or "").strip()
        if len(text) < 10:
            continue
        key = " ".join(text.lower().split())
        if key in seen_text:
            continue
        seen_text.add(key)
        valid.append(dict(card))
    valid.sort(key=_deck_card_sort_key)
    limit = max(0, int(max_cards or 0))
    if limit <= 0:
        return []
    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    hypothesis_cards = [card for card in valid if str(card.get("card_type") or "") == "hypothesis"]
    for card in hypothesis_cards[: min(max(0, int(min_hypotheses or 0)), limit)]:
        key = str(card.get("card_id") or id(card))
        selected.append(card)
        selected_keys.add(key)
    for card in valid:
        if len(selected) >= limit:
            break
        key = str(card.get("card_id") or id(card))
        if key in selected_keys:
            continue
        selected.append(card)
        selected_keys.add(key)
    selected.sort(key=_deck_card_sort_key)
    return selected


def _deck_card_sort_key(card: dict[str, Any]) -> tuple[int, int, float, int, str]:
    return (
        0 if str(card.get("card_type") or "") == "constraint" else 1,
        {"target": 0, "campaign": 1, "analogous": 2, "general": 3}.get(str(card.get("scope") or "general"), 99),
        -float(card.get("confidence", 0.0) or 0.0),
        {
            "mechanism": 0,
            "reagent_property": 1,
            "operating_window": 2,
            "failure_mode": 3,
            "hypothesis": 4,
            "interaction": 5,
            "analogy": 6,
            "constraint": 6,
        }.get(str(card.get("card_type") or ""), 99),
        str(card.get("card_id") or ""),
    )


def _assess_coverage_level(cards: list[dict[str, Any]]) -> str:
    active = [card for card in cards if str(card.get("status") or "active") in {"active", "validated"}]
    card_types = {str(card.get("card_type") or "") for card in active}
    non_constraint = card_types - {"constraint", ""}
    if not non_constraint:
        return "gap"
    if "mechanism" in card_types and ({"reagent_property", "operating_window", "interaction"} & card_types):
        return "good"
    if {"mechanism", "reagent_property", "operating_window"} & card_types:
        return "partial"
    return "weak"


def _reaction_identity_guard(problem_spec: dict[str, Any] | None) -> str:
    problem_spec = dict(problem_spec or {})
    reaction = problem_spec.get("reaction", {}) if isinstance(problem_spec.get("reaction"), dict) else {}
    family = str(reaction.get("family") or problem_spec.get("reaction_type") or "").strip().upper()
    canonical_name = str(reaction.get("canonical_name") or family).strip()
    aliases = [str(item).strip() for item in reaction.get("aliases", []) if str(item).strip()]
    if family == "DAR":
        alias_text = ", ".join(aliases[:4]) if aliases else "direct arylation"
        return (
            f"Reaction identity guard: this campaign is {canonical_name} ({family}; aliases: {alias_text}). "
            "Do not treat DAR as Diels-Alder."
        )
    if canonical_name:
        return f"Reaction identity guard: this campaign is {canonical_name} ({family})."
    return "Reaction identity guard: keep reasoning anchored to the structured reaction specification."


def _update_knowledge_deck_after_interpretation(
    *,
    knowledge_deck: dict[str, Any],
    hypotheses: list[dict[str, Any]],
    latest_observation: dict[str, Any],
    parsed: dict[str, Any],
    maintenance_new_rules: list[dict[str, Any]],
    direction: str,
    problem_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    deck = dict(knowledge_deck or {})
    cards = [dict(item) for item in deck.get("cards", []) if isinstance(item, dict)]
    candidate = latest_observation.get("candidate", {}) if isinstance(latest_observation.get("candidate"), dict) else {}
    current_iteration = int(latest_observation.get("iteration", 0) or 0)

    tension = {}
    episodic = parsed.get("episodic_memory", {}) if isinstance(parsed.get("episodic_memory"), dict) else {}
    if isinstance(episodic.get("knowledge_tension"), dict):
        tension = episodic.get("knowledge_tension", {})
    if not tension and isinstance(parsed.get("knowledge_conflict"), dict):
        tension = parsed.get("knowledge_conflict", {})
    contradicted_ids = {
        str(item).strip()
        for key in ("conflicting_cards", "conflicting_priors")
        for item in (tension.get(key, []) or [])
        if str(item).strip()
    }
    supported_hypothesis_ids = {str(item).strip() for item in parsed.get("supported_hypotheses", []) or [] if str(item).strip()}
    refuted_hypothesis_ids = {str(item).strip() for item in parsed.get("refuted_hypotheses", []) or [] if str(item).strip()}

    updated_cards: list[dict[str, Any]] = []
    for card in cards:
        targets = [str(item).strip() for item in card.get("targets", []) if str(item).strip()]
        used = bool(targets and any(target in candidate for target in targets))
        supported: bool | None = None
        if str(card.get("card_id") or "") in contradicted_ids:
            supported = False
        if str(card.get("card_type") or "") == "hypothesis":
            source_matches = [
                item
                for item in hypotheses or []
                if isinstance(item, dict)
                and str(item.get("source_card_id") or "")
                and str(item.get("source_card_id") or "") == str(card.get("card_id") or "")
            ]
            matched_ids = {str(item.get("id") or "").strip() for item in source_matches if str(item.get("id") or "").strip()}
            matched_texts = {str(item.get("text") or "").strip() for item in source_matches if str(item.get("text") or "").strip()}
            card_refs = {str(card.get("card_id") or "").strip(), str(card.get("text") or "").strip()} | matched_ids | matched_texts
            if card_refs & supported_hypothesis_ids:
                supported = True
            elif card_refs & refuted_hypothesis_ids:
                supported = False
        updated = update_card_validation(
            card,
            used=used,
            supported=supported,
            current_iteration=current_iteration,
        )
        if should_evict_card(updated, current_iteration):
            updated["status"] = "deprecated"
        updated_cards.append(updated)

    promoted = _promote_memory_rules_to_cards(updated_cards, maintenance_new_rules, current_iteration)
    promoted = _promote_new_evidence_cards(
        promoted,
        parsed.get("new_evidence_cards", []),
        current_iteration,
        problem_spec or {},
    )
    deck["cards"] = promoted
    summary = dict(deck.get("build_summary", {}) if isinstance(deck.get("build_summary"), dict) else {})
    summary["active_cards"] = len([card for card in deck["cards"] if str(card.get("status") or "active") in {"active", "validated"}])
    deck["build_summary"] = summary
    return deck


def _promote_new_evidence_cards(
    cards: list[dict[str, Any]],
    raw_cards: Any,
    current_iteration: int,
    problem_spec: dict[str, Any],
    max_cards: int = 12,
) -> list[dict[str, Any]]:
    if not isinstance(raw_cards, list):
        return cards
    variable_names = {
        str(variable.get("name") or "").strip()
        for variable in problem_spec.get("variables", [])
        if isinstance(variable, dict) and str(variable.get("name") or "").strip()
    }
    updated = list(cards)
    existing_text = {str(card.get("text") or "").strip().lower() for card in updated}
    for raw in raw_cards:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or raw.get("claim") or "").strip()
        if not text or text.lower() in existing_text:
            continue
        card_type = str(raw.get("card_type") or "reagent_property").strip()
        targets = [
            str(item).strip()
            for item in raw.get("targets", [])
            if str(item).strip() and (not variable_names or str(item).strip() in variable_names)
        ] if isinstance(raw.get("targets", []), list) else []
        confidence = min(0.5, max(0.0, float(raw.get("confidence", 0.5) or 0.5)))
        source_url = str(raw.get("source_url") or raw.get("url") or "").strip()
        try:
            updated.append(
                create_knowledge_card(
                    text=text,
                    card_type=card_type,
                    scope=str(raw.get("scope") or "target").strip() or "target",
                    confidence=confidence,
                    targets=targets,
                    actionable_for=["hypothesis_generation", "select_candidate", "result_interpretation"],
                    evidence_refs=[source_url] if source_url else [],
                    source_type="web_search",
                    created_at_iter=current_iteration,
                )
            )
            existing_text.add(text.lower())
        except Exception:
            continue
    active = [card for card in updated if str(card.get("status") or "active") in {"active", "validated"}]
    if len(active) <= max_cards:
        return updated
    ranked_active_ids = {card.get("card_id") for card in _rank_and_filter_cards(active, max_cards=max_cards)}
    for card in updated:
        if str(card.get("status") or "active") in {"active", "validated"} and card.get("card_id") not in ranked_active_ids:
            card["status"] = "deprecated"
    return updated


def _promote_memory_rules_to_cards(
    cards: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    current_iteration: int,
    max_cards: int = 12,
) -> list[dict[str, Any]]:
    updated = list(cards)
    existing_text = {str(card.get("text") or "").strip().lower() for card in updated}
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        confidence = float(rule.get("confidence", 0.0) or 0.0)
        statement = str(rule.get("statement") or rule.get("natural_language") or "").strip()
        evidence_count = int(rule.get("evidence_count", 0) or 0)
        if confidence < 0.8 or evidence_count < 3 or not statement or statement.lower() in existing_text:
            continue
        active = [card for card in updated if str(card.get("status") or "active") in {"active", "validated"}]
        if len(active) >= max_cards:
            evictable = [
                card for card in active
                if str(card.get("card_type") or "") != "constraint"
                and str(card.get("scope") or "") != "target"
            ]
            if not evictable:
                continue
            weakest_id = min(
                evictable,
                key=lambda card: (float(card.get("confidence", 0.0) or 0.0), str(card.get("card_id") or "")),
            ).get("card_id")
            for card in updated:
                if card.get("card_id") == weakest_id:
                    card["status"] = "deprecated"
                    break
        variables = rule.get("variables", [])
        if not isinstance(variables, list):
            conditions = rule.get("conditions", {}) if isinstance(rule.get("conditions"), dict) else {}
            variables = [conditions.get("variable")] if conditions.get("variable") else []
        try:
            updated.append(
                create_knowledge_card(
                    text=statement,
                    card_type="interaction",
                    scope="campaign",
                    confidence=confidence,
                    targets=[str(item) for item in variables if str(item).strip()],
                    actionable_for=["select_candidate", "run_bo_iteration", "result_interpretation"],
                    evidence_refs=[str(rule.get("id") or "")] if rule.get("id") else [],
                    source_type="campaign_observation",
                    created_at_iter=current_iteration,
                )
            )
            existing_text.add(statement.lower())
        except Exception:
            continue
    return updated


def build_chembo_graph(settings: Settings):
    class _DisabledLLM:
        def bind_tools(self, tools):
            del tools
            return self

        def invoke(self, messages):
            raise AssertionError(f"LLM invocation is disabled in zero-LLM ablation mode: {messages}")

    llm_disabled = _zero_llm_ablation_enabled(settings)
    llm_plain = _DisabledLLM() if llm_disabled else _create_llm(settings, enable_thinking_override=False)
    llm_thinking = _DisabledLLM() if llm_disabled else _create_llm(settings, enable_thinking_override=True)
    llm_prior_writer = (
        _DisabledLLM()
        if llm_disabled
        else _create_llm(
            settings,
            enable_thinking_override=False,
            max_tokens_override=int(getattr(settings, "prior_writer_max_tokens", settings.llm_max_tokens) or settings.llm_max_tokens),
        )
    )
    llm_warm_start = (
        _DisabledLLM()
        if llm_disabled
        else _create_llm(
            settings,
            enable_thinking_override=False,
            max_tokens_override=int(getattr(settings, "warm_start_llm_max_tokens", settings.llm_max_tokens) or settings.llm_max_tokens),
        )
    )
    graph = StateGraph(ChemBOState)
    proposal_strategy = _proposal_strategy_for_settings(settings)

    def _memory_manager_from_state(state: ChemBOState) -> MemoryManager:
        return MemoryManager.from_dict(
            state.get("memory", {}),
            capacity=settings.episodic_memory_capacity,
            node_budgets=getattr(settings, "memory_node_budgets", {}),
            consolidation_every_n=int(getattr(settings, "memory_consolidation_every_n", 5)),
            enable_llm_consolidation=(
                bool(getattr(settings, "memory_llm_consolidation_enabled", True)) and not _zero_llm_ablation_enabled(settings)
            ),
            llm_cooldown_iters=int(getattr(settings, "memory_llm_consolidation_cooldown_iters", 5)),
            memory_cooldown_enabled=bool(getattr(settings, "autobo_memory_cooldown_enabled", True)),
            episode_keep_recent=int(getattr(settings, "memory_episode_keep_recent", 24)),
            episode_keep_salient=int(getattr(settings, "memory_episode_keep_salient", 96)),
        )

    class _MemoryLLMAdapter:
        def __init__(self, llm_model):
            self.llm_model = llm_model

        def invoke_json(self, prompt: str, default: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            prompt_messages = [HumanMessage(content=prompt)]
            response, usage = _invoke_llm_with_tracking(
                self.llm_model,
                prompt_messages,
                input_breakdown=_build_input_breakdown(prompt_tokens=sum(_estimate_message_tokens(message) for message in prompt_messages)),
            )
            parsed = _extract_json_from_response(_message_text(response))
            if parsed is not None:
                return parsed, usage
            repair_messages = [
                HumanMessage(content=prompt),
                HumanMessage(content="Reply with strict JSON only. No prose."),
            ]
            repair_response, repair_usage = _invoke_llm_with_tracking(
                self.llm_model,
                repair_messages,
                input_breakdown=_build_input_breakdown(
                    prompt_tokens=sum(_estimate_message_tokens(message) for message in repair_messages),
                ),
            )
            usage = _accumulate_usage_delta(usage, repair_usage)
            repaired = _extract_json_from_response(_message_text(repair_response))
            return repaired or default, usage

    memory_llm_adapter = (
        _MemoryLLMAdapter(llm_thinking)
        if getattr(settings, "memory_llm_consolidation_enabled", True) and not _zero_llm_ablation_enabled(settings)
        else None
    )

    def parse_input(state: ChemBOState) -> dict[str, Any]:
        existing_spec = state["problem_spec"]
        if has_structured_problem_spec(existing_spec):
            problem_spec = normalize_problem_spec(dict(existing_spec))
            problem_spec.setdefault("raw_description", problem_spec.get("description", ""))
            messages = [AIMessage(content="Loaded structured problem specification from file; skipping LLM parsing.")]
        else:
            if _zero_llm_ablation_enabled(settings):
                raise RuntimeError("Zero-LLM ablation requires a structured problem specification.")
            prompt = f"""Analyze this chemical optimization problem and extract structured information.

PROBLEM DESCRIPTION:
{state["problem_spec"].get("raw_description", "")}

Return strict JSON:
{{
  "reaction_type": "...",
  "target_metric": "yield",
  "optimization_direction": "maximize",
  "variables": [
    {{"name": "ligand", "type": "categorical", "domain": ["A", "B"], "description": "..."}}
  ],
  "constraints": ["..."],
  "budget": 30,
  "additional_context": ""
}}"""
            default = {
                "reaction_type": "",
                "target_metric": "yield",
                "optimization_direction": "maximize",
                "variables": [],
                "constraints": [],
                "budget": settings.max_bo_iterations,
                "additional_context": "",
            }
            problem_spec, messages, llm_usage = _invoke_json_node(
                llm_thinking,
                state,
                prompt,
                default,
                node_name="parse_input",
                recent_message_limits=settings.memory_recent_message_limits,
                inject_campaign_summary=bool(getattr(settings, "inject_campaign_summary_in_context", False)),
            )
            problem_spec["raw_description"] = state["problem_spec"].get("raw_description", "")
            problem_spec = normalize_problem_spec(problem_spec)

        def _bootstrap_invoke_json(model, system_prompt: str, user_prompt: str, default: dict[str, Any]):
            prompt = f"{system_prompt}\n\n{user_prompt}"
            parsed, _, usage = _invoke_json_node(
                model,
                state,
                prompt,
                default,
                node_name="knowledge_prior_writer",
                recent_message_limits=settings.memory_recent_message_limits,
                inject_campaign_summary=bool(getattr(settings, "inject_campaign_summary_in_context", False)),
            )
            return parsed, usage

        knowledge_state, knowledge_deck, _retrieval_artifacts = _bootstrap_knowledge_state(
            problem_spec,
            settings,
            llm_prior_writer,
            _bootstrap_invoke_json,
        )
        bootstrap = bootstrap_autobo_state(
            state=state,
            problem_spec=problem_spec,
            settings=settings,
            proposal_strategy=proposal_strategy,
        )
        all_messages = list(messages) + list(bootstrap.get("messages", []))
        reaction_type = problem_spec.get("reaction_type", "")
        updates = {
            "messages": _state_messages(all_messages),
            "phase": CampaignPhase.PARSING.value,
            "problem_spec": problem_spec,
            "knowledge_state": knowledge_state,
            "knowledge_deck": knowledge_deck,
            "pending_evidence_questions": list(_retrieval_artifacts.get("pending_evidence_questions", []) or []),
            "optimization_direction": str(problem_spec.get("optimization_direction", "maximize")).lower(),
            "bo_config": bootstrap.get("bo_config", {}),
            "config_history": bootstrap.get("config_history", []),
            "effective_config": bootstrap.get("effective_config", {}),
            "autobo_state": bootstrap.get("autobo_state", state.get("autobo_state", {})),
            "campaign_summary": _updated_campaign_summary(state, all_messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [
                f"[parse_input] reaction_type={reaction_type or 'unknown'} "
                f"knowledge_cards={len((knowledge_deck.get('cards', []) if isinstance(knowledge_deck, dict) else []) or [])} "
                f"coverage={knowledge_state.get('coverage_level', 'gap')} "
                f"status={knowledge_state.get('status', 'unknown')}"
            ]
            + list(bootstrap.get("log_lines", [])),
        }
        if not has_structured_problem_spec(existing_spec):
            _attach_llm_usage(updates, state, "parse_input", llm_usage)
        prior_usage = (
            (_retrieval_artifacts.get("prior_writer_artifacts", {}) or {}).get("llm_usage", {})
            if isinstance(_retrieval_artifacts, dict)
            else {}
        )
        if int((prior_usage or {}).get("calls", 0) or 0) > 0:
            updates["llm_token_usage"] = _merge_llm_usage(
                updates.get("llm_token_usage", state.get("llm_token_usage", {})),
                "knowledge_prior_writer",
                prior_usage,
            )
        return updates

    def generate_hypotheses(state: ChemBOState) -> dict[str, Any]:
        memory_manager = _memory_manager_from_state(state)
        if _zero_llm_ablation_enabled(settings):
            placeholder = {
                "id": "H0",
                "text": "Zero-LLM ablation: rely on fixed warm start followed by deterministic AutoBO qLogEI selection.",
                "mechanism": "No LLM hypotheses are generated in this ablation.",
                "testable_prediction": "Warm-start observations followed by qLogEI-only AutoBO will define the trajectory.",
                "confidence": "low",
                "confidence_float": 0.3,
                "status": "active",
                "source_card_id": "",
            }
            memory_manager.update_working(
                "current_focus",
                "Execute the fixed historical warm start, then continue with deterministic qLogEI AutoBO.",
            )
            message = AIMessage(content="Zero-LLM ablation active; skipped hypothesis generation.")
            return {
                "messages": _state_messages([message]),
                "phase": CampaignPhase.HYPOTHESIZING.value,
                "hypotheses": [placeholder],
                "memory": memory_manager.to_dict(),
                "campaign_summary": _updated_campaign_summary(state, [message]),
                "llm_reasoning_log": state.get("llm_reasoning_log", []) + ["[generate_hypotheses] zero_llm_placeholder"],
            }
        deck = state.get("knowledge_deck", {}) if isinstance(state.get("knowledge_deck"), dict) else {}
        cards = deck.get("cards", []) if isinstance(deck.get("cards", []), list) else []
        hypothesis_cards = [
            card
            for card in cards
            if isinstance(card, dict)
            and str(card.get("card_type") or "") == "hypothesis"
            and str(card.get("status") or "active") in {"active", "validated"}
        ]
        hypotheses: list[dict[str, Any]] = []
        for index, card in enumerate(hypothesis_cards, start=1):
            confidence_float = float(card.get("confidence", 0.5) or 0.5)
            if confidence_float >= 0.65:
                confidence_label = "high"
            elif confidence_float >= 0.40:
                confidence_label = "medium"
            else:
                confidence_label = "low"
            hypotheses.append(
                {
                    "id": str(card.get("card_id") or f"H{index}"),
                    "text": str(card.get("text") or "").strip(),
                    "mechanism": "",
                    "testable_prediction": str(card.get("testable_prediction") or "").strip(),
                    "confidence": confidence_label,
                    "confidence_float": confidence_float,
                    "status": "active",
                    "supporting_iterations": [],
                    "refuting_iterations": [],
                    "created_at_iteration": int(card.get("created_at_iter", 0) or 0),
                    "source_card_id": str(card.get("card_id") or ""),
                }
            )
        if not hypotheses:
            hypotheses = [
                {
                    "id": "H0",
                    "text": "Optimize systematically; update working memory as observations accumulate.",
                    "mechanism": "No prior hypothesis cards were generated.",
                    "testable_prediction": "Performance improves as observations increase.",
                    "confidence": "low",
                    "confidence_float": 0.3,
                    "status": "active",
                    "supporting_iterations": [],
                    "refuting_iterations": [],
                    "created_at_iteration": 0,
                    "source_card_id": "",
                }
            ]
        best = max(hypotheses, key=lambda item: float(item.get("confidence_float", 0.0) or 0.0))
        memory_manager.update_working(
            "current_focus", str(best.get("text") or "Use knowledge cards to guide configuration and candidate selection.")
        )
        message = AIMessage(content=f"Loaded {len(hypotheses)} hypothesis card(s) from knowledge deck.")
        updates = {
            "messages": _state_messages([message]),
            "phase": CampaignPhase.HYPOTHESIZING.value,
            "hypotheses": hypotheses,
            "memory": memory_manager.to_dict(),
            "campaign_summary": _updated_campaign_summary(state, [message]),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[generate_hypotheses] hypothesis_cards={len(hypothesis_cards)} source=knowledge_deck"],
        }
        return updates

    def warm_start(state: ChemBOState) -> dict[str, Any]:
        if _zero_llm_ablation_enabled(settings):
            shortlist = resolve_zero_llm_fixed_warm_start(settings)
            message = AIMessage(
                content=(
                    f"Loaded {len(shortlist)} fixed warm-start experiments from historical logs for zero-LLM ablation."
                )
            )
            return {
                "messages": _state_messages([message]),
                "phase": CampaignPhase.WARM_STARTING.value,
                "proposal_shortlist": shortlist,
                "warm_start_queue": shortlist,
                "warm_start_target": len(shortlist),
                "warm_start_active": bool(shortlist),
                "_warm_start_postmortem_done": True,
                "campaign_summary": _updated_campaign_summary(state, [message]),
                "llm_reasoning_log": state.get("llm_reasoning_log", [])
                + [f"[warm_start] zero_llm_fixed_shortlist={len(shortlist)}"],
            }
        return plan_warm_start(
            state,
            settings,
            llm_warm_start,
            invoke_tool_loop=lambda llm_obj, current_state, prompt, tool_map, max_turns=6, node_name="", recent_message_limits=None: _invoke_tool_loop(
                llm_obj,
                current_state,
                prompt,
                tool_map,
                max_turns=max_turns,
                node_name=node_name,
                recent_message_limits=recent_message_limits,
                inject_campaign_summary=bool(getattr(settings, "inject_campaign_summary_in_context", False)),
            ),
            extract_last_json=_extract_last_json,
            state_messages=_state_messages,
            updated_campaign_summary=_updated_campaign_summary,
            attach_llm_usage=_attach_llm_usage,
        )

    def run_bo_iteration(state: ChemBOState) -> dict[str, Any]:
        runtime = run_autobo_iteration(
            state=state,
            settings=settings,
            llm=llm_thinking,
            invoke_json_node=lambda llm_obj, current_state, prompt, default, node_name="", lightweight=False: _invoke_json_node(
                llm_obj,
                current_state,
                prompt,
                default,
                node_name=node_name,
                recent_message_limits=settings.memory_recent_message_limits,
                inject_campaign_summary=bool(getattr(settings, "inject_campaign_summary_in_context", False)),
                lightweight=lightweight,
            ),
        )
        messages = runtime.get("messages", [])
        updates = {
            "messages": _state_messages(messages),
            "phase": CampaignPhase.RUNNING.value,
            "proposal_shortlist": runtime.get("proposal_shortlist", []),
            "last_tool_payload": _compact_tool_payload(runtime.get("payload", {})),
            "effective_config": runtime.get("effective_config", state.get("effective_config", {})),
            "bo_config": runtime.get("bo_config", state.get("bo_config", {})),
            "autobo_state": runtime.get("autobo_state", state.get("autobo_state", {})),
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", []) + list(runtime.get("log_lines", [])),
        }
        _attach_llm_usage(updates, state, "run_bo_iteration", runtime.get("llm_usage", _empty_usage_delta()))
        return updates

    def select_candidate(state: ChemBOState) -> dict[str, Any]:
        warm_start_queue = list(state.get("warm_start_queue", []))
        if state.get("warm_start_active") and warm_start_queue:
            oracle = DatasetOracle.from_problem_spec(state.get("problem_spec", {}))
            selected_index = 0
            selected_record = dict(warm_start_queue[0])
            if oracle is not None:
                resolved_selection = _first_dataset_backed_shortlist_record(warm_start_queue, oracle, preferred_index=0)
                if resolved_selection is not None:
                    selected_index, selected_record = resolved_selection
            candidate = dict(selected_record.get("candidate", {}))
            rationale = {
                "chemical_reasoning": selected_record.get("warm_start_rationale", "Executing the next queued warm-start experiment."),
                "hypothesis_alignment": "Warm-start queue execution",
                "information_value": "Initial design-of-experiments point",
                "concerns": "",
            }
            proposal_selected = {
                "selected_index": 0,
                "override": False,
                "candidate": candidate,
                "rationale": rationale,
                "confidence": 1.0,
                "selection_source": "warm_start_queue",
            }
            effective_queue = warm_start_queue[selected_index:] if selected_index > 0 else warm_start_queue
            current_proposal = {
                "candidates": [candidate],
                "selected_index": 0,
            }
            message_text = f"Selected warm-start candidate 1/{len(effective_queue)} from the pre-ranked queue."
            if selected_index > 0:
                message_text = (
                    f"Skipped {selected_index} invalid warm-start candidate(s) that were not present in the dataset. "
                    + message_text
                )
            message = AIMessage(content=message_text)
            return {
                "messages": _state_messages([message]),
                "phase": CampaignPhase.SELECTING_CANDIDATE.value,
                "proposal_selected": proposal_selected,
                "current_proposal": current_proposal,
                "warm_start_queue": effective_queue,
                "campaign_summary": _updated_campaign_summary(state, [message]),
                "llm_reasoning_log": state.get("llm_reasoning_log", [])
                + [f"[select_candidate] source=warm_start_queue index=0 skipped={selected_index}"],
            }

        selector = select_pure_reasoning_candidate if _pure_reasoning_ablation_enabled(settings) else select_autobo_candidate
        runtime = selector(
            state=state,
            settings=settings,
            llm=llm_thinking,
            invoke_json_node=lambda llm_obj, current_state, prompt, default, node_name="", lightweight=False: _invoke_json_node(
                llm_obj,
                current_state,
                prompt,
                default,
                node_name=node_name,
                recent_message_limits=settings.memory_recent_message_limits,
                inject_campaign_summary=bool(getattr(settings, "inject_campaign_summary_in_context", False)),
                lightweight=lightweight,
            ),
        )
        messages = _selection_state_messages(runtime)
        updates = {
            "messages": _state_messages(messages),
            "phase": CampaignPhase.SELECTING_CANDIDATE.value,
            "proposal_selected": runtime.get("proposal_selected", {}),
            "current_proposal": runtime.get("current_proposal", {}),
            "proposal_shortlist": runtime.get("proposal_shortlist", state.get("proposal_shortlist", [])),
            "effective_config": runtime.get("effective_config", state.get("effective_config", {})),
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", []) + list(runtime.get("log_lines", [])),
        }
        if "payload" in runtime:
            updates["last_tool_payload"] = _compact_tool_payload(runtime.get("payload", {}))
        _attach_llm_usage(updates, state, "select_candidate", runtime.get("llm_usage", _empty_usage_delta()))
        return updates

    def await_human_results(state: ChemBOState) -> dict[str, Any]:
        proposal = state.get("current_proposal", {})
        candidate = (proposal.get("candidates") or [{}])[0]
        iteration = state["iteration"]
        selected = state.get("proposal_selected", {}) or {}
        shortlist = state.get("proposal_shortlist", []) or []
        shortlist_record = _shortlist_record_for_selection(shortlist, selected, candidate)
        last_payload = state.get("last_tool_payload", {}) or {}
        payload_metadata = last_payload.get("metadata", {}) if isinstance(last_payload.get("metadata"), dict) else {}
        best_before_result = _coerce_finite_float(state.get("best_result"))
        human_response = interrupt(
            {
                "type": "experiment_request",
                "iteration": iteration + 1,
                "candidate": candidate,
                "message": f"Run experiment for iteration {iteration + 1}: {compact_json(candidate)}",
            }
        )
        result_value, notes, response_metadata = _parse_human_response(human_response)

        observation = {
            "iteration": iteration + 1,
            "candidate": candidate,
            "result": result_value,
            "metadata": _build_observation_metadata(
                state=state,
                notes=notes,
                selected=selected,
                shortlist_record=shortlist_record,
                payload_metadata=payload_metadata,
                last_payload=last_payload,
                best_before_result=best_before_result,
                response_metadata=response_metadata,
            ),
        }
        observations = state["observations"] + [observation]
        best_result, best_candidate, improved = _update_best(
            state.get("best_result"),
            state.get("best_candidate", {}),
            result_value,
            candidate,
            state.get("optimization_direction", "maximize"),
        )
        performance_log = state.get("performance_log", []) + [
            {
                "iteration": iteration + 1,
                "result": result_value,
                "best_so_far": best_result,
                "improved": improved,
            }
        ]
        autobo_result = _record_selection_outcome(
            state=state,
            settings=settings,
            selected=selected,
            shortlist=shortlist,
            candidate=candidate,
            result_value=result_value,
        )

        remaining_warm_start = list(state.get("warm_start_queue", []))
        if state.get("warm_start_active") and remaining_warm_start:
            remaining_warm_start = remaining_warm_start[1:]
        warm_start_active = bool(remaining_warm_start)
        updates = {
            "messages": _state_messages([HumanMessage(content=f"Experiment result: {result_value}. Notes: {notes}")]),
            "phase": CampaignPhase.AWAITING_HUMAN.value,
            "iteration": iteration + 1,
            "observations": observations,
            "best_result": best_result,
            "best_candidate": best_candidate,
            "performance_log": performance_log,
            "warm_start_queue": remaining_warm_start,
            "warm_start_active": warm_start_active,
            "proposal_shortlist": remaining_warm_start if state.get("warm_start_active") else state.get("proposal_shortlist", []),
            "autobo_state": autobo_result.get("autobo_state", state.get("autobo_state", {})),
        }
        if autobo_result.get("log_lines"):
            updates["llm_reasoning_log"] = state.get("llm_reasoning_log", []) + list(autobo_result.get("log_lines", []))
        return updates

    def _default_interpretation_payload(
        interpretation: str = "Result logged for future reasoning.",
        *,
        working_focus: str = "Continue collecting evidence.",
    ) -> dict[str, Any]:
        return {
            "interpretation": interpretation,
            "supported_hypotheses": [],
            "refuted_hypotheses": [],
            "archived_hypotheses": [],
            "reflection": interpretation,
            "knowledge_conflict": {
                "has_conflict": False,
                "conflicting_priors": [],
                "conflicting_cards": [],
                "reason": "",
            },
            "new_evidence_cards": [],
            "working_focus": working_focus,
        }

    def _interpret_result_no_llm(
        state: ChemBOState,
        memory_manager: MemoryManager,
        *,
        state_messages: Callable[[list[BaseMessage]], list[BaseMessage]],
        updated_campaign_summary: Callable[[dict[str, Any], list[BaseMessage]], str],
        label: str,
    ) -> dict[str, Any]:
        latest = state.get("observations", [])[-1] if state.get("observations") else {}
        selection_source = str((latest.get("metadata") or {}).get("selection_source") or "").strip() or "unknown"
        payload = {
            "interpretation": f"Recorded {selection_source} result: {latest.get('result')}",
            "supported_hypotheses": [],
            "refuted_hypotheses": [],
            "archived_hypotheses": [],
            "reflection": "Observation logged without LLM interpretation.",
            "knowledge_conflict": {
                "has_conflict": False,
                "conflicting_priors": [],
                "conflicting_cards": [],
                "reason": "",
            },
            "working_focus": "Continue executing the deterministic optimization loop.",
        }
        write_result = memory_manager.record_result(state, payload)
        message = AIMessage(content=f"Recorded experiment result without LLM interpretation ({selection_source}).")
        return {
            "messages": state_messages([message]),
            "phase": CampaignPhase.INTERPRETING.value,
            "memory": memory_manager.to_dict(),
            "campaign_summary": updated_campaign_summary(state, [message]),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[{label}] {payload['interpretation'][:120]}"]
            + [f"[memory] trigger={write_result.recommended_trigger} notes={'; '.join(write_result.notes[:2])}"],
        }

    def _result_scale(state: ChemBOState) -> float:
        values = [
            _coerce_finite_float(item.get("result"))
            for item in state.get("observations", [])
        ]
        usable = [value for value in values if value is not None]
        if len(usable) < 3:
            return 1.0
        return max(float(np.std(np.asarray(usable, dtype=float))), 1.0)

    def _is_extreme_result(state: ChemBOState, result: float | None) -> bool:
        if result is None:
            return False
        values = sorted(
            value
            for value in (
                _coerce_finite_float(item.get("result"))
                for item in state.get("observations", [])
            )
            if value is not None
        )
        if len(values) < 10:
            return False
        lower_index = max(0, min(len(values) - 1, int(len(values) * 0.1)))
        upper_index = max(0, min(len(values) - 1, int(len(values) * 0.9)))
        return result <= values[lower_index] or result >= values[upper_index]

    def _changed_variables(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
        names = []
        for variable in set(previous) | set(current):
            if previous.get(variable) != current.get(variable):
                names.append(str(variable))
        return names

    def _should_trigger_deep_interpretation(
        state: ChemBOState,
        latest_observation: dict[str, Any],
    ) -> bool:
        evidence_enabled = bool(getattr(settings, "knowledge_enabled", False)) and bool(getattr(settings, "evidence_search_enabled", True))
        if evidence_enabled and list(state.get("pending_evidence_questions", []) or []):
            return True
        iteration = int(latest_observation.get("iteration", state.get("iteration", 0)) or 0)
        if iteration <= 3:
            return True
        metadata = latest_observation.get("metadata", {}) or {}
        result = _coerce_finite_float(latest_observation.get("result"))
        best_before = _coerce_finite_float(metadata.get("best_before_result"))
        direction = str(state.get("optimization_direction", "maximize")).strip().lower()
        if result is not None and best_before is not None:
            improved = result < best_before if direction == "minimize" else result > best_before
            absolute_delta = abs(result - best_before)
            relative_delta = absolute_delta / max(abs(best_before), 1.0)
            if improved and (relative_delta >= 0.05 or absolute_delta >= 1.0):
                return True
        predicted = _coerce_finite_float(metadata.get("predicted_value"))
        uncertainty = _coerce_finite_float(metadata.get("uncertainty"))
        if result is not None and predicted is not None:
            denom = max(uncertainty or 0.0, _result_scale(state), 1.0)
            if abs(result - predicted) / denom >= float(getattr(settings, "interpret_results_surprise_threshold", 1.5)):
                return True
        if _is_extreme_result(state, result):
            return True
        knowledge_conflict = metadata.get("knowledge_conflict")
        if isinstance(knowledge_conflict, dict) and bool(knowledge_conflict.get("has_conflict")):
            return True
        return False

    def _should_bind_retrieval_tools(
        state: ChemBOState,
        latest_observation: dict[str, Any],
        memory_manager: MemoryManager,
    ) -> tuple[bool, list[str]]:
        del memory_manager
        if not bool(getattr(settings, "knowledge_enabled", False)) or not bool(getattr(settings, "evidence_search_enabled", True)):
            return False, []
        pending = [str(item).strip() for item in state.get("pending_evidence_questions", []) or [] if str(item).strip()]
        if pending:
            return True, pending[:1]
        metadata = latest_observation.get("metadata", {}) or {}
        knowledge_conflict = metadata.get("knowledge_conflict")
        if isinstance(knowledge_conflict, dict) and bool(knowledge_conflict.get("has_conflict")):
            return True, []
        result_value = _coerce_finite_float(latest_observation.get("result"))
        predicted = _coerce_finite_float(metadata.get("predicted_value"))
        if predicted is not None and result_value is not None:
            denom = max(_result_scale(state), 1.0)
            if abs(result_value - predicted) / denom >= float(
                getattr(settings, "interpret_results_surprise_threshold", 1.5)
            ):
                return True, []
        return False, []

    def _build_fast_interpretation_digest(
        state: ChemBOState,
        latest_observation: dict[str, Any],
        memory_manager: MemoryManager,
    ) -> dict[str, Any]:
        metadata = latest_observation.get("metadata", {}) or {}
        rules = [node.compact() for node in memory_manager.semantic_graph.query_rules(limit=3)]
        active_hypotheses = [
            {
                "id": item.get("id"),
                "text": item.get("text"),
                "confidence": item.get("confidence"),
            }
            for item in state.get("hypotheses", [])
            if item.get("status") in {"active", "supported"}
        ][:3]
        memory_packet = memory_manager.build_memory_packet(
            "interpret_results",
            state,
            {"candidate": latest_observation.get("candidate", {})},
        )
        contradiction_alerts = (memory_packet.get("sections", {}) or {}).get("contradiction_alerts", [])
        return {
            "latest_observation_brief": {
                "iteration": latest_observation.get("iteration"),
                "candidate": latest_observation.get("candidate", {}),
                "result": latest_observation.get("result"),
                "predicted_value": metadata.get("predicted_value"),
                "uncertainty": metadata.get("uncertainty"),
                "delta_best": _delta_best(
                    _coerce_finite_float(metadata.get("best_before_result")),
                    _coerce_finite_float(latest_observation.get("result")),
                    state.get("optimization_direction", "maximize"),
                ),
            },
            "top_active_hypotheses": active_hypotheses,
            "top_memory_rules": rules,
            "active_knowledge_cards": (state.get("knowledge_deck", {}) or {}).get("cards", [])[:6]
            if isinstance(state.get("knowledge_deck", {}), dict)
            else [],
            "knowledge_conflict_hint": {
                "recent_contradiction_alerts": len(contradiction_alerts) if isinstance(contradiction_alerts, list) else 0,
            },
        }

    def _finalize_interpretation_updates(
        state: ChemBOState,
        memory_manager: MemoryManager,
        parsed: dict[str, Any],
        messages: list[BaseMessage],
        llm_usage: dict[str, Any],
        latest_observation: dict[str, Any],
        *,
        mode_label: str,
        consumed_evidence_questions: list[str] | None = None,
    ) -> dict[str, Any]:
        state_messages = _interpretation_state_messages(mode_label, parsed)
        write_result = memory_manager.record_result(state, parsed)
        maintenance_state = dict(state)
        maintenance_state["iteration"] = int(latest_observation.get("iteration", state["iteration"]) or 0)
        maintenance_state["convergence_state"] = compute_convergence_state(maintenance_state, settings)
        maintenance_state["_memory_last_llm_iter"] = int(state.get("_memory_last_llm_iter", 0) or 0)
        maintenance_state["_memory_last_maint_iter"] = int(state.get("_memory_last_maint_iter", 0) or 0)
        maintenance_report = memory_manager.run_maintenance(
            maintenance_state,
            trigger=write_result.recommended_trigger,
            llm_adapter=memory_llm_adapter,
        )
        knowledge_deck = _update_knowledge_deck_after_interpretation(
            knowledge_deck=state.get("knowledge_deck", {}),
            hypotheses=state.get("hypotheses", []),
            latest_observation=latest_observation,
            parsed=parsed,
            maintenance_new_rules=list(maintenance_report.new_rules),
            direction=state.get("optimization_direction", "maximize"),
            problem_spec=state.get("problem_spec", {}),
        )
        hypotheses = _update_hypothesis_statuses(
            state.get("hypotheses", []),
            parsed.get("supported_hypotheses", []),
            parsed.get("refuted_hypotheses", []),
            parsed.get("archived_hypotheses", []),
            int(latest_observation.get("iteration", state["iteration"])),
        )
        updates = {
            "messages": _state_messages(state_messages),
            "phase": CampaignPhase.INTERPRETING.value,
            "memory": memory_manager.to_dict(),
            "knowledge_deck": knowledge_deck,
            "pending_evidence_questions": _remaining_pending_evidence_questions(
                state.get("pending_evidence_questions", []),
                consumed_evidence_questions or [],
            ),
            "hypotheses": hypotheses,
            "campaign_summary": _updated_campaign_summary(state, state_messages),
            "_memory_last_llm_iter": int(
                maintenance_report.state_updates.get("_memory_last_llm_iter", state.get("_memory_last_llm_iter", 0)) or 0
            ),
            "_memory_last_maint_iter": int(
                maintenance_report.state_updates.get("_memory_last_maint_iter", state.get("_memory_last_maint_iter", 0)) or 0
            ),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[{mode_label}] {parsed.get('interpretation', '')[:120]}"]
            + [f"[memory] trigger={write_result.recommended_trigger} notes={'; '.join(write_result.notes[:2])}"]
            + [f"[memory] new_rules={len(maintenance_report.new_rules)} updated_rules={len(maintenance_report.updated_rules)}"],
        }
        _attach_llm_usage(updates, state, "interpret_results", llm_usage)
        if int((maintenance_report.llm_usage or {}).get("calls", 0)) > 0:
            updates["llm_token_usage"] = _merge_llm_usage(
                updates.get("llm_token_usage", state.get("llm_token_usage", {})),
                "memory_consolidation",
                maintenance_report.llm_usage,
            )
        return updates

    def _remaining_pending_evidence_questions(pending: Any, consumed: list[str]) -> list[str]:
        consumed_set = {str(item).strip() for item in consumed if str(item).strip()}
        remaining: list[str] = []
        removed = False
        for raw in pending if isinstance(pending, list) else []:
            question = str(raw).strip()
            if not question:
                continue
            if question in consumed_set and not removed:
                removed = True
                continue
            remaining.append(question)
        return remaining

    def interpret_results(state: ChemBOState) -> dict[str, Any]:
        memory_manager = _memory_manager_from_state(state)
        if _zero_llm_ablation_enabled(settings):
            return _interpret_result_no_llm(
                state,
                memory_manager,
                state_messages=_state_messages,
                updated_campaign_summary=_updated_campaign_summary,
                label="interpret_results:zero_llm",
            )
        latest_observation = state["observations"][-1] if state.get("observations") else {}
        latest_selection_source = str((latest_observation.get("metadata") or {}).get("selection_source", ""))
        if latest_selection_source == "warm_start_queue":
            return interpret_warm_start_result(
                state,
                settings,
                llm_thinking,
                memory_manager=memory_manager,
                build_context_messages=_build_context_messages,
                invoke_llm_with_tracking=_invoke_llm_with_tracking,
                extract_json_from_response=_extract_json_from_response,
                message_text=_message_text,
                state_messages=_state_messages,
                updated_campaign_summary=_updated_campaign_summary,
                attach_llm_usage=_attach_llm_usage,
            )
        causal_discipline_block = """
[CAUSAL ATTRIBUTION DISCIPLINE]
When interpreting the latest result, use the available observations, episodes, and memory context to find meaningful comparators:
- Prefer past experiments that are chemically similar and differ in only one or a few variables.
- If no isolated or near-isolated comparison exists, do not attribute the result to a single variable.
- For multi-variable or confounded evidence, describe the combination, interaction, or configuration-level pattern instead.
- Any single-variable claim from confounded evidence must be tentative, low-confidence, and explicitly marked as confounded.
- Do not create permanent exclusions or broad causal rules from sparse or confounded evidence.
""".strip()
        if bool(getattr(settings, "interpret_results_fast_path_enabled", True)) and not _should_trigger_deep_interpretation(
            state,
            latest_observation,
        ):
            digest = _build_fast_interpretation_digest(state, latest_observation, memory_manager)
            prompt = f"""Briefly interpret this single experimental result.

DIGEST:
{compact_json(digest)}

{causal_discipline_block}

If the observation contradicts any Active Knowledge Card, put its card_id in conflicting_cards and explain why.
Only treat a card as supported when the result bears on that card's specific claim or prediction; variable overlap or improvement alone is not support.

Return strict JSON:
{{
  "interpretation": "...",
  "supported_hypotheses": ["H1"],
  "refuted_hypotheses": [],
  "archived_hypotheses": [],
  "reflection": "...",
  "knowledge_conflict": {{
    "has_conflict": false,
    "conflicting_priors": [],
    "conflicting_cards": [],
    "reason": ""
  }},
  "working_focus": "..."
}}"""
            parsed, messages, llm_usage = _invoke_json_node(
                llm_thinking,
                state,
                prompt,
                _default_interpretation_payload(),
                node_name="interpret_results",
                recent_message_limits=settings.memory_recent_message_limits,
                inject_campaign_summary=bool(getattr(settings, "inject_campaign_summary_in_context", False)),
            )
            return _finalize_interpretation_updates(
                state,
                memory_manager,
                parsed,
                messages,
                llm_usage,
                latest_observation,
                mode_label="interpret_results:fast",
            )

        context = ContextBuilder.for_interpret_results(state, memory_manager)
        should_bind_retrieval, suggested_questions = _should_bind_retrieval_tools(
            state,
            latest_observation,
            memory_manager,
        )

        def _evidence_invoke_json(model, system_prompt: str, user_prompt: str, default: dict[str, Any]):
            prompt = f"{system_prompt}\n\n{user_prompt}"
            parsed, _, usage = _invoke_json_node(
                model,
                state,
                prompt,
                default,
                node_name="evidence_search",
                recent_message_limits=settings.memory_recent_message_limits,
                inject_campaign_summary=bool(getattr(settings, "inject_campaign_summary_in_context", False)),
            )
            return parsed, usage

        retrieval_tools = build_retrieval_tools(
            settings,
            state["problem_spec"],
            llm_plain,
            _evidence_invoke_json,
        ) if should_bind_retrieval else []
        retrieval_tool_map = {tool.name: tool for tool in retrieval_tools}
        retrieval_protocol = (
            "Retrieval tools are available. Use search_chemistry_literature when current memory cannot explain the result, "
            "a conflict requires evidence, a prediction surprise needs evidence, or a suggested question is provided."
            if retrieval_tools
            else "Do not call retrieval tools for this interpretation; use current context only."
        )
        suggested_question_block = (
            "\nSUGGESTED_EXTERNAL_EVIDENCE_QUESTIONS:\n" + compact_json(suggested_questions)
            if suggested_questions
            else ""
        )
        prompt = f"""Interpret the latest experimental result and update campaign memory.

CONTEXT:
{compact_json(context)}

{causal_discipline_block}

{retrieval_protocol}
{suggested_question_block}

When knowledge affects your reasoning, cite card IDs. If the observation contradicts any Active Knowledge Card, put its card_id in conflicting_cards and explain why.
Only treat a card as supported when the result bears on that card's specific claim or prediction; variable overlap or improvement alone is not support.
If retrieval evidence supports a new compact claim, include it in new_evidence_cards with text, card_type, targets, source_url, and confidence <= 0.5.

Return strict JSON:
{{
  "interpretation": "...",
  "supported_hypotheses": ["H1"],
  "refuted_hypotheses": [],
  "archived_hypotheses": [],
  "reflection": "...",
  "knowledge_conflict": {{
    "has_conflict": false,
    "conflicting_priors": [],
    "conflicting_cards": [],
    "reason": ""
  }},
  "new_evidence_cards": [],
  "working_focus": "..."
}}"""
        if retrieval_tools:
            llm_with_retrieval = llm_thinking.bind_tools(retrieval_tools)
            messages, _, llm_usage = _invoke_tool_loop(
                llm_with_retrieval,
                state,
                prompt,
                tool_map=retrieval_tool_map,
                node_name="interpret_results",
                recent_message_limits=settings.memory_recent_message_limits,
                inject_campaign_summary=bool(getattr(settings, "inject_campaign_summary_in_context", False)),
            )
            parsed = _extract_last_json(messages) or _default_interpretation_payload("Stored the latest result.")
        else:
            parsed, messages, llm_usage = _invoke_json_node(
                llm_thinking,
                state,
                prompt,
                _default_interpretation_payload("Stored the latest result."),
                node_name="interpret_results",
                recent_message_limits=settings.memory_recent_message_limits,
                inject_campaign_summary=bool(getattr(settings, "inject_campaign_summary_in_context", False)),
            )
        return _finalize_interpretation_updates(
            state,
            memory_manager,
            parsed,
            messages,
            llm_usage,
            latest_observation,
            mode_label="interpret_results:deep",
            consumed_evidence_questions=suggested_questions,
        )

    def reflect_and_decide(state: ChemBOState) -> dict[str, Any]:
        memory_manager = _memory_manager_from_state(state)
        convergence_state = compute_convergence_state(state, settings)
        budget = resolve_campaign_budget(state.get("problem_spec", {}), settings)
        if len(state.get("observations", [])) >= budget:
            message = AIMessage(content=f"Budget exhausted ({budget} experiments). Campaign complete.")
            return {
                "messages": _state_messages([message]),
                "phase": CampaignPhase.SUMMARIZING.value,
                "next_action": NextAction.STOP.value,
                "convergence_state": convergence_state,
                "termination_reason": f"Budget exhausted after {budget} experiments.",
                "campaign_summary": _updated_campaign_summary(state, [message]),
            }

        if _zero_llm_ablation_enabled(settings):
            message = AIMessage(content="Zero-LLM ablation active; skipping reflection and continuing until budget exhaustion.")
            return {
                "messages": _state_messages([message]),
                "phase": CampaignPhase.REFLECTING.value,
                "next_action": NextAction.CONTINUE.value,
                "convergence_state": convergence_state,
                "campaign_summary": _updated_campaign_summary(state, [message]),
                "llm_reasoning_log": state.get("llm_reasoning_log", [])
                + [f"[reflect_and_decide] zero_llm_continue iter={len(state.get('observations', []))}"],
                "memory": memory_manager.to_dict(),
            }

        if state.get("warm_start_active") and state.get("warm_start_queue"):
            message = AIMessage(
                content=(
                    "Warm-start is still in progress; continue executing the queued initial experiments "
                    f"({len(state.get('warm_start_queue', []))} remaining)."
                )
            )
            return {
                "messages": _state_messages([message]),
                "phase": CampaignPhase.REFLECTING.value,
                "next_action": NextAction.CONTINUE.value,
                "convergence_state": convergence_state,
                "campaign_summary": _updated_campaign_summary(state, [message]),
                "llm_reasoning_log": state.get("llm_reasoning_log", [])
                + [f"[reflect_and_decide] warm_start_remaining={len(state.get('warm_start_queue', []))}"],
            }

        warm_start_just_completed = (
            not state.get("warm_start_active")
            and not state.get("warm_start_queue")
            and int(state.get("warm_start_target", 0) or 0) > 0
            and not bool(state.get("_warm_start_postmortem_done", False))
        )
        postmortem_payload: dict[str, Any] | None = None
        if warm_start_just_completed:
            postmortem_payload = run_warm_start_postmortem(
                state,
                settings,
                llm_thinking,
                memory_llm_adapter,
                memory_manager=memory_manager,
                build_context_messages=_build_context_messages,
                invoke_llm_with_tracking=_invoke_llm_with_tracking,
                extract_json_from_response=_extract_json_from_response,
                message_text=_message_text,
                compute_convergence_state=compute_convergence_state,
                update_hypothesis_statuses=_update_hypothesis_statuses,
                merge_llm_usage=_merge_llm_usage,
            )
            reflection_state = dict(state)
            reflection_state["memory"] = postmortem_payload["memory"]
            reflection_state["hypotheses"] = postmortem_payload["hypotheses"]
            reflection_state["_warm_start_postmortem_done"] = True
            reflection_state["_memory_last_llm_iter"] = int(
                (postmortem_payload.get("state_updates") or {}).get("_memory_last_llm_iter", state.get("_memory_last_llm_iter", 0)) or 0
            )
            reflection_state["_memory_last_maint_iter"] = int(
                (postmortem_payload.get("state_updates") or {}).get("_memory_last_maint_iter", state.get("_memory_last_maint_iter", 0)) or 0
            )
        else:
            reflection_state = dict(state)

        reflect_interval = int(getattr(settings, "reflect_interval", 10) or 10)
        current_n = len(state.get("observations", []))
        should_reflect = (
            warm_start_just_completed
            or
            reflect_interval <= 0
            or current_n % reflect_interval == 0
            or current_n >= max(budget - 1, 1)
        )
        if not should_reflect:
            next_reflect_at = ((current_n // reflect_interval) + 1) * reflect_interval if reflect_interval > 0 else current_n + 1
            message = AIMessage(
                content=(
                    f"Skipping LLM reflection at iteration {current_n} "
                    f"(next reflect at ~{next_reflect_at})."
                )
            )
            return {
                "messages": _state_messages([message]),
                "phase": CampaignPhase.REFLECTING.value,
                "next_action": NextAction.CONTINUE.value,
                "convergence_state": convergence_state,
                "campaign_summary": _updated_campaign_summary(state, [message]),
                "llm_reasoning_log": state.get("llm_reasoning_log", [])
                + [f"[reflect_and_decide] throttled at iter={current_n}"],
            }

        reflection_state["convergence_state"] = convergence_state
        reflection_state["_memory_last_llm_iter"] = int(state.get("_memory_last_llm_iter", 0) or 0)
        reflection_state["_memory_last_maint_iter"] = int(state.get("_memory_last_maint_iter", 0) or 0)
        memory_manager = _memory_manager_from_state(reflection_state)
        reflection_report = memory_manager.run_maintenance(
            reflection_state,
            trigger="reflection",
            llm_adapter=memory_llm_adapter,
        )
        context = ContextBuilder.for_reflect_and_decide(reflection_state, memory_manager)
        prompt = f"""Reflect on campaign progress and decide the next action.

CONTEXT:
{compact_json(context)}

The surrogate model is selected adaptively by the AutoBO engine. Do not request
reconfiguration or kernel changes.

Return strict JSON:
{{
  "decision": "continue|stop",
  "reasoning": "...",
  "confidence": 0.0
}}"""
        default = {
            "decision": "continue",
            "reasoning": "Continue collecting data.",
            "confidence": 0.5,
        }
        parsed, messages, llm_usage = _invoke_json_node(
            llm_thinking,
            reflection_state,
            prompt,
            default,
            node_name="reflect_and_decide",
            recent_message_limits=settings.memory_recent_message_limits,
            inject_campaign_summary=bool(getattr(settings, "inject_campaign_summary_in_context", False)),
        )
        decision = str(parsed.get("decision", "continue")).lower()
        next_action = NextAction.STOP.value if decision == "stop" else NextAction.CONTINUE.value
        phase = CampaignPhase.SUMMARIZING.value if decision == "stop" else CampaignPhase.REFLECTING.value
        termination_reason = str(parsed.get("reasoning", "")).strip() if decision == "stop" else ""
        messages = _reflection_state_messages(parsed)
        updates = {
            "messages": _state_messages(messages),
            "phase": phase,
            "next_action": next_action,
            "convergence_state": convergence_state,
            "memory": memory_manager.to_dict(),
            "termination_reason": termination_reason,
            "campaign_summary": _updated_campaign_summary(state, messages),
            "_memory_last_llm_iter": int(
                reflection_report.state_updates.get("_memory_last_llm_iter", state.get("_memory_last_llm_iter", 0)) or 0
            ),
            "_memory_last_maint_iter": int(
                reflection_report.state_updates.get("_memory_last_maint_iter", state.get("_memory_last_maint_iter", 0)) or 0
            ),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[reflect_and_decide] decision={decision} confidence={parsed.get('confidence', 0.0)}"]
            + [f"[memory_reflection] new_rules={len(reflection_report.new_rules)} updated_rules={len(reflection_report.updated_rules)}"],
        }
        if postmortem_payload is not None:
            updates["hypotheses"] = postmortem_payload["hypotheses"]
            updates["_warm_start_postmortem_done"] = True
            updates["llm_reasoning_log"] = updates["llm_reasoning_log"] + [
                f"[warm_start_postmortem] rules={postmortem_payload.get('added_rule_count', 0)} "
                f"summary={postmortem_payload.get('batch_interpretation', '')[:120]}"
            ]
        _attach_llm_usage(updates, state, "reflect_and_decide", llm_usage)
        if postmortem_payload is not None and int((postmortem_payload.get("llm_usage") or {}).get("calls", 0)) > 0:
            updates["llm_token_usage"] = _merge_llm_usage(
                updates.get("llm_token_usage", state.get("llm_token_usage", {})),
                "warm_start_postmortem",
                postmortem_payload["llm_usage"],
            )
        if int((reflection_report.llm_usage or {}).get("calls", 0)) > 0:
            updates["llm_token_usage"] = _merge_llm_usage(
                updates.get("llm_token_usage", state.get("llm_token_usage", {})),
                "memory_consolidation",
                reflection_report.llm_usage,
            )
        return updates

    def campaign_summary(state: ChemBOState) -> dict[str, Any]:
        summary = _build_final_summary(state)
        message = AIMessage(
            content=(
                "Prepared final campaign summary with "
                f"{summary['total_experiments']} experiment(s) and stop reason: {summary['stop_reason']}"
            )
        )
        return {
            "messages": _state_messages([message]),
            "phase": CampaignPhase.COMPLETED.value,
            "final_summary": summary,
            "campaign_summary": _updated_campaign_summary(state, [message]),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[campaign_summary] best_result={summary['best_result']} experiments={summary['total_experiments']}"],
        }

    def route_after_reflect(state: ChemBOState) -> Literal["select_candidate", "run_bo_iteration", "campaign_summary"]:
        return _route_after_reflect(state, settings)

    graph.add_node("parse_input", parse_input)
    graph.add_node("generate_hypotheses", generate_hypotheses)
    graph.add_node("warm_start", warm_start)
    graph.add_node("run_bo_iteration", run_bo_iteration)
    graph.add_node("select_candidate", select_candidate)
    graph.add_node("await_human_results", await_human_results)
    graph.add_node("interpret_results", interpret_results)
    graph.add_node("reflect_and_decide", reflect_and_decide)
    graph.add_node("campaign_summary", campaign_summary)

    graph.add_edge(START, "parse_input")
    graph.add_edge("parse_input", "generate_hypotheses")
    graph.add_edge("generate_hypotheses", "warm_start")
    graph.add_edge("warm_start", "select_candidate")
    graph.add_edge("run_bo_iteration", "select_candidate")
    graph.add_edge("select_candidate", "await_human_results")
    graph.add_edge("await_human_results", "interpret_results")
    graph.add_edge("interpret_results", "reflect_and_decide")
    graph.add_conditional_edges("reflect_and_decide", route_after_reflect)
    graph.add_edge("campaign_summary", END)

    return graph.compile(checkpointer=MemorySaver())


def _create_llm(
    settings: Settings,
    enable_thinking_override: bool | None = None,
    *,
    max_tokens_override: int | None = None,
):
    model_name = settings.llm_model.strip()
    lowered = model_name.lower()
    effective_thinking = settings.llm_enable_thinking if enable_thinking_override is None else enable_thinking_override
    max_tokens = int(max_tokens_override or settings.llm_max_tokens)
    if settings.llm_base_url:
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("OpenAI-compatible endpoints require 'langchain-openai'.") from exc
        api_key_env = _resolve_openai_api_key_env(settings, lowered)
        api_key = os.getenv(api_key_env)
        if not api_key:
            raise RuntimeError(f"{api_key_env} is not set for the configured endpoint.")
        extra_body = _openai_compatible_model_kwargs(settings, lowered, enable_thinking_override).get("extra_body")
        return ChatOpenAI(
            model=model_name,
            base_url=settings.llm_base_url,
            api_key=api_key,
            temperature=_resolve_temperature(settings, lowered, effective_thinking),
            max_tokens=max_tokens,
            extra_body=extra_body,
        )
    if lowered.startswith("claude"):
        from langchain_anthropic import ChatAnthropic

        if not os.getenv("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        return ChatAnthropic(
            model=model_name,
            temperature=settings.llm_temperature,
            max_tokens=max_tokens,
        )
    if lowered.startswith(("gpt", "o1", "o3", "o4")):
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("OpenAI chat models require 'langchain-openai'.") from exc
        api_key_env = settings.llm_api_key_env or "OPENAI_API_KEY"
        api_key = os.getenv(api_key_env)
        if not api_key:
            raise RuntimeError(f"{api_key_env} is not set for the configured OpenAI model.")
        return ChatOpenAI(
            model=model_name,
            api_key=api_key,
            temperature=settings.llm_temperature,
            max_tokens=max_tokens,
        )
    raise ValueError(f"Unsupported LLM model/provider for '{model_name}'.")


def _resolve_openai_api_key_env(settings: Settings, lowered_model_name: str) -> str:
    if settings.llm_api_key_env:
        return settings.llm_api_key_env
    if _is_dashscope_model(settings.llm_base_url, lowered_model_name):
        return "DASHSCOPE_API_KEY"
    return "OPENAI_API_KEY"


def _openai_compatible_model_kwargs(
    settings: Settings,
    lowered_model_name: str,
    enable_thinking_override: bool | None = None,
) -> dict[str, Any]:
    extra_body: dict[str, Any] = {}
    effective_thinking = settings.llm_enable_thinking if enable_thinking_override is None else enable_thinking_override
    if _is_moonshot_kimi_model(settings.llm_base_url, lowered_model_name):
        if effective_thinking is True:
            extra_body["thinking"] = {"type": "enabled"}
        elif effective_thinking is False:
            extra_body["thinking"] = {"type": "disabled"}
        return {"extra_body": extra_body} if extra_body else {}
    if effective_thinking is True:
        extra_body["enable_thinking"] = True
    elif effective_thinking is False:
        # Some OpenAI-compatible providers default certain models into thinking mode
        # unless the flag is explicitly disabled.
        extra_body["enable_thinking"] = False
    elif effective_thinking is None and _is_dashscope_model(settings.llm_base_url, lowered_model_name):
        # DashScope exposes Kimi 2.x thinking via the OpenAI-compatible API.
        extra_body["enable_thinking"] = True
    return {"extra_body": extra_body} if extra_body else {}


def _is_dashscope_model(base_url: str | None, lowered_model_name: str) -> bool:
    return bool(base_url and "dashscope.aliyuncs.com" in base_url.lower() and _is_kimi_k2_model_name(lowered_model_name))


def _is_moonshot_kimi_model(base_url: str | None, lowered_model_name: str) -> bool:
    return bool(base_url and "moonshot.cn" in base_url.lower() and _is_kimi_k2_model_name(lowered_model_name))


def _is_kimi_k2_model_name(lowered_model_name: str) -> bool:
    return lowered_model_name.startswith("kimi-k2.")


def _resolve_temperature(settings: Settings, lowered_model_name: str, effective_thinking: bool | None) -> float:
    if _is_moonshot_kimi_model(settings.llm_base_url, lowered_model_name):
        return 1.0 if effective_thinking is not False else 0.6
    return settings.llm_temperature


def compute_convergence_state(state: ChemBOState, settings: Settings) -> dict[str, Any]:
    perf_log = state.get("performance_log", [])
    patience = int(getattr(settings, "convergence_patience", 5))
    recent_bests = [_coerce_finite_float(entry.get("best_so_far")) for entry in perf_log[-patience:]]
    recent_bests = [value for value in recent_bests if value is not None]
    is_stagnant = len(recent_bests) >= patience and (max(recent_bests) - min(recent_bests)) < 1.0
    improvements: list[float] = []
    for idx in range(1, len(perf_log)):
        previous = _coerce_finite_float(perf_log[idx - 1].get("best_so_far"))
        current = _coerce_finite_float(perf_log[idx].get("best_so_far"))
        if previous is None or current is None:
            continue
        improvements.append(current - previous)
    recent_improvement_rate = sum(improvements[-3:]) / max(1, len(improvements[-3:])) if improvements else float("inf")
    payload = state.get("last_tool_payload", {})
    acquisition_values = [_coerce_finite_float(value) for value in (payload.get("acquisition_values", []) or [])]
    acquisition_values = [value for value in acquisition_values if value is not None]
    budget = resolve_campaign_budget(state.get("problem_spec", {}), settings)
    return {
        "is_stagnant": is_stagnant,
        "stagnation_length": _count_stagnation(perf_log),
        "recent_improvement_rate": recent_improvement_rate,
        "max_af_value": max(acquisition_values) if acquisition_values else None,
        "budget_used_ratio": len(state.get("observations", [])) / max(budget, 1),
        "last_improvement_iteration": _last_improvement_iteration(perf_log),
    }


def _invoke_tool_loop(
    llm,
    state: ChemBOState,
    prompt: str,
    tool_map: dict[str, Any],
    max_turns: int = 6,
    node_name: str = "",
    recent_message_limits: dict[str, int] | None = None,
    inject_campaign_summary: bool = False,
) -> tuple[list[BaseMessage], str, dict[str, Any]]:
    context_messages, summary, context_breakdown = _build_context_messages(
        state,
        node_name=node_name,
        recent_message_limits=recent_message_limits,
        inject_campaign_summary=inject_campaign_summary,
    )
    conversation: list[BaseMessage] = [HumanMessage(content=prompt)]
    usage = _empty_usage_delta()
    for _ in range(max_turns):
        response, step_usage = _invoke_llm_with_tracking(
            llm,
            context_messages + conversation,
            input_breakdown=_build_input_breakdown(
                system_tokens=context_breakdown["system"],
                campaign_summary_tokens=context_breakdown["campaign_summary"],
                recent_messages_tokens=context_breakdown["recent_messages"],
                prompt_tokens=sum(_estimate_message_tokens(message) for message in conversation),
            ),
        )
        usage = _accumulate_usage_delta(usage, step_usage)
        conversation.append(response)
        if not _message_has_tool_calls(response):
            break
        for tool_call in getattr(response, "tool_calls", []):
            tool_name = tool_call.get("name")
            tool = tool_map.get(tool_name)
            if tool is None:
                payload = json.dumps({"status": "error", "reason": f"Unknown tool '{tool_name}'."})
            else:
                try:
                    payload = tool.invoke(tool_call.get("args", {}))
                except Exception as exc:  # pragma: no cover
                    payload = json.dumps({"status": "error", "reason": f"{type(exc).__name__}: {exc}"})
            conversation.append(
                ToolMessage(
                    content=payload if isinstance(payload, str) else json.dumps(payload),
                    name=tool_name,
                    tool_call_id=tool_call.get("id", tool_name or "tool"),
                )
            )
    return conversation, summary, usage


def _invoke_json_node(
    llm,
    state: ChemBOState,
    prompt: str,
    default: dict[str, Any],
    node_name: str = "",
    recent_message_limits: dict[str, int] | None = None,
    inject_campaign_summary: bool = False,
    lightweight: bool = False,
) -> tuple[dict[str, Any], list[BaseMessage], dict[str, Any]]:
    if lightweight:
        light_system = SystemMessage(content=_LIGHTWEIGHT_SYSTEM_MSG)
        context_messages = [light_system]
        context_breakdown = _build_input_breakdown(system_tokens=_estimate_message_tokens(light_system))
    else:
        context_messages, _, context_breakdown = _build_context_messages(
            state,
            node_name=node_name,
            recent_message_limits=recent_message_limits,
            inject_campaign_summary=inject_campaign_summary,
        )
    usage = _empty_usage_delta()
    prompt_messages = [HumanMessage(content=prompt)]
    response, step_usage = _invoke_llm_with_tracking(
        llm,
        context_messages + prompt_messages,
        input_breakdown=_build_input_breakdown(
            system_tokens=context_breakdown["system"],
            campaign_summary_tokens=context_breakdown["campaign_summary"],
            recent_messages_tokens=context_breakdown["recent_messages"],
            prompt_tokens=sum(_estimate_message_tokens(message) for message in prompt_messages),
        ),
    )
    usage = _accumulate_usage_delta(usage, step_usage)
    messages: list[BaseMessage] = prompt_messages + [response]
    parsed = _extract_json_from_response(_message_text(response))
    if parsed is None:
        repair_prompt = "Reply with strict JSON only. No prose."
        repair_messages = messages + [HumanMessage(content=repair_prompt)]
        repair_response, repair_usage = _invoke_llm_with_tracking(
            llm,
            context_messages + repair_messages,
            input_breakdown=_build_input_breakdown(
                system_tokens=context_breakdown["system"],
                campaign_summary_tokens=context_breakdown["campaign_summary"],
                recent_messages_tokens=context_breakdown["recent_messages"],
                prompt_tokens=sum(_estimate_message_tokens(message) for message in repair_messages),
            ),
        )
        usage = _accumulate_usage_delta(usage, repair_usage)
        messages += [HumanMessage(content=repair_prompt), repair_response]
        parsed = _extract_json_from_response(_message_text(repair_response))
    return parsed or default, messages, usage


def _invoke_llm_with_tracking(
    llm,
    messages: list[BaseMessage],
    *,
    input_breakdown: dict[str, int] | None = None,
) -> tuple[BaseMessage, dict[str, Any]]:
    response = llm.invoke(messages)
    return response, _extract_llm_usage(response, messages, input_breakdown=input_breakdown)


def _extract_llm_usage(
    response: BaseMessage,
    prompt_messages: list[BaseMessage],
    *,
    input_breakdown: dict[str, int] | None = None,
) -> dict[str, Any]:
    provider_usage = _extract_provider_usage(response, input_breakdown=input_breakdown)
    if provider_usage is not None:
        return provider_usage
    return _estimate_llm_usage(prompt_messages, response, input_breakdown=input_breakdown)


def _extract_provider_usage(
    response: BaseMessage,
    *,
    input_breakdown: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    for payload in (
        getattr(response, "usage_metadata", None),
        getattr(response, "response_metadata", None),
        getattr(response, "additional_kwargs", None),
    ):
        usage = _parse_usage_payload(payload, input_breakdown=input_breakdown)
        if usage is not None:
            return usage
    return None


def _parse_usage_payload(payload: Any, *, input_breakdown: dict[str, int] | None = None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    input_tokens = _first_int(
        payload,
        "input_tokens",
        "prompt_tokens",
        "inputTokenCount",
        "prompt_token_count",
    )
    output_tokens = _first_int(
        payload,
        "output_tokens",
        "completion_tokens",
        "outputTokenCount",
        "completion_token_count",
    )
    total_tokens = _first_int(payload, "total_tokens", "totalTokenCount", "total_token_count")
    if input_tokens or output_tokens or total_tokens:
        input_tokens = int(input_tokens or 0)
        output_tokens = int(output_tokens or 0)
        total_tokens = int(total_tokens or (input_tokens + output_tokens))
        return {
            "calls": 1,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "estimated_calls": 0,
            "estimated": False,
            "input_breakdown": _coerce_input_breakdown(input_breakdown, input_tokens=input_tokens),
        }
    for key in ("usage_metadata", "token_usage", "usage", "tokens"):
        nested = _parse_usage_payload(payload.get(key), input_breakdown=input_breakdown)
        if nested is not None:
            return nested
    return None


def _estimate_llm_usage(
    prompt_messages: list[BaseMessage],
    response: BaseMessage,
    *,
    input_breakdown: dict[str, int] | None = None,
) -> dict[str, Any]:
    input_tokens = sum(_estimate_message_tokens(message) for message in prompt_messages)
    output_tokens = _estimate_message_tokens(response)
    return {
        "calls": 1,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "estimated_calls": 1,
        "estimated": True,
        "input_breakdown": _coerce_input_breakdown(input_breakdown, input_tokens=input_tokens),
    }


def _estimate_message_tokens(message: BaseMessage) -> int:
    text = _message_text(message)
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4) + 4


def _empty_input_breakdown() -> dict[str, int]:
    return {
        "system": 0,
        "campaign_summary": 0,
        "recent_messages": 0,
        "prompt": 0,
    }


def _build_input_breakdown(
    *,
    system_tokens: int = 0,
    campaign_summary_tokens: int = 0,
    recent_messages_tokens: int = 0,
    prompt_tokens: int = 0,
) -> dict[str, int]:
    return {
        "system": int(system_tokens or 0),
        "campaign_summary": int(campaign_summary_tokens or 0),
        "recent_messages": int(recent_messages_tokens or 0),
        "prompt": int(prompt_tokens or 0),
    }


def _coerce_input_breakdown(payload: Any, *, input_tokens: int | None = None) -> dict[str, int]:
    if not isinstance(payload, dict):
        payload = {}
    breakdown = _build_input_breakdown(
        system_tokens=_coerce_int(payload.get("system"), default=0),
        campaign_summary_tokens=_coerce_int(payload.get("campaign_summary"), default=0),
        recent_messages_tokens=_coerce_int(payload.get("recent_messages"), default=0),
        prompt_tokens=_coerce_int(payload.get("prompt"), default=0),
    )
    if input_tokens is not None and sum(breakdown.values()) <= 0:
        breakdown["prompt"] = int(input_tokens)
    return breakdown


def _merge_input_breakdown(base: Any, addition: Any) -> dict[str, int]:
    merged = _coerce_input_breakdown(base)
    incoming = _coerce_input_breakdown(addition)
    for key in merged:
        merged[key] += incoming.get(key, 0)
    return merged


def _first_int(payload: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def _empty_usage_delta() -> dict[str, Any]:
    return {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_calls": 0,
        "estimated": False,
        "input_breakdown": _empty_input_breakdown(),
    }


def _accumulate_usage_delta(base: dict[str, Any], addition: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base or _empty_usage_delta())
    for key in ("calls", "input_tokens", "output_tokens", "total_tokens", "estimated_calls"):
        merged[key] = int(merged.get(key, 0)) + int(addition.get(key, 0))
    merged["estimated"] = bool(merged.get("estimated_calls", 0))
    merged["input_breakdown"] = _merge_input_breakdown(
        merged.get("input_breakdown"),
        addition.get("input_breakdown"),
    )
    return merged


def _attach_llm_usage(update: dict[str, Any], state: ChemBOState, node_name: str, usage: dict[str, Any]) -> None:
    if not usage or int(usage.get("calls", 0)) <= 0:
        return
    totals = _merge_llm_usage(state.get("llm_token_usage", {}), node_name, usage)
    update["llm_token_usage"] = totals
    update["last_llm_usage"] = {
        "node": node_name,
        "calls": int(usage.get("calls", 0)),
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
        "total_tokens": int(usage.get("total_tokens", 0)),
        "estimated_calls": int(usage.get("estimated_calls", 0)),
        "estimated": bool(usage.get("estimated", False)),
        "input_breakdown": _coerce_input_breakdown(usage.get("input_breakdown")),
    }


def _merge_llm_usage(existing: dict[str, Any], node_name: str, usage: dict[str, Any]) -> dict[str, Any]:
    merged = {
        "calls": int(existing.get("calls", 0)),
        "input_tokens": int(existing.get("input_tokens", 0)),
        "output_tokens": int(existing.get("output_tokens", 0)),
        "total_tokens": int(existing.get("total_tokens", 0)),
        "estimated_calls": int(existing.get("estimated_calls", 0)),
        "input_breakdown": _coerce_input_breakdown(existing.get("input_breakdown")),
        "by_node": {key: dict(value) for key, value in (existing.get("by_node") or {}).items()},
    }
    for key in ("calls", "input_tokens", "output_tokens", "total_tokens", "estimated_calls"):
        merged[key] += int(usage.get(key, 0))
    merged["input_breakdown"] = _merge_input_breakdown(
        merged.get("input_breakdown"),
        usage.get("input_breakdown"),
    )

    node_totals = dict(
        merged["by_node"].get(
            node_name,
            {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "estimated_calls": 0,
                "input_breakdown": _empty_input_breakdown(),
            },
        )
    )
    for key in ("calls", "input_tokens", "output_tokens", "total_tokens", "estimated_calls"):
        node_totals[key] = int(node_totals.get(key, 0)) + int(usage.get(key, 0))
    node_totals["estimated"] = bool(node_totals.get("estimated_calls", 0))
    node_totals["input_breakdown"] = _merge_input_breakdown(
        node_totals.get("input_breakdown"),
        usage.get("input_breakdown"),
    )
    merged["by_node"][node_name] = node_totals
    return merged


def _build_context_messages(
    state: ChemBOState,
    *,
    node_name: str = "",
    recent_message_limits: dict[str, int] | None = None,
    inject_campaign_summary: bool = False,
) -> tuple[list[BaseMessage], str, dict[str, int]]:
    messages = state.get("messages", [])
    if not messages:
        return [], state.get("campaign_summary", ""), _empty_input_breakdown()
    system_message = messages[0]
    limits = recent_message_limits or {}
    limit = int(limits.get(node_name, limits.get("default", 20)) or 20)
    recent = messages[1:][-limit:]
    compressed: list[BaseMessage] = [system_message]
    summary = state.get("campaign_summary", "")
    breakdown = _build_input_breakdown(system_tokens=_estimate_message_tokens(system_message))
    if summary and inject_campaign_summary:
        compressed.append(HumanMessage(content=f"[CAMPAIGN SUMMARY]\n{summary}"))
        breakdown["campaign_summary"] += _estimate_message_tokens(compressed[-1])
    for message in recent:
        compacted = _compact_message_for_state(message, node_name=node_name)
        compressed.append(compacted)
        breakdown["recent_messages"] += _estimate_message_tokens(compacted)
    return compressed, summary, breakdown


def _updated_campaign_summary(state: ChemBOState, new_messages: list[BaseMessage]) -> str:
    return _build_structured_campaign_summary(state, new_messages)


def _parsed_json_state_messages(
    label: str,
    parsed: dict[str, Any],
    *,
    summary: str = "",
) -> list[BaseMessage]:
    payload = {
        "node": label,
        "summary": " ".join(str(summary or "").split())[:500],
        "parsed": parsed if isinstance(parsed, dict) else {},
    }
    return [AIMessage(content=f"[{label} parsed]\n{compact_json(payload)}")]


def _selection_state_messages(runtime: dict[str, Any]) -> list[BaseMessage]:
    selected = runtime.get("proposal_selected", {}) if isinstance(runtime.get("proposal_selected"), dict) else {}
    rationale = selected.get("rationale", {}) if isinstance(selected.get("rationale"), dict) else {}
    parsed = {
        "selection_source": selected.get("selection_source"),
        "selected_rank": selected.get("selected_rank") or selected.get("autobo_shortlist_rank") or selected.get("selected_index"),
        "override": selected.get("override"),
        "candidate": selected.get("candidate", {}),
        "confidence": selected.get("confidence"),
        "reasoning": rationale.get("chemical_reasoning") or rationale.get("reasoning") or "",
        "comparison_to_top1": rationale.get("comparison_to_top1") or "",
        "selection_mode": rationale.get("selection_mode") or "",
        "override_evidence": rationale.get("override_evidence") or {},
    }
    summary = (
        f"selected_rank={parsed.get('selected_rank')} source={parsed.get('selection_source')} "
        f"override={parsed.get('override')}"
    )
    return _parsed_json_state_messages("select_candidate", parsed, summary=summary)


def _interpretation_state_messages(mode_label: str, parsed: dict[str, Any]) -> list[BaseMessage]:
    summary = str(parsed.get("interpretation") or parsed.get("reflection") or "").strip()
    compact = {
        "interpretation": parsed.get("interpretation", ""),
        "supported_hypotheses": parsed.get("supported_hypotheses", []),
        "refuted_hypotheses": parsed.get("refuted_hypotheses", []),
        "archived_hypotheses": parsed.get("archived_hypotheses", []),
        "knowledge_conflict": parsed.get("knowledge_conflict", {}),
        "new_evidence_cards": parsed.get("new_evidence_cards", []),
        "working_focus": parsed.get("working_focus", ""),
    }
    return _parsed_json_state_messages(mode_label, compact, summary=summary)


def _reflection_state_messages(parsed: dict[str, Any]) -> list[BaseMessage]:
    summary = f"decision={parsed.get('decision', 'continue')} confidence={parsed.get('confidence', 0.0)}"
    compact = {
        "decision": parsed.get("decision", "continue"),
        "reasoning": parsed.get("reasoning", ""),
        "confidence": parsed.get("confidence", 0.0),
    }
    return _parsed_json_state_messages("reflect_and_decide", compact, summary=summary)


def _build_structured_campaign_summary(state: ChemBOState, new_messages: list[BaseMessage] | None = None) -> str:
    observations = [
        item for item in state.get("observations", [])
        if isinstance(item, dict) and _coerce_finite_float(item.get("result")) is not None
    ]
    direction = str(state.get("optimization_direction", "maximize")).strip().lower()
    ranked = sorted(
        observations,
        key=lambda item: float(_coerce_finite_float(item.get("result")) or 0.0),
        reverse=direction != "minimize",
    )
    summary = {
        "iteration": int(state.get("iteration", 0) or 0),
        "total_observations": len(observations),
        "best_result": _coerce_finite_float(state.get("best_result")),
        "best_candidate": state.get("best_candidate", {}),
        "top3": [
            {"iteration": item.get("iteration"), "candidate": item.get("candidate", {}), "result": item.get("result")}
            for item in ranked[:3]
        ],
        "bottom3": [
            {"iteration": item.get("iteration"), "candidate": item.get("candidate", {}), "result": item.get("result")}
            for item in ranked[-3:]
        ],
        "stagnation": {
            "is_stagnant": bool((state.get("convergence_state", {}) or {}).get("is_stagnant", False)),
            "stagnation_length": int((state.get("convergence_state", {}) or {}).get("stagnation_length", 0) or 0),
            "last_improvement_iteration": (state.get("convergence_state", {}) or {}).get("last_improvement_iteration"),
        },
        "recent_overrides": _campaign_summary_recent_overrides(state, lookback=5),
        "recent_messages": [
            _summarize_messages([message])
            for message in (new_messages or [])[-3:]
            if _summarize_messages([message])
        ],
    }
    return _truncate_campaign_summary(json.dumps(summary, ensure_ascii=False, sort_keys=True))


def _campaign_summary_recent_overrides(state: ChemBOState, lookback: int = 5) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    observations = [item for item in state.get("observations", []) if isinstance(item, dict)]
    for obs in observations[-max(int(lookback), 1) :]:
        metadata = obs.get("metadata", {}) if isinstance(obs.get("metadata"), dict) else {}
        rank = metadata.get("autobo_rank") or metadata.get("autobo_shortlist_rank")
        try:
            rank_int = int(rank)
        except (TypeError, ValueError):
            continue
        if rank_int <= 1:
            continue
        result = _coerce_finite_float(obs.get("result"))
        predicted = _coerce_finite_float(metadata.get("predicted_value"))
        best_before = _coerce_finite_float(metadata.get("best_before_result"))
        improved = None
        if result is not None and best_before is not None:
            improved = result < best_before if str(state.get("optimization_direction", "maximize")).lower() == "minimize" else result > best_before
        results.append(
            {
                "iteration": obs.get("iteration"),
                "rank": rank_int,
                "result": result,
                "predicted_value": predicted,
                "improved": improved,
            }
        )
    return results


def _summarize_tool_message(message: ToolMessage) -> str:
    payload = _extract_json_from_response(_message_text(message))
    if not payload:
        return _message_text(message)[:220]
    summary = {
        "status": payload.get("status"),
        "strategy": payload.get("strategy"),
        "resolved_components": payload.get("resolved_components"),
        "recommended_index": payload.get("recommended_index"),
        "num_candidates": len(payload.get("shortlist", payload.get("candidates", []))),
        "fallback_reason": payload.get("metadata", {}).get("fallback_reason"),
    }
    return json.dumps(summary)


def _sanitize_context_message(message: BaseMessage) -> BaseMessage:
    if isinstance(message, ToolMessage):
        tool_name = getattr(message, "name", None) or "tool"
        return HumanMessage(content=f"[TOOL RESULT {tool_name}]\n{_summarize_tool_message(message)}")

    if isinstance(message, AIMessage) and getattr(message, "tool_calls", None):
        tool_names = [
            str(tool_call.get("name") or "tool")
            for tool_call in getattr(message, "tool_calls", [])
            if isinstance(tool_call, dict)
        ]
        tool_label = ", ".join(tool_names) if tool_names else "tool"
        content = _message_text(message).strip() or f"Requested tool call(s): {tool_label}."
        return AIMessage(content=f"{content}\n[Tool calls requested: {tool_label}]")

    return message


NODE_MAX_CHARS = {
    "select_candidate": 2400,
    "run_bo_iteration": 2400,
    "interpret_results": 2400,
    "generate_hypotheses": 2400,
    "warm_start": 2000,
    "reflect_and_decide": 1600,
    "default": 1200,
}


def _truncate_message_text(text: str, max_chars: int = 1200) -> str:
    raw_text = str(text or "")
    if "</think>" in raw_text:
        raw_text = raw_text.split("</think>", 1)[1].strip()
    json_blocks = list(re.finditer(r"```json\s*(\{.*?\})\s*```", raw_text, flags=re.DOTALL))
    if json_blocks:
        last_json = json_blocks[-1].group(0)
        if len(last_json) < max_chars - 40:
            return last_json.strip()
    normalized = " ".join(raw_text.split())
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[: max_chars - 15].rstrip()} [truncated]"


def _compact_message_for_state(
    message: BaseMessage,
    max_chars: int | None = None,
    node_name: str = "default",
) -> BaseMessage:
    sanitized = _sanitize_context_message(message)
    content = _message_text(sanitized)
    if max_chars is None and node_name != "__no_truncate__":
        max_chars = NODE_MAX_CHARS.get(node_name, NODE_MAX_CHARS["default"])
    if max_chars is not None:
        content = _truncate_message_text(content, max_chars=max_chars)
    if isinstance(sanitized, SystemMessage):
        return SystemMessage(content=content)
    if isinstance(sanitized, HumanMessage):
        return HumanMessage(content=content)
    if isinstance(sanitized, ToolMessage):
        return ToolMessage(
            content=content,
            name=getattr(sanitized, "name", None) or "tool",
            tool_call_id=getattr(sanitized, "tool_call_id", None) or "tool",
        )
    return AIMessage(content=content)


def _state_messages(messages: list[BaseMessage], max_chars: int | None = None) -> list[BaseMessage]:
    node_name = "default" if max_chars is not None else "__no_truncate__"
    return [_compact_message_for_state(message, max_chars=max_chars, node_name=node_name) for message in messages]


def _compact_tool_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    compact: dict[str, Any] = {}
    for key in ("status", "strategy", "recommended_index", "resolved_components", "surrogate_metrics"):
        value = payload.get(key)
        if value not in (None, "", [], {}):
            compact[key] = value
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and metadata:
        compact["metadata"] = metadata
    acquisition_values = payload.get("acquisition_values")
    if isinstance(acquisition_values, list) and acquisition_values:
        compact["acquisition_values"] = acquisition_values[:10]
    return compact


def _summarize_messages(messages: list[BaseMessage]) -> str:
    parts = []
    for message in messages:
        text = _message_text(message).strip()
        if not text:
            continue
        role = message.__class__.__name__.replace("Message", "")
        parts.append(f"{role}: {' '.join(text.split())[:220]}")
    return "\n".join(parts[-8:])


def _extract_latest_tool_payload(messages: list[BaseMessage]) -> dict[str, Any] | None:
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            payload = _extract_json_from_response(_message_text(message))
            if payload is not None:
                return payload
    return None


def _extract_last_json(messages: list[BaseMessage]) -> dict[str, Any] | None:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            payload = _extract_json_from_response(_message_text(message))
            if payload is not None:
                return payload
    return None


def _safe_tool_payload_to_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        parsed = _extract_json_from_response(payload)
        if isinstance(parsed, dict):
            return parsed
    return {"status": "error", "reason": "Tool payload was not valid JSON."}


def _conversation_used_tool(messages: list[BaseMessage], tool_name: str) -> bool:
    return any(
        isinstance(message, ToolMessage) and getattr(message, "name", "") == tool_name
        for message in messages
    )


def _message_has_tool_calls(message: BaseMessage) -> bool:
    return bool(getattr(message, "tool_calls", None))


def _message_text(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(json.dumps(item))
        return "\n".join(parts)
    return str(content)


def _extract_json_from_response(text: str) -> dict[str, Any] | None:
    candidate_texts = [match.group(1) for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)\s*```", str(text or ""), re.IGNORECASE)]
    candidate_texts.append(str(text or ""))
    for candidate_text in reversed(candidate_texts):
        payload = _extract_last_json_payload(candidate_text)
        if isinstance(payload, dict):
            return payload
    return None


def _extract_last_json_payload(text: str) -> Any | None:
    raw_text = str(text or "")
    if not raw_text.strip():
        return None

    decoder = json.JSONDecoder()
    best_payload: Any | None = None
    best_end = -1
    best_start = len(raw_text) + 1

    for match in re.finditer(r"[{\[]", raw_text):
        start = match.start()
        if not _looks_like_json_root(raw_text, start):
            continue
        try:
            payload, relative_end = decoder.raw_decode(raw_text[start:])
        except json.JSONDecodeError:
            continue
        absolute_end = start + int(relative_end)
        if absolute_end > best_end or (absolute_end == best_end and start < best_start):
            best_payload = payload
            best_end = absolute_end
            best_start = start
    return best_payload


def _looks_like_json_root(text: str, start: int) -> bool:
    index = int(start) - 1
    while index >= 0 and str(text[index]).isspace():
        index -= 1
    if index < 0:
        return True
    return text[index] not in ':,[{"'


def _truncate_campaign_summary(summary: str, max_chars: int = 3000) -> str:
    if len(summary) <= max_chars:
        return summary
    tail = summary[-(max_chars - 4) :]
    return f"...\n{tail.lstrip()}"


def _normalize_hypothesis(item: dict[str, Any], iteration: int, fallback_id: str) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or fallback_id),
        "text": str(item.get("text") or item.get("hypothesis") or "").strip(),
        "mechanism": str(item.get("mechanism", "")).strip(),
        "testable_prediction": str(item.get("testable_prediction") or item.get("test") or "").strip(),
        "confidence": str(item.get("confidence", "medium")).strip().lower(),
        "status": str(item.get("status", "active")).strip().lower(),
        "supporting_iterations": list(item.get("supporting_iterations", [])),
        "refuting_iterations": list(item.get("refuting_iterations", [])),
        "created_at_iteration": int(item.get("created_at_iteration", iteration)),
    }


def _hypothesis_identity(item: dict[str, Any]) -> tuple[str, str]:
    return (
        str(item.get("id", "")).strip().lower(),
        " ".join(str(item.get("text", "")).strip().lower().split()),
    )


def _first_dataset_backed_shortlist_record(
    shortlist: list[dict[str, Any]],
    oracle: DatasetOracle,
    preferred_index: int = 0,
) -> tuple[int, dict[str, Any]] | None:
    if not shortlist:
        return None

    ordered_indices = [preferred_index] + [index for index in range(len(shortlist)) if index != preferred_index]
    for index in ordered_indices:
        record = shortlist[index]
        candidate = record.get("candidate", {})
        if not isinstance(candidate, dict) or not oracle.candidate_exists(candidate):
            continue
        normalized_record = dict(record)
        normalized_record["candidate"] = oracle.lookup(candidate)["candidate"]
        return index, normalized_record
    return None


def _variable_domain_labels(variable: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for item in variable.get("domain", []):
        if isinstance(item, dict):
            label = item.get("label") or item.get("name") or item.get("value")
            if label is not None:
                labels.append(str(label))
        else:
            labels.append(str(item))
    return labels


def _match_domain_value(value: Any, variable: dict[str, Any]) -> str | None:
    text = str(value).strip()
    if not text:
        return None
    numeric_text = _coerce_finite_float(text)
    for label in _variable_domain_labels(variable):
        if text == label.strip():
            return label
        numeric_label = _coerce_finite_float(label)
        if numeric_text is not None and numeric_label is not None and abs(numeric_text - numeric_label) < 1e-9:
            return label
    return None


def _variable_continuous_bounds(variable: dict[str, Any]) -> tuple[float, float]:
    domain = list(variable.get("domain", [0.0, 1.0]))
    if len(domain) < 2:
        return 0.0, 1.0
    low = _coerce_float(domain[0], default=0.0)
    high = _coerce_float(domain[1], default=1.0)
    return (min(low, high), max(low, high))


def _bounds_are_integral(low: float, high: float) -> bool:
    return float(low).is_integer() and float(high).is_integer()


def _normalize_best_result(state: ChemBOState) -> float | None:
    if not state.get("observations"):
        return None
    best_result = state.get("best_result")
    if isinstance(best_result, (int, float)) and np.isfinite(float(best_result)):
        return float(best_result)
    return None


def _build_final_summary(state: ChemBOState) -> dict[str, Any]:
    best_result = _normalize_best_result(state)
    best_candidate = state.get("best_candidate", {}) if best_result is not None else {}
    total_experiments = len(state.get("observations", []))
    stop_reason = str(state.get("termination_reason") or "Campaign completed.").strip()
    hypothesis_status = _hypothesis_status_counts(state.get("hypotheses", []))
    proposal_strategy = str(
        state.get("effective_config", {}).get("proposal_strategy")
        or state.get("last_tool_payload", {}).get("metadata", {}).get("proposal_strategy")
        or "bo"
    )
    conclusion = _final_campaign_conclusion(total_experiments, best_result, best_candidate, stop_reason)
    memory_export = MemoryManager.from_dict(state.get("memory", {})).export_campaign_memory()
    return {
        "best_result": best_result,
        "best_candidate": best_candidate,
        "total_experiments": total_experiments,
        "hypothesis_status": hypothesis_status,
        "stop_reason": stop_reason,
        "proposal_strategy": proposal_strategy,
        "convergence_state": state.get("convergence_state", {}),
        "final_config": state.get("bo_config", {}),
        "autobo_switch_summary": _autobo_switch_summary(state),
        "descriptor_schema_summary": _descriptor_schema_summary(state),
        "af_selection_summary": _autobo_af_selection_summary(state),
        "llm_token_usage": state.get("llm_token_usage", {}),
        "memory_export": memory_export,
        "conclusion": conclusion,
    }


def _autobo_af_selection_summary(state: ChemBOState) -> dict[str, Any]:
    observations = list(state.get("observations", []) or [])
    if not observations:
        return {}
    counts_by_af = {"qlogei": 0, "qucb": 0, "ts": 0}
    consensus_histogram = {"0": 0, "1": 0, "2": 0, "3": 0}
    combinations: dict[str, int] = {}
    for observation in observations:
        metadata = observation.get("metadata", {}) if isinstance(observation.get("metadata"), dict) else {}
        af_sources = sorted(
            str(item).strip().lower()
            for item in (metadata.get("af_sources", []) or [])
            if str(item).strip()
        )
        for af_key in counts_by_af:
            if af_key in af_sources:
                counts_by_af[af_key] += 1
        consensus_count = max(0, min(len(af_sources), 3))
        consensus_histogram[str(consensus_count)] += 1
        combo_key = "+".join(af_sources) if af_sources else "none"
        combinations[combo_key] = combinations.get(combo_key, 0) + 1
    return {
        "total_selected_points": len(observations),
        "selected_by_af": counts_by_af,
        "consensus_histogram": consensus_histogram,
        "source_combinations": combinations,
    }


def _final_campaign_conclusion(
    total_experiments: int,
    best_result: float | None,
    best_candidate: dict[str, Any],
    stop_reason: str,
) -> str:
    if total_experiments == 0:
        return f"The campaign stopped before any experiments were executed. Stop reason: {stop_reason}"
    if best_result is None:
        return f"The campaign completed after {total_experiments} experiments without a valid best result. Stop reason: {stop_reason}"
    return (
        f"The campaign completed after {total_experiments} experiments. "
        f"Best result: {best_result:.4f} with candidate {json.dumps(best_candidate, sort_keys=True)}. "
        f"Stop reason: {stop_reason}"
    )


def _hypothesis_status_counts(hypotheses: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in hypotheses:
        status = str(item.get("status", "active"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def _merge_hypotheses(
    existing: list[dict[str, Any]],
    generated: list[dict[str, Any]],
    iteration: int,
) -> list[dict[str, Any]]:
    archived_existing = []
    for item in existing:
        archived = dict(item)
        if archived.get("status") == "active":
            archived["status"] = "archived"
        archived_existing.append(archived)
    next_index = len(archived_existing) + 1
    normalized = []
    for item in generated:
        normalized.append(_normalize_hypothesis(item, iteration, f"H{next_index}"))
        next_index += 1
    return archived_existing + normalized


def _update_hypothesis_statuses(
    hypotheses: list[dict[str, Any]],
    supported: list[str],
    refuted: list[str],
    archived: list[str],
    iteration: int,
) -> list[dict[str, Any]]:
    supported_set = {str(item) for item in supported}
    refuted_set = {str(item) for item in refuted}
    archived_set = {str(item) for item in archived}
    updated = []
    for item in hypotheses:
        current = dict(item)
        identifier = str(current.get("id"))
        text = str(current.get("text", ""))
        if identifier in supported_set or text in supported_set:
            current["status"] = "supported"
            current.setdefault("supporting_iterations", []).append(iteration)
            current["confidence_float"] = round(min(0.92, float(current.get("confidence_float", 0.5) or 0.5) * 1.20), 4)
        if identifier in refuted_set or text in refuted_set:
            current["status"] = "refuted"
            current.setdefault("refuting_iterations", []).append(iteration)
            current["confidence_float"] = round(max(0.05, float(current.get("confidence_float", 0.5) or 0.5) * 0.70), 4)
        if identifier in archived_set or text in archived_set:
            current["status"] = "archived"
        confidence_float = float(current.get("confidence_float", 0.5) or 0.5)
        current["confidence"] = "high" if confidence_float >= 0.65 else ("medium" if confidence_float >= 0.40 else "low")
        updated.append(current)
    return updated


def _update_best(
    best_result: float | None,
    best_candidate: dict[str, Any],
    result_value: float,
    candidate: dict[str, Any],
    direction: str,
) -> tuple[float, dict[str, Any], bool]:
    if best_result is None:
        return result_value, candidate, True
    if direction == "minimize":
        improved = result_value < best_result
    else:
        improved = result_value > best_result
    return (result_value, candidate, True) if improved else (best_result, best_candidate, False)


def _parse_human_response(human_response) -> tuple[float, str, dict[str, Any]]:
    if isinstance(human_response, (int, float)):
        return float(human_response), "", {}
    if isinstance(human_response, dict):
        metadata = human_response.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        return float(human_response.get("result", 0.0)), str(human_response.get("notes", "")), metadata
    if isinstance(human_response, str):
        try:
            return float(human_response), "", {}
        except ValueError:
            try:
                parsed = json.loads(human_response)
                metadata = parsed.get("metadata", {})
                if not isinstance(metadata, dict):
                    metadata = {}
                return float(parsed.get("result", 0.0)), str(parsed.get("notes", "")), metadata
            except (json.JSONDecodeError, ValueError):
                return 0.0, human_response, {}
    return 0.0, str(human_response), {}


def _count_stagnation(perf_log: list[dict[str, Any]]) -> int:
    if not perf_log:
        return 0
    count = 0
    best = perf_log[-1].get("best_so_far")
    for entry in reversed(perf_log):
        if entry.get("best_so_far") == best:
            count += 1
        else:
            break
    return count


def _last_improvement_iteration(perf_log: list[dict[str, Any]]) -> int | None:
    for entry in reversed(perf_log):
        if entry.get("improved"):
            return entry.get("iteration")
    return perf_log[0].get("iteration") if perf_log else None


def _coerce_finite_float(value: Any) -> float | None:
    try:
        coerced = float(value)
    except (TypeError, ValueError):
        return None
    return coerced if np.isfinite(coerced) else None


def _delta_best(best_before: float | None, result_value: float | None, optimization_direction: str) -> float | None:
    if best_before is None or result_value is None:
        return None
    if str(optimization_direction).strip().lower() == "minimize":
        return best_before - result_value
    return result_value - best_before


def _coerce_float(value: Any, default: float) -> float:
    coerced = _coerce_finite_float(value)
    return float(default if coerced is None else coerced)


def _autobo_switch_summary(state: ChemBOState) -> dict[str, Any]:
    autobo_state = state.get("autobo_state", {}) or {}
    switches = autobo_state.get("switch_history", []) or []
    return {
        "total_switches": len(switches),
        "latest_switch": switches[-1] if switches else {},
        "active_model": autobo_state.get("active_model"),
    }


def _descriptor_schema_summary(state: ChemBOState) -> dict[str, Any]:
    autobo_state = state.get("autobo_state", {}) or {}
    history = list(autobo_state.get("descriptor_schema_history", []) or [])
    feature_spec = autobo_state.get("descriptor_feature_spec") or autobo_state.get("deep_ensemble_feature_spec") or {}
    diagnostics = feature_spec.get("descriptor_diagnostics", {}) if isinstance(feature_spec, dict) else {}
    schema_switches = [
        item for item in history
        if isinstance(item, dict) and str(item.get("event") or "") == "switch"
    ]
    return {
        "active_descriptor_schema_id": autobo_state.get("active_descriptor_schema_id", ""),
        "active_descriptor_schema": autobo_state.get("active_descriptor_schema", {}),
        "selected_descriptors_by_variable": diagnostics.get("selected_descriptors_by_variable", {}),
        "schema_switch_count": len(schema_switches),
        "latest_schema_switch": schema_switches[-1] if schema_switches else {},
        "last_descriptor_audit": autobo_state.get("last_descriptor_audit", {}),
        "descriptor_diagnostics": diagnostics,
        "schema_history": history,
    }


def _coerce_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if np.isfinite(value) else default
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            numeric = float(text)
        except ValueError:
            return default
        return int(numeric) if np.isfinite(numeric) else default
    return default
