"""
Deterministic warm-start planning and phase-specific helpers.
"""
from __future__ import annotations

import hashlib
import math
from typing import Any, Callable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from core.autobo_engine import (
    _build_pure_reasoning_space_spec,
    _resolve_structured_pure_reasoning_candidate,
)
from core.context_builder import ContextBuilder
from core.dataset_oracle import DatasetOracle
from core.prompt_utils import compact_json
from core.problem_loader import resolve_campaign_budget
from core.state import CampaignPhase
from knowledge.knowledge_state import knowledge_mode_from_deck
from pools.component_pools import (
    candidate_to_key,
    enumerate_discrete_candidates,
    hybrid_sample_candidates,
)

def plan_warm_start(
    state: dict[str, Any],
    settings,
    llm_plain,
    *,
    invoke_tool_loop: Callable[..., tuple[list[BaseMessage], str, dict[str, Any]]],
    extract_last_json: Callable[[list[BaseMessage]], dict[str, Any] | None],
    state_messages: Callable[[list[BaseMessage]], list[BaseMessage]],
    updated_campaign_summary: Callable[[dict[str, Any], list[BaseMessage]], str],
    attach_llm_usage: Callable[[dict[str, Any], dict[str, Any], str, dict[str, Any]], None],
) -> dict[str, Any]:
    budget = resolve_campaign_budget(state.get("problem_spec", {}), settings)
    variables = state.get("problem_spec", {}).get("variables", [])
    oracle = DatasetOracle.from_problem_spec(state.get("problem_spec", {}))
    observed_keys = {
        candidate_to_key(item.get("candidate", {}))
        for item in state.get("observations", [])
        if item.get("candidate")
    }
    hard_constraints: list[dict[str, Any]] = []
    raw_target = _compute_warm_start_target(settings, budget)
    dataset_pool = _dataset_candidate_pool(oracle)

    feasible_pool = _build_feasible_warm_start_pool(
        variables=variables,
        desired_count=max(raw_target * 4, 80),
        seed=_state_seed(state),
        observed_keys=observed_keys,
        hard_constraints=hard_constraints,
        candidate_pool=dataset_pool,
    )
    warm_start_target = min(raw_target, len(feasible_pool))
    if warm_start_target <= 0:
        message = AIMessage(content="Warm-start skipped because no feasible unseen candidates were available.")
        return {
            "messages": state_messages([message]),
            "phase": CampaignPhase.WARM_STARTING.value,
            "proposal_shortlist": [],
            "warm_start_queue": [],
            "warm_start_target": 0,
            "warm_start_active": False,
            "_warm_start_postmortem_done": False,
            "campaign_summary": updated_campaign_summary(state, [message]),
            "llm_reasoning_log": state.get("llm_reasoning_log", []) + ["[warm_start] skipped=no_feasible_candidates"],
        }

    context = ContextBuilder.for_warm_start(state, warm_start_target)
    knowledge_mode = knowledge_mode_from_deck(state.get("knowledge_deck", {}))

    direct_target = int(math.ceil(warm_start_target / 2.0))
    direct_records, direct_messages, llm_usage, direct_metadata = _select_llm_direct_warm_start_records(
        state=state,
        settings=settings,
        llm_plain=llm_plain,
        context=context,
        target=direct_target,
        observed_keys=observed_keys,
        candidate_pool=feasible_pool,
        invoke_tool_loop=invoke_tool_loop,
        extract_last_json=extract_last_json,
    )
    direct_keys = {
        candidate_to_key(item.get("candidate", {}))
        for item in direct_records
        if item.get("candidate")
    }
    random_target = max(0, warm_start_target - len(direct_records))
    random_pool = (
        _build_random_warm_start_pool(
            variables=variables,
            pool_size=random_target,
            seed=_state_seed(state, offset=17),
            observed_keys=set(observed_keys) | direct_keys,
            hard_constraints=hard_constraints,
            candidate_pool=dataset_pool,
            initial_selected=[item.get("candidate", {}) for item in direct_records],
        )
        if random_target > 0
        else []
    )
    random_records = [
        _make_random_warm_start_record(candidate, index=index)
        for index, candidate in enumerate(random_pool[:random_target], start=1)
    ]
    shortlist = direct_records + random_records

    outbound_messages = list(direct_messages)
    if warm_start_target < raw_target:
        outbound_messages.append(
            AIMessage(
                content=(
                    f"Warm-start target reduced from {raw_target} to {warm_start_target} because only "
                    f"{len(feasible_pool)} feasible unseen candidate(s) were available."
                )
            )
        )

    updates = {
        "messages": state_messages(outbound_messages),
        "phase": CampaignPhase.WARM_STARTING.value,
        "proposal_shortlist": shortlist,
        "warm_start_queue": shortlist,
        "warm_start_target": len(shortlist),
        "warm_start_active": bool(shortlist),
        "_warm_start_postmortem_done": False,
        "campaign_summary": updated_campaign_summary(state, outbound_messages),
        "llm_reasoning_log": state.get("llm_reasoning_log", [])
        + [
            f"[warm_start] shortlist={len(shortlist)} target={warm_start_target} "
            f"direct={len(direct_records)}/{direct_target} random={len(random_records)}/{random_target} "
            f"pool={len(feasible_pool)} representation_mode={direct_metadata.get('representation_mode', 'unknown')} "
            f"knowledge_mode={knowledge_mode} strategy={direct_metadata.get('strategy_summary', '')[:120]}"
        ],
    }
    attach_llm_usage(updates, state, "warm_start", llm_usage)
    return updates


