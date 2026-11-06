from __future__ import annotations

import re

from config import WRITE_MAX_CONTENT_CHARS, WRITE_MAX_TITLE_CHARS

# Runs of characters that can represent UTF-8 bytes misdecoded as Latin-1/CP1252.
# A reversible run is only a warning: legitimate quotations may contain it.
_BYTE_TEXT = re.compile(r"[\u0080-\u00ff\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192\u02c6\u02dc\u2013-\u2026\u2030\u2039\u203a\u20ac\u2122]+")
_HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>|<![^>]*>|<\?[^>]*>", re.IGNORECASE | re.MULTILINE | re.ASCII)
_SCRIPT_TAG = re.compile(r"<\s*script[^>]*>.*?</\s*script\s*>", re.IGNORECASE | re.MULTILINE | re.DOTALL | re.ASCII)
_EVENT_HANDLER = re.compile(r"on\w+\s*=", re.IGNORECASE | re.ASCII)
_CONTENT_BOUNDS = {"create_post": (50, 100000), "create_comment": (20, 1000),
                   "create_reply": (20, 1000), "send_note": (1, 5000)}


def validate_write_text(field_name: str, value: str, *, tool_name: str | None = None) -> None:
    """Check the Java DTO contract in UTF-16 units before allocating a challenge.

    Operator limits may narrow the backend maximum but never widen it. Post body
    HTML is handled by the backend's content path; NoHtml applies to its title and
    image alternative text, plus comment/reply/note bodies.
    """
    if field_name != "image_alt" and not value.strip():
        raise ValueError(f"{field_name} must not be empty.")
    if looks_corrupted_korean(value):
        raise ValueError(
            f"{field_name} contains a Unicode replacement character or unpaired surrogate. "
            "Read the original text through a verified UTF-8 channel before sending it."
        )
    minimum, maximum = (0 if field_name == "image_alt" else 1), WRITE_MAX_CONTENT_CHARS
    if field_name == "title":
        maximum = min(WRITE_MAX_TITLE_CHARS, 200)
    elif field_name == "image_alt":
        maximum = min(WRITE_MAX_CONTENT_CHARS, 300)
    elif field_name == "content" and tool_name in _CONTENT_BOUNDS:
        minimum, backend_maximum = _CONTENT_BOUNDS[tool_name]
        maximum = min(WRITE_MAX_CONTENT_CHARS, backend_maximum)
    length = len(value.encode("utf-16-le")) // 2
    if not minimum <= length <= maximum:
        raise ValueError(f"{field_name} must contain {minimum} to {maximum} UTF-16 code units.")
    no_html = field_name in {"title", "image_alt"} or (
        field_name == "content" and tool_name in {"create_comment", "create_reply", "send_note"})
    if no_html and any(pattern.search(value) for pattern in (_SCRIPT_TAG, _EVENT_HANDLER, _HTML_TAG)):
        raise ValueError(f"{field_name} must not contain HTML tags, scripts, or event handlers.")


def looks_corrupted_korean(value: str) -> bool:
    """Compatibility name for definite Unicode damage, independent of language."""
    return "\ufffd" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def write_text_warnings(*values: str | None) -> list[str]:
    """Flag suspicious byte-decoding patterns without rewriting or blocking text."""
    for value in values:
        for match in _BYTE_TEXT.finditer(value or ""):
            for encoding in ("latin-1", "cp1252"):
                try:
                    repaired = match[0].encode(encoding).decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    continue
                if repaired != match[0]:
                    return ["possible_mojibake"]
    return []
