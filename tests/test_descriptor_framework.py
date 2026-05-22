from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from core.problem_loader import load_problem_file
from embeddings.descriptors.rdkit_2d import calc_rdkit_2d, calc_smarts_counts, mol_from_smiles
from embeddings.descriptors.registry import DescriptorRegistry, build_descriptor_feature_spec
from embeddings.descriptors.resolver import EntityResolver
from embeddings.descriptors.audit_prompt import build_descriptor_audit_prompt
from embeddings.descriptors.selector_prompt import build_descriptor_selection_prompt
from embeddings.descriptors.validation import validate_descriptor_name
from embeddings.descriptors.yaml_expander import expand_problem_descriptors
from pools.component_pools import DeepEnsembleSurrogate


ROOT = Path(__file__).resolve().parents[1]


def test_descriptor_name_validation_semantic_numeric_boundary() -> None:
    with pytest.raises(ValueError):
        validate_descriptor_name("material_family", "continuous")
    with pytest.raises(ValueError):
        validate_descriptor_name("is_nanofiber", "binary_count")

    validate_descriptor_name("denticity_count", "integer_count")
    validate_descriptor_name("d_electron_count_common_oxide", "integer_count")
    validate_descriptor_name("boronic_acid_group_count", "binary_count")
    validate_descriptor_name("oxidative_addition_reactivity_score", "ordinal_semichemical")


def test_rdkit_descriptors_and_smarts_counts() -> None:
    pytest.importorskip("rdkit")
    values = calc_rdkit_2d("P(c1ccccc1)(c1ccccc1)c1ccccc1", ["MolWt", "MolLogP", "TPSA"])
    assert set(values) == {"MolWt", "MolLogP", "TPSA"}
    assert all(np.isfinite(list(values.values())))

    counts = calc_smarts_counts("Brc1ccccc1", ["aryl_halide_count", "aryl_bromide_count"])
    assert counts["aryl_halide_count"] == pytest.approx(1.0)
    assert counts["aryl_bromide_count"] == pytest.approx(1.0)

    with pytest.raises(ValueError):
        mol_from_smiles("not a smiles")


def test_resolver_maps_absent_aliases_and_critical_supports() -> None:
    resolver = EntityResolver()
    ligand_none = resolver.resolve(
        dataset="suzuki",
        variable="Ligand_Short_Hand",
        raw_value="None",
        entity_kind="ligand",
        allow_absent_values={"None"},
    )
    assert ligand_none.allow_absent is True

    thf = resolver.resolve(dataset="suzuki", variable="Solvent_1_Short_Hand", raw_value="THF", entity_kind="solvent")
    thf_v2 = resolver.resolve(dataset="suzuki", variable="Solvent_1_Short_Hand", raw_value="THF_V2", entity_kind="solvent")
    assert thf.entity_key == thf_v2.entity_key

    sic = resolver.resolve(dataset="ocm", variable="Support", raw_value="SiC", entity_kind="support")
    sicnf = resolver.resolve(dataset="ocm", variable="Support", raw_value="SiCnf", entity_kind="support")
    assert sic.entity_key != sicnf.entity_key


def test_yaml_expansion_has_descriptor_blocks_without_forbidden_names() -> None:
    for path in ["examples/dar_problem.yaml", "examples/suzuki_problem.yaml", "examples/ocm_problem.yaml"]:
        spec = load_problem_file(ROOT / path)
        expanded = expand_problem_descriptors(spec)
        categorical = [var for var in spec["variables"] if var.get("type", "categorical") != "continuous"]
        assert len(expanded["variables"]) == len(categorical)
        rendered = repr(expanded)
        assert "material_family" not in rendered
        assert "is_nanofiber" not in rendered
        assert "onehot_" not in rendered


def test_descriptor_selection_prompt_is_compact_whitelist() -> None:
    spec = load_problem_file(ROOT / "examples/dar_problem.yaml")
    prompt = build_descriptor_selection_prompt(spec)
    assert "available_descriptors" in prompt
    assert "rdkit_2d.MolWt" in prompt
    assert "meaning" in prompt
    for forbidden in ["scale_types", "validation", "resolver", "requires_source"]:
        assert forbidden not in prompt


