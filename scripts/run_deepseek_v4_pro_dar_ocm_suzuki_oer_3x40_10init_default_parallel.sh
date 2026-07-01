#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="${ROOT_DIR}/scripts/run_dataset_3x40_10init_no_pr_no_pure_autobo_ensemble.sh"

if [[ ! -x "${RUNNER}" ]]; then
  echo "Dataset runner is not executable: ${RUNNER}" >&2
  exit 1
fi

if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
  echo "DEEPSEEK_API_KEY is not set. Export it before launching this script." >&2
  exit 1
fi

DATASETS=(ocm dar suzuki oer)
REPEATS_PER_DATASET="${REPEATS_PER_DATASET:-3}"
TOTAL_CPU_THREAD_CAP="${TOTAL_CPU_THREAD_CAP:-90}"
BASE_RUN_SEED="${BASE_RUN_SEED:-42}"
RUN_SEED_STEP="${RUN_SEED_STEP:-1000}"
BO_TORCH_DEVICES="${BO_TORCH_DEVICES:-cuda:2,cuda:3,cuda:5}"
BATCH_OUTPUT_DIR="${BATCH_OUTPUT_DIR:-${ROOT_DIR}/outputs/deepseek_v4_pro_ocm_dar_suzuki_oer_3x40_10init_default_$(date +%Y%m%d_%H%M%S)}"

if [[ ! "${REPEATS_PER_DATASET}" =~ ^[0-9]+$ ]] || (( REPEATS_PER_DATASET < 1 )); then
  echo "REPEATS_PER_DATASET must be a positive integer; got: ${REPEATS_PER_DATASET}" >&2
  exit 1
fi

if [[ ! "${TOTAL_CPU_THREAD_CAP}" =~ ^[0-9]+$ ]] || (( TOTAL_CPU_THREAD_CAP < REPEATS_PER_DATASET )); then
  echo "TOTAL_CPU_THREAD_CAP must be an integer >= ${REPEATS_PER_DATASET}; got: ${TOTAL_CPU_THREAD_CAP}" >&2
  exit 1
fi

IFS=',' read -r -a GPU_DEVICES <<< "${BO_TORCH_DEVICES}"
GPU_DEVICE_COUNT="${#GPU_DEVICES[@]}"
if (( GPU_DEVICE_COUNT < 1 )); then
  echo "BO_TORCH_DEVICES must contain at least one CUDA device; got: ${BO_TORCH_DEVICES}" >&2
  exit 1
fi

MAX_PER_JOB_CPU_THREAD_CAP="$((TOTAL_CPU_THREAD_CAP / REPEATS_PER_DATASET))"
PER_JOB_CPU_THREAD_CAP="${PER_JOB_CPU_THREAD_CAP:-${CPU_THREAD_CAP:-${MAX_PER_JOB_CPU_THREAD_CAP}}}"
if [[ ! "${PER_JOB_CPU_THREAD_CAP}" =~ ^[0-9]+$ ]] || (( PER_JOB_CPU_THREAD_CAP < 1 )); then
  echo "PER_JOB_CPU_THREAD_CAP/CPU_THREAD_CAP must be a positive integer; got: ${PER_JOB_CPU_THREAD_CAP}" >&2
  exit 1
fi
if (( PER_JOB_CPU_THREAD_CAP > MAX_PER_JOB_CPU_THREAD_CAP )); then
  PER_JOB_CPU_THREAD_CAP="${MAX_PER_JOB_CPU_THREAD_CAP}"
fi

if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON_CMD=("${PYTHON_BIN}")
elif [[ "${CONDA_DEFAULT_ENV:-}" == "chembo" && -n "${CONDA_PREFIX:-}" ]]; then
  PYTHON_CMD=("${CONDA_PREFIX}/bin/python")
elif [[ -x "/Users/stevens/anaconda3/envs/chembo/bin/python" ]]; then
  PYTHON_CMD=("/Users/stevens/anaconda3/envs/chembo/bin/python")
else
  PYTHON_CMD=("python")
fi

mkdir -p "${BATCH_OUTPUT_DIR}/logs"

echo "[ChemBO] DeepSeek v4 pro batch: datasets=${DATASETS[*]}, repeats=${REPEATS_PER_DATASET}"
echo "[ChemBO] LLM model=deepseek-v4-pro; thinking enabled on existing thinking nodes"
echo "[ChemBO] BO torch devices=${BO_TORCH_DEVICES}"
echo "[ChemBO] SWITCH_SURROGATE=true forced for every run"
echo "[ChemBO] Datasets run sequentially; per-dataset parallel jobs=${REPEATS_PER_DATASET}; per-job CPU thread cap=${PER_JOB_CPU_THREAD_CAP}"
echo "[ChemBO] Batch output root=${BATCH_OUTPUT_DIR}"

