"""
Shared LLM factory helpers.
"""
from __future__ import annotations

import json
import os
from typing import Any

import requests
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from config.settings import Settings


def create_chat_llm(settings: Settings, *, temperature: float, max_tokens: int):
    model_name = settings.llm_model.strip()
    lowered = model_name.lower()

    if settings.llm_base_url:
        if _is_sjtu_minimax_model(settings.llm_base_url, lowered):
            api_key_env = _resolve_openai_api_key_env(settings, lowered)
            api_key = os.getenv(api_key_env)
            if not api_key:
                raise RuntimeError(f"{api_key_env} is not set for the configured endpoint.")
            return SJTUMiniMaxChatModel(
                model=model_name,
                base_url=settings.llm_base_url,
                api_key=api_key,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("OpenAI-compatible endpoints require 'langchain-openai'.") from exc

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
        # DashScope exposes Kimi 2.5 thinking via the OpenAI-compatible API.
        extra_body["enable_thinking"] = True
    return {"extra_body": extra_body} if extra_body else {}


def _is_dashscope_model(base_url: str | None, lowered_model_name: str) -> bool:
    return bool(base_url and "dashscope.aliyuncs.com" in base_url.lower() and lowered_model_name.startswith("kimi-k2.5"))


def _is_sjtu_minimax_model(base_url: str | None, lowered_model_name: str) -> bool:
    if not base_url:
        return False
    lowered_base = base_url.lower()
    return "models.sjtu.edu.cn" in lowered_base and lowered_model_name in {"minimax", "minimax-m2.5"}


class SJTUMiniMaxChatModel:
    """Direct chat/completions client for SJTU-hosted MiniMax models."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        temperature: float,
        max_tokens: int,
        timeout: float = 120.0,
        tools: list[Any] | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._tools = list(tools or [])

    def bind_tools(self, tools: list[Any]):
        return SJTUMiniMaxChatModel(
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            timeout=self.timeout,
            tools=list(tools),
        )

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        payload_messages = [_to_openai_message(message) for message in messages]
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        url = f"{self.base_url}/chat/completions"
        tools = [_tool_to_openai_tool(tool) for tool in self._tools] if self._tools else []

        last_error: Exception | None = None
        for model_name in _sjtu_minimax_model_candidates(self.model):
            payload: dict[str, Any] = {
                "model": model_name,
                "messages": payload_messages,
                "stream": False,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"

            try:
                response = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
                if response.status_code >= 400:
                    error_text = response.text[:500]
                    if response.status_code in {400, 404, 422} and model_name != _sjtu_minimax_model_candidates(self.model)[-1]:
                        last_error = RuntimeError(
                            f"SJTU MiniMax call rejected for model={model_name}: "
                            f"status={response.status_code} body={error_text}"
                        )
                        continue
                    response.raise_for_status()
                data = response.json()
                break
            except requests.exceptions.RequestException as exc:
                raise RuntimeError(
                    "Failed to call the SJTU MiniMax endpoint. "
                    "The official docs note this API is only reachable from the campus network or through VPN. "
                    f"url={url} error={type(exc).__name__}: {exc}"
                ) from exc
            except ValueError as exc:
                raise RuntimeError(f"SJTU MiniMax endpoint returned non-JSON content: {response.text[:300]}") from exc
        else:
            raise last_error or RuntimeError("SJTU MiniMax call failed before receiving a response.")

        choice = ((data.get("choices") or [{}])[:1] or [{}])[0]
        message = choice.get("message") or {}
        raw_tool_calls = message.get("tool_calls") or []
        parsed_tool_calls = _parse_tool_calls(raw_tool_calls)
        response_metadata = {
            "id": data.get("id"),
            "model": data.get("model"),
            "usage": data.get("usage"),
            "finish_reason": choice.get("finish_reason"),
        }
        additional_kwargs = {}
        if raw_tool_calls:
            additional_kwargs["tool_calls"] = raw_tool_calls

        return AIMessage(
            content=_normalize_message_content(message.get("content")),
            tool_calls=parsed_tool_calls,
            response_metadata=response_metadata,
            additional_kwargs=additional_kwargs,
        )


def _normalize_message_content(content: Any) -> str | list[Any]:
    if isinstance(content, (str, list)):
        return content
    if content is None:
        return ""
    return str(content)


def _to_openai_message(message: BaseMessage) -> dict[str, Any]:
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": _normalize_message_content(message.content)}
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": _normalize_message_content(message.content)}
    if isinstance(message, ToolMessage):
        payload = {
            "role": "tool",
            "content": _normalize_message_content(message.content),
            "tool_call_id": getattr(message, "tool_call_id", None),
        }
        return {key: value for key, value in payload.items() if value is not None}
    if isinstance(message, AIMessage):
        payload = {
            "role": "assistant",
            "content": _normalize_message_content(message.content),
        }
        raw_tool_calls = message.additional_kwargs.get("tool_calls") if isinstance(message.additional_kwargs, dict) else None
        if raw_tool_calls:
            payload["tool_calls"] = raw_tool_calls
        elif getattr(message, "tool_calls", None):
            payload["tool_calls"] = [_tool_call_to_openai_payload(item, index) for index, item in enumerate(message.tool_calls)]
        return payload
    return {"role": "user", "content": _normalize_message_content(getattr(message, "content", str(message)))}


def _tool_call_to_openai_payload(tool_call: dict[str, Any], index: int) -> dict[str, Any]:
    raw_args = tool_call.get("args", {})
    arguments = raw_args if isinstance(raw_args, str) else json.dumps(raw_args, ensure_ascii=False)
    return {
        "id": tool_call.get("id") or f"call_{index}",
        "type": "function",
        "function": {
            "name": tool_call.get("name"),
            "arguments": arguments,
        },
    }


def _parse_tool_calls(raw_tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for index, item in enumerate(raw_tool_calls or []):
        function_block = item.get("function") or {}
        raw_arguments = function_block.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        except json.JSONDecodeError:
            arguments = {"raw_arguments": raw_arguments}
        if not isinstance(arguments, dict):
            arguments = {"value": arguments}
        parsed.append(
            {
                "name": function_block.get("name"),
                "args": arguments,
                "id": item.get("id") or f"call_{index}",
                "type": "tool_call",
            }
        )
    return parsed


def _tool_to_openai_tool(tool: Any) -> dict[str, Any]:
    try:
        from langchain_core.utils.function_calling import convert_to_openai_tool

        return convert_to_openai_tool(tool)
    except Exception:
        schema = getattr(tool, "args_schema", None)
        parameters: dict[str, Any] = {"type": "object", "properties": {}}
        if schema is not None:
            if hasattr(schema, "model_json_schema"):
                parameters = schema.model_json_schema()
            elif hasattr(schema, "schema"):
                parameters = schema.schema()
        return {
            "type": "function",
            "function": {
                "name": getattr(tool, "name", "tool"),
                "description": getattr(tool, "description", ""),
                "parameters": parameters,
            },
        }


def _sjtu_minimax_model_candidates(model_name: str) -> list[str]:
    lowered = model_name.strip().lower()
    if lowered == "minimax-m2.5":
        return ["minimax-m2.5", "minimax"]
    if lowered == "minimax":
        return ["minimax", "minimax-m2.5"]
    return [model_name]
