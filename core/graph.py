"""
ChemBO Agent — LangGraph Workflow
==================================
Stateful workflow for the ChemBO Phase 1 demo.
"""
from __future__ import annotations

import json
from typing import Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt

from config.settings import Settings
from core.state import CampaignPhase, ChemBOState, NextAction
from knowledge import format_knowledge_for_llm
from memory.memory_manager import MemoryManager
from tools.chembo_tools import ALL_TOOLS


def build_chembo_graph(settings: Settings):
    """Build and compile the ChemBO LangGraph."""

    llm = _create_llm(settings)
    llm_with_tools = llm.bind_tools(ALL_TOOLS)
    tool_node = ToolNode(ALL_TOOLS)

    graph = StateGraph(ChemBOState)

    def analyze_problem(state: ChemBOState) -> dict:
        problem_desc = state["problem_spec"].get("raw_description", "")
        prompt = f"""Analyze this chemical optimization problem and extract structured information.

PROBLEM DESCRIPTION:
{problem_desc}

Respond with EXACT JSON:
{{
  "reaction_type": "DAR | BH | Suzuki | ...",
  "target_metric": "yield",
  "optimization_direction": "maximize",
  "variables": [
    {{"name": "ligand", "type": "categorical", "domain": ["A", "B"], "description": "..." }}
  ],
  "constraints": ["..."],
  "budget": 30,
  "additional_context": "..."
}}"""
        parsed_spec, messages = _invoke_json_node(llm, state, prompt, {"raw_description": problem_desc})
        parsed_spec["raw_description"] = problem_desc
        kb_context = format_knowledge_for_llm(parsed_spec.get("reaction_type", ""))
        return {
            "messages": messages,
            "problem_spec": parsed_spec,
            "kb_context": kb_context,
            "phase": CampaignPhase.ANALYZING.value,
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", []) + [
                f"[analyze] reaction_type={parsed_spec.get('reaction_type', 'unknown')}"
            ],
        }

    def generate_hypotheses(state: ChemBOState) -> dict:
        memory_manager = MemoryManager.from_dict(state["memory"], capacity=settings.episodic_memory_capacity)
        if _has_recent_tool_result(state):
            prompt = """Use the hypothesis_generator tool output above and now return the final strict JSON only.

{
  "hypotheses": [
    {
      "hypothesis": "...",
      "mechanism": "...",
      "test": "...",
      "confidence": "low|medium|high"
    }
  ],
  "working_memory_focus": "what to pay attention to next"
}

Do not call any more tools unless the existing tool output is clearly unusable."""
        else:
            prompt = f"""Generate 3-5 chemically grounded, testable hypotheses before BO configuration.

PROBLEM SPEC:
{json.dumps(state["problem_spec"], indent=2)}

DOMAIN KNOWLEDGE:
{state.get("kb_context", "")}

MEMORY CONTEXT:
{memory_manager.get_context_for_llm()}

Call the hypothesis_generator tool first, then return strict JSON:
{{
  "hypotheses": [
    {{
      "hypothesis": "...",
      "mechanism": "...",
      "test": "...",
      "confidence": "low|medium|high"
    }}
  ],
  "working_memory_focus": "what to pay attention to next"
}}"""
        response, summary = _invoke_tool_reasoner(
            llm_with_tools,
            state,
            prompt,
            origin_node="generate_hypotheses",
            phase=CampaignPhase.HYPOTHESIZING.value,
        )
        return {
            "messages": response,
            "phase": CampaignPhase.HYPOTHESIZING.value,
            "tool_origin_node": "generate_hypotheses",
            "campaign_summary": summary,
        }

    def finalize_hypotheses(state: ChemBOState) -> dict:
        default = {
            "hypotheses": [
                {
                    "hypothesis": "Explore literature-prior ligand, base, and solvent combinations early.",
                    "mechanism": "Known reaction priors usually dominate early yield differences.",
                    "test": "Sample high-prior and off-prior conditions in the first few experiments.",
                    "confidence": "medium",
                }
            ],
            "working_memory_focus": "Use the initial hypotheses to guide BO component selection.",
        }
        parsed, messages = _repair_json_from_conversation(
            llm,
            state,
            required_prompt="""Convert the hypothesis discussion above into strict JSON:
{
  "hypotheses": [{"hypothesis": "...", "mechanism": "...", "test": "...", "confidence": "low|medium|high"}],
  "working_memory_focus": "..."
}""",
            default=default,
        )
        memory_manager = MemoryManager.from_dict(state["memory"], capacity=settings.episodic_memory_capacity)
        memory_manager.update_working(
            "current_focus",
            parsed.get("working_memory_focus", "Use the hypotheses to configure BO."),
        )
        structured_hypotheses = parsed.get("hypotheses", [])
        hypothesis_texts = []
        for idx, item in enumerate(structured_hypotheses, start=1):
            if isinstance(item, dict):
                hypothesis_texts.append(
                    f"H{idx}: {item.get('hypothesis', '')} | mechanism: {item.get('mechanism', '')} | "
                    f"test: {item.get('test', '')} | confidence: {item.get('confidence', 'unknown')}"
                )
            else:
                hypothesis_texts.append(str(item))
        return {
            "messages": messages,
            "hypotheses": hypothesis_texts,
            "memory": memory_manager.to_dict(),
            "phase": CampaignPhase.HYPOTHESIZING.value,
            "campaign_summary": _updated_campaign_summary(state, messages),
        }

    def configure_bo(state: ChemBOState) -> dict:
        memory_manager = MemoryManager.from_dict(state["memory"], capacity=settings.episodic_memory_capacity)
        if _has_recent_tool_result(state):
            prompt = """Use the tool results above and synthesize the final strict JSON BO configuration only.

{
  "embedding_method": "<key>",
  "embedding_params": {},
  "embedding_rationale": "...",
  "surrogate_model": "<key>",
  "surrogate_params": {},
  "surrogate_rationale": "...",
  "acquisition_function": "<key>",
  "af_params": {},
  "af_rationale": "..."
}

Do not call more tools unless one of the required selections is still missing."""
        else:
            prompt = f"""Configure the BO pipeline after reviewing the current hypotheses.

PROBLEM SPEC:
{json.dumps(state["problem_spec"], indent=2)}

DOMAIN KNOWLEDGE:
{state.get("kb_context", "")}

ACTIVE HYPOTHESES:
{json.dumps(state.get("hypotheses", []), indent=2)}

MEMORY CONTEXT:
{memory_manager.get_context_for_llm()}

You must select embedding, surrogate, and acquisition components.
1. Call embedding_method_advisor.
2. Call surrogate_model_selector.
3. Call af_selector.
4. After tool calls, synthesize a final strict JSON config."""
        response, summary = _invoke_tool_reasoner(
            llm_with_tools,
            state,
            prompt,
            origin_node="configure_bo",
            phase=CampaignPhase.CONFIGURING.value,
        )
        return {
            "messages": response,
            "phase": CampaignPhase.CONFIGURING.value,
            "tool_origin_node": "configure_bo",
            "campaign_summary": summary,
        }

    def finalize_bo_config(state: ChemBOState) -> dict:
        default_config = {
            "embedding_method": "one_hot",
            "embedding_params": {},
            "embedding_rationale": "Stable baseline for mixed categorical/continuous chemistry search spaces.",
            "surrogate_model": "gp_matern52",
            "surrogate_params": {},
            "surrogate_rationale": "Reliable low-data surrogate for smooth optimization surfaces.",
            "acquisition_function": "ei",
            "af_params": {},
            "af_rationale": "Balanced exploration and exploitation for single-objective BO.",
        }
        parsed, messages = _repair_json_from_conversation(
            llm,
            state,
            required_prompt="""Convert the BO configuration discussion above into strict JSON:
{
  "embedding_method": "<key>",
  "embedding_params": {},
  "embedding_rationale": "...",
  "surrogate_model": "<key>",
  "surrogate_params": {},
  "surrogate_rationale": "...",
  "acquisition_function": "<key>",
  "af_params": {},
  "af_rationale": "..."
}""",
            default=default_config,
        )
        config = default_config | parsed
        config["config_version"] = len(state.get("config_history", [])) + 1
        history = state.get("config_history", []) + [config]
        return {
            "messages": messages,
            "bo_config": config,
            "config_history": history,
            "phase": CampaignPhase.CONFIGURING.value,
            "campaign_summary": _updated_campaign_summary(state, messages),
        }

    def run_bo_iteration(state: ChemBOState) -> dict:
        memory_manager = MemoryManager.from_dict(state["memory"], capacity=settings.episodic_memory_capacity)
        config = state["bo_config"]
        if _has_recent_tool_result(state):
            prompt = """Use the bo_runner output above and explain the proposal in chemical terms.

Respond in prose only. Do not call bo_runner again unless the prior tool result is invalid."""
        else:
            prompt = f"""Run BO iteration {state['iteration'] + 1}.

CURRENT BO CONFIG:
{json.dumps(config, indent=2)}

ACTIVE HYPOTHESES:
{json.dumps(state.get("hypotheses", []), indent=2)}

DOMAIN KNOWLEDGE:
{state.get("kb_context", "")}

MEMORY CONTEXT:
{memory_manager.get_context_for_llm()}

Call bo_runner with:
- embedding_method: {config.get('embedding_method', 'one_hot')}
- embedding_params: {json.dumps(config.get('embedding_params', {}))}
- surrogate_model: {config.get('surrogate_model', 'gp_matern52')}
- surrogate_params: {json.dumps(config.get('surrogate_params', {}))}
- acquisition_function: {config.get('acquisition_function', 'ei')}
- af_params: {json.dumps(config.get('af_params', {}))}
- search_space: {json.dumps(state['problem_spec'].get('variables', []))}
- observations: {json.dumps(state.get('observations', []))}
- batch_size: {settings.batch_size}

After the tool returns, explain why the proposed condition is promising in chemical terms."""
        response, summary = _invoke_tool_reasoner(
            llm_with_tools,
            state,
            prompt,
            origin_node="run_bo_iteration",
            phase=CampaignPhase.RUNNING.value,
        )
        return {
            "messages": response,
            "phase": CampaignPhase.RUNNING.value,
            "tool_origin_node": "run_bo_iteration",
            "campaign_summary": summary,
        }

    def finalize_bo_proposal(state: ChemBOState) -> dict:
        payload = _extract_latest_tool_payload(state["messages"]) or state.get("last_tool_payload", {})
        candidates = payload.get("candidates") or []
        proposal = {
            "candidates": candidates,
            "predicted_values": payload.get("predictions") or [None for _ in candidates],
            "uncertainties": payload.get("uncertainties") or [None for _ in candidates],
            "acquisition_values": payload.get("acquisition_values") or [None for _ in candidates],
            "rationale": _last_ai_content(state["messages"]),
        }
        return {
            "current_proposal": proposal,
            "last_tool_payload": payload,
            "phase": CampaignPhase.RUNNING.value,
        }

    def await_human_results(state: ChemBOState) -> dict:
        proposal = state.get("current_proposal", {})
        iteration = state["iteration"]

        display_msg = (
            f"\n{'=' * 50}\n"
            f"EXPERIMENT REQUEST — Iteration {iteration + 1}\n"
            f"{'=' * 50}\n"
            f"{json.dumps(proposal, indent=2)}\n"
            f"{'=' * 50}\n"
        )

        human_response = interrupt(
            {
                "type": "experiment_request",
                "iteration": iteration + 1,
                "candidate": proposal,
                "message": display_msg,
            }
        )
        result_value, notes = _parse_human_response(human_response)

        proposal_candidates = proposal.get("candidates") or [{}]
        executed_candidate = proposal_candidates[0] if proposal_candidates else {}
        new_obs = {
            "iteration": iteration + 1,
            "candidate": executed_candidate,
            "result": result_value,
            "metadata": {"notes": notes},
        }
        observations = state["observations"] + [new_obs]

        best_result = state["best_result"]
        best_candidate = state["best_candidate"]
        if result_value > best_result:
            best_result = result_value
            best_candidate = executed_candidate

        perf_log = state.get("performance_log", []) + [
            {
                "iteration": iteration + 1,
                "result": result_value,
                "best_so_far": best_result,
            }
        ]
        return {
            "messages": [HumanMessage(content=f"Experiment result: {result_value}. Notes: {notes}")],
            "observations": observations,
            "best_result": best_result,
            "best_candidate": best_candidate,
            "performance_log": perf_log,
            "phase": CampaignPhase.AWAITING_HUMAN.value,
            "iteration": iteration + 1,
        }

    def interpret_results(state: ChemBOState) -> dict:
        latest_observation = state["observations"][-1:] if state.get("observations") else []
        if _has_recent_tool_result(state):
            prompt = """Use the result_interpreter output above and now return the final strict JSON only.

{
  "interpretation": "...",
  "supported_hypotheses": ["..."],
  "refuted_hypotheses": ["..."],
  "episodic_memory": {
    "reflection": "...",
    "lesson_learned": "...",
    "non_numerical_observations": "..."
  },
  "semantic_rule": null | {"rule": "...", "confidence": 0.0},
  "working_memory": {"current_focus": "...", "pending_decisions": ["..."]}
}

Do not call more tools unless the tool output is unusable."""
        else:
            prompt = f"""Interpret the latest experimental result and update memory.

LATEST OBSERVATION:
{json.dumps(latest_observation, indent=2)}

ALL OBSERVATIONS:
{json.dumps(state.get('observations', []), indent=2)}

ACTIVE HYPOTHESES:
{json.dumps(state.get('hypotheses', []), indent=2)}

CURRENT BO CONFIG:
{json.dumps(state.get('bo_config', {}), indent=2)}

Call result_interpreter first, then return strict JSON with:
{{
  "interpretation": "...",
  "supported_hypotheses": ["..."],
  "refuted_hypotheses": ["..."],
  "episodic_memory": {{
    "reflection": "...",
    "lesson_learned": "...",
    "non_numerical_observations": "..."
  }},
  "semantic_rule": null | {{"rule": "...", "confidence": 0.0}},
  "working_memory": {{"current_focus": "...", "pending_decisions": ["..."]}}
}}"""
        response, summary = _invoke_tool_reasoner(
            llm_with_tools,
            state,
            prompt,
            origin_node="interpret_results",
            phase=CampaignPhase.INTERPRETING.value,
        )
        return {
            "messages": response,
            "phase": CampaignPhase.INTERPRETING.value,
            "tool_origin_node": "interpret_results",
            "campaign_summary": summary,
        }

    def finalize_interpretation(state: ChemBOState) -> dict:
        default = {
            "interpretation": "The latest result has been logged; gather more evidence before forming a strong rule.",
            "supported_hypotheses": [],
            "refuted_hypotheses": [],
            "episodic_memory": {
                "reflection": "Observed the latest experiment and stored it for future reasoning.",
                "lesson_learned": "",
                "non_numerical_observations": "",
            },
            "semantic_rule": None,
            "working_memory": {"current_focus": "Use the latest result to decide whether to continue or reconfigure."},
        }
        parsed, messages = _repair_json_from_conversation(
            llm,
            state,
            required_prompt="""Convert the interpretation discussion above into strict JSON:
{
  "interpretation": "...",
  "supported_hypotheses": ["..."],
  "refuted_hypotheses": ["..."],
  "episodic_memory": {
    "reflection": "...",
    "lesson_learned": "...",
    "non_numerical_observations": "..."
  },
  "semantic_rule": null | {"rule": "...", "confidence": 0.0},
  "working_memory": {"current_focus": "...", "pending_decisions": ["..."]}
}""",
            default=default,
        )
        latest_obs = state["observations"][-1] if state.get("observations") else {}
        memory_manager = MemoryManager.from_dict(state["memory"], capacity=settings.episodic_memory_capacity)
        episodic = parsed.get("episodic_memory", {})
        memory_manager.add_episode(
            iteration=int(latest_obs.get("iteration", state["iteration"])),
            config_snapshot=state.get("bo_config", {}),
            candidate=latest_obs.get("candidate", {}),
            result=latest_obs.get("result"),
            reflection=episodic.get("reflection", parsed.get("interpretation", "")),
            non_numerical_observations=episodic.get("non_numerical_observations", ""),
            lesson_learned=episodic.get("lesson_learned", ""),
        )
        semantic_rule = parsed.get("semantic_rule")
        if isinstance(semantic_rule, dict) and semantic_rule.get("rule"):
            confidence = float(semantic_rule.get("confidence", 0.0))
            if confidence >= 0.6:
                memory_manager.add_semantic_rule(
                    semantic_rule["rule"],
                    confidence,
                    [int(latest_obs.get("iteration", state["iteration"]))],
                )
        working_memory = parsed.get("working_memory", {})
        for key, value in working_memory.items():
            memory_manager.update_working(key, value)
        memory_manager.consolidate()
        return {
            "messages": messages,
            "memory": memory_manager.to_dict(),
            "phase": CampaignPhase.INTERPRETING.value,
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", []) + [
                f"[interpret] {parsed.get('interpretation', '')[:160]}"
            ],
        }

    def reflect_and_decide(state: ChemBOState) -> dict:
        observations = state.get("observations", [])
        budget = int(state["problem_spec"].get("budget", settings.max_bo_iterations))
        if len(observations) >= budget:
            return {
                "phase": CampaignPhase.COMPLETED.value,
                "next_action": NextAction.STOP.value,
                "messages": [AIMessage(content=f"Budget exhausted ({budget} experiments). Campaign complete.")],
            }

        memory_manager = MemoryManager.from_dict(state["memory"], capacity=settings.episodic_memory_capacity)
        perf_log = state.get("performance_log", [])
        recent_results = [entry["best_so_far"] for entry in perf_log[-settings.convergence_patience :]]
        stagnant = (
            len(recent_results) >= settings.convergence_patience
            and (max(recent_results) - min(recent_results)) < settings.convergence_threshold * 100
        )
        prompt = f"""Reflect on the campaign progress and decide the next action.

PERFORMANCE LOG:
{json.dumps(perf_log[-5:], indent=2)}

CURRENT BEST:
{state.get('best_result')}

ACTIVE HYPOTHESES:
{json.dumps(state.get('hypotheses', []), indent=2)}

DOMAIN KNOWLEDGE:
{state.get('kb_context', '')}

MEMORY CONTEXT:
{memory_manager.get_context_for_llm()}

STAGNANT:
{stagnant}

Respond with strict JSON:
{{
  "decision": "continue" | "reconfigure" | "stop",
  "reasoning": "...",
  "confidence": 0.0
}}"""
        default = {"decision": "continue", "reasoning": "Continue collecting data.", "confidence": 0.5}
        parsed, messages = _invoke_json_node(llm, state, prompt, default)
        decision = str(parsed.get("decision", "continue")).lower()
        if decision == "stop":
            next_action = NextAction.STOP.value
            phase = CampaignPhase.COMPLETED.value
        elif decision == "reconfigure" or stagnant:
            next_action = NextAction.RECONFIGURE.value
            phase = CampaignPhase.REFLECTING.value
        else:
            next_action = NextAction.CONTINUE.value
            phase = CampaignPhase.REFLECTING.value
        return {
            "messages": messages,
            "phase": phase,
            "next_action": next_action,
            "campaign_summary": _updated_campaign_summary(state, messages),
            "llm_reasoning_log": state.get("llm_reasoning_log", []) + [
                f"[reflect] decision={decision}, confidence={parsed.get('confidence', 0.0)}"
            ],
        }

    def route_after_tool_capable_node(
        state: ChemBOState,
    ) -> Literal["tool_node", "finalize_hypotheses", "finalize_bo_config", "finalize_bo_proposal", "finalize_interpretation"]:
        last_msg = state["messages"][-1]
        if _message_has_tool_calls(last_msg):
            return "tool_node"
        phase = state.get("phase", "")
        if phase == CampaignPhase.HYPOTHESIZING.value:
            return "finalize_hypotheses"
        if phase == CampaignPhase.CONFIGURING.value:
            return "finalize_bo_config"
        if phase == CampaignPhase.RUNNING.value:
            return "finalize_bo_proposal"
        return "finalize_interpretation"

    def route_after_tool_call(
        state: ChemBOState,
    ) -> Literal["generate_hypotheses", "configure_bo", "run_bo_iteration", "interpret_results"]:
        origin = state.get("tool_origin_node", "run_bo_iteration")
        if origin == "generate_hypotheses":
            return "generate_hypotheses"
        if origin == "configure_bo":
            return "configure_bo"
        if origin == "interpret_results":
            return "interpret_results"
        return "run_bo_iteration"

    def route_after_reflect(state: ChemBOState) -> Literal["run_bo_iteration", "generate_hypotheses", "__end__"]:
        action = state.get("next_action", "")
        if action == NextAction.STOP.value:
            return END
        if action == NextAction.RECONFIGURE.value:
            return "generate_hypotheses"
        return "run_bo_iteration"

    graph.add_node("analyze_problem", analyze_problem)
    graph.add_node("generate_hypotheses", generate_hypotheses)
    graph.add_node("finalize_hypotheses", finalize_hypotheses)
    graph.add_node("configure_bo", configure_bo)
    graph.add_node("finalize_bo_config", finalize_bo_config)
    graph.add_node("run_bo_iteration", run_bo_iteration)
    graph.add_node("finalize_bo_proposal", finalize_bo_proposal)
    graph.add_node("await_human_results", await_human_results)
    graph.add_node("interpret_results", interpret_results)
    graph.add_node("finalize_interpretation", finalize_interpretation)
    graph.add_node("reflect_and_decide", reflect_and_decide)
    graph.add_node("tool_node", tool_node)

    graph.add_edge(START, "analyze_problem")
    graph.add_edge("analyze_problem", "generate_hypotheses")
    graph.add_conditional_edges("generate_hypotheses", route_after_tool_capable_node)
    graph.add_conditional_edges("configure_bo", route_after_tool_capable_node)
    graph.add_conditional_edges("run_bo_iteration", route_after_tool_capable_node)
    graph.add_conditional_edges("interpret_results", route_after_tool_capable_node)
    graph.add_conditional_edges("tool_node", route_after_tool_call)
    graph.add_edge("finalize_hypotheses", "configure_bo")
    graph.add_edge("finalize_bo_config", "run_bo_iteration")
    graph.add_edge("finalize_bo_proposal", "await_human_results")
    graph.add_edge("await_human_results", "interpret_results")
    graph.add_edge("finalize_interpretation", "reflect_and_decide")
    graph.add_conditional_edges("reflect_and_decide", route_after_reflect)

    return graph.compile(checkpointer=MemorySaver())


