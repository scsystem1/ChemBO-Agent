"""
On-demand chemistry evidence search.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Callable

from config.settings import Settings
from knowledge.connectors import WebSearchConnector
from knowledge.leakage_filter import LeakageFilter
from knowledge.prompts import build_evidence_compression_prompt, build_evidence_query_prompt


@dataclass
class EvidenceAnswer:
    answer: str
    citation: str
    url: str
    relevance: float


@dataclass
class EvidenceSearchResult:
    question: str
    best_answer: str
    answers: list[EvidenceAnswer]
    queries_used: list[str]
    status: str
    notes: list[str]
    llm_usage: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "best_answer": self.best_answer,
            "answers": [asdict(answer) for answer in self.answers],
            "queries_used": list(self.queries_used),
            "status": self.status,
            "notes": list(self.notes),
            "llm_usage": dict(self.llm_usage),
        }

    def to_compact_json(self) -> str:
        payload = self.to_dict()
        payload.pop("llm_usage", None)
        return json.dumps(payload, ensure_ascii=False)


def search_chemistry_literature(
    question: str,
    context: str,
    *,
    problem_spec: dict[str, Any],
    settings: Settings,
    llm: Any,
    invoke_json: Callable[[Any, str, str, dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]],
) -> EvidenceSearchResult:
    question = str(question or "").strip()
    context = str(context or "").strip()
    if not question:
        return EvidenceSearchResult("", "", [], [], "no_results", ["empty question"], {})
    tavily_api_key = str(getattr(settings, "tavily_api_key", "") or "").strip()
    if not tavily_api_key:
        return EvidenceSearchResult(question, "", [], [], "search_unavailable", ["Tavily API key not configured."], {})

    usage: dict[str, Any] = {}
    system_prompt, user_prompt = build_evidence_query_prompt(question, context, problem_spec)
    reformulated, query_usage = invoke_json(llm, system_prompt, user_prompt, {"queries": [question], "key_terms": []})
    usage = _merge_usage(usage, query_usage)
    queries = _normalize_queries(reformulated.get("queries", []) if isinstance(reformulated, dict) else [], question)

    connector = WebSearchConnector(
        api_key=tavily_api_key,
        include_domains=list(getattr(settings, "evidence_search_domain_whitelist", []) or []) or None,
    )
    chunks = []
    notes: list[str] = []
    seen_sources: set[str] = set()
    max_per_query = int(getattr(settings, "evidence_search_max_results_per_query", 3) or 3)
    max_total = int(getattr(settings, "evidence_search_max_chunks_total", 6) or 6)
    for query in queries:
        try:
            results = connector.search(query, max_results=max_per_query, search_depth="advanced")
        except Exception as exc:
            notes.append(f"search failed for {query!r}: {type(exc).__name__}: {exc}")
            results = []
        for chunk in results:
            source_id = str(getattr(chunk, "source_id", "") or "")
            if source_id in seen_sources:
                continue
            seen_sources.add(source_id)
            chunks.append(chunk)
            if len(chunks) >= max_total:
                break
        if len(chunks) >= max_total:
            break
    if not chunks:
        return EvidenceSearchResult(question, "", [], queries, "no_results", notes or ["web search returned no results"], usage)

    leakage_filter = LeakageFilter(problem_spec)
    sanitized_chunks: list[dict[str, Any]] = []
    blocked = 0
    for index, chunk in enumerate(chunks, start=1):
        sanitized = leakage_filter.sanitize(str(getattr(chunk, "content", "") or ""))
        if sanitized.status == "blocked" or not sanitized.text:
            blocked += 1
            continue
        metadata = dict(getattr(chunk, "metadata", {}) or {})
        sanitized_chunks.append(
            {
                "snippet_id": f"W{index:02d}",
                "text": sanitized.text[:1600],
                "title": str(metadata.get("title", "") or ""),
                "url": str(metadata.get("url", "") or getattr(chunk, "source_id", "") or ""),
                "domain": str(metadata.get("domain", "") or ""),
                "sanitize_status": sanitized.status,
            }
        )
    if blocked:
        notes.append(f"blocked_or_empty_chunks={blocked}")
    if not sanitized_chunks:
        return EvidenceSearchResult(question, "", [], queries, "blocked_all", notes or ["all chunks blocked by leakage filter"], usage)

    system_prompt, user_prompt = build_evidence_compression_prompt(question, context, sanitized_chunks)
    compressed, compress_usage = invoke_json(llm, system_prompt, user_prompt, {"answers": [], "best_answer": "", "notes": []})
    usage = _merge_usage(usage, compress_usage)
    answers = _normalize_answers(compressed.get("answers", []) if isinstance(compressed, dict) else [])
    best_answer = str((compressed or {}).get("best_answer") or "").strip() if isinstance(compressed, dict) else ""
    if isinstance(compressed, dict) and isinstance(compressed.get("notes"), list):
        notes.extend(str(item) for item in compressed.get("notes", []) if str(item).strip())
    if not answers and sanitized_chunks:
        first = sanitized_chunks[0]
        answers = [
            EvidenceAnswer(
                answer=first["text"][:300],
                citation=first.get("title") or first.get("domain") or first.get("url"),
                url=first.get("url", ""),
                relevance=0.5,
            )
        ]
    if not best_answer:
        best_answer = answers[0].answer if answers else ""
    return EvidenceSearchResult(question, best_answer, answers[:3], queries, "success", notes, usage)


def _normalize_queries(raw_queries: Any, fallback: str) -> list[str]:
    queries: list[str] = []
    for raw in raw_queries if isinstance(raw_queries, list) else []:
        query = " ".join(str(raw or "").split())
        if query and query not in queries:
            queries.append(query[:180])
        if len(queries) >= 2:
            break
    return queries or [fallback[:180]]


def _normalize_answers(raw_answers: Any) -> list[EvidenceAnswer]:
    answers: list[EvidenceAnswer] = []
    for raw in raw_answers if isinstance(raw_answers, list) else []:
        if not isinstance(raw, dict):
            continue
        answer = str(raw.get("answer") or "").strip()
        if not answer:
            continue
        try:
            relevance = max(0.0, min(1.0, float(raw.get("relevance", 0.0) or 0.0)))
        except (TypeError, ValueError):
            relevance = 0.0
        answers.append(
            EvidenceAnswer(
                answer=answer,
                citation=str(raw.get("citation") or "").strip(),
                url=str(raw.get("url") or "").strip(),
                relevance=relevance,
            )
        )
    answers.sort(key=lambda item: -item.relevance)
    return answers


def _merge_usage(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left or {})
    for key in ("calls", "input_tokens", "output_tokens", "total_tokens", "estimated_calls"):
        merged[key] = int(merged.get(key, 0) or 0) + int((right or {}).get(key, 0) or 0)
    return merged


__all__ = ["EvidenceAnswer", "EvidenceSearchResult", "search_chemistry_literature"]
