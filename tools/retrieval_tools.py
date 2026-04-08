"""
Runtime retrieval tools for optional LLM-guided search.
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from config.settings import Settings
from knowledge.connectors import (
    LocalRAGConnector,
    SemanticScholarConnector,
    WebSearchConnector,
)
from knowledge.leakage_filter import LeakageFilter


def build_retrieval_tools(settings: Settings, problem_spec: dict[str, Any]) -> list[Any]:
    """Build retrieval tools bound to the current campaign settings and problem."""
    local_connector = LocalRAGConnector(settings=settings)
    semantic_scholar_api_key = str(getattr(settings, "semantic_scholar_api_key", "") or "").strip()
    literature_connector = SemanticScholarConnector(api_key=semantic_scholar_api_key)
    tavily_api_key = str(getattr(settings, "tavily_api_key", "") or "").strip()
    web_connector = WebSearchConnector(
        api_key=tavily_api_key,
        include_domains=list(getattr(settings, "web_search_domains", []) or []) or None,
    )
    leakage_filter = LeakageFilter(
        problem_spec,
        strict_mode=bool(getattr(settings, "leakage_filter_strict", True)),
    )

    @tool
    def local_rag_search(query: str, top_k: int = 3) -> str:
        """Search the local chemistry literature index for mechanism, precedent, or reagent-property evidence."""
        if not local_connector.is_available():
            return _json_dumps(
                {
                    "source": "local_rag",
                    "query": str(query or ""),
                    "status": "unavailable",
                    "results": [],
                    "result_count": 0,
                    "instruction": "Local RAG is unavailable. Continue without local retrieval evidence.",
                }
            )

        try:
            chunks = local_connector.search(str(query or ""), top_k=_coerce_positive_int(top_k, default=3))
        except Exception:
            chunks = []

        results = []
        for index, chunk in enumerate(chunks, start=1):
            filtered = leakage_filter.filter_single(chunk)
            text = str(getattr(filtered, "content", "") or "").strip()
            if not bool(getattr(filtered, "is_usable", False)) or not text:
                continue
            metadata = dict(getattr(chunk, "metadata", {}) or {})
            results.append(
                {
                    "snippet_id": _make_snippet_id("LR", index),
                    "text": _truncate_text(text, 600),
                    "source_file": str(metadata.get("source_file", "") or ""),
                    "collection": str(metadata.get("collection", "") or ""),
                    "relevance_score": float(getattr(chunk, "relevance_score", 0.0) or 0.0),
                }
            )

        return _json_dumps(
            {
                "source": "local_rag",
                "query": str(query or ""),
                "results": results,
                "result_count": len(results),
                "instruction": "Use these snippets as supporting context, not ground truth.",
            }
        )

    @tool
    def literature_search(query: str, max_results: int = 3) -> str:
        """Search Semantic Scholar abstracts for literature precedents relevant to the current reasoning step."""
        if not semantic_scholar_api_key:
            return _json_dumps(
                {
                    "source": "semantic_scholar",
                    "query": str(query or ""),
                    "status": "unavailable",
                    "results": [],
                    "result_count": 0,
                    "instruction": "Semantic Scholar is unavailable. Continue without literature retrieval evidence.",
                }
            )

        try:
            chunks = literature_connector.search(
                str(query or ""),
                max_results=_coerce_positive_int(max_results, default=3),
            )
        except Exception:
            chunks = []

        results = []
        for index, chunk in enumerate(chunks, start=1):
            metadata = dict(getattr(chunk, "metadata", {}) or {})
            results.append(
                {
                    "snippet_id": _make_snippet_id("S2", index),
                    "title": str(metadata.get("title", "") or ""),
                    "authors": str(metadata.get("authors", "") or ""),
                    "year": metadata.get("year"),
                    "doi": str(metadata.get("doi", "") or ""),
                    "abstract_excerpt": _truncate_text(_extract_abstract_excerpt(getattr(chunk, "content", "")), 500),
                }
            )

        return _json_dumps(
            {
                "source": "semantic_scholar",
                "query": str(query or ""),
                "results": results,
                "result_count": len(results),
                "instruction": "Cite supporting papers by snippet id when literature evidence changes your reasoning.",
            }
        )

    @tool
    def web_search_tool(query: str, max_results: int = 2) -> str:
        """Search the web for well-known reagent properties, named conditions, or broad chemistry references."""
        if not web_connector.is_available():
            return _json_dumps(
                {
                    "source": "web",
                    "query": str(query or ""),
                    "status": "unavailable",
                    "results": [],
                    "result_count": 0,
                    "instruction": "Web search is unavailable. Continue without web retrieval evidence.",
                }
            )

        try:
            chunks = web_connector.search(
                str(query or ""),
                max_results=_coerce_positive_int(max_results, default=2),
            )
        except Exception:
            chunks = []

        results = []
        for index, chunk in enumerate(chunks, start=1):
            filtered = leakage_filter.filter_single(chunk)
            text = str(getattr(filtered, "content", "") or "").strip()
            if not bool(getattr(filtered, "is_usable", False)) or not text:
                continue
            metadata = dict(getattr(chunk, "metadata", {}) or {})
            results.append(
                {
                    "snippet_id": _make_snippet_id("W", index),
                    "url": str(metadata.get("url", "") or ""),
                    "domain": str(metadata.get("domain", "") or ""),
                    "text": _truncate_text(text, 400),
                }
            )

        return _json_dumps(
            {
                "source": "web",
                "query": str(query or ""),
                "results": results,
                "result_count": len(results),
                "instruction": "Web content is lower-trust evidence. Use it cautiously and only as supporting context.",
            }
        )

    return [local_rag_search, literature_search, web_search_tool]


def _make_snippet_id(prefix: str, index: int) -> str:
    return f"{prefix}{max(1, int(index)):02d}"


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, coerced)


def _truncate_text(text: Any, max_chars: int) -> str:
    normalized = str(text or "").strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rstrip()


def _extract_abstract_excerpt(content: Any) -> str:
    text = str(content or "").strip()
    marker = "Abstract:"
    if marker in text:
        return text.split(marker, 1)[1].strip()
    return text


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
