#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export WANDB_MODE="${WANDB_MODE:-disabled}"
export HF_HOME="${HF_HOME:-/data/shared/huggingface}"
export TRANSFORMERS_CACHE="${TRANSFORMERS_CACHE:-${HF_HOME}/hub}"
export HUGGINGFACE_HUB_CACHE="${HUGGINGFACE_HUB_CACHE:-${HF_HOME}/hub}"
export GOLLUM_CUDA_VISIBLE_DEVICES="${GOLLUM_CUDA_VISIBLE_DEVICES:-2}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-${GOLLUM_CUDA_VISIBLE_DEVICES}}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export CPU_THREAD_CAP="${CPU_THREAD_CAP:-20}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-${CPU_THREAD_CAP}}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-${CPU_THREAD_CAP}}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-${CPU_THREAD_CAP}}"
export NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-${CPU_THREAD_CAP}}"
export BLIS_NUM_THREADS="${BLIS_NUM_THREADS:-${CPU_THREAD_CAP}}"

echo "[GOLLuM][SUZUKI] Starting run on CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES}, budget=40, trials=3, cpu_threads=${CPU_THREAD_CAP}"
conda run --no-capture-output -n gollum python "${ROOT_DIR}/baseline/gollum/run_tabular_gollum.py" \
  --dataset suzuki \
  --trials 3 \
  --total-budget 40 \
  --output-dir "${ROOT_DIR}/outputs/baseline_runs/gollum/suzuki"
