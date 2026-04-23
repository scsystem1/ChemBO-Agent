"""
ChemBO Agent workflow graph.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Literal

import numpy as np
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from config.settings import Settings
from core.autobo_engine import (
    bootstrap_autobo_state,
    record_autobo_result,
    run_autobo_iteration,
    select_autobo_candidate,
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
from knowledge.augmentation_pipeline import run_knowledge_augmentation
from knowledge.knowledge_card import create_knowledge_card, should_evict_card, update_card_validation
from knowledge.knowledge_state import empty_knowledge_state
from memory.memory_manager import MemoryManager
from tools import build_retrieval_tools
from tools.chembo_tools import hypothesis_generator, result_interpreter

logger = logging.getLogger(__name__)


def _bootstrap_knowledge_state(
    problem_spec: dict[str, Any],
    settings: Settings,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not bool(getattr(settings, "knowledge_enabled", False)):
        artifacts: dict[str, Any] = {
            "queries": [],
            "query_validation_notes": [],
            "retrieval_failures": [],
            "source_health": [],
            "chunk_counts": {},
            "leakage_filter_summary": {},
            "snippet_count": 0,
            "card_count": 0,
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
    try:
        knowledge_state, knowledge_deck, retrieval_artifacts = run_knowledge_augmentation(problem_spec, settings)
        return knowledge_state, knowledge_deck, retrieval_artifacts
    except Exception as exc:  # pragma: no cover - defensive runtime fallback
        logger.warning("Knowledge augmentation failed; continuing without cards: %s", exc)
        artifacts: dict[str, Any] = {
            "queries": [],
            "query_validation_notes": [],
            "retrieval_failures": [],
            "source_health": [],
            "chunk_counts": {},
            "leakage_filter_summary": {},
            "snippet_count": 0,
            "card_count": 0,
            "card_generation_notes": [f"Knowledge augmentation failed: {type(exc).__name__}: {exc}"],
            "status": "failed",
        }
        knowledge_state = empty_knowledge_state(problem_spec)
        knowledge_state["enabled"] = True
        knowledge_state["status"] = "failed"
        return knowledge_state, {
            "cards": [],
            "build_summary": {
                "enabled": True,
                "status": "failed",
                "coverage_level": "gap",
                "notes": artifacts["card_generation_notes"],
            },
        }, artifacts


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
    latest_observation: dict[str, Any],
    parsed: dict[str, Any],
    maintenance_new_rules: list[dict[str, Any]],
    direction: str,
) -> dict[str, Any]:
    deck = dict(knowledge_deck or {})
    cards = [dict(item) for item in deck.get("cards", []) if isinstance(item, dict)]
    candidate = latest_observation.get("candidate", {}) if isinstance(latest_observation.get("candidate"), dict) else {}
    result = _coerce_finite_float(latest_observation.get("result"))
    metadata = latest_observation.get("metadata", {}) if isinstance(latest_observation.get("metadata"), dict) else {}
    best_before = _coerce_finite_float(metadata.get("best_before_result"))
    current_iteration = int(latest_observation.get("iteration", 0) or 0)
    improved = False
    if result is not None and best_before is not None:
        improved = result < best_before if str(direction).strip().lower() == "minimize" else result > best_before

    tension = {}
    episodic = parsed.get("episodic_memory", {}) if isinstance(parsed.get("episodic_memory"), dict) else {}
    if isinstance(episodic.get("knowledge_tension"), dict):
        tension = episodic.get("knowledge_tension", {})
    contradicted_ids = {
        str(item).strip()
        for item in tension.get("conflicting_cards", tension.get("conflicting_priors", [])) or []
        if str(item).strip()
    }

    updated_cards: list[dict[str, Any]] = []
    for card in cards:
        targets = [str(item).strip() for item in card.get("targets", []) if str(item).strip()]
        used = bool(targets and any(target in candidate for target in targets))
        supported: bool | None = True if used and improved else None
        if str(card.get("card_id") or "") in contradicted_ids:
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

    deck["cards"] = _promote_memory_rules_to_cards(updated_cards, maintenance_new_rules, current_iteration)
    summary = dict(deck.get("build_summary", {}) if isinstance(deck.get("build_summary"), dict) else {})
    summary["active_cards"] = len([card for card in deck["cards"] if str(card.get("status") or "active") in {"active", "validated"}])
    deck["build_summary"] = summary
    return deck


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
        if confidence < 0.7 or not statement or statement.lower() in existing_text:
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
    llm_plain = _create_llm(settings, enable_thinking_override=False)
    llm_thinking = _create_llm(settings, enable_thinking_override=True)
    graph = StateGraph(ChemBOState)
    proposal_strategy = "autobo_adaptive"

    def _memory_manager_from_state(state: ChemBOState) -> MemoryManager:
        return MemoryManager.from_dict(
            state.get("memory", {}),
            capacity=settings.episodic_memory_capacity,
            node_budgets=getattr(settings, "memory_node_budgets", {}),
            consolidation_every_n=int(getattr(settings, "memory_consolidation_every_n", 5)),
            enable_llm_consolidation=bool(getattr(settings, "memory_llm_consolidation_enabled", True)),
            llm_cooldown_iters=int(getattr(settings, "memory_llm_consolidation_cooldown_iters", 5)),
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
        _MemoryLLMAdapter(llm_thinking) if getattr(settings, "memory_llm_consolidation_enabled", True) else None
    )

    def parse_input(state: ChemBOState) -> dict[str, Any]:
        existing_spec = state["problem_spec"]
        if has_structured_problem_spec(existing_spec):
            problem_spec = normalize_problem_spec(dict(existing_spec))
            problem_spec.setdefault("raw_description", problem_spec.get("description", ""))
            messages = [AIMessage(content="Loaded structured problem specification from file; skipping LLM parsing.")]
        else:
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
                llm_plain,
                state,
                prompt,
                default,
                node_name="parse_input",
                recent_message_limits=settings.memory_recent_message_limits,
                inject_campaign_summary=bool(getattr(settings, "inject_campaign_summary_in_context", False)),
            )
            problem_spec["raw_description"] = state["problem_spec"].get("raw_description", "")
            problem_spec = normalize_problem_spec(problem_spec)

        knowledge_state, knowledge_deck, _retrieval_artifacts = _bootstrap_knowledge_state(problem_spec, settings)
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
        return updates

    def generate_hypotheses(state: ChemBOState) -> dict[str, Any]:
        memory_manager = _memory_manager_from_state(state)
        context = ContextBuilder.for_generate_hypotheses(state, memory_manager)
        llm_with_hypothesis = llm_plain.bind_tools([hypothesis_generator])
        reaction_guard = _reaction_identity_guard(state.get("problem_spec", {}))
        prompt = f"""Generate 3-5 high-value hypotheses for this campaign.

