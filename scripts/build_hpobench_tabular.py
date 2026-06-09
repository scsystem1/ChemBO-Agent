#!/usr/bin/env python3
"""Export HPOBench tabular ML benchmarks into ChemBO dataset-backed CSV/YAML files."""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import yaml


DEFAULT_MODELS = ("svm", "rf", "xgb", "nn")
DEFAULT_BENCHMARKS: dict[str, dict[str, Any]] = {
    "svm": {
        "task_id": 146212,
        "dataset_name": "openml_146212",
        "display_name": "SVM x OpenML task 146212",
        "feature_columns": ["C", "gamma", "subsample"],
        "config_columns": ["C", "gamma"],
        "fidelity_columns": ["subsample"],
        "hp_space": "2D",
        "grid": "1323 configs",
        "data_summary": "5-seed aggregated tabular benchmark",
    },
    "rf": {
        "task_id": 146606,
        "dataset_name": "openml_146606",
        "display_name": "Random Forest x OpenML task 146606",
        "feature_columns": ["max_depth", "max_features", "min_samples_leaf", "min_samples_split", "n_estimators"],
        "config_columns": ["max_depth", "max_features", "min_samples_leaf", "min_samples_split"],
        "fidelity_columns": ["n_estimators"],
        "hp_space": "4D + n_estimators fidelity",
        "grid": "36000 configs",
        "data_summary": "5-seed aggregated tabular benchmark",
    },
    "xgb": {
        "task_id": 146606,
        "dataset_name": "openml_146606",
        "display_name": "XGBoost x OpenML task 146606",
        "feature_columns": ["colsample_bytree", "eta", "max_depth", "reg_lambda", "n_estimators"],
        "config_columns": ["colsample_bytree", "eta", "max_depth", "reg_lambda"],
        "fidelity_columns": ["n_estimators"],
        "hp_space": "4D + n_estimators fidelity",
        "grid": "36000 configs",
        "data_summary": "5-seed aggregated tabular benchmark",
    },
    "nn": {
        "task_id": 168912,
        "dataset_name": "openml_168912",
        "display_name": "Neural Network x OpenML task 168912",
        "feature_columns": ["alpha", "batch_size", "depth", "learning_rate_init", "width", "iter"],
        "config_columns": ["alpha", "batch_size", "depth", "learning_rate_init", "width"],
        "fidelity_columns": ["iter"],
        "hp_space": "5D + iter fidelity",
        "grid": "150000 configs",
        "data_summary": "5-seed aggregated tabular benchmark",
    },
}


def export_table_to_csv(
    rows: Iterable[dict[str, Any]],
    *,
    feature_columns: list[str],
    output_csv: Path,
    target_metric: str = "val_acc",
) -> list[dict[str, Any]]:
    """Aggregate a HPOBench table by config/fidelity key and write a ChemBO CSV."""
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = tuple(_canonical_value(row.get(column)) for column in feature_columns)
        if any(value == "" for value in key):
            continue
        grouped[key].append(row)
    if not grouped:
        raise ValueError("No exportable HPOBench rows were found.")

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(feature_columns) + ["val_acc", "val_loss", "test_acc", "cost", "seed_count", "row_id"]
    target_column = target_metric.strip() or "val_acc"
    exported: list[dict[str, Any]] = []
    for index, (key, key_rows) in enumerate(sorted(grouped.items()), start=1):
        metrics = [_extract_row_metrics(row) for row in key_rows]
        val_acc = _finite_mean(item.get("val_acc") for item in metrics)
        test_acc = _finite_mean(item.get("test_acc") for item in metrics)
        cost = _finite_mean(item.get("cost") for item in metrics)
        target_value = {
            "val_acc": val_acc,
            "val_loss": (1.0 - val_acc) if val_acc is not None else None,
            "test_acc": test_acc,
        }.get(target_column)
        if target_value is None:
            continue
        exported_row: dict[str, Any] = {column: value for column, value in zip(feature_columns, key)}
        exported_row["val_acc"] = _format_float(val_acc) if val_acc is not None else ""
        exported_row["val_loss"] = _format_float(1.0 - val_acc) if val_acc is not None else ""
        exported_row["test_acc"] = _format_float(test_acc) if test_acc is not None else ""
        exported_row["cost"] = _format_float(cost) if cost is not None else ""
        exported_row["seed_count"] = str(len(key_rows))
        exported_row["row_id"] = f"hpo_{index:06d}"
        exported.append(exported_row)

    if target_column not in fieldnames:
        raise ValueError(f"Unsupported target metric {target_metric!r}; expected one of {fieldnames}.")
    with output_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(exported)
    return exported


