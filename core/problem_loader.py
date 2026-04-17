"""
Problem loading helpers for raw-text and structured benchmark definitions.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Any

import yaml


VALID_VARIABLE_ROLES = {
    "ligand",
    "base",
    "solvent",
    "catalyst_precursor",
    "temperature",
    "concentration",
    "additive",
    "electrophile",
    "nucleophile",
    "oxidant",
    "reductant",
    "support",
    "metal_primary",
    "metal_promoter",
    "metal_selector",
    "contact_time",
    "flow_rate",
    "pressure",
    "other",
}

VALID_RETRIEVAL_GOALS = {"precedent", "mechanism", "property"}
VALID_RETRIEVAL_SOURCES = {"ord", "reviews", "textbooks", "supplementary"}
VALID_QUERY_MODES = {"optimize_conditions", "understand_mechanism", "survey"}
_WINDOWS_ABS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


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
        normalized["role"] = _normalize_role(
            normalized.get("role"),
            name=str(normalized.get("name", "")),
            var_type=str(normalized.get("type", "categorical")),
        )
        normalized_variables.append(normalized)
    if normalized_variables:
        spec["variables"] = normalized_variables

    reaction = spec.get("reaction")
    if isinstance(reaction, dict):
        reaction_spec = deepcopy(reaction)
    else:
        reaction_spec = {}
    reaction_spec.setdefault("family", str(spec.get("reaction_type", "")).strip().upper())
    reaction_spec.setdefault("reaction_smiles", "")
    reaction_spec["substrates"] = _normalize_substrates(reaction_spec.get("substrates", []))
    reaction_spec.setdefault("product_smiles", "")
    reaction_spec["known_fixed_context"] = _normalize_fixed_context(reaction_spec.get("known_fixed_context", []))
    spec["reaction"] = reaction_spec

    retrieval = spec.get("retrieval")
    if isinstance(retrieval, dict):
        retrieval_spec = deepcopy(retrieval)
    else:
        retrieval_spec = {}
    retrieval_spec["goals"] = _normalize_retrieval_values(
        retrieval_spec.get("goals", ["precedent", "mechanism"]),
        allowed=VALID_RETRIEVAL_GOALS,
        default=["precedent", "mechanism"],
    )
    retrieval_spec["must_match_roles"] = _normalize_role_list(
        retrieval_spec.get("must_match_roles", ["reaction_family"])
    ) or ["reaction_family"]
    retrieval_spec["prefer_sources"] = _normalize_retrieval_values(
        retrieval_spec.get("prefer_sources", ["ord", "reviews"]),
        allowed=VALID_RETRIEVAL_SOURCES,
        default=["ord", "reviews"],
    )
    query_mode = str(retrieval_spec.get("query_mode", "optimize_conditions")).strip().lower()
    retrieval_spec["query_mode"] = query_mode if query_mode in VALID_QUERY_MODES else "optimize_conditions"
    spec["retrieval"] = retrieval_spec

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


def resolve_campaign_budget(problem_spec: dict[str, Any] | None, settings) -> int:
    """Resolve the authoritative experiment budget for a campaign."""
    if isinstance(problem_spec, dict):
        budget = problem_spec.get("budget")
        try:
            if budget is not None:
                resolved = int(budget)
                if resolved > 0:
                    return resolved
        except (TypeError, ValueError):
            pass
    fallback = int(getattr(settings, "max_bo_iterations", 30))
    return fallback if fallback > 0 else 30


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
    text_value = str(path_value).strip()
    if _looks_like_windows_absolute_path(text_value):
        remapped = _remap_windows_dataset_path(text_value, source_path)
        if remapped is not None:
            return remapped
    candidate = Path(path_value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    if source_path is not None:
        base_path = Path(source_path)
        base_dir = base_path if base_path.is_dir() else base_path.parent
        return (base_dir / candidate).resolve()
    return candidate.resolve()


def _looks_like_windows_absolute_path(path_value: str) -> bool:
    value = str(path_value or "").strip()
    return bool(value and (_WINDOWS_ABS_PATH_RE.match(value) or value.startswith("\\\\")))


def _remap_windows_dataset_path(path_value: str, source_path: str | Path | None) -> Path | None:
    """
    Convert Windows-style absolute dataset paths into local project paths when possible.

    Example:
    E:\\science\\BO\\LLM_BO\\ChemBO-Agent\\data\\DAR.csv
    -> <local ...>/ChemBO-Agent/data/DAR.csv
    """
    normalized = PureWindowsPath(path_value).as_posix()
    marker = "/ChemBO-Agent/"
    if marker not in normalized:
        return None

    if source_path is None:
        return None

    base_path = Path(source_path)
    current = base_path if base_path.is_dir() else base_path.parent
    chembo_root: Path | None = None
    for parent in [current, *current.parents]:
        if parent.name == "ChemBO-Agent":
            chembo_root = parent
            break
    if chembo_root is None:
        return None

    relative_tail = normalized.split(marker, 1)[1].lstrip("/")
    if not relative_tail:
        return None
    return (chembo_root / relative_tail).resolve()


def _infer_role_from_name(name: str, var_type: str) -> str:
    lower = str(name or "").strip().lower()
    if "ligand" in lower:
        return "ligand"
    if "base" in lower:
        return "base"
    if "solvent" in lower:
        return "solvent"
    if "catalyst" in lower:
        return "catalyst_precursor"
    if "temp" in lower:
        return "temperature"
    if "conc" in lower:
        return "concentration"
    if "support" in lower:
        return "support"
    if lower == "m1":
        return "metal_primary"
    if lower == "m2":
        return "metal_promoter"
    if lower == "m3":
        return "metal_selector"
    if lower == "ct" or "contact" in lower:
        return "contact_time"
    if "flow" in lower:
        return "flow_rate"
    if str(var_type or "").lower() == "continuous" and "pressure" in lower:
        return "pressure"
    return "other"


def _normalize_role(raw_role: Any, *, name: str, var_type: str) -> str:
    cleaned = str(raw_role or "").strip().lower()
    if cleaned in VALID_VARIABLE_ROLES:
        return cleaned
    return _infer_role_from_name(name, var_type)


def _normalize_role_list(values: Any) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    raw_values = values if isinstance(values, list) else [values]
    for raw in raw_values:
        cleaned = str(raw or "").strip().lower()
        if not cleaned:
            continue
        if cleaned in VALID_VARIABLE_ROLES or cleaned == "reaction_family":
            if cleaned not in seen:
                normalized.append(cleaned)
                seen.add(cleaned)
    return normalized


def _normalize_retrieval_values(values: Any, *, allowed: set[str], default: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    raw_values = values if isinstance(values, list) else [values]
    for raw in raw_values:
        cleaned = str(raw or "").strip().lower()
        if cleaned in allowed and cleaned not in seen:
            normalized.append(cleaned)
            seen.add(cleaned)
    return normalized or list(default)


def _normalize_substrates(values: Any) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict):
            continue
        role = _normalize_role(item.get("role"), name=str(item.get("name", "")), var_type="categorical")
        normalized.append(
            {
                "role": role,
                "name": str(item.get("name", "")).strip(),
                "smiles": str(item.get("smiles", "")).strip(),
            }
        )
    return normalized


def _normalize_fixed_context(values: Any) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict):
            continue
        role = _normalize_role(item.get("role"), name=str(item.get("value", "")), var_type="categorical")
        value = str(item.get("value", "")).strip()
        if role and value:
            normalized.append({"role": role, "value": value})
    return normalized