{reaction_guard}

CONTEXT:
{compact_json(context)}

Call hypothesis_generator first, then respond with strict JSON:
{{
  "hypotheses": [
    {{
      "id": "H1",
      "text": "...",
      "mechanism": "...",
      "testable_prediction": "...",
      "confidence": "low|medium|high",
      "status": "active"
    }}
  ],
  "working_memory_focus": "..."
}}"""
        messages, _, llm_usage = _invoke_tool_loop(
            llm_with_hypothesis,
            state,
            prompt,
            tool_map={hypothesis_generator.name: hypothesis_generator},
            node_name="generate_hypotheses",
            recent_message_limits=settings.memory_recent_message_limits,
            inject_campaign_summary=bool(getattr(settings, "inject_campaign_summary_in_context", False)),
        )
        parsed = _extract_last_json(messages) or {
            "hypotheses": [
                {
                    "text": "Begin with stable baselines, then adapt once evidence accumulates.",
                    "mechanism": "Low-data BO benefits from conservative initial assumptions.",
                    "testable_prediction": "Early runs should distinguish promising regions from weak baselines.",
                    "confidence": "medium",
                    "status": "active",
                }
            ],
            "working_memory_focus": "Collect enough data to validate or refute the first-pass hypotheses.",
        }
        hypotheses = _merge_hypotheses(state.get("hypotheses", []), parsed.get("hypotheses", []), state["iteration"])
        memory_manager.update_working(
            "current_focus",
            parsed.get("working_memory_focus", "Use hypotheses to guide configuration and candidate selection."),
        )
        updates = {
            "messages": _state_messages(messages),
            "phase": CampaignPhase.HYPOTHESIZING.value,
            "hypotheses": hypotheses,
            "memory": memory_manager.to_dict(),
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[generate_hypotheses] count={len(parsed.get('hypotheses', []))}"],
        }
        _attach_llm_usage(updates, state, "generate_hypotheses", llm_usage)
        return updates

    def warm_start(state: ChemBOState) -> dict[str, Any]:
        return plan_warm_start(
            state,
            settings,
            llm_plain,
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
            llm=llm_plain,
            invoke_json_node=lambda llm_obj, current_state, prompt, default, node_name="": _invoke_json_node(
                llm_obj,
                current_state,
                prompt,
                default,
                node_name=node_name,
                recent_message_limits=settings.memory_recent_message_limits,
                inject_campaign_summary=bool(getattr(settings, "inject_campaign_summary_in_context", False)),
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

        runtime = select_autobo_candidate(
            state=state,
            settings=settings,
            llm=llm_plain,
            invoke_json_node=lambda llm_obj, current_state, prompt, default, node_name="": _invoke_json_node(
                llm_obj,
                current_state,
                prompt,
                default,
                node_name=node_name,
                recent_message_limits=settings.memory_recent_message_limits,
                inject_campaign_summary=bool(getattr(settings, "inject_campaign_summary_in_context", False)),
            ),
        )
        messages = runtime.get("messages", [])
        updates = {
            "messages": _state_messages(messages),
            "phase": CampaignPhase.SELECTING_CANDIDATE.value,
            "proposal_selected": runtime.get("proposal_selected", {}),
            "current_proposal": runtime.get("current_proposal", {}),
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", []) + list(runtime.get("log_lines", [])),
        }
        _attach_llm_usage(updates, state, "select_candidate", runtime.get("llm_usage", _empty_usage_delta()))
        return updates

    def await_human_results(state: ChemBOState) -> dict[str, Any]:
        proposal = state.get("current_proposal", {})
        candidate = (proposal.get("candidates") or [{}])[0]
        iteration = state["iteration"]
        selected = state.get("proposal_selected", {}) or {}
        shortlist = state.get("proposal_shortlist", []) or []
        selected_index = _coerce_int(selected.get("selected_index"), default=0)
        shortlist_record = shortlist[selected_index] if 0 <= selected_index < len(shortlist) else {}
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
            "metadata": {
                "notes": notes,
                "predicted_value": shortlist_record.get("predicted_value"),
                "uncertainty": shortlist_record.get("uncertainty"),
                "acquisition_value": shortlist_record.get("acquisition_value"),
                "best_before_result": best_before_result,
                "config_version": state.get("bo_config", {}).get("config_version"),
                "selection_source": selected.get("selection_source"),
                "active_model": payload_metadata.get("active_model"),
                "autobo_rank": shortlist_record.get("autobo_rank"),
                **response_metadata,
            },
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
        autobo_result = record_autobo_result(
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
            "working_focus": working_focus,
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
    ) -> bool:
        metadata = latest_observation.get("metadata", {}) or {}
        knowledge_conflict = metadata.get("knowledge_conflict")
        if isinstance(knowledge_conflict, dict) and bool(knowledge_conflict.get("has_conflict")):
            return True
        result_value = _coerce_finite_float(latest_observation.get("result"))
        predicted = _coerce_finite_float(metadata.get("predicted_value"))
        best_before = _coerce_finite_float(metadata.get("best_before_result"))
        if predicted is not None and result_value is not None:
            denom = max(_result_scale(state), 1.0)
            if abs(result_value - predicted) / denom >= float(
                getattr(settings, "interpret_results_surprise_threshold", 1.5)
            ):
                return True
        convergence_state = state.get("convergence_state", {}) or {}
        if int(convergence_state.get("stagnation_length", 0) or 0) >= 3:
            return True
        previous = state.get("observations", [])[-2] if len(state.get("observations", [])) >= 2 else {}
        changed_variables = _changed_variables(previous.get("candidate", {}), latest_observation.get("candidate", {}))
        if not changed_variables:
            return False
        return not bool(memory_manager.semantic_graph.query_rules(variables=changed_variables, limit=1))

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
    ) -> dict[str, Any]:
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
            latest_observation=latest_observation,
            parsed=parsed,
            maintenance_new_rules=list(maintenance_report.new_rules),
            direction=state.get("optimization_direction", "maximize"),
        )
        hypotheses = _update_hypothesis_statuses(
            state.get("hypotheses", []),
            parsed.get("supported_hypotheses", []),
            parsed.get("refuted_hypotheses", []),
            parsed.get("archived_hypotheses", []),
            int(latest_observation.get("iteration", state["iteration"])),
        )
        updates = {
            "messages": _state_messages(messages),
            "phase": CampaignPhase.INTERPRETING.value,
            "memory": memory_manager.to_dict(),
            "knowledge_deck": knowledge_deck,
            "hypotheses": hypotheses,
            "campaign_summary": _updated_campaign_summary(state, messages),
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

    def interpret_results(state: ChemBOState) -> dict[str, Any]:
        memory_manager = _memory_manager_from_state(state)
        latest_observation = state["observations"][-1] if state.get("observations") else {}
        latest_selection_source = str((latest_observation.get("metadata") or {}).get("selection_source", ""))
        if latest_selection_source == "warm_start_queue":
            return interpret_warm_start_result(
                state,
                settings,
                llm_plain,
                memory_manager=memory_manager,
                build_context_messages=_build_context_messages,
                invoke_llm_with_tracking=_invoke_llm_with_tracking,
                extract_json_from_response=_extract_json_from_response,
                message_text=_message_text,
                state_messages=_state_messages,
                updated_campaign_summary=_updated_campaign_summary,
                attach_llm_usage=_attach_llm_usage,
            )
        if bool(getattr(settings, "interpret_results_fast_path_enabled", True)) and not _should_trigger_deep_interpretation(
            state,
            latest_observation,
        ):
            digest = _build_fast_interpretation_digest(state, latest_observation, memory_manager)
            reaction_guard = _reaction_identity_guard(state.get("problem_spec", {}))
            prompt = f"""Briefly interpret this single experimental result.

