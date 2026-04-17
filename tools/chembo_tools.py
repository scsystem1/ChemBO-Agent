"""
ChemBO Agent shared tools and AutoBO helper utilities.
"""
from __future__ import annotations

import json
from typing import Any

import numpy as np
from langchain_core.tools import tool

from core.dataset_oracle import DatasetOracle
from pools.component_pools import (
    candidate_distance,
    candidate_to_key,
    enumerate_discrete_candidates,
    hybrid_sample_candidates,
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


def build_diverse_fallback_candidates(
    search_space: list[dict[str, Any]],
    n_total: int = 5,
    seed: int = 0,
    hard_constraints: list[dict[str, Any]] | None = None,
    observed_keys: set[str] | None = None,
    candidate_pool: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rng = np.random.default_rng(seed)
    hard_constraints = hard_constraints or []
    observed_keys = observed_keys or set()
    n_total = max(1, int(n_total))

    if candidate_pool is not None:
        valid_pool = filter_dataset_candidate_pool(candidate_pool, hard_constraints, observed_keys)
        if not valid_pool:
            return []
        return _select_diverse_candidates(valid_pool, search_space, n_total, rng)

    discrete_limit = max(n_total * 8, 64)
    discrete_candidates = enumerate_discrete_candidates(search_space, max_candidates=discrete_limit)
    if discrete_candidates:
        pool = discrete_candidates
    else:
        pool = hybrid_sample_candidates(search_space, discrete_limit, seed=seed + 31)

    feasible_pool = [
        candidate
        for candidate in pool
        if _is_candidate_allowed(candidate, hard_constraints, observed_keys)
    ]
    if not feasible_pool:
        return []
    return _select_diverse_candidates(feasible_pool, search_space, n_total, rng)


def filter_dataset_candidate_pool(
    candidate_pool: list[dict[str, Any]],
    hard_constraints: list[dict[str, Any]],
    observed_keys: set[str],
) -> list[dict[str, Any]]:
    valid_pool: list[dict[str, Any]] = []
    seen_keys = set(observed_keys)
    for candidate in candidate_pool:
        key = candidate_to_key(candidate)
        if key in seen_keys:
            continue
        if _check_constraints(candidate, hard_constraints):
            continue
        seen_keys.add(key)
        valid_pool.append(dict(candidate))
    return valid_pool


def _select_diverse_candidates(
    pool: list[dict[str, Any]],
    search_space: list[dict[str, Any]],
    limit: int,
    rng: np.random.Generator,
) -> list[dict[str, Any]]:
    if not pool:
        return []

    shuffled = [dict(pool[index]) for index in rng.permutation(len(pool))]
    selected: list[dict[str, Any]] = [shuffled[0]]
    remaining = shuffled[1:]

    while remaining and len(selected) < limit:
        best_index = 0
        best_score = float("-inf")
        for index, candidate in enumerate(remaining):
            score = min(candidate_distance(candidate, prior, search_space) for prior in selected)
            if score > best_score:
                best_score = score
                best_index = index
        selected.append(remaining.pop(best_index))
    return selected[:limit]


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


def dedupe_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
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


def observations_to_training_data(
    observations: list[dict[str, Any]],
    encoder,
) -> tuple[np.ndarray, np.ndarray]:
    fit_candidates = [observation["candidate"] for observation in observations if observation.get("result") is not None]
    y_obs = np.asarray([float(observation["result"]) for observation in observations if observation.get("result") is not None], dtype=float)
    X_obs = encoder.encode_batch(fit_candidates)
    return X_obs, y_obs


def build_candidate_pool(
    search_space: list[dict[str, Any]],
    observed_keys: set[str],
    candidate_pool_size: int,
    seed: int,
    hard_constraints: list[dict[str, Any]],
    candidate_pool: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if candidate_pool is not None:
        return filter_dataset_candidate_pool(candidate_pool, hard_constraints, observed_keys)

    discrete_candidates = enumerate_discrete_candidates(search_space, max_candidates=candidate_pool_size)
    if discrete_candidates:
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


def dataset_candidate_pool_from_spec(dataset_spec: dict[str, Any]) -> list[dict[str, Any]] | None:
    if not isinstance(dataset_spec, dict) or not dataset_spec:
        return None
    try:
        oracle = DatasetOracle(dataset_spec)
    except Exception:
        return None
    return [dict(candidate) for candidate in oracle.candidates]


def _check_constraints(candidate: dict[str, Any], constraints: list[dict[str, Any]]) -> list[str]:
    violations = []
    for constraint in constraints:
        checker = constraint.get("check")
        if checker is not None and not checker(candidate):
            violations.append(f"{constraint['name']}: {constraint['reason']}")
    return violations


def _is_candidate_allowed(candidate: dict[str, Any], hard_constraints: list[dict[str, Any]], seen_keys: set[str]) -> bool:
    return candidate_to_key(candidate) not in seen_keys and not _check_constraints(candidate, hard_constraints)


def build_shortlist_from_candidates(
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


_filter_dataset_candidate_pool = filter_dataset_candidate_pool
_dedupe_observations = dedupe_observations
_observations_to_training_data = observations_to_training_data
_build_candidate_pool = build_candidate_pool
_dataset_candidate_pool_from_spec = dataset_candidate_pool_from_spec
_build_shortlist_from_candidates = build_shortlist_from_candidates
