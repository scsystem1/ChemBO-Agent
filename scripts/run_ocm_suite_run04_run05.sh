#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_PROBLEM="${ROOT_DIR}/examples/ocm_problem.yaml"
DEFAULT_CONFIG="${ROOT_DIR}/dashscope_kimi_ocm.yaml"
BUDGET="${BUDGET:-40}"
REPEATS="${REPEATS:-2}"
START_RUN_INDEX="${START_RUN_INDEX:-4}"
MAX_CPU_CORES="${MAX_CPU_CORES:-50}"
BASE_SEED="${BASE_SEED:-20260423}"
SEED_STRIDE="${SEED_STRIDE:-1000}"

CHEMBO_OUTPUT_DIR="${CHEMBO_OUTPUT_DIR:-${ROOT_DIR}/outputs/ocm_5x_40iter}"
CHEMBO_TASK_NAME="${CHEMBO_TASK_NAME:-ocm_5x_40iter}"
REASONING_OUTPUT_DIR="${REASONING_OUTPUT_DIR:-${ROOT_DIR}/outputs/baseline_runs/reasoning_bo/ocm}"
GOLLUM_OUTPUT_DIR="${GOLLUM_OUTPUT_DIR:-${ROOT_DIR}/outputs/baseline_runs/gollum/ocm}"
PREFBO_OUTPUT_DIR="${PREFBO_OUTPUT_DIR:-${ROOT_DIR}/outputs/baseline_runs/prefbo/ocm}"
PREFBO_PREFERENCES_FILE="${PREFBO_PREFERENCES_FILE:-${ROOT_DIR}/baseline/Pref-BO/ocm_preferences.npy}"

export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export REASONINGBO_LLM_MODEL="${REASONINGBO_LLM_MODEL:-kimi-k2.5-thinking}"
export REASONINGBO_BASE_URL="${REASONINGBO_BASE_URL:-https://dashscope.aliyuncs.com/compatible-mode/v1}"
export PREFBO_LLM_MODEL="${PREFBO_LLM_MODEL:-kimi-k2.5-thinking}"
export PREFBO_BASE_URL="${PREFBO_BASE_URL:-https://dashscope.aliyuncs.com/compatible-mode/v1}"
export WANDB_MODE="${WANDB_MODE:-disabled}"
export HF_HOME="${HF_HOME:-/data/shared/huggingface}"
export TRANSFORMERS_CACHE="${TRANSFORMERS_CACHE:-${HF_HOME}/hub}"
export HUGGINGFACE_HUB_CACHE="${HUGGINGFACE_HUB_CACHE:-${HF_HOME}/hub}"
export GOLLUM_CUDA_VISIBLE_DEVICES="${GOLLUM_CUDA_VISIBLE_DEVICES:-3}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-${GOLLUM_CUDA_VISIBLE_DEVICES}}"

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

if [[ ! -f "${PREFBO_PREFERENCES_FILE}" ]]; then
  echo "PrefBO preferences file not found: ${PREFBO_PREFERENCES_FILE}" >&2
  exit 1
fi

