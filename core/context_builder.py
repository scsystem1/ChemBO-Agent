"""
Context builders for node-specific LLM inputs.
"""
from __future__ import annotations

from typing import Any

from core.problem_loader import resolve_campaign_budget
from knowledge.knowledge_card import format_deck_for_prompt
from knowledge.knowledge_state import knowledge_mode_from_deck


class ContextBuilder:
    """Assemble compact node-specific context blocks for the ChemBO graph."""

    @staticmethod
    def for_generate_hypotheses(state: dict[str, Any], memory_manager) -> dict[str, Any]:
        return {
            "problem_features": _problem_features(state.get("problem_spec", {})),
            "knowledge_cards_text": _deck_text_for_prompt(state, "hypothesis_generation", max_cards=10),
            "knowledge_cards": _deck_cards_for_node(state, "hypothesis_generation", max_cards=10),
            "knowledge_mode": knowledge_mode_from_deck(state.get("knowledge_deck", {})),
            "memory_packet": memory_manager.build_memory_packet(
                "generate_hypotheses",
                state,
                {"observations": state.get("observations", [])},
            ),
        }

    @staticmethod
    def for_warm_start(state: dict[str, Any], warm_start_target: int) -> dict[str, Any]:
        problem = state.get("problem_spec", {})
        knowledge_max_cards = max(12, min(2 * int(warm_start_target or 0), 30))
        return {
            "problem_features": _problem_features(problem),
            "knowledge_cards_text": _deck_text_for_prompt(state, "warm_start", max_cards=min(10, knowledge_max_cards)),
            "knowledge_cards": _deck_cards_for_node(state, "warm_start", max_cards=min(10, knowledge_max_cards)),
            "knowledge_mode": knowledge_mode_from_deck(state.get("knowledge_deck", {})),
            "proposal_value_guide": [_proposal_value_spec(variable) for variable in problem.get("variables", [])],
            "constraints": problem.get("constraints", []),
            "active_hypotheses": _active_hypotheses(state.get("hypotheses", [])),
            "warm_start_target": int(warm_start_target or 0),
            "dataset_backed": isinstance(problem.get("dataset"), dict),
        }

    @staticmethod
    def for_select_candidate(state: dict[str, Any], memory_manager) -> dict[str, Any]:
        return {
            "shortlist": state.get("proposal_shortlist", [])[:4],
            "active_hypotheses": _active_hypotheses(state.get("hypotheses", []))[:4],
            "knowledge_cards_text": _deck_text_for_prompt(state, "select_candidate", max_cards=10),
            "knowledge_cards": _deck_cards_for_node(state, "select_candidate", max_cards=10),
            "knowledge_mode": knowledge_mode_from_deck(state.get("knowledge_deck", {})),
            "best_so_far": {
                "result": state.get("best_result"),
                "candidate": state.get("best_candidate", {}),
            },
            "memory_packet": memory_manager.build_memory_packet(
                "select_candidate",
                state,
                {"candidate": (state.get("proposal_shortlist", [{}])[0] or {}).get("candidate", {})},
            ),
        }

    @staticmethod
    def for_interpret_results(state: dict[str, Any], memory_manager) -> dict[str, Any]:
        latest = state.get("observations", [])[-1] if state.get("observations") else {}
        return {
            "latest_observation": latest,
            "active_hypotheses": _active_hypotheses(state.get("hypotheses", [])),
            "knowledge_cards_text": _deck_text_for_prompt(state, "result_interpretation", max_cards=10),
            "knowledge_cards": _deck_cards_for_node(state, "result_interpretation", max_cards=10),
            "knowledge_mode": knowledge_mode_from_deck(state.get("knowledge_deck", {})),
            "memory_packet": memory_manager.build_memory_packet(
                "interpret_results",
                state,
                {"candidate": latest.get("candidate", {})},
            ),
        }

    @staticmethod
    def for_reflect_and_decide(state: dict[str, Any], memory_manager) -> dict[str, Any]:
        problem = state.get("problem_spec", {})
        budget = resolve_campaign_budget(problem, _ContextSettingsAdapter())
        return {
            "convergence_state": state.get("convergence_state", {}),
            "budget_status": {"used": len(state.get("observations", [])), "total": budget},
            "current_bo_config": state.get("bo_config", {}),
            "autobo_digest": _build_autobo_digest(state.get("autobo_state", {})),
            "knowledge_cards_text": _deck_text_for_prompt(state, "run_bo_iteration", max_cards=8),
            "knowledge_mode": knowledge_mode_from_deck(state.get("knowledge_deck", {})),
            "hypotheses_status": _hypothesis_status_summary(state.get("hypotheses", [])),
            "memory_packet": memory_manager.build_memory_packet(
                "reflect_and_decide",
                state,
                {"performance_log": state.get("performance_log", [])},
            ),
        }

    @staticmethod
    def for_autobo_surrogate_eval(state: dict[str, Any], memory_manager) -> dict[str, Any]:
        observations = [item for item in state.get("observations", []) if item.get("result") is not None]
        direction = str(state.get("optimization_direction", "maximize")).strip().lower()
        ranked = sorted(
            observations,
            key=lambda item: float(item.get("result", 0.0)),
            reverse=direction != "minimize",
        )
        return {
            "reaction_context": {
                "reaction_type": state.get("problem_spec", {}).get("reaction_type", ""),
                "target_metric": state.get("problem_spec", {}).get("target_metric", "yield"),
                "optimization_direction": direction,
            },
            "top_observations": ranked[:5],
            "bottom_observations": ranked[-3:] if len(ranked) > 3 else ranked[:],
            "knowledge_cards_text": _deck_text_for_prompt(state, "run_bo_iteration", max_cards=6),
            "knowledge_cards": _deck_cards_for_node(state, "run_bo_iteration", max_cards=6),
            "knowledge_mode": knowledge_mode_from_deck(state.get("knowledge_deck", {})),
            "memory_rules": [node.compact() for node in memory_manager.semantic_graph.query_rules(limit=4)],
            "active_model": (state.get("autobo_state", {}) or {}).get("active_model", ""),
        }

    @staticmethod
    def for_autobo_acquisition_select(state: dict[str, Any], memory_manager) -> dict[str, Any]:
        observations = [item for item in state.get("observations", []) if item.get("result") is not None]
        direction = str(state.get("optimization_direction", "maximize")).strip().lower()
        ranked = sorted(
            observations,
            key=lambda item: float(item.get("result", 0.0)),
            reverse=direction != "minimize",
        )
        return {
            "reaction_context": {
                "reaction_type": state.get("problem_spec", {}).get("reaction_type", ""),
                "target_metric": state.get("problem_spec", {}).get("target_metric", "yield"),
                "optimization_direction": direction,
            },
            "top_observations": ranked[:3],
            "bottom_observations": ranked[-3:] if len(ranked) > 3 else ranked[:],
            "knowledge_cards_text": _deck_text_for_prompt(state, "select_candidate", max_cards=6),
            "knowledge_cards": _deck_cards_for_node(state, "select_candidate", max_cards=6),
            "knowledge_mode": knowledge_mode_from_deck(state.get("knowledge_deck", {})),
            "memory_rules": [node.compact() for node in memory_manager.semantic_graph.query_rules(limit=4)],
            "active_hypotheses": _active_hypotheses(state.get("hypotheses", []))[:4],
            "total_observations": len(observations),
            "shortlist": state.get("proposal_shortlist", []),
            "memory_packet": memory_manager.build_memory_packet("select_candidate", state, {}),
        }


