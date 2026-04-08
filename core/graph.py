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
from core.context_builder import ContextBuilder
from core.dataset_oracle import DatasetOracle
from core.problem_loader import has_structured_problem_spec, normalize_problem_spec, resolve_campaign_budget
from core.state import CampaignPhase, ChemBOState, NextAction
from knowledge import (
    cards_to_structured_priors,
    run_knowledge_augmentation,
)
from memory.memory_manager import MemoryManager
from pools.component_pools import candidate_to_key, create_encoder, create_surrogate, detect_runtime_capabilities
from tools import build_retrieval_tools
from tools.chembo_tools import ALL_TOOLS, bo_runner, generate_warm_start_candidates

logger = logging.getLogger(__name__)


RECONFIG_RULES = {
    "min_iterations_since_last_reconfig": 3,
    "max_total_reconfigs": 3,
    "min_data_for_reconfig": 5,
    "require_backtesting": True,
    "max_relative_rmse_increase": 0.15,
}


def _bootstrap_knowledge_state(problem_spec: dict[str, Any], settings: Settings) -> tuple[list[dict[str, Any]], dict[str, Any], str, dict[str, Any]]:
    variables = problem_spec.get("variables", []) if isinstance(problem_spec, dict) else []
    empty_priors = cards_to_structured_priors([], variables)
    try:
        return run_knowledge_augmentation(problem_spec, settings)
    except Exception as exc:  # pragma: no cover - defensive runtime fallback
        logger.warning("Knowledge augmentation failed; continuing without cards: %s", exc)
        artifacts: dict[str, Any] = {
            "queries": [],
            "query_validation_notes": [],
            "retrieval_failures": [],
            "chunk_counts": {},
            "leakage_filter_summary": {},
            "snippet_count": 0,
            "card_count": 0,
            "card_generation_notes": [f"Knowledge augmentation failed: {type(exc).__name__}: {exc}"],
        }
        return [], artifacts, "", empty_priors


def build_chembo_graph(settings: Settings):
    llm = _create_llm(settings)
    llm_with_tools = llm.bind_tools(ALL_TOOLS)
    tool_map = {tool.name: tool for tool in ALL_TOOLS}
    graph = StateGraph(ChemBOState)
    proposal_strategy = "pure_reasoning" if bool(getattr(settings, "ablation_pure_reasoning", False)) else "bo"

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
            problem_spec, messages, llm_usage = _invoke_json_node(llm, state, prompt, default)
            problem_spec["raw_description"] = state["problem_spec"].get("raw_description", "")
            problem_spec = normalize_problem_spec(problem_spec)

        knowledge_cards, retrieval_artifacts, kb_context, kb_priors = _bootstrap_knowledge_state(problem_spec, settings)
        reaction_type = problem_spec.get("reaction_type", "")
        updates = {
            "messages": _state_messages(messages),
            "phase": CampaignPhase.PARSING.value,
            "problem_spec": problem_spec,
            "knowledge_cards": knowledge_cards,
            "retrieval_artifacts": retrieval_artifacts,
            "kb_context": kb_context,
            "kb_priors": kb_priors,
            "optimization_direction": str(problem_spec.get("optimization_direction", "maximize")).lower(),
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[parse_input] reaction_type={reaction_type or 'unknown'} knowledge_cards={len(knowledge_cards)}"],
        }
        if not has_structured_problem_spec(existing_spec):
            _attach_llm_usage(updates, state, "parse_input", llm_usage)
        return updates

    def select_embedding(state: ChemBOState) -> dict[str, Any]:
        if state.get("embedding_locked") and state.get("embedding_config"):
            message = AIMessage(content="Embedding already locked; reusing previous embedding configuration.")
            return {
                "messages": _state_messages([message]),
                "phase": CampaignPhase.SELECTING_EMBEDDING.value,
                "campaign_summary": _updated_campaign_summary(state, [message]),
            }

        context = ContextBuilder.for_select_embedding(state)
        prompt = f"""Select the single best embedding method for this optimization campaign.

CONTEXT:
{json.dumps(context, indent=2)}

Call embedding_method_advisor first, then respond with strict JSON:
{{
  "method": "<embedding key>",
  "params": {{}},
  "rationale": "...",
  "confidence": 0.0
}}"""
        messages, _, llm_usage = _invoke_tool_loop(
            llm_with_tools,
            state,
            prompt,
            tool_map=tool_map,
        )
        default = {
            "method": "one_hot",
            "params": {},
            "rationale": "Fallback to a stable baseline encoder for low-data BoTorch optimization.",
            "confidence": 0.5,
        }
        parsed = _extract_last_json(messages) or default
        encoder = create_encoder(parsed.get("method", "one_hot"), state["problem_spec"].get("variables", []), parsed.get("params", {}))
        embedding_config = {
            "method": encoder.metadata.get("resolved_key", parsed.get("method", "one_hot")),
            "requested_method": parsed.get("method", "one_hot"),
            "params": parsed.get("params", {}),
            "rationale": parsed.get("rationale", default["rationale"]),
            "dim": encoder.dim,
            "confidence": float(parsed.get("confidence", 0.5)),
            "metadata": encoder.metadata,
        }
        effective_config = dict(state.get("effective_config", {}))
        effective_config.update(
            {
                "runtime_mode": detect_runtime_capabilities()["runtime_mode"],
                "embedding_method": embedding_config["method"],
                "embedding_notes": encoder.metadata.get("notes", []),
            }
        )
        updates = {
            "messages": _state_messages(messages),
            "phase": CampaignPhase.SELECTING_EMBEDDING.value,
            "embedding_config": embedding_config,
            "embedding_locked": True,
            "effective_config": effective_config,
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[select_embedding] requested={parsed.get('method')} resolved={embedding_config['method']}"],
        }
        _attach_llm_usage(updates, state, "select_embedding", llm_usage)
        return updates

    def generate_hypotheses(state: ChemBOState) -> dict[str, Any]:
        memory_manager = MemoryManager.from_dict(state["memory"], capacity=settings.episodic_memory_capacity)
        context = ContextBuilder.for_generate_hypotheses(state, memory_manager)
        prompt = f"""Generate 3-5 high-value hypotheses for this campaign.

CONTEXT:
{json.dumps(context, indent=2)}

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
            llm_with_tools,
            state,
            prompt,
            tool_map=tool_map,
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

    def update_hypotheses(state: ChemBOState) -> dict[str, Any]:
        memory_manager = MemoryManager.from_dict(state["memory"], capacity=settings.episodic_memory_capacity)
        context = ContextBuilder.for_generate_hypotheses(state, memory_manager)
        active_hypotheses = [item for item in state.get("hypotheses", []) if item.get("status") in {"active", "supported"}]
        retrieval_tools = build_retrieval_tools(settings, state["problem_spec"])
        llm_with_retrieval = llm.bind_tools(ALL_TOOLS + retrieval_tools)
        retrieval_tool_map = {tool.name: tool for tool in retrieval_tools}
        full_tool_map = {**tool_map, **retrieval_tool_map}
        prompt = f"""Update the active hypotheses for reconfiguration.

CONTEXT:
{json.dumps(context, indent=2)}

CURRENT_ACTIVE_HYPOTHESES:
{json.dumps(active_hypotheses, indent=2)}

RETRIEVAL PROTOCOL (optional - call only when needed):
- You may call local_rag_search, literature_search, or web_search_tool if the current observations, memory, and KB context are insufficient to support or revise hypotheses.
- Prefer retrieval for mechanism explanations, literature precedents, or reagent/property references that are directly relevant to the active hypotheses.
- Do not retrieve to confirm facts that are already clear from the context above.
- When retrieval changes a hypothesis, cite the supporting snippet id in the hypothesis text or mechanism field.

