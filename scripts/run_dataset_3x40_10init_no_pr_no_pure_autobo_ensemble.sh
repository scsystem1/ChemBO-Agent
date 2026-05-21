#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -n "${DATASET_NAME:-}" ]]; then
  DATASET_NAME_RAW="${DATASET_NAME}"
  PROBLEM_ARG="${1:-}"
  CONFIG_ARG="${2:-}"
else
  DATASET_NAME_RAW="${1:-ocm}"
  PROBLEM_ARG="${2:-}"
  CONFIG_ARG="${3:-}"
fi
DATASET_NAME="$(printf '%s' "${DATASET_NAME_RAW}" | tr '[:upper:]' '[:lower:]')"
DATASET_LABEL="$(printf '%s' "${DATASET_NAME}" | tr '[:lower:]' '[:upper:]')"

case "${DATASET_NAME}" in
  dar)
    DEFAULT_PROBLEM="${ROOT_DIR}/examples/dar_problem.yaml"
    DEFAULT_CONFIG="${ROOT_DIR}/dashscope_kimi.yaml"
    ;;
  ocm)
    DEFAULT_PROBLEM="${ROOT_DIR}/examples/ocm_problem.yaml"
    DEFAULT_CONFIG="${ROOT_DIR}/dashscope_kimi_ocm.yaml"
    ;;
  suzuki)
    DEFAULT_PROBLEM="${ROOT_DIR}/examples/suzuki_problem.yaml"
    DEFAULT_CONFIG="${ROOT_DIR}/dashscope_kimi.yaml"
    ;;
  oer)
    DEFAULT_PROBLEM="${ROOT_DIR}/examples/oer_problem.yaml"
    DEFAULT_CONFIG="${ROOT_DIR}/dashscope_kimi.yaml"
    ;;
  *)
    echo "Unsupported DATASET_NAME: ${DATASET_NAME_RAW}. Use one of: dar, ocm, suzuki, oer." >&2
    exit 1
    ;;
esac

REPEATS="${REPEATS:-3}"
BUDGET="${BUDGET:-40}"
WARM_START="${WARM_START:-10}"
CPU_THREAD_CAP="${CPU_THREAD_CAP:-80}"
DEFAULT_OUTPUT_DIR="${ROOT_DIR}/outputs/${DATASET_NAME}_3x40_10init_no_pr_no_pure_autobo_ensemble"
OUTPUT_DIR="${OUTPUT_DIR:-${DEFAULT_OUTPUT_DIR}}"
TASK_NAME_OVERRIDE="${TASK_NAME:-${DATASET_NAME}_3x40_10init_no_pr_no_pure_autobo_ensemble}"
START_RUN_INDEX="${START_RUN_INDEX:-}"
BASE_RUN_SEED="${BASE_RUN_SEED:-42}"
RUN_SEED_STEP="${RUN_SEED_STEP:-1000}"
BO_TORCH_DEVICE="${BO_TORCH_DEVICE:-cuda:5}"

export CHEMBO_BO_TORCH_DEVICE="${CHEMBO_BO_TORCH_DEVICE:-${BO_TORCH_DEVICE}}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-${CPU_THREAD_CAP}}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-${CPU_THREAD_CAP}}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-${CPU_THREAD_CAP}}"
export NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-${CPU_THREAD_CAP}}"
export BLIS_NUM_THREADS="${BLIS_NUM_THREADS:-${CPU_THREAD_CAP}}"
export RAYON_NUM_THREADS="${RAYON_NUM_THREADS:-${CPU_THREAD_CAP}}"
export NUMBA_NUM_THREADS="${NUMBA_NUM_THREADS:-${CPU_THREAD_CAP}}"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON_CMD=("${PYTHON_BIN}")
elif [[ "${CONDA_DEFAULT_ENV:-}" == "chembo" && -n "${CONDA_PREFIX:-}" ]]; then
  PYTHON_CMD=("${CONDA_PREFIX}/bin/python")
