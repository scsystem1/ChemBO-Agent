from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from core.problem_loader import load_problem_file

DEFAULT_DATASETS = [
    "dar",
    "ocm",
    "suzuki",
    "oer",
    "hpobench_svm",
    "hpobench_rf",
    "hpobench_xgb",
    "hpobench_nn",
]

DATASET_PROBLEM_FILES = {
    "dar": ROOT / "examples" / "dar_problem.yaml",
    "ocm": ROOT / "examples" / "ocm_problem.yaml",
    "suzuki": ROOT / "examples" / "suzuki_problem.yaml",
    "oer": ROOT / "examples" / "oer_problem.yaml",
    "hpobench_svm": ROOT / "examples" / "hpobench_svm_146212_problem.yaml",
    "hpobench_rf": ROOT / "examples" / "hpobench_rf_146606_problem.yaml",
    "hpobench_xgb": ROOT / "examples" / "hpobench_xgb_146606_problem.yaml",
    "hpobench_nn": ROOT / "examples" / "hpobench_nn_168912_problem.yaml",
}

LOG_SCALES = {"log", "log10", "log-uniform"}
LOG2_SCALES = {"log2"}
CATEGORICAL_MISMATCH_PENALTY = 1.0e6
LD_REEXEC_ENV = "GP_HEDGE_LD_LIBRARY_PATH_REEXECED"


@dataclass(frozen=True)
class ProblemBundle:
    dataset_name: str
    problem_path: Path
    problem_spec: dict[str, Any]
    df: pd.DataFrame
    feature_columns: list[str]
    target_column: str
    variables: list[dict[str, Any]]
    variable_by_name: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class ProjectionResult:
    row_index: int
    candidate: dict[str, Any]
    objective: float
    distance: float


def log(message: str) -> None:
    print(message, flush=True)


def cap_cpu_threads(max_threads: int = 40) -> int:
    thread_cap = min(max_threads, os.cpu_count() or max_threads)
    for env_name in [
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "BLIS_NUM_THREADS",
    ]:
        current_value = os.getenv(env_name, "").strip()
        if current_value.isdigit() and int(current_value) > 0:
            thread_cap = min(thread_cap, int(current_value))
    for env_name in [
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "BLIS_NUM_THREADS",
    ]:
        os.environ[env_name] = str(thread_cap)
    return thread_cap


def seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)


def ensure_conda_runtime_libraries_preferred() -> None:
    """Re-exec once so SciPy/skopt prefer conda libstdc++ over system/CUDA paths."""
    conda_prefix = os.getenv("CONDA_PREFIX", "").strip()
    if not conda_prefix or os.getenv(LD_REEXEC_ENV) == "1":
        return

    conda_lib = str(Path(conda_prefix) / "lib")
    current_paths = [path for path in os.getenv("LD_LIBRARY_PATH", "").split(":") if path]
    if current_paths and current_paths[0] == conda_lib:
        return

    new_env = dict(os.environ)
    new_env[LD_REEXEC_ENV] = "1"
    new_env["LD_LIBRARY_PATH"] = ":".join([conda_lib, *[path for path in current_paths if path != conda_lib]])
    os.execvpe(sys.executable, [sys.executable, *sys.argv], new_env)


def skopt_version() -> str | None:
    try:
        import skopt
    except ModuleNotFoundError:
        return None
    return str(getattr(skopt, "__version__", "unknown"))


def _import_skopt_space():
    try:
        from skopt.space import Categorical, Real
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency `scikit-optimize`. Install `scikit-optimize==0.10.2` "
            "in the chembo environment before running the GP-Hedge baseline."
        ) from exc
    return Categorical, Real


def _import_skopt_optimizer():
    try:
        from skopt import Optimizer
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency `scikit-optimize`. Install `scikit-optimize==0.10.2` "
            "in the chembo environment before running the GP-Hedge baseline."
        ) from exc
    return Optimizer


