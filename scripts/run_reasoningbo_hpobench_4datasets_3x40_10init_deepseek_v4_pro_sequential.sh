#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="${ROOT_DIR}/baseline/Reasoning-BO/run_tabular_reasoning_bo.py"

if [[ ! -f "${RUNNER}" ]]; then
  echo "Reasoning-BO runner not found: ${RUNNER}" >&2
  exit 1
fi

if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
  echo "DEEPSEEK_API_KEY is not set. Export it before launching this script." >&2
  exit 1
fi

HPO_DATASETS=(hpobench_svm hpobench_rf hpobench_xgb hpobench_nn)
CHEM_DATASETS=(ocm dar suzuki oer)
DATASETS=("${HPO_DATASETS[@]}" "${CHEM_DATASETS[@]}")
REPEATS_PER_DATASET="${REPEATS_PER_DATASET:-3}"
MAX_ATTEMPTS_PER_DATASET="${MAX_ATTEMPTS_PER_DATASET:-5}"
DATASET_GROUP_SIZE="${DATASET_GROUP_SIZE:-2}"
TOTAL_CPU_THREAD_CAP="${TOTAL_CPU_THREAD_CAP:-90}"
BASE_RUN_SEED="${BASE_RUN_SEED:-42}"
RUN_SEED_STEP="${RUN_SEED_STEP:-1000}"
START_RUN_INDEX_BASE="${START_RUN_INDEX:-1}"
BATCH_OUTPUT_DIR="${BATCH_OUTPUT_DIR:-${ROOT_DIR}/outputs/baseline_runs/reasoning_bo/hpobench_chem_8datasets_3x40_10init_deepseek_v4_pro_$(date +%Y%m%d_%H%M%S)}"
HPOBENCH_DATA_DIR="${ROOT_DIR}/data/HPOBench"

if [[ ! "${REPEATS_PER_DATASET}" =~ ^[0-9]+$ ]] || (( REPEATS_PER_DATASET < 1 )); then
  echo "REPEATS_PER_DATASET must be a positive integer; got: ${REPEATS_PER_DATASET}" >&2
  exit 1
fi

if [[ ! "${MAX_ATTEMPTS_PER_DATASET}" =~ ^[0-9]+$ ]] || (( MAX_ATTEMPTS_PER_DATASET < REPEATS_PER_DATASET )); then
  echo "MAX_ATTEMPTS_PER_DATASET must be an integer >= REPEATS_PER_DATASET; got: ${MAX_ATTEMPTS_PER_DATASET}" >&2
  exit 1
fi

if [[ ! "${DATASET_GROUP_SIZE}" =~ ^[0-9]+$ ]] || (( DATASET_GROUP_SIZE < 1 )); then
  echo "DATASET_GROUP_SIZE must be a positive integer; got: ${DATASET_GROUP_SIZE}" >&2
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

mkdir -p "${BATCH_OUTPUT_DIR}/logs"

conda run --no-capture-output -n reasoning_bo python - "${HPOBENCH_DATA_DIR}" <<'PY'
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
print(f"[ReasoningBO] Verified processed max-fidelity HPOBench CSVs in {data_dir}")
PY

conda run --no-capture-output -n reasoning_bo python - "${ROOT_DIR}" "${DATASETS[@]}" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
sys.path.append(str(root / "baseline"))

from common.tabular_benchmarks import load_benchmark_spec

for dataset in sys.argv[2:]:
    spec, df = load_benchmark_spec(root, dataset)
    if df.empty:
        raise SystemExit(f"Dataset is empty: {dataset}")
    if not spec.feature_columns:
        raise SystemExit(f"Dataset has no feature columns: {dataset}")
    print(
        f"[ReasoningBO] Loaded {dataset}: rows={len(df)}, "
        f"target={spec.target_column}, features={spec.feature_columns}"
    )
PY

export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export REASONINGBO_REASONER="deepseek"
export DEEPSEEK_API_BASE="${DEEPSEEK_API_BASE:-https://api.deepseek.com}"
export DEEPSEEK_MODEL_NAME="${DEEPSEEK_MODEL_NAME:-deepseek-v4-pro}"

