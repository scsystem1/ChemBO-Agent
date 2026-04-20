"""
Prompt formatting helpers for LLM-facing payloads.
"""
from __future__ import annotations

import json
from typing import Any


def compact_json(payload: Any) -> str:
    """Serialize prompt-facing JSON without pretty-print whitespace."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
