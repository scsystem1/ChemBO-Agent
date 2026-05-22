from __future__ import annotations

import json
from typing import Any

from .selector_prompt import build_compact_descriptor_context


def _schema_summary(schema: dict[str, Any]) -> dict[str, Any]:
    selected = schema.get("selected_descriptors_by_variable") if isinstance(schema, dict) else {}
    rationales = schema.get("rationales") if isinstance(schema, dict) else {}
    return {
        "selected_descriptors_by_variable": selected if isinstance(selected, dict) else {},
        "rationales": rationales if isinstance(rationales, dict) else {},
        "warnings": list(schema.get("warnings") or []) if isinstance(schema, dict) else [],
    }


def build_descriptor_audit_prompt(
    *,
    problem_spec: dict[str, Any],
    active_schema: dict[str, Any],
    descriptor_diagnostics: dict[str, Any],
    optimization_summary: dict[str, Any],
    model_diagnostics: dict[str, Any],
) -> str:
    compact = build_compact_descriptor_context(problem_spec)
    if not compact.get("variables"):
        return ""
    audit_context = {
        "current_schema": _schema_summary(active_schema),
        "optimization_summary": optimization_summary,
        "model_diagnostics": model_diagnostics,
        "descriptor_diagnostics": {
            "status": descriptor_diagnostics.get("status"),
            "errors": descriptor_diagnostics.get("errors") or descriptor_diagnostics.get("error"),
            "collision_report": descriptor_diagnostics.get("descriptor_collision_report", {}),
            "deferred_or_missing": descriptor_diagnostics.get("deferred_descriptors_not_used", []),
            "warnings": descriptor_diagnostics.get("warnings", []),
        },
        "available_alternatives": compact,
    }
    return f"""You are auditing the descriptor schema used by a Bayesian optimization campaign.

Decide whether to keep the current schema or propose one challenger schema.

Rules:
- Prefer keep_current unless there is a clear representation issue.
- If proposing a challenger, make minimal changes.
- Change at most 1 descriptor per variable unless there is a strong reason.
- Keep 3-5 descriptors per variable.
- Use only descriptor IDs listed under available_alternatives/current descriptors.
- Do not invent descriptors or numeric values.
- AutoBO will decide whether to switch; you only propose.

Audit context:
{json.dumps(audit_context, ensure_ascii=False, indent=2)}

Return strict JSON:
{{
  "decision": "keep_current",
  "selected_descriptors_by_variable": {{
    "variable_name": [
      {{"pool": "pool_name", "name": "descriptor_name"}}
    ]
  }},
  "rationales": {{
    "variable_name": "one concise sentence"
  }},
  "warnings": []
}}

If decision is "propose_challenger", selected_descriptors_by_variable must be a complete challenger schema, not a diff.
If decision is "keep_current", selected_descriptors_by_variable may be empty or match the current schema."""
