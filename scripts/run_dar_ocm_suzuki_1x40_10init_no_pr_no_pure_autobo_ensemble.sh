#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="${ROOT_DIR}/scripts/run_dataset_3x40_10init_no_pr_no_pure_autobo_ensemble.sh"

if [[ ! -x "${RUNNER}" ]]; then
  echo "Dataset runner is not executable: ${RUNNER}" >&2
  exit 1
fi

DATASETS=(dar ocm suzuki)
EXPERIMENT_ROUNDS="${EXPERIMENT_ROUNDS:-3}"
REPEATS="${REPEATS:-1}"
TOTAL_CPU_THREAD_CAP="${TOTAL_CPU_THREAD_CAP:-90}"
BASE_RUN_SEED="${BASE_RUN_SEED:-42}"
RUN_SEED_STEP="${RUN_SEED_STEP:-1000}"
BATCH_OUTPUT_DIR="${BATCH_OUTPUT_DIR:-${ROOT_DIR}/outputs/dar_ocm_suzuki_3x_parallel_40_10init_pr_no_ablation_ensemble_af_$(date +%Y%m%d_%H%M%S)}"
DATASET_COUNT="${#DATASETS[@]}"

if [[ ! "${EXPERIMENT_ROUNDS}" =~ ^[0-9]+$ ]] || (( EXPERIMENT_ROUNDS < 1 )); then
  echo "EXPERIMENT_ROUNDS must be a positive integer; got: ${EXPERIMENT_ROUNDS}" >&2
  exit 1
fi

if [[ ! "${REPEATS}" =~ ^[0-9]+$ ]] || (( REPEATS < 1 )); then
  echo "REPEATS must be a positive integer; got: ${REPEATS}" >&2
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

if [[ ! "${TOTAL_CPU_THREAD_CAP}" =~ ^[0-9]+$ ]]; then
  echo "TOTAL_CPU_THREAD_CAP must be a positive integer; got: ${TOTAL_CPU_THREAD_CAP}" >&2
  exit 1
fi

if (( TOTAL_CPU_THREAD_CAP < DATASET_COUNT )); then
  echo "TOTAL_CPU_THREAD_CAP=${TOTAL_CPU_THREAD_CAP} is too small for ${DATASET_COUNT} parallel datasets." >&2
  exit 1
fi

MAX_PER_DATASET_CPU_THREAD_CAP="$((TOTAL_CPU_THREAD_CAP / DATASET_COUNT))"
REQUESTED_PER_DATASET_CPU_THREAD_CAP="${PER_DATASET_CPU_THREAD_CAP:-${CPU_THREAD_CAP:-${MAX_PER_DATASET_CPU_THREAD_CAP}}}"
if [[ ! "${REQUESTED_PER_DATASET_CPU_THREAD_CAP}" =~ ^[0-9]+$ ]]; then
  echo "PER_DATASET_CPU_THREAD_CAP/CPU_THREAD_CAP must be a positive integer; got: ${REQUESTED_PER_DATASET_CPU_THREAD_CAP}" >&2
  exit 1
fi

if (( REQUESTED_PER_DATASET_CPU_THREAD_CAP < 1 )); then
  echo "PER_DATASET_CPU_THREAD_CAP/CPU_THREAD_CAP must be at least 1." >&2
  exit 1
fi

if (( REQUESTED_PER_DATASET_CPU_THREAD_CAP > MAX_PER_DATASET_CPU_THREAD_CAP )); then
  PER_DATASET_CPU_THREAD_CAP="${MAX_PER_DATASET_CPU_THREAD_CAP}"
else
  PER_DATASET_CPU_THREAD_CAP="${REQUESTED_PER_DATASET_CPU_THREAD_CAP}"
fi

mkdir -p "${BATCH_OUTPUT_DIR}/logs"