elif [[ -x "/Users/stevens/anaconda3/envs/chembo/bin/python" ]]; then
  PYTHON_CMD=("/Users/stevens/anaconda3/envs/chembo/bin/python")
else
  PYTHON_CMD=("python")
fi

PROBLEM_FILE="${PROBLEM_FILE:-${PROBLEM_ARG:-$DEFAULT_PROBLEM}}"
CONFIG_FILE="${CONFIG_FILE:-${CONFIG_ARG:-$DEFAULT_CONFIG}}"

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

echo "[ChemBO][${DATASET_LABEL}] Starting dataset run: repeats=${REPEATS}, budget=${BUDGET}, warm_start=${WARM_START}, output=${OUTPUT_DIR}"

"${PYTHON_CMD[@]}" - "${ROOT_DIR}" "${DATASET_NAME}" "${PROBLEM_FILE}" "${CONFIG_FILE}" "${OUTPUT_DIR}" "${TASK_NAME_OVERRIDE}" "${REPEATS}" "${BUDGET}" "${WARM_START}" "${SUMMARY_JSON}" "${SUMMARY_CSV}" "${START_RUN_INDEX}" "${BASE_RUN_SEED}" "${RUN_SEED_STEP}" <<'PY'
from __future__ import annotations

import csv
import json
import os
import re
import sys
from pathlib import Path

root_dir = Path(sys.argv[1]).resolve()
dataset_name = sys.argv[2].strip().lower()
problem_path = Path(sys.argv[3]).resolve()
config_path = Path(sys.argv[4]).resolve()
output_dir = Path(sys.argv[5]).resolve()
task_name_override = sys.argv[6].strip()
repeats = int(sys.argv[7])
budget = int(sys.argv[8])
warm_start = int(sys.argv[9])
summary_json_path = Path(sys.argv[10]).resolve()
summary_csv_path = Path(sys.argv[11]).resolve()
start_run_index_raw = sys.argv[12].strip()
base_run_seed = int(sys.argv[13])
run_seed_step = int(sys.argv[14])

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
    normalized: list[str] = []
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


def _detect_next_run_index(output_dir: Path) -> int:
    pattern = re.compile(r"_run(\d+)$")
    max_run_index = 0
    if not output_dir.exists():
        return 1
    for child in output_dir.iterdir():
        if not child.is_dir():
            continue
        match = pattern.search(child.name)
        if match:
            max_run_index = max(max_run_index, int(match.group(1)))
    return max_run_index + 1 if max_run_index else 1