def _problem_features(problem_spec: dict[str, Any]) -> dict[str, Any]:
    variables = problem_spec.get("variables", [])
    categorical = [v for v in variables if v.get("type") != "continuous"]
    continuous = [v for v in variables if v.get("type") == "continuous"]
    return {
        "reaction_type": problem_spec.get("reaction_type", ""),
        "target_metric": problem_spec.get("target_metric", "yield"),
        "optimization_direction": problem_spec.get("optimization_direction", "maximize"),
        "budget": problem_spec.get("budget", 40),
        "num_variables": len(variables),
        "num_categoricals": len(categorical),
        "num_continuous": len(continuous),
        "total_categories": sum(len(variable.get("domain", [])) for variable in categorical),
        "has_smiles": any(bool(variable.get("smiles_map")) for variable in variables),
    }


def _deck_cards_for_node(state: dict[str, Any], current_node: str, max_cards: int) -> list[dict[str, Any]]:
    deck = state.get("knowledge_deck", {}) if isinstance(state.get("knowledge_deck"), dict) else {}
    cards = deck.get("cards", []) if isinstance(deck.get("cards"), list) else []
    selected = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        if str(card.get("status") or "active") not in {"active", "validated"}:
            continue
        actionable = [str(item).strip() for item in card.get("actionable_for", []) if str(item).strip()]
        if current_node and actionable and current_node not in actionable:
            continue
        selected.append(dict(card))
    selected.sort(
        key=lambda card: (
            0 if str(card.get("card_type") or "") == "constraint" else 1,
            {"target": 0, "campaign": 1, "analogous": 2, "general": 3}.get(str(card.get("scope") or "general"), 99),
            -float(card.get("confidence", 0.0) or 0.0),
            str(card.get("card_id") or ""),
        )
    )
    return selected[: max(0, int(max_cards or 0))]


