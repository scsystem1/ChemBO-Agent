#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_PROBLEM="${ROOT_DIR}/examples/dar_problem.yaml"
DEFAULT_CONFIG="${ROOT_DIR}/dashscope_kimi.yaml"

PROBLEM_FILE="${1:-$DEFAULT_PROBLEM}"
CONFIG_FILE="${2:-$DEFAULT_CONFIG}"
REPEATS="${REPEATS:-2}"
OUTPUT_DIR="${OUTPUT_DIR:-${ROOT_DIR}/outputs}"
TASK_NAME_OVERRIDE="${TASK_NAME:-}"
TASK_TYPE_OVERRIDE="${TASK_TYPE:-}"

if [[ ! -f "${PROBLEM_FILE}" ]]; then
  echo "Problem file not found: ${PROBLEM_FILE}" >&2
  exit 1
fi

if [[ -n "${CONFIG_FILE}" && ! -f "${CONFIG_FILE}" ]]; then
  echo "Config file not found: ${CONFIG_FILE}" >&2
  exit 1
fi

mkdir -p "${OUTPUT_DIR}"

for run_index in $(seq 1 "${REPEATS}"); do
  run_id="$(printf 'run%02d' "${run_index}")"
  echo "============================================================"
  echo "BO-enabled ablation repeat ${run_index}/${REPEATS}"
  echo "Problem file: ${PROBLEM_FILE}"
  echo "Config file: ${CONFIG_FILE}"
  echo "Output root: ${OUTPUT_DIR}"
  echo "Run id: ${run_id}"
  echo "============================================================"

  python - "${ROOT_DIR}" "${PROBLEM_FILE}" "${CONFIG_FILE}" "${OUTPUT_DIR}" "${TASK_NAME_OVERRIDE}" "${TASK_TYPE_OVERRIDE}" "${run_id}" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

root_dir = Path(sys.argv[1]).resolve()
problem_path = Path(sys.argv[2]).resolve()
config_path = Path(sys.argv[3]).resolve()
output_dir = Path(sys.argv[4]).resolve()
task_name_override = sys.argv[5].strip()
task_type_override = sys.argv[6].strip()
run_id = sys.argv[7].strip()

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


settings = Settings.from_yaml(str(config_path)) if config_path.exists() else Settings()
problem = load_problem_file(problem_path)

task_name = task_name_override or problem_path.stem
if task_type_override:
    task_type = task_type_override
elif isinstance(problem, dict):
    task_type = str(problem.get("reaction_type") or ("dataset_problem" if isinstance(problem.get("dataset"), dict) else "text_problem"))
else:
    task_type = "text_problem"

settings.ablation_pure_reasoning = False
settings.output_dir = str(output_dir)
settings.experiment_name = _slugify(task_name)
settings.experiment_id = _slugify(run_id)

graph = build_chembo_graph(settings)
initial_state = create_initial_state(problem, settings, problem_source_path=str(problem_path))
resolved_run_id = _default_run_id(initial_state, settings)
if task_type_override:
    parts = resolved_run_id.rsplit("_", 2)
    if len(parts) == 3:
        resolved_run_id = f"{parts[0]}_{_slugify(task_type)}_{parts[2]}"
state = run_campaign(
    graph,
    initial_state,
    settings,
    thread_id=resolved_run_id,
    printer=print,
)

summary = {
    "run_id": resolved_run_id,
    "best_result": state.get("best_result"),
    "best_candidate": state.get("best_candidate"),
    "proposal_strategy": (state.get("final_summary") or {}).get("proposal_strategy"),
    "output_dir": str(output_dir / resolved_run_id),
}
print("Run summary:")
print(json.dumps(summary, ensure_ascii=False, indent=2))
PY
done
