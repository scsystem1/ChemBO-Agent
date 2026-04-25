#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_OCM_PROBLEM="${ROOT_DIR}/examples/ocm_problem.yaml"
DEFAULT_OCM_CONFIG="${ROOT_DIR}/dashscope_kimi_ocm.yaml"
REPEATS="${REPEATS:-3}"
BUDGET="${BUDGET:-40}"
OUTPUT_DIR="${OUTPUT_DIR:-${ROOT_DIR}/outputs/dar_ocm_ensemble_3x_40iter}"
TASK_NAME_OVERRIDE="${TASK_NAME:-dar_ocm_ensemble_3x_40iter_ocm_only}"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON_CMD=("${PYTHON_BIN}")
elif [[ "${CONDA_DEFAULT_ENV:-}" == "chembo" && -n "${CONDA_PREFIX:-}" ]]; then
  PYTHON_CMD=("${CONDA_PREFIX}/bin/python")
elif [[ -x "/Users/stevens/anaconda3/envs/chembo/bin/python" ]]; then
  PYTHON_CMD=("/Users/stevens/anaconda3/envs/chembo/bin/python")
else
  PYTHON_CMD=("python")
fi

OCM_PROBLEM_FILE="${1:-$DEFAULT_OCM_PROBLEM}"
OCM_CONFIG_FILE="${2:-$DEFAULT_OCM_CONFIG}"

for required_file in "${OCM_PROBLEM_FILE}" "${OCM_CONFIG_FILE}"; do
  if [[ ! -f "${required_file}" ]]; then
    echo "Required file not found: ${required_file}" >&2
    exit 1
  fi
done

mkdir -p "${OUTPUT_DIR}"

SUMMARY_JSON="${OUTPUT_DIR}/run_summaries.json"
SUMMARY_CSV="${OUTPUT_DIR}/run_summaries.csv"

"${PYTHON_CMD[@]}" - "${ROOT_DIR}" "${OCM_PROBLEM_FILE}" "${OCM_CONFIG_FILE}" "${OUTPUT_DIR}" "${TASK_NAME_OVERRIDE}" "${REPEATS}" "${BUDGET}" "${SUMMARY_JSON}" "${SUMMARY_CSV}" <<'PY'
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

root_dir = Path(sys.argv[1]).resolve()
ocm_problem_path = Path(sys.argv[2]).resolve()
ocm_config_path = Path(sys.argv[3]).resolve()
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


OCM_RUN_SEEDS = {
    1: 20260423,
    2: 20261423,
    3: 20262423,
}


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


base_problem = load_problem_file(ocm_problem_path)
if not isinstance(base_problem, dict):
    raise RuntimeError("OCM ensemble batch script expects a structured YAML/JSON problem file.")

if repeats > len(OCM_RUN_SEEDS):
    raise RuntimeError(
        f"Configured repeats={repeats}, but only {len(OCM_RUN_SEEDS)} fixed OCM run seeds are defined."
    )

domain_output_dir = output_dir / "ocm"
domain_output_dir.mkdir(parents=True, exist_ok=True)

all_summaries: list[dict[str, object]] = []
for run_index in range(1, repeats + 1):
    run_id = f"run{run_index:02d}"
    run_seed = OCM_RUN_SEEDS[run_index]

    print("============================================================")
    print(f"OCM ensemble repeat {run_index}/{repeats}")
    print(f"Problem file: {ocm_problem_path}")
    print(f"Config file: {ocm_config_path}")
    print(f"Output root: {domain_output_dir}")
    print(f"Budget override: {budget}")
    print(f"Run id: {run_id}")
    print(f"Run seed: {run_seed}")
    print("============================================================")

    settings = Settings.from_yaml(str(ocm_config_path)) if ocm_config_path.exists() else Settings()
    settings.max_bo_iterations = budget
    settings.pure_reasoning_ablation_enabled = False
    settings.ensemble_af = True
    settings.random_seed = run_seed
    settings.output_dir = str(domain_output_dir)
    settings.experiment_name = _slugify(f"{task_name_override or ocm_problem_path.stem}_ocm")
    settings.experiment_id = run_id

    problem = json.loads(json.dumps(base_problem))
    problem["budget"] = budget

    graph = build_chembo_graph(settings)
    initial_state = create_initial_state(problem, settings, problem_source_path=str(ocm_problem_path))
    resolved_run_id = _default_run_id(initial_state, settings)
    state = run_campaign(
        graph,
        initial_state,
        settings,
        thread_id=resolved_run_id,
        printer=print,
    )

    final_summary = state.get("final_summary") or {}
    summary = {
        "domain": "ocm",
        "run_index": run_index,
        "run_id": resolved_run_id,
        "run_seed": settings.random_seed,
        "budget": budget,
        "warm_start": settings.initial_doe_size,
        "best_result": state.get("best_result"),
        "best_candidate": state.get("best_candidate"),
        "proposal_strategy": final_summary.get("proposal_strategy"),
        "stop_reason": final_summary.get("stop_reason"),
        "pure_reasoning_ablation_enabled": settings.pure_reasoning_ablation_enabled,
        "ensemble_af": settings.ensemble_af,
        "output_dir": str(domain_output_dir / resolved_run_id),
    }
    all_summaries.append(summary)
    print("Run summary:")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

summary_json_path.write_text(json.dumps(all_summaries, ensure_ascii=False, indent=2), encoding="utf-8")

with summary_csv_path.open("w", encoding="utf-8", newline="") as handle:
    writer = csv.DictWriter(
        handle,
        fieldnames=[
            "domain",
            "run_index",
            "run_id",
            "run_seed",
            "budget",
            "warm_start",
            "best_result",
            "proposal_strategy",
            "stop_reason",
            "pure_reasoning_ablation_enabled",
            "ensemble_af",
            "output_dir",
        ],
    )
    writer.writeheader()
    for item in all_summaries:
        writer.writerow({key: item.get(key) for key in writer.fieldnames})

print("============================================================")
print("Combined batch summary written:")
print(summary_json_path)
print(summary_csv_path)
print("============================================================")
PY