Call hypothesis_generator first. If needed, call retrieval tools. Then respond with strict JSON:
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
            llm_with_retrieval,
            state,
            prompt,
            tool_map=full_tool_map,
        )
        parsed = _extract_last_json(messages) or {
            "hypotheses": active_hypotheses,
            "working_memory_focus": "Preserve supported hypotheses and add only evidence-backed refinements.",
        }
        hypotheses = _incremental_hypotheses_update(
            state.get("hypotheses", []),
            parsed.get("hypotheses", []),
            state["iteration"],
        )
        memory_manager.update_working(
            "current_focus",
            parsed.get("working_memory_focus", "Refine BO settings without discarding supported hypotheses."),
        )
        updates = {
            "messages": _state_messages(messages),
            "phase": CampaignPhase.HYPOTHESIZING.value,
            "hypotheses": hypotheses,
            "memory": memory_manager.to_dict(),
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[update_hypotheses] count={len(parsed.get('hypotheses', []))}"],
        }
        _attach_llm_usage(updates, state, "update_hypotheses", llm_usage)
        return updates

    def configure_bo(state: ChemBOState) -> dict[str, Any]:
        memory_manager = MemoryManager.from_dict(state["memory"], capacity=settings.episodic_memory_capacity)
        context = ContextBuilder.for_configure_bo(state, memory_manager)
        prompt = f"""Configure the BoTorch surrogate, kernel, and acquisition function.

CONTEXT:
{json.dumps(context, indent=2)}

Call surrogate_model_selector and af_selector, then return strict JSON:
{{
  "surrogate_model": "gp",
  "surrogate_params": {{}},
  "kernel_config": {{
    "key": "matern52|matern32|rbf|sum_kernel|product_kernel|mixed_sum_product",
    "params": {{}},
    "rationale": "..."
  }},
  "acquisition_function": "log_ei|ucb|ts|qlog_ei",
  "af_params": {{}},
  "rationale": "...",
  "confidence": 0.0
}}"""
        messages, _, llm_usage = _invoke_tool_loop(llm_with_tools, state, prompt, tool_map=tool_map, max_turns=8)
        parsed = _extract_last_json(messages) or {
            "surrogate_model": "gp",
            "surrogate_params": {},
            "kernel_config": {"key": "matern52", "params": {}, "rationale": "General-purpose default."},
            "acquisition_function": "log_ei",
            "af_params": {},
            "rationale": "Stable default BoTorch BO stack for Phase 1.",
            "confidence": 0.5,
        }

        proposed_bo_config = {
            "surrogate_model": parsed.get("surrogate_model", "gp"),
            "surrogate_params": parsed.get("surrogate_params", {}),
            "kernel_config": {
                "key": parsed.get("kernel_config", {}).get("key", "matern52"),
                "params": parsed.get("kernel_config", {}).get("params", {}),
                "rationale": parsed.get("kernel_config", {}).get("rationale", ""),
            },
            "acquisition_function": parsed.get("acquisition_function", "log_ei"),
            "af_params": parsed.get("af_params", {}),
            "rationale": parsed.get("rationale", ""),
            "confidence": float(parsed.get("confidence", 0.5)),
            "config_version": len(state.get("config_history", [])) + 1,
            "validated": True,
        }

        effective_config = dict(state.get("effective_config", {}))
        effective_config.update(
            {
                "runtime_mode": detect_runtime_capabilities()["runtime_mode"],
                "embedding_method": state.get("embedding_config", {}).get("method"),
                "surrogate_model": proposed_bo_config["surrogate_model"],
                "kernel_config": proposed_bo_config["kernel_config"],
                "acquisition_function": proposed_bo_config["acquisition_function"],
                "proposal_strategy": proposal_strategy,
                "fallbacks": [],
            }
        )

        accepted_config = proposed_bo_config
        accepted = True
        backtest_summary: dict[str, Any] | None = None
        outbound_messages = list(messages)
        config_history = state.get("config_history", [])
        previous_config = state.get("bo_config", {})
        if previous_config:
            backtest_summary = _assess_reconfiguration_backtesting(state, previous_config, proposed_bo_config)
            accepted = bool(backtest_summary.get("accepted", False))
            if accepted:
                config_history = config_history + [proposed_bo_config]
            else:
                accepted_config = previous_config
                fallback_reason = str(backtest_summary.get("reason") or "Backtesting rejected the proposed BO configuration.")
                fallback_message = AIMessage(content=fallback_reason)
                outbound_messages.append(fallback_message)
                effective_config = dict(state.get("effective_config", {}))
                fallbacks = list(effective_config.get("fallbacks", []))
                fallbacks.append(fallback_reason)
                effective_config["fallbacks"] = fallbacks
        else:
            config_history = config_history + [proposed_bo_config]

        effective_config.update(
            {
                "surrogate_model": accepted_config.get("surrogate_model"),
                "kernel_config": accepted_config.get("kernel_config"),
                "acquisition_function": accepted_config.get("acquisition_function"),
            }
        )

        updates: dict[str, Any] = {
            "messages": _state_messages(outbound_messages),
            "phase": CampaignPhase.CONFIGURING.value,
            "bo_config": accepted_config,
            "effective_config": effective_config,
            "config_history": config_history,
            "campaign_summary": _updated_campaign_summary(state, outbound_messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [
                f"[configure_bo] surrogate={accepted_config['surrogate_model']} kernel={accepted_config['kernel_config']['key']} "
                f"af={accepted_config['acquisition_function']} accepted={accepted}"
            ],
        }
        if previous_config:
            updates["reconfig_history"] = state.get("reconfig_history", []) + [
                {
                    "iteration": state["iteration"],
                    "reason": proposed_bo_config.get("rationale", "Reconfigured after reflection."),
                    "old_config": previous_config,
                    "new_config": proposed_bo_config,
                    "accepted_config": accepted_config,
                    "validated": accepted,
                    "accepted": accepted,
                    "backtesting": backtest_summary or {"required": False, "accepted": accepted},
                }
            ]
            if accepted:
                updates["last_reconfig_iteration"] = state["iteration"]
                updates["total_reconfigs"] = int(state.get("total_reconfigs", 0)) + 1
        _attach_llm_usage(updates, state, "configure_bo", llm_usage)
        return updates

    def warm_start(state: ChemBOState) -> dict[str, Any]:
        context = ContextBuilder.for_warm_start(state)
        budget = resolve_campaign_budget(state.get("problem_spec", {}), settings)
        oracle = DatasetOracle.from_problem_spec(state.get("problem_spec", {}))
        generated = generate_warm_start_candidates(
            state["problem_spec"].get("variables", []),
            state.get("kb_priors", {}),
            n_total=min(settings.initial_doe_size, budget),
            n_prior=min(3, settings.initial_doe_size),
            seed=state["iteration"],
            hard_constraints=[],
            observed_keys={},
            candidate_pool=_dataset_candidate_pool(oracle),
        )
        default = {
            "initial_experiments": [
                {
                    "candidate": candidate,
                    "rationale": "Coverage-focused warm start candidate.",
                    "category": "exploration",
                }
                for candidate in generated
            ]
        }
        prompt = f"""Rank and explain the proposed initial experiments for the campaign.

CONTEXT:
{json.dumps(context, indent=2)}

GENERATED_CANDIDATES:
{json.dumps(generated, indent=2)}

Return strict JSON:
{{
  "initial_experiments": [
    {{
      "candidate": {{}},
      "rationale": "...",
      "category": "prior_guided|exploration"
    }}
  ]
}}"""
        parsed, messages, llm_usage = _invoke_json_node(llm, state, prompt, default)
        initial_experiments, filtered_count = _validate_warm_start_selection(
            generated,
            parsed.get("initial_experiments", []),
            settings.initial_doe_size,
        )
        outbound_messages = list(messages)
        if filtered_count:
            outbound_messages.append(
                AIMessage(
                    content=(
                        f"Filtered {filtered_count} invalid warm-start candidate(s) that were not present in the generated pool."
                    )
                )
            )
        shortlist = []
        for item in initial_experiments:
            shortlist.append(
                {
                    "candidate": item.get("candidate", {}),
                    "predicted_value": None,
                    "uncertainty": None,
                    "acquisition_value": None,
                    "constraint_violations": [],
                    "constraint_satisfied": True,
                    "warm_start_category": item.get("category", "exploration"),
                    "warm_start_rationale": item.get("rationale", ""),
                }
            )
        updates = {
            "messages": _state_messages(outbound_messages),
            "phase": CampaignPhase.WARM_STARTING.value,
            "proposal_shortlist": shortlist,
            "warm_start_queue": shortlist,
            "warm_start_target": len(shortlist),
            "warm_start_active": bool(shortlist),
            "campaign_summary": _updated_campaign_summary(state, outbound_messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[warm_start] shortlist={len(shortlist)} filtered={filtered_count}"],
        }
        _attach_llm_usage(updates, state, "warm_start", llm_usage)
        return updates

    def run_bo_iteration(state: ChemBOState) -> dict[str, Any]:
        config = state["bo_config"]
        tool_args = {
            "embedding_method": state.get("embedding_config", {}).get("method", "one_hot"),
            "embedding_params": json.dumps(state.get("embedding_config", {}).get("params", {})),
            "surrogate_model": config.get("surrogate_model", "gp"),
            "surrogate_params": json.dumps(config.get("surrogate_params", {})),
            "acquisition_function": config.get("acquisition_function", "log_ei"),
            "af_params": json.dumps(config.get("af_params", {})),
            "search_space": json.dumps(state["problem_spec"].get("variables", [])),
            "observations": json.dumps(state.get("observations", [])),
            "batch_size": settings.batch_size,
            "top_k": max(getattr(settings, "shortlist_top_k", 5), settings.batch_size),
            "kernel_config": json.dumps(config.get("kernel_config", {})),
            "reaction_type": state["problem_spec"].get("reaction_type", ""),
            "kb_priors": json.dumps(state.get("kb_priors", {}), default=str),
            "optimization_direction": state.get("optimization_direction", "maximize"),
        }
        payload_text = bo_runner.invoke(tool_args)
        messages = [
            AIMessage(content="Executed bo_runner directly from workflow state."),
            ToolMessage(
                content=payload_text if isinstance(payload_text, str) else json.dumps(payload_text),
                name=bo_runner.name,
                tool_call_id=f"direct-bo-runner-{state['iteration']}",
            ),
        ]
        payload = _extract_latest_tool_payload(messages) or {}
        compact_payload = _compact_tool_payload(payload)
        payload_metadata = dict(payload.get("metadata", {}))
        payload_metadata["proposal_strategy"] = proposal_strategy
        compact_payload["metadata"] = payload_metadata
        effective_config = dict(state.get("effective_config", {}))
        fallback_reasons = list(effective_config.get("fallbacks", []))
        payload_fallback = compact_payload.get("metadata", {}).get("fallback_reason")
        if payload_fallback:
            fallback_reasons.append(payload_fallback)
        effective_config.update(
            {
                "resolved_components": compact_payload.get("resolved_components", {}),
                "proposal_strategy": proposal_strategy,
                "fallbacks": fallback_reasons,
            }
        )
        return {
            "messages": _state_messages(messages),
            "phase": CampaignPhase.RUNNING.value,
            "proposal_shortlist": payload.get("shortlist", []),
            "last_tool_payload": compact_payload,
            "effective_config": effective_config,
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[run_bo_iteration] shortlist={len(payload.get('shortlist', []))}"],
        }

    def run_reasoning_iteration(state: ChemBOState) -> dict[str, Any]:
        memory_manager = MemoryManager.from_dict(state["memory"], capacity=settings.episodic_memory_capacity)
        context = ContextBuilder.for_run_reasoning_iteration(state, memory_manager)
        variables = state.get("problem_spec", {}).get("variables", [])
        observed_keys = {candidate_to_key(item.get("candidate", {})) for item in state.get("observations", []) if item.get("candidate")}
        hard_constraints: list[dict[str, Any]] = []
        oracle = DatasetOracle.from_problem_spec(state.get("problem_spec", {}))
        shortlist_target = max(getattr(settings, "shortlist_top_k", 5), settings.batch_size)
        fallback_candidates = _build_reasoning_fallback_candidates(
            variables=variables,
            kb_priors=state.get("kb_priors", {}),
            observed_keys=observed_keys,
            hard_constraints=hard_constraints,
            oracle=oracle,
            limit=shortlist_target,
            seed=state["iteration"],
        )
        default = {
            "proposals": [
                {
                    "candidate": item["candidate"],
                    "rationale": item["rationale"],
                    "category": item.get("category", "fallback"),
                }
                for item in fallback_candidates
            ]
        }
        prompt = f"""Propose exactly {shortlist_target} candidate experiments for the next iteration using pure scientific reasoning.

CONTEXT:
{json.dumps(context, indent=2)}

HARD_CONSTRAINTS:
{json.dumps([{"name": item.get("name", ""), "reason": item.get("reason", "")} for item in hard_constraints], indent=2)}

Rules:
- Use only the allowed values or numeric ranges in proposal_value_guide.
- Do not repeat any condition already listed in observed_candidates.
- Respect all hard constraints.
- If dataset_backed is true, every full candidate must correspond to a real dataset row.

Return strict JSON:
{{
  "proposals": [
    {{
      "candidate": {{}},
      "rationale": "...",
      "category": "exploitation|exploration|hypothesis_test"
    }}
  ]
}}"""
        parsed, messages, llm_usage = _invoke_json_node(llm, state, prompt, default)
        proposed = _normalize_reasoning_proposals(parsed.get("proposals", []), shortlist_target)
        validated, rejected = _validate_reasoning_proposals(
            proposed,
            variables=variables,
            hard_constraints=hard_constraints,
            observed_keys=observed_keys,
            oracle=oracle,
        )
        outbound_messages = list(messages)
        replacement_count = 0
        fallback_count = 0
        repair_attempted = False
        if len(validated) < shortlist_target:
            repair_attempted = True
            repaired = _repair_reasoning_shortlist(
                llm=llm,
                state=state,
                context=context,
                rejected=rejected,
                accepted=validated,
                variables=variables,
                hard_constraints=hard_constraints,
                observed_keys=observed_keys,
                oracle=oracle,
                target=shortlist_target,
            )
            repair_messages = repaired["messages"]
            if repair_messages:
                outbound_messages.extend(repair_messages)
            validated = repaired["accepted"]
            rejected = repaired["rejected"]
            replacement_count = repaired["replacement_count"]
            llm_usage = _accumulate_usage_delta(llm_usage, repaired["usage"])

        if len(validated) < shortlist_target:
            seen_shortlist = {candidate_to_key(item["candidate"]) for item in validated}
            for item in fallback_candidates:
                key = candidate_to_key(item["candidate"])
                if key in seen_shortlist:
                    continue
                validated.append(
                    {
                        "candidate": item["candidate"],
                        "rationale": item["rationale"],
                        "category": item.get("category", "fallback"),
                    }
                )
                seen_shortlist.add(key)
                fallback_count += 1
                if len(validated) >= shortlist_target:
                    break

        shortlist = [
            {
                "candidate": item["candidate"],
                "predicted_value": None,
                "uncertainty": None,
                "acquisition_value": None,
                "constraint_violations": [],
                "constraint_satisfied": True,
                "reasoning_rationale": item.get("rationale", ""),
                "reasoning_category": item.get("category", "reasoning"),
            }
            for item in validated[:shortlist_target]
        ]
        if rejected:
            outbound_messages.append(
                AIMessage(
                    content=(
                        f"Validated pure reasoning shortlist with {len(shortlist)} accepted candidate(s); "
                        f"{len(rejected)} proposal(s) were rejected during validation."
                    )
                )
            )
        payload = {
            "status": "success",
            "strategy": "pure_reasoning_shortlist",
            "shortlist": shortlist,
            "recommended_index": 0,
            "candidates": [item["candidate"] for item in shortlist],
            "predictions": [None for _ in shortlist],
            "uncertainties": [None for _ in shortlist],
            "acquisition_values": [None for _ in shortlist],
            "resolved_components": {
                "embedding_method": state.get("embedding_config", {}).get("method"),
                "surrogate_model": state.get("bo_config", {}).get("surrogate_model"),
                "kernel_config": state.get("bo_config", {}).get("kernel_config"),
                "acquisition_function": state.get("bo_config", {}).get("acquisition_function"),
            },
            "metadata": {
                "proposal_strategy": proposal_strategy,
                "validation_rejections": len(rejected),
                "repair_attempted": repair_attempted,
                "repair_replacements": replacement_count,
                "fallback_fill_count": fallback_count,
                "proposal_value_guide_size": len(context.get("proposal_value_guide", [])),
                "runtime_mode": detect_runtime_capabilities()["runtime_mode"],
            },
        }
        effective_config = dict(state.get("effective_config", {}))
        effective_config.update(
            {
                "proposal_strategy": proposal_strategy,
                "resolved_components": payload.get("resolved_components", {}),
            }
        )
        updates = {
            "messages": _state_messages(outbound_messages),
            "phase": CampaignPhase.RUNNING.value,
            "proposal_shortlist": shortlist,
            "last_tool_payload": _compact_tool_payload(payload),
            "effective_config": effective_config,
            "campaign_summary": _updated_campaign_summary(state, outbound_messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [
                f"[run_reasoning_iteration] shortlist={len(shortlist)} "
                f"rejected={len(rejected)} repaired={replacement_count} fallback={fallback_count}"
            ],
        }
        _attach_llm_usage(updates, state, "run_reasoning_iteration", llm_usage)
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

        memory_manager = MemoryManager.from_dict(state["memory"], capacity=settings.episodic_memory_capacity)
        context = ContextBuilder.for_select_candidate(state, memory_manager)
        default = {
            "selected_index": 0,
            "override": False,
            "override_candidate": None,
            "rationale": {
                "chemical_reasoning": "Default to the top shortlist candidate.",
                "hypothesis_alignment": "",
                "information_value": "",
                "concerns": "",
            },
            "confidence": 0.5,
        }
        prompt = f"""Select ONE candidate from the shortlist.

CONTEXT:
{json.dumps(context, indent=2)}

If none of the shortlist candidates is acceptable, you may override.
Return strict JSON:
{{
  "selected_index": 0,
  "override": false,
  "override_candidate": null,
  "rationale": {{
    "chemical_reasoning": "...",
    "hypothesis_alignment": "...",
    "information_value": "...",
    "concerns": "..."
  }},
  "confidence": 0.0
}}"""
        parsed, messages, llm_usage = _invoke_json_node(llm, state, prompt, default)
        shortlist = state.get("proposal_shortlist", [])
        raw_selected_index = parsed.get("selected_index", 0)
        chosen_index = _coerce_int(raw_selected_index, default=0)
        chosen_index = min(max(chosen_index, 0), max(len(shortlist) - 1, 0))
        outbound_messages = list(messages)
        if raw_selected_index != chosen_index and raw_selected_index not in (0, "0"):
            outbound_messages.append(
                AIMessage(
                    content=(
                        f"Normalized invalid selected_index={raw_selected_index!r} to shortlist index {chosen_index}."
                    )
                )
            )
        override_requested = bool(parsed.get("override", False))
        if override_requested and isinstance(parsed.get("override_candidate"), dict):
            candidate = parsed["override_candidate"]
            selected_record = {
                "candidate": candidate,
                "predicted_value": None,
                "uncertainty": None,
                "acquisition_value": None,
                "constraint_violations": [],
                "constraint_satisfied": True,
            }
        else:
            selected_record = shortlist[chosen_index] if shortlist else {
                "candidate": {},
                "predicted_value": None,
                "uncertainty": None,
                "acquisition_value": None,
                "constraint_violations": [],
                "constraint_satisfied": True,
            }
            candidate = selected_record.get("candidate", {})

        oracle = DatasetOracle.from_problem_spec(state.get("problem_spec", {}))
        if oracle is not None:
            if oracle.candidate_exists(candidate):
                candidate = oracle.lookup(candidate)["candidate"]
                selected_record = dict(selected_record)
                selected_record["candidate"] = candidate
            else:
                fallback_selection = _first_dataset_backed_shortlist_record(shortlist, oracle, preferred_index=chosen_index)
                if fallback_selection is not None:
                    fallback_index, fallback_record = fallback_selection
                    selected_record = fallback_record
                    candidate = fallback_record.get("candidate", {})
                    chosen_index = fallback_index
                    if override_requested:
                        override_requested = False
                        outbound_messages.append(
                            AIMessage(
                                content=(
                                    "Rejected override candidate because it is not present in the dataset; "
                                    f"falling back to shortlist index {fallback_index}."
                                )
                            )
                        )
                    else:
                        outbound_messages.append(
                            AIMessage(
                                content=(
                                    f"Replaced shortlist index {raw_selected_index!r} with dataset-backed shortlist index {fallback_index}."
                                )
                            )
                        )

        proposal_selected = {
            "selected_index": chosen_index,
            "override": override_requested,
            "candidate": candidate,
            "rationale": parsed.get("rationale", default["rationale"]),
            "confidence": _coerce_float(parsed.get("confidence"), default=0.5),
            "selection_source": "llm_shortlist",
        }
        current_proposal = {
            "candidates": [candidate],
            "selected_index": chosen_index,
        }
        updates = {
            "messages": _state_messages(outbound_messages),
            "phase": CampaignPhase.SELECTING_CANDIDATE.value,
            "proposal_selected": proposal_selected,
            "current_proposal": current_proposal,
            "campaign_summary": _updated_campaign_summary(state, outbound_messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[select_candidate] override={proposal_selected['override']} index={chosen_index}"],
        }
        _attach_llm_usage(updates, state, "select_candidate", llm_usage)
        return updates

    def await_human_results(state: ChemBOState) -> dict[str, Any]:
        proposal = state.get("current_proposal", {})
        candidate = (proposal.get("candidates") or [{}])[0]
        iteration = state["iteration"]
        human_response = interrupt(
            {
                "type": "experiment_request",
                "iteration": iteration + 1,
                "candidate": candidate,
                "message": f"Run experiment for iteration {iteration + 1}: {json.dumps(candidate, indent=2)}",
            }
        )
        result_value, notes, response_metadata = _parse_human_response(human_response)

        observation = {
            "iteration": iteration + 1,
            "candidate": candidate,
            "result": result_value,
            "metadata": {"notes": notes, **response_metadata},
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
        remaining_warm_start = list(state.get("warm_start_queue", []))
        if state.get("warm_start_active") and remaining_warm_start:
            remaining_warm_start = remaining_warm_start[1:]
        warm_start_active = bool(remaining_warm_start)
        return {
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
        }

    def interpret_results(state: ChemBOState) -> dict[str, Any]:
        memory_manager = MemoryManager.from_dict(state["memory"], capacity=settings.episodic_memory_capacity)
        context = ContextBuilder.for_interpret_results(state, memory_manager)
        retrieval_tools = build_retrieval_tools(settings, state["problem_spec"])
        llm_with_retrieval = llm.bind_tools(ALL_TOOLS + retrieval_tools)
        retrieval_tool_map = {tool.name: tool for tool in retrieval_tools}
        full_tool_map = {**tool_map, **retrieval_tool_map}
        prompt = f"""Interpret the latest experimental result and update memory.

CONTEXT:
{json.dumps(context, indent=2)}

RETRIEVAL PROTOCOL (optional - call only when needed):
- Only retrieve when the latest result is surprising, overturns an active hypothesis, or you cannot explain the behavior from the current context alone.
- Prefer retrieval for mechanism clarifications, literature precedents, or well-known reagent/property references that help explain the result.
- If the interpretation is already clear from observations and memory, do not retrieve.
- When retrieval changes the interpretation, cite the supporting snippet id in the interpretation or episodic_memory fields.

Call result_interpreter first. If needed, call retrieval tools. Then return strict JSON:
{{
  "interpretation": "...",
  "supported_hypotheses": ["H1"],
  "refuted_hypotheses": ["H2"],
  "archived_hypotheses": [],
  "episodic_memory": {{
    "reflection": "...",
    "lesson_learned": "...",
    "non_numerical_observations": "..."
  }},
  "semantic_rule": null,
  "working_memory": {{
    "current_focus": "...",
    "pending_decisions": ["..."]
  }}
}}"""
        messages, _, llm_usage = _invoke_tool_loop(llm_with_retrieval, state, prompt, tool_map=full_tool_map)
        parsed = _extract_last_json(messages) or {
            "interpretation": "Result logged for future reasoning.",
            "supported_hypotheses": [],
            "refuted_hypotheses": [],
            "archived_hypotheses": [],
            "episodic_memory": {
                "reflection": "Stored the latest result.",
                "lesson_learned": "",
                "non_numerical_observations": "",
            },
            "semantic_rule": None,
            "working_memory": {"current_focus": "Continue collecting evidence.", "pending_decisions": []},
        }

        latest_observation = state["observations"][-1] if state.get("observations") else {}
        episodic = parsed.get("episodic_memory", {})
        memory_manager.add_episode(
            iteration=int(latest_observation.get("iteration", state["iteration"])),
            config_snapshot=state.get("effective_config", {}),
            candidate=latest_observation.get("candidate", {}),
            result=latest_observation.get("result"),
            reflection=episodic.get("reflection", parsed.get("interpretation", "")),
            non_numerical_observations=episodic.get("non_numerical_observations", ""),
            lesson_learned=episodic.get("lesson_learned", ""),
        )
        if isinstance(parsed.get("semantic_rule"), dict) and parsed["semantic_rule"]:
            memory_manager.add_semantic_rule(parsed["semantic_rule"])
        for key, value in parsed.get("working_memory", {}).items():
            memory_manager.update_working(key, value)
        consolidation_trigger = "deep" if len(state.get("observations", [])) >= 5 and len(state.get("observations", [])) % 5 == 0 else "periodic"
        memory_manager.consolidate(state.get("observations", []), trigger=consolidation_trigger)
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
            "hypotheses": hypotheses,
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[interpret_results] {parsed.get('interpretation', '')[:120]}"],
        }
        _attach_llm_usage(updates, state, "interpret_results", llm_usage)
        return updates

    def reflect_and_decide(state: ChemBOState) -> dict[str, Any]:
        memory_manager = MemoryManager.from_dict(state["memory"], capacity=settings.episodic_memory_capacity)
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

        context = ContextBuilder.for_reflect_and_decide(state, memory_manager)
        prompt = f"""Reflect on campaign progress and decide the next action.

CONTEXT:
{json.dumps(context, indent=2)}

Return strict JSON:
{{
  "decision": "continue|reconfigure|stop",
  "reasoning": "...",
  "confidence": 0.0
}}"""
        default = {"decision": "continue", "reasoning": "Continue collecting data.", "confidence": 0.5}
        parsed, messages, llm_usage = _invoke_json_node(llm, state, prompt, default)
        decision = str(parsed.get("decision", "continue")).lower()
        next_action = NextAction.CONTINUE.value
        phase = CampaignPhase.REFLECTING.value
        termination_reason = ""
        if decision == "stop":
            next_action = NextAction.STOP.value
            phase = CampaignPhase.SUMMARIZING.value
            termination_reason = str(parsed.get("reasoning", "")).strip() or "The campaign was stopped by reflection."
        elif decision == "reconfigure":
            next_action = NextAction.RECONFIGURE.value
        updates = {
            "messages": _state_messages(messages),
            "phase": phase,
            "next_action": next_action,
            "convergence_state": convergence_state,
            "termination_reason": termination_reason,
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[reflect_and_decide] decision={decision} confidence={parsed.get('confidence', 0.0)}"],
        }
        _attach_llm_usage(updates, state, "reflect_and_decide", llm_usage)
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

    def reconfig_gate(state: ChemBOState) -> dict[str, Any]:
        observations = state.get("observations", [])
        iterations_since = state["iteration"] - int(state.get("last_reconfig_iteration", -999))
        reason = None
        if iterations_since < RECONFIG_RULES["min_iterations_since_last_reconfig"]:
            reason = "Rejected reconfiguration because it is too soon after the previous change."
        elif int(state.get("total_reconfigs", 0)) >= RECONFIG_RULES["max_total_reconfigs"]:
            reason = "Rejected reconfiguration because the maximum number of reconfigurations was reached."
        elif len(observations) < RECONFIG_RULES["min_data_for_reconfig"]:
            reason = "Rejected reconfiguration because there is not enough data yet."

        if reason is not None:
            message = AIMessage(content=reason)
            return {
                "messages": _state_messages([message]),
                "phase": CampaignPhase.REFLECTING.value,
                "next_action": NextAction.CONTINUE.value,
                "campaign_summary": _updated_campaign_summary(state, [message]),
                "llm_reasoning_log": state.get("llm_reasoning_log", []) + [f"[reconfig_gate] rejected {reason}"],
            }

        reconfig_message = (
            "Reconfiguration approved; refreshing hypotheses before the next pure reasoning proposal cycle."
            if proposal_strategy == "pure_reasoning"
            else "Reconfiguration approved; refreshing hypotheses and BO configuration."
        )
        message = AIMessage(content=reconfig_message)
        return {
            "messages": _state_messages([message]),
            "phase": CampaignPhase.RECONFIGURING.value,
            "next_action": NextAction.RECONFIGURE.value,
            "campaign_summary": _updated_campaign_summary(state, [message]),
            "llm_reasoning_log": state.get("llm_reasoning_log", []) + ["[reconfig_gate] approved"],
            "last_reconfig_iteration": state["iteration"] if proposal_strategy == "pure_reasoning" else state.get("last_reconfig_iteration"),
            "total_reconfigs": (
                int(state.get("total_reconfigs", 0)) + 1
                if proposal_strategy == "pure_reasoning"
                else int(state.get("total_reconfigs", 0))
            ),
        }

    def route_after_configure(state: ChemBOState) -> Literal["warm_start", "run_bo_iteration", "run_reasoning_iteration"]:
        if not state.get("observations"):
            return "warm_start"
        return "run_reasoning_iteration" if proposal_strategy == "pure_reasoning" else "run_bo_iteration"

    def route_after_reflect(state: ChemBOState) -> Literal["select_candidate", "run_bo_iteration", "run_reasoning_iteration", "reconfig_gate", "campaign_summary"]:
        if state.get("warm_start_active") and state.get("warm_start_queue"):
            return "select_candidate"
        action = state.get("next_action", "")
        if action == NextAction.STOP.value:
            return "campaign_summary"
        if action == NextAction.RECONFIGURE.value:
            return "reconfig_gate"
        return "run_reasoning_iteration" if proposal_strategy == "pure_reasoning" else "run_bo_iteration"

    def route_after_reconfig_gate(state: ChemBOState) -> Literal["update_hypotheses", "run_bo_iteration", "run_reasoning_iteration"]:
        if state.get("next_action") == NextAction.RECONFIGURE.value:
            return "update_hypotheses"
        return "run_reasoning_iteration" if proposal_strategy == "pure_reasoning" else "run_bo_iteration"

    def route_after_update_hypotheses(state: ChemBOState) -> Literal["configure_bo", "run_reasoning_iteration"]:
        return "run_reasoning_iteration" if proposal_strategy == "pure_reasoning" else "configure_bo"

    graph.add_node("parse_input", parse_input)
    graph.add_node("select_embedding", select_embedding)
    graph.add_node("generate_hypotheses", generate_hypotheses)
    graph.add_node("update_hypotheses", update_hypotheses)
    graph.add_node("configure_bo", configure_bo)
    graph.add_node("warm_start", warm_start)
    graph.add_node("run_bo_iteration", run_bo_iteration)
    graph.add_node("run_reasoning_iteration", run_reasoning_iteration)
    graph.add_node("select_candidate", select_candidate)
    graph.add_node("await_human_results", await_human_results)
    graph.add_node("interpret_results", interpret_results)
    graph.add_node("reflect_and_decide", reflect_and_decide)
    graph.add_node("campaign_summary", campaign_summary)
    graph.add_node("reconfig_gate", reconfig_gate)

    graph.add_edge(START, "parse_input")
    graph.add_edge("parse_input", "select_embedding")
    graph.add_edge("select_embedding", "generate_hypotheses")
    graph.add_edge("generate_hypotheses", "configure_bo")
    graph.add_conditional_edges("update_hypotheses", route_after_update_hypotheses)
    graph.add_conditional_edges("configure_bo", route_after_configure)
    graph.add_edge("warm_start", "select_candidate")
    graph.add_edge("run_bo_iteration", "select_candidate")
    graph.add_edge("run_reasoning_iteration", "select_candidate")
    graph.add_edge("select_candidate", "await_human_results")
    graph.add_edge("await_human_results", "interpret_results")
    graph.add_edge("interpret_results", "reflect_and_decide")
    graph.add_conditional_edges("reflect_and_decide", route_after_reflect)
    graph.add_conditional_edges("reconfig_gate", route_after_reconfig_gate)
    graph.add_edge("campaign_summary", END)

    return graph.compile(checkpointer=MemorySaver())


def _create_llm(settings: Settings):
    model_name = settings.llm_model.strip()
    lowered = model_name.lower()
    if settings.llm_base_url:
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("OpenAI-compatible endpoints require 'langchain-openai'.") from exc
        api_key_env = _resolve_openai_api_key_env(settings, lowered)
        api_key = os.getenv(api_key_env)
        if not api_key:
            raise RuntimeError(f"{api_key_env} is not set for the configured endpoint.")
        return ChatOpenAI(
            model=model_name,
            base_url=settings.llm_base_url,
            api_key=api_key,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            model_kwargs=_openai_compatible_model_kwargs(settings, lowered),
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


def _openai_compatible_model_kwargs(settings: Settings, lowered_model_name: str) -> dict[str, Any]:
    extra_body: dict[str, Any] = {}
    if settings.llm_enable_thinking is True:
        extra_body["enable_thinking"] = True
    elif settings.llm_enable_thinking is None and _is_dashscope_model(settings.llm_base_url, lowered_model_name):
        # DashScope exposes Kimi 2.5 thinking via the OpenAI-compatible API.
        extra_body["enable_thinking"] = True
    return {"extra_body": extra_body} if extra_body else {}


def _is_dashscope_model(base_url: str | None, lowered_model_name: str) -> bool:
    return bool(base_url and "dashscope.aliyuncs.com" in base_url.lower() and lowered_model_name.startswith("kimi-k2.5"))


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
) -> tuple[list[BaseMessage], str, dict[str, Any]]:
    context_messages, summary = _build_context_messages(state)
    conversation: list[BaseMessage] = [HumanMessage(content=prompt)]
    usage = _empty_usage_delta()
    for _ in range(max_turns):
        response, step_usage = _invoke_llm_with_tracking(llm, context_messages + conversation)
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
) -> tuple[dict[str, Any], list[BaseMessage], dict[str, Any]]:
    context_messages, _ = _build_context_messages(state)
    usage = _empty_usage_delta()
    response, step_usage = _invoke_llm_with_tracking(llm, context_messages + [HumanMessage(content=prompt)])
    usage = _accumulate_usage_delta(usage, step_usage)
    messages: list[BaseMessage] = [HumanMessage(content=prompt), response]
    parsed = _extract_json_from_response(_message_text(response))
    if parsed is None:
        repair_prompt = "Reply with strict JSON only. No prose."
        repair_response, repair_usage = _invoke_llm_with_tracking(
            llm,
            context_messages + messages + [HumanMessage(content=repair_prompt)],
        )
        usage = _accumulate_usage_delta(usage, repair_usage)
        messages += [HumanMessage(content=repair_prompt), repair_response]
        parsed = _extract_json_from_response(_message_text(repair_response))
    return parsed or default, messages, usage


def _invoke_llm_with_tracking(llm, messages: list[BaseMessage]) -> tuple[BaseMessage, dict[str, Any]]:
    response = llm.invoke(messages)
    return response, _extract_llm_usage(response, messages)


def _extract_llm_usage(response: BaseMessage, prompt_messages: list[BaseMessage]) -> dict[str, Any]:
    provider_usage = _extract_provider_usage(response)
    if provider_usage is not None:
        return provider_usage
    return _estimate_llm_usage(prompt_messages, response)


def _extract_provider_usage(response: BaseMessage) -> dict[str, Any] | None:
    for payload in (
        getattr(response, "usage_metadata", None),
        getattr(response, "response_metadata", None),
        getattr(response, "additional_kwargs", None),
    ):
        usage = _parse_usage_payload(payload)
        if usage is not None:
            return usage
    return None


def _parse_usage_payload(payload: Any) -> dict[str, Any] | None:
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
        }
    for key in ("usage_metadata", "token_usage", "usage", "tokens"):
        nested = _parse_usage_payload(payload.get(key))
        if nested is not None:
            return nested
    return None


def _estimate_llm_usage(prompt_messages: list[BaseMessage], response: BaseMessage) -> dict[str, Any]:
    input_tokens = sum(_estimate_message_tokens(message) for message in prompt_messages)
    output_tokens = _estimate_message_tokens(response)
    return {
        "calls": 1,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "estimated_calls": 1,
        "estimated": True,
    }


def _estimate_message_tokens(message: BaseMessage) -> int:
    text = _message_text(message)
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4) + 4


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
    }


