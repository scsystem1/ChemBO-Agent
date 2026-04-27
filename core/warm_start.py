"""
Deterministic warm-start planning and phase-specific helpers.
"""
from __future__ import annotations

import json
import math
from typing import Any, Callable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import tool

from core.autobo_engine import (
    _build_pure_reasoning_space_spec,
    _resolve_structured_pure_reasoning_candidate,
)
from core.autobo_prompts import build_warm_start_structured_seed_prompt
from core.context_builder import ContextBuilder
from core.dataset_oracle import DatasetOracle
from core.prompt_utils import compact_json
from core.problem_loader import resolve_campaign_budget
from core.state import CampaignPhase
from knowledge.knowledge_state import knowledge_mode_from_deck
from pools.component_pools import (
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
        "coverage_gain": 1.0,
        "diversity_gain": 0.8,
        "knowledge_bias_score": 0.5,
        "llm_pattern_score": 0.9,
        "priority_index_bonus": 0.45,
    },
    "balanced": {
        "coverage_gain": 0.9,
        "diversity_gain": 0.8,
        "knowledge_bias_score": 0.8,
        "llm_pattern_score": 1.0,
        "priority_index_bonus": 0.40,
    },
    "exploitation": {
        "coverage_gain": 0.3,
        "diversity_gain": 0.3,
        "knowledge_bias_score": 1.4,
        "llm_pattern_score": 1.4,
        "priority_index_bonus": 0.50,
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
    dataset_pool = _dataset_candidate_pool(oracle)

    probe_pool = _build_coverage_guaranteed_doe_pool(
        variables,
        pool_size=max(raw_target * 4, 80),
        seed=_state_seed(state),
        observed_keys=observed_keys,
        hard_constraints=hard_constraints,
        candidate_pool=dataset_pool,
    )
    warm_start_target = min(raw_target, len(probe_pool))
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
        str(item.get("card_id") or "").strip()
        for item in context.get("knowledge_cards", [])
        if str(item.get("card_id") or "").strip()
    }
    knowledge_mode = knowledge_mode_from_deck(state.get("knowledge_deck", {}))
    total_llm_usage = _empty_usage_delta()
    outbound_messages: list[BaseMessage] = []

    direct_seed_target = min(_compute_global_warm_start_seed_target(warm_start_target), warm_start_target)
    structured_spec = _build_warm_start_structured_space_spec(state)
    direct_records: list[dict[str, Any]] = []
    direct_keys: set[str] = set()
    direct_strategy_summary = ""

    if direct_seed_target > 0 and structured_spec is not None:
        direct_prompt = _build_global_warm_start_seed_prompt(
            context=context,
            structured_spec=structured_spec,
            warm_start_target=warm_start_target,
            direct_seed_target=direct_seed_target,
        )
        direct_messages, _, direct_usage = invoke_tool_loop(
            llm_plain,
            state,
            direct_prompt,
            tool_map={},
            max_turns=2,
            node_name="warm_start",
            recent_message_limits=getattr(settings, "memory_recent_message_limits", None),
        )
        outbound_messages.extend(direct_messages)
        total_llm_usage = _accumulate_usage_delta(total_llm_usage, direct_usage)
        direct_payload = extract_last_json(direct_messages) or _default_direct_seed_response()
        direct_strategy_summary = str(direct_payload.get("strategy_summary") or "").strip()
        direct_records = _resolve_structured_warm_start_candidates(
            direct_payload,
            structured_spec=structured_spec,
            state=state,
            target=direct_seed_target,
        )
        direct_keys = {
            candidate_to_key(item.get("candidate", {}))
            for item in direct_records
            if item.get("candidate")
        }

    residual_target = max(0, warm_start_target - len(direct_records))
    residual_shortlist: list[dict[str, Any]] = []
    residual_strategy_summary = ""
    residual_pool_size = 0

    if residual_target > 0:
        residual_observed_keys = set(observed_keys) | set(direct_keys)
        doe_pool = _build_coverage_guaranteed_doe_pool(
            variables,
            pool_size=max(residual_target * 4, 80),
            seed=_state_seed(state, offset=17),
            observed_keys=residual_observed_keys,
            hard_constraints=hard_constraints,
            candidate_pool=dataset_pool,
        )
        residual_pool_size = len(doe_pool)
        effective_residual_target = min(residual_target, residual_pool_size)
        if effective_residual_target > 0:
            doe_pool = doe_pool[:effective_residual_target * 4]
            search_tool = _build_warm_start_candidate_search_tool(
                variables=variables,
                observed_keys=residual_observed_keys,
                hard_constraints=hard_constraints,
                oracle=oracle,
                seed=_state_seed(state, offset=17),
            )
            warm_start_llm = llm_plain.bind_tools([search_tool])
            residual_prompt = _build_warm_start_guidance_prompt(
                context=context,
                doe_pool=doe_pool,
                target=effective_residual_target,
                direct_seed_count=len(direct_records),
                allow_selected_indices=False,
            )
            residual_messages, _, residual_usage = invoke_tool_loop(
                warm_start_llm,
                state,
                residual_prompt,
                tool_map={search_tool.name: search_tool},
                max_turns=max(8, effective_residual_target // 2 + 4),
                node_name="warm_start",
                recent_message_limits=getattr(settings, "memory_recent_message_limits", None),
            )
            outbound_messages.extend(residual_messages)
            total_llm_usage = _accumulate_usage_delta(total_llm_usage, residual_usage)
            parsed = extract_last_json(residual_messages) or _default_guidance(effective_residual_target, direct_seed_limit=0)
            guidance = _normalize_llm_guidance(
                parsed,
                target=effective_residual_target,
                valid_card_ids=valid_card_ids,
                doe_pool_size=len(doe_pool),
                direct_seed_limit=0,
            )
            residual_strategy_summary = str(guidance.get("strategy_summary") or "").strip()
            residual_shortlist = _select_warm_start_shortlist(
                doe_pool=doe_pool,
                variables=variables,
                target=effective_residual_target,
                knowledge_cards=context.get("knowledge_cards", []),
                llm_guidance=guidance,
            )
            residual_shortlist = _convert_legacy_shortlist_categories(residual_shortlist)

    if warm_start_target < raw_target:
        outbound_messages.append(
            AIMessage(
                content=(
                    f"Warm-start target reduced from {raw_target} to {warm_start_target} because only "
                    f"{len(probe_pool)} feasible unseen candidate(s) were available after coverage-aware pool construction."
                )
            )
        )

    shortlist = _sort_warm_start_queue(direct_records + residual_shortlist, variables)

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
            f"direct={len(direct_records)}/{direct_seed_target} residual={max(0, len(shortlist) - len(direct_records))} "
            f"residual_pool={residual_pool_size} structured_mode={str((structured_spec or {}).get('mode') or 'none')} "
            f"knowledge_mode={knowledge_mode} "
            f"direct_strategy={direct_strategy_summary[:80]} residual_strategy={residual_strategy_summary[:80]}"
        ],
    }
    attach_llm_usage(updates, state, "warm_start", total_llm_usage)
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