def _load_existing_summaries(summary_json_path: Path) -> list[dict[str, object]]:
    if not summary_json_path.exists():
        return []
    data = json.loads(summary_json_path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


base_problem = load_problem_file(problem_path)
if not isinstance(base_problem, dict):
    raise RuntimeError(f"{dataset_name.upper()} batch script expects a structured YAML/JSON problem file.")

start_run_index = int(start_run_index_raw) if start_run_index_raw else _detect_next_run_index(output_dir)
existing_summaries = _load_existing_summaries(summary_json_path)
new_summaries: list[dict[str, object]] = []

for offset in range(repeats):
    run_index = start_run_index + offset
    run_id = f"run{run_index:02d}"
    run_seed = base_run_seed + (run_index - 1) * run_seed_step
    print("============================================================")
    print(f"{dataset_name.upper()} repeat {offset + 1}/{repeats}")
    print(f"Problem file: {problem_path}")
    print(f"Config file: {config_path}")
    print(f"Output root: {output_dir}")
    print(f"Budget override: {budget}")
    print(f"Warm start override: {warm_start}")
    print(f"Run id: {run_id}")
    print(f"Run seed: {run_seed}")
    print(f"BO torch device: {os.environ.get('CHEMBO_BO_TORCH_DEVICE', 'cpu')}")
    print(
        "Settings override: "
        "prior_writer_enabled=False, "
        "pure_reasoning_ablation_enabled=False, "
        "zero_llm_ablation_enabled=False, "
        "autobo_llm_acq_enabled=True, "
        "autobo_llm_plaus_enabled=True, "
        "ensemble_sur=True, "
        "ensemble_af=True"
    )
    print("============================================================")

    settings = Settings.from_yaml(str(config_path)) if config_path.exists() else Settings()
    settings.max_bo_iterations = budget
    settings.initial_doe_size = warm_start
    settings.random_seed = run_seed
    settings.prior_writer_enabled = False
    settings.pure_reasoning_ablation_enabled = False
    settings.zero_llm_ablation_enabled = False
    settings.autobo_llm_acq_enabled = True
    settings.autobo_llm_plaus_enabled = True
    settings.ensemble_sur = True
    settings.ensemble_af = True
    settings.output_dir = str(output_dir)
    settings.experiment_name = _slugify(task_name_override or f"{dataset_name}_3x40_10init_no_pr_no_pure_autobo_ensemble")
    settings.experiment_id = run_id

    problem = json.loads(json.dumps(base_problem))
    problem["budget"] = budget

    graph = build_chembo_graph(settings)
    initial_state = create_initial_state(problem, settings, problem_source_path=str(problem_path))
    resolved_run_id = _default_run_id(initial_state, settings)
    target_dir = output_dir / resolved_run_id
    if target_dir.exists():
        raise RuntimeError(
            f"Target output directory already exists: {target_dir}\n"
            "Refusing to overwrite. Set START_RUN_INDEX to continue from a later run number."
        )
    state = run_campaign(
        graph,
        initial_state,
        settings,
        thread_id=resolved_run_id,
        printer=print,
    )

    final_summary = state.get("final_summary") or {}
    summary = {
        "dataset_name": dataset_name,
        "run_index": run_index,
        "run_id": resolved_run_id,
        "run_seed": run_seed,
        "budget": budget,
        "warm_start": settings.initial_doe_size,
        "best_result": state.get("best_result"),
        "best_candidate": state.get("best_candidate"),
        "proposal_strategy": final_summary.get("proposal_strategy"),
        "stop_reason": final_summary.get("stop_reason"),
        "prior_writer_enabled": settings.prior_writer_enabled,
        "pure_reasoning_ablation_enabled": settings.pure_reasoning_ablation_enabled,
        "zero_llm_ablation_enabled": settings.zero_llm_ablation_enabled,
        "autobo_llm_acq_enabled": settings.autobo_llm_acq_enabled,
        "autobo_llm_plaus_enabled": settings.autobo_llm_plaus_enabled,
        "ensemble_sur": settings.ensemble_sur,
        "ensemble_af": settings.ensemble_af,
        "bo_torch_device": os.environ.get("CHEMBO_BO_TORCH_DEVICE", "cpu"),
        "output_dir": str(target_dir),
    }
    new_summaries.append(summary)
    print("Run summary:")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

all_summaries = sorted(existing_summaries + new_summaries, key=lambda item: int(item.get("run_index", 0)))

summary_json_path.write_text(json.dumps(all_summaries, ensure_ascii=False, indent=2), encoding="utf-8")

with summary_csv_path.open("w", encoding="utf-8", newline="") as handle:
    writer = csv.DictWriter(
        handle,
        fieldnames=[
            "dataset_name",
            "run_index",
            "run_id",
            "run_seed",
            "budget",
            "warm_start",
            "best_result",
            "proposal_strategy",
            "stop_reason",
            "prior_writer_enabled",
            "pure_reasoning_ablation_enabled",
            "zero_llm_ablation_enabled",
            "autobo_llm_acq_enabled",
            "autobo_llm_plaus_enabled",
            "ensemble_sur",
            "ensemble_af",
            "bo_torch_device",
            "output_dir",
        ],
    )
    writer.writeheader()
    for item in all_summaries:
        writer.writerow({key: item.get(key) for key in writer.fieldnames})

print("============================================================")
print("Batch summary written:")
print(summary_json_path)
print(summary_csv_path)
print("============================================================")
PY
