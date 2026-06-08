#!/bin/sh
""":"
exec python3 "$0" "$@"
":"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.dataset_oracle import DatasetOracle
from core.problem_loader import load_problem_file
from embeddings.descriptors.registry import build_descriptor_feature_spec
from pools.component_pools import DeepEnsembleSurrogate


DEFAULT_OUTPUT_DIR = ROOT_DIR / "plot" / "descriptor_prediction_ablation"

DATASET_SPECS: dict[str, dict[str, Any]] = {
    "dar": {
        "problem_file": ROOT_DIR / "examples" / "dar_problem.yaml",
        "selection": {
            "base_SMILES": [
                ("base_reagent_physchem", "formula_weight_g_mol"),
                ("base_reagent_physchem", "pKa_conjugate_acid_water"),
                ("base_reagent_physchem", "cation_radius_shannon_cn6_A"),
            ],
            "ligand_SMILES": [
                ("rdkit_2d", "MolWt"),
                ("rdkit_2d", "MolLogP"),
                ("rdkit_2d", "BertzCT"),
            ],
            "solvent_SMILES": [
                ("solvent_physchem", "dielectric_constant_25C"),
                ("solvent_physchem", "donor_number_kcal_mol"),
                ("solvent_physchem", "boiling_point_C"),
            ],
        },
    },
    "suzuki": {
        "problem_file": ROOT_DIR / "examples" / "suzuki_problem.yaml",
        "selection": {
            "Reactant_1_Name": [
                ("rdkit_2d", "MolWt"),
                ("rdkit_substructure_counts", "aryl_halide_count"),
                ("rdkit_substructure_counts", "trifluoroborate_group_count"),
            ],
            "Reactant_2_Name": [
                ("rdkit_2d", "MolWt"),
                ("rdkit_substructure_counts", "boronic_acid_group_count"),
                ("rdkit_substructure_counts", "trifluoroborate_group_count"),
            ],
            "Ligand_Short_Hand": [
                ("rdkit_2d", "MolWt"),
                ("rdkit_2d", "MolLogP"),
                ("rdkit_substructure_counts", "phosphine_donor_count"),
            ],
            "Reagent_1_Short_Hand": [
                ("rdkit_2d", "MolWt"),
                ("base_reagent_physchem", "pKa_conjugate_acid_water"),
                ("base_reagent_physchem", "cation_radius_shannon_cn6_A"),
            ],
            "Solvent_1_Short_Hand": [
                ("rdkit_2d", "MolWt"),
                ("solvent_physchem", "water_volume_fraction"),
                ("solvent_physchem", "donor_number_kcal_mol"),
            ],
        },
    },
    "ocm": {
        "problem_file": ROOT_DIR / "examples" / "ocm_problem.yaml",
        "selection": {
            "M1": [
                ("element_oxide_physchem", "pauling_electronegativity"),
                ("element_oxide_physchem", "first_ionization_energy_eV"),
                ("element_oxide_physchem", "oxide_formation_enthalpy_kJ_mol_O"),
            ],
            "M2": [
                ("element_oxide_physchem", "pauling_electronegativity"),
                ("element_oxide_physchem", "first_ionization_energy_eV"),
                ("element_oxide_physchem", "oxide_formation_enthalpy_kJ_mol_O"),
            ],
            "M3": [
                ("element_oxide_physchem", "pauling_electronegativity"),
                ("element_oxide_physchem", "first_ionization_energy_eV"),
                ("element_oxide_physchem", "oxide_formation_enthalpy_kJ_mol_O"),
            ],
            "Support": [
                ("support_material_physchem", "point_of_zero_charge_pH"),
                ("support_material_physchem", "band_gap_eV"),
                ("support_material_physchem", "oxygen_to_metal_ratio"),
            ],
        },
    },
}

DEEP_ENSEMBLE_PARAMS = {
    "n_models": 3,
    "n_epochs": 120,
    "hidden1": 64,
    "hidden2": 32,
    "learning_rate": 1e-3,
    "weight_decay": 1e-3,
}

METRIC_FIELDS = [
    "dataset",
    "model",
    "seed_index",
    "split_seed",
    "encoding",
    "split",
    "n_train",
    "n_val",
    "n_test",
    "rmse",
    "mae",
    "r2",
    "spearman",
]

SUMMARY_FIELDS = [
    "dataset",
    "model",
    "n_pairs",
    "rmse_delta_mean",
    "rmse_delta_std",
    "rmse_delta_ci95_low",
    "rmse_delta_ci95_high",
    "rmse_p_value",
    "rmse_relative_improvement_pct",
    "mae_delta_mean",
    "r2_delta_mean",
    "spearman_delta_mean",
    "verdict",
]


@dataclass(frozen=True)
class LoadedProblemDataset:
    name: str
    problem_spec: dict[str, Any]
    search_space: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    y: np.ndarray
    descriptor_feature_spec: dict[str, Any]
    descriptor_schema: dict[str, Any]


def _silence_rdkit_warnings() -> None:
    try:
        from rdkit import RDLogger
    except Exception:
        return
    RDLogger.DisableLog("rdApp.warning")
    RDLogger.DisableLog("rdApp.error")


def _selection_payload(selection: dict[str, list[tuple[str, str]]]) -> dict[str, Any]:
    return {
        "selected_descriptors_by_variable": {
            variable: [{"pool": pool, "name": descriptor} for pool, descriptor in descriptors]
            for variable, descriptors in selection.items()
        },
        "rationales": {},
        "warnings": [],
    }


def _load_problem_dataset(dataset_name: str) -> LoadedProblemDataset:
    spec_entry = DATASET_SPECS[dataset_name]
    problem = load_problem_file(spec_entry["problem_file"])
    if not isinstance(problem, dict):
        raise RuntimeError(f"{dataset_name} expects a structured YAML problem file.")
    oracle = DatasetOracle.from_problem_spec(problem)
    if oracle is None:
        raise RuntimeError(f"{dataset_name} problem file does not define a dataset oracle.")

    candidates = [dict(candidate) for candidate in oracle.candidates]
    y = np.asarray([float(oracle.lookup(candidate)["result"]) for candidate in candidates], dtype=float)
    payload = _selection_payload(spec_entry["selection"])
    feature_spec = build_descriptor_feature_spec(problem_spec=problem, selection_payload=payload)
    if not feature_spec.get("variable_features"):
        raise RuntimeError(f"{dataset_name} descriptor schema produced no feature maps.")

    return LoadedProblemDataset(
        name=dataset_name,
        problem_spec=problem,
        search_space=list(problem.get("variables") or []),
        candidates=candidates,
        y=y,
        descriptor_feature_spec=feature_spec,
        descriptor_schema=payload,
    )


def _split_indices(n_rows: int, seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if n_rows < 5:
        raise RuntimeError(f"Need at least 5 rows for a 60/20/20 split; got {n_rows}.")
    indices = np.random.default_rng(seed).permutation(n_rows)
    n_train = int(math.floor(0.60 * n_rows))
    n_val = int(math.floor(0.20 * n_rows))
    train_idx = indices[:n_train]
    val_idx = indices[n_train : n_train + n_val]
    test_idx = indices[n_train + n_val :]
    return train_idx, val_idx, test_idx


def _encode_for_extra_trees(
    search_space: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    feature_spec: dict[str, Any],
) -> np.ndarray:
    encoder = DeepEnsembleSurrogate(search_space=search_space, params={"n_models": 1, "n_epochs": 1}, feature_spec=feature_spec)
    return np.asarray(encoder._encode_candidates(candidates), dtype=float)


def _fit_predict_extra_trees(
    dataset: LoadedProblemDataset,
    feature_spec: dict[str, Any],
    train_idx: np.ndarray,
    val_idx: np.ndarray,
    test_idx: np.ndarray,
    seed: int,
) -> dict[str, np.ndarray]:
    try:
        from sklearn.ensemble import ExtraTreesRegressor
    except ImportError as exc:
        raise RuntimeError("scikit-learn is required for the extra_trees ablation model.") from exc

    X = _encode_for_extra_trees(dataset.search_space, dataset.candidates, feature_spec)
    model = ExtraTreesRegressor(
        n_estimators=300,
        random_state=seed,
        n_jobs=-1,
        min_samples_leaf=1,
    )
    model.fit(X[train_idx], dataset.y[train_idx])
    return {
        "val": np.asarray(model.predict(X[val_idx]), dtype=float),
        "test": np.asarray(model.predict(X[test_idx]), dtype=float),
    }


def _fit_predict_deep_ensemble(
    dataset: LoadedProblemDataset,
    feature_spec: dict[str, Any],
    train_idx: np.ndarray,
    val_idx: np.ndarray,
    test_idx: np.ndarray,
    seed: int,
) -> dict[str, np.ndarray]:
    params = dict(DEEP_ENSEMBLE_PARAMS)
    params["random_seed"] = int(seed)
    model = DeepEnsembleSurrogate(
        search_space=dataset.search_space,
        params=params,
        feature_spec=feature_spec,
    )
    train_candidates = [dataset.candidates[index] for index in train_idx]
    model.fit(train_candidates, dataset.y[train_idx])
    val_mu, _ = model.predict([dataset.candidates[index] for index in val_idx])
    test_mu, _ = model.predict([dataset.candidates[index] for index in test_idx])
    return {"val": np.asarray(val_mu, dtype=float), "test": np.asarray(test_mu, dtype=float)}


def _spearman(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    if len(y_true) < 2:
        return float("nan")
    try:
        from scipy.stats import spearmanr

        value = spearmanr(y_true, y_pred).correlation
        return float(value) if value is not None and np.isfinite(float(value)) else float("nan")
    except Exception:
        true_rank = _average_ranks(y_true)
        pred_rank = _average_ranks(y_pred)
        if np.std(true_rank) < 1e-12 or np.std(pred_rank) < 1e-12:
            return float("nan")
        return float(np.corrcoef(true_rank, pred_rank)[0, 1])


def _average_ranks(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values), dtype=float)
    sorted_values = values[order]
    start = 0
    while start < len(values):
        end = start + 1
        while end < len(values) and sorted_values[end] == sorted_values[start]:
            end += 1
        rank = 0.5 * (start + end - 1) + 1.0
        ranks[order[start:end]] = rank
        start = end
    return ranks


def _metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    y_true = np.asarray(y_true, dtype=float).reshape(-1)
    y_pred = np.asarray(y_pred, dtype=float).reshape(-1)
    residual = y_true - y_pred
    mse = float(np.mean(residual**2)) if len(residual) else float("nan")
    mae = float(np.mean(np.abs(residual))) if len(residual) else float("nan")
    ss_res = float(np.sum(residual**2))
    ss_tot = float(np.sum((y_true - float(np.mean(y_true))) ** 2)) if len(y_true) else 0.0
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else float("nan")
    return {
        "rmse": float(math.sqrt(mse)) if np.isfinite(mse) else float("nan"),
        "mae": mae,
        "r2": float(r2),
        "spearman": _spearman(y_true, y_pred),
    }


def _ci95(values: np.ndarray) -> tuple[float, float]:
    finite = np.asarray([value for value in values if np.isfinite(value)], dtype=float)
    if len(finite) == 0:
        return float("nan"), float("nan")
    mean = float(np.mean(finite))
    if len(finite) == 1:
        return mean, mean
    try:
        from scipy.stats import t

        critical = float(t.ppf(0.975, df=len(finite) - 1))
    except Exception:
        critical = 1.96
    half_width = critical * float(np.std(finite, ddof=1)) / math.sqrt(len(finite))
    return mean - half_width, mean + half_width


def _paired_t_pvalue(values: np.ndarray) -> float:
    finite = np.asarray([value for value in values if np.isfinite(value)], dtype=float)
    if len(finite) < 2:
        return float("nan")
    if float(np.std(finite, ddof=1)) < 1e-12:
        return 1.0 if abs(float(np.mean(finite))) < 1e-12 else 0.0
    try:
        from scipy.stats import ttest_1samp

        return float(ttest_1samp(finite, 0.0).pvalue)
    except Exception:
        return float("nan")


def _verdict(relative_rmse_improvement_pct: float, ci_low: float, ci_high: float, rmse_delta_mean: float) -> str:
    if not np.isfinite(relative_rmse_improvement_pct) or not np.isfinite(ci_low) or not np.isfinite(ci_high):
        return "insufficient_data"
    if rmse_delta_mean > 0:
        return "descriptor_worse"
    if relative_rmse_improvement_pct >= 5.0 and ci_high < 0.0:
        return "clearly_more_accurate"
    if relative_rmse_improvement_pct >= 2.0 and ci_high < 0.0:
        return "slightly_more_accurate"
    return "not_material_or_uncertain"


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def _model_predictions(
    model_name: str,
    dataset: LoadedProblemDataset,
    feature_spec: dict[str, Any],
    train_idx: np.ndarray,
    val_idx: np.ndarray,
    test_idx: np.ndarray,
    seed: int,
) -> dict[str, np.ndarray]:
    if model_name == "extra_trees":
        return _fit_predict_extra_trees(dataset, feature_spec, train_idx, val_idx, test_idx, seed)
    if model_name == "deep_ensemble":
        return _fit_predict_deep_ensemble(dataset, feature_spec, train_idx, val_idx, test_idx, seed)
    raise ValueError(f"Unknown model: {model_name}")


def run_ablation(
    *,
    dataset_names: list[str],
    model_names: list[str],
    n_seeds: int,
    output_dir: Path,
    base_seed: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    _silence_rdkit_warnings()
    output_dir.mkdir(parents=True, exist_ok=True)
    metric_rows: list[dict[str, Any]] = []

    for dataset_name in dataset_names:
        dataset = _load_problem_dataset(dataset_name)
        print(f"\nDataset {dataset_name}: {len(dataset.candidates)} rows")
        for seed_index in range(n_seeds):
            split_seed = int(base_seed + seed_index * 9973)
            train_idx, val_idx, test_idx = _split_indices(len(dataset.candidates), split_seed)
            feature_specs = {
                "no_descriptor": {},
                "descriptor": dataset.descriptor_feature_spec,
            }
            for model_name in model_names:
                for encoding, feature_spec in feature_specs.items():
                    print(f"  seed={seed_index + 1:02d}/{n_seeds} model={model_name} encoding={encoding}")
                    predictions = _model_predictions(
                        model_name,
                        dataset,
                        feature_spec,
                        train_idx,
                        val_idx,
                        test_idx,
                        seed=split_seed,
                    )
                    for split_name, indices in (("val", val_idx), ("test", test_idx)):
                        scores = _metrics(dataset.y[indices], predictions[split_name])
                        metric_rows.append(
                            {
                                "dataset": dataset_name,
                                "model": model_name,
                                "seed_index": seed_index + 1,
                                "split_seed": split_seed,
                                "encoding": encoding,
                                "split": split_name,
                                "n_train": len(train_idx),
                                "n_val": len(val_idx),
                                "n_test": len(test_idx),
                                **scores,
                            }
                        )

    summary_rows = summarize_paired_deltas(metric_rows)
    _write_csv(output_dir / "descriptor_prediction_ablation_metrics.csv", metric_rows, METRIC_FIELDS)
    _write_csv(output_dir / "descriptor_prediction_ablation_summary.csv", summary_rows, SUMMARY_FIELDS)
    return metric_rows, summary_rows


def summarize_paired_deltas(metric_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in metric_rows:
        if row.get("split") != "test":
            continue
        key = (str(row["dataset"]), str(row["model"]), int(row["seed_index"]))
        grouped[key][str(row["encoding"])] = row

    by_dataset_model: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for (dataset, model, _seed_index), encodings in grouped.items():
        if "no_descriptor" in encodings and "descriptor" in encodings:
            by_dataset_model[(dataset, model)].append((encodings["no_descriptor"], encodings["descriptor"]))

    summary_rows: list[dict[str, Any]] = []
    for (dataset, model), pairs in sorted(by_dataset_model.items()):
        rmse_deltas = np.asarray([float(desc["rmse"]) - float(base["rmse"]) for base, desc in pairs], dtype=float)
        mae_deltas = np.asarray([float(desc["mae"]) - float(base["mae"]) for base, desc in pairs], dtype=float)
        r2_deltas = np.asarray([float(desc["r2"]) - float(base["r2"]) for base, desc in pairs], dtype=float)
        spearman_deltas = np.asarray([float(desc["spearman"]) - float(base["spearman"]) for base, desc in pairs], dtype=float)
        base_rmse = np.asarray([float(base["rmse"]) for base, _desc in pairs], dtype=float)
        rmse_delta_mean = float(np.mean(rmse_deltas)) if len(rmse_deltas) else float("nan")
        ci_low, ci_high = _ci95(rmse_deltas)
        relative_improvement = (
            float(-100.0 * rmse_delta_mean / np.mean(base_rmse))
            if len(base_rmse) and abs(float(np.mean(base_rmse))) > 1e-12
            else float("nan")
        )
        summary_rows.append(
            {
                "dataset": dataset,
                "model": model,
                "n_pairs": len(pairs),
                "rmse_delta_mean": rmse_delta_mean,
                "rmse_delta_std": float(np.std(rmse_deltas, ddof=1)) if len(rmse_deltas) > 1 else 0.0,
                "rmse_delta_ci95_low": ci_low,
                "rmse_delta_ci95_high": ci_high,
                "rmse_p_value": _paired_t_pvalue(rmse_deltas),
                "rmse_relative_improvement_pct": relative_improvement,
                "mae_delta_mean": float(np.mean(mae_deltas)) if len(mae_deltas) else float("nan"),
                "r2_delta_mean": float(np.mean(r2_deltas)) if len(r2_deltas) else float("nan"),
                "spearman_delta_mean": float(np.mean(spearman_deltas)) if len(spearman_deltas) else float("nan"),
                "verdict": _verdict(relative_improvement, ci_low, ci_high, rmse_delta_mean),
            }
        )
    return summary_rows


def print_summary(summary_rows: list[dict[str, Any]], output_dir: Path) -> None:
    print("\nDescriptor prediction ablation summary")
    print("negative RMSE delta means descriptor is better")
    print("-" * 112)
    print(
        f"{'dataset':<10} {'model':<14} {'pairs':>5} {'rmse_delta':>12} "
        f"{'ci95':>25} {'rel_impr%':>10} {'p':>9} {'verdict':<24}"
    )
    for row in summary_rows:
        ci = f"[{float(row['rmse_delta_ci95_low']):.4g}, {float(row['rmse_delta_ci95_high']):.4g}]"
        print(
            f"{row['dataset']:<10} {row['model']:<14} {int(row['n_pairs']):>5} "
            f"{float(row['rmse_delta_mean']):>12.4g} {ci:>25} "
            f"{float(row['rmse_relative_improvement_pct']):>10.3g} "
            f"{float(row['rmse_p_value']):>9.3g} {row['verdict']:<24}"
        )
    print("-" * 112)
    print(f"Metrics CSV: {output_dir / 'descriptor_prediction_ablation_metrics.csv'}")
    print(f"Summary CSV: {output_dir / 'descriptor_prediction_ablation_summary.csv'}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline prediction ablation for descriptor vs no-descriptor features.")
    parser.add_argument("--datasets", nargs="+", choices=sorted(DATASET_SPECS), default=["dar", "suzuki", "ocm"])
    parser.add_argument("--models", nargs="+", choices=["deep_ensemble", "extra_trees"], default=["deep_ensemble", "extra_trees"])
    parser.add_argument("--seeds", type=int, default=10, help="Number of deterministic 60/20/20 split seeds to run.")
    parser.add_argument("--base-seed", type=int, default=20260604, help="Base seed used to derive split seeds.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.seeds <= 0:
        raise SystemExit("--seeds must be positive.")
    _metric_rows, summary_rows = run_ablation(
        dataset_names=list(args.datasets),
        model_names=list(args.models),
        n_seeds=int(args.seeds),
        output_dir=Path(args.output_dir).expanduser().resolve(),
        base_seed=int(args.base_seed),
    )
    print_summary(summary_rows, Path(args.output_dir).expanduser().resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