for DATASET in "${DATASETS[@]}"; do
  DATASET_LABEL="$(printf '%s' "${DATASET}" | tr '[:lower:]' '[:upper:]')"
  DATASET_OUTPUT_DIR="${BATCH_OUTPUT_DIR}/${DATASET}"
  DATASET_SUMMARY_DIR="${DATASET_OUTPUT_DIR}/.parallel_run_summaries"
  mkdir -p "${DATASET_OUTPUT_DIR}/logs" "${DATASET_SUMMARY_DIR}"

  echo "============================================================"
  echo "[ChemBO][${DATASET_LABEL}] Starting ${REPEATS_PER_DATASET} parallel repeats"
  echo "============================================================"

  PIDS=()
  PID_LABELS=()
  PID_LOGS=()
  JOB_INDEX=0

  for RUN_INDEX in $(seq 1 "${REPEATS_PER_DATASET}"); do
    SELECTED_GPU="$(printf '%s' "${GPU_DEVICES[$((JOB_INDEX % GPU_DEVICE_COUNT))]}" | xargs)"
    LOG_FILE="${BATCH_OUTPUT_DIR}/logs/${DATASET}_run$(printf '%02d' "${RUN_INDEX}").log"
    RUN_SUMMARY_JSON="${DATASET_SUMMARY_DIR}/run$(printf '%02d' "${RUN_INDEX}").json"
    RUN_SUMMARY_CSV="${DATASET_SUMMARY_DIR}/run$(printf '%02d' "${RUN_INDEX}").csv"

    echo "[ChemBO][${DATASET_LABEL}] Launching run$(printf '%02d' "${RUN_INDEX}") gpu=${SELECTED_GPU}; log=${LOG_FILE}"
    (
      export DATASET_NAME="${DATASET}"
      export REPEATS=1
      export START_RUN_INDEX="${RUN_INDEX}"
      export BASE_RUN_SEED="${BASE_RUN_SEED}"
      export RUN_SEED_STEP="${RUN_SEED_STEP}"
      export OUTPUT_DIR="${DATASET_OUTPUT_DIR}"
      export TASK_NAME="${DATASET}_deepseek_v4_pro_3x40_10init_default"
      export CHEMBO_LLM_MODEL="deepseek-v4-pro"
      export CHEMBO_LLM_BASE_URL="https://api.deepseek.com"
      export CHEMBO_LLM_API_KEY_ENV="DEEPSEEK_API_KEY"
      export CHEMBO_LLM_ENABLE_THINKING="true"
      export SWITCH_SURROGATE="true"
      export BO_TORCH_DEVICE="${SELECTED_GPU}"
      export CHEMBO_BO_TORCH_DEVICE="${SELECTED_GPU}"
      export CHEMBO_BO_TORCH_DEVICES="${BO_TORCH_DEVICES}"
      export CPU_THREAD_CAP="${PER_JOB_CPU_THREAD_CAP}"
      export OMP_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
      export MKL_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
      export OPENBLAS_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
      export NUMEXPR_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
      export BLIS_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
      export RAYON_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
      export NUMBA_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
      export SUMMARY_JSON="${RUN_SUMMARY_JSON}"
      export SUMMARY_CSV="${RUN_SUMMARY_CSV}"
      "${RUNNER}"
    ) >"${LOG_FILE}" 2>&1 &

    PIDS+=("$!")
    PID_LABELS+=("${DATASET_LABEL} run$(printf '%02d' "${RUN_INDEX}")")
    PID_LOGS+=("${LOG_FILE}")
    JOB_INDEX="$((JOB_INDEX + 1))"
  done

  FAILED=0
  for INDEX in "${!PIDS[@]}"; do
    PID="${PIDS[$INDEX]}"
    LABEL="${PID_LABELS[$INDEX]}"
    LOG_FILE="${PID_LOGS[$INDEX]}"
    if wait "${PID}"; then
      echo "[ChemBO][${LABEL}] completed successfully"
    else
      STATUS="$?"
      echo "[ChemBO][${LABEL}] failed with exit code ${STATUS}; see ${LOG_FILE}" >&2
      FAILED=1
    fi
  done

  if (( FAILED != 0 )); then
    echo "[ChemBO][${DATASET_LABEL}] One or more DeepSeek jobs failed." >&2
    exit 1
  fi
done

for DATASET in "${DATASETS[@]}"; do
  DATASET_OUTPUT_DIR="${BATCH_OUTPUT_DIR}/${DATASET}"
  DATASET_SUMMARY_DIR="${DATASET_OUTPUT_DIR}/.parallel_run_summaries"
  SUMMARY_JSON="${DATASET_OUTPUT_DIR}/run_summaries.json"
  SUMMARY_CSV="${DATASET_OUTPUT_DIR}/run_summaries.csv"

  "${PYTHON_CMD[@]}" - "${SUMMARY_JSON}" "${SUMMARY_CSV}" "${DATASET_SUMMARY_DIR}" <<'PY'
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

summary_json_path = Path(sys.argv[1])
summary_csv_path = Path(sys.argv[2])
summary_dir = Path(sys.argv[3])

summaries: list[dict[str, object]] = []
for path in sorted(summary_dir.glob("run*.json")):
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        summaries.extend(item for item in data if isinstance(item, dict))

summaries = sorted(summaries, key=lambda item: int(item.get("run_index", 0)))
summary_json_path.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")

fieldnames = [
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
    "switch_surrogate",
    "ensemble_sur",
    "ensemble_af",
    "bo_torch_device",
    "output_dir",
]
with summary_csv_path.open("w", encoding="utf-8", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=fieldnames)
    writer.writeheader()
    for item in summaries:
        writer.writerow({key: item.get(key) for key in fieldnames})
PY
done

echo "============================================================"
echo "[ChemBO] DeepSeek v4 pro 4-dataset batch complete."
echo "[ChemBO] Batch output root=${BATCH_OUTPUT_DIR}"
echo "============================================================"
