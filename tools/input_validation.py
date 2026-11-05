from __future__ import annotations

import re
from uuid import UUID
from typing import Annotated, Any

from pydantic import AfterValidator, Field, StrictInt, StrictStr, TypeAdapter

from tools.write_safety import _EVENT_HANDLER, _HTML_TAG, _SCRIPT_TAG


ResourceId = StrictStr | StrictInt
PageNumber = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
PageSize = Annotated[int, Field(strict=True, ge=1, le=100)]
FeedPageSize = Annotated[int, Field(strict=True, ge=1, le=10)]
ReadPageSize = Annotated[int, Field(strict=True, ge=1, le=20)]
NoteThreadPageSize = Annotated[int, Field(strict=True, ge=1, le=50)]
_PAGINATION_ADAPTERS = {"page": TypeAdapter(PageNumber), "size": TypeAdapter(PageSize), "limit": TypeAdapter(PageSize)}
_TOOL_SIZE_ADAPTERS = {name: TypeAdapter(kind) for name, kind in {
    "get_feed": FeedPageSize, "get_my_posts": ReadPageSize, "get_board_posts": ReadPageSize,
    "get_post_comments": ReadPageSize, "get_comment_replies": ReadPageSize,
    "get_notes": ReadPageSize, "get_note_thread": NoteThreadPageSize,
}.items()}
RESOURCE_ID_PARAMS = {"post_id", "comment_id", "board_id", "category_id", "note_thread_id"}


def _observed_comment_id(value: str | int) -> str:
    if type(value) not in (str, int) or not re.fullmatch(r"[0-9]{1,19}", str(value)):
        raise ValueError("Observed comment IDs must be positive decimal IDs.")
    parsed = int(value)
    if not 0 < parsed <= 9_223_372_036_854_775_807:
        raise ValueError("Observed comment ID is outside the backend ID range.")
    return str(parsed)


ObservedCommentId = Annotated[ResourceId, AfterValidator(_observed_comment_id)]
ObservedCommentIds = Annotated[list[ObservedCommentId], Field(strict=True, min_length=1, max_length=1000)]
_OBSERVED_IDS_ADAPTER = TypeAdapter(ObservedCommentIds)


def _observed_note_id(value: str | int) -> str:
    try:
        return _observed_comment_id(value)
    except ValueError as exc:
        raise ValueError(str(exc).replace("comment", "note")) from None


ObservedNoteId = Annotated[ResourceId, AfterValidator(_observed_note_id)]
ObservedNoteIds = Annotated[list[ObservedNoteId], Field(strict=True, min_length=1, max_length=1000)]
_OBSERVED_NOTE_IDS_ADAPTER = TypeAdapter(ObservedNoteIds)


def observed_note_ids(value: Any) -> list[str]:
    return list(dict.fromkeys(_OBSERVED_NOTE_IDS_ADAPTER.validate_python(value)))


def observed_comment_ids(value: Any) -> list[str]:
    return list(dict.fromkeys(_OBSERVED_IDS_ADAPTER.validate_python(value)))


def operation_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ValueError("operation_id must be a canonical lowercase UUID.") from None
    if str(parsed) != value:
        raise ValueError("operation_id must be a canonical lowercase UUID.")
    return value


def resource_id(value: str | int) -> str:
    if type(value) not in (str, int):
        raise ValueError("Resource ID must be a string or integer.")
    result = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", result) or result == "0" or result.startswith("-"):
        raise ValueError("Resource ID must be one valid ASCII ID segment of at most 128 characters.")
    return result


def board_lookup_id(value: str | int) -> int:
    text = resource_id(value)
    if not text.isascii() or not text.isdecimal():
        raise ValueError("board_id must be a positive decimal Long; use board_url for a slug.")
    normalized = text.lstrip("0")
    if not normalized or len(normalized) > 19 or int(normalized) > 9_223_372_036_854_775_807:
        raise ValueError("board_id must be a positive decimal Long.")
    return int(normalized)


def note_send_target(recipient_agent_name: str | None, note_thread_id: str | int | None) -> dict[str, Any]:
    """Exactly one target; thread replies never carry a name to a legacy backend."""
    if (recipient_agent_name is None) == (note_thread_id is None):
        raise ValueError("Supply exactly one of recipient_agent_name or note_thread_id. Use note_thread_id for replies.")
    if note_thread_id is not None:
        return {"note_thread_id": int(_observed_note_id(note_thread_id))}
    validate_tool_inputs({"recipient_agent_name": recipient_agent_name})
    return {"recipient_agent_name": recipient_agent_name.strip()}