def interpret_warm_start_result(
    state: dict[str, Any],
    settings,
    llm_plain,
    *,
    memory_manager,
    build_context_messages: Callable[..., tuple[list[BaseMessage], str, dict[str, int]]],
    invoke_llm_with_tracking: Callable[..., tuple[BaseMessage, dict[str, Any]]],
    extract_json_from_response: Callable[[str], dict[str, Any] | None],
    message_text: Callable[[BaseMessage], str],
    state_messages: Callable[[list[BaseMessage]], list[BaseMessage]],
    updated_campaign_summary: Callable[[dict[str, Any], list[BaseMessage]], str],
    attach_llm_usage: Callable[[dict[str, Any], dict[str, Any], str, dict[str, Any]], None],
) -> dict[str, Any]:
    if not bool(getattr(settings, "warm_start_per_point_llm_interpret", False)):
        return _interpret_warm_start_no_llm(
            state,
            memory_manager=memory_manager,
            state_messages=state_messages,
            updated_campaign_summary=updated_campaign_summary,
        )

    latest = state.get("observations", [])[-1] if state.get("observations") else {}
    prompt = f"""Briefly interpret this warm-start experiment result in one sentence.

Candidate:
{compact_json(latest.get("candidate", {}))}

Result: {latest.get("result")}
Best so far: {state.get("best_result")}

Return strict JSON:
{{
  "interpretation": "...",
  "supported_hypotheses": [],
  "refuted_hypotheses": [],
  "archived_hypotheses": [],
  "episodic_memory": {{
    "reflection": "...",
    "lesson_learned": "",
    "non_numerical_observations": "",
    "causal_attributions": [],
    "hypothesis_evidence": [],
    "knowledge_tension": {{
      "has_conflict": false,
      "conflicting_priors": [],
      "conflicting_cards": [],
      "reason": ""
    }}
  }},
  "semantic_rule": null,
  "working_memory": {{
    "current_focus": "Collecting warm-start data.",
    "pending_decisions": []
  }}
}}"""
    context_messages, _, _ = build_context_messages(
        state,
        node_name="interpret_results",
        recent_message_limits=getattr(settings, "memory_recent_message_limits", None),
        inject_campaign_summary=bool(getattr(settings, "inject_campaign_summary_in_context", False)),
    )
    response, llm_usage = invoke_llm_with_tracking(llm_plain, context_messages + [HumanMessage(content=prompt)])
    messages: list[BaseMessage] = [HumanMessage(content=prompt), response]
    parsed = extract_json_from_response(message_text(response)) or {
        "interpretation": "Warm-start result recorded.",
        "supported_hypotheses": [],
        "refuted_hypotheses": [],
        "archived_hypotheses": [],
        "episodic_memory": {
            "reflection": "Warm-start observation logged.",
            "lesson_learned": "",
            "non_numerical_observations": "",
            "causal_attributions": [],
            "hypothesis_evidence": [],
            "knowledge_tension": {
                "has_conflict": False,
                "conflicting_priors": [],
                "conflicting_cards": [],
                "reason": "",
            },
        },
        "semantic_rule": None,
        "working_memory": {"current_focus": "Collecting warm-start data.", "pending_decisions": []},
    }
    write_result = memory_manager.record_result(state, parsed)

    updates = {
        "messages": state_messages(messages),
        "phase": CampaignPhase.INTERPRETING.value,
        "memory": memory_manager.to_dict(),
        "campaign_summary": updated_campaign_summary(state, messages),
        "llm_reasoning_log": state.get("llm_reasoning_log", [])
        + [f"[interpret_results:lightweight] {parsed.get('interpretation', '')[:120]}"]
        + [f"[memory] trigger={write_result.recommended_trigger} notes={'; '.join(write_result.notes[:2])}"],
    }
    attach_llm_usage(updates, state, "interpret_results", llm_usage)
    return updates


