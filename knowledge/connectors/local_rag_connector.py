"""
Adapter that exposes the local RAG store through the connector interface.
"""
from __future__ import annotations

import logging

from config.settings import Settings
from knowledge.connectors.base import BaseConnector, RetrievedChunk
from knowledge.local_rag import LocalRAGStore

logger = logging.getLogger(__name__)


class LocalRAGConnector(BaseConnector):
    """Wrap LocalRAGStore.search() and convert to connector chunks."""

    def __init__(self, store: LocalRAGStore | None = None, settings: Settings | None = None):
        self._settings = settings or Settings()
        self.last_status: dict[str, object] = {"status": "idle", "error_type": "", "message": "", "result_count": 0}
        if store is not None:
            self._store = store
            return
        try:
            self._store = LocalRAGStore(settings=self._settings)
        except Exception as exc:
            logger.warning("Failed to initialize LocalRAGStore for connector use: %s", exc)
            self._store = None

    def is_available(self) -> bool:
        if self._store is None:
            return False
        try:
            stats = self._store.get_stats()
        except Exception:
            return False
        return sum(int(value) for key, value in stats.items() if key != "backend" and isinstance(value, int)) > 0

    def search(
        self,
        query: str,
        top_k: int = 5,
        collections: list[str] | None = None,
        where: dict | None = None,
        **kwargs,
    ) -> list[RetrievedChunk]:
        del kwargs
        if self._store is None:
            self.last_status = {
                "status": "unavailable",
                "error_type": "index_unavailable",
                "message": "Local RAG store is unavailable.",
                "result_count": 0,
            }
            return []
        try:
            result = self._store.search(query=query, top_k=top_k, collections=collections, where=where)
        except Exception as exc:
            logger.warning("Local RAG connector search failed for '%s': %s", str(query)[:80], exc)
            self.last_status = {
                "status": "internal_error",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "result_count": 0,
            }
            return []

        chunks: list[RetrievedChunk] = []
        for rag_chunk in result.chunks:
            metadata = dict(rag_chunk.metadata)
            source_file = str(metadata.get("source_file", "")).strip()
            chunks.append(
                RetrievedChunk(
                    content=rag_chunk.compressed_content or rag_chunk.content,
                    source_type="local_rag",
                    source_id=f"chromadb:{rag_chunk.collection}:{rag_chunk.chunk_id}",
                    metadata={
                        "chunk_id": rag_chunk.chunk_id,
                        "collection": rag_chunk.collection,
                        "source_file": source_file,
                        "source_type_detail": metadata.get("source_type", ""),
                        "source_type": "local_rag",
                        "section_title": metadata.get("section_title", ""),
                        "doi": metadata.get("doi", ""),
                        "reaction_family": metadata.get("reaction_family", ""),
                        "document_title": metadata.get("document_title", ""),
                        "document_id": metadata.get("document_id", source_file),
                        "reaction_id": metadata.get("reaction_id", ""),
                        "has_yield": metadata.get("has_yield", False),
                        "yield_percent": metadata.get("yield_percent"),
                        "ligands_norm": metadata.get("ligands_norm", "[]"),
                        "bases_norm": metadata.get("bases_norm", "[]"),
                        "solvents_norm": metadata.get("solvents_norm", "[]"),
                        "catalysts_norm": metadata.get("catalysts_norm", "[]"),
                        "dense_score": rag_chunk.dense_score,
                        "sparse_score": rag_chunk.sparse_score,
                        "fusion_score": rag_chunk.fusion_score,
                        "rerank_score": rag_chunk.rerank_score,
                        "locator": rag_chunk.source_locator(),
                    },
                    relevance_score=float(rag_chunk.rerank_score or rag_chunk.fusion_score or rag_chunk.dense_score or rag_chunk.sparse_score or 0.0),
                    query=str(query or ""),
                )
            )
        self.last_status = {
            "status": "ok" if chunks else "available_no_result",
            "error_type": "",
            "message": "" if chunks else "Local RAG returned no matching results.",
            "result_count": len(chunks),
        }
        return chunks
