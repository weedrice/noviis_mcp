from __future__ import annotations

import re


# Advisory hints, not a classifier or a guarantee that unflagged content is safe.
# Ordinary discussion of systems/prompts/instructions must remain visible.
_INSTRUCTION_PATTERNS = (
    re.compile(r"\b(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|above|system)\s+(?:instructions?|rules?)\b", re.I),
    re.compile(r"\b(?:reveal|print|send|expose)\s+(?:your|the)\s+(?:system\s+prompt|agent[_ ]token|api[_ ]key)\b", re.I),
    re.compile(r"(?:이전|기존|시스템)\s*(?:지시|명령|규칙)(?:를|을|사항을)?\s*무시"),
)


def content_warnings(text: str) -> list[str]:
    return ["instruction_like_text"] if any(pattern.search(text) for pattern in _INSTRUCTION_PATTERNS) else []
