"""
Problem loading helpers for raw-text and structured benchmark definitions.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


def load_problem_file(path: str | Path) -> str | dict[str, Any]:
    """Load a problem file, preserving structured YAML/JSON specs when available."""
    source_path = Path(path).expanduser().resolve()
    raw_text = source_path.read_text(encoding="utf-8")
    parsed = _parse_problem_text(raw_text, source_path.suffix.lower())
    if isinstance(parsed, dict):
        return normalize_problem_spec(parsed, source_path)
    return raw_text


def normalize_problem_spec(problem_input: dict[str, Any], source_path: str | Path | None = None) -> dict[str, Any]:
    """Normalize a structured problem spec and resolve relative dataset paths."""
    spec = deepcopy(problem_input)
    if "problem" in spec and isinstance(spec["problem"], dict):
        spec = deepcopy(spec["problem"])

    description = str(spec.get("description") or spec.get("raw_description") or "").strip()
    if description:
        spec["description"] = description
        spec["raw_description"] = description
    else:
        spec.setdefault("raw_description", "")

    dataset = spec.get("dataset")
    if isinstance(dataset, dict):
        dataset_spec = deepcopy(dataset)
        csv_path = dataset_spec.get("csv_path")
        if csv_path:
            dataset_spec["csv_path"] = str(_resolve_optional_path(csv_path, source_path))
        spec["dataset"] = dataset_spec

    normalized_variables = []
    for variable in spec.get("variables", []):
        if not isinstance(variable, dict):
            continue
        normalized = deepcopy(variable)
        smiles_map = {str(k): str(v) for k, v in normalized.get("smiles_map", {}).items()}
        if "smiles" in normalized and isinstance(normalized["smiles"], dict):
            smiles_map.update({str(k): str(v) for k, v in normalized["smiles"].items()})
        if "smiles" in str(normalized.get("name", "")).lower() and not smiles_map:
            for entry in normalized.get("domain", []):
                if isinstance(entry, dict):
                    label = str(entry.get("label") or entry.get("name") or entry.get("value") or "")
                    smiles = str(entry.get("smiles") or "")
                    if label and smiles:
                        smiles_map[label] = smiles
                else:
                    label = str(entry)
                    smiles_map[label] = label
        if smiles_map:
            normalized["smiles_map"] = smiles_map
        normalized_variables.append(normalized)
    if normalized_variables:
        spec["variables"] = normalized_variables

    return spec


def has_structured_problem_spec(problem_spec: dict[str, Any]) -> bool:
    """Return True when the incoming spec is already decision-complete for the graph."""
    if not isinstance(problem_spec, dict):
        return False
    variables = problem_spec.get("variables")
    if not isinstance(variables, list) or not variables:
        return False
    return any(
        key in problem_spec
        for key in ("reaction_type", "target_metric", "optimization_direction", "budget", "dataset")
    )


def problem_preview(problem_input: str | dict[str, Any]) -> str:
    """Create a short human-readable preview for CLI output."""
    if isinstance(problem_input, dict):
        description = str(problem_input.get("description") or problem_input.get("raw_description") or "").strip()
        if description:
            return description
        reaction_type = str(problem_input.get("reaction_type", "")).strip()
        target_metric = str(problem_input.get("target_metric", "")).strip()
        if reaction_type or target_metric:
            return f"{reaction_type} optimization for {target_metric}".strip()
        return json.dumps(problem_input, ensure_ascii=False)
    return str(problem_input)


def _parse_problem_text(raw_text: str, suffix: str) -> Any:
    if suffix == ".json":
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            return raw_text
    if suffix in {".yaml", ".yml"}:
        try:
            parsed = yaml.safe_load(raw_text)
        except yaml.YAMLError:
            return raw_text
        return parsed if isinstance(parsed, dict) else raw_text

    try:
        parsed = yaml.safe_load(raw_text)
    except yaml.YAMLError:
        return raw_text
    return parsed if isinstance(parsed, dict) else raw_text


def _resolve_optional_path(path_value: str | Path, source_path: str | Path | None) -> Path:
    candidate = Path(path_value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    if source_path is not None:
        base_path = Path(source_path)
        base_dir = base_path if base_path.is_dir() else base_path.parent
        return (base_dir / candidate).resolve()
    return candidate.resolve()