def _create_llm(settings: Settings):
    model_name = settings.llm_model.strip()
    lowered = model_name.lower()
    if lowered.startswith("claude"):
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model_name,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )
    if lowered.startswith(("gpt", "o1", "o3", "o4")):
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover - depends on optional package
            raise RuntimeError(
                "OpenAI chat models require the 'langchain-openai' package to be installed."
            ) from exc
        return ChatOpenAI(
            model=model_name,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )
    raise ValueError(f"Unsupported LLM model/provider for '{model_name}'.")


def _invoke_tool_reasoner(llm, state: ChemBOState, prompt: str, origin_node: str, phase: str) -> tuple[list[BaseMessage], str]:
    context_messages, summary = _build_context_messages(state)
    response = llm.invoke(context_messages + [HumanMessage(content=prompt)])
    return [HumanMessage(content=prompt), response], summary


def _invoke_json_node(llm, state: ChemBOState, prompt: str, default: dict) -> tuple[dict, list[BaseMessage]]:
    context_messages, _ = _build_context_messages(state)
    response = llm.invoke(context_messages + [HumanMessage(content=prompt)])
    parsed = _extract_json_from_response(_message_text(response))
    messages: list[BaseMessage] = [HumanMessage(content=prompt), response]
    if parsed is None:
        repair_prompt = "Your previous response did not contain valid JSON. Reply with JSON only."
        repair_response = llm.invoke(context_messages + messages + [HumanMessage(content=repair_prompt)])
        parsed = _extract_json_from_response(_message_text(repair_response)) or default
        messages += [HumanMessage(content=repair_prompt), repair_response]
    return parsed or default, messages


