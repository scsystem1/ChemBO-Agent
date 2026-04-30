"""
Compact Suzuki domain encoding for pure-reasoning selection.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_SUZUKI_CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "suzuki.csv"


@dataclass(frozen=True)
class SuzukiDomainSpec:
    dataset_path: str
    raw_dataframe: pd.DataFrame
    dataframe: pd.DataFrame
    pair_list: tuple[tuple[str, str], ...]
    pair_index_to_pair: dict[str, tuple[str, str]]
    pair_to_index: dict[tuple[str, str], str]
    ligand_values: tuple[str, ...]
    reagent_values: tuple[str, ...]
    solvent_values: tuple[str, ...]


@lru_cache(maxsize=8)
def load_suzuki_domain_spec(dataset_path: str | Path | None = None) -> SuzukiDomainSpec:
    resolved_path = Path(dataset_path or DEFAULT_SUZUKI_CSV_PATH).expanduser().resolve()
    frame = pd.read_csv(resolved_path, dtype=str)
    if frame.empty:
        raise ValueError(f"Suzuki dataset is empty: {resolved_path}")

    required = [
        "Reactant_1_Name",
        "Reactant_2_Name",
        "Ligand_Short_Hand",
        "Reagent_1_Short_Hand",
        "Solvent_1_Short_Hand",
    ]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"Suzuki dataset is missing required columns: {missing}")

    raw_frame = frame.copy()
    normalized = frame.copy()
    for column in required:
        raw_frame[column] = raw_frame[column].map(_canonicalize_value)
        normalized[column] = normalized[column].map(_canonicalize_value)

    pair_records = sorted(
        {
            (str(row.Reactant_1_Name), str(row.Reactant_2_Name))
            for row in normalized.itertuples(index=False)
        },
        key=lambda item: (item[0], item[1]),
    )
    pair_list = tuple(pair_records)
    pair_index_to_pair = {str(index + 1): pair for index, pair in enumerate(pair_list)}
    pair_to_index = {pair: index for index, pair in pair_index_to_pair.items()}

    ligand_values = tuple(sorted({str(value) for value in normalized["Ligand_Short_Hand"]}))
    reagent_values = tuple(sorted({str(value) for value in normalized["Reagent_1_Short_Hand"]}))
    solvent_values = tuple(sorted({str(value) for value in normalized["Solvent_1_Short_Hand"]}))

    return SuzukiDomainSpec(
        dataset_path=str(resolved_path),
        raw_dataframe=raw_frame,
        dataframe=normalized,
        pair_list=pair_list,
        pair_index_to_pair=pair_index_to_pair,
        pair_to_index=pair_to_index,
        ligand_values=ligand_values,
        reagent_values=reagent_values,
        solvent_values=solvent_values,
    )


def build_domain_prompt(dataset_path: str | Path | None = None) -> str:
    spec = load_suzuki_domain_spec(dataset_path)
    lines = [
        "This Suzuki benchmark is fully defined by one legal substrate-pair choice plus exact ligand/base/solvent labels.",
        "Choose exactly one legal Reactant_1 + Reactant_2 pair from the list below.",
        "[Legal Substrate Pairs]",
    ]
    lines.extend(
        [
            f"- {pair_id}: {reactant_1} + {reactant_2}"
            for pair_id, (reactant_1, reactant_2) in spec.pair_index_to_pair.items()
        ]
    )
    lines.extend(
        [
            "[Other Variables]",
            f"Ligand_Short_Hand in {{{', '.join(spec.ligand_values)}}}",
            f"Reagent_1_Short_Hand in {{{', '.join(spec.reagent_values)}}}",
            f"Solvent_1_Short_Hand in {{{', '.join(spec.solvent_values)}}}",
            "For every legal substrate pair, all ligand/reagent/solvent combinations from these exact vocabularies are legal dataset rows.",
        ]
    )
    return "\n".join(lines)


def decode_candidate(
    proposal: dict[str, Any],
    dataset_path: str | Path | None = None,
) -> dict[str, str]:
    spec = load_suzuki_domain_spec(dataset_path)
    reactant_1, reactant_2 = _normalize_pair(proposal, spec)
    ligand = _normalize_exact_choice(
        _proposal_value(proposal, "Ligand_Short_Hand", "Ligand", "ligand"),
        spec.ligand_values,
        field_name="Ligand_Short_Hand",
    )
    reagent = _normalize_exact_choice(
        _proposal_value(proposal, "Reagent_1_Short_Hand", "Reagent", "reagent"),
        spec.reagent_values,
        field_name="Reagent_1_Short_Hand",
    )
    solvent = _normalize_exact_choice(
        _proposal_value(proposal, "Solvent_1_Short_Hand", "Solvent", "solvent"),
        spec.solvent_values,
        field_name="Solvent_1_Short_Hand",
    )
    return {
        "Reactant_1_Name": reactant_1,
        "Reactant_2_Name": reactant_2,
        "Ligand_Short_Hand": ligand,
        "Reagent_1_Short_Hand": reagent,
        "Solvent_1_Short_Hand": solvent,
    }


def decode_proposal(
    proposal: dict[str, Any],
    dataset_path: str | Path | None = None,
) -> pd.Series:
    spec = load_suzuki_domain_spec(dataset_path)
    candidate = decode_candidate(proposal, dataset_path=dataset_path)
    normalized_mask = (
        (spec.dataframe["Reactant_1_Name"] == candidate["Reactant_1_Name"])
        & (spec.dataframe["Reactant_2_Name"] == candidate["Reactant_2_Name"])
        & (spec.dataframe["Ligand_Short_Hand"] == candidate["Ligand_Short_Hand"])
        & (spec.dataframe["Reagent_1_Short_Hand"] == candidate["Reagent_1_Short_Hand"])
        & (spec.dataframe["Solvent_1_Short_Hand"] == candidate["Solvent_1_Short_Hand"])
    )
    matched_indexes = spec.dataframe.index[normalized_mask]
    if len(matched_indexes) == 0:
        raise ValueError("Decoded Suzuki proposal did not match any dataset row.")
    if len(matched_indexes) != 1:
        raise ValueError("Decoded Suzuki proposal matched multiple dataset rows.")
    return spec.raw_dataframe.loc[matched_indexes[0]]


def _normalize_pair(proposal: dict[str, Any], spec: SuzukiDomainSpec) -> tuple[str, str]:
    raw_pair_id = _proposal_value(proposal, "pair_id", "pair", "pair_index")
    if raw_pair_id is not None:
        pair_id = str(raw_pair_id).strip()
        if pair_id in spec.pair_index_to_pair:
            return spec.pair_index_to_pair[pair_id]
        raise ValueError(f"Unknown Suzuki pair_id '{raw_pair_id}'. Allowed: {list(spec.pair_index_to_pair)}")

    reactant_1 = _proposal_value(proposal, "Reactant_1_Name", "reactant_1")
    reactant_2 = _proposal_value(proposal, "Reactant_2_Name", "reactant_2")
    candidate_pair = (_canonicalize_value(reactant_1), _canonicalize_value(reactant_2))
    if candidate_pair in spec.pair_to_index:
        return candidate_pair
    raise ValueError("Proposal must include a valid `pair_id` or an exact legal (Reactant_1_Name, Reactant_2_Name) pair.")


def _proposal_value(proposal: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in proposal:
            return proposal.get(key)
    return None


def _normalize_exact_choice(raw_value: Any, allowed: tuple[str, ...], *, field_name: str) -> str:
    text = _canonicalize_value(raw_value)
    if not text:
        raise ValueError(f"Proposal is missing `{field_name}`.")
    for value in allowed:
        if text == value:
            return value
    raise ValueError(f"Invalid {field_name} '{raw_value}'. Allowed: {list(allowed)}")


def _canonicalize_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(value, ".15g")
    return str(value).strip()
