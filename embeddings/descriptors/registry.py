from __future__ import annotations

from dataclasses import asdict
from typing import Any

import numpy as np

from .formula_descriptors import element_descriptor, formula_descriptor
from .rdkit_2d import calc_rdkit_2d, calc_smarts_counts
from .resolver import EntityResolver
from .schema import DescriptorMatrix, ResolvedEntity
from .selector_prompt import build_descriptor_selection_prompt
from .table_store import DescriptorTableStore
from .validation import (
    collision_groups,
    normalize_selected_descriptors,
    validate_collision,
    validate_coverage,
    validate_selected_descriptors,
)
from .yaml_expander import categorical_descriptor_variables, dataset_key_from_problem


PROGRAMMATIC_POOLS = {"rdkit_2d", "rdkit_substructure_counts", "element_oxide_physchem", "support_material_physchem"}


class DescriptorRegistry:
    def __init__(
        self,
        *,
        table_store: DescriptorTableStore | None = None,
        resolver: EntityResolver | None = None,
    ):
        self.table_store = table_store or DescriptorTableStore()
        self.resolver = resolver or EntityResolver(self.table_store.descriptor_root)

    def build_matrix(
        self,
        *,
        dataset: str,
        variable: dict[str, Any],
        selected_descriptors: list[tuple[str, str]],
    ) -> DescriptorMatrix:
        name = str(variable.get("name") or "").strip()
        descriptor = dict(variable.get("descriptor") or {})
        absent_values = {str(item).strip() for item in descriptor.get("allow_absent_values") or []}
        labels = [str(item) for item in variable.get("domain", [])]
        resolved = [
            self.resolver.resolve(
                dataset=dataset,
                variable=name,
                raw_value=label,
                resolver=str(descriptor.get("resolver") or "dataset_value_map"),
                entity_kind=str(descriptor.get("entity_kind") or variable.get("role") or "molecule"),
                allow_absent_values=absent_values,
            )
            for label in labels
        ]
        raw_values = np.zeros((len(labels), len(selected_descriptors)), dtype=float)
        known_mask = np.ones((len(labels), len(selected_descriptors)), dtype=bool)
        present_mask = np.asarray([not entity.allow_absent for entity in resolved], dtype=bool)
        sources: dict[str, str] = {}
        deferred: list[str] = []

        for row_index, entity in enumerate(resolved):
            if entity.allow_absent:
                continue
            if entity.curation_status != "ready":
                known_mask[row_index, :] = False
                deferred.extend(f"{entity.entity_key}:{pool}.{desc}" for pool, desc in selected_descriptors)
                continue
            for col_index, (pool, desc) in enumerate(selected_descriptors):
                value, source_id = self._descriptor_value(entity, pool, desc)
                if value is None:
                    known_mask[row_index, col_index] = False
                    deferred.append(f"{entity.entity_key}:{pool}.{desc}")
                    continue
                raw_values[row_index, col_index] = float(value)
                if source_id:
                    sources[f"{entity.entity_key}:{pool}.{desc}"] = source_id

        matrix = DescriptorMatrix(
            labels=labels,
            entity_keys=[entity.entity_key for entity in resolved],
            descriptor_keys=list(selected_descriptors),
            values=raw_values,
            present_mask=present_mask,
            known_mask=known_mask,
            metadata={
                "resolved_entities": [asdict(entity) for entity in resolved],
                "sources": sources,
                "deferred_descriptors_not_used": sorted(set(deferred)),
            },
        )
        return matrix

    def validate_matrix(
        self,
        *,
        dataset: str,
        variable: dict[str, Any],
        matrix: DescriptorMatrix,
    ) -> None:
        name = str(variable.get("name") or "").strip()
        descriptor = dict(variable.get("descriptor") or {})
        validation = dict(descriptor.get("validation") or {})
        if str(validation.get("coverage") or "strict_non_absent") == "strict_non_absent":
            validate_coverage(
                labels=matrix.labels,
                descriptor_keys=matrix.descriptor_keys,
                known_mask=matrix.known_mask,
                present_mask=matrix.present_mask,
                dataset=dataset,
                variable=name,
            )
        collision_mode = str(validation.get("collision") or "fail_on_selected_descriptor_collision")
        if collision_mode == "fail_on_selected_descriptor_collision":
            validate_collision(
                labels=matrix.labels,
                values=matrix.values,
                present_mask=matrix.present_mask,
                critical_pairs=validation.get("critical_collision_pairs") or [],
            )
        elif collision_mode == "warn_if_rdkit_only_collision":
            matrix.metadata["collision_warnings"] = collision_groups(
                matrix.labels,
                matrix.values,
                matrix.present_mask,
            )

    def scaled_feature_map(self, matrix: DescriptorMatrix, *, include_present_mask: bool = False) -> dict[str, list[float]]:
        values = np.asarray(matrix.values, dtype=float).copy()
        known = np.asarray(matrix.known_mask, dtype=bool)
        present = np.asarray(matrix.present_mask, dtype=bool).reshape(-1)
        if values.size:
            for col in range(values.shape[1]):
                mask = present & known[:, col] & np.isfinite(values[:, col])
                if not np.any(mask):
                    values[:, col] = 0.0
                    continue
                col_min = float(np.min(values[mask, col]))
                col_max = float(np.max(values[mask, col]))
                if col_max - col_min < 1e-12:
                    values[:, col] = 0.0
                    continue
                values[:, col] = (values[:, col] - col_min) / (col_max - col_min)
                values[~(present & known[:, col] & np.isfinite(values[:, col])), col] = 0.0
        if include_present_mask:
            values = np.column_stack([values, present.astype(float)])
        return {
            label: [float(item) for item in values[index].reshape(-1).tolist()]
            for index, label in enumerate(matrix.labels)
        }

    def _descriptor_value(self, entity: ResolvedEntity, pool: str, desc: str) -> tuple[float | None, str]:
        if pool == "rdkit_2d":
            if not entity.smiles:
                return None, ""
            return calc_rdkit_2d(entity.smiles, [desc]).get(desc), "rdkit_programmatic"
        if pool == "rdkit_substructure_counts":
            if not entity.smiles:
                return None, ""
            return calc_smarts_counts(entity.smiles, [desc]).get(desc), "rdkit_programmatic"
        if pool == "element_oxide_physchem":
            if entity.entity_kind == "element":
                value = element_descriptor(entity.formula or entity.raw_value, desc)
                if value is not None:
                    return value, "periodic_table_builtin"
            table_value = self.table_store.get_value(entity.entity_key, pool, desc)
            return (table_value.value, table_value.source_id) if table_value is not None else (None, "")
        if pool == "support_material_physchem":
            if entity.formula:
                value = formula_descriptor(entity.formula, desc)
                if value is not None and np.isfinite(value):
                    return value, "formula_programmatic"
            table_value = self.table_store.get_value(entity.entity_key, pool, desc)
            return (table_value.value, table_value.source_id) if table_value is not None else (None, "")
        table_value = self.table_store.get_value(entity.entity_key, pool, desc)
        return (table_value.value, table_value.source_id) if table_value is not None else (None, "")


