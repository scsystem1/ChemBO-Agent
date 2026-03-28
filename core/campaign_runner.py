"""
Shared campaign execution loop for CLI and tests.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from langgraph.types import Command

from core.dataset_oracle import DatasetOracle
from core.problem_loader import resolve_campaign_budget


def run_campaign(
    graph,
    initial_state,
    settings,
    thread_id: str | None = None,
    printer: Callable[[str], None] | None = None,
    input_func: Callable[[str], str] = input,
):
    """Run a full campaign, auto-resuming interrupt steps when configured."""
    config = {"configurable": {"thread_id": thread_id or _default_run_id(initial_state, settings)}}
    run_logger = CampaignRunLogger(settings, config["configurable"]["thread_id"])
    run_logger.log_session_start(initial_state, config)
    if printer is not None:
        printer(f"Run log: {run_logger.log_path}")
        printer(f"Run timeline: {run_logger.timeline_path}")
    try:
        _stream_graph_updates(graph, initial_state, config, settings, printer, run_logger)
        state = graph.get_state(config).values

        while state["phase"] != "completed":
            proposal = state.get("current_proposal", {})
            candidates = proposal.get("candidates") or []
            if not candidates:
                raise RuntimeError("Campaign paused without a candidate proposal to execute.")

            candidate = candidates[0]
            response = _resolve_experiment_response(candidate, state.get("problem_spec", {}), settings, printer, input_func)
            run_logger.log_experiment_response(candidate, response, state)
            _stream_graph_updates(graph, Command(resume=response), config, settings, printer, run_logger)
            state = graph.get_state(config).values

        run_logger.log_session_end(state)
        return state
    except Exception as exc:
        run_logger.log_exception(exc)
        raise
    finally:
        run_logger.close()


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
        return {
            "result": matched["result"],
            "notes": "dataset_auto",
            "metadata": matched["metadata"],
        }

    result = float(input_func("Enter measured result: ").strip())
    notes = input_func("Enter notes (optional): ").strip()
    return {"result": result, "notes": notes}


def _use_dataset_auto(settings, problem_spec: dict) -> bool:
    mode = str(getattr(settings, "human_input_mode", "terminal")).strip().lower()
    has_dataset = isinstance(problem_spec.get("dataset"), dict)
    if mode == "dataset_auto":
        return has_dataset
    return False


def _stream_graph_updates(
    graph,
    payload,
    config: dict,
    settings,
    printer: Callable[[str], None] | None,
    run_logger: "CampaignRunLogger | None" = None,
) -> None:
    for event in graph.stream(payload, config=config, stream_mode="updates"):
        current_state = graph.get_state(config).values
        for node_name, update in event.items():
            progress_lines = format_progress_update(node_name, update, current_state, settings)
            if run_logger is not None:
                run_logger.log_graph_update(node_name, update, current_state, progress_lines)
            if printer is None:
                continue
            for line in progress_lines:
                printer(line)


def format_progress_update(node_name: str, update: Any, state: dict, settings) -> list[str]:
    budget = resolve_campaign_budget(state.get("problem_spec", {}), settings)
    observations = state.get("observations", [])
    used = len(observations)
    best_result = state.get("best_result")
    best_result_text = f"{best_result:.2f}" if isinstance(best_result, (int, float)) and best_result not in (float("inf"), float("-inf")) else "n/a"
    prefix = f"[{node_name}] iter {used}/{budget}"

    if not isinstance(update, dict):
        payload_text = _truncate(_json_inline(update), 220)
        if node_name == "__interrupt__":
            return [f"{prefix} interrupt payload={payload_text}"]
        return [f"{prefix} event payload={payload_text}"]

    if node_name == "parse_input":
        problem = state.get("problem_spec", {})
        return [f"{prefix} parsed reaction={problem.get('reaction_type') or 'unknown'} vars={len(problem.get('variables', []))} budget={budget}"]

    if node_name == "select_embedding":
        config_data = state.get("embedding_config", {})
        return [
            (
                f"{prefix} embedding={config_data.get('method', 'unknown')} dim={config_data.get('dim', '?')} "
                f"conf={config_data.get('confidence', 0.0):.2f}{_llm_usage_suffix(node_name, state)}"
            )
        ]

    if node_name in {"generate_hypotheses", "update_hypotheses"}:
        focus = state.get("memory", {}).get("working", {}).get("current_focus", "")
        return [f"{prefix} hypotheses={len(state.get('hypotheses', []))} focus={_truncate(focus, 100)}"]

    if node_name == "configure_bo":
        bo_config = state.get("bo_config", {})
        latest_reconfig = (state.get("reconfig_history") or [])[-1] if state.get("reconfig_history") else None
        accepted_text = ""
        if latest_reconfig is not None:
            accepted_text = f" accepted={latest_reconfig.get('accepted')}"
        return [
            (
                f"{prefix} configured surrogate={bo_config.get('surrogate_model')} "
                f"kernel={bo_config.get('kernel_config', {}).get('key')} "
                f"af={bo_config.get('acquisition_function')}{accepted_text}{_llm_usage_suffix(node_name, state)}"
            )
        ]

    if node_name == "warm_start":
        shortlist = state.get("proposal_shortlist", [])
        prior_guided = sum(1 for item in shortlist if item.get("warm_start_category") == "prior_guided")
        exploration = sum(1 for item in shortlist if item.get("warm_start_category") != "prior_guided")
        return [
            (
                f"{prefix} warm-start queued={len(shortlist)} prior_guided={prior_guided} "
                f"exploration={exploration}{_llm_usage_suffix(node_name, state)}"
            )
        ]

    if node_name in {"run_bo_iteration", "run_reasoning_iteration"}:
        shortlist = state.get("proposal_shortlist", [])
        top_candidate = shortlist[0].get("candidate", {}) if shortlist else {}
        payload = state.get("last_tool_payload", {})
        status = payload.get("status", "unknown")
        strategy = payload.get("strategy") or payload.get("metadata", {}).get("proposal_strategy") or "unknown"
        return [
            f"{prefix} shortlist={len(shortlist)} status={status} strategy={strategy} top={_candidate_brief(top_candidate)}"
        ]

    if node_name == "select_candidate":
        selected = state.get("proposal_selected", {})
        return [
            (
                f"{prefix} selected source={selected.get('selection_source', 'unknown')} "
                f"override={selected.get('override', False)} candidate={_candidate_brief(selected.get('candidate', {}))}"
                f"{_llm_usage_suffix(node_name, state)}"
            )
        ]

    if node_name == "await_human_results":
        latest = observations[-1] if observations else {}
        row_id = latest.get("metadata", {}).get("dataset_row_id")
        row_suffix = f" row={row_id}" if row_id not in (None, "") else ""
        return [f"{prefix} observed result={latest.get('result')} best={best_result_text}{row_suffix}"]

    if node_name == "interpret_results":
        latest_message = _truncate(_extract_interpretation_text(update), 220)
        return [f"{prefix} interpreted best={best_result_text}{_llm_usage_suffix(node_name, state)} note={latest_message}"]

    if node_name == "reflect_and_decide":
        convergence = state.get("convergence_state", {})
        return [
            (
                f"{prefix} decision={state.get('next_action', 'continue') or 'continue'} "
                f"stagnant={convergence.get('is_stagnant')} "
                f"best={best_result_text}{_llm_usage_suffix(node_name, state)}"
            )
        ]

    if node_name == "reconfig_gate":
        return [f"{prefix} {_truncate(_last_message_text(update), 120)}"]

    if node_name == "campaign_summary":
        summary = state.get("final_summary", {})
        token_usage = summary.get("llm_token_usage", {})
        token_suffix = ""
        if int(token_usage.get("total_tokens", 0)) > 0:
            token_suffix = (
                f" tokens={token_usage.get('input_tokens', 0)}/"
                f"{token_usage.get('output_tokens', 0)}/{token_usage.get('total_tokens', 0)}"
            )
        return [
            (
                f"{prefix} complete total={summary.get('total_experiments', used)} best={summary.get('best_result')} "
                f"strategy={summary.get('proposal_strategy', 'unknown')} "
                f"stop={_truncate(summary.get('stop_reason', ''), 100)}{token_suffix}"
            )
        ]

    return [f"{prefix} phase={state.get('phase', 'unknown')}"]


def _candidate_brief(candidate: dict) -> str:
    if not isinstance(candidate, dict) or not candidate:
        return "{}"
    items = list(candidate.items())[:3]
    text = ", ".join(f"{key}={value}" for key, value in items)
    if len(candidate) > 3:
        text += ", ..."
    return "{" + text + "}"


def _last_message_text(update: Any) -> str:
    messages = _update_messages(update)
    if not messages:
        return _json_inline(update) if not isinstance(update, dict) else ""
    return _message_text(messages[-1])


def _extract_interpretation_text(update: Any) -> str:
    messages = _update_messages(update)
    for message in reversed(messages):
        payload = _extract_json_dict(_message_text(message))
        if isinstance(payload, dict) and payload.get("interpretation"):
            return str(payload.get("interpretation", ""))
    return _last_message_text(update)


def _message_text(message) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(parts)
    return str(content)


def _extract_json_dict(text: str) -> dict | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _llm_usage_suffix(node_name: str, state: dict) -> str:
    usage = state.get("last_llm_usage", {})
    if usage.get("node") != node_name or int(usage.get("total_tokens", 0)) <= 0:
        return ""
    approx = "~" if usage.get("estimated") else ""
    return (
        f" tokens={approx}{usage.get('input_tokens', 0)}/"
        f"{usage.get('output_tokens', 0)}/{usage.get('total_tokens', 0)}"
    )


def _truncate(text: str, limit: int) -> str:
    raw = str(text or "").strip().replace("\n", " ")
    return raw if len(raw) <= limit else raw[: limit - 3] + "..."


class CampaignRunLogger:
    def __init__(self, settings, run_id: str):
        output_root = Path(str(getattr(settings, "output_dir", "./outputs"))).expanduser()
        self.run_dir = output_root / str(run_id)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.run_dir / "run_log.jsonl"
        self.timeline_path = self.run_dir / "timeline.md"
        self.final_summary_path = self.run_dir / "final_summary.json"
        self.final_state_path = self.run_dir / "final_state.json"
        self._handle = self.log_path.open("a", encoding="utf-8")
        self._timeline_handle = self.timeline_path.open("a", encoding="utf-8")
        self._settings = settings
        self._run_id = str(run_id)
        self._step_index = 0
        self._previous_state_digest: dict[str, Any] = {}
        self._previous_hypotheses: list[dict[str, Any]] = []
        if self.timeline_path.stat().st_size == 0:
            self._write_timeline(
                [
                    f"# ChemBO Run Timeline: `{self._run_id}`",
                    "",
                    f"- Started at: {_timestamp_now()}",
                    f"- JSONL log: `{self.log_path}`",
                    f"- Final summary: `{self.final_summary_path}`",
                    f"- Final state: `{self.final_state_path}`",
                    "",
                ]
            )

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()
        if not self._timeline_handle.closed:
            self._timeline_handle.close()

    def log_session_start(self, initial_state: dict, config: dict[str, Any]) -> None:
        settings_snapshot = _settings_snapshot(self._settings)
        problem_spec = initial_state.get("problem_spec", {}) if isinstance(initial_state, dict) else {}
        summary = "Initialized campaign session."
        outcome = [
            (
                f"model={settings_snapshot.get('llm_model') or 'unknown'} | "
                f"input_mode={settings_snapshot.get('human_input_mode') or 'unknown'} | "
                f"budget={resolve_campaign_budget(problem_spec, self._settings)}"
            ),
            _problem_overview(problem_spec),
        ]
        record = {
            "event_type": "session_start",
            "run_id": self._run_id,
            "summary": summary,
            "reasoning": [],
            "outcome": _section_items(outcome),
            "state_delta": {},
            "llm_usage": {},
            "config": _make_json_safe(config),
            "artifacts": _artifact_paths(self.log_path, self.timeline_path, self.final_summary_path, self.final_state_path),
        }
        self._emit_event(record, title="Session Start", meta=[f"Run: `{self._run_id}`"])
        self._previous_state_digest = _compact_state_digest(initial_state)
        self._previous_hypotheses = _make_json_safe(initial_state.get("hypotheses", []) or [])

    def log_graph_update(
        self,
        node_name: str,
        update: Any,
        current_state: dict[str, Any],
        progress_lines: list[str],
    ) -> None:
        self._step_index += 1
        current_digest = _compact_state_digest(current_state)
        state_delta = _state_delta(self._previous_state_digest, current_digest)
        event_details = _build_graph_event_details(
            node_name=node_name,
            update=update,
            current_state=current_state,
            progress_lines=progress_lines,
            previous_hypotheses=self._previous_hypotheses,
        )
        record = {
            "event_type": "graph_update",
            "step_index": self._step_index,
            "node": node_name,
            "phase": current_state.get("phase"),
            "iteration": current_state.get("iteration"),
            "summary": event_details["summary"],
            "reasoning": event_details["reasoning"],
            "outcome": event_details["outcome"],
            "state_delta": state_delta,
            "llm_usage": _graph_llm_usage(node_name, current_state),
        }
        meta = [
            f"Node: `{node_name}`",
            f"Phase: `{current_state.get('phase')}`",
            f"Iteration: `{current_state.get('iteration')}`",
        ]
        self._emit_event(record, title=f"Step {self._step_index}: `{node_name}`", meta=meta)
        self._previous_state_digest = current_digest
        self._previous_hypotheses = _make_json_safe(current_state.get("hypotheses", []) or [])

    def log_experiment_response(self, candidate: dict[str, Any], response: dict[str, Any], state: dict[str, Any]) -> None:
        source = "dataset_auto" if str(response.get("notes", "")).strip().lower() == "dataset_auto" else "human_input"
        result_value = response.get("result")
        summary = f"Queued experiment response for iteration {int(state.get('iteration', 0)) + 1}."
        outcome = [
            f"source={source} | result={_display_value(result_value)}",
            f"candidate={_candidate_brief(candidate)}",
        ]
        row_id = (response.get("metadata") or {}).get("dataset_row_id")
        if row_id not in (None, ""):
            outcome.append(f"dataset_row_id={row_id}")
        notes = str(response.get("notes", "")).strip()
        reasoning = [f"notes={_truncate(notes, 160)}"] if notes and notes.lower() != "dataset_auto" else []
        record = {
            "event_type": "experiment_response",
            "iteration": int(state.get("iteration", 0)) + 1,
            "source": source,
            "summary": summary,
            "reasoning": _section_items(reasoning),
            "outcome": _section_items(outcome),
            "state_delta": {},
            "llm_usage": {},
            "candidate": _make_json_safe(candidate),
            "result": _make_json_safe(result_value),
        }
        meta = [
            f"Iteration: `{int(state.get('iteration', 0)) + 1}`",
            f"Source: `{source}`",
        ]
        self._emit_event(record, title=f"Experiment Response: Iteration {int(state.get('iteration', 0)) + 1}", meta=meta)

    def log_session_end(self, final_state: dict[str, Any]) -> None:
        final_summary = _make_json_safe(final_state.get("final_summary", {}))
        final_state_snapshot = _final_state_artifact(final_state)
        self.final_summary_path.write_text(
            json.dumps(final_summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.final_state_path.write_text(
            json.dumps(final_state_snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        current_digest = _compact_state_digest(final_state)
        state_delta = _state_delta(self._previous_state_digest, current_digest)
        total_experiments = final_summary.get("total_experiments", len(final_state.get("observations", []) or []))
        best_candidate = final_summary.get("best_candidate", {}) if isinstance(final_summary, dict) else {}
        final_config = final_summary.get("final_config", {}) if isinstance(final_summary, dict) else {}
        outcome = [
            (
                f"best={_display_value(final_summary.get('best_result'))} | "
                f"candidate={_candidate_brief(best_candidate)}"
            ),
            (
                f"strategy={final_summary.get('proposal_strategy', 'unknown')} | "
                f"final_config={_bo_signature_from_config(final_config) or 'n/a'}"
            ),
        ]
        total_tokens = ((final_summary.get("llm_token_usage") or {}).get("total_tokens")) if isinstance(final_summary, dict) else None
        if int(total_tokens or 0) > 0:
            outcome.append(f"llm_total_tokens={int(total_tokens)}")
        record = {
            "event_type": "session_end",
            "summary": f"Campaign finished after {total_experiments} experiment(s).",
            "reasoning": _section_items([final_summary.get("stop_reason")]),
            "outcome": _section_items(outcome),
            "state_delta": state_delta,
            "llm_usage": {},
            "final_summary": final_summary,
            "artifacts": _artifact_paths(self.log_path, self.timeline_path, self.final_summary_path, self.final_state_path),
        }
        meta = [
            f"Experiments: `{total_experiments}`",
            f"Best: `{_display_value(final_summary.get('best_result'))}`",
        ]
        self._emit_event(record, title="Session End", meta=meta)
        self._previous_state_digest = current_digest
        self._previous_hypotheses = _make_json_safe(final_state.get("hypotheses", []) or [])

    def log_exception(self, exc: Exception) -> None:
        record = {
            "event_type": "exception",
            "summary": "Campaign run raised an exception.",
            "reasoning": _section_items([str(exc)]),
            "outcome": _section_items([f"type={type(exc).__name__}"]),
            "state_delta": {},
            "llm_usage": {},
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }
        self._emit_event(record, title="Exception", meta=[f"Type: `{type(exc).__name__}`"])

    def _emit_event(self, payload: dict[str, Any], title: str, meta: list[str] | None = None) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        self._write_record(record)
        self._write_timeline(_timeline_lines_for_event(record, title=title, meta=meta or []))

    def _write_record(self, record: dict[str, Any]) -> None:
        self._handle.write(json.dumps(_make_json_safe(record), ensure_ascii=False) + "\n")
        self._handle.flush()

    def _write_timeline(self, lines: list[str]) -> None:
        self._timeline_handle.write("\n".join(lines) + "\n")
        self._timeline_handle.flush()


def _settings_snapshot(settings) -> dict[str, Any]:
    return {
        "experiment_id": getattr(settings, "experiment_id", None),
        "experiment_name": getattr(settings, "experiment_name", None),
        "llm_model": getattr(settings, "llm_model", None),
        "llm_base_url": getattr(settings, "llm_base_url", None),
        "llm_api_key_env": getattr(settings, "llm_api_key_env", None),
        "llm_enable_thinking": getattr(settings, "llm_enable_thinking", None),
        "llm_temperature": getattr(settings, "llm_temperature", None),
        "llm_max_tokens": getattr(settings, "llm_max_tokens", None),
        "human_input_mode": getattr(settings, "human_input_mode", None),
        "output_dir": getattr(settings, "output_dir", None),
    }


def _default_run_id(initial_state: dict[str, Any], settings) -> str:
    problem_spec = initial_state.get("problem_spec", {}) if isinstance(initial_state, dict) else {}
    model_name = _slugify_run_component(getattr(settings, "llm_model", "unknown"))
    task_name = _slugify_run_component(getattr(settings, "experiment_name", "chembo_demo"))
    task_type_raw = (
        problem_spec.get("reaction_type")
        or ("dataset_problem" if isinstance(problem_spec.get("dataset"), dict) else None)
        or "text_problem"
    )
    task_type = _slugify_run_component(task_type_raw)
    run_id = _slugify_run_component(getattr(settings, "experiment_id", "run"))
    return f"{model_name}_{task_name}_{task_type}_{run_id}"


def _slugify_run_component(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    normalized = []
    previous_dash = False
    for char in text:
        if char.isalnum() or char in {".", "_", "-"}:
            normalized.append(char)
            previous_dash = False
            continue
        if not previous_dash:
            normalized.append("-")
            previous_dash = True
    slug = "".join(normalized).strip("-._")
    return slug or "unknown"


def _artifact_paths(log_path: Path, timeline_path: Path, final_summary_path: Path, final_state_path: Path) -> dict[str, str]:
    return {
        "run_log": str(log_path),
        "timeline": str(timeline_path),
        "final_summary": str(final_summary_path),
        "final_state": str(final_state_path),
    }


def _problem_overview(problem_spec: dict[str, Any]) -> str:
    summary = str(problem_spec.get("raw_description") or problem_spec.get("description") or "").strip()
    reaction_type = str(problem_spec.get("reaction_type") or "").strip()
    variables = problem_spec.get("variables", []) or []
    direction = str(problem_spec.get("optimization_direction") or "").strip()
    if summary:
        return f"problem={_truncate(summary, 160)}"
    return (
        f"reaction={reaction_type or 'unknown'} | "
        f"variables={len(variables)} | "
        f"direction={direction or 'unknown'}"
    )


def _compact_state_digest(state: dict[str, Any]) -> dict[str, Any]:
    observations = state.get("observations", []) or []
    selection = state.get("proposal_selected", {}) or {}
    digest = {
        "phase": state.get("phase"),
        "iteration": state.get("iteration"),
        "next_action": state.get("next_action") or None,
        "observations_count": len(observations),
        "best_result": _finite_number_or_none(state.get("best_result")),
        "best_candidate": _candidate_brief_or_none(state.get("best_candidate", {})),
        "embedding_method": (state.get("embedding_config", {}) or {}).get("method"),
        "bo_signature": _bo_signature_from_state(state),
        "proposal_shortlist_count": len(state.get("proposal_shortlist", []) or []),
        "selected_candidate": _candidate_brief_or_none(selection.get("candidate", {})),
        "selection_source": selection.get("selection_source") or None,
        "warm_start_queue_count": len(state.get("warm_start_queue", []) or []),
        "hypothesis_status_counts": _hypothesis_status_counts(state.get("hypotheses", []) or []),
        "working_memory_focus": _working_memory_focus(state),
        "convergence_state": _compact_convergence_state(state.get("convergence_state", {})),
        "termination_reason": _truncate(str(state.get("termination_reason") or ""), 160) or None,
    }
    return _make_json_safe(digest)


def _state_delta(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    for key in _state_digest_key_order():
        if _make_json_safe(current.get(key)) == _make_json_safe(previous.get(key)):
            continue
        value = current.get(key)
        if value in (None, "", {}, []):
            continue
        delta[key] = value
    return delta


def _state_digest_key_order() -> list[str]:
    return [
        "phase",
        "iteration",
        "next_action",
        "observations_count",
        "best_result",
        "best_candidate",
        "embedding_method",
        "bo_signature",
        "proposal_shortlist_count",
        "selected_candidate",
        "selection_source",
        "warm_start_queue_count",
        "hypothesis_status_counts",
        "working_memory_focus",
        "convergence_state",
        "termination_reason",
    ]


def _hypothesis_status_counts(hypotheses: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in hypotheses:
        status = str(item.get("status", "active")).strip().lower() or "active"
        counts[status] = counts.get(status, 0) + 1
    return counts


def _working_memory_focus(state: dict[str, Any]) -> str | None:
    focus = str(((state.get("memory") or {}).get("working") or {}).get("current_focus") or "").strip()
    return _truncate(focus, 160) if focus else None


def _compact_convergence_state(convergence: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "is_stagnant": convergence.get("is_stagnant"),
        "stagnation_length": convergence.get("stagnation_length"),
        "recent_improvement_rate": _finite_number_or_none(convergence.get("recent_improvement_rate")),
        "budget_used_ratio": _finite_number_or_none(convergence.get("budget_used_ratio")),
        "last_improvement_iteration": convergence.get("last_improvement_iteration"),
        "max_af_value": _finite_number_or_none(convergence.get("max_af_value")),
    }
    return {key: value for key, value in compact.items() if value not in (None, "", {}, [])}


def _bo_signature_from_state(state: dict[str, Any]) -> str | None:
    return _bo_signature_from_config(state.get("bo_config", {}) or state.get("effective_config", {}) or {})


def _bo_signature_from_config(config: dict[str, Any]) -> str | None:
    if not isinstance(config, dict) or not config:
        return None
    surrogate = config.get("surrogate_model")
    kernel = (config.get("kernel_config") or {}).get("key")
    acquisition = config.get("acquisition_function")
    parts = [str(part) for part in (surrogate, kernel, acquisition) if part not in (None, "")]
    return "/".join(parts) if parts else None


def _candidate_brief_or_none(candidate: dict[str, Any]) -> str | None:
    text = _candidate_brief(candidate)
    return None if text == "{}" else text


def _finite_number_or_none(value: Any) -> float | int | None:
    if not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    normalized = float(value)
    if normalized.is_integer():
        return int(normalized)
    return round(normalized, 6)


def _graph_llm_usage(node_name: str, state: dict[str, Any]) -> dict[str, Any]:
    usage = state.get("last_llm_usage", {}) or {}
    if usage.get("node") != node_name or int(usage.get("total_tokens", 0)) <= 0:
        return {}
    return {
        "calls": int(usage.get("calls", 0)),
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
        "total_tokens": int(usage.get("total_tokens", 0)),
        "estimated": bool(usage.get("estimated", False)),
    }


def _build_graph_event_details(
    node_name: str,
    update: Any,
    current_state: dict[str, Any],
    progress_lines: list[str],
    previous_hypotheses: list[dict[str, Any]],
) -> dict[str, Any]:
    messages = _update_messages(update)
    parsed = _extract_last_ai_json(messages)
    fallback = _progress_fallback(progress_lines)
    handlers = {
        "__interrupt__": lambda: _interrupt_event_details(update, current_state, fallback),
        "parse_input": lambda: _parse_input_event_details(current_state, fallback),
        "select_embedding": lambda: _select_embedding_event_details(current_state, parsed, fallback),
        "generate_hypotheses": lambda: _hypothesis_event_details("generated", current_state, previous_hypotheses, fallback),
        "update_hypotheses": lambda: _hypothesis_event_details("updated", current_state, previous_hypotheses, fallback),
        "configure_bo": lambda: _configure_bo_event_details(current_state, parsed, fallback),
        "warm_start": lambda: _warm_start_event_details(current_state, fallback),
        "run_bo_iteration": lambda: _run_bo_iteration_event_details(current_state, fallback),
        "run_reasoning_iteration": lambda: _run_reasoning_iteration_event_details(current_state, fallback),
        "select_candidate": lambda: _select_candidate_event_details(current_state, fallback),
        "await_human_results": lambda: _await_human_results_event_details(current_state, fallback),
        "interpret_results": lambda: _interpret_results_event_details(current_state, parsed, fallback),
        "reflect_and_decide": lambda: _reflect_event_details(current_state, parsed, update, fallback),
        "reconfig_gate": lambda: _reconfig_gate_event_details(update, current_state, fallback),
        "campaign_summary": lambda: _campaign_summary_event_details(current_state, fallback),
    }
    details = handlers.get(node_name, lambda: _default_event_details(node_name, fallback))()
    return {
        "summary": _truncate(details.get("summary") or fallback or f"Processed node `{node_name}`.", 160),
        "reasoning": _section_items(details.get("reasoning", [])),
        "outcome": _section_items(details.get("outcome", [])),
    }


def _interrupt_event_details(update: Any, state: dict[str, Any], fallback: str) -> dict[str, Any]:
    payload = _interrupt_payload(update)
    requested_candidate = payload.get("candidate")
    if not isinstance(requested_candidate, dict) or not requested_candidate:
        proposal = state.get("current_proposal", {}) or {}
        requested_candidate = (proposal.get("candidates") or [{}])[0]
    reasoning = []
    message = str(payload.get("message") or "").strip()
    if message:
        reasoning.append(message)
    outcome = []
    if payload.get("iteration") not in (None, ""):
        outcome.append(f"requested_iteration={payload.get('iteration')}")
    if isinstance(requested_candidate, dict) and requested_candidate:
        outcome.append(f"candidate={_candidate_brief(requested_candidate)}")
    return {
        "summary": "Waiting for experimental result.",
        "reasoning": reasoning,
        "outcome": outcome,
    }


def _parse_input_event_details(state: dict[str, Any], fallback: str) -> dict[str, Any]:
    problem = state.get("problem_spec", {}) or {}
    target = str(problem.get("target_metric") or "target").strip()
    direction = str(problem.get("optimization_direction") or "unknown").strip()
    return {
        "summary": f"Parsed problem specification for {problem.get('reaction_type') or 'unknown'} campaign.",
        "reasoning": [],
        "outcome": [
            f"variables={len(problem.get('variables', []) or [])} | budget={problem.get('budget', 'unknown')}",
            f"objective={direction} {target}",
            _problem_overview(problem),
        ] if problem else ([fallback] if fallback else []),
    }


def _select_embedding_event_details(state: dict[str, Any], parsed: dict[str, Any] | None, fallback: str) -> dict[str, Any]:
    config = state.get("embedding_config", {}) or {}
    outcome = [
        f"resolved={config.get('method', 'unknown')} | requested={config.get('requested_method', config.get('method', 'unknown'))}",
        f"dim={config.get('dim', '?')} | confidence={_display_value(config.get('confidence'))}",
    ]
    return {
        "summary": f"Chose embedding `{config.get('method', 'unknown')}`.",
        "reasoning": [parsed.get("rationale")] if isinstance(parsed, dict) else [config.get("rationale")],
        "outcome": outcome if config else ([fallback] if fallback else []),
    }


def _hypothesis_event_details(
    action: str,
    state: dict[str, Any],
    previous_hypotheses: list[dict[str, Any]],
    fallback: str,
) -> dict[str, Any]:
    hypotheses = state.get("hypotheses", []) or []
    status_counts = _hypothesis_status_counts(hypotheses)
    changed = _hypothesis_change_lines(previous_hypotheses, hypotheses)
    focus = _working_memory_focus(state)
    return {
        "summary": f"{action.capitalize()} hypotheses ({len(hypotheses)} total).",
        "reasoning": [focus] if focus else [],
        "outcome": [
            f"status_counts={_status_counts_text(status_counts)}",
            *changed,
        ] if hypotheses else ([fallback] if fallback else []),
    }


def _configure_bo_event_details(state: dict[str, Any], parsed: dict[str, Any] | None, fallback: str) -> dict[str, Any]:
    config = state.get("bo_config", {}) or {}
    latest_reconfig = (state.get("reconfig_history") or [])[-1] if state.get("reconfig_history") else None
    accepted = latest_reconfig.get("accepted") if isinstance(latest_reconfig, dict) else None
    backtest_reason = None
    if isinstance(latest_reconfig, dict):
        backtest_reason = (latest_reconfig.get("backtesting") or {}).get("reason") or latest_reconfig.get("reason")
    outcome = [
        f"signature={_bo_signature_from_config(config) or 'n/a'}",
        f"confidence={_display_value((parsed or {}).get('confidence'))}" if isinstance(parsed, dict) and parsed.get("confidence") is not None else None,
        f"backtest_accepted={accepted}" if accepted is not None else None,
    ]
    reasoning = []
    if isinstance(parsed, dict):
        reasoning.extend(
            [
                parsed.get("rationale"),
                ((parsed.get("kernel_config") or {}).get("rationale") if isinstance(parsed.get("kernel_config"), dict) else None),
            ]
        )
    if backtest_reason:
        reasoning.append(backtest_reason)
    summary = f"Configured BO stack `{_bo_signature_from_config(config) or 'unknown'}`."
    if accepted is False:
        summary = f"Retained BO stack `{_bo_signature_from_config(config) or 'unknown'}` after backtesting rejected the proposal."
    return {
        "summary": summary,
        "reasoning": reasoning,
        "outcome": outcome if config else ([fallback] if fallback else []),
    }


def _warm_start_event_details(state: dict[str, Any], fallback: str) -> dict[str, Any]:
    shortlist = state.get("proposal_shortlist", []) or []
    prior_guided = sum(1 for item in shortlist if item.get("warm_start_category") == "prior_guided")
    exploration = len(shortlist) - prior_guided
    return {
        "summary": f"Prepared warm-start shortlist with {len(shortlist)} candidate(s).",
        "reasoning": [f"prior_guided={prior_guided} | exploration={exploration}"],
        "outcome": _candidate_outcome_lines(shortlist, rationale_key="warm_start_rationale", category_key="warm_start_category")
        or ([fallback] if fallback else []),
    }


def _run_bo_iteration_event_details(state: dict[str, Any], fallback: str) -> dict[str, Any]:
    shortlist = state.get("proposal_shortlist", []) or []
    payload = state.get("last_tool_payload", {}) or {}
    metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}
    resolved = payload.get("resolved_components", {}) if isinstance(payload.get("resolved_components"), dict) else {}
    return {
        "summary": f"BO produced shortlist with {len(shortlist)} candidate(s).",
        "reasoning": [
            f"strategy={payload.get('strategy') or metadata.get('proposal_strategy') or 'bo'} | status={payload.get('status', 'unknown')}",
            _resolved_component_text(resolved),
            metadata.get("fallback_reason"),
        ],
        "outcome": _candidate_outcome_lines(shortlist) or ([fallback] if fallback else []),
    }


def _run_reasoning_iteration_event_details(state: dict[str, Any], fallback: str) -> dict[str, Any]:
    shortlist = state.get("proposal_shortlist", []) or []
    payload = state.get("last_tool_payload", {}) or {}
    metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}
    reasoning = [
        (
            f"validation_rejections={metadata.get('validation_rejections', 0)} | "
            f"repair_replacements={metadata.get('repair_replacements', 0)} | "
            f"fallback_fill={metadata.get('fallback_fill_count', 0)}"
        ),
        f"strategy={payload.get('strategy') or metadata.get('proposal_strategy') or 'pure_reasoning'}",
    ]
    return {
        "summary": f"Reasoning generated shortlist with {len(shortlist)} candidate(s).",
        "reasoning": reasoning,
        "outcome": _candidate_outcome_lines(shortlist, rationale_key="reasoning_rationale", category_key="reasoning_category")
        or ([fallback] if fallback else []),
    }


def _select_candidate_event_details(state: dict[str, Any], fallback: str) -> dict[str, Any]:
    selected = state.get("proposal_selected", {}) or {}
    rationale = selected.get("rationale", {}) if isinstance(selected.get("rationale"), dict) else {}
    return {
        "summary": f"Selected next experiment candidate from `{selected.get('selection_source', 'unknown')}`.",
        "reasoning": [
            rationale.get("chemical_reasoning"),
            rationale.get("hypothesis_alignment"),
            rationale.get("information_value"),
            rationale.get("concerns"),
        ],
        "outcome": [
            f"candidate={_candidate_brief(selected.get('candidate', {}))}",
            (
                f"index={selected.get('selected_index', 0)} | "
                f"override={selected.get('override', False)} | "
                f"confidence={_display_value(selected.get('confidence'))}"
            ),
        ] if selected else ([fallback] if fallback else []),
    }


def _await_human_results_event_details(state: dict[str, Any], fallback: str) -> dict[str, Any]:
    observations = state.get("observations", []) or []
    latest = observations[-1] if observations else {}
    performance = (state.get("performance_log") or [])[-1] if state.get("performance_log") else {}
    row_id = (latest.get("metadata") or {}).get("dataset_row_id")
    outcome = [
        (
            f"result={_display_value(latest.get('result'))} | "
            f"best_so_far={_display_value(state.get('best_result'))} | "
            f"improved={performance.get('improved')}"
        ),
        f"candidate={_candidate_brief(latest.get('candidate', {}))}" if latest else None,
        f"dataset_row_id={row_id}" if row_id not in (None, "") else None,
    ]
    reasoning = []
    notes = str((latest.get("metadata") or {}).get("notes") or "").strip()
    if notes and notes.lower() != "dataset_auto":
        reasoning.append(f"notes={_truncate(notes, 160)}")
    return {
        "summary": "Recorded experimental result.",
        "reasoning": reasoning,
        "outcome": outcome if latest else ([fallback] if fallback else []),
    }


def _interpret_results_event_details(state: dict[str, Any], parsed: dict[str, Any] | None, fallback: str) -> dict[str, Any]:
    interpretation = (parsed or {}).get("interpretation") if isinstance(parsed, dict) else None
    supported = (parsed or {}).get("supported_hypotheses", []) if isinstance(parsed, dict) else []
    refuted = (parsed or {}).get("refuted_hypotheses", []) if isinstance(parsed, dict) else []
    archived = (parsed or {}).get("archived_hypotheses", []) if isinstance(parsed, dict) else []
    working_focus = _working_memory_focus(state)
    outcome = [
        _hypothesis_ids_line("supported", supported),
        _hypothesis_ids_line("refuted", refuted),
        _hypothesis_ids_line("archived", archived),
    ]
    if working_focus:
        outcome.append(f"focus={working_focus}")
    return {
        "summary": _truncate(interpretation or "Interpreted latest result and updated memory.", 160),
        "reasoning": [((parsed or {}).get("episodic_memory") or {}).get("reflection")] if isinstance(parsed, dict) else [],
        "outcome": outcome if interpretation or outcome else ([fallback] if fallback else []),
    }


def _reflect_event_details(state: dict[str, Any], parsed: dict[str, Any] | None, update: Any, fallback: str) -> dict[str, Any]:
    next_action = state.get("next_action") or "continue"
    convergence = _compact_convergence_state(state.get("convergence_state", {}) or {})
    reasoning = []
    if isinstance(parsed, dict) and parsed.get("reasoning"):
        reasoning.append(parsed.get("reasoning"))
        if parsed.get("confidence") is not None:
            reasoning.append(f"confidence={_display_value(parsed.get('confidence'))}")
    else:
        reasoning.append(_last_message_text(update))
    outcome = [
        _convergence_text(convergence),
        f"best_so_far={_display_value(state.get('best_result'))}",
    ]
    return {
        "summary": f"Reflected on campaign progress and chose `{next_action}`.",
        "reasoning": reasoning,
        "outcome": outcome if convergence or state.get("best_result") is not None else ([fallback] if fallback else []),
    }


def _reconfig_gate_event_details(update: Any, state: dict[str, Any], fallback: str) -> dict[str, Any]:
    text = _truncate(_last_message_text(update), 160)
    approved = state.get("next_action") == "reconfigure"
    return {
        "summary": "Reconfiguration approved." if approved else "Reconfiguration rejected.",
        "reasoning": [text] if text else [],
        "outcome": [fallback] if fallback and not text else [],
    }


def _campaign_summary_event_details(state: dict[str, Any], fallback: str) -> dict[str, Any]:
    summary = state.get("final_summary", {}) or {}
    return {
        "summary": f"Campaign completed after {summary.get('total_experiments', len(state.get('observations', []) or []))} experiment(s).",
        "reasoning": [summary.get("stop_reason")],
        "outcome": [
            f"best={_display_value(summary.get('best_result'))} | candidate={_candidate_brief(summary.get('best_candidate', {}))}",
            f"strategy={summary.get('proposal_strategy', 'unknown')}",
        ] if summary else ([fallback] if fallback else []),
    }


def _default_event_details(node_name: str, fallback: str) -> dict[str, Any]:
    return {
        "summary": fallback or f"Processed node `{node_name}`.",
        "reasoning": [],
        "outcome": [],
    }


def _interrupt_payload(update: Any) -> dict[str, Any]:
    payload = _make_json_safe(update)
    if isinstance(payload, list) and payload:
        payload = payload[0]
    if isinstance(payload, dict) and isinstance(payload.get("value"), dict):
        return payload.get("value", {})
    return payload if isinstance(payload, dict) else {}


def _progress_fallback(progress_lines: list[str]) -> str:
    if not progress_lines:
        return ""
    text = str(progress_lines[0]).strip()
    if "] " in text:
        text = text.split("] ", 1)[1]
    return _truncate(text, 160)


def _extract_last_ai_json(messages: list[Any]) -> dict[str, Any] | None:
    for message in reversed(messages):
        if _message_type_name(message) != "AIMessage":
            continue
        payload = _extract_json_dict(_message_text(message))
        if isinstance(payload, dict):
            return payload
    return None


def _message_type_name(message: Any) -> str:
    return message.__class__.__name__


def _section_items(values: list[Any], limit: int = 3) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in (None, "", [], {}):
            continue
        text = _truncate(_stringify_section_value(value), 160)
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
        if len(items) >= limit:
            break
    return items


def _stringify_section_value(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        return _json_inline(value)
    if isinstance(value, (list, tuple, set)):
        return ", ".join(_stringify_section_value(item) for item in value if item not in (None, "", [], {}))
    return str(value)


def _candidate_outcome_lines(shortlist: list[dict[str, Any]], rationale_key: str | None = None, category_key: str | None = None) -> list[str]:
    lines: list[str] = []
    for index, item in enumerate(shortlist[:3], start=1):
        parts = [f"#{index}", _candidate_brief(item.get("candidate", {}))]
        if category_key and item.get(category_key):
            parts.append(f"category={item.get(category_key)}")
        if rationale_key and item.get(rationale_key):
            parts.append(f"why={_truncate(item.get(rationale_key), 100)}")
        elif item.get("predicted_value") not in (None, ""):
            parts.append(f"pred={_display_value(item.get('predicted_value'))}")
        lines.append(" | ".join(parts))
    return lines


def _resolved_component_text(resolved: dict[str, Any]) -> str | None:
    if not resolved:
        return None
    return (
        f"resolved={resolved.get('embedding_method') or 'n/a'}/"
        f"{resolved.get('surrogate_model') or 'n/a'}/"
        f"{((resolved.get('kernel_config') or {}).get('key') if isinstance(resolved.get('kernel_config'), dict) else 'n/a')}/"
        f"{resolved.get('acquisition_function') or 'n/a'}"
    )


def _hypothesis_change_lines(previous_hypotheses: list[dict[str, Any]], current_hypotheses: list[dict[str, Any]]) -> list[str]:
    previous_lookup = {str(item.get("id") or ""): item for item in previous_hypotheses if isinstance(item, dict)}
    lines: list[str] = []
    for item in current_hypotheses:
        if not isinstance(item, dict):
            continue
        hypothesis_id = str(item.get("id") or "").strip() or "H?"
        previous = previous_lookup.get(hypothesis_id)
        text = str(item.get("text") or "").strip()
        status = str(item.get("status") or "active").strip()
        confidence = str(item.get("confidence") or "unknown").strip()
        if previous is None:
            lines.append(f"{hypothesis_id} new ({status}, {confidence}): {_truncate(text, 96)}")
            continue
        changed = any(
            previous.get(key) != item.get(key)
            for key in ("text", "mechanism", "testable_prediction", "status", "confidence")
        )
        if changed:
            lines.append(f"{hypothesis_id} updated ({status}, {confidence}): {_truncate(text, 96)}")
    if lines:
        return lines[:3]
    for item in current_hypotheses[:3]:
        if not isinstance(item, dict):
            continue
        hypothesis_id = str(item.get("id") or "H?").strip()
        status = str(item.get("status") or "active").strip()
        text = str(item.get("text") or "").strip()
        lines.append(f"{hypothesis_id} ({status}): {_truncate(text, 96)}")
    return lines[:3]


def _status_counts_text(counts: dict[str, int]) -> str:
    parts = [f"{key}={value}" for key, value in sorted(counts.items())]
    return ", ".join(parts) if parts else "none"


def _hypothesis_ids_line(label: str, items: list[Any]) -> str | None:
    values = [str(item).strip() for item in items if str(item).strip()]
    if not values:
        return None
    return f"{label}={', '.join(values[:6])}"


def _convergence_text(convergence: dict[str, Any]) -> str | None:
    if not convergence:
        return None
    parts = []
    for key in ("is_stagnant", "stagnation_length", "recent_improvement_rate", "budget_used_ratio", "last_improvement_iteration", "max_af_value"):
        if key not in convergence:
            continue
        parts.append(f"{key}={_display_value(convergence.get(key))}")
    return ", ".join(parts) if parts else None


def _display_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return "n/a"
        normalized = float(value)
        if normalized.is_integer():
            return str(int(normalized))
        return f"{normalized:.4f}".rstrip("0").rstrip(".")
    if value in (None, "", {}, []):
        return "n/a"
    return str(value)


def _timeline_lines_for_event(record: dict[str, Any], title: str, meta: list[str]) -> list[str]:
    lines = [f"## {title}", "", f"Timestamp: {record.get('timestamp')}"]
    if meta:
        lines.append(" | ".join(meta))
    lines.append("")
    lines.extend(_timeline_section("Summary", [record.get("summary")]))
    lines.extend(_timeline_section("Reasoning", record.get("reasoning", [])))
    lines.extend(_timeline_section("Outcome", record.get("outcome", [])))
    lines.extend(_timeline_section("State Changes", _state_delta_lines(record.get("state_delta", {}))))
    if record.get("artifacts"):
        lines.extend(
            _timeline_section(
                "Artifacts",
                [
                    f"run_log={record['artifacts'].get('run_log')}",
                    f"timeline={record['artifacts'].get('timeline')}",
                    f"final_summary={record['artifacts'].get('final_summary')}",
                    f"final_state={record['artifacts'].get('final_state')}",
                ],
            )
        )
    lines.append("")
    return lines


def _timeline_section(title: str, items: list[str]) -> list[str]:
    content = _section_items(items)
    if not content:
        return []
    return [f"### {title}", "", *[f"- {item}" for item in content], ""]


def _state_delta_lines(delta: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key in _state_digest_key_order():
        if key not in delta:
            continue
        label = key.replace("_", " ")
        value = delta.get(key)
        if isinstance(value, dict):
            if key == "hypothesis_status_counts":
                lines.append(f"{label}: {_status_counts_text(value)}")
            elif key == "convergence_state":
                text = _convergence_text(value)
                if text:
                    lines.append(f"{label}: {text}")
            else:
                lines.append(f"{label}: {_json_inline(value)}")
        else:
            lines.append(f"{label}: {_display_value(value)}")
    return lines[:3]


def _final_state_artifact(state: dict[str, Any]) -> dict[str, Any]:
    return _make_json_safe({key: value for key, value in state.items() if key != "messages"})


def _make_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _make_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_make_json_safe(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "item") and callable(getattr(value, "item")):
        try:
            return _make_json_safe(value.item())
        except Exception:
            pass
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, bool, int, float)):
        return _make_json_safe(enum_value)
    if hasattr(value, "__dict__") and value.__dict__:
        return {key: _make_json_safe(item) for key, item in vars(value).items()}
    return repr(value)


def _json_code_block(value: Any) -> str:
    return "```json\n" + json.dumps(_make_json_safe(value), ensure_ascii=False, indent=2) + "\n```"


def _json_inline(value: Any) -> str:
    return json.dumps(_make_json_safe(value), ensure_ascii=False)


def _update_messages(update: Any) -> list[Any]:
    if not isinstance(update, dict):
        return []
    messages = update.get("messages", [])
    return messages if isinstance(messages, list) else []


def _timestamp_now() -> str:
    return datetime.now(timezone.utc).isoformat()