def _interpret_warm_start_no_llm(
    state: dict[str, Any],
    *,
    memory_manager,
    state_messages: Callable[[list[BaseMessage]], list[BaseMessage]],
    updated_campaign_summary: Callable[[dict[str, Any], list[BaseMessage]], str],
) -> dict[str, Any]:
    latest = state.get("observations", [])[-1] if state.get("observations") else {}
    payload = {
        "interpretation": f"Warm-start result recorded: {latest.get('result')}",
        "supported_hypotheses": [],
        "refuted_hypotheses": [],
        "archived_hypotheses": [],
        "reflection": "Warm-start observation logged.",
        "knowledge_conflict": {
            "has_conflict": False,
            "conflicting_priors": [],
            "conflicting_cards": [],
            "reason": "",
        },
        "working_focus": "Collecting warm-start data.",
    }
    write_result = memory_manager.record_result(state, payload)
    message = AIMessage(content="Warm-start result recorded without per-point LLM interpretation.")
    return {
        "messages": state_messages([message]),
        "phase": CampaignPhase.INTERPRETING.value,
        "memory": memory_manager.to_dict(),
        "campaign_summary": updated_campaign_summary(state, [message]),
        "llm_reasoning_log": state.get("llm_reasoning_log", [])
        + [f"[interpret_results:warm_start_light] {payload['interpretation'][:120]}"]
        + [f"[memory] trigger={write_result.recommended_trigger} notes={'; '.join(write_result.notes[:2])}"],
    }