def _deck_text_for_prompt(state: dict[str, Any], current_node: str, max_cards: int) -> str:
    deck = state.get("knowledge_deck", {}) if isinstance(state.get("knowledge_deck"), dict) else {}
    cards = deck.get("cards", []) if isinstance(deck.get("cards"), list) else []
    text = format_deck_for_prompt(cards, current_node, max_cards=max_cards)
    summary = deck.get("build_summary", {}) if isinstance(deck.get("build_summary"), dict) else {}
    coverage = str(summary.get("coverage_level") or "").strip()
    if coverage:
        text += f"\n[Knowledge Coverage: {coverage}]"
    return text


class _ContextSettingsAdapter:
    max_bo_iterations = 40


def _variable_summary(variable: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": variable.get("name"),
        "type": variable.get("type", "categorical"),
        "domain_size": len(variable.get("domain", [])),
        "has_smiles": bool(variable.get("smiles_map")),
        "description": variable.get("description", ""),
    }


def _proposal_value_spec(variable: dict[str, Any]) -> dict[str, Any]:
    if variable.get("type") == "continuous":
        domain = list(variable.get("domain", [0.0, 1.0]))
        return {
            "name": variable.get("name"),
            "type": "continuous",
            "range": domain[:2],
            "unit": variable.get("unit", ""),
            "description": variable.get("description", ""),
        }
    return {
        "name": variable.get("name"),
        "type": variable.get("type", "categorical"),
        "allowed_values": list(variable.get("domain", [])),
        "unit": variable.get("unit", ""),
        "description": variable.get("description", ""),
    }


def _active_hypotheses(hypotheses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in hypotheses if item.get("status") in {"active", "supported"}]


def _hypothesis_status_summary(hypotheses: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in hypotheses:
        status = str(item.get("status", "active"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def _config_history_summary(config_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "version": item.get("config_version"),
            "surrogate_model": item.get("surrogate_model"),
            "kernel": item.get("kernel_config", {}).get("key"),
            "acquisition_function": item.get("acquisition_function"),
            "validated": item.get("validated", True),
        }
        for item in config_history[-5:]
    ]


def _build_autobo_digest(autobo_state: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(autobo_state, dict) or not autobo_state:
        return {}
    fitness_log = autobo_state.get("fitness_log", {}) if isinstance(autobo_state.get("fitness_log"), dict) else {}
    latest_key = None
    numeric_keys = [key for key in fitness_log if str(key).isdigit()]
    if numeric_keys:
        latest_key = max(numeric_keys, key=lambda key: int(str(key)))
    calibration_log = autobo_state.get("calibration_log", []) if isinstance(autobo_state.get("calibration_log"), list) else []
    switch_history = autobo_state.get("switch_history", []) if isinstance(autobo_state.get("switch_history"), list) else []
    return {
        "active_model": autobo_state.get("active_model"),
        "effective_llm_weight": autobo_state.get("effective_llm_weight"),
        "latest_composite": fitness_log.get(latest_key, {}) if latest_key is not None else {},
        "latest_calibration": calibration_log[-1] if calibration_log else {},
        "recent_switch": switch_history[-1] if switch_history else {},
        "switches_total": len(switch_history),
        "last_layer2_iteration": int(autobo_state.get("last_layer2_iteration", 0) or 0),
    }
