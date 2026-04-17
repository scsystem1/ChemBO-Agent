#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_PROBLEM="${ROOT_DIR}/examples/ocm_problem.yaml"
DEFAULT_CONFIG="${ROOT_DIR}/minimax_m25_ocm.yaml"
REPEATS="${REPEATS:-5}"
BUDGET="${BUDGET:-30}"
OUTPUT_DIR="${OUTPUT_DIR:-${ROOT_DIR}/outputs/ocm_5x_30iter}"
TASK_NAME_OVERRIDE="${TASK_NAME:-ocm_5x_30iter}"

PROBLEM_FILE="${1:-$DEFAULT_PROBLEM}"
CONFIG_FILE="${2:-$DEFAULT_CONFIG}"

if [[ ! -f "${PROBLEM_FILE}" ]]; then
  echo "Problem file not found: ${PROBLEM_FILE}" >&2
  exit 1
fi

if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "Config file not found: ${CONFIG_FILE}" >&2
  exit 1
fi

mkdir -p "${OUTPUT_DIR}"

SUMMARY_JSON="${OUTPUT_DIR}/run_summaries.json"
SUMMARY_CSV="${OUTPUT_DIR}/run_summaries.csv"

python - "${ROOT_DIR}" "${PROBLEM_FILE}" "${CONFIG_FILE}" "${OUTPUT_DIR}" "${TASK_NAME_OVERRIDE}" "${REPEATS}" "${BUDGET}" "${SUMMARY_JSON}" "${SUMMARY_CSV}" <<'PY'
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

root_dir = Path(sys.argv[1]).resolve()
problem_path = Path(sys.argv[2]).resolve()
config_path = Path(sys.argv[3]).resolve()
output_dir = Path(sys.argv[4]).resolve()
task_name_override = sys.argv[5].strip()
repeats = int(sys.argv[6])
budget = int(sys.argv[7])
summary_json_path = Path(sys.argv[8]).resolve()
summary_csv_path = Path(sys.argv[9]).resolve()

sys.path.insert(0, str(root_dir))

from config.settings import Settings
from core.campaign_runner import _default_run_id, run_campaign
from core.graph import build_chembo_graph
from core.problem_loader import load_problem_file
from core.state import create_initial_state


def _slugify(value: str) -> str:
    text = value.strip()
    if not text:
        return "unknown"
    normalized = []
    last_dash = False
    for char in text:
        if char.isalnum() or char in {".", "_", "-"}:
            normalized.append(char)
            last_dash = False
        elif not last_dash:
            normalized.append("-")
            last_dash = True
    result = "".join(normalized).strip("-._")
    return result or "unknown"


base_problem = load_problem_file(problem_path)
if not isinstance(base_problem, dict):
    raise RuntimeError("OCM batch script expects a structured YAML/JSON problem file.")

summaries: list[dict[str, object]] = []

for run_index in range(1, repeats + 1):
    run_id = f"run{run_index:02d}"
    print("============================================================")
    print(f"OCM repeat {run_index}/{repeats}")
    print(f"Problem file: {problem_path}")
    print(f"Config file: {config_path}")
    print(f"Output root: {output_dir}")
    print(f"Budget override: {budget}")
    print(f"Run id: {run_id}")
    print("============================================================")

    settings = Settings.from_yaml(str(config_path)) if config_path.exists() else Settings()
    settings.max_bo_iterations = budget
    settings.output_dir = str(output_dir)
    settings.experiment_name = _slugify(task_name_override or problem_path.stem)
    settings.experiment_id = run_id

    problem = json.loads(json.dumps(base_problem))
    problem["budget"] = budget

    graph = build_chembo_graph(settings)
    initial_state = create_initial_state(problem, settings, problem_source_path=str(problem_path))
    resolved_run_id = _default_run_id(initial_state, settings)
    state = run_campaign(
        graph,
        initial_state,
        settings,
        thread_id=resolved_run_id,
        printer=print,
    )

    summary = {
        "run_index": run_index,
        "run_id": resolved_run_id,
        "budget": budget,
        "best_result": state.get("best_result"),
        "best_candidate": state.get("best_candidate"),
        "proposal_strategy": (state.get("final_summary") or {}).get("proposal_strategy"),
        "stop_reason": (state.get("final_summary") or {}).get("stop_reason"),
        "output_dir": str(output_dir / resolved_run_id),
    }
    summaries.append(summary)
    print("Run summary:")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

summary_json_path.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")

with summary_csv_path.open("w", encoding="utf-8", newline="") as handle:
    writer = csv.DictWriter(
        handle,
        fieldnames=[
            "run_index",
            "run_id",
            "budget",
            "best_result",
            "proposal_strategy",
            "stop_reason",
            "output_dir",
        ],
    )
    writer.writeheader()
    for item in summaries:
        writer.writerow({key: item.get(key) for key in writer.fieldnames})

print("============================================================")
print("Batch summary written:")
print(summary_json_path)
print(summary_csv_path)
print("============================================================")
PY