def _compute_global_warm_start_seed_target(target: int) -> int:
    target = max(0, int(target or 0))
    if target <= 0:
        return 0
    return max(1, min(int(math.ceil(float(target) * 0.25)), target))


def _empty_usage_delta() -> dict[str, Any]:
    return {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_calls": 0,
        "estimated": False,
    }


def _accumulate_usage_delta(base: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    base = dict(base or _empty_usage_delta())
    delta = dict(delta or {})
    return {
        "calls": int(base.get("calls", 0) or 0) + int(delta.get("calls", 0) or 0),
        "input_tokens": int(base.get("input_tokens", 0) or 0) + int(delta.get("input_tokens", 0) or 0),
        "output_tokens": int(base.get("output_tokens", 0) or 0) + int(delta.get("output_tokens", 0) or 0),
        "total_tokens": int(base.get("total_tokens", 0) or 0) + int(delta.get("total_tokens", 0) or 0),
        "estimated_calls": int(base.get("estimated_calls", 0) or 0) + int(delta.get("estimated_calls", 0) or 0),
        "estimated": bool(base.get("estimated", False) or delta.get("estimated", False)),
    }


def _build_warm_start_structured_space_spec(state: dict[str, Any]) -> dict[str, Any] | None:
    return _build_pure_reasoning_space_spec(state)


def _build_global_warm_start_seed_prompt(
    *,
    context: dict[str, Any],
    structured_spec: dict[str, Any],
    warm_start_target: int,
    direct_seed_target: int,
) -> str:
    return build_warm_start_structured_seed_prompt(
        reaction_context=context.get("problem_features", {}),
        active_hypotheses=context.get("active_hypotheses", []),
        warm_start_target=warm_start_target,
        direct_seed_target=direct_seed_target,
        space_description=str(structured_spec.get("space_description") or ""),
        single_experiment_schema=str(structured_spec.get("output_schema") or "{}"),
        knowledge_cards_text=str(context.get("knowledge_cards_text") or ""),
    )


def _default_direct_seed_response() -> dict[str, Any]:
    return {
        "strategy_summary": "Select the strongest warm-start seeds directly from the full legal structured search space.",
        "selected_experiments": [],
    }


def _resolve_structured_warm_start_candidates(
    payload: dict[str, Any],
    *,
    structured_spec: dict[str, Any],
    state: dict[str, Any],
    target: int,
) -> list[dict[str, Any]]:
    raw_items = payload.get("selected_experiments", [])
    if not isinstance(raw_items, list):
        raw_items = []
    if not raw_items and isinstance(payload, dict) and (
        "variables" in payload or "catalyst_combo_id" in payload or "cat" in payload
    ):
        raw_items = [payload]

    resolved: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for rank, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            continue
        candidate, _failure_reason = _resolve_structured_pure_reasoning_candidate(
            item,
            structured_spec=structured_spec,
            state=state,
        )
        if candidate is None:
            continue
        key = candidate_to_key(candidate)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        resolved.append(
            _make_global_direct_selected_record(
                candidate,
                reasoning=str(item.get("reasoning") or "").strip(),
                confidence=_coerce_finite_float(item.get("confidence")),
                card_refs=_normalize_card_refs(item.get("knowledge_card_ids", [])),
                rank=rank,
            )
        )
        if len(resolved) >= max(0, int(target or 0)):
            break
    return resolved


def _state_seed(state: dict[str, Any], *, offset: int = 0) -> int:
    return int(state.get("random_seed_base", 0) or 0) + int(state.get("iteration", 0) or 0) + int(offset or 0)


def _build_coverage_guaranteed_doe_pool(
    variables: list[dict[str, Any]],
    *,
    pool_size: int,
    seed: int,
    observed_keys: set[str],
    hard_constraints: list[dict[str, Any]],
    candidate_pool: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    target_size = max(1, int(pool_size or 1))
    excluded = set(observed_keys or set())
    constraints = list(hard_constraints or [])
    if candidate_pool is not None:
        raw_pool = [dict(candidate) for candidate in candidate_pool]
    else:
        discrete_candidates = enumerate_discrete_candidates(variables, max_candidates=max(target_size * 20, 4096))
        if discrete_candidates:
            raw_pool = [dict(candidate) for candidate in discrete_candidates]
        else:
            raw_pool = hybrid_sample_candidates(variables, max(target_size * 12, 512), seed=seed)

    feasible: list[dict[str, Any]] = []
    seen = set(excluded)
    for candidate in raw_pool:
        key = candidate_to_key(candidate)
        if key in seen or _candidate_violates_hard_constraints(candidate, constraints):
            continue
        seen.add(key)
        feasible.append(dict(candidate))
    if not feasible:
        return []

    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    categorical_variables = [
        variable
        for variable in variables
        if variable.get("type") != "continuous" and _variable_domain_labels(variable)
    ]
    categorical_variables.sort(
        key=lambda variable: (-len(_variable_domain_labels(variable)), str(variable.get("name") or ""))
    )

    for variable in categorical_variables:
        name = str(variable.get("name") or "")
        covered_values = {str(item.get(name, "")) for item in selected}
        for value in _variable_domain_labels(variable):
            if value in covered_values:
                continue
            matches = [
                dict(candidate)
                for candidate in feasible
                if str(candidate.get(name, "")) == value and candidate_to_key(candidate) not in selected_keys
            ]
            chosen = _pick_farthest_candidate(matches, selected, variables)
            if chosen is None:
                continue
            key = candidate_to_key(chosen)
            selected.append(chosen)
            selected_keys.add(key)
            covered_values.add(value)
            if len(selected) >= min(target_size, len(feasible)):
                return selected[:target_size]

    remaining = [
        dict(candidate)
        for candidate in feasible
        if candidate_to_key(candidate) not in selected_keys
    ]
    while remaining and len(selected) < min(target_size, len(feasible)):
        chosen = _pick_farthest_candidate(remaining, selected, variables)
        if chosen is None:
            break
        key = candidate_to_key(chosen)
        selected.append(dict(chosen))
        selected_keys.add(key)
        remaining = [candidate for candidate in remaining if candidate_to_key(candidate) != key]
    return selected[:target_size]


def _pick_farthest_candidate(
    pool: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    variables: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not pool:
        return None
    ordered = sorted((dict(candidate) for candidate in pool), key=candidate_to_key)
    if not selected:
        return ordered[0]
    best_candidate = ordered[0]
    best_distance = float("-inf")
    best_key = candidate_to_key(best_candidate)
    for candidate in ordered:
        distance = min(candidate_distance(candidate, prior, variables) for prior in selected)
        candidate_key = candidate_to_key(candidate)
        if distance > best_distance or (math.isclose(distance, best_distance) and candidate_key < best_key):
            best_candidate = candidate
            best_distance = distance
            best_key = candidate_key
    return best_candidate


def _build_warm_start_guidance_prompt(
    *,
    context: dict[str, Any],
    doe_pool: list[dict[str, Any]],
    target: int,
    direct_seed_count: int = 0,
    allow_selected_indices: bool = True,
) -> str:
    pool_summary = [{"index": index, "candidate": candidate} for index, candidate in enumerate(doe_pool)]
    knowledge_cards_text = str(context.get("knowledge_cards_text") or "")
    compact_context = {key: value for key, value in context.items() if key not in {"knowledge_cards_text", "knowledge_cards"}}
    direct_seed_limit = _compute_global_warm_start_seed_target(target) if allow_selected_indices else 0
    default_targets = _normalize_category_targets({}, target)
    if allow_selected_indices:
        direct_guidance = (
            f'- Use "selected_indices" only for a few high-confidence seed points you strongly believe deserve inclusion.\n'
            f'- Keep "selected_indices" short (maximum {direct_seed_limit}) and do not use them for every good-looking candidate.'
        )
    else:
        direct_guidance = (
            '- A previous full-space LLM stage already locked the direct warm-start seeds.\n'
            '- Do NOT use "selected_indices" here. Return an empty list for "selected_indices" and focus only on '
            "preferred/avoided patterns, priority indices, and category targets for the remaining slots."
        )
    return f"""Design deterministic guidance for the warm-start planner.

CONTEXT:
{compact_json(compact_context)}

{knowledge_cards_text}

DOE_POOL:
{compact_json(pool_summary)}

Rules:
- You are NOT selecting the full shortlist directly. The deterministic planner will enforce coverage and diversity.
- {direct_seed_count} direct warm-start seed(s) have already been locked from the full legal search space before this step.
- The final warm-start shortlist should balance chemical priors, categorical coverage, and exploration.
- The remaining {target} slot(s) should complement the locked direct seeds with good coverage and diversity.
{direct_guidance}
- Use preferred/avoided patterns to express chemistry knowledge that should influence the remaining slots.
- If the DoE pool is large, you may call warm_start_candidate_search to inspect specific regions before responding.
- Cite knowledge_card_ids only when the active knowledge cards materially influenced a pattern choice.

Return strict JSON:
{{
  "strategy_summary": "...",
  "selected_indices": [3, 17],
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
    "exploration": {default_targets["exploration"]},
    "balanced": {default_targets["balanced"]},
    "exploitation": {default_targets["exploitation"]}
  }},
  "priority_indices": [0, 1, 2]
}}"""


def _default_guidance(target: int, *, direct_seed_limit: int | None = None) -> dict[str, Any]:
    return {
        "strategy_summary": "Use a deterministic warm-start plan that balances coverage, chemistry priors, and exploration.",
        "selected_indices": [],
        "preferred_patterns": [],
        "avoided_patterns": [],
        "category_targets": _normalize_category_targets({}, target),
        "priority_indices": [],
        "_direct_seed_limit": direct_seed_limit,
    }


def _normalize_llm_guidance(
    payload: dict[str, Any],
    *,
    target: int,
    valid_card_ids: set[str],
    doe_pool_size: int,
    direct_seed_limit: int | None = None,
) -> dict[str, Any]:
    raw_selected = payload.get("selected_indices", [])
    return {
        "strategy_summary": str(payload.get("strategy_summary") or _default_guidance(target, direct_seed_limit=direct_seed_limit)["strategy_summary"]).strip(),
        "selected_indices": _normalize_selected_indices(
            raw_selected,
            target=target,
            pool_size=doe_pool_size,
            direct_seed_limit=direct_seed_limit,
        ),
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
        "priority_indices": _normalize_priority_indices(payload.get("priority_indices", []), pool_size=doe_pool_size),
    }


def _normalize_selected_indices(
    payload: Any,
    *,
    target: int,
    pool_size: int,
    direct_seed_limit: int | None = None,
) -> list[int]:
    raw_values = payload if isinstance(payload, list) else [payload]
    seen: set[int] = set()
    normalized: list[int] = []
    limit = _compute_global_warm_start_seed_target(target) if direct_seed_limit is None else max(0, int(direct_seed_limit))
    for raw in raw_values:
        value = _coerce_int(raw, default=-1)
        if value < 0 or value >= pool_size or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
        if len(normalized) >= limit:
            break
    return normalized


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


def _normalize_priority_indices(payload: Any, *, pool_size: int) -> list[int]:
    raw_values = payload if isinstance(payload, list) else [payload]
    seen: set[int] = set()
    normalized: list[int] = []
    for raw in raw_values:
        value = _coerce_int(raw, default=-1)
        if value < 0 or value >= pool_size or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _select_warm_start_shortlist(
    *,
    doe_pool: list[dict[str, Any]],
    variables: list[dict[str, Any]],
    target: int,
    knowledge_cards: list[dict[str, Any]],
    llm_guidance: dict[str, Any],
) -> list[dict[str, Any]]:
    if target <= 0 or not doe_pool:
        return []

    bias_map: dict[str, dict[str, float]] = {}
    category_targets = dict(llm_guidance.get("category_targets", _normalize_category_targets({}, target)))
    preferred_patterns = llm_guidance.get("preferred_patterns", [])
    avoided_patterns = llm_guidance.get("avoided_patterns", [])
    priority_indices = list(llm_guidance.get("priority_indices", []))
    selected_indices = list(llm_guidance.get("selected_indices", []))

    feature_map: dict[int, dict[str, Any]] = {}
    for index, candidate in enumerate(doe_pool):
        feature_map[index] = {
            "index": index,
            "candidate": dict(candidate),
            "knowledge_bias_score": _knowledge_bias_score(candidate, bias_map),
            "llm_pattern_score": _pattern_score(candidate, variables, preferred_patterns, avoided_patterns),
            "priority_index_bonus": 1.0 / (1 + priority_indices.index(index)) if index in priority_indices else 0.0,
            "card_refs": _relevant_card_refs(candidate, knowledge_cards, bias_map, preferred_patterns),
            "tie_breaker": f"{index:04d}:{candidate_to_key(candidate)}",
        }

    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    direct_records = 0
    for index in selected_indices:
        feature = feature_map.get(index)
        if feature is None:
            continue
        candidate = feature["candidate"]
        key = candidate_to_key(candidate)
        if key in selected_keys:
            continue
        if not _seed_keeps_coverage_viable(candidate, selected, variables, target):
            continue
        selected.append(_make_direct_selected_record(feature))
        selected_keys.add(key)
        direct_records += 1
        if direct_records >= _compute_global_warm_start_seed_target(target):
            break

    while len(selected) < target:
        uncovered = _uncovered_discrete_values(selected, variables)
        if not uncovered:
            break
        remaining = [
            feature
            for feature in feature_map.values()
            if candidate_to_key(feature["candidate"]) not in selected_keys
        ]
        if not remaining:
            break
        chosen = _choose_candidate_for_coverage(
            remaining=remaining,
            selected=selected,
            variables=variables,
            uncovered=uncovered,
        )
        if chosen is None:
            break
        candidate = chosen["candidate"]
        gain = _coverage_gain_count(candidate, selected, variables)
        if gain <= 0:
            break
        selected.append(_feature_to_shortlist_record(chosen, "balanced", reason_override="Selected to improve discrete coverage."))
        selected_keys.add(candidate_to_key(candidate))

    remaining_target = max(0, target - len(selected))
    if remaining_target <= 0:
        return selected[:target]

    scaled_targets = _rescale_category_targets(category_targets, target=target, remaining_target=remaining_target)
    remaining = {
        feature["index"]: feature
        for feature in feature_map.values()
        if candidate_to_key(feature["candidate"]) not in selected_keys
    }
    for category in CATEGORY_ORDER:
        quota = int(scaled_targets.get(category, 0))
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


def _seed_keeps_coverage_viable(
    candidate: dict[str, Any],
    selected: list[dict[str, Any]],
    variables: list[dict[str, Any]],
    target: int,
) -> bool:
    simulated = list(selected) + [{"candidate": dict(candidate)}]
    missing = _uncovered_discrete_values(simulated, variables)
    if not missing:
        return True
    required_minimum = max(len(values) for values in missing.values())
    remaining_slots = max(0, target - len(simulated))
    return remaining_slots >= required_minimum


def _choose_candidate_for_coverage(
    *,
    remaining: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    variables: list[dict[str, Any]],
    uncovered: dict[str, set[str]],
) -> dict[str, Any] | None:
    best_feature: dict[str, Any] | None = None
    best_score: tuple[float, float, float, float, str] | None = None
    for feature in remaining:
        candidate = feature["candidate"]
        gain = _coverage_gain_against_uncovered(candidate, uncovered)
        diversity = _diversity_gain(candidate, selected, remaining, variables)
        score = (
            float(gain),
            float(feature.get("llm_pattern_score", 0.0)),
            float(feature.get("knowledge_bias_score", 0.0)),
            float(feature.get("priority_index_bonus", 0.0)) + float(diversity),
            str(feature.get("tie_breaker", "")),
        )
        if best_score is None or score > best_score:
            best_feature = feature
            best_score = score
    return best_feature


def _coverage_gain_against_uncovered(candidate: dict[str, Any], uncovered: dict[str, set[str]]) -> int:
    gain = 0
    for variable_name, values in uncovered.items():
        if str(candidate.get(variable_name, "")) in values:
            gain += 1
    return gain


def _uncovered_discrete_values(
    selected: list[dict[str, Any]],
    variables: list[dict[str, Any]],
) -> dict[str, set[str]]:
    discrete_variables = [variable for variable in variables if variable.get("type") != "continuous"]
    covered = {
        str(variable.get("name") or ""): {
            str(item.get("candidate", {}).get(str(variable.get("name") or ""), ""))
            for item in selected
        }
        for variable in discrete_variables
    }
    uncovered: dict[str, set[str]] = {}
    for variable in discrete_variables:
        name = str(variable.get("name") or "")
        domain_values = set(_variable_domain_labels(variable))
        missing = {value for value in domain_values if value not in covered.get(name, set())}
        if missing:
            uncovered[name] = missing
    return uncovered


def _rescale_category_targets(
    category_targets: dict[str, int],
    *,
    target: int,
    remaining_target: int,
) -> dict[str, int]:
    if remaining_target <= 0:
        return {category: 0 for category in CATEGORY_ORDER}
    scale = float(remaining_target) / float(max(target, 1))
    scaled: dict[str, int] = {}
    assigned = 0
    remainders: list[tuple[float, str]] = []
    for category in CATEGORY_ORDER:
        exact = float(category_targets.get(category, 0)) * scale
        count = int(math.floor(exact))
        scaled[category] = count
        assigned += count
        remainders.append((exact - count, category))
    for _fraction, category in sorted(remainders, reverse=True):
        if assigned >= remaining_target:
            break
        scaled[category] = scaled.get(category, 0) + 1
        assigned += 1
    return scaled


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
    return float(_coverage_gain_count(candidate, selected, variables)) / float(max(len(categorical_variables), 1))


def _coverage_gain_count(candidate: dict[str, Any], selected: list[dict[str, Any]], variables: list[dict[str, Any]]) -> int:
    covered = {
        str(variable.get("name") or ""): {
            str(item.get("candidate", {}).get(str(variable.get("name") or ""), ""))
            for item in selected
        }
        for variable in variables
        if variable.get("type") != "continuous"
    }
    gain = 0
    for variable in variables:
        if variable.get("type") == "continuous":
            continue
        name = str(variable.get("name") or "")
        if str(candidate.get(name, "")) not in covered.get(name, set()):
            gain += 1
    return gain


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
    knowledge_cards: list[dict[str, Any]],
    bias_map: dict[str, dict[str, float]],
    preferred_patterns: list[dict[str, Any]],
) -> list[str]:
    pattern_variables = {str(entry.get("variable") or "") for entry in preferred_patterns if entry.get("variable")}
    scored: list[tuple[tuple[int, str], str]] = []
    for card in knowledge_cards:
        card_id = str(card.get("card_id") or "").strip()
        if not card_id:
            continue
        affected = [str(item).strip() for item in card.get("targets", card.get("variables_affected", [])) if str(item).strip()]
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


def _make_direct_selected_record(feature: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate": dict(feature["candidate"]),
        "predicted_value": None,
        "uncertainty": None,
        "acquisition_value": None,
        "constraint_violations": [],
        "constraint_satisfied": True,
        "warm_start_category": "exploitation",
        "warm_start_rationale": (
            f"Direct LLM seed selection from pool index {int(feature.get('index', -1))}. "
            "Retained as a high-confidence chemistry-guided starting point while preserving overall coverage."
        ),
        "warm_start_card_refs": list(feature.get("card_refs", [])),
        "warm_start_index": int(feature.get("index", -1)),
    }


def _make_global_direct_selected_record(
    candidate: dict[str, Any],
    *,
    reasoning: str,
    confidence: float | None,
    card_refs: list[str],
    rank: int,
) -> dict[str, Any]:
    confidence_text = ""
    if confidence is not None:
        confidence_text = f" Confidence={max(0.0, min(float(confidence), 1.0)):.2f}."
    rationale = (
        f"Full-space LLM direct seed #{int(rank)} selected before coverage planning."
        f"{confidence_text}"
    )
    if reasoning:
        rationale = f"{rationale} {reasoning}"
    return {
        "candidate": dict(candidate),
        "predicted_value": None,
        "uncertainty": None,
        "acquisition_value": None,
        "constraint_violations": [],
        "constraint_satisfied": True,
        "warm_start_category": "anchor",
        "warm_start_rationale": rationale.strip(),
        "warm_start_card_refs": list(card_refs),
        "warm_start_index": -1,
        "_warm_start_source": "global_llm_direct",
        "_warm_start_rank": int(rank),
    }


def _feature_to_shortlist_record(
    feature: dict[str, Any],
    category: str,
    *,
    reason_override: str | None = None,
) -> dict[str, Any]:
    rationale = reason_override or _build_selection_rationale(feature, category)
    return {
        "candidate": dict(feature["candidate"]),
        "predicted_value": None,
        "uncertainty": None,
        "acquisition_value": None,
        "constraint_violations": [],
        "constraint_satisfied": True,
        "warm_start_category": category,
        "warm_start_rationale": rationale,
        "warm_start_card_refs": list(feature.get("card_refs", [])),
        "warm_start_index": int(feature.get("index", -1)),
    }


def _build_selection_rationale(feature: dict[str, Any], category: str) -> str:
    parts = [f"Selected for {category}."]
    if feature.get("card_refs"):
        parts.append("Aligned with active knowledge cards.")
    if float(feature.get("llm_pattern_score", 0.0)) > 0:
        parts.append("Matches LLM-guided preferred patterns.")
    elif float(feature.get("llm_pattern_score", 0.0)) < 0:
        parts.append("Retained despite some avoided-pattern overlap because of coverage/diversity value.")
    if float(feature.get("priority_index_bonus", 0.0)) > 0:
        parts.append("Also appeared in the LLM priority set.")
    return " ".join(parts)


def _convert_legacy_shortlist_categories(shortlist: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mapping = {
        "exploitation": "anchor",
        "balanced": "contrast",
        "exploration": "wildcard",
    }
    converted: list[dict[str, Any]] = []
    for item in shortlist:
        updated = dict(item)
        updated["warm_start_category"] = mapping.get(str(item.get("warm_start_category") or ""), "wildcard")
        converted.append(updated)
    return converted


def _sort_warm_start_queue(
    shortlist: list[dict[str, Any]],
    variables: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    direct_prefix = [
        dict(item)
        for item in shortlist
        if str(item.get("_warm_start_source") or "") == "global_llm_direct"
    ]
    direct_prefix.sort(key=lambda item: int(item.get("_warm_start_rank", 0) or 0))
    residual = [
        dict(item)
        for item in shortlist
        if str(item.get("_warm_start_source") or "") != "global_llm_direct"
    ]
    anchors = [dict(item) for item in residual if item.get("warm_start_category") == "anchor"]
    contrasts = [dict(item) for item in residual if item.get("warm_start_category") == "contrast"]
    wildcards = [dict(item) for item in residual if item.get("warm_start_category") == "wildcard"]
    ordered = []
    ordered.extend(direct_prefix)
    ordered.extend(_order_bucket_by_distance(anchors, variables))
    ordered.extend(_order_bucket_by_distance(contrasts, variables))
    ordered.extend(_order_bucket_by_distance(wildcards, variables))
    return [_strip_internal_warm_start_fields(item) for item in ordered]


def _order_bucket_by_distance(
    bucket: list[dict[str, Any]],
    variables: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not bucket:
        return []
    remaining = sorted((dict(item) for item in bucket), key=lambda item: candidate_to_key(item.get("candidate", {})))
    selected: list[dict[str, Any]] = [remaining.pop(0)]
    while remaining:
        best_index = 0
        best_distance = float("-inf")
        best_key = candidate_to_key(remaining[0].get("candidate", {}))
        for index, item in enumerate(remaining):
            distance = min(candidate_distance(item["candidate"], prior["candidate"], variables) for prior in selected)
            candidate_key = candidate_to_key(item["candidate"])
            if distance > best_distance or (math.isclose(distance, best_distance) and candidate_key < best_key):
                best_index = index
                best_distance = distance
                best_key = candidate_key
        selected.append(remaining.pop(best_index))
    return selected


def _strip_internal_warm_start_fields(item: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(item)
    for key in list(cleaned):
        if key.startswith("_warm_start_"):
            cleaned.pop(key, None)
    return cleaned


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
            if actual is None:
                return False
            if isinstance(expected_value, (list, tuple)) and len(expected_value) == 2:
                low = _coerce_finite_float(expected_value[0])
                high = _coerce_finite_float(expected_value[1])
                if low is None or high is None or not (min(low, high) <= actual <= max(low, high)):
                    return False
                continue
            expected = _coerce_finite_float(expected_value)
            if expected is None or abs(actual - expected) > 1e-9:
                return False
            continue
        expected_values = expected_value if isinstance(expected_value, list) else [expected_value]
        normalized_expected = {str(value).strip() for value in expected_values if str(value).strip()}
        if str(candidate.get(key, "")).strip() not in normalized_expected:
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


def _variable_domain_labels(variable: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for entry in variable.get("domain", []):
        if isinstance(entry, dict):
            labels.append(str(entry.get("label") or entry.get("name") or entry.get("value") or entry))
        else:
            labels.append(str(entry))
    return labels


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
        if value is None or isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "plan_warm_start",
    "interpret_warm_start_result",
    "run_warm_start_postmortem",
    "_build_coverage_guaranteed_doe_pool",
    "_build_global_warm_start_seed_prompt",
    "_build_warm_start_candidate_search_tool",
    "_build_warm_start_structured_space_spec",
    "_compute_global_warm_start_seed_target",
    "_normalize_llm_guidance",
    "_resolve_structured_warm_start_candidates",
    "_select_warm_start_shortlist",
    "_sort_warm_start_queue",
]
