"""Small shared helpers (JSON extraction, token estimation)."""

from __future__ import annotations

import json
import re
from typing import Any

_CODE_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def estimate_tokens(text: str) -> int:
    """A provider-agnostic approximation: ~4 characters per token."""

    if not text:
        return 0
    return max(1, len(text) // 4)


def extract_json(text: str) -> Any:
    """Best-effort JSON extraction from a model utterance.

    Tries the whole string, fenced blocks, then the first balanced object.
    Returns ``None`` when nothing parseable exists.
    """

    candidate = text.strip()
    for payload in (candidate, *(_CODE_FENCE.findall(candidate))):
        try:
            return json.loads(payload)
        except (TypeError, ValueError):
            pass

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(candidate[start : end + 1])
        except ValueError:
            return None
    return None


def truncate(text: str, limit: int) -> str:
    """Truncate a string while preserving both ends of a long output."""

    if len(text) <= limit:
        return text
    head = max(1, limit // 2)
    tail = max(1, limit - head - 20)
    return f"{text[:head]}\n...[truncated]...\n{text[-tail:]}"
