"""
ChemBO Agent tools and BO execution helpers.
"""
from __future__ import annotations

import json
from typing import Any

import numpy as np
from langchain_core.tools import tool

from knowledge.reaction_kb import get_hard_constraints
from pools.component_pools import (
    candidate_to_key,
    create_acquisition,
    create_encoder,
    create_surrogate,
    detect_runtime_capabilities,
    enumerate_discrete_candidates,
    get_af_options,
    get_embedding_options,
    get_kernel_options,
    get_surrogate_options,
    hybrid_sample_candidates,
)


@tool
def embedding_method_advisor(
    problem_summary: str,
    variable_types: str,
    num_categoricals: int,
    num_continuous: int,
    has_smiles: bool,
    data_volume: int,
) -> str:
    """Return structured embedding options with selection guidance."""
    options = get_embedding_options()
    scored = []
    for option in options:
        tags = option["tags"]
        notes = []
        if has_smiles and tags.get("chemistry_aware"):
            notes.append("+captures chemistry-aware similarity")
        if not has_smiles and tags.get("chemistry_aware"):
            notes.append("-chemistry-aware path may be wasted without SMILES metadata")
        if data_volume < 10 and "very low data (<10 observations)" in tags.get("best_for", []):
            notes.append("+explicitly recommended for very low data")
        if num_categoricals > 4 and option["key"] == "one_hot":
            notes.append("-may grow quickly with many categorical values")
        if option["availability"]["is_available"]:
            notes.append("+available in current runtime")
        else:
            notes.append(f"-currently unavailable: {option['availability']['missing_dependencies']}")
        scored.append({**option, "suitability_notes": notes})

    return json.dumps(
        {
            "available_options": scored,
            "problem_context": {
                "problem_summary": problem_summary,
                "variable_types": variable_types,
                "num_categoricals": num_categoricals,
                "num_continuous": num_continuous,
                "has_smiles": has_smiles,
                "data_volume": data_volume,
                "runtime_capabilities": detect_runtime_capabilities(),
            },
            "instruction": (
                "Select exactly one embedding method by key. Prefer a method that is available, "
                "matches the problem structure, and is credible for the current data regime."
            ),
        },
        indent=2,
    )


@tool
def surrogate_model_selector(
    problem_summary: str,
    embedding_method: str,
    embedding_dim: int,
    num_variables: int,
    num_categoricals: int,
    expected_data_volume: int,
    noise_level: str,
) -> str:
    """Return structured surrogate and kernel options with guidance."""
    surrogate_options = get_surrogate_options()
    kernel_options = get_kernel_options()
    scored_models = []
    for option in surrogate_options:
        tags = option["tags"]
        notes = []
        if option["key"] == "gp":
            notes.append("+default uncertainty-aware choice")
            if embedding_dim > 40:
                notes.append("-embedding is fairly high-dimensional for standard GP")
        if option["key"] == "random_forest" and noise_level in {"medium", "high"}:
            notes.append("+robust to noisy observations")
        if option["key"] == "dkl" and embedding_method == "llm_embedding":
            notes.append("+best conceptual match for semantic embeddings")
        if expected_data_volume < 20 and option["key"] == "dkl":
            notes.append("-usually not worth it under 20 observations")
        if option["availability"]["is_available"]:
            notes.append("+available in current runtime")
        else:
            notes.append(f"-currently unavailable: {option['availability']['missing_dependencies']}")
        scored_models.append({**option, "suitability_notes": notes})

    scored_kernels = []
    for option in kernel_options:
        tags = option["tags"]
        notes = []
        if num_categoricals > 0 and option["key"] in {"sum_kernel", "product_kernel", "mixed_sum_product"}:
            notes.append("+mixed-space aware approximation")
        if option["key"] == "matern52":
            notes.append("+safe general-purpose default")
        if option["key"] == "matern32" and noise_level in {"medium", "high"}:
            notes.append("+more tolerant of rougher surfaces")
        if embedding_dim > 30 and option["key"] == "rbf":
            notes.append("-strong smoothness prior may be too rigid")
        scored_kernels.append({**option, "suitability_notes": notes})

    return json.dumps(
        {
            "surrogate_options": scored_models,
            "kernel_options": scored_kernels,
            "context": {
                "problem_summary": problem_summary,
                "embedding_method": embedding_method,
                "embedding_dim": embedding_dim,
                "num_variables": num_variables,
                "num_categoricals": num_categoricals,
                "expected_data_volume": expected_data_volume,
                "noise_level": noise_level,
                "runtime_capabilities": detect_runtime_capabilities(),
            },
            "instruction": (
                "Select one surrogate family and, if GP is selected, one kernel configuration. "
                "Your choice must consider data regime, dimensionality, uncertainty needs, and availability."
            ),
        },
        indent=2,
    )


