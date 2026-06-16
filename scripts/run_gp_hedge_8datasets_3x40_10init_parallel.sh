#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="${ROOT_DIR}/baseline/gp_hedge/run_tabular_gp_hedge.py"

if [[ ! -f "${RUNNER}" ]]; then
  echo "GP-Hedge runner not found: ${RUNNER}" >&2
  exit 1
fi

DATASETS=(dar ocm suzuki oer hpobench_svm hpobench_rf hpobench_xgb hpobench_nn)
REPEATS_PER_DATASET="${REPEATS_PER_DATASET:-5}"
TOTAL_CPU_THREAD_CAP="${TOTAL_CPU_THREAD_CAP:-90}"
BASE_RUN_SEED="${BASE_RUN_SEED:-314159}"
RUN_SEED_STEP="${RUN_SEED_STEP:-7919}"
START_RUN_INDEX_BASE="${START_RUN_INDEX:-1}"
BATCH_OUTPUT_DIR="${BATCH_OUTPUT_DIR:-${ROOT_DIR}/outputs/baseline_runs/gp_hedge/gp_hedge_8datasets_5x40_10init_$(date +%Y%m%d_%H%M%S)}"

if [[ ! "${REPEATS_PER_DATASET}" =~ ^[0-9]+$ ]] || (( REPEATS_PER_DATASET < 1 )); then
  echo "REPEATS_PER_DATASET must be a positive integer; got: ${REPEATS_PER_DATASET}" >&2
  exit 1
fi

if [[ ! "${TOTAL_CPU_THREAD_CAP}" =~ ^[0-9]+$ ]] || (( TOTAL_CPU_THREAD_CAP < REPEATS_PER_DATASET )); then
  echo "TOTAL_CPU_THREAD_CAP must be an integer >= ${REPEATS_PER_DATASET}; got: ${TOTAL_CPU_THREAD_CAP}" >&2
  exit 1
fi

if [[ ! "${START_RUN_INDEX_BASE}" =~ ^[0-9]+$ ]] || (( START_RUN_INDEX_BASE < 1 )); then
  echo "START_RUN_INDEX must be a positive integer; got: ${START_RUN_INDEX_BASE}" >&2
  exit 1
fi

if [[ ! "${BASE_RUN_SEED}" =~ ^-?[0-9]+$ ]]; then
  echo "BASE_RUN_SEED must be an integer; got: ${BASE_RUN_SEED}" >&2
  exit 1
fi

if [[ ! "${RUN_SEED_STEP}" =~ ^[0-9]+$ ]] || (( RUN_SEED_STEP < 1 )); then
  echo "RUN_SEED_STEP must be a positive integer; got: ${RUN_SEED_STEP}" >&2
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
else
  PYTHON_CMD=(
    conda run --no-capture-output -n chembo
    bash -lc 'export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${LD_LIBRARY_PATH:-}"; python "$@"'
    bash
  )
fi

mkdir -p "${BATCH_OUTPUT_DIR}/logs"

echo "[GP-Hedge] 8-dataset batch"
echo "[GP-Hedge] Datasets run sequentially: ${DATASETS[*]}"
echo "[GP-Hedge] Per-dataset parallel repeats=${REPEATS_PER_DATASET}; total_budget=40; init_size=10"
echo "[GP-Hedge] Start run index base=${START_RUN_INDEX_BASE}; base seed=${BASE_RUN_SEED}; seed step=${RUN_SEED_STEP}"
echo "[GP-Hedge] Per-job CPU thread cap=${PER_JOB_CPU_THREAD_CAP}"
echo "[GP-Hedge] Batch output root=${BATCH_OUTPUT_DIR}"

