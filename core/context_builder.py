"""
Context builders for node-specific LLM inputs.
"""
from __future__ import annotations

from typing import Any

from core.problem_loader import resolve_campaign_budget
from knowledge.knowledge_card import build_knowledge_guidance


class ContextBuilder:
    """Assemble compact node-specific context blocks for the ChemBO graph."""

    @staticmethod
    def for_generate_hypotheses(state: dict[str, Any], memory_manager) -> dict[str, Any]:
        return {
            "problem_features": _problem_features(state.get("problem_spec", {})),
            "knowledge_guidance": _knowledge_guidance(state, "hypothesis_generation", max_cards=12),
            "observations": state.get("observations", [])[-5:],
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
            "knowledge_guidance": _knowledge_guidance(state, "warm_start", max_cards=knowledge_max_cards),
            "proposal_value_guide": [_proposal_value_spec(variable) for variable in problem.get("variables", [])],
            "constraints": problem.get("constraints", []),
            "active_hypotheses": _active_hypotheses(state.get("hypotheses", [])),
            "warm_start_target": int(warm_start_target or 0),
            "dataset_backed": isinstance(problem.get("dataset"), dict),
        }

    @staticmethod
    def for_select_candidate(state: dict[str, Any], memory_manager) -> dict[str, Any]:
        return {
            "shortlist": state.get("proposal_shortlist", []),
            "active_hypotheses": _active_hypotheses(state.get("hypotheses", [])),
            "constraints": state.get("problem_spec", {}).get("constraints", []),
            "knowledge_guidance": _knowledge_guidance(state, "", max_cards=10),
            "best_so_far": {
                "result": state.get("best_result"),
                "candidate": state.get("best_candidate", {}),
            },
            "memory_packet": memory_manager.build_memory_packet(
                "select_candidate",
                state,
                {"candidate": (state.get("proposal_shortlist", [{}])[0] or {}).get("candidate", {})},
            ),
            "recent_observations": state.get("observations", [])[-5:],
        }

    @staticmethod
    def for_interpret_results(state: dict[str, Any], memory_manager) -> dict[str, Any]:
        latest = state.get("observations", [])[-1] if state.get("observations") else {}
        return {
            "latest_observation": latest,
            "observations": state.get("observations", [])[-10:],
            "hypotheses": state.get("hypotheses", []),
            "effective_config": state.get("effective_config", {}),
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
            "performance_log": state.get("performance_log", [])[-10:],
            "convergence_state": state.get("convergence_state", {}),
            "budget_status": {"used": len(state.get("observations", [])), "total": budget},
            "current_bo_config": state.get("bo_config", {}),
            "effective_config": state.get("effective_config", {}),
            "config_history": state.get("config_history", []),
            "autobo_state": state.get("autobo_state", {}),
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
            "knowledge_guidance": _knowledge_guidance(state, "select_candidate", max_cards=6),
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
            "knowledge_guidance": _knowledge_guidance(state, "select_candidate", max_cards=6),
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
        "budget": problem_spec.get("budget", 30),
        "num_variables": len(variables),
        "num_categoricals": len(categorical),
        "num_continuous": len(continuous),
        "total_categories": sum(len(variable.get("domain", [])) for variable in categorical),
        "has_smiles": any(bool(variable.get("smiles_map")) for variable in variables),
    }


def _knowledge_guidance(state: dict[str, Any], current_node: str, max_cards: int) -> list[dict[str, Any]]:
    problem = state.get("problem_spec", {})
    return build_knowledge_guidance(
        state.get("knowledge_cards", []),
        current_node=current_node,
        variables=problem.get("variables", []),
        max_cards=max_cards,
    )


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
