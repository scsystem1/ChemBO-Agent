from __future__ import annotations

import csv
from pathlib import Path

from config.settings import Settings
from core.autobo_prompts import build_acquisition_selection_prompt, build_surrogate_plausibility_prompt
from core.dataset_oracle import DatasetOracle
from core.problem_loader import load_problem_file
from core.state import create_initial_state
from core.warm_start import _build_warm_start_direct_candidate_pool_prompt
from embeddings.descriptors.selector_prompt import build_descriptor_selection_prompt
from knowledge.prompts import build_prior_writer_prompt
from pools.deep_ensemble_features import build_deep_ensemble_feature_spec_prompt
from scripts.build_hpobench_tabular import DEFAULT_BENCHMARKS, build_problem_spec, export_table_to_csv
from tools.retrieval_tools import build_retrieval_tools


FORBIDDEN_HPO_PROMPT_WORDS = ("chemical", "chemistry", "reaction", "physicochemical", "RDKit")


def _write_hpo_fixture(tmp_path: Path) -> Path:
    csv_path = tmp_path / "hpobench_svm_146212.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["C", "gamma", "subsample", "test_acc", "test_loss", "seed_count", "seeds", "row_id"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "C": "1.0",
                "gamma": "0.01",
                "subsample": "0.5",
                "test_acc": "0.82",
                "test_loss": "0.18",
                "seed_count": "2",
                "seeds": "1;2",
                "row_id": "hpo_000001",
            }
        )
    problem = build_problem_spec(
        model="svm",
        task_id=146212,
        csv_path=str(csv_path),
        feature_columns=["C", "gamma", "subsample"],
        rows=[
            {"C": "1.0", "gamma": "0.01", "subsample": "0.5"},
            {"C": "10.0", "gamma": "0.1", "subsample": "1.0"},
        ],
        target_metric="test_acc",
    )
    yaml_path = tmp_path / "hpobench_svm_problem.yaml"
    import yaml

    yaml_path.write_text(yaml.safe_dump(problem, sort_keys=False), encoding="utf-8")
    return yaml_path


def test_hpo_problem_loads_with_dataset_oracle(tmp_path: Path) -> None:
    problem = load_problem_file(_write_hpo_fixture(tmp_path))

    assert problem["application_domain"] == "hpo"
    assert problem["domain_profile"] == "ml_hyperparameter_optimization"
    assert problem["hpo_benchmark"]["model"] == "svm"
    assert problem["hpo_benchmark"]["task_id"] == 146212
    assert problem["hpo_benchmark"]["dataset_name"] == "openml_146212"
    assert problem["target_metric"] == "test_acc"
    assert problem["optimization_direction"] == "maximize"

    oracle = DatasetOracle.from_problem_spec(problem)
    assert oracle is not None
    assert oracle.feature_columns == ("C", "gamma", "subsample")
    match = oracle.lookup({"C": "1.0", "gamma": "0.01", "subsample": "0.5"})
    assert match["result"] == 0.82
    assert match["metadata"]["dataset_row_id"] == "hpo_000001"

    state = create_initial_state(problem, Settings())
    assert "machine learning hyperparameter optimization" in state["messages"][0].content


def test_hpo_domain_prompts_do_not_use_chemistry_persona(tmp_path: Path) -> None:
    problem = load_problem_file(_write_hpo_fixture(tmp_path))
    context = {
        "application_domain": "hpo",
        "domain_profile": "ml_hyperparameter_optimization",
        "reaction_type": problem["reaction_type"],
        "target_metric": "test_acc",
        "optimization_direction": "maximize",
    }
    candidates = [
        {
            "id": 1,
            "candidate": {"C": "1.0", "gamma": "0.01", "subsample": "0.5"},
            "predicted_value": 0.81,
            "uncertainty": 0.03,
            "acquisition_value": 0.1,
        },
        {
            "id": 2,
            "candidate": {"C": "10.0", "gamma": "0.1", "subsample": "1.0"},
            "predicted_value": 0.79,
            "uncertainty": 0.08,
            "acquisition_value": 0.09,
        },
    ]
    prompts = [
        build_acquisition_selection_prompt(context, [], [], candidates, total_observations=2),
        build_surrogate_plausibility_prompt(context, [], [], []),
        _build_warm_start_direct_candidate_pool_prompt(
            context={
                "problem_features": problem,
                "proposal_value_guide": [],
                "constraints": problem.get("constraints", []),
                "warm_start_target": 1,
            },
            candidates=[{"id": 1, "candidate": candidates[0]["candidate"]}],
            target=1,
            total_direct_target=1,
            validation_feedback="",
            accepted_records=[],
        ),
        build_prior_writer_prompt(problem, "ml_hyperparameter_optimization")[0],
    ]
    for prompt in prompts:
        lowered = prompt.lower()
        for word in FORBIDDEN_HPO_PROMPT_WORDS:
            assert word.lower() not in lowered
        assert "machine learning" in lowered or "hpo" in lowered
        assert "hyperparameter" in lowered

    joined = "\n".join(prompts).lower()
    assert "model capacity" in joined
    assert "regularization" in joined
    assert "overfitting" in joined
    assert "validation performance" in joined

    assert '"domain_argument": ""' in prompts[0]
    assert "chemistry_argument" not in prompts[0]


