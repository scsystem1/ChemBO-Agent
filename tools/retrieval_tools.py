"""
Runtime retrieval tools for on-demand chemistry evidence search.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from langchain_core.tools import tool

from config.settings import Settings
from core.domain import is_hpo_domain
from knowledge.evidence_search import search_chemistry_literature as search_chemistry_literature_impl


def build_retrieval_tools(
    settings: Settings,
    problem_spec: dict[str, Any],
    llm: Any,
    invoke_json: Callable[[Any, str, str, dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]],
) -> list[Any]:
    """Build retrieval tools bound to the current campaign settings and problem."""
    if is_hpo_domain(problem_spec):
        return []
    if not bool(getattr(settings, "knowledge_enabled", False)) or not bool(getattr(settings, "evidence_search_enabled", True)):
        return []

    @tool
    def search_chemistry_literature(question: str, context: str = "") -> str:
        """Ask a specific chemistry question and receive distilled literature evidence.

        Use this for surprise observations, active-card contradictions, stagnation, or
        explicit pending evidence questions. Do not use it for generic textbook facts,
        Bayesian optimization mechanics, or topics already covered by high-confidence cards.
        """
        result = search_chemistry_literature_impl(
            question=str(question or ""),
            context=str(context or ""),
            problem_spec=problem_spec,
            settings=settings,
            llm=llm,
            invoke_json=invoke_json,
        )
        return result.to_compact_json()

    return [search_chemistry_literature]


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


__all__ = ["build_retrieval_tools"]
