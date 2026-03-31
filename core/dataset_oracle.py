"""
Dataset-backed experiment oracle for benchmark-style campaigns.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LoadedDataset:
    csv_path: str
    feature_columns: tuple[str, ...]
    target_column: str
    row_id_column: str | None
    rows: tuple[dict[str, str], ...]
    candidates: tuple[dict[str, str], ...]
    index: dict[tuple[str, ...], dict[str, str]]
    domain_values: dict[str, tuple[str, ...]]


class DatasetOracle:
    """Lookup real experimental outcomes from a tabular benchmark dataset."""

    def __init__(self, dataset_spec: dict[str, Any]):
        csv_path = dataset_spec.get("csv_path")
        feature_columns = tuple(dataset_spec.get("feature_columns") or [])
        target_column = str(dataset_spec.get("target_column") or "")
        row_id_column = dataset_spec.get("row_id_column")
        if not csv_path or not feature_columns or not target_column:
            raise ValueError("Dataset spec requires csv_path, feature_columns, and target_column.")

        self._dataset = _load_dataset(
            str(Path(csv_path).expanduser().resolve()),
            feature_columns,
            target_column,
            str(row_id_column) if row_id_column is not None else None,
        )

    @classmethod
    def from_problem_spec(cls, problem_spec: dict[str, Any]) -> "DatasetOracle | None":
        dataset_spec = problem_spec.get("dataset")
        if not isinstance(dataset_spec, dict):
            return None
        return cls(dataset_spec)

    @property
    def feature_columns(self) -> tuple[str, ...]:
        return self._dataset.feature_columns

    @property
    def size(self) -> int:
        return len(self._dataset.rows)

    @property
    def domain_values(self) -> dict[str, tuple[str, ...]]:
        return self._dataset.domain_values

    @property
    def candidates(self) -> tuple[dict[str, str], ...]:
        return tuple(candidate.copy() for candidate in self._dataset.candidates)

    def candidate_exists(self, candidate: dict[str, Any]) -> bool:
        return self._candidate_key(candidate) in self._dataset.index

    def lookup(self, candidate: dict[str, Any]) -> dict[str, Any]:
        key = self._candidate_key(candidate)
        row = self._dataset.index.get(key)
        if row is None:
            formatted = {column: _canonicalize_value(candidate.get(column)) for column in self.feature_columns}
            raise KeyError(f"Candidate not found in dataset: {formatted}")

        canonical_candidate = {column: row[column] for column in self.feature_columns}
        metadata = {
            "dataset_path": self._dataset.csv_path,
            "dataset_target_column": self._dataset.target_column,
        }
        if self._dataset.row_id_column is not None:
            metadata["dataset_row_id"] = row.get(self._dataset.row_id_column, "")

        return {
            "candidate": canonical_candidate,
            "result": float(row[self._dataset.target_column]),
            "row": row.copy(),
            "metadata": metadata,
        }

    def _candidate_key(self, candidate: dict[str, Any]) -> tuple[str, ...]:
        return tuple(_canonicalize_value(candidate.get(column)) for column in self.feature_columns)


@lru_cache(maxsize=32)
def _load_dataset(
    csv_path: str,
    feature_columns: tuple[str, ...],
    target_column: str,
    row_id_column: str | None,
) -> LoadedDataset:
    with Path(csv_path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = [dict(row) for row in reader]

    if not rows:
        raise ValueError(f"Dataset is empty: {csv_path}")

    required = set(feature_columns) | {target_column}
    if row_id_column is not None:
        required.add(row_id_column)
    missing = sorted(column for column in required if column not in rows[0])
    if missing:
        raise ValueError(f"Dataset {csv_path} is missing required columns: {missing}")

    index: dict[tuple[str, ...], dict[str, str]] = {}
    domain_sets = {column: set() for column in feature_columns}
    for row in rows:
        normalized_row = {key: _canonicalize_value(value) for key, value in row.items()}
        key = tuple(normalized_row[column] for column in feature_columns)
        if key in index:
            raise ValueError(f"Duplicate candidate detected in dataset {csv_path}: {key}")
        index[key] = normalized_row
        for column in feature_columns:
            domain_sets[column].add(normalized_row[column])

    ordered_rows = tuple(index[key] for key in index)
    candidates = tuple(
        {column: row[column] for column in feature_columns}
        for row in ordered_rows
    )

    return LoadedDataset(
        csv_path=csv_path,
        feature_columns=feature_columns,
        target_column=target_column,
        row_id_column=row_id_column,
        rows=ordered_rows,
        candidates=candidates,
        index=index,
        domain_values={column: tuple(sorted(values)) for column, values in domain_sets.items()},
    )


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
