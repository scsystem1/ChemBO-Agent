#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export REPEATS_PER_DATASET="${REPEATS_PER_DATASET:-5}"
exec "${ROOT_DIR}/scripts/run_gp_hedge_8datasets_3x40_10init_parallel.sh" "$@"