def test_hpo_domain_disables_retrieval_and_descriptor_prompts(tmp_path: Path) -> None:
    problem = load_problem_file(_write_hpo_fixture(tmp_path))

    assert build_retrieval_tools(Settings(), problem, object(), lambda *args: ({}, {})) == []
    assert build_descriptor_selection_prompt(problem) == ""
    assert build_deep_ensemble_feature_spec_prompt(problem["variables"], problem) == ""


def test_hpobench_export_aggregates_seed_rows(tmp_path: Path) -> None:
    output_csv = tmp_path / "hpobench_svm_31.csv"
    rows = [
        {
            "C": 1.0,
            "gamma": 0.01,
            "dataset_fraction": 0.5,
            "seed": 1,
            "result": {"info": {"val_scores": {"acc": 0.8}, "test_scores": {"acc": 0.79}, "model_cost": 2.0}},
        },
        {
            "C": 1.0,
            "gamma": 0.01,
            "dataset_fraction": 0.5,
            "seed": 2,
            "result": {"info": {"val_scores": {"acc": 0.84}, "test_scores": {"acc": 0.81}, "model_cost": 4.0}},
        },
    ]

    exported = export_table_to_csv(
        rows,
        feature_columns=["C", "gamma", "dataset_fraction"],
        output_csv=output_csv,
    )

    assert len(exported) == 1
    assert exported[0]["val_acc"] == "0.82"
    assert exported[0]["val_loss"] == "0.18"
    assert exported[0]["test_acc"] == "0.8"
    assert exported[0]["cost"] == "3"
    assert exported[0]["seed_count"] == "2"
    assert output_csv.exists()


def test_hpobench_default_benchmark_mapping_matches_selected_tasks() -> None:
    assert DEFAULT_BENCHMARKS["svm"]["task_id"] == 146212
    assert DEFAULT_BENCHMARKS["svm"]["dataset_name"] == "openml_146212"
    assert DEFAULT_BENCHMARKS["svm"]["feature_columns"] == ["C", "gamma", "subsample"]

    assert DEFAULT_BENCHMARKS["rf"]["task_id"] == 146606
    assert DEFAULT_BENCHMARKS["rf"]["dataset_name"] == "openml_146606"
    assert DEFAULT_BENCHMARKS["rf"]["feature_columns"] == [
        "max_depth",
        "max_features",
        "min_samples_leaf",
        "min_samples_split",
        "n_estimators",
    ]

    assert DEFAULT_BENCHMARKS["xgb"]["task_id"] == 146606
    assert DEFAULT_BENCHMARKS["xgb"]["dataset_name"] == "openml_146606"
    assert DEFAULT_BENCHMARKS["xgb"]["feature_columns"] == [
        "colsample_bytree",
        "eta",
        "max_depth",
        "reg_lambda",
        "n_estimators",
    ]

    assert DEFAULT_BENCHMARKS["nn"]["task_id"] == 168912
    assert DEFAULT_BENCHMARKS["nn"]["dataset_name"] == "openml_168912"
    assert DEFAULT_BENCHMARKS["nn"]["feature_columns"] == [
        "alpha",
        "batch_size",
        "depth",
        "learning_rate_init",
        "width",
        "iter",
    ]
