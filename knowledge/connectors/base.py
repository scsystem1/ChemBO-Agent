"""
Base types and interface for external and wrapped knowledge retrieval connectors.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


VALID_SOURCE_TYPES = {"pubchem", "web", "local_rag"}


@dataclass
class RetrievedChunk:
    """Connector-facing chunk type shared across retrieval backends."""

    content: str
    source_type: str
    source_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    relevance_score: float = 0.0
    query: str = ""

    def __post_init__(self) -> None:
        if self.source_type not in VALID_SOURCE_TYPES:
            raise ValueError(
                f"Invalid source_type '{self.source_type}'. Must be one of {sorted(VALID_SOURCE_TYPES)}."
            )

    @property
    def short_source(self) -> str:
        if self.source_type == "pubchem":
            cid = str(self.metadata.get("cid", "")).strip() or "?"
            return f"PubChem:CID_{cid}"
        if self.source_type == "web":
            url = str(self.metadata.get("url") or self.source_id).strip()
            return url[:80]
        if self.source_type == "local_rag":
            collection = str(self.metadata.get("collection", "")).strip()
            source_file = str(self.metadata.get("source_file", "")).strip()
            locator = f"{collection}/{source_file}".strip("/")
            return f"LocalRAG:{locator}" if locator else self.source_id[:80]
        return self.source_id[:80]


class BaseConnector(ABC):
    """Abstract base class for retrieval connectors."""

    @abstractmethod
    def search(self, query: str, **kwargs: Any) -> list[RetrievedChunk]:
        """Execute a search and return connector chunks.

        Implementations must be fault tolerant and return an empty list on
        network or provider failures.
        """

    @abstractmethod
    def is_available(self) -> bool:
        """Return whether the connector is configured and usable."""