def _accumulate_usage_delta(base: dict[str, Any], addition: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base or _empty_usage_delta())
    for key in ("calls", "input_tokens", "output_tokens", "total_tokens", "estimated_calls"):
        merged[key] = int(merged.get(key, 0)) + int(addition.get(key, 0))
    merged["estimated"] = bool(merged.get("estimated_calls", 0))
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
    }


def _merge_llm_usage(existing: dict[str, Any], node_name: str, usage: dict[str, Any]) -> dict[str, Any]:
    merged = {
        "calls": int(existing.get("calls", 0)),
        "input_tokens": int(existing.get("input_tokens", 0)),
        "output_tokens": int(existing.get("output_tokens", 0)),
        "total_tokens": int(existing.get("total_tokens", 0)),
        "estimated_calls": int(existing.get("estimated_calls", 0)),
        "by_node": {key: dict(value) for key, value in (existing.get("by_node") or {}).items()},
    }
    for key in ("calls", "input_tokens", "output_tokens", "total_tokens", "estimated_calls"):
        merged[key] += int(usage.get(key, 0))

    node_totals = dict(
        merged["by_node"].get(
            node_name,
            {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "estimated_calls": 0,
            },
        )
    )
    for key in ("calls", "input_tokens", "output_tokens", "total_tokens", "estimated_calls"):
        node_totals[key] = int(node_totals.get(key, 0)) + int(usage.get(key, 0))
    node_totals["estimated"] = bool(node_totals.get("estimated_calls", 0))
    merged["by_node"][node_name] = node_totals
    return merged


