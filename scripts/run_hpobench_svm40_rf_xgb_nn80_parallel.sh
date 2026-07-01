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

DATASETS=(hpobench_svm hpobench_rf hpobench_xgb hpobench_nn)
GPU_DEVICES=(cuda:2 cuda:3 cuda:4 cuda:5)
EXPERIMENT_ROUNDS="${EXPERIMENT_ROUNDS:-2}"
REPEATS_PER_DATASET="${REPEATS_PER_DATASET:-1}"
TOTAL_CPU_THREAD_CAP="${TOTAL_CPU_THREAD_CAP:-80}"
BASE_RUN_SEED="${BASE_RUN_SEED:-42}"
RUN_SEED_STEP="${RUN_SEED_STEP:-1000}"
START_RUN_INDEX_BASE="${START_RUN_INDEX:-1}"
BATCH_OUTPUT_DIR="${BATCH_OUTPUT_DIR:-${ROOT_DIR}/outputs/hpobench_4datasets_40_10init_parallel_$(date +%Y%m%d_%H%M%S)}"
HPOBENCH_DATA_DIR="${ROOT_DIR}/data/HPOBench"

if [[ ! "${EXPERIMENT_ROUNDS}" =~ ^[0-9]+$ ]] || (( EXPERIMENT_ROUNDS < 1 )); then
  echo "EXPERIMENT_ROUNDS must be a positive integer; got: ${EXPERIMENT_ROUNDS}" >&2
  exit 1
fi

if [[ ! "${REPEATS_PER_DATASET}" =~ ^[0-9]+$ ]] || (( REPEATS_PER_DATASET < 1 )); then
  echo "REPEATS_PER_DATASET must be a positive integer; got: ${REPEATS_PER_DATASET}" >&2
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

DATASET_COUNT="${#DATASETS[@]}"
if [[ ! "${TOTAL_CPU_THREAD_CAP}" =~ ^[0-9]+$ ]] || (( TOTAL_CPU_THREAD_CAP < DATASET_COUNT )); then
  echo "TOTAL_CPU_THREAD_CAP must be an integer >= ${DATASET_COUNT}; got: ${TOTAL_CPU_THREAD_CAP}" >&2
  exit 1
fi

PER_JOB_CPU_THREAD_CAP="${PER_JOB_CPU_THREAD_CAP:-${CPU_THREAD_CAP:-$((TOTAL_CPU_THREAD_CAP / DATASET_COUNT))}}"
if [[ ! "${PER_JOB_CPU_THREAD_CAP}" =~ ^[0-9]+$ ]] || (( PER_JOB_CPU_THREAD_CAP < 1 )); then
  echo "PER_JOB_CPU_THREAD_CAP/CPU_THREAD_CAP must be a positive integer; got: ${PER_JOB_CPU_THREAD_CAP}" >&2
  exit 1
fi

if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON_CMD=("${PYTHON_BIN}")
elif [[ "${CONDA_DEFAULT_ENV:-}" == "chembo" && -n "${CONDA_PREFIX:-}" ]]; then
  PYTHON_CMD=("${CONDA_PREFIX}/bin/python")
elif [[ -x "/home/sunyuxiang/miniconda3/envs/chembo/bin/python" ]]; then
  PYTHON_CMD=("/home/sunyuxiang/miniconda3/envs/chembo/bin/python")
elif [[ -x "/home/sunyuxiang/anaconda3/envs/chembo/bin/python" ]]; then
  PYTHON_CMD=("/home/sunyuxiang/anaconda3/envs/chembo/bin/python")
else
  PYTHON_CMD=("python")
fi

mkdir -p "${BATCH_OUTPUT_DIR}/logs"

"${PYTHON_CMD[@]}" - "${HPOBENCH_DATA_DIR}" <<'PY'
from __future__ import annotations

import csv
import sys
from pathlib import Path

