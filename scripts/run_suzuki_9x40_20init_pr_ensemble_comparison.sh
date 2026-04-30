#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_PROBLEM="${ROOT_DIR}/examples/suzuki_problem.yaml"
DEFAULT_CONFIG="${ROOT_DIR}/dashscope_kimi.yaml"
REPEATS="${REPEATS:-3}"
BUDGET="${BUDGET:-40}"
WARM_START="${WARM_START:-20}"
OUTPUT_DIR="${OUTPUT_DIR:-${ROOT_DIR}/outputs/suzuki_9x40_20init_pr_ensemble_comparison}"
TASK_NAME_OVERRIDE="${TASK_NAME:-suzuki_9x40_20init_pr_ensemble_comparison}"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON_CMD=("${PYTHON_BIN}")
elif [[ "${CONDA_DEFAULT_ENV:-}" == "chembo" && -n "${CONDA_PREFIX:-}" ]]; then
  PYTHON_CMD=("${CONDA_PREFIX}/bin/python")
elif [[ -x "/Users/stevens/anaconda3/envs/chembo/bin/python" ]]; then
  PYTHON_CMD=("/Users/stevens/anaconda3/envs/chembo/bin/python")
else
  PYTHON_CMD=("python")
fi

PROBLEM_FILE="${1:-$DEFAULT_PROBLEM}"
CONFIG_FILE="${2:-$DEFAULT_CONFIG}"

if [[ ! -f "${PROBLEM_FILE}" ]]; then
  echo "Problem file not found: ${PROBLEM_FILE}" >&2
  exit 1
fi

if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "Config file not found: ${CONFIG_FILE}" >&2
  exit 1
fi

mkdir -p "${OUTPUT_DIR}"

SUMMARY_JSON="${OUTPUT_DIR}/run_summaries.json"
SUMMARY_CSV="${OUTPUT_DIR}/run_summaries.csv"

"${PYTHON_CMD[@]}" - "${ROOT_DIR}" "${PROBLEM_FILE}" "${CONFIG_FILE}" "${OUTPUT_DIR}" "${TASK_NAME_OVERRIDE}" "${REPEATS}" "${BUDGET}" "${WARM_START}" "${SUMMARY_JSON}" "${SUMMARY_CSV}" <<'PY'
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

root_dir = Path(sys.argv[1]).resolve()
problem_path = Path(sys.argv[2]).resolve()
config_path = Path(sys.argv[3]).resolve()
output_dir = Path(sys.argv[4]).resolve()
task_name_override = sys.argv[5].strip()
repeats = int(sys.argv[6])
budget = int(sys.argv[7])
warm_start = int(sys.argv[8])
summary_json_path = Path(sys.argv[9]).resolve()
summary_csv_path = Path(sys.argv[10]).resolve()

sys.path.insert(0, str(root_dir))

from config.settings import Settings
from core.campaign_runner import _default_run_id, run_campaign
from core.graph import build_chembo_graph
from core.problem_loader import load_problem_file
from core.state import create_initial_state


RUN_SEEDS = {
    1: 20260423,
    2: 20261423,
    3: 20262423,
}


def _slugify(value: str) -> str:
    text = value.strip()
    if not text:
        return "unknown"
    normalized = []
    last_dash = False
    for char in text:
        if char.isalnum() or char in {".", "_", "-"}:
            normalized.append(char)
            last_dash = False
        elif not last_dash:
            normalized.append("-")
            last_dash = True
    result = "".join(normalized).strip("-._")
    return result or "unknown"