if ! [[ "${REPEATS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "REPEATS must be a positive integer, got: ${REPEATS}" >&2
  exit 1
fi

if ! [[ "${START_RUN_INDEX}" =~ ^[1-9][0-9]*$ ]]; then
  echo "START_RUN_INDEX must be a positive integer, got: ${START_RUN_INDEX}" >&2
  exit 1
fi

if ! [[ "${MAX_CPU_CORES}" =~ ^[1-9][0-9]*$ ]]; then
  echo "MAX_CPU_CORES must be a positive integer, got: ${MAX_CPU_CORES}" >&2
  exit 1
fi

AVAILABLE_CORES=1
if command -v getconf >/dev/null 2>&1; then
  AVAILABLE_CORES="$(getconf _NPROCESSORS_ONLN)"
elif command -v nproc >/dev/null 2>&1; then
  AVAILABLE_CORES="$(nproc)"
fi

if [[ "${AVAILABLE_CORES}" =~ ^[1-9][0-9]*$ ]] && (( MAX_CPU_CORES > AVAILABLE_CORES )); then
  CPU_THREAD_CAP="${AVAILABLE_CORES}"
else
  CPU_THREAD_CAP="${MAX_CPU_CORES}"
fi

export OMP_NUM_THREADS="${OMP_NUM_THREADS:-${CPU_THREAD_CAP}}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-${CPU_THREAD_CAP}}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-${CPU_THREAD_CAP}}"
export NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-${CPU_THREAD_CAP}}"
export VECLIB_MAXIMUM_THREADS="${VECLIB_MAXIMUM_THREADS:-${CPU_THREAD_CAP}}"
export BLIS_NUM_THREADS="${BLIS_NUM_THREADS:-${CPU_THREAD_CAP}}"
export RAYON_NUM_THREADS="${RAYON_NUM_THREADS:-${CPU_THREAD_CAP}}"
export NUMBA_NUM_THREADS="${NUMBA_NUM_THREADS:-${CPU_THREAD_CAP}}"
export POLARS_MAX_THREADS="${POLARS_MAX_THREADS:-${CPU_THREAD_CAP}}"

CPU_AFFINITY_PREFIX=()
if command -v taskset >/dev/null 2>&1; then
  CPU_AFFINITY_PREFIX=("taskset" "-c" "0-$((CPU_THREAD_CAP - 1))")
fi

mkdir -p "${CHEMBO_OUTPUT_DIR}/.parallel_run_summaries"

END_RUN_INDEX=$((START_RUN_INDEX + REPEATS - 1))
SEED_START=$((START_RUN_INDEX - 1))

echo "[OCM Suite] Running ChemBO, Reasoning-BO, GOLLuM, and PrefBO for run$(printf "%02d" "${START_RUN_INDEX}")-run$(printf "%02d" "${END_RUN_INDEX}")"
echo "[OCM Suite] CPU thread cap: ${CPU_THREAD_CAP}"

for run_index in $(seq "${START_RUN_INDEX}" "${END_RUN_INDEX}"); do
  run_id="$(printf "run%02d" "${run_index}")"
  run_seed=$((BASE_SEED + (run_index - 1) * SEED_STRIDE))
  summary_path="${CHEMBO_OUTPUT_DIR}/.parallel_run_summaries/${run_id}.json"
  log_path="${CHEMBO_OUTPUT_DIR}/${run_id}.log"

  if [[ -e "${summary_path}" ]]; then
    echo "Refusing to overwrite existing ChemBO summary: ${summary_path}" >&2
    exit 1
  fi

  echo "[ChemBO][OCM] Starting ${run_id} with seed=${run_seed}"
  "${CPU_AFFINITY_PREFIX[@]}" conda run --no-capture-output -n chembo python - \
    "${ROOT_DIR}" "${PROBLEM_FILE}" "${CONFIG_FILE}" "${CHEMBO_OUTPUT_DIR}" "${CHEMBO_TASK_NAME}" "${BUDGET}" "${run_index}" "${run_seed}" "${summary_path}" \
    >"${log_path}" 2>&1 <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

root_dir = Path(sys.argv[1]).resolve()
problem_path = Path(sys.argv[2]).resolve()
config_path = Path(sys.argv[3]).resolve()
output_dir = Path(sys.argv[4]).resolve()
task_name_override = sys.argv[5].strip()
budget = int(sys.argv[6])
run_index = int(sys.argv[7])
run_seed = int(sys.argv[8])
summary_path = Path(sys.argv[9]).resolve()

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
    raise RuntimeError("OCM suite script expects a structured YAML/JSON problem file.")

run_id = f"run{run_index:02d}"

print("============================================================")
print(f"OCM repeat {run_index}")
print(f"Problem file: {problem_path}")
print(f"Config file: {config_path}")
print(f"Output root: {output_dir}")
print(f"Budget override: {budget}")
print(f"Run id: {run_id}")
print(f"Random seed: {run_seed}")
print("============================================================")

settings = Settings.from_yaml(str(config_path)) if config_path.exists() else Settings()
settings.max_bo_iterations = budget
settings.pure_reasoning_ablation_enabled = False
settings.ensemble_af = False
settings.output_dir = str(output_dir)
settings.experiment_name = _slugify(task_name_override or problem_path.stem)
settings.experiment_id = run_id
settings.random_seed = run_seed

problem = json.loads(json.dumps(base_problem))
problem["budget"] = budget

graph = build_chembo_graph(settings)
initial_state = create_initial_state(problem, settings, problem_source_path=str(problem_path))
resolved_run_id = _default_run_id(initial_state, settings)
resolved_output_dir = output_dir / resolved_run_id
if resolved_output_dir.exists():
    raise RuntimeError(f"Refusing to overwrite existing ChemBO output directory: {resolved_output_dir}")
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
    "warm_start": settings.initial_doe_size,
    "random_seed": settings.random_seed,
    "best_result": state.get("best_result"),
    "best_candidate": state.get("best_candidate"),
    "proposal_strategy": (state.get("final_summary") or {}).get("proposal_strategy"),
    "stop_reason": (state.get("final_summary") or {}).get("stop_reason"),
    "pure_reasoning_ablation_enabled": settings.pure_reasoning_ablation_enabled,
    "ensemble_af": settings.ensemble_af,
    "output_dir": str(resolved_output_dir),
}
summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