data_dir = Path(sys.argv[1]).resolve()
expected = {
    "hpobench_svm_146212.csv": (
        ["C", "gamma", "test_acc", "test_loss", "seed_count", "seeds", "row_id"],
        441,
    ),
    "hpobench_rf_146606.csv": (
        ["max_depth", "max_features", "min_samples_leaf", "min_samples_split", "test_acc", "test_loss", "seed_count", "seeds", "row_id"],
        9000,
    ),
    "hpobench_xgb_146606.csv": (
        ["colsample_bytree", "eta", "max_depth", "reg_lambda", "test_acc", "test_loss", "seed_count", "seeds", "row_id"],
        9000,
    ),
    "hpobench_nn_168912.csv": (
        ["alpha", "batch_size", "depth", "learning_rate_init", "width", "test_acc", "test_loss", "seed_count", "seeds", "row_id"],
        30000,
    ),
}
for filename, (expected_header, expected_rows) in expected.items():
    path = data_dir / filename
    if not path.exists():
        raise SystemExit(f"Missing processed HPOBench CSV: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise SystemExit(f"Empty processed HPOBench CSV: {path}") from exc
        row_count = sum(1 for _ in reader)
    if header != expected_header:
        raise SystemExit(
            f"Unexpected HPOBench CSV header for {path}:\n"
            f"  got      {header}\n"
            f"  expected {expected_header}"
        )
    if row_count != expected_rows:
        raise SystemExit(
            f"Unexpected HPOBench CSV row count for {path}: got {row_count}, expected {expected_rows}"
        )
print(f"[ChemBO] Verified processed max-fidelity HPOBench CSVs in {data_dir}")
PY

echo "[ChemBO] HPOBench parallel batch"
echo "[ChemBO] LLM model=deepseek-v4-pro; thinking enabled on existing thinking nodes"
echo "[ChemBO] Datasets=${DATASETS[*]}"
echo "[ChemBO] Budgets: all datasets=40/10 warm start"
echo "[ChemBO] HPOBench data dir=${HPOBENCH_DATA_DIR}"
echo "[ChemBO] GPUs: ${GPU_DEVICES[*]}"
echo "[ChemBO] Experiment rounds=${EXPERIMENT_ROUNDS}; repeats per dataset per round=${REPEATS_PER_DATASET}"
echo "[ChemBO] Start run index base=${START_RUN_INDEX_BASE}; base seed=${BASE_RUN_SEED}; seed step=${RUN_SEED_STEP}"
echo "[ChemBO] Per-job CPU thread cap=${PER_JOB_CPU_THREAD_CAP}"
echo "[ChemBO] Batch output root=${BATCH_OUTPUT_DIR}"

for ROUND in $(seq 1 "${EXPERIMENT_ROUNDS}"); do
  ROUND_START_INDEX="$((START_RUN_INDEX_BASE + (ROUND - 1) * REPEATS_PER_DATASET))"
  ROUND_SEED="$((BASE_RUN_SEED + (ROUND_START_INDEX - 1) * RUN_SEED_STEP))"
  echo "============================================================"
  echo "[ChemBO] Launching HPOBench round ${ROUND}/${EXPERIMENT_ROUNDS} with start_run_index=${ROUND_START_INDEX}, seed=${ROUND_SEED}"
  echo "============================================================"

  PIDS=()
  PID_LABELS=()
  PID_LOGS=()

  for INDEX in "${!DATASETS[@]}"; do
    DATASET="${DATASETS[$INDEX]}"
    GPU="${GPU_DEVICES[$INDEX]}"
    DATASET_LABEL="$(printf '%s' "${DATASET}" | tr '[:lower:]' '[:upper:]')"
    DATASET_OUTPUT_DIR="${BATCH_OUTPUT_DIR}/${DATASET}"
    LOG_FILE="${BATCH_OUTPUT_DIR}/logs/round$(printf '%02d' "${ROUND}")_${DATASET}.log"
    BUDGET=40
    WARM_START=10
    TASK_SUFFIX="40_10init"

    case "${DATASET}" in
      hpobench_svm)
        PROBLEM_FILE="${ROOT_DIR}/examples/hpobench_svm_146212_problem.yaml"
        ;;
      hpobench_rf)
        PROBLEM_FILE="${ROOT_DIR}/examples/hpobench_rf_146606_problem.yaml"
        ;;
      hpobench_xgb)
        PROBLEM_FILE="${ROOT_DIR}/examples/hpobench_xgb_146606_problem.yaml"
        ;;
      hpobench_nn)
        PROBLEM_FILE="${ROOT_DIR}/examples/hpobench_nn_168912_problem.yaml"
        ;;
      *)
        echo "Unsupported dataset in script: ${DATASET}" >&2
        exit 1
        ;;
    esac

    echo "[ChemBO][${DATASET_LABEL}] Round ${ROUND} launching budget=${BUDGET} warm_start=${WARM_START} problem=${PROBLEM_FILE} gpu=${GPU}; log=${LOG_FILE}"
    (
      export DATASET_NAME="${DATASET}"
      export PROBLEM_FILE="${PROBLEM_FILE}"
      export REPEATS="${REPEATS_PER_DATASET}"
      export BUDGET="${BUDGET}"
      export WARM_START="${WARM_START}"
      export START_RUN_INDEX="${ROUND_START_INDEX}"
      export BASE_RUN_SEED="${BASE_RUN_SEED}"
      export RUN_SEED_STEP="${RUN_SEED_STEP}"
      export OUTPUT_DIR="${DATASET_OUTPUT_DIR}"
      export TASK_NAME="${DATASET}_${TASK_SUFFIX}_pr_no_ablation_ensemble_af"
      export PYTHON_BIN="${PYTHON_CMD[0]}"
      export CHEMBO_LLM_MODEL="deepseek-v4-pro"
      export CHEMBO_LLM_BASE_URL="https://api.deepseek.com"
      export CHEMBO_LLM_API_KEY_ENV="DEEPSEEK_API_KEY"
      export CHEMBO_LLM_ENABLE_THINKING="true"
      export BO_TORCH_DEVICE="${GPU}"
      export CHEMBO_BO_TORCH_DEVICE="${GPU}"
      export CHEMBO_BO_TORCH_DEVICES="cuda:2,cuda:3,cuda:4,cuda:5"
      export CPU_THREAD_CAP="${PER_JOB_CPU_THREAD_CAP}"
      export OMP_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
      export MKL_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
      export OPENBLAS_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
      export NUMEXPR_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
      export BLIS_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
      export RAYON_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
      export NUMBA_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
      "${RUNNER}"
    ) >"${LOG_FILE}" 2>&1 &

    PIDS+=("$!")
    PID_LABELS+=("${DATASET_LABEL}")
    PID_LOGS+=("${LOG_FILE}")
  done

  FAILED=0
  for INDEX in "${!PIDS[@]}"; do
    PID="${PIDS[$INDEX]}"
    LABEL="${PID_LABELS[$INDEX]}"
    LOG_FILE="${PID_LOGS[$INDEX]}"
    if wait "${PID}"; then
      echo "[ChemBO][${LABEL}] round ${ROUND} completed successfully"
    else
      STATUS="$?"
      echo "[ChemBO][${LABEL}] round ${ROUND} failed with exit code ${STATUS}; see ${LOG_FILE}" >&2
      FAILED=1
    fi
  done

  if (( FAILED != 0 )); then
    echo "[ChemBO] One or more HPOBench jobs failed in round ${ROUND}." >&2
    exit 1
  fi
done

SUMMARY_JSON="${BATCH_OUTPUT_DIR}/run_summaries.json"
SUMMARY_CSV="${BATCH_OUTPUT_DIR}/run_summaries.csv"

"${PYTHON_CMD[@]}" - "${SUMMARY_JSON}" "${SUMMARY_CSV}" "${BATCH_OUTPUT_DIR}" "${DATASETS[@]}" <<'PY'
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

summary_json_path = Path(sys.argv[1])
summary_csv_path = Path(sys.argv[2])
batch_output_dir = Path(sys.argv[3])
datasets = sys.argv[4:]

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
    key=lambda item: (str(item.get("dataset_name", "")), int(item.get("run_index", 0))),
)
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

echo "============================================================"
echo "[ChemBO] HPOBench parallel batch complete."
echo "[ChemBO] Batch output root=${BATCH_OUTPUT_DIR}"
echo "[ChemBO] Summary JSON=${SUMMARY_JSON}"
echo "[ChemBO] Summary CSV=${SUMMARY_CSV}"
echo "============================================================"