echo "[ReasoningBO] HPOBench + Chem DeepSeek v4 pro batch"
echo "[ReasoningBO] HPO datasets run first: ${HPO_DATASETS[*]}"
echo "[ReasoningBO] Chem datasets run after HPO: ${CHEM_DATASETS[*]}"
echo "[ReasoningBO] Per-dataset target successes=${REPEATS_PER_DATASET}; max attempts per dataset=${MAX_ATTEMPTS_PER_DATASET}; total_budget=40"
echo "[ReasoningBO] Dataset group size=${DATASET_GROUP_SIZE}; max concurrent experiments=$((DATASET_GROUP_SIZE * REPEATS_PER_DATASET))"
echo "[ReasoningBO] DeepSeek base=${DEEPSEEK_API_BASE}; model=${DEEPSEEK_MODEL_NAME}"
echo "[ReasoningBO] Start run index base=${START_RUN_INDEX_BASE}; base seed=${BASE_RUN_SEED}; seed step=${RUN_SEED_STEP}"
echo "[ReasoningBO] Per-job CPU thread cap=${PER_JOB_CPU_THREAD_CAP}"
echo "[ReasoningBO] Batch output root=${BATCH_OUTPUT_DIR}"

success_count() {
  local dataset="$1"
  local dataset_output_dir="${BATCH_OUTPUT_DIR}/${dataset}"
  local count=0
  local path
  for path in "${dataset_output_dir}"/trial_*/"${dataset}_reasoning_bo_summary.json"; do
    if [[ -f "${path}" ]]; then
      count="$((count + 1))"
    fi
  done
  printf '%s\n' "${count}"
}

max_attempt_index() {
  local dataset="$1"
  local dataset_output_dir="${BATCH_OUTPUT_DIR}/${dataset}"
  local max_index="$((START_RUN_INDEX_BASE - 1))"
  local dir base index
  for dir in "${dataset_output_dir}"/trial_*; do
    [[ -d "${dir}" ]] || continue
    base="$(basename "${dir}")"
    index="${base#trial_}"
    if [[ "${index}" =~ ^[0-9]+$ ]] && (( index > max_index )); then
      max_index="${index}"
    fi
  done
  printf '%s\n' "${max_index}"
}

