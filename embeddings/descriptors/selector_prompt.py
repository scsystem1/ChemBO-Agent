from __future__ import annotations

import json
from typing import Any

from .table_store import DescriptorTableStore
from .yaml_expander import expand_problem_descriptors


def build_compact_descriptor_context(problem_spec: dict[str, Any]) -> dict[str, Any]:
    expanded = expand_problem_descriptors(problem_spec)
    store = DescriptorTableStore()
    reaction = problem_spec.get("reaction", {}) if isinstance(problem_spec.get("reaction"), dict) else {}
    fixed_context = reaction.get("known_fixed_context", []) if isinstance(reaction, dict) else []
    context: dict[str, Any] = {
        "problem": {
            "reaction_type": problem_spec.get("reaction_type") or reaction.get("family", ""),
            "target_metric": problem_spec.get("target_metric", ""),
            "optimization_direction": problem_spec.get("optimization_direction", "maximize"),
            "description": str(problem_spec.get("description") or problem_spec.get("raw_description") or "")[:700],
            "fixed_reaction_context": fixed_context[:8] if isinstance(fixed_context, list) else [],
        },
        "variables": [],
    }
    variables_by_name = {
        str(variable.get("name") or ""): variable
        for variable in problem_spec.get("variables", []) or []
        if isinstance(variable, dict)
    }
    for item in expanded.get("variables", []) or []:
        name = str(item.get("name") or "")
        variable = variables_by_name.get(name, {})
        descriptors = []
        available = item.get("available_descriptors", {}) if isinstance(item.get("available_descriptors"), dict) else {}
        for pool, names in available.items():
            for descriptor_name in names or []:
                spec = store.manifest.get((str(pool), str(descriptor_name)))
                descriptors.append(
                    {
                        "id": f"{pool}.{descriptor_name}",
                        "meaning": spec.description if spec is not None and spec.description else str(descriptor_name),
                    }
                )
        context["variables"].append(
            {
                "variable": name,
                "role": item.get("role", variable.get("role", "other")),
                "entity_kind": item.get("entity_kind"),
                "description": str(variable.get("description") or "")[:300],
                "domain_values": list(item.get("domain_values") or [])[:40],
                "max_selected_descriptors": int(item.get("max_selected_descriptors") or 3),
                "available_descriptors": descriptors,
            }
        )
    return context


def build_descriptor_selection_prompt(problem_spec: dict[str, Any]) -> str:
    compact = build_compact_descriptor_context(problem_spec)
    if not compact.get("variables"):
        return ""
    return f"""You are selecting compact physicochemical descriptor schemas for Bayesian optimization.

Choose 1 to 3 descriptors for each descriptor-enabled categorical variable.

Rules:
- Use only descriptor IDs listed under available_descriptors.
- A descriptor ID has format "pool.name".
- Do not invent descriptors or numeric values.
- All values within the same variable must share the same selected descriptor columns.
- Prefer mechanistically relevant, non-redundant physicochemical quantities.
- Select only descriptors that are plausibly relevant to the reaction result and useful for BO generalization.
- Do not pad to 3 descriptors with weak, redundant, or low-relevance quantities.
- Avoid pure identity/category labels.
- If one descriptor is clearly the only useful signal for a variable, select only that one.
- For OCM supports, prefer point_of_zero_charge_pH and band_gap_eV; point_of_zero_charge_pH distinguishes SiC from SiCnf.

Problem, variables, and available descriptors:
{json.dumps(compact, ensure_ascii=False, indent=2)}

Return strict JSON:
{{
  "selected_descriptors_by_variable": {{
    "variable_name": [
      {{"pool": "rdkit_2d", "name": "MolLogP"}}
    ]
  }},
  "rationales": {{
    "variable_name": "one-line rationale"
  }},
  "warnings": []
}}"""
