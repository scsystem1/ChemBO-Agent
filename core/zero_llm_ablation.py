"""
Helpers for the zero-LLM fixed-warm-start AutoBO ablation.
"""
import csv
import json
from pathlib import Path
from typing import Any


def zero_llm_ablation_enabled(settings) -> bool:
    return bool(getattr(settings, "zero_llm_ablation_enabled", False))


def resolve_zero_llm_fixed_warm_start(settings) -> list[dict[str, Any]]:
    warm_start_size = max(0, int(getattr(settings, "initial_doe_size", 20) or 20))
    inline_records = getattr(settings, "zero_llm_fixed_warm_start_records", None)
    if inline_records is not None:
        return normalize_zero_llm_warm_start_records(inline_records)

    manifest_path = str(getattr(settings, "zero_llm_fixed_warm_start_manifest_path", "") or "").strip()
    if manifest_path:
        payload = json.loads(Path(manifest_path).expanduser().resolve().read_text(encoding="utf-8"))
        manifest_key = str(getattr(settings, "zero_llm_fixed_warm_start_manifest_key", "") or "").strip()
        if manifest_key:
            if not isinstance(payload, dict) or manifest_key not in payload:
                raise ValueError(
                    f"Zero-LLM warm-start manifest {manifest_path} does not contain key '{manifest_key}'."
                )
            payload = payload[manifest_key]
        if isinstance(payload, dict) and isinstance(payload.get("records"), list):
            payload = payload["records"]
        if not isinstance(payload, list):
            raise ValueError(f"Zero-LLM warm-start manifest {manifest_path} must resolve to a list of records.")
        return normalize_zero_llm_warm_start_records(payload)

    source_dir = str(getattr(settings, "zero_llm_fixed_warm_start_source_dir", "") or "").strip()
    if source_dir:
        run_id = str(getattr(settings, "experiment_id", "") or "").strip()
        if not run_id:
            raise ValueError("Zero-LLM warm-start loading from source_dir requires settings.experiment_id to be set.")
        return load_zero_llm_warm_start_records(source_dir, run_id=run_id, warm_start_size=warm_start_size)

    raise ValueError(
        "Zero-LLM ablation requires fixed warm-start records, a manifest path, or a historical source directory."
    )


def load_zero_llm_warm_start_records(
    history_root: str | Path,
    *,
    run_id: str,
    warm_start_size: int = 20,
) -> list[dict[str, Any]]:
    run_dir = locate_zero_llm_history_run_dir(history_root, run_id=run_id)
    final_state_path = run_dir / "final_state.json"
    if not final_state_path.exists():
        raise FileNotFoundError(f"Expected final_state.json in historical run directory: {run_dir}")
    payload = json.loads(final_state_path.read_text(encoding="utf-8"))
    return extract_zero_llm_warm_start_records_from_final_state(
        payload,
        warm_start_size=warm_start_size,
        source_run_id=run_id,
    )


def locate_zero_llm_history_run_dir(history_root: str | Path, *, run_id: str) -> Path:
    root = Path(history_root).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"Historical warm-start root does not exist: {root}")
    if root.is_file():
        raise ValueError(f"Historical warm-start root must be a directory, got file: {root}")

    normalized_run_id = str(run_id).strip()
    candidates = sorted(
        path for path in root.iterdir()
        if path.is_dir() and path.name.endswith(f"_{normalized_run_id}")
    )
    if len(candidates) != 1:
        raise ValueError(
            f"Expected exactly one historical run directory ending with _{normalized_run_id} under {root}, "
            f"found {len(candidates)}."
        )
    return candidates[0]


