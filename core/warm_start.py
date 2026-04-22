"""
Deterministic warm-start planning and phase-specific helpers.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
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
from knowledge.knowledge_state import knowledge_mode_from_deck
from pools.component_pools import (
    build_doe_pool,
    candidate_distance,
    candidate_to_key,
    enumerate_discrete_candidates,
    hybrid_sample_candidates,
)

SlotType = str
SLOT_ORDER: list[SlotType] = ["anchor", "contrast", "wildcard"]
DEFAULT_SLOT_RATIOS: dict[SlotType, float] = {
    "anchor": 0.30,
    "contrast": 0.40,
    "wildcard": 0.30,
}
MAX_CONTRAST_PER_ANCHOR = 3
REGION_FALLBACK_TO_DOE_POOL = True

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


@dataclass(frozen=True)
class RegionSpec:
    """LLM-proposed warm-start region over the feasible search space."""

    name: str
    slot_type: SlotType
    filters: dict[str, Any]
    quota: int
    priority: int
    reason: str
    knowledge_card_ids: list[str]


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
    if dataset_pool is not None:
        full_pool = [
            dict(candidate)
            for candidate in dataset_pool
            if candidate_to_key(candidate) not in observed_keys
            and not _candidate_violates_hard_constraints(candidate, hard_constraints)
        ]
    else:
        full_pool = build_doe_pool(
            variables,
            pool_size=max(raw_target * 8, 200),
            seed=int(state.get("iteration", 0) or 0),
            observed_keys=observed_keys,
            hard_constraints=hard_constraints,
            candidate_pool=None,
        )

    warm_start_target = min(raw_target, len(full_pool))
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

    search_tool = _build_warm_start_candidate_search_tool(
        variables=variables,
        observed_keys=observed_keys,
        hard_constraints=hard_constraints,
        oracle=oracle,
        seed=int(state.get("iteration", 0) or 0),
    )
    warm_start_llm = llm_plain.bind_tools([search_tool])
    prompt = _build_region_guidance_prompt(
        context=context,
        space_summary=_build_variable_space_summary(variables, observed_keys, oracle),
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
    parsed = extract_last_json(messages) or {}
    shortlist: list[dict[str, Any]] = []
    strategy_label = ""
    has_region_payload = any(
        isinstance(parsed.get(key), list) and bool(parsed.get(key))
        for key in ("anchor_regions", "wildcard_regions")
    )
    if has_region_payload:
        region_guidance = _normalize_region_guidance(
            parsed,
            target=warm_start_target,
            valid_card_ids=valid_card_ids,
            variables=variables,
        )
        shortlist = _select_warm_start_from_regions(
            full_pool=full_pool,
            variables=variables,
            target=warm_start_target,
            region_guidance=region_guidance,
            observed_keys=observed_keys,
        )
        strategy_label = f"region_based [{region_guidance.get('strategy_summary', '')[:80]}]"
        if not shortlist and REGION_FALLBACK_TO_DOE_POOL:
            has_region_payload = False

    if not has_region_payload:
        doe_pool = build_doe_pool(
            variables,
            pool_size=max(warm_start_target * 3, 60),
            seed=int(state.get("iteration", 0) or 0),
            observed_keys=observed_keys,
            hard_constraints=hard_constraints,
            candidate_pool=dataset_pool,
        )
        guidance = _normalize_llm_guidance(parsed, target=warm_start_target, valid_card_ids=valid_card_ids)
        shortlist = _select_warm_start_shortlist(
            doe_pool=doe_pool,
            variables=variables,
            target=warm_start_target,
            knowledge_cards=context.get("knowledge_cards", []),
            llm_guidance=guidance,
        )
        shortlist = _convert_legacy_shortlist_categories(shortlist)
        strategy_label = f"doe_pool_fallback [{guidance.get('strategy_summary', '')[:80]}]"

    shortlist = _sort_warm_start_queue(shortlist, variables)

    outbound_messages = list(messages)
    if warm_start_target < raw_target:
        outbound_messages.append(
            AIMessage(
                content=(
                    f"Warm-start target reduced from {raw_target} to {warm_start_target} because only "
                    f"{len(full_pool)} feasible unseen candidate(s) were available after budget and feasibility checks."
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
            f"pool={len(full_pool)} knowledge_mode={knowledge_mode} strategy={strategy_label}"
        ],
    }
    attach_llm_usage(updates, state, "warm_start", llm_usage)
    return updates


def _allocate_slot_targets(target: int) -> dict[str, int]:
    if target <= 0:
        return {slot: 0 for slot in SLOT_ORDER}
    if target == 1:
        return {"anchor": 1, "contrast": 0, "wildcard": 0}
    if target == 2:
        return {"anchor": 1, "contrast": 1, "wildcard": 0}

    base = {slot: 0 for slot in SLOT_ORDER}
    assigned = 0
    remainders: list[tuple[float, str]] = []
    for slot in SLOT_ORDER:
        exact = float(target) * float(DEFAULT_SLOT_RATIOS[slot])
        count = int(math.floor(exact))
        base[slot] = count
        assigned += count
        remainders.append((exact - count, slot))
    for _fraction, slot in sorted(remainders, key=lambda item: (-item[0], SLOT_ORDER.index(item[1]))):
        if assigned >= target:
            break
        base[slot] += 1
        assigned += 1
    if base["anchor"] <= 0:
        donor = max((slot for slot in SLOT_ORDER if slot != "anchor"), key=lambda slot: (base[slot], -SLOT_ORDER.index(slot)))
        if base[donor] > 0:
            base[donor] -= 1
            base["anchor"] += 1
    return base


def _build_variable_space_summary(
    variables: list[dict[str, Any]],
    observed_keys: set[str],
    oracle: DatasetOracle | None,
) -> dict[str, Any]:
    full_pool = _dataset_candidate_pool(oracle)
    summary: list[dict[str, Any]] = []
    for variable in variables:
        name = str(variable.get("name") or "")
        variable_type = str(variable.get("type") or "categorical")
        if variable_type == "continuous":
            domain = list(variable.get("domain", [0.0, 1.0]))
            low = _coerce_finite_float(domain[0] if domain else 0.0)
            high = _coerce_finite_float(domain[1] if len(domain) > 1 else (domain[0] if domain else 1.0))
            summary.append(
                {
                    "name": name,
                    "type": "continuous",
                    "range": [low if low is not None else 0.0, high if high is not None else 1.0],
                    "unit": variable.get("unit", ""),
                    "description": variable.get("description", ""),
                }
            )
            continue

        labels = _variable_domain_labels(variable)
        entry: dict[str, Any] = {
            "name": name,
            "type": "categorical",
            "n_values": len(labels),
            "values": labels,
            "unit": variable.get("unit", ""),
            "description": variable.get("description", ""),
        }
        if full_pool:
            counts: dict[str, int] = {}
            for candidate in full_pool:
                value = str(candidate.get(name, ""))
                if value:
                    counts[value] = counts.get(value, 0) + 1
            if counts:
                preview = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:10]
                entry["value_frequency_preview"] = {value: count for value, count in preview}
                entry["total_candidates_in_pool"] = len(full_pool)
        summary.append(entry)

    total_pool = len(full_pool) if full_pool is not None else None
    already_observed = len(observed_keys)
    return {
        "variables": summary,
        "pool_info": {
            "total_candidates": total_pool,
            "already_observed": already_observed,
            "remaining": (total_pool - already_observed) if total_pool is not None else None,
            "is_dataset_mode": full_pool is not None,
        },
    }


def _build_region_guidance_prompt(
    *,
    context: dict[str, Any],
    space_summary: dict[str, Any],
    target: int,
) -> str:
    slot_targets = _allocate_slot_targets(target)
    knowledge_cards_text = str(context.get("knowledge_cards_text") or "")
    compact_context = {key: value for key, value in context.items() if key not in {"knowledge_cards_text", "knowledge_cards"}}
    return f"""You are designing the initial experimental campaign for chemical reaction optimization.

