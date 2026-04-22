"""
Deterministic warm-start planning and phase-specific helpers.
"""
from __future__ import annotations

import json
import math
from typing import Any, Callable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import tool

from core.context_builder import ContextBuilder
from core.dataset_oracle import DatasetOracle
from core.prompt_utils import compact_json
from core.problem_loader import resolve_campaign_budget
from core.state import CampaignPhase
from knowledge.knowledge_state import knowledge_mode_for_node, score_candidate_with_priors
from pools.component_pools import (
    build_doe_pool,
    candidate_distance,
    candidate_to_key,
    enumerate_discrete_candidates,
    hybrid_sample_candidates,
)

CategoryName = str
DEFAULT_CATEGORY_RATIOS: dict[CategoryName, float] = {
    "exploration": 0.40,
    "balanced": 0.35,
    "exploitation": 0.25,
}
CATEGORY_ORDER = ["exploration", "balanced", "exploitation"]
SCORING_WEIGHTS: dict[CategoryName, dict[str, float]] = {
    "exploration": {
        "coverage_gain": 1.4,
        "diversity_gain": 1.2,
        "knowledge_bias_score": 0.4,
        "llm_pattern_score": 0.4,
        "priority_index_bonus": 0.15,
    },
    "balanced": {
        "coverage_gain": 1.0,
        "diversity_gain": 0.9,
        "knowledge_bias_score": 0.8,
        "llm_pattern_score": 0.8,
        "priority_index_bonus": 0.20,
    },
    "exploitation": {
        "coverage_gain": 0.4,
        "diversity_gain": 0.4,
        "knowledge_bias_score": 1.4,
        "llm_pattern_score": 1.2,
        "priority_index_bonus": 0.35,
    },
}


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
    doe_pool = build_doe_pool(
        variables,
        pool_size=max(raw_target * 3, 60),
        seed=int(state.get("iteration", 0) or 0),
        observed_keys=observed_keys,
        hard_constraints=hard_constraints,
        candidate_pool=_dataset_candidate_pool(oracle),
    )
    warm_start_target = min(raw_target, len(doe_pool))
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
    valid_card_ids = {
        str(item.get("card_id") or item.get("prior_id") or item.get("reference_id") or "").strip()
        for item in context.get("knowledge_guidance", [])
        if str(item.get("card_id") or item.get("prior_id") or item.get("reference_id") or "").strip()
    }
    knowledge_state = state.get("knowledge_state", {}) if isinstance(state.get("knowledge_state"), dict) else {}
    coverage_report = knowledge_state.get("coverage_report", {}) if isinstance(knowledge_state.get("coverage_report"), dict) else {}
    served_priors = knowledge_state.get("served_priors", []) if isinstance(knowledge_state.get("served_priors"), list) else []
    knowledge_mode = knowledge_mode_for_node(coverage_report, served_priors, node_name="warm_start")

    search_tool = _build_warm_start_candidate_search_tool(
        variables=variables,
        observed_keys=observed_keys,
        hard_constraints=hard_constraints,
        oracle=oracle,
        seed=int(state.get("iteration", 0) or 0),
    )
    warm_start_llm = llm_plain.bind_tools([search_tool])
    default_guidance = _default_guidance(target=warm_start_target)
    prompt = _build_warm_start_guidance_prompt(
        context=context,
        doe_pool=doe_pool,
        target=warm_start_target,
    )
    messages, _, llm_usage = invoke_tool_loop(
        warm_start_llm,
        state,
        prompt,
        tool_map={search_tool.name: search_tool},
        max_turns=max(8, warm_start_target // 2 + 4),
        node_name="warm_start",
        recent_message_limits=getattr(settings, "memory_recent_message_limits", None),
    )
    parsed = extract_last_json(messages) or default_guidance
    guidance = _normalize_llm_guidance(parsed, target=warm_start_target, valid_card_ids=valid_card_ids)

    shortlist = _select_warm_start_shortlist(
        doe_pool=doe_pool,
        variables=variables,
        target=warm_start_target,
        knowledge_guidance=context.get("knowledge_guidance", []),
        llm_guidance=guidance,
        knowledge_priors=state.get("kb_priors", {}) or {},
        served_priors=served_priors,
        knowledge_mode=knowledge_mode,
    )
    shortlist = _sort_warm_start_queue(shortlist, variables)

    outbound_messages = list(messages)
    if warm_start_target < raw_target:
        outbound_messages.append(
            AIMessage(
                content=(
                    f"Warm-start target reduced from {raw_target} to {warm_start_target} because only "
                    f"{len(doe_pool)} feasible unseen candidate(s) were available after budget and feasibility checks."
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
        "knowledge_serving_stats": {
            **(state.get("knowledge_serving_stats", {}) or {}),
            "warm_start_knowledge_mode": knowledge_mode,
            "warm_start_applied_prior_count": len(
                {
                    prior_id
                    for item in shortlist
                    for prior_id in item.get("applied_prior_ids", [])
                }
            ),
        },
        "_warm_start_postmortem_done": False,
        "campaign_summary": updated_campaign_summary(state, outbound_messages),
        "llm_reasoning_log": state.get("llm_reasoning_log", [])
        + [
            f"[warm_start] shortlist={len(shortlist)} target={warm_start_target} "
            f"pool={len(doe_pool)} knowledge_mode={knowledge_mode} strategy={guidance.get('strategy_summary', '')[:120]}"
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


def _build_warm_start_guidance_prompt(
    *,
    context: dict[str, Any],
    doe_pool: list[dict[str, Any]],
    target: int,
) -> str:
    pool_summary = [
        {
            "index": index,
            "candidate": candidate,
        }
        for index, candidate in enumerate(doe_pool)
    ]
    return f"""Design deterministic guidance for the warm-start planner.

CONTEXT:
{compact_json(context)}

DOE_POOL:
{compact_json(pool_summary)}

Rules:
- You are NOT selecting final candidates directly. The deterministic planner will do that.
- Recommend patterns, value preferences, and bucket targets that help the planner balance coverage and chemistry-guided exploitation.
- The provided knowledge_guidance items may be served priors or evidence digests. Use knowledge_card_ids only as references to those provided ids.
- If the DoE pool is large, you may call warm_start_candidate_search to inspect specific regions before responding.
- Keep priority_indices short and focused; they are soft hints only.

Return strict JSON:
{{
  "strategy_summary": "...",
  "preferred_patterns": [
    {{
      "variable": "...",
      "preferred_values": ["..."],
      "weight": 1.0,
      "reason": "...",
      "knowledge_card_ids": ["kc_..."]
    }}
  ],
  "avoided_patterns": [
    {{
      "variable": "...",
      "avoided_values": ["..."],
      "weight": 1.0,
      "reason": "...",
      "knowledge_card_ids": ["kc_..."]
    }}
  ],
  "category_targets": {{
    "exploration": {max(1, int(round(target * DEFAULT_CATEGORY_RATIOS["exploration"])))},
    "balanced": {max(1, int(round(target * DEFAULT_CATEGORY_RATIOS["balanced"])))},
    "exploitation": {max(1, int(round(target * DEFAULT_CATEGORY_RATIOS["exploitation"])))}
  }},
  "priority_indices": [0, 1, 2]
}}"""


def _default_guidance(target: int) -> dict[str, Any]:
    return {
        "strategy_summary": "Use a deterministic warm-start plan that emphasizes coverage first, then knowledge-guided balance.",
        "preferred_patterns": [],
        "avoided_patterns": [],
        "category_targets": _normalize_category_targets({}, target),
        "priority_indices": [],
    }


def _normalize_llm_guidance(
    payload: dict[str, Any],
    *,
    target: int,
    valid_card_ids: set[str],
) -> dict[str, Any]:
    return {
        "strategy_summary": str(payload.get("strategy_summary") or _default_guidance(target)["strategy_summary"]).strip(),
        "preferred_patterns": _normalize_pattern_entries(
            payload.get("preferred_patterns", []),
            value_key="preferred_values",
            valid_card_ids=valid_card_ids,
        ),
        "avoided_patterns": _normalize_pattern_entries(
            payload.get("avoided_patterns", []),
            value_key="avoided_values",
            valid_card_ids=valid_card_ids,
        ),
        "category_targets": _normalize_category_targets(payload.get("category_targets", {}), target),
        "priority_indices": _normalize_priority_indices(payload.get("priority_indices", [])),
    }


def _normalize_pattern_entries(
    payload: Any,
    *,
    value_key: str,
    valid_card_ids: set[str],
) -> list[dict[str, Any]]:
    patterns = payload if isinstance(payload, list) else []
    normalized: list[dict[str, Any]] = []
    for entry in patterns:
        if not isinstance(entry, dict):
            continue
        variable = str(entry.get("variable") or "").strip()
        if not variable:
            continue
        raw_values = entry.get(value_key, [])
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        normalized_values = [str(value).strip() for value in values if str(value).strip()]
        if not normalized_values:
            continue
        normalized.append(
            {
                "variable": variable,
                value_key: normalized_values,
                "weight": max(0.0, float(entry.get("weight", 1.0) or 1.0)),
                "reason": str(entry.get("reason") or "").strip(),
                "knowledge_card_ids": [
                    card_id
                    for card_id in _normalize_card_refs(entry.get("knowledge_card_ids", []))
                    if card_id in valid_card_ids
                ],
            }
        )
    return normalized


def _normalize_category_targets(payload: Any, target: int) -> dict[str, int]:
    raw = payload if isinstance(payload, dict) else {}
    weights = {
        category: max(0.0, float(raw.get(category, DEFAULT_CATEGORY_RATIOS[category]) or DEFAULT_CATEGORY_RATIOS[category]))
        for category in CATEGORY_ORDER
    }
    total_weight = sum(weights.values())
    if total_weight <= 0:
        weights = dict(DEFAULT_CATEGORY_RATIOS)
        total_weight = sum(weights.values())

    base: dict[str, int] = {}
    remainders: list[tuple[float, str]] = []
    assigned = 0
    for category in CATEGORY_ORDER:
        exact = target * weights[category] / total_weight if target > 0 else 0.0
        count = int(math.floor(exact))
        base[category] = count
        assigned += count
        remainders.append((exact - count, category))

    for _fraction, category in sorted(remainders, reverse=True):
        if assigned >= target:
            break
        base[category] += 1
        assigned += 1

    return base


def _normalize_priority_indices(payload: Any) -> list[int]:
    raw_values = payload if isinstance(payload, list) else [payload]
    seen: set[int] = set()
    normalized: list[int] = []
    for raw in raw_values:
        value = _coerce_int(raw, default=-1)
        if value < 0 or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _select_warm_start_shortlist(
    *,
    doe_pool: list[dict[str, Any]],
    variables: list[dict[str, Any]],
    target: int,
    knowledge_guidance: list[dict[str, Any]],
    llm_guidance: dict[str, Any],
    knowledge_priors: dict[str, Any],
    served_priors: list[dict[str, Any]],
    knowledge_mode: str,
) -> list[dict[str, Any]]:
    if target <= 0 or not doe_pool:
        return []

    bias_map = (knowledge_priors or {}).get("warm_start_bias", {}) if isinstance(knowledge_priors, dict) else {}
    category_targets = dict(llm_guidance.get("category_targets", _normalize_category_targets({}, target)))
    preferred_patterns = llm_guidance.get("preferred_patterns", [])
    avoided_patterns = llm_guidance.get("avoided_patterns", [])
    priority_indices = set(llm_guidance.get("priority_indices", []))

    candidate_features = []
    for index, candidate in enumerate(doe_pool):
        prior_signal = score_candidate_with_priors(candidate, served_priors, node_name="warm_start")
        raw_knowledge_total = float((prior_signal.get("knowledge_score_breakdown", {}) or {}).get("total", 0.0) or 0.0)
        if knowledge_mode == "knowledge_guided":
            effective_knowledge_total = raw_knowledge_total
        elif knowledge_mode == "coverage_first":
            effective_knowledge_total = min(raw_knowledge_total, 0.0)
        else:
            effective_knowledge_total = 0.0
        feature = {
            "index": index,
            "candidate": candidate,
            "knowledge_bias_score": _knowledge_bias_score(candidate, bias_map) + effective_knowledge_total,
            "llm_pattern_score": _pattern_score(candidate, variables, preferred_patterns, avoided_patterns),
            "priority_index_bonus": 1.0 / (1 + max(llm_guidance.get("priority_indices", []).index(index), 0))
            if index in priority_indices
            else 0.0,
            "card_refs": _relevant_card_refs(candidate, knowledge_guidance, bias_map, preferred_patterns),
            "applied_prior_ids": list(prior_signal.get("applied_prior_ids", [])),
            "knowledge_score_breakdown": dict(prior_signal.get("knowledge_score_breakdown", {})),
            "knowledge_mode": knowledge_mode,
            "tie_breaker": f"{index:04d}:{candidate_to_key(candidate)}",
        }
        candidate_features.append(feature)

    selected: list[dict[str, Any]] = []
    remaining = {feature["index"]: feature for feature in candidate_features}
    for category in CATEGORY_ORDER:
        quota = int(category_targets.get(category, 0))
        for _ in range(quota):
            if not remaining or len(selected) >= target:
                break
            chosen = _choose_candidate_for_category(
                remaining=list(remaining.values()),
                selected=selected,
                variables=variables,
                category=category,
            )
            selected.append(_feature_to_shortlist_record(chosen, category))
            remaining.pop(chosen["index"], None)

    while remaining and len(selected) < target:
        chosen = _choose_candidate_for_category(
            remaining=list(remaining.values()),
            selected=selected,
            variables=variables,
            category="balanced",
        )
        selected.append(_feature_to_shortlist_record(chosen, "balanced"))
        remaining.pop(chosen["index"], None)

    return selected[:target]


def _choose_candidate_for_category(
    *,
    remaining: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    variables: list[dict[str, Any]],
    category: str,
) -> dict[str, Any]:
    weights = SCORING_WEIGHTS.get(category, SCORING_WEIGHTS["balanced"])
    best_feature = remaining[0]
    best_score = float("-inf")
    best_tie_breaker = str(best_feature.get("tie_breaker", ""))

    for feature in remaining:
        metrics = {
            "coverage_gain": _coverage_gain(feature["candidate"], selected, variables),
            "diversity_gain": _diversity_gain(feature["candidate"], selected, remaining, variables),
            "knowledge_bias_score": float(feature.get("knowledge_bias_score", 0.0)),
            "llm_pattern_score": float(feature.get("llm_pattern_score", 0.0)),
            "priority_index_bonus": float(feature.get("priority_index_bonus", 0.0)),
        }
        score = sum(metrics[name] * weights[name] for name in weights)
        tie_breaker = str(feature.get("tie_breaker", ""))
        if score > best_score or (math.isclose(score, best_score) and tie_breaker < best_tie_breaker):
            best_feature = feature
            best_score = score
            best_tie_breaker = tie_breaker
    return best_feature


def _coverage_gain(candidate: dict[str, Any], selected: list[dict[str, Any]], variables: list[dict[str, Any]]) -> float:
    categorical_variables = [variable for variable in variables if variable.get("type") != "continuous"]
    if not categorical_variables:
        return 0.0
    covered = {
        str(variable.get("name") or ""): {
            str(item["candidate"].get(str(variable.get("name") or ""), ""))
            for item in selected
        }
        for variable in categorical_variables
    }
    gain = 0.0
    for variable in categorical_variables:
        name = str(variable.get("name") or "")
        if str(candidate.get(name, "")) not in covered.get(name, set()):
            gain += 1.0
    return gain / max(len(categorical_variables), 1)


def _diversity_gain(
    candidate: dict[str, Any],
    selected: list[dict[str, Any]],
    remaining: list[dict[str, Any]],
    variables: list[dict[str, Any]],
) -> float:
    if selected:
        return min(candidate_distance(candidate, item["candidate"], variables) for item in selected)
    peer_distances = [
        candidate_distance(candidate, item["candidate"], variables)
        for item in remaining
        if candidate_to_key(candidate) != candidate_to_key(item["candidate"])
    ]
    if not peer_distances:
        return 0.0
    return float(sum(peer_distances)) / float(len(peer_distances))


def _knowledge_bias_score(candidate: dict[str, Any], bias_map: dict[str, dict[str, float]]) -> float:
    if not isinstance(bias_map, dict) or not bias_map:
        return 0.0
    scores = []
    baseline = 0.1
    for variable_name, value_scores in bias_map.items():
        if not isinstance(value_scores, dict):
            continue
        candidate_value = str(candidate.get(variable_name, ""))
        scores.append(float(value_scores.get(candidate_value, baseline)) - baseline)
    if not scores:
        return 0.0
    return sum(scores) / max(len(scores), 1)


def _pattern_score(
    candidate: dict[str, Any],
    variables: list[dict[str, Any]],
    preferred_patterns: list[dict[str, Any]],
    avoided_patterns: list[dict[str, Any]],
) -> float:
    variable_lookup = {
        str(variable.get("name") or ""): variable
        for variable in variables
        if str(variable.get("name") or "")
    }
    score = 0.0
    for entry in preferred_patterns:
        variable = variable_lookup.get(str(entry.get("variable") or ""))
        if variable is None:
            continue
        if _candidate_matches_any_value(candidate.get(entry["variable"]), entry.get("preferred_values", []), variable):
            score += float(entry.get("weight", 1.0))
    for entry in avoided_patterns:
        variable = variable_lookup.get(str(entry.get("variable") or ""))
        if variable is None:
            continue
        if _candidate_matches_any_value(candidate.get(entry["variable"]), entry.get("avoided_values", []), variable):
            score -= float(entry.get("weight", 1.0))
    return score


def _relevant_card_refs(
    candidate: dict[str, Any],
    knowledge_guidance: list[dict[str, Any]],
    bias_map: dict[str, dict[str, float]],
    preferred_patterns: list[dict[str, Any]],
) -> list[str]:
    pattern_variables = {str(entry.get("variable") or "") for entry in preferred_patterns if entry.get("variable")}
    scored: list[tuple[tuple[int, str], str]] = []
    for card in knowledge_guidance:
        card_id = str(card.get("card_id") or "").strip()
        if not card_id:
            continue
        affected = [str(item).strip() for item in card.get("variables_affected", []) if str(item).strip()]
        relevance = 0
        for variable_name in affected:
            if variable_name in bias_map and str(candidate.get(variable_name, "")) in bias_map.get(variable_name, {}):
                relevance += 1
            if variable_name in pattern_variables:
                relevance += 1
        if relevance <= 0:
            continue
        scored.append(((-relevance, card_id), card_id))
    scored.sort(key=lambda item: item[0])
    return [card_id for _key, card_id in scored[:3]]


def _feature_to_shortlist_record(feature: dict[str, Any], category: str) -> dict[str, Any]:
    rationale = _build_selection_rationale(feature, category)
    return {
        "candidate": feature["candidate"],
        "predicted_value": None,
        "uncertainty": None,
        "acquisition_value": None,
        "constraint_violations": [],
        "constraint_satisfied": True,
        "warm_start_category": category,
        "warm_start_rationale": rationale,
        "warm_start_card_refs": list(feature.get("card_refs", [])),
        "warm_start_index": int(feature.get("index", -1)),
        "applied_prior_ids": list(feature.get("applied_prior_ids", [])),
        "knowledge_score_breakdown": dict(feature.get("knowledge_score_breakdown", {})),
        "knowledge_mode": str(feature.get("knowledge_mode") or ""),
    }


def _build_selection_rationale(feature: dict[str, Any], category: str) -> str:
    parts = [f"Selected for {category}."]
    knowledge_mode = str(feature.get("knowledge_mode") or "")
    if knowledge_mode == "coverage_first":
        parts.append("Coverage-first mode kept knowledge as a risk screen rather than a positive ranking driver.")
    elif knowledge_mode == "knowledge_gap":
        parts.append("Knowledge gap mode relied on coverage and diversity only.")
    elif float(feature.get("knowledge_bias_score", 0.0)) > 0:
        parts.append("Aligned with knowledge priors.")
    if float(feature.get("llm_pattern_score", 0.0)) > 0:
        parts.append("Matches LLM-guided preferred patterns.")
    elif float(feature.get("llm_pattern_score", 0.0)) < 0:
        parts.append("Retained despite some avoided-pattern overlap because of coverage/diversity value.")
    if float(feature.get("priority_index_bonus", 0.0)) > 0:
        parts.append("Also appeared in the LLM priority set.")
    return " ".join(parts)


def _sort_warm_start_queue(
    shortlist: list[dict[str, Any]],
    variables: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    for category in CATEGORY_ORDER:
        bucket = [item for item in shortlist if item.get("warm_start_category") == category]
        if not bucket:
            continue
        selected: list[dict[str, Any]] = [bucket.pop(0)]
        while bucket:
            best_index = 0
            best_distance = float("-inf")
            for index, item in enumerate(bucket):
                distance = min(candidate_distance(item["candidate"], prior["candidate"], variables) for prior in selected)
                if distance > best_distance:
                    best_distance = distance
                    best_index = index
            selected.append(bucket.pop(best_index))
        ordered.extend(selected)
    return ordered


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


def _build_warm_start_candidate_search_tool(
    *,
    variables: list[dict[str, Any]],
    observed_keys: set[str],
    hard_constraints: list[dict[str, Any]],
    oracle: DatasetOracle | None,
    seed: int,
):
    dataset_pool = _dataset_candidate_pool(oracle)

    @tool
    def warm_start_candidate_search(
        objective: str = "",
        preferences: list[dict[str, Any]] | None = None,
        must_include: dict[str, Any] | None = None,
        max_results: int = 8,
    ) -> str:
        """Search feasible warm-start candidates that satisfy preferences while preserving diversity."""
        limit = max(1, min(int(max_results or 8), 12))
        search_seed = _warm_start_search_seed(
            base_seed=seed,
            objective=objective,
            preferences=preferences or [],
            must_include=must_include or {},
        )
        pool = _build_warm_start_search_pool(
            variables=variables,
            observed_keys=observed_keys,
            hard_constraints=hard_constraints,
            candidate_pool=dataset_pool,
            seed=search_seed,
            limit=max(limit * 12, 48),
        )
        filtered = [
            candidate
            for candidate in pool
            if _candidate_matches_partial_spec(candidate, must_include or {}, variables)
        ]
        if not filtered:
            return compact_json({"status": "no_matches", "objective": objective, "candidates": []})

        scored = []
        for candidate in filtered:
            preference_score, matched_preferences, avoided_hits = _score_warm_start_candidate(
                candidate,
                preferences or [],
                variables,
            )
            diversity_tags = matched_preferences[:] or [
                f"{name}={candidate.get(name)}"
                for name in list(candidate.keys())[: min(3, len(candidate))]
            ]
            scored.append(
                {
                    "candidate": candidate,
                    "preference_score": preference_score,
                    "diversity_tags": diversity_tags,
                    "reason": _warm_start_search_reason(
                        candidate,
                        objective=objective,
                        matched_preferences=matched_preferences,
                        avoided_hits=avoided_hits,
                    ),
                }
            )

        selected = _select_diverse_search_results(scored, variables, limit)
        return compact_json(
            {
                "status": "success",
                "objective": objective,
                "candidates": selected,
            }
        )

    return warm_start_candidate_search


def _warm_start_search_seed(
    *,
    base_seed: int,
    objective: str,
    preferences: list[dict[str, Any]],
    must_include: dict[str, Any],
) -> int:
    payload = json.dumps(
        {
            "objective": objective,
            "preferences": preferences,
            "must_include": must_include,
        },
        sort_keys=True,
        default=str,
    )
    offset = sum(ord(char) for char in payload) % 997
    return int(base_seed) + offset


def _build_warm_start_search_pool(
    *,
    variables: list[dict[str, Any]],
    observed_keys: set[str],
    hard_constraints: list[dict[str, Any]],
    candidate_pool: list[dict[str, Any]] | None,
    seed: int,
    limit: int,
) -> list[dict[str, Any]]:
    if candidate_pool is not None:
        raw_pool = candidate_pool
    else:
        discrete_candidates = enumerate_discrete_candidates(variables, max_candidates=limit)
        if discrete_candidates:
            raw_pool = discrete_candidates
        else:
            raw_pool = hybrid_sample_candidates(variables, max(limit, 64), seed=seed)

    filtered: list[dict[str, Any]] = []
    seen = set(observed_keys)
    for candidate in raw_pool:
        key = candidate_to_key(candidate)
        if key in seen or _candidate_violates_hard_constraints(candidate, hard_constraints):
            continue
        seen.add(key)
        filtered.append(dict(candidate))
        if candidate_pool is None and len(filtered) >= limit:
            break
    return filtered


def _candidate_violates_hard_constraints(
    candidate: dict[str, Any],
    hard_constraints: list[dict[str, Any]],
) -> bool:
    return any(not constraint.get("check", lambda _: True)(candidate) for constraint in hard_constraints)


def _candidate_matches_partial_spec(
    candidate: dict[str, Any],
    must_include: dict[str, Any],
    variables: list[dict[str, Any]],
) -> bool:
    if not must_include:
        return True
    variable_lookup = {
        str(variable.get("name") or ""): variable
        for variable in variables
        if str(variable.get("name") or "")
    }
    for key, expected_value in must_include.items():
        variable = variable_lookup.get(str(key))
        if variable is None or key not in candidate:
            return False
        if variable.get("type") == "continuous":
            actual = _coerce_finite_float(candidate.get(key))
            expected = _coerce_finite_float(expected_value)
            if actual is None or expected is None or abs(actual - expected) > 1e-9:
                return False
            continue
        if str(candidate.get(key, "")).strip() != str(expected_value).strip():
            return False
    return True


def _score_warm_start_candidate(
    candidate: dict[str, Any],
    preferences: list[dict[str, Any]],
    variables: list[dict[str, Any]],
) -> tuple[float, list[str], list[str]]:
    score = 0.0
    matched_preferences: list[str] = []
    avoided_hits: list[str] = []
    variable_lookup = {
        str(variable.get("name") or ""): variable
        for variable in variables
        if str(variable.get("name") or "")
    }
    for preference in preferences:
        if not isinstance(preference, dict):
            continue
        variable_name = str(preference.get("variable") or "").strip()
        if not variable_name or variable_name not in candidate:
            continue
        variable = variable_lookup.get(variable_name)
        if variable is None:
            continue
        weight = abs(float(preference.get("weight", 1.0) or 1.0))
        preferred_values = preference.get("preferred_values", []) or []
        avoided_values = preference.get("avoided_values", []) or []
        if _candidate_matches_any_value(candidate.get(variable_name), preferred_values, variable):
            score += weight
            matched_preferences.append(f"{variable_name}={candidate.get(variable_name)}")
        if _candidate_matches_any_value(candidate.get(variable_name), avoided_values, variable):
            score -= weight
            avoided_hits.append(f"{variable_name}={candidate.get(variable_name)}")
    return score, matched_preferences, avoided_hits


def _candidate_matches_any_value(value: Any, candidates: list[Any], variable: dict[str, Any]) -> bool:
    if variable.get("type") == "continuous":
        left = _coerce_finite_float(value)
        if left is None:
            return False
        return any(
            (right := _coerce_finite_float(candidate)) is not None and abs(left - right) <= 1e-9
            for candidate in candidates
        )
    return any(str(value).strip() == str(candidate).strip() for candidate in candidates)


def _warm_start_search_reason(
    candidate: dict[str, Any],
    *,
    objective: str,
    matched_preferences: list[str],
    avoided_hits: list[str],
) -> str:
    if matched_preferences:
        return f"Supports objective '{objective or 'warm_start'}' via {', '.join(matched_preferences[:3])}."
    if avoided_hits:
        return f"Feasible candidate for '{objective or 'warm_start'}' despite avoided values: {', '.join(avoided_hits[:2])}."
    return f"Feasible, diverse candidate for objective '{objective or 'warm_start'}'."


def _select_diverse_search_results(
    scored_candidates: list[dict[str, Any]],
    variables: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    remaining = list(scored_candidates)
    selected: list[dict[str, Any]] = []
    while remaining and len(selected) < limit:
        best_index = 0
        best_score = float("-inf")
        for index, record in enumerate(remaining):
            diversity_bonus = 0.0
            if selected:
                diversity_bonus = min(
                    candidate_distance(record["candidate"], prior["candidate"], variables)
                    for prior in selected
                )
            combined = float(record.get("preference_score", 0.0)) + 0.2 * diversity_bonus
            if combined > best_score:
                best_score = combined
                best_index = index
        chosen = remaining.pop(best_index)
        selected.append(
            {
                "candidate": chosen["candidate"],
                "preference_score": round(float(chosen.get("preference_score", 0.0)), 4),
                "diversity_tags": list(chosen.get("diversity_tags", [])),
                "reason": str(chosen.get("reason", "")).strip(),
            }
        )
    return selected


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


def _coerce_int(value: Any, default: int) -> int:
    try:
        if value is None:
            return default
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "plan_warm_start",
    "interpret_warm_start_result",
    "run_warm_start_postmortem",
    "_build_warm_start_candidate_search_tool",
]
