#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="${ROOT_DIR}/scripts/run_dataset_3x40_10init_no_pr_no_pure_autobo_ensemble.sh"

if [[ ! -x "${RUNNER}" ]]; then
  echo "Dataset runner is not executable: ${RUNNER}" >&2
  exit 1
fi

DATASETS=(hpobench_svm hpobench_rf hpobench_xgb hpobench_nn)
EXPERIMENT_ROUNDS="${EXPERIMENT_ROUNDS:-1}"
REPEATS="${REPEATS:-1}"
TOTAL_CPU_THREAD_CAP="${TOTAL_CPU_THREAD_CAP:-64}"
BASE_RUN_SEED="${BASE_RUN_SEED:-42}"
RUN_SEED_STEP="${RUN_SEED_STEP:-1000}"
BATCH_OUTPUT_DIR="${BATCH_OUTPUT_DIR:-${ROOT_DIR}/outputs/hpobench_4x_parallel_40_10init_pr_no_ablation_ensemble_af_$(date +%Y%m%d_%H%M%S)}"
DATASET_COUNT="${#DATASETS[@]}"

if (( TOTAL_CPU_THREAD_CAP < DATASET_COUNT )); then
  echo "TOTAL_CPU_THREAD_CAP=${TOTAL_CPU_THREAD_CAP} is too small for ${DATASET_COUNT} parallel datasets." >&2
  exit 1
fi

PER_DATASET_CPU_THREAD_CAP="${PER_DATASET_CPU_THREAD_CAP:-$((TOTAL_CPU_THREAD_CAP / DATASET_COUNT))}"
mkdir -p "${BATCH_OUTPUT_DIR}/logs"

echo "[ChemBO] HPOBench parallel datasets: ${DATASETS[*]}"
echo "[ChemBO] Before running, ensure official CSV/YAML files exist by running:"
echo "  python scripts/build_hpobench_tabular.py --models svm rf xgb nn"
echo "[ChemBO] Defaults: svm/nn use credit-g task 31, rf uses car task 146821, xgb uses segment task 146822."
echo "[ChemBO] Output root=${BATCH_OUTPUT_DIR}"

for ROUND in $(seq 1 "${EXPERIMENT_ROUNDS}"); do
  ROUND_START_INDEX="$(((ROUND - 1) * REPEATS + 1))"
  echo "============================================================"
  echo "[ChemBO] Launching HPOBench round ${ROUND}/${EXPERIMENT_ROUNDS}"
  echo "============================================================"
  PIDS=()
  for DATASET in "${DATASETS[@]}"; do
    DATASET_OUTPUT_DIR="${BATCH_OUTPUT_DIR}/${DATASET}"
    LOG_FILE="${BATCH_OUTPUT_DIR}/logs/round$(printf '%02d' "${ROUND}")_${DATASET}.log"
    (
      export DATASET_NAME="${DATASET}"
      export REPEATS="${REPEATS}"
      export START_RUN_INDEX="${ROUND_START_INDEX}"
      export BASE_RUN_SEED="${BASE_RUN_SEED}"
      export RUN_SEED_STEP="${RUN_SEED_STEP}"
      export OUTPUT_DIR="${DATASET_OUTPUT_DIR}"
      export TASK_NAME="${DATASET}_40_10init_pr_no_ablation_ensemble_af"
      export AUTOBO_DESCRIPTOR_ENABLED=false
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
    echo "[ChemBO][${DATASET}] launched log=${LOG_FILE}"
  done

  FAILED=0
  for INDEX in "${!PIDS[@]}"; do
    DATASET="${DATASETS[$INDEX]}"
    PID="${PIDS[$INDEX]}"
    if wait "${PID}"; then
      echo "[ChemBO][${DATASET}] round ${ROUND} completed"
    else
      STATUS="$?"
      echo "[ChemBO][${DATASET}] round ${ROUND} failed with exit code ${STATUS}" >&2
      FAILED=1
    fi
  done
  if (( FAILED != 0 )); then
    exit 1
  fi
done

echo "[ChemBO] HPOBench batch complete: ${BATCH_OUTPUT_DIR}"
