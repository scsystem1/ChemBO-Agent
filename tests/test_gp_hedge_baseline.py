from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from baseline.gp_hedge import run_tabular_gp_hedge as gp_hedge


def _skopt_importable() -> bool:
    if importlib.util.find_spec("skopt") is None:
        return False
    try:
        import skopt  # noqa: F401
    except ImportError:
        return False
    return True


def _dataset_csv_exists(dataset_name: str) -> bool:
    problem_path = Path(gp_hedge.DATASET_PROBLEM_FILES[dataset_name])
    problem_spec = gp_hedge.load_problem_file(problem_path)
    dataset_spec = problem_spec.get("dataset", {}) if isinstance(problem_spec, dict) else {}
    csv_path = Path(str(dataset_spec.get("csv_path") or "")).expanduser()
    if not csv_path.is_absolute():
        csv_path = (problem_path.parent / csv_path).resolve()
    return csv_path.exists()


def test_default_dataset_mapping_covers_eight_benchmarks() -> None:
    assert gp_hedge.DEFAULT_DATASETS == [
        "dar",
        "ocm",
        "suzuki",
        "oer",
        "hpobench_svm",
        "hpobench_rf",
        "hpobench_xgb",
        "hpobench_nn",
    ]
    assert set(gp_hedge.DATASET_PROBLEM_FILES) == set(gp_hedge.DEFAULT_DATASETS)
    for path in gp_hedge.DATASET_PROBLEM_FILES.values():
        assert Path(path).exists()


def test_ocm_uses_problem_yaml_features_without_derived_ct() -> None:
    bundle = gp_hedge.load_problem_bundle("ocm")
    assert bundle.feature_columns == [
        "M1",
        "M2",
        "M3",
        "Support",
        "Temp",
        "Ar_flow",
        "CH4_flow",
        "O2_flow",
    ]
    assert "CT" not in bundle.feature_columns


def test_suzuki_missing_categorical_values_are_yaml_none() -> None:
    bundle = gp_hedge.load_problem_bundle("suzuki")
    assert bundle.df["Ligand_Short_Hand"].isna().sum() == 0
    assert bundle.df["Reagent_1_Short_Hand"].isna().sum() == 0
    assert "nan" not in set(bundle.df["Ligand_Short_Hand"].astype(str))
    assert "nan" not in set(bundle.df["Reagent_1_Short_Hand"].astype(str))
    assert "None" in set(bundle.df["Ligand_Short_Hand"].astype(str))
    assert "None" in set(bundle.df["Reagent_1_Short_Hand"].astype(str))


@pytest.mark.skipif(not _skopt_importable(), reason="scikit-optimize is not importable in this runtime")
@pytest.mark.skipif(not _dataset_csv_exists("hpobench_svm"), reason="HPOBench CSV is not available in this checkout")
def test_yaml_variable_types_map_to_skopt_dimensions() -> None:
    dar = gp_hedge.load_problem_bundle("dar")
    dar_dimensions = gp_hedge.build_skopt_dimensions(dar)
    assert [type(dimension).__name__ for dimension in dar_dimensions] == [
        "Categorical",
        "Categorical",
        "Categorical",
        "Real",
        "Real",
    ]

    svm = gp_hedge.load_problem_bundle("hpobench_svm")
    svm_dimensions = gp_hedge.build_skopt_dimensions(svm)
    assert [type(dimension).__name__ for dimension in svm_dimensions] == ["Real", "Real"]
    assert all(getattr(dimension, "prior", None) == "log-uniform" for dimension in svm_dimensions)


@pytest.mark.skipif(not _dataset_csv_exists("hpobench_svm"), reason="HPOBench CSV is not available in this checkout")
def test_projection_does_not_return_evaluated_row() -> None:
    bundle = gp_hedge.load_problem_bundle("hpobench_svm")
    first_index = int(bundle.df.index[0])
    candidate = gp_hedge.candidate_from_row(bundle.df.loc[first_index], bundle)
    projection = gp_hedge.project_to_unevaluated_row(candidate, bundle, {first_index})
    assert projection.row_index != first_index
    assert projection.row_index not in {first_index}


def test_oer_projection_returns_dataset_backed_simplex_row() -> None:
    bundle = gp_hedge.load_problem_bundle("oer")
    candidate = {
        "ni_load": 0.33,
        "fe_load": 0.27,
        "co_load": 0.11,
        "mn_load": 0.06,
        "ce_load": 0.13,
        "la_load": 0.10,
    }
    projection = gp_hedge.project_to_unevaluated_row(candidate, bundle, set())
    total_loading = sum(float(projection.candidate[column]) for column in bundle.feature_columns)
    assert total_loading == pytest.approx(1.0, abs=1.0e-9)
    assert int(projection.row_index) in set(bundle.df.index.astype(int))


def test_hpo_log_distance_uses_log_space() -> None:
    values = pd.Series([1.0, 100.0])
    variable = {"name": "x", "type": "continuous", "domain": [1.0, 100.0], "scale": "log"}
    distances = gp_hedge._continuous_distance(10.0, values, variable)
    assert distances[0] == pytest.approx(distances[1])

    values_log2 = pd.Series([1.0, 16.0])
    variable_log2 = {"name": "x", "type": "continuous", "domain": [1.0, 16.0], "scale": "log2"}
    distances_log2 = gp_hedge._continuous_distance(4.0, values_log2, variable_log2)
    assert distances_log2[0] == pytest.approx(distances_log2[1])


def test_initial_indices_are_unique_and_seeded() -> None:
    bundle = gp_hedge.load_problem_bundle("dar")
    indices_a = gp_hedge.initial_row_indices(bundle, seed=42, init_size=10)
    indices_b = gp_hedge.initial_row_indices(bundle, seed=42, init_size=10)
    assert indices_a == indices_b
    assert len(indices_a) == len(set(indices_a)) == 10