@tool
def af_selector(
    problem_summary: str,
    surrogate_model: str,
    batch_size: int,
    budget_remaining: int,
    budget_total: int,
    num_objectives: int,
    current_best: float | None = None,
) -> str:
    """Return structured acquisition function options with guidance."""
    options = get_af_options()
    exploration_phase = budget_remaining > budget_total * 0.6
    scored = []
    for option in options:
        tags = option["tags"]
        notes = []
        if option["key"] == "log_ei":
            notes.append("+default stable improvement-based choice")
        if exploration_phase and option["key"] in {"ucb", "ts"}:
            notes.append("+strong early exploration fit")
        if batch_size > 1 and tags.get("batch_support"):
            notes.append("+supports batch reasoning")
        if batch_size > 1 and not tags.get("batch_support"):
            notes.append("-not a natural batch choice")
        if num_objectives > 1 and option["key"] == "qlog_nehvi":
            notes.append("+aligned with multi-objective campaigns")
        if num_objectives == 1 and option["key"] == "qlog_nehvi":
            notes.append("-overkill for single-objective optimization")
        scored.append({**option, "suitability_notes": notes})

    return json.dumps(
        {
            "available_options": scored,
            "context": {
                "problem_summary": problem_summary,
                "surrogate_model": surrogate_model,
                "batch_size": batch_size,
                "budget_remaining": budget_remaining,
                "budget_total": budget_total,
                "campaign_phase": "exploration" if exploration_phase else "exploitation",
                "num_objectives": num_objectives,
                "current_best": current_best,
                "runtime_capabilities": detect_runtime_capabilities(),
            },
            "instruction": (
                "Select one acquisition function by key. Prefer log_ei as the default unless you have a "
                "clear reason to use UCB, TS, or a placeholder advanced policy."
            ),
        },
        indent=2,
    )


