from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np

ScaleType = Literal[
    "continuous",
    "integer_count",
    "integer_state",
    "binary_count",
    "ordinal_semichemical",
]

SourceTier = Literal["A", "B", "C"]
CurationStatus = Literal["ready", "deferred", "not_applicable", "blocked"]


@dataclass(frozen=True)
class DescriptorSpec:
    pool: str
    name: str
    unit: str | None
    scale_type: ScaleType
    allowed_entity_kinds: tuple[str, ...]
    description: str
    preferred: bool = False
    requires_source: bool = True
    default_enabled: bool = True


@dataclass(frozen=True)
class DescriptorValue:
    entity_key: str
    pool: str
    name: str
    value: float
    unit: str | None
    source_id: str
    source_tier: SourceTier
    scale_type: ScaleType
    curation_status: CurationStatus


@dataclass
class ResolvedEntity:
    raw_value: str
    entity_key: str
    entity_kind: str
    smiles: str = ""
    formula: str = ""
    allow_absent: bool = False
    curation_status: str = "ready"
    source_id: str = ""
    notes: str = ""


@dataclass
class DescriptorMatrix:
    labels: list[str]
    entity_keys: list[str]
    descriptor_keys: list[tuple[str, str]]
    values: np.ndarray
    present_mask: np.ndarray
    known_mask: np.ndarray
    metadata: dict = field(default_factory=dict)

