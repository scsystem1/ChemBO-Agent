"""
Regression checks for the dataset-backed DAR benchmark integration.
"""
from __future__ import annotations

import json
from pathlib import Path

from core.dataset_oracle import DatasetOracle
from core.problem_loader import load_problem_file, resolve_campaign_budget
from config.settings import Settings
from pools.component_pools import candidate_to_key


ROOT = Path(__file__).resolve().parent
EXAMPLE_PATH = ROOT / "examples" / "dar_problem.yaml"


def test_problem_loader_and_path_resolution():
    problem = load_problem_file(EXAMPLE_PATH)
    assert isinstance(problem, dict)
    assert problem["reaction_type"] == "DAR"
    assert problem["target_metric"] == "yield"
    assert problem["dataset"]["csv_path"] == str((ROOT / "data" / "DAR.csv").resolve())
    assert len(problem["variables"]) == 5
    assert problem["variables"][3]["type"] == "categorical"
    assert problem["variables"][4]["type"] == "categorical"
    assert resolve_campaign_budget(problem, Settings(max_bo_iterations=4)) == 30


def test_dataset_domains_and_uniqueness():
    problem = load_problem_file(EXAMPLE_PATH)
    oracle = DatasetOracle.from_problem_spec(problem)
    assert oracle is not None
    assert oracle.size == 1728

    expected_sizes = {
        "base_SMILES": 4,
        "ligand_SMILES": 12,
        "solvent_SMILES": 4,
        "concentration": 3,
        "temperature": 3,
    }
    for variable in problem["variables"]:
        domain = tuple(str(item) for item in variable["domain"])
        observed = oracle.domain_values[variable["name"]]
        assert len(observed) == expected_sizes[variable["name"]]
        assert set(domain) == set(observed)

    seen = set()
    for base in oracle.domain_values["base_SMILES"]:
        for ligand in oracle.domain_values["ligand_SMILES"]:
            for solvent in oracle.domain_values["solvent_SMILES"]:
                for concentration in oracle.domain_values["concentration"]:
                    for temperature in oracle.domain_values["temperature"]:
                        candidate = {
                            "base_SMILES": base,
                            "ligand_SMILES": ligand,
                            "solvent_SMILES": solvent,
                            "concentration": concentration,
                            "temperature": temperature,
                        }
                        key = candidate_to_key(candidate)
                        assert key not in seen
                        seen.add(key)
    assert len(seen) == 1728


def test_oracle_lookup_matches_known_row():
    problem = load_problem_file(EXAMPLE_PATH)
    oracle = DatasetOracle.from_problem_spec(problem)
    assert oracle is not None

    candidate = {
        "base_SMILES": "O=C([O-])C.[K+]",
        "ligand_SMILES": "CC(C)C1=CC(C(C)C)=C(C(C(C)C)=C1)C2=C(P(C3CCCCC3)C4CCCCC4)C(OC)=CC=C2OC",
        "solvent_SMILES": "CC(N(C)C)=O",
        "concentration": "0.1",
        "temperature": "105",
    }
    match = oracle.lookup(candidate)
    assert abs(match["result"] - 5.47) < 1e-9
    assert match["metadata"]["dataset_row_id"] == "0"


def test_bo_runner_warm_start_fallback_resolves_kernel():
    try:
        from tools.chembo_tools import bo_runner
    except ModuleNotFoundError as exc:
        print(f"Skipping warm-start fallback regression test: {exc}")
        return

    problem = load_problem_file(EXAMPLE_PATH)
    oracle = DatasetOracle.from_problem_spec(problem)
    assert oracle is not None

    observed_candidate = {
        "base_SMILES": "O=C([O-])C.[K+]",
        "ligand_SMILES": "CC(C)C1=CC(C(C)C)=C(C(C(C)C)=C1)C2=C(P(C3CCCCC3)C4CCCCC4)C(OC)=CC=C2OC",
        "solvent_SMILES": "CC(N(C)C)=O",
        "concentration": "0.1",
        "temperature": "105",
    }
    observed_result = oracle.lookup(observed_candidate)["result"]

    payload = json.loads(
        bo_runner.invoke(
            {
                "embedding_method": "one_hot",
                "embedding_params": "{}",
                "surrogate_model": "gp",
                "surrogate_params": "{}",
                "acquisition_function": "log_ei",
                "af_params": '{"candidate_pool_size": 256, "initial_doe_size": 5}',
                "search_space": json.dumps(problem["variables"]),
                "observations": json.dumps([{"candidate": observed_candidate, "result": observed_result}]),
                "batch_size": 1,
                "kernel_config": '{"key": "matern52", "params": {}}',
                "reaction_type": "DAR",
            }
        )
    )
    assert payload["status"] == "warm_start_fallback"
    assert payload["resolved_components"]["kernel_config"]["key"] == "matern52"
    assert payload["shortlist"]