@tool
def bo_runner(
    embedding_method: str,
    embedding_params: str,
    surrogate_model: str,
    surrogate_params: str,
    acquisition_function: str,
    af_params: str,
    search_space: str,
    observations: str,
    batch_size: int = 1,
    top_k: int = 5,
    kernel_config: str = "{}",
    reaction_type: str = "",
    kb_priors: str = "{}",
    optimization_direction: str = "maximize",
) -> str:
    """Execute one BO scoring pass and return a shortlist of top candidates."""
    search_space_data = _loads(search_space, [])
    obs_data = _loads(observations, [])
    embedding_params_data = _loads(embedding_params, {})
    surrogate_params_data = _loads(surrogate_params, {})
    af_params_data = _loads(af_params, {})
    kernel_config_data = _loads(kernel_config, {})
    kb_priors_data = _loads(kb_priors, {})

    batch_size = max(1, int(batch_size or 1))
    top_k = max(batch_size, int(top_k or 5))
    seed = int(
        af_params_data.get(
            "random_state",
            surrogate_params_data.get("random_state", embedding_params_data.get("random_state", 0)),
        )
    )
    candidate_pool_size = int(af_params_data.get("candidate_pool_size", 512))
    initial_doe_size = int(af_params_data.get("initial_doe_size", surrogate_params_data.get("initial_doe_size", 5)))
    direction = str(optimization_direction or "maximize").strip().lower()

    encoder = create_encoder(embedding_method, search_space_data, embedding_params_data)
    deduped_observations = _dedupe_observations(obs_data)
    observed_candidates = [record.get("candidate", {}) for record in deduped_observations if record.get("candidate")]
    observed_keys = {candidate_to_key(candidate) for candidate in observed_candidates}
    hard_constraints = get_hard_constraints(reaction_type)

    candidate_pool = _build_candidate_pool(
        search_space_data,
        observed_keys=observed_keys,
        candidate_pool_size=candidate_pool_size,
        seed=seed,
        hard_constraints=hard_constraints,
    )
    if not candidate_pool:
        candidate_pool = generate_warm_start_candidates(
            search_space_data,
            kb_priors_data,
            n_total=top_k,
            n_prior=min(3, top_k),
            seed=seed,
            hard_constraints=hard_constraints,
            observed_keys=observed_keys,
        )

    if len(deduped_observations) < max(2, min(initial_doe_size, 3)):
        shortlist = _build_shortlist_from_candidates(candidate_pool[:top_k], hard_constraints)
        return json.dumps(
            {
                "status": "warm_start_fallback",
                "strategy": "exploration_shortlist",
                "shortlist": shortlist,
                "recommended_index": 0,
                "candidates": [item["candidate"] for item in shortlist],
                "predictions": [item["predicted_value"] for item in shortlist],
                "uncertainties": [item["uncertainty"] for item in shortlist],
                "acquisition_values": [item["acquisition_value"] for item in shortlist],
                "surrogate_metrics": {"model": None, "num_training_points": len(deduped_observations), "log_marginal_likelihood": None},
                "resolved_components": {
                    "embedding_method": encoder.metadata.get("resolved_key", embedding_method),
                    "surrogate_model": surrogate_model,
                    "kernel_config": {"key": resolved_kernel},
                    "acquisition_function": acquisition_function,
                },
                "metadata": {
                    "encoder_notes": encoder.metadata.get("notes", []),
                    "surrogate_notes": [],
                    "acquisition_notes": [],
                    "fallback_reason": "Insufficient observations for stable surrogate fitting.",
                    "candidate_pool_size": len(candidate_pool),
                    "runtime_mode": detect_runtime_capabilities()["runtime_mode"],
                },
            },
            indent=2,
        )

    X_obs, y_obs = _observations_to_training_data(deduped_observations, encoder)
    if X_obs.shape[0] == 0:
        shortlist = _build_shortlist_from_candidates(candidate_pool[:top_k], hard_constraints)
        return json.dumps(
            {
                "status": "warm_start_fallback",
                "strategy": "exploration_shortlist",
                "shortlist": shortlist,
                "recommended_index": 0,
                "candidates": [item["candidate"] for item in shortlist],
                "predictions": [item["predicted_value"] for item in shortlist],
                "uncertainties": [item["uncertainty"] for item in shortlist],
                "acquisition_values": [item["acquisition_value"] for item in shortlist],
                "surrogate_metrics": {"model": None, "num_training_points": 0, "log_marginal_likelihood": None},
                "resolved_components": {
                    "embedding_method": encoder.metadata.get("resolved_key", embedding_method),
                    "surrogate_model": surrogate_model,
                    "kernel_config": {"key": resolved_kernel},
                    "acquisition_function": acquisition_function,
                },
                "metadata": {
                    "encoder_notes": encoder.metadata.get("notes", []),
                    "surrogate_notes": [],
                    "acquisition_notes": [],
                    "fallback_reason": "No valid observations after deduplication.",
                    "candidate_pool_size": len(candidate_pool),
                    "runtime_mode": detect_runtime_capabilities()["runtime_mode"],
                },
            },
            indent=2,
        )

    y_mean = float(np.mean(y_obs))
    y_std = float(np.std(y_obs)) or 1.0
    y_model = y_obs if direction != "minimize" else -1.0 * y_obs
    y_scaled = (y_model - np.mean(y_model)) / (float(np.std(y_model)) or 1.0)

    resolved_kernel = str(kernel_config_data.get("key") or "matern52")
    surrogate = create_surrogate(surrogate_model, surrogate_params_data, resolved_kernel)
    acquisition = create_acquisition(acquisition_function, af_params_data)
    fallback_reason = None

    X_pool = encoder.encode_batch(candidate_pool)
    try:
        surrogate.fit(X_obs, y_scaled)
        pred_mean_scaled, pred_std_scaled = surrogate.predict(X_pool)
    except Exception as exc:  # pragma: no cover
        fallback_reason = f"{type(exc).__name__}: {exc}"
        if surrogate_model != "random_forest":
            surrogate = create_surrogate("random_forest", surrogate_params_data)
            try:
                surrogate.fit(X_obs, y_scaled)
                pred_mean_scaled, pred_std_scaled = surrogate.predict(X_pool)
            except Exception as rf_exc:
                fallback_reason = f"{fallback_reason}; random_forest fallback failed: {rf_exc}"
                return json.dumps(
                    _exploration_fallback_response(
                        candidate_pool=candidate_pool,
                        encoder=encoder,
                        surrogate_model=surrogate_model,
                        acquisition_function=acquisition_function,
                        fallback_reason=fallback_reason,
                        hard_constraints=hard_constraints,
                        top_k=top_k,
                    ),
                    indent=2,
                )
        else:
            return json.dumps(
                _exploration_fallback_response(
                    candidate_pool=candidate_pool,
                    encoder=encoder,
                    surrogate_model=surrogate_model,
                    acquisition_function=acquisition_function,
                    fallback_reason=fallback_reason,
                    hard_constraints=hard_constraints,
                    top_k=top_k,
                ),
                indent=2,
            )

    pred_mean_model = pred_mean_scaled * (float(np.std(y_model)) or 1.0) + float(np.mean(y_model))
    pred_std = np.maximum(pred_std_scaled * (float(np.std(y_model)) or 1.0), 1e-6)
    pred_mean = pred_mean_model if direction != "minimize" else -1.0 * pred_mean_model
    best_f = float(np.max(y_scaled)) if len(y_scaled) else None
    rng = np.random.default_rng(seed)
    acquisition_values = acquisition.score(pred_mean_scaled, pred_std_scaled, best_f, rng)
    top_indices = _top_k_indices(acquisition_values, top_k)

    shortlist = []
    for idx in top_indices:
        candidate = candidate_pool[idx]
        violations = _check_constraints(candidate, hard_constraints)
        shortlist.append(
            {
                "candidate": candidate,
                "predicted_value": float(pred_mean[idx]),
                "uncertainty": float(pred_std[idx]),
                "acquisition_value": float(acquisition_values[idx]),
                "constraint_violations": violations,
                "constraint_satisfied": len(violations) == 0,
            }
        )

    return json.dumps(
        {
            "status": "success" if fallback_reason is None else "fallback",
            "strategy": "model_guided_shortlist" if fallback_reason is None else "fallback_model_guided_shortlist",
            "shortlist": shortlist,
            "recommended_index": 0,
            "candidates": [item["candidate"] for item in shortlist],
            "predictions": [item["predicted_value"] for item in shortlist],
            "uncertainties": [item["uncertainty"] for item in shortlist],
            "acquisition_values": [item["acquisition_value"] for item in shortlist],
            "surrogate_metrics": {
                "model": surrogate.metadata.get("resolved_key", surrogate_model),
                "num_training_points": int(len(y_obs)),
                "log_marginal_likelihood": getattr(surrogate, "log_marginal_likelihood_", None),
            },
            "resolved_components": {
                "embedding_method": encoder.metadata.get("resolved_key", embedding_method),
                "surrogate_model": surrogate.metadata.get("resolved_key", surrogate_model),
                "kernel_config": {"key": surrogate.metadata.get("resolved_kernel", resolved_kernel)},
                "acquisition_function": acquisition.metadata.get("resolved_key", acquisition_function),
            },
            "metadata": {
                "encoder_notes": encoder.metadata.get("notes", []),
                "surrogate_notes": surrogate.metadata.get("notes", []),
                "acquisition_notes": acquisition.metadata.get("notes", []),
                "fallback_reason": fallback_reason,
                "candidate_pool_size": len(candidate_pool),
                "runtime_mode": detect_runtime_capabilities()["runtime_mode"],
            },
        },
        indent=2,
    )


