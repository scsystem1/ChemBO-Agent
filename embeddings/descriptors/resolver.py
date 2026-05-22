from __future__ import annotations

import csv
from pathlib import Path

from .rdkit_2d import canonicalize_smiles, looks_like_smiles
from .schema import ResolvedEntity
from .table_store import DEFAULT_DESCRIPTOR_ROOT


ABSENT_PREFIX = "absent:"


class EntityResolver:
    def __init__(self, descriptor_root: str | Path | None = None):
        root = Path(descriptor_root or DEFAULT_DESCRIPTOR_ROOT).resolve()
        self.name_to_structure_path = root / "entities" / "name_to_structure.csv"
        self.dataset_value_map_path = root / "entities" / "dataset_value_map.csv"
        self.name_to_structure = self._load_name_to_structure()
        self.dataset_value_map = self._load_dataset_value_map()

    def _load_name_to_structure(self) -> dict[str, dict[str, str]]:
        if not self.name_to_structure_path.exists():
            return {}
        out: dict[str, dict[str, str]] = {}
        with self.name_to_structure_path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                key = str(row.get("entity_key") or "").strip()
                if key:
                    out[key] = {str(k): str(v or "").strip() for k, v in row.items()}
        return out

    def _load_dataset_value_map(self) -> dict[tuple[str, str, str], dict[str, str]]:
        if not self.dataset_value_map_path.exists():
            return {}
        out: dict[tuple[str, str, str], dict[str, str]] = {}
        with self.dataset_value_map_path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                dataset = str(row.get("dataset") or "").strip().lower()
                variable = str(row.get("variable") or "").strip()
                raw_value = str(row.get("raw_value") or "").strip()
                if dataset and variable and raw_value:
                    out[(dataset, variable, raw_value)] = {str(k): str(v or "").strip() for k, v in row.items()}
        return out

    def resolve(
        self,
        *,
        dataset: str,
        variable: str,
        raw_value: str,
        resolver: str = "dataset_value_map",
        entity_kind: str = "molecule",
        allow_absent_values: set[str] | None = None,
    ) -> ResolvedEntity:
        raw = str(raw_value or "").strip()
        absent_values = {str(item).strip() for item in (allow_absent_values or set())}
        if raw in absent_values:
            return ResolvedEntity(
                raw_value=raw,
                entity_key=f"{ABSENT_PREFIX}{variable}:{raw}",
                entity_kind=entity_kind,
                allow_absent=True,
                curation_status="ready",
                notes="structural absence declared in YAML",
            )
        if resolver == "smiles_direct":
            canonical = canonicalize_smiles(raw)
            return ResolvedEntity(
                raw_value=raw,
                entity_key=f"smiles:{canonical}",
                entity_kind=entity_kind or "molecule",
                smiles=canonical,
                allow_absent=False,
                curation_status="ready",
                source_id="rdkit_programmatic",
            )

        row = self.dataset_value_map.get((str(dataset or "").strip().lower(), variable, raw))
        if row is None:
            if looks_like_smiles(raw):
                canonical = canonicalize_smiles(raw)
                return ResolvedEntity(
                    raw_value=raw,
                    entity_key=f"smiles:{canonical}",
                    entity_kind=entity_kind or "molecule",
                    smiles=canonical,
                    allow_absent=False,
                    curation_status="ready",
                    source_id="rdkit_programmatic",
                )
            raise KeyError(f"Cannot resolve descriptor entity for {dataset}.{variable}={raw}")

        entity_key = str(row.get("entity_key") or "").strip()
        allow_absent = str(row.get("allow_absent") or "").strip().lower() in {"true", "1", "yes"}
        entity = self.name_to_structure.get(entity_key, {})
        return ResolvedEntity(
            raw_value=raw,
            entity_key=entity_key,
            entity_kind=str(entity.get("entity_kind") or entity_kind or row.get("role") or "").strip(),
            smiles=str(entity.get("smiles") or "").strip(),
            formula=str(entity.get("formula") or "").strip(),
            allow_absent=allow_absent or entity_key.startswith(ABSENT_PREFIX),
            curation_status=str(row.get("curation_status") or entity.get("curation_status") or "ready").strip(),
            source_id=str(entity.get("source_id") or "").strip(),
            notes=str(row.get("notes") or entity.get("notes") or "").strip(),
        )

