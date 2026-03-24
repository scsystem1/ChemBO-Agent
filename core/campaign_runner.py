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
    config = {"configurable": {"thread_id": thread_id or settings.experiment_id}}
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

    if node_name == "run_bo_iteration":
        shortlist = state.get("proposal_shortlist", [])
        top_candidate = shortlist[0].get("candidate", {}) if shortlist else {}
        payload = state.get("last_tool_payload", {})
        status = payload.get("status", "unknown")
        return [f"{prefix} shortlist={len(shortlist)} status={status} top={_candidate_brief(top_candidate)}"]

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
        self._write_record(
            "session_start",
            {
                "run_id": self._run_id,
                "config": _make_json_safe(config),
                "settings": _settings_snapshot(self._settings),
                "initial_state": _state_snapshot(initial_state),
            },
        )
        self._write_timeline(
            [
                "## Session Start",
                "",
                f"Timestamp: {_timestamp_now()}",
                "",
                "### Settings",
                _json_code_block(_settings_snapshot(self._settings)),
                "",
                "### Initial State",
                _json_code_block(_state_snapshot(initial_state)),
                "",
                "### Config",
                _json_code_block(config),
                "",
            ]
        )

    def log_graph_update(
        self,
        node_name: str,
        update: Any,
        current_state: dict[str, Any],
        progress_lines: list[str],
    ) -> None:
        serialized_update = _serialize_update(update)
        self._write_record(
            "graph_update",
            {
                "node": node_name,
                "phase": current_state.get("phase"),
                "iteration": current_state.get("iteration"),
                "progress_lines": progress_lines,
                "update": serialized_update,
                "state_snapshot": _state_snapshot(current_state),
            },
        )
        self._step_index += 1
        timeline_lines = [
            f"## Step {self._step_index}: `{node_name}`",
            "",
            f"Timestamp: {_timestamp_now()}",
            f"Phase: `{current_state.get('phase')}`",
            f"Iteration: `{current_state.get('iteration')}`",
            "",
        ]
        if progress_lines:
            timeline_lines.extend(
                [
                    "### Progress",
                    "",
                    *[f"- {line}" for line in progress_lines],
                    "",
                ]
            )
        messages = serialized_update.get("messages", [])
        if messages:
            timeline_lines.extend(
                [
                    "### Messages",
                    "",
                    _messages_markdown(messages),
                    "",
                ]
            )
        if serialized_update.get("last_tool_payload") not in (None, {}, []):
            timeline_lines.extend(
                [
                    "### Tool Payload",
                    _json_code_block(serialized_update.get("last_tool_payload")),
                    "",
                ]
            )
        state_details = _timeline_state_details(current_state)
        if state_details:
            timeline_lines.extend(
                [
                    "### State Snapshot",
                    _json_code_block(state_details),
                    "",
                ]
            )
        extra_update = {key: value for key, value in serialized_update.items() if key != "messages"}
        if extra_update:
            timeline_lines.extend(
                [
                    "### Update Payload",
                    _json_code_block(extra_update),
                    "",
                ]
            )
        self._write_timeline(timeline_lines)

    def log_experiment_response(self, candidate: dict[str, Any], response: dict[str, Any], state: dict[str, Any]) -> None:
        self._write_record(
            "experiment_response",
            {
                "iteration": int(state.get("iteration", 0)) + 1,
                "candidate": _make_json_safe(candidate),
                "response": _make_json_safe(response),
                "source": "dataset_auto" if str(response.get("notes", "")).strip().lower() == "dataset_auto" else "human_input",
            },
        )
        self._write_timeline(
            [
                f"## Experiment Response: Iteration {int(state.get('iteration', 0)) + 1}",
                "",
                f"Timestamp: {_timestamp_now()}",
                f"Source: `{'dataset_auto' if str(response.get('notes', '')).strip().lower() == 'dataset_auto' else 'human_input'}`",
                "",
                "### Candidate",
                _json_code_block(candidate),
                "",
                "### Response",
                _json_code_block(response),
                "",
            ]
        )

    def log_session_end(self, final_state: dict[str, Any]) -> None:
        final_summary = _make_json_safe(final_state.get("final_summary", {}))
        final_state_snapshot = _state_snapshot(final_state, include_final_details=True)
        self.final_summary_path.write_text(
            json.dumps(final_summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.final_state_path.write_text(
            json.dumps(final_state_snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._write_record(
            "session_end",
            {
                "final_summary": final_summary,
                "state_snapshot": final_state_snapshot,
                "artifacts": {
                    "run_log": str(self.log_path),
                    "final_summary": str(self.final_summary_path),
                    "final_state": str(self.final_state_path),
                },
            },
        )
        self._write_timeline(
            [
                "## Session End",
                "",
                f"Timestamp: {_timestamp_now()}",
                "",
                "### Final Summary",
                _json_code_block(final_summary),
                "",
                "### Final State",
                _json_code_block(final_state_snapshot),
                "",
                "### Artifacts",
                "",
                f"- `run_log.jsonl`: `{self.log_path}`",
                f"- `timeline.md`: `{self.timeline_path}`",
                f"- `final_summary.json`: `{self.final_summary_path}`",
                f"- `final_state.json`: `{self.final_state_path}`",
                "",
            ]
        )

    def log_exception(self, exc: Exception) -> None:
        self._write_record(
            "exception",
            {
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            },
        )
        self._write_timeline(
            [
                "## Exception",
                "",
                f"Timestamp: {_timestamp_now()}",
                "",
                _json_code_block(
                    {
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    }
                ),
                "",
            ]
        )

    def _write_record(self, event_type: str, payload: dict[str, Any]) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            **payload,
        }
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


def _state_snapshot(state: dict[str, Any], include_final_details: bool = False) -> dict[str, Any]:
    observations = state.get("observations", []) or []
    snapshot = {
        "phase": state.get("phase"),
        "iteration": state.get("iteration"),
        "next_action": state.get("next_action"),
        "optimization_direction": state.get("optimization_direction"),
        "best_result": _make_json_safe(state.get("best_result")),
        "best_candidate": _make_json_safe(state.get("best_candidate", {})),
        "observations_count": len(observations),
        "latest_observation": _make_json_safe(observations[-1] if observations else {}),
        "embedding_config": _make_json_safe(state.get("embedding_config", {})),
        "bo_config": _make_json_safe(state.get("bo_config", {})),
        "effective_config": _make_json_safe(state.get("effective_config", {})),
        "current_proposal": _make_json_safe(state.get("current_proposal", {})),
        "proposal_selected": _make_json_safe(state.get("proposal_selected", {})),
        "proposal_shortlist_count": len(state.get("proposal_shortlist", []) or []),
        "warm_start_queue_count": len(state.get("warm_start_queue", []) or []),
        "warm_start_active": state.get("warm_start_active", False),
        "convergence_state": _make_json_safe(state.get("convergence_state", {})),
        "last_tool_payload": _make_json_safe(state.get("last_tool_payload", {})),
        "last_llm_usage": _make_json_safe(state.get("last_llm_usage", {})),
        "llm_token_usage": _make_json_safe(state.get("llm_token_usage", {})),
        "performance_log_tail": _make_json_safe((state.get("performance_log", []) or [])[-5:]),
        "config_history_tail": _make_json_safe((state.get("config_history", []) or [])[-3:]),
        "reconfig_history_tail": _make_json_safe((state.get("reconfig_history", []) or [])[-3:]),
        "llm_reasoning_log_tail": _make_json_safe((state.get("llm_reasoning_log", []) or [])[-12:]),
        "termination_reason": state.get("termination_reason", ""),
        "final_summary": _make_json_safe(state.get("final_summary", {})),
    }
    if include_final_details:
        snapshot["observations"] = _make_json_safe(observations)
        snapshot["hypotheses"] = _make_json_safe(state.get("hypotheses", []))
        snapshot["memory"] = _make_json_safe(state.get("memory", {}))
    return snapshot


def _serialize_update(update: Any) -> dict[str, Any]:
    if not isinstance(update, dict):
        return {"raw_update": _make_json_safe(update)}
    serialized: dict[str, Any] = {}
    for key, value in (update or {}).items():
        if key == "messages":
            serialized[key] = [_serialize_message(message) for message in value]
        else:
            serialized[key] = _make_json_safe(value)
    return serialized


def _serialize_message(message) -> dict[str, Any]:
    payload = {
        "message_type": message.__class__.__name__,
        "content": _make_json_safe(getattr(message, "content", "")),
        "name": getattr(message, "name", None),
        "tool_call_id": getattr(message, "tool_call_id", None),
        "tool_calls": _make_json_safe(getattr(message, "tool_calls", None)),
        "additional_kwargs": _make_json_safe(getattr(message, "additional_kwargs", None)),
        "response_metadata": _make_json_safe(getattr(message, "response_metadata", None)),
        "usage_metadata": _make_json_safe(getattr(message, "usage_metadata", None)),
    }
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


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


def _messages_markdown(messages: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    for index, message in enumerate(messages, start=1):
        message_type = message.get("message_type", "Message")
        sections.append(f"#### Message {index}: `{message_type}`")
        content = message.get("content")
        if content not in (None, "", [], {}):
            sections.append("")
            sections.append("Content:")
            sections.append(_json_or_text_block(content))
        tool_calls = message.get("tool_calls")
        if tool_calls not in (None, "", [], {}):
            sections.append("")
            sections.append("Tool calls:")
            sections.append(_json_code_block(tool_calls))
        metadata = {
            key: value
            for key, value in message.items()
            if key not in {"message_type", "content", "tool_calls"} and value not in (None, "", [], {})
        }
        if metadata:
            sections.append("")
            sections.append("Metadata:")
            sections.append(_json_code_block(metadata))
        sections.append("")
    return "\n".join(sections).rstrip()


def _json_or_text_block(value: Any) -> str:
    if isinstance(value, str):
        return "```text\n" + value + "\n```"
    return _json_code_block(value)


def _timeline_state_details(state: dict[str, Any]) -> dict[str, Any]:
    observations = state.get("observations", []) or []
    detail = {
        "best_result": _make_json_safe(state.get("best_result")),
        "best_candidate": _make_json_safe(state.get("best_candidate", {})),
        "observations_count": len(observations),
        "latest_observation": _make_json_safe(observations[-1] if observations else {}),
        "current_proposal": _make_json_safe(state.get("current_proposal", {})),
        "proposal_selected": _make_json_safe(state.get("proposal_selected", {})),
        "embedding_config": _make_json_safe(state.get("embedding_config", {})),
        "bo_config": _make_json_safe(state.get("bo_config", {})),
        "effective_config": _make_json_safe(state.get("effective_config", {})),
        "convergence_state": _make_json_safe(state.get("convergence_state", {})),
        "last_tool_payload": _make_json_safe(state.get("last_tool_payload", {})),
        "last_llm_usage": _make_json_safe(state.get("last_llm_usage", {})),
        "llm_token_usage": _make_json_safe(state.get("llm_token_usage", {})),
        "performance_log_tail": _make_json_safe((state.get("performance_log", []) or [])[-3:]),
        "config_history_tail": _make_json_safe((state.get("config_history", []) or [])[-2:]),
        "reconfig_history_tail": _make_json_safe((state.get("reconfig_history", []) or [])[-2:]),
        "llm_reasoning_log_tail": _make_json_safe((state.get("llm_reasoning_log", []) or [])[-8:]),
        "termination_reason": _make_json_safe(state.get("termination_reason", "")),
    }
    return {key: value for key, value in detail.items() if value not in (None, "", [], {})}


def _timestamp_now() -> str:
    return datetime.now(timezone.utc).isoformat()
