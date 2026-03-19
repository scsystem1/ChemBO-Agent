"""
ChemBO Agent Tools
===================
LangChain-compatible tools used by the single ChemBO reasoning core.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import numpy as np
from langchain_core.tools import tool

from pools.component_pools import (
    AF_POOL,
    EMBEDDING_POOL,
    SURROGATE_POOL,
    candidate_to_key,
    create_acquisition,
    create_encoder,
    create_surrogate,
    enumerate_discrete_candidates,
    get_af_options,
    get_embedding_options,
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
    """Select the best embedding method for encoding reaction conditions."""
    options = get_embedding_options()

    scored_options = []
    for opt in options:
        score_reasons = []
        tags = opt["tags"]

        if has_smiles and tags.get("chemistry_aware"):
            score_reasons.append("+chemistry_aware (has SMILES)")
        if num_categoricals > 3 and tags.get("handles_categorical"):
            score_reasons.append("+handles many categoricals")
        if not tags.get("requires_pretraining", False):
            score_reasons.append("+no pretraining needed")
        if data_volume < 10 and tags.get("learned"):
            score_reasons.append("-learned repr may underfit with little data")

        scored_options.append({**opt, "suitability_notes": score_reasons})

    return json.dumps(
        {
            "available_options": scored_options,
            "problem_context": {
                "problem_summary": problem_summary,
                "variable_types": variable_types,
                "num_categoricals": num_categoricals,
                "num_continuous": num_continuous,
                "has_smiles": has_smiles,
                "data_volume": data_volume,
            },
            "instruction": (
                "Review the options above. Select ONE embedding method by its key. "
                "Provide your rationale considering the problem's variable types, "
                "dimensionality, and available data."
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
    """Select the best surrogate model for the BO campaign."""
    options = get_surrogate_options()

    scored_options = []
    for opt in options:
        tags = opt["tags"]
        notes = []

        max_dims = tags.get("max_effective_dims", 20)
        if embedding_dim > max_dims:
            notes.append(f"-embedding_dim ({embedding_dim}) exceeds max ({max_dims})")
        else:
            notes.append(f"+embedding_dim ({embedding_dim}) within capacity ({max_dims})")

        if num_categoricals > 0 and tags.get("handles_categorical"):
            notes.append("+natively handles categoricals")

        min_data = tags.get("min_data_for_training", 0)
        if min_data > 0 and expected_data_volume < min_data * 2:
            notes.append(f"-needs >={min_data} points; budget may be tight")

        if noise_level == "high" and tags.get("model_type") == "random_forest":
            notes.append("+robust to noise")

        if embedding_method == "llm_embedding" and opt["key"] == "dkl":
            notes.append("+DKL can align LLM embeddings with target")

        scored_options.append({**opt, "suitability_notes": notes})

    return json.dumps(
        {
            "available_options": scored_options,
            "context": {
                "problem_summary": problem_summary,
                "embedding_method": embedding_method,
                "embedding_dim": embedding_dim,
                "num_variables": num_variables,
                "num_categoricals": num_categoricals,
                "expected_data_volume": expected_data_volume,
                "noise_level": noise_level,
            },
            "instruction": (
                "Select ONE surrogate model by its key. Consider compatibility with "
                "the chosen embedding method, expected data volume, and problem complexity."
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
    current_best: Optional[float] = None,
) -> str:
    """Select the acquisition function for the BO campaign."""
    options = get_af_options()
    exploration_phase = budget_remaining > budget_total * 0.6

    scored_options = []
    for opt in options:
        tags = opt["tags"]
        notes = []

        if batch_size > 1 and not tags.get("batch_support"):
            notes.append("-does NOT support batch mode")
        elif batch_size > 1 and tags.get("batch_support"):
            notes.append("+supports batch mode")

        if num_objectives > 1 and not tags.get("multi_objective"):
            notes.append("-single-objective only")
        elif num_objectives > 1 and tags.get("multi_objective"):
            notes.append("+supports multi-objective")

        if exploration_phase and "exploration" in str(tags.get("exploration_exploitation", "")):
            notes.append("+good for exploration phase")
        if not exploration_phase and tags.get("type") == "improvement":
            notes.append("+good for exploitation phase")

        scored_options.append({**opt, "suitability_notes": notes})

    return json.dumps(
        {
            "available_options": scored_options,
            "context": {
                "problem_summary": problem_summary,
                "surrogate_model": surrogate_model,
                "batch_size": batch_size,
                "budget_remaining": budget_remaining,
                "campaign_phase": "exploration" if exploration_phase else "exploitation",
                "num_objectives": num_objectives,
                "current_best": current_best,
            },
            "instruction": (
                "Select ONE acquisition function by its key. Consider the campaign "
                "phase, batch requirements, and optimization objectives."
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
) -> str:
    """Execute one BO iteration and return structured candidate proposals."""
    search_space_data = _loads(search_space, [])
    obs_data = _loads(observations, [])
    embedding_params_data = _loads(embedding_params, {})
    surrogate_params_data = _loads(surrogate_params, {})
    af_params_data = _loads(af_params, {})

    seed = int(
        af_params_data.get(
            "random_state",
            surrogate_params_data.get("random_state", embedding_params_data.get("random_state", 0)),
        )
    )
    batch_size = max(1, int(batch_size or 1))
    initial_doe_size = int(
        af_params_data.get("initial_doe_size", surrogate_params_data.get("initial_doe_size", 5))
    )
    candidate_pool_size = int(af_params_data.get("candidate_pool_size", 512))

    encoder = create_encoder(embedding_method, search_space_data, embedding_params_data)
    observed_candidates = [record.get("candidate", {}) for record in obs_data if record.get("candidate")]
    observed_keys = {candidate_to_key(candidate) for candidate in observed_candidates}
    deduped_observations = _dedupe_observations(obs_data)

    cold_start = len(deduped_observations) < initial_doe_size
    if cold_start:
        candidates = _initial_design_candidates(
            search_space_data,
            observed_keys,
            batch_size=batch_size,
            initial_doe_size=initial_doe_size,
            seed=seed,
        )
        return json.dumps(
            {
                "status": "success",
                "strategy": "initial_doe",
                "candidates": candidates,
                "predictions": [None for _ in candidates],
                "uncertainties": [None for _ in candidates],
                "acquisition_values": [None for _ in candidates],
                "surrogate_metrics": {
                    "model": surrogate_model,
                    "num_training_points": len(deduped_observations),
                    "log_marginal_likelihood": None,
                },
                "resolved_components": {
                    "embedding_method": encoder.metadata.get("resolved_key", embedding_method),
                    "surrogate_model": surrogate_model,
                    "acquisition_function": acquisition_function,
                },
                "metadata": {
                    "encoder_notes": encoder.metadata.get("notes", []),
                    "fallback_reason": None,
                    "candidate_pool_size": len(candidates),
                },
                "message": (
                    f"Cold start: proposing {len(candidates)} design-of-experiments candidate(s) "
                    f"before fitting a surrogate."
                ),
            },
            indent=2,
        )

    X_obs, y_obs, fit_candidates = _observations_to_training_data(deduped_observations, encoder)
    y_mean = float(np.mean(y_obs))
    y_std = float(np.std(y_obs)) or 1.0
    y_scaled = (y_obs - y_mean) / y_std

    surrogate = create_surrogate(surrogate_model, surrogate_params_data)
    acquisition = create_acquisition(acquisition_function, af_params_data)
    fallback_reason = None

    candidate_pool = _build_candidate_pool(
        search_space_data,
        observed_keys=observed_keys,
        candidate_pool_size=candidate_pool_size,
        seed=seed,
    )
    if not candidate_pool:
        candidate_pool = _initial_design_candidates(
            search_space_data,
            observed_keys=set(),
            batch_size=max(candidate_pool_size, batch_size),
            initial_doe_size=initial_doe_size,
            seed=seed,
        )

    X_pool = encoder.encode_batch(candidate_pool)
    best_f = float(np.max(y_obs)) if len(y_obs) else None

    try:
        surrogate.fit(X_obs, y_scaled)
        pred_mean_scaled, pred_std_scaled = surrogate.predict(X_pool)
        pred_mean = pred_mean_scaled * y_std + y_mean
        pred_std = np.maximum(pred_std_scaled * abs(y_std), 1e-6)
    except Exception as exc:  # pragma: no cover - exercised in runtime fallback
        fallback_reason = f"{type(exc).__name__}: {exc}"
        if surrogate_model != "random_forest":
            surrogate = create_surrogate("random_forest", surrogate_params_data)
            try:
                surrogate.fit(X_obs, y_scaled)
                pred_mean_scaled, pred_std_scaled = surrogate.predict(X_pool)
                pred_mean = pred_mean_scaled * y_std + y_mean
                pred_std = np.maximum(pred_std_scaled * abs(y_std), 1e-6)
            except Exception as rf_exc:
                fallback_reason = f"{fallback_reason}; random_forest fallback failed: {rf_exc}"
                return json.dumps(
                    _exploration_fallback_response(
                        candidate_pool=candidate_pool,
                        surrogate_model=surrogate_model,
                        acquisition_function=acquisition_function,
                        fallback_reason=fallback_reason,
                        batch_size=batch_size,
                    ),
                    indent=2,
                )
        else:
            return json.dumps(
                _exploration_fallback_response(
                    candidate_pool=candidate_pool,
                    surrogate_model=surrogate_model,
                    acquisition_function=acquisition_function,
                    fallback_reason=fallback_reason,
                    batch_size=batch_size,
                ),
                indent=2,
            )

    rng = np.random.default_rng(seed)
    acquisition_values = acquisition.score(pred_mean, pred_std, best_f, rng)
    top_indices = _top_k_indices(acquisition_values, batch_size)

    chosen_candidates = [candidate_pool[idx] for idx in top_indices]
    chosen_predictions = [float(pred_mean[idx]) for idx in top_indices]
    chosen_uncertainties = [float(pred_std[idx]) for idx in top_indices]
    chosen_acq = [float(acquisition_values[idx]) for idx in top_indices]

    return json.dumps(
        {
            "status": "success" if fallback_reason is None else "fallback",
            "strategy": "model_guided_search" if fallback_reason is None else "fallback_model_guided_search",
            "candidates": chosen_candidates,
            "predictions": chosen_predictions,
            "uncertainties": chosen_uncertainties,
            "acquisition_values": chosen_acq,
            "surrogate_metrics": {
                "model": surrogate.metadata.get("resolved_key", surrogate_model),
                "num_training_points": int(len(y_obs)),
                "log_marginal_likelihood": getattr(surrogate, "log_marginal_likelihood_", None),
            },
            "resolved_components": {
                "embedding_method": encoder.metadata.get("resolved_key", embedding_method),
                "surrogate_model": surrogate.metadata.get("resolved_key", surrogate_model),
                "acquisition_function": acquisition.metadata.get("resolved_key", acquisition_function),
            },
            "metadata": {
                "encoder_notes": encoder.metadata.get("notes", []),
                "surrogate_notes": surrogate.metadata.get("notes", []),
                "acquisition_notes": acquisition.metadata.get("notes", []),
                "fallback_reason": fallback_reason,
                "candidate_pool_size": len(candidate_pool),
            },
            "message": (
                f"Scored {len(candidate_pool)} candidate(s) with "
                f"{surrogate.metadata.get('resolved_key', surrogate_model)} + "
                f"{acquisition.metadata.get('resolved_key', acquisition_function)}."
            ),
        },
        indent=2,
    )


@tool
def hypothesis_generator(
    problem_spec: str,
    current_observations: str,
    memory_context: str,
) -> str:
    """Generate structured context for chemically grounded hypothesis generation."""
    obs = _loads(current_observations, [])

    if obs:
        results = [o.get("result", 0) for o in obs if o.get("result") is not None]
        summary = {
            "num_experiments": len(obs),
            "best_result": max(results) if results else None,
            "worst_result": min(results) if results else None,
            "mean_result": sum(results) / len(results) if results else None,
            "trend": "improving" if len(results) > 1 and results[-1] >= results[-2] else "flat_or_declining",
        }
    else:
        summary = {"num_experiments": 0, "note": "No data yet — generate prior hypotheses"}

    return json.dumps(
        {
            "problem_spec": _loads(problem_spec, {}),
            "data_summary": summary,
            "memory_context": _loads(memory_context, {}),
            "instruction": (
                "Based on the problem specification, observed data patterns, and chemistry knowledge, "
                "generate 3-5 specific hypotheses. Return strict JSON with key 'hypotheses', where "
                "each item has fields: hypothesis, mechanism, test, confidence."
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
    """Generate structured context for interpreting results and updating memory."""
    all_obs = _loads(all_observations, [])
    latest = _loads(latest_observations, [])
    config = _loads(bo_config, {})
    active_hypotheses = _loads(hypotheses, [])

    results = [o.get("result", 0) for o in all_obs if o.get("result") is not None]
    running_improvement = []
    best_so_far = None
    for obs in all_obs:
        result = obs.get("result")
        if result is None:
            continue
        best_so_far = result if best_so_far is None else max(best_so_far, result)
        running_improvement.append(best_so_far)

    return json.dumps(
        {
            "statistical_summary": {
                "total_experiments": len(all_obs),
                "current_best": max(results) if results else None,
                "latest_result": latest[0].get("result") if latest else None,
                "running_improvement": running_improvement,
            },
            "latest_results": latest,
            "bo_config": config,
            "hypotheses": active_hypotheses,
            "instruction": (
                "Return strict JSON with keys: interpretation, supported_hypotheses, refuted_hypotheses, "
                "episodic_memory, semantic_rule, working_memory. episodic_memory must include: reflection, "
                "lesson_learned, non_numerical_observations. semantic_rule may be null or an object with "
                "rule and confidence."
            ),
        },
        indent=2,
    )


def _loads(value: str | dict | list | None, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return default
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return default


def _dedupe_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for obs in observations:
        candidate = obs.get("candidate") or {}
        key = candidate_to_key(candidate)
        bucket = grouped.setdefault(
            key,
            {
                "candidate": candidate,
                "results": [],
                "metadata": [],
            },
        )
        if obs.get("result") is not None:
            bucket["results"].append(float(obs["result"]))
        if obs.get("metadata"):
            bucket["metadata"].append(obs["metadata"])

    deduped = []
    for bucket in grouped.values():
        results = bucket["results"]
        deduped.append(
            {
                "candidate": bucket["candidate"],
                "result": float(np.mean(results)) if results else None,
                "replicates": len(results),
                "metadata": bucket["metadata"][-1] if bucket["metadata"] else {},
            }
        )
    return deduped


def _observations_to_training_data(
    observations: list[dict[str, Any]],
    encoder,
) -> tuple[np.ndarray, np.ndarray, list[dict[str, Any]]]:
    fit_candidates = [obs["candidate"] for obs in observations if obs.get("result") is not None]
    y_obs = np.asarray([float(obs["result"]) for obs in observations if obs.get("result") is not None], dtype=float)
    X_obs = encoder.encode_batch(fit_candidates)
    return X_obs, y_obs, fit_candidates


def _build_candidate_pool(
    search_space: list[dict[str, Any]],
    observed_keys: set[str],
    candidate_pool_size: int,
    seed: int,
) -> list[dict[str, Any]]:
    discrete_candidates = enumerate_discrete_candidates(search_space)
    if discrete_candidates and len(discrete_candidates) <= candidate_pool_size:
        pool = discrete_candidates
    else:
        pool = hybrid_sample_candidates(search_space, num_samples=max(candidate_pool_size, 64), seed=seed)

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set(observed_keys)
    for candidate in pool:
        key = candidate_to_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _initial_design_candidates(
    search_space: list[dict[str, Any]],
    observed_keys: set[str],
    batch_size: int,
    initial_doe_size: int,
    seed: int,
) -> list[dict[str, Any]]:
    discrete_candidates = enumerate_discrete_candidates(search_space)
    if discrete_candidates:
        rng = np.random.default_rng(seed)
        available = [candidate for candidate in discrete_candidates if candidate_to_key(candidate) not in observed_keys]
        rng.shuffle(available)
        return available[:batch_size]

    pool = hybrid_sample_candidates(
        search_space,
        num_samples=max(initial_doe_size * 8, batch_size * 8, 32),
        seed=seed,
    )
    result = []
    seen = set(observed_keys)
    for candidate in pool:
        key = candidate_to_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
        if len(result) >= batch_size:
            break
    return result


def _top_k_indices(values: np.ndarray, k: int) -> list[int]:
    if len(values) == 0:
        return []
    order = np.argsort(np.asarray(values, dtype=float))[::-1]
    return [int(idx) for idx in order[:k]]


def _exploration_fallback_response(
    candidate_pool: list[dict[str, Any]],
    surrogate_model: str,
    acquisition_function: str,
    fallback_reason: str,
    batch_size: int,
) -> dict[str, Any]:
    selected = candidate_pool[:batch_size]
    return {
        "status": "fallback",
        "strategy": "random_exploration",
        "candidates": selected,
        "predictions": [None for _ in selected],
        "uncertainties": [None for _ in selected],
        "acquisition_values": [None for _ in selected],
        "surrogate_metrics": {
            "model": surrogate_model,
            "num_training_points": None,
            "log_marginal_likelihood": None,
        },
        "resolved_components": {
            "embedding_method": None,
            "surrogate_model": surrogate_model,
            "acquisition_function": acquisition_function,
        },
        "metadata": {
            "fallback_reason": fallback_reason,
            "candidate_pool_size": len(candidate_pool),
        },
        "message": "Model fitting failed; returning exploratory candidates from the search space.",
    }


ALL_TOOLS = [
    embedding_method_advisor,
    surrogate_model_selector,
    af_selector,
    bo_runner,
    hypothesis_generator,
    result_interpreter,
]