def _run_mode(
    *,
    mode_name: str,
    pure_reasoning_enabled: bool,
    ensemble_af_enabled: bool,
    mode_output_dir: Path,
    experiment_name: str,
) -> list[dict[str, object]]:
    base_problem = load_problem_file(problem_path)
    if not isinstance(base_problem, dict):
        raise RuntimeError("Suzuki comparison script expects a structured YAML/JSON problem file.")

    if repeats > len(RUN_SEEDS):
        raise RuntimeError(
            f"Configured repeats={repeats}, but only {len(RUN_SEEDS)} fixed run seeds are defined."
        )

    summaries: list[dict[str, object]] = []
    for run_index in range(1, repeats + 1):
        run_id = f"run{run_index:02d}"
        run_seed = RUN_SEEDS[run_index]

        print("============================================================")
        print(f"Suzuki mode: {mode_name} | repeat {run_index}/{repeats}")
        print(f"Problem file: {problem_path}")
        print(f"Config file: {config_path}")
        print(f"Output root: {mode_output_dir}")
        print(f"Budget override: {budget}")
        print(f"Warm start override: {warm_start}")
        print(f"Run id: {run_id}")
        print(f"Run seed: {run_seed}")
        print(
            "Settings override: "
            f"pure_reasoning_ablation_enabled={pure_reasoning_enabled}, "
            f"ensemble_af={ensemble_af_enabled}"
        )
        print("============================================================")

        settings = Settings.from_yaml(str(config_path)) if config_path.exists() else Settings()
        settings.max_bo_iterations = budget
        settings.initial_doe_size = warm_start
        settings.random_seed = run_seed
        settings.pure_reasoning_ablation_enabled = pure_reasoning_enabled
        settings.ensemble_af = ensemble_af_enabled
        settings.output_dir = str(mode_output_dir)
        settings.experiment_name = _slugify(experiment_name)
        settings.experiment_id = run_id

        problem = json.loads(json.dumps(base_problem))
        problem["budget"] = budget

        graph = build_chembo_graph(settings)
        initial_state = create_initial_state(problem, settings, problem_source_path=str(problem_path))
        resolved_run_id = _default_run_id(initial_state, settings)
        state = run_campaign(
            graph,
            initial_state,
            settings,
            thread_id=resolved_run_id,
            printer=print,
        )

        final_summary = state.get("final_summary") or {}
        summary = {
            "mode": mode_name,
            "run_index": run_index,
            "run_id": resolved_run_id,
            "run_seed": run_seed,
            "budget": budget,
            "warm_start": settings.initial_doe_size,
            "best_result": state.get("best_result"),
            "best_candidate": state.get("best_candidate"),
            "proposal_strategy": final_summary.get("proposal_strategy"),
            "stop_reason": final_summary.get("stop_reason"),
            "pure_reasoning_ablation_enabled": settings.pure_reasoning_ablation_enabled,
            "ensemble_af": settings.ensemble_af,
            "output_dir": str(mode_output_dir / resolved_run_id),
        }
        summaries.append(summary)
        print("Run summary:")
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    return summaries


mode_specs = [
    {
        "mode_name": "no_pure_reasoning_with_ensemble",
        "pure_reasoning_enabled": False,
        "ensemble_af_enabled": True,
        "output_dir": output_dir / "no_pure_reasoning_with_ensemble",
        "experiment_name": f"{task_name_override}_ensemble",
    },
    {
        "mode_name": "no_pure_reasoning_no_ensemble",
        "pure_reasoning_enabled": False,
        "ensemble_af_enabled": False,
        "output_dir": output_dir / "no_pure_reasoning_no_ensemble",
        "experiment_name": f"{task_name_override}_baseline",
    },
    {
        "mode_name": "pure_reasoning_no_ensemble",
        "pure_reasoning_enabled": True,
        "ensemble_af_enabled": False,
        "output_dir": output_dir / "pure_reasoning_no_ensemble",
        "experiment_name": f"{task_name_override}_pure_reasoning_no_ensemble",
    },
]

all_summaries = []
for mode_spec in mode_specs:
    mode_output_dir = mode_spec["output_dir"]
    mode_output_dir.mkdir(parents=True, exist_ok=True)
    all_summaries.extend(
        _run_mode(
            mode_name=mode_spec["mode_name"],
            pure_reasoning_enabled=mode_spec["pure_reasoning_enabled"],
            ensemble_af_enabled=mode_spec["ensemble_af_enabled"],
            mode_output_dir=mode_output_dir,
            experiment_name=mode_spec["experiment_name"],
        )
    )

summary_json_path.write_text(json.dumps(all_summaries, ensure_ascii=False, indent=2), encoding="utf-8")

with summary_csv_path.open("w", encoding="utf-8", newline="") as handle:
    writer = csv.DictWriter(
        handle,
        fieldnames=[
            "mode",
            "run_index",
            "run_id",
            "run_seed",
            "budget",
            "warm_start",
            "best_result",
            "proposal_strategy",
            "stop_reason",
            "pure_reasoning_ablation_enabled",
            "ensemble_af",
            "output_dir",
        ],
    )
    writer.writeheader()
    for item in all_summaries:
        writer.writerow({key: item.get(key) for key in writer.fieldnames})

print("============================================================")
print("Combined batch summary written:")
print(summary_json_path)
print(summary_csv_path)
print("============================================================")
PY
