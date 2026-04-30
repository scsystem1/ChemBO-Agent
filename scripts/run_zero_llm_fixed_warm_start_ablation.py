#!/bin/sh
""":"
exec python3 "$0" "$@"
":"""

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config.settings import Settings
from core.campaign_runner import _default_run_id, run_campaign
from core.graph import build_chembo_graph
from core.problem_loader import load_problem_file
from core.state import create_initial_state
from core.zero_llm_ablation import (
    load_zero_llm_manifest,
    manifest_records_for_domain_run,
    write_zero_llm_combined_experiment_csv,
)


RUN_SEEDS = {
    1: 20260423,
    2: 20261423,
    3: 20262423,
}

DEFAULT_MANIFEST_PATH = ROOT_DIR / "data/zero_llm_fixed_warm_start_manifest.json"

DEFAULT_DOMAIN_SPECS = {
    "dar": {
        "problem_file": ROOT_DIR / "examples/dar_problem.yaml",
        "config_file": ROOT_DIR / "dashscope_kimi.yaml",
    },
    "ocm": {
        "problem_file": ROOT_DIR / "examples/ocm_problem.yaml",
        "config_file": ROOT_DIR / "dashscope_kimi_ocm.yaml",
    },
    "suzuki": {
        "problem_file": ROOT_DIR / "examples/suzuki_problem.yaml",
        "config_file": ROOT_DIR / "dashscope_kimi.yaml",
    },
}


def _slugify(value: str) -> str:
    text = value.strip()
    if not text:
        return "unknown"
    normalized: list[str] = []
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


def _domain_output_dir(output_root: Path, domain: str) -> Path:
    path = output_root / domain
    path.mkdir(parents=True, exist_ok=True)
    return path


def _silence_rdkit_warnings() -> None:
    try:
        from rdkit import RDLogger
    except Exception:
        return
    RDLogger.DisableLog("rdApp.warning")
    RDLogger.DisableLog("rdApp.error")


