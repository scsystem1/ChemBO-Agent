#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="${ROOT_DIR}/scripts/run_dataset_3x40_10init_no_pr_no_pure_autobo_ensemble.sh"

if [[ ! -x "${RUNNER}" ]]; then
  echo "Dataset runner is not executable: ${RUNNER}" >&2
  exit 1
fi

DATASETS=(dar ocm suzuki)
REPEATS="${REPEATS:-1}"
TOTAL_CPU_THREAD_CAP="${TOTAL_CPU_THREAD_CAP:-90}"
DATASET_COUNT="${#DATASETS[@]}"

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

echo "[ChemBO] Parallel dataset run: ${DATASETS[*]}"
echo "[ChemBO] Per-dataset repeats=${REPEATS}"
echo "[ChemBO] Total CPU thread cap=${TOTAL_CPU_THREAD_CAP}; per-dataset CPU thread cap=${PER_DATASET_CPU_THREAD_CAP}"

PIDS=()
for DATASET in "${DATASETS[@]}"; do
  DATASET_LABEL="$(printf '%s' "${DATASET}" | tr '[:lower:]' '[:upper:]')"
  echo "============================================================"
  echo "[ChemBO][${DATASET_LABEL}] Launching"
  echo "============================================================"

  (
    export DATASET_NAME="${DATASET}"
    export REPEATS="${REPEATS}"
    export CPU_THREAD_CAP="${PER_DATASET_CPU_THREAD_CAP}"
    export OMP_NUM_THREADS="${PER_DATASET_CPU_THREAD_CAP}"
    export MKL_NUM_THREADS="${PER_DATASET_CPU_THREAD_CAP}"
    export OPENBLAS_NUM_THREADS="${PER_DATASET_CPU_THREAD_CAP}"
    export NUMEXPR_NUM_THREADS="${PER_DATASET_CPU_THREAD_CAP}"
    export BLIS_NUM_THREADS="${PER_DATASET_CPU_THREAD_CAP}"
    export RAYON_NUM_THREADS="${PER_DATASET_CPU_THREAD_CAP}"
    export NUMBA_NUM_THREADS="${PER_DATASET_CPU_THREAD_CAP}"
    "${RUNNER}"
  ) &
  PIDS+=("$!")
done

FAILED=0
for INDEX in "${!PIDS[@]}"; do
  DATASET="${DATASETS[$INDEX]}"
  PID="${PIDS[$INDEX]}"
  if wait "${PID}"; then
    echo "[ChemBO][${DATASET^^}] Completed successfully"
  else
    STATUS="$?"
    echo "[ChemBO][${DATASET^^}] Failed with exit code ${STATUS}" >&2
    FAILED=1
  fi
done

if (( FAILED != 0 )); then
  echo "[ChemBO] One or more parallel dataset runs failed." >&2
  exit 1
fi

echo "============================================================"
echo "[ChemBO] Parallel dataset run complete: ${DATASETS[*]}"
echo "============================================================"
