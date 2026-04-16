from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def _slugify(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    normalized: list[str] = []
    previous_dash = False
    for char in text:
        if char.isalnum() or char in {".", "_", "-"}:
            normalized.append(char)
            previous_dash = False
        elif not previous_dash:
            normalized.append("-")
            previous_dash = True
    slug = "".join(normalized).strip("-._")
    return slug or "unknown"


def _parse_methods(raw: str) -> list[str]:
    methods = [item.strip() for item in str(raw or "").split(",") if item.strip()]
    if not methods:
        raise ValueError("At least one embedding method must be provided.")
    return methods


def _float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _best_so_far_curve(results: list[float], direction: str) -> list[float]:
    curve: list[float] = []
    if direction == "minimize":
        best = float("inf")
        for value in results:
            best = min(best, value)
            curve.append(best)
    else:
        best = float("-inf")
        for value in results:
            best = max(best, value)
            curve.append(best)
    return curve


def _auc(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))


def _read_experiment_results(run_dir: Path) -> list[float]:
    csv_path = run_dir / "experiment_records.csv"
    if not csv_path.exists():
        return []
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        results: list[float] = []
        for row in reader:
            value = _float_or_none(row.get("result"))
            if value is not None:
                results.append(value)
    return results


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _last_graph_update_node(run_dir: Path) -> str | None:
    log_path = run_dir / "run_log.jsonl"
    if not log_path.exists():
        return None
    last_node: str | None = None
    with log_path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if payload.get("event_type") == "graph_update" and payload.get("node"):
                last_node = str(payload.get("node"))
    return last_node


def _phase_resume_node(state: dict[str, Any]) -> str:
    phase = str(state.get("phase") or "").strip().lower()
    return {
        "parsing": "parse_input",
        "selecting_embedding": "select_embedding",
        "hypothesizing": "generate_hypotheses",
        "configuring": "configure_bo",
        "warm_starting": "warm_start",
        "running": "run_bo_iteration",
        "selecting_candidate": "select_candidate",
        "awaiting_human": "await_human_results",
        "interpreting": "interpret_results",
        "reflecting": "reflect_and_decide",
        "reconfiguring": "reconfig_gate",
        "summarizing": "campaign_summary",
        "completed": "campaign_summary",
    }.get(phase, "parse_input")