def run_zero_llm_ablation_batch(
    *,
    output_root: Path,
    manifest_path: Path,
    repeats: int = 3,
    budget: int = 40,
    warm_start_size: int = 20,
    task_name: str = "zero_llm_fixed_warm_start_ablation",
) -> list[dict[str, object]]:
    if repeats > len(RUN_SEEDS):
        raise RuntimeError(
            f"Configured repeats={repeats}, but only {len(RUN_SEEDS)} fixed run seeds are defined."
        )

    _silence_rdkit_warnings()
    output_root.mkdir(parents=True, exist_ok=True)
    manifest = load_zero_llm_manifest(manifest_path)
    summaries: list[dict[str, object]] = []
    combined_entries: list[dict[str, object]] = []

    for domain, spec in DEFAULT_DOMAIN_SPECS.items():
        base_problem = load_problem_file(spec["problem_file"])
        if not isinstance(base_problem, dict):
            raise RuntimeError(f"{domain.upper()} batch run expects a structured YAML/JSON problem file.")

        domain_output_dir = _domain_output_dir(output_root, domain)
        for run_index in range(1, repeats + 1):
            run_seed = RUN_SEEDS[run_index]
            run_label = f"run{run_index:02d}"

            print("============================================================")
            print(f"ZERO-LLM {domain.upper()} repeat {run_index}/{repeats}")
            print(f"Problem file: {spec['problem_file']}")
            print(f"Config file: {spec['config_file']}")
            print(f"Manifest path: {manifest_path}")
            print(f"Output root: {domain_output_dir}")
            print(f"Budget override: {budget}")
            print(f"Warm start size: {warm_start_size}")
            print(f"Run id: {run_label}")
            print(f"Run seed: {run_seed}")
            print("============================================================")

            settings = Settings.from_yaml(str(spec["config_file"])) if Path(spec["config_file"]).exists() else Settings()
            warm_start_records = manifest_records_for_domain_run(manifest, domain=domain, run_id=run_label)
            if len(warm_start_records) != warm_start_size:
                raise RuntimeError(
                    f"Manifest entry {domain}.{run_label} contains {len(warm_start_records)} records, "
                    f"expected {warm_start_size}."
                )
            settings.max_bo_iterations = budget
            settings.initial_doe_size = warm_start_size
            settings.random_seed = run_seed
            settings.output_dir = str(domain_output_dir)
            settings.experiment_name = _slugify(f"{task_name}_{domain}")
            settings.experiment_id = run_label
            settings.zero_llm_ablation_enabled = True
            settings.zero_llm_fixed_warm_start_source_dir = None
            settings.zero_llm_fixed_warm_start_records = warm_start_records
            settings.zero_llm_fixed_warm_start_manifest_path = None
            settings.zero_llm_fixed_warm_start_manifest_key = None
            settings.autobo_llm_acq_enabled = False
            settings.autobo_llm_plaus_enabled = False
            settings.memory_llm_consolidation_enabled = False
            settings.warm_start_per_point_llm_interpret = False
            settings.knowledge_enabled = False
            settings.ensemble_af = False
            settings.human_input_mode = "dataset_auto"

            problem = json.loads(json.dumps(base_problem))
            problem["budget"] = budget

            graph = build_chembo_graph(settings)
            initial_state = create_initial_state(problem, settings, problem_source_path=str(spec["problem_file"]))
            resolved_run_id = _default_run_id(initial_state, settings)
            state = run_campaign(
                graph,
                initial_state,
                settings,
                thread_id=resolved_run_id,
                printer=print,
            )

            run_dir = domain_output_dir / resolved_run_id
            experiment_csv_path = run_dir / "experiment_records.csv"
            final_summary = state.get("final_summary") or {}
            summary = {
                "domain": domain,
                "run_index": run_index,
                "run_id": resolved_run_id,
                "run_seed": run_seed,
                "budget": budget,
                "warm_start": warm_start_size,
                "best_result": state.get("best_result"),
                "best_candidate": state.get("best_candidate"),
                "proposal_strategy": final_summary.get("proposal_strategy"),
                "stop_reason": final_summary.get("stop_reason"),
                "output_dir": str(run_dir),
                "experiment_csv_path": str(experiment_csv_path),
            }
            summaries.append(summary)
            combined_entries.append(
                {
                    "domain": domain,
                    "run_id": resolved_run_id,
                    "experiment_csv_path": str(experiment_csv_path),
                }
            )
            print("Run summary:")
            print(json.dumps(summary, ensure_ascii=False, indent=2))

    combined_csv_path = write_zero_llm_combined_experiment_csv(output_root / "combined_experiment_records.csv", combined_entries)
    summary_json_path = output_root / "run_summaries.json"
    summary_csv_path = output_root / "run_summaries.csv"
    summary_json_path.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")

    with summary_csv_path.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "domain",
            "run_index",
            "run_id",
            "run_seed",
            "budget",
            "warm_start",
            "best_result",
            "proposal_strategy",
            "stop_reason",
            "output_dir",
            "experiment_csv_path",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in summaries:
            writer.writerow({name: row.get(name) for name in fieldnames})

    print("============================================================")
    print("Batch outputs written:")
    print(summary_json_path)
    print(summary_csv_path)
    print(combined_csv_path)
    print("============================================================")
    return summaries


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the zero-LLM fixed-warm-start AutoBO ablation.")
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(ROOT_DIR / "outputs/zero_llm_fixed_warm_start_ablation"),
        help="Directory to write per-run artifacts and the combined experiment CSV.",
    )
    parser.add_argument(
        "--manifest-path",
        type=str,
        default=str(DEFAULT_MANIFEST_PATH),
        help="Path to the checked-in zero-LLM fixed warm-start manifest JSON.",
    )
    parser.add_argument("--repeats", type=int, default=3, help="Number of repeats per domain.")
    parser.add_argument("--budget", type=int, default=40, help="Total experiment budget per run.")
    parser.add_argument("--warm-start-size", type=int, default=20, help="Fixed historical warm-start size.")
    parser.add_argument(
        "--task-name",
        type=str,
        default="zero_llm_fixed_warm_start_ablation",
        help="Task slug used in run ids.",
    )
    args = parser.parse_args()

    run_zero_llm_ablation_batch(
        output_root=Path(args.output_dir).expanduser().resolve(),
        manifest_path=Path(args.manifest_path).expanduser().resolve(),
        repeats=args.repeats,
        budget=args.budget,
        warm_start_size=args.warm_start_size,
        task_name=args.task_name,
    )


if __name__ == "__main__":
    main()
