from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import sys
from pathlib import Path
from typing import Any

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.dataset_oracle import DatasetOracle
from core.problem_loader import load_problem_file, resolve_campaign_budget
from core.virtual_oracle import AutoGluonVirtualOracle
from pools.component_pools import (
    build_doe_pool,
    candidate_to_key,
    create_acquisition,
    create_encoder,
    create_surrogate,
    describe_optional_embedding_backends,
    discrete_search_space_size,
    enumerate_discrete_candidates,
    get_embedding_options,
    hybrid_sample_candidates,
    refresh_optional_embedding_backends,
)


def _slugify(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "unknown"
    chars: list[str] = []
    prev_dash = False
    for char in raw:
        if char.isalnum() or char in {".", "_", "-"}:
            chars.append(char)
            prev_dash = False
        elif not prev_dash:
            chars.append("-")
            prev_dash = True
    return "".join(chars).strip("-._") or "unknown"


def _parse_methods(raw: str) -> list[str]:
    methods = [item.strip() for item in str(raw or "").split(",") if item.strip()]
    if not methods:
        raise ValueError("At least one embedding method must be provided.")
    return methods


def _available_embedding_keys() -> list[str]:
    options = get_embedding_options()
    blocked = {"llm_embedding"}
    return [str(item.get("key")) for item in options if str(item.get("key")) not in blocked]


def _apply_embedding_backend_overrides(
    *,
    molclr_path: str | None = None,
    chemberta_path: str | None = None,
) -> dict[str, Any]:
    if molclr_path:
        os.environ["CHEMBO_MOLCLR_PATH"] = str(Path(molclr_path).expanduser().resolve())
    if chemberta_path:
        os.environ["CHEMBO_CHEMBERTA_PATH"] = str(Path(chemberta_path).expanduser().resolve())
    refresh_optional_embedding_backends()
    return describe_optional_embedding_backends()


def _mean(values: list[float]) -> float | None:
    return float(statistics.mean(values)) if values else None


def _std(values: list[float]) -> float | None:
    return float(statistics.stdev(values)) if len(values) >= 2 else (0.0 if values else None)


def _best_so_far_curve(values: list[float], direction: str) -> list[float]:
    curve: list[float] = []
    if direction == "minimize":
        best = float("inf")
        for value in values:
            best = min(best, value)
            curve.append(best)
    else:
        best = float("-inf")
        for value in values:
            best = max(best, value)
            curve.append(best)
    return curve


def _auc(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _resolve_oracle(problem_spec: dict[str, Any]):
    dataset_oracle = DatasetOracle.from_problem_spec(problem_spec)
    if dataset_oracle is not None:
        return "dataset", dataset_oracle
    virtual_oracle = AutoGluonVirtualOracle.from_problem_spec(problem_spec, settings=None)
    if virtual_oracle is not None:
        return "virtual", virtual_oracle
    raise ValueError("Problem spec must contain either a dataset oracle or a virtual_oracle.")


def _evaluate_candidate(oracle_kind: str, oracle, candidate: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    matched = oracle.lookup(candidate)
    result = float(matched["result"])
    metadata = dict(matched.get("metadata") or {})
    metadata["oracle_kind"] = oracle_kind
    return result, metadata


def _candidate_pool_from_problem(problem_spec: dict[str, Any], oracle_kind: str, oracle) -> list[dict[str, Any]] | None:
    if oracle_kind == "dataset":
        return [dict(candidate) for candidate in oracle.candidates]

    variables = list(problem_spec.get("variables") or [])
    total_size = discrete_search_space_size(variables, max_candidates=20000)
    if total_size is not None and total_size <= 20000:
        return enumerate_discrete_candidates(variables, max_candidates=20000)
    return None


def _initial_design(
    variables: list[dict[str, Any]],
    candidate_pool: list[dict[str, Any]] | None,
    initial_doe_size: int,
    seed: int,
) -> list[dict[str, Any]]:
    return build_doe_pool(
        variables,
        pool_size=max(1, int(initial_doe_size)),
        seed=seed,
        candidate_pool=candidate_pool,
    )


def _build_acquisition_pool(
    variables: list[dict[str, Any]],
    candidate_pool: list[dict[str, Any]] | None,
    observed_keys: set[str],
    candidate_pool_size: int,
    seed: int,
) -> list[dict[str, Any]]:
    if candidate_pool is not None:
        filtered = []
        for candidate in candidate_pool:
            key = candidate_to_key(candidate)
            if key in observed_keys:
                continue
            filtered.append(dict(candidate))
        return filtered

    raw = hybrid_sample_candidates(variables, max(candidate_pool_size, 64), seed=seed)
    deduped: list[dict[str, Any]] = []
    seen = set(observed_keys)
    for candidate in raw:
        key = candidate_to_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(dict(candidate))
    return deduped


def _training_arrays(observations: list[dict[str, Any]], encoder, direction: str) -> tuple[np.ndarray, np.ndarray]:
    candidates = [item["candidate"] for item in observations]
    values = np.asarray([float(item["result"]) for item in observations], dtype=float)
    y_model = values if direction != "minimize" else -1.0 * values
    x_obs = encoder.encode_batch(candidates)
    return x_obs, y_model


def _summarize_fit_arrays(
    *,
    x_obs: np.ndarray,
    y_train: np.ndarray,
    x_pool: np.ndarray,
    observations: list[dict[str, Any]],
    embedding_method: str,
    direction: str,
    encoder_metadata: dict[str, Any],
) -> dict[str, Any]:
    x_obs = np.asarray(x_obs, dtype=float)
    y_train = np.asarray(y_train, dtype=float).reshape(-1)
    x_pool = np.asarray(x_pool, dtype=float)

    row_keys = [candidate_to_key(item["candidate"]) for item in observations]
    unique_row_count = len(set(row_keys))
    feature_variances = np.var(x_obs, axis=0) if x_obs.size else np.asarray([], dtype=float)
    zero_variance_mask = np.isclose(feature_variances, 0.0, atol=1e-12)
    near_zero_variance_mask = feature_variances <= 1e-8

    x_centered = x_obs - np.mean(x_obs, axis=0, keepdims=True) if x_obs.size else x_obs
    finite_centered = np.all(np.isfinite(x_centered))
    singular_values: list[float] = []
    matrix_rank = None
    condition_number = None
    if x_centered.size and finite_centered:
        try:
            singular_values = [
                float(value)
                for value in np.linalg.svd(x_centered, full_matrices=False, compute_uv=False)
            ]
        except np.linalg.LinAlgError:
            singular_values = []
        if singular_values:
            matrix_rank = int(np.sum(np.asarray(singular_values) > 1e-10))
            nonzero = [value for value in singular_values if value > 1e-12]
            if nonzero:
                condition_number = float(max(nonzero) / min(nonzero))

    y_std = float(np.std(y_train)) if y_train.size else None
    y_unique = len({round(float(value), 12) for value in y_train.tolist()}) if y_train.size else 0
    observed_results = np.asarray([float(item["result"]) for item in observations], dtype=float)

    return {
        "embedding_method": embedding_method,
        "direction": direction,
        "num_observations": len(observations),
        "x_obs_shape": list(x_obs.shape),
        "x_pool_shape": list(x_pool.shape),
        "y_shape": list(y_train.shape),
        "x_obs_all_finite": bool(np.all(np.isfinite(x_obs))),
        "x_pool_all_finite": bool(np.all(np.isfinite(x_pool))),
        "y_all_finite": bool(np.all(np.isfinite(y_train))),
        "x_obs_min": float(np.min(x_obs)) if x_obs.size else None,
        "x_obs_max": float(np.max(x_obs)) if x_obs.size else None,
        "y_train_input_min": float(np.min(y_train)) if y_train.size else None,
        "y_train_input_max": float(np.max(y_train)) if y_train.size else None,
        "y_train_input_mean": float(np.mean(y_train)) if y_train.size else None,
        "y_train_input_std": y_std,
        "y_train_input_unique_count": y_unique,
        "y_observed_raw_min": float(np.min(observed_results)) if observed_results.size else None,
        "y_observed_raw_max": float(np.max(observed_results)) if observed_results.size else None,
        "y_observed_raw_mean": float(np.mean(observed_results)) if observed_results.size else None,
        "y_observed_raw_std": float(np.std(observed_results)) if observed_results.size else None,
        "duplicate_observation_count": max(0, len(observations) - unique_row_count),
        "zero_variance_feature_count": int(np.sum(zero_variance_mask)),
        "near_zero_variance_feature_count": int(np.sum(near_zero_variance_mask)),
        "matrix_rank_centered_x_obs": matrix_rank,
        "condition_number_centered_x_obs": condition_number,
        "top_singular_values": singular_values[:10],
        "encoder_metadata": encoder_metadata,
    }


def _select_next_candidate(
    variables: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    embedding_method: str,
    direction: str,
    candidate_pool: list[dict[str, Any]] | None,
    seed: int,
    candidate_pool_size: int,
    run_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    observed_keys = {candidate_to_key(item["candidate"]) for item in observations}
    pool = _build_acquisition_pool(
        variables=variables,
        candidate_pool=candidate_pool,
        observed_keys=observed_keys,
        candidate_pool_size=candidate_pool_size,
        seed=seed,
    )
    if not pool:
        raise RuntimeError("No remaining candidates available for BO selection.")

    encoder = create_encoder(embedding_method, variables, {})
    surrogate = create_surrogate("gp", {}, "matern52", {})
    acquisition = create_acquisition("log_ei", {})

    x_obs, y_train = _training_arrays(observations, encoder, direction)
    x_pool = encoder.encode_batch(pool)
    fit_summary = _summarize_fit_arrays(
        x_obs=x_obs,
        y_train=y_train,
        x_pool=x_pool,
        observations=observations,
        embedding_method=embedding_method,
        direction=direction,
        encoder_metadata=dict(getattr(encoder, "metadata", {}) or {}),
    )
    try:
        surrogate.fit(x_obs, y_train)
    except Exception as exc:
        raise RuntimeError(
            "GP fit failed for "
            f"method='{embedding_method}' run_id='{run_id}' iteration={len(observations) + 1}. "
            f"x_obs_shape={tuple(x_obs.shape)} y_std={fit_summary.get('y_train_input_std')} "
            f"zero_var_features={fit_summary.get('zero_variance_feature_count')} "
            f"matrix_rank={fit_summary.get('matrix_rank_centered_x_obs')}."
        ) from exc
    pred_mean, pred_std = surrogate.predict(x_pool)
    acquisition_values = acquisition.score(
        surrogate,
        x_pool,
        float(np.max(y_train)) if len(y_train) else None,
        np.random.default_rng(seed),
    )
    best_index = int(np.argmax(np.asarray(acquisition_values, dtype=float)))
    stats = {
        "predicted_value": float(pred_mean[best_index]),
        "uncertainty": float(pred_std[best_index]),
        "acquisition_value": float(acquisition_values[best_index]),
        "surrogate_model": "gp",
        "kernel": "matern52",
        "acquisition_function": "log_ei",
    }
    return dict(pool[best_index]), stats


def run_fixed_bo_embedding_compare(
    *,
    problem_path: Path,
    methods: list[str],
    repeats: int,
    output_root: Path,
    initial_doe_size: int | None = None,
    max_iterations: int | None = None,
    candidate_pool_size: int = 512,
    seed: int = 0,
    embedding_backend_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    problem_spec = load_problem_file(problem_path)
    if not isinstance(problem_spec, dict):
        raise ValueError("Expected a structured YAML/JSON problem file.")

    direction = str(problem_spec.get("optimization_direction") or "maximize").strip().lower()
    budget = int(max_iterations or resolve_campaign_budget(problem_spec, _BudgetAdapter()))
    variables = list(problem_spec.get("variables") or [])
    oracle_kind, oracle = _resolve_oracle(problem_spec)
    oracle_pool = _candidate_pool_from_problem(problem_spec, oracle_kind, oracle)
    doe_size = int(initial_doe_size or min(5, budget))

    root = output_root / f"{_slugify(problem_path.stem)}_fixed_bo_embedding_compare"
    root.mkdir(parents=True, exist_ok=True)

    run_rows: list[dict[str, Any]] = []
    trajectory_rows: list[dict[str, Any]] = []
    observation_rows: list[dict[str, Any]] = []

    for method in methods:
        for repeat_index in range(1, repeats + 1):
            run_seed = seed + (1000 * repeat_index) + (10000 * methods.index(method))
            run_id = f"{_slugify(method)}_run{repeat_index:02d}"
            observations: list[dict[str, Any]] = []

            initial_candidates = _initial_design(
                variables=variables,
                candidate_pool=oracle_pool,
                initial_doe_size=min(doe_size, budget),
                seed=run_seed,
            )
            for candidate in initial_candidates:
                result, metadata = _evaluate_candidate(oracle_kind, oracle, candidate)
                observations.append({"candidate": dict(candidate), "result": result, "metadata": metadata})

            while len(observations) < budget:
                candidate, stats = _select_next_candidate(
                    variables=variables,
                    observations=observations,
                    embedding_method=method,
                    direction=direction,
                    candidate_pool=oracle_pool,
                    seed=run_seed + len(observations),
                    candidate_pool_size=candidate_pool_size,
                    run_id=run_id,
                )
                result, metadata = _evaluate_candidate(oracle_kind, oracle, candidate)
                metadata.update(stats)
                observations.append({"candidate": dict(candidate), "result": result, "metadata": metadata})

            results = [float(item["result"]) for item in observations]
            curve = _best_so_far_curve(results, direction)
            best_result = curve[-1] if curve else None
            best_index = (
                int(np.argmin(results)) if direction == "minimize" else int(np.argmax(results))
            ) if results else None
            best_candidate = observations[best_index]["candidate"] if best_index is not None else {}

            run_dir = root / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "embedding_method": method,
                        "repeat_index": repeat_index,
                        "run_id": run_id,
                        "best_result": best_result,
                        "best_candidate": best_candidate,
                        "oracle_kind": oracle_kind,
                        "budget": budget,
                        "initial_doe_size": min(doe_size, budget),
                        "surrogate_model": "gp",
                        "kernel": "matern52",
                        "acquisition_function": "log_ei",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            _write_csv(
                run_dir / "experiment_records.csv",
                ["iteration", "result", "candidate", "metadata"],
                [
                    {
                        "iteration": idx,
                        "result": item["result"],
                        "candidate": json.dumps(item["candidate"], ensure_ascii=False),
                        "metadata": json.dumps(item["metadata"], ensure_ascii=False),
                    }
                    for idx, item in enumerate(observations, start=1)
                ],
            )

            run_rows.append(
                {
                    "embedding_method": method,
                    "repeat_index": repeat_index,
                    "run_id": run_id,
                    "best_result": best_result,
                    "best_so_far_auc": _auc(curve),
                    "num_experiments": len(observations),
                    "output_dir": str(run_dir),
                    "best_candidate": json.dumps(best_candidate, ensure_ascii=False),
                }
            )

            for iteration, item in enumerate(observations, start=1):
                trajectory_rows.append(
                    {
                        "embedding_method": method,
                        "repeat_index": repeat_index,
                        "run_id": run_id,
                        "iteration": iteration,
                        "best_so_far": curve[iteration - 1],
                    }
                )
                observation_rows.append(
                    {
                        "embedding_method": method,
                        "repeat_index": repeat_index,
                        "run_id": run_id,
                        "iteration": iteration,
                        "result": item["result"],
                        "candidate": json.dumps(item["candidate"], ensure_ascii=False),
                        "metadata": json.dumps(item["metadata"], ensure_ascii=False),
                    }
                )

    summary_rows: list[dict[str, Any]] = []
    for method in methods:
        rows = [row for row in run_rows if row["embedding_method"] == method]
        best_values = [float(row["best_result"]) for row in rows if row["best_result"] is not None]
        auc_values = [float(row["best_so_far_auc"]) for row in rows if row["best_so_far_auc"] is not None]
        summary_rows.append(
            {
                "embedding_method": method,
                "repeats": len(rows),
                "best_result_mean": _mean(best_values),
                "best_result_std": _std(best_values),
                "best_result_max": max(best_values) if best_values else None,
                "best_result_min": min(best_values) if best_values else None,
                "best_so_far_auc_mean": _mean(auc_values),
                "best_so_far_auc_std": _std(auc_values),
            }
        )

    _write_csv(
        root / "embedding_run_summary.csv",
        [
            "embedding_method",
            "repeat_index",
            "run_id",
            "best_result",
            "best_so_far_auc",
            "num_experiments",
            "output_dir",
            "best_candidate",
        ],
        run_rows,
    )
    _write_csv(
        root / "embedding_trajectory.csv",
        ["embedding_method", "repeat_index", "run_id", "iteration", "best_so_far"],
        trajectory_rows,
    )
    _write_csv(
        root / "embedding_observations.csv",
        ["embedding_method", "repeat_index", "run_id", "iteration", "result", "candidate", "metadata"],
        observation_rows,
    )
    _write_csv(
        root / "embedding_aggregate_summary.csv",
        [
            "embedding_method",
            "repeats",
            "best_result_mean",
            "best_result_std",
            "best_result_max",
            "best_result_min",
            "best_so_far_auc_mean",
            "best_so_far_auc_std",
        ],
        summary_rows,
    )

    payload = {
        "problem_file": str(problem_path),
        "methods": methods,
        "repeats": repeats,
        "budget": budget,
        "initial_doe_size": min(doe_size, budget),
        "candidate_pool_size": candidate_pool_size,
        "oracle_kind": oracle_kind,
        "surrogate_model": "gp",
        "kernel": "matern52",
        "acquisition_function": "log_ei",
        "output_dir": str(root),
        "embedding_backend_config": embedding_backend_config or describe_optional_embedding_backends(),
        "summary": summary_rows,
        "runs": run_rows,
    }
    (root / "embedding_compare_summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


class _BudgetAdapter:
    max_bo_iterations = 30


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare embedding methods with a fixed BO stack: GP + Matern5/2 + log-EI."
    )
    parser.add_argument("--problem-file", required=True, help="Path to the structured problem YAML/JSON.")
    parser.add_argument(
        "--methods",
        default="one_hot,fingerprint_concat,physicochemical_descriptors,molclr_concat,chemberta_concat",
        help="Comma-separated embedding keys to compare.",
    )
    parser.add_argument("--repeats", type=int, default=3, help="Number of repeated BO runs per embedding.")
    parser.add_argument("--max-iterations", type=int, default=None, help="Override optimization budget.")
    parser.add_argument("--initial-doe-size", type=int, default=20, help="Initial DOE size.")
    parser.add_argument("--candidate-pool-size", type=int, default=512, help="Acquisition candidate pool size.")
    parser.add_argument("--seed", type=int, default=0, help="Base random seed.")
    parser.add_argument("--molclr-path", default=None, help="Optional local MolCLR repo path override.")
    parser.add_argument("--chemberta-path", default=None, help="Optional local ChemBERTa snapshot path override.")
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).resolve().parent),
        help="Directory where comparison outputs will be written.",
    )
    parser.add_argument(
        "--list-methods",
        action="store_true",
        help="Print available non-LLM embedding keys and exit.",
    )
    parser.add_argument(
        "--show-embedding-config",
        action="store_true",
        help="Print resolved MolCLR/ChemBERTa backend configuration and exit.",
    )
    args = parser.parse_args()

    backend_config = _apply_embedding_backend_overrides(
        molclr_path=args.molclr_path,
        chemberta_path=args.chemberta_path,
    )

    if args.list_methods:
        print(json.dumps(_available_embedding_keys(), ensure_ascii=False, indent=2))
        return
    if args.show_embedding_config:
        print(json.dumps(backend_config, ensure_ascii=False, indent=2))
        return

    payload = run_fixed_bo_embedding_compare(
        problem_path=Path(args.problem_file).resolve(),
        methods=_parse_methods(args.methods),
        repeats=max(1, int(args.repeats)),
        output_root=Path(args.output_dir).resolve(),
        initial_doe_size=max(1, int(args.initial_doe_size)),
        max_iterations=args.max_iterations,
        candidate_pool_size=max(32, int(args.candidate_pool_size)),
        seed=int(args.seed),
        embedding_backend_config=backend_config,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
