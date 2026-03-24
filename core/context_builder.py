"""
Context builders for node-specific LLM inputs.
"""
from __future__ import annotations

from typing import Any

from core.problem_loader import resolve_campaign_budget


class ContextBuilder:
    """Assemble compact node-specific context blocks for the ChemBO graph."""

    @staticmethod
    def for_select_embedding(state: dict[str, Any]) -> dict[str, Any]:
        problem = state.get("problem_spec", {})
        variables = problem.get("variables", [])
        return {
            "problem_features": _problem_features(problem),
            "variable_summary": [_variable_summary(variable) for variable in variables],
            "kb_context": state.get("kb_context", ""),
            "kb_priors": state.get("kb_priors", {}),
        }

    @staticmethod
    def for_generate_hypotheses(state: dict[str, Any], memory_manager) -> dict[str, Any]:
        return {
            "problem_features": _problem_features(state.get("problem_spec", {})),
            "kb_context": state.get("kb_context", ""),
            "kb_priors": state.get("kb_priors", {}),
            "observations": state.get("observations", [])[-5:],
            "memory_context": memory_manager.get_context_for_node(
                "generate_hypotheses",
                {"observations": state.get("observations", [])},
            ),
        }

    @staticmethod
    def for_configure_bo(state: dict[str, Any], memory_manager) -> dict[str, Any]:
        return {
            "problem_features": _problem_features(state.get("problem_spec", {})),
            "embedding_info": state.get("embedding_config", {}),
            "data_volume": len(state.get("observations", [])),
            "active_hypotheses": _active_hypotheses(state.get("hypotheses", [])),
            "config_history_summary": _config_history_summary(state.get("config_history", [])),
            "memory_context": memory_manager.get_context_for_node(
                "configure_bo",
                {"config_history": state.get("config_history", [])},
            ),
        }

    @staticmethod
    def for_warm_start(state: dict[str, Any]) -> dict[str, Any]:
        return {
            "problem_features": _problem_features(state.get("problem_spec", {})),
            "kb_priors": state.get("kb_priors", {}),
            "constraints": state.get("problem_spec", {}).get("constraints", []),
            "active_hypotheses": _active_hypotheses(state.get("hypotheses", [])),
        }

    @staticmethod
    def for_select_candidate(state: dict[str, Any], memory_manager) -> dict[str, Any]:
        return {
            "shortlist": state.get("proposal_shortlist", []),
            "active_hypotheses": _active_hypotheses(state.get("hypotheses", [])),
            "constraints": state.get("problem_spec", {}).get("constraints", []),
            "kb_context": state.get("kb_context", ""),
            "best_so_far": {
                "result": state.get("best_result"),
                "candidate": state.get("best_candidate", {}),
            },
            "memory_context": memory_manager.get_context_for_node(
                "select_candidate",
                {"candidates": [item.get("candidate", {}) for item in state.get("proposal_shortlist", [])]},
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
            "memory_context": memory_manager.get_context_for_node(
                "interpret_results",
                {"latest_candidate": latest.get("candidate", {})},
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
            "config_history": state.get("config_history", []),
            "reconfig_history": state.get("reconfig_history", []),
            "hypotheses_status": _hypothesis_status_summary(state.get("hypotheses", [])),
            "memory_context": memory_manager.get_context_for_node(
                "reflect_and_decide",
                {"performance_log": state.get("performance_log", [])},
            ),
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


class _ContextSettingsAdapter:
    max_bo_iterations = 30


def _variable_summary(variable: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": variable.get("name"),
        "type": variable.get("type", "categorical"),
        "domain_size": len(variable.get("domain", [])),
        "has_smiles": bool(variable.get("smiles_map")),
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
