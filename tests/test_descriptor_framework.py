from __future__ import annotations

from pathlib import Path

import json
import numpy as np
import pytest

from core.campaign_runner import _iteration_config_csv_artifact
from core.problem_loader import load_problem_file
from embeddings.descriptors.formula_descriptors import element_descriptor, formula_descriptor
from embeddings.descriptors.rdkit_2d import calc_rdkit_2d, calc_smarts_counts, mol_from_smiles
from embeddings.descriptors.registry import DescriptorRegistry, build_descriptor_feature_spec
from embeddings.descriptors.resolver import EntityResolver
from embeddings.descriptors.audit_prompt import build_descriptor_audit_prompt
from embeddings.descriptors.selector_prompt import build_descriptor_selection_prompt
from embeddings.descriptors.validation import validate_descriptor_name, validate_selected_descriptors
from embeddings.descriptors.yaml_expander import categorical_descriptor_variables, dataset_key_from_problem, expand_problem_descriptors
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


def test_formula_and_element_descriptors_are_finite_for_ocm_entities() -> None:
    assert formula_descriptor("SiO2", "oxygen_to_metal_ratio") == pytest.approx(2.0)
    assert np.isfinite(formula_descriptor("SiC", "oxygen_to_metal_ratio"))
    assert np.isfinite(formula_descriptor("BN", "oxygen_to_metal_ratio"))
    assert element_descriptor("La", "first_ionization_energy_eV") is not None
    assert element_descriptor("La", "oxide_band_gap_eV") is not None


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
    meoh = resolver.resolve(dataset="suzuki", variable="Solvent_1_Short_Hand", raw_value="MeOH", entity_kind="solvent")
    meoh_water = resolver.resolve(
        dataset="suzuki",
        variable="Solvent_1_Short_Hand",
        raw_value="MeOH/H2O_V2 9:1",
        entity_kind="solvent",
    )
    assert meoh.entity_key != meoh_water.entity_key

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
    assert "Choose 1 to 3 descriptors" in prompt
    assert "Do not pad to 3 descriptors" in prompt
    assert "EXACTLY 3" not in prompt
    assert "3-5 descriptors" not in prompt
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
        optimization_summary={
            "n_observations": 3,
            "optimization_direction": "maximize",
            "observations_raw": [
                {"candidate": {"base_SMILES": "A"}, "result": 10.0},
                {"candidate": {"base_SMILES": "B"}, "result": 30.0},
                {"candidate": {"base_SMILES": "C"}, "result": 20.0},
            ],
        },
        model_diagnostics={"ranked_models": []},
    )
    assert '"decision": "keep_current"' in prompt
    assert "propose_challenger" in prompt
    assert "complete challenger schema" in prompt
    assert "representative_observations" in prompt
    assert "observations_raw" not in prompt
    assert "Choose 1 to 3 descriptors" in prompt
    assert "reasonable mechanistic or model-diagnostic basis" in prompt
    assert "Prefer keep_current unless" not in prompt
    assert "at most 1 descriptor change" not in prompt
    assert "EXACTLY 3" not in prompt
    for forbidden in ["scale_types", "resolver", "requires_source"]:
        assert forbidden not in prompt


def test_selected_descriptor_validation_accepts_one_to_three_descriptors() -> None:
    available = {"rdkit_2d": ["MolWt", "MolLogP", "TPSA", "BertzCT"]}
    with pytest.raises(ValueError, match="Between 1 and 3"):
        validate_selected_descriptors(
            selected_descriptors=[],
            available_descriptors=available,
            scale_types={},
            allow_semichemical_ordinal=False,
        )
    with pytest.raises(ValueError, match="Between 1 and 3"):
        validate_selected_descriptors(
            selected_descriptors=[
                ("rdkit_2d", "MolWt"),
                ("rdkit_2d", "MolLogP"),
                ("rdkit_2d", "TPSA"),
                ("rdkit_2d", "BertzCT"),
            ],
            available_descriptors=available,
            scale_types={},
            allow_semichemical_ordinal=False,
        )
    validate_selected_descriptors(
        selected_descriptors=[("rdkit_2d", "MolWt")],
        available_descriptors=available,
        scale_types={},
        allow_semichemical_ordinal=False,
    )
    validate_selected_descriptors(
        selected_descriptors=[("rdkit_2d", "MolWt"), ("rdkit_2d", "MolLogP")],
        available_descriptors=available,
        scale_types={},
        allow_semichemical_ordinal=False,
    )
    validate_selected_descriptors(
        selected_descriptors=[("rdkit_2d", "MolWt"), ("rdkit_2d", "MolLogP"), ("rdkit_2d", "TPSA")],
        available_descriptors=available,
        scale_types={},
        allow_semichemical_ordinal=False,
    )


