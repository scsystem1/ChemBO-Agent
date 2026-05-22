from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .schema import DescriptorSpec, DescriptorValue
from .validation import validate_descriptor_name


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DESCRIPTOR_ROOT = PROJECT_ROOT / "data" / "descriptors"


def _split_kinds(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in str(value or "").split("|") if item.strip())


def _as_bool(value: Any, default: bool = False) -> bool:
    text = str(value if value is not None else "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on"}


class DescriptorTableStore:
    def __init__(self, descriptor_root: str | Path | None = None):
        self.descriptor_root = Path(descriptor_root or DEFAULT_DESCRIPTOR_ROOT).resolve()
        self.manifest_path = self.descriptor_root / "manifests" / "descriptor_manifest.csv"
        self.tables_dir = self.descriptor_root / "tables_long"
        self.manifest = self._load_manifest()
        self.values = self._load_values()

    def _load_manifest(self) -> dict[tuple[str, str], DescriptorSpec]:
        if not self.manifest_path.exists():
            return {}
        out: dict[tuple[str, str], DescriptorSpec] = {}
        with self.manifest_path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                pool = str(row.get("pool") or "").strip()
                name = str(row.get("descriptor_name") or "").strip()
                if not pool or not name:
                    continue
                scale_type = str(row.get("scale_type") or "continuous").strip()
                validate_descriptor_name(name, scale_type)
                out[(pool, name)] = DescriptorSpec(
                    pool=pool,
                    name=name,
                    unit=str(row.get("unit") or "").strip() or None,
                    scale_type=scale_type,  # type: ignore[arg-type]
                    allowed_entity_kinds=_split_kinds(row.get("allowed_entity_kinds", "")),
                    description=str(row.get("description") or "").strip(),
                    preferred=_as_bool(row.get("preferred")),
                    requires_source=_as_bool(row.get("requires_source"), default=True),
                    default_enabled=_as_bool(row.get("default_enabled"), default=True),
                )
        return out

    def _load_values(self) -> dict[tuple[str, str, str], DescriptorValue]:
        if not self.tables_dir.exists():
            return {}
        out: dict[tuple[str, str, str], DescriptorValue] = {}
        for path in sorted(self.tables_dir.glob("*.csv")):
            with path.open(newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    entity_key = str(row.get("entity_key") or "").strip()
                    pool = str(row.get("pool") or "").strip()
                    name = str(row.get("descriptor_name") or "").strip()
                    raw_value = str(row.get("value") or "").strip()
                    if not entity_key or not pool or not name or not raw_value:
                        continue
                    try:
                        value = float(raw_value)
                    except ValueError:
                        continue
                    scale_type = str(row.get("scale_type") or self.scale_type(pool, name)).strip()
                    validate_descriptor_name(name, scale_type)
                    out[(entity_key, pool, name)] = DescriptorValue(
                        entity_key=entity_key,
                        pool=pool,
                        name=name,
                        value=value,
                        unit=str(row.get("unit") or "").strip() or None,
                        source_id=str(row.get("source_id") or "").strip(),
                        source_tier=str(row.get("source_tier") or "C").strip(),  # type: ignore[arg-type]
                        scale_type=scale_type,  # type: ignore[arg-type]
                        curation_status=str(row.get("curation_status") or "deferred").strip(),  # type: ignore[arg-type]
                    )
        return out

    def scale_type(self, pool: str, descriptor_name: str) -> str:
        spec = self.manifest.get((pool, descriptor_name))
        return str(spec.scale_type) if spec is not None else "continuous"

    def scale_types_for_available(self, available: dict[str, list[str]]) -> dict[tuple[str, str], str]:
        return {
            (str(pool), str(name)): self.scale_type(str(pool), str(name))
            for pool, names in (available or {}).items()
            for name in names or []
        }

    def get_value(self, entity_key: str, pool: str, descriptor_name: str) -> DescriptorValue | None:
        value = self.values.get((entity_key, pool, descriptor_name))
        if value is None or value.curation_status != "ready":
            return None
        return value