def load_problem_bundle(dataset_name: str) -> ProblemBundle:
    key = dataset_name.strip().lower()
    if key not in DATASET_PROBLEM_FILES:
        supported = ", ".join(DEFAULT_DATASETS)
        raise ValueError(f"Unsupported dataset: {dataset_name}. Use one of: {supported}.")

    problem_path = DATASET_PROBLEM_FILES[key]
    problem_spec = load_problem_file(problem_path)
    if not isinstance(problem_spec, dict):
        raise RuntimeError(f"Expected structured problem file: {problem_path}")

    dataset_spec = problem_spec.get("dataset")
    if not isinstance(dataset_spec, dict):
        raise RuntimeError(f"Problem file missing dataset spec: {problem_path}")

    feature_columns = [str(column) for column in dataset_spec.get("feature_columns", [])]
    target_column = str(dataset_spec.get("target_column") or problem_spec.get("target_metric") or "")
    if not feature_columns or not target_column:
        raise RuntimeError(f"Problem file missing feature_columns/target_column: {problem_path}")

    csv_path = Path(str(dataset_spec.get("csv_path") or "")).expanduser()
    if not csv_path.is_absolute():
        csv_path = (problem_path.parent / csv_path).resolve()
    df = pd.read_csv(csv_path)
    df = _drop_index_like_columns(df)

    missing_columns = [column for column in [*feature_columns, target_column] if column not in df.columns]
    if missing_columns:
        raise RuntimeError(f"{csv_path} is missing required column(s): {missing_columns}")

    variables = [dict(variable) for variable in problem_spec.get("variables", []) if isinstance(variable, dict)]
    variable_by_name = {str(variable.get("name")): variable for variable in variables}
    missing_variables = [column for column in feature_columns if column not in variable_by_name]
    if missing_variables:
        raise RuntimeError(
            f"Problem variables do not define dataset feature column(s): {missing_variables}"
        )

    df = normalize_categorical_dataframe_values(df, feature_columns, variable_by_name)
    duplicate_count = int(len(df) - len(df.drop_duplicates(subset=feature_columns)))
    if duplicate_count:
        raise RuntimeError(
            f"{csv_path} contains {duplicate_count} duplicate design row(s) for {feature_columns}; "
            "GP-Hedge expects one objective per legal design point."
        )

    return ProblemBundle(
        dataset_name=key,
        problem_path=problem_path,
        problem_spec=problem_spec,
        df=df,
        feature_columns=feature_columns,
        target_column=target_column,
        variables=variables,
        variable_by_name=variable_by_name,
    )


def _drop_index_like_columns(df: pd.DataFrame) -> pd.DataFrame:
    drop_columns = [column for column in df.columns if str(column).startswith("Unnamed:")]
    if "" in df.columns:
        drop_columns.append("")
    return df.drop(columns=drop_columns) if drop_columns else df


def normalize_categorical_dataframe_values(
    df: pd.DataFrame,
    feature_columns: list[str],
    variable_by_name: dict[str, dict[str, Any]],
) -> pd.DataFrame:
    normalized = df.copy()
    for column in feature_columns:
        variable = variable_by_name[column]
        if str(variable.get("type") or "categorical").lower() != "categorical":
            continue
        normalized[column] = normalized[column].map(
            lambda value, variable=variable: normalize_candidate_value(value, variable)
        )
    return normalized


def build_skopt_dimensions(bundle: ProblemBundle) -> list[Any]:
    Categorical, Real = _import_skopt_space()
    dimensions: list[Any] = []
    for column in bundle.feature_columns:
        variable = bundle.variable_by_name[column]
        var_type = str(variable.get("type") or "categorical").lower()
        if var_type == "categorical":
            values = list(variable.get("domain") or [])
            if not values:
                values = sorted(bundle.df[column].dropna().unique().tolist(), key=str)
            dimensions.append(Categorical(values, name=column))
            continue

        if var_type != "continuous":
            raise ValueError(f"Unsupported GP-Hedge variable type for {column}: {var_type}")

        domain = list(variable.get("domain") or [])
        if len(domain) != 2:
            low = float(bundle.df[column].min())
            high = float(bundle.df[column].max())
        else:
            low = float(domain[0])
            high = float(domain[1])
        scale = str(variable.get("scale") or "").strip().lower()
        if scale in LOG2_SCALES:
            dimensions.append(Real(low, high, prior="log-uniform", base=2, name=column))
        elif scale in LOG_SCALES or scale.startswith("log"):
            dimensions.append(Real(low, high, prior="log-uniform", base=10, name=column))
        else:
            dimensions.append(Real(low, high, prior="uniform", name=column))
    return dimensions


def build_optimizer(bundle: ProblemBundle, seed: int, init_size: int):
    Optimizer = _import_skopt_optimizer()
    return Optimizer(
        dimensions=build_skopt_dimensions(bundle),
        base_estimator="GP",
        acq_func="gp_hedge",
        random_state=seed,
        n_initial_points=init_size,
    )


def candidate_from_row(row: pd.Series, bundle: ProblemBundle) -> dict[str, Any]:
    return {
        column: normalize_candidate_value(row[column], bundle.variable_by_name[column])
        for column in bundle.feature_columns
    }