def _existing_run_state(run_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    state = _read_json(run_dir / "final_state.json")
    if state is None:
        return None, None
    last_node = _last_graph_update_node(run_dir) or _phase_resume_node(state)
    return state, last_node


def _mean(values: list[float]) -> float | None:
    return float(statistics.mean(values)) if values else None


def _std(values: list[float]) -> float | None:
    return float(statistics.stdev(values)) if len(values) >= 2 else 0.0 if values else None


def _render_bar(ratio: float, width: int = 24) -> str:
    clamped = max(0.0, min(1.0, float(ratio)))
    filled = int(round(clamped * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _format_duration(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "--:--:--"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class _ProgressReporter:
    _ITER_RE = re.compile(r"^\[(?P<node>[^\]]+)\]\s+iter\s+(?P<used>\d+)/(?P<budget>\d+)")

    def __init__(self, total_runs: int, budget_per_run: int):
        self.total_runs = max(1, int(total_runs))
        self.budget_per_run = max(1, int(budget_per_run))
        self.total_planned_experiments = self.total_runs * self.budget_per_run
        self.start_time = time.monotonic()
        self.completed_runs = 0
        self.completed_experiments = 0
        self.current_run_index = 0
        self.current_method = ""
        self.current_repeat = 0
        self.current_run_id = ""
        self.current_budget = self.budget_per_run
        self.current_used = 0
        self.current_node = "pending"
        self.current_run_start = self.start_time
        self._last_render = ""

    def start_run(
        self,
        run_index: int,
        method: str,
        repeat_index: int,
        run_id: str,
        *,
        starting_used: int = 0,
        budget: int | None = None,
        resumed: bool = False,
        skipped: bool = False,
    ) -> None:
        self.current_run_index = max(1, int(run_index))
        self.current_method = str(method)
        self.current_repeat = int(repeat_index)
        self.current_run_id = str(run_id)
        self.current_budget = max(1, int(budget or self.budget_per_run))
        self.current_used = max(0, int(starting_used))
        self.current_node = "resume" if resumed else "start"
        self.current_run_start = time.monotonic()
        status = "skipping completed run" if skipped else ("resuming saved run" if resumed else "starting run")
        print(
            f"\n[{self.current_run_index}/{self.total_runs}] {status}: "
            f"{self.current_method} repeat {self.current_repeat} ({self.current_run_id})",
            flush=True,
        )
        self.render(force=True)

    def advance_completed_run(self, used_observations: int) -> None:
        self.current_used = max(0, int(used_observations))
        self.current_node = "completed"
        self.render(force=True)
        self.completed_runs += 1
        self.completed_experiments += self.current_used
        self.current_used = 0
        print("", flush=True)

    def printer(self, line: str) -> None:
        text = str(line or "").strip()
        if not text:
            return
        match = self._ITER_RE.match(text)
        if match:
            self.current_node = match.group("node")
            self.current_used = max(self.current_used, int(match.group("used")))
            self.current_budget = max(1, int(match.group("budget")))
            self.render()
            return
        if text.startswith("Run log:") or text.startswith("Run timeline:") or text.startswith("Experiment CSV:") or text.startswith("Iteration config CSV:") or text.startswith("LLM trace:"):
            return
        print(f"\n{text}", flush=True)
        self.render(force=True)

    def render(self, *, force: bool = False) -> None:
        overall_completed = self.completed_experiments + self.current_used
        overall_ratio = overall_completed / max(self.total_planned_experiments, 1)
        run_ratio = self.current_used / max(self.current_budget, 1)
        elapsed = time.monotonic() - self.start_time
        eta = None
        if overall_completed > 0:
            rate = overall_completed / max(elapsed, 1e-6)
            remaining = max(self.total_planned_experiments - overall_completed, 0)
            eta = remaining / rate if rate > 0 else None
        message = (
            f"\rRun {self.current_run_index}/{self.total_runs} "
            f"{_render_bar(run_ratio)} {self.current_used}/{self.current_budget} "
            f"| Overall {_render_bar(overall_ratio)} {overall_completed}/{self.total_planned_experiments} "
            f"| Node={self.current_node} | ETA={_format_duration(eta)}"
        )
        if force or message != self._last_render:
            print(message, end="", flush=True)
            self._last_render = message


def compare_embeddings(
    problem_path: Path,
    config_path: Path | None,
    methods: list[str],
    repeats: int,
    output_root: Path,
    experiment_name: str | None = None,
    llm_temperature: float | None = None,
    disable_knowledge: bool = True,
    resume: bool = True,
) -> dict[str, Any]:
    from config.settings import Settings
    from core.campaign_runner import _default_run_id, run_campaign
    from core.graph import build_chembo_graph
    from core.problem_loader import load_problem_file, resolve_campaign_budget
    from core.state import create_initial_state

    problem = load_problem_file(problem_path)
    direction = str((problem or {}).get("optimization_direction") or "maximize").strip().lower()
    experiment_slug = _slugify(experiment_name or problem_path.stem)
    ablation_root = output_root / f"{experiment_slug}_embedding_compare"
    ablation_root.mkdir(parents=True, exist_ok=True)
    budget = int(resolve_campaign_budget(problem, Settings.from_yaml(str(config_path)) if config_path else Settings()))

    run_rows: list[dict[str, Any]] = []
    trajectory_rows: list[dict[str, Any]] = []
    total_runs = len(methods) * repeats
    progress = _ProgressReporter(total_runs=total_runs, budget_per_run=budget)
    run_index = 0

    for method in methods:
        for repeat_index in range(1, repeats + 1):
            run_index += 1
            settings = Settings.from_yaml(str(config_path)) if config_path else Settings()
            settings.output_dir = str(ablation_root)
            settings.experiment_name = f"{experiment_slug}_{_slugify(method)}"
            settings.experiment_id = f"run{repeat_index:02d}"
            settings.force_embedding_method = method
            if disable_knowledge:
                settings.enable_knowledge_augmentation = False
                settings.enable_runtime_retrieval = False
            if llm_temperature is not None:
                settings.llm_temperature = float(llm_temperature)

            graph = build_chembo_graph(settings)
            initial_state = create_initial_state(problem, settings, problem_source_path=str(problem_path))
            run_id = _default_run_id(initial_state, settings)
            run_dir = ablation_root / run_id
            resume_state = None
            resume_as_node = None
            existing_state, existing_node = _existing_run_state(run_dir) if resume else (None, None)
            if existing_state is not None and str(existing_state.get("phase") or "").strip().lower() == "completed":
                progress.start_run(
                    run_index,
                    method,
                    repeat_index,
                    run_id,
                    starting_used=len(existing_state.get("observations", []) or []),
                    budget=budget,
                    skipped=True,
                )
                state = existing_state
                progress.advance_completed_run(len(existing_state.get("observations", []) or []))
            else:
                if existing_state is not None:
                    resume_state = existing_state
                    resume_as_node = existing_node
                progress.start_run(
                    run_index,
                    method,
                    repeat_index,
                    run_id,
                    starting_used=len((existing_state or {}).get("observations", []) or []),
                    budget=budget,
                    resumed=resume_state is not None,
                )
                state = run_campaign(
                    graph,
                    initial_state,
                    settings,
                    thread_id=run_id,
                    printer=progress.printer,
                    resume_state=resume_state,
                    resume_as_node=resume_as_node,
                )
                progress.advance_completed_run(len(state.get("observations", []) or []))

            results = _read_experiment_results(run_dir)
            curve = _best_so_far_curve(results, direction)
            final_best = _float_or_none(state.get("best_result"))
            auc = _auc(curve)

            run_rows.append(
                {
                    "embedding_method": method,
                    "repeat_index": repeat_index,
                    "run_id": run_id,
                    "best_result": final_best,
                    "best_so_far_auc": auc,
                    "num_experiments": len(results),
                    "output_dir": str(run_dir),
                    "best_candidate": json.dumps(state.get("best_candidate"), ensure_ascii=False),
                }
            )

            for iteration, value in enumerate(curve, start=1):
                trajectory_rows.append(
                    {
                        "embedding_method": method,
                        "repeat_index": repeat_index,
                        "run_id": run_id,
                        "iteration": iteration,
                        "best_so_far": value,
                    }
                )

    summary_rows: list[dict[str, Any]] = []
    for method in methods:
        method_rows = [row for row in run_rows if row["embedding_method"] == method]
        final_values = [float(row["best_result"]) for row in method_rows if row["best_result"] is not None]
        auc_values = [float(row["best_so_far_auc"]) for row in method_rows if row["best_so_far_auc"] is not None]
        summary_rows.append(
            {
                "embedding_method": method,
                "repeats": len(method_rows),
                "best_result_mean": _mean(final_values),
                "best_result_std": _std(final_values),
                "best_result_max": max(final_values) if final_values else None,
                "best_result_min": min(final_values) if final_values else None,
                "best_so_far_auc_mean": _mean(auc_values),
                "best_so_far_auc_std": _std(auc_values),
            }
        )

    run_csv = ablation_root / "embedding_run_summary.csv"
    trajectory_csv = ablation_root / "embedding_trajectory.csv"
    aggregate_csv = ablation_root / "embedding_aggregate_summary.csv"
    payload_path = ablation_root / "embedding_compare_summary.json"

    _write_csv(
        run_csv,
        [
            "embedding_method",
            "repeat_index",
            "run_id",
            "best_result",
            "best_so_far_auc",
            "num_experiments",
            "output_dir",
            "best_candidate",
        ],
        run_rows,
    )
    _write_csv(
        trajectory_csv,
        ["embedding_method", "repeat_index", "run_id", "iteration", "best_so_far"],
        trajectory_rows,
    )
    _write_csv(
        aggregate_csv,
        [
            "embedding_method",
            "repeats",
            "best_result_mean",
            "best_result_std",
            "best_result_max",
            "best_result_min",
            "best_so_far_auc_mean",
            "best_so_far_auc_std",
        ],
        summary_rows,
    )

    payload = {
        "problem_file": str(problem_path),
        "config_file": str(config_path) if config_path else None,
        "methods": methods,
        "repeats": repeats,
        "disable_knowledge": disable_knowledge,
        "resume": resume,
        "optimization_direction": direction,
        "output_dir": str(ablation_root),
        "run_summary_csv": str(run_csv),
        "trajectory_csv": str(trajectory_csv),
        "aggregate_summary_csv": str(aggregate_csv),
        "summary": summary_rows,
        "runs": run_rows,
    }
    payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare ChemBO embedding methods with repeated campaigns.")
    parser.add_argument("--problem-file", required=True, help="Path to YAML problem file.")
    parser.add_argument("--config", default=None, help="Optional settings YAML.")
    parser.add_argument(
        "--methods",
        default="one_hot,fingerprint_concat,physicochemical_descriptors",
        help="Comma-separated embedding keys to compare.",
    )
    parser.add_argument("--repeats", type=int, default=3, help="Number of repeated runs per embedding.")
    parser.add_argument("--output-dir", default=str(ROOT / "outputs"), help="Root output directory.")
    parser.add_argument("--experiment-name", default=None, help="Optional experiment name prefix.")
    parser.add_argument(
        "--disable-knowledge",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Disable local knowledge augmentation and runtime retrieval during embedding comparison.",
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Resume incomplete runs from saved state and reuse completed runs when available.",
    )
    parser.add_argument(
        "--llm-temperature",
        type=float,
        default=None,
        help="Optional override to reduce run-to-run LLM randomness during comparison.",
    )
    args = parser.parse_args()

    payload = compare_embeddings(
        problem_path=Path(args.problem_file).resolve(),
        config_path=Path(args.config).resolve() if args.config else None,
        methods=_parse_methods(args.methods),
        repeats=max(1, int(args.repeats)),
        output_root=Path(args.output_dir).resolve(),
        experiment_name=args.experiment_name,
        llm_temperature=args.llm_temperature,
        disable_knowledge=bool(args.disable_knowledge),
        resume=bool(args.resume),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