@tool
def hypothesis_generator(
    problem_spec: str,
    current_observations: str,
    memory_context: str,
) -> str:
    """Package structured inputs for hypothesis generation."""
    observations = _loads(current_observations, [])
    results = [float(item["result"]) for item in observations if item.get("result") is not None]
    summary = {
        "num_experiments": len(observations),
        "best_result": max(results) if results else None,
        "worst_result": min(results) if results else None,
        "mean_result": (sum(results) / len(results)) if results else None,
    }
    return json.dumps(
        {
            "problem_spec": _loads(problem_spec, {}),
            "observation_summary": summary,
            "memory_context": _loads(memory_context, {}),
            "instruction": (
                "Generate 3-5 concrete, testable hypotheses. Each hypothesis must include "
                "id, text, mechanism, testable_prediction, confidence, and active status."
            ),
        },
        indent=2,
    )


@tool
def result_interpreter(
    latest_observations: str,
    all_observations: str,
    bo_config: str,
    hypotheses: str,
) -> str:
    """Package structured inputs for result interpretation."""
    latest = _loads(latest_observations, [])
    observations = _loads(all_observations, [])
    results = [float(item["result"]) for item in observations if item.get("result") is not None]
    return json.dumps(
        {
            "statistical_summary": {
                "total_experiments": len(observations),
                "current_best": max(results) if results else None,
                "latest_result": latest[0].get("result") if latest else None,
                "mean_result": (sum(results) / len(results)) if results else None,
            },
            "latest_observations": latest,
            "all_observations": observations[-10:],
            "bo_config": _loads(bo_config, {}),
            "hypotheses": _loads(hypotheses, []),
            "instruction": (
                "Return strict JSON with keys: interpretation, supported_hypotheses, refuted_hypotheses, "
                "archived_hypotheses, episodic_memory, semantic_rule, working_memory."
            ),
        },
        indent=2,
    )