{reaction_guard}

DIGEST:
{compact_json(digest)}

If the observation contradicts any Active Knowledge Card, put its card_id in conflicting_cards and explain why.

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
                llm_plain,
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
        retrieval_tools = build_retrieval_tools(settings, state["problem_spec"]) if _should_bind_retrieval_tools(
            state,
            latest_observation,
            memory_manager,
        ) else []
        bound_tools = [result_interpreter] + retrieval_tools
        llm_with_retrieval = llm_thinking.bind_tools(bound_tools)
        retrieval_tool_map = {tool.name: tool for tool in retrieval_tools}
        full_tool_map = {result_interpreter.name: result_interpreter, **retrieval_tool_map}
        retrieval_protocol = (
            "Retrieval tools are available. Only retrieve when current memory cannot explain the result or a conflict requires evidence."
            if retrieval_tools
            else "Do not call retrieval tools for this interpretation; use current context only."
        )
        reaction_guard = _reaction_identity_guard(state.get("problem_spec", {}))
        prompt = f"""Interpret the latest experimental result and update campaign memory.

{reaction_guard}

CONTEXT:
{compact_json(context)}

{retrieval_protocol}

When knowledge affects your reasoning, cite card IDs. If the observation contradicts any Active Knowledge Card, put its card_id in conflicting_cards and explain why.

Call result_interpreter first. Then return strict JSON:
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
        messages, _, llm_usage = _invoke_tool_loop(
            llm_with_retrieval,
            state,
            prompt,
            tool_map=full_tool_map,
            node_name="interpret_results",
            recent_message_limits=settings.memory_recent_message_limits,
            inject_campaign_summary=bool(getattr(settings, "inject_campaign_summary_in_context", False)),
        )
        parsed = _extract_last_json(messages) or _default_interpretation_payload("Stored the latest result.")
        return _finalize_interpretation_updates(
            state,
            memory_manager,
            parsed,
            messages,
            llm_usage,
            latest_observation,
            mode_label="interpret_results:deep",
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
        if state.get("warm_start_active") and state.get("warm_start_queue"):
            return "select_candidate"
        action = state.get("next_action", "")
        if action == NextAction.STOP.value:
            return "campaign_summary"
        return "run_bo_iteration"

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


def _create_llm(settings: Settings, enable_thinking_override: bool | None = None):
    model_name = settings.llm_model.strip()
    lowered = model_name.lower()
    effective_thinking = settings.llm_enable_thinking if enable_thinking_override is None else enable_thinking_override
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
            max_tokens=settings.llm_max_tokens,
            extra_body=extra_body,
        )
    if lowered.startswith("claude"):
        from langchain_anthropic import ChatAnthropic

        if not os.getenv("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        return ChatAnthropic(
            model=model_name,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
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
            max_tokens=settings.llm_max_tokens,
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
        # DashScope exposes Kimi 2.5 thinking via the OpenAI-compatible API.
        extra_body["enable_thinking"] = True
    return {"extra_body": extra_body} if extra_body else {}


def _is_dashscope_model(base_url: str | None, lowered_model_name: str) -> bool:
    return bool(base_url and "dashscope.aliyuncs.com" in base_url.lower() and lowered_model_name.startswith("kimi-k2.5"))


def _is_moonshot_kimi_model(base_url: str | None, lowered_model_name: str) -> bool:
    return bool(base_url and "moonshot.cn" in base_url.lower() and lowered_model_name.startswith("kimi-k2.5"))


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
) -> tuple[dict[str, Any], list[BaseMessage], dict[str, Any]]:
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
        sanitized = _sanitize_context_message(message)
        compressed.append(sanitized)
        breakdown["recent_messages"] += _estimate_message_tokens(sanitized)
    return compressed, summary, breakdown


def _updated_campaign_summary(state: ChemBOState, new_messages: list[BaseMessage]) -> str:
    existing = state.get("campaign_summary", "")
    snippet = _summarize_messages(new_messages)
    if not snippet:
        return existing
    combined = f"{existing}\n{snippet}".strip() if existing else snippet
    return _truncate_campaign_summary(combined)


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


def _truncate_message_text(text: str, max_chars: int = 1200) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[: max_chars - 15].rstrip()} [truncated]"


def _compact_message_for_state(message: BaseMessage, max_chars: int = 1200) -> BaseMessage:
    sanitized = _sanitize_context_message(message)
    content = _truncate_message_text(_message_text(sanitized), max_chars=max_chars)
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


def _state_messages(messages: list[BaseMessage], max_chars: int = 1200) -> list[BaseMessage]:
    return [_compact_message_for_state(message, max_chars=max_chars) for message in messages]


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
    import re

    code_block = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if code_block:
        try:
            return json.loads(code_block.group(1))
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return None
    return None


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
        "llm_token_usage": state.get("llm_token_usage", {}),
        "memory_export": memory_export,
        "conclusion": conclusion,
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
        if identifier in refuted_set or text in refuted_set:
            current["status"] = "refuted"
            current.setdefault("refuting_iterations", []).append(iteration)
        if identifier in archived_set or text in archived_set:
            current["status"] = "archived"
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