def test_descriptor_audit_prompt_supports_keep_current_and_challenger_shape() -> None:
    spec = load_problem_file(ROOT / "examples/dar_problem.yaml")
    active_schema = {
        "selected_descriptors_by_variable": {
            "base_SMILES": [
                {"pool": "rdkit_2d", "name": "MolWt"},
                {"pool": "rdkit_2d", "name": "TPSA"},
                {"pool": "rdkit_2d", "name": "FormalCharge"},
            ]
        },
        "rationales": {"base_SMILES": "baseline"},
        "warnings": [],
    }
    prompt = build_descriptor_audit_prompt(
        problem_spec=spec,
        active_schema=active_schema,
        descriptor_diagnostics={"status": "ok"},
        optimization_summary={"n_observations": 20},
        model_diagnostics={"ranked_models": []},
    )
    assert '"decision": "keep_current"' in prompt
    assert "propose_challenger" in prompt
    assert "complete challenger schema" in prompt
    for forbidden in ["scale_types", "resolver", "requires_source"]:
        assert forbidden not in prompt


def test_dar_smiles_descriptor_feature_spec_generates_feature_map() -> None:
    pytest.importorskip("rdkit")
    spec = load_problem_file(ROOT / "examples/dar_problem.yaml")
    feature_spec = build_descriptor_feature_spec(
        problem_spec=spec,
        selection_payload={
            "selected_descriptors_by_variable": {
                "base_SMILES": [
                    {"pool": "rdkit_2d", "name": "MolWt"},
                    {"pool": "rdkit_2d", "name": "TPSA"},
                    {"pool": "rdkit_2d", "name": "FormalCharge"},
                ]
            }
        },
    )
    feature_map = feature_spec["variable_features"]["base_SMILES"]["feature_map"]
    assert len(feature_map) == 4
    assert all(len(vector) == 3 for vector in feature_map.values())


def test_missing_source_locked_descriptor_fails_coverage_for_present_entity() -> None:
    spec = load_problem_file(ROOT / "examples/suzuki_problem.yaml")
    with pytest.raises(ValueError, match="Missing selected descriptor"):
        build_descriptor_feature_spec(
            problem_spec=spec,
            selection_payload={
                "selected_descriptors_by_variable": {
                    "Reactant_2_Name": [
                        {"pool": "cross_coupling_substrate_physchem", "name": "oxidative_addition_reactivity_score"}
                    ]
                }
            },
        )


def test_absent_entity_does_not_fail_missing_descriptor() -> None:
    spec = {
        "reaction_type": "Suzuki",
        "variables": [
            {
                "name": "Ligand_Short_Hand",
                "role": "ligand",
                "type": "categorical",
                "domain": ["None"],
                "descriptor": {
                    "enabled": True,
                    "entity_kind": "ligand",
                    "resolver": "dataset_value_map",
                    "allow_absent_values": ["None"],
                    "max_selected_descriptors": 1,
                    "available_descriptors": {"ligand_physchem": ["TEP_cm_minus_1"]},
                    "validation": {"coverage": "strict_non_absent", "collision": "fail_on_selected_descriptor_collision"},
                },
            }
        ],
    }
    feature_spec = build_descriptor_feature_spec(
        problem_spec=spec,
        selection_payload={
            "selected_descriptors_by_variable": {
                "Ligand_Short_Hand": [{"pool": "ligand_physchem", "name": "TEP_cm_minus_1"}]
            }
        },
    )
    assert feature_spec["variable_features"]["Ligand_Short_Hand"]["feature_map"]["None"] == [0.0, 0.0]


def test_ocm_support_critical_collision_fails_for_sic_vs_sicnf() -> None:
    spec = load_problem_file(ROOT / "examples/ocm_problem.yaml")
    with pytest.raises(ValueError, match="Critical descriptor collision"):
        build_descriptor_feature_spec(
            problem_spec=spec,
            selection_payload={
                "selected_descriptors_by_variable": {
                    "Support": [
                        {"pool": "support_material_physchem", "name": "formula_weight_g_mol_formula_unit"},
                        {"pool": "support_material_physchem", "name": "oxygen_atomic_fraction"},
                    ]
                }
            },
        )


def test_deep_ensemble_prefers_descriptor_v2_feature_map() -> None:
    search_space = [
        {"name": "ligand", "type": "categorical", "domain": ["A", "B"]},
        {"name": "temp", "type": "continuous", "domain": [0.0, 100.0]},
    ]
    surrogate = DeepEnsembleSurrogate(
        search_space=search_space,
        params={"n_models": 1, "n_epochs": 1},
        feature_spec={
            "variable_features": {
                "ligand": {
                    "source": "descriptor_v2",
                    "descriptor_names": ["rdkit_2d.MolWt"],
                    "feature_map": {"A": [0.25, 1.0], "B": [-0.25, 1.0]},
                }
            }
        },
    )
    encoded = surrogate._encode_candidates([{"ligand": "A", "temp": 50.0}, {"ligand": "B", "temp": 25.0}])
    assert encoded.shape == (2, 3)
    assert encoded[0, :2].tolist() == pytest.approx([0.25, 1.0])
