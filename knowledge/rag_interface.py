"""
Local keyword-based RAG interface for curated reaction KB content.
"""
from __future__ import annotations

from typing import Any

from knowledge.reaction_kb import get_reaction_knowledge


class LocalKnowledgeRAG:
    """Phase 1 keyword-overlap retrieval over curated KB content."""

    def __init__(self):
        self.chunks: list[dict[str, Any]] = []

    def load_from_kb(self, reaction_type: str):
        kb = get_reaction_knowledge(reaction_type)
        if not kb:
            return
        for factor in kb.get("key_factors", []):
            self.chunks.append(
                {"text": factor, "tags": ["mechanism", reaction_type], "source": f"KB:{reaction_type}.key_factors"}
            )
        for pitfall in kb.get("common_pitfalls", []):
            self.chunks.append(
                {"text": pitfall, "tags": ["constraint", reaction_type], "source": f"KB:{reaction_type}.pitfalls"}
            )
        for key, value in kb.get("literature_priors", {}).items():
            self.chunks.append(
                {"text": f"{key}: {value}", "tags": ["prior", reaction_type], "source": f"KB:{reaction_type}.literature_priors"}
            )

    def retrieve(self, query: str, tags: list[str] | None = None, top_k: int = 5) -> list[dict[str, Any]]:
        query_words = set(str(query).lower().split())
        scored = []
        for chunk in self.chunks:
            if tags and not any(tag in chunk.get("tags", []) for tag in tags):
                continue
            chunk_words = set(chunk["text"].lower().split())
            overlap = len(query_words & chunk_words)
            scored.append((overlap, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [chunk for score, chunk in scored[:top_k] if score > 0]