Your task is to identify which CHEMICAL REGIONS of the search space are most valuable to explore first.
You are NOT selecting individual experiments. The deterministic planner handles final candidate selection.

CAMPAIGN CONTEXT:
{compact_json(compact_context)}

{knowledge_cards_text}

FULL SEARCH SPACE SUMMARY:
{compact_json(space_summary)}

SLOT BUDGET:
- anchor_slots: {slot_targets["anchor"]}  (highest-confidence chemistry-guided regions)
- wildcard_slots: {slot_targets["wildcard"]}  (speculative but chemically motivated regions)
- contrast_slots: {slot_targets["contrast"]}  (auto-generated by the planner; provide variable priorities only)

RULES:
- Each region filter must use exact variable names from the search-space summary.
- Categorical filters must list exact string values from the variable domain.
- Continuous filters must be [lower_bound, upper_bound].
- An empty filter {{}} is allowed for a broad wildcard region.
- Use knowledge_card_ids only when an active knowledge card in the prompt materially influenced the region choice.
- Keep reasons concise and chemical.
- If you need local inspection, call warm_start_candidate_search before responding.

Return strict JSON:
{{
  "strategy_summary": "...",
  "anchor_regions": [
    {{
      "name": "descriptive region name",
      "filter": {{"variable_name": ["value_a", "value_b"]}},
      "quota": 2,
      "priority": 1,
      "reason": "...",
      "knowledge_card_ids": ["kc_..."]
    }}
  ],
  "wildcard_regions": [
    {{
      "name": "descriptive region name",
      "filter": {{"variable_name": ["value_x"]}},
      "quota": 2,
      "priority": 1,
      "reason": "...",
      "knowledge_card_ids": []
    }}
  ],
  "contrast_variable_priority": ["variable_name_1", "variable_name_2"]
}}"""


def _normalize_region_guidance(
    payload: dict[str, Any],
    *,
    target: int,
    valid_card_ids: set[str],
    variables: list[dict[str, Any]],
) -> dict[str, Any]:
    variable_lookup = {
        str(variable.get("name") or ""): variable
        for variable in variables
        if str(variable.get("name") or "")
    }
    slot_targets = _allocate_slot_targets(target)

    def _normalize_region_list(raw_list: Any, slot_type: SlotType) -> list[RegionSpec]:
        if not isinstance(raw_list, list):
            return []
        regions: list[RegionSpec] = []
        for index, raw in enumerate(raw_list, start=1):
            if not isinstance(raw, dict):
                continue
            filters = _normalize_region_filters(raw.get("filter", {}), variable_lookup)
            regions.append(
                RegionSpec(
                    name=str(raw.get("name") or f"{slot_type}_{index}").strip() or f"{slot_type}_{index}",
                    slot_type=slot_type,
                    filters=filters,
                    quota=max(1, _coerce_int(raw.get("quota"), default=1)),
                    priority=max(1, _coerce_int(raw.get("priority"), default=99)),
                    reason=str(raw.get("reason") or "").strip(),
                    knowledge_card_ids=[
                        card_id
                        for card_id in _normalize_card_refs(raw.get("knowledge_card_ids", []))
                        if card_id in valid_card_ids
                    ],
                )
            )
        regions.sort(key=lambda region: (region.priority, region.name))
        return _rescale_region_quotas(regions, slot_targets.get(slot_type, 0))

    raw_priority = payload.get("contrast_variable_priority", [])
    contrast_priority = [
        name
        for name in _normalize_card_refs(raw_priority)
        if name in variable_lookup
    ]
    if not contrast_priority:
        categorical = [
            variable
            for variable in variables
            if variable.get("type") != "continuous" and str(variable.get("name") or "")
        ]
        categorical.sort(key=lambda variable: (-len(_variable_domain_labels(variable)), str(variable.get("name") or "")))
        contrast_priority = [str(variable.get("name") or "") for variable in categorical[:3]]

    return {
        "strategy_summary": str(payload.get("strategy_summary") or "Region-based warm-start.").strip(),
        "anchor_regions": _normalize_region_list(payload.get("anchor_regions"), "anchor"),
        "wildcard_regions": _normalize_region_list(payload.get("wildcard_regions"), "wildcard"),
        "contrast_variable_priority": contrast_priority[:3],
    }


def _normalize_region_filters(
    raw_filters: Any,
    variable_lookup: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(raw_filters, dict):
        return {}
    normalized: dict[str, Any] = {}
    for raw_name, raw_value in raw_filters.items():
        name = str(raw_name or "").strip()
        variable = variable_lookup.get(name)
        if variable is None:
            continue
        if variable.get("type") == "continuous":
            if isinstance(raw_value, (list, tuple)) and len(raw_value) == 2:
                clipped = _clip_continuous_filter(raw_value, variable)
                if clipped is not None:
                    normalized[name] = clipped
            continue
        allowed = set(_variable_domain_labels(variable))
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        clean_values = [str(value).strip() for value in values if str(value).strip() in allowed]
        if clean_values:
            normalized[name] = clean_values
    return normalized


def _clip_continuous_filter(raw_value: list[Any] | tuple[Any, Any], variable: dict[str, Any]) -> list[float] | None:
    low = _coerce_finite_float(raw_value[0])
    high = _coerce_finite_float(raw_value[1])
    if low is None or high is None:
        return None
    domain = list(variable.get("domain", [0.0, 1.0]))
    domain_low = _coerce_finite_float(domain[0] if domain else 0.0)
    domain_high = _coerce_finite_float(domain[1] if len(domain) > 1 else (domain[0] if domain else 1.0))
    if domain_low is None or domain_high is None:
        return None
    clipped_low = max(min(low, high), min(domain_low, domain_high))
    clipped_high = min(max(low, high), max(domain_low, domain_high))
    if clipped_low > clipped_high:
        return None
    return [clipped_low, clipped_high]


def _rescale_region_quotas(regions: list[RegionSpec], total_budget: int) -> list[RegionSpec]:
    if not regions or total_budget < 0:
        return regions
    total_requested = sum(max(0, int(region.quota)) for region in regions)
    if total_requested <= total_budget:
        return regions
    if total_budget == 0:
        return [replace(region, quota=0) for region in regions]

    exacts: list[tuple[float, int, RegionSpec]] = []
    assigned = 0
    quotas: dict[str, int] = {}
    for region in regions:
        exact = float(total_budget) * float(region.quota) / float(total_requested)
        quota = int(math.floor(exact))
        quotas[region.name] = quota
        assigned += quota
        exacts.append((exact - quota, region.priority, region))
    for _fraction, _priority, region in sorted(exacts, key=lambda item: (-item[0], item[1], item[2].name)):
        if assigned >= total_budget:
            break
        quotas[region.name] += 1
        assigned += 1
    return [replace(region, quota=quotas.get(region.name, 0)) for region in regions if quotas.get(region.name, 0) >= 0]


def _candidates_matching_region(
    region: RegionSpec,
    full_pool: list[dict[str, Any]],
    variables: list[dict[str, Any]],
    excluded_keys: set[str],
) -> list[dict[str, Any]]:
    variable_lookup = {
        str(variable.get("name") or ""): variable
        for variable in variables
        if str(variable.get("name") or "")
    }
    matched: list[dict[str, Any]] = []
    for candidate in full_pool:
        key = candidate_to_key(candidate)
        if key in excluded_keys:
            continue
        keep = True
        for variable_name, filter_value in region.filters.items():
            variable = variable_lookup.get(variable_name)
            if variable is None:
                continue
            candidate_value = candidate.get(variable_name)
            if variable.get("type") == "continuous":
                if not isinstance(filter_value, (list, tuple)) or len(filter_value) != 2:
                    continue
                value = _coerce_finite_float(candidate_value)
                low = _coerce_finite_float(filter_value[0])
                high = _coerce_finite_float(filter_value[1])
                if value is None or low is None or high is None or not (min(low, high) <= value <= max(low, high)):
                    keep = False
                    break
                continue
            allowed = {str(item).strip() for item in (filter_value if isinstance(filter_value, list) else [filter_value])}
            if str(candidate_value).strip() not in allowed:
                keep = False
                break
        if keep:
            matched.append(dict(candidate))
    matched.sort(key=candidate_to_key)
    return matched


def _farthest_first_sample(
    pool: list[dict[str, Any]],
    variables: list[dict[str, Any]],
    n: int,
    initial_selected: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if n <= 0 or not pool:
        return []
    selected: list[dict[str, Any]] = [dict(candidate) for candidate in (initial_selected or [])]
    remaining = sorted((dict(candidate) for candidate in pool), key=candidate_to_key)
    fresh: list[dict[str, Any]] = []
    while remaining and len(fresh) < n:
        if not selected:
            chosen = remaining.pop(0)
        else:
            best_index = 0
            best_distance = float("-inf")
            best_key = candidate_to_key(remaining[0])
            for index, candidate in enumerate(remaining):
                min_distance = min(candidate_distance(candidate, prior, variables) for prior in selected)
                candidate_key = candidate_to_key(candidate)
                if min_distance > best_distance or (math.isclose(min_distance, best_distance) and candidate_key < best_key):
                    best_index = index
                    best_distance = min_distance
                    best_key = candidate_key
            chosen = remaining.pop(best_index)
        selected.append(dict(chosen))
        fresh.append(dict(chosen))
    return fresh


def _contrast_support_ratio(
    full_pool: list[dict[str, Any]],
    variable_name: str,
    all_variable_names: list[str],
) -> float:
    if not full_pool or variable_name not in all_variable_names:
        return 0.0
    other_variables = [name for name in all_variable_names if name != variable_name]
    if not other_variables:
        return 0.0
    group_counts: dict[tuple[str, ...], int] = {}
    for candidate in full_pool:
        key = tuple(str(candidate.get(name, "")) for name in other_variables)
        group_counts[key] = group_counts.get(key, 0) + 1
    supported = sum(
        1
        for candidate in full_pool
        if group_counts.get(tuple(str(candidate.get(name, "")) for name in other_variables), 0) > 1
    )
    return float(supported) / float(len(full_pool))


def _filter_and_rank_contrast_variables(
    requested_priority: list[str],
    full_pool: list[dict[str, Any]],
    variables: list[dict[str, Any]],
) -> list[str]:
    all_names = [str(variable.get("name") or "") for variable in variables if str(variable.get("name") or "")]
    support = {name: _contrast_support_ratio(full_pool, name, all_names) for name in all_names}
    requested = [name for name in requested_priority if name in support]
    supported_requested = [name for name in requested if support.get(name, 0.0) > 0.0]
    if supported_requested:
        extras = [
            name
            for name in sorted(all_names, key=lambda item: (-support.get(item, 0.0), all_names.index(item), item))
            if support.get(name, 0.0) > 0.0 and name not in supported_requested
        ]
        return (supported_requested + extras)[:3]
    fallback = sorted(all_names, key=lambda name: (-support.get(name, 0.0), all_names.index(name), name))
    return [name for name in fallback[:3] if support.get(name, 0.0) > 0.0] or fallback[:3]


def _score_contrast_candidate(
    anchor: dict[str, Any],
    candidate: dict[str, Any],
    priority_variables: list[str],
    all_variable_names: list[str],
    selected_candidates: list[dict[str, Any]],
    variables: list[dict[str, Any]],
) -> tuple[int, float, float, float, float] | None:
    changed_priority = [
        name
        for name in priority_variables
        if str(candidate.get(name, "")) != str(anchor.get(name, ""))
    ]
    if not changed_priority:
        return None
    changed_all = [
        name
        for name in all_variable_names
        if str(candidate.get(name, "")) != str(anchor.get(name, ""))
    ]
    changed_other = [name for name in changed_all if name not in priority_variables]
    if 1 <= len(changed_priority) <= 2 and len(changed_other) <= 1:
        tier = 2
        precision_score = -float(len(changed_other))
        focus_score = -float(len(changed_priority))
    else:
        tier = 1
        precision_score = -float(len(changed_other))
        focus_score = float(len(changed_priority))
    priority_rank_score = -float(min(priority_variables.index(name) for name in changed_priority))
    diversity_bonus = 0.0
    if selected_candidates:
        diversity_bonus = min(candidate_distance(candidate, prior, variables) for prior in selected_candidates)
    return tier, priority_rank_score, focus_score, precision_score, diversity_bonus


def _generate_contrast_candidates(
    anchor_candidates: list[dict[str, Any]],
    contrast_var_priority: list[str],
    full_pool: list[dict[str, Any]],
    variables: list[dict[str, Any]],
    n_contrast: int,
    excluded_keys: set[str],
) -> list[dict[str, Any]]:
    if n_contrast <= 0 or not anchor_candidates or not full_pool:
        return []

    all_variable_names = [str(variable.get("name") or "") for variable in variables if str(variable.get("name") or "")]
    priority_variables = _filter_and_rank_contrast_variables(contrast_var_priority, full_pool, variables)
    if not priority_variables:
        return []

    anchor_keys = {candidate_to_key(anchor): anchor for anchor in anchor_candidates}
    used_keys = set(excluded_keys)
    per_anchor_counts = {candidate_to_key(anchor): 0 for anchor in anchor_candidates}
    contrasts: list[dict[str, Any]] = []

    def _best_for_anchor(anchor: dict[str, Any], selected_candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        best_payload: dict[str, Any] | None = None
        best_score: tuple[int, float, float, float, float] | None = None
        best_key = ""
        for candidate in full_pool:
            candidate_key = candidate_to_key(candidate)
            if candidate_key in used_keys:
                continue
            score = _score_contrast_candidate(
                anchor,
                candidate,
                priority_variables,
                all_variable_names,
                selected_candidates,
                variables,
            )
            if score is None:
                continue
            if best_score is None or score > best_score or (score == best_score and candidate_key < best_key):
                best_score = score
                best_key = candidate_key
                best_payload = {
                    "candidate": dict(candidate),
                    "anchor_key": candidate_to_key(anchor),
                    "anchor_candidate": dict(anchor),
                    "pair_rank": per_anchor_counts.get(candidate_to_key(anchor), 0),
                    "contrast_variables": [
                        name
                        for name in priority_variables
                        if str(candidate.get(name, "")) != str(anchor.get(name, ""))
                    ],
                }
        return best_payload

    selected_candidates = list(anchor_candidates)
    for anchor in anchor_candidates:
        if len(contrasts) >= n_contrast:
            break
        choice = _best_for_anchor(anchor, selected_candidates)
        if choice is None:
            continue
        candidate_key = candidate_to_key(choice["candidate"])
        anchor_key = str(choice["anchor_key"])
        used_keys.add(candidate_key)
        per_anchor_counts[anchor_key] = per_anchor_counts.get(anchor_key, 0) + 1
        selected_candidates.append(dict(choice["candidate"]))
        contrasts.append(choice)

    while len(contrasts) < n_contrast:
        best_choice: dict[str, Any] | None = None
        best_score: tuple[int, int, float, float, float, float] | None = None
        best_key = ""
        for anchor in anchor_candidates:
            anchor_key = candidate_to_key(anchor)
            current_count = per_anchor_counts.get(anchor_key, 0)
            if current_count >= MAX_CONTRAST_PER_ANCHOR:
                continue
            choice = _best_for_anchor(anchor, selected_candidates)
            if choice is None:
                continue
            choice_key = candidate_to_key(choice["candidate"])
            score = _score_contrast_candidate(
                anchor,
                choice["candidate"],
                priority_variables,
                all_variable_names,
                selected_candidates,
                variables,
            )
            if score is None:
                continue
            ranked_score = (score[0], -current_count, score[1], score[2], score[3], score[4])
            if best_score is None or ranked_score > best_score or (ranked_score == best_score and choice_key < best_key):
                best_choice = choice
                best_score = ranked_score
                best_key = choice_key
        if best_choice is None:
            break
        chosen_candidate = dict(best_choice["candidate"])
        chosen_key = candidate_to_key(chosen_candidate)
        anchor_key = str(best_choice["anchor_key"])
        best_choice["pair_rank"] = per_anchor_counts.get(anchor_key, 0)
        used_keys.add(chosen_key)
        per_anchor_counts[anchor_key] = per_anchor_counts.get(anchor_key, 0) + 1
        selected_candidates.append(chosen_candidate)
        contrasts.append(best_choice)

    for payload in contrasts:
        payload.setdefault("anchor_candidate", dict(anchor_keys.get(str(payload.get("anchor_key")), {})))
    return contrasts[:n_contrast]


def _select_warm_start_from_regions(
    *,
    full_pool: list[dict[str, Any]],
    variables: list[dict[str, Any]],
    target: int,
    region_guidance: dict[str, Any],
    observed_keys: set[str],
) -> list[dict[str, Any]]:
    if target <= 0 or not full_pool:
        return []

    slot_targets = _allocate_slot_targets(target)
    anchor_regions: list[RegionSpec] = list(region_guidance.get("anchor_regions", []))
    wildcard_regions: list[RegionSpec] = list(region_guidance.get("wildcard_regions", []))
    contrast_priority: list[str] = list(region_guidance.get("contrast_variable_priority", []))

    selected_keys = set(observed_keys)
    anchor_records: list[tuple[dict[str, Any], RegionSpec | None]] = []
    wildcard_records: list[tuple[dict[str, Any], RegionSpec | None]] = []

    for region in anchor_regions:
        if len(anchor_records) >= slot_targets.get("anchor", 0):
            break
        if region.quota <= 0:
            continue
        matched = _candidates_matching_region(region, full_pool, variables, selected_keys)
        sampled = _farthest_first_sample(
            matched,
            variables,
            min(region.quota, slot_targets.get("anchor", 0) - len(anchor_records)),
            initial_selected=[candidate for candidate, _region in anchor_records],
        )
        for candidate in sampled:
            key = candidate_to_key(candidate)
            if key in selected_keys:
                continue
            anchor_records.append((dict(candidate), region))
            selected_keys.add(key)

    contrast_payloads = _generate_contrast_candidates(
        anchor_candidates=[candidate for candidate, _region in anchor_records],
        contrast_var_priority=contrast_priority,
        full_pool=full_pool,
        variables=variables,
        n_contrast=slot_targets.get("contrast", 0),
        excluded_keys=selected_keys,
    )
    for payload in contrast_payloads:
        selected_keys.add(candidate_to_key(payload["candidate"]))

    for region in wildcard_regions:
        if len(wildcard_records) >= slot_targets.get("wildcard", 0):
            break
        if region.quota <= 0:
            continue
        matched = _candidates_matching_region(region, full_pool, variables, selected_keys)
        sampled = _farthest_first_sample(
            matched,
            variables,
            min(region.quota, slot_targets.get("wildcard", 0) - len(wildcard_records)),
            initial_selected=[candidate for candidate, _region in anchor_records]
            + [payload["candidate"] for payload in contrast_payloads]
            + [candidate for candidate, _region in wildcard_records],
        )
        for candidate in sampled:
            key = candidate_to_key(candidate)
            if key in selected_keys:
                continue
            wildcard_records.append((dict(candidate), region))
            selected_keys.add(key)

    selected_count = len(anchor_records) + len(contrast_payloads) + len(wildcard_records)
    if selected_count < target:
        fallback_pool = [
            dict(candidate)
            for candidate in full_pool
            if candidate_to_key(candidate) not in selected_keys
        ]
        extra = _farthest_first_sample(
            fallback_pool,
            variables,
            target - selected_count,
            initial_selected=[candidate for candidate, _region in anchor_records]
            + [payload["candidate"] for payload in contrast_payloads]
            + [candidate for candidate, _region in wildcard_records],
        )
        for candidate in extra:
            key = candidate_to_key(candidate)
            if key in selected_keys:
                continue
            wildcard_records.append((dict(candidate), None))
            selected_keys.add(key)

    shortlist: list[dict[str, Any]] = []
    for candidate, region in anchor_records:
        key = candidate_to_key(candidate)
        rationale_parts = ["Selected as anchor slot."]
        if region is not None:
            rationale_parts.append(f"Region: {region.name}.")
            if region.reason:
                rationale_parts.append(region.reason)
            if region.knowledge_card_ids:
                rationale_parts.append(f"Supported by knowledge cards: {', '.join(region.knowledge_card_ids)}.")
        shortlist.append(
            {
                "candidate": candidate,
                "predicted_value": None,
                "uncertainty": None,
                "acquisition_value": None,
                "constraint_violations": [],
                "constraint_satisfied": True,
                "warm_start_category": "anchor",
                "warm_start_rationale": " ".join(rationale_parts),
                "warm_start_card_refs": list(region.knowledge_card_ids) if region is not None else [],
                "warm_start_index": -1,
                "_warm_start_anchor_key": key,
            }
        )

    for payload in contrast_payloads:
        anchor_key = str(payload.get("anchor_key") or "")
        anchor_region = next(
            (region for candidate, region in anchor_records if candidate_to_key(candidate) == anchor_key),
            None,
        )
        rationale_parts = ["Auto-generated contrast point to help GP learn main-effect directions."]
        if payload.get("contrast_variables"):
            rationale_parts.append(
                "Contrast variables: " + ", ".join(str(item) for item in payload.get("contrast_variables", [])) + "."
            )
        if anchor_region is not None:
            rationale_parts.append(f"Paired with anchor region {anchor_region.name}.")
        shortlist.append(
            {
                "candidate": dict(payload["candidate"]),
                "predicted_value": None,
                "uncertainty": None,
                "acquisition_value": None,
                "constraint_violations": [],
                "constraint_satisfied": True,
                "warm_start_category": "contrast",
                "warm_start_rationale": " ".join(rationale_parts),
                "warm_start_card_refs": list(anchor_region.knowledge_card_ids) if anchor_region is not None else [],
                "warm_start_index": -1,
                "_warm_start_pair_anchor_key": anchor_key,
                "_warm_start_pair_rank": int(payload.get("pair_rank", 0) or 0),
            }
        )

    for candidate, region in wildcard_records:
        rationale_parts = ["Selected as wildcard slot."]
        if region is not None:
            rationale_parts.append(f"Region: {region.name}.")
            if region.reason:
                rationale_parts.append(region.reason)
            if region.knowledge_card_ids:
                rationale_parts.append(f"Supported by knowledge cards: {', '.join(region.knowledge_card_ids)}.")
        else:
            rationale_parts.append("Backfilled from the remaining feasible space to preserve warm-start coverage.")
        shortlist.append(
            {
                "candidate": candidate,
                "predicted_value": None,
                "uncertainty": None,
                "acquisition_value": None,
                "constraint_violations": [],
                "constraint_satisfied": True,
                "warm_start_category": "wildcard",
                "warm_start_rationale": " ".join(rationale_parts),
                "warm_start_card_refs": list(region.knowledge_card_ids) if region is not None else [],
                "warm_start_index": -1,
            }
        )

    return shortlist[:target]


def _variable_domain_labels(variable: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for entry in variable.get("domain", []):
        if isinstance(entry, dict):
            labels.append(str(entry.get("label") or entry.get("name") or entry.get("value") or entry))
        else:
            labels.append(str(entry))
    return labels


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
    knowledge_cards_text = str(context.get("knowledge_cards_text") or "")
    compact_context = {key: value for key, value in context.items() if key not in {"knowledge_cards_text", "knowledge_cards"}}
    return f"""Design deterministic guidance for the warm-start planner.