def test_bo_runner_proposes_only_dataset_points():
    try:
        from tools.chembo_tools import bo_runner
    except ModuleNotFoundError as exc:
        print(f"Skipping BO runner restriction test: {exc}")
        return

    problem = load_problem_file(EXAMPLE_PATH)
    oracle = DatasetOracle.from_problem_spec(problem)
    assert oracle is not None

    observed_candidate = {
        "base_SMILES": "O=C([O-])C.[K+]",
        "ligand_SMILES": "CC(C)C1=CC(C(C)C)=C(C(C(C)C)=C1)C2=C(P(C3CCCCC3)C4CCCCC4)C(OC)=CC=C2OC",
        "solvent_SMILES": "CC(N(C)C)=O",
        "concentration": "0.1",
        "temperature": "105",
    }
    observed_result = oracle.lookup(observed_candidate)["result"]

    payload = json.loads(
        bo_runner.invoke(
            {
                "embedding_method": "one_hot",
                "embedding_params": "{}",
                "surrogate_model": "gp",
                "surrogate_params": "{}",
                "acquisition_function": "log_ei",
                "af_params": '{"candidate_pool_size": 256, "initial_doe_size": 1}',
                "search_space": json.dumps(problem["variables"]),
                "observations": json.dumps([{"candidate": observed_candidate, "result": observed_result}]),
                "batch_size": 1,
                "kernel_config": '{"key": "matern52", "params": {}}',
                "reaction_type": "DAR",
            }
        )
    )
    assert payload["shortlist"]
    candidate = payload["candidates"][0]
    assert oracle.candidate_exists(candidate)
    assert candidate_to_key(candidate) != candidate_to_key(observed_candidate)
    assert payload["resolved_components"]["acquisition_function"] == "log_ei"


def test_end_to_end_dataset_auto_campaign():
    try:
        import core.graph as graph_module
        from config.settings import Settings
        from core.campaign_runner import run_campaign
        from core.state import create_initial_state
        from test_mock import MockChemBOLLM
    except ModuleNotFoundError as exc:
        print(f"Skipping end-to-end dataset-auto test: {exc}")
        return

    original_factory = graph_module._create_llm
    graph_module._create_llm = lambda settings: MockChemBOLLM()
    try:
        problem = load_problem_file(EXAMPLE_PATH)
        problem["budget"] = 4
        settings = Settings(llm_model="mock", batch_size=1, max_bo_iterations=4, human_input_mode="dataset_auto")
        graph = graph_module.build_chembo_graph(settings)
        initial_state = create_initial_state(problem, settings)
        state = run_campaign(graph, initial_state, settings, thread_id="dataset-auto-test", printer=None)

        oracle = DatasetOracle.from_problem_spec(state["problem_spec"])
        assert oracle is not None
        assert len(state["observations"]) >= 1
        for observation in state["observations"]:
            matched = oracle.lookup(observation["candidate"])
            assert abs(observation["result"] - matched["result"]) < 1e-9
            assert observation["metadata"]["notes"] == "dataset_auto"
            assert observation["metadata"]["dataset_row_id"] == matched["metadata"]["dataset_row_id"]
    finally:
        graph_module._create_llm = original_factory


if __name__ == "__main__":
    test_problem_loader_and_path_resolution()
    test_dataset_domains_and_uniqueness()
    test_oracle_lookup_matches_known_row()
    test_bo_runner_warm_start_fallback_resolves_kernel()
    test_bo_runner_proposes_only_dataset_points()
    test_end_to_end_dataset_auto_campaign()
    print("Dataset-backed DAR tests passed.")