def build_problem_spec(
    *,
    model: str,
    task_id: int,
    csv_path: str,
    feature_columns: list[str],
    rows: list[dict[str, Any]],
    target_metric: str = "val_acc",
    benchmark_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    direction = "minimize" if target_metric == "val_loss" else "maximize"
    benchmark_metadata = dict(benchmark_metadata or DEFAULT_BENCHMARKS.get(model, {}))
    variables = []
    for column in feature_columns:
        domain = sorted({_canonical_value(row.get(column)) for row in rows if _canonical_value(row.get(column))})
        role = "fidelity" if _looks_like_fidelity(column) else "hyperparameter"
        variables.append(
            {
                "name": column,
                "role": role,
                "type": "categorical",
                "domain": domain,
                "description": (
                    f"HPOBench {model} {'fidelity' if role == 'fidelity' else 'hyperparameter'} "
                    f"{column}; values are the discrete tabular grid levels."
                ),
            }
        )

    label = str(benchmark_metadata.get("display_name") or (model.upper() if model != "nn" else "Neural Network"))
    dataset_name = str(benchmark_metadata.get("dataset_name") or "").strip()
    return {
        "problem": {
            "application_domain": "hpo",
            "domain_profile": "ml_hyperparameter_optimization",
            "description": (
                f"Optimize HPOBench {label} validation accuracy on OpenML task {task_id}. "
                "The search space is restricted to configurations present in the exported tabular benchmark CSV. "
                "Scores are revealed only through the dataset oracle."
            ),
            "reaction_type": f"HPOBENCH_{model.upper()}",
            "target_metric": target_metric,
            "optimization_direction": direction,
            "budget": 40,
            "hpo_benchmark": {
                "source": "HPOBench",
                "model": model,
                "task_id": int(task_id),
                "dataset_name": dataset_name,
                "metric": target_metric,
                "hp_space": benchmark_metadata.get("hp_space", ""),
                "grid": benchmark_metadata.get("grid", ""),
                "data_summary": benchmark_metadata.get("data_summary", ""),
                "config_columns": list(benchmark_metadata.get("config_columns") or []),
                "fidelity_columns": list(benchmark_metadata.get("fidelity_columns") or []),
            },
            "variables": variables,
            "constraints": [
                f"Only propose configurations that correspond to rows present in the HPOBench {model} task {task_id} dataset.",
                "Treat fidelity columns as part of the legal tabular configuration key.",
            ],
            "dataset": {
                "csv_path": csv_path,
                "feature_columns": feature_columns,
                "target_column": target_metric,
                "row_id_column": "row_id",
            },
            "initial_data": [],
        }
    }


def export_hpobench_model(
    *,
    model: str,
    task_id: int,
    output_dir: Path,
    examples_dir: Path,
    target_metric: str = "val_acc",
    data_dir: Path | None = None,
    write_problem: bool = True,
    benchmark_metadata: dict[str, Any] | None = None,
) -> tuple[Path, Path | None]:
    benchmark_metadata = dict(benchmark_metadata or DEFAULT_BENCHMARKS.get(model, {}))
    try:
        from hpobench.benchmarks.ml.tabular_benchmark import TabularBenchmark
    except Exception as exc:  # pragma: no cover - depends on optional environment
        raise RuntimeError(
            "HPOBench is not installed. Install optional dependencies first, for example: "
            "python -m pip install git+https://github.com/automl/HPOBench.git pyarrow"
        ) from exc

    benchmark_kwargs: dict[str, Any] = {"model": model, "task_id": int(task_id)}
    if data_dir is not None:
        benchmark_kwargs["data_dir"] = data_dir
    benchmark = TabularBenchmark(**benchmark_kwargs)
    table = benchmark.table
    config_columns = _space_names(benchmark.get_configuration_space())
    fidelity_columns = _space_names(benchmark.get_fidelity_space())
    expected_columns = list(benchmark_metadata.get("feature_columns") or [])
    if expected_columns:
        feature_columns = expected_columns
        missing = [column for column in feature_columns if column not in set(config_columns) | set(fidelity_columns)]
        if missing:
            raise RuntimeError(
                f"HPOBench metadata for model={model}, task_id={task_id} did not expose expected columns: {missing}. "
                f"Available config={config_columns}, fidelity={fidelity_columns}"
            )
    else:
        feature_columns = config_columns + fidelity_columns
    if not feature_columns:
        raise RuntimeError(f"Could not resolve HPOBench feature columns for model={model}, task_id={task_id}.")
    rows = table.to_dict(orient="records")
    omitted_fidelities = [column for column in fidelity_columns if column not in feature_columns]
    if omitted_fidelities:
        rows = _filter_rows_to_max_fidelity(rows, omitted_fidelities, benchmark.get_fidelity_space())
    output_csv = output_dir / f"hpobench_{model}_{task_id}.csv"
    exported_rows = export_table_to_csv(
        rows,
        feature_columns=feature_columns,
        output_csv=output_csv,
        target_metric=target_metric,
    )
    problem_path: Path | None = None
    if write_problem:
        problem = build_problem_spec(
            model=model,
            task_id=int(task_id),
            csv_path=f"../data/HPOBench/{output_csv.name}",
            feature_columns=feature_columns,
            rows=exported_rows,
            target_metric=target_metric,
            benchmark_metadata=benchmark_metadata,
        )
        examples_dir.mkdir(parents=True, exist_ok=True)
        problem_path = examples_dir / f"hpobench_{model}_{task_id}_problem.yaml"
        problem_path.write_text(yaml.safe_dump(problem, sort_keys=False, allow_unicode=False), encoding="utf-8")
    return output_csv, problem_path


def _space_names(space: Any) -> list[str]:
    if space is None:
        return []
    if hasattr(space, "get_hyperparameter_names"):
        return [str(item) for item in space.get_hyperparameter_names()]
    hps = space.get_hyperparameters() if hasattr(space, "get_hyperparameters") else []
    return [str(getattr(item, "name", "")) for item in hps if str(getattr(item, "name", ""))]


def _filter_rows_to_max_fidelity(
    rows: list[dict[str, Any]],
    fidelity_columns: list[str],
    fidelity_space: Any,
) -> list[dict[str, Any]]:
    max_values = _max_fidelity_values(fidelity_space, fidelity_columns)
    if not max_values:
        return rows
    filtered = [
        row for row in rows
        if all(_canonical_value(row.get(column)) == _canonical_value(value) for column, value in max_values.items())
    ]
    if not filtered:
        raise RuntimeError(f"No HPOBench rows matched max omitted fidelity values: {max_values}")
    return filtered


def _max_fidelity_values(space: Any, fidelity_columns: list[str]) -> dict[str, Any]:
    names = set(fidelity_columns)
    values: dict[str, Any] = {}
    hps = space.get_hyperparameters() if hasattr(space, "get_hyperparameters") else []
    for hp in hps:
        name = str(getattr(hp, "name", ""))
        if name not in names:
            continue
        sequence = list(getattr(hp, "sequence", []) or [])
        if not sequence:
            choices = list(getattr(hp, "choices", []) or [])
            sequence = choices
        if sequence:
            values[name] = max(sequence, key=_numeric_sort_key)
    return values


def _extract_row_metrics(row: dict[str, Any]) -> dict[str, float | None]:
    result = _coerce_result(row.get("result"))
    info = result.get("info", {}) if isinstance(result.get("info"), dict) else {}
    val_acc = _nested_float(info, "val_scores", "acc")
    test_acc = _nested_float(info, "test_scores", "acc")
    model_cost = _coerce_float(info.get("model_cost"))
    val_cost = _nested_float(info, "val_scores", "cost")
    cost_parts = [item for item in (model_cost, val_cost) if item is not None]
    return {
        "val_acc": val_acc,
        "test_acc": test_acc,
        "cost": sum(cost_parts) if cost_parts else model_cost,
    }


def _coerce_result(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _nested_float(payload: dict[str, Any], *path: str) -> float | None:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return _coerce_float(current)


def _finite_mean(values: Iterable[float | None]) -> float | None:
    usable = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if not usable:
        return None
    return sum(usable) / len(usable)


def _coerce_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _format_float(value: float) -> str:
    return format(float(value), ".15g")


def _canonical_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return _format_float(value)
    return str(value).strip()


def _looks_like_fidelity(column: str) -> bool:
    lower = str(column or "").strip().lower()
    return lower in {"budget", "epoch", "epochs", "iter", "iteration", "n_estimators", "dataset_fraction", "subsample"} or "fidelity" in lower


def _numeric_sort_key(value: Any) -> tuple[int, float | str]:
    try:
        return (0, float(value))
    except (TypeError, ValueError):
        return (1, str(value))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS), choices=list(DEFAULT_MODELS))
    parser.add_argument("--task-id", type=int, default=None, help="Optional override applied to all selected models.")
    parser.add_argument("--target-metric", default="test_acc", choices=["val_acc", "val_loss", "test_acc"])
    parser.add_argument("--output-dir", type=Path, default=Path("data/HPOBench"))
    parser.add_argument("--examples-dir", type=Path, default=Path("examples"))
    parser.add_argument("--data-dir", type=Path, default=None, help="Optional HPOBench tabular data cache directory.")
    parser.add_argument("--skip-problem-yaml", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    for model in args.models:
        metadata = dict(DEFAULT_BENCHMARKS[model])
        task_id = int(args.task_id if args.task_id is not None else metadata["task_id"])
        csv_path, problem_path = export_hpobench_model(
            model=model,
            task_id=task_id,
            output_dir=args.output_dir,
            examples_dir=args.examples_dir,
            target_metric=args.target_metric,
            data_dir=args.data_dir,
            write_problem=not args.skip_problem_yaml,
            benchmark_metadata=metadata,
        )
        if problem_path is None:
            print(f"[HPOBench] wrote {csv_path}")
        else:
            print(f"[HPOBench] wrote {csv_path} and {problem_path}")


if __name__ == "__main__":
    main()