def generate_warm_start_candidates(
    search_space: list[dict[str, Any]],
    kb_priors: dict[str, Any],
    n_total: int = 5,
    n_prior: int = 3,
    seed: int = 0,
    hard_constraints: list[dict[str, Any]] | None = None,
    observed_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    rng = np.random.default_rng(seed)
    hard_constraints = hard_constraints or []
    observed_keys = observed_keys or set()
    n_total = max(1, int(n_total))
    n_prior = max(0, min(int(n_prior), n_total))

    candidates: list[dict[str, Any]] = []
    for _ in range(n_prior):
        candidate = {}
        for variable in search_space:
            name = variable["name"]
            if variable.get("type") == "continuous":
                prior_range = kb_priors.get("continuous_priors", {}).get(name, variable.get("domain", [0.0, 1.0]))
                low = float(prior_range[0])
                high = float(prior_range[1])
                value = rng.uniform(low, high)
                candidate[name] = round(value if not float(low).is_integer() else round(value), 6)
            else:
                labels = [str(item) for item in variable.get("domain", [])]
                weights = kb_priors.get("warm_start_bias", {}).get(name, {})
                if weights:
                    probs = np.asarray([float(weights.get(label, 0.1)) for label in labels], dtype=float)
                    probs = probs / probs.sum()
                    candidate[name] = str(rng.choice(labels, p=probs))
                else:
                    candidate[name] = str(rng.choice(labels))
        if _is_candidate_allowed(candidate, hard_constraints, observed_keys):
            candidates.append(candidate)

    explore_pool = hybrid_sample_candidates(search_space, max(n_total * 4, 24), seed=seed + 31)
    for candidate in explore_pool:
        if not _is_candidate_allowed(candidate, hard_constraints, observed_keys | {candidate_to_key(c) for c in candidates}):
            continue
        candidates.append(candidate)
        if len(candidates) >= n_total:
            break
    return candidates[:n_total]


def _loads(value: str | dict | list | None, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    text = str(value).strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def _dedupe_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for observation in observations:
        candidate = observation.get("candidate") or {}
        key = candidate_to_key(candidate)
        bucket = grouped.setdefault(key, {"candidate": candidate, "results": [], "metadata": []})
        if observation.get("result") is not None:
            bucket["results"].append(float(observation["result"]))
        if observation.get("metadata"):
            bucket["metadata"].append(observation["metadata"])
    deduped = []
    for bucket in grouped.values():
        if not bucket["results"]:
            continue
        deduped.append(
            {
                "candidate": bucket["candidate"],
                "result": float(np.mean(bucket["results"])),
                "replicates": len(bucket["results"]),
                "metadata": bucket["metadata"][-1] if bucket["metadata"] else {},
            }
        )
    return deduped


def _observations_to_training_data(
    observations: list[dict[str, Any]],
    encoder,
) -> tuple[np.ndarray, np.ndarray]:
    fit_candidates = [observation["candidate"] for observation in observations if observation.get("result") is not None]
    y_obs = np.asarray([float(observation["result"]) for observation in observations if observation.get("result") is not None], dtype=float)
    X_obs = encoder.encode_batch(fit_candidates)
    return X_obs, y_obs


def _build_candidate_pool(
    search_space: list[dict[str, Any]],
    observed_keys: set[str],
    candidate_pool_size: int,
    seed: int,
    hard_constraints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    discrete_candidates = enumerate_discrete_candidates(search_space)
    if discrete_candidates and len(discrete_candidates) <= candidate_pool_size:
        pool = discrete_candidates
    else:
        pool = hybrid_sample_candidates(search_space, max(candidate_pool_size, 64), seed=seed)

    deduped = []
    seen = set(observed_keys)
    for candidate in pool:
        if not _is_candidate_allowed(candidate, hard_constraints, seen):
            continue
        seen.add(candidate_to_key(candidate))
        deduped.append(candidate)
    return deduped


def _check_constraints(candidate: dict[str, Any], constraints: list[dict[str, Any]]) -> list[str]:
    violations = []
    for constraint in constraints:
        checker = constraint.get("check")
        if checker is not None and not checker(candidate):
            violations.append(f"{constraint['name']}: {constraint['reason']}")
    return violations


def _is_candidate_allowed(candidate: dict[str, Any], hard_constraints: list[dict[str, Any]], seen_keys: set[str]) -> bool:
    return candidate_to_key(candidate) not in seen_keys and not _check_constraints(candidate, hard_constraints)


def _build_shortlist_from_candidates(
    candidates: list[dict[str, Any]],
    hard_constraints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    shortlist = []
    for candidate in candidates:
        violations = _check_constraints(candidate, hard_constraints)
        shortlist.append(
            {
                "candidate": candidate,
                "predicted_value": None,
                "uncertainty": None,
                "acquisition_value": None,
                "constraint_violations": violations,
                "constraint_satisfied": len(violations) == 0,
            }
        )
    return shortlist


def _top_k_indices(values: np.ndarray, k: int) -> list[int]:
    if len(values) == 0:
        return []
    order = np.argsort(np.asarray(values, dtype=float))[::-1]
    return [int(idx) for idx in order[:k]]


def _exploration_fallback_response(
    candidate_pool: list[dict[str, Any]],
    encoder,
    surrogate_model: str,
    acquisition_function: str,
    fallback_reason: str,
    hard_constraints: list[dict[str, Any]],
    top_k: int,
) -> dict[str, Any]:
    shortlist = _build_shortlist_from_candidates(candidate_pool[:top_k], hard_constraints)
    return {
        "status": "fallback",
        "strategy": "random_exploration",
        "shortlist": shortlist,
        "recommended_index": 0,
        "candidates": [item["candidate"] for item in shortlist],
        "predictions": [item["predicted_value"] for item in shortlist],
        "uncertainties": [item["uncertainty"] for item in shortlist],
        "acquisition_values": [item["acquisition_value"] for item in shortlist],
        "surrogate_metrics": {"model": surrogate_model, "num_training_points": None, "log_marginal_likelihood": None},
        "resolved_components": {
            "embedding_method": encoder.metadata.get("resolved_key"),
            "surrogate_model": surrogate_model,
            "kernel_config": None,
            "acquisition_function": acquisition_function,
        },
        "metadata": {
            "fallback_reason": fallback_reason,
            "candidate_pool_size": len(candidate_pool),
            "runtime_mode": detect_runtime_capabilities()["runtime_mode"],
        },
    }


ALL_TOOLS = [
    embedding_method_advisor,
    surrogate_model_selector,
    af_selector,
    bo_runner,
    hypothesis_generator,
    result_interpreter,
]
