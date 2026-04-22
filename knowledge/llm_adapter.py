"""
LLM adapter for Local RAG.

This adapter intentionally reuses the same provider configuration surface as the
main agent, while keeping RAG prompts isolated from graph prompts.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from config.llm_factory import create_chat_llm
from config.settings import Settings
from core.prompt_utils import compact_json


def _extract_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part)
    return str(content or "")


def _extract_json_block(text: str) -> dict[str, Any] | None:
    cleaned = str(text or "").strip()
    if not cleaned:
        return None
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _create_rag_llm(settings: Settings, max_tokens_override: int | None = None):
    temperature = float(getattr(settings, "rag_llm_temperature", 0.1))
    max_tokens = int(max_tokens_override if max_tokens_override is not None else getattr(settings, "rag_llm_max_tokens", 1024))
    return create_chat_llm(settings, temperature=temperature, max_tokens=max_tokens)


@dataclass
class RAGLLMResponse:
    payload: dict[str, Any]
    raw_text: str = ""
    used_fallback: bool = False
    error: str = ""


class RAGLLMAdapter:
    """RAG-specific structured prompting on top of the project's main LLM config."""

    def __init__(
        self,
        settings: Settings | None = None,
        llm: Any | None = None,
        responder: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ):
        self.settings = settings or Settings()
        self._llm = llm
        self._responder = responder
        self._available: bool | None = None

    @property
    def available(self) -> bool:
        if self._available is not None:
            return self._available
        if self._responder is not None or self._llm is not None:
            self._available = True
            return True
        try:
            self._llm = _create_rag_llm(self.settings)
            self._available = True
        except Exception:
            self._available = False
        return self._available

    def invoke_json(
        self,
        task_name: str,
        system_prompt: str,
        user_prompt: str,
        default: dict[str, Any],
        max_tokens_override: int | None = None,
    ) -> RAGLLMResponse:
        if self._responder is not None:
            try:
                return RAGLLMResponse(payload=self._responder(task_name, {"system": system_prompt, "user": user_prompt}))
            except Exception as exc:
                return RAGLLMResponse(payload=default, used_fallback=True, error=f"{type(exc).__name__}: {exc}")

        if not self.available:
            return RAGLLMResponse(payload=default, used_fallback=True, error="LLM unavailable")

        try:
            from langchain_core.messages import HumanMessage, SystemMessage

            llm = self._llm
            if max_tokens_override is not None:
                llm = _create_rag_llm(self.settings, max_tokens_override=max_tokens_override)
            response = llm.invoke([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)])
            raw_text = _extract_text_content(getattr(response, "content", ""))
            parsed = _extract_json_block(raw_text) or default
            return RAGLLMResponse(payload=parsed, raw_text=raw_text, used_fallback=parsed is default)
        except Exception as exc:
            return RAGLLMResponse(payload=default, used_fallback=True, error=f"{type(exc).__name__}: {exc}")

    def expand_query(self, query_payload: dict[str, Any], default: dict[str, Any]) -> RAGLLMResponse:
        system_prompt = (
            "You are expanding a chemistry retrieval query. "
            "Return strict JSON with keys: expanded_terms (list[str]), alternate_queries (list[str]), rationale (str). "
            "Add only terms that improve retrieval for chemistry literature and reaction-condition records."
        )
        user_prompt = (
            "Expand this query for retrieval.\n"
            f"{compact_json(query_payload)}"
        )
        return self.invoke_json("expand_query", system_prompt, user_prompt, default)

    def generate_hyde(self, query_payload: dict[str, Any], default: dict[str, Any]) -> RAGLLMResponse:
        system_prompt = (
            "You are generating a hypothetical evidence paragraph for chemistry retrieval. "
            "Return strict JSON with keys: hypothetical_passage (str), cues (list[str]). "
            "Write 4-8 sentences that look like a literature or experiment description."
        )
        user_prompt = (
            "Generate a retrieval-oriented hypothetical passage for this chemistry query.\n"
            f"{compact_json(query_payload)}"
        )
        return self.invoke_json("generate_hyde", system_prompt, user_prompt, default)

    def compress_chunk(self, query_payload: dict[str, Any], chunk_payload: dict[str, Any], default: dict[str, Any]) -> RAGLLMResponse:
        system_prompt = (
            "You are compressing a retrieved chemistry document chunk into evidence. "
            "Return strict JSON with keys: compressed_snippet (str), kept_points (list[str]). "
            "Keep 1-3 sentences, preserve factual wording, and do not invent unsupported claims."
        )
        user_prompt = (
            "Compress this chunk into the most relevant evidence for the query.\n"
            f"QUERY:\n{compact_json(query_payload)}\n\n"
            f"CHUNK:\n{compact_json(chunk_payload)}"
        )
        return self.invoke_json("compress_chunk", system_prompt, user_prompt, default)

    def rerank_chunks(self, query_payload: dict[str, Any], chunk_payloads: list[dict[str, Any]], default: dict[str, Any]) -> RAGLLMResponse:
        system_prompt = (
            "You are reranking retrieved chemistry evidence. "
            "Return strict JSON with key scores, where scores is a list of objects with chunk_id and score (0-1)."
        )
        user_prompt = (
            "Rerank these retrieved chunks against the query.\n"
            f"QUERY:\n{compact_json(query_payload)}\n\n"
            f"CHUNKS:\n{compact_json(chunk_payloads)}"
        )
        return self.invoke_json("rerank_chunks", system_prompt, user_prompt, default)


__all__ = ["RAGLLMAdapter", "RAGLLMResponse"]