def test_dar_dataset_mapped_descriptor_feature_spec_generates_feature_map() -> None:
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


def test_removed_source_locked_descriptor_is_not_selectable() -> None:
    spec = load_problem_file(ROOT / "examples/suzuki_problem.yaml")
    with pytest.raises(ValueError, match="not declared as available"):
        build_descriptor_feature_spec(
            problem_spec=spec,
            selection_payload={
                "selected_descriptors_by_variable": {
                    "Reactant_2_Name": [
                        {"pool": "rdkit_2d", "name": "MolWt"},
                        {"pool": "rdkit_2d", "name": "TPSA"},
                        {"pool": "cross_coupling_substrate_physchem", "name": "oxidative_addition_reactivity_score"},
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
                    "max_selected_descriptors": 3,
                    "available_descriptors": {
                        "ligand_physchem": ["TEP_cm_minus_1", "percent_Vbur", "tolman_cone_angle_deg"]
                    },
                    "validation": {"coverage": "strict_non_absent", "collision": "fail_on_selected_descriptor_collision"},
                },
            }
        ],
    }
    feature_spec = build_descriptor_feature_spec(
        problem_spec=spec,
        selection_payload={
            "selected_descriptors_by_variable": {
                "Ligand_Short_Hand": [
                    {"pool": "ligand_physchem", "name": "TEP_cm_minus_1"},
                    {"pool": "ligand_physchem", "name": "percent_Vbur"},
                    {"pool": "ligand_physchem", "name": "tolman_cone_angle_deg"},
                ]
            }
        },
    )
    assert feature_spec["variable_features"]["Ligand_Short_Hand"]["feature_map"]["None"] == [0.0, 0.0, 0.0]


def test_ocm_support_formula_only_collision_is_reported_as_warning() -> None:
    spec = load_problem_file(ROOT / "examples/ocm_problem.yaml")
    feature_spec = build_descriptor_feature_spec(
        problem_spec=spec,
        selection_payload={
            "selected_descriptors_by_variable": {
                "Support": [
                    {"pool": "support_material_physchem", "name": "formula_weight_g_mol_formula_unit"},
                    {"pool": "support_material_physchem", "name": "oxygen_atomic_fraction"},
                    {"pool": "support_material_physchem", "name": "metal_atomic_fraction"},
                ]
            }
        },
    )
    warnings = feature_spec["descriptor_diagnostics"]["descriptor_collision_report"]["Support"]["warnings"]
    assert any({"SiC", "SiCnf"}.issubset(set(group)) for group in warnings)


def test_ocm_support_pzc_breaks_sic_sicnf_collision() -> None:
    spec = load_problem_file(ROOT / "examples/ocm_problem.yaml")
    feature_spec = build_descriptor_feature_spec(
        problem_spec=spec,
        selection_payload={
            "selected_descriptors_by_variable": {
                "Support": [
                    {"pool": "support_material_physchem", "name": "formula_weight_g_mol_formula_unit"},
                    {"pool": "support_material_physchem", "name": "oxygen_atomic_fraction"},
                    {"pool": "support_material_physchem", "name": "point_of_zero_charge_pH"},
                ]
            }
        },
    )
    feature_map = feature_spec["variable_features"]["Support"]["feature_map"]
    assert feature_map["SiC"] != feature_map["SiCnf"]


