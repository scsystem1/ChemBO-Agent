"""
Regression checks for the DAR SMILES-backed search-space chain.
"""
from __future__ import annotations

from core.dataset_oracle import DatasetOracle
from core.problem_loader import load_problem_file
from knowledge.reaction_kb import get_hard_constraints
from pools.component_pools import FingerprintConcatEncoder, detect_runtime_capabilities


def test_dar_smiles_maps_propagate_to_encoder():
    problem = load_problem_file("examples/dar_problem.yaml")
    oracle = DatasetOracle.from_problem_spec(problem)
    assert oracle is not None

    smiles_variables = [variable for variable in problem["variables"] if variable["name"].endswith("_SMILES")]
    assert len(smiles_variables) == 3
    for variable in smiles_variables:
        smiles_map = variable.get("smiles_map", {})
        assert smiles_map
        for entry in variable["domain"]:
            assert smiles_map[str(entry)] == str(entry)

    encoder = FingerprintConcatEncoder(problem["variables"])
    candidate = {
        "base_SMILES": oracle.domain_values["base_SMILES"][0],
        "ligand_SMILES": oracle.domain_values["ligand_SMILES"][0],
        "solvent_SMILES": oracle.domain_values["solvent_SMILES"][0],
        "concentration": oracle.domain_values["concentration"][0],
        "temperature": oracle.domain_values["temperature"][0],
    }
    encoded = encoder.encode(candidate)
    decoded = encoder.decode(encoded)

    assert oracle.candidate_exists(candidate)
    assert encoded.shape[0] == encoder.dim
    for key, value in candidate.items():
        assert str(decoded[key]) == str(value)

    runtime = detect_runtime_capabilities()
    spec_types = {spec["name"]: spec["type"] for spec in encoder.specs}
    if runtime["rdkit"]:
        for variable in smiles_variables:
            assert spec_types[variable["name"]] == "fingerprint"
    else:
        assert any("fallback" in note.lower() or "fingerprints unavailable" in note.lower() for note in encoder.metadata.get("notes", []))


def test_dar_constraints_use_exact_smiles_and_alias_matching():
    checks = {item["name"]: item["check"] for item in get_hard_constraints("DAR")}

    assert checks["dmac_temp_limit"]({"solvent_SMILES": "CC(N(C)C)=O", "temperature": "170"}) is False
    assert checks["dmac_temp_limit"]({"solvent_SMILES": "cc(n(c)c)=o", "temperature": "170"}) is True
    assert checks["dmac_temp_limit"]({"solvent": "DMAc", "temperature": "170"}) is False

    assert checks["toluene_min_temp"]({"solvent_SMILES": "CC1=CC=C(C)C=C1", "temperature": "90"}) is False
    assert checks["toluene_min_temp"]({"solvent": "toluene", "temperature": "90"}) is False
