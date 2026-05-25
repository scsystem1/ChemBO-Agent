from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np


FORBIDDEN_EXACT = {
    "material_family",
    "solvent_type",
    "ligand_class",
    "support_class",
    "element_group_label",
    "substrate_class",
    "handle_class",
    "ligand_family",
    "support_family",
}

FORBIDDEN_PREFIXES = (
    "is_",
    "has_label_",
    "onehot_",
    "category_",
)

FORBIDDEN_CONTAINS = (
    "class_id",
    "label_id",
    "category_id",
    "identity",
    "one_hot",
)

ALLOWED_BINARY_OR_INTEGER_PATTERNS = (
    "_count",
    "count_",
    "formal_charge",
    "formalcharge",
    "cation_charge",
    "anion_charge",
    "oxidation_state",
    "d_electron_count",
    "denticity",
    "period",
    "group",
    "num",
    "ringcount",
    "atomcount",
    "nhohcount",
    "nocount",
)

CRITICAL_COLLISION_PAIRS = {
    tuple(sorted(pair))
    for pair in (
        ("SiC", "SiCnf"),
        ("SiO2", "BEA"),
        ("SiO2", "ZSM-5"),
    )
}


def validate_descriptor_name(name: str, scale_type: str) -> None:
    normalized = str(name or "").strip()
    if not normalized:
        raise ValueError("Descriptor name cannot be empty.")
    lowered = normalized.lower()
    if lowered in FORBIDDEN_EXACT:
        raise ValueError(f"Pure categorical descriptor is forbidden: {normalized}")
    if lowered.startswith(FORBIDDEN_PREFIXES):
        raise ValueError(f"Descriptor name looks categorical/one-hot: {normalized}")
    if any(token in lowered for token in FORBIDDEN_CONTAINS):
        raise ValueError(f"Descriptor name looks like an arbitrary category id: {normalized}")
    if scale_type in {"integer_count", "integer_state", "binary_count"}:
        if not any(pattern in lowered for pattern in ALLOWED_BINARY_OR_INTEGER_PATTERNS):
            raise ValueError(
                "Integer/binary descriptor must be a chemically defined count/state: "
                f"{normalized}"
            )


def normalize_selected_descriptors(payload: Any) -> list[tuple[str, str]]:
    selected: list[tuple[str, str]] = []
    if not isinstance(payload, list):
        return selected
    for item in payload:
        if isinstance(item, dict):
            pool = str(item.get("pool") or "").strip()
            name = str(item.get("name") or item.get("descriptor_name") or "").strip()
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            pool = str(item[0]).strip()
            name = str(item[1]).strip()
        else:
            continue
        if pool and name and (pool, name) not in selected:
            selected.append((pool, name))
    return selected


DESCRIPTORS_MIN_PER_VARIABLE: int = 1
DESCRIPTORS_MAX_PER_VARIABLE: int = 3


def validate_selected_descriptors(
    *,
    selected_descriptors: list[tuple[str, str]],
    available_descriptors: dict[str, list[str]],
    scale_types: dict[tuple[str, str], str],
    allow_semichemical_ordinal: bool,
    max_selected: int | None = None,
) -> None:
    min_required = DESCRIPTORS_MIN_PER_VARIABLE
    max_allowed = int(max_selected or DESCRIPTORS_MAX_PER_VARIABLE)
    n_selected = len(selected_descriptors)
    if n_selected < min_required or n_selected > max_allowed:
        raise ValueError(
            f"Between {min_required} and {max_allowed} descriptors must be selected per variable; got {n_selected}."
        )
    allowed = {
        (str(pool), str(name))
        for pool, names in (available_descriptors or {}).items()
        for name in (names or [])
    }
    for pool, name in selected_descriptors:
        if (pool, name) not in allowed:
            raise ValueError(f"Descriptor was not declared as available in YAML: {pool}.{name}")
        scale_type = str(scale_types.get((pool, name), "continuous"))
        validate_descriptor_name(name, scale_type)
        if scale_type == "ordinal_semichemical" and not allow_semichemical_ordinal:
            raise ValueError(f"Ordinal semichemical descriptor is not allowed here: {pool}.{name}")


def validate_coverage(
    *,
    labels: list[str],
    descriptor_keys: list[tuple[str, str]],
    known_mask: np.ndarray,
    present_mask: np.ndarray,
    dataset: str,
    variable: str,
) -> None:
    known = np.asarray(known_mask, dtype=bool)
    present = np.asarray(present_mask, dtype=bool).reshape(-1)
    for row_index, label in enumerate(labels):
        if not present[row_index]:
            continue
        for col_index, descriptor_key in enumerate(descriptor_keys):
            if not known[row_index, col_index]:
                pool, desc = descriptor_key
                raise ValueError(
                    f"Missing selected descriptor for {dataset}.{variable}: "
                    f"raw_value={label}, descriptor={pool}.{desc}"
                )


def collision_groups(
    labels: list[str],
    values: np.ndarray,
    present_mask: np.ndarray,
    *,
    decimals: int = 6,
) -> list[list[str]]:
    buckets: dict[tuple[float, ...], list[str]] = defaultdict(list)
    rounded = np.round(np.asarray(values, dtype=float), decimals=decimals)
    present = np.asarray(present_mask, dtype=bool).reshape(-1)
    for label, row, is_present in zip(labels, rounded, present):
        if not is_present:
            continue
        buckets[tuple(row.tolist())].append(str(label))
    return [group for group in buckets.values() if len(group) > 1]


def validate_collision(
    *,
    labels: list[str],
    values: np.ndarray,
    present_mask: np.ndarray,
    critical_pairs: list[list[str]] | None = None,
    decimals: int = 6,
) -> None:
    collisions = collision_groups(labels, values, present_mask, decimals=decimals)
    if not collisions:
        return
    critical = set(CRITICAL_COLLISION_PAIRS)
    for pair in critical_pairs or []:
        if len(pair) >= 2:
            critical.add(tuple(sorted((str(pair[0]), str(pair[1])))))
    for group in collisions:
        group_set = set(group)
        for left, right in critical:
            if left in group_set and right in group_set:
                raise ValueError(f"Critical descriptor collision detected: {left} vs {right}")
    raise ValueError(f"Descriptor collisions detected: {collisions}")