def candidate_to_point(candidate: dict[str, Any], bundle: ProblemBundle) -> list[Any]:
    return [candidate[column] for column in bundle.feature_columns]


def point_to_candidate(point: list[Any], bundle: ProblemBundle) -> dict[str, Any]:
    return {
        column: normalize_candidate_value(point[index], bundle.variable_by_name[column])
        for index, column in enumerate(bundle.feature_columns)
    }


def normalize_candidate_value(value: Any, variable: dict[str, Any]) -> Any:
    var_type = str(variable.get("type") or "categorical").lower()
    if var_type == "categorical":
        if pd.isna(value):
            missing_label = categorical_missing_label(variable)
            if missing_label is not None:
                return missing_label
        return str(value)
    return float(value)


def categorical_missing_label(variable: dict[str, Any]) -> str | None:
    domain = [str(item) for item in variable.get("domain", [])]
    for label in ("None", "none", "n.a.", "N/A", "NA", "nan"):
        if label in domain:
            return label
    return None


def _continuous_distance(
    candidate_value: float,
    values: pd.Series,
    variable: dict[str, Any],
) -> np.ndarray:
    series = values.astype(float).to_numpy(dtype=float)
    scale = str(variable.get("scale") or "").strip().lower()
    if scale in LOG2_SCALES:
        return _log_distance(candidate_value, series, base=2.0)
    if scale in LOG_SCALES or scale.startswith("log"):
        return _log_distance(candidate_value, series, base=10.0)

    domain = list(variable.get("domain") or [])
    if len(domain) == 2:
        span = abs(float(domain[1]) - float(domain[0]))
    else:
        span = float(np.nanmax(series) - np.nanmin(series))
    span = max(span, 1.0e-12)
    return ((series - float(candidate_value)) / span) ** 2


def _log_distance(candidate_value: float, series: np.ndarray, *, base: float) -> np.ndarray:
    safe_candidate = max(float(candidate_value), 1.0e-300)
    safe_series = np.maximum(series.astype(float), 1.0e-300)
    log_candidate = math.log(safe_candidate, base)
    log_series = np.log(safe_series) / math.log(base)
    span = max(float(np.nanmax(log_series) - np.nanmin(log_series)), 1.0e-12)
    return ((log_series - log_candidate) / span) ** 2


def project_to_unevaluated_row(
    candidate: dict[str, Any],
    bundle: ProblemBundle,
    evaluated_indices: set[int],
) -> ProjectionResult:
    available_mask = ~bundle.df.index.isin(evaluated_indices)
    available = bundle.df.loc[available_mask]
    if available.empty:
        raise RuntimeError("No unevaluated dataset rows remain.")

    scores = np.zeros(len(available), dtype=float)
    for column in bundle.feature_columns:
        variable = bundle.variable_by_name[column]
        var_type = str(variable.get("type") or "categorical").lower()
        value = candidate.get(column)
        if var_type == "categorical":
            matches = available[column].astype(str).to_numpy() == str(value)
            scores += np.where(matches, 0.0, CATEGORICAL_MISMATCH_PENALTY)
        elif var_type == "continuous":
            scores += _continuous_distance(float(value), available[column], variable)
        else:
            raise ValueError(f"Unsupported GP-Hedge variable type for {column}: {var_type}")

    best_position = int(np.argmin(scores))
    row_index = int(available.index[best_position])
    row = bundle.df.loc[row_index]
    return ProjectionResult(
        row_index=row_index,
        candidate=candidate_from_row(row, bundle),
        objective=float(row[bundle.target_column]),
        distance=float(scores[best_position]),
    )


