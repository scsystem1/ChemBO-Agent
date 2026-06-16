#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export REASONINGBO_LLM_MODEL="${REASONINGBO_LLM_MODEL:-kimi-k2.5-thinking}"
export REASONINGBO_BASE_URL="${REASONINGBO_BASE_URL:-https://dashscope.aliyuncs.com/compatible-mode/v1}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-40}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-40}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-40}"
export NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-40}"

echo "[ReasoningBO][OER] Starting run with model=${REASONINGBO_LLM_MODEL}, total_budget=40, initial points decided by LLM, reasoning_batch_size=3, trials=3"
conda run --no-capture-output -n reasoning_bo python "${ROOT_DIR}/baseline/Reasoning-BO/run_tabular_reasoning_bo.py" \
  --dataset oer \
  --trials 3 \
  --seed-start "${SEED_START:-0}" \
  --trial-start-index "${TRIAL_START_INDEX:-1}" \
  --total-budget 40 \
  --reasoning-batch-size "${REASONING_BATCH_SIZE:-3}" \
  --output-dir "${ROOT_DIR}/outputs/baseline_runs/reasoning_bo/oer"