print("Run summary:")
print(json.dumps(summary, ensure_ascii=False, indent=2))
print(f"Per-run summary written to: {summary_path}")
PY
done

"${CPU_AFFINITY_PREFIX[@]}" conda run --no-capture-output -n chembo python - \
  "${CHEMBO_OUTPUT_DIR}/.parallel_run_summaries" "${CHEMBO_OUTPUT_DIR}/run_summaries.json" "${CHEMBO_OUTPUT_DIR}/run_summaries.csv" <<'PY'
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

summary_tmp_dir = Path(sys.argv[1]).resolve()
summary_json_path = Path(sys.argv[2]).resolve()
summary_csv_path = Path(sys.argv[3]).resolve()

summaries = []
for path in sorted(summary_tmp_dir.glob("run*.json")):
    summaries.append(json.loads(path.read_text(encoding="utf-8")))

summaries.sort(key=lambda item: int(item.get("run_index", 0)))
summary_json_path.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")

with summary_csv_path.open("w", encoding="utf-8", newline="") as handle:
    writer = csv.DictWriter(
        handle,
        fieldnames=[
            "run_index",
            "run_id",
            "budget",
            "warm_start",
            "random_seed",
            "best_result",
            "proposal_strategy",
            "stop_reason",
            "pure_reasoning_ablation_enabled",
            "ensemble_af",
            "output_dir",
        ],
    )
    writer.writeheader()
    for item in summaries:
        writer.writerow({key: item.get(key) for key in writer.fieldnames})
PY

echo "[ReasoningBO][OCM] Appending trials ${START_RUN_INDEX}-${END_RUN_INDEX}"
"${CPU_AFFINITY_PREFIX[@]}" conda run --no-capture-output -n reasoning_bo python "${ROOT_DIR}/baseline/Reasoning-BO/run_tabular_reasoning_bo.py" \
  --dataset ocm \
  --trials "${REPEATS}" \
  --trial-start-index "${START_RUN_INDEX}" \
  --seed-start "${SEED_START}" \
  --append-to-existing \
  --total-budget "${BUDGET}" \
  --reasoning-batch-size 3 \
  --output-dir "${REASONING_OUTPUT_DIR}"

echo "[GOLLuM][OCM] Appending trials ${START_RUN_INDEX}-${END_RUN_INDEX}"
"${CPU_AFFINITY_PREFIX[@]}" conda run --no-capture-output -n gollum python "${ROOT_DIR}/baseline/gollum/run_tabular_gollum.py" \
  --dataset ocm \
  --trials "${REPEATS}" \
  --trial-start-index "${START_RUN_INDEX}" \
  --seed-start "${SEED_START}" \
  --append-to-existing \
  --total-budget "${BUDGET}" \
  --output-dir "${GOLLUM_OUTPUT_DIR}"

echo "[PrefBO][OCM] Appending trials ${START_RUN_INDEX}-${END_RUN_INDEX} using ${PREFBO_PREFERENCES_FILE}"
"${CPU_AFFINITY_PREFIX[@]}" conda run --no-capture-output -n prefbo python "${ROOT_DIR}/baseline/Pref-BO/run_tabular_preference_bo.py" \
  --dataset ocm \
  --trials "${REPEATS}" \
  --trial-start-index "${START_RUN_INDEX}" \
  --seed-start "${SEED_START}" \
  --append-to-existing \
  --total-budget "${BUDGET}" \
  --preferences-file "${PREFBO_PREFERENCES_FILE}" \
  --max-cpu-threads "${CPU_THREAD_CAP}" \
  --output-dir "${PREFBO_OUTPUT_DIR}"

echo "[OCM Suite] Completed run$(printf "%02d" "${START_RUN_INDEX}")-run$(printf "%02d" "${END_RUN_INDEX}")"