def run_warm_start_postmortem(
    state: dict[str, Any],
    settings,
    llm_thinking,
    memory_llm_adapter,
    *,
    memory_manager,
    build_context_messages: Callable[..., tuple[list[BaseMessage], str, dict[str, int]]],
    invoke_llm_with_tracking: Callable[..., tuple[BaseMessage, dict[str, Any]]],
    extract_json_from_response: Callable[[str], dict[str, Any] | None],
    message_text: Callable[[BaseMessage], str],
    compute_convergence_state: Callable[[dict[str, Any], Any], dict[str, Any]],
    update_hypothesis_statuses: Callable[..., list[dict[str, Any]]],
    merge_llm_usage: Callable[[dict[str, Any], str, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    warm_start_observations = [
        item
        for item in state.get("observations", [])
        if str((item.get("metadata") or {}).get("selection_source", "")) == "warm_start_queue"
    ]
    prompt = f"""Review the complete warm-start experimental results and extract key patterns.

WARM_START_OBSERVATIONS ({len(warm_start_observations)} experiments):
{compact_json(warm_start_observations)}

HYPOTHESES:
{compact_json(state.get("hypotheses", []))}

Return strict JSON:
{{
  "batch_interpretation": "...",
  "supported_hypotheses": ["H1"],
  "refuted_hypotheses": [],
  "key_patterns": ["..."],
  "semantic_rules": [
    {{
      "rule_type": "chemical_effect",
      "statement": "...",
      "variables": ["..."],
      "conditions": {{}},
      "confidence": 0.0
    }}
  ]
}}"""
    context_messages, _, _ = build_context_messages(
        state,
        node_name="interpret_results",
        recent_message_limits=getattr(settings, "memory_recent_message_limits", None),
        inject_campaign_summary=bool(getattr(settings, "inject_campaign_summary_in_context", False)),
    )
    response, llm_usage = invoke_llm_with_tracking(llm_thinking, context_messages + [HumanMessage(content=prompt)])
    parsed = extract_json_from_response(message_text(response)) or {
        "batch_interpretation": "Warm-start phase complete.",
        "supported_hypotheses": [],
        "refuted_hypotheses": [],
        "key_patterns": [],
        "semantic_rules": [],
    }

    added_rule_count = 0
    for rule_payload in parsed.get("semantic_rules", []):
        if not isinstance(rule_payload, dict) or not str(rule_payload.get("statement") or "").strip():
            continue
        memory_manager.add_semantic_rule(
            {
                **rule_payload,
                "source": "warm_start_postmortem",
                "created_at_iteration": int(state.get("iteration", 0) or 0),
                "last_validated": int(state.get("iteration", 0) or 0),
            }
        )
        added_rule_count += 1

    maintenance_state = dict(state)
    maintenance_state["memory"] = memory_manager.to_dict()
    maintenance_state["convergence_state"] = compute_convergence_state(maintenance_state, settings)
    maintenance_report = memory_manager.run_maintenance(
        maintenance_state,
        trigger="milestone",
        llm_adapter=memory_llm_adapter,
    )
    hypotheses = update_hypothesis_statuses(
        state.get("hypotheses", []),
        parsed.get("supported_hypotheses", []),
        parsed.get("refuted_hypotheses", []),
        [],
        int(state.get("iteration", 0) or 0),
    )
    combined_usage = llm_usage
    if int((maintenance_report.llm_usage or {}).get("calls", 0)) > 0:
        combined_usage = merge_llm_usage({"by_node": {}}, "interpret_results", llm_usage)
        combined_usage = merge_llm_usage(combined_usage, "memory_consolidation", maintenance_report.llm_usage)

    return {
        "memory": memory_manager.to_dict(),
        "hypotheses": hypotheses,
        "llm_usage": combined_usage,
        "maintenance_report": maintenance_report,
        "state_updates": dict(maintenance_report.state_updates),
        "batch_interpretation": str(parsed.get("batch_interpretation") or "").strip(),
        "added_rule_count": added_rule_count,
    }


def _compute_warm_start_target(settings, budget: int) -> int:
    ratio_cap = max(1, math.floor(int(budget or 0) * float(getattr(settings, "warm_start_budget_ratio", 0.5) or 0.5)))
    return max(0, min(int(getattr(settings, "initial_doe_size", 0) or 0), int(budget or 0), ratio_cap))


def _state_seed(state: dict[str, Any], *, offset: int = 0) -> int:
    return int(state.get("random_seed_base", 0) or 0) + int(state.get("iteration", 0) or 0) + int(offset or 0)


def _select_llm_direct_warm_start_records(
    *,
    state: dict[str, Any],
    settings,
    llm_plain,
    context: dict[str, Any],
    target: int,
    observed_keys: set[str],
    candidate_pool: list[dict[str, Any]],
    invoke_tool_loop: Callable[..., tuple[list[BaseMessage], str, dict[str, Any]]],
    extract_last_json: Callable[[list[BaseMessage]], dict[str, Any] | None],
) -> tuple[list[dict[str, Any]], list[BaseMessage], dict[str, Any], dict[str, Any]]:
    if target <= 0:
        return [], [], _empty_usage_delta(), {
            "representation_mode": "none",
            "strategy_summary": "",
        }

    structured_spec = _build_pure_reasoning_space_spec(state)
    if structured_spec is None:
        return _select_llm_direct_warm_start_from_candidate_pool(
            state=state,
            settings=settings,
            llm_plain=llm_plain,
            context=context,
            target=target,
            observed_keys=observed_keys,
            candidate_pool=candidate_pool,
            invoke_tool_loop=invoke_tool_loop,
            extract_last_json=extract_last_json,
        )

    records: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    all_messages: list[BaseMessage] = []
    total_usage = _empty_usage_delta()
    validation_feedback = ""
    strategy_summary = ""
    failures: list[str] = []

    for _attempt in range(2):
        remaining = max(0, target - len(records))
        if remaining <= 0:
            break
        prompt = _build_warm_start_direct_structured_prompt(
            context=context,
            structured_spec=structured_spec,
            target=remaining,
            total_direct_target=target,
            validation_feedback=validation_feedback,
            accepted_records=records,
        )
        messages, _, usage = invoke_tool_loop(
            llm_plain,
            state,
            prompt,
            tool_map={},
            max_turns=2,
            node_name="warm_start",
            recent_message_limits=getattr(settings, "memory_recent_message_limits", None),
        )
        all_messages.extend(messages)
        total_usage = _accumulate_usage_delta(total_usage, usage)
        parsed = extract_last_json(messages) or _default_direct_warm_start_response(structured_spec, remaining)
        strategy_summary = str(parsed.get("strategy_summary") or strategy_summary or "").strip()
        selections = _extract_direct_selection_payloads(parsed)
        failures = []
        if len(selections) < remaining:
            failures.append(f"Expected {remaining} recommendation(s), got {len(selections)}.")
        for selection_index, selection in enumerate(selections, start=1):
            candidate, failure_reason = _resolve_structured_pure_reasoning_candidate(
                selection,
                structured_spec=structured_spec,
                state=state,
            )
            if candidate is None:
                failures.append(f"Selection {selection_index}: {failure_reason or 'invalid structured recommendation'}")
                continue
            key = candidate_to_key(candidate)
            if key in selected_keys:
                failures.append(f"Selection {selection_index}: duplicates another direct warm-start recommendation.")
                continue
            if key in observed_keys:
                failures.append(f"Selection {selection_index}: repeats an already observed experiment.")
                continue
            records.append(
                _make_llm_direct_warm_start_record(
                    candidate,
                    selection,
                    index=len(records) + 1,
                    representation_mode=str(structured_spec.get("mode") or "structured_space"),
                )
            )
            selected_keys.add(key)
            if len(records) >= target:
                break
        if len(records) >= target:
            break
        validation_feedback = _build_direct_validation_feedback(
            failures=failures,
            accepted_records=records,
            remaining=target - len(records),
        )

    return records[:target], all_messages, total_usage, {
        "representation_mode": str(structured_spec.get("mode") or "structured_space"),
        "strategy_summary": strategy_summary,
        "direct_failures": failures,
    }


def _select_llm_direct_warm_start_from_candidate_pool(
    *,
    state: dict[str, Any],
    settings,
    llm_plain,
    context: dict[str, Any],
    target: int,
    observed_keys: set[str],
    candidate_pool: list[dict[str, Any]],
    invoke_tool_loop: Callable[..., tuple[list[BaseMessage], str, dict[str, Any]]],
    extract_last_json: Callable[[list[BaseMessage]], dict[str, Any] | None],
) -> tuple[list[dict[str, Any]], list[BaseMessage], dict[str, Any], dict[str, Any]]:
    prompt_candidates = [
        {"id": index + 1, "candidate": dict(candidate)}
        for index, candidate in enumerate(candidate_pool[: max(target * 8, 32)])
    ]
    if not prompt_candidates:
        return [], [], _empty_usage_delta(), {
            "representation_mode": "candidate_pool_fallback",
            "strategy_summary": "",
        }
    records: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    all_messages: list[BaseMessage] = []
    total_usage = _empty_usage_delta()
    validation_feedback = ""
    strategy_summary = ""
    failures: list[str] = []
    candidate_by_id = {int(item["id"]): dict(item["candidate"]) for item in prompt_candidates}

    for _attempt in range(2):
        remaining = max(0, target - len(records))
        if remaining <= 0:
            break
        prompt = _build_warm_start_direct_candidate_pool_prompt(
            context=context,
            candidates=prompt_candidates,
            target=remaining,
            total_direct_target=target,
            validation_feedback=validation_feedback,
            accepted_records=records,
        )
        messages, _, usage = invoke_tool_loop(
            llm_plain,
            state,
            prompt,
            tool_map={},
            max_turns=2,
            node_name="warm_start",
            recent_message_limits=getattr(settings, "memory_recent_message_limits", None),
        )
        all_messages.extend(messages)
        total_usage = _accumulate_usage_delta(total_usage, usage)
        parsed = extract_last_json(messages) or {"selected_ids": []}
        strategy_summary = str(parsed.get("strategy_summary") or strategy_summary or "").strip()
        selected_ids = _extract_direct_selected_ids(parsed)
        failures = []
        if len(selected_ids) < remaining:
            failures.append(f"Expected {remaining} candidate id(s), got {len(selected_ids)}.")
        for raw_id in selected_ids:
            selected_id = _coerce_int(raw_id, default=-1)
            candidate = candidate_by_id.get(selected_id)
            if candidate is None:
                failures.append(f"Candidate id {raw_id} is not in the compact fallback pool.")
                continue
            key = candidate_to_key(candidate)
            if key in observed_keys:
                failures.append(f"Candidate id {raw_id} repeats an already observed experiment.")
                continue
            if key in selected_keys:
                failures.append(f"Candidate id {raw_id} duplicates another direct warm-start recommendation.")
                continue
            records.append(
                _make_llm_direct_warm_start_record(
                    candidate,
                    {
                        "reasoning": _reason_for_direct_id(parsed, selected_id),
                        "confidence": parsed.get("confidence", 0.6),
                        "information_value": "",
                        "concerns": "",
                    },
                    index=len(records) + 1,
                    representation_mode="candidate_pool_fallback",
                )
            )
            selected_keys.add(key)
            if len(records) >= target:
                break
        if len(records) >= target:
            break
        validation_feedback = _build_direct_validation_feedback(
            failures=failures,
            accepted_records=records,
            remaining=target - len(records),
        )

    return records[:target], all_messages, total_usage, {
        "representation_mode": "candidate_pool_fallback",
        "strategy_summary": strategy_summary,
        "direct_failures": failures,
    }


def _build_warm_start_direct_structured_prompt(
    *,
    context: dict[str, Any],
    structured_spec: dict[str, Any],
    target: int,
    total_direct_target: int,
    validation_feedback: str,
    accepted_records: list[dict[str, Any]],
) -> str:
    knowledge_cards_text = str(context.get("knowledge_cards_text") or "")
    compact_context = {key: value for key, value in context.items() if key not in {"knowledge_cards_text", "knowledge_cards"}}
    validation_section = _validation_feedback_section(validation_feedback, accepted_records)
    return f"""Select high-value direct warm-start experiments for a chemical optimization campaign.

CONTEXT:
{compact_json(compact_context)}

{knowledge_cards_text}
{validation_section}

STRUCTURED SEARCH SPACE:
Choose directly from the full legal search space below. If categorical options are represented by IDs, return those IDs exactly.

{structured_spec.get("space_description", "")}

Task:
- Return exactly {target} new direct warm-start recommendation(s); this is part of a total direct LLM allocation of {total_direct_target}.
- Prioritize the experiments that look most valuable to run early from chemical reasoning.
- You may consider diversity between recommendations, but high expected value is more important than forced diversity.
- Do not refer to BO, surrogate predictions, acquisition scores, or ranked planner indices.
- Every recommendation must be legal, unseen, and non-duplicate.

Each item in "selections" must follow this single-experiment schema:
{structured_spec.get("output_schema", "{}")}

Return strict JSON:
{{
  "strategy_summary": "...",
  "selections": [
    {structured_spec.get("output_schema", "{}")}
  ]
}}"""


def _build_warm_start_direct_candidate_pool_prompt(
    *,
    context: dict[str, Any],
    candidates: list[dict[str, Any]],
    target: int,
    total_direct_target: int,
    validation_feedback: str,
    accepted_records: list[dict[str, Any]],
) -> str:
    knowledge_cards_text = str(context.get("knowledge_cards_text") or "")
    compact_context = {key: value for key, value in context.items() if key not in {"knowledge_cards_text", "knowledge_cards"}}
    validation_section = _validation_feedback_section(validation_feedback, accepted_records)
    return f"""Select high-value direct warm-start experiments for a chemical optimization campaign.

CONTEXT:
{compact_json(compact_context)}

{knowledge_cards_text}
{validation_section}

COMPACT LEGAL CANDIDATE POOL:
The full search space could not be represented compactly, so use this diverse legal fallback pool.
{compact_json(candidates)}

Task:
- Return exactly {target} candidate id(s); this is part of a total direct LLM allocation of {total_direct_target}.
- Prioritize the experiments that look most valuable to run early from chemical reasoning.
- Diversity is useful but not mandatory.
- Do not refer to BO, surrogate predictions, acquisition scores, or ranked planner indices.

Return strict JSON:
{{
  "strategy_summary": "...",
  "selected_ids": [1, 2],
  "reasoning_by_id": {{"1": "...", "2": "..."}},
  "confidence": 0.6
}}"""


def _validation_feedback_section(validation_feedback: str, accepted_records: list[dict[str, Any]]) -> str:
    accepted = [item.get("candidate", {}) for item in accepted_records if item.get("candidate")]
    parts: list[str] = []
    if accepted:
        parts.append("[Already Accepted Direct Warm-Start Recommendations]\n" + compact_json(accepted))
    if str(validation_feedback or "").strip():
        parts.append("[Validation Feedback]\n" + str(validation_feedback).strip())
    return ("\n\n" + "\n\n".join(parts) + "\n") if parts else ""


def _default_direct_warm_start_response(structured_spec: dict[str, Any], target: int) -> dict[str, Any]:
    default_selection = dict(structured_spec.get("default_response", {}))
    return {
        "strategy_summary": "Use the structured search-space default when no valid direct response is available.",
        "selections": [dict(default_selection) for _ in range(max(0, target))],
    }


def _extract_direct_selection_payloads(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("selections", "candidates", "warm_start_points", "recommendations"):
        raw = parsed.get(key)
        if isinstance(raw, list):
            return [dict(item) for item in raw if isinstance(item, dict)]
    if isinstance(parsed.get("variables"), dict):
        return [dict(parsed)]
    return []


def _extract_direct_selected_ids(parsed: dict[str, Any]) -> list[Any]:
    for key in ("selected_ids", "candidate_ids", "ids"):
        raw = parsed.get(key)
        if isinstance(raw, list):
            return list(raw)
    selections = parsed.get("selections")
    if isinstance(selections, list):
        return [item.get("id") for item in selections if isinstance(item, dict)]
    selected_id = parsed.get("selected_id")
    return [selected_id] if selected_id is not None else []


def _reason_for_direct_id(parsed: dict[str, Any], selected_id: int) -> str:
    reasoning_by_id = parsed.get("reasoning_by_id", {})
    if isinstance(reasoning_by_id, dict):
        reason = reasoning_by_id.get(str(selected_id), reasoning_by_id.get(selected_id))
        if str(reason or "").strip():
            return str(reason).strip()
    return str(parsed.get("reasoning") or "Selected directly by the LLM from the compact warm-start pool.").strip()


def _build_direct_validation_feedback(
    *,
    failures: list[str],
    accepted_records: list[dict[str, Any]],
    remaining: int,
) -> str:
    accepted_text = compact_json([item.get("candidate", {}) for item in accepted_records if item.get("candidate")])
    failure_text = "; ".join(str(item).strip() for item in failures if str(item).strip())
    return (
        f"{failure_text or 'Not enough valid direct recommendations were produced.'} "
        f"Already accepted: {accepted_text}. Return {max(0, remaining)} additional non-duplicate legal unseen recommendation(s)."
    )


def _make_llm_direct_warm_start_record(
    candidate: dict[str, Any],
    selection: dict[str, Any],
    *,
    index: int,
    representation_mode: str,
) -> dict[str, Any]:
    rationale = str(selection.get("reasoning") or "Selected directly by the LLM as a high-value warm-start point.").strip()
    return {
        "candidate": dict(candidate),
        "predicted_value": None,
        "uncertainty": None,
        "acquisition_value": None,
        "constraint_violations": [],
        "constraint_satisfied": True,
        "warm_start_category": "llm_direct",
        "warm_start_rationale": rationale,
        "warm_start_card_refs": _normalize_card_refs(selection.get("knowledge_card_ids", [])),
        "warm_start_index": int(index),
        "warm_start_representation_mode": representation_mode,
        "warm_start_confidence": _coerce_float(selection.get("confidence"), default=0.6),
        "warm_start_information_value": str(selection.get("information_value") or "").strip(),
        "warm_start_concerns": str(selection.get("concerns") or "").strip(),
    }


def _make_random_warm_start_record(candidate: dict[str, Any], *, index: int) -> dict[str, Any]:
    return {
        "candidate": dict(candidate),
        "predicted_value": None,
        "uncertainty": None,
        "acquisition_value": None,
        "constraint_violations": [],
        "constraint_satisfied": True,
        "warm_start_category": "random",
        "warm_start_rationale": "Selected by seeded full-space random exploration after the LLM direct warm-start picks.",
        "warm_start_card_refs": [],
        "warm_start_index": int(index),
    }


def _build_random_warm_start_pool(
    variables: list[dict[str, Any]],
    *,
    pool_size: int,
    seed: int,
    observed_keys: set[str],
    hard_constraints: list[dict[str, Any]],
    candidate_pool: list[dict[str, Any]] | None,
    initial_selected: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    target_size = max(0, int(pool_size or 0))
    if target_size <= 0:
        return []
    selected_keys = {
        candidate_to_key(candidate)
        for candidate in (initial_selected or [])
        if isinstance(candidate, dict) and candidate
    }
    feasible = _build_feasible_warm_start_pool(
        variables=variables,
        desired_count=target_size,
        seed=seed,
        observed_keys=set(observed_keys) | selected_keys,
        hard_constraints=hard_constraints,
        candidate_pool=candidate_pool,
    )
    return feasible[:target_size]


def _build_feasible_warm_start_pool(
    *,
    variables: list[dict[str, Any]],
    desired_count: int,
    seed: int,
    observed_keys: set[str],
    hard_constraints: list[dict[str, Any]],
    candidate_pool: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    target_size = max(0, int(desired_count or 0))
    if target_size <= 0:
        return []

    if candidate_pool is not None:
        feasible = _filter_feasible_candidates(
            candidate_pool,
            observed_keys=observed_keys,
            hard_constraints=hard_constraints,
        )
        return _deterministic_candidate_order(feasible, seed)

    discrete_candidates = enumerate_discrete_candidates(variables, max_candidates=4096)
    if discrete_candidates:
        feasible = _filter_feasible_candidates(
            discrete_candidates,
            observed_keys=observed_keys,
            hard_constraints=hard_constraints,
        )
        return _deterministic_candidate_order(feasible, seed)

    collected: list[dict[str, Any]] = []
    seen = set(observed_keys or set())
    batch_size = max(target_size * 12, 512)
    max_attempts = 6
    for attempt in range(max_attempts):
        sampled = hybrid_sample_candidates(variables, batch_size, seed=seed + 997 * attempt)
        for candidate in sampled:
            key = candidate_to_key(candidate)
            if key in seen or _candidate_violates_hard_constraints(candidate, hard_constraints):
                continue
            seen.add(key)
            collected.append(dict(candidate))
        if len(collected) >= target_size:
            break
        batch_size *= 2
    return _deterministic_candidate_order(collected, seed)


def _filter_feasible_candidates(
    candidates: list[dict[str, Any]],
    *,
    observed_keys: set[str],
    hard_constraints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    feasible: list[dict[str, Any]] = []
    seen = set(observed_keys or set())
    for candidate in candidates:
        key = candidate_to_key(candidate)
        if key in seen or _candidate_violates_hard_constraints(candidate, hard_constraints):
            continue
        seen.add(key)
        feasible.append(dict(candidate))
    return feasible


def _deterministic_candidate_order(candidates: list[dict[str, Any]], seed: int) -> list[dict[str, Any]]:
    return sorted(
        (dict(candidate) for candidate in candidates),
        key=lambda candidate: hashlib.sha256(f"{seed}:{candidate_to_key(candidate)}".encode("utf-8")).hexdigest(),
    )


def _normalize_card_refs(values: Any) -> list[str]:
    raw_values = values if isinstance(values, list) else [values]
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        value = str(raw).strip()
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return normalized


def _candidate_violates_hard_constraints(
    candidate: dict[str, Any],
    hard_constraints: list[dict[str, Any]],
) -> bool:
    return any(not constraint.get("check", lambda _: True)(candidate) for constraint in hard_constraints)


def _dataset_candidate_pool(oracle: DatasetOracle | None) -> list[dict[str, Any]] | None:
    if oracle is None:
        return None
    return [dict(candidate) for candidate in oracle.candidates]


def _coerce_finite_float(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


def _coerce_float(value: Any, default: float) -> float:
    numeric = _coerce_finite_float(value)
    return float(default) if numeric is None else float(numeric)


def _coerce_int(value: Any, default: int) -> int:
    try:
        if value is None or isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


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
    incoming = dict(addition or _empty_usage_delta())
    for key in ("calls", "input_tokens", "output_tokens", "total_tokens", "estimated_calls"):
        merged[key] = int(merged.get(key, 0) or 0) + int(incoming.get(key, 0) or 0)
    merged["estimated"] = bool(merged.get("estimated_calls", 0))
    return merged


__all__ = [
    "plan_warm_start",
    "interpret_warm_start_result",
    "run_warm_start_postmortem",
    "_build_random_warm_start_pool",
]
