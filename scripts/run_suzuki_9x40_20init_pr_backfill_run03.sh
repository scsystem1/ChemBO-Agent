#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_PROBLEM="${ROOT_DIR}/examples/suzuki_problem.yaml"
DEFAULT_CONFIG="${ROOT_DIR}/dashscope_kimi.yaml"
DEFAULT_MODE_OUTPUT_DIR="${ROOT_DIR}/outputs/suzuki_9x40_20init_pr_ensemble_comparison/pure_reasoning_no_ensemble"
DEFAULT_EXPERIMENT_NAME="suzuki_9x40_20init_pr_ensemble_comparison_pure_reasoning_no_ensemble"
RUN_ID="${RUN_ID:-run03}"
RUN_SEED="${RUN_SEED:-20262423}"
BUDGET="${BUDGET:-40}"
WARM_START="${WARM_START:-20}"
MODE_OUTPUT_DIR="${OUTPUT_DIR:-${DEFAULT_MODE_OUTPUT_DIR}}"
EXPERIMENT_NAME="${TASK_NAME:-${DEFAULT_EXPERIMENT_NAME}}"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON_CMD=("${PYTHON_BIN}")
elif [[ "${CONDA_DEFAULT_ENV:-}" == "chembo" && -n "${CONDA_PREFIX:-}" ]]; then
  PYTHON_CMD=("${CONDA_PREFIX}/bin/python")
elif [[ -x "/Users/stevens/anaconda3/envs/chembo/bin/python" ]]; then
  PYTHON_CMD=("/Users/stevens/anaconda3/envs/chembo/bin/python")
else
  PYTHON_CMD=("python")
fi

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

mkdir -p "${MODE_OUTPUT_DIR}"

"${PYTHON_CMD[@]}" - "${ROOT_DIR}" "${PROBLEM_FILE}" "${CONFIG_FILE}" "${MODE_OUTPUT_DIR}" "${EXPERIMENT_NAME}" "${RUN_ID}" "${RUN_SEED}" "${BUDGET}" "${WARM_START}" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

root_dir = Path(sys.argv[1]).resolve()
problem_path = Path(sys.argv[2]).resolve()
config_path = Path(sys.argv[3]).resolve()
mode_output_dir = Path(sys.argv[4]).resolve()
experiment_name = sys.argv[5].strip()
run_id = sys.argv[6].strip()
run_seed = int(sys.argv[7])
budget = int(sys.argv[8])
warm_start = int(sys.argv[9])

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
    raise RuntimeError("Suzuki backfill script expects a structured YAML/JSON problem file.")

settings = Settings.from_yaml(str(config_path)) if config_path.exists() else Settings()
settings.max_bo_iterations = budget
settings.initial_doe_size = warm_start
settings.random_seed = run_seed
settings.pure_reasoning_ablation_enabled = True
settings.ensemble_af = False
settings.output_dir = str(mode_output_dir)
settings.experiment_name = _slugify(experiment_name)
settings.experiment_id = run_id

problem = json.loads(json.dumps(base_problem))
problem["budget"] = budget

graph = build_chembo_graph(settings)
initial_state = create_initial_state(problem, settings, problem_source_path=str(problem_path))
resolved_run_id = _default_run_id(initial_state, settings)
target_dir = mode_output_dir / resolved_run_id

if target_dir.exists():
    raise RuntimeError(
        f"Target output directory already exists: {target_dir}\n"
        "Refusing to overwrite. Move/delete it first or change RUN_ID/OUTPUT_DIR."
    )

print("============================================================")
print("Suzuki pure-reasoning backfill run")
print(f"Problem file: {problem_path}")
print(f"Config file: {config_path}")
print(f"Output root: {mode_output_dir}")
print(f"Target run id: {resolved_run_id}")
print(f"Run seed: {run_seed}")
print(f"Budget: {budget}")
print(f"Warm start: {warm_start}")
print("Settings override: pure_reasoning_ablation_enabled=True, ensemble_af=False")
print("============================================================")

state = run_campaign(
    graph,
    initial_state,
    settings,
    thread_id=resolved_run_id,
    printer=print,
)

final_summary = state.get("final_summary") or {}
summary = {
    "mode": "pure_reasoning_no_ensemble",
    "run_id": resolved_run_id,
    "run_seed": run_seed,
    "budget": budget,
    "warm_start": warm_start,
    "best_result": state.get("best_result"),
    "best_candidate": state.get("best_candidate"),
    "proposal_strategy": final_summary.get("proposal_strategy"),
    "stop_reason": final_summary.get("stop_reason"),
    "pure_reasoning_ablation_enabled": settings.pure_reasoning_ablation_enabled,
    "ensemble_af": settings.ensemble_af,
    "output_dir": str(target_dir),
}

print("Run summary:")
print(json.dumps(summary, ensure_ascii=False, indent=2))
PY
