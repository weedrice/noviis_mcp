from __future__ import annotations


_MOJIBAKE_MARKERS = ("\u00c3", "\u00c2", "\u00ec", "\u00ed", "\u00eb", "\u00ea")


def validate_write_text(field_name: str, value: str) -> None:
    if looks_corrupted_korean(value):
        raise ValueError(
            f"{field_name} appears to contain corrupted Korean text. "
            "Use Git Bash, WSL, Unicode escape literals, a verified UTF-8 file, "
            "or another encoding-safe channel instead of a PowerShell raw Hangul here-string."
        )


def looks_corrupted_korean(value: str) -> bool:
    if "\ufffd" in value:
        return True
    has_hangul = any(_is_hangul(char) for char in value)
    if has_hangul:
        return False
    return "??" in value or any(marker in value for marker in _MOJIBAKE_MARKERS)


def _is_hangul(char: str) -> bool:
    codepoint = ord(char)
    return (
        0xAC00 <= codepoint <= 0xD7A3
        or 0x1100 <= codepoint <= 0x11FF
        or 0x3130 <= codepoint <= 0x318F
    )
