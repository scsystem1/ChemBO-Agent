from __future__ import annotations

import json

import pytest

pytest.importorskip("langchain_core")

from config.settings import Settings
from tools.retrieval_tools import build_retrieval_tools


def test_build_retrieval_tools_exposes_single_evidence_tool(monkeypatch) -> None:
    def fake_search(question, context, *, problem_spec, settings, llm, invoke_json):
        del context, problem_spec, settings, llm, invoke_json

        class Result:
            def to_compact_json(self):
                return json.dumps({"status": "success", "best_answer": f"answer: {question}", "answers": []})

        return Result()

    monkeypatch.setattr("tools.retrieval_tools.search_chemistry_literature_impl", fake_search)

    tools = build_retrieval_tools(Settings(), {"reaction_type": "BH"}, object(), lambda *args: ({}, {}))

    assert [tool.name for tool in tools] == ["search_chemistry_literature"]
    payload = json.loads(tools[0].invoke({"question": "Why does ligand matter?", "context": "iter 4"}))
    assert payload["status"] == "success"
    assert "ligand" in payload["best_answer"]


def test_build_retrieval_tools_returns_empty_when_disabled() -> None:
    settings = Settings()
    settings.evidence_search_enabled = False

    assert build_retrieval_tools(settings, {}, object(), lambda *args: ({}, {})) == []