def _repair_json_from_conversation(
    llm,
    state: ChemBOState,
    required_prompt: str,
    default: dict,
) -> tuple[dict, list[BaseMessage]]:
    parsed = _extract_json_from_response(_last_ai_content(state["messages"]))
    if parsed is not None:
        return parsed, []
    context_messages, _ = _build_context_messages(state)
    response = llm.invoke(context_messages + [HumanMessage(content=required_prompt)])
    parsed = _extract_json_from_response(_message_text(response))
    messages: list[BaseMessage] = [HumanMessage(content=required_prompt), response]
    if parsed is None:
        retry_prompt = "Reply with JSON only. Do not add prose."
        retry_response = llm.invoke(context_messages + messages + [HumanMessage(content=retry_prompt)])
        parsed = _extract_json_from_response(_message_text(retry_response)) or default
        messages += [HumanMessage(content=retry_prompt), retry_response]
    return parsed or default, messages


def _build_context_messages(state: ChemBOState) -> tuple[list[BaseMessage], str]:
    messages = state.get("messages", [])
    if not messages:
        return [], state.get("campaign_summary", "")
    system_message = messages[0]
    recent = messages[1:]
    summary = state.get("campaign_summary", "")
    if len(recent) > 20:
        older = recent[:-20]
        recent = recent[-20:]
        older_summary = _summarize_messages(older)
        summary = older_summary if not summary else f"{summary}\n{older_summary}".strip()
    context: list[BaseMessage] = [system_message]
    if summary:
        context.append(HumanMessage(content=f"[CAMPAIGN SUMMARY]\n{summary}"))
    context.extend(recent)
    return context, summary


