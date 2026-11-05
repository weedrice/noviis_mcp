from __future__ import annotations

import re

from config import WRITE_MAX_CONTENT_CHARS, WRITE_MAX_TITLE_CHARS

# Runs of characters that can represent UTF-8 bytes misdecoded as Latin-1/CP1252.
# A reversible run is only a warning: legitimate quotations may contain it.
_BYTE_TEXT = re.compile(r"[\u0080-\u00ff\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192\u02c6\u02dc\u2013-\u2026\u2030\u2039\u203a\u20ac\u2122]+")


def validate_write_text(field_name: str, value: str) -> None:
    if field_name != "image_alt" and not value.strip():
        raise ValueError(f"{field_name} must not be empty.")
    limit = WRITE_MAX_TITLE_CHARS if field_name == "title" else WRITE_MAX_CONTENT_CHARS
    if len(value) > limit:
        raise ValueError(f"{field_name} must be at most {limit} characters.")
    if looks_corrupted_korean(value):
        raise ValueError(
            f"{field_name} contains a Unicode replacement character or unpaired surrogate. "
            "Read the original text through a verified UTF-8 channel before sending it."
        )


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