def extract_zero_llm_warm_start_records_from_final_state(
    payload: dict[str, Any],
    *,
    warm_start_size: int = 20,
    source_run_id: str = "",
) -> list[dict[str, Any]]:
    observations = payload.get("observations", [])
    if not isinstance(observations, list):
        raise ValueError("Historical final_state payload is missing a valid observations list.")

    selected = [
        observation
        for observation in observations
        if str(((observation.get("metadata") or {}).get("selection_source") or "")).strip() == "warm_start_queue"
    ]
    if len(selected) < int(warm_start_size):
        raise ValueError(
            f"Historical warm-start extraction expected at least {warm_start_size} warm_start_queue observations, "
            f"found {len(selected)}."
        )
    return normalize_zero_llm_warm_start_records(
        selected[: int(warm_start_size)],
        source_run_id=source_run_id,
    )


def normalize_zero_llm_warm_start_records(
    records: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    source_run_id: str = "",
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, raw_record in enumerate(records, start=1):
        if not isinstance(raw_record, dict):
            raise ValueError(f"Warm-start record #{index} is not a dictionary.")
        candidate = raw_record.get("candidate", raw_record)
        if not isinstance(candidate, dict) or not candidate:
            raise ValueError(f"Warm-start record #{index} is missing a valid candidate payload.")
        metadata = raw_record.get("metadata", {}) if isinstance(raw_record.get("metadata"), dict) else {}
        normalized.append(
            {
                "candidate": dict(candidate),
                "predicted_value": None,
                "uncertainty": None,
                "acquisition_value": None,
                "acquisition_value_raw": None,
                "selection_step": index,
                "selection_mode": "historical_fixed_warm_start",
                "constraint_violations": [],
                "constraint_satisfied": True,
                "autobo_rank": index,
                "warm_start_category": "historical_fixed",
                "warm_start_rationale": (
                    f"Loaded from historical warm-start seed {source_run_id}."
                    if source_run_id
                    else "Loaded from a historical warm-start seed."
                ),
                "source_run_id": source_run_id or None,
                "source_observation_iteration": raw_record.get("iteration"),
                "source_selection_source": metadata.get("selection_source", "warm_start_queue"),
                "source_dataset_row_id": metadata.get("dataset_row_id"),
            }
        )
    return normalized


def write_zero_llm_combined_experiment_csv(
    output_path: str | Path,
    run_entries: list[dict[str, Any]],
) -> Path:
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = ["domain", "run_id"]
    rows: list[dict[str, str]] = []
    for entry in run_entries:
        domain = str(entry.get("domain") or "").strip()
        run_id = str(entry.get("run_id") or "").strip()
        csv_path = Path(str(entry.get("experiment_csv_path") or "")).expanduser().resolve()
        with csv_path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            source_fieldnames = list(reader.fieldnames or [])
            for name in source_fieldnames:
                if name not in fieldnames:
                    fieldnames.append(name)
            for raw_row in reader:
                row = {"domain": domain, "run_id": run_id}
                row.update({key: str(value or "") for key, value in raw_row.items()})
                rows.append(row)

    with output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})
    return output


def load_zero_llm_manifest(manifest_path: str | Path) -> dict[str, Any]:
    path = Path(manifest_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Zero-LLM warm-start manifest does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Zero-LLM warm-start manifest must be a JSON object: {path}")
    return payload


def manifest_records_for_domain_run(
    manifest: dict[str, Any],
    *,
    domain: str,
    run_id: str,
) -> list[dict[str, Any]]:
    domain_payload = manifest.get(domain)
    if not isinstance(domain_payload, dict):
        raise KeyError(f"Zero-LLM warm-start manifest is missing domain '{domain}'.")
    run_payload = domain_payload.get(run_id)
    if not isinstance(run_payload, dict):
        raise KeyError(f"Zero-LLM warm-start manifest is missing run '{domain}.{run_id}'.")
    records = run_payload.get("records")
    if not isinstance(records, list):
        raise ValueError(f"Zero-LLM warm-start manifest entry '{domain}.{run_id}' is missing a records list.")
    return normalize_zero_llm_warm_start_records(records, source_run_id=run_id)