write_aggregate_summary() {
  conda run --no-capture-output -n reasoning_bo python - "${BATCH_OUTPUT_DIR}" "${DATASETS[@]}" <<'PY'
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

batch_output_dir = Path(sys.argv[1])
datasets = sys.argv[2:]
summaries: list[dict[str, object]] = []
for dataset in datasets:
    for path in sorted((batch_output_dir / dataset).glob("trial_*/**/*_reasoning_bo_summary.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["summary_path"] = str(path)
        summaries.append(data)
summaries = sorted(
    summaries,
    key=lambda item: (str(item.get("dataset", "")), str(item.get("trial_numbers", []))),
)
(batch_output_dir / "run_summaries.json").write_text(
    json.dumps(summaries, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
fieldnames = [
    "dataset",
    "trials",
    "trial_numbers",
    "target_budget",
    "actual_evaluations_per_trial",
    "reasoning_batch_size",
    "initial_mean",
    "final_mean",
    "final_std",
    "reasoner",
    "llm_model",
    "summary_path",
]
with (batch_output_dir / "run_summaries.csv").open("w", encoding="utf-8", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=fieldnames)
    writer.writeheader()
    for item in summaries:
        writer.writerow({key: item.get(key) for key in fieldnames})
print(f"[ReasoningBO] Wrote aggregate summary with {len(summaries)} runs")
PY
}

run_dataset_group() {
  local group=("$@")
  local max_run_index="$((START_RUN_INDEX_BASE + MAX_ATTEMPTS_PER_DATASET - 1))"

  echo "============================================================"
  echo "[ReasoningBO] Starting dataset group: ${group[*]}"
  echo "============================================================"

  while true; do
    local pids=()
    local labels=()
    local logs=()
    local launched=0
    local all_done=1
    local dataset dataset_label dataset_output_dir successes max_index remaining available launch_count run_index run_seed run_output_dir log_file

    for dataset in "${group[@]}"; do
      dataset_label="$(printf '%s' "${dataset}" | tr '[:lower:]' '[:upper:]')"
      dataset_output_dir="${BATCH_OUTPUT_DIR}/${dataset}"
      mkdir -p "${dataset_output_dir}"

      successes="$(success_count "${dataset}")"
      max_index="$(max_attempt_index "${dataset}")"

      if (( successes >= REPEATS_PER_DATASET )); then
        echo "[ReasoningBO][${dataset_label}] already complete: successes=${successes}/${REPEATS_PER_DATASET}, max_trial=${max_index}"
        continue
      fi

      if (( max_index >= max_run_index )); then
        echo "[ReasoningBO][${dataset_label}] skipping after max attempts: successes=${successes}/${REPEATS_PER_DATASET}, max_trial=${max_index}" >&2
        continue
      fi

      all_done=0
      remaining="$((REPEATS_PER_DATASET - successes))"
      available="$((max_run_index - max_index))"
      launch_count="${remaining}"
      if (( launch_count > available )); then
        launch_count="${available}"
      fi

      for OFFSET in $(seq 1 "${launch_count}"); do
        run_index="$((max_index + OFFSET))"
        run_seed="$((BASE_RUN_SEED + (run_index - 1) * RUN_SEED_STEP))"
        run_output_dir="${dataset_output_dir}/trial_${run_index}"
        log_file="${BATCH_OUTPUT_DIR}/logs/${dataset}_trial$(printf '%02d' "${run_index}").log"

        echo "[ReasoningBO][${dataset_label}] Launching trial_$(printf '%02d' "${run_index}") seed=${run_seed}; log=${log_file}"
        (
          export OMP_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
          export MKL_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
          export OPENBLAS_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
          export NUMEXPR_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
          export BLIS_NUM_THREADS="${PER_JOB_CPU_THREAD_CAP}"
          conda run --no-capture-output -n reasoning_bo python "${RUNNER}" \
            --dataset "${dataset}" \
            --reasoner deepseek \
            --trials 1 \
            --seed-start "${run_seed}" \
            --trial-start-index "${run_index}" \
            --total-budget 40 \
            --reasoning-batch-size "${REASONING_BATCH_SIZE:-3}" \
            --output-dir "${run_output_dir}"
        ) >"${log_file}" 2>&1 &

        pids+=("$!")
        labels+=("${dataset_label} trial_$(printf '%02d' "${run_index}")")
        logs+=("${log_file}")
        launched="$((launched + 1))"
      done
    done

    if (( launched == 0 )); then
      if (( all_done != 0 )); then
        echo "[ReasoningBO] Dataset group complete: ${group[*]}"
      else
        echo "[ReasoningBO] Dataset group exhausted retries: ${group[*]}" >&2
      fi
      break
    fi

    local failed=0
    local index pid label status
    for index in "${!pids[@]}"; do
      pid="${pids[$index]}"
      label="${labels[$index]}"
      log_file="${logs[$index]}"
      if wait "${pid}"; then
        echo "[ReasoningBO][${label}] completed successfully"
      else
        status="$?"
        echo "[ReasoningBO][${label}] failed with exit code ${status}; see ${log_file}" >&2
        failed=1
      fi
    done

    if (( failed != 0 )); then
      echo "[ReasoningBO] Continuing after failure; failed dataset(s) will be retried until trial_$(printf '%02d' "${max_run_index}")" >&2
    fi
  done
}

for ((GROUP_START = 0; GROUP_START < ${#DATASETS[@]}; GROUP_START += DATASET_GROUP_SIZE)); do
  GROUP=("${DATASETS[@]:GROUP_START:DATASET_GROUP_SIZE}")
  run_dataset_group "${GROUP[@]}"
  write_aggregate_summary
done

echo "============================================================"
echo "[ReasoningBO] HPOBench + Chem DeepSeek v4 pro batch complete."
echo "[ReasoningBO] Batch output root=${BATCH_OUTPUT_DIR}"
echo "[ReasoningBO] Summary JSON=${BATCH_OUTPUT_DIR}/run_summaries.json"
echo "[ReasoningBO] Summary CSV=${BATCH_OUTPUT_DIR}/run_summaries.csv"
echo "============================================================"
