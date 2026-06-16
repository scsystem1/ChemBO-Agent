"""Application-domain helpers for prompt and workflow specialization."""
from __future__ import annotations

from typing import Any


CHEMISTRY_DOMAIN = "chemistry"
HPO_DOMAIN = "hpo"
HPO_PROFILE = "ml_hyperparameter_optimization"


def application_domain(problem_spec: dict[str, Any] | None) -> str:
    """Return the normalized high-level application domain for a problem spec."""
    spec = problem_spec if isinstance(problem_spec, dict) else {}
    raw = str(spec.get("application_domain") or spec.get("domain") or "").strip().lower()
    profile = str(spec.get("domain_profile") or "").strip().lower()
    reaction_type = str(spec.get("reaction_type") or "").strip().upper()
    if raw in {"hpo", "ml", "machine_learning", "machine-learning"}:
        return HPO_DOMAIN
    if profile == HPO_PROFILE or reaction_type.startswith("HPOBENCH"):
        return HPO_DOMAIN
    return CHEMISTRY_DOMAIN


def domain_profile(problem_spec: dict[str, Any] | None) -> str:
    spec = problem_spec if isinstance(problem_spec, dict) else {}
    explicit = str(spec.get("domain_profile") or "").strip()
    if explicit:
        return explicit
    return HPO_PROFILE if application_domain(spec) == HPO_DOMAIN else CHEMISTRY_DOMAIN


def is_hpo_domain(problem_spec: dict[str, Any] | None) -> bool:
    return application_domain(problem_spec) == HPO_DOMAIN


def is_chemistry_domain(problem_spec: dict[str, Any] | None) -> bool:
    return application_domain(problem_spec) == CHEMISTRY_DOMAIN


def domain_terms(problem_spec: dict[str, Any] | None) -> dict[str, str]:
    """Compact wording palette used by prompt builders."""
    if is_hpo_domain(problem_spec):
        return {
            "agent_identity": "an expert AI system for machine learning hyperparameter optimization using Bayesian Optimization",
            "assistant_identity": "a computation assistant for Bayesian optimization of machine learning hyperparameters",
            "expert_directive": (
                "Act as a machine learning hyperparameter optimization expert: reason about model capacity, "
                "regularization, learning dynamics, fidelity/budget tradeoffs, validation noise, and overfitting or underfitting."
            ),
            "knowledge_guidance": (
                "Use machine learning and HPO knowledge about hyperparameter sensitivity, parameter interactions, "
                "training stability, model complexity, and validation performance. Do not use unrelated application-domain reasoning."
            ),
            "optimization_campaign": "machine learning hyperparameter optimization campaign",
            "optimization_problem": "machine learning hyperparameter optimization problem",
            "context_header": "HPO Context",
            "plausible": "ML-plausible",
            "intuition": "machine learning and HPO intuition",
            "expectations": "machine learning performance expectations",
            "reasoning": "HPO reasoning",
            "evidence_type": "domain",
            "evidence_argument": "domain_argument",
            "effect_rule": "parameter_effect",
            "knowledge_noun": "HPO prior",
            "expert": "machine learning hyperparameter tuning expert",
        }
    return {
        "agent_identity": "an expert AI system for chemical reaction optimization using Bayesian Optimization",
        "assistant_identity": "a computation assistant for Bayesian optimization of chemical reactions",
        "expert_directive": (
            "Act as a chemical reaction optimization expert: reason about mechanism, reagent properties, "
            "catalyst behavior, solvent/base effects, operating windows, and reaction failure modes."
        ),
        "knowledge_guidance": (
            "Use chemistry knowledge about mechanism, molecular or material properties, reagent interactions, "
            "operating conditions, and plausible reaction outcomes."
        ),
        "optimization_campaign": "chemical reaction optimization campaign",
        "optimization_problem": "chemical optimization problem",
        "context_header": "Reaction Context",
        "plausible": "chemically plausible",
        "intuition": "chemistry intuition",
        "expectations": "chemical expectations",
        "reasoning": "chemical reasoning",
        "evidence_type": "chemistry",
        "evidence_argument": "chemistry_argument",
        "effect_rule": "chemical_effect",
        "knowledge_noun": "chemistry",
        "expert": "chemical reaction optimization expert",
    }


def problem_from_context(context: dict[str, Any] | None) -> dict[str, Any]:
    """Extract a lightweight problem-like payload from prompt context."""
    ctx = context if isinstance(context, dict) else {}
    if "problem_spec" in ctx and isinstance(ctx["problem_spec"], dict):
        return dict(ctx["problem_spec"])
    if "problem_features" in ctx and isinstance(ctx["problem_features"], dict):
        return dict(ctx["problem_features"])
    return dict(ctx)


__all__ = [
    "CHEMISTRY_DOMAIN",
    "HPO_DOMAIN",
    "HPO_PROFILE",
    "application_domain",
    "domain_profile",
    "domain_terms",
    "is_chemistry_domain",
    "is_hpo_domain",
    "problem_from_context",
]