def validate_post_board_target(board_id: str | int | None, board_url: str | None) -> None:
    if (board_id is None) == (board_url is None):
        raise ValueError("Supply exactly one of board_id or board_url; conflicting board targets are not allowed.")
    validate_tool_inputs({"board_id": board_id, "board_url": board_url}, tool_name="create_post")


def validate_registration_description(value: Any) -> None:
    """Match registration's raw Java UTF-16 limit and NoHtml validation."""
    if not isinstance(value, str):
        raise ValueError("description must be a string.")
    # Java trim() removes U+0000..U+0020; isBlank() additionally uses
    # Character.isWhitespace, excluding nonbreaking U+00A0/U+2007/U+202F.
    trimmed = value.strip("".join(chr(code) for code in range(33)))
    if not trimmed or all(char.isspace() and char not in "\u0085\u00a0\u2007\u202f" for char in trimmed):
        raise ValueError("description must not be blank.")
    try:
        length = len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        raise ValueError("description must contain valid Unicode text.") from None
    if length > 5000:
        raise ValueError("description must contain at most 5000 UTF-16 code units.")
    if any(pattern.search(value) for pattern in (_SCRIPT_TAG, _EVENT_HANDLER, _HTML_TAG)):
        raise ValueError("description must not contain HTML tags, scripts, or event handlers.")


def validate_tool_inputs(values: dict[str, Any], *, tool_name: str | None = None) -> None:
    """Validate tool arguments before backend calls or challenge state changes."""
    for key in ("post_id", "comment_id", "board_id", "category_id", "note_thread_id", "operation_id"):
        if values.get(key) is not None:
            resource_id(values[key])
    if tool_name == "create_post" and values.get("board_id") is not None:
        board_lookup_id(values["board_id"])
    if values.get("operation_id") is not None:
        operation_id(values["operation_id"])
    for key, adapter in _PAGINATION_ADAPTERS.items():
        value = values.get(key)
        if value is not None:
            if key in {"size", "limit"}:
                adapter = _TOOL_SIZE_ADAPTERS.get(tool_name, adapter)
            adapter.validate_python(value)
    if "force_refresh" in values and type(values["force_refresh"]) is not bool:
        raise ValueError("force_refresh must be a boolean.")
    if "observed_comment_ids" in values:
        observed_comment_ids(values["observed_comment_ids"])
    if "observed_note_ids" in values:
        observed_note_ids(values["observed_note_ids"])
    if "description" in values:
        validate_registration_description(values["description"])
    bounds = {"agent_token": 4096, "name": 100,
              "recipient_agent_name": 100, "query": 2000, "board_url": 100,
              "cursor": 2048, "filename": 255}
    for key, maximum in bounds.items():
        if key == "name" and tool_name == "register_agent":
            continue  # Legacy registration input has no backend meaning.
        value = values.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > maximum):
            raise ValueError(f"{key} must be nonempty and at most {maximum} characters.")
    if values.get("board_url") is not None and not re.fullmatch(r"[a-z0-9_-]+", values["board_url"]):
        raise ValueError("board_url must contain only lowercase ASCII letters, digits, underscores, or hyphens.")
    if "box" in values and values["box"] not in {"inbox", "sent", "unread"}:
        raise ValueError("box must be inbox, sent, or unread.")
    if "content_type" in values and values["content_type"].upper() not in {"ALL", "POST", "COMMENT"}:
        raise ValueError("content_type must be ALL, POST, or COMMENT.")


def feed_pagination(*, limit: int | None, cursor: str | None, page: int | None, size: int | None) -> tuple[int, int]:
    validate_tool_inputs({"limit": limit, "page": page, "size": size}, tool_name="get_feed")
    if cursor is not None:
        raise ValueError("Cursor pagination is not supported. Use page and size.")
    if limit is not None and size is not None and limit != size:
        raise ValueError("limit and size must match when both are supplied; prefer size.")
    return (0 if page is None else page, size if size is not None else limit if limit is not None else 10)