CONTEXT:
{compact_json(compact_context)}

{knowledge_cards_text}

DOE_POOL:
{compact_json(pool_summary)}

Rules:
- You are NOT selecting final candidates directly. The deterministic planner will do that.
- Recommend patterns, value preferences, and bucket targets that help the planner balance coverage and chemistry-guided exploitation.
- Use Active Knowledge Cards as text context. Cite card IDs in knowledge_card_ids when they influence preferred or avoided patterns.
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
    knowledge_cards: list[dict[str, Any]],
    llm_guidance: dict[str, Any],
) -> list[dict[str, Any]]:
    if target <= 0 or not doe_pool:
        return []

    bias_map: dict[str, dict[str, float]] = {}
    category_targets = dict(llm_guidance.get("category_targets", _normalize_category_targets({}, target)))
    preferred_patterns = llm_guidance.get("preferred_patterns", [])
    avoided_patterns = llm_guidance.get("avoided_patterns", [])
    priority_indices = set(llm_guidance.get("priority_indices", []))

    candidate_features = []
    for index, candidate in enumerate(doe_pool):
        feature = {
            "index": index,
            "candidate": candidate,
            "knowledge_bias_score": _knowledge_bias_score(candidate, bias_map),
            "llm_pattern_score": _pattern_score(candidate, variables, preferred_patterns, avoided_patterns),
            "priority_index_bonus": 1.0 / (1 + max(llm_guidance.get("priority_indices", []).index(index), 0))
            if index in priority_indices
            else 0.0,
            "card_refs": _relevant_card_refs(candidate, knowledge_cards, bias_map, preferred_patterns),
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


def _sort_warm_start_queue(
    shortlist: list[dict[str, Any]],
    variables: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    anchors = [dict(item) for item in shortlist if item.get("warm_start_category") == "anchor"]
    contrasts = [dict(item) for item in shortlist if item.get("warm_start_category") == "contrast"]
    wildcards = [dict(item) for item in shortlist if item.get("warm_start_category") == "wildcard"]

    ordered: list[dict[str, Any]] = []
    ordered_anchors = _order_bucket_by_distance(anchors, variables)
    contrast_by_anchor: dict[str, list[dict[str, Any]]] = {}
    extra_contrasts: list[dict[str, Any]] = []
    for item in contrasts:
        anchor_key = str(item.get("_warm_start_pair_anchor_key") or "")
        if anchor_key:
            contrast_by_anchor.setdefault(anchor_key, []).append(item)
        else:
            extra_contrasts.append(item)
    for anchor_key in list(contrast_by_anchor):
        contrast_by_anchor[anchor_key].sort(
            key=lambda item: (
                int(item.get("_warm_start_pair_rank", 0) or 0),
                candidate_to_key(item.get("candidate", {})),
            )
        )

    for anchor in ordered_anchors:
        ordered.append(anchor)
        anchor_key = str(anchor.get("_warm_start_anchor_key") or candidate_to_key(anchor.get("candidate", {})))
        pair_bucket = contrast_by_anchor.get(anchor_key, [])
        if pair_bucket:
            ordered.append(pair_bucket.pop(0))
        extra_contrasts.extend(pair_bucket)

    if extra_contrasts:
        extra_contrasts.sort(
            key=lambda item: (
                str(item.get("_warm_start_pair_anchor_key") or ""),
                int(item.get("_warm_start_pair_rank", 0) or 0),
                candidate_to_key(item.get("candidate", {})),
            )
        )
        ordered.extend(extra_contrasts)

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