echo "[ChemBO] Parallel dataset rounds: ${DATASETS[*]}"
echo "[ChemBO] Experiment rounds=${EXPERIMENT_ROUNDS}; per-dataset repeats=${REPEATS}"
echo "[ChemBO] Base seed=${BASE_RUN_SEED}; seed step=${RUN_SEED_STEP}"
echo "[ChemBO] Batch output root=${BATCH_OUTPUT_DIR}"
echo "[ChemBO] Total CPU thread cap=${TOTAL_CPU_THREAD_CAP}; per-dataset CPU thread cap=${PER_DATASET_CPU_THREAD_CAP}"

for ROUND in $(seq 1 "${EXPERIMENT_ROUNDS}"); do
  ROUND_START_INDEX="$(((ROUND - 1) * REPEATS + 1))"
  ROUND_SEED="$((BASE_RUN_SEED + (ROUND_START_INDEX - 1) * RUN_SEED_STEP))"
  echo "============================================================"
  echo "[ChemBO] Launching round ${ROUND}/${EXPERIMENT_ROUNDS} with start_run_index=${ROUND_START_INDEX}, seed=${ROUND_SEED}"
  echo "============================================================"

  PIDS=()
  for DATASET in "${DATASETS[@]}"; do
    DATASET_LABEL="$(printf '%s' "${DATASET}" | tr '[:lower:]' '[:upper:]')"
    DATASET_OUTPUT_DIR="${BATCH_OUTPUT_DIR}/${DATASET}"
    LOG_FILE="${BATCH_OUTPUT_DIR}/logs/round$(printf '%02d' "${ROUND}")_${DATASET}.log"
    echo "[ChemBO][${DATASET_LABEL}] Round ${ROUND} launching; output=${DATASET_OUTPUT_DIR}; log=${LOG_FILE}"

    (
      export DATASET_NAME="${DATASET}"
      export REPEATS="${REPEATS}"
      export START_RUN_INDEX="${ROUND_START_INDEX}"
      export BASE_RUN_SEED="${BASE_RUN_SEED}"
      export RUN_SEED_STEP="${RUN_SEED_STEP}"
      export OUTPUT_DIR="${DATASET_OUTPUT_DIR}"
      export TASK_NAME="${DATASET}_3x40_10init_pr_no_ablation_ensemble_af"
      export CPU_THREAD_CAP="${PER_DATASET_CPU_THREAD_CAP}"
      export OMP_NUM_THREADS="${PER_DATASET_CPU_THREAD_CAP}"
      export MKL_NUM_THREADS="${PER_DATASET_CPU_THREAD_CAP}"
      export OPENBLAS_NUM_THREADS="${PER_DATASET_CPU_THREAD_CAP}"
      export NUMEXPR_NUM_THREADS="${PER_DATASET_CPU_THREAD_CAP}"
      export BLIS_NUM_THREADS="${PER_DATASET_CPU_THREAD_CAP}"
      export RAYON_NUM_THREADS="${PER_DATASET_CPU_THREAD_CAP}"
      export NUMBA_NUM_THREADS="${PER_DATASET_CPU_THREAD_CAP}"
      "${RUNNER}"
    ) >"${LOG_FILE}" 2>&1 &
    PIDS+=("$!")
  done

  FAILED=0
  for INDEX in "${!PIDS[@]}"; do
    DATASET="${DATASETS[$INDEX]}"
    PID="${PIDS[$INDEX]}"
    LOG_FILE="${BATCH_OUTPUT_DIR}/logs/round$(printf '%02d' "${ROUND}")_${DATASET}.log"
    if wait "${PID}"; then
      echo "[ChemBO][${DATASET^^}] Round ${ROUND} completed successfully"
    else
      STATUS="$?"
      echo "[ChemBO][${DATASET^^}] Round ${ROUND} failed with exit code ${STATUS}; see ${LOG_FILE}" >&2
      FAILED=1
    fi
  done

  if (( FAILED != 0 )); then
    echo "[ChemBO] One or more datasets failed in round ${ROUND}." >&2
    exit 1
  fi
done

echo "============================================================"
echo "[ChemBO] Parallel dataset rounds complete: ${DATASETS[*]}"
echo "[ChemBO] Batch output root=${BATCH_OUTPUT_DIR}"
echo "============================================================"
