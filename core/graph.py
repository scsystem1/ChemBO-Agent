"""
ChemBO Agent workflow graph.
"""
from __future__ import annotations

import json
import os
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from config.settings import Settings
from core.context_builder import ContextBuilder
from core.problem_loader import has_structured_problem_spec
from core.state import CampaignPhase, ChemBOState, NextAction
from knowledge import format_knowledge_for_llm, get_hard_constraints, get_structured_priors
from memory.memory_manager import MemoryManager
from pools.component_pools import create_encoder, detect_runtime_capabilities
from tools.chembo_tools import ALL_TOOLS, generate_warm_start_candidates


RECONFIG_RULES = {
    "min_iterations_since_last_reconfig": 3,
    "max_total_reconfigs": 3,
    "min_data_for_reconfig": 5,
}


def build_chembo_graph(settings: Settings):
    llm = _create_llm(settings)
    llm_with_tools = llm.bind_tools(ALL_TOOLS)
    tool_map = {tool.name: tool for tool in ALL_TOOLS}
    graph = StateGraph(ChemBOState)

    def parse_input(state: ChemBOState) -> dict[str, Any]:
        existing_spec = state["problem_spec"]
        if has_structured_problem_spec(existing_spec):
            problem_spec = dict(existing_spec)
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
            problem_spec, messages = _invoke_json_node(llm, state, prompt, default)
            problem_spec["raw_description"] = state["problem_spec"].get("raw_description", "")

        reaction_type = problem_spec.get("reaction_type", "")
        kb_context = format_knowledge_for_llm(reaction_type)
        kb_priors = get_structured_priors(reaction_type, problem_spec)
        return {
            "messages": messages,
            "phase": CampaignPhase.PARSING.value,
            "problem_spec": problem_spec,
            "kb_context": kb_context,
            "kb_priors": kb_priors,
            "optimization_direction": str(problem_spec.get("optimization_direction", "maximize")).lower(),
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[parse_input] reaction_type={reaction_type or 'unknown'}"],
        }

    def select_embedding(state: ChemBOState) -> dict[str, Any]:
        if state.get("embedding_locked") and state.get("embedding_config"):
            message = AIMessage(content="Embedding already locked; reusing previous embedding configuration.")
            return {
                "messages": [message],
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
        messages, _ = _invoke_tool_loop(
            llm_with_tools,
            state,
            prompt,
            tool_map=tool_map,
        )
        default = {
            "method": "one_hot",
            "params": {},
            "rationale": "Fallback to stable baseline in low-data core mode.",
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
        return {
            "messages": messages,
            "phase": CampaignPhase.SELECTING_EMBEDDING.value,
            "embedding_config": embedding_config,
            "embedding_locked": True,
            "effective_config": effective_config,
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[select_embedding] requested={parsed.get('method')} resolved={embedding_config['method']}"],
        }

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
        messages, _ = _invoke_tool_loop(
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
        return {
            "messages": messages,
            "phase": CampaignPhase.HYPOTHESIZING.value,
            "hypotheses": hypotheses,
            "memory": memory_manager.to_dict(),
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[generate_hypotheses] count={len(parsed.get('hypotheses', []))}"],
        }

    def configure_bo(state: ChemBOState) -> dict[str, Any]:
        memory_manager = MemoryManager.from_dict(state["memory"], capacity=settings.episodic_memory_capacity)
        context = ContextBuilder.for_configure_bo(state, memory_manager)
        prompt = f"""Configure the surrogate family, kernel, and acquisition function.

CONTEXT:
{json.dumps(context, indent=2)}

Call surrogate_model_selector and af_selector, then return strict JSON:
{{
  "surrogate_model": "gp|random_forest|dkl",
  "surrogate_params": {{}},
  "kernel_config": {{
    "key": "matern52|matern32|rbf|sum_kernel|product_kernel|mixed_sum_product",
    "params": {{}},
    "rationale": "..."
  }},
  "acquisition_function": "log_ei|ucb|ts|qlog_ei|kg|qlog_nehvi",
  "af_params": {{}},
  "rationale": "...",
  "confidence": 0.0
}}"""
        messages, _ = _invoke_tool_loop(llm_with_tools, state, prompt, tool_map=tool_map)
        parsed = _extract_last_json(messages) or {
            "surrogate_model": "gp",
            "surrogate_params": {},
            "kernel_config": {"key": "matern52", "params": {}, "rationale": "General-purpose default."},
            "acquisition_function": "log_ei",
            "af_params": {},
            "rationale": "Stable default BO stack for Phase 1.",
            "confidence": 0.5,
        }

        bo_config = {
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
                "surrogate_model": bo_config["surrogate_model"],
                "kernel_config": bo_config["kernel_config"],
                "acquisition_function": bo_config["acquisition_function"],
                "fallbacks": [],
            }
        )

        config_history = state.get("config_history", []) + [bo_config]
        updates: dict[str, Any] = {
            "messages": messages,
            "phase": CampaignPhase.CONFIGURING.value,
            "bo_config": bo_config,
            "effective_config": effective_config,
            "config_history": config_history,
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [
                f"[configure_bo] surrogate={bo_config['surrogate_model']} kernel={bo_config['kernel_config']['key']} "
                f"af={bo_config['acquisition_function']}"
            ],
        }
        if state.get("bo_config"):
            updates["reconfig_history"] = state.get("reconfig_history", []) + [
                {
                    "iteration": state["iteration"],
                    "reason": bo_config.get("rationale", "Reconfigured after reflection."),
                    "old_config": state.get("bo_config", {}),
                    "new_config": bo_config,
                    "validated": True,
                }
            ]
            updates["last_reconfig_iteration"] = state["iteration"]
            updates["total_reconfigs"] = int(state.get("total_reconfigs", 0)) + 1
        return updates

    def warm_start(state: ChemBOState) -> dict[str, Any]:
        context = ContextBuilder.for_warm_start(state)
        generated = generate_warm_start_candidates(
            state["problem_spec"].get("variables", []),
            state.get("kb_priors", {}),
            n_total=min(settings.initial_doe_size, int(state["problem_spec"].get("budget", settings.initial_doe_size))),
            n_prior=min(3, settings.initial_doe_size),
            seed=state["iteration"],
            hard_constraints=get_hard_constraints(state["problem_spec"].get("reaction_type", "")),
            observed_keys={},
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
        parsed, messages = _invoke_json_node(llm, state, prompt, default)
        shortlist = []
        for item in parsed.get("initial_experiments", [])[: settings.initial_doe_size]:
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
        return {
            "messages": messages,
            "phase": CampaignPhase.WARM_STARTING.value,
            "proposal_shortlist": shortlist,
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[warm_start] shortlist={len(shortlist)}"],
        }

    def run_bo_iteration(state: ChemBOState) -> dict[str, Any]:
        memory_manager = MemoryManager.from_dict(state["memory"], capacity=settings.episodic_memory_capacity)
        context = ContextBuilder.for_configure_bo(state, memory_manager)
        config = state["bo_config"]
        prompt = f"""Run one Bayesian optimization shortlist step.

CONTEXT:
{json.dumps(context, indent=2)}

Call bo_runner with:
- embedding_method: {state.get('embedding_config', {}).get('method', 'one_hot')}
- embedding_params: {json.dumps(state.get('embedding_config', {}).get('params', {}))}
- surrogate_model: {config.get('surrogate_model', 'gp')}
- surrogate_params: {json.dumps(config.get('surrogate_params', {}))}
- acquisition_function: {config.get('acquisition_function', 'log_ei')}
- af_params: {json.dumps(config.get('af_params', {}))}
- search_space: {json.dumps(state['problem_spec'].get('variables', []))}
- observations: {json.dumps(state.get('observations', []))}
- batch_size: {settings.batch_size}
- top_k: {max(getattr(settings, 'shortlist_top_k', 5), settings.batch_size)}
- kernel_config: {json.dumps(config.get('kernel_config', {}))}
- reaction_type: {state['problem_spec'].get('reaction_type', '')}
- kb_priors: {json.dumps(state.get('kb_priors', {}), default=str)}
- optimization_direction: {state.get('optimization_direction', 'maximize')}

After the tool returns, reply with strict JSON:
{{
  "note": "shortlist generated",
  "confidence": 0.0
}}"""
        messages, _ = _invoke_tool_loop(llm_with_tools, state, prompt, tool_map=tool_map)
        payload = _extract_latest_tool_payload(messages) or {}
        effective_config = dict(state.get("effective_config", {}))
        effective_config.update(
            {
                "resolved_components": payload.get("resolved_components", {}),
                "fallbacks": [payload.get("metadata", {}).get("fallback_reason")] if payload.get("metadata", {}).get("fallback_reason") else [],
            }
        )
        return {
            "messages": messages,
            "phase": CampaignPhase.RUNNING.value,
            "proposal_shortlist": payload.get("shortlist", []),
            "last_tool_payload": payload,
            "effective_config": effective_config,
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[run_bo_iteration] shortlist={len(payload.get('shortlist', []))}"],
        }

    def select_candidate(state: ChemBOState) -> dict[str, Any]:
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
        parsed, messages = _invoke_json_node(llm, state, prompt, default)
        shortlist = state.get("proposal_shortlist", [])
        chosen_index = int(parsed.get("selected_index", 0))
        chosen_index = min(max(chosen_index, 0), max(len(shortlist) - 1, 0))
        if parsed.get("override") and isinstance(parsed.get("override_candidate"), dict):
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

        proposal_selected = {
            "selected_index": chosen_index,
            "override": bool(parsed.get("override", False)),
            "candidate": candidate,
            "rationale": parsed.get("rationale", default["rationale"]),
            "confidence": float(parsed.get("confidence", 0.5)),
        }
        current_proposal = {
            "candidates": [candidate],
            "shortlist": shortlist,
            "selected_index": chosen_index,
            "selected_record": selected_record,
        }
        return {
            "messages": messages,
            "phase": CampaignPhase.SELECTING_CANDIDATE.value,
            "proposal_selected": proposal_selected,
            "current_proposal": current_proposal,
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[select_candidate] override={proposal_selected['override']} index={chosen_index}"],
        }

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
        return {
            "messages": [HumanMessage(content=f"Experiment result: {result_value}. Notes: {notes}")],
            "phase": CampaignPhase.AWAITING_HUMAN.value,
            "iteration": iteration + 1,
            "observations": observations,
            "best_result": best_result,
            "best_candidate": best_candidate,
            "performance_log": performance_log,
        }

    def interpret_results(state: ChemBOState) -> dict[str, Any]:
        memory_manager = MemoryManager.from_dict(state["memory"], capacity=settings.episodic_memory_capacity)
        context = ContextBuilder.for_interpret_results(state, memory_manager)
        prompt = f"""Interpret the latest experimental result and update memory.

CONTEXT:
{json.dumps(context, indent=2)}

Call result_interpreter first, then return strict JSON:
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
        messages, _ = _invoke_tool_loop(llm_with_tools, state, prompt, tool_map=tool_map)
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
        return {
            "messages": messages,
            "phase": CampaignPhase.INTERPRETING.value,
            "memory": memory_manager.to_dict(),
            "hypotheses": hypotheses,
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[interpret_results] {parsed.get('interpretation', '')[:120]}"],
        }

    def reflect_and_decide(state: ChemBOState) -> dict[str, Any]:
        memory_manager = MemoryManager.from_dict(state["memory"], capacity=settings.episodic_memory_capacity)
        convergence_state = compute_convergence_state(state, settings)
        budget = int(state["problem_spec"].get("budget", settings.max_bo_iterations))
        if len(state.get("observations", [])) >= budget:
            message = AIMessage(content=f"Budget exhausted ({budget} experiments). Campaign complete.")
            return {
                "messages": [message],
                "phase": CampaignPhase.COMPLETED.value,
                "next_action": NextAction.STOP.value,
                "convergence_state": convergence_state,
                "campaign_summary": _updated_campaign_summary(state, [message]),
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
        parsed, messages = _invoke_json_node(llm, state, prompt, default)
        decision = str(parsed.get("decision", "continue")).lower()
        next_action = NextAction.CONTINUE.value
        phase = CampaignPhase.REFLECTING.value
        if decision == "stop":
            next_action = NextAction.STOP.value
            phase = CampaignPhase.COMPLETED.value
        elif decision == "reconfigure":
            next_action = NextAction.RECONFIGURE.value
        return {
            "messages": messages,
            "phase": phase,
            "next_action": next_action,
            "convergence_state": convergence_state,
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", [])
            + [f"[reflect_and_decide] decision={decision} confidence={parsed.get('confidence', 0.0)}"],
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
                "messages": [message],
                "phase": CampaignPhase.REFLECTING.value,
                "next_action": NextAction.CONTINUE.value,
                "campaign_summary": _updated_campaign_summary(state, [message]),
                "llm_reasoning_log": state.get("llm_reasoning_log", []) + [f"[reconfig_gate] rejected {reason}"],
            }

        message = AIMessage(content="Reconfiguration approved; refreshing hypotheses and BO configuration.")
        return {
            "messages": [message],
            "phase": CampaignPhase.RECONFIGURING.value,
            "next_action": NextAction.RECONFIGURE.value,
            "campaign_summary": _updated_campaign_summary(state, [message]),
            "llm_reasoning_log": state.get("llm_reasoning_log", []) + ["[reconfig_gate] approved"],
        }

    def route_after_configure(state: ChemBOState) -> Literal["warm_start", "run_bo_iteration"]:
        return "warm_start" if not state.get("observations") else "run_bo_iteration"

    def route_after_reflect(state: ChemBOState) -> Literal["run_bo_iteration", "reconfig_gate", "__end__"]:
        action = state.get("next_action", "")
        if action == NextAction.STOP.value:
            return END
        if action == NextAction.RECONFIGURE.value:
            return "reconfig_gate"
        return "run_bo_iteration"

    def route_after_reconfig_gate(state: ChemBOState) -> Literal["generate_hypotheses", "run_bo_iteration"]:
        if state.get("next_action") == NextAction.RECONFIGURE.value:
            return "generate_hypotheses"
        return "run_bo_iteration"

    graph.add_node("parse_input", parse_input)
    graph.add_node("select_embedding", select_embedding)
    graph.add_node("generate_hypotheses", generate_hypotheses)
    graph.add_node("configure_bo", configure_bo)
    graph.add_node("warm_start", warm_start)
    graph.add_node("run_bo_iteration", run_bo_iteration)
    graph.add_node("select_candidate", select_candidate)
    graph.add_node("await_human_results", await_human_results)
    graph.add_node("interpret_results", interpret_results)
    graph.add_node("reflect_and_decide", reflect_and_decide)
    graph.add_node("reconfig_gate", reconfig_gate)

    graph.add_edge(START, "parse_input")
    graph.add_edge("parse_input", "select_embedding")
    graph.add_edge("select_embedding", "generate_hypotheses")
    graph.add_edge("generate_hypotheses", "configure_bo")
    graph.add_conditional_edges("configure_bo", route_after_configure)
    graph.add_edge("warm_start", "select_candidate")
    graph.add_edge("run_bo_iteration", "select_candidate")
    graph.add_edge("select_candidate", "await_human_results")
    graph.add_edge("await_human_results", "interpret_results")
    graph.add_edge("interpret_results", "reflect_and_decide")
    graph.add_conditional_edges("reflect_and_decide", route_after_reflect)
    graph.add_conditional_edges("reconfig_gate", route_after_reconfig_gate)

    return graph.compile(checkpointer=MemorySaver())


def _create_llm(settings: Settings):
    model_name = settings.llm_model.strip()
    lowered = model_name.lower()
    if settings.llm_base_url:
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("OpenAI-compatible endpoints require 'langchain-openai'.") from exc
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set for the configured endpoint.")
        return ChatOpenAI(
            model=model_name,
            base_url=settings.llm_base_url,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
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
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set for the configured OpenAI model.")
        return ChatOpenAI(
            model=model_name,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )
    raise ValueError(f"Unsupported LLM model/provider for '{model_name}'.")


def compute_convergence_state(state: ChemBOState, settings: Settings) -> dict[str, Any]:
    perf_log = state.get("performance_log", [])
    patience = int(getattr(settings, "convergence_patience", 5))
    recent_bests = [entry.get("best_so_far") for entry in perf_log[-patience:] if entry.get("best_so_far") is not None]
    is_stagnant = len(recent_bests) >= patience and (max(recent_bests) - min(recent_bests)) < 1.0
    improvements = []
    for idx in range(1, len(perf_log)):
        previous = perf_log[idx - 1].get("best_so_far")
        current = perf_log[idx].get("best_so_far")
        if previous is None or current is None:
            continue
        improvements.append(current - previous)
    recent_improvement_rate = sum(improvements[-3:]) / max(1, len(improvements[-3:])) if improvements else float("inf")
    payload = state.get("last_tool_payload", {})
    acquisition_values = payload.get("acquisition_values", []) or []
    budget = int(state["problem_spec"].get("budget", settings.max_bo_iterations))
    return {
        "is_stagnant": is_stagnant,
        "stagnation_length": _count_stagnation(perf_log),
        "recent_improvement_rate": recent_improvement_rate,
        "max_af_value": max(acquisition_values) if acquisition_values else 0.0,
        "budget_used_ratio": len(state.get("observations", [])) / max(budget, 1),
        "last_improvement_iteration": _last_improvement_iteration(perf_log),
    }


def _invoke_tool_loop(llm, state: ChemBOState, prompt: str, tool_map: dict[str, Any], max_turns: int = 6) -> tuple[list[BaseMessage], str]:
    context_messages, summary = _build_context_messages(state)
    conversation: list[BaseMessage] = [HumanMessage(content=prompt)]
    for _ in range(max_turns):
        response = llm.invoke(context_messages + conversation)
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
    return conversation, summary


def _invoke_json_node(llm, state: ChemBOState, prompt: str, default: dict[str, Any]) -> tuple[dict[str, Any], list[BaseMessage]]:
    context_messages, _ = _build_context_messages(state)
    response = llm.invoke(context_messages + [HumanMessage(content=prompt)])
    messages: list[BaseMessage] = [HumanMessage(content=prompt), response]
    parsed = _extract_json_from_response(_message_text(response))
    if parsed is None:
        repair_prompt = "Reply with strict JSON only. No prose."
        repair_response = llm.invoke(context_messages + messages + [HumanMessage(content=repair_prompt)])
        messages += [HumanMessage(content=repair_prompt), repair_response]
        parsed = _extract_json_from_response(_message_text(repair_response))
    return parsed or default, messages


def _build_context_messages(state: ChemBOState) -> tuple[list[BaseMessage], str]:
    messages = state.get("messages", [])
    if not messages:
        return [], state.get("campaign_summary", "")
    system_message = messages[0]
    recent = messages[1:][-12:]
    compressed: list[BaseMessage] = [system_message]
    summary = state.get("campaign_summary", "")
    if summary:
        compressed.append(HumanMessage(content=f"[CAMPAIGN SUMMARY]\n{summary}"))
    for message in recent:
        if isinstance(message, ToolMessage):
            compressed.append(
                ToolMessage(
                    content=_summarize_tool_message(message),
                    name=getattr(message, "name", None),
                    tool_call_id=getattr(message, "tool_call_id", "tool"),
                )
            )
        else:
            compressed.append(message)
    return compressed, summary


def _updated_campaign_summary(state: ChemBOState, new_messages: list[BaseMessage]) -> str:
    existing = state.get("campaign_summary", "")
    snippet = _summarize_messages(new_messages)
    if not snippet:
        return existing
    return f"{existing}\n{snippet}".strip() if existing else snippet


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
        normalized.append(
            {
                "id": str(item.get("id") or f"H{next_index}"),
                "text": str(item.get("text") or item.get("hypothesis") or "").strip(),
                "mechanism": str(item.get("mechanism", "")).strip(),
                "testable_prediction": str(item.get("testable_prediction") or item.get("test") or "").strip(),
                "confidence": str(item.get("confidence", "medium")).strip().lower(),
                "status": str(item.get("status", "active")).strip().lower(),
                "supporting_iterations": list(item.get("supporting_iterations", [])),
                "refuting_iterations": list(item.get("refuting_iterations", [])),
                "created_at_iteration": int(item.get("created_at_iteration", iteration)),
            }
        )
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
