"""
Web search connector via Tavily.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from knowledge.connectors.base import BaseConnector, RetrievedChunk

logger = logging.getLogger(__name__)


class WebSearchConnector(BaseConnector):
    """Retrieve web results with extracted page text from Tavily."""

    def __init__(
        self,
        api_key: str = "",
        search_depth: str = "advanced",
        max_results: int = 5,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        client: Any | None = None,
    ):
        self.api_key = str(api_key or "").strip()
        self.search_depth = str(search_depth or "advanced")
        self.max_results = int(max_results or 5)
        self.include_domains = list(include_domains or []) or None
        self.exclude_domains = list(exclude_domains or []) or None
        self._client = client

    def is_available(self) -> bool:
        return bool(self.api_key)

    def search(
        self,
        query: str,
        max_results: int | None = None,
        search_depth: str | None = None,
        include_domains: list[str] | None = None,
        topic: str = "general",
    ) -> list[RetrievedChunk]:
        query = str(query or "").strip()
        if not query or not self.api_key:
            if query and not self.api_key:
                logger.warning("Tavily API key not configured. Skipping web search.")
            return []

        client = self._get_client()
        if client is None:
            return []

        try:
            params: dict[str, Any] = {
                "query": query,
                "search_depth": str(search_depth or self.search_depth),
                "max_results": int(max_results or self.max_results),
                "topic": str(topic or "general"),
            }
            domains = include_domains or self.include_domains
            if domains:
                params["include_domains"] = list(domains)
            if self.exclude_domains:
                params["exclude_domains"] = list(self.exclude_domains)
            response = client.search(**params)
        except Exception as exc:
            logger.warning("Tavily search failed for '%s': %s", query[:80], exc)
            return []

        chunks: list[RetrievedChunk] = []
        for result in response.get("results", []) or []:
            url = str(result.get("url", "")).strip()
            title = str(result.get("title", "")).strip()
            content = str(result.get("content", "") or "").strip()
            raw_content = str(result.get("raw_content", "") or "").strip()
            score = float(result.get("score", 0.0) or 0.0)
            text = raw_content if len(raw_content) > len(content) else content
            if not text:
                continue
            if len(text) > 3000:
                text = text[:3000].rstrip() + "... [truncated]"
            full_content = f"Source: {title}\nURL: {url}\n\n{text}" if title or url else text
            chunks.append(
                RetrievedChunk(
                    content=full_content,
                    source_type="web",
                    source_id=url or title or f"web:{len(chunks)}",
                    metadata={
                        "url": url,
                        "title": title,
                        "domain": _extract_domain(url),
                        "content_length": len(text),
                    },
                    relevance_score=score,
                    query=query,
                )
            )
        return chunks

    def _get_client(self) -> Any | None:
        if self._client is not None:
            return self._client
        try:
            from tavily import TavilyClient
        except ImportError:
            logger.error("tavily-python is not installed. Web search connector is disabled.")
            return None
        try:
            self._client = TavilyClient(api_key=self.api_key)
        except Exception as exc:
            logger.error("Failed to initialize Tavily client: %s", exc)
            return None
        return self._client


def _extract_domain(url: str) -> str:
    try:
        return urlparse(str(url or "")).netloc
    except Exception:
        return ""

