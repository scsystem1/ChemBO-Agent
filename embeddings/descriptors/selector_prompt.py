from __future__ import annotations

import json
from typing import Any

from .yaml_expander import expand_problem_descriptors


def build_descriptor_selection_prompt(problem_spec: dict[str, Any]) -> str:
    expanded = expand_problem_descriptors(problem_spec)
    if not expanded.get("variables"):
        return ""
    return f"""You are selecting chemically meaningful numeric descriptors for BO representations.

Use only descriptors declared in available_descriptors. Do not invent descriptors or values.
Prefer continuous physical/computed/experimental quantities, then chemically defined counts/states/charges.
Avoid redundant descriptors such as selecting both MolWt and ExactMolWt unless needed.
Use ordinal_semichemical descriptors only as fallback and only when the variable allows them.
For OCM elements, prefer oxide/redox descriptors when available. For ligands, prefer TEP, %Vbur, cone/bite angle when source-covered.
For supports, do not pretend SiC and SiCnf are distinguished unless selected numeric morphology/surface descriptors cover them.

Descriptor request:
{json.dumps(expanded, ensure_ascii=False, indent=2)}

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