def build_descriptor_feature_spec(
    *,
    problem_spec: dict[str, Any],
    selection_payload: dict[str, Any],
    registry: DescriptorRegistry | None = None,
) -> dict[str, Any]:
    reg = registry or DescriptorRegistry()
    dataset = dataset_key_from_problem(problem_spec)
    by_variable = selection_payload.get("selected_descriptors_by_variable") if isinstance(selection_payload, dict) else {}
    if not isinstance(by_variable, dict):
        by_variable = {}
    variable_features: dict[str, dict[str, Any]] = {}
    diagnostics: dict[str, Any] = {
        "selected_descriptors_by_variable": {},
        "descriptor_coverage_report": {},
        "descriptor_collision_report": {},
        "descriptor_sources": {},
        "deferred_descriptors_not_used": [],
        "warnings": list(selection_payload.get("warnings") or []) if isinstance(selection_payload, dict) else [],
    }

    for variable in categorical_descriptor_variables(problem_spec):
        name = str(variable.get("name") or "").strip()
        descriptor = dict(variable.get("descriptor") or {})
        selected = normalize_selected_descriptors(by_variable.get(name))
        if not selected:
            continue
        validate_selected_descriptors(
            selected_descriptors=selected,
            available_descriptors=descriptor.get("available_descriptors") or {},
            scale_types=reg.table_store.scale_types_for_available(descriptor.get("available_descriptors") or {}),
            allow_semichemical_ordinal=bool((descriptor.get("validation") or {}).get("allow_semichemical_ordinal", False)),
            max_selected=int(descriptor.get("max_selected_descriptors") or 5),
        )
        matrix = reg.build_matrix(dataset=dataset, variable=variable, selected_descriptors=selected)
        reg.validate_matrix(dataset=dataset, variable=variable, matrix=matrix)
        feature_map = reg.scaled_feature_map(matrix, include_present_mask=bool(descriptor.get("include_present_mask", False)))
        variable_features[name] = {
            "feature_map": feature_map,
            "descriptor_names": [f"{pool}.{desc}" for pool, desc in selected],
            "source": "descriptor_v2",
        }
        diagnostics["selected_descriptors_by_variable"][name] = [
            {"pool": pool, "name": desc}
            for pool, desc in selected
        ]
        diagnostics["descriptor_coverage_report"][name] = {
            "status": "ok",
            "domain_size": len(matrix.labels),
            "descriptor_count": len(matrix.descriptor_keys),
        }
        diagnostics["descriptor_collision_report"][name] = {
            "status": "ok",
            "warnings": matrix.metadata.get("collision_warnings", []),
        }
        diagnostics["descriptor_sources"][name] = matrix.metadata.get("sources", {})
        diagnostics["deferred_descriptors_not_used"].extend(matrix.metadata.get("deferred_descriptors_not_used", []))

    diagnostics["deferred_descriptors_not_used"] = sorted(set(diagnostics["deferred_descriptors_not_used"]))
    return {
        "variable_features": variable_features,
        "descriptor_diagnostics": diagnostics,
        "selection_payload": selection_payload,
    }


def build_descriptor_selection_or_empty_prompt(problem_spec: dict[str, Any]) -> str:
    return build_descriptor_selection_prompt(problem_spec)