def _updated_campaign_summary(state: ChemBOState, new_messages: list[BaseMessage]) -> str:
    existing = state.get("campaign_summary", "")
    snippet = _summarize_messages(new_messages)
    if not snippet:
        return existing
    return f"{existing}\n{snippet}".strip() if existing else snippet


def _summarize_messages(messages: list[BaseMessage]) -> str:
    parts = []
    for message in messages:
        text = _message_text(message).strip()
        if not text:
            continue
        role = message.__class__.__name__.replace("Message", "")
        compact = " ".join(text.split())
        parts.append(f"{role}: {compact[:200]}")
    return "\n".join(parts[-8:])


def _extract_latest_tool_payload(messages: list[BaseMessage]) -> dict | None:
    for message in reversed(messages):
        if not isinstance(message, ToolMessage):
            continue
        payload = _extract_json_from_response(_message_text(message))
        if payload is not None:
            return payload
    return None


def _last_ai_content(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return _message_text(message)
    return ""


def _message_text(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(json.dumps(item))
        return "\n".join(parts)
    return str(content)


def _message_has_tool_calls(message: BaseMessage) -> bool:
    return bool(getattr(message, "tool_calls", None))


def _has_recent_tool_result(state: ChemBOState) -> bool:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, ToolMessage):
            return True
        if isinstance(message, HumanMessage):
            break
    return False


def _parse_human_response(human_response) -> tuple[float, str]:
    if isinstance(human_response, (int, float)):
        return float(human_response), ""
    if isinstance(human_response, dict):
        return float(human_response.get("result", 0.0)), str(human_response.get("notes", ""))
    if isinstance(human_response, str):
        try:
            return float(human_response), ""
        except ValueError:
            try:
                parsed = json.loads(human_response)
                return float(parsed.get("result", 0.0)), str(parsed.get("notes", ""))
            except (json.JSONDecodeError, ValueError):
                return 0.0, human_response
    return 0.0, str(human_response)


def _extract_json_from_response(text: str) -> dict | None:
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
            pass
    return None