def _build_context_messages(state: ChemBOState) -> tuple[list[BaseMessage], str]:
    messages = state.get("messages", [])
    if not messages:
        return [], state.get("campaign_summary", "")
    system_message = messages[0]
    recent = messages[1:][-20:]
    compressed: list[BaseMessage] = [system_message]
    summary = state.get("campaign_summary", "")
    if summary:
        compressed.append(HumanMessage(content=f"[CAMPAIGN SUMMARY]\n{summary}"))
    for message in recent:
        compressed.append(_sanitize_context_message(message))
    return compressed, summary


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


def _validate_warm_start_selection(
    generated: list[dict[str, Any]],
    proposed: list[dict[str, Any]],
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    generated_lookup = {candidate_to_key(candidate): candidate for candidate in generated}
    validated: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    filtered_count = 0

    for item in proposed:
        candidate = item.get("candidate")
        if not isinstance(candidate, dict):
            filtered_count += 1
            continue
        key = candidate_to_key(candidate)
        if key not in generated_lookup or key in seen_keys:
            filtered_count += 1
            continue
        seen_keys.add(key)
        validated.append(
            {
                "candidate": generated_lookup[key],
                "rationale": item.get("rationale", "Accepted ranked warm-start candidate."),
                "category": item.get("category", "exploration"),
            }
        )
        if len(validated) >= limit:
            return validated, filtered_count

    for candidate in generated:
        key = candidate_to_key(candidate)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        validated.append(
            {
                "candidate": candidate,
                "rationale": "Fallback to generated warm-start candidate after validation.",
                "category": "exploration",
            }
        )
        if len(validated) >= limit:
            break
    return validated, filtered_count


def _normalize_reasoning_proposals(proposals: list[dict[str, Any]], target: int) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in proposals[:target]:
        if isinstance(item, dict):
            normalized.append(
                {
                    "candidate": item.get("candidate", item.get("proposal", {})),
                    "rationale": str(item.get("rationale") or item.get("reasoning") or "").strip(),
                    "category": str(item.get("category") or "reasoning").strip(),
                }
            )
        else:
            normalized.append({"candidate": {}, "rationale": "", "category": "reasoning"})
    while len(normalized) < target:
        normalized.append({"candidate": {}, "rationale": "", "category": "reasoning"})
    return normalized


def _validate_reasoning_proposals(
    proposals: list[dict[str, Any]],
    *,
    variables: list[dict[str, Any]],
    hard_constraints: list[dict[str, Any]],
    observed_keys: set[str],
    oracle: DatasetOracle | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    shortlist_keys: set[str] = set()

    for slot, item in enumerate(proposals):
        candidate, reasons = _canonicalize_reasoning_candidate(
            item.get("candidate"),
            variables=variables,
            hard_constraints=hard_constraints,
            observed_keys=observed_keys,
            oracle=oracle,
        )
        if candidate is not None:
            key = candidate_to_key(candidate)
            if key in shortlist_keys:
                reasons.append("Duplicate of another proposal in the same shortlist.")
            else:
                shortlist_keys.add(key)
        if reasons or candidate is None:
            rejected.append(
                {
                    "slot": slot,
                    "candidate": item.get("candidate", {}),
                    "rationale": item.get("rationale", ""),
                    "category": item.get("category", "reasoning"),
                    "rejection_reasons": reasons or ["Candidate could not be normalized into the problem domain."],
                }
            )
            continue
        accepted.append(
            {
                "slot": slot,
                "candidate": candidate,
                "rationale": item.get("rationale", ""),
                "category": item.get("category", "reasoning"),
            }
        )
    return accepted, rejected


def _canonicalize_reasoning_candidate(
    candidate: Any,
    *,
    variables: list[dict[str, Any]],
    hard_constraints: list[dict[str, Any]],
    observed_keys: set[str],
    oracle: DatasetOracle | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(candidate, dict):
        return None, ["Candidate must be a JSON object with one value per variable."]

    normalized: dict[str, Any] = {}
    reasons: list[str] = []
    for variable in variables:
        name = str(variable.get("name") or "")
        variable_type = str(variable.get("type") or "categorical")
        if name not in candidate:
            reasons.append(f"Missing required variable '{name}'.")
            continue
        raw_value = candidate.get(name)
        if variable_type == "continuous":
            low, high = _variable_continuous_bounds(variable)
            value = _coerce_finite_float(raw_value)
            if value is None:
                reasons.append(f"Variable '{name}' must be numeric.")
                continue
            if value < low or value > high:
                reasons.append(f"Variable '{name}'={value} is outside the allowed range [{low}, {high}].")
                continue
            normalized[name] = round(value if not (_bounds_are_integral(low, high)) else round(value), 6)
            continue

        matched = _match_domain_value(raw_value, variable)
        if matched is None:
            reasons.append(
                f"Variable '{name}' must use one of the allowed values: {json.dumps(_variable_domain_labels(variable), ensure_ascii=False)}."
            )
            continue
        normalized[name] = matched

    if reasons:
        return None, reasons

    violations = [constraint["reason"] for constraint in hard_constraints if not constraint.get("check", lambda _: True)(normalized)]
    if violations:
        return None, [f"Hard constraint violated: {reason}" for reason in violations]

    key = candidate_to_key(normalized)
    if key in observed_keys:
        return None, ["Candidate has already been observed in this campaign."]

    if oracle is not None:
        if not oracle.candidate_exists(normalized):
            return None, ["Candidate does not correspond to a real row in the dataset."]
        normalized = oracle.lookup(normalized)["candidate"]

    return normalized, []


def _repair_reasoning_shortlist(
    *,
    llm,
    state: ChemBOState,
    context: dict[str, Any],
    rejected: list[dict[str, Any]],
    accepted: list[dict[str, Any]],
    variables: list[dict[str, Any]],
    hard_constraints: list[dict[str, Any]],
    observed_keys: set[str],
    oracle: DatasetOracle | None,
    target: int,
) -> dict[str, Any]:
    if not rejected:
        return {
            "accepted": accepted,
            "rejected": rejected,
            "messages": [],
            "usage": _empty_usage_delta(),
            "replacement_count": 0,
        }

    accepted_records = [
        {
            "candidate": item["candidate"],
            "rationale": item.get("rationale", ""),
            "category": item.get("category", "reasoning"),
        }
        for item in accepted
    ]
    repair_default = {
        "replacements": [
            {
                "slot": item["slot"],
                "candidate": {},
                "rationale": "Repair pass placeholder.",
                "category": "repair",
            }
            for item in rejected
        ]
    }
    prompt = f"""Repair the invalid proposal slots in this pure reasoning shortlist.

CONTEXT:
{json.dumps(context, indent=2)}

ACCEPTED_PROPOSALS:
{json.dumps(accepted_records, indent=2)}

INVALID_SLOTS:
{json.dumps(rejected, indent=2)}

Rules:
- Replace only the listed invalid slots.
- Keep all accepted proposals unchanged.
- Do not repeat any observed candidate or accepted proposal.
- Respect all hard constraints.
- If dataset_backed is true, every replacement must correspond to a real dataset row.

Return strict JSON:
{{
  "replacements": [
    {{
      "slot": 0,
      "candidate": {{}},
      "rationale": "...",
      "category": "repair"
    }}
  ]
}}"""
    parsed, messages, usage = _invoke_json_node(llm, state, prompt, repair_default)
    proposed_replacements = {}
    for item in parsed.get("replacements", []):
        if not isinstance(item, dict):
            continue
        slot = _coerce_int(item.get("slot"), default=-1)
        if slot < 0:
            continue
        proposed_replacements[slot] = {
            "candidate": item.get("candidate", {}),
            "rationale": str(item.get("rationale") or "").strip(),
            "category": str(item.get("category") or "repair").strip(),
        }

    rebuilt = accepted_records[:]
    for item in rejected:
        rebuilt.append(proposed_replacements.get(item["slot"], {"candidate": {}, "rationale": "", "category": "repair"}))

    revalidated, rerejected = _validate_reasoning_proposals(
        _normalize_reasoning_proposals(rebuilt, target),
        variables=variables,
        hard_constraints=hard_constraints,
        observed_keys=observed_keys,
        oracle=oracle,
    )
    return {
        "accepted": revalidated,
        "rejected": rerejected,
        "messages": messages,
        "usage": usage,
        "replacement_count": len(proposed_replacements),
    }


def _build_reasoning_fallback_candidates(
    *,
    variables: list[dict[str, Any]],
    kb_priors: dict[str, Any],
    observed_keys: set[str],
    hard_constraints: list[dict[str, Any]],
    oracle: DatasetOracle | None,
    limit: int,
    seed: int,
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    candidate_pool = _dataset_candidate_pool(oracle)

    for offset in range(4):
        generated = generate_warm_start_candidates(
            variables,
            kb_priors,
            n_total=max(limit * 2, 10),
            n_prior=min(3, max(limit, 1)),
            seed=seed + offset * 17,
            hard_constraints=hard_constraints,
            observed_keys=observed_keys | seen_keys,
            candidate_pool=candidate_pool,
        )
        for candidate in generated:
            normalized, reasons = _canonicalize_reasoning_candidate(
                candidate,
                variables=variables,
                hard_constraints=hard_constraints,
                observed_keys=observed_keys | seen_keys,
                oracle=oracle,
            )
            if normalized is None or reasons:
                continue
            key = candidate_to_key(normalized)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            collected.append(
                {
                    "candidate": normalized,
                    "rationale": "Deterministic fallback candidate generated from existing warm-start helpers.",
                    "category": "fallback",
                }
            )
            if len(collected) >= limit:
                return collected
    return collected


def _dataset_candidate_pool(oracle: DatasetOracle | None) -> list[dict[str, Any]] | None:
    if oracle is None:
        return None
    return [dict(candidate) for candidate in oracle.candidates]


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
    return {
        "best_result": best_result,
        "best_candidate": best_candidate,
        "total_experiments": total_experiments,
        "hypothesis_status": hypothesis_status,
        "stop_reason": stop_reason,
        "proposal_strategy": proposal_strategy,
        "convergence_state": state.get("convergence_state", {}),
        "final_config": state.get("bo_config", {}),
        "llm_token_usage": state.get("llm_token_usage", {}),
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


def _incremental_hypotheses_update(
    existing: list[dict[str, Any]],
    generated: list[dict[str, Any]],
    iteration: int,
) -> list[dict[str, Any]]:
    preserved: list[dict[str, Any]] = []
    seen_identities: set[tuple[str, str]] = set()
    next_index = len(existing) + 1

    for item in existing:
        current = dict(item)
        if current.get("status") in {"refuted", "archived"}:
            current["status"] = "archived"
        identity = _hypothesis_identity(current)
        if identity != ("", ""):
            seen_identities.add(identity)
        preserved.append(current)

    for item in generated:
        normalized = _normalize_hypothesis(item, iteration, f"H{next_index}")
        identity = _hypothesis_identity(normalized)
        next_index += 1
        if identity in seen_identities:
            continue
        preserved.append(normalized)
        seen_identities.add(identity)
    return preserved


def _assess_reconfiguration_backtesting(
    state: ChemBOState,
    old_config: dict[str, Any],
    new_config: dict[str, Any],
) -> dict[str, Any]:
    if not RECONFIG_RULES.get("require_backtesting", False):
        return {"required": False, "accepted": True, "reason": "Backtesting disabled."}

    old_score = _score_bo_config_on_observations(state, old_config)
    new_score = _score_bo_config_on_observations(state, new_config)
    if not old_score.get("ok") or not new_score.get("ok"):
        reason = str(new_score.get("reason") or old_score.get("reason") or "Backtesting could not evaluate both configurations.")
        return {
            "required": True,
            "accepted": False,
            "metric": "in_sample_rmse",
            "old_score": old_score,
            "new_score": new_score,
            "reason": reason,
        }

    old_rmse = float(old_score["rmse"])
    new_rmse = float(new_score["rmse"])
    threshold = old_rmse * (1.0 + float(RECONFIG_RULES.get("max_relative_rmse_increase", 0.15)))
    if old_rmse <= 1e-9:
        accepted = new_rmse <= 1e-9
    else:
        accepted = new_rmse <= threshold
    reason = (
        f"Accepted new configuration after backtesting (old RMSE={old_rmse:.4f}, new RMSE={new_rmse:.4f})."
        if accepted
        else (
            f"Rejected new configuration because backtesting RMSE worsened "
            f"from {old_rmse:.4f} to {new_rmse:.4f}."
        )
    )
    return {
        "required": True,
        "accepted": accepted,
        "metric": "in_sample_rmse",
        "old_score": old_score,
        "new_score": new_score,
        "threshold": threshold,
        "reason": reason,
    }


def _score_bo_config_on_observations(state: ChemBOState, config: dict[str, Any]) -> dict[str, Any]:
    observations = _dedupe_numeric_observations(state.get("observations", []))
    if len(observations) < RECONFIG_RULES["min_data_for_reconfig"]:
        return {"ok": False, "reason": "Not enough observations for backtesting.", "num_observations": len(observations)}

    variables = state.get("problem_spec", {}).get("variables", [])
    embedding_config = state.get("embedding_config", {})
    encoder = create_encoder(
        embedding_config.get("method", "one_hot"),
        variables,
        embedding_config.get("params", {}),
    )
    candidates = [item["candidate"] for item in observations]
    y_true = np.asarray([float(item["result"]) for item in observations], dtype=float)
    X_obs = encoder.encode_batch(candidates)
    if X_obs.shape[0] == 0:
        return {"ok": False, "reason": "No encodable observations for backtesting.", "num_observations": 0}

    direction = str(state.get("optimization_direction", "maximize")).strip().lower()
    y_model = y_true if direction != "minimize" else -1.0 * y_true
    y_mean = float(np.mean(y_model))
    y_std = float(np.std(y_model)) or 1.0
    y_scaled = (y_model - y_mean) / y_std

    try:
        surrogate = create_surrogate(
            config.get("surrogate_model", "gp"),
            config.get("surrogate_params", {}),
            config.get("kernel_config", {}).get("key", "matern52"),
        )
        surrogate.fit(X_obs, y_scaled)
        pred_scaled, _ = surrogate.predict(X_obs)
    except Exception as exc:  # pragma: no cover
        return {
            "ok": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "num_observations": len(observations),
            "config": {
                "surrogate_model": config.get("surrogate_model"),
                "kernel": config.get("kernel_config", {}).get("key"),
            },
        }

    pred_model = np.asarray(pred_scaled, dtype=float) * y_std + y_mean
    pred = pred_model if direction != "minimize" else -1.0 * pred_model
    rmse = float(np.sqrt(np.mean((pred - y_true) ** 2)))
    return {
        "ok": True,
        "rmse": rmse,
        "num_observations": len(observations),
        "resolved_surrogate": surrogate.metadata.get("resolved_key", config.get("surrogate_model")),
        "resolved_kernel": surrogate.metadata.get("resolved_kernel", config.get("kernel_config", {}).get("key")),
    }


def _dedupe_numeric_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for observation in observations:
        if observation.get("result") is None:
            continue
        candidate = observation.get("candidate") or {}
        key = candidate_to_key(candidate)
        bucket = grouped.setdefault(key, {"candidate": candidate, "results": []})
        bucket["results"].append(float(observation["result"]))
    deduped = []
    for bucket in grouped.values():
        deduped.append(
            {
                "candidate": bucket["candidate"],
                "result": float(np.mean(bucket["results"])),
            }
        )
    return deduped


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


def _coerce_float(value: Any, default: float) -> float:
    coerced = _coerce_finite_float(value)
    return float(default if coerced is None else coerced)


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