def test_yaml_exposed_descriptors_have_strict_present_coverage() -> None:
    pytest.importorskip("rdkit")
    registry = DescriptorRegistry()
    for path in ["examples/dar_problem.yaml", "examples/suzuki_problem.yaml", "examples/ocm_problem.yaml"]:
        spec = load_problem_file(ROOT / path)
        dataset = dataset_key_from_problem(spec)
        for variable in categorical_descriptor_variables(spec):
            available = variable["descriptor"]["available_descriptors"]
            for pool, names in available.items():
                for name in names:
                    matrix = registry.build_matrix(
                        dataset=dataset,
                        variable=variable,
                        selected_descriptors=[(pool, name)],
                    )
                    present = np.asarray(matrix.present_mask, dtype=bool)
                    assert np.all(matrix.known_mask[present]), (path, variable["name"], pool, name)
                    assert np.all(np.isfinite(matrix.values[present])), (path, variable["name"], pool, name)
                    for entity in matrix.metadata["resolved_entities"]:
                        if not entity["allow_absent"]:
                            assert entity["curation_status"] == "ready", (path, variable["name"], entity)


def test_scaled_feature_map_is_finite_minmax_without_default_present_mask() -> None:
    pytest.importorskip("rdkit")
    registry = DescriptorRegistry()
    spec = load_problem_file(ROOT / "examples/dar_problem.yaml")
    variable = next(var for var in categorical_descriptor_variables(spec) if var["name"] == "solvent_SMILES")
    matrix = registry.build_matrix(
        dataset=dataset_key_from_problem(spec),
        variable=variable,
        selected_descriptors=[
            ("solvent_physchem", "dielectric_constant_25C"),
            ("solvent_physchem", "boiling_point_C"),
        ],
    )
    feature_map = registry.scaled_feature_map(matrix)
    assert all(len(vector) == 2 for vector in feature_map.values())
    for vector in feature_map.values():
        assert all(np.isfinite(vector))
        assert all(0.0 <= value <= 1.0 for value in vector)


def test_suzuki_meoh_water_mixture_descriptor_distinguishes_meoh_without_failing_thf_v2_warning() -> None:
    spec = load_problem_file(ROOT / "examples/suzuki_problem.yaml")
    feature_spec = build_descriptor_feature_spec(
        problem_spec=spec,
        selection_payload={
            "selected_descriptors_by_variable": {
                "Solvent_1_Short_Hand": [
                    {"pool": "solvent_physchem", "name": "water_volume_fraction"},
                    {"pool": "solvent_physchem", "name": "dielectric_constant_25C"},
                ]
            }
        },
    )
    feature_map = feature_spec["variable_features"]["Solvent_1_Short_Hand"]["feature_map"]
    assert feature_map["MeOH"] != feature_map["MeOH/H2O_V2 9:1"]
    warnings = feature_spec["descriptor_diagnostics"]["descriptor_collision_report"]["Solvent_1_Short_Hand"]["warnings"]
    assert any({"THF", "THF_V2"}.issubset(set(group)) for group in warnings)


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


def test_iteration_config_records_include_descriptor_schema() -> None:
    active_schema = {
        "selected_descriptors_by_variable": {
            "ligand": [
                {"pool": "rdkit_2d", "name": "MolWt"},
                {"pool": "rdkit_2d", "name": "MolLogP"},
                {"pool": "rdkit_2d", "name": "TPSA"},
            ]
        },
        "rationales": {"ligand": "test schema"},
        "warnings": [],
    }
    artifact = _iteration_config_csv_artifact(
        {
            "observations": [
                {
                    "iteration": 1,
                    "candidate": {"ligand": "A"},
                    "result": 42.0,
                    "metadata": {
                        "proposal_strategy": "autobo_runtime",
                        "resolved_components": {
                            "surrogate_model": "deep_ensemble",
                            "kernel_config": {"key": "none"},
                            "acquisition_function": "ei",
                        },
                        "active_descriptor_schema_id": "schema_0",
                        "active_descriptor_schema": active_schema,
                        "selected_descriptors_by_variable": active_schema["selected_descriptors_by_variable"],
                        "descriptor_schema_switch_info": {"switched": False},
                    },
                }
            ],
            "effective_config": {},
            "bo_config": {},
        }
    )
    row = artifact["rows"][0]
    assert "active_descriptor_schema_id" in artifact["fieldnames"]
    assert row["active_descriptor_schema_id"] == "schema_0"
    assert json.loads(row["selected_descriptors_by_variable"]) == active_schema["selected_descriptors_by_variable"]
    assert json.loads(row["active_descriptor_schema"]) == active_schema