def initial_row_indices(bundle: ProblemBundle, seed: int, init_size: int) -> list[int]:
    if init_size > len(bundle.df):
        raise ValueError(f"--init-size={init_size} exceeds dataset size {len(bundle.df)}.")
    rng = np.random.default_rng(seed)
    return [int(index) for index in rng.choice(bundle.df.index.to_numpy(), size=init_size, replace=False)]


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _write_trace_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fieldnames = [
        "evaluation",
        "phase",
        "dataset_row_index",
        "objective",
        "best_so_far",
        "projection_distance",
        "proposed_candidate_json",
        "evaluated_candidate_json",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow({field: record.get(field) for field in fieldnames})


def run_trial(
    bundle: ProblemBundle,
    seed: int,
    total_budget: int,
    init_size: int,
    trial_number: int,
    output_dir: Path,
) -> dict[str, Any]:
    seed_everything(seed)
    optimizer = build_optimizer(bundle, seed=seed, init_size=init_size)
    evaluated_indices: set[int] = set()
    trace: list[float] = []
    records: list[dict[str, Any]] = []
    best_value = float("-inf")
    best_candidate: dict[str, Any] | None = None
    best_row_index: int | None = None

    log(
        f"[GP-Hedge][{bundle.dataset_name.upper()}] trial_{trial_number:02d} "
        f"seed={seed} init_size={init_size} total_budget={total_budget}"
    )

    for row_index in initial_row_indices(bundle, seed=seed, init_size=init_size):
        row = bundle.df.loc[row_index]
        candidate = candidate_from_row(row, bundle)
        objective = float(row[bundle.target_column])
        optimizer.tell(candidate_to_point(candidate, bundle), -objective)
        evaluated_indices.add(row_index)
        if objective > best_value:
            best_value = objective
            best_candidate = candidate
            best_row_index = row_index
        trace.append(best_value)
        records.append(
            {
                "evaluation": len(records) + 1,
                "phase": "initial",
                "dataset_row_index": row_index,
                "objective": objective,
                "best_so_far": best_value,
                "projection_distance": 0.0,
                "proposed_candidate_json": _json_dumps(candidate),
                "evaluated_candidate_json": _json_dumps(candidate),
            }
        )

    while len(records) < total_budget:
        proposed_point = optimizer.ask()
        proposed_candidate = point_to_candidate(proposed_point, bundle)
        projection = project_to_unevaluated_row(proposed_candidate, bundle, evaluated_indices)
        optimizer.tell(candidate_to_point(projection.candidate, bundle), -projection.objective)
        evaluated_indices.add(projection.row_index)
        if projection.objective > best_value:
            best_value = projection.objective
            best_candidate = projection.candidate
            best_row_index = projection.row_index
        trace.append(best_value)
        records.append(
            {
                "evaluation": len(records) + 1,
                "phase": "gp_hedge",
                "dataset_row_index": projection.row_index,
                "objective": projection.objective,
                "best_so_far": best_value,
                "projection_distance": projection.distance,
                "proposed_candidate_json": _json_dumps(proposed_candidate),
                "evaluated_candidate_json": _json_dumps(projection.candidate),
            }
        )
        log(
            f"[GP-Hedge][{bundle.dataset_name.upper()}] trial_{trial_number:02d} "
            f"eval={len(records)}/{total_budget} objective={projection.objective:.6g} "
            f"best={best_value:.6g} row={projection.row_index}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_trace_csv(output_dir / "trace.csv", records)
    metadata = {
        "baseline": "gp_hedge",
        "dataset": bundle.dataset_name,
        "trial_number": trial_number,
        "seed": seed,
        "problem_file": str(bundle.problem_path),
        "data_path": str(bundle.problem_spec.get("dataset", {}).get("csv_path")),
        "target_column": bundle.target_column,
        "feature_columns": bundle.feature_columns,
        "skopt_version": skopt_version(),
        "total_budget": total_budget,
        "init_size": init_size,
        "actual_evaluations": len(records),
        "best_value": best_value,
        "best_row_index": best_row_index,
        "best_candidate": best_candidate,
        "final_best": trace[-1],
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    np.savez(
        output_dir / f"{bundle.dataset_name}_gp_hedge_results.npz",
        results=np.asarray([trace], dtype=float),
        trace_lengths=np.asarray([len(trace)], dtype=int),
        trial_numbers=np.asarray([trial_number], dtype=int),
        seeds=np.asarray([seed], dtype=int),
    )
    return {
        "dataset": bundle.dataset_name,
        "trial_number": trial_number,
        "seed": seed,
        "total_budget": total_budget,
        "init_size": init_size,
        "actual_evaluations": len(records),
        "initial_best": float(trace[0]),
        "final_best": float(trace[-1]),
        "best_value": best_value,
        "best_row_index": best_row_index,
        "best_candidate": best_candidate,
        "trace_path": str(output_dir / "trace.csv"),
        "metadata_path": str(output_dir / "metadata.json"),
        "results_path": str(output_dir / f"{bundle.dataset_name}_gp_hedge_results.npz"),
        "output_dir": str(output_dir),
    }


def _trial_output_dir(output_dir: Path, trials: int, trial_number: int) -> Path:
    if trials == 1:
        return output_dir
    return output_dir / f"trial_{trial_number:02d}"


def _pad_results_matrix(results: np.ndarray, target_width: int) -> np.ndarray:
    if results.shape[1] >= target_width:
        return results
    padded = np.full((results.shape[0], target_width), np.nan, dtype=float)
    padded[:, : results.shape[1]] = results
    return padded


def _write_invocation_outputs(
    output_dir: Path,
    bundle: ProblemBundle,
    summaries: list[dict[str, Any]],
    traces: list[np.ndarray],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    max_len = max(len(trace) for trace in traces)
    results = np.full((len(traces), max_len), np.nan, dtype=float)
    trace_lengths = np.empty(len(traces), dtype=int)
    for idx, trace in enumerate(traces):
        results[idx, : len(trace)] = trace
        trace_lengths[idx] = len(trace)
    trial_numbers = np.asarray([int(item["trial_number"]) for item in summaries], dtype=int)
    seeds = np.asarray([int(item["seed"]) for item in summaries], dtype=int)
    np.savez(
        output_dir / f"{bundle.dataset_name}_gp_hedge_results.npz",
        results=results,
        trace_lengths=trace_lengths,
        trial_numbers=trial_numbers,
        seeds=seeds,
    )
    (output_dir / "run_summaries.json").write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with (output_dir / "run_summaries.csv").open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "dataset",
            "trial_number",
            "seed",
            "total_budget",
            "init_size",
            "actual_evaluations",
            "initial_best",
            "final_best",
            "best_value",
            "best_row_index",
            "trace_path",
            "metadata_path",
            "results_path",
            "output_dir",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in summaries:
            writer.writerow({key: item.get(key) for key in fieldnames})


def _trace_from_summary(summary: dict[str, Any]) -> np.ndarray:
    results_path = Path(str(summary["results_path"]))
    with np.load(results_path) as payload:
        results = np.asarray(payload["results"], dtype=float)
        trace_lengths = np.asarray(payload["trace_lengths"], dtype=int)
    return results[0, : int(trace_lengths[0])]


def main() -> None:
    ensure_conda_runtime_libraries_preferred()
    parser = argparse.ArgumentParser(description="Run scikit-optimize GP-Hedge on tabular ChemBO benchmarks.")
    parser.add_argument("--dataset", choices=DEFAULT_DATASETS, required=True)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--trial-start-index", type=int, default=1)
    parser.add_argument("--seed-start", type=int, default=42)
    parser.add_argument("--seed-step", type=int, default=1000)
    parser.add_argument("--total-budget", type=int, default=40)
    parser.add_argument("--init-size", type=int, default=10)
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    cap_cpu_threads()
    if args.trials <= 0:
        raise ValueError("--trials must be positive.")
    if args.trial_start_index <= 0:
        raise ValueError("--trial-start-index must be positive.")
    if args.seed_step <= 0:
        raise ValueError("--seed-step must be positive.")
    if args.total_budget <= 0:
        raise ValueError("--total-budget must be positive.")
    if args.init_size <= 0:
        raise ValueError("--init-size must be positive.")
    if args.init_size >= args.total_budget:
        raise ValueError("--init-size must be smaller than --total-budget.")

    bundle = load_problem_bundle(args.dataset)
    if args.total_budget > len(bundle.df):
        raise ValueError(f"--total-budget={args.total_budget} exceeds dataset size {len(bundle.df)}.")

    output_dir = Path(args.output_dir or ROOT / "outputs" / "baseline_runs" / "gp_hedge" / args.dataset)
    output_dir.mkdir(parents=True, exist_ok=True)
    log(
        f"[GP-Hedge][{bundle.dataset_name.upper()}] loaded {len(bundle.df)} legal rows, "
        f"features={bundle.feature_columns}, target={bundle.target_column}, output={output_dir}"
    )

    summaries: list[dict[str, Any]] = []
    traces: list[np.ndarray] = []
    for offset in range(args.trials):
        trial_number = args.trial_start_index + offset
        seed = args.seed_start + offset * args.seed_step
        trial_dir = _trial_output_dir(output_dir, args.trials, trial_number)
        if trial_dir.exists() and any(trial_dir.iterdir()):
            raise ValueError(f"Refusing to overwrite non-empty trial output directory: {trial_dir}")
        summary = run_trial(
            bundle=bundle,
            seed=seed,
            total_budget=args.total_budget,
            init_size=args.init_size,
            trial_number=trial_number,
            output_dir=trial_dir,
        )
        summaries.append(summary)
        traces.append(_trace_from_summary(summary))

    _write_invocation_outputs(output_dir, bundle, summaries, traces)
    final_values = np.asarray([item["final_best"] for item in summaries], dtype=float)
    log(
        f"[GP-Hedge][{bundle.dataset_name.upper()}] completed {len(summaries)} trial(s): "
        f"final_mean={float(final_values.mean()):.6g}, final_std={float(final_values.std()):.6g}"
    )


if __name__ == "__main__":
    main()
