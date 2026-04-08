from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("langchain_core")

from config.settings import Settings
from knowledge.connectors.base import RetrievedChunk
from tools.retrieval_tools import build_retrieval_tools


def _tool_map(settings: Settings, problem_spec: dict | None = None) -> dict[str, object]:
    tools = build_retrieval_tools(settings, problem_spec or {})
    return {tool.name: tool for tool in tools}


def test_local_rag_search_formats_filtered_results(monkeypatch) -> None:
    class FakeLocalRAGConnector:
        def __init__(self, settings=None):
            del settings

        def is_available(self) -> bool:
            return True

        def search(self, query: str, top_k: int = 3):
            del query, top_k
            return [
                RetrievedChunk(
                    content="Original mechanism text",
                    source_type="local_rag",
                    source_id="rag:1",
                    metadata={"source_file": "review.pdf", "collection": "reviews"},
                    relevance_score=0.91,
                )
            ]

    class FakeLeakageFilter:
        def __init__(self, problem_spec, strict_mode=True):
            del problem_spec, strict_mode

        def filter_single(self, chunk):
            return SimpleNamespace(is_usable=True, content="Sanitized mechanism explanation", original=chunk)

    monkeypatch.setattr("tools.retrieval_tools.LocalRAGConnector", FakeLocalRAGConnector)
    monkeypatch.setattr("tools.retrieval_tools.LeakageFilter", FakeLeakageFilter)

    payload = json.loads(_tool_map(Settings())["local_rag_search"].invoke({"query": "ligand effect", "top_k": 1}))

    assert payload["source"] == "local_rag"
    assert payload["result_count"] == 1
    assert payload["results"][0]["snippet_id"] == "LR01"
    assert payload["results"][0]["text"] == "Sanitized mechanism explanation"
    assert payload["results"][0]["source_file"] == "review.pdf"
    assert payload["results"][0]["collection"] == "reviews"
    assert payload["results"][0]["relevance_score"] == pytest.approx(0.91)


def test_local_rag_search_returns_unavailable_when_index_is_empty(monkeypatch) -> None:
    class EmptyLocalRAGConnector:
        def __init__(self, settings=None):
            del settings

        def is_available(self) -> bool:
            return False

    monkeypatch.setattr("tools.retrieval_tools.LocalRAGConnector", EmptyLocalRAGConnector)

    payload = json.loads(_tool_map(Settings())["local_rag_search"].invoke({"query": "base effect"}))

    assert payload["source"] == "local_rag"
    assert payload["status"] == "unavailable"
    assert payload["results"] == []


def test_web_search_returns_unavailable_without_api_key() -> None:
    payload = json.loads(
        _tool_map(Settings(tavily_api_key=""))["web_search_tool"].invoke({"query": "buchwald ligand properties"})
    )

    assert payload["source"] == "web"
    assert payload["status"] == "unavailable"
    assert payload["result_count"] == 0


def test_literature_search_returns_unavailable_without_api_key() -> None:
    payload = json.loads(
        _tool_map(Settings(semantic_scholar_api_key=""))["literature_search"].invoke(
            {"query": "direct arylation precedent"}
        )
    )

    assert payload["source"] == "semantic_scholar"
    assert payload["status"] == "unavailable"
    assert payload["results"] == []


def test_web_search_truncates_text_and_skips_unusable_results(monkeypatch) -> None:
    long_text = "A" * 500

    class FakeWebSearchConnector:
        def __init__(self, api_key="", search_depth="advanced", max_results=5, include_domains=None, exclude_domains=None, client=None):
            del api_key, search_depth, max_results, include_domains, exclude_domains, client

        def is_available(self) -> bool:
            return True

        def search(self, query: str, max_results: int = 2):
            del query, max_results
            return [
                RetrievedChunk(
                    content="result 1",
                    source_type="web",
                    source_id="https://example.com/1",
                    metadata={"url": "https://example.com/1", "domain": "example.com"},
                ),
                RetrievedChunk(
                    content="result 2",
                    source_type="web",
                    source_id="https://example.com/2",
                    metadata={"url": "https://example.com/2", "domain": "example.com"},
                ),
            ]

    class FakeLeakageFilter:
        def __init__(self, problem_spec, strict_mode=True):
            del problem_spec, strict_mode

        def filter_single(self, chunk):
            if chunk.source_id.endswith("/1"):
                return SimpleNamespace(is_usable=True, content=long_text, original=chunk)
            return SimpleNamespace(is_usable=False, content="", original=chunk)

    monkeypatch.setattr("tools.retrieval_tools.WebSearchConnector", FakeWebSearchConnector)
    monkeypatch.setattr("tools.retrieval_tools.LeakageFilter", FakeLeakageFilter)

    payload = json.loads(_tool_map(Settings(tavily_api_key="test-key"))["web_search_tool"].invoke({"query": "dmf properties"}))

    assert payload["source"] == "web"
    assert payload["result_count"] == 1
    assert payload["results"][0]["snippet_id"] == "W01"
    assert payload["results"][0]["domain"] == "example.com"
    assert len(payload["results"][0]["text"]) == 400


def test_literature_search_formats_abstract_excerpt(monkeypatch) -> None:
    class FakeSemanticScholarConnector:
        def __init__(self, api_key="", timeout=15.0):
            del api_key, timeout

        def search(self, query: str, max_results: int = 3, **kwargs):
            del query, max_results, kwargs
            return [
                RetrievedChunk(
                    content="Title: Paper\n\nAbstract: Useful abstract content for reasoning.",
                    source_type="semantic_scholar",
                    source_id="doi:10.1/abc",
                    metadata={
                        "title": "Paper",
                        "authors": "A. Chemist",
                        "year": 2023,
                        "doi": "10.1/abc",
                    },
                )
            ]

    monkeypatch.setattr("tools.retrieval_tools.SemanticScholarConnector", FakeSemanticScholarConnector)

    payload = json.loads(
        _tool_map(Settings(semantic_scholar_api_key="test-key"))["literature_search"].invoke(
            {"query": "nickel catalysis", "max_results": 1}
        )
    )

    assert payload["source"] == "semantic_scholar"
    assert payload["result_count"] == 1
    assert payload["results"][0]["snippet_id"] == "S201"
    assert payload["results"][0]["title"] == "Paper"
    assert payload["results"][0]["doi"] == "10.1/abc"
    assert payload["results"][0]["abstract_excerpt"] == "Useful abstract content for reasoning."
