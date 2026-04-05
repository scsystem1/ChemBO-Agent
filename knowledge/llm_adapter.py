"""
LLM adapter for Local RAG.

This adapter intentionally reuses the same provider configuration surface as the
main agent, while keeping RAG prompts isolated from graph prompts.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable

from config.settings import Settings


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


def _is_dashscope_model(base_url: str | None, lowered_model_name: str) -> bool:
    return bool(base_url and "dashscope.aliyuncs.com" in base_url.lower() and lowered_model_name.startswith("kimi-k2.5"))


def _resolve_openai_api_key_env(settings: Settings, lowered_model_name: str) -> str:
    if settings.llm_api_key_env:
        return settings.llm_api_key_env
    if _is_dashscope_model(settings.llm_base_url, lowered_model_name):
        return "DASHSCOPE_API_KEY"
    return "OPENAI_API_KEY"


def _openai_compatible_model_kwargs(settings: Settings, lowered_model_name: str) -> dict[str, Any]:
    extra_body: dict[str, Any] = {}
    if settings.llm_enable_thinking is True:
        extra_body["enable_thinking"] = True
    elif settings.llm_enable_thinking is None and _is_dashscope_model(settings.llm_base_url, lowered_model_name):
        extra_body["enable_thinking"] = True
    return {"extra_body": extra_body} if extra_body else {}


def _create_rag_llm(settings: Settings):
    model_name = settings.llm_model.strip()
    lowered = model_name.lower()
    temperature = float(getattr(settings, "rag_llm_temperature", 0.1))
    max_tokens = int(getattr(settings, "rag_llm_max_tokens", 1024))

    if settings.llm_base_url:
        from langchain_openai import ChatOpenAI

        api_key_env = _resolve_openai_api_key_env(settings, lowered)
        api_key = os.getenv(api_key_env)
        if not api_key:
            raise RuntimeError(f"{api_key_env} is not set for the configured endpoint.")
        return ChatOpenAI(
            model=model_name,
            base_url=settings.llm_base_url,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            model_kwargs=_openai_compatible_model_kwargs(settings, lowered),
        )

    if lowered.startswith("claude"):
        from langchain_anthropic import ChatAnthropic

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        return ChatAnthropic(model=model_name, temperature=temperature, max_tokens=max_tokens)

    if lowered.startswith(("gpt", "o1", "o3", "o4")):
        from langchain_openai import ChatOpenAI

        api_key_env = settings.llm_api_key_env or "OPENAI_API_KEY"
        api_key = os.getenv(api_key_env)
        if not api_key:
            raise RuntimeError(f"{api_key_env} is not set for the configured OpenAI model.")
        return ChatOpenAI(model=model_name, api_key=api_key, temperature=temperature, max_tokens=max_tokens)

    raise ValueError(f"Unsupported LLM model/provider for '{model_name}'.")


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

            response = self._llm.invoke([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)])
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
            f"{json.dumps(query_payload, ensure_ascii=False, indent=2)}"
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
            f"{json.dumps(query_payload, ensure_ascii=False, indent=2)}"
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
            f"QUERY:\n{json.dumps(query_payload, ensure_ascii=False, indent=2)}\n\n"
            f"CHUNK:\n{json.dumps(chunk_payload, ensure_ascii=False, indent=2)}"
        )
        return self.invoke_json("compress_chunk", system_prompt, user_prompt, default)

    def rerank_chunks(self, query_payload: dict[str, Any], chunk_payloads: list[dict[str, Any]], default: dict[str, Any]) -> RAGLLMResponse:
        system_prompt = (
            "You are reranking retrieved chemistry evidence. "
            "Return strict JSON with key scores, where scores is a list of objects with chunk_id and score (0-1)."
        )
        user_prompt = (
            "Rerank these retrieved chunks against the query.\n"
            f"QUERY:\n{json.dumps(query_payload, ensure_ascii=False, indent=2)}\n\n"
            f"CHUNKS:\n{json.dumps(chunk_payloads, ensure_ascii=False, indent=2)}"
        )
        return self.invoke_json("rerank_chunks", system_prompt, user_prompt, default)


__all__ = ["RAGLLMAdapter", "RAGLLMResponse"]
