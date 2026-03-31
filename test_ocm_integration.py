"""
Regression coverage for the dataset-backed OCM benchmark integration.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path

from config.settings import Settings
from core.dataset_oracle import DatasetOracle
from core.problem_loader import load_problem_file
from knowledge.reaction_kb import get_hard_constraints, get_reaction_knowledge, get_structured_priors

try:
    import core.graph as graph_module
    from core.campaign_runner import run_campaign
    from core.state import create_initial_state
    from test_mock import MockChemBOLLM

    GRAPH_TEST_DEPS_AVAILABLE = True
except ModuleNotFoundError as exc:  # pragma: no cover - local env may lack optional deps
    graph_module = None
    run_campaign = None
    create_initial_state = None
    MockChemBOLLM = None
    GRAPH_TEST_DEPS_AVAILABLE = False
    IMPORT_ERROR = exc


ROOT = Path(__file__).resolve().parent
OCM_DATASET_PATH = ROOT / "data" / "OCM.csv"
OCM_EXAMPLE_PATH = ROOT / "examples" / "ocm_problem.yaml"
OCM_CONFIG_PATH = ROOT / "dashscope_kimi_ocm.yaml"
OCM_FEATURE_COLUMNS = ["M1", "M2", "M3", "Support", "Temp", "Ar_flow", "CH4_flow", "O2_flow", "CT"]


def test_ocm_problem_loader_structure_and_config():
    problem = load_problem_file(OCM_EXAMPLE_PATH)

    assert isinstance(problem, dict)
    assert problem["reaction_type"] == "OCM"
    assert problem["target_metric"] == "C2+ yield"
    assert problem["dataset"]["csv_path"] == str(OCM_DATASET_PATH.resolve())
    assert [variable["name"] for variable in problem["variables"]] == OCM_FEATURE_COLUMNS
    assert not any(name.endswith("_mol") for name in [variable["name"] for variable in problem["variables"]])

    settings = Settings.from_yaml(str(OCM_CONFIG_PATH))
    assert settings.human_input_mode == "dataset_auto"
    assert settings.ablation_pure_reasoning is True
    assert settings.max_bo_iterations == 30


def test_ocm_knowledge_base_constraints_and_priors():
    problem = load_problem_file(OCM_EXAMPLE_PATH)
    kb = get_reaction_knowledge("OCM")
    constraints = get_hard_constraints("OCM")
    priors = get_structured_priors("OCM", problem)

    assert kb is not None
    assert kb["full_name"] == "Oxidative Coupling of Methane"
    assert len(constraints) == 3
    assert set(priors["warm_start_bias"]) >= {"M1", "M2", "M3", "Support", "Temp", "CT"}

    checks = {item["name"]: item["check"] for item in constraints}
    valid_candidate = {
        "M1": "Mn",
        "M2": "Na",
        "M3": "W",
        "Support": "SiO2",
        "Temp": "800",
        "Ar_flow": "3.0",
        "CH4_flow": "14.6",
        "O2_flow": "2.4",
        "CT": "0.38",
    }
    assert checks["ocm_ch4_o2_ratio_lower_bound"](valid_candidate) is True
    assert checks["ocm_ch4_o2_ratio_upper_bound"](valid_candidate) is True
    assert checks["ocm_all_metals_vacant"](valid_candidate) is True
    assert checks["ocm_ch4_o2_ratio_lower_bound"]({**valid_candidate, "CH4_flow": "2.0", "O2_flow": "2.4"}) is False
    assert checks["ocm_ch4_o2_ratio_upper_bound"]({**valid_candidate, "CH4_flow": "14.6", "O2_flow": "0.4"}) is False
    assert checks["ocm_all_metals_vacant"]({**valid_candidate, "M1": "n.a.", "M2": "n.a.", "M3": "n.a."}) is False


def test_ocm_domains_match_csv_and_cover_sparse_dataset():
    if not OCM_DATASET_PATH.exists():
        print(f"Skipping OCM dataset domain test: missing {OCM_DATASET_PATH}")
        return

    problem = load_problem_file(OCM_EXAMPLE_PATH)
    rows = _read_csv_rows(OCM_DATASET_PATH)
    assert rows

    unique_values = {column: {row[column] for row in rows} for column in OCM_FEATURE_COLUMNS}
    for variable in problem["variables"]:
        expected_values = unique_values[variable["name"]]
        assert set(variable["domain"]) == expected_values
        assert variable["domain"] == _stable_domain_order(expected_values)

    cartesian_size = math.prod(len(unique_values[column]) for column in OCM_FEATURE_COLUMNS)
    oracle = DatasetOracle.from_problem_spec(problem)
    assert oracle is not None
    assert oracle.size == len(rows)
    assert oracle.size < cartesian_size


def test_ocm_dataset_retains_mol_columns_as_metadata():
    if not OCM_DATASET_PATH.exists():
        print(f"Skipping OCM mol metadata test: missing {OCM_DATASET_PATH}")
        return

    first_row = _read_csv_rows(OCM_DATASET_PATH)[0]
    assert {"M1_mol", "M2_mol", "M3_mol"}.issubset(first_row)

    problem = load_problem_file(OCM_EXAMPLE_PATH)
    oracle = DatasetOracle.from_problem_spec(problem)
    assert oracle is not None

    candidate = {column: first_row[column] for column in OCM_FEATURE_COLUMNS}
    matched = oracle.lookup(candidate)
    assert matched["row"]["M1_mol"] == first_row["M1_mol"]
    assert matched["row"]["M2_mol"] == first_row["M2_mol"]
    assert matched["row"]["M3_mol"] == first_row["M3_mol"]


def test_ocm_oracle_round_trips_first_dataset_row():
    if not OCM_DATASET_PATH.exists():
        print(f"Skipping OCM oracle test: missing {OCM_DATASET_PATH}")
        return

    problem = load_problem_file(OCM_EXAMPLE_PATH)
    oracle = DatasetOracle.from_problem_spec(problem)
    assert oracle is not None

    first_row = _read_csv_rows(OCM_DATASET_PATH)[0]
    candidate = {column: first_row[column] for column in OCM_FEATURE_COLUMNS}
    matched = oracle.lookup(candidate)

    assert oracle.candidate_exists(candidate)
    assert matched["candidate"] == candidate
    assert abs(matched["result"] - float(first_row["Performance"])) < 1e-9
    assert matched["metadata"]["dataset_target_column"] == "Performance"


def test_ocm_dataset_auto_campaign_smoke():
    if not OCM_DATASET_PATH.exists():
        print(f"Skipping OCM smoke test: missing {OCM_DATASET_PATH}")
        return
    if not GRAPH_TEST_DEPS_AVAILABLE:
        print(f"Skipping OCM smoke test: {IMPORT_ERROR}")
        return

    original_factory = graph_module._create_llm
    graph_module._create_llm = lambda settings: MockChemBOLLM()
    try:
        problem = load_problem_file(OCM_EXAMPLE_PATH)
        problem["budget"] = 6
        settings = Settings.from_yaml(str(OCM_CONFIG_PATH))
        settings.max_bo_iterations = 6
        graph = graph_module.build_chembo_graph(settings)
        initial_state = create_initial_state(problem, settings)
        state = run_campaign(graph, initial_state, settings, thread_id="ocm-smoke", printer=None)

        oracle = DatasetOracle.from_problem_spec(state["problem_spec"])
        assert oracle is not None
        assert len(state["observations"]) == 6
        assert state["effective_config"]["proposal_strategy"] == "pure_reasoning"
        assert state["last_tool_payload"]["strategy"] == "pure_reasoning_shortlist"
        for observation in state["observations"]:
            matched = oracle.lookup(observation["candidate"])
            assert abs(observation["result"] - matched["result"]) < 1e-9
            assert observation["metadata"]["notes"] == "dataset_auto"
    finally:
        graph_module._create_llm = original_factory


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _stable_domain_order(values: set[str]) -> list[str]:
    entries = [str(value) for value in values]
    if all(_is_numeric_token(value) for value in entries):
        return [value for value, _ in sorted(((value, float(value)) for value in entries), key=lambda item: item[1])]
    return sorted(entries)


def _is_numeric_token(value: str) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True
