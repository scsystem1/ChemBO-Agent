"""
Regression coverage for campaign runner logging around non-dict stream events.
"""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path


langgraph = types.ModuleType("langgraph")
langgraph_types = types.ModuleType("langgraph.types")


class Command:
    def __init__(self, resume=None):
        self.resume = resume


langgraph_types.Command = Command
langgraph.types = langgraph_types
sys.modules.setdefault("langgraph", langgraph)
sys.modules.setdefault("langgraph.types", langgraph_types)

from config.settings import Settings
from core.campaign_runner import run_campaign


class _DummyState:
    def __init__(self, values):
        self.values = values


class _DummyGraph:
    def __init__(self):
        self.state = {
            "phase": "completed",
            "iteration": 0,
            "next_action": "stop",
            "optimization_direction": "maximize",
            "observations": [],
            "best_result": float("-inf"),
            "best_candidate": {},
            "embedding_config": {},
            "bo_config": {},
            "effective_config": {},
            "current_proposal": {},
            "proposal_selected": {},
            "proposal_shortlist": [],
            "warm_start_queue": [],
            "warm_start_active": False,
            "convergence_state": {},
            "last_tool_payload": {},
            "last_llm_usage": {},
            "llm_token_usage": {},
            "performance_log": [],
            "config_history": [],
            "reconfig_history": [],
            "llm_reasoning_log": [],
            "termination_reason": "completed",
            "final_summary": {"total_experiments": 0, "best_result": None, "stop_reason": "completed"},
            "memory": {},
            "hypotheses": [],
            "problem_spec": {"budget": 1},
        }

    def stream(self, payload, config=None, stream_mode=None):
        yield {"__interrupt__": ({"value": {"message": "run experiment"}},)}
        yield {"campaign_summary": {"final_summary": self.state["final_summary"], "messages": []}}

    def get_state(self, config):
        return _DummyState(self.state)


def test_run_campaign_logs_tuple_stream_events_without_crashing():
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(llm_model="mock", output_dir=tmp)
        graph = _DummyGraph()

        final_state = run_campaign(
            graph,
            {"problem_spec": {"budget": 1}},
            settings,
            thread_id="tuple-event-smoke",
            printer=None,
        )

        run_dir = Path(tmp) / "tuple-event-smoke"
        timeline = (run_dir / "timeline.md").read_text(encoding="utf-8")
        run_log = (run_dir / "run_log.jsonl").read_text(encoding="utf-8")

        assert final_state["phase"] == "completed"
        assert "interrupt payload=" in timeline
        assert '"raw_update": [{"value": {"message": "run experiment"}}]' in run_log
