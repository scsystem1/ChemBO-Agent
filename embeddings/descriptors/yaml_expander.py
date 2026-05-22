from __future__ import annotations

from typing import Any

from .table_store import DescriptorTableStore
from .validation import validate_descriptor_name


def dataset_key_from_problem(problem_spec: dict[str, Any]) -> str:
    reaction = str(problem_spec.get("reaction_type") or problem_spec.get("reaction", {}).get("family") or "").strip()
    return reaction.lower() or "unknown"


def categorical_descriptor_variables(problem_spec: dict[str, Any]) -> list[dict[str, Any]]:
    variables = []
    for variable in problem_spec.get("variables", []) or []:
        if not isinstance(variable, dict):
            continue
        if variable.get("type", "categorical") == "continuous":
            continue
        descriptor = variable.get("descriptor")
        if isinstance(descriptor, dict) and bool(descriptor.get("enabled", False)):
            variables.append(variable)
    return variables


def expand_problem_descriptors(
    problem_spec: dict[str, Any],
    *,
    table_store: DescriptorTableStore | None = None,
) -> dict[str, Any]:
    store = table_store or DescriptorTableStore()
    dataset = dataset_key_from_problem(problem_spec)
    variables = []
    for variable in categorical_descriptor_variables(problem_spec):
        descriptor = dict(variable.get("descriptor") or {})
        available = {
            str(pool): [str(name) for name in (names or [])]
            for pool, names in (descriptor.get("available_descriptors") or {}).items()
        }
        for pool, names in available.items():
            for name in names:
                validate_descriptor_name(name, store.scale_type(pool, name))
        variables.append(
            {
                "name": variable.get("name"),
                "role": variable.get("role", "other"),
                "entity_kind": descriptor.get("entity_kind"),
                "domain_values": [str(item) for item in variable.get("domain", [])],
                "allow_absent_values": list(descriptor.get("allow_absent_values") or []),
                "max_selected_descriptors": int(descriptor.get("max_selected_descriptors") or 5),
                "candidate_pools": list(descriptor.get("candidate_pools") or []),
                "available_descriptors": available,
                "scale_types": {
                    f"{pool}.{name}": store.scale_type(pool, name)
                    for pool, names in available.items()
                    for name in names
                },
                "validation": dict(descriptor.get("validation") or {}),
            }
        )
    return {"dataset": dataset, "variables": variables}

