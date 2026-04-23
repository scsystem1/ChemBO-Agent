"""
Compact OCM domain encoding for pure-reasoning selection.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_OCM_CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "OCM.csv"
AR_LEVEL_LABELS = ("low", "mid", "high")


@dataclass(frozen=True)
class OCMDomainSpec:
    dataset_path: str
    raw_dataframe: pd.DataFrame
    dataframe: pd.DataFrame
    catalyst_list: tuple[str, ...]
    catalyst_to_combo: dict[str, tuple[str, str, str, str]]
    catalyst_index_to_name: dict[str, str]
    temperature_values: tuple[str, ...]
    ct_values: tuple[str, ...]
    ar_level_values_by_ct: dict[str, tuple[str, ...]]
    ar_flow_by_ct_level: dict[tuple[str, str], str]
    ratio_slots_by_ct_level: dict[tuple[str, str], tuple[int, ...]]
    condition_lookup: dict[tuple[str, str, int], tuple[str, str, str]]
    restricted_temperatures_by_catalyst: dict[str, tuple[str, ...]]


@lru_cache(maxsize=8)
def load_ocm_domain_spec(dataset_path: str | Path | None = None) -> OCMDomainSpec:
    resolved_path = Path(dataset_path or DEFAULT_OCM_CSV_PATH).expanduser().resolve()
    frame = pd.read_csv(resolved_path, dtype=str)
    if frame.empty:
        raise ValueError(f"OCM dataset is empty: {resolved_path}")
    required = ["Name", "M1", "M2", "M3", "Support", "Temp", "Ar_flow", "CH4_flow", "O2_flow", "CT"]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"OCM dataset is missing required columns: {missing}")

    raw_frame = frame.copy()
    normalized = frame.copy()
    for column in required:
        raw_frame[column] = raw_frame[column].map(lambda value: str(value).strip())
        normalized[column] = normalized[column].map(_canonicalize_value)

    catalyst_to_combo: dict[str, tuple[str, str, str, str]] = {}
    for row in normalized.itertuples(index=False):
        combo = (row.M1, row.M2, row.M3, row.Support)
        previous = catalyst_to_combo.setdefault(row.Name, combo)
        if previous != combo:
            raise ValueError(f"OCM catalyst name '{row.Name}' maps to multiple composition tuples.")

    catalyst_list = tuple(sorted(catalyst_to_combo.keys()))
    catalyst_index_to_name = {str(index): name for index, name in enumerate(catalyst_list)}

    temperature_values = tuple(sorted({str(value) for value in normalized["Temp"]}, key=_sort_key))
    ct_values = tuple(sorted({str(value) for value in normalized["CT"]}, key=_sort_key))

    ar_level_values_by_ct: dict[str, tuple[str, ...]] = {}
    ar_flow_by_ct_level: dict[tuple[str, str], str] = {}
    ratio_slots_by_ct_level: dict[tuple[str, str], tuple[int, ...]] = {}
    condition_lookup: dict[tuple[str, str, int], tuple[str, str, str]] = {}

    for ct in ct_values:
        ct_rows = normalized[normalized["CT"] == ct]
        ar_values = sorted({str(value) for value in ct_rows["Ar_flow"]}, key=_sort_key)
        level_labels = tuple(AR_LEVEL_LABELS[: len(ar_values)]) if len(ar_values) <= len(AR_LEVEL_LABELS) else tuple(
            f"level_{index}" for index in range(len(ar_values))
        )
        ar_level_values_by_ct[ct] = level_labels
        for ar_value, level in zip(ar_values, level_labels):
            ar_flow_by_ct_level[(ct, level)] = ar_value
            level_rows = ct_rows[ct_rows["Ar_flow"] == ar_value].copy()
            level_rows["_ratio"] = level_rows.apply(
                lambda row: float(row["CH4_flow"]) / float(row["O2_flow"]),
                axis=1,
            )
            recipes = sorted(
                {
                    (
                        float(row["_ratio"]),
                        str(row["Ar_flow"]),
                        str(row["CH4_flow"]),
                        str(row["O2_flow"]),
                    )
                    for _, row in level_rows.iterrows()
                },
                key=lambda item: item[0],
            )
            slots = tuple(range(len(recipes)))
            ratio_slots_by_ct_level[(ct, level)] = slots
            for slot, (_ratio, ar_flow, ch4_flow, o2_flow) in enumerate(recipes):
                key = (ct, level, slot)
                value = (ar_flow, ch4_flow, o2_flow)
                previous = condition_lookup.setdefault(key, value)
                if previous != value:
                    raise ValueError(f"OCM condition lookup collision detected for key {key}.")

    restricted_temperatures_by_catalyst: dict[str, tuple[str, ...]] = {}
    for catalyst_name in catalyst_list:
        catalyst_rows = normalized[normalized["Name"] == catalyst_name]
        available_temps = tuple(sorted({str(value) for value in catalyst_rows["Temp"]}, key=_sort_key))
        if available_temps != temperature_values:
            restricted_temperatures_by_catalyst[catalyst_name] = available_temps

    return OCMDomainSpec(
        dataset_path=str(resolved_path),
        raw_dataframe=raw_frame,
        dataframe=normalized,
        catalyst_list=catalyst_list,
        catalyst_to_combo=catalyst_to_combo,
        catalyst_index_to_name=catalyst_index_to_name,
        temperature_values=temperature_values,
        ct_values=ct_values,
        ar_level_values_by_ct=ar_level_values_by_ct,
        ar_flow_by_ct_level=ar_flow_by_ct_level,
        ratio_slots_by_ct_level=ratio_slots_by_ct_level,
        condition_lookup=condition_lookup,
        restricted_temperatures_by_catalyst=restricted_temperatures_by_catalyst,
    )

def build_domain_prompt(dataset_path: str | Path | None = None) -> str:
    spec = load_ocm_domain_spec(dataset_path)
    catalyst_text = ", ".join(
        f"{index}: {name}" for index, name in enumerate(spec.catalyst_list)
    )
    lines = [
        "[Catalysts]",
        catalyst_text,
        "[Condition Domain]",
        f"Temp in {{{', '.join(spec.temperature_values)}}}",
        f"CT in {{{', '.join(spec.ct_values)}}}",
        "For each CT, ar_level selects the Ar_flow bucket:",
    ]
    for ct in spec.ct_values:
        level_text = ", ".join(
            f"{level}={spec.ar_flow_by_ct_level[(ct, level)]}"
            for level in spec.ar_level_values_by_ct[ct]
        )
        lines.append(f"- CT={ct}: {level_text}")
    lines.append(
        "ch4_o2_ratio is an integer slot. Use 0..N-1 in increasing CH4/O2 ratio order within the chosen (CT, ar_level) bucket."
    )
    unique_slot_counts = sorted({len(value) for value in spec.ratio_slots_by_ct_level.values()})
    lines.append(f"Observed slot counts per bucket: {unique_slot_counts}")
    if spec.restricted_temperatures_by_catalyst:
        lines.append("[Restrictions]")
        for catalyst_name, temps in spec.restricted_temperatures_by_catalyst.items():
            catalyst_index = spec.catalyst_list.index(catalyst_name)
            lines.append(f"- {catalyst_index}: {catalyst_name} only allows Temp in {{{', '.join(temps)}}}")
    return "\n".join(lines)


def decode_candidate(
    proposal: dict[str, Any],
    dataset_path: str | Path | None = None,
) -> dict[str, str]:
    spec = load_ocm_domain_spec(dataset_path)
    catalyst_name = _normalize_cat(proposal.get("cat"), spec)
    temp = _normalize_temp(proposal.get("Temp"), spec)
    ct = _normalize_ct(proposal.get("CT"), spec)
    ar_level = _normalize_ar_level(proposal.get("ar_level"), spec, ct=ct)
    ratio_slot = _normalize_ratio_slot(proposal.get("ch4_o2_ratio"), spec, ct=ct, ar_level=ar_level)

    allowed_temps = spec.restricted_temperatures_by_catalyst.get(catalyst_name, spec.temperature_values)
    if temp not in allowed_temps:
        raise ValueError(
            f"Catalyst '{catalyst_name}' does not allow Temp={temp}. Allowed: {list(allowed_temps)}."
        )

    try:
        ar_flow, ch4_flow, o2_flow = spec.condition_lookup[(ct, ar_level, ratio_slot)]
    except KeyError as exc:
        raise ValueError(f"No OCM condition recipe for {(ct, ar_level, ratio_slot)}.") from exc

    m1, m2, m3, support = spec.catalyst_to_combo[catalyst_name]
    return {
        "M1": m1,
        "M2": m2,
        "M3": m3,
        "Support": support,
        "Temp": temp,
        "Ar_flow": ar_flow,
        "CH4_flow": ch4_flow,
        "O2_flow": o2_flow,
        "CT": ct,
        "Name": catalyst_name,
    }


def decode_proposal(
    proposal: dict[str, Any],
    dataset_path: str | Path | None = None,
) -> pd.Series:
    spec = load_ocm_domain_spec(dataset_path)
    candidate = decode_candidate(proposal, dataset_path=dataset_path)
    normalized_mask = (
        (spec.dataframe["M1"] == candidate["M1"])
        & (spec.dataframe["M2"] == candidate["M2"])
        & (spec.dataframe["M3"] == candidate["M3"])
        & (spec.dataframe["Support"] == candidate["Support"])
        & (spec.dataframe["Temp"] == candidate["Temp"])
        & (spec.dataframe["Ar_flow"] == candidate["Ar_flow"])
        & (spec.dataframe["CH4_flow"] == candidate["CH4_flow"])
        & (spec.dataframe["O2_flow"] == candidate["O2_flow"])
        & (spec.dataframe["CT"] == candidate["CT"])
    )
    matched_indexes = spec.dataframe.index[normalized_mask]
    if len(matched_indexes) == 0:
        raise ValueError("Decoded OCM proposal did not match any dataset row.")
    if len(matched_indexes) != 1:
        raise ValueError("Decoded OCM proposal matched multiple dataset rows.")
    return spec.raw_dataframe.loc[matched_indexes[0]]


def _normalize_cat(raw_value: Any, spec: OCMDomainSpec) -> str:
    text = str(raw_value or "").strip()
    if not text:
        raise ValueError("Proposal is missing `cat`.")
    if text in spec.catalyst_index_to_name:
        return spec.catalyst_index_to_name[text]
    for index, name in enumerate(spec.catalyst_list):
        if text == name:
            return name
        if text == str(index):
            return name
    raise ValueError(f"Unknown OCM catalyst identifier: {text}")


def _normalize_temp(raw_value: Any, spec: OCMDomainSpec) -> str:
    return _normalize_exact_choice(raw_value, spec.temperature_values, field_name="Temp")


def _normalize_ct(raw_value: Any, spec: OCMDomainSpec) -> str:
    return _normalize_exact_choice(raw_value, spec.ct_values, field_name="CT")


def _normalize_ar_level(raw_value: Any, spec: OCMDomainSpec, *, ct: str) -> str:
    text = str(raw_value or "").strip().lower()
    allowed = spec.ar_level_values_by_ct.get(ct, ())
    if text in allowed:
        return text
    raise ValueError(f"Invalid ar_level '{raw_value}'. Allowed for CT={ct}: {list(allowed)}")


def _normalize_ratio_slot(raw_value: Any, spec: OCMDomainSpec, *, ct: str, ar_level: str) -> int:
    try:
        slot = int(str(raw_value).strip())
    except Exception as exc:
        raise ValueError("`ch4_o2_ratio` must be an integer slot.") from exc
    allowed = spec.ratio_slots_by_ct_level.get((ct, ar_level), ())
    if slot not in allowed:
        raise ValueError(f"Invalid ch4_o2_ratio slot {slot} for CT={ct}, ar_level={ar_level}. Allowed: {list(allowed)}")
    return slot


def _normalize_exact_choice(raw_value: Any, allowed: tuple[str, ...], *, field_name: str) -> str:
    text = str(raw_value or "").strip()
    if not text:
        raise ValueError(f"Proposal is missing `{field_name}`.")
    raw_numeric = _try_float(text)
    for value in allowed:
        if text == value:
            return value
        allowed_numeric = _try_float(value)
        if raw_numeric is not None and allowed_numeric is not None and abs(raw_numeric - allowed_numeric) < 1e-9:
            return value
    raise ValueError(f"Invalid {field_name} '{raw_value}'. Allowed: {list(allowed)}")


def _sort_key(value: Any) -> tuple[int, float | str]:
    numeric = _try_float(value)
    if numeric is not None:
        return (0, float(numeric))
    return (1, str(value))


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


def _try_float(value: Any) -> float | None:
    try:
        numeric = float(str(value).strip())
    except Exception:
        return None
    return numeric


CATALYST_LIST = load_ocm_domain_spec().catalyst_list
CONDITION_LOOKUP = load_ocm_domain_spec().condition_lookup
