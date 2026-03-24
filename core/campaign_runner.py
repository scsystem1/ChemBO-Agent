"""
Shared campaign execution loop for CLI and tests.
"""
from __future__ import annotations

from typing import Callable

from langgraph.types import Command

from core.dataset_oracle import DatasetOracle


def run_campaign(
    graph,
    initial_state,
    settings,
    thread_id: str | None = None,
    printer: Callable[[str], None] | None = None,
    input_func: Callable[[str], str] = input,
):
    """Run a full campaign, auto-resuming interrupt steps when configured."""
    config = {"configurable": {"thread_id": thread_id or settings.experiment_id}}
    graph.invoke(initial_state, config=config)
    state = graph.get_state(config).values

    while state["phase"] != "completed":
        proposal = state.get("current_proposal", {})
        candidates = proposal.get("candidates") or []
        if not candidates:
            raise RuntimeError("Campaign paused without a candidate proposal to execute.")

        candidate = candidates[0]
        response = _resolve_experiment_response(candidate, state.get("problem_spec", {}), settings, printer, input_func)
        graph.invoke(Command(resume=response), config=config)
        state = graph.get_state(config).values

    return state


def _resolve_experiment_response(
    candidate: dict,
    problem_spec: dict,
    settings,
    printer: Callable[[str], None] | None,
    input_func: Callable[[str], str],
) -> dict:
    oracle = DatasetOracle.from_problem_spec(problem_spec) if _use_dataset_auto(settings, problem_spec) else None
    if oracle is not None:
        matched = oracle.lookup(candidate)
        row_id = matched["metadata"].get("dataset_row_id")
        if printer is not None:
            suffix = f" (row {row_id})" if row_id not in (None, "") else ""
            printer(f"Dataset oracle observed yield={matched['result']}{suffix}")
        return {
            "result": matched["result"],
            "notes": "dataset_auto",
            "metadata": matched["metadata"],
        }

    if printer is not None:
        printer("\nProposed experiment:")
        printer(str(candidate))
    result = float(input_func("Enter measured result: ").strip())
    notes = input_func("Enter notes (optional): ").strip()
    return {"result": result, "notes": notes}


def _use_dataset_auto(settings, problem_spec: dict) -> bool:
    mode = str(getattr(settings, "human_input_mode", "terminal")).strip().lower()
    has_dataset = isinstance(problem_spec.get("dataset"), dict)
    if mode == "dataset_auto":
        return has_dataset
    return False
