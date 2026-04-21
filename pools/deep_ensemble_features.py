"""LLM-guided physicochemical feature extraction for Deep Ensemble surrogates."""
from __future__ import annotations

import json
from typing import Any

import numpy as np


FALLBACK_DESCRIPTORS = [
    "MolWt",
    "MolLogP",
    "TPSA",
    "NumHDonors",
    "NumHAcceptors",
    "NumRotatableBonds",
]


def build_deep_ensemble_feature_spec_prompt(
    search_space: list[dict[str, Any]],
    problem_spec: dict[str, Any],
) -> str:
    variables = []
    for variable in search_space:
        if variable.get("type", "categorical") == "continuous":
            continue
        smiles_map = variable.get("smiles_map") or {}
        if not smiles_map:
            continue
        labels = []
        for entry in variable.get("domain", []):
            if isinstance(entry, dict):
                labels.append(str(entry.get("label") or entry.get("name") or entry.get("value") or entry))
            else:
                labels.append(str(entry))
        variables.append(
            {
                "name": variable.get("name"),
                "role": variable.get("role", "other"),
                "description": variable.get("description", ""),
                "domain_values": labels[:8],
                "example_smiles": list(smiles_map.values())[:3],
            }
        )

    if not variables:
        return ""

    return f"""You are selecting molecular descriptors for a neural-network surrogate in chemical reaction optimization.

Problem: {problem_spec.get("reaction_type", "chemical optimization")}
Target: {problem_spec.get("target_metric", "yield")}

For each categorical variable below, recommend 5-6 RDKit physicochemical descriptors likely to predict reaction outcome.
Use exact RDKit attribute names from rdkit.Chem.Descriptors or rdkit.Chem.rdMolDescriptors.
Examples: MolWt, MolLogP, TPSA, NumHDonors, NumHAcceptors, NumRotatableBonds, NumAromaticRings, BertzCT, Chi0v, CalcNumRings, CalcTPSA, CalcNumAromaticRings.

Variables:
{json.dumps(variables, ensure_ascii=False, indent=2)}

Return strict JSON:
{{
  "variable_features": {{
    "variable_name": {{
      "descriptor_names": ["MolLogP", "TPSA", "NumHDonors"],
      "reasoning": "one-line rationale"
    }}
  }}
}}"""


def compute_rdkit_features_for_variable(
    variable: dict[str, Any],
    descriptor_names: list[str],
    rdkit_modules: tuple[Any, Any, Any],
) -> dict[str, np.ndarray] | None:
    Chem, Descriptors, rdMolDescriptors = rdkit_modules
    if Chem is None:
        return None

    smiles_map = {str(key): str(value) for key, value in (variable.get("smiles_map") or {}).items()}
    if not smiles_map:
        return None

    valid: list[tuple[Any, str]] = []
    for name in list(descriptor_names or []) + FALLBACK_DESCRIPTORS:
        if any(existing == name for _module, existing in valid):
            continue
        if Descriptors is not None and hasattr(Descriptors, name):
            valid.append((Descriptors, name))
        elif rdMolDescriptors is not None and hasattr(rdMolDescriptors, name):
            valid.append((rdMolDescriptors, name))

    if not valid:
        return None

    feature_map: dict[str, np.ndarray] = {}
    for label, smiles in smiles_map.items():
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            continue
        values = []
        for module, name in valid[:6]:
            try:
                raw = getattr(module, name)(mol)
                values.append(float(raw) if raw is not None and np.isfinite(float(raw)) else 0.0)
            except Exception:
                values.append(0.0)
        feature_map[label] = np.asarray(values, dtype=float)

    if len(feature_map) < 0.8 * len(smiles_map):
        return None

    if len(feature_map) >= 2:
        matrix = np.stack(list(feature_map.values()), axis=0)
        mean = np.mean(matrix, axis=0)
        std = np.std(matrix, axis=0)
        std = np.where(std < 1e-8, 1.0, std)
        for label in list(feature_map):
            feature_map[label] = (feature_map[label] - mean) / std

    return feature_map
