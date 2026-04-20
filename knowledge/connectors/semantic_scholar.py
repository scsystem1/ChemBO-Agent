"""
Semantic Scholar Academic Graph connector.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

from knowledge.connectors.base import BaseConnector, RetrievedChunk

logger = logging.getLogger(__name__)


S2_BASE_URL = "https://api.semanticscholar.org/graph/v1"
S2_SEARCH_ENDPOINT = f"{S2_BASE_URL}/paper/search"
S2_PAPER_ENDPOINT = f"{S2_BASE_URL}/paper"
DEFAULT_FIELDS = "title,abstract,year,citationCount,externalIds,venue,authors,paperId"


class SemanticScholarConnector(BaseConnector):
    """Search literature abstracts and metadata via Semantic Scholar."""

    def __init__(self, api_key: str = "", timeout: float = 15.0):
        self.api_key = str(api_key or "").strip()
        self.timeout = float(timeout)
        self._last_call_time = 0.0
        self._min_interval = 0.12 if self.api_key else 1.05
        self.last_status: dict[str, Any] = {"status": "idle", "error_type": "", "message": "", "result_count": 0}

    def is_available(self) -> bool:
        return bool(self.api_key)

    def search(
        self,
        query: str,
        max_results: int = 5,
        fields: str = DEFAULT_FIELDS,
        year_range: str = "",
        min_citations: int = 0,
    ) -> list[RetrievedChunk]:
        query = str(query or "").strip()
        if not query:
            self.last_status = {
                "status": "available_no_result",
                "error_type": "",
                "message": "Empty query.",
                "result_count": 0,
            }
            return []
        if not self.api_key:
            self.last_status = {
                "status": "unavailable",
                "error_type": "auth_error",
                "message": "Semantic Scholar api_key not configured.",
                "result_count": 0,
            }
            return []

        self._rate_limit()
        params: dict[str, Any] = {
            "query": query,
            "limit": min(max(max_results, 1) * 2, 20),
            "fields": fields,
        }
        if year_range:
            params["year"] = year_range

        try:
            response = requests.get(
                S2_SEARCH_ENDPOINT,
                params=params,
                headers=self._headers(),
                timeout=self.timeout,
            )
            self._last_call_time = time.time()
            if response.status_code == 429:
                logger.warning("Semantic Scholar rate limit hit for query '%s'.", query[:80])
                self.last_status = {
                    "status": "network_error",
                    "error_type": "rate_limit",
                    "message": "Semantic Scholar rate limit hit.",
                    "result_count": 0,
                }
                return []
            if response.status_code != 200:
                logger.warning("Semantic Scholar returned status %s for query '%s'.", response.status_code, query[:80])
                error_type = "auth_error" if response.status_code in {401, 403} else "network_error"
                self.last_status = {
                    "status": error_type,
                    "error_type": error_type,
                    "message": f"Semantic Scholar returned HTTP {response.status_code}.",
                    "result_count": 0,
                }
                return []
            payload = response.json()
        except requests.exceptions.Timeout:
            logger.warning("Semantic Scholar timed out for query '%s'.", query[:80])
            self.last_status = {
                "status": "timeout",
                "error_type": "timeout",
                "message": "Semantic Scholar request timed out.",
                "result_count": 0,
            }
            return []
        except requests.exceptions.RequestException as exc:
            logger.warning("Semantic Scholar request failed for query '%s': %s", query[:80], exc)
            self.last_status = {
                "status": "network_error",
                "error_type": "network_error",
                "message": str(exc),
                "result_count": 0,
            }
            return []
        except ValueError:
            logger.warning("Semantic Scholar returned invalid JSON for query '%s'.", query[:80])
            self.last_status = {
                "status": "internal_error",
                "error_type": "invalid_json",
                "message": "Semantic Scholar returned invalid JSON.",
                "result_count": 0,
            }
            return []
        except Exception as exc:
            logger.warning("Semantic Scholar unexpected failure for query '%s': %s", query[:80], exc)
            self.last_status = {
                "status": "internal_error",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "result_count": 0,
            }
            return []

        chunks: list[RetrievedChunk] = []
        for paper in payload.get("data", []) or []:
            citation_count = int(paper.get("citationCount") or 0)
            if citation_count < int(min_citations or 0):
                continue
            external_ids = paper.get("externalIds") or {}
            doi = str(external_ids.get("DOI", "")).strip()
            title = str(paper.get("title", "Unknown Title")).strip() or "Unknown Title"
            abstract = str(paper.get("abstract", "") or "").strip()
            year = paper.get("year", "")
            venue = str(paper.get("venue", "") or "").strip()
            authors = paper.get("authors") or []
            author_names = [str(author.get("name", "")).strip() for author in authors[:3] if str(author.get("name", "")).strip()]
            authors_str = ", ".join(author_names)
            if len(authors) > 3 and authors_str:
                authors_str += " et al."

            content = f"Title: {title}"
            if abstract:
                content += f"\n\nAbstract: {abstract}"
            else:
                content += "\n\n[Abstract not available in Semantic Scholar]"

            source_id = f"doi:{doi}" if doi else f"s2:{paper.get('paperId', 'unknown')}"
            chunks.append(
                RetrievedChunk(
                    content=content,
                    source_type="semantic_scholar",
                    source_id=source_id,
                    metadata={
                        "title": title,
                        "year": year,
                        "authors": authors_str,
                        "venue": venue,
                        "citation_count": citation_count,
                        "doi": doi,
                        "paper_id": str(paper.get("paperId", "")).strip(),
                        "has_abstract": bool(abstract),
                    },
                    relevance_score=0.0,
                    query=query,
                )
            )
            if len(chunks) >= max(max_results, 1):
                break
        self.last_status = {
            "status": "ok" if chunks else "available_no_result",
            "error_type": "",
            "message": "" if chunks else "Semantic Scholar returned no matching results.",
            "result_count": len(chunks),
        }
        return chunks

    def get_paper_by_doi(self, doi: str) -> RetrievedChunk | None:
        doi = str(doi or "").strip()
        if not doi:
            return None
        self._rate_limit()
        try:
            response = requests.get(
                f"{S2_PAPER_ENDPOINT}/DOI:{doi}",
                params={"fields": DEFAULT_FIELDS},
                headers=self._headers(),
                timeout=self.timeout,
            )
            self._last_call_time = time.time()
            if response.status_code != 200:
                return None
            paper = response.json()
        except Exception as exc:
            logger.warning("Semantic Scholar DOI lookup failed for '%s': %s", doi, exc)
            return None

        title = str(paper.get("title", "") or "").strip()
        abstract = str(paper.get("abstract", "") or "").strip()
        content = f"Title: {title}" if title else "Title: Unknown"
        if abstract:
            content += f"\n\nAbstract: {abstract}"
        return RetrievedChunk(
            content=content,
            source_type="semantic_scholar",
            source_id=f"doi:{doi}",
            metadata={
                "title": title,
                "year": paper.get("year", ""),
                "doi": doi,
                "citation_count": int(paper.get("citationCount") or 0),
            },
            query=f"DOI lookup: {doi}",
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def _rate_limit(self) -> None:
        elapsed = time.time() - self._last_call_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