for DATASET in "${DATASETS[@]}"; do
  DATASET_LABEL="$(printf '%s' "${DATASET}" | tr '[:lower:]' '[:upper:]')"
  DATASET_OUTPUT_DIR="${BATCH_OUTPUT_DIR}/${DATASET}"
  mkdir -p "${DATASET_OUTPUT_DIR}"

  echo "============================================================"
  echo "[GP-Hedge][${DATASET_LABEL}] Starting ${REPEATS_PER_DATASET} parallel repeats"
  echo "============================================================"

  PIDS=()
  PID_LABELS=()
  PID_LOGS=()

  for OFFSET in $(seq 0 "$((REPEATS_PER_DATASET - 1))"); do
    RUN_INDEX="$((START_RUN_INDEX_BASE + OFFSET))"
    RUN_SEED="$((BASE_RUN_SEED + (RUN_INDEX - 1) * RUN_SEED_STEP))"
    RUN_OUTPUT_DIR="${DATASET_OUTPUT_DIR}/trial_$(printf '%02d' "${RUN_INDEX}")"
    LOG_FILE="${BATCH_OUTPUT_DIR}/logs/${DATASET}_trial$(printf '%02d' "${RUN_INDEX}").log"

    echo "[GP-Hedge][${DATASET_LABEL}] Launching trial_$(printf '%02d' "${RUN_INDEX}") seed=${RUN_SEED}; log=${LOG_FILE}"
    (
      export OMP_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
      export MKL_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
      export OPENBLAS_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
      export NUMEXPR_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
      export BLIS_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
      "${PYTHON_CMD[@]}" "${RUNNER}" \
        --dataset "${DATASET}" \
        --trials 1 \
        --trial-start-index "${RUN_INDEX}" \
        --seed-start "${RUN_SEED}" \
        --seed-step "${RUN_SEED_STEP}" \
        --total-budget 40 \
        --init-size 10 \
        --output-dir "${RUN_OUTPUT_DIR}"
    ) >"${LOG_FILE}" 2>&1 &

    PIDS+=("$!")
    PID_LABELS+=("${DATASET_LABEL} trial_$(printf '%02d' "${RUN_INDEX}")")
    PID_LOGS+=("${LOG_FILE}")
  done

  FAILED=0
  for INDEX in "${!PIDS[@]}"; do
    PID="${PIDS[$INDEX]}"
    LABEL="${PID_LABELS[$INDEX]}"
    LOG_FILE="${PID_LOGS[$INDEX]}"
    if wait "${PID}"; then
      echo "[GP-Hedge][${LABEL}] completed successfully"
    else
      STATUS="$?"
      echo "[GP-Hedge][${LABEL}] failed with exit code ${STATUS}; see ${LOG_FILE}" >&2
      FAILED=1
    fi
  done

  if (( FAILED != 0 )); then
    echo "[GP-Hedge][${DATASET_LABEL}] One or more jobs failed." >&2
    exit 1
  fi

  "${PYTHON_CMD[@]}" - "${DATASET_OUTPUT_DIR}" <<'PY'
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

dataset_output_dir = Path(sys.argv[1])
summaries: list[dict[str, object]] = []
for path in sorted(dataset_output_dir.glob("trial_*/run_summaries.json")):
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        summaries.extend(item for item in data if isinstance(item, dict))
summaries = sorted(summaries, key=lambda item: int(item.get("trial_number", 0)))
(dataset_output_dir / "run_summaries.json").write_text(
    json.dumps(summaries, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
fieldnames = [
    "dataset",
    "trial_number",
    "seed",
    "total_budget",
    "init_size",
    "actual_evaluations",
    "initial_best",
    "final_best",
    "best_value",
    "best_row_index",
    "trace_path",
    "metadata_path",
    "results_path",
    "output_dir",
]
with (dataset_output_dir / "run_summaries.csv").open("w", encoding="utf-8", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=fieldnames)
    writer.writeheader()
    for item in summaries:
        writer.writerow({key: item.get(key) for key in fieldnames})
print(f"[GP-Hedge] Wrote {len(summaries)} summaries for {dataset_output_dir.name}")
PY
done

"${PYTHON_CMD[@]}" - "${BATCH_OUTPUT_DIR}" "${DATASETS[@]}" <<'PY'
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

batch_output_dir = Path(sys.argv[1])
datasets = sys.argv[2:]
summaries: list[dict[str, object]] = []
for dataset in datasets:
    path = batch_output_dir / dataset / "run_summaries.json"
    if not path.exists():
        continue
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        summaries.extend(item for item in data if isinstance(item, dict))
summaries = sorted(
    summaries,
    key=lambda item: (str(item.get("dataset", "")), int(item.get("trial_number", 0))),
)
(batch_output_dir / "run_summaries.json").write_text(
    json.dumps(summaries, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
fieldnames = [
    "dataset",
    "trial_number",
    "seed",
    "total_budget",
    "init_size",
    "actual_evaluations",
    "initial_best",
    "final_best",
    "best_value",
    "best_row_index",
    "trace_path",
    "metadata_path",
    "results_path",
    "output_dir",
]
with (batch_output_dir / "run_summaries.csv").open("w", encoding="utf-8", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=fieldnames)
    writer.writeheader()
    for item in summaries:
        writer.writerow({key: item.get(key) for key in fieldnames})
print(f"[GP-Hedge] Wrote aggregate summary with {len(summaries)} runs")
PY

echo "============================================================"
echo "[GP-Hedge] 8-dataset batch complete."
echo "[GP-Hedge] Batch output root=${BATCH_OUTPUT_DIR}"
echo "[GP-Hedge] Summary JSON=${BATCH_OUTPUT_DIR}/run_summaries.json"
echo "[GP-Hedge] Summary CSV=${BATCH_OUTPUT_DIR}/run_summaries.csv"
echo "============================================================"
